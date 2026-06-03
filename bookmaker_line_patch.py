from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any

VERSION = "BOOKMAKER_LINE_PATCH_V1"
PICKS = ("1", "X", "2")
DRAW_NAMES = {"draw", "tie", "x", "ничья"}

TEAM_ALIASES = {
    "man city": "manchester city",
    "man utd": "manchester united",
    "man united": "manchester united",
    "psg": "paris saint germain",
    "paris sg": "paris saint germain",
    "spurs": "tottenham hotspur",
    "tottenham": "tottenham hotspur",
    "inter milan": "inter",
    "internazionale": "inter",
    "ac milan": "milan",
    "bayern": "bayern munich",
    "leverkusen": "bayer leverkusen",
    "dortmund": "borussia dortmund",
    "barca": "barcelona",
}


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


def _float_or_none(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number <= 1.0:
        return None
    return round(number, 2)


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


def _norm_team(value: Any) -> str:
    text = str(value or "").casefold().strip()
    text = text.replace("&", " and ")
    text = re.sub(r"\([^)]*\)", " ", text)
    text = re.sub(r"\b(fc|cf|afc|sc|club|team|women|wta|atp|de|the)\b", " ", text)
    text = re.sub(r"[^a-z0-9а-яё\s.-]", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip()
    return TEAM_ALIASES.get(text, text)


def _tokens(value: Any) -> set[str]:
    return {token for token in re.split(r"\s+", _norm_team(value)) if len(token) >= 2}


def _team_score(left: str, right: str) -> int:
    left_norm = _norm_team(left)
    right_norm = _norm_team(right)
    if not left_norm or not right_norm:
        return 0
    if left_norm == right_norm:
        return 10
    if left_norm in right_norm or right_norm in left_norm:
        return 6
    shared = _tokens(left_norm) & _tokens(right_norm)
    if not shared:
        return 0
    return len(shared)


def _event_score(match_home: str, match_away: str, event_home: str, event_away: str) -> tuple[int, bool]:
    direct_home = _team_score(match_home, event_home)
    direct_away = _team_score(match_away, event_away)
    reverse_home = _team_score(match_home, event_away)
    reverse_away = _team_score(match_away, event_home)
    direct = direct_home + direct_away if direct_home and direct_away else 0
    reverse = reverse_home + reverse_away if reverse_home and reverse_away else 0
    if reverse > direct:
        return reverse, True
    return direct, False


def _parse_title_teams(app: Any, title: str) -> tuple[str, str] | None:
    parser = getattr(app, "_parse_title_teams", None)
    if callable(parser):
        try:
            parsed = parser(title)
            if parsed:
                return parsed
        except Exception:
            pass
    for pattern in (r"\s+vs\.?\s+", r"\s+v\.?\s+", r"\s+[—–-]\s+"):
        parts = re.split(pattern, str(title or ""), maxsplit=1, flags=re.IGNORECASE)
        if len(parts) == 2 and parts[0].strip() and parts[1].strip():
            return parts[0].strip(), parts[1].strip()
    return None


def _sport_text(value: Any) -> str:
    return str(value or "").casefold()


def _candidate_accepts_sport_key(candidate: dict[str, Any], sport_key: str) -> bool:
    sport_key = str(sport_key or "").lower()
    text = f"{candidate.get('sport') or ''} {candidate.get('league') or ''}".lower()
    if sport_key.startswith("soccer_"):
        return "football" in text or "soccer" in text
    if sport_key == "icehockey_nhl":
        return "hockey" in text or "nhl" in text
    if sport_key.startswith("tennis_"):
        return "tennis" in text or "atp" in text or "wta" in text
    return True


def _candidate_has_draw(candidate: dict[str, Any]) -> bool:
    text = f"{candidate.get('sport') or ''} {candidate.get('league') or ''}".lower()
    if "tennis" in text or "hockey" in text or "nhl" in text:
        return False
    return True


def _is_draw_name(name: str) -> bool:
    normalized = _norm_team(name)
    return normalized in DRAW_NAMES or str(name or "").strip().casefold() in DRAW_NAMES


def _line_from_event(candidate: dict[str, Any], event: dict[str, Any]) -> dict[str, Any] | None:
    odds: dict[str, float | None] = {"1": None, "X": None, "2": None}
    books: dict[str, str] = {}
    draw_allowed = _candidate_has_draw(candidate)

    for book in event.get("bookmakers", []) or []:
        book_key = str(book.get("key") or book.get("title") or "bookmaker").strip()
        if not book_key:
            book_key = "bookmaker"
        book_title = str(book.get("title") or book_key).strip() or book_key
        label = book_key if len(book_key) <= 18 else book_title
        for market in book.get("markets", []) or []:
            if str(market.get("key") or "") != "h2h":
                continue
            for outcome in market.get("outcomes", []) or []:
                price = _float_or_none(outcome.get("price"))
                name = str(outcome.get("name") or "")
                if price is None or not name:
                    continue

                target = ""
                if _is_draw_name(name):
                    if draw_allowed:
                        target = "X"
                else:
                    home_score = _team_score(str(candidate["home"]), name)
                    away_score = _team_score(str(candidate["away"]), name)
                    if home_score > 0 or away_score > 0:
                        target = "1" if home_score >= away_score else "2"

                if target not in PICKS:
                    continue
                if odds[target] is None or price > float(odds[target] or 0):
                    odds[target] = price
                    books[target] = label

    if odds["1"] is None or odds["2"] is None:
        return None

    used_books: list[str] = []
    for pick in PICKS:
        book = books.get(pick)
        if book and book not in used_books:
            used_books.append(book)
    if not used_books:
        return None

    source = "BK:" + ",".join(used_books[:4])
    if len(used_books) > 4:
        source += f"+{len(used_books) - 4}"

    return {
        "odds": odds,
        "books": books,
        "source": source,
        "event_id": event.get("id") or event.get("commence_time") or "",
        "event_home": event.get("home_team") or "",
        "event_away": event.get("away_team") or "",
    }


def _select_sport_keys(app: Any, sports: list[dict[str, Any]]) -> list[str]:
    try:
        import market_odds

        fn = getattr(market_odds, "_configured_sport_keys", None)
        if callable(fn):
            keys = list(fn(app, sports) or [])
            if keys:
                return keys
    except Exception:
        pass

    explicit = [item.strip().lower() for item in os.getenv("ODDS_SPORT_KEYS", "").split(",") if item.strip()]
    if explicit:
        return explicit
    keys = []
    for sport in sports:
        key = str(sport.get("key") or "").lower()
        if not sport.get("active") or not key:
            continue
        if key.startswith("soccer_") or key == "icehockey_nhl" or key.startswith("tennis_"):
            keys.append(key)
    max_keys = int(os.getenv("ODDS_MAX_SPORT_KEYS", "8") or "8")
    return keys[: max(1, max_keys)]


def _request_params(app: Any) -> dict[str, Any]:
    bookmakers = os.getenv("ODDS_BOOKMAKERS", "").strip()
    params = {
        "apiKey": (os.getenv("ODDS_API_KEY") or getattr(app, "ODDS_API_KEY", "") or "").strip(),
        "markets": os.getenv("ODDS_MARKETS", getattr(app, "ODDS_MARKETS", "h2h")),
        "oddsFormat": os.getenv("ODDS_ODDS_FORMAT", getattr(app, "ODDS_ODDS_FORMAT", "decimal")),
        "dateFormat": os.getenv("ODDS_DATE_FORMAT", getattr(app, "ODDS_DATE_FORMAT", "iso")),
    }
    if bookmakers:
        params["bookmakers"] = bookmakers
    else:
        params["regions"] = os.getenv("ODDS_REGIONS", getattr(app, "ODDS_REGIONS", "eu"))
    return params


async def refresh_bookmaker_best_line_once(app: Any) -> int:
    provider = os.getenv("ODDS_PROVIDER", "theoddsapi").strip().lower()
    api_key = (os.getenv("ODDS_API_KEY") or getattr(app, "ODDS_API_KEY", "") or "").strip()
    if provider not in {"theoddsapi", "the_odds_api", "market"} or not api_key:
        return 0

    aiohttp = getattr(app, "aiohttp", None)
    if aiohttp is None:
        return 0

    now = app.now_utc()
    lookahead = int(os.getenv("ODDS_LOOKAHEAD_HOURS", str(getattr(app, "ODDS_LOOKAHEAD_HOURS", 72))) or "72")
    horizon = now + timedelta(hours=max(1, lookahead))
    start_param = app.iso(now - timedelta(hours=2))
    end_param = app.iso(horizon)

    with app.db() as con:
        rows = con.execute(
            """
            SELECT * FROM matches
            WHERE status='open'
              AND COALESCE(start_time_utc, start_time) >= ?
              AND COALESCE(start_time_utc, start_time) <= ?
            ORDER BY COALESCE(start_time_utc, start_time) ASC
            """,
            (start_param, end_param),
        ).fetchall()

    candidates: list[dict[str, Any]] = []
    for row in rows:
        teams = _parse_title_teams(app, str(_row_value(row, "title", "")))
        starts_at = _parse_dt(_row_value(row, "start_time_utc") or _row_value(row, "start_time"))
        if teams and starts_at:
            candidates.append({
                "id": int(_row_value(row, "id")),
                "home": teams[0],
                "away": teams[1],
                "sport": _row_value(row, "sport", ""),
                "league": _row_value(row, "league", ""),
                "starts_at": starts_at,
            })
    if not candidates:
        return 0

    updated = 0
    base_url = (os.getenv("ODDS_BASE_URL") or getattr(app, "ODDS_BASE_URL", "https://api.the-odds-api.com") or "https://api.the-odds-api.com").rstrip("/")
    timeout = aiohttp.ClientTimeout(total=25)
    params = _request_params(app)
    time_window_hours = float(os.getenv("ODDS_MATCH_TIME_WINDOW_HOURS", "18") or "18")
    min_score = int(os.getenv("ODDS_MATCH_MIN_SCORE", "2") or "2")
    logger = _log(app)

    async with aiohttp.ClientSession() as session:
        async with session.get(f"{base_url}/v4/sports", params={"apiKey": api_key, "all": "false"}, timeout=timeout) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise RuntimeError(f"The Odds API sports HTTP {resp.status}: {text[:240]}")
            sports = await resp.json()

        sport_keys = _select_sport_keys(app, sports or [])
        if logger:
            logger.info("bookmaker line: selected sport keys=%s", ",".join(sport_keys) or "none")

        for sport_key in sport_keys:
            try:
                async with session.get(
                    f"{base_url}/v4/sports/{sport_key}/odds",
                    params=params,
                    timeout=timeout,
                ) as resp:
                    if resp.status != 200:
                        text = await resp.text()
                        if logger:
                            logger.warning("bookmaker line fetch failed sport=%s status=%s err=%s", sport_key, resp.status, text[:180])
                        continue
                    events = await resp.json()
            except Exception as exc:
                if logger:
                    logger.warning("bookmaker line fetch failed sport=%s err=%s", sport_key, exc)
                continue

            event_items: list[dict[str, Any]] = []
            for event in events or []:
                event_dt = _parse_dt(event.get("commence_time"))
                home = str(event.get("home_team") or "")
                away = str(event.get("away_team") or "")
                if event_dt and home and away:
                    event_items.append({"event": event, "home": home, "away": away, "starts_at": event_dt})

            sport_updated = 0
            with app.db() as con:
                cur = con.cursor()
                for candidate in candidates:
                    if not _candidate_accepts_sport_key(candidate, sport_key):
                        continue

                    best: dict[str, Any] | None = None
                    best_score = 0
                    best_delta = 10**9
                    for item in event_items:
                        delta_h = abs((item["starts_at"] - candidate["starts_at"]).total_seconds()) / 3600.0
                        if delta_h > time_window_hours:
                            continue
                        score, _ = _event_score(candidate["home"], candidate["away"], item["home"], item["away"])
                        if score > best_score or (score == best_score and delta_h < best_delta):
                            best = item["event"]
                            best_score = score
                            best_delta = delta_h

                    if not best or best_score < min_score:
                        continue

                    line = _line_from_event(candidate, best)
                    if not line:
                        continue
                    odds = line["odds"]
                    cur.execute(
                        """
                        UPDATE matches
                        SET odds_1=?, odds_x=?, odds_2=?, odds_updated_at=?, odds_source=?
                        WHERE id=? AND status='open'
                        """,
                        (odds.get("1"), odds.get("X"), odds.get("2"), app.iso(app.now_utc()), line["source"], candidate["id"]),
                    )
                    updated += int(cur.rowcount or 0)
                    sport_updated += int(cur.rowcount or 0)
                con.commit()

            if logger:
                logger.info("bookmaker line: sport=%s events=%s updated=%s", sport_key, len(event_items), sport_updated)

    return updated


def _patch_market_module(app: Any) -> None:
    try:
        import market_odds
    except Exception as exc:
        logger = _log(app)
        if logger:
            logger.exception("bookmaker line: market_odds import failed: %s", exc)
        return

    if getattr(market_odds.refresh_market_odds_once, "_bookmaker_best_line", False):
        return

    async def patched_refresh_market_odds_once(bot_app: Any) -> int:
        return await refresh_bookmaker_best_line_once(bot_app)

    patched_refresh_market_odds_once._bookmaker_best_line = True
    market_odds.refresh_market_odds_once = patched_refresh_market_odds_once
    app.refresh_odds_once = lambda: patched_refresh_market_odds_once(app)


def apply(app: Any) -> None:
    if getattr(app, "_BOOKMAKER_LINE_PATCH_APPLIED", False):
        return
    os.environ.setdefault("ODDS_PROVIDER", "theoddsapi")
    os.environ.setdefault("ODDS_MARKETS", "h2h")
    _patch_market_module(app)
    app._BOOKMAKER_LINE_PATCH_APPLIED = True
    print(f"{VERSION}_APPLIED")
