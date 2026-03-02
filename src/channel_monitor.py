"""
Channel Monitor — uses Telethon (bot token) to poll public Telegram channels for new posts.
No user session required — authenticates with bot token.
"""

import asyncio
import logging
import os
from typing import Callable, Optional, List

from telethon import TelegramClient
from telethon.tl.types import (
    MessageMediaPhoto,
    MessageMediaDocument,
    DocumentAttributeVideo,
)

from src.config import Config
from src.database import Database

logger = logging.getLogger(__name__)


class ChannelMonitor:
    """Monitors public Telegram channels for new posts using Telethon bot token + polling."""

    def __init__(self, config: Config, db: Database):
        self.config = config
        self.db = db
        self.client: Optional[TelegramClient] = None
        self._on_new_post_callback: Optional[Callable] = None
        self._polling_task: Optional[asyncio.Task] = None
        self._running = False

    async def start(self):
        """Initialize Telethon client with bot token and start polling."""
        session_path = os.path.join("data", "bot_session")
        os.makedirs("data", exist_ok=True)

        self.client = TelegramClient(
            session_path,
            self.config.api_id,
            self.config.api_hash,
        )

        # Authenticate with bot token — no phone/code needed!
        await self.client.start(bot_token=self.config.bot_token)
        me = await self.client.get_me()
        logger.info(f"Telethon bot client started: {me.first_name} (ID: {me.id})")

        # Register source channels in the DB
        for channel in self.config.source_channels:
            await self.db.add_source(channel)
            logger.info(f"Registered source channel: @{channel}")

        # Start polling loop
        self._running = True
        self._polling_task = asyncio.create_task(self._poll_channels())
        logger.info(f"Polling {len(self.config.source_channels)} channels every {self.config.check_interval}s")

    async def stop(self):
        """Stop polling and disconnect Telethon client."""
        self._running = False
        if self._polling_task:
            self._polling_task.cancel()
            try:
                await self._polling_task
            except asyncio.CancelledError:
                pass
        if self.client:
            await self.client.disconnect()
            logger.info("Telethon client disconnected")

    def on_new_post(self, callback: Callable):
        """Register a callback for when a new post is found and saved to the DB."""
        self._on_new_post_callback = callback

    async def _poll_channels(self):
        """Continuously poll all source channels for new messages."""
        logger.info("Channel polling loop started")

        # Initial catch-up: fetch recent posts
        for channel in self.config.source_channels:
            try:
                await self._check_channel(channel, limit=5)
            except Exception as e:
                logger.error(f"Initial fetch failed for @{channel}: {e}")

        # Main polling loop
        while self._running:
            try:
                await asyncio.sleep(self.config.check_interval)

                for channel in self.config.source_channels:
                    try:
                        await self._check_channel(channel, limit=10)
                    except Exception as e:
                        logger.error(f"Poll check failed for @{channel}: {e}")

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Polling loop error: {e}", exc_info=True)
                await asyncio.sleep(30)  # Wait before retrying

        logger.info("Channel polling loop stopped")

    async def _check_channel(self, channel_username: str, limit: int = 10):
        """Check a channel for new messages since last processed ID."""
        if not self.client:
            return

        last_id = await self.db.get_last_message_id(channel_username)
        new_count = 0

        try:
            entity = await self.client.get_entity(channel_username)
            messages = []

            async for message in self.client.iter_messages(
                entity, limit=limit, min_id=last_id
            ):
                messages.append(message)

            # Process in chronological order (oldest first)
            for message in reversed(messages):
                if not message.text or len(message.text) < self.config.min_text_length:
                    continue
                if message.forward:
                    continue

                # Determine media type
                media_type = "none"
                media_local_path = None

                if message.media:
                    if isinstance(message.media, MessageMediaPhoto):
                        media_type = "photo"
                    elif isinstance(message.media, MessageMediaDocument):
                        doc = message.media.document
                        if doc and doc.attributes:
                            for attr in doc.attributes:
                                if isinstance(attr, DocumentAttributeVideo):
                                    media_type = "video"
                                    break
                            if media_type == "none":
                                media_type = "document"

                    # Download media
                    if media_type in ("photo", "video"):
                        try:
                            os.makedirs(self.config.media_dir, exist_ok=True)
                            media_local_path = await message.download_media(
                                file=self.config.media_dir
                            )
                            logger.info(f"Downloaded media: {media_local_path}")
                        except Exception as e:
                            logger.error(f"Failed to download media: {e}")

                # Save post to database
                post_id = await self.db.add_post(
                    source_channel=channel_username,
                    source_message_id=message.id,
                    original_text=message.text,
                    media_type=media_type,
                    media_local_path=media_local_path,
                )

                if post_id:
                    await self.db.update_last_message_id(channel_username, message.id)
                    new_count += 1

                    # Trigger callback
                    if self._on_new_post_callback:
                        await self._on_new_post_callback(post_id)

                    logger.info(f"Post #{post_id} from @{channel_username}: {message.text[:80]}...")

            if new_count > 0:
                logger.info(f"Fetched {new_count} new posts from @{channel_username}")

        except Exception as e:
            logger.error(f"Failed to check @{channel_username}: {e}")
