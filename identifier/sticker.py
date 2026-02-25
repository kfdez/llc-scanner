"""
Sticker / label detection and inpainting for card preprocessing.

Price stickers on scanned cards change pixel content, causing hash and ML
embedding scores to drift from clean reference images.  This module detects
rectangular sticker regions using contour analysis and inpaints them with
OpenCV's Telea algorithm before the image is hashed or embedded.

Public API
----------
detect_sticker(card_bgr)   → binary mask (uint8) or None
inpaint_sticker(card_bgr, mask, radius=5) → cleaned BGR image
mask_from_rect(h, w, rect_px)   → binary mask
scale_rect(rect_px, src_hw, dst_hw) → scaled rect tuple

Detection thresholds are in config.py (STICKER_SIZE_MAX, STICKER_COLOR_STD_MAX)
so they can be tuned without touching this file.
"""

import cv2
import numpy as np
from config import STICKER_SIZE_MAX, STICKER_COLOR_STD_MAX


# ── Auto-detection ─────────────────────────────────────────────────────────────

def detect_sticker(card_bgr: np.ndarray) -> "np.ndarray | None":
    """
    Return a binary mask (uint8, 255=sticker) or None if nothing found.

    Operates on the already-extracted card image (e.g. 300×420 BGR).
    Looks for solid-coloured rectangular regions that fit the profile of a
    price sticker: roughly 1–STICKER_SIZE_MAX% of card area, 4 straight edges,
    high fill ratio, low colour variance inside the bounding box.

    The colour std-dev threshold (STICKER_COLOR_STD_MAX) is set conservatively
    high (~90) so white stickers carrying printed text or a logo are still
    detected — a pure solid sticker scores ~0–10, a white label with dark
    text/logo typically scores ~60–90.

    Returns None if no plausible sticker is found.
    """
    h, w = card_bgr.shape[:2]
    card_area = h * w

    gray  = cv2.cvtColor(card_bgr, cv2.COLOR_BGR2GRAY)
    blur  = cv2.GaussianBlur(gray, (5, 5), 0)
    edges = cv2.Canny(blur, 30, 100)
    dil   = cv2.dilate(edges, np.ones((3, 3), np.uint8), iterations=1)

    contours, _ = cv2.findContours(dil, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    best: "tuple | None" = None
    best_score = 0.0

    for cnt in contours:
        area = cv2.contourArea(cnt)
        # Size: 1%–STICKER_SIZE_MAX% of card — too small = noise, too large = border/art
        if not (card_area * 0.01 <= area <= card_area * STICKER_SIZE_MAX):
            continue

        # Shape: must approximate to a quadrilateral
        peri   = cv2.arcLength(cnt, True)
        approx = cv2.approxPolyDP(cnt, 0.04 * peri, True)
        if len(approx) != 4:
            continue

        x, y, bw, bh = cv2.boundingRect(cnt)
        fill = area / (bw * bh) if bw * bh else 0
        # Must be mostly filled (a frame/outline would fail this)
        if fill < 0.70:
            continue

        roi = card_bgr[y:y + bh, x:x + bw]
        if roi.size == 0:
            continue

        # Colour uniformity: solid/white stickers have low-to-moderate std dev.
        # STICKER_COLOR_STD_MAX ~90 catches white labels with text (which have
        # ~60–90 std-dev) while still rejecting colourful card-art regions (>100).
        mean_std = float(np.mean(np.std(roi.reshape(-1, 3).astype(np.float32), axis=0)))
        if mean_std > STICKER_COLOR_STD_MAX:
            continue  # too much colour variation → not a price sticker

        score = fill / (mean_std + 1.0)
        if score > best_score:
            best_score, best = score, (x, y, bw, bh)

    if best is None:
        return None

    x, y, bw, bh = best
    # Add a small margin so we capture sticker edges completely
    margin = max(2, int(min(bw, bh) * 0.05))
    mask = np.zeros((h, w), dtype=np.uint8)
    mask[
        max(0, y - margin):min(h, y + bh + margin),
        max(0, x - margin):min(w, x + bw + margin),
    ] = 255
    return mask


# ── Inpainting ──────────────────────────────────────────────────────────────────

def inpaint_sticker(card_bgr: np.ndarray, mask: np.ndarray,
                    radius: int = 5) -> np.ndarray:
    """
    Inpaint the masked region with OpenCV Telea and return the cleaned image.

    Parameters
    ----------
    card_bgr : np.ndarray
        BGR card image (any size).
    mask : np.ndarray
        Binary mask, same spatial size as card_bgr (uint8, 255=region to fill).
    radius : int
        Inpaint neighbourhood radius (pixels).  5 works well for typical
        small price stickers.

    Returns
    -------
    np.ndarray
        BGR image with the masked region filled in.
    """
    return cv2.inpaint(card_bgr, mask, radius, cv2.INPAINT_TELEA)


# ── Coordinate helpers ──────────────────────────────────────────────────────────

def mask_from_rect(h: int, w: int, rect_px: tuple) -> np.ndarray:
    """
    Create a binary mask from a (x, y, bw, bh) pixel rectangle.

    Parameters
    ----------
    h, w : int
        Height and width of the output mask image.
    rect_px : tuple[int, int, int, int]
        (x, y, bw, bh) bounding rectangle in pixel coordinates.

    Returns
    -------
    np.ndarray
        uint8 mask of shape (h, w), 255 inside the rectangle, 0 elsewhere.
    """
    x, y, bw, bh = (int(v) for v in rect_px)
    mask = np.zeros((h, w), dtype=np.uint8)
    mask[max(0, y):min(h, y + bh), max(0, x):min(w, x + bw)] = 255
    return mask


def scale_rect(rect_px: tuple, src_hw: tuple, dst_hw: tuple) -> tuple:
    """
    Scale a (x, y, bw, bh) rectangle from one image size to another.

    Parameters
    ----------
    rect_px : tuple[float, float, float, float]
        Source rectangle as (x, y, bw, bh).
    src_hw : tuple[int, int]
        Source image (height, width).
    dst_hw : tuple[int, int]
        Destination image (height, width).

    Returns
    -------
    tuple[float, float, float, float]
        Scaled rectangle (x, y, bw, bh) in destination pixel coordinates.
    """
    x, y, bw, bh = rect_px
    sy = dst_hw[0] / src_hw[0]
    sx = dst_hw[1] / src_hw[1]
    return (x * sx, y * sy, bw * sx, bh * sy)
