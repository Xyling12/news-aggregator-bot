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
    gemini_api_keys: List[str] = field(default_factory=list)  # All keys for fallback
    gemini_model_names: List[str] = field(default_factory=list)

    # Source channels to monitor
    source_channels: List[str] = field(default_factory=list)

    # Optional: Stock photo APIs
    pixabay_api_key: str = ""       # Primary (Russian language support)
    unsplash_access_key: str = ""   # Fallback

    # Optional: YandexGPT API
    yandex_api_key: str = ""
    yandex_folder_id: str = ""

    # Optional: Groq API
    groq_api_key: str = ""
    groq_model: str = "llama-3.3-70b-versatile"

    # Optional: AITUNNEL (OpenAI-compatible proxy, primary engine)
    aitunnel_api_key: str = ""
    aitunnel_model: str = "gpt-4o-mini"

    # Optional: ReText.AI API
    retext_api_key: str = ""

    # Optional: OpenWeatherMap API
    openweather_api_key: str = ""

    # Optional: Yandex Weather API (free 50 req/day, Russian conditions)
    yandex_weather_api_key: str = ""

    # Optional: VK crossposting
    vk_access_token: str = ""     # Group token — for wall posts
    vk_user_token: str = ""       # User token — for photo uploads (needs 'photos' scope)
    vk_group_id: str = ""
    vk_seo_enabled: bool = True
    vk_seo_max_tags: int = 9
    vk_self_comment_enabled: bool = False
    use_source_media: bool = True
    vk_competitor_commenting_enabled: bool = False
    vk_competitor_targets: List[str] = field(default_factory=list)
    vk_competitor_keywords: List[str] = field(default_factory=list)
    vk_competitor_comments_per_day: int = 2
    vk_competitor_min_gap_minutes: int = 480
    vk_competitor_scan_limit: int = 6

    # Optional: MAX crossposting
    max_bot_token: str = ""
    max_chat_id: str = ""

    # Optional: Pexels API (better Russian stock photos)
    pexels_api_key: str = ""

    # Settings
    min_text_length: int = 100
    check_interval: int = 60
    language: str = "ru"
    publish_interval: int = 900  # Auto-publish interval in seconds (default 15min)
    auto_publish: bool = False   # Skip moderation and auto-approve all posts
    publish_max_per_day: int = 14   # Daily cap on auto-published news (0 = unlimited)
    publish_active_start: int = 7   # Prime-time window start hour (Izhevsk UTC+4)
    publish_active_end: int = 23    # Prime-time window end hour (exclusive)

    # Ad filter stop-words
    ad_stop_words: List[str] = field(default_factory=lambda: [
        "реклама", "промокод", "скидка", "акция", "партнёрский",
        "переходи по ссылке", "подписывайся", "розыгрыш", "конкурс",
        "заработок", "бесплатно", "жми", "переходи", "регистрируйся",
        "p.s. реклама", "на правах рекламы", "erid", "#реклама",
        "#промо", "#ad", "оплаченная публикация",
    ])

    # Low-value content filter (weather, horoscopes, astrology, etc.)
    lowvalue_stop_words: List[str] = field(default_factory=lambda: [
        "погода", "прогноз погоды", "температура воздуха", "облачно",
        "осадки", "давление мм", "ветер м/с", "гороскоп",
        "знак зодиака", "лунный календарь", "цитата дня",
        "утренняя зарядка", "доброе утро", "с добрым утром",
        # Astrology & mysticism
        "астролог", "предсказание астролог", "предупреждение астролог",
        "лунное затмение", "меркурий ретроград", "полнолуние",
        "энергетика дня", "коридор затмений", "портал связан",
        "ясновидящий", "гадание", "таромант", "магия", "заговор",
    ])

    # Breaking news keywords (auto-publish without moderation)
    breaking_keywords: List[str] = field(default_factory=lambda: [
        "срочно", "молния", "breaking", "экстренно", "внимание",
        "чрезвычайная ситуация", "чс ", "эвакуация", "теракт",
        "землетрясение", "наводнение", "объявлена тревога",
        # Drone / air defense alerts (critical for Udmurtia residents)
        "беспилотная опасность", "воздушная тревога", "угроза бпла",
        "сигнал бпла", "отбой тревог", "отмена режима", "ракетная опасность",
        "объявлен сигнал", "режим повышенной", "введён режим",
    ])

    # Paths
    db_path: str = "data/bot.db"
    media_dir: str = "media"
    session_name: str = "news_bot_session"

    @classmethod
    def from_env(cls) -> "Config":
        """Create Config from environment variables."""
        def _split_csv(name: str) -> List[str]:
            raw = os.getenv(name, "")
            return [x.strip() for x in raw.split(",") if x.strip()]

        def _env_bool(name: str, default: bool) -> bool:
            raw = os.getenv(name)
            if raw is None:
                return default
            return raw.strip().lower() in ("1", "true", "yes", "on")

        # Prime-time window, e.g. PUBLISH_ACTIVE_HOURS="7-23" (Izhevsk local time)
        _active_start, _active_end = 7, 23
        try:
            _raw_hours = os.getenv("PUBLISH_ACTIVE_HOURS", "7-23").replace(" ", "")
            _s, _e = _raw_hours.split("-")
            _active_start = max(0, min(23, int(_s)))
            _active_end = max(1, min(24, int(_e)))
        except Exception:
            _active_start, _active_end = 7, 23

        admin_ids_raw = os.getenv("ADMIN_IDS", "")
        admin_ids = [int(x.strip()) for x in admin_ids_raw.split(",") if x.strip()]

        source_channels = _split_csv("SOURCE_CHANNELS")

        gemini_model_names = _split_csv("GEMINI_MODEL_NAMES")

        # Support multiple Gemini keys: GEMINI_API_KEYS=key1,key2 or fallback to GEMINI_API_KEY
        gemini_api_keys_raw = os.getenv("GEMINI_API_KEYS", "")
        if gemini_api_keys_raw:
            gemini_api_keys = [k.strip() for k in gemini_api_keys_raw.split(",") if k.strip()]
        else:
            single_key = os.getenv("GEMINI_API_KEY", "")
            gemini_api_keys = [single_key] if single_key else []

        return cls(
            api_id=int(os.getenv("TELEGRAM_API_ID", "0")),
            api_hash=os.getenv("TELEGRAM_API_HASH", ""),
            bot_token=os.getenv("BOT_TOKEN", ""),
            target_channel=os.getenv("TARGET_CHANNEL", ""),
            admin_ids=admin_ids,
            gemini_api_key=gemini_api_keys[0] if gemini_api_keys else "",
            gemini_api_keys=gemini_api_keys,
            gemini_model_names=gemini_model_names,
            source_channels=source_channels,
            pixabay_api_key=os.getenv("PIXABAY_API_KEY", ""),
            unsplash_access_key=os.getenv("UNSPLASH_ACCESS_KEY", ""),
            yandex_api_key=os.getenv("YANDEX_API_KEY", ""),
            yandex_folder_id=os.getenv("YANDEX_FOLDER_ID", ""),
            groq_api_key=os.getenv("GROQ_API_KEY", ""),
            groq_model=os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile"),
            aitunnel_api_key=os.getenv("AITUNNEL_API_KEY", ""),
            aitunnel_model=os.getenv("AITUNNEL_MODEL", "gpt-4o-mini"),
            retext_api_key=os.getenv("RETEXT_API_KEY", ""),
            openweather_api_key=os.getenv("OPENWEATHER_API_KEY", ""),
            yandex_weather_api_key=os.getenv("YANDEX_WEATHER_API_KEY", ""),
            vk_access_token=os.getenv("VK_ACCESS_TOKEN", ""),
            vk_user_token=os.getenv("VK_USER_TOKEN", ""),
            vk_group_id=os.getenv("VK_GROUP_ID", ""),
            vk_seo_enabled=_env_bool("VK_SEO_ENABLED", True),
            vk_seo_max_tags=max(3, min(15, int(os.getenv("VK_SEO_MAX_TAGS", "9")))),
            vk_self_comment_enabled=_env_bool("VK_SELF_COMMENT_ENABLED", False),
            use_source_media=_env_bool("USE_SOURCE_MEDIA", True),
            vk_competitor_commenting_enabled=_env_bool("VK_COMPETITOR_COMMENTING_ENABLED", False),
            vk_competitor_targets=_split_csv("VK_COMPETITOR_TARGETS"),
            vk_competitor_keywords=_split_csv("VK_COMPETITOR_KEYWORDS"),
            vk_competitor_comments_per_day=max(
                1, min(5, int(os.getenv("VK_COMPETITOR_COMMENTS_PER_DAY", "2")))
            ),
            vk_competitor_min_gap_minutes=max(
                30, int(os.getenv("VK_COMPETITOR_MIN_GAP_MINUTES", "480"))
            ),
            vk_competitor_scan_limit=max(
                3, min(20, int(os.getenv("VK_COMPETITOR_SCAN_LIMIT", "6")))
            ),
            max_bot_token=os.getenv("MAX_BOT_TOKEN", ""),
            max_chat_id=os.getenv("MAX_CHAT_ID", ""),
            pexels_api_key=os.getenv("PEXELS_API_KEY", ""),
            min_text_length=int(os.getenv("MIN_TEXT_LENGTH", "100")),
            check_interval=int(os.getenv("CHECK_INTERVAL", "60")),
            language=os.getenv("LANGUAGE", "ru"),
            publish_interval=int(os.getenv("PUBLISH_INTERVAL", "900")),
            auto_publish=os.getenv("AUTO_PUBLISH", "false").lower() == "true",
            publish_max_per_day=max(0, int(os.getenv("PUBLISH_MAX_PER_DAY", "14"))),
            publish_active_start=_active_start,
            publish_active_end=_active_end,
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
        val = await db.get_setting("auto_publish")
        if val is not None:
            self.auto_publish = val.lower() == "true"
