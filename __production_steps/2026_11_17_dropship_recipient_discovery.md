# Recipient discovery before messaging

The agent checked its own name, then sent to an unverified recipient spelling. The skill and MCP send tool now
require status lookup before first contact, exact advertised address selection, automatic resolution of a clear
name/role match, and a brief clarification only for ambiguity. Replies reuse the authenticated sender address.
Do not use discover as a refresh (it trusts the network), silently substitute a bare machine, or call submission
an acknowledgement. This updates both Claude and Codex instructions after their installed plugin is refreshed.

The receiver already kept explicit valid names from falling back. It now drops unknown explicit destinations
instead of persisting unowned mail, and rejects malformed destinations instead of converting them to bare-machine
mail. Existing stable aliases and bare-machine routing remain supported. Presence is still a bounded snapshot;
this change does not introduce live remote session verification or delivery receipts.

Validation: isolated recipient-routing regression tests and existing access tests; Python syntax and whitespace.
Commit message: [ proper discovery before messaging ]
