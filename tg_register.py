import os
import json, random
import logging
from dotenv import load_dotenv
from urllib.parse import quote_plus
import aiosqlite
from fastapi import FastAPI, Request, HTTPException
from aiogram import Bot, types
from aiogram.exceptions import TelegramBadRequest
import html
import re
import textwrap 
from settings_utils import (
    save_user_setting,
    get_user_setting,
    build_settings_keyboard,
    build_main_menu_keyboard,
    set_pending,
    get_pending,
)
from claude_client import generate_cover_letter
from resume_utils import build_resume_keyboard
import hh_api
from fastapi.responses import HTMLResponse

# ────────── базовая инициализация ──────────
load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("TG_BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("TG_BOT_TOKEN not set")

bot = Bot(token=BOT_TOKEN)
app = FastAPI()

DB_PATH = "tg_users.db"
if not hasattr(app.state, "cursor"):
    app.state.cursor = {}

# ─── добавь эту строчку ───
if not hasattr(app.state, "jobs_by_id"):
    app.state.jobs_by_id = {}          # {uid: {vac_id: vacancy_dict}}

# добавляем ↓
if not hasattr(app.state, "jobs_cache"):
    app.state.jobs_cache = {}          # {uid: [vacancy, …]}


# ────────── helpers ──────────
async def get_user_token(tg_user: int) -> str | None:
    """
    Читаем access_token из таблицы user_tokens.
    Возвращаем None, если запись не найдена.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT access_token FROM user_tokens WHERE tg_user = ?",
            (tg_user,),
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else None


# ────────── подсказки ──────────
SCHEDULE_SUGGESTIONS = ["полный день", "гибкий график", "сменный график"]
WORK_FORMAT_SUGGESTIONS = ["дистанционно", "офис", "гибрид"]
EMPLOYMENT_TYPE_SUGGESTIONS = ["полная", "частичная", "проектная", "стажировка"]

MULTI_KEYS = {
    "schedule": SCHEDULE_SUGGESTIONS,
    "work_format": WORK_FORMAT_SUGGESTIONS,
    "employment_type": EMPLOYMENT_TYPE_SUGGESTIONS,
}

# ────────── helpers ──────────
def format_vacancy(v: dict) -> tuple[str, str | None]:
    """
    Возвращает (caption, logo_url).
    caption — HTML-текст, logo_url может быть None.
    """
    title   = v["name"]
    company = v.get("employer", {}).get("name", "Без названия")
    logo    = v.get("employer", {}).get("logo_urls", {}).get("240")

    # ─ salary ─
    salary = "не указана"
    if isinstance(v.get("salary"), dict):
        s   = v["salary"];  fr, to, cur = s.get("from"), s.get("to"), s.get("currency", "")
        if fr and to: salary = f"{fr}–{to} {cur}"
        elif fr:      salary = f"от {fr} {cur}"
        elif to:      salary = f"до {to} {cur}"

    # ─ description ─  (строка или словарь)
    snip = v.get("snippet")
    if isinstance(snip, dict):
        resp = snip.get("responsibility") or ""
        req  = snip.get("requirement") or ""
        raw  = f"{resp}\n{req}".strip()
    else:
        raw = snip or v.get("description", "") or ""

    full = strip_html(raw)
    descr = wrap_long(full[:1200]) + ("…" if len(full) > 1200 else "")

    caption = (
        f"💼 <b>{title}</b>\n"
        f"\n🏢 <i>{company}</i>\n"
        f"\n💰 ЗП: {salary}\n"
        f"\n{descr}\n"
        f"\n<a href='{v['url']}'>Открыть на hh.ru</a>"
    )
    return caption, logo

async def get_resume_summary(uid: int) -> str:
    """Возвращает короткое описание резюме (для сопроводительного)."""
    rid = await get_user_setting(uid, "resume")
    if not rid:
        return ""
    token = await get_user_token(uid)
    client = hh_api.HHApiClient(token) if token else hh_api.HHApiClient()
    txt = await client.get_resume_text(rid)          # у тебя уже есть метод
    return txt[:1200]                                # лишнее обрежем


async def send_apply(uid: int, vacancy_id: str, cover_letter: str) -> None:
    """
    Шлёт отклик + сопроводительное в HH.
    """
    token = await get_user_token(uid)
    resume_id = await get_user_setting(uid, "resume")
    if not resume_id:
        raise RuntimeError("Резюме не выбрано")

    client = hh_api.HHApiClient(token) if token else hh_api.HHApiClient()
    await client.respond_to_vacancy(
        vacancy_id=vacancy_id,
        resume_id=resume_id,
        cover_letter=cover_letter,
    )


def build_job_kb(vac_id: int) -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text="✅ Откликнуться", callback_data=f"job_apply_{str(vac_id)}"
                ),
                types.InlineKeyboardButton(
                    text="⭐️ В избранное", callback_data=f"job_fav_{vac_id}"
                ),
            ],
            [
                types.InlineKeyboardButton(text="⬅️ Предыдущая", callback_data="job_prev"),
                types.InlineKeyboardButton(text="➡️ Следующая",  callback_data="job_next"),
            ],
            [types.InlineKeyboardButton(text="↩️ Меню", callback_data="back_menu")],
        ]
    )


async def run_jobs(uid: int) -> None:
    """
    Логика команды /jobs вынесена сюда, чтобы можно было
    запускать её и из текстового сообщения, и из callback-кнопки.
    """
    token  = await get_user_token(uid)
    client = hh_api.HHApiClient(token) if token else hh_api.HHApiClient()

    page = await next_jobs_page(uid)            # счётчик 0–19
    keyword = await get_user_setting(uid, "keyword") or ""

    params = {"text": keyword, "per_page": 20, "page": page}
    resp   = await client._client.get(f"{client.BASE_URL}/vacancies", params=params)
    resp.raise_for_status()
    raw_vacs = resp.json().get("items", [])

    if not raw_vacs:
        await bot.send_message(uid, "По вашим фильтрам вакансий не найдено 😕")
        return

    vacancies: list[dict] = []
    for v in raw_vacs:
        resp_txt = v.get("snippet", {}).get("responsibility") or ""
        req_txt  = v.get("snippet", {}).get("requirement")      or ""
        full_descr = strip_html(f"{resp_txt}\n{req_txt}".strip()) or "нет описания"

        vacancies.append({
            "id":       v["id"],
            "name":     v["name"],
            "url":      v["alternate_url"],
            "salary":   v.get("salary"),
            "employer": v.get("employer"),
            "snippet":  (full_descr[:1200] + "…") if len(full_descr) > 1200 else full_descr,
        })

    # сохраняем в pending_jobs
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO pending_jobs(tg_user, jobs_json, cursor)
            VALUES(?, ?, 0)
            ON CONFLICT(tg_user) DO UPDATE
            SET jobs_json = excluded.jobs_json,
                cursor     = 0
            """,
            (uid, json.dumps(vacancies)),
        )
        await db.commit()

    # держим копию в RAM
    app.state.jobs_cache = getattr(app.state, "jobs_cache", {})
    app.state.jobs_cache[uid] = vacancies          # старый список (может пригодиться)

    app.state.jobs_by_id = getattr(app.state, "jobs_by_id", {})
    app.state.jobs_by_id[uid] = {
        str(v["id"]): v          # '123456'
        for v in vacancies
    } | {
        int(v["id"]): v          # 123456   (на случай, если id будет int)
        for v in vacancies
    }   # ← новый dict

    app.state.cursor.setdefault(uid, {})["jobs"] = 0

    # первая карточка
    first = vacancies[0]
    caption, logo = format_vacancy(first)
    kb_first = build_job_kb(first["id"])

    if logo:
        await bot.send_photo(uid, logo, caption=caption, parse_mode="HTML", reply_markup=kb_first)
    else:
        await bot.send_message(uid, caption, parse_mode="HTML", reply_markup=kb_first)


def build_fav_kb(fid: int) -> types.InlineKeyboardMarkup:
    """Клавиатура для карточек в ⭐ Избранное."""
    return types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text="🗑 Удалить",
                    callback_data=f"fav_del_{fid}",
                ),
                types.InlineKeyboardButton(
                    text="⬅️ Предыдущая",
                    callback_data="fav_prev",
                ),
                types.InlineKeyboardButton(
                    text="➡️ Следующая",
                    callback_data="fav_next",
                ),
            ],
            [types.InlineKeyboardButton(text="↩️ Меню", callback_data="back_menu")],
        ]
    )

def strip_html(text: str) -> str:
    """Очень грубо убирает теги, чтобы Telegram не порезал сообщение."""
    return re.sub(r"<[^>]+>", "", text or "")

def wrap_long(text: str, width: int = 60) -> str:
    """Разбивает параграф на короткие строки, чтобы они не растягивались во всю ширину."""
    return "\n".join(textwrap.wrap(text, width=width))

def build_inline_suggestions(
    values: list[str],
    prefix: str,
    selected: set[str] | None = None,
    with_back: bool = False,
):
    """Собирает клавиатуру‑однострочник; отмечает выбранные чек‑марк."""
    selected = selected or set()
    rows = [
        [
            types.InlineKeyboardButton(
                text=("✅ " if v in selected else "") + v,
                callback_data=f"{prefix}_{v}",
            )
        ]
        for v in values
    ]
    if with_back:
        rows.append([
            types.InlineKeyboardButton(
                text="⬅️ Назад", callback_data="back_settings"
            )
        ])
    return types.InlineKeyboardMarkup(inline_keyboard=rows)


async def toggle_multi_value(user_id: int, key: str, value: str) -> set[str]:
    curr = await get_user_setting(user_id, key) or ""
    items = {v.strip() for v in curr.split(",") if v.strip()}
    if value in items:
        items.remove(value)
    else:
        items.add(value)
    await save_user_setting(user_id, key, ",".join(items))
    return items

async def next_jobs_page(uid: int) -> int:
    """
    Увеличивает счётчик page для /jobs и возвращает новое значение.
    Когда дойдём до 20-й страницы – начинаем сначала.
    """
    curr = int(await get_user_setting(uid, "jobs_page") or 0)
    new  = curr + 1 if curr < 19 else 0
    await save_user_setting(uid, "jobs_page", str(new))
    return curr

async def build_filters_summary(uid: int) -> str:
    def esc(v):
        return html.escape(str(v)) if v else "—"

    region_raw = await get_user_setting(uid, "region")
    region = esc(await hh_api.area_name(region_raw))
    salary = esc(await get_user_setting(uid, "salary") or "—")
    schedule = esc(await get_user_setting(uid, "schedule") or "—")
    work_fmt = esc(await get_user_setting(uid, "work_format") or "—")
    employ = esc(await get_user_setting(uid, "employment_type") or "—")
    keyword = esc(await get_user_setting(uid, "keyword") or "—")

    return (
    "<b>📋 Ваши действующие фильтры</b>\n"
    f"• Регион: {region}\n"
    f"• ЗП ≥ {salary}\n"
    f"• График: {schedule}\n"
    f"• Формат работы: {work_fmt}\n"
    f"• Тип занятости: {employ}\n"
    f"• Ключевое слово: {keyword}"
)


def build_oauth_url(tg_user: int) -> str:
    rid = os.getenv("REDIRECT_URI")
    return (
        "https://hh.ru/oauth/authorize?"
        f"response_type=code&client_id={os.getenv('HH_CLIENT_ID')}"
        f"&redirect_uri={quote_plus(rid, safe='')}"
        f"&state={tg_user}"
    )


async def safe_edit_markup(message: types.Message, markup: types.InlineKeyboardMarkup | None = None):
    """Обновить reply_markup; игнорировать BadRequest, если не изменилось."""
    try:
        await bot.edit_message_reply_markup(
            chat_id=message.chat.id,
            message_id=message.message_id,
            reply_markup=markup,
        )
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise


async def safe_edit_text(
    message: types.Message,
    text: str,
    markup: types.InlineKeyboardMarkup | None,
    html: bool = False,
):
    """Безопасно обновить текст сообщения и клавиатуру."""
    try:
        await bot.edit_message_text(
            text=text,
            chat_id=message.chat.id,
            message_id=message.message_id,
            reply_markup=markup,
            parse_mode="HTML" if html else None,
        )
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise


async def safe_edit_media(
    message: types.Message,
    photo_url: str,
    caption: str,
    markup: types.InlineKeyboardMarkup,
):
    """
    Пытаемся заменить фото и подпись, ловим «message is not modified».
    Если исходное сообщение было без фото – просто удаляем и отправляем заново.
    """
    try:
        media = types.InputMediaPhoto(media=photo_url, caption=caption, parse_mode="HTML")
        await bot.edit_message_media(
            media=media,
            chat_id=message.chat.id,
            message_id=message.message_id,
            reply_markup=markup,
        )
    except TelegramBadRequest as e:
        err = str(e)
        # если сообщение не изменилось или это было text-message, пробуем edit_text
        if "message is not modified" in err:
            await safe_edit_text(message, caption, markup, html=True)
        elif "type of file" in err or "message content is not modified" in err:
            # скорее всего исходное сообщение не фото – удаляем и шлём новое
            await bot.delete_message(message.chat.id, message.message_id)
            await bot.send_photo(
                message.chat.id,
                photo_url,
                caption=caption,
                parse_mode="HTML",
                reply_markup=markup,
            )
        else:
            raise



async def get_settings_msg_id(uid: int) -> int | None:
    """Возвращает сохранённый msg_id сообщения настроек."""
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            async with db.execute(
                "SELECT settings_msg_id FROM users WHERE chat_id = ?",
                (uid,),
            ) as cur:
                row = await cur.fetchone()
                return row[0] if row else None
        except aiosqlite.OperationalError as e:
            if "no such column" in str(e).lower():
                await db.execute(
                    "ALTER TABLE users ADD COLUMN settings_msg_id INTEGER"
                )
                await db.commit()
                return None
            raise


async def set_settings_msg_id(uid: int, msg_id: int) -> None:
    """Сохраняет msg_id сообщения настроек."""
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute(
                "UPDATE users SET settings_msg_id = ? WHERE chat_id = ?",
                (msg_id, uid),
            )
        except aiosqlite.OperationalError as e:
            if "no such column" in str(e).lower():
                await db.execute(
                    "ALTER TABLE users ADD COLUMN settings_msg_id INTEGER"
                )
                await db.execute(
                    "UPDATE users SET settings_msg_id = ? WHERE chat_id = ?",
                    (msg_id, uid),
                )
        await db.commit()


async def safe_edit_text_by_id(
    uid: int,
    msg_id: int | None,
    text: str,
    markup: types.InlineKeyboardMarkup | None,
    html: bool = False,
):
    """Редактирует сообщение по id, отправляя новое при ошибке."""
    if msg_id is None:
        new_msg = await bot.send_message(uid, text, reply_markup=markup)
        await set_settings_msg_id(uid, new_msg.message_id)
        return
    try:
        await bot.edit_message_text(
            text=text,
            chat_id=uid,
            message_id=msg_id,
            reply_markup=markup,
            parse_mode="HTML" if html else None,
        )
    except TelegramBadRequest as e:
        err = str(e).lower()
        if "message to edit not found" in err:
            new_msg = await bot.send_message(uid, text, reply_markup=markup)
            await set_settings_msg_id(uid, new_msg.message_id)
        elif "message is not modified" not in err:
            raise

async def show_prev_job(call: types.CallbackQuery, uid: int):
    jobs   = app.state.jobs_cache.get(uid, [])
    cursor = app.state.cursor.get(uid, {}).get("jobs", 0)

    if cursor > 0:
        cursor -= 1
    app.state.cursor.setdefault(uid, {})["jobs"] = cursor

    if not jobs:
        await bot.answer_callback_query(call.id, "Список пуст")
        return {"ok": True}

    job = jobs[cursor]
    kb  = build_job_kb(job["id"])          # helper из п. 1-б
    text, logo = format_vacancy(job)

    if logo:
        await safe_edit_media(call.message, logo, text, kb)
    else:
        await safe_edit_text(call.message, text, kb, html=True)

    await bot.answer_callback_query(call.id)
    return {"ok": True}

async def show_prev_fav(call: types.CallbackQuery, uid: int):
    favs   = app.state.favs_cache.get(uid, [])
    cursor = app.state.cursor.get(uid, {}).get("favs", 0)

    if cursor > 0:
        cursor -= 1
    app.state.cursor.setdefault(uid, {})["favs"] = cursor

    if not favs:
        await bot.answer_callback_query(call.id, "Список пуст")
        return {"ok": True}

    fid, title, url = favs[cursor]
    kb = build_fav_kb(fid)                 # вынеси клаву в helper, чтобы не дублировать
    text = f"⭐️ <b>{html.escape(title)}</b>\n<a href='{url}'>Открыть на hh.ru</a>"
    await safe_edit_text(call.message, text, kb, html=True)
    await bot.answer_callback_query(call.id)
    return {"ok": True}

async def show_next_fav(call: types.CallbackQuery, uid: int) -> dict:
    # берём список из кеша и сдвигаем курсор
    favs = getattr(app.state, "favs_cache", {}).get(uid, [])
    if favs:
        favs.pop(0)                 # убираем показанную карточку

    # если больше нечего показывать
    if not favs:
        await safe_edit_text(call.message, "⭐️ Избранное закончилось.", None)
        await bot.answer_callback_query(call.id, "Список пуст ✅")
        app.state.favs_cache.pop(uid, None)
        return {"ok": True}

    # показываем следующую карточку
    app.state.favs_cache[uid] = favs          # обновили кеш
    fid, title, url = favs[0]

    text = (
        f"⭐️ <b>{html.escape(title)}</b>\n"
        f"<a href='{url}'>Открыть на hh.ru</a>"
    )
    kb = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(text="🗑 Удалить",     callback_data=f"fav_del_{fid}"),
                types.InlineKeyboardButton(text="⬅️ Предыдущая", callback_data="fav_prev"),
                types.InlineKeyboardButton(text="➡️ Следующая", callback_data="fav_next"),  
            ],
            [types.InlineKeyboardButton(text="⬅️ Назад", callback_data="back_menu")],
        ]
    )
    await safe_edit_text(call.message, text, kb, html=True)
    await bot.answer_callback_query(call.id)
    return {"ok": True}



async def safe_delete(message: types.Message) -> None:
    "Пытаемся удалить сообщение пользователя, не роняя обработчик."
    try:
        await message.delete()
    except TelegramBadRequest:
        # например, если бот не админ или сообщение старше 48 ч
        pass
    except Exception:
        pass


# ────────── FastAPI lifecycle ──────────
@app.on_event("startup")
async def _startup():
    webhook = os.getenv("WEBHOOK_URL")
    if webhook:
        await bot.delete_webhook(drop_pending_updates=True)
        await bot.set_webhook(webhook)
        logger.info("Webhook set: %s", webhook)


@app.on_event("shutdown")
async def _shutdown():
    await bot.session.close()


# ────────── main webhook ──────────
@app.post("/bot{token:path}")
async def telegram_webhook(request: Request, token: str):
    if token != BOT_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid token")

    update = types.Update(**await request.json())

    # ===== CALLBACKS =====
    if update.callback_query:
        call = update.callback_query
        uid = call.from_user.id
        data = call.data

        # ensure user row exists
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("INSERT OR IGNORE INTO users(chat_id) VALUES (?)", (uid,))
            await db.commit()

        # === возврат в главное меню ===
        if data == "back_menu":
            # убираем открытую карточку (если ещё не удалили)
            try:
                await bot.delete_message(uid, call.message.message_id)
            except TelegramBadRequest:
                pass

            smsg = await get_settings_msg_id(uid)
            await safe_edit_text_by_id(
                uid, smsg, "📌 Главное меню:", build_main_menu_keyboard()
            )
            await bot.answer_callback_query(call.id)
            return {"ok": True}


         # ─── запуск авто-откликов (20 штук) ───
        if data == "start_auto":
            await bot.answer_callback_query(call.id)

            # берём свежие 20 вакансий через уже готовый fetch в run_jobs
            vacancies = await fetch_vacancies(uid)      # функция есть в run_jobs
            resume = await get_resume_summary(uid)

            sent = 0
            for v in vacancies[:20]:
                cover = await generate_cover_letter(v["snippet"] or v["name"], resume)
                try:
                    await send_apply(uid, v["id"], cover)
                    sent += 1
                except Exception as e:
                    logger.warning("fail apply %s: %s", v["id"], e)

            await bot.send_message(uid, f"🚀 Автоотклики отправлены: {sent}/20")
            return {"ok": True}

        # ─── ручной режим: просто открываем /jobs на первую страницу ───
        if data == "start_manual":
            await bot.answer_callback_query(call.id)
            await run_jobs(uid)          # функция уже есть
            return {"ok": True}
            
        # === открыть настройку фильтров ===
        if data == "open_settings":
            smsg = await get_settings_msg_id(uid)
            await safe_edit_text_by_id(uid, smsg, "Ваши фильтры:", build_settings_keyboard())
            await bot.answer_callback_query(call.id)
            return {"ok": True}

        # === открыть резюме ===
        if data == "open_resumes":
            kb = await build_resume_keyboard(uid)
            await safe_edit_text(
                call.message,
                "📄 Ваши резюме:",
                kb,
            )
            return {"ok": True}

        # === открыть избранные вакансии ===
        if data == "open_favorites":
            async with aiosqlite.connect(DB_PATH) as db:
                async with db.execute(
                    "SELECT vacancy_id, title, url FROM user_favorites "
                    "WHERE tg_user = ? ORDER BY rowid DESC",
                    (uid,),
                ) as cur:
                    favs = await cur.fetchall()

            if not favs:
                await bot.answer_callback_query(call.id, "Список пуст 🙂", show_alert=True)
                return {"ok": True}

            # показываем по одной как в /jobs
            fid, title, url = favs[0]
            text = f"⭐️ <b>{html.escape(title)}</b>\n<a href='{url}'>Открыть на hh.ru</a>"

            kb_fav = types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(text="🗑 Удалить",   callback_data=f"fav_del_{fid}"),
                        types.InlineKeyboardButton(text="⬅️ Предыдущая", callback_data="fav_prev"),
                        types.InlineKeyboardButton(text="➡️ Следующая", callback_data="fav_next"),     
                    ],
                    [
                        types.InlineKeyboardButton(text="⬅️ Назад",     callback_data="back_menu"),
                    ],
                ]
            )

            # сохраняем список в память пользователя
            await bot.delete_message(uid, call.message.message_id)
            await bot.send_message(uid, text, reply_markup=kb_fav, parse_mode="HTML")
            await bot.answer_callback_query(call.id)
            # временно кладём favs в RAM-словарь (ключ = uid)
            app.state.favs_cache = getattr(app.state, "favs_cache", {})
            app.state.favs_cache[uid] = favs
            return {"ok": True}

            
        # ---------- листаем избранное ----------  <-- новый комментарий
        elif data == "fav_prev":
            return await show_prev_fav(call, uid)

        elif data == "fav_next":
            return await show_next_fav(call, uid)

        elif data.startswith("fav_del_"):
            fid = int(data.split("_")[-1])
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "DELETE FROM user_favorites WHERE tg_user = ? AND vacancy_id = ?",
                    (uid, fid),
                )
                await db.commit()

            # обновляем кеш и сразу показываем следующую
            favs = getattr(app.state, "favs_cache", {}).get(uid, [])
            favs = [f for f in favs if f[0] != fid]
            app.state.favs_cache[uid] = favs
            await bot.answer_callback_query(call.id, "Удалено")
            return await show_next_fav(call, uid)
        
        # ---------- fav_del_<id> ----------
        if data.startswith("fav_del_"):
            fid = int(data.split("_")[-1])
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "DELETE FROM user_favorites WHERE tg_user = ? AND vacancy_id = ?",
                    (uid, fid),
                )
                await db.commit()

            # убираем из кеша и жмём fav_next, чтобы показать следующее
            favs = getattr(app.state, "favs_cache", {}).get(uid, [])
            favs = [f for f in favs if f[0] != fid]
            app.state.favs_cache[uid] = favs
            await bot.answer_callback_query(call.id, "Удалено")
            return await show_next_fav(call, uid)

        if data == "show_filters":
            summary = await build_filters_summary(uid)
            await safe_edit_text(
                call.message,
                summary,
                types.InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            types.InlineKeyboardButton(
                                text="⬅️ В меню", callback_data="back_menu"
                            )
                        ]
                    ]
                ),
                html=True,
            )
            await bot.answer_callback_query(call.id)
            return {"ok": True}

        if data == "back_settings":
            smsg = await get_settings_msg_id(uid)
            await safe_edit_text_by_id(
                uid,
                smsg,
                "Ваши фильтры:",
                build_settings_keyboard(),
            )
            await bot.answer_callback_query(call.id)
            return {"ok": True}


        # ---------- запуск фильтров ----------
        if data.startswith("filter_"):
            fkey = data.split("_", 1)[1]

            if fkey == "region":
                await set_pending(uid, "region")
                await safe_edit_text(call.message, "Введите название региона:", None)
                return {"ok": True}

            if fkey == "salary":
                await set_pending(uid, "salary")
                await safe_edit_text(call.message, "Введите минимальную зарплату (число):", None)
                return {"ok": True}

            if fkey == "keyword":
                await set_pending(uid, "keyword")
                await safe_edit_text(call.message, "Введите ключевое слово:", None)
                return {"ok": True}

            if fkey in MULTI_KEYS:
                selection = await get_user_setting(uid, fkey) or ""
                sel_set = {i.strip() for i in selection.split(",") if i.strip()}
                await safe_edit_text(
                    call.message,
                    f"Выберите {fkey.replace('_', ' ')} (можно несколько):",
                    build_inline_suggestions(
                        MULTI_KEYS[fkey], f"{fkey}_suggest", sel_set, with_back=True
                    ),
                )
                return {"ok": True}

        # ---------- мультивыбор ----------
        for m in MULTI_KEYS:
            prefix = f"{m}_suggest_"
            if data.startswith(prefix):
                val = data[len(prefix):]
                sel_set = await toggle_multi_value(uid, m, val)
                await safe_edit_markup(
                    call.message,
                    build_inline_suggestions(
                        MULTI_KEYS[m], f"{m}_suggest", sel_set, with_back=True
                    ),
                )
                await bot(call.answer("✓"))
                return {"ok": True}


        # ---------- region из suggestions ----------
        if data.startswith("region_suggest_"):
            area_id = int(data.split("_")[-1])
            await save_user_setting(uid, "region", area_id)
            await safe_edit_markup(call.message, None)
            await bot(call.answer("Сохранено"))
            return {"ok": True}

        # ---------- выбор резюме ----------
        if data.startswith("select_resume_"):
            rid = data.split("_")[-1]
            await save_user_setting(uid, "resume", rid)
            await bot(call.answer("Резюме сохранено"))
            return {"ok": True}
        
        if data == "find_jobs":
            await bot.answer_callback_query(call.id)
            await bot.send_message(uid,
                "Окей! Нажмите /jobs, чтобы получить список вакансий по вашим фильтрам.")
            return {"ok": True}

                # ─────────── кнопки поиска вакансий ───────────
                # ─────────── кнопки поиска вакансий ───────────

        if data == "job_prev":
            return await show_prev_job(call, uid)

        if data == "job_next":
            async with aiosqlite.connect(DB_PATH) as db:
                async with db.execute(
                    "SELECT jobs_json, cursor FROM pending_jobs WHERE tg_user = ?",
                    (uid,),
                ) as cur:
                    row = await cur.fetchone()

                if not row:
                    await bot.answer_callback_query(call.id, "Список пуст.")
                    return {"ok": True}

                jobs, cursor = json.loads(row[0]), row[1] + 1
                app.state.cursor.setdefault(uid, {})["jobs"] = cursor

                # ----- если дошли до конца списка -----
                if cursor >= len(jobs):
                    await bot.answer_callback_query(call.id, "Вакансии закончились ✅")
                    await safe_edit_markup(call.message, None)
                    await db.execute(
                        "DELETE FROM pending_jobs WHERE tg_user = ?",
                        (uid,),
                    )
                    await db.commit()
                    return {"ok": True}

                # сохраняем новый cursor
                await db.execute(
                    "UPDATE pending_jobs SET cursor = ? WHERE tg_user = ?",
                    (cursor, uid),
                )
                await db.commit()

            # пропускаем пустые элементы
            while cursor < len(jobs) and not isinstance(jobs[cursor], dict):
                cursor += 1

            # дошли до конца — выходим
            if cursor >= len(jobs):
                await bot.answer_callback_query(call.id, "Вакансии закончились ✅")
                await safe_edit_markup(call.message, None)
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute("DELETE FROM pending_jobs WHERE tg_user = ?", (uid,))
                    await db.commit()
                return {"ok": True}

            # сохраняем скорректированный cursor
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "UPDATE pending_jobs SET cursor = ? WHERE tg_user = ?",
                    (cursor, uid),
                )
                await db.commit()
            app.state.cursor.setdefault(uid, {})["jobs"] = cursor

            job = jobs[cursor]                       # гарантированно dict
            caption, logo = format_vacancy(job)      # текст + картинка (если есть)

            kb_next = build_job_kb(job["id"])

            # убираем старое сообщение и отправляем новое
            await bot.delete_message(uid, call.message.message_id)
            if logo:
                await bot.send_photo(
                    uid,
                    logo,
                    caption=caption,
                    parse_mode="HTML",
                    reply_markup=kb_next,
                )
            else:
                await bot.send_message(
                    uid,
                    caption,
                    parse_mode="HTML",
                    reply_markup=kb_next,
                )

            await bot.answer_callback_query(call.id)
            return {"ok": True}



        if data.startswith("job_apply_"):
            vac_id = data.split("_")[-1]

            # берём текст текущей вакансии из jobs_cache
            store = getattr(app.state, "jobs_by_id", {}).get(uid, {})
            job = store.get(vac_id)

            # запасной обход списка, если вдруг словарь пустой
            if not job:
                for j in getattr(app.state, "jobs_cache", {}).get(uid, []):
                    if str(j["id"]) == vac_id:
                        job = j
                        break

            if not job:                                   # всё равно не нашли
                await bot.answer_callback_query(call.id, "⛔️ Вакансия не найдена")
                return {"ok": True}

            vacancy_text = job["snippet"] or job["name"]

            resume_text = await get_resume_summary(uid)
            cover = await generate_cover_letter(vacancy_text, resume_text)

            await send_apply(uid, vac_id, cover)
            await bot.answer_callback_query(call.id, "🔔 Отклик + письмо отправлены!")
            return {"ok": True}


        if data.startswith("job_fav_"):
            vac_id = int(data.split("_")[-1])

            # --- универсально берём HTML-текст карточки (text или caption)
            content_html = (
                getattr(call.message, "html_text", None)
                or getattr(call.message, "caption_html", None)
                or call.message.text
                or call.message.caption
                or ""
            )
            title = content_html.split("\n")[0].replace("<b>", "").replace("</b>", "")

            # ссылки могут быть в entities ИЛИ caption_entities
            entities = call.message.entities or call.message.caption_entities or []
            url_ent  = next((e for e in entities if e.type == "text_link"), None)

            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    """
                    INSERT OR IGNORE INTO user_favorites(tg_user, vacancy_id, title, url)
                    VALUES(?, ?, ?, ?)
                    """,
                    (uid, vac_id, title, url_ent.url if url_ent else "")
                )
                await db.commit()

            await bot.answer_callback_query(call.id, "⭐️ Добавлено в избранное")
            return {"ok": True}


    # ===== TEXT =====
    if update.message and update.message.text:
        msg = update.message
        uid = msg.from_user.id
        text = msg.text.strip()
        pending = await get_pending(uid)

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("INSERT OR IGNORE INTO users(chat_id) VALUES (?)", (uid,))
            await db.commit()

        try:
            # ---------- commands ----------
            if text.startswith("/"):
                # ───── /start ───────────────────────────────────────────
                if text == "/start":
                    await set_pending(uid, None)

                    intro = (
                        "<b>👋 Добро пожаловать!</b>\n\n"
                        "Этот бот автоматически откликается на подходящие вакансии hh.ru по вашим фильтрам. "
                        "Настройте регион, зарплату, график и выберите резюме — остальное я сделаю сам."
                    )

                    token = await get_user_token(uid)
                    if token is None:
                        # ещё не авторизован — показываем ссылку OAuth
                        kb = types.InlineKeyboardMarkup(
                            inline_keyboard=[[types.InlineKeyboardButton(
                                text="🚀 Начать автоотклики", url=build_oauth_url(uid)
                            )]]
                        )
                        await bot.send_message(
                            uid,
                            intro
                            + "\n\n<b>Шаг 1.</b> Нажмите кнопку ниже и дайте боту доступ к вашему аккаунту hh.ru.",
                            reply_markup=kb,
                            parse_mode="HTML",
                        )
                        return {"ok": True}

                    # уже есть токен — сразу выводим меню
                    menu_msg = await bot.send_message(
                        uid,
                        f"✅ Вы уже авторизованы.\n\n{intro}",
                        reply_markup=build_main_menu_keyboard(),
                        parse_mode="HTML",
                    )
                    await set_settings_msg_id(uid, menu_msg.message_id)
                    return {"ok": True}

                                 # ───── /jobs — показать вакансии ─────
                                # ───── /jobs — показать вакансии ─────
                if text == "/jobs":
                    await set_pending(uid, None)
                    await run_jobs(uid)
                    return {"ok": True}


                # ───── /menu ────────────────────────────────────────────
                if text == "/menu":
                    await set_pending(uid, None)
                    menu_msg = await bot.send_message(
                        uid,
                        "📌 Главное меню:",
                        reply_markup=build_main_menu_keyboard(),
                    )
                    await set_settings_msg_id(uid, menu_msg.message_id)
                    return {"ok": True}

                if text == "/settings":
                    await set_pending(uid, None)
                    msg = await bot.send_message(
                        uid, "Ваши фильтры:", reply_markup=build_settings_keyboard()
                    )
                    await set_settings_msg_id(uid, msg.message_id)
                    return {"ok": True}

            if pending == "region":
                await save_user_setting(uid, "region", text)
                await set_pending(uid, None)
                msg_id = await get_settings_msg_id(uid)
                await safe_edit_text_by_id(
                    uid, msg_id, "Ваши фильтры:", build_settings_keyboard()
                )
                return {"ok": True}

            if pending == "salary" and text.isdigit():
                await save_user_setting(uid, "salary", text)
                await set_pending(uid, None)
                msg_id = await get_settings_msg_id(uid)
                await safe_edit_text_by_id(
                    uid, msg_id, "Ваши фильтры:", build_settings_keyboard()
                )
                return {"ok": True}

            if pending == "keyword":
                await save_user_setting(uid, "keyword", text)
                await set_pending(uid, None)
                msg_id = await get_settings_msg_id(uid)
                await safe_edit_text_by_id(
                    uid, msg_id, "Ваши фильтры:", build_settings_keyboard()
                )
                return {"ok": True}
        finally:
            if pending:
                await safe_delete(msg)