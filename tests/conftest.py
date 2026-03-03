"""
conftest.py — Shared pytest fixtures.
Applied skill: python-testing-patterns / Pattern 9 (Custom Fixtures and Conftest)
"""
import os
import sys
import asyncio
import pytest

# Project root → sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import Config


# ─── Config fixtures ────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def minimal_config() -> Config:
    """Minimal valid Config — used by tests that need a real Config object."""
    return Config(
        bot_token="1234567890:AABBCCDDEEFFaabbccddeeff1234567890",
        target_channel="@IzhevskTodayNews",
        admin_ids=[376060133],
        gemini_api_key="test-gemini-key",
        source_channels=["izhevsk_smi", "izhevsktop1"],
    )

@pytest.fixture
def empty_config() -> Config:
    """Empty Config — used for validation tests (no required fields set)."""
    return Config()


# ─── VKPublisher fixtures ────────────────────────────────────────────────────

@pytest.fixture
def vk_publisher():
    """VKPublisher in disabled state (no token)."""
    from src.vk_publisher import VKPublisher
    return VKPublisher(access_token="", group_id="")

@pytest.fixture
def vk_publisher_enabled():
    """VKPublisher with fake creds — enabled but won't make real API calls."""
    from src.vk_publisher import VKPublisher
    return VKPublisher(access_token="fake_vk_token", group_id="236380336")


# ─── MediaProcessor fixtures ─────────────────────────────────────────────────

@pytest.fixture
def media_processor(tmp_path):
    """MediaProcessor with a real temp directory. Applied: tmp_path (Pattern 8)."""
    from src.media_processor import MediaProcessor
    return MediaProcessor(
        pixabay_key="",
        unsplash_key="",
        pexels_key="",
        media_dir=str(tmp_path / "media"),
    )


# ─── Environment fixtures ────────────────────────────────────────────────────

@pytest.fixture
def clean_env(monkeypatch):
    """Remove all bot env vars to test Config.from_env() isolation.
    Applied: monkeypatch (Pattern 7)."""
    keys = [
        "BOT_TOKEN", "TARGET_CHANNEL", "ADMIN_IDS", "GEMINI_API_KEY",
        "SOURCE_CHANNELS", "VK_ACCESS_TOKEN", "VK_GROUP_ID",
        "TELEGRAM_API_ID", "TELEGRAM_API_HASH", "AUTO_PUBLISH", "PUBLISH_INTERVAL",
    ]
    for k in keys:
        monkeypatch.delenv(k, raising=False)
    yield


@pytest.fixture
def bot_env(monkeypatch):
    """Set valid env vars — simulates Docker deployment environment."""
    monkeypatch.setenv("BOT_TOKEN", "1111:AAAAAA")
    monkeypatch.setenv("TARGET_CHANNEL", "@TestChannel")
    monkeypatch.setenv("ADMIN_IDS", "376060133")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("SOURCE_CHANNELS", "ch1,ch2")
    monkeypatch.setenv("AUTO_PUBLISH", "true")
    monkeypatch.setenv("PUBLISH_INTERVAL", "900")
    yield
