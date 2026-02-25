import json
import os
from pathlib import Path

BASE_DIR = Path(__file__).parent

# Settings file lives next to this script and persists the user's chosen data directory.
_SETTINGS_FILE = BASE_DIR / "settings.json"

_DEFAULT_DATA_DIR = BASE_DIR / "data"


def _load_settings() -> dict:
    if _SETTINGS_FILE.exists():
        try:
            return json.loads(_SETTINGS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_settings(data_dir: Path = None, extra: dict = None):
    """Persist settings to settings.json.

    data_dir  — if provided, updates the data_dir key.
    extra     — dict of additional keys to merge in (e.g. column widths).
    Both are optional; omitting data_dir leaves the existing value intact.
    """
    current = _load_settings()
    if data_dir is not None:
        current["data_dir"] = str(data_dir)
    if extra:
        current.update(extra)
    _SETTINGS_FILE.write_text(
        json.dumps(current, indent=2),
        encoding="utf-8",
    )


_settings = _load_settings()

DATA_DIR = Path(_settings.get("data_dir", _DEFAULT_DATA_DIR))
IMAGES_DIR = DATA_DIR / "images"
DB_PATH = DATA_DIR / "cards.db"

# TCGdex language — "en" for English
TCGDEX_LANGUAGE = "en"

# Set ID prefixes to exclude from matching/indexing.
# Pokemon TCG Pocket sets all use A*/B*/P-A* prefixes.
# Cards whose set_id starts with any of these are skipped when building
# the hash and embedding indexes, so they will never appear as match results.
# The raw data stays in the DB — removing a prefix here re-enables those sets
# on next startup without re-downloading anything.
EXCLUDED_SET_ID_PREFIXES: list[str] = ["A", "B", "P-A"]

# Perceptual hash types to compute/store per card
HASH_TYPES = ["phash", "ahash", "dhash", "whash"]

# Hash size (bits per side) — each hash stores hash_size² bits total.
# 8  → 64-bit hashes  (original, fast, less discriminative across 22k cards)
# 16 → 256-bit hashes (4× more data, much better for large databases)
# Changing this requires clearing card_hashes and re-running hashing.
PHASH_SIZE = 16

# Hash distance thresholds for confidence scoring.
# Scaled for PHASH_SIZE=16 (256-bit hashes, max distance = 256).
# For PHASH_SIZE=8 (64-bit) divide these by 4.
CONFIDENCE_HIGH = 15    # Hamming distance ≤ 15  → high confidence
CONFIDENCE_MED = 40     # Hamming distance ≤ 40  → medium confidence
# > 40 = low confidence

# Number of top matches to return
TOP_K_MATCHES = 5

# Image download settings
DOWNLOAD_WORKERS = 8        # concurrent image download threads
DOWNLOAD_TIMEOUT = 15       # seconds per image request
IMAGE_QUALITY = "high"      # "high" (600x825) or "low" (245x337)

# Normalised card size for hashing (width x height in px)
HASH_IMAGE_SIZE = (300, 420)

# ── ML Embedding Matcher ──────────────────────────────────────────────────────
# timm model name.
# DINOv2 (Meta, self-supervised ViT) is trained for fine-grained visual
# similarity and discriminates similar Pokemon cards far better than
# EfficientNet-B0 (general ImageNet classifier).
# Output: 768-dim L2-normalised vector.  Input: 518×518 (patch14 × 37 patches).
EMBEDDING_MODEL = "vit_base_patch14_dinov2.lvd142m"

# Native input resolution for the embedding model (pixels per side).
# EfficientNet-B0: 224.  DINOv2 vit_base_patch14: 518.
EMBEDDING_INPUT_SIZE = 518

# GPU batch size for embedding computation.
# Halved vs EfficientNet-B0 because 518×518 images are ~5× larger in VRAM.
# Reduce further if you get CUDA OOM errors on your GPU.
EMBEDDING_BATCH_SIZE = 16

# Cosine similarity thresholds (0.0–1.0, higher = more similar).
EMBEDDING_CONFIDENCE_HIGH = 0.90
EMBEDDING_CONFIDENCE_MED  = 0.75

# Ensure data directories exist
DATA_DIR.mkdir(exist_ok=True)
IMAGES_DIR.mkdir(exist_ok=True)

# ── Sticker / Label Compensation ─────────────────────────────────────────────
# Auto-detect solid-coloured rectangular stickers on scanned cards and inpaint
# them before hashing / embedding.  Improves identification accuracy when cards
# have price stickers or condition labels applied.
STICKER_AUTO_DETECT    = True  # default; overridden at runtime by settings.json
STICKER_INPAINT_RADIUS = 5     # OpenCV Telea inpaint neighbourhood radius (px)

# Sticker detection tuning — passed to identifier/sticker.detect_sticker()
STICKER_SIZE_MAX      = 0.30   # max sticker area as a fraction of card area (was implicit 0.25)
                                # raised to catch slightly larger labels
STICKER_COLOR_STD_MAX = 90     # max mean per-channel colour std-dev to qualify as a sticker
                                # ~55 = plain solid colour, ~90 = white label with text/logo

# ── CLAHE Normalization ───────────────────────────────────────────────────────
# Contrast Limited Adaptive Histogram Equalization applied to scans before
# hashing/embedding.  Compensates for aged/yellowed cards by normalizing
# brightness and contrast to a standard appearance, bringing scans closer to
# the clean TCGdex reference images.
# Disable if clean modern cards start misidentifying (unlikely with clip limit).
CLAHE_ENABLED    = True
CLAHE_CLIP_LIMIT = 2.0    # OpenCV default; higher = more contrast
CLAHE_TILE_SIZE  = (8, 8) # tile grid size for adaptive equalization

# ── Art-Zone Hash ─────────────────────────────────────────────────────────────
# When a sticker is detected or manually masked, the full-card hash is compared
# alongside a hash of just the illustration box (the middle portion of the card).
# The artwork sits away from both top and bottom sticker placement zones and is
# the most visually distinctive region across all card types (standard, full art,
# special illustration rare, trainer full art, energy cards, etc.).
#
# Coordinates are fractions of HASH_IMAGE_SIZE height (420 px):
#   y0 ≈ 55 px  — below the name/HP header
#   y1 ≈ 221 px — above the attack/stats text block
#
# After running "Setup → Rehash All Cards" the DB stores an additional
# phash_art / ahash_art / dhash_art / whash_art entry per card.
# np.minimum(full_distances, art_distances) is used when sticker is active,
# so a card with a clean art zone always wins regardless of sticker location.
HASH_ART_Y0 = 0.13   # top of illustration box  (≈ 55 px)
HASH_ART_Y1 = 0.53   # bottom of illustration box (≈ 221 px)

# ── eBay CSV Export ───────────────────────────────────────────────────────────
# All values are persisted in settings.json so the user only has to enter them
# once.  Loaded below from settings; these are just the in-process defaults.

_EBAY_DEFAULTS = {
    "ebay_site_params":        "SiteID=Canada|Country=CA|Currency=CAD|Version=1193|CC=UTF-8",
    "ebay_category_id":        "183454",
    "ebay_store_category":     "0",
    "ebay_location":           "",
    "ebay_dispatch_days":      "1",
    "ebay_best_offer_enabled": "1",
    "ebay_shipping_profile":   "",
    "ebay_return_profile":     "",
    "ebay_payment_profile":    "",
    # If True, fall back to the TCGdex reference image when no scan URL is set.
    # Disabled by default — eBay does not reliably transload TCGdex URLs.
    "ebay_tcgdex_pic_fallback": False,
    # Base URL prefix for user-hosted scan images (e.g. "https://my-bucket.s3.amazonaws.com/scans/")
    # The scan filename (stem of local_image_path) is appended automatically.
    "ebay_pic_url_base":        "",
    # imgbb API key — if set, scans are auto-uploaded to imgbb on CSV export
    # and the returned URL is used as PicURL. Images expire after 24 hours.
    # Get a free key at: https://api.imgbb.com/
    "ebay_imgbb_api_key":       "",
    # If True, upload scans to imgbb automatically when exporting CSV
    "ebay_imgbb_auto_upload":   False,
    # HTML listing description template.  Placeholders: {name} {set} {number} {rarity} {condition}
    "ebay_description_template": (
        "<h2>For Sale</h2>"
        "<p>The card you are viewing is <strong>{name}</strong> from <strong>{set}</strong>, "
        "card number {number} in <strong>{condition}</strong> condition.</p>"
        "<p>You are purchasing the exact card shown in the photos unless otherwise stated. "
        "Please review all images carefully for condition details.</p>"
        "<h3>Shipping</h3>"
        "<ul><li>Cards ship securely sleeved in a top loader.</li>"
        "<li>We combine shipping on multiple purchases.</li></ul>"
        "<h3>Authenticity</h3>"
        "<p>All items are 100% authentic. No proxies or reprints unless clearly stated.</p>"
    ),
}

def _ebay_setting(key: str):
    """Return the current value of an eBay export setting (settings.json > default)."""
    return _load_settings().get(key, _EBAY_DEFAULTS.get(key, ""))
