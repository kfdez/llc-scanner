"""prices/fetcher.py — Live market price fetching for LLC Scanner.

Fetches pricing from the TCGdex REST API (which aggregates TCGPlayer USD and
CardMarket EUR data) and converts to CAD using the Frankfurter forex API.

No API keys required. Forex rates and card pricing are cached per session.
"""

import requests

TCGDEX_CARD_URL = "https://api.tcgdex.net/v2/en/cards/{}"
FRANKFURTER_URL = "https://api.frankfurter.app/latest?from={}&to=CAD"

# Finish label → preferred TCGdex tcgplayer variant key
_FINISH_TO_TCGP: dict[str, str] = {
    "Holo":             "holofoil",
    "Poke Ball Holo":   "holofoil",
    "Master Ball Holo": "holofoil",
    "Reverse Holo":     "reverseHolofoil",
    "Non-Holo":         "normal",
}


def _finish_label_to_tcgp(finish: str) -> str:
    """Map a finish label (possibly with subtype suffix) to a TCGPlayer variant key.

    Strips any parenthetical suffix before lookup so granular labels like
    ``"Holo (Shadowless)"`` or ``"Holo (1st Ed)"`` all resolve to ``"holofoil"``.
    """
    base = finish.split("(")[0].strip()   # "Holo (Shadowless)" → "Holo"
    return _FINISH_TO_TCGP.get(base, _FINISH_TO_TCGP.get(finish, "normal"))

# Fallback order when the preferred variant has no TCGPlayer price.
# Ordered ascending by typical value so we err on the conservative side.
_TCGP_FALLBACK_ORDER = ["normal", "reverseHolofoil", "holofoil"]

# Module-level caches — populated once per session.
_forex_cache:   dict[str, float] = {}   # currency -> CAD rate
_pricing_cache: dict[str, dict]  = {}   # card_id  -> raw pricing dict


def _get_rate(currency: str) -> float | None:
    """Return the CAD exchange rate for *currency* (e.g. 'USD', 'EUR').

    Result is cached for the lifetime of the process. Returns None on error.
    """
    if currency in _forex_cache:
        return _forex_cache[currency]
    try:
        r = requests.get(FRANKFURTER_URL.format(currency), timeout=5)
        r.raise_for_status()
        rate = r.json()["rates"]["CAD"]
        _forex_cache[currency] = rate
        return rate
    except Exception:
        return None


def fetch_price(card_id: str, finish: str) -> tuple[float | None, str | None]:
    """Fetch the market price for a card in CAD, given its TCGdex ID and finish.

    Priority:
    1. TCGPlayer USD market price for the matching variant → converted to CAD.
       If the exact variant is missing, falls back through available variants
       (normal → reverseHolofoil → holofoil) rather than jumping straight to
       CardMarket, since TCGPlayer data is generally more accurate.
    2. CardMarket EUR average price → converted to CAD.

    Pricing JSON is cached per card_id so repeated calls during a session
    (e.g. when cycling candidates) do not hit the network again.

    Returns:
        (price_cad, source_label)  if a price was found
        (None, None)               if no price data is available or on any error

    source_label examples:
        "TCGPlayer holofoil (USD->CAD)"
        "TCGPlayer normal (USD->CAD)"
        "CardMarket (EUR->CAD)"
    """
    if not card_id:
        return None, None

    # Fetch and cache the pricing block for this card
    if card_id not in _pricing_cache:
        try:
            r = requests.get(TCGDEX_CARD_URL.format(card_id), timeout=8)
            r.raise_for_status()
            _pricing_cache[card_id] = r.json().get("pricing") or {}
        except Exception:
            return None, None
    pricing = _pricing_cache[card_id]

    # ── TCGPlayer (USD) ───────────────────────────────────────────────────────
    tcgp   = pricing.get("tcgplayer") or {}
    wanted = _finish_label_to_tcgp(finish)

    # Try the preferred variant first, then fall back through the priority list
    others = [v for v in _TCGP_FALLBACK_ORDER if v != wanted]
    for variant_key in [wanted] + others:
        variant_data = tcgp.get(variant_key) or {}
        usd_price = variant_data.get("marketPrice") or variant_data.get("midPrice")
        if usd_price:
            rate = _get_rate("USD")
            if rate:
                return round(float(usd_price) * rate, 2), f"TCGPlayer {variant_key} (USD->CAD)"

    # ── CardMarket (EUR) fallback ─────────────────────────────────────────────
    cm = pricing.get("cardmarket") or {}
    _holo_base = finish.split("(")[0].strip()
    if _holo_base in ("Holo", "Poke Ball Holo", "Master Ball Holo"):
        eur_price = cm.get("avg-holo") or cm.get("avg")
    else:
        eur_price = cm.get("avg")

    if eur_price:
        rate = _get_rate("EUR")
        if rate:
            return round(float(eur_price) * rate, 2), "CardMarket (EUR->CAD)"

    return None, None
