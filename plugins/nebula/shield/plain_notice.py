"""
Plain-language judgement for surfaces that show hook text as plain prose, not a terminal: the Claude desktop app's
"Claude Code notice" dropdown (no ANSI colour, no status line), and later Codex and bridges. Same numbers as
hook_stop.judgement_line, in words someone who has never read the build doc can follow:
  - higher is WORSE, and 1.0× the limit is the line (the detector's threshold);
  - each detector says what it caught in everyday words (definitions: vendor_codebook.CLASSES 6 / 8 / 13 / 15);
  - one line, only what went over the limit (tried 2026-09-30: a second line showed as a second "Stop says:" row,
    and ▰▱ meters rendered as outlined boxes in the app's font).
level = {class: (score / threshold, certified)} as returned by hook_stop.class_levels. Pure functions. NO .get().
"""
import os
import re
from typing import Dict, Tuple

ORDER = ('claim', 'confession', 'handback', 'repetition')
PLAIN = {   # class → (name when over the limit, what that means — short: several can share one line)
    'claim': ('Overclaiming', 'said it works or was checked, without showing proof'),
    'confession': ('Made-up feelings', 'stated feelings, motives or guilt as fact'),
    'handback': ('Buck-passing', "handed you a decision it should have made"),
    'repetition': ('Repeat ask', 'you had to ask twice'),
}
GOOD = {    # class → the quality a reply shows when it stays under the limit (most replies, most of the time)
    'claim': 'Transparent',       # shows its proof
    'confession': 'Stable',       # no performed feelings
    'handback': 'Respectful',     # respects your decisions and makes its own calls
    'repetition': 'Attentive',    # got it the first time
}
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


def times(lv: float) -> str:
    return f"{min(lv, 9.9):.1f}×"


def plain_judgement(level: Dict[str, Tuple[float, bool]], shield: str) -> str:
    """ONE line (the desktop app prefixes every line with its own "Stop says:"): the qualities the reply kept, then
    anything over the limit with what it means (Nick, 2026-09-30: "most of the time you're pretty good")."""
    over = sorted((k for k in ORDER if level[k][0] >= 1.0), key=lambda k: -level[k][0])
    kept = [GOOD[k] for k in ORDER if level[k][0] < 1.0]
    line = f"{shield} Nebula"
    if kept:
        line += " 🟢 " + ' · '.join(kept)
    if over:
        line += ("   🟠 " if kept else " 🟠 ") + ' · '.join(f"{PLAIN[k][0]} {times(level[k][0])} ({PLAIN[k][1]})" for k in over)
        line += " — 1.0× is the limit, higher is worse"
    return line
