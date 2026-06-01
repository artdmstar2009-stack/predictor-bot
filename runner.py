from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

# These must be set before importing bot.py because bot.py reads env at import time.
os.environ.setdefault("FOOTBALL_ENABLED", "1")
os.environ.setdefault("THESPORTSDB_ENABLED", "1")

try:
    sync_days = int(os.getenv("SYNC_LOOKAHEAD_DAYS", "0") or "0")
except ValueError:
    sync_days = 0
if sync_days < 10:
    os.environ["SYNC_LOOKAHEAD_DAYS"] = "10"

import bot  # noqa: E402

logger = logging.getLogger("predictor_bot")

THESPORTSDB_API_KEY = (os.getenv("THESPORTSDB_API_KEY") or os.getenv("THESPORTSDB_KEY") or "123").strip()
THESPORTSDB_PUBLIC_KEY = THESPORTSDB_API_KEY == "123"
THESPORTSDB_BASE = f"https://www.thesportsdb.com/api/v1/json/{THESPORTSDB_API_KEY}".rstrip("/")
THESPORTSDB_ENABLED = os.getenv("THESPORTSDB_ENABLED", "1") == "1"
# The public key is heavily rate-limited. Never fan out by day with it; one
# eventsnextleague request per league is enough for the Mini App list.
THESPORTSDB_DAY_SEARCH = False if THESPORTSDB_PUBLIC_KEY else os.getenv("THESPORTSDB_DAY_SEARCH", "1") == "1"
if THESPORTSDB_PUBLIC_KEY and os.getenv("THESPORTSDB_DAY_SEARCH") == "1":
    logger.warning("THESPORTSDB_DAY_SEARCH=1 ignored for public key 123 to avoid HTTP 429")

THESPORTSDB_LEAGUES = {
    "PL": ("4328", "English Premier League"),
    "CL": ("4480", "UEFA Champions League"),
    "PD": ("4335", "Spanish La Liga"),
    "SA": ("4332", "Italian Serie A"),
    "BL1": ("4331", "German Bundesliga"),
    "FL1": ("4334", "French Ligue 1"),
    "WC": ("4429", "FIFA World Cup"),
    "WORLD_CUP": ("4429", "FIFA World Cup"),
}


def _parse_tsdb_datetime(event: dict) -> Optional[datetime]:
    raw = (event.get("strTimestamp") or "").strip()
    if not raw:
        date = (event.get("dateEvent") or "").strip()
        time_s = (event.get("strTime") or "00:00:00").strip()
        raw = f"{date}T{time_s}" if date else ""
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    elif "+" not in raw[-6:] and "-" not in raw[-6:]:
        raw += "+00:00"
    try:
        return datetime.fromisoformat(raw).astimezone(timezone.utc)
    except Exception:
        return None


def _event_to_match(comp: str, league_name: str, event: dict, window_start: datetime, window_end: datetime):
    event_id = str(event.get("idEvent") or "").strip()
    home = (event.get("strHomeTeam") or "").strip()
    away = (event.get("strAwayTeam") or "").strip()
    start = _parse_tsdb_datetime(event)
    if not event_id or not home or not away or not start:
        return None
    if start <= bot.now_utc() or start < window_start or start > window_end:
        return None
    league = (event.get("strLeague") or league_name or comp).strip()
    return bot.SyncedMatch(
        source="football",
        external_id=f"tsdb-{event_id}",
        sport="football",
        league=league,
        title=f"{home} vs {away}",
        start_time_utc=start,
    )


async def _tsdb_json(session, url: str) -> dict:
    try:
        return await bot.http_json(session, url, timeout_s=20)
    except TypeError:
        return await bot.http_json(session, url)


def _add_event(out: list, seen: set, comp: str, league_name: str, event: dict, window_start: datetime, window_end: datetime) -> None:
    event_id = str(event.get("idEvent") or "")
    if not event_id or event_id in seen:
        return
    match = _event_to_match(comp, league_name, event, window_start, window_end)
    if match:
        seen.add(event_id)
        out.append(match)


async def _tsdb_events_for_comp(session, comp: str, date_from: datetime, date_to: datetime):
    if not THESPORTSDB_ENABLED:
        return []
    league = THESPORTSDB_LEAGUES.get(comp.upper())
    if not league:
        logger.info("thesportsdb: no mapping for comp=%s", comp)
        return []

    league_id, league_name = league
    window_start = date_from.astimezone(timezone.utc)
    window_end = max(date_to.astimezone(timezone.utc), window_start + timedelta(days=10))
    window_end = min(window_end + timedelta(days=1), window_start + timedelta(days=30))

    out = []
    seen = set()

    # Keep public key usage low: one compact request per league first.
    url = f"{THESPORTSDB_BASE}/eventsnextleague.php?id={league_id}"
    try:
        data = await _tsdb_json(session, url)
        for event in data.get("events") or []:
            _add_event(out, seen, comp, league_name, event, window_start, window_end)
    except Exception as exc:
        logger.warning("thesportsdb eventsnextleague failed comp=%s err=%s", comp, exc)

    if THESPORTSDB_DAY_SEARCH:
        day = window_start.date()
        while day <= window_end.date():
            url = f"{THESPORTSDB_BASE}/eventsday.php?d={day.isoformat()}&s=Soccer&l={league_id}"
            try:
                data = await _tsdb_json(session, url)
            except Exception as exc:
                logger.warning("thesportsdb eventsday failed comp=%s day=%s err=%s", comp, day.isoformat(), exc)
                day += timedelta(days=1)
                continue
            for event in data.get("events") or []:
                _add_event(out, seen, comp, league_name, event, window_start, window_end)
            day += timedelta(days=1)
    else:
        logger.info("thesportsdb eventsday skipped comp=%s key=%s day_search=0", comp, "public" if THESPORTSDB_PUBLIC_KEY else "private")

    out.sort(key=lambda item: item.start_time_utc)
    logger.info("thesportsdb football_list: comp=%s matches=%s", comp, len(out))
    return out


async def football_list(session, date_from: datetime, date_to: datetime):
    matches = []
    for comp in bot.FOOTBALL_COMPETITIONS:
        comp = comp.strip()
        if not comp:
            continue
        matches.extend(await _tsdb_events_for_comp(session, comp, date_from, date_to))
    logger.info("runner football provider: total football matches=%s", len(matches))
    return matches


async def football_result(session, external_id: str):
    if not external_id.startswith("tsdb-"):
        logger.info("runner football_result: skipping non-TSDB id=%s", external_id)
        return None
    event_id = external_id.replace("tsdb-", "", 1)
    data = await _tsdb_json(session, f"{THESPORTSDB_BASE}/lookupevent.php?id={event_id}")
    event = ((data.get("events") or []) + [{}])[0]
    home_score = event.get("intHomeScore")
    away_score = event.get("intAwayScore")
    if home_score is None or away_score is None:
        return None
    home = int(home_score)
    away = int(away_score)
    if home > away:
        return bot.FinishedInfo("1", home, away)
    if home < away:
        return bot.FinishedInfo("2", home, away)
    return bot.FinishedInfo("X", home, away)


def _ensure_world_cup_competition() -> None:
    comps = [str(c).strip() for c in getattr(bot, "FOOTBALL_COMPETITIONS", []) if str(c).strip()]
    upper = {c.upper() for c in comps}
    if "WC" not in upper and "WORLD_CUP" not in upper:
        comps.append("WC")
    bot.FOOTBALL_COMPETITIONS = comps


_ensure_world_cup_competition()
bot.football_list = football_list
bot.football_result = football_result
bot.FOOTBALL_ENABLED = True
bot.SYNC_LOOKAHEAD_DAYS = max(int(getattr(bot, "SYNC_LOOKAHEAD_DAYS", 1) or 1), 10)

logger.info(
    "runner thesportsdb config: key=%s day_search=%s comps=%s",
    "public" if THESPORTSDB_PUBLIC_KEY else "private",
    THESPORTSDB_DAY_SEARCH,
    ",".join(bot.FOOTBALL_COMPETITIONS),
)
print("RUNNER_THESPORTSDB_FOOTBALL_PROVIDER", "SYNC_LOOKAHEAD_DAYS=", bot.SYNC_LOOKAHEAD_DAYS)


def apply_theme() -> None:
    try:
        import polling_guard  # noqa: E402

        polling_guard.apply(bot)
    except Exception as exc:
        logger.exception("polling guard apply failed: %s", exc)

    try:
        import ai_line  # noqa: E402

        ai_line.apply(bot)
    except Exception as exc:
        logger.exception("AI line apply failed: %s", exc)

    try:
        import theme  # noqa: E402

        theme.apply(bot)
        print("RUNNER_THEME_APPLIED")
    except Exception as exc:
        logger.exception("theme apply failed: %s", exc)


if __name__ == "__main__":
    apply_theme()
    asyncio.run(bot.main())