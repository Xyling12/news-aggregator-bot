"""
demo_clip.py — демонстрация генерации VK Клипа (вертикальное видео 9:16).

Запуск:
    python scripts/demo_clip.py

Результат: demo_clip_output.mp4 (1080x1920, 15 сек)
Требует: pip install ffmpeg-python aiohttp pillow
         + ffmpeg установленный в системе
"""

import asyncio
import io
import os
import sys
import textwrap

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import aiohttp
import ffmpeg
from PIL import Image, ImageDraw, ImageFont

# ── Настройки демо ────────────────────────────────────────────────────────────
DEMO_HEADLINE = "В Ижевске открылся новый ТЦ «Столица»: 200 магазинов и каток"
DEMO_CHANNEL  = "@IzhevskTodayNews"
DEMO_PHOTO_URL = None  # None = возьмёт фото автоматически из Pexels/Wikimedia

ASSETS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets")
FONT_BOLD   = os.path.join(ASSETS_DIR, "Roboto-Bold.ttf")
FONT_REG    = os.path.join(ASSETS_DIR, "Roboto-Regular.ttf")
OUTPUT_PATH = "demo_clip_output.mp4"

# ── Цветовая тема: красная (новости) ─────────────────────────────────────────
THEME = {
    "grad_top": (140, 15, 20),
    "grad_bot": (40, 5, 5),
    "accent":   (255, 80, 50),
    "header":   "ИЖЕВСК СЕГОДНЯ",
}


async def _download_image(url: str) -> Image.Image | None:
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status == 200:
                    return Image.open(io.BytesIO(await r.read())).convert("RGB")
    except Exception as e:
        print(f"  [!] Ошибка загрузки фото: {e}")
    return None


def _crop_to_916(img: Image.Image, w=1080, h=1920) -> Image.Image:
    iw, ih = img.size
    ratio = w / h
    if iw / ih > ratio:
        nw = int(ih * ratio)
        img = img.crop(((iw - nw) // 2, 0, (iw - nw) // 2 + nw, ih))
    else:
        nh = int(iw / ratio)
        img = img.crop((0, (ih - nh) // 2, iw, (ih - nh) // 2 + nh))
    return img.resize((w, h), Image.LANCZOS)


def _generate_frame(headline: str, channel: str, photo: Image.Image | None) -> bytes:
    """Генерирует один PNG-кадр 1080x1920 — это будет «обложка» Клипа."""
    W, H = 1080, 1920
    theme = THEME

    # 1. Фон — градиент
    base = Image.new("RGBA", (W, H))
    draw_bg = ImageDraw.Draw(base)
    gt, gb = theme["grad_top"], theme["grad_bot"]
    for y in range(H):
        t = y / H
        r = int(gt[0] + (gb[0] - gt[0]) * t)
        g = int(gt[1] + (gb[1] - gt[1]) * t)
        b = int(gt[2] + (gb[2] - gt[2]) * t)
        draw_bg.line([(0, y), (W, y)], fill=(r, g, b, 255))

    # 2. Фото поверх градиента (78% прозрачности)
    if photo:
        photo = _crop_to_916(photo, W, H).convert("RGBA")
        photo.putalpha(200)
        base = Image.alpha_composite(base, photo)
        # Тинт чтобы тема читалась
        tint = Image.new("RGBA", (W, H), (*theme["grad_bot"], 90))
        base = Image.alpha_composite(base, tint)

    # 3. Нижний градиент-занавес для читаемости текста
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ov_draw = ImageDraw.Draw(overlay)
    for y in range(H // 2, H):
        alpha = int(((y - H // 2) / (H // 2)) * 200)
        ov_draw.line([(0, y), (W, y)], fill=(0, 0, 0, alpha))
    base = Image.alpha_composite(base, overlay)

    draw = ImageDraw.Draw(base)

    # 4. Шрифты
    try:
        font_h = ImageFont.truetype(FONT_BOLD, 52)
        font_t = ImageFont.truetype(FONT_BOLD, 58)
        font_b = ImageFont.truetype(FONT_REG, 36)
    except:
        font_h = font_t = font_b = ImageFont.load_default()

    # 5. Хедер "ИЖЕВСК СЕГОДНЯ" + линии
    accent = theme["accent"]
    hdr = theme["header"]
    draw.line([(W//2 - 220, 230), (W//2 + 220, 230)], fill=(*accent, 220), width=3)
    hw = draw.textlength(hdr, font=font_h)
    draw.text(((W - hw) // 2, 255), hdr, font=font_h, fill=(255, 255, 255, 230))
    draw.line([(W//2 - 220, 325), (W//2 + 220, 325)], fill=(*accent, 220), width=3)

    # 6. Стеклянная карточка с заголовком (внизу, ~800–1600px)
    lines = textwrap.wrap(headline, width=28)
    line_h = 78
    total_h = len(lines) * line_h
    card_y1 = 1080
    card_y2 = card_y1 + total_h + 80
    card_x1, card_x2 = 60, W - 60

    glass = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    glass_draw = ImageDraw.Draw(glass)
    glass_draw.rounded_rectangle(
        [(card_x1, card_y1), (card_x2, card_y2)],
        radius=28, fill=(0, 0, 0, 150),
    )
    glass_draw.rounded_rectangle(
        [(card_x1, card_y1), (card_x2, card_y2)],
        radius=28, fill=None, outline=(*accent, 80), width=2,
    )
    base = Image.alpha_composite(base, glass)
    draw = ImageDraw.Draw(base)

    y_text = card_y1 + 35
    for line in lines:
        lw = draw.textlength(line, font=font_t)
        # Тень
        draw.text(((W - lw) // 2 + 2, y_text + 2), line, font=font_t, fill=(0, 0, 0, 150))
        draw.text(((W - lw) // 2, y_text), line, font=font_t, fill=(255, 255, 255, 255))
        y_text += line_h

    # 7. Метка канала (самый низ)
    bw = draw.textlength(channel, font=font_b)
    draw.text(((W - bw) // 2, H - 100), channel, font=font_b, fill=(255, 255, 255, 150))
    # Акцент-точка
    draw.ellipse([(W//2 - 4, H - 60), (W//2 + 4, H - 52)], fill=(*accent, 180))

    out = io.BytesIO()
    base.convert("RGB").save(out, format="PNG")
    return out.getvalue()


async def generate_demo_clip():
    print("=" * 60)
    print("  VK Клип — демо-генерация")
    print("=" * 60)
    print(f"  Заголовок: {DEMO_HEADLINE[:50]}...")

    # 1. Получаем фото
    photo = None
    if DEMO_PHOTO_URL:
        print(f"  Загружаю фото: {DEMO_PHOTO_URL[:60]}")
        photo = await _download_image(DEMO_PHOTO_URL)
    else:
        print("  Ищу фото через Wikimedia Commons...")
        try:
            import os as _os
            sys.path.insert(0, "src")
            from src.media_processor import MediaProcessor
            mp = MediaProcessor(pexels_key=_os.getenv("PEXELS_API_KEY", ""))
            photos = await mp.search_stock_photo(["Izhevsk city", "urban street"], count=3)
            if photos:
                print(f"  Фото найдено: {photos[0]['url'][:70]}")
                photo = await _download_image(photos[0]["url"])
            else:
                print("  Фото не найдено — будет градиент")
        except Exception as e:
            print(f"  MediaProcessor недоступен ({e}) — будет градиент")

    # 2. Генерируем PNG-кадр (обложка видео)
    print("  Генерирую PNG-кадр (обложка)...")
    frame_bytes = _generate_frame(DEMO_HEADLINE, DEMO_CHANNEL, photo)
    frame_path = "/tmp/demo_clip_frame.png"
    with open(frame_path, "wb") as f:
        f.write(frame_bytes)
    print(f"  Кадр сохранён: {frame_path}")

    # 3. ffmpeg: кадр → видео 15 сек (статичное + fade in)
    print(f"  ffmpeg: создаю {OUTPUT_PATH} (15 сек)...")
    try:
        stream = ffmpeg.input(frame_path, loop=1, t=15, framerate=25)
        # Fade in 0.5s, fade out 0.5s
        stream = ffmpeg.filter(stream, "fade", type="in", start_time=0, duration=0.5)
        stream = ffmpeg.filter(stream, "fade", type="out", start_time=14, duration=0.8)
        out = ffmpeg.output(
            stream, OUTPUT_PATH,
            vcodec="libx264",
            pix_fmt="yuv420p",
            crf=23,
            preset="fast",
            an=None,  # no audio (can add background music later)
        )
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: ffmpeg.run(out, overwrite_output=True, capture_stderr=True),
        )
        size_kb = os.path.getsize(OUTPUT_PATH) // 1024
        print(f"\n  ✅ Готово! {OUTPUT_PATH} ({size_kb} KB)")
        print(f"  Размер: 1080x1920, 15 сек, H.264")
        print(f"\n  Это и есть VK Клип. Загрузить можно через:")
        print(f"  vk_publisher.upload_clip('{OUTPUT_PATH}')")
    except ffmpeg.Error as e:
        err = e.stderr.decode("utf-8") if e.stderr else str(e)
        print(f"\n  [!] ffmpeg ошибка: {err[:400]}")
        print("  Убедись что ffmpeg установлен: apt install ffmpeg / brew install ffmpeg")
    except Exception as e:
        print(f"\n  [!] Ошибка: {e}")


if __name__ == "__main__":
    asyncio.run(generate_demo_clip())
