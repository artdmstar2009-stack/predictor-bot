from __future__ import annotations

import os
from html import escape
from typing import Any


def _safe(value: Any) -> str:
    return escape(str(value or "").strip())


def _bar(value: int, total: int, width: int = 10) -> str:
    if total <= 0:
        return "▱" * width
    filled = round((value / total) * width)
    filled = max(0, min(width, filled))
    return "▰" * filled + "▱" * (width - filled)


def _short(text: str, limit: int) -> str:
    text = str(text or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _apply_optional(bot, module_name: str) -> None:
    try:
        module = __import__(module_name)
        module.apply(bot)
    except Exception as exc:
        logger = getattr(bot, "logger", None)
        if logger:
            logger.exception("%s apply failed from theme: %s", module_name, exc)


def apply(bot) -> None:
    os.environ.setdefault("AI_LINE_HOME_ADV_FOOTBALL", "0")
    os.environ.setdefault("AI_LINE_MARGIN", "0.06")

    _apply_optional(bot, "polling_guard")
    _apply_optional(bot, "ai_line")
    _apply_optional(bot, "line_backtest")

    if getattr(bot, "_PRETTY_THEME_APPLIED", False):
        _apply_optional(bot, "mini_app")
        _apply_optional(bot, "mini_app_patch")
        _apply_optional(bot, "tennis_patch")
        return

    InlineKeyboardButton = bot.InlineKeyboardButton
    InlineKeyboardMarkup = bot.InlineKeyboardMarkup
    KeyboardButton = bot.KeyboardButton
    ReplyKeyboardMarkup = bot.ReplyKeyboardMarkup
    Message = bot.Message
    CallbackQuery = bot.CallbackQuery

    def main_menu() -> ReplyKeyboardMarkup:
        rows = [
            [KeyboardButton(text=bot.BTN_ACTIVE), KeyboardButton(text=bot.BTN_TODAY)],
            [KeyboardButton(text=bot.BTN_FIND_MATCH), KeyboardButton(text=bot.BTN_MY)],
            [KeyboardButton(text=bot.BTN_LB), KeyboardButton(text=bot.BTN_PROFILE)],
            [KeyboardButton(text=bot.BTN_HELP)],
        ]
        return ReplyKeyboardMarkup(
            keyboard=rows,
            resize_keyboard=True,
            input_field_placeholder="Матчи, прогнозы, профиль",
        )

    def ikb_sports(sports):
        rows = []
        for sport, cnt in sports:
            s = (sport or "other").lower()
            label = bot.SPORT_PRETTY.get(s, f"🏟 {s}")
            rows.append([InlineKeyboardButton(text=f"{label}  {cnt} матчей", callback_data=f"sport:{s}:0")])
        rows.append([InlineKeyboardButton(text="📋 Все активные матчи", callback_data="sport:all:0")])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    def ikb_matches_list(sport: str, page: int, items, total: int):
        rows = []
        for r in items:
            mid = int(r["id"])
            title = bot._pretty_title((r["title"] or ""), (r["sport"] or sport))
            st = bot._pretty_time((r["start_time_utc"] or r["start_time"] or ""))
            time_short = st.split("•")[-1].strip() if "•" in st else st
            text = _short(f"{time_short}  {title}", 60)
            rows.append([InlineKeyboardButton(text=text, callback_data=f"mopen:{mid}")])

        max_page = max(0, (total - 1) // bot.PER_PAGE)
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="‹ Назад", callback_data=f"sport:{sport}:{page-1}"))
        nav.append(InlineKeyboardButton(text=f"Стр. {page + 1}/{max_page + 1}", callback_data="theme:noop"))
        if page < max_page:
            nav.append(InlineKeyboardButton(text="Вперед ›", callback_data=f"sport:{sport}:{page+1}"))
        rows.append(nav)
        rows.append([InlineKeyboardButton(text="← Виды спорта", callback_data="back:sports")])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    def ikb_match_card(match_id: int):
        return InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="П1", callback_data=f"pick:{match_id}:1"),
                InlineKeyboardButton(text="X", callback_data=f"pick:{match_id}:X"),
                InlineKeyboardButton(text="П2", callback_data=f"pick:{match_id}:2"),
            ],
            [InlineKeyboardButton(text="📊 Статистика голосов", callback_data=f"stats:{match_id}")],
            [InlineKeyboardButton(text="← К списку матчей", callback_data="back:sports")],
        ])

    def _odds_line(match) -> str:
        odds_match = None
        try:
            odds_match = bot.ai_odds_for_match(dict(match))
        except Exception:
            odds_match = None
        if not odds_match:
            return ""

        parts = []
        if odds_match.get("odds_1"):
            parts.append(f"П1 <b>{float(odds_match['odds_1']):.2f}</b>")
        if odds_match.get("odds_x"):
            parts.append(f"X <b>{float(odds_match['odds_x']):.2f}</b>")
        if odds_match.get("odds_2"):
            parts.append(f"П2 <b>{float(odds_match['odds_2']):.2f}</b>")
        if not parts:
            return ""
        return "\n<b>AI-линия:</b> " + "   ".join(parts)

    async def show_match_card(target, match_id: int) -> None:
        match = bot.get_match(match_id)
        if not match:
            text = "Матч не найден."
            if isinstance(target, Message):
                await target.answer(text, reply_markup=main_menu())
            else:
                await target.message.answer(text, reply_markup=main_menu())
            return

        stats = bot.match_stats(match_id)
        total_votes = stats["1"] + stats["X"] + stats["2"]
        user_id = target.from_user.id if target.from_user else 0
        my_pick = bot.get_my_pick(user_id, match_id) if user_id else None
        allowed, why = bot.can_predict(match)
        dl = bot.deadline_for_match(match)

        title = bot._pretty_title((match["title"] or ""), (match["sport"] or "other"))
        league = _safe(match["league"] or "—")
        start = bot._pretty_time((match["start_time_utc"] or match["start_time"] or ""))
        deadline = bot._pretty_time(bot.iso(dl)) if dl else "—"
        status = "Открыто для прогноза" if allowed else _safe(why)

        def line(label: str, key: str) -> str:
            count = stats[key]
            pct = 0 if total_votes <= 0 else round((count / total_votes) * 100)
            return f"{label:<2} <code>{_bar(count, total_votes)}</code> <b>{pct}%</b> · {count}"

        text = (
            f"<b>{_safe(title)}</b>\n"
            f"<i>{league}</i>\n\n"
            f"🕒 Старт: <b>{_safe(start)}</b>\n"
            f"⏳ Дедлайн: <i>{_safe(deadline)}</i>\n"
            f"▫️ {status}{_odds_line(match)}\n\n"
            f"<b>Прогнозы игроков</b> · {total_votes}\n"
            f"{line('П1', '1')}\n"
            f"{line('X', 'X')}\n"
            f"{line('П2', '2')}\n\n"
            f"Твой выбор: <b>{_safe(my_pick or '—')}</b>"
        )

        if isinstance(target, Message):
            await target.answer(text, reply_markup=ikb_match_card(match_id))
        else:
            await target.message.answer(text, reply_markup=ikb_match_card(match_id))

    @bot.dp.callback_query(bot.F.data == "theme:noop")
    async def theme_noop(cb: CallbackQuery):
        await cb.answer()

    bot.main_menu = main_menu
    bot.ikb_sports = ikb_sports
    bot.ikb_matches_list = ikb_matches_list
    bot.ikb_match_card = ikb_match_card
    bot.show_match_card = show_match_card
    bot._PRETTY_THEME_APPLIED = True

    _apply_optional(bot, "mini_app")
    _apply_optional(bot, "mini_app_patch")
    _apply_optional(bot, "tennis_patch")
