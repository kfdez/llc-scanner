"""
Compute perceptual hashes for all downloaded card images and store them in the DB.

Run via:  python main.py --setup   (called automatically after download)
or call:  compute_all_hashes(progress_callback=...)
"""

from PIL import Image
import imagehash

from config import HASH_TYPES, HASH_IMAGE_SIZE, PHASH_SIZE
from db.database import get_cards_without_hashes, upsert_hashes_batch


_HASH_FN = {
    "phash": imagehash.phash,
    "ahash": imagehash.average_hash,
    "dhash": imagehash.dhash,
    "whash": imagehash.whash,
}


def compute_hashes_for_image(image_path: str) -> dict[str, str]:
    """Load an image and return a dict of {hash_type: hash_string}."""
    img = Image.open(image_path).convert("RGB").resize(HASH_IMAGE_SIZE)
    return {ht: str(_HASH_FN[ht](img, hash_size=PHASH_SIZE)) for ht in HASH_TYPES}


def compute_all_hashes(progress_callback=None) -> int:
    """Compute hashes for every card that has a local image but no hash yet."""
    pending = get_cards_without_hashes()

    if not pending:
        if progress_callback:
            progress_callback("All card hashes already computed.")
        return 0

    if progress_callback:
        progress_callback(f"Computing hashes for {len(pending)} cards...")

    batch: list[dict] = []
    done = 0
    errors = 0

    for row in pending:
        card_id = row["id"]
        image_path = row["local_image_path"]

        try:
            hashes = compute_hashes_for_image(image_path)
            for ht, hv in hashes.items():
                batch.append({"card_id": card_id, "hash_type": ht, "hash_value": hv})
        except Exception:
            errors += 1
            continue

        done += 1

        # Flush to DB in batches of 1000 records
        if len(batch) >= 1000:
            upsert_hashes_batch(batch)
            batch.clear()

        if progress_callback and done % 500 == 0:
            progress_callback(f"Hashed {done}/{len(pending)} cards...")

    if batch:
        upsert_hashes_batch(batch)

    if progress_callback:
        progress_callback(f"Hashing complete: {done} ok, {errors} errors.")

    return done
