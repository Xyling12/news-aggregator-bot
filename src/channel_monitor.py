"""
Channel Monitor — uses Telethon to listen for new messages in public Telegram channels.
"""

import asyncio
import logging
import os
from typing import Callable, Optional, List

from telethon import TelegramClient, events
from telethon.tl.types import (
    MessageMediaPhoto,
    MessageMediaDocument,
    DocumentAttributeVideo,
)

from src.config import Config
from src.database import Database

logger = logging.getLogger(__name__)


class ChannelMonitor:
    """Monitors public Telegram channels for new posts using Telethon."""

    def __init__(self, config: Config, db: Database):
        self.config = config
        self.db = db
        self.client: Optional[TelegramClient] = None
        self._on_new_post_callback: Optional[Callable] = None

    async def start(self):
        """Initialize Telethon client and start monitoring."""
        session_path = os.path.join("data", self.config.session_name)
        self.client = TelegramClient(
            session_path,
            self.config.api_id,
            self.config.api_hash,
        )

        await self.client.start()
        logger.info("Telethon client started successfully")

        # Register source channels in the DB
        for channel in self.config.source_channels:
            await self.db.add_source(channel)
            logger.info(f"Registered source channel: @{channel}")

        # Set up event handler for new messages
        sources = self.config.source_channels
        if sources:
            @self.client.on(events.NewMessage(chats=sources))
            async def handler(event):
                await self._handle_new_message(event)

            logger.info(f"Listening to {len(sources)} channels: {', '.join(sources)}")

    async def stop(self):
        """Disconnect Telethon client."""
        if self.client:
            await self.client.disconnect()
            logger.info("Telethon client disconnected")

    def on_new_post(self, callback: Callable):
        """Register a callback for when a new post is found and saved to the DB."""
        self._on_new_post_callback = callback

    async def _handle_new_message(self, event):
        """Process a new message from a source channel."""
        message = event.message

        # Skip messages without text or too short
        if not message.text or len(message.text) < self.config.min_text_length:
            logger.debug(f"Skipping short/empty message {message.id} from {event.chat.username}")
            return

        # Skip forwarded messages (they are reposts, not original content)
        if message.forward:
            logger.debug(f"Skipping forwarded message {message.id}")
            return

        channel_username = event.chat.username or str(event.chat_id)
        logger.info(f"New post from @{channel_username}: {message.text[:80]}...")

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
            # Update last processed message ID
            await self.db.update_last_message_id(channel_username, message.id)

            # Trigger callback
            if self._on_new_post_callback:
                await self._on_new_post_callback(post_id)

            logger.info(f"Post #{post_id} saved to queue")
        else:
            logger.debug(f"Duplicate post skipped: {channel_username}/{message.id}")

    async def fetch_recent_posts(self, channel_username: str, limit: int = 5) -> List[dict]:
        """Fetch recent posts from a channel (for initial setup / catch-up)."""
        if not self.client:
            return []

        last_id = await self.db.get_last_message_id(channel_username)
        posts = []

        try:
            entity = await self.client.get_entity(channel_username)
            async for message in self.client.iter_messages(
                entity, limit=limit, min_id=last_id
            ):
                if not message.text or len(message.text) < self.config.min_text_length:
                    continue
                if message.forward:
                    continue

                media_type = "none"
                media_local_path = None

                if isinstance(getattr(message, "media", None), MessageMediaPhoto):
                    media_type = "photo"
                    try:
                        os.makedirs(self.config.media_dir, exist_ok=True)
                        media_local_path = await message.download_media(
                            file=self.config.media_dir
                        )
                    except Exception as e:
                        logger.error(f"Failed to download media: {e}")

                post_id = await self.db.add_post(
                    source_channel=channel_username,
                    source_message_id=message.id,
                    original_text=message.text,
                    media_type=media_type,
                    media_local_path=media_local_path,
                )

                if post_id:
                    await self.db.update_last_message_id(channel_username, message.id)
                    posts.append({"id": post_id, "text": message.text[:100]})

                    if self._on_new_post_callback:
                        await self._on_new_post_callback(post_id)

        except Exception as e:
            logger.error(f"Failed to fetch from @{channel_username}: {e}")

        return posts
