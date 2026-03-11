"""
utils.py — Shared utilities, text-processing helpers, and content constants.

Centralises logic that is used across bot.py and other modules so each
function/constant is defined in exactly one place.
"""

import html
import re
from typing import Optional


# ── HTML Escaping ─────────────────────────────────────────────────────────────

def escape_html(text: str) -> str:
    """Escape HTML special characters using the standard library.

    Uses html.escape with quote=False so that double-quotes inside
    Telegram message text are left as-is (Telegram HTML parser expects
    attribute values to be inside tags, not in plain text).
    """
    return html.escape(text, quote=False)


# ── Text Cleaning ─────────────────────────────────────────────────────────────

# ── Known media brands (case-insensitive, matched as standalone words) ────────
# Any new brand can be appended here — no other changes needed.
# Literal brand names (escaped verbatim)
_MEDIA_BRANDS_LITERAL = [
    "сусанин", "susanin",
    "удм-инфо", "udm-info", "udminfo",
    "ижевск онлайн", "izhevsk.ru",
    "ижлайф", "izh.life",
    "сусанин медиа",
    "информ удмуртия",
    "мк удмуртия", "аиф удмуртия",
    "коммерсант ижевск",
    "провэд", "глазов.ру", "сарапул.ру",
    "рустем", "udmpravda", "udm-pravda",
]

# Compile: literal brands OR «ИА <Слово>» pattern
_BRAND_RE = re.compile(
    r'(?:' + r'|'.join(re.escape(b) for b in _MEDIA_BRANDS_LITERAL) + r'|\bиа\s+\w+)',
    re.IGNORECASE,
)

# Generic inline patterns: "читайте в материале X", "по данным X", etc.
# These are applied to the whole line text (not anchored to line start).
_INLINE_STRIP_PATTERNS = [
    # "читайте в новом материале Имя" / "читайте на сайте Имя"
    re.compile(r'\bчитайте\s+(?:в\s+)?(?:новом\s+)?(?:материале?\s+)?[\w\s"]{2,35}\.?', re.IGNORECASE),
    # "подробнее в материале Имя" / "подробнее на сайте"
    re.compile(r'\bподробнее\s+(?:в\s+материале?|на\s+сайте)[^.\n]*\.?', re.IGNORECASE),
    # "материал подготовлен / опубликован / предоставлен X"
    re.compile(r'\bматериал\s+(?:подготовлен|опубликован|предоставлен)[^.\n]*\.?', re.IGNORECASE),
    # "по информации / по данным / по сообщению Издание"
    re.compile(r'\bпо\s+(?:информации|данным|сообщению)\s+[\w\s"]{2,35}\.?', re.IGNORECASE),
    # "источник: Что-то" inline (not caught by line-level skip)
    re.compile(r'\bисточник\s*:\s*[^.\n]+\.?', re.IGNORECASE),
]

_SKIP_PATTERNS = [
    r'подписаться на',
    r'подписывайтесь',
    r'подписаться\s*[|:]',
    r'подписаться$',
    r'повестка дня.*на сайте',
    r'читайте.*на сайте',
    r'читайте нас',
    r'читайте.*в\s*(max|макс|vk|вк|дзен)',
    r'источник:',
    r'источник фото\s*:',
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
    r'^\s*https?://',       # standalone URLs
    r'^\s*t\.me/',
    r'^\s*@\w+\s*$',        # standalone @mentions
    r'^\s*[📲😊📩📢🔔💬]\s*(подписа|присла|читай|наш)',  # emoji CTA lines
    # Photo attribution lines from any source channel
    r'^\s*фото\s*[:：]',                   # Фото: ИА Сусанин / Фото: любой источник
    r'^\s*фото\s+[а-яёa-z©]',             # Фото ИА Сусанин (без двоеточия)
    r'^\s*©\s*\w',                         # © ИА Сусанин
    r'^\s*изображени[ея]\s*[:：]',         # Изображение: ...
    r'^\s*на\s*фото\s*[:：]',              # На фото: ...
    r'^\s*фотограф\s*[:：]',              # Фотограф: ...
    r'^\s*фото\s*и\s*видео\s*[:：]',      # Фото и видео: ...
    r'^\s*автор\s*фото\s*[:：]',          # Автор фото: ...
    r'^\s*photo\s*[:：]',                  # Photo: (английский вариант)
]


def _strip_inline_brands(line: str) -> str:
    """Remove inline brand mentions and generic 'read more at X' phrases."""
    # Remove specific known brand names
    line = _BRAND_RE.sub('', line)
    # Remove generic publisher reference patterns
    for pat in _INLINE_STRIP_PATTERNS:
        line = pat.sub('', line)
    # Tidy up: collapse multiple spaces and strip dangling punctuation
    line = re.sub(r'[ \t]{2,}', ' ', line)
    line = re.sub(r'(?<=[а-яёa-z])\s*[—–-]\s*$', '', line.strip())
    return line.strip()


def clean_text(text: str) -> str:
    """Remove source attribution lines, subscribe links, external URLs,
    and inline mentions of publisher brands / 'read more at X' phrases.

    Strips promotional/CTA lines that are injected by source channels so
    that the AI rewriter and deduplication logic work on pure content.
    """
    lines = text.split('\n')
    cleaned = []
    for line in lines:
        line_lower = line.strip().lower()
        if not line_lower:
            cleaned.append(line)
            continue
        skip = any(re.search(pat, line_lower) for pat in _SKIP_PATTERNS)
        if skip:
            continue
        # Inline brand/publisher removal
        line = _strip_inline_brands(line)
        if line:  # Don't add blank lines created by stripping
            cleaned.append(line)
    return '\n'.join(cleaned).rstrip()


# ── Similarity / Deduplication ────────────────────────────────────────────────

def word_overlap(text1: str, text2: str) -> float:
    """Return Jaccard similarity between two texts (0.0–1.0).

    Uses intersection / union (Jaccard) instead of intersection / min so that
    short posts don't get unfairly penalised. Only words longer than 4
    characters are considered to filter out stop-words.
    """
    words1 = {w for w in re.findall(r'[а-яёa-z0-9]+', text1.lower()) if len(w) > 4}
    words2 = {w for w in re.findall(r'[а-яёa-z0-9]+', text2.lower()) if len(w) > 4}
    if not words1 or not words2:
        return 0.0
    union = words1 | words2
    if not union:
        return 0.0
    return len(words1 & words2) / len(union)


def find_similar_candidate(
    text: str,
    candidates: list,
    rewriter,
    *,
    similarity_threshold: float = 0.88,
    overlap_threshold: float = 0.72,
    require_both: bool = False,
    hard_similarity_threshold: float = 0.96,
    hard_overlap_threshold: float = 0.86,
):
    """Return details for the first candidate considered a duplicate."""
    for existing in candidates:
        if not existing or existing == text:
            continue
        similarity = 1.0 - rewriter.calculate_uniqueness(text, existing)
        overlap = word_overlap(text, existing)

        hard_match = similarity > hard_similarity_threshold or overlap > hard_overlap_threshold
        soft_match = (
            similarity > similarity_threshold and overlap > overlap_threshold
            if require_both
            else similarity > similarity_threshold or overlap > overlap_threshold
        )
        if hard_match or soft_match:
            return {
                "text": existing,
                "similarity": round(similarity, 3),
                "overlap": round(overlap, 3),
            }
    return None


def is_similar_to_any(text: str, candidates: list, rewriter) -> bool:
    """Return True if *text* is too similar to any text in *candidates*.

    Two-tier check:
      1. Cosine-like uniqueness via AIRewriter.calculate_uniqueness (>0.88 similarity).
      2. Jaccard word-overlap ratio (>0.72).

    Thresholds are calibrated for short regional news posts. Short posts about
    the same event from different sources should NOT be blocked — only
    near-identical copy-paste duplicates should be caught.

    The *rewriter* argument is the AIRewriter instance (passed in to avoid
    a circular import between utils ↔ ai_rewriter).
    """
    return find_similar_candidate(text, candidates, rewriter) is not None


# ── Rubric Detection ──────────────────────────────────────────────────────────

#: Ordered list of (label, hashtag, keyword_list) tuples used to identify
#: which content category best fits a given post.  Order matters — more
#: specific rubrics should come before broader ones.
RUBRIC_MAP = [
    ("⚡ Срочно",        "#срочно",       ["срочно", "молния", "только что"]),
    ("🔴 Происшествия", "#происшествия", [
        "пожар", "огонь", "горит", "авария", "дтп", "столкновение",
        "взрыв", "чп", "погиб", "гибель", "задержан",
        "арестован", "ограбление", "кража", "преступление",
    ]),
    ("🚗 Транспорт",    "#транспорт",    [
        "дорог", "маршрут", "автобус", "трамвай",
        "пробки", "светофор", "остановк", "транспорт", "коллапс",
    ]),
    ("🏗 ЖКХ",          "#жкх",          [
        "жкх", "коммунальн", "отопление", "водоснабжени",
        "электричество", "канализаци", "управляющая компани",
        "горячая вода", "отключен",
    ]),
    ("💰 Экономика",    "#экономика",    [
        "цены", "инфляци", "зарплат", "налог", "бизнес",
        "банк", "кредит", "ипотек", "рубл", "тариф",
    ]),
    ("🏛 Власть",       "#власть",       [
        "глава", "мэр", "губернатор", "бречалов", "дума",
        "закон", "постановлени", "администраци", "правительств",
    ]),
    ("🌡 Погода",       "#погода",       [
        "погода", "мороз", "снег", "дождь", "гроза",
        "метель", "оттепель", "похолодани", "потеплени",
    ]),
    ("⚽ Спорт",        "#спорт",        [
        "матч", "чемпионат", "гол", "турнир",
        "соревновани", "стадион", "спортсмен", "спорткомп",
    ]),
]

#: Keywords that trigger immediate auto-publication (breaking news flow).
BREAKING_KEYWORDS = [
    "срочно", "молния", "только что", "пожар", "горит", "взрыв",
    "чп", "чрезвычайн", "стрельба", "теракт", "жертв", "погиб", "обрушени",
    # Drone / air defense alerts (critical for Udmurtia residents — Radar BPV posts)
    "бпла", "беспилотн", "дрон", "воздушная тревога", "воздушн тревог",
    "опасность по бпла", "угроза бпла", "сигнал бпла", "отбой тревог",
    "ракетная опасность", "ракетная тревога", "объявлена тревога",
    "введён режим", "режим повышенной", "эвакуация",
]


def detect_rubric(text: str):
    """Return (rubric_label, rubric_hashtag) for a post text, or (None, None).

    Only scans the post body — lines that start with '#' (hashtag lines)
    are excluded to prevent words like 'погода' inside a hashtag from
    triggering a false rubric match.
    """
    # Strip hashtag lines before scanning to avoid self-referential matches
    body_lines = [
        line for line in text.split("\n")
        if not line.strip().startswith("#")
    ]
    text_lower = " ".join(body_lines).lower()
    for label, hashtag, keywords in RUBRIC_MAP:
        if any(kw in text_lower for kw in keywords):
            return label, hashtag
    return None, None


# ── Post Formatting ───────────────────────────────────────────────────────────

def format_post(text: str, hashtags: list) -> str:
    """Format a post with rubric label, body text, hashtag footer and CTA links.

    Converts **bold** markdown to <b>HTML</b> and strips leftover # headers
    before assembling the final Telegram-HTML message.
    """
    # Convert **bold** markdown → <b>bold</b> HTML
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    # Remove leftover markdown headers
    text = re.sub(r'^#{1,3}\s*', '', text, flags=re.MULTILINE)

    lines = text.strip().split("\n")
    if not lines:
        return text

    rubric_label, rubric_hashtag = detect_rubric(text)
    parts = []

    # Title (first line from AI output — already has emoji + topic)
    parts.append(f"<b>{lines[0].strip()}</b>")
    parts.append("")

    # Body
    body = "\n".join(lines[1:]).strip()
    if body:
        parts.append(body)
        parts.append("")

    # Hashtags: only rubric tag + fixed city tags (no noisy AI tags)
    all_tags = []
    if rubric_hashtag:
        all_tags.append(rubric_hashtag)
    for city_tag in ["#Ижевск", "#Удмуртия", "#ИжевскСегодня"]:
        if city_tag not in all_tags:
            all_tags.append(city_tag)
    if all_tags:
        parts.append(" ".join(all_tags))
        parts.append("")

    # Footer CTA
    parts.append(
        '😊 <a href="https://t.me/IzhevskTodayNews">Подписаться в TG</a>'
        ' | 📱 <a href="https://vk.com/club236380336">Подписаться в ВК</a>'
        ' | 📩 <a href="https://t.me/NewsRussain11_bot">Прислать новость</a>'
    )

    return "\n".join(parts)
