from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from html import escape
from typing import Any

VERSION = "DAY_CYCLE_PATCH_V1"
PICKS = ("1", "X", "2")

DAY_CYCLE_CSS = r"""
    /* DAY_CYCLE_PATCH_V1 */
    .match.finished {
      border-color:rgba(110,231,183,.38);
      background:linear-gradient(180deg, rgba(110,231,183,.13), rgba(255,255,255,.035));
    }
    .match.finished::before { background:linear-gradient(180deg, var(--good, #6be49d), var(--accent2, #f4c430)); }
    .match.finished.user-won { border-color:rgba(110,231,183,.58); }
    .match.finished.user-lost { border-color:rgba(255,114,114,.42); }
    .result-strip {
      display:flex;
      justify-content:space-between;
      align-items:center;
      gap:10px;
      padding:9px 10px;
      border:1px solid rgba(110,231,183,.28);
      border-radius:8px;
      background:rgba(110,231,183,.09);
      color:var(--muted);
      font-size:12px;
    }
    .result-strip strong { color:var(--good, #6be49d); font-size:13px; text-align:right; }
    .result-strip.draw strong { color:var(--warn, #ffd166); }
    .result-strip.user-lost { border-color:rgba(255,114,114,.25); background:rgba(255,114,114,.07); }
    .result-strip.user-lost strong { color:var(--bad, #ff7272); }
    .pick-cta.result-winner {
      border-color:rgba(110,231,183,.72);
      background:linear-gradient(180deg, rgba(110,231,183,.30), rgba(110,231,183,.13));
      box-shadow:0 12px 26px rgba(110,231,183,.12);
    }
    .pick-cta.result-winner span, .pick-cta.result-winner b { color:var(--good, #6be49d); }
    .pick-cta.result-loser { opacity:.58; }
    .pick-cta.result-draw {
      border-color:rgba(255,209,102,.62);
      background:rgba(255,209,102,.13);
    }
    .pick-cta.result-draw span, .pick-cta.result-draw b { color:var(--warn, #ffd166); }
    .day-note {
      margin:0 10px 10px;
      padding:9px 10px;
      border:1px solid rgba(255,255,255,.08);
      border-radius:8px;
      background:rgba(255,255,255,.035);
      color:var(--muted);
      font-size:11px;
    }
"""

DAY_CYCLE_JS = r"""
const dayEsc = value => String(value ?? '').replace(/[&<>'"]/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[ch]));
const dayPickLabel = p => (typeof pickLabel === 'function' ? pickLabel(p) : ({'1':'П1','X':'X','2':'П2'}[p] || p));
const dayFmtOdd = v => (typeof fmtOdd === 'function' ? fmtOdd(v) : (v == null ? '—' : Number(v).toFixed(2)));
const dayFmtPct = v => (typeof fmtPct === 'function' ? fmtPct(v) : (v == null ? '—' : `${Math.round(v*1000)/10}%`));
const dayMoney = value => (typeof beautyMoney === 'function' ? beautyMoney(value) : Number(value || 0).toLocaleString('ru-RU'));
const daySportIcon = sport => (typeof beautySportIcon === 'function' ? beautySportIcon(sport) : ({football:'⚽', hockey:'🏒', nhl:'🏒', tennis:'🎾'}[(sport || '').toLowerCase()] || '•'));
const daySportName = sport => (typeof beautySportName === 'function' ? beautySportName(sport) : (typeof sportLabel === 'function' ? sportLabel(sport) : (sport || 'Матчи')));
const dayPicks = m => (m?.available_picks && m.available_picks.length ? m.available_picks : ['1','X','2']);
function dayBestPick(m){
  const picks = dayPicks(m);
  let best = picks[0], score = -1;
  picks.forEach(p => { const v = Number(m?.probabilities?.[p] || 0); if(v > score){ score = v; best = p; } });
  return {pick:best, score};
}
function dayPickButtons(m){
  const picks = dayPicks(m);
  return picks.map(p => {
    const finished = Boolean(m.is_finished);
    const isWinner = finished && m.winner_pick === p;
    const classes = ['pick-cta'];
    if(m.my_pick === p) classes.push('picked');
    if(finished) classes.push(isWinner ? (p === 'X' ? 'result-draw' : 'result-winner') : 'result-loser');
    const disabled = finished || !m.can_predict || !picks.includes(p);
    const odd = m.odds?.[p];
    const label = finished && isWinner ? 'Победил' : dayPickLabel(p);
    return `<button class="${classes.join(' ')}" data-pick="${p}" data-id="${m.id}" ${disabled?'disabled':''}><span>${label}</span><b>${dayPickLabel(p)} ${dayFmtOdd(odd)}</b></button>`;
  }).join('');
}
function dayResultStrip(m){
  if(!m.is_finished) return '';
  const cls = m.result_state === 'lost' ? 'user-lost' : (m.winner_pick === 'X' ? 'draw' : '');
  const state = m.result_state === 'won' ? ' · твоя ставка выиграла' : (m.result_state === 'lost' ? ' · твоя ставка проиграла' : '');
  return `<div class="result-strip ${cls}"><span>Итог: <b>${dayPickLabel(m.winner_pick)}</b>${state}</span><strong>${dayEsc(m.winner_name || m.winner_label || 'Матч завершен')}</strong></div>`;
}
function renderMatches(items){
  try { lastMatches = items || []; } catch(_e) {}
  const el = document.getElementById('matches');
  const count = document.getElementById('matchCount');
  const done = (items || []).filter(x => x.is_finished).length;
  const open = (items || []).length - done;
  if(count) count.textContent = `${open} активных · ${done} итогов`;
  if(!items || !items.length){ el.innerHTML = '<div class="empty">На сегодня матчей нет. После ночного обновления появится новая линия.</div>'; return; }
  const note = done ? '<div class="day-note">Завершенные матчи остаются здесь до конца дня, победитель подсвечен зеленым. После 00:10 МСК они уйдут из витрины.</div>' : '';
  el.innerHTML = note + items.map(m => {
    const picks = dayPicks(m);
    const best = dayBestPick(m);
    const finished = Boolean(m.is_finished);
    const ticket = finished
      ? (m.my_pick ? `Твой прогноз: <b>${dayPickLabel(m.my_pick)}</b>` : 'Матч завершен')
      : (m.my_pick ? `Ставка: <b>${dayPickLabel(m.my_pick)} · ${dayMoney(m.my_stake)} ${m.my_odds ? dayFmtOdd(m.my_odds) : ''}</b>` : (m.can_predict ? 'Ставки открыты' : dayEsc(m.blocked_reason || 'Ставки закрыты')));
    const classes = ['match', 'beauty-match'];
    if(m.my_pick) classes.push('has-pick');
    if(finished) classes.push('finished');
    if(m.result_state === 'won') classes.push('user-won');
    if(m.result_state === 'lost') classes.push('user-lost');
    return `<article class="${classes.join(' ')}">
      <div class="match-head">
        <span class="sport-pill">${daySportIcon(m.sport)} ${dayEsc(daySportName(m.sport))}</span>
        <span class="time-pill">${dayEsc(m.display_time || '—')}</span>
      </div>
      <div class="match-title">${dayEsc(m.title || 'Матч')}</div>
      <div class="match-meta"><span>${dayEsc(m.league || '—')}</span><span>Голоса ${Number(m.votes?.total || 0)}</span><span>${finished ? 'Результат зафиксирован' : `AI пик ${dayPickLabel(best.pick)} ${dayFmtPct(best.score)}`}</span></div>
      ${dayResultStrip(m)}
      <div class="ai-strip"><span>${finished ? 'Линия закрыта' : 'AI-линия'}</span><b>${picks.map(p => `${dayPickLabel(p)} ${dayFmtPct(m.probabilities?.[p])}`).join(' · ')}</b></div>
      <div class="pick-row ${picks.length === 2 ? 'two' : ''}">${dayPickButtons(m)}</div>
      <div class="ticket-line"><span>${ticket}</span>${finished ? `<span class="status-pill">Итог</span>` : '<span class="status-pill">Live</span>'}</div>
    </article>`;
  }).join('');
  el.querySelectorAll('button[data-pick]:not(:disabled)').forEach(b=>b.onclick=()=>{
    if(typeof openBetModal === 'function') openBetModal(Number(b.dataset.id), b.dataset.pick);
    else sendPick(Number(b.dataset.id), b.dataset.pick);
  });
}
function renderMine(items){
  const el = document.getElementById('myPredictions');
  if(!initData){ el.innerHTML = '<div class="empty">Открой Mini App через Telegram</div>'; return; }
  if(!items || !items.length){ el.innerHTML = '<div class="empty">Прогнозов пока нет</div>'; return; }
  el.innerHTML = items.map(x=>{
    const done = x.result === '1' || x.result === 'X' || x.result === '2';
    const won = done && x.pick === x.result;
    const cls = done ? (won ? 'user-won' : 'user-lost') : '';
    const result = done ? ` → ${dayPickLabel(x.result)}` : '';
    return `<div class="row ${cls}"><span>${dayEsc(x.title)}<br><span class="muted">${daySportIcon(x.sport)} ${dayEsc(x.league || '—')} · ${dayEsc(x.status || '')}</span></span><b>${dayPickLabel(x.pick)}${x.stake ? ` · ${dayMoney(x.stake)} ${x.odds ? dayFmtOdd(x.odds) : ''}` : ''}${result}</b></div>`;
  }).join('');
}
"""


def _row_value(row: Any, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    if isinstance(row, dict):
        return row.get(key, default)
    try:
        return row[key]
    except Exception:
        return default


def _parse_dt(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _day_bounds(app: Any, now: datetime | None = None) -> tuple[datetime, datetime]:
    zone = getattr(app, "DISPLAY_ZONE", timezone.utc)
    current = (now or app.now_utc()).astimezone(zone)
    start_local = current.replace(hour=0, minute=0, second=0, microsecond=0)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def _visible_day_where() -> str:
    return """
    (
      (status='open' AND (COALESCE(start_time_utc, start_time) IS NULL OR COALESCE(start_time_utc, start_time) >= ?))
      OR
      (status='closed' AND result IN ('1','X','2') AND COALESCE(start_time_utc, start_time) >= ? AND COALESCE(start_time_utc, start_time) < ?)
    )
    """


def _active_matches(bot: Any, mini_app: Any, sport: str = "all", limit: int = 80, query: str = "", user_id: int | None = None) -> list[dict[str, Any]]:
    sport = (sport or "all").lower()
    limit = max(1, min(int(limit or 80), 200))
    query = (query or "").strip().lower()
    day_start, day_end = _day_bounds(bot)
    where = [_visible_day_where()]
    params: list[Any] = [bot.iso(day_start), bot.iso(day_start), bot.iso(day_end)]
    if sport != "all":
        where.append("COALESCE(NULLIF(LOWER(sport), ''), 'other')=?")
        params.append(sport)
    if query:
        where.append("LOWER(COALESCE(title,'') || ' ' || COALESCE(league,'')) LIKE ?")
        params.append(f"%{query}%")
    params.append(limit)
    with bot.db() as con:
        rows = con.execute(
            f"""
            SELECT * FROM matches
            WHERE {' AND '.join(where)}
            ORDER BY
              CASE WHEN status='closed' THEN 0 ELSE 1 END,
              CASE WHEN status='closed' THEN COALESCE(start_time_utc, start_time) END DESC,
              COALESCE(start_time_utc, start_time) ASC
            LIMIT ?
            """,
            params,
        ).fetchall()
    return [mini_app._row_to_match(bot, row, user_id) for row in rows]


def _visible_sports(bot: Any) -> list[tuple[str, int]]:
    day_start, day_end = _day_bounds(bot)
    with bot.db() as con:
        rows = con.execute(
            f"""
            SELECT COALESCE(NULLIF(LOWER(sport), ''), 'other') AS sport, COUNT(*) AS c
            FROM matches
            WHERE {_visible_day_where()}
            GROUP BY COALESCE(NULLIF(LOWER(sport), ''), 'other')
            ORDER BY c DESC
            """,
            (bot.iso(day_start), bot.iso(day_start), bot.iso(day_end)),
        ).fetchall()
    return [(row["sport"], int(row["c"] or 0)) for row in rows]


def _winner_name(app: Any, row: Any, result: str) -> str:
    teams = None
    parser = getattr(app, "_parse_title_teams", None)
    if callable(parser):
        try:
            teams = parser(str(_row_value(row, "title", "") or ""))
        except Exception:
            teams = None
    if result == "1":
        return str(teams[0] if teams else "П1")
    if result == "2":
        return str(teams[1] if teams else "П2")
    if result == "X":
        return "Ничья"
    return ""


def _patch_row_to_match(app: Any, mini_app: Any) -> None:
    original = getattr(mini_app, "_row_to_match", None)
    if not callable(original) or getattr(original, "_day_cycle_wrapped", False):
        return

    def patched_row_to_match(bot: Any, row: Any, user_id: int | None = None) -> dict[str, Any]:
        payload = dict(original(bot, row, user_id) or {})
        result = str(_row_value(row, "result", "") or "").upper()
        status = str(_row_value(row, "status", "") or "").lower()
        is_finished = status == "closed" and result in PICKS
        payload["is_finished"] = is_finished
        payload["winner_pick"] = result if is_finished else ""
        payload["winner_label"] = {"1": "П1", "X": "X", "2": "П2"}.get(result, "")
        payload["winner_name"] = _winner_name(bot, row, result) if is_finished else ""
        if is_finished:
            payload["can_predict"] = False
            payload["blocked_reason"] = "Матч завершен."
            my_pick = str(payload.get("my_pick") or "").upper()
            if my_pick in PICKS:
                payload["result_state"] = "won" if my_pick == result else "lost"
            else:
                payload["result_state"] = "finished"
        else:
            payload.setdefault("result_state", "open")
        return payload

    patched_row_to_match._day_cycle_wrapped = True
    mini_app._row_to_match = patched_row_to_match


def _patch_active_matches(app: Any, mini_app: Any) -> None:
    original = getattr(mini_app, "_active_matches", None)
    if not callable(original) or getattr(original, "_day_cycle_wrapped", False):
        return

    def patched_active_matches(bot: Any, sport: str = "all", limit: int = 80, query: str = "", user_id: int | None = None) -> list[dict[str, Any]]:
        return _active_matches(bot, mini_app, sport, limit, query, user_id)

    patched_active_matches._day_cycle_wrapped = True
    mini_app._active_matches = patched_active_matches


def _patch_summary(app: Any, mini_app: Any) -> None:
    original = getattr(mini_app, "_summary", None)
    if not callable(original) or getattr(original, "_day_cycle_wrapped", False):
        return

    def patched_summary(bot: Any) -> dict[str, Any]:
        data = dict(original(bot) or {})
        sports = _visible_sports(bot)
        data["sports"] = [{"sport": sport, "count": count} for sport, count in sports]
        start, end = _day_bounds(bot)
        data["day_cycle"] = {"version": VERSION, "day_start": bot.iso(start), "day_end": bot.iso(end)}
        return data

    patched_summary._day_cycle_wrapped = True
    mini_app._summary = patched_summary


def _archive_day_window(app: Any) -> int:
    day_start, _ = _day_bounds(app)
    cutoff = app.iso(day_start)
    with app.db() as con:
        cur = con.cursor()
        cur.execute(
            """
            UPDATE matches
            SET status='archived'
            WHERE status IN ('open', 'closed')
              AND COALESCE(start_time_utc, start_time) < ?
            """,
            (cutoff,),
        )
        con.commit()
        return int(cur.rowcount or 0)


def _patch_archive(app: Any) -> None:
    def archive_past_matches() -> int:
        return _archive_day_window(app)

    def archive_old_matches() -> int:
        return _archive_day_window(app)

    app.archive_past_matches = archive_past_matches
    app.archive_old_matches = archive_old_matches


def _patch_daily_rollover(app: Any) -> None:
    if getattr(app, "_DAY_CYCLE_ROLLOVER_WRAPPED", False):
        return

    async def daily_rollover_loop():
        while True:
            try:
                zone = getattr(app, "DISPLAY_ZONE", timezone.utc)
                now = app.now_utc()
                local = now.astimezone(zone)
                run_local = local.replace(hour=0, minute=10, second=0, microsecond=0)
                if run_local <= local:
                    run_local += timedelta(days=1)
                wait = max(5, int((run_local.astimezone(timezone.utc) - now).total_seconds()))
                await asyncio.sleep(wait)

                sync = getattr(app, "fixed_sync_once", None)
                if callable(sync):
                    msg = await sync()
                else:
                    archived = _archive_day_window(app)
                    auto = getattr(app, "autosync_once", None)
                    msg = f"archived={archived}"
                    if callable(auto):
                        result = auto()
                        msg += "; " + (await result if asyncio.iscoroutine(result) else str(result))
                logger = getattr(app, "logger", None)
                if logger:
                    logger.info("day cycle rollover: %s", msg)
            except Exception:
                logger = getattr(app, "logger", None)
                if logger:
                    logger.exception("day cycle rollover error")
                await asyncio.sleep(60)

    app.daily_rollover_loop = daily_rollover_loop
    app._DAY_CYCLE_ROLLOVER_WRAPPED = True


def _register_command(app: Any) -> None:
    if getattr(app, "_DAY_CYCLE_COMMANDS_REGISTERED", False):
        return

    @app.dp.message(app.Command("day_rollover_now"))
    async def day_rollover_now_cmd(m: Any):
        if not m.from_user or (getattr(app, "ADMIN_ID", 0) and int(m.from_user.id) != int(app.ADMIN_ID)):
            return await m.answer("Недостаточно прав.")
        sync = getattr(app, "fixed_sync_once", None)
        if callable(sync):
            msg = await sync()
        else:
            msg = f"archived={_archive_day_window(app)}"
        await m.answer(f"<pre>{escape(str(msg))}</pre>")

    app._DAY_CYCLE_COMMANDS_REGISTERED = True


def _patch_index_html(mini_app: Any) -> None:
    original = getattr(mini_app, "_index_html", None)
    if not callable(original) or getattr(original, "_day_cycle_wrapped", False):
        return

    def patched_index_html() -> str:
        html = original()
        if "DAY_CYCLE_PATCH_V1" not in html:
            html = html.replace("</style>", DAY_CYCLE_CSS + "\n  </style>", 1)
            html = html.replace(
                "document.getElementById('refresh').onclick = load;",
                DAY_CYCLE_JS + "\ndocument.getElementById('refresh').onclick = load;",
                1,
            )
        return html

    patched_index_html._day_cycle_wrapped = True
    mini_app._index_html = patched_index_html


def apply(app: Any) -> None:
    if getattr(app, "_DAY_CYCLE_PATCH_APPLIED", False):
        return
    try:
        import mini_app
    except Exception as exc:
        logger = getattr(app, "logger", None)
        if logger:
            logger.exception("day cycle patch import failed: %s", exc)
        return

    _patch_row_to_match(app, mini_app)
    _patch_active_matches(app, mini_app)
    _patch_summary(app, mini_app)
    _patch_archive(app)
    _patch_daily_rollover(app)
    _register_command(app)
    _patch_index_html(mini_app)
    app._DAY_CYCLE_PATCH_APPLIED = True
    print(f"{VERSION}_APPLIED")
