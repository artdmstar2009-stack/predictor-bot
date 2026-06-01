from __future__ import annotations

import math
from typing import Any

VERSION = "AI_LINE_BACKTEST_V2"


def _prob_map(priced: dict[str, Any]) -> dict[str, float]:
    out = {
        "1": float(priced.get("prob_1") or 0.0),
        "2": float(priced.get("prob_2") or 0.0),
    }
    if priced.get("prob_x") is not None:
        out["X"] = float(priced.get("prob_x") or 0.0)
    total = sum(v for v in out.values() if v > 0)
    if total > 0:
        out = {k: v / total for k, v in out.items()}
    return out


def _odds_for_pick(priced: dict[str, Any], pick: str) -> float | None:
    key = {"1": "odds_1", "X": "odds_x", "2": "odds_2"}.get(pick)
    if not key or priced.get(key) is None:
        return None
    try:
        return float(priced[key])
    except Exception:
        return None


def _bucket(confidence: float) -> str:
    pct = int(confidence * 100)
    low = max(0, min(90, (pct // 10) * 10))
    high = low + 10
    if low < 40:
        return "<40%"
    if low >= 90:
        return "90%+"
    return f"{low}-{high}%"


def _empty_result(limit: int) -> dict[str, Any]:
    return {
        "version": VERSION,
        "limit": limit,
        "matches": 0,
        "accuracy": 0.0,
        "avg_confidence": 0.0,
        "brier": None,
        "log_loss": None,
        "virtual_roi": None,
        "by_sport": {},
        "calibration": [],
        "recent": [],
    }


def run_backtest(bot, limit: int = 500) -> dict[str, Any]:
    limit = max(10, min(int(limit or 500), 5000))
    with bot.db() as con:
        rows = con.execute(
            """
            SELECT *
            FROM matches
            WHERE result IN ('1', 'X', '2')
            ORDER BY COALESCE(start_time_utc, start_time, created_at) DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    if not rows:
        return _empty_result(limit)

    total = 0
    correct = 0
    conf_sum = 0.0
    brier_sum = 0.0
    log_sum = 0.0
    stake_sum = 0.0
    profit_sum = 0.0
    by_sport: dict[str, dict[str, Any]] = {}
    buckets: dict[str, dict[str, Any]] = {}
    recent: list[dict[str, Any]] = []

    for row in rows:
        actual = (row["result"] or "").strip().upper()
        if actual not in ("1", "X", "2"):
            continue
        try:
            match = dict(row)
            priced = bot.ai_odds_for_match(match)
            probs = _prob_map(priced)
        except Exception:
            logger = getattr(bot, "logger", None)
            if logger:
                logger.exception("backtest pricing failed for match id=%s", row["id"])
            continue

        if actual not in probs or not probs:
            continue

        pick = max(probs, key=probs.get)
        confidence = float(probs[pick])
        p_actual = max(1e-9, float(probs.get(actual, 0.0)))
        is_correct = pick == actual
        outcomes = sorted(probs.keys())
        brier = sum((float(probs[o]) - (1.0 if o == actual else 0.0)) ** 2 for o in outcomes)
        log_loss = -math.log(p_actual)

        total += 1
        correct += int(is_correct)
        conf_sum += confidence
        brier_sum += brier
        log_sum += log_loss

        odds = _odds_for_pick(priced, pick)
        if odds:
            stake_sum += 1.0
            profit_sum += (odds - 1.0) if is_correct else -1.0

        sport = (row["sport"] or "other").lower()
        sport_stat = by_sport.setdefault(sport, {"matches": 0, "correct": 0, "confidence": 0.0})
        sport_stat["matches"] += 1
        sport_stat["correct"] += int(is_correct)
        sport_stat["confidence"] += confidence

        bucket_key = _bucket(confidence)
        bucket_stat = buckets.setdefault(bucket_key, {"bucket": bucket_key, "matches": 0, "correct": 0, "confidence": 0.0})
        bucket_stat["matches"] += 1
        bucket_stat["correct"] += int(is_correct)
        bucket_stat["confidence"] += confidence

        if len(recent) < 20:
            recent.append({
                "id": int(row["id"]),
                "title": row["title"],
                "sport": sport,
                "league": row["league"],
                "start_time": row["start_time_utc"] or row["start_time"],
                "actual": actual,
                "pick": pick,
                "confidence": round(confidence, 4),
                "correct": is_correct,
                "odds": odds,
            })

    if total == 0:
        return _empty_result(limit)

    for stat in by_sport.values():
        stat["accuracy"] = round(stat["correct"] / stat["matches"], 4)
        stat["avg_confidence"] = round(stat["confidence"] / stat["matches"], 4)
        del stat["confidence"]

    calibration = []
    for key in sorted(buckets.keys()):
        stat = buckets[key]
        calibration.append({
            "bucket": key,
            "matches": stat["matches"],
            "accuracy": round(stat["correct"] / stat["matches"], 4),
            "avg_confidence": round(stat["confidence"] / stat["matches"], 4),
        })

    return {
        "version": VERSION,
        "limit": limit,
        "matches": total,
        "accuracy": round(correct / total, 4),
        "avg_confidence": round(conf_sum / total, 4),
        "brier": round(brier_sum / total, 4),
        "log_loss": round(log_sum / total, 4),
        "virtual_roi": None if stake_sum <= 0 else round(profit_sum / stake_sum, 4),
        "by_sport": by_sport,
        "calibration": calibration,
        "recent": recent,
    }


def format_backtest(result: dict[str, Any]) -> str:
    if result.get("matches", 0) <= 0:
        return "<b>Backtest AI-line</b>\nNo closed matches with results yet."

    roi = result.get("virtual_roi")
    roi_text = "-" if roi is None else f"{roi:+.1%}"
    lines = [
        "<b>Backtest AI-line</b>",
        f"Matches: <b>{result['matches']}</b>",
        f"Top-pick accuracy: <b>{result['accuracy']:.1%}</b>",
        f"Average confidence: <b>{result['avg_confidence']:.1%}</b>",
        f"Brier: <b>{result['brier']:.3f}</b> · LogLoss: <b>{result['log_loss']:.3f}</b>",
        f"Virtual ROI by internal line: <b>{roi_text}</b>",
    ]

    if result.get("by_sport"):
        lines.append("\n<b>By sport</b>")
        for sport, stat in sorted(result["by_sport"].items()):
            lines.append(f"{sport}: {stat['accuracy']:.1%} · {stat['matches']} matches")

    if result.get("calibration"):
        lines.append("\n<b>Calibration</b>")
        for stat in result["calibration"][:8]:
            lines.append(
                f"{stat['bucket']}: actual {stat['accuracy']:.1%}, expected {stat['avg_confidence']:.1%}, n={stat['matches']}"
            )

    return "\n".join(lines)


def _register_command(bot) -> None:
    if getattr(bot, "_AI_LINE_BACKTEST_REGISTERED", False):
        return
    Command = getattr(bot, "Command", None)
    Message = getattr(bot, "Message", None)
    if Command is None or Message is None:
        return

    @bot.dp.message(Command("backtest"))
    async def backtest_cmd(m: Message):
        if getattr(bot, "ADMIN_ID", 0) and int(m.from_user.id) != int(bot.ADMIN_ID):
            return await m.answer("Недостаточно прав.")
        raw = (m.text or "").split(maxsplit=1)
        limit = 500
        if len(raw) > 1 and raw[1].strip().isdigit():
            limit = int(raw[1].strip())
        result = run_backtest(bot, limit)
        await m.answer(format_backtest(result))

    bot._AI_LINE_BACKTEST_REGISTERED = True


def _apply_mini_betting(bot) -> None:
    if getattr(bot, "_MINI_APP_BETTING_APPLIED", False):
        return
    try:
        import mini_betting_patch

        mini_betting_patch.apply(bot)
    except Exception as exc:
        logger = getattr(bot, "logger", None)
        if logger:
            logger.exception("mini betting apply failed from line_backtest: %s", exc)


def apply(bot) -> None:
    if getattr(bot, "_AI_LINE_BACKTEST_APPLIED", False):
        _apply_mini_betting(bot)
        return
    bot.run_ai_line_backtest = lambda limit=500: run_backtest(bot, limit)
    bot.format_ai_line_backtest = format_backtest
    _register_command(bot)
    bot._AI_LINE_BACKTEST_APPLIED = True
    _apply_mini_betting(bot)
    print(f"{VERSION}_APPLIED")
