"""
Media Processor — handles watermark detection, media downloading, and stock photo search.

Stock photo priority:
  1. Wikimedia Commons (primary) — free, no key required, works from Russia.
  2. Pixabay (fallback) — free API, register at https://pixabay.com/api/docs/
  3. Pexels (last resort) — blocked from Russian IPs by Cloudflare.
     Register at https://www.pexels.com/api/ to get a free API key.
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
        pexels_key: str = "",
        media_dir: str = "media",
    ):
        self.unsplash_key = unsplash_key
        self.pixabay_key = pixabay_key
        self.pexels_key = pexels_key  # Reserved for future Pexels API integration
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
        """Search for stock photos.

        Priority:
          1. Wikimedia Commons — free, no key, works from Russia
          2. Pixabay — free API, good variety
          3. Pexels — best quality but blocked from Russian IPs by Cloudflare
        """
        results = await self._search_wikimedia(keywords, count)
        if not results:
            results = await self._search_pixabay(keywords, count)
        if not results:
            results = await self._search_pexels(keywords, count)
        return results

    async def _search_wikimedia(self, keywords: List[str], count: int) -> List[dict]:
        """Search Wikimedia Commons for photos.

        Uses the public MediaWiki API — no key required, works from Russia.
        Two-step process:
          1. Full-text search in file namespace (NS=6)
          2. Resolve image URL + thumbnail for each result

        Search strategy: topic keywords FIRST (best relevance), Izhevsk only as fallback.
        """
        query = " ".join(keywords[:4])
        results = []

        COMMONS_API = "https://commons.wikimedia.org/w/api.php"

        try:
            async with aiohttp.ClientSession(
                headers={"User-Agent": "IzhevskTodayNewsBot/1.0 (https://t.me/IzhevskTodayNews)"}
            ) as session:
                # ── Step 1: search for file titles ─────────────────────────

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

                # Strategy: topic keywords first → best relevance for the subject
                # Only add Izhevsk as a later fallback for local place queries
                generic_q = " ".join(keywords[:3])
                titles = await _search_titles(generic_q)
                if titles:
                    logger.info(f"Wikimedia: found results for topic query '{generic_q}'")
                if not titles:
                    # Fallback: try with Izhevsk prefix (for local-specific topics)
                    query = " ".join(["Izhevsk"] + keywords[:3])
                    titles = await _search_titles(query)
                    if titles:
                        logger.info(f"Wikimedia: found results with Izhevsk prefix for '{query}'")
                if not titles:
                    # Last resort: English keywords only
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
                    "iiprop": "url|mime|extmetadata|size",
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

                    # Skip low-resolution images (thumbnails, old scans, screenshots)
                    width = info.get("width", 0)
                    height = info.get("height", 0)
                    if width < 600 or height < 400:
                        logger.debug(f"Wikimedia: skipping small image {width}x{height}")
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

    async def _search_pixabay(self, keywords: List[str], count: int) -> List[dict]:
        """Search Pixabay for photos. Requires PIXABAY_API_KEY.

        Free API — register at https://pixabay.com/api/docs/
        Accessible from Russia; supports English and Russian queries.
        """
        if not self.pixabay_key:
            logger.info("Pixabay: no API key configured, skipping")
            return []

        query = " ".join(keywords[:4])
        results = []

        try:
            async with aiohttp.ClientSession() as session:
                params = {
                    "key": self.pixabay_key,
                    "q": query,
                    "image_type": "photo",
                    "orientation": "horizontal",
                    "per_page": count + 5,   # fetch extra, filter later
                    "safesearch": "true",
                    "lang": "en",
                }
                async with session.get(
                    "https://pixabay.com/api/",
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        for hit in data.get("hits", [])[:count]:
                            results.append({
                                "url": hit["largeImageURL"],
                                "thumb_url": hit["previewURL"],
                                "description": hit.get("tags", ""),
                                "author": hit.get("user", "Pixabay"),
                                "source": "pixabay",
                            })
                        logger.info(
                            f"Pixabay: found {len(results)} photos for '{query}'"
                        )
                    elif resp.status == 400:
                        logger.error("Pixabay: bad request (check API key or query)")
                    elif resp.status == 429:
                        logger.warning("Pixabay: rate limit exceeded")
                    else:
                        logger.error(f"Pixabay API {resp.status}")

        except asyncio.TimeoutError:
            logger.error("Pixabay API timeout (>15s)")
        except aiohttp.ClientError as e:
            logger.error(f"Pixabay network error: {type(e).__name__}: {e}")
        except Exception as e:
            logger.error(f"Pixabay search failed: {type(e).__name__}: {e}")

        return results

    async def _search_pexels(self, keywords: List[str], count: int) -> List[dict]:
        """Search Pexels for photos. Requires PEXELS_API_KEY.

        Free API — 200 requests/hour. Register at https://www.pexels.com/api/
        Excellent photo quality, supports Russian queries.
        """
        if not self.pexels_key:
            logger.debug("Pexels: no API key configured, skipping")
            return []

        query = " ".join(keywords[:4])
        results = []

        try:
            headers = {
                "Authorization": self.pexels_key,
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            }
            async with aiohttp.ClientSession(headers=headers) as session:
                params = {
                    "query": query,
                    "per_page": count + 3,   # fetch extra to filter
                    "orientation": "landscape",
                }
                async with session.get(
                    "https://api.pexels.com/v1/search",
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        for photo in data.get("photos", [])[:count]:
                            # Use 'large' size — good quality, reasonable file size
                            url = photo.get("src", {}).get("large", "")
                            if not url:
                                continue
                            results.append({
                                "url": url,
                                "thumb_url": photo.get("src", {}).get("medium", url),
                                "description": photo.get("alt", "") or query,
                                "author": photo.get("photographer", "Pexels"),
                                "source": "pexels",
                            })
                        logger.info(
                            f"Pexels: found {len(results)} photos for '{query}'"
                        )
                    elif resp.status == 429:
                        logger.warning("Pexels: rate limit exceeded (200 req/hour)")
                    elif resp.status == 401:
                        logger.error("Pexels: invalid API key")
                    else:
                        body = await resp.text()
                        logger.error(f"Pexels API {resp.status}: {body[:200]}")

        except asyncio.TimeoutError:
            logger.error("Pexels API timeout (>15s)")
        except aiohttp.ClientError as e:
            logger.error(f"Pexels network error: {type(e).__name__}: {e}")
        except Exception as e:
            logger.error(f"Pexels search failed: {type(e).__name__}: {e}")

        return results

    async def download_stock_photo(self, photo_url: str, filename: str) -> Optional[str]:
        """Download a stock photo and save it locally.

        Sends a proper User-Agent to avoid 403 from Wikimedia and similar CDNs.
        """
        try:
            filepath = os.path.join(self.media_dir, filename)
            headers = {
                "User-Agent": "IzhevskTodayNewsBot/1.0 (https://t.me/IzhevskTodayNews)"
            }
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.get(
                    photo_url, timeout=aiohttp.ClientTimeout(total=20)
                ) as resp:
                    if resp.status == 200:
                        content = await resp.read()
                        if len(content) > 1000:  # Sanity check
                            with open(filepath, "wb") as f:
                                f.write(content)
                            logger.info(f"Downloaded stock photo: {filepath}")
                            return filepath
                        else:
                            logger.warning(f"Stock photo too small ({len(content)} bytes): {photo_url}")
                    else:
                        logger.error(f"Failed to download photo: {resp.status} from {photo_url}")
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
