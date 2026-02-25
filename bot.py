# -*- coding: utf-8 -*-
"""
Telegram Predictor Bot (aiogram v3) — 1X2 only
================================================

✅ What you asked for now:
- Re-connect AUTOSYNC (football-data.org + NHL schedules)
- Keep the bot stable (auto-restart polling)
- Sport categories: Football / Hockey / Esports / Other (based on matches.sport)
- Auto-results (for synced matches): bot pulls final result and awards points automatically

ENV (Render)
------------
Required:
- BOT_TOKEN

Recommended:
- ADMIN_ID

Autosync:
- SYNC_ENABLED=1
- SYNC_INTERVAL=3600            # seconds (default 3600)
- SYNC_LOOKAHEAD_DAYS=1         # today..today+N (default 1)

Football (football-data.org):
- FOOTBALL_ENABLED=1
- FOOTBALL_DATA_TOKEN=xxx       # required for football sync/results
- FOOTBALL_COMPETITIONS=PL,CL,PD,SA,BL1,FL1   # optional
- FOOTBALL_BASE=https://api.football-data.org/v4

NHL:
- NHL_ENABLED=1                 # public endpoints, no token

Auto-results:
- AUTO_RESULTS_ENABLED=1
- AUTO_RESULTS_INTERVAL=300     # seconds (default 300)
- AUTO_RESULTS_MIN_AGE_MIN=20   # don't try too early

Keep-alive (optional, for free hosting sleep):
- KEEPALIVE_ENABLED=1
- KEEPALIVE_URL=https://your-service.onrender.com/
- KEEPALIVE_INTERVAL=300

Scoring:
- POINTS_FOR_CORRECT=3
"""

import asyncio
import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
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
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or "0")

DB_PATH = os.getenv("DB_PATH", "bot.db")

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

# keepalive
KEEPALIVE_ENABLED = os.getenv("KEEPALIVE_ENABLED", "0") == "1"
KEEPALIVE_URL = (os.getenv("KEEPALIVE_URL") or "").strip()
KEEPALIVE_INTERVAL = int(os.getenv("KEEPALIVE_INTERVAL", "300") or "300")

# scoring
POINTS_FOR_CORRECT = int(os.getenv("POINTS_FOR_CORRECT", "3") or "3")

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper())
logger = logging.getLogger("bot")

bot = Bot(
    BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
dp = Dispatcher()

# =========================
# DB
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
        # base matches table (legacy-compatible)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS matches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT,
            start_time TEXT,
            status TEXT DEFAULT 'open',
            result TEXT
        )
        """)
        # migrations / new columns
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
BTN_SYNC = "🔄 Sync now"

SPORT_PRETTY = {
    "football": "⚽ Футбол",
    "hockey": "🏒 Хоккей",
    "nhl": "🏒 Хоккей",
    "esports": "🎮 Киберспорт",
    "other": "🏟 Другое",
    "manual": "📝 Ручные",
}

def main_menu(match_ids: Optional[List[int]] = None, admin: bool = False) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text=BTN_ACTIVE)],
        [KeyboardButton(text=BTN_LB), KeyboardButton(text=BTN_PROFILE)],
        [KeyboardButton(text=BTN_HELP)],
    ]
    if admin:
        rows.append([KeyboardButton(text=BTN_SYNC)])
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
        ], [
            InlineKeyboardButton(text="📊 Статистика", callback_data=f"stats:{match_id}"),
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
            cur.execute("SELECT id, title, start_time_utc, league, sport FROM matches WHERE status='open' ORDER BY COALESCE(start_time_utc, start_time) ASC, id DESC")
        else:
            cur.execute(
                "SELECT id, title, start_time_utc, league, sport FROM matches WHERE status='open' AND COALESCE(NULLIF(LOWER(sport), ''), 'other')=? ORDER BY COALESCE(start_time_utc, start_time) ASC, id DESC",
                (sport,),
            )
        return cur.fetchall()

def get_match(mid: int) -> Optional[sqlite3.Row]:
    with db() as con:
        cur = con.cursor()
        cur.execute("SELECT * FROM matches WHERE id=?", (mid,))
        return cur.fetchone()

def match_stats(match_id: int) -> Dict[str, int]:
    counts = {"1": 0, "X": 0, "2": 0}
    with db() as con:
        cur = con.cursor()
        cur.execute("SELECT pick, COUNT(*) c FROM votes WHERE match_id=? GROUP BY pick", (match_id,))
        for r in cur.fetchall():
            p = r["pick"]
            if p in counts:
                counts[p] += int(r["c"])
    return counts

# =========================
# AUTOSYNC MODELS
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

# =========================
# HTTP helper
# =========================
async def http_json(session: aiohttp.ClientSession, url: str, headers: Optional[Dict[str, str]] = None, timeout_s: int = 20) -> Any:
    async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=timeout_s)) as resp:
        txt = await resp.text()
        if resp.status >= 400:
            raise RuntimeError(f"HTTP {resp.status} {url}: {txt[:300]}")
        return await resp.json()

# =========================
# FOOTBALL sync/results
# =========================
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

# =========================
# NHL sync/results
# =========================
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
            # Teams
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

# =========================
# AUTOSYNC UPSERT
# =========================
def upsert_matches(matches: List[SyncedMatch]) -> Tuple[int, int]:
    inserted = 0
    updated = 0
    with db() as con:
        cur = con.cursor()
        for m in matches:
            # ensure unique key
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

async def autosync_once(notify_admin: bool = False) -> str:
    start = now_utc().replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=max(0, SYNC_LOOKAHEAD_DAYS))
    report: List[str] = []

    async with aiohttp.ClientSession() as session:
        allm: List[SyncedMatch] = []
        if FOOTBALL_ENABLED:
            fm = await football_list(session, start, end)
            allm.extend(fm)
            report.append(f"Football fetched: {len(fm)}")
        if NHL_ENABLED:
            nm = await nhl_list(session, start, end)
            allm.extend(nm)
            report.append(f"NHL fetched: {len(nm)}")
        ins, upd = upsert_matches(allm)
        report.append(f"DB: inserted={ins} updated={upd}")

    text = "Sync report:\n" + "\n".join(report)
    logger.info(text.replace("\n", " | "))
    if notify_admin and ADMIN_ID:
        try:
            await bot.send_message(ADMIN_ID, text)
        except Exception:
            pass
    return text

# =========================
# HANDLERS
# =========================
@dp.message(Command("start"))
async def start(m: Message):
    await m.answer("Бот запущен ✅\nЖми кнопки снизу.", reply_markup=main_menu(admin=is_admin(m.from_user.id)))

@dp.message(F.text == BTN_HELP)
async def help_btn(m: Message):
    await m.answer(
        "ℹ️ Помощь\n\n"
        "1) «⚽ Активные матчи» → выбери спорт → выбери матч (#id)\n"
        "2) Выбери исход 1 / X / 2\n\n"
        "Если включён авто-итог — бот сам начислит очки после финала.",
        reply_markup=main_menu(admin=is_admin(m.from_user.id)),
    )

@dp.message(F.text == BTN_SYNC)
async def sync_btn(m: Message):
    if not is_admin(m.from_user.id):
        return
    rep = await autosync_once(notify_admin=False)
    await m.answer(rep)

@dp.message(Command("sync_now"))
async def sync_cmd(m: Message):
    if not is_admin(m.from_user.id):
        await m.answer("Только админ.")
        return
    rep = await autosync_once(notify_admin=False)
    await m.answer(rep)

@dp.message(F.text == BTN_ACTIVE)
async def active_matches(m: Message):
    sports = get_open_sports()
    if not sports:
        await m.answer("Активных матчей нет.", reply_markup=main_menu(admin=is_admin(m.from_user.id)))
        return
    await m.answer("Выбери вид спорта 👇", reply_markup=main_menu(admin=is_admin(m.from_user.id)))
    await m.answer("Категории:", reply_markup=sport_categories_kb(sports))

@dp.callback_query(F.data.startswith("sport:"))
async def sport_pick(cb: CallbackQuery):
    sport = cb.data.split(":", 1)[1].strip().lower()
    rows = get_open_matches_by_sport(sport)

    if not rows:
        await cb.answer("В этой категории матчей нет.", show_alert=True)
        return

    ids = [int(r["id"]) for r in rows][:40]

    def line(r: sqlite3.Row) -> str:
        st = r["start_time_utc"] or r["start_time"] or ""
        league = (r["league"] or "").strip()
        prefix = f"[{league}] " if league else ""
        return f"#{r['id']} {prefix}{r['title']} (UTC {st})"

    lines = [line(r) for r in rows[:40]]

    header = "Активные матчи: ВСЕ" if sport == "all" else f"Активные матчи: {SPORT_PRETTY.get(sport, sport)}"
    text = header + ":\n\n" + "\n".join(lines)

    await cb.message.answer(text, reply_markup=main_menu(match_ids=ids, admin=is_admin(cb.from_user.id)))
    await cb.answer()

@dp.message(F.text.startswith("#"))
async def open_match(m: Message):
    try:
        match_id = int(m.text.strip().replace("#", ""))
    except ValueError:
        return

    match = get_match(match_id)
    if not match or match["status"] != "open":
        await m.answer("Матч не найден или уже закрыт.", reply_markup=main_menu(admin=is_admin(m.from_user.id)))
        return

    stats = match_stats(match_id)
    await m.answer(
        f"Матч #{match_id}:\n<b>{match['title']}</b>\n\n"
        f"Голоса: 1={stats['1']}  X={stats['X']}  2={stats['2']}\n\n"
        "Выбери исход 1X2:",
        reply_markup=match_actions_kb(match_id),
    )

@dp.callback_query(F.data.startswith("stats:"))
async def stats_cb(cb: CallbackQuery):
    match_id = int(cb.data.split(":")[1])
    s = match_stats(match_id)
    await cb.answer(f"1={s['1']} X={s['X']} 2={s['2']}", show_alert=True)

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

@dp.message(F.text == BTN_LB)
async def leaderboard(m: Message):
    with db() as con:
        cur = con.cursor()
        cur.execute("SELECT user_id, points FROM scores ORDER BY points DESC LIMIT 10")
        rows = cur.fetchall()
    if not rows:
        await m.answer("Лидерборд пуст.")
        return
    text = "🏆 Лидерборд:\n\n" + "\n".join([f"{i+1}. {r['user_id']} — {r['points']} очков" for i, r in enumerate(rows)])
    await m.answer(text)

@dp.message(F.text == BTN_PROFILE)
async def profile(m: Message):
    with db() as con:
        cur = con.cursor()
        cur.execute("SELECT points FROM scores WHERE user_id=?", (m.from_user.id,))
        r = cur.fetchone()
    pts = int(r["points"]) if r else 0
    await m.answer(f"👤 Профиль\n\nОчки: <b>{pts}</b>")

# =========================
# BACKGROUND LOOPS
# =========================
async def autosync_loop():
    while True:
        try:
            if SYNC_ENABLED:
                await autosync_once(notify_admin=False)
        except Exception as e:
            logger.exception("autosync_loop error: %s", e)
        await asyncio.sleep(max(300, SYNC_INTERVAL))

async def auto_results_loop():
    """
    Checks open synced matches that are old enough and fetches their final result from APIs.
    When finished, sets matches.result and awards points, then closes match.
    """
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                await asyncio.sleep(max(30, AUTO_RESULTS_INTERVAL))
                if not AUTO_RESULTS_ENABLED:
                    continue

                cutoff = now_utc() - timedelta(minutes=max(0, AUTO_RESULTS_MIN_AGE_MIN))

                with db() as con:
                    cur = con.cursor()
                    cur.execute("""
                        SELECT id, source, external_id, title, start_time_utc
                        FROM matches
                        WHERE status='open' AND source IS NOT NULL AND external_id IS NOT NULL
                              AND COALESCE(start_time_utc, start_time) <= ?
                        ORDER BY COALESCE(start_time_utc, start_time) ASC
                        LIMIT 60
                    """, (iso(cutoff),))
                    candidates = cur.fetchall()

                for m in candidates:
                    mid = int(m["id"])
                    source = (m["source"] or "").lower()
                    ext = (m["external_id"] or "").strip()
                    if not ext:
                        continue

                    fin: Optional[FinishedInfo] = None
                    if source == "football":
                        try:
                            fin = await football_result(session, ext)
                        except Exception as e:
                            logger.warning("football_result failed ext=%s err=%s", ext, e)
                    elif source == "nhl":
                        try:
                            fin = await nhl_result(session, ext)
                        except Exception as e:
                            logger.warning("nhl_result failed ext=%s err=%s", ext, e)

                    if not fin:
                        continue

                    # Apply scoring + close
                    with db() as con:
                        cur = con.cursor()

                        # save result for transparency
                        cur.execute("UPDATE matches SET result=? WHERE id=? AND status='open'", (fin.result_1x2, mid))

                        # score
                        cur.execute("SELECT user_id, pick FROM votes WHERE match_id=?", (mid,))
                        votes = cur.fetchall()
                        for v in votes:
                            uid = int(v["user_id"])
                            if v["pick"] == fin.result_1x2:
                                cur.execute("INSERT OR IGNORE INTO scores(user_id, points) VALUES(?,0)", (uid,))
                                cur.execute("UPDATE scores SET points = points + ? WHERE user_id=?", (POINTS_FOR_CORRECT, uid))

                        # close
                        cur.execute("UPDATE matches SET status='closed' WHERE id=?", (mid,))
                        con.commit()

                    # notify admin
                    if ADMIN_ID:
                        try:
                            await bot.send_message(ADMIN_ID, f"Auto-result: #{mid} = {fin.result_1x2}")
                        except Exception:
                            pass

            except Exception as e:
                logger.exception("auto_results_loop error: %s", e)

async def keepalive_loop():
    if not KEEPALIVE_ENABLED or not KEEPALIVE_URL:
        return
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                async with session.get(KEEPALIVE_URL, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    await resp.text()
            except Exception as e:
                logger.warning("keepalive ping failed: %s", e)
            await asyncio.sleep(max(60, KEEPALIVE_INTERVAL))

# =========================
# MAIN
# =========================
async def main():
    init_db()

    # start background
    if SYNC_ENABLED:
        asyncio.create_task(autosync_loop())
    if AUTO_RESULTS_ENABLED:
        asyncio.create_task(auto_results_loop())
    if KEEPALIVE_ENABLED and KEEPALIVE_URL:
        asyncio.create_task(keepalive_loop())

    # Polling auto-restart (stability)
    while True:
        try:
            await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
            break
        except Exception as e:
            logger.exception("Polling crashed, restarting in 5 seconds: %s", e)
            await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(main())
