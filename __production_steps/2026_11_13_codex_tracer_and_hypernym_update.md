# Codex session support, tracer integration, and Hypernym update routing

Scope: this session's Codex/tracer/update changes only. Original Dropship design and implementation notes are maintained separately by the Claude agent.

Date: 2026-11-13
Repository: /Users/fieldempress/Desktop/source/hypernym/hypernym_nebula_boosties
Version: 0.7.34
Pushed commit: 8e0c855
Commit message: [ hypernym update has been set up ]

## Implemented

- Shared dropship_tracer MCP tool and CLI route for Claude and Codex. Creating an invitation starts ten-minute pairing; accepting reuses a friendship or pairs and checks a round trip over the encrypted mailbox.
- Stable session aliases preserve invitation routing across display-name changes. Receipt proves courier routing, not an AI response or an awake terminal.
- Codex registration reads the latest session_index.jsonl name. Status snapshots appear through conversation hooks and suppress unchanged end-of-turn output, including in projects without a GitHub remote.
- Send/read refuse ambiguous session selection. Codex resume hints use codex resume. Startup replaces an older courier lacking tracer capability.
- The Hypernym skill treats hypernym update as an instruction to use the current client's plugin manager, preserve local configuration/data/patches, verify the installed version, and report reconnect requirements.

## Verification in the actual development checkout

- Seven focused invitation, naming, routing, MCP dispatch, and status-hook checks passed.
- Five session-routing regression checks passed with a terminal attached. The earlier non-terminal run failed the existing Claude waiting-state expectation; the terminal-attached rerun passed.
- Isolated public ntfy.sh test passed: pairing, encrypted tracer receipt, and ordinary mailbox message delivery to the invited session. A second run confirmed the final stable alias and longer receipt wait.
- Plugin validation passed with a listing-icon warning. Skill validation and git diff --check passed.

## Deployment boundary

Both computers need 0.7.34 and a client reconnect to load the new MCP tool. A live Claude UI session and the user's Fieldriser Codex session were not exercised end to end. Neither a continuously refreshing Codex footer nor automatic waking of an idle agent is established by these changes.

## Publication convention

User requires only their exact commit message, without attribution, trailers, or public release-note changes. RELEASE.md was restored to its previous contents and the commit amended and pushed with an exact force-with-lease check. Keep implementation notes in the relevant __production_steps directory.
