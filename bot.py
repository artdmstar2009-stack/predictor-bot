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
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

# ===== CONFIG =====
TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
DB_PATH = "predictor.db"
# ==================

dp = Dispatcher()

# ===== Fake HTTP server (Render free) =====
class DummyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running")

def run_http():
    port = int(os.environ.get("PORT", 10000))
    HTTPServer(("0.0.0.0", port), DummyHandler).serve_forever()

threading.Thread(target=run_http, daemon=True).start()
# ========================================


# ===== DATABASE =====
def db():
    return sqlite3.connect(DB_PATH)

def init_db():
    with db() as con:
        con.execute("""
        CREATE TABLE IF NOT EXISTS matches(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT,
            status TEXT DEFAULT 'open'
        )
        """)
        con.execute("""
        CREATE TABLE IF NOT EXISTS votes(
            match_id INTEGER,
            user_id INTEGER,
            username TEXT,
            bet_type TEXT,
            choice TEXT,
            PRIMARY KEY(match_id, user_id)
        )
        """)
        con.commit()

def is_admin(uid): 
    return uid == ADMIN_ID


# ===== MENUS =====
def main_menu(is_admin=False):
    kb = ReplyKeyboardBuilder()
    kb.button(text="⚽ Активные матчи")
    kb.button(text="🗳 Сделать прогноз")
    kb.button(text="📊 Мои прогнозы")
    kb.button(text="🏆 Лидерборд")
    kb.button(text="ℹ️ Помощь")
    if is_admin:
        kb.button(text="➕ Создать матч")
    kb.adjust(2)
    return kb.as_markup(resize_keyboard=True)


# ===== INLINE KEYBOARDS =====
def bet_type_kb(mid):
    kb = InlineKeyboardBuilder()
    kb.button(text="1️⃣ 1X2", callback_data=f"type:{mid}:1x2")
    kb.button(text="🎯 Точный счёт", callback_data=f"type:{mid}:score")
    kb.button(text="⚽ Тотал", callback_data=f"type:{mid}:total")
    kb.button(text="🔥 Обе забьют", callback_data=f"type:{mid}:btts")
    kb.adjust(2)
    return kb.as_markup()

def kb_1x2(mid):
    kb = InlineKeyboardBuilder()
    kb.button(text="🏠 Хозяева", callback_data=f"vote:{mid}:1x2:home")
    kb.button(text="🤝 Ничья", callback_data=f"vote:{mid}:1x2:draw")
    kb.button(text="🚌 Гости", callback_data=f"vote:{mid}:1x2:away")
    kb.adjust(1)
    return kb.as_markup()

def kb_score(mid):
    kb = InlineKeyboardBuilder()
    for s in ["1:0","2:1","1:1","0:0"]:
        kb.button(text=s, callback_data=f"vote:{mid}:score:{s}")
    kb.adjust(2)
    return kb.as_markup()

def kb_total(mid):
    kb = InlineKeyboardBuilder()
    kb.button(text="⚽ Больше 2.5", callback_data=f"vote:{mid}:total:over")
    kb.button(text="🧱 Меньше 2.5", callback_data=f"vote:{mid}:total:under")
    kb.adjust(1)
    return kb.as_markup()

def kb_btts(mid):
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Да", callback_data=f"vote:{mid}:btts:yes")
    kb.button(text="❌ Нет", callback_data=f"vote:{mid}:btts:no")
    kb.adjust(1)
    return kb.as_markup()


# ===== HANDLERS =====
@dp.message(CommandStart())
async def start(m: Message):
    await m.answer(
        "🤖 Добро пожаловать в Predictor Bot!\n\n"
        "Здесь ты можешь:\n"
        "• смотреть матчи\n"
        "• делать прогнозы\n"
        "• набирать очки\n\n"
        "Выбирай действие в меню 👇",
        reply_markup=main_menu(is_admin(m.from_user.id))
    )

# ---- Меню кнопки ----

@dp.message(F.text == "⚽ Активные матчи")
async def active_matches(m: Message):
    with db() as con:
        rows = con.execute("SELECT id,title FROM matches WHERE status='open'").fetchall()
    if not rows:
        await m.answer("Нет активных матчей.")
        return
    text = "Активные матчи:\n"
    for mid, title in rows:
        text += f"\n#{mid} {title}"
    await m.answer(text)

@dp.message(F.text == "🗳 Сделать прогноз")
async def make_vote(m: Message):
    with db() as con:
        rows = con.execute("SELECT id,title FROM matches WHERE status='open'").fetchall()
    if not rows:
        await m.answer("Нет матчей для прогнозов.")
        return
    text = "Выбери матч:\n"
    for mid, title in rows:
        text += f"\n/vote {mid} — {title}"
    await m.answer(text)

@dp.message(F.text == "📊 Мои прогнозы")
async def my_votes(m: Message):
    with db() as con:
        rows = con.execute("""
        SELECT match_id, bet_type, choice 
        FROM votes WHERE user_id=?
        """, (m.from_user.id,)).fetchall()
    if not rows:
        await m.answer("Ты ещё не делал прогнозов.")
        return
    text = "Твои прогнозы:\n"
    for mid, bt, ch in rows:
        text += f"\nМатч #{mid}: {bt} → {ch}"
    await m.answer(text)

@dp.message(F.text == "🏆 Лидерборд")
async def leaderboard(m: Message):
    await m.answer("🏆 Лидерборд пока в разработке.\n(Добавим начисление очков следующим шагом)")

@dp.message(F.text == "ℹ️ Помощь")
async def help_menu(m: Message):
    await m.answer(
        "ℹ️ Помощь\n\n"
        "1. Нажми «Активные матчи»\n"
        "2. Потом «Сделать прогноз»\n"
        "3. Выбери матч и тип ставки\n\n"
        "Всё делается кнопками."
    )

# ---- Админ ----
@dp.message(F.text == "➕ Создать матч")
async def newmatch_menu(m: Message):
    if not is_admin(m.from_user.id): 
        return
    await m.answer("Введи:\n/newmatch <название>")

@dp.message(Command("newmatch"))
async def newmatch_cmd(m: Message):
    if not is_admin(m.from_user.id): 
        return
    title = m.text.replace("/newmatch","").strip()
    with db() as con:
        con.execute("INSERT INTO matches(title) VALUES(?)", (title,))
        con.commit()
    await m.answer("Матч создан ✅")

# ---- Прогнозы ----
@dp.message(Command("vote"))
async def vote_cmd(m: Message):
    mid = int(m.text.split()[1])
    await m.answer("Выбери тип прогноза:", reply_markup=bet_type_kb(mid))

@dp.callback_query(F.data.startswith("type:"))
async def choose_type(c: CallbackQuery):
    _, mid, t = c.data.split(":")
    mid = int(mid)
    if t == "1x2":
        await c.message.edit_text("Исход 1X2:", reply_markup=kb_1x2(mid))
    elif t == "score":
        await c.message.edit_text("Точный счёт:", reply_markup=kb_score(mid))
    elif t == "total":
        await c.message.edit_text("Тотал:", reply_markup=kb_total(mid))
    elif t == "btts":
        await c.message.edit_text("Обе забьют?", reply_markup=kb_btts(mid))

@dp.callback_query(F.data.startswith("vote:"))
async def save_vote(c: CallbackQuery):
    _, mid, bet_type, choice = c.data.split(":")
    with db() as con:
        con.execute("""
        INSERT OR REPLACE INTO votes
        VALUES (?,?,?,?,?)
        """, (int(mid), c.from_user.id, c.from_user.username, bet_type, choice))
        con.commit()
    await c.answer("Прогноз сохранён ✅", show_alert=True)

# ===== MAIN =====
async def main():
    logging.basicConfig(level=logging.INFO)
    init_db()
    bot = Bot(TOKEN)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
