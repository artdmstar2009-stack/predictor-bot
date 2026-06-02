from __future__ import annotations

import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from urllib.parse import parse_qsl

VERSION = "PRO_FEATURES_V1"
PICKS = ("1", "X", "2")


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


def _env_int(name: str, default: int, minimum: int = 0, maximum: int = 10**9) -> int:
    try:
        value = int(os.getenv(name, str(default)) or default)
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _ensure_schema(app: Any) -> None:
    try:
        app.init_db()
    except Exception:
        logger = _log(app)
        if logger:
            logger.exception("pro features: base init_db failed")

    with app.db() as con:
        cur = con.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS provider_health (
                provider TEXT PRIMARY KEY,
                enabled INTEGER DEFAULT 1,
                status TEXT DEFAULT 'unknown',
                last_ok_at TEXT,
                last_error_at TEXT,
                last_error TEXT,
                last_count INTEGER DEFAULT 0,
                last_latency_ms INTEGER DEFAULT 0,
                updated_at TEXT
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS referrals (
                referrer_id INTEGER,
                referred_id INTEGER UNIQUE,
                referrer_bonus INTEGER DEFAULT 0,
                referred_bonus INTEGER DEFAULT 0,
                created_at TEXT
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS ix_referrals_referrer ON referrals(referrer_id)")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS seasons (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT UNIQUE,
                title TEXT,
                starts_at TEXT,
                ends_at TEXT,
                status TEXT DEFAULT 'active',
                created_at TEXT
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS season_points (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                season_code TEXT,
                user_id INTEGER,
                match_id INTEGER,
                points INTEGER DEFAULT 0,
                created_at TEXT,
                UNIQUE(season_code, user_id, match_id)
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS ix_season_points_top ON season_points(season_code, points)")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_season_points_date ON season_points(created_at)")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS admin_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_id INTEGER,
                action TEXT,
                details TEXT,
                created_at TEXT
            )
            """
        )
        con.commit()


def _month_bounds(dt: datetime) -> tuple[datetime, datetime]:
    start = dt.astimezone(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start, end


def _current_season_code(app: Any) -> str:
    start, _ = _month_bounds(app.now_utc())
    return f"{start.year:04d}-{start.month:02d}"


def ensure_current_season(app: Any) -> dict[str, Any]:
    start, end = _month_bounds(app.now_utc())
    code = f"{start.year:04d}-{start.month:02d}"
    title = f"Сезон {start.month:02d}.{start.year}"
    with app.db() as con:
        cur = con.cursor()
        cur.execute(
            """
            INSERT OR IGNORE INTO seasons(code, title, starts_at, ends_at, status, created_at)
            VALUES(?,?,?,?, 'active', ?)
            """,
            (code, title, app.iso(start), app.iso(end), _now_iso(app)),
        )
        row = cur.execute("SELECT * FROM seasons WHERE code=?", (code,)).fetchone()
        con.commit()
    if row:
        return dict(row)
    return {"code": code, "title": title, "starts_at": app.iso(start), "ends_at": app.iso(end), "status": "active"}


def _record_admin_action(app: Any, admin_id: int, action: str, details: str = "") -> None:
    try:
        with app.db() as con:
            con.execute(
                "INSERT INTO admin_actions(admin_id, action, details, created_at) VALUES(?,?,?,?)",
                (admin_id, action, details[:500], _now_iso(app)),
            )
            con.commit()
    except Exception:
        logger = _log(app)
        if logger:
            logger.exception("pro features: admin action log failed")


def _record_season_points(app: Any, match_id: int) -> int:
    season = ensure_current_season(app)
    code = str(season["code"])
    with app.db() as con:
        cur = con.cursor()
        match = cur.execute("SELECT id, status, result FROM matches WHERE id=?", (int(match_id),)).fetchone()
        if not match or match["status"] != "closed" or not match["result"]:
            return 0
        votes = cur.execute("SELECT user_id, pick FROM votes WHERE match_id=?", (int(match_id),)).fetchall()
        inserted = 0
        now = _now_iso(app)
        for vote in votes:
            uid = int(vote["user_id"])
            pick = str(vote["pick"] or "")
            points = int(getattr(app, "POINTS_FOR_CORRECT", 3) if pick == match["result"] else getattr(app, "POINTS_FOR_WRONG", 0))
            try:
                cur.execute(
                    "INSERT INTO season_points(season_code, user_id, match_id, points, created_at) VALUES(?,?,?,?,?)",
                    (code, uid, int(match_id), points, now),
                )
                inserted += 1
            except sqlite3.IntegrityError:
                pass
        con.commit()
        return inserted


def _backfill_current_season(app: Any, max_matches: int = 500) -> int:
    season = ensure_current_season(app)
    starts = str(season["starts_at"])
    ends = str(season["ends_at"])
    total = 0
    with app.db() as con:
        rows = con.execute(
            """
            SELECT id FROM matches
            WHERE status='closed'
              AND COALESCE(start_time_utc, start_time, created_at) >= ?
              AND COALESCE(start_time_utc, start_time, created_at) < ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (starts, ends, int(max_matches)),
        ).fetchall()
    for row in rows:
        total += _record_season_points(app, int(row["id"]))
    return total


def _season_top_from_points(app: Any, original: Callable | None, limit: int = 10) -> list[tuple[int, int]]:
    season = ensure_current_season(app)
    code = str(season["code"])
    try:
        _backfill_current_season(app)
    except Exception:
        logger = _log(app)
        if logger:
            logger.exception("pro features: season backfill failed")
    with app.db() as con:
        rows = con.execute(
            """
            SELECT user_id, COALESCE(SUM(points),0) AS pts
            FROM season_points
            WHERE season_code=?
            GROUP BY user_id
            HAVING pts > 0
            ORDER BY pts DESC
            LIMIT ?
            """,
            (code, int(limit)),
        ).fetchall()
    if rows:
        return [(int(r["user_id"]), int(r["pts"])) for r in rows]
    if callable(original):
        try:
            return list(original(limit))
        except Exception:
            return []
    return []


def _top_points_since_from_season(app: Any, original: Callable | None, since: datetime, limit: int = 10) -> list[tuple[int, int]]:
    with app.db() as con:
        rows = con.execute(
            """
            SELECT user_id, COALESCE(SUM(points),0) AS pts
            FROM season_points
            WHERE created_at >= ?
            GROUP BY user_id
            HAVING pts > 0
            ORDER BY pts DESC
            LIMIT ?
            """,
            (app.iso(since), int(limit)),
        ).fetchall()
    if rows:
        return [(int(r["user_id"]), int(r["pts"])) for r in rows]
    if callable(original):
        try:
            return list(original(since, limit))
        except Exception:
            return []
    return []


def _patch_scoring(app: Any) -> None:
    original = getattr(app, "apply_scoring_for_match", None)
    if not callable(original) or getattr(original, "_pro_features_wrapped", False):
        return

    async def patched_apply_scoring_for_match(match_id: int, result_1x2: str, home_score: int | None = None, away_score: int | None = None):
        result = await original(match_id, result_1x2, home_score, away_score)
        try:
            _record_season_points(app, int(match_id))
        except Exception:
            logger = _log(app)
            if logger:
                logger.exception("pro features: season scoring failed match_id=%s", match_id)
        return result

    patched_apply_scoring_for_match._pro_features_wrapped = True
    app.apply_scoring_for_match = patched_apply_scoring_for_match


def _patch_leaderboards(app: Any) -> None:
    original_season_top = getattr(app, "season_top", None)
    original_top_since = getattr(app, "top_points_since", None)

    if not getattr(app, "_PRO_SEASON_TOP_PATCHED", False):
        def season_top(limit: int = 10):
            return _season_top_from_points(app, original_season_top, limit)

        app.season_top = season_top
        app._PRO_SEASON_TOP_PATCHED = True

    if not getattr(app, "_PRO_TOP_SINCE_PATCHED", False):
        def top_points_since(since: datetime, limit: int = 10):
            return _top_points_since_from_season(app, original_top_since, since, limit)

        app.top_points_since = top_points_since
        app._PRO_TOP_SINCE_PATCHED = True


def _provider_enabled(app: Any, provider: str) -> bool:
    if provider == "football":
        return bool(getattr(app, "FOOTBALL_ENABLED", True))
    if provider == "hockey":
        return bool(getattr(app, "NHL_ENABLED", True))
    if provider == "tennis":
        return os.getenv("TENNIS_ENABLED", "1") == "1"
    if provider == "odds":
        return bool((os.getenv("ODDS_API_KEY") or getattr(app, "ODDS_API_KEY", "") or "").strip())
    return True


def record_provider_health(app: Any, provider: str, enabled: bool, status: str, count: int = 0, error: str = "", latency_ms: int = 0) -> None:
    now = _now_iso(app)
    status = "disabled" if not enabled else ("empty" if status == "ok" and count <= 0 else status)
    with app.db() as con:
        cur = con.cursor()
        old = cur.execute("SELECT * FROM provider_health WHERE provider=?", (provider,)).fetchone()
        old_ok = _row_value(old, "last_ok_at")
        old_error_at = _row_value(old, "last_error_at")
        old_error = _row_value(old, "last_error") or ""
        last_ok = now if status in ("ok", "empty") else old_ok
        last_error_at = now if status == "error" else old_error_at
        last_error = str(error or "")[:500] if status == "error" else ("" if status in ("ok", "empty") else old_error)
        cur.execute(
            """
            INSERT INTO provider_health(provider, enabled, status, last_ok_at, last_error_at, last_error, last_count, last_latency_ms, updated_at)
            VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(provider) DO UPDATE SET
              enabled=excluded.enabled,
              status=excluded.status,
              last_ok_at=excluded.last_ok_at,
              last_error_at=excluded.last_error_at,
              last_error=excluded.last_error,
              last_count=excluded.last_count,
              last_latency_ms=excluded.last_latency_ms,
              updated_at=excluded.updated_at
            """,
            (provider, 1 if enabled else 0, status, last_ok, last_error_at, last_error, int(count), int(latency_ms), now),
        )
        con.commit()


def _count_result(result: Any) -> int:
    if result is None:
        return 0
    if isinstance(result, (list, tuple, set, dict)):
        return len(result)
    if isinstance(result, int):
        return result
    try:
        return len(result)
    except Exception:
        return 0


def _wrap_provider(app: Any, attr: str, provider: str) -> None:
    original = getattr(app, attr, None)
    if not callable(original) or getattr(original, "_pro_health_wrapped", False):
        return

    async def wrapped(*args, **kwargs):
        enabled = _provider_enabled(app, provider)
        started = time.perf_counter()
        try:
            result = await original(*args, **kwargs)
            latency = int((time.perf_counter() - started) * 1000)
            record_provider_health(app, provider, enabled, "ok", _count_result(result), "", latency)
            return result
        except Exception as exc:
            latency = int((time.perf_counter() - started) * 1000)
            try:
                record_provider_health(app, provider, enabled, "error", 0, str(exc), latency)
            except Exception:
                pass
            raise

    wrapped._pro_health_wrapped = True
    setattr(app, attr, wrapped)


def _patch_provider_health(app: Any) -> None:
    _wrap_provider(app, "football_list", "football")
    _wrap_provider(app, "nhl_list", "hockey")
    _wrap_provider(app, "tennis_list", "tennis")
    _wrap_provider(app, "refresh_odds_once", "odds")


def provider_health_rows(app: Any) -> list[dict[str, Any]]:
    providers = ["football", "hockey", "tennis", "odds"]
    with app.db() as con:
        rows = con.execute("SELECT * FROM provider_health").fetchall()
    by_name = {str(row["provider"]): dict(row) for row in rows}
    out: list[dict[str, Any]] = []
    for provider in providers:
        if provider in by_name:
            out.append(by_name[provider])
        else:
            out.append(
                {
                    "provider": provider,
                    "enabled": 1 if _provider_enabled(app, provider) else 0,
                    "status": "waiting" if _provider_enabled(app, provider) else "disabled",
                    "last_ok_at": None,
                    "last_error_at": None,
                    "last_error": "",
                    "last_count": 0,
                    "last_latency_ms": 0,
                    "updated_at": None,
                }
            )
    return out


def _provider_title(provider: str) -> str:
    return {
        "football": "Футбол",
        "hockey": "Хоккей/NHL",
        "tennis": "Теннис",
        "odds": "Коэффициенты",
    }.get(provider, provider)


def _status_title(status: str) -> str:
    return {
        "ok": "OK",
        "empty": "0 матчей",
        "error": "Ошибка",
        "disabled": "Выключено",
        "waiting": "Ждёт синка",
        "unknown": "Неизвестно",
    }.get(status or "unknown", status or "unknown")


def provider_health_text(app: Any) -> str:
    lines = ["<b>Provider Health</b>"]
    for row in provider_health_rows(app):
        provider = str(row.get("provider") or "")
        status = str(row.get("status") or "unknown")
        count = int(row.get("last_count") or 0)
        latency = int(row.get("last_latency_ms") or 0)
        updated = row.get("updated_at") or "ещё не запускался"
        lines.append(f"• <b>{_provider_title(provider)}</b>: {_status_title(status)} · {count} · {latency} ms")
        if updated and updated != "ещё не запускался":
            lines.append(f"  обновлено: <code>{str(updated)[:19]}</code>")
        elif updated:
            lines.append(f"  {updated}")
        if status == "error" and row.get("last_error"):
            lines.append(f"  <code>{str(row['last_error'])[:180]}</code>")
    if os.getenv("THESPORTSDB_API_KEY", "123") == "123":
        lines.append("\nФутбол на публичном ключе TheSportsDB может давать 429. Для стабильности добавь <code>THESPORTSDB_API_KEY</code> в Render.")
    if not _provider_enabled(app, "odds"):
        lines.append("Для реальной букмекерской линии добавь <code>ODDS_API_KEY</code>; без него работает AI/ELO-линия.")
    return "\n".join(lines)


def _bot_username() -> str:
    return (
        os.getenv("BOT_USERNAME")
        or os.getenv("PUBLIC_BOT_USERNAME")
        or os.getenv("TELEGRAM_BOT_USERNAME")
        or ""
    ).strip().lstrip("@")


def referral_link(user_id: int, username: str | None = None) -> str:
    username = (username or _bot_username()).strip().lstrip("@")
    if not username:
        return ""
    return f"https://t.me/{username}?startapp=ref_{int(user_id)}"


def record_referral(app: Any, referrer_id: int, referred_id: int) -> bool:
    referrer_id = int(referrer_id or 0)
    referred_id = int(referred_id or 0)
    if referrer_id <= 0 or referred_id <= 0 or referrer_id == referred_id:
        return False
    ref_bonus = _env_int("REFERRAL_BONUS", 250, 0, 10**9)
    new_bonus = _env_int("REFERRAL_REFEREE_BONUS", 100, 0, 10**9)
    try:
        with app.db() as con:
            cur = con.cursor()
            cur.execute(
                "INSERT INTO referrals(referrer_id, referred_id, referrer_bonus, referred_bonus, created_at) VALUES(?,?,?,?,?)",
                (referrer_id, referred_id, ref_bonus, new_bonus, _now_iso(app)),
            )
            con.commit()
    except sqlite3.IntegrityError:
        return False
    except Exception:
        logger = _log(app)
        if logger:
            logger.exception("pro features: referral save failed")
        return False

    try:
        if ref_bonus:
            app.add_balance(referrer_id, ref_bonus)
        if new_bonus:
            app.add_balance(referred_id, new_bonus)
    except Exception:
        logger = _log(app)
        if logger:
            logger.exception("pro features: referral bonus failed")
    return True


def referral_stats(app: Any, user_id: int) -> dict[str, Any]:
    with app.db() as con:
        row = con.execute(
            """
            SELECT COUNT(*) AS c, COALESCE(SUM(referrer_bonus),0) AS bonus
            FROM referrals
            WHERE referrer_id=?
            """,
            (int(user_id),),
        ).fetchone()
    invited = int(_row_value(row, "c", 0) or 0)
    bonus = int(_row_value(row, "bonus", 0) or 0)
    return {
        "code": f"ref_{int(user_id)}",
        "link": referral_link(int(user_id)),
        "invited": invited,
        "bonus": bonus,
        "referrer_bonus": _env_int("REFERRAL_BONUS", 250, 0, 10**9),
        "referred_bonus": _env_int("REFERRAL_REFEREE_BONUS", 100, 0, 10**9),
    }


def _extract_start_param(mini_app: Any, request: Any, body: dict[str, Any] | None = None) -> str:
    value = ""
    try:
        value = request.query.get("start_param") or request.query.get("startapp") or ""
    except Exception:
        value = ""
    if value:
        return str(value)
    raw = ""
    try:
        raw = mini_app._request_init_data(request, body)
    except Exception:
        raw = ""
    if raw:
        pairs = dict(parse_qsl(raw, keep_blank_values=True))
        value = pairs.get("start_param") or pairs.get("tgWebAppStartParam") or ""
        if value:
            return str(value)
    if body:
        value = body.get("start_param") or body.get("startParam") or ""
    return str(value or "")


def _probabilities(app: Any, row: Any, priced: dict[str, Any]) -> dict[str, float | None]:
    probs = {
        "1": priced.get("prob_1"),
        "X": priced.get("prob_x"),
        "2": priced.get("prob_2"),
    }
    if all(value is None for value in probs.values()):
        try:
            p1, px, p2 = app.ai_probs_1x2(dict(row))
            probs = {"1": p1, "X": px, "2": p2}
        except Exception:
            pass
    return probs


def _odds(row: Any, priced: dict[str, Any]) -> dict[str, float | None]:
    return {
        "1": priced.get("odds_1") or _row_value(row, "odds_1"),
        "X": priced.get("odds_x") or _row_value(row, "odds_x"),
        "2": priced.get("odds_2") or _row_value(row, "odds_2"),
    }


def _available_picks(row: Any, priced: dict[str, Any]) -> list[str]:
    sport = str(_row_value(row, "sport", "") or "").lower()
    league = str(_row_value(row, "league", "") or "").lower()
    if "tennis" in sport or "tennis" in league or "nhl" in sport or "hockey" in sport:
        return ["1", "2"]
    if priced.get("prob_x") is None and priced.get("odds_x") is None and _row_value(row, "odds_x") is None:
        return ["1", "2"]
    return ["1", "X", "2"]


def match_insights(app: Any, row: Any, priced: dict[str, Any] | None = None) -> dict[str, Any]:
    priced = priced or {}
    if not priced:
        try:
            priced = app.ai_odds_for_match(dict(row))
        except Exception:
            priced = {}
    picks = _available_picks(row, priced)
    probs = _probabilities(app, row, priced)
    odds = _odds(row, priced)

    best_pick = None
    best_prob = -1.0
    for pick in picks:
        try:
            value = float(probs.get(pick) or 0)
        except (TypeError, ValueError):
            value = 0.0
        if value > best_prob:
            best_pick = pick
            best_prob = value

    fair_odds = round(1.0 / best_prob, 2) if best_prob > 0 else None
    market_odds = odds.get(best_pick or "")
    edge = None
    try:
        if market_odds and best_prob > 0:
            edge = round((float(market_odds) * best_prob - 1.0) * 100, 1)
    except Exception:
        edge = None

    title = str(_row_value(row, "title", "Матч") or "Матч")
    league = str(_row_value(row, "league", "") or "")
    sport = str(_row_value(row, "sport", "") or "").lower()
    votes_total = 0
    try:
        stats = app.match_stats(int(_row_value(row, "id", 0) or 0))
        votes_total = int(stats.get("1", 0) + stats.get("X", 0) + stats.get("2", 0))
    except Exception:
        votes_total = 0

    reasons: list[str] = []
    if hasattr(app, "_parse_title_teams"):
        try:
            teams = app._parse_title_teams(title)
        except Exception:
            teams = None
        if teams:
            reasons.append(f"AI сравнил форму и рейтинг: {teams[0]} vs {teams[1]}")
    if "tennis" in sport or "tennis" in league.lower():
        reasons.append("Для тенниса линия двухисходная: победа игрока 1 или игрока 2")
    elif "hockey" in sport or "nhl" in sport:
        reasons.append("Для хоккея ничья скрыта, ставка идёт на победителя")
    else:
        reasons.append("Модель учитывает силу команд, домашний фактор и вероятность ничьей")
    source = priced.get("odds_source") or _row_value(row, "odds_source") or "AI"
    reasons.append(f"Источник линии: {source}")
    if votes_total:
        reasons.append(f"Активность игроков: {votes_total} прогнозов")
    if not reasons:
        reasons.append("AI-линия обновляется автоматически после синка матчей")

    return {
        "best_pick": best_pick,
        "confidence": round(best_prob, 4) if best_prob >= 0 else None,
        "fair_odds": fair_odds,
        "market_odds": market_odds,
        "edge_pct": edge,
        "league": league,
        "reasons": reasons[:4],
        "note": "AI-линия помогает оценивать вероятность, но не гарантирует исход.",
    }


PRO_CSS = r"""
    .reason-list { display:flex; flex-wrap:wrap; gap:6px; margin-top:-2px; }
    .reason-chip { display:inline-flex; align-items:center; min-height:24px; padding:4px 7px; border:1px solid rgba(255,255,255,.09); border-radius:8px; background:rgba(255,255,255,.04); color:var(--muted); font-size:11px; line-height:1.25; }
    .profile-wide { grid-column:1/-1; min-height:auto; }
    .profile-wide b { font-size:14px; line-height:1.25; word-break:break-word; }
    .profile-wide button { margin-top:10px; min-height:38px; border-color:rgba(110,231,183,.35); background:rgba(110,231,183,.1); }
"""

PRO_JS = r"""
const proEsc = value => String(value ?? '').replace(/[&<>'"]/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[ch]));
const proMoney = value => Number(value || 0).toLocaleString('ru-RU');
async function loadSummary(){
  const summary = await api('/api/summary');
  renderSports(summary.sports || []);
  renderBacktest(summary.backtest);
  const status = document.getElementById('status');
  const health = summary.provider_health || [];
  const errors = health.filter(x => x.status === 'error').length;
  if(status) status.textContent = `${summary.ai_line ? 'AI линия активна' : 'AI линия выключена'} · ${authLabel()} · ${errors ? 'провайдеры: ' + errors + ' ошибка' : 'провайдеры OK'} · ${new Date(summary.now).toLocaleTimeString()}`;
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
  const refBlock = ref.link
    ? `<div class="metric profile-wide"><span>Реферальная ссылка</span><b>${proEsc(ref.link)}</b><button id="copyRef">Скопировать ссылку</button><span>Приглашено: ${Number(ref.invited || 0)} · бонусы: ${proMoney(ref.bonus || 0)}</span></div>`
    : `<div class="metric profile-wide"><span>Реферальная ссылка</span><b>${proEsc(ref.code || 'ref')}</b><span>Добавь BOT_USERNAME в Render, чтобы ссылка стала полной.</span></div>`;
  const seasonBlock = `<div class="metric profile-wide"><span>Текущий сезон</span><b>${proEsc(season.title || 'Сезон')}</b><span>${proEsc((season.starts_at || '').slice(0,10))} — ${proEsc((season.ends_at || '').slice(0,10))}</span></div>`;
  el.innerHTML = `
    <div class="metric gold"><span>Баланс</span><b>${proMoney(p?.balance || 0)}</b></div>
    <div class="metric good"><span>Очки</span><b>${proMoney(p?.points || 0)}</b></div>
    <div class="metric"><span>Точность</span><b>${winrate}</b></div>
    <div class="metric"><span>Серия</span><b>${p?.streak || 0}</b></div>
    ${refBlock}${seasonBlock}`;
  const copy = document.getElementById('copyRef');
  if(copy) copy.onclick = async () => { try { await navigator.clipboard.writeText(ref.link); toast('Ссылка скопирована'); } catch(_e) { toast(ref.link); } };
}
"""


def _patch_html(html: str) -> str:
    html = html.replace("</style>", PRO_CSS + "\n  </style>", 1)
    target = r'''      <div class=\"ai-strip\"><span>AI-\u043b\u0438\u043d\u0438\u044f</span><b>${beautyPickSummary(m.probabilities, picks)}</b></div>'''
    reason = r'''
      <div class=\"reason-list\">${(m.insights?.reasons || []).slice(0,3).map(r => `<span class=\"reason-chip\">${beautyEsc(r)}</span>`).join('')}</div>'''
    if target in html and reason not in html:
        html = html.replace(target, target + reason, 1)
    html = html.replace("document.getElementById('refresh').onclick = load;", PRO_JS + "\ndocument.getElementById('refresh').onclick = load;", 1)
    return html


def _patch_mini_app(app: Any) -> None:
    if getattr(app, "_PRO_FEATURES_MINI_PATCHED", False):
        return
    try:
        import mini_app
    except Exception as exc:
        logger = _log(app)
        if logger:
            logger.exception("pro features: mini_app import failed: %s", exc)
        return

    original_summary = getattr(mini_app, "_summary", None)
    if callable(original_summary) and not getattr(original_summary, "_pro_features_wrapped", False):
        def patched_summary(bot: Any) -> dict[str, Any]:
            data = dict(original_summary(bot) or {})
            data["provider_health"] = provider_health_rows(bot)
            data["active_season"] = ensure_current_season(bot)
            return data

        patched_summary._pro_features_wrapped = True
        mini_app._summary = patched_summary

    original_profile = getattr(mini_app, "_profile", None)
    if callable(original_profile) and not getattr(original_profile, "_pro_features_wrapped", False):
        def patched_profile(bot: Any, user_id: int) -> dict[str, Any]:
            data = dict(original_profile(bot, user_id) or {})
            data["referral"] = referral_stats(bot, int(user_id))
            data["season"] = ensure_current_season(bot)
            return data

        patched_profile._pro_features_wrapped = True
        mini_app._profile = patched_profile

    original_row_to_match = getattr(mini_app, "_row_to_match", None)
    if callable(original_row_to_match) and not getattr(original_row_to_match, "_pro_features_wrapped", False):
        def patched_row_to_match(bot: Any, row: Any, user_id: int | None = None) -> dict[str, Any]:
            payload = dict(original_row_to_match(bot, row, user_id) or {})
            try:
                priced = bot.ai_odds_for_match(dict(row))
            except Exception:
                priced = {}
            payload["insights"] = match_insights(bot, row, priced)
            return payload

        patched_row_to_match._pro_features_wrapped = True
        mini_app._row_to_match = patched_row_to_match

    original_auth = getattr(mini_app, "_auth_user", None)
    if callable(original_auth) and not getattr(original_auth, "_pro_features_wrapped", False):
        def patched_auth_user(bot: Any, request: Any, body: dict[str, Any] | None = None):
            user_id, user, error = original_auth(bot, request, body)
            if user_id:
                try:
                    start = _extract_start_param(mini_app, request, body)
                    if start.startswith("ref_"):
                        record_referral(bot, int(start.replace("ref_", "", 1)), int(user_id))
                except Exception:
                    logger = _log(bot)
                    if logger:
                        logger.exception("pro features: referral capture failed")
            return user_id, user, error

        patched_auth_user._pro_features_wrapped = True
        mini_app._auth_user = patched_auth_user

    original_index = getattr(mini_app, "_index_html", None)
    if callable(original_index) and not getattr(original_index, "_pro_features_wrapped", False):
        def patched_index_html() -> str:
            return _patch_html(original_index())

        patched_index_html._pro_features_wrapped = True
        mini_app._index_html = patched_index_html

    app._PRO_FEATURES_MINI_PATCHED = True


def _admin_stats_text(app: Any) -> str:
    with app.db() as con:
        users = con.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
        open_matches = con.execute("SELECT COUNT(*) AS c FROM matches WHERE status='open'").fetchone()["c"]
        closed_matches = con.execute("SELECT COUNT(*) AS c FROM matches WHERE status='closed'").fetchone()["c"]
        votes = con.execute("SELECT COUNT(*) AS c FROM votes").fetchone()["c"]
        stakes = con.execute("SELECT COALESCE(SUM(COALESCE(stake,0)),0) AS s FROM votes").fetchone()["s"]
        sports = con.execute(
            """
            SELECT COALESCE(NULLIF(LOWER(sport), ''), 'other') AS sport, COUNT(*) AS c
            FROM matches
            WHERE status='open'
            GROUP BY COALESCE(NULLIF(LOWER(sport), ''), 'other')
            ORDER BY c DESC
            """
        ).fetchall()
    season = ensure_current_season(app)
    sport_line = ", ".join(f"{r['sport']}={r['c']}" for r in sports) or "нет"
    return (
        "<b>Админ-панель</b>\n"
        f"Сезон: <b>{season['title']}</b>\n"
        f"Пользователи: <b>{users}</b>\n"
        f"Матчи: open <b>{open_matches}</b> / closed <b>{closed_matches}</b>\n"
        f"Прогнозы: <b>{votes}</b> · сумма ставок <b>{int(stakes or 0)}</b>\n"
        f"Спорт: <code>{sport_line}</code>"
    )


def _admin_keyboard(app: Any):
    return app.InlineKeyboardMarkup(
        inline_keyboard=[
            [app.InlineKeyboardButton(text="Provider Health", callback_data="proadmin:health")],
            [app.InlineKeyboardButton(text="Синхронизировать", callback_data="proadmin:sync")],
            [app.InlineKeyboardButton(text="Статистика", callback_data="proadmin:stats")],
            [app.InlineKeyboardButton(text="Сезон", callback_data="proadmin:season")],
        ]
    )


def _patch_admin(app: Any) -> None:
    if getattr(app, "_PRO_ADMIN_PATCHED", False):
        return

    @app.dp.message(app.Command("admin"))
    async def pro_admin_cmd(m):
        app.upsert_user_from_message(m)
        if not m.from_user or not app.is_admin(m.from_user.id):
            return
        await m.answer(_admin_stats_text(app), reply_markup=_admin_keyboard(app))

    @app.dp.message(app.Command("provider_health"))
    async def pro_provider_health_cmd(m):
        app.upsert_user_from_message(m)
        if not m.from_user or not app.is_admin(m.from_user.id):
            return
        await m.answer(provider_health_text(app), reply_markup=_admin_keyboard(app))

    @app.dp.message(app.Command("ref"))
    async def pro_ref_cmd(m):
        app.upsert_user_from_message(m)
        if not m.from_user:
            return
        username = _bot_username()
        if not username:
            try:
                me = await app.bot.get_me()
                username = getattr(me, "username", "") or ""
            except Exception:
                username = ""
        stats = referral_stats(app, m.from_user.id)
        link = referral_link(m.from_user.id, username) or stats["code"]
        await m.answer(
            "<b>Реферальная ссылка</b>\n"
            f"<code>{link}</code>\n\n"
            f"Приглашено: <b>{stats['invited']}</b>\n"
            f"Бонус тебе: <b>{stats['referrer_bonus']}</b> · другу: <b>{stats['referred_bonus']}</b>"
        )

    @app.dp.callback_query(app.F.data.startswith("proadmin:"))
    async def pro_admin_cb(cb):
        if not cb.from_user or not app.is_admin(cb.from_user.id):
            return await cb.answer("Недостаточно прав", show_alert=True)
        action = cb.data.split(":", 1)[1]
        if action == "health":
            await cb.message.answer(provider_health_text(app), reply_markup=_admin_keyboard(app))
            await cb.answer()
            return
        if action == "stats":
            await cb.message.answer(_admin_stats_text(app), reply_markup=_admin_keyboard(app))
            await cb.answer()
            return
        if action == "season":
            season = ensure_current_season(app)
            top = app.season_top(10) if hasattr(app, "season_top") else []
            lines = [f"<b>{season['title']}</b>", f"Период: <code>{season['starts_at'][:10]} - {season['ends_at'][:10]}</code>"]
            if top:
                lines.append("\nТоп сезона:")
                for i, (uid, pts) in enumerate(top, 1):
                    lines.append(f"{i}. {app.pretty_user(uid)} — <b>{pts}</b>")
            else:
                lines.append("\nТоп сезона пока пуст.")
            await cb.message.answer("\n".join(lines), reply_markup=_admin_keyboard(app))
            await cb.answer()
            return
        if action == "sync":
            await cb.answer("Синхронизирую...")
            sync_fn = getattr(app, "fixed_sync_once", None) or getattr(app, "autosync_once", None)
            if not callable(sync_fn):
                return await cb.message.answer("Синк недоступен.", reply_markup=_admin_keyboard(app))
            try:
                msg = await sync_fn()
                _record_admin_action(app, cb.from_user.id, "sync", msg)
                await cb.message.answer(f"✅ {msg}\n\n{provider_health_text(app)}", reply_markup=_admin_keyboard(app))
            except Exception as exc:
                logger = _log(app)
                if logger:
                    logger.exception("pro features: admin sync failed")
                await cb.message.answer(f"❌ Ошибка синка: <code>{str(exc)[:300]}</code>", reply_markup=_admin_keyboard(app))
            return
        await cb.answer()

    app._PRO_ADMIN_PATCHED = True


def apply(app: Any) -> None:
    if getattr(app, "_PRO_FEATURES_APPLIED", False):
        return
    _ensure_schema(app)
    ensure_current_season(app)
    _patch_provider_health(app)
    _patch_scoring(app)
    _patch_leaderboards(app)
    _patch_mini_app(app)
    _patch_admin(app)
    try:
        _backfill_current_season(app)
    except Exception:
        logger = _log(app)
        if logger:
            logger.exception("pro features: initial season backfill failed")
    app.provider_health_rows = lambda: provider_health_rows(app)
    app.provider_health_text = lambda: provider_health_text(app)
    app.match_insights = lambda row, priced=None: match_insights(app, row, priced)
    app.referral_stats = lambda user_id: referral_stats(app, int(user_id))
    app._PRO_FEATURES_APPLIED = True
    print(f"{VERSION}_APPLIED")
