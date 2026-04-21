"""
Prompts, topic pools and generation functions for the GrandTransfer VK content scheduler.
"""

import random
from typing import Optional, Tuple
import aiohttp
import logging

logger = logging.getLogger(__name__)

# ─── Стиль написания ─────────────────────────────────────────────────────────

HUMAN_STYLE = """
СТИЛЬ — КРИТИЧЕСКИ ВАЖНО:
- Пиши живо и по-человечески, НЕ как реклама из газеты
- Короткие фразы, иногда многоточие... для паузы
- Лёгкий юмор уместен
- НЕ используй Markdown заголовки (# ## ###)
- НЕ используй эмодзи-цифры (1️⃣ 2️⃣)
- НЕ используй клише: «лучший в городе», «качество гарантировано»
"""

# ─── Промпты ─────────────────────────────────────────────────────────────────

PROMO_PROMPT = """Напиши рекламный пост для VK-группы межгородского такси «GrandTransfer».

Маршруты: {route}
Тип авто: {car_type}
Особенность: {feature}

СТРУКТУРА:
1. Цепляющая первая строка с эмодзи (не «приглашаем», а что-то живое)
2. Суть предложения в 2-3 предложениях
3. Конкретная выгода для пассажира
4. Призыв к действию (забронировать/написать)

Максимум 8 строк. Добавь в конце: 📞 Бронь: межгород.com | WhatsApp/Telegram
""" + HUMAN_STYLE

ROUTE_TIP_PROMPT = """Напиши пост о маршруте межгородского такси для VK.

Маршрут: {route}
Расстояние: {distance} км
Время в пути: примерно {hours} ч

СТРУКТУРА:
1. Эмодзи + «Маршрут дня» + название маршрута
2. Что интересного в пункте назначения (1 факт)
3. Сколько занимает и чем лучше ехать с нами, чем на автобусе/поезде
4. CTA: «Бронируй заранее 👇»

НЕ выдумывай цены — пиши «уточняйте» или «от X₽».
Добавь в конце: 📞 межгород.com | WhatsApp/Telegram
""" + HUMAN_STYLE

TRAVEL_TIP_PROMPT = """Напиши полезный совет для пассажиров межгородского такси для VK.

Тема совета: {topic}

СТРУКТУРА:
1. 💡 + цепляющий заголовок (проблема или вопрос)
2. Практичный совет в 3-4 предложениях
3. Конкретный пример или ситуация
4. Короткий вывод

Пиши как человек который сам много ездит, не как инструкция.
Добавь в конце: 🚕 GrandTransfer — межгород.com
""" + HUMAN_STYLE

MINIVAN_PROMPT = """Напиши пост про минивэн для компании/семьи для VK межгородского такси.

Сценарий: {scenario}

СТРУКТУРА:
1. 🚐 + живая ситуация («Едете большой компанией?»)
2. Почему минивэн выгоднее чем несколько легковых
3. Вместимость: до 7 человек + багаж
4. Расчёт: «Х мест = 1 машина вместо Y»

Максимум 7 строк. Добавь в конце: 📞 Бронь: межгород.com | WhatsApp/Telegram
""" + HUMAN_STYLE

FAQ_PROMPT = """Напиши пост «вопрос-ответ» для VK-группы межгородского такси GrandTransfer.

Вопрос: {question}

СТРУКТУРА:
❓ [Вопрос] — первая строка
✅ [Развёрнутый ответ — 3-4 предложения, конкретно и честно]
💬 Есть ещё вопросы? Пишите в комментариях или в ЛС!

Добавь в конце: 🚕 GrandTransfer — межгород.com
""" + HUMAN_STYLE

# ─── Пулы тем ───────────────────────────────────────────────────────────────

ROUTES = [
    ("Москва → Курск", 540, 6),
    ("Москва → Белгород", 700, 7),
    ("Москва → Ростов-на-Дону", 1100, 12),
    ("Москва → Воронеж", 520, 6),
    ("Курск → Белгород", 130, 2),
    ("Москва → Брянск", 380, 4),
    ("Москва → Тула", 180, 2),
    ("Белгород → Ростов-на-Дону", 600, 7),
    ("Курск → Воронеж", 250, 3),
    ("Москва → Липецк", 430, 5),
    ("Москва → Тамбов", 500, 5),
    ("Москва → Орёл", 370, 4),
    ("Курск → Орёл", 180, 2),
    ("Белгород → Воронеж", 290, 3),
    ("Москва → ДНР/Мариуполь", 1300, 14),
]

PROMO_CARS = [
    ("Комфорт-седан", "Kia Rio / Hyundai Solaris — климат, USB, вода"),
    ("Бизнес-седан", "Toyota Camry / Skoda Octavia — кожа, тишина"),
    ("Минивэн", "Ford Transit / Volkswagen Caravelle — до 7 мест + багаж"),
    ("Эконом", "Lada Vesta / ВАЗ — бюджетно и надёжно"),
]

PROMO_FEATURES = [
    "круглосуточный выезд",
    "встреча у подъезда",
    "фиксированная цена без доплат",
    "оплата картой или наличными",
    "бронь за 2 часа",
    "детское кресло бесплатно",
    "провоз домашних животных",
    "трансфер в аэропорт",
]

TRAVEL_TIPS = [
    "Как не укачаться в дальней поездке",
    "Что взять в межгород на 8+ часов",
    "Как правильно выбрать место в такси",
    "Когда лучше бронировать межгород: за день или за час",
    "Чем отличается трансфер от обычного такси",
    "Как путешествовать с ребёнком на длинные расстояния",
    "Что делать если рейс задержали а вас встречают",
    "Стоит ли делать остановки на маршруте",
    "Как правильно упаковать вещи в багажник такси",
    "Плюсы межгородского такси перед автобусом",
    "Как выбрать надёжного перевозчика",
    "Всё о страховке в межгородском такси",
]

MINIVAN_SCENARIOS = [
    "семья с детьми и детскими колясками едет на море",
    "компания друзей 6 человек едет на свадьбу в другой город",
    "вахтовики едут к месту работы с инструментами",
    "переезд: нужно перевезти крупные вещи + людей",
    "военные едут к месту службы с вещами",
    "корпоратив: коллеги едут на мероприятие в соседний город",
]

FAQ_QUESTIONS = [
    "Можно ли взять животное в салон?",
    "Как отменить бронирование и вернуть деньги?",
    "Можно ли сделать остановку по дороге?",
    "Что будет если я опоздаю на посадку?",
    "Как оплатить поездку?",
    "Водитель встретит меня у аэропорта с табличкой?",
    "Есть ли скидки на постоянных клиентов?",
    "Можно ли ехать с велосипедом или крупным багажом?",
    "Страхуете ли вы пассажиров?",
    "Есть ли детские кресла?",
]

# ─── Фото по маршрутам ───────────────────────────────────────────────────────

ROUTE_PHOTO_MAP = {
    "Курск": ["kursk russia city", "kursk cathedral"],
    "Белгород": ["belgorod russia", "russian city street"],
    "Ростов": ["rostov-on-don russia city", "rostov river"],
    "Воронеж": ["voronezh russia city", "russian city center"],
    "Брянск": ["bryansk russia", "russian city"],
    "Тула": ["tula russia kremlin", "tula city"],
    "Тамбов": ["tambov russia", "provincial city"],
    "Орёл": ["oryol russia city", "provincial city"],
    "Липецк": ["lipetsk russia", "russian city"],
    "Москва": ["moscow russia city night", "moscow skyline"],
}

# ─── Вспомогательные функции ─────────────────────────────────────────────────

# Стоп-список использованных тем (сбрасывается каждый день в TransferScheduler)
_USED_TODAY: dict = {}


def pick_unused(key: str, pool: list) -> any:
    """Выбрать случайный неиспользованный элемент из пула."""
    used = _USED_TODAY.get(key, [])
    available = [x for x in pool if x not in used]
    if not available:
        _USED_TODAY[key] = []
        available = pool
    chosen = random.choice(available)
    _USED_TODAY.setdefault(key, []).append(chosen)
    return chosen


def reset_daily() -> None:
    """Сбросить использованные темы (вызывать в полночь)."""
    _USED_TODAY.clear()


async def find_photo(keywords: list, pexels_key: str) -> Optional[str]:
    """Найти фото на Pexels по ключевым словам. Возвращает URL или None."""
    if not pexels_key:
        return None
    try:
        query = " ".join(keywords[:3])
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://api.pexels.com/v1/search",
                params={"query": query, "per_page": 15, "orientation": "landscape"},
                headers={"Authorization": pexels_key},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status == 200:
                    photos = (await resp.json()).get("photos", [])
                    if photos:
                        return random.choice(photos[:10])["src"]["large"]
    except Exception as e:
        logger.warning(f"Pexels ({keywords}): {e}")
    return None


async def ask_ai(prompt: str, api_key: str) -> Optional[str]:
    """Запрос к AITUNNEL (OpenAI-совместимый прокси)."""
    if not api_key:
        return None
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.aitunnel.ru/v1/chat/completions",
                json={
                    "model": "gpt-4o-mini",
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.8,
                    "max_tokens": 400,
                },
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status == 200:
                    return (await resp.json())["choices"][0]["message"]["content"].strip()
                logger.error(f"AI HTTP {resp.status}: {await resp.text()}")
    except Exception as e:
        logger.error(f"AI request failed: {e}")
    return None


# ─── Генераторы рубрик ───────────────────────────────────────────────────────

async def generate_promo(ai_key: str, pexels_key: str) -> Tuple[str, Optional[str]]:
    route_name, distance, hours = pick_unused("promo_route", ROUTES)
    car_type, car_detail = pick_unused("promo_car", PROMO_CARS)
    feature = pick_unused("promo_feature", PROMO_FEATURES)
    prompt = PROMO_PROMPT.format(
        route=f"{route_name} ({distance} км, ~{hours} ч)",
        car_type=f"{car_type}: {car_detail}",
        feature=feature,
    )
    text = await ask_ai(prompt, ai_key) or (
        f"🚕 {route_name}\n\nЕдем комфортно на {car_type.lower()}.\n"
        f"Особенность: {feature}.\n\n📞 Бронь: межгород.com | WhatsApp/Telegram"
    )
    text += "\n\n#межгород #такси #трансфер #grandtransfer"
    photo = await find_photo(["taxi car road", "intercity travel highway"], pexels_key)
    return text, photo


async def generate_route_tip(ai_key: str, pexels_key: str) -> Tuple[str, Optional[str]]:
    route_name, distance, hours = pick_unused("route_tip", ROUTES)
    prompt = ROUTE_TIP_PROMPT.format(route=route_name, distance=distance, hours=hours)
    text = await ask_ai(prompt, ai_key) or (
        f"🗺 Маршрут дня: {route_name}\n\nРасстояние {distance} км, в пути ~{hours} ч.\n"
        f"Door-to-Door доставка.\n\n📞 Бронь: межгород.com"
    )
    text += "\n\n#маршрут #межгород #такси #трансфер"
    dest = route_name.split("→")[-1].strip()
    keywords = next(
        (v for k, v in ROUTE_PHOTO_MAP.items() if k in dest),
        ["highway road trip", "intercity travel"],
    )
    photo = await find_photo(keywords, pexels_key)
    return text, photo


async def generate_travel_tip(ai_key: str, pexels_key: str) -> Tuple[str, Optional[str]]:
    topic = pick_unused("travel_tip", TRAVEL_TIPS)
    text = await ask_ai(TRAVEL_TIP_PROMPT.format(topic=topic), ai_key) or (
        f"💡 {topic}\n\n🚕 GrandTransfer — межгород.com"
    )
    text += "\n\n#советы #пассажирам #межгород"
    photo = await find_photo(["travel tips car road", "passenger journey advice"], pexels_key)
    return text, photo


async def generate_minivan(ai_key: str, pexels_key: str) -> Tuple[str, Optional[str]]:
    scenario = pick_unused("minivan", MINIVAN_SCENARIOS)
    text = await ask_ai(MINIVAN_PROMPT.format(scenario=scenario), ai_key) or (
        "🚐 Едете большой компанией?\n\nМинивэн до 7 мест — одна машина вместо двух.\n"
        "Дешевле и удобнее — все вместе, без пересадок.\n\n"
        "📞 Бронь: межгород.com | WhatsApp/Telegram"
    )
    text += "\n\n#минивэн #такси #межгород #компания"
    photo = await find_photo(["minivan family road trip", "passenger van travel"], pexels_key)
    return text, photo


async def generate_faq(ai_key: str, pexels_key: str) -> Tuple[str, Optional[str]]:
    question = pick_unused("faq", FAQ_QUESTIONS)
    text = await ask_ai(FAQ_PROMPT.format(question=question), ai_key) or (
        f"❓ {question}\n\nНапишите нам в ЛС — ответим!\n\n🚕 GrandTransfer — межгород.com"
    )
    text += "\n\n#вопросответ #межгород #такси"
    photo = await find_photo(["taxi passenger car", "transport service"], pexels_key)
    return text, photo
