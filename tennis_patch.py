from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from typing import Any

VERSION = "TENNIS_PATCH_V1"
ESPN_TENNIS_BASE = "https://site.api.espn.com/apis/site/v2/sports/tennis"


def _enabled() -> bool:
    return os.getenv("TENNIS_ENABLED", "1") == "1"


def _tours() -> list[str]:
    raw = os.getenv("TENNIS_TOURS", "atp,wta")
    tours: list[str] = []
    for item in raw.split(","):
        tour = item.strip().lower()
        if tour in ("atp", "wta") and tour not in tours:
            tours.append(tour)
    return tours or ["atp", "wta"]


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    if isinstance(row, dict):
        return row.get(key, default)
    try:
        return row[key]
    except Exception:
        return default


def _parse_dt(value: str | None) -> datetime | None:
    raw = (value or "").strip()
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


def _event_name(event: dict[str, Any]) -> str:
    return str(event.get("shortName") or event.get("name") or "Tennis").strip() or "Tennis"


def _group_name(grouping: dict[str, Any], competition: dict[str, Any]) -> str:
    group = grouping.get("grouping") or {}
    ctype = competition.get("type") or {}
    return str(group.get("displayName") or ctype.get("text") or "").strip()


def _round_name(competition: dict[str, Any]) -> str:
    round_obj = competition.get("round") or {}
    return str(round_obj.get("displayName") or "").strip()


def _competitor_name(comp: dict[str, Any]) -> str:
    athlete = comp.get("athlete") or {}
    for key in ("displayName", "fullName", "shortName", "name"):
        value = str(athlete.get(key) or "").strip()
        if value:
            return value

    athletes = comp.get("athletes") or []
    names = []
    for item in athletes:
        athlete = item.get("athlete") if isinstance(item, dict) else None
        if not isinstance(athlete, dict):
            athlete = item if isinstance(item, dict) else {}
        name = str(athlete.get("displayName") or athlete.get("fullName") or "").strip()
        if name:
            names.append(name)
    if names:
        return " / ".join(names[:2])

    team = comp.get("team") or {}
    for key in ("displayName", "name", "shortDisplayName", "abbreviation"):
        value = str(team.get(key) or "").strip()
        if value:
            return value

    return str(comp.get("displayName") or comp.get("name") or "").strip()


def _ordered_competitors(competition: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]] | None:
    competitors = [c for c in (competition.get("competitors") or []) if isinstance(c, dict)]
    if len(competitors) < 2:
        return None

    home = next((c for c in competitors if str(c.get("homeAway") or "").lower() == "home"), None)
    away = next((c for c in competitors if str(c.get("homeAway") or "").lower() == "away"), None)
    if home and away:
        return home, away

    def order_key(c: dict[str, Any]) -> int:
        try:
            return int(c.get("order") or 99)
        except Exception:
            return 99

    ordered = sorted(competitors, key=order_key)
    return ordered[0], ordered[1]


def _iter_competitions(data: dict[str, Any]):
    for event in data.get("events") or []:
        if not isinstance(event, dict):
            continue
        for grouping in event.get("groupings") or []:
            if not isinstance(grouping, dict):
                continue
            for competition in grouping.get("competitions") or []:
                if isinstance(competition, dict):
                    yield event, grouping, competition


def _is_completed(competition: dict[str, Any]) -> bool:
    status = competition.get("status") or {}
    stype = status.get("type") or {}
    if bool(stype.get("completed")):
        return True
    return str(stype.get("state") or "").lower() == "post"


def _is_tennis(row_or_match: Any) -> bool:
    sport = str(_row_get(row_or_match, "sport", "") or "").lower()
    league = str(_row_get(row_or_match, "league", "") or "").lower()
    return "tennis" in sport or "tennis" in league or "atp" in league or "wta" in league


def _external_id(comp_id: str) -> str:
    return f"espn-tennis-{comp_id}"


def _competition_to_match(app: Any, event: dict[str, Any], grouping: dict[str, Any], competition: dict[str, Any], window_start: datetime, window_end: datetime):
    comp_id = str(competition.get("id") or "").strip()
    if not comp_id or _is_completed(competition):
        return None

    start = _parse_dt(str(competition.get("date") or competition.get("startDate") or ""))
    if not start or start <= app.now_utc() or start < window_start or start > window_end:
        return None

    ordered = _ordered_competitors(competition)
    if not ordered:
        return None
    home_c, away_c = ordered
    home = _competitor_name(home_c)
    away = _competitor_name(away_c)
    if not home or not away:
        return None

    pieces = ["Tennis", _event_name(event)]
    group = _group_name(grouping, competition)
    rnd = _round_name(competition)
    if group:
        pieces.append(group)
    if rnd:
        pieces.append(rnd)
    league = " · ".join(dict.fromkeys(pieces))

    return app.SyncedMatch(
        source="tennis",
        external_id=_external_id(comp_id),
        sport="tennis",
        league=league,
        title=f"{home} vs {away}",
        start_time_utc=start,
    )


async def tennis_list(app: Any, session, date_from: datetime, date_to: datetime):
    if not _enabled():
        return []

    window_start = date_from.astimezone(timezone.utc)
    window_end = max(date_to.astimezone(timezone.utc), window_start + timedelta(days=3))
    window_end = min(window_end, window_start + timedelta(days=30))

    out = []
    seen: set[str] = set()
    for tour in _tours():
        url = f"{ESPN_TENNIS_BASE}/{tour}/scoreboard"
        try:
            data = await app.http_json(session, url, timeout_s=25)
        except TypeError:
            data = await app.http_json(session, url)
        except Exception as exc:
            app.logger.warning("tennis scoreboard failed tour=%s err=%s", tour, exc)
            continue

        for event, grouping, competition in _iter_competitions(data or {}):
            match = _competition_to_match(app, event, grouping, competition, window_start, window_end)
            if not match or match.external_id in seen:
                continue
            seen.add(match.external_id)
            out.append(match)

    out.sort(key=lambda item: item.start_time_utc)
    app.logger.info("tennis provider: total tennis matches=%s", len(out))
    return out


def _sets_won(comp: dict[str, Any]) -> int:
    total = 0
    for item in comp.get("linescores") or []:
        if isinstance(item, dict) and bool(item.get("winner")):
            total += 1
    return total


async def tennis_result(app: Any, session, external_id: str):
    comp_id = str(external_id or "").replace("espn-tennis-", "", 1)
    if not comp_id or comp_id == external_id:
        return None

    for tour in _tours():
        url = f"{ESPN_TENNIS_BASE}/{tour}/scoreboard"
        try:
            data = await app.http_json(session, url, timeout_s=25)
        except TypeError:
            data = await app.http_json(session, url)
        except Exception as exc:
            app.logger.warning("tennis result failed tour=%s id=%s err=%s", tour, comp_id, exc)
            continue

        for _, _, competition in _iter_competitions(data or {}):
            if str(competition.get("id") or "") != comp_id:
                continue
            if not _is_completed(competition):
                return None
            ordered = _ordered_competitors(competition)
            if not ordered:
                return None
            home_c, away_c = ordered
            home_won = bool(home_c.get("winner"))
            away_won = bool(away_c.get("winner"))
            if not home_won and not away_won:
                return None
            home_sets = _sets_won(home_c)
            away_sets = _sets_won(away_c)
            if home_sets == away_sets:
                home_sets = 1 if home_won else 0
                away_sets = 1 if away_won else 0
            return app.FinishedInfo("1" if home_won else "2", home_sets, away_sets)
    return None


def _patch_sport_labels(app: Any) -> None:
    try:
        app.SPORT_PRETTY["tennis"] = "Tennis"
    except Exception:
        pass

    original_emoji = getattr(app, "_sport_emoji", None)
    if callable(original_emoji) and not getattr(original_emoji, "_tennis_wrapped", False):
        def sport_emoji(sport: str) -> str:
            if "tennis" in str(sport or "").lower():
                return "Tennis"
            return original_emoji(sport)

        sport_emoji._tennis_wrapped = True
        app._sport_emoji = sport_emoji


def _patch_ai_line(app: Any) -> None:
    original_key = getattr(app, "_sport_key_for_ai", None)
    if callable(original_key) and not getattr(original_key, "_tennis_wrapped", False):
        def sport_key_for_ai(sport: str | None, league: str | None = None) -> str:
            text = f"{sport or ''} {league or ''}".lower()
            if "tennis" in text or " atp" in text or " wta" in text:
                return "tennis"
            return original_key(sport, league)

        sport_key_for_ai._tennis_wrapped = True
        app._sport_key_for_ai = sport_key_for_ai

    original_odds = getattr(app, "ai_odds_for_match", None)
    if callable(original_odds) and not getattr(original_odds, "_tennis_wrapped", False):
        def tennis_probs(match: dict[str, Any]) -> tuple[float, None, float]:
            teams = app._parse_title_teams(str(match.get("title") or "")) if hasattr(app, "_parse_title_teams") else None
            if not teams:
                return 0.50, None, 0.50
            home, away = teams
            try:
                home_elo = int(app.get_team_elo("tennis", home))
                away_elo = int(app.get_team_elo("tennis", away))
            except Exception:
                home_elo = away_elo = 1500
            try:
                home_form = int(app.get_form_bonus("tennis", home, 5))
                away_form = int(app.get_form_bonus("tennis", away, 5))
            except Exception:
                home_form = away_form = 0
            diff = (home_elo + home_form) - (away_elo + away_form)
            p1 = 1.0 / (1.0 + 10 ** (-diff / 420.0))
            p1 = max(0.18, min(0.82, p1))
            return float(p1), None, float(1.0 - p1)

        def ai_odds_for_match(match: dict[str, Any]) -> dict[str, Any]:
            if not _is_tennis(match):
                return original_odds(match)
            p1, px, p2 = tennis_probs(match)
            o1, ox, o2 = app.probs_to_odds(p1, px, p2) if callable(getattr(app, "probs_to_odds", None)) else app.probs_to_odds(p1, px, p2)
            out = dict(match)
            teams = app._parse_title_teams(str(out.get("title") or "")) if hasattr(app, "_parse_title_teams") else None
            if teams:
                out["home_team"] = teams[0]
                out["away_team"] = teams[1]
            out["prob_1"] = round(p1, 4)
            out["prob_x"] = None
            out["prob_2"] = round(p2, 4)
            out["odds_1"] = o1
            out["odds_x"] = ox
            out["odds_2"] = o2
            out["odds_source"] = VERSION
            out["odds_updated_at"] = app.iso(app.now_utc())
            return out

        ai_odds_for_match._tennis_wrapped = True
        app.ai_odds_for_match = ai_odds_for_match

        def match_odds_for_pick(match: dict[str, Any], pick: str) -> float | None:
            priced = ai_odds_for_match(dict(match))
            if pick == "1":
                return priced.get("odds_1")
            if pick == "2":
                return priced.get("odds_2")
            if pick == "X":
                return priced.get("odds_x")
            return None

        app.match_odds_for_pick = match_odds_for_pick


def _patch_mini_app(app: Any) -> None:
    try:
        import mini_app
    except Exception:
        return

    original_available = getattr(mini_app, "_available_picks", None)
    if callable(original_available) and not getattr(original_available, "_tennis_wrapped", False):
        def available_picks(row, priced: dict[str, Any]) -> list[str]:
            if _is_tennis(row):
                return ["1", "2"]
            return original_available(row, priced)

        available_picks._tennis_wrapped = True
        mini_app._available_picks = available_picks

    original_index = getattr(mini_app, "_index_html", None)
    if callable(original_index) and not getattr(original_index, "_tennis_wrapped", False):
        def index_html() -> str:
            html = original_index()
            html = html.replace(
                "nhl:'🏒 Хоккей', all:'Все'",
                "nhl:'🏒 Хоккей', tennis:'Tennis', all:'Все'",
            )
            return html

        index_html._tennis_wrapped = True
        mini_app._index_html = index_html


def _patch_sync(app: Any) -> None:
    if getattr(app, "_TENNIS_SYNC_PATCHED", False):
        return

    async def autosync_once() -> str:
        start = app.now_utc()
        end = start + timedelta(days=max(1, int(getattr(app, "SYNC_LOOKAHEAD_DAYS", 10) or 10)))
        report: list[str] = []
        async with app.aiohttp.ClientSession() as session:
            all_matches = []
            if getattr(app, "FOOTBALL_ENABLED", False):
                fm = await app.football_list(session, start, end)
                all_matches.extend(fm)
                report.append(f"Football {len(fm)}")
            if getattr(app, "NHL_ENABLED", False):
                nm = await app.nhl_list(session, start, end)
                all_matches.extend(nm)
                report.append(f"NHL {len(nm)}")
            if _enabled():
                tm = await tennis_list(app, session, start, end)
                all_matches.extend(tm)
                report.append(f"Tennis {len(tm)}")
            inserted, updated = app.upsert_matches(all_matches)
            report.append(f"DB +{inserted}/~{updated}")
        msg = "Sync: " + " | ".join(report) if report else "Sync: nothing"
        app.logger.info(msg)
        return msg

    async def auto_results_loop() -> None:
        async with app.aiohttp.ClientSession() as session:
            while True:
                try:
                    await asyncio.sleep(max(30, int(getattr(app, "AUTO_RESULTS_INTERVAL", 300) or 300)))
                    if not getattr(app, "AUTO_RESULTS_ENABLED", True):
                        continue

                    cutoff = app.now_utc() - timedelta(minutes=max(0, int(getattr(app, "AUTO_RESULTS_MIN_AGE_MIN", 20) or 20)))
                    with app.db() as con:
                        candidates = con.execute(
                            """
                            SELECT id, source, external_id
                            FROM matches
                            WHERE status='open'
                              AND source IS NOT NULL AND external_id IS NOT NULL
                              AND COALESCE(start_time_utc, start_time) <= ?
                            ORDER BY COALESCE(start_time_utc, start_time) ASC
                            LIMIT 80
                            """,
                            (app.iso(cutoff),),
                        ).fetchall()

                    for row in candidates:
                        match_id = int(row["id"])
                        source = str(row["source"] or "").lower()
                        external_id = str(row["external_id"] or "").strip()
                        if not external_id:
                            continue

                        fin = None
                        if source == "football":
                            try:
                                fin = await app.football_result(session, external_id)
                            except Exception as exc:
                                app.logger.warning("football_result failed: %s", exc)
                        elif source == "nhl":
                            try:
                                fin = await app.nhl_result(session, external_id)
                            except Exception as exc:
                                app.logger.warning("nhl_result failed: %s", exc)
                        elif source == "tennis":
                            try:
                                fin = await tennis_result(app, session, external_id)
                            except Exception as exc:
                                app.logger.warning("tennis_result failed: %s", exc)

                        if not fin:
                            continue

                        await app.apply_scoring_for_match(match_id, fin.result_1x2, fin.home_score, fin.away_score)
                        admin_id = int(getattr(app, "ADMIN_ID", 0) or 0)
                        if admin_id:
                            try:
                                await app.bot.send_message(admin_id, f"✅ Матч закрыт: {fin.result_1x2} (id={match_id})")
                            except Exception:
                                pass
                except Exception as exc:
                    app.logger.exception("auto_results_loop error: %s", exc)

    app.tennis_list = lambda session, date_from, date_to: tennis_list(app, session, date_from, date_to)
    app.tennis_result = lambda session, external_id: tennis_result(app, session, external_id)
    app.autosync_once = autosync_once
    app.auto_results_loop = auto_results_loop
    app._TENNIS_SYNC_PATCHED = True


def apply(app: Any) -> None:
    if getattr(app, "_TENNIS_PATCH_APPLIED", False):
        return
    os.environ.setdefault("TENNIS_ENABLED", "1")
    _patch_sport_labels(app)
    _patch_ai_line(app)
    _patch_mini_app(app)
    _patch_sync(app)
    app._TENNIS_PATCH_APPLIED = True
    print(f"{VERSION}_APPLIED")
