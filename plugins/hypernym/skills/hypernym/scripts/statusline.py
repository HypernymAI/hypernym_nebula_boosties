#!/usr/bin/env python3
"""
Claude Code STATUS LINE segment: Hypernym state for the repo this session is in.

stdin: the status-line JSON (session_id, workspace.current_dir / cwd). Reads ~/.hypernym/status/<owner__repo>.json, written by
`hypernym.py statuscache` through the API with the user's key. Never calls the network itself: if the cache is older
than REFRESH_S and no refresh is in flight, it starts one detached and shows the cached line meanwhile.

    echo '{"session_id": "s1", "cwd": "…"}' | python3 statusline.py

THE ROW (the Pilot's design, 2026-09-29 — only these states; no prefix, no counts, no drift, no account or balance;
the server name only when more than one server is configured):
    ⢤⣶⡾ say "hypernym login"                   not signed in / signed out
    ⢤⣶⡾ add credit — portal.hypernym.ai/plan   the Zephyr refused a submission for credit (402 insufficient_credit)
    ⢤⣶⡾ analyzing… 12/40 files                 a job running: floats completed/total
    ⢤⣶⡾ analysis in use                        THIS session loaded this repo's package (~/.hypernym/sessions/<id>.json)
    ⢤⣶⡾ analysis ready                         a package is on disk that this session has not loaded yet
    ⢤⣶⡾ no analysis for this repo              signed in, nothing for this repo
    not a git repo / no github remote          as before
A server error that is none of these (unreachable, HTTP 5xx) is shown as it is — never hidden.

The Zephyr icon flashes (brand.icon_flash, shared with nebula) for 2.4–2.9 s whenever the Zephyr-side data behind
the line changes — jobs, stages, floats, packages, errors — per Claude Code window (key zephyr:<session_id>).
NO .get().
"""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from brand import ZEPHYR_ICON, icon_flash

HERE = Path(__file__).resolve().parent
STATUS = Path.home() / '.hypernym' / 'status'
CONFIG = Path.home() / '.hypernym' / 'config.json'
SESSIONS = Path.home() / '.hypernym' / 'sessions'
CREDIT_REFUSED = Path.home() / '.hypernym' / 'credit_refused.json'
LOGIN = 'say "hypernym login"'
REFRESH_S = 20
LOCK_S = 120
RESET, DIM = '\x1b[0m', '\x1b[2m'
GREEN, AMBER, GREY, RED = ('\x1b[38;2;120;200;140m', '\x1b[38;2;255;170;40m', '\x1b[38;2;110;110;110m',
                           '\x1b[38;2;255;70;70m')
# The ZEPHYR racer (Hypernym's own design, claudestorm images/zephyr/pink/design_racer.png) as a 3×1 braille icon —
# the same glyphs + colours as ctxshield brand.ZEPHYR_ICON (brand_assets/README.md §8). Replaced the ☁️ cloud.
CLOUD = ('\x1b[38;2;255;92;170m⢤\x1b[0m\x1b[38;2;232;24;128m⣶\x1b[0m\x1b[38;2;190;60;200m⡾\x1b[0m')


def repo_key(url: str) -> str:
    m = re.search(r'github\.com[:/]+([^/]+)/([^/]+?)(?:\.git)?/?$', url.strip())
    if m is None:
        return ''
    return f"{m.group(1)}/{m.group(2)}".lower()


def refresh(cwd: str, key: str) -> None:
    lock = STATUS / f"{key.replace('/', '__')}.lock"
    if lock.exists() and time.time() - lock.stat().st_mtime < LOCK_S:
        return
    lock.write_text(str(time.time()))
    log = open(STATUS / 'refresh.log', 'a')
    subprocess.Popen(
        ['sh', '-c', f'"{sys.executable}" "{HERE / "hypernym.py"}" --dir "$1" statuscache; rm -f "$2"', 'sh', cwd,
         str(lock)],
        stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    log.close()


def signed_in() -> bool:
    """An env key, or at least one configured server (a RESTORIA device enrollment or a key)."""
    if 'HYPERNYM_API_KEY' in os.environ:
        return True
    if not CONFIG.exists():
        return False
    return len(json.loads(CONFIG.read_text())['servers']) > 0


def zephyr_signature(c: dict) -> str:
    """Everything Zephyr-side the line shows, and nothing else (not refreshed_at): a change here is an update."""
    return json.dumps({'servers': c['servers'], 'packages': c['packages'], 'newest_package': c['newest_package']},
                      sort_keys=True)


def in_use(session_id: str, key: str) -> bool:
    """This session loaded this repo's package (written by record_load.py, the PostToolUse hook) and it is still on disk."""
    rec = SESSIONS / f"{session_id}.json"
    if not rec.exists():
        return False
    r = json.loads(rec.read_text())
    return r['repo'] == key and Path(r['package']).exists()


def server_state(s: dict):
    """(colour, text) for one server, or None when it has nothing of its own to say (then the repo's state shows)."""
    if s['error'] == 'HTTP 401':
        return AMBER, LOGIN
    if s['error'] == 'HTTP 402':
        return RED, 'add credit — portal.hypernym.ai/plan'
    if s['error']:
        return RED, s['error']
    running = [j for j in s['jobs'] if j['status'] == 'PROCESSING']
    if running:
        j = running[0]
        if j['floats_total'] is not None:
            return AMBER, f"analyzing… {j['floats_completed']}/{j['floats_total']} files"
        return AMBER, 'analyzing…'
    return None


def render(c: dict, session_id: str, key: str, stale: bool, icon: str) -> str:
    many = len(c['servers']) > 1
    parts = []
    if CREDIT_REFUSED.exists():
        parts.append(f"{RED}add credit — portal.hypernym.ai/plan{RESET}")
    else:
        for s in c['servers']:
            st = server_state(s)
            if st is not None:
                parts.append(f"{st[0]}{s['name'] + ' ' if many else ''}{st[1]}{RESET}")
    if not parts:
        if in_use(session_id, key):
            parts.append(f"{GREEN}analysis in use{RESET}")
        elif c['newest_package'] is not None:
            parts.append(f"{GREEN}analysis ready{RESET}")
        else:
            parts.append(f"{GREY}no analysis for this repo{RESET}")
    tail = ''
    if stale:
        tail = f" {DIM}⟳{RESET}"
    return f"{icon} " + '  '.join(parts) + tail


def main() -> None:
    event = json.load(sys.stdin)
    if not signed_in():
        print(f"{CLOUD} {AMBER}{LOGIN}{RESET}")
        return
    if 'workspace' in event and 'current_dir' in event['workspace']:
        cwd = event['workspace']['current_dir']
    else:
        cwd = event['cwd']
    r = subprocess.run(['git', '-C', cwd, 'remote', 'get-url', 'origin'], capture_output=True, text=True, timeout=2)
    if r.returncode != 0:
        print(f"{CLOUD} {GREY}not a git repo{RESET}")
        return
    key = repo_key(r.stdout)
    if not key:
        print(f"{CLOUD} {GREY}no github remote{RESET}")
        return
    STATUS.mkdir(parents=True, exist_ok=True)
    cache = STATUS / f"{key.replace('/', '__')}.json"
    stale = not cache.exists() or time.time() - cache.stat().st_mtime > REFRESH_S
    if stale:
        refresh(cwd, key)
    if not cache.exists():
        print(f"{CLOUD} {GREY}…{RESET}")
        return
    c = json.loads(cache.read_text())
    icon = icon_flash(ZEPHYR_ICON, f"zephyr:{event['session_id']}", zephyr_signature(c), time.time())
    print(render(c, event['session_id'], key, stale, icon))


if __name__ == '__main__':
    main()
