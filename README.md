# 🤖 News Aggregator Bot

Telegram бот для мониторинга публичных каналов, AI рерайта новостей и публикации в свой канал с модерацией.

## 🚀 Быстрый старт

### 1. Получите API ключи

| Ключ | Где получить |
|------|-------------|
| `TELEGRAM_API_ID`, `TELEGRAM_API_HASH` | [my.telegram.org/apps](https://my.telegram.org/apps) |
| `BOT_TOKEN` | [@BotFather](https://t.me/BotFather) |
| `GEMINI_API_KEY` | [aistudio.google.com](https://aistudio.google.com) |
| `UNSPLASH_ACCESS_KEY` (опционально) | [unsplash.com/developers](https://unsplash.com/developers) |

### 2. Настройте `.env`

```bash
cp .env.example .env
# Заполните все обязательные поля в .env
```

### 3. Первый запуск (локально)

```bash
pip install -r requirements.txt
python -m src.main
```

> ⚠️ При первом запуске Telethon попросит ввести номер телефона и OTP-код из Telegram. Это нужно сделать один раз — сессия сохранится в `data/news_bot_session.session`.

### 4. Docker

```bash
docker-compose up -d
```

## 📋 Команды бота

| Команда | Описание |
|---------|----------|
| `/start` | Главное меню |
| `/queue` | Очередь постов на модерации |
| `/stats` | Статистика |
| `/sources` | Управление каналами-источниками |
| `/publish` | Опубликовать все одобренные посты |
| `/help` | Справка |

## 🔄 Как это работает

```
1. Бот мониторит указанные каналы через Telethon
2. Новый пост → AI рерайт через Gemini API
3. Проверка фото на водяные знаки
4. Отправка админу на модерацию (с кнопками)
5. Админ: ✅ Опубликовать / ✏️ Редактировать / 🔄 Перерайт / ❌ Отклонить
6. Одобренный пост публикуется в целевой канал
```

## 📁 Структура проекта

```
news-bot/
├── src/
│   ├── __init__.py
│   ├── main.py              # Точка входа
│   ├── config.py             # Конфигурация
│   ├── database.py           # SQLite БД
│   ├── channel_monitor.py    # Мониторинг каналов (Telethon)
│   ├── ai_rewriter.py        # AI рерайт (Gemini/ReText)
│   ├── media_processor.py    # Обработка медиа
│   └── bot.py                # Telegram бот (Aiogram 3)
├── data/                     # БД и сессии
├── media/                    # Скачанные медиафайлы
├── .env.example
├── requirements.txt
├── Dockerfile
└── docker-compose.yml
```
