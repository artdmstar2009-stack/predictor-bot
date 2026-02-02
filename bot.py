import asyncio
import logging
import os
import sqlite3
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime
from typing import Optional, Tuple

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

# ===== CONFIG =====
TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
DB_PATH = "predictor.db"
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
    # сезон = месяц в UTC: YYYY-MM
    return datetime.utcnow().strftime("%Y-%m")

def init_db():
    with db() as con:
        # пользователи
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
        # матчи
        con.execute("""
        CREATE TABLE IF NOT EXISTS matches(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open'  -- open/closed
        )
        """)
        # голоса: ключ (match_id,user_id,bet_type), чтобы можно было делать несколько типов прогнозов на один матч
        con.execute("""
        CREATE TABLE IF NOT EXISTS votes(
            match_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            username TEXT,
            bet_type TEXT NOT NULL,  -- 1x2/score/total/btts
            choice TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(match_id, user_id, bet_type)
        )
        """)
        # результаты (для начисления очков)
        con.execute("""
        CREATE TABLE IF NOT EXISTS results(
            match_id INTEGER NOT NULL,
            bet_type TEXT NOT NULL,
            result TEXT NOT NULL,
            scored INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(match_id, bet_type)
        )
        """)
        # очки по сезонам
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
        # подписки
        con.execute("""
        CREATE TABLE IF NOT EXISTS follows(
            follower_id INTEGER NOT NULL,
            followee_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(follower_id, followee_id)
        )
        """)
        # ачивки
        con.execute("""
        CREATE TABLE IF NOT EXISTS user_achievements(
            user_id INTEGER NOT NULL,
            season TEXT NOT NULL,
            key TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(user_id, season, key)
        )
        """)
        con.commit()


# ===================== Core helpers =====================
def is_admin(uid: int) -> bool:
    return uid == ADMIN_ID

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
        row = con.execute("SELECT 1 FROM scores_season WHERE season=? AND user_id=?", (season, user_id)).fetchone()
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
            con.execute("INSERT INTO follows(follower_id, followee_id, created_at) VALUES(?,?,?)",
                        (follower_id, followee_id, now_iso()))
            con.commit()
            return "ok"
        except sqlite3.IntegrityError:
            return "already"

def unfollow_user(follower_id: int, followee_id: int) -> str:
    with db() as con:
        con.execute("DELETE FROM follows WHERE follower_id=? AND followee_id=?",
                    (follower_id, followee_id))
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
        if r["user_id"] == user_id:
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


# ===================== Labels & UI =====================
BTN_ACTIVE = "⚽ Активные матчи"
BTN_MY = "📊 Мои прогнозы"
BTN_LB = "🏆 Лидерборд"
BTN_PROFILE = "👤 Профиль"
BTN_HELP = "ℹ️ Помощь"
BTN_NEW = "➕ Создать матч"
BTN_BACK = "⬅️ Назад"

BET_LABEL = {"1x2": "1X2", "score": "Точный счёт", "total": "Тотал", "btts": "Обе забьют"}

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

def main_menu_kb(user_is_admin: bool):
    kb = ReplyKeyboardBuilder()
    kb.button(text=BTN_ACTIVE)
    kb.button(text=BTN_MY)
    kb.button(text=BTN_LB)
    kb.button(text=BTN_PROFILE)
    kb.button(text=BTN_HELP)
    if user_is_admin:
        kb.button(text=BTN_NEW)
    kb.adjust(2)
    return kb.as_markup(resize_keyboard=True)

def matches_list_kb(matches, user_is_admin: bool):
    kb = ReplyKeyboardBuilder()
    for r in matches:
        kb.button(text=f"🏟 #{r['id']} {r['title']}")
    kb.button(text=BTN_BACK)
    if user_is_admin:
        kb.button(text=BTN_NEW)
    kb.adjust(1)
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

def bet_type_kb(mid: int, mode: str):
    # mode: vote | setres
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
    for s in ["1:0", "2:1", "1:1", "0:0"]:
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
    kb.button(text="🔗 Поделиться профилем", callback_data=f"share:{target_id}")
    kb.adjust(1)
    return kb.as_markup()

# ===================== Parsing helpers =====================
def parse_match_button(text: str) -> Optional[int]:
    text = text.strip()
    if not text.startswith("🏟 #"):
        return None
    try:
        after_hash = text.split("#", 1)[1]
        mid_str = after_hash.split(" ", 1)[0]
        return int(mid_str)
    except Exception:
        return None

def start_payload(message: Message) -> Optional[str]:
    # /start payload
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) == 2:
        return parts[1].strip()
    return None

# ===================== Notifications (Layer 2) =====================
async def safe_dm(bot: Bot, user_id: int, text: str):
    try:
        await bot.send_message(user_id, text)
    except Exception:
        # пользователь мог запретить/не стартовал бота
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
            SELECT points, correct, total, streak
            FROM scores_season
            WHERE season=? AND user_id=?
        """, (season, user_id)).fetchone()

    if not row:
        return

    total = int(row["total"])
    correct = int(row["correct"])
    streak = int(row["streak"])

    # достижения
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
    # уведомляем только если улучшил позицию (или впервые появился)
    if old_rank is None:
        set_last_rank(user_id, new_rank)
        await safe_dm(bot, user_id, f"🏆 Ты появился в рейтинге сезона {season}!\nТекущее место: #{new_rank}")
        return

    if new_rank < old_rank:
        set_last_rank(user_id, new_rank)
        await safe_dm(bot, user_id, f"📈 Ты поднялся в рейтинге сезона {season}!\nТеперь ты #{new_rank} (было #{old_rank}).")
    else:
        set_last_rank(user_id, new_rank)

# ===================== Scoring (Layer 3) =====================
def can_score(mid: int, bet_type: str) -> bool:
    with db() as con:
        r = con.execute("SELECT scored FROM results WHERE match_id=? AND bet_type=?", (mid, bet_type)).fetchone()
        return not (r and int(r["scored"]) == 1)

def set_result_and_score(mid: int, bet_type: str, result: str) -> Tuple[str, int]:
    """
    Ставит результат по типу прогноза и начисляет очки (+1) за верный прогноз.
    Обновляет total/correct/streak в текущем сезоне.
    Возвращает ("ok", winners_count) | ("already",0) | ("not_found",0)
    """
    if not can_score(mid, bet_type):
        return ("already", 0)

    season = current_season()

    with db() as con:
        m = con.execute("SELECT id FROM matches WHERE id=?", (mid,)).fetchone()
        if not m:
            return ("not_found", 0)

        # фиксируем результат
        con.execute("""
            INSERT INTO results(match_id, bet_type, result, scored)
            VALUES(?,?,?,1)
            ON CONFLICT(match_id, bet_type) DO UPDATE SET
              result=excluded.result,
              scored=1
        """, (mid, bet_type, result))

        # все, кто голосовал по этому типу
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

            # total +1 всем участникам по этому bet_type
            con.execute("""
                UPDATE scores_season
                SET total = total + 1,
                    username = COALESCE(?, username)
                WHERE season=? AND user_id=?
            """, (uname, season, uid))

            if choice == result:
                winners_count += 1
                # points +1, correct +1, streak +1, best_streak
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
                # промах: streak сбрасываем
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
    for bt in ["1x2", "score", "total", "btts"]:
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
    # fallback
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

    payload = start_payload(message)
    if payload and payload.startswith("profile_"):
        try:
            uid = int(payload.split("_", 1)[1])
            await send_profile(message, uid)
            return
        except Exception:
            pass

    await message.answer(
        "🤖 Predictor Bot\n\n"
        "• «⚽ Активные матчи» → выбирай матч кнопкой\n"
        "• Делай прогнозы, набирай очки, поднимайся в рейтинге\n"
        "• Следи за профилями и подписывайся ⭐\n\n"
        "Меню снизу 👇",
        reply_markup=main_menu_kb(is_admin(message.from_user.id))
    )

@dp.message(F.text == BTN_BACK)
async def back_to_main(message: Message):
    upsert_user(message.from_user.id, message.from_user.username, message.from_user.first_name, message.from_user.last_name)
    await message.answer("Главное меню 👇", reply_markup=main_menu_kb(is_admin(message.from_user.id)))

@dp.message(F.text == BTN_ACTIVE)
async def active_matches(message: Message):
    upsert_user(message.from_user.id, message.from_user.username, message.from_user.first_name, message.from_user.last_name)
    matches = get_open_matches()
    if not matches:
        await message.answer("Нет активных матчей.", reply_markup=main_menu_kb(is_admin(message.from_user.id)))
        return
    await message.answer("Выбери матч кнопкой ниже 👇", reply_markup=matches_list_kb(matches, is_admin(message.from_user.id)))

@dp.message(F.text.startswith("🏟 #"))
async def picked_match(message: Message):
    upsert_user(message.from_user.id, message.from_user.username, message.from_user.first_name, message.from_user.last_name)

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
    upsert_user(call.from_user.id, call.from_user.username, call.from_user.first_name, call.from_user.last_name)

    _, _, mid, bt = call.data.split(":")
    mid = int(mid)

    m = get_match(mid)
    if not m:
        await call.answer("Матч не найден.", show_alert=True)
        return
    if m["status"] != "open":
        await call.answer("Матч закрыт.", show_alert=True)
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
    upsert_user(call.from_user.id, call.from_user.username, call.from_user.first_name, call.from_user.last_name)

    # vote:{mid}:{bet_type}:{choice}
    _, mid, bt, choice = call.data.split(":", 3)
    mid = int(mid)

    season = current_season()
    ensure_score_row(season, call.from_user.id, call.from_user.username)

    with db() as con:
        st = con.execute("SELECT status, title FROM matches WHERE id=?", (mid,)).fetchone()
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

    # достижение "first_pred" проверим отдельно (Layer 3 + уведомления Layer 2)
    bot = call.bot
    await check_and_notify_achievements(bot, season, call.from_user.id)

@dp.message(F.text == BTN_MY)
async def my_votes(message: Message):
    upsert_user(message.from_user.id, message.from_user.username, message.from_user.first_name, message.from_user.last_name)

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

    viewer_id = message_or_call.from_user.id if hasattr(message_or_call, "from_user") else None
    text = profile_text(season, urow, srow)

    if isinstance(message_or_call, Message):
        await message_or_call.answer(text, reply_markup=main_menu_kb(is_admin(message_or_call.from_user.id)), disable_web_page_preview=True)
        # отдельно кнопки профиля — в инлайне:
        await message_or_call.answer("Действия:", reply_markup=profile_kb(message_or_call.from_user.id, target_user_id))
    else:
        # callback query
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
            # Уведомление цели (Layer 2)
            await safe_dm(call.bot, target_id, f"⭐ У тебя новый подписчик: @{call.from_user.username}" if call.from_user.username else "⭐ У тебя новый подписчик!")
    else:
        unfollow_user(call.from_user.id, target_id)
        await call.answer("Отписался ✅", show_alert=True)

    # обновим карточку профиля
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

    await message.answer(
        "ℹ️ Помощь\n\n"
        "1) «⚽ Активные матчи» → выбери матч (🏟 #...)\n"
        "2) «🗳 Сделать прогноз» → выбери тип → исход кнопками\n"
        "3) «📈 Статистика» — смотри проценты по матчу\n"
        "4) «🏆 Лидерборд» — сезонный топ\n"
        "5) «👤 Профиль» — твоя публичная карточка\n\n"
        "📩 Уведомления приходят в ЛС, когда:\n"
        "• ты получил достижение\n"
        "• тебе начислили очки по результату матча\n"
        "• на тебя подписались\n"
        "• ты поднялся в рейтинге\n\n"
        "Команда админа:\n"
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

# ===== ADMIN actions per match =====
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
            f"Матч #{mid}: {row['title']}\n\nВыбери, по чему ставим результат (и начисляем очки):",
            reply_markup=bet_type_kb(mid, mode="setres")
        )
        await call.answer()
        return

@dp.callback_query(F.data.startswith("type:setres:"))
async def choose_type_setres(call: CallbackQuery):
    upsert_user(call.from_user.id, call.from_user.username, call.from_user.first_name, call.from_user.last_name)

    if not is_admin(call.from_user.id):
        await call.answer("Только для админа.", show_alert=True)
        return

    _, _, mid, bt = call.data.split(":")
    mid = int(mid)

    if bt == "1x2":
        await call.message.edit_text("Поставь результат 1X2:", reply_markup=kb_1x2(mid, "setr"))
    elif bt == "score":
        await call.message.edit_text("Поставь результат (точный счёт):", reply_markup=kb_score(mid, "setr"))
    elif bt == "total":
        await call.message.edit_text("Поставь результат (тотал):", reply_markup=kb_total(mid, "setr"))
    elif bt == "btts":
        await call.message.edit_text("Поставь результат (обе забьют):", reply_markup=kb_btts(mid, "setr"))

    await call.answer()

@dp.callback_query(F.data.startswith("setr:"))
async def set_result_cb(call: CallbackQuery):
    upsert_user(call.from_user.id, call.from_user.username, call.from_user.first_name, call.from_user.last_name)

    if not is_admin(call.from_user.id):
        await call.answer("Только для админа.", show_alert=True)
        return

    # setr:{mid}:{bet_type}:{result}
    _, mid, bt, result = call.data.split(":", 3)
    mid = int(mid)

    m = get_match(mid)
    if not m:
        await call.answer("Матч не найден.", show_alert=True)
        return

    # для уведомлений: статистика по матчу (до начисления)
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

    # Уведомления (Layer 2): всем, кто голосовал по этому типу — исход и очки
    season = current_season()
    with db() as con:
        voters = con.execute("""
            SELECT user_id, COALESCE(username,'') as username, choice
            FROM votes
            WHERE match_id=? AND bet_type=?
        """, (mid, bt)).fetchall()

        # обновим ranks/achievements после начислений
        bot = call.bot

        for v in voters:
            uid = int(v["user_id"])
            uname = v["username"]
            ch = v["choice"]
            correct = (ch == result)

            # место/очки/серия после начисления
            srow = con.execute("""
                SELECT points, correct, total, streak
                FROM scores_season WHERE season=? AND user_id=?
            """, (season, uid)).fetchone()

            pts = int(srow["points"]) if srow else 0
            streak = int(srow["streak"]) if srow else 0

            # распределение толпы
            crowd_line = ""
            if total_this_type > 0:
                # соберём самые популярные 2
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

            # ачивки + изменение ранга
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

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
