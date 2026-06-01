from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from html import escape
from typing import Any

VERSION = "MINI_APP_V1"
MINI_APP_BUTTON = "📱 Mini App"


def _public_base_url() -> str:
    raw = (
        os.getenv("MINI_APP_URL")
        or os.getenv("PUBLIC_BASE_URL")
        or os.getenv("RENDER_EXTERNAL_URL")
        or os.getenv("KEEP_ALIVE_URL")
        or ""
    ).strip()
    if raw.endswith("/health"):
        raw = raw[:-7]
    return raw.rstrip("/")


def _json_response(bot, data: Any):
    return bot.web.Response(
        text=json.dumps(data, ensure_ascii=False, default=str),
        content_type="application/json; charset=utf-8",
    )


def _row_to_match(bot, row) -> dict[str, Any]:
    match = dict(row)
    try:
        priced = bot.ai_odds_for_match(dict(row))
    except Exception:
        priced = {}
    stats = bot.match_stats(int(row["id"])) if hasattr(bot, "match_stats") else {"1": 0, "X": 0, "2": 0}
    total_votes = int(stats.get("1", 0) + stats.get("X", 0) + stats.get("2", 0))
    return {
        "id": int(row["id"]),
        "title": row["title"],
        "sport": row["sport"] or "other",
        "league": row["league"] or "",
        "status": row["status"],
        "result": row["result"],
        "start_time": row["start_time_utc"] or row["start_time"],
        "display_time": bot._pretty_time(row["start_time_utc"] or row["start_time"] or "") if hasattr(bot, "_pretty_time") else (row["start_time_utc"] or row["start_time"]),
        "odds": {
            "1": priced.get("odds_1") or row["odds_1"],
            "X": priced.get("odds_x") or row["odds_x"],
            "2": priced.get("odds_2") or row["odds_2"],
        },
        "probabilities": {
            "1": priced.get("prob_1"),
            "X": priced.get("prob_x"),
            "2": priced.get("prob_2"),
        },
        "votes": {"1": int(stats.get("1", 0)), "X": int(stats.get("X", 0)), "2": int(stats.get("2", 0)), "total": total_votes},
    }


def _active_matches(bot, sport: str = "all", limit: int = 80) -> list[dict[str, Any]]:
    sport = (sport or "all").lower()
    limit = max(1, min(int(limit or 80), 200))
    cutoff_fn = getattr(bot, "_today_msk_start_utc", None)
    cutoff = bot.iso(cutoff_fn()) if callable(cutoff_fn) else bot.iso(bot.now_utc())
    with bot.db() as con:
        if sport == "all":
            rows = con.execute(
                """
                SELECT * FROM matches
                WHERE status='open'
                  AND (COALESCE(start_time_utc, start_time) IS NULL OR COALESCE(start_time_utc, start_time) >= ?)
                ORDER BY COALESCE(start_time_utc, start_time) ASC
                LIMIT ?
                """,
                (cutoff, limit),
            ).fetchall()
        else:
            rows = con.execute(
                """
                SELECT * FROM matches
                WHERE status='open'
                  AND COALESCE(NULLIF(LOWER(sport), ''), 'other')=?
                  AND (COALESCE(start_time_utc, start_time) IS NULL OR COALESCE(start_time_utc, start_time) >= ?)
                ORDER BY COALESCE(start_time_utc, start_time) ASC
                LIMIT ?
                """,
                (sport, cutoff, limit),
            ).fetchall()
    return [_row_to_match(bot, row) for row in rows]


def _summary(bot) -> dict[str, Any]:
    try:
        sports = bot.get_open_sports()
    except Exception:
        sports = []
    try:
        backtest = bot.run_ai_line_backtest(500) if hasattr(bot, "run_ai_line_backtest") else None
    except Exception:
        backtest = None
    return {
        "version": VERSION,
        "now": datetime.now(timezone.utc).isoformat(),
        "sports": [{"sport": sport, "count": count} for sport, count in sports],
        "backtest": backtest,
        "ai_line": getattr(bot, "_AI_LINE_APPLIED", False),
        "theme": getattr(bot, "_PRETTY_THEME_APPLIED", False),
    }


def _index_html() -> str:
    return """<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <script src="https://telegram.org/js/telegram-web-app.js"></script>
  <title>Predictor Bot</title>
  <style>
    :root { color-scheme: dark; --bg:#0b1016; --panel:#111923; --panel2:#162231; --text:#ecf3fb; --muted:#91a4b8; --line:#263546; --accent:#56c2ff; --good:#6be49d; --bad:#ff7272; --warn:#ffd166; }
    * { box-sizing:border-box; }
    body { margin:0; font-family: Inter, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background:var(--bg); color:var(--text); }
    .shell { width:min(1180px, 100%); margin:0 auto; padding:16px; }
    header { display:flex; justify-content:space-between; gap:16px; align-items:flex-start; padding:8px 0 14px; border-bottom:1px solid var(--line); }
    h1 { margin:0; font-size:22px; font-weight:750; }
    .sub { color:var(--muted); font-size:13px; margin-top:4px; }
    .tabs { display:flex; gap:8px; margin:16px 0; overflow-x:auto; }
    button { border:1px solid var(--line); background:var(--panel); color:var(--text); border-radius:8px; padding:9px 12px; font-size:14px; cursor:pointer; white-space:nowrap; }
    button.active { border-color:var(--accent); color:#071018; background:var(--accent); font-weight:700; }
    .grid { display:grid; grid-template-columns: 1.35fr .85fr; gap:14px; align-items:start; }
    .panel { background:var(--panel); border:1px solid var(--line); border-radius:8px; overflow:hidden; }
    .panel h2 { margin:0; padding:13px 14px; font-size:15px; border-bottom:1px solid var(--line); }
    .matches { display:grid; gap:0; }
    .match { display:grid; grid-template-columns: 1fr auto; gap:12px; padding:13px 14px; border-bottom:1px solid var(--line); }
    .match:last-child { border-bottom:0; }
    .title { font-weight:720; line-height:1.25; }
    .meta { color:var(--muted); font-size:12px; margin-top:4px; }
    .odds { display:grid; grid-template-columns:repeat(3, minmax(54px,1fr)); gap:6px; min-width:190px; }
    .odd { background:var(--panel2); border:1px solid var(--line); border-radius:7px; padding:7px 8px; text-align:center; }
    .odd span { display:block; color:var(--muted); font-size:11px; }
    .odd b { display:block; font-size:15px; margin-top:2px; }
    .stats { display:grid; grid-template-columns: repeat(4, minmax(0,1fr)); gap:8px; padding:12px; }
    .stat { background:var(--panel2); border:1px solid var(--line); border-radius:8px; padding:10px; }
    .stat span { color:var(--muted); font-size:12px; display:block; }
    .stat b { font-size:21px; display:block; margin-top:5px; }
    .list { padding:0 12px 12px; }
    .row { display:flex; justify-content:space-between; gap:10px; padding:9px 0; border-bottom:1px solid var(--line); font-size:13px; }
    .row:last-child { border-bottom:0; }
    .good { color:var(--good); } .bad { color:var(--bad); } .warn { color:var(--warn); }
    .empty { color:var(--muted); padding:18px 14px; }
    @media (max-width: 820px) { .grid { grid-template-columns:1fr; } header { display:block; } .match { grid-template-columns:1fr; } .odds { min-width:0; } .stats { grid-template-columns:repeat(2,1fr); } }
  </style>
</head>
<body>
  <div class="shell">
    <header>
      <div><h1>Predictor Bot</h1><div class="sub" id="status">Загрузка</div></div>
      <button id="refresh">Обновить</button>
    </header>
    <div class="tabs" id="tabs"></div>
    <div class="grid">
      <section class="panel"><h2>Активные матчи</h2><div class="matches" id="matches"></div></section>
      <aside class="panel"><h2>Backtest AI-линии</h2><div class="stats" id="btStats"></div><div class="list" id="calibration"></div></aside>
    </div>
  </div>
<script>
const tg = window.Telegram?.WebApp; if (tg) { tg.ready(); tg.expand(); }
let currentSport = 'all';
const fmtPct = v => v == null ? '—' : `${Math.round(v*1000)/10}%`;
const fmtOdd = v => v == null ? '—' : Number(v).toFixed(2);
async function api(path){ const r = await fetch(path, {cache:'no-store'}); if(!r.ok) throw new Error(path); return await r.json(); }
function sportLabel(s){ return ({football:'⚽ Футбол', hockey:'🏒 Хоккей', nhl:'🏒 Хоккей', all:'Все'})[s] || s; }
function renderTabs(sports){
  const el = document.getElementById('tabs');
  const total = sports.reduce((a,x)=>a+x.count,0);
  const items = [{sport:'all', count:total}, ...sports];
  el.innerHTML = items.map(x=>`<button class="${x.sport===currentSport?'active':''}" data-sport="${x.sport}">${sportLabel(x.sport)} · ${x.count}</button>`).join('');
  el.querySelectorAll('button').forEach(b=>b.onclick=()=>{currentSport=b.dataset.sport; load();});
}
function renderMatches(items){
  const el = document.getElementById('matches');
  if(!items.length){ el.innerHTML = '<div class="empty">Активных матчей нет</div>'; return; }
  el.innerHTML = items.map(m=>`
    <article class="match">
      <div>
        <div class="title">${m.title || 'Матч'}</div>
        <div class="meta">${m.league || '—'} · ${m.display_time || '—'} · голосов ${m.votes.total}</div>
        <div class="meta">AI: П1 ${fmtPct(m.probabilities['1'])} · X ${fmtPct(m.probabilities.X)} · П2 ${fmtPct(m.probabilities['2'])}</div>
      </div>
      <div class="odds">
        <div class="odd"><span>П1</span><b>${fmtOdd(m.odds['1'])}</b></div>
        <div class="odd"><span>X</span><b>${fmtOdd(m.odds.X)}</b></div>
        <div class="odd"><span>П2</span><b>${fmtOdd(m.odds['2'])}</b></div>
      </div>
    </article>`).join('');
}
function renderBacktest(bt){
  const stats = document.getElementById('btStats');
  if(!bt || !bt.matches){ stats.innerHTML = '<div class="empty">Нет закрытых матчей</div>'; document.getElementById('calibration').innerHTML=''; return; }
  const roi = bt.virtual_roi == null ? '—' : `${(bt.virtual_roi*100).toFixed(1)}%`;
  stats.innerHTML = `
    <div class="stat"><span>Матчи</span><b>${bt.matches}</b></div>
    <div class="stat"><span>Точность</span><b>${fmtPct(bt.accuracy)}</b></div>
    <div class="stat"><span>Brier</span><b>${bt.brier}</b></div>
    <div class="stat"><span>ROI</span><b class="${bt.virtual_roi>=0?'good':'bad'}">${roi}</b></div>`;
  const cal = document.getElementById('calibration');
  cal.innerHTML = (bt.calibration || []).map(x=>`<div class="row"><span>${x.bucket} · n=${x.matches}</span><b>${fmtPct(x.accuracy)} / ${fmtPct(x.avg_confidence)}</b></div>`).join('');
}
async function load(){
  document.getElementById('status').textContent = 'Обновление';
  const [summary, matches] = await Promise.all([api('/api/summary'), api(`/api/matches?sport=${encodeURIComponent(currentSport)}`)]);
  renderTabs(summary.sports || []); renderMatches(matches.items || []); renderBacktest(summary.backtest);
  document.getElementById('status').textContent = `AI ${summary.ai_line ? 'ON' : 'OFF'} · ${new Date(summary.now).toLocaleTimeString()}`;
}
document.getElementById('refresh').onclick = load;
load().catch(e => { document.getElementById('status').textContent = 'Ошибка загрузки'; console.error(e); });
</script>
</body>
</html>"""


def apply(bot) -> None:
    if getattr(bot, "_MINI_APP_APPLIED", False):
        return

    try:
        import line_backtest

        line_backtest.apply(bot)
    except Exception as exc:
        logger = getattr(bot, "logger", None)
        if logger:
            logger.exception("line_backtest apply failed from mini_app: %s", exc)

    WebAppInfo = None
    try:
        from aiogram.types import WebAppInfo as _WebAppInfo

        WebAppInfo = _WebAppInfo
    except Exception:
        WebAppInfo = None

    async def index(_request):
        return bot.web.Response(text="ok", content_type="text/plain; charset=utf-8")

    async def app_page(_request):
        return bot.web.Response(text=_index_html(), content_type="text/html; charset=utf-8")

    async def api_summary(_request):
        return _json_response(bot, _summary(bot))

    async def api_matches(request):
        sport = request.query.get("sport", "all")
        limit = int(request.query.get("limit", "80") or "80")
        return _json_response(bot, {"items": _active_matches(bot, sport, limit)})

    async def api_match(request):
        match_id = int(request.match_info.get("match_id", "0") or "0")
        row = bot.get_match(match_id)
        if not row:
            return _json_response(bot, {"error": "not_found"})
        return _json_response(bot, _row_to_match(bot, row))

    async def api_backtest(request):
        limit = int(request.query.get("limit", "500") or "500")
        if not hasattr(bot, "run_ai_line_backtest"):
            return _json_response(bot, {"error": "backtest_unavailable"})
        return _json_response(bot, bot.run_ai_line_backtest(limit))

    async def start_web_server():
        if bot.PORT <= 0:
            return
        app = bot.web.Application()
        app.router.add_get("/", index)
        app.router.add_get("/health", index)
        app.router.add_get("/app", app_page)
        app.router.add_get("/api/summary", api_summary)
        app.router.add_get("/api/matches", api_matches)
        app.router.add_get(r"/api/matches/{match_id:\d+}", api_match)
        app.router.add_get("/api/backtest", api_backtest)

        runner = bot.web.AppRunner(app)
        await runner.setup()
        site = bot.web.TCPSite(runner, host="0.0.0.0", port=bot.PORT)
        await site.start()
        bot.logger.info("mini app web server started on 0.0.0.0:%s", bot.PORT)
        while True:
            await bot.asyncio.sleep(3600)

    def app_keyboard():
        base = _public_base_url()
        url = f"{base}/app" if base else ""
        if url and WebAppInfo is not None:
            return bot.InlineKeyboardMarkup(inline_keyboard=[[
                bot.InlineKeyboardButton(text="Открыть Mini App", web_app=WebAppInfo(url=url))
            ]])
        if url:
            return bot.InlineKeyboardMarkup(inline_keyboard=[[
                bot.InlineKeyboardButton(text="Открыть Mini App", url=url)
            ]])
        return None

    @bot.dp.message(bot.Command("app"))
    @bot.dp.message(bot.Command("miniapp"))
    @bot.dp.message(bot.F.text == MINI_APP_BUTTON)
    async def mini_app_cmd(m: bot.Message):
        base = _public_base_url()
        kb = app_keyboard()
        if kb:
            return await m.answer("<b>Mini App</b>", reply_markup=kb)
        await m.answer(
            "Mini App включен на /app. Добавь в Render env MINI_APP_URL=https://адрес-сервиса.onrender.com",
            reply_markup=bot.main_menu(),
        )

    def main_menu():
        WebAppButton = None
        base = _public_base_url()
        url = f"{base}/app" if base and WebAppInfo is not None else ""
        if url:
            WebAppButton = bot.KeyboardButton(text=MINI_APP_BUTTON, web_app=WebAppInfo(url=url))
        rows = [
            [bot.KeyboardButton(text=bot.BTN_ACTIVE), bot.KeyboardButton(text=bot.BTN_TODAY)],
            [bot.KeyboardButton(text=bot.BTN_FIND_MATCH), bot.KeyboardButton(text=bot.BTN_MY)],
            [bot.KeyboardButton(text=bot.BTN_LB), bot.KeyboardButton(text=bot.BTN_PROFILE)],
            [WebAppButton or bot.KeyboardButton(text=MINI_APP_BUTTON), bot.KeyboardButton(text=bot.BTN_HELP)],
        ]
        return bot.ReplyKeyboardMarkup(
            keyboard=rows,
            resize_keyboard=True,
            input_field_placeholder="Матчи, прогнозы, Mini App",
        )

    bot.start_web_server = start_web_server
    bot.main_menu = main_menu
    bot._MINI_APP_APPLIED = True
    print(f"{VERSION}_APPLIED")
