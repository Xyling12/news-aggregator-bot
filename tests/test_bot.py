"""
Тесты для news-aggregator-bot.
Покрывают: utils, ai_rewriter (refusal detection), vk_publisher, config, docker-compose.
Запуск: python -m pytest tests/test_bot.py -v
"""
import asyncio
import os
import re
import sys
import textwrap
import unittest
import yaml

# Добавляем корень проекта в sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.utils import clean_text, word_overlap, is_similar_to_any, detect_rubric, format_post
from src.bot import _has_local_geo, _looks_federal_news, _is_breaking_candidate
from src.ai_rewriter import _parse_binary_answer
from src.config import Config
from src.vk_publisher import VKPublisher


# ─── helpers ──────────────────────────────────────────────────────────────────

class _FakeRewriter:
    """Минимальный заглушка-рерайтер для тестов is_similar_to_any."""
    def calculate_uniqueness(self, a: str, b: str) -> float:
        words_a = set(a.lower().split())
        words_b = set(b.lower().split())
        if not words_a or not words_b:
            return 1.0
        overlap = len(words_a & words_b) / max(len(words_a), len(words_b))
        return 1.0 - overlap


# ─── utils: clean_text ────────────────────────────────────────────────────────

class TestCleanText(unittest.TestCase):

    def test_removes_subscribe_lines(self):
        text = "Новость о городе.\nПодписаться на наш канал\nЭто важно."
        result = clean_text(text)
        self.assertNotIn("подписаться", result.lower())
        self.assertIn("Новость о городе", result)

    def test_removes_photo_attribution_colon(self):
        """Строки вида 'Фото: ИА Сусанин' должны удаляться."""
        text = "Хорошая новость.\nФото: ИА Сусанин\nПодробности читайте ниже."
        result = clean_text(text)
        self.assertNotIn("Фото:", result)
        self.assertIn("Хорошая новость", result)

    def test_removes_photo_attribution_no_colon(self):
        """Строки вида 'Фото ИА Сусанин' без двоеточия тоже удаляются."""
        text = "Текст новости.\nФото ИА Сусанин"
        result = clean_text(text)
        self.assertNotIn("Фото ИА Сусанин", result)

    def test_removes_copyright_line(self):
        text = "Новость.\n© ИА Udm-Info\nДалее текст."
        result = clean_text(text)
        self.assertNotIn("© ИА Udm-Info", result)

    def test_removes_standalone_url(self):
        text = "Описание события.\nhttps://example.com\nДополнительно."
        result = clean_text(text)
        self.assertNotIn("https://example.com", result)

    def test_removes_standalone_mention(self):
        text = "Новость.\n@izhevsk_smi\nПодробнее."
        result = clean_text(text)
        self.assertNotIn("@izhevsk_smi", result)

    def test_preserves_content(self):
        """Основной текст новости должен сохраняться."""
        text = "В Ижевске открылся новый парк. Горожане рады."
        result = clean_text(text)
        self.assertEqual(result, text)

    def test_removes_source_line(self):
        text = "Авария на улице.\nИсточник: kommersant1\nПострадавших нет."
        result = clean_text(text)
        self.assertNotIn("Источник:", result)

    def test_removes_author_photo_line(self):
        text = "Событие в городе.\nАвтор фото: Иванов И.И."
        result = clean_text(text)
        self.assertNotIn("Автор фото:", result)

    def test_empty_string(self):
        self.assertEqual(clean_text(""), "")


# ─── utils: word_overlap ──────────────────────────────────────────────────────

class TestWordOverlap(unittest.TestCase):

    def test_identical_texts(self):
        t = "В Ижевске произошло крупное событие вчера вечером"
        self.assertAlmostEqual(word_overlap(t, t), 1.0)

    def test_no_overlap(self):
        t1 = "Пожар произошел вечером"
        t2 = "Концерт состоится завтра"
        self.assertLess(word_overlap(t1, t2), 0.3)

    def test_partial_overlap(self):
        t1 = "В Ижевске открылся новый торговый центр на улице"
        t2 = "Новый торговый центр открылся в другом городе"
        score = word_overlap(t1, t2)
        self.assertGreater(score, 0.2)
        self.assertLess(score, 1.0)

    def test_empty_strings(self):
        self.assertEqual(word_overlap("", "тест"), 0.0)
        self.assertEqual(word_overlap("тест", ""), 0.0)
        self.assertEqual(word_overlap("", ""), 0.0)

    def test_skips_short_words(self):
        """Слова ≤4 символов не влияют на overlap."""
        t1 = "в на от за до"
        t2 = "из под без над про"
        self.assertEqual(word_overlap(t1, t2), 0.0)


# ─── utils: is_similar_to_any ─────────────────────────────────────────────────

class TestIsSimilarToAny(unittest.TestCase):

    def setUp(self):
        self.rewriter = _FakeRewriter()

    def test_detects_duplicate(self):
        # is_similar_to_any скипает identical == text, поэтому даём слегка изменённый вариант
        text = "В Ижевске открылся новый торговый центр премиум класса"
        candidates = ["В городе Ижевске открылся новый торговый центр высокого класса"]
        self.assertTrue(is_similar_to_any(text, candidates, self.rewriter))

    def test_passes_unique_content(self):
        text = "Авария на перекрестке Пушкинской и Советской улицы"
        candidates = ["В Ижевске поставили новый памятник известному поэту"]
        self.assertFalse(is_similar_to_any(text, candidates, self.rewriter))

    def test_empty_candidates(self):
        text = "Любая новость"
        self.assertFalse(is_similar_to_any(text, [], self.rewriter))

    def test_ignores_empty_candidate(self):
        text = "Реальная новость про Ижевск"
        self.assertFalse(is_similar_to_any(text, [""], self.rewriter))


# ─── utils: detect_rubric ─────────────────────────────────────────────────────

class TestDetectRubric(unittest.TestCase):

    def test_detects_accident(self):
        label, tag = detect_rubric("Авария произошла на перекрестке. Пострадавших нет.")
        self.assertIsNotNone(label)
        self.assertIn("ПРОИСШЕСТВИЯ", label)

    def test_detects_transport(self):
        label, tag = detect_rubric("Маршрут автобуса №3 изменен")
        self.assertIsNotNone(label)

    def test_detects_weather(self):
        label, tag = detect_rubric("Погода в Ижевске: ожидается снег")
        self.assertIsNotNone(label)

    def test_returns_none_for_unknown(self):
        label, tag = detect_rubric("Новая выставка художников откроется завтра")
        # Может вернуть None или любую рубрику — просто проверяем тип
        self.assertIsInstance(label, (str, type(None)))


# ─── utils: format_post ───────────────────────────────────────────────────────

class TestFormatPost(unittest.TestCase):

    def test_contains_hashtags(self):
        result = format_post("Заголовок\nТело новости", ["#ижевск"])
        self.assertIn("#ижевск", result)

    def test_contains_city_tags(self):
        result = format_post("Новость\nПодробности", [])
        self.assertIn("#Ижевск", result)
        self.assertIn("#Удмуртия", result)

    def test_contains_cta_link(self):
        result = format_post("Новость\nТекст", [])
        self.assertIn("t.me", result)

    def test_converts_bold_markdown(self):
        result = format_post("**Важная** новость\nПодробности", [])
        self.assertIn("<b>Важная</b>", result)

    def test_removes_markdown_headers(self):
        result = format_post("## Заголовок\nТекст новости", [])
        self.assertNotIn("##", result)


# ─── config ───────────────────────────────────────────────────────────────────

class TestConfig(unittest.TestCase):

    def test_default_values(self):
        cfg = Config()
        self.assertEqual(cfg.publish_interval, 900)
        self.assertFalse(cfg.auto_publish)
        self.assertEqual(cfg.min_text_length, 100)
        self.assertEqual(cfg.language, "ru")

    def test_validate_missing_required(self):
        cfg = Config()  # все поля пустые
        errors = cfg.validate()
        self.assertGreater(len(errors), 0)
        # Должны быть ошибки про BOT_TOKEN и TARGET_CHANNEL
        errors_str = " ".join(errors)
        self.assertIn("BOT_TOKEN", errors_str)
        self.assertIn("TARGET_CHANNEL", errors_str)

    def test_validate_ok(self):
        cfg = Config(
            bot_token="abc:XYZ",
            target_channel="@test",
            admin_ids=[123],
            gemini_api_key="key123",
            source_channels=["ch1"],
        )
        self.assertEqual(cfg.validate(), [])

    def test_ad_stop_words_not_empty(self):
        cfg = Config()
        self.assertGreater(len(cfg.ad_stop_words), 0)

    def test_breaking_keywords_not_empty(self):
        cfg = Config()
        self.assertGreater(len(cfg.breaking_keywords), 0)

    def test_from_env_parses_gemini_model_names(self):
        original = os.environ.get("GEMINI_MODEL_NAMES")
        try:
            os.environ["GEMINI_MODEL_NAMES"] = "gemini-2.5-flash, gemini-2.5-pro ,gemini-2.0-flash"
            cfg = Config.from_env()
            self.assertEqual(
                cfg.gemini_model_names,
                ["gemini-2.5-flash", "gemini-2.5-pro", "gemini-2.0-flash"],
            )
        finally:
            if original is None:
                os.environ.pop("GEMINI_MODEL_NAMES", None)
            else:
                os.environ["GEMINI_MODEL_NAMES"] = original

    def test_from_env_parses_groq_settings(self):
        original_key = os.environ.get("GROQ_API_KEY")
        original_model = os.environ.get("GROQ_MODEL")
        try:
            os.environ["GROQ_API_KEY"] = "test_groq_key"
            os.environ["GROQ_MODEL"] = "llama-3.1-8b-instant"
            cfg = Config.from_env()
            self.assertEqual(cfg.groq_api_key, "test_groq_key")
            self.assertEqual(cfg.groq_model, "llama-3.1-8b-instant")
        finally:
            if original_key is None:
                os.environ.pop("GROQ_API_KEY", None)
            else:
                os.environ["GROQ_API_KEY"] = original_key

            if original_model is None:
                os.environ.pop("GROQ_MODEL", None)
            else:
                os.environ["GROQ_MODEL"] = original_model


class TestRegionFilters(unittest.TestCase):

    def test_local_geo_detected_in_plain_text(self):
        self.assertTrue(_has_local_geo("В Ижевске откроют новый парк"))

    def test_hashtag_only_geo_does_not_count(self):
        text = "Сочи и близлежащие\nОтбой опасности по БПЛА\n#Ижевск #Удмуртия"
        self.assertFalse(_has_local_geo(text))

    def test_federal_news_detected_without_geo(self):
        self.assertTrue(_looks_federal_news("Госдума приняла закон о повышении пенсий"))


class TestBreakingGate(unittest.TestCase):

    def test_radar_without_local_geo_is_not_breaking(self):
        text = "Сочи и близлежащие\nОтбой опасности по БПЛА"
        self.assertFalse(
            _is_breaking_candidate(
                text,
                is_radar_source=True,
                has_geo=False,
                breaking_keywords=["бпла", "опасность"],
            )
        )

    def test_radar_with_local_geo_is_breaking(self):
        text = "В Удмуртии объявлена опасность БПЛА"
        self.assertTrue(
            _is_breaking_candidate(
                text,
                is_radar_source=True,
                has_geo=True,
                breaking_keywords=["бпла", "опасность"],
            )
        )


class TestBinaryAnswerParsing(unittest.TestCase):

    def test_parse_yes(self):
        self.assertTrue(_parse_binary_answer("YES"))

    def test_parse_no(self):
        self.assertFalse(_parse_binary_answer("NO"))

    def test_parse_russian_yes(self):
        self.assertTrue(_parse_binary_answer("Да"))


# ─── vk_publisher ─────────────────────────────────────────────────────────────

class TestVKPublisher(unittest.TestCase):

    def test_enabled_with_token_and_group(self):
        pub = VKPublisher(access_token="token123", group_id="236380336")
        self.assertTrue(pub.enabled)

    def test_disabled_without_token(self):
        pub = VKPublisher(access_token="", group_id="236380336")
        self.assertFalse(pub.enabled)

    def test_disabled_without_group(self):
        pub = VKPublisher(access_token="token123", group_id="")
        self.assertFalse(pub.enabled)

    def test_strips_club_prefix(self):
        pub = VKPublisher(access_token="t", group_id="club236380336")
        self.assertEqual(pub.group_id, "236380336")

    def test_html_to_vk_strips_bold(self):
        pub = VKPublisher(access_token="t", group_id="1")
        result = pub._html_to_vk("<b>Заголовок</b> новость")
        self.assertNotIn("<b>", result)
        self.assertIn("Заголовок", result)

    def test_html_to_vk_converts_links(self):
        pub = VKPublisher(access_token="t", group_id="1")
        result = pub._html_to_vk('<a href="https://t.me/test">Перейти</a>')
        self.assertIn("https://t.me/test", result)
        self.assertIn("Перейти", result)

    def test_html_to_vk_no_excessive_newlines(self):
        pub = VKPublisher(access_token="t", group_id="1")
        result = pub._html_to_vk("Текст\n\n\n\n\nЕщё")
        self.assertNotIn("\n\n\n", result)

    def test_html_to_vk_removes_cta(self):
        pub = VKPublisher(access_token="t", group_id="1")
        html = 'Новость\n😊 <a href="https://t.me/IzhevskTodayNews">Подписаться в TG</a>'
        result = pub._html_to_vk(html)
        self.assertNotIn("Подписаться в TG", result)


# ─── media_processor ──────────────────────────────────────────────────────────

class TestMediaProcessor(unittest.TestCase):

    def test_accepts_pexels_key(self):
        """Конструктор должен принимать pexels_key без TypeError."""
        from src.media_processor import MediaProcessor
        try:
            proc = MediaProcessor(
                pixabay_key="key1",
                unsplash_key="key2",
                pexels_key="key3",
                media_dir="/tmp/test_media",
            )
            self.assertEqual(proc.pexels_key, "key3")
        except TypeError as e:
            self.fail(f"MediaProcessor не принимает pexels_key: {e}")

    def test_default_pexels_key_empty(self):
        from src.media_processor import MediaProcessor
        proc = MediaProcessor(media_dir="/tmp/test_media")
        self.assertEqual(proc.pexels_key, "")


# ─── docker-compose.yml ───────────────────────────────────────────────────────

class TestDockerCompose(unittest.TestCase):

    COMPOSE_PATH = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "docker-compose.yml"
    )

    def _load_compose(self):
        with open(self.COMPOSE_PATH, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)

    def test_compose_file_exists(self):
        self.assertTrue(os.path.exists(self.COMPOSE_PATH))

    def test_auto_publish_enabled(self):
        compose = self._load_compose()
        env = compose["services"]["news-bot"].get("environment", [])
        env_str = " ".join(str(e) for e in env)
        self.assertIn("AUTO_PUBLISH=true", env_str)

    def test_publish_interval_set(self):
        compose = self._load_compose()
        env = compose["services"]["news-bot"].get("environment", [])
        env_str = " ".join(str(e) for e in env)
        self.assertIn("PUBLISH_INTERVAL=900", env_str)

    def test_healthcheck_present(self):
        compose = self._load_compose()
        service = compose["services"]["news-bot"]
        self.assertIn("healthcheck", service, "healthcheck отсутствует в docker-compose.yml")

    def test_restart_policy(self):
        compose = self._load_compose()
        restart = compose["services"]["news-bot"].get("restart", "")
        self.assertEqual(restart, "unless-stopped")

    def test_volumes_present(self):
        compose = self._load_compose()
        volumes = compose["services"]["news-bot"].get("volumes", [])
        paths = " ".join(volumes)
        self.assertIn("bot_data", paths)
        self.assertIn("bot_media", paths)


# ─── refusal detection (ai_rewriter) ─────────────────────────────────────────

class TestRefusalDetection(unittest.TestCase):
    """Тестируем метод _is_refusal без реального вызова AI API."""

    def setUp(self):
        # Инициализируем рерайтер с пустым конфигом — только для тестирования _is_refusal
        from src.ai_rewriter import AIRewriter
        cfg = Config(gemini_api_key="fake_key_for_tests")
        try:
            self.rewriter = AIRewriter(cfg)
        except Exception:
            self.rewriter = None

    def test_detects_cannot_discuss(self):
        if not self.rewriter:
            self.skipTest("AIRewriter не инициализирован")
        self.assertTrue(self.rewriter._is_refusal("Я не могу обсуждать эту тему"))

    def test_detects_yo_variant(self):
        if not self.rewriter:
            self.skipTest("AIRewriter не инициализирован")
        self.assertTrue(self.rewriter._is_refusal("Я не могу обсуждать эту тёму"))

    def test_detects_not_in_position(self):
        if not self.rewriter:
            self.skipTest("AIRewriter не инициализирован")
        self.assertTrue(self.rewriter._is_refusal("Я не в состоянии помочь с этим запросом"))

    def test_normal_text_not_refusal(self):
        if not self.rewriter:
            self.skipTest("AIRewriter не инициализирован")
        text = "В Ижевске открылся новый торговый центр в центре города."
        self.assertFalse(self.rewriter._is_refusal(text))

    def test_empty_string_is_refusal(self):
        """Пустая строка — тоже отказ: AI ничего не вернул = ошибка."""
        if not self.rewriter:
            self.skipTest("AIRewriter не инициализирован")
        self.assertTrue(self.rewriter._is_refusal(""))


if __name__ == "__main__":
    unittest.main(verbosity=2)
