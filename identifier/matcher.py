"""
Hash-based card identification pipeline.

Given a path to a scanned card image, preprocess it and find the closest
matching card(s) in the local hash database.

Performance design
------------------
The index stores hashes as packed uint8 arrays (one matrix per hash type):
    arrays[ht] : np.ndarray, shape (N, bytes_per_hash), dtype=uint8

Scoring is fully vectorised:
    xored  = arrays[ht] ^ scan_packed[ht]          # broadcast XOR
    dists  = np.unpackbits(xored, axis=1).sum(1)   # popcount per row

Compared with the original imagehash-based loop:
    index load  : ~96 ms  (was ~3 200 ms — hex_to_hash bottleneck)
    score/scan  : ~15 ms  (was ~290 ms  — pure-Python loop)
"""

from __future__ import annotations

import numpy as np
import imagehash             # still used only to hash the *scan* image
from PIL import Image

from config import (
    HASH_TYPES, HASH_IMAGE_SIZE, TOP_K_MATCHES,
    CONFIDENCE_HIGH, CONFIDENCE_MED, PHASH_SIZE,
)
from db.database import get_all_hashes, get_card_by_id
from cards.hasher import _HASH_FN
from identifier.preprocess import preprocess_for_hashing as _preprocess_image
from identifier.enricher import enrich_result


# Hash type weights: phash double-weighted (most robust to lighting / compression)
_WEIGHTS: dict[str, float] = {"phash": 2.0, "ahash": 1.0, "dhash": 1.0, "whash": 1.0}
_WEIGHT_SUM = sum(_WEIGHTS.values())   # 5.0

# ---------------------------------------------------------------------------
# Vectorised index
# ---------------------------------------------------------------------------

class _HashIndex:
    """
    Compact, numpy-backed hash index.

    Attributes
    ----------
    card_ids : list[str]
        Card IDs in row order — same for every hash type.
    arrays : dict[str, np.ndarray]
        ``arrays[ht]`` has shape ``(N, bytes_per_hash)``, dtype uint8.
        Each row is the packed bit representation of one card's hash.
    """

    def __init__(
        self,
        card_ids: list[str],
        arrays: dict[str, np.ndarray],
    ):
        self.card_ids = card_ids
        self.arrays   = arrays

    @classmethod
    def build(cls) -> "_HashIndex":
        """Load all hashes from DB and assemble the index."""
        # Collect rows per hash type
        rows_by_ht: dict[str, list] = {ht: get_all_hashes(ht) for ht in HASH_TYPES}

        # Determine the canonical ordering of card IDs from phash rows
        # (all hash types should have the same set of cards, but phash is baseline)
        primary_ht = HASH_TYPES[0]   # "phash"
        card_ids = [r["card_id"] for r in rows_by_ht[primary_ht]]
        id_to_idx = {cid: i for i, cid in enumerate(card_ids)}
        n = len(card_ids)

        if n == 0:
            return cls([], {})

        # bytes_per_hash: for PHASH_SIZE=16 (256 bits) → 32 bytes
        bytes_per_hash = (PHASH_SIZE * PHASH_SIZE) // 8

        arrays: dict[str, np.ndarray] = {}
        for ht in HASH_TYPES:
            mat = np.zeros((n, bytes_per_hash), dtype=np.uint8)
            for row in rows_by_ht[ht]:
                idx = id_to_idx.get(row["card_id"])
                if idx is None:
                    continue
                try:
                    packed = np.frombuffer(
                        bytes.fromhex(row["hash_value"]), dtype=np.uint8
                    )
                    # Guard: truncate / pad to expected length
                    if len(packed) >= bytes_per_hash:
                        mat[idx] = packed[:bytes_per_hash]
                    else:
                        mat[idx, :len(packed)] = packed
                except (ValueError, TypeError):
                    pass   # leave as zeros — will score as max distance
            arrays[ht] = mat

        return cls(card_ids, arrays)

    def is_empty(self) -> bool:
        return len(self.card_ids) == 0

    def score(self, scan_hashes: dict[str, imagehash.ImageHash]) -> np.ndarray:
        """
        Return a float32 array of weighted average Hamming distances, one per card.

        Parameters
        ----------
        scan_hashes : dict[str, ImageHash]
            Output of ``_HASH_FN[ht](img, hash_size=PHASH_SIZE)`` for each ht.
        """
        n = len(self.card_ids)
        accumulated = np.zeros(n, dtype=np.float32)

        for ht, weight in _WEIGHTS.items():
            if ht not in self.arrays or ht not in scan_hashes:
                continue
            # Pack the scan hash to bytes the same way the DB stores them
            scan_packed = np.packbits(scan_hashes[ht].hash.flatten())

            xored = np.bitwise_xor(self.arrays[ht], scan_packed)   # (N, B)
            dists = np.unpackbits(xored, axis=1).sum(axis=1)        # (N,) popcount
            accumulated += dists.astype(np.float32) * weight

        return accumulated / _WEIGHT_SUM


# Module-level cache — loaded lazily on first identify call, refreshed after setup
_index: _HashIndex | None = None


def reload_index() -> None:
    """Force reload of the hash index from DB (call after setup completes)."""
    global _index
    _index = _HashIndex.build()


def _get_index() -> _HashIndex:
    global _index
    if _index is None:
        _index = _HashIndex.build()
    return _index


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _confidence_label(distance: float) -> str:
    if distance <= CONFIDENCE_HIGH:
        return "high"
    if distance <= CONFIDENCE_MED:
        return "medium"
    return "low"


def identify_card(image_path: str) -> list[dict]:
    """
    Identify a card from an image file using perceptual hashing.

    Scoring: phash is weighted 2x (most robust to compression/lighting),
    ahash/dhash/whash weighted 1x each.  Final score is the weighted average
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
    if index.is_empty():
        return []

    # Preprocess scan using the full card-detection pipeline
    pil_img = _preprocess_image(image_path)

    # Compute hashes for the scanned card (imagehash objects — only N=1 image)
    scan_hashes: dict[str, imagehash.ImageHash] = {
        ht: _HASH_FN[ht](pil_img, hash_size=PHASH_SIZE) for ht in HASH_TYPES
    }

    # Vectorised scoring — returns float32 array of length N
    distances = index.score(scan_hashes)

    # Partial sort: find the TOP_K_MATCHES smallest distances
    k = min(TOP_K_MATCHES, len(distances))
    top_indices = np.argpartition(distances, k - 1)[:k]
    top_indices = top_indices[np.argsort(distances[top_indices])]  # sort ascending

    results = []
    for idx in top_indices:
        card_id  = index.card_ids[idx]
        distance = float(distances[idx])
        row = get_card_by_id(card_id)
        if row is None:
            continue
        result = {
            "card_id":          card_id,
            "name":             row["name"],
            "set_id":           row["set_id"]   or "",
            "set_name":         row["set_name"] or "",
            "number":           row["number"]   or "",
            "rarity":           row["rarity"]   or "",
            "category":         row["category"] or "",
            "hp":               row["hp"]       or "",
            "types":            row["types"]    or "",
            "image_url":        row["image_url"]         or "",
            "local_image_path": row["local_image_path"]  or "",
            "distance":         round(distance, 2),
            "confidence":       _confidence_label(distance),
        }
        enrich_result(result)
        results.append(result)

    return results
