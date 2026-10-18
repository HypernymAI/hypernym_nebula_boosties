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
    ⢤⣶⡾ top up to keep going — portal.hypernym.ai/plan   the Zephyr asked for credit (402 insufficient_credit)
    ⢤⣶⡾ ranking files                          a job before its files start: a plain word per stage, no counter
    ⢤⣶⡾ [ analyzing 15/37 files 41% ] ⣀⣤⣶⣿⣦⣀⣄⣤   files running: the row's background fills with a lighter blue
                                               (the tide) as files finish; one braille column per running file,
                                               its height that file's real progress. Percent only — never a time.
    ⢤⣶⡾ finishing up                           the stages after the files
    ⢤⣶⡾ analysis paused at a server checkpoint  the server holds the job at a dry-run gate
    ⢤⣶⡾ ready for a fresh run — say "hypernym go"   the newest job did not finish (exact reason: `hypernym help`)
    ⢤⣶⡾ analysis in use — 40 files             THIS session loaded this repo's package (~/.hypernym/sessions/<id>.json)
    ⢤⣶⡾ analysis complete — 40 files           every selected file analyzed AND the whole package auto-loaded
    ⢤⣶⡾ alpha · analysis complete — 38 files   some selected files not analyzed; it works (sky blue)
    ⢤⣶⡾ alpha · analysis downloaded — loads in your next session   autoload not current yet (sky blue)
    ⢤⣶⡾ not analyzed yet — say "hypernym go"   signed in, nothing for this repo
    not a git repo / no github remote          as before
Server states show in sky blue with a slow clock — never hidden, never alarming, never an error code:
    ⢤⣶⡾ Zephyr is on its way back — patience ⠐     HTTP 502/503/504
    ⢤⣶⡾ reconnecting to Zephyr — patience ⠐        no connection
    ⢤⣶⡾ Zephyr is catching its breath — patience ⠐ any other 5xx or unexpected answer
NOTHING in this row is ever red or amber, and nothing tells the customer that anything is wrong (the Pilot,
2026-10-02). Every state is sky blue, green or grey; exact server errors live only in `hypernym help`.

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
GREEN, GREY = '\x1b[38;2;120;200;140m', '\x1b[38;2;110;110;110m'  # no red, no amber — ever (the Pilot, 2026-10-02)
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


# Transient server states are not the user's problem to fix: a restart (502/503/504), a server hiccup (any other
# 5xx) or a dropped connection clear on their own. They show in sky blue with a slow clock, never red (the Pilot,
# 2026-10-01: "502 looks terrible in red — sky blue for warnings, patience"; 2026-10-02: no red or amber anywhere).
SKY = '\x1b[38;2;64;180;255m'  # saturated sky blue: LightSkyBlue (135,206,250) read as white on dark terminals


def patience_line(error: str, now: float):
    """Sky-blue wording for a transient server state, or None for any other answer (that gets the calm catch-all)."""
    tick = CLOCK[int(now) % len(CLOCK)]
    if error in ('HTTP 502', 'HTTP 503', 'HTTP 504'):
        return f"Zephyr is on its way back — patience {tick}"
    if error == 'unreachable':
        return f"reconnecting to Zephyr — patience {tick}"
    if error.startswith('HTTP 5'):
        return f"Zephyr is catching its breath — patience {tick}"
    return None


def server_state(s: dict):
    """(colour, text) for one server, or None when it has nothing of its own to say (then the repo's state shows)."""
    if s['error'] == 'HTTP 401':
        return SKY, LOGIN
    if s['error'] == 'HTTP 402':
        return SKY, 'top up to keep going — portal.hypernym.ai/plan'
    if s['error']:
        patience = patience_line(s['error'], time.time())
        if patience is not None:
            return SKY, patience
        # Any other server answer is not something to read raw in a status bar (the Pilot, 2026-10-01: "map the
        # error messages to chill messages"). The exact text stays in the cache file and `hypernym help`.
        return SKY, f"Zephyr is catching its breath — patience {CLOCK[int(time.time()) % len(CLOCK)]}"
    running = [j for j in s['jobs'] if j['status'] == 'PROCESSING']
    if running:
        j = running[0]
        return SKY, analyzing_line(j, time.time())
    if s['jobs']:
        newest = s['jobs'][0]
        if newest['status'].startswith('DRY_RUN_GATE'):
            return SKY, "analysis paused at a server checkpoint — it continues when the server opens it"
        if newest['status'] == 'FAILED':
            # Never the raw exception here — calm wording; `hypernym help` shows the exact server error.
            return SKY, "ready for a fresh run — say \"hypernym go\""
        if newest['status'] == 'CANCELLED':
            return SKY, "ready when you are — say \"hypernym go\""
    return None


# ---- the analyzing line (the Pilot, 2026-10-02: "subtle same chill blue … waves gently lapping and it being gently
# filled up with lighter background color"; "no time estimation please just status %"; "gentle motions for each, they
# take forever, be really really gentle"). The row's own background fills with a lighter blue as files finish (the
# tide); its edge breathes once every BREATH_S seconds. Each running file shows one braille column whose height is its
# real progress, so it only rises when that file's trials do. No stage counter, no ETA, nothing red or amber.
CLOCK = '⠈⠐⠠⢀⡀⠄⠂⠁'
WATER = '⣀⣄⣤⣦⣶⣷⣿'
SKY_RGB = (64, 180, 255)
TIDE_BG, TIDE_EDGE_BG = (26, 50, 66), (38, 70, 90)
SHALLOW, DEEP = (95, 150, 190), (150, 215, 255)
BREATH_S = 4
STAGE_WORDS = {
    'REPO_FETCH': 'fetching repo', 'BM25_BUILD': 'indexing', 'CALLGRAPH_PAGERANK': 'mapping the code',
    'FILE_RANKING': 'ranking files', 'FLOAT_FILES': 'starting files', 'AWAIT_FLOATS': 'analyzing files',
    'EARTH_CONFIG': 'finishing up', 'NAVIGATOR': 'finishing up', 'FACT_EXTRACTION': 'finishing up',
    'PACK_FACTS': 'finishing up', 'TREATMENT_PROMPT': 'finishing up',
}


def rgb(c: tuple, text: str) -> str:
    return f"\x1b[38;2;{c[0]};{c[1]};{c[2]}m{text}{RESET}"


def lerp(a: tuple, b: tuple, t: float) -> tuple:
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


def tide(text: str, fraction: float, now: float) -> str:
    """`text` in sky blue with its background filled to `fraction` in a lighter blue. The cell just past the
    waterline takes a slightly brighter background on every other BREATH_S beat — the only motion."""
    filled = round(len(text) * fraction)
    breathing = int(now // BREATH_S) % 2 == 0
    fg = f"\x1b[38;2;{SKY_RGB[0]};{SKY_RGB[1]};{SKY_RGB[2]}m"
    out = ''
    for i, ch in enumerate(text):
        bg = ''
        if i < filled:
            bg = f"\x1b[48;2;{TIDE_BG[0]};{TIDE_BG[1]};{TIDE_BG[2]}m"
        elif i == filled and breathing and 0 < fraction < 1:
            bg = f"\x1b[48;2;{TIDE_EDGE_BG[0]};{TIDE_EDGE_BG[1]};{TIDE_EDGE_BG[2]}m"
        out += f"{bg}{fg}{ch}{RESET}"
    return out


def water(active: list) -> str:
    """One braille column per running file, its height and depth of blue = that file's own progress (0..1)."""
    cols = ''
    for a in active:
        level = min(len(WATER) - 1, int(a['overall'] * len(WATER)))
        cols += rgb(lerp(SHALLOW, DEEP, a['overall']), WATER[level])
    return cols


def analyzing_line(j: dict, now: float) -> str:
    word = 'analyzing'
    if j['stage'] in STAGE_WORDS:
        word = STAGE_WORDS[j['stage']]
    if j['floats_total'] is None or j['floats_total'] == 0:
        return f"{SKY}{word}{RESET}"
    done, total = j['floats_completed'], j['floats_total']
    line = tide(f" analyzing {done}/{total} files {round(100 * done / total)}% ", done / total, now)
    if 'active' in j and j['active']:
        line += ' ' + water(j['active'])
    return line


def package_state(p: dict, used: bool) -> str:
    """The analysis, said positively (the Pilot, 2026-10-02: "if it's bad news it just needs to be alpha sky blue,
    'analysis complete' and just put 38 files … no commits behind … stop being negative"):
      green     "analysis complete — 40 files"   every selected file analyzed, loading into every session
      green     "analysis in use — 40 files"     this session loaded it itself
      sky blue  "alpha · analysis complete — 38 files"   some selected files were not analyzed; it still works
      sky blue  "alpha · analysis downloaded — loads in your next session"   autoload not current yet
    Never commits behind (the session-start note tells the model which files changed). Caches written before
    floats_completed existed show no count."""
    files = ''
    if 'floats_completed' in p:
        files = f" — {p['floats_completed']} files"
    if 'autoload' in p and not p['autoload']:
        return f"{SKY}alpha · analysis downloaded — loads in your next session{RESET}"
    if 'floats_completed' in p and p['floats_completed'] < p['files_selected']:
        return f"{SKY}alpha · analysis complete{files}{RESET}"
    if used:
        return f"{GREEN}analysis in use{files}{RESET}"
    return f"{GREEN}analysis complete{files}{RESET}"


def render(c: dict, session_id: str, key: str, stale: bool, icon: str) -> str:
    many = len(c['servers']) > 1
    parts = []
    if CREDIT_REFUSED.exists():
        parts.append(f"{SKY}top up to keep going — portal.hypernym.ai/plan{RESET}")
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
        print(f"{CLOUD} {SKY}{LOGIN}{RESET}")
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
        # First tick in a repo: say something useful right away, not an ellipsis, while the refresh runs.
        print(f"{CLOUD} {SKY}checking Zephyr {CLOCK[int(time.time()) % len(CLOCK)]}{RESET}")
        return
    try:
        c = json.loads(cache.read_text())
    except json.JSONDecodeError:
        # Damaged cache: the next refresh rewrites it; until then say so plainly, never a traceback.
        print(f"{CLOUD} {SKY}checking Zephyr {CLOCK[int(time.time()) % len(CLOCK)]}{RESET}")
        return
    icon = icon_flash(ZEPHYR_ICON, f"zephyr:{event['session_id']}", zephyr_signature(c), time.time())
    print(render(c, event['session_id'], key, stale, icon))


if __name__ == '__main__':
    main()
