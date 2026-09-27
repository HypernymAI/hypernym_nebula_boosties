#!/usr/bin/env python3
"""
SessionStart hook: keep the shared hypernym-nebula status line current.

nebula_statusline.auto() refreshes ~/.hypernym-nebula/bin and sets statusLine only when there is none or it is one
of ours (settings.json backed up first; never a foreign one; never after `remove`). Hooks must print nothing.
NO .get().
"""
import sys

import nebula_statusline


def main() -> None:
    sys.stdin.read()
    nebula_statusline.auto()


if __name__ == '__main__':
    main()
