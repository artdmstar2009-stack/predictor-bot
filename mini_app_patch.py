from __future__ import annotations

import os
from typing import Any

VERSION = "MINI_APP_PATCH_V5"

BEAUTY_CSS = r"""
    /* MINI_APP_BEAUTY_V1 */
    :root {
      --bg:#080b0a;
      --panel:#111615;
      --panel2:#171d1b;
      --panel3:#1d2522;
      --text:#f4f0e8;
      --muted:#9aa6a0;
      --line:#2a342f;
      --accent:#6ee7b7;
      --accent2:#f4c430;
      --cyan:#56d3ff;
      --bad:#ff6b6b;
      --shadow:0 18px 44px rgba(0,0,0,.36);
    }
    html { min-height:100%; background:var(--bg); }
    body {
      min-height:100%;
      background:
        linear-gradient(180deg, rgba(110,231,183,.09), rgba(8,11,10,0) 220px),
        linear-gradient(135deg, rgba(244,196,48,.055), rgba(86,211,255,.035) 48%, rgba(8,11,10,0) 76%),
        var(--bg);
      color:var(--text);
      letter-spacing:0;
    }
    .shell { width:min(1040px,100%); padding:12px; }
    header {
      position:sticky;
      top:0;
      z-index:12;
      display:flex;
      justify-content:space-between;
      gap:10px;
      align-items:center;
      margin:-12px -12px 10px;
      padding:12px;
      border-bottom:1px solid rgba(255,255,255,.07);
      background:rgba(8,11,10,.86);
      backdrop-filter:blur(16px);
    }
    .brand { display:flex; align-items:center; gap:10px; min-width:0; }
    .brand-mark {
      width:38px;
      height:38px;
      display:grid;
      place-items:center;
      border:1px solid rgba(110,231,183,.38);
      border-radius:8px;
      background:linear-gradient(135deg, rgba(110,231,183,.26), rgba(244,196,48,.14));
      color:var(--text);
      font-weight:900;
      box-shadow:0 10px 28px rgba(110,231,183,.12);
    }
    h1 { font-size:18px; line-height:1.05; }
    .sub { max-width:220px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-size:11px; color:var(--muted); }
    .top-actions { display:flex; align-items:center; gap:8px; }
    .balance-chip {
      min-height:38px;
      display:flex;
      flex-direction:column;
      justify-content:center;
      padding:7px 10px;
      border:1px solid rgba(244,196,48,.32);
      border-radius:8px;
      background:rgba(244,196,48,.08);
      white-space:nowrap;
    }
    .balance-chip span { color:var(--muted); font-size:10px; line-height:1; }
    .balance-chip b { color:var(--accent2); font-size:14px; line-height:1.18; }
    #refresh {
      width:38px;
      height:38px;
      display:grid;
      place-items:center;
      padding:0;
      border-color:rgba(110,231,183,.28);
      background:rgba(110,231,183,.1);
      font-size:19px;
    }
    .tabs {
      position:sticky;
      top:63px;
      z-index:11;
      margin:0 -12px 12px;
      padding:8px 12px;
      gap:6px;
      border-bottom:1px solid rgba(255,255,255,.06);
      background:rgba(8,11,10,.82);
      backdrop-filter:blur(16px);
      scrollbar-width:none;
    }
    .tabs::-webkit-scrollbar, .sports::-webkit-scrollbar { display:none; }
    button {
      border-color:rgba(255,255,255,.09);
      background:rgba(255,255,255,.045);
      color:var(--text);
      border-radius:8px;
      transition:transform .14s ease, border-color .14s ease, background .14s ease, color .14s ease;
    }
    button:active { transform:translateY(1px); }
    .tabs button {
      min-height:36px;
      padding:8px 11px;
      color:var(--muted);
      background:transparent;
    }
    button.active, .tabs button.active {
      border-color:rgba(110,231,183,.6);
      color:#07100d;
      background:var(--accent);
      box-shadow:0 10px 24px rgba(110,231,183,.15);
    }
    input#search {
      height:43px;
      margin:2px 0 10px;
      border-color:rgba(255,255,255,.11);
      background:rgba(255,255,255,.055);
      box-shadow:inset 0 1px 0 rgba(255,255,255,.04);
    }
    .sports {
      gap:7px;
      margin:0 -12px 12px;
      padding:0 12px 2px;
      scrollbar-width:none;
    }
    .sports button {
      min-height:36px;
      padding:8px 11px;
      border-color:rgba(255,255,255,.1);
      background:rgba(255,255,255,.04);
      color:var(--muted);
    }
    .grid { grid-template-columns:1.18fr .82fr; gap:12px; }
    .panel {
      border:1px solid rgba(255,255,255,.08);
      border-radius:8px;
      background:rgba(17,22,21,.78);
      box-shadow:var(--shadow);
      overflow:hidden;
    }
    .section-head {
      display:flex;
      align-items:center;
      justify-content:space-between;
      gap:10px;
      padding:13px 14px;
      border-bottom:1px solid rgba(255,255,255,.07);
    }
    .section-head h2, h2 { padding:0; border:0; font-size:13px; color:var(--muted); text-transform:uppercase; font-weight:800; }
    .section-count { color:var(--accent); font-size:12px; font-weight:800; }
    .matches { display:grid; gap:10px; padding:10px; }
    .match {
      position:relative;
      display:grid;
      grid-template-columns:1fr;
      gap:10px;
      padding:13px;
      border:1px solid rgba(255,255,255,.08);
      border-radius:8px;
      background:linear-gradient(180deg, rgba(255,255,255,.052), rgba(255,255,255,.025));
      box-shadow:0 12px 28px rgba(0,0,0,.2);
    }
    .match::before {
      content:"";
      position:absolute;
      inset:0 auto 0 0;
      width:3px;
      border-radius:8px 0 0 8px;
      background:linear-gradient(180deg, var(--accent), var(--accent2));
    }
    .match.has-pick::before { background:linear-gradient(180deg, var(--accent2), var(--cyan)); }
    .match-head { display:flex; justify-content:space-between; gap:10px; align-items:center; padding-left:2px; }
    .sport-pill, .time-pill, .status-pill {
      display:inline-flex;
      align-items:center;
      gap:6px;
      min-height:25px;
      padding:4px 8px;
      border:1px solid rgba(255,255,255,.08);
      border-radius:8px;
      background:rgba(255,255,255,.04);
      color:var(--muted);
      font-size:11px;
      font-weight:700;
      white-space:nowrap;
    }
    .time-pill { color:var(--accent2); border-color:rgba(244,196,48,.24); background:rgba(244,196,48,.07); }
    .status-pill { color:var(--accent); border-color:rgba(110,231,183,.24); background:rgba(110,231,183,.07); }
    .match-title { font-size:15px; line-height:1.24; font-weight:850; padding-left:2px; }
    .match-meta { display:flex; flex-wrap:wrap; gap:7px; align-items:center; color:var(--muted); font-size:11px; padding-left:2px; }
    .ai-strip {
      display:grid;
      grid-template-columns:auto 1fr;
      gap:8px;
      align-items:center;
      padding:8px 10px;
      border:1px solid rgba(86,211,255,.16);
      border-radius:8px;
      background:rgba(86,211,255,.055);
      color:var(--muted);
      font-size:11px;
    }
    .ai-strip b { color:var(--text); font-size:12px; text-align:right; }
    .picks, .pick-row { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:7px; margin-top:0; }
    .pick-row.two { grid-template-columns:repeat(2,minmax(0,1fr)); }
    .pick-cta {
      min-height:46px;
      display:flex;
      flex-direction:column;
      align-items:center;
      justify-content:center;
      gap:2px;
      padding:7px 6px;
      border-color:rgba(110,231,183,.22);
      background:rgba(110,231,183,.07);
      white-space:normal;
    }
    .pick-cta span { font-size:12px; color:var(--muted); font-weight:800; }
    .pick-cta b { font-size:16px; color:var(--text); }
    .pick-cta.picked {
      border-color:rgba(244,196,48,.72);
      background:linear-gradient(180deg, rgba(244,196,48,.28), rgba(244,196,48,.14));
      color:var(--text);
      box-shadow:0 12px 26px rgba(244,196,48,.12);
    }
    .ticket-line {
      display:flex;
      justify-content:space-between;
      gap:8px;
      align-items:center;
      color:var(--muted);
      font-size:11px;
    }
    .ticket-line b { color:var(--accent2); }
    .odds { display:none !important; }
    .stats { grid-template-columns:repeat(4,minmax(0,1fr)); gap:8px; padding:10px; }
    .metric, .stat {
      min-height:74px;
      padding:11px;
      border:1px solid rgba(255,255,255,.08);
      border-radius:8px;
      background:rgba(255,255,255,.04);
    }
    .metric span, .stat span { color:var(--muted); font-size:11px; }
    .metric b, .stat b { display:block; margin-top:6px; font-size:20px; line-height:1.05; }
    .metric.good b { color:var(--accent); }
    .metric.gold b { color:var(--accent2); }
    .list { padding:10px; }
    .row {
      align-items:center;
      min-height:48px;
      padding:10px;
      border:1px solid rgba(255,255,255,.07);
      border-radius:8px;
      background:rgba(255,255,255,.035);
      margin-bottom:8px;
    }
    .row:last-child { margin-bottom:0; }
    .empty {
      margin:10px;
      padding:18px 14px;
      border:1px dashed rgba(255,255,255,.13);
      border-radius:8px;
      background:rgba(255,255,255,.025);
      text-align:center;
    }
    .bet-modal { align-items:end; backdrop-filter:blur(14px); background:rgba(0,0,0,.62); }
    .bet-card {
      border-color:rgba(255,255,255,.1);
      background:linear-gradient(180deg, #171d1b, #101413);
      box-shadow:0 28px 70px rgba(0,0,0,.58);
    }
    .bet-head { border-bottom-color:rgba(255,255,255,.08); }
    .bet-title { font-size:16px; }
    .bet-actions button, .stake-presets button { min-height:40px; }
    #betConfirm { background:var(--accent); color:#07100d; border-color:var(--accent); font-weight:900; }
    #betCancel, #betClose { color:var(--muted); }
    .toast { border-color:rgba(110,231,183,.22); background:#151c1a; }
    @media (max-width: 820px) {
      .shell { padding:10px; }
      header { margin:-10px -10px 8px; padding:10px; }
      .tabs { top:59px; margin:0 -10px 10px; padding:7px 10px; }
      .grid { grid-template-columns:1fr; }
      .stats { grid-template-columns:repeat(2,minmax(0,1fr)); }
      .balance-chip { padding:6px 8px; }
      .match-title { font-size:14px; }
      .panel { box-shadow:none; }
    }
    @media (max-width: 430px) {
      .brand-mark { width:34px; height:34px; }
      h1 { font-size:16px; }
      .sub { max-width:160px; }
      .tabs button { padding:8px 10px; }
      .match { padding:12px; }
      .pick-cta { min-height:43px; }
      .pick-cta b { font-size:15px; }
      .ai-strip { grid-template-columns:1fr; }
      .ai-strip b { text-align:left; }
    }
"""

BEAUTY_HEADER = r"""
<header>
  <div class="brand">
    <div class="brand-mark">P</div>
    <div><h1>Predictor</h1><div class="sub" id="status">Загрузка</div></div>
  </div>
  <div class="top-actions">
    <div class="balance-chip"><span>Баланс</span><b id="balanceChip">—</b></div>
    <button id="refresh" aria-label="Обновить">↻</button>
  </div>
</header>"""

BEAUTY_JS = r"""
const beautyEsc = value => String(value ?? '').replace(/[&<>'"]/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[ch]));
const beautyMoney = value => Number(value || 0).toLocaleString('ru-RU');
const beautySportIcon = sport => ({football:'⚽', hockey:'🏒', nhl:'🏒', tennis:'🎾', all:'★'})[(sport || '').toLowerCase()] || '•';
const beautySportName = sport => {
  const label = sportLabel((sport || 'all').toLowerCase());
  return String(label || sport || 'Матчи').replace(/^[^\p{L}\p{N}]+/u, '').trim() || 'Матчи';
};
const beautyPicks = m => (m?.available_picks && m.available_picks.length ? m.available_picks : ['1','X','2']);
const beautyPickSummary = (values, picks) => beautyPicks({available_picks:picks}).map(p => `${pickLabel(p)} ${fmtPct(values?.[p])}`).join(' · ');
function beautyProfileState(){ try { return profileState || {}; } catch(_e) { return {}; } }
function setBalanceChip(value){ const el=document.getElementById('balanceChip'); if(el) el.textContent = value == null ? '—' : `${beautyMoney(value)}`; }
function bestPick(m){
  const picks = beautyPicks(m);
  let best = picks[0], score = -1;
  picks.forEach(p => { const v = Number(m?.probabilities?.[p] || 0); if(v > score){ score = v; best = p; } });
  return {pick:best, score};
}
function pickButtonsPretty(m){
  const picks = beautyPicks(m);
  return picks.map(p => {
    const disabled = !m.can_predict || !picks.includes(p);
    const cls = m.my_pick === p ? 'picked' : '';
    return `<button class="pick-cta ${cls}" data-pick="${p}" data-id="${m.id}" ${disabled?'disabled':''}><span>${pickLabel(p)}</span><b>@${fmtOdd(m.odds?.[p])}</b></button>`;
  }).join('');
}
function renderSports(sports){
  const el = document.getElementById('sports');
  const total = (sports || []).reduce((a,x)=>a+Number(x.count || 0),0);
  const items = [{sport:'all', count:total}, ...(sports || [])];
  el.innerHTML = items.map(x=>`<button class="${x.sport===currentSport?'active':''}" data-sport="${x.sport}">${beautySportIcon(x.sport)} ${beautySportName(x.sport)} · ${x.count}</button>`).join('');
  el.querySelectorAll('button').forEach(b=>b.onclick=()=>{currentSport=b.dataset.sport; loadMatches();});
}
function renderMatches(items){
  try { lastMatches = items || []; } catch(_e) {}
  const el = document.getElementById('matches');
  const count = document.getElementById('matchCount');
  if(count) count.textContent = `${(items || []).length} матчей`;
  if(!items || !items.length){ el.innerHTML = '<div class="empty">Активных матчей нет</div>'; return; }
  el.innerHTML = items.map(m => {
    const picks = beautyPicks(m);
    const best = bestPick(m);
    const ticket = m.my_pick ? `Ставка: <b>${pickLabel(m.my_pick)} · ${beautyMoney(m.my_stake)} @${fmtOdd(m.my_odds)}</b>` : (m.can_predict ? 'Ставки открыты' : beautyEsc(m.blocked_reason || 'Ставки закрыты'));
    return `<article class="match beauty-match ${m.my_pick ? 'has-pick' : ''}">
      <div class="match-head">
        <span class="sport-pill">${beautySportIcon(m.sport)} ${beautyEsc(beautySportName(m.sport))}</span>
        <span class="time-pill">${beautyEsc(m.display_time || '—')}</span>
      </div>
      <div class="match-title">${beautyEsc(m.title || 'Матч')}</div>
      <div class="match-meta"><span>${beautyEsc(m.league || '—')}</span><span>Голоса ${Number(m.votes?.total || 0)}</span><span>AI пик ${pickLabel(best.pick)} ${fmtPct(best.score)}</span></div>
      <div class="ai-strip"><span>AI-линия</span><b>${beautyPickSummary(m.probabilities, picks)}</b></div>
      <div class="pick-row ${picks.length === 2 ? 'two' : ''}">${pickButtonsPretty(m)}</div>
      <div class="ticket-line"><span>${ticket}</span>${m.my_payout ? `<b>Выплата ${beautyMoney(m.my_payout)}</b>` : '<span class="status-pill">Live</span>'}</div>
    </article>`;
  }).join('');
  el.querySelectorAll('button[data-pick]').forEach(b=>b.onclick=()=>{
    if(typeof openBetModal === 'function') openBetModal(Number(b.dataset.id), b.dataset.pick);
    else sendPick(Number(b.dataset.id), b.dataset.pick);
  });
}
function renderProfile(p){
  try { profileState = Object.assign(beautyProfileState(), p || {}); if(Array.isArray(p?.stake_presets)) profileState.stake_presets = p.stake_presets; } catch(_e) {}
  setBalanceChip(p?.balance);
  const el = document.getElementById('profileStats');
  if(!initData){ el.innerHTML = '<div class="empty">Открой Mini App через Telegram</div>'; return; }
  const total = Number(p?.total || 0);
  const correct = Number(p?.correct || 0);
  const winrate = total ? `${Math.round(correct / total * 100)}%` : '—';
  el.innerHTML = `
    <div class="metric gold"><span>Баланс</span><b>${beautyMoney(p?.balance || 0)}</b></div>
    <div class="metric good"><span>Очки</span><b>${beautyMoney(p?.points || 0)}</b></div>
    <div class="metric"><span>Точность</span><b>${winrate}</b></div>
    <div class="metric"><span>Серия</span><b>${p?.streak || 0}</b></div>`;
}
function renderBacktest(bt){
  const stats = document.getElementById('btStats');
  const quick = document.getElementById('quickStats');
  if(!bt || !bt.matches){
    const empty = '<div class="empty">Backtest пока пуст</div>';
    stats.innerHTML = quick.innerHTML = empty;
    document.getElementById('calibration').innerHTML = document.getElementById('quickList').innerHTML = '';
    return;
  }
  const roi = bt.virtual_roi == null ? '—' : `${(bt.virtual_roi*100).toFixed(1)}%`;
  const html = `
    <div class="metric"><span>Матчи</span><b>${bt.matches}</b></div>
    <div class="metric good"><span>Точность</span><b>${fmtPct(bt.accuracy)}</b></div>
    <div class="metric"><span>Brier</span><b>${bt.brier ?? '—'}</b></div>
    <div class="metric gold"><span>ROI</span><b>${roi}</b></div>`;
  stats.innerHTML = quick.innerHTML = html;
  const rows = (bt.calibration || []).map(x=>`<div class="row"><span>${beautyEsc(x.bucket)} · n=${x.matches}</span><b>${fmtPct(x.accuracy)} / ${fmtPct(x.avg_confidence)}</b></div>`).join('');
  document.getElementById('calibration').innerHTML = rows || '<div class="empty">Калибровки пока нет</div>';
  document.getElementById('quickList').innerHTML = rows || '<div class="empty">Калибровки пока нет</div>';
}
function renderMine(items){
  const el = document.getElementById('myPredictions');
  if(!initData){ el.innerHTML = '<div class="empty">Открой Mini App через Telegram</div>'; return; }
  if(!items || !items.length){ el.innerHTML = '<div class="empty">Прогнозов пока нет</div>'; return; }
  el.innerHTML = items.map(x=>`<div class="row"><span>${beautyEsc(x.title)}<br><span class="muted">${beautySportIcon(x.sport)} ${beautyEsc(x.league || '—')} · ${beautyEsc(x.status || '')}</span></span><b>${pickLabel(x.pick)}${x.stake ? ` · ${beautyMoney(x.stake)} @${fmtOdd(x.odds)}` : ''} ${x.result ? `→ ${pickLabel(x.result)}` : ''}</b></div>`).join('');
}
function renderLeaders(data){
  const row = x => `<div class="row"><span>${x.place}. ${beautyEsc(x.name)}</span><b>${beautyMoney(x.points)}</b></div>`;
  document.getElementById('seasonLeaders').innerHTML = (data.season || []).map(row).join('') || '<div class="empty">Пока пусто</div>';
  document.getElementById('periodLeaders').innerHTML = '<div class="muted" style="padding:4px 2px 8px">Неделя</div>' + ((data.week || []).map(row).join('') || '<div class="empty">Пока пусто</div>') + '<div class="muted" style="padding:10px 2px 8px">Месяц</div>' + ((data.month || []).map(row).join('') || '<div class="empty">Пока пусто</div>');
}
async function loadSummary(){
  const summary = await api('/api/summary');
  renderSports(summary.sports || []);
  renderBacktest(summary.backtest);
  const status = document.getElementById('status');
  if(status) status.textContent = `${summary.ai_line ? 'AI линия активна' : 'AI линия выключена'} · ${authLabel()} · ${new Date(summary.now).toLocaleTimeString()}`;
}
document.querySelector('#matchesView .panel')?.classList.add('matches-panel');
document.querySelector('#matchesView .panel h2')?.parentElement?.classList.add('legacy-head');
const matchesPanel = document.querySelector('#matchesView .matches-panel');
if(matchesPanel && !document.getElementById('matchCount')){
  const h2 = matchesPanel.querySelector('h2');
  if(h2) h2.outerHTML = '<div class="section-head"><h2>Линия матчей</h2><span class="section-count" id="matchCount">0 матчей</span></div>';
}
const aiPanel = document.querySelector('#matchesView aside.panel');
if(aiPanel){ const h2 = aiPanel.querySelector('h2'); if(h2) h2.outerHTML = '<div class="section-head"><h2>AI-линия</h2><span class="section-count">Backtest</span></div>'; }
const refreshButton = document.getElementById('refresh');
if(refreshButton) refreshButton.textContent = '↻';
"""


def _patch_html(html: str) -> str:
    html = html.replace(
        ".toast { position:fixed; left:14px; right:14px; bottom:14px; padding:12px 14px; border:1px solid var(--line); border-radius:8px; background:#182332; color:var(--text); display:none; box-shadow:0 12px 32px rgba(0,0,0,.35); }",
        ".auth-block { display:none; width:min(560px, calc(100% - 28px)); margin:42px auto; padding:22px; border:1px solid var(--line); border-radius:8px; background:var(--panel); }\n    .auth-block h1 { font-size:22px; margin:0 0 8px; }\n    .auth-block p { color:var(--muted); line-height:1.45; margin:8px 0; }\n    .auth-block .steps { margin:14px 0 0; padding-left:20px; color:var(--text); line-height:1.65; }\n    .auth-hidden { display:none !important; }\n    .toast { position:fixed; left:14px; right:14px; bottom:14px; padding:12px 14px; border:1px solid var(--line); border-radius:8px; background:#182332; color:var(--text); display:none; box-shadow:0 12px 32px rgba(0,0,0,.35); }",
    )
    html = html.replace(
        ".match { display:grid; grid-template-columns: 1fr auto; gap:12px; padding:13px 14px; border-bottom:1px solid var(--line); }",
        ".match { display:grid; grid-template-columns:1fr; gap:12px; padding:13px 14px; border-bottom:1px solid var(--line); }",
    )
    html = html.replace(
        ".odds { display:grid; grid-template-columns:repeat(3, minmax(54px,1fr)); gap:6px; min-width:190px; }",
        ".odds { display:none; }",
    )
    html = html.replace(
        '<body>\n  <div class="shell">',
        '<body>\n  <section class="auth-block" id="authBlock">\n    <h1>Открой Mini App в Telegram</h1>\n    <p>Эта страница открыта как обычный сайт, поэтому Telegram не передал пользователя. Прогнозы здесь не записываются.</p>\n    <ol class="steps">\n      <li>Закрой это окно.</li>\n      <li>Открой чат с ботом в Telegram.</li>\n      <li>Нажми кнопку <b>📱 Mini App</b> или отправь <b>/app</b> и нажми <b>Открыть Mini App</b>.</li>\n    </ol>\n  </section>\n  <div class="shell" id="appShell">',
    )
    html = html.replace(
        '<header>\n      <div><h1>Predictor Bot</h1><div class="sub" id="status">Загрузка</div></div>\n      <button id="refresh">Обновить</button>\n    </header>',
        BEAUTY_HEADER,
        1,
    )
    html = html.replace(
        '<input id="search" placeholder="Поиск матча" />',
        '<input id="search" placeholder="Найти матч, лигу или турнир" />',
        1,
    )
    html = html.replace(
        "const initData = tg?.initData || '';",
        "const initData = tg?.initData || '';\nconst tgUser = tg?.initDataUnsafe?.user || null;\nconst authLabel = () => initData ? `TG OK: ${tgUser?.username ? '@' + tgUser.username : (tgUser?.first_name || 'user')}` : 'TG NO';\nfunction enforceTelegramLaunch(){ const block=document.getElementById('authBlock'); const shell=document.getElementById('appShell'); if(!initData){ if(block) block.style.display='block'; if(shell) shell.classList.add('auth-hidden'); document.body.style.background='var(--bg)'; return false; } if(block) block.style.display='none'; if(shell) shell.classList.remove('auth-hidden'); return true; }",
    )
    html = html.replace(
        "const pickLabel = p => ({'1':'П1','X':'X','2':'П2'})[p] || p;",
        "const pickLabel = p => ({'1':'П1','X':'X','2':'П2'})[p] || p;\nconst pickSummary = (values, picks) => (picks || ['1','X','2']).map(p => `${pickLabel(p)} ${fmtPct(values?.[p])}`).join(' · ');",
    )
    html = html.replace(
        "nhl:'🏒 Хоккей', all:'Все'",
        "nhl:'🏒 Хоккей', tennis:'🎾 Теннис', all:'Все'",
    )
    html = html.replace(
        "return ['1','X','2'].map(p=>{",
        "return (m.available_picks || ['1','X','2']).map(p=>{",
    )
    html = html.replace(
        "const disabled = !initData || !m.can_predict || !(m.available_picks || []).includes(p);",
        "const disabled = !m.can_predict || !(m.available_picks || []).includes(p);",
    )
    html = html.replace(
        "<div class=\"meta\">AI: П1 ${fmtPct(m.probabilities['1'])} · X ${fmtPct(m.probabilities.X)} · П2 ${fmtPct(m.probabilities['2'])}</div>",
        "<div class=\"meta\">AI: ${pickSummary(m.probabilities, m.available_picks)}</div>",
    )
    html = html.replace(
        "if(!initData){ toast('Открой через Telegram'); return; }",
        "if(!initData){ enforceTelegramLaunch(); return; }",
    )
    html = html.replace(
        "toast(e.message || 'Ошибка прогноза');",
        "const msg=e.message || 'Ошибка прогноза'; tg?.showAlert?.(msg); toast(msg);",
    )
    html = html.replace(
        "document.getElementById('status').textContent = `AI ${summary.ai_line ? 'ON' : 'OFF'} · ${new Date(summary.now).toLocaleTimeString()}`;",
        "document.getElementById('status').textContent = `AI ${summary.ai_line ? 'ON' : 'OFF'} · ${authLabel()} · ${new Date(summary.now).toLocaleTimeString()}`;",
    )
    html = html.replace(
        "async function load(){ await Promise.all([loadSummary(), loadMatches(), loadMine(), loadProfile(), loadLeaders()]); if(!initData) toast('Для прогнозов открой Mini App из Telegram'); }",
        "async function load(){ if(!enforceTelegramLaunch()) return; await Promise.all([loadSummary(), loadMatches(), loadMine(), loadProfile(), loadLeaders()]); }",
    )
    html = html.replace(
        "load().catch(e => { document.getElementById('status').textContent = 'Ошибка загрузки'; toast(e.message || 'Ошибка загрузки'); console.error(e); });",
        "enforceTelegramLaunch();\nload().catch(e => { document.getElementById('status').textContent = 'Ошибка загрузки'; toast(e.message || 'Ошибка загрузки'); console.error(e); });",
    )
    html = html.replace("</style>", BEAUTY_CSS + "\n  </style>", 1)
    html = html.replace("document.getElementById('refresh').onclick = load;", BEAUTY_JS + "\ndocument.getElementById('refresh').onclick = load;", 1)
    return html


async def _set_menu_button(bot: Any) -> None:
    base_url_fn = None
    try:
        import mini_app

        base_url_fn = getattr(mini_app, "_public_base_url", None)
    except Exception:
        base_url_fn = None

    base = base_url_fn() if callable(base_url_fn) else ""
    url = f"{base}/app" if base else ""
    if not url:
        return

    try:
        from aiogram.types import MenuButtonWebApp, WebAppInfo

        await bot.bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(text="Predictor", web_app=WebAppInfo(url=url))
        )
        logger = getattr(bot, "logger", None)
        if logger:
            logger.info("mini app menu button set to %s", url)
    except Exception as exc:
        logger = getattr(bot, "logger", None)
        if logger:
            logger.warning("mini app menu button setup failed: %s", exc)


def _patch_start_web_server(bot: Any) -> None:
    original = getattr(bot, "start_web_server", None)
    if not callable(original) or getattr(original, "_mini_app_menu_wrapped", False):
        return

    async def patched_start_web_server(*args, **kwargs):
        await _set_menu_button(bot)
        return await original(*args, **kwargs)

    patched_start_web_server._mini_app_menu_wrapped = True
    bot.start_web_server = patched_start_web_server


def apply(bot: Any) -> None:
    if getattr(bot, "_MINI_APP_PATCH_APPLIED", False):
        return

    os.environ.setdefault("MINI_APP_AUTH_MAX_AGE", "604800")

    try:
        import mini_app
    except Exception as exc:
        logger = getattr(bot, "logger", None)
        if logger:
            logger.exception("mini app patch import failed: %s", exc)
        return

    original_index_html = getattr(mini_app, "_index_html", None)
    if callable(original_index_html) and not getattr(original_index_html, "_mini_app_patch_wrapped", False):
        def patched_index_html() -> str:
            return _patch_html(original_index_html())

        patched_index_html._mini_app_patch_wrapped = True
        mini_app._index_html = patched_index_html

    _patch_start_web_server(bot)
    bot._MINI_APP_PATCH_APPLIED = True
    print(f"{VERSION}_APPLIED")
