---
description: Install the shared hypernym-nebula status line (nebula + Zephyr), "remove" it, or "force" it over an unrecognized one. Backs up settings.json first.
argument-hint: [remove | force]
allowed-tools: Bash(*)
---

!`python3 "${CLAUDE_PLUGIN_ROOT}/skills/hypernym/scripts/nebula_statusline.py" $( [ "$ARGUMENTS" = "remove" ] && echo remove || { [ "$ARGUMENTS" = "force" ] && echo install --force || echo install; } )`

Show the output above verbatim. Do not run anything else.
