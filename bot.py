# -*- coding: utf-8 -*-
"""
Predictor Bot (aiogram v3.7+)

UI
- Главное меню (кнопки)
- ⚡ Активные матчи -> выбор спорта -> список матчей (пагинация) -> карточка матча
- 🔎 Поиск матча по названию/командам
- 🔖 Короткий код матча (ABC-DEF • HH:MM)

Функции
- Только 1X2
- Дедлайн прогнозов: PREDICT_DEADLINE_MIN минут до старта
- Автосинк матчей: football-data.org + NHL
- Авто-итоги и начисление очков
- Профиль + лидерборд
- Keep-alive (Render free): пинг public /health если задан KEEP_ALIVE_URL
- Render health server на 0.0.0.0:$PORT (если PORT задан)

ENV
- BOT_TOKEN (required)
- ADMIN_ID (optional)
- PORT (Render sets; if you deploy as Web Service)
- KEEP_ALIVE_URL (optional, e.g. https://<service>.onrender.com/health)
- KEEP_ALIVE_INTERVAL=300
- PREDICT_DEADLINE_MIN=5
- SYNC_ENABLED=1, SYNC_INTERVAL=3600, SYNC_LOOKAHEAD_DAYS=1
- FOOTBALL_ENABLED=1, FOOTBALL_DATA_TOKEN=..., FOOTBALL_COMPETITIONS=PL,CL,PD,SA,BL1,FL1
- NHL_ENABLED=1
- AUTO_RESULTS_ENABLED=1, AUTO_RESULTS_INTERVAL=300, AUTO_RESULTS_MIN_AGE_MIN=20
- POINTS_FOR_CORRECT=3, POINTS_FOR_WRONG=0
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
from aiohttp import web

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

# =========================
# CONFIG
# =========================

BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or "0")
DB_PATH = os.getenv("DB_PATH", "bot.db")
PORT = int(os.getenv("PORT", "0") or "0")

KEEP_ALIVE_URL = (os.getenv("KEEP_ALIVE_URL") or "").strip()
KEEP_ALIVE_INTERVAL = int(os.getenv("KEEP_ALIVE_INTERVAL", "300") or "300")

PREDICT_DEADLINE_MIN = int(os.getenv("PREDICT_DEADLINE_MIN", "5") or "5")

SYNC_ENABLED = os.getenv("SYNC_ENABLED", "1") == "1"
SYNC_INTERVAL = int(os.getenv("SYNC_INTERVAL", "3600") or "3600")
SYNC_LOOKAHEAD_DAYS = int(os.getenv("SYNC_LOOKAHEAD_DAYS", "1") or "1")

FOOTBALL_ENABLED = os.getenv("FOOTBALL_ENABLED", "1") == "1"
FOOTBALL_DATA_TOKEN = (os.getenv("FOOTBALL_DATA_TOKEN") or "").strip()
FOOTBALL_COMPETITIONS = [
    c.strip()
    for c in (os.getenv("FOOTBALL_COMPETITIONS") or "PL,CL,PD,SA,BL1,FL1").split(",")
    if c.strip()
]
FOOTBALL_BASE = (os.getenv("FOOTBALL_BASE") or "https://api.football-data.org/v4").rstrip("/")

NHL_ENABLED = os.getenv("NHL_ENABLED", "1") == "1"

AUTO_RESULTS_ENABLED = os.getenv("AUTO_RESULTS_ENABLED", "1") == "1"
AUTO_RESULTS_INTERVAL = int(os.getenv("AUTO_RESULTS_INTERVAL", "300") or "300")
AUTO_RESULTS_MIN_AGE_MIN = int(os.getenv("AUTO_RESULTS_MIN_AGE_MIN", "20") or "20")

POINTS_FOR_CORRECT = int(os.getenv("POINTS_FOR_CORRECT", "3") or "3")
POINTS_FOR_WRONG = int(os.getenv("POINTS_FOR_WRONG", "0") or "0")

# Betting / bankroll
BETTING_ENABLED = os.getenv("BETTING_ENABLED", "1") == "1"
WEEKLY_BONUS_ENABLED = os.getenv("WEEKLY_BONUS_ENABLED", "1") == "1"
WEEKLY_BONUS_AMOUNT = int(os.getenv("WEEKLY_BONUS_AMOUNT", "1000") or "1000")
DISPLAY_TZ = os.getenv("DISPLAY_TZ", "Europe/Moscow")
try:
    DISPLAY_ZONE = ZoneInfo(DISPLAY_TZ)
except Exception:
    DISPLAY_ZONE = ZoneInfo("Europe/Moscow")

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL)
logger = logging.getLogger("predictor_bot")

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# =========================
# IN-MEMORY PREFS
# =========================

PREFS: Dict[int, Dict[str, Any]] = {}

def set_pref(user_id: int, **data: Any) -> None:
    cur = PREFS.get(user_id, {})
    cur.update(data)
    PREFS[user_id] = cur

def get_pref(user_id: int, key: str, default: Any = None) -> Any:
    return PREFS.get(user_id, {}).get(key, default)

def clear_pref(user_id: int, key: str) -> None:
    if user_id in PREFS and key in PREFS[user_id]:
        del PREFS[user_id][key]

# =========================
# DB + UTILS
# =========================

def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()

def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)

def is_admin(uid: int) -> bool:
    return ADMIN_ID != 0 and uid == ADMIN_ID

def init_db() -> None:
    with db() as con:
        cur = con.cursor()

        cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            updated_at TEXT
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS matches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT,
            start_time TEXT,
            status TEXT DEFAULT 'open',
            result TEXT
        )
        """)
        for stmt in [
            "ALTER TABLE matches ADD COLUMN sport TEXT",
            "ALTER TABLE matches ADD COLUMN league TEXT",
            "ALTER TABLE matches ADD COLUMN source TEXT",
            "ALTER TABLE matches ADD COLUMN external_id TEXT",
            "ALTER TABLE matches ADD COLUMN start_time_utc TEXT",
            "ALTER TABLE matches ADD COLUMN created_at TEXT",
                    "ALTER TABLE matches ADD COLUMN odds_1 REAL",
            "ALTER TABLE matches ADD COLUMN odds_x REAL",
            "ALTER TABLE matches ADD COLUMN odds_2 REAL",
]:
            try:
                cur.execute(stmt)
            except sqlite3.OperationalError:
                pass
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_match_src_ext ON matches(source, external_id)")

        cur.execute("""
        CREATE TABLE IF NOT EXISTS votes (
            user_id INTEGER,
            match_id INTEGER,
            pick TEXT,
            created_at TEXT,
            UNIQUE(user_id, match_id)
        )
        """)


for stmt in [
    "ALTER TABLE votes ADD COLUMN stake INTEGER",
    "ALTER TABLE votes ADD COLUMN odds REAL",
]:
    try:
        cur.execute(stmt)
    except sqlite3.OperationalError:
        pass
        cur.execute("""
        CREATE TABLE IF NOT EXISTS scores (
            user_id INTEGER PRIMARY KEY,
            points INTEGER DEFAULT 0,
            correct INTEGER DEFAULT 0,
            total INTEGER DEFAULT 0,
            streak INTEGER DEFAULT 0,
            best_streak INTEGER DEFAULT 0,
            updated_at TEXT
        )
        """)
for stmt in [
    "ALTER TABLE scores ADD COLUMN balance INTEGER DEFAULT 0",
]:
    try:
        cur.execute(stmt)
    except sqlite3.OperationalError:
        pass


        cur.execute("""
        CREATE TABLE IF NOT EXISTS points_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            match_id INTEGER,
            points INTEGER,
            created_at TEXT
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS featured (
            day_utc TEXT PRIMARY KEY,
            match_id INTEGER,
            created_at TEXT
        )
        """)

        con.commit()

def upsert_user_from_message(m: Message | CallbackQuery) -> None:
    u = m.from_user
    if not u:
        return
    with db() as con:
        cur = con.cursor()
        cur.execute("""
            INSERT INTO users(user_id, username, first_name, last_name, updated_at)
            VALUES(?,?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET
              username=excluded.username,
              first_name=excluded.first_name,
              last_name=excluded.last_name,
              updated_at=excluded.updated_at
        """, (u.id, u.username or "", u.first_name or "", u.last_name or "", iso(now_utc())))
        cur.execute("INSERT OR IGNORE INTO scores(user_id, updated_at) VALUES(?,?)", (u.id, iso(now_utc())))
        con.commit()

def pretty_user(user_id: int) -> str:
    with db() as con:
        r = con.execute("SELECT username, first_name, last_name FROM users WHERE user_id=?", (user_id,)).fetchone()
    if not r:
        return str(user_id)
    if (r["username"] or "").strip():
        return f"@{r['username'].strip()}"
    name = f"{(r['first_name'] or '').strip()} {(r['last_name'] or '').strip()}".strip()
    return name if name else str(user_id)

# =========================
# UI
# =========================

BTN_ACTIVE = "⚡ Активные матчи"
BTN_TODAY = "🔥 Матч дня"
BTN_FIND_MATCH = "🔎 Найти матч"
BTN_MY = "🧾 Мои прогнозы"
BTN_LB = "🏆 Лидерборд"
BTN_PROFILE = "👤 Профиль"
BTN_HELP = "ℹ️ Помощь"

SPORT_PRETTY = {
    "football": "⚽ Футбол",
    "hockey": "🏒 Хоккей",
    "nhl": "🏒 Хоккей",
    "esports": "🎮 Киберспорт",
    "other": "🏟 Другое",
}

PER_PAGE = 10
RU_MON = ["янв","фев","мар","апр","май","июн","июл","авг","сен","окт","ноя","дек"]

def _sport_emoji(sport: str) -> str:
    s = (sport or "").lower()
    if "foot" in s:
        return "⚽"
    if "hock" in s or "nhl" in s:
        return "🏒"
    if "esport" in s or "dota" in s or "cs" in s:
        return "🎮"
    return "🏟"

def _pretty_time(dt_raw: str) -> str:
    if not dt_raw:
        return "—"
    try:
        dt = datetime.fromisoformat(dt_raw.replace("Z", "+00:00")).astimezone(timezone.utc)
        return f"{dt.day:02d} {RU_MON[dt.month-1]} • {dt.hour:02d}:{dt.minute:02d} UTC"
    except Exception:
        return dt_raw.replace("T", " ").replace("+00:00", " UTC")

def _pretty_title(title: str, sport: str) -> str:
    t = (title or "").strip()
    t = re.sub(r"\s+vs\.?\s+", " 🆚 ", t, flags=re.I)
    t = re.sub(r"\s+-\s+", " 🆚 ", t)
    if "🆚" in t:
        a, b = [p.strip() for p in t.split("🆚", 1)]
        if a and b:
            t = f"{a.upper()} 🆚 {b.upper()}"
    return f"{_sport_emoji(sport)} {t}"

def _abbr_team(name: str) -> str:
    s = re.sub(r"[^A-Za-zА-Яа-я0-9 ]+", " ", name).strip()
    if not s:
        return "---"
    parts = [p for p in s.split() if p]
    base = (parts[0] if parts else s)[:3]
    return base.upper()

def _short_code(title: str) -> str:
    t = re.sub(r"\s+vs\.?\s+", " 🆚 ", (title or ""), flags=re.I)
    t = re.sub(r"\s+-\s+", " 🆚 ", t)
    if "🆚" in t:
        a, b = [p.strip() for p in t.split("🆚", 1)]
        return f"{_abbr_team(a)}-{_abbr_team(b)}"
    return _abbr_team(t)

def main_menu() -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text=BTN_ACTIVE), KeyboardButton(text=BTN_TODAY)],
        [KeyboardButton(text=BTN_FIND_MATCH), KeyboardButton(text=BTN_MY)],
        [KeyboardButton(text=BTN_LB), KeyboardButton(text=BTN_PROFILE)],
        [KeyboardButton(text=BTN_HELP)],
    ]
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)

def ikb_sports(sports: List[Tuple[str, int]]) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    for sport, cnt in sports:
        s = (sport or "other").lower()
        label = SPORT_PRETTY.get(s, f"🏟 {s}")
        rows.append([InlineKeyboardButton(text=f"{label} ({cnt})", callback_data=f"sport:{s}:0")])
    rows.append([InlineKeyboardButton(text="📋 Все матчи", callback_data="sport:all:0")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def ikb_matches_list(sport: str, page: int, items: List[sqlite3.Row], total: int) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    for r in items:
        mid = int(r["id"])
        title = _pretty_title((r["title"] or ""), (r["sport"] or sport))
        if len(title) > 46:
            title = title[:46] + "…"
        rows.append([InlineKeyboardButton(text=title, callback_data=f"mopen:{mid}")])

    max_page = max(0, (total - 1) // PER_PAGE)
    nav: List[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"sport:{sport}:{page-1}"))
    nav.append(InlineKeyboardButton(text=f"{page+1}/{max_page+1}", callback_data="noop"))
    if page < max_page:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"sport:{sport}:{page+1}"))
    rows.append(nav)

    rows.append([InlineKeyboardButton(text="⬅️ Назад к видам спорта", callback_data="back:sports")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def ikb_match_card(match_id: int) -> InlineKeyboardMarkup:
    odds = compute_live_odds(match_id) if BETTING_ENABLED else {"1": 0, "X": 0, "2": 0}
    b1 = f"1 ({odds['1']})" if BETTING_ENABLED else "1"
    bx = f"X ({odds['X']})" if BETTING_ENABLED else "X"
    b2 = f"2 ({odds['2']})" if BETTING_ENABLED else "2"
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=b1, callback_data=f"pick:{match_id}:1"),
            InlineKeyboardButton(text=bx, callback_data=f"pick:{match_id}:X"),
            InlineKeyboardButton(text=b2, callback_data=f"pick:{match_id}:2"),
        ],

        [InlineKeyboardButton(text="📊 Голоса", callback_data=f"stats:{match_id}")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back:sports")],
    ])

# =========================
# QUERIES + DEADLINE
# =========================

def get_open_sports() -> List[Tuple[str, int]]:
    with db() as con:
        rows = con.execute("""
            SELECT COALESCE(NULLIF(LOWER(sport), ''), 'other') AS sport, COUNT(*) AS c
            FROM matches
            WHERE status='open'
            GROUP BY COALESCE(NULLIF(LOWER(sport), ''), 'other')
            ORDER BY c DESC
        """).fetchall()
    return [(r["sport"], int(r["c"])) for r in rows]

def count_open_matches(sport: str) -> int:
    with db() as con:
        if sport == "all":
            r = con.execute("SELECT COUNT(*) c FROM matches WHERE status='open'").fetchone()
        else:
            r = con.execute("""
                SELECT COUNT(*) c FROM matches
                WHERE status='open' AND COALESCE(NULLIF(LOWER(sport), ''), 'other')=?
            """, (sport,)).fetchone()
    return int(r["c"]) if r else 0

def get_open_matches_page(sport: str, page: int) -> List[sqlite3.Row]:
    offset = max(0, page) * PER_PAGE
    with db() as con:
        if sport == "all":
            return con.execute("""
                SELECT id, title, start_time_utc, league, sport, start_time
                FROM matches
                WHERE status='open'
                ORDER BY COALESCE(start_time_utc, start_time) ASC
                LIMIT ? OFFSET ?
            """, (PER_PAGE, offset)).fetchall()

        return con.execute("""
            SELECT id, title, start_time_utc, league, sport, start_time
            FROM matches
            WHERE status='open' AND COALESCE(NULLIF(LOWER(sport), ''), 'other')=?
            ORDER BY COALESCE(start_time_utc, start_time) ASC
            LIMIT ? OFFSET ?
        """, (sport, PER_PAGE, offset)).fetchall()

def get_match(match_id: int) -> Optional[sqlite3.Row]:
    with db() as con:
        return con.execute("SELECT * FROM matches WHERE id=?", (match_id,)).fetchone()

def get_my_pick(user_id: int, match_id: int) -> Optional[str]:
    with db() as con:
        r = con.execute("SELECT pick FROM votes WHERE user_id=? AND match_id=?", (user_id, match_id)).fetchone()
    return r["pick"] if r else None

def match_stats(match_id: int) -> Dict[str, int]:
    counts = {"1": 0, "X": 0, "2": 0}
    with db() as con:
        rows = con.execute("SELECT pick, COUNT(*) c FROM votes WHERE match_id=? GROUP BY pick", (match_id,)).fetchall()
    for r in rows:
        p = r["pick"]
        if p in counts:
            counts[p] += int(r["c"])
    return counts

def deadline_for_match(match_row: sqlite3.Row) -> Optional[datetime]:
    st = (match_row["start_time_utc"] or match_row["start_time"] or "").strip()
    if not st:
        return None
    try:
        start_dt = parse_iso(st)
    except Exception:
        return None
    return start_dt - timedelta(minutes=max(0, PREDICT_DEADLINE_MIN))

def can_predict(match_row: sqlite3.Row) -> Tuple[bool, str]:
    if match_row["status"] != "open":
        return False, "Матч закрыт."
    dl = deadline_for_match(match_row)
    if not dl:
        return True, ""
    if now_utc() >= dl:
        return False, f"Ставки закрыты (дедлайн за {PREDICT_DEADLINE_MIN} мин до старта)."
    return True, ""

# =========================
# AUTOSYNC + RESULTS
# =========================

@dataclass
class SyncedMatch:
    source: str
    external_id: str
    sport: str
    league: str
    title: str
    start_time_utc: datetime

@dataclass
class FinishedInfo:
    result_1x2: str

async def http_json(session: aiohttp.ClientSession, url: str, headers: Optional[Dict[str, str]] = None, timeout_s: int = 20) -> Any:
    async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=timeout_s)) as resp:
        txt = await resp.text()
        if resp.status >= 400:
            raise RuntimeError(f"HTTP {resp.status} {url}: {txt[:200]}")
        return await resp.json()

async def football_list(session: aiohttp.ClientSession, date_from: datetime, date_to: datetime) -> List[SyncedMatch]:
    if not FOOTBALL_ENABLED or not FOOTBALL_DATA_TOKEN:
        return []
    headers = {"X-Auth-Token": FOOTBALL_DATA_TOKEN}
    df = date_from.date().isoformat()
    dt = date_to.date().isoformat()
    out: List[SyncedMatch] = []
    for comp in FOOTBALL_COMPETITIONS:
        url = f"{FOOTBALL_BASE}/competitions/{comp}/matches?dateFrom={df}&dateTo={dt}"
        try:
            data = await http_json(session, url, headers=headers)
        except Exception as e:
            logger.warning("football list failed comp=%s err=%s", comp, e)
            continue
        for m in (data.get("matches") or []):
            mid = str(m.get("id") or "")
            utc = m.get("utcDate") or ""
            if not mid or not utc:
                continue
            try:
                start = datetime.fromisoformat(utc.replace("Z", "+00:00")).astimezone(timezone.utc)
            except Exception:
                continue
            home = ((m.get("homeTeam") or {}).get("name") or "").strip()
            away = ((m.get("awayTeam") or {}).get("name") or "").strip()
            league = ((m.get("competition") or {}).get("name") or comp).strip()
            title = f"{home} vs {away}".strip()
            out.append(SyncedMatch("football", mid, "football", league, title, start))
    return out

async def football_result(session: aiohttp.ClientSession, external_id: str) -> Optional[FinishedInfo]:
    if not FOOTBALL_DATA_TOKEN:
        return None
    headers = {"X-Auth-Token": FOOTBALL_DATA_TOKEN}
    data = await http_json(session, f"{FOOTBALL_BASE}/matches/{external_id}", headers=headers)
    match = data.get("match") or {}
    status = (match.get("status") or "").upper()
    if status not in ("FINISHED", "AWARDED"):
        return None
    score = match.get("score") or {}
    ft = (score.get("fullTime") or {})
    hg = ft.get("home")
    ag = ft.get("away")
    if hg is None or ag is None:
        winner = score.get("winner")
        if winner == "HOME_TEAM":
            return FinishedInfo("1")
        if winner == "AWAY_TEAM":
            return FinishedInfo("2")
        if winner == "DRAW":
            return FinishedInfo("X")
        return None
    hg = int(hg); ag = int(ag)
    if hg > ag:
        return FinishedInfo("1")
    if hg < ag:
        return FinishedInfo("2")
    return FinishedInfo("X")

async def nhl_list(session: aiohttp.ClientSession, date_from: datetime, date_to: datetime) -> List[SyncedMatch]:
    if not NHL_ENABLED:
        return []
    out: List[SyncedMatch] = []
    day = date_from.date()
    end = date_to.date()
    while day <= end:
        ds = day.isoformat()
        data = None
        try:
            data = await http_json(session, f"https://api-web.nhle.com/v1/schedule/{ds}")
        except Exception:
            try:
                data = await http_json(session, f"https://statsapi.web.nhl.com/api/v1/schedule?date={ds}")
            except Exception as e:
                logger.warning("nhl schedule failed day=%s err=%s", ds, e)
                data = None
        games: List[Dict[str, Any]] = []
        if data:
            if "gameWeek" in data:
                for d in data.get("gameWeek", []) or []:
                    if (d.get("date") or "") == ds:
                        games = d.get("games") or []
                        break
            elif "dates" in data:
                for d in data.get("dates", []) or []:
                    games.extend(d.get("games", []) or [])
        for g in games:
            gid = str(g.get("id") or g.get("gamePk") or "")
            if not gid:
                continue
            start_raw = g.get("startTimeUTC") or g.get("gameDate") or ""
            if not start_raw:
                continue
            try:
                start = datetime.fromisoformat(start_raw.replace("Z", "+00:00")).astimezone(timezone.utc)
            except Exception:
                continue
            if "homeTeam" in g:
                home = str(((g["homeTeam"].get("placeName") or {}).get("default")) or g["homeTeam"].get("name", ""))
                away = str(((g["awayTeam"].get("placeName") or {}).get("default")) or g["awayTeam"].get("name", ""))
            else:
                home = ((g.get("teams") or {}).get("home") or {}).get("team", {}).get("name", "")
                away = ((g.get("teams") or {}).get("away") or {}).get("team", {}).get("name", "")
            out.append(SyncedMatch("nhl", gid, "hockey", "NHL", f"{home} vs {away}", start))
        day = day + timedelta(days=1)
    return out

async def nhl_result(session: aiohttp.ClientSession, external_id: str) -> Optional[FinishedInfo]:
    try:
        data = await http_json(session, f"https://api-web.nhle.com/v1/gamecenter/{external_id}/landing")
        state = (data.get("gameState") or "").upper()
        if state not in ("FINAL", "OFF", "OVER"):
            return None
        hs = data.get("homeTeam", {}).get("score")
        a_s = data.get("awayTeam", {}).get("score")
        if hs is None or a_s is None:
            return None
        hs = int(hs); a_s = int(a_s)
        if hs > a_s:
            return FinishedInfo("1")
        if hs < a_s:
            return FinishedInfo("2")
        return FinishedInfo("X")
    except Exception:
        return None

def upsert_matches(matches: List[SyncedMatch]) -> Tuple[int, int]:
    inserted = 0
    updated = 0
    with db() as con:
        cur = con.cursor()
        for m in matches:
            cur.execute("SELECT id FROM matches WHERE source=? AND external_id=?", (m.source, m.external_id))
            row = cur.fetchone()
            if row:
                cur.execute("""
                    UPDATE matches
                    SET title=?, sport=?, league=?, start_time_utc=?
                    WHERE source=? AND external_id=? AND status='open'
                """, (m.title, m.sport, m.league, iso(m.start_time_utc), m.source, m.external_id))
                updated += 1
            else:
                cur.execute("""
                    INSERT INTO matches(title, start_time, status, result, sport, league, source, external_id, start_time_utc, created_at)
                    VALUES(?, ?, 'open', NULL, ?, ?, ?, ?, ?, ?)
                """, (m.title, iso(m.start_time_utc), m.sport, m.league, m.source, m.external_id, iso(m.start_time_utc), iso(now_utc())))
                inserted += 1
        con.commit()
    return inserted, updated

async def autosync_once() -> str:
    start = now_utc().replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=max(0, SYNC_LOOKAHEAD_DAYS))
    report: List[str] = []
    async with aiohttp.ClientSession() as session:
        allm: List[SyncedMatch] = []
        if FOOTBALL_ENABLED:
            fm = await football_list(session, start, end)
            allm.extend(fm)
            report.append(f"Football {len(fm)}")
        if NHL_ENABLED:
            nm = await nhl_list(session, start, end)
            allm.extend(nm)
            report.append(f"NHL {len(nm)}")
        ins, upd = upsert_matches(allm)
        report.append(f"DB +{ins}/~{upd}")
    msg = "Sync: " + " | ".join(report) if report else "Sync: nothing"
    logger.info(msg)
    return msg

def today_key() -> str:
    return now_utc().date().isoformat()

def pick_featured_for_today() -> Optional[int]:
    day = today_key()
    with db() as con:
        r = con.execute("SELECT match_id FROM featured WHERE day_utc=?", (day,)).fetchone()
        if r:
            return int(r["match_id"])
        start = now_utc().replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        row = con.execute("""
            SELECT id FROM matches
            WHERE status='open'
              AND COALESCE(start_time_utc, start_time) >= ?
              AND COALESCE(start_time_utc, start_time) < ?
            ORDER BY COALESCE(start_time_utc, start_time) ASC
            LIMIT 1
        """, (iso(start), iso(end))).fetchone()
        if not row:
            return None
        mid = int(row["id"])
        con.execute("INSERT OR REPLACE INTO featured(day_utc, match_id, created_at) VALUES(?,?,?)", (day, mid, iso(now_utc())))
        con.commit()
        return mid

    async def apply_scoring_for_match(match_id: int, result_1x2: str) -> None:
        with db() as con:
            cur = con.cursor()
            st = cur.execute("SELECT status FROM matches WHERE id=?", (match_id,)).fetchone()
            if not st or st["status"] != "open":
                return

            cur.execute("UPDATE matches SET result=?, status='closed' WHERE id=?", (result_1x2, match_id))

            votes = cur.execute("SELECT user_id, pick, stake, odds FROM votes WHERE match_id=?", (match_id,)).fetchall()
            for v in votes:
                uid = int(v["user_id"])
                pick = v["pick"]
                stake = int(v["stake"]) if v["stake"] is not None else 0
                odds = float(v["odds"]) if v["odds"] is not None else 0.0

                correct = (pick == result_1x2)
                delta_points = POINTS_FOR_CORRECT if correct else POINTS_FOR_WRONG

                cur.execute("INSERT OR IGNORE INTO scores(user_id, updated_at, balance) VALUES(?,?,?)", (uid, iso(now_utc()), 0))

                # points/streak
                if correct:
                    cur.execute(
                        """
                        UPDATE scores
                        SET points = points + ?,
                            correct = correct + 1,
                            total = total + 1,
                            streak = streak + 1,
                            best_streak = CASE WHEN streak + 1 > best_streak THEN streak + 1 ELSE best_streak END,
                            updated_at = ?
                        WHERE user_id=?
                        """,
                        (delta_points, iso(now_utc()), uid),
                    )
                else:
                    cur.execute(
                        """
                        UPDATE scores
                        SET points = points + ?,
                            total = total + 1,
                            streak = 0,
                            updated_at = ?
                        WHERE user_id=?
                        """,
                        (delta_points, iso(now_utc()), uid),
                    )

                if delta_points != 0:
                    cur.execute(
                        "INSERT INTO points_log(user_id, match_id, points, created_at) VALUES(?,?,?,?)",
                        (uid, match_id, delta_points, iso(now_utc())),
                    )

                # betting settlement
                win_amount = 0
                net_profit = 0
                if BETTING_ENABLED and stake > 0 and odds > 0:
                    if correct:
                        win_amount = int(round(stake * odds))
                        net_profit = win_amount - stake
                        cur.execute("UPDATE scores SET balance = balance + ? WHERE user_id=?", (win_amount, uid))
                    else:
                        net_profit = -stake

            con.commit()

        # Notify users (outside transaction)
        try:
            match = get_match(match_id)
            title = _pretty_title((match["title"] or ""), (match["sport"] or "other")) if match else f"Матч #{match_id}"
        except Exception:
            title = f"Матч #{match_id}"

        for v in votes:
            try:
                uid = int(v["user_id"])
                pick = v["pick"]
                stake = int(v["stake"]) if v["stake"] is not None else 0
                odds = float(v["odds"]) if v["odds"] is not None else 0.0
                correct = (pick == result_1x2)

                # fetch new balance
                with db() as con:
                    r = con.execute("SELECT balance, points FROM scores WHERE user_id=?", (uid,)).fetchone()
                bal = int(r["balance"]) if r and r["balance"] is not None else 0

                if BETTING_ENABLED and stake > 0 and odds > 0:
                    if correct:
                        win_amount = int(round(stake * odds))
                        profit = win_amount - stake
                        await bot.send_message(
                            uid,
                            f"✅ <b>Результат матча</b>\n{title}\n\n"
                            f"Итог: <b>{result_1x2}</b>\n"
                            f"Твой прогноз: <b>{pick}</b>\n\n"
                            f"💰 Ты выиграл: <b>{win_amount}</b> (профит {profit:+d})\n"
                            f"Баланс: <b>{bal}</b>",
                        )
                    else:
                        await bot.send_message(
                            uid,
                            f"❌ <b>Результат матча</b>\n{title}\n\n"
                            f"Итог: <b>{result_1x2}</b>\n"
                            f"Твой прогноз: <b>{pick}</b>\n\n"
                            f"💸 Ты проиграл: <b>{stake}</b>\n"
                            f"Баланс: <b>{bal}</b>",
                        )
                else:
                    # points-only notification (optional)
                    await bot.send_message(
                        uid,
                        f"🏁 Матч завершён\n{title}\n"
                        f"Итог: <b>{result_1x2}</b>\n"
                        f"Твой прогноз: <b>{pick}</b>",
                    )
            except Exception:
                pass

async def autosync_loop():
    while True:
        try:
            if SYNC_ENABLED:
                await autosync_once()
                pick_featured_for_today()
        except Exception as e:
            logger.exception("autosync_loop error: %s", e)
        await asyncio.sleep(max(300, SYNC_INTERVAL))

async def auto_results_loop():
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                await asyncio.sleep(max(30, AUTO_RESULTS_INTERVAL))
                if not AUTO_RESULTS_ENABLED:
                    continue

                cutoff = now_utc() - timedelta(minutes=max(0, AUTO_RESULTS_MIN_AGE_MIN))
                cutoff_s = iso(cutoff)

                with db() as con:
                    candidates = con.execute("""
                        SELECT id, source, external_id
                        FROM matches
                        WHERE status='open'
                          AND source IS NOT NULL AND external_id IS NOT NULL
                          AND COALESCE(start_time_utc, start_time) <= ?
                        ORDER BY COALESCE(start_time_utc, start_time) ASC
                        LIMIT 80
                    """, (cutoff_s,)).fetchall()

                for r in candidates:
                    mid = int(r["id"])
                    source = (r["source"] or "").lower()
                    ext = (r["external_id"] or "").strip()
                    if not ext:
                        continue

                    fin: Optional[FinishedInfo] = None
                    if source == "football":
                        try:
                            fin = await football_result(session, ext)
                        except Exception as e:
                            logger.warning("football_result failed: %s", e)
                    elif source == "nhl":
                        try:
                            fin = await nhl_result(session, ext)
                        except Exception as e:
                            logger.warning("nhl_result failed: %s", e)

                    if not fin:
                        continue

                    await apply_scoring_for_match(mid, fin.result_1x2)

                    if ADMIN_ID:
                        try:
                            await bot.send_message(ADMIN_ID, f"✅ Итог проставлен: {fin.result_1x2}")
                        except Exception:
                            pass

            except Exception as e:
                logger.exception("auto_results_loop error: %s", e)


    def next_weekly_bonus_run(now: datetime) -> datetime:
        """Next Monday 12:00 in DISPLAY_ZONE."""
        local = now.astimezone(DISPLAY_ZONE)
        # Monday=0
        days_ahead = (0 - local.weekday()) % 7
        target = local.replace(hour=12, minute=0, second=0, microsecond=0) + timedelta(days=days_ahead)
        if target <= local:
            target = target + timedelta(days=7)
        return target.astimezone(timezone.utc)

    async def weekly_bonus_loop():
        if not WEEKLY_BONUS_ENABLED:
            return
        while True:
            try:
                run_at = next_weekly_bonus_run(now_utc())
                sleep_s = max(5, int((run_at - now_utc()).total_seconds()))
                await asyncio.sleep(sleep_s)

                # award
                with db() as con:
                    cur = con.cursor()
                    user_rows = cur.execute("SELECT user_id FROM users").fetchall()
                    uids = [int(r["user_id"]) for r in user_rows]
                    for uid in uids:
                        cur.execute("INSERT OR IGNORE INTO scores(user_id, updated_at, balance) VALUES(?,?,?)", (uid, iso(now_utc()), 0))
                    cur.execute("UPDATE scores SET balance = COALESCE(balance,0) + ?, updated_at=?", (WEEKLY_BONUS_AMOUNT, iso(now_utc())))
                    con.commit()

                # notify
                for uid in uids:
                    try:
                        await bot.send_message(
                            uid,
                            f"🎁 Еженедельный бонус: +<b>{WEEKLY_BONUS_AMOUNT}</b> баллов!\n"
                            f"Выдан в понедельник 12:00 по МСК.",
                        )
                    except Exception:
                        pass
            except Exception as e:
                logger.exception("weekly_bonus_loop error: %s", e)
                await asyncio.sleep(60)

async def keep_alive_loop():
    """Keep Render free Web Service from spinning down.
    Set KEEP_ALIVE_URL to your public /health URL and it will ping it periodically.
    """
    if not KEEP_ALIVE_URL:
        return
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                async with session.get(KEEP_ALIVE_URL, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    await resp.text()
                logger.debug("keep-alive ping ok")
            except Exception as e:
                logger.warning("keep-alive ping failed: %s", e)
            await asyncio.sleep(max(60, KEEP_ALIVE_INTERVAL))

# =========================
# WEB SERVER (Render)
# =========================

async def start_web_server():
    # If you deploy as Render Web Service, PORT must be bound.
    if PORT <= 0:
        return

    async def ok(_request: web.Request) -> web.Response:
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_get("/", ok)
    app.router.add_get("/health", ok)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=PORT)
    await site.start()
    logger.info("web server started on 0.0.0.0:%s", PORT)

    while True:
        await asyncio.sleep(3600)

# =========================
# HANDLERS
# =========================

@dp.message(Command("start"))
async def cmd_start(m: Message):
    upsert_user_from_message(m)
    text_msg = (
        "👋 <b>Привет!</b>\n\n"
        "Это бот прогнозов <b>1X2</b>.\n"
        "Жми <b>⚡ Активные матчи</b> → выбери спорт → матч.\n\n"
        f"⏱ Дедлайн: за <b>{PREDICT_DEADLINE_MIN}</b> мин до старта."
    )
    await m.answer(text_msg, reply_markup=main_menu())

@dp.message(Command("help"))
@dp.message(F.text == BTN_HELP)
async def help_btn(m: Message):
    upsert_user_from_message(m)
    await m.answer(
        "ℹ️ <b>Помощь</b>\n\n"
        "• ⚡ Активные матчи → спорт → матч → 1/X/2\n"
        "• 🔎 Найти матч — поиск по команде/части названия\n"
        "• 🔥 Матч дня — быстрый доступ к матчу сегодня\n\n"
        f"Очки за верный исход: <b>+{POINTS_FOR_CORRECT}</b>",
        reply_markup=main_menu(),
    )

@dp.message(Command("sync_now"))
async def sync_cmd(m: Message):
    upsert_user_from_message(m)
    if not m.from_user or not is_admin(m.from_user.id):
        return
    msg = await autosync_once()
    pick_featured_for_today()
    await m.answer(f"✅ {msg}", reply_markup=main_menu())


# Fallback: some clients send commands as plain text with mention (e.g. /sync_now@MyBot) or extra spaces.
# This handler ensures /sync_now works even if Command() filter misses for any reason.
@dp.message(F.text.regexp(r"^/sync_now(@\w+)?(\s|$)"))
async def sync_cmd_fallback(m: Message):
    await sync_cmd(m)


@dp.message(F.text == BTN_ACTIVE)
async def active_matches(m: Message):
    upsert_user_from_message(m)
    sports = get_open_sports()
    if not sports:
        await m.answer("Пока нет активных матчей.", reply_markup=main_menu())
        return
    await m.answer("⚡ <b>Активные матчи</b>\n\nВыбери вид спорта 👇", reply_markup=main_menu())
    await m.answer("Категории:", reply_markup=ikb_sports(sports))

@dp.callback_query(F.data.startswith("sport:"))
async def cb_sport(cb: CallbackQuery):
    upsert_user_from_message(cb)
    try:
        _, sport, page_s = cb.data.split(":")
        page = int(page_s)
    except Exception:
        await cb.answer("Ошибка.", show_alert=True)
        return

    sport = (sport or "all").lower()
    total = count_open_matches(sport)
    if total <= 0:
        await cb.answer("В этой категории матчей нет.", show_alert=True)
        return

    max_page = max(0, (total - 1) // PER_PAGE)
    page = min(max(page, 0), max_page)

    items = get_open_matches_page(sport, page)
    header = "📋 Все матчи" if sport == "all" else SPORT_PRETTY.get(sport, f"🏟 {sport}")

    blocks: List[str] = []
    for r in items:
        st = _pretty_time((r["start_time_utc"] or r["start_time"] or ""))
        league = (r["league"] or "").strip()
        title = _pretty_title((r["title"] or ""), (r["sport"] or sport))
        code = _short_code(r["title"] or "")
        time_short = st.split("•")[-1].strip() if "•" in st else st
        if league:
            blocks.append(f"━━━━━━━━━━━━━━━━\n<b>{title}</b>\n🏆 {league}\n🔖 {code} • {time_short}\n🕒 {st}")
        else:
            blocks.append(f"━━━━━━━━━━━━━━━━\n<b>{title}</b>\n🔖 {code} • {time_short}\n🕒 {st}")

    await cb.message.answer(
        f"{header}\n\n" + "\n\n".join(blocks),
        reply_markup=ikb_matches_list(sport, page, items, total),
    )
    await cb.answer()

@dp.callback_query(F.data == "back:sports")
async def cb_back_sports(cb: CallbackQuery):
    upsert_user_from_message(cb)
    sports = get_open_sports()
    if not sports:
        await cb.answer("Матчей нет.", show_alert=True)
        return
    await cb.message.answer("⬅️ Назад. Выбери спорт:", reply_markup=ikb_sports(sports))
    await cb.answer()

@dp.callback_query(F.data == "noop")
async def cb_noop(cb: CallbackQuery):
    await cb.answer()

@dp.message(F.text == BTN_TODAY)
async def match_of_day(m: Message):
    upsert_user_from_message(m)
    mid = pick_featured_for_today()
    if not mid:
        await m.answer("Сегодня нет матчей в базе 😕\nНажми «⚡ Активные матчи».", reply_markup=main_menu())
        return
    await show_match_card(m, mid)

@dp.callback_query(F.data.startswith("mopen:"))
async def cb_open_match(cb: CallbackQuery):
    upsert_user_from_message(cb)
    try:
        mid = int(cb.data.split(":")[1])
    except Exception:
        await cb.answer("Ошибка матча.", show_alert=True)
        return
    await show_match_card(cb, mid)
    await cb.answer()

async def show_match_card(target: Message | CallbackQuery, match_id: int) -> None:
    match = get_match(match_id)
    if not match:
        if isinstance(target, Message):
            await target.answer("Матч не найден.", reply_markup=main_menu())
        else:
            await target.message.answer("Матч не найден.", reply_markup=main_menu())
        return

    stats = match_stats(match_id)
    total_votes = stats["1"] + stats["X"] + stats["2"]

    def pct(n: int) -> str:
        if total_votes <= 0:
            return "0%"
        return f"{round((n / total_votes) * 100)}%"

    user_id = target.from_user.id if target.from_user else 0  # type: ignore
    my_pick = get_my_pick(user_id, match_id) if user_id else None
    odds = compute_live_odds(match_id) if BETTING_ENABLED else None
    bal = get_balance(user_id) if (BETTING_ENABLED and user_id) else None

    title = _pretty_title((match["title"] or ""), (match["sport"] or "other"))
    league = (match["league"] or "").strip()
    st = _pretty_time((match["start_time_utc"] or match["start_time"] or ""))
    code = _short_code(match["title"] or "")
    time_short = st.split("•")[-1].strip() if "•" in st else st

    dl = deadline_for_match(match)
    dl_text = _pretty_time(iso(dl)) if dl else "—"
    allowed, why = can_predict(match)

    sep = "━━━━━━━━━━━━━━━━"
    text_msg = (
        f"{sep}\n"
        f"<b>{title}</b>\n"
        f"🏆 {league or '—'}\n"
        f"🔖 {code} • {time_short}\n\n"
        f"🕒 Старт: <i>{st}</i>\n"
        f"⏳ Дедлайн: <i>{dl_text}</i>\n"
        f"{'✅ Ставки открыты' if allowed else '🔒 ' + why}\n\n"
        f"📊 <b>Прогнозы</b>:\n"
        f"1️⃣ {pct(stats['1'])} ({stats['1']})   🤝 {pct(stats['X'])} ({stats['X']})   2️⃣ {pct(stats['2'])} ({stats['2']})\n"
        f"🎯 Твой выбор: <b>{my_pick or '—'}</b>\n"
        + (f"💰 Баланс: <b>{bal}</b>\n" if bal is not None else "")
        + (f"📈 Коэф: 1=<b>{odds['1']}</b>  X=<b>{odds['X']}</b>  2=<b>{odds['2']}</b>\n" if odds else "")
        + f"{sep}\n\n"
        "Выбери исход 1X2:"
    )

    if isinstance(target, Message):
        await target.answer(text_msg, reply_markup=ikb_match_card(match_id))
    else:
        await target.message.answer(text_msg, reply_markup=ikb_match_card(match_id))

@dp.callback_query(F.data.startswith("stats:"))
async def cb_stats(cb: CallbackQuery):
    try:
        match_id = int(cb.data.split(":")[1])
    except Exception:
        await cb.answer("Ошибка.", show_alert=True)
        return
    s = match_stats(match_id)
    await cb.answer(f"1={s['1']}  X={s['X']}  2={s['2']}", show_alert=True)

    @dp.callback_query(F.data.startswith("pick:"))
    async def cb_pick(cb: CallbackQuery):
        upsert_user_from_message(cb)
        try:
            _, match_id_s, pick = cb.data.split(":")
            match_id = int(match_id_s)
        except Exception:
            await cb.answer("Ошибка.", show_alert=True)
            return
        if pick not in ("1", "X", "2"):
            await cb.answer("Неверный исход.", show_alert=True)
            return

        match = get_match(match_id)
        if not match:
            await cb.answer("Матч не найден.", show_alert=True)
            return

        ok, why = can_predict(match)
        if not ok:
            await cb.answer(why, show_alert=True)
            return

        if not BETTING_ENABLED:
            with db() as con:
                con.execute(
                    "INSERT OR REPLACE INTO votes(user_id, match_id, pick, created_at) VALUES(?,?,?,?)",
                    (cb.from_user.id, match_id, pick, iso(now_utc())),
                )
                con.commit()
            await cb.answer("✅ Принято!", show_alert=True)
            return

        # Betting flow: ask stake
        odds = compute_live_odds(match_id)
        set_pref(cb.from_user.id, awaiting_stake=True, bet_match_id=match_id, bet_pick=pick, bet_odds=odds.get(pick, 2.0))
        bal = get_balance(cb.from_user.id)
        await cb.message.answer(
            f"💸 Введи сумму ставки (целое число).\n"
            f"Текущий баланс: <b>{bal}</b>\n"
            f"Твой исход: <b>{pick}</b> | Коэффициент: <b>{odds.get(pick, 2.0)}</b>\n\n"
            f"Пример: <code>200</code>",
            reply_markup=main_menu(),
        )
        await cb.answer()

@dp.message(F.text == BTN_FIND_MATCH)
async def find_match(m: Message):
    upsert_user_from_message(m)
    if not m.from_user:
        return
    set_pref(m.from_user.id, awaiting_match_search=True)
    await m.answer(
        "🔎 Напиши команду/часть названия (пример: <code>arsenal</code> или <code>real</code>):",
        reply_markup=main_menu(),
    )


    @dp.message(lambda m: bool(getattr(m, "text", None)) and m.from_user and get_pref(m.from_user.id, "awaiting_stake", False))
    async def stake_amount_handler(m: Message):
        upsert_user_from_message(m)
        uid = m.from_user.id
        raw = (m.text or "").strip()

        # clear flag first to avoid trapping user
        clear_pref(uid, "awaiting_stake")

        match_id = int(get_pref(uid, "bet_match_id", 0) or 0)
        pick = str(get_pref(uid, "bet_pick", "") or "")
        odds = float(get_pref(uid, "bet_odds", 2.0) or 2.0)

        # cleanup
        clear_pref(uid, "bet_match_id")
        clear_pref(uid, "bet_pick")
        clear_pref(uid, "bet_odds")

        if not match_id or pick not in ("1", "X", "2"):
            await m.answer("Ставка отменена.", reply_markup=main_menu())
            return

        match = get_match(match_id)
        if not match:
            await m.answer("Матч не найден.", reply_markup=main_menu())
            return

        ok, why = can_predict(match)
        if not ok:
            await m.answer(why, reply_markup=main_menu())
            return

        try:
            stake = int(raw)
        except Exception:
            await m.answer("Нужно целое число (пример: 200).", reply_markup=main_menu())
            return

        if stake <= 0:
            await m.answer("Ставка должна быть > 0.", reply_markup=main_menu())
            return

        # handle overwrite: refund previous stake if exists
        with db() as con:
            cur = con.cursor()
            cur.execute("INSERT OR IGNORE INTO scores(user_id, updated_at, balance) VALUES(?,?,?)", (uid, iso(now_utc()), 0))
            prev = cur.execute("SELECT stake FROM votes WHERE user_id=? AND match_id=?", (uid, match_id)).fetchone()
            prev_stake = int(prev["stake"]) if prev and prev["stake"] is not None else 0

            bal = cur.execute("SELECT balance FROM scores WHERE user_id=?", (uid,)).fetchone()
            balance = int(bal["balance"]) if bal and bal["balance"] is not None else 0

            # refund previous stake (if any) before taking new stake
            balance += prev_stake

            if stake > balance:
                await m.answer(f"Недостаточно средств. Доступно: <b>{balance}</b>", reply_markup=main_menu())
                return

            balance -= stake

            cur.execute("UPDATE scores SET balance=?, updated_at=? WHERE user_id=?", (balance, iso(now_utc()), uid))
            cur.execute(
                "INSERT OR REPLACE INTO votes(user_id, match_id, pick, created_at, stake, odds) VALUES(?,?,?,?,?,?)",
                (uid, match_id, pick, iso(now_utc()), stake, odds),
            )
            con.commit()

        await m.answer(
            f"✅ Ставка принята!\n"
            f"Исход: <b>{pick}</b>\n"
            f"Сумма: <b>{stake}</b>\n"
            f"Коэффициент: <b>{odds}</b>\n"
            f"Баланс: <b>{balance}</b>",
            reply_markup=main_menu(),
        )

# IMPORTANT: This handler MUST NOT match every message, иначе ломает кнопки.
@dp.message(lambda m: bool(getattr(m, "text", None)) and m.from_user and get_pref(m.from_user.id, "awaiting_match_search", False))
async def catch_text(m: Message):
    # filter guarantees: text + from_user + awaiting flag
    q = (m.text or "").strip()
    clear_pref(m.from_user.id, "awaiting_match_search")

    if len(q) < 2:
        await m.answer("Слишком коротко. Попробуй 2+ символа.", reply_markup=main_menu())
        return

    ql = q.lower()
    with db() as con:
        rows = con.execute("""
            SELECT id, title, start_time_utc, start_time, league, sport
            FROM matches
            WHERE status='open' AND LOWER(title) LIKE ?
            ORDER BY COALESCE(start_time_utc, start_time) ASC
            LIMIT 20
        """, (f"%{ql}%",)).fetchall()

    if not rows:
        await m.answer("Ничего не нашёл 😕 Попробуй другое слово.", reply_markup=main_menu())
        return

    kb_rows: List[List[InlineKeyboardButton]] = []
    blocks: List[str] = [f"🔎 <b>Найдено: {len(rows)}</b>\n"]
    for r in rows:
        mid = int(r["id"])
        st = _pretty_time((r["start_time_utc"] or r["start_time"] or ""))
        title = _pretty_title((r["title"] or ""), (r["sport"] or "other"))
        league = (r["league"] or "").strip()
        code = _short_code(r["title"] or "")
        time_short = st.split("•")[-1].strip() if "•" in st else st
        if league:
            blocks.append(f"━━━━━━━━━━━━━━━━\n<b>{title}</b>\n🏆 {league}\n🔖 {code} • {time_short}\n🕒 {st}")
        else:
            blocks.append(f"━━━━━━━━━━━━━━━━\n<b>{title}</b>\n🔖 {code} • {time_short}\n🕒 {st}")
        kb_rows.append([InlineKeyboardButton(text=title[:48], callback_data=f"mopen:{mid}")])

    await m.answer("\n\n".join(blocks), reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))

@dp.message(F.text == BTN_MY)
async def my_predictions(m: Message):
    upsert_user_from_message(m)
    if not m.from_user:
        return
    with db() as con:
        rows = con.execute("""
            SELECT v.match_id, v.pick, v.created_at, m.title, m.status, m.result, m.sport
            FROM votes v
            JOIN matches m ON m.id = v.match_id
            WHERE v.user_id=?
            ORDER BY v.created_at DESC
            LIMIT 30
        """, (m.from_user.id,)).fetchall()

    if not rows:
        await m.answer("У тебя пока нет прогнозов.", reply_markup=main_menu())
        return

    lines = ["🧾 <b>Мои прогнозы</b> (последние 30)\n"]
    for r in rows:
        title = _pretty_title((r["title"] or ""), (r["sport"] or "other"))
        res = r["result"] or "—"
        lines.append(f"• <b>{title}</b> | pick=<b>{r['pick']}</b> | res=<b>{res}</b> | {r['status']}")
    await m.answer("\n".join(lines), reply_markup=main_menu())

def start_of_week_utc(dt: datetime) -> datetime:
    d = dt.astimezone(timezone.utc)
    return (d - timedelta(days=d.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)

def start_of_month_utc(dt: datetime) -> datetime:
    d = dt.astimezone(timezone.utc)
    return d.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

def top_points_since(since: datetime, limit: int = 10) -> List[Tuple[int, int]]:
    with db() as con:
        rows = con.execute("""
            SELECT user_id, COALESCE(SUM(points),0) AS pts
            FROM points_log
            WHERE created_at >= ?
            GROUP BY user_id
            ORDER BY pts DESC
            LIMIT ?
        """, (iso(since), limit)).fetchall()
    return [(int(r["user_id"]), int(r["pts"])) for r in rows]

def season_top(limit: int = 10) -> List[Tuple[int, int]]:
    with db() as con:
        rows = con.execute("""
            SELECT user_id, points
            FROM scores
            ORDER BY points DESC
            LIMIT ?
        """, (limit,)).fetchall()
    return [(int(r["user_id"]), int(r["points"])) for r in rows]

@dp.message(F.text == BTN_LB)
async def leaderboard(m: Message):
    upsert_user_from_message(m)
    week = start_of_week_utc(now_utc())
    month = start_of_month_utc(now_utc())
    top_season = season_top(10)
    top_w = top_points_since(week, 10)
    top_m = top_points_since(month, 10)

    def fmt(title: str, rows: List[Tuple[int, int]]) -> str:
        if not rows:
            return f"{title}\n<i>пусто</i>\n"
        out = [title]
        for i, (uid, pts) in enumerate(rows, 1):
            out.append(f"{i}. {pretty_user(uid)} — <b>{pts}</b>")
        return "\n".join(out) + "\n"

    text_msg = (
        "🏆 <b>Лидерборд</b>\n\n"
        + fmt("📅 Неделя:", top_w) + "\n"
        + fmt("🗓 Месяц:", top_m) + "\n"
        + fmt("🏅 Сезон:", top_season)
    )
    await m.answer(text_msg, reply_markup=main_menu())


def get_balance(user_id: int) -> int:
    s = get_score_row(user_id)
    try:
        return int(s["balance"])
    except Exception:
        return 0

def compute_live_odds(match_id: int) -> Dict[str, float]:
    """Simple crowd-based odds (no external odds feed).
    The more people pick an outcome, the lower the odds.
    """
    s = match_stats(match_id)
    total = s["1"] + s["X"] + s["2"]

    # small priors so odds exist even with 0 votes
    pri_1, pri_x, pri_2 = 2.0, 1.5, 2.0  # draw usually less likely
    c1 = s["1"] + 1
    cx = s["X"] + 1
    c2 = s["2"] + 1
    denom = (c1 + cx + c2)
    p1 = c1 / denom
    px = cx / denom
    p2 = c2 / denom

    def odds_from_p(p: float, margin: float = 0.08) -> float:
        p = max(0.05, min(0.95, p))
        o = (1.0 / p) * (1.0 - margin)
        return max(1.15, round(o, 2))

    o1 = odds_from_p(p1)
    ox = odds_from_p(px)
    o2 = odds_from_p(p2)
    return {"1": o1, "X": ox, "2": o2}
def get_score_row(user_id: int) -> sqlite3.Row:
    with db() as con:
        r = con.execute("SELECT * FROM scores WHERE user_id=?", (user_id,)).fetchone()
        if not r:
            con.execute("INSERT OR IGNORE INTO scores(user_id, updated_at) VALUES(?,?)", (user_id, iso(now_utc())))
            con.commit()
            r = con.execute("SELECT * FROM scores WHERE user_id=?", (user_id,)).fetchone()
    return r

@dp.message(F.text == BTN_PROFILE)
async def profile(m: Message):
    upsert_user_from_message(m)
    if not m.from_user:
        return
    s = get_score_row(m.from_user.id)
    await m.answer(
        (
            "👤 <b>Профиль</b>\n\n"
            f"Игрок: {pretty_user(m.from_user.id)}\n"
            f"Очки: <b>{int(s['points'])}</b>\n"
            + (f"Баланс: <b>{int(s['balance'])}</b>\n" if BETTING_ENABLED else "")
            + f"Победы: <b>{int(s['correct'])}</b> / Игр: <b>{int(s['total'])}</b>\n"
            + f"Серия: <b>{int(s['streak'])}</b> (лучшая {int(s['best_streak'])})\n"
        ),
        reply_markup=main_menu(),
    )
# =========================
# MAIN
# =========================

async def main():
    init_db()

    # For Render Web Service: keep port open (health endpoint)
    asyncio.create_task(start_web_server())

    # Keep-alive ping (only if KEEP_ALIVE_URL is set)
    asyncio.create_task(keep_alive_loop())

    # Weekly bonus (Monday 12:00 MSK)
    asyncio.create_task(weekly_bonus_loop())

    if SYNC_ENABLED:
        asyncio.create_task(autosync_loop())
    if AUTO_RESULTS_ENABLED:
        asyncio.create_task(auto_results_loop())

    # Restart polling if it crashes (common on flaky hosting)
    while True:
        try:
            await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
            break
        except Exception as e:
            logger.exception("Polling crashed, restarting in 5 seconds: %s", e)
            await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(main())
# =========================
# REAL BOOKMAKER ODDS (The Odds API) - optional
# =========================

ODDS_API_BASE = "https://api.the-odds-api.com/v4"

def _split_teams(title: str) -> tuple[str, str]:
    # common formats: "A vs B", "A - B", "A — B"
    for sep in (" vs ", " - ", " — "):
        if sep in title:
            a, b = title.split(sep, 1)
            return a.strip(), b.strip()
    return title.strip(), ""

def _norm_name(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"[\(\)\[\]\{\}\.,:;!\?\+\-_/\\]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def _teams_match(home: str, away: str, ev_home: str, ev_away: str) -> bool:
    h, a = _norm_name(home), _norm_name(away)
    eh, ea = _norm_name(ev_home), _norm_name(ev_away)
    # try both orientations
    ok1 = (h in eh or eh in h) and (a in ea or ea in a)
    ok2 = (h in ea or ea in h) and (a in eh or eh in a)
    return ok1 or ok2

async def _theoddsapi_list_odds(sport_key: str) -> list[dict]:
    # https://api.the-odds-api.com/v4/sports/{sport}/odds
    params = {
        "apiKey": ODDS_API_KEY,
        "regions": ODDS_REGION,
        "markets": ODDS_MARKET,  # h2h
        "oddsFormat": "decimal",
        "dateFormat": "iso",
    }
    url = f"{ODDS_API_BASE}/sports/{sport_key}/odds"
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
        async with session.get(url, params=params) as r:
            if r.status != 200:
                try:
                    body = await r.text()
                except Exception:
                    body = ""
                logger.warning("Odds API status=%s sport=%s body=%s", r.status, sport_key, body[:200])
                return []
            return await r.json()

def _pick_bookmaker(bookmakers: list[dict]) -> dict | None:
    if not bookmakers:
        return None
    if ODDS_BOOKMAKER:
        target = ODDS_BOOKMAKER.lower()
        for b in bookmakers:
            if (b.get("key") or "").lower() == target or (b.get("title") or "").lower() == target:
                return b
    return bookmakers[0]

def _extract_h2h_odds(event: dict) -> dict[str, float] | None:
    # Returns {"1": home, "2": away, "X": draw?} when possible.
    home_team = event.get("home_team") or ""
    away_team = event.get("away_team") or ""
    bookmakers = event.get("bookmakers") or []
    book = _pick_bookmaker(bookmakers)
    if not book:
        return None

    markets = book.get("markets") or []
    h2h = None
    for m in markets:
        if (m.get("key") or "") == "h2h":
            h2h = m
            break
    if not h2h:
        return None

    out = {"1": None, "X": None, "2": None}
    for o in (h2h.get("outcomes") or []):
        name = (o.get("name") or "").strip()
        price = o.get("price")
        if price is None:
            continue
        n = _norm_name(name)
        if n in (_norm_name(home_team), _norm_name(event.get("home_team") or "")):
            out["1"] = float(price)
        elif n in (_norm_name(away_team), _norm_name(event.get("away_team") or "")):
            out["2"] = float(price)
        elif n in ("draw", "tie", "the draw"):
            out["X"] = float(price)

    # Some sports return 2-way odds (no draw). We'll still store 1/2 if available.
    if out["1"] and out["2"]:
        res = {"1": round(float(out["1"]), 2), "2": round(float(out["2"]), 2)}
        if out["X"]:
            res["X"] = round(float(out["X"]), 2)
        return res
    return None

async def refresh_real_odds_once():
    if not (ODDS_PROVIDER == "theoddsapi" and ODDS_API_KEY):
        return

    sport_keys = get_odds_sport_keys()
    if not sport_keys:
        return

    # Load upcoming open matches (next 72h) for sports we support
    nowu = now_utc()
    horizon = nowu + timedelta(hours=72)
    with db() as con:
        matches = con.execute(
            "SELECT id, title, sport, start_time FROM matches "
            "WHERE status='open' AND start_time IS NOT NULL "
            "ORDER BY start_time ASC LIMIT 200"
        ).fetchall()

    # Cache events per sport_key (reduce API calls)
    events_by_key: dict[str, list[dict]] = {}
    for sk in sport_keys:
        events_by_key[sk] = await _theoddsapi_list_odds(sk)

    for m in matches:
        mid = int(m["id"])
        title = (m["title"] or "").strip()
        sport = (m["sport"] or "").strip().lower()
        start_time = (m["start_time"] or "").strip()

        if not title or not start_time:
            continue

        try:
            st = datetime.fromisoformat(start_time.replace("Z", "+00:00")).astimezone(timezone.utc)
        except Exception:
            continue

        if st < (nowu - timedelta(hours=1)) or st > horizon:
            continue

        home, away = _split_teams(title)
        if not home or not away:
            continue

        # Choose which OddsAPI sport key to match against:
        # - football -> try "soccer" first if present
        # - nhl -> try "icehockey_nhl" if present
        preferred = []
        if sport in ("football", "soccer"):
            preferred = ["soccer"]
        elif sport in ("nhl", "hockey"):
            preferred = ["icehockey_nhl"]
        # fall back to all configured
        keys_to_try = [k for k in preferred if k in events_by_key] + [k for k in sport_keys if k not in preferred]

        best_odds = None
        best_diff = None

        for sk in keys_to_try:
            for ev in events_by_key.get(sk, []):
                ev_home = ev.get("home_team") or ""
                ev_away = ev.get("away_team") or ""
                if not _teams_match(home, away, ev_home, ev_away):
                    continue
                try:
                    ev_t = datetime.fromisoformat((ev.get("commence_time") or "").replace("Z", "+00:00")).astimezone(timezone.utc)
                except Exception:
                    continue
                diff = abs((ev_t - st).total_seconds())
                if diff > 6 * 3600:
                    continue
                odds = _extract_h2h_odds(ev)
                if not odds:
                    continue
                if best_diff is None or diff < best_diff:
                    best_diff = diff
                    best_odds = odds

        if not best_odds:
            continue

        o1 = best_odds.get("1")
        ox = best_odds.get("X")
        o2 = best_odds.get("2")

        with db() as con:
            con.execute(
                "UPDATE matches SET odds_1=?, odds_x=?, odds_2=? WHERE id=?",
                (o1, ox, o2, mid),
            )
            con.commit()

async def refresh_real_odds_loop():
    # background refresh loop
    if not (ODDS_PROVIDER == "theoddsapi" and ODDS_API_KEY):
        return
    while True:
        try:
            await refresh_real_odds_once()
            await asyncio.sleep(max(60, ODDS_REFRESH_INTERVAL))
        except Exception as e:
            logger.exception("refresh_real_odds_loop error: %s", e)
            await asyncio.sleep(120)


