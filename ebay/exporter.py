"""
eBay bulk-upload CSV exporter for the Pokemon Card Identifier.

Generates a CSV file compatible with eBay's File Exchange / bulk listing tool
(Canada site, CAD, category 183454 – Pokemon TCG Singles).

Row 1 : version/template header  (required by eBay)
Row 2 : column headers
Row 3+: one data row per batch entry
"""

import csv
import json
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# TCGdex language code → eBay language display string
# ---------------------------------------------------------------------------
_LANGUAGE_MAP: dict[str, str] = {
    "en": "English",
    "fr": "French",
    "de": "German",
    "es": "Spanish",
    "it": "Italian",
    "pt": "Portuguese",
    "ja": "Japanese",
    "ko": "Korean",
    "zh": "Chinese",
    "zh-tw": "Chinese",
}

# ---------------------------------------------------------------------------
# eBay condition mapping
# App condition string → (ConditionID, CD:Card Condition display string)
# ---------------------------------------------------------------------------
_CONDITION_MAP: dict[str, tuple[str, str]] = {
    "Near Mint":         ("4000", "Near mint or better - (ID: 400010)"),
    "Lightly Played":    ("4000", "Near mint or better - (ID: 400010)"),
    "Moderately Played": ("3000", "Very good - (ID: 300010)"),
    "Heavily Played":    ("2500", "Good - (ID: 250010)"),
    "Damaged":           ("1000", "Acceptable - (ID: 100010)"),
}

# ---------------------------------------------------------------------------
# Finish mapping  (app finish_var → eBay *C:Finish vocabulary)
# ---------------------------------------------------------------------------
_FINISH_MAP: dict[str, str] = {
    "Non-Holo":        "Non-Holo",
    "Reverse Holo":    "Reverse Holofoil",
    "Holo":            "Holofoil",
    "Poke Ball Holo":  "Holofoil",
    "Master Ball Holo": "Holofoil",
}

# ---------------------------------------------------------------------------
# Column header row (row 2 of the CSV).
# The Action column header embeds the site parameters — must match exactly.
# ---------------------------------------------------------------------------
def _action_header(site_params: str) -> str:
    return f"*Action({site_params})"


# Full ordered column list.  Order must never change — eBay parses by position.
_COLUMNS = [
    "__action__",           # replaced at write-time with the site-params header
    "CustomLabel",
    "*Category",
    "StoreCategory",
    "*Title",
    "Subtitle",
    "Relationship",
    "RelationshipDetails",
    "ScheduleTime",
    "*ConditionID",
    "CD:Professional Grader - (ID: 27501)",
    "CD:Grade - (ID: 27502)",
    "CDA:Certification Number - (ID: 27503)",
    "CD:Card Condition - (ID: 40001)",
    "*C:Game",
    "*C:Card Name",
    "*C:Character",
    "C:Grade",
    "*C:Card Type",
    "*C:Speciality",
    "C:Age Level",
    "*C:Set",
    "*C:Rarity",
    "*C:Features",
    "*C:Finish",
    "*C:Attribute/MTG:Color",
    "*C:Manufacturer",
    "C:Creature/Monster Type",
    "C:Autographed",
    "*C:Card Number",
    "*C:Language",
    "C:Card Size",
    "C:Year Manufactured",
    "C:Graded",
    "C:Stage",
    "C:Professional Grader",
    "C:Card Condition",
    "C:Material",
    "C:Vintage",
    "C:Country/Region of Manufacture",
    "C:Signed By",
    "C:Convention/Event",
    "C:Franchise",
    "C:Autograph Format",
    "C:Autograph Authentication",
    "C:Certification Number",
    "C:Illustrator",
    "C:HP",
    "C:Attack/Power",
    "C:Defense/Toughness",
    "C:Cost",
    "C:Autograph Authentication Number",
    "PicURL",
    "GalleryType",
    "VideoID",
    "*Description",
    "*Format",
    "*Duration",
    "*StartPrice",
    "BuyItNowPrice",
    "BestOfferEnabled",
    "BestOfferAutoAcceptPrice",
    "MinimumBestOfferPrice",
    "*Quantity",
    "ImmediatePayRequired",
    "*Location",
    "ShippingType",
    "ShippingService-1:Option",
    "ShippingService-1:Cost",
    "ShippingService-2:Option",
    "ShippingService-2:Cost",
    "*DispatchTimeMax",
    "PromotionalShippingDiscount",
    "ShippingDiscountProfileID",
    "DomesticRateTable",
    "*ReturnsAcceptedOption",
    "ReturnsWithinOption",
    "RefundOption",
    "ShippingCostPaidByOption",
    "AdditionalDetails",
    "ShippingProfileName",
    "ReturnProfileName",
    "PaymentProfileName",
    "Product Safety Pictograms",
    "Product Safety Statements",
    "Product Safety Component",
    "Regulatory Document Ids",
    "Manufacturer Name",
    "Manufacturer AddressLine1",
    "Manufacturer AddressLine2",
    "Manufacturer City",
    "Manufacturer Country",
    "Manufacturer PostalCode",
    "Manufacturer StateOrProvince",
    "Manufacturer Phone",
    "Manufacturer Email",
    "Manufacturer ContactURL",
    "Responsible Person 1",
    "Responsible Person 1 Type",
    "Responsible Person 1 AddressLine1",
    "Responsible Person 1 AddressLine2",
    "Responsible Person 1 City",
    "Responsible Person 1 Country",
    "Responsible Person 1 PostalCode",
    "Responsible Person 1 StateOrProvince",
    "Responsible Person 1 Phone",
    "Responsible Person 1 Email",
    "Responsible Person 1 ContactURL",
]


def _parse_types(types_raw: Any) -> str:
    """Return the first Pokemon type string from the DB types field.

    The DB stores types as a JSON array string (e.g. '["Fire"]') or plain text.
    eBay's *C:Card Type column expects a single type string.
    """
    if not types_raw:
        return ""
    if isinstance(types_raw, str):
        try:
            parsed = json.loads(types_raw)
            if isinstance(parsed, list) and parsed:
                return str(parsed[0])
        except (json.JSONDecodeError, ValueError):
            return types_raw.strip()
    return str(types_raw)


def _pic_url(candidate: dict, settings: dict) -> str:
    """Build the PicURL value for a row.

    Priority:
      1. imgbb URL override (set by export_csv after a successful upload)
      2. User-hosted scan image: pic_url_base + scan filename (if base is set)
      3. TCGdex reference image (if tcgdex_pic_fallback is enabled)
      4. Empty string
    """
    # 1. imgbb upload result injected by export_csv
    override = (settings.get("_imgbb_url_override") or "").strip()
    if override:
        return override

    # 2. Manual self-hosted base URL
    base = (settings.get("ebay_pic_url_base") or "").strip().rstrip("/")
    if base:
        scan_path = candidate.get("local_image_path") or candidate.get("image_path", "")
        if scan_path:
            filename = Path(scan_path).name
            return f"{base}/{filename}"

    # 3. TCGdex reference image fallback (disabled by default — eBay doesn't
    #    reliably transload TCGdex URLs)
    if settings.get("ebay_tcgdex_pic_fallback") in (True, "true", "True", 1, "1"):
        img = (candidate.get("image_url") or "").strip().rstrip("/")
        if img:
            # image_url may already be the full URL (e.g. ".../high.png") or
            # just the base path — only append the quality suffix if needed.
            if not img.endswith(".png") and not img.endswith(".jpg"):
                img = f"{img}/high.png"
            return img

    return ""


def _build_description(candidate: dict, cond: str, template: str,
                       set_name: str | None = None) -> str:
    """Render the HTML description template with card-specific values.

    Uses simple token replacement instead of str.format() so that any { }
    braces in CSS, inline styles, or JavaScript within the template don't
    cause a KeyError or ValueError.

    set_name overrides candidate["set_name"] when the user has manually edited
    the Set field in the GUI.
    """
    number = candidate.get("number") or ""
    set_total = candidate.get("set_total") or ""
    fmt_number = f"{number}/{set_total}" if number and set_total else number
    values = {
        "name":      candidate.get("name") or "",
        "set":       set_name if set_name is not None else (candidate.get("set_name") or ""),
        "number":    fmt_number,
        "rarity":    candidate.get("rarity") or "",
        "condition": cond,
    }
    result = template
    for key, val in values.items():
        result = result.replace(f"{{{key}}}", val)
    return result


def build_row(
    candidate: dict,
    widgets: dict,
    scan_path: str,
    row_number: int,
    settings: dict,
) -> dict:
    """Build one eBay CSV data row dict from a batch row's data.

    Parameters
    ----------
    candidate   : the active candidate dict from the batch row
    widgets     : row.widgets dict (contains StringVars for user-entered fields)
    scan_path   : row.image_path (the original scan file)
    row_number  : row.row_number (1-based)
    settings    : dict loaded from settings.json (via config._load_settings())
    """
    # --- User-entered widget values ---
    label    = widgets["label_var"].get().strip()
    finish   = widgets["finish_var"].get()
    edition  = widgets["edition_var"].get()
    cond     = widgets["cond_var"].get()
    qty      = widgets["qty_var"].get()
    price    = widgets["price_var"].get()
    desc_override = widgets.get("desc_var") and widgets["desc_var"].get().strip()
    # Set name may have been manually edited by the user
    set_name = widgets["set_var"].get().strip() if widgets.get("set_var") else (candidate.get("set_name") or "")

    # --- Derived values ---
    cond_id, cond_display = _CONDITION_MAP.get(cond, ("4000", "Near mint or better - (ID: 400010)"))
    ebay_finish = _FINISH_MAP.get(finish, finish)
    card_type = _parse_types(candidate.get("types", ""))

    # Title from widget (already built by _build_title)
    title = (widgets.get("title_var") and widgets["title_var"].get()) or ""

    # Zero-pad label row number portion to 3 digits if label ends with -N
    # e.g. "BatchName-1" → "BatchName-001"
    if "-" in label:
        prefix, _, suffix = label.rpartition("-")
        if suffix.isdigit():
            label = f"{prefix}-{suffix.zfill(3)}"
    elif label.isdigit():
        label = label.zfill(3)

    # Use the template from settings; fall back to the full HTML default from config
    from config import _EBAY_DEFAULTS as _EDEFS
    _default_template = _EDEFS.get("ebay_description_template",
                                    "Pokemon card {name} from {set}, #{number}, {rarity}, {condition}.")
    description = desc_override or _build_description(
        candidate, cond,
        settings.get("ebay_description_template") or _default_template,
        set_name=set_name,
    )

    # Candidate dict has image_path for PicURL resolution
    candidate_with_scan = dict(candidate)
    candidate_with_scan["image_path"] = scan_path
    pic_url = _pic_url(candidate_with_scan, settings)

    best_offer = "1" if settings.get("ebay_best_offer_enabled", True) else "0"

    row: dict[str, str] = {col: "" for col in _COLUMNS}
    row.update({
        "__action__":                      "Add",
        "CustomLabel":                     label,
        "*Category":                       settings.get("ebay_category_id", "183454"),
        "StoreCategory":                   settings.get("ebay_store_category", "0"),
        "*Title":                          title,
        "*ConditionID":                    cond_id,
        "CD:Card Condition - (ID: 40001)": cond_display,
        "*C:Game":                         "Pokémon TCG",
        "*C:Card Name":                    candidate.get("name") or "",
        "*C:Character":                    candidate.get("name") or "",
        "*C:Card Type":                    card_type,
        "*C:Set":                          set_name,
        "*C:Rarity":                       candidate.get("rarity") or "",
        "*C:Finish":                       ebay_finish,
        "*C:Manufacturer":                 "The Pokémon Company",
        "*C:Card Number":                  (candidate.get("number") or "").split("/")[0].strip(),
        "*C:Language":                     _LANGUAGE_MAP.get(
                                               settings.get("ebay_language",
                                                            __import__("config").TCGDEX_LANGUAGE),
                                               "English"),
        "C:HP":                            candidate.get("hp") or "",
        "PicURL":                          pic_url,
        "*Description":                    description,
        "*Format":                         "FixedPrice",
        "*Duration":                       "GTC",
        "*StartPrice":                     price,
        "BuyItNowPrice":                   "0",
        "BestOfferEnabled":                best_offer,
        "*Quantity":                       qty,
        "ImmediatePayRequired":            "",
        "*Location":                       settings.get("ebay_location", ""),
        "*DispatchTimeMax":                settings.get("ebay_dispatch_days", "1"),
        "ShippingProfileName":             settings.get("ebay_shipping_profile", ""),
        "ReturnProfileName":               settings.get("ebay_return_profile", ""),
        "PaymentProfileName":              settings.get("ebay_payment_profile", ""),
    })

    # 1st Edition: append to C:Features when applicable
    if edition and edition != "Unlimited":
        row["*C:Features"] = "1st Edition"

    return row


def _upload_scans_to_imgbb(
    batch_rows: list,
    api_key: str,
    progress_callback=None,
) -> dict[str, str]:
    """Upload all unique scan images to imgbb and return {local_path: url}.

    Only uploads scans that have a valid local image path. Skips rows without
    a scan. Images expire after 24 hours.
    """
    from ebay.imgbb_uploader import upload_image

    # Collect unique (path, label) pairs for both front and back images.
    entries: list[tuple[Path, str]] = []
    seen: set[str] = set()
    for br in batch_rows:
        label = (br.widgets.get("label_var") and
                 br.widgets["label_var"].get().strip()) or ""
        for img_path, suffix in [
            (br.image_path,      ""),
            (getattr(br, "back_image_path", ""), "-back"),
        ]:
            if img_path and str(img_path) not in seen:
                path_obj = Path(img_path)
                if path_obj.exists():
                    upload_name = (label + suffix) if label else (path_obj.stem + suffix)
                    entries.append((path_obj, upload_name))
                    seen.add(str(img_path))

    url_map: dict[str, str] = {}
    total = len(entries)
    for i, (path, upload_name) in enumerate(entries):
        try:
            url = upload_image(path, api_key, name=upload_name)
            url_map[str(path.resolve())] = url
            url_map[str(path)] = url
            print(f"[imgbb] Uploaded '{upload_name}' ({path.name}) → {url}")
        except Exception as exc:
            print(f"[imgbb] Failed to upload {path.name}: {exc}")
        if progress_callback:
            progress_callback(i + 1, total)

    return url_map


def export_csv(
    batch_rows: list,
    output_path: str | Path,
    settings: dict,
    progress_callback=None,
) -> int:
    """Write the eBay CSV file and return the number of data rows written.

    Parameters
    ----------
    batch_rows        : list of BatchRow dataclass instances from the GUI
    output_path       : destination file path
    settings          : dict from config._load_settings()
    progress_callback : optional callable(done: int, total: int) for upload progress
    """
    output_path = Path(output_path)
    site_params = settings.get("ebay_site_params",
                               "SiteID=Canada|Country=CA|Currency=CAD|Version=1193|CC=UTF-8")

    # ── imgbb auto-upload ──────────────────────────────────────────────────
    imgbb_url_map: dict[str, str] = {}
    api_key = (settings.get("ebay_imgbb_api_key") or "").strip()
    auto_upload = settings.get("ebay_imgbb_auto_upload") in (True, "true", "True", 1, "1")
    if auto_upload and api_key:
        imgbb_url_map = _upload_scans_to_imgbb(batch_rows, api_key, progress_callback)

    # Build the real header list (replace __action__ placeholder)
    headers = [_action_header(site_params) if c == "__action__" else c for c in _COLUMNS]

    rows_written = 0
    with output_path.open("w", newline="", encoding="utf-8-sig") as fh:
        # Row 1: eBay version/template header
        fh.write("Info,Version=1.0.0,Template=fx_category_template_EBAY_ENCA\n")

        writer = csv.DictWriter(fh, fieldnames=headers, quoting=csv.QUOTE_ALL,
                                extrasaction="ignore")
        writer.writeheader()

        for br in batch_rows:
            if not br.candidates:
                continue
            top = br.candidates[br.current_idx]

            # If we uploaded this scan to imgbb, inject the URL into settings
            # for _pic_url() to pick up via the pic_url_base mechanism, or
            # pass it directly via a special override key in settings.
            row_settings = dict(settings)
            if br.image_path:
                # Normalise to resolved absolute path for lookup, fall back to raw string
                _key = str(Path(br.image_path).resolve())
                _front_url = imgbb_url_map.get(_key) or imgbb_url_map.get(str(br.image_path))

                # Also look up back image URL (if front/back mode was used)
                _back_url = ""
                _back_path = getattr(br, "back_image_path", "") or ""
                if _back_path:
                    _bkey = str(Path(_back_path).resolve())
                    _back_url = imgbb_url_map.get(_bkey) or imgbb_url_map.get(str(_back_path)) or ""

                # Pipe-join front|back when both are available, otherwise use whichever exists
                if _front_url and _back_url:
                    row_settings["_imgbb_url_override"] = f"{_front_url}|{_back_url}"
                elif _front_url:
                    row_settings["_imgbb_url_override"] = _front_url

            data_row = build_row(
                candidate=top,
                widgets=br.widgets,
                scan_path=br.image_path,
                row_number=br.row_number,
                settings=row_settings,
            )
            # Remap __action__ key to the real header key
            action_key = _action_header(site_params)
            data_row[action_key] = data_row.pop("__action__", "Add")
            writer.writerow(data_row)
            rows_written += 1

    return rows_written
