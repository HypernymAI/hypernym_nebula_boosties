#!/usr/bin/env python3
"""
CODEX ONLY — the Zephyr status row, shown inside the Codex conversation (2026-10-19).

Codex has no status line a plugin can draw, so the row Claude Code's status line shows (statusline.py — the same words,
colours and states: analyzing 15/37 files 41%, analysis complete — 38 files, top up to keep going, reconnecting to
Zephyr — patience …) is shown the way nebula's score line is: as the hook's systemMessage, which Codex prints in the
conversation as `↳ Hook · <row>`.
  SessionStart (startup / resume / clear): the current row, once — unless the Zephyr cache is still being filled
                ("checking Zephyr") or this is not a GitHub repo (session_context says nothing there either).
  Stop:         the row under the reply only when it differs from the last one shown in this session (while a job
                runs its progress changes, so it shows every turn; at rest it shows once per change).
Claude Code (no PLUGIN_ROOT in the hook's environment): prints nothing — its status line already shows the row.
The row is produced by running statusline.py on the hook's own event; this file only decides when to show it.
Remembered per session in ~/.hypernym/codex_status/<session_id>.txt (local only). NO .get().
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SHOWN = Path.home() / '.hypernym' / 'codex_status'
ANSI = re.compile(r'\x1b\[[0-9;]*m')
MOTION = '⠈⠐⠠⢀⡀⠄⠂⠁⟳'                    # statusline's patience clock and its stale mark: motion, not a change
QUIET = ('checking Zephyr', 'not a git repo', 'no github remote')


def row(event: dict) -> str:
    r = subprocess.run([sys.executable, str(HERE / 'statusline.py')], input=json.dumps(event), capture_output=True,
                       text=True, timeout=8)
    if r.returncode != 0:
        raise RuntimeError(f"statusline.py failed: {r.stderr.strip()[-300:]}")
    return r.stdout.strip()


def change_key(text: str) -> str:
    plain = ANSI.sub('', text)
    for ch in MOTION:
        plain = plain.replace(ch, '')
    return ' '.join(plain.split())


def main() -> None:
    event = json.loads(sys.stdin.read())
    if 'PLUGIN_ROOT' not in os.environ:
        return
    line = row({'session_id': event['session_id'], 'cwd': event['cwd']})
    key = change_key(line)
    for q in QUIET:
        if q in key:
            return
    SHOWN.mkdir(parents=True, exist_ok=True)
    memo = SHOWN / f"{event['session_id']}.txt"
    if event['hook_event_name'] == 'Stop':
        if memo.exists():
            if memo.read_text() == key:
                return
    tmp = memo.with_name(f'{memo.name}.{os.getpid()}.tmp')
    tmp.write_text(key)
    tmp.replace(memo)
    print(json.dumps({'systemMessage': line}))


if __name__ == '__main__':
    main()
