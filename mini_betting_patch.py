from __future__ import annotations

import os
import re
import sqlite3
from typing import Any

VERSION = "MINI_APP_BETTING_V1"
PICKS = ("1", "X", "2")


def _env_int(name: str, default: int, minimum: int = 0, maximum: int = 10**9) -> int:
    try:
        value = int(os.getenv(name, str(default)) or default)
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _start_balance() -> int:
    return _env_int("MINI_APP_START_BALANCE", _env_int("START_BALANCE", 1000), 0, 10**9)


def _max_stake() -> int:
    return _env_int("MINI_APP_MAX_STAKE", 100000, 1, 10**9)


def _default_stake() -> int:
    presets = _stake_presets()
    return _env_int("MINI_APP_DEFAULT_STAKE", presets[0] if presets else 100, 1, _max_stake())


def _stake_presets() -> list[int]:
    raw = os.getenv("MINI_APP_STAKE_PRESETS", "50,100,200,500,1000")
    values: list[int] = []
    for part in raw.split(","):
        try:
            value = int(part.strip())
        except (TypeError, ValueError):
            continue
        if value > 0 and value <= _max_stake() and value not in values:
            values.append(value)
    return values or [50, 100, 200, 500, 1000]


def _betting_enabled(bot: Any | None = None) -> bool:
    if os.getenv("MINI_APP_BETTING_ENABLED") is not None:
        return os.getenv("MINI_APP_BETTING_ENABLED", "1") == "1"
    return bool(getattr(bot, "BETTING_ENABLED", True)) if bot is not None else True


def _row_value(row: Any, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    if isinstance(row, dict):
        return row.get(key, default)
    try:
        return row[key]
    except Exception:
        return default


def _now(bot: Any) -> str:
    return bot.iso(bot.now_utc())


def _score_exists(bot: Any, user_id: int) -> bool:
    try:
        with bot.db() as con:
            row = con.execute("SELECT 1 FROM scores WHERE user_id=?", (user_id,)).fetchone()
            return bool(row)
    except sqlite3.OperationalError:
        try:
            bot.init_db()
        except Exception:
            pass
        return False


def _grant_start_balance(bot: Any, user_id: int, force_if_missing: bool = False) -> None:
    start_balance = _start_balance()
    if start_balance <= 0:
        return
    try:
        bot.init_db()
    except Exception:
        pass

    with bot.db() as con:
        cur = con.cursor()
        now = _now(bot)
        row = cur.execute(
            "SELECT balance, total, correct FROM scores WHERE user_id=?",
            (user_id,),
        ).fetchone()
        if not row:
            cur.execute(
                """
                INSERT OR IGNORE INTO scores(user_id, points, balance, correct, total, streak, best_streak, updated_at)
                VALUES(?, 0, ?, 0, 0, 0, 0, ?)
                """,
                (user_id, start_balance, now),
            )
            con.commit()
            return

        balance = int(_row_value(row, "balance", 0) or 0)
        total = int(_row_value(row, "total", 0) or 0)
        if balance > 0 or total > 0:
            return

        votes = cur.execute("SELECT COUNT(*) AS c FROM votes WHERE user_id=?", (user_id,)).fetchone()
        vote_count = int(_row_value(votes, "c", 0) or 0)
        if force_if_missing or vote_count == 0:
            cur.execute(
                "UPDATE scores SET balance=?, updated_at=? WHERE user_id=?",
                (start_balance, now, user_id),
            )
            con.commit()


def _my_vote(bot: Any, user_id: int, match_id: int):
    try:
        with bot.db() as con:
            return con.execute(
                "SELECT pick, stake, odds FROM votes WHERE user_id=? AND match_id=?",
                (user_id, match_id),
            ).fetchone()
    except sqlite3.OperationalError:
        try:
            bot.init_db()
        except Exception:
            pass
        with bot.db() as con:
            return con.execute(
                "SELECT pick, stake, odds FROM votes WHERE user_id=? AND match_id=?",
                (user_id, match_id),
            ).fetchone()


def _parse_pick_and_stake(raw_pick: str) -> tuple[str, int | None]:
    value = (raw_pick or "").strip().upper()
    for sep in (":", "|", "@"):
        if sep in value:
            pick, stake_s = value.split(sep, 1)
            pick = pick.strip().upper()
            try:
                stake = int(stake_s.strip())
            except ValueError:
                stake = None
            return pick, stake
    match = re.match(r"^([12X])\s+(\d+)$", value)
    if match:
        return match.group(1), int(match.group(2))
    if value in PICKS:
        return value, _default_stake()
    return value, None


def _odds_for_pick(bot: Any, match: Any, pick: str, priced: dict[str, Any] | None = None) -> float | None:
    priced = priced or {}
    key = {"1": "odds_1", "X": "odds_x", "2": "odds_2"}.get(pick)
    if not key:
        return None
    value = priced.get(key)
    if value is None:
        value = _row_value(match, key)
    if value is None and hasattr(bot, "match_odds_for_pick"):
        try:
            value = bot.match_odds_for_pick(dict(match), pick)
        except Exception:
            value = None
    try:
        odds = float(value)
    except (TypeError, ValueError):
        return None
    return odds if odds > 1 else None


def _patch_upsert_user(mini_app: Any) -> None:
    original = getattr(mini_app, "_upsert_user", None)
    if not callable(original) or getattr(original, "_mini_betting_wrapped", False):
        return

    def patched_upsert_user(bot: Any, user: dict[str, Any]) -> int:
        user_id = int(user["id"])
        existed = _score_exists(bot, user_id)
        saved_id = int(original(bot, user))
        _grant_start_balance(bot, saved_id, force_if_missing=not existed)
        return saved_id

    patched_upsert_user._mini_betting_wrapped = True
    mini_app._upsert_user = patched_upsert_user


def _patch_profile(mini_app: Any) -> None:
    original = getattr(mini_app, "_profile", None)
    if not callable(original) or getattr(original, "_mini_betting_wrapped", False):
        return

    def patched_profile(bot: Any, user_id: int) -> dict[str, Any]:
        _grant_start_balance(bot, int(user_id))
        profile = dict(original(bot, user_id) or {})
        profile["betting_enabled"] = _betting_enabled(bot)
        profile["stake_presets"] = _stake_presets()
        profile["max_stake"] = _max_stake()
        return profile

    patched_profile._mini_betting_wrapped = True
    mini_app._profile = patched_profile


def _patch_row_to_match(mini_app: Any) -> None:
    original = getattr(mini_app, "_row_to_match", None)
    if not callable(original) or getattr(original, "_mini_betting_wrapped", False):
        return

    def patched_row_to_match(bot: Any, row: Any, user_id: int | None = None) -> dict[str, Any]:
        payload = dict(original(bot, row, user_id) or {})
        payload["betting_enabled"] = _betting_enabled(bot)
        payload.setdefault("my_stake", 0)
        payload.setdefault("my_odds", None)
        payload.setdefault("my_payout", 0)
        if user_id:
            vote = _my_vote(bot, int(user_id), int(_row_value(row, "id", 0) or 0))
            if vote:
                stake = int(_row_value(vote, "stake", 0) or 0)
                odds_raw = _row_value(vote, "odds")
                try:
                    odds = float(odds_raw) if odds_raw is not None else None
                except (TypeError, ValueError):
                    odds = None
                payload["my_pick"] = _row_value(vote, "pick") or payload.get("my_pick")
                payload["my_stake"] = stake
                payload["my_odds"] = odds
                payload["my_payout"] = int(round(stake * odds)) if stake > 0 and odds else 0
        return payload

    patched_row_to_match._mini_betting_wrapped = True
    mini_app._row_to_match = patched_row_to_match


def _patch_place_prediction(mini_app: Any) -> None:
    original = getattr(mini_app, "_place_prediction", None)
    if not callable(original) or getattr(original, "_mini_betting_wrapped", False):
        return

    def patched_place_prediction(bot: Any, user_id: int, match_id: int, pick: str):
        clean_pick, stake = _parse_pick_and_stake(pick)
        if not _betting_enabled(bot):
            return original(bot, user_id, match_id, clean_pick)

        if clean_pick not in PICKS:
            return False, "Неверный исход.", None
        if stake is None:
            return False, "Выбери сумму ставки.", None
        stake = int(stake)
        if stake <= 0:
            return False, "Сумма ставки должна быть больше 0.", None
        if stake > _max_stake():
            return False, f"Максимальная ставка: {_max_stake()}.", None

        try:
            bot.init_db()
        except Exception:
            pass
        _grant_start_balance(bot, int(user_id))

        match = bot.get_match(match_id)
        if not match:
            return False, "Матч не найден.", None
        ok, why = bot.can_predict(match) if hasattr(bot, "can_predict") else (True, "")
        if not ok:
            return False, why or "Ставки закрыты.", None

        try:
            priced = bot.ai_odds_for_match(dict(match))
        except Exception:
            priced = {}
        if clean_pick not in mini_app._available_picks(match, priced):
            return False, "Этот исход недоступен для матча.", None

        odds = _odds_for_pick(bot, match, clean_pick, priced)
        if odds is None:
            return False, "Коэффициент для этого исхода недоступен.", None
        odds = round(float(odds), 2)
        payout = int(round(stake * odds))

        now = _now(bot)
        with bot.db() as con:
            cur = con.cursor()
            cur.execute("BEGIN IMMEDIATE")
            cur.execute(
                """
                INSERT OR IGNORE INTO scores(user_id, points, balance, correct, total, streak, best_streak, updated_at)
                VALUES(?, 0, ?, 0, 0, 0, 0, ?)
                """,
                (user_id, _start_balance(), now),
            )
            score = cur.execute("SELECT balance FROM scores WHERE user_id=?", (user_id,)).fetchone()
            balance = int(_row_value(score, "balance", 0) or 0)
            old_vote = cur.execute(
                "SELECT stake FROM votes WHERE user_id=? AND match_id=?",
                (user_id, match_id),
            ).fetchone()
            old_stake = int(_row_value(old_vote, "stake", 0) or 0) if old_vote else 0
            available_balance = balance + old_stake
            if available_balance < stake:
                con.rollback()
                return False, f"Недостаточно баланса. Доступно: {available_balance}.", None

            new_balance = available_balance - stake
            cur.execute(
                "UPDATE scores SET balance=?, updated_at=? WHERE user_id=?",
                (new_balance, now, user_id),
            )
            cur.execute(
                """
                INSERT OR REPLACE INTO votes(user_id, match_id, pick, created_at, stake, odds)
                VALUES(?,?,?,?,?,?)
                """,
                (user_id, match_id, clean_pick, now, stake, odds),
            )
            con.commit()

        action = "Ставка обновлена" if old_stake else "Ставка принята"
        message = f"{action}. Списано {stake}. Баланс: {new_balance}. Возможная выплата: {payout}."
        return True, message, mini_app._row_to_match(bot, match, user_id)

    patched_place_prediction._mini_betting_wrapped = True
    mini_app._place_prediction = patched_place_prediction


def _replace_block(html: str, start_marker: str, end_marker: str, replacement: str) -> str:
    start = html.find(start_marker)
    if start < 0:
        return html
    end = html.find(end_marker, start)
    if end < 0:
        return html
    return html[:start] + replacement + html[end:]


def _patch_html(html: str) -> str:
    if "betModal" in html:
        return html

    presets = _stake_presets()
    css = """
    .bet-modal { position:fixed; inset:0; display:none; align-items:end; justify-content:center; padding:14px; background:rgba(0,0,0,.58); z-index:20; }
    .bet-modal.active { display:flex; }
    .bet-card { width:min(520px, 100%); border:1px solid var(--line); border-radius:8px; background:var(--panel); box-shadow:0 22px 60px rgba(0,0,0,.48); overflow:hidden; }
    .bet-body { padding:14px; display:grid; gap:10px; }
    .bet-head { display:flex; justify-content:space-between; gap:10px; align-items:start; padding:14px; border-bottom:1px solid var(--line); }
    .bet-title { font-weight:760; line-height:1.25; }
    .bet-sub { color:var(--muted); font-size:12px; margin-top:4px; }
    .stake-presets { display:grid; grid-template-columns:repeat(5, minmax(0,1fr)); gap:6px; }
    .stake-presets button { padding:8px 4px; }
    .bet-actions { display:grid; grid-template-columns:1fr 1fr; gap:8px; padding:0 14px 14px; }
    .bet-line { display:flex; justify-content:space-between; gap:10px; color:var(--muted); font-size:13px; }
    @media (max-width: 520px) { .stake-presets { grid-template-columns:repeat(3, minmax(0,1fr)); } }
"""
    html = html.replace("    .toast { position:fixed;", css + "    .toast { position:fixed;", 1)

    modal = """
  <div class="bet-modal" id="betModal" aria-hidden="true">
    <div class="bet-card">
      <div class="bet-head">
        <div>
          <div class="bet-title" id="betTitle">Матч</div>
          <div class="bet-sub" id="betSub">Исход</div>
        </div>
        <button id="betClose" type="button">Закрыть</button>
      </div>
      <div class="bet-body">
        <div class="bet-line"><span>Баланс</span><b id="betBalance">0</b></div>
        <div class="bet-line"><span>КФ</span><b id="betOdds">0.00</b></div>
        <div class="stake-presets" id="stakePresets"></div>
        <input id="betStake" inputmode="numeric" pattern="[0-9]*" placeholder="Сумма ставки" />
        <div class="bet-line"><span>Возможная выплата</span><b id="betPayout">0</b></div>
      </div>
      <div class="bet-actions">
        <button id="betCancel" type="button">Отмена</button>
        <button id="betConfirm" class="good" type="button">Поставить</button>
      </div>
    </div>
  </div>
"""
    html = html.replace('  <div class="toast" id="toast"></div>', modal + '  <div class="toast" id="toast"></div>', 1)

    html = html.replace(
        "let searchTimer = null;",
        f"let searchTimer = null;\nlet lastMatches = [];\nconst defaultStakePresets = {presets};\nlet profileState = {{balance:0, stake_presets:defaultStakePresets, max_stake:100000}};\nlet pendingBet = null;\nconst fmtMoney = v => Number(v || 0).toLocaleString('ru-RU');",
        1,
    )

    helpers = """
function stakeList(){ return Array.isArray(profileState.stake_presets) && profileState.stake_presets.length ? profileState.stake_presets : defaultStakePresets; }
function closeBetModal(){ const modal=document.getElementById('betModal'); if(modal) modal.classList.remove('active'); pendingBet=null; }
function setStakeValue(value){ const input=document.getElementById('betStake'); if(input) input.value=String(value || ''); updateBetPayout(); }
function updateBetPayout(){
  if(!pendingBet) return;
  const input=document.getElementById('betStake');
  const stake=Math.max(0, parseInt(input?.value || '0', 10) || 0);
  const payout=Math.round(stake * Number(pendingBet.odds || 0));
  const maxAvailable=Number(pendingBet.available || 0);
  const confirm=document.getElementById('betConfirm');
  document.getElementById('betPayout').textContent=fmtMoney(payout);
  if(confirm) confirm.disabled = stake <= 0 || stake > maxAvailable || stake > Number(profileState.max_stake || 100000);
}
function renderStakePresets(){
  const el=document.getElementById('stakePresets');
  if(!el || !pendingBet) return;
  const maxAvailable=Number(pendingBet.available || 0);
  el.innerHTML=stakeList().map(v=>`<button type="button" data-stake="${v}" ${v>maxAvailable?'disabled':''}>${fmtMoney(v)}</button>`).join('');
  el.querySelectorAll('button[data-stake]').forEach(b=>b.onclick=()=>setStakeValue(Number(b.dataset.stake)));
}
function openBetModal(matchId, pick){
  if(!initData){ enforceTelegramLaunch(); return; }
  const match=(lastMatches || []).find(x=>Number(x.id)===Number(matchId));
  if(!match){ toast('Матч не найден'); return; }
  const odds=Number(match.odds?.[pick] || 0);
  if(!odds){ toast('Коэффициент недоступен'); return; }
  const oldStake=Number(match.my_stake || 0);
  const available=Number(profileState.balance || 0) + oldStake;
  pendingBet={matchId:Number(matchId), pick, odds, available};
  document.getElementById('betTitle').textContent=match.title || 'Матч';
  document.getElementById('betSub').textContent=`${pickLabel(pick)} · КФ ${fmtOdd(odds)}`;
  document.getElementById('betBalance').textContent=fmtMoney(available);
  document.getElementById('betOdds').textContent=fmtOdd(odds);
  renderStakePresets();
  const first=stakeList().find(v=>v<=available) || Math.min(available, stakeList()[0] || 50);
  setStakeValue(oldStake || first || '');
  document.getElementById('betModal').classList.add('active');
  document.getElementById('betStake')?.focus();
}
async function confirmBet(){
  if(!pendingBet) return;
  const stake=parseInt(document.getElementById('betStake')?.value || '0', 10) || 0;
  await sendPick(pendingBet.matchId, pendingBet.pick, stake);
}
"""
    html = html.replace("function sportLabel(s){", helpers + "function sportLabel(s){", 1)

    html = html.replace("function renderMatches(items){", "function renderMatches(items){\n  lastMatches = items || [];", 1)
    html = html.replace(
        "return `<button class=\"${cls}\" data-pick=\"${p}\" data-id=\"${m.id}\" ${disabled?'disabled':''}>${pickLabel(p)}</button>`;",
        "return `<button class=\"${cls}\" data-pick=\"${p}\" data-id=\"${m.id}\" ${disabled?'disabled':''}>${pickLabel(p)} @${fmtOdd(m.odds[p])}</button>`;",
        1,
    )
    html = html.replace(
        "<div class=\"meta\">${m.my_pick ? `Твой прогноз: ${pickLabel(m.my_pick)}` : (m.can_predict ? 'Прогноз открыт' : (m.blocked_reason || 'Прогноз закрыт'))}</div>",
        "<div class=\"meta\">${m.my_pick ? `Твоя ставка: ${pickLabel(m.my_pick)} · ${fmtMoney(m.my_stake)} @${fmtOdd(m.my_odds)} · выплата ${fmtMoney(m.my_payout)}` : (m.can_predict ? 'Ставки открыты' : (m.blocked_reason || 'Ставки закрыты'))}</div>",
        1,
    )
    html = html.replace(
        "el.querySelectorAll('button[data-pick]').forEach(b=>b.onclick=()=>sendPick(Number(b.dataset.id), b.dataset.pick));",
        "el.querySelectorAll('button[data-pick]').forEach(b=>b.onclick=()=>openBetModal(Number(b.dataset.id), b.dataset.pick));",
        1,
    )
    html = html.replace(
        "function renderProfile(p){\n  const el = document.getElementById('profileStats');",
        "function renderProfile(p){\n  profileState = Object.assign(profileState, p || {});\n  if(Array.isArray(p?.stake_presets)) profileState.stake_presets = p.stake_presets;\n  const el = document.getElementById('profileStats');",
        1,
    )
    html = html.replace(
        "<div class=\"row\"><span>${x.title}<br><span class=\"muted\">${x.league || '—'} · ${x.status}</span></span><b>${pickLabel(x.pick)} ${x.result ? `→ ${x.result}` : ''}</b></div>",
        "<div class=\"row\"><span>${x.title}<br><span class=\"muted\">${x.league || '—'} · ${x.status}</span></span><b>${pickLabel(x.pick)}${x.stake ? ` · ${fmtMoney(x.stake)} @${fmtOdd(x.odds)}` : ''} ${x.result ? `→ ${x.result}` : ''}</b></div>",
        1,
    )

    send_pick = """async function sendPick(matchId, pick, stake){
  if(!initData){ enforceTelegramLaunch(); return; }
  if(!stake || stake <= 0){ toast('Введи сумму ставки'); return; }
  try{
    tg?.HapticFeedback?.impactOccurred('light');
    const data = await api('/api/predict', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({match_id:matchId, pick:`${pick}:${stake}`, stake, initData})});
    closeBetModal();
    toast(data.message || 'Ставка принята');
    await Promise.all([loadMatches(), loadMine(), loadProfile()]);
  }catch(e){ const msg=e.message || 'Ошибка ставки'; tg?.showAlert?.(msg); toast(msg); }
}
"""
    html = _replace_block(html, "async function sendPick(matchId, pick){", "async function loadSummary", send_pick)

    hooks = """
document.getElementById('betClose')?.addEventListener('click', closeBetModal);
document.getElementById('betCancel')?.addEventListener('click', closeBetModal);
document.getElementById('betConfirm')?.addEventListener('click', confirmBet);
document.getElementById('betStake')?.addEventListener('input', updateBetPayout);
"""
    html = html.replace("document.getElementById('refresh').onclick = load;", hooks + "document.getElementById('refresh').onclick = load;", 1)
    return html


def _patch_index_html(mini_app: Any) -> None:
    original = getattr(mini_app, "_index_html", None)
    if not callable(original) or getattr(original, "_mini_betting_wrapped", False):
        return

    def patched_index_html() -> str:
        return _patch_html(original())

    patched_index_html._mini_betting_wrapped = True
    mini_app._index_html = patched_index_html


def apply(bot: Any) -> None:
    if getattr(bot, "_MINI_APP_BETTING_APPLIED", False):
        return

    try:
        import mini_app
    except Exception as exc:
        logger = getattr(bot, "logger", None)
        if logger:
            logger.exception("mini betting import failed: %s", exc)
        return

    _patch_upsert_user(mini_app)
    _patch_profile(mini_app)
    _patch_row_to_match(mini_app)
    _patch_place_prediction(mini_app)
    _patch_index_html(mini_app)

    bot._MINI_APP_BETTING_APPLIED = True
    print(f"{VERSION}_APPLIED")
