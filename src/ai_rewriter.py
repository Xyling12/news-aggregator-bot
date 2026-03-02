"""
AI Rewriter — rewrites news text using Google Gemini API to avoid copyright issues.
Falls back to ReText.AI if Gemini is unavailable.
"""

import logging
import asyncio
from typing import Optional, Tuple

import google.generativeai as genai
import aiohttp

from src.config import Config

logger = logging.getLogger(__name__)

# Prompt template for news rewriting
REWRITE_PROMPT = """Ты — профессиональный рерайтер новостей на русском языке. 

Твоя задача — полностью переписать текст новости, сохранив:
- Все ключевые факты и цифры
- Хронологию событий
- Имена и названия

При этом ты ДОЛЖЕН:
1. Полностью изменить структуру предложений
2. Использовать другие формулировки и синонимы  
3. Изменить порядок подачи информации
4. Сделать текст уникальным (не менее 80% уникальности)
5. Сохранить информативный стиль
6. НЕ добавлять своих комментариев или оценок
7. НЕ упоминать источник оригинальной новости
8. Написать новый заголовок (первая строка)

Формат ответа:
Первая строка — заголовок (жирным не выделять)
Далее — текст новости

Вот оригинальный текст для рерайта:

{text}"""

# Simpler prompt for shorter texts
REWRITE_SHORT_PROMPT = """Перепиши этот текст своими словами, сохранив смысл и факты. 
Измени формулировки и структуру предложений. Не добавляй комментариев.

{text}"""

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
        """Rewrite text using Google Gemini."""
        try:
            prompt = REWRITE_PROMPT.format(text=text) if len(text) > 300 else REWRITE_SHORT_PROMPT.format(text=text)

            # Run sync Gemini call in executor to avoid blocking
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                lambda: self._gemini_model.generate_content(
                    prompt,
                    generation_config=genai.GenerationConfig(
                        temperature=0.7,
                        max_output_tokens=2048,
                    ),
                ),
            )

            if response and response.text:
                rewritten = response.text.strip()
                # Basic quality check
                if len(rewritten) > 50 and rewritten != text:
                    logger.info(f"Gemini rewrite successful ({len(text)} -> {len(rewritten)} chars)")
                    return rewritten
                else:
                    logger.warning("Gemini returned too short or identical text")
                    return None
            else:
                logger.warning("Gemini returned empty response")
                return None

        except Exception as e:
            logger.error(f"Gemini rewrite failed: {e}")
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
