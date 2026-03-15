"""
Content Scheduler — publishes auto-generated content on a fixed daily schedule.
Posts are published DIRECTLY to the channel at exact local times (UTC+4 Izhevsk),
bypassing the regular moderation queue.
"""

import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, Callable, List

import io

import aiohttp
from PIL import Image as PILImage

from aiogram import Bot
from aiogram.types import FSInputFile, BufferedInputFile
from aiogram.enums import ParseMode

from src.config import Config
from src.content_generator import ContentGenerator
from src.database import Database
from src.ai_rewriter import AIRewriter

logger = logging.getLogger(__name__)

# Izhevsk timezone: UTC+4 (Europe/Samara)
TZ_IZHEVSK = timezone(timedelta(hours=4))

# ── Schedule: (hour, minute) -> rubric method name ───────────────────────
DEFAULT_SCHEDULE = [
    (7,  0,  "weather",       "🌤 Погода"),
    (8,  0,  "holiday",       "🎉 Праздник"),  # Only publishes if today is a holiday
    (9,  0,  "history_fact",  "📅 История"),
    (10, 0,  "cat_story",     "🐾 Котики (VK Story)"),
    (11, 0,  "five_facts",    "📌 5 фактов"),
    (12, 0,  "video_story",   "🎥 Видео-факт (VK Story)"),
    (13, 0,  "recipe",        "🍽 Рецепт"),
    (14, 0,  "cat_story",     "🐾 Котики (VK Story)"),
    (15, 0,  "lifehack",      "💡 Полезно"),
    (16, 0,  "fact_story",    "❓ Факт (VK Story)"),
    (17, 0,  "place",         "📍 Места Удмуртии"),
    (19, 0,  "evening_fun",   "😄 Вечерний"),
    (21, 0,  "daily_digest",  "📊 Итоги дня"),
    (22, 0,  "cat_story",     "🐾 Котики (VK Story)"),
]


class ContentScheduler:
    """Publishes auto-generated content at fixed times (Izhevsk local time)."""

    def __init__(
        self,
        config: Config,
        bot: Bot,
        generator: ContentGenerator,
        db: Database,
        rewriter: Optional[AIRewriter] = None,
    ):
        self.config = config
        self.bot = bot
        self.generator = generator
        self.db = db
        self.rewriter = rewriter
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._published_today: set[str] = set()  # Track what was published today
        self._last_date: Optional[str] = None
        self._failed_slots: dict[str, int] = {}  # slot_key -> retry count

    def _now(self) -> datetime:
        """Get current time in Izhevsk timezone."""
        return datetime.now(TZ_IZHEVSK)

    async def start(self):
        """Start the content scheduler loop."""
        self._running = True
        self._task = asyncio.create_task(self._scheduler_loop())
        logger.info(
            f"Content scheduler started: {len(DEFAULT_SCHEDULE)} rubrics/day, "
            f"timezone: UTC+4 (Izhevsk)"
        )

    async def stop(self):
        """Stop the scheduler."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Content scheduler stopped")

    async def _notify_admins(self, text: str) -> None:
        """Send a short alert to all configured admin IDs."""
        if not self.config.admin_ids:
            return
        for admin_id in self.config.admin_ids:
            try:
                await self.bot.send_message(admin_id, text, parse_mode=None)
            except Exception as e:
                logger.warning(f"Failed to notify admin {admin_id}: {e}")

    async def _scheduler_loop(self):
        """Main loop: check every 30 seconds if it's time to publish."""
        logger.info("Content scheduler loop started")

        while self._running:
            try:
                now = self._now()
                today_str = now.strftime("%Y-%m-%d")

                # Reset published list at midnight
                if self._last_date != today_str:
                    self._published_today.clear()
                    self._failed_slots.clear()
                    self._last_date = today_str
                    logger.info(f"New day: {today_str}, schedule reset")

                # Max retries for a single slot before giving up for today
                MAX_CATCH_UP_RETRIES = 2

                # Check each scheduled rubric
                for hour, minute, rubric, label in DEFAULT_SCHEDULE:
                    slot_key = f"{today_str}_{rubric}"

                    # Already published or permanently skipped?
                    if slot_key in self._published_today:
                        continue

                    # Is it time? (within a 2-minute window)
                    if now.hour == hour and now.minute >= minute and now.minute < minute + 2:
                        logger.info(f"⏰ Time to publish: {label} ({rubric})")
                        try:
                            await self._publish_rubric(rubric, label)
                            self._published_today.add(slot_key)
                            self._failed_slots.pop(slot_key, None)  # clear on success
                        except Exception as e:
                            logger.error(f"Failed to publish {rubric}: {e}", exc_info=True)
                            retries = self._failed_slots.get(slot_key, 0) + 1
                            self._failed_slots[slot_key] = retries
                            if retries >= MAX_CATCH_UP_RETRIES:
                                msg = (
                                    f"⚠️ Scheduler: рубрика '{label}' ПРОПУЩЕНА сегодня.\n"
                                    f"Причина: {e}\n"
                                    f"Проверь квоту Gemini API или добавь GROQ_API_KEY в env."
                                )
                                logger.warning(f"⛔ {rubric}: {retries} failures — marking as SKIPPED for today. Причина: {e}")
                                self._published_today.add(slot_key)  # stop retrying
                                asyncio.create_task(self._notify_admins(msg))

                    # Catch up: if bot was down and missed a slot (within 30 min window)
                    elif now.hour == hour and now.minute >= minute and now.minute < minute + 30:
                        retries = self._failed_slots.get(slot_key, 0)
                        if retries >= MAX_CATCH_UP_RETRIES:
                            continue  # Already gave up on this slot
                        logger.info(f"⏰ Catch-up publish: {label} ({rubric}) [attempt {retries + 1}/{MAX_CATCH_UP_RETRIES}]")
                        try:
                            await self._publish_rubric(rubric, label)
                            self._published_today.add(slot_key)
                            self._failed_slots.pop(slot_key, None)
                        except Exception as e:
                            logger.error(f"Failed catch-up {rubric}: {e}", exc_info=True)
                            retries = self._failed_slots.get(slot_key, 0) + 1
                            self._failed_slots[slot_key] = retries
                            if retries >= MAX_CATCH_UP_RETRIES:
                                msg = (
                                    f"⚠️ Catch-up: рубрика '{label}' ПРОПУЩЕНА сегодня.\n"
                                    f"AI квота исчерпана (все Gemini ключи + fallback).\n"
                                    f"Добавь GROQ_API_KEY в env для автоматического резерва."
                                )
                                logger.warning(
                                    f"⛔ {rubric}: catch-up {retries}/{MAX_CATCH_UP_RETRIES} — "
                                    f"SKIPPED для сегодня. AI квота исчерпана."
                                )
                                self._published_today.add(slot_key)  # stop retrying
                                asyncio.create_task(self._notify_admins(msg))

                await asyncio.sleep(30)  # Check every 30 seconds

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Scheduler loop error: {e}", exc_info=True)
                await asyncio.sleep(60)

        logger.info("Content scheduler loop stopped")

    async def _publish_rubric(self, rubric: str, label: str) -> bool:
        """Generate and publish a rubric post directly to the channel. Returns True on success."""
        target = self.config.target_channel
        if not target.startswith("@") and not target.startswith("-"):
            target = f"@{target}"

        # Generate content
        text, photo_url = None, None
        weather_data = None
        
        if rubric == "cat_story":
            try:
                import src.bot as bot_module
                vk = getattr(bot_module, '_vk_publisher', None)
                if vk and vk.enabled:
                    if not hasattr(self, 'story_generator'):
                        from src.story_generator import StoryGenerator
                        self.story_generator = StoryGenerator()
                    story_bytes = await self.story_generator.generate_cat_story()
                    if story_bytes:
                        s_res = await vk.upload_story_photo(
                            story_bytes,
                            link_text="learn_more",
                            link_url="https://vk.com/izhevsk_segodnya"
                        )
                        if s_res:
                            logger.info(f"✅ VK Cat Story published")
                            return True
            except Exception as e:
                logger.error(f"Failed to publish VK Cat Story: {e}", exc_info=True)
            return False

        if rubric == "video_story":
            try:
                import src.bot as bot_module
                vk = getattr(bot_module, '_vk_publisher', None)
                if vk and vk.enabled:
                    if not hasattr(self, 'story_generator'):
                        from src.story_generator import StoryGenerator
                        self.story_generator = StoryGenerator()
                    
                    # 1. Generate text using facts or lifehacks logic from ContentGenerator
                    text, photo_url = await self.generator.generate_five_facts()
                    import re
                    # Extract just the first bullet point or sentence
                    if text:
                        match = re.search(r'1\.\s(.*?)\n', text)
                        short_text = match.group(1) if match else "Интересно, не правда ли?"
                    else:
                        short_text = "Время интересных фактов об Удмуртии!"

                    # 2. Get a stock video 
                    from src.media_processor import MediaProcessor
                    import os
                    mp = MediaProcessor(pexels_key=os.getenv("PEXELS_API_KEY", ""))
                    v_url = await mp.search_pexels_video(["Izhevsk", "nature", "city"], min_duration=5, max_duration=15)
                    
                    if v_url:
                        vid_path = await self.story_generator.generate_video_story(v_url, short_text)
                        
                        # 3. Upload to VK
                        if vid_path:
                            s_res = await vk.upload_story_video(
                                vid_path,
                                link_text="learn_more",
                                link_url="https://vk.com/izhevsk_segodnya"
                            )
                            if s_res:
                                logger.info(f"✅ VK Video Story published")
                                return True
            except Exception as e:
                logger.error(f"Failed to publish VK Video Story: {e}", exc_info=True)
            return False

        if rubric == "fact_story":
            try:
                import src.bot as bot_module
                vk = getattr(bot_module, '_vk_publisher', None)
                if vk and vk.enabled:
                    if not hasattr(self, 'story_generator'):
                        from src.story_generator import StoryGenerator
                        self.story_generator = StoryGenerator()
                    
                    text, photo_url = await self.generator.generate_history_fact()
                    import re
                    # Pluck a short sentence from the fact
                    sentences = re.split(r'(?<=[.!?]) +', text.replace('\n', ' ')) if text else []
                    short_text = sentences[1] if len(sentences) > 1 else (sentences[0] if sentences else "Ижевск — город тружеников!")
                    
                    story_bytes = await self.story_generator.generate_quiz_story(photo_url, short_text)
                    if story_bytes:
                        s_res = await vk.upload_story_photo(
                            story_bytes,
                            link_text="learn_more",
                            link_url="https://vk.com/izhevsk_segodnya"
                        )
                        if s_res:
                            logger.info(f"✅ VK Fact Story published")
                            return True
            except Exception as e:
                logger.error(f"Failed to publish VK Fact Story: {e}", exc_info=True)
            return False

        if rubric == "weather":
            res = await self.generator.generate_weather()
            if res:
                if len(res) == 3:
                    text, photo_url, weather_data = res
                else:
                    text, photo_url = res
        elif rubric == "holiday":
            text, photo_url = await self.generator.generate_holiday()
            if not text:  # Not a holiday today — skip silently
                logger.info("No holiday today, skipping")
                return True  # Not an error — just no holiday
        elif rubric == "history_fact":
            text, photo_url = await self.generator.generate_history_fact()
        elif rubric == "five_facts":
            text, photo_url = await self.generator.generate_five_facts()
        elif rubric == "recipe":
            text, photo_url = await self.generator.generate_recipe()
        elif rubric == "lifehack":
            text, photo_url = await self.generator.generate_lifehack()
        elif rubric == "place":
            text, photo_url = await self.generator.generate_place()
        elif rubric == "evening_fun":
            text, photo_url = await self.generator.generate_evening_fun()
        elif rubric == "daily_digest":
            published = await self.db.get_today_published_texts()
            text, photo_url = await self.generator.generate_daily_digest(published)

        if not text:
            logger.warning(f"Content generation returned empty for {rubric} — Gemini may be rate-limited")
            raise RuntimeError(f"AI вернул пустой текст для '{label}'. Возможно, квота Gemini исчерпана — попробуй позже.")

        # Guard: reject AI refusals before publishing ("Я не могу обсудить эту тему...")
        try:
            from src.ai_rewriter import AIRewriter as _AIR
            if _AIR._is_refusal(text):
                logger.warning(f"Skipping {rubric}: AI returned a refusal message")
                raise RuntimeError(f"AI отказался генерировать '{label}' (safety filter). Пропускаем публикацию.")
        except ImportError:
            pass  # Safety: never block publication due to import errors

        # Convert any leftover Markdown to Telegram HTML (safety net — prompts forbid Markdown,
        # but AI sometimes ignores instructions)
        import re as _re
        def _md_to_html(t: str) -> str:
            # **bold** → <b>bold</b>
            t = _re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', t, flags=_re.DOTALL)
            # *italic* or _italic_ → <i>italic</i>
            t = _re.sub(r'(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)', r'<i>\1</i>', t, flags=_re.DOTALL)
            t = _re.sub(r'__(.+?)__', r'<i>\1</i>', t, flags=_re.DOTALL)
            # `code` → <code>code</code>
            t = _re.sub(r'`(.+?)`', r'<code>\1</code>', t)
            # Remove bare # headers (just strip the #)
            t = _re.sub(r'^#{1,3}\s*', '', t, flags=_re.MULTILINE)
            return t

        text = _md_to_html(text)

        # Publish with photo if available
        # IMPORTANT: Telegram caption limit is 1024 chars. For long posts (5 facts, history, etc.)
        # we send photo first (no caption), then full text as a separate message.
        CAPTION_LIMIT = 900  # safe threshold below 1024
        msg = None
        try:
            if photo_url:
                use_caption = len(text) <= CAPTION_LIMIT

                async def _send_photo_inner(photo_source) -> bool:
                    """Try send_photo; return True on success."""
                    nonlocal msg
                    caption_arg = text[:CAPTION_LIMIT] if use_caption else None
                    msg = await self.bot.send_photo(
                        target,
                        photo=photo_source,
                        caption=caption_arg,
                        parse_mode=ParseMode.HTML if caption_arg else None,
                    )
                    return True

                photo_sent = False
                try:
                    photo_sent = await _send_photo_inner(photo_url)
                    logger.info(f"✅ Published {label} photo to {target} (caption={'yes' if use_caption else 'no'})")
                except Exception as photo_err:
                    logger.warning(f"Photo send by URL failed ({photo_err}), trying file upload")
                    try:
                        headers = {"User-Agent": "IzhevskTodayNewsBot/1.0 (https://t.me/IzhevskTodayNews)"}
                        async with aiohttp.ClientSession(headers=headers) as session:
                            async with session.get(photo_url, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                                if resp.status == 200:
                                    img_bytes = await resp.read()
                                    if len(img_bytes) > 1000:
                                        # Convert to JPEG in memory to fix format mismatch
                                        # (Wikimedia returns PNG/WEBP — Telegram fails if bytes ≠ extension)
                                        pil_img = PILImage.open(io.BytesIO(img_bytes))
                                        if pil_img.mode in ('RGBA', 'LA', 'P'):
                                            pil_img = pil_img.convert('RGB')
                                        jpeg_buf = io.BytesIO()
                                        pil_img.save(jpeg_buf, format='JPEG', quality=85)
                                        jpeg_bytes = jpeg_buf.getvalue()
                                        if len(jpeg_bytes) > 8 * 1024 * 1024:
                                            raise ValueError("Image too large for Telegram (>8MB)")
                                        input_file = BufferedInputFile(jpeg_bytes, filename="photo.jpg")
                                        photo_sent = await _send_photo_inner(input_file)
                                        logger.info(f"✅ Published {label} photo (file upload) to {target}")
                    except Exception as upload_err:
                        logger.warning(f"Photo file upload also failed ({upload_err}), sending text only")

                # If text was too long for caption — send as separate message
                if photo_sent and not use_caption:
                    msg = await self.bot.send_message(
                        target,
                        text[:4096],
                        parse_mode=ParseMode.HTML,
                    )
                    logger.info(f"✅ Published {label} full text (separate message) to {target}")
                elif not photo_sent:
                    # Photo completely failed — fallback to text-only
                    msg = await self.bot.send_message(
                        target,
                        text[:4096],
                        parse_mode=ParseMode.HTML,
                    )
                    logger.info(f"✅ Published {label} (text only, photo failed) to {target}")
            else:
                msg = await self.bot.send_message(
                    target,
                    text[:4096],
                    parse_mode=ParseMode.HTML,
                )
                logger.info(f"✅ Published {label} (no photo) to {target}")

            # ── Emoji reaction directly on post (skip digest — it's already analytical) ──
            # Telegram bots can set only ONE reaction per message (non-premium limit).
            if rubric != "daily_digest" and msg:
                try:
                    _t = text.lower()
                    if any(w in _t for w in ["погиб", "авария", "дтп", "пожар", "трагед", "жертв"]):
                        _emoji = "😢"
                    elif any(w in _t for w in ["жкх", "тариф", "чиновник", "мэр", "депутат"]):
                        _emoji = "😡"
                    elif any(w in _t for w in ["открыт", "новый", "запуст", "построен"]):
                        _emoji = "🔥"
                    elif rubric in ("recipe", "place", "history_fact"):
                        _emoji = "🔥"
                    elif rubric == "weather":
                        _emoji = "😐"
                    else:
                        _emoji = "👍"
                    from aiogram.types import ReactionTypeEmoji as _Rte
                    await self.bot.set_message_reaction(
                        chat_id=target,
                        message_id=msg.message_id,
                        reaction=[_Rte(emoji=_emoji)],
                    )
                    logger.info(f"✅ Reaction set for {label}: {_emoji}")
                except Exception as react_err:
                    logger.warning(f"Reaction failed for {label}: {react_err}")

            # VK crosspost
            try:
                import src.bot as bot_module
                vk = getattr(bot_module, '_vk_publisher', None)
                if vk and vk.enabled:
                    await vk.publish(text, photo_url=photo_url)
                    logger.info(f"✅ VK crosspost: {label}")
                    
                    # ── Publish STORY if applicable ──
                    if rubric == "weather" and weather_data:
                        try:
                            if not hasattr(self, 'story_generator'):
                                from src.story_generator import StoryGenerator
                                self.story_generator = StoryGenerator()
                            
                            now_dt = self._now()
                            date_str = now_dt.strftime("%d %B").lstrip("0")
                            temp_val = weather_data.get('temp', 0)
                            temp_str = f"+{temp_val}°C" if temp_val > 0 else f"{temp_val}°C"
                            
                            story_bytes = await self.story_generator.generate_weather_story(
                                bg_url=photo_url,
                                temp_str=temp_str,
                                desc=weather_data.get('description', '').capitalize(),
                                date_str=date_str
                            )
                            if story_bytes:
                                s_res = await vk.upload_story_photo(
                                    story_bytes,
                                    link_text="learn_more",
                                    link_url="https://vk.com/izhevsk_segodnya"
                                )
                                if s_res:
                                    logger.info(f"✅ VK Weather Story published")
                        except Exception as s_err:
                            logger.error(f"Failed to publish VK story: {s_err}", exc_info=True)
                            
            except Exception as vk_err:
                logger.warning(f"VK crosspost failed for {label}: {vk_err}")

            # Notify admins
            for admin_id in self.config.admin_ids:
                try:
                    await self.bot.send_message(
                        admin_id,
                        f"🤖 Авто-контент опубликован:\n{label}\n\n"
                        f"📝 {text[:200]}...",
                        parse_mode=ParseMode.HTML,
                    )
                except Exception:
                    pass

        except Exception as e:
            logger.error(f"Failed to publish {label} to {target}: {e}")
            raise

    async def force_publish(self, rubric: str) -> bool:
        """Force-publish a rubric (for /test_content command)."""
        label = rubric
        for _, _, r, l in DEFAULT_SCHEDULE:
            if r == rubric:
                label = l
                break

        try:
            await self._publish_rubric(rubric, label)
            return True
        except Exception as e:
            logger.error(f"Force publish failed: {e}")
            return False
