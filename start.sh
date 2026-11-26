#!/bin/bash
# Supervisor launcher for the whizclip bot.
# Runs the bot under a restart loop (a simple alternative to systemd on
# hosts without a user bus). For a systemd setup, see the unit example
# in README.md instead.
cd "$(dirname "$0")"
export NO_PROXY="localhost,127.0.0.1"
export no_proxy="localhost,127.0.0.1"
# Point this at the transcription venv's Python if it is not the default
# ./transcribe-venv (see README):
# export TRANSCRIBE_PYTHON=/path/to/transcribe-venv/bin/python
while true; do
  echo "[start.sh] starting bot $(date -Is)" >> bot-supervisor.log
  venv/bin/python bot.py >> bot.log 2>&1
  echo "[start.sh] bot exited ($?), restarting in 5s $(date -Is)" >> bot-supervisor.log
  sleep 5
done
