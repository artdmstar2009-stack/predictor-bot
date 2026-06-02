from __future__ import annotations

import asyncio
import os
import time
from datetime import timedelta
from html import escape
from typing import Any, Callable

VERSION = "MARKET_ODDS_GUARD_V2"
INVALID_KEY_PARTS = (
    "winner",
    "championship",
    "super_bowl",
    "outright",
    "futures",
)
PREFERRED_KEYS = (
    "soccer_epl",
    "soccer_uefa_champs_league",
    "soccer_spain_la_liga",
    "soccer_italy_serie_a",
    "soccer_germany_bundesliga",
    "soccer_france_ligue_one",
    "soccer_fifa_world_cup",
    "soccer_fifa_club_world_cup",
    "icehockey_nhl",
)


def _log(app: Any):
    return getattr(app, "logger", None)


def _clean_key(value: Any) -> str:
    return str(value or "").strip().lower()


def _safe_int(value: Any, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(low, min(high, number))


def _safe_float(value: Any, default: float, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return max(low, min(high, number))


def _key_allowed(key: str) -> bool:
    key = _clean_key(key)
    if not key:
        return False
    if any(part in key for part in INVALID_KEY_PARTS):
        return False
    if key.startswith("americanfootball"):
        return False
    if key.startswith("aussierules") or key.startswith("baseball") or key.startswith("basketball"):
        return False
    if key.startswith("soccer_"):
        return True
    if key == "icehockey_nhl":
        return True
    if key.startswith("tennis_"):
        return True
    return False


def _sport_key_allowed(sport: dict[str, Any]) -> bool:
    return bool(sport.get("active")) and _key_allowed(str(sport.get("key") or ""))


def _wanted_groups(app: Any) -> tuple[bool, bool, bool]:
    try:
        with app.db() as con:
            rows = con.execute(
                """
                SELECT DISTINCT LOWER(COALESCE(sport,'')) AS sport,
                                LOWER(COALESCE(league,'')) AS league
                FROM matches
                WHERE status='open'
                """
            ).fetchall()
    except Exception:
        return True, True, True

    if not rows:
        return True, True, True

    has_soccer = False
    has_nhl = False
    has_tennis = False
    for row in rows:
        text = f"{row['sport']} {row['league']}"
        has_soccer = has_soccer or "football" in text or "soccer" in text
        has_nhl = has_nhl or "hockey" in text or "nhl" in text
        has_tennis = has_tennis or "tennis" in text or "atp" in text or "wta" in text
    return has_soccer, has_nhl, has_tennis


def _configured_sport_keys(app: Any, sports: list[dict[str, Any]]) -> list[str]:
    logger = _log(app)
    max_keys = _safe_int(os.getenv("ODDS_MAX_SPORT_KEYS", "8"), 8, 1, 10)
    explicit = [_clean_key(item) for item in os.getenv("ODDS_SPORT_KEYS", "").split(",") if item.strip()]
    if explicit:
        keys = [key for key in explicit if _key_allowed(key)]
        skipped = [key for key in explicit if key not in keys]
        if skipped and logger:
            logger.info("market odds guard: skipped unsupported ODDS_SPORT_KEYS=%s", ",".join(skipped))
        return keys[:max_keys]

    active = {_clean_key(sport.get("key")): sport for sport in sports if sport.get("active") and sport.get("key")}
    has_soccer, has_nhl, has_tennis = _wanted_groups(app)
    selected: list[str] = []

    def add(key: str) -> None:
        if key in active and _key_allowed(key) and key not in selected:
            if key.startswith("soccer_") and not has_soccer:
                return
            if key == "icehockey_nhl" and not has_nhl:
                return
            if key.startswith("tennis_") and not has_tennis:
                return
            selected.append(key)

    for key in PREFERRED_KEYS:
        add(key)

    for key in sorted(active):
        if len(selected) >= max_keys:
            break
        add(key)

    if logger:
        logger.info("market odds guard: selected sport keys=%s", ",".join(selected) or "none")
    return selected[:max_keys]


def _provider_and_key(app: Any) -> tuple[str, str]:
    provider = os.getenv("ODDS_PROVIDER", "theoddsapi").strip().lower()
    api_key = (os.getenv("ODDS_API_KEY") or getattr(app, "ODDS_API_KEY", "") or "").strip()
    return provider, api_key


def _open_match_stats(app: Any) -> dict[str, Any]:
    stats = {
        "open_total": 0,
        "candidate_total": 0,
        "sports": {},
        "sources": {},
        "market_priced": 0,
        "ai_priced": 0,
        "unpriced": 0,
    }
    try:
        lookahead = _safe_int(
            os.getenv("ODDS_LOOKAHEAD_HOURS", str(getattr(app, "ODDS_LOOKAHEAD_HOURS", 72))),
            72,
            1,
            24 * 30,
        )
        now = app.now_utc()
        start_param = app.iso(now - timedelta(hours=2))
        end_param = app.iso(now + timedelta(hours=lookahead))
        with app.db() as con:
            rows = con.execute(
                """
                SELECT sport, league, odds_source, odds_1, odds_x, odds_2,
                       COALESCE(start_time_utc, start_time) AS starts_at
                FROM matches
                WHERE status='open'
                """
            ).fetchall()
            stats["candidate_total"] = int(
                con.execute(
                    """
                    SELECT COUNT(*) AS c
                    FROM matches
                    WHERE status='open'
                      AND COALESCE(start_time_utc, start_time) >= ?
                      AND COALESCE(start_time_utc, start_time) <= ?
                    """,
                    (start_param, end_param),
                ).fetchone()["c"]
                or 0
            )
    except Exception:
        return stats

    stats["open_total"] = len(rows)
    for row in rows:
        sport = str(row["sport"] or "other").lower() or "other"
        source = str(row["odds_source"] or "none").strip() or "none"
        stats["sports"][sport] = int(stats["sports"].get(sport, 0)) + 1
        stats["sources"][source] = int(stats["sources"].get(source, 0)) + 1
        has_odds = bool(row["odds_1"] or row["odds_x"] or row["odds_2"])
        source_upper = source.upper()
        if has_odds and source != "none" and not source_upper.startswith("AI"):
            stats["market_priced"] += 1
        elif has_odds:
            stats["ai_priced"] += 1
        else:
            stats["unpriced"] += 1
    return stats


def _cooldown_left(app: Any) -> int:
    min_interval = _safe_int(os.getenv("ODDS_MIN_REFRESH_INTERVAL", "900"), 900, 60, 7200)
    try:
        now_ts = int(app.now_utc().timestamp())
    except Exception:
        now_ts = int(time.time())
    last_ts = int(getattr(app, "_MARKET_ODDS_LAST_REFRESH_TS", 0) or 0)
    if not last_ts:
        return 0
    return max(0, min_interval - (now_ts - last_ts))


def _debug_text(app: Any) -> str:
    provider, api_key = _provider_and_key(app)
    has_soccer, has_nhl, has_tennis = _wanted_groups(app)
    stats = _open_match_stats(app)
    explicit = [_clean_key(item) for item in os.getenv("ODDS_SPORT_KEYS", "").split(",") if item.strip()]
    allowed_explicit = [key for key in explicit if _key_allowed(key)]
    blocked_explicit = [key for key in explicit if key not in allowed_explicit]
    groups = []
    if has_soccer:
        groups.append("soccer")
    if has_nhl:
        groups.append("nhl")
    if has_tennis:
        groups.append("tennis")
    lines = [
        "Market odds debug",
        f"version={VERSION}",
        f"provider={provider}",
        f"api_key={'set' if api_key else 'missing'}",
        f"regions={os.getenv('ODDS_REGIONS', getattr(app, 'ODDS_REGIONS', 'eu'))}",
        f"markets={os.getenv('ODDS_MARKETS', getattr(app, 'ODDS_MARKETS', 'h2h'))}",
        f"lookahead_hours={os.getenv('ODDS_LOOKAHEAD_HOURS', getattr(app, 'ODDS_LOOKAHEAD_HOURS', 72))}",
        f"max_sport_keys={os.getenv('ODDS_MAX_SPORT_KEYS', '8')}",
        f"request_delay_seconds={os.getenv('ODDS_REQUEST_DELAY_SECONDS', '1.25')}",
        f"cooldown_left_sec={_cooldown_left(app)}",
        f"wanted_groups={','.join(groups) or 'none'}",
        f"odds_sport_keys={','.join(allowed_explicit) if allowed_explicit else 'auto'}",
        f"blocked_sport_keys={','.join(blocked_explicit) if blocked_explicit else 'none'}",
        f"open_matches={stats['open_total']}",
        f"api_candidates={stats['candidate_total']}",
        f"market_priced={stats['market_priced']}",
        f"ai_priced={stats['ai_priced']}",
        f"unpriced={stats['unpriced']}",
        "sports=" + (", ".join(f"{k}:{v}" for k, v in sorted(stats["sports"].items())) or "none"),
        "sources=" + (", ".join(f"{k}:{v}" for k, v in sorted(stats["sources"].items())) or "none"),
    ]
    return "\n".join(lines)


class _DelayedGetContext:
    def __init__(
        self,
        request_factory: Callable[..., Any],
        delay: float,
        lock: asyncio.Lock,
        state: dict[str, float],
        *args: Any,
        **kwargs: Any,
    ) -> None:
        self._request_factory = request_factory
        self._delay = delay
        self._lock = lock
        self._state = state
        self._args = args
        self._kwargs = kwargs
        self._ctx: Any = None

    async def __aenter__(self) -> Any:
        async with self._lock:
            last_ts = float(self._state.get("last_request_ts", 0.0) or 0.0)
            wait_s = max(0.0, self._delay - (time.monotonic() - last_ts)) if last_ts else 0.0
            if wait_s > 0:
                await asyncio.sleep(wait_s)
            self._state["last_request_ts"] = time.monotonic()
        self._ctx = self._request_factory(*self._args, **self._kwargs)
        return await self._ctx.__aenter__()

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> Any:
        if self._ctx is None:
            return None
        return await self._ctx.__aexit__(exc_type, exc, tb)


def _patch_request_delay(app: Any) -> Callable[[], None]:
    delay = _safe_float(os.getenv("ODDS_REQUEST_DELAY_SECONDS", "1.25"), 1.25, 0.0, 10.0)
    aiohttp = getattr(app, "aiohttp", None)
    if delay <= 0 or aiohttp is None or not hasattr(aiohttp, "ClientSession"):
        return lambda: None

    original_client_session = aiohttp.ClientSession
    if getattr(original_client_session, "_market_odds_delayed", False):
        return lambda: None

    lock = asyncio.Lock()
    state = {"last_request_ts": 0.0}

    class DelayedClientSession:
        _market_odds_delayed = True

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self._session = original_client_session(*args, **kwargs)

        async def __aenter__(self) -> Any:
            await self._session.__aenter__()
            return self

        async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> Any:
            return await self._session.__aexit__(exc_type, exc, tb)

        def get(self, *args: Any, **kwargs: Any) -> _DelayedGetContext:
            return _DelayedGetContext(self._session.get, delay, lock, state, *args, **kwargs)

        def __getattr__(self, name: str) -> Any:
            return getattr(self._session, name)

    aiohttp.ClientSession = DelayedClientSession

    def restore() -> None:
        aiohttp.ClientSession = original_client_session

    return restore


def _wrap_refresh(app: Any, market_odds: Any) -> None:
    original = getattr(market_odds, "refresh_market_odds_once", None)
    if not callable(original) or getattr(original, "_guard_wrapped", False):
        return

    app._MARKET_ODDS_GUARD_ORIGINAL_REFRESH = original

    async def guarded_refresh_market_odds_once(bot_app: Any) -> int:
        provider, api_key = _provider_and_key(bot_app)
        logger = _log(bot_app)
        if provider not in {"theoddsapi", "the_odds_api", "market"}:
            if logger:
                logger.warning("market odds guard: disabled provider=%s", provider)
            return 0
        if not api_key:
            if logger:
                logger.warning("market odds guard: ODDS_API_KEY is missing")
            return 0

        force = bool(getattr(bot_app, "_MARKET_ODDS_FORCE_REFRESH", False))
        bot_app._MARKET_ODDS_FORCE_REFRESH = False
        min_interval = _safe_int(os.getenv("ODDS_MIN_REFRESH_INTERVAL", "900"), 900, 60, 7200)
        now_ts = int(bot_app.now_utc().timestamp())
        last_ts = int(getattr(bot_app, "_MARKET_ODDS_LAST_REFRESH_TS", 0) or 0)
        if not force and last_ts and now_ts - last_ts < min_interval:
            if logger:
                logger.info("market odds guard: skipped refresh cooldown=%ss", min_interval - (now_ts - last_ts))
            return 0

        bot_app._MARKET_ODDS_LAST_REFRESH_TS = now_ts
        restore_delay = _patch_request_delay(bot_app)
        try:
            updated = await original(bot_app)
            if logger:
                logger.info("market odds guard: refresh done updated=%s force=%s", updated, force)
            return int(updated or 0)
        except Exception:
            bot_app._MARKET_ODDS_LAST_REFRESH_TS = 0
            raise
        finally:
            restore_delay()

    guarded_refresh_market_odds_once._guard_wrapped = True
    market_odds.refresh_market_odds_once = guarded_refresh_market_odds_once
    app.refresh_odds_once = lambda: guarded_refresh_market_odds_once(app)


def _register_commands(app: Any, market_odds: Any) -> None:
    if getattr(app, "_MARKET_ODDS_GUARD_COMMANDS_REGISTERED", False):
        return

    async def market_odds_debug_cmd(m: Any) -> Any:
        if not m.from_user or (hasattr(app, "is_admin") and not app.is_admin(m.from_user.id)):
            return await m.answer("Not enough rights.")
        return await m.answer(f"<pre>{escape(_debug_text(app))}</pre>")

    async def market_odds_force_cmd(m: Any) -> Any:
        if not m.from_user or (hasattr(app, "is_admin") and not app.is_admin(m.from_user.id)):
            return await m.answer("Not enough rights.")
        await m.answer("Forcing market odds refresh...")
        app._MARKET_ODDS_FORCE_REFRESH = True
        try:
            market_updated = await market_odds.refresh_market_odds_once(app)
            ai_updated = 0
            refresh_ai = getattr(app, "refresh_ai_odds_for_open_matches", None)
            if callable(refresh_ai):
                ai_updated = int(refresh_ai() or 0)
            text = f"market_updated={market_updated}\nai_fallback={ai_updated}\n\n{_debug_text(app)}"
            return await m.answer(f"<pre>{escape(text)}</pre>")
        except Exception as exc:
            logger = _log(app)
            if logger:
                logger.exception("market_odds_force failed")
            return await m.answer(f"Market odds refresh failed: {escape(str(exc))}")

    app.dp.message(app.Command("market_odds_debug"))(market_odds_debug_cmd)
    app.dp.message(app.Command("market_odds_force"))(market_odds_force_cmd)
    app._MARKET_ODDS_GUARD_COMMANDS_REGISTERED = True


def apply(app: Any) -> None:
    if getattr(app, "_MARKET_ODDS_GUARD_APPLIED", False):
        return
    try:
        import market_odds
    except Exception as exc:
        logger = _log(app)
        if logger:
            logger.exception("market odds guard import failed: %s", exc)
        return

    market_odds._sport_key_allowed = _sport_key_allowed
    market_odds._configured_sport_keys = _configured_sport_keys
    _wrap_refresh(app, market_odds)
    _register_commands(app, market_odds)
    app._MARKET_ODDS_GUARD_APPLIED = True
    print(f"{VERSION}_APPLIED")
