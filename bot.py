# -*- coding: utf-8 -*-
"""
Telegram prediction bot (football-data only) with:
- Render Web Service port binding (aiohttp health endpoint)
- /sync_today admin flow (football-data)
- Auto-close predictions before kickoff
- Auto-finish matches + scoring (1X2)
- Profile card (level, points, balance, accuracy, streak)
- Leaderboard
- Duels (stake points, accept/decline, auto resolve)
Stability:
- All SQLite and blocking work runs in threads via asyncio.to_thread
- Background loops are guarded (try/except) and started once at startup
"""

import os
import json
import math
import asyncio
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, date
from typing import Optional, List, Tuple, Dict

import aiohttp
from aiohttp import web

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from aiogram.enums import ParseMode
from zoneinfo import ZoneInfo

# ------------------------- Config -------------------------

BOT_TOKEN = os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing in environment")

ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or "0")
if ADMIN_ID <= 0:
    raise RuntimeError("ADMIN_ID is missing or invalid")

FOOTBALL_DATA_TOKEN = os.getenv("FOOTBALL_DATA_TOKEN") or os.getenv("FOOTBALL_DATA_API_TOKEN")
if not FOOTBALL_DATA_TOKEN:
    raise RuntimeError("FOOTBALL_DATA_TOKEN is missing in environment")

FD_COMPETITIONS = [x.strip() for x in (os.getenv("FD_COMPETITIONS", "")).split(",") if x.strip()]
PREDICTION_CLOSE_SECONDS = int(os.getenv("PREDICTION_CLOSE_SECONDS", "120"))
DB_PATH = os.getenv("DB_PATH", "bot.db")

AUTO_SYNC_ENABLED = os.getenv("AUTO_SYNC_ENABLED", "0").strip() in ("1", "true", "True", "yes", "YES")
AUTO_SYNC_HOUR_LOCAL = int(os.getenv("AUTO_SYNC_HOUR_LOCAL", "4"))
AUTO_SYNC_TZ = os.getenv("AUTO_SYNC_TZ", "Europe/London")  # IANA tz

HTTP_PORT = int(os.getenv("PORT", "10000"))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("bot")

# ------------------------- Helpers -------------------------

def utcnow() -> datetime:
    return datetime.now(timezone.utc)

def iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()

def parse_utc(iso: str) -> datetime:
    # football-data uses ISO with Z
    if iso.endswith("Z"):
        iso = iso.replace("Z", "+00:00")
    return datetime.fromisoformat(iso).astimezone(timezone.utc)

def current_season() -> str:
    # Season key: current year in UTC
    return str(utcnow().year)

def level_from_rating(r: int) -> Tuple[str, int, Optional[int]]:
    """
    Returns (level_name, current_floor, next_floor or None)
    Rating here is season_points (can be replaced later by separate rating).
    """
    tiers = [
        ("Bronze", 0, 500),
        ("Silver", 500, 1200),
        ("Gold", 1200, 2000),
        ("Elite", 2000, None),
    ]
    for name, floor, nxt in tiers:
        if nxt is None and r >= floor:
            return (name, floor, None)
        if nxt is not None and floor <= r < nxt:
            return (name, floor, nxt)
    return ("Bronze", 0, 500)

def progress_bar(current: int, floor: int, nxt: Optional[int], width: int = 10) -> str:
    if nxt is None:
        return "█" * width
    span = max(1, nxt - floor)
    filled = int(round(((current - floor) / span) * width))
    filled = max(0, min(width, filled))
    return "█" * filled + "░" * (width - filled)

def fmt_int(n: int) -> str:
    return f"{n:,}".replace(",", " ")

def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID

# ------------------------- DB (threaded) -------------------------

def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con

def _init_db_sync():
    con = _connect()
    cur = con.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        created_at TEXT,
        season TEXT,
        season_points INTEGER DEFAULT 0,
        total_preds INTEGER DEFAULT 0,
        correct_preds INTEGER DEFAULT 0,
        streak INTEGER DEFAULT 0,
        balance INTEGER DEFAULT 1000
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS matches (
        match_id INTEGER PRIMARY KEY AUTOINCREMENT,
        ext_id TEXT UNIQUE,
        competition TEXT,
        home TEXT,
        away TEXT,
        kickoff_utc TEXT,
        status TEXT, -- open|closed|finished
        result TEXT, -- 1|X|2
        home_score INTEGER,
        away_score INTEGER,
        created_at TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS predictions (
        pred_id INTEGER PRIMARY KEY AUTOINCREMENT,
        match_id INTEGER,
        user_id INTEGER,
        outcome TEXT, -- 1|X|2
        created_at TEXT,
        UNIQUE(match_id, user_id),
        FOREIGN KEY(match_id) REFERENCES matches(match_id),
        FOREIGN KEY(user_id) REFERENCES users(user_id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS duels (
        duel_id INTEGER PRIMARY KEY AUTOINCREMENT,
        match_id INTEGER,
        challenger_id INTEGER,
        opponent_id INTEGER,
        challenger_outcome TEXT,
        opponent_outcome TEXT,
        stake INTEGER,
        status TEXT, -- pending|active|finished|declined
        created_at TEXT,
        finished_at TEXT,
        winner_id INTEGER,
        FOREIGN KEY(match_id) REFERENCES matches(match_id)
    )
    """)

    con.commit()
    con.close()

async def adb(func, *args, timeout: int = 15):
    return await asyncio.wait_for(asyncio.to_thread(func, *args), timeout=timeout)

def _upsert_user_sync(user_id: int, username: str, first_name: str):
    con = _connect()
    cur = con.cursor()
    season = current_season()
    now = iso_utc(utcnow())
    cur.execute("SELECT season FROM users WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    if row is None:
        cur.execute(
            "INSERT INTO users (user_id, username, first_name, created_at, season, season_points, total_preds, correct_preds, streak, balance) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (user_id, username, first_name, now, season, 0, 0, 0, 0, 1000),
        )
    else:
        # rollover season if needed
        if row["season"] != season:
            cur.execute(
                "UPDATE users SET season=?, season_points=0, total_preds=0, correct_preds=0, streak=0 WHERE user_id=?",
                (season, user_id),
            )
        cur.execute(
            "UPDATE users SET username=?, first_name=? WHERE user_id=?",
            (username, first_name, user_id),
        )
    con.commit()
    con.close()

def _get_user_sync(user_id: int) -> sqlite3.Row:
    con = _connect()
    cur = con.cursor()
    cur.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    con.close()
    return row

def _top_users_sync(limit: int = 20) -> List[sqlite3.Row]:
    con = _connect()
    cur = con.cursor()
    season = current_season()
    cur.execute(
        "SELECT user_id, username, first_name, season_points, total_preds, correct_preds FROM users WHERE season=? "
        "ORDER BY season_points DESC, correct_preds DESC, total_preds DESC LIMIT ?",
        (season, limit),
    )
    rows = cur.fetchall()
    con.close()
    return rows

def _user_rank_sync(user_id: int) -> Optional[int]:
    con = _connect()
    cur = con.cursor()
    season = current_season()
    cur.execute(
        "SELECT user_id FROM users WHERE season=? ORDER BY season_points DESC, correct_preds DESC, total_preds DESC",
        (season,),
    )
    ids = [r["user_id"] for r in cur.fetchall()]
    con.close()
    if user_id in ids:
        return ids.index(user_id) + 1
    return None

def _list_open_matches_sync() -> List[sqlite3.Row]:
    con = _connect()
    cur = con.cursor()
    cur.execute("SELECT * FROM matches WHERE status='open' ORDER BY kickoff_utc ASC")
    rows = cur.fetchall()
    con.close()
    return rows

def _list_active_matches_sync() -> List[sqlite3.Row]:
    con = _connect()
    cur = con.cursor()
    cur.execute("SELECT * FROM matches WHERE status IN ('open','closed') ORDER BY kickoff_utc ASC")
    rows = cur.fetchall()
    con.close()
    return rows

def _get_match_sync(match_id: int) -> Optional[sqlite3.Row]:
    con = _connect()
    cur = con.cursor()
    cur.execute("SELECT * FROM matches WHERE match_id=?", (match_id,))
    row = cur.fetchone()
    con.close()
    return row

def _insert_match_sync(ext_id: str, competition: str, home: str, away: str, kickoff_utc: str):
    con = _connect()
    cur = con.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO matches (ext_id, competition, home, away, kickoff_utc, status, created_at) VALUES (?,?,?,?,?,?,?)",
        (ext_id, competition, home, away, kickoff_utc, "open", iso_utc(utcnow())),
    )
    con.commit()
    con.close()

def _set_match_status_sync(match_id: int, status: str):
    con = _connect()
    cur = con.cursor()
    cur.execute("UPDATE matches SET status=? WHERE match_id=?", (status, match_id))
    con.commit()
    con.close()

def _set_match_result_sync(match_id: int, result: str, hs: int, as_: int):
    con = _connect()
    cur = con.cursor()
    cur.execute(
        "UPDATE matches SET status='finished', result=?, home_score=?, away_score=? WHERE match_id=?",
        (result, hs, as_, match_id),
    )
    con.commit()
    con.close()

def _upsert_prediction_sync(match_id: int, user_id: int, outcome: str) -> str:
    con = _connect()
    cur = con.cursor()
    now = iso_utc(utcnow())
    cur.execute("SELECT outcome FROM predictions WHERE match_id=? AND user_id=?", (match_id, user_id))
    row = cur.fetchone()
    if row is None:
        cur.execute(
            "INSERT INTO predictions (match_id, user_id, outcome, created_at) VALUES (?,?,?,?)",
            (match_id, user_id, outcome, now),
        )
        action = "created"
    else:
        cur.execute(
            "UPDATE predictions SET outcome=?, created_at=? WHERE match_id=? AND user_id=?",
            (outcome, now, match_id, user_id),
        )
        action = "updated"
    con.commit()
    con.close()
    return action

def _user_predictions_sync(user_id: int, limit: int = 20) -> List[sqlite3.Row]:
    con = _connect()
    cur = con.cursor()
    cur.execute("""
        SELECT p.*, m.home, m.away, m.kickoff_utc, m.status, m.result, m.home_score, m.away_score
        FROM predictions p
        JOIN matches m ON m.match_id = p.match_id
        WHERE p.user_id=?
        ORDER BY m.kickoff_utc DESC
        LIMIT ?
    """, (user_id, limit))
    rows = cur.fetchall()
    con.close()
    return rows

def _increment_user_stats_sync(user_id: int, correct: bool):
    con = _connect()
    cur = con.cursor()
    # always increment total_preds
    if correct:
        cur.execute(
            "UPDATE users SET season_points=season_points+1, total_preds=total_preds+1, correct_preds=correct_preds+1, streak=streak+1 WHERE user_id=?",
            (user_id,),
        )
    else:
        cur.execute(
            "UPDATE users SET total_preds=total_preds+1, streak=0 WHERE user_id=?",
            (user_id,),
        )
    con.commit()
    con.close()

def _get_predictions_for_match_sync(match_id: int) -> List[sqlite3.Row]:
    con = _connect()
    cur = con.cursor()
    cur.execute("SELECT * FROM predictions WHERE match_id=?", (match_id,))
    rows = cur.fetchall()
    con.close()
    return rows

def _get_vote_stats_sync(match_id: int) -> Dict[str, int]:
    con = _connect()
    cur = con.cursor()
    cur.execute("SELECT outcome, COUNT(*) as c FROM predictions WHERE match_id=? GROUP BY outcome", (match_id,))
    stats = {"1": 0, "X": 0, "2": 0}
    for r in cur.fetchall():
        stats[r["outcome"]] = int(r["c"])
    con.close()
    return stats

def _find_user_by_username_sync(username: str) -> Optional[int]:
    u = username.lstrip("@").strip()
    con = _connect()
    cur = con.cursor()
    cur.execute("SELECT user_id FROM users WHERE LOWER(username)=LOWER(?)", (u,))
    row = cur.fetchone()
    con.close()
    return int(row["user_id"]) if row else None

def _get_balance_sync(user_id: int) -> int:
    con = _connect()
    cur = con.cursor()
    cur.execute("SELECT balance FROM users WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    con.close()
    return int(row["balance"]) if row else 0

def _add_balance_sync(user_id: int, delta: int):
    con = _connect()
    cur = con.cursor()
    cur.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (delta, user_id))
    con.commit()
    con.close()

def _create_duel_sync(match_id: int, challenger_id: int, opponent_id: int, stake: int, challenger_outcome: str) -> int:
    con = _connect()
    cur = con.cursor()
    now = iso_utc(utcnow())
    cur.execute(
        "INSERT INTO duels (match_id, challenger_id, opponent_id, challenger_outcome, stake, status, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (match_id, challenger_id, opponent_id, challenger_outcome, stake, "pending", now),
    )
    duel_id = cur.lastrowid
    con.commit()
    con.close()
    return int(duel_id)

def _get_duel_sync(duel_id: int) -> Optional[sqlite3.Row]:
    con = _connect()
    cur = con.cursor()
    cur.execute("SELECT * FROM duels WHERE duel_id=?", (duel_id,))
    row = cur.fetchone()
    con.close()
    return row

def _set_duel_status_sync(duel_id: int, status: str):
    con = _connect()
    cur = con.cursor()
    cur.execute("UPDATE duels SET status=? WHERE duel_id=?", (status, duel_id))
    con.commit()
    con.close()

def _accept_duel_sync(duel_id: int, opponent_outcome: str):
    con = _connect()
    cur = con.cursor()
    cur.execute("UPDATE duels SET opponent_outcome=?, status='active' WHERE duel_id=?", (opponent_outcome, duel_id))
    con.commit()
    con.close()

def _finish_duel_sync(duel_id: int, winner_id: Optional[int]):
    con = _connect()
    cur = con.cursor()
    cur.execute(
        "UPDATE duels SET status='finished', finished_at=?, winner_id=? WHERE duel_id=?",
        (iso_utc(utcnow()), winner_id, duel_id),
    )
    con.commit()
    con.close()

def _list_unresolved_duels_for_match_sync(match_id: int) -> List[sqlite3.Row]:
    con = _connect()
    cur = con.cursor()
    cur.execute("SELECT * FROM duels WHERE match_id=? AND status IN ('pending','active')", (match_id,))
    rows = cur.fetchall()
    con.close()
    return rows

# ------------------------- Football-data API -------------------------

FD_BASE = "https://api.football-data.org/v4"

@dataclass
class FDMatch:
    ext_id: str
    competition: str
    home: str
    away: str
    kickoff_utc: datetime

async def fd_get_json(session: aiohttp.ClientSession, url: str) -> dict:
    headers = {"X-Auth-Token": FOOTBALL_DATA_TOKEN}
    async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
        resp.raise_for_status()
        return await resp.json()

async def fd_today_matches() -> List[FDMatch]:
    # football-data supports /matches?dateFrom=YYYY-MM-DD&dateTo=YYYY-MM-DD
    d = date.today().isoformat()
    url = f"{FD_BASE}/matches?dateFrom={d}&dateTo={d}"
    async with aiohttp.ClientSession() as session:
        data = await fd_get_json(session, url)
    out: List[FDMatch] = []
    for m in data.get("matches", []):
        comp = (m.get("competition") or {}).get("code") or (m.get("competition") or {}).get("name") or "UNK"
        if FD_COMPETITIONS and comp not in FD_COMPETITIONS:
            continue
        ext_id = str(m.get("id"))
        utc_date = m.get("utcDate")
        if not utc_date:
            continue
        kickoff = parse_utc(utc_date)
        home = (m.get("homeTeam") or {}).get("shortName") or (m.get("homeTeam") or {}).get("name") or "Home"
        away = (m.get("awayTeam") or {}).get("shortName") or (m.get("awayTeam") or {}).get("name") or "Away"
        out.append(FDMatch(ext_id=ext_id, competition=comp, home=home, away=away, kickoff_utc=kickoff))
    out.sort(key=lambda x: x.kickoff_utc)
    return out

async def fd_match_status(ext_id: str) -> Optional[dict]:
    url = f"{FD_BASE}/matches/{ext_id}"
    async with aiohttp.ClientSession() as session:
        return await fd_get_json(session, url)

def outcome_from_score(hs: int, as_: int) -> str:
    if hs > as_:
        return "1"
    if hs < as_:
        return "2"
    return "X"

# ------------------------- UI -------------------------

main_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="⚽ Активные матчи"), KeyboardButton(text="👤 Профиль")],
        [KeyboardButton(text="🏆 Лидерборд"), KeyboardButton(text="🧾 Мои прогнозы")],
        [KeyboardButton(text="⚔️ Дуэли"), KeyboardButton(text="❓ Помощь")],
    ],
    resize_keyboard=True,
)

def match_card(mrow: sqlite3.Row) -> str:
    kickoff = parse_utc(mrow["kickoff_utc"]).astimezone(ZoneInfo(AUTO_SYNC_TZ))
    status = mrow["status"]
    line = f"{mrow['home']} — {mrow['away']}\n🕒 {kickoff.strftime('%d %b %H:%M')} ({AUTO_SYNC_TZ})\n🏟 {mrow['competition']}"
    if status == "open":
        return "🟢 " + line
    if status == "closed":
        return "🟠 " + line + "\n🔒 Прогнозы закрыты"
    return "⚫ " + line

def kb_match_actions(match_id: int, allow_predict: bool) -> InlineKeyboardMarkup:
    buttons = []
    if allow_predict:
        buttons.append([InlineKeyboardButton(text="✅ Прогноз", callback_data=f"pred:{match_id}")])
    buttons.append([InlineKeyboardButton(text="📊 Голоса", callback_data=f"votes:{match_id}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def kb_pred_choices(match_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🏠 1", callback_data=f"pick:{match_id}:1"),
            InlineKeyboardButton(text="🤝 X", callback_data=f"pick:{match_id}:X"),
            InlineKeyboardButton(text="🚩 2", callback_data=f"pick:{match_id}:2"),
        ],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"match:{match_id}")],
    ])

def kb_admin_sync(match: FDMatch) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Добавить", callback_data=f"admin_add:{match.ext_id}"),
            InlineKeyboardButton(text="❌ Пропустить", callback_data=f"admin_skip:{match.ext_id}"),
        ]
    ])

def kb_duel_start() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Создать дуэль", callback_data="duel:new")],
    ])

def kb_duel_pick_match(match_id: int, opponent_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Выбрать этот матч", callback_data=f"duel:match:{match_id}:{opponent_id}")],
    ])

def kb_duel_stakes(match_id: int, opponent_id: int, outcome: str) -> InlineKeyboardMarkup:
    opts = [50, 100, 200, 500]
    rows = []
    for s in opts:
        rows.append([InlineKeyboardButton(text=f"{s} 🪙", callback_data=f"duel:stake:{match_id}:{opponent_id}:{outcome}:{s}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="duel:new")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_duel_accept(duel_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Принять", callback_data=f"duel:accept:{duel_id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"duel:decline:{duel_id}"),
        ]
    ])

def kb_duel_pick_outcome(duel_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🏠 1", callback_data=f"duel:pick:{duel_id}:1"),
            InlineKeyboardButton(text="🤝 X", callback_data=f"duel:pick:{duel_id}:X"),
            InlineKeyboardButton(text="🚩 2", callback_data=f"duel:pick:{duel_id}:2"),
        ]
    ])

# ------------------------- Bot setup -------------------------

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# In-memory cache for pending sync cards mapping ext_id -> FDMatch
SYNC_CACHE: Dict[str, FDMatch] = {}

# ------------------------- Commands -------------------------

@dp.message(CommandStart())
async def cmd_start(message: Message):
    u = message.from_user
    await adb(_upsert_user_sync, u.id, u.username or "", u.first_name or "")
    await message.answer("Привет! Это бот прогнозов ⚽\nВыбирай в меню:", reply_markup=main_kb)

@dp.message(Command("sync_today"))
async def cmd_sync_today(message: Message):
    if not is_admin(message.from_user.id):
        return await message.answer("⛔ Только админ.")
    await message.answer("🔄 Тяну матчи на сегодня из football-data…")
    try:
        matches = await fd_today_matches()
    except Exception as e:
        logger.exception("sync_today failed")
        return await message.answer(f"Ошибка при получении матчей: {e}")

    now = utcnow()
    shown = 0
    for m in matches:
        # skip already started or too soon (within close window)
        if m.kickoff_utc <= now + timedelta(seconds=PREDICTION_CLOSE_SECONDS):
            continue
        SYNC_CACHE[m.ext_id] = m
        text = (f"📌 <b>{m.home} — {m.away}</b>\n"
                f"🏟 {m.competition}\n"
                f"🕒 {m.kickoff_utc.astimezone(ZoneInfo(AUTO_SYNC_TZ)).strftime('%d %b %H:%M')} ({AUTO_SYNC_TZ})\n"
                f"ID: <code>{m.ext_id}</code>")
        await message.answer(text, reply_markup=kb_admin_sync(m))
        shown += 1

    if shown == 0:
        await message.answer("На сегодня нет подходящих матчей (либо уже начались/слишком скоро).")

@dp.message(Command("help"))
@dp.message(F.text == "❓ Помощь")
async def help_msg(message: Message):
    await message.answer(
        "Как играть:\n"
        "• ⚽ <b>Активные матчи</b> → выбери матч → сделай прогноз (1/X/2)\n"
        f"• Прогнозы закрываются за <b>{PREDICTION_CLOSE_SECONDS}</b> сек до матча\n"
        "• После завершения матча бот сам подводит итоги и начисляет очки\n\n"
        "⚔️ Дуэли:\n"
        "• Создай дуэль, выбери соперника, матч и ставку 🪙\n"
        "• Победитель забирает банк, при ничьей — возврат\n"
    )

# ------------------------- Menu buttons -------------------------

@dp.message(F.text == "⚽ Активные матчи")
async def active_matches(message: Message):
    rows = await adb(_list_active_matches_sync)
    if not rows:
        return await message.answer("Сегодня активных матчей нет.", reply_markup=main_kb)
    for r in rows:
        m = await adb(_get_match_sync, int(r["match_id"]))
        if not m:
            continue
        allow = (m["status"] == "open")
        await message.answer(match_card(m), reply_markup=kb_match_actions(int(m["match_id"]), allow))

@dp.message(F.text == "🏆 Лидерборд")
async def leaderboard(message: Message):
    rows = await adb(_top_users_sync, 20)
    if not rows:
        return await message.answer("Пока нет данных по лидерборду.")
    lines = [f"🏆 <b>Лидерборд сезона {current_season()}</b>\n"]
    for i, r in enumerate(rows, 1):
        name = r["username"] or r["first_name"] or str(r["user_id"])
        pts = int(r["season_points"])
        lines.append(f"{i}. <b>{name}</b> — {pts} очк.")
    await message.answer("\n".join(lines))

@dp.message(F.text == "👤 Профиль")
async def profile(message: Message):
    u = message.from_user
    await adb(_upsert_user_sync, u.id, u.username or "", u.first_name or "")
    row = await adb(_get_user_sync, u.id)
    if not row:
        return await message.answer("Профиль не найден.")
    pts = int(row["season_points"])
    total = int(row["total_preds"])
    correct = int(row["correct_preds"])
    streak = int(row["streak"])
    balance = int(row["balance"])
    acc = (correct / total * 100.0) if total > 0 else 0.0
    lvl, floor, nxt = level_from_rating(pts)
    bar = progress_bar(pts, floor, nxt, 10)
    rank = await adb(_user_rank_sync, u.id)
    next_txt = "Максимум" if nxt is None else f"{max(0, nxt-pts)} очк."
    text = (
        f"👤 <b>{u.first_name}</b>\n"
        f"━━━━━━━━━━━━━━\n"
        f"🏅 Уровень: <b>{lvl}</b>\n"
        f"📊 Рейтинг: <b>{pts}</b>\n"
        f"💰 Баланс: <b>{fmt_int(balance)}</b> 🪙\n\n"
        f"⚽ Прогнозов: <b>{total}</b>\n"
        f"✅ Точность: <b>{acc:.0f}%</b>\n"
        f"🔥 Серия: <b>{streak}</b>\n\n"
        f"🏆 Очки сезона: <b>{pts}</b>\n"
        f"🥇 Место в сезоне: <b>#{rank or '—'}</b>\n"
        f"━━━━━━━━━━━━━━\n"
        f"{bar}\n"
        f"⬆️ До следующего уровня: <b>{next_txt}</b>"
    )
    await message.answer(text)

@dp.message(F.text == "🧾 Мои прогнозы")
async def my_preds(message: Message):
    rows = await adb(_user_predictions_sync, message.from_user.id, 20)
    if not rows:
        return await message.answer("У тебя пока нет прогнозов.")
    lines = ["🧾 <b>Твои прогнозы (последние 20)</b>\n"]
    for r in rows:
        kickoff = parse_utc(r["kickoff_utc"]).astimezone(ZoneInfo(AUTO_SYNC_TZ)).strftime('%d %b %H:%M')
        out = r["outcome"]
        status = r["status"]
        res = r["result"]
        tag = "🟢" if status == "open" else ("🟠" if status == "closed" else "⚫")
        if status == "finished" and res:
            ok = "✅" if out == res else "❌"
            lines.append(f"{tag} {r['home']}—{r['away']} ({kickoff}) | ты: <b>{out}</b> | итог: <b>{res}</b> {ok}")
        else:
            lines.append(f"{tag} {r['home']}—{r['away']} ({kickoff}) | ты: <b>{out}</b>")
    await message.answer("\n".join(lines))

@dp.message(F.text == "⚔️ Дуэли")
async def duels_menu(message: Message):
    await message.answer(
        "⚔️ <b>Дуэли</b>\n"
        "• Создай дуэль с другом и поставьте 🪙 на матч.\n"
        "• Победитель забирает банк.\n",
        reply_markup=kb_duel_start()
    )

# ------------------------- Callbacks: matches/predictions -------------------------

@dp.callback_query(F.data.startswith("match:"))
async def cb_match(query: CallbackQuery):
    match_id = int(query.data.split(":")[1])
    m = await adb(_get_match_sync, match_id)
    if not m:
        return await query.answer("Матч не найден", show_alert=True)
    allow = (m["status"] == "open")
    await query.message.edit_text(match_card(m), reply_markup=kb_match_actions(match_id, allow))
    await query.answer()

@dp.callback_query(F.data.startswith("pred:"))
async def cb_pred(query: CallbackQuery):
    match_id = int(query.data.split(":")[1])
    m = await adb(_get_match_sync, match_id)
    if not m:
        return await query.answer("Матч не найден", show_alert=True)
    if m["status"] != "open":
        return await query.answer("Прогнозы закрыты", show_alert=True)
    await query.message.edit_text(
        f"Выбери исход для матча:\n<b>{m['home']} — {m['away']}</b>",
        reply_markup=kb_pred_choices(match_id),
    )
    await query.answer()

@dp.callback_query(F.data.startswith("pick:"))
async def cb_pick(query: CallbackQuery):
    _, mid, outcome = query.data.split(":")
    match_id = int(mid)
    u = query.from_user
    await adb(_upsert_user_sync, u.id, u.username or "", u.first_name or "")
    m = await adb(_get_match_sync, match_id)
    if not m:
        return await query.answer("Матч не найден", show_alert=True)
    if m["status"] != "open":
        return await query.answer("Прогнозы закрыты", show_alert=True)

    action = await adb(_upsert_prediction_sync, match_id, u.id, outcome)
    txt = "✅ Прогноз принят" if action == "created" else "✅ Прогноз обновлён"
    await query.answer(txt, show_alert=True)
    await query.message.edit_text(match_card(m), reply_markup=kb_match_actions(match_id, True))

@dp.callback_query(F.data.startswith("votes:"))
async def cb_votes(query: CallbackQuery):
    match_id = int(query.data.split(":")[1])
    m = await adb(_get_match_sync, match_id)
    if not m:
        return await query.answer("Матч не найден", show_alert=True)
    stats = await adb(_get_vote_stats_sync, match_id)
    total = stats["1"] + stats["X"] + stats["2"]
    text = (
        f"📊 <b>Голоса</b>\n"
        f"{m['home']} — {m['away']}\n\n"
        f"🏠 1: <b>{stats['1']}</b>\n"
        f"🤝 X: <b>{stats['X']}</b>\n"
        f"🚩 2: <b>{stats['2']}</b>\n\n"
        f"Всего: <b>{total}</b>"
    )
    await query.answer()
    await query.message.answer(text)

# ------------------------- Callbacks: admin sync -------------------------

@dp.callback_query(F.data.startswith("admin_add:"))
async def cb_admin_add(query: CallbackQuery):
    if not is_admin(query.from_user.id):
        return await query.answer("⛔", show_alert=True)
    ext_id = query.data.split(":")[1]
    m = SYNC_CACHE.get(ext_id)
    if not m:
        return await query.answer("Кэш пуст", show_alert=True)
    await adb(_insert_match_sync, m.ext_id, m.competition, m.home, m.away, iso_utc(m.kickoff_utc))
    await query.answer("✅ Добавлено", show_alert=True)
    await query.message.edit_reply_markup(reply_markup=None)

@dp.callback_query(F.data.startswith("admin_skip:"))
async def cb_admin_skip(query: CallbackQuery):
    if not is_admin(query.from_user.id):
        return await query.answer("⛔", show_alert=True)
    await query.answer("Ок")
    await query.message.edit_reply_markup(reply_markup=None)

# ------------------------- Duels flow -------------------------

# Duel creation is a simple flow:
# 1) user taps "Создать дуэль" -> ask @username
# 2) user sends @username -> show open matches list (choose)
# 3) choose outcome + stake -> create pending duel, reserve challenger stake
# 4) bot sends opponent DM to accept/decline
# 5) opponent accepts -> choose outcome -> reserve opponent stake -> duel active
# 6) on match finish -> resolve duel and pay out/refund

PENDING_OPPONENT: Dict[int, int] = {}  # creator_id -> opponent_id

@dp.callback_query(F.data == "duel:new")
async def cb_duel_new(query: CallbackQuery):
    await query.answer()
    await query.message.answer("Напиши @username соперника для дуэли:")

@dp.message(F.text.regexp(r"^@[\w\d_]{3,}$"))
async def duel_username_input(message: Message):
    if message.text is None:
        return
    creator = message.from_user.id
    opp_id = await adb(_find_user_by_username_sync, message.text)
    if not opp_id:
        return await message.answer("Не нашёл такого игрока. Пусть он сначала нажмёт /start.")
    if opp_id == creator:
        return await message.answer("Нельзя вызвать самого себя 🙂")
    PENDING_OPPONENT[creator] = opp_id
    matches = await adb(_list_open_matches_sync)
    if not matches:
        return await message.answer("Нет открытых матчей для дуэли.")
    await message.answer("Выбери матч для дуэли:")
    for m in matches[:12]:
        await message.answer(match_card(m), reply_markup=kb_duel_pick_match(int(m["match_id"]), opp_id))

@dp.callback_query(F.data.startswith("duel:match:"))
async def cb_duel_match(query: CallbackQuery):
    _, _, match_id, opp_id = query.data.split(":")
    match_id = int(match_id); opp_id = int(opp_id)
    m = await adb(_get_match_sync, match_id)
    if not m or m["status"] != "open":
        return await query.answer("Матч недоступен", show_alert=True)
    await query.answer()
    await query.message.answer(
        f"Выбери свой исход для дуэли:\n<b>{m['home']} — {m['away']}</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="🏠 1", callback_data=f"duel:out:{match_id}:{opp_id}:1"),
                InlineKeyboardButton(text="🤝 X", callback_data=f"duel:out:{match_id}:{opp_id}:X"),
                InlineKeyboardButton(text="🚩 2", callback_data=f"duel:out:{match_id}:{opp_id}:2"),
            ]
        ])
    )

@dp.callback_query(F.data.startswith("duel:out:"))
async def cb_duel_outcome(query: CallbackQuery):
    _, _, match_id, opp_id, out = query.data.split(":")
    match_id = int(match_id); opp_id = int(opp_id)
    await query.answer()
    await query.message.answer(
        f"Окей. Теперь выбери ставку 🪙 (будет списано сразу после создания дуэли):",
        reply_markup=kb_duel_stakes(match_id, opp_id, out)
    )

@dp.callback_query(F.data.startswith("duel:stake:"))
async def cb_duel_stake(query: CallbackQuery):
    _, _, match_id, opp_id, out, stake = query.data.split(":")
    match_id = int(match_id); opp_id = int(opp_id); stake = int(stake)
    u = query.from_user
    await adb(_upsert_user_sync, u.id, u.username or "", u.first_name or "")

    m = await adb(_get_match_sync, match_id)
    if not m or m["status"] != "open":
        return await query.answer("Матч недоступен", show_alert=True)

    bal = await adb(_get_balance_sync, u.id)
    if stake <= 0 or stake > bal:
        return await query.answer("Недостаточно баланса", show_alert=True)

    # Reserve challenger stake
    await adb(_add_balance_sync, u.id, -stake)
    duel_id = await adb(_create_duel_sync, match_id, u.id, opp_id, stake, out)

    await query.answer("✅ Дуэль создана", show_alert=True)
    await query.message.answer(
        f"⚔️ Дуэль создана!\n"
        f"Матч: <b>{m['home']} — {m['away']}</b>\n"
        f"Твоя ставка: <b>{stake} 🪙</b>\n"
        f"Твой исход: <b>{out}</b>\n"
        f"Ждём ответа соперника…"
    )

    # Notify opponent
    try:
        await bot.send_message(
            opp_id,
            f"⚔️ Тебе бросили вызов!\n"
            f"Матч: <b>{m['home']} — {m['away']}</b>\n"
            f"Ставка: <b>{stake} 🪙</b>\n"
            f"Принять дуэль?",
            reply_markup=kb_duel_accept(duel_id),
        )
    except Exception:
        # If can't message opponent, decline and refund
        await adb(_set_duel_status_sync, duel_id, "declined")
        await adb(_add_balance_sync, u.id, stake)
        return

@dp.callback_query(F.data.startswith("duel:decline:"))
async def cb_duel_decline(query: CallbackQuery):
    duel_id = int(query.data.split(":")[2])
    duel = await adb(_get_duel_sync, duel_id)
    if not duel:
        return await query.answer("Дуэль не найдена", show_alert=True)
    if query.from_user.id != int(duel["opponent_id"]):
        return await query.answer("Не тебе", show_alert=True)
    if duel["status"] != "pending":
        return await query.answer("Уже обработано", show_alert=True)

    await adb(_set_duel_status_sync, duel_id, "declined")
    # refund challenger
    await adb(_add_balance_sync, int(duel["challenger_id"]), int(duel["stake"]))
    await query.answer("Отклонено", show_alert=True)
    await query.message.edit_text("Ты отклонил дуэль ❌")

    try:
        await bot.send_message(int(duel["challenger_id"]), "❌ Дуэль отклонена. Ставка возвращена.")
    except Exception:
        pass

@dp.callback_query(F.data.startswith("duel:accept:"))
async def cb_duel_accept(query: CallbackQuery):
    duel_id = int(query.data.split(":")[2])
    duel = await adb(_get_duel_sync, duel_id)
    if not duel:
        return await query.answer("Дуэль не найдена", show_alert=True)
    if query.from_user.id != int(duel["opponent_id"]):
        return await query.answer("Не тебе", show_alert=True)
    if duel["status"] != "pending":
        return await query.answer("Уже обработано", show_alert=True)

    stake = int(duel["stake"])
    bal = await adb(_get_balance_sync, query.from_user.id)
    if stake > bal:
        # decline + refund challenger
        await adb(_set_duel_status_sync, duel_id, "declined")
        await adb(_add_balance_sync, int(duel["challenger_id"]), stake)
        await query.answer("Недостаточно баланса — дуэль отклонена", show_alert=True)
        await query.message.edit_text("Недостаточно баланса. Дуэль отклонена ❌")
        try:
            await bot.send_message(int(duel["challenger_id"]), "❌ Соперник не смог принять дуэль (не хватило баланса). Ставка возвращена.")
        except Exception:
            pass
        return

    await query.answer()
    await query.message.edit_text("Принято ✅ Выбери исход:")
    await query.message.edit_reply_markup(reply_markup=kb_duel_pick_outcome(duel_id))

@dp.callback_query(F.data.startswith("duel:pick:"))
async def cb_duel_pick(query: CallbackQuery):
    duel_id = int(query.data.split(":")[2])
    out = query.data.split(":")[3]
    duel = await adb(_get_duel_sync, duel_id)
    if not duel:
        return await query.answer("Дуэль не найдена", show_alert=True)
    if query.from_user.id != int(duel["opponent_id"]):
        return await query.answer("Не тебе", show_alert=True)
    if duel["status"] != "pending":
        return await query.answer("Уже обработано", show_alert=True)

    stake = int(duel["stake"])
    # reserve opponent stake
    await adb(_add_balance_sync, query.from_user.id, -stake)
    await adb(_accept_duel_sync, duel_id, out)

    m = await adb(_get_match_sync, int(duel["match_id"]))
    await query.answer("✅ Дуэль активна", show_alert=True)
    await query.message.edit_text(
        f"⚔️ Дуэль активна!\n"
        f"Матч: <b>{m['home']} — {m['away']}</b>\n"
        f"Твоя ставка: <b>{stake} 🪙</b>\n"
        f"Твой исход: <b>{out}</b>"
    )
    try:
        await bot.send_message(
            int(duel["challenger_id"]),
            f"✅ Соперник принял дуэль!\n"
            f"Матч: <b>{m['home']} — {m['away']}</b>\n"
            f"Ставка: <b>{stake} 🪙</b>\n"
            f"Соперник выбрал исход."
        )
    except Exception:
        pass

# ------------------------- Background loops -------------------------

async def loop_heartbeat():
    while True:
        logger.info("HEARTBEAT: bot alive")
        await asyncio.sleep(300)

async def loop_auto_close():
    while True:
        try:
            rows = await adb(_list_open_matches_sync)
            now = utcnow()
            for m in rows:
                kickoff = parse_utc(m["kickoff_utc"])
                if now >= kickoff - timedelta(seconds=PREDICTION_CLOSE_SECONDS):
                    await adb(_set_match_status_sync, int(m["match_id"]), "closed")
        except Exception:
            logger.exception("auto_close loop error")
        await asyncio.sleep(30)

async def loop_auto_finish():
    while True:
        try:
            rows = await adb(_list_active_matches_sync)
            for m in rows:
                if not m["ext_id"] or m["status"] == "finished":
                    continue
                # only check if kickoff passed (or soon)
                kickoff = parse_utc(m["kickoff_utc"])
                if utcnow() < kickoff - timedelta(minutes=5):
                    continue
                try:
                    data = await fd_match_status(str(m["ext_id"]))
                except Exception:
                    continue
                status = (data.get("status") or "").upper()
                if status != "FINISHED":
                    continue

                score = (data.get("score") or {}).get("fullTime") or {}
                hs = score.get("home") if "home" in score else score.get("homeTeam")
                as_ = score.get("away") if "away" in score else score.get("awayTeam")
                if hs is None or as_ is None:
                    continue
                hs = int(hs); as_ = int(as_)
                result = outcome_from_score(hs, as_)
                match_id = int(m["match_id"])

                # set match finished
                await adb(_set_match_result_sync, match_id, result, hs, as_)

                # scoring users
                preds = await adb(_get_predictions_for_match_sync, match_id)
                for p in preds:
                    uid = int(p["user_id"])
                    ok = (p["outcome"] == result)
                    await adb(_increment_user_stats_sync, uid, ok)
                    # notify user
                    try:
                        await bot.send_message(
                            uid,
                            f"🏁 Матч завершён: <b>{m['home']} — {m['away']}</b>\n"
                            f"Счёт: <b>{hs}:{as_}</b> | итог: <b>{result}</b>\n"
                            f"Твой прогноз: <b>{p['outcome']}</b> {'✅' if ok else '❌'}\n"
                            f"{'+1 очко' if ok else '+0 очков'}"
                        )
                    except Exception:
                        pass

                # resolve duels
                duels = await adb(_list_unresolved_duels_for_match_sync, match_id)
                for d in duels:
                    duel_id = int(d["duel_id"])
                    stake = int(d["stake"])
                    ch = int(d["challenger_id"])
                    op = int(d["opponent_id"])
                    status_d = d["status"]
                    if status_d == "pending":
                        # opponent never answered -> decline + refund challenger
                        await adb(_set_duel_status_sync, duel_id, "declined")
                        await adb(_add_balance_sync, ch, stake)
                        try:
                            await bot.send_message(ch, "⚔️ Дуэль отменена (соперник не ответил). Ставка возвращена.")
                        except Exception:
                            pass
                        continue
                    if status_d != "active":
                        continue
                    ch_ok = (d["challenger_outcome"] == result)
                    op_ok = (d["opponent_outcome"] == result)
                    winner = None
                    if ch_ok and not op_ok:
                        winner = ch
                    elif op_ok and not ch_ok:
                        winner = op

                    if winner is None:
                        # refund both
                        await adb(_add_balance_sync, ch, stake)
                        await adb(_add_balance_sync, op, stake)
                        await adb(_finish_duel_sync, duel_id, None)
                        msg = f"⚔️ Дуэль завершилась ничьей. Ставки возвращены.\nМатч {m['home']}—{m['away']} итог {result}."
                        for uid in (ch, op):
                            try:
                                await bot.send_message(uid, msg)
                            except Exception:
                                pass
                    else:
                        # payout winner
                        pot = stake * 2
                        await adb(_add_balance_sync, winner, pot)
                        await adb(_finish_duel_sync, duel_id, winner)
                        msg_w = f"🏆 Ты выиграл дуэль! +{pot} 🪙\nМатч {m['home']}—{m['away']} итог {result}."
                        msg_l = f"😬 Ты проиграл дуэль. -{stake} 🪙\nМатч {m['home']}—{m['away']} итог {result}."
                        try:
                            await bot.send_message(winner, msg_w)
                        except Exception:
                            pass
                        loser = op if winner == ch else ch
                        try:
                            await bot.send_message(loser, msg_l)
                        except Exception:
                            pass

        except Exception:
            logger.exception("auto_finish loop error")
        await asyncio.sleep(60)

async def loop_auto_sync():
    if not AUTO_SYNC_ENABLED:
        return
    tz = ZoneInfo(AUTO_SYNC_TZ)
    last_run: Optional[date] = None
    while True:
        try:
            now_local = datetime.now(tz)
            if now_local.hour == AUTO_SYNC_HOUR_LOCAL and now_local.minute == 0:
                if last_run != now_local.date():
                    # run
                    last_run = now_local.date()
                    if ADMIN_ID:
                        try:
                            await bot.send_message(ADMIN_ID, "⏰ Автосинк: запускаю /sync_today")
                        except Exception:
                            pass
                    try:
                        matches = await fd_today_matches()
                    except Exception:
                        matches = []
                    # auto-send cards to admin for confirmation
                    now = utcnow()
                    for m in matches:
                        if m.kickoff_utc <= now + timedelta(seconds=PREDICTION_CLOSE_SECONDS):
                            continue
                        SYNC_CACHE[m.ext_id] = m
                        text = (f"📌 <b>{m.home} — {m.away}</b>\n"
                                f"🏟 {m.competition}\n"
                                f"🕒 {m.kickoff_utc.astimezone(tz).strftime('%d %b %H:%M')} ({AUTO_SYNC_TZ})\n"
                                f"ID: <code>{m.ext_id}</code>")
                        try:
                            await bot.send_message(ADMIN_ID, text, reply_markup=kb_admin_sync(m))
                        except Exception:
                            pass
        except Exception:
            logger.exception("auto_sync loop error")
        await asyncio.sleep(60)

# ------------------------- Render Web Service (port binding) -------------------------

async def health(request):
    return web.Response(text="OK")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", HTTP_PORT)
    await site.start()
    logger.info(f"Web server started on port {HTTP_PORT}")

# ------------------------- Startup -------------------------

async def on_startup():
    await adb(_init_db_sync)
    # background tasks
    asyncio.create_task(start_web_server())
    asyncio.create_task(loop_heartbeat())
    asyncio.create_task(loop_auto_close())
    asyncio.create_task(loop_auto_finish())
    asyncio.create_task(loop_auto_sync())

async def main():
    await on_startup()
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

if __name__ == "__main__":
    asyncio.run(main())
