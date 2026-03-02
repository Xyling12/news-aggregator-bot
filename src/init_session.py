"""
Interactive Telethon session initialization with SMS fallback.
Run: docker exec -it news-bot python -m src.init_session
"""

import asyncio
import os
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from src.config import Config


async def main():
    config = Config.from_env()
    print("=" * 50)
    print("🔐 Telethon — Авторизация")
    print("=" * 50)
    print()

    session_path = os.path.join("data", config.session_name)
    os.makedirs("data", exist_ok=True)

    client = TelegramClient(
        session_path,
        config.api_id,
        config.api_hash,
    )

    await client.connect()

    # Check if already authorized
    if await client.is_user_authorized():
        me = await client.get_me()
        print(f"✅ Уже авторизован как {me.first_name} (ID: {me.id})")
        await client.disconnect()
        return

    # Get phone number
    phone = input("Введите номер телефона (+79XXXXXXXXX): ").strip()

    # Send code request
    sent = await client.send_code_request(phone, force_sms=False)
    print()
    print("📱 Код отправлен в Telegram-приложение.")
    print("   Проверьте чат с 'Telegram' (синяя галочка).")
    print()

    code = input("Введите код: ").strip()

    try:
        await client.sign_in(phone, code, phone_code_hash=sent.phone_code_hash)
    except SessionPasswordNeededError:
        # 2FA enabled
        print()
        print("🔒 На аккаунте включена двухфакторная аутентификация.")
        password = input("Введите пароль 2FA: ").strip()
        await client.sign_in(password=password)

    me = await client.get_me()
    print()
    print(f"✅ Авторизация успешна! Вы {me.first_name} (ID: {me.id})")
    print(f"📁 Сессия сохранена: {session_path}.session")
    print()
    print("Теперь перезапустите контейнер: docker restart news-bot")

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
