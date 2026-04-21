"""
Transfer Content Scheduler — публикует тематические посты для VK vk.com/grandtransfer.
Запускается как отдельный процесс (grandtransfer-bot контейнер).

Расписание (Московское время, UTC+3):
  09:00 — Промо/акция
  11:00 — Маршрут дня
  13:00 — Совет пассажиру
  15:30 — Минивэн
  18:00 — Вечерняя акция
  20:00 — FAQ / Вопрос-ответ
"""

import asyncio
import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Optional, Tuple

import aiohttp

from src.transfer_content import (
    generate_promo, generate_route_tip, generate_travel_tip,
    generate_minivan, generate_faq, reset_daily,
)

logger = logging.getLogger(__name__)

TZ_MSK = timezone(timedelta(hours=3))

TRANSFER_SCHEDULE = [
    (9,  0,  "promo",      "🚕 Реклама/акция"),
    (11, 0,  "route_tip",  "🗺 Маршрут дня"),
    (13, 0,  "travel_tip", "💡 Совет пассажиру"),
    (15, 30, "minivan",    "🚐 Минивэн"),
    (18, 0,  "promo",      "🚕 Вечерняя акция"),
    (20, 0,  "faq",        "❓ FAQ"),
]


class SimpleVKPublisher:
    """Минимальный VK API клиент для постинга на стену группы."""

    def __init__(self, access_token: str, group_id: str):
        self.access_token = access_token
        self.group_id = group_id
        self.enabled = bool(access_token and group_id)

    async def post(self, text: str, photo_url: Optional[str] = None) -> Optional[int]:
        if not self.enabled:
            return None
        params = {
            "owner_id": f"-{self.group_id}",
            "from_group": 1,
            "message": text,
            "v": "5.199",
            "access_token": self.access_token,
        }
        if photo_url:
            try:
                attachment = await self._upload_photo(photo_url)
                if attachment:
                    params["attachments"] = attachment
            except Exception as e:
                logger.warning(f"Photo upload failed, text-only: {e}")
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    "https://api.vk.com/method/wall.post",
                    data=params,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    data = await resp.json()
                    if "response" in data:
                        return data["response"]["post_id"]
                    logger.error(f"VK wall.post error: {data}")
        except Exception as e:
            logger.error(f"VK request failed: {e}")
        return None

    async def _upload_photo(self, photo_url: str) -> Optional[str]:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://api.vk.com/method/photos.getWallUploadServer",
                params={"group_id": self.group_id, "v": "5.199",
                        "access_token": self.access_token},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as r:
                upload_url = (await r.json())["response"]["upload_url"]

            headers = {"User-Agent": "GrandTransferBot/1.0"}
            async with session.get(photo_url, headers=headers,
                                   timeout=aiohttp.ClientTimeout(total=20)) as r:
                if r.status != 200:
                    raise ValueError(f"Photo download HTTP {r.status}")
                img_bytes = await r.read()

            form = aiohttp.FormData()
            form.add_field("photo", img_bytes, filename="photo.jpg",
                           content_type="image/jpeg")
            async with session.post(upload_url, data=form,
                                    timeout=aiohttp.ClientTimeout(total=30)) as r:
                up = await r.json()

            async with session.post(
                "https://api.vk.com/method/photos.saveWallPhoto",
                params={"group_id": self.group_id, "server": up["server"],
                        "photo": up["photo"], "hash": up["hash"],
                        "v": "5.199", "access_token": self.access_token},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as r:
                saved = (await r.json())["response"][0]
                return f"photo{saved['owner_id']}_{saved['id']}"


class TransferScheduler:
    """Планировщик VK-постов для группы grandtransfer."""

    def __init__(self, vk: SimpleVKPublisher, ai_key: str, pexels_key: str):
        self.vk = vk
        self.ai_key = ai_key
        self.pexels_key = pexels_key
        self._published_today: set = set()
        self._last_date: Optional[str] = None
        self._failed: dict = {}

    def _now(self) -> datetime:
        return datetime.now(TZ_MSK)

    async def _generate(self, rubric: str) -> Tuple[str, Optional[str]]:
        generators = {
            "promo": generate_promo,
            "route_tip": generate_route_tip,
            "travel_tip": generate_travel_tip,
            "minivan": generate_minivan,
            "faq": generate_faq,
        }
        fn = generators.get(rubric)
        if not fn:
            raise ValueError(f"Unknown rubric: {rubric}")
        return await fn(self.ai_key, self.pexels_key)

    async def _tick(self) -> None:
        now = self._now()
        today = now.strftime("%Y-%m-%d")

        if self._last_date != today:
            self._published_today.clear()
            self._failed.clear()
            reset_daily()
            self._last_date = today
            logger.info(f"New day: {today}")

        MAX_RETRIES = 2
        for hour, minute, rubric, label in TRANSFER_SCHEDULE:
            slot = f"{today}_{hour:02d}{minute:02d}_{rubric}"
            if slot in self._published_today:
                continue
            in_window = now.hour == hour and minute <= now.minute < minute + 2
            catch_up = now.hour == hour and minute <= now.minute < minute + 30
            if not (in_window or catch_up):
                continue
            if self._failed.get(slot, 0) >= MAX_RETRIES:
                continue

            logger.info(f"⏰ {label}")
            try:
                text, photo = await self._generate(rubric)
                post_id = await self.vk.post(text, photo)
                if post_id:
                    self._published_today.add(slot)
                    self._failed.pop(slot, None)
                    logger.info(f"✅ {label} (post_id={post_id})")
                else:
                    raise RuntimeError("VK returned None")
            except Exception as e:
                retries = self._failed.get(slot, 0) + 1
                self._failed[slot] = retries
                logger.error(f"❌ {rubric} attempt {retries}: {e}")
                if retries >= MAX_RETRIES:
                    self._published_today.add(slot)
                    logger.warning(f"⛔ {rubric} skipped for today")

    async def run(self) -> None:
        logger.info(
            f"TransferScheduler started | VK group {self.vk.group_id} "
            f"enabled={self.vk.enabled}"
        )
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Scheduler error: {e}", exc_info=True)
            await asyncio.sleep(30)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    vk_token = os.environ.get("VK_ACCESS_TOKEN_GT", "")
    vk_group_id = os.environ.get("VK_GROUP_ID_GT", "218903564")
    ai_key = os.environ.get("AITUNNEL_API_KEY", "")
    pexels_key = os.environ.get("PEXELS_API_KEY", "")

    if not vk_token:
        logger.error("VK_ACCESS_TOKEN_GT not set — exiting")
        return

    vk = SimpleVKPublisher(vk_token, vk_group_id)
    scheduler = TransferScheduler(vk, ai_key, pexels_key)
    await scheduler.run()
