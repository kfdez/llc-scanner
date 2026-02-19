"""
Hash-based card identification pipeline.

Given a path to a scanned card image, preprocess it and find the closest
matching card(s) in the local hash database.
"""

import numpy as np
from PIL import Image
import imagehash

from config import HASH_TYPES, HASH_IMAGE_SIZE, TOP_K_MATCHES, CONFIDENCE_HIGH, CONFIDENCE_MED, PHASH_SIZE
from db.database import get_all_hashes, get_card_by_id
from cards.hasher import _HASH_FN
from identifier.preprocess import preprocess_for_hashing as _preprocess_image
from identifier.enricher import enrich_result


def _load_hash_index() -> dict[str, dict[str, imagehash.ImageHash]]:
    """
    Load all stored hashes from the DB into memory.
    Returns: {card_id: {hash_type: ImageHash}}
    """
    index: dict[str, dict[str, imagehash.ImageHash]] = {}
    for ht in HASH_TYPES:
        rows = get_all_hashes(ht)
        for row in rows:
            cid = row["card_id"]
            if cid not in index:
                index[cid] = {}
            index[cid][ht] = imagehash.hex_to_hash(row["hash_value"])
    return index


# Module-level cache so we don't reload from DB on every scan
_hash_index: dict | None = None


def reload_index():
    """Force reload of the hash index from DB (call after setup completes)."""
    global _hash_index
    _hash_index = _load_hash_index()


def _get_index() -> dict:
    global _hash_index
    if _hash_index is None:
        _hash_index = _load_hash_index()
    return _hash_index


def _confidence_label(distance: float) -> str:
    if distance <= CONFIDENCE_HIGH:
        return "high"
    if distance <= CONFIDENCE_MED:
        return "medium"
    return "low"


def identify_card(image_path: str) -> list[dict]:
    """
    Identify a card from an image file using perceptual hashing.

    Scoring: phash is weighted 2× (most robust to compression/lighting),
    ahash/dhash/whash weighted 1× each. Final score is the weighted average
    Hamming distance — lower is better.

    Returns a list of up to TOP_K_MATCHES dicts, sorted by ascending distance:
    [
        {
            "card_id": str,
            "name": str,
            "set_name": str,
            "number": str,
            "rarity": str,
            "category": str,
            "hp": str,
            "image_url": str,
            "local_image_path": str,
            "distance": float,
            "confidence": "high" | "medium" | "low",
        },
        ...
    ]
    """
    index = _get_index()
    if not index:
        return []

    # Preprocess scan using the full card-detection pipeline
    pil_img = _preprocess_image(image_path)

    # Compute hashes for the scanned card
    scan_hashes: dict[str, imagehash.ImageHash] = {
        ht: _HASH_FN[ht](pil_img, hash_size=PHASH_SIZE) for ht in HASH_TYPES
    }

    # Hash type weights: phash is most robust, double-weighted
    _WEIGHTS = {"phash": 2, "ahash": 1, "dhash": 1, "whash": 1}

    # Score every card in the index
    scores: list[tuple[float, str]] = []
    for card_id, stored in index.items():
        total = 0.0
        weight_sum = 0
        for ht in HASH_TYPES:
            if ht in stored:
                w = _WEIGHTS.get(ht, 1)
                total += (scan_hashes[ht] - stored[ht]) * w
                weight_sum += w
        if weight_sum:
            avg_distance = total / weight_sum
            scores.append((avg_distance, card_id))

    scores.sort(key=lambda x: x[0])
    top = scores[:TOP_K_MATCHES]

    results = []
    for distance, card_id in top:
        row = get_card_by_id(card_id)
        if row is None:
            continue
        result = {
            "card_id": card_id,
            "name": row["name"],
            "set_id": row["set_id"] or "",
            "set_name": row["set_name"] or "",
            "number": row["number"] or "",
            "rarity": row["rarity"] or "",
            "category": row["category"] or "",
            "hp": row["hp"] or "",
            "types": row["types"] or "",
            "image_url": row["image_url"] or "",
            "local_image_path": row["local_image_path"] or "",
            "distance": round(distance, 2),
            "confidence": _confidence_label(distance),
        }
        enrich_result(result)
        results.append(result)

    return results
