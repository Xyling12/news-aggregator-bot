#!/bin/sh
# init-session.sh — Interactive Telethon session initialization
# Run this inside Docker to create the session file
# Usage: docker exec -it news-bot python -m src.init_session

import asyncio
from telethon import TelegramClient
from src.config import Config

async def main():
    config = Config.from_env()
    print("=" * 50)
    print("🔐 Telethon Session Initialization")
    print("=" * 50)
    print()
    print("Вам нужно ввести номер телефона и код из Telegram.")
    print("Это нужно только ОДИН раз.")
    print()

    client = TelegramClient(
        config.session_name,
        config.api_id,
        config.api_hash,
    )

    await client.start()
    me = await client.get_me()
    print(f"\n✅ Сессия создана! Вы {me.first_name} (ID: {me.id})")
    await client.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
