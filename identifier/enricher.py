"""
Lazy enrichment of matcher result dicts with variant and set-total data.

The TCGdex listSync() endpoint returns CardResume objects that do NOT include
variant flags (normal / reverse / holo / firstEdition / wPromo) or the total
card count for the set.  Both require a full sdk.card.getSync() fetch.

To avoid 22k individual API calls at setup time, we fetch the full Card object
the first time a card_id is seen at identify time and cache the result in the
DB (variants TEXT column and set_total TEXT column).  Subsequent identifies of
the same card read directly from the DB — zero API calls.

Public API
----------
enrich_result(result_dict)  →  result_dict (modified in-place, also returned)
    Adds keys:
        "variants"   : dict | None
            {"normal": bool, "reverse": bool, "holo": bool,
             "firstEdition": bool, "wPromo": bool}
        "set_total"  : str | None   e.g. "102"
"""

import json

import requests as _requests

from config import TCGDEX_LANGUAGE, IMAGE_QUALITY
from db.database import get_card_by_id, update_card_details

# Sentinel so we only try the API once per card_id per process lifetime even
# if the API call fails (avoids hammering the endpoint on every identify).
_failed_ids: set[str] = set()


def _fetch_full_card(card_id: str) -> dict | None:
    """
    Fetch the full Card object from TCGdex and extract variants + set_total.
    Returns a dict with keys 'variants_json' (str) and 'set_total' (str|None),
    or None if the fetch fails or the card is not found.
    """
    try:
        from tcgdexsdk import TCGdex
        sdk = TCGdex(TCGDEX_LANGUAGE)
        card = sdk.card.getSync(card_id)
        if card is None:
            return None

        # ── variants ─────────────────────────────────────────────────────────
        # TCGdex SDK exposes a Variants object on the full Card.
        # Attribute names: normal, reverse, holo, firstEdition, wPromo
        variants_obj = getattr(card, "variants", None)
        if variants_obj is not None:
            variants = {
                "normal":       bool(getattr(variants_obj, "normal",       False)),
                "reverse":      bool(getattr(variants_obj, "reverse",      False)),
                "holo":         bool(getattr(variants_obj, "holo",         False)),
                "firstEdition": bool(getattr(variants_obj, "firstEdition", False)),
                "wPromo":       bool(getattr(variants_obj, "wPromo",       False)),
            }
            variants_json = json.dumps(variants)
        else:
            variants_json = None

        # ── set_total ─────────────────────────────────────────────────────────
        # card.set.cardCount.total (int) — total cards printed in the set
        set_total = None
        set_obj = getattr(card, "set", None)
        if set_obj is not None:
            card_count = getattr(set_obj, "cardCount", None)
            if card_count is not None:
                total = getattr(card_count, "total", None)
                if total is not None:
                    set_total = str(total)

        # ── types ─────────────────────────────────────────────────────────────
        types = getattr(card, "types", None)
        types_json = json.dumps(types) if types else None

        # ── variants_detailed (REST-only — SDK does not expose it) ────────────
        variants_detailed_json = None
        try:
            r = _requests.get(
                f"https://api.tcgdex.net/v2/en/cards/{card_id}", timeout=8
            )
            if r.status_code == 200:
                vd = r.json().get("variants_detailed")
                if vd:
                    variants_detailed_json = json.dumps(vd)
        except Exception:
            pass

        return {
            "variants_json":          variants_json,
            "set_total":              set_total,
            "types_json":             types_json,
            "variants_detailed_json": variants_detailed_json,
        }

    except Exception:
        return None


def enrich_result(result: dict) -> dict:
    """
    Add 'variants' (dict|None) and 'set_total' (str|None) to a matcher result.

    1. If the DB already has variants + set_total cached → use them instantly.
    2. Otherwise, call TCGdex getSync once, write to DB, then add to result.
    3. If the API fetch fails, both keys are set to None and the failure is
       remembered for this process lifetime (no repeated API hammering).

    Modifies result in-place and also returns it for convenience.
    """
    card_id = result.get("card_id")
    if not card_id:
        result["variants"] = None
        result["set_total"] = None
        return result

    # ── Check DB cache first ──────────────────────────────────────────────────
    row = get_card_by_id(card_id)
    if row is not None:
        cached_variants          = row["variants"]          if "variants"          in row.keys() else None
        cached_total             = row["set_total"]         if "set_total"         in row.keys() else None
        cached_types             = row["types"]             if "types"             in row.keys() else None
        cached_variants_detailed = row["variants_detailed"] if "variants_detailed" in row.keys() else None

        if cached_variants is not None or cached_total is not None:
            # At least one field is cached — use whatever we have
            try:
                result["variants"] = json.loads(cached_variants) if cached_variants else None
            except (json.JSONDecodeError, TypeError):
                result["variants"] = None
            result["set_total"] = cached_total

            # types / variants_detailed may have been added later — re-fetch if missing
            needs_refetch = (
                (cached_types is None or cached_variants_detailed is None)
                and card_id not in _failed_ids
            )
            if needs_refetch:
                fetched = _fetch_full_card(card_id)
                if fetched:
                    cached_types             = fetched.get("types_json")             or cached_types
                    cached_variants_detailed = fetched.get("variants_detailed_json") or cached_variants_detailed
                    update_card_details(card_id, cached_variants, cached_total,
                                        cached_types, cached_variants_detailed)
                else:
                    _failed_ids.add(card_id)

            result["types"] = cached_types or result.get("types", "")
            try:
                result["variants_detailed"] = (
                    json.loads(cached_variants_detailed) if cached_variants_detailed else None
                )
            except (json.JSONDecodeError, TypeError):
                result["variants_detailed"] = None
            return result

    # ── Cache miss — fetch from API (unless a previous attempt failed) ────────
    if card_id in _failed_ids:
        result["variants"] = None
        result["set_total"] = None
        return result

    fetched = _fetch_full_card(card_id)
    if fetched is None:
        _failed_ids.add(card_id)
        result["variants"] = None
        result["set_total"] = None
        result["variants_detailed"] = None
        return result

    variants_json          = fetched["variants_json"]
    set_total              = fetched["set_total"]
    types_json             = fetched.get("types_json")
    variants_detailed_json = fetched.get("variants_detailed_json")

    # Persist to DB so subsequent identifies skip the API call
    update_card_details(card_id, variants_json, set_total, types_json, variants_detailed_json)

    try:
        result["variants"] = json.loads(variants_json) if variants_json else None
    except (json.JSONDecodeError, TypeError):
        result["variants"] = None
    result["set_total"] = set_total
    result["types"]     = types_json or result.get("types", "")
    try:
        result["variants_detailed"] = (
            json.loads(variants_detailed_json) if variants_detailed_json else None
        )
    except (json.JSONDecodeError, TypeError):
        result["variants_detailed"] = None

    return result
