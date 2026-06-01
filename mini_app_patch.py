from __future__ import annotations

import os
from typing import Any

VERSION = "MINI_APP_PATCH_V1"


def _patch_html(html: str) -> str:
    html = html.replace(
        "const initData = tg?.initData || '';",
        "const initData = tg?.initData || '';\nconst tgUser = tg?.initDataUnsafe?.user || null;\nconst authLabel = () => initData ? `TG OK: ${tgUser?.username ? '@' + tgUser.username : (tgUser?.first_name || 'user')}` : 'TG NO: открой через кнопку Mini App в Telegram';",
    )
    html = html.replace(
        "const disabled = !initData || !m.can_predict || !(m.available_picks || []).includes(p);",
        "const disabled = !m.can_predict || !(m.available_picks || []).includes(p);",
    )
    html = html.replace(
        "if(!initData){ toast('Открой через Telegram'); return; }",
        "if(!initData){ const msg='Нет Telegram initData. Закрой страницу и открой Mini App кнопкой в чате бота, не обычной ссылкой.'; tg?.showAlert?.(msg); toast(msg); return; }",
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
        "if(!initData) toast('Для прогнозов открой Mini App из Telegram');",
        "if(!initData) toast('Прогнозы доступны только при открытии через Telegram Mini App кнопку');",
    )
    return html


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

    bot._MINI_APP_PATCH_APPLIED = True
    print(f"{VERSION}_APPLIED")
