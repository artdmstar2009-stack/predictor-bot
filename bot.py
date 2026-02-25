# -*- coding: utf-8 -*-
"""
Презентабельный Telegram бот-прогнозист (aiogram v3.7+) — только исходы 1X2
========================================================================

Что сделано "красиво":
✅ Главное меню — компактное, без огромного списка кнопок
✅ "⚡ Активные матчи" → сначала выбор спорта (Футбол / Хоккей / Киберспорт / Другое / Все)
✅ Внутри спорта — аккуратный список матчей кнопками (Inline), с пагинацией и кнопкой «⬅️ Назад»
✅ Карточка матча — понятная, со статистикой голосов и кнопками 1 / X / 2
✅ Лидерборд и профиль — с отображением @username (если есть)

Функции:
- Автосинк матчей:
  - ⚽ football-data.org (нужен FOOTBALL_DATA_TOKEN)
  - 🏒 NHL (публичные endpoints)
- Авто-итоги:
  - Бот сам подтягивает финальный результат и начисляет очки
- Стабильность:
  - polling авто-перезапуск (если Telegram/сеть глюкнули)
  - Для Render Web Service: встроен мини web-server на 0.0.0.0:$PORT ("/" и "/health" -> "ok")
  - (Рекомендуется: запускать как Background Worker, но и Web Service ок)

ENV (Render):
- BOT_TOKEN (required)
- ADMIN_ID (optional)

Port binding (если Web Service):
- PORT (Render выставляет сам)

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
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
)

# =========================
# CONFIG
# =========================

BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or "0")
DB_PATH = os.getenv("DB_PATH", "bot.db")

PORT = int(os.getenv("PORT", "0") or "0")  # Render Web Service binds this

SYNC_ENABLED = os.getenv("SYNC_ENABLED", "1") == "1"
SYNC_INTERVAL = int(os.getenv("SYNC_INTERVAL", "3600") or "3600")
SYNC_LOOKAHEAD_DAYS = int(os.getenv("SYNC_LOOKAHEAD_DAYS", "1") or "1")

FOOTBALL_ENABLED = os.getenv("FOOTBALL_ENABLED", "1") == "1"
FOOTBALL_DATA_TOKEN = (os.getenv("FOOTBALL_DATA_TOKEN") or "").strip()
FOOTBALL_COMPETITIONS = [c.strip() for c in (os.getenv("FOOTBALL_COMPETITIONS") or "PL,CL,PD,SA,BL1,FL1").split(",") if c.strip()]
FOOTBALL_BASE = (os.getenv("FOOTBALL_BASE") or "https://api.football-data.org/v4").rstrip("/")

NHL_ENABLED = os.getenv("NHL_ENABLED", "1") == "1"

AUTO_RESULTS_ENABLED = os.getenv("AUTO_RESULTS_ENABLED", "1") == "1"
AUTO_RESULTS_INTERVAL = int(os.getenv("AUTO_RESULTS_INTERVAL", "300") or "300")
AUTO_RESULTS_MIN_AGE_MIN = int(os.getenv("AUTO_RESULTS_MIN_AGE_MIN", "20") or "20")

POINTS_FOR_CORRECT = int(os.getenv("POINTS_FOR_CORRECT", "3") or "3")

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL)
logger = logging.getLogger("predictor_bot")

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

def is_admin(uid: int) -> bool:
    return ADMIN_ID != 0 and uid == ADMIN_ID

def init_db() -> None:
    with db() as con:
        cur = con.cursor()

        # users for pretty names
        cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            updated_at TEXT
        )
        """)

        # matches legacy-compatible + migrations
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

def upsert_user(user_id: int, username: Optional[str], first_name: Optional[str], last_name: Optional[str]) -> None:
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
        """, (user_id, username or "", first_name or "", last_name or "", iso(now_utc())))
        cur.execute("INSERT OR IGNORE INTO scores(user_id, points) VALUES(?,0)", (user_id,))
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
# UI / TEXT
# =========================

BTN_ACTIVE = "⚡ Активные матчи"
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

PER_PAGE = 10

def main_menu(admin: bool = False) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text=BTN_ACTIVE)],
        [KeyboardButton(text=BTN_LB), KeyboardButton(text=BTN_PROFILE)],
        [KeyboardButton(text=BTN_HELP)],
    ]
    if admin:
        rows.append([KeyboardButton(text=BTN_SYNC)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)

def ikb_match_pick(match_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="1", callback_data=f"pick:{match_id}:1"),
            InlineKeyboardButton(text="X", callback_data=f"pick:{match_id}:X"),
            InlineKeyboardButton(text="2", callback_data=f"pick:{match_id}:2"),
        ],
        [InlineKeyboardButton(text="📊 Голоса", callback_data=f"stats:{match_id}")],
        [InlineKeyboardButton(text="⬅️ Назад к матчам", callback_data="back:matches")],
    ])

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
        # compact label
        title = (r["title"] or "").strip()
        if len(title) > 32:
            title = title[:32] + "…"
        rows.append([InlineKeyboardButton(text=f"#{mid} — {title}", callback_data=f"mopen:{mid}")])

    # pagination row
    nav: List[InlineKeyboardButton] = []
    max_page = max(0, (total - 1) // PER_PAGE)
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"sport:{sport}:{page-1}"))
    nav.append(InlineKeyboardButton(text=f"{page+1}/{max_page+1}", callback_data="noop"))
    if page < max_page:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"sport:{sport}:{page+1}"))
    if nav:
        rows.append(nav)

    rows.append([InlineKeyboardButton(text="⬅️ Назад к видам спорта", callback_data="back:sports")])
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

def get_match(mid: int) -> Optional[sqlite3.Row]:
    with db() as con:
        return con.execute("SELECT * FROM matches WHERE id=?", (mid,)).fetchone()

def match_stats(match_id: int) -> Dict[str, int]:
    counts = {"1": 0, "X": 0, "2": 0}
    with db() as con:
        for r in con.execute("SELECT pick, COUNT(*) c FROM votes WHERE match_id=? GROUP BY pick", (match_id,)).fetchall():
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
            raise RuntimeError(f"HTTP {resp.status} {url}: {txt[:200]}")
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
            report.append(f"Football: {len(fm)}")
        if NHL_ENABLED:
            nm = await nhl_list(session, start, end)
            allm.extend(nm)
            report.append(f"NHL: {len(nm)}")
        ins, upd = upsert_matches(allm)
        report.append(f"DB +{ins} / ~{upd}")

    text = "Sync: " + " | ".join(report) if report else "Sync: nothing"
    logger.info(text)
    return text

# =========================
# WEB SERVER (Render Web Service)
# =========================

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

# =========================
# HANDLERS
# =========================

async def safe_answer(obj: Message | CallbackQuery, text: str, **kwargs: Any) -> None:
    # small helper to avoid crashes on message length etc
    try:
        if isinstance(obj, Message):
            await obj.answer(text, **kwargs)
        else:
            await obj.message.answer(text, **kwargs)
    except Exception as e:
        logger.warning("send failed: %s", e)

@dp.message(Command("start"))
async def start(m: Message):
    upsert_user(m.from_user.id, m.from_user.username, m.from_user.first_name, m.from_user.last_name)
    await m.answer(
        "👋 Привет! Это бот прогнозов.\n\n"
        "Жми <b>⚡ Активные матчи</b>, выбирай спорт → матч → исход 1X2.",
        reply_markup=main_menu(admin=is_admin(m.from_user.id)),
    )

@dp.message(F.text == BTN_HELP)
async def help_btn(m: Message):
    upsert_user(m.from_user.id, m.from_user.username, m.from_user.first_name, m.from_user.last_name)
    await m.answer(
        "ℹ️ <b>Помощь</b>\n\n"
        "• <b>⚡ Активные матчи</b> → спорт → матч → 1/X/2\n"
        f"• За верный исход: <b>+{POINTS_FOR_CORRECT}</b> очка\n\n"
        "Если матч завершён — бот сам подтянет результат и начислит очки.",
        reply_markup=main_menu(admin=is_admin(m.from_user.id)),
    )

@dp.message(F.text == BTN_SYNC)
async def sync_btn(m: Message):
    upsert_user(m.from_user.id, m.from_user.username, m.from_user.first_name, m.from_user.last_name)
    if not is_admin(m.from_user.id):
        return
    rep = await autosync_once()
    await m.answer(rep, reply_markup=main_menu(admin=True))

@dp.message(Command("sync_now"))
async def sync_cmd(m: Message):
    upsert_user(m.from_user.id, m.from_user.username, m.from_user.first_name, m.from_user.last_name)
    if not is_admin(m.from_user.id):
        await m.answer("Только админ.")
        return
    rep = await autosync_once()
    await m.answer(rep, reply_markup=main_menu(admin=True))

@dp.message(F.text == BTN_ACTIVE)
async def active_matches(m: Message):
    upsert_user(m.from_user.id, m.from_user.username, m.from_user.first_name, m.from_user.last_name)

    sports = get_open_sports()
    if not sports:
        await m.answer("Пока нет активных матчей.", reply_markup=main_menu(admin=is_admin(m.from_user.id)))
        return

    await m.answer(
        "⚡ <b>Активные матчи</b>\n\n"
        "Выбери вид спорта 👇",
        reply_markup=main_menu(admin=is_admin(m.from_user.id)),
    )
    await m.answer("Категории:", reply_markup=ikb_sports(sports))

@dp.callback_query(F.data.startswith("sport:"))
async def cb_sport(cb: CallbackQuery):
    upsert_user(cb.from_user.id, cb.from_user.username, cb.from_user.first_name, cb.from_user.last_name)

    try:
        _, sport, page_s = cb.data.split(":")
        page = int(page_s)
    except Exception:
        await cb.answer("Ошибка категории.", show_alert=True)
        return

    sport = (sport or "all").lower()
    total = count_open_matches(sport)
    if total <= 0:
        await cb.answer("В этой категории матчей нет.", show_alert=True)
        return

    max_page = max(0, (total - 1) // PER_PAGE)
    if page < 0:
        page = 0
    if page > max_page:
        page = max_page

    items = get_open_matches_page(sport, page)

    header = "📋 Все матчи" if sport == "all" else SPORT_PRETTY.get(sport, sport)
    lines: List[str] = []
    for r in items:
        st = (r["start_time_utc"] or r["start_time"] or "").replace("T", " ").replace("+00:00", " UTC")
        league = (r["league"] or "").strip()
        prefix = f"[{league}] " if league else ""
        lines.append(f"• <b>#{r['id']}</b> {prefix}{r['title']}\n  <i>{st}</i>")

    text = f"{header}\n\n" + "\n".join(lines)
    await cb.message.answer(text, reply_markup=ikb_matches_list(sport, page, items, total))
    await cb.answer()

@dp.callback_query(F.data == "back:sports")
async def cb_back_sports(cb: CallbackQuery):
    sports = get_open_sports()
    if not sports:
        await cb.answer("Матчей нет.", show_alert=True)
        return
    await cb.message.answer("Категории:", reply_markup=ikb_sports(sports))
    await cb.answer()

@dp.callback_query(F.data == "back:matches")
async def cb_back_matches(cb: CallbackQuery):
    # Just show sports again — simplest and clean
    sports = get_open_sports()
    if not sports:
        await cb.answer("Матчей нет.", show_alert=True)
        return
    await cb.message.answer("⬅️ Назад. Выбери спорт:", reply_markup=ikb_sports(sports))
    await cb.answer()

@dp.callback_query(F.data == "noop")
async def cb_noop(cb: CallbackQuery):
    await cb.answer()

@dp.callback_query(F.data.startswith("mopen:"))
async def cb_open_match(cb: CallbackQuery):
    upsert_user(cb.from_user.id, cb.from_user.username, cb.from_user.first_name, cb.from_user.last_name)

    try:
        match_id = int(cb.data.split(":")[1])
    except Exception:
        await cb.answer("Ошибка матча.", show_alert=True)
        return

    match = get_match(match_id)
    if not match:
        await cb.answer("Матч не найден.", show_alert=True)
        return
    if match["status"] != "open":
        await cb.answer("Матч уже закрыт.", show_alert=True)
        return

    stats = match_stats(match_id)
    my_pick = None
    with db() as con:
        r = con.execute("SELECT pick FROM votes WHERE user_id=? AND match_id=?", (cb.from_user.id, match_id)).fetchone()
        my_pick = r["pick"] if r else None

    st = (match["start_time_utc"] or match["start_time"] or "").replace("T", " ").replace("+00:00", " UTC")
    sport = SPORT_PRETTY.get((match["sport"] or "other").lower(), match["sport"] or "other")
    league = (match["league"] or "").strip()

    text = (
        f"🏟 <b>Матч #{match_id}</b>\n"
        f"{match['title']}\n\n"
        f"• Спорт: <b>{sport}</b>\n"
        f"• Лига: <b>{league or '—'}</b>\n"
        f"• Старт: <i>{st}</i>\n\n"
        f"📊 Голоса: 1={stats['1']}  X={stats['X']}  2={stats['2']}\n"
        f"🎯 Твой выбор: <b>{my_pick or '—'}</b>\n\n"
        "Выбери исход 1X2:"
    )

    await cb.message.answer(text, reply_markup=ikb_match_pick(match_id))
    await cb.answer()

@dp.callback_query(F.data.startswith("stats:"))
async def cb_stats(cb: CallbackQuery):
    match_id = int(cb.data.split(":")[1])
    s = match_stats(match_id)
    await cb.answer(f"1={s['1']}  X={s['X']}  2={s['2']}", show_alert=True)

@dp.callback_query(F.data.startswith("pick:"))
async def cb_pick(cb: CallbackQuery):
    upsert_user(cb.from_user.id, cb.from_user.username, cb.from_user.first_name, cb.from_user.last_name)

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
    if not match or match["status"] != "open":
        await cb.answer("Матч закрыт.", show_alert=True)
        return

    with db() as con:
        con.execute("INSERT OR REPLACE INTO votes(user_id, match_id, pick) VALUES(?,?,?)",
                    (cb.from_user.id, match_id, pick))
        con.commit()

    await cb.answer("✅ Принято!", show_alert=True)

@dp.message(F.text == BTN_LB)
async def leaderboard(m: Message):
    upsert_user(m.from_user.id, m.from_user.username, m.from_user.first_name, m.from_user.last_name)

    with db() as con:
        rows = con.execute("SELECT user_id, points FROM scores ORDER BY points DESC, user_id ASC LIMIT 10").fetchall()

    if not rows:
        await m.answer("Лидерборд пуст.")
        return

    lines = []
    for i, r in enumerate(rows, 1):
        lines.append(f"{i}. {pretty_user(int(r['user_id']))} — <b>{int(r['points'])}</b>")

    await m.answer("🏆 <b>Лидерборд</b>\n\n" + "\n".join(lines), reply_markup=main_menu(admin=is_admin(m.from_user.id)))

@dp.message(F.text == BTN_PROFILE)
async def profile(m: Message):
    upsert_user(m.from_user.id, m.from_user.username, m.from_user.first_name, m.from_user.last_name)

    with db() as con:
        r = con.execute("SELECT points FROM scores WHERE user_id=?", (m.from_user.id,)).fetchone()
    pts = int(r["points"]) if r else 0

    await m.answer(
        f"👤 <b>Профиль</b>\n\n"
        f"Игрок: {pretty_user(m.from_user.id)}\n"
        f"Очки: <b>{pts}</b>\n",
        reply_markup=main_menu(admin=is_admin(m.from_user.id)),
    )

@dp.message()
async def fallback(m: Message):
    upsert_user(m.from_user.id, m.from_user.username, m.from_user.first_name, m.from_user.last_name)
    await m.answer("Используй кнопки меню 👇", reply_markup=main_menu(admin=is_admin(m.from_user.id)))

# =========================
# BACKGROUND LOOPS
# =========================

async def autosync_loop():
    while True:
        try:
            if SYNC_ENABLED:
                await autosync_once()
        except Exception as e:
            logger.exception("autosync_loop error: %s", e)
        await asyncio.sleep(max(300, SYNC_INTERVAL))

async def auto_results_loop():
    """
    For finished matches we:
    - fetch final result from source API
    - write matches.result
    - award points
    - close match
    """
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
                        LIMIT 60
                    """, (cutoff_s,)).fetchall()

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
                            logger.warning("football_result failed: %s", e)
                    elif source == "nhl":
                        try:
                            fin = await nhl_result(session, ext)
                        except Exception as e:
                            logger.warning("nhl_result failed: %s", e)

                    if not fin:
                        continue

                    with db() as con:
                        cur = con.cursor()
                        cur.execute("UPDATE matches SET result=? WHERE id=? AND status='open'", (fin.result_1x2, mid))

                        votes = cur.execute("SELECT user_id, pick FROM votes WHERE match_id=?", (mid,)).fetchall()
                        for v in votes:
                            if v["pick"] == fin.result_1x2:
                                cur.execute("INSERT OR IGNORE INTO scores(user_id, points) VALUES(?,0)", (int(v["user_id"]),))
                                cur.execute("UPDATE scores SET points = points + ? WHERE user_id=?",
                                            (POINTS_FOR_CORRECT, int(v["user_id"])))
                        cur.execute("UPDATE matches SET status='closed' WHERE id=?", (mid,))
                        con.commit()

                    if ADMIN_ID:
                        try:
                            await bot.send_message(ADMIN_ID, f"✅ Итог: матч #{mid} = {fin.result_1x2}")
                        except Exception:
                            pass

            except Exception as e:
                logger.exception("auto_results_loop error: %s", e)

async def heartbeat_loop():
    while True:
        logger.info("heartbeat: alive")
        await asyncio.sleep(900)

# =========================
# MAIN
# =========================

async def main():
    init_db()

    # Web server for Render Web Service
    asyncio.create_task(start_web_server())

    # Background tasks
    if SYNC_ENABLED:
        asyncio.create_task(autosync_loop())
    if AUTO_RESULTS_ENABLED:
        asyncio.create_task(auto_results_loop())
    asyncio.create_task(heartbeat_loop())

    # Polling auto-restart
    while True:
        try:
            await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
            break
        except Exception as e:
            logger.exception("Polling crashed, restarting in 5 seconds: %s", e)
            await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(main())
