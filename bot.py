# ===================== IMPORTS =====================
import os
import asyncio
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional, List

import aiohttp
from aiohttp import web

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import CommandStart
from aiogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
)

# ===================== CONFIG =====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
LIQUIPEDIA_USER_AGENT = os.getenv(
    "LIQUIPEDIA_USER_AGENT",
    "PredBot/1.0 (contact: @example)"
)

DB_PATH = "bot.db"

logging.basicConfig(level=logging.INFO)

# ===================== BOT =====================
bot = Bot(
    BOT_TOKEN,
    default=DefaultBotProperties(parse_mode="HTML")
)
dp = Dispatcher()

# ===================== DATABASE =====================
def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    con = db()
    cur = con.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            sport TEXT,
            league TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS matches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sport TEXT,
            league TEXT,
            team1 TEXT,
            team2 TEXT,
            start_time TEXT,
            status TEXT
        )
    """)

    con.commit()
    con.close()

# ===================== WEB SERVER (RENDER) =====================
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
    logging.info(f"Web server started on port {port}")

# ===================== KEYBOARDS =====================
def sport_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚽ Футбол", callback_data="sport:football")],
        [InlineKeyboardButton(text="🏒 Хоккей", callback_data="sport:hockey")],
        [InlineKeyboardButton(text="🎮 Киберспорт", callback_data="sport:esports")]
    ])

def hockey_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🇺🇸 NHL", callback_data="league:NHL")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back:sport")]
    ])

def esports_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔫 CS2", callback_data="league:CS2")],
        [InlineKeyboardButton(text="🧙 Dota 2", callback_data="league:Dota2")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back:sport")]
    ])

def main_menu():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="⚽ Активные матчи")],
            [KeyboardButton(text="🏟 Выбрать спорт")],
            [KeyboardButton(text="❓ Помощь")]
        ],
        resize_keyboard=True
    )

# ===================== HELPERS =====================
def get_user(user_id: int):
    con = db()
    cur = con.cursor()
    cur.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    con.close()
    return row

def save_user(user_id: int, username: str, sport: str, league: Optional[str]):
    con = db()
    cur = con.cursor()
    cur.execute("""
        INSERT INTO users (user_id, username, sport, league)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
        username=excluded.username,
        sport=excluded.sport,
        league=excluded.league
    """, (user_id, username, sport, league))
    con.commit()
    con.close()

# ===================== START =====================
@dp.message(CommandStart())
async def start_cmd(message: Message):
    user = get_user(message.from_user.id)
    if not user or not user["sport"]:
        await message.answer(
            "👋 Добро пожаловать!\nВыбери вид спорта:",
            reply_markup=sport_keyboard()
        )
        return

    await message.answer(
        "Главное меню:",
        reply_markup=main_menu()
    )

# ===================== SPORT SELECTION =====================
@dp.callback_query(F.data.startswith("sport:"))
async def choose_sport(cb):
    sport = cb.data.split(":")[1]

    if sport == "football":
        save_user(cb.from_user.id, cb.from_user.username, "football", None)
        await cb.message.answer("⚽ Футбол выбран", reply_markup=main_menu())

    elif sport == "hockey":
        save_user(cb.from_user.id, cb.from_user.username, "hockey", None)
        await cb.message.answer("🏒 Выбери лигу:", reply_markup=hockey_keyboard())

    elif sport == "esports":
        save_user(cb.from_user.id, cb.from_user.username, "esports", None)
        await cb.message.answer("🎮 Выбери дисциплину:", reply_markup=esports_keyboard())

    await cb.answer()

@dp.callback_query(F.data.startswith("league:"))
async def choose_league(cb):
    league = cb.data.split(":")[1]
    user = get_user(cb.from_user.id)

    save_user(cb.from_user.id, cb.from_user.username, user["sport"], league)
    await cb.message.answer(
        f"✅ Выбрано: {user['sport'].upper()} / {league}",
        reply_markup=main_menu()
    )
    await cb.answer()

@dp.callback_query(F.data == "back:sport")
async def back_to_sport(cb):
    await cb.message.answer("Выбери вид спорта:", reply_markup=sport_keyboard())
    await cb.answer()

# ===================== ACTIVE MATCHES =====================
@dp.message(F.text == "⚽ Активные матчи")
async def active_matches(message: Message):
    user = get_user(message.from_user.id)
    if not user:
        await message.answer("Сначала /start")
        return

    con = db()
    cur = con.cursor()
    cur.execute(
        "SELECT * FROM matches WHERE sport=? AND (league=? OR ? IS NULL)",
        (user["sport"], user["league"], user["league"])
    )
    rows = cur.fetchall()
    con.close()

    if not rows:
        await message.answer("Нет активных матчей.")
        return

    text = "📋 Активные матчи:\n\n"
    for m in rows:
        text += f"• {m['team1']} vs {m['team2']}\n"

    await message.answer(text)

# ===================== HELP =====================
@dp.message(F.text == "❓ Помощь")
async def help_cmd(message: Message):
    await message.answer(
        "ℹ️ Это мультиспорт-бот прогнозов.\n\n"
        "1️⃣ Выбери спорт\n"
        "2️⃣ Смотри активные матчи\n"
        "3️⃣ Делай прогнозы (в разработке)\n\n"
        "Поддержка: @your_username"
    )

# ===================== MAIN =====================
async def main():
    init_db()
    asyncio.create_task(start_web())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
