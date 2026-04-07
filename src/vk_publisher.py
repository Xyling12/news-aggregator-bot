"""
VK Publisher — cross-posts news to VKontakte community wall.
Uses VK API to publish text and photo posts.
"""

import asyncio
import logging
import re
import os
import tempfile
import random
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)


class VKPublisher:
    """Publishes posts to a VKontakte community (group/public page)."""

    API_VERSION = "5.199"
    API_BASE = "https://api.vk.com/method"
    DEFAULT_VK_SEO_TAGS = [
        "#Ижевск",
        "#Удмуртия",
        "#НовостиИжевска",
        "#НовостиУдмуртии",
        "#Новости",
    ]
    SEO_TOPIC_RULES = [
        (("дтп", "авар", "пожар", "чп", "краж", "мошен"), ["#Происшествия", "#Безопасность"]),
        (("жкх", "отоплен", "вода", "коммунал"), ["#ЖКХ", "#Город"]),
        (("дорог", "автобус", "трамва", "маршрут", "пробк"), ["#Транспорт", "#Дороги"]),
        (("мэр", "глава", "администрац", "депутат", "дума"), ["#Власть", "#Город"]),
        (("школ", "детсад", "универс", "образован"), ["#Образование", "#Дети"]),
        (("больниц", "поликлиник", "медици", "врач"), ["#Здоровье", "#Медицина"]),
        (("спорт", "матч", "турнир", "чемпион"), ["#Спорт"]),
        (("погод", "мороз", "снег", "дожд"), ["#Погода"]),
        (("работ", "зарплат", "бизнес", "налог", "цен"), ["#Экономика", "#Работа"]),
        (("концерт", "театр", "фестивал", "выставк"), ["#Культура", "#Афиша"]),
    ]
    COMMENT_TOPIC_RULES = [
        (("дтп", "авар", "пожар", "чп"), [
            "Спасибо за оперативную информацию. Берегите себя и близких.",
            "Важная тема. Надеемся, что пострадавшим быстро окажут помощь.",
        ]),
        (("жкх", "отоплен", "вода", "коммунал"), [
            "Тема действительно важная для жителей. Спасибо, что подсвечиваете.",
            "Надеемся, по этому вопросу дадут конкретные сроки решения.",
        ]),
        (("дорог", "маршрут", "автобус", "трамва"), [
            "Хорошо, что поднимаете вопрос транспорта. Это влияет на всех каждый день.",
            "Спасибо за новость. Будем следить за развитием ситуации.",
        ]),
        (("школ", "детсад", "образован"), [
            "Спасибо за освещение темы. Для семей с детьми это особенно важно.",
            "Полезная новость для родителей, благодарим за информацию.",
        ]),
    ]
    GENERIC_COMMENT_TEMPLATES = [
        "Спасибо за полезную публикацию. Тема точно заслуживает обсуждения.",
        "Благодарим за информацию. Вопрос важный для жителей города.",
        "Полезный пост, спасибо. Будем следить за обновлениями по теме.",
    ]

    def __init__(self, access_token: str, group_id: str, user_token: str = ""):
        self.access_token = access_token
        self.has_explicit_user_token = bool((user_token or "").strip())
        self.user_token = (user_token or access_token).strip()
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
            r'\n[😊📱📩🔔💬📢📲🤖]\s*(Подписа|Прислать|Читайте|IzhevskToday|NewsRussain|t\.me|vk\.com|@Izhevsk)[^\n]*',
            re.IGNORECASE | re.UNICODE,
        )
        text = cta_pattern.sub('', text)

        # Also strip the separator line before CTA if it's now trailing
        text = re.sub(r'\n[─ ]+\n?$', '', text)

        # Clean up excessive whitespace
        text = re.sub(r'\n{3,}', '\n\n', text)
        text = text.strip()

        return text

    @staticmethod
    def _normalize_hashtag(tag: str) -> str:
        tag = re.sub(r"[^A-Za-zА-Яа-яЁё0-9_]", "", tag)
        return f"#{tag}" if tag else ""

    def _append_vk_seo_tags(self, text: str, max_tags: int = 9) -> str:
        if max_tags <= 0:
            return text

        found = re.findall(r"(?<!\w)#([A-Za-zА-Яа-яЁё0-9_]+)", text)
        existing = [self._normalize_hashtag(t) for t in found]
        existing = [t for t in existing if t]

        tags: list[str] = []
        seen = set()

        def add_tag(raw_tag: str):
            tag = self._normalize_hashtag(raw_tag.lstrip("#"))
            if not tag:
                return
            key = tag.lower()
            if key in seen:
                return
            seen.add(key)
            tags.append(tag)

        for tag in existing:
            add_tag(tag)

        for tag in self.DEFAULT_VK_SEO_TAGS:
            add_tag(tag)

        text_wo_tags = re.sub(r"(?<!\w)#[A-Za-zА-Яа-яЁё0-9_]+", " ", text.lower())
        for keywords, seo_tags in self.SEO_TOPIC_RULES:
            if any(kw in text_wo_tags for kw in keywords):
                for tag in seo_tags:
                    add_tag(tag)

        tags = tags[:max_tags]
        if not tags:
            return text

        text_without_tag_lines = re.sub(r"(?m)^\s*(?:#[A-Za-zА-Яа-яЁё0-9_]+\s*)+$", "", text).strip()
        return f"{text_without_tag_lines}\n\n{' '.join(tags)}"

    @staticmethod
    def _normalize_wall_target(target: str) -> dict:
        value = (target or "").strip()
        value = re.sub(r"^https?://(www\.)?vk\.com/", "", value, flags=re.IGNORECASE)
        value = value.strip("/")
        if value.startswith("@"):
            value = value[1:]

        if re.fullmatch(r"-?\d+", value):
            owner_id = int(value)
            if owner_id > 0:
                owner_id = -owner_id
            return {"owner_id": owner_id}

        m = re.fullmatch(r"(?:club|public)(\d+)", value, flags=re.IGNORECASE)
        if m:
            return {"owner_id": -int(m.group(1))}

        return {"domain": value}

    async def find_external_post_candidate(
        self,
        targets: list[str],
        *,
        keywords: list[str],
        scan_limit: int = 6,
        skip_post_keys: Optional[set[str]] = None,
    ) -> Optional[dict]:
        if not self.has_explicit_user_token:
            logger.info("VK outreach scan skipped: VK_USER_TOKEN is not configured")
            return None

        skip = skip_post_keys or set()
        normalized_keywords = [k.lower().strip() for k in keywords if k.strip()]

        for raw_target in targets:
            params = self._normalize_wall_target(raw_target)
            params.update({"count": max(3, min(20, scan_limit)), "filter": "owner"})
            wall = await self._api_call("wall.get", _token_override=self.user_token, **params)
            if not wall or "items" not in wall:
                continue

            for item in wall.get("items", []):
                owner_id = item.get("owner_id")
                post_id = item.get("id")
                text = (item.get("text") or "").strip()
                if not owner_id or not post_id or len(text) < 40:
                    continue
                if item.get("marked_as_ads") == 1 or item.get("is_deleted") == 1:
                    continue
                if item.get("copy_history"):
                    continue
                comments_meta = item.get("comments", {})
                if isinstance(comments_meta, dict) and comments_meta.get("can_post") == 0:
                    continue

                post_key = f"{owner_id}_{post_id}"
                if post_key in skip:
                    continue

                text_lower = text.lower()
                if normalized_keywords and not any(kw in text_lower for kw in normalized_keywords):
                    continue

                return {
                    "owner_id": owner_id,
                    "post_id": post_id,
                    "text": text,
                    "post_key": post_key,
                    "target": raw_target,
                }
        return None

    def build_thematic_comment(self, post_text: str) -> str:
        text_lower = (post_text or "").lower()
        for keywords, templates in self.COMMENT_TOPIC_RULES:
            if any(kw in text_lower for kw in keywords):
                return random.choice(templates)
        return random.choice(self.GENERIC_COMMENT_TEMPLATES)

    async def _api_call(self, method: str, **params) -> Optional[dict]:
        """Make a VK API call.

        Distinguishes between network-level errors (no connectivity / timeout)
        and VK API-level errors (bad token, access denied, quota exceeded).
        """
        session = await self._get_session()

        # Allow overriding the token (e.g. user token for photo uploads)
        token = params.pop("_token_override", None) or self.access_token
        params.update({
            "access_token": token,
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
                    if error_code == 27:
                        if method.startswith("photos."):
                            hint = "Method requires a user token with 'photos' scope"
                        elif method.startswith("stories."):
                            hint = "Method requires a user token with 'stories' scope"
                        elif method in {"wall.get", "wall.createComment"}:
                            hint = "This VK outreach action requires VK_USER_TOKEN"
                        else:
                            hint = "This method requires a user token instead of a group token"
                    else:
                        hint = {
                            5:  "Invalid access token - check VK_TOKEN",
                            15: "Access denied - check group admin rights",
                            100: "Invalid parameter passed to VK API",
                            214: "Post rejected by VK moderation",
                        }.get(error_code, "")
                    log_level = logger.error
                    if method == "wall.createComment" and error_code == 15:
                        log_level = logger.warning

                    log_level(
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

    async def _upload_photo(self, photo_url: str, photo_path: Optional[str] = None) -> Optional[str]:
        """Download a photo from URL (or read from local path) and upload it to VK.
        
        If `photo_path` is set, the local file is used directly — no HTTP download needed.
        This avoids 403 errors from Wikimedia/CDNs when re-downloading already-cached files.
        Returns VK photo attachment string like 'photo-123_456' or None on failure.
        """
        session = await self._get_session()

        # Step 1: Get upload server URL (requires user token with 'photos' scope)
        photo_token = self.user_token or self.access_token
        upload_server = await self._api_call(
            "photos.getWallUploadServer",
            group_id=int(self.group_id),
            _token_override=photo_token,
        )
        if not upload_server:
            return None

        upload_url = upload_server.get("upload_url")
        if not upload_url:
            return None

        # Step 2: Get photo bytes — from local file or by downloading from URL
        if photo_path and os.path.exists(photo_path):
            # Use already-downloaded local file — avoids repeated HTTP request and 403 from CDNs
            try:
                with open(photo_path, "rb") as f:
                    photo_data = f.read()
                logger.info(f"VK photo: using local file {photo_path}")
            except OSError as e:
                logger.error(f"Failed to read local photo file {photo_path}: {e}")
                return None
        else:
            # Use a dedicated session with User-Agent — Wikimedia Commons returns 403 without it
            _download_headers = {
                "User-Agent": "IzhevskTodayNewsBot/1.0 (https://t.me/IzhevskTodayNews)"
            }
            try:
                async with aiohttp.ClientSession(headers=_download_headers) as dl_session:
                    async with dl_session.get(photo_url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
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

        # Step 4: Save the uploaded photo (requires user token with 'photos' scope)
        save_result = await self._api_call(
            "photos.saveWallPhoto",
            group_id=int(self.group_id),
            photo=upload_result["photo"],
            server=upload_result["server"],
            hash=upload_result["hash"],
            _token_override=photo_token,
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
        photo_path: Optional[str] = None,
        *,
        seo_enabled: bool = True,
        seo_max_tags: int = 9,
    ) -> Optional[int]:
        """
        Publish a post to the VK community wall.
        
        Args:
            text: HTML-formatted post text (will be converted to plain text)
            photo_url: Optional URL of a photo to attach
            photo_path: Optional local file path (preferred over photo_url — avoids CDN 403)
            
        Returns:
            VK post_id on success, None on failure
        """
        # Convert HTML to VK-compatible text
        vk_text = self._html_to_vk(text)
        vk_text += (
            "\n\n─ ─ ─ ─ ─\n"
            "📸 Есть новость, фото или проблема в городе?\n"
            "Присылай нам в сообщения группы (опубликуем): vk.com/im/convo/-236380336\n"
            "📱 Наш Telegram: t.me/IzhevskTodayNews"
        )

        if seo_enabled:
            vk_text = self._append_vk_seo_tags(vk_text, max_tags=seo_max_tags)

        # Truncate to VK wall post limit (16384 chars)
        if len(vk_text) > 16000:
            vk_text = vk_text[:16000] + "..."

        params = {
            "owner_id": -int(self.group_id),
            "from_group": 1,
            "message": vk_text,
        }

        # Upload and attach photo if available (prefer local file to avoid CDN 403)
        if photo_path or photo_url:
            attachment = await self._upload_photo(photo_url or "", photo_path=photo_path)
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

    async def upload_story_photo(self, photo_bytes: bytes, link_text: str = "", link_url: str = "") -> bool:
        """
        Upload a generated image (bytes) to VK Stories on behalf of the community.
        Returns True if successful.
        """
        if not self.user_token:
            logger.error("VK stories require user_token with 'stories' access scope.")
            return False

        # Step 1: Get upload server
        params = {
            "add_to_news": 1,
            "group_id": abs(int(self.group_id)),
            "_token_override": self.user_token
        }
        if link_text and link_url:
            params["link_text"] = link_text
            params["link_url"] = link_url
            
        upload_server = await self._api_call("stories.getPhotoUploadServer", **params)
        if not upload_server or not upload_server.get("upload_url"):
            logger.error("VK story upload: failed to get upload server")
            return False
            
        upload_url = upload_server["upload_url"]

        # Step 2: Form POST the file
        try:
            form = aiohttp.FormData()
            form.add_field(
                "file",
                photo_bytes,
                filename="story.jpg",
                content_type="image/jpeg",
            )
            
            session = await self._get_session()
            async with session.post(upload_url, data=form, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                upload_result = await resp.json()
                
            if "response" in upload_result:
                upload_result = upload_result["response"]
                
            if not upload_result.get("upload_result"):
                logger.error(f"VK story upload returned empty: {upload_result}")
                return False
                
        except Exception as e:
            logger.error(f"VK story upload network error: {type(e).__name__}: {e}")
            return False

        # Step 3: Save the story
        save_result = await self._api_call(
            "stories.save",
            upload_results=upload_result["upload_result"],
            _token_override=self.user_token
        )
        
        if save_result and "count" in save_result:
            logger.info("VK Story published successfully!")
            return True
            
        logger.error(f"VK Story save failed: {save_result}")
        return False

    async def upload_story_video(self, video_path: str, link_text: str = "", link_url: str = "") -> bool:
        """
        Upload a generated video file to VK Stories on behalf of the community.
        Returns True if successful.
        """
        if not self.user_token:
            logger.error("VK stories require user_token with 'stories' access scope.")
            return False

        # Step 1: Get upload server for video
        params = {
            "add_to_news": 1,
            "group_id": abs(int(self.group_id)),
            "_token_override": self.user_token
        }
        if link_text and link_url:
            params["link_text"] = link_text
            params["link_url"] = link_url
            
        upload_server = await self._api_call("stories.getVideoUploadServer", **params)
        if not upload_server or not upload_server.get("upload_url"):
            logger.error("VK story video upload: failed to get upload server")
            return False
            
        upload_url = upload_server["upload_url"]

        # Step 2: Form POST the file
        try:
            form = aiohttp.FormData()
            with open(video_path, 'rb') as f:
                video_bytes = f.read()
                
            form.add_field(
                "video_file",
                video_bytes,
                filename="story.mp4",
                content_type="video/mp4",
            )
            
            session = await self._get_session()
            async with session.post(upload_url, data=form, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                upload_result = await resp.json()
                
            if "response" in upload_result:
                upload_result = upload_result["response"]
                
            if not upload_result.get("upload_result"):
                logger.error(f"VK story video upload returned empty: {upload_result}")
                return False
                
        except Exception as e:
            logger.error(f"VK story video upload network error: {type(e).__name__}: {e}")
            return False

        # Step 3: Save the story
        save_result = await self._api_call(
            "stories.save",
            upload_results=upload_result["upload_result"],
            _token_override=self.user_token
        )
        
        if save_result and "count" in save_result:
            logger.info("VK Video Story published successfully!")
            return True
            
        logger.error(f"VK Video Story save failed: {save_result}")
        return False

    async def upload_clip(
        self,
        video_path: str,
        caption: str = "",
        link_url: str = "",
    ) -> Optional[int]:
        """
        Upload a short video as a VK Clip (Shorts/Reels format).

        VK Clips use video.save + dedicated video upload server.
        Requires group access_token with video scope.
        Returns VK video ID on success, None on failure.

        Args:
            video_path: Path to local .mp4 file (vertical 9:16, max 60s recommended)
            caption:    Clip description/caption text (shown below clip)
            link_url:   Optional URL link to add to the clip
        """
        if not os.path.exists(video_path):
            logger.error(f"Clip video file not found: {video_path}")
            return None

        file_size = os.path.getsize(video_path)
        if file_size > 256 * 1024 * 1024:  # 256 MB VK limit
            logger.error(f"Clip video too large: {file_size / 1024 / 1024:.1f} MB (max 256 MB)")
            return None
        # VK Clips don't support "link" parameter for groups (causes Error 100).
        # Append the link to the description text instead.
        if link_url:
            caption = f"{caption}\n\n{link_url}" if caption else link_url

        # Step 1: Save video object — get upload server URL
        save_params: dict = {
            "group_id": int(self.group_id),
            "name": caption[:128] if caption else "Клип",
            "description": caption[:4096] if caption else "",
            "is_private": 0,
            "wallpost": 0,
            "repeat": 0,
        }
        if self.has_explicit_user_token:
            save_params["to_clips"] = 1
            save_params["_token_override"] = self.user_token
        else:
            # Fallback: if VK_USER_TOKEN is not configured, publish as regular video.
            save_params["wallpost"] = 1
            logger.warning("VK Clip upload: VK_USER_TOKEN is empty, fallback to regular VK video mode")

        save_result = await self._api_call("video.save", **save_params)
        if not save_result or not save_result.get("upload_url"):
            logger.error(f"VK Clip: failed to get upload server. Response: {save_result}")
            return None

        upload_url = save_result["upload_url"]
        video_id   = save_result.get("video_id")
        owner_id   = save_result.get("owner_id")

        # Step 2: Upload the video file
        try:
            with open(video_path, "rb") as f:
                video_bytes = f.read()

            form = aiohttp.FormData()
            form.add_field(
                "video_file",
                video_bytes,
                filename="clip.mp4",
                content_type="video/mp4",
            )

            session = await self._get_session()
            async with session.post(
                upload_url,
                data=form,
                timeout=aiohttp.ClientTimeout(total=120),  # large files need more time
            ) as resp:
                import json
                raw_text = await resp.text()
                try:
                    upload_result = json.loads(raw_text)
                except json.JSONDecodeError:
                    logger.error(f"VK Clip upload Non-JSON HTTP {resp.status}: {raw_text[:500]}")
                    return None
                
                if resp.status != 200:
                    logger.error(
                        f"VK Clip upload HTTP {resp.status}: {str(upload_result)[:200]}"
                    )
                    return None

            logger.debug(f"VK Clip upload response: {upload_result}")

        except asyncio.TimeoutError:
            logger.error("VK Clip upload timeout — server took >120s")
            return None
        except Exception as e:
            logger.error(f"VK Clip upload error: {type(e).__name__}: {e}")
            return None

        # Step 3:
        # - real VK Clips (`to_clips=1`) are left for VK processing;
        # - fallback regular videos are explicitly posted to the wall so they become visible in the community feed.
        if video_id and owner_id:
            if not self.has_explicit_user_token:
                attachment = f"video{owner_id}_{video_id}"
                wall_params = {
                    "owner_id": -int(self.group_id),
                    "from_group": 1,
                    "message": caption[:4096] if caption else "",
                    "attachments": attachment,
                }
                for attempt in range(5):
                    if attempt:
                        await asyncio.sleep(12)
                    wall_result = await self._api_call("wall.post", **wall_params)
                    if wall_result and "post_id" in wall_result:
                        logger.info(
                            f"✅ VK fallback video published to wall: vk.com/wall-{self.group_id}_{wall_result['post_id']} "
                            f"(video {attachment})"
                        )
                        return video_id
                logger.warning(f"VK fallback video uploaded but wall.post failed for {attachment}")
                return video_id

            logger.info(f"✅ VK Clip uploaded successfully (video_id={video_id}) and sent to processing.")
            return video_id

        logger.error("VK Clip: video_id missing from save response")
        return None

    async def create_comment(
        self,
        post_id: int,
        message: str,
        *,
        owner_id: Optional[int] = None,
    ) -> Optional[int]:
        """
        Create a comment on a wall post from behalf of the group.
        Used to spark engagement (first comment bait).
        """
        if not post_id or not message:
            return None
        if not self.has_explicit_user_token:
            logger.info("VK comment skipped: VK_USER_TOKEN is not configured")
            return None
             
        params = {
            "owner_id": owner_id if owner_id is not None else -int(self.group_id),
            "post_id": post_id,
            "from_group": int(self.group_id),
            "message": message,
            "_token_override": self.user_token,
        }
        result = await self._api_call("wall.createComment", **params)
        if result and "comment_id" in result:
            logger.info(f"✅ First comment added to post {post_id}: {message[:40]}...")
            return result["comment_id"]
            
        logger.warning(f"Skipped creating first comment on post {post_id} (missing rights or API error)")
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
