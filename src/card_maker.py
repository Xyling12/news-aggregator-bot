"""
Branded headline-card generator.

When a local news post has no usable source photo, generating a clean branded card
(category colour + headline + «Ижевск Сегодня») beats a random foreign-city stock
photo: it's always on-topic and on-brand, and never shows Moscow/Italy/Spain.
"""
import os
import re
from PIL import Image, ImageDraw, ImageFont

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ASSETS = os.path.join(_HERE, "assets")
_FONT_BOLD = os.path.join(_ASSETS, "Roboto-Bold.ttf")

W, H = 1280, 854

# Roboto has no colour-emoji glyphs — strip emoji so the card has no tofu boxes
_EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF☀-➿←-⇿⬀-⯿️‍⃣]",
    flags=re.UNICODE,
)


def _clean_title(text: str) -> str:
    """Take the headline (first line), strip HTML/emoji/hashtags, trim length."""
    first = (text or "").strip().split("\n")[0]
    first = re.sub(r"<[^>]+>", "", first)
    first = re.sub(r"#\S+", "", first)
    first = _EMOJI.sub("", first)
    first = re.sub(r"\s+", " ", first).strip(" -—:·")
    LIMIT = 120
    if len(first) <= LIMIT:
        return first
    # Prefer ending on the first full sentence; otherwise cut on a word boundary.
    cut = first[:LIMIT]
    m = re.search(r"^(.{40,}?[.!?])\s", cut)
    if m:
        return m.group(1).strip()
    return cut.rsplit(" ", 1)[0].rstrip(" ,.;:—-") + "…"


def _wrap(draw, text, font, max_w):
    words = text.split()
    lines, cur = [], ""
    for w in words:
        test = (cur + " " + w).strip()
        if draw.textlength(test, font=font) <= max_w:
            cur = test
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    """Load the branded bold font, falling back to PIL's built-in font if the
    TTF asset is missing (e.g. incomplete deploy) so card rendering never crashes."""
    try:
        return ImageFont.truetype(_FONT_BOLD, size)
    except OSError:
        return ImageFont.load_default(size=size)


def make_news_card(title: str, category_label: str, bg_color, out_path: str) -> str:
    """Render a branded headline card to out_path and return the path."""
    img = Image.new("RGB", (W, H), tuple(bg_color))
    d = ImageDraw.Draw(img)
    cx = W // 2

    # Category label (top-left, uppercase) with an underline accent
    f_cat = _load_font(46)
    label = (category_label or "Новости").upper()
    d.text((90, 72), label, font=f_cat, fill=(255, 255, 255))
    lw = d.textlength(label, font=f_cat)
    d.rectangle((90, 138, 90 + lw, 144), fill=(255, 255, 255))

    # Headline — centered, wrapped, size depends on length
    title = _clean_title(title) or "Новости Ижевска"
    size = 80 if len(title) < 70 else (66 if len(title) < 110 else 56)
    f_title = _load_font(size)
    lines = _wrap(d, title, f_title, W - 180)[:6]
    line_h = size + 18
    total_h = len(lines) * line_h
    y = max(190, (H - total_h) // 2)
    for ln in lines:
        tw = d.textlength(ln, font=f_title)
        d.text((cx - tw / 2, y), ln, font=f_title, fill=(255, 255, 255))
        y += line_h

    # Footer brand
    f_foot = _load_font(40)
    foot = "ИЖЕВСК СЕГОДНЯ"
    fw = d.textlength(foot, font=f_foot)
    d.text((cx - fw / 2, H - 96), foot, font=f_foot, fill=(255, 255, 255))

    img.save(out_path, quality=90)
    return out_path
