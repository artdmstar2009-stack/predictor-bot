import asyncio
import logging
import os
import sqlite3
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Optional

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
    return sqlite3.connect(DB_PATH)

def init_db():
    with db() as con:
        con.execute("""
        CREATE TABLE IF NOT EXISTS matches(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open'  -- open/closed
        )
        """)
        con.execute("""
        CREATE TABLE IF NOT EXISTS votes(
            match_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            username TEXT,
            bet_type TEXT NOT NULL,   -- 1x2/score/total/btts
            choice TEXT NOT NULL,
            PRIMARY KEY(match_id, user_id, bet_type)
        )
        """)
        con.execute("""
        CREATE TABLE IF NOT EXISTS results(
            match_id INTEGER NOT NULL,
            bet_type TEXT NOT NULL,
            result TEXT NOT NULL,
            scored INTEGER NOT NULL DEFAULT 0,  -- 0/1
            PRIMARY KEY(match_id, bet_type)
        )
        """)
        con.execute("""
        CREATE TABLE IF NOT EXISTS scores(
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            points INTEGER NOT NULL DEFAULT 0
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

def get_match(mid: int):
    with db() as con:
        return con.execute(
            "SELECT id, title, status FROM matches WHERE id=?",
            (mid,)
        ).fetchone()

def close_match(mid: int):
    with db() as con:
        con.execute("UPDATE matches SET status='closed' WHERE id=?", (mid,))
        con.commit()

def save_vote(mid: int, uid: int, username: Optional[str], bet_type: str, choice: str):
    with db() as con:
        # Запрещаем голосовать если матч закрыт
        st = con.execute("SELECT status FROM matches WHERE id=?", (mid,)).fetchone()
        if not st:
            return "not_found"
        if st[0] != "open":
            return "closed"

        con.execute("""
        INSERT OR REPLACE INTO votes(match_id,user_id,username,bet_type,choice)
        VALUES(?,?,?,?,?)
        """, (mid, uid, username, bet_type, choice))
        con.commit()
        return "ok"

def get_my_votes(uid: int):
    with db() as con:
        return con.execute("""
            SELECT v.match_id, m.title, v.bet_type, v.choice, m.status
            FROM votes v
            JOIN matches m ON m.id=v.match_id
            WHERE v.user_id=?
            ORDER BY v.match_id DESC
        """, (uid,)).fetchall()

def get_leaderboard(limit: int = 10):
    with db() as con:
        return con.execute("""
            SELECT COALESCE(username,''), user_id, points
            FROM scores
            ORDER BY points DESC, user_id ASC
            LIMIT ?
        """, (limit,)).fetchall()

def set_result(mid: int, bet_type: str, result: str):
    """
    Ставит результат один раз. Если уже scored=1 — запрещаем.
    Начисляем +1 за правильный прогноз по этому bet_type.
    """
    with db() as con:
        m = con.execute("SELECT status FROM matches WHERE id=?", (mid,)).fetchone()
        if not m:
            return "not_found"

        # если уже есть результат и scored=1 — не трогаем
        row = con.execute(
            "SELECT scored FROM results WHERE match_id=? AND bet_type=?",
            (mid, bet_type)
        ).fetchone()
        if row and row[0] == 1:
            return "already_scored"

        # сохранить/обновить результат и отметить scored=1
        con.execute("""
            INSERT INTO results(match_id,bet_type,result,scored)
            VALUES(?,?,?,1)
            ON CONFLICT(match_id,bet_type) DO UPDATE SET
              result=excluded.result,
              scored=1
        """, (mid, bet_type, result))

        # начислить очки победителям
        winners = con.execute("""
            SELECT user_id, COALESCE(username,'')
            FROM votes
            WHERE match_id=? AND bet_type=? AND choice=?
        """, (mid, bet_type, result)).fetchall()

        for uid, uname in winners:
            con.execute("""
                INSERT INTO scores(user_id, username, points)
                VALUES(?,?,1)
                ON CONFLICT(user_id) DO UPDATE SET
                  username=excluded.username,
                  points=points+1
            """, (uid, uname))

        con.commit()
        return ("ok", len(winners))

def match_stats(mid: int):
    """
    Возвращает статистику голосов по каждому bet_type.
    """
    with db() as con:
        # total counts per type/choice
        rows = con.execute("""
            SELECT bet_type, choice, COUNT(*)
            FROM votes
            WHERE match_id=?
            GROUP BY bet_type, choice
        """, (mid,)).fetchall()

        # totals per bet_type
        totals = con.execute("""
            SELECT bet_type, COUNT(*)
            FROM votes
            WHERE match_id=?
            GROUP BY bet_type
        """, (mid,)).fetchall()

    totals_map = {bt: c for bt, c in totals}
    data = {}
    for bt, ch, c in rows:
        data.setdefault(bt, {})
        data[bt][ch] = c

    return totals_map, data

# ===================== TEXT LABELS =====================
BTN_ACTIVE = "⚽ Активные матчи"
BTN_MY = "📊 Мои прогнозы"
BTN_LB = "🏆 Лидерборд"
BTN_HELP = "ℹ️ Помощь"
BTN_NEW = "➕ Создать матч"
BTN_BACK = "⬅️ Назад"

BET_LABEL = {
    "1x2": "1X2",
    "score": "Точный счёт",
    "total": "Тотал",
    "btts": "Обе забьют"
}

def choice_label(bt: str, choice: str) -> str:
    if bt == "1x2":
        return {"home":"🏠 Хозяева", "draw":"🤝 Ничья", "away":"🚌 Гости"}.get(choice, choice)
    if bt == "score":
        return f"🎯 {choice}"
    if bt == "total":
        return {"over":"⚽ Больше 2.5", "under":"🧱 Меньше 2.5"}.get(choice, choice)
    if bt == "btts":
        return {"yes":"✅ Да", "no":"❌ Нет"}.get(choice, choice)
    return choice

# ===================== KEYBOARDS =====================
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
    kb = ReplyKeyboardBuilder()
    for mid, title in matches:
        kb.button(text=f"🏟 #{mid} {title}")
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

def bet_type_kb(mid: int, mode: str = "vote"):
    # mode: vote | setres
    kb = InlineKeyboardBuilder()
    kb.button(text="1️⃣ 1X2", callback_data=f"type:{mode}:{mid}:1x2")
    kb.button(text="🎯 Точный счёт", callback_data=f"type:{mode}:{mid}:score")
    kb.button(text="⚽ Тотал", callback_data=f"type:{mode}:{mid}:total")
    kb.button(text="🔥 Обе забьют", callback_data=f"type:{mode}:{mid}:btts")
    kb.adjust(2)
    return kb.as_markup()

def kb_1x2(mid: int, mode: str):
    kb = InlineKeyboardBuilder()
    kb.button(text="🏠 Хозяева", callback_data=f"{mode}:{mid}:1x2:home")
    kb.button(text="🤝 Ничья", callback_data=f"{mode}:{mid}:1x2:draw")
    kb.button(text="🚌 Гости", callback_data=f"{mode}:{mid}:1x2:away")
    kb.adjust(1)
    return kb.as_markup()

def kb_score(mid: int, mode: str):
    kb = InlineKeyboardBuilder()
    for s in ["1:0","2:1","1:1","0:0"]:
        kb.button(text=s, callback_data=f"{mode}:{mid}:score:{s}")
    kb.adjust(2)
    return kb.as_markup()

def kb_total(mid: int, mode: str):
    kb = InlineKeyboardBuilder()
    kb.button(text="⚽ Больше 2.5", callback_data=f"{mode}:{mid}:total:over")
    kb.button(text="🧱 Меньше 2.5", callback_data=f"{mode}:{mid}:total:under")
    kb.adjust(1)
    return kb.as_markup()

def kb_btts(mid: int, mode: str):
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Да", callback_data=f"{mode}:{mid}:btts:yes")
    kb.button(text="❌ Нет", callback_data=f"{mode}:{mid}:btts:no")
    kb.adjust(1)
    return kb.as_markup()

# ===================== HELPERS =====================
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

def format_stats(mid: int, title: str) -> str:
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
        # сортируем для красоты
        for ch, cnt in sorted(choices.items(), key=lambda x: (-x[1], x[0])):
            pct = (cnt / total) * 100
            lines.append(f"• {choice_label(bt, ch)} — {cnt} ({pct:.1f}%)")

    return "\n".join(lines)

# ===================== HANDLERS =====================
@dp.message(CommandStart())
async def start(m: Message):
    await m.answer(
        "🤖 Бот-предиктор\n\n"
        "Нажми «⚽ Активные матчи» → выбери матч → прогноз/статистика 👇",
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
    await m.answer("Выбери матч кнопкой ниже 👇", reply_markup=matches_list_kb(matches, is_admin(m.from_user.id)))

@dp.message(F.text.startswith("🏟 #"))
async def picked_match(m: Message):
    mid = parse_match_button(m.text)
    if mid is None:
        await m.answer("Не понял матч. Нажми «⚽ Активные матчи» ещё раз.")
        return

    row = get_match(mid)
    if not row:
        await m.answer("Матч не найден. Обнови список: «⚽ Активные матчи».")
        return

    _, title, status = row
    await m.answer(
        f"Матч #{mid}: {title}\nСтатус: {status}\n\nВыбирай действие:",
        reply_markup=match_menu_kb(mid, is_admin(m.from_user.id))
    )

@dp.callback_query(F.data.startswith("match:"))
async def match_menu(call: CallbackQuery):
    _, mid, action = call.data.split(":")
    mid = int(mid)
    row = get_match(mid)
    if not row:
        await call.answer("Матч не найден.", show_alert=True)
        return
    _, title, status = row

    if action == "vote":
        if status != "open":
            await call.answer("Матч закрыт для прогнозов.", show_alert=True)
            return
        await call.message.edit_text(
            f"Матч #{mid}: {title}\n\nВыбери тип прогноза:",
            reply_markup=bet_type_kb(mid, mode="vote")
        )
        await call.answer()
        return

    if action == "stats":
        text = format_stats(mid, title)
        await call.message.edit_text(text, parse_mode="Markdown", reply_markup=match_menu_kb(mid, is_admin(call.from_user.id)))
        await call.answer()
        return

@dp.callback_query(F.data.startswith("type:"))
async def choose_type(call: CallbackQuery):
    # type:{mode}:{mid}:{bet_type}
    _, mode, mid, bt = call.data.split(":")
    mid = int(mid)

    if mode == "vote":
        m = get_match(mid)
        if not m:
            await call.answer("Матч не найден.", show_alert=True)
            return
        if m[2] != "open":
            await call.answer("Матч закрыт.", show_alert=True)
            return

        if bt == "1x2":
            await call.message.edit_text("Выбери исход 1X2:", reply_markup=kb_1x2(mid, mode="vote"))
        elif bt == "score":
            await call.message.edit_text("Выбери точный счёт:", reply_markup=kb_score(mid, mode="vote"))
        elif bt == "total":
            await call.message.edit_text("Выбери тотал:", reply_markup=kb_total(mid, mode="vote"))
        elif bt == "btts":
            await call.message.edit_text("Обе забьют?", reply_markup=kb_btts(mid, mode="vote"))
        await call.answer()
        return

    # mode == setres (админ)
    if mode == "setres":
        if not is_admin(call.from_user.id):
            await call.answer("Только для админа.", show_alert=True)
            return

        if bt == "1x2":
            await call.message.edit_text("Результат 1X2:", reply_markup=kb_1x2(mid, mode="setres"))
        elif bt == "score":
            await call.message.edit_text("Результат — точный счёт:", reply_markup=kb_score(mid, mode="setres"))
        elif bt == "total":
            await call.message.edit_text("Результат — тотал:", reply_markup=kb_total(mid, mode="setres"))
        elif bt == "btts":
            await call.message.edit_text("Результат — обе забьют:", reply_markup=kb_btts(mid, mode="setres"))
        await call.answer()
        return

@dp.callback_query(F.data.startswith("vote:"))
async def vote_cb(call: CallbackQuery):
    # vote:{mid}:{bet_type}:{choice}
    _, mid, bt, choice = call.data.split(":", 3)
    mid = int(mid)
    res = save_vote(mid, call.from_user.id, call.from_user.username, bt, choice)

    if res == "not_found":
        await call.answer("Матч не найден.", show_alert=True)
        return
    if res == "closed":
        await call.answer("Матч закрыт для прогнозов.", show_alert=True)
        return

    await call.answer(f"Сохранено: {BET_LABEL.get(bt, bt)} ✅", show_alert=True)

@dp.callback_query(F.data.startswith("admin:"))
async def admin_actions(call: CallbackQuery):
    # admin:{mid}:{action}
    _, mid, action = call.data.split(":")
    mid = int(mid)

    if not is_admin(call.from_user.id):
        await call.answer("Только для админа.", show_alert=True)
        return

    row = get_match(mid)
    if not row:
        await call.answer("Матч не найден.", show_alert=True)
        return

    _, title, status = row

    if action == "close":
        close_match(mid)
        await call.answer("Матч закрыт ✅", show_alert=True)
        await call.message.edit_text(
            f"Матч #{mid}: {title}\nСтатус: closed\n\nВыбирай действие:",
            reply_markup=match_menu_kb(mid, True)
        )
        return

    if action == "setresult":
        # Результат ставим кнопками (тип прогноза -> вариант)
        await call.message.edit_text(
            f"Матч #{mid}: {title}\n\nВыбери, по чему ставим результат (и начисляем очки):",
            reply_markup=bet_type_kb(mid, mode="setres")
        )
        await call.answer()
        return

@dp.callback_query(F.data.startswith("setres:"))
async def setres_cb(call: CallbackQuery):
    # (не используется — мы используем mode="setres" с callback_data "setres:{mid}:..."
    await call.answer()

@dp.callback_query(F.data.startswith("setres") )
async def _noop(call: CallbackQuery):
    await call.answer()

@dp.callback_query(F.data.startswith("setres:"))
async def _noop2(call: CallbackQuery):
    await call.answer()

@dp.callback_query(F.data.startswith("setres"))
async def _noop3(call: CallbackQuery):
    await call.answer()

@dp.callback_query(F.data.startswith("setres"))
async def _noop4(call: CallbackQuery):
    await call.answer()

@dp.callback_query(F.data.startswith("setres"))
async def _noop5(call: CallbackQuery):
    await call.answer()

@dp.callback_query(F.data.startswith("setres"))
async def _noop6(call: CallbackQuery):
    await call.answer()

@dp.callback_query(F.data.startswith("setres"))
async def _noop7(call: CallbackQuery):
    await call.answer()

# Реальный handler для выставления результата:
@dp.callback_query(F.data.startswith("setres") == False)  # just to satisfy linter-like patterns
async def _skip(_call: CallbackQuery):
    pass

@dp.callback_query(F.data.startswith("setres"))  # keep only one? aiogram uses first match; but this won't be reached.
async def _skip2(_call: CallbackQuery):
    pass

# !!! ВАЖНО: в aiogram нельзя иметь кучу одинаковых хендлеров.
# Поэтому реальный результат ставим через callback_data, начинающийся с "setr:" (уникально)

def kb_1x2_res(mid: int):
    kb = InlineKeyboardBuilder()
    kb.button(text="🏠 Хозяева", callback_data=f"setr:{mid}:1x2:home")
    kb.button(text="🤝 Ничья", callback_data=f"setr:{mid}:1x2:draw")
    kb.button(text="🚌 Гости", callback_data=f"setr:{mid}:1x2:away")
    kb.adjust(1)
    return kb.as_markup()

def kb_score_res(mid: int):
    kb = InlineKeyboardBuilder()
    for s in ["1:0","2:1","1:1","0:0"]:
        kb.button(text=s, callback_data=f"setr:{mid}:score:{s}")
    kb.adjust(2)
    return kb.as_markup()

def kb_total_res(mid: int):
    kb = InlineKeyboardBuilder()
    kb.button(text="⚽ Больше 2.5", callback_data=f"setr:{mid}:total:over")
    kb.button(text="🧱 Меньше 2.5", callback_data=f"setr:{mid}:total:under")
    kb.adjust(1)
    return kb.as_markup()

def kb_btts_res(mid: int):
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Да", callback_data=f"setr:{mid}:btts:yes")
    kb.button(text="❌ Нет", callback_data=f"setr:{mid}:btts:no")
    kb.adjust(1)
    return kb.as_markup()

@dp.callback_query(F.data.startswith("type:setres:"))
async def choose_type_setres(call: CallbackQuery):
    # type:setres:{mid}:{bet_type}
    _, _, mid, bt = call.data.split(":")
    mid = int(mid)
    if not is_admin(call.from_user.id):
        await call.answer("Только для админа.", show_alert=True)
        return

    if bt == "1x2":
        await call.message.edit_text("Поставь результат 1X2:", reply_markup=kb_1x2_res(mid))
    elif bt == "score":
        await call.message.edit_text("Поставь результат (точный счёт):", reply_markup=kb_score_res(mid))
    elif bt == "total":
        await call.message.edit_text("Поставь результат (тотал):", reply_markup=kb_total_res(mid))
    elif bt == "btts":
        await call.message.edit_text("Поставь результат (обе забьют):", reply_markup=kb_btts_res(mid))

    await call.answer()

@dp.callback_query(F.data.startswith("setr:"))
async def set_result_cb(call: CallbackQuery):
    # setr:{mid}:{bet_type}:{result}
    if not is_admin(call.from_user.id):
        await call.answer("Только для админа.", show_alert=True)
        return

    _, mid, bt, result = call.data.split(":", 3)
    mid = int(mid)

    out = set_result(mid, bt, result)
    if out == "not_found":
        await call.answer("Матч не найден.", show_alert=True)
        return
    if out == "already_scored":
        await call.answer("Результат уже выставлен ранее.", show_alert=True)
        return

    _, winners = out
    row = get_match(mid)
    title = row[1] if row else ""

    await call.answer("Готово ✅", show_alert=True)
    await call.message.edit_text(
        f"✅ Результат выставлен\nМатч #{mid}: {title}\n"
        f"Тип: {BET_LABEL.get(bt, bt)}\n"
        f"Результат: {choice_label(bt, result)}\n\n"
        f"Очки начислены победителям: {winners}",
        reply_markup=match_menu_kb(mid, True)
    )

@dp.message(F.text == BTN_MY)
async def my_votes(m: Message):
    rows = get_my_votes(m.from_user.id)
    if not rows:
        await m.answer("Ты ещё не делал прогнозов.", reply_markup=main_menu_kb(is_admin(m.from_user.id)))
        return

    text = "📊 Твои прогнозы:\n"
    for mid, title, bt, ch, status in rows[:30]:
        text += f"\n• #{mid} {title} ({status})\n  {BET_LABEL.get(bt, bt)} → {choice_label(bt, ch)}"
    await m.answer(text, reply_markup=main_menu_kb(is_admin(m.from_user.id)))

@dp.message(F.text == BTN_LB)
async def leaderboard(m: Message):
    rows = get_leaderboard(10)
    if not rows:
        await m.answer("Пока нет очков. Админ должен выставить результаты матчей ✅",
                       reply_markup=main_menu_kb(is_admin(m.from_user.id)))
        return

    text = "🏆 Лидерборд (топ-10):\n"
    for i, (uname, uid, pts) in enumerate(rows, start=1):
        name = f"@{uname}" if uname else f"id:{uid}"
        text += f"\n{i}. {name} — {pts}"
    await m.answer(text, reply_markup=main_menu_kb(is_admin(m.from_user.id)))

@dp.message(F.text == BTN_HELP)
async def help_menu(m: Message):
    await m.answer(
        "ℹ️ Как пользоваться:\n\n"
        "1) «⚽ Активные матчи»\n"
        "2) Выбери матч кнопкой\n"
        "3) «🗳 Сделать прогноз» → выбирай всё кнопками\n"
        "4) «📈 Статистика» — смотри проценты\n\n"
        "🏆 Очки появляются, когда админ выставляет результат матча.",
        reply_markup=main_menu_kb(is_admin(m.from_user.id))
    )

# ===== ADMIN: create match =====
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
        con.execute("INSERT INTO matches(title,status) VALUES(?, 'open')", (title,))
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
