from __future__ import annotations

import os
from typing import Any

VERSION = "MINI_APP_PATCH_V3"


def _patch_html(html: str) -> str:
    html = html.replace(
        ".toast { position:fixed; left:14px; right:14px; bottom:14px; padding:12px 14px; border:1px solid var(--line); border-radius:8px; background:#182332; color:var(--text); display:none; box-shadow:0 12px 32px rgba(0,0,0,.35); }",
        ".auth-block { display:none; width:min(560px, calc(100% - 28px)); margin:42px auto; padding:22px; border:1px solid var(--line); border-radius:8px; background:var(--panel); }\n    .auth-block h1 { font-size:22px; margin:0 0 8px; }\n    .auth-block p { color:var(--muted); line-height:1.45; margin:8px 0; }\n    .auth-block .steps { margin:14px 0 0; padding-left:20px; color:var(--text); line-height:1.65; }\n    .auth-hidden { display:none !important; }\n    .toast { position:fixed; left:14px; right:14px; bottom:14px; padding:12px 14px; border:1px solid var(--line); border-radius:8px; background:#182332; color:var(--text); display:none; box-shadow:0 12px 32px rgba(0,0,0,.35); }",
    )
    html = html.replace(
        '<body>\n  <div class="shell">',
        '<body>\n  <section class="auth-block" id="authBlock">\n    <h1>Открой Mini App в Telegram</h1>\n    <p>Эта страница открыта как обычный сайт, поэтому Telegram не передал пользователя. Прогнозы здесь не записываются.</p>\n    <ol class="steps">\n      <li>Закрой это окно.</li>\n      <li>Открой чат с ботом в Telegram.</li>\n      <li>Нажми кнопку <b>📱 Mini App</b> или отправь <b>/app</b> и нажми <b>Открыть Mini App</b>.</li>\n    </ol>\n  </section>\n  <div class="shell" id="appShell">',
    )
    html = html.replace(
        "const initData = tg?.initData || '';",
        "const initData = tg?.initData || '';\nconst tgUser = tg?.initDataUnsafe?.user || null;\nconst authLabel = () => initData ? `TG OK: ${tgUser?.username ? '@' + tgUser.username : (tgUser?.first_name || 'user')}` : 'TG NO';\nfunction enforceTelegramLaunch(){ const block=document.getElementById('authBlock'); const shell=document.getElementById('appShell'); if(!initData){ if(block) block.style.display='block'; if(shell) shell.classList.add('auth-hidden'); document.body.style.background='var(--bg)'; return false; } if(block) block.style.display='none'; if(shell) shell.classList.remove('auth-hidden'); return true; }",
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
