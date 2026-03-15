"""
Story Generator — Creates 9:16 vertical images and videos for VK Stories.
"""

import os
import io
import logging
import asyncio
import aiohttp
from PIL import Image, ImageDraw, ImageFont, ImageFilter
from typing import Optional

logger = logging.getLogger(__name__)

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

        # Bottom text prompt (optional, maybe "Смахивай вверх чтобы почитать новости")
        bottom_text = "Подробнее о погоде и новостях города"
        current_ratio = w / h

        if current_ratio > target_ratio:
            new_w = int(h * target_ratio)
            left = (w - new_w) // 2
            img = img.crop((left, 0, left + new_w, h))
        elif current_ratio < target_ratio:
            new_h = int(w / target_ratio)
            top = (h - new_h) // 2
            img = img.crop((0, top, w, top + new_h))

        return img.resize((target_w, target_h), Image.LANCZOS)

    # ── Universal rubric story generator ─────────────────────────────────

    async def generate_rubric_story(
        self, text: str, rubric: str = "fact", photo_url: Optional[str] = None
    ) -> Optional[bytes]:
        """Generate a themed 1080x1920 story for any rubric."""
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

    async def _generate_rubric_story_inner(
        self, text: str, rubric: str, photo_url: Optional[str]
    ) -> Optional[bytes]:
        import textwrap
        W, H = 1080, 1920

        theme = RUBRIC_THEMES.get(rubric, RUBRIC_THEMES["fact"])
        gt = theme["grad_top"]
        gm = theme["grad_mid"]
        gb = theme["grad_bot"]
        accent = theme["accent"]
        orb_base = theme["orb_base"]
        header = theme["header"]
        bar_style = theme.get("bar_style", "lines")

        # ── 1. THREE-STOP GRADIENT ──
        base_img = Image.new("RGBA", (W, H))
        draw_bg = ImageDraw.Draw(base_img)
        for y in range(H):
            t = y / H
            if t < 0.5:
                t2 = t * 2
                r = int(gt[0] + (gm[0] - gt[0]) * t2)
                g = int(gt[1] + (gm[1] - gt[1]) * t2)
                b = int(gt[2] + (gm[2] - gt[2]) * t2)
            else:
                t2 = (t - 0.5) * 2
                r = int(gm[0] + (gb[0] - gm[0]) * t2)
                g = int(gm[1] + (gb[1] - gm[1]) * t2)
                b = int(gm[2] + (gb[2] - gm[2]) * t2)
            draw_bg.line([(0, y), (W, y)], fill=(r, g, b, 255))

        # ── 2. GLOWING ORBS ──
        orbs_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        orb_positions = [
            (180, 300, 200, 25), (900, 500, 160, 20),
            (540, 1400, 250, 18), (100, 1600, 180, 15), (950, 1700, 130, 20),
        ]
        for cx, cy, radius, base_alpha in orb_positions:
            for ring in range(radius, 0, -2):
                alpha = int(base_alpha * (ring / radius))
                orb_draw = ImageDraw.Draw(orbs_layer)
                orb_draw.ellipse(
                    [(cx - ring, cy - ring), (cx + ring, cy + ring)],
                    fill=(orb_base[0], orb_base[1], orb_base[2], alpha)
                )
        base_img = Image.alpha_composite(base_img, orbs_layer)

        # ── 3. BACKGROUND PHOTO ──
        if photo_url:
            photo = await self._download_image(photo_url)
            if photo:
                photo = self._crop_and_resize(photo, W, H).convert("RGBA")
                photo.putalpha(110)
                base_img = Image.alpha_composite(base_img, photo)

        # ── 4. TOP VIGNETTE ──
        vignette = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        vignette_draw = ImageDraw.Draw(vignette)
        for y in range(400):
            alpha = int(80 * (1 - y / 400))
            vignette_draw.line([(0, y), (W, y)], fill=(0, 0, 0, alpha))
        base_img = Image.alpha_composite(base_img, vignette)

        combined = base_img
        draw = ImageDraw.Draw(combined)

        # 5. FONTS ──
        try:
            font_title = ImageFont.truetype(self.font_bold_path, 52)
            font_q = ImageFont.truetype(self.font_bold_path, 52)
            font_brand = ImageFont.truetype(self.font_reg_path, 32)
        except:
            font_title = font_q = font_brand = ImageFont.load_default()

        def draw_text_glow(draw_obj, pos, txt, font, text_color, glow_color=(0,0,0,120), offset=3):
            x, y = pos
            for dx, dy in [(-1,-1),(1,-1),(-1,1),(1,1),(0,2),(2,0)]:
                draw_obj.text((x + dx*offset, y + dy*offset), txt, font=font, fill=glow_color)
            draw_obj.text((x, y), txt, font=font, fill=text_color)

        # ── 6. HEADER DECORATION ──
        accent_rgba = (*accent, 200)
        if bar_style == "bars":
            draw.rectangle([(0, 180), (W, 188)], fill=(*accent, 200))
            th_w = draw.textlength(header, font=font_title)
            draw_text_glow(draw, ((W - th_w) // 2, 210), header, font=font_title, text_color=(*accent, 255))
            draw.rectangle([(0, 280), (W, 288)], fill=(*accent, 200))
        else:
            line_w = 200
            draw.line([(W//2 - line_w, 220), (W//2 + line_w, 220)], fill=accent_rgba, width=3)
            th_w = draw.textlength(header, font=font_title)
            draw_text_glow(draw, ((W - th_w) // 2, 245), header, font=font_title, text_color=(*accent, 255))
            draw.line([(W//2 - line_w, 325), (W//2 + line_w, 325)], fill=accent_rgba, width=3)

        # ── 7. WORD WRAP ──
        lines = textwrap.wrap(text, width=32)
        if len(lines) > 8:
            lines = textwrap.wrap(text, width=38)
            try:
                font_q = ImageFont.truetype(self.font_bold_path, 44)
            except:
                pass

        # ── 8. FROSTED GLASS CARD ──
        line_h = 70
        line_spacing = 12
        total_text_h = len(lines) * line_h + (len(lines) - 1) * line_spacing
        start_y = 400 if bar_style == "lines" else 350
        box_x1, box_x2 = 60, W - 60
        box_y1 = start_y - 45
        box_y2 = start_y + total_text_h + 45

        glass = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        glass_draw = ImageDraw.Draw(glass)
        glass_draw.rounded_rectangle(
            [(box_x1 - 4, box_y1 - 4), (box_x2 + 4, box_y2 + 4)],
            radius=28, fill=(255, 255, 255, 8)
        )
        glass_draw.rounded_rectangle(
            [(box_x1, box_y1), (box_x2, box_y2)],
            radius=24, fill=(255, 255, 255, 22)
        )
        border_color = (*accent[:3], 40) if bar_style == "bars" else (255, 255, 255, 50)
        glass_draw.rounded_rectangle(
            [(box_x1, box_y1), (box_x2, box_y2)],
            radius=24, fill=None, outline=border_color, width=2
        )
        combined = Image.alpha_composite(combined, glass)
        draw = ImageDraw.Draw(combined)

        # ── 9. TEXT ──
        y_text = start_y
        for line in lines:
            lw = draw.textlength(line, font=font_q)
            draw_text_glow(
                draw, ((W - lw) // 2, y_text), line, font=font_q,
                text_color=(255, 255, 255, 255), glow_color=(0, 0, 0, 100), offset=2
            )
            y_text += line_h + line_spacing

        # ── 10. BRAND WATERMARK ──
        brand = "ИЖЕВСК СЕГОДНЯ"
        bw = draw.textlength(brand, font=font_brand)
        draw.text(((W - bw) // 2, H - 120), brand, font=font_brand, fill=(255, 255, 255, 80))
        draw.ellipse([(W//2 - 4, H - 80), (W//2 + 4, H - 72)], fill=(*accent, 120))

        # ── OUTPUT ──
        final_img = combined.convert("RGB")
        out_bytes = io.BytesIO()
        final_img.save(out_bytes, format="JPEG", quality=92)
        return out_bytes.getvalue()

    # ── Convenience wrappers (backward compat) ───────────────────────────

    async def generate_quiz_story(self, bg_url: Optional[str], question: str) -> Optional[bytes]:
        """Generate a fact/quiz story (purple theme)."""
        return await self.generate_rubric_story(question, rubric="fact", photo_url=bg_url)

    async def generate_news_story(self, headline: str, photo_url: Optional[str] = None) -> Optional[bytes]:
        """Generate a breaking news story (red theme)."""
        return await self.generate_rubric_story(headline, rubric="news", photo_url=photo_url)

    async def generate_cat_story(self) -> Optional[bytes]:
        """
        Fetch a random cat from TheCatAPI, crop to 9:16, add a subtle funny text if possible.
        """
        W, H = 1080, 1920
        # Get random cat photo that is reasonably tall
        cat_api_url = "https://api.thecatapi.com/v1/images/search"
        
        try:
            headers = {"User-Agent": "IzhevskTodayNewsBot/1.0"}
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.get(cat_api_url, timeout=10) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        cat_img_url = data[0]["url"]
                    else:
                        cat_img_url = None
        except Exception as e:
            logger.error(f"Failed to fetch cat API: {e}")
            cat_img_url = None

        if cat_img_url:
            base_img = await self._download_image(cat_img_url)
        else:
            base_img = None
            
        if not base_img:
            return None # Skip if no cat found

        base_img = self._crop_and_resize(base_img, W, H)
        
        # Add a subtle gradient at the bottom so we can put text, or just leave it bare
        dark_overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        draw_overlay = ImageDraw.Draw(dark_overlay)
        # Gradient bottom half
        for y in range(H//2, H):
            alpha = int(((y - H//2) / (H//2)) * 180)
            draw_overlay.line([(0, y), (W, y)], fill=(0, 0, 0, alpha))
            
        base_img = base_img.convert("RGBA")
        combined = Image.alpha_composite(base_img, dark_overlay)
        
        draw = ImageDraw.Draw(combined)
        try:
            font_title = ImageFont.truetype(self.font_bold_path, 80)
        except:
            font_title = ImageFont.load_default()

        # Fun text options without emojis for PIL safety
        import random
        texts = [
            "Всем хорошего дня!",
            "Время немножко отдохнуть",
            "Пуньк!",
            "Спокойной ночи, Ижевск!",
            "Котиков много не бывает",
            "Мяу!"
        ]
        text_str = random.choice(texts)
        tw = draw.textlength(text_str, font=font_title)
        draw.text(((W - tw) // 2, 1600), text_str, font=font_title, fill=(255, 255, 255, 255))
        
        final_img = combined.convert("RGB")
        out_bytes = io.BytesIO()
        final_img.save(out_bytes, format="JPEG", quality=90)
        return out_bytes.getvalue()

    async def generate_video_story(self, video_url: str, text: str, output_path: str = "story_temp.mp4", music_url: Optional[str] = None) -> Optional[str]:
        """
        Generate a 1080x1920 MP4 story from a source video URL.
        Downloads the video, crops to 9:16, darkens it, adds text overlay, and limits to 15 seconds.
        Returns the path to the generated MP4, or None on failure.
        """
        import tempfile
        import ffmpeg
        
        try:
            # 1. Download source video
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
                shadowcolor='black',
                shadowx=2,
                shadowy=2
            )
            
            # Output
            # If no audio in source, this might fail unless we just take video stream,
            # so we explicitly take the video stream 'v' from the filter output.
            out = ffmpeg.output(stream, output_path, vcodec='libx264', pix_fmt='yuv420p', crf=23, acodec='aac')
            
            # Run
            ffmpeg.run(out, overwrite_output=True, capture_stdout=True, capture_stderr=True)
            
            # Cleanup temp input
            try:
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
