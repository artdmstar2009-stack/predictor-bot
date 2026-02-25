# -*- coding: utf-8 -*-
"""
Ultra-upgrade Predictor Bot (aiogram v3.7+)
===========================================
Пакет апгрейдов "всё сразу" (насколько реально и без ломания основы):

✅ UI / презентабельность
- Красивое меню
- ⚡ Активные матчи -> выбор спорта -> матчи с пагинацией -> карточка матча
- 🔥 Матч дня (выбор автоматически на сегодня)

✅ Игровые механики
- Дедлайн прогнозов (по умолчанию за 5 минут до старта)
- Очки + журнал начислений (points_log) -> рейтинги за неделю/месяц/сезон
- Ачивки (простые, но рабочие): FIRST_WIN, STREAK_3, STREAK_5, WINS_10, WINS_50
- Стрики (поддерживаются через таблицу scores)

✅ Социалка
- Поиск игрока (по @username) + карточка профиля
- Комнаты/лиги друзей (создать, вступить по коду, топ внутри комнаты)
- Мини-комментарии под матчем (последние 5 комментариев)

✅ Автосинк
- football-data.org (токен нужен)
- NHL schedule (без токена)

✅ Авто-итоги
- Бот сам подтягивает результат и начисляет очки

✅ Стабильность
- polling auto-restart (если Telegram/сеть падает)
- для Render Web Service: мини HTTP сервер на 0.0.0.0:$PORT ("/" и "/health" -> ok)

ENV (Render)
------------
Required:
- BOT_TOKEN

Recommended:
- ADMIN_ID

Render Web Service:
- PORT  (Render ставит сам)

Deadlines:
- PREDICT_DEADLINE_MIN=5

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

Notifications (daily digest):
- DIGEST_ENABLED=1
- DIGEST_HOUR_UTC=12
- DIGEST_MINUTE_UTC=0

Scoring:
- POINTS_FOR_CORRECT=3
"""

from __future__ import annotations

import asyncio
import logging
import os
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

# deadlines
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

# daily digest notifications
DIGEST_ENABLED = os.getenv("DIGEST_ENABLED", "1") == "1"
DIGEST_HOUR_UTC = int(os.getenv("DIGEST_HOUR_UTC", "12") or "12")
DIGEST_MINUTE_UTC = int(os.getenv("DIGEST_MINUTE_UTC", "0") or "0")

# scoring
POINTS_FOR_CORRECT = int(os.getenv("POINTS_FOR_CORRECT", "3") or "3")
POINTS_FOR_WRONG = int(os.getenv("POINTS_FOR_WRONG", "0") or "0")

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL)
logger = logging.getLogger("predictor_bot")

bot = Bot(
    BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
dp = Dispatcher()

# ============================================================
# BASIC STATE (in-memory)
# ============================================================

STATE: Dict[int, Dict[str, Any]] = {}

def set_state(user_id: int, name: str, **data: Any) -> None:
    STATE[user_id] = {"name": name, "data": data, "ts": datetime.now().timestamp()}

def get_state(user_id: int) -> Tuple[str, Dict[str, Any]]:
    s = STATE.get(user_id)
    if not s:
        return "", {}
    return s.get("name", ""), s.get("data", {}) or {}

def clear_state(user_id: int) -> None:
    STATE.pop(user_id, None)

# ============================================================
# DB
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

        # users
        cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            updated_at TEXT
        )
        """)

        # matches (legacy compatible) + new columns
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

        # votes
        cur.execute("""
        CREATE TABLE IF NOT EXISTS votes (
            user_id INTEGER,
            match_id INTEGER,
            pick TEXT,
            created_at TEXT,
            UNIQUE(user_id, match_id)
        )
        """)

        # scores with streak
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

        # points log (for weekly/monthly/season)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS points_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            match_id INTEGER,
            points INTEGER,
            created_at TEXT
        )
        """)

        # achievements
        cur.execute("""
        CREATE TABLE IF NOT EXISTS achievements (
            user_id INTEGER,
            code TEXT,
            earned_at TEXT,
            UNIQUE(user_id, code)
        )
        """)

        # rooms (friend leagues)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS rooms (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE,
            name TEXT,
            owner_id INTEGER,
            created_at TEXT
        )
        """)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS room_members (
            room_id INTEGER,
            user_id INTEGER,
            joined_at TEXT,
            UNIQUE(room_id, user_id)
        )
        """)

        # match comments
        cur.execute("""
        CREATE TABLE IF NOT EXISTS match_comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            match_id INTEGER,
            user_id INTEGER,
            text TEXT,
            created_at TEXT
        )
        """)

        # featured match of day
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
# UI / MENU
# ============================================================

BTN_ACTIVE = "⚡ Активные матчи"
BTN_TODAY = "🔥 Матч дня"
BTN_MY = "🧾 Мои прогнозы"
BTN_ROOMS = "👥 Комнаты"
BTN_LB = "🏆 Лидерборд"
BTN_PROFILE = "👤 Профиль"
BTN_FIND = "🔎 Найти игрока"
BTN_HELP = "ℹ️ Помощь"

SPORT_PRETTY = {
    "football": "⚽ Футбол",
    "hockey": "🏒 Хоккей",
    "nhl": "🏒 Хоккей",
    "esports": "🎮 Киберспорт",
    "other": "🏟 Другое",
    "manual": "📝 Ручные",
}

PER_PAGE = 10
COMMENTS_SHOW = 5

def main_menu() -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text=BTN_ACTIVE), KeyboardButton(text=BTN_TODAY)],
        [KeyboardButton(text=BTN_MY), KeyboardButton(text=BTN_LB)],
        [KeyboardButton(text=BTN_PROFILE), KeyboardButton(text=BTN_FIND)],
        [KeyboardButton(text=BTN_ROOMS), KeyboardButton(text=BTN_HELP)],
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
        title = (r["title"] or "").strip()
        if len(title) > 34:
            title = title[:34] + "…"
        rows.append([InlineKeyboardButton(text=f"#{mid} — {title}", callback_data=f"mopen:{mid}")])

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
        [
            InlineKeyboardButton(text="📊 Голоса", callback_data=f"stats:{match_id}"),
            InlineKeyboardButton(text="💬 Комментарий", callback_data=f"comment:{match_id}"),
        ],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back:sports")],
    ])

def ikb_rooms_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Создать комнату", callback_data="room:create")],
        [InlineKeyboardButton(text="🔗 Вступить по коду", callback_data="room:join")],
        [InlineKeyboardButton(text="📋 Мои комнаты", callback_data="room:list")],
    ])

def ikb_room_actions(room_id: int, is_owner: bool) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="🏆 Топ комнаты", callback_data=f"room:top:{room_id}")],
        [InlineKeyboardButton(text="🚪 Выйти", callback_data=f"room:leave:{room_id}")],
    ]
    if is_owner:
        rows.insert(0, [InlineKeyboardButton(text="🗑 Удалить комнату", callback_data=f"room:delete:{room_id}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="room:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

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
                SELECT id, title, start_time_utc, league, sport
                FROM matches
                WHERE status='open'
                ORDER BY COALESCE(start_time_utc, start_time) ASC, id DESC
                LIMIT ? OFFSET ?
            """, (PER_PAGE, offset)).fetchall()
        return con.execute("""
            SELECT id, title, start_time_utc, league, sport
            FROM matches
            WHERE status='open' AND COALESCE(NULLIF(LOWER(sport), ''), 'other')=?
            ORDER BY COALESCE(start_time_utc, start_time) ASC, id DESC
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

def get_last_comments(match_id: int, limit: int = COMMENTS_SHOW) -> List[sqlite3.Row]:
    with db() as con:
        return con.execute("""
            SELECT user_id, text, created_at
            FROM match_comments
            WHERE match_id=?
            ORDER BY id DESC
            LIMIT ?
        """, (match_id, limit)).fetchall()

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
        return False, f"Дедлайн прошёл (за {PREDICT_DEADLINE_MIN} мин до старта)."
    return True, ""

# ============================================================
# LEADERBOARDS (weekly/monthly/season)
# ============================================================

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

# ============================================================
# ACHIEVEMENTS
# ============================================================

ACH_LIST = [
    ("FIRST_WIN", "🏁 Первый верный прогноз"),
    ("STREAK_3", "🔥 Серия 3"),
    ("STREAK_5", "💥 Серия 5"),
    ("WINS_10", "🎯 10 побед"),
    ("WINS_50", "🏆 50 побед"),
]

def has_ach(user_id: int, code: str) -> bool:
    with db() as con:
        r = con.execute("SELECT 1 FROM achievements WHERE user_id=? AND code=?", (user_id, code)).fetchone()
    return r is not None

def grant_ach(user_id: int, code: str) -> bool:
    if has_ach(user_id, code):
        return False
    with db() as con:
        try:
            con.execute("INSERT INTO achievements(user_id, code, earned_at) VALUES(?,?,?)", (user_id, code, iso(now_utc())))
            con.commit()
            return True
        except sqlite3.IntegrityError:
            return False

def get_score_row(user_id: int) -> sqlite3.Row:
    with db() as con:
        r = con.execute("SELECT * FROM scores WHERE user_id=?", (user_id,)).fetchone()
        if not r:
            con.execute("INSERT OR IGNORE INTO scores(user_id, updated_at) VALUES(?,?)", (user_id, iso(now_utc())))
            con.commit()
            r = con.execute("SELECT * FROM scores WHERE user_id=?", (user_id,)).fetchone()
    return r

async def check_achievements(user_id: int) -> List[str]:
    s = get_score_row(user_id)
    unlocked: List[str] = []
    if int(s["correct"]) >= 1 and grant_ach(user_id, "FIRST_WIN"):
        unlocked.append("🏁 Первый верный прогноз")
    if int(s["streak"]) >= 3 and grant_ach(user_id, "STREAK_3"):
        unlocked.append("🔥 Серия 3")
    if int(s["streak"]) >= 5 and grant_ach(user_id, "STREAK_5"):
        unlocked.append("💥 Серия 5")
    if int(s["correct"]) >= 10 and grant_ach(user_id, "WINS_10"):
        unlocked.append("🎯 10 побед")
    if int(s["correct"]) >= 50 and grant_ach(user_id, "WINS_50"):
        unlocked.append("🏆 50 побед")
    return unlocked

def list_achievements(user_id: int) -> List[str]:
    with db() as con:
        rows = con.execute("SELECT code FROM achievements WHERE user_id=? ORDER BY earned_at ASC", (user_id,)).fetchall()
    codes = [r["code"] for r in rows]
    pretty = {c: t for c, t in ACH_LIST}
    return [pretty.get(c, c) for c in codes]

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
# ROOMS
# ============================================================

def gen_room_code() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    import random
    return "".join(random.choice(alphabet) for _ in range(6))

def create_room(owner_id: int, name: str) -> Tuple[int, str]:
    code = gen_room_code()
    with db() as con:
        cur = con.cursor()
        for _ in range(20):
            try:
                cur.execute("INSERT INTO rooms(code, name, owner_id, created_at) VALUES(?,?,?,?)", (code, name, owner_id, iso(now_utc())))
                room_id = cur.lastrowid
                cur.execute("INSERT OR IGNORE INTO room_members(room_id, user_id, joined_at) VALUES(?,?,?)", (room_id, owner_id, iso(now_utc())))
                con.commit()
                return int(room_id), code
            except sqlite3.IntegrityError:
                code = gen_room_code()
        raise RuntimeError("Could not generate unique room code")

def join_room(user_id: int, code: str) -> Optional[int]:
    with db() as con:
        r = con.execute("SELECT id FROM rooms WHERE code=?", (code.upper(),)).fetchone()
        if not r:
            return None
        room_id = int(r["id"])
        con.execute("INSERT OR IGNORE INTO room_members(room_id, user_id, joined_at) VALUES(?,?,?)", (room_id, user_id, iso(now_utc())))
        con.commit()
        return room_id

def my_rooms(user_id: int) -> List[sqlite3.Row]:
    with db() as con:
        return con.execute("""
            SELECT rm.room_id, r.code, r.name, r.owner_id
            FROM room_members rm
            JOIN rooms r ON r.id = rm.room_id
            WHERE rm.user_id=?
            ORDER BY r.created_at DESC
        """, (user_id,)).fetchall()

def room_top(room_id: int, since: Optional[datetime] = None, limit: int = 10) -> List[Tuple[int, int]]:
    with db() as con:
        if since:
            rows = con.execute("""
                SELECT pl.user_id, COALESCE(SUM(pl.points),0) pts
                FROM points_log pl
                JOIN room_members rm ON rm.user_id = pl.user_id AND rm.room_id=?
                WHERE pl.created_at >= ?
                GROUP BY pl.user_id
                ORDER BY pts DESC
                LIMIT ?
            """, (room_id, iso(since), limit)).fetchall()
        else:
            rows = con.execute("""
                SELECT s.user_id, s.points pts
                FROM scores s
                JOIN room_members rm ON rm.user_id = s.user_id AND rm.room_id=?
                ORDER BY pts DESC
                LIMIT ?
            """, (room_id, limit)).fetchall()
    return [(int(r["user_id"]), int(r["pts"])) for r in rows]

def leave_room(room_id: int, user_id: int) -> None:
    with db() as con:
        con.execute("DELETE FROM room_members WHERE room_id=? AND user_id=?", (room_id, user_id))
        con.commit()

def delete_room(room_id: int, user_id: int) -> bool:
    with db() as con:
        r = con.execute("SELECT owner_id FROM rooms WHERE id=?", (room_id,)).fetchone()
        if not r or int(r["owner_id"]) != user_id:
            return False
        con.execute("DELETE FROM room_members WHERE room_id=?", (room_id,))
        con.execute("DELETE FROM rooms WHERE id=?", (room_id,))
        con.commit()
        return True

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
    await m.answer(
        "👋 <b>Привет!</b>\n\n"
        "Это бот прогнозов 1X2.\n"
        "Жми <b>⚡ Активные матчи</b> → выбери спорт → матч → исход.\n\n"
        f"⏱ Дедлайн: за <b>{PREDICT_DEADLINE_MIN} мин</b> до начала матча.",
        reply_markup=main_menu(),
    )

@dp.message(Command("help"))
@dp.message(F.text == BTN_HELP)
async def help_btn(m: Message):
    upsert_user_from_message(m)
    await m.answer(
        "ℹ️ <b>Помощь</b>\n\n"
        "• ⚡ Активные матчи → спорт → матч → 1/X/2\n"
        "• 🔥 Матч дня — быстрый доступ к главному матчу сегодня\n"
        "• 🧾 Мои прогнозы — последние ставки\n"
        "• 👥 Комнаты — мини-лиги друзей\n\n"
        f"Очки за верный исход: <b>+{POINTS_FOR_CORRECT}</b>",
        reply_markup=main_menu(),
    )

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
    header = "📋 Все матчи" if sport == "all" else SPORT_PRETTY.get(sport, sport)
    lines: List[str] = []
    for r in items:
        st = (r["start_time_utc"] or r["start_time"] or "").replace("T", " ").replace("+00:00", " UTC")
        league = (r["league"] or "").strip()
        prefix = f"[{league}] " if league else ""
        lines.append(f"• <b>#{r['id']}</b> {prefix}{r['title']}\n  <i>{st}</i>")

    await cb.message.answer(f"{header}\n\n" + "\n".join(lines), reply_markup=ikb_matches_list(sport, page, items, total))
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
        return f"{round((n/total_votes)*100)}%"

    my_pick = get_my_pick(target.from_user.id, match_id) if target.from_user else None  # type: ignore
    st = (match["start_time_utc"] or match["start_time"] or "").replace("T", " ").replace("+00:00", " UTC")
    sport = SPORT_PRETTY.get((match["sport"] or "other").lower(), match["sport"] or "other")
    league = (match["league"] or "").strip()
    dl = deadline_for_match(match)
    dl_text = dl.isoformat().replace("T", " ").replace("+00:00", " UTC") if dl else "—"
    allowed, why = can_predict(match)

    comments = get_last_comments(match_id, COMMENTS_SHOW)
    if comments:
        cm_lines = []
        for c in reversed(comments):
            name = pretty_user(int(c["user_id"]))
            cm_lines.append(f"• <b>{name}</b>: {c['text']}")
        comments_text = "\n".join(cm_lines)
    else:
        comments_text = "<i>Пока нет комментариев.</i>"

    lock_line = f"🔒 <b>Ставки закрыты</b>: {why}" if not allowed else "✅ <b>Ставки открыты</b>"
    text = (
        f"🏟 <b>Матч #{match_id}</b>\n"
        f"{match['title']}\n\n"
        f"• Спорт: <b>{sport}</b>\n"
        f"• Лига: <b>{league or '—'}</b>\n"
        f"• Старт: <i>{st}</i>\n"
        f"• Дедлайн: <i>{dl_text}</i>\n"
        f"{lock_line}\n\n"
        f"📊 Голоса: 1={stats['1']} ({pct(stats['1'])})   X={stats['X']} ({pct(stats['X'])})   2={stats['2']} ({pct(stats['2'])})\n"
        f"🎯 Твой выбор: <b>{my_pick or '—'}</b>\n\n"
        f"💬 <b>Комментарии</b>\n{comments_text}\n\n"
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

@dp.callback_query(F.data.startswith("comment:"))
async def cb_comment(cb: CallbackQuery):
    upsert_user_from_message(cb)
    try:
        match_id = int(cb.data.split(":")[1])
    except Exception:
        await cb.answer("Ошибка.", show_alert=True)
        return
    set_state(cb.from_user.id, "await_comment", match_id=match_id)
    await cb.message.answer("💬 Напиши комментарий (до 200 символов):")
    await cb.answer()

@dp.message(F.text == BTN_MY)
async def my_predictions(m: Message):
    upsert_user_from_message(m)
    with db() as con:
        rows = con.execute("""
            SELECT v.match_id, v.pick, v.created_at, m.title, m.status, m.result
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
        res = r["result"] or "—"
        lines.append(f"• #{r['match_id']} {r['title']} | pick=<b>{r['pick']}</b> | res=<b>{res}</b> | {r['status']}")
    await m.answer("\n".join(lines), reply_markup=main_menu())

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

async def send_profile(m: Message, user_id: int) -> None:
    s = get_score_row(user_id)
    ach = list_achievements(user_id)
    ach_text = "\n".join([f"• {a}" for a in ach]) if ach else "<i>пока нет</i>"
    text = (
        f"👤 <b>Профиль</b>\n\n"
        f"Игрок: {pretty_user(user_id)}\n"
        f"Очки: <b>{int(s['points'])}</b>\n"
        f"Победы: <b>{int(s['correct'])}</b> / Игр: <b>{int(s['total'])}</b>\n"
        f"Серия: <b>{int(s['streak'])}</b> (лучшая {int(s['best_streak'])})\n\n"
        f"🏅 <b>Достижения</b>\n{ach_text}"
    )
    await m.answer(text, reply_markup=main_menu())

@dp.message(F.text == BTN_PROFILE)
async def profile(m: Message):
    upsert_user_from_message(m)
    await send_profile(m, m.from_user.id)

@dp.message(F.text == BTN_FIND)
async def find_player(m: Message):
    upsert_user_from_message(m)
    set_state(m.from_user.id, "await_find")
    await m.answer("🔎 Отправь @username игрока (например @nickname):", reply_markup=main_menu())

@dp.message(F.text == BTN_ROOMS)
async def rooms(m: Message):
    upsert_user_from_message(m)
    await m.answer("👥 <b>Комнаты</b>\n\nСоздай комнату или вступи по коду:", reply_markup=ikb_rooms_menu())

@dp.callback_query(F.data == "room:back")
async def cb_room_back(cb: CallbackQuery):
    await cb.message.answer("👥 Комнаты:", reply_markup=ikb_rooms_menu())
    await cb.answer()

@dp.callback_query(F.data == "room:create")
async def cb_room_create(cb: CallbackQuery):
    upsert_user_from_message(cb)
    set_state(cb.from_user.id, "await_room_name")
    await cb.message.answer("➕ Введи название комнаты:")
    await cb.answer()

@dp.callback_query(F.data == "room:join")
async def cb_room_join(cb: CallbackQuery):
    upsert_user_from_message(cb)
    set_state(cb.from_user.id, "await_room_code")
    await cb.message.answer("🔗 Введи код комнаты (6 символов):")
    await cb.answer()

@dp.callback_query(F.data == "room:list")
async def cb_room_list(cb: CallbackQuery):
    upsert_user_from_message(cb)
    rooms_ = my_rooms(cb.from_user.id)
    if not rooms_:
        await cb.message.answer("Ты пока не состоишь ни в одной комнате.", reply_markup=ikb_rooms_menu())
        await cb.answer()
        return
    lines = ["📋 <b>Мои комнаты</b>\n"]
    kb_rows: List[List[InlineKeyboardButton]] = []
    for r in rooms_[:15]:
        rid = int(r["room_id"])
        lines.append(f"• <b>{r['name']}</b> (<code>{r['code']}</code>)")
        kb_rows.append([InlineKeyboardButton(text=f"Открыть: {r['name']}", callback_data=f"room:open:{rid}")])
    kb_rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="room:back")])
    await cb.message.answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))
    await cb.answer()

@dp.callback_query(F.data.startswith("room:open:"))
async def cb_room_open(cb: CallbackQuery):
    upsert_user_from_message(cb)
    room_id = int(cb.data.split(":")[2])
    await show_room(cb.message, room_id)
    await cb.answer()

async def show_room(m: Message, room_id: int) -> None:
    with db() as con:
        r = con.execute("SELECT id, code, name, owner_id FROM rooms WHERE id=?", (room_id,)).fetchone()
    if not r:
        await m.answer("Комната не найдена.")
        return
    owner = int(r["owner_id"])
    is_owner = (m.from_user.id == owner) if m.from_user else False
    await m.answer(
        f"👥 <b>{r['name']}</b>\nКод: <code>{r['code']}</code>\nВладелец: {pretty_user(owner)}",
        reply_markup=ikb_room_actions(room_id, is_owner),
    )

@dp.callback_query(F.data.startswith("room:top:"))
async def cb_room_top(cb: CallbackQuery):
    upsert_user_from_message(cb)
    room_id = int(cb.data.split(":")[2])
    rows = room_top(room_id, since=start_of_week_utc(now_utc()), limit=10)
    if not rows:
        await cb.message.answer("Топ пока пуст.")
        await cb.answer()
        return
    lines = ["🏆 <b>Топ комнаты (неделя)</b>\n"]
    for i, (uid, pts) in enumerate(rows, 1):
        lines.append(f"{i}. {pretty_user(uid)} — <b>{pts}</b>")
    await cb.message.answer("\n".join(lines))
    await cb.answer()

@dp.callback_query(F.data.startswith("room:leave:"))
async def cb_room_leave(cb: CallbackQuery):
    upsert_user_from_message(cb)
    room_id = int(cb.data.split(":")[2])
    leave_room(room_id, cb.from_user.id)
    await cb.message.answer("🚪 Ты вышел из комнаты.", reply_markup=ikb_rooms_menu())
    await cb.answer()

@dp.callback_query(F.data.startswith("room:delete:"))
async def cb_room_delete(cb: CallbackQuery):
    upsert_user_from_message(cb)
    room_id = int(cb.data.split(":")[2])
    ok = delete_room(room_id, cb.from_user.id)
    await cb.message.answer("🗑 Комната удалена." if ok else "❌ Удалить может только владелец.", reply_markup=ikb_rooms_menu())
    await cb.answer()

async def state_router(m: Message):
    # placeholder; actual router is above (decorated)
    return

@dp.message(F.text == BTN_HELP)
async def help_dup(m: Message):
    # avoid duplicate: do nothing (the help handler above already handles)
    return

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

    for v in votes:
        uid = int(v["user_id"])
        unlocked = await check_achievements(uid)
        if unlocked:
            try:
                await bot.send_message(uid, "🏅 Новые достижения!\n" + "\n".join([f"• {x}" for x in unlocked]))
            except Exception:
                pass

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
                            await bot.send_message(ADMIN_ID, f"✅ Итог: матч #{mid} = {fin.result_1x2}")
                        except Exception:
                            pass

            except Exception as e:
                logger.exception("auto_results_loop error: %s", e)

async def daily_digest_loop():
    last_sent_day = ""
    while True:
        try:
            if not DIGEST_ENABLED:
                await asyncio.sleep(60)
                continue

            now = now_utc()
            day_key = now.date().isoformat()
            if day_key != last_sent_day and now.hour == DIGEST_HOUR_UTC and now.minute >= DIGEST_MINUTE_UTC:
                start = now.replace(hour=0, minute=0, second=0, microsecond=0)
                end = start + timedelta(days=1)
                with db() as con:
                    matches = con.execute("""
                        SELECT id, title, COALESCE(start_time_utc, start_time) st, sport
                        FROM matches
                        WHERE status='open'
                          AND COALESCE(start_time_utc, start_time) >= ?
                          AND COALESCE(start_time_utc, start_time) < ?
                        ORDER BY st ASC
                        LIMIT 20
                    """, (iso(start), iso(end))).fetchall()
                    users = con.execute("SELECT user_id FROM users ORDER BY updated_at DESC LIMIT 2000").fetchall()

                if matches:
                    lines = ["📅 <b>Матчи сегодня</b>\n"]
                    for m in matches:
                        sport = SPORT_PRETTY.get((m["sport"] or "other").lower(), m["sport"] or "other")
                        st = (m["st"] or "").replace("T", " ").replace("+00:00", " UTC")
                        lines.append(f"• <b>#{m['id']}</b> {m['title']}  <i>{st}</i>  ({sport})")
                    text = "\n".join(lines) + "\n\nЖми «⚡ Активные матчи» или «🔥 Матч дня»."
                else:
                    text = "📅 Сегодня нет матчей в базе. Загляни позже 🙂"

                for u in users:
                    uid = int(u["user_id"])
                    try:
                        await bot.send_message(uid, text)
                    except Exception:
                        pass

                last_sent_day = day_key

        except Exception as e:
            logger.exception("daily_digest_loop error: %s", e)

        await asyncio.sleep(30)

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
    if DIGEST_ENABLED:
        asyncio.create_task(daily_digest_loop())

    while True:
        try:
            await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
            break
        except Exception as e:
            logger.exception("Polling crashed, restarting in 5 seconds: %s", e)
            await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(main())
