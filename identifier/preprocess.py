"""
Shared image preprocessing for card identification.

Two public functions are provided:
  - preprocess_for_hashing(image_path)  → PIL.Image at HASH_IMAGE_SIZE
      Full card-detection pipeline (Canny + adaptive threshold + Otsu),
      perspective warp when a card quad is found, centre-crop fallback.
      Used by the hash-based matcher.

  - preprocess_for_embedding(image_path) → np.ndarray (3, 224, 224) float32
      Simple centre-crop to card aspect ratio then resize to 224×224,
      normalised with ImageNet mean/std ready for torch.
      Does NOT use perspective warp — CNNs handle natural variation better
      than geometrically distorted crops.
      Used by the ML embedding matcher.
"""

import cv2
import numpy as np
from PIL import Image

from config import (HASH_IMAGE_SIZE, EMBEDDING_INPUT_SIZE,
                    STICKER_AUTO_DETECT, STICKER_INPAINT_RADIUS,
                    CLAHE_ENABLED, CLAHE_CLIP_LIMIT, CLAHE_TILE_SIZE)

# Standard Pokemon card aspect ratio: height / width  (88 mm / 63 mm ≈ 1.396)
_CARD_ASPECT = 88 / 63

# ImageNet normalisation constants (timm standard)
_IMAGENET_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
_IMAGENET_STD  = np.array([0.229, 0.224, 0.225], dtype=np.float32)

# CLAHE object (created lazily on first use)
_clahe: "cv2.cuda.CLAHE | None" = None


def _get_clahe() -> "cv2.cuda.CLAHE | None":
    """Lazily create CLAHE object with configured parameters."""
    global _clahe
    if _clahe is None and CLAHE_ENABLED:
        _clahe = cv2.createCLAHE(clipLimit=CLAHE_CLIP_LIMIT, tileGridSize=CLAHE_TILE_SIZE)
    return _clahe


def _apply_clahe(bgr: np.ndarray) -> np.ndarray:
    """Apply CLAHE to the BGR image and return the result.

    Applies to the L channel in LAB color space for better color preservation.
    Handles both CPU and GPU CLAHE gracefully.
    """
    clahe = _get_clahe()
    if clahe is None:
        return bgr

    try:
        # Try GPU version first (faster on systems with CUDA)
        lab = cv2.cvtColor(bgr, cv2.COLOR_BGR2LAB)
        lab[:, :, 0] = clahe.apply(lab[:, :, 0])
        return cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)
    except Exception:
        # Fallback: just return original if CLAHE fails
        return bgr


# ── Private helpers ───────────────────────────────────────────────────────────

def _order_points(pts: np.ndarray) -> np.ndarray:
    """Order four points as: top-left, top-right, bottom-right, bottom-left."""
    rect = np.zeros((4, 2), dtype=np.float32)
    s = pts.sum(axis=1)
    rect[0] = pts[np.argmin(s)]   # top-left  (smallest x+y)
    rect[2] = pts[np.argmax(s)]   # bottom-right (largest x+y)
    diff = np.diff(pts, axis=1)
    rect[1] = pts[np.argmin(diff)]  # top-right  (smallest y-x)
    rect[3] = pts[np.argmax(diff)]  # bottom-left (largest y-x)
    return rect


def _four_point_transform(image: np.ndarray, pts: np.ndarray) -> np.ndarray | None:
    """
    Apply a perspective warp to the region defined by four points.
    Returns the warped image at its natural dimensions, or None if degenerate.
    """
    pts = _order_points(pts)
    (tl, tr, br, bl) = pts

    width_a = np.linalg.norm(br - bl)
    width_b = np.linalg.norm(tr - tl)
    max_width = max(int(width_a), int(width_b))

    height_a = np.linalg.norm(tr - br)
    height_b = np.linalg.norm(tl - bl)
    max_height = max(int(height_a), int(height_b))

    if max_width < 10 or max_height < 10:
        return None

    dst = np.array([
        [0, 0],
        [max_width - 1, 0],
        [max_width - 1, max_height - 1],
        [0, max_height - 1],
    ], dtype=np.float32)

    M = cv2.getPerspectiveTransform(pts, dst)
    return cv2.warpPerspective(image, M, (max_width, max_height))


def _is_card_shaped(pts: np.ndarray, img_h: int, img_w: int) -> bool:
    """
    Return True if the four-point region is plausibly a Pokemon card:
    - Aspect ratio (h/w) within 30% of the standard card ratio or its inverse
    - Quad area is at least 5% of the total image area
    """
    pts = _order_points(pts)
    (tl, tr, br, bl) = pts

    w = max(np.linalg.norm(tr - tl), np.linalg.norm(br - bl))
    h = max(np.linalg.norm(tl - bl), np.linalg.norm(tr - br))

    if w < 20 or h < 20:
        return False

    aspect = h / w
    card_asp = _CARD_ASPECT
    inv_asp = 1.0 / card_asp

    portrait_ok  = abs(aspect - card_asp) / card_asp < 0.30
    landscape_ok = abs(aspect - inv_asp)  / inv_asp  < 0.30

    if not (portrait_ok or landscape_ok):
        return False

    quad_area = cv2.contourArea(pts.reshape(4, 1, 2).astype(np.float32))
    img_area  = img_h * img_w
    return (quad_area / img_area) >= 0.05


def _detect_card_quad(gray: np.ndarray, img_h: int, img_w: int) -> np.ndarray | None:
    """
    Try three preprocessing strategies (Canny, adaptive threshold, Otsu) to
    find a card-shaped quadrilateral. Returns the best 4-point float32 array
    (shape 4×2) sorted by contour area, or None if nothing plausible was found.
    """
    candidates: list[tuple[float, np.ndarray]] = []

    def _try_edges(edged: np.ndarray) -> None:
        contours, _ = cv2.findContours(edged, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        contours = sorted(contours, key=cv2.contourArea, reverse=True)
        for cnt in contours[:10]:
            peri = cv2.arcLength(cnt, True)
            for eps in (0.01, 0.02, 0.04, 0.06):
                approx = cv2.approxPolyDP(cnt, eps * peri, True)
                if len(approx) == 4:
                    pts = approx.reshape(4, 2).astype(np.float32)
                    if _is_card_shaped(pts, img_h, img_w):
                        candidates.append((cv2.contourArea(cnt), pts))
                    break  # stop trying epsilon values once a quad is found

    # Method 1: Canny edges
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    edged = cv2.Canny(blurred, 30, 120)
    edged = cv2.dilate(edged, None, iterations=1)
    _try_edges(edged)

    # Method 2: Adaptive threshold
    thresh = cv2.adaptiveThreshold(
        cv2.GaussianBlur(gray, (7, 7), 0), 255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 11, 2,
    )
    thresh = cv2.bitwise_not(thresh)
    thresh = cv2.dilate(thresh, None, iterations=2)
    _try_edges(thresh)

    # Method 3: Otsu threshold
    _, otsu = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    otsu = cv2.bitwise_not(otsu)
    otsu = cv2.dilate(otsu, None, iterations=1)
    _try_edges(otsu)

    if not candidates:
        return None

    # Return the candidate with the largest contour area
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


def _center_crop_to_card(img_bgr: np.ndarray, target_size: tuple[int, int]) -> np.ndarray:
    """
    Centre-crop img_bgr to card aspect ratio, then resize to target_size.
    target_size is (width, height) as expected by cv2.resize.
    """
    h, w = img_bgr.shape[:2]
    card_asp = _CARD_ASPECT  # height / width

    if h / w > card_asp:
        # Image is taller than card — trim top and bottom
        crop_h = int(w * card_asp)
        y0 = (h - crop_h) // 2
        cropped = img_bgr[y0: y0 + crop_h, :]
    else:
        # Image is wider than card — trim left and right
        crop_w = int(h / card_asp)
        x0 = (w - crop_w) // 2
        cropped = img_bgr[:, x0: x0 + crop_w]

    return cv2.resize(cropped, target_size)


# ── Public API ────────────────────────────────────────────────────────────────

def preprocess_to_card_image(image_path: str) -> np.ndarray:
    """
    Extract and return the card region as a BGR numpy array at HASH_IMAGE_SIZE.

    Applies the same quad-detection / perspective-warp / centre-crop logic as
    preprocess_for_hashing but returns the raw BGR array instead of computing
    hashes.  Used by the GUI to display the card crop in the sticker-mask
    dialog so the user can draw a mask in card-image coordinates.
    """
    img_bgr = cv2.imread(str(image_path))
    if img_bgr is None:
        raise ValueError(f"Cannot read image: {image_path}")

    h, w = img_bgr.shape[:2]
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    quad = _detect_card_quad(gray, h, w)

    if quad is not None:
        warped = _four_point_transform(img_bgr, quad)
        return (
            cv2.resize(warped, HASH_IMAGE_SIZE)
            if warped is not None
            else _center_crop_to_card(img_bgr, HASH_IMAGE_SIZE)
        )
    return _center_crop_to_card(img_bgr, HASH_IMAGE_SIZE)


def preprocess_for_hashing(image_path: str,
                            sticker_mask_px: "tuple | None" = None,
                            auto_detect: "bool | None" = None) -> Image.Image:
    """
    Full card-detection preprocessing pipeline for the hash matcher.

    1. Detect card boundary via Canny / adaptive / Otsu edge detection
    2. Validate detected quad has card-like aspect ratio
    3. Apply perspective warp; fallback to centre-crop if no valid quad found
    4. Optionally detect and inpaint price stickers
    5. Resize to HASH_IMAGE_SIZE and return as RGB PIL Image

    Parameters
    ----------
    sticker_mask_px : tuple | None
        Manual sticker region as (x, y, bw, bh) in HASH_IMAGE_SIZE pixel
        space (300×420).  If provided, the region is inpainted before hashing
        regardless of the auto_detect setting.
    auto_detect : bool | None
        Whether to run automatic sticker detection.  None → use the
        STICKER_AUTO_DETECT config constant (default True).
    """
    from identifier.sticker import detect_sticker, inpaint_sticker, mask_from_rect

    _auto = auto_detect if auto_detect is not None else STICKER_AUTO_DETECT

    img_bgr = cv2.imread(str(image_path))
    if img_bgr is None:
        raise ValueError(f"Cannot read image: {image_path}")

    # ── Manual mask: inpaint on full scan BEFORE card extraction ─────────────
    # sticker_mask_px is in original scan pixel coordinates.
    if sticker_mask_px is not None:
        ih, iw = img_bgr.shape[:2]
        manual_mask = mask_from_rect(ih, iw, sticker_mask_px)
        img_bgr = inpaint_sticker(img_bgr, manual_mask, STICKER_INPAINT_RADIUS)

    # Card extraction
    h, w = img_bgr.shape[:2]
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    quad = _detect_card_quad(gray, h, w)

    if quad is not None:
        warped = _four_point_transform(img_bgr, quad)
        card_bgr = (
            cv2.resize(warped, HASH_IMAGE_SIZE)
            if warped is not None
            else _center_crop_to_card(img_bgr, HASH_IMAGE_SIZE)
        )
    else:
        card_bgr = _center_crop_to_card(img_bgr, HASH_IMAGE_SIZE)

    # ── Auto-detect: runs on the extracted card crop (no manual mask set) ─────
    if sticker_mask_px is None and _auto:
        auto_mask = detect_sticker(card_bgr)
        if auto_mask is not None:
            card_bgr = inpaint_sticker(card_bgr, auto_mask, STICKER_INPAINT_RADIUS)

    # ── CLAHE: normalize brightness/contrast for aged/yellowed cards ────────────
    card_bgr = _apply_clahe(card_bgr)

    rgb = cv2.cvtColor(card_bgr, cv2.COLOR_BGR2RGB)
    return Image.fromarray(rgb)


def preprocess_for_embedding(image_path: str,
                              sticker_mask_px: "tuple | None" = None,
                              auto_detect: "bool | None" = None) -> np.ndarray:
    """
    Simple preprocessing for the ML embedding matcher.

    Centre-crops the image to card aspect ratio, resizes to
    EMBEDDING_INPUT_SIZE × EMBEDDING_INPUT_SIZE (518 for DINOv2, 224 for
    EfficientNet-B0), optionally removes price stickers, then normalises with
    ImageNet mean/std (timm convention).

    Returns a float32 numpy array of shape (3, S, S) in CHW layout where
    S = EMBEDDING_INPUT_SIZE, ready to be converted to a torch tensor with
    torch.from_numpy().

    Does NOT apply perspective warp — pretrained ViTs are robust to mild
    angle/perspective variation, and an incorrect warp can corrupt card
    artwork and degrade accuracy.

    Parameters
    ----------
    sticker_mask_px : tuple | None
        Manual sticker region as (x, y, bw, bh) in **original scan** pixel
        space (same as drawn in the sticker mask dialog).  Applied to the
        full scan before centre-crop.
    auto_detect : bool | None
        Whether to run automatic sticker detection.  None → use the
        STICKER_AUTO_DETECT config constant.
    """
    from identifier.sticker import detect_sticker, inpaint_sticker, mask_from_rect

    _auto = auto_detect if auto_detect is not None else STICKER_AUTO_DETECT

    img_bgr = cv2.imread(str(image_path))
    if img_bgr is None:
        raise ValueError(f"Cannot read image: {image_path}")

    # ── Manual mask: inpaint on full scan BEFORE centre-crop ─────────────────
    if sticker_mask_px is not None:
        ih, iw = img_bgr.shape[:2]
        manual_mask = mask_from_rect(ih, iw, sticker_mask_px)
        img_bgr = inpaint_sticker(img_bgr, manual_mask, STICKER_INPAINT_RADIUS)

    size = EMBEDDING_INPUT_SIZE   # 518 for DINOv2, 224 for EfficientNet-B0
    card_bgr = _center_crop_to_card(img_bgr, (size, size))

    # ── Auto-detect: runs on the extracted card crop (no manual mask set) ─────
    if sticker_mask_px is None and _auto:
        auto_mask = detect_sticker(card_bgr)
        if auto_mask is not None:
            card_bgr = inpaint_sticker(card_bgr, auto_mask, STICKER_INPAINT_RADIUS)

    # ── CLAHE: normalize brightness/contrast for aged/yellowed cards ────────────
    card_bgr = _apply_clahe(card_bgr)

    rgb = cv2.cvtColor(card_bgr, cv2.COLOR_BGR2RGB)

    # Normalise: scale to [0,1] then apply ImageNet mean/std
    arr = rgb.astype(np.float32) / 255.0           # (S, S, 3)  HWC
    arr = (arr - _IMAGENET_MEAN) / _IMAGENET_STD   # (S, S, 3)  HWC
    arr = arr.transpose(2, 0, 1)                   # (3, S, S)  CHW
    return arr
