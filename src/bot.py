"""
Telegram Bot — Aiogram 3 bot for admin moderation, post management, and publishing.
"""

import asyncio
import logging
import os
from typing import Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    FSInputFile,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.enums import ParseMode

from src.config import Config
from src.database import Database
from src.ai_rewriter import AIRewriter
from src.media_processor import MediaProcessor

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
_bot: Optional[Bot] = None
_content_scheduler = None  # Set by main.py after init
_vk_publisher = None  # Set by main.py after init
_max_publisher = None  # Set by main.py after init


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
        await message.answer(
            "📰 <b>Ижевск Сегодня</b>\n\n"
            "Привет! Я бот новостного канала @IzhevskTodayNews.\n\n"
            "📩 Хочешь прислать новость? Нажми /news\n"
            "📲 Подписаться: @IzhevskTodayNews",
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


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    """Cancel any active FSM state."""
    await state.clear()
    await message.answer("❌ Отменено.")


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

    import traceback
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
        import google.generativeai as genai
        genai.configure(api_key=_config.gemini_api_key)
        model = genai.GenerativeModel("gemini-2.0-flash")
        response = model.generate_content("Скажи одно слово: привет")
        if response and response.text:
            lines.append(f"📡 API Call: ✅ OK — '{response.text.strip()[:50]}'")
        else:
            lines.append(f"📡 API Call: ❌ Empty response")
            if hasattr(response, 'candidates'):
                lines.append(f"   Candidates: {response.candidates}")
    except Exception as e:
        lines.append(f"📡 API Call: ❌ ERROR")
        lines.append(f"   {type(e).__name__}: {str(e)[:200]}")
        lines.append(f"   Traceback: {traceback.format_exc()[-300:]}")

    await message.answer("\n".join(lines))


@router.message(Command("testai"))
async def cmd_test_ai(message: Message):
    """Test ALL AI engines — admin only diagnostic."""
    if not is_admin(message.from_user.id):
        return

    import traceback
    import aiohttp
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


@router.message(Command("publish"))
async def cmd_publish(message: Message):
    """Publish all approved posts."""
    if not is_admin(message.from_user.id):
        return

    approved = await _db.get_approved_posts()
    if not approved:
        await message.answer("📭 Нет одобренных постов для публикации.")
        return

    published_count = 0
    for post in approved:
        success = await _publish_post(post)
        if success:
            published_count += 1
        await asyncio.sleep(1)  # Rate limit

    await message.answer(f"📢 Опубликовано постов: {published_count}/{len(approved)}")


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
    """Settings button handler — interactive with change buttons."""
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Нет доступа", show_alert=True)
        return

    await callback.answer()
    await _send_settings_menu(callback.message.chat.id)


async def _send_settings_menu(chat_id: int):
    """Send the interactive settings menu."""
    pub_min = _config.publish_interval // 60
    text = (
        f"⚙️ <b>Настройки бота</b>\n\n"
        f"📡 Источников: <b>{len(_config.source_channels)}</b>\n"
        f"📢 Канал: <b>@{_config.target_channel}</b>\n\n"
        f"📤 Интервал публикации: <b>{pub_min} мин</b>\n"
        f"⏱ Интервал проверки: <b>{_config.check_interval} сек</b>\n"
        f"📏 Мин. длина текста: <b>{_config.min_text_length} символов</b>\n\n"
        f"🚫 Фильтры: реклама ({len(_config.ad_stop_words)}), "
        f"политика ({len(_config.politics_stop_words)}), "
        f"мусор ({len(_config.lowvalue_stop_words)}), "
        f"срочные ({len(_config.breaking_keywords)})\n\n"
        f"👇 Нажми кнопку, чтобы изменить:"
    )

    # Mark current values with ✓
    def _pub_label(minutes):
        marker = " ✓" if _config.publish_interval == minutes * 60 else ""
        return f"{minutes} мин{marker}"

    def _chk_label(seconds):
        marker = " ✓" if _config.check_interval == seconds else ""
        return f"{seconds} сек{marker}"

    def _len_label(chars):
        marker = " ✓" if _config.min_text_length == chars else ""
        return f"{chars}{marker}"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        # Publish interval row
        [InlineKeyboardButton(text="📤 Публикация:", callback_data="noop")],
        [
            InlineKeyboardButton(text=_pub_label(30), callback_data="set_pub:1800"),
            InlineKeyboardButton(text=_pub_label(60), callback_data="set_pub:3600"),
            InlineKeyboardButton(text=_pub_label(120), callback_data="set_pub:7200"),
            InlineKeyboardButton(text=_pub_label(180), callback_data="set_pub:10800"),
        ],
        # Check interval row
        [InlineKeyboardButton(text="⏱ Проверка каналов:", callback_data="noop")],
        [
            InlineKeyboardButton(text=_chk_label(30), callback_data="set_chk:30"),
            InlineKeyboardButton(text=_chk_label(60), callback_data="set_chk:60"),
            InlineKeyboardButton(text=_chk_label(120), callback_data="set_chk:120"),
            InlineKeyboardButton(text=_chk_label(300), callback_data="set_chk:300"),
        ],
        # Min text length row
        [InlineKeyboardButton(text="📏 Мин. длина текста:", callback_data="noop")],
        [
            InlineKeyboardButton(text=_len_label(50), callback_data="set_len:50"),
            InlineKeyboardButton(text=_len_label(100), callback_data="set_len:100"),
            InlineKeyboardButton(text=_len_label(200), callback_data="set_len:200"),
            InlineKeyboardButton(text=_len_label(300), callback_data="set_len:300"),
        ],
    ])
    await _bot.send_message(chat_id, text, reply_markup=kb, parse_mode=ParseMode.HTML)


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


@router.callback_query(F.data == "noop")
async def cb_noop(callback: CallbackQuery):
    """No-op handler for label-only buttons."""
    await callback.answer()


@router.callback_query(F.data.startswith("set_pub:"))
async def cb_set_publish_interval(callback: CallbackQuery):
    """Change publish interval."""
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Нет доступа", show_alert=True)
        return
    seconds = int(callback.data.split(":")[1])
    _config.publish_interval = seconds
    await _db.set_setting("publish_interval", str(seconds))
    await callback.answer(f"✅ Интервал публикации: {seconds // 60} мин")
    # Refresh settings menu
    try:
        await callback.message.delete()
    except Exception:
        pass
    await _send_settings_menu(callback.message.chat.id)


@router.callback_query(F.data.startswith("set_chk:"))
async def cb_set_check_interval(callback: CallbackQuery):
    """Change channel check interval."""
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Нет доступа", show_alert=True)
        return
    seconds = int(callback.data.split(":")[1])
    _config.check_interval = seconds
    await _db.set_setting("check_interval", str(seconds))
    await callback.answer(f"✅ Интервал проверки: {seconds} сек")
    try:
        await callback.message.delete()
    except Exception:
        pass
    await _send_settings_menu(callback.message.chat.id)


@router.callback_query(F.data.startswith("set_len:"))
async def cb_set_min_length(callback: CallbackQuery):
    """Change minimum text length."""
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Нет доступа", show_alert=True)
        return
    chars = int(callback.data.split(":")[1])
    _config.min_text_length = chars
    await _db.set_setting("min_text_length", str(chars))
    await callback.answer(f"✅ Мин. длина: {chars} символов")
    try:
        await callback.message.delete()
    except Exception:
        pass
    await _send_settings_menu(callback.message.chat.id)


# ── Helper Functions ─────────────────────────────────────────────────────

def _escape_html(text: str) -> str:
    """Escape HTML special characters in text."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _format_post(text: str, hashtags: list) -> str:
    """Format post with premium Telegram template and convert markdown to HTML."""
    import re
    
    # Convert **bold** markdown to <b>bold</b> HTML
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    # Remove any leftover markdown headers (# ## ###)
    text = re.sub(r'^#{1,3}\s*', '', text, flags=re.MULTILINE)
    
    lines = text.strip().split("\n")
    if not lines:
        return text

    # Build formatted post
    parts = []

    # Title — Gemini already provides emoji + title, keep as-is
    parts.append(lines[0].strip())
    parts.append("")

    # Body text
    body = "\n".join(lines[1:]).strip()
    if body:
        parts.append(body)
        parts.append("")

    # Hashtags
    if hashtags:
        parts.append(" ".join(hashtags))
        parts.append("")

    # Premium CTA footer (plain text — renders well in both TG and VK)
    parts.append("📲 @IzhevskTodayNews | 📩 @IzhevskTodayBot")

    return "\n".join(parts)


def _clean_text(text: str) -> str:
    """Remove source attribution lines, subscribe links, and external URLs from post text."""
    import re
    lines = text.split('\n')
    cleaned = []
    skip_patterns = [
        r'подписаться на',
        r'подписывайтесь',
        r'подписаться\s*[|:]',
        r'подписаться$',
        r'повестка дня.*на сайте',
        r'читайте.*на сайте',
        r'читайте нас',
        r'читайте.*в\s*(max|макс|vk|вк|дзен)',
        r'источник:',
        r'подробнее.*на сайте',
        r'на нашем сайте',
        r'наш.*канал',
        r'присоединяйтесь',
        r'подробности.*по ссылке',
        r'ранее.*писал[аи]?',
        r'прислать новость',
        r'поделиться новостью',
        r'купить рекламу',
        r'пригласить друзей',
        r'реклама[.:]',
        r'^\s*https?://',  # standalone URLs
        r'^\s*t\.me/',
        r'^\s*@\w+\s*$',  # standalone @mentions
        r'^\s*[📲😊📩📢🔔💬]\s*(подписа|присла|читай|наш)',  # emoji CTA lines
    ]
    for line in lines:
        line_lower = line.strip().lower()
        if not line_lower:
            cleaned.append(line)
            continue
        skip = False
        for pattern in skip_patterns:
            if re.search(pattern, line_lower):
                skip = True
                break
        if not skip:
            cleaned.append(line)
    # Remove trailing empty lines
    result = '\n'.join(cleaned).rstrip()
    return result


async def _send_review_post(chat_id: int, post: dict):
    """Send a post for admin review with moderation buttons."""
    original = _escape_html(_truncate(post["original_text"], 300))
    rewritten = post.get("rewritten_text") or "⏳ Ещё не переписан"
    # Preserve <b> tags in rewritten text for proper rendering
    rewritten_safe = _truncate(rewritten, 500)
    # Escape & < > but then restore <b> and </b> tags
    rewritten_display = _escape_html(rewritten_safe)
    rewritten_display = rewritten_display.replace("&lt;b&gt;", "<b>").replace("&lt;/b&gt;", "</b>")

    status = _status_emoji(post["status"])
    source = post["source_channel"]

    # Format date as d.m.Y H:M
    from datetime import datetime as dt
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
    """Publish a post to the target channel."""
    text = post.get("rewritten_text") or post["original_text"]
    target = _config.target_channel

    if not target.startswith("@") and not target.startswith("-"):
        target = f"@{target}"

    try:
        media_path = post.get("media_local_path")
        media_url = post.get("media_file_id")  # Remote URL fallback
        replacement_url = post.get("replacement_media_url")

        # Use replacement photo if available
        if replacement_url:
            msg = await _bot.send_photo(
                target,
                photo=replacement_url,
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
                try:
                    msg = await _bot.send_photo(
                        target,
                        photo=photo_source,
                        caption=text[:1024],
                        parse_mode=ParseMode.HTML,
                    )
                except Exception as photo_err:
                    logger.warning(f"Photo send failed ({photo_err}), publishing as text")
                    photo_source = None

            if not photo_source:
                # Fallback: publish as text if photo unavailable
                msg = await _bot.send_message(
                    target,
                    text[:4096],
                    parse_mode=ParseMode.HTML,
                )
        else:
            msg = await _bot.send_message(
                target,
                text[:4096],
                parse_mode=ParseMode.HTML,
            )

        # Record publication
        await _db.update_post_status(post["id"], "published")
        await _db.add_published(post["id"], msg.message_id)

        logger.info(f"Published post #{post['id']} to {target}")

        # VK crosspost
        if _vk_publisher and _vk_publisher.enabled:
            try:
                vk_photo = replacement_url or media_url or None
                await _vk_publisher.publish(text, photo_url=vk_photo)
            except Exception as vk_err:
                logger.warning(f"VK crosspost failed for post #{post['id']}: {vk_err}")

        # MAX crosspost
        if _max_publisher and _max_publisher.enabled:
            try:
                max_photo = replacement_url or media_url or None
                await _max_publisher.publish(text, photo_url=max_photo)
            except Exception as max_err:
                logger.warning(f"MAX crosspost failed for post #{post['id']}: {max_err}")

        return True

    except Exception as e:
        logger.error(f"Failed to publish post #{post['id']}: {e}")
        return False


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
    ad_matches = [w for w in _config.ad_stop_words if w in text_lower]
    if len(ad_matches) >= 2:  # 2+ ad stop-words = spam
        await _db.update_post_status(post_id, "rejected")
        logger.info(f"Post #{post_id} rejected: ad/spam (matched: {', '.join(ad_matches[:3])})")
        return

    # Step 0a2: Politics filter — skip political posts
    politics_matches = [w for w in _config.politics_stop_words if w in text_lower]
    if len(politics_matches) >= 2:  # 2+ political keywords = politics
        await _db.update_post_status(post_id, "rejected")
        logger.info(f"Post #{post_id} rejected: politics (matched: {', '.join(politics_matches[:3])})")
        return

    # Step 0a3: Low-value content filter — skip weather, horoscopes, etc.
    lowvalue_matches = [w for w in _config.lowvalue_stop_words if w in text_lower]
    if len(lowvalue_matches) >= 1:  # Even 1 match = low-value (these are very specific)
        await _db.update_post_status(post_id, "rejected")
        logger.info(f"Post #{post_id} rejected: low-value content (matched: {', '.join(lowvalue_matches[:3])})")
        return

    # Step 0b: Relevance filter for federal channels
    source = post["source_channel"].lower()
    local_keywords = ["izhevsk", "izh", "udm", "удмурт", "ижевск", "18"]
    is_local = any(kw in source for kw in local_keywords)

    if not is_local:
        is_relevant = await _rewriter.check_relevance(original_text)
        if not is_relevant:
            await _db.update_post_status(post_id, "rejected")
            logger.info(f"Post #{post_id} rejected: regional news from federal channel @{source}")
            return

    # Step 0c: Deduplication — skip if SAME news already exists from another channel
    # Only reject TRUE duplicates, not just news on similar topics
    recent_texts = await _db.get_recent_texts(hours=24)
    for existing_text in recent_texts:
        if existing_text == original_text:
            continue
        # Method 1: Text similarity (catches same text from different sources)
        similarity = 1.0 - _rewriter.calculate_uniqueness(original_text, existing_text)
        if similarity > 0.65:  # High threshold: only near-identical texts
            await _db.update_post_status(post_id, "rejected")
            logger.info(f"Post #{post_id} rejected: duplicate text (similarity {similarity:.0%})")
            return
        # Method 2: Keyword overlap (catches same story rephrased)
        import re as _re
        # Use longer words (6+) to avoid false positives from common regional words
        words1 = set(w for w in _re.findall(r'[а-яёa-z0-9]+', original_text.lower()) if len(w) > 5)
        words2 = set(w for w in _re.findall(r'[а-яёa-z0-9]+', existing_text.lower()) if len(w) > 5)
        if words1 and words2 and len(words1) >= 5 and len(words2) >= 5:
            overlap = len(words1 & words2) / min(len(words1), len(words2))
            if overlap > 0.75:  # High threshold: only near-identical topics
                await _db.update_post_status(post_id, "rejected")
                logger.info(f"Post #{post_id} rejected: duplicate topic (keyword overlap {overlap:.0%})")
                return

    # Step 0d: Breaking news detection — auto-publish without moderation
    is_breaking = any(kw in text_lower for kw in _config.breaking_keywords)

    # Step 1: AI Rewrite
    await _db.update_post_status(post_id, "rewriting")
    rewritten, engine = await _rewriter.rewrite(original_text)

    if rewritten:
        rewritten = _clean_text(rewritten)  # Clean AI output too
        uniqueness = _rewriter.calculate_uniqueness(original_text, rewritten)
        logger.info(f"Post #{post_id} rewritten by {engine} (uniqueness: {uniqueness:.0%})")
    else:
        rewritten = original_text
        logger.warning(f"Post #{post_id}: AI rewrite failed, using original text")

    # Step 2: Generate hashtags
    hashtags = await _rewriter.generate_hashtags(rewritten)

    # Step 2.5: Format post with premium template
    rewritten = _format_post(rewritten, hashtags)

    await _db.update_post_rewrite(post_id, rewritten)

    # Step 3: Find unique stock photo (EVERY post must have a photo)
    stock_url = None
    try:
        # Method 1: AI-generated keywords
        keywords = await _rewriter.generate_keywords(original_text)
        if keywords:
            stock_photos = await _media_processor.search_stock_photo(keywords)
            if stock_photos:
                stock_url = stock_photos[0]["url"]
                logger.info(f"Post #{post_id}: stock photo found (AI keywords: {' '.join(keywords)})")
    except Exception as e:
        logger.warning(f"Post #{post_id}: AI keyword generation failed: {e}")

    # Method 2: Simple keyword extraction (fallback if AI unavailable)
    if not stock_url:
        try:
            import re as _re
            # Extract long meaningful words from text
            words = _re.findall(r'[а-яёА-ЯЁ]{6,}', original_text)
            # Pick top unique words (skip common ones)
            common = {'который', 'которая', 'которые', 'однако', 'несколько', 'сообщил', 'сообщила',
                      'сообщили', 'рассказал', 'рассказала', 'отметил', 'отметила', 'заявил',
                      'является', 'составил', 'составила', 'сделать', 'поэтому', 'например',
                      'очередной', 'основных', 'обратить', 'сегодня', 'которое', 'связано',
                      'получить', 'возможно', 'подробнее', 'источник', 'подписать', 'читайте'}
            unique_words = []
            seen = set()
            for w in words:
                wl = w.lower()
                if wl not in common and wl not in seen:
                    seen.add(wl)
                    unique_words.append(wl)
                    if len(unique_words) >= 3:
                        break
            if unique_words:
                stock_photos = await _media_processor.search_stock_photo(unique_words)
                if stock_photos:
                    stock_url = stock_photos[0]["url"]
                    logger.info(f"Post #{post_id}: stock photo found (text keywords: {' '.join(unique_words)})")
        except Exception as e2:
            logger.warning(f"Post #{post_id}: fallback keyword extraction failed: {e2}")

    # Method 3: Generic topic photo (last resort)
    if not stock_url:
        try:
            generic_queries = ["city news", "Izhevsk Russia", "urban life", "newspaper"]
            for query in generic_queries:
                stock_photos = await _media_processor.search_stock_photo([query])
                if stock_photos:
                    stock_url = stock_photos[0]["url"]
                    logger.info(f"Post #{post_id}: generic stock photo found ({query})")
                    break
        except Exception as e3:
            logger.warning(f"Post #{post_id}: generic photo search failed: {e3}")

    if stock_url:
        await _db.update_post_media(post_id, replacement_url=stock_url)

    # Step 3b: Watermark detection on original photo
    if post["media_type"] == "photo" and post.get("media_local_path"):
        has_watermark, confidence = _media_processor.detect_watermark(post["media_local_path"])
        if has_watermark:
            await _db.update_post_media(post_id, has_watermark=True)
            logger.info(f"Post #{post_id}: watermark detected (confidence: {confidence:.2f})")

    # Step 4: Breaking news → auto-publish, regular → send for review
    if is_breaking:
        logger.info(f"⚡ Post #{post_id} is BREAKING NEWS — auto-publishing!")
        updated_post = await _db.get_post(post_id)
        await _db.update_post_status(post_id, "approved")
        success = await _publish_post(updated_post)
        if success:
            # Notify admins about auto-published breaking news
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
    else:
        # Regular post — send for moderation
        for admin_id in _config.admin_ids:
            try:
                updated_post = await _db.get_post(post_id)
                await _send_review_post(admin_id, updated_post)
            except Exception as e:
                logger.error(f"Failed to send review to admin {admin_id}: {e}")

        logger.info(f"Post #{post_id} sent for review to {len(_config.admin_ids)} admin(s)")


# ── Auto-Publish Scheduler ───────────────────────────────────────────────

# ── /test_content command ────────────────────────────────────────────────

@router.message(Command("test_content"))
async def cmd_test_content(message: Message):
    """Admin command: /test_content <rubric> — test a content rubric."""
    if not is_admin(message.from_user.id):
        return

    if not _content_scheduler:
        await message.reply("❌ Планировщик контента не инициализирован")
        return

    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        rubrics = [
            "weather", "holiday", "history_fact", "five_facts",
            "recipe", "lifehack", "place", "evening_fun", "daily_digest"
        ]
        await message.reply(
            "📝 <b>Тест рубрик</b>\n\n"
            "Использование: /test_content <рубрика>\n\n"
            "Доступные рубрики:\n" +
            "\n".join(f"• <code>{r}</code>" for r in rubrics),
            parse_mode=ParseMode.HTML,
        )
        return

    rubric = args[1].strip().lower()
    await message.reply(f"⏳ Генерация: {rubric}...")

    try:
        success = await _content_scheduler.force_publish(rubric)
        if success:
            await message.reply(f"✅ Рубрика '{rubric}' опубликована!")
        else:
            await message.reply(f"❌ Не удалось сгенерировать '{rubric}'")
    except Exception as e:
        await message.reply(f"❌ Ошибка: {e}")

async def auto_publish_loop():
    """Background task: publish ONE approved post per interval for even distribution."""
    # Load saved settings from DB on startup
    try:
        await _config.reload_from_db(_db)
        logger.info(f"Loaded settings from DB: publish_interval={_config.publish_interval}s")
    except Exception as e:
        logger.warning(f"Could not load settings from DB: {e}")

    while True:
        try:
            interval = _config.publish_interval if _config else 7200
            await asyncio.sleep(interval)

            # Publish ONE post (oldest approved)
            post = await _db.get_oldest_approved_post()
            if not post:
                continue

            success = await _publish_post(post)
            if success:
                logger.info(
                    f"📢 Scheduled publish: post #{post['id']} from @{post['source_channel']}"
                )

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Auto-publish error: {e}")
            await asyncio.sleep(60)


# ── Bot Initialization ──────────────────────────────────────────────────

def create_bot(config: Config, db: Database, rewriter: AIRewriter, media_proc: MediaProcessor) -> tuple:
    """Create and configure the bot. Returns (bot, dispatcher)."""
    global _config, _db, _rewriter, _media_processor, _bot

    _config = config
    _db = db
    _rewriter = rewriter
    _media_processor = media_proc

    bot = Bot(token=config.bot_token)
    _bot = bot

    dp = Dispatcher()
    dp.include_router(router)

    return bot, dp
