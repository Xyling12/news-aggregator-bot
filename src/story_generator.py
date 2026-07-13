"""
Story Generator — Creates 9:16 vertical images and videos for VK Stories.
"""

import os
import io
import re
import logging
import asyncio
import aiohttp
from PIL import Image, ImageDraw, ImageFont, ImageFilter
from typing import Optional

logger = logging.getLogger(__name__)

# ── Emoji removal regex ──────────────────────────────────────────────────
# Covers all common emoji Unicode ranges. Roboto font doesn't have emoji
# glyphs, so they render as □ boxes in PIL.
_EMOJI_RE = re.compile(
    "["
    "\U0001F600-\U0001F64F"  # emoticons
    "\U0001F300-\U0001F5FF"  # misc symbols & pictographs
    "\U0001F680-\U0001F6FF"  # transport & map
    "\U0001F1E0-\U0001F1FF"  # flags
    "\U0001F900-\U0001F9FF"  # supplemental symbols
    "\U0001FA00-\U0001FA6F"  # chess, extended-A
    "\U0001FA70-\U0001FAFF"  # extended-B
    "\U00002702-\U000027B0"  # dingbats
    "\U000024C2-\U0001F251"  # enclosed chars
    "\U0000FE0F"             # variation selector-16
    "\U0000200D"             # ZWJ
    "\U00002600-\U000026FF"  # misc symbols (☀, ⛅, etc.)
    "\U00002B50-\U00002B55"  # stars
    "\U000020E3"             # combining enclosing keycap (for 1️⃣ etc.)
    "\U000000A9"             # ©
    "\U000000AE"             # ®
    "\U0000203C-\U00003299"  # misc symbols & CJK (‼, ™, ℹ, etc.)
    "\U00010000-\U0001FFFF"  # catch-all supplemental planes
    "]+",
    flags=re.UNICODE,
)


def _strip_emoji(text: str) -> str:
    """Remove emoji characters that Roboto can't render."""
    return _EMOJI_RE.sub("", text).strip()

ASSETS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "assets")
FONT_REGULAR = os.path.join(ASSETS_DIR, "Roboto-Regular.ttf")
FONT_BOLD = os.path.join(ASSETS_DIR, "Roboto-Bold.ttf")

# ── Per-rubric visual themes ─────────────────────────────────────────────
RUBRIC_THEMES = {
    "fact": {
        "grad_top": (15, 20, 120), "grad_mid": (90, 20, 160), "grad_bot": (140, 15, 120),
        "accent": (255, 214, 0), "orb_base": (100, 50, 220),
        "header": "А ЗНАЕТЕ ЛИ ВЫ?", "bar_style": "lines",
    },
    "weather": {
        "grad_top": (15, 50, 130), "grad_mid": (30, 100, 180), "grad_bot": (20, 70, 150),
        "accent": (100, 220, 255), "orb_base": (50, 120, 220),
        "header": "ПОГОДА СЕГОДНЯ", "bar_style": "lines",
    },
    "recipe": {
        "grad_top": (120, 50, 10), "grad_mid": (160, 80, 20), "grad_bot": (100, 40, 15),
        "accent": (255, 180, 50), "orb_base": (200, 100, 30),
        "header": "РЕЦЕПТ ДНЯ", "bar_style": "lines",
    },
    "history": {
        "grad_top": (60, 40, 25), "grad_mid": (90, 60, 35), "grad_bot": (50, 30, 20),
        "accent": (220, 190, 130), "orb_base": (140, 100, 50),
        "header": "ИСТОРИЯ ИЖЕВСКА", "bar_style": "lines",
    },
    "lifehack": {
        "grad_top": (10, 80, 60), "grad_mid": (20, 130, 100), "grad_bot": (15, 90, 80),
        "accent": (50, 255, 180), "orb_base": (30, 180, 120),
        "header": "ПОЛЕЗНОЕ", "bar_style": "lines",
    },
    "place": {
        "grad_top": (15, 70, 50), "grad_mid": (25, 120, 80), "grad_bot": (20, 80, 60),
        "accent": (80, 230, 150), "orb_base": (40, 160, 100),
        "header": "МЕСТА УДМУРТИИ", "bar_style": "lines",
    },
    "news": {
        "grad_top": (140, 15, 20), "grad_mid": (180, 40, 15), "grad_bot": (120, 20, 30),
        "accent": (255, 80, 50), "orb_base": (220, 60, 30),
        "header": "НОВОСТИ", "bar_style": "bars",
    },
    "evening": {
        "grad_top": (10, 10, 50), "grad_mid": (25, 15, 80), "grad_bot": (15, 10, 60),
        "accent": (180, 150, 255), "orb_base": (80, 50, 180),
        "header": "ВЕЧЕРНИЙ ИЖЕВСК", "bar_style": "lines",
    },
    "digest": {
        "grad_top": (20, 30, 80), "grad_mid": (40, 50, 120), "grad_bot": (25, 35, 90),
        "accent": (100, 180, 255), "orb_base": (50, 100, 200),
        "header": "ГЛАВНОЕ ЗА ДЕНЬ", "bar_style": "bars",
    },
    "five_facts": {
        "grad_top": (80, 15, 100), "grad_mid": (120, 25, 140), "grad_bot": (90, 20, 110),
        "accent": (255, 150, 255), "orb_base": (160, 50, 180),
        "header": "5 ФАКТОВ", "bar_style": "lines",
    },
    "holiday": {
        "grad_top": (100, 20, 60), "grad_mid": (150, 30, 80), "grad_bot": (110, 25, 70),
        "accent": (255, 100, 150), "orb_base": (200, 50, 100),
        "header": "ПРАЗДНИК", "bar_style": "lines",
    },
}

class StoryGenerator:
    """Generates media for VK Stories."""

    def __init__(self):
        # Ensure fonts exist
        if not os.path.exists(FONT_REGULAR) or not os.path.exists(FONT_BOLD):
            logger.warning("Fonts not found in assets. Text rendering may fail or look bad if default fonts are used.")
            self.font_reg_path = "arial.ttf"  # Fallback
            self.font_bold_path = "arial.ttf" # Fallback
        else:
            self.font_reg_path = FONT_REGULAR
            self.font_bold_path = FONT_BOLD

    async def _download_image(self, url: str) -> Optional[Image.Image]:
        """Download an image from a URL into a PIL Image."""
        try:
            headers = {"User-Agent": "IzhevskTodayNewsBot/1.0 (VK Stories Renderer)"}
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status == 200:
                        data = await resp.read()
                        img = Image.open(io.BytesIO(data))
                        if img.mode != "RGB":
                            img = img.convert("RGB")
                        return img
        except Exception as e:
            logger.error(f"Failed to download story background {url}: {e}")
        return None

    def _crop_and_resize(self, img: Image.Image, target_w: int = 1080, target_h: int = 1920) -> Image.Image:
        """Crop and resize image to exactly 1080x1920 (9:16)."""
        img_w, img_h = img.size
        # Calculate target aspect ratio
        target_ratio = target_w / target_h
        img_ratio = img_w / img_h

        if img_ratio > target_ratio:
            # Image is wider than target. Crop width.
            new_w = int(img_h * target_ratio)
            left = (img_w - new_w) // 2
            img = img.crop((left, 0, left + new_w, img_h))
        elif img_ratio < target_ratio:
            # Image is taller than target. Crop height.
            new_h = int(img_w / target_ratio)
            top = (img_h - new_h) // 2
            img = img.crop((0, top, img_w, top + new_h))

        return img.resize((target_w, target_h), Image.Resampling.LANCZOS)

    def _fit_story_photo(self, img: Image.Image, target_w: int = 1080, target_h: int = 1920) -> Image.Image:
        """Prepare a photo for a 9:16 story WITHOUT cutting the subject.

        Near-portrait photos are cover-cropped as before. Landscape/square
        photos (cats, news shots) lose their subject when hard-cropped to
        9:16 — for those, paste the full photo (contain) over a blurred,
        darkened cover-crop of itself."""
        from PIL import ImageFilter, ImageEnhance
        img_ratio = img.width / img.height
        target_ratio = target_w / target_h  # 0.5625
        # close enough to 9:16 → normal cover crop loses almost nothing
        if img_ratio <= target_ratio * 1.25:
            return self._crop_and_resize(img, target_w, target_h)

        # blurred backdrop from the same photo
        bg = self._crop_and_resize(img, target_w, target_h)
        bg = bg.filter(ImageFilter.GaussianBlur(38))
        bg = ImageEnhance.Brightness(bg).enhance(0.55)

        # foreground: full photo, fitted to width
        fg_w = target_w
        fg_h = int(fg_w / img_ratio)
        max_fg_h = int(target_h * 0.62)  # keep bottom free for scrim/text
        if fg_h > max_fg_h:
            fg_h = max_fg_h
            fg_w = int(fg_h * img_ratio)
        fg = img.resize((fg_w, fg_h), Image.Resampling.LANCZOS)
        # place slightly above center so the headline zone stays clear
        x = (target_w - fg_w) // 2
        y = int(target_h * 0.42) - fg_h // 2
        y = max(int(target_h * 0.10), y)
        bg.paste(fg, (x, y))
        return bg

    def _draw_rounded_rectangle(self, draw: ImageDraw.ImageDraw, xy, radius, fill):
        """Draw a rounded rectangle (for backgrounds)."""
        x1, y1, x2, y2 = xy
        draw.rectangle(
            [(x1, y1 + radius), (x2, y2 - radius)],
            fill=fill
        )
        draw.rectangle(
            [(x1 + radius, y1), (x2 - radius, y2)],
            fill=fill
        )
        draw.pieslice([(x1, y1), (x1 + radius * 2, y1 + radius * 2)], 180, 270, fill=fill)
        draw.pieslice([(x2 - radius * 2, y2 - radius * 2), (x2, y2)], 0, 90, fill=fill)
        draw.pieslice([(x1, y2 - radius * 2), (x1 + radius * 2, y2)], 90, 180, fill=fill)
        draw.pieslice([(x2 - radius * 2, y1), (x2, y1 + radius * 2)], 270, 360, fill=fill)

    async def generate_weather_story(self, bg_url: Optional[str], temp_str: str, desc: str, date_str: str, city: str = "ИЖЕВСК СЕГОДНЯ") -> Optional[bytes]:
        """
        Generate a 1080x1920 weather story image.
        """
        W, H = 1080, 1920
        
        # Base image
        if bg_url:
            base_img = await self._download_image(bg_url)
        else:
            base_img = None
            
        if not base_img:
            # Create a simple gradient or solid background if download failed
            base_img = Image.new("RGB", (W, H), color=(40, 44, 52))

        # Crop and resize
        base_img = self._crop_and_resize(base_img, W, H)
        
        # Darken the background slightly to make text readable
        dark_overlay = Image.new("RGBA", (W, H), (0, 0, 0, 80))
        base_img = base_img.convert("RGBA")
        combined = Image.alpha_composite(base_img, dark_overlay)
        
        draw = ImageDraw.Draw(combined)
        
        try:
            font_title = ImageFont.truetype(self.font_bold_path, 60)
            font_temp = ImageFont.truetype(self.font_bold_path, 250)
            font_desc = ImageFont.truetype(self.font_reg_path, 70)
            font_date = ImageFont.truetype(self.font_reg_path, 50)
        except Exception as e:
            logger.error(f"Failed to load fonts: {e}")
            font_title = font_temp = font_desc = font_date = ImageFont.load_default()

        # Draw "ИЖЕВСК СЕГОДНЯ" (top)
        title_w = draw.textlength(city, font=font_title)
        draw.text(((W - title_w) // 2, 250), city, font=font_title, fill=(255, 255, 255, 200))
        
        # Draw small line under title
        draw.line(((W - 100) // 2, 330, (W + 100) // 2, 330), fill=(225, 50, 50, 255), width=8)

        # Draw Date
        date_w = draw.textlength(date_str, font=font_date)
        draw.text(((W - date_w) // 2, 400), date_str, font=font_date, fill=(255, 255, 255, 255))
        
        # Draw Temperature (Center)
        # Handle minus signs nicely if needed, but temp_str should be like "+15°C"
        temp_w = draw.textlength(temp_str, font=font_temp)
        draw.text(((W - temp_w) // 2, 700), temp_str, font=font_temp, fill=(255, 255, 255, 255))
        
        # Draw Description inside a pill/box (Below Temp)
        desc_w = draw.textlength(desc, font=font_desc)
        box_padding_x = 60
        box_padding_y = 30
        box_x1 = (W - desc_w) / 2 - box_padding_x
        box_y1 = 1050
        box_x2 = (W + desc_w) / 2 + box_padding_x
        box_y2 = 1050 + 70 + box_padding_y * 2
        
        self._draw_rounded_rectangle(draw, (box_x1, box_y1, box_x2, box_y2), 40, fill=(0, 0, 0, 150))
        draw.text(((W - desc_w) // 2, box_y1 + box_padding_y), desc, font=font_desc, fill=(255, 255, 255, 255))

        # ── OUTPUT ──
        final_img = combined.convert("RGB")
        out_bytes = io.BytesIO()
        final_img.save(out_bytes, format="JPEG", quality=92)
        return out_bytes.getvalue()

    # ── Per-rubric fallback photo keywords ────────────────────────────────
    # Used when caller passes photo_url=None (e.g. Wikimedia search failed upstream)
    _RUBRIC_PHOTO_FALLBACK: dict = {
        "weather":    ["izhevsk city winter", "russian city skyline"],
        "history":    ["old russian city historical", "sepia vintage"],
        "five_facts": ["izhevsk udmurtia nature", "russian landscape"],
        "recipe":     ["russian food cooking", "homemade dish"],
        "lifehack":   ["city life russia", "people street"],
        "place":      ["udmurtia nature landscape", "russia park"],
        "evening":    ["city night lights", "russia evening"],
        "digest":     ["izhevsk city news", "urban russia"],
        "holiday":    ["russian celebration holiday", "festive decoration"],
        "fact":       ["russia nature landscape", "architecture"],
        "news":       ["izhevsk city street", "russia news"],
    }

    # ── Universal rubric story generator ─────────────────────────────────

    async def generate_rubric_story(
        self, text: str, rubric: str = "fact", photo_url: Optional[str] = None
    ) -> Optional[bytes]:
        """Generate a themed 1080x1920 story for any rubric."""

        # If no photo provided, try to fetch one ourselves before rendering
        if not photo_url:
            try:
                from src.media_processor import MediaProcessor
                import os
                mp = MediaProcessor(
                    pexels_key=os.getenv("PEXELS_API_KEY", ""),
                    pixabay_key=os.getenv("PIXABAY_API_KEY", ""),
                )
                keywords = self._RUBRIC_PHOTO_FALLBACK.get(rubric, ["russia city", "landscape"])
                photos = await mp.search_stock_photo(keywords, count=3)
                if photos:
                    photo_url = photos[0]["url"]
                    logger.info(f"Story fallback photo found for rubric '{rubric}': {photo_url[:60]}")
                else:
                    logger.info(f"Story: no fallback photo found for rubric '{rubric}', using gradient")
            except Exception as ph_err:
                logger.warning(f"Story fallback photo search failed: {ph_err}")

        try:
            return await asyncio.wait_for(
                self._generate_rubric_story_inner(text, rubric, photo_url), timeout=45
            )
        except asyncio.TimeoutError:
            logger.error(f"Rubric story ({rubric}) timed out after 45s")
            return None
        except Exception as e:
            logger.error(f"Rubric story ({rubric}) failed: {e}")
            return None

    # Clean unified story style → (background colour, label)
    _CLEAN_STORY = {
        "fact": ((64, 42, 110), "А знаете ли вы?"),
        "weather": ((26, 82, 150), "Погода"),
        "history": ((74, 56, 36), "История Ижевска"),
        "recipe": ((140, 72, 24), "Рецепт дня"),
        "lifehack": ((20, 104, 82), "Полезное"),
        "place": ((26, 92, 66), "Места Удмуртии"),
        "news": ((150, 38, 38), "Новости"),
        "evening": ((26, 22, 72), "Вечерний Ижевск"),
        "digest": ((30, 46, 96), "Главное за день"),
        "five_facts": ((92, 32, 112), "5 фактов"),
        "holiday": ((124, 32, 72), "Праздник"),
    }

    @staticmethod
    def _wrap_story(draw, text, font, max_w):
        words = text.split()
        lines, cur = [], ""
        for w in words:
            t = (cur + " " + w).strip()
            if draw.textlength(t, font=font) <= max_w:
                cur = t
            else:
                if cur:
                    lines.append(cur)
                cur = w
        if cur:
            lines.append(cur)
        return lines

    def _render_clean_story(self, headline: str, label: Optional[str],
                            photo: Optional[Image.Image] = None,
                            bg_color=(30, 40, 70)) -> bytes:
        """Clean modern 9:16 story: photo + bottom scrim + label + headline + brand.
        Solid brand-colour background when no photo. No orbs/glow/glass."""
        W, H = 1080, 1920
        headline = _strip_emoji(headline or "").strip()

        if photo is not None:
            base = self._fit_story_photo(photo, W, H).convert("RGBA")
            scrim = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            sd = ImageDraw.Draw(scrim)
            b_start = int(H * 0.40)
            for y in range(b_start, H):
                a = min(int(240 * (y - b_start) / (H - b_start)), 240)
                sd.line([(0, y), (W, y)], fill=(0, 0, 0, a))
            top_h = int(H * 0.16)
            for y in range(top_h):
                sd.line([(0, y), (W, y)], fill=(0, 0, 0, int(110 * (1 - y / top_h))))
            base = Image.alpha_composite(base, scrim)
        else:
            base = Image.new("RGBA", (W, H), (*bg_color, 255))

        draw = ImageDraw.Draw(base)
        margin = 80
        f_label = ImageFont.truetype(self.font_bold_path, 44)
        f_brand = ImageFont.truetype(self.font_bold_path, 38)

        if label:
            draw.text((margin, 120), label.upper(), font=f_label, fill=(255, 255, 255))
            lw = draw.textlength(label.upper(), font=f_label)
            draw.rectangle((margin, 178, margin + lw, 184), fill=(255, 255, 255))

        headline = _strip_emoji(headline or "").strip()
        total_len = len(headline)
        size = 76 if total_len < 80 else (62 if total_len < 170 else 50)
        f_head = ImageFont.truetype(self.font_bold_path, size)
        line_h = size + 16
        # Respect explicit line breaks (e.g. a digest list), then word-wrap each line
        raw_lines = [l.strip() for l in headline.split("\n") if l.strip()]
        lines = []
        for rl in raw_lines:
            lines.extend(self._wrap_story(draw, rl, f_head, W - 2 * margin))
        lines = lines[:10]
        block_h = len(lines) * line_h
        if photo is not None:
            y = H - 240 - block_h               # bottom, over the scrim
        else:
            y = max(300, (H - block_h) // 2)     # vertically centered text card
        for ln in lines:
            draw.text((margin, y), ln, font=f_head, fill=(255, 255, 255))
            y += line_h

        draw.rectangle((margin, H - 150, margin + 56, H - 144), fill=(255, 255, 255))
        draw.text((margin, H - 132), "ИЖЕВСК СЕГОДНЯ", font=f_brand, fill=(255, 255, 255))

        out = base.convert("RGB")
        buf = io.BytesIO()
        out.save(buf, format="JPEG", quality=90)
        return buf.getvalue()

    async def _generate_rubric_story_inner(
        self, text: str, rubric: str, photo_url: Optional[str]
    ) -> Optional[bytes]:
        bg_color, label = self._CLEAN_STORY.get(rubric, self._CLEAN_STORY["fact"])
        photo = await self._download_image(photo_url) if photo_url else None
        return self._render_clean_story(text, label, photo=photo, bg_color=bg_color)

    # ── Convenience wrappers (backward compat) ───────────────────────────

    async def generate_quiz_story(self, bg_url: Optional[str], question: str) -> Optional[bytes]:
        """Generate a fact/quiz story (purple theme)."""
        return await self.generate_rubric_story(question, rubric="fact", photo_url=bg_url)

    async def generate_news_story(self, headline: str, photo_url: Optional[str] = None) -> Optional[bytes]:
        """Generate a breaking news story (red theme)."""
        return await self.generate_rubric_story(headline, rubric="news", photo_url=photo_url)

    async def generate_cat_story(self, hour: int = -1) -> Optional[bytes]:
        """A feel-good cat story. High-quality photo from Pexels (TheCatAPI as
        fallback), time-aware greeting, clean unified style."""
        from datetime import datetime, timezone, timedelta
        import random

        if hour < 0:
            hour = datetime.now(timezone(timedelta(hours=4))).hour

        # 1) Prefer Pexels — far better quality than TheCatAPI's random uploads
        photo = None
        try:
            from src.media_processor import MediaProcessor
            mp = MediaProcessor(
                pexels_key=os.getenv("PEXELS_API_KEY", ""),
                pixabay_key=os.getenv("PIXABAY_API_KEY", ""),
            )
            query = random.choice([
                ["cute cat portrait"], ["kitten cozy home"],
                ["cat sunlight window"], ["fluffy cat close up"], ["sleeping cat blanket"],
            ])
            photos = await mp.search_stock_photo(query, count=8, orientation="portrait")
            if photos:
                url = random.choice(photos[:6])["url"]
                photo = await self._download_image(url)
        except Exception as e:
            logger.warning(f"Cat story: Pexels fetch failed ({e}), trying TheCatAPI")

        # 2) Fallback: TheCatAPI (high-res only)
        if photo is None:
            try:
                headers = {"User-Agent": "IzhevskTodayNewsBot/1.0"}
                async with aiohttp.ClientSession(headers=headers) as session:
                    async with session.get(
                        "https://api.thecatapi.com/v1/images/search"
                        "?limit=10&mime_types=image/jpeg,image/png&size=full",
                        timeout=10,
                    ) as resp:
                        if resp.status == 200:
                            cands = await resp.json()
                            hq = [c for c in cands if min(c.get("width", 0), c.get("height", 0)) >= 1000]
                            pool = hq or cands
                            if pool:
                                photo = await self._download_image(random.choice(pool[:5])["url"])
            except Exception as e:
                logger.error(f"Cat story: TheCatAPI failed: {e}")

        if photo is None:
            return None

        if hour >= 20:
            texts = ["Спокойной ночи, Ижевск", "Сладких снов", "Ночь. Котики. Покой"]
        elif hour >= 12:
            texts = ["Котиков много не бывает", "Минутка позитива", "Обеденный котик"]
        else:
            texts = ["Доброе утро, Ижевск", "Всем хорошего дня", "Котиков много не бывает"]
        return self._render_clean_story(random.choice(texts), label=None, photo=photo)

    async def generate_video_story(self, video_url: str, text: str, output_path: str = "story_temp.mp4", music_url: Optional[str] = None) -> Optional[str]:
        """
        Generate a 1080x1920 MP4 story from a source video URL or local path.
        Downloads the video (if URL), crops to 9:16, darkens it, adds text overlay, and limits to 15 seconds.
        Returns the path to the generated MP4, or None on failure.
        """
        import tempfile
        import ffmpeg
        import os
        
        try:
            # 1. Download source video or use local path
            is_local = os.path.exists(video_url)
            if is_local:
                input_mp4 = video_url
            else:
                input_mp4 = tempfile.mktemp(suffix=".mp4")
                headers = {"User-Agent": "IzhevskTodayNewsBot/1.0"}
                async with aiohttp.ClientSession(headers=headers) as session:
                    async with session.get(video_url, timeout=30) as resp:
                        if resp.status == 200:
                            with open(input_mp4, 'wb') as f:
                                f.write(await resp.read())
                        else:
                            logger.error(f"Failed to download video: {resp.status}")
                            return None

            # 2. Process with ffmpeg
            # We need to scale/crop to exactly 1080x1920.
            # -vf "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,colorchannelmixer=r=.6:g=.6:b=.6,drawtext=..."
            
            # Strip emoji — Roboto font can't render them in ffmpeg either
            text = _strip_emoji(text)
            # Prepare text for ffmpeg drawtext (escape colons, backslashes and single quotes)
            safe_text = text.replace('\\', '\\\\').replace(':', '\\:').replace("'", "'\\''")
            
            # ffmpeg-python pipeline
            stream = ffmpeg.input(input_mp4, t=15) # Cut at 15s max
            
            # Scale & Crop & Darken
            stream = ffmpeg.filter(stream, 'scale', 1080, 1920, force_original_aspect_ratio='increase')
            stream = ffmpeg.filter(stream, 'crop', 1080, 1920)
            stream = ffmpeg.filter(stream, 'colorchannelmixer', rr=0.6, gg=0.6, bb=0.6) # Darken to 60%
            
            # Draw Text (Centered)
            # We use a built-in font or try to pass our Roboto path. Windows paths in ffmpeg can be tricky,
            # so we'll format the path with forward slashes.
            font_path_ff = self.font_bold_path.replace("\\", "/")
            stream = ffmpeg.filter(
                stream, 'drawtext',
                fontfile=font_path_ff,
                text=safe_text,
                fontcolor='white',
                fontsize=80,
                x='(w-text_w)/2',
                y='(h-text_h)/2',
                box=1,
                boxcolor='black@0.6',
                boxborderw=30
            )
            
            # Add watermark at bottom
            stream = ffmpeg.filter(
                stream, 'drawtext',
                fontfile=font_path_ff,
                text="ИЖЕВСК СЕГОДНЯ",
                fontcolor='white@0.5',
                fontsize=50,
                x='(w-text_w)/2',
                y='h-150'
            )
            
            # Output
            # If no audio in source, this might fail unless we just take video stream,
            # so we explicitly take the video stream 'v' from the filter output.
            out = ffmpeg.output(stream, output_path, vcodec='libx264', pix_fmt='yuv420p', crf=23, acodec='aac')
            
            # Run in executor to avoid blocking the asyncio event loop
            loop = asyncio.get_event_loop()
            _out = out
            await loop.run_in_executor(
                None,
                lambda: ffmpeg.run(_out, overwrite_output=True, capture_stdout=True, capture_stderr=True)
            )
            
            # Cleanup temp input if we downloaded it
            try:
                if not is_local:
                    os.remove(input_mp4)
            except:
                pass
                
            return output_path
            
        except ffmpeg.Error as e:
            logger.error(f"FFmpeg error: {e.stderr.decode('utf-8')}")
            return None
        except Exception as e:
            logger.error(f"Video story generation failed: {e}")
            return None
