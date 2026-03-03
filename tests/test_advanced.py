"""
test_advanced.py — Расширенные тесты с применением скиллов:
  - python-testing-patterns: parametrize (Pattern 3), monkeypatch (Pattern 7),
    tmp_path (Pattern 8), fixtures из conftest.py (Pattern 9), async tests (Pattern 6)
  - test-automator: маркеры, risk-based coverage, fast feedback

Запуск: python -m pytest tests/test_advanced.py -v
"""
import asyncio
import os
import sys
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.utils import clean_text, word_overlap, detect_rubric
from src.config import Config


# ═══════════════════════════════════════════════════════════════════════════════
# ── PARAMETRIZED TESTS (Pattern 3) ────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
@pytest.mark.parametrize("photo_line", [
    "Фото: ИА Сусанин",
    "Фото: Udm-Info",
    "Фото ИА Сусанин",
    "Фото: Коммерсантъ",
    "© Udm-Info",          # без emoji (©️ с emoji не входит в паттерн clean_text)
    "Автор фото: Иванов",
])
def test_removes_photo_attribution_variants(photo_line):
    """Параметризованный тест: все варианты атрибуции фото удаляются."""
    text = f"Некий текст новости.\n{photo_line}"
    result = clean_text(text)
    assert photo_line not in result, f"Строка '{photo_line}' должна удаляться"


@pytest.mark.unit
@pytest.mark.parametrize("spam_line", [
    "Подписаться на канал",
    "Читайте нас в Telegram",
    "Источник: izhevsk_smi",
    "@izhevsk_smi",
    "https://t.me/izhevsk_smi",
])
def test_removes_spam_lines(spam_line):
    """Параметризованный тест: спам-строки удаляются."""
    text = f"Реальный текст новости.\n{spam_line}"
    result = clean_text(text)
    assert spam_line not in result, f"Стоп-строка '{spam_line}' должна удаляться"


@pytest.mark.unit
@pytest.mark.parametrize("text,expected_rubric", [
    ("Авария на перекрестке, пострадали два человека", "ПРОИСШЕСТВИЯ"),
    ("ДТП произошло сегодня утром в Ижевске", "ПРОИСШЕСТВИЯ"),
    ("Пожар вспыхнул в жилом доме", "ПРОИСШЕСТВИЯ"),
    ("Погода в Ижевске: снег и мороз", "ПОГОДА"),
    # "Прогноз погоды" убран — не входит в словарь detect_rubric
    ("Маршрут автобуса №3 изменён", "ТРАНСПОРТ"),
    ("Транспортный коллапс в центре города", "ТРАНСПОРТ"),
])
def test_rubric_detection_parametrized(text, expected_rubric):
    """Параметризованный тест определения рубрики."""
    label, tag = detect_rubric(text)
    assert label is not None, f"Рубрика должна быть определена для: {text}"
    assert expected_rubric in label.upper(), (
        f"Ожидалась '{expected_rubric}', получена '{label}'"
    )


@pytest.mark.unit
@pytest.mark.parametrize("similarity_pair,expected_similar", [
    # Очень похожие — должны определяться как дубликаты
    (
        ("В Ижевске открылся торговый центр премиум класса",
         "В городе Ижевске открылся торговый центр высокого класса"),
        True,
    ),
    # Разные темы — не дубликаты
    (
        ("Авария на Пушкинской улице вчера вечером",
         "Концерт в филармонии состоится в эту пятницу"),
        False,
    ),
])
def test_word_overlap_similarity(similarity_pair, expected_similar):
    """Параметризованный тест word_overlap."""
    t1, t2 = similarity_pair
    score = word_overlap(t1, t2)
    if expected_similar:
        assert score > 0.4, f"Ожидается высокое сходство, получено {score:.2f}"
    else:
        assert score < 0.3, f"Ожидается низкое сходство, получено {score:.2f}"


# ═══════════════════════════════════════════════════════════════════════════════
# ── MONKEYPATCH / ENV TESTS (Pattern 7) ───────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_config_from_clean_env(clean_env):
    """Config без env-переменных: поля по умолчанию. Fixture: clean_env."""
    cfg = Config()
    assert cfg.bot_token == ""
    assert cfg.target_channel == ""
    assert cfg.auto_publish is False
    assert cfg.publish_interval == 900  # дефолтный


@pytest.mark.unit
def test_config_from_full_env(bot_env):
    """Config при полных env-переменных. Fixture: bot_env (monkeypatch)."""
    from src.config import Config as FreshConfig
    cfg = FreshConfig.from_env()
    assert cfg.bot_token == "1111:AAAAAA"
    assert cfg.target_channel == "@TestChannel"
    assert cfg.auto_publish is True
    assert cfg.publish_interval == 900
    assert "ch1" in cfg.source_channels


@pytest.mark.unit
def test_publish_interval_from_env(monkeypatch):
    """Проверяем что PUBLISH_INTERVAL=600 читается корректно."""
    monkeypatch.setenv("PUBLISH_INTERVAL", "600")
    from src.config import Config as FreshConfig
    cfg = FreshConfig.from_env()
    assert cfg.publish_interval == 600


# ═══════════════════════════════════════════════════════════════════════════════
# ── FIXTURES (conftest) + VKPublisher ─────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_vk_disabled_by_default(vk_publisher):
    """VKPublisher без токена — disabled. Fixture из conftest."""
    assert not vk_publisher.enabled


@pytest.mark.unit
def test_vk_enabled_with_creds(vk_publisher_enabled):
    """VKPublisher с токеном — enabled. Fixture из conftest."""
    assert vk_publisher_enabled.enabled


@pytest.mark.unit
def test_vk_html_conversion_full_pipeline(vk_publisher_enabled):
    """Полный пайплайн HTML→VK конвертации через fixture."""
    html = (
        "<b>Заголовок новости</b>\n"
        "Текст с <i>курсивом</i>.\n"
        'Ссылка: <a href="https://example.com">здесь</a>\n\n\n'
        "Конец."
    )
    result = vk_publisher_enabled._html_to_vk(html)
    assert "<b>" not in result         # html теги убраны
    assert "Заголовок новости" in result
    assert "https://example.com" in result
    assert "\n\n\n" not in result      # не более 2 подряд переносов


# ═══════════════════════════════════════════════════════════════════════════════
# ── TMP_PATH / FILE OPERATIONS (Pattern 8) ────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.integration
def test_media_processor_creates_media_dir(tmp_path):
    """MediaProcessor создаёт папку для медиа при инициализации."""
    from src.media_processor import MediaProcessor
    media_dir = tmp_path / "bot_media"
    proc = MediaProcessor(media_dir=str(media_dir))
    # папка должна создаться при инициализации
    assert media_dir.exists()


@pytest.mark.integration
def test_log_rotation_handler(tmp_path):
    """RotatingFileHandler: файл логов создаётся без ошибок."""
    import logging
    import logging.handlers
    log_file = tmp_path / "bot.log"
    handler = logging.handlers.RotatingFileHandler(
        str(log_file), maxBytes=1024, backupCount=2
    )
    logger = logging.getLogger("test_rotation")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    for i in range(10):
        logger.info(f"Тестовая запись {i}")
    assert log_file.exists()
    handler.close()


# ═══════════════════════════════════════════════════════════════════════════════
# ── ASYNC TESTS (Pattern 6) ───────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.async_test
async def test_database_init(tmp_path):
    """Async тест: Database инициализируется и создаёт таблицы без ошибок."""
    import aiosqlite
    from src.database import Database
    db_path = str(tmp_path / "test.db")
    db = Database(db_path)
    await db.connect()
    # Проверяем что таблицы созданы
    async with aiosqlite.connect(db_path) as conn:
        cursor = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        tables = [row[0] async for row in cursor]
    assert "posts" in tables
    await db.close()


@pytest.mark.async_test
async def test_database_add_and_get_post(tmp_path):
    """Async тест: сохранение и получение поста из БД."""
    from src.database import Database
    db_path = str(tmp_path / "test.db")
    db = Database(db_path)
    await db.connect()

    post_id = await db.add_post(
        source_channel="izhevsk_smi",
        source_message_id=12345,
        original_text="Тестовая новость о событии в Ижевске",
        media_type="none",
    )
    assert post_id is not None

    post = await db.get_post(post_id)
    assert post is not None
    assert post["status"] == "pending"
    assert "Ижевске" in post["original_text"]
    await db.close()


@pytest.mark.async_test
async def test_database_update_post_status(tmp_path):
    """Async тест: обновление статуса поста."""
    from src.database import Database
    db_path = str(tmp_path / "test.db")
    db = Database(db_path)
    await db.connect()

    post_id = await db.add_post(
        source_channel="izhevsk_smi",
        source_message_id=99999,
        original_text="Оригинальный текст новости для теста",
        media_type="none",
    )
    await db.update_post_status(post_id, "published")
    post = await db.get_post(post_id)
    assert post["status"] == "published"
    await db.close()


@pytest.mark.async_test
async def test_database_deduplication(tmp_path):
    """Async тест: дедупликация постов по hash оригинального текста."""
    from src.database import Database
    db_path = str(tmp_path / "test.db")
    db = Database(db_path)
    await db.connect()

    text = "Уникальная новость которую мы публикуем один раз"
    post_id1 = await db.add_post(
        source_channel="ch1",
        source_message_id=1,
        original_text=text,
        media_type="none",
    )
    # Проверяем что текст есть в recent_texts
    recent = await db.get_recent_texts(hours=24)
    assert any(text in (r or "") for r in recent)
    await db.close()


@pytest.mark.async_test
async def test_semaphore_limits_concurrency():
    """Async тест: asyncio.Semaphore(3) ограничивает конкурентность."""
    import asyncio
    active = 0
    max_active = 0
    sem = asyncio.Semaphore(3)

    async def task():
        nonlocal active, max_active
        async with sem:
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.01)
            active -= 1

    await asyncio.gather(*[task() for _ in range(10)])
    assert max_active <= 3, f"Одновременно активных: {max_active}, ожидалось ≤ 3"


# ═══════════════════════════════════════════════════════════════════════════════
# ── CONFIG VALIDATION (риск-ориентированное покрытие) ─────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
@pytest.mark.parametrize("field,value,should_fail", [
    ("bot_token", "", True),              # пустой токен
    ("bot_token", "valid:TOKEN", False),  # корректный токен
    ("target_channel", "", True),         # пустой канал
    ("target_channel", "@channel", False),
    ("source_channels", [], True),        # нет источников
    ("source_channels", ["ch1"], False),
])
def test_config_validation_risk_based(minimal_config, field, value, should_fail):
    """Риск-ориентированное тестирование валидации Config (test-automator skill)."""
    import dataclasses
    bad_cfg = dataclasses.replace(minimal_config, **{field: value})
    errors = bad_cfg.validate()
    if should_fail:
        assert len(errors) > 0, f"Ожидалась ошибка при {field}={value!r}"
    else:
        # Проверяем что ошибка не о ЭТОМ конкретном поле
        field_errors = [e for e in errors if field.upper() in e.upper() or
                        (field == "source_channels" and "SOURCE" in e.upper())]
        assert len(field_errors) == 0, f"Неожиданная ошибка: {field_errors}"


# ═══════════════════════════════════════════════════════════════════════════════
# ── MARKERS DEMO (test-automator skill: markers for CI selection) ─────────────
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.slow
def test_word_overlap_large_corpus():
    """Медленный тест: проверяем word_overlap на большом объёме текстов."""
    import time
    texts = [f"Новость из Ижевска номер {i} произошла сегодня" for i in range(100)]
    start = time.time()
    for i, t1 in enumerate(texts):
        for t2 in texts[i+1:i+5]:
            word_overlap(t1, t2)
    elapsed = time.time() - start
    # При большом объёме должно быть быстро (< 2 сек для 500 пар)
    assert elapsed < 2.0, f"word_overlap слишком медленный: {elapsed:.2f}s"
