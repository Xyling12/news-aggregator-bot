"""
AI Rewriter — rewrites news text using Google Gemini API to avoid copyright issues.
Falls back to ReText.AI if Gemini is unavailable.
"""

import logging
import asyncio
from typing import Optional, Tuple

import google.generativeai as genai
from google.generativeai.types import HarmCategory, HarmBlockThreshold
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
SAFETY_SETTINGS = {
    HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
    HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
    HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
    HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
}

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


class AIRewriter:
    """Rewrites news text using AI to create unique content."""

    def __init__(self, config: Config):
        self.config = config
        self._gemini_model = None
        self._setup_gemini()

    def _setup_gemini(self):
        """Initialize Gemini API client."""
        if self.config.gemini_api_key:
            try:
                genai.configure(api_key=self.config.gemini_api_key)
                self._gemini_model = genai.GenerativeModel("gemini-2.0-flash")
                logger.info("Gemini API configured successfully")
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
            Tuple of (rewritten_text or None, engine_used: 'gemini' | 'retext' | 'error')
        """
        # Try Gemini first
        if self._gemini_model:
            result = await self._rewrite_with_gemini(original_text)
            if result:
                return result, "gemini"

        # Fallback to ReText.AI
        if self.config.retext_api_key:
            result = await self._rewrite_with_retext(original_text)
            if result:
                return result, "retext"

        logger.error("All AI rewrite engines failed")
        return None, "error"

    async def _rewrite_with_gemini(self, text: str) -> Optional[str]:
        """Rewrite text using Google Gemini with retry on rate limit."""
        max_retries = 3
        delays = [5, 15, 30]  # seconds between retries

        for attempt in range(max_retries):
            try:
                # On retry after low uniqueness, use stronger instruction
                if attempt > 0:
                    extra = "\n\n⚠️ ПРЕДУПРЕЖДЕНИЕ: Предыдущий рерайт был СЛИШКОМ ПОХОЖ на оригинал. ПОЛНОСТЬЮ ПЕРЕПИШИ КАЖДОЕ ПРЕДЛОЖЕНИЕ. Используй ДРУГИЕ слова, ДРУГУЮ структуру, ДРУГОЙ порядок фактов."
                    prompt = (REWRITE_PROMPT + extra).format(text=text)
                else:
                    prompt = REWRITE_PROMPT.format(text=text) if len(text) > 300 else REWRITE_SHORT_PROMPT.format(text=text)

                # Run sync Gemini call in executor to avoid blocking
                loop = asyncio.get_event_loop()
                response = await loop.run_in_executor(
                    None,
                    lambda: self._gemini_model.generate_content(
                        prompt,
                        generation_config=genai.GenerationConfig(
                            temperature=0.9,
                            max_output_tokens=2048,
                        ),
                        safety_settings=SAFETY_SETTINGS,
                    ),
                )

                if response and response.text:
                    rewritten = response.text.strip()
                    # Quality check: length + uniqueness
                    if len(rewritten) > 50 and rewritten != text:
                        uniqueness = self.calculate_uniqueness(text, rewritten)
                        logger.info(f"Gemini attempt {attempt+1}: uniqueness {uniqueness:.0%} ({len(text)} -> {len(rewritten)} chars)")
                        
                        if uniqueness >= 0.4:  # 40%+ uniqueness = OK
                            return rewritten
                        else:
                            logger.warning(f"Gemini rewrite too similar ({uniqueness:.0%}), retrying...")
                            if attempt < max_retries - 1:
                                await asyncio.sleep(2)
                                continue
                            else:
                                logger.warning("All retries exhausted with low uniqueness, using last result")
                                return rewritten
                    else:
                        logger.warning("Gemini returned too short or identical text")
                        return None
                else:
                    logger.warning(f"Gemini returned empty response (attempt {attempt+1})")
                    if response:
                        logger.warning(f"Response candidates: {response.candidates if hasattr(response, 'candidates') else 'none'}")
                    return None

            except Exception as e:
                error_str = str(e).lower()
                is_rate_limit = "429" in error_str or "rate" in error_str or "quota" in error_str or "resource" in error_str

                if is_rate_limit and attempt < max_retries - 1:
                    delay = delays[attempt]
                    logger.warning(f"Gemini rate limit (attempt {attempt + 1}/{max_retries}), retrying in {delay}s...")
                    await asyncio.sleep(delay)
                    continue
                else:
                    logger.error(f"Gemini rewrite failed: {e}")
                    return None

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
            prompt = f"""Придумай 2-4 хэштега для этой новости. Хэштеги должны быть на русском, релевантные и популярные.

Категории: #экономика #спорт #политика #ижевск #удмуртия #россия #происшествия #погода #общество #технологии #закон #здоровье #образование #транспорт

Верни ТОЛЬКО хэштеги через пробел, без объяснений.

Текст: {text[:400]}"""

            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                lambda: self._gemini_model.generate_content(
                    prompt,
                    generation_config=genai.GenerationConfig(
                        temperature=0.3,
                        max_output_tokens=50,
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
            return []

        try:
            prompt = f"""Извлеки 3-5 ключевых слов на английском из этого новостного текста для поиска подходящего стокового фото.
Верни ТОЛЬКО слова через запятую, без объяснений.

Текст: {text[:500]}"""

            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                lambda: self._gemini_model.generate_content(prompt),
            )

            if response and response.text:
                keywords = [kw.strip().lower() for kw in response.text.strip().split(",")]
                return keywords[:5]

        except Exception as e:
            logger.error(f"Keyword extraction failed: {e}")

        return []

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
