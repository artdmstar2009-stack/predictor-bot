from __future__ import annotations

import os
from typing import Any

VERSION = "SILENT_RESULTS_PATCH_V1"


def _env_enabled(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _should_suppress(chat_id: Any, text: Any) -> bool:
    message = _text(text)
    if not message:
        return False

    # User-facing settlement messages from apply_scoring_for_match.
    if not _env_enabled("RESULT_NOTIFICATIONS_ENABLED", "0"):
        if "Итог матча" in message or message.startswith("🏁 Итог"):
            return True

    # Admin spam from auto_results_loop: one message per closed match.
    if not _env_enabled("ADMIN_MATCH_CLOSED_MESSAGES_ENABLED", "0"):
        if message.startswith("✅ Матч закрыт"):
            return True

    return False


def _patch_send_message(app: Any) -> None:
    bot_obj = getattr(app, "bot", None)
    original = getattr(bot_obj, "send_message", None)
    if not callable(original) or getattr(original, "_silent_results_wrapped", False):
        return

    async def patched_send_message(chat_id: Any, text: Any, *args: Any, **kwargs: Any) -> Any:
        if _should_suppress(chat_id, text):
            logger = getattr(app, "logger", None)
            if logger:
                logger.info("silent results: suppressed result message chat_id=%s", chat_id)
            return None
        return await original(chat_id, text, *args, **kwargs)

    patched_send_message._silent_results_wrapped = True
    bot_obj.send_message = patched_send_message


def apply(app: Any) -> None:
    if getattr(app, "_SILENT_RESULTS_PATCH_APPLIED", False):
        return
    _patch_send_message(app)
    app._SILENT_RESULTS_PATCH_APPLIED = True
    print(f"{VERSION}_APPLIED")
