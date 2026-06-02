from __future__ import annotations

import asyncio
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any

VERSION = "MARKET_ODDS_V1"
AI_SOURCE_PREFIXES = ("AI", "AI_LINE", "MARKET_FALLBACK")
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


def _market_source(source: Any) -> str:
    return str(source or "").strip()


def _is_market_source(source: Any) -> bool:
    raw = _market_source(source)
    if not raw:
        return False
    upper = raw.upper()
    return not any(upper.startswith(prefix) for prefix in AI_SOURCE_PREFIXES)


def _pick_value(data: dict[str, Any], pick: str) -> Any:
    if pick == "1":
        return data.get("odds_1")
    if pick == "X":
        return data.get("odds_x")
    if pick == "2":
        return data.get("odds_2")
    return None


def _set_pick_value(data: dict[str, Any], pick: str, value: Any) -> None:
    if pick == "1":
        data["odds_1"] = value
    elif pick == "X":
        data["odds_x"] = value
    elif pick == "2":
        data["odds_2"] = value


def _market_from_match(match: Any) -> dict[str, Any] | None:
    source = _row_value(match, "odds_source", "")
    if not _is_market_source(source):
        return None
    odds = {
        "1": _float_or_none(_row_value(match, "odds_1")),
        "X": _float_or_none(_row_value(match, "odds_x")),
        "2": _float_or_none(_row_value(match, "odds_2")),
    }
    if not any(odds.values()):
        return None
    return {
        "source": _market_source(source),
        "updated_at": _row_value(match, "odds_updated_at"),
        "odds": odds,
    }


def _edge(prob: Any, odd: Any) -> float | None:
    try:
        probability = float(prob)
        price = float(odd)
    except (TypeError, ValueError):
        return None
    if probability <= 0 or price <= 1:
        return None
    return round(probability * price - 1.0, 4)


def _best_value(edge: dict[str, float | None]) -> dict[str, Any] | None:
    items = [(pick, value) for pick, value in edge.items() if value is not None]
    if not items:
        return None
    pick, value = max(items, key=lambda item: item[1])
    return {"pick": pick, "edge": value, "positive": value > 0}


def _merge_priced_match(app: Any, original_ai: Any, match: Any) -> dict[str, Any]:
    try:
        ai = dict(original_ai(dict(match)))
    except Exception:
        logger = _log(app)
        if logger:
            logger.exception("market odds: AI pricing failed")
        ai = dict(match or {})

    out = dict(ai)
    out["ai_odds_1"] = _float_or_none(ai.get("odds_1"))
    out["ai_odds_x"] = _float_or_none(ai.get("odds_x"))
    out["ai_odds_2"] = _float_or_none(ai.get("odds_2"))
    out["ai_prob_1"] = ai.get("prob_1")
    out["ai_prob_x"] = ai.get("prob_x")
    out["ai_prob_2"] = ai.get("prob_2")

    market = _market_from_match(match)
    if market:
        odds = market["odds"]
        for pick in PICKS:
            _set_pick_value(out, pick, odds.get(pick))
        out["line_source"] = "market"
        out["market_source"] = market["source"]
        out["market_updated_at"] = market.get("updated_at")
        out["odds_source"] = market["source"]
        out["odds_updated_at"] = market.get("updated_at") or out.get("odds_updated_at")
    else:
        out["line_source"] = "ai"
        out["market_source"] = ""
        out["market_updated_at"] = None
        out["odds_source"] = out.get("odds_source") or "AI_LINE"

    edge = {
        "1": _edge(out.get("prob_1"), out.get("odds_1")) if market else None,
        "X": _edge(out.get("prob_x"), out.get("odds_x")) if market else None,
        "2": _edge(out.get("prob_2"), out.get("odds_2")) if market else None,
    }
    out["edge_1"] = edge["1"]
    out["edge_x"] = edge["X"]
    out["edge_2"] = edge["2"]
    out["best_value"] = _best_value(edge)
    return out


def _norm_team(value: Any) -> str:
    text = str(value or "").casefold().strip()
    text = text.replace("&", " and ")
    text = re.sub(r"\b(fc|cf|afc|sc|club|team|women|wta|atp)\b", " ", text)
    text = re.sub(r"[^a-z0-9а-яё\s.-]", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _tokens(value: Any) -> set[str]:
    return {token for token in re.split(r"\s+", _norm_team(value)) if len(token) >= 2}


def _team_score(left: str, right: str) -> int:
    left_norm = _norm_team(left)
    right_norm = _norm_team(right)
    if not left_norm or not right_norm:
        return 0
    if left_norm == right_norm:
        return 8
    if left_norm in right_norm or right_norm in left_norm:
        return 5
    return len(_tokens(left_norm) & _tokens(right_norm))


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


def _bookmaker_choice(app: Any, bookmakers: list[dict[str, Any]]) -> tuple[str, dict[str, Any]] | None:
    if not bookmakers:
        return None
    preferred = [item.strip() for item in os.getenv("ODDS_PREFERRED_BOOKS", "pinnacle,bet365,williamhill").split(",") if item.strip()]
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
    best_price = None
    best_score = 0
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


def _sport_key_allowed(sport: dict[str, Any]) -> bool:
    key = str(sport.get("key") or "").lower()
    group = str(sport.get("group") or "").lower()
    title = str(sport.get("title") or "").lower()
    text = f"{key} {group} {title}"
    return any(token in text for token in ("soccer", "football", "icehockey", "nhl", "tennis"))


def _configured_sport_keys(app: Any, sports: list[dict[str, Any]]) -> list[str]:
    explicit = [item.strip() for item in os.getenv("ODDS_SPORT_KEYS", "").split(",") if item.strip()]
    if explicit:
        return explicit
    keys = [str(sport.get("key")) for sport in sports if sport.get("active") and sport.get("key") and _sport_key_allowed(sport)]
    max_keys = int(os.getenv("ODDS_MAX_SPORT_KEYS", "16") or "16")
    return keys[: max(1, max_keys)]


async def refresh_market_odds_once(app: Any) -> int:
    provider = os.getenv("ODDS_PROVIDER", "theoddsapi").strip().lower()
    api_key = (os.getenv("ODDS_API_KEY") or getattr(app, "ODDS_API_KEY", "") or "").strip()
    if provider not in {"theoddsapi", "the_odds_api", "market"} or not api_key:
        return 0

    aiohttp = getattr(app, "aiohttp", None)
    if aiohttp is None:
        return 0

    now = app.now_utc()
    horizon = now + timedelta(hours=int(os.getenv("ODDS_LOOKAHEAD_HOURS", str(getattr(app, "ODDS_LOOKAHEAD_HOURS", 72))) or "72"))
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

    candidates = []
    for row in rows:
        teams = _parse_title_teams(app, _row_value(row, "title", ""))
        starts_at = _parse_dt(_row_value(row, "start_time_utc") or _row_value(row, "start_time"))
        if teams and starts_at:
            candidates.append({"id": int(_row_value(row, "id")), "home": teams[0], "away": teams[1], "starts_at": starts_at})
    if not candidates:
        return 0

    updated = 0
    base_url = (os.getenv("ODDS_BASE_URL") or getattr(app, "ODDS_BASE_URL", "https://api.the-odds-api.com") or "https://api.the-odds-api.com").rstrip("/")
    regions = os.getenv("ODDS_REGIONS", getattr(app, "ODDS_REGIONS", "eu"))
    markets = os.getenv("ODDS_MARKETS", getattr(app, "ODDS_MARKETS", "h2h"))
    odds_format = os.getenv("ODDS_ODDS_FORMAT", getattr(app, "ODDS_ODDS_FORMAT", "decimal"))
    date_format = os.getenv("ODDS_DATE_FORMAT", getattr(app, "ODDS_DATE_FORMAT", "iso"))
    time_window_hours = float(os.getenv("ODDS_MATCH_TIME_WINDOW_HOURS", "18") or "18")

    async with aiohttp.ClientSession() as session:
        timeout = aiohttp.ClientTimeout(total=25)
        async with session.get(f"{base_url}/v4/sports", params={"apiKey": api_key, "all": "false"}, timeout=timeout) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise RuntimeError(f"The Odds API sports HTTP {resp.status}: {text[:240]}")
            sports = await resp.json()

        for sport_key in _configured_sport_keys(app, sports or []):
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
                    if resp.status != 200:
                        text = await resp.text()
                        logger = _log(app)
                        if logger:
                            logger.warning("market odds fetch failed sport=%s status=%s err=%s", sport_key, resp.status, text[:180])
                        continue
                    events = await resp.json()
            except Exception as exc:
                logger = _log(app)
                if logger:
                    logger.warning("market odds fetch failed sport=%s err=%s", sport_key, exc)
                continue

            event_items = []
            for event in events or []:
                event_dt = _parse_dt(event.get("commence_time"))
                home = str(event.get("home_team") or "")
                away = str(event.get("away_team") or "")
                if event_dt and home and away:
                    event_items.append({"event": event, "home": home, "away": away, "starts_at": event_dt})

            with app.db() as con:
                cur = con.cursor()
                for candidate in candidates:
                    best_event = None
                    best_score = 0
                    for item in event_items:
                        delta_h = abs((item["starts_at"] - candidate["starts_at"]).total_seconds()) / 3600.0
                        if delta_h > time_window_hours:
                            continue
                        score, _ = _event_score(candidate["home"], candidate["away"], item["home"], item["away"])
                        if score > best_score:
                            best_score = score
                            best_event = item["event"]
                    if not best_event or best_score < 2:
                        continue

                    choice = _bookmaker_choice(app, list(best_event.get("bookmakers", []) or []))
                    if not choice:
                        continue
                    bookmaker_key, book = choice
                    outcomes = _h2h_outcomes(book)
                    if not outcomes:
                        continue
                    o1 = _price_for_team(candidate["home"], outcomes)
                    ox = _draw_price(outcomes)
                    o2 = _price_for_team(candidate["away"], outcomes)
                    if not o1 and not o2:
                        continue
                    cur.execute(
                        """
                        UPDATE matches
                        SET odds_1=?, odds_x=?, odds_2=?, odds_updated_at=?, odds_source=?
                        WHERE id=?
                        """,
                        (o1, ox, o2, app.iso(app.now_utc()), bookmaker_key, candidate["id"]),
                    )
                    updated += 1
                con.commit()

    return updated


def refresh_ai_fallback_for_open_matches(app: Any, original_ai: Any) -> int:
    updated = 0
    cutoff = app.iso(app._today_msk_start_utc()) if hasattr(app, "_today_msk_start_utc") else app.iso(app.now_utc())
    with app.db() as con:
        cur = con.cursor()
        rows = cur.execute(
            """
            SELECT * FROM matches
            WHERE status='open'
              AND COALESCE(start_time_utc, start_time) >= ?
            ORDER BY COALESCE(start_time_utc, start_time) ASC
            """,
            (cutoff,),
        ).fetchall()
        for row in rows:
            if _market_from_match(row):
                continue
            try:
                priced = dict(original_ai(dict(row)))
                cur.execute(
                    """
                    UPDATE matches
                    SET odds_1=?, odds_x=?, odds_2=?, odds_updated_at=?, odds_source='AI'
                    WHERE id=?
                    """,
                    (priced.get("odds_1"), priced.get("odds_x"), priced.get("odds_2"), app.iso(app.now_utc()), int(row["id"])),
                )
                updated += 1
            except Exception:
                logger = _log(app)
                if logger:
                    logger.exception("market odds: AI fallback refresh failed match id=%s", row["id"])
        con.commit()
    return updated


MARKET_CSS = r"""
    .market-strip {
      display:grid;
      grid-template-columns:auto 1fr;
      gap:8px;
      align-items:center;
      padding:8px 10px;
      border:1px solid rgba(244,196,48,.18);
      border-radius:8px;
      background:rgba(244,196,48,.06);
      color:var(--muted);
      font-size:11px;
    }
    .market-strip b { color:var(--accent2); font-size:12px; text-align:right; }
    .market-strip.market-ai { border-color:rgba(86,211,255,.16); background:rgba(86,211,255,.05); }
    .market-strip.market-ai b { color:var(--cyan); }
    @media (max-width: 430px) { .market-strip { grid-template-columns:1fr; } .market-strip b { text-align:left; } }
"""

MARKET_JS = r"""
function marketSourceLabel(line){
  if(line?.source === 'market') return `\u0420\u044b\u043d\u043e\u043a \u00b7 ${line.bookmaker || '\u0431\u0443\u043a\u043c\u0435\u043a\u0435\u0440'}`;
  return '\u0420\u044b\u043d\u043e\u043a \u043d\u0435\u0434\u043e\u0441\u0442\u0443\u043f\u0435\u043d \u00b7 AI fallback';
}
function marketEdgeLabel(m){
  const best = m?.best_value;
  if(!best || best.edge == null) return m?.line?.source === 'market' ? '\u0431\u0435\u0437 value' : '\u0440\u0430\u0441\u0447\u0451\u0442 AI';
  const value = Math.round(Number(best.edge || 0) * 1000) / 10;
  if(value > 0) return `Value ${pickLabel(best.pick)} +${value}%`;
  return `Value ${pickLabel(best.pick)} ${value}%`;
}
function marketDecorateMatches(items){
  const cards = Array.from(document.querySelectorAll('#matches .match'));
  cards.forEach((card, index) => {
    const m = (items || [])[index] || {};
    if(card.querySelector('.market-strip')) return;
    const line = m.line || {};
    const cls = line.source === 'market' ? 'market-live' : 'market-ai';
    const html = `<div class="market-strip ${cls}"><span>${marketSourceLabel(line)}</span><b>${marketEdgeLabel(m)}</b></div>`;
    const aiStrip = card.querySelector('.ai-strip');
    if(aiStrip){
      const label = aiStrip.querySelector('span');
      if(label) label.textContent = 'AI-\u0430\u043d\u0430\u043b\u0438\u0437';
      aiStrip.insertAdjacentHTML('beforebegin', html);
    }else{
      card.insertAdjacentHTML('beforeend', html);
    }
  });
}
if(typeof renderMatches === 'function' && !window.__marketOddsRenderMatchesWrapped){
  const marketPreviousRenderMatches = renderMatches;
  renderMatches = function(items){ marketPreviousRenderMatches(items); marketDecorateMatches(items || []); };
  window.__marketOddsRenderMatchesWrapped = true;
}
"""


def _patch_mini_app(app: Any) -> None:
    try:
        import mini_app
    except Exception as exc:
        logger = _log(app)
        if logger:
            logger.exception("market odds: mini_app import failed: %s", exc)
        return

    original_row_to_match = getattr(mini_app, "_row_to_match", None)
    if callable(original_row_to_match) and not getattr(original_row_to_match, "_market_odds_wrapped", False):
        def patched_row_to_match(bot: Any, row: Any, user_id: int | None = None) -> dict[str, Any]:
            item = dict(original_row_to_match(bot, row, user_id) or {})
            priced = bot.ai_odds_for_match(dict(row))
            source = str(priced.get("line_source") or "ai")
            item["odds"] = {"1": priced.get("odds_1"), "X": priced.get("odds_x"), "2": priced.get("odds_2")}
            item["probabilities"] = {"1": priced.get("prob_1"), "X": priced.get("prob_x"), "2": priced.get("prob_2")}
            item["ai_odds"] = {"1": priced.get("ai_odds_1"), "X": priced.get("ai_odds_x"), "2": priced.get("ai_odds_2")}
            item["edge"] = {"1": priced.get("edge_1"), "X": priced.get("edge_x"), "2": priced.get("edge_2")}
            item["best_value"] = priced.get("best_value")
            item["line"] = {
                "source": source,
                "bookmaker": priced.get("market_source") or "",
                "updated_at": priced.get("market_updated_at") or priced.get("odds_updated_at"),
                "label": "market" if source == "market" else "ai_fallback",
            }
            return item

        patched_row_to_match._market_odds_wrapped = True
        mini_app._row_to_match = patched_row_to_match

    original_summary = getattr(mini_app, "_summary", None)
    if callable(original_summary) and not getattr(original_summary, "_market_odds_wrapped", False):
        def patched_summary(bot: Any) -> dict[str, Any]:
            data = dict(original_summary(bot) or {})
            data["market_odds"] = {
                "enabled": bool((os.getenv("ODDS_API_KEY") or getattr(bot, "ODDS_API_KEY", "") or "").strip()),
                "provider": os.getenv("ODDS_PROVIDER", "theoddsapi"),
                "regions": os.getenv("ODDS_REGIONS", getattr(bot, "ODDS_REGIONS", "eu")),
            }
            return data

        patched_summary._market_odds_wrapped = True
        mini_app._summary = patched_summary

    original_index = getattr(mini_app, "_index_html", None)
    if callable(original_index) and not getattr(original_index, "_market_odds_wrapped", False):
        def patched_index_html() -> str:
            html = original_index()
            if ".market-strip" not in html:
                html = html.replace("</style>", MARKET_CSS + "\n  </style>", 1)
            if "marketDecorateMatches" not in html:
                html = html.replace("document.getElementById('refresh').onclick = load;", MARKET_JS + "\ndocument.getElementById('refresh').onclick = load;", 1)
            return html

        patched_index_html._market_odds_wrapped = True
        mini_app._index_html = patched_index_html


def _patch_runtime(app: Any) -> None:
    original_ai = getattr(app, "ai_odds_for_match", None)
    if not callable(original_ai):
        return

    if not getattr(app, "_MARKET_ODDS_AI_WRAPPED", False):
        app._MARKET_ODDS_ORIGINAL_AI = original_ai
        app.ai_odds_for_match = lambda match: _merge_priced_match(app, app._MARKET_ODDS_ORIGINAL_AI, match)
        app.match_odds_for_pick = lambda match, pick: _pick_value(app.ai_odds_for_match(dict(match)), str(pick).upper())
        app.refresh_ai_odds_for_open_matches = lambda: refresh_ai_fallback_for_open_matches(app, app._MARKET_ODDS_ORIGINAL_AI)
        app._MARKET_ODDS_AI_WRAPPED = True

    async def patched_refresh_odds_once() -> int:
        return await refresh_market_odds_once(app)

    app.refresh_odds_once = patched_refresh_odds_once

    async def patched_fixed_sync_once() -> str:
        archived = app.archive_past_matches() if hasattr(app, "archive_past_matches") else 0
        try:
            sync_msg = await app.autosync_once()
        except Exception as exc:
            logger = _log(app)
            if logger:
                logger.exception("market odds: autosync_once failed")
            sync_msg = f"sync error: {exc}"

        market_updated = 0
        try:
            market_updated = await refresh_market_odds_once(app)
        except Exception as exc:
            logger = _log(app)
            if logger:
                logger.exception("market odds refresh failed: %s", exc)

        ai_updated = refresh_ai_fallback_for_open_matches(app, app._MARKET_ODDS_ORIGINAL_AI)
        try:
            app.pick_featured_for_today()
        except Exception:
            logger = _log(app)
            if logger:
                logger.exception("market odds: pick_featured_for_today failed")
        return f"archived={archived}; {sync_msg}; market_odds={market_updated}; ai_fallback={ai_updated}"

    app.fixed_sync_once = patched_fixed_sync_once

    async def market_odds_now_cmd(m):
        if not m.from_user or (hasattr(app, "is_admin") and not app.is_admin(m.from_user.id)):
            return await m.answer("Недостаточно прав.")
        await m.answer("Обновляю рыночную линию...")
        try:
            market_updated = await refresh_market_odds_once(app)
            ai_updated = refresh_ai_fallback_for_open_matches(app, app._MARKET_ODDS_ORIGINAL_AI)
            await m.answer(f"Готово: рынок={market_updated}, AI fallback={ai_updated}")
        except Exception as exc:
            logger = _log(app)
            if logger:
                logger.exception("market_odds_now failed")
            await m.answer(f"Ошибка обновления линии: {exc}")

    if not getattr(app, "_MARKET_ODDS_COMMANDS_REGISTERED", False):
        app.dp.message(app.Command("market_odds_now"))(market_odds_now_cmd)
        app._MARKET_ODDS_COMMANDS_REGISTERED = True


def apply(app: Any) -> None:
    if getattr(app, "_MARKET_ODDS_APPLIED", False):
        return
    _patch_runtime(app)
    _patch_mini_app(app)
    app._MARKET_ODDS_APPLIED = True
    print(f"{VERSION}_APPLIED")
