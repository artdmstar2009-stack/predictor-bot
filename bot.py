
def ikb_stake_amounts(match_id: int, pick: str) -> 'InlineKeyboardMarkup':
    """Keyboard for stake selection."""
    # Common stake sizes; you can change later
    amounts = [50, 100, 200, 500, 1000]
    rows: list[list[InlineKeyboardButton]] = []
    # 2 per row
    for i in range(0, len(amounts), 2):
        row = []
        for amt in amounts[i:i+2]:
            row.append(InlineKeyboardButton(text=f"{amt}💰", callback_data=f"stake:{match_id}:{pick}:{amt}"))
        rows.append(row)
    # custom amount
    rows.append([InlineKeyboardButton(text="✍️ Другая сумма", callback_data=f"stake_custom:{match_id}:{pick}")])
    # back to match
    rows.append([InlineKeyboardButton(text="⬅️ Назад к матчу", callback_data=f"match:{match_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


import os
print("BOT_AI_FORM_V3", "ODDS_LOOKAHEAD_HOURS=", os.getenv("ODDS_LOOKAHEAD_HOURS", "72"))



def acquire_polling_lock() -> bool:
    """Best-effort lock to ensure only one instance runs polling (avoids TelegramConflictError)."""
    owner = os.getenv("RENDER_INSTANCE_ID") or os.getenv("HOSTNAME") or str(os.getpid())
    now = iso(now_utc())
    with db() as con:
        cur = con.cursor()
        cur.execute("""
        CREATE TABLE IF NOT EXISTS app_lock (
            key TEXT PRIMARY KEY,
            owner TEXT,
            updated_at TEXT
        )
        """)
        con.commit()
        try:
            cur.execute(
                "INSERT INTO app_lock(key, owner, updated_at) VALUES(?,?,?)",
                ("polling", owner, now),
            )
            con.commit()
            return True
        except sqlite3.IntegrityError:
            row = cur.execute("SELECT owner, updated_at FROM app_lock WHERE key='polling'").fetchone()
            if not row:
                return False
            try:
                last = datetime.fromisoformat(row["updated_at"])
            except Exception:
                last = now_utc() - timedelta(hours=1)
            # If lock is stale (no heartbeat for 3 minutes), take over
            if (now_utc() - last).total_seconds() > 180:
                cur.execute(
                    "UPDATE app_lock SET owner=?, updated_at=? WHERE key='polling'",
                    (owner, now),
                )
                con.commit()
                return True
            return False

async def polling_lock_heartbeat():
    owner = os.getenv("RENDER_INSTANCE_ID") or os.getenv("HOSTNAME") or str(os.getpid())
    while True:
        try:
            with db() as con:
                cur = con.cursor()
                cur.execute(
                    "UPDATE app_lock SET updated_at=? WHERE key='polling' AND owner=?",
                    (iso(now_utc()), owner),
                )
                con.commit()
        except Exception:
            pass
        await asyncio.sleep(60)

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
print('BOT_ODDS_REAL_V3_4')
print('BOT_FULL_FINAL_FIXED_V2')
print('BOT_FULL_FINAL_V5')

import asyncio
import logging
import os
import re
import sqlite3

def _has_column(con: sqlite3.Connection, table: str, column: str) -> bool:
    try:
        rows = con.execute(f"PRAGMA table_info({table})").fetchall()
        return any(r[1] == column for r in rows)
    except Exception:
        return False

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
MATCH_LIST_LIMIT = int(os.getenv('MATCH_LIST_LIMIT', '20'))
AUTOSYNC_INTERVAL_MIN = int(os.getenv('AUTOSYNC_INTERVAL_MIN', '30'))

AI_MARGIN = float(os.getenv('AI_MARGIN', '0.07'))  # 7% bookmaker margin
ELO_START = int(os.getenv('ELO_START', '1500'))
ELO_K = int(os.getenv('ELO_K', '20'))
HOME_ADV_FOOTBALL = int(os.getenv('HOME_ADV_FOOTBALL', '60'))
HOME_ADV_NHL = int(os.getenv('HOME_ADV_NHL', '45'))

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
WEEKLY_BONUS_AMOUNT = int(os.getenv("WEEKLY_BONUS_AMOUNT", "1000"))

ODDS_API_KEY = os.getenv("ODDS_API_KEY", "")
ODDS_BASE_URL = os.getenv("ODDS_BASE_URL", "https://api.the-odds-api.com").rstrip("/")
ODDS_REGIONS = os.getenv("ODDS_REGIONS", "eu")  # eu/us/uk/au
ODDS_MARKETS = os.getenv("ODDS_MARKETS", "h2h")
ODDS_ODDS_FORMAT = os.getenv("ODDS_ODDS_FORMAT", "decimal")
ODDS_DATE_FORMAT = os.getenv("ODDS_DATE_FORMAT", "iso")
ODDS_REFRESH_INTERVAL = int(os.getenv("ODDS_REFRESH_INTERVAL", "900"))
ODDS_LOOKAHEAD_HOURS = int(os.getenv("ODDS_LOOKAHEAD_HOURS", "72"))
ODDS_PREFERRED_BOOKS = [s.strip() for s in os.getenv("ODDS_PREFERRED_BOOKS", "pinnacle,bet365,williamhill").split(",") if s.strip()]
ODDS_PROVIDER = os.getenv("ODDS_PROVIDER", "none").lower()  # 'theoddsapi' or 'none'


LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
DISPLAY_TZ = os.getenv('DISPLAY_TZ', 'Europe/Moscow')
DISPLAY_ZONE = ZoneInfo(DISPLAY_TZ)
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



def get_balance(user_id: int) -> int:
    with db() as con:
        cur = con.cursor()
        try:
            row = cur.execute("SELECT balance FROM scores WHERE user_id=?", (user_id,)).fetchone()
        except sqlite3.OperationalError:
            init_db()
            row = cur.execute("SELECT balance FROM scores WHERE user_id=?", (user_id,)).fetchone()
        if not row:
            cur.execute("INSERT OR IGNORE INTO scores(user_id, updated_at, balance) VALUES(?,?,?)",
                        (user_id, iso(now_utc()), 0))
            con.commit()
            return 0
        return int(row["balance"] or 0)

def add_balance(user_id: int, delta: int) -> None:
    with db() as con:
        cur = con.cursor()
        try:
            cur.execute("INSERT OR IGNORE INTO scores(user_id, updated_at, balance) VALUES(?,?,?)",
                        (user_id, iso(now_utc()), 0))
            cur.execute("UPDATE scores SET balance = COALESCE(balance,0) + ?, updated_at=? WHERE user_id=?",
                        (delta, iso(now_utc()), user_id))
            con.commit()
        except sqlite3.OperationalError:
            init_db()
            cur = con.cursor()
            cur.execute("INSERT OR IGNORE INTO scores(user_id, updated_at, balance) VALUES(?,?,?)",
                        (user_id, iso(now_utc()), 0))
            cur.execute("UPDATE scores SET balance = COALESCE(balance,0) + ?, updated_at=? WHERE user_id=?",
                        (delta, iso(now_utc()), user_id))
            con.commit()

def match_odds_for_pick(match: dict, pick: str) -> float | None:
    """Return decimal odds for pick ('1','X','2'). Uses internal AI odds."""
    m = ai_odds_for_match(dict(match))
    if pick == "1":
        return m.get("odds_1")
    if pick == "X":
        return m.get("odds_x")
    if pick == "2":
        return m.get("odds_2")
    return None

def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)

def is_admin(uid: int) -> bool:
    return ADMIN_ID != 0 and uid == ADMIN_ID

def init_db() -> None:
    with db() as con:
        cur = con.cursor()

        def _safe(stmt: str):
            try:
                cur.execute(stmt)
            except sqlite3.OperationalError:
                pass

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
            "ALTER TABLE matches ADD COLUMN odds_updated_at TEXT",
            "ALTER TABLE matches ADD COLUMN odds_source TEXT",
        ]:
            _safe(stmt)

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
            _safe(stmt)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS scores (
            user_id INTEGER PRIMARY KEY,
            points INTEGER DEFAULT 0,
            balance INTEGER DEFAULT 0,
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

        # AI odds: ELO ratings table
        cur.execute("""
        CREATE TABLE IF NOT EXISTS team_ratings (
            sport TEXT,
            team TEXT,
            elo INTEGER,
            updated_at TEXT,
            PRIMARY KEY (sport, team)
        )
        """)

        # Team form history (last matches) for AI odds

        cur.execute("""

        CREATE TABLE IF NOT EXISTS team_form (

            id INTEGER PRIMARY KEY AUTOINCREMENT,

            sport TEXT,

            team TEXT,

            match_time TEXT,

            result TEXT,          -- 'W','D','L'

            gf INTEGER,

            ga INTEGER

        )

        """)

        cur.execute("CREATE INDEX IF NOT EXISTS ix_team_form ON team_form(sport, team, match_time)")


        con.commit()

# =========================
# AI ODDS (ELO-based)
# =========================

def _sport_key_for_ai(sport: str | None, league: str | None = None) -> str:
    s = (sport or "").lower()
    l = (league or "").lower()
    if "nhl" in s or "nhl" in l or "ice" in s:
        return "nhl"
    return "football"

def get_team_elo(sport_key: str, team: str) -> int:
    team = (team or "").strip()
    if not team:
        return ELO_START
    with db() as con:
        r = con.execute(
            "SELECT elo FROM team_ratings WHERE sport=? AND team=?",
            (sport_key, team),
        ).fetchone()
        if r:
            return int(r[0])
        con.execute(
            "INSERT OR REPLACE INTO team_ratings(sport, team, elo, updated_at) VALUES(?,?,?,?)",
            (sport_key, team, ELO_START, iso(now_utc())),
        )
        con.commit()
    return ELO_START

def set_team_elo(sport_key: str, team: str, elo: int) -> None:
    team = (team or "").strip()
    if not team:
        return
    with db() as con:
        con.execute(
            "INSERT OR REPLACE INTO team_ratings(sport, team, elo, updated_at) VALUES(?,?,?,?)",
            (sport_key, team, int(elo), iso(now_utc())),
        )
        con.commit()

def _expected_score(elo_a: float, elo_b: float) -> float:
    return 1.0 / (1.0 + 10 ** ((elo_b - elo_a) / 400.0))

def update_elo_after_match(sport_key: str, home: str, away: str, result_1x2: str) -> None:
    """Update ELO after match. result_1x2 is '1','X','2'."""
    eh = get_team_elo(sport_key, home)
    ea = get_team_elo(sport_key, away)

    # home advantage applied in expected score only
    adv = HOME_ADV_NHL if sport_key == "nhl" else HOME_ADV_FOOTBALL
    exp_home = _expected_score(eh + adv, ea)

    if result_1x2 == "1":
        s_home = 1.0
    elif result_1x2 == "2":
        s_home = 0.0
    else:
        s_home = 0.5

    new_eh = round(eh + ELO_K * (s_home - exp_home))
    new_ea = round(ea + ELO_K * ((1.0 - s_home) - (1.0 - exp_home)))

    set_team_elo(sport_key, home, new_eh)
    set_team_elo(sport_key, away, new_ea)

def ai_probs_1x2(match: dict) -> tuple[float, float | None, float]:
    """Returns (p1, px_or_none, p2)."""
    sport_key = _sport_key_for_ai(match.get("sport"), match.get("league"))
    home = match.get("home_team") or ""
    away = match.get("away_team") or ""
    if not home or not away:
        # fallback: parse title
        teams = _parse_title_teams(match.get("title", "")) if "_parse_title_teams" in globals() else None
        if teams:
            home, away = teams
    eh = get_team_elo(sport_key, home)
    ea = get_team_elo(sport_key, away)

    fb_home = get_form_bonus(sport_key, home, 5)
    fb_away = get_form_bonus(sport_key, away, 5)

    adv = HOME_ADV_NHL if sport_key == "nhl" else HOME_ADV_FOOTBALL

    eh_adj = eh + adv + fb_home - fb_away
    ea_adj = ea + fb_away - fb_home

    p_home_nd = _expected_score(eh_adj, ea_adj)  # win prob in 2-way

    if sport_key == "nhl":
        return float(p_home_nd), None, float(1.0 - p_home_nd)

    # Football: add draw probability
    diff = abs(eh_adj - ea_adj)
    # base draw ~0.26, increases when teams close, decreases when mismatch
    p_draw = 0.20 + 0.12 * (1.0 / (1.0 + (diff / 250.0)))
    p_draw = max(0.18, min(0.32, p_draw))
    rem = 1.0 - p_draw
    p1 = rem * p_home_nd
    p2 = rem * (1.0 - p_home_nd)

    # safety normalize
    s = p1 + p_draw + p2
    return p1 / s, p_draw / s, p2 / s

def probs_to_odds(p1: float, px: float | None, p2: float) -> tuple[float, float | None, float]:
    """Apply margin and convert to decimal odds."""
    margin = max(0.0, float(AI_MARGIN))
    if px is None:
        # 2-way
        p1i = p1 * (1.0 + margin)
        p2i = p2 * (1.0 + margin)
        o1 = 1.0 / max(1e-6, p1i)
        o2 = 1.0 / max(1e-6, p2i)
        return round(max(1.01, min(50.0, o1)), 2), None, round(max(1.01, min(50.0, o2)), 2)

    p1i = p1 * (1.0 + margin)
    pxi = px * (1.0 + margin)
    p2i = p2 * (1.0 + margin)
    o1 = 1.0 / max(1e-6, p1i)
    ox = 1.0 / max(1e-6, pxi)
    o2 = 1.0 / max(1e-6, p2i)
    return (
        round(max(1.01, min(80.0, o1)), 2),
        round(max(1.01, min(80.0, ox)), 2),
        round(max(1.01, min(80.0, o2)), 2),
    )

def ai_odds_for_match(match: dict) -> dict:
    p1, px, p2 = ai_probs_1x2(match)
    o1, ox, o2 = probs_to_odds(p1, px, p2)
    out = dict(match)
    out["odds_1"] = o1
    out["odds_x"] = ox
    out["odds_2"] = o2
    out["odds_source"] = "AI"
    out["odds_updated_at"] = iso(now_utc())
    return out



# =========================
# TEAM FORM (last 5)
# =========================

def add_team_form_record(sport_key: str, team: str, match_time: datetime, result: str, gf: int | None, ga: int | None) -> None:
    team = (team or "").strip()
    if not team:
        return
    with db() as con:
        cur = con.cursor()
        cur.execute(
            "INSERT INTO team_form(sport, team, match_time, result, gf, ga) VALUES(?,?,?,?,?,?)",
            (sport_key, team, iso(match_time), result, int(gf or 0), int(ga or 0)),
        )
        # keep db small: keep last 40 rows per team
        cur.execute(
            """
            DELETE FROM team_form
            WHERE id IN (
                SELECT id FROM team_form
                WHERE sport=? AND team=?
                ORDER BY match_time DESC
                LIMIT -1 OFFSET 40
            )
            """,
            (sport_key, team),
        )
        con.commit()

def get_team_last_form(sport_key: str, team: str, n: int = 5) -> list[sqlite3.Row]:
    team = (team or "").strip()
    if not team:
        return []
    with db() as con:
        return con.execute(
            "SELECT result, gf, ga FROM team_form WHERE sport=? AND team=? ORDER BY match_time DESC LIMIT ?",
            (sport_key, team, int(n)),
        ).fetchall()

def get_form_bonus(sport_key: str, team: str, n: int = 5) -> int:
    """Return ELO-like bonus based on last N matches. Range примерно [-120..+120]."""
    rows = get_team_last_form(sport_key, team, n)
    if not rows:
        return 0

    pts = 0
    gd = 0
    for r in rows:
        res = (r["result"] or "").upper()
        if res == "W":
            pts += 1
        elif res == "L":
            pts -= 1
        try:
            gf = int(r["gf"] or 0)
            ga = int(r["ga"] or 0)
            gd += (gf - ga)
        except Exception:
            pass

    bonus = pts * 25 + max(-10, min(10, gd)) * 8
    return int(max(-120, min(120, bonus)))

def _norm_team(s: str) -> str:
    s = (s or "").lower().strip()
    s = re.sub(r"[\(\)\[\]\{\}]", " ", s)
    s = re.sub(r"[^a-z0-9а-яё\s\-\.]", " ", s, flags=re.IGNORECASE)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def _parse_title_teams(title: str) -> tuple[str, str] | None:
    t = (title or "").strip()
    if not t:
        return None
    low = t.lower()
    seps = [" vs ", " vs. ", " v ", " — ", " - ", " – "]
    for sep in seps:
        if sep in low:
            parts = re.split(re.escape(sep), t, flags=re.IGNORECASE)
            if len(parts) >= 2:
                a = parts[0].strip()
                b = parts[1].strip()
                if a and b:
                    return a, b
    m2 = re.match(r"^(.+?)[\-–—](.+)$", t)
    if m2:
        a, b = m2.group(1).strip(), m2.group(2).strip()
        if a and b:
            return a, b
    return None

async def _odds_api_get_json(session: aiohttp.ClientSession, path: str, params: dict) -> Any:
    url = f"{ODDS_BASE_URL}{path}"
    params = dict(params)
    params["apiKey"] = ODDS_API_KEY
    async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=25)) as resp:
        if resp.status != 200:
            text = await resp.text()
            raise RuntimeError(f"Odds API HTTP {resp.status}: {text[:300]}")
        return await resp.json()

async def odds_list_sports(session: aiohttp.ClientSession) -> list[dict]:
    return await _odds_api_get_json(session, "/v4/sports", {"all": "false"})

async def odds_fetch_for_sport(session: aiohttp.ClientSession, sport_key: str) -> list[dict]:
    return await _odds_api_get_json(
        session,
        f"/v4/sports/{sport_key}/odds",
        {
            "regions": ODDS_REGIONS,
            "markets": ODDS_MARKETS,
            "oddsFormat": ODDS_ODDS_FORMAT,
            "dateFormat": ODDS_DATE_FORMAT,
        },
    )

def _pick_bookmaker(bookmakers: list[dict]) -> tuple[str, dict] | None:
    if not bookmakers:
        return None
    by_key = {b.get("key"): b for b in bookmakers if b.get("key")}
    for pref in ODDS_PREFERRED_BOOKS:
        if pref in by_key:
            return pref, by_key[pref]
    b = bookmakers[0]
    return (b.get("key") or "unknown"), b

def _extract_h2h_prices(book: dict) -> dict[str, float]:
    out: dict[str, float] = {}
    for market in book.get("markets", []) or []:
        if market.get("key") != "h2h":
            continue
        for o in market.get("outcomes", []) or []:
            name = o.get("name")
            price = o.get("price")
            if name and isinstance(price, (int, float)):
                out[name] = float(price)
    return out

def _map_prices_to_1x2(event: dict, prices: dict[str, float]) -> tuple[float | None, float | None, float | None]:
    home = event.get("home_team")
    away = event.get("away_team")
    o1 = prices.get(home) if home else None
    o2 = prices.get(away) if away else None
    ox = prices.get("Draw") or prices.get("draw")
    return o1, ox, o2

async def refresh_odds_once() -> int:
    if not ODDS_API_KEY:
        return 0

    now = now_utc()
    horizon = now + timedelta(hours=int(globals().get("ODDS_LOOKAHEAD_HOURS", 72)))

    with db() as con:
        cur = con.cursor()
        rows = cur.execute(
            "SELECT id, sport, title, start_time FROM matches "
            "WHERE start_time IS NOT NULL AND start_time >= ? AND start_time <= ?",
            (iso(now), iso(horizon)),
        ).fetchall()

    candidates = []
    for r in rows:
        teams = _parse_title_teams(r["title"])
        if not teams:
            continue
        nh, na = _norm_team(teams[0]), _norm_team(teams[1])
        if nh and na:
            candidates.append((int(r["id"]), nh, na))

    if not candidates:
        return 0

    updated = 0
    async with aiohttp.ClientSession() as session:
        sports = await odds_list_sports(session)

        sport_keys: list[str] = []
        for s in sports:
            if not s.get("active"):
                continue
            key = s.get("key")
            group = (s.get("group") or "").lower()
            if not key:
                continue
            if "soccer" in group or key == "icehockey_nhl":
                sport_keys.append(key)

        for skey in sport_keys:
            try:
                events = await odds_fetch_for_sport(session, skey)
            except Exception as e:
                logger.warning("Odds fetch failed for %s: %s", skey, e)
                continue

            # Build list of events for fuzzy matching (by time + token overlap)
            ev_list: list[dict] = []
            for ev in events or []:
                ht, at = ev.get("home_team"), ev.get("away_team")
                ct = ev.get("commence_time")
                if not ht or not at or not ct:
                    continue
                try:
                    ev_dt = datetime.fromisoformat(ct.replace("Z", "+00:00"))
                except Exception:
                    continue
                ev_list.append({"ev": ev, "ht": ht, "at": at, "dt": ev_dt})

            def _team_tokens(s: str) -> set[str]:
                s = _norm_team(s)
                toks = {t for t in re.split(r"\s+", s) if len(t) >= 2}
                return toks

            with db() as con:
                cur = con.cursor()
                for mid, nh, na in candidates:
                    row = cur.execute("SELECT start_time FROM matches WHERE id=?", (mid,)).fetchone()
                    if not row or not row["start_time"]:
                        continue
                    try:
                        mdt = datetime.fromisoformat(row["start_time"])
                        if mdt.tzinfo is None:
                            mdt = mdt.replace(tzinfo=timezone.utc)
                    except Exception:
                        continue

                    tA = _team_tokens(nh)
                    tB = _team_tokens(na)

                    best = None
                    best_score = -1
                    for item in ev_list:
                        ev_dt = item["dt"]
                        if abs((ev_dt - mdt).total_seconds()) > 3 * 3600:
                            continue
                        ht_t = _team_tokens(item["ht"])
                        at_t = _team_tokens(item["at"])
                        s1 = (len(tA & ht_t) + len(tB & at_t))
                        s2 = (len(tA & at_t) + len(tB & ht_t))
                        score = max(s1, s2)
                        if score > best_score:
                            best_score = score
                            best = item["ev"]

                    if not best or best_score <= 0:
                        continue

                    pick = _pick_bookmaker(best.get("bookmakers", []) or [])
                    if not pick:
                        continue
                    bkey, book = pick
                    prices = _extract_h2h_prices(book)
                    if not prices:
                        continue
                    o1, ox, o2 = _map_prices_to_1x2(best, prices)
                    # Dynamic update: support older DB schema
                    if _has_column(con, "matches", "odds_updated_at") and _has_column(con, "matches", "odds_source"):
                        cur.execute(
                            "UPDATE matches SET odds_1=?, odds_x=?, odds_2=?, odds_updated_at=?, odds_source=? WHERE id=?",
                            (o1, ox, o2, iso(now_utc()), bkey, mid),
                        )
                    else:
                        cur.execute(
                            "UPDATE matches SET odds_1=?, odds_x=?, odds_2=? WHERE id=?",
                            (o1, ox, o2, mid),
                        )
                    updated += 1
                con.commit()

    return updated

async def odds_refresh_loop():
    while True:
        try:
            n = await refresh_odds_once()
            if n:
                logger.info("Odds updated for %s matches", n)
        except Exception as e:
            logger.exception("odds_refresh_loop error: %s", e)
        await asyncio.sleep(max(60, ODDS_REFRESH_INTERVAL))


async def ensure_odds_for_match(match_id: int) -> dict | None:
    match = get_match(match_id)
    if not match:
        return None
    return ai_odds_for_match(dict(match))

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
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="1", callback_data=f"pick:{match_id}:1"),
            InlineKeyboardButton(text="X", callback_data=f"pick:{match_id}:X"),
            InlineKeyboardButton(text="2", callback_data=f"pick:{match_id}:2"),
        ],
        [InlineKeyboardButton(text="📊 Голоса", callback_data=f"stats:{match_id}")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back:sports")],
    ])

# =========================
# QUERIES + DEADLINE
# =========================

def get_open_sports() -> List[Tuple[str, int]]:
    cutoff = iso(_today_msk_start_utc()) if "_today_msk_start_utc" in globals() else iso(now_utc().replace(hour=0, minute=0, second=0, microsecond=0))
    with db() as con:
        rows = con.execute("""
            SELECT COALESCE(NULLIF(LOWER(sport), ''), 'other') AS sport, COUNT(*) AS c
            FROM matches
            WHERE status='open'
              AND (COALESCE(start_time_utc, start_time) IS NULL OR COALESCE(start_time_utc, start_time) >= ?)
            GROUP BY COALESCE(NULLIF(LOWER(sport), ''), 'other')
            ORDER BY c DESC
        """, (cutoff,)).fetchall()
    return [(r["sport"], int(r["c"])) for r in rows]


def count_open_matches(sport: str) -> int:
    cutoff = iso(_today_msk_start_utc()) if "_today_msk_start_utc" in globals() else iso(now_utc().replace(hour=0, minute=0, second=0, microsecond=0))
    with db() as con:
        if sport == "all":
            r = con.execute(
                """
                SELECT COUNT(*) c FROM matches
                WHERE status='open'
                  AND (COALESCE(start_time_utc, start_time) IS NULL OR COALESCE(start_time_utc, start_time) >= ?)
                """,
                (cutoff,),
            ).fetchone()
        else:
            r = con.execute("""
                SELECT COUNT(*) c FROM matches
                WHERE status='open'
                  AND (COALESCE(start_time_utc, start_time) IS NULL OR COALESCE(start_time_utc, start_time) >= ?)
                  AND COALESCE(NULLIF(LOWER(sport), ''), 'other')=?
            """, (cutoff, sport)).fetchone()
    return int(r["c"]) if r else 0


def get_open_matches_page(sport: str, page: int) -> List[sqlite3.Row]:
    offset = max(0, page) * PER_PAGE
    cutoff = iso(_today_msk_start_utc()) if "_today_msk_start_utc" in globals() else iso(now_utc().replace(hour=0, minute=0, second=0, microsecond=0))
    limit = int(globals().get("MATCH_LIST_LIMIT", PER_PAGE))
    with db() as con:
        if sport == "all":
            return con.execute("""
                SELECT id, title, start_time_utc, league, sport, start_time
                FROM matches
                WHERE status='open'
                  AND (COALESCE(start_time_utc, start_time) IS NULL OR COALESCE(start_time_utc, start_time) >= ?)
                ORDER BY COALESCE(start_time_utc, start_time) ASC
                LIMIT ? OFFSET ?
            """, (cutoff, limit, offset)).fetchall()

        return con.execute("""
            SELECT id, title, start_time_utc, league, sport, start_time
            FROM matches
            WHERE status='open'
              AND (COALESCE(start_time_utc, start_time) IS NULL OR COALESCE(start_time_utc, start_time) >= ?)
              AND COALESCE(NULLIF(LOWER(sport), ''), 'other')=?
            ORDER BY COALESCE(start_time_utc, start_time) ASC
            LIMIT ? OFFSET ?
        """, (cutoff, sport, limit, offset)).fetchall()


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
    home_score: int | None = None
    away_score: int | None = None

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
            return FinishedInfo("1", hg, ag)
        if winner == "AWAY_TEAM":
            return FinishedInfo("2", hg, ag)
        if winner == "DRAW":
            return FinishedInfo("X", hg, ag)
        return None
    hg = int(hg); ag = int(ag)
    if hg > ag:
        return FinishedInfo("1", hg, ag)
    if hg < ag:
        return FinishedInfo("2", hg, ag)
    return FinishedInfo("X", hg, ag)

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
            return FinishedInfo("1", hs, a_s)
        if hs < a_s:
            return FinishedInfo("2", hs, a_s)
        return FinishedInfo("X", hs, a_s)
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


async def apply_scoring_for_match(match_id: int, result_1x2: str, home_score: int | None = None, away_score: int | None = None) -> None:
    """Closes match, updates points, handles betting payouts, notifies users."""
    notifications: list[tuple[int, str]] = []
    with db() as con:
        cur = con.cursor()
        st = cur.execute("SELECT status, title FROM matches WHERE id=?", (match_id,)).fetchone()
        if not st or st["status"] != "open":
            return

        cur.execute("UPDATE matches SET result=?, status='closed' WHERE id=?", (result_1x2, match_id))
        # AI ratings update (ELO + form)
        try:
            mrow = cur.execute("SELECT sport, league, title FROM matches WHERE id=?", (match_id,)).fetchone()
            sport_key = _sport_key_for_ai((mrow["sport"] if mrow else ""), (mrow["league"] if mrow else ""))
            teams = _parse_title_teams(((mrow["title"] if mrow else "") or (st["title"] or "")) or "")
            if teams:
                home, away = teams
                update_elo_after_match(sport_key, home, away, result_1x2)
                if home_score is not None and away_score is not None:
                    mt = now_utc()
                    if result_1x2 == "1":
                        rh, ra = "W", "L"
                    elif result_1x2 == "2":
                        rh, ra = "L", "W"
                    else:
                        rh = ra = "D"
                    add_team_form_record(sport_key, home, mt, rh, int(home_score), int(away_score))
                    add_team_form_record(sport_key, away, mt, ra, int(away_score), int(home_score))
        except Exception:
            logger.exception("AI update failed")

        votes = cur.execute(
            "SELECT user_id, pick, COALESCE(stake,0) AS stake, COALESCE(odds,0) AS odds FROM votes WHERE match_id=?",
            (match_id,),
        ).fetchall()

        for v in votes:
            uid = int(v["user_id"])
            pick = v["pick"]
            stake = int(v["stake"] or 0)
            odds = float(v["odds"] or 0.0)

            correct = (pick == result_1x2)
            delta = POINTS_FOR_CORRECT if correct else POINTS_FOR_WRONG

            cur.execute("INSERT OR IGNORE INTO scores(user_id, updated_at, balance) VALUES(?,?,?)", (uid, iso(now_utc()), 0))

            if correct:
                cur.execute(
                    """UPDATE scores
                       SET points = points + ?,
                           correct = correct + 1,
                           total = total + 1,
                           streak = streak + 1,
                           best_streak = CASE WHEN streak + 1 > best_streak THEN streak + 1 ELSE best_streak END,
                           updated_at = ?
                       WHERE user_id = ?""",
                    (delta, iso(now_utc()), uid),
                )
            else:
                cur.execute(
                    """UPDATE scores
                       SET points = points + ?,
                           total = total + 1,
                           streak = 0,
                           updated_at = ?
                       WHERE user_id = ?""",
                    (delta, iso(now_utc()), uid),
                )

            profit = 0
            if BETTING_ENABLED and stake > 0:
                if odds <= 0:
                    odds = match_odds_for_pick(get_match(match_id) or {}, pick)
                if correct:
                    payout = int(round(stake * odds))
                    profit = payout - stake
                    cur.execute("UPDATE scores SET balance = COALESCE(balance,0) + ? WHERE user_id=?", (payout, uid))
                else:
                    profit = -stake

                title = st["title"] or "Матч"
                teams = _parse_title_teams(title)
                home_name = teams[0] if teams else "Хозяева"
                away_name = teams[1] if teams else "Гости"

                score_txt = ""
                if home_score is not None and away_score is not None:
                    score_txt = f"\nСчёт: <b>{home_score}:{away_score}</b>"

                if result_1x2 == "1":
                    winner_txt = f"🏆 Победа: <b>{home_name}</b>"
                elif result_1x2 == "2":
                    winner_txt = f"🏆 Победа: <b>{away_name}</b>"
                else:
                    winner_txt = "🤝 Ничья"

                outcome_txt = "✅ Выигрыш" if correct else "❌ Проигрыш"
                profit_txt = f"<b>{profit:+d}</b>"

                msg = (
                    f"🏁 Итог матча: <b>{title}</b>\n"
                    f"{winner_txt}{score_txt}\n"
                    f"Твой выбор: <b>{pick}</b> | КФ: <b>{odds:.2f}</b>\n"
                    f"Ставка: <b>{stake}</b>\n"
                    f"{outcome_txt}: {profit_txt}"
                )

                notifications.append((uid, msg))

        con.commit()

    for uid, msg in notifications:
        try:
            await bot.send_message(uid, msg)
        except Exception:
            pass

async def autosync_loop():
    while True:
        try:
            if SYNC_ENABLED:
                msg = await fixed_sync_once()
                logger.info("autosync_loop: %s", msg)
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

                    await apply_scoring_for_match(mid, fin.result_1x2, fin.home_score, fin.away_score)

                    if ADMIN_ID:
                        try:
                            await bot.send_message(ADMIN_ID, f"✅ Матч закрыт: {fin.result_1x2} (id={mid})")
                        except Exception:
                            pass

            except Exception as e:
                logger.exception("auto_results_loop error: %s", e)

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


@dp.message(Command("odds_now"))
async def odds_now_cmd(m: Message):
    if m.from_user.id != ADMIN_ID:
        return await m.answer("Недостаточно прав.")
    await m.answer("Odds API отключён. Коэффициенты считаются внутренним AI (ELO) автоматически.")

@dp.message(F.text == BTN_ACTIVE)
async def active_matches(m: Message):
    upsert_user_from_message(m)
    try:
        init_db()
        # archive old matches before showing active list
        try:
            archive_past_matches()
        except Exception:
            logger.exception("archive_past_matches failed in active_matches")

        sports = get_open_sports()
        if not sports:
            await m.answer(
                "Пока нет активных матчей.\nАдмину можно нажать /sync_now, чтобы обновить список.",
                reply_markup=main_menu(),
            )
            return

        await m.answer("⚡ <b>Активные матчи</b>\n\nВыбери вид спорта 👇", reply_markup=main_menu())
        await m.answer("Категории:", reply_markup=ikb_sports(sports))
    except Exception as e:
        logger.exception("active_matches error")
        await m.answer(f"❌ Ошибка при загрузке активных матчей: {e}", reply_markup=main_menu())

@dp.callback_query(F.data.startswith("sport:"))
async def cb_sport(cb: CallbackQuery):
    upsert_user_from_message(cb)
    try:
        _, sport, page_s = cb.data.split(":")
        page = int(page_s)
        sport = (sport or "all").lower()

        total = count_open_matches(sport)
        if total <= 0:
            await cb.answer("В этой категории матчей нет.", show_alert=True)
            return

        max_page = max(0, (total - 1) // PER_PAGE)
        page = min(max(page, 0), max_page)

        items = get_open_matches_page(sport, page)
        if not items:
            await cb.answer("Матчи не найдены.", show_alert=True)
            return

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
    except Exception as e:
        logger.exception("cb_sport error")
        await cb.answer("Ошибка загрузки матчей.", show_alert=True)
        try:
            await cb.message.answer(f"❌ Ошибка загрузки матчей: {e}")
        except Exception:
            pass


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

# =========================
# CALLBACKS: OPEN MATCH / STATS / PICK
# =========================

    match = get_match(mid)
    if not match:
        return await cb.answer("Матч не найден.", show_alert=True)

    # Show card with odds (if present)
    o1 = match.get("odds_1")
    ox = match.get("odds_x")
    o2 = match.get("odds_2")
    odds_line = ""
    if o1 or ox or o2:
        parts = []
        if o1: parts.append(f"1: <b>{float(o1):.2f}</b>")
        if ox: parts.append(f"X: <b>{float(ox):.2f}</b>")
        if o2: parts.append(f"2: <b>{float(o2):.2f}</b>")
        odds_line = "\nКоэффициенты: " + "  ".join(parts)

    text = f"🏟 <b>{match['title']}</b>\n🕒 {fmt_dt(match.get('start_time_utc') or match.get('start_time') or '')}{odds_line}\n\nВыбери исход:"
    await cb.message.answer(text, reply_markup=ikb_match_card(mid))
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
        f"{sep}\n\n"
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

    if BETTING_ENABLED:
        set_pref(cb.from_user.id, pending_match_id=match_id, pending_pick=pick, awaiting_stake=True)
        odds = match_odds_for_pick(match, pick) or 2.00
        kb = stake_keyboard(match_id, pick, float(odds))
        await cb.message.answer(
            f"💰 Выбери сумму ставки\n"
            f"Исход: <b>{pick}</b>  |  КФ: <b>{odds:.2f}</b>\n"
            f"Твой баланс: <b>{get_balance(cb.from_user.id)}</b>",
            reply_markup=kb,
        )
        await cb.answer()
        return

    with db() as con:
        con.execute(
            "INSERT OR REPLACE INTO votes(user_id, match_id, pick, created_at) VALUES(?,?,?,?)",
            (cb.from_user.id, match_id, pick, iso(now_utc())),
        )
        con.commit()

    await cb.answer("✅ Принято!", show_alert=True)


def stake_keyboard(match_id: int, pick: str, odds: float):
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    presets = [100, 200, 500, 1000, 2000]
    rows = []
    row = []
    for amt in presets:
        row.append(InlineKeyboardButton(text=f"{amt}", callback_data=f"stake:{match_id}:{pick}:{amt}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="✍️ Другая сумма", callback_data=f"stake_custom:{match_id}:{pick}")])
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="stake_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

@dp.callback_query(F.data == "stake_cancel")
async def cb_stake_cancel(cb: CallbackQuery):
    set_pref(cb.from_user.id, awaiting_stake=False, awaiting_custom_stake=False, pending_match_id=None, pending_pick=None)
    await cb.answer("Отменено.", show_alert=True)

@dp.callback_query(F.data.startswith("stake_custom:"))
async def cb_stake_custom(cb: 'CallbackQuery'):
    upsert_user_from_message(cb)
    try:
        _, match_id_s, pick = cb.data.split(":")
        match_id = int(match_id_s)
    except Exception:
        await cb.answer("Ошибка.", show_alert=True)
        return
    set_pref(cb.from_user.id, awaiting_custom_stake=True, pending_match_id=match_id, pending_pick=pick)
    await cb.message.answer("✍️ Введи сумму ставки числом (например 250):")
    await cb.answer()

@dp.callback_query(F.data.startswith("stake:"))
async def cb_stake_amount(cb: CallbackQuery):
    upsert_user_from_message(cb)
    try:
        _, match_id_s, pick, amt_s = cb.data.split(":")
        match_id = int(match_id_s)
        stake = int(amt_s)
    except Exception:
        await cb.answer("Ошибка.", show_alert=True)
        return
    await place_bet(cb.from_user.id, match_id, pick, stake, cb)

@dp.message(lambda m: bool(getattr(m, "text", None)) and m.from_user and get_pref(m.from_user.id, "awaiting_custom_stake", False))
async def stake_amount_text(m: Message):
    upsert_user_from_message(m)
    try:
        stake = int(m.text.strip())
    except Exception:
        return await m.answer("Введите число.")
    match_id = int(get_pref(m.from_user.id, "pending_match_id", 0) or 0)
    pick = str(get_pref(m.from_user.id, "pending_pick", "") or "")
    if not match_id or pick not in ("1", "X", "2"):
        set_pref(m.from_user.id, awaiting_custom_stake=False)
        return await m.answer("Ставка отменена.")
    await place_bet(m.from_user.id, match_id, pick, stake, m)

async def place_bet(user_id: int, match_id: int, pick: str, stake: int, event):
    if stake <= 0:
        msg = "Неверная сумма."
        if isinstance(event, CallbackQuery):
            return await event.answer(msg, show_alert=True)
        return await event.answer(msg)

    match = get_match(match_id)
    if not match:
        msg = "Матч не найден."
        if isinstance(event, CallbackQuery):
            return await event.answer(msg, show_alert=True)
        return await event.answer(msg)

    ok, why = can_predict(match)
    if not ok:
        if isinstance(event, CallbackQuery):
            return await event.answer(why, show_alert=True)
        return await event.answer(why)

    bal = get_balance(user_id)
    if bal < stake:
        msg = f"Недостаточно баланса. Баланс: {bal}"
        if isinstance(event, CallbackQuery):
            return await event.answer(msg, show_alert=True)
        return await event.answer(msg)

    odds = match_odds_for_pick(match, pick)
    add_balance(user_id, -stake)

    with db() as con:
        con.execute(
            "INSERT OR REPLACE INTO votes(user_id, match_id, pick, created_at, stake, odds) VALUES(?,?,?,?,?,?)",
            (user_id, match_id, pick, iso(now_utc()), stake, odds),
        )
        con.commit()

    set_pref(user_id, awaiting_stake=False, awaiting_custom_stake=False, pending_match_id=None, pending_pick=None)

    text = (
        f"✅ Ставка принята!\n"
        f"Матч: <b>{match['title']}</b>\n"
        f"Исход: <b>{pick}</b> | КФ: <b>{odds:.2f}</b>\n"
        f"Сумма: <b>{stake}</b>\n"
        f"Баланс: <b>{get_balance(user_id)}</b>"
    )
    if isinstance(event, CallbackQuery):
        await event.message.answer(text)
        await event.answer()
    else:
        await event.answer(text)

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
        "👤 <b>Профиль</b>\n\n"
        f"Игрок: {pretty_user(m.from_user.id)}\n"
        f"Очки: <b>{int(s['points'])}</b>\n"
        f"Баланс: <b>{int(s['balance'] or 0)}</b>\n"
        f"Победы: <b>{int(s['correct'])}</b> / Игр: <b>{int(s['total'])}</b>\n"
        f"Серия: <b>{int(s['streak'])}</b> (лучшая {int(s['best_streak'])})\n",
        reply_markup=main_menu(),
    )

# =========================
# MAIN
# =========================



def next_weekly_bonus_run(now: datetime) -> datetime:
    local = now.astimezone(DISPLAY_ZONE)
    days_ahead = (0 - local.weekday()) % 7
    target = local.replace(hour=12, minute=0, second=0, microsecond=0) + timedelta(days=days_ahead)
    if target <= local:
        target += timedelta(days=7)
    return target.astimezone(timezone.utc)

async def weekly_bonus_loop():
    if not WEEKLY_BONUS_ENABLED:
        return
    while True:
        try:
            run_at = next_weekly_bonus_run(now_utc())
            sleep_s = max(5, int((run_at - now_utc()).total_seconds()))
            await asyncio.sleep(sleep_s)

            with db() as con:
                cur = con.cursor()
                try:
                    cur.execute("SELECT 1 FROM scores LIMIT 1")
                except sqlite3.OperationalError:
                    init_db()
                users = cur.execute("SELECT user_id FROM users").fetchall()
                uids = [int(r["user_id"]) for r in users]
                for uid in uids:
                    cur.execute("INSERT OR IGNORE INTO scores(user_id, updated_at, balance) VALUES(?,?,?)",
                                (uid, iso(now_utc()), 0))
                cur.execute("UPDATE scores SET balance = COALESCE(balance,0) + ?, updated_at=?",
                            (WEEKLY_BONUS_AMOUNT, iso(now_utc())))
                con.commit()

            for uid in uids:
                try:
                    await bot.send_message(uid, f"🎁 Еженедельный бонус: +<b>{WEEKLY_BONUS_AMOUNT}</b> баллов!")
                except Exception:
                    pass
        except Exception as e:
            logger.exception("weekly_bonus_loop error: %s", e)
            await asyncio.sleep(60)


# =========================
# SECRET ADMIN COMMANDS
# =========================
@dp.message(Command("secret_add5000"))
async def secret_add5000(m: Message):
    if not m.from_user:
        return

    if ADMIN_ID and int(m.from_user.id) != int(ADMIN_ID):
        return await m.answer("Недостаточно прав.")

    init_db()
    upsert_user_from_message(m)

    with db() as con:
        cur = con.cursor()
        cur.execute(
            "INSERT OR IGNORE INTO scores(user_id, points, balance, correct, total, streak, best_streak, updated_at) "
            "VALUES(?, 0, 0, 0, 0, 0, 0, ?)",
            (m.from_user.id, iso(now_utc())),
        )
        cur.execute(
            "UPDATE scores SET balance = COALESCE(balance, 0) + 5000, updated_at=? WHERE user_id=?",
            (iso(now_utc()), m.from_user.id),
        )
        row = cur.execute("SELECT balance FROM scores WHERE user_id=?", (m.from_user.id,)).fetchone()
        con.commit()

    balance = int(row["balance"] if row and row["balance"] is not None else 0)
    await m.answer(f"💰 +5000 начислено. Баланс теперь: <b>{balance}</b>")

@dp.message(Command("secret_balance"))
async def secret_balance(m: Message):
    if not m.from_user:
        return

    if ADMIN_ID and int(m.from_user.id) != int(ADMIN_ID):
        return await m.answer("Недостаточно прав.")

    init_db()
    upsert_user_from_message(m)

    with db() as con:
        row = con.execute("SELECT balance FROM scores WHERE user_id=?", (m.from_user.id,)).fetchone()

    balance = int(row["balance"] if row and row["balance"] is not None else 0)
    await m.answer(f"💰 Баланс: <b>{balance}</b>")


@dp.message()
async def on_custom_stake_amount(m: Message):
    # This handler only triggers when user is awaiting a custom stake
    try:
        prefs = get_pref(m.from_user.id)
    except Exception:
        prefs = {}
    if not prefs or not prefs.get("awaiting_custom_stake"):
        return  # let other handlers work

    text = (m.text or "").strip()
    if not text.isdigit():
        return await m.answer("Нужна сумма числом. Пример: 250")

    stake = int(text)
    if stake <= 0:
        return await m.answer("Сумма должна быть > 0.")
    if stake > 1_000_000:
        return await m.answer("Слишком большая сумма.")

    match_id = prefs.get("pending_match_id")
    pick = prefs.get("pending_pick")
    if not match_id or not pick:
        set_pref(m.from_user.id, awaiting_custom_stake=False, pending_match_id=None, pending_pick=None)
        return await m.answer("Не вижу выбранный матч/исход. Открой матч заново.")

    # Place bet
    set_pref(m.from_user.id, awaiting_custom_stake=False)
    await place_bet(m.from_user.id, int(match_id), str(pick), stake, m)


@dp.message(Command("secret_add5000"))
async def secret_add5000(m: Message):
    # secret admin-only command: silently ignore everyone else
    if int(m.from_user.id) != int(ADMIN_ID):
        return

    upsert_user_from_message(m)

    with db() as con:
        cur = con.cursor()
        # ensure score row exists
        cur.execute(
            "INSERT OR IGNORE INTO scores(user_id, points, balance, correct, total, streak, best_streak, updated_at) "
            "VALUES(?, 0, 0, 0, 0, 0, 0, ?)",
            (m.from_user.id, iso(now_utc())),
        )
        cur.execute(
            "UPDATE scores SET balance = COALESCE(balance,0) + 5000, updated_at=? WHERE user_id=?",
            (iso(now_utc()), m.from_user.id),
        )
        con.commit()

    await m.answer("💰 +5000 баллов начислено на баланс (тестовая команда).")


@dp.message(Command("secret_whoami"))
async def secret_whoami(m: Message):
    if int(m.from_user.id) != int(ADMIN_ID):
        return
    await m.answer(f"ADMIN_ID={ADMIN_ID}\nYOUR_ID={m.from_user.id}")



# =========================
# AUTO ARCHIVE OLD MATCHES
# =========================
from datetime import datetime, timezone, timedelta

def msk_day_start_utc(now=None):
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(DISPLAY_ZONE)
    local_start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return local_start.astimezone(timezone.utc)

def archive_old_matches():
    cutoff = msk_day_start_utc()
    with db() as con:
        cur = con.cursor()
        cur.execute(
            "UPDATE matches SET status='archived' WHERE start_time_utc < ? AND status!='archived'",
            (cutoff.isoformat(),)
        )
        con.commit()
        return cur.rowcount

async def daily_rollover_loop():
    while True:
        try:
            now = datetime.now(timezone.utc)
            local = now.astimezone(DISPLAY_ZONE)

            run_local = local.replace(hour=0, minute=10, second=0, microsecond=0)
            if run_local <= local:
                run_local = run_local + timedelta(days=1)

            run_utc = run_local.astimezone(timezone.utc)
            wait = (run_utc - now).total_seconds()
            await asyncio.sleep(max(5, int(wait)))

            n = archive_old_matches()
            logger.info(f"Archived old matches: {n}")

        except Exception:
            logger.exception("daily_rollover_loop error")
            await asyncio.sleep(60)

async def periodic_autosync_loop():
    """Runs autosync every AUTOSYNC_INTERVAL_MIN minutes."""
    while True:
        try:
            await asyncio.sleep(max(60, AUTOSYNC_INTERVAL_MIN * 60))
            fn = globals().get("autosync_once")
            if fn:
                res = fn()
                if asyncio.iscoroutine(res):
                    await res
            logger.info("Periodic autosync done")
        except Exception:
            logger.exception("periodic_autosync_loop error")
            await asyncio.sleep(60)


@dp.message(Command("ping"))
async def cmd_ping(m: Message):
    await m.answer("pong ✅")


# =========================
# FIXED SYNC + AI ODDS PIPELINE
# =========================

def _match_start_value(row) -> str:
    try:
        return (row["start_time_utc"] or row["start_time"] or "").strip()
    except Exception:
        return ""

def _today_msk_start_utc() -> datetime:
    local = now_utc().astimezone(DISPLAY_ZONE)
    start_local = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start_local.astimezone(timezone.utc)

def archive_past_matches() -> int:
    """Archive matches older than today's 00:00 MSK. Keeps history and votes safe."""
    cutoff = iso(_today_msk_start_utc())
    with db() as con:
        cur = con.cursor()
        cur.execute(
            """
            UPDATE matches
            SET status='archived'
            WHERE status='open'
              AND COALESCE(start_time_utc, start_time) < ?
            """,
            (cutoff,),
        )
        con.commit()
        return int(cur.rowcount or 0)

def refresh_ai_odds_for_open_matches() -> int:
    """Recalculate internal AI odds for every open match and store them in DB."""
    updated = 0
    with db() as con:
        cur = con.cursor()
        rows = cur.execute(
            """
            SELECT *
            FROM matches
            WHERE status='open'
              AND COALESCE(start_time_utc, start_time) >= ?
            ORDER BY COALESCE(start_time_utc, start_time) ASC
            """,
            (iso(_today_msk_start_utc()),),
        ).fetchall()

        for r in rows:
            try:
                odds_match = ai_odds_for_match(dict(r))
                cur.execute(
                    """
                    UPDATE matches
                    SET odds_1=?,
                        odds_x=?,
                        odds_2=?,
                        odds_updated_at=?,
                        odds_source='AI'
                    WHERE id=?
                    """,
                    (
                        odds_match.get("odds_1"),
                        odds_match.get("odds_x"),
                        odds_match.get("odds_2"),
                        iso(now_utc()),
                        int(r["id"]),
                    ),
                )
                updated += 1
            except Exception:
                logger.exception("AI odds refresh failed for match id=%s", r["id"])
        con.commit()
    return updated

async def fixed_sync_once() -> str:
    """One reliable sync cycle: archive old -> sync sources -> recalc AI odds -> pick featured."""
    archived = archive_past_matches()

    sync_msg = ""
    try:
        sync_msg = await autosync_once()
    except Exception as e:
        logger.exception("autosync_once failed")
        sync_msg = f"sync error: {e}"

    odds_updated = refresh_ai_odds_for_open_matches()

    try:
        pick_featured_for_today()
    except Exception:
        logger.exception("pick_featured_for_today failed")

    return f"archived={archived}; {sync_msg}; ai_odds={odds_updated}"

async def fixed_periodic_sync_loop():
    """Regular sync every AUTOSYNC_INTERVAL_MIN minutes."""
    while True:
        try:
            await asyncio.sleep(max(60, AUTOSYNC_INTERVAL_MIN * 60))
            msg = await fixed_sync_once()
            logger.info("Fixed periodic sync: %s", msg)
        except Exception:
            logger.exception("fixed_periodic_sync_loop error")
            await asyncio.sleep(60)


# =========================
# ADMIN SYNC COMMAND
# =========================
@dp.message(Command("sync_now"))
async def sync_now_cmd(m: Message):
    if ADMIN_ID and int(m.from_user.id) != int(ADMIN_ID):
        return await m.answer("Недостаточно прав.")

    await m.answer("🔄 Обновляю матчи и AI-коэффициенты...")

    try:
        msg = await fixed_sync_once()
        await m.answer(f"✅ Синхронизация завершена.\n<code>{msg}</code>")
    except Exception as e:
        logger.exception("sync_now error")
        await m.answer(f"❌ Ошибка синхронизации: {e}")


@dp.message(Command("ai_odds_refresh"))
async def ai_odds_refresh_cmd(m: Message):
    if ADMIN_ID and int(m.from_user.id) != int(ADMIN_ID):
        return await m.answer("Недостаточно прав.")
    try:
        n = refresh_ai_odds_for_open_matches()
        await m.answer(f"✅ AI-коэффициенты пересчитаны для матчей: {n}")
    except Exception as e:
        logger.exception("ai_odds_refresh error")
        await m.answer(f"❌ Ошибка: {e}")


async def main():
    init_db()
    asyncio.create_task(weekly_bonus_loop())
    asyncio.create_task(daily_rollover_loop())

    # For Render Web Service: keep port open (health endpoint)
    asyncio.create_task(start_web_server())

    # Keep-alive ping (only if KEEP_ALIVE_URL is set)
    asyncio.create_task(keep_alive_loop())

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
