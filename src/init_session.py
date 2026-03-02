"""
Interactive Telethon session initialization.
Run inside Docker: docker exec -it news-bot python -m src.init_session
"""

import asyncio
import os
from telethon import TelegramClient
from src.config import Config


async def main():
    config = Config.from_env()
    print("=" * 50)
    print("🔐 Telethon Session Initialization")
    print("=" * 50)
    print()
    print("Введите номер телефона (например +79XXXXXXXXX).")
    print("Код придёт в Telegram-приложение (не SMS).")
    print("Это нужно только ОДИН раз.")
    print()

    # Use same path as channel_monitor.py
    session_path = os.path.join("data", config.session_name)
    os.makedirs("data", exist_ok=True)

    client = TelegramClient(
        session_path,
        config.api_id,
        config.api_hash,
    )

    await client.start()
    me = await client.get_me()
    print(f"\n✅ Сессия создана! Вы {me.first_name} (ID: {me.id})")
    print(f"📁 Файл сессии: {session_path}.session")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
