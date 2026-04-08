<div align="center">
  <h1>🤖 IzhevskToday (News Aggregator Bot)</h1>
  <p><b>Автоматический Telegram-бот для мониторинга новостных каналов, AI-рерайта и публикации</b></p>

  [![Python](https://img.shields.io/badge/Python-3.11+-blue.svg?style=flat&logo=python&logoColor=white)](https://www.python.org)
  [![Aiogram](https://img.shields.io/badge/Aiogram-3.x-2CA5E0.svg?style=flat&logo=telegram&logoColor=white)](https://docs.aiogram.dev/en/latest/)
  [![Telethon](https://img.shields.io/badge/Telethon-MTProto-0088cc.svg?style=flat&logo=telegram&logoColor=white)](https://docs.telethon.dev/)
  [![Docker](https://img.shields.io/badge/Docker-Ready-2496ED.svg?style=flat&logo=docker&logoColor=white)](https://www.docker.com/)
</div>

---

## 📖 О проекте

Проект представляет собой полностью автономную систему для агрегации, обработки нейросетями и автоматической публикации новостей в Telegram-канал **[@IzhevskTodayNews](https://t.me/IzhevskTodayNews)**, ВКонтакте и Одноклассники (MAX).

### 🔥 Ключевые возможности

*   📡 **Мониторинг каналов**: Telethon парсит новости из 7+ заданных каналов.
*   🧠 **AI-рерайт**: Использует каскад моделей (AITUNNEL GPT-4o-mini → Gemini → Groq → YandexGPT → ReText.AI) благодаря встроенному **Circuit Breaker**.
*   🖼️ **Умный сток медиа**: Динамический поиск и загрузка фотографий по сгенерированным AI-тегам (Pexels → Pixabay → Wikimedia Commons).
*   🔍 **AI Фильтрация**: Гео-фильтр (только Ижевск/Удмуртия), проверка релевантности и срочности.
*   ✅ **Режим модерации**: Inline-кнопки (Одобрить / Редактировать / Рерайт / Удалить) перед публикацией, с единым Telegram-меню для админа.
*   📰 **Авто-рубрики**: Генерация контента по расписанию (Погода, рецепты, факты, лайфхаки, видео с котиками в VK Клипы).

---

## 🛠 Технический стек и Архитектура

```text
📁 news-aggregator-bot
├── 📂 src/
│   ├── main.py              # Точка входа в систему
│   ├── config.py             # Загрузка и валидация ENV
│   ├── database.py           # SQLite - хранение постов и тем
│   ├── channel_monitor.py    # Парсинг Telegram-каналов доноров
│   ├── ai_rewriter.py        # Каскад нейросетей для умного рерайта
│   ├── media_processor.py    # Подбор картинок + анализ вотермарок
│   ├── content_generator.py  # Генерация утренних/вечерних дайджестов
│   ├── content_scheduler.py  # Планировщик рубрик по времени
│   ├── vk_publisher.py       # API интеграция с VK (Посты/Клипы/Сторис)
│   ├── max_publisher.py      # Интеграция с Одноклассниками
│   └── bot.py                # Интерфейс админа (Aiogram)
├── 📂 data/                  # База данных, сессии Telethon, used_topics.json
└── 📂 media/                 # Временное хранение картинок для постов
```

> **Архитектурный пайплайн:** Источники → Мониторинг → AI-Фильтр → AI-Рерайт → Подбор Фото → Админ(Модерация) → Мульти-Кросспостинг

---

## 📱 Меню бота и Команды управления

Панель управления реализована прямо в Telegram:

| Команда | Описание функции в меню |
| :--- | :--- |
| `/start` | 🎛 Главный дашборд. Текущий статус бота. |
| `/queue` | 📝 Список новостей в очереди на проверку админом. |
| `/stats` | 📊 Статистика опубликованного за неделю/месяц. |
| `/sources` | ➕ Управление каналами-донорами (добавить/удалить). |
| `/publish` | 🚀 Принудительно разослать всё уже одобренное. |

Под каждым новым постом админу выводятся **Кнопки действий**:
*   ✅ `Одобрить`
*   ✏️ `Изменить текст`
*   🔄 `Переписать нейросетью`
*   ❌ `Отклонить`

---

## 🚀 Установка и Базовые настройки

### 1. API ключи (Заполнить `.env`)

| Ключ | Сервис | Обязательность |
| :--- | :--- | :---: |
| `BOT_TOKEN` | [@BotFather](https://t.me/BotFather) | ✅ |
| `TELEGRAM_API_ID/HASH` | [my.telegram.org](https://my.telegram.org/apps) | ✅ |
| `AITUNNEL_API_KEY` | [AITunnel](https://aitunnel.ru) | ✅ (Основа) |
| `GEMINI/GROQ/YANDEX_KEY`| LLM Провайдеры | ⚡ Fallbacks |
| `PEXELS/PIXABAY_KEY` | Фотостоки | 📷 Рекомендуется |
| `VK_ACCESS_TOKEN` | Управление группой VK | 📤 Опционально |

### 2. Запуск локально

```bash
cp .env.example .env
# Заполните ключи в файле .env

pip install -r requirements.txt
python -m src.main
```
*(При первом запуске Telethon запросит OTP код для авторизации userbot)*

### 3. Запуск в Docker

```bash
docker-compose up -d --build
```
> Используется **GitHub Actions** → Docker image → VPS (Dokploy). Пуш в `main` автоматически выкатывает контейнер `ghcr.io/xyling12/news-aggregator-bot:main`.

---

## 💰 Экономика бота

Система спроектирована для максимальной экономии:
*   **LLM Токены**: Основной поток идет через `AITUNNEL` (~300₽/мес). Резерв бесплатный (Gemini/Groq).
*   **Фото**: Бесплатные API (Pexels, Pixabay, WMC).
*   **Сервер**: Обычный VPS (~300₽/мес).
**Итого:** Полностью автономное СМИ за **~600₽/мес**.
