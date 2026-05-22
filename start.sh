#!/bin/bash
set -e
export PYTHONPATH="${PYTHONPATH:+$PYTHONPATH:}."
python -c "import sitecustomize, runpy; runpy.run_path('bot.py', run_name='__main__')"
