# Parley — Design

The design record for Parley: per-user AI agents that negotiate meeting times
**agent-to-agent**, semi-autonomously and privately. Start with the **goal**, then the
three design docs describe the two channels and the wire between agents.

| Doc | What it covers |
|---|---|
| [intuition.md](../vision/intuition.md) | **Why Parley exists and how an agent should reason** — the three core design moves (semi-negotiating, information-minimized, override-as-training-data) worked through in examples. The north star the designs below serve. |
| [agent-architecture.md](architecture/agent-architecture.md) | **What runs today.** One **Brain** (a single LLM tool-loop, iteration 08) + deterministic plumbing: Gateway, ToolBelt, Guardrail, NegotiationMemory, Journal, Wire. Diagrams inline. |
| [agent-architecture-future-development.md](../iterations/agent-architecture-future-development.md) | **Snapshot survey** (now in `docs/iterations/`, current up to ~iteration 20): the remaining architecture simplifications — everything-is-an-event, one timer, one store + one log, settlement-is-infra, account facets — plus the wave sequencing. (Wave 1 — permission model, trust table, gateway classifier — shipped in iteration 12 and lives in the as-built doc.) |
| [client-server.md](architecture/client-server.md) | The **human ↔ own agent** channel: clients, the local server, the API, the human gate. |
| [communication-protocol.md](architecture/communication-protocol.md) | The **agent ↔ agent** wire: object identity (`iCalUID`), the end-to-end state machine (all states, both channels), the email serialization. |
| [communication-protocol-future-development.md](../iterations/communication-protocol-future-development.md) | **Snapshot survey** (now in `docs/iterations/`, current up to ~iteration 20): the same behavior compressed — 4 states × attributes, 4 peer verbs, ~6 tools — with a lossless mapping back to the catalogue. |
| [glossary.md](glossary.md) | One-line definitions for every term, each pointing to its authoritative doc. |
| [use-cases/](use-cases/) | The scenarios Parley must handle (capability + interface use cases) and [what actually ships](use-cases/supported-use-cases.md). |

**Two channels, never conflated:** *client-server* (a human and their own agent) and
*peer-to-peer* (an agent and another agent). Every design doc keeps them separate; so does
the code. Diagrams live **inside** each doc — there is no separate diagram file.

> The `architecture/` folder name is historical; the three docs above are the design set.
