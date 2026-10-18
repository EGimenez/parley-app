"""Parley — per-user agents that negotiate meeting times agent-to-agent.

Package layout (2026-08-31 refactor — see docs/iterations/refactor-plan.md):
  * core             — value types + the email wire protocol (no deps).
  * connectors/      — the reusable connector: `Account` + concrete backends
                       (gmail/outlook/yahoo/local_outlook/test_account). No
                       Parley-specific concept lives here on purpose.
  * agent/           — the Agent and what lives inside it: the single LLM Brain
                       (brain.py) deciding everything via `AccountsGate`,
                       `ShortTermMemory` (working state), `UserMessaging` (the
                       human channel), `Log` (the flight recorder, a sibling of
                       Memory, not inside it), `Policy` (data), `gateway.route`
                       and `guardrail.permission` (pure functions).
  * client_server/   — the human↔agent side: HTTP server, CLI, config, desktop app.
  * testing/         — the in-memory selftest, fakes, and the Yahoo test sender.
  * runtime          — wiring: build an Agent from config and run/serve it (main()).
"""

__version__ = "0.1.0"
