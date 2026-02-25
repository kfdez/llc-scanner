"""
ML embedding-based card identification.

Uses a FAISS IndexFlatIP (inner product search) over L2-normalised
DINOv2 (vit_base_patch14) embeddings. Because the vectors are L2-normalised,
inner product == cosine similarity — higher is more similar.

Index is loaded lazily from the DB on first identify call and cached
in module-level state. Call reload_embedding_index() after computing
new embeddings to refresh the in-process cache.

Test-Time Augmentation (TTA):
  At identify time, 4 slightly jittered crops of the query image are
  embedded in a single GPU batch, then averaged and re-normalised before
  the FAISS search.  This reduces sensitivity to exact crop alignment,
  compression artefacts, and lighting variation.
"""

import random

import numpy as np

from config import TOP_K_MATCHES, EMBEDDING_CONFIDENCE_HIGH, EMBEDDING_CONFIDENCE_MED
from db.database import get_all_embeddings, get_card_by_id
from identifier.enricher import enrich_result

# Embedding dimension produced by DINOv2 vit_base_patch14 global-average-pool head
_EMBEDDING_DIM = 768

# ── Module-level FAISS index cache ───────────────────────────────────────────
_faiss_index = None           # faiss.IndexFlatIP, built at load time
_index_card_ids: list[str] = []  # parallel list: FAISS row i → card_id


def _load_embedding_index():
    """
    Read all embeddings from the DB and build a FAISS IndexFlatIP.
    Returns (index, card_ids) — or (None, []) if no embeddings are stored.
    """
    import faiss

    rows = get_all_embeddings()
    if not rows:
        return None, []

    card_ids: list[str] = []
    vectors: list[np.ndarray] = []

    for row in rows:
        card_id = row["card_id"]
        # np.frombuffer returns a read-only array backed by the SQLite bytes
        # object.  .copy() makes it writable and C-contiguous — both required
        # by faiss.IndexFlatIP.add().
        vec = np.frombuffer(row["embedding"], dtype=np.float32).copy()
        if vec.shape[0] != _EMBEDDING_DIM:
            continue  # skip corrupted / old-format rows
        card_ids.append(card_id)
        vectors.append(vec)

    if not vectors:
        return None, []

    matrix = np.stack(vectors, axis=0)   # (N, 768) float32, C-contiguous

    # IndexFlatIP: exact inner-product search (= cosine similarity for L2-normed vecs)
    # No training step needed. At 22k cards this takes < 5ms on CPU per query.
    index = faiss.IndexFlatIP(_EMBEDDING_DIM)
    index.add(matrix)

    return index, card_ids


def reload_embedding_index() -> None:
    """Force reload of the FAISS index from DB (call after embedding completes)."""
    global _faiss_index, _index_card_ids
    _faiss_index, _index_card_ids = _load_embedding_index()


def _get_index():
    global _faiss_index, _index_card_ids
    if _faiss_index is None:
        _faiss_index, _index_card_ids = _load_embedding_index()
    return _faiss_index, _index_card_ids


def _confidence_label(similarity: float) -> str:
    if similarity >= EMBEDDING_CONFIDENCE_HIGH:
        return "high"
    if similarity >= EMBEDDING_CONFIDENCE_MED:
        return "medium"
    return "low"


# ── Test-Time Augmentation ────────────────────────────────────────────────────

# ImageNet normalisation constants — same for DINOv2 and EfficientNet-B0.
# Imported from preprocess.py to avoid duplication.
from identifier.preprocess import _IMAGENET_MEAN, _IMAGENET_STD  # noqa: E402

# Lazy import of the model singleton from embedding_computer.
from cards.embedding_computer import _get_model as _get_model_for_tta  # noqa: E402


def _embed_with_tta(image_path: str, num_crops: int = 4,
                    sticker_mask_px: "tuple | None" = None,
                    auto_detect: "bool | None" = None) -> np.ndarray:
    """
    Compute a TTA-averaged embedding for a single query image.

    Embeds `num_crops` slightly jittered centre-crops of the card in a
    single GPU batch, then returns the L2-normalised average vector.

    The first crop (i == 0) is always an exact centre-crop (jitter = 0.0)
    to ensure a reproducible baseline; subsequent crops apply ±8% random
    scale/position jitter to reduce sensitivity to exact alignment.

    Sticker compensation is applied to all crops: if sticker_mask_px is
    given it is scaled to each crop's size; otherwise auto-detection runs
    on the first crop and the same mask is reused for subsequent crops.

    Returns shape (768,) float32, L2-normalised.
    """
    import cv2
    import torch
    from config import EMBEDDING_INPUT_SIZE, STICKER_AUTO_DETECT, STICKER_INPAINT_RADIUS
    from identifier.sticker import (
        detect_sticker, inpaint_sticker, mask_from_rect, scale_rect,
    )

    _auto = auto_detect if auto_detect is not None else STICKER_AUTO_DETECT

    # Standard Pokemon card aspect ratio: height / width  (88 mm / 63 mm)
    _CARD_ASPECT = 88 / 63

    img_bgr = cv2.imread(str(image_path))
    if img_bgr is None:
        raise ValueError(f"Cannot read image: {image_path}")

    # ── Manual mask: inpaint on full scan BEFORE any cropping ────────────────
    # sticker_mask_px is in original scan pixel coordinates.
    if sticker_mask_px is not None:
        mh, mw = img_bgr.shape[:2]
        manual_m = mask_from_rect(mh, mw, sticker_mask_px)
        img_bgr = inpaint_sticker(img_bgr, manual_m, STICKER_INPAINT_RADIUS)

    h, w = img_bgr.shape[:2]
    model, device = _get_model_for_tta()

    # Auto-detect mask in 518×518 space — detected on first crop, reused for rest
    _sticker_mask_518: "np.ndarray | None" = None

    arrays = []
    for i in range(num_crops):
        # First crop: clean centre-crop with no jitter (deterministic baseline).
        # Subsequent crops: random ±8% scale/position jitter.
        jitter = 0.0 if i == 0 else random.uniform(-0.08, 0.08)

        if h / w > _CARD_ASPECT:
            # Image taller than card — crop height
            crop_h = int(w * _CARD_ASPECT * (1.0 + jitter))
            crop_h = max(10, min(crop_h, h))
            y_offset = int(jitter * h * 0.1) if i > 0 else 0
            y0 = max(0, min((h - crop_h) // 2 + y_offset, h - crop_h))
            cropped = img_bgr[y0: y0 + crop_h, :]
        else:
            # Image wider than card — crop width
            crop_w = int(h / _CARD_ASPECT * (1.0 + jitter))
            crop_w = max(10, min(crop_w, w))
            x_offset = int(jitter * w * 0.1) if i > 0 else 0
            x0 = max(0, min((w - crop_w) // 2 + x_offset, w - crop_w))
            cropped = img_bgr[:, x0: x0 + crop_w]

        resized = cv2.resize(cropped, (EMBEDDING_INPUT_SIZE, EMBEDDING_INPUT_SIZE))

        # Auto-detect on first crop (only when no manual mask was set); reuse for rest
        if i == 0 and _auto and sticker_mask_px is None:
            _sticker_mask_518 = detect_sticker(resized)

        if _sticker_mask_518 is not None:
            resized = inpaint_sticker(resized, _sticker_mask_518, STICKER_INPAINT_RADIUS)

        rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)

        arr = rgb.astype(np.float32) / 255.0
        arr = (arr - _IMAGENET_MEAN) / _IMAGENET_STD
        arr = arr.transpose(2, 0, 1)   # CHW
        arrays.append(arr)

    # Single GPU forward pass for all crops — no extra latency vs single crop
    batch = torch.from_numpy(np.stack(arrays)).to(device)   # (num_crops, 3, S, S)
    with torch.no_grad():
        embs = model(batch)   # (num_crops, 768)

    vecs = embs.cpu().numpy()   # (num_crops, 768)

    # Average then re-normalise to get the TTA embedding
    avg = vecs.mean(axis=0)     # (768,)
    norm = np.linalg.norm(avg)
    return (avg / norm if norm > 0 else avg).astype(np.float32)


# ── Public API ────────────────────────────────────────────────────────────────

def identify_card_embedding(image_path: str,
                            sticker_mask_px: "tuple | None" = None,
                            auto_detect: "bool | None" = None) -> list[dict]:
    """
    Identify a card from an image file using the ML embedding matcher.

    Uses Test-Time Augmentation: 4 jittered crops are embedded in one GPU
    batch, averaged, and re-normalised before the FAISS search.  This makes
    identification more robust to exact crop position, scan artefacts, and
    mild lighting variation.

    Returns a list of up to TOP_K_MATCHES dicts sorted by descending cosine
    similarity (best match first). Uses the same key schema as identify_card()
    in matcher.py so the GUI can consume either result list unchanged.

    The 'distance' field holds the cosine similarity (0.0–1.0, higher = better).
    Confidence thresholds: ≥ 0.90 = high, ≥ 0.75 = medium, < 0.75 = low.
    """
    index, card_ids = _get_index()
    if index is None or index.ntotal == 0:
        return []

    # Compute TTA query embedding (768-dim, L2-normalised)
    query_vec = _embed_with_tta(image_path,
                                sticker_mask_px=sticker_mask_px,
                                auto_detect=auto_detect)  # (768,) float32
    query_matrix = query_vec.reshape(1, -1)        # (1, 768) for FAISS

    # index.search returns (similarities, indices) each of shape (1, k)
    k = min(TOP_K_MATCHES, index.ntotal)
    similarities, indices = index.search(query_matrix, k)

    sims = similarities[0]    # (k,) — cosine similarities, descending
    idxs = indices[0]         # (k,) — row indices into _index_card_ids

    results = []
    for sim, idx in zip(sims, idxs):
        # FAISS pads with -1 when ntotal < k
        if idx < 0 or idx >= len(card_ids):
            continue

        card_id = card_ids[int(idx)]
        row = get_card_by_id(card_id)
        if row is None:
            continue

        similarity = float(sim)
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
            "distance": round(similarity, 4),   # cosine similarity; higher = better
            "confidence": _confidence_label(similarity),
        }
        enrich_result(result)
        results.append(result)

    return results
