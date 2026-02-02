import asyncio
import logging
import os
import sqlite3
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

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

# ===== Fake HTTP server (Render free) =====
class DummyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running")

def run_http():
    port = int(os.environ.get("PORT", 10000))
    HTTPServer(("0.0.0.0", port), DummyHandler).serve_forever()

threading.Thread(target=run_http, daemon=True).start()
# ========================================


# ===== DATABASE =====
def db():
    return sqlite3.connect(DB_PATH)

def init_db():
    with db() as con:
        con.execute("""
        CREATE TABLE IF NOT EXISTS matches(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            status TEXT DEFAULT 'open'
        )
        """)
        con.execute("""
        CREATE TABLE IF NOT EXISTS votes(
            match_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            username TEXT,
            bet_type TEXT NOT NULL,
            choice TEXT NOT NULL,
            PRIMARY KEY(match_id, user_id)
        )
        """)
        con.commit()

def is_admin(uid: int) -> bool:
    return uid == ADMIN_ID

def get_open_matches():
    with db() as con:
        return con.execute(
            "SELECT id, title FROM matches WHERE status='open' ORDER BY id DESC"
        ).fetchall()


# ===== MENUS (Reply keyboards) =====
BTN_ACTIVE = "⚽ Активные матчи"
BTN_MY = "📊 Мои прогнозы"
BTN_LB = "🏆 Лидерборд"
BTN_HELP = "ℹ️ Помощь"
BTN_NEW = "➕ Создать матч"
BTN_BACK = "⬅️ Назад"

def main_menu_kb(user_is_admin: bool):
    kb = ReplyKeyboardBuilder()
    kb.button(text=BTN_ACTIVE)
    kb.button(text=BTN_MY)
    kb.button(text=BTN_LB)
    kb.button(text=BTN_HELP)
    if user_is_admin:
        kb.button(text=BTN_NEW)
    kb.adjust(2)
    return kb.as_markup(resize_keyboard=True)

def matches_list_kb(matches, user_is_admin: bool):
    """
    Показываем список матчей как кнопки меню.
    Кнопка содержит id: '🏟 #12 Real vs Barca'
    """
    kb = ReplyKeyboardBuilder()

    for mid, title in matches:
        # Важно: id в кнопке нужен, чтобы однозначно понять какой матч выбран
        kb.button(text=f"🏟 #{mid} {title}")

    kb.button(text=BTN_BACK)
    if user_is_admin:
        kb.button(text=BTN_NEW)

    kb.adjust(1)
    return kb.as_markup(resize_keyboard=True)


# ===== INLINE KEYBOARDS (выбор прогноза) =====
def bet_type_kb(mid: int):
    kb = InlineKeyboardBuilder()
    kb.button(text="1️⃣ 1X2", callback_data=f"type:{mid}:1x2")
    kb.button(text="🎯 Точный счёт", callback_data=f"type:{mid}:score")
    kb.button(text="⚽ Тотал", callback_data=f"type:{mid}:total")
    kb.button(text="🔥 Обе забьют", callback_data=f"type:{mid}:btts")
    kb.adjust(2)
    return kb.as_markup()

def kb_1x2(mid: int):
    kb = InlineKeyboardBuilder()
    kb.button(text="🏠 Хозяева", callback_data=f"vote:{mid}:1x2:home")
    kb.button(text="🤝 Ничья", callback_data=f"vote:{mid}:1x2:draw")
    kb.button(text="🚌 Гости", callback_data=f"vote:{mid}:1x2:away")
    kb.adjust(1)
    return kb.as_markup()

def kb_score(mid: int):
    kb = InlineKeyboardBuilder()
    for s in ["1:0", "2:1", "1:1", "0:0"]:
        kb.button(text=s, callback_data=f"vote:{mid}:score:{s}")
    kb.adjust(2)
    return kb.as_markup()

def kb_total(mid: int):
    kb = InlineKeyboardBuilder()
    kb.button(text="⚽ Больше 2.5", callback_data=f"vote:{mid}:total:over")
    kb.button(text="🧱 Меньше 2.5", callback_data=f"vote:{mid}:total:under")
    kb.adjust(1)
    return kb.as_markup()

def kb_btts(mid: int):
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Да", callback_data=f"vote:{mid}:btts:yes")
    kb.button(text="❌ Нет", callback_data=f"vote:{mid}:btts:no")
    kb.adjust(1)
    return kb.as_markup()


# ===== HELPERS =====
def parse_match_button(text: str) -> int | None:
    """
    Ожидаем формат: '🏟 #12 Название'
    Достаём 12.
    """
    text = text.strip()
    if not text.startswith("🏟 #"):
        return None
    try:
        after_hash = text.split("#", 1)[1]          # '12 Название'
        mid_str = after_hash.split(" ", 1)[0]       # '12'
        return int(mid_str)
    except Exception:
        return None


# ===== HANDLERS =====
@dp.message(CommandStart())
async def start(m: Message):
    await m.answer(
        "🤖 Бот-предиктор\n\n"
        "Нажми «⚽ Активные матчи» → выбери матч кнопкой → выбери прогноз 👇",
        reply_markup=main_menu_kb(is_admin(m.from_user.id))
    )


@dp.message(F.text == BTN_BACK)
async def back_to_main(m: Message):
    await m.answer("Главное меню 👇", reply_markup=main_menu_kb(is_admin(m.from_user.id)))


@dp.message(F.text == BTN_ACTIVE)
async def active_matches(m: Message):
    matches = get_open_matches()
    if not matches:
        await m.answer("Нет активных матчей.", reply_markup=main_menu_kb(is_admin(m.from_user.id)))
        return

    await m.answer(
        "Выбери матч кнопкой ниже 👇",
        reply_markup=matches_list_kb(matches, is_admin(m.from_user.id))
    )


# Выбор матча кнопкой из списка
@dp.message(F.text.startswith("🏟 #"))
async def picked_match(m: Message):
    mid = parse_match_button(m.text)
    if mid is None:
        await m.answer("Не понял матч. Нажми «⚽ Активные матчи» ещё раз.")
        return

    # Проверим что матч существует и открыт
    with db() as con:
        row = con.execute("SELECT title, status FROM matches WHERE id=?", (mid,)).fetchone()

    if not row:
        await m.answer("Матч не найден. Обнови список: «⚽ Активные матчи».")
        return

    title, status = row
    if status != "open":
        await m.answer("Этот матч уже закрыт. Обнови список: «⚽ Активные матчи».")
        return

    await m.answer(
        f"Матч #{mid}: {title}\n\nВыбери тип прогноза:",
        reply_markup=bet_type_kb(mid)
    )


@dp.callback_query(F.data.startswith("type:"))
async def choose_type(c: CallbackQuery):
    _, mid, t = c.data.split(":")
    mid = int(mid)

    if t == "1x2":
        await c.message.edit_text("Выбери исход 1X2:", reply_markup=kb_1x2(mid))
    elif t == "score":
        await c.message.edit_text("Выбери точный счёт:", reply_markup=kb_score(mid))
    elif t == "total":
        await c.message.edit_text("Выбери тотал:", reply_markup=kb_total(mid))
    elif t == "btts":
        await c.message.edit_text("Обе забьют?", reply_markup=kb_btts(mid))

    await c.answer()


@dp.callback_query(F.data.startswith("vote:"))
async def save_vote(c: CallbackQuery):
    _, mid, bet_type, choice = c.data.split(":")
    mid = int(mid)

    with db() as con:
        con.execute(
            "INSERT OR REPLACE INTO votes(match_id, user_id, username, bet_type, choice) VALUES (?,?,?,?,?)",
            (mid, c.from_user.id, c.from_user.username, bet_type, choice)
        )
        con.commit()

    await c.answer("Прогноз сохранён ✅", show_alert=True)


@dp.message(F.text == BTN_MY)
async def my_votes(m: Message):
    with db() as con:
        rows = con.execute("""
            SELECT v.match_id, m.title, v.bet_type, v.choice
            FROM votes v
            JOIN matches m ON m.id = v.match_id
            WHERE v.user_id=?
            ORDER BY v.match_id DESC
        """, (m.from_user.id,)).fetchall()

    if not rows:
        await m.answer("Ты ещё не делал прогнозов.", reply_markup=main_menu_kb(is_admin(m.from_user.id)))
        return

    text = "📊 Твои прогнозы:\n"
    for mid, title, bt, ch in rows[:30]:
        text += f"\n• #{mid} {title}\n  {bt} → {ch}"
    await m.answer(text, reply_markup=main_menu_kb(is_admin(m.from_user.id)))


@dp.message(F.text == BTN_LB)
async def leaderboard(m: Message):
    await m.answer("🏆 Лидерборд добавим следующим шагом (когда подключим очки).",
                   reply_markup=main_menu_kb(is_admin(m.from_user.id)))


@dp.message(F.text == BTN_HELP)
async def help_menu(m: Message):
    await m.answer(
        "ℹ️ Как пользоваться:\n\n"
        "1) Нажми «⚽ Активные матчи»\n"
        "2) Выбери матч кнопкой (🏟 #id ...)\n"
        "3) Выбери тип прогноза\n"
        "4) Выбери исход\n\n"
        "Команды админа:\n"
        "/newmatch <название>\n",
        reply_markup=main_menu_kb(is_admin(m.from_user.id))
    )


# ===== ADMIN =====
@dp.message(F.text == BTN_NEW)
async def newmatch_hint(m: Message):
    if not is_admin(m.from_user.id):
        return
    await m.answer("Создай матч командой:\n/newmatch Real vs Barca (02.02 20:00)")

@dp.message(Command("newmatch"))
async def newmatch_cmd(m: Message):
    if not is_admin(m.from_user.id):
        return
    title = m.text.replace("/newmatch", "").strip()
    if not title:
        await m.answer("Формат: /newmatch <название>")
        return

    with db() as con:
        con.execute("INSERT INTO matches(title, status) VALUES(?, 'open')", (title,))
        con.commit()

    await m.answer("Матч создан ✅\nНажми «⚽ Активные матчи», чтобы увидеть его в списке.")


# ===== MAIN =====
async def main():
    logging.basicConfig(level=logging.INFO)
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN не задан")
    if ADMIN_ID == 0:
        raise RuntimeError("ADMIN_ID не задан (число)")

    init_db()
    bot = Bot(TOKEN)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
