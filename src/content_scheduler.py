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
    (8,  0,  "holiday",       "🎉 Праздник"),
    (9,  0,  "animal_clip",   "🐶 Животные утро (VK Клип)"),
    (9,  30, "history_fact",  "📅 История"),
    (10, 0,  "cat_story",     "🐾 Котики (VK Story)"),
    (10, 30, "cat_clip",      "😸 Котики (VK Клип)"),
    (11, 0,  "five_facts",    "📌 5 фактов"),
    (12, 0,  "video_story",   "🎥 Видео-факт (VK Story)"),
    (13, 0,  "animal_clip",   "🐱 Животные обед (VK Клип)"),
    (13, 30, "recipe",        "🍽 Рецепт"),
    (14, 0,  "cat_story",     "🐾 Котики (VK Story)"),
    (15, 0,  "lifehack",      "💡 Полезно"),
    (16, 0,  "fact_story",    "❓ Факт (VK Story)"),
    (17, 0,  "place",         "📍 Места Удмуртии"),
    (18, 0,  "animal_clip",   "🐾 Животные вечер (VK Клип)"),
    (19, 0,  "evening_fun",   "😄 Вечерний"),
    (20, 0,  "animal_clip",   "🐱 Животные ночь (VK Клип)"),
    (21, 0,  "daily_digest",  "📊 Итоги дня"),
    (21, 0,  "cat_clip",      "😹 Котики ночь (VK Клип)"),
    (22, 0,  "cat_story",     "🐾 Котики (VK Story)"),
]


# How many days to remember a photo URL to avoid repeats
_PHOTO_DEDUP_DAYS = 30
_SLOT_DEDUP_DAYS = 7


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
        self._used_photo_urls: set[str] = self._load_photo_history()  # Persistent dedup

    def _now(self) -> datetime:
        """Get current time in Izhevsk timezone."""
        return datetime.now(TZ_IZHEVSK)

    # ── Persistent photo deduplication ────────────────────────────────────────

    def _photo_history_path(self) -> str:
        import os
        return os.path.join(self.config.media_dir, "..", "data", "photo_url_history.json")

    def _published_slots_path(self) -> str:
        import os
        return os.path.join(self.config.media_dir, "..", "data", "published_slots.json")

    def _load_photo_history(self) -> set:
        """Load used photo URLs from disk; prune entries older than _PHOTO_DEDUP_DAYS."""
        import json, os
        path = os.path.normpath(self._photo_history_path())
        try:
            with open(path, "r") as f:
                data = json.load(f)  # {url: iso_date_str}
            cutoff = (datetime.now() - timedelta(days=_PHOTO_DEDUP_DAYS)).date().isoformat()
            fresh = {url for url, date in data.items() if date >= cutoff}
            logger.info(f"Photo history loaded: {len(fresh)} URLs in last {_PHOTO_DEDUP_DAYS} days")
            return fresh
        except (FileNotFoundError, json.JSONDecodeError):
            return set()

    def _save_photo_history(self, new_urls: set) -> None:
        """Persist new photo URLs with today's date to disk."""
        import json, os
        path = os.path.normpath(self._photo_history_path())
        os.makedirs(os.path.dirname(path), exist_ok=True)
        today = datetime.now().date().isoformat()
        # Load existing file to preserve old entries
        try:
            with open(path, "r") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            data = {}
        for url in new_urls:
            data[url] = today
        # Prune entries older than _PHOTO_DEDUP_DAYS
        cutoff = (datetime.now() - timedelta(days=_PHOTO_DEDUP_DAYS)).date().isoformat()
        data = {url: date for url, date in data.items() if date >= cutoff}
        try:
            with open(path, "w") as f:
                json.dump(data, f)
        except Exception as e:
            logger.warning(f"Failed to save photo history: {e}")

    def _load_published_slots(self, today_str: str) -> set[str]:
        """Load already-published slot keys for today from disk and prune old days."""
        import json, os
        path = os.path.normpath(self._published_slots_path())
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if not isinstance(raw, dict):
                return set()
        except (FileNotFoundError, json.JSONDecodeError):
            return set()
        except Exception as e:
            logger.warning(f"Failed to load published slot history: {e}")
            return set()

        cutoff = (self._now().date() - timedelta(days=_SLOT_DEDUP_DAYS)).isoformat()
        cleaned: dict[str, list[str]] = {}
        for day, slots in raw.items():
            if not isinstance(day, str) or day < cutoff:
                continue
            if not isinstance(slots, list):
                continue
            valid_slots = [slot for slot in slots if isinstance(slot, str)]
            if valid_slots:
                cleaned[day] = valid_slots

        if cleaned != raw:
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(cleaned, f, ensure_ascii=False)
            except Exception as e:
                logger.warning(f"Failed to rewrite pruned slot history: {e}")

        return set(cleaned.get(today_str, []))

    def _save_published_slots(self, today_str: str) -> None:
        """Persist today's published slot keys to disk."""
        import json, os
        path = os.path.normpath(self._published_slots_path())
        os.makedirs(os.path.dirname(path), exist_ok=True)

        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if not isinstance(raw, dict):
                raw = {}
        except (FileNotFoundError, json.JSONDecodeError):
            raw = {}
        except Exception as e:
            logger.warning(f"Failed to read slot history before save: {e}")
            raw = {}

        cutoff = (self._now().date() - timedelta(days=_SLOT_DEDUP_DAYS)).isoformat()
        cleaned: dict[str, list[str]] = {}
        for day, slots in raw.items():
            if not isinstance(day, str) or day < cutoff:
                continue
            if isinstance(slots, list):
                valid_slots = [slot for slot in slots if isinstance(slot, str)]
                if valid_slots:
                    cleaned[day] = valid_slots

        if self._published_today:
            cleaned[today_str] = sorted(self._published_today)
        else:
            cleaned.pop(today_str, None)

        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(cleaned, f, ensure_ascii=False)
        except Exception as e:
            logger.warning(f"Failed to save published slot history: {e}")

    def _mark_slot_done(self, today_str: str, slot_key: str, persist: bool = True) -> None:
        """Mark slot as done for today; optionally persist to survive restarts."""
        self._published_today.add(slot_key)
        if persist:
            self._save_published_slots(today_str)

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
                now_total_minutes = now.hour * 60 + now.minute

                # Reset published list at midnight
                if self._last_date != today_str:
                    self._published_today = self._load_published_slots(today_str)
                    self._failed_slots.clear()
                    # Reload photo history from disk (do NOT clear — dedup is persistent)
                    self._used_photo_urls = self._load_photo_history()
                    self._last_date = today_str
                    logger.info(
                        f"New day: {today_str}, schedule reset "
                        f"(restored slots: {len(self._published_today)})"
                    )

                # Max retries for a single slot before giving up for today
                MAX_CATCH_UP_RETRIES = 2

                # Check each scheduled rubric
                for hour, minute, rubric, label in DEFAULT_SCHEDULE:
                    is_clip_slot = rubric in {"animal_clip", "cat_clip"}
                    slot_max_retries = 8 if is_clip_slot else MAX_CATCH_UP_RETRIES
                    slot_catchup_minutes = 60 if is_clip_slot else 30
                    slot_start_minutes = hour * 60 + minute
                    # Include hour+minute so same rubric at different times fires independently
                    # (e.g. animal_clip 4x/day, cat_clip 2x/day)
                    slot_key = f"{today_str}_{hour:02d}{minute:02d}_{rubric}"

                    # Already published or permanently skipped?
                    if slot_key in self._published_today:
                        continue

                    # Is it time? (within a 2-minute window)
                    if slot_start_minutes <= now_total_minutes < slot_start_minutes + 2:
                        logger.info(f"⏰ Time to publish: {label} ({rubric})")
                        try:
                            ok = await self._publish_rubric(rubric, label)
                            if not ok:
                                raise RuntimeError(f"{rubric} returned False (nothing published)")
                            self._mark_slot_done(today_str, slot_key, persist=True)
                            self._failed_slots.pop(slot_key, None)  # clear on success
                        except Exception as e:
                            logger.error(f"Failed to publish {rubric}: {e}", exc_info=True)
                            retries = self._failed_slots.get(slot_key, 0) + 1
                            self._failed_slots[slot_key] = retries
                            if retries >= slot_max_retries:
                                msg = (
                                    f"⚠️ Scheduler: рубрика '{label}' ПРОПУЩЕНА сегодня.\n"
                                    f"Причина: {e}\n"
                                    "Проверь env и внешние интеграции (VK/PEXELS/API)."
                                )
                                logger.warning(f"⛔ {rubric}: {retries} failures — marking as SKIPPED for today. Причина: {e}")
                                self._mark_slot_done(today_str, slot_key, persist=False)  # stop retrying
                                asyncio.create_task(self._notify_admins(msg))

                    # Catch up: if bot was down and missed a slot
                    elif slot_start_minutes <= now_total_minutes < slot_start_minutes + slot_catchup_minutes:
                        retries = self._failed_slots.get(slot_key, 0)
                        if retries >= slot_max_retries:
                            continue  # Already gave up on this slot
                        logger.info(f"⏰ Catch-up publish: {label} ({rubric}) [attempt {retries + 1}/{slot_max_retries}]")
                        try:
                            ok = await self._publish_rubric(rubric, label)
                            if not ok:
                                raise RuntimeError(f"{rubric} returned False (nothing published)")
                            self._mark_slot_done(today_str, slot_key, persist=True)
                            self._failed_slots.pop(slot_key, None)
                        except Exception as e:
                            logger.error(f"Failed catch-up {rubric}: {e}", exc_info=True)
                            retries = self._failed_slots.get(slot_key, 0) + 1
                            self._failed_slots[slot_key] = retries
                            if retries >= slot_max_retries:
                                msg = (
                                    f"⚠️ Catch-up: рубрика '{label}' ПРОПУЩЕНА сегодня.\n"
                                    f"Причина: {e}\n"
                                    "Проверь env и внешние интеграции (VK/PEXELS/API)."
                                )
                                logger.warning(
                                    f"⛔ {rubric}: catch-up {retries}/{slot_max_retries} — "
                                    f"SKIPPED for today. Reason: {e}"
                                )
                                self._mark_slot_done(today_str, slot_key, persist=False)  # stop retrying
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
        
        if rubric == "animal_clip":
            try:
                import src.bot as bot_module
                vk = getattr(bot_module, '_vk_publisher', None)
                if not (vk and vk.enabled):
                    raise RuntimeError("animal_clip: VK not configured")

                import os, json, random, time
                from src.media_processor import MediaProcessor
                import aiohttp as _aiohttp
                pexels_key = os.getenv("PEXELS_API_KEY", "").strip()

                if not pexels_key:
                    raise RuntimeError("animal_clip: PEXELS_API_KEY is empty")
                if not getattr(vk, "user_token", ""):
                    raise RuntimeError("animal_clip: VK_USER_TOKEN is empty")

                # ── Load history of used Pexels video IDs ──────────────────
                history_path = os.path.join(self.config.media_dir, "..", "data", "clip_history.json")
                history_path = os.path.normpath(history_path)
                os.makedirs(os.path.dirname(history_path), exist_ok=True)
                try:
                    with open(history_path, "r") as hf:
                        used_ids: list = json.load(hf)
                except (FileNotFoundError, json.JSONDecodeError):
                    used_ids = []

                # ── Pick random animal category ──────────────────────────────
                animal_pools = [
                    ["funny cat", "cute kitten playing"],
                    ["funny dog", "puppy playing"],
                    ["funny animals", "cute pets"],
                    ["baby animals", "cute animal"],
                    ["funny bunny rabbit", "hamster cute"],
                    ["dogs playing", "cats funny"],
                    ["cute kitten", "cat video"],
                    ["adorable puppy", "dog funny"],
                ]
                keywords = random.choice(animal_pools)

                mp = MediaProcessor(
                    pexels_key=pexels_key,
                    media_dir=self.config.media_dir,
                )

                logger.info(f"animal_clip: searching Pexels HD videos for {keywords} (excluding {len(used_ids)} used)")
                result = await mp.search_pexels_video(
                    keywords,
                    min_duration=5,
                    max_duration=40,
                    exclude_ids=used_ids,
                    max_pages=4,
                )

                if not result:
                    # Try generic fallback if specific query failed
                    logger.info("animal_clip: retry with generic 'cute animals'")
                    result = await mp.search_pexels_video(
                        ["cute animals"],
                        min_duration=5,
                        max_duration=40,
                        exclude_ids=used_ids,
                        max_pages=4,
                    )

                if not result:
                    # Final fallback: allow repeats to avoid empty slot
                    logger.info("animal_clip: retry with repeats allowed")
                    result = await mp.search_pexels_video(
                        ["cute animals"],
                        min_duration=5,
                        max_duration=60,
                        exclude_ids=[],
                        max_pages=2,
                    )

                if not result:
                    raise RuntimeError("animal_clip: no HD videos found on Pexels")

                pexels_vid_id, v_url = result

                # ── Download video with retry on 403 ─────────────────────────
                tmp_path = os.path.join(
                    self.config.media_dir,
                    f"animal_clip_{int(time.time())}.mp4"
                )
                dl_headers = {
                    "Authorization": os.getenv("PEXELS_API_KEY", ""),
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                    "Referer": "https://www.pexels.com/",
                    "Accept": "video/mp4,video/*;q=0.9,*/*;q=0.8",
                }
                download_ok = False
                for _dl_try in range(3):
                    async with _aiohttp.ClientSession(headers=dl_headers) as sess:
                        async with sess.get(v_url, timeout=_aiohttp.ClientTimeout(total=90)) as resp:
                            if resp.status == 200:
                                with open(tmp_path, "wb") as f:
                                    f.write(await resp.read())
                                download_ok = True
                                break
                            elif resp.status in (403, 404):
                                logger.warning(f"animal_clip: video {resp.status} id={pexels_vid_id}, trying next video")
                                used_ids.append(pexels_vid_id)
                                retry_result = await mp.search_pexels_video(
                                    keywords, min_duration=5, max_duration=40, exclude_ids=used_ids
                                )
                                if not retry_result:
                                    break
                                pexels_vid_id, v_url = retry_result
                            else:
                                logger.error(f"animal_clip: download failed HTTP {resp.status}")
                                break
                if not download_ok:
                    raise RuntimeError("animal_clip: all download attempts failed")


                file_mb = os.path.getsize(tmp_path) / 1024 / 1024
                logger.info(f"animal_clip: downloaded {file_mb:.1f} MB id={pexels_vid_id}")

                # ── Upload to VK as Clip ─────────────────────────────────────
                captions = [
                    "🐾 Позитивный момент дня",
                    "🐱 Котики заряжают!",
                    "🐶 Хорошего настроения, Ижевск!",
                    "🐾 Доза позитива",
                    "😄 Смотришь и улыбаешься",
                    "🐱 Они просто наслаждаются жизнью",
                    "🐶 Лучший контент в интернете",
                    "🐾 Просто посмотри",
                    "😻 Ну как тут не улыбнуться?",
                ]
                caption = random.choice(captions) + "\n\n📱 @IzhevskTodayNews"

                clip_id = await vk.upload_clip(
                    tmp_path,
                    caption=caption,
                    link_url="https://vk.com/izhevsk_segodnya",
                )

                # ── Cleanup ──────────────────────────────────────────────────
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass

                if clip_id:
                    # Save used ID to history (keep last 500)
                    used_ids.append(pexels_vid_id)
                    if len(used_ids) > 500:
                        used_ids = used_ids[-500:]
                    try:
                        with open(history_path, "w") as hf:
                            json.dump(used_ids, hf)
                    except Exception as he:
                        logger.warning(f"animal_clip: failed to save history: {he}")

                    logger.info(f"✅ VK Animal Clip published (video_id={clip_id}, pexels_id={pexels_vid_id})")
                    return True
                else:
                    raise RuntimeError("animal_clip: upload_clip returned None")

            except Exception as e:
                logger.error(f"animal_clip failed: {e}", exc_info=True)
                raise RuntimeError(f"animal_clip failed: {e}") from e

        if rubric == "cat_clip":
            """10:30 и 21:00 — VK Клип только с котиками (Pexels Video)."""
            try:
                import src.bot as bot_module
                vk = getattr(bot_module, '_vk_publisher', None)
                if not (vk and vk.enabled):
                    logger.info("cat_clip skipped: VK not configured")
                    return False

                import os, json, random, time
                from src.media_processor import MediaProcessor
                import aiohttp as _aiohttp
                pexels_key = os.getenv("PEXELS_API_KEY", "").strip()

                if not pexels_key:
                    logger.error("cat_clip: PEXELS_API_KEY is empty; cannot fetch videos")
                    return False
                if not getattr(vk, "user_token", ""):
                    logger.error("cat_clip: VK_USER_TOKEN is empty; cannot upload VK Clips")
                    return False

                # ── История просмотренных ID (отдельный файл от animal_clip) ──
                history_path = os.path.normpath(
                    os.path.join(self.config.media_dir, "..", "data", "cat_clip_history.json")
                )
                os.makedirs(os.path.dirname(history_path), exist_ok=True)
                try:
                    with open(history_path, "r") as hf:
                        used_ids: list = json.load(hf)
                except (FileNotFoundError, json.JSONDecodeError):
                    used_ids = []

                # ── Только запросы с котиками ────────────────────────────────
                cat_pools = [
                    ["funny cat", "kitten playing"],
                    ["cute kitten", "cat funny"],
                    ["cat video", "kitten cute"],
                    ["funny kitten", "cat silly"],
                    ["cats playing", "kittens"],
                ]
                keywords = random.choice(cat_pools)

                mp = MediaProcessor(
                    pexels_key=pexels_key,
                    media_dir=self.config.media_dir,
                )

                logger.info(f"cat_clip: searching Pexels for {keywords} (excluding {len(used_ids)} used)")
                result = await mp.search_pexels_video(
                    keywords,
                    min_duration=5,
                    max_duration=45,
                    exclude_ids=used_ids,
                    max_pages=4,
                )
                if not result:
                    result = await mp.search_pexels_video(
                        ["cats", "kittens"],
                        min_duration=5,
                        max_duration=60,
                        exclude_ids=used_ids,
                        max_pages=4,
                    )
                if not result:
                    logger.info("cat_clip: retry with repeats allowed")
                    result = await mp.search_pexels_video(
                        ["cats", "kittens"],
                        min_duration=5,
                        max_duration=90,
                        min_quality_px=720,
                        exclude_ids=[],
                        max_pages=2,
                    )
                if not result:
                    logger.warning("cat_clip: no video found on Pexels, skipping")
                    return False

                pexels_vid_id, v_url = result

                # ── Скачиваем видео с retry на 403 ───────────────────────────
                tmp_path = os.path.join(
                    self.config.media_dir,
                    f"cat_clip_{int(time.time())}.mp4"
                )
                dl_headers = {
                    "Authorization": os.getenv("PEXELS_API_KEY", ""),
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                    "Referer": "https://www.pexels.com/",
                    "Accept": "video/mp4,video/*;q=0.9,*/*;q=0.8",
                }
                download_ok = False
                for _dl_try in range(3):
                    async with _aiohttp.ClientSession(headers=dl_headers) as sess:
                        async with sess.get(v_url, timeout=_aiohttp.ClientTimeout(total=90)) as resp:
                            if resp.status == 200:
                                with open(tmp_path, "wb") as f:
                                    f.write(await resp.read())
                                download_ok = True
                                break
                            elif resp.status in (403, 404):
                                logger.warning(f"cat_clip: video {resp.status} id={pexels_vid_id}, trying next video")
                                used_ids.append(pexels_vid_id)
                                retry_result = await mp.search_pexels_video(
                                    keywords, min_duration=5, max_duration=45, exclude_ids=used_ids
                                )
                                if not retry_result:
                                    break
                                pexels_vid_id, v_url = retry_result
                            else:
                                logger.error(f"cat_clip: download failed HTTP {resp.status}")
                                break
                if not download_ok:
                    logger.error("cat_clip: all download attempts failed, skipping")
                    return False

                file_mb = os.path.getsize(tmp_path) / 1024 / 1024
                logger.info(f"cat_clip: downloaded {file_mb:.1f} MB id={pexels_vid_id}")

                # ── Загружаем в VK Клипы ─────────────────────────────────────
                captions = [
                    "😸 Котики — лучшее лекарство",
                    "🐱 Просто котик. Просто хорошо.",
                    "😹 Ну как тут не улыбнуться?",
                    "🐾 Котики заряжают позитивом!",
                    "😻 Они просто наслаждаются жизнью",
                    "🐱 Смотришь — и день стал лучше",
                    "😸 Минута позитива с котиком",
                    "🐾 Лучший контент в интернете — факт!",
                ]
                caption = random.choice(captions) + "\n\n📱 @IzhevskTodayNews"

                clip_id = await vk.upload_clip(
                    tmp_path,
                    caption=caption,
                    link_url="https://vk.com/izhevsk_segodnya",
                )

                # ── Чистим файл ───────────────────────────────────────────────
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass

                if clip_id:
                    used_ids.append(pexels_vid_id)
                    if len(used_ids) > 500:
                        used_ids = used_ids[-500:]
                    try:
                        with open(history_path, "w") as hf:
                            json.dump(used_ids, hf)
                    except Exception as he:
                        logger.warning(f"cat_clip: failed to save history: {he}")
                    logger.info(f"✅ VK Cat Clip published (video_id={clip_id}, pexels_id={pexels_vid_id})")
                    return True
                else:
                    logger.warning("cat_clip: upload_clip returned None")
                    return False

            except Exception as e:
                logger.error(f"cat_clip failed: {e}", exc_info=True)
            return False

        if rubric == "cat_story":
            try:
                import src.bot as bot_module
                vk = getattr(bot_module, '_vk_publisher', None)
                if vk and vk.enabled:
                    if not hasattr(self, 'story_generator'):
                        from src.story_generator import StoryGenerator
                        self.story_generator = StoryGenerator()
                    story_bytes = await self.story_generator.generate_cat_story(hour=self._now().hour)
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

                    # 2. Get a stock video — use PEXELS_PROXY from env if set
                    from src.media_processor import MediaProcessor
                    import os
                    mp = MediaProcessor(
                        pexels_key=os.getenv("PEXELS_API_KEY", ""),
                        media_dir=self.config.media_dir,
                    )
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
                    # Pluck first meaningful sentence as short caption
                    sentences = re.split(r'(?<=[.!?]) +', text.replace('\n', ' ')) if text else []
                    # Take first non-empty sentence ≥ 10 chars; fallback to full first 100 chars
                    short_text = next(
                        (s for s in sentences if len(s) >= 10),
                        text[:100] if text else "Ижевск — город тружеников!"
                    )
                    
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

        # ── Photo deduplication: if the same URL was already used today, try to
        # pick a different photo so consecutive posts look visually distinct.
        if photo_url and photo_url in self._used_photo_urls:
            logger.warning(
                f"{rubric}: photo URL already used today — searching for alternative"
            )
            try:
                import random
                alt_quals = [
                    ["city", "russia", "nature", "architecture"],
                    ["beautiful", "landscape", "russia"],
                    ["street", "urban", "russia"],
                    ["nature", "scenic", "view", "russia"]
                ]
                alt_photos = await self.generator._media.search_stock_photo(
                    random.choice(alt_quals), count=25
                )
                random.shuffle(alt_photos)
                for candidate in alt_photos:
                    alt_url = candidate.get("url", "")
                    if alt_url and alt_url not in self._used_photo_urls:
                        photo_url = alt_url
                        logger.info(f"{rubric}: alternative photo found: {alt_url[:60]}")
                        break
                else:
                    logger.warning(f"{rubric}: no unique alternative photo found, reusing existing")
            except Exception as dedup_err:
                logger.warning(f"{rubric}: photo dedup search failed: {dedup_err}")

        if photo_url:
            self._used_photo_urls.add(photo_url)
            self._save_photo_history({photo_url})

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
                    
                    # ── Publish VK Story for ALL text-based rubrics ──
                    # Map scheduler rubric names → story theme names
                    _RUBRIC_TO_THEME = {
                        "weather": "weather",
                        "history_fact": "history",
                        "five_facts": "five_facts",
                        "recipe": "recipe",
                        "lifehack": "lifehack",
                        "place": "place",
                        "evening_fun": "evening",
                        "daily_digest": "digest",
                        "holiday": "holiday",
                    }
                    theme_name = _RUBRIC_TO_THEME.get(rubric)
                    if theme_name and text:
                        try:
                            if not hasattr(self, 'story_generator'):
                                from src.story_generator import StoryGenerator
                                self.story_generator = StoryGenerator()
                            # Extract headline from the FIRST LINE of the post.
                            # Sentence-based extraction picks up "1." from numbered lists
                            # (e.g. "5 фактов ... которые вы не знали  1."), so we
                            # split by newlines and take the first non-empty line instead.
                            import re as _re2
                            clean_txt = _re2.sub(r'<[^>]+>', '', text)  # strip HTML
                            clean_txt = _re2.sub(r'#\S+', '', clean_txt).strip()
                            # Remove leading emoji-chars that might appear alone on a line
                            clean_txt = _re2.sub(r'^[\U0001F300-\U0001FAFF\s]+\n', '', clean_txt, flags=_re2.MULTILINE)
                            lines = [l.strip() for l in clean_txt.splitlines() if l.strip()]
                            headline = lines[0] if lines else clean_txt[:120]
                            # Strip numbered-list start if it leaked into headline (e.g. "Title  1.")
                            headline = _re2.sub(r'\s+\d+\.\s*$', '', headline).strip()
                            if len(headline) > 120:
                                headline = headline[:117] + "..."
                            
                            if len(headline) > 15:
                                story_bytes = await self.story_generator.generate_rubric_story(
                                    headline, rubric=theme_name, photo_url=photo_url
                                )
                                if story_bytes:
                                    s_res = await vk.upload_story_photo(
                                        story_bytes,
                                        link_text="learn_more",
                                        link_url="https://vk.com/izhevsk_segodnya"
                                    )
                                    if s_res:
                                        logger.info(f"✅ VK {label} Story published")
                                    else:
                                        logger.warning(f"VK {label} Story upload failed")
                        except Exception as s_err:
                            logger.error(f"Failed to publish VK story for {rubric}: {s_err}", exc_info=True)
                            
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

            return True

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
