"""
Media Processor — handles watermark detection, media downloading, and stock photo search.

Stock photo priority:
  1. Pixabay (primary) — free API, Russian language support, accessible from Russia.
     Register at https://pixabay.com/api/docs/ to get a free API key.
  2. Unsplash (fallback) — used only if Pixabay returns no results or is not configured.
"""

import asyncio
import logging
import os
import re
from typing import Optional, List, Tuple

import aiohttp
from PIL import Image

logger = logging.getLogger(__name__)


class MediaProcessor:
    """Processes media files: watermark detection and stock photo search."""

    def __init__(
        self,
        unsplash_key: str = "",
        pixabay_key: str = "",
        media_dir: str = "media",
    ):
        self.unsplash_key = unsplash_key
        self.pixabay_key = pixabay_key
        self.media_dir = media_dir
        os.makedirs(media_dir, exist_ok=True)

    def detect_watermark(self, image_path: str) -> Tuple[bool, float]:
        """
        Simple watermark detection using corner analysis.
        Checks if corners have semi-transparent overlays typical of watermarks.
        
        Returns:
            Tuple of (has_watermark: bool, confidence: float 0.0-1.0)
        """
        try:
            import cv2
            import numpy as np

            img = cv2.imread(image_path)
            if img is None:
                return False, 0.0

            h, w = img.shape[:2]
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

            # Check corners for typical watermark patterns
            corner_size = min(h, w) // 5
            corners = [
                gray[:corner_size, :corner_size],          # top-left
                gray[:corner_size, w - corner_size:],       # top-right
                gray[h - corner_size:, :corner_size],       # bottom-left
                gray[h - corner_size:, w - corner_size:],   # bottom-right
            ]

            # Calculate variance in corners - low variance + high brightness = potential watermark
            watermark_indicators = 0
            for corner in corners:
                mean_val = np.mean(corner)
                std_val = np.std(corner)
                # Watermarks often show as semi-transparent overlays
                # They tend to have consistent patterns (low std relative to mean)
                if std_val < 30 and mean_val > 200:
                    watermark_indicators += 1

            # Check for text-like patterns using edge detection
            edges = cv2.Canny(gray, 100, 200)
            bottom_strip = edges[int(h * 0.85):, :]
            edge_density_bottom = np.mean(bottom_strip) / 255.0

            # Many watermarks are in the bottom portion
            if edge_density_bottom > 0.15:
                watermark_indicators += 1

            has_watermark = watermark_indicators >= 2
            confidence = min(watermark_indicators / 4.0, 1.0)

            logger.info(f"Watermark detection for {image_path}: "
                       f"detected={has_watermark}, confidence={confidence:.2f}")
            return has_watermark, confidence

        except ImportError:
            logger.warning("OpenCV not available, skipping watermark detection")
            return False, 0.0
        except Exception as e:
            logger.error(f"Watermark detection failed: {e}")
            return False, 0.0

    async def search_stock_photo(self, keywords: List[str], count: int = 3) -> List[dict]:
        """Search for stock photos. Tries Wikimedia Commons first (no key, accessible from Russia),
        falls back to Unsplash if Wikimedia returns nothing.

        Returns:
            List of dicts with 'url', 'thumb_url', 'description', 'author', 'source'
        """
        results = await self._search_wikimedia(keywords, count)
        if not results:
            results = await self._search_unsplash(keywords, count)
        return results

    async def _search_wikimedia(self, keywords: List[str], count: int) -> List[dict]:
        """Search Wikimedia Commons for photos.

        Uses the public MediaWiki API — no key required, works from Russia.
        Two-step process:
          1. Full-text search in file namespace (NS=6)
          2. Resolve image URL + thumbnail for each result
        """
        query = " ".join(keywords[:4])
        results = []

        COMMONS_API = "https://commons.wikimedia.org/w/api.php"

        try:
            async with aiohttp.ClientSession(
                headers={"User-Agent": "IzhevskTodayNewsBot/1.0 (https://t.me/IzhevskTodayNews)"}
            ) as session:
                # ── Step 1: search for file titles ─────────────────────────

                # Try Russian query first, fall back to English keywords if empty
                async def _search_titles(q: str) -> List[str]:
                    params = {
                        "action": "query",
                        "list": "search",
                        "srsearch": q,
                        "srnamespace": "6",        # NS 6 = File:
                        "srlimit": str(count * 3), # fetch extra — many will be SVG/audio
                        "srwhat": "text",
                        "format": "json",
                    }
                    async with session.get(
                        COMMONS_API, params=params,
                        timeout=aiohttp.ClientTimeout(total=15),
                    ) as resp:
                        if resp.status != 200:
                            return []
                        data = await resp.json()
                        return [
                            item["title"]
                            for item in data.get("query", {}).get("search", [])
                        ]

                titles = await _search_titles(query)
                if not titles:
                    # Fallback: use first keyword in English if all were Cyrillic
                    en_kw = [k for k in keywords if k.isascii()][:2]
                    if en_kw:
                        titles = await _search_titles(" ".join(en_kw))

                if not titles:
                    logger.info(f"Wikimedia: no results for '{query}'")
                    return []

                # ── Step 2: resolve image URLs in one batch request ─────────

                # MediaWiki accepts up to 50 titles per request
                batch = "|".join(titles[: min(count * 3, 15)])
                img_params = {
                    "action": "query",
                    "titles": batch,
                    "prop": "imageinfo",
                    "iiprop": "url|mime|extmetadata",
                    "iiurlwidth": "1200",   # request a 1200px-wide scaled version
                    "format": "json",
                }
                async with session.get(
                    COMMONS_API, params=img_params,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status != 200:
                        logger.error(f"Wikimedia imageinfo {resp.status}")
                        return []
                    data = await resp.json()

                pages = data.get("query", {}).get("pages", {}).values()
                for page in pages:
                    if len(results) >= count:
                        break
                    infos = page.get("imageinfo", [])
                    if not infos:
                        continue
                    info = infos[0]

                    # Skip non-photo types
                    mime = info.get("mime", "")
                    if not mime.startswith("image/"):
                        continue
                    if mime in ("image/svg+xml", "image/gif"):
                        continue

                    full_url = info.get("url", "")
                    thumb_url = info.get("thumburl", full_url)
                    if not full_url:
                        continue

                    # Extract author and description from extmetadata
                    meta = info.get("extmetadata", {})
                    author = (
                        meta.get("Artist", {}).get("value", "")
                        or meta.get("Credit", {}).get("value", "Wikimedia Commons")
                    )
                    # Strip HTML tags from author string
                    author = re.sub(r"<[^>]+>", "", author).strip() or "Wikimedia Commons"

                    description = (
                        meta.get("ImageDescription", {}).get("value", "")
                        or page.get("title", "").replace("File:", "")
                    )
                    description = re.sub(r"<[^>]+>", "", description).strip()[:120]

                    results.append({
                        "url": full_url,
                        "thumb_url": thumb_url,
                        "description": description,
                        "author": author,
                        "source": "wikimedia",
                    })

                logger.info(f"Wikimedia: found {len(results)} photos for '{query}'")

        except asyncio.TimeoutError:
            logger.error("Wikimedia Commons API timeout (>15s)")
        except aiohttp.ClientError as e:
            logger.error(f"Wikimedia network error: {type(e).__name__}: {e}")
        except Exception as e:
            logger.error(f"Wikimedia search failed: {type(e).__name__}: {e}")

        return results

    async def _search_unsplash(self, keywords: List[str], count: int) -> List[dict]:
        """Search Unsplash as a fallback. Requires UNSPLASH_ACCESS_KEY."""
        if not self.unsplash_key:
            return []

        query = " ".join(keywords[:3])
        results = []

        try:
            async with aiohttp.ClientSession() as session:
                params = {
                    "query": query,
                    "per_page": count,
                    "orientation": "landscape",
                }
                headers = {"Authorization": f"Client-ID {self.unsplash_key}"}

                async with session.get(
                    "https://api.unsplash.com/search/photos",
                    params=params,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        for photo in data.get("results", []):
                            results.append({
                                "url": photo["urls"]["regular"],
                                "thumb_url": photo["urls"]["thumb"],
                                "description": photo.get("description", ""),
                                "author": photo["user"]["name"],
                                "source": "unsplash",
                            })
                        logger.info(
                            f"Unsplash: found {len(results)} photos for '{query}'"
                        )
                    else:
                        error = await resp.text()
                        logger.error(f"Unsplash API {resp.status}: {error[:200]}")

        except asyncio.TimeoutError:
            logger.error("Unsplash API timeout (>15s)")
        except aiohttp.ClientError as e:
            logger.error(f"Unsplash network error: {type(e).__name__}: {e}")
        except Exception as e:
            logger.error(f"Unsplash search failed: {type(e).__name__}: {e}")

        return results

    async def download_stock_photo(self, photo_url: str, filename: str) -> Optional[str]:
        """Download a stock photo and save it locally."""
        try:
            filepath = os.path.join(self.media_dir, filename)
            async with aiohttp.ClientSession() as session:
                async with session.get(photo_url) as resp:
                    if resp.status == 200:
                        content = await resp.read()
                        with open(filepath, "wb") as f:
                            f.write(content)
                        logger.info(f"Downloaded stock photo: {filepath}")
                        return filepath
                    else:
                        logger.error(f"Failed to download photo: {resp.status}")
        except Exception as e:
            logger.error(f"Photo download failed: {e}")
        return None

    def resize_for_telegram(self, image_path: str, max_size: int = 1280) -> str:
        """Resize image to fit Telegram's limits (max 1280px on longest side)."""
        try:
            img = Image.open(image_path)
            w, h = img.size

            if max(w, h) > max_size:
                ratio = max_size / max(w, h)
                new_size = (int(w * ratio), int(h * ratio))
                img = img.resize(new_size, Image.LANCZOS)

                # Save resized
                resized_path = image_path.rsplit(".", 1)
                resized_path = f"{resized_path[0]}_resized.{resized_path[1]}" if len(resized_path) > 1 else f"{image_path}_resized"
                img.save(resized_path, quality=90)
                logger.info(f"Resized image: {w}x{h} -> {new_size[0]}x{new_size[1]}")
                return resized_path

            return image_path
        except Exception as e:
            logger.error(f"Image resize failed: {e}")
            return image_path
