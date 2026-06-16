"""
Generates a local pool of official-style alert template images for emergency posts
(ракетная опасность, опасное небо, БПЛА, воздушная тревога, отмена/отбой).

These ship inside the Docker image (assets/ is NOT a runtime volume) so the bot
always has a clean, on-topic image for danger posts instead of a random Moscow stock
photo. Re-run after editing to refresh:  python scripts/generate_alert_images.py
"""
import os
from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS = os.path.join(HERE, "assets")
OUT = os.path.join(ASSETS, "alerts")
os.makedirs(OUT, exist_ok=True)

FONT_BOLD = os.path.join(ASSETS, "Roboto-Bold.ttf")
FONT_REG = os.path.join(ASSETS, "Roboto-Regular.ttf")

W, H = 1280, 854

# (background, accent/text color)
RED = ((214, 40, 40), (255, 255, 255))
GREEN = ((33, 122, 60), (255, 255, 255))
GREY = ((238, 240, 242), (40, 44, 52))
ORANGE = ((222, 110, 20), (255, 255, 255))


def _font(path, size):
    return ImageFont.truetype(path, size)


def _center_text(draw, cx, y, text, font, fill):
    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    draw.text((cx - tw / 2, y), text, font=font, fill=fill)
    return y + (bbox[3] - bbox[1])


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


def make(filename, palette, badge, heading, subtitle, phone=True):
    bg, fg = palette
    img = Image.new("RGB", (W, H), bg)
    d = ImageDraw.Draw(img)
    cx = W // 2

    # Big exclamation badge
    f_badge = _font(FONT_BOLD, 200)
    _center_text(d, cx, 60, badge, f_badge, fg)

    # Heading (can be 1-2 lines)
    f_head = _font(FONT_BOLD, 82)
    y = 350
    for line in heading.split("\n"):
        y = _center_text(d, cx, y, line, f_head, fg) + 14

    # White rounded box with subtitle
    f_sub = _font(FONT_BOLD, 46)
    sub_lines = _wrap(d, subtitle, f_sub, W - 280)
    line_h = 64
    box_h = len(sub_lines) * line_h + 70
    # Keep the box high enough that the footer fits below it
    box_top = max(y + 30, 560)
    if box_top + box_h > H - 95:
        box_top = H - 95 - box_h
    box = (120, box_top, W - 120, box_top + box_h)
    d.rounded_rectangle(box, radius=28, fill=(255, 255, 255))
    sy = box_top + 35
    for line in sub_lines:
        sy = _center_text(d, cx, sy, line, f_sub, (30, 33, 40)) + (line_h - 46)

    # Emergency phone footer — placed below the box
    if phone:
        f_foot = _font(FONT_REG, 32)
        _center_text(d, cx, box_top + box_h + 24, "Телефон вызова экстренных служб — 112", f_foot, fg)

    path = os.path.join(OUT, filename)
    img.save(path, quality=92)
    print("wrote", path, img.size)


def main():
    # Ракетная опасность
    make("rocket_1.png", RED, "!", "ВНИМАНИЕ",
         "На территории Удмуртской Республики введён сигнал «Ракетная опасность»")
    make("rocket_2.png", GREY, "!", "РАКЕТНАЯ\nОПАСНОСТЬ",
         "Введён сигнал на территории Удмуртской Республики. Будьте в укрытии")

    # Опасное небо
    make("sky_1.png", RED, "!", "ВАЖНАЯ\nИНФОРМАЦИЯ",
         "В Удмуртии введён сигнал «Опасное небо». Силы и средства в повышенной готовности")
    make("sky_2.png", RED, "!", "ОПАСНОЕ\nНЕБО",
         "Объявлен сигнал «Опасное небо». Следуйте указаниям экстренных служб")

    # БПЛА / беспилотники
    make("drones_1.png", ORANGE, "!", "УГРОЗА БПЛА",
         "Возможна атака беспилотников. Не подходите к окнам, оставайтесь в укрытии")

    # Воздушная тревога / сирены
    make("sirens_1.png", RED, "!", "ВОЗДУШНАЯ\nТРЕВОГА",
         "Звучат сирены. Пройдите в укрытие и следите за официальными сообщениями")

    # Отмена / отбой
    make("cancel_1.png", GREEN, "!", "ВАЖНАЯ\nИНФОРМАЦИЯ",
         "Отмена сигнала «Опасное небо». Угроза миновала", phone=False)
    make("cancel_2.png", GREEN, "!", "ОТБОЙ\nТРЕВОГИ",
         "Режим «Ракетная опасность» снят. Можно вернуться к обычной жизни", phone=False)


if __name__ == "__main__":
    main()
