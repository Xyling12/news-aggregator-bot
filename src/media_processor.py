"""
Media Processor — handles watermark detection, media downloading, and stock photo search.

Stock photo priority:
  1. Pexels (primary) — best quality, 200 req/hour free. Routed via NL VPS SOCKS5 proxy
     (PEXELS_PROXY=socks5://user:pass@host:port) to bypass Cloudflare geo-block.
  2. Pixabay (fallback) — free API, register at https://pixabay.com/api/docs/
  3. Wikimedia Commons (last resort) — free, no key required.
"""

import asyncio
import logging
import os
import re
from typing import Optional, List, Tuple

import aiohttp
try:
    from aiohttp_socks import ProxyConnector
    SOCKS_AVAILABLE = True
except ImportError:
    SOCKS_AVAILABLE = False
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
          1. Pexels — best quality, routed via NL VPS SOCKS5 proxy (PEXELS_PROXY env)
          2. Pixabay — free API, good variety
          3. Wikimedia Commons — free, no key, last resort
        """
        results = await self._search_pexels(keywords, count)
        if not results:
            results = await self._search_pixabay(keywords, count)
        if not results:
            results = await self._search_wikimedia(keywords, count)
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
                        "srlimit": str(min(count * 6, 30)),  # more candidates for strict filtering
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

                # Filenames that indicate low-quality / irrelevant file types
                _BAD_FILENAME_WORDS = {
                    "plaque", "tablet", "tablica", "sign", "inscription",
                    "memorial", "monument", "logo", "coat", "arm", "герб",
                    "табличк", "надпись", "значок", "эмблем", "stamp",
                    "diagram", "схем", "map", "карт", "chart", "graph",
                    "screenshot", "скриншот", "scan", "скан", "postcard",
                    "открытк",
                }

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

                    # Skip bad filenames (tablets, plaques, logos, diagrams)
                    fname_lower = (page.get("title", "") + full_url).lower()
                    if any(bad in fname_lower for bad in _BAD_FILENAME_WORDS):
                        logger.debug(f"Wikimedia: skipping bad filename {page.get('title', '')}")
                        continue

                    # Require reasonable resolution — no thumbnails or old scans
                    width = info.get("width", 0)
                    height = info.get("height", 0)
                    if width < 800 or height < 500:
                        logger.debug(f"Wikimedia: skipping low-res {width}x{height}")
                        continue

                    # Filter out extreme portrait (tall narrow) shots only
                    if height > 0 and width / height < 0.5:
                        logger.debug(f"Wikimedia: skipping extreme portrait {width}x{height}")
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
        Auto-falls back to direct connection if proxy fails.
        """
        if not self.pexels_key:
            logger.debug("Pexels: no API key configured, skipping")
            return []

        query = " ".join(keywords[:4])
        results = []

        headers = {
            "Authorization": self.pexels_key,
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        }
        proxy_url = os.getenv("PEXELS_PROXY", "").strip()
        use_proxy = bool(proxy_url)
        import requests
        
        async def _do_request(with_proxy: bool) -> List[dict]:
            def sync_fetch():
                url = "https://api.pexels.com/v1/search"
                params = {
                    "query": query,
                    "per_page": count + 5,
                    "orientation": "landscape",
                    "locale": "ru-RU",
                }
                proxies = {"http": proxy_url, "https": proxy_url} if (with_proxy and proxy_url) else None
                if proxies:
                    logger.debug(f"Pexels: using proxy by requests {proxy_url[:30]}...")
                
                return requests.get(url, headers=headers, params=params, proxies=proxies, timeout=15)
            
            found = []
            try:
                resp = await asyncio.to_thread(sync_fetch)
                if resp.status_code == 200:
                    data = resp.json()
                    all_photos = data.get("photos", [])

                    people_words = {"people", "person", "man", "woman", "group",
                                    "team", "couple", "family", "children", "russian"}
                    query_words = set(query.lower().split())
                    query_wants_people = bool(query_words & people_words)

                    if not query_wants_people and len(all_photos) > 1:
                        people_alt = {"people", "person", "man", "woman", "group",
                                      "team", "couple", "meeting", "coworkers"}
                        all_photos.sort(key=lambda p: any(
                            w in (p.get("alt", "") or "").lower()
                            for w in people_alt
                        ))

                    for photo in all_photos[:count]:
                        url = photo.get("src", {}).get("large", "")
                        if not url:
                            continue
                        found.append({
                            "url": url,
                            "thumb_url": photo.get("src", {}).get("medium", url),
                            "description": photo.get("alt", "") or query,
                            "author": photo.get("photographer", "Pexels"),
                            "source": "pexels",
                        })
                    logger.info(f"Pexels: found {len(found)} photos for '{query}'")
                elif resp.status_code == 429:
                    logger.warning("Pexels: rate limit exceeded (200 req/hour)")
                elif resp.status_code == 401:
                    logger.error("Pexels: invalid API key")
                else:
                    logger.error(f"Pexels API {resp.status_code}: {resp.text[:200]}")
                    if with_proxy:
                        raise RuntimeError(f"Proxy returned HTTP {resp.status_code}")
            except Exception as e:
                logger.error(f"Pexels requests proxy error: {e}")
                if with_proxy:
                    raise  # Re-raise to trigger the direct fallback in the outer block
            
            return found

        try:
            results = await _do_request(with_proxy=use_proxy)
        except asyncio.TimeoutError:
            logger.error("Pexels API timeout (>15s)")
        except Exception as e:
            if use_proxy:
                logger.warning(f"Pexels proxy failed ({type(e).__name__}: {e}) — retrying direct")
                try:
                    results = await _do_request(with_proxy=False)
                except Exception as e2:
                    logger.error(f"Pexels direct also failed: {type(e2).__name__}: {e2}")
            else:
                logger.error(f"Pexels search failed: {type(e).__name__}: {e}")

        return results

    async def search_pexels_video(
        self,
        keywords: List[str],
        min_duration: int = 5,
        max_duration: int = 30,
        min_quality_px: int = 1080,   # min pixels on longer side (HD threshold)
        exclude_ids: Optional[List[int]] = None,   # already-used Pexels video IDs
        max_pages: int = 3,
        allow_repeat_fallback: bool = True,
    ) -> Optional[tuple]:
        candidate = await self.search_pexels_video_candidate(
            keywords,
            min_duration=min_duration,
            max_duration=max_duration,
            min_quality_px=min_quality_px,
            exclude_ids=exclude_ids,
            max_pages=max_pages,
            allow_repeat_fallback=allow_repeat_fallback,
        )
        if not candidate:
            return None
        return (candidate["id"], candidate["url"])

    async def search_pexels_video_candidate(
        self,
        keywords: List[str],
        min_duration: int = 5,
        max_duration: int = 30,
        min_quality_px: int = 1080,
        exclude_ids: Optional[List[int]] = None,
        max_pages: int = 3,
        allow_repeat_fallback: bool = True,
    ) -> Optional[dict]:
        """Search Pexels for short portrait videos. Requires PEXELS_API_KEY.

        Quality filtering:
          - Only returns files where the longer dimension >= min_quality_px (default 1080)
          - Sorts candidates by pixel count (width × height) — highest quality first
          - Skips quality tags: 'sd', '360', '240' in file quality field

        Returns {"id": video_id, "url": best_url, "urls": [fallbacks...]} or None.
        """
        if not self.pexels_key:
            logger.debug("Pexels Video: no API key configured, skipping")
            return None

        query = " ".join(keywords[:3]) or "animals"
        exclude_ids = set(exclude_ids or [])

        headers = {
            "Authorization": self.pexels_key,
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        }
        proxy_url = os.getenv("PEXELS_PROXY", "").strip()
        use_proxy = bool(proxy_url and SOCKS_AVAILABLE and proxy_url.startswith("socks"))

        async def _fetch_videos(with_proxy: bool, page: int):
            if with_proxy:
                connector = ProxyConnector.from_url(proxy_url)
                sess = aiohttp.ClientSession(connector=connector, headers=headers)
            else:
                sess = aiohttp.ClientSession(headers=headers)
            async with sess as session:
                params = {
                    "query": query,
                    "per_page": 15,
                    "page": page,
                    "orientation": "portrait",
                }
                async with session.get(
                    "https://api.pexels.com/videos/search",
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status != 200:
                        logger.error(f"Pexels Video API {resp.status}")
                        return None
                    return (await resp.json()).get("videos", [])

        videos: list = []
        for page in range(1, max_pages + 1):
            page_videos = None
            try:
                page_videos = await _fetch_videos(with_proxy=use_proxy, page=page)
            except Exception as e:
                if use_proxy:
                    logger.warning(f"Pexels Video proxy failed ({e}) — retrying direct")
                    try:
                        page_videos = await _fetch_videos(with_proxy=False, page=page)
                    except Exception as e2:
                        logger.error(f"Pexels Video direct also failed: {e2}")
                        return None
                else:
                    logger.error(f"Pexels Video search failed: {e}")
                    return None

            if not page_videos:
                break
            videos.extend(page_videos)

        if not videos:
            logger.warning(f"Pexels Video: no results for '{query}'")
            return None

        # Keep first occurrence only (stable order), then randomize for variety.
        uniq = {}
        for v in videos:
            vid = v.get("id")
            if vid and vid not in uniq:
                uniq[vid] = v
        videos = list(uniq.values())

        import random
        random.shuffle(videos)

        def _ranked_hd_links(files: list) -> List[str]:
            """Return ranked MP4 URLs for one Pexels video, best candidate first."""
            _bad_quality = {"sd", "360p", "240p"}
            hd_files = [
                f for f in files
                if (f.get("link") or "")
                and max(f.get("width", 0), f.get("height", 0)) >= min_quality_px
                and (f.get("quality") or "").lower() not in _bad_quality
            ]
            if not hd_files:
                return []

            def _sort_key(f: dict) -> tuple:
                width = int(f.get("width", 0) or 0)
                height = int(f.get("height", 0) or 0)
                longer = max(width, height)
                quality = (f.get("quality") or "").lower()
                file_type = (f.get("file_type") or "").lower()
                link = (f.get("link") or "").lower()
                return (
                    1 if file_type == "video/mp4" or ".mp4" in link else 0,
                    1 if quality == "hd" else 0,
                    1 if longer <= 1920 else 0,
                    1 if height >= width else 0,
                    -abs(longer - 1280),
                    width * height,
                )

            hd_files.sort(
                key=_sort_key,
                reverse=True,
            )
            links = []
            seen = set()
            for file_info in hd_files:
                link = (file_info.get("link") or "").strip()
                if not link or link in seen:
                    continue
                seen.add(link)
                links.append(link)
            return links

        def _pick_video(ignore_exclude: bool = False) -> Optional[dict]:
            # Pass 1: duration filter + HD
            for vid in videos:
                vid_id = vid.get("id")
                if not ignore_exclude and vid_id in exclude_ids:
                    continue
                duration = vid.get("duration", 0)
                if min_duration <= duration <= max_duration:
                    urls = _ranked_hd_links(vid.get("video_files", []))
                    if urls:
                        logger.info(
                            f"Pexels Video: selected id={vid_id} "
                            f"duration={duration}s quality=HD query='{query}'"
                        )
                        return {"id": vid_id, "url": urls[0], "urls": urls, "duration": duration}

            # Pass 2: relax duration, still require HD
            for vid in videos:
                vid_id = vid.get("id")
                if not ignore_exclude and vid_id in exclude_ids:
                    continue
                urls = _ranked_hd_links(vid.get("video_files", []))
                if urls:
                    logger.info(f"Pexels Video: relaxed-duration id={vid_id} query='{query}'")
                    return {
                        "id": vid_id,
                        "url": urls[0],
                        "urls": urls,
                        "duration": vid.get("duration", 0),
                    }
            return None

        selected = _pick_video(ignore_exclude=False)
        if selected:
            return selected

        if allow_repeat_fallback and exclude_ids:
            logger.info(
                "Pexels Video: no fresh HD videos left after exclude_ids; "
                "falling back to repeats"
            )
            selected = _pick_video(ignore_exclude=True)
            if selected:
                return selected

        logger.warning(f"Pexels Video: no HD results for '{query}'")
        return None


    async def download_stock_photo(self, photo_url: str, filename: str) -> Optional[str]:
        """Download a stock photo and save it locally.

        Sends a proper User-Agent to avoid 403 from Wikimedia and similar CDNs.
        Uses proxy for pexels.com URLs.
        """
        try:
            filepath = os.path.join(self.media_dir, filename)
            headers = {
                "User-Agent": "IzhevskTodayNewsBot/1.0 (https://t.me/IzhevskTodayNews)"
            }

            proxy_url = os.getenv("PEXELS_PROXY", "").strip()
            use_proxy = bool(proxy_url and "pexels.com" in photo_url.lower())

            if use_proxy and SOCKS_AVAILABLE and proxy_url.startswith("socks"):
                connector = ProxyConnector.from_url(proxy_url)
                session_ctx = aiohttp.ClientSession(connector=connector, headers=headers)
            else:
                session_ctx = aiohttp.ClientSession(headers=headers)

            async with session_ctx as session:
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

    async def fetch_telegram_clip(self, channel_names: list[str], exclude_urls: list[str] = None) -> tuple[Optional[str], Optional[str]]:
        """
        Scrapes the Telegram web preview to find the latest MP4 video from the provided channels.
        Returns a tuple: (local_file_path, original_url).
        """
        import random
        import tempfile
        import re
        
        exclude = set(exclude_urls or [])
        channel_name = random.choice(channel_names)
        base_url = f"https://t.me/s/{channel_name}"
        logger.info(f"MediaProcessor: fetching from Telegram web: {base_url}")
        
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.get(base_url, timeout=15) as resp:
                    if resp.status != 200:
                        logger.error(f"TG fetch failed: HTTP {resp.status}")
                        return None, None
                    html = await resp.text()

            # Find all video tags
            matches = re.findall(r'<video[^>]+src="([^"]+\.mp4[^"]*)"', html, flags=re.IGNORECASE)
            
            # Filter out already seen urls
            fresh_matches = [m for m in matches if m not in exclude]
            
            if not fresh_matches:
                logger.warning(f"No unseen MP4 videos found in {channel_name} (found {len(matches)} total)")
                return None, None
            
            recent_vids = fresh_matches[-10:] if len(fresh_matches) > 10 else fresh_matches
            target_url = random.choice(recent_vids)
            
            logger.info(f"TG: downloading MP4 from {target_url[:50]}...")
            
            out = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
            out.close()
            
            async with aiohttp.ClientSession() as sess:
                async with sess.get(target_url, timeout=120) as resp:
                    if resp.status != 200:
                        logger.error(f"TG download failed: HTTP {resp.status}")
                        return None, None
                    with open(out.name, "wb") as f:
                        async for chunk in resp.content.iter_chunked(65536):
                            f.write(chunk)
            
            logger.info(f"TG clip ready -> {out.name}")
            return out.name, target_url

        except Exception as e:
            logger.error(f"TG fetch error: {e}")
            return None, None
