from __future__ import annotations

import os
from typing import Any

VERSION = "MARKET_ODDS_GUARD_V1"
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


def _wrap_refresh(app: Any, market_odds: Any) -> None:
    original = getattr(market_odds, "refresh_market_odds_once", None)
    if not callable(original) or getattr(original, "_guard_wrapped", False):
        return

    async def guarded_refresh_market_odds_once(bot_app: Any) -> int:
        min_interval = _safe_int(os.getenv("ODDS_MIN_REFRESH_INTERVAL", "900"), 900, 60, 7200)
        now_ts = int(bot_app.now_utc().timestamp())
        last_ts = int(getattr(bot_app, "_MARKET_ODDS_LAST_REFRESH_TS", 0) or 0)
        if last_ts and now_ts - last_ts < min_interval:
            logger = _log(bot_app)
            if logger:
                logger.info("market odds guard: skipped refresh cooldown=%ss", min_interval - (now_ts - last_ts))
            return 0
        bot_app._MARKET_ODDS_LAST_REFRESH_TS = now_ts
        return await original(bot_app)

    guarded_refresh_market_odds_once._guard_wrapped = True
    market_odds.refresh_market_odds_once = guarded_refresh_market_odds_once
    app.refresh_odds_once = lambda: guarded_refresh_market_odds_once(app)


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
    app._MARKET_ODDS_GUARD_APPLIED = True
    print(f"{VERSION}_APPLIED")
