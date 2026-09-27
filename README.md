# Hypernym Nebula

This marketplace holds two separate plugins. Install either one or both.

| plugin | what it does | network |
|---|---|---|
| **hypernym** (Zephyr connector) | Repo analyze for the git repo you're in. It checks your floats and jobs, submits an analysis, downloads the finished package, shows how far it has drifted from your current code, and loads it into the session. | Talks **only** to the Hypernym server you log in to, using **your** key. |
| **nebulashield** | Local shield that flags fabricated confession, decision handback, claim beyond evidence, and forced repetition in Claude's replies. | **None.** It sends nothing and trains on nothing. See [`plugins/nebulashield/PRIVACY.md`](plugins/nebulashield/PRIVACY.md). |

The two plugins never import each other. nebulashield's no-network guarantee depends on that.

## Requirements

- **uv** (https://docs.astral.sh/uv/). nebulashield builds its own Python environment with it on first run.
- **python3**, available as `python3`. The hypernym plugin uses only the standard library.
- **nebulashield** runs on macOS with Apple Silicon only, because it ships a prebuilt native scorer.
- **hypernym** needs a Hypernym API key. Keys are issued per person; ask Hypernym for one.

## Install — Claude Code

Paste ONE of these into a terminal (macOS). Each line is complete: it installs `uv` only if you don't have it, adds the
marketplace, and installs the plugin(s). Then start Claude Code — the first session finishes setup and turns on the
status line.

**nebulashield only** (local, offline shield)
```
command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh; claude plugin marketplace add HypernymAI/hypernym-nebula && claude plugin install nebulashield@hypernym-nebula
```

**hypernym only** (Zephyr connector)
```
claude plugin marketplace add HypernymAI/hypernym-nebula && claude plugin install hypernym@hypernym-nebula
```

**Both** (recommended — they share one status line)
```
command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh; claude plugin marketplace add HypernymAI/hypernym-nebula && claude plugin install nebulashield@hypernym-nebula && claude plugin install hypernym@hypernym-nebula
```

Already inside Claude Code? The same thing as slash commands:
```
/plugin marketplace add HypernymAI/hypernym-nebula
/plugin install nebulashield@hypernym-nebula
/plugin install hypernym@hypernym-nebula
```
then start a new session. Add the other plugin any time: hypernym joins the status line within a second;
nebulashield shows "finishing setup" until your next session start builds its local Python environment (once).

## Install — Codex

Only **hypernym** is listed for Codex. nebulashield reads Claude Code transcripts and has no Codex adapter yet.
Add this repo as a plugin marketplace in Codex, then install `hypernym`.
<!-- release owner: confirm the exact Codex commands during the clean-install check -->

## First run — hypernym

In any git repo, tell Claude:

1. **"hypernym login"** opens GitHub in your browser. Approve, and you're signed in for 30 days (like
   `claude login`); the session is saved to `~/.hypernym/config.json` (readable only by you). Your first login
   creates your Hypernym account; later logins on any computer find the same one. Given a key instead? Use
   `hypernym login --key -`.
2. **"hypernym help"** shows your account, this repo's floats and repo-analyze jobs, and any packages on disk with
   their drift.
3. **Analyze**: describe the bug or area you want covered. Claude does a dry run first and shows exactly what
   would be sent. A real run happens only after you approve it, because every file the server hasn't compressed
   before costs a float.
4. **Private repos**: say "hypernym github link" and approve access in the browser. Check with "hypernym github
   status".

Finished packages are saved to `~/.hypernym/packages/<owner>__<repo>/…`. Each one has the treatment prompt, a
manifest with per-file SHA-256 at the analyzed commit, and the raw job. Every session on the machine can load
them.

## Status line (Claude Code)

Both plugins share one status line: nebulashield's badge rows with the Zephyr line appended, or whichever one
you have installed. It turns itself on at session start only if you have no status line (or an older one of
ours); it never replaces someone else's, and never comes back after you remove it.

```
/hypernym-statusline          # install
/hypernym-statusline remove   # remove
/hypernym-statusline force    # replace an unrecognized status line (settings.json is backed up first)
```

## What each plugin sends, and where

**hypernym** sends HTTPS requests to your server with your key in `X-API-Key`. The requests are:

- account and quota checks
- listing your floats and jobs
- for an analysis: the repo's GitHub URL, the commit, your question, and the file count and budget
- job status, download, and cancel
- GitHub link setup

It sends **no source code**. The server clones the repo from GitHub itself. Private repos are cloned with the
GitHub access you linked: the server stores that token encrypted, and each job's private checkout is deleted
when the job ends. Drift is computed locally by hashing your working tree, and those hashes are never uploaded.

**nebulashield** sends nothing. The only download is `uv` fetching its pinned Python packages from PyPI on first
run. Details and tests are in [`plugins/nebulashield/PRIVACY.md`](plugins/nebulashield/PRIVACY.md).

## Files on your machine

- `~/.hypernym/` holds `config.json` (your login, mode 600), `packages/`, and the status cache.
- `~/.hypernym-nebula/` holds the shared status-line launcher.
- `~/.nebulashield/` holds nebulashield's settings and local database. See PRIVACY.md.
