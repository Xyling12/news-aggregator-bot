"""
VK Publisher — crossposting to VK community wall.
Uses VK API v5.199 to publish posts with photos.
"""

import asyncio
import logging
import os
import re
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

VK_API_VERSION = "5.199"
VK_API_BASE = "https://api.vk.com/method"


class VKPublisher:
    """Publishes posts to a VK community wall."""

    def __init__(self, access_token: str, group_id: str):
        """
        Args:
            access_token: VK community access token
            group_id: Numeric group ID (without minus sign)
        """
        self.access_token = access_token
        self.group_id = str(group_id).lstrip("-")
        self._enabled = bool(access_token and group_id)

        if self._enabled:
            logger.info(f"VK Publisher initialized for group {self.group_id}")
        else:
            logger.warning("VK Publisher disabled: missing token or group_id")

    @property
    def enabled(self) -> bool:
        return self._enabled

    def _clean_html_for_vk(self, text: str) -> str:
        """Convert HTML formatting to VK-compatible plain text and swap footer."""
        # Replace <b>text</b> with text in caps or just text
        text = re.sub(r'<b>(.*?)</b>', r'\1', text)
        text = re.sub(r'<i>(.*?)</i>', r'\1', text)
        text = re.sub(r'<a href="(.*?)">(.*?)</a>', r'\2 (\1)', text)
        # Remove any remaining HTML tags
        text = re.sub(r'<[^>]+>', '', text)
        # Decode HTML entities
        text = text.replace('&amp;', '&')
        text = text.replace('&lt;', '<')
        text = text.replace('&gt;', '>')
        text = text.replace('&quot;', '"')

        # Remove Telegram-specific footer and replace with VK footer
        text = re.sub(r'\n*📲\s*@\w+\s*\|\s*📩\s*@\w+\s*$', '', text.strip())
        text = text.strip()
        text += f"\n\n📲 Подписывайтесь: vk.com/club{self.group_id}"

        return text

    async def _upload_photo_from_url(self, photo_url: str) -> Optional[str]:
        """Download photo from URL and upload to VK. Returns VK attachment string."""
        if not photo_url:
            return None

        try:
            async with aiohttp.ClientSession() as session:
                # Step 1: Get upload URL from VK
                params = {
                    "access_token": self.access_token,
                    "v": VK_API_VERSION,
                    "group_id": self.group_id,
                }
                async with session.get(
                    f"{VK_API_BASE}/photos.getWallUploadServer",
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    data = await resp.json()
                    if "error" in data:
                        logger.error(f"VK getWallUploadServer error: {data['error']}")
                        return None
                    upload_url = data["response"]["upload_url"]

                # Step 2: Download the photo (follow redirects, detect content type)
                async with session.get(
                    photo_url,
                    timeout=aiohttp.ClientTimeout(total=30),
                    allow_redirects=True,
                ) as resp:
                    if resp.status != 200:
                        logger.error(f"Failed to download photo: {resp.status} {photo_url[:80]}")
                        return None
                    photo_data = await resp.read()
                    content_type = resp.content_type or "image/jpeg"
                    # Determine file extension from content-type
                    ext_map = {
                        "image/jpeg": "jpg",
                        "image/jpg": "jpg",
                        "image/png": "png",
                        "image/webp": "jpg",  # VK doesn't handle WebP well, rename to jpg
                        "image/gif": "gif",
                    }
                    ext = ext_map.get(content_type.split(";")[0].strip(), "jpg")
                    # Convert WebP data to just let VK handle it (most modern VK handles it fine)
                    upload_content_type = "image/jpeg" if "webp" in content_type else content_type.split(";")[0].strip()

                # Step 3: Upload to VK
                form = aiohttp.FormData()
                form.add_field("photo", photo_data, filename=f"photo.{ext}", content_type=upload_content_type)
                async with session.post(upload_url, data=form, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    upload_result = await resp.json()
                    if not upload_result.get("photo") or upload_result["photo"] == "[]":
                        logger.error("VK photo upload returned empty")
                        return None

                # Step 4: Save photo on VK wall
                save_params = {
                    "access_token": self.access_token,
                    "v": VK_API_VERSION,
                    "group_id": self.group_id,
                    "photo": upload_result["photo"],
                    "server": upload_result["server"],
                    "hash": upload_result["hash"],
                }
                async with session.get(
                    f"{VK_API_BASE}/photos.saveWallPhoto",
                    params=save_params,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    save_data = await resp.json()
                    if "error" in save_data:
                        logger.error(f"VK saveWallPhoto error: {save_data['error']}")
                        return None
                    photo = save_data["response"][0]
                    return f"photo{photo['owner_id']}_{photo['id']}"

        except Exception as e:
            logger.error(f"VK photo upload failed: {e}")
            return None

    async def publish(
        self,
        text: str,
        photo_url: Optional[str] = None,
        local_photo_path: Optional[str] = None,
    ) -> Optional[int]:
        """
        Publish a post to VK community wall.
        Returns the post ID on success, None on failure.
        """
        if not self._enabled:
            return None

        # Clean text for VK (remove HTML)
        vk_text = self._clean_html_for_vk(text)

        # Upload photo if available
        attachment = None
        source_url = photo_url or local_photo_path
        if photo_url:
            attachment = await self._upload_photo_from_url(photo_url)

        try:
            async with aiohttp.ClientSession() as session:
                params = {
                    "access_token": self.access_token,
                    "v": VK_API_VERSION,
                    "owner_id": f"-{self.group_id}",
                    "from_group": 1,
                    "message": vk_text[:16000],  # VK wall limit
                }
                if attachment:
                    params["attachments"] = attachment

                async with session.get(
                    f"{VK_API_BASE}/wall.post",
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    data = await resp.json()
                    if "error" in data:
                        logger.error(f"VK wall.post error: {data['error']}")
                        return None

                    post_id = data["response"]["post_id"]
                    logger.info(f"✅ VK post published: {post_id}")
                    return post_id

        except Exception as e:
            logger.error(f"VK publish failed: {e}")
            return None
