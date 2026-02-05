# ===================== IMPORTS =====================
import os
import asyncio
import logging
import sqlite3
from datetime import datetime, timezone, timedelta
from typing import Optional

import aiohttp
from aiohttp import web

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import CommandStart
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
)

# ===================== CONFIG =====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
FOOTBALL_API_KEY = os.getenv("FOOTBALL_DATA_API_KEY")
LIQUIPEDIA_UA = os.getenv("LIQUIPEDIA_USER_AGENT", "PredBot/1.0")

DB_PATH = "bot.db"
SYNC_INTERVAL = 300            # 5 минут
PREDICT_CLOSE_SEC = 120        # закрытие прогнозов

logging.basicConfig(level=logging.INFO)

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN not set")

# ===================== BOT =====================
bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()

# ===================== DB =====================
def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    con = db()
    cur = con.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS users(
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        sport TEXT,
        league TEXT,
        points INTEGER DEFAULT 0,
        correct INTEGER DEFAULT 0,
        total INTEGER DEFAULT 0
    )""")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS matches(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sport TEXT,
        league TEXT,
        team1 TEXT,
        team2 TEXT,
        start_time TEXT,
        status TEXT,
        result1 INTEGER,
        result2 INTEGER,
        external_id TEXT
    )""")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS predictions(
        match_id INTEGER,
        user_id INTEGER,
        pick TEXT,
        UNIQUE(match_id, user_id)
    )""")

    con.commit()
    con.close()

# ===================== WEB (Render) =====================
async def health(request):
    return web.Response(text="OK")

async def start_web():
    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", "10000"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

# ===================== HELPERS =====================
def now_utc():
    return datetime.now(timezone.utc)

async def fetch_json(url, headers=None):
    async with aiohttp.ClientSession(headers=headers) as s:
        async with s.get(url, timeout=20) as r:
            if r.status != 200:
                return None
            return await r.json()

def allow_draw(sport):
    return sport == "football"

# ===================== KEYBOARDS =====================
def main_menu():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="⚽ Активные матчи")],
            [KeyboardButton(text="👤 Профиль"), KeyboardButton(text="🏆 Лидерборд")],
            [KeyboardButton(text="🏟 Выбрать спорт"), KeyboardButton(text="❓ Помощь")]
        ],
        resize_keyboard=True
    )

def sport_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚽ Футбол", callback_data="sport:football")],
        [InlineKeyboardButton(text="🏒 NHL", callback_data="sport:hockey")],
        [InlineKeyboardButton(text="🎮 CS2", callback_data="sport:esports")]
    ])

# ===================== START =====================
@dp.message(CommandStart())
async def start_cmd(message: Message):
    con = db()
    cur = con.cursor()
    cur.execute("INSERT OR IGNORE INTO users(user_id,username,first_name) VALUES(?,?,?)",
                (message.from_user.id, message.from_user.username, message.from_user.first_name))
    con.commit()
    con.close()

    await message.answer("Выбери спорт:", reply_markup=sport_kb())

@dp.message(F.text == "🏟 Выбрать спорт")
async def choose_sport(message: Message):
    await message.answer("Выбери спорт:", reply_markup=sport_kb())

@dp.callback_query(F.data.startswith("sport:"))
async def set_sport(cb: CallbackQuery):
    sport = cb.data.split(":")[1]
    con = db()
    cur = con.cursor()
    cur.execute("UPDATE users SET sport=?, league=? WHERE user_id=?",
                (sport, "NHL" if sport=="hockey" else "CS2" if sport=="esports" else None, cb.from_user.id))
    con.commit()
    con.close()

    await cb.message.answer("Готово", reply_markup=main_menu())
    await cb.answer()

# ===================== ACTIVE MATCHES =====================
@dp.message(F.text == "⚽ Активные матчи")
async def active_matches(message: Message):
    con = db()
    cur = con.cursor()
    cur.execute("SELECT sport, league FROM users WHERE user_id=?", (message.from_user.id,))
    u = cur.fetchone()
    if not u or not u["sport"]:
        await message.answer("Сначала выбери спорт")
        return

    cur.execute("""
        SELECT * FROM matches
        WHERE sport=? AND (league=? OR league IS NULL) AND status!='finished'
        ORDER BY start_time
    """, (u["sport"], u["league"]))
    rows = cur.fetchall()
    con.close()

    if not rows:
        await message.answer("Матчей нет")
        return

    for m in rows:
        text = f"<b>{m['team1']} vs {m['team2']}</b>\n🕒 {m['start_time']}\nСтатус: {m['status']}"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="1", callback_data=f"pick:{m['id']}:1"),
                InlineKeyboardButton(text="X", callback_data=f"pick:{m['id']}:X") if allow_draw(m["sport"]) else InlineKeyboardButton(text="2", callback_data=f"pick:{m['id']}:2"),
                InlineKeyboardButton(text="2", callback_data=f"pick:{m['id']}:2")
            ]
        ])
        await message.answer(text, reply_markup=kb)

# ===================== PICKS =====================
@dp.callback_query(F.data.startswith("pick:"))
async def pick(cb: CallbackQuery):
    _, mid, pick = cb.data.split(":")
    con = db()
    cur = con.cursor()

    cur.execute("SELECT start_time FROM matches WHERE id=?", (mid,))
    m = cur.fetchone()
    if not m:
        await cb.answer("Матч не найден")
        return

    if now_utc() >= datetime.fromisoformat(m["start_time"]) - timedelta(seconds=PREDICT_CLOSE_SEC):
        await cb.answer("Прогнозы закрыты")
        return

    cur.execute("INSERT OR REPLACE INTO predictions(match_id,user_id,pick) VALUES(?,?,?)",
                (mid, cb.from_user.id, pick))
    con.commit()
    con.close()
    await cb.answer("Прогноз принят")

# ===================== PROFILE =====================
@dp.message(F.text == "👤 Профиль")
async def profile(message: Message):
    con = db()
    cur = con.cursor()
    cur.execute("SELECT * FROM users WHERE user_id=?", (message.from_user.id,))
    u = cur.fetchone()
    con.close()

    if not u:
        return

    acc = (u["correct"]/u["total"]*100) if u["total"] else 0
    await message.answer(
        f"👤 @{u['username']}\n"
        f"🏆 Очки: {u['points']}\n"
        f"📊 Точность: {acc:.0f}%"
    )

# ===================== LEADERBOARD =====================
@dp.message(F.text == "🏆 Лидерборд")
async def leaderboard(message: Message):
    con = db()
    cur = con.cursor()
    cur.execute("SELECT username, points FROM users ORDER BY points DESC LIMIT 10")
    rows = cur.fetchall()
    con.close()

    text = "🏆 <b>Лидерборд</b>\n\n"
    for i,r in enumerate(rows,1):
        text += f"{i}. @{r['username']} — {r['points']}\n"

    await message.answer(text)

# ===================== HELP =====================
@dp.message(F.text == "❓ Помощь")
async def help_cmd(message: Message):
    await message.answer("Автосинк матчей + авто-итоги работают автоматически.")

# ===================== AUTOSYNC =====================
async def sync_football():
    if not FOOTBALL_API_KEY:
        return
    headers = {"X-Auth-Token": FOOTBALL_API_KEY}
    data = await fetch_json("https://api.football-data.org/v4/matches", headers)
    if not data:
        return

    con = db()
    cur = con.cursor()
    for m in data.get("matches", []):
        ext = str(m["id"])
        home = m["homeTeam"]["name"]
        away = m["awayTeam"]["name"]
        start = m["utcDate"]
        status = m["status"]

        cur.execute("SELECT id,status FROM matches WHERE external_id=?", (ext,))
        row = cur.fetchone()
        if not row:
            cur.execute("""
                INSERT INTO matches(sport,league,team1,team2,start_time,status,external_id)
                VALUES('football',?,?,?,?,'open',?)
            """, (m["competition"]["name"], home, away, start, ext))
        elif status == "FINISHED" and row["status"] != "finished":
            s1 = m["score"]["fullTime"]["home"]
            s2 = m["score"]["fullTime"]["away"]
            cur.execute("""
                UPDATE matches SET status='finished',result1=?,result2=? WHERE external_id=?
            """, (s1, s2, ext))
    con.commit()
    con.close()

async def autosync_loop():
    while True:
        try:
            await sync_football()
        except Exception:
            logging.exception("autosync error")
        await asyncio.sleep(SYNC_INTERVAL)

# ===================== MAIN =====================
async def main():
    init_db()
    asyncio.create_task(start_web())
    asyncio.create_task(autosync_loop())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
