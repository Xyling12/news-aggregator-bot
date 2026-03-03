"""
MAX Publisher — crossposting to MAX messenger channels.
Uses MAX Platform API (platform-api.max.ru) to publish posts with photos.
Docs: https://dev.max.ru/docs-api
"""

import asyncio
import logging
import re
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

MAX_API_BASE = "https://platform-api.max.ru"


class MAXPublisher:
    """Publishes posts to a MAX messenger channel."""

    def __init__(self, bot_token: str, chat_id: str):
        """
        Args:
            bot_token: MAX bot access token (from business.max.ru)
            chat_id: Target channel/chat ID in MAX
        """
        self.bot_token = bot_token
        self.chat_id = str(chat_id) if chat_id else ""
        self._enabled = bool(bot_token and chat_id)

        if self._enabled:
            logger.info(f"MAX Publisher initialized for chat {self.chat_id}")
        else:
            logger.warning("MAX Publisher disabled: missing token or chat_id")

    @property
    def enabled(self) -> bool:
        return self._enabled

    def _headers(self) -> dict:
        """Auth headers for MAX API."""
        return {"Authorization": self.bot_token}

    def _clean_html_for_max(self, text: str) -> str:
        """Convert HTML formatting to MAX-compatible HTML and swap footer."""
        # MAX supports HTML with format="html", so keep <b>, <i>, <a> tags
        # Just remove Telegram-specific footer and replace with MAX footer
        text = re.sub(r'\n*📲\s*@\w+\s*\|\s*📩\s*@\w+\s*$', '', text.strip())
        text = text.strip()
        text += "\n\n📲 Читайте нас в MAX!"
        return text

    async def _upload_photo_from_url(self, photo_url: str) -> Optional[str]:
        """Download photo from URL and upload to MAX. Returns attachment token."""
        if not photo_url:
            return None

        try:
            async with aiohttp.ClientSession() as session:
                # Step 1: Get upload URL from MAX
                async with session.post(
                    f"{MAX_API_BASE}/uploads",
                    params={"type": "image"},
                    headers=self._headers(),
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status != 200:
                        logger.error(f"MAX get upload URL failed: {resp.status}")
                        return None
                    data = await resp.json()
                    upload_url = data.get("url")
                    if not upload_url:
                        logger.error(f"MAX upload URL missing in response: {data}")
                        return None

                # Step 2: Download the photo
                async with session.get(
                    photo_url,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status != 200:
                        logger.error(f"Failed to download photo: {resp.status}")
                        return None
                    photo_data = await resp.read()

                # Step 3: Upload to MAX
                form = aiohttp.FormData()
                form.add_field(
                    "data", photo_data,
                    filename="photo.jpg",
                    content_type="image/jpeg",
                )
                async with session.post(
                    upload_url,
                    data=form,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status != 200:
                        logger.error(f"MAX photo upload failed: {resp.status}")
                        return None
                    upload_result = await resp.json()
                    token = upload_result.get("token")
                    if not token:
                        # For images, the response may contain photos array
                        photos = upload_result.get("photos")
                        if photos and isinstance(photos, dict):
                            # Get first photo token
                            for key, val in photos.items():
                                if isinstance(val, dict) and "token" in val:
                                    token = val["token"]
                                    break
                    if not token:
                        logger.error(f"MAX upload token missing: {upload_result}")
                        return None

                    # Wait a bit for file processing
                    await asyncio.sleep(1)
                    return token

        except Exception as e:
            logger.error(f"MAX photo upload failed: {e}")
            return None

    async def publish(
        self,
        text: str,
        photo_url: Optional[str] = None,
    ) -> Optional[str]:
        """
        Publish a post to a MAX channel.
        Returns the message ID on success, None on failure.
        """
        if not self._enabled:
            return None

        # Clean text for MAX
        max_text = self._clean_html_for_max(text)

        # Build message body
        body = {
            "text": max_text[:4000],  # MAX limit
            "format": "html",
            "notify": True,
        }

        # Upload photo if available
        if photo_url:
            token = await self._upload_photo_from_url(photo_url)
            if token:
                body["attachments"] = [
                    {"type": "image", "payload": {"token": token}}
                ]

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{MAX_API_BASE}/messages",
                    params={"chat_id": self.chat_id},
                    headers={
                        **self._headers(),
                        "Content-Type": "application/json",
                    },
                    json=body,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    data = await resp.json()

                    if resp.status != 200:
                        logger.error(f"MAX send message error ({resp.status}): {data}")
                        # Retry once without attachment if it's an attachment error
                        if (
                            photo_url
                            and data.get("code") == "attachment.not.ready"
                        ):
                            logger.info("MAX: retrying without photo...")
                            body.pop("attachments", None)
                            async with session.post(
                                f"{MAX_API_BASE}/messages",
                                params={"chat_id": self.chat_id},
                                headers={
                                    **self._headers(),
                                    "Content-Type": "application/json",
                                },
                                json=body,
                                timeout=aiohttp.ClientTimeout(total=15),
                            ) as retry_resp:
                                retry_data = await retry_resp.json()
                                if retry_resp.status == 200:
                                    msg = retry_data.get("message", {})
                                    mid = msg.get("body", {}).get("mid")
                                    logger.info(f"✅ MAX post published (text only): {mid}")
                                    return mid
                        return None

                    msg = data.get("message", {})
                    mid = msg.get("body", {}).get("mid")
                    logger.info(f"✅ MAX post published: {mid}")
                    return mid

        except Exception as e:
            logger.error(f"MAX publish failed: {e}")
            return None
