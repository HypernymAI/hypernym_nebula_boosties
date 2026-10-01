#!/usr/bin/env python3
"""
SessionStart hook (startup, resume, clear, compact): tell the agent, in a few lines, what Hypernym holds for the repo
it was launched in, so a brand-new session or one that was just compacted knows the package exists without anyone
having to say "hypernym help".

What it prints is the agent's context (plain stdout, the one form both Claude Code and Codex accept for SessionStart):
  - the newest package for this repo: job, commit, how many commits behind HEAD, the question it answered, its size
  - drift: which packaged files differ from the working tree (read those directly; the package describes old versions)
  - that loading is the AGENT's job: load it yourself when the work touches this repo's code (the full package is ~80K
    tokens — too much for a hook to inject, and wasted on unrelated work); the user never runs load/sync/wait
  - with no package: offer an analysis only when the task is about this repo's code (the approval dialog is the user's
    only decision)
  - after a compaction (source=compact): ONLY if this session had loaded a package (~/.hypernym/sessions/<session_id>.json,
    written by record_load.py) — "load it again now", with the exact command; otherwise nothing
  - AUTOLOAD (default; HYPERNYM_AUTOLOAD=0 turns it off): the newest package is imported in full by <repo>/CLAUDE.local.md
    (hypernym.install_autoload), so every session starts with all of it and compaction re-injects all of it. Then the
    startup line says it is already in context, and after a compaction the user sees one line:
    "Hypernym analysis reloaded after compaction — job …, N tokens, commit …"

Local only: reads ~/.hypernym and runs git. Never calls the network, never submits anything. Prints nothing outside a
GitHub repo, and nothing when HYPERNYM_SESSION_CONTEXT=0. NO .get().
"""
import json
import os
import sys
import time
from pathlib import Path

import hypernym

HERE = Path(__file__).resolve().parent
MAX_CHARS = 6000          # Claude Code caps hook context at 10,000 characters; Codex at ~2,500 tokens by default
MAX_LISTED = 15


def load_command(cwd: str) -> str:
    return f'python3 "{HERE / "hypernym.py"}" --dir "{cwd}" load'


INVITED = hypernym.HOME / 'invited'
START_PHRASES = ('"hypernym go", "hypernym analyze", "hypernym do it", or "yes" to your Hypernym offer')


def first_time_here(info: dict) -> bool:
    """True the first time a session starts in this repo on this computer (the invite is shown once per repo)."""
    marker = INVITED / info['key_lower'].replace('/', '__')
    if marker.exists():
        return False
    INVITED.mkdir(parents=True, exist_ok=True)
    marker.write_text(str(time.time()))
    return True


def package_lines(info: dict, cwd: str, source: str) -> list:
    pkgs = hypernym.list_packages(info)
    if not pkgs:
        if not hypernym.load_servers():
            return []
        start = (f"When the user says {START_PHRASES}, start it right away: call the hypernym_analyze tool with "
                 "execute=true (pass their current task as the question if they described one; otherwise omit it and "
                 "the whole repo is analyzed). Don't ask them anything first: the approval dialog shows the cost and is "
                 "their only decision. After Submit, tell them in one line that it started and you'll keep working and "
                 "switch to it when it's ready; after Cancel, one line that nothing was sent.")
        if source == 'startup' and first_time_here(info):
            return [f"Hypernym: {info['key']} hasn't been analyzed yet (first session here). In your first reply, add "
                    "ONE short line inviting the user, then carry on with what they asked: \"Hypernym can analyze this "
                    "repo so I work from a compressed map of the code that matters. Say "
                    "'hypernym go' to start.\"", start]
        return [f"Hypernym: {info['key']} hasn't been analyzed yet. Don't bring it up unless the task is about this "
                "repo's code.", start]
    pkg = pkgs[0]
    m = json.loads((pkg / 'manifest.json').read_text())
    d = hypernym.drift(info, m)
    differ = d['changed'] + d['missing']
    behind = 'an unknown number of commits'
    if d['commits_behind'] is not None:
        behind = f"{d['commits_behind']} commit"
        if d['commits_behind'] != 1:
            behind += 's'
    if hypernym.autoload_enabled():
        if hypernym.autoload_current(info, pkg):
            head = (f"Hypernym: this repo's full analysis is already in your context — imported in full through "
                    f"CLAUDE.local.md (@{hypernym.LOCAL_COPY.as_posix()}). Use it; do not run load, and never ask the "
                    "user to.")
        else:
            hypernym.install_autoload(info, pkg)
            head = (f"Hypernym: this repo's newest analysis was just set up to load in full in every session here "
                    f"(CLAUDE.local.md imports {hypernym.LOCAL_COPY.as_posix()}), but this session started before it. "
                    f"Read {info['root'] / hypernym.LOCAL_COPY} in full now, before replying (every chunk; it is "
                    f"{m['metadata']['prompt_tokens']} tokens). From the next session and after any compaction it "
                    "is in context automatically.")
    else:
        head = (f"Hypernym: an analysis for {info['key']} is ready on this computer. Load it yourself when the work "
                f"touches this repo's code (loading sends nothing anywhere; never ask the user to run load, sync or "
                f"wait), and tell them in one line: \"The Hypernym analysis for this repo is ready — I'm using it now.\" "
                f"{load_command(cwd)}")
    lines = [
        head,
        f"Newest: job {m['job_id']} from {m['server']}, commit {m['commit'][:12]} ({behind} behind HEAD), "
        f"completed {m['completed_at_utc']}, {m['metadata']['prompt_tokens']} tokens covering "
        f"{m['metadata']['files_selected']} files.",
    ]
    if m['question']:
        lines.append(f"It answers: {m['question'][:300]}")
    if differ:
        lines.append(f"Drift: {len(differ)} of {d['files']} packaged files differ from the working tree; the package "
                     "describes their older versions, so read these directly:")
        for path in differ[:MAX_LISTED]:
            if path in d['missing']:
                lines.append(f"  - {path} (deleted)")
            else:
                lines.append(f"  - {path}")
        if len(differ) > MAX_LISTED:
            lines.append(f"  - … and {len(differ) - MAX_LISTED} more (`hypernym packages` lists them all)")
    else:
        lines.append(f"Drift: none; all {d['files']} packaged files match the working tree.")
    if d['unknown']:
        lines.append(f"({d['unknown']} packaged files have no baseline hash, so their drift is unknown.)")
    if len(pkgs) > 1:
        lines.append(f"{len(pkgs) - 1} older package(s) for this repo are also on disk (`hypernym packages`).")
    return lines


def reload_lines(payload: dict, info: dict, cwd: str) -> list:
    """After a compaction: only when THIS session had loaded a package for this repo, and it is still on disk."""
    rec = hypernym.SESSIONS / f"{payload['session_id']}.json"
    if not rec.exists():
        return []
    r = json.loads(rec.read_text())
    if r['repo'] != info['key_lower'] or not Path(r['package']).exists():
        return []
    return [f"Hypernym: you had this repo's analysis loaded in this session and compaction removed it from context. "
            f"Load it again now (the user does nothing): {load_command(cwd)} --package \"{r['package']}\""]


def main() -> None:
    payload = json.loads(sys.stdin.read())
    if 'HYPERNYM_SESSION_CONTEXT' in os.environ:
        if os.environ['HYPERNYM_SESSION_CONTEXT'] == '0':
            return
    cwd = payload['cwd']
    try:
        info = hypernym.repo_info(cwd)
    except SystemExit:
        return  # not a git repo, or not a GitHub one: Hypernym has nothing to say here
    if payload['source'] == 'compact':
        pkgs = hypernym.list_packages(info)
        if pkgs and hypernym.autoload_enabled() and hypernym.autoload_current(info, pkgs[0]):
            # The import already put the whole package back after the summary; say so to the user, without noise.
            m = json.loads((pkgs[0] / 'manifest.json').read_text())
            print(json.dumps({
                'systemMessage': (f"Hypernym analysis reloaded after compaction — job {m['job_id'][:13]}…, "
                                  f"{m['metadata']['prompt_tokens']:,} tokens, commit {m['commit'][:7]}"),
                'hookSpecificOutput': {
                    'hookEventName': 'SessionStart',
                    'additionalContext': ("Hypernym: compaction kept this repo's full analysis — it is re-imported "
                                          "through CLAUDE.local.md. Use it; do not run load."),
                },
            }))
            return
        lines = reload_lines(payload, info, cwd)
    else:
        lines = package_lines(info, cwd, payload['source'])
    if not lines:
        return
    text = '\n'.join(lines)
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS - 40] + '\n… (truncated; run `hypernym packages`)'
    print(text)


if __name__ == '__main__':
    main()
