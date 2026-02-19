"""
imgbb image uploader for Pokemon Card Identifier.

Uploads a local scan to imgbb and returns the direct image URL.
Images are set to expire after 24 hours (86400 seconds) so they
are only accessible long enough for eBay to transload them on import.

API docs: https://api.imgbb.com/
"""

import base64
import urllib.parse
import urllib.request
import json
from pathlib import Path


# 24 hours in seconds
_EXPIRATION_SECONDS = 86400

_UPLOAD_URL = "https://api.imgbb.com/1/upload"


def upload_image(image_path: str | Path, api_key: str, name: str | None = None) -> str:
    """Upload a local image file to imgbb and return the direct image URL.

    Parameters
    ----------
    image_path : path to the local scan file (JPEG/PNG)
    api_key    : imgbb API key (from https://api.imgbb.com/)
    name       : optional filename to use on imgbb (e.g. the custom label).
                 Defaults to the stem of image_path if not provided.

    Returns
    -------
    str  — the direct image URL (e.g. "https://i.ibb.co/abc123/card.jpg")

    Raises
    ------
    ValueError  — if the API returns an error or the response is unexpected
    OSError     — if the file cannot be read
    """
    image_path = Path(image_path)
    image_data = base64.b64encode(image_path.read_bytes()).decode("utf-8")

    upload_name = name.strip() if name and name.strip() else image_path.stem

    # POST body: multipart would be cleaner but urllib supports form-encoded
    # base64 upload just fine for files up to ~32 MB.
    post_data = urllib.parse.urlencode({
        "key":        api_key,
        "image":      image_data,
        "name":       upload_name,
        "expiration": str(_EXPIRATION_SECONDS),
    }).encode("utf-8")

    req = urllib.request.Request(
        _UPLOAD_URL,
        data=post_data,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise ValueError(f"imgbb upload failed (HTTP {exc.code}): {body}") from exc

    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ValueError(f"imgbb returned non-JSON response: {body[:200]}") from exc

    if not parsed.get("success"):
        status = parsed.get("status", "?")
        error  = parsed.get("error", {})
        msg    = error.get("message", str(parsed)) if isinstance(error, dict) else str(error)
        raise ValueError(f"imgbb upload error (status {status}): {msg}")

    url = parsed["data"].get("url") or parsed["data"].get("display_url", "")
    if not url:
        raise ValueError(f"imgbb response missing URL: {parsed}")

    return url


def upload_batch(image_paths: list[str | Path], api_key: str,
                 progress_callback=None) -> dict[str, str]:
    """Upload multiple images and return a dict mapping local path → URL.

    Parameters
    ----------
    image_paths       : list of local file paths
    api_key           : imgbb API key
    progress_callback : optional callable(done: int, total: int) for progress updates

    Returns
    -------
    dict[str, str]  — {str(path): url}  (failed uploads are omitted; errors logged)
    """
    results: dict[str, str] = {}
    total = len(image_paths)
    for i, path in enumerate(image_paths):
        try:
            url = upload_image(path, api_key)
            results[str(path)] = url
        except Exception as exc:
            # Don't abort the whole batch on a single failure — leave it out
            print(f"[imgbb] Failed to upload {path}: {exc}")
        if progress_callback:
            progress_callback(i + 1, total)
    return results
