from __future__ import annotations

import sqlite3
from typing import Any

VERSION = "FINISHED_CARDS_PATCH_V1"
PICKS = ("1", "X", "2")

FINISHED_CARDS_CSS = r"""
    /* FINISHED_CARDS_PATCH_V1 */
    .match.finished-card {
      overflow:hidden;
      border-color:rgba(110,231,183,.36);
      background:
        linear-gradient(180deg, rgba(110,231,183,.15), rgba(255,255,255,.035)),
        rgba(17,22,21,.88);
    }
    .match.finished-card.user-lost {
      border-color:rgba(255,114,114,.34);
      background:
        linear-gradient(180deg, rgba(255,114,114,.105), rgba(255,255,255,.03)),
        rgba(17,22,21,.88);
    }
    .match.finished-card::before { background:linear-gradient(180deg, var(--good, #6be49d), var(--accent2, #f4c430)); }
    .match.finished-card.user-lost::before { background:linear-gradient(180deg, var(--bad, #ff7272), var(--accent2, #f4c430)); }
    .result-hero {
      display:grid;
      grid-template-columns:1fr auto;
      gap:10px;
      align-items:center;
      padding:11px;
      border:1px solid rgba(110,231,183,.24);
      border-radius:8px;
      background:rgba(110,231,183,.075);
    }
    .finished-card.user-lost .result-hero { border-color:rgba(255,114,114,.22); background:rgba(255,114,114,.055); }
    .result-kicker { color:var(--muted); font-size:10px; text-transform:uppercase; font-weight:900; letter-spacing:0; }
    .result-title { margin-top:3px; color:var(--text); font-size:14px; font-weight:900; line-height:1.18; }
    .score-pill {
      min-width:66px;
      min-height:50px;
      display:grid;
      place-items:center;
      padding:8px 10px;
      border:1px solid rgba(244,196,48,.34);
      border-radius:8px;
      background:rgba(244,196,48,.09);
      color:var(--accent2);
      font-size:18px;
      font-weight:950;
      white-space:nowrap;
    }
    .settlement-grid { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:7px; }
    .settlement-item {
      min-height:58px;
      padding:9px;
      border:1px solid rgba(255,255,255,.08);
      border-radius:8px;
      background:rgba(255,255,255,.035);
    }
    .settlement-item span { display:block; color:var(--muted); font-size:10px; }
    .settlement-item b { display:block; margin-top:4px; font-size:15px; color:var(--text); }
    .settlement-item.good b { color:var(--good, #6be49d); }
    .settlement-item.bad b { color:var(--bad, #ff7272); }
    .market-strip {
      display:grid;
      grid-template-columns:1fr auto;
      gap:8px;
      align-items:center;
      padding:8px 10px;
      border:1px solid rgba(244,196,48,.18);
      border-radius:8px;
      background:rgba(244,196,48,.055);
      color:var(--muted);
      font-size:11px;
    }
    .market-strip b { color:var(--accent2); font-size:12px; text-align:right; }
    .market-strip.market-ai { border-color:rgba(86,211,255,.16); background:rgba(86,211,255,.05); }
    .market-strip.market-ai b { color:var(--cyan); }
    .market-books { color:var(--text); font-weight:800; }
    .pick-cta.result-winner {
      border-color:rgba(110,231,183,.72);
      background:linear-gradient(180deg, rgba(110,231,183,.30), rgba(110,231,183,.13));
      box-shadow:0 12px 26px rgba(110,231,183,.12);
    }
    .pick-cta.result-winner span, .pick-cta.result-winner b { color:var(--good, #6be49d); }
    .pick-cta.result-loser { opacity:.54; }
    .pick-cta.result-draw {
      border-color:rgba(255,209,102,.62);
      background:rgba(255,209,102,.13);
    }
    .pick-cta.result-draw span, .pick-cta.result-draw b { color:var(--warn, #ffd166); }
    .result-note {
      display:flex;
      justify-content:space-between;
      gap:8px;
      align-items:center;
      color:var(--muted);
      font-size:11px;
    }
    @media (max-width: 520px) {
      .result-hero { grid-template-columns:1fr; }
      .score-pill { justify-self:start; min-height:40px; }
      .settlement-grid { grid-template-columns:1fr; }
      .market-strip { grid-template-columns:1fr; }
      .market-strip b { text-align:left; }
    }
"""

FINISHED_CARDS_JS = r"""
(function(){
if(window.__finishedCardsPatchV1) return;
window.__finishedCardsPatchV1 = true;
const fcEsc = value => String(value ?? '').replace(/[&<>'"]/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[ch]));
const fcMoney = value => Number(value || 0).toLocaleString('ru-RU');
const fcPick = p => (typeof pickLabel === 'function' ? pickLabel(p) : ({'1':'П1','X':'X','2':'П2'}[p] || p));
const fcOdd = v => (typeof fmtOdd === 'function' ? fmtOdd(v) : (v == null ? '—' : Number(v).toFixed(2)));
const fcPct = v => (typeof fmtPct === 'function' ? fmtPct(v) : (v == null ? '—' : `${Math.round(Number(v || 0)*1000)/10}%`));
const fcSportIcon = sport => (typeof beautySportIcon === 'function' ? beautySportIcon(sport) : ({football:'⚽', hockey:'🏒', nhl:'🏒', tennis:'🎾', all:'★'}[(sport || '').toLowerCase()] || '•'));
const fcSportName = sport => (typeof beautySportName === 'function' ? beautySportName(sport) : (typeof sportLabel === 'function' ? String(sportLabel(sport)).replace(/^[^\p{L}\p{N}]+/u,'').trim() : (sport || 'Матчи')));
function fcPicks(m){ return (m?.available_picks && m.available_picks.length ? m.available_picks : ['1','X','2']); }
function fcBestPick(m){
  const picks = fcPicks(m); let best = picks[0], score = -1;
  picks.forEach(p => { const v = Number(m?.probabilities?.[p] || 0); if(v > score){ score = v; best = p; } });
  return {pick:best, score};
}
function fcBookLabel(raw){
  let value = String(raw || '').trim();
  if(!value) return 'букмекеры';
  if(value.startsWith('BK:')) value = value.slice(3);
  return value.replaceAll(',', ' · ');
}
function fcMarketLabel(m){
  const line = m?.line || {};
  if(line.source === 'market') return `БК-линия · <span class="market-books">${fcEsc(fcBookLabel(line.bookmaker))}</span>`;
  return 'AI fallback · рынок пока не найден';
}
function fcEdgeLabel(m){
  const best = m?.best_value;
  if(best && best.edge != null){
    const value = Math.round(Number(best.edge || 0) * 1000) / 10;
    return value > 0 ? `Value ${fcPick(best.pick)} +${value}%` : `Value ${fcPick(best.pick)} ${value}%`;
  }
  return m?.line?.source === 'market' ? 'коэффициенты из БК' : 'коэффициенты AI';
}
function fcMarketStrip(m){
  const cls = m?.line?.source === 'market' ? 'market-live' : 'market-ai';
  return `<div class="market-strip ${cls}"><span>${fcMarketLabel(m)}</span><b>${fcEsc(fcEdgeLabel(m))}</b></div>`;
}
function fcScore(m){
  const h = m?.score?.home;
  const a = m?.score?.away;
  if(h !== null && h !== undefined && a !== null && a !== undefined) return `${h}:${a}`;
  return fcPick(m?.winner_pick || m?.result || '—');
}
function fcResultHero(m){
  if(!m?.is_finished) return '';
  const title = m.winner_pick === 'X' ? 'Ничья' : `Победитель: ${m.winner_name || m.winner_label || fcPick(m.winner_pick)}`;
  return `<div class="result-hero"><div><div class="result-kicker">Матч завершен</div><div class="result-title">${fcEsc(title)}</div></div><div class="score-pill">${fcEsc(fcScore(m))}</div></div>`;
}
function fcSettlement(m){
  if(!m?.is_finished) return '';
  if(!m.my_pick){
    return `<div class="settlement-grid"><div class="settlement-item"><span>Прогноз</span><b>не сделан</b></div><div class="settlement-item"><span>Исход</span><b>${fcPick(m.winner_pick)}</b></div><div class="settlement-item"><span>Статус</span><b>закрыт</b></div></div>`;
  }
  const won = m.result_state === 'won';
  const profit = Number(m.my_profit || 0);
  const payout = Number(m.my_payout || 0);
  return `<div class="settlement-grid">
    <div class="settlement-item"><span>Твой выбор</span><b>${fcPick(m.my_pick)} · ${fcMoney(m.my_stake || 0)}</b></div>
    <div class="settlement-item"><span>Коэффициент</span><b>${fcOdd(m.my_odds)}</b></div>
    <div class="settlement-item ${won ? 'good' : 'bad'}"><span>${won ? 'Выигрыш' : 'Проигрыш'}</span><b>${profit > 0 ? '+' : ''}${fcMoney(profit || -Number(m.my_stake || 0))}</b></div>
  </div>${won ? `<div class="result-note"><span>Начисление по ставке</span><b>+${fcMoney(payout)}</b></div>` : ''}`;
}
function fcPickButtons(m){
  const picks = fcPicks(m);
  const finished = Boolean(m.is_finished);
  return picks.map(p => {
    const winner = finished && m.winner_pick === p;
    const classes = ['pick-cta'];
    if(m.my_pick === p) classes.push('picked');
    if(finished) classes.push(winner ? (p === 'X' ? 'result-draw' : 'result-winner') : 'result-loser');
    const disabled = finished || !m.can_predict || !picks.includes(p);
    const label = finished && winner ? 'Победил' : fcPick(p);
    return `<button class="${classes.join(' ')}" data-pick="${p}" data-id="${m.id}" ${disabled?'disabled':''}><span>${label}</span><b>${fcPick(p)} ${fcOdd(m.odds?.[p])}</b></button>`;
  }).join('');
}
function fcTicketLine(m){
  if(m.is_finished){
    const state = m.result_state === 'won' ? 'Ставка выиграла' : (m.result_state === 'lost' ? 'Ставка проиграла' : 'Итог зафиксирован');
    return `<div class="ticket-line"><span>${state}</span><span class="status-pill">Итог</span></div>`;
  }
  const ticket = m.my_pick ? `Ставка: <b>${fcPick(m.my_pick)} · ${fcMoney(m.my_stake)} ${m.my_odds ? fcOdd(m.my_odds) : ''}</b>` : (m.can_predict ? 'Ставки открыты' : fcEsc(m.blocked_reason || 'Ставки закрыты'));
  return `<div class="ticket-line"><span>${ticket}</span><span class="status-pill">Live</span></div>`;
}
function renderMatches(items){
  try { lastMatches = items || []; } catch(_e) {}
  const el = document.getElementById('matches');
  const count = document.getElementById('matchCount');
  const list = items || [];
  const done = list.filter(x => x.is_finished).length;
  const open = list.length - done;
  if(count) count.textContent = `${open} активных · ${done} итогов`;
  if(!el) return;
  if(!list.length){ el.innerHTML = '<div class="empty">На сегодня матчей нет. Новая линия появится после синхронизации.</div>'; return; }
  el.innerHTML = list.map(m => {
    const picks = fcPicks(m);
    const best = fcBestPick(m);
    const classes = ['match','beauty-match'];
    if(m.my_pick) classes.push('has-pick');
    if(m.is_finished) classes.push('finished-card');
    if(m.result_state === 'won') classes.push('user-won');
    if(m.result_state === 'lost') classes.push('user-lost');
    return `<article class="${classes.join(' ')}">
      <div class="match-head">
        <span class="sport-pill">${fcSportIcon(m.sport)} ${fcEsc(fcSportName(m.sport))}</span>
        <span class="time-pill">${fcEsc(m.display_time || '—')}</span>
      </div>
      <div class="match-title">${fcEsc(m.title || 'Матч')}</div>
      <div class="match-meta"><span>${fcEsc(m.league || '—')}</span><span>Голоса ${Number(m.votes?.total || 0)}</span><span>${m.is_finished ? 'Результат зафиксирован' : `AI пик ${fcPick(best.pick)} ${fcPct(best.score)}`}</span></div>
      ${fcResultHero(m)}
      ${fcMarketStrip(m)}
      <div class="ai-strip"><span>${m.is_finished ? 'AI-прогноз до матча' : 'AI-анализ'}</span><b>${picks.map(p => `${fcPick(p)} ${fcPct(m.probabilities?.[p])}`).join(' · ')}</b></div>
      <div class="pick-row ${picks.length === 2 ? 'two' : ''}">${fcPickButtons(m)}</div>
      ${fcSettlement(m)}
      ${fcTicketLine(m)}
    </article>`;
  }).join('');
  el.querySelectorAll('button[data-pick]:not(:disabled)').forEach(b=>b.onclick=()=>{
    if(typeof openBetModal === 'function') openBetModal(Number(b.dataset.id), b.dataset.pick);
    else sendPick(Number(b.dataset.id), b.dataset.pick);
  });
}
function renderMine(items){
  const el = document.getElementById('myPredictions');
  if(!el) return;
  if(!initData){ el.innerHTML = '<div class="empty">Открой Mini App через Telegram</div>'; return; }
  if(!items || !items.length){ el.innerHTML = '<div class="empty">Прогнозов пока нет</div>'; return; }
  el.innerHTML = items.map(x=>{
    const done = x.result === '1' || x.result === 'X' || x.result === '2';
    const won = done && x.pick === x.result;
    const cls = done ? (won ? 'user-won' : 'user-lost') : '';
    const profit = done && Number(x.stake || 0) > 0 ? (won ? Math.round(Number(x.stake || 0) * Number(x.odds || 0)) - Number(x.stake || 0) : -Number(x.stake || 0)) : null;
    const right = `${fcPick(x.pick)}${x.stake ? ` · ${fcMoney(x.stake)} ${x.odds ? fcOdd(x.odds) : ''}` : ''}${done ? ` → ${fcPick(x.result)}` : ''}${profit !== null ? ` · ${profit > 0 ? '+' : ''}${fcMoney(profit)}` : ''}`;
    return `<div class="row ${cls}"><span>${fcEsc(x.title)}<br><span class="muted">${fcSportIcon(x.sport)} ${fcEsc(x.league || '—')} · ${fcEsc(x.status || '')}</span></span><b>${right}</b></div>`;
  }).join('');
}
})();
"""


def _log(app: Any):
    return getattr(app, "logger", None)


def _row_value(row: Any, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    if isinstance(row, dict):
        return row.get(key, default)
    try:
        return row[key]
    except Exception:
        return default


def _safe_alter(cur: Any, statement: str) -> None:
    try:
        cur.execute(statement)
    except sqlite3.OperationalError:
        pass


def _ensure_schema(app: Any) -> None:
    try:
        app.init_db()
    except Exception:
        logger = _log(app)
        if logger:
            logger.exception("finished cards: init_db failed")
    with app.db() as con:
        cur = con.cursor()
        _safe_alter(cur, "ALTER TABLE matches ADD COLUMN home_score INTEGER")
        _safe_alter(cur, "ALTER TABLE matches ADD COLUMN away_score INTEGER")
        _safe_alter(cur, "ALTER TABLE matches ADD COLUMN settled_at TEXT")
        con.commit()


def _patch_scoring(app: Any) -> None:
    original = getattr(app, "apply_scoring_for_match", None)
    if not callable(original) or getattr(original, "_finished_cards_wrapped", False):
        return

    async def patched_apply_scoring_for_match(match_id: int, result_1x2: str, home_score: int | None = None, away_score: int | None = None) -> None:
        await original(match_id, result_1x2, home_score, away_score)
        try:
            with app.db() as con:
                if home_score is not None or away_score is not None:
                    con.execute(
                        """
                        UPDATE matches
                        SET home_score=?, away_score=?, settled_at=COALESCE(settled_at, ?)
                        WHERE id=? AND status='closed'
                        """,
                        (home_score, away_score, app.iso(app.now_utc()), int(match_id)),
                    )
                else:
                    con.execute(
                        "UPDATE matches SET settled_at=COALESCE(settled_at, ?) WHERE id=? AND status='closed'",
                        (app.iso(app.now_utc()), int(match_id)),
                    )
                con.commit()
        except Exception:
            logger = _log(app)
            if logger:
                logger.exception("finished cards: score metadata update failed match_id=%s", match_id)

    patched_apply_scoring_for_match._finished_cards_wrapped = True
    app.apply_scoring_for_match = patched_apply_scoring_for_match


def _vote_payload(app: Any, user_id: int | None, match_id: int, result: str) -> dict[str, Any]:
    if not user_id:
        return {}
    try:
        with app.db() as con:
            row = con.execute(
                "SELECT pick, COALESCE(stake,0) AS stake, COALESCE(odds,0) AS odds FROM votes WHERE user_id=? AND match_id=?",
                (int(user_id), int(match_id)),
            ).fetchone()
    except Exception:
        row = None
    if not row:
        return {}
    pick = str(_row_value(row, "pick", "") or "").upper()
    stake = int(_row_value(row, "stake", 0) or 0)
    try:
        odds = float(_row_value(row, "odds", 0) or 0)
    except (TypeError, ValueError):
        odds = 0.0
    payload = {"my_pick": pick or None, "my_stake": stake, "my_odds": odds if odds > 0 else None}
    if result in PICKS and pick in PICKS and stake > 0:
        if pick == result and odds > 0:
            payout = int(round(stake * odds))
            payload["my_payout"] = payout
            payload["my_profit"] = payout - stake
        else:
            payload["my_payout"] = 0
            payload["my_profit"] = -stake
    return payload


def _patch_row_to_match(app: Any, mini_app: Any) -> None:
    original = getattr(mini_app, "_row_to_match", None)
    if not callable(original) or getattr(original, "_finished_cards_wrapped", False):
        return

    def patched_row_to_match(bot: Any, row: Any, user_id: int | None = None) -> dict[str, Any]:
        item = dict(original(bot, row, user_id) or {})
        match_id = int(_row_value(row, "id", item.get("id") or 0) or 0)
        result = str(_row_value(row, "result", item.get("result") or "") or "").upper()
        home_score = _row_value(row, "home_score")
        away_score = _row_value(row, "away_score")
        item["score"] = {"home": home_score, "away": away_score}
        item["settled_at"] = _row_value(row, "settled_at")
        if user_id and match_id:
            item.update(_vote_payload(bot, int(user_id), match_id, result))
        if item.get("is_finished") and result in PICKS:
            my_pick = str(item.get("my_pick") or "").upper()
            if my_pick in PICKS:
                item["result_state"] = "won" if my_pick == result else "lost"
            else:
                item["result_state"] = "finished"
        return item

    patched_row_to_match._finished_cards_wrapped = True
    mini_app._row_to_match = patched_row_to_match


def _patch_index_html(mini_app: Any) -> None:
    original = getattr(mini_app, "_index_html", None)
    if not callable(original) or getattr(original, "_finished_cards_wrapped", False):
        return

    def patched_index_html() -> str:
        html = original()
        if "FINISHED_CARDS_PATCH_V1" not in html:
            html = html.replace("</style>", FINISHED_CARDS_CSS + "\n  </style>", 1)
        if "__finishedCardsPatchV1" not in html:
            html = html.replace("document.getElementById('refresh').onclick = load;", FINISHED_CARDS_JS + "\ndocument.getElementById('refresh').onclick = load;", 1)
        return html

    patched_index_html._finished_cards_wrapped = True
    mini_app._index_html = patched_index_html


def apply(app: Any) -> None:
    if getattr(app, "_FINISHED_CARDS_PATCH_APPLIED", False):
        return
    try:
        import mini_app
    except Exception as exc:
        logger = _log(app)
        if logger:
            logger.exception("finished cards import failed: %s", exc)
        return
    _ensure_schema(app)
    _patch_scoring(app)
    _patch_row_to_match(app, mini_app)
    _patch_index_html(mini_app)
    app._FINISHED_CARDS_PATCH_APPLIED = True
    print(f"{VERSION}_APPLIED")
