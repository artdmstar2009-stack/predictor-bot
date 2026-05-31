from __future__ import annotations

"""Compatibility launcher.

Render may start `python bot.py`, while runner.py contains the patched football
provider. The original bot module lives in bot_core.py.
"""

import asyncio
import importlib
import sys

bot_core = importlib.import_module("bot_core")
sys.modules["bot"] = bot_core


def main() -> None:
    runner = importlib.import_module("runner")
    apply_theme = getattr(runner, "apply_theme", None)
    if callable(apply_theme):
        apply_theme()
    else:
        try:
            theme = importlib.import_module("theme")
            theme.apply(bot_core)
            print("RUNNER_THEME_APPLIED")
        except Exception as exc:
            bot_core.logger.exception("theme apply failed: %s", exc)

    asyncio.run(bot_core.main())


if __name__ == "__main__":
    main()
