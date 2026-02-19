"""
Bulk card metadata and image downloader using the TCGdex API.

Run via:  python main.py --setup
or call:  download_all(progress_callback=...)
"""

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
from tcgdexsdk import TCGdex, Query
from tqdm import tqdm

import config as _config
from config import (
    TCGDEX_LANGUAGE,
    DOWNLOAD_WORKERS,
    DOWNLOAD_TIMEOUT,
    IMAGE_QUALITY,
)
from db.database import (
    init_db,
    upsert_cards_batch,
    get_cards_without_images,
    get_cards_without_image_url,
    get_cards_without_set_name,
    update_local_image_path,
    update_image_url,
    update_card_full_metadata,
)


def _fetch_all_card_summaries(progress_callback=None) -> list:
    """Page through the TCGdex card list and return all card summary objects."""
    sdk = TCGdex(TCGDEX_LANGUAGE)
    all_cards = []
    page = 1
    items_per_page = 250

    while True:
        query = Query().paginate(page=page, itemsPerPage=items_per_page)
        batch = sdk.card.listSync(query)

        if not batch:
            break

        all_cards.extend(batch)

        if progress_callback:
            progress_callback(f"Fetched metadata page {page} ({len(all_cards)} cards so far)...")

        if len(batch) < items_per_page:
            break

        page += 1
        time.sleep(0.1)  # be polite to the API

    return all_cards


def _card_summary_to_row(card) -> dict:
    """Convert a TCGdex card summary object to a DB row dict."""
    set_obj = getattr(card, "set", None)
    set_id = getattr(set_obj, "id", None) if set_obj else None
    set_name = getattr(set_obj, "name", None) if set_obj else None
    # serie is not available on SetResume — only on the full Set object
    series_name = None

    # Build image URL from card image field (may be None for some cards)
    image_base = getattr(card, "image", None)
    image_url = f"{image_base}/{IMAGE_QUALITY}.png" if image_base else None

    types = getattr(card, "types", None)
    types_str = json.dumps(types) if types else None

    return {
        "id": card.id,
        "name": card.name,
        "set_id": set_id,
        "set_name": set_name,
        "series": series_name,
        "number": getattr(card, "localId", None),
        "rarity": getattr(card, "rarity", None),
        "category": getattr(card, "category", None),
        "hp": str(getattr(card, "hp", None)) if getattr(card, "hp", None) is not None else None,
        "types": types_str,
        "image_url": image_url,
        "local_image_path": None,
    }


def _download_image(card_id: str, image_url: str) -> tuple[str, str | None]:
    """Download a single card image. Returns (card_id, local_path or None on failure).

    Reads config.IMAGES_DIR at call time so that runtime directory changes
    (e.g. via the GUI's Change Data Directory dialog) are respected.
    """
    dest = _config.IMAGES_DIR / f"{card_id}.png"
    if dest.exists():
        return card_id, str(dest)

    try:
        resp = requests.get(image_url, timeout=DOWNLOAD_TIMEOUT, stream=True)
        resp.raise_for_status()
        dest.write_bytes(resp.content)
        return card_id, str(dest)
    except Exception:
        return card_id, None


def _fetch_image_url_for_card(card_id: str) -> tuple[str, str | None]:
    """Fetch the full Card object from TCGdex and return its image URL.
    Returns (card_id, image_url or None).
    """
    try:
        sdk = TCGdex(TCGDEX_LANGUAGE)
        card = sdk.card.getSync(card_id)
        if card is None:
            return card_id, None
        url = card.get_image_url(IMAGE_QUALITY, "png")
        return card_id, url
    except Exception:
        return card_id, None


def _backfill_missing_image_urls(progress_callback=None) -> int:
    """For cards whose image_url is NULL, fetch the full card from the API to recover it.
    Returns the number of URLs successfully recovered.
    """
    missing = get_cards_without_image_url()
    if not missing:
        return 0

    if progress_callback:
        progress_callback(f"Recovering image URLs for {len(missing)} cards via individual API lookups...")

    recovered = 0
    workers = min(4, DOWNLOAD_WORKERS)  # be conservative for individual card fetches

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(_fetch_image_url_for_card, row["id"]): row["id"]
            for row in missing
        }

        bar = tqdm(as_completed(futures), total=len(futures), desc="Recovering URLs", unit="card")
        for future in bar:
            card_id, url = future.result()
            if url:
                update_image_url(card_id, url)
                recovered += 1
            bar.set_postfix({"recovered": recovered})

    if progress_callback:
        progress_callback(f"Recovered {recovered}/{len(missing)} missing image URLs.")

    return recovered


def _fetch_full_metadata_for_card(card_id: str) -> tuple[str, dict | None]:
    """Fetch a full Card object and extract all fields missing from CardResume.
    Returns (card_id, metadata_dict | None).
    """
    try:
        sdk = TCGdex(TCGDEX_LANGUAGE)
        card = sdk.card.getSync(card_id)
        if card is None:
            return card_id, None

        import json as _json

        set_obj = getattr(card, "set", None)
        set_name = getattr(set_obj, "name", None) if set_obj else None
        set_total = None
        if set_obj is not None:
            card_count = getattr(set_obj, "cardCount", None)
            if card_count is not None:
                total = getattr(card_count, "total", None)
                if total is not None:
                    set_total = str(total)

        variants_obj = getattr(card, "variants", None)
        if variants_obj is not None:
            variants = {
                "normal":       bool(getattr(variants_obj, "normal",       False)),
                "reverse":      bool(getattr(variants_obj, "reverse",      False)),
                "holo":         bool(getattr(variants_obj, "holo",         False)),
                "firstEdition": bool(getattr(variants_obj, "firstEdition", False)),
                "wPromo":       bool(getattr(variants_obj, "wPromo",       False)),
            }
            variants_json = _json.dumps(variants)
        else:
            variants_json = None

        hp = str(getattr(card, "hp", None)) if getattr(card, "hp", None) is not None else None

        types = getattr(card, "types", None)
        types_json = _json.dumps(types) if types else None

        return card_id, {
            "set_name":      set_name,
            "rarity":        getattr(card, "rarity", None),
            "category":      getattr(card, "category", None),
            "hp":            hp,
            "variants_json": variants_json,
            "set_total":     set_total,
            "types_json":    types_json,
        }
    except Exception:
        return card_id, None


def backfill_metadata(progress_callback=None) -> int:
    """Fetch full metadata (set name, rarity, variants, etc.) for all cards that are
    missing it, using parallel getSync calls. Returns number of cards updated.

    This is needed because the list endpoint only returns CardResume objects with
    just id/name/localId/image — no set name, rarity, or variants.
    """
    missing = get_cards_without_set_name()
    if not missing:
        if progress_callback:
            progress_callback("All cards already have full metadata.")
        return 0

    total = len(missing)
    if progress_callback:
        progress_callback(f"Fetching full metadata for {total:,} cards (20 workers)...")

    updated = 0
    workers = min(20, DOWNLOAD_WORKERS * 2)

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(_fetch_full_metadata_for_card, row["id"]): row["id"]
            for row in missing
        }

        bar = tqdm(as_completed(futures), total=len(futures),
                   desc="Backfilling metadata", unit="card")
        for future in bar:
            card_id, meta = future.result()
            if meta:
                update_card_full_metadata(
                    card_id,
                    meta["set_name"],
                    meta["rarity"],
                    meta["category"],
                    meta["hp"],
                    meta["variants_json"],
                    meta["set_total"],
                    meta.get("types_json"),
                )
                updated += 1
            bar.set_postfix({"updated": updated})
            if progress_callback and updated % 200 == 0:
                progress_callback(
                    f"Fetching metadata… {updated}/{total} done"
                )

    if progress_callback:
        progress_callback(f"Metadata backfill complete: {updated:,}/{total:,} cards updated.")

    return updated


def download_metadata(progress_callback=None) -> int:
    """Fetch all card metadata from TCGdex and store in the DB. Returns number of cards stored."""
    init_db()

    if progress_callback:
        progress_callback("Fetching card list from TCGdex...")

    summaries = _fetch_all_card_summaries(progress_callback)

    if progress_callback:
        progress_callback(f"Saving {len(summaries)} card records to database...")

    rows = [_card_summary_to_row(c) for c in summaries]

    # Batch insert in chunks of 500
    chunk_size = 500
    for i in range(0, len(rows), chunk_size):
        upsert_cards_batch(rows[i : i + chunk_size])

    # The list endpoint only returns CardResume (id/name/localId/image).
    # Backfill set_name, rarity, variants, etc. via individual getSync calls
    # for any cards still missing that data.
    backfill_metadata(progress_callback)

    return len(rows)


def download_images(progress_callback=None) -> tuple[int, int]:
    """Download images for all cards that are missing one. Returns (success_count, fail_count).

    Pass 1: recover image_url for any cards that had None from the list endpoint.
    Pass 2: download all cards that still have no local image file.
    """
    # Pass 1: backfill missing image URLs via individual API lookups
    _backfill_missing_image_urls(progress_callback)

    # Pass 2: download all cards still missing a local image
    pending = get_cards_without_images()

    if not pending:
        if progress_callback:
            progress_callback("All card images already downloaded.")
        return 0, 0

    # Filter to only rows that now have an image_url (some may still be None if API had nothing)
    downloadable = [row for row in pending if row["image_url"]]
    skipped = len(pending) - len(downloadable)

    if progress_callback:
        msg = f"Downloading {len(downloadable)} card images with {DOWNLOAD_WORKERS} workers..."
        if skipped:
            msg += f" ({skipped} skipped — no image available on TCGdex)"
        progress_callback(msg)

    success = 0
    fail = 0

    with ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS) as executor:
        futures = {
            executor.submit(_download_image, row["id"], row["image_url"]): row["id"]
            for row in downloadable
        }

        bar = tqdm(as_completed(futures), total=len(futures), desc="Downloading images", unit="img")
        for future in bar:
            card_id, local_path = future.result()
            if local_path:
                update_local_image_path(card_id, local_path)
                success += 1
            else:
                fail += 1
                bar.set_postfix({"failed": fail})

    if progress_callback:
        progress_callback(f"Images downloaded: {success} ok, {fail} failed.")

    return success, fail


def download_all(progress_callback=None):
    """Full setup: fetch metadata then download all images."""
    count = download_metadata(progress_callback)
    if progress_callback:
        progress_callback(f"Metadata complete: {count} cards in database.")
    download_images(progress_callback)
    if progress_callback:
        progress_callback("Setup complete.")
