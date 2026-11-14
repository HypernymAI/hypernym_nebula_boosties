# Codex Dropship local control

Date: 2026-11-13
Plugin version: 0.7.35
Commit message: [ added better dropship local control ]
Scope: this session's local-control and tracer corrections. Original Dropship design notes remain the other agent's responsibility.

## Changes

- Added dropship_local MCP actions and skill routing for identity, computer rename, peer nickname, and nickname removal. First use records a readable default computer name without trusting a network.
- Names remain independent of persistent machine IDs and cryptographic keys. Nicknames live in a separate private file keyed by peer ID. Colliding advertised labels receive public-key fingerprint suffixes.
- LAN and pairing use the same name resolution. Renames propagate through LAN discovery and encrypted mailbox presence; local nicknames remain unchanged. Pairing refuses replacement of an existing friend's keys.
- Mailbox queues and new conversation records retain peer IDs, preventing a rename or reused label from redirecting pending sends or established threads. Session directories also carry peer IDs.
- Tracer checks restart outdated local couriers, wait for mailbox readiness, accept line-wrapped pasted tokens, and match receipts by machine ID. Both sides still need updated code; creating an invitation does not prove a remote agent is awake.
- Fixed the Dropship skill's YAML description so the skill validator can parse it.

## Verification

- Seven local-control unit checks and seven existing tracer/session/status checks passed.
- Five session-routing integration checks passed with a terminal attached.
- Real LAN rename/nickname test passed: renamed a simulated computer, nicknamed it on its peer, delivered to the exact session, and verified keys and identity survived. An initial test accidentally selected mailbox transport during LAN rediscovery; corrected the test to wait explicitly for the LAN route.
- Public-mailbox tracer integration passed again after the final stable-ID send change (50.2 seconds).
- Skill validation and git diff --check passed. Plugin validation passed with the existing missing listing-icon warning.

## Live diagnosis

The user's remote computer changed its name from fieldriser-fields-mac-studio to chris-biggun without changing machine identity. The old courier showed conflicting LAN and mailbox names; the new local courier resolves both consistently. An acknowledgement and diagnostic message reached the remote Codex session, which replied over LAN. The user confirmed the other side was still running prior code in Borg mode. No encrypted tracer receipt was confirmed for that machine; the supplied invitations expired. Update the other plugin, reconnect the client, and generate a fresh tracer to verify the long-range path.

No public release notes or attribution are added. Work was performed in the existing boosties checkout, with isolated test state under its ignored tests/dropship/.run directory.
