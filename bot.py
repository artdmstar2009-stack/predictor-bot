import os
import asyncio
import logging
import sqlite3
from datetime import datetime, timezone, timedelta
from typing import Optional, Tuple, List

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
)

# ===================== CONFIG =====================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0").strip() or "0")
DB_PATH = os.getenv("DB_PATH", "bot.db")

PREDICTION_CLOSE_SECONDS = int(os.getenv("PREDICTION_CLOSE_SECONDS", "120"))
START_BALANCE = int(os.getenv("START_BALANCE", "1000"))

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bot")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

# ===================== BOT =====================
bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()

# ===================== RENDER WEB SERVICE =====================
async def health(request):
    return web.Response(text="OK")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", "10000"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info(f"🌐 Web server started on port {port}")

# ===================== DB HELPERS =====================
def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def _col_exists(cur: sqlite3.Cursor, table: str, col: str) -> bool:
    cur.execute(f"PRAGMA table_info({table})")
    return any(r["name"] == col for r in cur.fetchall())

def init_db():
    con = db()
    cur = con.cursor()

    # users
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            sport TEXT,
            league TEXT,
            balance INTEGER DEFAULT 0,
            points INTEGER DEFAULT 0,
            correct INTEGER DEFAULT 0,
            total INTEGER DEFAULT 0,
            streak INTEGER DEFAULT 0
        )
    """)

    # matches
    cur.execute("""
        CREATE TABLE IF NOT EXISTS matches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sport TEXT NOT NULL,
            league TEXT,
            team1 TEXT NOT NULL,
            team2 TEXT NOT NULL,
            start_time_utc TEXT NOT NULL,   -- ISO 8601 UTC
            status TEXT NOT NULL DEFAULT 'open',  -- open/locked/finished
            result_team1 INTEGER,
            result_team2 INTEGER
        )
    """)

    # predictions
    cur.execute("""
        CREATE TABLE IF NOT EXISTS predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            match_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            outcome TEXT NOT NULL,           -- '1','X','2'
            created_at_utc TEXT NOT NULL,
            UNIQUE(match_id, user_id),
            FOREIGN KEY(match_id) REFERENCES matches(id)
        )
    """)

    # --- MIGRATIONS (safe add columns if DB is older) ---
    # users columns
    for col, ddl in [
        ("sport", "ALTER TABLE users ADD COLUMN sport TEXT"),
        ("league", "ALTER TABLE users ADD COLUMN league TEXT"),
        ("balance", "ALTER TABLE users ADD COLUMN balance INTEGER DEFAULT 0"),
        ("points", "ALTER TABLE users ADD COLUMN points INTEGER DEFAULT 0"),
        ("correct", "ALTER TABLE users ADD COLUMN correct INTEGER DEFAULT 0"),
        ("total", "ALTER TABLE users ADD COLUMN total INTEGER DEFAULT 0"),
        ("streak", "ALTER TABLE users ADD COLUMN streak INTEGER DEFAULT 0"),
        ("first_name", "ALTER TABLE users ADD COLUMN first_name TEXT"),
    ]:
        if not _col_exists(cur, "users", col):
            cur.execute(ddl)

    # matches columns
    for col, ddl in [
        ("status", "ALTER TABLE matches ADD COLUMN status TEXT NOT NULL DEFAULT 'open'"),
        ("result_team1", "ALTER TABLE matches ADD COLUMN result_team1 INTEGER"),
        ("result_team2", "ALTER TABLE matches ADD COLUMN result_team2 INTEGER"),
        ("league", "ALTER TABLE matches ADD COLUMN league TEXT"),
    ]:
        if not _col_exists(cur, "matches", col):
            cur.execute(ddl)

    con.commit()
    con.close()

def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def parse_utc_iso(s: str) -> datetime:
    # expects ISO string; allow Z
    s = s.replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

def to_utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()

# ===================== USER LOGIC =====================
def upsert_user(user_id: int, username: Optional[str], first_name: Optional[str]) -> sqlite3.Row:
    con = db()
    cur = con.cursor()
    cur.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    if row is None:
        cur.execute("""
            INSERT INTO users (user_id, username, first_name, balance, points, correct, total, streak, sport, league)
            VALUES (?, ?, ?, ?, 0, 0, 0, 0, NULL, NULL)
        """, (user_id, username or "", first_name or "", START_BALANCE))
        con.commit()
    else:
        cur.execute("""
            UPDATE users SET username=?, first_name=? WHERE user_id=?
        """, (username or row["username"] or "", first_name or row["first_name"] or "", user_id))
        con.commit()

    cur.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
    row2 = cur.fetchone()
    con.close()
    return row2

def set_user_sport(user_id: int, sport: str, league: Optional[str]):
    con = db()
    cur = con.cursor()
    cur.execute("UPDATE users SET sport=?, league=? WHERE user_id=?", (sport, league, user_id))
    con.commit()
    con.close()

def get_user(user_id: int) -> Optional[sqlite3.Row]:
    con = db()
    cur = con.cursor()
    cur.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    con.close()
    return row

# ===================== MATCH LOGIC =====================
def sport_allows_draw(sport: str, league: Optional[str]) -> bool:
    # Football: allow draw
    return sport == "football"

def match_is_locked(start_time_utc: datetime) -> bool:
    return now_utc() >= (start_time_utc - timedelta(seconds=PREDICTION_CLOSE_SECONDS))

def ensure_match_statuses():
    """Lock matches that are near kickoff (do not finish here)."""
    con = db()
    cur = con.cursor()
    cur.execute("SELECT id, start_time_utc, status FROM matches WHERE status='open'")
    rows = cur.fetchall()
    for r in rows:
        st = parse_utc_iso(r["start_time_utc"])
        if match_is_locked(st):
            cur.execute("UPDATE matches SET status='locked' WHERE id=?", (r["id"],))
    con.commit()
    con.close()

def list_matches_for_user_sport(user: sqlite3.Row) -> List[sqlite3.Row]:
    ensure_match_statuses()
    con = db()
    cur = con.cursor()
    # show open or locked (active), but not finished
    if user["league"]:
        cur.execute("""
            SELECT * FROM matches
            WHERE sport=? AND league=? AND status IN ('open','locked')
            ORDER BY start_time_utc ASC
            LIMIT 30
        """, (user["sport"], user["league"]))
    else:
        cur.execute("""
            SELECT * FROM matches
            WHERE sport=? AND (league IS NULL OR league='') AND status IN ('open','locked')
            ORDER BY start_time_utc ASC
            LIMIT 30
        """, (user["sport"],))
    rows = cur.fetchall()
    con.close()
    return rows

def get_match(match_id: int) -> Optional[sqlite3.Row]:
    ensure_match_statuses()
    con = db()
    cur = con.cursor()
    cur.execute("SELECT * FROM matches WHERE id=?", (match_id,))
    row = cur.fetchone()
    con.close()
    return row

def format_match_row(m: sqlite3.Row) -> str:
    st = parse_utc_iso(m["start_time_utc"])
    status = m["status"]
    lock_tag = "🔒" if status == "locked" else "✅"
    # show UTC time (simple). If want local later.
    time_txt = st.strftime("%Y-%m-%d %H:%M UTC")
    league = (m["league"] or "").strip()
    league_txt = f" ({league})" if league else ""
    return f"{lock_tag} <b>{m['team1']}</b> vs <b>{m['team2']}</b>{league_txt}\n🕒 {time_txt}"

def compute_outcome(score1: int, score2: int, allow_draw: bool) -> str:
    if score1 > score2:
        return "1"
    if score2 > score1:
        return "2"
    return "X" if allow_draw else "X"  # for non-draw sports, we won't allow predicting X anyway

# ===================== PREDICTIONS / POINTS =====================
def get_user_prediction(match_id: int, user_id: int) -> Optional[sqlite3.Row]:
    con = db()
    cur = con.cursor()
    cur.execute("SELECT * FROM predictions WHERE match_id=? AND user_id=?", (match_id, user_id))
    row = cur.fetchone()
    con.close()
    return row

def upsert_prediction(match_id: int, user_id: int, outcome: str):
    con = db()
    cur = con.cursor()
    cur.execute("""
        INSERT INTO predictions (match_id, user_id, outcome, created_at_utc)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(match_id, user_id) DO UPDATE SET
            outcome=excluded.outcome,
            created_at_utc=excluded.created_at_utc
    """, (match_id, user_id, outcome, to_utc_iso(now_utc())))
    con.commit()
    con.close()

def award_points_for_match(match_id: int):
    m = get_match(match_id)
    if not m:
        return
    if m["status"] == "finished":
        return
    if m["result_team1"] is None or m["result_team2"] is None:
        return

    allow_draw = sport_allows_draw(m["sport"], m["league"])
    real_outcome = compute_outcome(int(m["result_team1"]), int(m["result_team2"]), allow_draw)

    con = db()
    cur = con.cursor()

    # mark finished
    cur.execute("UPDATE matches SET status='finished' WHERE id=?", (match_id,))

    # fetch predictions
    cur.execute("SELECT user_id, outcome FROM predictions WHERE match_id=?", (match_id,))
    preds = cur.fetchall()

    for p in preds:
        uid = int(p["user_id"])
        guess = p["outcome"]

        # update totals
        cur.execute("SELECT * FROM users WHERE user_id=?", (uid,))
        u = cur.fetchone()
        if not u:
            continue

        total = int(u["total"] or 0) + 1
        correct = int(u["correct"] or 0)
        points = int(u["points"] or 0)
        streak = int(u["streak"] or 0)

        if guess == real_outcome:
            correct += 1
            points += 1
            streak = streak + 1 if streak >= 0 else 1
        else:
            streak = streak - 1 if streak <= 0 else -1

        cur.execute("""
            UPDATE users SET total=?, correct=?, points=?, streak=?
            WHERE user_id=?
        """, (total, correct, points, streak, uid))

    con.commit()
    con.close()

# ===================== UI: KEYBOARDS =====================
def main_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="⚽ Активные матчи")],
            [KeyboardButton(text="📌 Мои прогнозы")],
            [KeyboardButton(text="👤 Профиль"), KeyboardButton(text="🏆 Лидерборд")],
            [KeyboardButton(text="🏟 Выбрать спорт"), KeyboardButton(text="❓ Помощь")],
        ],
        resize_keyboard=True
    )

def sport_choice_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚽ Футбол", callback_data="sport:football")],
        [InlineKeyboardButton(text="🏒 Хоккей", callback_data="sport:hockey")],
        [InlineKeyboardButton(text="🎮 Киберспорт", callback_data="sport:esports")],
    ])

def hockey_league_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🇺🇸 NHL", callback_data="league:NHL")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back:sport")],
    ])

def esports_league_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔫 CS2", callback_data="league:CS2")],
        [InlineKeyboardButton(text="🧙 Dota 2", callback_data="league:Dota2")],
        [InlineKeyboardButton(text="🧠 LoL", callback_data="league:LoL")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back:sport")],
    ])

def match_list_kb(matches: List[sqlite3.Row]) -> InlineKeyboardMarkup:
    rows = []
    for m in matches:
        st = parse_utc_iso(m["start_time_utc"])
        label_time = st.strftime("%d %b %H:%M")
        rows.append([InlineKeyboardButton(
            text=f"{label_time} · {m['team1']} vs {m['team2']}",
            callback_data=f"match:{m['id']}"
        )])
    rows.append([InlineKeyboardButton(text="🏟 Сменить спорт", callback_data="open:sport")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def outcome_kb(match_id: int, allow_draw: bool) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(text="1️⃣ Победа 1", callback_data=f"pick:{match_id}:1"),
    ]
    if allow_draw:
        buttons.append(InlineKeyboardButton(text="➖ Ничья", callback_data=f"pick:{match_id}:X"))
    buttons.append(InlineKeyboardButton(text="2️⃣ Победа 2", callback_data=f"pick:{match_id}:2"))
    # arrange rows
    row1 = buttons[:2] if allow_draw else buttons[:1] + buttons[1:2]
    rows = []
    if allow_draw:
        rows.append([buttons[0], buttons[1]])
        rows.append([buttons[2]])
    else:
        rows.append([buttons[0]])
        rows.append([buttons[1]])
    rows.append([InlineKeyboardButton(text="⬅️ К матчам", callback_data="open:matches")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

# ===================== DISPLAY HELPERS =====================
def display_user_name(user_id: int, username: Optional[str], first_name: Optional[str]) -> str:
    if username and username.strip():
        u = username.strip().lstrip("@")
        return f"@{u}"
    name = (first_name or "Игрок").replace("<", "").replace(">", "")
    return f'<a href="tg://user?id={user_id}">{name}</a>'

def level_from_points(points: int) -> Tuple[str, int, Optional[int]]:
    # name, lower_bound, next_bound
    tiers = [
        ("Bronze", 0, 50),
        ("Silver", 50, 150),
        ("Gold", 150, 400),
        ("Elite", 400, None),
    ]
    for name, lo, hi in tiers:
        if hi is None:
            if points >= lo:
                return (name, lo, None)
        else:
            if lo <= points < hi:
                return (name, lo, hi)
    return ("Bronze", 0, 50)

def progress_bar(curr: int, lo: int, hi: Optional[int], width: int = 14) -> str:
    if hi is None:
        return "██████████████"
    span = max(hi - lo, 1)
    pos = max(min(curr - lo, span), 0)
    filled = int(round(width * (pos / span)))
    return "█" * filled + "░" * (width - filled)

# ===================== GUARD =====================
async def ensure_sport_selected(message: Message) -> Optional[sqlite3.Row]:
    user = upsert_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    if not user["sport"] or not str(user["sport"]).strip():
        await message.answer("👋 Выбери вид спорта:", reply_markup=sport_choice_kb())
        return None
    return user

# ===================== HANDLERS =====================
@dp.message(CommandStart())
async def cmd_start(message: Message):
    user = upsert_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    if not user["sport"] or not str(user["sport"]).strip():
        await message.answer("👋 Добро пожаловать!\nВыбери вид спорта:", reply_markup=sport_choice_kb())
        return
    await message.answer("Главное меню:", reply_markup=main_menu_kb())

@dp.message(F.text == "🏟 Выбрать спорт")
async def btn_choose_sport(message: Message):
    # Починено: всегда открывает выбор спорта
    upsert_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    await message.answer("🏟 Выбери вид спорта:", reply_markup=sport_choice_kb())

@dp.callback_query(F.data == "open:sport")
async def cb_open_sport(cb: CallbackQuery):
    upsert_user(cb.from_user.id, cb.from_user.username, cb.from_user.first_name)
    await cb.message.answer("🏟 Выбери вид спорта:", reply_markup=sport_choice_kb())
    await cb.answer()

@dp.callback_query(F.data.startswith("sport:"))
async def cb_choose_sport(cb: CallbackQuery):
    sport = cb.data.split(":", 1)[1]
    if sport == "football":
        set_user_sport(cb.from_user.id, "football", None)
        await cb.message.answer("⚽ Футбол выбран", reply_markup=main_menu_kb())
    elif sport == "hockey":
        set_user_sport(cb.from_user.id, "hockey", None)
        await cb.message.answer("🏒 Выбери лигу:", reply_markup=hockey_league_kb())
    elif sport == "esports":
        set_user_sport(cb.from_user.id, "esports", None)
        await cb.message.answer("🎮 Выбери дисциплину:", reply_markup=esports_league_kb())
    await cb.answer()

@dp.callback_query(F.data.startswith("league:"))
async def cb_choose_league(cb: CallbackQuery):
    league = cb.data.split(":", 1)[1]
    user = get_user(cb.from_user.id)
    sport = (user["sport"] if user else "") or ""
    set_user_sport(cb.from_user.id, sport, league)
    await cb.message.answer(f"✅ Выбрано: <b>{sport}</b> / <b>{league}</b>", reply_markup=main_menu_kb())
    await cb.answer()

@dp.callback_query(F.data == "back:sport")
async def cb_back_sport(cb: CallbackQuery):
    await cb.message.answer("🏟 Выбери вид спорта:", reply_markup=sport_choice_kb())
    await cb.answer()

@dp.message(F.text == "❓ Помощь")
async def btn_help(message: Message):
    await message.answer(
        "ℹ️ <b>Как играть</b>\n"
        "1) Выбери спорт\n"
        "2) Открой «Активные матчи»\n"
        "3) Выбери матч и исход\n"
        f"⏳ Прогнозы закрываются за <b>{PREDICTION_CLOSE_SECONDS//60}–{max(1,PREDICTION_CLOSE_SECONDS//60)}</b> минут до начала.\n\n"
        "Очки: +1 за угаданный исход.\n"
        "Админ: /addmatch, /setresult, /matches"
    )

@dp.message(F.text == "⚽ Активные матчи")
async def btn_active_matches(message: Message):
    user = await ensure_sport_selected(message)
    if not user:
        return
    if user["sport"] in ("hockey", "esports") and not (user["league"] and str(user["league"]).strip()):
        # need league selection for hockey/esports
        if user["sport"] == "hockey":
            await message.answer("🏒 Выбери лигу:", reply_markup=hockey_league_kb())
        else:
            await message.answer("🎮 Выбери дисциплину:", reply_markup=esports_league_kb())
        return

    matches = list_matches_for_user_sport(user)
    if not matches:
        await message.answer("Пока нет активных матчей для выбранного спорта/лиги.")
        return

    await message.answer("📋 Выбери матч:", reply_markup=match_list_kb(matches))

@dp.callback_query(F.data == "open:matches")
async def cb_open_matches(cb: CallbackQuery):
    user = get_user(cb.from_user.id)
    if not user or not user["sport"]:
        await cb.message.answer("Выбери спорт:", reply_markup=sport_choice_kb())
        await cb.answer()
        return

    matches = list_matches_for_user_sport(user)
    if not matches:
        await cb.message.answer("Пока нет активных матчей для выбранного спорта/лиги.")
        await cb.answer()
        return

    await cb.message.answer("📋 Выбери матч:", reply_markup=match_list_kb(matches))
    await cb.answer()

@dp.callback_query(F.data.startswith("match:"))
async def cb_match_details(cb: CallbackQuery):
    user = get_user(cb.from_user.id)
    if not user or not user["sport"]:
        await cb.message.answer("Выбери спорт:", reply_markup=sport_choice_kb())
        await cb.answer()
        return

    match_id = int(cb.data.split(":", 1)[1])
    m = get_match(match_id)
    if not m:
        await cb.message.answer("Матч не найден.")
        await cb.answer()
        return

    allow_draw = sport_allows_draw(m["sport"], m["league"])
    st = parse_utc_iso(m["start_time_utc"])
    locked = (m["status"] == "locked") or match_is_locked(st)
    if locked and m["status"] != "finished":
        # ensure status updated
        con = db()
        cur = con.cursor()
        cur.execute("UPDATE matches SET status='locked' WHERE id=? AND status!='finished'", (match_id,))
        con.commit()
        con.close()
        m = get_match(match_id)

    pred = get_user_prediction(match_id, cb.from_user.id)
    pred_txt = f"\n\n🧾 Твой прогноз: <b>{pred['outcome']}</b>" if pred else ""

    header = format_match_row(m) + pred_txt

    if m["status"] == "finished":
        s1 = m["result_team1"]
        s2 = m["result_team2"]
        await cb.message.answer(f"{header}\n\n✅ Завершён: <b>{s1}:{s2}</b>")
        await cb.answer()
        return

    if m["status"] == "locked":
        await cb.message.answer(f"{header}\n\n🔒 Прогнозы закрыты.")
        await cb.answer()
        return

    # open for voting
    await cb.message.answer(header + "\n\nВыбери исход:", reply_markup=outcome_kb(match_id, allow_draw))
    await cb.answer()

@dp.callback_query(F.data.startswith("pick:"))
async def cb_pick_outcome(cb: CallbackQuery):
    _, match_id_s, outcome = cb.data.split(":", 2)
    match_id = int(match_id_s)

    user = upsert_user(cb.from_user.id, cb.from_user.username, cb.from_user.first_name)
    m = get_match(match_id)
    if not m:
        await cb.message.answer("Матч не найден.")
        await cb.answer()
        return

    allow_draw = sport_allows_draw(m["sport"], m["league"])
    if outcome == "X" and not allow_draw:
        await cb.message.answer("Для этого спорта ничья недоступна.")
        await cb.answer()
        return

    st = parse_utc_iso(m["start_time_utc"])
    if match_is_locked(st) or m["status"] != "open":
        await cb.message.answer("🔒 Прогнозы уже закрыты.")
        await cb.answer()
        return

    upsert_prediction(match_id, user["user_id"], outcome)
    await cb.message.answer(f"✅ Прогноз сохранён: <b>{outcome}</b>")
    await cb.answer()

@dp.message(F.text == "📌 Мои прогнозы")
async def btn_my_predictions(message: Message):
    user = await ensure_sport_selected(message)
    if not user:
        return

    con = db()
    cur = con.cursor()
    cur.execute("""
        SELECT p.outcome, p.created_at_utc, m.team1, m.team2, m.start_time_utc, m.status, m.result_team1, m.result_team2
        FROM predictions p
        JOIN matches m ON m.id = p.match_id
        WHERE p.user_id = ?
        ORDER BY p.created_at_utc DESC
        LIMIT 15
    """, (user["user_id"],))
    rows = cur.fetchall()
    con.close()

    if not rows:
        await message.answer("У тебя пока нет прогнозов.")
        return

    lines = ["📌 <b>Твои прогнозы</b> (последние):\n"]
    for r in rows:
        st = parse_utc_iso(r["start_time_utc"]).strftime("%d %b %H:%M UTC")
        base = f"• {r['team1']} vs {r['team2']} ({st}) — прогноз <b>{r['outcome']}</b>"
        if r["status"] == "finished":
            base += f" | итог <b>{r['result_team1']}:{r['result_team2']}</b>"
        elif r["status"] == "locked":
            base += " | 🔒 закрыто"
        lines.append(base)

    await message.answer("\n".join(lines))

@dp.message(F.text == "👤 Профиль")
async def btn_profile(message: Message):
    user = await ensure_sport_selected(message)
    if not user:
        return

    points = int(user["points"] or 0)
    correct = int(user["correct"] or 0)
    total = int(user["total"] or 0)
    streak = int(user["streak"] or 0)
    balance = int(user["balance"] or START_BALANCE)

    acc = (correct / total * 100.0) if total > 0 else 0.0
    lvl, lo, hi = level_from_points(points)
    bar = progress_bar(points, lo, hi)
    to_next = (hi - points) if hi is not None else None

    name = display_user_name(user["user_id"], user["username"], user["first_name"])
    streak_txt = f"🔥 Серия: {streak:+d}" if streak != 0 else "🔥 Серия: 0"

    lines = [
        f"👤 {name}",
        "━━━━━━━━━━━━━━",
        f"🏅 Уровень: <b>{lvl}</b>",
        f"🏆 Очки: <b>{points}</b>",
        f"💰 Баланс: <b>{balance}</b> 🪙",
        "",
        f"⚽ Прогнозов: <b>{total}</b>",
        f"✅ Точность: <b>{acc:.0f}%</b>",
        streak_txt,
        "━━━━━━━━━━━━━━",
        f"{bar}",
    ]
    if to_next is not None:
        lines.append(f"⬆️ До следующего уровня: <b>{to_next}</b> очков")
    else:
        lines.append("⭐ Максимальный уровень!")

    await message.answer("\n".join(lines))

@dp.message(F.text == "🏆 Лидерборд")
async def btn_leaderboard(message: Message):
    user = await ensure_sport_selected(message)
    if not user:
        return

    con = db()
    cur = con.cursor()
    # global leaderboard by points (can later filter by sport if we keep separate stats)
    cur.execute("""
        SELECT user_id, username, first_name, points
        FROM users
        ORDER BY points DESC, correct DESC, total DESC
        LIMIT 15
    """)
    rows = cur.fetchall()
    con.close()

    if not rows:
        await message.answer("Пока нет данных лидерборда.")
        return

    lines = ["🏆 <b>Лидерборд</b>\n"]
    medals = ["🥇", "🥈", "🥉"]
    for i, r in enumerate(rows):
        medal = medals[i] if i < 3 else f"{i+1}."
        name = display_user_name(int(r["user_id"]), r["username"], r["first_name"])
        pts = int(r["points"] or 0)
        lines.append(f"{medal} {name} — <b>{pts}</b>")

    await message.answer("\n".join(lines))

# ===================== ADMIN COMMANDS =====================
def is_admin(user_id: int) -> bool:
    return ADMIN_ID != 0 and user_id == ADMIN_ID

@dp.message(Command("matches"))
async def cmd_matches(message: Message):
    if not is_admin(message.from_user.id):
        return
    con = db()
    cur = con.cursor()
    cur.execute("""
        SELECT id, sport, league, team1, team2, start_time_utc, status, result_team1, result_team2
        FROM matches
        ORDER BY start_time_utc ASC
        LIMIT 50
    """)
    rows = cur.fetchall()
    con.close()

    if not rows:
        await message.answer("Матчей нет.")
        return

    out = ["📋 <b>Матчи</b> (id / sport / league / status):\n"]
    for r in rows:
        st = parse_utc_iso(r["start_time_utc"]).strftime("%Y-%m-%d %H:%M")
        lg = r["league"] or "-"
        res = ""
        if r["status"] == "finished":
            res = f" | итог {r['result_team1']}:{r['result_team2']}"
        out.append(f"#{r['id']} · {r['sport']} · {lg} · {r['team1']} vs {r['team2']} · {st} UTC · {r['status']}{res}")
    await message.answer("\n".join(out))

@dp.message(Command("addmatch"))
async def cmd_addmatch(message: Message):
    """
    /addmatch sport league team1 | team2 | 2026-02-06 20:30
    examples:
    /addmatch football - Arsenal | Chelsea | 2026-02-06 20:30
    /addmatch hockey NHL Rangers | Bruins | 2026-02-06 00:00
    /addmatch esports CS2 NAVI | FaZe | 2026-02-06 18:00
    """
    if not is_admin(message.from_user.id):
        return

    text = message.text.strip()
    parts = text.split(" ", 2)
    if len(parts) < 3:
        await message.answer("Формат:\n/addmatch sport league team1 | team2 | YYYY-MM-DD HH:MM (UTC)")
        return

    _, sport, rest = parts
    rest = rest.strip()
    # league then remainder
    rest2 = rest.split(" ", 1)
    if len(rest2) < 2:
        await message.answer("Формат:\n/addmatch sport league team1 | team2 | YYYY-MM-DD HH:MM (UTC)")
        return
    league, payload = rest2[0].strip(), rest2[1].strip()
    if league == "-" or league.lower() == "none":
        league = ""

    try:
        t1, t2, dt_s = [x.strip() for x in payload.split("|", 2)]
        dt = datetime.strptime(dt_s, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    except Exception:
        await message.answer("Не могу разобрать.\nПример:\n/addmatch hockey NHL Rangers | Bruins | 2026-02-06 00:00")
        return

    con = db()
    cur = con.cursor()
    cur.execute("""
        INSERT INTO matches (sport, league, team1, team2, start_time_utc, status)
        VALUES (?, ?, ?, ?, ?, 'open')
    """, (sport, league, t1, t2, to_utc_iso(dt)))
    mid = cur.lastrowid
    con.commit()
    con.close()

    await message.answer(f"✅ Матч добавлен: id #{mid}\n{t1} vs {t2} ({to_utc_iso(dt)})")

@dp.message(Command("setresult"))
async def cmd_setresult(message: Message):
    """
    /setresult <match_id> <score1>-<score2>
    example: /setresult 12 3-2
    """
    if not is_admin(message.from_user.id):
        return

    parts = message.text.strip().split()
    if len(parts) != 3:
        await message.answer("Формат:\n/setresult <match_id> <score1>-<score2>\nПример: /setresult 12 3-2")
        return

    try:
        mid = int(parts[1])
        s1_s, s2_s = parts[2].split("-", 1)
        s1, s2 = int(s1_s), int(s2_s)
    except Exception:
        await message.answer("Неверный формат.\nПример: /setresult 12 3-2")
        return

    m = get_match(mid)
    if not m:
        await message.answer("Матч не найден.")
        return

    con = db()
    cur = con.cursor()
    cur.execute("""
        UPDATE matches SET result_team1=?, result_team2=? WHERE id=?
    """, (s1, s2, mid))
    con.commit()
    con.close()

    award_points_for_match(mid)
    await message.answer(f"✅ Результат сохранён и очки начислены: матч #{mid} итог {s1}:{s2}")

# ===================== BACKGROUND =====================
async def background_lock_loop():
    while True:
        try:
            ensure_match_statuses()
        except Exception:
            log.exception("lock loop error")
        await asyncio.sleep(30)

async def heartbeat():
    while True:
        log.info("HEARTBEAT: bot alive")
        await asyncio.sleep(300)

# ===================== MAIN =====================
async def main():
    init_db()
    asyncio.create_task(start_web_server())
    asyncio.create_task(background_lock_loop())
    asyncio.create_task(heartbeat())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
