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
    ⢤⣶⡾ analyzing… 12/40 files ⠐ ▃▆▂█ ▆▅▃ 4m ▇▆▄ 31m   a job running: floats completed/total, clock, one power bar per
                                               running float, fastest float ETA sparkline, whole-job ETA sparkline
    ⢤⣶⡾ stopped at server gate 1 after file ranking   the server holds the job at a dry-run gate
    ⢤⣶⡾ analysis failed at float files — <error>      the newest job failed or was cancelled
    ⢤⣶⡾ analysis in use                        THIS session loaded this repo's package (~/.hypernym/sessions/<id>.json)
    ⢤⣶⡾ analysis ready · 2 commits behind      WORKS: every selected file analyzed AND the whole package auto-loaded
    ⢤⣶⡾ ⚠ alpha: analysis partial — 30/40 files   some files never analyzed (amber)
    ⢤⣶⡾ ⚠ alpha: analysis on disk, not loaded into sessions   autoload off or not current (amber)
    ⢤⣶⡾ not analyzed yet — say "hypernym go"   signed in, nothing for this repo
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

from brand import ZEPHYR_ICON, gentle_phase, icon_flash

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
            return AMBER, f"analyzing… {j['floats_completed']}/{j['floats_total']} files{live_gauges(j, time.time())}"
        return AMBER, f"analyzing… {j['stage'].lower().replace('_', ' ')} ({j['stage_number']}/11){live_gauges(j, time.time())}"
    if s['jobs']:
        newest = s['jobs'][0]
        if newest['status'].startswith('DRY_RUN_GATE'):
            return AMBER, f"stopped at server gate {newest['status'][-1]} after {newest['stage'].lower().replace('_', ' ')}"
        if newest['status'] in ('FAILED', 'CANCELLED'):
            why = ''
            if 'error' in newest:
                why = f" — {newest['error']}"
            return RED, f"analysis {newest['status'].lower()} at {newest['stage'].lower().replace('_', ' ')}{why}"
    return None


# ---- live gauges while a job runs (the Pilot's design, 2026-10-01): a braille clock ticking every second, one tiny
# power bar per running float, a sparkline of the fastest float's ETA (baby blue → deep blue) and a sparkline of the
# whole remaining job's ETA (Hypernym shiny). They MOVE only at the normal once-in-a-while moment (pilot,
# 2026-10-01: "it should only animate at the normal once in a while"): the logo's gentle flash (brand.gentle_phase —
# every 10-15 min, ~8 s). At rest the clock holds one glyph and the shine holds its colours; the values still update.
CLOCK = '⠈⠐⠠⢀⡀⠄⠂⠁'
BARS = '▁▂▃▄▅▆▇█'
BABY_BLUE, DEEP_BLUE = (150, 210, 255), (10, 40, 160)
SHINY = ((79, 133, 206), (84, 151, 219), (176, 216, 147), (189, 217, 130), (148, 218, 151), (217, 207, 111),
         (246, 187, 128), (255, 154, 139), (227, 140, 196), (139, 145, 228))


def rgb(c: tuple, text: str) -> str:
    return f"\x1b[38;2;{c[0]};{c[1]};{c[2]}m{text}{RESET}"


def lerp(a: tuple, b: tuple, t: float) -> tuple:
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


def spark(values: list) -> list:
    """Sparkline glyphs, scaled to this series' own min..max (flat series → mid bars)."""
    lo, hi = min(values), max(values)
    if hi - lo < 1e-9:
        return [BARS[3] for _ in values]
    return [BARS[round((v - lo) / (hi - lo) * (len(BARS) - 1))] for v in values]


def short_eta(seconds: float) -> str:
    if seconds < 90:
        return f"{round(seconds)}s"
    if seconds < 5400:
        return f"{round(seconds / 60)}m"
    return f"{seconds / 3600:.1f}h"


def rest_etas(history: list) -> list:
    """Remaining-job ETA at each sample from the progress rate over the samples before it (None until two samples)."""
    out = []
    for i in range(1, len(history)):
        t0, _, f0 = history[max(0, i - 6)]
        t1, _, f1 = history[i]
        if t1 > t0 and f1 > f0:
            out.append((1.0 - f1) / ((f1 - f0) / (t1 - t0)))
    return out


def live_gauges(j: dict, now: float) -> str:
    moving = gentle_phase(now, 1.0)[0] == 'shiny'
    parts = [rgb((200, 200, 200), CLOCK[int(now) % len(CLOCK)] if moving else CLOCK[0])]
    if 'active' in j and j['active']:
        bars = ''.join(rgb(lerp((90, 170, 110), (120, 255, 150), a['overall']),
                           BARS[min(len(BARS) - 1, int(a['overall'] * len(BARS)))]) for a in j['active'])
        parts.append(bars)
    if 'history' in j:
        fast = [h[1] for h in j['history'] if h[1] is not None]
        if len(fast) >= 2:
            glyphs = spark(fast)
            n = len(glyphs)
            parts.append(''.join(rgb(lerp(BABY_BLUE, DEEP_BLUE, i / max(1, n - 1)), g) for i, g in enumerate(glyphs))
                         + rgb(DEEP_BLUE, f" {short_eta(fast[-1])}"))
        rest = rest_etas(j['history'])
        if len(rest) >= 2:
            glyphs = spark(rest)
            shift = int(now * 3) if moving else 0
            parts.append(''.join(rgb(SHINY[(i + shift) % len(SHINY)], g) for i, g in enumerate(glyphs))
                         + rgb(SHINY[shift % len(SHINY)], f" {short_eta(rest[-1])}"))
    return ' ' + ' '.join(parts)


def package_state(p: dict, used: bool) -> str:
    """Green "analysis ready" ONLY when it works properly: every selected file was analyzed AND the whole package is
    in context (autoload import current). Anything less is an amber alpha warning naming what is missing (the Pilot,
    2026-10-01). Commits behind HEAD is shown as information, not as a failure. Caches written before these fields
    existed fall back to the plain old wording."""
    behind = ''
    if p['commits_behind']:
        behind = f" · {p['commits_behind']} commit{'s' if p['commits_behind'] != 1 else ''} behind"
    if 'floats_completed' in p and p['floats_completed'] < p['files_selected']:
        return (f"{AMBER}⚠ alpha: analysis partial — {p['floats_completed']}/{p['files_selected']} files"
                f"{behind}{RESET}")
    if 'autoload' in p and not p['autoload']:
        return f"{AMBER}⚠ alpha: analysis on disk, not loaded into sessions{behind}{RESET}"
    if used:
        return f"{GREEN}analysis in use{RESET}{DIM}{behind}{RESET}"
    return f"{GREEN}analysis ready{RESET}{DIM}{behind}{RESET}"


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
        if c['newest_package'] is not None:
            parts.append(package_state(c['newest_package'], in_use(session_id, key)))
        else:
            parts.append(f"{GREY}not analyzed yet — say \"hypernym go\"{RESET}")
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
