# Hypernym Nebula — release ownership and checklist

One marketplace (`hypernym-nebula`), ONE package, two plugins that know about each other:
**nebula** (local, offline shield) and **hypernym** (Zephyr connector). Either installs alone; together they share
one status line. Nothing here is published until every box below is checked and the owner has signed off on the gates.

## Who owns what

| area | owner | files |
|---|---|---|
| **Final packaging / release** (marketplace listing, versions, install boxes, this checklist, go/no-go) | the **shield session** (release owner, per the owner's instruction) | `.claude-plugin/marketplace.json`, `.agents/plugins/marketplace.json`, `RELEASE.md`, `.gitignore`, README install section |
| `plugins/nebula/` — the offline shield | shield session | written ONLY by `ctxshield-plugin/tools/sync_to_marketplace.py` from the dev repo `ctxshield-plugin/plugin/` — never edited here |
| **Shared status line** `nebula_statusline.py` (byte-identical in both plugins) | shield session = source of truth (`ctxshield-plugin/plugin/shield/`) | the sync writes BOTH copies (`plugins/nebula/shield/`, `plugins/hypernym/skills/hypernym/scripts/`) and verifies identity; never edit a copy here |
| `plugins/hypernym/` — the Zephyr connector (everything except the shared file) | the **Zephyr session** | `plugins/hypernym/` |
| Marketplace README (first run, what is sent where) | Zephyr session drafts; release owner signs off | `README.md` |
| GitHub publishing mechanics (remote, push, tags) | Zephyr session, **after** release sign-off | — |
| Zephyr server | Zephyr session | `portable_hypernym_processor` |
| **Gates** | **the owner** | see "Gates" below |

Rules: the two plugins never IMPORT each other (nebula's no-network guarantee depends on it); they know each other
through Claude Code's plugin registry and the shared status line. Each session edits only its own files; changes to the
other's files go through a message.

## How the two fit together
- Claude Code's registry (`installed_plugins.json` + `enabledPlugins`) is the only signal of what is installed —
  uninstalling leaves files on disk (measured).
- Either plugin's SessionStart refreshes `~/.hypernym-nebula/bin/{statusline,nebula_statusline.py}` and sets `statusLine`
  only when there is none or it is one of ours (backup first; never a foreign one; never after `remove`).
- Every refresh: nebula's 3 rows, hypernym's Zephyr line appended to the last row — or whichever one is installed.
  A failing part says so (`✖ <plugin>: …`); a freshly (re)installed shield says "finishing setup at session start".
- Each plugin's status (`/nebula:shield-status`, `hypernym help`) says whether the other is installed + how.

## Release checklist

Build
- [x] marketplace renamed `hypernym-nebula` (Claude Code + Codex manifests); plugin `ctxshield` → `nebulashield` 0.3.2
      (commands `/nebulashield:*`, data `~/.nebulashield`); hypernym 0.5.1
- [x] `plugins/nebulashield/` synced — refuses ungated/private/personal content; 154 files, 19.2 MB; old `plugins/ctxshield/` removed
- [x] `claude plugin validate .` + both plugins `--strict` pass
- [x] shared status line byte-identical in both plugins (written + verified by the sync)
- [x] plugin `nebulashield` → `nebula` 0.4.0 (pilot, 2026-09-28): commands `/nebula:*`, data `~/.nebula`, DB `nebula.db`,
      `bin/nebula-run`; old `plugins/nebulashield/` removed by the sync; hypernym 0.5.3 (shared status line VERSION 4)
- [x] nebula NOT in the Codex marketplace (needs a Codex transcript adapter + parity test first)
- [x] `.gitignore`: `.venv/`, `__pycache__/`, `*.pyc`
- [x] README install section = the website's three copy-paste boxes (shield / hypernym / both) + slash-command form
- [ ] README placeholder: exact Codex install commands

Tests (ctxshield dev repo — all pass 2026-09-27)
- [x] `tests/test_plugin.py` 1–8 · `tests/test_privacy.py` N1–N3, D1–D6 (D6 = shared status-line safety)
- [x] `tests/test_parity_vs_claudestorm.py` (scoring identical to the original; 6,541 replies, badge byte-identical)
- [x] `tests/test_statusline.py` S1–S11 under the system python3 (3.9.6): every composition, failure, hang (process
      group ended), project scope, version guard, peers
- [x] `tests/test_install_matrix.py` — REAL `claude plugin` installs in throwaway HOME + CLAUDE_CONFIG_DIR, each
      plugin's own hooks: box 1 / box 2 / box 3 in BOTH orders (same final bar); uninstall either → other intact;
      reinstall → "finishing setup"; disable; no uv → clear message; a user's own status line never touched

Gates (owner)
- [x] **CLOSED 2026-09-28 (hypernym 0.6.0, reviewed by the release owner):** LOGIN_APP deleted; sign-in = RESTORIA via GitHub
      (invite-only), every request signed by a per-install device key; grep of plugins/hypernym: no app id / secret / hash;
      Zephyr B enroll endpoints live; the pilot's identity signs in (account list, help, github status). Original entry:
      **BLOCKER — app credential in the hypernym plugin:** `plugins/hypernym/skills/hypernym/scripts/hypernym.py:45-46`
      ships `LOGIN_APP = {app_id: claudestorm, app_secret_hash: 0cabf530…}`. The server takes that hash as-is
      (`X-App-Secret-Hash`), and the same app credential is what calls `/api/users/benefact` (adds FL0WT to any account)
      and `/api/status?user_email=` (any user's balance) per ClaudeStorm's CLAUDE.md. Anyone who installs the plugin can
      read it. Options: a separate login-only app whose credential can do nothing else, or the keyless invite-list
      login start; rotate the claudestorm secret if the repo was ever pushed with it.
- [x] repo: **private** `HypernymAI/hypernym-nebula` created + pushed (owner, 2026-09-27: "private first"); the
      website box installs both plugins from it (verified in a throwaway HOME with GitHub access)
- [ ] make it public (owner) — only after the app-credential blocker is fixed
- [x] access mirrored from fieldConstrictor: team `hypernym-devs` = write (push); org defaults for everyone else
- [ ] outside collaborator `mary-octavia` (write on fieldConstrictor) — NOT invited: would be the first
      non-member able to read the embedded app credential; owner's call
- [ ] H06_S2 (claim beyond evidence): ship · gate harder · drop — fails probe v2.1 (p95 0.00156 vs 0.00054); H15_S6 fails by a tie (0.00407 vs 0.00405)
- [x] weights confirmed first-hand (leakage-gated + author names scrubbed, S34; ungated originals stay in `ctxshield-plugin/private_models/`)
- [ ] GitHub OAuth callback — closes on one real `hypernym login` / `hypernym github link` from a rider machine
- [ ] one real end-to-end repo-analyze run (gate open, float cost)
- [x] TLS on Zephyr B — verified by the release owner: `*.hypernym.ai`, valid to 2027-03-03, 401 without a key

Switch this Mac (owner's go; after the gates)
- [x] (done S38; renamed again to `~/.nebula` in S45) move `~/.ctxshield` → `~/.nebulashield` (DB `ctxshield.db` → `nebulashield.db`; keep retention full, wings 76);
      uninstall `ctxshield@ctxshield-local` + `hypernym@hypernym`; remove the old marketplace entries; add
      `hypernym-nebula`; install both; new session → the shared status line replaces the hand-made one-liner.

Known, accepted
- `plugins/nebula/shield/vendor_strata.py` keeps ClaudeStorm's class-10 regex byte-exact (parity proof).
- nebula: macOS 11+ (arm64 + x86_64) and Linux x86_64/aarch64 glibc 2.28+, one prebuilt CPU scorer per platform
  (ctxshield-plugin/tools/build_native_all.sh, RELEASING.md); Windows = WSL. Background auto-detect = Apple Terminal;
  elsewhere the logo is drawn opaque.
- The internal scoring library inside nebula is still the python package `ctxshield` (not user-visible).
