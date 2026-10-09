#!/bin/bash
# Doppelklick im Finder startet SongBridge und öffnet den Browser.
cd "$(dirname "$0")"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt
exec .venv/bin/python -m songbridge
