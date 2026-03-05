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

DIGEST_PROMPT = """Составь дайджест дня из новостей:

{news_list}

Начни: 📊 Главное за {date}
Выдели 3-5 самых важных — каждая в 1 предложение с эмодзи.
Компактный список, как подборка для друга.
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

    def __init__(self, config: Config, gemini_model=None, media_processor=None):
        self.config = config
        self._model = gemini_model
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

    async def _ask_gemini(self, prompt: str, temperature: float = 0.8) -> Optional[str]:
        """Send prompt to Gemini and return response text."""
        if not self._model:
            logger.error("No Gemini model available for content generation")
            return None

        try:
            loop = asyncio.get_event_loop()
            _model = self._model
            _prompt = prompt
            response = await loop.run_in_executor(
                None,
                lambda: _model.generate_content(
                    _prompt,
                    generation_config=genai.GenerationConfig(
                        temperature=temperature,
                        max_output_tokens=2048,
                    ),
                    safety_settings=SAFETY_SETTINGS,
                ),
            )

            if response and response.text:
                text = response.text.strip()
                text = re.sub(r'^#{1,3}\s*', '', text, flags=re.MULTILINE)
                # Convert **bold** markdown to <b>bold</b> HTML
                text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
                return text

        except Exception as e:
            logger.error(f"Gemini content generation failed: {e}")

        return None

    async def _find_photo(self, text: str) -> Optional[str]:
        """Find a relevant stock photo URL for the given text."""
        if not self._media or not self._media.unsplash_key:
            return None

        # Ask Gemini for photo keywords
        try:
            prompt = PHOTO_KEYWORDS_PROMPT.format(text=text[:300])
            keywords_text = await self._ask_gemini(prompt, temperature=0.2)
            if keywords_text:
                # Clean HTML tags that _ask_gemini may have added
                keywords_text = re.sub(r'<[^>]+>', '', keywords_text)
                keywords = [kw.strip().lower() for kw in keywords_text.split(",")]
            else:
                keywords = ["udmurtia", "russia", "city"]
        except Exception:
            keywords = ["udmurtia", "russia", "nature"]

        # Search Unsplash
        try:
            photos = await self._media.search_stock_photo(keywords, count=3)
            if photos:
                return photos[0]["url"]
        except Exception as e:
            logger.error(f"Photo search failed: {e}")

        return None

    async def _generate_with_photo(self, text: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
        """Return (text, photo_url) tuple. Always tries to find a photo."""
        if not text:
            return None, None
        photo_url = await self._find_photo(text)
        return text, photo_url

    # ── Rubric Methods ── each returns (text, photo_url) ─────────────────

    async def generate_weather(self) -> Tuple[Optional[str], Optional[str]]:
        """Generate weather post using OpenWeatherMap API."""
        api_key = self.config.openweather_api_key
        if not api_key:
            logger.warning("OpenWeatherMap API key not set, using AI fallback")
            text = await self._generate_weather_ai()
            return await self._generate_with_photo(text)

        try:
            lat, lon = 56.8526, 53.2114
            url = f"https://api.openweathermap.org/data/2.5/weather?lat={lat}&lon={lon}&appid={api_key}&units=metric&lang=ru"

            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status != 200:
                        logger.error(f"OpenWeatherMap returned {resp.status}")
                        text = await self._generate_weather_ai()
                        return await self._generate_with_photo(text)
                    data = await resp.json()

            temp = round(data["main"]["temp"])
            feels_like = round(data["main"]["feels_like"])
            description = data["weather"][0]["description"]
            wind = round(data["wind"]["speed"], 1)
            humidity = data["main"]["humidity"]
            pressure = round(data["main"]["pressure"] * 0.750062)

            now = datetime.now()
            date_str = now.strftime("%d %B").lstrip("0")

            prompt = WEATHER_FORMAT.format(
                temp=temp, feels_like=feels_like, description=description,
                wind=wind, humidity=humidity, pressure=pressure, date=date_str,
            )
            text = await self._ask_gemini(prompt, temperature=0.5)
            if text:
                text += "\n\n#погода #ижевск"
                text += "\n\n📲 @IzhevskTodayNews | 📩 @IzhevskTodayBot"
            return await self._generate_with_photo(text)

        except Exception as e:
            logger.error(f"Weather generation failed: {e}")
            text = await self._generate_weather_ai()
            return await self._generate_with_photo(text)

    async def _generate_weather_ai(self) -> Optional[str]:
        """Fallback: generate weather purely via AI."""
        now = datetime.now()
        month = now.strftime("%B")
        day = now.day
        prompt = (
            f"Напиши типичный прогноз погоды для Ижевска на {day} {month}. "
            "Основывайся на климатических нормах. "
            "Эмодзи + 'Ижевск, дата' + 3-4 строки + совет что надеть. "
            "Пиши как человек, не как бот.\n" + HUMAN_STYLE
        )
        text = await self._ask_gemini(prompt, temperature=0.6)
        if text:
            text += "\n\n#погода #ижевск"
            text += "\n\n📲 @IzhevskTodayNews | 📩 @IzhevskTodayBot"
        return text

    async def generate_history_fact(self) -> Tuple[Optional[str], Optional[str]]:
        """Generate 'This day in history' post."""
        now = datetime.now()
        date_str = f"{now.day} {now.strftime('%B')}"
        prompt = HISTORY_PROMPT.format(date=date_str)
        text = await self._ask_gemini(prompt)
        if text:
            text += "\n\n#история #удмуртия #ижевск"
            text += "\n\n📲 @IzhevskTodayNews | 📩 @IzhevskTodayBot"
        return await self._generate_with_photo(text)

    async def generate_five_facts(self) -> Tuple[Optional[str], Optional[str]]:
        """Generate '5 facts about...' post."""
        topic = self._pick_topic("facts", FIVE_FACTS_TOPICS)
        prompt = FIVE_FACTS_PROMPT.format(topic=topic)
        text = await self._ask_gemini(prompt)
        if text:
            text += "\n\n#факты #ижевск #удмуртия"
            text += "\n\n📲 @IzhevskTodayNews | 📩 @IzhevskTodayBot"
        return await self._generate_with_photo(text)

    async def generate_recipe(self) -> Tuple[Optional[str], Optional[str]]:
        """Generate a recipe post."""
        topic = self._pick_topic("recipe", RECIPE_TOPICS)
        prompt = RECIPE_PROMPT.format(topic=topic)
        text = await self._ask_gemini(prompt)
        if text:
            text += "\n\n#рецепт #удмуртия #кухня"
            text += "\n\n📲 @IzhevskTodayNews | 📩 @IzhevskTodayBot"
        return await self._generate_with_photo(text)

    async def generate_lifehack(self) -> Tuple[Optional[str], Optional[str]]:
        """Generate a lifehack post."""
        topic = self._pick_topic("lifehack", LIFEHACK_TOPICS)
        prompt = LIFEHACK_PROMPT.format(topic=topic)
        text = await self._ask_gemini(prompt)
        if text:
            text += "\n\n#полезно #ижевск #лайфхак"
            text += "\n\n📲 @IzhevskTodayNews | 📩 @IzhevskTodayBot"
        return await self._generate_with_photo(text)

    async def generate_place(self) -> Tuple[Optional[str], Optional[str]]:
        """Generate a place of Udmurtia guide post."""
        topic = self._pick_topic("places", UDMURTIA_PLACES)
        prompt = PLACE_PROMPT.format(topic=topic)
        text = await self._ask_gemini(prompt)
        if text:
            text += "\n\n#места #удмуртия"
            text += "\n\n📲 @IzhevskTodayNews | 📩 @IzhevskTodayBot"
        return await self._generate_with_photo(text)

    async def generate_evening_fun(self) -> Tuple[Optional[str], Optional[str]]:
        """Generate evening entertainment post."""
        content_type = random.choice(EVENING_FUN_TYPES)
        prompt = EVENING_FUN_PROMPT.format(content_type=content_type)
        text = await self._ask_gemini(prompt, temperature=0.9)
        if text:
            text += "\n\n#вечер #ижевск #развлечения"
            text += "\n\n📲 @IzhevskTodayNews | 📩 @IzhevskTodayBot"
        return await self._generate_with_photo(text)

    async def generate_daily_digest(self, published_texts: List[str]) -> Tuple[Optional[str], Optional[str]]:
        """Generate daily digest from published news."""
        if not published_texts:
            return None, None

        now = datetime.now()
        date_str = now.strftime("%d.%m.%Y")
        news_list = "\n\n---\n\n".join([t[:200] for t in published_texts[:10]])
        prompt = DIGEST_PROMPT.format(news_list=news_list, date=date_str)
        text = await self._ask_gemini(prompt, temperature=0.3)
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
        text = await self._ask_gemini(prompt)
        if text:
            text += "\n\n#праздник #ижевск"
            text += "\n\n📲 @IzhevskTodayNews | 📩 @IzhevskTodayBot"
        return await self._generate_with_photo(text)
