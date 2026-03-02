"""
Создаёт StringSession ЛОКАЛЬНО на вашем ПК.
Запуск: python create_session.py

Скопируйте полученную строку в Dokploy → Environment → TELETHON_SESSION
"""

import asyncio
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError

API_ID = 33112824
API_HASH = "7526146bb71fe8ca2fcf1b353f34425e"


async def main():
    print("=" * 50)
    print("🔐 Создание Telethon StringSession")
    print("=" * 50)
    print()

    client = TelegramClient(StringSession(), API_ID, API_HASH)
    await client.connect()

    phone = input("Введите номер телефона (+79XXXXXXXXX): ").strip()

    sent = await client.send_code_request(phone)
    print()
    print("📱 Код отправлен в Telegram!")
    print()

    code = input("Введите код из Telegram: ").strip()

    try:
        await client.sign_in(phone, code, phone_code_hash=sent.phone_code_hash)
    except SessionPasswordNeededError:
        print()
        print("🔒 Двухфакторная аутентификация включена.")
        password = input("Введите пароль 2FA: ").strip()
        await client.sign_in(password=password)

    me = await client.get_me()
    session_string = client.session.save()

    print()
    print(f"✅ Авторизация успешна! Вы: {me.first_name}")
    print()
    print("=" * 50)
    print("📋 СКОПИРУЙТЕ ЭТУ СТРОКУ:")
    print("=" * 50)
    print()
    print(session_string)
    print()
    print("=" * 50)
    print()
    print("Вставьте её в Dokploy → Environment:")
    print("  TELETHON_SESSION=<строка выше>")
    print()
    print("Затем нажмите Redeploy")

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
