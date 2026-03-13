# 🤖 News Aggregator Bot — Ижевск Сегодня

Автоматический Telegram-бот для мониторинга новостных каналов, AI-рерайта и публикации в канал **[@IzhevskTodayNews](https://t.me/IzhevskTodayNews)**.

## 🔥 Возможности

- 📡 **Мониторинг каналов** — Telethon, 7+ источников
- 🧠 **AI-рерайт** — AITUNNEL (GPT-4o-mini) → Gemini → Groq → YandexGPT → ReText.AI
- 🖼️ **Стоковые фото** — Pexels → Pixabay → Wikimedia Commons (автоматический поиск)
- 🔍 **Фильтрация** — гео-фильтр, AI-проверка релевантности и срочности
- ✅ **Модерация** — кнопки одобрения, редактирования, перерайта
- 📤 **Кросс-постинг** — VK, MAX (OK Мессенджер)
- 📰 **Генерация контента** — погода, рецепты (60+), факты, лайфхаки, места, праздники
- 📊 **Дайджест** — ежедневный вечерний обзор новостей
- 🛡️ **Circuit Breaker** — автопереключение между AI-движками при сбоях

## 🚀 Быстрый старт

### 1. API ключи

| Ключ | Где получить | Обязательно |
|------|-------------|:-----------:|
| `BOT_TOKEN` | [@BotFather](https://t.me/BotFather) | ✅ |
| `TELEGRAM_API_ID` / `API_HASH` | [my.telegram.org](https://my.telegram.org/apps) | ✅ |
| `AITUNNEL_API_KEY` | [aitunnel.ru](https://aitunnel.ru) | ✅ |
| `GEMINI_API_KEYS` | [aistudio.google.com](https://aistudio.google.com) | ⚡ fallback |
| `PEXELS_API_KEY` | [pexels.com/api](https://www.pexels.com/api/) | 📷 рекомендуется |
| `YANDEX_API_KEY` | [console.yandex.cloud](https://console.yandex.cloud) | ⚡ fallback |
| `VK_ACCESS_TOKEN` | VK API | 📤 опционально |

### 2. Настройка

```bash
cp .env.example .env
# Заполните ключи в .env
```

### 3. Запуск

```bash
# Локально
pip install -r requirements.txt
python -m src.main

# Docker
docker-compose up -d
```

> ⚠️ При первом запуске Telethon попросит номер телефона + OTP. Сессия сохранится в `data/`.

## 📋 Команды бота

| Команда | Описание |
|---------|----------|
| `/start` | Главное меню |
| `/queue` | Очередь на модерации |
| `/stats` | Статистика |
| `/sources` | Управление источниками |
| `/publish` | Опубликовать одобренные |
| `/help` | Справка |

## 🔄 Архитектура

```
Источники (7 каналов) → Telethon мониторинг
    ↓
Фильтрация (гео + AI релевантность + срочность)
    ↓
AI рерайт (AITUNNEL → Gemini → Groq → YandexGPT)
    ↓
Стоковое фото (Pexels → Pixabay → Wikimedia)
    ↓
Модерация (кнопки: ✅/✏️/🔄/❌)
    ↓
Публикация → Telegram + VK + MAX
```

## 📁 Структура

```
news-aggregator-bot/
├── src/
│   ├── main.py              # Точка входа
│   ├── config.py             # Конфигурация
│   ├── database.py           # SQLite БД
│   ├── channel_monitor.py    # Мониторинг каналов (Telethon)
│   ├── ai_rewriter.py        # AI рерайт (AITUNNEL/Gemini/Groq/YandexGPT)
│   ├── media_processor.py    # Стоковые фото + водяные знаки
│   ├── content_generator.py  # Генерация рубрик (погода, рецепты, факты)
│   ├── content_scheduler.py  # Расписание публикации рубрик
│   ├── vk_publisher.py       # Кросс-постинг в VK
│   ├── max_publisher.py      # Кросс-постинг в MAX (ОК)
│   └── bot.py                # Telegram бот (Aiogram 3)
├── data/                     # БД, сессии, used_topics.json
├── media/                    # Скачанные медиафайлы
├── .env.example
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## 💰 Стоимость

| Сервис | Цена | Использование |
|--------|------|---------------|
| AITUNNEL (GPT-4o-mini) | ~300₽/мес | AI рерайт, рубрики, фильтрация |
| Pexels | Бесплатно | 200 запросов/час |
| Gemini | Бесплатно | Fallback AI |
| Groq | Бесплатно | Fallback AI |
| VPS (Beget) | ~300₽/мес | Хостинг |

**Итого: ~600₽/мес** за полностью автоматический новостной канал.

## 🛠️ Деплой

Деплой через **GitHub Actions** → Docker image → VPS (Dokploy).

При пуше в `main` автоматически собирается образ `ghcr.io/xyling12/news-aggregator-bot:main`.

---

📲 **Канал**: [@IzhevskTodayNews](https://t.me/IzhevskTodayNews)
