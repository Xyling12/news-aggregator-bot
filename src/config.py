"""
Configuration module — loads environment variables and provides typed settings.
"""

import os
from dataclasses import dataclass, field
from typing import List

from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    """Application configuration loaded from environment variables."""

    # Telegram API (Telethon)
    api_id: int = 0
    api_hash: str = ""

    # Telegram Bot (Aiogram)
    bot_token: str = ""

    # Target channel for publishing
    target_channel: str = ""

    # Admin user IDs
    admin_ids: List[int] = field(default_factory=list)

    # Google Gemini API
    gemini_api_key: str = ""

    # Source channels to monitor
    source_channels: List[str] = field(default_factory=list)

    # Optional: Unsplash API
    unsplash_access_key: str = ""

    # Optional: YandexGPT API
    yandex_api_key: str = ""
    yandex_folder_id: str = ""

    # Optional: ReText.AI API
    retext_api_key: str = ""

    # Optional: OpenWeatherMap API
    openweather_api_key: str = ""

    # Optional: VK crossposting
    vk_access_token: str = ""
    vk_group_id: str = ""

    # Optional: Pexels API (better Russian stock photos)
    pexels_api_key: str = ""

    # Settings
    min_text_length: int = 100
    check_interval: int = 60
    language: str = "ru"
    publish_interval: int = 7200  # Auto-publish interval in seconds (default 2h)

    # Ad filter stop-words
    ad_stop_words: List[str] = field(default_factory=lambda: [
        "реклама", "промокод", "скидка", "акция", "партнёрский",
        "переходи по ссылке", "подписывайся", "розыгрыш", "конкурс",
        "заработок", "бесплатно", "жми", "переходи", "регистрируйся",
        "p.s. реклама", "на правах рекламы", "erid", "#реклама",
        "#промо", "#ad", "оплаченная публикация",
    ])

    # Politics filter stop-words
    politics_stop_words: List[str] = field(default_factory=lambda: [
        "выборы", "голосование", "кандидат", "депутат", "партия",
        "единая россия", "лдпр", "кпрф", "справедливая россия",
        "госдума", "государственная дума", "совет федерации",
        "политик", "оппозиция", "митинг", "протест", "санкции",
        "геополитик", "закон о", "законопроект", "путин", "президент",
        "губернатор", "мэр ", "администрация города", "правительство",
        "парламент", "фракция", "импичмент", "референдум",
        "предвыборн", "агитация", "пропаганда",
    ])

    # Low-value content filter (weather, horoscopes, etc.)
    lowvalue_stop_words: List[str] = field(default_factory=lambda: [
        "погода", "прогноз погоды", "температура воздуха", "облачно",
        "осадки", "давление мм", "ветер м/с", "гороскоп",
        "знак зодиака", "лунный календарь", "цитата дня",
        "утренняя зарядка", "доброе утро", "с добрым утром",
    ])

    # Breaking news keywords (auto-publish without moderation)
    breaking_keywords: List[str] = field(default_factory=lambda: [
        "срочно", "молния", "breaking", "экстренно", "внимание",
        "чрезвычайная ситуация", "чс ", "эвакуация", "теракт",
        "землетрясение", "наводнение", "объявлена тревога",
    ])

    # Paths
    db_path: str = "data/bot.db"
    media_dir: str = "media"
    session_name: str = "news_bot_session"

    @classmethod
    def from_env(cls) -> "Config":
        """Create Config from environment variables."""
        admin_ids_raw = os.getenv("ADMIN_IDS", "")
        admin_ids = [int(x.strip()) for x in admin_ids_raw.split(",") if x.strip()]

        source_channels_raw = os.getenv("SOURCE_CHANNELS", "")
        source_channels = [x.strip() for x in source_channels_raw.split(",") if x.strip()]

        return cls(
            api_id=int(os.getenv("TELEGRAM_API_ID", "0")),
            api_hash=os.getenv("TELEGRAM_API_HASH", ""),
            bot_token=os.getenv("BOT_TOKEN", ""),
            target_channel=os.getenv("TARGET_CHANNEL", ""),
            admin_ids=admin_ids,
            gemini_api_key=os.getenv("GEMINI_API_KEY", ""),
            source_channels=source_channels,
            unsplash_access_key=os.getenv("UNSPLASH_ACCESS_KEY", ""),
            yandex_api_key=os.getenv("YANDEX_API_KEY", ""),
            yandex_folder_id=os.getenv("YANDEX_FOLDER_ID", ""),
            retext_api_key=os.getenv("RETEXT_API_KEY", ""),
            openweather_api_key=os.getenv("OPENWEATHER_API_KEY", ""),
            vk_access_token=os.getenv("VK_ACCESS_TOKEN", ""),
            vk_group_id=os.getenv("VK_GROUP_ID", ""),
            pexels_api_key=os.getenv("PEXELS_API_KEY", ""),
            min_text_length=int(os.getenv("MIN_TEXT_LENGTH", "100")),
            check_interval=int(os.getenv("CHECK_INTERVAL", "60")),
            language=os.getenv("LANGUAGE", "ru"),
            publish_interval=int(os.getenv("PUBLISH_INTERVAL", "7200")),
        )

    def validate(self) -> List[str]:
        """Validate that all required settings are present. Returns list of error messages."""
        errors = []
        if not self.bot_token:
            errors.append("BOT_TOKEN is required")
        if not self.target_channel:
            errors.append("TARGET_CHANNEL is required")
        if not self.admin_ids:
            errors.append("ADMIN_IDS is required (at least one admin)")
        if not self.gemini_api_key:
            errors.append("GEMINI_API_KEY is required")
        if not self.source_channels:
            errors.append("SOURCE_CHANNELS is required (at least one channel)")
        return errors

    async def reload_from_db(self, db) -> None:
        """Reload editable settings from the database (applied without restart)."""
        val = await db.get_setting("publish_interval")
        if val:
            self.publish_interval = int(val)
        val = await db.get_setting("check_interval")
        if val:
            self.check_interval = int(val)
        val = await db.get_setting("min_text_length")
        if val:
            self.min_text_length = int(val)
