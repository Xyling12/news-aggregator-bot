"""
Telegram Bot — Aiogram 3 bot for admin moderation, post management, and publishing.
"""

import asyncio
import json
import logging
import os
import random
import re
import traceback
from datetime import datetime as dt, timedelta, timezone
from typing import Optional

import aiohttp

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
from src.story_generator import StoryGenerator
from src.utils import (
    escape_html,
    clean_text,
    word_overlap,
    is_similar_to_any,
    find_similar_candidate,
    find_same_event_candidate,
    extract_event_entities,
    detect_rubric,
    format_post,
    RUBRIC_MAP,
    BREAKING_KEYWORDS,
)
from src.content_filter import filter_sensitive_content, FilterAction

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
_story_generator: Optional[StoryGenerator] = None
_bot: Optional[Bot] = None

# Rate limiting: max 3 concurrent AI calls to avoid Gemini 429 errors
_ai_semaphore = asyncio.Semaphore(3)

# Global dedup set for stock photo URLs — prevents same image appearing on multiple posts
# published in quick succession (parallel processing race condition)
_used_stock_urls: set[str] = set()
_used_stock_urls_lock = asyncio.Lock()

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
                from src.ai_rewriter import _GenConfig as _GC
                resp = await loop.run_in_executor(
                    None,
                    lambda: model.generate_content(
                        test_prompt,
                        generation_config=_GC(max_output_tokens=50),
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
        from google import genai as _genai_check
        _tc = _genai_check.Client(api_key=_config.gemini_api_key)
        response = _tc.models.generate_content(
            model="gemini-2.0-flash",
            contents="Скажи одно слово: привет",
        )
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

            # ── Фильтр чувствительного контента ──────────────────────────
            _cf_result = filter_sensitive_content(rewritten)
            if _cf_result.action == FilterAction.BLOCK:
                await _db.update_post_status(post_id, "rejected")
                logger.warning(
                    f"Post #{post_id} BLOCKED by content_filter (manual rewrite): {_cf_result.reason}"
                )
                await callback.message.reply(
                    f"🚫 Пост заблокирован фильтром чувствительного контента.\n"
                    f"<code>{_cf_result.reason}</code>",
                    parse_mode=ParseMode.HTML,
                )
                return
            rewritten = _cf_result.text  # Возможно заменены эвфемизмы / добавлен дисклеймер
            # ─────────────────────────────────────────────────────────────

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


# ── Alert image pool ────────────────────────────────────────────────────
# Official-style templates for emergency posts, so air-raid/danger news always
# gets a clean on-topic image instead of a random Moscow stock photo.
_ALERTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "alerts"
)
# Danger subjects — the post must clearly be about one of these to get an alert card.
_ALERT_SUBJECTS = {
    "rocket": ("ракетная опас", "ракетной опас", "ракетную опас"),
    "sky": ("опасное небо", "опасного неба", "опасному небу"),
    "drones": ("беспилотн", "бпла"),
    "sirens": ("воздушная тревог", "воздушной тревог", "звуки сирен", "звуках сирен",
               "звук сирен", "вой сирен", "сигнал сирен", "сигналы сирен"),
}
_ALERT_FILES = {
    "rocket": ("rocket_1.png", "rocket_2.png"),
    "sky": ("sky_1.png", "sky_2.png"),
    "drones": ("drones_1.png",),
    "sirens": ("sirens_1.png",),
}
# Cancellation markers — only consulted once a danger subject is already matched.
_ALERT_CANCEL = ("отмен", "отбой", "сняли", "снят", "миновал", "завершён", "завершен")


# News category → (label, card colour, mode, fixed_stock_keywords).
# mode "stock" = use a real photo. To avoid foreign cityscapes we pass our OWN
# neutral close-up keywords (objects/interiors), NOT the AI's "city" keywords.
# mode "card" = branded card (kept only where any photo would look like a city —
# власть/officials and the generic default).
_NEWS_CATEGORIES = [
    (("дтп", "авар", "пожар", "полиц", "суд ", "суда", "осуд", "кража", "мошен",
      "задержа", "арест", "происш", "погиб", "пострад", "ножев", "взлом", "наезд",
      "сбит", "ракет", "атаков", "противник"),
     ("Происшествия", (176, 42, 46), "card", None)),
    (("трамва", "троллейбус", "автобус", "маршрут", "дорог", "транспорт", "пробк",
      "остановк", "перекрыт", "тротуар"),
     ("Транспорт", (33, 79, 140), "stock", ["tram close up", "city bus interior"])),
    (("мэр", "глава города", "депутат", "госсовет", "администрац", "бюджет",
      "губернат", "власт", "чиновник", "министр", "госдум", "выбор"),
     ("Власть", (40, 54, 85), "card", None)),
    (("жкх", "отоплен", "коммунал", "водоснаб", "тариф", "электроснаб", "газоснаб",
      "капремонт", "управляющ"),
     ("ЖКХ", (20, 110, 110), "stock", ["heating radiator close up", "water pipes utility"])),
    (("цена", "подорожал", "зарплат", "налог", "бизнес", "эконом", "инфляц",
      "кредит", "ипотек", "пенси", "пособи"),
     ("Экономика", (150, 90, 30), "stock", ["ruble banknotes money", "calculator finance documents"])),
    (("школ", "детсад", "вуз", "универ", "образован", "ученик", "студент", "егэ"),
     ("Образование", (70, 90, 150), "stock", ["empty classroom desks", "books library"])),
    (("больниц", "поликлин", "врач", "медиц", "здоров", "вакцин", "госпитал"),
     ("Здоровье", (28, 120, 92), "stock", ["hospital corridor", "stethoscope medical"])),
    (("театр", "концерт", "выставк", "фестивал", "культур", "музей", "кино", "премьер"),
     ("Культура", (120, 50, 130), "stock", ["theater stage curtain", "concert stage lights"])),
    (("спорт", "матч", "турнир", "чемпион", "соревнов", "стадион"),
     ("Спорт", (35, 100, 60), "stock", ["stadium sport field", "running track"])),
    # photo-friendly — AI/weather keywords are fine (sky/nature, never a city)
    (("погод", "температур", "прогноз", "осадк", "дожд", "снег", "мороз", "гроза", "ветер"),
     ("Погода", None, "stock", None)),
    (("природ", "лес", "река", "парк", "животн", "птиц", "рыбал", "озер", "сад", "урожай"),
     ("Природа", None, "stock", None)),
]
_DEFAULT_CATEGORY = ("Новости", (45, 55, 75), "card", None)
_CIVIC_CATEGORIES = {"Транспорт", "ЖКХ", "Экономика", "Власть"}
_POLLS_PER_DAY = 2  # at most N polls/day so they aren't under every post
_SELF_COMMENTS_PER_DAY = 4  # at most N seeded first-comments/day — under every post it reads as a bot
# Never seed a discussion comment under tragedies/casualties
_TRAGEDY_WORDS = (
    "погиб", "умер", "сконча", "жертв", "труп", "смерт", "трагед",
    "убий", "суицид", "выпал из окна", "утону", "сбил", "насмерть",
)


def _detect_news_category(text: str):
    """Return (label, card_color, mode) for a news text."""
    # Strip hashtags first — an AI tag like #спорт must NOT decide the category
    # (a moon post tagged #спорт was mislabelled "СПОРТ").
    t = re.sub(r"#\S+", " ", (text or "").lower())
    for triggers, cat in _NEWS_CATEGORIES:
        if any(k in t for k in triggers):
            return cat
    return _DEFAULT_CATEGORY


def _is_air_raid(text: str) -> bool:
    """True if the text is about an air-raid/danger subject (rocket/sky/drone/siren).
    Used to force instant publishing — these can't wait in the queue."""
    t = (text or "").lower()
    return any(any(w in t for w in kws) for kws in _ALERT_SUBJECTS.values())


def _pick_alert_image(text: str) -> Optional[str]:
    """Return a local official-style template path for an emergency post, or None.

    Only fires when the text is clearly about an air-raid/danger subject, so
    everyday phrases like «сняли ограничения на дороге» don't trigger it.
    """
    t = (text or "").lower()
    subject = next((k for k, kws in _ALERT_SUBJECTS.items() if any(w in t for w in kws)), None)
    if not subject:
        return None
    if any(c in t for c in _ALERT_CANCEL):
        files = ("cancel_1.png", "cancel_2.png")
    else:
        files = _ALERT_FILES[subject]
    existing = [
        os.path.join(_ALERTS_DIR, f)
        for f in files
        if os.path.exists(os.path.join(_ALERTS_DIR, f))
    ]
    return random.choice(existing) if existing else None


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

    allow_source_media = bool(_config and _config.use_source_media)
    media_path = post.get("media_local_path") if allow_source_media else None
    media_url = post.get("media_file_id") if allow_source_media else None
    replacement_url = post.get("replacement_media_url")

    # Collect extra photo paths (album posts)
    extra_paths: list[str] = []
    if allow_source_media:
        raw_extra = post.get("media_extra_paths")
        if raw_extra:
            try:
                parsed = json.loads(raw_extra)
                if isinstance(parsed, list):
                    extra_paths = [p for p in parsed if isinstance(p, str) and os.path.exists(p)]
            except Exception:
                pass

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
        elif (
            post["media_type"] == "video"
            and media_path
            and media_path.lower().endswith(".mp4")
            and os.path.exists(media_path)
        ):
            # Real source video — repost it as a video, not a stock photo
            msg = await _bot.send_video(
                target,
                video=FSInputFile(media_path),
                caption=text[:1024],
                parse_mode=ParseMode.HTML,
                supports_streaming=True,
            )
        elif post["media_type"] in ("photo", "video"):
            # For video posts without an MP4, media_local_path is the preview frame
            primary_source = None
            if media_path and os.path.exists(media_path):
                primary_source = FSInputFile(media_path)
            elif media_url and post["media_type"] == "photo":
                primary_source = media_url

            if primary_source:
                if extra_paths:
                    # Album post — send all photos as a media group
                    from aiogram.types import InputMediaPhoto
                    all_sources = [primary_source] + [FSInputFile(p) for p in extra_paths]
                    media_group = [
                        InputMediaPhoto(
                            media=src,
                            caption=text[:1024] if i == 0 else None,
                            parse_mode=ParseMode.HTML if i == 0 else None,
                        )
                        for i, src in enumerate(all_sources[:10])
                    ]
                    msgs = await _bot.send_media_group(target, media=media_group)
                    msg = msgs[0] if msgs else None
                else:
                    msg = await _bot.send_photo(
                        target,
                        photo=primary_source,
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
    vk_post_id = None
    if _vk_publisher and _vk_publisher.enabled:
        try:
            photo_for_vk_url = post.get("replacement_media_url")
            # Real source video → upload to VK as a video attachment (not a photo)
            is_video_file = bool(
                post["media_type"] == "video"
                and media_path
                and media_path.lower().endswith(".mp4")
                and os.path.exists(media_path)
            )
            video_attachment = None
            if is_video_file:
                try:
                    video_attachment = await _vk_publisher.upload_video(
                        media_path, name=re.sub(r'<[^>]+>', '', text)[:80]
                    )
                except Exception as v_err:
                    logger.warning(f"Post #{post['id']}: VK video upload failed ({v_err})")
                photo_for_vk_path = None  # don't try to send an mp4 as a photo
            else:
                # Priority: 1) local stock file (on disk), 2) original TG photo file, 3) URL
                photo_for_vk_path = (
                    local_stock
                    or (media_path if media_path and os.path.exists(media_path) else None)
                )
            logger.info(
                f"Post #{post['id']}: starting VK crosspost "
                f"(media={'video' if video_attachment else ('local' if photo_for_vk_path else ('url' if photo_for_vk_url else 'none'))})"
            )

            # ── VK engagement: AI first-comment + optional poll (boosts smart-feed) ──
            eng_comment = None
            poll_attachment = None
            if _config.vk_self_comment_enabled and _rewriter:
                try:
                    eng_source = re.sub(r'<[^>]+>', '', text)
                    eng_source = re.sub(r'#\S+', '', eng_source).strip()
                    engagement = await _rewriter.generate_engagement(eng_source)
                    eng_comment = engagement.get("comment")
                    poll = engagement.get("poll")
                    _els = eng_source.lower()
                    if eng_comment and any(w in _els for w in _TRAGEDY_WORDS):
                        eng_comment = None  # no discussion prompts under tragedies
                    if eng_comment:
                        _cmday = dt.now(timezone(timedelta(hours=4))).strftime("%Y-%m-%d")
                        if await _db.get_daily_counter("selfcomment", _cmday) >= _SELF_COMMENTS_PER_DAY:
                            eng_comment = None
                        else:
                            await _db.bump_daily_counter("selfcomment", _cmday)
                    # Polls only on civic topics AND capped per day — civic categories
                    # cover most news, so without a cap polls end up under every post.
                    cat_label = _detect_news_category(eng_source)[0]
                    if poll and poll.get("options") and cat_label in _CIVIC_CATEGORIES:
                        _pday = dt.now(timezone(timedelta(hours=4))).strftime("%Y-%m-%d")
                        if await _db.get_daily_counter("pollcount", _pday) < _POLLS_PER_DAY:
                            poll_attachment = await _vk_publisher.create_poll(
                                poll["question"], poll["options"]
                            )
                            if poll_attachment:
                                await _db.bump_daily_counter("pollcount", _pday)
                except Exception as eng_err:
                    logger.warning(f"Post #{post['id']}: engagement gen failed ({eng_err})")

            combined_attachment = ",".join(
                a for a in (video_attachment, poll_attachment) if a
            ) or None
            # Album posts: send the extra source photos to VK too (only when the
            # primary is the source photo — not a stock/card/video).
            vk_extra_photos = extra_paths if (photo_for_vk_path == media_path and not video_attachment) else None
            vk_post_id = await _vk_publisher.publish(
                text,
                photo_url=photo_for_vk_url,
                photo_path=photo_for_vk_path,
                seo_enabled=_config.vk_seo_enabled,
                seo_max_tags=_config.vk_seo_max_tags,
                extra_attachment=combined_attachment,
                extra_photo_paths=vk_extra_photos,
            )
            if vk_post_id:
                logger.info(f"Post #{post['id']} cross-posted to VK (vk_post_id={vk_post_id})")
                # Social proof: 1 like on our own post (posts otherwise look dead at 0)
                try:
                    await _vk_publisher.like_post(vk_post_id)
                except Exception:
                    pass

                # Repost the source VIDEO to VK Clips — fresh, local, current content
                # (skip accidents/tragedies; cap a few per day).
                if is_video_file and media_path:
                    try:
                        _vt = re.sub(r'<[^>]+>', '', text).lower()
                        _vcat = _detect_news_category(_vt)[0]
                        _sensitive = _vcat == "Происшествия" or any(
                            w in _vt for w in (
                                "погиб", "авари", "дтп", "пожар", "труп", "жертв",
                                "стрель", "взрыв", "убий", "ножев", "суицид", "трагед", "смерт",
                            )
                        )
                        _cday = dt.now(timezone(timedelta(hours=4))).strftime("%Y-%m-%d")
                        if not _sensitive and await _db.get_daily_counter("srcclips", _cday) < 3:
                            _ccap = re.sub(r'<[^>]+>', '', text).strip().split("\n")[0][:200]
                            _clip_id = await _vk_publisher.upload_clip(
                                media_path, caption=_ccap,
                                link_url=f"https://vk.com/wall-{_vk_publisher.group_id}_{vk_post_id}",
                            )
                            if _clip_id:
                                await _db.bump_daily_counter("srcclips", _cday)
                                logger.info(f"Post #{post['id']}: source video → VK Clips ✅")
                    except Exception as _ce:
                        logger.warning(f"Post #{post['id']}: source-video clip failed ({_ce})")
                # Seed a first comment from the community to spark discussion
                if eng_comment:
                    try:
                        await _vk_publisher.create_comment(vk_post_id, eng_comment)
                    except Exception as c_err:
                        logger.warning(f"Post #{post['id']}: VK self-comment failed ({c_err})")
            else:
                logger.warning(f"Post #{post['id']} VK crosspost failed — publish() returned None")
        except Exception as e:
            logger.error(f"VK crosspost error for post #{post['id']}: {e}", exc_info=True)
    elif _vk_publisher and not _vk_publisher.enabled:
        logger.debug("VK crosspost skipped: token or group_id not configured")

    # ── Publish as VK Story (only the first 2 news/day — no story-spam) ─────
    if _vk_publisher and _vk_publisher.enabled and _story_generator:
        try:
            _day_key = dt.now(timezone(timedelta(hours=4))).strftime("%Y-%m-%d")
            news_stories_today = await _db.get_daily_counter("newsstory", _day_key)
            if news_stories_today >= 2:
                logger.debug(f"Post #{post['id']}: news-story daily cap reached, skipping story")
            else:
                raw_text = re.sub(r'<[^>]+>', '', text)
                raw_text = re.sub(r'#\S+', '', raw_text).strip()
                first_sentence = raw_text.split('.')[0].strip() if '.' in raw_text[:150] else raw_text[:120]
                if len(first_sentence) > 15:
                    photo_for_story = post.get("replacement_media_url")
                    story_bytes = await _story_generator.generate_news_story(
                        first_sentence, photo_url=photo_for_story
                    )
                    if story_bytes:
                        _story_link = (
                            f"https://vk.com/wall-{_vk_publisher.group_id}_{vk_post_id}"
                            if vk_post_id else "https://vk.com/izhevsk_segodnya"
                        )
                        story_result = await _vk_publisher.upload_story_photo(
                            story_bytes, link_text="learn_more", link_url=_story_link
                        )
                        if story_result:
                            await _db.bump_daily_counter("newsstory", _day_key)
                            logger.info(f"Post #{post['id']}: VK Story published!")
                        else:
                            logger.warning(f"Post #{post['id']}: VK Story upload failed")
        except Exception as e:
            logger.error(f"VK Story error for post #{post['id']}: {e}")

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
        # Медицинские/клиника/роды-реклама
        "принимаем роды", "платные роды", "роды под контролем",
        "запишитесь на приём", "запишитесь к врачу", "онлайн запись",
        "акция для пациентов", "бесплатная консультация врача",
        "звоните нам", "наш номер телефона", "обращайтесь к нам",
        # Недвижимость/строительство-реклама
        "звоните и приезжайте", "позвоните нам", "наши менеджеры",
        "оставьте заявку", "заполните форму", "получите скидку",
        "узнайте подробности", "узнайте цену", "узнайте стоимость",
        # Общие рекламные паттерны
        "переходите по ссылке", "перейдите по ссылке", "нажмите на ссылку",
        "ссылка в шапке профиля", "ссылка в описании", "ссылка в bio",
        "пишите в директ", "пишите в лс", "написать в директ",
        "⬇️ подробнее", "👇 подробнее", "👇 жми",
        # Ресторанные/развлекательные акции
        "специальное предложение", "день суши", "день пиццы", "день бургер",
        "роллов всего за", "роллов за", "пицц за", "бургеров за",
        "в программе — специальное", "акция:", "акция!", "акция в ",
        "сертификат на скидку", "дарим скидку", "скидка именинникам",
        "бронируйте столик", "забронировать столик", "бронируй стол",
        "happy hour", "хэппи аур", "business lunch", "бизнес-ланч от",
        # MLM / инфобизнес / «лёгкий заработок»
        "создайте свой чат-бот", "создай свой чат-бот", "создать чат-бота и зарабат",
        "зарабатывайте от", "зарабатывай от", "зарабатывать от",
        "доход от 70", "доход в 70", "доход 70-80", "70-80 тысяч",
        "тысяч рублей в месяц", "тысяч в месяц удал", "пассивный доход",
        "не нужно быть программистом", "не нужно иметь высшее",
        "пошаговые инструкции по созданию", "покинуть офисные будни",
        "финансовая свобода", "удалённый заработок", "удаленный заработок",
        "работа на дому от", "инвестиции с гарантией", "гарантированный доход",
        # Нативная реклама / заказуха (антиреклама конкурентов, «партнёр проекта»)
        "станет главным партнером", "станет главным партнёром",
        "главным партнером этого", "главным партнёром этого",
        "наш партнёр", "наш партнер", "партнёр проекта", "партнер проекта",
        "подслушала разговор", "подслушал разговор", "по секрету расскажу",
        "спа maitai", "спа майтай", "тайский спа",
        # Нативка «я случайно узнала/стала свидетелем» + антиреклама магазина
        "стала свидетелем", "стал свидетелем", "свидетелем обсуждения",
        "обманывают покупателей", "наклеивают жёлтые ценники", "наклеивают желтые ценники",
        "золотое яблоко", "предпочитает заказывать", "заказывать в другом месте",
        # Job/«подработка на дому» скам
        "ищем сотрудников", "ищу сотрудников", "требуются сотрудники",
        "подработка на дому", "подработку на дому", "работа на дому",
        "пишите менеджеру", "напишите менеджеру", "пиши менеджеру",
        "ежедневная оплата", "ежедневную оплату", "минимальная занятость",
        "оплата от 2000", "работа полностью удалённ", "работа полностью удаленн",
        "нет опыта — это предложение", "от 18 лет и у вас нет опыта",
    ]
    if any(w in text_lower for w in _HARD_AD_WORDS):
        await _db.update_post_status(post_id, "rejected")
        logger.info(f"Post #{post_id} rejected: hard ad keyword matched")
        return

    # Tier 1b: income-promise regex — «от 70 до 250 тысяч рублей», «от 2000 до 5000 рублей»,
    # «2000-5000 ₽», «100 000 ₽/мес» и т.п. (с тыс. ИЛИ просто рубли + контекст работы/дохода)
    _money = re.search(
        r"от\s*\d[\d\s]{1,7}\s*(?:до\s*\d[\d\s]{1,7}\s*)?(?:тыс|000|руб|₽)"
        r"|\d[\d\s]{2,7}\s*[-–—]\s*\d[\d\s]{2,7}\s*(?:руб|₽|тыс)",
        text_lower,
    )
    if _money and any(
        w in text_lower for w in (
            "заработ", "доход", "рублей в месяц", "в месяц", "ежемесячно",
            "оплата", "оплату", "подработк", "вакансия", "сотрудник", "занятость",
        )
    ):
        await _db.update_post_status(post_id, "rejected")
        logger.info(f"Post #{post_id} rejected: income/job-ad pattern")
        return

    # Taboo topics — never publish (suicide / self-harm, esp. children). Editorial rule.
    _TABOO = [
        "выпал из окна", "выпала из окна", "выпал с окна", "выпала с окна",
        "выбросился", "выбросилась", "выбросился из окна", "выбросилась из окна",
        "покончил с собой", "покончила с собой", "покончил жизнь", "покончила жизнь",
        "свёл счёты с жизнью", "свела счёты с жизнью", "свел счеты с жизнью",
        "наложил на себя руки", "наложила на себя руки",
        "самоубий", "суицид", "повесил", "повесилась", "вскрыл вены", "вскрыла вены",
        "спрыгнул с", "спрыгнула с", "прыгнул с крыши", "прыгнула с крыши",
        "шагнул из окна", "шагнула из окна", "свёл счёты", "свела счёты",
    ]
    if any(w in text_lower for w in _TABOO):
        await _db.update_post_status(post_id, "rejected")
        logger.info(f"Post #{post_id} rejected: taboo topic (suicide/self-harm)")
        return

    # Tier 2: soft stop — 2+ generic ad words
    ad_matches = [w for w in _config.ad_stop_words if w in text_lower]
    if len(ad_matches) >= 2:  # 2+ ad stop-words = spam
        await _db.update_post_status(post_id, "rejected")
        logger.info(f"Post #{post_id} rejected: ad/spam (matched: {', '.join(ad_matches[:3])})")
        return

    # Step 0b: Topic cooldown — prevent same-topic flood
    _WEATHER_KEYWORDS = ["погод", "температур", "прогноз", "осадк", "гололед", "мороз"]
    text_lower_w = original_text.lower()
    if sum(1 for kw in _WEATHER_KEYWORDS if kw in text_lower_w) >= 2:
        if await _db.has_recent_topic_post(_WEATHER_KEYWORDS[:4], hours=4):
            await _db.update_post_status(post_id, "rejected")
            logger.info(f"Post #{post_id} rejected: weather cooldown (duplicate weather post in last 4h)")
            return

    # Step 0b2: Air-raid / danger cooldown — одна тревожная волна = 1 пост, а не 8.
    # Пропускаем ПЕРВЫЙ сигнал и отдельно отбой/отмену, режем все промежуточные повторы.
    _DANGER_KEYWORDS = [
        "ракетная опасность", "ракетной опасности", "опасное небо", "опасного неба",
        "воздушная тревога", "воздушной тревоги", "беспилотн", "бпла", "дрон",
        "угроза атаки", "сигнал тревог", "звуки сирен", "сирены", "сирен ",
    ]
    _CANCEL_MARKERS = ["отмен", "отбой", "сняли", "завершен", "завершён", "миновал", "опасность мин"]
    is_danger = any(kw in text_lower_w for kw in _DANGER_KEYWORDS)
    is_cancel = any(m in text_lower_w for m in _CANCEL_MARKERS)
    if is_danger and not is_cancel:
        # Если в последние 3 часа уже был тревожный пост — это дубль той же волны
        if await _db.has_recent_topic_post(
            ["ракетная опасн", "опасное небо", "воздушная тревог", "беспилотн", "сирен"],
            hours=3,
        ):
            await _db.update_post_status(post_id, "rejected")
            logger.info(f"Post #{post_id} rejected: air-raid cooldown (duplicate alert within 3h)")
            return

    # Step 0c: Relevance filter for federal channels
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

    if is_local:
        # Local source: apply geo filter — reject only if text explicitly points to another region
        if has_non_local_geo and not has_geo:
            await _db.update_post_status(post_id, "rejected")
            logger.info(f"Post #{post_id} rejected: local source but non-local geo markers in text")
            return
    else:
        # Federal/non-local source: skip hard geo filter, use AI relevance check instead.
        # Hard geo filter was too strict — it blocked international/political/consumer news
        # (e.g. "Аферисты продают БАДы", "Ким Чен Ын") that are relevant to all readers.
        if has_geo or looks_federal:
            # Fast-path: obvious local/federal relevance — skip AI call
            pass
        else:
            # Use AI to decide relevance for everything else
            is_relevant = await _rewriter.check_relevance(original_text)
            if not is_relevant:
                await _db.update_post_status(post_id, "rejected")
                logger.info(f"Post #{post_id} rejected: not relevant per AI check (federal channel @{source})")
                return

    # Step 0c: Deduplication — smart two-tier check
    # Tier 1: Compare against PUBLISHED posts (last 12h) — don't repeat what's already on the channel
    published_texts = await _db.get_texts_by_status(["published"], hours=48)
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

    # Tier 3: Same-event check over 7 days — text similarity misses the same
    # story rewritten differently days later (e.g. the same quote resurfacing).
    week_texts = await _db.get_texts_by_status(["published"], hours=168)
    same_event = find_same_event_candidate(original_text, week_texts)
    if same_event:
        await _db.update_post_status(post_id, "rejected")
        logger.info(
            f"Post #{post_id} rejected: same event already covered this week "
            f"(entities match: {sorted(extract_event_entities(original_text) & extract_event_entities(same_event))[:6]})"
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
    # Air-raid / БПЛА / опасное небо / отбой — ALWAYS instant (skip queue + geo check).
    # Reuses the alert-subject stems so падежи ("ракетной опасности") and "опасное небо"
    # are caught regardless of breaking_keywords case.
    if _is_air_raid(original_text):
        is_breaking = True
        logger.info(f"Post #{post_id}: air-raid/alert → instant publish")

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

        # ── Фильтр чувствительного контента ──────────────────────────────
        _cf_result = filter_sensitive_content(rewritten)
        if _cf_result.action == FilterAction.BLOCK:
            await _db.update_post_status(post_id, "rejected")
            logger.warning(
                f"Post #{post_id} BLOCKED by content_filter (pipeline): {_cf_result.reason}"
            )
            return
        rewritten = _cf_result.text  # Возможно заменены эвфемизмы / добавлен дисклеймер
        # ─────────────────────────────────────────────────────────────────

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

        # Priority 0: emergency posts → use official-style local alert template.
        # Source channels rarely attach the proper МЧС card, and generic stock gives
        # a random Moscow photo, so we override with our own on-topic image.
        alert_img = _pick_alert_image(original_text + " " + (rewritten or ""))
        if alert_img:
            await _db.set_local_media_override(post_id, alert_img)
            logger.info(
                f"Post #{post_id}: emergency post — using local alert template "
                f"{os.path.basename(alert_img)}"
            )
        else:
            has_original_clean = (
                bool(_config.use_source_media)
                and post["media_type"] in ("photo", "video")
                and post.get("media_local_path")
                and not post.get("has_watermark")
            )

            if post["media_type"] in ("photo", "video") and post.get("media_local_path") and _config.use_source_media:
                # Best-effort detector in addition to DB watermark flag.
                detected, confidence = await asyncio.to_thread(
                    _media_processor.detect_watermark, post["media_local_path"]
                )
                if detected and confidence >= 0.25:
                    has_original_clean = False
                    logger.info(
                        f"Post #{post_id}: source photo blocked by watermark detector "
                        f"(confidence={confidence:.2f})"
                    )

            if has_original_clean:
                # Original photo from source — allowed only when USE_SOURCE_MEDIA=true and clean.
                logger.info(f"Post #{post_id}: using original source photo (no watermark)")
            else:
                # No usable source photo. Mix strategy:
                #   • photo-friendly topics (weather/nature) → real stock photo
                #   • everything local (city/court/власть/ДТП…) → branded headline card,
                #     because stock has no Izhevsk and returns random foreign cities.
                cat_label, cat_color, cat_mode, cat_kw = _detect_news_category(
                    original_text + " " + (rewritten or "")
                )
                if cat_mode == "stock":
                    # Use the category's fixed neutral keywords (close-up objects, no
                    # cityscape) when defined; otherwise fall back to AI keywords.
                    keywords = cat_kw or _ai_photo_keywords or _rewriter._extract_keywords_fallback(original_text)
                    if keywords and len(keywords) >= 1:
                        stock_photos = await _media_processor.search_stock_photo(keywords, count=12)
                        if stock_photos:
                            # Shuffle so we don't always publish the same top result
                            # (the fixed keywords otherwise returned one identical photo).
                            random.shuffle(stock_photos)
                            async with _used_stock_urls_lock:
                                chosen = None
                                for candidate in stock_photos:
                                    url = candidate.get("url", "")
                                    if url and url not in _used_stock_urls:
                                        chosen = candidate
                                        _used_stock_urls.add(url)
                                        if len(_used_stock_urls) > 500:
                                            _used_stock_urls.pop()
                                        break
                                if not chosen:
                                    chosen = stock_photos[0]
                            stock_url = chosen["url"]
                            logger.info(
                                f"Post #{post_id}: stock photo ({cat_label}) from "
                                f"{chosen.get('source', '?')}, keywords={keywords[:3]}"
                            )
                        else:
                            logger.info(f"Post #{post_id}: no stock photos found — text-only")
                    else:
                        logger.info(f"Post #{post_id}: insufficient keywords for stock — text-only")
                else:
                    # Branded headline card — always on-topic, never a foreign city
                    try:
                        from src.card_maker import make_news_card
                        card_path = os.path.join(_config.media_dir, f"card_{post_id}.jpg")
                        await asyncio.to_thread(
                            make_news_card, rewritten or original_text, cat_label, cat_color, card_path
                        )
                        await _db.set_local_media_override(post_id, card_path)
                        logger.info(f"Post #{post_id}: branded card ({cat_label}) — no local photo source")
                    except Exception as ce:
                        logger.error(f"Post #{post_id}: card generation failed: {ce}")

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

            # Prime-time window: only publish during active hours (Izhevsk UTC+4).
            # Outside the window the queue is held, not dropped.
            izh_now = dt.now(timezone(timedelta(hours=4)))
            start_h = _config.publish_active_start
            end_h = _config.publish_active_end
            if not (start_h <= izh_now.hour < end_h):
                continue

            # Daily cap: avoid the "firehose" that kills VK/TG reach.
            day_key = izh_now.strftime("%Y-%m-%d")
            max_per_day = _config.publish_max_per_day
            if max_per_day > 0:
                published_today = await _db.get_daily_counter("autopublish", day_key)
                if published_today >= max_per_day:
                    continue

            approved = await _db.get_approved_posts()
            if not approved:
                continue

            # Publish only ONE post per interval
            post = approved[0]
            success = await _publish_post(post)

            if success:
                last_published_at = time.monotonic()
                if max_per_day > 0:
                    await _db.bump_daily_counter("autopublish", day_key)
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


async def setup_vk_community():
    """One-time community setup: status line, discussion topics, pinned welcome.
    Idempotent — guarded by DB flags so it doesn't repeat on every restart."""
    if not (_vk_publisher and _vk_publisher.enabled and _db):
        return

    # Discussion topics (once)
    try:
        if not await _db.get_setting("vk_topics_created"):
            existing = await _vk_publisher.get_board_topics()
            if len(existing) < 2:
                for title in (
                    "📨 Прислать новость или фото",
                    "❓ Вопросы и ответы",
                    "🛒 Барахолка Ижевска",
                ):
                    await _vk_publisher.add_board_topic(
                        title, f"{title}. Пишите здесь — мы читаем 👇"
                    )
                    await asyncio.sleep(1.0)
            await _db.set_setting("vk_topics_created", "1")
    except Exception as e:
        logger.warning(f"VK topics setup failed: {e}")

    # Pinned welcome post (once)
    try:
        if not await _db.get_setting("vk_welcome_pinned"):
            welcome = (
                "📰 Добро пожаловать в «Ижевск Сегодня»!\n\n"
                "Главные новости Ижевска и Удмуртии каждый день: происшествия, "
                "транспорт, ЖКХ, погода и всё важное для города — коротко и по делу.\n\n"
                "📨 Есть новость или фото? Пишите в сообщения сообщества — опубликуем.\n"
                "🔔 Подписывайтесь, чтобы быть в курсе!"
            )
            pid = await _vk_publisher.publish(welcome, seo_enabled=False)
            if pid:
                await _vk_publisher.pin_post(pid)
                await _db.set_setting("vk_welcome_pinned", "1")
                logger.info("✅ VK welcome post created and pinned")
    except Exception as e:
        logger.warning(f"VK welcome pin failed: {e}")


async def media_cleanup_loop():
    """Delete downloaded media (mp4/cards/stock) older than 3 days so the volume
    doesn't fill up. Alert templates live in assets/ and are never touched."""
    import glob
    import time as _t
    while True:
        try:
            await asyncio.sleep(6 * 3600)  # every 6h
            if not _config:
                continue
            cutoff = _t.time() - 3 * 86400
            removed = 0
            for f in glob.glob(os.path.join(_config.media_dir, "*")):
                try:
                    if os.path.isfile(f) and os.path.getmtime(f) < cutoff:
                        os.remove(f)
                        removed += 1
                except Exception:
                    pass
            if removed:
                logger.info(f"Media cleanup: removed {removed} old files")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Media cleanup error: {e}")
            await asyncio.sleep(600)


async def weekly_report_loop():
    """Send admins a weekly summary (Mon 10:00 Izhevsk): subscribers + post stats."""
    while True:
        try:
            await asyncio.sleep(1800)  # check every 30 min
            if not (_config and _db):
                continue
            izh = dt.now(timezone(timedelta(hours=4)))
            if izh.weekday() != 0 or izh.hour != 10:
                continue
            week_key = izh.strftime("%Y-W%W")
            if await _db.get_setting("weekly_report_sent") == week_key:
                continue

            stats = await _db.get_weekly_stats()
            members = None
            if _vk_publisher and _vk_publisher.enabled:
                try:
                    members = await _vk_publisher.get_members_count()
                except Exception:
                    pass
            lines = [
                "📊 <b>Недельный отчёт «Ижевск Сегодня»</b>",
                f"👥 Подписчиков ВК: <b>{members if members is not None else '?'}</b>",
                f"📝 Постов за неделю: <b>{stats.get('total', 0)}</b>",
                f"✅ Опубликовано: {stats.get('published', 0)} · ❌ отклонено: {stats.get('rejected', 0)}",
            ]
            by_src = stats.get("by_source", {})
            if by_src:
                top = sorted(by_src.items(), key=lambda x: -x[1])[:5]
                lines.append("📡 Источники: " + ", ".join(f"{s}×{c}" for s, c in top))
            await _alert_admins("\n".join(lines))
            await _db.set_setting("weekly_report_sent", week_key)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Weekly report error: {e}")
            await asyncio.sleep(600)


async def youtube_clips_loop():
    """Post a few local Izhevsk YouTube Shorts to VK Clips per day, at morning/noon/
    evening slots within the prime-time window. Vertical + short + non-sensitive only.
    """
    CHECK_EVERY = 600  # check every 10 min
    fetcher = None
    while True:
        try:
            await asyncio.sleep(CHECK_EVERY)
            if not (_config and _db and _vk_publisher and _vk_publisher.enabled):
                continue
            if not getattr(_config, "yt_clips_enabled", False) or not _config.yt_clips_channels:
                continue
            if not _vk_publisher.has_explicit_user_token:
                continue

            izh_now = dt.now(timezone(timedelta(hours=4)))
            slots = sorted(_config.yt_clips_slots)
            cap = min(_config.yt_clips_per_day, len(slots))
            day_key = izh_now.strftime("%Y-%m-%d")
            done = await _db.get_daily_counter("ytclips", day_key)
            if done >= cap:
                continue
            # Time for the next slot? (slot hours = morning/noon/evening)
            if izh_now.hour < slots[done]:
                continue

            if fetcher is None:
                from src.youtube_clips import YouTubeClips
                seen_path = os.path.join(_config.media_dir, "..", "data", "yt_clips_seen.json")
                fetcher = YouTubeClips(_config.yt_clips_channels, seen_path)

            import tempfile
            import shutil
            tmpd = tempfile.mkdtemp(prefix="ytclip_")
            try:
                clip = await fetcher.fetch_one(tmpd)
                if not clip:
                    logger.info("YT clips: no fresh suitable short found this slot")
                    continue
                _ch = clip.get("channel") or ""
                _credit = f"Видео: YouTube · {_ch}" if _ch else "Видео: YouTube"
                caption = f"{clip['title']}\n\n{_credit}"
                link = f"https://vk.com/public{_config.vk_group_id}" if _config.vk_group_id else ""
                vid_id = await _vk_publisher.upload_clip(clip["path"], caption=caption[:300], link_url=link)
                if vid_id:
                    await _db.bump_daily_counter("ytclips", day_key)
                    logger.info(
                        f"YT clip → VK Clips OK: {clip['id']} ({clip['channel']}) "
                        f"slot {done + 1}/{cap}"
                    )
                else:
                    logger.warning(f"YT clip upload to VK failed for {clip['id']}")
            finally:
                shutil.rmtree(tmpd, ignore_errors=True)

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"YT clips loop error: {e}")
            await asyncio.sleep(60)


# VK error codes that signal a ban/throttle (vs benign "comments closed on that post")
_VK_BAN_CODES = {5, 8, 9, 29, 214}


async def _alert_admins(message: str):
    """Send a Telegram alert to all admins (best-effort)."""
    if not (_bot and _config):
        return
    for admin_id in _config.admin_ids:
        try:
            await _bot.send_message(admin_id, message, parse_mode=ParseMode.HTML)
        except Exception:
            pass


async def vk_outreach_loop():
    """Conservative organic-growth loop: post a few unique, on-topic comments per day
    in target VK communities. AI writes each comment (no templates, no links).
    Auto-pauses 24h and alerts admins on any ban/throttle code; auto-disables after
    two blocks in a row so a banned account can't keep hammering VK.
    """
    CHECK_EVERY = 1800  # check every 30 min
    consecutive_blocks = 0
    paused_until = 0.0
    disabled = False

    # Restore the persisted "already commented" set so we don't double-comment
    skip_keys: set[str] = set()
    try:
        if _db:
            raw = await _db.get_setting("outreach_skip", "")
            if raw:
                skip_keys = set(json.loads(raw))
    except Exception:
        pass

    while True:
        try:
            await asyncio.sleep(CHECK_EVERY)
            if disabled or not (_config and _db and _vk_publisher and _rewriter):
                continue
            if not _config.vk_competitor_commenting_enabled:
                continue
            if not _vk_publisher.has_explicit_user_token or not _config.vk_competitor_targets:
                continue

            import time as _t
            now_ts = _t.time()
            if now_ts < paused_until:
                continue

            izh_now = dt.now(timezone(timedelta(hours=4)))
            if not (9 <= izh_now.hour < 22):   # daytime only — night comments look bot-like
                continue

            day_key = izh_now.strftime("%Y-%m-%d")
            if await _db.get_daily_counter("outreach", day_key) >= _config.vk_competitor_comments_per_day:
                continue

            last_ts = float(await _db.get_setting("outreach_last_ts", "0") or 0)
            if now_ts - last_ts < _config.vk_competitor_min_gap_minutes * 60:
                continue

            candidate = await _vk_publisher.find_external_post_candidate(
                _config.vk_competitor_targets,
                keywords=_config.vk_competitor_keywords,
                scan_limit=_config.vk_competitor_scan_limit,
                skip_post_keys=skip_keys,
            )

            scan_code = _vk_publisher._last_error_code
            if scan_code in _VK_BAN_CODES:
                consecutive_blocks += 1
                paused_until = now_ts + 24 * 3600
                await _alert_admins(
                    f"⚠️ <b>VK аутрич: блок при сканировании</b> (code={scan_code}).\n"
                    f"Пауза 24 ч. Проверь аккаунт/токен."
                )
                if consecutive_blocks >= 2:
                    disabled = True
                    await _alert_admins("🛑 VK аутрич <b>авто-отключён</b> после 2 блоков подряд. Включи вручную после проверки.")
                continue

            if not candidate:
                continue

            comment = await _rewriter.generate_outreach_comment(candidate["text"])
            if not comment:
                continue

            cid = await _vk_publisher.create_comment(
                candidate["post_id"], comment, owner_id=candidate["owner_id"]
            )
            if cid:
                consecutive_blocks = 0
                skip_keys.add(candidate["post_key"])
                done = await _db.bump_daily_counter("outreach", day_key)
                await _db.set_setting("outreach_last_ts", str(now_ts))
                await _db.set_setting(
                    "outreach_skip", json.dumps(list(skip_keys)[-300:], ensure_ascii=False)
                )
                logger.info(
                    f"VK outreach: commented under {candidate['post_key']} "
                    f"({done}/{_config.vk_competitor_comments_per_day} today)"
                )
            else:
                code = _vk_publisher._last_error_code
                if code in _VK_BAN_CODES:
                    consecutive_blocks += 1
                    paused_until = now_ts + 24 * 3600
                    await _alert_admins(
                        f"⚠️ <b>VK аутрич: блокировка</b> (code={code}) при комментировании.\n"
                        f"Пауза 24 ч. Возможен бан — проверь аккаунт."
                    )
                    if consecutive_blocks >= 2:
                        disabled = True
                        await _alert_admins("🛑 VK аутрич <b>авто-отключён</b> после 2 блоков подряд.")
                else:
                    # Benign (comments closed/denied on that post) — skip it and move on
                    skip_keys.add(candidate["post_key"])

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"VK outreach loop error: {e}")
            await asyncio.sleep(60)


# ── Bot Initialization ──────────────────────────────────────────────────

def create_bot(config: Config, db: Database, rewriter: AIRewriter, media_proc: MediaProcessor, vk_pub: Optional[VKPublisher] = None) -> tuple:
    """Create and configure the bot. Returns (bot, dispatcher)."""
    global _config, _db, _rewriter, _media_processor, _vk_publisher, _story_generator, _bot

    _config = config
    _db = db
    _rewriter = rewriter
    _media_processor = media_proc
    _vk_publisher = vk_pub
    _story_generator = StoryGenerator()

    # Use TELEGRAM_PROXY if set, fallback to PEXELS_PROXY (same box)
    _proxy_url = (
        os.getenv("TELEGRAM_PROXY", "").strip()
        or os.getenv("PEXELS_PROXY", "").strip()
    )
    if _proxy_url:
        from aiogram.client.session.aiohttp import AiohttpSession
        session = AiohttpSession(proxy=_proxy_url)
        bot = Bot(token=config.bot_token, session=session)
        logger.info(f"Aiogram: using proxy {_proxy_url[:40]}...")
    else:
        bot = Bot(token=config.bot_token)
    _bot = bot

    dp = Dispatcher()
    dp.include_router(router)

    return bot, dp
