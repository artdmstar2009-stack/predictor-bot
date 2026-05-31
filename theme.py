from __future__ import annotations

from html import escape
from typing import Any


def _safe(value: Any) -> str:
    return escape(str(value or "").strip())


def _bar(value: int, total: int, width: int = 8) -> str:
    if total <= 0:
        return "░" * width
    filled = round((value / total) * width)
    filled = max(0, min(width, filled))
    return "▓" * filled + "░" * (width - filled)


def apply(bot) -> None:
    if getattr(bot, "_PRETTY_THEME_APPLIED", False):
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
            input_field_placeholder="Выбери раздел",
        )

    def ikb_sports(sports):
        rows = []
        for sport, cnt in sports:
            s = (sport or "other").lower()
            label = bot.SPORT_PRETTY.get(s, f"🏟 {s}")
            rows.append([InlineKeyboardButton(text=f"{label} · {cnt}", callback_data=f"sport:{s}:0")])
        rows.append([InlineKeyboardButton(text="📋 Все матчи", callback_data="sport:all:0")])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    def ikb_matches_list(sport: str, page: int, items, total: int):
        rows = []
        for r in items:
            mid = int(r["id"])
            title = bot._pretty_title((r["title"] or ""), (r["sport"] or sport))
            st = bot._pretty_time((r["start_time_utc"] or r["start_time"] or ""))
            time_short = st.split("•")[-1].strip() if "•" in st else st
            text = f"{time_short} · {title}"
            if len(text) > 58:
                text = text[:57] + "…"
            rows.append([InlineKeyboardButton(text=text, callback_data=f"mopen:{mid}")])

        max_page = max(0, (total - 1) // bot.PER_PAGE)
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="‹", callback_data=f"sport:{sport}:{page-1}"))
        nav.append(InlineKeyboardButton(text=f"{page + 1}/{max_page + 1}", callback_data="theme:noop"))
        if page < max_page:
            nav.append(InlineKeyboardButton(text="›", callback_data=f"sport:{sport}:{page+1}"))
        rows.append(nav)
        rows.append([InlineKeyboardButton(text="← Виды спорта", callback_data="back:sports")])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    def ikb_match_card(match_id: int):
        return InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="🏠 1", callback_data=f"pick:{match_id}:1"),
                InlineKeyboardButton(text="🤝 X", callback_data=f"pick:{match_id}:X"),
                InlineKeyboardButton(text="🚌 2", callback_data=f"pick:{match_id}:2"),
            ],
            [InlineKeyboardButton(text="📊 Голоса", callback_data=f"stats:{match_id}")],
            [InlineKeyboardButton(text="← К категориям", callback_data="back:sports")],
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
            parts.append(f"1 <b>{float(odds_match['odds_1']):.2f}</b>")
        if odds_match.get("odds_x"):
            parts.append(f"X <b>{float(odds_match['odds_x']):.2f}</b>")
        if odds_match.get("odds_2"):
            parts.append(f"2 <b>{float(odds_match['odds_2']):.2f}</b>")
        if not parts:
            return ""
        return "\n🎯 " + "   ".join(parts)

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
        status = "🟢 прогнозы открыты" if allowed else f"🔒 {_safe(why)}"

        def line(label: str, key: str) -> str:
            count = stats[key]
            pct = 0 if total_votes <= 0 else round((count / total_votes) * 100)
            return f"{label}  <code>{_bar(count, total_votes)}</code>  <b>{pct}%</b> · {count}"

        text = (
            f"<b>{_safe(title)}</b>\n"
            f"🏆 {league}\n"
            f"🕒 <b>{_safe(start)}</b>\n"
            f"⏳ дедлайн: <i>{_safe(deadline)}</i>\n"
            f"{status}{_odds_line(match)}\n\n"
            f"<b>Прогнозы</b> · всего {total_votes}\n"
            f"{line('🏠 1', '1')}\n"
            f"{line('🤝 X', 'X')}\n"
            f"{line('🚌 2', '2')}\n\n"
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
