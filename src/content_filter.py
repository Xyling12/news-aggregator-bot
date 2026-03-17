"""
content_filter.py — Фильтр чувствительного контента для новостного бота.

Цель: предотвратить публикацию материалов, нарушающих правила Telegram/VK
и требования ВОЗ по освещению тем суицида и самоповреждений.

Логика (3 уровня):
  1. BLOCK    — пост блокируется полностью (детальное описание способа и т.п.)
  2. REWRITE  — ключевые слова заменяются эвфемизмами
  3. DISCLAIMER — добавляется дисклеймер с телефоном доверия

Порядок применения: BLOCK → REWRITE → DISCLAIMER.
"""

import logging
import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


# ── Уровни реакции ────────────────────────────────────────────────────────────

class FilterAction(Enum):
    PASS = "pass"          # Публиковать как есть
    REWRITE = "rewrite"    # Заменить слова эвфемизмами
    DISCLAIMER = "disclaimer"  # Добавить дисклеймер
    BLOCK = "block"        # Полностью заблокировать


@dataclass
class FilterResult:
    action: FilterAction
    text: str                      # Итоговый текст (отфильтрованный или оригинал)
    reason: Optional[str] = None   # Причина блокировки / замены (для лога)


# ── 1. БЛОКИРОВКА: детальное описание способа, пропаганда ────────────────────
# Паттерны, при которых пост не публикуется вообще.

_BLOCK_PATTERNS = [
    # Конкретные способы суицида
    re.compile(r'\b(повес[иилтялась]+(?:ся|сь)?)\b', re.IGNORECASE),
    re.compile(r'\b(застрел[иилтялась]+(?:ся|сь)?)\b', re.IGNORECASE),
    re.compile(r'\b(прыгнул?\s+[сс]\s+(моста|крыши|здания|окна|путепровода))\b', re.IGNORECASE),
    re.compile(r'\b(вскрыл?\s+[вв]ены?)\b', re.IGNORECASE),
    re.compile(r'\b(выпрыгнул?\s+из\s+окна)\b', re.IGNORECASE),
    re.compile(r'\b(приня[лт]а?\s+яд|отравил[аи]?ся\s+таблетка)\b', re.IGNORECASE),
    re.compile(r'\b(способ\s+(?:совершить|уйти|ухода)\s+из\s+жизни)\b', re.IGNORECASE),
    re.compile(r'\b(как\s+(?:легко|быстро|лучше)\s+(?:умереть|покончить))\b', re.IGNORECASE),
    # Прямая пропаганда
    re.compile(r'\b(суицид\s+это\s+(выход|решение|освобождение))\b', re.IGNORECASE),
    re.compile(r'\b(лучше\s+умереть\s+чем)\b', re.IGNORECASE),
    re.compile(r'\b(жизнь\s+не\s+стоит\s+того)\b', re.IGNORECASE),
]

# ── 2. ЗАМЕНА: мягкие словарные синонимы ──────────────────────────────────────
# Заменяем слова по журналистским стандартам ВОЗ ("Safe messaging").

_REPLACEMENTS = [
    # суицид, суицид*
    (re.compile(r'\bсуицид\w*\b', re.IGNORECASE), 'уход из жизни'),
    # самоубийств*
    (re.compile(r'\bсамоубийств\w*\b', re.IGNORECASE), 'добровольный уход из жизни'),
    # покончил(а) с собой / покончить с собой
    (re.compile(r'\bпокончил[аи]?\s+с\s+соб[ойя]\b', re.IGNORECASE), 'ушёл из жизни'),
    (re.compile(r'\bпокончить\s+с\s+соб[ойя]\b', re.IGNORECASE), 'уйти из жизни'),
    # наложил(а) на себя руки
    (re.compile(r'\bналожи[лт][аи]?\s+на\s+себя\s+руки\b', re.IGNORECASE), 'ушёл из жизни'),
    # попытка суицида / попытка самоубийства
    (re.compile(r'\bпопытк[ауи]\s+(суицид\w*|самоубийств\w*)\b', re.IGNORECASE), 'попытка ухода из жизни'),
    # добровольный уход (уже нейтральное — оставляем, но нормализуем регистр)
    # лишил(а) себя жизни
    (re.compile(r'\bлиши[лт][аи]?\s+себя\s+жизни\b', re.IGNORECASE), 'ушёл из жизни'),
    # умер / погиб при невыясненных обстоятельствах — НЕ трогаем, нейтрально
]

# ── 3. ДИСКЛЕЙМЕР: если осталось мягкое упоминание темы ─────────────────────

_DISCLAIMER_TRIGGER = re.compile(
    r'\b(уход\s+из\s+жизни|добровольный\s+уход|психическ\w+|депресс\w+|'
    r'кризис\w*|психолог\w*|телефон\s+доверия)\b',
    re.IGNORECASE,
)

_DISCLAIMER_TEXT = (
    "\n\n⚠️ <b>Нужна помощь?</b> "
    "<a href=\"https://telefon-doveria.ru\">Телефон доверия: 8-800-2000-122</a> (бесплатно, круглосуточно)"
)


# ── Публичный API ─────────────────────────────────────────────────────────────

def filter_sensitive_content(text: str, add_disclaimer: bool = True) -> FilterResult:
    """Пропустить текст через трёхуровневый фильтр чувствительного контента.

    Args:
        text: Текст поста (до или после AI-переписывания).
        add_disclaimer: Добавлять ли телефон доверия при мягком упоминании темы.

    Returns:
        FilterResult с итоговым текстом и рекомендованным действием.
    """
    text_lower = text.lower()

    # ── Уровень 1: Блокировка ──────────────────────────────────────────────
    for pat in _BLOCK_PATTERNS:
        m = pat.search(text)
        if m:
            reason = f"Блокировка: найден паттерн «{m.group()}»"
            logger.warning("content_filter BLOCK | %s | snippet: %.120s", reason, text[:120])
            return FilterResult(action=FilterAction.BLOCK, text=text, reason=reason)

    # ── Уровень 2: Замена эвфемизмами ──────────────────────────────────────
    replaced = text
    replacements_made = []
    for pat, replacement in _REPLACEMENTS:
        new_text, count = pat.subn(replacement, replaced)
        if count:
            replacements_made.append(f"{pat.pattern!r} → '{replacement}' (×{count})")
            replaced = new_text

    if replacements_made:
        reason = "; ".join(replacements_made)
        logger.info("content_filter REWRITE | %s", reason)
        text = replaced
        action = FilterAction.REWRITE
    else:
        action = FilterAction.PASS

    # ── Уровень 3: Дисклеймер ──────────────────────────────────────────────
    if add_disclaimer and _DISCLAIMER_TRIGGER.search(text):
        # Не дублируем, если дисклеймер уже есть
        if "Телефон доверия" not in text and "8-800-2000" not in text:
            logger.info("content_filter DISCLAIMER added")
            text = text + _DISCLAIMER_TEXT
            action = FilterAction.DISCLAIMER

    return FilterResult(action=action, text=text, reason=None if action == FilterAction.PASS else action.value)


def is_blocked(text: str) -> bool:
    """Быстрая проверка — заблокировать ли пост без изменения текста."""
    for pat in _BLOCK_PATTERNS:
        if pat.search(text):
            return True
    return False
