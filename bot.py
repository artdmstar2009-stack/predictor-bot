import asyncio
import logging
import os
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, date
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Optional, Dict, Any, List, Tuple

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage

# ================== НАСТРОЙКИ ==================
TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
DB_PATH = os.getenv("DB_PATH", "predictor.db")
# ===============================================

# ------------------ Render dummy HTTP server ------------------
class DummyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running")

def run_http_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), DummyHandler)
    server.serve_forever()

threading.Thread(target=run_http_server, daemon=True).start()
# -------------------------------------------------------------

# ------------------ Dispatcher ------------------
dp = Dispatcher(storage=MemoryStorage())

# ------------------ FSM ------------------
class FindPlayer(StatesGroup):
    waiting_username = State()

class ExactScoreFSM(StatesGroup):
    waiting_score = State()

# ------------------ Helpers ------------------
def utcnow_iso() -> str:
    return datetime.utcnow().isoformat()

def today_utc_str() -> str:
    return date.today().isoformat()

def is_admin(user_id: int) -> bool:
    return ADMIN_ID != 0 and user_id == ADMIN_ID

USERNAME_RE = re.compile(r"^@?([a-zA-Z0-9_]{5,32})$")

def normalize_username(text: str) -> Optional[str]:
    text = (text or "").strip()
    m = USERNAME_RE.match(text)
    if not m:
        return None
    return m.group(1)

SCORE_RE = re.compile(r"^\s*(\d{1,2})\s*[-:]\s*(\d{1,2})\s*$")

def parse_score(text: str) -> Optional[Tuple[int, int]]:
    m = SCORE_RE.match(text or "")
    if not m:
        return None
    a, b = int(m.group(1)), int(m.group(2))
    if a < 0 or b < 0 or a > 20 or b > 20:
        return None
    return a, b

def outcome_from_score(home_goals: int, away_goals: int) -> str:
    if home_goals > away_goals:
        return "home"
    if home_goals < away_goals:
        return "away"
    return "draw"

def choice_label_1x2(choice: str) -> str:
    return {"home": "Победа хозяев", "draw": "Ничья", "away": "Победа гостей"}[choice]

def btts_from_score(h: int, a: int) -> str:
    return "yes" if (h > 0 and a > 0) else "no"

# ------------------ DB ------------------
def db():
    return sqlite3.connect(DB_PATH)

def init_db():
    with db() as con:
        con.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            joined_at TEXT NOT NULL
        )
        """)
        con.execute("""
        CREATE TABLE IF NOT EXISTS matches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            created_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',   -- open|closed|scored
            result_outcome TEXT,                   -- home/draw/away
            result_score TEXT,                     -- "2-1" optional
            is_featured INTEGER NOT NULL DEFAULT 0,
            bonus_multiplier REAL NOT NULL DEFAULT 1.0
        )
        """)
        con.execute("""
        CREATE TABLE IF NOT EXISTS predictions (
            match_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            username TEXT,
            bet_type TEXT NOT NULL,        -- 1x2|score|total|btts
            prediction TEXT NOT NULL,      -- see formats below
            created_at TEXT NOT NULL,
            PRIMARY KEY(match_id, user_id, bet_type)
        )
        """)
        con.execute("""
        CREATE TABLE IF NOT EXISTS scores (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            points INTEGER NOT NULL DEFAULT 0,
            last_update TEXT
        )
        """)
        con.execute("""
        CREATE TABLE IF NOT EXISTS follows (
            follower_id INTEGER NOT NULL,
            target_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(follower_id, target_id)
        )
        """)
        con.execute("""
        CREATE TABLE IF NOT EXISTS achievements (
            user_id INTEGER NOT NULL,
            code TEXT NOT NULL,
            achieved_at TEXT NOT NULL,
            PRIMARY KEY(user_id, code)
        )
        """)
        con.execute("""
        CREATE TABLE IF NOT EXISTS streaks (
            user_id INTEGER PRIMARY KEY,
            current_streak INTEGER NOT NULL DEFAULT 0,
            best_streak INTEGER NOT NULL DEFAULT 0,
            last_active_date TEXT
        )
        """)
        con.execute("""
        CREATE TABLE IF NOT EXISTS daily_quests (
            user_id INTEGER NOT NULL,
            day TEXT NOT NULL,
            predictions_made INTEGER NOT NULL DEFAULT 0,
            claimed INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(user_id, day)
        )
        """)

        # Duels
        con.execute("""
        CREATE TABLE IF NOT EXISTS duels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            challenger_id INTEGER NOT NULL,
            opponent_id INTEGER NOT NULL,
            match_id INTEGER NOT NULL,
            bet_type TEXT NOT NULL,                 -- 1x2|score|total|btts
            status TEXT NOT NULL DEFAULT 'pending', -- pending|accepted|declined|completed|cancelled
            accepted_at TEXT,
            completed_at TEXT,
            winner_id INTEGER,                      -- NULL = draw
            notes TEXT
        )
        """)
        con.execute("""
        CREATE TABLE IF NOT EXISTS duel_predictions (
            duel_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            prediction TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(duel_id, user_id)
        )
        """)
        con.execute("""
        CREATE TABLE IF NOT EXISTS duel_stats (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            wins INTEGER NOT NULL DEFAULT 0,
            losses INTEGER NOT NULL DEFAULT 0,
            draws INTEGER NOT NULL DEFAULT 0,
            last_update TEXT
        )
        """)
        con.commit()

def upsert_user(user_id: int, username: Optional[str]):
    with db() as con:
        con.execute("""
            INSERT INTO users(user_id, username, joined_at)
            VALUES(?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET username=excluded.username
        """, (user_id, username, utcnow_iso()))
        con.commit()

def create_match(title: str, featured: bool = False, bonus_multiplier: float = 1.0) -> int:
    with db() as con:
        cur = con.execute("""
            INSERT INTO matches(title, created_at, status, is_featured, bonus_multiplier)
            VALUES(?,?, 'open', ?, ?)
        """, (title, utcnow_iso(), 1 if featured else 0, float(bonus_multiplier)))
        con.commit()
        return cur.lastrowid

def list_open_matches() -> List[Tuple[int, str, int, float]]:
    with db() as con:
        cur = con.execute("""
            SELECT id, title, is_featured, bonus_multiplier
            FROM matches
            WHERE status='open'
            ORDER BY id DESC
        """)
        return cur.fetchall()

def get_match(match_id: int) -> Optional[Tuple[int, str, str, Optional[str], Optional[str], int, float]]:
    with db() as con:
        cur = con.execute("""
            SELECT id, title, status, result_outcome, result_score, is_featured, bonus_multiplier
            FROM matches WHERE id=?
        """, (match_id,))
        return cur.fetchone()

def set_prediction(match_id: int, user_id: int, username: Optional[str], bet_type: str, prediction: str):
    with db() as con:
        con.execute("""
            INSERT INTO predictions(match_id, user_id, username, bet_type, prediction, created_at)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(match_id, user_id, bet_type) DO UPDATE SET
                prediction=excluded.prediction,
                created_at=excluded.created_at,
                username=excluded.username
        """, (match_id, user_id, username, bet_type, prediction, utcnow_iso()))
        con.commit()

def list_my_predictions(user_id: int, limit: int = 20) -> List[Tuple[int, str, str, str]]:
    with db() as con:
        cur = con.execute("""
            SELECT p.match_id, m.title, p.bet_type, p.prediction
            FROM predictions p
            JOIN matches m ON m.id=p.match_id
            WHERE p.user_id=?
            ORDER BY p.created_at DESC
            LIMIT ?
        """, (user_id, limit))
        return cur.fetchall()

def leaderboard(limit: int = 10) -> List[Tuple[str, int]]:
    with db() as con:
        cur = con.execute("""
            SELECT COALESCE(username, 'unknown') as username, points
            FROM scores
            ORDER BY points DESC
            LIMIT ?
        """, (limit,))
        return cur.fetchall()

def get_user_points(user_id: int) -> int:
    with db() as con:
        row = con.execute("SELECT points FROM scores WHERE user_id=?", (user_id,)).fetchone()
        return int(row[0]) if row else 0

def get_rank(user_id: int) -> Optional[int]:
    with db() as con:
        row = con.execute("SELECT points FROM scores WHERE user_id=?", (user_id,)).fetchone()
        if not row:
            return None
        pts = int(row[0])
        r = con.execute("SELECT 1 + COUNT(*) FROM scores WHERE points > ?", (pts,)).fetchone()
        return int(r[0]) if r else None

def add_points(user_id: int, username: Optional[str], delta: int):
    if delta == 0:
        return
    with db() as con:
        con.execute("""
            INSERT INTO scores(user_id, username, points, last_update)
            VALUES(?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET
                points=points+excluded.points,
                username=excluded.username,
                last_update=excluded.last_update
        """, (user_id, username, int(delta), utcnow_iso()))
        con.commit()

def close_match(match_id: int):
    with db() as con:
        con.execute("UPDATE matches SET status='closed' WHERE id=?", (match_id,))
        con.commit()

def set_result(match_id: int, outcome: str, score_text: Optional[str]):
    with db() as con:
        con.execute("""
            UPDATE matches
            SET result_outcome=?, result_score=?, status='scored'
            WHERE id=?
        """, (outcome, score_text, match_id))
        con.commit()

def match_vote_stats(match_id: int, bet_type: str) -> List[Tuple[str, int]]:
    with db() as con:
        cur = con.execute("""
            SELECT prediction, COUNT(*)
            FROM predictions
            WHERE match_id=? AND bet_type=?
            GROUP BY prediction
            ORDER BY COUNT(*) DESC
        """, (match_id, bet_type))
        return cur.fetchall()

def follow(follower_id: int, target_id: int):
    with db() as con:
        con.execute("""
            INSERT OR IGNORE INTO follows(follower_id, target_id, created_at)
            VALUES(?,?,?)
        """, (follower_id, target_id, utcnow_iso()))
        con.commit()

def unfollow(follower_id: int, target_id: int):
    with db() as con:
        con.execute("DELETE FROM follows WHERE follower_id=? AND target_id=?", (follower_id, target_id))
        con.commit()

def is_following(follower_id: int, target_id: int) -> bool:
    with db() as con:
        row = con.execute("""
            SELECT 1 FROM follows WHERE follower_id=? AND target_id=? LIMIT 1
        """, (follower_id, target_id)).fetchone()
        return bool(row)

def get_user_public_by_username(username: str) -> Optional[Dict[str, Any]]:
    with db() as con:
        row = con.execute("""
            SELECT u.user_id, COALESCE(u.username, '') as username
            FROM users u
            WHERE LOWER(u.username)=LOWER(?)
            LIMIT 1
        """, (username,)).fetchone()
        if not row:
            return None
        user_id, uname = int(row[0]), row[1]
        pts = get_user_points(user_id)
        rk = get_rank(user_id)
        ds = con.execute("""
            SELECT wins, losses, draws FROM duel_stats WHERE user_id=?
        """, (user_id,)).fetchone()
        wins, losses, draws = (ds if ds else (0, 0, 0))
        return {
            "user_id": user_id,
            "username": uname,
            "points": pts,
            "rank": rk,
            "duel_wins": int(wins),
            "duel_losses": int(losses),
            "duel_draws": int(draws),
        }

def get_user_public_by_id(user_id: int) -> Optional[Dict[str, Any]]:
    with db() as con:
        row = con.execute("""
            SELECT u.user_id, COALESCE(u.username, '') as username
            FROM users u
            WHERE u.user_id=?
            LIMIT 1
        """, (user_id,)).fetchone()
        if not row:
            return None
        uid, uname = int(row[0]), row[1]
        pts = get_user_points(uid)
        rk = get_rank(uid)
        ds = con.execute("SELECT wins, losses, draws FROM duel_stats WHERE user_id=?", (uid,)).fetchone()
        wins, losses, draws = (ds if ds else (0, 0, 0))
        return {
            "user_id": uid,
            "username": uname,
            "points": pts,
            "rank": rk,
            "duel_wins": int(wins),
            "duel_losses": int(losses),
            "duel_draws": int(draws),
        }

# ---- Achievements / streak / quests ----
def has_achievement(user_id: int, code: str) -> bool:
    with db() as con:
        row = con.execute("SELECT 1 FROM achievements WHERE user_id=? AND code=? LIMIT 1", (user_id, code)).fetchone()
        return bool(row)

def grant_achievement(user_id: int, code: str):
    with db() as con:
        con.execute("""
            INSERT OR IGNORE INTO achievements(user_id, code, achieved_at)
            VALUES(?,?,?)
        """, (user_id, code, utcnow_iso()))
        con.commit()

def update_streak_and_quest(user_id: int) -> Tuple[Optional[int], bool]:
    """
    returns (current_streak, quest_claimable_now)
    quest: make 3 predictions today -> claimable
    """
    day = today_utc_str()
    with db() as con:
        row = con.execute("SELECT current_streak, best_streak, last_active_date FROM streaks WHERE user_id=?", (user_id,)).fetchone()
        if not row:
            current, best, last_day = 0, 0, None
        else:
            current, best, last_day = int(row[0]), int(row[1]), row[2]

        if last_day == day:
            pass
        else:
            # simple streak: if yesterday then +1 else reset to 1
            # (без заморочек по UTC-вчера — делаем мягко: если last_day == day-1 то продолжаем)
            # Для стабильности: считаем по ISO строкам (date), без timezone.
            try:
                if last_day:
                    last_d = date.fromisoformat(last_day)
                    today_d = date.fromisoformat(day)
                    if (today_d - last_d).days == 1:
                        current += 1
                    else:
                        current = 1
                else:
                    current = 1
            except Exception:
                current = 1

            if current > best:
                best = current

            con.execute("""
                INSERT INTO streaks(user_id, current_streak, best_streak, last_active_date)
                VALUES(?,?,?,?)
                ON CONFLICT(user_id) DO UPDATE SET
                    current_streak=excluded.current_streak,
                    best_streak=excluded.best_streak,
                    last_active_date=excluded.last_active_date
            """, (user_id, current, best, day))

        # quest progress
        q = con.execute("""
            SELECT predictions_made, claimed FROM daily_quests WHERE user_id=? AND day=?
        """, (user_id, day)).fetchone()
        if not q:
            preds, claimed = 0, 0
            con.execute("""
                INSERT INTO daily_quests(user_id, day, predictions_made, claimed)
                VALUES(?,?,0,0)
            """, (user_id, day))
        else:
            preds, claimed = int(q[0]), int(q[1])

        preds += 1
        con.execute("""
            UPDATE daily_quests
            SET predictions_made=?
            WHERE user_id=? AND day=?
        """, (preds, user_id, day))

        con.commit()

        claimable = (preds >= 3 and claimed == 0)
        return current, claimable

def claim_daily_quest(user_id: int) -> bool:
    day = today_utc_str()
    with db() as con:
        row = con.execute("""
            SELECT predictions_made, claimed FROM daily_quests
            WHERE user_id=? AND day=?
        """, (user_id, day)).fetchone()
        if not row:
            return False
        preds, claimed = int(row[0]), int(row[1])
        if preds < 3 or claimed == 1:
            return False
        con.execute("""
            UPDATE daily_quests SET claimed=1
            WHERE user_id=? AND day=?
        """, (user_id, day))
        con.commit()
        return True

# ---- Duels ----
def create_duel(challenger_id: int, opponent_id: int, match_id: int, bet_type: str) -> int:
    with db() as con:
        cur = con.execute("""
            INSERT INTO duels(created_at, challenger_id, opponent_id, match_id, bet_type, status)
            VALUES(?,?,?,?,?, 'pending')
        """, (utcnow_iso(), challenger_id, opponent_id, match_id, bet_type))
        con.commit()
        return cur.lastrowid

def get_duel(duel_id: int) -> Optional[Tuple]:
    with db() as con:
        return con.execute("""
            SELECT id, challenger_id, opponent_id, match_id, bet_type, status
            FROM duels WHERE id=?
        """, (duel_id,)).fetchone()

def set_duel_status(duel_id: int, status: str):
    with db() as con:
        if status == "accepted":
            con.execute("""
                UPDATE duels SET status='accepted', accepted_at=?
                WHERE id=? AND status='pending'
            """, (utcnow_iso(), duel_id))
        else:
            con.execute("""
                UPDATE duels SET status=?
                WHERE id=?
            """, (status, duel_id))
        con.commit()

def save_duel_prediction(duel_id: int, user_id: int, prediction: str):
    with db() as con:
        con.execute("""
            INSERT INTO duel_predictions(duel_id, user_id, prediction, created_at)
            VALUES(?,?,?,?)
            ON CONFLICT(duel_id, user_id) DO UPDATE SET
                prediction=excluded.prediction,
                created_at=excluded.created_at
        """, (duel_id, user_id, prediction, utcnow_iso()))
        con.commit()

def get_accepted_duels_for_match(match_id: int) -> List[Tuple[int, int, int, str]]:
    with db() as con:
        cur = con.execute("""
            SELECT id, challenger_id, opponent_id, bet_type
            FROM duels
            WHERE match_id=? AND status='accepted'
        """, (match_id,))
        return cur.fetchall()

def get_duel_predictions(duel_id: int) -> Dict[int, str]:
    with db() as con:
        cur = con.execute("SELECT user_id, prediction FROM duel_predictions WHERE duel_id=?", (duel_id,))
        return {int(u): p for (u, p) in cur.fetchall()}

def update_duel_stats(winner_id: Optional[int], challenger_id: int, opponent_id: int, challenger_username: Optional[str], opponent_username: Optional[str]):
    with db() as con:
        def upsert(uid: int, uname: Optional[str]):
            con.execute("""
                INSERT OR IGNORE INTO duel_stats(user_id, username, wins, losses, draws, last_update)
                VALUES(?,?,0,0,0,?)
            """, (uid, uname, utcnow_iso()))
            con.execute("""
                UPDATE duel_stats SET username=?, last_update=? WHERE user_id=?
            """, (uname, utcnow_iso(), uid))

        upsert(challenger_id, challenger_username)
        upsert(opponent_id, opponent_username)

        if winner_id is None:
            con.execute("UPDATE duel_stats SET draws=draws+1 WHERE user_id IN (?,?)", (challenger_id, opponent_id))
        else:
            loser_id = opponent_id if winner_id == challenger_id else challenger_id
            con.execute("UPDATE duel_stats SET wins=wins+1 WHERE user_id=?", (winner_id,))
            con.execute("UPDATE duel_stats SET losses=losses+1 WHERE user_id=?", (loser_id,))
        con.commit()

def complete_duel(duel_id: int, winner_id: Optional[int], notes: str):
    with db() as con:
        con.execute("""
            UPDATE duels
            SET status='completed', completed_at=?, winner_id=?, notes=?
            WHERE id=?
        """, (utcnow_iso(), winner_id, notes, duel_id))
        con.commit()

# ------------------ UI keyboards ------------------
def main_menu_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="⚡ Активные матчи", callback_data="menu:matches")
    kb.button(text="🧾 Мои прогнозы", callback_data="menu:my")
    kb.button(text="🏆 Лидерборд", callback_data="menu:lb")
    kb.button(text="👤 Профиль", callback_data="menu:profile")
    kb.button(text="🔎 Найти игрока", callback_data="menu:find")
    kb.button(text="❓ Помощь", callback_data="menu:help")
    kb.adjust(2, 2, 2)
    return kb.as_markup()

def matches_kb(rows: List[Tuple[int, str, int, float]]):
    kb = InlineKeyboardBuilder()
    for mid, title, featured, mult in rows:
        star = "⭐ " if featured else ""
        bonus = f" x{mult:g}" if featured and mult != 1.0 else ""
        kb.button(text=f"{star}{title}{bonus}", callback_data=f"match:open:{mid}")
    kb.button(text="⬅️ В меню", callback_data="menu:home")
    kb.adjust(1)
    return kb.as_markup()

def match_view_kb(match_id: int):
    kb = InlineKeyboardBuilder()
    kb.button(text="📝 Сделать прогноз", callback_data=f"pred:start:{match_id}")
    kb.button(text="📊 Статистика по матчу", callback_data=f"match:stats:{match_id}")
    kb.button(text="⬅️ К матчам", callback_data="menu:matches")
    kb.adjust(1)
    return kb.as_markup()

def pred_type_kb(match_id: int):
    kb = InlineKeyboardBuilder()
    kb.button(text="1X2", callback_data=f"pred:type:{match_id}:1x2")
    kb.button(text="Точный счёт", callback_data=f"pred:type:{match_id}:score")
    kb.button(text="Тотал", callback_data=f"pred:type:{match_id}:total")
    kb.button(text="Обе забьют", callback_data=f"pred:type:{match_id}:btts")
    kb.button(text="⬅️ Назад", callback_data=f"match:open:{match_id}")
    kb.adjust(2, 2, 1)
    return kb.as_markup()

def pred_1x2_kb(match_id: int, context: str):
    # context: "normal" or "duel:<duel_id>"
    kb = InlineKeyboardBuilder()
    kb.button(text="🏠 Победа хозяев", callback_data=f"pred:1x2:{match_id}:home:{context}")
    kb.button(text="🤝 Ничья", callback_data=f"pred:1x2:{match_id}:draw:{context}")
    kb.button(text="🚌 Победа гостей", callback_data=f"pred:1x2:{match_id}:away:{context}")
    kb.button(text="⬅️ Назад", callback_data=f"pred:start:{match_id}")
    kb.adjust(1)
    return kb.as_markup()

def pred_total_kb(match_id: int, context: str):
    kb = InlineKeyboardBuilder()
    # фиксируем простые линии для старта
    for line in (2.5, 3.5):
        kb.button(text=f"⬆️ Больше {line}", callback_data=f"pred:total:{match_id}:over:{line}:{context}")
        kb.button(text=f"⬇️ Меньше {line}", callback_data=f"pred:total:{match_id}:under:{line}:{context}")
    kb.button(text="⬅️ Назад", callback_data=f"pred:start:{match_id}")
    kb.adjust(2, 2, 1)
    return kb.as_markup()

def pred_btts_kb(match_id: int, context: str):
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Да", callback_data=f"pred:btts:{match_id}:yes:{context}")
    kb.button(text="❌ Нет", callback_data=f"pred:btts:{match_id}:no:{context}")
    kb.button(text="⬅️ Назад", callback_data=f"pred:start:{match_id}")
    kb.adjust(2, 1)
    return kb.as_markup()

def public_profile_kb(target_id: int, subscribed: bool):
    kb = InlineKeyboardBuilder()
    if subscribed:
        kb.button(text="➖ Отписаться", callback_data=f"unfollow:{target_id}")
    else:
        kb.button(text="➕ Подписаться", callback_data=f"follow:{target_id}")
    kb.button(text="📤 Поделиться профилем", callback_data=f"share_profile:{target_id}")
    kb.button(text="⚔️ Вызвать на дуэль", callback_data=f"duel:start:{target_id}")
    kb.adjust(1)
    return kb.as_markup()

def duel_pick_match_kb(target_id: int, rows: List[Tuple[int, str, int, float]]):
    kb = InlineKeyboardBuilder()
    for mid, title, featured, mult in rows:
        star = "⭐ " if featured else ""
        bonus = f" x{mult:g}" if featured and mult != 1.0 else ""
        kb.button(text=f"{star}{title}{bonus}", callback_data=f"duel:pickmatch:{target_id}:{mid}")
    kb.button(text="⬅️ Назад", callback_data=f"profile:open:{target_id}")
    kb.adjust(1)
    return kb.as_markup()

def duel_pick_type_kb(target_id: int, match_id: int):
    kb = InlineKeyboardBuilder()
    kb.button(text="1X2", callback_data=f"duel:picktype:{target_id}:{match_id}:1x2")
    kb.button(text="Точный счёт", callback_data=f"duel:picktype:{target_id}:{match_id}:score")
    kb.button(text="Тотал", callback_data=f"duel:picktype:{target_id}:{match_id}:total")
    kb.button(text="Обе забьют", callback_data=f"duel:picktype:{target_id}:{match_id}:btts")
    kb.button(text="⬅️ Назад", callback_data=f"duel:start:{target_id}")
    kb.adjust(2, 2, 1)
    return kb.as_markup()

def duel_invite_kb(duel_id: int):
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Принять", callback_data=f"duel:accept:{duel_id}")
    kb.button(text="❌ Отклонить", callback_data=f"duel:decline:{duel_id}")
    kb.adjust(2)
    return kb.as_markup()

def quest_kb(claimable: bool):
    kb = InlineKeyboardBuilder()
    if claimable:
        kb.button(text="🎁 Забрать награду за ежедневку", callback_data="quest:claim")
    kb.button(text="⬅️ В меню", callback_data="menu:home")
    kb.adjust(1)
    return kb.as_markup()

# ------------------ Core logic: scoring ------------------
@dataclass
class MatchResult:
    outcome: str          # home/draw/away
    score: Optional[str]  # "2-1"
    total_goals: Optional[int]
    btts: Optional[str]   # yes/no

def build_match_result(outcome: str, score_text: Optional[str]) -> MatchResult:
    if score_text:
        parsed = parse_score(score_text)
        if parsed:
            h, a = parsed
            return MatchResult(
                outcome=outcome_from_score(h, a),
                score=f"{h}-{a}",
                total_goals=h + a,
                btts=btts_from_score(h, a)
            )
    return MatchResult(outcome=outcome, score=None, total_goals=None, btts=None)

def calc_points_for_prediction(result: MatchResult, bet_type: str, pred: str) -> int:
    """
    bet_type:
      1x2 -> pred in home/draw/away
      score -> pred "2-1"
      total -> pred "over:2.5" / "under:3.5"
      btts -> pred "yes" / "no"
    """
    if bet_type == "1x2":
        return 1 if pred == result.outcome else 0

    if bet_type == "score":
        return 3 if (result.score and pred == result.score) else 0

    if bet_type == "total":
        if result.total_goals is None:
            return 0
        try:
            side, line = pred.split(":")
            line_f = float(line)
        except Exception:
            return 0
        if side == "over":
            return 1 if result.total_goals > line_f else 0
        if side == "under":
            return 1 if result.total_goals < line_f else 0
        return 0

    if bet_type == "btts":
        return 1 if (result.btts and pred == result.btts) else 0

    return 0

async def notify_rank_change(bot: Bot, user_id: int, old_rank: Optional[int], new_rank: Optional[int]):
    if old_rank is None or new_rank is None:
        return
    if new_rank != old_rank:
        direction = "⬆️" if new_rank < old_rank else "⬇️"
        try:
            await bot.send_message(user_id, f"{direction} Изменение ранга: было #{old_rank}, стало #{new_rank}")
        except Exception:
            pass

async def maybe_grant_achievements(bot: Bot, user_id: int, username: Optional[str], streak: Optional[int]):
    # 1) First prediction
    if not has_achievement(user_id, "first_pred"):
        grant_achievement(user_id, "first_pred")
        try:
            await bot.send_message(user_id, "🏅 Ачивка получена: *Первый прогноз!*", parse_mode="Markdown")
        except Exception:
            pass

    # 2) 7-day streak
    if streak and streak >= 7 and not has_achievement(user_id, "streak_7"):
        grant_achievement(user_id, "streak_7")
        try:
            await bot.send_message(user_id, "🔥 Ачивка: *Стрик 7 дней!*", parse_mode="Markdown")
        except Exception:
            pass

    # 3) 50 points
    pts = get_user_points(user_id)
    if pts >= 50 and not has_achievement(user_id, "points_50"):
        grant_achievement(user_id, "points_50")
        try:
            await bot.send_message(user_id, "🏆 Ачивка: *50 очков в сезоне!*", parse_mode="Markdown")
        except Exception:
            pass

async def resolve_duels_for_match(bot: Bot, match_id: int, result: MatchResult):
    duels = get_accepted_duels_for_match(match_id)
    if not duels:
        return

    for duel_id, challenger_id, opponent_id, bet_type in duels:
        preds = get_duel_predictions(duel_id)
        p1 = preds.get(challenger_id)
        p2 = preds.get(opponent_id)

        # if someone didn't submit => other wins if correct, else draw
        def is_correct(pred: Optional[str]) -> bool:
            return bool(pred) and calc_points_for_prediction(result, bet_type, pred) > 0

        c1 = is_correct(p1)
        c2 = is_correct(p2)

        winner_id = None
        if c1 and not c2:
            winner_id = challenger_id
        elif c2 and not c1:
            winner_id = opponent_id
        else:
            winner_id = None

        notes = f"type={bet_type}; p1={p1}; p2={p2}; res={result.outcome}/{result.score}"
        complete_duel(duel_id, winner_id, notes)

        # fetch usernames for nicer stats
        cdata = get_user_public_by_id(challenger_id)
        odata = get_user_public_by_id(opponent_id)
        cuname = cdata["username"] if cdata else None
        ouname = odata["username"] if odata else None
        update_duel_stats(winner_id, challenger_id, opponent_id, cuname, ouname)

        # notify both
        def duel_text(for_user: int) -> str:
            opp = opponent_id if for_user == challenger_id else challenger_id
            opp_u = (odata["username"] if for_user == challenger_id else (cdata["username"] if cdata else "")) if (cdata or odata) else ""
            if winner_id is None:
                return f"⚔️ Дуэль завершена: ничья.\nМатч #{match_id}\nТвой прогноз: {preds.get(for_user)}"
            if winner_id == for_user:
                return f"⚔️ Дуэль завершена: ты победил ✅\nМатч #{match_id}\nТвой прогноз: {preds.get(for_user)}"
            return f"⚔️ Дуэль завершена: ты проиграл ❌\nМатч #{match_id}\nТвой прогноз: {preds.get(for_user)}"

        for uid in (challenger_id, opponent_id):
            try:
                await bot.send_message(uid, duel_text(uid))
            except Exception:
                pass

async def score_match_and_notify(bot: Bot, match_id: int, result: MatchResult):
    """
    начисляет очки по всем прогнозам, учитывая featured multiplier,
    и отправляет уведомления о ранге/достижениях.
    """
    m = get_match(match_id)
    if not m:
        return

    _, title, status, _, _, is_featured, mult = m
    mult = float(mult) if mult else 1.0
    multiplier = mult if is_featured else 1.0

    # get all predictions for match
    with db() as con:
        rows = con.execute("""
            SELECT user_id, username, bet_type, prediction
            FROM predictions
            WHERE match_id=?
        """, (match_id,)).fetchall()

    # rank snapshot before
    before_ranks = {}
    for uid, _, _, _ in rows:
        uid = int(uid)
        if uid not in before_ranks:
            before_ranks[uid] = get_rank(uid)

    # calculate & add points
    for user_id, username, bet_type, prediction in rows:
        user_id = int(user_id)
        base = calc_points_for_prediction(result, bet_type, prediction)
        gained = int(round(base * multiplier))
        add_points(user_id, username, gained)

    # resolve duels (separate)
    await resolve_duels_for_match(bot, match_id, result)

    # after ranks + notify
    for uid in before_ranks.keys():
        old = before_ranks.get(uid)
        new = get_rank(uid)
        await notify_rank_change(bot, uid, old, new)

    # achievements + streak quest claimable ping (light)
    for uid in before_ranks.keys():
        st_row = None
        with db() as con:
            st_row = con.execute("SELECT current_streak FROM streaks WHERE user_id=?", (uid,)).fetchone()
        current_streak = int(st_row[0]) if st_row else None
        await maybe_grant_achievements(bot, uid, None, current_streak)

# ------------------ Menu handlers ------------------
@dp.message(CommandStart())
async def start(message: Message, state: FSMContext, bot: Bot):
    upsert_user(message.from_user.id, message.from_user.username)
    await state.clear()
    await message.answer(
        "👋 Привет! Я бот-предиктор.\n"
        "Выбирай в меню, делай прогнозы, набирай очки и побеждай в дуэлях ⚔️",
        reply_markup=main_menu_kb()
    )

@dp.callback_query(F.data == "menu:home")
async def menu_home(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text("🏠 Главное меню:", reply_markup=main_menu_kb())
    await call.answer()

@dp.callback_query(F.data == "menu:help")
async def menu_help(call: CallbackQuery):
    text = (
        "❓ Помощь\n\n"
        "• Выбери матч → Сделай прогноз → тип → выбери исход\n"
        "• Очки начисляются после того, как админ выставит результат\n"
        "• Публичные профили можно смотреть через поиск игрока\n"
        "• Дуэли: вызови игрока из его профиля ⚔️\n\n"
        "Админ:\n"
        "/newmatch <название>\n"
        "/newfeatured <множитель> <название>\n"
        "/close <match_id>\n"
        "/setresult <match_id> <home|draw|away> [2-1]\n"
    )
    await call.message.edit_text(text, reply_markup=main_menu_kb())
    await call.answer()

@dp.callback_query(F.data == "menu:matches")
async def menu_matches(call: CallbackQuery):
    rows = list_open_matches()
    if not rows:
        await call.message.edit_text("Активных матчей пока нет.", reply_markup=main_menu_kb())
        await call.answer()
        return
    await call.message.edit_text("⚡ Активные матчи:", reply_markup=matches_kb(rows))
    await call.answer()

@dp.callback_query(F.data == "menu:my")
async def menu_my(call: CallbackQuery):
    rows = list_my_predictions(call.from_user.id)
    if not rows:
        await call.message.edit_text("🧾 У тебя пока нет прогнозов.", reply_markup=main_menu_kb())
        await call.answer()
        return

    lines = ["🧾 Мои прогнозы (последние):\n"]
    for mid, title, bt, pred in rows:
        label = {"1x2": "1X2", "score": "Счёт", "total": "Тотал", "btts": "ОЗ"}[bt]
        lines.append(f"• {title} — {label}: {pred}")
    await call.message.edit_text("\n".join(lines), reply_markup=main_menu_kb())
    await call.answer()

@dp.callback_query(F.data == "menu:lb")
async def menu_lb(call: CallbackQuery):
    rows = leaderboard(10)
    if not rows:
        await call.message.edit_text("🏆 Лидерборд пуст.", reply_markup=main_menu_kb())
        await call.answer()
        return
    text = "🏆 Лидерборд (сезон):\n\n"
    for i, (uname, pts) in enumerate(rows, 1):
        text += f"{i}. @{uname} — {pts}\n"
    await call.message.edit_text(text, reply_markup=main_menu_kb())
    await call.answer()

@dp.callback_query(F.data == "menu:profile")
async def menu_profile(call: CallbackQuery, bot: Bot):
    upsert_user(call.from_user.id, call.from_user.username)
    pts = get_user_points(call.from_user.id)
    rk = get_rank(call.from_user.id)

    with db() as con:
        st = con.execute("SELECT current_streak, best_streak FROM streaks WHERE user_id=?", (call.from_user.id,)).fetchone()
        st_cur, st_best = (int(st[0]), int(st[1])) if st else (0, 0)

        day = today_utc_str()
        q = con.execute("SELECT predictions_made, claimed FROM daily_quests WHERE user_id=? AND day=?", (call.from_user.id, day)).fetchone()
        preds, claimed = (int(q[0]), int(q[1])) if q else (0, 0)

        ds = con.execute("SELECT wins, losses, draws FROM duel_stats WHERE user_id=?", (call.from_user.id,)).fetchone()
        wins, losses, draws = (ds if ds else (0, 0, 0))

    claimable = (preds >= 3 and claimed == 0)

    text = (
        f"👤 Профиль @{call.from_user.username or 'unknown'}\n"
        f"🏆 Очки: {pts}\n"
        f"📌 Ранг: #{rk if rk else '—'}\n\n"
        f"⚔️ Дуэли: {wins}W / {losses}L / {draws}D\n"
        f"🔥 Стрик: {st_cur} (лучший: {st_best})\n"
        f"🎯 Ежедневка: прогнозы сегодня {preds}/3 {'✅' if claimable else ''}\n"
    )
    await call.message.edit_text(text, reply_markup=quest_kb(claimable))
    await call.answer()

@dp.callback_query(F.data == "quest:claim")
async def quest_claim(call: CallbackQuery):
    ok = claim_daily_quest(call.from_user.id)
    if not ok:
        await call.answer("Пока нечего забирать 🙂", show_alert=True)
        return
    # награда: +1 очко (можешь менять)
    old_rank = get_rank(call.from_user.id)
    add_points(call.from_user.id, call.from_user.username, 1)
    new_rank = get_rank(call.from_user.id)
    await call.answer("🎁 Забрал! +1 очко", show_alert=True)
    # обновим профиль
    await menu_profile(call, bot=None)  # bot тут не обязателен
    # rank notify проще тут не спамить, но можно:
    if old_rank and new_rank and old_rank != new_rank:
        # silently ignore, user already on profile
        pass

# ------------------ Match view ------------------
@dp.callback_query(F.data.startswith("match:open:"))
async def match_open(call: CallbackQuery):
    match_id = int(call.data.split(":")[2])
    m = get_match(match_id)
    if not m:
        await call.answer("Матч не найден", show_alert=True)
        return

    mid, title, status, res_out, res_score, featured, mult = m
    star = "⭐ " if featured else ""
    bonus = f" x{mult:g}" if featured and mult != 1.0 else ""

    text = f"{star}Матч #{mid}{bonus}\n{title}\n\nСтатус: {status}"
    await call.message.edit_text(text, reply_markup=match_view_kb(match_id))
    await call.answer()

@dp.callback_query(F.data.startswith("match:stats:"))
async def match_stats(call: CallbackQuery):
    match_id = int(call.data.split(":")[2])
    m = get_match(match_id)
    if not m:
        await call.answer("Матч не найден", show_alert=True)
        return

    mid, title, status, _, _, _, _ = m
    parts = [f"📊 Статистика по матчу #{mid}\n{title}\n"]

    for bt, label in (("1x2", "1X2"), ("score", "Точный счёт"), ("total", "Тотал"), ("btts", "Обе забьют")):
        stats = match_vote_stats(match_id, bt)
        if not stats:
            continue
        parts.append(f"\n— {label}:")
        for pred, cnt in stats[:10]:
            parts.append(f"  • {pred} — {cnt}")

    if len(parts) == 1:
        parts.append("\nПока нет прогнозов.")
    parts.append("\n")
    await call.message.edit_text("\n".join(parts), reply_markup=match_view_kb(match_id))
    await call.answer()

# ------------------ Predictions flow ------------------
@dp.callback_query(F.data.startswith("pred:start:"))
async def pred_start(call: CallbackQuery, state: FSMContext):
    match_id = int(call.data.split(":")[2])
    m = get_match(match_id)
    if not m or m[2] != "open":
        await call.answer("Голосование закрыто", show_alert=True)
        return

    await state.clear()
    await state.update_data(pred_context="normal")
    await call.message.edit_text("Выбери тип прогноза:", reply_markup=pred_type_kb(match_id))
    await call.answer()

@dp.callback_query(F.data.startswith("pred:type:"))
async def pred_type(call: CallbackQuery, state: FSMContext):
    _, _, match_id, bet_type = call.data.split(":")
    match_id = int(match_id)

    data = await state.get_data()
    context = data.get("pred_context", "normal")

    if bet_type == "1x2":
        await call.message.edit_text("1X2 — выбери исход:", reply_markup=pred_1x2_kb(match_id, context))
        await call.answer()
        return

    if bet_type == "total":
        await call.message.edit_text("Тотал — выбери линию:", reply_markup=pred_total_kb(match_id, context))
        await call.answer()
        return

    if bet_type == "btts":
        await call.message.edit_text("Обе забьют — выбери:", reply_markup=pred_btts_kb(match_id, context))
        await call.answer()
        return

    if bet_type == "score":
        # ждём ввод в FSM
        await state.set_state(ExactScoreFSM.waiting_score)
        await state.update_data(score_match_id=match_id)
        await call.message.edit_text(
            "Точный счёт: отправь сообщением формат типа `2-1`.\n"
            "Чтобы отменить — /cancel",
            reply_markup=None
        )
        await call.answer()
        return

    await call.answer("Неизвестный тип", show_alert=True)

@dp.callback_query(F.data.startswith("pred:1x2:"))
async def pred_1x2(call: CallbackQuery, state: FSMContext, bot: Bot):
    # pred:1x2:<match_id>:<choice>:<context>
    _, _, match_id, choice, context = call.data.split(":")
    match_id = int(match_id)

    m = get_match(match_id)
    if not m or m[2] != "open":
        await call.answer("Голосование закрыто", show_alert=True)
        return

    set_prediction(match_id, call.from_user.id, call.from_user.username, "1x2", choice)
    streak, claimable = update_streak_and_quest(call.from_user.id)
    await maybe_grant_achievements(bot, call.from_user.id, call.from_user.username, streak)

    # duel context?
    if context.startswith("duel_"):
        duel_id = int(context.split("_", 1)[1])
        save_duel_prediction(duel_id, call.from_user.id, choice)

    msg = f"✅ Прогноз сохранён: 1X2 — {choice_label_1x2(choice)}"
    if claimable:
        msg += "\n🎯 Ежедневка выполнена: зайди в профиль и забери награду!"
    await call.answer("Сохранено ✅", show_alert=True)
    await call.message.edit_text(msg, reply_markup=match_view_kb(match_id))

@dp.callback_query(F.data.startswith("pred:total:"))
async def pred_total(call: CallbackQuery, state: FSMContext, bot: Bot):
    # pred:total:<match_id>:<side>:<line>:<context>
    _, _, match_id, side, line, context = call.data.split(":")
    match_id = int(match_id)

    m = get_match(match_id)
    if not m or m[2] != "open":
        await call.answer("Голосование закрыто", show_alert=True)
        return

    prediction = f"{side}:{line}"
    set_prediction(match_id, call.from_user.id, call.from_user.username, "total", prediction)
    streak, claimable = update_streak_and_quest(call.from_user.id)
    await maybe_grant_achievements(bot, call.from_user.id, call.from_user.username, streak)

    if context.startswith("duel_"):
        duel_id = int(context.split("_", 1)[1])
        save_duel_prediction(duel_id, call.from_user.id, prediction)

    msg = f"✅ Прогноз сохранён: Тотал — {prediction}"
    if claimable:
        msg += "\n🎯 Ежедневка выполнена: зайди в профиль и забери награду!"
    await call.answer("Сохранено ✅", show_alert=True)
    await call.message.edit_text(msg, reply_markup=match_view_kb(match_id))

@dp.callback_query(F.data.startswith("pred:btts:"))
async def pred_btts(call: CallbackQuery, bot: Bot):
    # pred:btts:<match_id>:<yes|no>:<context>
    _, _, match_id, yn, context = call.data.split(":")
    match_id = int(match_id)

    m = get_match(match_id)
    if not m or m[2] != "open":
        await call.answer("Голосование закрыто", show_alert=True)
        return

    set_prediction(match_id, call.from_user.id, call.from_user.username, "btts", yn)
    streak, claimable = update_streak_and_quest(call.from_user.id)
    await maybe_grant_achievements(bot, call.from_user.id, call.from_user.username, streak)

    if context.startswith("duel_"):
        duel_id = int(context.split("_", 1)[1])
        save_duel_prediction(duel_id, call.from_user.id, yn)

    msg = f"✅ Прогноз сохранён: ОЗ — {yn}"
    if claimable:
        msg += "\n🎯 Ежедневка выполнена: зайди в профиль и забери награду!"
    await call.answer("Сохранено ✅", show_alert=True)
    await call.message.edit_text(msg, reply_markup=match_view_kb(match_id))

@dp.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Ок, отменил ✅", reply_markup=main_menu_kb())

@dp.message(ExactScoreFSM.waiting_score)
async def exact_score_input(message: Message, state: FSMContext, bot: Bot):
    parsed = parse_score(message.text or "")
    if not parsed:
        await message.answer("❌ Неверный формат. Напиши, например: 2-1")
        return

    h, a = parsed
    data = await state.get_data()
    match_id = int(data.get("score_match_id", 0))
    context = data.get("pred_context", "normal")

    m = get_match(match_id)
    if not m or m[2] != "open":
        await message.answer("Голосование закрыто.", reply_markup=main_menu_kb())
        await state.clear()
        return

    pred = f"{h}-{a}"
    set_prediction(match_id, message.from_user.id, message.from_user.username, "score", pred)
    streak, claimable = update_streak_and_quest(message.from_user.id)
    await maybe_grant_achievements(bot, message.from_user.id, message.from_user.username, streak)

    # duel context?
    if isinstance(context, str) and context.startswith("duel_"):
        duel_id = int(context.split("_", 1)[1])
        save_duel_prediction(duel_id, message.from_user.id, pred)

    await state.clear()
    text = f"✅ Прогноз сохранён: Точный счёт — {pred}"
    if claimable:
        text += "\n🎯 Ежедневка выполнена: зайди в профиль и забери награду!"
    await message.answer(text, reply_markup=match_view_kb(match_id))

# ------------------ Find player ------------------
@dp.callback_query(F.data == "menu:find")
async def find_player_start(call: CallbackQuery, state: FSMContext):
    await state.set_state(FindPlayer.waiting_username)
    await call.message.edit_text(
        "🔎 Введи @username игрока (например: @artem) или просто username.\n\n"
        "Отмена: /cancel",
        reply_markup=None
    )
    await call.answer()

@dp.message(FindPlayer.waiting_username)
async def find_player_entered(message: Message, state: FSMContext):
    uname = normalize_username(message.text or "")
    if not uname:
        await message.answer("❌ Неверный формат. Пришли @username или username (5–32 символа).")
        return

    data = get_user_public_by_username(uname)
    if not data:
        await message.answer("Не нашёл такого игрока 😕\nПроверь username и попробуй снова.")
        return

    if data["user_id"] == message.from_user.id:
        await message.answer("Это ты 🙂\nОткрой свой профиль через меню.", reply_markup=main_menu_kb())
        await state.clear()
        return

    subscribed = is_following(message.from_user.id, data["user_id"])
    text = (
        f"👤 Публичный профиль: @{data['username']}\n"
        f"🏆 Очки: {data['points']}\n"
        f"📌 Ранг: #{data['rank'] if data['rank'] else '—'}\n"
        f"⚔️ Дуэли: {data['duel_wins']}W / {data['duel_losses']}L / {data['duel_draws']}D\n"
    )
    await message.answer(text, reply_markup=public_profile_kb(data["user_id"], subscribed))
    await state.clear()

@dp.callback_query(F.data.startswith("profile:open:"))
async def profile_open(call: CallbackQuery):
    target_id = int(call.data.split(":")[2])
    data = get_user_public_by_id(target_id)
    if not data:
        await call.answer("Профиль не найден", show_alert=True)
        return
    subscribed = is_following(call.from_user.id, target_id)
    text = (
        f"👤 Публичный профиль: @{data['username']}\n"
        f"🏆 Очки: {data['points']}\n"
        f"📌 Ранг: #{data['rank'] if data['rank'] else '—'}\n"
        f"⚔️ Дуэли: {data['duel_wins']}W / {data['duel_losses']}L / {data['duel_draws']}D\n"
    )
    await call.message.edit_text(text, reply_markup=public_profile_kb(target_id, subscribed))
    await call.answer()

@dp.callback_query(F.data.startswith("follow:"))
async def follow_cb(call: CallbackQuery):
    target_id = int(call.data.split(":")[1])
    if target_id == call.from_user.id:
        await call.answer("На себя подписаться нельзя 🙂", show_alert=True)
        return
    follow(call.from_user.id, target_id)
    await call.message.edit_reply_markup(reply_markup=public_profile_kb(target_id, True))
    await call.answer("Подписка оформлена ✅", show_alert=True)

@dp.callback_query(F.data.startswith("unfollow:"))
async def unfollow_cb(call: CallbackQuery):
    target_id = int(call.data.split(":")[1])
    unfollow(call.from_user.id, target_id)
    await call.message.edit_reply_markup(reply_markup=public_profile_kb(target_id, False))
    await call.answer("Отписался ✅", show_alert=True)

@dp.callback_query(F.data.startswith("share_profile:"))
async def share_profile_cb(call: CallbackQuery, bot: Bot):
    target_id = int(call.data.split(":")[1])
    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start=prof_{target_id}"
    await call.message.answer("📤 Ссылка для шаринга профиля:\n" + link)
    await call.answer()

# Handle /start prof_ID deep-link
@dp.message(CommandStart(deep_link=True))
async def start_deeplink(message: Message, command: CommandStart, state: FSMContext, bot: Bot):
    # aiogram may route both handlers; keep safe:
    args = (message.text or "").split(maxsplit=1)
    payload = args[1] if len(args) > 1 else ""
    if payload.startswith("prof_"):
        try:
            target_id = int(payload.split("_", 1)[1])
        except Exception:
            await message.answer("Некорректная ссылка профиля.", reply_markup=main_menu_kb())
            return

        upsert_user(message.from_user.id, message.from_user.username)
        data = get_user_public_by_id(target_id)
        if not data:
            await message.answer("Профиль не найден 😕", reply_markup=main_menu_kb())
            return
        if target_id == message.from_user.id:
            await message.answer("Это твой профиль 🙂", reply_markup=main_menu_kb())
            return
        subscribed = is_following(message.from_user.id, target_id)
        text = (
            f"👤 Публичный профиль: @{data['username']}\n"
            f"🏆 Очки: {data['points']}\n"
            f"📌 Ранг: #{data['rank'] if data['rank'] else '—'}\n"
            f"⚔️ Дуэли: {data['duel_wins']}W / {data['duel_losses']}L / {data['duel_draws']}D\n"
        )
        await message.answer(text, reply_markup=public_profile_kb(target_id, subscribed))
        return

    # if no prof payload, fallback to normal start
    await start(message, state, bot)

# ------------------ Duels ------------------
@dp.callback_query(F.data.startswith("duel:start:"))
async def duel_start(call: CallbackQuery):
    target_id = int(call.data.split(":")[2])
    if target_id == call.from_user.id:
        await call.answer("Сам себя вызвать нельзя 🙂", show_alert=True)
        return

    rows = list_open_matches()
    if not rows:
        await call.answer("Сейчас нет активных матчей", show_alert=True)
        return

    await call.message.edit_text(
        "⚔️ Выбери матч для дуэли:",
        reply_markup=duel_pick_match_kb(target_id, rows)
    )
    await call.answer()

@dp.callback_query(F.data.startswith("duel:pickmatch:"))
async def duel_pickmatch(call: CallbackQuery):
    _, _, target_id, match_id = call.data.split(":")
    target_id = int(target_id)
    match_id = int(match_id)

    m = get_match(match_id)
    if not m or m[2] != "open":
        await call.answer("Матч недоступен", show_alert=True)
        return

    await call.message.edit_text(
        "⚔️ Выбери тип дуэли:",
        reply_markup=duel_pick_type_kb(target_id, match_id)
    )
    await call.answer()

@dp.callback_query(F.data.startswith("duel:picktype:"))
async def duel_picktype(call: CallbackQuery, bot: Bot):
    _, _, target_id, match_id, bet_type = call.data.split(":")
    target_id = int(target_id)
    match_id = int(match_id)

    if target_id == call.from_user.id:
        await call.answer("Сам себя вызвать нельзя 🙂", show_alert=True)
        return

    m = get_match(match_id)
    if not m or m[2] != "open":
        await call.answer("Матч недоступен", show_alert=True)
        return

    duel_id = create_duel(call.from_user.id, target_id, match_id, bet_type)

    # invite opponent
    try:
        title = m[1]
        label = {"1x2": "1X2", "score": "Точный счёт", "total": "Тотал", "btts": "Обе забьют"}[bet_type]
        await bot.send_message(
            target_id,
            f"⚔️ Тебя вызывают на дуэль!\n"
            f"От: @{call.from_user.username or call.from_user.id}\n"
            f"Матч: {title}\n"
            f"Тип: {label}\n\n"
            f"Принять дуэль?",
            reply_markup=duel_invite_kb(duel_id)
        )
    except Exception:
        # opponent may have closed DMs
        set_duel_status(duel_id, "cancelled")
        await call.answer("Не смог отправить приглашение (у соперника закрыты ЛС).", show_alert=True)
        return

    await call.message.edit_text(
        "✅ Приглашение отправлено сопернику в ЛС.\n"
        "После принятия дуэли сделай прогноз на этот матч (обычно), и он автоматически засчитается и в дуэль.",
        reply_markup=main_menu_kb()
    )
    await call.answer()

@dp.callback_query(F.data.startswith("duel:accept:"))
async def duel_accept(call: CallbackQuery, state: FSMContext, bot: Bot):
    duel_id = int(call.data.split(":")[2])
    d = get_duel(duel_id)
    if not d:
        await call.answer("Дуэль не найдена", show_alert=True)
        return

    _, challenger_id, opponent_id, match_id, bet_type, status = d
    if call.from_user.id != opponent_id:
        await call.answer("Это не твоя дуэль", show_alert=True)
        return
    if status != "pending":
        await call.answer("Дуэль уже обработана", show_alert=True)
        return

    set_duel_status(duel_id, "accepted")

    m = get_match(match_id)
    title = m[1] if m else f"#{match_id}"
    label = {"1x2": "1X2", "score": "Точный счёт", "total": "Тотал", "btts": "Обе забьют"}[bet_type]

    # notify challenger
    try:
        await bot.send_message(challenger_id, f"✅ Дуэль принята!\nМатч: {title}\nТип: {label}")
    except Exception:
        pass

    # tell opponent to predict
    await call.message.edit_text(
        f"✅ Дуэль принята!\nМатч: {title}\nТип: {label}\n\n"
        "Теперь сделай прогноз на этот матч в боте — он автоматически засчитается в дуэль.\n"
        "Подсказка: открой Активные матчи → выбери матч → Сделать прогноз.",
        reply_markup=main_menu_kb()
    )
    await call.answer("Принято ✅", show_alert=True)

@dp.callback_query(F.data.startswith("duel:decline:"))
async def duel_decline(call: CallbackQuery, bot: Bot):
    duel_id = int(call.data.split(":")[2])
    d = get_duel(duel_id)
    if not d:
        await call.answer("Дуэль не найдена", show_alert=True)
        return

    _, challenger_id, opponent_id, match_id, bet_type, status = d
    if call.from_user.id != opponent_id:
        await call.answer("Это не твоя дуэль", show_alert=True)
        return
    if status != "pending":
        await call.answer("Дуэль уже обработана", show_alert=True)
        return

    set_duel_status(duel_id, "declined")
    try:
        await bot.send_message(challenger_id, "❌ Твой вызов на дуэль отклонён.")
    except Exception:
        pass

    await call.message.edit_text("Ок, отклонил дуэль ❌", reply_markup=main_menu_kb())
    await call.answer()

# ------------------ Admin commands ------------------
@dp.message(Command("newmatch"))
async def cmd_newmatch(message: Message):
    if not is_admin(message.from_user.id):
        return
    title = message.text.replace("/newmatch", "", 1).strip()
    if not title:
        await message.answer("Используй: /newmatch <название>")
        return
    mid = create_match(title, featured=False, bonus_multiplier=1.0)
    await message.answer(f"✅ Создан матч #{mid}")

@dp.message(Command("newfeatured"))
async def cmd_newfeatured(message: Message):
    if not is_admin(message.from_user.id):
        return
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 3:
        await message.answer("Используй: /newfeatured <множитель> <название>")
        return
    try:
        mult = float(parts[1])
    except Exception:
        await message.answer("Множитель должен быть числом, например 2")
        return
    title = parts[2].strip()
    mid = create_match(title, featured=True, bonus_multiplier=mult)
    await message.answer(f"⭐ Создан матч дня #{mid} (x{mult:g})")

@dp.message(Command("close"))
async def cmd_close(message: Message):
    if not is_admin(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer("Используй: /close <match_id>")
        return
    mid = int(parts[1])
    close_match(mid)
    await message.answer("✅ Матч закрыт")

@dp.message(Command("setresult"))
async def cmd_setresult(message: Message, bot: Bot):
    if not is_admin(message.from_user.id):
        return

    parts = (message.text or "").split()
    if len(parts) < 3:
        await message.answer("Используй: /setresult <match_id> <home|draw|away> [2-1]")
        return

    match_id = int(parts[1])
    outcome = parts[2].strip().lower()
    score_text = parts[3] if len(parts) >= 4 else None

    if outcome not in ("home", "draw", "away"):
        await message.answer("Исход должен быть: home/draw/away")
        return

    m = get_match(match_id)
    if not m:
        await message.answer("Матч не найден")
        return

    # build result; if score provided, override outcome based on score (честнее)
    res = build_match_result(outcome, score_text)
    set_result(match_id, res.outcome, res.score)

    # scoring & notifications
    await score_match_and_notify(bot, match_id, res)

    await message.answer(f"✅ Результат сохранён для матча #{match_id}: {res.outcome} {res.score or ''}".strip())

# ------------------ MAIN ------------------
async def main():
    logging.basicConfig(level=logging.INFO)
    if not TOKEN:
        raise RuntimeError("Нет BOT_TOKEN")
    if ADMIN_ID == 0:
        raise RuntimeError("Нет ADMIN_ID")

    init_db()
    bot = Bot(TOKEN)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
