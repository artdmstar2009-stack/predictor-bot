# -*- coding: utf-8 -*-
"""
Predictor Bot (aiogram v3.7+)
============================
Версия: sporty UI, без комментариев, без показа ID матчей в тексте.

✅ UI
- Красивое меню
- ⚡ Активные матчи -> выбор спорта -> матчи (пагинация) -> карточка матча
- 🔥 Матч дня

✅ Функции
- 1X2 исходы
- Автосинк (football-data.org + NHL)
- Авто-итоги (начисление очков)
- Лидерборд (неделя/месяц/сезон)
- Профиль

✅ Стабильность
- Polling auto-restart
- HTTP health server для Render Web Service (0.0.0.0:$PORT)

ENV
---
Required:
- BOT_TOKEN

Recommended:
- ADMIN_ID
- PORT (Render ставит сам)

Autosync:
- SYNC_ENABLED=1
- SYNC_INTERVAL=3600
- SYNC_LOOKAHEAD_DAYS=1

Football:
- FOOTBALL_ENABLED=1
- FOOTBALL_DATA_TOKEN=...
- FOOTBALL_COMPETITIONS=PL,CL,PD,SA,BL1,FL1

NHL:
- NHL_ENABLED=1

Auto-results:
- AUTO_RESULTS_ENABLED=1
- AUTO_RESULTS_INTERVAL=300
- AUTO_RESULTS_MIN_AGE_MIN=20

Scoring:
- POINTS_FOR_CORRECT=3
- POINTS_FOR_WRONG=0
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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

# ============================================================
# CONFIG
# ============================================================

BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or "0")
DB_PATH = os.getenv("DB_PATH", "bot.db")

PORT = int(os.getenv("PORT", "0") or "0")

# deadline (minutes before start)
PREDICT_DEADLINE_MIN = int(os.getenv("PREDICT_DEADLINE_MIN", "5") or "5")

# autosync
SYNC_ENABLED = os.getenv("SYNC_ENABLED", "1") == "1"
SYNC_INTERVAL = int(os.getenv("SYNC_INTERVAL", "3600") or "3600")
SYNC_LOOKAHEAD_DAYS = int(os.getenv("SYNC_LOOKAHEAD_DAYS", "1") or "1")

# football-data
FOOTBALL_ENABLED = os.getenv("FOOTBALL_ENABLED", "1") == "1"
FOOTBALL_DATA_TOKEN = (os.getenv("FOOTBALL_DATA_TOKEN") or "").strip()
FOOTBALL_COMPETITIONS = [c.strip() for c in (os.getenv("FOOTBALL_COMPETITIONS") or "PL,CL,PD,SA,BL1,FL1").split(",") if c.strip()]
FOOTBALL_BASE = (os.getenv("FOOTBALL_BASE") or "https://api.football-data.org/v4").rstrip("/")

# NHL
NHL_ENABLED = os.getenv("NHL_ENABLED", "1") == "1"

# auto-results
AUTO_RESULTS_ENABLED = os.getenv("AUTO_RESULTS_ENABLED", "1") == "1"
AUTO_RESULTS_INTERVAL = int(os.getenv("AUTO_RESULTS_INTERVAL", "300") or "300")
AUTO_RESULTS_MIN_AGE_MIN = int(os.getenv("AUTO_RESULTS_MIN_AGE_MIN", "20") or "20")

# scoring
POINTS_FOR_CORRECT = int(os.getenv("POINTS_FOR_CORRECT", "3") or "3")
POINTS_FOR_WRONG = int(os.getenv("POINTS_FOR_WRONG", "0") or "0")

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL)
logger = logging.getLogger("predictor_bot")

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# ============================================================
# IN-MEMORY STATE (for simple flows)
# ============================================================

STATE: Dict[int, Dict[str, Any]] = {}

def set_pref(user_id: int, **data: Any) -> None:
    cur = STATE.get(user_id, {})
    cur.update(data)
    STATE[user_id] = cur

def get_pref(user_id: int, key: str, default: Any = None) -> Any:
    return STATE.get(user_id, {}).get(key, default)

def clear_pref(user_id: int, key: str) -> None:
    if user_id in STATE and key in STATE[user_id]:
        del STATE[user_id][key]

# ============================================================
# DB + utils
# ============================================================

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

# ============================================================
# UI
# ============================================================

BTN_ACTIVE = "⚡ Активные матчи"
BTN_TODAY = "🔥 Матч дня"
BTN_MY = "🧾 Мои прогнозы"
BTN_LB = "🏆 Лидерборд"
BTN_PROFILE = "👤 Профиль"
BTN_HELP = "ℹ️ Помощь"
BTN_FIND_MATCH = "🔎 Найти матч"

SPORT_PRETTY = {
    "football": "⚽ Футбол",
    "hockey": "🏒 Хоккей",
    "nhl": "🏒 Хоккей",
    "esports": "🎮 Киберспорт",
    "other": "🏟 Другое",
}

PER_PAGE = 10

# ---------- Pretty formatting (sporty) ----------
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

def _pretty_title(title: str, sport: str) -> str:
    t = (title or "").strip()
    t = re.sub(r"\s+vs\.?\s+", " 🆚 ", t, flags=re.I)
    t = re.sub(r"\s+-\s+", " 🆚 ", t)
    if "🆚" in t:
        a, b = [p.strip() for p in t.split("🆚", 1)]
        if a and b:
            t = f"{a.upper()} 🆚 {b.upper()}"
    return f"{_sport_emoji(sport)} {t}"

def ikb_day_filter(current: str = 'all') -> InlineKeyboardMarkup:
    cur = (current or 'all').lower()
    def mark(lbl: str, key: str) -> str:
        return f"✅ {lbl}" if cur == key else lbl
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=mark('📅 Сегодня', 'today'), callback_data='day:today'),
        InlineKeyboardButton(text=mark('📅 Завтра', 'tomorrow'), callback_data='day:tomorrow'),
        InlineKeyboardButton(text=mark('🗂 Все даты', 'all'), callback_data='day:all'),
    ]])

def main_menu() -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text=BTN_ACTIVE), KeyboardButton(text=BTN_TODAY)],
        [KeyboardButton(text=BTN_FIND_MATCH), KeyboardButton(text=BTN_MY)],
        [KeyboardButton(text=BTN_LB), KeyboardButton(text=BTN_PROFILE)],
        [KeyboardButton(text=BTN_HELP)],
    ]
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)
(keyboard=rows, resize_keyboard=True)

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
        if len(title) > 40:
            title = title[:40] + "…"
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
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="1", callback_data=f"pick:{match_id}:1"),
            InlineKeyboardButton(text="X", callback_data=f"pick:{match_id}:X"),
            InlineKeyboardButton(text="2", callback_data=f"pick:{match_id}:2"),
        ],
        [InlineKeyboardButton(text="📊 Голоса", callback_data=f"stats:{match_id}")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back:sports")],
    ])

# ============================================================
# QUERIES
# ============================================================

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

def count_open_matches(sport: str, day_filter: str = 'all') -> int:
    with db() as con:
        df = (day_filter or 'all').lower()
        start = end = None
        if df in ('today','tomorrow'):
            base = now_utc().replace(hour=0, minute=0, second=0, microsecond=0)
            if df == 'tomorrow':
                base = base + timedelta(days=1)
            start = iso(base)
            end = iso(base + timedelta(days=1))
        if sport == "all":
            if start and end:
                r = con.execute("""
                    SELECT COUNT(*) c FROM matches
                    WHERE status='open'
                      AND COALESCE(start_time_utc, start_time) >= ?
                      AND COALESCE(start_time_utc, start_time) < ?
                """, (start, end)).fetchone()
            else:
                r = con.execute("SELECT COUNT(*) c FROM matches WHERE status='open'").fetchone()
        else:
            if start and end:
                r = con.execute("""
                    SELECT COUNT(*) c FROM matches
                    WHERE status='open'
                      AND COALESCE(NULLIF(LOWER(sport), ''), 'other')=?
                      AND COALESCE(start_time_utc, start_time) >= ?
                      AND COALESCE(start_time_utc, start_time) < ?
                """, (sport, start, end)).fetchone()
            else:
                r = con.execute("""
                    SELECT COUNT(*) c FROM matches
                    WHERE status='open' AND COALESCE(NULLIF(LOWER(sport), ''), 'other')=?
                """, (sport,)).fetchone()
    return int(r["c"]) if r else 0

def get_open_matches_page(sport: str, page: int, day_filter: str = 'all') -> List[sqlite3.Row]:
    offset = max(0, page) * PER_PAGE
    df = (day_filter or 'all').lower()
    start = end = None
    if df in ('today','tomorrow'):
        base = now_utc().replace(hour=0, minute=0, second=0, microsecond=0)
        if df == 'tomorrow':
            base = base + timedelta(days=1)
        start = iso(base)
        end = iso(base + timedelta(days=1))
    with db() as con:
        if sport == "all":
            if start and end:
                return con.execute("""
                    SELECT id, title, start_time_utc, league, sport, start_time
                    FROM matches
                    WHERE status='open'
                      AND COALESCE(start_time_utc, start_time) >= ?
                      AND COALESCE(start_time_utc, start_time) < ?
                    ORDER BY COALESCE(start_time_utc, start_time) ASC
                    LIMIT ? OFFSET ?
                """, (start, end, PER_PAGE, offset)).fetchall()
            return con.execute("""
                SELECT id, title, start_time_utc, league, sport, start_time
                FROM matches
                WHERE status='open'
                ORDER BY COALESCE(start_time_utc, start_time) ASC
                LIMIT ? OFFSET ?
            """, (PER_PAGE, offset)).fetchall()
        if start and end:
            return con.execute("""
                SELECT id, title, start_time_utc, league, sport, start_time
                FROM matches
                WHERE status='open' AND COALESCE(NULLIF(LOWER(sport), ''), 'other')=?
                  AND COALESCE(start_time_utc, start_time) >= ?
                  AND COALESCE(start_time_utc, start_time) < ?
                ORDER BY COALESCE(start_time_utc, start_time) ASC
                LIMIT ? OFFSET ?
            """, (sport, start, end, PER_PAGE, offset)).fetchall()
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

# ============================================================
# AUTOSYNC MODELS + API
# ============================================================

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
    url = f"{FOOTBALL_BASE}/matches/{external_id}"
    data = await http_json(session, url, headers=headers)
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

        games = []
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
            title = f"{home} vs {away}"
            out.append(SyncedMatch("nhl", gid, "hockey", "NHL", title, start))
        day = day + timedelta(days=1)
    return out

async def nhl_result(session: aiohttp.ClientSession, external_id: str) -> Optional[FinishedInfo]:
    gid = external_id
    try:
        data = await http_json(session, f"https://api-web.nhle.com/v1/gamecenter/{gid}/landing")
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
    text = "Sync: " + " | ".join(report) if report else "Sync: nothing"
    logger.info(text)
    return text

# ============================================================
# FEATURED MATCH OF THE DAY
# ============================================================

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
            SELECT id
            FROM matches
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

# ============================================================
# WEB SERVER (Render Web Service)
# ============================================================

async def start_web_server():
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

# ============================================================
# HANDLERS
# ============================================================

@dp.message(Command("start"))
async def cmd_start(m: Message):
    upsert_user_from_message(m)
    text_msg = (
        "👋 <b>Привет!</b>

"
        "Это бот прогнозов <b>1X2</b>.
"
        "Жми <b>⚡ Активные матчи</b> → выбери спорт → матч → исход.

"
        f"⏱ Дедлайн прогнозов: за <b>{PREDICT_DEADLINE_MIN}</b> мин до старта."
    )
    await m.answer(text_msg, reply_markup=main_menu())

@dp.message(Command("help"))
@dp.message(F.text == BTN_HELP)
async def help_btn(m: Message):
    upsert_user_from_message(m)
    await m.answer(
        "ℹ️ <b>Помощь</b>\n\n"
        "• ⚡ Активные матчи → спорт → матч → 1/X/2\n"
        "• 🔥 Матч дня — быстрый доступ к матчу сегодня\n"
        "• 🧾 Мои прогнозы — твои ставки\n"
        "• 🏆 Лидерборд — топ игроков\n\n"
        f"Очки за верный исход: <b>+{POINTS_FOR_CORRECT}</b>",
        reply_markup=main_menu(),
    )

@dp.message(Command("sync_now"))
async def sync_cmd(m: Message):
    upsert_user_from_message(m)
    if not is_admin(m.from_user.id):
        return
    msg = await autosync_once()
    pick_featured_for_today()
    await m.answer(f"✅ {msg}", reply_markup=main_menu())

@dp.message(F.text == BTN_ACTIVE)
async def active_matches(m: Message):
    upsert_user_from_message(m)
    set_pref(m.from_user.id, day_filter=get_pref(m.from_user.id, 'day_filter', 'all'))
    sports = get_open_sports()
    if not sports:
        await m.answer("Пока нет активных матчей.", reply_markup=main_menu())
        return
    await m.answer("⚡ <b>Активные матчи</b>

Выбери фильтр по дате 👇", reply_markup=main_menu())
    await m.answer("Фильтр:", reply_markup=ikb_day_filter(get_pref(m.from_user.id,'day_filter','all')))
    await m.answer("Теперь выбери вид спорта 👇", reply_markup=ikb_sports(sports))

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
    df = get_pref(cb.from_user.id, 'day_filter', 'all')
    total = count_open_matches(sport, df)
    if total <= 0:
        await cb.answer("В этой категории матчей нет.", show_alert=True)
        return
    max_page = max(0, (total - 1) // PER_PAGE)
    page = min(max(page, 0), max_page)

    items = get_open_matches_page(sport, page, df)
    header = "📋 Все матчи" if sport == "all" else SPORT_PRETTY.get(sport, f"🏟 {sport}")
    df_label = {'all':'🗂 Все даты','today':'📅 Сегодня','tomorrow':'📅 Завтра'}.get(df,'🗂 Все даты')

    blocks: List[str] = []
    for r in items:
        st = _pretty_time((r["start_time_utc"] or r["start_time"] or ""))
        league = (r["league"] or "").strip()
        title = _pretty_title((r["title"] or ""), (r["sport"] or sport))
        code = _short_code(r["title"] or "")
        time_short = st.split('•')[-1].strip() if '•' in st else st
        if league:
            blocks.append(f"━━━━━━━━━━━━━━━━\n<b>{title}</b>\n🏆 {league}\n🔖 {code} • {time_short}\n🕒 {st}")
        else:
            blocks.append(f"━━━━━━━━━━━━━━━━\n<b>{title}</b>\n🔖 {code} • {time_short}\n🕒 {st}")

    await cb.message.answer(f"{header}  •  {df_label}\n\n" + "\n\n".join(blocks), reply_markup=ikb_matches_list(sport, page, items, total))
    await cb.answer()

@dp.callback_query(F.data == "back:sports")
async def cb_back_sports(cb: CallbackQuery):
    upsert_user_from_message(cb)
    sports = get_open_sports()
    if not sports:
        await cb.answer("Матчей нет.", show_alert=True)
        return
    await cb.message.answer("⬅️ Назад. Фильтр по дате:", reply_markup=ikb_day_filter(get_pref(cb.from_user.id,'day_filter','all')))
    await cb.message.answer("Выбери спорт:", reply_markup=ikb_sports(sports))
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
        return f"{round((n/total_votes)*100)}%"

    user_id = target.from_user.id if target.from_user else 0  # type: ignore
    my_pick = get_my_pick(user_id, match_id) if user_id else None

    title = _pretty_title((match["title"] or ""), (match["sport"] or "other"))
    league = (match["league"] or "").strip()
    st = _pretty_time((match["start_time_utc"] or match["start_time"] or ""))
    code = _short_code(match["title"] or "")
    time_short = st.split('•')[-1].strip() if '•' in st else st

    sep = "━━━━━━━━━━━━━━━━"
    text = (
    dl = deadline_for_match(match)
    dl_text = _pretty_time(iso(dl)) if dl else "—"
    allowed, why = can_predict(match)

        f"{sep}\n"
        f"<b>{title}</b>\n"
        f"🏆 {league or '—'}\n"
        f"🔖 {code} • {time_short}\n\n"
        f"🕒 Старт: <i>{st}</i>\n\n"
        f"⏳ Дедлайн: <i>{dl_text}</i>
"
        f"{'✅ Ставки открыты' if allowed else '🔒 ' + why}

"
        f"📊 <b>Прогнозы</b>:\n"
        f"1️⃣ {pct(stats['1'])} ({stats['1']})   🤝 {pct(stats['X'])} ({stats['X']})   2️⃣ {pct(stats['2'])} ({stats['2']})\n"
        f"🎯 Твой выбор: <b>{my_pick or '—'}</b>\n"
        f"{sep}\n\n"
        "Выбери исход 1X2:"
    )

    if isinstance(target, Message):
        await target.answer(text, reply_markup=ikb_match_card(match_id))
    else:
        await target.message.answer(text, reply_markup=ikb_match_card(match_id))

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

    with db() as con:
        con.execute(
            "INSERT OR REPLACE INTO votes(user_id, match_id, pick, created_at) VALUES(?,?,?,?)",
            (cb.from_user.id, match_id, pick, iso(now_utc())),
        )
        con.commit()

    await cb.answer("✅ Принято!", show_alert=True)


@dp.message(F.text == BTN_FIND_MATCH)
async def find_match(m: Message):
    upsert_user_from_message(m)
    set_pref(m.from_user.id, awaiting_match_search=True)
    await m.answer("🔎 Напиши команду/часть названия (пример: <code>arsenal</code> или <code>real</code>):", reply_markup=main_menu())

@dp.message()
async def catch_text(m: Message):
    # lightweight router for search input
    if not m.text or not m.from_user:
        return
    if not get_pref(m.from_user.id, 'awaiting_match_search', False):
        return
    q = m.text.strip()
    clear_pref(m.from_user.id, 'awaiting_match_search')
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
    kb_rows = []
    blocks = [f"🔎 <b>Найдено: {len(rows)}</b>\n"]
    for r in rows:
        mid = int(r['id'])
        st = _pretty_time((r['start_time_utc'] or r['start_time'] or ''))
        title = _pretty_title((r['title'] or ''), (r['sport'] or 'other'))
        league = (r['league'] or '').strip()
        code = _short_code(r['title'] or '')
        time_short = st.split('•')[-1].strip() if '•' in st else st
        if league:
            blocks.append(f"━━━━━━━━━━━━━━━━\n<b>{title}</b>\n🏆 {league}\n🔖 {code} • {time_short}\n🕒 {st}")
        else:
            blocks.append(f"━━━━━━━━━━━━━━━━\n<b>{title}</b>\n🔖 {code} • {time_short}\n🕒 {st}")
        kb_rows.append([InlineKeyboardButton(text=title[:48], callback_data=f"mopen:{mid}")])
    await m.answer("\n\n".join(blocks), reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))

@dp.message(F.text == BTN_MY)
async def my_predictions(m: Message):
    upsert_user_from_message(m)
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

    text = (
        "🏆 <b>Лидерборд</b>\n\n"
        + fmt("📅 Неделя:", top_w) + "\n"
        + fmt("🗓 Месяц:", top_m) + "\n"
        + fmt("🏅 Сезон:", top_season)
    )
    await m.answer(text, reply_markup=main_menu())

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
    s = get_score_row(m.from_user.id)
    text = (
        f"👤 <b>Профиль</b>\n\n"
        f"Игрок: {pretty_user(m.from_user.id)}\n"
        f"Очки: <b>{int(s['points'])}</b>\n"
        f"Победы: <b>{int(s['correct'])}</b> / Игр: <b>{int(s['total'])}</b>\n"
        f"Серия: <b>{int(s['streak'])}</b> (лучшая {int(s['best_streak'])})\n"
    )
    await m.answer(text, reply_markup=main_menu())

# ============================================================
# BACKGROUND LOOPS
# ============================================================

async def autosync_loop():
    while True:
        try:
            if SYNC_ENABLED:
                await autosync_once()
                pick_featured_for_today()
        except Exception as e:
            logger.exception("autosync_loop error: %s", e)
        await asyncio.sleep(max(300, SYNC_INTERVAL))

async def apply_scoring_for_match(match_id: int, result_1x2: str) -> None:
    with db() as con:
        cur = con.cursor()
        st = cur.execute("SELECT status FROM matches WHERE id=?", (match_id,)).fetchone()
        if not st or st["status"] != "open":
            return

        cur.execute("UPDATE matches SET result=?, status='closed' WHERE id=?", (result_1x2, match_id))

        votes = cur.execute("SELECT user_id, pick FROM votes WHERE match_id=?", (match_id,)).fetchall()
        for v in votes:
            uid = int(v["user_id"])
            pick = v["pick"]
            correct = (pick == result_1x2)
            delta = POINTS_FOR_CORRECT if correct else POINTS_FOR_WRONG

            cur.execute("INSERT OR IGNORE INTO scores(user_id, updated_at) VALUES(?,?)", (uid, iso(now_utc())))

            if correct:
                cur.execute("""
                    UPDATE scores
                    SET points = points + ?,
                        correct = correct + 1,
                        total = total + 1,
                        streak = streak + 1,
                        best_streak = CASE WHEN streak + 1 > best_streak THEN streak + 1 ELSE best_streak END,
                        updated_at = ?
                    WHERE user_id=?
                """, (delta, iso(now_utc()), uid))
            else:
                cur.execute("""
                    UPDATE scores
                    SET points = points + ?,
                        total = total + 1,
                        streak = 0,
                        updated_at = ?
                    WHERE user_id=?
                """, (delta, iso(now_utc()), uid))

            if delta != 0:
                cur.execute("INSERT INTO points_log(user_id, match_id, points, created_at) VALUES(?,?,?,?)",
                            (uid, match_id, delta, iso(now_utc())))

        con.commit()

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
                            await bot.send_message(ADMIN_ID, f"✅ Итог проставлен: матч закрыт ({fin.result_1x2})")
                        except Exception:
                            pass

            except Exception as e:
                logger.exception("auto_results_loop error: %s", e)

async def heartbeat_loop():
    while True:
        logger.info("heartbeat: alive")
        await asyncio.sleep(900)

# ============================================================
# MAIN
# ============================================================

async def main():
    init_db()

    asyncio.create_task(start_web_server())
    asyncio.create_task(heartbeat_loop())

    if SYNC_ENABLED:
        asyncio.create_task(autosync_loop())
    if AUTO_RESULTS_ENABLED:
        asyncio.create_task(auto_results_loop())

    while True:
        try:
            await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
            break
        except Exception as e:
            logger.exception("Polling crashed, restarting in 5 seconds: %s", e)
            await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(main())

@dp.callback_query(F.data.startswith("day:"))
async def cb_day(cb: CallbackQuery):
    upsert_user_from_message(cb)
    try:
        df = cb.data.split(":", 1)[1].strip().lower()
    except Exception:
        df = 'all'
    if df not in ('all','today','tomorrow'):
        df = 'all'
    set_pref(cb.from_user.id, day_filter=df)
    sports = get_open_sports()
    await cb.message.answer("✅ Фильтр установлен. Выбери спорт:", reply_markup=ikb_sports(sports))
    await cb.answer()
