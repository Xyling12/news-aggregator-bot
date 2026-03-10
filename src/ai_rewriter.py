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

DEFAULT_GEMINI_MODEL_NAMES = [
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.5-pro",
    "gemini-2.0-flash",
    "gemini-2.0-flash-lite",
]


def _parse_binary_answer(answer: str) -> Optional[bool]:
    """Parse a strict YES/NO answer from a model response."""
    if not answer:
        return None

    normalized = answer.strip().lower()
    for char in ".!?:":
        normalized = normalized.replace(char, " ")
    tokens = [token for token in normalized.split() if token]
    if not tokens:
        return None

    yes_tokens = {"yes", "да", "true"}
    no_tokens = {"no", "нет", "false"}
    if tokens[0] in yes_tokens:
        return True
    if tokens[0] in no_tokens:
        return False
    if any(token in yes_tokens for token in tokens):
        return True
    if any(token in no_tokens for token in tokens):
        return False
    return None

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

# Prompt to check if news is relevant to Izhevsk/Udmurtia readers
RELEVANCE_PROMPT = """You decide whether a news item fits a regional channel about Izhevsk and Udmurtia.

Answer YES only if at least one statement is true:
- The text directly mentions Izhevsk, Udmurtia, the Udmurt Republic, or a city in Udmurtia.
- It is clearly nationwide news that directly affects residents in Izhevsk too, such as pensions, taxes, tariffs, benefits, or key-rate decisions.
- It is a rating, research, or major story where Izhevsk or Udmurtia is explicitly involved.

Answer NO if any statement is true:
- The story is about another region with no explicit connection to Izhevsk or Udmurtia.
- It is a local emergency, drone alert, accident, fire, utility issue, or appointment in another region.
- The text has no explicit local connection and is not clearly nationwide.

If unsure, answer NO.
Reply with exactly one word: YES or NO.

News text:
{text}"""


# Prompt to check if a news post is genuinely important/urgent for readers
URGENCY_PROMPT = """You decide whether a news item is important enough to publish in a regional news channel.

Answer YES if it is genuinely important, urgent, or useful:
- emergency, accident, fire, court case, arrest, outage, road closure, public safety warning
- a decision that clearly affects money, transport, utilities, schools, hospitals, or daily life
- a major local opening, closure, investigation, event, rating, or research result

Answer NO if it is mostly noise:
- press release, promotion, greeting, vague announcement, opinion without facts
- routine meeting or ceremonial event without concrete consequences
- recycled old news or trivial filler

Reply with exactly one word: YES or NO.

News text:
{text}"""


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
        self._current_key_index = 0  # Index of active API key
        self._setup_gemini()

    def _setup_gemini(self):
        """Initialize Gemini API client using the current key index."""
        keys = self.config.gemini_api_keys
        if not keys:
            logger.error("⚠️ No GEMINI API keys configured! AI rewrite will NOT work!")
            return

        key = keys[self._current_key_index]
        key_num = self._current_key_index + 1
        try:
            genai.configure(api_key=key)
            # Multiple models — each has separate free tier quota
            model_names = self._resolve_gemini_model_names()
            self._gemini_models = []
            for name in model_names:
                try:
                    model = genai.GenerativeModel(name)
                    self._gemini_models.append((name, model))
                except Exception as e:
                    logger.warning(f"Failed to init model {name}: {e}")

            if self._gemini_models:
                self._gemini_model = self._gemini_models[0][1]
                names = [m[0] for m in self._gemini_models]
                logger.info(f"Gemini API configured (key #{key_num}/{len(keys)}): {', '.join(names)}")
            else:
                logger.error(f"No Gemini models available for key #{key_num}!")
        except Exception as e:
            logger.error(f"Failed to configure Gemini API (key #{key_num}): {e}")

    def _switch_gemini_key(self) -> bool:
        """Switch to the next available Gemini API key. Returns True if switched."""
        keys = self.config.gemini_api_keys
        next_index = self._current_key_index + 1
        if next_index >= len(keys):
            logger.error(f"All {len(keys)} Gemini API key(s) exhausted!")
            return False
        self._current_key_index = next_index
        logger.warning(f"🔑 Switching to Gemini API key #{next_index + 1}/{len(keys)}")
        self._setup_gemini()
        return bool(self._gemini_models)

    def _resolve_gemini_model_names(self) -> list[str]:
        """Return configured Gemini model ids, filtered against ListModels when possible."""
        requested = self.config.gemini_model_names or DEFAULT_GEMINI_MODEL_NAMES

        deduped: list[str] = []
        seen = set()
        for name in requested:
            if name and name not in seen:
                deduped.append(name)
                seen.add(name)

        try:
            available = set()
            for model in genai.list_models():
                methods = getattr(model, "supported_generation_methods", []) or []
                if "generateContent" not in methods:
                    continue
                model_name = getattr(model, "name", "")
                if model_name.startswith("models/"):
                    model_name = model_name.split("/", 1)[1]
                if model_name:
                    available.add(model_name)

            matched = [name for name in deduped if name in available]
            missing = [name for name in deduped if name not in available]
            for name in missing:
                logger.warning(f"Gemini model not available for generateContent, skipping: {name}")
            if matched:
                return matched
            logger.warning("No configured Gemini models matched ListModels; using requested order without validation")
        except Exception as e:
            logger.warning(f"Gemini model validation via list_models failed: {e}")

        return deduped

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
                is_relevant = _parse_binary_answer(response.text)
                if is_relevant is None:
                    logger.warning(f"Relevance check returned ambiguous answer: {response.text!r}")
                    return False
                logger.info(f"Relevance check: {'✅ general' if is_relevant else '❌ regional'}")
                return is_relevant

        except Exception as e:
            logger.error(f"Relevance check failed: {e}")

        return False  # On error, be conservative

    async def check_urgency(self, text: str) -> bool:
        """
        Check if a news post is genuinely important/urgent for readers.
        Returns True if worth publishing, False if it's noise/fluff.
        """
        if not self._gemini_model:
            return True  # If no Gemini, let everything through

        try:
            prompt = URGENCY_PROMPT.format(text=text[:500])
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
                is_urgent = _parse_binary_answer(response.text)
                if is_urgent is None:
                    logger.warning(f"Urgency check returned ambiguous answer: {response.text!r}")
                    return False
                logger.info(f"Urgency check: {'✅ important' if is_urgent else '❌ skipped (not urgent)'}")
                return is_urgent

        except Exception as e:
            logger.error(f"Urgency check failed: {e}")

        return False  # On error, be conservative

    async def rewrite(self, original_text: str) -> Tuple[Optional[str], str]:
        """
        Rewrite text using AI.
        
        Returns:
            Tuple of (rewritten_text or None, engine_used: 'gemini' | 'groq' | 'yandexgpt' | 'retext' | 'error')
        """
        # Try Gemini first
        if self._gemini_model:
            result = await self._rewrite_with_gemini(original_text)
            if result:
                return result, "gemini"

        # Fallback to Groq
        if self.config.groq_api_key:
            result = await self._rewrite_with_groq(original_text)
            if result:
                return result, "groq"

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

    async def rewrite_full(
        self, original_text: str
    ) -> Tuple[Optional[str], str, list[str], list[str]]:
        """Efficient all-in-one: rewrite + hashtags + photo keywords in ONE Gemini call.

        Returns:
            (rewritten_text or None, engine_name, hashtags_list, photo_keywords_list)

        Falls back to calling rewrite()/generate_hashtags()/generate_keywords() separately
        if the combined prompt fails.
        """
        import re as _re

        COMBINED_PROMPT = f"""Ты — главный редактор Telegram-канала «Ижевск Сегодня».

Твоя задача по данной новости — выполнить ТРИ действия сразу и вернуть ответ СТРОГО в указанном формате.

ДЕЙСТВИЕ 1 — РЕРАЙТ:
Полностью перепиши новость своим языком. Правила:
- Заголовок (первая строка): чёткий, без кликбейта, до 80 символов
- 2-5 абзацев, живой язык, НЕ канцелярит
- Запрещено: «подписывайся», источник оригинала, реклама
- Используй эмодзи уместно (1-2 штуки)

ДЕЙСТВИЕ 2 — ХЭШТЕГИ:
2-4 хэштега для новости. Обязательно #Ижевск или #Удмуртия если про регион.
Допустимые тематические: #экономика #здоровье #образование #транспорт #жкх #общество #закон #технологии #политика

ДЕЙСТВИЕ 3 — КЛЮЧЕВЫЕ СЛОВА ДЛЯ ФОТО (на английском):
2-3 слова для поиска стокового фото. Описывай ЛЮДЕЙ в СИТУАЦИИ (не абстракции, не «news», не «technology»).
Примеры: "schoolchildren classroom", "doctor hospital patient", "road construction workers"

ФОРМАТ ОТВЕТА (строго, ничего лишнего кроме этих блоков):
РЕРАЙТ:
<переписанный текст>
ХЭШТЕГИ:
<хэштеги через пробел>
ФОТО:
<ключевые слова через запятую>

Исходная новость:
{original_text}"""

        # ── Try combined Gemini call ─────────────────────────────────────
        if self._gemini_models:
            loop = asyncio.get_event_loop()
            for model_name, model in self._gemini_models:
                try:
                    _prompt = COMBINED_PROMPT
                    _model = model
                    response = await loop.run_in_executor(
                        None,
                        lambda: _model.generate_content(
                            _prompt,
                            generation_config=genai.GenerationConfig(
                                temperature=0.85,
                                max_output_tokens=2048,
                            ),
                            safety_settings=SAFETY_SETTINGS if _HAS_SAFETY else None,
                        ),
                    )

                    if response and response.text:
                        raw = response.text.strip()

                        # Parse РЕРАЙТ block
                        rewrite_match = _re.search(
                            r'РЕРАЙТ:\s*\n(.*?)(?=\nХЭШТЕГИ:|$)',
                            raw, _re.DOTALL
                        )
                        hashtag_match = _re.search(
                            r'ХЭШТЕГИ:\s*\n(.*?)(?=\nФОТО:|$)',
                            raw, _re.DOTALL
                        )
                        photo_match = _re.search(
                            r'ФОТО:\s*\n(.*?)$',
                            raw, _re.DOTALL
                        )

                        rewritten = rewrite_match.group(1).strip() if rewrite_match else None
                        hashtags_raw = hashtag_match.group(1).strip() if hashtag_match else ""
                        photo_raw = photo_match.group(1).strip() if photo_match else ""

                        if rewritten and not self._is_refusal(rewritten) and len(rewritten) > 50:
                            # Parse hashtags
                            hashtags = [
                                t.strip() for t in hashtags_raw.split()
                                if t.strip().startswith("#")
                            ][:4]

                            # Parse photo keywords
                            bad_keywords = {
                                "news", "information", "article", "report", "update",
                                "abstract", "concept", "technology", "digital", "modern",
                                "bell", "ring", "sound", "alarm", "signal",
                                "sport", "fitness", "gym", "workout", "climbing",
                            }
                            photo_keywords = [
                                kw.strip().lower()
                                for kw in photo_raw.split(",")
                                if kw.strip() and kw.strip().lower() not in bad_keywords
                            ][:3]

                            uniqueness = self.calculate_uniqueness(original_text, rewritten)
                            logger.info(
                                f"rewrite_full [{model_name}]: OK — "
                                f"uniqueness {uniqueness:.0%}, "
                                f"{len(hashtags)} tags, {len(photo_keywords)} photo kw "
                                f"(1 API call instead of 3)"
                            )
                            return rewritten, model_name, hashtags, photo_keywords

                        logger.warning(f"rewrite_full [{model_name}]: parse failed or refusal, raw={raw[:100]}")

                except Exception as e:
                    err = str(e).lower()
                    if "429" in err or "quota" in err or "resource" in err:
                        logger.warning(f"rewrite_full [{model_name}]: quota hit, trying next model")
                        continue
                    logger.error(f"rewrite_full [{model_name}]: {e}")

        # ── Fallback: call old methods separately ────────────────────────
        logger.warning("rewrite_full: combined call failed, falling back to separate methods")
        rewritten, engine = await self.rewrite(original_text)
        hashtags = await self.generate_hashtags(rewritten or original_text)
        photo_keywords = await self.generate_keywords(original_text)
        return rewritten, engine, hashtags, photo_keywords

    async def ask_ai(self, prompt: str, temperature: float = 0.8, _key_switched: bool = False) -> Optional[str]:
        """Generic AI text generation with Gemini→Groq→YandexGPT fallback.

        Used by ContentGenerator for rubric posts (weather, recipe, facts, etc.).
        """
        import re as _re

        # Try Gemini first — loop through all models on the current key
        if self._gemini_models:
            all_quota = True  # Track whether ALL failures were quota-related
            try:
                loop = asyncio.get_event_loop()
                for _name, model in self._gemini_models:
                    try:
                        response = await loop.run_in_executor(
                            None,
                            lambda m=model: m.generate_content(
                                prompt,
                                generation_config=genai.GenerationConfig(
                                    temperature=temperature,
                                    max_output_tokens=2048,
                                ),
                                safety_settings=SAFETY_SETTINGS if _HAS_SAFETY else None,
                            ),
                        )
                        if response and response.text:
                            text = response.text.strip()
                            text = _re.sub(r'^#{1,3}\s*', '', text, flags=_re.MULTILINE)
                            text = _re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
                            logger.info(f"ask_ai: Gemini/{_name} OK ({len(text)} chars)")
                            return text
                        all_quota = False
                    except Exception as gem_err:
                        err_str = str(gem_err).lower()
                        if "quota" in err_str or "limit" in err_str or "429" in err_str:
                            logger.warning(f"ask_ai: {_name} quota hit, trying next")
                            continue
                        logger.warning(f"ask_ai: {_name} error: {gem_err}")
                        all_quota = False
            except Exception as e:
                logger.warning(f"ask_ai: Gemini failed: {e}")
                all_quota = False

            # All models on current key hit quota — try switching to next key
            if all_quota and not _key_switched:
                if self._switch_gemini_key():
                    logger.info("ask_ai: switched to next Gemini key, retrying...")
                    return await self.ask_ai(prompt, temperature, _key_switched=True)

        # Fallback to Groq
        if self.config.groq_api_key:
            text = await self._groq_chat(
                prompt=prompt,
                temperature=temperature,
                max_tokens=2048,
            )
            if text and len(text) > 20:
                logger.info(f"ask_ai: Groq fallback OK ({len(text)} chars)")
                return text

        # Fallback to YandexGPT
        if self.config.yandex_api_key and self.config.yandex_folder_id:
            try:
                import aiohttp as _aiohttp
                url = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"
                headers = {
                    "Authorization": f"Api-Key {self.config.yandex_api_key}",
                    "Content-Type": "application/json",
                }
                model_uri = f"gpt://{self.config.yandex_folder_id}/yandexgpt-lite/latest"
                body = {
                    "modelUri": model_uri,
                    "completionOptions": {"stream": False, "temperature": temperature, "maxTokens": "2048"},
                    "messages": [
                        {"role": "system", "text": "Ты — автор Telegram-канала «Ижевск Сегодня». Пиши живо, по-человечески."},
                        {"role": "user", "text": prompt},
                    ],
                }
                timeout = _aiohttp.ClientTimeout(total=30)
                async with _aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.post(url, json=body, headers=headers) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            alternatives = data.get("result", {}).get("alternatives", [])
                            if alternatives:
                                text = alternatives[0].get("message", {}).get("text", "").strip()
                                if text and len(text) > 30:
                                    logger.info(f"ask_ai: YandexGPT fallback OK ({len(text)} chars)")
                                    return text
                        else:
                            logger.error(f"ask_ai: YandexGPT returned {resp.status}")
            except Exception as e:
                logger.error(f"ask_ai: YandexGPT failed: {e}")

        logger.error("ask_ai: all engines failed")
        return None

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

        # All models on current key exhausted — try next key
        if self._switch_gemini_key():
            return await self._rewrite_with_gemini(text)

        logger.warning("All Gemini API keys exhausted, falling back to YandexGPT...")
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

            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout) as session:
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

    async def _groq_chat(self, prompt: str, temperature: float, max_tokens: int) -> Optional[str]:
        """Generic Groq chat completion helper (OpenAI-compatible endpoint)."""
        if not self.config.groq_api_key:
            return None

        url = "https://api.groq.com/openai/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.config.groq_api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.config.groq_model,
            "messages": [
                {"role": "system", "content": "You are a concise Russian news editor for a Telegram channel."},
                {"role": "user", "content": prompt},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        try:
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, json=payload, headers=headers) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        choices = data.get("choices", [])
                        if not choices:
                            return None
                        message = choices[0].get("message", {}) or {}
                        content = (message.get("content") or "").strip()
                        return content or None
                    error_text = await resp.text()
                    logger.warning(f"Groq returned {resp.status}: {error_text[:200]}")
        except Exception as e:
            logger.warning(f"Groq request failed: {e}")

        return None

    async def _rewrite_with_groq(self, text: str) -> Optional[str]:
        """Rewrite text using Groq API."""
        prompt = REWRITE_PROMPT.format(text=text) if len(text) > 300 else REWRITE_SHORT_PROMPT.format(text=text)
        rewritten = await self._groq_chat(prompt=prompt, temperature=0.9, max_tokens=2048)
        if not rewritten:
            return None
        if self._is_refusal(rewritten):
            logger.warning(f"Groq: refusal detected: {rewritten[:80]}")
            return None
        if len(rewritten) <= 50 or rewritten == text:
            logger.warning("Groq: too short or identical")
            return None

        uniqueness = self.calculate_uniqueness(text, rewritten)
        logger.info(f"Groq: uniqueness {uniqueness:.0%} ({len(text)} -> {len(rewritten)} chars)")
        return rewritten

    async def _rewrite_with_retext(self, text: str) -> Optional[str]:
        """Rewrite text using ReText.AI API (fallback)."""
        try:
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout) as session:
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
   - #здоровье — медицина, больницы, лекарства
   - #образование — школы, университеты, дети
   - #транспорт — дороги, автобусы, метро
   - #жкх — ремонт, отопление, коммуналка
   - #общество — культура, события, люди
   - #закон — законы, суды, полиция
   - #технологии — IT, гаджеты, интернет
   - #политика — власть, решения, чиновники
4. СТРОГО ЗАПРЕЩЕНО использовать: #погода #происшествия #спорт #срочно
   (эти рубрики назначаются автоматически по содержанию, не тобой)
5. НЕ используй хэштеги не из списка выше
6. Верни ТОЛЬКО хэштеги через пробел, без объяснений

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

    async def generate_poll_options(self, text: str) -> dict:
        """Generate a native Telegram poll question + emoji options for a news post.

        Returns a dict with:
            question (str): short poll question (max 300 chars)
            options (list[str]): 3-4 emoji answer options (max 100 chars each)

        Falls back to topic-based presets if Gemini is unavailable.
        """
        # ── Fallback presets by topic ─────────────────────────────────────
        def _fallback(text_lower: str) -> dict:
            if any(w in text_lower for w in ["жкх", "коммунал", "отоплен", "тариф", "ремонт"]):
                return {"question": "Как вы к этому относитесь?",
                        "options": ["😡 Позор", "🤷 Привыкли", "😂 Ожидаемо", "👍 Нормально"]}
            if any(w in text_lower for w in ["чиновник", "мэр", "власт", "депутат", "администрац"]):
                return {"question": "Ваша реакция?",
                        "options": ["🤦 Без комментариев", "😡 Возмутительно", "😐 Как всегда", "👏 Молодцы"]}
            if any(w in text_lower for w in ["дтп", "авария", "пожар", "происшеств", "погиб"]):
                return {"question": "Как вы?",
                        "options": ["😢 Сочувствую", "😱 Шок", "🙏 Надеюсь все живы"]}
            if any(w in text_lower for w in ["строительств", "открыт", "новый", "запуст"]):
                return {"question": "Как вам новость?",
                        "options": ["🔥 Отлично!", "🤔 Посмотрим", "😐 Всё равно", "👎 Не верю"]}
            if any(w in text_lower for w in ["цен", "рост", "подорожал", "инфляц", "зарплат"]):
                return {"question": "Как ощущаете на кармане?",
                        "options": ["💸 Уже больно", "😬 Скоро почувствую", "🤷 Пока норм", "🫡 Держимся"]}
            # Default
            return {"question": "Что думаете?",
                    "options": ["🔥 Важно!", "😐 Норм", "🥱 Неинтересно", "😱 Вот это да"]}

        text_lower = text.lower()

        if not self._gemini_model:
            return _fallback(text_lower)

        try:
            prompt = f"""Ты создаёшь интерактивный опрос для Telegram-канала под новость.

ЗАДАЧА: придумай короткий вопрос и 3-4 варианта ответа с эмодзи — так, чтобы читатель не думал долго и сразу нажал.

ПРАВИЛА:
- Вопрос: max 60 символов, разговорный тон
- Варианты: каждый начинается с ОДНОГО эмодзи, потом 2-4 слова, max 30 символов
- Варианты должны охватывать разные эмоции/позиции
- НЕ используй нейтральные корпоративные формулировки
- Учитывай тему: для ЖКХ/чиновников — с иронией, для трагедий — сочувственно, для событий — позитивно

ТЕМЫ-ПОДСКАЗКИ:
- ЖКХ/чиновники → ирония, желчь
- ДТП/пожар/трагедия → эмпатия, без юмора
- Стройка/открытие → оптимизм vs скептицизм
- Цены/тарифы → боль, юмор про кошелёк

ФОРМАТ ОТВЕТА (строго, ничего лишнего):
ВОПРОС: <текст вопроса>
ВАРИАНТЫ:
😡 Текст варианта 1
🤷 Текст варианта 2
😂 Текст варианта 3
👍 Текст варианта 4

Новость: {text[:400]}"""

            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                lambda: self._gemini_model.generate_content(
                    prompt,
                    generation_config=genai.GenerationConfig(
                        temperature=0.7,
                        max_output_tokens=200,
                    ),
                ),
            )

            if response and response.text:
                raw = response.text.strip()
                lines = [l.strip() for l in raw.splitlines() if l.strip()]

                question = ""
                options = []
                in_options = False

                for line in lines:
                    if line.upper().startswith("ВОПРОС:"):
                        question = line.split(":", 1)[1].strip()
                    elif "ВАРИАНТЫ" in line.upper():
                        in_options = True
                    elif in_options and line:
                        options.append(line[:100])

                if question and len(options) >= 2:
                    logger.info(f"Poll generated: '{question}' with {len(options)} options")
                    return {"question": question[:300], "options": options[:4]}

        except Exception as e:
            logger.error(f"Poll generation failed: {e}")

        return _fallback(text_lower)


    async def generate_keywords(self, text: str) -> list[str]:
        """Extract keywords from text for stock photo search."""
        if not self._gemini_model:
            return self._extract_keywords_fallback(text)

        try:
            prompt = f"""Ты помогаешь найти подходящее стоковое фото для новости.

Задача: придумай 2-3 ключевых слова НА АНГЛИЙСКОМ для поиска фото на Pixabay.

ГЛАВНОЕ ПРАВИЛО: ключевые слова должны описывать СЦЕНУ С ЛЮДЬМИ или МЕСТО — то, что фотогенично и легко найти на стоке.

ЗАПРЕЩЕНО использовать:
- Названия звуков или абстрактных понятий: bell, ring, sound, noise, alarm, signal
- Абстракции: news, information, article, report, technology, digital, modern
- Технические объекты, которые плохо смотрятся: pipe, tube, wire, equipment (если не это главная суть)
- Слова, которые дадут технические фото вместо репортажных
- ЗАПРЕЩЕНО: military vehicle, air force truck, US army, NATO vehicle, warship, fighter jet, missile — иностранная военная техника

ПРАВИЛА:
- Описывай ЛЮДЕЙ в СИТУАЦИИ, а не объект (teacher NOT bell, students NOT ring)
- Используй конкретные сцены: "smiling schoolchildren", "classroom russia", не просто "school"
- 2-3 слова через запятую

Примеры:
- Новость про звонки в школах с песнями → "schoolchildren classroom, students happy school"
- Новость про строительство спорткомплекса → "sports complex construction, gym building workers"
- Новость про пожар в доме → "firefighters apartment fire, rescue workers"
- Новость про зарплаты учителей → "teacher classroom, school education russia"
- Новость про ремонт дороги → "road construction workers, highway asphalt"
- Новость про застройщиков мошенников → "fraud court justice, police handcuffs"
- Новость про погоду / сугробы / снег → "snowy city street, people winter snow" (НЕ "snow tire chains")
- Новость про больницу/медицину → "doctor hospital patient, medical workers"
- Новость про БПЛА / опасное небо / угрозу атаки → "school evacuation drill, civil defense warning, children safety" (НЕ "military vehicle, air force")
- Новость про военное положение/обстрелы → "bomb shelter interior, citizens bunker, safety drill"

Ответь ТОЛЬКО словами через запятую, без объяснений.

Текст: {text[:500]}"""

            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                lambda: self._gemini_model.generate_content(prompt),
            )

            if response and response.text:
                keywords = [kw.strip().lower() for kw in response.text.strip().split(",")]
                # Filter out generic/useless/dangerous keywords that lead to wrong photos
                bad_keywords = {
                    # Generic/meta
                    "news", "information", "article", "report", "update", "story", "newspaper",
                    # Sound/tech objects
                    "bell", "ring", "sound", "alarm", "signal", "noise", "tone", "ringtone",
                    "pipe", "tube", "wire", "equipment",
                    # Abstract/design
                    "abstract", "concept", "symbol", "icon", "background", "digital", "modern",
                    "technology",
                    # Sport/fitness — these get triggered by «движение» (movement/traffic)
                    "climbing", "sport", "fitness", "gym", "exercise", "athlete", "workout",
                    "athletic", "mountain", "boulder", "competition", "race", "runner",
                    "jump", "jumping", "sports", "movement", "motion", "activity",
                    # Foreign military hardware — causes US/NATO vehicle photos
                    "military vehicle", "air force", "military truck", "warplane", "fighter jet",
                    "military aircraft", "warship", "army vehicle", "military equipment",
                    "missile launcher", "armored vehicle", "military strike", "drone attack",
                }
                keywords = [kw for kw in keywords if kw and kw not in bad_keywords]
                # If AI returned only bad words, fall through to reliable fallback
                if not keywords:
                    logger.warning("generate_keywords: all AI keywords were filtered, using fallback")
                    return self._extract_keywords_fallback(text)
                return keywords[:3]

        except Exception as e:
            logger.error(f"Keyword extraction failed: {e}")

        # Fallback: topic-based keyword mapping without AI
        return self._extract_keywords_fallback(text)

    async def check_photo_relevance(self, news_text: str, photo_url: str) -> bool:
        """Check if a stock photo is relevant to the given news text using Gemini vision.

        Downloads the photo and asks Gemini multimodal whether the image matches
        the news topic. If Gemini is not available or any error occurs, returns True
        (accept the photo) to avoid blocking post publication.

        Returns:
            True  — photo is relevant, use it
            False — photo is irrelevant, try another or publish without photo
        """
        if not self._gemini_model:
            return True  # No model → accept all photos

        try:
            from io import BytesIO
            import PIL.Image

            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(photo_url) as resp:
                    if resp.status != 200:
                        return True  # Can't download → accept
                    image_bytes = await resp.read()

            img = PIL.Image.open(BytesIO(image_bytes))

            prompt = (
                f"Посмотри на это фото и скажи, подходит ли оно к данной новости.\n\n"
                f"Новость: {news_text[:300]}\n\n"
                "Фото НЕ подходит если:\n"
                "- Оно про совершенно другую тему (спорт для новости о транспорте, "
                "природа для новости о суде, скалолазание для новости о троллейбусах)\n"
                "- Изображение технической схемы/диаграммы для обычной новости\n"
                "Фото ПОДХОДИТ если оно хотя бы отдалённо соответствует теме.\n\n"
                "Ответь ТОЛЬКО одним словом: ДА или НЕТ."
            )

            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                lambda: self._gemini_model.generate_content(
                    [prompt, img],
                    generation_config=genai.GenerationConfig(
                        temperature=0.0,
                        max_output_tokens=5,
                    ),
                ),
            )

            if response and response.text:
                answer = response.text.strip().upper()
                is_relevant = "ДА" in answer
                logger.info(f"Photo relevance check: {'✅ relevant' if is_relevant else '❌ NOT relevant'} — {photo_url[:60]}")
                return is_relevant

        except ImportError:
            logger.warning("PIL not available for photo relevance check")
        except Exception as e:
            logger.error(f"Photo relevance check failed: {e}")

        return True  # On any error → accept photo to avoid blocking publication


    async def check_photo_relevance_safe(self, news_text: str, photo_url: str) -> bool:
        """Safer photo relevance check using an ASCII-only prompt and strict parsing."""
        if not self._gemini_model:
            return False

        try:
            from io import BytesIO
            import PIL.Image

            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(photo_url) as resp:
                    if resp.status != 200:
                        return False
                    image_bytes = await resp.read()

            img = PIL.Image.open(BytesIO(image_bytes))
            prompt = (
                "Decide whether this photo is relevant for the news item.\n\n"
                f"News text: {news_text[:300]}\n\n"
                "Answer NO if the image is about a clearly different topic or is just a technical object shot that "
                "would look wrong for a normal news post.\n"
                "Answer YES if it is at least reasonably close to the topic or mood.\n\n"
                "Reply with exactly one word: YES or NO."
            )

            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                lambda: self._gemini_model.generate_content(
                    [prompt, img],
                    generation_config=genai.GenerationConfig(
                        temperature=0.0,
                        max_output_tokens=5,
                    ),
                ),
            )

            if response and response.text:
                parsed = _parse_binary_answer(response.text)
                if parsed is None:
                    logger.warning(f"Photo relevance returned ambiguous answer: {response.text!r}")
                    return False
                return parsed

        except ImportError:
            logger.warning("PIL not available for photo relevance check")
        except Exception as e:
            logger.error(f"Safe photo relevance check failed: {e}")

        return False

    @staticmethod
    def _extract_keywords_fallback(text: str) -> list[str]:
        """Extract stock photo keywords using topic dictionary (no AI needed).

        Uses a scored approach: counts keyword hits per topic and picks
        the best-matching category, so incidental mentions (e.g. «в больнице»
        at the end of a snow-rescue story) don't override the main topic.
        """
        text_lower = text.lower()
        topic_map = [
            # Emergency / rescue — check first, very specific
            (["спасен", "спасатель", "пропав", "застрял", "поиск"],
             ["rescue", "search rescue", "emergency team"]),
            (["пожар", "горит", "огонь", "возгорание"],
             ["fire", "firefighters", "flames"]),
            (["авария", "дтп", "столкновение"],
             ["car accident", "traffic", "road"]),
            (["взрыв", "взрывч", "беспилот", "атак", "обстрел", "опасное небо", "бпла", "угроз"],
             ["civil defense siren", "school evacuation", "safety warning"]),
            (["полиция", "задержан", "арест", "преступ"],
             ["police", "law enforcement", "justice"]),
            # Nature / weather — before hospital so «осмотрели в больнице» doesn't win
            (["снег", "мороз", "пурга", "метель", "снежн"],
             ["winter", "snow", "blizzard"]),
            (["погода", "дождь", "гроза", "оттепел", "похолодан"],
             ["weather", "rain", "nature"]),
            # Infrastructure
            (["строительств", "ремонт", "дорог", "стройк"],
             ["construction", "road", "workers"]),
            (["жкх", "коммунал", "отоплен", "водоснабжен"],
             ["city infrastructure", "heating", "utilities"]),
            (["транспорт", "автобус", "трамвай"],
             ["public transport", "bus", "city"]),
            # Society
            (["школ", "образован", "учител", "студент"],
             ["school", "education", "classroom"]),
            (["больниц", "медицин", "врач", "хирург"],
             ["hospital", "doctor", "healthcare"]),
            (["экономик", "цен", "рубл", "инфляц", "зарплат"],
             ["economy", "finance", "money"]),
            (["суд", "закон", "право"],
             ["court", "justice", "law"]),
            (["спорт", "матч", "команд", "чемпион"],
             ["sport", "competition", "athletes"]),
            # Geopolitics — use neutral visuals, NOT foreign military equipment
            (["армия", "воен", "солдат", "флот", "вмс", "нато"],
             ["government meeting", "security officials", "ministry building"]),
            (["нефт", "газ", "топлив", "энерг"],
             ["oil", "gas", "energy"]),
            (["выбор", "политик", "депутат", "власт"],
             ["politics", "government", "parliament"]),
            (["технолог", "цифров", "интернет"],
             ["technology", "digital", "innovation"]),
        ]
        # Score each topic by number of keyword hits
        best_score = 0
        best_keywords = ["city", "news", "building"]
        for keywords_ru, keywords_en in topic_map:
            score = sum(1 for kw in keywords_ru if kw in text_lower)
            if score > best_score:
                best_score = score
                best_keywords = keywords_en
        return best_keywords

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
