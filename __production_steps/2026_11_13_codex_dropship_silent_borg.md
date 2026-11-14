# Codex Dropship silent Borg waiting

Date: 2026-11-13
Plugin: 0.7.36
Commit message: [ made dropship borg frictionless ]

The user observed a token-consuming loop: a 60-second dropship_wait returned no mail, Codex narrated that it was still listening, and called wait again. Borg now ignores finite wait arguments and sleeps inside a single subprocess. It returns on real mail, cancellation/off, or one scheduled renewal after 23 hours 30 minutes. The user explicitly accepted the renewal as a compromise. The plugin declares a 24-hour host tool timeout, leaving a 30-minute margin. Renewal instructs one new direct call without commentary; real host errors/timeouts must not trigger an automatic retry loop.

Unchanged inboxes are no longer reread and rewritten every half-second. MCP cancellation and disconnect stop the child. Ordinary explicitly bounded waits remain bounded outside Borg. Receiving messages and entering/renewing a tool call still involve model turns; the local pending wait does not make model calls.

Codex's code-mode wrapper can itself yield and invoke the model while a nested tool waits. Quiet Borg therefore requires direct tool exposure. This computer's config was backed up and features.code_mode.direct_only_tool_namespaces was set to ["mcp__hypernym"], preserving all other settings. Codex accepted the configuration. A restart is required to activate it in existing sessions. The skill includes the same merge-preserving setup for other computers; it forbids falling back to token-consuming exec/wait polling when direct exposure is unavailable. Host working timers are not token counters.

References checked:
- https://raw.githubusercontent.com/openai/codex/main/codex-rs/core/config.schema.json (direct-only namespace setting)
- https://raw.githubusercontent.com/openai/codex/main/codex-rs/codex-mcp/src/plugin_config.rs (plugin MCP config normalization)
- https://learn.chatgpt.com/docs/extend/mcp?surface=cli (host tool timeout)

Verification:
- Real MCP test stayed silent for 65 seconds despite a requested 1-second Borg timeout, then returned exactly one response on mail. Cancellation and Borg-off returned promptly.
- Final four wait integration checks passed, covering bounded waits, concurrent send during wait, cancellation, and silent Borg delivery/off.
- Two Borg integration checks passed before renewal was added; five fast-clock checks verify renewal at 84600 seconds, bounded one-off waits, immediate mail, disabled Dropship, and absence of a short subprocess timeout.
- Seven tracer checks and seven local-control checks passed.
- Skill validation, plugin validation, and git diff --check passed; plugin validation retains its existing missing listing-icon warning.
- Initial isolated integration failures exposed inherited real session IDs and pre-existing fixture dependencies; test machines now clear those environment IDs and create their own needed session. An old bounded-wait wording assertion was updated to the explicit stop-without-retry result.

No live Codex token-meter test or multi-day soak has been performed. The other computer still needs the plugin update and direct-tool configuration followed by a reconnect. No public release notes or attribution are added.
