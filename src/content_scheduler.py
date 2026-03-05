"""
Content Scheduler — publishes auto-generated content on a fixed daily schedule.
Posts are published DIRECTLY to the channel at exact local times (UTC+4 Izhevsk),
bypassing the regular moderation queue.
"""

import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, Callable, List

from aiogram import Bot
from aiogram.types import FSInputFile
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
    (11, 0,  "five_facts",    "📌 5 фактов"),
    (13, 0,  "recipe",        "🍽 Рецепт"),
    (15, 0,  "lifehack",      "💡 Полезно"),
    (17, 0,  "place",         "📍 Места Удмуртии"),
    (19, 0,  "evening_fun",   "😄 Вечерний"),
    (21, 0,  "daily_digest",  "📊 Итоги дня"),
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
                    self._last_date = today_str
                    logger.info(f"New day: {today_str}, schedule reset")

                # Check each scheduled rubric
                for hour, minute, rubric, label in DEFAULT_SCHEDULE:
                    slot_key = f"{today_str}_{rubric}"

                    # Already published?
                    if slot_key in self._published_today:
                        continue

                    # Is it time? (within a 2-minute window)
                    if now.hour == hour and now.minute >= minute and now.minute < minute + 2:
                        logger.info(f"⏰ Time to publish: {label} ({rubric})")
                        try:
                            await self._publish_rubric(rubric, label)
                            self._published_today.add(slot_key)
                        except Exception as e:
                            logger.error(f"Failed to publish {rubric}: {e}", exc_info=True)

                    # Catch up: if bot was down and missed a slot (within 30 min window)
                    elif now.hour == hour and now.minute >= minute and now.minute < minute + 30:
                        if slot_key not in self._published_today:
                            logger.info(f"⏰ Catch-up publish: {label} ({rubric})")
                            try:
                                await self._publish_rubric(rubric, label)
                                self._published_today.add(slot_key)
                            except Exception as e:
                                logger.error(f"Failed catch-up {rubric}: {e}", exc_info=True)

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

        if rubric == "weather":
            text, photo_url = await self.generator.generate_weather()
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

        # Publish with photo if available
        try:
            if photo_url:
                try:
                    msg = await self.bot.send_photo(
                        target,
                        photo=photo_url,
                        caption=text[:1024],
                        parse_mode=ParseMode.HTML,
                    )
                    logger.info(f"✅ Published {label} with photo to {target}")
                except Exception as photo_err:
                    logger.warning(f"Photo send failed ({photo_err}), sending text only")
                    msg = await self.bot.send_message(
                        target,
                        text[:4096],
                        parse_mode=ParseMode.HTML,
                    )
                    logger.info(f"✅ Published {label} (text only) to {target}")
            else:
                msg = await self.bot.send_message(
                    target,
                    text[:4096],
                    parse_mode=ParseMode.HTML,
                )
                logger.info(f"✅ Published {label} (no photo) to {target}")

            # ── Emoji reactions directly on post (skip digest — it's already analytical) ──
            if rubric != "daily_digest" and msg:
                try:
                    _t = text.lower()
                    if any(w in _t for w in ["погиб", "авария", "дтп", "пожар", "трагед", "жертв"]):
                        _emojis = ["😢", "🙏", "😱"]
                    elif any(w in _t for w in ["жкх", "тариф", "чиновник", "мэр", "депутат"]):
                        _emojis = ["😡", "🤷", "😂"]
                    elif any(w in _t for w in ["открыт", "новый", "запуст", "построен"]):
                        _emojis = ["🔥", "👍", "🎉"]
                    elif rubric in ("recipe", "place", "history_fact"):
                        _emojis = ["🔥", "👍", "🤔"]
                    elif rubric == "weather":
                        _emojis = ["😐", "🥱", "🔥"]
                    else:
                        _emojis = ["😡", "🤷", "😂", "👍"]
                    from aiogram.types import ReactionTypeEmoji as _Rte
                    await self.bot.set_message_reaction(
                        chat_id=target,
                        message_id=msg.message_id,
                        reaction=[_Rte(emoji=e) for e in _emojis],
                    )
                    logger.info(f"✅ Reactions set for {label}: {_emojis}")
                except Exception as react_err:
                    logger.warning(f"Reactions failed for {label}: {react_err}")

            # VK crosspost
            try:
                import src.bot as bot_module
                vk = getattr(bot_module, '_vk_publisher', None)
                if vk and vk.enabled:
                    await vk.publish(text, photo_url=photo_url)
                    logger.info(f"✅ VK crosspost: {label}")
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
