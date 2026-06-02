from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from typing import Any

VERSION = "GROWTH_FEATURES_V1"
PICKS = ("1", "X", "2")

RANKS = [
    {"code": "rookie", "title": "Новичок", "min_points": 0, "min_total": 0},
    {"code": "scout", "title": "Скаут", "min_points": 10, "min_total": 5},
    {"code": "analyst", "title": "Аналитик", "min_points": 30, "min_total": 15},
    {"code": "pro", "title": "Профи", "min_points": 75, "min_total": 30},
    {"code": "legend", "title": "Легенда", "min_points": 150, "min_total": 60},
]


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


def _now_iso(app: Any) -> str:
    return app.iso(app.now_utc())


def _normalize_code(code: str) -> str:
    return "".join(ch for ch in str(code or "").upper().strip() if ch.isalnum() or ch in ("-", "_"))[:40]


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


def _ensure_schema(app: Any) -> None:
    try:
        app.init_db()
    except Exception:
        logger = _log(app)
        if logger:
            logger.exception("growth features: base init failed")

    with app.db() as con:
        cur = con.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS promo_codes (
                code TEXT PRIMARY KEY,
                amount INTEGER NOT NULL,
                max_uses INTEGER DEFAULT 0,
                used_count INTEGER DEFAULT 0,
                expires_at TEXT,
                is_active INTEGER DEFAULT 1,
                created_by INTEGER DEFAULT 0,
                created_at TEXT
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS promo_redemptions (
                code TEXT,
                user_id INTEGER,
                amount INTEGER,
                created_at TEXT,
                UNIQUE(code, user_id)
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS ix_promo_redemptions_user ON promo_redemptions(user_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_promo_redemptions_code ON promo_redemptions(code)")
        con.commit()


def create_promo_code(app: Any, code: str, amount: int, max_uses: int = 0, created_by: int = 0, expires_at: str | None = None) -> tuple[bool, str]:
    code = _normalize_code(code)
    amount = int(amount or 0)
    max_uses = max(0, int(max_uses or 0))
    if not code:
        return False, "Код пустой."
    if amount <= 0:
        return False, "Сумма промокода должна быть больше 0."
    with app.db() as con:
        con.execute(
            """
            INSERT INTO promo_codes(code, amount, max_uses, used_count, expires_at, is_active, created_by, created_at)
            VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(code) DO UPDATE SET
              amount=excluded.amount,
              max_uses=excluded.max_uses,
              expires_at=excluded.expires_at,
              is_active=1
            """,
            (code, amount, max_uses, 0, expires_at, 1, int(created_by or 0), _now_iso(app)),
        )
        con.commit()
    uses = "без лимита" if max_uses <= 0 else f"{max_uses} активаций"
    return True, f"Промокод {code} создан: +{amount}, {uses}."


def redeem_promo_code(app: Any, user_id: int, code: str) -> tuple[bool, str, dict[str, Any]]:
    code = _normalize_code(code)
    user_id = int(user_id or 0)
    if not code:
        return False, "Введи промокод.", {}
    if user_id <= 0:
        return False, "Пользователь не найден.", {}

    now = _now_iso(app)
    with app.db() as con:
        cur = con.cursor()
        try:
            cur.execute("BEGIN IMMEDIATE")
            promo = cur.execute("SELECT * FROM promo_codes WHERE code=?", (code,)).fetchone()
            if not promo or int(promo["is_active"] or 0) != 1:
                con.rollback()
                return False, "Промокод не найден или выключен.", {}

            expires_at = _parse_dt(promo["expires_at"])
            if expires_at and app.now_utc() > expires_at:
                con.rollback()
                return False, "Срок действия промокода истёк.", {}

            max_uses = int(promo["max_uses"] or 0)
            used_count = int(promo["used_count"] or 0)
            if max_uses > 0 and used_count >= max_uses:
                con.rollback()
                return False, "Промокод уже закончился.", {}

            amount = int(promo["amount"] or 0)
            try:
                cur.execute(
                    "INSERT INTO promo_redemptions(code, user_id, amount, created_at) VALUES(?,?,?,?)",
                    (code, user_id, amount, now),
                )
            except sqlite3.IntegrityError:
                con.rollback()
                return False, "Ты уже активировал этот промокод.", {}

            cur.execute("UPDATE promo_codes SET used_count=used_count+1 WHERE code=?", (code,))
            cur.execute(
                "INSERT OR IGNORE INTO scores(user_id, points, balance, correct, total, streak, best_streak, updated_at) VALUES(?,0,0,0,0,0,0,?)",
                (user_id, now),
            )
            cur.execute("UPDATE scores SET balance=COALESCE(balance,0)+?, updated_at=? WHERE user_id=?", (amount, now, user_id))
            balance_row = cur.execute("SELECT balance FROM scores WHERE user_id=?", (user_id,)).fetchone()
            con.commit()
        except Exception:
            con.rollback()
            raise

    balance = int(_row_value(balance_row, "balance", 0) or 0)
    return True, f"Промокод активирован: +{amount}. Баланс: {balance}.", {"balance": balance, "amount": amount, "code": code}


def list_promo_codes(app: Any, limit: int = 20) -> list[dict[str, Any]]:
    with app.db() as con:
        rows = con.execute(
            """
            SELECT code, amount, max_uses, used_count, expires_at, is_active, created_at
            FROM promo_codes
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
    return [dict(row) for row in rows]


def betting_stats(app: Any, user_id: int) -> dict[str, Any]:
    with app.db() as con:
        rows = con.execute(
            """
            SELECT v.pick, COALESCE(v.stake,0) AS stake, COALESCE(v.odds,0) AS odds,
                   m.status, m.result, COALESCE(NULLIF(LOWER(m.sport), ''), 'other') AS sport
            FROM votes v
            JOIN matches m ON m.id = v.match_id
            WHERE v.user_id=? AND COALESCE(v.stake,0) > 0
            """,
            (int(user_id),),
        ).fetchall()

    total_bets = len(rows)
    open_bets = 0
    settled_bets = 0
    won = 0
    lost = 0
    total_staked = 0
    settled_staked = 0
    total_payout = 0
    biggest_win = 0
    by_sport: dict[str, dict[str, int]] = {}

    for row in rows:
        stake = int(row["stake"] or 0)
        try:
            odds = float(row["odds"] or 0)
        except (TypeError, ValueError):
            odds = 0.0
        status = str(row["status"] or "")
        result = str(row["result"] or "")
        pick = str(row["pick"] or "")
        sport = str(row["sport"] or "other")
        total_staked += stake
        item = by_sport.setdefault(sport, {"bets": 0, "staked": 0, "payout": 0, "profit": 0})
        item["bets"] += 1
        item["staked"] += stake

        if status != "closed" or not result:
            open_bets += 1
            continue

        settled_bets += 1
        settled_staked += stake
        payout = int(round(stake * odds)) if pick == result and odds > 0 else 0
        profit = payout - stake
        total_payout += payout
        item["payout"] += payout
        item["profit"] += profit
        if payout > 0:
            won += 1
            biggest_win = max(biggest_win, profit)
        else:
            lost += 1

    profit = total_payout - settled_staked
    roi = round(profit / settled_staked, 4) if settled_staked > 0 else 0.0
    winrate = round(won / settled_bets, 4) if settled_bets > 0 else 0.0
    best_sport = None
    if by_sport:
        best_sport = max(by_sport.items(), key=lambda item: item[1]["profit"])[0]

    return {
        "total_bets": total_bets,
        "open_bets": open_bets,
        "settled_bets": settled_bets,
        "won": won,
        "lost": lost,
        "total_staked": total_staked,
        "settled_staked": settled_staked,
        "total_payout": total_payout,
        "profit": profit,
        "roi": roi,
        "winrate": winrate,
        "biggest_win": biggest_win,
        "best_sport": best_sport,
        "by_sport": by_sport,
    }


def prediction_profit(item: dict[str, Any]) -> dict[str, Any]:
    stake = int(item.get("stake") or 0)
    try:
        odds = float(item.get("odds") or 0)
    except (TypeError, ValueError):
        odds = 0.0
    status = str(item.get("status") or "")
    result = str(item.get("result") or "")
    pick = str(item.get("pick") or "")
    if stake <= 0:
        item.update({"payout": 0, "profit": None, "settled": status == "closed" and bool(result), "won": False})
        return item
    if status != "closed" or not result:
        item.update({"payout": int(round(stake * odds)) if odds > 0 else 0, "profit": None, "settled": False, "won": False})
        return item
    won = pick == result
    payout = int(round(stake * odds)) if won and odds > 0 else 0
    item.update({"payout": payout, "profit": payout - stake, "settled": True, "won": won})
    return item


def rank_for_user(profile: dict[str, Any], stats: dict[str, Any]) -> dict[str, Any]:
    points = int(profile.get("points") or 0)
    total = int(profile.get("total") or 0)
    current = RANKS[0]
    next_rank = None
    for rank in RANKS:
        if points >= int(rank["min_points"]) and total >= int(rank["min_total"]):
            current = rank
        elif next_rank is None:
            next_rank = rank
    if next_rank is None:
        progress = 1.0
        missing_points = 0
        missing_total = 0
    else:
        missing_points = max(0, int(next_rank["min_points"]) - points)
        missing_total = max(0, int(next_rank["min_total"]) - total)
        point_progress = 1.0 if int(next_rank["min_points"]) <= 0 else min(1.0, points / int(next_rank["min_points"]))
        total_progress = 1.0 if int(next_rank["min_total"]) <= 0 else min(1.0, total / int(next_rank["min_total"]))
        progress = round((point_progress + total_progress) / 2, 4)

    return {
        "code": current["code"],
        "title": current["title"],
        "progress": progress,
        "next_title": next_rank["title"] if next_rank else None,
        "missing_points": missing_points,
        "missing_predictions": missing_total,
        "points": points,
        "predictions": total,
        "roi": stats.get("roi", 0),
        "winrate": stats.get("winrate", 0),
    }


def _patch_mini_app(app: Any) -> None:
    if getattr(app, "_GROWTH_MINI_PATCHED", False):
        return
    try:
        import mini_app
    except Exception as exc:
        logger = _log(app)
        if logger:
            logger.exception("growth features: mini_app import failed: %s", exc)
        return

    original_profile = getattr(mini_app, "_profile", None)
    if callable(original_profile) and not getattr(original_profile, "_growth_wrapped", False):
        def patched_profile(bot: Any, user_id: int) -> dict[str, Any]:
            profile = dict(original_profile(bot, user_id) or {})
            stats = betting_stats(bot, int(user_id))
            profile["betting_stats"] = stats
            profile["rank"] = rank_for_user(profile, stats)
            return profile

        patched_profile._growth_wrapped = True
        mini_app._profile = patched_profile

    original_my_predictions = getattr(mini_app, "_my_predictions", None)
    if callable(original_my_predictions) and not getattr(original_my_predictions, "_growth_wrapped", False):
        def patched_my_predictions(bot: Any, user_id: int, limit: int = 50) -> list[dict[str, Any]]:
            items = [dict(item) for item in (original_my_predictions(bot, user_id, limit) or [])]
            return [prediction_profit(item) for item in items]

        patched_my_predictions._growth_wrapped = True
        mini_app._my_predictions = patched_my_predictions

    original_index = getattr(mini_app, "_index_html", None)
    if callable(original_index) and not getattr(original_index, "_growth_wrapped", False):
        def patched_index_html() -> str:
            return _patch_html(original_index())

        patched_index_html._growth_wrapped = True
        mini_app._index_html = patched_index_html

    _patch_start_web_server(app, mini_app)
    app._GROWTH_MINI_PATCHED = True


GROWTH_CSS = r"""
    .rank-card { grid-column:1/-1; min-height:auto; }
    .rank-head { display:flex; justify-content:space-between; gap:10px; align-items:center; }
    .rank-title { font-size:18px; color:var(--accent2); font-weight:900; }
    .rank-bar { height:8px; border-radius:8px; background:rgba(255,255,255,.08); overflow:hidden; margin-top:10px; }
    .rank-fill { height:100%; border-radius:8px; background:linear-gradient(90deg,var(--accent),var(--accent2)); }
    .promo-box { grid-column:1/-1; min-height:auto; }
    .promo-row { display:grid; grid-template-columns:1fr auto; gap:8px; margin-top:10px; }
    .promo-row input { height:40px; min-width:0; }
    .promo-row button { min-height:40px; border-color:rgba(110,231,183,.35); background:rgba(110,231,183,.1); }
    .profit-good { color:var(--accent) !important; }
    .profit-bad { color:var(--bad) !important; }
    .history-tag { color:var(--muted); font-size:11px; }
"""

GROWTH_JS = r"""
const growthEsc = value => String(value ?? '').replace(/[&<>'"]/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[ch]));
const growthMoney = value => Number(value || 0).toLocaleString('ru-RU');
const growthPct = value => `${Math.round(Number(value || 0) * 1000) / 10}%`;
function growthProfit(value){
  if(value == null) return '<span class="history-tag">ожидает</span>';
  const cls = Number(value) >= 0 ? 'profit-good' : 'profit-bad';
  const sign = Number(value) > 0 ? '+' : '';
  return `<b class="${cls}">${sign}${growthMoney(value)}</b>`;
}
async function redeemPromo(){
  const input = document.getElementById('promoCode');
  const code = (input?.value || '').trim();
  if(!code){ toast('Введи промокод'); return; }
  try{
    const data = await api('/api/promo/redeem', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({code, initData})});
    toast(data.message || 'Промокод активирован');
    if(input) input.value = '';
    if(data.profile) renderProfile(data.profile);
    await Promise.all([loadProfile(), loadMine()]);
  }catch(e){ const msg=e.message || 'Промокод не активирован'; tg?.showAlert?.(msg); toast(msg); }
}
function renderProfile(p){
  try { profileState = Object.assign(beautyProfileState(), p || {}); if(Array.isArray(p?.stake_presets)) profileState.stake_presets = p.stake_presets; } catch(_e) {}
  if(typeof setBalanceChip === 'function') setBalanceChip(p?.balance);
  const el = document.getElementById('profileStats');
  if(!initData){ el.innerHTML = '<div class="empty">Открой Mini App через Telegram</div>'; return; }
  const total = Number(p?.total || 0);
  const correct = Number(p?.correct || 0);
  const winrate = total ? `${Math.round(correct / total * 100)}%` : '—';
  const ref = p?.referral || {};
  const season = p?.season || {};
  const stats = p?.betting_stats || {};
  const rank = p?.rank || {};
  const profitCls = Number(stats.profit || 0) >= 0 ? 'good' : '';
  const refBlock = ref.link
    ? `<div class="metric profile-wide"><span>Реферальная ссылка</span><b>${growthEsc(ref.link)}</b><button id="copyRef">Скопировать ссылку</button><span>Приглашено: ${Number(ref.invited || 0)} · бонусы: ${growthMoney(ref.bonus || 0)}</span></div>`
    : `<div class="metric profile-wide"><span>Реферальная ссылка</span><b>${growthEsc(ref.code || 'ref')}</b><span>Добавь BOT_USERNAME в Render, чтобы ссылка стала полной.</span></div>`;
  const nextText = rank.next_title ? `До ранга ${growthEsc(rank.next_title)}: ${Number(rank.missing_points || 0)} очков и ${Number(rank.missing_predictions || 0)} прогнозов` : 'Максимальный ранг';
  el.innerHTML = `
    <div class="metric rank-card"><div class="rank-head"><span>Ранг</span><b class="rank-title">${growthEsc(rank.title || 'Новичок')}</b></div><div class="rank-bar"><div class="rank-fill" style="width:${Math.round(Number(rank.progress || 0) * 100)}%"></div></div><span>${nextText}</span></div>
    <div class="metric gold"><span>Баланс</span><b>${growthMoney(p?.balance || 0)}</b></div>
    <div class="metric good"><span>Очки</span><b>${growthMoney(p?.points || 0)}</b></div>
    <div class="metric"><span>Точность</span><b>${winrate}</b></div>
    <div class="metric"><span>Серия</span><b>${p?.streak || 0}</b></div>
    <div class="metric ${profitCls}"><span>P/L</span><b>${Number(stats.profit || 0) > 0 ? '+' : ''}${growthMoney(stats.profit || 0)}</b></div>
    <div class="metric"><span>ROI</span><b>${growthPct(stats.roi)}</b></div>
    <div class="metric"><span>Ставки</span><b>${Number(stats.settled_bets || 0)}/${Number(stats.total_bets || 0)}</b></div>
    <div class="metric"><span>Выиграно</span><b>${Number(stats.won || 0)}</b></div>
    <div class="metric promo-box"><span>Промокод</span><div class="promo-row"><input id="promoCode" autocomplete="off" placeholder="Введи код" /><button id="promoApply" type="button">Активировать</button></div></div>
    ${refBlock}
    <div class="metric profile-wide"><span>Текущий сезон</span><b>${growthEsc(season.title || 'Сезон')}</b><span>${growthEsc((season.starts_at || '').slice(0,10))} — ${growthEsc((season.ends_at || '').slice(0,10))}</span></div>`;
  const copy = document.getElementById('copyRef');
  if(copy) copy.onclick = async () => { try { await navigator.clipboard.writeText(ref.link); toast('Ссылка скопирована'); } catch(_e) { toast(ref.link); } };
  const promo = document.getElementById('promoApply');
  if(promo) promo.onclick = redeemPromo;
}
function renderMine(items){
  const el = document.getElementById('myPredictions');
  if(!initData){ el.innerHTML = '<div class="empty">Открой Mini App через Telegram</div>'; return; }
  if(!items || !items.length){ el.innerHTML = '<div class="empty">Прогнозов пока нет</div>'; return; }
  el.innerHTML = items.map(x=>{
    const stake = Number(x.stake || 0);
    const odds = x.odds ? ` · ${fmtOdd(x.odds)}` : '';
    const profit = stake ? ` · ${growthProfit(x.profit)}` : '';
    const result = x.result ? ` → ${pickLabel(x.result)}` : '';
    return `<div class="row"><span>${growthEsc(x.title)}<br><span class="muted">${beautySportIcon(x.sport)} ${growthEsc(x.league || '—')} · ${growthEsc(x.status || '')}</span></span><b>${pickLabel(x.pick)}${stake ? ` · ${growthMoney(stake)}${odds}` : ''}${result}${profit}</b></div>`;
  }).join('');
}
"""


def _patch_html(html: str) -> str:
    if ".rank-card" not in html:
        html = html.replace("</style>", GROWTH_CSS + "\n  </style>", 1)
    if "async function redeemPromo" not in html:
        html = html.replace("document.getElementById('refresh').onclick = load;", GROWTH_JS + "\ndocument.getElementById('refresh').onclick = load;", 1)
    return html


def _patch_start_web_server(app: Any, mini_app: Any) -> None:
    if getattr(app, "_GROWTH_WEB_PATCHED", False):
        return

    async def index(_request):
        return mini_app._text_response(app, "ok", "text/plain")

    async def app_page(_request):
        return mini_app._text_response(app, mini_app._index_html(), "text/html")

    async def api_summary(_request):
        return mini_app._json_response(app, mini_app._summary(app))

    async def api_matches(request):
        user_id, _, _ = mini_app._auth_user(app, request)
        sport = request.query.get("sport", "all")
        query = request.query.get("q", "")
        limit = mini_app._int_query(request.query.get("limit"), 80, 1, 200)
        return mini_app._json_response(app, {"items": mini_app._active_matches(app, sport, limit, query, user_id)})

    async def api_match(request):
        user_id, _, _ = mini_app._auth_user(app, request)
        match_id = int(request.match_info.get("match_id", "0") or "0")
        row = app.get_match(match_id)
        if not row:
            return mini_app._json_response(app, {"error": "not_found"}, status=404)
        return mini_app._json_response(app, mini_app._row_to_match(app, row, user_id))

    async def api_predict(request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        user_id, _, error = mini_app._auth_user(app, request, body)
        if not user_id:
            return mini_app._json_response(app, {"error": error}, status=401)
        match_id = mini_app._int_query(str(body.get("match_id", "0")), 0, 0, 10**12)
        pick = str(body.get("pick", "")).upper()
        ok, message, match = mini_app._place_prediction(app, user_id, match_id, pick)
        if not ok:
            return mini_app._json_response(app, {"error": message}, status=400)
        return mini_app._json_response(app, {"ok": True, "message": message, "match": match, "profile": mini_app._profile(app, user_id)})

    async def api_me(request):
        user_id, user, error = mini_app._auth_user(app, request)
        if not user_id:
            return mini_app._json_response(app, {"error": error}, status=401)
        return mini_app._json_response(app, {"user": user, "profile": mini_app._profile(app, user_id)})

    async def api_my_predictions(request):
        user_id, _, error = mini_app._auth_user(app, request)
        if not user_id:
            return mini_app._json_response(app, {"error": error}, status=401)
        limit = mini_app._int_query(request.query.get("limit"), 50, 1, 100)
        return mini_app._json_response(app, {"items": mini_app._my_predictions(app, user_id, limit)})

    async def api_leaderboard(_request):
        return mini_app._json_response(app, mini_app._leaderboard(app))

    async def api_backtest(request):
        limit = mini_app._int_query(request.query.get("limit"), 500, 1, 2000)
        if not hasattr(app, "run_ai_line_backtest"):
            return mini_app._json_response(app, {"error": "backtest_unavailable"}, status=503)
        return mini_app._json_response(app, app.run_ai_line_backtest(limit))

    async def api_promo_redeem(request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        user_id, user, error = mini_app._auth_user(app, request, body)
        if not user_id:
            return mini_app._json_response(app, {"error": error}, status=401)
        ok, message, extra = redeem_promo_code(app, int(user_id), str(body.get("code", "")))
        status = 200 if ok else 400
        return mini_app._json_response(app, {"ok": ok, "message": message, "extra": extra, "user": user, "profile": mini_app._profile(app, int(user_id))}, status=status)

    async def _set_menu_button() -> None:
        base = mini_app._public_base_url() if hasattr(mini_app, "_public_base_url") else ""
        url = f"{base}/app" if base else ""
        if not url:
            return
        try:
            from aiogram.types import MenuButtonWebApp, WebAppInfo
            await app.bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Predictor", web_app=WebAppInfo(url=url)))
        except Exception as exc:
            logger = _log(app)
            if logger:
                logger.warning("growth features: menu button setup failed: %s", exc)

    async def start_web_server():
        if app.PORT <= 0:
            return
        await _set_menu_button()
        web_app = app.web.Application()
        web_app.router.add_get("/", index)
        web_app.router.add_get("/health", index)
        web_app.router.add_get("/app", app_page)
        web_app.router.add_get("/api/summary", api_summary)
        web_app.router.add_get("/api/matches", api_matches)
        web_app.router.add_get(r"/api/matches/{match_id:\d+}", api_match)
        web_app.router.add_post("/api/predict", api_predict)
        web_app.router.add_get("/api/me", api_me)
        web_app.router.add_get("/api/me/predictions", api_my_predictions)
        web_app.router.add_get("/api/leaderboard", api_leaderboard)
        web_app.router.add_get("/api/backtest", api_backtest)
        web_app.router.add_post("/api/promo/redeem", api_promo_redeem)

        runner = app.web.AppRunner(web_app)
        await runner.setup()
        site = app.web.TCPSite(runner, host="0.0.0.0", port=app.PORT)
        await site.start()
        app.logger.info("growth mini app web server started on 0.0.0.0:%s", app.PORT)
        while True:
            await app.asyncio.sleep(3600)

    app.start_web_server = start_web_server
    app._GROWTH_WEB_PATCHED = True


def _rank_text(app: Any, user_id: int) -> str:
    try:
        import mini_app
        profile = mini_app._profile(app, int(user_id))
    except Exception:
        profile = {}
        stats = betting_stats(app, int(user_id))
        profile["rank"] = rank_for_user(profile, stats)
        profile["betting_stats"] = stats
    rank = profile.get("rank") or {}
    stats = profile.get("betting_stats") or {}
    return (
        f"<b>Ранг: {rank.get('title', 'Новичок')}</b>\n"
        f"Очки: <b>{rank.get('points', 0)}</b> · прогнозы: <b>{rank.get('predictions', 0)}</b>\n"
        f"P/L: <b>{int(stats.get('profit', 0) or 0):+d}</b> · ROI: <b>{round(float(stats.get('roi', 0) or 0) * 100, 1)}%</b>\n"
        f"Следующий: <b>{rank.get('next_title') or 'максимальный ранг'}</b>"
    )


def _patch_commands(app: Any) -> None:
    if getattr(app, "_GROWTH_COMMANDS_PATCHED", False):
        return

    @app.dp.message(app.Command("promo"))
    async def promo_cmd(m):
        app.upsert_user_from_message(m)
        args = (m.text or "").split()[1:]
        if not m.from_user:
            return
        if not args:
            if app.is_admin(m.from_user.id):
                return await m.answer("Использование: /promo CODE AMOUNT [MAX_USES]\nДля игрока: /promo CODE")
            return await m.answer("Введи промокод так: /promo CODE")

        if app.is_admin(m.from_user.id) and len(args) >= 2:
            try:
                amount = int(args[1])
                max_uses = int(args[2]) if len(args) >= 3 else 0
            except ValueError:
                return await m.answer("Сумма и лимит должны быть числами.")
            ok, message = create_promo_code(app, args[0], amount, max_uses, m.from_user.id)
            return await m.answer(("✅ " if ok else "❌ ") + message)

        ok, message, _ = redeem_promo_code(app, m.from_user.id, args[0])
        await m.answer(("✅ " if ok else "❌ ") + message)

    @app.dp.message(app.Command("promos"))
    async def promos_cmd(m):
        app.upsert_user_from_message(m)
        if not m.from_user or not app.is_admin(m.from_user.id):
            return
        rows = list_promo_codes(app, 20)
        if not rows:
            return await m.answer("Промокодов пока нет. Создай: /promo START500 500 100")
        lines = ["<b>Промокоды</b>"]
        for row in rows:
            max_uses = int(row.get("max_uses") or 0)
            limit = "∞" if max_uses <= 0 else str(max_uses)
            state = "on" if int(row.get("is_active") or 0) else "off"
            lines.append(f"<code>{row['code']}</code> +{row['amount']} · {row['used_count']}/{limit} · {state}")
        await m.answer("\n".join(lines))

    @app.dp.message(app.Command("rank"))
    async def rank_cmd(m):
        app.upsert_user_from_message(m)
        if not m.from_user:
            return
        await m.answer(_rank_text(app, m.from_user.id))

    app._GROWTH_COMMANDS_PATCHED = True


def apply(app: Any) -> None:
    if getattr(app, "_GROWTH_FEATURES_APPLIED", False):
        return
    _ensure_schema(app)
    _patch_mini_app(app)
    _patch_commands(app)
    app.betting_stats = lambda user_id: betting_stats(app, int(user_id))
    app.rank_for_user = lambda profile, stats=None: rank_for_user(dict(profile or {}), dict(stats or {}))
    app.create_promo_code = lambda code, amount, max_uses=0: create_promo_code(app, code, int(amount), int(max_uses))
    app.redeem_promo_code = lambda user_id, code: redeem_promo_code(app, int(user_id), code)
    app._GROWTH_FEATURES_APPLIED = True
    print(f"{VERSION}_APPLIED")
