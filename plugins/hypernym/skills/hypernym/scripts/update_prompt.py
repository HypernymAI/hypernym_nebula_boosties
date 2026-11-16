#!/usr/bin/env python3
"""Route an explicit update request to plugin maintenance, without running an installer in a hook."""
import json
import re
import sys
from pathlib import Path


def response(event):
    if not isinstance(event, dict) or 'prompt' not in event or not isinstance(event['prompt'], str):
        return None
    if not re.fullmatch(r'\s*(?:hypernym\s+update|/hypernym:hypernym-update|/hypernym-update)\s*[.!]?\s*',
                        event['prompt'], re.IGNORECASE):
        return None
    skill = Path(__file__).resolve().parents[1] / 'SKILL.md'
    context = (
        'The user requested a Hypernym PLUGIN UPDATE. Call the hypernym_hot_update tool (no tool: run python3 '
        + str(Path(__file__).resolve().parent / 'dropship.py') + ' hot-update): it updates every client on this '
        'computer, Claude Code and Codex, brings open windows onto the new code without restarting them, and restarts '
        'the courier. Report its output, then continue any task that was in progress. If that is unavailable, read the update workflow in '
        + str(skill) + ' and execute it for the current client using its supported plugin manager. '
        'Inspect the installed source/scope first, preserve local changes and credentials, refresh the marketplace, '
        'update the installed plugin, and verify the installed version. A marketplace refresh alone is not completion. '
        'Do not run hypernym.py help/sync/load/analyze: those are repo analysis, not plugin updates. '
        'A Zephyr timeout is unrelated to plugin updating. Do not stop or resume unrelated project work. '
        'Distinguish installed code from tools still loaded in this session; explain any required reconnect. '
        'Keep normal client trust and permission checks. Do not ask what update means.'
    )
    return {'hookSpecificOutput': {'hookEventName': 'UserPromptSubmit', 'additionalContext': context}}


def main():
    try:
        result = response(json.load(sys.stdin))
    except (ValueError, OSError):
        return
    if result:
        print(json.dumps(result))


if __name__ == '__main__':
    main()
