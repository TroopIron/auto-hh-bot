import os
import html
from dotenv import load_dotenv
load_dotenv()
import logging
import time
import aiosqlite
from fastapi import FastAPI, Request, HTTPException
from hh_api import HHApiClient
from chatgpt_client import ChatGPTClient
from aiogram import Bot

# Настройки из переменных окружения
CLIENT_ID = os.getenv("HH_CLIENT_ID")
CLIENT_SECRET = os.getenv("HH_CLIENT_SECRET")
REDIRECT_URI = os.getenv("REDIRECT_URI")
BOT_TOKEN = os.getenv("TG_BOT_TOKEN")

# Логирование
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Инициализация FastAPI и клиентов
app = FastAPI()
hh_client = HHApiClient()
chatgpt_client = ChatGPTClient()

# Путь к SQLite
DB_PATH = "tg_users.db"

async def get_user_token(tg_user: int) -> str | None:
    """Возвращает access_token для указанного tg_user из БД или None."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT access_token FROM user_tokens WHERE tg_user = ?",
            (tg_user,),
        )
        row = await cur.fetchone()
    return row[0] if row else None

@app.get("/")
async def root(tg_user: int):
    """Выдаёт ссылку для OAuth HH.ru, передавая tg_user в state."""
    auth_url = (
        f"https://hh.ru/oauth/authorize?"
        f"response_type=code"
        f"&client_id={CLIENT_ID}"
        f"&redirect_uri={REDIRECT_URI}"
        f"&state={tg_user}"
    )
    return {"auth_url": auth_url}

@app.get("/callback")
async def callback(code: str, state: str):
    """Обрабатывает OAuth-редирект, сохраняет токены в БД и уведомляет пользователя."""
    try:
        tokens = await hh_client.exchange_code_for_token(code)
    except Exception as e:
        logger.error("Ошибка обмена кода на токен: %s", e)
        raise HTTPException(500, "Failed to exchange code for token")

    tg_user = int(state)
    expires_at = int(time.time()) + tokens.get("expires_in", 0)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT OR REPLACE INTO user_tokens
                (tg_user, access_token, refresh_token, expires_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                tg_user,
                tokens.get("access_token"),
                tokens.get("refresh_token"),
                expires_at,
            ),
        )
        await db.commit()

    # Уведомление пользователя в Telegram
    bot = Bot(token=BOT_TOKEN)
    try:
        await bot.send_message(
            tg_user,
            "✅ Вы успешно авторизовались в HeadHunter! \n\n"
            "<b>Шаг 2</b>. Перейдите в /menu для настройки ваших фильтров",
            parse_mode="HTML"
        )
    except Exception as e:
        logger.warning("Не удалось отправить Telegram-сообщение: %s", e)
    finally:
        await bot.session.close()

    return """✅ Вы успешно авторизованы! Можете вернуться в Telegram"""

@app.get("/search")
async def search(tg_user: int, text: str = "python", per_page: int = 10):
    """Ищет вакансии через HH API для указанного пользователя."""
    token = await get_user_token(tg_user)
    if not token:
        raise HTTPException(401, "No token stored for user")
    client = HHApiClient(token)
    try:
        vacancies = await client.search_vacancies(text=text, per_page=per_page)
    except Exception as e:
        logger.error("HH API error при поиске: %s", e)
        raise HTTPException(500, "HH API error")
    finally:
        await client.close()
    return {"vacancies": vacancies}

@app.get("/resumes")
async def resumes(tg_user: int):
    """Возвращает список резюме пользователя через HH API."""
    token = await get_user_token(tg_user)
    if not token:
        raise HTTPException(401, "No token stored for user")
    client = HHApiClient(token)
    try:
        resumes = await client.list_resumes()
    except Exception as e:
        logger.error("HH API error при получении резюме: %s", e)
        raise HTTPException(500, "HH API error on resumes")
    finally:
        await client.close()
    return {"resumes": resumes}

@app.post("/auto_reply")
async def auto_reply(tg_user: int, vacancy_id: str, resume_id: str):
    """Генерирует сопроводительное письмо через ChatGPT и отправляет отклик."""
    token = await get_user_token(tg_user)
    if not token:
        raise HTTPException(401, "No token stored for user")
    try:
        cover_letter = await chatgpt_client.generate_cover_letter(vacancy_id, resume_id)
    except Exception as e:
        logger.error("ChatGPT error при генерации сопроводительного письма: %s", e)
        raise HTTPException(500, "ChatGPT generation error")
    client = HHApiClient(token)
    try:
        result = await client.respond_to_vacancy(
            vacancy_id, resume_id, cover_letter
        )
    except Exception as e:
        logger.error("HH API error при отправке отклика: %s", e)
        raise HTTPException(500, "HH API respond error")
    finally:
        await client.close()
    return {"result": result}