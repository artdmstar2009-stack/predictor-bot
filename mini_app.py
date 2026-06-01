from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qsl

VERSION = "MINI_APP_V2"
MINI_APP_BUTTON = "📱 Mini App"
PICKS = ("1", "X", "2")


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
    if raw.endswith("/app"):
        raw = raw[:-4]
    return raw.rstrip("/")


def _json_response(bot, data: Any, status: int = 200):
    return bot.web.Response(
        status=status,
        text=json.dumps(data, ensure_ascii=False, default=str),
        content_type="application/json",
        charset="utf-8",
    )


def _text_response(bot, text: str, content_type: str):
    return bot.web.Response(text=text, content_type=content_type, charset="utf-8")


def _int_query(value: str | None, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value or default)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(number, maximum))


def _verify_init_data(bot, init_data: str) -> tuple[dict[str, Any] | None, str]:
    init_data = (init_data or "").strip()
    if not init_data:
        return None, "Открой Mini App через Telegram."

    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    their_hash = (pairs.pop("hash", "") or "").strip()
    if not their_hash:
        return None, "Telegram не передал подпись пользователя."

    token = (getattr(bot, "BOT_TOKEN", "") or os.getenv("BOT_TOKEN") or "").strip()
    if not token:
        return None, "BOT_TOKEN не настроен."

    data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(pairs.items()))
    secret_key = hmac.new(b"WebAppData", token.encode("utf-8"), hashlib.sha256).digest()
    calc_hash = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc_hash, their_hash):
        return None, "Не удалось проверить Telegram-подпись."

    max_age = int(os.getenv("MINI_APP_AUTH_MAX_AGE", "86400") or "0")
    if max_age > 0:
        try:
            auth_date = int(pairs.get("auth_date", "0") or "0")
        except ValueError:
            auth_date = 0
        if auth_date <= 0 or time.time() - auth_date > max_age:
            return None, "Сессия Mini App устарела. Открой её заново."

    try:
        user = json.loads(pairs.get("user", "{}") or "{}")
    except json.JSONDecodeError:
        user = {}
    if not user.get("id"):
        return None, "Telegram не передал пользователя."
    return user, ""


def _request_init_data(request, body: dict[str, Any] | None = None) -> str:
    return (
        request.headers.get("X-Telegram-Init-Data")
        or request.headers.get("Telegram-Init-Data")
        or (body or {}).get("initData")
        or ""
    )


def _upsert_user(bot, user: dict[str, Any]) -> int:
    user_id = int(user["id"])
    now = bot.iso(bot.now_utc())
    with bot.db() as con:
        con.execute(
            """
            INSERT INTO users(user_id, username, first_name, last_name, updated_at)
            VALUES(?,?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET
              username=excluded.username,
              first_name=excluded.first_name,
              last_name=excluded.last_name,
              updated_at=excluded.updated_at
            """,
            (
                user_id,
                user.get("username") or "",
                user.get("first_name") or "",
                user.get("last_name") or "",
                now,
            ),
        )
        con.execute("INSERT OR IGNORE INTO scores(user_id, updated_at) VALUES(?,?)", (user_id, now))
        con.commit()
    return user_id


def _auth_user(bot, request, body: dict[str, Any] | None = None) -> tuple[int | None, dict[str, Any] | None, str]:
    user, error = _verify_init_data(bot, _request_init_data(request, body))
    if not user:
        return None, None, error
    try:
        user_id = _upsert_user(bot, user)
    except Exception as exc:
        logger = getattr(bot, "logger", None)
        if logger:
            logger.exception("mini app user upsert failed: %s", exc)
        return None, None, "Не удалось сохранить пользователя."
    return user_id, user, ""


def _available_picks(row, priced: dict[str, Any]) -> list[str]:
    sport = (row["sport"] or "").lower()
    has_draw = (
        priced.get("prob_x") is not None
        or priced.get("odds_x") is not None
        or row["odds_x"] is not None
        or sport not in ("hockey", "nhl")
    )
    return ["1", "X", "2"] if has_draw else ["1", "2"]


def _row_to_match(bot, row, user_id: int | None = None) -> dict[str, Any]:
    try:
        priced = bot.ai_odds_for_match(dict(row))
    except Exception:
        priced = {}
    stats = bot.match_stats(int(row["id"])) if hasattr(bot, "match_stats") else {"1": 0, "X": 0, "2": 0}
    total_votes = int(stats.get("1", 0) + stats.get("X", 0) + stats.get("2", 0))
    start_value = row["start_time_utc"] or row["start_time"]
    my_pick = None
    if user_id and hasattr(bot, "get_my_pick"):
        try:
            my_pick = bot.get_my_pick(user_id, int(row["id"]))
        except Exception:
            my_pick = None
    allowed = True
    why = ""
    if hasattr(bot, "can_predict"):
        try:
            allowed, why = bot.can_predict(row)
        except Exception:
            allowed, why = True, ""
    return {
        "id": int(row["id"]),
        "title": row["title"],
        "sport": row["sport"] or "other",
        "league": row["league"] or "",
        "status": row["status"],
        "result": row["result"],
        "start_time": start_value,
        "display_time": bot._pretty_time(start_value or "") if hasattr(bot, "_pretty_time") else start_value,
        "can_predict": bool(allowed),
        "blocked_reason": why,
        "my_pick": my_pick,
        "available_picks": _available_picks(row, priced),
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


def _active_matches(bot, sport: str = "all", limit: int = 80, query: str = "", user_id: int | None = None) -> list[dict[str, Any]]:
    sport = (sport or "all").lower()
    limit = max(1, min(int(limit or 80), 200))
    query = (query or "").strip().lower()
    cutoff_fn = getattr(bot, "_today_msk_start_utc", None)
    cutoff = bot.iso(cutoff_fn()) if callable(cutoff_fn) else bot.iso(bot.now_utc())
    where = [
        "status='open'",
        "(COALESCE(start_time_utc, start_time) IS NULL OR COALESCE(start_time_utc, start_time) >= ?)",
    ]
    params: list[Any] = [cutoff]
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
            ORDER BY COALESCE(start_time_utc, start_time) ASC
            LIMIT ?
            """,
            params,
        ).fetchall()
    return [_row_to_match(bot, row, user_id) for row in rows]


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


def _profile(bot, user_id: int) -> dict[str, Any]:
    row = bot.get_score_row(user_id) if hasattr(bot, "get_score_row") else None
    if not row:
        return {"user_id": user_id, "points": 0, "balance": 0, "correct": 0, "total": 0, "streak": 0, "best_streak": 0}
    return {
        "user_id": user_id,
        "name": bot.pretty_user(user_id) if hasattr(bot, "pretty_user") else str(user_id),
        "points": int(row["points"] or 0),
        "balance": int(row["balance"] or 0),
        "correct": int(row["correct"] or 0),
        "total": int(row["total"] or 0),
        "streak": int(row["streak"] or 0),
        "best_streak": int(row["best_streak"] or 0),
    }


def _my_predictions(bot, user_id: int, limit: int = 50) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit or 50), 100))
    with bot.db() as con:
        rows = con.execute(
            """
            SELECT v.match_id, v.pick, v.created_at, v.stake, v.odds,
                   m.title, m.status, m.result, m.sport, m.league, m.start_time_utc, m.start_time
            FROM votes v
            JOIN matches m ON m.id = v.match_id
            WHERE v.user_id=?
            ORDER BY v.created_at DESC
            LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def _leaderboard(bot) -> dict[str, Any]:
    def pack(rows):
        return [
            {"place": i, "user_id": uid, "name": bot.pretty_user(uid) if hasattr(bot, "pretty_user") else str(uid), "points": pts}
            for i, (uid, pts) in enumerate(rows or [], 1)
        ]

    try:
        season = bot.season_top(10)
    except Exception:
        season = []
    try:
        week = bot.top_points_since(bot.start_of_week_utc(bot.now_utc()), 10)
    except Exception:
        week = []
    try:
        month = bot.top_points_since(bot.start_of_month_utc(bot.now_utc()), 10)
    except Exception:
        month = []
    return {"season": pack(season), "week": pack(week), "month": pack(month)}


def _place_prediction(bot, user_id: int, match_id: int, pick: str) -> tuple[bool, str, dict[str, Any] | None]:
    pick = (pick or "").upper()
    if pick not in PICKS:
        return False, "Неверный исход.", None
    match = bot.get_match(match_id)
    if not match:
        return False, "Матч не найден.", None
    ok, why = bot.can_predict(match) if hasattr(bot, "can_predict") else (True, "")
    if not ok:
        return False, why or "Прогнозы закрыты.", None
    try:
        priced = bot.ai_odds_for_match(dict(match))
    except Exception:
        priced = {}
    if pick not in _available_picks(match, priced):
        return False, "Этот исход недоступен для матча.", None
    with bot.db() as con:
        con.execute(
            "INSERT OR REPLACE INTO votes(user_id, match_id, pick, created_at) VALUES(?,?,?,?)",
            (user_id, match_id, pick, bot.iso(bot.now_utc())),
        )
        con.commit()
    return True, "Прогноз принят.", _row_to_match(bot, match, user_id)


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
    .shell { width:min(1180px, 100%); margin:0 auto; padding:14px; }
    header { display:flex; justify-content:space-between; gap:12px; align-items:flex-start; padding:6px 0 12px; border-bottom:1px solid var(--line); }
    h1 { margin:0; font-size:22px; font-weight:750; }
    h2 { margin:0; padding:13px 14px; font-size:15px; border-bottom:1px solid var(--line); }
    .sub, .meta, .muted { color:var(--muted); }
    .sub { font-size:13px; margin-top:4px; }
    .tabs, .sports { display:flex; gap:8px; margin:12px 0; overflow-x:auto; }
    button, input { font:inherit; }
    button { border:1px solid var(--line); background:var(--panel); color:var(--text); border-radius:8px; padding:9px 12px; font-size:14px; cursor:pointer; white-space:nowrap; }
    button.active { border-color:var(--accent); color:#071018; background:var(--accent); font-weight:700; }
    button.good { border-color:rgba(107,228,157,.45); background:rgba(107,228,157,.13); }
    button.warn { border-color:rgba(255,209,102,.45); background:rgba(255,209,102,.13); }
    button:disabled { opacity:.5; cursor:not-allowed; }
    input { width:100%; border:1px solid var(--line); background:var(--panel); color:var(--text); border-radius:8px; padding:10px 12px; outline:none; }
    .grid { display:grid; grid-template-columns: 1.25fr .75fr; gap:14px; align-items:start; }
    .panel { background:var(--panel); border:1px solid var(--line); border-radius:8px; overflow:hidden; }
    .view { display:none; } .view.active { display:block; }
    .matches { display:grid; gap:0; }
    .match { display:grid; grid-template-columns: 1fr auto; gap:12px; padding:13px 14px; border-bottom:1px solid var(--line); }
    .match:last-child { border-bottom:0; }
    .title { font-weight:720; line-height:1.25; }
    .meta { font-size:12px; margin-top:4px; }
    .odds { display:grid; grid-template-columns:repeat(3, minmax(54px,1fr)); gap:6px; min-width:190px; }
    .odd, .stat { background:var(--panel2); border:1px solid var(--line); border-radius:7px; }
    .odd { padding:7px 8px; text-align:center; }
    .odd span, .stat span { display:block; color:var(--muted); font-size:11px; }
    .odd b { display:block; font-size:15px; margin-top:2px; }
    .picks { display:flex; gap:6px; margin-top:10px; flex-wrap:wrap; }
    .picked { border-color:var(--good); color:#071018; background:var(--good); font-weight:800; }
    .stats { display:grid; grid-template-columns: repeat(4, minmax(0,1fr)); gap:8px; padding:12px; }
    .stat { padding:10px; }
    .stat b { font-size:21px; display:block; margin-top:5px; }
    .list { padding:0 12px 12px; }
    .row { display:flex; justify-content:space-between; gap:10px; padding:10px 0; border-bottom:1px solid var(--line); font-size:13px; }
    .row:last-child { border-bottom:0; }
    .empty { color:var(--muted); padding:18px 14px; }
    .toast { position:fixed; left:14px; right:14px; bottom:14px; padding:12px 14px; border:1px solid var(--line); border-radius:8px; background:#182332; color:var(--text); display:none; box-shadow:0 12px 32px rgba(0,0,0,.35); }
    @media (max-width: 820px) { .grid { grid-template-columns:1fr; } header { display:block; } .match { grid-template-columns:1fr; } .odds { min-width:0; } .stats { grid-template-columns:repeat(2,1fr); } }
  </style>
</head>
<body>
  <div class="shell">
    <header>
      <div><h1>Predictor Bot</h1><div class="sub" id="status">Загрузка</div></div>
      <button id="refresh">Обновить</button>
    </header>
    <div class="tabs" id="mainTabs">
      <button class="active" data-view="matchesView">Матчи</button>
      <button data-view="myView">Мои</button>
      <button data-view="profileView">Профиль</button>
      <button data-view="leadersView">Лидеры</button>
      <button data-view="aiView">AI</button>
    </div>
    <section class="view active" id="matchesView">
      <input id="search" placeholder="Поиск матча" />
      <div class="sports" id="sports"></div>
      <div class="grid">
        <section class="panel"><h2>Активные матчи</h2><div class="matches" id="matches"></div></section>
        <aside class="panel"><h2>AI-линия</h2><div class="stats" id="quickStats"></div><div class="list" id="quickList"></div></aside>
      </div>
    </section>
    <section class="view" id="myView"><div class="panel"><h2>Мои прогнозы</h2><div class="list" id="myPredictions"></div></div></section>
    <section class="view" id="profileView"><div class="panel"><h2>Профиль</h2><div class="stats" id="profileStats"></div></div></section>
    <section class="view" id="leadersView"><div class="grid"><section class="panel"><h2>Сезон</h2><div class="list" id="seasonLeaders"></div></section><section class="panel"><h2>Неделя / месяц</h2><div class="list" id="periodLeaders"></div></section></div></section>
    <section class="view" id="aiView"><div class="panel"><h2>Backtest AI-линии</h2><div class="stats" id="btStats"></div><div class="list" id="calibration"></div></div></section>
  </div>
  <div class="toast" id="toast"></div>
<script>
const tg = window.Telegram?.WebApp;
if (tg) { tg.ready(); tg.expand(); }
const initData = tg?.initData || '';
let currentSport = 'all';
let currentView = 'matchesView';
let searchTimer = null;
const fmtPct = v => v == null ? '—' : `${Math.round(v*1000)/10}%`;
const fmtOdd = v => v == null ? '—' : Number(v).toFixed(2);
const pickLabel = p => ({'1':'П1','X':'X','2':'П2'})[p] || p;
const authHeaders = () => initData ? {'X-Telegram-Init-Data': initData} : {};
async function api(path, options={}){
  const headers = Object.assign({}, authHeaders(), options.headers || {});
  const r = await fetch(path, Object.assign({cache:'no-store', headers}, options));
  const data = await r.json().catch(()=>({error:'bad_json'}));
  if(!r.ok) throw new Error(data.error || data.message || path);
  return data;
}
function toast(text){ const el = document.getElementById('toast'); el.textContent = text; el.style.display='block'; clearTimeout(el._t); el._t=setTimeout(()=>el.style.display='none', 2400); }
function sportLabel(s){ return ({football:'⚽ Футбол', hockey:'🏒 Хоккей', nhl:'🏒 Хоккей', all:'Все'})[s] || s; }
function renderSports(sports){
  const el = document.getElementById('sports');
  const total = sports.reduce((a,x)=>a+x.count,0);
  const items = [{sport:'all', count:total}, ...sports];
  el.innerHTML = items.map(x=>`<button class="${x.sport===currentSport?'active':''}" data-sport="${x.sport}">${sportLabel(x.sport)} · ${x.count}</button>`).join('');
  el.querySelectorAll('button').forEach(b=>b.onclick=()=>{currentSport=b.dataset.sport; loadMatches();});
}
function pickButtons(m){
  return ['1','X','2'].map(p=>{
    const disabled = !initData || !m.can_predict || !(m.available_picks || []).includes(p);
    const cls = m.my_pick === p ? 'picked' : '';
    return `<button class="${cls}" data-pick="${p}" data-id="${m.id}" ${disabled?'disabled':''}>${pickLabel(p)}</button>`;
  }).join('');
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
        <div class="meta">${m.my_pick ? `Твой прогноз: ${pickLabel(m.my_pick)}` : (m.can_predict ? 'Прогноз открыт' : (m.blocked_reason || 'Прогноз закрыт'))}</div>
        <div class="picks">${pickButtons(m)}</div>
      </div>
      <div class="odds">
        <div class="odd"><span>П1</span><b>${fmtOdd(m.odds['1'])}</b></div>
        <div class="odd"><span>X</span><b>${fmtOdd(m.odds.X)}</b></div>
        <div class="odd"><span>П2</span><b>${fmtOdd(m.odds['2'])}</b></div>
      </div>
    </article>`).join('');
  el.querySelectorAll('button[data-pick]').forEach(b=>b.onclick=()=>sendPick(Number(b.dataset.id), b.dataset.pick));
}
function renderBacktest(bt){
  const stats = document.getElementById('btStats');
  const quick = document.getElementById('quickStats');
  if(!bt || !bt.matches){
    stats.innerHTML = quick.innerHTML = '<div class="empty">Нет закрытых матчей</div>';
    document.getElementById('calibration').innerHTML = document.getElementById('quickList').innerHTML = '';
    return;
  }
  const roi = bt.virtual_roi == null ? '—' : `${(bt.virtual_roi*100).toFixed(1)}%`;
  const html = `
    <div class="stat"><span>Матчи</span><b>${bt.matches}</b></div>
    <div class="stat"><span>Точность</span><b>${fmtPct(bt.accuracy)}</b></div>
    <div class="stat"><span>Brier</span><b>${bt.brier}</b></div>
    <div class="stat"><span>ROI</span><b>${roi}</b></div>`;
  stats.innerHTML = quick.innerHTML = html;
  const rows = (bt.calibration || []).map(x=>`<div class="row"><span>${x.bucket} · n=${x.matches}</span><b>${fmtPct(x.accuracy)} / ${fmtPct(x.avg_confidence)}</b></div>`).join('');
  document.getElementById('calibration').innerHTML = rows;
  document.getElementById('quickList').innerHTML = rows || '<div class="empty">Калибровки пока нет</div>';
}
function renderMine(items){
  const el = document.getElementById('myPredictions');
  if(!initData){ el.innerHTML = '<div class="empty">Открой Mini App через Telegram</div>'; return; }
  if(!items.length){ el.innerHTML = '<div class="empty">Прогнозов пока нет</div>'; return; }
  el.innerHTML = items.map(x=>`<div class="row"><span>${x.title}<br><span class="muted">${x.league || '—'} · ${x.status}</span></span><b>${pickLabel(x.pick)} ${x.result ? `→ ${x.result}` : ''}</b></div>`).join('');
}
function renderProfile(p){
  const el = document.getElementById('profileStats');
  if(!initData){ el.innerHTML = '<div class="empty">Открой Mini App через Telegram</div>'; return; }
  el.innerHTML = `
    <div class="stat"><span>Очки</span><b>${p.points || 0}</b></div>
    <div class="stat"><span>Баланс</span><b>${p.balance || 0}</b></div>
    <div class="stat"><span>Победы</span><b>${p.correct || 0}/${p.total || 0}</b></div>
    <div class="stat"><span>Серия</span><b>${p.streak || 0}</b></div>`;
}
function renderLeaders(data){
  const row = x => `<div class="row"><span>${x.place}. ${x.name}</span><b>${x.points}</b></div>`;
  document.getElementById('seasonLeaders').innerHTML = (data.season || []).map(row).join('') || '<div class="empty">Пусто</div>';
  document.getElementById('periodLeaders').innerHTML = '<div class="muted" style="padding:10px 0">Неделя</div>' + ((data.week || []).map(row).join('') || '<div class="empty">Пусто</div>') + '<div class="muted" style="padding:10px 0">Месяц</div>' + ((data.month || []).map(row).join('') || '<div class="empty">Пусто</div>');
}
async function sendPick(matchId, pick){
  if(!initData){ toast('Открой через Telegram'); return; }
  try{
    tg?.HapticFeedback?.impactOccurred('light');
    await api('/api/predict', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({match_id:matchId, pick, initData})});
    toast('Прогноз принят');
    await Promise.all([loadMatches(), loadMine(), loadProfile()]);
  }catch(e){ toast(e.message || 'Ошибка прогноза'); }
}
async function loadSummary(){ const summary = await api('/api/summary'); renderSports(summary.sports || []); renderBacktest(summary.backtest); document.getElementById('status').textContent = `AI ${summary.ai_line ? 'ON' : 'OFF'} · ${new Date(summary.now).toLocaleTimeString()}`; }
async function loadMatches(){ const q = document.getElementById('search').value || ''; const data = await api(`/api/matches?sport=${encodeURIComponent(currentSport)}&q=${encodeURIComponent(q)}`); renderMatches(data.items || []); }
async function loadMine(){ if(!initData) return renderMine([]); const data = await api('/api/me/predictions'); renderMine(data.items || []); }
async function loadProfile(){ if(!initData) return renderProfile({}); const data = await api('/api/me'); renderProfile(data.profile || {}); }
async function loadLeaders(){ const data = await api('/api/leaderboard'); renderLeaders(data); }
async function load(){ await Promise.all([loadSummary(), loadMatches(), loadMine(), loadProfile(), loadLeaders()]); if(!initData) toast('Для прогнозов открой Mini App из Telegram'); }
document.getElementById('refresh').onclick = load;
document.getElementById('search').oninput = () => { clearTimeout(searchTimer); searchTimer = setTimeout(loadMatches, 250); };
document.querySelectorAll('#mainTabs button').forEach(b=>b.onclick=()=>{ document.querySelectorAll('#mainTabs button').forEach(x=>x.classList.remove('active')); b.classList.add('active'); document.querySelectorAll('.view').forEach(x=>x.classList.remove('active')); document.getElementById(b.dataset.view).classList.add('active'); currentView=b.dataset.view; });
load().catch(e => { document.getElementById('status').textContent = 'Ошибка загрузки'; toast(e.message || 'Ошибка загрузки'); console.error(e); });
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
        return _text_response(bot, "ok", "text/plain")

    async def app_page(_request):
        return _text_response(bot, _index_html(), "text/html")

    async def api_summary(_request):
        return _json_response(bot, _summary(bot))

    async def api_matches(request):
        user_id, _, _ = _auth_user(bot, request)
        sport = request.query.get("sport", "all")
        query = request.query.get("q", "")
        limit = _int_query(request.query.get("limit"), 80, 1, 200)
        return _json_response(bot, {"items": _active_matches(bot, sport, limit, query, user_id)})

    async def api_match(request):
        user_id, _, _ = _auth_user(bot, request)
        match_id = int(request.match_info.get("match_id", "0") or "0")
        row = bot.get_match(match_id)
        if not row:
            return _json_response(bot, {"error": "not_found"}, status=404)
        return _json_response(bot, _row_to_match(bot, row, user_id))

    async def api_predict(request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        user_id, _, error = _auth_user(bot, request, body)
        if not user_id:
            return _json_response(bot, {"error": error}, status=401)
        match_id = _int_query(str(body.get("match_id", "0")), 0, 0, 10**12)
        pick = str(body.get("pick", "")).upper()
        ok, message, match = _place_prediction(bot, user_id, match_id, pick)
        if not ok:
            return _json_response(bot, {"error": message}, status=400)
        return _json_response(bot, {"ok": True, "message": message, "match": match, "profile": _profile(bot, user_id)})

    async def api_me(request):
        user_id, user, error = _auth_user(bot, request)
        if not user_id:
            return _json_response(bot, {"error": error}, status=401)
        return _json_response(bot, {"user": user, "profile": _profile(bot, user_id)})

    async def api_my_predictions(request):
        user_id, _, error = _auth_user(bot, request)
        if not user_id:
            return _json_response(bot, {"error": error}, status=401)
        limit = _int_query(request.query.get("limit"), 50, 1, 100)
        return _json_response(bot, {"items": _my_predictions(bot, user_id, limit)})

    async def api_leaderboard(_request):
        return _json_response(bot, _leaderboard(bot))

    async def api_backtest(request):
        limit = _int_query(request.query.get("limit"), 500, 1, 2000)
        if not hasattr(bot, "run_ai_line_backtest"):
            return _json_response(bot, {"error": "backtest_unavailable"}, status=503)
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
        app.router.add_post("/api/predict", api_predict)
        app.router.add_get("/api/me", api_me)
        app.router.add_get("/api/me/predictions", api_my_predictions)
        app.router.add_get("/api/leaderboard", api_leaderboard)
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
        kb = app_keyboard()
        if kb:
            return await m.answer("<b>Predictor Bot</b>", reply_markup=kb)
        await m.answer(
            "Mini App включен на /app. Добавь в Render env MINI_APP_URL=https://адрес-сервиса.onrender.com",
            reply_markup=bot.main_menu(),
        )

    def main_menu():
        base = _public_base_url()
        url = f"{base}/app" if base and WebAppInfo is not None else ""
        button = bot.KeyboardButton(text=MINI_APP_BUTTON, web_app=WebAppInfo(url=url)) if url else bot.KeyboardButton(text=MINI_APP_BUTTON)
        return bot.ReplyKeyboardMarkup(
            keyboard=[[button]],
            resize_keyboard=True,
            input_field_placeholder="Открой Mini App",
        )

    bot.start_web_server = start_web_server
    bot.main_menu = main_menu
    bot._MINI_APP_APPLIED = True
    print(f"{VERSION}_APPLIED")
