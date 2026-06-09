"""
results_tab_patch.py  —  RESULTS_TAB_PATCH_V1

Добавляет отдельную вкладку «Итоги» в Mini App с красивыми карточками
завершённых матчей.

Что добавляет:
  • Вкладка «Итоги» между «Мои» и «Профиль»
  • GET /api/results  — список завершённых матчей (статус closed/finished)
  • Каждая карточка показывает:
      - Счёт матча (home_score : away_score или исход)
      - Победитель / Ничья / AI-прогноз до матча
      - Коэффициенты из БК (если были) и реальный edge
      - Прогноз пользователя + результат ставки (выиграл / проиграл)
      - Дата завершения
  • Фильтры: Все · Выиграл · Проиграл · Сегодня
  • Пагинация (кнопка «Загрузить ещё»)
  • Цветовое кодирование: зелёный = победа, красный = поражение, жёлтый = ничья
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

VERSION = "RESULTS_TAB_PATCH_V1"
PICKS = ("1", "X", "2")


# ──────────────────────────────────────────────────────
# Утилиты
# ──────────────────────────────────────────────────────

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
        pass
    try:
        with app.db() as con:
            cur = con.cursor()
            _safe_alter(cur, "ALTER TABLE matches ADD COLUMN home_score INTEGER")
            _safe_alter(cur, "ALTER TABLE matches ADD COLUMN away_score INTEGER")
            _safe_alter(cur, "ALTER TABLE matches ADD COLUMN settled_at TEXT")
            con.commit()
    except Exception as exc:
        logger = _log(app)
        if logger:
            logger.exception("results_tab: schema migration failed: %s", exc)


def _int_query(value: str | None, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value or default)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(number, maximum))


def _json_response(bot: Any, data: Any, status: int = 200):
    return bot.web.Response(
        status=status,
        text=json.dumps(data, ensure_ascii=False, default=str),
        content_type="application/json",
        charset="utf-8",
    )


def _build_results_item(bot: Any, row: Any, user_id: int | None = None) -> dict[str, Any]:
    match_id = int(_row_value(row, "id") or 0)
    result = str(_row_value(row, "result") or "").upper()
    home_score = _row_value(row, "home_score")
    away_score = _row_value(row, "away_score")

    try:
        priced = bot.ai_odds_for_match(dict(row)) if hasattr(bot, "ai_odds_for_match") else {}
    except Exception:
        priced = {}

    item: dict[str, Any] = {
        "id": match_id,
        "title": _row_value(row, "title", ""),
        "sport": _row_value(row, "sport", "other"),
        "league": _row_value(row, "league", ""),
        "result": result,
        "score": {"home": home_score, "away": away_score},
        "settled_at": _row_value(row, "settled_at"),
        "display_time": None,
        "winner_pick": result if result in PICKS else None,
        "odds": {
            "1": priced.get("odds_1") or _row_value(row, "odds_1"),
            "X": priced.get("odds_x") or _row_value(row, "odds_x"),
            "2": priced.get("odds_2") or _row_value(row, "odds_2"),
        },
        "probabilities": {
            "1": priced.get("prob_1"),
            "X": priced.get("prob_x"),
            "2": priced.get("prob_2"),
        },
        "odds_source": str(_row_value(row, "odds_source") or ""),
        "ai_odds": {
            "1": priced.get("ai_odds_1"),
            "X": priced.get("ai_odds_x"),
            "2": priced.get("ai_odds_2"),
        },
        "my_pick": None,
        "my_stake": 0,
        "my_odds": None,
        "my_payout": None,
        "my_profit": None,
        "result_state": None,
    }

    # Время
    start_value = _row_value(row, "start_time_utc") or _row_value(row, "start_time")
    if hasattr(bot, "_pretty_time") and start_value:
        try:
            item["display_time"] = bot._pretty_time(str(start_value))
        except Exception:
            item["display_time"] = str(start_value)
    else:
        item["display_time"] = str(start_value or "")

    # Прогноз пользователя
    if user_id and match_id:
        try:
            with bot.db() as con:
                vote_row = con.execute(
                    "SELECT pick, COALESCE(stake,0) AS stake, COALESCE(odds,0) AS odds "
                    "FROM votes WHERE user_id=? AND match_id=?",
                    (int(user_id), match_id),
                ).fetchone()
        except Exception:
            vote_row = None

        if vote_row:
            pick = str(_row_value(vote_row, "pick") or "").upper()
            stake = int(_row_value(vote_row, "stake") or 0)
            try:
                odds = float(_row_value(vote_row, "odds") or 0)
            except (TypeError, ValueError):
                odds = 0.0
            item["my_pick"] = pick or None
            item["my_stake"] = stake
            item["my_odds"] = odds if odds > 0 else None

            if pick in PICKS and result in PICKS and stake > 0:
                if pick == result and odds > 0:
                    payout = int(round(stake * odds))
                    item["my_payout"] = payout
                    item["my_profit"] = payout - stake
                    item["result_state"] = "won"
                else:
                    item["my_payout"] = 0
                    item["my_profit"] = -stake
                    item["result_state"] = "lost"
            elif result in PICKS:
                item["result_state"] = "finished"

    # Если нет ставки — только пометить как завершённый
    if item["result_state"] is None and result in PICKS:
        item["result_state"] = "finished"

    return item


def _fetch_results(
    bot: Any,
    user_id: int | None = None,
    limit: int = 30,
    offset: int = 0,
    filter_state: str = "all",
    sport: str = "all",
) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit or 30), 100))
    offset = max(0, int(offset or 0))
    sport = (sport or "all").lower()

    where = ["(status='closed' OR status='finished' OR (status='open' AND result IN ('1','X','2')))"]
    params: list[Any] = []

    if sport != "all":
        where.append("COALESCE(NULLIF(LOWER(sport),''),'other')=?")
        params.append(sport)

    if filter_state == "won" and user_id:
        where.append(
            "id IN (SELECT match_id FROM votes WHERE user_id=? AND pick=result)"
        )
        params.append(int(user_id))
    elif filter_state == "lost" and user_id:
        where.append(
            "id IN (SELECT match_id FROM votes WHERE user_id=? AND pick!=result)"
        )
        params.append(int(user_id))
    elif filter_state == "today":
        where.append("DATE(COALESCE(settled_at, start_time_utc, start_time)) = DATE('now')")

    params.extend([limit, offset])

    try:
        with bot.db() as con:
            rows = con.execute(
                f"""
                SELECT * FROM matches
                WHERE {' AND '.join(where)}
                ORDER BY COALESCE(settled_at, start_time_utc, start_time) DESC
                LIMIT ? OFFSET ?
                """,
                params,
            ).fetchall()
    except Exception as exc:
        logger = _log(bot)
        if logger:
            logger.exception("results_tab: fetch_results failed: %s", exc)
        return []

    return [_build_results_item(bot, row, user_id) for row in rows]


# ──────────────────────────────────────────────────────
# CSS + JS для вкладки «Итоги»
# ──────────────────────────────────────────────────────

RESULTS_TAB_CSS = r"""
    /* RESULTS_TAB_PATCH_V1 */
    #resultsView .result-card {
      position:relative;
      overflow:hidden;
      padding:14px 15px;
      border-bottom:1px solid var(--line);
      transition:background .15s;
    }
    #resultsView .result-card:last-child { border-bottom:0; }
    #resultsView .result-card::before {
      content:'';
      position:absolute;
      left:0; top:0; bottom:0;
      width:3px;
      border-radius:0 3px 3px 0;
      background:var(--line);
    }
    #resultsView .result-card.rc-won::before  { background:#6be49d; }
    #resultsView .result-card.rc-lost::before { background:#ff7272; }
    #resultsView .result-card.rc-draw::before { background:#ffd166; }
    #resultsView .result-card.rc-won  { background:rgba(107,228,157,.04); }
    #resultsView .result-card.rc-lost { background:rgba(255,114,114,.035); }

    .rc-head {
      display:flex;
      justify-content:space-between;
      align-items:flex-start;
      gap:8px;
      margin-bottom:8px;
    }
    .rc-sport-pill {
      display:inline-flex;
      align-items:center;
      gap:4px;
      padding:2px 7px;
      border-radius:10px;
      background:rgba(255,255,255,.07);
      font-size:10px;
      font-weight:700;
      color:var(--muted);
      white-space:nowrap;
    }
    .rc-time { color:var(--muted); font-size:11px; }
    .rc-title { font-weight:800; font-size:14px; line-height:1.25; margin-bottom:4px; }
    .rc-league { color:var(--muted); font-size:11px; margin-bottom:10px; }
    .rc-score-block {
      display:flex;
      align-items:center;
      gap:10px;
      margin-bottom:10px;
    }
    .rc-score-pill {
      display:inline-flex;
      align-items:center;
      justify-content:center;
      min-width:64px;
      padding:6px 10px;
      border-radius:8px;
      background:rgba(244,196,48,.12);
      border:1px solid rgba(244,196,48,.32);
      color:var(--accent2, #f4c430);
      font-size:20px;
      font-weight:950;
    }
    .rc-winner-label {
      font-size:12px;
      font-weight:700;
    }
    .rc-winner-label .rc-winner-team { color:var(--text); }
    .rc-winner-label .rc-winner-pick { color:var(--muted); font-size:11px; font-weight:500; }
    .rc-draw-label { color:#ffd166; font-weight:800; font-size:13px; }

    .rc-odds-row {
      display:grid;
      grid-template-columns:repeat(3,1fr);
      gap:6px;
      margin-bottom:8px;
    }
    .rc-odd-cell {
      padding:7px 6px;
      border:1px solid var(--line);
      border-radius:8px;
      background:var(--panel2);
      text-align:center;
    }
    .rc-odd-cell span { display:block; color:var(--muted); font-size:10px; }
    .rc-odd-cell b { display:block; font-size:14px; font-weight:900; margin-top:2px; }
    .rc-odd-cell.rc-result-cell {
      border-color:rgba(107,228,157,.35);
      background:rgba(107,228,157,.09);
    }
    .rc-odd-cell.rc-result-cell b { color:#6be49d; }
    .rc-odd-cell.rc-draw-cell {
      border-color:rgba(255,209,102,.35);
      background:rgba(255,209,102,.07);
    }
    .rc-odd-cell.rc-draw-cell b { color:#ffd166; }

    .rc-user-bet {
      display:flex;
      justify-content:space-between;
      align-items:center;
      gap:8px;
      padding:8px 10px;
      border-radius:8px;
      background:rgba(255,255,255,.04);
      border:1px solid rgba(255,255,255,.07);
      font-size:12px;
    }
    .rc-user-bet .rc-bet-pick { font-weight:800; }
    .rc-user-bet .rc-bet-profit.pos { color:#6be49d; font-weight:800; }
    .rc-user-bet .rc-bet-profit.neg { color:#ff7272; font-weight:800; }
    .rc-bk-badge {
      display:inline-flex;
      align-items:center;
      gap:4px;
      font-size:10px;
      font-weight:700;
      color:var(--muted);
      margin-bottom:6px;
    }
    .rc-bk-badge .rc-bk-dot {
      width:5px; height:5px; border-radius:50%;
      background:#6be49d; flex-shrink:0;
    }
    .rc-bk-badge.ai-bk .rc-bk-dot { background:#56c2ff; }

    .results-filters {
      display:flex;
      gap:6px;
      padding:10px 0 6px;
      overflow-x:auto;
      -webkit-overflow-scrolling:touch;
    }
    .results-filters button {
      font-size:12px;
      padding:6px 12px;
      border-radius:20px;
      white-space:nowrap;
      flex-shrink:0;
    }
    .rc-load-more {
      width:100%;
      margin-top:10px;
      padding:11px;
      border-radius:9px;
      background:rgba(255,255,255,.04);
      border:1px solid var(--line);
      color:var(--muted);
      font-size:13px;
      cursor:pointer;
    }
    .rc-load-more:hover { background:rgba(255,255,255,.07); }
    .rc-empty { padding:24px 14px; color:var(--muted); text-align:center; font-size:14px; }
    @media (max-width:480px) {
      .rc-odds-row { grid-template-columns:repeat(3,1fr); }
      .rc-score-pill { font-size:17px; min-width:54px; }
    }
"""

RESULTS_TAB_JS = r"""
(function(){
if(window.__resultsTabV1) return;
window.__resultsTabV1 = true;

const rtEsc = s => String(s ?? '').replace(/[&<>'"]/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
const rtFmt = v => v == null ? '—' : Number(v).toFixed(2);
const rtMoney = v => Number(v || 0).toLocaleString('ru-RU');
const rtPick = p => ({'1':'П1','X':'X','2':'П2'}[p] || p || '—');
const rtSportIcon = s => ({football:'⚽',soccer:'⚽',hockey:'🏒',nhl:'🏒',tennis:'🎾',basketball:'🏀',baseball:'⚾',mma:'🥊',other:'•'}[(s||'').toLowerCase()] || '•');

let rtCurrentFilter = 'all';
let rtCurrentOffset = 0;
const RT_PAGE = 20;
let rtLoading = false;

function rtBkBadge(source) {
  if(!source) return '';
  const isAI = !source || ['AI','AI_LINE','MARKET_FALLBACK'].some(p => source.toUpperCase().startsWith(p));
  const cls = isAI ? 'ai-bk' : '';
  const label = isAI ? 'AI коэффициент' : `БК · ${rtEsc(source)}`;
  return `<div class="rc-bk-badge ${cls}"><span class="rc-bk-dot"></span>${label}</div>`;
}

function rtScoreBlock(m) {
  const h = m.score?.home, a = m.score?.away;
  const hasScore = h !== null && h !== undefined && a !== null && a !== undefined;
  const scoreText = hasScore ? `${h}:${a}` : (m.result || '—');
  const isX = m.result === 'X';
  const winnerName = isX ? null :
    (m.result === '1' ? m.title?.split(/\s+vs\.?\s+|\s+v\.?\s+|\s+[—–-]\s+/i)[0] :
                        m.title?.split(/\s+vs\.?\s+|\s+v\.?\s+|\s+[—–-]\s+/i)[1]);
  const winnerHtml = isX
    ? `<div class="rc-draw-label">Ничья</div>`
    : winnerName
      ? `<div class="rc-winner-label"><div class="rc-winner-team">${rtEsc((winnerName||'').trim())}</div><div class="rc-winner-pick">${rtPick(m.result)}</div></div>`
      : `<div class="rc-winner-label"><span class="rc-winner-pick">${rtPick(m.result)}</span></div>`;
  return `<div class="rc-score-block">
    <div class="rc-score-pill">${rtEsc(scoreText)}</div>
    ${winnerHtml}
  </div>`;
}

function rtOddsRow(m) {
  const picks = ['1','X','2'];
  return `<div class="rc-odds-row">${picks.map(p => {
    const isResult = p === m.result;
    const isDraw = p === 'X' && m.result === 'X';
    const cls = isDraw ? 'rc-draw-cell' : (isResult ? 'rc-result-cell' : '');
    const val = m.odds?.[p];
    return `<div class="rc-odd-cell ${cls}">
      <span>${rtPick(p)}</span>
      <b>${rtFmt(val)}</b>
    </div>`;
  }).join('')}</div>`;
}

function rtUserBet(m) {
  if(!m.my_pick) return '';
  const won = m.result_state === 'won';
  const lost = m.result_state === 'lost';
  const profit = m.my_profit;
  const profitHtml = profit !== null && profit !== undefined
    ? `<span class="rc-bet-profit ${profit >= 0 ? 'pos' : 'neg'}">${profit > 0 ? '+' : ''}${rtMoney(profit)}</span>`
    : '';
  return `<div class="rc-user-bet">
    <div>
      <span class="rc-bet-pick">${rtPick(m.my_pick)}</span>
      ${m.my_stake ? ` · ${rtMoney(m.my_stake)}` : ''}
      ${m.my_odds ? ` · ${rtFmt(m.my_odds)}` : ''}
    </div>
    <div style="display:flex;align-items:center;gap:6px">
      ${profitHtml}
      <span style="font-size:11px;color:var(--muted)">${won?'Выиграл':lost?'Проиграл':'Завершён'}</span>
    </div>
  </div>`;
}

function rtRenderCard(m) {
  const stateClass = m.result_state === 'won' ? 'rc-won' :
                     m.result_state === 'lost' ? 'rc-lost' :
                     m.result === 'X' ? 'rc-draw' : '';
  return `<div class="result-card ${stateClass}">
    <div class="rc-head">
      <span class="rc-sport-pill">${rtSportIcon(m.sport)} ${rtEsc(m.sport || 'other')}</span>
      <span class="rc-time">${rtEsc(m.display_time || m.settled_at || '')}</span>
    </div>
    <div class="rc-title">${rtEsc(m.title || 'Матч')}</div>
    <div class="rc-league">${rtEsc(m.league || '—')}</div>
    ${m.result ? rtScoreBlock(m) : ''}
    ${rtBkBadge(m.odds_source)}
    ${rtOddsRow(m)}
    ${rtUserBet(m)}
  </div>`;
}

function rtRenderList(items, append) {
  const el = document.getElementById('resultsList');
  if(!el) return;
  if(!items || !items.length) {
    if(!append) el.innerHTML = '<div class="rc-empty">Завершённых матчей пока нет</div>';
    return;
  }
  const html = items.map(rtRenderCard).join('');
  if(append) el.insertAdjacentHTML('beforeend', html);
  else el.innerHTML = html;
}

async function rtLoad(append) {
  if(rtLoading) return;
  rtLoading = true;
  const btn = document.getElementById('rtLoadMore');
  if(btn) btn.disabled = true;
  try {
    const sport = typeof currentSport !== 'undefined' ? currentSport : 'all';
    const url = `/api/results?limit=${RT_PAGE}&offset=${rtCurrentOffset}&filter=${rtCurrentFilter}&sport=${encodeURIComponent(sport)}`;
    const headers = typeof authHeaders === 'function' ? authHeaders() : {};
    const r = await fetch(url, {cache:'no-store', headers});
    const data = await r.json();
    const items = data.items || [];
    rtRenderList(items, append);
    rtCurrentOffset += items.length;
    if(btn) btn.style.display = items.length < RT_PAGE ? 'none' : 'block';
  } catch(e) {
    const el = document.getElementById('resultsList');
    if(el && !append) el.innerHTML = `<div class="rc-empty">Ошибка загрузки: ${rtEsc(e.message)}</div>`;
  } finally {
    rtLoading = false;
    if(btn) btn.disabled = false;
  }
}

function rtReload() {
  rtCurrentOffset = 0;
  rtLoad(false);
}

// Инициализация фильтров
function rtInitFilters() {
  const fEl = document.getElementById('resultsFilters');
  if(!fEl || fEl.dataset.init) return;
  fEl.dataset.init = '1';
  const filters = [
    {key:'all', label:'Все'},
    {key:'won', label:'✅ Выиграл'},
    {key:'lost', label:'❌ Проиграл'},
    {key:'today', label:'📅 Сегодня'},
  ];
  fEl.innerHTML = filters.map(f =>
    `<button class="${f.key === rtCurrentFilter ? 'active' : ''}" data-filter="${f.key}">${f.key === 'all' ? 'Все' : f.label}</button>`
  ).join('');
  fEl.querySelectorAll('button').forEach(b => b.onclick = () => {
    rtCurrentFilter = b.dataset.filter;
    fEl.querySelectorAll('button').forEach(x => x.classList.remove('active'));
    b.classList.add('active');
    rtReload();
  });
}

// Подхватить смену вкладки
const _origTabSetup = window.__rtOrigTabSetup;
document.querySelectorAll('#mainTabs button[data-view]').forEach(b => {
  const origOnClick = b.onclick;
  b.addEventListener('click', () => {
    if(b.dataset.view === 'resultsView') {
      setTimeout(() => { rtInitFilters(); if(!rtCurrentOffset) rtLoad(false); }, 50);
    }
  });
});

window.rtReload = rtReload;
window.rtLoad = rtLoad;
})();
"""

# ──────────────────────────────────────────────────────
# HTML-блок новой вкладки (вставляется в <body>)
# ──────────────────────────────────────────────────────

RESULTS_TAB_HTML = """
    <section class="view" id="resultsView">
      <div class="panel">
        <h2>Итоги матчей</h2>
        <div class="results-filters" id="resultsFilters"></div>
        <div id="resultsList"><div class="rc-empty">Загрузка...</div></div>
        <button class="rc-load-more" id="rtLoadMore" style="display:none" onclick="rtLoad(true)">Загрузить ещё</button>
      </div>
    </section>"""

# ──────────────────────────────────────────────────────
# Патчинг
# ──────────────────────────────────────────────────────

def _patch_index_html(mini_app: Any) -> None:
    original = getattr(mini_app, "_index_html", None)
    if not callable(original) or getattr(original, "_results_tab_wrapped", False):
        return

    def patched_index_html() -> str:
        html = original()

        # 1. CSS
        if "RESULTS_TAB_PATCH_V1" not in html:
            html = html.replace("</style>", RESULTS_TAB_CSS + "\n  </style>", 1)

        # 2. Добавить кнопку вкладки в #mainTabs
        if "resultsView" not in html:
            html = html.replace(
                'data-view="myView">Мои</button>',
                'data-view="myView">Мои</button>\n      '
                '<button data-view="resultsView">Итоги</button>',
            )

        # 3. HTML-секция вкладки (после <section id="myView">...)
        if "resultsView" not in html:
            html = html.replace(
                '<section class="view" id="profileView">',
                RESULTS_TAB_HTML + '\n    <section class="view" id="profileView">',
            )
        else:
            # Кнопка уже добавлена, убедимся что секция тоже есть
            if 'id="resultsView"' not in html:
                html = html.replace(
                    '<section class="view" id="profileView">',
                    RESULTS_TAB_HTML + '\n    <section class="view" id="profileView">',
                )

        # 4. JS
        if "__resultsTabV1" not in html:
            html = html.replace(
                "document.getElementById('refresh').onclick = load;",
                RESULTS_TAB_JS + "\ndocument.getElementById('refresh').onclick = load;",
                1,
            )

        return html

    patched_index_html._results_tab_wrapped = True
    mini_app._index_html = patched_index_html


def _register_api_route(app: Any, mini_app: Any) -> None:
    """Добавить GET /api/results в aiohttp-роутер."""
    def _make_handler(bot: Any):
        async def api_results(request):
            # Авторизация (опционально)
            user_id = None
            try:
                from mini_app import _auth_user
                user_id, _, _ = _auth_user(bot, request)
            except Exception:
                pass

            limit = _int_query(request.query.get("limit"), 30, 1, 100)
            offset = _int_query(request.query.get("offset"), 0, 0, 10**9)
            filter_state = (request.query.get("filter") or "all").lower()
            sport = (request.query.get("sport") or "all").lower()

            items = _fetch_results(bot, user_id, limit, offset, filter_state, sport)
            return _json_response(bot, {"items": items, "offset": offset, "limit": limit})

        return api_results

    # Патч start_web_server чтобы добавить маршрут
    original_start = getattr(app, "_mini_app_start_web_server", None)
    if original_start is not None:
        return  # уже патчено

    # Более надёжный подход: патчить _register_routes если он есть
    # Иначе — перехватить запуск через web.Application
    _handler = _make_handler(app)

    original_web_app = getattr(getattr(app, "web", None), "Application", None)
    if original_web_app is None:
        return

    web_module = app.web

    class PatchedApplication(web_module.Application):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            # Маршрут добавим чуть позже через setup
            self._results_handler = _handler

        def setup_router(self):
            pass

    # Вместо подмены класса — добавим маршрут в _apply через runner hook
    # Самый надёжный способ: патчим mini_app.apply() чтобы добавить route после старта
    _inject_route_via_startup(app, mini_app, _handler)


def _inject_route_via_startup(app: Any, mini_app: Any, handler) -> None:
    """Внедряем маршрут через патч функции запуска в mini_app."""
    original_apply = getattr(mini_app, "apply", None)
    if not callable(original_apply) or getattr(original_apply, "_results_route_injected", False):
        # Попробуем другой путь — прямой патч существующего router после старта
        _try_direct_router_patch(app, handler)
        return


def _try_direct_router_patch(app: Any, handler) -> None:
    """Пробуем подключиться к существующему aiohttp router."""
    try:
        web = getattr(app, "web", None)
        if web is None:
            return

        # Запоминаем обработчик, он будет добавлен при следующем старте
        app._results_api_handler = handler
        app._results_route_pending = True

        # Патчим Application.__init__ чтобы поймать момент создания роутера
        orig_app_cls = web.Application

        class PatchedWebApp(orig_app_cls):
            def __init__(self_inner, *a, **kw):
                super().__init__(*a, **kw)
                if getattr(app, "_results_route_pending", False):
                    self_inner.router.add_get("/api/results", handler)
                    app._results_route_pending = False

        web.Application = PatchedWebApp
        app._RESULTS_TAB_ROUTER_PATCHED = True
    except Exception as exc:
        logger = _log(app)
        if logger:
            logger.warning("results_tab: router patch failed: %s", exc)


def apply(app: Any) -> None:
    if getattr(app, "_RESULTS_TAB_PATCH_APPLIED", False):
        return

    try:
        import mini_app
    except Exception as exc:
        logger = _log(app)
        if logger:
            logger.exception("results_tab: mini_app import failed: %s", exc)
        return

    _ensure_schema(app)
    _patch_index_html(mini_app)
    _register_api_route(app, mini_app)

    app._RESULTS_TAB_PATCH_APPLIED = True
    print(f"{VERSION}_APPLIED")
