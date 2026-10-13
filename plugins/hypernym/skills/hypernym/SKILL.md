---
name: hypernym
description: Hypernym repo analysis for the git repo you are working in — check floats and repo-analyze jobs, submit a repo analyze, download the finished package, report how far it has drifted from the current code, and load it into the session. Use when the user says "hypernym go", "hypernym analyze", "hypernym do it" or "yes" to a Hypernym offer (start an analysis of this repo now), "hypernym help" or "hypernym login" (sign in to Hypernym/Zephyr — use this skill's login, never a hand-written curl), asks about floats, float status, repo analyze, a Hypernym package or treatment prompt, or wants Hypernym context for this repo loaded.
---

# Hypernym

All actions go through one script in this skill's `scripts/` folder. Run it from the repo, with the
absolute path to this skill's directory:

```bash
python3 <this skill's directory>/scripts/hypernym.py --dir "$PWD" <command>
```

## "hypernym go" — start an analysis

"hypernym go", "hypernym analyze", "hypernym do it", or "yes" to your Hypernym offer all mean: start it now.
Call the `hypernym_analyze` tool with `execute: true`. If the user described a task, pass it as `question`;
otherwise leave `question` out and the whole repo is analyzed. Don't ask them anything first — Hypernym's approval
dialog shows what is sent, the cost and who pays, and it is their only decision.
- Submitted: one line — "Started. I'll keep working and switch to the analysis when it's ready."
- Cancelled: one line — "Not started; nothing was sent. Say 'hypernym go' whenever you want it."
- Finished: it downloads by itself; load it yourself when the work touches this code and say once:
  "The Hypernym analysis for this repo is ready — I'm using it now." Never ask the user to run load, sync or wait.

## "What does this do?" — `about`

```bash
python3 <skill dir>/scripts/hypernym.py about
```

Prints what the two plugins are (Nebula, the local shield; Hypernym, the Zephyr connector) and the install and
update lines. Works before sign-in. Show it as-is.

## At session start (automatic)

**Loading is your job, never the user's.** Never ask the user to run `load`, `sync` or `wait`: finished analyses
download by themselves, and you load them. When a session starts, resumes or is cleared inside a GitHub repo, a hook
adds a short note: if an analysis for this repo is ready, load it yourself (the note gives the exact command) when the
work touches this repo's code; if there is none, offer one only when the task is about this repo's code — the approval
dialog is the user's only decision. After a compaction the note appears only if you had loaded the analysis in this
session: load it again right away with the command it gives. The hook never calls the network.
`HYPERNYM_SESSION_CONTEXT=0` turns the note off.

## "hypernym help" — always start here

```bash
python3 <skill dir>/scripts/hypernym.py --dir "$PWD" help
```

It checks the API key works on each configured server, lists this repo's floats and repo-analyze jobs,
downloads any completed job that is not on disk yet, and lists local packages with drift. Show the
output, then summarize: auth status per server, jobs in flight, and the newest package with its drift.
If a package exists, offer to load it.

## Run repo analyze

1. The focus: the user's current task if they described one; otherwise none, and the whole repo is analyzed (see
   "hypernym go" above — never make the user answer a question before an analysis can start). The file count is
   40 unless they ask for another; it is the most credits it can use. The approval dialog is the final gate.
2. Dry run first — always. It submits nothing and shows exactly what would be sent, the cost (up to N new floats =
   up to N credits), who pays (the account this machine acts as), and **what is not on GitHub**: the server reads
   the pushed commit, so commits not pushed, uncommitted edits and untracked files are described in their GitHub
   version (or not at all). That is information, not an error — files always change. Show it to the user.
   ```bash
   python3 <skill dir>/scripts/hypernym.py --dir "$PWD" analyze --question "<question>" [--top-n N]
   ```
   **To submit, use the MCP tool `hypernym_analyze` with `execute: true`.** It shows the person Hypernym's own
   approval dialog with that preview (Submit / Cancel, file count); only their Submit sends anything, and
   Decline or Cancel sends nothing. You cannot approve for them — don't try to. If the tool says the client
   cannot show the dialog, show the dry run and get an explicit yes in their latest message; only then run the
   CLI with `--execute`. Never submit on your own initiative, never to "test".
   The person's **sending** setting (portal → Sending, printed by `help`) decides further:
   - `off` for this repo: nothing is sent; the script refuses. Don't work around it.
   - `ask` (the default): the rule above.
   - `auto`: you may start a submission on your own with `auto: true` (CLI `--auto`) when an analysis would
     clearly help the task. Through the MCP tool the approval dialog still opens; where no dialog can be shown,
     the script submits within the person's daily credit limit and refuses beyond it (then show the dry run and
     ask). Say that you submitted and why.
   It analyzes the newest commit that exists on GitHub (the server clones from there) and says so if
   the local HEAD is ahead. Private repos work if the user's Hypernym account has GitHub linked.
   If the server has its dry-run gate on, an executed job stops after ranking and `wait` prints the
   ranked files instead of a package — review them with the user before anything else.
3. The job runs on the server: files the server has never compressed take a few minutes each, three at
   a time. Wait with:
   ```bash
   python3 <skill dir>/scripts/hypernym.py --dir "$PWD" wait <job_id> --minutes 10
   ```
   Run `wait` again yourself if it is still running; finished jobs also download by themselves in the background.
   Never ask the user to run it.
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
- `load` prints the newest package (or `--package DIR`) for you to read into the session — you run it, the user
  never does. Mention the drift when you load it: files that changed since the package describe older code.
  The status line shows "analysis in use" once this session has loaded it.

## Setup — login and GitHub

"hypernym login" signs in through the browser, like `claude login`: it opens GitHub, the user approves, done. It
is the same on every machine — first time or tenth — with nothing to copy, remember or type. Sign-in is
invite-only: the Zephyr checks the GitHub account is invited and lands the user on the account Hypernym set up for
them. The plugin holds no Hypernym secret: this machine gets its own key (`~/.hypernym/identity/device.json`, mode
600) that signs every request, and no token is stored. Run it in the background or with a long timeout, tell the
user to approve in the browser that opened (print the URL too), and wait:

```bash
python3 <skill dir>/scripts/hypernym.py login
```

"not on this Zephyr's invite list" means the operator has not invited that GitHub account yet — nothing was
created. If the user was given a key instead, log in with the key:

```bash
python3 <skill dir>/scripts/hypernym.py login --key -        # then paste the key
```

`--url` defaults to `https://zephyr-b-gpu.hypernym.ai`; `--name` defaults to `zephyr`. Logging in again
with the same name replaces that server's login. `HYPERNYM_API_KEY` / `HYPERNYM_API_URL`, if set, take
precedence over the config file. Never write a key into this skill's folder or the repo.

One identity can hold several accounts on a Zephyr (e.g. two emails). Commands act as this machine's default
account; if the user expected work that isn't there (e.g. `help` shows 0 jobs), check which account is in use:

```bash
python3 <skill dir>/scripts/hypernym.py account list                 # accounts, and which one this machine acts as
python3 <skill dir>/scripts/hypernym.py account use EMAIL            # make EMAIL this machine's default
python3 <skill dir>/scripts/hypernym.py --account EMAIL --dir "$PWD" help   # one command as another account
```

On each new machine, run `account use` once for the account that holds their work (the Pilot: `account use
chris@hypernym.ai`, otherwise they see 0 jobs).

Private repos need the rider's GitHub account linked to their Hypernym account:

```bash
python3 <skill dir>/scripts/hypernym.py --dir "$PWD" github status   # lists active links
python3 <skill dir>/scripts/hypernym.py --dir "$PWD" github link     # prints a GitHub approval URL, waits
```

Give the user the approval URL from `github link`; they open it in a browser. The server keeps the
GitHub token encrypted and uses it only to clone that user's private repos; private checkouts are
deleted when each job ends.
