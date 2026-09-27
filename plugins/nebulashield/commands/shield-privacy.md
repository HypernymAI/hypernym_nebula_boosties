---
description: nebulashield privacy report — exactly what is stored on this machine, where, how much, and what is excluded. Nothing leaves your machine.
allowed-tools: Bash(*)
---

!`"${CLAUDE_PLUGIN_ROOT}/bin/nebulashield-run" privacy.py report`

Show the report above verbatim in a code block. Then list the controls: /nebulashield:shield-retention none|excerpt|full, /nebulashield:shield-exclude <glob>, /nebulashield:shield-include <glob>, /nebulashield:shield-export, /nebulashield:shield-purge. Do not run anything else.
