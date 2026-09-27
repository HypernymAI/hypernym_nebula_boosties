"""
Turn state of a live Claude Code transcript (build doc S24/S25) — shared by the badge (must stay light: no numpy, no
models) and the tracker's callers. A turn has ENDED when the last user/assistant/system record is a system
`turn_duration` / `stop_hook_summary` (what Claude Code writes after a turn). NO .get(). Imports at top.
"""
import json
from pathlib import Path

TURN_END = ('turn_duration', 'stop_hook_summary')
TAIL_BYTES = 262144


def records(data: bytes) -> list:
    """JSON records of a byte span; a line cut by a seek (first/last) is not a record and is skipped."""
    out = []
    for line in data.split(b'\n'):
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def turn_ended(jsonl: Path) -> bool:
    with open(jsonl, 'rb') as f:
        f.seek(0, 2)
        f.seek(max(0, f.tell() - TAIL_BYTES))
        recs = records(f.read())
    for r in reversed(recs):
        if isinstance(r, dict) and 'type' in r and r['type'] in ('user', 'assistant', 'system'):
            return r['type'] == 'system' and 'subtype' in r and r['subtype'] in TURN_END
    return False
