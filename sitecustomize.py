"""Runtime defaults and API fallback for the Render deployment.

Python imports this module automatically on startup when it is present on
sys.path. The bot reads its environment at import time, so this is a small
place to keep deployment-safe defaults and patch a disabled football-data.org
account without rewriting the main bot file.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlencode, urlparse


def _ensure_min_int(name: str, minimum: int) -> None:
    raw = os.getenv(name, "").strip()
    try:
        value = int(raw) if raw else 0
    except ValueError:
        value = 0
    if value < minimum:
        os.environ[name] = str(minimum)


os.environ.setdefault("FOOTBALL_ENABLED", "1")
_ensure_min_int("SYNC_LOOKAHEAD_DAYS", 7)


TSDB_LEAGUES = {
    "PL": ("4328", "English Premier League"),
    "CL": ("4480", "UEFA Champions League"),
    "PD": ("4335", "Spanish La Liga"),
    "SA": ("4332", "Italian Serie A"),
    "BL1": ("4331", "German Bundesliga"),
    "FL1": ("4334", "French Ligue 1"),
}


def _tsdb_base() -> str:
    key = (os.getenv("THESPORTSDB_API_KEY") or os.getenv("THESPORTSDB_KEY") or "123").strip()
    return f"https://www.thesportsdb.com/api/v1/json/{key}"


def _parse_dt(value: str) -> datetime | None:
    value = (value or "").strip()
    if not value:
        return None
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    elif "+" not in value[-6:] and "-" not in value[-6:]:
        value += "+00:00"
    try:
        return datetime.fromisoformat(value).astimezone(timezone.utc)
    except ValueError:
        return None


def _event_timestamp(event: dict) -> str:
    ts = event.get("strTimestamp") or ""
    if not ts:
        date = event.get("dateEvent") or ""
        time = event.get("strTime") or "00:00:00"
        ts = f"{date}T{time}" if date else ""
    if ts and not ts.endswith("Z") and "+" not in ts[-6:] and "-" not in ts[-6:]:
        ts += "Z"
    return ts


def _event_to_match(event: dict, fallback_league: str) -> dict | None:
    event_id = str(event.get("idEvent") or "").strip()
    home = (event.get("strHomeTeam") or "").strip()
    away = (event.get("strAwayTeam") or "").strip()
    ts = _event_timestamp(event)
    if not event_id or not home or not away or not ts:
        return None
    return {
        "id": event_id,
        "utcDate": ts,
        "status": "SCHEDULED",
        "competition": {"name": event.get("strLeague") or fallback_league},
        "homeTeam": {"name": home, "shortName": home, "tla": home[:3].upper()},
        "awayTeam": {"name": away, "shortName": away, "tla": away[:3].upper()},
    }


class _FakeResponse:
    status = 200

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    async def text(self) -> str:
        return json.dumps(self._payload)

    async def json(self) -> dict:
        return self._payload


class _FakeContext:
    def __init__(self, coro) -> None:
        self._coro = coro
        self._response = None

    def __await__(self):
        return self._coro.__await__()

    async def __aenter__(self):
        self._response = await self._coro
        return self._response

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


async def _fetch_json_with_original_get(session, original_get, url: str, timeout=None) -> dict:
    async with original_get(session, url, timeout=timeout) as response:
        text = await response.text()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {}


async def _fake_competition_matches(session, original_get, code: str, query: dict, timeout=None) -> _FakeResponse:
    league = TSDB_LEAGUES.get(code.upper())
    if not league:
        return _FakeResponse({"matches": []})

    league_id, league_name = league
    start = _parse_dt((query.get("dateFrom") or [""])[0] + "T00:00:00Z") or datetime.now(timezone.utc)
    end = _parse_dt((query.get("dateTo") or [""])[0] + "T00:00:00Z") or (start + timedelta(days=7))
    if end <= start:
        end = start + timedelta(days=1)

    matches = []
    seen = set()
    day = start.date()
    last_day = (end - timedelta(seconds=1)).date()
    while day <= last_day:
        url = f"{_tsdb_base()}/eventsday.php?" + urlencode({"d": day.isoformat(), "s": "Soccer", "l": league_id})
        data = await _fetch_json_with_original_get(session, original_get, url, timeout=timeout)
        for event in data.get("events") or []:
            event_id = str(event.get("idEvent") or "")
            if event_id in seen:
                continue
            seen.add(event_id)
            match = _event_to_match(event, league_name)
            if match:
                matches.append(match)
        day += timedelta(days=1)

    if not matches:
        url = f"{_tsdb_base()}/eventsnextleague.php?" + urlencode({"id": league_id})
        data = await _fetch_json_with_original_get(session, original_get, url, timeout=timeout)
        for event in data.get("events") or []:
            match = _event_to_match(event, league_name)
            dt = _parse_dt(match["utcDate"]) if match else None
            if match and dt and start <= dt < end:
                matches.append(match)

    return _FakeResponse({"matches": matches})


async def _fake_match(session, original_get, event_id: str, timeout=None) -> _FakeResponse:
    url = f"{_tsdb_base()}/lookupevent.php?" + urlencode({"id": event_id})
    data = await _fetch_json_with_original_get(session, original_get, url, timeout=timeout)
    event = ((data.get("events") or []) + [{}])[0]
    home_score = event.get("intHomeScore")
    away_score = event.get("intAwayScore")
    winner = None
    status = "SCHEDULED"
    if home_score is not None and away_score is not None:
        status = "FINISHED"
        home_i = int(home_score)
        away_i = int(away_score)
        if home_i > away_i:
            winner = "HOME_TEAM"
        elif home_i < away_i:
            winner = "AWAY_TEAM"
        else:
            winner = "DRAW"
    return _FakeResponse({
        "match": {
            "status": status,
            "score": {
                "winner": winner,
                "fullTime": {"home": home_score, "away": away_score},
            },
        }
    })


def _install_thesportsdb_fallback() -> None:
    if os.getenv("THESPORTSDB_ENABLED", "1") != "1":
        return
    try:
        import aiohttp
    except Exception:
        return

    original_get = aiohttp.ClientSession.get

    def patched_get(self, url, *args, **kwargs):
        url_s = str(url)
        parsed = urlparse(url_s)
        if parsed.netloc == "api.football-data.org" and parsed.path.startswith("/v4/"):
            timeout = kwargs.get("timeout")
            match_list = re.search(r"/competitions/([^/]+)/matches$", parsed.path)
            if match_list:
                query = parse_qs(parsed.query)
                return _FakeContext(_fake_competition_matches(self, original_get, match_list.group(1), query, timeout))
            match_one = re.search(r"/matches/([^/]+)$", parsed.path)
            if match_one:
                return _FakeContext(_fake_match(self, original_get, match_one.group(1), timeout))
        return original_get(self, url, *args, **kwargs)

    aiohttp.ClientSession.get = patched_get


def _install_theme_autorun() -> None:
    try:
        import asyncio
        import sys
    except Exception:
        return

    original_run = asyncio.run
    if getattr(original_run, "_predictor_theme_wrapped", False):
        return

    def themed_run(main, *args, **kwargs):
        try:
            frame = getattr(main, "cr_frame", None)
            globs = getattr(frame, "f_globals", {}) if frame else {}
            module_name = globs.get("__name__")
            module = sys.modules.get(module_name)
            looks_like_predictor_bot = all(
                key in globs
                for key in ("dp", "bot", "show_match_card", "InlineKeyboardButton", "ReplyKeyboardMarkup")
            )
            if module is not None and looks_like_predictor_bot and not getattr(module, "_PRETTY_THEME_APPLIED", False):
                import theme

                theme.apply(module)
                print("SITECUSTOMIZE_THEME_APPLIED")
        except Exception as exc:
            try:
                print(f"SITECUSTOMIZE_THEME_FAILED {exc}")
            except Exception:
                pass
        return original_run(main, *args, **kwargs)

    themed_run._predictor_theme_wrapped = True
    asyncio.run = themed_run


def _install_mini_betting_autorun() -> None:
    try:
        import builtins
        import importlib
        import sys
    except Exception:
        return

    def apply_betting(bot_module) -> None:
        if bot_module is None or getattr(bot_module, "_MINI_APP_BETTING_APPLIED", False):
            return
        try:
            import mini_betting_patch

            mini_betting_patch.apply(bot_module)
        except Exception as exc:
            try:
                print(f"SITECUSTOMIZE_BETTING_FAILED {exc}")
            except Exception:
                pass

    def wrap_theme(theme_module) -> None:
        apply_fn = getattr(theme_module, "apply", None)
        if not callable(apply_fn) or getattr(apply_fn, "_mini_betting_wrapped", False):
            return

        def apply_with_betting(bot_module, *args, **kwargs):
            result = apply_fn(bot_module, *args, **kwargs)
            apply_betting(bot_module)
            return result

        apply_with_betting._mini_betting_wrapped = True
        theme_module.apply = apply_with_betting

    original_import = builtins.__import__
    if not getattr(original_import, "_mini_betting_import_wrapped", False):
        def import_with_betting(name, globals=None, locals=None, fromlist=(), level=0):
            module = original_import(name, globals, locals, fromlist, level)
            if name == "theme" or name.endswith(".theme"):
                wrap_theme(sys.modules.get("theme") or module)
            return module

        import_with_betting._mini_betting_import_wrapped = True
        builtins.__import__ = import_with_betting

    original_import_module = importlib.import_module
    if not getattr(original_import_module, "_mini_betting_import_wrapped", False):
        def import_module_with_betting(name, package=None):
            module = original_import_module(name, package)
            if name == "theme" or name.endswith(".theme"):
                wrap_theme(module)
            return module

        import_module_with_betting._mini_betting_import_wrapped = True
        importlib.import_module = import_module_with_betting

    theme_module = sys.modules.get("theme")
    if theme_module is not None:
        wrap_theme(theme_module)


_install_thesportsdb_fallback()
_install_theme_autorun()
_install_mini_betting_autorun()
