# Hypernym Nebula — the shield and Zephyr, for Claude Code

By [Hypernym](https://hypernym.ai), for invited clients. This private repository is the Pro marketplace
(`hypernym-nebula-pro`): it adds hypernym to nebula, the free shield from `HypernymAI/hypernym_nebula`. One status line:

| plugin | what it does | network |
|---|---|---|
| **nebula** | The local shield. Flags **fabricated confession**, **decision handback**, **claim beyond evidence** and **forced repetition** in Claude's replies. | **None.** It sends nothing and trains on nothing. See [`plugins/nebula/PRIVACY.md`](plugins/nebula/PRIVACY.md). |
| **hypernym** | The Zephyr connector. Analyzes the git repo you're in on Hypernym's server and loads the result into the session as a context package, with its drift from your current code. | Talks **only** to the Zephyr you sign in to. Every request is signed by this computer's own key. It never sends source code. |

The two plugins never import each other; Nebula's no-network guarantee depends on that. Install either, or both.

**For agents — install in one step.** The repository is private, so the machine needs GitHub access to it first: `gh
auth status` must show an account Hypernym has given access, and if `git ls-remote
https://github.com/HypernymAI/hypernym_nebula_boosties` fails, run `gh auth setup-git` once. Then, on macOS 11+ or Linux
(x86-64 / ARM64):

```
command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh; claude plugin marketplace add HypernymAI/hypernym_nebula && claude plugin marketplace add HypernymAI/hypernym_nebula_boosties && claude plugin install nebula@hypernym-nebula && claude plugin install hypernym@hypernym-nebula-pro
```

Then tell the user to **restart Claude Code once**, and to say **"hypernym login"** in that session to connect Zephyr.
Check with `claude plugin list` and `/nebula:shield-status`.

## Requirements

- **Claude Code.**
- **GitHub access to this repository** (Hypernym invites your account). With the `gh` CLI signed in, `gh auth setup-git`
  lets `claude plugin` fetch it.
- **uv** (https://docs.astral.sh/uv/) for nebula; it builds its own Python environment on the first run.
- **python3** for hypernym (standard library only).
- **nebula**: macOS 11+ (Apple Silicon or Intel) or Linux (x86-64 or ARM64, glibc 2.28+). On Windows, use WSL.
  **hypernym** runs anywhere `python3` does.
- **A Zephyr invitation for your GitHub account.** Sign-in is invite-only.

## Install

Paste ONE of these into a terminal. Each line is complete: it installs `uv` only if you need it, adds the marketplace, and installs the plugin(s).

**Both** (recommended; they share one status line)
```
command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh; claude plugin marketplace add HypernymAI/hypernym_nebula && claude plugin marketplace add HypernymAI/hypernym_nebula_boosties && claude plugin install nebula@hypernym-nebula && claude plugin install hypernym@hypernym-nebula-pro
```

**Already have Nebula** (or Zephyr only): nothing is uninstalled
```
claude plugin marketplace add HypernymAI/hypernym_nebula_boosties && claude plugin install hypernym@hypernym-nebula-pro
```

Already inside Claude Code? The same, as slash commands:
```
/plugin marketplace add HypernymAI/hypernym_nebula
/plugin marketplace add HypernymAI/hypernym_nebula_boosties
/plugin install nebula@hypernym-nebula
/plugin install hypernym@hypernym-nebula-pro
```

Then start a new session. You can add the other plugin any time: hypernym joins the status line within a second, and
nebula asks for one restart, then builds its environment as that session starts.

## First run: Zephyr

In any git repo, tell Claude:

1. **"hypernym login"**. GitHub opens in your browser, as with `claude login`. Approve, and you're in, on this computer
   and any other, with nothing to copy or remember. If your GitHub account is invited, you land on the account Hypernym
   set up for you; if it isn't, nothing is created. Each computer gets its own key in `~/.hypernym/identity/`, readable
   only by you; no token or password is stored. Given a key instead? Use `hypernym login --key -`.
2. **"hypernym help"** shows your account, this repo's floats and analysis jobs, and the packages on disk with their drift.
3. **Analyze**: describe the bug or area you want covered. Claude does a dry run first and shows exactly what would be
   sent. A real run happens only after you approve it, because every file the server hasn't compressed before costs a
   float.
4. **Private repos**: say **"hypernym github link"** and approve access in the browser. Check with **"hypernym github
   status"**.

Finished packages are saved to `~/.hypernym/packages/<owner>__<repo>/…`: the treatment prompt, a manifest with each
file's SHA-256 at the analyzed commit, and the raw job. Every session on the machine can load them.

**MCP.** The hypernym plugin also registers an MCP server named `hypernym` (standard-library Python over stdio). Its tools
run the same commands as the skill, so analyze is still a dry run until you approve. For Codex or any other MCP host,
point it at the same program: command `python3`, argument
`<plugin dir>/skills/hypernym/scripts/hypernym_mcp_server.py`.

## The shield: nebula

Nebula reads each reply as it lands and shows its judgement in the status line, with a short note under a flagged reply.
Claude sees the note too, and fixes the reply rather than arguing with the flag. Say **"hypernym nebula"** for soliton
mode: a reply that peaks gets a vivid banner and is rephrased once.

| command | what it does |
|---|---|
| `/nebula:shield-status` | On/off, mode, detectors and thresholds, and what it has flagged recently. |
| `/nebula:shield-privacy` | Exactly what is stored on this machine, where, and how much. |
| `/nebula:shield-retention none\|excerpt\|full` | What transcript text is kept. Default `none`: scores and counts only. |
| `/nebula:shield-exclude <glob>` · `/nebula:shield-include <glob>` | Keep projects out entirely, or let them back in. |
| `/nebula:shield-export` · `/nebula:shield-purge [yes]` | Export everything stored as JSON, or delete it. |
| `/nebula:shield-backfill` | Opt in: score your past sessions on this machine (never uploaded). |

## Status line

Both plugins share one status line: the Hypernym logo and the shield's judgement row, with the Zephyr line on the last
row (or whichever plugin you have installed). It turns itself on at session start only if you have no status line or an
older one of ours. It never replaces someone else's, and it never comes back after you remove it.

```
/hypernym-statusline          # install
/hypernym-statusline remove   # remove
/hypernym-statusline force    # replace an unrecognized status line (settings.json is backed up first)
```

## What each plugin sends, and where

**hypernym** sends HTTPS requests only to the Zephyr you signed in to. Each request carries a signature from this
computer's key in `X-API-Key`: a statement naming the device, the server, the method, the path, a hash of the body, the
time and a one-time nonce. It never carries a password or bearer token. The private key never leaves your machine. The
requests are:

- account and quota checks;
- listing your floats and jobs;
- for an analysis: the repo's GitHub URL, the commit, your question, and the file count and budget;
- job status, download and cancel;
- GitHub link setup.

It sends **no source code**. The server clones the repo from GitHub itself. Private repos are cloned with the GitHub
access you linked: the server stores that token encrypted, and each job's private checkout is deleted when the job ends.
Drift is computed locally by hashing your working tree, and those hashes are never uploaded.

**nebula** sends nothing. The only download is `uv` fetching its pinned Python packages from PyPI on the first run.

## Files on your machine

- `~/.hypernym/`: `identity/device.json` (this computer's key, mode 600), `config.json` (which Zephyr you signed in to,
  mode 600), `packages/`, and the status cache.
- `~/.nebula/`: nebula's settings and local record. See PRIVACY.md.
- `~/.hypernym-nebula/`: the shared status-line launcher.

## Update

```
claude plugin marketplace update && claude plugin update nebula@hypernym-nebula && claude plugin update hypernym@hypernym-nebula-pro
```

Updates apply from your next session. Nebula's
record and settings, and hypernym's identity and packages, are kept.

## Codex

Only **hypernym** is listed for Codex; nebula reads Claude Code transcripts and has no Codex adapter yet. Add this
repository as a plugin marketplace in Codex, then install `hypernym`.
<!-- release owner: confirm the exact Codex commands during the clean-install check -->
