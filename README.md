# Hypernym Nebula

**For agents — install in one step.** The repo `HypernymAI/hypernym-nebula` is private: the machine needs GitHub access
to it (`gh auth status` shows an account in the HypernymAI org; if `git ls-remote https://github.com/HypernymAI/hypernym-nebula`
fails, run `gh auth setup-git` once). On macOS with Apple Silicon, run
`command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh; claude plugin marketplace add HypernymAI/hypernym-nebula && claude plugin install nebula@hypernym-nebula && claude plugin install hypernym@hypernym-nebula`
(leave out either `install` for one plugin; `hypernym` alone also runs on other systems with `python3`). Then start a new
Claude Code session: **nebula** (offline shield: no account, sends nothing) sets itself up in seconds, and the
shared status line (Hypernym logo, the shield's judgement row, the Zephyr line) turns on by itself unless a custom
`statusLine` is already set (`/nebula:statusline force` replaces it, backed up first). For Zephyr, say **"hypernym login"**: GitHub opens, you approve, done, on every computer
(invite-only; an invited account lands on the account Hypernym set up for you).
Check with `claude plugin list` and `/nebula:shield-status`; update with
`claude plugin marketplace update hypernym-nebula && claude plugin update nebula@hypernym-nebula && claude plugin update hypernym@hypernym-nebula`.

This marketplace holds two separate plugins. Install either one or both.

| plugin | what it does | network |
|---|---|---|
| **hypernym** (Zephyr connector) | Repo analyze for the git repo you're in. It checks your floats and jobs, submits an analysis, downloads the finished package, shows how far it has drifted from your current code, and loads it into the session. | Talks **only** to the Hypernym server you sign in to; every request is signed by this install's device key. |
| **nebula** | Local shield that flags fabricated confession, decision handback, claim beyond evidence, and forced repetition in Claude's replies. | **None.** It sends nothing and trains on nothing. See [`plugins/nebula/PRIVACY.md`](plugins/nebula/PRIVACY.md). |

The two plugins never import each other. nebula's no-network guarantee depends on that.

## Requirements

- **uv** (https://docs.astral.sh/uv/). nebula builds its own Python environment with it on first run.
- **python3**, available as `python3`. The hypernym plugin uses only the standard library.
- **nebula** runs on macOS with Apple Silicon only, because it ships a prebuilt native scorer.
- **hypernym** needs a GitHub account invited by Hypernym (sign-in is invite-only).

## Install — Claude Code

Paste ONE of these into a terminal (macOS). Each line is complete: it installs `uv` only if you don't have it, adds the
marketplace, and installs the plugin(s). Then start Claude Code — the first session finishes setup and turns on the
status line.

**nebula only** (local, offline shield)
```
command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh; claude plugin marketplace add HypernymAI/hypernym-nebula && claude plugin install nebula@hypernym-nebula
```

**hypernym only** (Zephyr connector)
```
claude plugin marketplace add HypernymAI/hypernym-nebula && claude plugin install hypernym@hypernym-nebula
```

**Both** (recommended — they share one status line)
```
command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh; claude plugin marketplace add HypernymAI/hypernym-nebula && claude plugin install nebula@hypernym-nebula && claude plugin install hypernym@hypernym-nebula
```

Already inside Claude Code? The same thing as slash commands:
```
/plugin marketplace add HypernymAI/hypernym-nebula
/plugin install nebula@hypernym-nebula
/plugin install hypernym@hypernym-nebula
```
then start a new session. Add the other plugin any time: hypernym joins the status line within a second;
nebula shows "finishing setup" until your next session start builds its local Python environment (once).

## Install — Codex

Only **hypernym** is listed for Codex. nebula reads Claude Code transcripts and has no Codex adapter yet.
Add this repo as a plugin marketplace in Codex, then install `hypernym`.
<!-- release owner: confirm the exact Codex commands during the clean-install check -->

## First run — hypernym

In any git repo, tell Claude:

1. **"hypernym login"** opens GitHub in your browser, like `claude login`. Approve, and you're in, on this and any
   other computer, with nothing to copy or remember. Sign-in is invite-only: if your GitHub account is invited, you
   land on the account Hypernym set up for you; if it isn't, nothing is created. The plugin holds no secret of
   Hypernym's: each computer gets its own key in `~/.hypernym/identity/` (readable only by you) that signs every
   request, and no token is stored. Given a key instead? Use `hypernym login --key -`.
2. **"hypernym help"** shows your account, this repo's floats and repo-analyze jobs, and any packages on disk with
   their drift.
3. **Analyze**: describe the bug or area you want covered. Claude does a dry run first and shows exactly what
   would be sent. A real run happens only after you approve it, because every file the server hasn't compressed
   before costs a float.
4. **Private repos**: say "hypernym github link" and approve access in the browser. Check with "hypernym github
   status".

**MCP.** The hypernym plugin also registers an MCP server named `hypernym` (standard-library Python over stdio). Its
tools (help, floats, analyze, wait, cancel, sync, packages, load, github status, account list/use, login start/status) run the same
commands as the skill, so analyze is still a dry run until you approve. For Codex or any other MCP host, point it
at the same program: command `python3`, argument `<plugin dir>/skills/hypernym/scripts/hypernym_mcp_server.py`.

Finished packages are saved to `~/.hypernym/packages/<owner>__<repo>/…`. Each one has the treatment prompt, a
manifest with per-file SHA-256 at the analyzed commit, and the raw job. Every session on the machine can load
them.

## Status line (Claude Code)

Both plugins share one status line: nebula's badge rows with the Zephyr line appended, or whichever one
you have installed. It turns itself on at session start only if you have no status line (or an older one of
ours); it never replaces someone else's, and never comes back after you remove it.

```
/hypernym-statusline          # install
/hypernym-statusline remove   # remove
/hypernym-statusline force    # replace an unrecognized status line (settings.json is backed up first)
```

## What each plugin sends, and where

**hypernym** sends HTTPS requests to the Zephyr you signed in to. Each request carries a signature from this
install's device key in `X-API-Key` (a `restoria1.` statement naming the device, the server, the method, the
path, a hash of the body, the time and a one-time nonce) — never a password or bearer token. Sign-in sends
this device's address (a public key) and a signed request; GitHub then tells the Zephyr which account approved.
The device's private key never leaves your machine. The requests are:

- account and quota checks
- listing your floats and jobs
- for an analysis: the repo's GitHub URL, the commit, your question, and the file count and budget
- job status, download, and cancel
- GitHub link setup

It sends **no source code**. The server clones the repo from GitHub itself. Private repos are cloned with the
GitHub access you linked: the server stores that token encrypted, and each job's private checkout is deleted
when the job ends. Unless you already hold your own identity key, the Zephyr creates it at your first sign-in and keeps it, encrypted like the GitHub token; the Zephyr then vouches for each computer you sign in from. Drift is computed locally by hashing your working tree, and those hashes are never uploaded.

**nebula** sends nothing. The only download is `uv` fetching its pinned Python packages from PyPI on first
run. Details and tests are in [`plugins/nebula/PRIVACY.md`](plugins/nebula/PRIVACY.md).

## Files on your machine

- `~/.hypernym/` holds `identity/device.json` (this computer's key, mode 600), `config.json` (which Zephyr you signed in to, mode 600), `packages/`, and the status cache.
- `~/.hypernym-nebula/` holds the shared status-line launcher.
- `~/.nebula/` holds nebula's settings and local database. See PRIVACY.md.
