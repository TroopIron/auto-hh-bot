import aiosqlite, asyncio

DB_PATH = "tg_users.db"

async def upgrade(db: aiosqlite.Connection):
    # --- базовые таблицы ---
    await db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            chat_id INTEGER PRIMARY KEY
        );
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS user_tokens (
            tg_user       INTEGER PRIMARY KEY,
            access_token  TEXT    NOT NULL,
            refresh_token TEXT    NOT NULL,
            expires_at    INTEGER NOT NULL
        );
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS queues (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            tg_user    INTEGER NOT NULL,
            vacancy_id TEXT    NOT NULL,
            created_at INTEGER NOT NULL DEFAULT (strftime('%s','now'))
        );
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS user_settings (
            tg_user INTEGER,
            key     TEXT,
            value   TEXT,
            PRIMARY KEY (tg_user, key)
        );
    """)

    # --- новые таблицы ---
    await db.execute("""
        CREATE TABLE IF NOT EXISTS pending_jobs (
            tg_user   INTEGER PRIMARY KEY,
            jobs_json TEXT,
            cursor    INTEGER DEFAULT 0
        );
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS user_favorites (
            tg_user    INTEGER,
            vacancy_id INTEGER,
            title      TEXT,
            url        TEXT,
            PRIMARY KEY (tg_user, vacancy_id)
        );
    """)

    await db.commit()

async def main():
    async with aiosqlite.connect(DB_PATH) as db:
        await upgrade(db)

asyncio.run(main())
