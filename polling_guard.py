from __future__ import annotations

import asyncio
import os
from typing import Any

VERSION = "POLLING_GUARD_V1"
FALSE_VALUES = {"0", "false", "no", "off", "disabled"}


def _env_enabled(name: str, default: str = "1") -> bool:
    return (os.getenv(name, default) or default).strip().lower() not in FALSE_VALUES


async def _sleep_forever(bot_module: Any, reason: str) -> None:
    logger = getattr(bot_module, "logger", None)
    if logger:
        logger.warning("polling guard: %s", reason)
    while True:
        await asyncio.sleep(3600)


def _patch_get_updates(bot_module: Any) -> None:
    tg_bot = getattr(bot_module, "bot", None)
    original_get_updates = getattr(tg_bot, "get_updates", None)
    if tg_bot is None or not callable(original_get_updates) or getattr(original_get_updates, "_polling_guard_wrapped", False):
        return

    try:
        from aiogram.exceptions import TelegramConflictError
    except Exception:  # pragma: no cover - aiogram is present in production
        TelegramConflictError = RuntimeError

    conflict_count = 0

    async def guarded_get_updates(*args, **kwargs):
        nonlocal conflict_count
        try:
            return await original_get_updates(*args, **kwargs)
        except TelegramConflictError:
            conflict_count += 1
            logger = getattr(bot_module, "logger", None)
            sleep_s = max(30, int(os.getenv("POLLING_CONFLICT_SLEEP", "300") or "300"))
            max_conflicts = max(1, int(os.getenv("POLLING_CONFLICT_MAX", "2") or "2"))
            if logger:
                logger.error(
                    "polling guard: Telegram getUpdates conflict (%s/%s). Another instance uses this BOT_TOKEN.",
                    conflict_count,
                    max_conflicts,
                )
            if conflict_count >= max_conflicts:
                await _sleep_forever(
                    bot_module,
                    "too many Telegram conflicts; this process will keep web/Mini App alive without polling",
                )
            await asyncio.sleep(sleep_s)
            return []

    guarded_get_updates._polling_guard_wrapped = True
    tg_bot.get_updates = guarded_get_updates


def apply(bot_module: Any) -> None:
    if getattr(bot_module, "_POLLING_GUARD_APPLIED", False):
        return

    dp = getattr(bot_module, "dp", None)
    original_start_polling = getattr(dp, "start_polling", None)
    if dp is None or not callable(original_start_polling):
        return

    _patch_get_updates(bot_module)

    async def guarded_start_polling(*args, **kwargs):
        if not _env_enabled("POLLING_ENABLED", "1"):
            return await _sleep_forever(bot_module, "POLLING_ENABLED=0; polling disabled for this process")

        if _env_enabled("POLLING_LOCK_ENABLED", "1") and callable(getattr(bot_module, "acquire_polling_lock", None)):
            try:
                locked = bool(bot_module.acquire_polling_lock())
            except Exception as exc:
                logger = getattr(bot_module, "logger", None)
                if logger:
                    logger.exception("polling guard: lock check failed: %s", exc)
                locked = True

            if not locked:
                return await _sleep_forever(
                    bot_module,
                    "another local process owns the polling lock; Mini App/web stays online here",
                )

            heartbeat = getattr(bot_module, "polling_lock_heartbeat", None)
            if callable(heartbeat):
                asyncio.create_task(heartbeat())

        logger = getattr(bot_module, "logger", None)
        if logger:
            logger.info("polling guard: polling owner started")
        return await original_start_polling(*args, **kwargs)

    guarded_start_polling._polling_guard_wrapped = True
    dp.start_polling = guarded_start_polling
    bot_module._POLLING_GUARD_APPLIED = True
    print(f"{VERSION}_APPLIED")
