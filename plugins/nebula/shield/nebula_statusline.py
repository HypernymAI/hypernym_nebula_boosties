#!/usr/bin/env python3
"""
hypernym-nebula SHARED status line — byte-identical in BOTH plugins of the hypernym-nebula marketplace, together with
the Hypernym logo code it draws (SHARED below: brand.py, termbg.py, brand_assets/wings_candidates.json):
  nebula  plugin/shield/
  hypernym      skills/hypernym/scripts/
Standard library only. No network. It never imports either plugin; it only RUNS their status-line programs.

    nebula_statusline.py compose             the status line itself (stdin: Claude Code's status-line JSON)
    nebula_statusline.py refresh             SessionStart of either plugin: copy this file + write the launcher
    nebula_statusline.py auto                SessionStart: set statusLine if there is none or it is one of ours
    nebula_statusline.py install [--force]   set statusLine (settings.json backed up first); --force replaces a foreign one
    nebula_statusline.py remove              take our statusLine out; not re-added automatically until `install`
    nebula_statusline.py peers <plugin>      is the other plugin installed? (+ the line that installs it)

Which plugins are installed is read from Claude Code's OWN registry on every refresh —
(CLAUDE_CONFIG_DIR | ~/.claude)/plugins/installed_plugins.json (installPath) + enabledPlugins in the settings — because
uninstalling a plugin leaves its files on disk (measured, build doc S35-pre). So install / uninstall / disable of either
plugin, in any order, shows up within one refresh.

Rows (S39): whenever EITHER plugin is installed, the Hypernym braille logo (3 rows, gentle shine, alpha against the
terminal's real background — or fully OPAQUE where that background cannot be read: any terminal but Apple Terminal,
Linux; nothing is blended then, so no colour is guessed) is drawn at the left; the MIDDLE row carries nebula's judgement row (if installed)
and the wings + HYPERNYM.ai wordmark; hypernym's Zephyr line rides the LAST row. Brand settings come from nebula's
settings when present (~/.nebula/config.json → brand), else BRAND_DEFAULTS (= nebula's shipped defaults).
A part that fails shows "✖ <plugin>: <reason>" — never a blank line.
NO .get(). Imports at top.
"""
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from brand import icon, logo, wings_icon
from termbg import background

VERSION = 5                                   # a newer copy is never overwritten by an older one (refresh)
MARKETPLACE = 'hypernym-nebula'
ORDER = ('nebula', 'hypernym')          # row order: shield rows, then the Zephyr line
NICE = {'nebula': 'nebula (local offline shield)', 'hypernym': 'hypernym (Zephyr connector)'}
HOME_DIR = Path.home() / '.hypernym-nebula'
BIN = HOME_DIR / 'bin'
COPY = BIN / 'nebula_statusline.py'
LAUNCHER = BIN / 'statusline'
OFF = HOME_DIR / 'statusline_off'             # written by `remove`: SessionStart will not re-add the status line
TIMEOUT_S = 4.0
SHARED = ('nebula_statusline.py', 'brand.py', 'termbg.py', 'brand_assets/wings_candidates.json')
BRAND_DEFAULTS = {'enabled': True, 'bg': 'auto', 'icons': True, 'mark': 'mid', 'mode': 'gentle', 'rest_alpha': 0.1,
                  'wings': 76}
SHIELD_CONFIG = Path.home() / '.nebula' / 'config.json'
RECOGNIZED = ('.hypernym-nebula/bin/statusline',  # ours
              '.hypernym/bin/statusline', 'compose_statusline.py',    # hypernym ≤ 0.4 composer
              'ctxshield-badge', 'shield_badge.py')                   # ctxshield ≤ 0.2 badge / hand-made one-liner


def config_dir() -> Path:
    if 'CLAUDE_CONFIG_DIR' in os.environ and os.environ['CLAUDE_CONFIG_DIR']:
        return Path(os.environ['CLAUDE_CONFIG_DIR'])
    return Path.home() / '.claude'


def settings_path() -> Path:
    return config_dir() / 'settings.json'


def load_json(p: Path) -> dict:
    if not p.exists():
        return {}
    d = json.loads(p.read_text())
    if not isinstance(d, dict):
        raise ValueError(f"{p} must hold a JSON object")
    return d


# ---------------------------------------------------------------------------------------------------- the registry
def enabled(key: str, project_dir: str) -> bool:
    """The most specific settings file that mentions the plugin decides (project local → project → user)."""
    files = []
    if project_dir:
        files = [Path(project_dir) / '.claude' / 'settings.local.json', Path(project_dir) / '.claude' / 'settings.json']
    for p in files + [settings_path()]:
        s = load_json(p)
        if 'enabledPlugins' in s and key in s['enabledPlugins']:
            return s['enabledPlugins'][key] is True
    return False


def installed(name: str, project_dir: str):
    """(install path, enabled) of <name>@hypernym-nebula for this project, or None when it is not installed."""
    reg = load_json(config_dir() / 'plugins' / 'installed_plugins.json')
    key = f'{name}@{MARKETPLACE}'
    if 'plugins' not in reg or key not in reg['plugins']:
        return None
    for entry in reg['plugins'][key]:
        if entry['scope'] == 'user' or ('projectPath' in entry and project_dir
                                        and Path(entry['projectPath']).resolve() == Path(project_dir).resolve()):
            return Path(entry['installPath']), enabled(key, project_dir)
    return None


def shield_ready(root: Path) -> bool:
    """nebula's SessionStart marks its own install directory once its Python environment is built."""
    return (root / '.venv' / '.nebula_ready').exists()


def part_argv(name: str, root: Path) -> list:
    if name == 'nebula':
        return [str(root / 'bin' / 'nebula-run'), 'shield_badge.py', '--row']
    return [sys.executable, str(root / 'skills' / 'hypernym' / 'scripts' / 'statusline.py')]


def project_of(event: str) -> str:
    e = json.loads(event) if event.strip() else {}
    if 'workspace' in e and isinstance(e['workspace'], dict) and 'project_dir' in e['workspace']:
        return e['workspace']['project_dir']
    if 'cwd' in e:
        return e['cwd']
    return ''


# ---------------------------------------------------------------------------------------------------- compose
def last_words(text: str) -> str:
    lines = [ln.strip() for ln in re.sub(r'\x1b\[[0-9;]*m', '', text).splitlines() if ln.strip()]
    return lines[-1][:160] if lines else 'exited with an error and no message'


def compose(event: str) -> list:
    proj = project_of(event)
    active = []
    for name in ORDER:
        found = installed(name, proj)
        if found is not None and found[1]:
            active.append((name, found[0]))
    if not active:
        off = [n for n in ORDER if installed(n, proj) is not None]
        if off:
            return [f"\x1b[2mhypernym-nebula: {' and '.join(off)} installed but disabled — "
                    f"claude plugin enable {off[0]}@{MARKETPLACE}\x1b[0m"]
        return [f"\x1b[2mhypernym-nebula: neither plugin is installed — remove this status line: "
                f"python3 {COPY} remove\x1b[0m"]
    out, procs = {}, {}
    for name, root in active:
        if name == 'nebula' and not shield_ready(root):
            chk = subprocess.run([str(root / 'bin' / 'nebula-run'), '--check'], capture_output=True, text=True,
                                 timeout=TIMEOUT_S)
            out[name] = (["\x1b[2m⋯ nebula: finishing setup at session start (builds its local Python environment once)\x1b[0m"]
                         if chk.returncode == 0 else [f"✖ {last_words(chk.stderr)}"])
            continue
        argv = part_argv(name, root)
        if not Path(argv[0] if name == 'nebula' else argv[1]).exists():
            out[name] = [f"✖ {name}: {argv[0] if name == 'nebula' else argv[1]} is missing — reinstall the plugin"]
            continue
        p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                             env=dict(os.environ, CLAUDE_PLUGIN_ROOT=str(root)), start_new_session=True)
        p.stdin.write(event)                      # small (a few KB): fits the pipe, so both parts start at once
        p.stdin.close()
        procs[name] = p
    deadline = time.monotonic() + TIMEOUT_S
    for name, p in procs.items():
        try:
            so = p.stdout.read() if p.wait(timeout=max(0.05, deadline - time.monotonic())) == 0 else None
            if so is None:
                msg = last_words(p.stderr.read())
                out[name] = [f"✖ {msg}" if msg.startswith(name) else f"✖ {name}: {msg}"]
            else:
                rows = [ln for ln in so.splitlines() if ln.strip()]
                out[name] = rows if rows else [f"✖ {name}: printed nothing"]
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGTERM)       # the whole part (uv → python), so a hang never piles up
            out[name] = [f"✖ {name}: no answer within {TIMEOUT_S:.0f} s"]
    return layout(out['nebula'] if 'nebula' in out else [],
                  '  '.join(out['hypernym']) if 'hypernym' in out else '', time.time())


def brand_cfg() -> dict:
    c = load_json(SHIELD_CONFIG)
    return {**BRAND_DEFAULTS, **(c['brand'] if 'brand' in c else {})}


def plain(shield: list, zephyr: str) -> list:
    rows = list(shield)
    if zephyr:
        if rows:
            rows[-1] = f"{rows[-1]}  {zephyr}"
        else:
            rows = [zephyr]
    return rows


def layout(shield: list, zephyr: str, now: float) -> list:
    b = brand_cfg()
    if b['enabled'] is not True:
        return plain(shield, zephyr)
    bg, alpha, mode = None, b['rest_alpha'], b['mode']
    if b['bg'] == 'auto':
        try:
            bg = background(HOME_DIR / 'termbg.json')              # the REAL background of this tab (S26e)
        except RuntimeError:                                       # unreadable here (not Apple Terminal, Linux …):
            bg, alpha = (0, 0, 0), 1.0                             # draw OPAQUE — at alpha 1 nothing is blended, so
            mode = 'gentle' if mode == 'cameo' else mode           # no colour is guessed (cameo's fade needs a bg)
    else:
        bg = tuple(b['bg'])
    rows, word = logo(now, bg, mode, alpha, b['mark'])
    if b['icons'] is True:
        word = icon(wings_icon(b['wings'])) + ' ' + word          # the chosen wings (contact sheet #)
    k = (len(rows) - 1) // 2                                       # mid 3 rows → 1 · compact 1 → 0
    lines = list(rows)
    lines[k] = '  '.join([lines[k]] + list(shield) + [word])
    if zephyr:
        lines[-1] = f"{lines[-1]}  {zephyr}"
    return lines


# ---------------------------------------------------------------------------------------------------- refresh
def version_of(p: Path) -> int:
    m = re.search(r'^VERSION = (\d+)', p.read_text(), re.M) if p.exists() else None
    return int(m.group(1)) if m else -1


def refresh() -> None:
    """Copy the SHARED files to ~/.hypernym-nebula/bin (never over a newer set) and write the launcher (if changed)."""
    BIN.mkdir(parents=True, exist_ok=True)
    here = Path(__file__).resolve().parent
    if version_of(COPY) <= VERSION:
        for rel in SHARED:
            src, dst = here / rel, BIN / rel
            if not dst.exists() or dst.read_bytes() != src.read_bytes():
                dst.parent.mkdir(parents=True, exist_ok=True)
                tmp = dst.with_name(dst.name + '.tmp')
                shutil.copyfile(src, tmp)
                tmp.replace(dst)
    text = ("#!/bin/sh\n"
            "# hypernym-nebula status line — written by the nebula / hypernym plugins on session start.\n"
            "# Shows whichever of the two is installed (read from Claude Code's plugin registry every refresh).\n"
            f'exec python3 "{COPY}" compose\n')
    if not LAUNCHER.exists() or LAUNCHER.read_text() != text:
        tmp = LAUNCHER.with_suffix('.tmp')
        tmp.write_text(text)
        tmp.replace(LAUNCHER)
    LAUNCHER.chmod(0o755)


# ---------------------------------------------------------------------------------------------------- settings.json
def current_command(s: dict):
    if 'statusLine' in s and isinstance(s['statusLine'], dict) and 'command' in s['statusLine']:
        return s['statusLine']['command']
    return None


def recognized(cmd) -> bool:
    return cmd is not None and any(m in cmd for m in RECOGNIZED)


def write_settings(s: dict) -> str:
    """Backup (when the file exists), then an atomic replace; the new file must parse before it replaces the old one."""
    p = settings_path()
    note = ''
    if p.exists():
        b = p.with_name(f"settings.json.backup_nebula_{time.strftime('%Y%m%d_%H%M%S')}_{os.getpid()}")
        shutil.copy2(p, b)
        note = f"backup: {b}"
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name('settings.json.nebula_tmp')
    tmp.write_text(json.dumps(s, indent=2) + '\n')
    json.loads(tmp.read_text())
    tmp.replace(p)
    return note


def ours() -> dict:
    return {'type': 'command', 'command': str(LAUNCHER), 'refreshInterval': 1}


def install(force: bool) -> int:
    refresh()
    s = load_json(settings_path())
    cur = current_command(s)
    if 'statusLine' in s and not recognized(cur) and not force:
        print(f"Not replacing your existing status line: {json.dumps(s['statusLine'])}\n"
              f"Run with --force to replace it (settings.json is backed up first).")
        return 1
    if OFF.exists():
        OFF.unlink()
    if 'statusLine' in s and s['statusLine'] == ours():
        print(f"The hypernym-nebula status line is already set: {LAUNCHER}")
        return 0
    s['statusLine'] = ours()
    note = write_settings(s)
    print('\n'.join(x for x in (note, f"status line set: {LAUNCHER} (refreshes every second)") if x))
    return 0


def auto() -> str:
    """SessionStart: set our status line when there is none, or when it is one of ours / an older version of ours.
    Never a foreign one, never after `remove`. Returns what it did ('' = nothing) — hooks must not print."""
    refresh()
    if OFF.exists():
        return ''
    s = load_json(settings_path())
    if 'statusLine' in s and (not recognized(current_command(s)) or s['statusLine'] == ours()):
        return ''
    s['statusLine'] = ours()
    return write_settings(s) or 'status line set'


def remove() -> int:
    s = load_json(settings_path())
    if current_command(s) != str(LAUNCHER):
        print(f"The status line is not hypernym-nebula's — left unchanged: "
              f"{json.dumps(s['statusLine']) if 'statusLine' in s else '(none)'}")
        return 0
    del s['statusLine']
    note = write_settings(s)
    HOME_DIR.mkdir(parents=True, exist_ok=True)
    OFF.write_text("statusLine removed by the user; `install` re-adds it\n")
    print('\n'.join(x for x in (note, "hypernym-nebula status line removed (not re-added automatically)") if x))
    return 0


# ---------------------------------------------------------------------------------------------------- peers
def peers(me: str) -> str:
    other = [n for n in ORDER if n != me][0]
    found = installed(other, os.getcwd())
    line = f"claude plugin install {other}@{MARKETPLACE}   (or in Claude Code: /plugin install {other}@{MARKETPLACE})"
    if found is None:
        return f"{NICE[other]}: not installed — {line}"
    if not found[1]:
        return f"{NICE[other]}: installed but disabled — claude plugin enable {other}@{MARKETPLACE}"
    return f"{NICE[other]}: installed — its line shares this status bar"


def main() -> int:
    a = sys.argv[1:]
    if a == ['compose']:
        print('\n'.join(compose(sys.stdin.read())))
        return 0
    if a == ['refresh']:
        refresh()
        return 0
    if a == ['auto']:
        auto()
        return 0
    if a in (['install'], ['install', '--force']):
        return install(a == ['install', '--force'])
    if a == ['remove']:
        return remove()
    if len(a) == 2 and a[0] == 'peers' and a[1] in ORDER:
        print(peers(a[1]))
        return 0
    sys.exit(__doc__)


if __name__ == '__main__':
    sys.exit(main())
