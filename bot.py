
import asyncio
import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Optional, Dict, Tuple, List
from urllib.parse import urlencode, urlparse, parse_qs
from urllib.request import Request, urlopen

from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder


# ===================== Async helpers =====================
async def to_thread_timeout(func, *args, timeout: int = 35, **kwargs):
    return await asyncio.wait_for(asyncio.to_thread(func, *args, **kwargs), timeout=timeout)

async def adb(func, *args, timeout: int = 25, **kwargs):
    return await to_thread_timeout(func, *args, timeout=timeout, **kwargs)


# ===================== ENV =====================
TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
DB_PATH = (os.getenv("DB_PATH") or "predictor.db").strip()

try:
    ADMIN_ID = int((os.getenv("ADMIN_ID") or "0").strip())
except Exception:
    ADMIN_ID = 0

FOOTBALL_DATA_TOKEN = (os.getenv("FOOTBALL_DATA_TOKEN") or "").strip()
FD_COMPETITIONS = (os.getenv("FD_COMPETITIONS") or "").strip()  # "PL,CL,PD"

AUTO_SYNC_ENABLED = (os.getenv("AUTO_SYNC_ENABLED") or "0").strip() == "1"
AUTO_SYNC_TZ = (os.getenv("AUTO_SYNC_TZ") or "").strip()  # e.g. "Europe/London"
try:
    AUTO_SYNC_HOUR_LOCAL = int((os.getenv("AUTO_SYNC_HOUR_LOCAL") or "4").strip())
except Exception:
    AUTO_SYNC_HOUR_LOCAL = 4
try:
    AUTO_SYNC_HOUR_UTC = int((os.getenv("AUTO_SYNC_HOUR_UTC") or "8").strip())
except Exception:
    AUTO_SYNC_HOUR_UTC = 8

CRON_SECRET = (os.getenv("CRON_SECRET") or "").strip()

# close voting N seconds before kickoff (default 120s = 2 min)
try:
    PREDICTION_CLOSE_SECONDS = int((os.getenv("PREDICTION_CLOSE_SECONDS") or "120").strip())
except Exception:
    PREDICTION_CLOSE_SECONDS = 120


# ===================== Render Web Service port binding =====================
CRON_REQUESTED = False

class DummyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        global CRON_REQUESTED
        parsed = urlparse(self.path)

        if parsed.path == "/cron/sync":
            qs = parse_qs(parsed.query or "")
            key = (qs.get("key", [""])[0] or "").strip()

            if not CRON_SECRET:
                self.send_response(400); self.end_headers()
                self.wfile.write(b"CRON_SECRET not set")
                return
            if key != CRON_SECRET:
                self.send_response(403); self.end_headers()
                self.wfile.write(b"Forbidden")
                return

            CRON_REQUESTED = True
            self.send_response(200); self.end_headers()
            self.wfile.write(b"OK: sync requested")
            return

        self.send_response(200); self.end_headers()
        self.wfile.write(b"OK")

def run_http():
    port = int(os.environ.get("PORT", 10000))
    HTTPServer(("0.0.0.0", port), DummyHandler).serve_forever()

threading.Thread(target=run_http, daemon=True).start()


# ===================== Time helpers =====================
def now_iso() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")

def today_key_utc() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d")

def current_season() -> str:
    return datetime.utcnow().strftime("%Y-%m")

def is_admin(uid: int) -> bool:
    return ADMIN_ID != 0 and int(uid) == int(ADMIN_ID)

def parse_utc(iso_utc: str) -> Optional[datetime]:
    try:
        if iso_utc.endswith("Z"):
            iso_utc = iso_utc[:-1] + "+00:00"
        dt = datetime.fromisoformat(iso_utc)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


# ===================== DB =====================
def db():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA synchronous=NORMAL;")
    return con

def init_db():
    with db() as con:
        con.execute("""
        CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            created_at TEXT,
            last_seen TEXT
        )
        """)
        con.execute("""
        CREATE TABLE IF NOT EXISTS matches(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            external_id TEXT,
            kickoff_utc TEXT
        )
        """)
        con.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_matches_external_id ON matches(external_id)")
        con.execute("""
        CREATE TABLE IF NOT EXISTS votes(
            match_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            username TEXT,
            choice TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(match_id, user_id)
        )
        """)
        con.execute("""
        CREATE TABLE IF NOT EXISTS results(
            match_id INTEGER PRIMARY KEY,
            result TEXT NOT NULL,
            scored INTEGER NOT NULL DEFAULT 0
        )
        """)
        con.execute("""
        CREATE TABLE IF NOT EXISTS scores_season(
            season TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            username TEXT,
            points INTEGER NOT NULL DEFAULT 0,
            correct INTEGER NOT NULL DEFAULT 0,
            total INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(season, user_id)
        )
        """)
        con.execute("""
        CREATE TABLE IF NOT EXISTS pending_fixtures(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ext_id TEXT NOT NULL UNIQUE,
            day TEXT NOT NULL,
            title TEXT NOT NULL,
            kickoff_utc TEXT,
            competition TEXT,
            created_at TEXT NOT NULL
        )
        """)
        con.commit()

def upsert_user_db(u):
    with db() as con:
        row = con.execute("SELECT 1 FROM users WHERE user_id=?", (u.id,)).fetchone()
        if row:
            con.execute("UPDATE users SET username=?, first_name=?, last_name=?, last_seen=? WHERE user_id=?",
                        (u.username, u.first_name, u.last_name, now_iso(), u.id))
        else:
            con.execute("INSERT INTO users(user_id, username, first_name, last_name, created_at, last_seen) VALUES(?,?,?,?,?,?)",
                        (u.id, u.username, u.first_name, u.last_name, now_iso(), now_iso()))
        con.commit()

def ensure_score_row_db(season: str, uid: int, username: Optional[str]):
    with db() as con:
        row = con.execute("SELECT 1 FROM scores_season WHERE season=? AND user_id=?", (season, uid)).fetchone()
        if not row:
            con.execute("INSERT INTO scores_season(season,user_id,username,points,correct,total) VALUES(?,?,?,?,?,?)",
                        (season, uid, username, 0, 0, 0))
        else:
            con.execute("UPDATE scores_season SET username=COALESCE(?, username) WHERE season=? AND user_id=?",
                        (username, season, uid))
        con.commit()

def get_open_matches_db():
    with db() as con:
        return con.execute("SELECT id,title,status,kickoff_utc FROM matches WHERE status='open' ORDER BY id DESC").fetchall()

def get_match_db(mid: int):
    with db() as con:
        return con.execute("SELECT id,title,status,external_id,kickoff_utc FROM matches WHERE id=?", (mid,)).fetchone()

def close_match_db(mid: int):
    with db() as con:
        con.execute("UPDATE matches SET status='closed' WHERE id=?", (mid,))
        con.commit()

def match_exists_by_ext_db(ext_id: str) -> bool:
    with db() as con:
        return con.execute("SELECT 1 FROM matches WHERE external_id=?", (ext_id,)).fetchone() is not None

def create_match_db(title: str, external_id: Optional[str], kickoff_utc: Optional[str]) -> int:
    with db() as con:
        cur = con.execute("INSERT INTO matches(title,status,external_id,kickoff_utc) VALUES(?, 'open', ?, ?)",
                          (title, external_id, kickoff_utc))
        con.commit()
        return int(cur.lastrowid)

def save_vote_db(mid: int, uid: int, username: str, choice: str):
    with db() as con:
        con.execute("INSERT OR REPLACE INTO votes(match_id,user_id,username,choice,created_at) VALUES(?,?,?,?,?)",
                    (mid, uid, username, choice, now_iso()))
        con.commit()

def fetch_votes_stats_db(mid: int) -> Dict[str, int]:
    with db() as con:
        rows = con.execute("SELECT choice, COUNT(*) as c FROM votes WHERE match_id=? GROUP BY choice", (mid,)).fetchall()
        return {r["choice"]: int(r["c"]) for r in rows}

def fetch_my_votes_db(uid: int):
    with db() as con:
        return con.execute("""
            SELECT v.match_id, m.title, m.status, v.choice, v.created_at
            FROM votes v JOIN matches m ON m.id=v.match_id
            WHERE v.user_id=?
            ORDER BY v.created_at DESC
        """, (uid,)).fetchall()

def leaderboard_db(season: str, limit: int = 20):
    with db() as con:
        return con.execute("""
            SELECT COALESCE(username,'') as username, user_id, points, correct, total
            FROM scores_season
            WHERE season=?
            ORDER BY points DESC, user_id ASC
            LIMIT ?
        """, (season, limit)).fetchall()

def can_score_db(mid: int) -> bool:
    with db() as con:
        r = con.execute("SELECT scored FROM results WHERE match_id=?", (mid,)).fetchone()
        return not (r and int(r["scored"]) == 1)

def set_result_and_score_db(mid: int, result: str) -> Tuple[str, int]:
    if not can_score_db(mid):
        return "already", 0

    season = current_season()
    winners = 0
    with db() as con:
        con.execute("""
            INSERT INTO results(match_id,result,scored) VALUES(?,?,1)
            ON CONFLICT(match_id) DO UPDATE SET result=excluded.result, scored=1
        """, (mid, result))

        voters = con.execute("SELECT user_id, COALESCE(username,'') as username, choice FROM votes WHERE match_id=?",
                             (mid,)).fetchall()
        for v in voters:
            uid = int(v["user_id"])
            uname = v["username"]
            ensure_score_row_db(season, uid, uname)
            con.execute("UPDATE scores_season SET total=total+1, username=COALESCE(?, username) WHERE season=? AND user_id=?",
                        (uname, season, uid))
            if v["choice"] == result:
                winners += 1
                con.execute("UPDATE scores_season SET points=points+1, correct=correct+1 WHERE season=? AND user_id=?",
                            (season, uid))
        con.commit()
    return "ok", winners

# pending fixtures
def pending_save_db(ext_id: str, day: str, title: str, kickoff_utc: str, competition: str) -> Optional[int]:
    with db() as con:
        try:
            con.execute("""
                INSERT INTO pending_fixtures(ext_id, day, title, kickoff_utc, competition, created_at)
                VALUES(?,?,?,?,?,?)
            """, (ext_id, day, title, kickoff_utc, competition, now_iso()))
            con.commit()
            r = con.execute("SELECT id FROM pending_fixtures WHERE ext_id=?", (ext_id,)).fetchone()
            return int(r["id"]) if r else None
        except sqlite3.IntegrityError:
            return None

def pending_get_db(pid: int):
    with db() as con:
        return con.execute("SELECT * FROM pending_fixtures WHERE id=?", (pid,)).fetchone()

def pending_delete_db(pid: int):
    with db() as con:
        con.execute("DELETE FROM pending_fixtures WHERE id=?", (pid,))
        con.commit()


# ===================== football-data =====================
def fetch_today_fixtures_from_fd(day_yyyy_mm_dd: str) -> list:
    if not FOOTBALL_DATA_TOKEN:
        return [{"__error__": "FOOTBALL_DATA_TOKEN not set"}]
    url = "https://api.football-data.org/v4/matches?" + urlencode({"date": day_yyyy_mm_dd})
    req = Request(url, headers={"X-Auth-Token": FOOTBALL_DATA_TOKEN, "Accept": "application/json"})
    try:
        with urlopen(req, timeout=25) as r:
            data = json.loads(r.read().decode("utf-8"))
            return data.get("matches", [])
    except Exception as e:
        logging.exception("football-data fetch failed")
        return [{"__error__": str(e), "__url__": url}]

async def sync_today_internal(bot: Bot, requested_by: str):
    if ADMIN_ID == 0:
        return
    day = today_key_utc()
    fixtures = await to_thread_timeout(fetch_today_fixtures_from_fd, day, timeout=35)
    if fixtures and isinstance(fixtures[0], dict) and fixtures[0].get("__error__"):
        await bot.send_message(ADMIN_ID, f"⚠️ /sync_today ошибка ({requested_by}): {fixtures[0].get('__error__')}")
        return

    comps = set(x.strip() for x in FD_COMPETITIONS.split(",") if x.strip())
    now_utc = datetime.now(timezone.utc)
    sent = 0

    await bot.send_message(ADMIN_ID, f"📅 Матчи на {day} ({requested_by}) — отправляю на подтверждение…")

    for m in fixtures:
        comp_code = ((m.get("competition") or {}).get("code") or "").strip()
        if comps and comp_code not in comps:
            continue

        ext_id = str(m.get("id") or "").strip()
        if not ext_id or await adb(match_exists_by_ext_db, ext_id):
            continue

        home = ((m.get("homeTeam") or {}).get("name") or "").strip()
        away = ((m.get("awayTeam") or {}).get("name") or "").strip()
        if not home or not away:
            continue

        title = f"{home} vs {away}"
        kickoff = (m.get("utcDate") or "").strip()
        kdt = parse_utc(kickoff) if kickoff else None

        # skip already started / too close
        if kdt and (kdt <= now_utc or (kdt - now_utc).total_seconds() <= PREDICTION_CLOSE_SECONDS):
            continue

        pid = await adb(pending_save_db, ext_id, day, title, kickoff, comp_code)
        if not pid:
            continue

        kb = InlineKeyboardBuilder()
        kb.button(text="✅ Добавить", callback_data=f"pf:add:{pid}")
        kb.button(text="❌ Пропустить", callback_data=f"pf:skip:{pid}")
        kb.adjust(1)

        await bot.send_message(
            ADMIN_ID,
            f"🏟 {title}\nЛига: {comp_code or '—'}\nKickoff(UTC): {kickoff or '—'}",
            reply_markup=kb.as_markup()
        )
        sent += 1
        await asyncio.sleep(0.2)

    await bot.send_message(ADMIN_ID, f"✅ Готово. Карточек: {sent}")


# ===================== Auto loops =====================
def next_run_utc_from_local(tz_name: str, hour_local: int) -> datetime:
    tz = ZoneInfo(tz_name)
    now_local = datetime.now(tz)
    target_local = now_local.replace(hour=hour_local, minute=0, second=0, microsecond=0)
    if target_local <= now_local:
        target_local += timedelta(days=1)
    return target_local.astimezone(timezone.utc)

def next_run_utc(hour_utc: int) -> datetime:
    now = datetime.now(timezone.utc)
    target = now.replace(hour=hour_utc, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target

async def auto_sync_loop(bot: Bot):
    global CRON_REQUESTED
    await asyncio.sleep(2)
    while True:
        try:
            if CRON_REQUESTED:
                CRON_REQUESTED = False
                await sync_today_internal(bot, "cron")
                await asyncio.sleep(5)
                continue

            if not AUTO_SYNC_ENABLED:
                await asyncio.sleep(30)
                continue

            run_at = next_run_utc_from_local(AUTO_SYNC_TZ, AUTO_SYNC_HOUR_LOCAL) if AUTO_SYNC_TZ else next_run_utc(AUTO_SYNC_HOUR_UTC)
            sleep_s = (run_at - datetime.now(timezone.utc)).total_seconds()

            while sleep_s > 0:
                if CRON_REQUESTED:
                    break
                step = min(60, sleep_s)
                await asyncio.sleep(step)
                sleep_s -= step

            if CRON_REQUESTED:
                continue

            await sync_today_internal(bot, "auto")
        except Exception:
            logging.exception("auto_sync_loop crashed")
            await asyncio.sleep(10)

async def auto_close_loop():
    await asyncio.sleep(5)
    while True:
        try:
            rows = await adb(get_open_matches_db)
            now_utc = datetime.now(timezone.utc)
            for r in rows:
                kickoff = (r["kickoff_utc"] or "").strip()
                if not kickoff:
                    continue
                kdt = parse_utc(kickoff)
                if not kdt:
                    continue
                if (kdt - now_utc).total_seconds() <= PREDICTION_CLOSE_SECONDS:
                    await adb(close_match_db, int(r["id"]))
            await asyncio.sleep(30)
        except Exception:
            logging.exception("auto_close_loop crashed")
            await asyncio.sleep(10)


# ===================== UI =====================
BTN_ACTIVE = "⚽ Активные матчи"
BTN_MY = "📊 Мои прогнозы"
BTN_LB = "🏆 Лидерборд"
BTN_PROFILE = "👤 Профиль"
BTN_HELP = "ℹ️ Помощь"
BTN_BACK = "⬅️ Назад"

def main_menu_kb():
    kb = ReplyKeyboardBuilder()
    for t in [BTN_ACTIVE, BTN_MY, BTN_LB, BTN_PROFILE, BTN_HELP]:
        kb.button(text=t)
    kb.adjust(2)
    return kb.as_markup(resize_keyboard=True)

def matches_list_kb(rows):
    kb = ReplyKeyboardBuilder()
    for r in rows:
        kb.button(text=f"🏟 #{r['id']} {r['title']}")
    kb.button(text=BTN_BACK)
    kb.adjust(1)
    return kb.as_markup(resize_keyboard=True)

def parse_match_button(text: str) -> Optional[int]:
    text = (text or "").strip()
    if not text.startswith("🏟 #"):
        return None
    try:
        return int(text.split("#", 1)[1].split(" ", 1)[0])
    except Exception:
        return None

def match_kb(mid: int, admin: bool):
    kb = InlineKeyboardBuilder()
    kb.button(text="🗳 Прогноз", callback_data=f"m:{mid}:vote")
    kb.button(text="📈 Статистика", callback_data=f"m:{mid}:stats")
    if admin:
        kb.button(text="🔒 Закрыть", callback_data=f"a:{mid}:close")
        kb.button(text="✅ Результат", callback_data=f"a:{mid}:res")
    kb.adjust(2)
    return kb.as_markup()

def vote_kb(mid: int):
    kb = InlineKeyboardBuilder()
    kb.button(text="🏠 Хозяева", callback_data=f"v:{mid}:home")
    kb.button(text="🤝 Ничья", callback_data=f"v:{mid}:draw")
    kb.button(text="🚌 Гости", callback_data=f"v:{mid}:away")
    kb.adjust(1)
    return kb.as_markup()


# ===================== Bot =====================
dp = Dispatcher()
BOT_USERNAME: Optional[str] = None

@dp.message(CommandStart())
async def start(message: Message):
    await adb(upsert_user_db, message.from_user)
    await adb(ensure_score_row_db, current_season(), message.from_user.id, message.from_user.username)
    await message.answer("🤖 Predictor Bot\n\nМеню снизу 👇", reply_markup=main_menu_kb())

@dp.message(Command("sync_today"))
async def sync_today_cmd(message: Message):
    await adb(upsert_user_db, message.from_user)
    if not is_admin(message.from_user.id):
        return
    await sync_today_internal(message.bot, "manual")

@dp.message(F.text == BTN_ACTIVE)
async def active_matches(message: Message):
    await adb(upsert_user_db, message.from_user)
    rows = await adb(get_open_matches_db)
    if not rows:
        await message.answer("Нет активных матчей.", reply_markup=main_menu_kb())
        return
    await message.answer("Выбери матч 👇", reply_markup=matches_list_kb(rows))

@dp.message(F.text.startswith("🏟 #"))
async def pick_match(message: Message):
    await adb(upsert_user_db, message.from_user)
    mid = parse_match_button(message.text)
    if not mid:
        return
    m = await adb(get_match_db, mid)
    if not m:
        await message.answer("Матч не найден.")
        return
    await message.answer(f"Матч #{mid}: {m['title']}\nСтатус: {m['status']}", reply_markup=match_kb(mid, is_admin(message.from_user.id)))

@dp.callback_query(F.data.startswith("m:"))
async def match_actions(call: CallbackQuery):
    await adb(upsert_user_db, call.from_user)
    _, mid, act = call.data.split(":")
    mid = int(mid)
    m = await adb(get_match_db, mid)
    if not m:
        await call.answer("Матч не найден", show_alert=True); return

    if act == "vote":
        if m["status"] != "open":
            await call.answer("Голосование закрыто", show_alert=True); return
        await call.message.edit_text("Выбери исход 1X2:", reply_markup=vote_kb(mid))
        await call.answer(); return

    if act == "stats":
        dist = await adb(fetch_votes_stats_db, mid)
        total = sum(dist.values())
        if total == 0:
            txt = f"📈 #{mid} {m['title']}\n\nПока нет голосов."
        else:
            def pct(x): return (x/total*100) if total else 0
            txt = (f"📈 #{mid} {m['title']}\n\n"
                   f"🏠 Хозяева: {dist.get('home',0)} ({pct(dist.get('home',0)):.0f}%)\n"
                   f"🤝 Ничья: {dist.get('draw',0)} ({pct(dist.get('draw',0)):.0f}%)\n"
                   f"🚌 Гости: {dist.get('away',0)} ({pct(dist.get('away',0)):.0f}%)\n")
        await call.message.edit_text(txt, reply_markup=match_kb(mid, is_admin(call.from_user.id)))
        await call.answer(); return

@dp.callback_query(F.data.startswith("v:"))
async def vote(call: CallbackQuery):
    await adb(upsert_user_db, call.from_user)
    _, mid, choice = call.data.split(":")
    mid = int(mid)
    m = await adb(get_match_db, mid)
    if not m or m["status"] != "open":
        await call.answer("Голосование закрыто", show_alert=True); return
    await adb(ensure_score_row_db, current_season(), call.from_user.id, call.from_user.username)
    await adb(save_vote_db, mid, call.from_user.id, call.from_user.username, choice)
    await call.answer("Сохранено ✅", show_alert=True)

@dp.callback_query(F.data.startswith("a:"))
async def admin_actions(call: CallbackQuery):
    await adb(upsert_user_db, call.from_user)
    if not is_admin(call.from_user.id):
        await call.answer("Только админ", show_alert=True); return
    _, mid, act = call.data.split(":")
    mid = int(mid)
    m = await adb(get_match_db, mid)
    if not m:
        await call.answer("Матч не найден", show_alert=True); return

    if act == "close":
        await adb(close_match_db, mid)
        await call.answer("Закрыто ✅", show_alert=True)
        await call.message.edit_text(f"Матч #{mid}: {m['title']}\nСтатус: closed", reply_markup=match_kb(mid, True))
        return

    if act == "res":
        kb = InlineKeyboardBuilder()
        kb.button(text="🏠 Хозяева", callback_data=f"r:{mid}:home")
        kb.button(text="🤝 Ничья", callback_data=f"r:{mid}:draw")
        kb.button(text="🚌 Гости", callback_data=f"r:{mid}:away")
        kb.adjust(1)
        await call.message.edit_text("Выбери результат 1X2:", reply_markup=kb.as_markup())
        await call.answer()
        return

@dp.callback_query(F.data.startswith("r:"))
async def set_result(call: CallbackQuery):
    await adb(upsert_user_db, call.from_user)
    if not is_admin(call.from_user.id):
        await call.answer("Только админ", show_alert=True); return
    _, mid, result = call.data.split(":")
    mid = int(mid)
    m = await adb(get_match_db, mid)
    if not m:
        await call.answer("Матч не найден", show_alert=True); return

    status, winners = await adb(set_result_and_score_db, mid, result)
    if status == "already":
        await call.answer("Уже выставлен", show_alert=True); return

    await call.answer("Готово ✅", show_alert=True)
    await call.message.edit_text(f"✅ Результат выставлен\n#{mid} {m['title']}\nРезультат: {result}\nПобедителей: {winners}",
                                 reply_markup=match_kb(mid, True))

@dp.callback_query(F.data.startswith("pf:"))
async def pending_actions(call: CallbackQuery):
    await adb(upsert_user_db, call.from_user)
    if not is_admin(call.from_user.id):
        await call.answer("Только админ", show_alert=True); return
    _, action, pid_str = call.data.split(":")
    pid = int(pid_str)
    p = await adb(pending_get_db, pid)
    if not p:
        await call.answer("Уже обработано", show_alert=True); return

    if action == "skip":
        await adb(pending_delete_db, pid)
        await call.message.edit_text(call.message.text + "\n\n❌ Пропущено")
        await call.answer(); return

    if action == "add":
        if await adb(match_exists_by_ext_db, p["ext_id"]):
            await adb(pending_delete_db, pid)
            await call.message.edit_text(call.message.text + "\n\n⚠️ Уже добавлено ранее (дубликат).")
            await call.answer(); return

        await adb(create_match_db, p["title"], p["ext_id"], p["kickoff_utc"])
        await adb(pending_delete_db, pid)
        await call.message.edit_text(call.message.text + "\n\n✅ Добавлено")
        await call.answer(); return

@dp.message(F.text == BTN_MY)
async def my_votes(message: Message):
    await adb(upsert_user_db, message.from_user)
    rows = await adb(fetch_my_votes_db, message.from_user.id)
    if not rows:
        await message.answer("Пока нет прогнозов.", reply_markup=main_menu_kb()); return
    lines = ["📊 Твои прогнозы:"]
    for r in rows[:30]:
        lines.append(f"• #{r['match_id']} {r['title']} ({r['status']}) → {r['choice']}")
    await message.answer("\n".join(lines), reply_markup=main_menu_kb())

@dp.message(F.text == BTN_LB)
async def lb(message: Message):
    await adb(upsert_user_db, message.from_user)
    season = current_season()
    rows = await adb(leaderboard_db, season, 20)
    if not rows:
        await message.answer("Пока нет очков.", reply_markup=main_menu_kb()); return
    lines = [f"🏆 Лидерборд {season}:"]
    for i, r in enumerate(rows, start=1):
        name = f"@{r['username']}" if r["username"] else f"id:{r['user_id']}"
        total = int(r["total"]); correct = int(r["correct"])
        acc = (correct/total*100) if total else 0
        lines.append(f"{i}. {name} — {int(r['points'])} pts | {acc:.0f}%")
    await message.answer("\n".join(lines), reply_markup=main_menu_kb())

@dp.message(F.text == BTN_PROFILE)
async def profile(message: Message):
    await adb(upsert_user_db, message.from_user)
    season = current_season()
    with db() as con:
        u = con.execute("SELECT username, first_name, last_name FROM users WHERE user_id=?", (message.from_user.id,)).fetchone()
        s = con.execute("SELECT points, correct, total FROM scores_season WHERE season=? AND user_id=?", (season, message.from_user.id)).fetchone()
    pts = int(s["points"]) if s else 0
    correct = int(s["correct"]) if s else 0
    total = int(s["total"]) if s else 0
    acc = (correct/total*100) if total else 0
    name = f"@{u['username']}" if u and u["username"] else message.from_user.full_name
    await message.answer(f"👤 {name}\nСезон: {season}\n🏆 Очки: {pts}\n🎯 Точность: {acc:.1f}% ({correct}/{total})",
                         reply_markup=main_menu_kb())

@dp.message(F.text == BTN_HELP)
async def help_menu(message: Message):
    await adb(upsert_user_db, message.from_user)
    await message.answer(
        "ℹ️ Помощь\n\n"
        "• «⚽ Активные матчи» → матч → прогноз/статистика\n"
        "• «📊 Мои прогнозы»\n"
        "• «🏆 Лидерборд»\n"
        "• «👤 Профиль»\n\n"
        "Админ:\n"
        "/sync_today\n"
        "/whoami\n",
        reply_markup=main_menu_kb()
    )

@dp.message(Command("whoami"))
async def whoami(message: Message):
    await adb(upsert_user_db, message.from_user)
    await message.answer(
        f"Твой id: {message.from_user.id}\n"
        f"ADMIN_ID: {ADMIN_ID}\n"
        f"Ты админ: {'ДА' if is_admin(message.from_user.id) else 'НЕТ'}\n"
        f"AUTO_SYNC_ENABLED: {AUTO_SYNC_ENABLED}\n"
        f"AUTO_SYNC_TZ: {AUTO_SYNC_TZ or '(не задан)'}\n"
        f"AUTO_SYNC_HOUR_LOCAL: {AUTO_SYNC_HOUR_LOCAL}\n"
        f"PREDICTION_CLOSE_SECONDS: {PREDICTION_CLOSE_SECONDS}\n"
    )


async def main():
    logging.basicConfig(level=logging.INFO)
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN not set")

    init_db()

    bot = Bot(TOKEN)
    global BOT_USERNAME
    me = await bot.get_me()
    BOT_USERNAME = me.username

    asyncio.create_task(auto_sync_loop(bot))
    asyncio.create_task(auto_close_loop())

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
