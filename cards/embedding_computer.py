"""
Compute DINOv2 (vit_base_patch14) embeddings for all downloaded card images
and store them as BLOBs in the card_embeddings table.

Run via:  python main.py --embed
or call:  compute_all_embeddings(progress_callback=...)
"""

import numpy as np

from config import EMBEDDING_MODEL, EMBEDDING_BATCH_SIZE
from db.database import get_cards_without_embeddings, upsert_embeddings_batch


# ── Lazy model singleton ──────────────────────────────────────────────────────
# torch and timm are only imported inside _get_model() so that:
#   - GUI startup is not delayed by ~1-2s of torch import time
#   - Hash-only sessions never pay the torch memory overhead (~300 MB)
_model = None
_device = None


def _get_model():
    """
    Load the DINOv2 model on first call (lazy singleton).
    Returns (model, device).
    """
    global _model, _device
    if _model is not None:
        return _model, _device

    import torch
    import timm

    _device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    # num_classes=0 removes the classifier head.
    # global_pool="token" uses the CLS token output — the canonical DINOv2
    # pooling strategy.  "avg" triggers an fc_norm layer that is absent from
    # the pretrained DINOv2 weights and causes a RuntimeError at load time.
    # Output shape: (batch, 768) for vit_base_patch14.
    _model = timm.create_model(
        EMBEDDING_MODEL,
        pretrained=True,
        num_classes=0,
        global_pool="token",
    )
    _model.eval()
    _model.to(_device)

    return _model, _device


# ── Public API ────────────────────────────────────────────────────────────────

def compute_embedding_for_image(image_path: str) -> np.ndarray:
    """
    Compute a single L2-normalised 768-dim float32 embedding for one image.
    Used at identify time (not batched).
    Returns shape (768,).
    """
    import torch
    from identifier.preprocess import preprocess_for_embedding

    model, device = _get_model()

    arr = preprocess_for_embedding(image_path)               # (3, 518, 518) float32
    tensor = torch.from_numpy(arr).unsqueeze(0).to(device)  # (1, 3, 518, 518)

    with torch.no_grad():
        embedding = model(tensor)                            # (1, 768)

    vec = embedding.squeeze(0).cpu().numpy()                 # (768,) float32

    # L2-normalise so IndexFlatIP == cosine similarity
    norm = np.linalg.norm(vec)
    if norm > 0:
        vec = vec / norm
    return vec.astype(np.float32)


def _load_image_batch(rows: list) -> tuple[list, list]:
    """
    Preprocess a batch of card images into numpy arrays.
    Skips unreadable images silently.
    Returns (valid_rows, arrays) — parallel lists of rows and (3,518,518) arrays.
    """
    from identifier.preprocess import preprocess_for_embedding

    valid_rows = []
    arrays = []
    for row in rows:
        try:
            arr = preprocess_for_embedding(row["local_image_path"])
            valid_rows.append(row)
            arrays.append(arr)
        except Exception:
            pass  # error counted in caller via len(chunk) - len(valid_rows)

    return valid_rows, arrays


def compute_all_embeddings(progress_callback=None) -> int:
    """
    Compute embeddings for every card that has a local image but no embedding.
    Processes images in GPU batches of EMBEDDING_BATCH_SIZE for efficiency.
    Returns the number of cards successfully embedded.
    """
    import torch

    pending = get_cards_without_embeddings()

    if not pending:
        if progress_callback:
            progress_callback("All card embeddings already computed.")
        return 0

    device_label = "GPU" if torch.cuda.is_available() else "CPU"
    if progress_callback:
        progress_callback(
            f"Computing embeddings for {len(pending)} cards ({device_label})..."
        )

    model, device = _get_model()

    done = 0
    errors = 0
    db_batch: list[dict] = []

    for batch_start in range(0, len(pending), EMBEDDING_BATCH_SIZE):
        chunk = pending[batch_start: batch_start + EMBEDDING_BATCH_SIZE]

        valid_rows, arrays = _load_image_batch(chunk)
        errors += len(chunk) - len(valid_rows)

        if not arrays:
            continue

        # Stack into (N, 3, 518, 518) and run a single GPU forward pass
        batch_tensor = torch.from_numpy(
            np.stack(arrays, axis=0)          # already float32 from preprocess
        ).to(device)

        with torch.no_grad():
            embeddings = model(batch_tensor)  # (N, 768)

        # L2-normalise on CPU (avoids GPU-tensor → numpy conversion issues)
        vecs = embeddings.cpu().numpy()        # (N, 768)
        norms = np.linalg.norm(vecs, axis=1, keepdims=True)
        norms = np.where(norms == 0, 1.0, norms)
        vecs = (vecs / norms).astype(np.float32)

        for row, vec in zip(valid_rows, vecs):
            db_batch.append({
                "card_id": row["id"],
                "embedding_bytes": vec.tobytes(),
            })
            done += 1

        # Flush to DB every 500 cards to reduce transaction overhead
        if len(db_batch) >= 500:
            upsert_embeddings_batch(db_batch)
            db_batch.clear()

        if progress_callback and done % 500 == 0:
            progress_callback(f"Embedded {done}/{len(pending)} cards...")

    if db_batch:
        upsert_embeddings_batch(db_batch)

    if progress_callback:
        progress_callback(f"Embedding complete: {done} ok, {errors} errors.")

    return done
