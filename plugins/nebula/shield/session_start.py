#!/usr/bin/env python3
"""
SessionStart hook: prepare nebula for this Claude Code session. Runs through bin/nebula-run, so the FIRST
session after install also creates the plugin's own Python environment (uv; numpy is the only dependency).

  - ~/.nebula exists, private (0700); the effective config is (re)built from defaults ⊕ your settings
  - <this plugin version>/.venv/.nebula_ready marks the environment as built — the shared status line waits for it
    (shows "setting up" until then) instead of starting a second environment build every second
  - the shared hypernym-nebula status line (nebula_statusline.py, identical in the hypernym plugin) is refreshed in
    ~/.hypernym-nebula/bin; ~/.claude/settings.json gets statusLine ONLY when there is none or it is one of ours
    (backed up first; never a foreign one; never again after `/nebula:statusline remove`)
  - prints nothing to Claude Code (no context is injected)
It never reads a transcript and never backfills history (opt-in: /nebula:shield-backfill).
NO .get(). Imports at top.
"""
import noegress                                   # noqa: F401 — FIRST: the no-network guarantee (noegress.py)
import json
import sys
from pathlib import Path

import nebula_statusline
from settings import PLUGIN_ROOT, resolve_config


def main() -> None:
    event = json.load(sys.stdin)                  # the SessionStart event: only its transcript path is read
    resolve_config(None)                          # creates ~/.nebula (0700) + the effective config
    codex = 'transcript_path' in event and isinstance(event['transcript_path'], str) \
        and Path(event['transcript_path']).name.startswith('rollout-')
    if not codex:                                 # Codex (harness.py) has no status line: never edit Claude's settings
        nebula_statusline.auto()                  # refresh the shared launcher; statusLine only if none / ours
    ready = PLUGIN_ROOT / '.venv' / '.nebula_ready'
    if not ready.exists():
        ready.write_text("the plugin's Python environment is built (written by session_start.py)\n")


if __name__ == '__main__':
    main()
