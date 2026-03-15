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
                async with session.get(url, timeout=20) as resp:
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
        font_bottom = font_date
        bottom_w = draw.textlength(bottom_text, font=font_bottom)
        draw.text(((W - bottom_w) // 2, 1600), bottom_text, font=font_bottom, fill=(255, 255, 255, 180))
        
        # Convert back to RGB and save to bytes
        final_img = combined.convert("RGB")
        out_bytes = io.BytesIO()
        final_img.save(out_bytes, format="JPEG", quality=90)
        return out_bytes.getvalue()

    async def generate_quiz_story(self, bg_url: Optional[str], question: str) -> Optional[bytes]:
        """
        Generate a 1080x1920 quiz story with space exactly for the VK Poll widget.
        The Poll widget in VK stories is usually placed in the center or bottom third.
        We'll put the text in the top half.
        """
        W, H = 1080, 1920
        
        if bg_url:
            base_img = await self._download_image(bg_url)
        else:
            base_img = None
            
        if not base_img:
            # Fallback to a dark blue modern gradient instead of flat color
            base_img = Image.new("RGB", (W, H))
            draw_bg = ImageDraw.Draw(base_img)
            for y in range(H):
                # Gradient from dark blue to dark purple
                r = int(20 + (30 - 20) * (y / H))
                g = int(25 + (20 - 25) * (y / H))
                b = int(40 + (50 - 40) * (y / H))
                draw_bg.line([(0, y), (W, y)], fill=(r, g, b))

        base_img = self._crop_and_resize(base_img, W, H)
        
        # Add a stylish gradient overlay that gets darker at the top for the text
        dark_overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        draw_overlay = ImageDraw.Draw(dark_overlay)
        for y in range(H):
            # Darker at top (y=0), almost transparent at bottom (y=H)
            alpha = int(200 - (120 * (y / H)))
            draw_overlay.line([(0, y), (W, y)], fill=(0, 0, 0, alpha))
            
        base_img = base_img.convert("RGBA")
        combined = Image.alpha_composite(base_img, dark_overlay)
        
        draw = ImageDraw.Draw(combined)
        
        try:
            font_title = ImageFont.truetype(self.font_bold_path, 65)
            font_q = ImageFont.truetype(self.font_bold_path, 75)
        except:
            font_title = font_q = ImageFont.load_default()

        # Helper for drawing text with shadow
        def draw_text_with_shadow(draw_obj, pos, text, font, text_color, shadow_color=(0,0,0,200), offset=4):
            x, y = pos
            draw_obj.text((x+offset, y+offset), text, font=font, fill=shadow_color)
            draw_obj.text((x, y), text, font=font, fill=text_color)

        # Header (Removed emoji to avoid squares, using bright accent color)
        header = "А ЗНАЕТЕ ЛИ ВЫ?"
        th_w = draw.textlength(header, font=font_title)
        draw_text_with_shadow(draw, ((W - th_w) // 2, 250), header, font=font_title, text_color=(255, 214, 0, 255))
        
        # Word wrap for question
        import textwrap
        lines = textwrap.wrap(question, width=24)
        
        # Draw a semi-transparent rounded rectangle behind text for better readability
        # Calculate text block height
        line_spacing = 20
        total_text_h = len(lines) * 90 + (len(lines) - 1) * line_spacing
        
        start_y = 420
        box_padding = 50
        box_y1 = start_y - box_padding
        box_y2 = start_y + total_text_h + box_padding
        
        # Draw background bubble
        bubble = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        bubble_draw = ImageDraw.Draw(bubble)
        bubble_draw.rounded_rectangle([(80, box_y1), (W - 80, box_y2)], radius=30, fill=(0, 0, 0, 140), width=3, outline=(255, 255, 255, 30))
        combined = Image.alpha_composite(combined, bubble)
        draw = ImageDraw.Draw(combined)
        
        y_text = start_y
        for line in lines:
            lw = draw.textlength(line, font=font_q)
            draw_text_with_shadow(draw, ((W - lw) // 2, y_text), line, font=font_q, text_color=(255, 255, 255, 255), offset=3)
            y_text += 90 + line_spacing
            
        # The bottom half is left empty/dark for the VK Poll/Link Widget
        final_img = combined.convert("RGB")
        out_bytes = io.BytesIO()
        final_img.save(out_bytes, format="JPEG", quality=90)
        return out_bytes.getvalue()

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
