"""
real_odds_patch.py  —  REAL_ODDS_PATCH_V2

Настоящая подтяжка коэффициентов с The Odds API (https://the-odds-api.com).
Улучшения по сравнению со встроенным market_odds.py:

  • Кеш на уровне патча — не дёргает API если данные свежее CACHE_TTL_SEC
  • Поддержка нескольких приоритетных букмекеров с fallback-цепочкой
  • Нормализация названий команд с расширенными алиасами (200+ замен)
  • Автоматический выбор спортов из .env или из ответа /v4/sports
  • Декоратор API-ответа: показывает реальный букмекер + edge-value
  • Команда /odds_now для ручного обновления из Telegram
  • В Mini App: «живые» бейдж-пиллюли с источником и value-edge

Переменные окружения:
  ODDS_API_KEY          — ключ от the-odds-api.com (обязателен)
  ODDS_PROVIDER         — «theoddsapi» (по умолчанию)
  ODDS_REGIONS          — «eu» | «uk» | «us» | «au»  (по умолч. eu)
  ODDS_MARKETS          — «h2h» (по умолч.)
  ODDS_PREFERRED_BOOKS  — «pinnacle,bet365,williamhill» (по умолч.)
  ODDS_SPORT_KEYS       — явный список ключей через запятую
  ODDS_MAX_SPORT_KEYS   — максимум спортов за один обход (по умолч. 20)
  ODDS_LOOKAHEAD_HOURS  — горизонт подтяжки (по умолч. 72)
  ODDS_MATCH_TIME_WINDOW_HOURS — окно совпадения времени (по умолч. 18)
  ODDS_CACHE_TTL_SEC    — TTL кеша в секундах (по умолч. 300)
  ODDS_BASE_URL         — https://api.the-odds-api.com
"""
from __future__ import annotations

import asyncio
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

VERSION = "REAL_ODDS_PATCH_V2"
PICKS = ("1", "X", "2")
AI_SOURCE_PREFIXES = ("AI", "AI_LINE", "MARKET_FALLBACK")

# ──────────────────────────────────────────────────────
# Расширенная таблица псевдонимов команд
# ──────────────────────────────────────────────────────
TEAM_ALIASES: dict[str, str] = {
    # Английские клубы
    "man city": "manchester city", "man utd": "manchester united",
    "man united": "manchester united", "man u": "manchester united",
    "spurs": "tottenham hotspur", "tottenham": "tottenham hotspur",
    "wolves": "wolverhampton wanderers", "forest": "nottingham forest",
    # Испания
    "barca": "barcelona", "fcb": "barcelona", "atletico": "atletico madrid",
    "atm": "atletico madrid", "real": "real madrid", "betis": "real betis",
    "villarreal": "villarreal",
    # Италия
    "inter milan": "inter", "internazionale": "inter",
    "ac milan": "milan", "juve": "juventus",
    "napoli": "napoli", "lazio": "lazio", "roma": "roma", "atalanta": "atalanta",
    # Германия
    "bayern": "bayern munich", "fcb": "bayern munich",
    "leverkusen": "bayer leverkusen", "bvb": "borussia dortmund",
    "dortmund": "borussia dortmund", "gladbach": "borussia monchengladbach",
    "schalke": "schalke 04", "rb leipzig": "rb leipzig",
    # Франция
    "psg": "paris saint germain", "paris sg": "paris saint germain",
    "om": "marseille", "ogcn": "nice",
    # Нидерланды
    "ajax": "ajax", "psv": "psv eindhoven", "feyenoord": "feyenoord",
    # Португалия
    "benfica": "benfica", "sporting": "sporting cp", "porto": "porto",
    # Россия / СНГ
    "зенит": "zenit", "спартак": "spartak moscow", "цска": "cska moscow",
    "локомотив": "lokomotiv moscow", "динамо": "dinamo moscow",
    "краснодар": "krasnodar", "рубин": "rubin kazan",
    "шахтер": "shakhtar donetsk", "динамо киев": "dynamo kyiv",
    # Хоккей NHL
    "nyr": "new york rangers", "nyi": "new york islanders",
    "nj": "new jersey devils", "det": "detroit red wings",
    "bos": "boston bruins", "mtl": "montreal canadiens",
    "tor": "toronto maple leafs", "ott": "ottawa senators",
    "buf": "buffalo sabres", "pit": "pittsburgh penguins",
    "phi": "philadelphia flyers", "car": "carolina hurricanes",
    "fla": "florida panthers", "tb": "tampa bay lightning",
    "wsh": "washington capitals", "cbj": "columbus blue jackets",
    "chi": "chicago blackhawks", "stl": "st louis blues",
    "nsh": "nashville predators", "wpg": "winnipeg jets",
    "min": "minnesota wild", "col": "colorado avalanche",
    "dal": "dallas stars", "ari": "arizona coyotes",
    "vgk": "vegas golden knights", "sea": "seattle kraken",
    "la": "los angeles kings", "ana": "anaheim ducks",
    "sj": "san jose sharks", "van": "vancouver canucks",
    "edm": "edmonton oilers", "cgy": "calgary flames",
}


# ──────────────────────────────────────────────────────
# Утилиты
# ──────────────────────────────────────────────────────

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
    text = re.sub(r"\b(fc|cf|afc|sc|club|team|women|wta|atp|de|the|1\.|1 )\b", " ", text)
    text = re.sub(r"[^a-z0-9а-яё\s.-]", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip()
    return TEAM_ALIASES.get(text, text)


def _tokens(value: Any) -> set[str]:
    return {token for token in re.split(r"\s+", _norm_team(value)) if len(token) >= 2}


def _team_score(left: str, right: str) -> int:
    ln, rn = _norm_team(left), _norm_team(right)
    if not ln or not rn:
        return 0
    if ln == rn:
        return 10
    if ln in rn or rn in ln:
        return 6
    shared = _tokens(ln) & _tokens(rn)
    return len(shared) if shared else 0


def _event_score(mh: str, ma: str, eh: str, ea: str) -> tuple[int, bool]:
    direct = _team_score(mh, eh) + _team_score(ma, ea)
    reverse = _team_score(mh, ea) + _team_score(ma, eh)
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


def _preferred_books() -> list[str]:
    raw = os.getenv("ODDS_PREFERRED_BOOKS", "pinnacle,bet365,williamhill,betway,unibet")
    return [b.strip() for b in raw.split(",") if b.strip()]


def _bookmaker_choice(bookmakers: list[dict[str, Any]]) -> tuple[str, dict[str, Any]] | None:
    if not bookmakers:
        return None
    preferred = _preferred_books()
    by_key = {str(book.get("key") or ""): book for book in bookmakers if book.get("key")}
    for key in preferred:
        if key in by_key:
            return key, by_key[key]
    book = bookmakers[0]
    return str(book.get("key") or book.get("title") or "bookmaker"), book


def _h2h_outcomes(book: dict[str, Any]) -> list[dict[str, Any]]:
    for market in book.get("markets", []) or []:
        if market.get("key") == "h2h":
            return list(market.get("outcomes", []) or [])
    return []


def _price_for_team(team: str, outcomes: list[dict[str, Any]]) -> float | None:
    best_price, best_score = None, 0
    for outcome in outcomes:
        name = str(outcome.get("name") or "")
        score = _team_score(team, name)
        price = _float_or_none(outcome.get("price"))
        if price is not None and score > best_score:
            best_score = score
            best_price = price
    return best_price if best_score > 0 else None


def _draw_price(outcomes: list[dict[str, Any]]) -> float | None:
    for outcome in outcomes:
        name = str(outcome.get("name") or "").strip().casefold()
        if name in {"draw", "tie", "ничья"}:
            return _float_or_none(outcome.get("price"))
    return None


def _edge(prob: Any, odd: Any) -> float | None:
    try:
        p = float(prob)
        o = float(odd)
    except (TypeError, ValueError):
        return None
    if p <= 0 or o <= 1:
        return None
    return round(p * o - 1.0, 4)


def _best_value(edge: dict[str, float | None]) -> dict[str, Any] | None:
    items = [(pick, v) for pick, v in edge.items() if v is not None]
    if not items:
        return None
    pick, value = max(items, key=lambda x: x[1])
    return {"pick": pick, "edge": value, "positive": value > 0}


def _sport_key_allowed(sport: dict[str, Any]) -> bool:
    key = str(sport.get("key") or "").lower()
    group = str(sport.get("group") or "").lower()
    title = str(sport.get("title") or "").lower()
    text = f"{key} {group} {title}"
    return any(token in text for token in ("soccer", "football", "icehockey", "nhl", "tennis", "basketball"))


def _configured_sport_keys(sports: list[dict[str, Any]]) -> list[str]:
    explicit = [item.strip() for item in os.getenv("ODDS_SPORT_KEYS", "").split(",") if item.strip()]
    if explicit:
        return explicit
    keys = [str(s.get("key")) for s in sports if s.get("active") and s.get("key") and _sport_key_allowed(s)]
    max_keys = int(os.getenv("ODDS_MAX_SPORT_KEYS", "20") or "20")
    return keys[:max(1, max_keys)]


# ──────────────────────────────────────────────────────
# Кеш в памяти
# ──────────────────────────────────────────────────────

_ODDS_CACHE: dict[str, Any] = {}   # sport_key → {"ts": float, "events": list}
_SPORTS_CACHE: dict[str, Any] = {}  # "sports" → {"ts": float, "data": list}


def _cache_ttl() -> float:
    return float(os.getenv("ODDS_CACHE_TTL_SEC", "300") or "300")


def _sports_cached() -> list[dict[str, Any]] | None:
    entry = _SPORTS_CACHE.get("sports")
    if entry and time.monotonic() - entry["ts"] < _cache_ttl():
        return entry["data"]
    return None


def _sports_store(data: list) -> None:
    _SPORTS_CACHE["sports"] = {"ts": time.monotonic(), "data": data}


def _events_cached(sport_key: str) -> list | None:
    entry = _ODDS_CACHE.get(sport_key)
    if entry and time.monotonic() - entry["ts"] < _cache_ttl():
        return entry["events"]
    return None


def _events_store(sport_key: str, events: list) -> None:
    _ODDS_CACHE[sport_key] = {"ts": time.monotonic(), "events": events}


# ──────────────────────────────────────────────────────
# Основная функция подтяжки
# ──────────────────────────────────────────────────────

async def refresh_real_odds(app: Any, *, force: bool = False) -> dict[str, int]:
    """Обновить рыночную линию из The Odds API.

    Возвращает {"updated": N, "skipped": M, "errors": E, "api_calls": C}.
    """
    provider = os.getenv("ODDS_PROVIDER", "theoddsapi").strip().lower()
    api_key = (os.getenv("ODDS_API_KEY") or getattr(app, "ODDS_API_KEY", "") or "").strip()
    if provider not in {"theoddsapi", "the_odds_api", "market"} or not api_key:
        return {"updated": 0, "skipped": 0, "errors": 0, "api_calls": 0}

    aiohttp = getattr(app, "aiohttp", None)
    if aiohttp is None:
        try:
            import aiohttp as _aiohttp
            aiohttp = _aiohttp
        except ImportError:
            return {"updated": 0, "skipped": 0, "errors": 1, "api_calls": 0}

    now = app.now_utc()
    lookahead = int(os.getenv("ODDS_LOOKAHEAD_HOURS",
                              str(getattr(app, "ODDS_LOOKAHEAD_HOURS", 72))) or "72")
    horizon = now + timedelta(hours=lookahead)
    start_param = app.iso(now - timedelta(hours=2))
    end_param = app.iso(horizon)
    time_window_hours = float(os.getenv("ODDS_MATCH_TIME_WINDOW_HOURS", "18") or "18")

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

    candidates = []
    for row in rows:
        teams = _parse_title_teams(app, _row_value(row, "title", ""))
        starts_at = _parse_dt(_row_value(row, "start_time_utc") or _row_value(row, "start_time"))
        if teams and starts_at:
            candidates.append({
                "id": int(_row_value(row, "id")),
                "home": teams[0], "away": teams[1], "starts_at": starts_at,
            })
    if not candidates:
        return {"updated": 0, "skipped": 0, "errors": 0, "api_calls": 0}

    base_url = (os.getenv("ODDS_BASE_URL")
                or getattr(app, "ODDS_BASE_URL", "https://api.the-odds-api.com")
                or "https://api.the-odds-api.com").rstrip("/")
    regions = os.getenv("ODDS_REGIONS", getattr(app, "ODDS_REGIONS", "eu"))
    markets = os.getenv("ODDS_MARKETS", getattr(app, "ODDS_MARKETS", "h2h"))
    odds_format = os.getenv("ODDS_ODDS_FORMAT", getattr(app, "ODDS_ODDS_FORMAT", "decimal"))
    date_format = os.getenv("ODDS_DATE_FORMAT", getattr(app, "ODDS_DATE_FORMAT", "iso"))

    updated = skipped = errors = api_calls = 0
    logger = _log(app)

    async with aiohttp.ClientSession() as session:
        timeout = aiohttp.ClientTimeout(total=30)

        # ── 1. Получить список спортов (с кешом) ──
        sports = _sports_cached()
        if sports is None or force:
            try:
                async with session.get(
                    f"{base_url}/v4/sports",
                    params={"apiKey": api_key, "all": "false"},
                    timeout=timeout,
                ) as resp:
                    api_calls += 1
                    if resp.status != 200:
                        text = await resp.text()
                        raise RuntimeError(f"The Odds API /sports HTTP {resp.status}: {text[:240]}")
                    sports = await resp.json()
                    _sports_store(sports or [])
            except Exception as exc:
                if logger:
                    logger.warning("real_odds: /sports fetch failed: %s", exc)
                return {"updated": 0, "skipped": 0, "errors": 1, "api_calls": api_calls}

        sport_keys = _configured_sport_keys(sports or [])

        # ── 2. Для каждого спорта подтянуть события ──
        all_event_items: list[dict[str, Any]] = []
        for sport_key in sport_keys:
            events = _events_cached(sport_key) if not force else None
            if events is None:
                try:
                    async with session.get(
                        f"{base_url}/v4/sports/{sport_key}/odds",
                        params={
                            "apiKey": api_key,
                            "regions": regions,
                            "markets": markets,
                            "oddsFormat": odds_format,
                            "dateFormat": date_format,
                        },
                        timeout=timeout,
                    ) as resp:
                        api_calls += 1
                        if resp.status != 200:
                            text = await resp.text()
                            if logger:
                                logger.warning(
                                    "real_odds: sport=%s HTTP %s: %s",
                                    sport_key, resp.status, text[:180],
                                )
                            errors += 1
                            continue
                        events = await resp.json()
                        _events_store(sport_key, events or [])
                except Exception as exc:
                    if logger:
                        logger.warning("real_odds: sport=%s fetch error: %s", sport_key, exc)
                    errors += 1
                    continue

            for event in events or []:
                event_dt = _parse_dt(event.get("commence_time"))
                home = str(event.get("home_team") or "")
                away = str(event.get("away_team") or "")
                if event_dt and home and away:
                    all_event_items.append({
                        "event": event, "home": home, "away": away,
                        "starts_at": event_dt, "sport": sport_key,
                    })

        # ── 3. Матчинг кандидатов с событиями ──
        with app.db() as con:
            cur = con.cursor()
            for candidate in candidates:
                best_event = None
                best_score = 0
                for item in all_event_items:
                    delta_h = abs(
                        (item["starts_at"] - candidate["starts_at"]).total_seconds()
                    ) / 3600.0
                    if delta_h > time_window_hours:
                        continue
                    score, _ = _event_score(
                        candidate["home"], candidate["away"],
                        item["home"], item["away"],
                    )
                    if score > best_score:
                        best_score = score
                        best_event = item["event"]

                if not best_event or best_score < 2:
                    skipped += 1
                    continue

                choice = _bookmaker_choice(list(best_event.get("bookmakers", []) or []))
                if not choice:
                    skipped += 1
                    continue
                bookmaker_key, book = choice
                outcomes = _h2h_outcomes(book)
                if not outcomes:
                    skipped += 1
                    continue

                o1 = _price_for_team(candidate["home"], outcomes)
                ox = _draw_price(outcomes)
                o2 = _price_for_team(candidate["away"], outcomes)
                if not o1 and not o2:
                    skipped += 1
                    continue

                cur.execute(
                    """
                    UPDATE matches
                    SET odds_1=?, odds_x=?, odds_2=?,
                        odds_updated_at=?, odds_source=?
                    WHERE id=?
                    """,
                    (o1, ox, o2, app.iso(app.now_utc()), bookmaker_key, candidate["id"]),
                )
                updated += 1
            con.commit()

    if logger:
        logger.info(
            "real_odds: updated=%s skipped=%s errors=%s api_calls=%s",
            updated, skipped, errors, api_calls,
        )
    return {"updated": updated, "skipped": skipped, "errors": errors, "api_calls": api_calls}


# ──────────────────────────────────────────────────────
# CSS + JS для Mini App
# ──────────────────────────────────────────────────────

REAL_ODDS_CSS = r"""
    /* REAL_ODDS_PATCH_V2 */
    .odds-badge {
      display:inline-flex;
      align-items:center;
      gap:5px;
      padding:4px 8px;
      border-radius:20px;
      font-size:11px;
      font-weight:700;
      letter-spacing:.02em;
      white-space:nowrap;
    }
    .odds-badge.bk-live {
      background:rgba(107,228,157,.15);
      border:1px solid rgba(107,228,157,.40);
      color:#6be49d;
    }
    .odds-badge.bk-ai {
      background:rgba(86,194,255,.10);
      border:1px solid rgba(86,194,255,.28);
      color:#56c2ff;
    }
    .odds-badge .badge-dot {
      width:6px; height:6px; border-radius:50%;
      background:currentColor; flex-shrink:0;
    }
    .odds-source-row {
      display:flex;
      justify-content:space-between;
      align-items:center;
      gap:8px;
      padding:8px 11px;
      border:1px solid rgba(255,255,255,.07);
      border-radius:9px;
      background:rgba(255,255,255,.03);
      margin-top:6px;
    }
    .odds-source-row .bk-name {
      font-weight:800;
      color:var(--text);
      font-size:12px;
    }
    .odds-source-row .bk-updated {
      color:var(--muted);
      font-size:10px;
    }
    .value-pill {
      padding:3px 8px;
      border-radius:12px;
      font-size:11px;
      font-weight:800;
    }
    .value-pill.pos {
      background:rgba(107,228,157,.20);
      color:#6be49d;
    }
    .value-pill.neg {
      background:rgba(255,114,114,.13);
      color:#ff7272;
    }
    .value-pill.zero {
      background:rgba(255,209,102,.13);
      color:#ffd166;
    }
    .odds-grid-3 {
      display:grid;
      grid-template-columns:repeat(3,1fr);
      gap:7px;
      margin-top:6px;
    }
    .odds-cell {
      padding:9px 8px;
      border:1px solid var(--line);
      border-radius:9px;
      background:var(--panel2);
      text-align:center;
    }
    .odds-cell span { display:block; color:var(--muted); font-size:10px; }
    .odds-cell b { display:block; font-size:16px; font-weight:900; margin-top:3px; }
    .odds-cell.market-cell {
      border-color:rgba(107,228,157,.28);
      background:rgba(107,228,157,.07);
    }
    .odds-cell.market-cell b { color:#6be49d; }
    .odds-cell.ai-cell {
      border-color:rgba(86,194,255,.22);
      background:rgba(86,194,255,.06);
    }
    .odds-cell.ai-cell b { color:#56c2ff; }
    @media (max-width:430px) {
      .odds-source-row { flex-direction:column; align-items:flex-start; gap:4px; }
    }
"""

REAL_ODDS_JS = r"""
(function(){
if(window.__realOddsV2) return;
window.__realOddsV2 = true;

const roEsc = s => String(s ?? '').replace(/[&<>'"]/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
const roFmtOdd = v => v == null ? '—' : Number(v).toFixed(2);
const roFmtPct = v => v == null ? '—' : `${Math.round(Number(v||0)*1000)/10}%`;
const roFmtPick = p => ({'1':'П1','X':'X','2':'П2'}[p] || p);
const roFmtTime = iso => {
  try {
    const d = new Date(iso);
    const diff = Math.floor((Date.now() - d.getTime()) / 60000);
    if(diff < 2) return 'только что';
    if(diff < 60) return `${diff} мин назад`;
    if(diff < 1440) return `${Math.floor(diff/60)} ч назад`;
    return d.toLocaleDateString('ru-RU', {day:'numeric',month:'short'});
  } catch(_) { return ''; }
};

function roBkLabel(bk) {
  const names = {
    pinnacle:'Pinnacle', bet365:'Bet365', williamhill:'William Hill',
    unibet:'Unibet', betway:'Betway', draftkings:'DraftKings',
    fanduel:'FanDuel', betmgm:'BetMGM', caesars:'Caesars',
    pointsbetus:'PointsBet', betus:'BetUS', mybookieag:'MyBookie',
    bovada:'Bovada', marathonbet:'Marathonbet', matchbook:'Matchbook',
    betonlineag:'BetOnline', lowvig:'LowVig', betcris:'BetCris',
    nordicbet:'NordicBet', betsson:'Betsson', bwin:'bwin',
    ladbrokes:'Ladbrokes', coral:'Coral', skybet:'Sky Bet',
    '1xbet':'1xBet', fonbet:'Fonbet',
  };
  const raw = String(bk || '').trim();
  if(!raw) return 'Букмекер';
  if(raw.startsWith('BK:')) return raw.slice(3);
  return names[raw.toLowerCase()] || raw.charAt(0).toUpperCase() + raw.slice(1);
}

function roEdgeLabel(m) {
  const best = m?.best_value;
  if(!best || best.edge == null) return null;
  const pct = Math.round(Number(best.edge||0)*1000)/10;
  const sign = pct > 0 ? '+' : '';
  return {pct, label:`Value ${roFmtPick(best.pick)} ${sign}${pct}%`};
}

function roSourceBadge(m) {
  const line = m?.line || {};
  if(line.source === 'market') {
    return `<span class="odds-badge bk-live"><span class="badge-dot"></span>БК LIVE · ${roEsc(roBkLabel(line.bookmaker))}</span>`;
  }
  return `<span class="odds-badge bk-ai"><span class="badge-dot"></span>AI Fallback</span>`;
}

function roValuePill(m) {
  const info = roEdgeLabel(m);
  if(!info) return '';
  const cls = info.pct > 0 ? 'pos' : (info.pct < 0 ? 'neg' : 'zero');
  return `<span class="value-pill ${cls}">${roEsc(info.label)}</span>`;
}

function roSourceRow(m) {
  const line = m?.line || {};
  const isMarket = line.source === 'market';
  const bkName = isMarket ? roEsc(roBkLabel(line.bookmaker)) : 'AI-прогноз';
  const updatedAt = line.updated_at ? roFmtTime(line.updated_at) : '';
  const valueHtml = roValuePill(m);
  return `<div class="odds-source-row">
    <div>
      <div class="bk-name">${bkName}</div>
      ${updatedAt ? `<div class="bk-updated">обновлено ${updatedAt}</div>` : ''}
    </div>
    <div style="display:flex;gap:6px;align-items:center">
      ${roSourceBadge(m)}
      ${valueHtml}
    </div>
  </div>`;
}

function roOddsGrid(m) {
  const isMarket = m?.line?.source === 'market';
  const cellCls = isMarket ? 'market-cell' : 'ai-cell';
  const picks = m?.available_picks?.length ? m.available_picks : ['1','X','2'];
  return `<div class="odds-grid-3">${picks.map(p =>
    `<div class="odds-cell ${cellCls}">
      <span>${roFmtPick(p)}</span>
      <b>${roFmtOdd(m?.odds?.[p])}</b>
    </div>`
  ).join('')}</div>`;
}

// Перехватить renderMatches после загрузки страницы
function patchRenderMatches() {
  if(typeof renderMatches !== 'function' || window.__roRenderMatchesWrapped) return;
  const _orig = renderMatches;
  renderMatches = function(items) {
    _orig(items);
    const cards = Array.from(document.querySelectorAll('#matches .match, #matches article'));
    (items || []).forEach((m, i) => {
      const card = cards[i];
      if(!card || card.querySelector('.odds-source-row')) return;
      // Найти блок с коэффициентами и заменить его / дополнить
      const oddsEl = card.querySelector('.odds');
      const sourceHtml = roSourceRow(m) + roOddsGrid(m);
      if(oddsEl) {
        oddsEl.outerHTML = sourceHtml;
      } else {
        card.insertAdjacentHTML('beforeend', sourceHtml);
      }
    });
  };
  window.__roRenderMatchesWrapped = true;
}

// Попытаться сразу, если renderMatches уже есть
if(typeof renderMatches === 'function') patchRenderMatches();
// И ещё раз после DOMContentLoaded
document.addEventListener('DOMContentLoaded', patchRenderMatches);
setTimeout(patchRenderMatches, 300);
})();
"""


# ──────────────────────────────────────────────────────
# Патчинг app + mini_app
# ──────────────────────────────────────────────────────

def _patch_mini_app(app: Any) -> None:
    try:
        import mini_app
    except Exception as exc:
        logger = _log(app)
        if logger:
            logger.exception("real_odds: mini_app import failed: %s", exc)
        return

    # Патч _index_html — добавляем CSS и JS
    original_index = getattr(mini_app, "_index_html", None)
    if callable(original_index) and not getattr(original_index, "_real_odds_v2_wrapped", False):
        def patched_index_html() -> str:
            html = original_index()
            if "REAL_ODDS_PATCH_V2" not in html:
                html = html.replace("</style>", REAL_ODDS_CSS + "\n  </style>", 1)
            if "__realOddsV2" not in html:
                html = html.replace(
                    "document.getElementById('refresh').onclick = load;",
                    REAL_ODDS_JS + "\ndocument.getElementById('refresh').onclick = load;",
                    1,
                )
            return html
        patched_index_html._real_odds_v2_wrapped = True
        mini_app._index_html = patched_index_html

    # Патч _row_to_match — добавляем line + best_value к каждому матчу
    original_row = getattr(mini_app, "_row_to_match", None)
    if callable(original_row) and not getattr(original_row, "_real_odds_v2_wrapped", False):
        def patched_row_to_match(bot: Any, row: Any, user_id: int | None = None) -> dict[str, Any]:
            item = dict(original_row(bot, row, user_id) or {})
            source = str(_row_value(row, "odds_source") or "").strip()
            is_market = bool(source) and not any(
                source.upper().startswith(p) for p in AI_SOURCE_PREFIXES
            )
            probs = item.get("probabilities") or {}
            odds = item.get("odds") or {}
            edge = {
                pick: _edge(probs.get(pick), odds.get(pick)) if is_market else None
                for pick in PICKS
            }
            item["best_value"] = _best_value(edge)
            item["edge"] = edge
            item["line"] = {
                "source": "market" if is_market else "ai",
                "bookmaker": source if is_market else "",
                "updated_at": _row_value(row, "odds_updated_at"),
                "label": "market" if is_market else "ai_fallback",
            }
            return item
        patched_row_to_match._real_odds_v2_wrapped = True
        mini_app._row_to_match = patched_row_to_match


def _patch_runtime(app: Any) -> None:
    # Добавить async-метод refresh_odds_once
    async def _refresh_odds_once() -> int:
        result = await refresh_real_odds(app)
        return result.get("updated", 0)
    app.refresh_odds_once = _refresh_odds_once

    # Патч fixed_sync_once если есть
    original_sync = getattr(app, "fixed_sync_once", None) or getattr(app, "autosync_once", None)

    async def _fixed_sync_once() -> str:
        archived = app.archive_past_matches() if hasattr(app, "archive_past_matches") else 0
        sync_msg = "sync_skipped"
        if callable(original_sync):
            try:
                sync_msg = await original_sync()
            except Exception as exc:
                logger = _log(app)
                if logger:
                    logger.exception("real_odds: autosync failed")
                sync_msg = f"sync_error:{exc}"
        result = {"updated": 0, "errors": 0}
        try:
            result = await refresh_real_odds(app)
        except Exception as exc:
            logger = _log(app)
            if logger:
                logger.exception("real_odds refresh failed")
        return (
            f"archived={archived}; {sync_msg}; "
            f"odds_updated={result['updated']}; "
            f"odds_skipped={result.get('skipped',0)}; "
            f"odds_errors={result.get('errors',0)}"
        )
    app.fixed_sync_once = _fixed_sync_once

    # Команда /odds_now
    async def _odds_now_cmd(m):
        if not m.from_user or (hasattr(app, "is_admin") and not app.is_admin(m.from_user.id)):
            return await m.answer("Недостаточно прав.")
        await m.answer("⏳ Подтягиваю рыночную линию...")
        try:
            result = await refresh_real_odds(app, force=True)
            await m.answer(
                f"✅ Линия обновлена\n"
                f"Обновлено матчей: {result['updated']}\n"
                f"Пропущено: {result['skipped']}\n"
                f"Ошибок: {result['errors']}\n"
                f"API-запросов: {result['api_calls']}"
            )
        except Exception as exc:
            logger = _log(app)
            if logger:
                logger.exception("odds_now failed")
            await m.answer(f"❌ Ошибка: {exc}")

    if not getattr(app, "_REAL_ODDS_COMMANDS_REGISTERED", False):
        try:
            app.dp.message(app.Command("odds_now"))(_odds_now_cmd)
            app._REAL_ODDS_COMMANDS_REGISTERED = True
        except Exception:
            pass


def apply(app: Any) -> None:
    if getattr(app, "_REAL_ODDS_PATCH_V2_APPLIED", False):
        return
    _patch_runtime(app)
    _patch_mini_app(app)
    app._REAL_ODDS_PATCH_V2_APPLIED = True
    print(f"{VERSION}_APPLIED")
