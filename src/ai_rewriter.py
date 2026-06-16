"""
AI Rewriter — rewrites news text using Google Gemini API to avoid copyright issues.
Falls back to ReText.AI if Gemini is unavailable.
"""

import logging
import asyncio
from typing import Optional, Tuple

from google import genai
from google.genai import types
_HAS_SAFETY = True  # Always available in google-genai SDK
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
REWRITE_PROMPT = """Ты — живой редактор новостного Telegram-канала «Ижевск Сегодня». Пишешь для жителей Ижевска — просто, по-человечески, без канцелярита.

ЗАДАЧА: полностью перепиши новость своими словами. Читатель не должен узнать оригинал.

КАК ПИСАТЬ:
— Первая строка: один эмодзи по теме + короткий заголовок (до 10 слов). Заголовок должен цеплять, но не кричать
— Пиши как живой человек, не как пресс-служба. «Аэропорт снова принимает рейсы» лучше чем «сняты ограничения на вылет и прилёт авиарейсов»
— Короткие абзацы. Одна мысль — один абзац
— Числа, даты, имена — выделяй **жирным**
— Можно начать с вопроса, восклицания или неожиданного факта
— 3-5 абзацев, не больше

ЗАПРЕЩЕНО:
— Копировать фразы из оригинала
— «В Ижевске рады сообщить», «отмечается», «по имеющимся данным», «сообщается» — это канцелярит
— Упоминать источник
— «Подпишись», «читайте на сайте»
— Markdown-заголовки (# ##)
— Эмодзи 😀😊😄😂🎉 в новостях о гибели, трагедиях, ДТП, пожарах

Перепиши:

{text}"""

# Simpler prompt for shorter texts
REWRITE_SHORT_PROMPT = """Ты — редактор Telegram-канала «Ижевск Сегодня». Пиши как живой человек, не как робот.

Перепиши новость СВОИМИ СЛОВАМИ:
— Эмодзи + цепляющий заголовок (первая строка)
— Короткие абзацы, живой язык
— **Жирный** для цифр, дат, имён
— Без копипасты оригинала
— Без упоминания источника

{text}"""

# Safety settings — allow all content (news about accidents/crime gets blocked otherwise)
SAFETY_SETTINGS = [
    types.SafetySetting(category="HARM_CATEGORY_HARASSMENT", threshold="BLOCK_NONE"),
    types.SafetySetting(category="HARM_CATEGORY_HATE_SPEECH", threshold="BLOCK_NONE"),
    types.SafetySetting(category="HARM_CATEGORY_SEXUALLY_EXPLICIT", threshold="BLOCK_NONE"),
    types.SafetySetting(category="HARM_CATEGORY_DANGEROUS_CONTENT", threshold="BLOCK_NONE"),
]

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


# Prompt to generate reader engagement (first comment + optional poll) for a VK post
ENGAGEMENT_PROMPT = """Ты — SMM-редактор паблика «Ижевск Сегодня». По новости ниже придумай вовлечение для читателей VK.

Верни СТРОГО JSON без пояснений и без текста вокруг:
{{"comment": "...", "poll": {{"question": "...", "options": ["...", "..."]}}}}

Правила:
- comment: одна живая короткая фраза-вопрос ОТ ЛИЦА паблика, побуждает написать в комментарии. Без ссылок и хэштегов. Пример: «А вы заметили подорожание? Делитесь в комментариях 👇»
- poll: короткий опрос по теме новости. question — до 100 символов; options — 2–4 коротких варианта (до 40 символов каждый).
- ❗ ЕСЛИ новость про гибель, трагедию, ДТП с пострадавшими, пожар/утопление с жертвами, похороны, тяжёлое ЧП, болезни — НЕ делай опрос: верни "poll": null, а comment сделай мягким/сочувственным или тоже null.
- Только русский язык. Ничего, кроме JSON.

Новость:
{text}"""


# Prompt to generate a unique, neutral outreach comment for another community's post
OUTREACH_COMMENT_PROMPT = """Ты — обычный неравнодушный житель Ижевска, читаешь местный паблик. Напиши ОДИН короткий живой комментарий к посту ниже.

Правила:
- 1 предложение, по-человечески и строго по теме поста.
- Нейтрально-доброжелательно или с уместным личным мнением. Без негатива, политики, оскорблений.
- БЕЗ ссылок, БЕЗ рекламы, БЕЗ упоминания других пабликов или каналов.
- НЕ используй шаблоны вроде «спасибо за информацию» — пиши естественно и по сути.
- Верни только текст комментария, без кавычек и пояснений.

Пост:
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


class _GenConfig:
    """Drop-in shim for the old genai.GenerationConfig."""
    def __init__(self, temperature=None, max_output_tokens=None, top_p=None, top_k=None, **kw):
        self.temperature = temperature
        self.max_output_tokens = max_output_tokens
        self.top_p = top_p
        self.top_k = top_k
        for k, v in kw.items():
            setattr(self, k, v)


class _GeminiWrapper:
    """Wraps google.genai Client to match the old GenerativeModel interface."""

    def __init__(self, client: "genai.Client", model_name: str):
        self._client = client
        self._name = model_name

    def generate_content(self, prompt, generation_config=None, safety_settings=None):
        cfg_kw: dict = {"safety_settings": safety_settings if safety_settings is not None else SAFETY_SETTINGS}
        if generation_config is not None:
            for attr in ("temperature", "max_output_tokens", "top_p", "top_k"):
                val = getattr(generation_config, attr, None)
                if val is not None:
                    cfg_kw[attr] = val

        # Handle multimodal input (list of [text, PIL.Image])
        if isinstance(prompt, list):
            parts = []
            for item in prompt:
                if isinstance(item, str):
                    parts.append(types.Part.from_text(text=item))
                else:
                    try:
                        from io import BytesIO
                        buf = BytesIO()
                        item.save(buf, format="JPEG")
                        parts.append(types.Part.from_bytes(data=buf.getvalue(), mime_type="image/jpeg"))
                    except Exception:
                        pass
            contents = [types.Content(role="user", parts=parts)]
        else:
            contents = prompt

        return self._client.models.generate_content(
            model=self._name,
            contents=contents,
            config=types.GenerateContentConfig(**cfg_kw),
        )


class AIRewriter:
    """Rewrites news text using AI to create unique content."""

    def __init__(self, config: Config):
        self.config = config
        self._gemini_model = None
        self._gemini_models = []  # Fallback models list
        self._current_key_index = 0  # Index of active API key

        # ── Circuit Breaker — auto-bypass Gemini when it keeps failing ─────
        # After _CB_MAX_ERRORS errors within _CB_WINDOW_SECONDS, circuit OPENS:
        # all requests go straight to YandexGPT/Groq, saving time and quota.
        # Resets automatically at midnight (new day = fresh quota).
        self._cb_error_times: list = []      # timestamps of recent Gemini errors
        self._CB_MAX_ERRORS: int = 3         # trip after this many errors
        self._CB_WINDOW_SECONDS: int = 3600  # within this window (1 hour)
        self._cb_open_until: float = 0.0     # monotonic time when circuit re-closes

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
            client = genai.Client(api_key=key)
            # Multiple models — each has separate free tier quota
            model_names = self._resolve_gemini_model_names(client)
            self._gemini_models = []
            for name in model_names:
                try:
                    wrapper = _GeminiWrapper(client, name)
                    self._gemini_models.append((name, wrapper))
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

    def _gemini_circuit_open(self) -> bool:
        """Return True if Gemini circuit breaker is open (too many recent errors).

        When open, callers should skip Gemini entirely and go to YandexGPT/Groq.
        """
        import time
        import datetime

        now = time.monotonic()

        # Auto-reset at midnight (new day = fresh quota)
        midnight_reset = datetime.datetime.now().replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        seconds_since_midnight = (datetime.datetime.now() - midnight_reset).total_seconds()
        if seconds_since_midnight < 60:  # It's a new day — reset breaker
            self._cb_error_times.clear()
            self._cb_open_until = 0.0

        # Still in penalty window?
        if now < self._cb_open_until:
            return True

        # Purge errors older than the window
        cutoff = now - self._CB_WINDOW_SECONDS
        self._cb_error_times = [t for t in self._cb_error_times if t > cutoff]

        return False  # Circuit is closed

    def _cb_record_error(self) -> None:
        """Record a Gemini error. Opens circuit if threshold exceeded."""
        import time
        now = time.monotonic()
        self._cb_error_times.append(now)
        cutoff = now - self._CB_WINDOW_SECONDS
        recent = [t for t in self._cb_error_times if t > cutoff]
        self._cb_error_times = recent

        if len(recent) >= self._CB_MAX_ERRORS:
            # Open circuit for 60 minutes
            self._cb_open_until = now + 3600
            logger.warning(
                f"⚡ Gemini Circuit Breaker OPENED after {len(recent)} errors in 1h. "
                f"Skipping Gemini for 60 min → YandexGPT/Groq will handle all requests."
            )

    def _resolve_gemini_model_names(self, client=None) -> list[str]:
        """Return configured Gemini model ids, filtered against ListModels when possible."""
        requested = self.config.gemini_model_names or DEFAULT_GEMINI_MODEL_NAMES

        deduped: list[str] = []
        seen = set()
        for name in requested:
            if name and name not in seen:
                deduped.append(name)
                seen.add(name)

        if client:
            try:
                available = set()
                for model in client.models.list():
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
        prompt = RELEVANCE_PROMPT.format(text=text[:500])

        # Try AITUNNEL first
        if self.config.aitunnel_api_key:
            try:
                answer = await self._aitunnel_chat(prompt=prompt, temperature=0.1, max_tokens=10)
                if answer:
                    is_relevant = _parse_binary_answer(answer)
                    if is_relevant is not None:
                        logger.info(f"Relevance check [AITUNNEL]: {'✅ general' if is_relevant else '❌ regional'}")
                        return is_relevant
            except Exception as e:
                logger.warning(f"Relevance check [AITUNNEL] failed: {e}")

        # Fallback to Gemini
        if not self._gemini_model or self._gemini_circuit_open():
            return True  # If no AI, let everything through

        try:
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                lambda: self._gemini_model.generate_content(
                    prompt,
                    generation_config=_GenConfig(
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
        prompt = URGENCY_PROMPT.format(text=text[:500])

        # Try AITUNNEL first
        if self.config.aitunnel_api_key:
            try:
                answer = await self._aitunnel_chat(prompt=prompt, temperature=0.1, max_tokens=10)
                if answer:
                    is_urgent = _parse_binary_answer(answer)
                    if is_urgent is not None:
                        logger.info(f"Urgency check [AITUNNEL]: {'✅ important' if is_urgent else '❌ skipped'}")
                        return is_urgent
            except Exception as e:
                logger.warning(f"Urgency check [AITUNNEL] failed: {e}")

        # Fallback to Gemini
        if not self._gemini_model or self._gemini_circuit_open():
            return True

        try:
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                lambda: self._gemini_model.generate_content(
                    prompt,
                    generation_config=_GenConfig(
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

        return False

    async def rewrite(self, original_text: str) -> Tuple[Optional[str], str]:
        """
        Rewrite text using AI.
        
        Returns:
            Tuple of (rewritten_text or None, engine_used)
        Fallback chain: AITUNNEL → Gemini → Groq → YandexGPT → ReText
        """
        # Try AITUNNEL first (OpenAI-compatible, paid, most reliable)
        if self.config.aitunnel_api_key:
            result = await self._rewrite_with_aitunnel(original_text)
            if result:
                return result, "aitunnel"

        # Fallback to Gemini
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
- СТРОГО ЗАПРЕЩЕНО: радостные/позитивные эмодзи (😀😊😄😂🎉😁😆🥳) в новостях о гибели людей, трагедиях, ДТП, пожарах, утоплениях — только 😢🙏⚠️❗️ или без эмодзи
- НИКОГДА не выдумывай и не меняй даты, время, числа (жертвы, суммы, проценты, температуру) — переноси их СТРОГО из оригинала. Если в оригинале нет конкретной даты — НЕ указывай её, пиши «сегодня»/«на днях» или вообще без даты. Запрещено брать даты «из головы» (например «25 октября», если её нет в тексте)

ДЕЙСТВИЕ 2 — ХЭШТЕГИ:
2-4 хэштега для новости. Обязательно #Ижевск или #Удмуртия если про регион.
Допустимые тематические: #экономика #здоровье #образование #транспорт #жкх #общество #закон #технологии #политика

ДЕЙСТВИЕ 3 — КЛЮЧЕВЫЕ СЛОВА ДЛЯ ФОТО (на английском):
2-3 конкретных слова для поиска стокового фото на Pexels. Правила:
- ГЛАВНОЕ: описывай только ГЛАВНУЮ тему новости — ту, что в ПЕРВОМ абзаце. Второстепенные упоминания (в конце текста) — ИГНОРИРУЙ
- Для аэропорта/авиарейсов: "airport terminal interior", "airplane runway departure", "flight departure board"
- Для вейпов/электронных сигарет/табака: "vape electronic cigarette smoke", "cigarette ban sign"
- Для транспорта/автобусов: "intercity bus road", "bus station city"
- Для электросамокатов/СИМ: "electric scooter city street", "kick scooter parking"
- Для медицины: "hospital exterior building", "ambulance emergency"
- Для строительства: "construction site crane", "road repair workers"
- Для чиновников/власти/суда: "official meeting room", "court hearing", "government office"
- Для аварий/ЧП/пожаров: "fire truck emergency", "rescue team"
- Для экономики/цен/долгов: "apartment building heating", "utility bills payment"
- Для дронов/БПЛА/военного: "military drone surveillance", "air defense system"
- Для ЖКХ/коммуналки/теплоснабжения: "heating plant pipes", "heating radiator apartment"
- Для образования: "school classroom students", "university lecture hall"
- Для полиции/правоохранителей: "police officer patrol", "law enforcement"
- Для утопления/водоёма/купания: "pond summer water", "lake summer shore", "swimming danger warning"
- Для трагедий/гибели: "rescue team water", "emergency response", "memorial flowers"
- Для погоды/прогноза: ОПИСЫВАЙ НЕБО по состоянию — "blue sky white clouds", "grey rainy sky street", "sunny summer day park", "thunderstorm dark clouds", "wet asphalt rain". НИКОГДА не город, не панораму, не Москву
- СТРОГО НЕЛЬЗЯ: добавлять слова "russia", "russian", "winter" если новость не про зиму/мороз
- СТРОГО НЕЛЬЗЯ: "city skyline", "downtown", "cityscape", "Moscow", небоскрёбы, городские панорамы — если новость не про конкретное здание/стройку
- СТРОГО НЕЛЬЗЯ: люди с едой, повара, рестораны — если новость не про еду/общепит
- СТРОГО НЕЛЬЗЯ: абстракции (news, information, technology, concept, digital)
- СТРОГО НЕЛЬЗЯ: животные, природа, пейзажи — если новость не про это
- ЕСЛИ новость не про конкретный видимый объект (слухи, фейк-аккаунт, мнения, чьи-то слова) — верни пустую строку. Лучше совсем без фото, чем случайный город

ФОРМАТ ОТВЕТА (строго, ничего лишнего кроме этих блоков):
РЕРАЙТ:
<переписанный текст>
ХЭШТЕГИ:
<хэштеги через пробел>
ФОТО:
<ключевые слова через запятую>

Исходная новость:
{original_text}"""

        # ── Try AITUNNEL (OpenAI-compatible, paid, most reliable) ─────────
        if self.config.aitunnel_api_key:
            import re as _re2
            try:
                aitunnel_raw = await self._aitunnel_chat(
                    prompt=COMBINED_PROMPT,
                    temperature=0.85,
                    max_tokens=2048,
                )
                if aitunnel_raw:
                    # Parse the same РЕРАЙТ/ХЭШТЕГИ/ФОТО format
                    rewrite_match = _re.search(
                        r'РЕРАЙТ:\s*\n(.*?)(?=\nХЭШТЕГИ:|$)',
                        aitunnel_raw, _re.DOTALL
                    )
                    hashtag_match = _re.search(
                        r'ХЭШТЕГИ:\s*\n(.*?)(?=\nФОТО:|$)',
                        aitunnel_raw, _re.DOTALL
                    )
                    photo_match = _re.search(
                        r'ФОТО:\s*\n(.*?)$',
                        aitunnel_raw, _re.DOTALL
                    )

                    rewritten = rewrite_match.group(1).strip() if rewrite_match else None
                    hashtags_raw = hashtag_match.group(1).strip() if hashtag_match else ""
                    photo_raw = photo_match.group(1).strip() if photo_match else ""

                    if rewritten and not self._is_refusal(rewritten) and len(rewritten) > 50:
                        hashtags = [
                            t.strip() for t in hashtags_raw.split()
                            if t.strip().startswith("#")
                        ][:4]

                        bad_keywords = {
                            "news", "information", "article", "report", "update",
                            "abstract", "concept", "technology", "digital", "modern",
                            "bell", "ring", "sound", "alarm", "signal",
                            "sport", "fitness", "gym", "workout", "climbing",
                            "nature", "forest", "animal", "bird", "wildlife",
                            "landscape", "mountain", "ocean", "sea", "river",
                            "background", "texture", "pattern", "wallpaper",
                            "food", "cook", "chef", "restaurant", "meal", "dish",
                            "coffee", "cafe", "kitchen", "cooking", "eating",
                            "smile", "happy", "people", "person", "man", "woman",
                            "social media", "phone", "smartphone", "internet",
                            "siren", "sound", "noise", "discussion", "social",
                        }
                        photo_keywords = [
                            kw.strip().lower()
                            for kw in photo_raw.split(",")
                            if kw.strip() and kw.strip().lower() not in bad_keywords
                        ][:3]

                        uniqueness = self.calculate_uniqueness(original_text, rewritten)
                        logger.info(
                            f"rewrite_full [aitunnel/{self.config.aitunnel_model}]: OK — "
                            f"uniqueness {uniqueness:.0%}, "
                            f"{len(hashtags)} tags, {len(photo_keywords)} photo kw"
                        )
                        return rewritten, f"aitunnel/{self.config.aitunnel_model}", hashtags, photo_keywords

                    logger.warning(f"rewrite_full [aitunnel]: parse failed or refusal")

            except Exception as e:
                logger.error(f"rewrite_full [aitunnel]: {e}")

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
                            generation_config=_GenConfig(
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
                                "nature", "forest", "animal", "bird", "wildlife",
                                "landscape", "mountain", "ocean", "sea", "river",
                                "background", "texture", "pattern", "wallpaper",
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
                    is_skip = "429" in err or "quota" in err or "resource" in err
                    is_geo = "400" in err and "location" in err
                    if is_skip or is_geo:
                        if is_geo:
                            logger.warning(f"rewrite_full [{model_name}]: Gemini geo-blocked (400 location), opening circuit breaker")
                            self._cb_record_error()
                            self._cb_open_until = __import__('time').monotonic() + 7200  # 2h penalty
                            break  # All models will have the same geo issue
                        logger.warning(f"rewrite_full [{model_name}]: quota hit, trying next model")
                        continue
                    logger.error(f"rewrite_full [{model_name}]: {e}")

            # All models on current key exhausted — try next key before fallback
            if self._switch_gemini_key():
                logger.info("rewrite_full: switched to next Gemini key, retrying combined call")
                # Recursive retry with the new key (only one level deep since
                # _switch_gemini_key won't switch past the last key)
                return await self.rewrite_full(original_text)

        # ── Fallback: call old methods separately ────────────────────────
        logger.warning("rewrite_full: combined call failed, falling back to separate methods")
        rewritten, engine = await self.rewrite(original_text)
        hashtags = await self.generate_hashtags(rewritten or original_text)
        photo_keywords = await self.generate_keywords(original_text)
        return rewritten, engine, hashtags, photo_keywords

    async def ask_ai(self, prompt: str, temperature: float = 0.8, _key_switched: bool = False) -> Optional[str]:
        """Generic AI text generation with AITUNNEL→Gemini→Groq→YandexGPT fallback.

        Used by ContentGenerator for rubric posts (weather, recipe, facts, etc.).
        """
        import re as _re

        # Try AITUNNEL first (most reliable, paid)
        if self.config.aitunnel_api_key:
            text = await self._aitunnel_chat(
                prompt=prompt,
                temperature=temperature,
                max_tokens=2048,
            )
            if text and len(text) > 20:
                text = _re.sub(r'^#{1,3}\s*', '', text, flags=_re.MULTILINE)
                text = _re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
                logger.info(f"ask_ai: AITUNNEL OK ({len(text)} chars)")
                return text

        # Try Gemini — unless circuit breaker is open
        if self._gemini_models and not self._gemini_circuit_open():
            all_quota = True  # Track whether ALL failures were quota-related
            try:
                loop = asyncio.get_event_loop()
                for _name, model in self._gemini_models:
                    try:
                        response = await loop.run_in_executor(
                            None,
                            lambda m=model: m.generate_content(
                                prompt,
                                generation_config=_GenConfig(
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
                        is_geo = "400" in err_str and "location" in err_str
                        if "quota" in err_str or "limit" in err_str or "429" in err_str or is_geo:
                            if is_geo:
                                logger.warning(f"ask_ai: {_name} geo-blocked (400 location), opening circuit breaker")
                                self._cb_open_until = __import__('time').monotonic() + 7200
                            else:
                                logger.warning(f"ask_ai: {_name} quota hit, trying next")
                            self._cb_record_error()
                            continue
                        logger.warning(f"ask_ai: {_name} error: {gem_err}")
                        self._cb_record_error()
                        all_quota = False
            except Exception as e:
                logger.warning(f"ask_ai: Gemini failed: {e}")
                self._cb_record_error()
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
                        generation_config=_GenConfig(
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
                                    generation_config=_GenConfig(temperature=0.95, max_output_tokens=2048),
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
                is_geo = "400" in error_str and "location" in error_str

                if is_quota or is_geo:
                    if is_geo:
                        logger.warning(f"Gemini [{model_name}]: geo-blocked (400 location), skipping all Gemini models")
                        self._cb_record_error()
                        self._cb_open_until = __import__('time').monotonic() + 7200
                        return None  # All models blocked, fall through to YandexGPT
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

    async def _aitunnel_chat(self, prompt: str, temperature: float, max_tokens: int) -> Optional[str]:
        """Generic AITUNNEL chat completion helper (OpenAI-compatible endpoint)."""
        if not self.config.aitunnel_api_key:
            return None

        url = "https://api.aitunnel.ru/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.config.aitunnel_api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.config.aitunnel_model,
            "messages": [
                {"role": "system", "content": "Ты — главный редактор популярного новостного Telegram-канала «Ижевск Сегодня». Пиши живо, по-человечески."},
                {"role": "user", "content": prompt},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        try:
            timeout = aiohttp.ClientTimeout(total=45)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, json=payload, headers=headers, ssl=False) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        choices = data.get("choices", [])
                        if not choices:
                            return None
                        message = choices[0].get("message", {}) or {}
                        content = (message.get("content") or "").strip()
                        if content:
                            logger.info(f"AITUNNEL/{self.config.aitunnel_model}: OK ({len(content)} chars)")
                            return content
                        return None
                    error_text = await resp.text()
                    logger.error(f"AITUNNEL returned {resp.status}: {error_text[:200]}")
        except asyncio.TimeoutError:
            logger.error("AITUNNEL request timeout (>45s)")
        except Exception as e:
            logger.error(f"AITUNNEL request failed: {e}")

        return None

    async def _rewrite_with_aitunnel(self, text: str) -> Optional[str]:
        """Rewrite text using AITUNNEL API (OpenAI-compatible, GPT-4o-mini)."""
        prompt = REWRITE_PROMPT.format(text=text) if len(text) > 300 else REWRITE_SHORT_PROMPT.format(text=text)
        rewritten = await self._aitunnel_chat(prompt=prompt, temperature=0.9, max_tokens=2048)
        if not rewritten:
            return None
        if self._is_refusal(rewritten):
            logger.warning(f"AITUNNEL: refusal detected: {rewritten[:80]}")
            return None
        if len(rewritten) <= 50 or rewritten == text:
            logger.warning("AITUNNEL: too short or identical")
            return None

        uniqueness = self.calculate_uniqueness(text, rewritten)
        logger.info(f"AITUNNEL: uniqueness {uniqueness:.0%} ({len(text)} -> {len(rewritten)} chars)")
        return rewritten

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
        if not self._gemini_model or self._gemini_circuit_open():
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
                    generation_config=_GenConfig(
                        temperature=0.2,
                        max_output_tokens=60,
                    ),
                ),
            )

            if response and response.text:
                tags = [t.strip() for t in response.text.strip().split() if t.startswith("#")]
                return tags[:4]

        except Exception as e:
            err_str = str(e).lower()
            if "400" in err_str and "location" in err_str:
                logger.warning(f"Hashtag generation: Gemini geo-blocked, opening circuit breaker")
                self._cb_open_until = __import__('time').monotonic() + 7200
                self._cb_record_error()
            else:
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
                    generation_config=_GenConfig(
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


    async def generate_outreach_comment(self, post_text: str) -> Optional[str]:
        """Generate a short, unique, neutral comment for another community's post.

        Used for organic outreach growth. No links, no self-promo, no templates —
        VK flags repetitive/promotional comments, so each one must look human.
        """
        if not self.config.aitunnel_api_key or not post_text:
            return None
        try:
            raw = await self._aitunnel_chat(
                prompt=OUTREACH_COMMENT_PROMPT.format(text=post_text[:1200]),
                temperature=0.85,
                max_tokens=120,
            )
        except Exception as e:
            logger.warning(f"Outreach comment generation failed: {e}")
            return None
        if not raw:
            return None
        comment = raw.strip().strip('"').strip().split("\n")[0].strip()
        if len(comment) < 8 or len(comment) > 280:
            return None
        # Safety: never let a link slip into an outreach comment (ban risk)
        if any(x in comment.lower() for x in ("http", "t.me", "vk.com", "@")):
            return None
        return comment

    async def generate_engagement(self, text: str) -> dict:
        """Generate an engaging first-comment and (when appropriate) a VK poll.

        Returns {"comment": str|None, "poll": {"question": str, "options": [...]}|None}.
        Sensitive topics (death/tragedy/accident) yield no poll by design.
        """
        import json as _json
        import re as _re
        empty = {"comment": None, "poll": None}
        if not self.config.aitunnel_api_key or not text:
            return empty
        try:
            raw = await self._aitunnel_chat(
                prompt=ENGAGEMENT_PROMPT.format(text=text[:1500]),
                temperature=0.7,
                max_tokens=400,
            )
        except Exception as e:
            logger.warning(f"Engagement generation failed: {e}")
            return empty
        if not raw:
            return empty
        match = _re.search(r"\{.*\}", raw, _re.DOTALL)
        if not match:
            return empty
        try:
            data = _json.loads(match.group(0))
        except Exception:
            return empty

        comment = (data.get("comment") or "").strip() or None
        poll = data.get("poll")
        if isinstance(poll, dict):
            q = (poll.get("question") or "").strip()
            opts = [str(o).strip() for o in (poll.get("options") or []) if str(o).strip()]
            poll = {"question": q, "options": opts} if q and len(opts) >= 2 else None
        else:
            poll = None
        return {"comment": comment, "poll": poll}

    async def generate_keywords(self, text: str) -> list[str]:
        """Extract keywords from text for stock photo search."""
        if not self._gemini_circuit_open() and self._gemini_model:
            try:
                prompt = f"""You help find a stock photo for a news story.

Task: suggest 2-3 ENGLISH keywords for searching a photo on Pexels.

RULES:
- Keywords must describe a SCENE or PLACE — something photogenic and easy to find on stock sites.
- Prefer people in a situation, not objects.
- 2-3 comma-separated phrases.
- BAD: news, information, bell, abstract, technology, digital, military vehicle, fighter jet
- GOOD: "airplane cockpit pilot", "soup bowl food", "court justice", "teacher classroom"

News text: {text[:500]}

Reply ONLY with comma-separated keywords, no explanations."""

                loop = asyncio.get_event_loop()
                response = await loop.run_in_executor(
                    None,
                    lambda: self._gemini_model.generate_content(prompt),
                )

                if response and response.text:
                    keywords = [kw.strip().lower() for kw in response.text.strip().split(",")]
                    bad_keywords = {
                        "news", "information", "article", "report", "update", "story", "newspaper",
                        "bell", "ring", "sound", "alarm", "signal", "noise", "tone", "ringtone",
                        "pipe", "tube", "wire", "equipment",
                        "abstract", "concept", "symbol", "icon", "background", "digital", "modern",
                        "technology",
                        "climbing", "sport", "fitness", "gym", "exercise", "athlete", "workout",
                        "athletic", "mountain", "boulder", "competition", "race", "runner",
                        "jump", "jumping", "sports", "movement", "motion", "activity",
                        "military vehicle", "air force", "military truck", "warplane", "fighter jet",
                        "military aircraft", "warship", "army vehicle", "military equipment",
                        "missile launcher", "armored vehicle", "military strike", "drone attack",
                    }
                    keywords = [kw for kw in keywords if kw and kw not in bad_keywords]
                    if keywords:
                        return keywords[:3]
                    logger.warning("generate_keywords: all Gemini keywords were filtered, using AITUNNEL/fallback")

            except Exception as e:
                err_str = str(e).lower()
                if "400" in err_str and "location" in err_str:
                    logger.warning(f"Keyword extraction: Gemini geo-blocked, opening circuit breaker")
                    self._cb_open_until = __import__('time').monotonic() + 7200
                    self._cb_record_error()
                else:
                    logger.error(f"Keyword extraction failed (Gemini): {e}")

        # --- AITUNNEL fallback (works from RU servers) ---
        if self.config.aitunnel_api_key:
            try:
                aitunnel_prompt = (
                    f"You help find a stock photo for a news story. "
                    f"Suggest 2-3 English keywords (comma-separated) describing a photogenic SCENE or PLACE. "
                    f"Examples: 'airplane cockpit pilot', 'hot soup bowl food', 'court justice judge'. "
                    f"AVOID: news, digital, military vehicle, abstract. "
                    f"Reply ONLY with comma-separated keywords.\n\nNews: {text[:400]}"
                )
                raw = await self._aitunnel_chat(aitunnel_prompt, temperature=0.3, max_tokens=60)
                if raw:
                    kws = [kw.strip().lower() for kw in raw.split(",") if kw.strip()]
                    kws = [kw for kw in kws if len(kw) > 2]
                    if kws:
                        logger.info(f"generate_keywords: AITUNNEL fallback OK: {kws}")
                        return kws[:3]
            except Exception as e:
                logger.error(f"Keyword extraction failed (AITUNNEL): {e}")

        # --- Static dictionary fallback ---
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
                    generation_config=_GenConfig(
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
                    generation_config=_GenConfig(
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
            # Aviation / airline
            (["авиа", "самолёт", "самолет", "аэропорт", "рейс", "авиакомпани", "пилот", "борт"],
             ["airplane cockpit pilot", "aircraft airport", "airline boarding"]),
            # Food / recipe
            (["рецепт", "блюдо", "суп", "щи", "борщ", "еда", "приготовл", "кулинар", "вкусн"],
             ["homemade soup bowl", "food cooking kitchen", "delicious meal"]),
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
            # Nature / weather
            (["снег", "мороз", "пурга", "метель", "снежн"],
             ["winter", "snow", "blizzard"]),
            (["погода", "дождь", "гроза", "оттепел", "похолодан"],
             ["weather", "rain", "nature"]),
            # Infrastructure
            (["строительств", "ремонт", "дорог", "стройк"],
             ["construction", "road", "workers"]),
            (["жкх", "коммунал", "отоплен", "водоснабжен"],
             ["city infrastructure", "heating", "utilities"]),
            (["транспорт", "автобус", "трамвай", "маршрут"],
             ["public transport", "bus", "city street"]),
            # Society
            (["школ", "образован", "учител", "студент"],
             ["school", "education", "classroom"]),
            (["больниц", "медицин", "врач", "хирург"],
             ["hospital", "doctor", "healthcare"]),
            (["экономик", "цен", "рубл", "инфляц", "зарплат", "инвестиц", "бюджет"],
             ["economy", "finance", "money"]),
            (["суд", "закон", "право"],
             ["court", "justice", "law"]),
            (["спорт", "матч", "команд", "чемпион"],
             ["sport", "competition", "athletes"]),
            # Geopolitics
            (["армия", "воен", "солдат", "флот", "вмс", "нато"],
             ["government meeting", "security officials", "ministry building"]),
            (["нефт", "газ", "топлив", "энерг"],
             ["oil", "gas", "energy"]),
            (["выбор", "политик", "депутат", "власт", "мэр", "чиновник"],
             ["politics", "government", "parliament"]),
            (["технолог", "цифров", "интернет"],
             ["technology", "digital", "innovation"]),
            # Retail / market
            (["магазин", "торговл", "рынок", "продаж"],
             ["market", "shop", "retail"]),
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
