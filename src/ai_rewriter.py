"""
AI Rewriter — rewrites news text using Google Gemini API to avoid copyright issues.
Falls back to ReText.AI if Gemini is unavailable.
"""

import logging
import asyncio
from typing import Optional, Tuple

import google.generativeai as genai
try:
    from google.generativeai.types import HarmCategory, HarmBlockThreshold
    _HAS_SAFETY = True
except ImportError:
    _HAS_SAFETY = False
import aiohttp

from src.config import Config

logger = logging.getLogger(__name__)

# Prompt template for news rewriting — premium Telegram channel style
REWRITE_PROMPT = """Ты — главный редактор популярного новостного Telegram-канала "Ижевск Сегодня".

Твоя задача — ПОЛНОСТЬЮ ПЕРЕПИСАТЬ текст новости в стиле крупного Telegram-канала.

ОБЯЗАТЕЛЬНЫЕ ПРАВИЛА ОФОРМЛЕНИЯ:

1. ЗАГОЛОВОК (первая строка):
   - Начни с ОДНОГО тематического эмодзи (❄️ погода, 🔥 пожар, ⚡ срочное, 🚗 транспорт, 💰 экономика, 🏗 стройка, 👮 полиция, 🏥 медицина, 📚 образование, ⚽ спорт, 🌡 погода, 😱 шок, ❗️ важное, 🎉 праздник, 🔧 ЖКХ)
   - Дальше — короткий ЦЕПЛЯЮЩИЙ заголовок (макс 10 слов)
   - Пример: ❄️ Ещё 11 крыш рухнуло в Удмуртии из-за снега

2. ТЕКСТ НОВОСТИ:
   - Короткие абзацы (2-3 предложения МАКСИМУМ)
   - Ключевые факты выделяй **жирным** (даты, цифры, имена, адреса)
   - Если есть перечисление — оформи как список с маркером ❗️
   - НЕ БОЛЕЕ 5-7 абзацев
   - Пиши живым языком, НЕ канцелярит

3. ЗАПРЕЩЕНО:
   - Упоминать источник оригинала
   - Оставлять "подписаться", "читайте на сайте" и т.п.
   - Копировать текст — ВСЁ должно быть ПЕРЕПИСАНО своими словами
   - Добавлять свои комментарии или оценки
   - Использовать Markdown заголовки (# ## ###)

4. ФОРМАТ ОТВЕТА:
Эмодзи + Заголовок

Первый абзац текста.

Второй абзац с **ключевыми фактами**.

❗️ Пункт списка один
❗️ Пункт списка два

Завершающий абзац.

Перепиши эту новость:

{text}"""

# Simpler prompt for shorter texts
REWRITE_SHORT_PROMPT = """Ты — редактор Telegram-канала "Ижевск Сегодня". 
Перепиши новость СВОИМИ СЛОВАМИ. 

Правила:
- Начни с эмодзи по теме + короткий цепляющий заголовок
- Выдели **жирным** ключевые факты
- Короткие абзацы (2-3 предложения)
- НЕ копируй текст, ПЕРЕПИШИ полностью
- НЕ упоминай источник

{text}"""

# Safety settings — allow all content (news about accidents/crime gets blocked otherwise)
if _HAS_SAFETY:
    SAFETY_SETTINGS = {
        HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
        HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
        HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
        HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
    }
else:
    SAFETY_SETTINGS = None

# Prompt to check if news is of general interest (not region-specific)
RELEVANCE_PROMPT = """Ты — редактор новостного канала для жителей Ижевска и Удмуртии.

Определи, нужна ли эта новость нашим читателям.

Новость НУЖНА (ответь ДА), если это:
- Федеральные законы, указы, решения правительства (касаются всех)
- Экономика: курс валют, инфляция, цены, тарифы ЖКХ
- Погода/катастрофы федерального масштаба
- Международные отношения
- Изменения в пенсиях, зарплатах, налогах, льготах
- Общероссийские события, праздники
- Технологии, наука
- Спорт (сборная России, крупные турниры)
- ЛЮБЫЕ новости про Ижевск, Удмуртию, Удмуртскую Республику

Новость НЕ НУЖНА (ответь НЕТ), если это:
- Происшествие в ДРУГОМ городе/регионе (не в Ижевске/Удмуртии)
- Ремонт дорог/зданий в ДРУГОМ регионе
- Местные назначения/выборы в ДРУГОМ регионе
- Региональные мероприятия ДРУГОГО региона

Ответь ТОЛЬКО одним словом: ДА или НЕТ.

Новость: {text}"""


# Phrases that indicate AI refused to process the text (safety/policy filters)
REFUSAL_PHRASES = [
    "я не могу обсуждать",
    "не могу помочь с этим",
    "я не в состоянии",
    "не могу выполнить",
    "я не могу создать",
    "я не могу написать",
    "я не могу переписать",
    "я не могу обработать",
    "давайте поговорим о чём-нибудь",
    "давайте поговорим о чем-нибудь",
    "давайте поговорим о другом",
    "поговорим о другом",
    "давайте сменим тему",
    "предлагаю сменить тему",
    "не буду обсуждать",
    "отказываюсь обсуждать",
    "не могу поддержать эту тему",
    "не могу генерировать",
    "не соответствует правилам",
    "нарушает правила",
    "не могу обсуждать эту тему",
    "поговорим о чём-нибудь ещё",
    "поговорим о чем-нибудь еще",
    "эта тема противоречит",
    "противоречит моим принципам",
    "i cannot",
    "i can't",
    "i'm unable to",
    "i am unable to",
    "as an ai",
    "i'm not able to",
    "i apologize, but",
    "извините, но я не",
    "к сожалению, я не могу",
    "мне не следует",
]


class AIRewriter:
    """Rewrites news text using AI to create unique content."""

    def __init__(self, config: Config):
        self.config = config
        self._gemini_model = None
        self._gemini_models = []  # Fallback models list
        self._setup_gemini()

    def _setup_gemini(self):
        """Initialize Gemini API client with fallback models."""
        if self.config.gemini_api_key:
            try:
                genai.configure(api_key=self.config.gemini_api_key)
                # Multiple models — each has separate free tier quota
                # Updated March 2026: gemini-1.5-flash is deprecated (404)
                model_names = [
                    "gemini-2.0-flash",           # Primary: fast, free tier
                    "gemini-2.0-flash-lite",       # Fallback: lighter version
                    "gemini-2.5-pro-exp-03-25",    # Fallback: experimental but available
                    "gemini-1.5-flash-latest",     # Last resort: 1.5 via latest alias
                ]
                for name in model_names:
                    try:
                        model = genai.GenerativeModel(name)
                        self._gemini_models.append((name, model))
                    except Exception as e:
                        logger.warning(f"Failed to init model {name}: {e}")
                
                if self._gemini_models:
                    self._gemini_model = self._gemini_models[0][1]
                    names = [m[0] for m in self._gemini_models]
                    logger.info(f"Gemini API configured: {', '.join(names)}")
                else:
                    logger.error("No Gemini models available!")
            except Exception as e:
                logger.error(f"Failed to configure Gemini API: {e}")
        else:
            logger.error("⚠️ GEMINI_API_KEY is NOT SET! AI rewrite will NOT work!")

    async def check_relevance(self, text: str) -> bool:
        """
        Check if a news post is of general interest (not region-specific).
        Returns True if the news is relevant to everyone, False if regional.
        """
        if not self._gemini_model:
            # If no Gemini, let everything through
            return True

        try:
            prompt = RELEVANCE_PROMPT.format(text=text[:500])
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                lambda: self._gemini_model.generate_content(
                    prompt,
                    generation_config=genai.GenerationConfig(
                        temperature=0.1,
                        max_output_tokens=10,
                    ),
                ),
            )

            if response and response.text:
                answer = response.text.strip().upper()
                is_relevant = "ДА" in answer
                logger.info(f"Relevance check: {'✅ general' if is_relevant else '❌ regional'}")
                return is_relevant

        except Exception as e:
            logger.error(f"Relevance check failed: {e}")

        return True  # On error, let it through

    async def rewrite(self, original_text: str) -> Tuple[Optional[str], str]:
        """
        Rewrite text using AI.
        
        Returns:
            Tuple of (rewritten_text or None, engine_used: 'gemini' | 'yandexgpt' | 'retext' | 'error')
        """
        # Try Gemini first
        if self._gemini_model:
            result = await self._rewrite_with_gemini(original_text)
            if result:
                return result, "gemini"

        # Fallback to YandexGPT
        if self.config.yandex_api_key and self.config.yandex_folder_id:
            result = await self._rewrite_with_yandexgpt(original_text)
            if result:
                return result, "yandexgpt"

        # Last resort: ReText.AI
        if self.config.retext_api_key:
            result = await self._rewrite_with_retext(original_text)
            if result:
                return result, "retext"

        logger.error("All AI rewrite engines failed")
        return None, "error"

    async def _rewrite_with_gemini(self, text: str) -> Optional[str]:
        """Rewrite text using Google Gemini with model fallback on quota errors."""
        if not self._gemini_models:
            logger.error("No Gemini models available for rewrite")
            return None

        for model_name, model in self._gemini_models:
            try:
                prompt = REWRITE_PROMPT.format(text=text) if len(text) > 300 else REWRITE_SHORT_PROMPT.format(text=text)

                # Run sync Gemini call in executor to avoid blocking
                loop = asyncio.get_event_loop()
                _model = model  # capture for lambda
                _prompt = prompt
                response = await loop.run_in_executor(
                    None,
                    lambda: _model.generate_content(
                        _prompt,
                        generation_config=genai.GenerationConfig(
                            temperature=0.9,
                            max_output_tokens=2048,
                        ),
                        safety_settings=SAFETY_SETTINGS,
                    ),
                )

                if response and response.text:
                    rewritten = response.text.strip()
                    
                    # Check for AI refusal
                    if self._is_refusal(rewritten):
                        logger.warning(f"Gemini [{model_name}]: REFUSAL detected, skipping: {rewritten[:80]}")
                        continue  # Try next model
                    
                    if len(rewritten) > 50 and rewritten != text:
                        uniqueness = self.calculate_uniqueness(text, rewritten)
                        logger.info(f"Gemini [{model_name}]: uniqueness {uniqueness:.0%} ({len(text)} -> {len(rewritten)} chars)")
                        
                        if uniqueness >= 0.4:
                            return rewritten
                        else:
                            # Low uniqueness — retry with stronger prompt
                            extra = "\n\n⚠️ ПОЛНОСТЬЮ ПЕРЕПИШИ КАЖДОЕ ПРЕДЛОЖЕНИЕ. Используй ДРУГИЕ слова и структуру."
                            strong_prompt = (REWRITE_PROMPT + extra).format(text=text)
                            _sp = strong_prompt
                            response2 = await loop.run_in_executor(
                                None,
                                lambda: _model.generate_content(
                                    _sp,
                                    generation_config=genai.GenerationConfig(temperature=0.95, max_output_tokens=2048),
                                    safety_settings=SAFETY_SETTINGS,
                                ),
                            )
                            if response2 and response2.text:
                                retry_text = response2.text.strip()
                                if not self._is_refusal(retry_text):
                                    return retry_text
                                logger.warning(f"Gemini [{model_name}]: retry also refused")
                            return rewritten  # Use low-uniqueness version as last resort
                    else:
                        logger.warning(f"Gemini [{model_name}]: too short or identical")
                        return None
                else:
                    logger.warning(f"Gemini [{model_name}]: empty response")
                    return None

            except Exception as e:
                error_str = str(e).lower()
                is_quota = "429" in error_str or "quota" in error_str or "resource" in error_str
                
                if is_quota:
                    logger.warning(f"Gemini [{model_name}]: quota exhausted, trying next model...")
                    continue  # Try next model
                else:
                    logger.error(f"Gemini [{model_name}] error: {e}")
                    return None

        logger.warning("All Gemini models exhausted (quota), falling back to YandexGPT...")
        return None

    async def _rewrite_with_yandexgpt(self, text: str) -> Optional[str]:
        """Rewrite text using YandexGPT API (REST via aiohttp)."""
        try:
            url = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"
            headers = {
                "Authorization": f"Api-Key {self.config.yandex_api_key}",
                "Content-Type": "application/json",
            }

            # Use lite model for cost efficiency
            model_uri = f"gpt://{self.config.yandex_folder_id}/yandexgpt-lite/latest"

            prompt = REWRITE_PROMPT.format(text=text) if len(text) > 300 else REWRITE_SHORT_PROMPT.format(text=text)

            body = {
                "modelUri": model_uri,
                "completionOptions": {
                    "stream": False,
                    "temperature": 0.9,
                    "maxTokens": "2048",
                },
                "messages": [
                    {
                        "role": "system",
                        "text": "Ты — главный редактор популярного новостного Telegram-канала. Перепиши новость полностью своими словами.",
                    },
                    {
                        "role": "user",
                        "text": prompt,
                    },
                ],
            }

            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=body, headers=headers) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        # YandexGPT response format
                        result = data.get("result", {})
                        alternatives = result.get("alternatives", [])
                        if alternatives:
                            rewritten = alternatives[0].get("message", {}).get("text", "").strip()
                            # Check for AI refusal
                            if self._is_refusal(rewritten):
                                logger.warning(f"YandexGPT: REFUSAL detected: {rewritten[:80]}")
                            elif rewritten and len(rewritten) > 50 and rewritten != text:
                                uniqueness = self.calculate_uniqueness(text, rewritten)
                                logger.info(
                                    f"YandexGPT: uniqueness {uniqueness:.0%} "
                                    f"({len(text)} -> {len(rewritten)} chars)"
                                )
                                return rewritten
                            else:
                                logger.warning("YandexGPT: too short or identical")
                    else:
                        error = await resp.text()
                        logger.error(f"YandexGPT returned {resp.status}: {error[:200]}")

        except Exception as e:
            logger.error(f"YandexGPT rewrite failed: {e}")

        return None

    async def _rewrite_with_retext(self, text: str) -> Optional[str]:
        """Rewrite text using ReText.AI API (fallback)."""
        try:
            async with aiohttp.ClientSession() as session:
                # ReText.AI API endpoint
                url = "https://retext.ai/api/v1/rewrite"
                headers = {
                    "Authorization": f"Bearer {self.config.retext_api_key}",
                    "Content-Type": "application/json",
                }
                payload = {
                    "text": text,
                    "language": "ru",
                    "mode": "full",  # full rewrite mode
                }

                async with session.post(url, json=payload, headers=headers) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        rewritten = data.get("result", "").strip()
                        if rewritten and len(rewritten) > 50:
                            logger.info(f"ReText.AI rewrite successful ({len(text)} -> {len(rewritten)} chars)")
                            return rewritten
                    else:
                        error = await resp.text()
                        logger.error(f"ReText.AI returned {resp.status}: {error}")

        except Exception as e:
            logger.error(f"ReText.AI rewrite failed: {e}")

        return None

    async def generate_hashtags(self, text: str) -> list[str]:
        """Generate relevant hashtags for a news post."""
        if not self._gemini_model:
            return []

        try:
            prompt = f"""Придумай 2-4 хэштега для этой новости на русском языке.

ПРАВИЛА:
1. Хэштег должен ТОЧНО соответствовать теме новости
2. ОБЯЗАТЕЛЬНО включи #Ижевск или #Удмуртия если новость про регион
3. Выбери тематический хэштег ТОЛЬКО если он явно подходит:
   - #экономика — цены, тарифы, бизнес, зарплаты
   - #происшествия — аварии, пожары, преступления, ЧП
   - #здоровье — медицина, больницы, лекарства
   - #образование — школы, университеты, дети
   - #транспорт — дороги, автобусы, метро
   - #жкх — ремонт, отопление, коммуналка
   - #спорт — ТОЛЬКО если прямо про соревнования или спортивные объекты
   - #общество — культура, события, люди
   - #закон — законы, суды, полиция
   - #технологии — IT, гаджеты, интернет
4. НЕ используй #спорт для новостей про здоровье, астрологию или алкоголь
5. Верни ТОЛЬКО хэштеги через пробел, без объяснений

Текст: {text[:400]}"""

            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                lambda: self._gemini_model.generate_content(
                    prompt,
                    generation_config=genai.GenerationConfig(
                        temperature=0.2,
                        max_output_tokens=60,
                    ),
                ),
            )

            if response and response.text:
                tags = [t.strip() for t in response.text.strip().split() if t.startswith("#")]
                return tags[:4]

        except Exception as e:
            logger.error(f"Hashtag generation failed: {e}")

        return []


    async def generate_keywords(self, text: str) -> list[str]:
        """Extract keywords from text for stock photo search."""
        if not self._gemini_model:
            return self._extract_keywords_fallback(text)

        try:
            prompt = f"""Ты помогаешь найти подходящее стоковое фото для новости.

Задача: придумай 3-4 ключевых слова НА АНГЛИЙСКОМ для поиска фото на Unsplash.

ПРАВИЛА:
- Слова должны описывать ВИЗУАЛЬНЫЙ ПРЕДМЕТ новости (что показать на фото)
- НЕ используй абстрактные слова (news, information, article)
- Используй конкретные, фотогеничные понятия
- Первое слово — ГЛАВНЫЙ объект новости

Примеры:
- Новость про строительство спорткомплекса → "sports complex, construction, building, gym"  
- Новость про пожар в доме → "fire, apartment building, firefighters, flames"
- Новость про зарплаты учителей → "teacher, classroom, school, education"
- Новость про ремонт дороги → "road construction, asphalt, highway, roadwork"
- Новость про QR-коды → "QR code, mobile payment, smartphone, digital"

Ответь ТОЛЬКО словами через запятую, без объяснений.

Текст: {text[:500]}"""

            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                lambda: self._gemini_model.generate_content(prompt),
            )

            if response and response.text:
                keywords = [kw.strip().lower() for kw in response.text.strip().split(",")]
                # Filter out generic/useless keywords
                bad_keywords = {"news", "information", "article", "report", "update", "story", "newspaper"}
                keywords = [kw for kw in keywords if kw and kw not in bad_keywords]
                return keywords[:5]

        except Exception as e:
            logger.error(f"Keyword extraction failed: {e}")

        # Fallback: topic-based keyword mapping without AI
        return self._extract_keywords_fallback(text)

    @staticmethod
    def _extract_keywords_fallback(text: str) -> list[str]:
        """Extract stock photo keywords using topic dictionary (no AI needed)."""
        text_lower = text.lower()
        topic_map = [
            (["пожар", "горит", "огонь", "возгорание"], ["fire", "firefighters", "flames"]),
            (["авария", "дтп", "столкновение"], ["car accident", "traffic", "road"]),
            (["полиция", "задержан", "арест", "преступ"], ["police", "law enforcement", "justice"]),
            (["больниц", "медицин", "врач", "здоровь"], ["hospital", "doctor", "healthcare"]),
            (["школ", "образован", "учител", "студент"], ["school", "education", "classroom"]),
            (["строительств", "ремонт", "дорог", "стройк"], ["construction", "road", "workers"]),
            (["экономик", "цен", "рубл", "инфляц", "зарплат"], ["economy", "finance", "money"]),
            (["погода", "снег", "мороз", "дождь"], ["weather", "winter", "nature"]),
            (["суд", "закон", "право"], ["court", "justice", "law"]),
            (["спорт", "матч", "команд", "чемпион"], ["sport", "competition", "athletes"]),
            (["армия", "воен", "солдат", "флот", "вмс", "нато"], ["military", "navy", "defense"]),
            (["нефт", "газ", "топлив", "энерг"], ["oil", "gas", "energy"]),
            (["выбор", "политик", "депутат", "власт"], ["politics", "government", "parliament"]),
            (["технолог", "цифров", "интернет"], ["technology", "digital", "innovation"]),
            (["жкх", "коммунал", "отоплен"], ["city infrastructure", "heating", "utilities"]),
            (["транспорт", "автобус", "трамвай"], ["public transport", "bus", "city"]),
        ]
        for keywords_ru, keywords_en in topic_map:
            if any(kw in text_lower for kw in keywords_ru):
                return keywords_en
        return ["city", "news", "building"]

    @staticmethod
    def _is_refusal(text: str) -> bool:
        """Check if AI response is a refusal/safety-filter message."""
        if not text:
            return True
        # Normalize ё→е so we don't miss variants like «чём» vs «чем»
        text_lower = text.lower().strip().replace("ё", "е")
        for phrase in REFUSAL_PHRASES:
            phrase_norm = phrase.replace("ё", "е")
            if phrase_norm in text_lower:
                return True
        # Reject if response is suspiciously short and sounds like a refusal
        if len(text_lower) < 120 and any(w in text_lower for w in (
            "не могу", "давайте", "не буду", "отказываюсь", "невозможно",
        )):
            return True
        return False

    def calculate_uniqueness(self, original: str, rewritten: str) -> float:
        """
        Simple uniqueness score based on word overlap.
        Returns a float 0.0-1.0 where 1.0 is completely unique.
        """
        original_words = set(original.lower().split())
        rewritten_words = set(rewritten.lower().split())

        if not original_words or not rewritten_words:
            return 0.0

        common = original_words & rewritten_words
        # Exclude common short words (prepositions, articles, etc.)
        common_significant = {w for w in common if len(w) > 3}
        total_significant = {w for w in original_words | rewritten_words if len(w) > 3}

        if not total_significant:
            return 1.0

        overlap = len(common_significant) / len(total_significant)
        return round(1.0 - overlap, 2)
