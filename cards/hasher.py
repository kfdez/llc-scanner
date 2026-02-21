"""
Compute perceptual hashes for all downloaded card images and store them in the DB.

Run via:  python main.py --setup   (called automatically after download)
or call:  compute_all_hashes(progress_callback=...)
"""

import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from PIL import Image
import imagehash

from config import HASH_TYPES, HASH_IMAGE_SIZE, PHASH_SIZE
from db.database import get_cards_without_hashes, upsert_hashes_batch

# Number of parallel worker threads for hashing.
# Benchmarks on a 12-core machine show 4 workers give ~4x speedup over sequential;
# beyond 8 the gain is marginal (disk I/O becomes the bottleneck).
_HASH_WORKERS = min(8, max(1, (os.cpu_count() or 4)))

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


def _hash_row(row) -> tuple[str, dict[str, str] | None]:
    """Worker function: hash one card image. Returns (card_id, hashes | None)."""
    try:
        hashes = compute_hashes_for_image(row["local_image_path"])
        return row["id"], hashes
    except Exception:
        return row["id"], None


def compute_all_hashes(progress_callback=None) -> int:
    """Compute hashes for every card that has a local image but no hash yet.

    Uses a thread pool for parallel I/O + hashing — typically 4x faster than
    sequential on a multi-core machine.
    """
    pending = get_cards_without_hashes()

    if not pending:
        if progress_callback:
            progress_callback("All card hashes already computed.")
        return 0

    total = len(pending)
    if progress_callback:
        progress_callback(
            f"Computing hashes for {total:,} cards "
            f"({_HASH_WORKERS} parallel workers)..."
        )

    batch: list[dict] = []
    done = 0
    errors = 0

    with ThreadPoolExecutor(max_workers=_HASH_WORKERS) as executor:
        futures = {executor.submit(_hash_row, row): row for row in pending}

        for future in as_completed(futures):
            card_id, hashes = future.result()

            if hashes is None:
                errors += 1
            else:
                for ht, hv in hashes.items():
                    batch.append({"card_id": card_id, "hash_type": ht, "hash_value": hv})
                done += 1

            # Flush to DB in batches of 1000 records
            if len(batch) >= 1000:
                upsert_hashes_batch(batch)
                batch.clear()

            if progress_callback and (done + errors) % 500 == 0:
                progress_callback(
                    f"Hashed {done:,}/{total:,} cards "
                    f"({errors} errors)..."
                )

    if batch:
        upsert_hashes_batch(batch)

    if progress_callback:
        progress_callback(f"Hashing complete: {done:,} ok, {errors} errors.")

    return done
