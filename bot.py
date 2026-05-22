from __future__ import annotations

"""Compatibility launcher.

Render was still starting `python bot.py`, so keep that command working while
running the patched football provider from runner.py. The original bot module
lives in bot_core.py.
"""

import importlib
import runpy
import sys

bot_core = importlib.import_module("bot_core")
sys.modules["bot"] = bot_core

runpy.run_module("runner", run_name="__main__")
