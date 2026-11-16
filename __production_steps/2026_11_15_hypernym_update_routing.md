# Hypernym update routing

The reported Claude session loaded Hypernym 0.7.33 and treated `hypernym update` as repo-analysis help.
The current skill already defines plugin updating; old cached skill code cannot receive that behavior until
its client performs a plugin-manager update and reconnects.

Added a UserPromptSubmit router for exact update requests, an explicit Claude hypernym-update command,
and stale-skill recovery guidance. The hook only supplies context; the agent uses the supported current-client
plugin manager and retains its normal approval/trust checks. No Zephyr connection is required for this workflow.
No public release notes. No plugin cache edits or unrelated project operations.

Validation: tests/test_update_prompt.py, skill validation, hooks JSON parsing, git diff whitespace check.
TransportSession is to review and commit this update task separately using exactly:
[ hypernym update updated ]
