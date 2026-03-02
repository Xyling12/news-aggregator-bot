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


# ── Globals (set during init) ────────────────────────────────────────────

_config: Optional[Config] = None
_db: Optional[Database] = None
_rewriter: Optional[AIRewriter] = None
_media_processor: Optional[MediaProcessor] = None
_bot: Optional[Bot] = None


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
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Доступ запрещён. Только для администраторов.")
        return

    await message.answer(
        "🤖 **News Aggregator Bot**\n\n"
        "Я мониторю каналы-источники, переписываю новости через AI "
        "и отправляю их тебе на модерацию.\n\n"
        "📌 Используй меню ниже для управления:",
        reply_markup=get_main_keyboard(),
        parse_mode=ParseMode.MARKDOWN,
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
        f"  • Политика: <b>{len(_config.politics_stop_words)} слов</b>\n"
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

        rewritten, engine = await _rewriter.rewrite(post["original_text"])
        if rewritten:
            await _db.update_post_rewrite(post_id, rewritten)
            uniqueness = _rewriter.calculate_uniqueness(post["original_text"], rewritten)

            updated_post = await _db.get_post(post_id)
            await callback.message.reply(f"✅ Перерайт завершён (движок: {engine}, уникальность: {uniqueness:.0%})")
            await _send_review_post(callback.message.chat.id, updated_post)
        else:
            await callback.message.reply("❌ Рерайт не удался. Попробуйте позже.")
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


# ── Helper Functions ─────────────────────────────────────────────────────

def _escape_html(text: str) -> str:
    """Escape HTML special characters in text."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


async def _send_review_post(chat_id: int, post: dict):
    """Send a post for admin review with moderation buttons."""
    original = _escape_html(_truncate(post["original_text"], 300))
    rewritten = post.get("rewritten_text") or "⏳ Ещё не переписан"
    rewritten_display = _escape_html(_truncate(rewritten, 500))

    status = _status_emoji(post["status"])
    source = post["source_channel"]

    text = (
        f"{status} <b>Пост #{post['id']}</b> | Источник: @{source}\n"
        f"📅 {post['created_at']}\n\n"
        f"📝 <b>Оригинал:</b>\n{original}\n\n"
        f"✍️ <b>Рерайт:</b>\n{rewritten_display}"
    )

    # Add media info
    if post["media_type"] != "none":
        text += f"\n\n🖼 Медиа: {post['media_type']}"
        if post.get("has_watermark"):
            text += " ⚠️ Обнаружен водяной знак!"

    # If post has media, send with media
    media_path = post.get("media_local_path")
    if media_path and os.path.exists(media_path) and post["media_type"] == "photo":
        try:
            photo = FSInputFile(media_path)
            await _bot.send_photo(
                chat_id,
                photo=photo,
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
        replacement_url = post.get("replacement_media_url")

        # Use replacement photo if available
        if replacement_url:
            msg = await _bot.send_photo(
                target,
                photo=replacement_url,
                caption=text[:1024],
                parse_mode=ParseMode.HTML,
            )
        elif media_path and os.path.exists(media_path) and post["media_type"] == "photo":
            photo = FSInputFile(media_path)
            msg = await _bot.send_photo(
                target,
                photo=photo,
                caption=text[:1024],
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
    original_text = post["original_text"]
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

    # Step 0c: Deduplication — skip if similar post already exists
    recent_texts = await _db.get_recent_texts(hours=24)
    for existing_text in recent_texts:
        if existing_text == original_text:
            continue
        similarity = 1.0 - _rewriter.calculate_uniqueness(original_text, existing_text)
        if similarity > 0.6:
            await _db.update_post_status(post_id, "rejected")
            logger.info(f"Post #{post_id} rejected: duplicate (similarity {similarity:.0%})")
            return

    # Step 0d: Breaking news detection — auto-publish without moderation
    is_breaking = any(kw in text_lower for kw in _config.breaking_keywords)

    # Step 1: AI Rewrite
    await _db.update_post_status(post_id, "rewriting")
    rewritten, engine = await _rewriter.rewrite(original_text)

    if rewritten:
        uniqueness = _rewriter.calculate_uniqueness(original_text, rewritten)
        logger.info(f"Post #{post_id} rewritten by {engine} (uniqueness: {uniqueness:.0%})")
    else:
        rewritten = original_text
        logger.warning(f"Post #{post_id}: AI rewrite failed, using original text")

    # Step 2: Generate hashtags
    hashtags = await _rewriter.generate_hashtags(rewritten)
    if hashtags:
        rewritten = rewritten.rstrip() + "\n\n" + " ".join(hashtags)
        logger.info(f"Post #{post_id} hashtags: {' '.join(hashtags)}")

    await _db.update_post_rewrite(post_id, rewritten)

    # Step 3: Media processing (watermark detection)
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

async def auto_publish_loop():
    """Background task: auto-publish approved posts on schedule."""
    while True:
        try:
            interval = _config.publish_interval if _config else 7200
            await asyncio.sleep(interval)

            approved = await _db.get_approved_posts()
            if not approved:
                continue

            published_count = 0
            for post in approved:
                success = await _publish_post(post)
                if success:
                    published_count += 1
                await asyncio.sleep(2)  # Space out posts

            if published_count > 0:
                logger.info(f"Auto-publisher: published {published_count} posts")
                for admin_id in _config.admin_ids:
                    try:
                        await _bot.send_message(
                            admin_id,
                            f"📢 Авто-публикация: опубликовано {published_count} постов.",
                        )
                    except Exception:
                        pass

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
