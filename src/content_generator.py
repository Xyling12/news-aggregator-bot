"""
Content Generator — generates unique daily content (weather, facts, recipes, etc.)
using Gemini AI and external APIs. Each post includes a stock photo.
"""

import asyncio
import logging
import random
import re
from datetime import datetime
from typing import Optional, List, Tuple

import aiohttp
import google.generativeai as genai

try:
    from google.generativeai.types import HarmCategory, HarmBlockThreshold

    SAFETY_SETTINGS = {
        HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
        HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
        HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
        HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
    }
except ImportError:
    SAFETY_SETTINGS = None

from src.config import Config

logger = logging.getLogger(__name__)

# ── Human-like writing style instruction (added to ALL prompts) ──────────

HUMAN_STYLE = """
СТИЛЬ НАПИСАНИЯ — ЭТО КРИТИЧЕСКИ ВАЖНО:
- Пиши НЕФОРМАЛЬНО, как реальный человек в Телеграме, а НЕ как робот
- Используй разговорные выражения: "кстати", "между прочим", "ну и конечно"
- Иногда ставь многоточие... для паузы
- Допускай лёгкий юмор и иронию
- НЕ используй канцелярит, сухие формулировки
- НЕ начинай каждый абзац одинаково
- Чередуй длинные и короткие предложения
- Пиши так, будто рассказываешь другу за кофе
- НИКАКИХ "В заключение", "Таким образом", "Следует отметить"
- НЕ используй Markdown заголовки (# ## ###)
"""

# ── Prompt Templates ─────────────────────────────────────────────────────

WEATHER_FORMAT = """Напиши пост о погоде для Telegram-канала "Ижевск Сегодня".

Данные:
- Температура: {temp}°C (ощущается как {feels_like}°C)
- Описание: {description}
- Ветер: {wind} м/с
- Влажность: {humidity}%
- Давление: {pressure} мм рт. ст.

Начни с эмодзи погоды + "Ижевск, {date}"
Кратко опиши — что ожидать (2-3 предложения). Добавь совет что одеть.
Максимум 5 строк. Пиши как обычный человек, не как бот.
""" + HUMAN_STYLE

HISTORY_PROMPT = """Ты — краевед, который обожает Удмуртию.

Расскажи что интересного случилось в Ижевске или Удмуртии {date} (или около этой даты в другие годы).

Начни с 📅 + дата + год + цепляющий заголовок
Расскажи 1 факт подробно (3-4 абзаца), как будто рассказываешь другу.
Выдели **жирным** даты, имена, цифры.
НЕ придумывай несуществующие факты.
""" + HUMAN_STYLE

FIVE_FACTS_PROMPT = """Напиши пост «5 фактов» на тему: {topic}

Начни: 📌 5 фактов о [тема], которые вы не знали
Каждый факт с номером (1️⃣ 2️⃣ 3️⃣ 4️⃣ 5️⃣)
Факты РЕАЛЬНЫЕ. **Жирным** ключевые цифры.
Рассказывай увлекательно, с удивлением — "а вы знали, что..."
""" + HUMAN_STYLE

RECIPE_PROMPT = """Поделись рецептом: {topic}

Начни с 🍽 + название блюда
Коротко расскажи историю блюда (1-2 предложения, с душой)
Ингредиенты — кратко через запятую
Приготовление — 3-5 шагов, просто и понятно
Добавь свой совет — как будто ты сам это готовишь каждую неделю
Упор на удмуртскую кухню.
""" + HUMAN_STYLE

LIFEHACK_PROMPT = """Напиши полезный пост для жителей Ижевска на тему: {topic}

Начни: 💡 + цепляющий заголовок
3-5 конкретных советов с привязкой к Ижевску / Удмуртии
**Жирным** ключевые моменты.
Пиши так, будто советуешь соседу.
""" + HUMAN_STYLE

PLACE_PROMPT = """Расскажи про место в Удмуртии: {topic}

Начни: 📍 + название места
Расскажи коротко историю (1-2 абзаца), почему стоит побывать.
Добавь интересный факт и как добраться.
**Жирным** ключевые детали.
Пиши с любовью к этому месту — как будто сам там был много раз.
""" + HUMAN_STYLE

EVENING_FUN_PROMPT = """Создай {content_type} на тему Ижевска / Удмуртии.

Это должно быть ВЕСЕЛО и ИНТЕРЕСНО.
Начни с подходящего эмодзи.
Максимум 8 строк. Пиши как живой человек с юмором.
""" + HUMAN_STYLE

DIGEST_PROMPT = """Ты — главный редактор канала «Ижевск Сегодня». Напиши вечерний дайджест.

Новости дня:
{news_list}

СТРУКТУРА ПОСТА:

📊 Главное за {date}

[3-5 главных новостей — каждая в 1 коротком предложении с эмодзи по теме. Самую важную ставь первой.]

✏️ Редакция:
[2-3 предложения — живой авторский комментарий: что сегодня запомнилось, какой тренд прослеживается, что это значит для жителей Ижевска. Пиши как умный человек с позицией, а не как нейтральный робот. Можно слегка иронично для бытовых тем.]

ПРАВИЛА:
- НЕ пересказывай новости подробно — только суть в одном предложении
- Редакционный комментарий — это ТВОЯ мысль, не пересказ
- Разговорный тон, никакого канцелярита
- НЕ используй Markdown заголовки (# ## ###)
""" + HUMAN_STYLE

PHOTO_KEYWORDS_PROMPT = """Придумай 2-3 ключевых слова НА АНГЛИЙСКОМ для поиска фото к этому посту.
Слова должны точно описывать визуальный контент поста.
Верни ТОЛЬКО слова через запятую, без объяснений.

Пост: {text}"""


# ── Topic pools ──────────────────────────────────────────────────────────

FIVE_FACTS_TOPICS = [
    "набережной Ижевского пруда", "Ижевском оружейном заводе",
    "удмуртском языке", "Калашникове и Ижевске",
    "удмуртской кухне", "зоопарке Удмуртии",
    "реке Иж", "архитектуре старого Ижевска",
    "удмуртских традициях", "Ижевском цирке",
    "спорте в Удмуртии", "мотоцикле Иж",
    "ледовом городке Ижевска", "культуре Удмуртии",
    "музеях Ижевска", "удмуртском мёде",
    "Ижевских улицах", "парках Ижевска",
    "образовании в Удмуртии", "транспорте Ижевска",
    "промышленности Ижевска", "театрах Ижевска",
    "природе Удмуртии", "знаменитых людях Удмуртии",
    "фестивалях Ижевска", "Камбарке",
    "Воткинске и Чайковском", "Сарапуле",
]

RECIPE_TOPICS = [
    "удмуртские перепечи с мясом",
    "удмуртские табани с каймаком",
    "пельмени по-удмуртски",
    "шаньги с картошкой",
    "удмуртский суп шыд",
    "перепечи с грибами",
    "удмуртская выпечка кокрок",
    "удмуртский кисель из овсянки",
    "сезонный суп из местных овощей",
    "домашний хлеб по-удмуртски",
    "блины с начинкой из местных ягод",
    "пирог с калиной по-удмуртски",
]

LIFEHACK_TOPICS = [
    "Как сэкономить на ЖКХ в Ижевске",
    "Куда сдать ненужные вещи в Ижевске",
    "Бесплатные мероприятия в Ижевске",
    "Как правильно жаловаться на УК в Ижевске",
    "Где бесплатно заниматься спортом в Ижевске",
    "Лайфхаки для муниципального транспорта Ижевска",
    "Полезные приложения для жителей Ижевска",
    "Как подготовить машину к зиме в Удмуртии",
    "Где собирать грибы и ягоды в Удмуртии",
    "Как экономить на продуктах в Ижевске",
    "Куда обращаться если яма на дороге",
    "Как получить льготы в Удмуртии",
]

UDMURTIA_PLACES = [
    "Набережная Ижевского пруда",
    "Монумент дружбы народов",
    "Свято-Михайловский собор",
    "Музей Калашникова",
    "Центральная площадь Ижевска",
    "Парк Кирова в Ижевске",
    "Зоопарк Удмуртии",
    "Ижевский цирк",
    "Летний сад имени Горького",
    "Национальный музей Удмуртии",
    "Удмуртский драмтеатр",
    "Резиденция Тол Бабая в Шаркане",
    "Архитектурно-этнографический музей Лудорвай",
    "Нечкинский национальный парк",
    "Дом-музей Чайковского в Воткинске",
    "Сарапульский музей-заповедник",
    "Гора Байгурезь в Дебёсском районе",
    "Каповая пещера в Удмуртии",
    "Камбарский краеведческий музей",
    "Урочище Сидоровы горы",
    "Озеро Карасёво в Завьяловском районе",
    "Свято-Троицкий собор в Ижевске",
    "Ботанический сад УдГУ",
    "Увинский рыбхоз и пруды",
    "Село Бураново — родина Бурановских бабушек",
]

EVENING_FUN_TYPES = [
    "викторину (3 вопроса с ответами внизу)",
    "топ-5 забавных ситуаций",
    "подборку «А вы знали?» (3-4 факта)",
    "мини-тест «Настоящий ли ты ижевчанин?» (5 вопросов)",
    "подборку «Только в Ижевске...» (5 пунктов)",
]

# ── Russian & Udmurt holidays database ───────────────────────────────────

HOLIDAYS = {
    # Январь
    (1, 1): "Новый год",
    (1, 7): "Рождество Христово",
    (1, 13): "Старый Новый год",
    (1, 25): "День студента (Татьянин день)",
    # Февраль
    (2, 8): "День российской науки",
    (2, 14): "День всех влюблённых",
    (2, 23): "День защитника Отечества",
    # Март
    (3, 1): "День кошек в России",
    (3, 8): "Международный женский день",
    (3, 18): "День воссоединения Крыма с Россией",
    (3, 27): "Международный день театра",
    # Апрель
    (4, 1): "День смеха",
    (4, 7): "День здоровья",
    (4, 12): "День космонавтики",
    (4, 22): "День Земли",
    # Май
    (5, 1): "Праздник Весны и Труда",
    (5, 9): "День Победы",
    (5, 24): "День славянской письменности",
    (5, 27): "День библиотекаря",
    # Июнь
    (6, 1): "День защиты детей",
    (6, 6): "День русского языка (Пушкинский день)",
    (6, 12): "День России",
    (6, 22): "День памяти и скорби",
    # Июль
    (7, 8): "День семьи, любви и верности",
    (7, 28): "День крещения Руси",
    # Август
    (8, 2): "День ВДВ",
    (8, 12): "День молодёжи",
    (8, 22): "День Государственного флага РФ",
    # Сентябрь
    (9, 1): "День знаний",
    (9, 3): "День солидарности в борьбе с терроризмом",
    (9, 27): "День воспитателя",
    # Октябрь
    (10, 1): "День пожилых людей",
    (10, 4): "День учителя",
    (10, 25): "День таможенника",
    # Ноябрь
    (11, 4): "День народного единства",
    (11, 10): "День сотрудника МВД",
    (11, 21): "День бухгалтера",
    (11, 26): "День матери",
    # Декабрь
    (12, 4): "День информатики",
    (12, 12): "День Конституции РФ",
    (12, 22): "День энергетика",
    (12, 31): "Канун Нового года",
    # Удмуртские праздники
    (6, 15): "Гербер — удмуртский праздник окончания посевных работ",
    (11, 4): "День государственности Удмуртской Республики",
    (2, 21): "Международный день родного языка (удмуртский)",
}

HOLIDAY_PROMPT = """Напиши поздравительный пост для Telegram-канала "Ижевск Сегодня".

Сегодня: {holiday_name}
Дата: {date}

Правила:
- Начни с праздничного эмодзи (🎉🎊🎁❤️🌸💐🎄⭐ и т.п.) + название праздника
- Поздравь читателей тепло и душевно (2-3 абзаца)
- Если праздник связан с Удмуртией — добавь местный контекст
- Добавь пожелание
- Пиши от души, как живой человек, НЕ как бот
- НЕ используй Markdown заголовки
""" + HUMAN_STYLE


class ContentGenerator:
    """Generates unique daily content for the channel with stock photos."""

    def __init__(self, config: Config, rewriter=None, media_processor=None):
        self.config = config
        self._rewriter = rewriter  # AIRewriter — used for ask_ai (Gemini→YandexGPT fallback)
        self._media = media_processor
        self._used_topics: dict[str, list] = {}  # Track used topics per rubric

    def _pick_topic(self, rubric: str, pool: list) -> str:
        """Pick a random unused topic from the pool."""
        used = self._used_topics.get(rubric, [])
        available = [t for t in pool if t not in used]
        if not available:
            self._used_topics[rubric] = []
            available = pool
        topic = random.choice(available)
        self._used_topics.setdefault(rubric, []).append(topic)
        return topic

    async def _ask_ai(self, prompt: str, temperature: float = 0.8) -> Optional[str]:
        """Send prompt to AI with Gemini→YandexGPT fallback."""
        if not self._rewriter:
            logger.error("No AI rewriter available for content generation")
            return None
        return await self._rewriter.ask_ai(prompt, temperature=temperature)

    async def _find_photo(self, text: str, hint_keywords: Optional[list] = None) -> Optional[str]:
        """Find a relevant stock photo URL for the given text.

        Args:
            text: Post text (used to generate keywords via AI if hint_keywords not provided).
            hint_keywords: Optional pre-defined keywords for this rubric — take priority over AI-generated ones.
        """
        if not self._media:
            return None

        # Use hint keywords first; fall back to AI-generated keywords
        if hint_keywords:
            keywords = hint_keywords
        else:
            try:
                prompt = PHOTO_KEYWORDS_PROMPT.format(text=text[:300])
                keywords_text = await self._ask_ai(prompt, temperature=0.2)
                if keywords_text:
                    # Clean HTML tags that _ask_ai may have added
                    keywords_text = re.sub(r'<[^>]+>', '', keywords_text)
                    keywords = [kw.strip().lower() for kw in keywords_text.split(",")]
                else:
                    keywords = ["udmurtia", "russia", "city"]
            except Exception:
                keywords = ["udmurtia", "russia", "nature"]

        # Search Unsplash — pick a random photo from top-5 to add variety
        try:
            photos = await self._media.search_stock_photo(keywords, count=5)
            if photos:
                return random.choice(photos[:5])["url"]
        except Exception as e:
            logger.error(f"Photo search failed ({keywords}): {e}")

        # Fallback: try broader keywords if specific ones returned nothing
        if hint_keywords:
            try:
                fallback = [hint_keywords[0], "russia"]
                photos = await self._media.search_stock_photo(fallback, count=5)
                if photos:
                    return random.choice(photos[:5])["url"]
            except Exception:
                pass

        return None

    async def _generate_with_photo(
        self, text: Optional[str], hint_keywords: Optional[list] = None
    ) -> Tuple[Optional[str], Optional[str]]:
        """Return (text, photo_url) tuple. Always tries to find a photo."""
        if not text:
            return None, None
        photo_url = await self._find_photo(text, hint_keywords=hint_keywords)
        return text, photo_url

    # ── Rubric Methods ── each returns (text, photo_url) ─────────────────

    async def generate_weather(self) -> Tuple[Optional[str], Optional[str]]:
        """Generate weather post using real weather APIs (no AI hallucination).

        Priority:
          1. Яндекс.Погода API (free 50 req/day, Russian conditions)
          2. OpenWeatherMap (if OPENWEATHER_API_KEY is set)
          3. Open-Meteo (free, no key needed)
          4. Return None — do NOT invent weather via AI.
        """
        lat, lon = 56.8526, 53.2114  # Izhevsk coordinates

        # ── 1. Яндекс.Погода (данные уже на русском) ──────────────────────
        yandex_weather_key = self.config.yandex_weather_api_key
        if yandex_weather_key:
            try:
                # Yandex condition codes → Russian text
                YANDEX_CONDITIONS = {
                    "clear": "ясно", "partly-cloudy": "малооблачно",
                    "cloudy": "облачно с прояснениями", "overcast": "пасмурно",
                    "drizzle": "морось", "light-rain": "небольшой дождь",
                    "rain": "дождь", "moderate-rain": "умеренный дождь",
                    "heavy-rain": "сильный дождь", "continuous-heavy-rain": "очень сильный дождь",
                    "showers": "ливень", "wet-snow": "дождь со снегом",
                    "light-snow": "небольшой снег", "snow": "снег",
                    "snow-showers": "снегопад", "hail": "град",
                    "thunderstorm": "гроза", "thunderstorm-with-rain": "гроза с дождём",
                    "thunderstorm-with-hail": "гроза с градом",
                }
                url = (
                    f"https://api.weather.yandex.ru/v2/forecast"
                    f"?lat={lat}&lon={lon}&lang=ru_RU&limit=1&hours=false&extra=false"
                )
                headers = {"X-Yandex-Weather-Key": yandex_weather_key}
                async with aiohttp.ClientSession(headers=headers) as session:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            fact = data["fact"]
                            condition_code = fact.get("condition", "cloudy")
                            description = YANDEX_CONDITIONS.get(condition_code, "переменная облачность")
                            return await self._build_weather_post(
                                temp=fact["temp"],
                                feels_like=fact["feels_like"],
                                description=description,
                                wind=fact.get("wind_speed", 0),
                                humidity=fact.get("humidity", 0),
                                pressure=fact.get("pressure_mm", 760),
                            )
                        logger.warning(f"Яндекс.Погода HTTP {resp.status}, trying OpenWeatherMap")
            except Exception as e:
                logger.warning(f"Яндекс.Погода failed ({e}), trying OpenWeatherMap")
        api_key = self.config.openweather_api_key
        if api_key:
            try:
                url = (
                    f"https://api.openweathermap.org/data/2.5/weather"
                    f"?lat={lat}&lon={lon}&appid={api_key}&units=metric&lang=ru"
                )
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            return await self._build_weather_post(
                                temp=round(data["main"]["temp"]),
                                feels_like=round(data["main"]["feels_like"]),
                                description=data["weather"][0]["description"],
                                wind=round(data["wind"]["speed"], 1),
                                humidity=data["main"]["humidity"],
                                pressure=round(data["main"]["pressure"] * 0.750062),
                            )
                        logger.warning(f"OpenWeatherMap HTTP {resp.status}, falling back to Open-Meteo")
            except Exception as e:
                logger.warning(f"OpenWeatherMap failed ({e}), falling back to Open-Meteo")

        # ── Fallback: Open-Meteo (completely free, no API key) ────────────
        try:
            # WMO weather codes → Russian description
            WMO_CODES = {
                0: "ясно", 1: "преимущественно ясно", 2: "переменная облачность", 3: "пасмурно",
                45: "туман", 48: "изморозь", 51: "лёгкая морось", 53: "морось", 55: "сильная морось",
                61: "лёгкий дождь", 63: "дождь", 65: "сильный дождь",
                71: "лёгкий снег", 73: "снег", 75: "сильный снег", 77: "снежная крупа",
                80: "ливень", 81: "ливень", 82: "сильный ливень",
                85: "снегопад", 86: "сильный снегопад",
                95: "гроза", 96: "гроза с градом", 99: "сильная гроза с градом",
            }
            url = (
                f"https://api.open-meteo.com/v1/forecast"
                f"?latitude={lat}&longitude={lon}"
                f"&current=temperature_2m,apparent_temperature,weather_code,"
                f"wind_speed_10m,relative_humidity_2m,surface_pressure"
                f"&wind_speed_unit=ms&timezone=Europe%2FSamara"
            )
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        cur = data["current"]
                        wmo = cur.get("weather_code", 0)
                        description = WMO_CODES.get(wmo, "переменная облачность")
                        return await self._build_weather_post(
                            temp=round(cur["temperature_2m"]),
                            feels_like=round(cur["apparent_temperature"]),
                            description=description,
                            wind=round(cur["wind_speed_10m"], 1),
                            humidity=cur["relative_humidity_2m"],
                            pressure=round(cur["surface_pressure"] * 0.750062),
                        )
                    logger.error(f"Open-Meteo HTTP {resp.status}")
        except Exception as e:
            logger.error(f"Open-Meteo failed: {e}")

        # ── All APIs failed — do NOT invent weather ───────────────────────
        logger.error("All weather APIs failed — skipping weather post to avoid fake data")
        return None, None

    async def _build_weather_post(
        self, temp: int, feels_like: int, description: str,
        wind: float, humidity: int, pressure: int,
    ) -> Tuple[Optional[str], Optional[str]]:
        """Build the weather post text from real weather data using AI for natural language."""
        now = datetime.now()
        date_str = now.strftime("%d %B").lstrip("0")
        prompt = WEATHER_FORMAT.format(
            temp=temp, feels_like=feels_like, description=description,
            wind=wind, humidity=humidity, pressure=pressure, date=date_str,
        )
        text = await self._ask_ai(prompt, temperature=0.5)
        if text:
            text += "\n\n#погода #ижевск"
            text += "\n\n📲 @IzhevskTodayNews | 📩 @IzhevskTodayBot"
        return await self._generate_with_photo(text, hint_keywords=["izhevsk winter city", "russia weather"])

    async def generate_history_fact(self) -> Tuple[Optional[str], Optional[str]]:
        """Generate 'This day in history' post."""
        now = datetime.now()
        date_str = f"{now.day} {now.strftime('%B')}"
        prompt = HISTORY_PROMPT.format(date=date_str)
        text = await self._ask_ai(prompt)
        if text:
            text += "\n\n#история #удмуртия #ижевск"
            text += "\n\n📲 @IzhevskTodayNews | 📩 @IzhevskTodayBot"
        return await self._generate_with_photo(text, hint_keywords=["history", "archive", "russia"])

    async def generate_five_facts(self) -> Tuple[Optional[str], Optional[str]]:
        """Generate '5 facts about...' post."""
        topic = self._pick_topic("facts", FIVE_FACTS_TOPICS)
        prompt = FIVE_FACTS_PROMPT.format(topic=topic)
        text = await self._ask_ai(prompt)
        if text:
            text += "\n\n#факты #ижевск #удмуртия"
            text += "\n\n📲 @IzhevskTodayNews | 📩 @IzhevskTodayBot"
        # Derive English search keywords from Russian topic
        topic_en_map = {
            "набережной": "embankment waterfront", "оружейном": "gun factory weapons",
            "удмуртском языке": "language culture", "Калашникове": "kalashnikov gun",
            "кухне": "food cooking", "зоопарк": "zoo animals",
            "реке": "river nature", "архитектур": "architecture building",
            "традиц": "tradition culture folk", "цирк": "circus entertainment",
            "спорт": "sport athletics", "мотоцикл": "motorcycle",
            "ледов": "ice winter", "культур": "culture art",
            "музей": "museum exhibit", "мёд": "honey bees",
            "улиц": "street city", "парк": "park green",
            "образован": "education school", "транспорт": "transport bus",
            "промышленност": "industry factory", "театр": "theater stage",
            "природ": "nature forest", "знаменит": "portrait people",
            "фестивал": "festival crowd", "Камбарк": "russia town",
            "Воткинск": "russia city", "Сарапул": "russia river",
        }
        hint = ["facts", "russia"]
        for key, val in topic_en_map.items():
            if key.lower() in topic.lower():
                hint = val.split() + ["russia"]
                break
        return await self._generate_with_photo(text, hint_keywords=hint)

    async def generate_recipe(self) -> Tuple[Optional[str], Optional[str]]:
        """Generate a recipe post."""
        topic = self._pick_topic("recipe", RECIPE_TOPICS)
        prompt = RECIPE_PROMPT.format(topic=topic)
        text = await self._ask_ai(prompt)
        if text:
            text += "\n\n#рецепт #удмуртия #кухня"
            text += "\n\n📲 @IzhevskTodayNews | 📩 @IzhevskTodayBot"
        # Recipe photos: use food-specific keywords for better Unsplash results
        recipe_photo_map = {
            "перепеч": ["meat pie", "pastry baked"],
            "табан": ["pancakes", "traditional food"],
            "пельмен": ["dumplings", "homemade food"],
            "шаньг": ["potato pastry", "baked goods"],
            "шыд": ["soup broth", "homemade soup"],
            "гриб": ["mushroom dish", "forest mushrooms"],
            "кокрок": ["pastry baked", "homemade bread"],
            "кисель": ["porridge oats", "traditional drink"],
            "суп": ["vegetable soup", "rustic cooking"],
            "хлеб": ["homemade bread", "bakery"],
            "блин": ["pancakes berries", "traditional food"],
            "пирог": ["berry pie", "fruit cake baked"],
        }
        hint = ["food cooking", "homemade"]
        for key, val in recipe_photo_map.items():
            if key.lower() in topic.lower():
                hint = val
                break
        return await self._generate_with_photo(text, hint_keywords=hint)

    async def generate_lifehack(self) -> Tuple[Optional[str], Optional[str]]:
        """Generate a lifehack post."""
        topic = self._pick_topic("lifehack", LIFEHACK_TOPICS)
        prompt = LIFEHACK_PROMPT.format(topic=topic)
        text = await self._ask_ai(prompt)
        if text:
            text += "\n\n#полезно #ижевск #лайфхак"
            text += "\n\n📲 @IzhevskTodayNews | 📩 @IzhevskTodayBot"
        lifehack_photo_map = {
            "ЖКХ": ["utility bills apartment", "apartment interior"],
            "вещи": ["donate clothes", "thrift store items"],
            "мероприятия": ["city event festival", "outdoor activities"],
            "УК": ["apartment building housing", "city services"],
            "спорт": ["outdoor sport exercise", "gym fitness"],
            "транспорт": ["bus public transport", "city commute"],
            "приложен": ["smartphone app", "mobile phone"],
            "машину": ["car winter", "automobile maintenance"],
            "грибы": ["forest mushrooms picking", "nature walk"],
            "продукт": ["supermarket grocery", "food shopping"],
            "яма": ["road repair", "street infrastructure"],
            "льготы": ["documents paperwork", "social services"],
        }
        hint = ["city life tips", "urban living"]
        for key, val in lifehack_photo_map.items():
            if key.lower() in topic.lower():
                hint = val
                break
        return await self._generate_with_photo(text, hint_keywords=hint)

    async def generate_place(self) -> Tuple[Optional[str], Optional[str]]:
        """Generate a place of Udmurtia guide post."""
        topic = self._pick_topic("places", UDMURTIA_PLACES)
        prompt = PLACE_PROMPT.format(topic=topic)
        text = await self._ask_ai(prompt)
        if text:
            text += "\n\n#места #удмуртия"
            text += "\n\n📲 @IzhevskTodayNews | 📩 @IzhevskTodayBot"
        place_photo_map = {
            "набережн": ["waterfront embankment", "river promenade"],
            "монумент": ["monument statue", "memorial sculpture"],
            "собор": ["orthodox church", "cathedral russia"],
            "музей Калашникова": ["gun museum weapons", "military museum"],
            "площадь": ["city square plaza", "urban square"],
            "парк Кирова": ["city park trees", "park walking"],
            "зоопарк": ["zoo animals", "wildlife park"],
            "цирк": ["circus arena", "circus performance"],
            "летний сад": ["summer garden park", "botanical garden"],
            "музей": ["museum interior exhibit", "gallery art"],
            "театр": ["theater stage", "historic theater"],
            "Тол Бабай": ["winter fairy tale", "snow forest cottage"],
            "Лудорвай": ["open air museum", "folk village ethnography"],
            "Нечкинск": ["national park nature", "kama river"],
            "Чайковского": ["historic house museum", "composer piano"],
            "Сарапул": ["historic town river", "russia old town"],
            "гора": ["hill landscape", "nature panorama"],
            "пещера": ["cave spelunking", "rock cave"],
            "Камбарск": ["small town russia", "local museum"],
            "горы": ["hills forest nature", "landscape panorama"],
            "озеро": ["lake nature", "tranquil lake water"],
            "Троицкий": ["orthodox church", "cathedral architecture"],
            "ботанический": ["botanical garden plants", "greenhouse garden"],
            "рыбхоз": ["fish pond lake", "fishing rural"],
            "Бураново": ["village countryside", "folk singing"],
        }
        hint = ["russia travel landmark", "russian landscape"]
        for key, val in place_photo_map.items():
            if key.lower() in topic.lower():
                hint = val
                break
        return await self._generate_with_photo(text, hint_keywords=hint)

    async def generate_evening_fun(self) -> Tuple[Optional[str], Optional[str]]:
        """Generate evening entertainment post."""
        content_type = random.choice(EVENING_FUN_TYPES)
        prompt = EVENING_FUN_PROMPT.format(content_type=content_type)
        text = await self._ask_ai(prompt, temperature=0.9)
        if text:
            text += "\n\n#вечер #ижевск #развлечения"
            text += "\n\n📲 @IzhevskTodayNews | 📩 @IzhevskTodayBot"
        fun_keywords = random.choice([
            ["happy people laughing", "friends fun"],
            ["evening city lights", "night life"],
            ["entertainment quiz game", "trivia fun"],
            ["cozy evening home", "relax leisure"],
        ])
        return await self._generate_with_photo(text, hint_keywords=fun_keywords)

    async def generate_daily_digest(self, published_texts: List[str]) -> Tuple[Optional[str], Optional[str]]:
        """Generate daily digest from published news."""
        if not published_texts:
            return None, None

        now = datetime.now()
        date_str = now.strftime("%d.%m.%Y")
        news_list = "\n\n---\n\n".join([t[:200] for t in published_texts[:10]])
        prompt = DIGEST_PROMPT.format(news_list=news_list, date=date_str)
        text = await self._ask_ai(prompt, temperature=0.3)
        if text:
            text += "\n\n#итогидня #ижевск"
            text += "\n\n📲 @IzhevskTodayNews | 📩 @IzhevskTodayBot"
        return await self._generate_with_photo(text)

    async def generate_holiday(self) -> Tuple[Optional[str], Optional[str]]:
        """Generate holiday greeting post if today is a holiday."""
        now = datetime.now()
        key = (now.month, now.day)
        holiday_name = HOLIDAYS.get(key)

        if not holiday_name:
            return None, None

        date_str = now.strftime("%d.%m.%Y")
        prompt = HOLIDAY_PROMPT.format(holiday_name=holiday_name, date=date_str)
        text = await self._ask_ai(prompt)
        if text:
            text += "\n\n#праздник #ижевск"
            text += "\n\n📲 @IzhevskTodayNews | 📩 @IzhevskTodayBot"
        holiday_photo_map = {
            "Новый год": ["new year celebration", "fireworks snow"],
            "Рождество": ["christmas decoration", "winter holiday"],
            "женский день": ["flowers bouquet women", "spring flowers"],
            "Победы": ["victory parade memorial", "war veterans"],
            "защитника": ["military honor soldiers", "patriotic"],
            "России": ["russia flag celebration", "patriotic holiday"],
            "детей": ["children playing happy", "kids outdoor"],
            "влюблённых": ["romantic couple love", "valentine hearts"],
            "знаний": ["school books students", "education first day"],
            "матери": ["mother child family", "mother daughter"],
            "учителя": ["teacher classroom education", "school"],
            "семьи": ["happy family", "family outdoors"],
            "космонавтики": ["space stars cosmos", "rocket launch"],
        }
        hint = ["celebration holiday festive", "holiday decoration"]
        for key_word, val in holiday_photo_map.items():
            if key_word.lower() in holiday_name.lower():
                hint = val
                break
        return await self._generate_with_photo(text, hint_keywords=hint)
