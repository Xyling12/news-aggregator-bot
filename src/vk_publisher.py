"""
VK Publisher — cross-posts news to VKontakte community wall.
Uses VK API to publish text and photo posts.
"""

import asyncio
import logging
import re
import os
import tempfile
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)


class VKPublisher:
    """Publishes posts to a VKontakte community (group/public page)."""

    API_VERSION = "5.199"
    API_BASE = "https://api.vk.com/method"

    def __init__(self, access_token: str, group_id: str):
        self.access_token = access_token
        self.group_id = group_id.replace("club", "")  # Strip "club" prefix if present
        self._session: Optional[aiohttp.ClientSession] = None

    @property
    def enabled(self) -> bool:
        """True if VK crossposting is configured."""
        return bool(self.access_token and self.group_id)

    async def _get_session(self) -> aiohttp.ClientSession:
        if not self._session or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    def _html_to_vk(self, html_text: str) -> str:
        """Convert HTML-formatted post to VK-compatible plain text.
        
        VK wall posts don't support HTML, so we convert:
        - <b>text</b> → text (VK doesn't support bold in wall posts)
        - <a href="url">text</a> → text (url)
        - <br> → newline
        - Strip all other tags
        """
        text = html_text

        # Convert <a> links to text (url) format
        text = re.sub(r'<a[^>]*href="([^"]*)"[^>]*>(.*?)</a>', r'\2 (\1)', text)

        # Remove <b> tags but keep content
        text = re.sub(r'</?b>', '', text)
        text = re.sub(r'</?i>', '', text)

        # Convert <br> to newlines
        text = re.sub(r'<br\s*/?>', '\n', text)

        # Remove any remaining HTML tags
        text = re.sub(r'<[^>]+>', '', text)

        # Strip the CTA footer block VK doesn't need — already shown as channel name
        # Matches lines like "😊 Подписаться в TG ...", "📱 Подписывайтесь ...", "📩 Прислать новость ...", etc.
        cta_pattern = re.compile(
            r'\n[😊📱📩🔔💬📢]\s*(Подписа|Прислать|Читайте|IzhevskToday|NewsRussain|t\.me|vk\.com)[^\n]*',
            re.IGNORECASE | re.UNICODE,
        )
        text = cta_pattern.sub('', text)

        # Also strip the separator line before CTA if it's now trailing
        text = re.sub(r'\n─+\n?$', '', text)

        # Clean up excessive whitespace
        text = re.sub(r'\n{3,}', '\n\n', text)
        text = text.strip()

        return text

    async def _api_call(self, method: str, **params) -> Optional[dict]:
        """Make a VK API call.

        Distinguishes between network-level errors (no connectivity / timeout)
        and VK API-level errors (bad token, access denied, quota exceeded).
        """
        session = await self._get_session()

        params.update({
            "access_token": self.access_token,
            "v": self.API_VERSION,
        })

        try:
            async with session.post(
                f"{self.API_BASE}/{method}",
                data=params,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                data = await resp.json()

                if "error" in data:
                    error = data["error"]
                    error_code = error.get("error_code")
                    error_msg = error.get("error_msg", "unknown")
                    # Provide actionable context based on known VK error codes
                    hint = {
                        5:  "Invalid access token — check VK_TOKEN",
                        15: "Access denied — check group admin rights",
                        100: "Invalid parameter passed to VK API",
                        214: "Post rejected by VK moderation",
                    }.get(error_code, "")
                    logger.error(
                        f"VK API error [{method}] code={error_code} msg='{error_msg}'"
                        + (f" | hint: {hint}" if hint else "")
                    )
                    return None

                return data.get("response")

        except asyncio.TimeoutError:
            logger.error(f"VK API timeout [{method}] — request exceeded 30s")
            return None
        except aiohttp.ClientError as e:
            logger.error(f"VK API network error [{method}]: {type(e).__name__}: {e}")
            return None
        except Exception as e:
            logger.error(f"VK API unexpected error [{method}]: {type(e).__name__}: {e}")
            return None

    async def _upload_photo(self, photo_url: str) -> Optional[str]:
        """Download a photo from URL and upload it to VK.
        
        Returns VK photo attachment string like 'photo-123_456' or None on failure.
        """
        session = await self._get_session()

        # Step 1: Get upload server URL
        upload_server = await self._api_call(
            "photos.getWallUploadServer",
            group_id=self.group_id,
        )
        if not upload_server:
            return None

        upload_url = upload_server.get("upload_url")
        if not upload_url:
            return None

        # Step 2: Download the photo from source URL
        try:
            async with session.get(photo_url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status != 200:
                    logger.error(f"Photo download failed: HTTP {resp.status} from {photo_url}")
                    return None
                photo_data = await resp.read()
        except asyncio.TimeoutError:
            logger.error(f"Photo download timeout (>30s): {photo_url}")
            return None
        except aiohttp.ClientError as e:
            logger.error(f"Photo download network error: {type(e).__name__}: {e}")
            return None

        # Step 3: Upload to VK
        try:
            form = aiohttp.FormData()
            form.add_field(
                "photo",
                photo_data,
                filename="photo.jpg",
                content_type="image/jpeg",
            )

            async with session.post(
                upload_url,
                data=form,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                upload_result = await resp.json()

            if not upload_result.get("photo") or upload_result["photo"] == "[]":
                logger.error("VK photo upload: server returned empty photo field — "
                             "file may be too large or in unsupported format")
                return None

        except asyncio.TimeoutError:
            logger.error("VK photo upload timeout — server took >30s")
            return None
        except aiohttp.ClientError as e:
            logger.error(f"VK photo upload network error: {type(e).__name__}: {e}")
            return None
        except Exception as e:
            logger.error(f"VK photo upload unexpected error: {type(e).__name__}: {e}")
            return None

        # Step 4: Save the uploaded photo
        save_result = await self._api_call(
            "photos.saveWallPhoto",
            group_id=self.group_id,
            photo=upload_result["photo"],
            server=upload_result["server"],
            hash=upload_result["hash"],
        )

        if save_result and len(save_result) > 0:
            photo = save_result[0]
            attachment = f"photo{photo['owner_id']}_{photo['id']}"
            logger.info(f"VK photo uploaded: {attachment}")
            return attachment

        return None

    async def publish(
        self,
        text: str,
        photo_url: Optional[str] = None,
    ) -> Optional[int]:
        """
        Publish a post to the VK community wall.
        
        Args:
            text: HTML-formatted post text (will be converted to plain text)
            photo_url: Optional URL of a photo to attach
            
        Returns:
            VK post_id on success, None on failure
        """
        # Convert HTML to VK-compatible text
        vk_text = self._html_to_vk(text)

        # Truncate to VK wall post limit (16384 chars)
        if len(vk_text) > 16000:
            vk_text = vk_text[:16000] + "..."



        # Add clean VK footer with plain URLs (VK auto-links them)
        vk_text += (
            "\n\n"
            "📱 Telegram: https://t.me/IzhevskTodayNews\n"
            "📩 Прислать новость: https://m.vk.com/mail?community=236380336"
        )

        params = {
            "owner_id": f"-{self.group_id}",
            "from_group": 1,
            "message": vk_text,
        }

        # Upload and attach photo if available
        if photo_url:
            attachment = await self._upload_photo(photo_url)
            if attachment:
                params["attachments"] = attachment

        result = await self._api_call("wall.post", **params)

        if result and "post_id" in result:
            logger.info(
                f"VK post published: vk.com/wall-{self.group_id}_{result['post_id']}"
            )
            return result["post_id"]

        logger.error("VK publish failed: no post_id in response")
        return None

    async def test_connection(self) -> dict:
        """Test VK API connection and return group info."""
        result = {
            "status": "error",
            "group_id": self.group_id,
            "token_set": bool(self.access_token),
        }

        # Try to get group info
        groups = await self._api_call(
            "groups.getById",
            group_id=self.group_id,
        )

        if groups:
            group = groups.get("groups", groups)
            if isinstance(group, list) and group:
                group = group[0]
            result["status"] = "ok"
            result["group_name"] = group.get("name", "unknown")
            result["group_url"] = f"https://vk.com/club{self.group_id}"

        return result
