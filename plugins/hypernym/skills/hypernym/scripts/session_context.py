#!/usr/bin/env python3
"""
SessionStart hook (startup, resume, clear, compact): tell the agent, in a few lines, what Hypernym holds for the repo
it was launched in, so a brand-new session or one that was just compacted knows the package exists without anyone
having to say "hypernym help".

What it prints is the agent's context (plain stdout, the one form both Claude Code and Codex accept for SessionStart):
  - the newest package for this repo: job, commit, how many commits behind HEAD, the question it answered, its size
  - drift: which packaged files differ from the working tree (read those directly; the package describes old versions)
  - how to load it (the full package is ~80K tokens: far over what a hook may inject, and too much to spend on work
    that doesn't touch this code, so loading stays one explicit step)
  - after a compaction: that a package loaded earlier is gone from context and must be reloaded if still needed

Local only: reads ~/.hypernym and runs git. Never calls the network, never submits anything. Prints nothing outside a
GitHub repo, and nothing when HYPERNYM_SESSION_CONTEXT=0. NO .get().
"""
import json
import os
import sys
from pathlib import Path

import hypernym

HERE = Path(__file__).resolve().parent
MAX_CHARS = 6000          # Claude Code caps hook context at 10,000 characters; Codex at ~2,500 tokens by default
MAX_LISTED = 15


def load_command(cwd: str) -> str:
    return f'python3 "{HERE / "hypernym.py"}" --dir "{cwd}" load'


def package_lines(info: dict, cwd: str) -> list:
    pkgs = hypernym.list_packages(info)
    if not pkgs:
        if not hypernym.load_servers():
            return []
        return [f"Hypernym: no context package for {info['key']} on this computer yet. "
                "`hypernym help` shows this repo's jobs and downloads any that finished; an analysis is only ever "
                "submitted with the user's explicit approval."]
    pkg = pkgs[0]
    m = json.loads((pkg / 'manifest.json').read_text())
    d = hypernym.drift(info, m)
    differ = d['changed'] + d['missing']
    behind = 'an unknown number of commits'
    if d['commits_behind'] is not None:
        behind = f"{d['commits_behind']} commit"
        if d['commits_behind'] != 1:
            behind += 's'
    lines = [
        f"Hypernym context package for {info['key']} (on this computer; loading it sends nothing anywhere).",
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
    lines.append(f"Load it when the work touches this repo's code (skip it for unrelated work): {load_command(cwd)}")
    if len(pkgs) > 1:
        lines.append(f"{len(pkgs) - 1} older package(s) for this repo are also on disk (`hypernym packages`).")
    return lines


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
    lines = package_lines(info, cwd)
    if not lines:
        return
    if payload['source'] == 'compact':
        lines.insert(0, "Context was just compacted: if the Hypernym package was loaded earlier in this session, its "
                        "text is no longer in context; reload it if the work still needs it.")
    text = '\n'.join(lines)
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS - 40] + '\n… (truncated; run `hypernym packages`)'
    print(text)


if __name__ == '__main__':
    main()
