# Parley Docs — Index

Per-user AI agents that negotiate meeting slots with the other side's **agent — or
directly with a human** over normal email — semi-autonomously and privately. New
here? Read top to bottom — each doc assumes the ones above it.

> 🚀 **Just want to install and use Parley?** See the
> [User Guide](user-documentation/README.md). The docs below are the design record.
>
> 📖 **Need a term defined?** The [Glossary](design/glossary.md) has one-liners for every
> piece of vocabulary, each pointing to its authoritative doc.

## Reading order

Diagrams live **inside** each doc (Mermaid) — there is no separate diagram file.

| # | Doc | Folder | What it answers | Status |
|---|---|---|---|---|
| 1 | [intuition.md](vision/intuition.md) | `vision/` | **Why Parley works the way it does** — how an agent should *reason* about a negotiation, in worked examples. Start here; it's the most useful doc in the set and often runs *ahead* of the shipped code. | living, future-dev |
| 2 | [use-cases.md](design/use-cases/use-cases.md) | `design/use-cases/` | *What* the human delegates — capability use cases (UC-0…6). | design |
| 3 | [interface-use-cases.md](design/use-cases/interface-use-cases.md) | `design/use-cases/` | *Through which surface* human and agent reach each other (IUC-1…6). | design + impl |
| 4 | [agent-architecture.md](design/architecture/agent-architecture.md) | `design/architecture/` | **What runs today** — one **Brain** (LLM tool-loop) + deterministic plumbing: Gateway, ToolBelt, Guardrail, Memory, Journal, Wire. Diagrams inline. | implemented |
| 5 | [communication-protocol.md](design/architecture/communication-protocol.md) | `design/architecture/` | *How* two agents talk over email (the wire). | design |
| 6 | [client-server.md](design/architecture/client-server.md) | `design/architecture/` | Client↔Server topology + client-server API (human↔agent), incl. the CLI, tray app and setup wizard surfaces. | design + impl |
| 6b | [code-map.md](design/architecture/code-map.md) | `design/architecture/` | Where every file lives — the source-tree map. | living |
| 7 | [supported-use-cases.md](design/use-cases/supported-use-cases.md) | `design/use-cases/` | **What actually ships.** The status index. | living |
| 13 | [User Guide appendix — Backend setup](user-documentation/README.md#appendix--backend-setup-credentials-per-provider) | `user-documentation/` | Per-provider credentials reference (gmail / outlook / yahoo / classic Outlook). | living |

See [design/README.md](design/README.md) for the design set on its own.

> The acceptance test specs, the feature log and the iteration notes are kept in the
> author's working copy, not published — they name real accounts and in-flight work.

## Quick paths

- **Just want to use it** → the [User Guide](user-documentation/README.md), not this set.
- **Why it works this way** → 1.
- **What works today** → 7.
- **Implementing / reviewing code** → 4 (diagrams inline) → 5 → 6.
- **Designing a new use case or surface** → 2 → 3 (and check 1).

## Map

```
Intuition (1)
 ├─ Capability use cases (2) ──┐
 ├─ Interface use cases  (3) ──┤  describe WHAT & WHERE
 │                             │
 ├─ Agent architecture  (4) ──┐│
 ├─ Comm protocol       (5)   ├┤  describe HOW
 └─ Client–server       (6) ──┘│
                               │
 Supported use cases (7) ◀─────┘  tracks what of the above is SHIPPED
```
