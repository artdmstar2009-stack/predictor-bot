import asyncio
import logging
import os
import sqlite3
from datetime import datetime
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

# ================== НАСТРОЙКИ ==================
TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
DB_PATH = "predictor.db"
# ===============================================

dp = Dispatcher()

# ------------------ FAKE HTTP SERVER (для Render) ------------------
class DummyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running")

def run_http_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), DummyHandler)
    server.serve_forever()

threading.Thread(target=run_http_server, daemon=True).start()
# ------------------------------------------------------------------


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
            status TEXT NOT NULL DEFAULT 'open',
            result TEXT
        )
        """)
        con.execute("""
        CREATE TABLE IF NOT EXISTS votes (
            match_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            username TEXT,
            choice TEXT NOT NULL,
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
        cur = con.execute("SELECT id, title FROM matches WHERE status='open'")
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
            choice=excluded.choice,
            voted_at=excluded.voted_at
        """, (match_id, user_id, username, choice, datetime.utcnow().isoformat()))
        con.commit()

def close_match(match_id: int):
    with db() as con:
        con.execute("UPDATE matches SET status='closed' WHERE id=?", (match_id,))
        con.commit()

def set_result_and_score(match_id: int, result: str):
    with db() as con:
        cur = con.execute("SELECT status FROM matches WHERE id=?", (match_id,))
        row = cur.fetchone()
        if not row:
            return "not_found"
        if row[0] != "closed":
            return "not_closed"

        con.execute("UPDATE matches SET result=?, status='scored' WHERE id=?", (result, match_id))
        cur = con.execute("SELECT user_id, username FROM votes WHERE match_id=? AND choice=?",
                          (match_id, result))
        winners = cur.fetchall()

        for user_id, username in winners:
            con.execute("""
            INSERT INTO scores(user_id, username, points, last_update)
            VALUES(?,?,1,?)
            ON CONFLICT(user_id) DO UPDATE SET
                points=points+1
            """, (user_id, username, datetime.utcnow().isoformat()))

        con.commit()
        return len(winners)

def leaderboard():
    with db() as con:
        cur = con.execute("SELECT username, points FROM scores ORDER BY points DESC LIMIT 10")
        return cur.fetchall()

# ------------------ UI ------------------
def vote_kb(match_id: int):
    kb = InlineKeyboardBuilder()
    kb.button(text="🏠 Победа хозяев", callback_data=f"vote:{match_id}:home")
    kb.button(text="🤝 Ничья", callback_data=f"vote:{match_id}:draw")
    kb.button(text="🚌 Победа гостей", callback_data=f"vote:{match_id}:away")
    kb.adjust(1)
    return kb.as_markup()

def choice_label(choice: str):
    return {"home": "Победа хозяев", "draw": "Ничья", "away": "Победа гостей"}[choice]

# ------------------ HANDLERS ------------------
@dp.message(CommandStart())
async def start(message: Message):
    await message.answer(
        "Я бот-предиктор 🤖\n"
        "/matches — матчи\n"
        "/leaderboard — топ\n"
        "Админ: /newmatch /close /setresult"
    )

@dp.message(Command("matches"))
async def matches_cmd(message: Message):
    rows = list_open_matches()
    if not rows:
        await message.answer("Активных матчей нет.")
        return
    text = "Матчи:\n"
    for mid, title in rows:
        text += f"\n#{mid} — {title}\n/vote {mid}"
    await message.answer(text)

@dp.message(Command("vote"))
async def vote_cmd(message: Message):
    mid = int(message.text.split()[1])
    m = get_match(mid)
    if not m or m[2] != "open":
        await message.answer("Голосование закрыто.")
        return
    await message.answer(f"{m[1]}\nВыбери исход:", reply_markup=vote_kb(mid))

@dp.callback_query(F.data.startswith("vote:"))
async def vote_cb(call: CallbackQuery):
    _, mid, choice = call.data.split(":")
    set_vote(int(mid), call.from_user.id, call.from_user.username, choice)
    await call.answer("Голос принят ✅", show_alert=True)

@dp.message(Command("newmatch"))
async def newmatch_cmd(message: Message):
    if not is_admin(message.from_user.id):
        return
    title = message.text.replace("/newmatch", "").strip()
    mid = create_match(title)
    await message.answer(f"Создан матч #{mid}")

@dp.message(Command("close"))
async def close_cmd(message: Message):
    if not is_admin(message.from_user.id):
        return
    mid = int(message.text.split()[1])
    close_match(mid)
    await message.answer("Матч закрыт")

@dp.message(Command("setresult"))
async def setresult_cmd(message: Message):
    if not is_admin(message.from_user.id):
        return
    _, mid, result = message.text.split()
    winners = set_result_and_score(int(mid), result)
    await message.answer(f"Результат сохранён. Победителей: {winners}")

@dp.message(Command("leaderboard"))
async def lb_cmd(message: Message):
    rows = leaderboard()
    text = "🏆 Топ:\n"
    for i, (user, pts) in enumerate(rows, 1):
        text += f"{i}. @{user} — {pts}\n"
    await message.answer(text)

# ------------------ MAIN ------------------
async def main():
    logging.basicConfig(level=logging.INFO)
    if not TOKEN:
        raise RuntimeError("Нет BOT_TOKEN")
    if ADMIN_ID == 0:
        raise RuntimeError("Нет ADMIN_ID")

    init_db()
    bot = Bot(TOKEN)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
