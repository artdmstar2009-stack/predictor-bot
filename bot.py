# -*- coding: utf-8 -*-
"""
Minimal Predictor Bot (aiogram v3) — based on your current bot.py, but fixed:

✅ 1) Bot "dies" after a few hours:
   - start_polling() is wrapped into an auto-restart loop (network/Telegram hiccups won't kill the process).
   - auto_results loop catches exceptions and continues.

✅ 2) Active matches grouped by sport:
   - "⚽ Активные матчи" now shows sport categories first (Football / Hockey / Esports / Other / All).
   - After choosing a category, bot shows matches and adds #ID buttons into the reply keyboard.

Still: only 1X2 picks and auto-results (points +3 for correct).
"""

import asyncio
import logging
import os
import sqlite3
from typing import List, Tuple, Optional

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import (
    Message,
    CallbackQuery,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

# =========================
# CONFIG
# =========================
BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or "0")

AUTO_RESULTS_ENABLED = os.getenv("AUTO_RESULTS_ENABLED", "1") == "1"
AUTO_RESULTS_INTERVAL = int(os.getenv("AUTO_RESULTS_INTERVAL", "300") or "300")

DB_PATH = os.getenv("DB_PATH", "bot.db")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("bot")

bot = Bot(BOT_TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher()

# =========================
# DB
# =========================
def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def init_db() -> None:
    with db() as con:
        cur = con.cursor()
        cur.execute("""
        CREATE TABLE IF NOT EXISTS matches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT,
            start_time TEXT,
            status TEXT DEFAULT 'open',
            result TEXT
        )
        """)
        # --- migrations (safe) ---
        for stmt in [
            "ALTER TABLE matches ADD COLUMN sport TEXT",
            "ALTER TABLE matches ADD COLUMN league TEXT",
            "ALTER TABLE matches ADD COLUMN source TEXT",
            "ALTER TABLE matches ADD COLUMN external_id TEXT",
        ]:
            try:
                cur.execute(stmt)
            except sqlite3.OperationalError:
                pass

        cur.execute("""
        CREATE TABLE IF NOT EXISTS votes (
            user_id INTEGER,
            match_id INTEGER,
            pick TEXT,
            UNIQUE(user_id, match_id)
        )
        """)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS scores (
            user_id INTEGER PRIMARY KEY,
            points INTEGER DEFAULT 0
        )
        """)
        con.commit()

# =========================
# UI
# =========================
BTN_ACTIVE = "⚽ Активные матчи"
BTN_LB = "🏆 Лидерборд"
BTN_PROFILE = "👤 Профиль"
BTN_HELP = "ℹ️ Помощь"

SPORT_PRETTY = {
    "football": "⚽ Футбол",
    "hockey": "🏒 Хоккей",
    "esports": "🎮 Киберспорт",
    "other": "🏟 Другое",
}

def main_menu(match_ids: Optional[List[int]] = None) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text=BTN_ACTIVE)],
        [KeyboardButton(text=BTN_LB), KeyboardButton(text=BTN_PROFILE)],
        [KeyboardButton(text=BTN_HELP)],
    ]
    # Add match #id buttons (one per row) — so user can click, not type.
    if match_ids:
        for mid in match_ids[:40]:
            rows.append([KeyboardButton(text=f"#{mid}")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)

def match_actions_kb(match_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="1", callback_data=f"pick:{match_id}:1"),
            InlineKeyboardButton(text="X", callback_data=f"pick:{match_id}:X"),
            InlineKeyboardButton(text="2", callback_data=f"pick:{match_id}:2"),
        ]]
    )

def sport_categories_kb(sports: List[Tuple[str, int]]) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    for sport, cnt in sports:
        s = (sport or "other").lower()
        label = SPORT_PRETTY.get(s, f"🏟 {s}")
        rows.append([InlineKeyboardButton(text=f"{label} ({cnt})", callback_data=f"sport:{s}")])
    rows.append([InlineKeyboardButton(text="📋 Все матчи", callback_data="sport:all")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

# =========================
# QUERIES
# =========================
def get_open_sports() -> List[Tuple[str, int]]:
    with db() as con:
        cur = con.cursor()
        cur.execute("""
            SELECT COALESCE(NULLIF(LOWER(sport), ''), 'other') AS sport, COUNT(*) AS c
            FROM matches
            WHERE status='open'
            GROUP BY COALESCE(NULLIF(LOWER(sport), ''), 'other')
            ORDER BY c DESC
        """)
        return [(r["sport"], int(r["c"])) for r in cur.fetchall()]

def get_open_matches_by_sport(sport: str) -> List[sqlite3.Row]:
    with db() as con:
        cur = con.cursor()
        if sport == "all":
            cur.execute("SELECT id, title FROM matches WHERE status='open' ORDER BY id DESC")
        else:
            cur.execute(
                "SELECT id, title FROM matches WHERE status='open' AND COALESCE(NULLIF(LOWER(sport), ''), 'other')=? ORDER BY id DESC",
                (sport,),
            )
        return cur.fetchall()

def get_match(mid: int) -> Optional[sqlite3.Row]:
    with db() as con:
        cur = con.cursor()
        cur.execute("SELECT * FROM matches WHERE id=?", (mid,))
        return cur.fetchone()

# =========================
# HANDLERS
# =========================
@dp.message(Command("start"))
async def start(m: Message):
    await m.answer("Бот запущен ✅\nЖми кнопки снизу.", reply_markup=main_menu())

@dp.message(F.text == BTN_HELP)
async def help_btn(m: Message):
    await m.answer(
        "ℹ️ Помощь\n\n"
        "1) «⚽ Активные матчи» → выбери спорт → выбери матч (#id)\n"
        "2) Выбери исход 1 / X / 2\n\n"
        "Авто-итоги (если включены) начисляют +3 за верный исход.",
        reply_markup=main_menu(),
    )

@dp.message(F.text == BTN_ACTIVE)
async def active_matches(m: Message):
    sports = get_open_sports()
    if not sports:
        await m.answer("Активных матчей нет.", reply_markup=main_menu())
        return
    await m.answer("Выбери вид спорта 👇", reply_markup=main_menu())
    await m.answer("Категории:", reply_markup=sport_categories_kb(sports))

@dp.callback_query(F.data.startswith("sport:"))
async def sport_pick(cb: CallbackQuery):
    sport = cb.data.split(":", 1)[1].strip().lower()
    rows = get_open_matches_by_sport(sport)

    if not rows:
        await cb.answer("В этой категории матчей нет.", show_alert=True)
        return

    ids = [int(r["id"]) for r in rows][:40]
    lines = [f"#{r['id']} {r['title']}" for r in rows[:40]]

    header = "Активные матчи: ВСЕ" if sport == "all" else f"Активные матчи: {SPORT_PRETTY.get(sport, sport)}"
    text = header + ":\n\n" + "\n".join(lines)

    await cb.message.answer(text, reply_markup=main_menu(match_ids=ids))
    await cb.answer()

@dp.message(F.text.startswith("#"))
async def open_match(m: Message):
    try:
        match_id = int(m.text.strip().replace("#", ""))
    except ValueError:
        return

    match = get_match(match_id)
    if not match or match["status"] != "open":
        await m.answer("Матч не найден или уже закрыт.", reply_markup=main_menu())
        return

    await m.answer(f"Матч #{match_id}:\n{match['title']}\n\nВыбери исход 1X2:", reply_markup=match_actions_kb(match_id))

@dp.callback_query(F.data.startswith("pick:"))
async def pick(cb: CallbackQuery):
    _, match_id, pick = cb.data.split(":")
    match_id = int(match_id)

    match = get_match(match_id)
    if not match or match["status"] != "open":
        await cb.answer("Матч закрыт.", show_alert=True)
        return

    if pick not in ("1", "X", "2"):
        await cb.answer("Неверный выбор.", show_alert=True)
        return

    with db() as con:
        cur = con.cursor()
        cur.execute(
            "INSERT OR REPLACE INTO votes (user_id, match_id, pick) VALUES (?, ?, ?)",
            (cb.from_user.id, match_id, pick),
        )
        con.commit()

    await cb.answer("Принято ✅", show_alert=True)

# =========================
# AUTO RESULTS
# =========================
async def auto_results_loop():
    while True:
        try:
            await asyncio.sleep(max(30, AUTO_RESULTS_INTERVAL))
            with db() as con:
                cur = con.cursor()
                cur.execute("SELECT id, result FROM matches WHERE status='open' AND result IS NOT NULL")
                matches = cur.fetchall()

                for r in matches:
                    mid = int(r["id"])
                    res = (r["result"] or "").strip()
                    if res not in ("1", "X", "2"):
                        # allow old words, but ignore if unknown
                        continue

                    cur.execute("SELECT user_id, pick FROM votes WHERE match_id=?", (mid,))
                    votes = cur.fetchall()

                    for v in votes:
                        uid = int(v["user_id"])
                        pick = v["pick"]
                        if pick == res:
                            cur.execute("INSERT OR IGNORE INTO scores (user_id, points) VALUES (?,0)", (uid,))
                            cur.execute("UPDATE scores SET points = points + 3 WHERE user_id=?", (uid,))

                    cur.execute("UPDATE matches SET status='closed' WHERE id=?", (mid,))
                con.commit()
        except Exception as e:
            logger.exception("auto_results_loop error: %s", e)

# =========================
# MAIN
# =========================
async def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN not set")
    init_db()

    if AUTO_RESULTS_ENABLED:
        asyncio.create_task(auto_results_loop())

    # Polling auto-restart: prevents "dies after a few hours"
    while True:
        try:
            await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
            break
        except Exception as e:
            logger.exception("Polling crashed, restarting in 5 seconds: %s", e)
            await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(main())
