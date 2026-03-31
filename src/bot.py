"""
Telegram Bot вЂ” Aiogram 3 bot for admin moderation, post management, and publishing.
"""

import asyncio
import json
import logging
import os
import re
import traceback
from datetime import datetime as dt, timedelta
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
from src.story_generator import StoryGenerator
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
from src.content_filter import filter_sensitive_content, FilterAction

logger = logging.getLogger(__name__)

router = Router()


# в”Ђв”Ђ FSM States в”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђ

class EditPostStates(StatesGroup):
    waiting_for_text = State()

class AddSourceStates(StatesGroup):
    waiting_for_channel = State()

class SendNewsStates(StatesGroup):
    waiting_for_news = State()


# в”Ђв”Ђ Globals (set during init) в”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђ

_config: Optional[Config] = None
_db: Optional[Database] = None
_rewriter: Optional[AIRewriter] = None
_media_processor: Optional[MediaProcessor] = None
_vk_publisher: Optional[VKPublisher] = None
_story_generator: Optional[StoryGenerator] = None
_bot: Optional[Bot] = None

# Rate limiting: max 3 concurrent AI calls to avoid Gemini 429 errors
_ai_semaphore = asyncio.Semaphore(3)

LOCAL_GEO_KEYWORDS = [
    "СѓРґРјСѓСЂС‚",
    "РёР¶РµРІСЃРє",
    "РіР»Р°Р·РѕРІ",
    "СЃР°СЂР°РїСѓР»",
    "РІРѕС‚РєРёРЅСЃРє",
    "РјРѕР¶РіР°",
    "РєР°РјР±Р°СЂРє",
    "Р±Р°Р»РµР·РёРЅ",
    "Р·Р°РІСЊСЏР»РѕРІ",
    "СѓРґРјСѓСЂС‚СЃРє",
]

FEDERAL_NEWS_KEYWORDS = [
    "С„РµРґРµСЂР°Р»СЊРЅ",
    "РіРѕСЃРґСѓРј",
    "РіРѕСЃСѓРґР°СЂСЃС‚РІРµРЅРЅ",
    "РїСЂР°РІРёС‚РµР»СЊСЃС‚РІ",
    "РјРёРЅС„РёРЅ",
    "С†РµРЅС‚СЂРѕР±Р°РЅРє",
    "С†РµРЅС‚СЂР°Р»СЊРЅ Р±Р°РЅРє",
    "РєР»СЋС‡РµРІ",
    "РїРµРЅСЃРё",
    "РЅР°Р»РѕРі",
    "РїРѕСЃРѕР±Рё",
    "РјСЂРѕС‚",
    "Р¶РєС… С‚Р°СЂРёС„",
    "С‚Р°СЂРёС„",
    "РёРЅС„Р»СЏС†",
    "СЃС‚Р°РІРє",
]

RADAR_SOURCE_MARKERS = ["СЂР°РґР°СЂ", "radar", "Р±РїР»Р°", "РІРѕР·РґСѓС…", "С‚СЂРµРІРѕРі"]


NON_LOCAL_REGION_KEYWORDS = [
    # Explicit non-local cities/regions that should not pass as Izhevsk-only updates.
    "СЃРѕС‡Рё",
    "РєСЂР°СЃРЅРѕРґР°СЂ",
    "РєСЂР°СЃРЅРѕРґР°СЂСЃРє",
    "Р°РґР»РµСЂ",
    "РєСѓР±Р°РЅ",
    "Р°РЅР°Рї",
    "РіРµР»РµРЅРґР¶РёРє",
    "РЅРѕРІРѕСЂРѕСЃСЃРёР№СЃРє",
    "СЂРѕСЃС‚РѕРІ",
    "Р±РµР»РіРѕСЂРѕРґ",
    "РєСѓСЂСЃРє",
    "РІРѕСЂРѕРЅРµР¶",
    "Р±СЂСЏРЅСЃРє",
    "С‚РІРµСЂ",
    "РјРѕСЃРєРІР°",
    "СЃР°РЅРєС‚-РїРµС‚РµСЂР±СѓСЂРі",
    "РїРµС‚РµСЂР±СѓСЂРі",
    "СЃРїР±",
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
        "pending": "вЏі",
        "rewriting": "рџ”„",
        "review": "рџ‘Ђ",
        "approved": "вњ…",
        "rejected": "вќЊ",
        "published": "рџ“ў",
    }.get(status, "вќ“")


# в”Ђв”Ђ Moderation Keyboard в”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђ

def get_review_keyboard(post_id: int) -> InlineKeyboardMarkup:
    """Create inline keyboard for post moderation."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="вњ… РћРїСѓР±Р»РёРєРѕРІР°С‚СЊ", callback_data=f"approve:{post_id}"),
            InlineKeyboardButton(text="вќЊ РћС‚РєР»РѕРЅРёС‚СЊ", callback_data=f"reject:{post_id}"),
        ],
        [
            InlineKeyboardButton(text="вњЏпёЏ Р РµРґР°РєС‚РёСЂРѕРІР°С‚СЊ", callback_data=f"edit:{post_id}"),
            InlineKeyboardButton(text="рџ”„ РџРµСЂРµСЂР°Р№С‚", callback_data=f"rewrite:{post_id}"),
        ],
        [
            InlineKeyboardButton(text="рџ–ј РСЃРєР°С‚СЊ С„РѕС‚Рѕ", callback_data=f"search_photo:{post_id}"),
        ],
    ])


def get_main_keyboard() -> InlineKeyboardMarkup:
    """Create main menu keyboard."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="рџ“‹ РћС‡РµСЂРµРґСЊ", callback_data="queue"),
            InlineKeyboardButton(text="рџ“Љ РЎС‚Р°С‚РёСЃС‚РёРєР°", callback_data="stats"),
        ],
        [
            InlineKeyboardButton(text="рџ“Ў РСЃС‚РѕС‡РЅРёРєРё", callback_data="sources"),
            InlineKeyboardButton(text="вљ™пёЏ РќР°СЃС‚СЂРѕР№РєРё", callback_data="settings"),
        ],
    ])


# в”Ђв”Ђ Command Handlers в”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђ

@router.message(CommandStart())
async def cmd_start(message: Message):
    """Handle /start command."""
    if is_admin(message.from_user.id):
        await message.answer(
            "рџ¤– <b>РР¶РµРІСЃРє РЎРµРіРѕРґРЅСЏ вЂ” РђРґРјРёРЅ-РїР°РЅРµР»СЊ</b>\n\n"
            "РЇ РјРѕРЅРёС‚РѕСЂСЋ РєР°РЅР°Р»С‹-РёСЃС‚РѕС‡РЅРёРєРё, РїРµСЂРµРїРёСЃС‹РІР°СЋ РЅРѕРІРѕСЃС‚Рё С‡РµСЂРµР· AI "
            "Рё РѕС‚РїСЂР°РІР»СЏСЋ РёС… С‚РµР±Рµ РЅР° РјРѕРґРµСЂР°С†РёСЋ.\n\n"
            "рџ“Њ РСЃРїРѕР»СЊР·СѓР№ РјРµРЅСЋ РЅРёР¶Рµ РґР»СЏ СѓРїСЂР°РІР»РµРЅРёСЏ:",
            reply_markup=get_main_keyboard(),
            parse_mode=ParseMode.HTML,
        )
    else:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="рџ“© РџСЂРёСЃР»Р°С‚СЊ РЅРѕРІРѕСЃС‚СЊ", callback_data="send_news")],
            [InlineKeyboardButton(text="рџ“І РџРµСЂРµР№С‚Рё РЅР° РєР°РЅР°Р»", url="https://t.me/IzhevskTodayNews")],
        ])
        await message.answer(
            "рџ“° <b>РР¶РµРІСЃРє РЎРµРіРѕРґРЅСЏ</b>\n\n"
            "РџСЂРёРІРµС‚! РЇ Р±РѕС‚ РЅРѕРІРѕСЃС‚РЅРѕРіРѕ РєР°РЅР°Р»Р° @IzhevskTodayNews.\n\n"
            "Р—РЅР°РµС€СЊ Рѕ РІР°Р¶РЅРѕРј СЃРѕР±С‹С‚РёРё РІ РР¶РµРІСЃРєРµ? "
            "РќР°Р¶РјРё РєРЅРѕРїРєСѓ РЅРёР¶Рµ вЂ” Рё РјС‹ СЂР°СЃСЃРјРѕС‚СЂРёРј С‚РІРѕСЋ РЅРѕРІРѕСЃС‚СЊ РґР»СЏ РїСѓР±Р»РёРєР°С†РёРё рџ‘‡",
            reply_markup=kb,
            parse_mode=ParseMode.HTML,
        )


@router.message(Command("news"))
async def cmd_news(message: Message, state: FSMContext):
    """Start news submission from any user."""
    await state.set_state(SendNewsStates.waiting_for_news)
    await message.answer(
        "рџ“© <b>РџСЂРёСЃР»Р°С‚СЊ РЅРѕРІРѕСЃС‚СЊ</b>\n\n"
        "РћС‚РїСЂР°РІСЊ РјРЅРµ С‚РµРєСЃС‚ РёР»Рё С„РѕС‚Рѕ СЃ РѕРїРёСЃР°РЅРёРµРј РЅРѕРІРѕСЃС‚Рё.\n"
        "Р•СЃР»Рё РЅРѕРІРѕСЃС‚СЊ РёРЅС‚РµСЂРµСЃРЅР°СЏ вЂ” РјС‹ РѕРїСѓР±Р»РёРєСѓРµРј РµС‘ РЅР° РєР°РЅР°Р»Рµ!\n\n"
        "Р”Р»СЏ РѕС‚РјРµРЅС‹ РЅР°Р¶РјРё /cancel",
        parse_mode=ParseMode.HTML,
    )


@router.callback_query(F.data == "send_news")
async def cb_send_news(callback: CallbackQuery, state: FSMContext):
    """Handle 'РџСЂРёСЃР»Р°С‚СЊ РЅРѕРІРѕСЃС‚СЊ' button from /start menu."""
    await callback.answer()
    await state.set_state(SendNewsStates.waiting_for_news)
    await callback.message.answer(
        "рџ“© <b>РџСЂРёСЃР»Р°С‚СЊ РЅРѕРІРѕСЃС‚СЊ</b>\n\n"
        "РћС‚РїСЂР°РІСЊ РјРЅРµ С‚РµРєСЃС‚ РёР»Рё С„РѕС‚Рѕ СЃ РѕРїРёСЃР°РЅРёРµРј РЅРѕРІРѕСЃС‚Рё.\n"
        "Р•СЃР»Рё РЅРѕРІРѕСЃС‚СЊ РёРЅС‚РµСЂРµСЃРЅР°СЏ вЂ” РјС‹ РѕРїСѓР±Р»РёРєСѓРµРј РµС‘ РЅР° РєР°РЅР°Р»Рµ!\n\n"
        "Р”Р»СЏ РѕС‚РјРµРЅС‹ РЅР°Р¶РјРё /cancel",
        parse_mode=ParseMode.HTML,
    )


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    """Cancel any active FSM state."""
    await state.clear()
    await message.answer("вќЊ РћС‚РјРµРЅРµРЅРѕ.")


@router.message(Command("test_ai"))
async def cmd_test_ai(message: Message):
    """Admin: test all AI engines and report which ones work."""
    if not is_admin(message.from_user.id):
        return
    if not _rewriter:
        await message.answer("вќЊ AI rewriter РЅРµ РёРЅРёС†РёР°Р»РёР·РёСЂРѕРІР°РЅ.")
        return

    test_prompt = "РќР°РїРёС€Рё РѕРґРЅРѕ РєРѕСЂРѕС‚РєРѕРµ РїСЂРµРґР»РѕР¶РµРЅРёРµ: В«РР¶РµРІСЃРє вЂ” СЃС‚РѕР»РёС†Р° РЈРґРјСѓСЂС‚РёРёВ»."
    result_lines = ["рџ§Є <b>РўРµСЃС‚ AI РґРІРёР¶РєРѕРІ</b>\n"]

    # Test Gemini
    import time
    if _rewriter._gemini_models:
        if _rewriter._gemini_circuit_open():
            result_lines.append("вљЎ <b>Gemini</b> вЂ” Circuit Breaker РћРўРљР Р«Рў (СЃР»РёС€РєРѕРј РјРЅРѕРіРѕ РѕС€РёР±РѕРє Р·Р° С‡Р°СЃ)")
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
                    result_lines.append(f"вњ… <b>Gemini/{name}</b> вЂ” СЂР°Р±РѕС‚Р°РµС‚ ({elapsed:.1f}s)")
                    result_lines.append(f"   в†’ {resp.text.strip()[:80]}")
                else:
                    result_lines.append(f"вљ пёЏ <b>Gemini/{name}</b> вЂ” РїСѓСЃС‚РѕР№ РѕС‚РІРµС‚ ({elapsed:.1f}s)")
            except Exception as e:
                result_lines.append(f"вќЊ <b>Gemini</b> вЂ” РѕС€РёР±РєР°: {str(e)[:100]}")
    else:
        result_lines.append("вќЊ <b>Gemini</b> вЂ” РЅРµ РЅР°СЃС‚СЂРѕРµРЅ (РЅРµС‚ GEMINI_API_KEYS)")

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
                        result_lines.append(f"вњ… <b>YandexGPT</b> вЂ” СЂР°Р±РѕС‚Р°РµС‚ ({elapsed:.1f}s)")
                        result_lines.append(f"   в†’ {text.strip()[:80]}")
                    else:
                        body_text = await resp.text()
                        result_lines.append(
                            f"вќЊ <b>YandexGPT</b> вЂ” HTTP {resp.status} ({elapsed:.1f}s)\n"
                            f"   {body_text[:120]}"
                        )
        except Exception as e:
            result_lines.append(f"вќЊ <b>YandexGPT</b> вЂ” РѕС€РёР±РєР°: {str(e)[:100]}")
    else:
        result_lines.append("вљ пёЏ <b>YandexGPT</b> вЂ” РЅРµ РЅР°СЃС‚СЂРѕРµРЅ (РЅРµС‚ YANDEX_API_KEY / YANDEX_FOLDER_ID)")

    await message.answer("\n".join(result_lines), parse_mode=ParseMode.HTML)


@router.message(Command("aistats"))
async def cmd_aistats(message: Message):
    """Admin: show AI circuit breaker status and error stats."""
    if not is_admin(message.from_user.id):
        return
    if not _rewriter:
        await message.answer("вќЊ AI rewriter РЅРµ РёРЅРёС†РёР°Р»РёР·РёСЂРѕРІР°РЅ.")
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
        cb_status = f"вљЎ РћРўРљР Р«Рў вЂ” СЃР±СЂРѕСЃ С‡РµСЂРµР· {int(remaining // 60)} РјРёРЅ {int(remaining % 60)} СЃ"
    else:
        cb_status = "вњ… Р—РђРљР Р«Рў (Gemini СЂР°Р±РѕС‚Р°РµС‚ РІ С€С‚Р°С‚РЅРѕРј СЂРµР¶РёРјРµ)"

    # Gemini keys status
    keys = _rewriter.config.gemini_api_keys or []
    current_key = _rewriter._current_key_index + 1

    lines = [
        "рџ“Љ <b>AI Statistics</b>\n",
        f"рџ”Ґ <b>Circuit Breaker:</b> {cb_status}",
        f"вљ пёЏ <b>РћС€РёР±РѕРє Gemini (Р·Р° 1С‡):</b> {len(recent_errors)}/{max_errors}",
        "",
        f"рџ”‘ <b>Gemini API РєР»СЋС‡Рё:</b> {current_key}/{len(keys)} Р°РєС‚РёРІРµРЅ",
        f"рџ¤– <b>РњРѕРґРµР»Рё Gemini:</b> {len(_rewriter._gemini_models)} Р·Р°РіСЂСѓР¶РµРЅРѕ",
        "",
        f"рџ‡·рџ‡є <b>YandexGPT:</b> {'вњ… РєР»СЋС‡ РµСЃС‚СЊ' if _rewriter.config.yandex_api_key else 'вќЊ РЅРµС‚ РєР»СЋС‡Р°'}",
        "",
        "рџ’Ў РСЃРїРѕР»СЊР·СѓР№ /test_ai РґР»СЏ Р¶РёРІРѕРіРѕ С‚РµСЃС‚Р° РІСЃРµС… РґРІРёР¶РєРѕРІ",
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
                f"рџ“© <b>РќРѕРІРѕСЃС‚СЊ РѕС‚ РїРѕРґРїРёСЃС‡РёРєР°</b>\n"
                f"рџ‘¤ {_escape_html(user_info)}\n\n"
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
                admin_text += "(РјРµРґРёР°-СЃРѕРѕР±С‰РµРЅРёРµ)"
                await _bot.send_message(admin_id, admin_text, parse_mode=ParseMode.HTML)
                await message.forward(admin_id)
        except Exception as e:
            logger.error(f"Failed to forward user news to admin {admin_id}: {e}")

    await message.answer(
        "вњ… РЎРїР°СЃРёР±Рѕ! Р’Р°С€Р° РЅРѕРІРѕСЃС‚СЊ РѕС‚РїСЂР°РІР»РµРЅР° СЂРµРґР°РєС†РёРё.\n"
        "Р•СЃР»Рё РѕРЅР° РёРЅС‚РµСЂРµСЃРЅР°СЏ вЂ” РјС‹ РѕРїСѓР±Р»РёРєСѓРµРј РµС‘ РЅР° РєР°РЅР°Р»Рµ @IzhevskTodayNews!",
    )


# в”Ђв”Ђ Chat Moderation в”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђ

_MOD_RULES = [
    ("СЂРµРєР»Р°РјР°/СЃРїР°Рј", [
        "РєСѓРї", "РїСЂРѕРґР°Рј", "РїСЂРѕРґР°СЋ", "СЃРєРёРґРєР°", "Р°РєС†РёСЏ", "РїСЂРѕРјРѕРєРѕРґ", "Р·Р°СЂР°Р±РѕС‚",
        "РёРЅРІРµСЃС‚РёС†", "РєСЂРёРїС‚", "Р±РёС‚РєРѕРёРЅ", "РєР°Р·РёРЅРѕ", "СЃС‚Р°РІРє", "Р±СѓРєРјРµРєРµСЂ",
    ]),
    ("РЅР°СЂРєРѕС‚РёРєРё", [
        "РјРµС„РµРґСЂРѕРЅ", "Р°РјС„РµС‚Р°РјРёРЅ", "РіРµСЂРѕРёРЅ", "РєРѕРєР°РёРЅ", "РіР°С€РёС€", "РјР°СЂРёС…СѓР°РЅ",
        "СЃРїР°Р№СЃ", "Р·Р°РєР»Р°РґРє", "РЅР°СЂРє", "РІРµС‰РµСЃС‚РІР°",
    ]),
    ("РїРѕР»РёС‚РёРєР°/СЌРєСЃС‚СЂРµРјРёР·Рј", [
        "РїСѓС‚РёРЅ С…", "СЃР»Р°РІР° СѓРєСЂР°РёРЅРµ", "С…РѕС…РѕР»", "РєР°С†Р°Рї", "РЅР°С†РёСЃС‚", "С„Р°С€РёСЃС‚",
        "РґРѕР»РѕР№ РІР»Р°СЃС‚СЊ", "СЃРІРµСЂРіРЅСѓС‚СЊ", "РјРёС‚РёРЅРі РѕСЂРіР°РЅРёР·СѓРµРј", "РїСЂРѕС‚РµСЃС‚ РѕСЂРіР°РЅРёР·СѓРµРј",
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
        violated = "СЃСЃС‹Р»РєРё"
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
                f"в›” РЎРѕРѕР±С‰РµРЅРёРµ СѓРґР°Р»РµРЅРѕ ({violated}). РЎРѕР±Р»СЋРґР°Р№С‚Рµ РїСЂР°РІРёР»Р° С‡Р°С‚Р°."
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
        await message.answer("рџ“­ РћС‡РµСЂРµРґСЊ РїСѓСЃС‚Р° вЂ” РЅРµС‚ РїРѕСЃС‚РѕРІ РЅР° РјРѕРґРµСЂР°С†РёРё.")
        return

    await message.answer(f"рџ“‹ **Р’ РѕС‡РµСЂРµРґРё РЅР° РјРѕРґРµСЂР°С†РёСЋ: {len(posts)} РїРѕСЃС‚РѕРІ**\n\n"
                        "РћС‚РїСЂР°РІР»СЏСЋ РїРµСЂРІС‹Р№ РїРѕСЃС‚...",
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
        "рџ“Љ **РЎС‚Р°С‚РёСЃС‚РёРєР° РїРѕСЃС‚РѕРІ:**\n\n"
        f"вЏі Р’ РѕР¶РёРґР°РЅРёРё СЂРµСЂР°Р№С‚Р°: {stats.get('pending', 0)}\n"
        f"рџ”„ Р РµСЂР°Р№С‚РёС‚СЃСЏ: {stats.get('rewriting', 0)}\n"
        f"рџ‘Ђ РќР° РјРѕРґРµСЂР°С†РёРё: {stats.get('review', 0)}\n"
        f"вњ… РћРґРѕР±СЂРµРЅРѕ: {stats.get('approved', 0)}\n"
        f"вќЊ РћС‚РєР»РѕРЅРµРЅРѕ: {stats.get('rejected', 0)}\n"
        f"рџ“ў РћРїСѓР±Р»РёРєРѕРІР°РЅРѕ: {stats.get('published', 0)}\n"
    )
    await message.answer(text, parse_mode=ParseMode.MARKDOWN)


@router.message(Command("sources"))
async def cmd_sources(message: Message):
    """Show source channels."""
    if not is_admin(message.from_user.id):
        return

    sources = await _db.get_active_sources()
    if not sources:
        text = "рџ“Ў РќРµС‚ Р°РєС‚РёРІРЅС‹С… РёСЃС‚РѕС‡РЅРёРєРѕРІ."
    else:
        lines = ["рџ“Ў **РђРєС‚РёРІРЅС‹Рµ РёСЃС‚РѕС‡РЅРёРєРё:**\n"]
        for s in sources:
            lines.append(f"  вЂў @{s['channel_username']} (РїРѕСЃР»РµРґРЅРёР№ ID: {s['last_message_id']})")
        text = "\n".join(lines)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="вћ• Р”РѕР±Р°РІРёС‚СЊ РёСЃС‚РѕС‡РЅРёРє", callback_data="add_source")],
    ])
    await message.answer(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)


@router.message(Command("help"))
async def cmd_help(message: Message):
    """Show help."""
    text = (
        "рџ“– **РљРѕРјР°РЅРґС‹:**\n\n"
        "/start вЂ” Р“Р»Р°РІРЅРѕРµ РјРµРЅСЋ\n"
        "/queue вЂ” РћС‡РµСЂРµРґСЊ РЅР° РјРѕРґРµСЂР°С†РёСЋ\n"
        "/stats вЂ” РЎС‚Р°С‚РёСЃС‚РёРєР°\n"
        "/sources вЂ” РЈРїСЂР°РІР»РµРЅРёРµ РёСЃС‚РѕС‡РЅРёРєР°РјРё\n"
        "/publish вЂ” РћРїСѓР±Р»РёРєРѕРІР°С‚СЊ РѕРґРѕР±СЂРµРЅРЅС‹Рµ РїРѕСЃС‚С‹\n"
        "/report вЂ” РќРµРґРµР»СЊРЅС‹Р№ РѕС‚С‡С‘С‚\n"
        "/help вЂ” Р­С‚Рѕ СЃРѕРѕР±С‰РµРЅРёРµ"
    )
    await message.answer(text, parse_mode=ParseMode.MARKDOWN)


@router.message(Command("testgemini"))
async def cmd_test_gemini(message: Message):
    """Test Gemini API connectivity вЂ” admin only diagnostic."""
    if not is_admin(message.from_user.id):
        return

    lines = []

    # Check 1: API key
    key = _config.gemini_api_key if _config else "NO CONFIG"
    lines.append(f"рџ”‘ API Key: {'SET (' + key[:10] + '...)' if key else 'вќЊ NOT SET'}")

    # Check 2: Model
    if _rewriter and _rewriter._gemini_model:
        lines.append("рџ¤– Model: вњ… initialized")
    else:
        lines.append("рџ¤– Model: вќЊ NOT initialized")

    # Check 3: Try actual API call
    try:
        genai.configure(api_key=_config.gemini_api_key)
        model = genai.GenerativeModel("gemini-2.0-flash")
        response = model.generate_content("РЎРєР°Р¶Рё РѕРґРЅРѕ СЃР»РѕРІРѕ: РїСЂРёРІРµС‚")
        if response and response.text:
            lines.append(f"рџ“Ў API Call: вњ… OK вЂ” '{response.text.strip()[:50]}'")
        else:
            lines.append("рџ“Ў API Call: вќЊ Empty response")
            if hasattr(response, 'candidates'):
                lines.append(f"   Candidates: {response.candidates}")
    except Exception as e:
        lines.append("рџ“Ў API Call: вќЊ ERROR")
        lines.append(f"   {type(e).__name__}: {str(e)[:200]}")
        lines.append(f"   Traceback: {traceback.format_exc()[-300:]}")

    await message.answer("\n".join(lines))


@router.message(Command("testai"))
async def cmd_test_ai(message: Message):
    """Test ALL AI engines вЂ” admin only diagnostic."""
    if not is_admin(message.from_user.id):
        return

    lines = ["рџ”Ќ **РўРµСЃС‚ РІСЃРµС… AI-РґРІРёР¶РєРѕРІ:**\n"]

    # Test 1: Gemini
    lines.append("в•ђв•ђв•ђ GEMINI в•ђв•ђв•ђ")
    key = _config.gemini_api_key if _config else ""
    lines.append(f"рџ”‘ Key: {'SET (' + key[:10] + '...)' if key else 'вќЊ NOT SET'}")
    if _rewriter and _rewriter._gemini_models:
        names = [m[0] for m in _rewriter._gemini_models]
        lines.append(f"рџ¤– Models: {', '.join(names)}")
    else:
        lines.append("рџ¤– Models: вќЊ none")

    # Test 2: YandexGPT
    lines.append("\nв•ђв•ђв•ђ YANDEX GPT в•ђв•ђв•ђ")
    ykey = _config.yandex_api_key if _config else ""
    yfolder = _config.yandex_folder_id if _config else ""
    lines.append(f"рџ”‘ Key: {'SET (' + ykey[:10] + '...)' if ykey else 'вќЊ NOT SET'}")
    lines.append(f"рџ“Ѓ Folder: {yfolder if yfolder else 'вќЊ NOT SET'}")

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
                "messages": [{"role": "user", "text": "РЎРєР°Р¶Рё РѕРґРЅРѕ СЃР»РѕРІРѕ: РїСЂРёРІРµС‚"}],
            }
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=body, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        text = data["result"]["alternatives"][0]["message"]["text"]
                        lines.append(f"рџ“Ў API: вњ… OK вЂ” '{text.strip()[:50]}'")
                    else:
                        error = await resp.text()
                        lines.append(f"рџ“Ў API: вќЊ HTTP {resp.status}")
                        lines.append(f"   {error[:300]}")
        except Exception as e:
            lines.append(f"рџ“Ў API: вќЊ {type(e).__name__}: {str(e)[:200]}")

    # Test 3: ReText
    lines.append("\nв•ђв•ђв•ђ RETEXT.AI в•ђв•ђв•ђ")
    rkey = _config.retext_api_key if _config else ""
    lines.append(f"рџ”‘ Key: {'SET' if rkey else 'вќЊ NOT SET'}")

    await message.answer("\n".join(lines))


@router.message(Command("testvk"))
async def cmd_test_vk(message: Message):
    """Test VK API connection вЂ” admin only diagnostic."""
    if not is_admin(message.from_user.id):
        return

    if not _vk_publisher:
        await message.answer("вќЊ VK publisher not initialized (bot restarting?)")
        return

    lines = ["рџ”Ќ <b>Р”РёР°РіРЅРѕСЃС‚РёРєР° VK</b>\n"]
    lines.append(f"рџ”‘ РўРѕРєРµРЅ: {'вњ… SET (' + _vk_publisher.access_token[:8] + '...)' if _vk_publisher.access_token else 'вќЊ РќР• Р—РђР”РђРќ'}")
    lines.append(f"рџ‘Ґ Group ID: {'вњ… ' + _vk_publisher.group_id if _vk_publisher.group_id else 'вќЊ РќР• Р—РђР”РђРќ'}")
    lines.append(f"рџ“Ў Enabled: {'вњ… Р”Р°' if _vk_publisher.enabled else 'вќЊ РќРµС‚'}")

    if _vk_publisher.enabled:
        lines.append("\nвЏі РџСЂРѕРІРµСЂСЏСЋ СЃРѕРµРґРёРЅРµРЅРёРµ СЃ VK API...")
        await message.answer("\n".join(lines), parse_mode=ParseMode.HTML)
        result = await _vk_publisher.test_connection()
        status_lines = [
            f"\nрџ“Љ <b>Р РµР·СѓР»СЊС‚Р°С‚ РїСЂРѕРІРµСЂРєРё:</b>",
            f"Status: {'вњ… OK' if result.get('status') == 'ok' else 'вќЊ ERROR'}",
        ]
        if result.get('group_name'):
            status_lines.append(f"Р“СЂСѓРїРїР°: {result['group_name']}")
            status_lines.append(f"URL: {result.get('group_url', '')}")
        await message.answer("\n".join(status_lines), parse_mode=ParseMode.HTML)
    else:
        lines.append("\nв›” VK crosspost РѕС‚РєР»СЋС‡С‘РЅ вЂ” Р·Р°РґР°Р№С‚Рµ VK_ACCESS_TOKEN Рё VK_GROUP_ID РІ Dokploy.")
        await message.answer("\n".join(lines), parse_mode=ParseMode.HTML)


@router.message(Command("testcontent"))
async def cmd_test_content(message: Message):
    """Manually trigger any content rubric right now вЂ” admin only."""
    if not is_admin(message.from_user.id):
        return

    args = message.text.split(maxsplit=1)
    rubric = args[1].strip().lower() if len(args) > 1 else ""

    valid = {
        "weather": "рџЊ¤ РџРѕРіРѕРґР°",
        "history_fact": "рџ“… РСЃС‚РѕСЂРёСЏ РґРЅСЏ",
        "five_facts": "рџ“Њ 5 С„Р°РєС‚РѕРІ",
        "recipe": "рџЌЅ Р РµС†РµРїС‚",
        "lifehack": "рџ’Ў Р›Р°Р№С„С…Р°Рє",
        "place": "рџ“Ќ РњРµСЃС‚Рѕ",
        "evening_fun": "рџ„ Р’РµС‡РµСЂРЅРёР№ fun",
        "daily_digest": "рџ“Љ Р”Р°Р№РґР¶РµСЃС‚",
    }

    if rubric not in valid:
        lines = ["<b>рџ“‹ Р”РѕСЃС‚СѓРїРЅС‹Рµ СЂСѓР±СЂРёРєРё:</b>"]
        for key, name in valid.items():
            lines.append(f"  <code>/testcontent {key}</code> вЂ” {name}")
        await message.answer("\n".join(lines), parse_mode=ParseMode.HTML)
        return

    await message.answer(f"вЏі Р“РµРЅРµСЂРёСЂСѓСЋ <b>{valid[rubric]}</b>...", parse_mode=ParseMode.HTML)
    sched = _content_scheduler
    if not sched:
        await message.answer("вќЊ Content scheduler РЅРµ РёРЅРёС†РёР°Р»РёР·РёСЂРѕРІР°РЅ (Р±РѕС‚ СЂРµСЃС‚Р°СЂС‚СѓРµС‚?)")
        return

    try:
        await sched._publish_rubric(rubric, valid[rubric])
        await message.answer(f"вњ… <b>{valid[rubric]}</b> РѕРїСѓР±Р»РёРєРѕРІР°РЅРѕ РІ РєР°РЅР°Р»!", parse_mode=ParseMode.HTML)
    except Exception as e:
        await message.answer(f"вќЊ РћС€РёР±РєР°: <code>{e}</code>", parse_mode=ParseMode.HTML)


# в”Ђв”Ђ Reference to content scheduler (set from main.py) в”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђ
_content_scheduler = None


@router.message(Command("publish"))
async def cmd_publish(message: Message):
    """Publish all approved posts with delays between them."""
    if not is_admin(message.from_user.id):
        return

    approved = await _db.get_approved_posts()
    if not approved:
        await message.answer("рџ“­ РќРµС‚ РѕРґРѕР±СЂРµРЅРЅС‹С… РїРѕСЃС‚РѕРІ РґР»СЏ РїСѓР±Р»РёРєР°С†РёРё.")
        return

    total = len(approved)
    await message.answer(f"рџ“ў РќР°С‡РёРЅР°СЋ РїСѓР±Р»РёРєР°С†РёСЋ {total} РїРѕСЃС‚РѕРІ СЃ РёРЅС‚РµСЂРІР°Р»РѕРј 30 СЃРµРє...")

    published_count = 0
    for i, post in enumerate(approved):
        success = await _publish_post(post)
        if success:
            published_count += 1
        # Don't sleep after the last post
        if i < total - 1:
            await asyncio.sleep(30)  # 30 seconds between posts to avoid flooding

    await message.answer(f"вњ… РћРїСѓР±Р»РёРєРѕРІР°РЅРѕ: {published_count}/{total}")


@router.message(Command("report"))
async def cmd_report(message: Message):
    """Show weekly analytics report."""
    if not is_admin(message.from_user.id):
        return

    stats = await _db.get_stats()
    weekly = await _db.get_weekly_stats()

    total = sum(stats.values())
    text = (
        "рџ“Љ **РќРµРґРµР»СЊРЅС‹Р№ РѕС‚С‡С‘С‚:**\n\n"
        f"рџ“Ґ РЎРѕР±СЂР°РЅРѕ РЅРѕРІРѕСЃС‚РµР№: {weekly.get('total', 0)}\n"
        f"вњ… РћРґРѕР±СЂРµРЅРѕ: {weekly.get('approved', 0)}\n"
        f"вќЊ РћС‚РєР»РѕРЅРµРЅРѕ: {weekly.get('rejected', 0)}\n"
        f"  в†і рџ”„ Р”СѓР±Р»РёРєР°С‚С‹: Р°РІС‚РѕРјР°С‚РёС‡РµСЃРєРё\n"
        f"  в†і рџ“ў Р РµРєР»Р°РјР°: Р°РІС‚РѕРјР°С‚РёС‡РµСЃРєРё\n"
        f"  в†і рџЊЌ РќРµСЂРµР»РµРІР°РЅС‚РЅС‹Рµ: Р°РІС‚РѕРјР°С‚РёС‡РµСЃРєРё\n"
        f"рџ“ў РћРїСѓР±Р»РёРєРѕРІР°РЅРѕ: {weekly.get('published', 0)}\n\n"
        "рџ“Ў **РџРѕ РёСЃС‚РѕС‡РЅРёРєР°Рј:**\n"
    )

    for src, count in weekly.get('by_source', {}).items():
        text += f"  вЂў @{src}: {count}\n"

    await message.answer(text, parse_mode=ParseMode.MARKDOWN)


# в”Ђв”Ђ Callback Handlers в”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђ

@router.callback_query(F.data == "queue")
async def cb_queue(callback: CallbackQuery):
    """Queue button handler."""
    if not is_admin(callback.from_user.id):
        await callback.answer("в›” РќРµС‚ РґРѕСЃС‚СѓРїР°", show_alert=True)
        return

    await callback.answer()
    posts = await _db.get_review_posts(limit=5)
    if not posts:
        await callback.message.answer("рџ“­ РћС‡РµСЂРµРґСЊ РїСѓСЃС‚Р°.")
        return

    await callback.message.answer(f"рџ“‹ РќР° РјРѕРґРµСЂР°С†РёРё: {len(posts)} РїРѕСЃС‚РѕРІ")
    for post in posts[:3]:
        await _send_review_post(callback.message.chat.id, post)
        await asyncio.sleep(0.5)


@router.callback_query(F.data == "stats")
async def cb_stats(callback: CallbackQuery):
    """Stats button handler."""
    if not is_admin(callback.from_user.id):
        await callback.answer("в›” РќРµС‚ РґРѕСЃС‚СѓРїР°", show_alert=True)
        return

    await callback.answer()
    stats = await _db.get_stats()
    text = (
        "рџ“Љ **РЎС‚Р°С‚РёСЃС‚РёРєР°:**\n\n"
        f"вЏі РћР¶РёРґР°РЅРёРµ: {stats.get('pending', 0)} | "
        f"рџ‘Ђ РњРѕРґРµСЂР°С†РёСЏ: {stats.get('review', 0)} | "
        f"рџ“ў РћРїСѓР±Р»РёРєРѕРІР°РЅРѕ: {stats.get('published', 0)}"
    )
    await callback.message.answer(text, parse_mode=ParseMode.MARKDOWN)


@router.callback_query(F.data == "sources")
async def cb_sources(callback: CallbackQuery):
    """Sources button handler."""
    await callback.answer()
    sources = await _db.get_active_sources()
    lines = ["рџ“Ў <b>РСЃС‚РѕС‡РЅРёРєРё:</b>\n"] + [f"  вЂў @{s['channel_username']}" for s in sources]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="вћ• Р”РѕР±Р°РІРёС‚СЊ", callback_data="add_source")],
    ])
    await callback.message.answer("\n".join(lines) or "РќРµС‚ РёСЃС‚РѕС‡РЅРёРєРѕРІ", reply_markup=kb, parse_mode=ParseMode.HTML)


@router.callback_query(F.data == "settings")
async def cb_settings(callback: CallbackQuery):
    """Settings button handler."""
    if not is_admin(callback.from_user.id):
        await callback.answer("в›” РќРµС‚ РґРѕСЃС‚СѓРїР°", show_alert=True)
        return

    await callback.answer()
    text = (
        f"вљ™пёЏ <b>РќР°СЃС‚СЂРѕР№РєРё Р±РѕС‚Р°</b>\n\n"
        f"рџ“Ў РСЃС‚РѕС‡РЅРёРєРѕРІ: <b>{len(_config.source_channels)}</b>\n"
        f"вЏ± РРЅС‚РµСЂРІР°Р» РїСЂРѕРІРµСЂРєРё: <b>{_config.check_interval} СЃРµРє</b>\n"
        f"рџ“¤ РРЅС‚РµСЂРІР°Р» РїСѓР±Р»РёРєР°С†РёРё: <b>{_config.publish_interval // 60} РјРёРЅ</b>\n"
        f"рџ“Џ РњРёРЅ. РґР»РёРЅР° С‚РµРєСЃС‚Р°: <b>{_config.min_text_length} СЃРёРјРІРѕР»РѕРІ</b>\n"
        f"рџ—Ј РЇР·С‹Рє: <b>{_config.language}</b>\n\n"
        f"рџљ« Р¤РёР»СЊС‚СЂС‹:\n"
        f"  вЂў Р РµРєР»Р°РјР°: <b>{len(_config.ad_stop_words)} СЃР»РѕРІ</b>\n"
        f"  вЂў РЎСЂРѕС‡РЅС‹Рµ РЅРѕРІРѕСЃС‚Рё: <b>{len(_config.breaking_keywords)} СЃР»РѕРІ</b>\n\n"
        f"рџ“ў РљР°РЅР°Р»: <b>@{_config.target_channel}</b>"
    )
    await callback.message.answer(text, parse_mode=ParseMode.HTML)


@router.callback_query(F.data == "add_source")
async def cb_add_source(callback: CallbackQuery, state: FSMContext):
    """Start adding a source channel."""
    if not is_admin(callback.from_user.id):
        await callback.answer("в›” РќРµС‚ РґРѕСЃС‚СѓРїР°", show_alert=True)
        return

    await callback.answer()
    await callback.message.answer(
        "рџ“Ў РћС‚РїСЂР°РІСЊ username РєР°РЅР°Р»Р° (Р±РµР· @).\n"
        "РќР°РїСЂРёРјРµСЂ: `ria_novosti`",
        parse_mode=ParseMode.MARKDOWN,
    )
    await state.set_state(AddSourceStates.waiting_for_channel)


@router.message(AddSourceStates.waiting_for_channel)
async def process_add_source(message: Message, state: FSMContext):
    """Process new source channel username."""
    channel = message.text.strip().lstrip("@")
    if not channel:
        await message.answer("вќЊ РџСѓСЃС‚РѕРµ РёРјСЏ РєР°РЅР°Р»Р°.")
        return

    await _db.add_source(channel)
    _config.source_channels.append(channel)
    await state.clear()
    await message.answer(
        f"вњ… РљР°РЅР°Р» @{channel} РґРѕР±Р°РІР»РµРЅ РІ РёСЃС‚РѕС‡РЅРёРєРё!\n\n"
        "вљ пёЏ Р”Р»СЏ Р°РєС‚РёРІР°С†РёРё РјРѕРЅРёС‚РѕСЂРёРЅРіР° РЅРѕРІРѕРіРѕ РєР°РЅР°Р»Р° РїРµСЂРµР·Р°РїСѓСЃС‚РёС‚Рµ Р±РѕС‚Р°.",
    )


# в”Ђв”Ђ Moderation Callbacks в”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђ

@router.callback_query(F.data.startswith("approve:"))
async def cb_approve(callback: CallbackQuery):
    """Approve a post for publishing."""
    if not is_admin(callback.from_user.id):
        await callback.answer("в›” РќРµС‚ РґРѕСЃС‚СѓРїР°", show_alert=True)
        return

    post_id = int(callback.data.split(":")[1])
    await _db.update_post_status(post_id, "approved", reviewed_by=callback.from_user.id)
    await callback.answer("вњ… РџРѕСЃС‚ РѕРґРѕР±СЂРµРЅ!")

    # Ask if should publish now
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="рџ“ў РћРїСѓР±Р»РёРєРѕРІР°С‚СЊ СЃРµР№С‡Р°СЃ", callback_data=f"publish_now:{post_id}"),
            InlineKeyboardButton(text="вЏ° РџРѕР·Р¶Рµ", callback_data="dismiss"),
        ],
    ])
    await callback.message.edit_reply_markup(reply_markup=kb)


@router.callback_query(F.data.startswith("reject:"))
async def cb_reject(callback: CallbackQuery):
    """Reject a post."""
    if not is_admin(callback.from_user.id):
        await callback.answer("в›” РќРµС‚ РґРѕСЃС‚СѓРїР°", show_alert=True)
        return

    post_id = int(callback.data.split(":")[1])
    await _db.update_post_status(post_id, "rejected", reviewed_by=callback.from_user.id)
    await callback.answer("вќЊ РџРѕСЃС‚ РѕС‚РєР»РѕРЅС‘РЅ")
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.reply("вќЊ РџРѕСЃС‚ РѕС‚РєР»РѕРЅС‘РЅ Рё СѓРґР°Р»С‘РЅ РёР· РѕС‡РµСЂРµРґРё.")


@router.callback_query(F.data.startswith("edit:"))
async def cb_edit(callback: CallbackQuery, state: FSMContext):
    """Start editing a post."""
    if not is_admin(callback.from_user.id):
        await callback.answer("в›” РќРµС‚ РґРѕСЃС‚СѓРїР°", show_alert=True)
        return

    post_id = int(callback.data.split(":")[1])
    await state.update_data(edit_post_id=post_id)
    await state.set_state(EditPostStates.waiting_for_text)
    await callback.answer()
    await callback.message.reply(
        "вњЏпёЏ РћС‚РїСЂР°РІСЊ РЅРѕРІС‹Р№ С‚РµРєСЃС‚ РґР»СЏ СЌС‚РѕРіРѕ РїРѕСЃС‚Р°.\n"
        "РћС‚РїСЂР°РІСЊ /cancel РґР»СЏ РѕС‚РјРµРЅС‹."
    )


@router.message(EditPostStates.waiting_for_text)
async def process_edit_text(message: Message, state: FSMContext):
    """Process edited text for a post."""
    if message.text == "/cancel":
        await state.clear()
        await message.answer("вќЊ Р РµРґР°РєС‚РёСЂРѕРІР°РЅРёРµ РѕС‚РјРµРЅРµРЅРѕ.")
        return

    data = await state.get_data()
    post_id = data.get("edit_post_id")
    if not post_id:
        await state.clear()
        return

    await _db.update_post_text(post_id, message.text)
    await state.clear()

    post = await _db.get_post(post_id)
    await message.answer("вњ… РўРµРєСЃС‚ РѕР±РЅРѕРІР»С‘РЅ! РћС‚РїСЂР°РІР»СЏСЋ РїРѕСЃС‚ РЅР° РїРѕРІС‚РѕСЂРЅСѓСЋ РјРѕРґРµСЂР°С†РёСЋ:")
    await _send_review_post(message.chat.id, post)


@router.callback_query(F.data.startswith("rewrite:"))
async def cb_rewrite(callback: CallbackQuery):
    """Re-run AI rewrite on a post."""
    if not is_admin(callback.from_user.id):
        await callback.answer("в›” РќРµС‚ РґРѕСЃС‚СѓРїР°", show_alert=True)
        return

    post_id = int(callback.data.split(":")[1])
    await callback.answer("рџ”„ РџРµСЂРµР·Р°РїСѓСЃРєР°СЋ СЂРµСЂР°Р№С‚...")

    post = await _db.get_post(post_id)
    if post:
        await _db.update_post_status(post_id, "rewriting")

        # Clean original text before rewriting (same as pipeline)
        clean_original = _clean_text(post["original_text"])
        rewritten, engine = await _rewriter.rewrite(clean_original)
        if rewritten:
            rewritten = _clean_text(rewritten)  # Clean AI output

            # в”Ђв”Ђ Р¤РёР»СЊС‚СЂ С‡СѓРІСЃС‚РІРёС‚РµР»СЊРЅРѕРіРѕ РєРѕРЅС‚РµРЅС‚Р° в”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђ
            _cf_result = filter_sensitive_content(rewritten)
            if _cf_result.action == FilterAction.BLOCK:
                await _db.update_post_status(post_id, "rejected")
                logger.warning(
                    f"Post #{post_id} BLOCKED by content_filter (manual rewrite): {_cf_result.reason}"
                )
                await callback.message.reply(
                    f"рџљ« РџРѕСЃС‚ Р·Р°Р±Р»РѕРєРёСЂРѕРІР°РЅ С„РёР»СЊС‚СЂРѕРј С‡СѓРІСЃС‚РІРёС‚РµР»СЊРЅРѕРіРѕ РєРѕРЅС‚РµРЅС‚Р°.\n"
                    f"<code>{_cf_result.reason}</code>",
                    parse_mode=ParseMode.HTML,
                )
                return
            rewritten = _cf_result.text  # Р’РѕР·РјРѕР¶РЅРѕ Р·Р°РјРµРЅРµРЅС‹ СЌРІС„РµРјРёР·РјС‹ / РґРѕР±Р°РІР»РµРЅ РґРёСЃРєР»РµР№РјРµСЂ
            # в”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђ

            # Generate hashtags and format
            hashtags = await _rewriter.generate_hashtags(rewritten)
            rewritten = _format_post(rewritten, hashtags)
            
            await _db.update_post_rewrite(post_id, rewritten)
            uniqueness = _rewriter.calculate_uniqueness(clean_original, rewritten)

            updated_post = await _db.get_post(post_id)
            await callback.message.reply(f"вњ… РџРµСЂРµСЂР°Р№С‚ Р·Р°РІРµСЂС€С‘РЅ (РґРІРёР¶РѕРє: {engine}, СѓРЅРёРєР°Р»СЊРЅРѕСЃС‚СЊ: {uniqueness:.0%})")
            await _send_review_post(callback.message.chat.id, updated_post)
        else:
            logger.error(f"Post #{post_id}: manual rewrite failed - all AI engines returned None")
            await callback.message.reply("вќЊ Р РµСЂР°Р№С‚ РЅРµ СѓРґР°Р»СЃСЏ. Р’СЃРµ AI-РґРІРёР¶РєРё (Gemini/YandexGPT) РЅРµРґРѕСЃС‚СѓРїРЅС‹.")
            await _db.update_post_status(post_id, "review")


@router.callback_query(F.data.startswith("search_photo:"))
async def cb_search_photo(callback: CallbackQuery):
    """Search for stock photos for a post."""
    if not is_admin(callback.from_user.id):
        await callback.answer("в›” РќРµС‚ РґРѕСЃС‚СѓРїР°", show_alert=True)
        return

    post_id = int(callback.data.split(":")[1])
    await callback.answer("рџ”Ќ РС‰Сѓ РїРѕРґС…РѕРґСЏС‰РёРµ С„РѕС‚Рѕ...")

    post = await _db.get_post(post_id)
    if not post:
        return

    # Extract keywords
    text = post.get("rewritten_text") or post["original_text"]
    keywords = await _rewriter.generate_keywords(text)

    if not keywords:
        await callback.message.reply("вќЊ РќРµ СѓРґР°Р»РѕСЃСЊ РёР·РІР»РµС‡СЊ РєР»СЋС‡РµРІС‹Рµ СЃР»РѕРІР° РґР»СЏ РїРѕРёСЃРєР°.")
        return

    # Search stock photos
    photos = await _media_processor.search_stock_photo(keywords, count=3)
    if not photos:
        await callback.message.reply(f"рџ“· Р¤РѕС‚Рѕ РЅРµ РЅР°Р№РґРµРЅС‹. РљР»СЋС‡РµРІС‹Рµ СЃР»РѕРІР°: {', '.join(keywords)}")
        return

    # Send photo options
    await callback.message.reply(f"рџ”Ќ РљР»СЋС‡РµРІС‹Рµ СЃР»РѕРІР°: {', '.join(keywords)}\nРќР°Р№РґРµРЅРѕ {len(photos)} С„РѕС‚Рѕ:")

    for i, photo in enumerate(photos):
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text=f"вњ… РСЃРїРѕР»СЊР·РѕРІР°С‚СЊ СЌС‚Рѕ С„РѕС‚Рѕ",
                callback_data=f"use_photo:{post_id}:{i}",
            )],
        ])
        caption = f"рџ“· {photo.get('description', 'Stock photo')} | by {photo['author']}"
        try:
            await _bot.send_photo(
                callback.message.chat.id,
                photo=photo["thumb_url"],
                caption=caption[:200],
                reply_markup=kb,
            )
        except Exception as e:
            await callback.message.reply(f"Р¤РѕС‚Рѕ {i+1}: {photo['url']}", reply_markup=kb)

        await asyncio.sleep(0.5)


@router.callback_query(F.data.startswith("publish_now:"))
async def cb_publish_now(callback: CallbackQuery):
    """Publish a specific post immediately."""
    if not is_admin(callback.from_user.id):
        await callback.answer("в›” РќРµС‚ РґРѕСЃС‚СѓРїР°", show_alert=True)
        return

    post_id = int(callback.data.split(":")[1])
    post = await _db.get_post(post_id)

    if not post:
        await callback.answer("вќЊ РџРѕСЃС‚ РЅРµ РЅР°Р№РґРµРЅ", show_alert=True)
        return

    await callback.answer("рџ“ў РџСѓР±Р»РёРєСѓСЋ...")
    success = await _publish_post(post)

    if success:
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.reply("рџ“ў РџРѕСЃС‚ РѕРїСѓР±Р»РёРєРѕРІР°РЅ!")
    else:
        await callback.message.reply("вќЊ РћС€РёР±РєР° РїСЂРё РїСѓР±Р»РёРєР°С†РёРё. РџСЂРѕРІРµСЂСЊС‚Рµ РЅР°СЃС‚СЂРѕР№РєРё РєР°РЅР°Р»Р°.")


@router.callback_query(F.data == "dismiss")
async def cb_dismiss(callback: CallbackQuery):
    """Dismiss a notification."""
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)


# в”Ђв”Ђ Helper Functions (thin wrappers for backwards compat) в”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђ
# All implementations live in src/utils.py

def _escape_html(text: str) -> str:
    """Escape HTML вЂ” delegates to utils.escape_html."""
    return escape_html(text)


def _clean_text(text: str) -> str:
    """Clean post text вЂ” delegates to utils.clean_text."""
    return clean_text(text)


def _format_post(text: str, hashtags: list) -> str:
    """Format post вЂ” delegates to utils.format_post."""
    return format_post(text, hashtags)


def _is_similar_to_any(text: str, candidates: list) -> bool:
    """Deduplication check вЂ” delegates to utils.is_similar_to_any."""
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
    rewritten = post.get("rewritten_text") or "вЏі Р•С‰С‘ РЅРµ РїРµСЂРµРїРёСЃР°РЅ"
    # Don't escape rewritten text вЂ” it contains intentional HTML from _format_post (<b>, <a>)
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
        f"{status} <b>РџРѕСЃС‚ #{post['id']}</b> | РСЃС‚РѕС‡РЅРёРє: @{source}\n"
        f"рџ“… {date_str}\n\n"
        f"рџ“ќ <b>РћСЂРёРіРёРЅР°Р»:</b>\n{original}\n\n"
        f"вњЌпёЏ <b>Р РµСЂР°Р№С‚:</b>\n{rewritten_display}"
    )

    # Add media info
    if post.get("replacement_media_url"):
        text += f"\n\nрџ–ј РЎС‚РѕРєРѕРІРѕРµ С„РѕС‚Рѕ РїРѕРґРѕР±СЂР°РЅРѕ вњ…"
    elif post["media_type"] != "none":
        text += f"\n\nрџ–ј РњРµРґРёР°: {post['media_type']}"
        if post.get("has_watermark"):
            text += " вљ пёЏ РћР±РЅР°СЂСѓР¶РµРЅ РІРѕРґСЏРЅРѕР№ Р·РЅР°Рє!"

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

    allow_source_media = bool(_config and _config.use_source_media)
    media_path = post.get("media_local_path") if allow_source_media else None
    media_url = post.get("media_file_id") if allow_source_media else None
    replacement_url = post.get("replacement_media_url")

    msg = None
    local_stock: Optional[str] = None  # Will hold local path of downloaded stock photo for VK reuse

    # в”Ђв”Ђ Try to publish with photo в”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђ
    try:
        if replacement_url:
            # Download locally first вЂ” Wikimedia/CDN URLs often block Telegram's fetcher
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

    # в”Ђв”Ђ Fallback: text-only в”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђ
    if msg is None:
        try:
            msg = await _bot.send_message(
                target,
                text[:4096],
                parse_mode=ParseMode.HTML,
            )
        except Exception as text_err:
            # Even text failed вЂ” mark as failed so queue is not blocked
            logger.error(
                f"Post #{post['id']}: text-only fallback also failed: {text_err} вЂ” marking as publish_failed"
            )
            await _db.update_post_status(post["id"], "publish_failed")
            return False

    # в”Ђв”Ђ Record publication в”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђ
    await _db.update_post_status(post["id"], "published")
    await _db.add_published(post["id"], msg.message_id)
    logger.info(f"Published post #{post['id']} to {target}")

    # в”Ђв”Ђ Emoji reaction directly on the post в”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђ
    # Telegram bots can set only ONE reaction per message (non-premium limit).
    try:
        _t = (post.get("rewritten_text") or post["original_text"]).lower()
        if any(w in _t for w in ["РїРѕРіРёР±", "Р°РІР°СЂРёСЏ", "РґС‚Рї", "РїРѕР¶Р°СЂ", "С‚СЂР°РіРµРґ", "Р¶РµСЂС‚РІ"]):
            _emoji = "рџў"                                          # tragedy в†’ empathy
        elif any(w in _t for w in ["Р¶РєС…", "С‚Р°СЂРёС„", "С‡РёРЅРѕРІРЅРёРє", "РјСЌСЂ", "РґРµРїСѓС‚Р°С‚", "Р±СЋРґР¶РµС‚"]):
            _emoji = "рџЎ"                                          # bureaucracy в†’ sarcasm
        elif any(w in _t for w in ["РѕС‚РєСЂС‹С‚", "РЅРѕРІС‹Р№", "Р·Р°РїСѓСЃС‚", "РїРѕСЃС‚СЂРѕРµРЅ", "РїРѕР±РµРґРёР»"]):
            _emoji = "рџ”Ґ"                                          # good news в†’ positivity
        elif any(w in _t for w in ["С†РµРЅ", "РїРѕРґРѕСЂРѕР¶Р°Р»", "СЂРѕСЃС‚", "РёРЅС„Р»СЏС†", "Р·Р°СЂРїР»Р°С‚"]):
            _emoji = "рџЎ"                                          # prices в†’ frustration
        else:
            _emoji = "рџ‘Ќ"                                          # universal default
        await _bot.set_message_reaction(
            chat_id=target,
            message_id=msg.message_id,
            reaction=[ReactionTypeEmoji(emoji=_emoji)],
        )
        logger.info(f"Post #{post['id']}: reaction set {_emoji}")
    except Exception as react_err:
        logger.warning(f"Post #{post['id']}: reaction failed ({react_err})")

    # в”Ђв”Ђ Cross-post to VK в”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђ
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
                seo_enabled=_config.vk_seo_enabled,
                seo_max_tags=_config.vk_seo_max_tags,
            )
            if vk_post_id:
                logger.info(f"Post #{post['id']} cross-posted to VK (vk_post_id={vk_post_id})")
                if _config.vk_self_comment_enabled:
                    import random
                    comments = [
                        "А что вы думаете об этом? Пишите в комментариях 👇",
                        "Ваше мнение? Делитесь в комментариях 👇",
                        "Согласны с этим? Ждем ваше мнение в комментариях 👇",
                        "Сталкивались с подобным? Расскажите в комментариях 👇",
                    ]
                    await asyncio.sleep(2)
                    await _vk_publisher.create_comment(vk_post_id, random.choice(comments))
            else:
                logger.warning(f"Post #{post['id']} VK crosspost failed вЂ” publish() returned None")
        except Exception as e:
            logger.error(f"VK crosspost error for post #{post['id']}: {e}", exc_info=True)
    elif _vk_publisher and not _vk_publisher.enabled:
        logger.debug("VK crosspost skipped: token or group_id not configured")

    # в”Ђв”Ђ Publish as VK Story в”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђ
    if _vk_publisher and _vk_publisher.enabled and _story_generator:
        try:
            # Extract first sentence or first 100 chars as headline for story
            raw_text = re.sub(r'<[^>]+>', '', text)  # strip HTML tags
            raw_text = re.sub(r'#\S+', '', raw_text).strip()  # strip hashtags
            # Take first sentence or first 120 chars
            first_sentence = raw_text.split('.')[0].strip() if '.' in raw_text[:150] else raw_text[:120]
            if len(first_sentence) > 15:  # only if headline is meaningful
                photo_for_story = post.get("replacement_media_url")
                story_bytes = await _story_generator.generate_news_story(
                    first_sentence, photo_url=photo_for_story
                )
                if story_bytes:
                    story_result = await _vk_publisher.upload_story_photo(story_bytes)
                    if story_result:
                        logger.info(f"Post #{post['id']}: VK Story published!")
                    else:
                        logger.warning(f"Post #{post['id']}: VK Story upload failed")
                else:
                    logger.warning(f"Post #{post['id']}: news story image generation failed")
        except Exception as e:
            logger.error(f"VK Story error for post #{post['id']}: {e}")

    # в”Ђв”Ђ Cross-post to MAX в”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђ
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


# в”Ђв”Ђ Post Processing Pipeline в”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђ

_DEFAULT_VK_COMPETITOR_KEYWORDS = [
    "РёР¶РµРІСЃРє",
    "СѓРґРјСѓСЂС‚",
    "РґС‚Рї",
    "Р¶РєС…",
    "С‚СЂР°РЅСЃРїРѕСЂС‚",
    "РґРѕСЂРѕРі",
    "С€РєРѕР»",
    "Р±РѕР»СЊРЅРёС†",
]


def _normalize_competitor_target(target: str) -> str:
    value = (target or "").strip().lower()
    value = re.sub(r"^https?://(www\.)?vk\.com/", "", value)
    if value.startswith("@"):
        value = value[1:]
    return value.strip("/")


async def _maybe_comment_competitor_post() -> None:
    """Leave a limited topical comment on competitor/community posts from group account."""
    if not (_config and _db and _vk_publisher and _vk_publisher.enabled):
        return
    if not _config.vk_competitor_commenting_enabled:
        return

    targets_raw = [x.strip() for x in _config.vk_competitor_targets if x.strip()]
    targets: list[tuple[str, str]] = []
    seen_keys = set()
    for target in targets_raw:
        key = _normalize_competitor_target(target)
        if not key or key in seen_keys:
            continue
        seen_keys.add(key)
        targets.append((target, key))

    if not targets:
        return

    now = dt.now()
    today = now.strftime("%Y-%m-%d")

    day = await _db.get_setting("vk_competitor_comment_counts_day", "")
    counts_raw = await _db.get_setting("vk_competitor_comment_counts_json", "{}")
    cursor_raw = await _db.get_setting("vk_competitor_target_cursor", "0")
    last_actions_raw = await _db.get_setting("vk_competitor_last_action_map_json", "{}")

    try:
        counts = json.loads(counts_raw or "{}")
    except (json.JSONDecodeError, TypeError):
        counts = {}
    if not isinstance(counts, dict):
        counts = {}

    normalized_counts: dict[str, int] = {}
    for key, value in counts.items():
        k = _normalize_competitor_target(key)
        if not k:
            continue
        try:
            normalized_counts[k] = max(0, int(value))
        except (ValueError, TypeError):
            normalized_counts[k] = 0
    counts = normalized_counts

    try:
        cursor = int(cursor_raw or "0")
    except ValueError:
        cursor = 0

    try:
        last_actions = json.loads(last_actions_raw or "{}")
    except (json.JSONDecodeError, TypeError):
        last_actions = {}
    if not isinstance(last_actions, dict):
        last_actions = {}

    try:
        per_target_limit = max(1, int(_config.vk_competitor_comments_per_day))
    except (ValueError, TypeError):
        per_target_limit = 1

    if day != today:
        day = today
        counts = {}
        await _db.set_setting("vk_competitor_comment_counts_day", today)
        await _db.set_setting("vk_competitor_comment_counts_json", "{}")
        await _db.set_setting("vk_competitor_comment_day", today)  # legacy key for visibility
        await _db.set_setting("vk_competitor_comment_count", "0")  # legacy key for visibility

    active_keys = {key for _, key in targets}
    for key in list(counts.keys()):
        if key not in active_keys:
            counts.pop(key, None)
    for key in list(last_actions.keys()):
        norm_key = _normalize_competitor_target(key)
        if not norm_key or norm_key not in active_keys:
            last_actions.pop(key, None)

    if all(counts.get(key, 0) >= per_target_limit for _, key in targets):
        return

    posted_raw = await _db.get_setting("vk_competitor_post_keys", "[]")
    try:
        posted_keys = set(json.loads(posted_raw or "[]"))
    except (json.JSONDecodeError, TypeError):
        posted_keys = set()

    keywords = _config.vk_competitor_keywords or _DEFAULT_VK_COMPETITOR_KEYWORDS
    target_count = len(targets)
    cursor = cursor % target_count
    ordered_targets = targets[cursor:] + targets[:cursor]
    candidate = None
    selected_key = ""
    selected_raw_target = ""
    next_cursor = (cursor + 1) % target_count

    for idx, (raw_target, key) in enumerate(ordered_targets):
        if counts.get(key, 0) >= per_target_limit:
            continue
        last_action_raw = last_actions.get(key)
        if last_action_raw:
            try:
                last_action = dt.fromisoformat(last_action_raw)
                if now - last_action < timedelta(minutes=_config.vk_competitor_min_gap_minutes):
                    continue
            except ValueError:
                pass
        one_candidate = await _vk_publisher.find_external_post_candidate(
            [raw_target],
            keywords=keywords,
            scan_limit=_config.vk_competitor_scan_limit,
            skip_post_keys=posted_keys,
        )
        if one_candidate:
            candidate = one_candidate
            selected_key = key
            selected_raw_target = raw_target
            next_cursor = (cursor + idx + 1) % target_count
            break

    await _db.set_setting("vk_competitor_target_cursor", str(next_cursor))

    if not candidate:
        return

    comment_text = _vk_publisher.build_thematic_comment(candidate["text"])
    comment_id = await _vk_publisher.create_comment(
        candidate["post_id"],
        comment_text,
        owner_id=candidate["owner_id"],
    )
    if not comment_id:
        return

    posted_keys.add(candidate["post_key"])
    posted_keys_list = list(posted_keys)[-200:]
    await _db.set_setting("vk_competitor_post_keys", json.dumps(posted_keys_list, ensure_ascii=False))
    counts[selected_key] = counts.get(selected_key, 0) + 1
    total_today = sum(counts.values())
    last_actions[selected_key] = now.isoformat(timespec="seconds")
    await _db.set_setting("vk_competitor_comment_counts_day", today)
    await _db.set_setting("vk_competitor_comment_counts_json", json.dumps(counts, ensure_ascii=False))
    await _db.set_setting("vk_competitor_last_action_map_json", json.dumps(last_actions, ensure_ascii=False))
    await _db.set_setting("vk_competitor_comment_day", today)  # legacy key for visibility
    await _db.set_setting("vk_competitor_comment_count", str(total_today))  # legacy key for visibility
    await _db.set_setting("vk_competitor_last_action_at", now.isoformat(timespec="seconds"))  # legacy key for visibility

    logger.info(
        "VK outreach comment created: target=%s post=%s target_count=%s/%s total_today=%s",
        selected_raw_target or candidate["target"],
        candidate["post_key"],
        counts[selected_key],
        per_target_limit,
        total_today,
    )

    for admin_id in _config.admin_ids:
        try:
            await _bot.send_message(
                admin_id,
                "VK outreach: РєРѕРјРјРµРЅС‚Р°СЂРёР№ РѕСЃС‚Р°РІР»РµРЅ РѕС‚ Р»РёС†Р° РіСЂСѓРїРїС‹ "
                f"({counts[selected_key]}/{per_target_limit} РґР»СЏ СЌС‚РѕРіРѕ РїР°Р±Р»РёРєР° СЃРµРіРѕРґРЅСЏ)\n"
                f"Р¦РµР»СЊ: {selected_raw_target or candidate['target']}\n"
                f"Р’СЃРµРіРѕ СЃРµРіРѕРґРЅСЏ: {total_today}\n"
                f"РџРѕСЃС‚: https://vk.com/wall{candidate['post_key']}",
            )
        except Exception:
            pass


async def process_new_post(post_id: int):
    """Full processing pipeline for a new post: rewrite + media check + send for review."""
    post = await _db.get_post(post_id)
    if not post:
        return

    logger.info(f"Processing new post #{post_id}")
    original_text = _clean_text(post["original_text"])
    text_lower = original_text.lower()

    # Step 0a: Ad filter вЂ” skip promotional posts
    # Tier 1: hard stop вЂ” 1 word is enough for blatant ads
    _HARD_AD_WORDS = [
        "Р»РёС†Рѕ Р±СЂРµРЅРґР°", "Р»РёС†Рѕ kari", "Р»РёС†Рѕ Р±СЂРµРЅРґ", "Р°РјР±Р°СЃСЃР°РґРѕСЂ",
        "СЃРїРѕРЅСЃРѕСЂ", "РїР°СЂС‚РЅС‘СЂСЃРєРёР№ РјР°С‚РµСЂРёР°Р»", "РЅР° РїСЂР°РІР°С… СЂРµРєР»Р°РјС‹",
        "РЅР° РїСЂР°РІР°С… СЃРѕС†РёР°Р»СЊРЅРѕР№", "СЃРѕС†РёР°Р»СЊРЅР°СЏ СЂРµРєР»Р°РјР°",
        "erid", "orid", "СЂРµРєР»Р°РјРѕРґР°С‚РµР»СЊ", "СЂРµРєР»Р°РјРЅС‹Р№ РїРѕСЃС‚",
        "РїРѕСЃРїРµС€РёС‚Рµ РїСЂРёРѕР±СЂРµСЃС‚Рё", "СѓСЃРїРµР№С‚Рµ РєСѓРїРёС‚СЊ", "РЅРµ СѓРїСѓСЃС‚РёС‚Рµ СЃРІРѕР№ С€Р°РЅСЃ",
        "РЅРѕРІРёРЅРєРё РєРѕР»Р»РµРєС†РёРё", "РєРѕР»Р»Р°Р±РѕСЂР°С†РёРё РїРµСЂРІРѕРіРѕ СѓСЂРѕРІРЅСЏ",
        "РїРѕРґСЂРѕР±РЅРѕСЃС‚Рё вЂ” С‡РёС‚Р°Р№С‚Рµ РІ РєР°СЂС‚РѕС‡РєР°С…",
        # Р”РѕРЅР°С‚-РїРѕСЃС‚С‹ Рё РїСЂРёР·С‹РІС‹ Рє РїРѕР¶РµСЂС‚РІРѕРІР°РЅРёСЏРј
        "СЂР°РґР°СЂ СЂР°Р±РѕС‚Р°РµС‚ Р±Р»Р°РіРѕРґР°СЂСЏ РІР°Рј", "РїРѕРґРґРµСЂР¶РёС‚Рµ Р»СЋР±РѕР№ СЃСѓРјРјРѕР№",
        "РґР°Р¶Рµ РЅРµР±РѕР»СЊС€РѕР№ РґРѕРЅР°С‚", "РјС‹ РЅРµ СЂР°Р·РјРµС‰Р°РµРј СЂРµРєР»Р°РјСѓ",
        "РїСЂРѕРµРєС‚ РґРµСЂР¶РёС‚СЃСЏ РЅР° РїРѕРґРґРµСЂР¶РєРµ", "Р·Р°РґРѕРЅР°С‚РёС‚СЊ",
        # РЎР°РјРѕСЂРµРєР»Р°РјРЅС‹Рµ РїРѕСЃС‚С‹ РёСЃС‚РѕС‡РЅРёРєР°
        "РїРѕРґРїРёСЃР°С‚СЊСЃСЏ РІ vk", "РїРѕРґРїРёСЃР°С‚СЊСЃСЏ РІ С‚Рі", "РїРѕРґРїРёСЃР°С‚СЊСЃСЏ РІ tg",
        "РїСЂРёСЃР»Р°С‚СЊ РЅРѕРІРѕСЃС‚СЊ", "telegram Р·Р°Р±Р»РѕРєРёСЂСѓСЋС‚", "С‚РµР»РµРіСЂР°Рј Р·Р°Р±Р»РѕРєРёСЂСѓСЋС‚",
        "РЅРµ СЂРµРєР»Р°РјР°!!!", "РЅРµ СЂРµРєР»Р°РјР°!", "СЌС‚Рѕ РЅРµ СЂРµРєР»Р°РјР°",
    ]
    if any(w in text_lower for w in _HARD_AD_WORDS):
        await _db.update_post_status(post_id, "rejected")
        logger.info(f"Post #{post_id} rejected: hard ad keyword matched")
        return

    # Tier 2: soft stop вЂ” 2+ generic ad words
    ad_matches = [w for w in _config.ad_stop_words if w in text_lower]
    if len(ad_matches) >= 2:  # 2+ ad stop-words = spam
        await _db.update_post_status(post_id, "rejected")
        logger.info(f"Post #{post_id} rejected: ad/spam (matched: {', '.join(ad_matches[:3])})")
        return

    # Step 0b: Topic cooldown вЂ” prevent same-topic flood
    _WEATHER_KEYWORDS = ["РїРѕРіРѕРґ", "С‚РµРјРїРµСЂР°С‚СѓСЂ", "РїСЂРѕРіРЅРѕР·", "РѕСЃР°РґРє", "РіРѕР»РѕР»РµРґ", "РјРѕСЂРѕР·"]
    text_lower_w = original_text.lower()
    if sum(1 for kw in _WEATHER_KEYWORDS if kw in text_lower_w) >= 2:
        if await _db.has_recent_topic_post(_WEATHER_KEYWORDS[:4], hours=4):
            await _db.update_post_status(post_id, "rejected")
            logger.info(f"Post #{post_id} rejected: weather cooldown (duplicate weather post in last 4h)")
            return

    # Step 0c: Relevance filter for federal channels
    source = post["source_channel"].lower()

    # Channels whose username contains these fragments are treated as local without geo-check
    _LOCAL_SOURCE_KEYWORDS = [
        "izhevsk", "izh", "udm", "СѓРґРјСѓСЂС‚", "РёР¶РµРІСЃРє", "18",
        "radar", "vrv", "izhlife", "udm18", "РёР¶life", "РёР¶18",
        "СЂР°РґР°СЂ", "РёР¶РµРІСЃРє", "СѓРґРјСѓСЂС‚РёСЏ", "РІСЏС‚РєР°", "РёР¶РЅРµС‚",
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
        # Local source: apply geo filter вЂ” reject only if text explicitly points to another region
        if has_non_local_geo and not has_geo:
            await _db.update_post_status(post_id, "rejected")
            logger.info(f"Post #{post_id} rejected: local source but non-local geo markers in text")
            return
    else:
        # Federal/non-local source: skip hard geo filter, use AI relevance check instead.
        # Hard geo filter was too strict вЂ” it blocked international/political/consumer news
        # (e.g. "РђС„РµСЂРёСЃС‚С‹ РїСЂРѕРґР°СЋС‚ Р‘РђР”С‹", "РљРёРј Р§РµРЅ Р«РЅ") that are relevant to all readers.
        if has_geo or looks_federal:
            # Fast-path: obvious local/federal relevance вЂ” skip AI call
            pass
        else:
            # Use AI to decide relevance for everything else
            is_relevant = await _rewriter.check_relevance(original_text)
            if not is_relevant:
                await _db.update_post_status(post_id, "rejected")
                logger.info(f"Post #{post_id} rejected: not relevant per AI check (federal channel @{source})")
                return

    # Step 0c: Deduplication вЂ” smart two-tier check
    # Tier 1: Compare against PUBLISHED posts (last 12h) вЂ” don't repeat what's already on the channel
    published_texts = await _db.get_texts_by_status(["published"], hours=12)
    published_match = _find_similar_match(original_text, published_texts)
    if published_match:
        await _db.update_post_status(post_id, "rejected")
        logger.info(
            f"Post #{post_id} rejected: similar to published post "
            f"(similarity={published_match['similarity']:.2f}, overlap={published_match['overlap']:.2f})"
        )
        return

    # Tier 2: Compare against QUEUED posts (pending/rewriting/approved) вЂ” first-in-queue wins, later duplicates rejected
    queued_texts = await _db.get_texts_by_status(["pending", "rewriting", "approved"], hours=12)
    queued_match = _find_similar_match(original_text, queued_texts, queued=True)
    if queued_match:
        await _db.update_post_status(post_id, "rejected")
        logger.info(
            f"Post #{post_id} rejected: similar post already in queue "
            f"(similarity={queued_match['similarity']:.2f}, overlap={queued_match['overlap']:.2f})"
        )
        return

    # Step 0d: Breaking news detection вЂ” auto-publish without moderation.
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

        # Guard: if AI returned a refusal message вЂ” reject post immediately
        if _rewriter._is_refusal(rewritten):
            await _db.update_post_status(post_id, "rejected")
            logger.warning(f"Post #{post_id} rejected: AI refusal detected in rewritten text")
            return

        # в”Ђв”Ђ Р¤РёР»СЊС‚СЂ С‡СѓРІСЃС‚РІРёС‚РµР»СЊРЅРѕРіРѕ РєРѕРЅС‚РµРЅС‚Р° в”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђ
        _cf_result = filter_sensitive_content(rewritten)
        if _cf_result.action == FilterAction.BLOCK:
            await _db.update_post_status(post_id, "rejected")
            logger.warning(
                f"Post #{post_id} BLOCKED by content_filter (pipeline): {_cf_result.reason}"
            )
            return
        rewritten = _cf_result.text  # Р’РѕР·РјРѕР¶РЅРѕ Р·Р°РјРµРЅРµРЅС‹ СЌРІС„РµРјРёР·РјС‹ / РґРѕР±Р°РІР»РµРЅ РґРёСЃРєР»РµР№РјРµСЂ
        # в”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђ

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

    # Step 3b: Smart photo strategy (no Gemini call вЂ” saves quota)
    #
    # Priority:
    #   1. Post has original photo WITHOUT watermark в†’ use it as-is, skip stock search
    #   2. Post has original photo WITH watermark в†’ replace with stock
    #   3. Post has no photo в†’ search stock only if keywords are highly specific
    #   4. No suitable photo found в†’ publish text-only (better than generic stock)
    try:
        stock_url = None
        has_original_clean = (
            bool(_config.use_source_media)
            and post["media_type"] == "photo"
            and post.get("media_local_path")
            and not post.get("has_watermark")
        )

        if post["media_type"] == "photo" and post.get("media_local_path") and _config.use_source_media:
            # Best-effort watermark detection on source image (in addition to DB flag).
            detected, confidence = _media_processor.detect_watermark(post["media_local_path"])
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
            # Need stock: either source disabled, watermark replacement, or no photo at all
            keywords = _ai_photo_keywords or _rewriter._extract_keywords_fallback(original_text)

            if keywords and len(keywords) >= 2:
                stock_photos = await _media_processor.search_stock_photo(keywords, count=3)

                if stock_photos:
                    # Take FIRST result вЂ” Pexels/Wikimedia already sorts by relevance.
                    # kw_match is used only for logging вЂ” NOT as a barrier.
                    # Wikimedia often has empty descriptions, causing false "mismatch".
                    best = stock_photos[0]
                    desc = best.get("description", "")
                    kw_match = any(kw.lower() in desc.lower() for kw in keywords[:3]) if desc else False
                    stock_url = best["url"]
                    logger.info(
                        f"Post #{post_id}: stock photo selected from {best.get('source', 'unknown')} "
                        f"(kw_match={kw_match}, keywords={keywords[:3]})"
                    )
                else:
                    logger.info(f"Post #{post_id}: no stock photos found вЂ” publishing text-only")
            else:
                logger.info(f"Post #{post_id}: insufficient keywords for stock search вЂ” text-only")

        if stock_url:
            await _db.update_post_media(post_id, replacement_url=stock_url)
    except Exception as e:
        logger.error(f"Post #{post_id}: stock photo search failed: {e}")

    # Step 4: Breaking news в†’ auto-publish without moderation; regular в†’ auto-approve for queue
    if is_breaking:
        logger.info(f"вљЎ Post #{post_id} is BREAKING NEWS вЂ” auto-publishing!")
        updated_post = await _db.get_post(post_id)
        await _db.update_post_status(post_id, "approved")
        success = await _publish_post(updated_post)
        if success:
            for admin_id in _config.admin_ids:
                try:
                    await _bot.send_message(
                        admin_id,
                        f"вљЎ <b>РЎР РћР§РќРђРЇ РќРћР’РћРЎРўР¬</b> Р°РІС‚РѕРјР°С‚РёС‡РµСЃРєРё РѕРїСѓР±Р»РёРєРѕРІР°РЅР°!\n\n"
                        f"РџРѕСЃС‚ #{post_id} РёР· @{post['source_channel']}",
                        parse_mode=ParseMode.HTML,
                    )
                except Exception:
                    pass
        return  # Breaking news processing complete, skip regular queue flow

    # Regular post вЂ” auto-approve and add to publish queue
    logger.info(f"Post #{post_id} auto-approved вЂ” will be published on next interval")
    await _db.update_post_status(post_id, "approved")

    # Notify admins (optional, informational only вЂ” no action needed)
    approved_count = await _db.get_approved_posts()
    for admin_id in _config.admin_ids:
        try:
            await _bot.send_message(
                admin_id,
                f"вњ… РџРѕСЃС‚ #{post_id} РґРѕР±Р°РІР»РµРЅ РІ РѕС‡РµСЂРµРґСЊ РїСѓР±Р»РёРєР°С†РёРё.\n"
                f"рџ“‹ Р’ РѕС‡РµСЂРµРґРё: {len(approved_count)} РїРѕСЃС‚РѕРІ\n"
                f"вЏ° РЎР»РµРґСѓСЋС‰РёР№ РІС‹С…РѕРґ вЂ” С‡РµСЂРµР· ~{_config.publish_interval // 60} РјРёРЅ",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass


# в”Ђв”Ђ Auto-Publish Scheduler в”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђ

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

            try:
                await _maybe_comment_competitor_post()
            except Exception as outreach_err:
                logger.warning(f"VK outreach comment failed: {outreach_err}")

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
                            f"рџ“ў РђРІС‚Рѕ-РїСѓР±Р»РёРєР°С†РёСЏ: РѕРїСѓР±Р»РёРєРѕРІР°РЅ РїРѕСЃС‚ #{post['id']}.\n"
                            f"рџ“‹ Р’ РѕС‡РµСЂРµРґРё РѕСЃС‚Р°Р»РѕСЃСЊ: {remaining}",
                        )
                    except Exception:
                        pass

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Auto-publish error: {e}")
            await asyncio.sleep(60)


# в”Ђв”Ђ Bot Initialization в”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђ

def create_bot(config: Config, db: Database, rewriter: AIRewriter, media_proc: MediaProcessor, vk_pub: Optional[VKPublisher] = None) -> tuple:
    """Create and configure the bot. Returns (bot, dispatcher)."""
    global _config, _db, _rewriter, _media_processor, _vk_publisher, _story_generator, _bot

    _config = config
    _db = db
    _rewriter = rewriter
    _media_processor = media_proc
    _vk_publisher = vk_pub
    _story_generator = StoryGenerator()

    bot = Bot(token=config.bot_token)
    _bot = bot

    dp = Dispatcher()
    dp.include_router(router)

    return bot, dp
