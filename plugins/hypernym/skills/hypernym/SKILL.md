---
name: hypernym
description: Hypernym repo analysis for the git repo you are working in — check floats and repo-analyze jobs, submit a repo analyze, download the finished package, report how far it has drifted from the current code, and load it into the session. Use when the user says "hypernym help" or "hypernym login" (sign in to Hypernym/Zephyr — use this skill's login, never a hand-written curl), asks about floats, float status, repo analyze, a Hypernym package or treatment prompt, or wants Hypernym context for this repo loaded.
---

# Hypernym

All actions go through one script in this skill's `scripts/` folder. Run it from the repo, with the
absolute path to this skill's directory:

```bash
python3 <this skill's directory>/scripts/hypernym.py --dir "$PWD" <command>
```

## "hypernym help" — always start here

```bash
python3 <skill dir>/scripts/hypernym.py --dir "$PWD" help
```

It checks the API key works on each configured server, lists this repo's floats and repo-analyze jobs,
downloads any completed job that is not on disk yet, and lists local packages with drift. Show the
output, then summarize: auth status per server, jobs in flight, and the newest package with its drift.
If a package exists, offer to load it.

## Run repo analyze

1. Get the question from the user: the bug, feature, or area they want the package to cover. Do not
   invent one. Ask how many files (`--top-n`, default 40) only if they care about size or cost.
2. Dry run first — always. Without `--execute` the script submits nothing and prints exactly what it
   would send:
   ```bash
   python3 <skill dir>/scripts/hypernym.py --dir "$PWD" analyze --question "<question>" [--top-n N]
   ```
   Show that to the user. A real run creates a float (paid compression) for every ranked file the
   server has not compressed before. Add `--execute` ONLY when the user explicitly approves this
   submission in their latest message; never on your own initiative, never to "test".
   It analyzes the newest commit that exists on GitHub (the server clones from there) and says so if
   the local HEAD is ahead. Private repos work if the user's Hypernym account has GitHub linked.
   If the server has its dry-run gate on, an executed job stops after ranking and `wait` prints the
   ranked files instead of a package — review them with the user before anything else.
3. The job runs on the server: files the server has never compressed take a few minutes each, three at
   a time. Wait with:
   ```bash
   python3 <skill dir>/scripts/hypernym.py --dir "$PWD" wait <job_id> --minutes 10
   ```
   Run `wait` again if it is still running, or tell the user the next "hypernym help" in any session
   will download it.
4. To stop a running job (only when the user asks):
   ```bash
   python3 <skill dir>/scripts/hypernym.py --dir "$PWD" cancel <job_id>
   ```
   It cancels the job's in-flight floats and leaves finished ones, and floats shared from other users,
   untouched.

## Packages

Completed jobs are saved to `~/.hypernym/packages/<owner>__<repo>/<completed UTC>_<commit>_<job_id>/`:
`treatment_prompt.txt` (the package), `manifest.json` (job, commit, question, timestamps, per-file
SHA-256 at the analyzed commit), and `job.json` (raw API response). Every session and tool on the
machine can read them.

- `packages` lists them with drift: how many of the package's files differ in the working tree
  now, and how many commits HEAD is past the package's commit.
- `load` prints the newest package (or `--package DIR`) for you to read into the session. Mention the
  drift when you load it: files that changed since the package describe older code.

## Setup — login and GitHub

"hypernym login" signs in through the browser, like `claude login`: it opens GitHub, the user approves,
and the session (30 days) is saved to `~/.hypernym/config.json`, mode 600. Run it in the background or with
a long timeout, tell the user to approve in the browser that opened (print the URL too), and wait:

```bash
python3 <skill dir>/scripts/hypernym.py login
```

It works on any machine with no setup: the same GitHub account always gets the same Hypernym account
(created on first login). If the user was given a key instead, log in with the key:

```bash
python3 <skill dir>/scripts/hypernym.py login --key -        # then paste the key
```

`--url` defaults to `https://zephyr-b-gpu.hypernym.ai`; `--name` defaults to `zephyr`. Logging in again
with the same name replaces that server's login. `HYPERNYM_API_KEY` / `HYPERNYM_API_URL`, if set, take
precedence over the config file. Never write a key into this skill's folder or the repo.

Private repos need the rider's GitHub account linked to their Hypernym account:

```bash
python3 <skill dir>/scripts/hypernym.py --dir "$PWD" github status   # lists active links
python3 <skill dir>/scripts/hypernym.py --dir "$PWD" github link     # prints a GitHub approval URL, waits
```

Give the user the approval URL from `github link`; they open it in a browser. The server keeps the
GitHub token encrypted and uses it only to clone that user's private repos; private checkouts are
deleted when each job ends.
