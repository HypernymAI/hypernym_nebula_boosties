---
description: Install the shared hypernym-nebula status line (nebula + Zephyr), "remove" it, or "force" it over an unrecognized one. Backs up settings.json first.
argument-hint: [remove | force]
allowed-tools: mcp__plugin_hypernym_hypernym__hypernym_statusline
---

The user's argument to this command: `$ARGUMENTS`
- empty → call the hypernym MCP tool `hypernym_statusline` with `action: "install"`
- `remove` or `force` → call `hypernym_statusline` with that `action`
- anything else → say this command takes `remove`, `force` or nothing, and call nothing.
Show the tool's output verbatim. Do not run anything else.
