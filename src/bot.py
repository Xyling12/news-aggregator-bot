"""
Telegram Bot — Aiogram 3 bot for admin moderation, post management, and publishing.
"""

import asyncio
import logging
import os
import re
import traceback
from datetime import datetime as dt
from typing import Optional

import aiohttp
import google.generativeai as genai

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    FSInputFile,
    ReactionTypeEmoji,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.enums import ParseMode

from src.config import Config
from src.database import Database
from src.ai_rewriter import AIRewriter
from src.media_processor import MediaProcessor
from src.vk_publisher import VKPublisher
from src.utils import (
    escape_html,
    clean_text,
    word_overlap,
    is_similar_to_any,
    find_similar_candidate,
    detect_rubric,
    format_post,
    RUBRIC_MAP,
    BREAKING_KEYWORDS,
)

logger = logging.getLogger(__name__)

router = Router()


# ── FSM States ───────────────────────────────────────────────────────────

class EditPostStates(StatesGroup):
    waiting_for_text = State()

class AddSourceStates(StatesGroup):
    waiting_for_channel = State()

class SendNewsStates(StatesGroup):
    waiting_for_news = State()


# ── Globals (set during init) ────────────────────────────────────────────

_config: Optional[Config] = None
_db: Optional[Database] = None
_rewriter: Optional[AIRewriter] = None
_media_processor: Optional[MediaProcessor] = None
_vk_publisher: Optional[VKPublisher] = None
_bot: Optional[Bot] = None

# Rate limiting: max 3 concurrent AI calls to avoid Gemini 429 errors
_ai_semaphore = asyncio.Semaphore(3)

LOCAL_GEO_KEYWORDS = [
    "удмурт",
    "ижевск",
    "глазов",
    "сарапул",
    "воткинск",
    "можга",
    "камбарк",
    "балезин",
    "завьялов",
    "удмуртск",
]

FEDERAL_NEWS_KEYWORDS = [
    "федеральн",
    "госдум",
    "государственн",
    "правительств",
    "минфин",
    "центробанк",
    "центральн банк",
    "ключев",
    "пенси",
    "налог",
    "пособи",
    "мрот",
    "жкх тариф",
    "тариф",
    "инфляц",
    "ставк",
]

RADAR_SOURCE_MARKERS = ["радар", "radar", "бпла", "воздух", "тревог"]


NON_LOCAL_REGION_KEYWORDS = [
    # Explicit non-local cities/regions that should not pass as Izhevsk-only updates.
    "сочи",
    "краснодар",
    "краснодарск",
    "адлер",
    "кубан",
    "анап",
    "геленджик",
    "новороссийск",
    "ростов",
    "белгород",
    "курск",
    "воронеж",
    "брянск",
    "твер",
    "москва",
    "санкт-петербург",
    "петербург",
    "спб",
]


def _normalize_geo_text(text: str) -> str:
    """Remove hashtag-only lines so region checks use the main body."""
    return re.sub(r"(?m)^\s*#.*$", "", text.lower()).strip()


def _has_local_geo(text: str) -> bool:
    normalized = _normalize_geo_text(text)
    return any(keyword in normalized for keyword in LOCAL_GEO_KEYWORDS)


def _looks_federal_news(text: str) -> bool:
    normalized = _normalize_geo_text(text)
    return any(keyword in normalized for keyword in FEDERAL_NEWS_KEYWORDS)


def _has_non_local_geo(text: str) -> bool:
    normalized = _normalize_geo_text(text)
    return any(keyword in normalized for keyword in NON_LOCAL_REGION_KEYWORDS)


def _should_reject_by_geo(
    *,
    is_local_source: bool,
    has_local_geo: bool,
    looks_federal: bool,
    has_non_local_geo: bool,
) -> bool:
    """Geo gate used before rewrite/publish."""
    if has_local_geo or looks_federal:
        return False
    if not is_local_source:
        return True
    # Local sources are allowed without explicit geo markers,
    # except when text clearly points to another region.
    return has_non_local_geo


def _is_breaking_candidate(
    text: str,
    *,
    is_radar_source: bool,
    has_geo: bool,
    breaking_keywords: list[str],
) -> bool:
    """Return True only for local breaking posts."""
    text_lower = text.lower()
    return (is_radar_source and has_geo) or (
        has_geo and any(kw in text_lower for kw in breaking_keywords)
    )


def is_admin(user_id: int) -> bool:
    """Check if user is admin."""
    return _config and user_id in _config.admin_ids


def _truncate(text: str, max_len: int = 200) -> str:
    """Truncate text for display."""
    if len(text) <= max_len:
        return text
    return text[:max_len] + "..."


def _status_emoji(status: str) -> str:
    """Get emoji for post status."""
    return {
        "pending": "⏳",
        "rewriting": "🔄",
        "review": "👀",
        "approved": "✅",
        "rejected": "❌",
        "published": "📢",
    }.get(status, "❓")


# ── Moderation Keyboard ─────────────────────────────────────────────────

def get_review_keyboard(post_id: int) -> InlineKeyboardMarkup:
    """Create inline keyboard for post moderation."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Опубликовать", callback_data=f"approve:{post_id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject:{post_id}"),
        ],
        [
            InlineKeyboardButton(text="✏️ Редактировать", callback_data=f"edit:{post_id}"),
            InlineKeyboardButton(text="🔄 Перерайт", callback_data=f"rewrite:{post_id}"),
        ],
        [
            InlineKeyboardButton(text="🖼 Искать фото", callback_data=f"search_photo:{post_id}"),
        ],
    ])


def get_main_keyboard() -> InlineKeyboardMarkup:
    """Create main menu keyboard."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📋 Очередь", callback_data="queue"),
            InlineKeyboardButton(text="📊 Статистика", callback_data="stats"),
        ],
        [
            InlineKeyboardButton(text="📡 Источники", callback_data="sources"),
            InlineKeyboardButton(text="⚙️ Настройки", callback_data="settings"),
        ],
    ])


# ── Command Handlers ─────────────────────────────────────────────────────

@router.message(CommandStart())
async def cmd_start(message: Message):
    """Handle /start command."""
    if is_admin(message.from_user.id):
        await message.answer(
            "🤖 <b>Ижевск Сегодня — Админ-панель</b>\n\n"
            "Я мониторю каналы-источники, переписываю новости через AI "
            "и отправляю их тебе на модерацию.\n\n"
            "📌 Используй меню ниже для управления:",
            reply_markup=get_main_keyboard(),
            parse_mode=ParseMode.HTML,
        )
    else:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📩 Прислать новость", callback_data="send_news")],
            [InlineKeyboardButton(text="📲 Перейти на канал", url="https://t.me/IzhevskTodayNews")],
        ])
        await message.answer(
            "📰 <b>Ижевск Сегодня</b>\n\n"
            "Привет! Я бот новостного канала @IzhevskTodayNews.\n\n"
            "Знаешь о важном событии в Ижевске? "
            "Нажми кнопку ниже — и мы рассмотрим твою новость для публикации 👇",
            reply_markup=kb,
            parse_mode=ParseMode.HTML,
        )


@router.message(Command("news"))
async def cmd_news(message: Message, state: FSMContext):
    """Start news submission from any user."""
    await state.set_state(SendNewsStates.waiting_for_news)
    await message.answer(
        "📩 <b>Прислать новость</b>\n\n"
        "Отправь мне текст или фото с описанием новости.\n"
        "Если новость интересная — мы опубликуем её на канале!\n\n"
        "Для отмены нажми /cancel",
        parse_mode=ParseMode.HTML,
    )


@router.callback_query(F.data == "send_news")
async def cb_send_news(callback: CallbackQuery, state: FSMContext):
    """Handle 'Прислать новость' button from /start menu."""
    await callback.answer()
    await state.set_state(SendNewsStates.waiting_for_news)
    await callback.message.answer(
        "📩 <b>Прислать новость</b>\n\n"
        "Отправь мне текст или фото с описанием новости.\n"
        "Если новость интересная — мы опубликуем её на канале!\n\n"
        "Для отмены нажми /cancel",
        parse_mode=ParseMode.HTML,
    )


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    """Cancel any active FSM state."""
    await state.clear()
    await message.answer("❌ Отменено.")


@router.message(Command("test_ai"))
async def cmd_test_ai(message: Message):
    """Admin: test all AI engines and report which ones work."""
    if not is_admin(message.from_user.id):
        return
    if not _rewriter:
        await message.answer("❌ AI rewriter не инициализирован.")
        return

    test_prompt = "Напиши одно короткое предложение: «Ижевск — столица Удмуртии»."
    result_lines = ["🧪 <b>Тест AI движков</b>\n"]

    # Test Gemini
    import time
    if _rewriter._gemini_models:
        if _rewriter._gemini_circuit_open():
            result_lines.append("⚡ <b>Gemini</b> — Circuit Breaker ОТКРЫТ (слишком много ошибок за час)")
        else:
            try:
                t0 = time.monotonic()
                import asyncio as _aio
                loop = _aio.get_event_loop()
                name, model = _rewriter._gemini_models[0]
                import google.generativeai as _genai
                resp = await loop.run_in_executor(
                    None,
                    lambda: model.generate_content(
                        test_prompt,
                        generation_config=_genai.GenerationConfig(max_output_tokens=50),
                    ),
                )
                elapsed = time.monotonic() - t0
                if resp and resp.text:
                    result_lines.append(f"✅ <b>Gemini/{name}</b> — работает ({elapsed:.1f}s)")
                    result_lines.append(f"   → {resp.text.strip()[:80]}")
                else:
                    result_lines.append(f"⚠️ <b>Gemini/{name}</b> — пустой ответ ({elapsed:.1f}s)")
            except Exception as e:
                result_lines.append(f"❌ <b>Gemini</b> — ошибка: {str(e)[:100]}")
    else:
        result_lines.append("❌ <b>Gemini</b> — не настроен (нет GEMINI_API_KEYS)")

    result_lines.append("")

    # Test YandexGPT
    if _rewriter.config.yandex_api_key and _rewriter.config.yandex_folder_id:
        try:
            import aiohttp as _aiohttp
            t0 = time.monotonic()
            url = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"
            headers = {
                "Authorization": f"Api-Key {_rewriter.config.yandex_api_key}",
                "Content-Type": "application/json",
            }
            body = {
                "modelUri": f"gpt://{_rewriter.config.yandex_folder_id}/yandexgpt-lite/latest",
                "completionOptions": {"stream": False, "temperature": 0.3, "maxTokens": "50"},
                "messages": [{"role": "user", "text": test_prompt}],
            }
            async with _aiohttp.ClientSession() as session:
                async with session.post(url, json=body, headers=headers,
                                        timeout=_aiohttp.ClientTimeout(total=20)) as resp:
                    elapsed = time.monotonic() - t0
                    if resp.status == 200:
                        data = await resp.json()
                        text = data.get("result", {}).get("alternatives", [{}])[0] \
                                   .get("message", {}).get("text", "")
                        result_lines.append(f"✅ <b>YandexGPT</b> — работает ({elapsed:.1f}s)")
                        result_lines.append(f"   → {text.strip()[:80]}")
                    else:
                        body_text = await resp.text()
                        result_lines.append(
                            f"❌ <b>YandexGPT</b> — HTTP {resp.status} ({elapsed:.1f}s)\n"
                            f"   {body_text[:120]}"
                        )
        except Exception as e:
            result_lines.append(f"❌ <b>YandexGPT</b> — ошибка: {str(e)[:100]}")
    else:
        result_lines.append("⚠️ <b>YandexGPT</b> — не настроен (нет YANDEX_API_KEY / YANDEX_FOLDER_ID)")

    await message.answer("\n".join(result_lines), parse_mode=ParseMode.HTML)


@router.message(Command("aistats"))
async def cmd_aistats(message: Message):
    """Admin: show AI circuit breaker status and error stats."""
    if not is_admin(message.from_user.id):
        return
    if not _rewriter:
        await message.answer("❌ AI rewriter не инициализирован.")
        return

    import time
    now = time.monotonic()
    window = _rewriter._CB_WINDOW_SECONDS

    # Count recent errors
    cutoff = now - window
    recent_errors = [t for t in _rewriter._cb_error_times if t > cutoff]
    max_errors = _rewriter._CB_MAX_ERRORS

    if _rewriter._gemini_circuit_open():
        remaining = max(0, _rewriter._cb_open_until - now)
        cb_status = f"⚡ ОТКРЫТ — сброс через {int(remaining // 60)} мин {int(remaining % 60)} с"
    else:
        cb_status = "✅ ЗАКРЫТ (Gemini работает в штатном режиме)"

    # Gemini keys status
    keys = _rewriter.config.gemini_api_keys or []
    current_key = _rewriter._current_key_index + 1

    lines = [
        "📊 <b>AI Statistics</b>\n",
        f"🔥 <b>Circuit Breaker:</b> {cb_status}",
        f"⚠️ <b>Ошибок Gemini (за 1ч):</b> {len(recent_errors)}/{max_errors}",
        "",
        f"🔑 <b>Gemini API ключи:</b> {current_key}/{len(keys)} активен",
        f"🤖 <b>Модели Gemini:</b> {len(_rewriter._gemini_models)} загружено",
        "",
        f"🇷🇺 <b>YandexGPT:</b> {'✅ ключ есть' if _rewriter.config.yandex_api_key else '❌ нет ключа'}",
        "",
        "💡 Используй /test_ai для живого теста всех движков",
    ]

    await message.answer("\n".join(lines), parse_mode=ParseMode.HTML)



@router.message(SendNewsStates.waiting_for_news)
async def process_user_news(message: Message, state: FSMContext):
    """Process user-submitted news and forward to admins."""
    await state.clear()

    user = message.from_user
    user_info = f"{user.full_name}"
    if user.username:
        user_info += f" (@{user.username})"

    # Notify all admins
    for admin_id in _config.admin_ids:
        try:
            admin_text = (
                f"📩 <b>Новость от подписчика</b>\n"
                f"👤 {_escape_html(user_info)}\n\n"
            )
            if message.text:
                admin_text += f"{_escape_html(message.text[:2000])}"
                await _bot.send_message(admin_id, admin_text, parse_mode=ParseMode.HTML)
            elif message.photo:
                caption = message.caption or ""
                admin_text += f"{_escape_html(caption[:800])}"
                await _bot.send_photo(
                    admin_id,
                    photo=message.photo[-1].file_id,
                    caption=admin_text,
                    parse_mode=ParseMode.HTML,
                )
            else:
                admin_text += "(медиа-сообщение)"
                await _bot.send_message(admin_id, admin_text, parse_mode=ParseMode.HTML)
                await message.forward(admin_id)
        except Exception as e:
            logger.error(f"Failed to forward user news to admin {admin_id}: {e}")

    await message.answer(
        "✅ Спасибо! Ваша новость отправлена редакции.\n"
        "Если она интересная — мы опубликуем её на канале @IzhevskTodayNews!",
    )


# ── Chat Moderation ──────────────────────────────────────────────────────────

_MOD_RULES = [
    ("реклама/спам", [
        "куп", "продам", "продаю", "скидка", "акция", "промокод", "заработ",
        "инвестиц", "крипт", "биткоин", "казино", "ставк", "букмекер",
    ]),
    ("наркотики", [
        "мефедрон", "амфетамин", "героин", "кокаин", "гашиш", "марихуан",
        "спайс", "закладк", "нарк", "вещества",
    ]),
    ("политика/экстремизм", [
        "путин х", "слава украине", "хохол", "кацап", "нацист", "фашист",
        "долой власть", "свергнуть", "митинг организуем", "протест организуем",
    ]),
]
_LINK_PAT = re.compile(r'(https?://|t\.me/|vk\.com/|telegram\.me/|bit\.ly/)', re.IGNORECASE)


@router.message(F.chat.type.in_({"group", "supergroup"}))
async def chat_moderation(message: Message):
    """Auto-delete rule-breaking messages in discussion chat."""
    text = (message.text or message.caption or "").lower()
    if not text:
        return

    violated = None
    if _LINK_PAT.search(text):
        violated = "ссылки"
    if not violated:
        for category, keywords in _MOD_RULES:
            if any(kw in text for kw in keywords):
                violated = category
                break

    if violated:
        try:
            await message.delete()
        except Exception:
            pass
        try:
            warn = await message.answer(
                f"⛔ Сообщение удалено ({violated}). Соблюдайте правила чата."
            )
            await asyncio.sleep(10)
            await warn.delete()
        except Exception:
            pass


@router.message(
    F.chat.type.in_({"group", "supergroup"}),
    F.or_(
        F.new_chat_members,
        F.left_chat_member,
        F.new_chat_title,
        F.new_chat_photo,
        F.delete_chat_photo,
        F.group_chat_created,
        F.supergroup_chat_created,
        F.message_auto_delete_timer_changed,
        F.pinned_message,
        F.video_chat_started,
        F.video_chat_ended,
        F.video_chat_participants_invited,
    )
)
async def delete_service_messages(message: Message):
    """Silently delete Telegram system/service messages to keep chat clean."""
    try:
        await message.delete()
    except Exception:
        pass


@router.message(Command("queue"))
async def cmd_queue(message: Message):
    """Show posts queue."""
    if not is_admin(message.from_user.id):
        return

    posts = await _db.get_review_posts(limit=5)
    if not posts:
        await message.answer("📭 Очередь пуста — нет постов на модерации.")
        return

    await message.answer(f"📋 **В очереди на модерацию: {len(posts)} постов**\n\n"
                        "Отправляю первый пост...",
                        parse_mode=ParseMode.MARKDOWN)

    for post in posts[:3]:
        await _send_review_post(message.chat.id, post)
        await asyncio.sleep(0.5)


@router.message(Command("stats"))
async def cmd_stats(message: Message):
    """Show statistics."""
    if not is_admin(message.from_user.id):
        return

    stats = await _db.get_stats()
    text = (
        "📊 **Статистика постов:**\n\n"
        f"⏳ В ожидании рерайта: {stats.get('pending', 0)}\n"
        f"🔄 Рерайтится: {stats.get('rewriting', 0)}\n"
        f"👀 На модерации: {stats.get('review', 0)}\n"
        f"✅ Одобрено: {stats.get('approved', 0)}\n"
        f"❌ Отклонено: {stats.get('rejected', 0)}\n"
        f"📢 Опубликовано: {stats.get('published', 0)}\n"
    )
    await message.answer(text, parse_mode=ParseMode.MARKDOWN)


@router.message(Command("sources"))
async def cmd_sources(message: Message):
    """Show source channels."""
    if not is_admin(message.from_user.id):
        return

    sources = await _db.get_active_sources()
    if not sources:
        text = "📡 Нет активных источников."
    else:
        lines = ["📡 **Активные источники:**\n"]
        for s in sources:
            lines.append(f"  • @{s['channel_username']} (последний ID: {s['last_message_id']})")
        text = "\n".join(lines)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить источник", callback_data="add_source")],
    ])
    await message.answer(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)


@router.message(Command("help"))
async def cmd_help(message: Message):
    """Show help."""
    text = (
        "📖 **Команды:**\n\n"
        "/start — Главное меню\n"
        "/queue — Очередь на модерацию\n"
        "/stats — Статистика\n"
        "/sources — Управление источниками\n"
        "/publish — Опубликовать одобренные посты\n"
        "/report — Недельный отчёт\n"
        "/help — Это сообщение"
    )
    await message.answer(text, parse_mode=ParseMode.MARKDOWN)


@router.message(Command("testgemini"))
async def cmd_test_gemini(message: Message):
    """Test Gemini API connectivity — admin only diagnostic."""
    if not is_admin(message.from_user.id):
        return

    lines = []

    # Check 1: API key
    key = _config.gemini_api_key if _config else "NO CONFIG"
    lines.append(f"🔑 API Key: {'SET (' + key[:10] + '...)' if key else '❌ NOT SET'}")

    # Check 2: Model
    if _rewriter and _rewriter._gemini_model:
        lines.append("🤖 Model: ✅ initialized")
    else:
        lines.append("🤖 Model: ❌ NOT initialized")

    # Check 3: Try actual API call
    try:
        genai.configure(api_key=_config.gemini_api_key)
        model = genai.GenerativeModel("gemini-2.0-flash")
        response = model.generate_content("Скажи одно слово: привет")
        if response and response.text:
            lines.append(f"📡 API Call: ✅ OK — '{response.text.strip()[:50]}'")
        else:
            lines.append("📡 API Call: ❌ Empty response")
            if hasattr(response, 'candidates'):
                lines.append(f"   Candidates: {response.candidates}")
    except Exception as e:
        lines.append("📡 API Call: ❌ ERROR")
        lines.append(f"   {type(e).__name__}: {str(e)[:200]}")
        lines.append(f"   Traceback: {traceback.format_exc()[-300:]}")

    await message.answer("\n".join(lines))


@router.message(Command("testai"))
async def cmd_test_ai(message: Message):
    """Test ALL AI engines — admin only diagnostic."""
    if not is_admin(message.from_user.id):
        return

    lines = ["🔍 **Тест всех AI-движков:**\n"]

    # Test 1: Gemini
    lines.append("═══ GEMINI ═══")
    key = _config.gemini_api_key if _config else ""
    lines.append(f"🔑 Key: {'SET (' + key[:10] + '...)' if key else '❌ NOT SET'}")
    if _rewriter and _rewriter._gemini_models:
        names = [m[0] for m in _rewriter._gemini_models]
        lines.append(f"🤖 Models: {', '.join(names)}")
    else:
        lines.append("🤖 Models: ❌ none")

    # Test 2: YandexGPT
    lines.append("\n═══ YANDEX GPT ═══")
    ykey = _config.yandex_api_key if _config else ""
    yfolder = _config.yandex_folder_id if _config else ""
    lines.append(f"🔑 Key: {'SET (' + ykey[:10] + '...)' if ykey else '❌ NOT SET'}")
    lines.append(f"📁 Folder: {yfolder if yfolder else '❌ NOT SET'}")

    if ykey and yfolder:
        try:
            url = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"
            headers = {
                "Authorization": f"Api-Key {ykey}",
                "Content-Type": "application/json",
            }
            body = {
                "modelUri": f"gpt://{yfolder}/yandexgpt-lite/latest",
                "completionOptions": {"stream": False, "temperature": 0.3, "maxTokens": "50"},
                "messages": [{"role": "user", "text": "Скажи одно слово: привет"}],
            }
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=body, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        text = data["result"]["alternatives"][0]["message"]["text"]
                        lines.append(f"📡 API: ✅ OK — '{text.strip()[:50]}'")
                    else:
                        error = await resp.text()
                        lines.append(f"📡 API: ❌ HTTP {resp.status}")
                        lines.append(f"   {error[:300]}")
        except Exception as e:
            lines.append(f"📡 API: ❌ {type(e).__name__}: {str(e)[:200]}")

    # Test 3: ReText
    lines.append("\n═══ RETEXT.AI ═══")
    rkey = _config.retext_api_key if _config else ""
    lines.append(f"🔑 Key: {'SET' if rkey else '❌ NOT SET'}")

    await message.answer("\n".join(lines))


@router.message(Command("testvk"))
async def cmd_test_vk(message: Message):
    """Test VK API connection — admin only diagnostic."""
    if not is_admin(message.from_user.id):
        return

    if not _vk_publisher:
        await message.answer("❌ VK publisher not initialized (bot restarting?)")
        return

    lines = ["🔍 <b>Диагностика VK</b>\n"]
    lines.append(f"🔑 Токен: {'✅ SET (' + _vk_publisher.access_token[:8] + '...)' if _vk_publisher.access_token else '❌ НЕ ЗАДАН'}")
    lines.append(f"👥 Group ID: {'✅ ' + _vk_publisher.group_id if _vk_publisher.group_id else '❌ НЕ ЗАДАН'}")
    lines.append(f"📡 Enabled: {'✅ Да' if _vk_publisher.enabled else '❌ Нет'}")

    if _vk_publisher.enabled:
        lines.append("\n⏳ Проверяю соединение с VK API...")
        await message.answer("\n".join(lines), parse_mode=ParseMode.HTML)
        result = await _vk_publisher.test_connection()
        status_lines = [
            f"\n📊 <b>Результат проверки:</b>",
            f"Status: {'✅ OK' if result.get('status') == 'ok' else '❌ ERROR'}",
        ]
        if result.get('group_name'):
            status_lines.append(f"Группа: {result['group_name']}")
            status_lines.append(f"URL: {result.get('group_url', '')}")
        await message.answer("\n".join(status_lines), parse_mode=ParseMode.HTML)
    else:
        lines.append("\n⛔ VK crosspost отключён — задайте VK_ACCESS_TOKEN и VK_GROUP_ID в Dokploy.")
        await message.answer("\n".join(lines), parse_mode=ParseMode.HTML)


@router.message(Command("testcontent"))
async def cmd_test_content(message: Message):
    """Manually trigger any content rubric right now — admin only."""
    if not is_admin(message.from_user.id):
        return

    args = message.text.split(maxsplit=1)
    rubric = args[1].strip().lower() if len(args) > 1 else ""

    valid = {
        "weather": "🌤 Погода",
        "history_fact": "📅 История дня",
        "five_facts": "📌 5 фактов",
        "recipe": "🍽 Рецепт",
        "lifehack": "💡 Лайфхак",
        "place": "📍 Место",
        "evening_fun": "😄 Вечерний fun",
        "daily_digest": "📊 Дайджест",
    }

    if rubric not in valid:
        lines = ["<b>📋 Доступные рубрики:</b>"]
        for key, name in valid.items():
            lines.append(f"  <code>/testcontent {key}</code> — {name}")
        await message.answer("\n".join(lines), parse_mode=ParseMode.HTML)
        return

    await message.answer(f"⏳ Генерирую <b>{valid[rubric]}</b>...", parse_mode=ParseMode.HTML)
    sched = _content_scheduler
    if not sched:
        await message.answer("❌ Content scheduler не инициализирован (бот рестартует?)")
        return

    try:
        await sched._publish_rubric(rubric, valid[rubric])
        await message.answer(f"✅ <b>{valid[rubric]}</b> опубликовано в канал!", parse_mode=ParseMode.HTML)
    except Exception as e:
        await message.answer(f"❌ Ошибка: <code>{e}</code>", parse_mode=ParseMode.HTML)


# ── Reference to content scheduler (set from main.py) ─────────────────────
_content_scheduler = None


@router.message(Command("publish"))
async def cmd_publish(message: Message):
    """Publish all approved posts with delays between them."""
    if not is_admin(message.from_user.id):
        return

    approved = await _db.get_approved_posts()
    if not approved:
        await message.answer("📭 Нет одобренных постов для публикации.")
        return

    total = len(approved)
    await message.answer(f"📢 Начинаю публикацию {total} постов с интервалом 30 сек...")

    published_count = 0
    for i, post in enumerate(approved):
        success = await _publish_post(post)
        if success:
            published_count += 1
        # Don't sleep after the last post
        if i < total - 1:
            await asyncio.sleep(30)  # 30 seconds between posts to avoid flooding

    await message.answer(f"✅ Опубликовано: {published_count}/{total}")


@router.message(Command("report"))
async def cmd_report(message: Message):
    """Show weekly analytics report."""
    if not is_admin(message.from_user.id):
        return

    stats = await _db.get_stats()
    weekly = await _db.get_weekly_stats()

    total = sum(stats.values())
    text = (
        "📊 **Недельный отчёт:**\n\n"
        f"📥 Собрано новостей: {weekly.get('total', 0)}\n"
        f"✅ Одобрено: {weekly.get('approved', 0)}\n"
        f"❌ Отклонено: {weekly.get('rejected', 0)}\n"
        f"  ↳ 🔄 Дубликаты: автоматически\n"
        f"  ↳ 📢 Реклама: автоматически\n"
        f"  ↳ 🌍 Нерелевантные: автоматически\n"
        f"📢 Опубликовано: {weekly.get('published', 0)}\n\n"
        "📡 **По источникам:**\n"
    )

    for src, count in weekly.get('by_source', {}).items():
        text += f"  • @{src}: {count}\n"

    await message.answer(text, parse_mode=ParseMode.MARKDOWN)


# ── Callback Handlers ────────────────────────────────────────────────────

@router.callback_query(F.data == "queue")
async def cb_queue(callback: CallbackQuery):
    """Queue button handler."""
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Нет доступа", show_alert=True)
        return

    await callback.answer()
    posts = await _db.get_review_posts(limit=5)
    if not posts:
        await callback.message.answer("📭 Очередь пуста.")
        return

    await callback.message.answer(f"📋 На модерации: {len(posts)} постов")
    for post in posts[:3]:
        await _send_review_post(callback.message.chat.id, post)
        await asyncio.sleep(0.5)


@router.callback_query(F.data == "stats")
async def cb_stats(callback: CallbackQuery):
    """Stats button handler."""
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Нет доступа", show_alert=True)
        return

    await callback.answer()
    stats = await _db.get_stats()
    text = (
        "📊 **Статистика:**\n\n"
        f"⏳ Ожидание: {stats.get('pending', 0)} | "
        f"👀 Модерация: {stats.get('review', 0)} | "
        f"📢 Опубликовано: {stats.get('published', 0)}"
    )
    await callback.message.answer(text, parse_mode=ParseMode.MARKDOWN)


@router.callback_query(F.data == "sources")
async def cb_sources(callback: CallbackQuery):
    """Sources button handler."""
    await callback.answer()
    sources = await _db.get_active_sources()
    lines = ["📡 <b>Источники:</b>\n"] + [f"  • @{s['channel_username']}" for s in sources]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить", callback_data="add_source")],
    ])
    await callback.message.answer("\n".join(lines) or "Нет источников", reply_markup=kb, parse_mode=ParseMode.HTML)


@router.callback_query(F.data == "settings")
async def cb_settings(callback: CallbackQuery):
    """Settings button handler."""
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Нет доступа", show_alert=True)
        return

    await callback.answer()
    text = (
        f"⚙️ <b>Настройки бота</b>\n\n"
        f"📡 Источников: <b>{len(_config.source_channels)}</b>\n"
        f"⏱ Интервал проверки: <b>{_config.check_interval} сек</b>\n"
        f"📤 Интервал публикации: <b>{_config.publish_interval // 60} мин</b>\n"
        f"📏 Мин. длина текста: <b>{_config.min_text_length} символов</b>\n"
        f"🗣 Язык: <b>{_config.language}</b>\n\n"
        f"🚫 Фильтры:\n"
        f"  • Реклама: <b>{len(_config.ad_stop_words)} слов</b>\n"
        f"  • Срочные новости: <b>{len(_config.breaking_keywords)} слов</b>\n\n"
        f"📢 Канал: <b>@{_config.target_channel}</b>"
    )
    await callback.message.answer(text, parse_mode=ParseMode.HTML)


@router.callback_query(F.data == "add_source")
async def cb_add_source(callback: CallbackQuery, state: FSMContext):
    """Start adding a source channel."""
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Нет доступа", show_alert=True)
        return

    await callback.answer()
    await callback.message.answer(
        "📡 Отправь username канала (без @).\n"
        "Например: `ria_novosti`",
        parse_mode=ParseMode.MARKDOWN,
    )
    await state.set_state(AddSourceStates.waiting_for_channel)


@router.message(AddSourceStates.waiting_for_channel)
async def process_add_source(message: Message, state: FSMContext):
    """Process new source channel username."""
    channel = message.text.strip().lstrip("@")
    if not channel:
        await message.answer("❌ Пустое имя канала.")
        return

    await _db.add_source(channel)
    _config.source_channels.append(channel)
    await state.clear()
    await message.answer(
        f"✅ Канал @{channel} добавлен в источники!\n\n"
        "⚠️ Для активации мониторинга нового канала перезапустите бота.",
    )


# ── Moderation Callbacks ─────────────────────────────────────────────────

@router.callback_query(F.data.startswith("approve:"))
async def cb_approve(callback: CallbackQuery):
    """Approve a post for publishing."""
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Нет доступа", show_alert=True)
        return

    post_id = int(callback.data.split(":")[1])
    await _db.update_post_status(post_id, "approved", reviewed_by=callback.from_user.id)
    await callback.answer("✅ Пост одобрен!")

    # Ask if should publish now
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📢 Опубликовать сейчас", callback_data=f"publish_now:{post_id}"),
            InlineKeyboardButton(text="⏰ Позже", callback_data="dismiss"),
        ],
    ])
    await callback.message.edit_reply_markup(reply_markup=kb)


@router.callback_query(F.data.startswith("reject:"))
async def cb_reject(callback: CallbackQuery):
    """Reject a post."""
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Нет доступа", show_alert=True)
        return

    post_id = int(callback.data.split(":")[1])
    await _db.update_post_status(post_id, "rejected", reviewed_by=callback.from_user.id)
    await callback.answer("❌ Пост отклонён")
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.reply("❌ Пост отклонён и удалён из очереди.")


@router.callback_query(F.data.startswith("edit:"))
async def cb_edit(callback: CallbackQuery, state: FSMContext):
    """Start editing a post."""
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Нет доступа", show_alert=True)
        return

    post_id = int(callback.data.split(":")[1])
    await state.update_data(edit_post_id=post_id)
    await state.set_state(EditPostStates.waiting_for_text)
    await callback.answer()
    await callback.message.reply(
        "✏️ Отправь новый текст для этого поста.\n"
        "Отправь /cancel для отмены."
    )


@router.message(EditPostStates.waiting_for_text)
async def process_edit_text(message: Message, state: FSMContext):
    """Process edited text for a post."""
    if message.text == "/cancel":
        await state.clear()
        await message.answer("❌ Редактирование отменено.")
        return

    data = await state.get_data()
    post_id = data.get("edit_post_id")
    if not post_id:
        await state.clear()
        return

    await _db.update_post_text(post_id, message.text)
    await state.clear()

    post = await _db.get_post(post_id)
    await message.answer("✅ Текст обновлён! Отправляю пост на повторную модерацию:")
    await _send_review_post(message.chat.id, post)


@router.callback_query(F.data.startswith("rewrite:"))
async def cb_rewrite(callback: CallbackQuery):
    """Re-run AI rewrite on a post."""
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Нет доступа", show_alert=True)
        return

    post_id = int(callback.data.split(":")[1])
    await callback.answer("🔄 Перезапускаю рерайт...")

    post = await _db.get_post(post_id)
    if post:
        await _db.update_post_status(post_id, "rewriting")

        # Clean original text before rewriting (same as pipeline)
        clean_original = _clean_text(post["original_text"])
        rewritten, engine = await _rewriter.rewrite(clean_original)
        if rewritten:
            rewritten = _clean_text(rewritten)  # Clean AI output
            # Generate hashtags and format
            hashtags = await _rewriter.generate_hashtags(rewritten)
            rewritten = _format_post(rewritten, hashtags)
            
            await _db.update_post_rewrite(post_id, rewritten)
            uniqueness = _rewriter.calculate_uniqueness(clean_original, rewritten)

            updated_post = await _db.get_post(post_id)
            await callback.message.reply(f"✅ Перерайт завершён (движок: {engine}, уникальность: {uniqueness:.0%})")
            await _send_review_post(callback.message.chat.id, updated_post)
        else:
            logger.error(f"Post #{post_id}: manual rewrite failed - all AI engines returned None")
            await callback.message.reply("❌ Рерайт не удался. Все AI-движки (Gemini/YandexGPT) недоступны.")
            await _db.update_post_status(post_id, "review")


@router.callback_query(F.data.startswith("search_photo:"))
async def cb_search_photo(callback: CallbackQuery):
    """Search for stock photos for a post."""
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Нет доступа", show_alert=True)
        return

    post_id = int(callback.data.split(":")[1])
    await callback.answer("🔍 Ищу подходящие фото...")

    post = await _db.get_post(post_id)
    if not post:
        return

    # Extract keywords
    text = post.get("rewritten_text") or post["original_text"]
    keywords = await _rewriter.generate_keywords(text)

    if not keywords:
        await callback.message.reply("❌ Не удалось извлечь ключевые слова для поиска.")
        return

    # Search stock photos
    photos = await _media_processor.search_stock_photo(keywords, count=3)
    if not photos:
        await callback.message.reply(f"📷 Фото не найдены. Ключевые слова: {', '.join(keywords)}")
        return

    # Send photo options
    await callback.message.reply(f"🔍 Ключевые слова: {', '.join(keywords)}\nНайдено {len(photos)} фото:")

    for i, photo in enumerate(photos):
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text=f"✅ Использовать это фото",
                callback_data=f"use_photo:{post_id}:{i}",
            )],
        ])
        caption = f"📷 {photo.get('description', 'Stock photo')} | by {photo['author']}"
        try:
            await _bot.send_photo(
                callback.message.chat.id,
                photo=photo["thumb_url"],
                caption=caption[:200],
                reply_markup=kb,
            )
        except Exception as e:
            await callback.message.reply(f"Фото {i+1}: {photo['url']}", reply_markup=kb)

        await asyncio.sleep(0.5)


@router.callback_query(F.data.startswith("publish_now:"))
async def cb_publish_now(callback: CallbackQuery):
    """Publish a specific post immediately."""
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Нет доступа", show_alert=True)
        return

    post_id = int(callback.data.split(":")[1])
    post = await _db.get_post(post_id)

    if not post:
        await callback.answer("❌ Пост не найден", show_alert=True)
        return

    await callback.answer("📢 Публикую...")
    success = await _publish_post(post)

    if success:
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.reply("📢 Пост опубликован!")
    else:
        await callback.message.reply("❌ Ошибка при публикации. Проверьте настройки канала.")


@router.callback_query(F.data == "dismiss")
async def cb_dismiss(callback: CallbackQuery):
    """Dismiss a notification."""
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)


# ── Helper Functions (thin wrappers for backwards compat) ────────────────────
# All implementations live in src/utils.py

def _escape_html(text: str) -> str:
    """Escape HTML — delegates to utils.escape_html."""
    return escape_html(text)


def _clean_text(text: str) -> str:
    """Clean post text — delegates to utils.clean_text."""
    return clean_text(text)


def _format_post(text: str, hashtags: list) -> str:
    """Format post — delegates to utils.format_post."""
    return format_post(text, hashtags)


def _is_similar_to_any(text: str, candidates: list) -> bool:
    """Deduplication check — delegates to utils.is_similar_to_any."""
    return is_similar_to_any(text, candidates, _rewriter)


def _find_similar_match(text: str, candidates: list, *, queued: bool = False):
    """Return duplicate details for logging and threshold tuning."""
    if queued:
        return find_similar_candidate(
            text,
            candidates,
            _rewriter,
            similarity_threshold=0.83,
            overlap_threshold=0.58,
            require_both=True,
            hard_similarity_threshold=0.96,
            hard_overlap_threshold=0.86,
        )
    return find_similar_candidate(text, candidates, _rewriter)


async def _send_review_post(chat_id: int, post: dict):
    """Send a post for admin review with moderation buttons."""
    original = _escape_html(_truncate(post["original_text"], 300))
    rewritten = post.get("rewritten_text") or "⏳ Ещё не переписан"
    # Don't escape rewritten text — it contains intentional HTML from _format_post (<b>, <a>)
    rewritten_display = _truncate(rewritten, 500)

    status = _status_emoji(post["status"])
    source = post["source_channel"]

    # Format date as d.m.Y H:M
    try:
        created = dt.fromisoformat(str(post['created_at']))
        date_str = created.strftime("%d.%m.%Y %H:%M")
    except Exception:
        date_str = str(post['created_at'])

    text = (
        f"{status} <b>Пост #{post['id']}</b> | Источник: @{source}\n"
        f"📅 {date_str}\n\n"
        f"📝 <b>Оригинал:</b>\n{original}\n\n"
        f"✍️ <b>Рерайт:</b>\n{rewritten_display}"
    )

    # Add media info
    if post.get("replacement_media_url"):
        text += f"\n\n🖼 Стоковое фото подобрано ✅"
    elif post["media_type"] != "none":
        text += f"\n\n🖼 Медиа: {post['media_type']}"
        if post.get("has_watermark"):
            text += " ⚠️ Обнаружен водяной знак!"

    # If post has media, send with media
    replacement_url = post.get("replacement_media_url")
    media_path = post.get("media_local_path")
    media_url = post.get("media_file_id")  # Remote URL fallback

    # Try: stock photo > local file > remote URL
    photo_source = None
    if replacement_url:
        photo_source = replacement_url
    elif media_path and os.path.exists(media_path) and post["media_type"] == "photo":
        photo_source = FSInputFile(media_path)
    elif media_url and post["media_type"] == "photo":
        photo_source = media_url

    if photo_source:
        try:
            await _bot.send_photo(
                chat_id,
                photo=photo_source,
                caption=text[:1024],
                reply_markup=get_review_keyboard(post["id"]),
                parse_mode=ParseMode.HTML,
            )
            return
        except Exception as e:
            logger.error(f"Failed to send media: {e}")

    # Send text only
    await _bot.send_message(
        chat_id,
        text[:4096],
        reply_markup=get_review_keyboard(post["id"]),
        parse_mode=ParseMode.HTML,
    )


async def _publish_post(post: dict) -> bool:
    """Publish a post to the target channel.

    If sending with a photo fails (bad URL, expired file_id, etc.),
    falls back to text-only. If even text fails, marks the post as
    'publish_failed' so it does not block the auto-publish queue forever.
    """
    text = post.get("rewritten_text") or post["original_text"]
    target = _config.target_channel

    if not target.startswith("@") and not target.startswith("-"):
        target = f"@{target}"

    media_path = post.get("media_local_path")
    media_url = post.get("media_file_id")
    replacement_url = post.get("replacement_media_url")

    msg = None
    local_stock: Optional[str] = None  # Will hold local path of downloaded stock photo for VK reuse

    # ── Try to publish with photo ─────────────────────────────────────────
    try:
        if replacement_url:
            # Download locally first — Wikimedia/CDN URLs often block Telegram's fetcher
            local_stock = await _media_processor.download_stock_photo(
                replacement_url, f"stock_{post['id']}.jpg"
            )
            photo_source = FSInputFile(local_stock) if local_stock else replacement_url
            msg = await _bot.send_photo(
                target,
                photo=photo_source,
                caption=text[:1024],
                parse_mode=ParseMode.HTML,
            )
        elif post["media_type"] == "photo":
            photo_source = None
            if media_path and os.path.exists(media_path):
                photo_source = FSInputFile(media_path)
            elif media_url:
                photo_source = media_url

            if photo_source:
                msg = await _bot.send_photo(
                    target,
                    photo=photo_source,
                    caption=text[:1024],
                    parse_mode=ParseMode.HTML,
                )
    except Exception as photo_err:
        logger.warning(
            f"Post #{post['id']}: photo send failed ({photo_err}), falling back to text-only"
        )

    # ── Fallback: text-only ───────────────────────────────────────────────
    if msg is None:
        try:
            msg = await _bot.send_message(
                target,
                text[:4096],
                parse_mode=ParseMode.HTML,
            )
        except Exception as text_err:
            # Even text failed — mark as failed so queue is not blocked
            logger.error(
                f"Post #{post['id']}: text-only fallback also failed: {text_err} — marking as publish_failed"
            )
            await _db.update_post_status(post["id"], "publish_failed")
            return False

    # ── Record publication ────────────────────────────────────────────────
    await _db.update_post_status(post["id"], "published")
    await _db.add_published(post["id"], msg.message_id)
    logger.info(f"Published post #{post['id']} to {target}")

    # ── Emoji reaction directly on the post ───────────────────────────────
    # Telegram bots can set only ONE reaction per message (non-premium limit).
    try:
        _t = (post.get("rewritten_text") or post["original_text"]).lower()
        if any(w in _t for w in ["погиб", "авария", "дтп", "пожар", "трагед", "жертв"]):
            _emoji = "😢"                                          # tragedy → empathy
        elif any(w in _t for w in ["жкх", "тариф", "чиновник", "мэр", "депутат", "бюджет"]):
            _emoji = "😡"                                          # bureaucracy → sarcasm
        elif any(w in _t for w in ["открыт", "новый", "запуст", "построен", "победил"]):
            _emoji = "🔥"                                          # good news → positivity
        elif any(w in _t for w in ["цен", "подорожал", "рост", "инфляц", "зарплат"]):
            _emoji = "😡"                                          # prices → frustration
        else:
            _emoji = "👍"                                          # universal default
        await _bot.set_message_reaction(
            chat_id=target,
            message_id=msg.message_id,
            reaction=[ReactionTypeEmoji(emoji=_emoji)],
        )
        logger.info(f"Post #{post['id']}: reaction set {_emoji}")
    except Exception as react_err:
        logger.warning(f"Post #{post['id']}: reaction failed ({react_err})")

    # ── Cross-post to VK ──────────────────────────────────────────────────
    if _vk_publisher and _vk_publisher.enabled:
        try:
            photo_for_vk_url = post.get("replacement_media_url")
            # Priority: 1) local stock file (already on disk), 2) original TG photo file, 3) URL
            photo_for_vk_path = (
                local_stock
                or (media_path if media_path and os.path.exists(media_path) else None)
            )
            logger.info(
                f"Post #{post['id']}: starting VK crosspost "
                f"(photo={'local' if photo_for_vk_path else ('url' if photo_for_vk_url else 'none')})"
            )
            vk_post_id = await _vk_publisher.publish(
                text,
                photo_url=photo_for_vk_url,
                photo_path=photo_for_vk_path,
            )
            if vk_post_id:
                logger.info(f"Post #{post['id']} cross-posted to VK (vk_post_id={vk_post_id})")
            else:
                logger.warning(f"Post #{post['id']} VK crosspost failed — publish() returned None")
        except Exception as e:
            logger.error(f"VK crosspost error for post #{post['id']}: {e}", exc_info=True)
    elif _vk_publisher and not _vk_publisher.enabled:
        logger.debug("VK crosspost skipped: token or group_id not configured")

    # ── Cross-post to MAX ─────────────────────────────────────────────────
    if _max_publisher and _max_publisher.enabled:
        try:
            photo_for_max = post.get("replacement_media_url")
            max_post_id = await _max_publisher.publish(text, photo_url=photo_for_max)
            if max_post_id:
                logger.info(f"Post #{post['id']} cross-posted to MAX (mid={max_post_id})")
            else:
                logger.warning(f"Post #{post['id']} MAX crosspost failed")
        except Exception as e:
            logger.error(f"MAX crosspost error for post #{post['id']}: {e}", exc_info=True)

    return True


# ── Post Processing Pipeline ────────────────────────────────────────────

async def process_new_post(post_id: int):
    """Full processing pipeline for a new post: rewrite + media check + send for review."""
    post = await _db.get_post(post_id)
    if not post:
        return

    logger.info(f"Processing new post #{post_id}")
    original_text = _clean_text(post["original_text"])
    text_lower = original_text.lower()

    # Step 0a: Ad filter — skip promotional posts
    # Tier 1: hard stop — 1 word is enough for blatant ads
    _HARD_AD_WORDS = [
        "лицо бренда", "лицо kari", "лицо бренд", "амбассадор",
        "спонсор", "партнёрский материал", "на правах рекламы",
        "на правах социальной", "социальная реклама",
        "erid", "orid", "рекламодатель", "рекламный пост",
        "поспешите приобрести", "успейте купить", "не упустите свой шанс",
        "новинки коллекции", "коллаборации первого уровня",
        "подробности — читайте в карточках",
        # Донат-посты и призывы к пожертвованиям
        "радар работает благодаря вам", "поддержите любой суммой",
        "даже небольшой донат", "мы не размещаем рекламу",
        "проект держится на поддержке", "задонатить",
        # Саморекламные посты источника
        "подписаться в vk", "подписаться в тг", "подписаться в tg",
        "прислать новость", "telegram заблокируют", "телеграм заблокируют",
        "не реклама!!!", "не реклама!", "это не реклама",
    ]
    if any(w in text_lower for w in _HARD_AD_WORDS):
        await _db.update_post_status(post_id, "rejected")
        logger.info(f"Post #{post_id} rejected: hard ad keyword matched")
        return

    # Tier 2: soft stop — 2+ generic ad words
    ad_matches = [w for w in _config.ad_stop_words if w in text_lower]
    if len(ad_matches) >= 2:  # 2+ ad stop-words = spam
        await _db.update_post_status(post_id, "rejected")
        logger.info(f"Post #{post_id} rejected: ad/spam (matched: {', '.join(ad_matches[:3])})")
        return



    # Step 0b: Relevance filter for federal channels
    source = post["source_channel"].lower()

    # Channels whose username contains these fragments are treated as local without geo-check
    _LOCAL_SOURCE_KEYWORDS = [
        "izhevsk", "izh", "udm", "удмурт", "ижевск", "18",
        "radar", "vrv", "izhlife", "udm18", "ижlife", "иж18",
        "радар", "ижевск", "удмуртия", "вятка", "ижнет",
    ]
    # Fully-trusted channels: always pass without geo-filter regardless of username
    _TRUSTED_LOCAL_CHANNELS = [
        "vrv_radar", "vrv radar", "izhevsk_today", "izhlife",
        "udmurtia_news", "izh_radar", "radar18",
    ]

    is_local = (
        any(kw in source for kw in _LOCAL_SOURCE_KEYWORDS)
        or any(trusted in source for trusted in _TRUSTED_LOCAL_CHANNELS)
    )
    is_radar_source = any(m in source for m in RADAR_SOURCE_MARKERS)
    has_geo = _has_local_geo(original_text)
    looks_federal = _looks_federal_news(original_text)
    has_non_local_geo = _has_non_local_geo(original_text)

    if _should_reject_by_geo(
        is_local_source=is_local,
        has_local_geo=has_geo,
        looks_federal=looks_federal,
        has_non_local_geo=has_non_local_geo,
    ):
        await _db.update_post_status(post_id, "rejected")
        if is_local and has_non_local_geo:
            logger.info(f"Post #{post_id} rejected: local source but non-local geo markers in text")
        else:
            logger.info(f"Post #{post_id} rejected: no local geo markers in text")
        return

    if not is_local:
        # Secondary AI check for nuanced relevance (only if geo check passed)
        is_relevant = await _rewriter.check_relevance(original_text)
        if not is_relevant:
            await _db.update_post_status(post_id, "rejected")
            logger.info(f"Post #{post_id} rejected: regional news from federal channel @{source}")
            return

    # Step 0c: Deduplication — smart two-tier check
    # Tier 1: Compare against PUBLISHED posts (last 12h) — don't repeat what's already on the channel
    published_texts = await _db.get_texts_by_status(["published"], hours=12)
    published_match = _find_similar_match(original_text, published_texts)
    if published_match:
        await _db.update_post_status(post_id, "rejected")
        logger.info(
            f"Post #{post_id} rejected: similar to published post "
            f"(similarity={published_match['similarity']:.2f}, overlap={published_match['overlap']:.2f})"
        )
        return

    # Tier 2: Compare against QUEUED posts (pending/rewriting/approved) — first-in-queue wins, later duplicates rejected
    queued_texts = await _db.get_texts_by_status(["pending", "rewriting", "approved"], hours=12)
    queued_match = _find_similar_match(original_text, queued_texts, queued=True)
    if queued_match:
        await _db.update_post_status(post_id, "rejected")
        logger.info(
            f"Post #{post_id} rejected: similar post already in queue "
            f"(similarity={queued_match['similarity']:.2f}, overlap={queued_match['overlap']:.2f})"
        )
        return

    # Step 0d: Breaking news detection — auto-publish without moderation.
    # Radar source alone is not enough: breaking mode is only for posts with local geo markers.
    is_breaking = _is_breaking_candidate(
        original_text,
        is_radar_source=is_radar_source,
        has_geo=has_geo,
        breaking_keywords=_config.breaking_keywords,
    )

    # Step 1: AI Rewrite + hashtags + photo keywords (all in ONE Gemini call to save quota)
    await _db.update_post_status(post_id, "rewriting")
    _ai_hashtags: list = []
    _ai_photo_keywords: list = []
    async with _ai_semaphore:
        rewritten, engine, _ai_hashtags, _ai_photo_keywords = await _rewriter.rewrite_full(original_text)

    if rewritten:
        rewritten = _clean_text(rewritten)  # Clean AI output too

        # Guard: if AI returned a refusal message — reject post immediately
        if _rewriter._is_refusal(rewritten):
            await _db.update_post_status(post_id, "rejected")
            logger.warning(f"Post #{post_id} rejected: AI refusal detected in rewritten text")
            return

        uniqueness = _rewriter.calculate_uniqueness(original_text, rewritten)
        logger.info(f"Post #{post_id} rewritten by {engine} (uniqueness: {uniqueness:.0%})")
    else:
        rewritten = original_text
        logger.warning(f"Post #{post_id}: AI rewrite failed, using original text")

    # Step 2: Deduplicate by REWRITTEN text BEFORE formatting
    # (must be done before format_post adds the same footer/hashtags to every post)
    published_rewritten = await _db.get_rewritten_texts_by_status(["published"], hours=12)
    rewritten_match = _find_similar_match(rewritten, published_rewritten)
    if rewritten_match:
        await _db.update_post_status(post_id, "rejected")
        logger.info(
            f"Post #{post_id} rejected: rewritten text too similar to recently published post "
            f"(similarity={rewritten_match['similarity']:.2f}, overlap={rewritten_match['overlap']:.2f})"
        )
        return

    # Step 2.5: Format post (hashtags already from rewrite_full)
    rewritten = _format_post(rewritten, _ai_hashtags)

    await _db.update_post_rewrite(post_id, rewritten)

    # Step 3b: Smart photo strategy (no Gemini call — saves quota)
    #
    # Priority:
    #   1. Post has original photo WITHOUT watermark → use it as-is, skip stock search
    #   2. Post has original photo WITH watermark → replace with stock
    #   3. Post has no photo → search stock only if keywords are highly specific
    #   4. No suitable photo found → publish text-only (better than generic stock)
    try:
        stock_url = None
        has_original_clean = (
            post["media_type"] == "photo"
            and post.get("media_local_path")
            and not has_watermark
        )

        if has_original_clean:
            # Original photo from source — best quality, keep it
            logger.info(f"Post #{post_id}: using original source photo (no watermark)")
        else:
            # Need stock: either watermark replacement or no photo at all
            keywords = _ai_photo_keywords or _rewriter._extract_keywords_fallback(original_text)

            if keywords and len(keywords) >= 2:
                stock_photos = await _media_processor.search_stock_photo(keywords, count=3)

                if stock_photos:
                    # Take FIRST result — Wikimedia already sorts by relevance
                    # Skip if description doesn't share ANY keyword (basic sanity check)
                    best = stock_photos[0]
                    desc_lower = (best.get("description", "") + " " + " ".join(keywords)).lower()
                    # Accept if ≥1 keyword appears in description or title
                    kw_match = any(kw.lower() in desc_lower for kw in keywords[:3])
                    if kw_match or has_watermark:
                        stock_url = best["url"]
                        logger.info(
                            f"Post #{post_id}: stock photo selected "
                            f"(kw_match={kw_match}, keywords={keywords[:3]})"
                        )
                    else:
                        logger.info(
                            f"Post #{post_id}: stock photo description mismatch — "
                            f"publishing text-only (keywords={keywords[:3]})"
                        )
                else:
                    logger.info(f"Post #{post_id}: no stock photos found — publishing text-only")
            else:
                logger.info(f"Post #{post_id}: insufficient keywords for stock search — text-only")

        if stock_url:
            await _db.update_post_media(post_id, replacement_url=stock_url)
    except Exception as e:
        logger.error(f"Post #{post_id}: stock photo search failed: {e}")

    # Step 4: Breaking news → auto-publish without moderation; regular → auto-approve for queue
    if is_breaking:
        logger.info(f"⚡ Post #{post_id} is BREAKING NEWS — auto-publishing!")
        updated_post = await _db.get_post(post_id)
        await _db.update_post_status(post_id, "approved")
        success = await _publish_post(updated_post)
        if success:
            for admin_id in _config.admin_ids:
                try:
                    await _bot.send_message(
                        admin_id,
                        f"⚡ <b>СРОЧНАЯ НОВОСТЬ</b> автоматически опубликована!\n\n"
                        f"Пост #{post_id} из @{post['source_channel']}",
                        parse_mode=ParseMode.HTML,
                    )
                except Exception:
                    pass
        return  # Breaking news processing complete, skip regular queue flow

    # Regular post — auto-approve and add to publish queue
    logger.info(f"Post #{post_id} auto-approved — will be published on next interval")
    await _db.update_post_status(post_id, "approved")

    # Notify admins (optional, informational only — no action needed)
    approved_count = await _db.get_approved_posts()
    for admin_id in _config.admin_ids:
        try:
            await _bot.send_message(
                admin_id,
                f"✅ Пост #{post_id} добавлен в очередь публикации.\n"
                f"📋 В очереди: {len(approved_count)} постов\n"
                f"⏰ Следующий выход — через ~{_config.publish_interval // 60} мин",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass


# ── Auto-Publish Scheduler ───────────────────────────────────────────────

async def auto_publish_loop():
    """Background task: auto-publish ONE approved post per interval.
    
    Checks the queue every 60 seconds. Publishes a post only if enough time
    has passed since the last publication (governed by PUBLISH_INTERVAL).
    This way posts don't sit waiting for up to PUBLISH_INTERVAL seconds.
    """
    last_published_at: float = 0.0
    CHECK_EVERY = 60  # Check queue every 60 seconds

    while True:
        try:
            await asyncio.sleep(CHECK_EVERY)

            if not _config or not _db:
                continue

            interval = _config.publish_interval
            import time
            now = time.monotonic()

            # Not enough time since last publish
            if now - last_published_at < interval:
                continue

            approved = await _db.get_approved_posts()
            if not approved:
                continue

            # Publish only ONE post per interval
            post = approved[0]
            success = await _publish_post(post)

            if success:
                last_published_at = time.monotonic()
                logger.info(f"Auto-publisher: published post #{post['id']} ({len(approved)-1} remaining in queue)")
                for admin_id in _config.admin_ids:
                    try:
                        remaining = len(approved) - 1
                        await _bot.send_message(
                            admin_id,
                            f"📢 Авто-публикация: опубликован пост #{post['id']}.\n"
                            f"📋 В очереди осталось: {remaining}",
                        )
                    except Exception:
                        pass

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Auto-publish error: {e}")
            await asyncio.sleep(60)


# ── Bot Initialization ──────────────────────────────────────────────────

def create_bot(config: Config, db: Database, rewriter: AIRewriter, media_proc: MediaProcessor, vk_pub: Optional[VKPublisher] = None) -> tuple:
    """Create and configure the bot. Returns (bot, dispatcher)."""
    global _config, _db, _rewriter, _media_processor, _vk_publisher, _bot

    _config = config
    _db = db
    _rewriter = rewriter
    _media_processor = media_proc
    _vk_publisher = vk_pub

    bot = Bot(token=config.bot_token)
    _bot = bot

    dp = Dispatcher()
    dp.include_router(router)

    return bot, dp
