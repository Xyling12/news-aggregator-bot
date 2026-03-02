/**
 * Создаёт Telethon-совместимую StringSession с помощью GramJS (JS-порт Telethon).
 * Запуск: node create_session.mjs
 */

import { TelegramClient } from "telegram";
import { StringSession } from "telegram/sessions/index.js";
import readline from "readline";

const API_ID = 33112824;
const API_HASH = "7526146bb71fe8ca2fcf1b353f34425e";

const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
const ask = (q) => new Promise((r) => rl.question(q, r));

async function main() {
    console.log("=".repeat(50));
    console.log("🔐 Создание Telethon StringSession (Node.js)");
    console.log("=".repeat(50));
    console.log();

    const session = new StringSession("");
    const client = new TelegramClient(session, API_ID, API_HASH, {
        connectionRetries: 3,
    });

    await client.start({
        phoneNumber: async () => await ask("Введите номер телефона (+79XXXXXXXXX): "),
        phoneCode: async () => await ask("Введите код из Telegram: "),
        password: async () => await ask("Введите пароль 2FA: "),
        onError: (err) => console.error("Ошибка:", err.message),
    });

    const me = await client.getMe();
    const sessionStr = client.session.save();

    console.log();
    console.log(`✅ Авторизация успешна! Вы: ${me.firstName}`);
    console.log();
    console.log("=".repeat(50));
    console.log("📋 СКОПИРУЙТЕ ЭТУ СТРОКУ:");
    console.log("=".repeat(50));
    console.log();
    console.log(sessionStr);
    console.log();
    console.log("=".repeat(50));
    console.log();
    console.log("Вставьте в Dokploy → Environment:");
    console.log("  TELETHON_SESSION=<строка выше>");
    console.log();
    console.log("Затем нажмите Redeploy");

    await client.disconnect();
    rl.close();
}

main().catch(console.error);
