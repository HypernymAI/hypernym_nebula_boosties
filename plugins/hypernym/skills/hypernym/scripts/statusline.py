#!/usr/bin/env python3
"""
Claude Code STATUS LINE segment: Hypernym state for the repo this session is in.

stdin: the status-line JSON (session_id, workspace.current_dir / cwd). Reads ~/.hypernym/status/<owner__repo>.json, written by
`hypernym.py statuscache` through the API with the user's key. Never calls the network itself: if the cache is older
than REFRESH_S and no refresh is in flight, it starts one detached and shows the cached line meanwhile.

    echo '{"session_id": "s1", "cwd": "…"}' | python3 statusline.py

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


def render(c: dict, stale: bool, icon: str) -> str:
    parts = []
    if 'sending_off' in c:
        parts.append(f"{GREY}sending off for this repo{RESET}")
    for s in c['servers']:
        if s['error'] == 'HTTP 401':
            parts.append(f"{AMBER}{s['name']} signed out — run: hypernym login{RESET}")
            continue
        if s['error']:
            parts.append(f"{RED}{s['name']} {s['error']}{RESET}")
            continue
        running = [j for j in s['jobs'] if j['status'] == 'PROCESSING']
        if running:
            j = running[0]
            prog = f"{j['stage']} {j['stage_number']}/11"
            if j['floats_total'] is not None:
                prog += f" · floats {j['floats_completed']}/{j['floats_total']}"
            more = ''
            if len(running) > 1:
                more = f" +{len(running) - 1}"
            parts.append(f"{AMBER}{s['name']} ⟳ {prog}{more}{RESET}")
        elif s['floats'] or s['jobs']:
            done = len([j for j in s['jobs'] if j['status'] == 'COMPLETED'])
            parts.append(f"{GREEN}{s['name']} floats {s['floats_done']}/{s['floats']} · jobs {done}✓{RESET}")
        else:
            parts.append(f"{GREY}{s['name']} nothing yet{RESET}")
    pkg = f"{GREY}no package{RESET}"
    if c['newest_package'] is not None:
        p = c['newest_package']
        behind = ''
        if p['commits_behind']:
            behind = f", +{p['commits_behind']} commits"
        colour = GREEN
        if p['changed'] or p['commits_behind']:
            colour = AMBER
        pkg = f"{colour}pkg {c['packages']} · drift {p['changed']}/{p['files']}{behind}{RESET}"
    tail = ''
    if stale:
        tail = f" {DIM}⟳{RESET}"
    return f"{icon} " + '  '.join(parts) + f"  {pkg}{tail}"


def main() -> None:
    event = json.load(sys.stdin)
    if not signed_in():
        print(f"{CLOUD} {AMBER}hypernym: not signed in — run: hypernym login{RESET}")
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
        print(f"{CLOUD} {GREY}hypernym: loading {key}…{RESET}")
        return
    c = json.loads(cache.read_text())
    icon = icon_flash(ZEPHYR_ICON, f"zephyr:{event['session_id']}", zephyr_signature(c), time.time())
    print(render(c, stale, icon))


if __name__ == '__main__':
    main()
