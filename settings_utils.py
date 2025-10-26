import aiosqlite
from aiogram import types
from typing import Optional
from aiogram.utils.keyboard import InlineKeyboardBuilder
# Путь к SQLite базе
DB_PATH = "tg_users.db"


# ───────── pending ─────────
async def set_pending(tg_user: int, field: Optional[str]) -> None:
    """
    Помечаем, что для пользователя tg_user сейчас ожидается ввод для поля field.
    Для сброса передайте field=None.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT OR REPLACE INTO user_settings (tg_user, key, value)
            VALUES (?, 'pending', ?)
            """,
            (tg_user, field),
        )
        await db.commit()


async def get_pending(tg_user: int) -> Optional[str]:
    """Возвращает текущее pending-поле или None."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT value FROM user_settings
            WHERE tg_user = ? AND key = 'pending'
            """,
            (tg_user,),
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else None


# ───────── user settings ─────────
async def save_user_setting(tg_user: int, key: str, value: str) -> None:
    """
    Сохраняет пользовательское значение (фильтр) по ключу key.
    Пример key: 'region', 'salary', 'work_format', 'employment_type', 'keyword'.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT OR REPLACE INTO user_settings (tg_user, key, value)
            VALUES (?, ?, ?)
            """,
            (tg_user, key, value),
        )
        await db.commit()


async def get_user_setting(tg_user: int, key: str) -> Optional[str]:
    """Получает сохранённое значение пользователя по ключу key."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT value FROM user_settings
            WHERE tg_user = ? AND key = ?
            """,
            (tg_user, key),
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else None


# ───────── Keyboards ─────────
def build_main_menu_keyboard() -> types.InlineKeyboardMarkup:
    """
    Две «широкие» кнопки-режима по одной в строке,
    ниже – обычное меню 2×2.
    Работает на aiogram-3 через InlineKeyboardBuilder.
    """
    b = InlineKeyboardBuilder()

    # большие кнопки
    b.row(
        types.InlineKeyboardButton(
            text="🚀 Начать автоотклики (20)",
            callback_data="start_auto",
        )
    )
    b.row(
        types.InlineKeyboardButton(
            text="🕹 Ручной режим откликов",
            callback_data="start_manual",
        )
    )

    # компактные пункты меню (2 в ряд)
    b.row(
        types.InlineKeyboardButton(text="⚙️ Настройка фильтров", callback_data="open_settings"),
        types.InlineKeyboardButton(text="📄 Резюме",              callback_data="open_resumes"),
    )
    b.row(
        types.InlineKeyboardButton(text="⭐️ Избранные вакансии", callback_data="open_favorites"),
        types.InlineKeyboardButton(text="👁️ Мои фильтры",        callback_data="show_filters"),
    )

    return b.as_markup()


def build_settings_keyboard(with_back: bool = True) -> types.InlineKeyboardMarkup:
    """Клавиатура управления фильтрами."""
    rows = [
        [
            types.InlineKeyboardButton(text="Регион",          callback_data="filter_region"),
            types.InlineKeyboardButton(text="График",          callback_data="filter_schedule"),
        ],
        [
            types.InlineKeyboardButton(text="Формат работы",   callback_data="filter_work_format"),
            types.InlineKeyboardButton(text="ЗП",              callback_data="filter_salary"),
        ],
        [
            types.InlineKeyboardButton(text="Тип занятости",   callback_data="filter_employment_type"),
            types.InlineKeyboardButton(text="Ключевое слово",  callback_data="filter_keyword"),
        ],
    ]
    if with_back:
        rows.append([types.InlineKeyboardButton(text="⬅️ В меню", callback_data="back_menu")])
    return types.InlineKeyboardMarkup(inline_keyboard=rows)
