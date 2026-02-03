import asyncio
import json
import logging
import os
import sqlite3
import threading
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Optional
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder


# ===================== CONFIG =====================
TOKEN = os.getenv("BOT_TOKEN", "").strip()

# Важно: если ADMIN_ID не задан/кривой — бот всё равно запустится,
# но /whoami покажет проблему.
try:
    ADMIN_ID = int(os.getenv("ADMIN_ID", "0").strip() or "0")
except Exception:
    ADMIN_ID = 0

DB_PATH = os.getenv("DB_PATH", "predictor.db")

# football-data.org (опционально)
FOOTBALL_DATA_TOKEN = os.getenv("FOOTBALL_DATA_TOKEN", "").strip()
FD_COMPETITIONS = os.getenv("FD_COMPETITIONS", "").strip()  # "PL,CL,PD"
# ================================================


dp = Dispatcher()
BOT_USERNAME: Optional[str] = None


# ===== Dummy HTTP server for Render Free Web Service =====
class DummyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

def run_http():
    port = int(os.environ.get("PORT", 10000))
    HTTPServer(("0.0.0.0", port), DummyHandler).serve_forever()

threading.Thread(target=run_http, daemon=True).start()
# ========================================================


# ===================== DB =====================
def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def now_iso() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")

def today_key() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d")

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
            last_seen TEXT
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

        # pending fixtures for admin approval
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

        # user state (only for "find player" input)
        con.execute("""
        CREATE TABLE IF NOT EXISTS user_state(
            user_id INTEGER PRIMARY KEY,
            state TEXT,
            updated_at TEXT
        )
        """)
        con.commit()


# ===================== USERS / STATE =====================
def upsert_user(u: Message):
    with db() as con:
        row = con.execute("SELECT user_id FROM users WHERE user_id=?", (u.from_user.id,)).fetchone()
        if row:
            con.execute("""
                UPDATE users SET username=?, first_name=?, last_name=?, last_seen=?
                WHERE user_id=?
            """, (u.from_user.username, u.from_user.first_name, u.from_user.last_name, now_iso(), u.from_user.id))
        else:
            con.execute("""
                INSERT INTO users(user_id, username, first_name, last_name, created_at, last_seen)
                VALUES(?,?,?,?,?,?)
            """, (u.from_user.id, u.from_user.username, u.from_user.first_name, u.from_user.last_name, now_iso(), now_iso()))
        con.commit()

def set_state(user_id: int, state: Optional[str]):
    with db() as con:
        if state is None:
            con.execute("DELETE FROM user_state WHERE user_id=?", (user_id,))
        else:
            con.execute("""
                INSERT INTO user_state(user_id, state, updated_at)
                VALUES(?,?,?)
                ON CONFLICT(user_id) DO UPDATE SET state=excluded.state, updated_at=excluded.updated_at
            """, (user_id, state, now_iso()))
        con.commit()

def get_state(user_id: int) -> Optional[str]:
    with db() as con:
        r = con.execute("SELECT state FROM user_state WHERE user_id=?", (user_id,)).fetchone()
        return r["state"] if r else None


# ===================== MATCHES =====================
def get_open_matches():
    with db() as con:
        return con.execute("""
            SELECT id, title, status, is_featured, bonus_multiplier
            FROM matches
            WHERE status='open'
            ORDER BY id DESC
        """).fetchall()

def get_match(mid: int):
    with db() as con:
        return con.execute("""
            SELECT id, title, status, is_featured, bonus_multiplier
            FROM matches
            WHERE id=?
        """, (mid,)).fetchone()

def create_match(title: str, featured: int = 0, mult: float = 1.0, external_id: Optional[str] = None) -> int:
    with db() as con:
        cur = con.execute("""
            INSERT INTO matches(title, status, is_featured, bonus_multiplier, external_id)
            VALUES(?, 'open', ?, ?, ?)
        """, (title, int(featured), float(mult), external_id))
        con.commit()
        return int(cur.lastrowid)


# ===================== UI =====================
BTN_ACTIVE = "⚽ Активные матчи"
BTN_HELP = "ℹ️ Помощь"
BTN_FIND = "🔎 Найти игрока"
BTN_NEW = "➕ Создать матч"
BTN_BACK = "⬅️ Назад"

def main_menu_kb(user_is_admin: bool):
    kb = ReplyKeyboardBuilder()
    kb.button(text=BTN_ACTIVE)
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


# ===================== /sync_today (variant 3) =====================
def fetch_today_fixtures(day_yyyy_mm_dd: str) -> list:
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

def match_exists_by_ext(ext_id: str) -> bool:
    with db() as con:
        r = con.execute("SELECT 1 FROM matches WHERE external_id=?", (ext_id,)).fetchone()
        return r is not None

def pending_kb(pid: int):
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Добавить", callback_data=f"pf:add:{pid}")
    kb.button(text="⭐ Матч дня x2", callback_data=f"pf:feat:{pid}:2")
    kb.button(text="❌ Пропустить", callback_data=f"pf:skip:{pid}")
    kb.adjust(1)
    return kb.as_markup()


# ===================== COMMANDS / HANDLERS =====================
@dp.message(CommandStart())
async def start(message: Message):
    upsert_user(message)
    set_state(message.from_user.id, None)
    await message.answer(
        "🤖 Predictor Bot\n\nМеню снизу 👇",
        reply_markup=main_menu_kb(is_admin(message.from_user.id))
    )

@dp.message(Command("whoami"))
async def whoami(message: Message):
    # главная диагностика: видит ли бот ADMIN_ID
    upsert_user(message)
    uid = message.from_user.id
    await message.answer(
        "🧩 Диагностика\n"
        f"Твой id: {uid}\n"
        f"ADMIN_ID (env): {ADMIN_ID}\n"
        f"Ты админ: {'ДА' if is_admin(uid) else 'НЕТ'}\n\n"
        f"FOOTBALL_DATA_TOKEN: {'есть' if bool(FOOTBALL_DATA_TOKEN) else 'нет'}\n"
        f"FD_COMPETITIONS: {FD_COMPETITIONS or '(пусто)'}",
        reply_markup=main_menu_kb(is_admin(uid))
    )

@dp.message(F.text == BTN_BACK)
async def back(message: Message):
    upsert_user(message)
    set_state(message.from_user.id, None)
    await message.answer("Главное меню 👇", reply_markup=main_menu_kb(is_admin(message.from_user.id)))

@dp.message(F.text == BTN_ACTIVE)
async def active_matches(message: Message):
    upsert_user(message)
    set_state(message.from_user.id, None)
    matches = get_open_matches()
    if not matches:
        await message.answer("Активных матчей пока нет.", reply_markup=main_menu_kb(is_admin(message.from_user.id)))
        return
    await message.answer("Выбери матч кнопкой ниже 👇", reply_markup=matches_list_kb(matches, is_admin(message.from_user.id)))

@dp.message(F.text.startswith("🏟 #"))
async def picked_match(message: Message):
    upsert_user(message)
    set_state(message.from_user.id, None)

    mid = parse_match_button(message.text)
    if not mid:
        await message.answer("Не понял матч. Нажми «⚽ Активные матчи».")
        return

    m = get_match(mid)
    if not m:
        await message.answer("Матч не найден. Обнови список: «⚽ Активные матчи».")
        return

    star = "⭐ " if int(m["is_featured"]) == 1 else ""
    mult = float(m["bonus_multiplier"]) if m["bonus_multiplier"] is not None else 1.0
    bonus = f"(x{mult:g}) " if int(m["is_featured"]) == 1 and mult != 1.0 else ""
    await message.answer(f"{star}{bonus}Матч #{mid}: {m['title']}\nСтатус: {m['status']}")

@dp.message(F.text == BTN_HELP)
async def help_menu(message: Message):
    upsert_user(message)
    set_state(message.from_user.id, None)
    await message.answer(
        "ℹ️ Помощь\n\n"
        "• «⚽ Активные матчи» — список матчей\n"
        "• «➕ Создать матч» — доступно только админу\n"
        "• /newmatch <название>\n"
        "• /newfeatured <множитель> <название>\n"
        "• /sync_today — подтянуть матчи на сегодня (админ)\n\n"
        "Диагностика:\n"
        "• /whoami — покажет твой id и ADMIN_ID",
        reply_markup=main_menu_kb(is_admin(message.from_user.id))
    )

# --- Create match button (admin) ---
@dp.message(F.text == BTN_NEW)
async def new_button(message: Message):
    upsert_user(message)
    if not is_admin(message.from_user.id):
        await message.answer("Эта кнопка только для админа.", reply_markup=main_menu_kb(False))
        return
    await message.answer(
        "➕ Создание матча\n\n"
        "• /newmatch Real vs Barca (20:00)\n"
        "• /newfeatured 2 Real vs Barca\n"
        "• /sync_today (карточки с подтверждением)\n",
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
    mid = create_match(title, featured=0, mult=1.0, external_id=None)
    await message.answer(f"✅ Матч создан: #{mid}\nНажми «⚽ Активные матчи».")

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
        await message.answer("Множитель должен быть числом, например 2")
        return
    title = parts[2].strip()
    mid = create_match(title, featured=1, mult=mult, external_id=None)
    await message.answer(f"⭐ Матч дня создан: #{mid} (x{mult:g})\nНажми «⚽ Активные матчи».")

# --- Sync today (admin) ---
@dp.message(Command("sync_today"))
async def sync_today(message: Message):
    upsert_user(message)
    if not is_admin(message.from_user.id):
        await message.answer("Только админ может делать /sync_today")
        return

    day = today_key()
    fixtures = fetch_today_fixtures(day)

    # показать ошибку, чтобы не было "тишины"
    if fixtures and isinstance(fixtures[0], dict) and fixtures[0].get("__error__"):
        await message.answer(
            "⚠️ Ошибка при запросе матчей:\n"
            f"{fixtures[0].get('__error__')}\n\n"
            f"{fixtures[0].get('__url__', '')}".strip()
        )
        return

    comps = set(x.strip() for x in FD_COMPETITIONS.split(",") if x.strip())
    sent = 0
    skipped = 0

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

        await message.answer(
            f"📅 {day}\n🏟 {title}\nЛига: {comp_code or '—'}\nKickoff(UTC): {kickoff or '—'}",
            reply_markup=pending_kb(pid)
        )
        sent += 1

    await message.answer(f"Готово ✅\nК рассмотрению: {sent}\nПропущено дублей: {skipped}")

@dp.callback_query(F.data.startswith("pf:"))
async def pending_actions(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Только админ", show_alert=True)
        return

    _, action, pid, *rest = call.data.split(":")
    pid = int(pid)

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
        mult = float(rest[0]) if rest else 2.0
        create_match(p["title"], featured=1, mult=mult, external_id=p["ext_id"])
        pending_delete(pid)
        await call.message.edit_text(call.message.text + f"\n\n⭐ Добавлено как Матч дня (x{mult:g})")
        await call.answer()
        return


# ===================== FIND PLAYER (без “съедания” всех сообщений) =====================
@dp.message(F.text == BTN_FIND)
async def find_start(message: Message):
    upsert_user(message)
    set_state(message.from_user.id, "await_find_username")
    await message.answer("🔎 Введи @username игрока (или просто username).")

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

@dp.message(lambda m: get_state(m.from_user.id) == "await_find_username")
async def find_input(message: Message):
    upsert_user(message)
    username = (message.text or "").strip()
    uid = find_user_by_username(username)
    set_state(message.from_user.id, None)
    if not uid:
        await message.answer("Не нашёл игрока. Он должен хотя бы раз запустить бота. Попробуй ещё раз.")
        return
    await message.answer(f"✅ Нашёл пользователя с id: {uid}")

# =========================================================


async def main():
    logging.basicConfig(level=logging.INFO)

    # Суперважные логи — сразу видно видит ли бот ADMIN_ID
    logging.info("BOOT: ADMIN_ID=%s (env), DB=%s", ADMIN_ID, DB_PATH)
    logging.info("BOOT: FOOTBALL_DATA_TOKEN=%s, FD_COMPETITIONS=%s",
                 "set" if FOOTBALL_DATA_TOKEN else "not set",
                 FD_COMPETITIONS or "(empty)")

    if not TOKEN:
        raise RuntimeError("BOT_TOKEN не задан в переменных окружения")

    init_db()

    bot = Bot(TOKEN)
    global BOT_USERNAME
    me = await bot.get_me()
    BOT_USERNAME = me.username

    try:
        await dp.start_polling(bot)
    except Exception:
        # если будет TelegramConflictError — увидишь в логах
        logging.exception("Polling crashed")
        raise

if __name__ == "__main__":
    asyncio.run(main())
