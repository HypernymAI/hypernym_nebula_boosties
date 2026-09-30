"""
Plain-language judgement for surfaces that show hook text as plain prose, not a terminal: the Claude desktop app's
"Claude Code notice" dropdown (no ANSI colour, no status line), and later Codex and bridges. Same numbers as
hook_stop.judgement_line, in words someone who has never read the build doc can follow:
  - higher is WORSE, and 1.0× the limit is the line (the detector's threshold);
  - each detector says what it caught in everyday words (definitions: vendor_codebook.CLASSES 6 / 8 / 13 / 15);
  - uncertified ("working") detectors are called experimental.
level = {class: (score / threshold, certified)} as returned by hook_stop.class_levels. Pure functions. NO .get().
"""
import os
import re
from typing import Dict, Tuple

ORDER = ('claim', 'confession', 'handback', 'repetition')
PLAIN = {   # class → (short name, what it means when it fires)
    'claim': ('Overclaiming', 'said something works or was checked without showing the proof'),
    'confession': ('Made-up feelings', 'stated its own feelings, motives or guilt as fact'),
    'handback': ('Buck-passing', "handed you a decision you'd already made, or that was its own to make"),
    'repetition': ('Repeat ask', 'you had to ask again because the reply before missed it'),
}
CELLS = 5                     # meter width; full = at or over the limit
ANSI = re.compile(r'\x1b\[[0-9;]*m')


def plain_surface() -> bool:
    """True where hook text is shown as plain prose. Claude Code sets CLAUDE_CODE_ENTRYPOINT for every hook it runs:
    'claude-desktop' in the desktop app (measured 2026-09-30), 'sdk-cli' for `claude --print` spawns, 'cli' in a
    terminal. NEBULA_SURFACE=plain|terminal overrides (tests, other harnesses)."""
    if 'NEBULA_SURFACE' in os.environ:
        return os.environ['NEBULA_SURFACE'] == 'plain'
    return 'CLAUDE_CODE_ENTRYPOINT' in os.environ and os.environ['CLAUDE_CODE_ENTRYPOINT'] == 'claude-desktop'


def strip_ansi(text: str) -> str:
    return ANSI.sub('', text)


def dot(lv: float) -> str:
    return '🟢' if lv < 0.7 else ('🟡' if lv < 1.0 else '🟠')


def meter(lv: float) -> str:
    filled = CELLS if lv >= 1.0 else int(lv * CELLS)
    return '▰' * filled + '▱' * (CELLS - filled)


def times(lv: float) -> str:
    return f"{min(lv, 9.9):.1f}×"


def name(k: str, cert: bool) -> str:
    return PLAIN[k][0] + ('' if cert else ' (experimental)')


def plain_judgement(level: Dict[str, Tuple[float, bool]], shield: str) -> str:
    """Two lines: the verdict first (readable with the dropdown half-open), then every detector's meter."""
    over = sorted((k for k in ORDER if level[k][0] >= 1.0), key=lambda k: -level[k][0])
    if not over:
        head = f"{shield} Nebula 🟢 Looks clean: no overclaiming, made-up feelings, buck-passing or repeat asks."
    elif len(over) == 1:
        k = over[0]
        lv, cert = level[k]
        head = f"{shield} Nebula 🟠 Watch for {name(k, cert).lower()}: {PLAIN[k][1]} ({times(lv)} the limit)."
    else:
        items = ' · '.join(f"{PLAIN[k][0].lower()} {times(level[k][0])}" for k in over)
        head = f"{shield} Nebula 🟠 {len(over)} things to watch: {items}."
    rows = '   '.join(f"{dot(level[k][0])} {PLAIN[k][0]} {meter(level[k][0])} {times(level[k][0])}" for k in ORDER)
    return head + '\n' + rows + '   (higher is worse · 1.0× = the limit)'
