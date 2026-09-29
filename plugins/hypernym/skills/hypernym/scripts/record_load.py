#!/usr/bin/env python3
"""
PostToolUse hook: remember which Hypernym package THIS session loaded, so "analysis in use" on the status line and the
reload after a compaction are true (the Pilot's design, 2026-09-29).

Fires on the hypernym_load MCP tool and on Bash (only a `hypernym.py … load` command counts). The package is read from
the load's own output — `===== <package>/treatment_prompt.txt =====` — so only a load that actually printed a package is
recorded. Writes ~/.hypernym/sessions/<session_id>.json {package, job_id, repo, loaded_at}. Prints nothing; local only.
NO .get().
"""
import json
import re
import sys
import time
from pathlib import Path

SESSIONS = Path.home() / '.hypernym' / 'sessions'
MARK = re.compile(r'===== (.+?)/treatment_prompt\.txt =====')


def main() -> None:
    payload = json.loads(sys.stdin.read())
    if 'tool_name' not in payload:
        return                                          # not a tool event (a hook never errors after the user's turn)
    tool = payload['tool_name']
    if tool == 'Bash':
        command = payload['tool_input']['command']
        if 'hypernym.py' not in command or not re.search(r'\bload\b', command):
            return
    elif not tool.endswith('hypernym_load'):
        return
    m = MARK.search(json.dumps(payload['tool_response'], ensure_ascii=False))
    if m is None:
        return                                          # the load printed no package (none yet, or it failed)
    pkg = Path(m.group(1).replace('\\\\', '\\'))
    manifest = json.loads((pkg / 'manifest.json').read_text())
    SESSIONS.mkdir(parents=True, exist_ok=True)
    target = SESSIONS / f"{payload['session_id']}.json"
    tmp = target.with_suffix('.tmp')
    tmp.write_text(json.dumps({'package': str(pkg), 'job_id': manifest['job_id'], 'repo': manifest['repo'].lower(),
                               'loaded_at': time.time()}))
    tmp.replace(target)


if __name__ == '__main__':
    main()
