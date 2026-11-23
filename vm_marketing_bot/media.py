"""Downloading product photos from the WebDAV media server."""
from __future__ import annotations

import asyncio
import base64
import io
import logging
from urllib.parse import quote

import aiohttp

from .config import Settings

log = logging.getLogger(__name__)

MAX_SIDE = 2560
SHRINK_ABOVE_BYTES = 3 * 1024 * 1024


def build_image_url(base_url: str, photo_path: str) -> str:
    photo_path = (photo_path or "").strip().replace("\\", "/")
    if photo_path.startswith(("http://", "https://")):
        url = photo_path
    else:
        url = base_url.rstrip("/") + "/" + photo_path.lstrip("/")
    return quote(url, safe=":/?&=%")


def shrink_image(data: bytes, force: bool = False) -> bytes:
    """Make an image Telegram-friendly (JPEG, max 2560px). Returns original on failure."""
    if not force and len(data) <= SHRINK_ABOVE_BYTES:
        return data
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as img:
            img = img.convert("RGB")
            img.thumbnail((MAX_SIDE, MAX_SIDE))
            out = io.BytesIO()
            img.save(out, format="JPEG", quality=85, optimize=True)
            return out.getvalue()
    except Exception as exc:  # pragma: no cover - depends on image
        log.warning("Image shrink failed: %s", exc)
        return data


class ImageFetcher:
    def __init__(self, settings: Settings, session: aiohttp.ClientSession, parallel: int = 4):
        self._settings = settings
        self._session = session
        token = base64.b64encode(f"{settings.webdav_user}:{settings.webdav_password}".encode()).decode()
        self._headers = {"Authorization": f"Basic {token}"}
        self._sem = asyncio.Semaphore(parallel)

    async def fetch(self, photo_path: str, attempts: int = 3) -> bytes | None:
        if not photo_path:
            return None
        url = build_image_url(self._settings.base_image_url, photo_path)
        async with self._sem:
            for attempt in range(1, attempts + 1):
                try:
                    async with self._session.get(
                        url,
                        headers=self._headers,
                        ssl=False,
                        timeout=aiohttp.ClientTimeout(total=60),
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.read()
                            return await asyncio.to_thread(shrink_image, data)
                        if resp.status in (401, 403, 404):
                            log.warning("Image %s: HTTP %s", url, resp.status)
                            return None
                        log.warning("Image %s: HTTP %s (attempt %s)", url, resp.status, attempt)
                except Exception as exc:
                    log.warning("Image %s: %s (attempt %s)", url, exc, attempt)
                await asyncio.sleep(1.5 * attempt)
        return None
