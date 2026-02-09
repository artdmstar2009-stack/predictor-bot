import asyncio
import logging
import os
import sqlite3
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, date, timedelta
from typing import Optional, Tuple
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
import aiohttp
from zoneinfo import ZoneInfo
# ===== CONFIG =====
TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
DB_PATH = "predictor.db"
# ===== AUTOSYNC CONFIG =====
SYNC_ENABLED = os.getenv('SYNC_ENABLED', '1') == '1'
SYNC_TZ = os.getenv('SYNC_TZ', 'Europe/London')
SYNC_HOUR = int(os.getenv('SYNC_HOUR', '4'))  # default: 04:00 London
SYNC_MINUTE = int(os.getenv('SYNC_MINUTE', '0'))
SYNC_LOOKAHEAD_DAYS = int(os.getenv('SYNC_LOOKAHEAD_DAYS', '1'))
FOOTBALL_DATA_TOKEN = os.getenv('FOOTBALL_DATA_TOKEN', '')
# Example: PL,PD,SA,BL1,FL1,CL
FOOTBALL_COMPETITIONS = [c.strip() for c in os.getenv('FOOTBALL_COMPETITIONS', 'PL,CL').split(',') if c.strip()]
NHL_ENABLED = os.getenv('NHL_ENABLED', '1') == '1'
LIQUIPEDIA_ENABLED = os.getenv('LIQUIPEDIA_ENABLED', '0') == '1'
# Example: counterstrike,dota2
LIQUIPEDIA_GAMES = [g.strip() for g in os.getenv('LIQUIPEDIA_GAMES', '').split(',') if g.strip()]
# ===== AUTO RESULTS CONFIG =====
AUTO_RESULTS_ENABLED = os.getenv('AUTO_RESULTS_ENABLED', '1') == '1'
AUTO_RESULTS_INTERVAL = int(os.getenv('AUTO_RESULTS_INTERVAL', '300'))  # seconds
AUTO_RESULTS_MIN_AGE_MINUTES = int(os.getenv('AUTO_RESULTS_MIN_AGE_MINUTES', '30'))  # don't finalize too early

# ===== KEEPALIVE (prevent sleeping on free hosting) =====
KEEPALIVE_URL = os.getenv('KEEPALIVE_URL', '')
KEEPALIVE_INTERVAL = int(os.getenv('KEEPALIVE_INTERVAL', '300'))  # seconds
# ============================
# ==================
dp = Dispatcher()
BOT_USERNAME: Optional[str] = None
# ===== Fake HTTP server (Render free Web Service needs open port) =====
class DummyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running")
def run_http():
    port = int(os.environ.get("PORT", 10000))
    HTTPServer(("0.0.0.0", port), DummyHandler).serve_forever()
threading.Thread(target=run_http, daemon=True).start()
# ====================================================================
# ===================== DB =====================
def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con
def now_iso() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")
def current_season() -> str:
    # season = month: YYYY-MM (UTC)
    return datetime.utcnow().strftime("%Y-%m")
def init_db():
    with db() as con:
        con.execute("""
        CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            created_at TEXT,
            last_seen TEXT,
            last_rank_season INTEGER DEFAULT NULL
        )
        """)
        con.execute("""
        CREATE TABLE IF NOT EXISTS matches(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open'
        )
        """)
        # --- migrations for autosync (safe to run every start) ---
        for stmt in [
            "ALTER TABLE matches ADD COLUMN sport TEXT",
            "ALTER TABLE matches ADD COLUMN league TEXT",
            "ALTER TABLE matches ADD COLUMN start_time TEXT",
            "ALTER TABLE matches ADD COLUMN source TEXT",
            "ALTER TABLE matches ADD COLUMN external_id TEXT",
            "ALTER TABLE matches ADD COLUMN home TEXT",
            "ALTER TABLE matches ADD COLUMN away TEXT",
            "ALTER TABLE matches ADD COLUMN created_at TEXT",
        ]:
            try:
                con.execute(stmt)
            except sqlite3.OperationalError:
                pass
        con.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_matches_source_ext ON matches(source, external_id)")
        # ---------------------------------------------------------
        con.execute("""
        CREATE TABLE IF NOT EXISTS votes(
            match_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            username TEXT,
            bet_type TEXT NOT NULL,
            choice TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(match_id, user_id, bet_type)
        )
        """)
        con.execute("""
        CREATE TABLE IF NOT EXISTS results(
            match_id INTEGER NOT NULL,
            bet_type TEXT NOT NULL,
            result TEXT NOT NULL,
            scored INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(match_id, bet_type)
        )
        """)
        # season scores
        con.execute("""
        CREATE TABLE IF NOT EXISTS scores_season(
            season TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            username TEXT,
            points INTEGER NOT NULL DEFAULT 0,
            correct INTEGER NOT NULL DEFAULT 0,
            total INTEGER NOT NULL DEFAULT 0,
            streak INTEGER NOT NULL DEFAULT 0,
            best_streak INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(season, user_id)
        )
        """)
        # follows
        con.execute("""
        CREATE TABLE IF NOT EXISTS follows(
            follower_id INTEGER NOT NULL,
            followee_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(follower_id, followee_id)
        )
        """)
        # achievements
        con.execute("""
        CREATE TABLE IF NOT EXISTS user_achievements(
            user_id INTEGER NOT NULL,
            season TEXT NOT NULL,
            key TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(user_id, season, key)
        )
        """)
        # user state (for find player flow)
        con.execute("""
        CREATE TABLE IF NOT EXISTS user_state(
            user_id INTEGER PRIMARY KEY,
            state TEXT,
            updated_at TEXT
        )
        """)
        con.commit()
# ===================== Core helpers =====================
def is_admin(uid: int) -> bool:
    return uid == ADMIN_ID
# ===================== AUTOSYNC =====================
def _match_exists(source: str, external_id: str) -> bool:
    with db() as con:
        row = con.execute(
            "SELECT 1 FROM matches WHERE source=? AND external_id=? LIMIT 1",
            (source, external_id),
        ).fetchone()
        return row is not None
def upsert_external_match(*, title: str, source: str, external_id: str, sport: str, league: str,
                          start_time: Optional[str], home: Optional[str], away: Optional[str]) -> bool:
    """Insert a match if it doesn't exist yet. Returns True if inserted."""
    if not source or not external_id:
        return False
    if _match_exists(source, external_id):
        return False
    with db() as con:
        con.execute(
            """INSERT INTO matches(title, status, sport, league, start_time, source, external_id, home, away, created_at)
               VALUES(?, 'open', ?, ?, ?, ?, ?, ?, ?, ?)""",
            (title, sport, league, start_time, source, external_id, home, away, now_iso()),
        )
    return True
async def _http_get_json(session: aiohttp.ClientSession, url: str, headers: Optional[dict] = None, params: Optional[dict] = None) -> dict:
    async with session.get(url, headers=headers, params=params, timeout=aiohttp.ClientTimeout(total=25)) as r:
        r.raise_for_status()
        return await r.json()

async def keepalive_loop():
    """Optional: periodically GET KEEPALIVE_URL to keep the service warm on some hosts."""
    if not KEEPALIVE_URL:
        logging.info("KEEPALIVE_URL not set; keepalive disabled")
        return
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        while True:
            try:
                async with session.get(KEEPALIVE_URL) as r:
                    _ = await r.text()
                logging.info("Keepalive ping OK")
            except Exception as e:
                logging.warning(f"Keepalive ping failed: {e}")
            await asyncio.sleep(max(60, KEEPALIVE_INTERVAL))

async def sync_football_data(*, lookahead_days: int) -> Tuple[int, str]:
    """Sync upcoming football matches via football-data.org (requires FOOTBALL_DATA_TOKEN)."""
    if not FOOTBALL_DATA_TOKEN:
        return 0, "FOOTBALL_DATA_TOKEN is not set"
    if not FOOTBALL_COMPETITIONS:
        return 0, "FOOTBALL_COMPETITIONS is empty"
    today = date.today()
    date_from = today.isoformat()
    date_to = (today + timedelta(days=max(0, lookahead_days))).isoformat()
    inserted = 0
    async with aiohttp.ClientSession(headers={"User-Agent": "predictor-bot/1.0"}) as session:
        for comp in FOOTBALL_COMPETITIONS:
            try:
                data = await _http_get_json(
                    session,
                    f"https://api.football-data.org/v4/competitions/{comp}/matches",
                    headers={"X-Auth-Token": FOOTBALL_DATA_TOKEN},
                    params={"status": "SCHEDULED", "dateFrom": date_from, "dateTo": date_to},
                )
                for m in data.get("matches", []):
                    mid = str(m.get("id", ""))
                    utc = m.get("utcDate")
                    home = (m.get("homeTeam") or {}).get("name")
                    away = (m.get("awayTeam") or {}).get("name")
                    comp_name = ((m.get("competition") or {}).get("code") or comp)
                    title = f"⚽ {comp_name}: {home} vs {away}"
                    if utc:
                        title += f" • {utc.replace('T',' ')[:16]} UTC"
                    if upsert_external_match(
                        title=title,
                        source="football-data",
                        external_id=mid,
                        sport="football",
                        league=comp_name,
                        start_time=utc,
                        home=home,
                        away=away,
                    ):
                        inserted += 1
            except Exception as e:
                # keep going; we'll report aggregated error at the end
                logging.exception("football-data sync failed for %s: %s", comp, e)
    return inserted, "ok"
async def sync_nhl(*, lookahead_days: int) -> Tuple[int, str]:
    """Sync upcoming NHL games. Uses the newer api-web.nhle.com schedule endpoint, fallback to legacy statsapi."""
    if not NHL_ENABLED:
        return 0, "NHL_ENABLED=0"
    inserted = 0
    tz_utc = ZoneInfo("UTC")
    today = datetime.now(tz_utc).date()
    days = [today + timedelta(days=i) for i in range(0, max(1, lookahead_days + 1))]
    async with aiohttp.ClientSession(headers={"User-Agent": "predictor-bot/1.0"}) as session:
        for d in days:
            dstr = d.isoformat()
            data = None
            # Newer endpoint (works when legacy is blocked)
            try:
                data = await _http_get_json(session, f"https://api-web.nhle.com/v1/schedule/{dstr}")
            except Exception:
                # Legacy fallback
                try:
                    data = await _http_get_json(session, "https://statsapi.web.nhl.com/api/v1/schedule", params={"date": dstr})
                except Exception as e:
                    logging.exception("NHL sync failed for %s: %s", dstr, e)
                    continue
            # Parse both shapes
            games = []
            if "gameWeek" in (data or {}):
                # api-web.nhle.com shape
                for week in data.get("gameWeek", []):
                    for gday in week.get("games", []):
                        games.append(gday)
            elif "dates" in (data or {}):
                # legacy statsapi shape
                for dt in data.get("dates", []):
                    games.extend(dt.get("games", []))
            for g in games:
                try:
                    # new shape uses gameId, startTimeUTC, homeTeam/awayTeam with 'placeName'
                    gid = str(g.get("id") or g.get("gamePk") or g.get("gameId") or "")
                    if not gid:
                        continue
                    start_utc = g.get("startTimeUTC") or (g.get("gameDate"))
                    if "homeTeam" in g and isinstance(g["homeTeam"], dict):
                        home = g["homeTeam"].get("placeName", {}).get("default") or g["homeTeam"].get("name", {}).get("default") or g["homeTeam"].get("teamName", {}).get("default")
                        away = g.get("awayTeam", {}).get("placeName", {}).get("default") or g.get("awayTeam", {}).get("name", {}).get("default") or g.get("awayTeam", {}).get("teamName", {}).get("default")
                    else:
                        # legacy statsapi
                        home = (g.get("teams") or {}).get("home", {}).get("team", {}).get("name")
                        away = (g.get("teams") or {}).get("away", {}).get("team", {}).get("name")
                    title = f"🏒 NHL: {home} vs {away}"
                    if start_utc:
                        title += f" • {str(start_utc).replace('T',' ')[:16]} UTC"
                    if upsert_external_match(
                        title=title,
                        source="nhl",
                        external_id=gid,
                        sport="hockey",
                        league="NHL",
                        start_time=str(start_utc) if start_utc else None,
                        home=home,
                        away=away,
                    ):
                        inserted += 1
                except Exception as e:
                    logging.exception("NHL game parse failed: %s", e)
    return inserted, "ok"
async def sync_liquipedia(*, lookahead_days: int) -> Tuple[int, str]:
    """Stub sync for Liquipedia (MediaWiki API). Enable with LIQUIPEDIA_ENABLED=1 and implement per-game parsing."""
    if not LIQUIPEDIA_ENABLED:
        return 0, "LIQUIPEDIA_ENABLED=0"
    if not LIQUIPEDIA_GAMES:
        return 0, "LIQUIPEDIA_GAMES is empty (set e.g. counterstrike,dota2)"
    # NOTE: Liquipedia data structure differs per game; real parsing needs per-wiki templates.
    # We keep this as a placeholder so the bot doesn't crash if enabled accidentally.
    return 0, "not implemented (needs per-game parser)"
async def sync_all_sources(bot: Bot, *, lookahead_days: int) -> str:
    total_inserted = 0
    parts = []
    f_ins, f_msg = await sync_football_data(lookahead_days=lookahead_days)
    total_inserted += f_ins
    parts.append(f"⚽ football-data: +{f_ins} ({f_msg})")
    n_ins, n_msg = await sync_nhl(lookahead_days=lookahead_days)
    total_inserted += n_ins
    parts.append(f"🏒 NHL: +{n_ins} ({n_msg})")
    l_ins, l_msg = await sync_liquipedia(lookahead_days=lookahead_days)
    total_inserted += l_ins
    parts.append(f"🎮 Liquipedia: +{l_ins} ({l_msg})")
    report = "Автосинк матчей завершён\n" + "\n".join(parts) + f"\n\nИтого добавлено: {total_inserted}"
    # notify admin if configured
    if ADMIN_ID:
        await safe_dm(bot, ADMIN_ID, report)
    return report
def _next_run_dt(now_tz: datetime, hour: int, minute: int) -> datetime:
    candidate = now_tz.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now_tz:
        candidate = candidate + timedelta(days=1)
    return candidate
async def autosync_loop(bot: Bot):
    if not SYNC_ENABLED:
        logging.info("Autosync disabled (SYNC_ENABLED=0)")
        return
    tz = ZoneInfo(SYNC_TZ)
    logging.info("Autosync enabled: %s %02d:%02d (lookahead=%d days)", SYNC_TZ, SYNC_HOUR, SYNC_MINUTE, SYNC_LOOKAHEAD_DAYS)
    while True:
        now_tz = datetime.now(tz)
        nxt = _next_run_dt(now_tz, SYNC_HOUR, SYNC_MINUTE)
        sleep_s = (nxt - now_tz).total_seconds()
        logging.info("Next autosync at %s (%s) in %.0f seconds", nxt.isoformat(timespec="seconds"), SYNC_TZ, sleep_s)
        await asyncio.sleep(max(5, sleep_s))
        try:
            await sync_all_sources(bot, lookahead_days=SYNC_LOOKAHEAD_DAYS)
        except Exception as e:
            logging.exception("Autosync failed: %s", e)
            if ADMIN_ID:
                await safe_dm(bot, ADMIN_ID, f"❌ Автосинк упал: {e}")
# ===================================================
async def _parse_utc_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        ss = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(ss)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo("UTC"))
        return dt.astimezone(ZoneInfo("UTC"))
    except Exception:
        return None
def _get_open_external_matches_for_results():
    with db() as con:
        rows = con.execute(
            """SELECT id, title, status, source, external_id, start_time
                 FROM matches
                 WHERE status='open'
                   AND source IN ('football-data','nhl')
                   AND external_id IS NOT NULL
                 ORDER BY id DESC"""
        ).fetchall()
        return rows
async def fetch_result_football_data(external_id: str) -> Optional[str]:
    if not FOOTBALL_DATA_TOKEN:
        return None
    async with aiohttp.ClientSession(headers={"User-Agent": "predictor-bot/1.0"}) as session:
        try:
            data = await _http_get_json(
                session,
                f"https://api.football-data.org/v4/matches/{external_id}",
                headers={"X-Auth-Token": FOOTBALL_DATA_TOKEN},
            )
        except Exception:
            return None
    status = (data.get("status") or "").upper()
    if status not in {"FINISHED", "AWARDED"}:
        return None
    winner = ((data.get("score") or {}).get("winner") or "").upper()
    if winner == "HOME_TEAM":
        return "home"
    if winner == "AWAY_TEAM":
        return "away"
    if winner == "DRAW" or winner == "":
        return "draw"
    return None
async def fetch_result_nhl(external_id: str) -> Optional[str]:
    async with aiohttp.ClientSession(headers={"User-Agent": "predictor-bot/1.0"}) as session:
        # Preferred (new) endpoint
        try:
            data = await _http_get_json(session, f"https://api-web.nhle.com/v1/gamecenter/{external_id}/landing")
            state = (data.get("gameState") or data.get("gameStateId") or "").__str__().upper()
            # In practice gameState is like 'FINAL' / 'OFF' / 'LIVE'
            if "FINAL" not in state and "OFF" not in state:
                # Some shapes use gameStateId int; try status in 'gameStatus' too
                pass
            home_score = ((data.get("homeTeam") or {}).get("score"))
            away_score = ((data.get("awayTeam") or {}).get("score"))
            if isinstance(home_score, int) and isinstance(away_score, int):
                if home_score > away_score:
                    return "home"
                if away_score > home_score:
                    return "away"
                return "draw"
        except Exception:
            pass
        # Fallback legacy
        try:
            data = await _http_get_json(session, f"https://statsapi.web.nhl.com/api/v1/game/{external_id}/feed/live")
            st = (((data.get("gameData") or {}).get("status") or {}).get("abstractGameState") or "").upper()
            if st != "FINAL":
                return None
            linescore = (data.get("liveData") or {}).get("linescore") or {}
            teams = linescore.get("teams") or {}
            hs = (teams.get("home") or {}).get("goals")
            as_ = (teams.get("away") or {}).get("goals")
            if isinstance(hs, int) and isinstance(as_, int):
                if hs > as_:
                    return "home"
                if as_ > hs:
                    return "away"
                return "draw"
        except Exception:
            return None
    return None
async def auto_finalize_match(bot: Bot, mid: int, result: str):
    bt = "1x2"
    m = get_match(mid)
    if not m:
        return
    status, winners_count = set_result_and_score(mid, bt, result)
    if status != "ok":
        return
    # Close match after scoring
    close_match(mid)
    # Notify voters + achievements + rank
    season = current_season()
    with db() as con:
        voters = con.execute(
            """SELECT user_id, COALESCE(username,'') as username, choice
                 FROM votes WHERE match_id=? AND bet_type=?""",
            (mid, bt)
        ).fetchall()
    for v in voters:
        uid = int(v["user_id"])
        ch = v["choice"]
        correct = (ch == result)
        with db() as con:
            srow = con.execute(
                "SELECT points, streak FROM scores_season WHERE season=? AND user_id=?",
                (season, uid)
            ).fetchone()
        pts = int(srow["points"]) if srow else 0
        streak = int(srow["streak"]) if srow else 0
        text = (
            f"🏁 Авто-итоги по матчу: {m['title']}\n\n"
            f"✅ Правильный исход: {choice_label(bt, result)}\n"
            f"Твой прогноз: {choice_label(bt, ch)}\n"
            f"{'🎉 Ты угадал! +1 очко' if correct else '❌ Не угадал. +0'}\n\n"
            f"🏆 Твои очки в сезоне {season}: {pts}\n"
            f"🔥 Текущая серия: {streak}\n"
        )
        await safe_dm(bot, uid, text)
        await check_and_notify_achievements(bot, season, uid)
        await notify_rank_change(bot, season, uid)
    # Notify admin
    if ADMIN_ID:
        await safe_dm(bot, ADMIN_ID, f"✅ Авто-итоги: матч #{mid} закрыт. Результат 1X2 = {choice_label(bt, result)}. Победителей: {winners_count}")
async def autoresults_loop(bot: Bot):
    if not AUTO_RESULTS_ENABLED:
        logging.info("Auto-results disabled (AUTO_RESULTS_ENABLED=0)")
        return
    logging.info("Auto-results enabled: interval=%ds min_age=%dmin", AUTO_RESULTS_INTERVAL, AUTO_RESULTS_MIN_AGE_MINUTES)
    while True:
        try:
            now_utc = datetime.now(ZoneInfo("UTC"))
            for r in _get_open_external_matches_for_results():
                mid = int(r["id"])
                source = (r["source"] or "").strip()
                ext = (r["external_id"] or "").strip()
                st = await _parse_utc_iso(r["start_time"])
                if st:
                    age_min = (now_utc - st).total_seconds() / 60.0
                    if age_min < AUTO_RESULTS_MIN_AGE_MINUTES:
                        continue
                # already scored?
                if not can_score(mid, "1x2"):
                    # ensure match closed
                    close_match(mid)
                    continue
                res = None
                if source == "football-data":
                    res = await fetch_result_football_data(ext)
                elif source == "nhl":
                    res = await fetch_result_nhl(ext)
                if res in {"home", "away", "draw"}:
                    await auto_finalize_match(bot, mid, res)
        except Exception as e:
            logging.exception("Auto-results loop failed: %s", e)
            if ADMIN_ID:
                await safe_dm(bot, ADMIN_ID, f"❌ Авто-итоги упали: {e}")
        await asyncio.sleep(max(30, AUTO_RESULTS_INTERVAL))
def upsert_user(user_id: int, username: Optional[str], first_name: Optional[str], last_name: Optional[str]):
    with db() as con:
        row = con.execute("SELECT user_id FROM users WHERE user_id=?", (user_id,)).fetchone()
        if row:
            con.execute("""
                UPDATE users SET
                    username=?,
                    first_name=?,
                    last_name=?,
                    last_seen=?
                WHERE user_id=?
            """, (username, first_name, last_name, now_iso(), user_id))
        else:
            con.execute("""
                INSERT INTO users(user_id, username, first_name, last_name, created_at, last_seen)
                VALUES(?,?,?,?,?,?)
            """, (user_id, username, first_name, last_name, now_iso(), now_iso()))
        con.commit()
def ensure_score_row(season: str, user_id: int, username: Optional[str]):
    with db() as con:
        row = con.execute(
            "SELECT 1 FROM scores_season WHERE season=? AND user_id=?",
            (season, user_id)
        ).fetchone()
        if not row:
            con.execute("""
                INSERT INTO scores_season(season, user_id, username, points, correct, total, streak, best_streak)
                VALUES(?,?,?,?,?,?,?,?)
            """, (season, user_id, username, 0, 0, 0, 0, 0))
        else:
            con.execute("""
                UPDATE scores_season SET username=COALESCE(?, username)
                WHERE season=? AND user_id=?
            """, (username, season, user_id))
        con.commit()
def get_open_matches():
    with db() as con:
        return con.execute("SELECT id, title FROM matches WHERE status='open' ORDER BY id DESC").fetchall()
def get_match(mid: int):
    with db() as con:
        return con.execute("SELECT id, title, status FROM matches WHERE id=?", (mid,)).fetchone()
def close_match(mid: int):
    with db() as con:
        con.execute("UPDATE matches SET status='closed' WHERE id=?", (mid,))
        con.commit()
def match_stats(mid: int) -> Tuple[dict, dict]:
    """
    totals_map[bet_type] = total_count
    data[bet_type][choice] = count
    """
    with db() as con:
        rows = con.execute("""
            SELECT bet_type, choice, COUNT(*) as c
            FROM votes
            WHERE match_id=?
            GROUP BY bet_type, choice
        """, (mid,)).fetchall()
        totals = con.execute("""
            SELECT bet_type, COUNT(*) as c
            FROM votes
            WHERE match_id=?
            GROUP BY bet_type
        """, (mid,)).fetchall()
    totals_map = {r["bet_type"]: r["c"] for r in totals}
    data = {}
    for r in rows:
        bt = r["bet_type"]
        ch = r["choice"]
        c = r["c"]
        data.setdefault(bt, {})
        data[bt][ch] = c
    return totals_map, data
def count_followers(user_id: int) -> int:
    with db() as con:
        r = con.execute("SELECT COUNT(*) as c FROM follows WHERE followee_id=?", (user_id,)).fetchone()
        return int(r["c"])
def count_following(user_id: int) -> int:
    with db() as con:
        r = con.execute("SELECT COUNT(*) as c FROM follows WHERE follower_id=?", (user_id,)).fetchone()
        return int(r["c"])
def is_following(follower_id: int, followee_id: int) -> bool:
    with db() as con:
        r = con.execute("SELECT 1 FROM follows WHERE follower_id=? AND followee_id=?", (follower_id, followee_id)).fetchone()
        return r is not None
def follow_user(follower_id: int, followee_id: int) -> str:
    if follower_id == followee_id:
        return "self"
    with db() as con:
        try:
            con.execute(
                "INSERT INTO follows(follower_id, followee_id, created_at) VALUES(?,?,?)",
                (follower_id, followee_id, now_iso())
            )
            con.commit()
            return "ok"
        except sqlite3.IntegrityError:
            return "already"
def unfollow_user(follower_id: int, followee_id: int) -> str:
    with db() as con:
        con.execute("DELETE FROM follows WHERE follower_id=? AND followee_id=?", (follower_id, followee_id))
        con.commit()
    return "ok"
def get_rank(season: str, user_id: int) -> Optional[int]:
    with db() as con:
        rows = con.execute("""
            SELECT user_id, points
            FROM scores_season
            WHERE season=?
            ORDER BY points DESC, user_id ASC
        """, (season,)).fetchall()
    for i, r in enumerate(rows, start=1):
        if int(r["user_id"]) == int(user_id):
            return i
    return None
def set_last_rank(user_id: int, rank: Optional[int]):
    with db() as con:
        con.execute("UPDATE users SET last_rank_season=? WHERE user_id=?", (rank, user_id))
        con.commit()
def get_last_rank(user_id: int) -> Optional[int]:
    with db() as con:
        r = con.execute("SELECT last_rank_season FROM users WHERE user_id=?", (user_id,)).fetchone()
        if not r:
            return None
        return r["last_rank_season"]
# ---------- user state (for "Find player") ----------
def set_state(user_id: int, state: Optional[str]):
    with db() as con:
        if state is None:
            con.execute("DELETE FROM user_state WHERE user_id=?", (user_id,))
        else:
            con.execute("""
                INSERT INTO user_state(user_id, state, updated_at)
                VALUES(?,?,?)
                ON CONFLICT(user_id) DO UPDATE SET
                    state=excluded.state,
                    updated_at=excluded.updated_at
            """, (user_id, state, now_iso()))
        con.commit()
def get_state(user_id: int) -> Optional[str]:
    with db() as con:
        r = con.execute("SELECT state FROM user_state WHERE user_id=?", (user_id,)).fetchone()
        return r["state"] if r else None
def find_user_by_username(username: str) -> Optional[int]:
    u = (username or "").strip()
    if u.startswith("@"):
        u = u[1:]
    u = u.strip()
    if not u:
        return None
    with db() as con:
        r = con.execute("""
            SELECT user_id FROM users
            WHERE LOWER(username)=LOWER(?)
            ORDER BY last_seen DESC
            LIMIT 1
        """, (u,)).fetchone()
        return int(r["user_id"]) if r else None
# ===================== Labels & UI =====================
BTN_ACTIVE = "⚽ Активные матчи"
BTN_MY = "📊 Мои прогнозы"
BTN_LB = "🏆 Лидерборд"
BTN_PROFILE = "👤 Профиль"
BTN_FIND = "🔎 Найти игрока"
BTN_HELP = "ℹ️ Помощь"
BTN_NEW = "➕ Создать матч"
BTN_BACK = "⬅️ Назад"
BET_LABEL = {"1x2": "1X2"}
def choice_label(bt: str, choice: str) -> str:
    # only 1X2 is used right now
    return {"home": "🏠 Хозяева", "draw": "🤝 Ничья", "away": "🚌 Гости"}.get(choice, choice)
def main_menu_kb(user_is_admin: bool):
    kb = ReplyKeyboardBuilder()
    kb.button(text=BTN_ACTIVE)
    kb.button(text=BTN_MY)
    kb.button(text=BTN_LB)
    kb.button(text=BTN_PROFILE)
    kb.button(text=BTN_FIND)
    kb.button(text=BTN_HELP)
    if user_is_admin:
        kb.button(text=BTN_NEW)
    kb.adjust(2)
    return kb.as_markup(resize_keyboard=True)
def matches_list_kb(matches, user_is_admin: bool):
    kb = ReplyKeyboardBuilder()
    for r in matches[:50]:
        kb.button(text=f"🏟 #{r['id']}")
    kb.button(text=BTN_BACK)
    if user_is_admin:
        kb.button(text=BTN_NEW)
    kb.adjust(1)
    return kb.as_markup(resize_keyboard=True)
def combined_menu_kb(matches, user_is_admin: bool):
    """One reply menu: main buttons + current open matches."""
    kb = ReplyKeyboardBuilder()
    # Main menu buttons (go first)
    kb.button(text=BTN_ACTIVE)
    kb.button(text=BTN_MY)
    kb.button(text=BTN_LB)
    kb.button(text=BTN_PROFILE)
    kb.button(text=BTN_FIND)
    kb.button(text=BTN_HELP)
    if user_is_admin:
        kb.button(text=BTN_NEW)
    # Matches (below)
    for r in matches[:50]:
        kb.button(text=f"🏟 #{r['id']}")
    # Layout: first rows in 2 columns, then one match per row
    kb.adjust(2, 2, 2, 1)
    return kb.as_markup(resize_keyboard=True)
def match_menu_kb(mid: int, user_is_admin: bool):
    kb = InlineKeyboardBuilder()
    kb.button(text="🗳 Сделать прогноз", callback_data=f"match:{mid}:vote")
    kb.button(text="📈 Статистика", callback_data=f"match:{mid}:stats")
    if user_is_admin:
        kb.button(text="🔒 Закрыть матч", callback_data=f"admin:{mid}:close")
        kb.button(text="✅ Указать результат", callback_data=f"admin:{mid}:setresult")
    kb.adjust(2)
    return kb.as_markup()
def kb_1x2(mid: int, prefix: str):
    kb = InlineKeyboardBuilder()
    kb.button(text="🏠 Хозяева", callback_data=f"{prefix}:{mid}:1x2:home")
    kb.button(text="🤝 Ничья", callback_data=f"{prefix}:{mid}:1x2:draw")
    kb.button(text="🚌 Гости", callback_data=f"{prefix}:{mid}:1x2:away")
    kb.adjust(1)
    return kb.as_markup()
def profile_kb(viewer_id: int, target_id: int):
    kb = InlineKeyboardBuilder()
    if viewer_id != target_id:
        if is_following(viewer_id, target_id):
            kb.button(text="💔 Отписаться", callback_data=f"follow:{target_id}:off")
        else:
            kb.button(text="⭐ Подписаться", callback_data=f"follow:{target_id}:on")
    kb.button(text="🔗 Поделиться профилем", callback_data=f"share:{target_id}")
    kb.adjust(1)
    return kb.as_markup()
# ===================== Parsing helpers =====================
def parse_match_button(text: str) -> Optional[int]:
    text = (text or "").strip()
    if not text.startswith("🏟 #"):
        return None
    try:
        after_hash = text.split("#", 1)[1]
        mid_str = after_hash.split(" ", 1)[0]
        return int(mid_str)
    except Exception:
        return None
def start_payload(message: Message) -> Optional[str]:
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) == 2:
        return parts[1].strip()
    return None
# ===================== Notifications / Achievements / Rank =====================
async def safe_dm(bot: Bot, user_id: int, text: str):
    try:
        await bot.send_message(user_id, text)
    except Exception:
        # user may not have started bot / blocked bot
        pass
def achievement_unlocked(user_id: int, season: str, key: str) -> bool:
    with db() as con:
        exists = con.execute("""
            SELECT 1 FROM user_achievements
            WHERE user_id=? AND season=? AND key=?
        """, (user_id, season, key)).fetchone()
        if exists:
            return False
        con.execute("""
            INSERT INTO user_achievements(user_id, season, key, created_at)
            VALUES(?,?,?,?)
        """, (user_id, season, key, now_iso()))
        con.commit()
        return True
def achievement_text(key: str) -> str:
    return {
        "first_pred": "🟢 Первое участие — ты сделал первый прогноз!",
        "first_win": "🏅 Первая победа — ты впервые угадал!",
        "streak3": "🔥 Серия 3 — ты угадал 3 раза подряд!",
        "streak5": "💎 Серия 5 — ты угадал 5 раз подряд!",
    }.get(key, f"🏆 Достижение: {key}")
async def check_and_notify_achievements(bot: Bot, season: str, user_id: int):
    with db() as con:
        row = con.execute("""
            SELECT correct, total, streak
            FROM scores_season
            WHERE season=? AND user_id=?
        """, (season, user_id)).fetchone()
    if not row:
        return
    total = int(row["total"])
    correct = int(row["correct"])
    streak = int(row["streak"])
    if total >= 1 and achievement_unlocked(user_id, season, "first_pred"):
        await safe_dm(bot, user_id, "🏅 Новое достижение!\n" + achievement_text("first_pred"))
    if correct >= 1 and achievement_unlocked(user_id, season, "first_win"):
        await safe_dm(bot, user_id, "🏅 Новое достижение!\n" + achievement_text("first_win"))
    if streak >= 3 and achievement_unlocked(user_id, season, "streak3"):
        await safe_dm(bot, user_id, "🏅 Новое достижение!\n" + achievement_text("streak3"))
    if streak >= 5 and achievement_unlocked(user_id, season, "streak5"):
        await safe_dm(bot, user_id, "🏅 Новое достижение!\n" + achievement_text("streak5"))
async def notify_rank_change(bot: Bot, season: str, user_id: int):
    new_rank = get_rank(season, user_id)
    old_rank = get_last_rank(user_id)
    if new_rank is None:
        return
    if old_rank is None:
        set_last_rank(user_id, new_rank)
        await safe_dm(bot, user_id, f"🏆 Ты появился в рейтинге сезона {season}!\nТекущее место: #{new_rank}")
        return
    if new_rank < old_rank:
        set_last_rank(user_id, new_rank)
        await safe_dm(bot, user_id, f"📈 Ты поднялся в рейтинге сезона {season}!\nТеперь ты #{new_rank} (было #{old_rank}).")
    else:
        set_last_rank(user_id, new_rank)
# ===================== Scoring =====================
def can_score(mid: int, bet_type: str) -> bool:
    with db() as con:
        r = con.execute("SELECT scored FROM results WHERE match_id=? AND bet_type=?", (mid, bet_type)).fetchone()
        return not (r and int(r["scored"]) == 1)
def set_result_and_score(mid: int, bet_type: str, result: str) -> Tuple[str, int]:
    """
    Sets result once per match+bet_type and scores current season:
    - total +1 for each participant
    - if correct: points+1, correct+1, streak+1, best_streak update
    - if wrong: streak = 0
    """
    if not can_score(mid, bet_type):
        return ("already", 0)
    season = current_season()
    with db() as con:
        m = con.execute("SELECT id FROM matches WHERE id=?", (mid,)).fetchone()
        if not m:
            return ("not_found", 0)
        con.execute("""
            INSERT INTO results(match_id, bet_type, result, scored)
            VALUES(?,?,?,1)
            ON CONFLICT(match_id, bet_type) DO UPDATE SET
              result=excluded.result,
              scored=1
        """, (mid, bet_type, result))
        voters = con.execute("""
            SELECT user_id, COALESCE(username,'') as username, choice
            FROM votes
            WHERE match_id=? AND bet_type=?
        """, (mid, bet_type)).fetchall()
        winners_count = 0
        for v in voters:
            uid = int(v["user_id"])
            uname = v["username"]
            choice = v["choice"]
            ensure_score_row(season, uid, uname)
            # total +1
            con.execute("""
                UPDATE scores_season
                SET total = total + 1,
                    username = COALESCE(?, username)
                WHERE season=? AND user_id=?
            """, (uname, season, uid))
            if choice == result:
                winners_count += 1
                con.execute("""
                    UPDATE scores_season
                    SET points = points + 1,
                        correct = correct + 1,
                        streak = streak + 1,
                        best_streak = CASE
                            WHEN (streak + 1) > best_streak THEN (streak + 1)
                            ELSE best_streak
                        END
                    WHERE season=? AND user_id=?
                """, (season, uid))
            else:
                con.execute("""
                    UPDATE scores_season
                    SET streak = 0
                    WHERE season=? AND user_id=?
                """, (season, uid))
        con.commit()
    return ("ok", winners_count)
def format_match_stats(mid: int, title: str) -> str:
    totals_map, data = match_stats(mid)
    if not totals_map:
        return f"📈 Статистика по матчу #{mid}: {title}\n\nПока никто не голосовал."
    lines = [f"📈 Статистика по матчу #{mid}: {title}"]
    for bt in ["1x2"]:
        total = totals_map.get(bt, 0)
        if total == 0:
            continue
        lines.append(f"\n**{BET_LABEL[bt]}** (всего: {total})")
        choices = data.get(bt, {})
        for ch, cnt in sorted(choices.items(), key=lambda x: (-x[1], x[0])):
            pct = (cnt / total) * 100
            lines.append(f"• {choice_label(bt, ch)} — {cnt} ({pct:.1f}%)")
    return "\n".join(lines)
def get_profile(season: str, user_id: int):
    with db() as con:
        u = con.execute("""
            SELECT user_id, username, first_name, last_name
            FROM users WHERE user_id=?
        """, (user_id,)).fetchone()
        s = con.execute("""
            SELECT points, correct, total, streak, best_streak
            FROM scores_season
            WHERE season=? AND user_id=?
        """, (season, user_id)).fetchone()
    return u, s
def display_name(urow: sqlite3.Row) -> str:
    if not urow:
        return "unknown"
    if urow["username"]:
        return f"@{urow['username']}"
    fn = (urow["first_name"] or "").strip()
    ln = (urow["last_name"] or "").strip()
    n = (fn + " " + ln).strip()
    return n if n else f"id:{urow['user_id']}"
def profile_text(season: str, urow, srow) -> str:
    name = display_name(urow)
    followers = count_followers(int(urow["user_id"]))
    following = count_following(int(urow["user_id"]))
    rank = get_rank(season, int(urow["user_id"]))
    rank_text = f"#{rank}" if rank else "—"
    if srow:
        pts = int(srow["points"])
        correct = int(srow["correct"])
        total = int(srow["total"])
        streak = int(srow["streak"])
        best = int(srow["best_streak"])
        acc = (correct / total * 100) if total else 0.0
    else:
        pts = correct = total = streak = best = 0
        acc = 0.0
    return (
        f"👤 Профиль {name}\n"
        f"Сезон: {season}\n\n"
        f"🏆 Очки: {pts}\n"
        f"🎯 Точность: {acc:.1f}% ({correct}/{total})\n"
        f"🔥 Серия: {streak}\n"
        f"💎 Лучшая серия: {best}\n"
        f"📊 Место в рейтинге: {rank_text}\n\n"
        f"⭐ Подписчики: {followers}\n"
        f"➡️ Подписок: {following}"
    )
# ===================== Handlers =====================
@dp.message(CommandStart())
async def start(message: Message):
    upsert_user(message.from_user.id, message.from_user.username, message.from_user.first_name, message.from_user.last_name)
    ensure_score_row(current_season(), message.from_user.id, message.from_user.username)
    set_state(message.from_user.id, None)
    payload = start_payload(message)
    if payload and payload.startswith("profile_"):
        try:
            uid = int(payload.split("_", 1)[1])
            await send_profile(message, uid)
            return
        except Exception:
            pass
    await message.answer(
        "🤖 Predictor Bot\n\nМеню снизу 👇",
        reply_markup=main_menu_kb(is_admin(message.from_user.id))
    )
@dp.message(F.text == BTN_BACK)
async def back_to_main(message: Message):
    upsert_user(message.from_user.id, message.from_user.username, message.from_user.first_name, message.from_user.last_name)
    set_state(message.from_user.id, None)
    await message.answer("Главное меню 👇", reply_markup=main_menu_kb(is_admin(message.from_user.id)))
@dp.message(F.text == BTN_ACTIVE)
async def active_matches(message: Message):
    upsert_user(message.from_user.id, message.from_user.username, message.from_user.first_name, message.from_user.last_name)
    set_state(message.from_user.id, None)
    matches = get_open_matches()
    if not matches:
        await message.answer("Нет активных матчей.", reply_markup=main_menu_kb(is_admin(message.from_user.id)))
        return
    matches_text = "
".join([f"🏟 #{r['id']} {r['title']}" for r in matches[:40]])
    if len(matches) > 40:
        matches_text += f"
…и ещё {len(matches)-40} матч(ей)."
    await message.answer(
        "Активные матчи:
" + matches_text + "

Выбери матч в меню ниже 👇
(нажми «⚽ Активные матчи», чтобы обновить список)",
        reply_markup=combined_menu_kb(matches, is_admin(message.from_user.id))
    )
@dp.message(F.text.startswith("🏟 #"))
async def picked_match(message: Message):
    upsert_user(message.from_user.id, message.from_user.username, message.from_user.first_name, message.from_user.last_name)
    set_state(message.from_user.id, None)
    mid = parse_match_button(message.text)
    if mid is None:
        await message.answer("Не понял матч. Нажми «⚽ Активные матчи» ещё раз.")
        return
    row = get_match(mid)
    if not row:
        await message.answer("Матч не найден. Обнови список: «⚽ Активные матчи».")
        return
    await message.answer(
        f"Матч #{mid}: {row['title']}\nСтатус: {row['status']}\n\nВыбирай действие:",
        reply_markup=match_menu_kb(mid, is_admin(message.from_user.id))
    )
@dp.callback_query(F.data.startswith("match:"))
async def match_menu(call: CallbackQuery):
    upsert_user(call.from_user.id, call.from_user.username, call.from_user.first_name, call.from_user.last_name)
    _, mid, action = call.data.split(":")
    mid = int(mid)
    row = get_match(mid)
    if not row:
        await call.answer("Матч не найден.", show_alert=True)
        return
    if action == "vote":
        if row["status"] != "open":
            await call.answer("Матч закрыт для прогнозов.", show_alert=True)
            return
        await call.message.edit_text(
            f"Матч #{mid}: {row['title']}\n\nВыбери исход 1X2:",
            reply_markup=kb_1x2(mid, "vote")
        )
        await call.answer()
        return
    if action == "stats":
        text = format_match_stats(mid, row["title"])
        await call.message.edit_text(text, parse_mode="Markdown", reply_markup=match_menu_kb(mid, is_admin(call.from_user.id)))
        await call.answer()
        return
@dp.callback_query(F.data.startswith("vote:"))
async def vote_cb(call: CallbackQuery):
    upsert_user(call.from_user.id, call.from_user.username, call.from_user.first_name, call.from_user.last_name)
    _, mid, bt, choice = call.data.split(":", 3)
    mid = int(mid)
    season = current_season()
    ensure_score_row(season, call.from_user.id, call.from_user.username)
    with db() as con:
        st = con.execute("SELECT status FROM matches WHERE id=?", (mid,)).fetchone()
        if not st:
            await call.answer("Матч не найден.", show_alert=True)
            return
        if st["status"] != "open":
            await call.answer("Матч закрыт для прогнозов.", show_alert=True)
            return
        con.execute("""
            INSERT OR REPLACE INTO votes(match_id,user_id,username,bet_type,choice,created_at)
            VALUES(?,?,?,?,?,?)
        """, (mid, call.from_user.id, call.from_user.username, bt, choice, now_iso()))
        con.commit()
    await call.answer(f"Сохранено: {BET_LABEL.get(bt, bt)} ✅", show_alert=True)
    await check_and_notify_achievements(call.bot, season, call.from_user.id)
@dp.message(F.text == BTN_MY)
async def my_votes(message: Message):
    upsert_user(message.from_user.id, message.from_user.username, message.from_user.first_name, message.from_user.last_name)
    set_state(message.from_user.id, None)
    with db() as con:
        rows = con.execute("""
            SELECT v.match_id, m.title, v.bet_type, v.choice, m.status, v.created_at
            FROM votes v
            JOIN matches m ON m.id=v.match_id
            WHERE v.user_id=?
            ORDER BY v.match_id DESC, v.created_at DESC
        """, (message.from_user.id,)).fetchall()
    if not rows:
        await message.answer("Ты ещё не делал прогнозов.", reply_markup=main_menu_kb(is_admin(message.from_user.id)))
        return
    text = "📊 Твои прогнозы:\n"
    for r in rows[:30]:
        text += (
            f"\n• #{r['match_id']} {r['title']} ({r['status']})\n"
            f"  {BET_LABEL.get(r['bet_type'], r['bet_type'])} → {choice_label(r['bet_type'], r['choice'])}"
        )
    await message.answer(text, reply_markup=main_menu_kb(is_admin(message.from_user.id)))
@dp.message(F.text == BTN_LB)
async def leaderboard(message: Message):
    upsert_user(message.from_user.id, message.from_user.username, message.from_user.first_name, message.from_user.last_name)
    set_state(message.from_user.id, None)
    season = current_season()
    with db() as con:
        rows = con.execute("""
            SELECT COALESCE(username,'') as username, user_id, points, correct, total
            FROM scores_season
            WHERE season=?
            ORDER BY points DESC, user_id ASC
            LIMIT 20
        """, (season,)).fetchall()
    if not rows:
        await message.answer("Пока нет очков в этом сезоне.", reply_markup=main_menu_kb(is_admin(message.from_user.id)))
        return
    text = f"🏆 Лидерборд сезона {season} (топ-20):\n"
    for i, r in enumerate(rows, start=1):
        uname = r["username"]
        name = f"@{uname}" if uname else f"id:{r['user_id']}"
        total = int(r["total"])
        correct = int(r["correct"])
        acc = (correct / total * 100) if total else 0.0
        text += f"\n{i}. {name} — {int(r['points'])} pts | {acc:.0f}%"
    await message.answer(text, reply_markup=main_menu_kb(is_admin(message.from_user.id)))
@dp.message(F.text == BTN_PROFILE)
async def my_profile(message: Message):
    upsert_user(message.from_user.id, message.from_user.username, message.from_user.first_name, message.from_user.last_name)
    set_state(message.from_user.id, None)
    await send_profile(message, message.from_user.id)
# ===== Find player flow =====
@dp.message(F.text == BTN_FIND)
async def find_player_start(message: Message):
    upsert_user(message.from_user.id, message.from_user.username, message.from_user.first_name, message.from_user.last_name)
    set_state(message.from_user.id, "await_find_username")
    await message.answer(
        "🔎 Напиши @username игрока (или просто username).\n\n"
        "Важно: игрок должен хотя бы раз запустить бота, чтобы появился в базе.",
        reply_markup=main_menu_kb(is_admin(message.from_user.id))
    )
@dp.message(Command("find"))
async def find_player_cmd(message: Message):
    upsert_user(message.from_user.id, message.from_user.username, message.from_user.first_name, message.from_user.last_name)
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        set_state(message.from_user.id, "await_find_username")
        await message.answer("Напиши @username игрока (или просто username).")
        return
    username = parts[1].strip()
    uid = find_user_by_username(username)
    if not uid:
        await message.answer("Не нашёл такого игрока. Проверь @username.")
        return
    set_state(message.from_user.id, None)
    await send_profile(message, uid)
@dp.message(F.text.startswith('@'))
async def state_router(message: Message):
    st = get_state(message.from_user.id)
    if st != "await_find_username":
        return
    upsert_user(message.from_user.id, message.from_user.username, message.from_user.first_name, message.from_user.last_name)
    username = (message.text or "").strip()
    uid = find_user_by_username(username)
    set_state(message.from_user.id, None)
    if not uid:
        await message.answer("Не нашёл такого игрока. Попробуй ещё раз: @username")
        return
    await send_profile(message, uid)
async def send_profile(message_or_call, target_user_id: int):
    season = current_season()
    urow, srow = get_profile(season, target_user_id)
    if not urow:
        if isinstance(message_or_call, Message):
            await message_or_call.answer("Профиль не найден.")
        else:
            await message_or_call.answer("Профиль не найден.", show_alert=True)
        return
    viewer_id = message_or_call.from_user.id if hasattr(message_or_call, "from_user") else None
    text = profile_text(season, urow, srow)
    if isinstance(message_or_call, Message):
        await message_or_call.answer(text, reply_markup=main_menu_kb(is_admin(message_or_call.from_user.id)), disable_web_page_preview=True)
        await message_or_call.answer("Действия:", reply_markup=profile_kb(message_or_call.from_user.id, target_user_id))
    else:
        await message_or_call.message.edit_text(text, reply_markup=profile_kb(viewer_id, target_user_id), disable_web_page_preview=True)
@dp.callback_query(F.data.startswith("follow:"))
async def follow_cb(call: CallbackQuery):
    upsert_user(call.from_user.id, call.from_user.username, call.from_user.first_name, call.from_user.last_name)
    _, target_id_str, mode = call.data.split(":")
    target_id = int(target_id_str)
    if mode == "on":
        res = follow_user(call.from_user.id, target_id)
        if res == "self":
            await call.answer("Нельзя подписаться на себя 🙂", show_alert=True)
            return
        if res == "already":
            await call.answer("Ты уже подписан.", show_alert=True)
        else:
            await call.answer("Подписка оформлена ⭐", show_alert=True)
            await safe_dm(
                call.bot,
                target_id,
                f"⭐ У тебя новый подписчик: @{call.from_user.username}" if call.from_user.username else "⭐ У тебя новый подписчик!"
            )
    else:
        unfollow_user(call.from_user.id, target_id)
        await call.answer("Отписался ✅", show_alert=True)
    # refresh profile card in the same message
    season = current_season()
    urow, srow = get_profile(season, target_id)
    if not urow:
        return
    await call.message.edit_text(profile_text(season, urow, srow), reply_markup=profile_kb(call.from_user.id, target_id))
@dp.callback_query(F.data.startswith("share:"))
async def share_profile(call: CallbackQuery):
    upsert_user(call.from_user.id, call.from_user.username, call.from_user.first_name, call.from_user.last_name)
    _, target_id_str = call.data.split(":")
    target_id = int(target_id_str)
    if not BOT_USERNAME:
        await call.answer("Не могу получить username бота.", show_alert=True)
        return
    link = f"https://t.me/{BOT_USERNAME}?start=profile_{target_id}"
    await call.answer("Ссылка готова ✅", show_alert=True)
    await call.message.reply(f"🔗 Ссылка на профиль:\n{link}")
@dp.message(F.text == BTN_HELP)
async def help_menu(message: Message):
    upsert_user(message.from_user.id, message.from_user.username, message.from_user.first_name, message.from_user.last_name)
    set_state(message.from_user.id, None)
    await message.answer(
        "ℹ️ Помощь\n\n"
        "• «⚽ Активные матчи» → выбирай матч → прогноз/статистика\n"
        "• «🏆 Лидерборд» — топ сезона\n"
        "• «👤 Профиль» — твой профиль\n"
        "• «🔎 Найти игрока» — поиск по @username\n\n"
        "Можно также:\n"
        "/find @username\n\n"
        "Админ:\n"
        "/newmatch <название>",
        reply_markup=main_menu_kb(is_admin(message.from_user.id))
    )
# ===== ADMIN: create match =====
@dp.message(F.text == BTN_NEW)
async def newmatch_hint(message: Message):
    if not is_admin(message.from_user.id):
        return
    await message.answer("Создай матч командой:\n/newmatch Real vs Barca (02.02 20:00)")
@dp.message(Command("newmatch"))
async def newmatch_cmd(message: Message):
    upsert_user(message.from_user.id, message.from_user.username, message.from_user.first_name, message.from_user.last_name)
    if not is_admin(message.from_user.id):
        return
    title = message.text.replace("/newmatch", "").strip()
    if not title:
        await message.answer("Формат: /newmatch <название>")
        return
    with db() as con:
        con.execute("INSERT INTO matches(title,status) VALUES(?, 'open')", (title,))
        con.commit()
    await message.answer("Матч создан ✅\nНажми «⚽ Активные матчи», чтобы увидеть его в списке.")
# ===== Admin actions (close / set result) =====
@dp.message(Command("sync_now"))
async def sync_now_cmd(message: Message):
    upsert_user(message.from_user.id, message.from_user.username, message.from_user.first_name, message.from_user.last_name)
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔️ Только админ.")
        return
    await message.answer("🔄 Запускаю автосинк источников...")
    report = await sync_all_sources(message.bot, lookahead_days=SYNC_LOOKAHEAD_DAYS)
    await message.answer(report)
@dp.callback_query(F.data.startswith("admin:"))
async def admin_actions(call: CallbackQuery):
    upsert_user(call.from_user.id, call.from_user.username, call.from_user.first_name, call.from_user.last_name)
    if not is_admin(call.from_user.id):
        await call.answer("Только для админа.", show_alert=True)
        return
    _, mid, action = call.data.split(":")
    mid = int(mid)
    row = get_match(mid)
    if not row:
        await call.answer("Матч не найден.", show_alert=True)
        return
    if action == "close":
        close_match(mid)
        await call.answer("Матч закрыт ✅", show_alert=True)
        await call.message.edit_text(
            f"Матч #{mid}: {row['title']}\nСтатус: closed\n\nВыбирай действие:",
            reply_markup=match_menu_kb(mid, True)
        )
        return
    if action == "setresult":
        await call.message.edit_text(
            f"Матч #{mid}: {row['title']}\n\nПоставь результат 1X2 (и начисляем очки):",
            reply_markup=kb_1x2(mid, "setr")
        )
        await call.answer()
        return
@dp.callback_query(F.data.startswith("setr:"))
async def set_result_cb(call: CallbackQuery):
    upsert_user(call.from_user.id, call.from_user.username, call.from_user.first_name, call.from_user.last_name)
    if not is_admin(call.from_user.id):
        await call.answer("Только для админа.", show_alert=True)
        return
    _, mid, bt, result = call.data.split(":", 3)
    mid = int(mid)
    m = get_match(mid)
    if not m:
        await call.answer("Матч не найден.", show_alert=True)
        return
    totals_map, data = match_stats(mid)
    total_this_type = totals_map.get(bt, 0)
    dist = data.get(bt, {})
    status, winners_count = set_result_and_score(mid, bt, result)
    if status == "already":
        await call.answer("Результат уже выставлен ранее.", show_alert=True)
        return
    if status == "not_found":
        await call.answer("Матч не найден.", show_alert=True)
        return
    season = current_season()
    with db() as con:
        voters = con.execute("""
            SELECT user_id, COALESCE(username,'') as username, choice
            FROM votes
            WHERE match_id=? AND bet_type=?
        """, (mid, bt)).fetchall()
        bot = call.bot
        for v in voters:
            uid = int(v["user_id"])
            ch = v["choice"]
            correct = (ch == result)
            srow = con.execute("""
                SELECT points, streak
                FROM scores_season WHERE season=? AND user_id=?
            """, (season, uid)).fetchone()
            pts = int(srow["points"]) if srow else 0
            streak = int(srow["streak"]) if srow else 0
            crowd_line = ""
            if total_this_type > 0:
                ordered = sorted(dist.items(), key=lambda x: (-x[1], x[0]))[:3]
                parts = []
                for opt, cnt in ordered:
                    pct = cnt / total_this_type * 100
                    parts.append(f"{choice_label(bt, opt)} {pct:.0f}%")
                crowd_line = "👥 Толпа: " + " • ".join(parts)
            text = (
                f"⚽ Результат по матчу: {m['title']}\n"
                f"Тип: {BET_LABEL.get(bt, bt)}\n\n"
                f"✅ Правильный ответ: {choice_label(bt, result)}\n"
                f"Твой прогноз: {choice_label(bt, ch)}\n"
                f"{'🎉 Ты угадал! +1 очко' if correct else '❌ Не угадал. +0'}\n\n"
                f"🏆 Твои очки в сезоне {season}: {pts}\n"
                f"🔥 Текущая серия: {streak}\n"
            )
            if crowd_line:
                text += "\n" + crowd_line
            await safe_dm(bot, uid, text)
            await check_and_notify_achievements(bot, season, uid)
            await notify_rank_change(bot, season, uid)
    await call.answer("Готово ✅", show_alert=True)
    await call.message.edit_text(
        f"✅ Результат выставлен\nМатч #{mid}: {m['title']}\n"
        f"Тип: {BET_LABEL.get(bt, bt)}\n"
        f"Результат: {choice_label(bt, result)}\n\n"
        f"Очки начислены. Победителей: {winners_count}",
        reply_markup=match_menu_kb(mid, True)
    )
# ===================== MAIN =====================
async def main():
    global BOT_USERNAME
    logging.basicConfig(level=logging.INFO)
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN не задан")
    if ADMIN_ID == 0:
        raise RuntimeError("ADMIN_ID не задан (число)")
    init_db()
    bot = Bot(TOKEN)
    me = await bot.get_me()
    BOT_USERNAME = me.username
    # start autosync in background
    asyncio.create_task(autosync_loop(bot))
    asyncio.create_task(autoresults_loop(bot))
    asyncio.create_task(keepalive_loop())
    await dp.start_polling(bot)
if __name__ == "__main__":
    asyncio.run(main())
