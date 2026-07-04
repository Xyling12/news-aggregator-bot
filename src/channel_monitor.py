"""
Channel Monitor — scrapes public Telegram channel web previews (t.me/s/channel).
No Telethon, no API keys, no user session needed.
"""

import asyncio
import json
import logging
import os
import re
from datetime import datetime
from typing import Callable, Optional, List
from html import unescape

import aiohttp

from src.config import Config
from src.database import Database

logger = logging.getLogger(__name__)


class ChannelMonitor:
    """Monitors public Telegram channels by scraping t.me/s/ web previews."""

    def __init__(self, config: Config, db: Database):
        self.config = config
        self.db = db
        self._on_new_post_callback: Optional[Callable] = None
        self._polling_task: Optional[asyncio.Task] = None
        self._running = False
        self._session: Optional[aiohttp.ClientSession] = None
        # t.me is blocked/slow from the RU server → route scraping via TELEGRAM_PROXY
        self._proxy = os.getenv("TELEGRAM_PROXY", "").strip() or None

    async def start(self):
        """Start the web scraping polling loop."""
        self._session = aiohttp.ClientSession(
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        )
        self._running = True

        # Register source channels in the DB
        for channel in self.config.source_channels:
            await self.db.add_source(channel)
            logger.info(f"Registered source: @{channel}")

        # Start polling
        self._polling_task = asyncio.create_task(self._poll_channels())
        logger.info(
            f"Web scraping monitor started: {len(self.config.source_channels)} channels, "
            f"interval: {self.config.check_interval}s"
        )

    async def stop(self):
        """Stop polling and close HTTP session."""
        self._running = False
        if self._polling_task:
            self._polling_task.cancel()
            try:
                await self._polling_task
            except asyncio.CancelledError:
                pass
        if self._session:
            await self._session.close()
            logger.info("HTTP session closed")

    def on_new_post(self, callback: Callable):
        """Register a callback for new posts."""
        self._on_new_post_callback = callback

    async def _poll_channels(self):
        """Poll all source channels for new messages."""
        logger.info("Channel polling loop started")

        # Initial catch-up
        for channel in self.config.source_channels:
            try:
                await self._check_channel(channel)
            except Exception as e:
                logger.error(f"Initial fetch failed for @{channel}: {e}")
            await asyncio.sleep(2)  # Be polite between channels

        # Main loop
        while self._running:
            try:
                await asyncio.sleep(self.config.check_interval)
                for channel in self.config.source_channels:
                    try:
                        await self._check_channel(channel)
                    except Exception as e:
                        logger.error(f"Poll failed for @{channel}: {e}")
                    await asyncio.sleep(2)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Polling loop error: {e}", exc_info=True)
                await asyncio.sleep(30)

        logger.info("Channel polling loop stopped")

    async def _check_channel(self, channel_username: str):
        """Fetch and parse the latest posts from a channel's web preview."""
        if not self._session:
            return

        last_id = await self.db.get_last_message_id(channel_username)
        url = f"https://t.me/s/{channel_username}"

        try:
            async with self._session.get(url, timeout=aiohttp.ClientTimeout(total=30), proxy=self._proxy) as resp:
                if resp.status != 200:
                    logger.warning(f"HTTP {resp.status} for @{channel_username}")
                    return
                html = await resp.text()
        except Exception as e:
            logger.error(f"HTTP error for @{channel_username}: {e}")
            return

        # Parse posts from HTML
        posts = self._parse_posts(html, channel_username)
        new_count = 0

        for post in posts:
            msg_id = post["message_id"]
            text = post["text"]

            # Skip already processed
            if msg_id <= last_id:
                continue

            # Skip short texts
            if len(text) < self.config.min_text_length:
                continue

            # Determine media type from HTML
            media_type = post.get("media_type", "none")
            media_url = post.get("media_url")
            media_local_path = None

            # For video posts, try to grab the actual MP4 so our repost has the
            # real clip (not a stock photo). Falls back to the preview frame.
            video_url = post.get("video_url")
            if media_type == "video" and video_url:
                try:
                    media_local_path = await self._download_video(
                        video_url, channel_username, msg_id
                    )
                except Exception as e:
                    logger.error(f"Video download failed: {e}")

            # Download primary photo / video preview frame (when no MP4 was grabbed)
            if media_local_path is None and media_url and media_type in ("photo", "video"):
                try:
                    media_local_path = await self._download_media(
                        media_url, channel_username, msg_id
                    )
                except Exception as e:
                    logger.error(f"Media download failed: {e}")

                # Reject tiny thumbnails (e.g. 90x67 court previews) — they look awful
                # published full-size. Drop the image but keep the text.
                if media_local_path and self._is_image_too_small(media_local_path):
                    logger.info(
                        f"@{channel_username}: msg {msg_id} — source image too small, "
                        f"dropping it (stock/local image will be used instead)"
                    )
                    media_local_path = None
                    if media_type == "photo":
                        media_type = "none"

            # Download extra photos from album posts
            extra_media_urls = post.get("extra_media_urls", [])
            extra_local_paths = []
            for idx, extra_url in enumerate(extra_media_urls[:9]):  # max 10 total
                try:
                    extra_path = await self._download_media(
                        extra_url, channel_username, f"{msg_id}_x{idx+1}"
                    )
                    if extra_path:
                        extra_local_paths.append(extra_path)
                except Exception as e:
                    logger.error(f"Extra media download failed: {e}")
            media_extra_paths_json = json.dumps(extra_local_paths, ensure_ascii=False) if extra_local_paths else None

            # Skip weather forecast posts during morning (6-11 AM) — we generate our own at 7:00
            if self._is_weather_report(text) and 6 <= datetime.now().hour <= 11:
                logger.info(f"@{channel_username}: skipping scraped weather report (scheduler handles this)")
                await self.db.update_last_message_id(channel_username, msg_id)
                continue

            # Skip entertainment/greetings — we generate our own morning/evening/fun rubrics
            if self._is_entertainment_post(text):
                logger.info(f"@{channel_username}: skipping entertainment/greeting post")
                await self.db.update_last_message_id(channel_username, msg_id)
                continue

            # Save to database (store remote URL in media_file_id for fallback)
            post_id = await self.db.add_post(
                source_channel=channel_username,
                source_message_id=msg_id,
                original_text=text,
                media_type=media_type,
                media_file_id=media_url,  # Store remote URL for fallback
                media_local_path=media_local_path,
                media_extra_paths=media_extra_paths_json,
            )

            if post_id:
                await self.db.update_last_message_id(channel_username, msg_id)
                new_count += 1

                if self._on_new_post_callback:
                    await self._on_new_post_callback(post_id)
                    await asyncio.sleep(3)  # Delay between posts to avoid AI rate limits

                logger.info(f"Post #{post_id} from @{channel_username}: {text[:80]}...")

        if new_count > 0:
            logger.info(f"Fetched {new_count} new posts from @{channel_username}")

    def _parse_posts(self, html: str, channel_username: str) -> List[dict]:
        """Parse post data from t.me/s/ HTML page."""
        posts = []

        # Find all message blocks: <div class="tgme_widget_message_wrap ...">
        # Each message has data-post="channel/messageId"
        msg_pattern = re.compile(
            r'<div[^>]*class="tgme_widget_message_wrap[^"]*"[^>]*>'
            r'.*?<div[^>]*class="tgme_widget_message[^"]*"[^>]*data-post="([^"]+)"',
            re.DOTALL
        )

        # Split HTML into message blocks
        blocks = re.split(
            r'(?=<div[^>]*class="tgme_widget_message_wrap)',
            html
        )

        for block in blocks:
            # Extract data-post attribute
            post_match = re.search(r'data-post="([^"]+)"', block)
            if not post_match:
                continue

            data_post = post_match.group(1)  # e.g., "rian_ru/334180"
            parts = data_post.split("/")
            if len(parts) != 2:
                continue

            try:
                msg_id = int(parts[1])
            except ValueError:
                continue

            # Skip forwarded messages
            if 'tgme_widget_message_forwarded_from' in block:
                continue

            # Extract text content
            text_match = re.search(
                r'<div[^>]*class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>',
                block,
                re.DOTALL
            )
            if not text_match:
                continue

            raw_text = text_match.group(1)
            # Clean HTML tags but preserve line breaks
            text = re.sub(r'<br\s*/?>', '\n', raw_text)
            text = re.sub(r'<[^>]+>', '', text)
            text = unescape(text).strip()

            if not text:
                continue

            # Detect media — collect ALL photo URLs for album posts
            media_type = "none"
            all_photo_urls: List[str] = []

            # Pattern 1: background-image: url(...) — covers both single and album posts
            bg_matches = re.findall(
                r'background-image:\s*url\([\'"]?(https://[^\'"\)]+)[\'"]?\)',
                block
            )
            all_photo_urls.extend(bg_matches)

            # Pattern 2: data-media-url="..."
            if not all_photo_urls:
                data_media_matches = re.findall(
                    r'data-media-url=[\'"]?(https://[^\'"\s>]+)',
                    block
                )
                all_photo_urls.extend(data_media_matches)

            # Pattern 3: <img src="..."> inside cdn.telegram.org (album posts)
            if not all_photo_urls:
                img_matches = re.findall(
                    r'<img[^>]+src=[\'"]?(https://cdn(?:\d+)?\.telegram\.org/[^\'"\s>]+)',
                    block,
                    re.IGNORECASE,
                )
                all_photo_urls.extend(img_matches)

            # Deduplicate while preserving order
            seen: set = set()
            unique_urls: List[str] = []
            for u in all_photo_urls:
                if u not in seen:
                    seen.add(u)
                    unique_urls.append(u)

            if unique_urls:
                media_type = "photo"

            media_url = unique_urls[0] if unique_urls else None
            extra_media_urls = unique_urls[1:] if len(unique_urls) > 1 else []

            # Check for videos — also grab the direct MP4 URL when t.me exposes it
            video_url = None
            if '<video' in block or 'tgme_widget_message_video' in block:
                media_type = "video"
                mp4_matches = re.findall(
                    r'<video[^>]+src="([^"]+\.mp4[^"]*)"', block, flags=re.IGNORECASE
                )
                if mp4_matches:
                    video_url = unescape(mp4_matches[0])

            posts.append({
                "message_id": msg_id,
                "text": text,
                "media_type": media_type,
                "media_url": media_url,
                "extra_media_urls": extra_media_urls,
                "video_url": video_url,
            })

        return posts

    async def _download_video(
        self, url: str, channel: str, msg_id: int, max_mb: int = 50
    ) -> Optional[str]:
        """Download a source MP4 (size-capped). Returns local path or None."""
        if not self._session:
            return None
        os.makedirs(self.config.media_dir, exist_ok=True)
        filepath = os.path.join(self.config.media_dir, f"{channel}_{msg_id}.mp4")
        max_bytes = max_mb * 1024 * 1024
        try:
            async with self._session.get(
                url, timeout=aiohttp.ClientTimeout(total=120), proxy=self._proxy
            ) as resp:
                if resp.status != 200:
                    logger.warning(f"Video download HTTP {resp.status}: {url[:80]}")
                    return None
                size = 0
                with open(filepath, "wb") as f:
                    async for chunk in resp.content.iter_chunked(65536):
                        size += len(chunk)
                        if size > max_bytes:
                            logger.info(f"Video too large (>{max_mb}MB), skipping MP4: {url[:80]}")
                            f.close()
                            os.remove(filepath)
                            return None
                        f.write(chunk)
            if size < 10000:  # too small to be a real clip
                os.remove(filepath)
                return None
            logger.info(f"Downloaded source video: {filepath} ({size // 1024} KB)")
            return filepath
        except Exception as e:
            logger.error(f"Video download error: {e}")
            try:
                if os.path.exists(filepath):
                    os.remove(filepath)
            except Exception:
                pass
            return None

    async def _download_media(
        self, url: str, channel: str, msg_id: int
    ) -> Optional[str]:
        """Download a photo from URL with retry."""
        if not self._session:
            return None

        os.makedirs(self.config.media_dir, exist_ok=True)

        # Determine extension from URL
        ext = ".jpg"
        if ".png" in url:
            ext = ".png"
        elif ".webp" in url:
            ext = ".webp"

        filename = f"{channel}_{msg_id}{ext}"
        filepath = os.path.join(self.config.media_dir, filename)

        for attempt in range(2):
            try:
                async with self._session.get(url, timeout=aiohttp.ClientTimeout(total=30), proxy=self._proxy) as resp:
                    if resp.status == 200:
                        data = await resp.read()
                        if len(data) > 1000:  # Sanity check: skip tiny/empty responses
                            with open(filepath, "wb") as f:
                                f.write(data)
                            logger.info(f"Downloaded media: {filepath} ({len(data)} bytes)")
                            return filepath
                        else:
                            logger.warning(f"Media too small ({len(data)} bytes), skipping: {url}")
                    else:
                        logger.warning(f"Media download HTTP {resp.status} (attempt {attempt+1}): {url}")
            except Exception as e:
                logger.error(f"Media download error (attempt {attempt+1}): {e}")

            if attempt == 0:
                await asyncio.sleep(2)  # Wait before retry

        return None

    # Minimum acceptable resolution for a source photo (long side, px).
    # Junk preview thumbnails are ~90px; real t.me previews are 250-400px and are
    # fine to publish. Keep the bar low so we don't drop good photos → ugly cards.
    _MIN_PHOTO_PX = 200

    @classmethod
    def _is_image_too_small(cls, path: str) -> bool:
        """Return True if the downloaded image is a tiny thumbnail (e.g. 90x67) or
        an almost fully black/corrupt frame (a "чёрный квадрат")."""
        try:
            from PIL import Image, ImageStat
            with Image.open(path) as img:
                w, h = img.size
                if max(w, h) < cls._MIN_PHOTO_PX:
                    return True
                # Near-black / blank frame → drop (use a card instead)
                stat = ImageStat.Stat(img.convert("L"))
                if stat.mean[0] < 18:  # 0=black, 255=white
                    logger.info(f"Image {path} is near-black (mean={stat.mean[0]:.0f}) — dropping")
                    return True
            return False
        except Exception as e:
            logger.debug(f"Could not measure image {path}: {e}")
            return False  # On error, don't discard — better a photo than none

    @staticmethod
    def _is_weather_report(text: str) -> bool:
        """Return True if text looks like a weather forecast/report.

        Requires a temperature reading (digit + ° sign OR 'градус') AND at least
        2 weather-specific words so incidental mentions do not get caught.
        """
        lower = text.lower()
        has_temperature = bool(
            re.search(r'-?\d+\s*°', text) or 'градус' in lower
        )
        weather_words = [
            'температур', 'ветер', 'влажн', 'давлен',
            'облачн', 'снегопад', 'мороз', 'погода',
            'прогноз', 'осадк', 'ясно', 'метель',
        ]
        hits = sum(1 for w in weather_words if w in lower)
        return has_temperature and hits >= 2

    @staticmethod
    def _is_entertainment_post(text: str) -> bool:
        """Check if post is a generic greeting or game (we generate our own)."""
        lower = text.lower()
        phrases = [
            "доброе утро", "доброй ночи", "спокойной ночи",
            "хорошего вечера", "прекрасного вечера", "отличных выходных",
            "играем в города", "играем в слова", "играем ночами",
            "поиграем в", "интерактив:", "вечерний интерактив",
            "какой сегодня праздник", "гороскоп на",
            "найди отличия", "загадка:"
        ]
        return any(p in lower for p in phrases)
