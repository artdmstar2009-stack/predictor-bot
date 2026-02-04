import asyncio
import json
import logging
import os
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Optional, Tuple, List
from urllib.parse import urlencode, urlparse, parse_qs
from urllib.request import Request, urlopen

from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

# ===================== Async helpers =====================
async def to_thread_timeout(func, *args, timeout: int = 35, **kwargs):
    """Run blocking func in a thread with a hard timeout to avoid freezing the event loop."""
    return await asyncio.wait_for(asyncio.to_thread(func, *args, **kwargs), timeout=timeout)



# ===================== ENV / CONFIG =====================
TOKEN = (os.getenv("BOT_TOKEN") or "").strip()

try:
    ADMIN_ID = int((os.getenv("ADMIN_ID") or "0").strip())
except Exception:
    ADMIN_ID = 0

DB_PATH = (os.getenv("DB_PATH") or "predictor.db").strip()

FOOTBALL_DATA_TOKEN = (os.getenv("FOOTBALL_DATA_TOKEN") or "").strip()
FD_COMPETITIONS = (os.getenv("FD_COMPETITIONS") or "").strip()  # "PL,CL,PD"

AUTO_SYNC_ENABLED = (os.getenv("AUTO_SYNC_ENABLED") or "0").strip() == "1"

# OLD mode (UTC hour), kept for backward compatibility
try:
    AUTO_SYNC_HOUR_UTC = int((os.getenv("AUTO_SYNC_HOUR_UTC") or "8").strip())  # default 08:00 UTC
except Exception:
    AUTO_SYNC_HOUR_UTC = 8

# NEW mode (local timezone + local hour) for "04:00 London" with DST support
AUTO_SYNC_TZ = (os.getenv("AUTO_SYNC_TZ") or "").strip()  # e.g. "Europe/London"
try:
    AUTO_SYNC_HOUR_LOCAL = int((os.getenv("AUTO_SYNC_HOUR_LOCAL") or "4").strip())  # default 04:00 local
except Exception:
    AUTO_SYNC_HOUR_LOCAL = 4

CRON_SECRET = (os.getenv("CRON_SECRET") or "").strip()
# =======================================================

dp = Dispatcher()
BOT_USERNAME: Optional[str] = None

# Cron trigger flag (set by HTTP thread, read by async loop)
CRON_REQUESTED = False


# ===================== Render dummy HTTP server (+ optional cron trigger) =====================
class DummyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        global CRON_REQUESTED

        parsed = urlparse(self.path)
        if parsed.path == "/cron/sync":
            qs = parse_qs(parsed.query or "")
            key = (qs.get("key", [""])[0] or "").strip()

            if not CRON_SECRET:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"CRON_SECRET not set")
                return

            if key != CRON_SECRET:
                self.send_response(403)
                self.end_headers()
                self.wfile.write(b"Forbidden")
                return

            CRON_REQUESTED = True
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK: sync requested")
            return

        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

def run_http():
    port = int(os.environ.get("PORT", 10000))
    HTTPServer(("0.0.0.0", port), DummyHandler).serve_forever()

threading.Thread(target=run_http, daemon=True).start()
# ============================================================================================


# ===================== DB helpers =====================
def db():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA synchronous=NORMAL;")
    return con

def now_iso() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")

def today_key_utc() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d")

def current_season() -> str:
    return datetime.utcnow().strftime("%Y-%m")

def is_admin(uid: int) -> bool:
    return ADMIN_ID != 0 and int(uid) == int(ADMIN_ID)

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
            status TEXT NOT NULL DEFAULT 'open',
            is_featured INTEGER NOT NULL DEFAULT 0,
            bonus_multiplier REAL NOT NULL DEFAULT 1.0,
            external_id TEXT
        )
        """)
        con.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_matches_external_id ON matches(external_id)")

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

        con.execute("""
        CREATE TABLE IF NOT EXISTS follows(
            follower_id INTEGER NOT NULL,
            followee_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(follower_id, followee_id)
        )
        """)

        con.execute("""
        CREATE TABLE IF NOT EXISTS user_achievements(
            user_id INTEGER NOT NULL,
            season TEXT NOT NULL,
            key TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(user_id, season, key)
        )
        """)

        con.execute("""
        CREATE TABLE IF NOT EXISTS user_state(
            user_id INTEGER PRIMARY KEY,
            state TEXT,
            updated_at TEXT
        )
        """)

        con.execute("""
        CREATE TABLE IF NOT EXISTS daily_tasks(
            user_id INTEGER NOT NULL,
            day TEXT NOT NULL,
            task_key TEXT NOT NULL,
            goal INTEGER NOT NULL,
            progress INTEGER NOT NULL DEFAULT 0,
            claimed INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(user_id, day, task_key)
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

        con.execute("""
        CREATE TABLE IF NOT EXISTS duels(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            season TEXT NOT NULL,
            challenger_id INTEGER NOT NULL,
            opponent_id INTEGER NOT NULL,
            match_id INTEGER NOT NULL,
            bet_type TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            accepted_at TEXT,
            completed_at TEXT,
            winner_id INTEGER,
            notes TEXT
        )
        """)
        con.execute("""
        CREATE TABLE IF NOT EXISTS duel_predictions(
            duel_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            choice TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(duel_id, user_id)
        )
        """)
        con.execute("""
        CREATE TABLE IF NOT EXISTS duel_stats(
            season TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            username TEXT,
            wins INTEGER NOT NULL DEFAULT 0,
            losses INTEGER NOT NULL DEFAULT 0,
            draws INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(season, user_id)
        )
        """)
        con.commit()


# ===================== Core data ops =====================
def upsert_user(user: Message | CallbackQuery):
    u = user.from_user
    with db() as con:
        row = con.execute("SELECT user_id FROM users WHERE user_id=?", (u.id,)).fetchone()
        if row:
            con.execute("""
                UPDATE users
                SET username=?, first_name=?, last_name=?, last_seen=?
                WHERE user_id=?
            """, (u.username, u.first_name, u.last_name, now_iso(), u.id))
        else:
            con.execute("""
                INSERT INTO users(user_id, username, first_name, last_name, created_at, last_seen)
                VALUES(?,?,?,?,?,?)
            """, (u.id, u.username, u.first_name, u.last_name, now_iso(), now_iso()))
        con.commit()

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

def ensure_score_row(season: str, user_id: int, username: Optional[str]):
    with db() as con:
        r = con.execute("SELECT 1 FROM scores_season WHERE season=? AND user_id=?", (season, user_id)).fetchone()
        if not r:
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

def get_rank(season: str, user_id: int) -> Optional[int]:
    with db() as con:
        rows = con.execute("""
            SELECT user_id
            FROM scores_season
            WHERE season=?
            ORDER BY points DESC, user_id ASC
        """, (season,)).fetchall()
    for i, r in enumerate(rows, start=1):
        if int(r["user_id"]) == int(user_id):
            return i
    return None

def get_last_rank(user_id: int) -> Optional[int]:
    with db() as con:
        r = con.execute("SELECT last_rank_season FROM users WHERE user_id=?", (user_id,)).fetchone()
        return r["last_rank_season"] if r else None

def set_last_rank(user_id: int, rank: Optional[int]):
    with db() as con:
        con.execute("UPDATE users SET last_rank_season=? WHERE user_id=?", (rank, user_id))
        con.commit()

def get_open_matches():
    with db() as con:
        return con.execute("""
            SELECT id, title, status, is_featured, bonus_multiplier
            FROM matches
            WHERE status='open'
            ORDER BY is_featured DESC, id DESC
        """).fetchall()

def get_match(mid: int):
    with db() as con:
        return con.execute("""
            SELECT id, title, status, is_featured, bonus_multiplier, external_id
            FROM matches WHERE id=?
        """, (mid,)).fetchone()

def close_match(mid: int):
    with db() as con:
        con.execute("UPDATE matches SET status='closed' WHERE id=?", (mid,))
        con.commit()

def match_exists_by_ext(ext_id: str) -> bool:
    with db() as con:
        r = con.execute("SELECT 1 FROM matches WHERE external_id=?", (ext_id,)).fetchone()
        return r is not None

def create_match(title: str, featured: int = 0, mult: float = 1.0, external_id: Optional[str] = None) -> int:
    with db() as con:
        cur = con.execute("""
            INSERT INTO matches(title, status, is_featured, bonus_multiplier, external_id)
            VALUES(?, 'open', ?, ?, ?)
        """, (title, int(featured), float(mult), external_id))
        con.commit()
        return int(cur.lastrowid)

def get_match_multiplier(mid: int) -> float:
    m = get_match(mid)
    if not m:
        return 1.0
    if int(m["is_featured"]) == 1:
        try:
            return float(m["bonus_multiplier"])
        except Exception:
            return 1.0
    return 1.0

def match_stats(mid: int) -> Tuple[dict, dict]:
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
        data.setdefault(r["bet_type"], {})
        data[r["bet_type"]][r["choice"]] = int(r["c"])
    return totals_map, data


# ===================== Social (follow/share/find) =====================
def is_following(follower_id: int, followee_id: int) -> bool:
    with db() as con:
        r = con.execute("SELECT 1 FROM follows WHERE follower_id=? AND followee_id=?", (follower_id, followee_id)).fetchone()
        return r is not None

def follow_user(follower_id: int, followee_id: int) -> str:
    if follower_id == followee_id:
        return "self"
    with db() as con:
        try:
            con.execute("INSERT INTO follows(follower_id, followee_id, created_at) VALUES(?,?,?)",
                        (follower_id, followee_id, now_iso()))
            con.commit()
            return "ok"
        except sqlite3.IntegrityError:
            return "already"

def unfollow_user(follower_id: int, followee_id: int):
    with db() as con:
        con.execute("DELETE FROM follows WHERE follower_id=? AND followee_id=?", (follower_id, followee_id))
        con.commit()

def count_followers(uid: int) -> int:
    with db() as con:
        r = con.execute("SELECT COUNT(*) as c FROM follows WHERE followee_id=?", (uid,)).fetchone()
        return int(r["c"])

def count_following(uid: int) -> int:
    with db() as con:
        r = con.execute("SELECT COUNT(*) as c FROM follows WHERE follower_id=?", (uid,)).fetchone()
        return int(r["c"])

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


# ===================== Achievements + notifications =====================
async def safe_dm(bot: Bot, user_id: int, text: str):
    try:
        await bot.send_message(user_id, text)
    except Exception:
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
        await safe_dm(bot, user_id, "🏅 Достижение: первый прогноз!")
    if correct >= 1 and achievement_unlocked(user_id, season, "first_win"):
        await safe_dm(bot, user_id, "🏅 Достижение: первая победа!")
    if streak >= 3 and achievement_unlocked(user_id, season, "streak3"):
        await safe_dm(bot, user_id, "🏅 Достижение: серия 3!")
    if streak >= 5 and achievement_unlocked(user_id, season, "streak5"):
        await safe_dm(bot, user_id, "🏅 Достижение: серия 5!")

async def notify_rank_change(bot: Bot, season: str, user_id: int):
    new_rank = get_rank(season, user_id)
    old_rank = get_last_rank(user_id)
    if new_rank is None:
        return

    if old_rank is None:
        set_last_rank(user_id, new_rank)
        await safe_dm(bot, user_id, f"🏆 Ты появился в рейтинге сезона {season}! Место: #{new_rank}")
        return

    if new_rank < old_rank:
        set_last_rank(user_id, new_rank)
        await safe_dm(bot, user_id, f"📈 Ты поднялся в рейтинге! Теперь #{new_rank} (было #{old_rank}).")
    elif new_rank > old_rank:
        set_last_rank(user_id, new_rank)


# ===================== Daily tasks / quests =====================
TASK_DEFS = {
    "pred_3": {"name": "Сделай 3 прогноза сегодня", "goal": 3, "reward_points": 2},
    "correct_1": {"name": "Угадай 1 прогноз сегодня", "goal": 1, "reward_points": 3},
    "duel_1": {"name": "Сыграй 1 дуэль сегодня", "goal": 1, "reward_points": 3},
}

def ensure_daily_tasks(user_id: int):
    day = today_key_utc()
    with db() as con:
        for key, meta in TASK_DEFS.items():
            row = con.execute("""
                SELECT 1 FROM daily_tasks WHERE user_id=? AND day=? AND task_key=?
            """, (user_id, day, key)).fetchone()
            if not row:
                con.execute("""
                    INSERT INTO daily_tasks(user_id, day, task_key, goal, progress, claimed)
                    VALUES(?,?,?,?,0,0)
                """, (user_id, day, key, int(meta["goal"])))
        con.commit()

def inc_task_progress(user_id: int, task_key: str, amount: int = 1):
    day = today_key_utc()
    with db() as con:
        row = con.execute("""
            SELECT progress, goal, claimed FROM daily_tasks
            WHERE user_id=? AND day=? AND task_key=?
        """, (user_id, day, task_key)).fetchone()
        if not row:
            return
        if int(row["claimed"]) == 1:
            return
        progress = int(row["progress"])
        goal = int(row["goal"])
        if progress >= goal:
            return
        progress = min(goal, progress + amount)
        con.execute("""
            UPDATE daily_tasks SET progress=?
            WHERE user_id=? AND day=? AND task_key=?
        """, (progress, user_id, day, task_key))
        con.commit()

def get_tasks_for_user(user_id: int):
    day = today_key_utc()
    with db() as con:
        return con.execute("""
            SELECT task_key, goal, progress, claimed
            FROM daily_tasks
            WHERE user_id=? AND day=?
            ORDER BY task_key ASC
        """, (user_id, day)).fetchall()

def claim_task_reward(season: str, user_id: int, username: Optional[str], task_key: str) -> Tuple[bool, str]:
    day = today_key_utc()
    if task_key not in TASK_DEFS:
        return False, "Неизвестное задание"

    reward = int(TASK_DEFS[task_key]["reward_points"])
    with db() as con:
        row = con.execute("""
            SELECT goal, progress, claimed
            FROM daily_tasks
            WHERE user_id=? AND day=? AND task_key=?
        """, (user_id, day, task_key)).fetchone()
        if not row:
            return False, "Задание не найдено"
        if int(row["claimed"]) == 1:
            return False, "Награда уже получена"
        if int(row["progress"]) < int(row["goal"]):
            return False, "Задание ещё не выполнено"

        con.execute("""
            UPDATE daily_tasks SET claimed=1
            WHERE user_id=? AND day=? AND task_key=?
        """, (user_id, day, task_key))

        ensure_score_row(season, user_id, username)
        con.execute("""
            UPDATE scores_season
            SET points=points+?, username=COALESCE(?, username)
            WHERE season=? AND user_id=?
        """, (reward, username, season, user_id))
        con.commit()

    return True, f"🎁 Награда получена: +{reward} очков!"

def tasks_text(user_id: int) -> str:
    rows = get_tasks_for_user(user_id)
    if not rows:
        return "🎯 Заданий на сегодня нет."
    lines = ["🎯 Задания на сегодня:\n"]
    for r in rows:
        key = r["task_key"]
        meta = TASK_DEFS.get(key, {"name": key, "reward_points": 0})
        goal = int(r["goal"])
        prog = int(r["progress"])
        claimed = int(r["claimed"])
        status = "✅" if claimed else ("🎉" if prog >= goal else "⏳")
        lines.append(f"{status} {meta['name']} — {prog}/{goal} (награда +{meta['reward_points']})")
    return "\n".join(lines)

def tasks_kb(user_id: int):
    kb = InlineKeyboardBuilder()
    rows = get_tasks_for_user(user_id)
    for r in rows:
        key = r["task_key"]
        goal = int(r["goal"])
        prog = int(r["progress"])
        claimed = int(r["claimed"])
        if claimed == 0 and prog >= goal:
            kb.button(text=f"🎁 Забрать: {key}", callback_data=f"task:claim:{key}")
    kb.adjust(1)
    return kb.as_markup() if kb.buttons else None


# ===================== Duels =====================
BET_LABEL = {"1x2": "1X2", "score": "Точный счёт", "total": "Тотал", "btts": "Обе забьют"}

def create_duel(season: str, challenger_id: int, opponent_id: int, match_id: int, bet_type: str) -> int:
    with db() as con:
        cur = con.execute("""
            INSERT INTO duels(created_at, season, challenger_id, opponent_id, match_id, bet_type, status)
            VALUES(?,?,?,?,?,?, 'pending')
        """, (now_iso(), season, challenger_id, opponent_id, match_id, bet_type))
        con.commit()
        return int(cur.lastrowid)

def get_duel(duel_id: int):
    with db() as con:
        return con.execute("SELECT * FROM duels WHERE id=?", (duel_id,)).fetchone()

def set_duel_status(duel_id: int, status: str):
    with db() as con:
        if status == "accepted":
            con.execute("""
                UPDATE duels SET status='accepted', accepted_at=?
                WHERE id=? AND status='pending'
            """, (now_iso(), duel_id))
        else:
            con.execute("UPDATE duels SET status=? WHERE id=?", (status, duel_id))
        con.commit()

def save_duel_prediction(duel_id: int, user_id: int, choice: str):
    with db() as con:
        con.execute("""
            INSERT INTO duel_predictions(duel_id, user_id, choice, created_at)
            VALUES(?,?,?,?)
            ON CONFLICT(duel_id, user_id) DO UPDATE SET
                choice=excluded.choice,
                created_at=excluded.created_at
        """, (duel_id, user_id, choice, now_iso()))
        con.commit()

def get_duel_choice(duel_id: int, user_id: int) -> Optional[str]:
    with db() as con:
        r = con.execute("""
            SELECT choice FROM duel_predictions WHERE duel_id=? AND user_id=?
        """, (duel_id, user_id)).fetchone()
        return r["choice"] if r else None

def find_active_duels_for_vote(user_id: int, match_id: int, bet_type: str) -> List[int]:
    season = current_season()
    with db() as con:
        rows = con.execute("""
            SELECT id FROM duels
            WHERE season=? AND match_id=? AND bet_type=? AND status='accepted'
              AND (challenger_id=? OR opponent_id=?)
        """, (season, match_id, bet_type, user_id, user_id)).fetchall()
        return [int(r["id"]) for r in rows]

def upsert_duel_stats(season: str, user_id: int, username: Optional[str]):
    with db() as con:
        con.execute("""
            INSERT OR IGNORE INTO duel_stats(season, user_id, username, wins, losses, draws)
            VALUES(?,?,?,0,0,0)
        """, (season, user_id, username))
        con.execute("""
            UPDATE duel_stats SET username=COALESCE(?, username)
            WHERE season=? AND user_id=?
        """, (username, season, user_id))
        con.commit()

def complete_duel(season: str, duel_id: int, winner_id: Optional[int], notes: str,
                  challenger_id: int, opponent_id: int,
                  challenger_username: Optional[str], opponent_username: Optional[str]):
    with db() as con:
        con.execute("""
            UPDATE duels
            SET status='completed', completed_at=?, winner_id=?, notes=?
            WHERE id=? AND status='accepted'
        """, (now_iso(), winner_id, notes, duel_id))

        upsert_duel_stats(season, challenger_id, challenger_username)
        upsert_duel_stats(season, opponent_id, opponent_username)

        if winner_id is None:
            con.execute("""
                UPDATE duel_stats SET draws=draws+1
                WHERE season=? AND user_id IN (?,?)
            """, (season, challenger_id, opponent_id))
        else:
            loser_id = opponent_id if winner_id == challenger_id else challenger_id
            con.execute("""
                UPDATE duel_stats SET wins=wins+1
                WHERE season=? AND user_id=?
            """, (season, winner_id))
            con.execute("""
                UPDATE duel_stats SET losses=losses+1
                WHERE season=? AND user_id=?
            """, (season, loser_id))
        con.commit()

def get_duel_stats(season: str, user_id: int) -> Tuple[int, int, int]:
    with db() as con:
        r = con.execute("SELECT wins, losses, draws FROM duel_stats WHERE season=? AND user_id=?",
                        (season, user_id)).fetchone()
    if not r:
        return 0, 0, 0
    return int(r["wins"]), int(r["losses"]), int(r["draws"])


# ===================== Scoring =====================
def can_score(mid: int, bet_type: str) -> bool:
    with db() as con:
        r = con.execute("SELECT scored FROM results WHERE match_id=? AND bet_type=?", (mid, bet_type)).fetchone()
        return not (r and int(r["scored"]) == 1)

def set_result_and_score(mid: int, bet_type: str, result: str) -> Tuple[str, int, int]:
    if not can_score(mid, bet_type):
        return ("already", 0, 1)

    season = current_season()
    mult = get_match_multiplier(mid)
    points_per_win = int(round(1 * mult))
    if points_per_win < 1:
        points_per_win = 1

    with db() as con:
        m = con.execute("SELECT id FROM matches WHERE id=?", (mid,)).fetchone()
        if not m:
            return ("not_found", 0, points_per_win)

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
            ensure_daily_tasks(uid)

            con.execute("""
                UPDATE scores_season
                SET total=total+1, username=COALESCE(?, username)
                WHERE season=? AND user_id=?
            """, (uname, season, uid))

            if choice == result:
                winners_count += 1
                con.execute("""
                    UPDATE daily_tasks
                    SET progress = CASE
                      WHEN progress < goal AND claimed=0 THEN progress + 1
                      ELSE progress
                    END
                    WHERE user_id=? AND day=? AND task_key='correct_1'
                """, (uid, today_key_utc()))

                con.execute("""
                    UPDATE scores_season
                    SET points=points+?,
                        correct=correct+1,
                        streak=streak+1,
                        best_streak=CASE WHEN (streak+1)>best_streak THEN (streak+1) ELSE best_streak END
                    WHERE season=? AND user_id=?
                """, (points_per_win, season, uid))
            else:
                con.execute("""
                    UPDATE scores_season
                    SET streak=0
                    WHERE season=? AND user_id=?
                """, (season, uid))

        con.commit()

    return ("ok", winners_count, points_per_win)

def settle_duels_for_result(bot: Bot, mid: int, bet_type: str, result: str):
    season = current_season()
    with db() as con:
        duels = con.execute("""
            SELECT id, challenger_id, opponent_id
            FROM duels
            WHERE season=? AND match_id=? AND bet_type=? AND status='accepted'
        """, (season, mid, bet_type)).fetchall()

    async def _run():
        for d in duels:
            duel_id = int(d["id"])
            ch_id = int(d["challenger_id"])
            op_id = int(d["opponent_id"])

            ch_choice = get_duel_choice(duel_id, ch_id)
            op_choice = get_duel_choice(duel_id, op_id)

            if not ch_choice and not op_choice:
                winner = None
                notes = "Оба не сделали прогноз — ничья."
            elif ch_choice and not op_choice:
                winner = ch_id
                notes = "Соперник не сделал прогноз — тех.победа."
            elif op_choice and not ch_choice:
                winner = op_id
                notes = "Соперник не сделал прогноз — тех.победа."
            else:
                ch_win = (ch_choice == result)
                op_win = (op_choice == result)
                if ch_win and not op_win:
                    winner = ch_id
                    notes = "Победа: твой прогноз верный."
                elif op_win and not ch_win:
                    winner = op_id
                    notes = "Победа: твой прогноз верный."
                else:
                    winner = None
                    notes = "Оба угадали или оба не угадали — ничья."

            with db() as con2:
                ru = con2.execute("SELECT username FROM users WHERE user_id=?", (ch_id,)).fetchone()
                ou = con2.execute("SELECT username FROM users WHERE user_id=?", (op_id,)).fetchone()
            ch_un = (ru["username"] if ru else None)
            op_un = (ou["username"] if ou else None)

            complete_duel(season, duel_id, winner, notes, ch_id, op_id, ch_un, op_un)

            inc_task_progress(ch_id, "duel_1", 1)
            inc_task_progress(op_id, "duel_1", 1)

            await safe_dm(bot, ch_id, f"⚔️ Дуэль завершена!\n{notes}")
            await safe_dm(bot, op_id, f"⚔️ Дуэль завершена!\n{notes}")

    asyncio.create_task(_run())


# ===================== Fixtures sync (variant 3) =====================
def fetch_today_fixtures_from_fd(day_yyyy_mm_dd: str) -> list:
    if not FOOTBALL_DATA_TOKEN:
        return [{"__error__": "FOOTBALL_DATA_TOKEN not set"}]

    params = {"date": day_yyyy_mm_dd}
    url = "https://api.football-data.org/v4/matches?" + urlencode(params)
    req = Request(url, headers={"X-Auth-Token": FOOTBALL_DATA_TOKEN, "Accept": "application/json"})
    try:
        with urlopen(req, timeout=25) as r:
            raw = r.read().decode("utf-8")
            data = json.loads(raw)
            return data.get("matches", [])
    except Exception as e:
        logging.exception("football-data fetch failed")
        return [{"__error__": str(e), "__url__": url}]

def pending_save(ext_id: str, day: str, title: str, kickoff_utc: str, competition: str) -> Optional[int]:
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

def pending_get(pid: int):
    with db() as con:
        return con.execute("SELECT * FROM pending_fixtures WHERE id=?", (pid,)).fetchone()

def pending_delete(pid: int):
    with db() as con:
        con.execute("DELETE FROM pending_fixtures WHERE id=?", (pid,))
        con.commit()

def pending_kb(pid: int):
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Добавить", callback_data=f"pf:add:{pid}")
    kb.button(text="⭐ Матч дня x2", callback_data=f"pf:feat:{pid}:2")
    kb.button(text="❌ Пропустить", callback_data=f"pf:skip:{pid}")
    kb.adjust(1)
    return kb.as_markup()

async def sync_today_internal(bot: Bot, requested_by: str = "auto"):
    if ADMIN_ID == 0:
        return

    day = today_key_utc()
    fixtures = await to_thread_timeout(fetch_today_fixtures_from_fd, day, timeout=35)

    if fixtures and isinstance(fixtures[0], dict) and fixtures[0].get("__error__"):
        err = fixtures[0].get("__error__")
        url = fixtures[0].get("__url__", "")
        await safe_dm(bot, ADMIN_ID, f"⚠️ /sync_today ошибка ({requested_by}):\n{err}\n{url}".strip())
        return

    comps = set(x.strip() for x in FD_COMPETITIONS.split(",") if x.strip())
    sent = 0
    skipped = 0

    await safe_dm(bot, ADMIN_ID, f"🕓 Лондон 04:00 → синк матчей на {day} ({requested_by})\nК подтверждению отправляю карточки…")

    for m in fixtures:
        comp_code = ((m.get("competition") or {}).get("code") or "").strip()
        if comps and comp_code not in comps:
            continue

        ext_id = str(m.get("id") or "").strip()
        if not ext_id:
            continue
        if match_exists_by_ext(ext_id):
            skipped += 1
            continue

        home = ((m.get("homeTeam") or {}).get("name") or "").strip()
        away = ((m.get("awayTeam") or {}).get("name") or "").strip()
        if not home or not away:
            continue

        title = f"{home} vs {away}"
        kickoff = (m.get("utcDate") or "").strip()

        pid = pending_save(ext_id, day, title, kickoff, comp_code)
        if not pid:
            continue

        await bot.send_message(
            ADMIN_ID,
            f"📅 {day}\n🏟 {title}\nЛига: {comp_code or '—'}\nKickoff(UTC): {kickoff or '—'}",
            reply_markup=pending_kb(pid)
        )
        sent += 1
        await asyncio.sleep(0.25)

    await safe_dm(bot, ADMIN_ID, f"✅ Синк готов.\nК рассмотрению: {sent}\nПропущено дублей: {skipped}")


# ===================== UI / Menu =====================
BTN_ACTIVE = "⚽ Активные матчи"
BTN_MY = "📊 Мои прогнозы"
BTN_LB = "🏆 Лидерборд"
BTN_PROFILE = "👤 Профиль"
BTN_FIND = "🔎 Найти игрока"
BTN_HELP = "ℹ️ Помощь"
BTN_NEW = "➕ Создать матч"
BTN_BACK = "⬅️ Назад"

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
    for r in matches:
        star = "⭐ " if int(r["is_featured"]) == 1 else ""
        mult = float(r["bonus_multiplier"]) if r["bonus_multiplier"] is not None else 1.0
        bonus = f"(x{mult:g}) " if int(r["is_featured"]) == 1 and mult != 1.0 else ""
        kb.button(text=f"🏟 #{r['id']} {star}{bonus}{r['title']}")
    kb.button(text=BTN_BACK)
    if user_is_admin:
        kb.button(text=BTN_NEW)
    kb.adjust(1)
    return kb.as_markup(resize_keyboard=True)

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

def match_menu_kb(mid: int, user_is_admin: bool):
    kb = InlineKeyboardBuilder()
    kb.button(text="🗳 Сделать прогноз", callback_data=f"match:{mid}:vote")
    kb.button(text="📈 Статистика", callback_data=f"match:{mid}:stats")
    if user_is_admin:
        kb.button(text="🔒 Закрыть матч", callback_data=f"admin:{mid}:close")
        kb.button(text="✅ Указать результат", callback_data=f"admin:{mid}:setresult")
    kb.adjust(2)
    return kb.as_markup()

def bet_type_kb(mid: int, mode: str):
    kb = InlineKeyboardBuilder()
    kb.button(text="1️⃣ 1X2", callback_data=f"type:{mode}:{mid}:1x2")
    kb.button(text="🎯 Точный счёт", callback_data=f"type:{mode}:{mid}:score")
    kb.button(text="⚽ Тотал", callback_data=f"type:{mode}:{mid}:total")
    kb.button(text="🔥 Обе забьют", callback_data=f"type:{mode}:{mid}:btts")
    kb.adjust(2)
    return kb.as_markup()

def kb_1x2(mid: int, prefix: str):
    kb = InlineKeyboardBuilder()
    kb.button(text="🏠 Хозяева", callback_data=f"{prefix}:{mid}:1x2:home")
    kb.button(text="🤝 Ничья", callback_data=f"{prefix}:{mid}:1x2:draw")
    kb.button(text="🚌 Гости", callback_data=f"{prefix}:{mid}:1x2:away")
    kb.adjust(1)
    return kb.as_markup()

def kb_score(mid: int, prefix: str):
    kb = InlineKeyboardBuilder()
    for s in ["1:0", "2:1", "1:1", "0:0", "0:1", "1:2", "2:0", "0:2"]:
        kb.button(text=s, callback_data=f"{prefix}:{mid}:score:{s}")
    kb.adjust(2)
    return kb.as_markup()

def kb_total(mid: int, prefix: str):
    kb = InlineKeyboardBuilder()
    kb.button(text="⚽ Больше 2.5", callback_data=f"{prefix}:{mid}:total:over")
    kb.button(text="🧱 Меньше 2.5", callback_data=f"{prefix}:{mid}:total:under")
    kb.adjust(1)
    return kb.as_markup()

def kb_btts(mid: int, prefix: str):
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Да", callback_data=f"{prefix}:{mid}:btts:yes")
    kb.button(text="❌ Нет", callback_data=f"{prefix}:{mid}:btts:no")
    kb.adjust(1)
    return kb.as_markup()

def profile_kb(viewer_id: int, target_id: int):
    kb = InlineKeyboardBuilder()
    if viewer_id != target_id:
        if is_following(viewer_id, target_id):
            kb.button(text="💔 Отписаться", callback_data=f"follow:{target_id}:off")
        else:
            kb.button(text="⭐ Подписаться", callback_data=f"follow:{target_id}:on")
        kb.button(text="⚔️ Вызвать на дуэль", callback_data=f"duel:start:{target_id}")
    kb.button(text="🔗 Поделиться профилем", callback_data=f"share:{target_id}")
    kb.adjust(1)
    return kb.as_markup()


# ===================== Choice labels / stats text =====================
def choice_label(bt: str, choice: str) -> str:
    if bt == "1x2":
        return {"home": "🏠 Хозяева", "draw": "🤝 Ничья", "away": "🚌 Гости"}.get(choice, choice)
    if bt == "score":
        return f"🎯 {choice}"
    if bt == "total":
        return {"over": "⚽ Больше 2.5", "under": "🧱 Меньше 2.5"}.get(choice, choice)
    if bt == "btts":
        return {"yes": "✅ Да", "no": "❌ Нет"}.get(choice, choice)
    return choice

def format_match_stats(mid: int, title: str) -> str:
    totals_map, data = match_stats(mid)
    if not totals_map:
        return f"📈 Статистика по матчу #{mid}: {title}\n\nПока никто не голосовал."

    lines = [f"📈 Статистика по матчу #{mid}: {title}"]
    for bt in ["1x2", "score", "total", "btts"]:
        total = totals_map.get(bt, 0)
        if total == 0:
            continue
        lines.append(f"\n**{BET_LABEL[bt]}** (всего: {total})")
        choices = data.get(bt, {})
        for ch, cnt in sorted(choices.items(), key=lambda x: (-x[1], x[0])):
            pct = (cnt / total) * 100 if total else 0
            lines.append(f"• {choice_label(bt, ch)} — {cnt} ({pct:.1f}%)")
    return "\n".join(lines)


# ===================== Profile text =====================
def get_profile(season: str, user_id: int):
    with db() as con:
        u = con.execute("SELECT user_id, username, first_name, last_name FROM users WHERE user_id=?", (user_id,)).fetchone()
        s = con.execute("""
            SELECT points, correct, total, streak, best_streak
            FROM scores_season
            WHERE season=? AND user_id=?
        """, (season, user_id)).fetchone()
    return u, s

def display_name(urow) -> str:
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

    wins, losses, draws = get_duel_stats(season, int(urow["user_id"]))
    ensure_daily_tasks(int(urow["user_id"]))
    ttxt = tasks_text(int(urow["user_id"]))

    return (
        f"👤 Профиль {name}\n"
        f"Сезон: {season}\n\n"
        f"🏆 Очки: {pts}\n"
        f"🎯 Точность: {acc:.1f}% ({correct}/{total})\n"
        f"🔥 Серия: {streak}\n"
        f"💎 Лучшая серия: {best}\n"
        f"📊 Место в рейтинге: {rank_text}\n\n"
        f"⚔️ Дуэли: {wins}W / {losses}L / {draws}D\n\n"
        f"⭐ Подписчики: {followers}\n"
        f"➡️ Подписок: {following}\n\n"
        f"{ttxt}"
    )


# ===================== Commands / Handlers =====================
@dp.message(CommandStart())
async def start(message: Message):
    """
    + Поддержка deep-link:
      /start profile_123 -> показать публичный профиль
    """
    upsert_user(message)
    ensure_score_row(current_season(), message.from_user.id, message.from_user.username)
    ensure_daily_tasks(message.from_user.id)
    set_state(message.from_user.id, None)

    arg = (message.text or "").split(maxsplit=1)
    payload = arg[1].strip() if len(arg) > 1 else ""

    await message.answer("🤖 Predictor Bot\n\nМеню снизу 👇", reply_markup=main_menu_kb(is_admin(message.from_user.id)))

    if payload.startswith("profile_"):
        try:
            target_id = int(payload.split("_", 1)[1])
            await send_profile(message, target_id)
        except Exception:
            pass

@dp.message(Command("whoami"))
async def whoami(message: Message):
    upsert_user(message)
    await message.answer(
        "🧩 Диагностика\n"
        f"Твой id: {message.from_user.id}\n"
        f"ADMIN_ID(env): {ADMIN_ID}\n"
        f"Ты админ: {'ДА' if is_admin(message.from_user.id) else 'НЕТ'}\n\n"
        f"AUTO_SYNC_ENABLED: {AUTO_SYNC_ENABLED}\n"
        f"AUTO_SYNC_TZ: {AUTO_SYNC_TZ or '(не задан)'}\n"
        f"AUTO_SYNC_HOUR_LOCAL: {AUTO_SYNC_HOUR_LOCAL}\n"
        f"AUTO_SYNC_HOUR_UTC (fallback): {AUTO_SYNC_HOUR_UTC}\n\n"
        f"FOOTBALL_DATA_TOKEN: {'есть' if bool(FOOTBALL_DATA_TOKEN) else 'нет'}\n"
        f"FD_COMPETITIONS: {FD_COMPETITIONS or '(пусто)'}"
    )

@dp.message(F.text == BTN_BACK)
async def back_to_main(message: Message):
    upsert_user(message)
    set_state(message.from_user.id, None)
    await message.answer("Главное меню 👇", reply_markup=main_menu_kb(is_admin(message.from_user.id)))

@dp.message(F.text == BTN_ACTIVE)
async def active_matches(message: Message):
    upsert_user(message)
    set_state(message.from_user.id, None)
    matches = get_open_matches()
    if not matches:
        await message.answer("Нет активных матчей.", reply_markup=main_menu_kb(is_admin(message.from_user.id)))
        return
    await message.answer("Выбери матч кнопкой ниже 👇", reply_markup=matches_list_kb(matches, is_admin(message.from_user.id)))

@dp.message(F.text.startswith("🏟 #"))
async def picked_match(message: Message):
    upsert_user(message)
    set_state(message.from_user.id, None)

    mid = parse_match_button(message.text)
    if mid is None:
        await message.answer("Не понял матч. Нажми «⚽ Активные матчи» ещё раз.")
        return

    row = get_match(mid)
    if not row:
        await message.answer("Матч не найден. Обнови список: «⚽ Активные матчи».")
        return

    star = "⭐ " if int(row["is_featured"]) == 1 else ""
    mult = float(row["bonus_multiplier"]) if row["bonus_multiplier"] is not None else 1.0
    bonus = f"(x{mult:g}) " if int(row["is_featured"]) == 1 and mult != 1.0 else ""

    await message.answer(
        f"{star}{bonus}Матч #{mid}: {row['title']}\nСтатус: {row['status']}\n\nВыбирай действие:",
        reply_markup=match_menu_kb(mid, is_admin(message.from_user.id))
    )

@dp.callback_query(F.data.startswith("match:"))
async def match_menu(call: CallbackQuery):
    upsert_user(call)

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
            f"Матч #{mid}: {row['title']}\n\nВыбери тип прогноза:",
            reply_markup=bet_type_kb(mid, mode="vote")
        )
        await call.answer()
        return

    if action == "stats":
        text = format_match_stats(mid, row["title"])
        await call.message.edit_text(text, parse_mode="Markdown", reply_markup=match_menu_kb(mid, is_admin(call.from_user.id)))
        await call.answer()
        return

@dp.callback_query(F.data.startswith("type:vote:"))
async def choose_type_vote(call: CallbackQuery):
    upsert_user(call)
    _, _, mid, bt = call.data.split(":")
    mid = int(mid)
    m = get_match(mid)
    if not m or m["status"] != "open":
        await call.answer("Матч закрыт/не найден.", show_alert=True)
        return

    if bt == "1x2":
        await call.message.edit_text("Выбери исход 1X2:", reply_markup=kb_1x2(mid, "vote"))
    elif bt == "score":
        await call.message.edit_text("Выбери точный счёт:", reply_markup=kb_score(mid, "vote"))
    elif bt == "total":
        await call.message.edit_text("Выбери тотал:", reply_markup=kb_total(mid, "vote"))
    elif bt == "btts":
        await call.message.edit_text("Обе забьют?", reply_markup=kb_btts(mid, "vote"))
    await call.answer()

@dp.callback_query(F.data.startswith("vote:"))
async def vote_cb(call: CallbackQuery):
    upsert_user(call)
    _, mid, bt, choice = call.data.split(":", 3)
    mid = int(mid)

    m = get_match(mid)
    if not m or m["status"] != "open":
        await call.answer("Матч закрыт.", show_alert=True)
        return

    season = current_season()
    ensure_score_row(season, call.from_user.id, call.from_user.username)
    ensure_daily_tasks(call.from_user.id)

    with db() as con:
        con.execute("""
            INSERT OR REPLACE INTO votes(match_id,user_id,username,bet_type,choice,created_at)
            VALUES(?,?,?,?,?,?)
        """, (mid, call.from_user.id, call.from_user.username, bt, choice, now_iso()))
        con.commit()

    inc_task_progress(call.from_user.id, "pred_3", 1)

    duel_ids = find_active_duels_for_vote(call.from_user.id, mid, bt)
    for duel_id in duel_ids:
        save_duel_prediction(duel_id, call.from_user.id, choice)

    await call.answer(f"Сохранено: {BET_LABEL.get(bt, bt)} ✅", show_alert=True)
    await check_and_notify_achievements(call.bot, season, call.from_user.id)

@dp.message(F.text == BTN_MY)
async def my_votes(message: Message):
    upsert_user(message)
    set_state(message.from_user.id, None)

    with db() as con:
        rows = con.execute("""
            SELECT v.match_id, m.title, v.bet_type, v.choice, m.status, v.created_at,
                   m.is_featured, m.bonus_multiplier
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
        star = "⭐ " if int(r["is_featured"]) == 1 else ""
        mult = float(r["bonus_multiplier"]) if r["bonus_multiplier"] is not None else 1.0
        bonus = f"(x{mult:g}) " if int(r["is_featured"]) == 1 and mult != 1.0 else ""
        text += (
            f"\n• #{r['match_id']} {star}{bonus}{r['title']} ({r['status']})\n"
            f"  {BET_LABEL.get(r['bet_type'], r['bet_type'])} → {choice_label(r['bet_type'], r['choice'])}"
        )
    await message.answer(text, reply_markup=main_menu_kb(is_admin(message.from_user.id)))

@dp.message(F.text == BTN_LB)
async def leaderboard(message: Message):
    upsert_user(message)
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
    upsert_user(message)
    set_state(message.from_user.id, None)
    await send_profile(message, message.from_user.id)

async def send_profile(message_or_call, target_user_id: int):
    season = current_season()
    urow, srow = get_profile(season, target_user_id)
    if not urow:
        if isinstance(message_or_call, Message):
            await message_or_call.answer("Профиль не найден.")
        else:
            await message_or_call.answer("Профиль не найден.", show_alert=True)
        return

    viewer_id = message_or_call.from_user.id
    text = profile_text(season, urow, srow)
    kb = profile_kb(viewer_id, target_user_id)

    if isinstance(message_or_call, Message):
        await message_or_call.answer(text, disable_web_page_preview=True)
        await message_or_call.answer("Действия:", reply_markup=kb)
        t_kb = tasks_kb(target_user_id) if target_user_id == viewer_id else None
        if t_kb:
            await message_or_call.answer("🎁 Награды за задания:", reply_markup=t_kb)
    else:
        await message_or_call.message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)

@dp.callback_query(F.data.startswith("follow:"))
async def follow_cb(call: CallbackQuery):
    upsert_user(call)
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
            await safe_dm(call.bot, target_id, "⭐ У тебя новый подписчик!")
    else:
        unfollow_user(call.from_user.id, target_id)
        await call.answer("Отписался ✅", show_alert=True)

    await send_profile(call, target_id)

@dp.callback_query(F.data.startswith("share:"))
async def share_profile(call: CallbackQuery):
    upsert_user(call)
    _, target_id_str = call.data.split(":")
    target_id = int(target_id_str)
    if not BOT_USERNAME:
        await call.answer("Не могу получить username бота.", show_alert=True)
        return
    link = f"https://t.me/{BOT_USERNAME}?start=profile_{target_id}"
    await call.answer("Ссылка готова ✅", show_alert=True)
    await call.message.reply(f"🔗 Ссылка на профиль:\n{link}")

@dp.message(F.text == BTN_FIND)
async def find_player_start(message: Message):
    upsert_user(message)
    set_state(message.from_user.id, "await_find_username")
    await message.answer("🔎 Напиши @username игрока (он должен хотя бы раз запустить бота).",
                         reply_markup=main_menu_kb(is_admin(message.from_user.id)))

@dp.message(Command("find"))
async def find_player_cmd(message: Message):
    upsert_user(message)
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

@dp.message(lambda m: get_state(m.from_user.id) == "await_find_username")
async def find_player_input(message: Message):
    upsert_user(message)
    username = (message.text or "").strip()
    uid = find_user_by_username(username)
    set_state(message.from_user.id, None)
    if not uid:
        await message.answer("Не нашёл игрока. Проверь @username или пусть он запустит бота.")
        return
    await send_profile(message, uid)

@dp.message(F.text == BTN_HELP)
async def help_menu(message: Message):
    upsert_user(message)
    set_state(message.from_user.id, None)
    await message.answer(
        "ℹ️ Помощь\n\n"
        "• «⚽ Активные матчи» → выбери матч → прогноз/статистика\n"
        "• «📊 Мои прогнозы» — все твои ставки\n"
        "• «🏆 Лидерборд» — топ сезона\n"
        "• «👤 Профиль» — профиль + задания + дуэли\n"
        "• «🔎 Найти игрока» — поиск по @username\n\n"
        "Админ:\n"
        "/newmatch <название>\n"
        "/newfeatured <множитель> <название>\n"
        "/sync_today (ручной)\n"
        "/whoami (диагностика)\n",
        reply_markup=main_menu_kb(is_admin(message.from_user.id))
    )

@dp.callback_query(F.data.startswith("task:claim:"))
async def task_claim(call: CallbackQuery):
    upsert_user(call)
    season = current_season()
    key = call.data.split(":")[2]
    ensure_daily_tasks(call.from_user.id)
    ok, msg = claim_task_reward(season, call.from_user.id, call.from_user.username, key)
    await call.answer(msg, show_alert=True)
    await notify_rank_change(call.bot, season, call.from_user.id)
    await send_profile(call.message, call.from_user.id)

@dp.message(F.text == BTN_NEW)
async def newmatch_hint(message: Message):
    upsert_user(message)
    if not is_admin(message.from_user.id):
        return
    await message.answer(
        "Создай матч:\n"
        "/newmatch Real vs Barca (20:00)\n\n"
        "Матч дня:\n"
        "/newfeatured 2 Real vs Barca\n\n"
        "Авто-матчи на сегодня:\n"
        "/sync_today",
        reply_markup=main_menu_kb(True)
    )

@dp.message(Command("newmatch"))
async def newmatch_cmd(message: Message):
    upsert_user(message)
    if not is_admin(message.from_user.id):
        return
    title = (message.text or "").replace("/newmatch", "", 1).strip()
    if not title:
        await message.answer("Формат: /newmatch <название>")
        return
    mid = create_match(title)
    await message.answer(f"Матч создан ✅ #{mid}")

@dp.message(Command("newfeatured"))
async def newfeatured_cmd(message: Message):
    upsert_user(message)
    if not is_admin(message.from_user.id):
        return
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 3:
        await message.answer("Формат: /newfeatured <множитель> <название>")
        return
    try:
        mult = float(parts[1])
    except Exception:
        await message.answer("Множитель должен быть числом")
        return
    title = parts[2].strip()
    mid = create_match(title, featured=1, mult=mult)
    await message.answer(f"⭐ Матч дня создан ✅ #{mid} (x{mult:g})")

@dp.message(Command("sync_today"))
async def sync_today_cmd(message: Message):
    upsert_user(message)
    if not is_admin(message.from_user.id):
        return
    await sync_today_internal(message.bot, requested_by="manual")

@dp.callback_query(F.data.startswith("pf:"))
async def pending_fixture_actions(call: CallbackQuery):
    upsert_user(call)
    if not is_admin(call.from_user.id):
        await call.answer("Только админ", show_alert=True)
        return

    parts = call.data.split(":")
    action = parts[1]
    pid = int(parts[2])
    mult = float(parts[3]) if len(parts) > 3 else 2.0

    p = pending_get(pid)
    if not p:
        await call.answer("Уже обработано.", show_alert=True)
        return

    if action == "skip":
        pending_delete(pid)
        await call.message.edit_text(call.message.text + "\n\n❌ Пропущено")
        await call.answer()
        return

    if match_exists_by_ext(p["ext_id"]):
        pending_delete(pid)
        await call.message.edit_text(call.message.text + "\n\n⚠️ Уже добавлено ранее (дубликат).")
        await call.answer()
        return

    if action == "add":
        create_match(p["title"], featured=0, mult=1.0, external_id=p["ext_id"])
        pending_delete(pid)
        await call.message.edit_text(call.message.text + "\n\n✅ Добавлено в активные матчи")
        await call.answer()
        return

    if action == "feat":
        create_match(p["title"], featured=1, mult=mult, external_id=p["ext_id"])
        pending_delete(pid)
        await call.message.edit_text(call.message.text + f"\n\n⭐ Добавлено как Матч дня (x{mult:g})")
        await call.answer()
        return

@dp.callback_query(F.data.startswith("admin:"))
async def admin_actions(call: CallbackQuery):
    upsert_user(call)
    if not is_admin(call.from_user.id):
        await call.answer("Только админ.", show_alert=True)
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
            f"Матч #{mid}: {row['title']}\n\nВыбери тип результата:",
            reply_markup=bet_type_kb(mid, mode="setres")
        )
        await call.answer()
        return

@dp.callback_query(F.data.startswith("type:setres:"))
async def choose_type_setres(call: CallbackQuery):
    upsert_user(call)
    if not is_admin(call.from_user.id):
        await call.answer("Только админ.", show_alert=True)
        return

    _, _, mid, bt = call.data.split(":")
    mid = int(mid)

    if bt == "1x2":
        await call.message.edit_text("Поставь результат 1X2:", reply_markup=kb_1x2(mid, "setr"))
    elif bt == "score":
        await call.message.edit_text("Поставь точный счёт:", reply_markup=kb_score(mid, "setr"))
    elif bt == "total":
        await call.message.edit_text("Поставь тотал:", reply_markup=kb_total(mid, "setr"))
    elif bt == "btts":
        await call.message.edit_text("Обе забьют?:", reply_markup=kb_btts(mid, "setr"))
    await call.answer()

@dp.callback_query(F.data.startswith("setr:"))
async def set_result_cb(call: CallbackQuery):
    upsert_user(call)
    if not is_admin(call.from_user.id):
        await call.answer("Только админ.", show_alert=True)
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

    status, winners_count, points_per_win = set_result_and_score(mid, bt, result)
    if status == "already":
        await call.answer("Результат уже выставлен.", show_alert=True)
        return

    season = current_season()

    with db() as con:
        voters = con.execute("""
            SELECT user_id, COALESCE(username,'') as username, choice
            FROM votes
            WHERE match_id=? AND bet_type=?
        """, (mid, bt)).fetchall()

    mult = get_match_multiplier(mid)
    mult_line = f"⭐ Матч дня! Множитель: x{mult:g}\n" if mult != 1.0 else ""

    for v in voters:
        uid = int(v["user_id"])
        ch = v["choice"]
        correct = (ch == result)

        crowd_line = ""
        if total_this_type > 0:
            ordered = sorted(dist.items(), key=lambda x: (-x[1], x[0]))[:3]
            parts = []
            for opt, cnt in ordered:
                pct = cnt / total_this_type * 100
                parts.append(f"{choice_label(bt, opt)} {pct:.0f}%")
            crowd_line = "👥 Толпа: " + " • ".join(parts)

        with db() as con2:
            srow = con2.execute("""
                SELECT points, streak FROM scores_season
                WHERE season=? AND user_id=?
            """, (season, uid)).fetchone()
        pts = int(srow["points"]) if srow else 0
        streak = int(srow["streak"]) if srow else 0

        text = (
            f"⚽ Результат: {m['title']}\n"
            f"{mult_line}"
            f"Тип: {BET_LABEL.get(bt, bt)}\n\n"
            f"✅ Правильно: {choice_label(bt, result)}\n"
            f"Твой прогноз: {choice_label(bt, ch)}\n"
            f"{'🎉 Угадал! +' + str(points_per_win) if correct else '❌ Не угадал. +0'}\n\n"
            f"🏆 Очки сезона {season}: {pts}\n"
            f"🔥 Серия: {streak}"
        )
        if crowd_line:
            text += "\n\n" + crowd_line

        await safe_dm(call.bot, uid, text)
        await check_and_notify_achievements(call.bot, season, uid)
        await notify_rank_change(call.bot, season, uid)

    settle_duels_for_result(call.bot, mid, bt, result)

    await call.answer("Готово ✅", show_alert=True)
    await call.message.edit_text(
        f"✅ Результат выставлен\nМатч #{mid}: {m['title']}\n"
        f"Тип: {BET_LABEL.get(bt, bt)}\n"
        f"Результат: {choice_label(bt, result)}\n\n"
        f"Очки начислены. Победителей: {winners_count}",
        reply_markup=match_menu_kb(mid, True)
    )


# ===================== Duels UI (unchanged) =====================
def duel_pick_match_kb(target_id: int, matches):
    kb = InlineKeyboardBuilder()
    for r in matches:
        star = "⭐ " if int(r["is_featured"]) == 1 else ""
        mult = float(r["bonus_multiplier"]) if r["bonus_multiplier"] is not None else 1.0
        bonus = f"(x{mult:g}) " if int(r["is_featured"]) == 1 and mult != 1.0 else ""
        kb.button(text=f"🏟 #{r['id']} {star}{bonus}{r['title']}",
                  callback_data=f"duel:pickmatch:{target_id}:{r['id']}")
    kb.adjust(1)
    return kb.as_markup()

def duel_pick_type_kb(target_id: int, mid: int):
    kb = InlineKeyboardBuilder()
    kb.button(text="1️⃣ 1X2", callback_data=f"duel:picktype:{target_id}:{mid}:1x2")
    kb.button(text="🎯 Точный счёт", callback_data=f"duel:picktype:{target_id}:{mid}:score")
    kb.button(text="⚽ Тотал", callback_data=f"duel:picktype:{target_id}:{mid}:total")
    kb.button(text="🔥 Обе забьют", callback_data=f"duel:picktype:{target_id}:{mid}:btts")
    kb.adjust(2)
    return kb.as_markup()

def duel_invite_kb(duel_id: int):
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Принять", callback_data=f"duel:accept:{duel_id}")
    kb.button(text="❌ Отклонить", callback_data=f"duel:decline:{duel_id}")
    kb.adjust(2)
    return kb.as_markup()

@dp.callback_query(F.data.startswith("duel:start:"))
async def duel_start(call: CallbackQuery):
    upsert_user(call)
    target_id = int(call.data.split(":")[2])
    if target_id == call.from_user.id:
        await call.answer("Нельзя вызвать себя 🙂", show_alert=True)
        return
    matches = get_open_matches()
    if not matches:
        await call.answer("Сейчас нет активных матчей", show_alert=True)
        return
    await call.message.edit_text("⚔️ Выбери матч для дуэли:", reply_markup=duel_pick_match_kb(target_id, matches))
    await call.answer()

@dp.callback_query(F.data.startswith("duel:pickmatch:"))
async def duel_pickmatch(call: CallbackQuery):
    upsert_user(call)
    _, _, target_id, mid = call.data.split(":")
    target_id = int(target_id)
    mid = int(mid)

    m = get_match(mid)
    if not m or m["status"] != "open":
        await call.answer("Матч недоступен", show_alert=True)
        return

    await call.message.edit_text("⚔️ Выбери тип дуэли:", reply_markup=duel_pick_type_kb(target_id, mid))
    await call.answer()

@dp.callback_query(F.data.startswith("duel:picktype:"))
async def duel_picktype(call: CallbackQuery):
    upsert_user(call)
    _, _, target_id, mid, bt = call.data.split(":")
    target_id = int(target_id)
    mid = int(mid)

    if target_id == call.from_user.id:
        await call.answer("Нельзя вызвать себя 🙂", show_alert=True)
        return

    m = get_match(mid)
    if not m or m["status"] != "open":
        await call.answer("Матч недоступен", show_alert=True)
        return

    season = current_season()
    duel_id = create_duel(season, call.from_user.id, target_id, mid, bt)

    try:
        title = m["title"]
        from_txt = f"@{call.from_user.username}" if call.from_user.username else f"id:{call.from_user.id}"
        await call.bot.send_message(
            target_id,
            f"⚔️ Тебя вызывают на дуэль!\nОт: {from_txt}\nМатч: #{mid} {title}\nТип: {BET_LABEL.get(bt, bt)}\n\nПринять?",
            reply_markup=duel_invite_kb(duel_id)
        )
    except Exception:
        set_duel_status(duel_id, "cancelled")
        await call.answer("Не смог отправить приглашение (у соперника закрыты ЛС).", show_alert=True)
        return

    await call.message.edit_text(
        "✅ Приглашение отправлено сопернику в ЛС.\n\n"
        "После принятия — каждый делает прогноз на этот матч по выбранному типу.\n"
        "После выставления результата бот сам подведёт итог ⚔️"
    )
    await call.answer()

@dp.callback_query(F.data.startswith("duel:accept:"))
async def duel_accept(call: CallbackQuery):
    upsert_user(call)
    duel_id = int(call.data.split(":")[2])
    d = get_duel(duel_id)
    if not d:
        await call.answer("Дуэль не найдена", show_alert=True)
        return
    if int(d["opponent_id"]) != call.from_user.id:
        await call.answer("Это не твоя дуэль", show_alert=True)
        return
    if d["status"] != "pending":
        await call.answer("Дуэль уже обработана", show_alert=True)
        return

    set_duel_status(duel_id, "accepted")
    await call.answer("Принято ✅", show_alert=True)

    try:
        await call.bot.send_message(int(d["challenger_id"]), "✅ Твою дуэль приняли! Делай прогноз на матч 👍")
    except Exception:
        pass

    await call.message.edit_text(
        "✅ Дуэль принята!\n\n"
        "Теперь сделай прогноз на этот матч по указанному типу.\n"
        "Открой: «⚽ Активные матчи» → матч → Сделать прогноз."
    )

@dp.callback_query(F.data.startswith("duel:decline:"))
async def duel_decline(call: CallbackQuery):
    upsert_user(call)
    duel_id = int(call.data.split(":")[2])
    d = get_duel(duel_id)
    if not d:
        await call.answer("Дуэль не найдена", show_alert=True)
        return
    if int(d["opponent_id"]) != call.from_user.id:
        await call.answer("Это не твоя дуэль", show_alert=True)
        return
    if d["status"] != "pending":
        await call.answer("Дуэль уже обработана", show_alert=True)
        return

    set_duel_status(duel_id, "declined")
    await call.answer("Отклонено ❌", show_alert=True)

    try:
        await call.bot.send_message(int(d["challenger_id"]), "❌ Твою дуэль отклонили.")
    except Exception:
        pass

    await call.message.edit_text("Ок, отклонил дуэль ❌")


# ===================== Auto sync loop (London-aware) =====================
def next_run_utc_from_local(tz_name: str, hour_local: int) -> datetime:
    """
    Calculates the next run time in UTC, based on local time zone and local hour.
    Supports DST correctly (e.g., Europe/London).
    """
    tz = ZoneInfo(tz_name)
    now_local = datetime.now(tz)

    # next local time at HH:00
    target_local = now_local.replace(hour=hour_local, minute=0, second=0, microsecond=0)
    if target_local <= now_local:
        target_local += timedelta(days=1)

    # convert to UTC
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
            # Cron trigger has priority
            if CRON_REQUESTED:
                CRON_REQUESTED = False
                await sync_today_internal(bot, requested_by="cron")
                await asyncio.sleep(5)
                continue

            if not AUTO_SYNC_ENABLED:
                await asyncio.sleep(30)
                continue

            # schedule: prefer local tz if provided
            if AUTO_SYNC_TZ:
                run_at = next_run_utc_from_local(AUTO_SYNC_TZ, AUTO_SYNC_HOUR_LOCAL)
            else:
                run_at = next_run_utc(AUTO_SYNC_HOUR_UTC)

            sleep_s = (run_at - datetime.now(timezone.utc)).total_seconds()

            # sleep in chunks so cron triggers still work
            while sleep_s > 0:
                if CRON_REQUESTED:
                    break
                step = min(60, sleep_s)
                await asyncio.sleep(step)
                sleep_s -= step

            if CRON_REQUESTED:
                continue

            await sync_today_internal(bot, requested_by="auto")
        except Exception:
            logging.exception("auto_sync_loop crashed")
            await asyncio.sleep(10)


# ===================== MAIN =====================
async def main():
    logging.basicConfig(level=logging.INFO)

    if not TOKEN:
        raise RuntimeError("BOT_TOKEN not set")
    if ADMIN_ID == 0:
        logging.warning("ADMIN_ID is not set! Admin features will not work.")

    init_db()

    bot = Bot(TOKEN)
    global BOT_USERNAME
    me = await bot.get_me()
    BOT_USERNAME = me.username

    asyncio.create_task(auto_sync_loop(bot))

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
