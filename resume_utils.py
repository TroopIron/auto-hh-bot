from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
import os
import httpx
import aiosqlite
from hh_api import HHApiClient
from aiogram import types
from aiogram.utils.keyboard import InlineKeyboardBuilder
DB_PATH = "tg_users.db"


def build_oauth_url(tg_user: int) -> str:
    return (
        "https://hh.ru/oauth/authorize?response_type=code"
        f"&client_id={os.getenv('HH_CLIENT_ID')}"
        f"&redirect_uri={os.getenv('REDIRECT_URI')}"
        f"&state={tg_user}"
    )

async def get_user_token(tg_user: int) -> str | None:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT access_token FROM user_tokens WHERE tg_user = ?",
            (tg_user,),
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else None


async def build_resume_keyboard(uid: int) -> types.InlineKeyboardMarkup:
    token = await get_user_token(uid)
    if not token:
        return types.InlineKeyboardMarkup(
            inline_keyboard=[[types.InlineKeyboardButton(
                text="🔑 Авторизоваться",
                url=build_oauth_url(uid)
            )]]
        )

    client = HHApiClient(token)
    try:
        resumes = await client.list_resumes()      # 200 OK мы уже видели
    finally:
        await client.close()

    builder = InlineKeyboardBuilder()
    for r in resumes:
        builder.button(
            text=r["title"] or r.get("profession", "Без названия"),
            callback_data=f"select_resume_{r['id']}"
        )

    builder.button(text="⬅️ В меню", callback_data="back_menu")
    builder.adjust(1)               # каждая кнопка в своей строке
    return builder.as_markup()