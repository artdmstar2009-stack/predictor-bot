import asyncio
import logging
import os
import sqlite3
from datetime import datetime

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

# ================== НАСТРОЙКИ ==================
TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))  # обязательно выставь
DB_PATH = "predictor.db"
# ===============================================

dp = Dispatcher()


# ------------------ DB ------------------
def db():
    return sqlite3.connect(DB_PATH)

def init_db():
    with db() as con:
        con.execute("""
        CREATE TABLE IF NOT EXISTS matches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            created_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',   -- open/closed/scored
            result TEXT                             -- home/draw/away
        )
        """)
        con.execute("""
        CREATE TABLE IF NOT EXISTS votes (
            match_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            username TEXT,
            choice TEXT NOT NULL,                  -- home/draw/away
            voted_at TEXT NOT NULL,
            PRIMARY KEY (match_id, user_id)
        )
        """)
        con.execute("""
        CREATE TABLE IF NOT EXISTS scores (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            points INTEGER NOT NULL DEFAULT 0,
            last_update TEXT
        )
        """)
        con.commit()

def is_admin(user_id: int) -> bool:
    return ADMIN_ID != 0 and user_id == ADMIN_ID

def create_match(title: str) -> int:
    with db() as con:
        cur = con.execute(
            "INSERT INTO matches(title, created_at, status) VALUES(?,?, 'open')",
            (title, datetime.utcnow().isoformat())
        )
        con.commit()
        return cur.lastrowid

def list_open_matches():
    with db() as con:
        cur = con.execute("SELECT id, title, created_at FROM matches WHERE status='open' ORDER BY id DESC")
        return cur.fetchall()

def get_match(match_id: int):
    with db() as con:
        cur = con.execute("SELECT id, title, status, result FROM matches WHERE id=?", (match_id,))
        return cur.fetchone()

def set_vote(match_id: int, user_id: int, username: str | None, choice: str):
    with db() as con:
        con.execute("""
        INSERT INTO votes(match_id, user_id, username, choice, voted_at)
        VALUES(?,?,?,?,?)
        ON CONFLICT(match_id, user_id) DO UPDATE SET
            username=excluded.username,
            choice=excluded.choice,
            voted_at=excluded.voted_at
        """, (match_id, user_id, username, choice, datetime.utcnow().isoformat()))
        con.commit()

def close_match(match_id: int):
    with db() as con:
        con.execute("UPDATE matches SET status='closed' WHERE id=? AND status='open'", (match_id,))
        con.commit()

def set_result_and_score(match_id: int, result: str):
    # выставляем результат и начисляем очки
    with db() as con:
        # матч должен быть closed
        cur = con.execute("SELECT status FROM matches WHERE id=?", (match_id,))
        row = cur.fetchone()
        if not row:
            return "not_found"
        if row[0] == "scored":
            return "already_scored"
        if row[0] != "closed":
            return "not_closed"

        con.execute("UPDATE matches SET result=?, status='scored' WHERE id=?", (result, match_id))

        # всем, кто угадал — +1
        cur = con.execute("SELECT user_id, COALESCE(username,'') FROM votes WHERE match_id=? AND choice=?",
                          (match_id, result))
        winners = cur.fetchall()

        for user_id, username in winners:
            con.execute("""
            INSERT INTO scores(user_id, username, points, last_update)
            VALUES(?,?,1,?)
            ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username,
                points=points+1,
                last_update=excluded.last_update
            """, (user_id, username, datetime.utcnow().isoformat()))

        con.commit()
        return ("ok", len(winners))

def count_votes(match_id: int):
    with db() as con:
        cur = con.execute("""
        SELECT choice, COUNT(*) FROM votes
        WHERE match_id=?
        GROUP BY choice
        """, (match_id,))
        data = dict(cur.fetchall())
    return {
        "home": data.get("home", 0),
        "draw": data.get("draw", 0),
        "away": data.get("away", 0),
    }

def user_votes(user_id: int):
    with db() as con:
        cur = con.execute("""
        SELECT v.match_id, m.title, v.choice, m.status
        FROM votes v
        JOIN matches m ON m.id=v.match_id
        WHERE v.user_id=?
        ORDER BY v.match_id DESC
        """, (user_id,))
        return cur.fetchall()

def leaderboard(limit: int = 10):
    with db() as con:
        cur = con.execute("""
        SELECT COALESCE(username,''), user_id, points
        FROM scores
        ORDER BY points DESC, user_id ASC
        LIMIT ?
        """, (limit,))
        return cur.fetchall()


# ------------------ UI helpers ------------------
def vote_kb(match_id: int):
    kb = InlineKeyboardBuilder()
    kb.button(text="🏠 Победа хозяев", callback_data=f"vote:{match_id}:home")
    kb.button(text="🤝 Ничья", callback_data=f"vote:{match_id}:draw")
    kb.button(text="🚌 Победа гостей", callback_data=f"vote:{match_id}:away")
    kb.adjust(1)
    return kb.as_markup()

def choice_label(choice: str):
    return {"home": "Победа хозяев", "draw": "Ничья", "away": "Победа гостей"}.get(choice, choice)


# ------------------ Handlers ------------------
@dp.message(CommandStart())
async def start(message: Message):
    await message.answer(
        "Ку! Я бот-предиктор 🤖\n\n"
        "Команды:\n"
        "/matches — активные матчи\n"
        "/my — мои голоса\n"
        "/leaderboard — топ игроков\n\n"
        "Голосование без денег — просто угадай исход 🙂"
    )

@dp.message(Command("matches"))
async def matches_cmd(message: Message):
    rows = list_open_matches()
    if not rows:
        await message.answer("Активных матчей нет.")
        return

    text = "Активные матчи:\n"
    for mid, title, created_at in rows:
        text += f"\n• #{mid} — {title}\n  Голосовать: /vote {mid}"
    await message.answer(text)

@dp.message(Command("vote"))
async def vote_cmd(message: Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("Используй: /vote <id>\nПример: /vote 3")
        return

    match_id = int(parts[1])
    m = get_match(match_id)
    if not m:
        await message.answer("Матч не найден.")
        return

    _, title, status, result = m
    if status != "open":
        await message.answer(f"Голосование закрыто. Статус: {status}.")
        return

    stats = count_votes(match_id)
    await message.answer(
        f"Матч #{match_id}: {title}\n\n"
        f"Текущие голоса:\n"
        f"🏠 {stats['home']}  🤝 {stats['draw']}  🚌 {stats['away']}\n\n"
        "Выбери исход:",
        reply_markup=vote_kb(match_id)
    )

@dp.callback_query(F.data.startswith("vote:"))
async def vote_cb(call: CallbackQuery):
    _, mid, choice = call.data.split(":")
    match_id = int(mid)

    m = get_match(match_id)
    if not m:
        await call.answer("Матч не найден.", show_alert=True)
        return

    _, title, status, _ = m
    if status != "open":
        await call.answer("Голосование уже закрыто.", show_alert=True)
        return

    user = call.from_user
    set_vote(match_id, user.id, user.username, choice)

    stats = count_votes(match_id)
    await call.message.edit_text(
        f"Матч #{match_id}: {title}\n\n"
        f"Текущие голоса:\n"
        f"🏠 {stats['home']}  🤝 {stats['draw']}  🚌 {stats['away']}\n\n"
        f"Твой выбор: **{choice_label(choice)}**",
        parse_mode="Markdown",
        reply_markup=vote_kb(match_id)
    )
    await call.answer("Голос учтён ✅")

@dp.message(Command("my"))
async def my_cmd(message: Message):
    rows = user_votes(message.from_user.id)
    if not rows:
        await message.answer("У тебя пока нет голосов.")
        return

    text = "Твои голоса:\n"
    for mid, title, choice, status in rows[:20]:
        text += f"\n• #{mid} — {title}\n  Выбор: {choice_label(choice)} | Статус: {status}"
    await message.answer(text)

@dp.message(Command("leaderboard"))
async def lb_cmd(message: Message):
    rows = leaderboard(10)
    if not rows:
        await message.answer("Пока нет результатов. Сначала нужно закрыть матч и поставить итог.")
        return

    text = "🏆 Топ-10 по очкам:\n"
    for i, (username, user_id, points) in enumerate(rows, start=1):
        name = f"@{username}" if username else f"id:{user_id}"
        text += f"\n{i}. {name} — {points}"
    await message.answer(text)

# --------- Admin commands ----------
@dp.message(Command("newmatch"))
async def newmatch_cmd(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("Эта команда только для админа.")
        return

    title = message.text.split(maxsplit=1)[1] if len(message.text.split(maxsplit=1)) > 1 else ""
    if not title.strip():
        await message.answer("Используй: /newmatch <название>\nПример: /newmatch Real vs Barca (02.02 20:00)")
        return

    mid = create_match(title.strip())
    await message.answer(f"Создан матч #{mid}: {title}\nГолосовать: /vote {mid}")

@dp.message(Command("close"))
async def close_cmd(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("Эта команда только для админа.")
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("Используй: /close <id>\nПример: /close 3")
        return

    mid = int(parts[1])
    close_match(mid)
    await message.answer(f"Матч #{mid} закрыт для голосования ✅")

@dp.message(Command("setresult"))
async def setresult_cmd(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("Эта команда только для админа.")
        return

    parts = message.text.split()
    if len(parts) != 3 or not parts[1].isdigit() or parts[2] not in ("home", "draw", "away"):
        await message.answer("Используй: /setresult <id> <home|draw|away>\nПример: /setresult 3 home")
        return

    mid = int(parts[1])
    result = parts[2]

    res = set_result_and_score(mid, result)
    if res == "not_found":
        await message.answer("Матч не найден.")
    elif res == "not_closed":
        await message.answer("Сначала закрой матч: /close <id>")
    elif res == "already_scored":
        await message.answer("Результат уже выставлен ранее.")
    else:
        _, winners = res
        await message.answer(f"Результат для #{mid}: {choice_label(result)} ✅\nОчки начислены. Победителей: {winners}")

# ------------------ main ------------------
async def main():
    logging.basicConfig(level=logging.INFO)
    if not TOKEN:
        raise RuntimeError("Нет BOT_TOKEN. Задай переменную окружения BOT_TOKEN.")
    if ADMIN_ID == 0:
        raise RuntimeError("Нет ADMIN_ID. Задай переменную окружения ADMIN_ID (число).")

    init_db()
    bot = Bot(TOKEN)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())