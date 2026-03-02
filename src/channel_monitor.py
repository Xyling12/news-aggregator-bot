"""
Channel Monitor — scrapes public Telegram channel web previews (t.me/s/channel).
No Telethon, no API keys, no user session needed.
"""

import asyncio
import logging
import os
import re
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
            async with self._session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
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

            # Download photo if available
            if media_type == "photo" and media_url:
                try:
                    media_local_path = await self._download_media(
                        media_url, channel_username, msg_id
                    )
                except Exception as e:
                    logger.error(f"Media download failed: {e}")

            # Save to database (store remote URL in media_file_id for fallback)
            post_id = await self.db.add_post(
                source_channel=channel_username,
                source_message_id=msg_id,
                original_text=text,
                media_type=media_type,
                media_file_id=media_url,  # Store remote URL for fallback
                media_local_path=media_local_path,
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

            # Detect media
            media_type = "none"
            media_url = None

            # Check for photos
            photo_match = re.search(
                r'background-image:\s*url\([\'"]?(https://[^\'"\)]+)[\'"]?\)',
                block
            )
            if photo_match:
                media_type = "photo"
                media_url = photo_match.group(1)

            # Check for videos
            if '<video' in block or 'tgme_widget_message_video' in block:
                media_type = "video"

            posts.append({
                "message_id": msg_id,
                "text": text,
                "media_type": media_type,
                "media_url": media_url,
            })

        return posts

    async def _download_media(
        self, url: str, channel: str, msg_id: int
    ) -> Optional[str]:
        """Download a photo from URL."""
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

        try:
            async with self._session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status == 200:
                    with open(filepath, "wb") as f:
                        f.write(await resp.read())
                    logger.info(f"Downloaded media: {filepath}")
                    return filepath
        except Exception as e:
            logger.error(f"Media download error: {e}")

        return None
