import os
import sys
import asyncio
import logging

import aiosqlite
from aiogram import Bot

# Загрузка токена бота из переменных окружения
TOKEN = os.getenv("TG_BOT_TOKEN")

# Настройка логгера
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def _get_all_chats() -> list[int]:
    """
    Возвращает список chat_id всех пользователей из таблицы users.
    """
    async with aiosqlite.connect("tg_users.db") as db:
        cur = await db.execute("SELECT chat_id FROM users")
        rows = await cur.fetchall()
    return [r[0] for r in rows]

async def send_to_all(text: str):
    """
    Отправляет сообщение text всем chat_id из БД.
    """
    bot = Bot(token=TOKEN)
    for chat_id in await _get_all_chats():
        try:
            await bot.send_message(chat_id, text)
        except Exception as e:
            logger.warning("Не удалось отправить сообщение %s: %s", chat_id, e)
    # Закрываем сессию бота
    await bot.session.close()

if __name__ == "__main__":
    # Запуск рассылки из командной строки: python tg_bridge.py "Ваше сообщение"
    message = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "Hello from tg_bridge!"
    asyncio.run(send_to_all(message))