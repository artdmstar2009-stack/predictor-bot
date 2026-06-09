from __future__ import annotations

import math
import os
import re
from datetime import datetime, timezone, timedelta
from typing import Any

VERSION = "AI_LINE_V4"

COMMON_ALIASES = {
    "man city": "manchester city",
    "manchester city fc": "manchester city",
    "man utd": "manchester united",
    "man united": "manchester united",
    "manchester utd": "manchester united",
    "spurs": "tottenham hotspur",
    "tottenham": "tottenham hotspur",
    "psg": "paris saint germain",
    "paris sg": "paris saint germain",
    "inter milan": "inter",
    "internazionale": "inter",
    "ac milan": "milan",
    "bayern": "bayern munich",
    "bayer 04 leverkusen": "bayer leverkusen",
    "leverkusen": "bayer leverkusen",
    "dortmund": "borussia dortmund",
    "atl madrid": "atletico madrid",
    "atletico de madrid": "atletico madrid",
    "barca": "barcelona",
    "roma": "as roma",
    "sporting": "sporting cp",
    "sporting lisbon": "sporting cp",
    "porto": "fc porto",
    "benfica": "sl benfica",
    "psv": "psv eindhoven",
    "rb leipzig": "rasenballsport leipzig",
}

TEAM_POWER_RAW = {
    # England
    "Manchester City": 1900,
    "Arsenal": 1845,
    "Liverpool": 1835,
    "Chelsea": 1745,
    "Newcastle United": 1730,
    "Tottenham Hotspur": 1725,
    "Aston Villa": 1715,
    "Manchester United": 1705,
    "Brighton and Hove Albion": 1665,
    "West Ham United": 1635,
    "Crystal Palace": 1625,
    "Brentford": 1605,
    "Fulham": 1600,
    "Bournemouth": 1585,
    "Everton": 1585,
    "Wolverhampton Wanderers": 1575,
    "Nottingham Forest": 1565,
    "Leicester City": 1560,
    "Leeds United": 1560,
    "Southampton": 1515,
    "Burnley": 1510,
    "Sheffield United": 1480,
    "Ipswich Town": 1485,
    "Sunderland": 1500,
    # Spain
    "Real Madrid": 1910,
    "Barcelona": 1880,
    "Atletico Madrid": 1810,
    "Athletic Club": 1725,
    "Villarreal": 1690,
    "Real Sociedad": 1690,
    "Real Betis": 1660,
    "Girona": 1650,
    "Sevilla": 1620,
    "Valencia": 1605,
    "Celta Vigo": 1585,
    "Osasuna": 1575,
    "Getafe": 1555,
    "Rayo Vallecano": 1550,
    "Mallorca": 1545,
    "Espanyol": 1520,
    # Italy
    "Inter": 1850,
    "Napoli": 1830,
    "Juventus": 1785,
    "Milan": 1775,
    "Atalanta": 1765,
    "AS Roma": 1715,
    "Lazio": 1705,
    "Fiorentina": 1680,
    "Bologna": 1665,
    "Torino": 1605,
    "Genoa": 1570,
    "Udinese": 1565,
    "Cagliari": 1530,
    # Germany
    "Bayern Munich": 1900,
    "Bayer Leverkusen": 1845,
    "Borussia Dortmund": 1785,
    "RasenBallsport Leipzig": 1775,
    "Eintracht Frankfurt": 1710,
    "Stuttgart": 1710,
    "Wolfsburg": 1635,
    "Borussia Monchengladbach": 1625,
    "Freiburg": 1620,
    "Hoffenheim": 1605,
    "Werder Bremen": 1585,
    "Mainz": 1580,
    "Union Berlin": 1570,
    # France
    "Paris Saint Germain": 1890,
    "Monaco": 1720,
    "Marseille": 1720,
    "Lille": 1700,
    "Lyon": 1685,
    "Lens": 1665,
    "Nice": 1660,
    "Rennes": 1655,
    "Strasbourg": 1580,
    "Nantes": 1545,
    "Toulouse": 1540,
    "Montpellier": 1530,
    # Europe / common Champions League clubs
    "Sporting CP": 1745,
    "SL Benfica": 1740,
    "FC Porto": 1730,
    "PSV Eindhoven": 1760,
    "Feyenoord": 1710,
    "Ajax": 1690,
    "Celtic": 1620,
    "Rangers": 1605,
    "Galatasaray": 1645,
    "Fenerbahce": 1640,
    "Shakhtar Donetsk": 1605,
    "Club Brugge": 1600,
    "Salzburg": 1595,
}

LEAGUE_BASE = {
    "premier league": 1620,
    "english premier league": 1620,
    "uefa champions league": 1730,
    "champions league": 1730,
    "la liga": 1605,
    "spanish la liga": 1605,
    "serie a": 1600,
    "italian serie a": 1600,
    "bundesliga": 1605,
    "german bundesliga": 1605,
    "ligue 1": 1575,
    "french ligue 1": 1575,
    "nhl": 1540,
}

# League-specific home advantage
LEAGUE_HOME_ADV = {
    "premier league": 72,
    "la liga": 68,
    "serie a": 70,
    "bundesliga": 70,
    "ligue 1": 65,
    "champions league": 60,
    "nhl": 45,
}


def _norm(value: Any) -> str:
    s = str(value or "").casefold().strip()
    s = s.replace("&", " and ")
    s = re.sub(r"[^a-z0-9а-яё\s.-]", " ", s, flags=re.IGNORECASE)
    s = re.sub(r"\b(fc|cf|afc|sc|club|de|the)\b", " ", s, flags=re.IGNORECASE)
    s = re.sub(r"\s+", " ", s).strip()
    return COMMON_ALIASES.get(s, s)


TEAM_POWER = {_norm(name): rating for name, rating in TEAM_POWER_RAW.items()}
for alias, canonical in COMMON_ALIASES.items():
    if canonical in TEAM_POWER:
        TEAM_POWER[alias] = TEAM_POWER[canonical]


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _parse_title_teams(bot, title: str) -> tuple[str, str] | None:
    parser = getattr(bot, "_parse_title_teams", None)
    if callable(parser):
        parsed = parser(title)
        if parsed:
            return parsed
    text = str(title or "").strip()
    if not text:
        return None
    for pattern in (r"\s+vs\.?\s+", r"\s+v\.?\s+", r"\s+[—–-]\s+"):
        parts = re.split(pattern, text, maxsplit=1, flags=re.IGNORECASE)
        if len(parts) == 2 and parts[0].strip() and parts[1].strip():
            return parts[0].strip(), parts[1].strip()
    return None


def _sport_key(bot, match: dict) -> str:
    fn = getattr(bot, "_sport_key_for_ai", None)
    if callable(fn):
        return fn(match.get("sport"), match.get("league"))
    s = f"{match.get('sport') or ''} {match.get('league') or ''}".casefold()
    return "nhl" if "nhl" in s or "hockey" in s else "football"


def _league_base(league: str, sport_key: str) -> int:
    if sport_key == "nhl":
        return 1540
    normalized = _norm(league)
    for key, rating in LEAGUE_BASE.items():
        if key in normalized:
            return rating
    return 1540


def _get_league_home_adv(league: str) -> int:
    """Returns league-specific home advantage."""
    normalized = _norm(league).lower()
    for key, adv in LEAGUE_HOME_ADV.items():
        if key in normalized:
            return adv
    return 68  # default


def _db_rating(bot, sport_key: str, team: str) -> int | None:
    try:
        with bot.db() as con:
            row = con.execute(
                "SELECT elo FROM team_ratings WHERE sport=? AND team=?",
                (sport_key, team),
            ).fetchone()
            if row:
                return int(row[0])
            normalized = _norm(team)
            rows = con.execute(
                "SELECT team, elo FROM team_ratings WHERE sport=? LIMIT 500",
                (sport_key,),
            ).fetchall()
            for item in rows:
                if _norm(item[0]) == normalized:
                    return int(item[1])
    except Exception:
        return None
    return None


def _advanced_form_bonus(bot, sport_key: str, team: str) -> int:
    """
    Advanced form analysis with:
    - Streak bonuses (exponential)
    - Win rate percentage
    - Goal differential
    - Momentum curve
    Returns: [-180..+180]
    """
    fn = getattr(bot, "get_team_last_form", None)
    if not callable(fn):
        return 0
    
    try:
        rows = fn(sport_key, team, 10)  # Last 10 matches
        if not rows:
            return 0
        
        # Streak detection
        consecutive_wins = 0
        consecutive_losses = 0
        for r in rows:
            res = (r.get("result") or "").upper()
            if res == "W":
                consecutive_wins += 1
                consecutive_losses = 0
            elif res == "L":
                consecutive_losses += 1
                consecutive_wins = 0
            else:
                break
        
        # Streak bonus (exponential, more aggressive)
        if consecutive_wins >= 5:
            streak_bonus = min(180, 40 + 28 * consecutive_wins)  # 5 wins = +180
        elif consecutive_wins > 0:
            streak_bonus = min(100, 20 + 16 * consecutive_wins)
        elif consecutive_losses >= 5:
            streak_bonus = max(-180, -40 - 28 * consecutive_losses)  # 5 losses = -180
        elif consecutive_losses > 0:
            streak_bonus = max(-100, -20 - 16 * consecutive_losses)
        else:
            streak_bonus = 0
        
        # Overall win rate (last 10)
        wins = sum(1 for r in rows if (r.get("result") or "").upper() == "W")
        losses = sum(1 for r in rows if (r.get("result") or "").upper() == "L")
        draws = sum(1 for r in rows if (r.get("result") or "").upper() == "D")
        
        # Win rate bonus
        win_rate = wins / max(len(rows), 1)
        if win_rate > 0.6:
            wr_bonus = int(40 + (win_rate - 0.6) * 200)  # 100% = +80
        elif win_rate < 0.4:
            wr_bonus = int(-40 + (win_rate - 0.4) * 200)  # 0% = -80
        else:
            wr_bonus = 0
        
        # Goal differential bonus
        gd = sum(int(r.get("gf", 0) or 0) - int(r.get("ga", 0) or 0) for r in rows)
        gd_bonus = int(_clamp(gd * 3, -40, 40))
        
        # Momentum: recent matches weighted more
        momentum = 0
        for i, r in enumerate(rows):
            res = (r.get("result") or "").upper()
            weight = (10 - i) / 10.0  # More recent = higher weight
            if res == "W":
                momentum += 20 * weight
            elif res == "L":
                momentum -= 20 * weight
        momentum = int(_clamp(momentum, -40, 40))
        
        # Combine with intelligent weighting
        final_bonus = int(
            streak_bonus * 0.50 +  # Streaks are most important (50%)
            wr_bonus * 0.25 +      # Win rate (25%)
            gd_bonus * 0.15 +      # Goal differential (15%)
            momentum * 0.10        # Momentum (10%)
        )
        
        return int(_clamp(final_bonus, -180, 180))
    except Exception:
        return 0


def _head_to_head_bonus(bot, sport_key: str, home_team: str, away_team: str) -> tuple[int, int]:
    """
    Analyzes historical H2H between teams.
    Returns (home_bonus, away_bonus) with max ±40 each.
    Weight: max 25% of form bonus effect.
    """
    fn = getattr(bot, "get_h2h_stats", None)
    if not callable(fn):
        return 0, 0
    
    try:
        h2h_data = fn(sport_key, home_team, away_team)
        if not h2h_data or not isinstance(h2h_data, dict):
            return 0, 0
        
        matches = h2h_data.get('matches_count', 0)
        if matches < 2:
            return 0, 0  # Need significant data
        
        home_wins = h2h_data.get('home_wins', 0)
        away_wins = h2h_data.get('away_wins', 0)
        
        home_gf = h2h_data.get('home_goals_for', 0)
        home_ga = h2h_data.get('home_goals_against', 0)
        away_gf = h2h_data.get('away_goals_for', 0)
        away_ga = h2h_data.get('away_goals_against', 0)
        
        # Weight increases with matches count
        h2h_weight = min(matches / 8.0, 1.0)  # 8+ matches = full weight
        
        # Win ratio bonus
        home_wr = (home_wins - away_wins) / max(matches, 1)
        home_wr_bonus = int(home_wr * 80 * h2h_weight)  # ±80 max
        
        # Goal differential bonus
        home_gd = (home_gf - home_ga) / max(matches, 1)
        home_gd_bonus = int(home_gd * 20 * h2h_weight)  # ±20 max
        
        # Combined
        home_bonus = int(_clamp(
            home_wr_bonus * 0.7 + home_gd_bonus * 0.3,
            -40, 40
        ))
        away_bonus = -home_bonus
        
        return home_bonus, away_bonus
    except Exception:
        return 0, 0


def _rest_injury_factor(bot, sport_key: str, home_team: str, away_team: str, match_time: datetime | None = None) -> tuple[int, int]:
    """
    Estimates rest advantage based on match frequency.
    Higher match frequency = higher fatigue risk.
    Returns (home_bonus, away_bonus) with max ±25 each.
    """
    fn = getattr(bot, "get_team_last_form", None)
    if not callable(fn):
        return 0, 0
    
    try:
        match_time = match_time or datetime.now(timezone.utc)
        home_recent = fn(sport_key, home_team, 5)
        away_recent = fn(sport_key, away_team, 5)
        
        # Count matches in last 14 days
        def count_recent_matches(form_rows):
            if not form_rows:
                return 0
            cutoff = match_time - timedelta(days=14)
            count = 0
            for row in form_rows:
                try:
                    mt = row.get("match_time")
                    if isinstance(mt, str):
                        mt = datetime.fromisoformat(mt.replace("Z", "+00:00"))
                    if mt and mt > cutoff:
                        count += 1
                except Exception:
                    pass
            return count
        
        home_matches = count_recent_matches(home_recent)
        away_matches = count_recent_matches(away_recent)
        
        # 3+ matches in 14 days = fatigue factor
        home_fatigue = max(0, (home_matches - 3) * 5)  # +5 per extra match
        away_fatigue = max(0, (away_matches - 3) * 5)
        
        # Home advantage if away team more fatigued
        home_bonus = int(away_fatigue - home_fatigue)
        home_bonus = int(_clamp(home_bonus, -25, 25))
        
        return home_bonus, -home_bonus
    except Exception:
        return 0, 0


def _team_rating(bot, sport_key: str, team: str, league: str) -> int:
    normalized = _norm(team)
    seeded = TEAM_POWER.get(normalized)
    db_value = _db_rating(bot, sport_key, team)

    if seeded is not None and db_value is not None and abs(db_value - 1500) > 15:
        return round(seeded * 0.60 + db_value * 0.40)
    if seeded is not None:
        return int(seeded)
    if db_value is not None and abs(db_value - 1500) > 15:
        return int(db_value)
    return _league_base(league, sport_key)


def _poisson(lam: float, goals: int) -> float:
    return math.exp(-lam) * (lam ** goals) / math.factorial(goals)


def _football_probs(bot, home_rating: int, away_rating: int, league: str, home_team: str, away_team: str, sport_key: str, match_time: datetime | None = None) -> tuple[float, float, float]:
    """
    Football/Soccer probability calculation with advanced factors.
    """
    home_adv = _get_league_home_adv(league)
    
    # Advanced form analysis
    home_form = _advanced_form_bonus(bot, sport_key, home_team)
    away_form = _advanced_form_bonus(bot, sport_key, away_team)
    
    # H2H analysis
    h2h_home, h2h_away = _head_to_head_bonus(bot, sport_key, home_team, away_team)
    
    # Rest/Injury factor
    rest_home, rest_away = _rest_injury_factor(bot, sport_key, home_team, away_team, match_time)
    
    # Combined ELO difference
    diff = (home_rating + home_adv + home_form + h2h_home + rest_home) - \
           (away_rating + away_form + h2h_away + rest_away)

    # xG calculation with advanced model
    base_home = 1.42
    base_away = 1.08
    diff_scale = diff / 700.0
    
    home_xg = _clamp(base_home * math.exp(diff_scale), 0.30, 3.80)
    away_xg = _clamp(base_away * math.exp(-diff_scale), 0.20, 3.30)

    # Realistic total goals (2.5-3.2 per match on average)
    target_total = _clamp(2.70 + abs(diff) / 1500.0, 2.30, 3.40)
    scale = target_total / max(0.1, home_xg + away_xg)
    home_xg *= scale
    away_xg *= scale

    # Poisson distribution
    p1 = px = p2 = 0.0
    max_goals = 10
    home_probs = [_poisson(home_xg, i) for i in range(max_goals + 1)]
    away_probs = [_poisson(away_xg, i) for i in range(max_goals + 1)]
    
    for hg, hp in enumerate(home_probs):
        for ag, ap in enumerate(away_probs):
            p = hp * ap
            if hg > ag:
                p1 += p
            elif hg == ag:
                px += p
            else:
                p2 += p

    total = p1 + px + p2
    if total <= 0:
        return 0.45, 0.28, 0.27
    p1, px, p2 = p1 / total, px / total, p2 / total

    # Realistic clamping (avoid absurd lines)
    p1 = _clamp(p1, 0.030, 0.90)
    px = _clamp(px, 0.08, 0.36)
    p2 = _clamp(p2, 0.030, 0.90)
    total = p1 + px + p2
    return p1 / total, px / total, p2 / total


def _hockey_probs(bot, home_rating: int, away_rating: int, home_team: str, away_team: str, sport_key: str, match_time: datetime | None = None) -> tuple[float, None, float]:
    """
    Hockey (NHL) probability calculation.
    """
    home_adv = 45  # NHL home advantage
    
    home_form = _advanced_form_bonus(bot, sport_key, home_team)
    away_form = _advanced_form_bonus(bot, sport_key, away_team)
    
    h2h_home, h2h_away = _head_to_head_bonus(bot, sport_key, home_team, away_team)
    rest_home, rest_away = _rest_injury_factor(bot, sport_key, home_team, away_team, match_time)
    
    diff = (home_rating + home_adv + home_form + h2h_home + rest_home) - \
           (away_rating + away_form + h2h_away + rest_away)
    
    p1 = 1.0 / (1.0 + 10 ** (-diff / 420.0))
    p1 = _clamp(p1, 0.18, 0.82)
    return p1, None, 1.0 - p1


def _margin(bot) -> float:
    """
    Dynamic margin: 3-6% based on competition level.
    Closer matches have higher margin (more uncertainty).
    """
    raw = os.getenv("AI_LINE_MARGIN")
    if raw is None:
        raw = str(getattr(bot, "AI_MARGIN", 0.04))
    try:
        return _clamp(float(raw), 0.03, 0.06)
    except ValueError:
        return 0.04


def probs_to_odds(bot, p1: float, px: float | None, p2: float) -> tuple[float, float | None, float]:
    """
    Convert probabilities to decimal odds with bookmaker margin.
    """
    margin = _margin(bot)

    def odd(prob: float) -> float:
        if prob <= 0:
            return 99.0
        odds_val = 1.0 / (prob * (1.0 + margin))
        return round(_clamp(odds_val, 1.01, 99.0), 2)

    if px is None:
        return odd(p1), None, odd(p2)
    return odd(p1), odd(px), odd(p2)


def ai_probs_1x2(bot, match: dict) -> tuple[float, float | None, float]:
    """
    Calculate 1X2 match probabilities using all advanced factors.
    """
    sport_key = _sport_key(bot, match)
    league = str(match.get("league") or "")
    teams = _parse_title_teams(bot, str(match.get("title") or ""))
    
    if not teams:
        if sport_key == "nhl":
            return 0.52, None, 0.48
        return 0.45, 0.28, 0.27

    home, away = teams
    home_rating = _team_rating(bot, sport_key, home, league)
    away_rating = _team_rating(bot, sport_key, away, league)
    
    # Parse match time
    match_time = None
    try:
        time_str = match.get("start_time_utc") or match.get("start_time") or ""
        if time_str:
            match_time = datetime.fromisoformat(time_str.replace("Z", "+00:00"))
    except Exception:
        pass

    if sport_key == "nhl":
        return _hockey_probs(bot, home_rating, away_rating, home, away, sport_key, match_time)
    return _football_probs(bot, home_rating, away_rating, league, home, away, sport_key, match_time)


def ai_odds_for_match(bot, match: dict) -> dict:
    """
    Calculate final odds for a match.
    """
    p1, px, p2 = ai_probs_1x2(bot, match)
    o1, ox, o2 = probs_to_odds(bot, p1, px, p2)
    out = dict(match)
    teams = _parse_title_teams(bot, str(match.get("title") or ""))
    if teams:
        out["home_team"] = teams[0]
        out["away_team"] = teams[1]
    out["prob_1"] = round(p1, 4)
    out["prob_x"] = None if px is None else round(px, 4)
    out["prob_2"] = round(p2, 4)
    out["odds_1"] = o1
    out["odds_x"] = ox
    out["odds_2"] = o2
    out["odds_source"] = VERSION
    try:
        out["odds_updated_at"] = bot.iso(bot.now_utc())
    except Exception:
        out["odds_updated_at"] = datetime.now(timezone.utc).isoformat()
    return out


def match_odds_for_pick(bot, match: dict, pick: str) -> float | None:
    """
    Get odds for specific pick (1, X, 2).
    """
    priced = ai_odds_for_match(bot, dict(match))
    if pick == "1":
        return priced.get("odds_1")
    if pick == "X":
        return priced.get("odds_x")
    if pick == "2":
        return priced.get("odds_2")
    return None


def _has_column(con, table: str, column: str) -> bool:
    try:
        rows = con.execute(f"PRAGMA table_info({table})").fetchall()
        return any(row[1] == column for row in rows)
    except Exception:
        return False


def refresh_ai_odds_for_open_matches(bot) -> int:
    """
    Refresh AI odds for all open matches.
    """
    updated = 0
    cutoff_fn = getattr(bot, "_today_msk_start_utc", None)
    if callable(cutoff_fn):
        cutoff = bot.iso(cutoff_fn())
    else:
        cutoff = bot.iso(bot.now_utc())

    with bot.db() as con:
        cur = con.cursor()
        rows = cur.execute(
            """
            SELECT *
            FROM matches
            WHERE status='open'
              AND COALESCE(start_time_utc, start_time) >= ?
            ORDER BY COALESCE(start_time_utc, start_time) ASC
            """,
            (cutoff,),
        ).fetchall()

        has_meta = _has_column(con, "matches", "odds_updated_at") and _has_column(con, "matches", "odds_source")
        for row in rows:
            try:
                priced = ai_odds_for_match(bot, dict(row))
                if has_meta:
                    cur.execute(
                        """
                        UPDATE matches
                        SET odds_1=?, odds_x=?, odds_2=?, odds_updated_at=?, odds_source=?
                        WHERE id=?
                        """,
                        (
                            priced.get("odds_1"),
                            priced.get("odds_x"),
                            priced.get("odds_2"),
                            priced.get("odds_updated_at"),
                            VERSION,
                            int(row["id"]),
                        ),
                    )
                else:
                    cur.execute(
                        "UPDATE matches SET odds_1=?, odds_x=?, odds_2=? WHERE id=?",
                        (priced.get("odds_1"), priced.get("odds_x"), priced.get("odds_2"), int(row["id"])),
                    )
                updated += 1
            except Exception:
                logger = getattr(bot, "logger", None)
                if logger:
                    logger.exception("%s refresh failed for match id=%s", VERSION, row["id"])
        con.commit()
    return updated


def _register_debug_command(bot) -> None:
    """
    Register /line_debug command for admins.
    """
    if getattr(bot, "_AI_LINE_DEBUG_REGISTERED", False):
        return
    Command = getattr(bot, "Command", None)
    Message = getattr(bot, "Message", None)
    if Command is None or Message is None:
        return

    @bot.dp.message(Command("line_debug"))
    async def line_debug_cmd(m: Message):
        if getattr(bot, "ADMIN_ID", 0) and int(m.from_user.id) != int(bot.ADMIN_ID):
            return await m.answer("Недостаточно прав.")

        raw = (m.text or "").split(maxsplit=1)
        match_id = 0
        if len(raw) > 1 and raw[1].strip().isdigit():
            match_id = int(raw[1].strip())
        if not match_id:
            with bot.db() as con:
                row = con.execute(
                    """
                    SELECT id FROM matches
                    WHERE status='open'
                    ORDER BY COALESCE(start_time_utc, start_time) ASC
                    LIMIT 1
                    """
                ).fetchone()
            match_id = int(row["id"]) if row else 0
        if not match_id:
            return await m.answer("Нет открытых матчей для проверки линии.")

        match = bot.get_match(match_id)
        if not match:
            return await m.answer("Матч не найден.")
        
        priced = ai_odds_for_match(bot, dict(match))
        teams = _parse_title_teams(bot, priced.get("title") or "") or ("—", "—")
        sport_key = _sport_key(bot, priced)
        home_rating = _team_rating(bot, sport_key, teams[0], priced.get("league") or "") if teams[0] != "—" else 0
        away_rating = _team_rating(bot, sport_key, teams[1], priced.get("league") or "") if teams[1] != "—" else 0

        text = (
            f"<b>{VERSION}</b>\n"
            f"Матч #{match_id}: <b>{priced.get('title')}</b>\n"
            f"Рейтинг: {teams[0]} <b>{home_rating}</b> — {teams[1]} <b>{away_rating}</b>\n"
            f"Вероятности: П1 <b>{priced.get('prob_1'):.1%}</b>"
        )
        if priced.get("prob_x") is not None:
            text += f" · X <b>{priced.get('prob_x'):.1%}</b>"
        text += f" · П2 <b>{priced.get('prob_2'):.1%}</b>\n"
        text += f"КФ: П1 <b>{priced.get('odds_1')}</b>"
        if priced.get("odds_x") is not None:
            text += f" · X <b>{priced.get('odds_x')}</b>"
        text += f" · П2 <b>{priced.get('odds_2')}</b>"
        await m.answer(text)

    bot._AI_LINE_DEBUG_REGISTERED = True


def apply(bot) -> None:
    """
    Apply AI odds module to bot.
    """
    if getattr(bot, "_AI_LINE_APPLIED", False):
        return

    bot.ai_probs_1x2 = lambda match: ai_probs_1x2(bot, match)
    bot.probs_to_odds = lambda p1, px, p2: probs_to_odds(bot, p1, px, p2)
    bot.ai_odds_for_match = lambda match: ai_odds_for_match(bot, match)
    bot.match_odds_for_pick = lambda match, pick: match_odds_for_pick(bot, match, pick)
    bot.refresh_ai_odds_for_open_matches = lambda: refresh_ai_odds_for_open_matches(bot)
    _register_debug_command(bot)
    bot._AI_LINE_APPLIED = True
    print(f"{VERSION}_APPLIED")
