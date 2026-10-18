# Parley — Glossary

One-line definitions for the vocabulary used across the design docs and code. The
**Where** column points to the authoritative source (which carries the full treatment).
Terms marked *(target)* describe designs not yet in the running code.

## Channels & topology

| Term | Meaning | Where |
|---|---|---|
| **Agent** | One per user; the composition root — accounts, poll loop, human gate, client-server bindings. Since iteration 08 it *decides nothing*: the Brain does. | [architecture](architecture/agent-architecture.md) |
| **Account** | The provider-agnostic I/O surface for **one** mailbox + calendar + contacts. Knows nothing about negotiation. One agent can drive several. | [architecture §Account](architecture/agent-architecture.md) |
| **Client–server channel** | Human ↔ their *own* agent: the CLI/`instruct`, the tray, the human gate. **Bidirectional**: the agent asks (`/v1/pending` + a tray toast) and the human answers (`/v1/decide`/`answer`). All over HTTP — an earlier email-to-self variant was retired 2026-08-31. | [client-server](architecture/client-server.md) |
| **Peer-to-peer** | Agent ↔ agent (and real human invites): ordinary emails carrying a data block. Never conflated with the client-server channel. | [communication-protocol §10](architecture/communication-protocol.md) |
| **The inbox is the bus** | There's no shared broker — agents negotiate by sending each other normal email. | [README](../../README.md) |
| **Backend** | A concrete account: `gmail` (Google API), `outlook` (MS Graph), `yahoo` (IMAP/SMTP + CalDAV calendar), `local-outlook` (classic Outlook COM), `memory` (credential-free in-process; drives the selftest). | [README](../../README.md) |

## The brain (agent internals)

| Term | Meaning | Where |
|---|---|---|
| **Brain** | **The one decision-maker** (iteration 08): a single LLM tool-use loop, one turn per admitted event, reading policy prose + learned guidance + a summary of all open negotiations. Replaces the old dispatcher/Orchestrator pair. | [architecture §Brain](architecture/agent-architecture.md) |
| **Gateway** | The relevance funnel, two-stage (iteration 12): deterministic rules (wire/invite admit, bulk drop, keyword) + an optional cheap **LLM classifier** on the residual plain mail — fail-open, cached, journaled. Trust lives in the Guardrail, not here. | [architecture §Gateway](architecture/agent-architecture.md) |
| **LLM port / `LLMClient`** | The provider-agnostic seam the brain talks to (`complete(system, transcript, tools)`); the **only** vendor-SDK import. Adapters: Anthropic (live), `FakeLLMClient` (token-free selftest). No agent framework. | `agent/llm_port.py` |
| **ToolBelt / Tool** | The brain's hands: **thin, judgment-free** verbs (10 model-facing tools), each taking an explicit `conn`, writes gated by the Guardrail. Owns the two infra guarantees: the one timezone conversion (`_when`) and idempotent settlement (`create_event`). | [architecture §ToolBelt](architecture/agent-architecture.md) |
| **Intent continuity** | A user's meeting ask becomes a durable negotiation object the moment it exists (`open_negotiation`, before anything is sent), and turns never end on a plain-text question (`ask_user`, self-contained) — so "book it anyway" and "the F1 meeting" resolve in any later turn or session. | [architecture §Brain](architecture/agent-architecture.md) |
| **NegotiationMemory** | Working state across concurrent negotiations: summary each turn, full detail on demand (`get_negotiation`); persisted (`negotiations.json`) until closed; terminal ids retained as idempotency **tombstones**. | [architecture §NegotiationMemory](architecture/agent-architecture.md) |
| **Journal** | The flight recorder (iteration 10): every message received + every decision made → one JSON line, daily file under `~/.parley/logs/<agent>/`; no silent drops; never crashes a turn. | [architecture §Journal](architecture/agent-architecture.md) |
| **Wire** | The deterministic peer-to-peer encode/parse (`wire.py`): four message types (`meeting_request`, `request_confirmed`, `proposal`, `decline`), invite → `via_invite` normalization, deterministic `inv-` ids. No policy, no choreography. | [architecture §Wire](architecture/agent-architecture.md) |
| **Instruction (client-server)** | Free text only ("set a meeting with x before friday") — `/v1/instruct {text}`; the brain comprehends, plans, and acts in one turn. There is no structured-intent form and no keyword fallback. | [client-server §3](architecture/client-server.md) |
| **Learned policy** | Standing guidance the user gives while answering ("from now on keep Fridays free"), persisted by the brain's `remember_policy` to `policy_learned.md` and applied with the same weight as the cfg policy — override-as-training-data, running today. | [architecture §Policy](architecture/agent-architecture.md) |

## Policy & gating

| Term | Meaning | Where |
|---|---|---|
| **Policy** | Decision **data**, not code, per account: `{ structured, unstructured }` + the learned file. | [architecture §Policy](architecture/agent-architecture.md) |
| **structured / unstructured** | `structured` = machine rules (the guardrail seam); `unstructured` = free prose the brain follows (*when to act vs. ask*). | [architecture §Policy](architecture/agent-architecture.md) |
| **Guardrail** | **The one permission model** (iteration 12): given an `Action` — intake (`handle_inbound`) or any tool call — returns **ALLOW \| ASK \| DENY** from the trust table. Empty = ALLOW all; the table only narrows. The independent *authority* next to the brain's *judgment*. | [architecture §Guardrail](architecture/agent-architecture.md) |
| **Trust table** | Per-account `principal → auto \| ask \| ignore` (+ per-verb overrides) in `Policy.structured["trust"]` — one place answering "what may this person trigger?". Table beats prose; `accept_requests_from`/`allow_from` fold in as shims. | [architecture §Guardrail](architecture/agent-architecture.md) |
| **One-shot grant** | A human's affirmative answer to a guardrail ASK arms exactly one retry of that (account, verb, principal) action — consumed on use, never standing. | [architecture §Guardrail](architecture/agent-architecture.md) |
| **Action** | The unit the guardrail rules on: `{ kind, conn, target, title, slot, payload }`. | `agent/guardrail.py` |
| **Escalate / human gate** | When the brain won't decide on its own (policy prose says ask, or policy is silent on a commitment), it parks the question (`awaiting`, **one ask per thing**) and the human answers on any surface (**first answer wins**). | [architecture §Agent](architecture/agent-architecture.md) |
| **Semi-negotiating** | The MVP posture: agents negotiate slots autonomously, but **commitment is human-gated**. The core design move. | [intuition](../vision/intuition.md) |

## Wire & identity

| Term | Meaning | Where |
|---|---|---|
| **PARLEY block / sidecar** | The machine payload in a peer-to-peer email. Today a JSON block in the body (`-----BEGIN PARLEY-----`). *(target: iCalendar `X-PARLEY-*` properties — tamper-resistant.)* | [communication-protocol §6](architecture/communication-protocol.md) |
| **iCalUID / meeting_uid** *(target)* | The stable identity of a meeting object, consistent across providers and attendees' copies. Identity is *carried*, never re-derived. | [communication-protocol §2](architecture/communication-protocol.md) |
| **negotiation_id** | Stable id for a whole exchange, minted by the asker (or derived deterministically for invites: `inv-<hash(organizer|slot)>`), correlating every message to its negotiation with no LLM guess. | [architecture §Wire](architecture/agent-architecture.md) |
| **question_id / ref** | Correlates an `ask_user` question to the human's client-server answer (the `ref:<id>` marker on `/v1/decide`/`answer`), so one question resolves exactly once. | [architecture §Agent](architecture/agent-architecture.md) |
| **via_invite** | A real calendar invite normalized into a `meeting_request` with this flag set: the slot is **fixed** (RSVP or decline, don't negotiate it); acceptance is still policy's call. | [architecture §Wire](architecture/agent-architecture.md) |
| **NEGOTIATE / COMMIT phase** *(target)* | Negotiation (non-binding, mutates no calendar) vs. commitment (binding, `CONFIRM`). Decoupling them is the core privacy/safety move; today's code can still write mid-exchange. | [communication-protocol §4](architecture/communication-protocol.md) |
| **State catalogue** *(target)* | The end-to-end lifecycle of one negotiation object: `DRAFT → CLARIFYING/SOURCING/EVALUATING → NEGOTIATING → AWAITING_USER → TENTATIVELY_AGREED → CONFIRMING → BOOKED`, with `STALLED`/`FAILED`/`DECLINED`/`CANCELLED`/`SUPERSEDED`. Spans both channels; transitions labeled `CS`/`P2P`. | [communication-protocol §4.3](architecture/communication-protocol.md) |
| **Counterpart visibility** *(target)* | Whether the Agent can read a counterpart's free/busy: `VISIBLE` (corporate-style colleague availability — future) vs `NONE` (today: ask over email). Branches `SOURCING` on a broad instruction. | [communication-protocol §4.2](architecture/communication-protocol.md) |
| **TOPIC_REQUEST / AVAILABILITY_QUERY** *(target)* | Pre-proposal peer messages: willingness ask ("meet to discuss T?") and slot ask ("what works before Friday?") — negotiate without offering slots yet. | [communication-protocol §5.1](architecture/communication-protocol.md) |
| **NUDGE / WITHDRAW** *(target)* | Timeout reminder to a silent counterpart / retraction of our own still-unanswered proposal. | [communication-protocol §5.1](architecture/communication-protocol.md) |
| **SUPERSEDED / human wins** *(target)* | Terminal state when the human acted directly (manual accept/move/cancel, UC-5): reachable from any state, always outranks the agent, override captured. | [communication-protocol §4.3](architecture/communication-protocol.md) |
| **Graceful degradation** | The first peer-to-peer message is a valid plain invite / readable prose, so a counterpart with **no** agent can still accept/decline natively. | [communication-protocol §7](architecture/communication-protocol.md) |

## Reliability & infra guarantees

| Term | Meaning | Where |
|---|---|---|
| **Slot / Event / Message** | The dependency-free value types in `core.py`: a time window, a calendar entry, an email. | `core.py` |
| **Settlement is infra** | Closing a negotiation as booked/declined happens at the calendar write / confirmation, by deterministic code — never left to the model. Terminal ids are kept as tombstones; repeats drop. | [architecture §ToolBelt](architecture/agent-architecture.md) |
| **`_when` / one tz conversion** | The single place timezone math happens (iteration 09): model passes times verbatim; infra interprets local times in the calendar's zone. Unresolvable zone = loud failure + a startup probe (iteration 11). | [architecture §ToolBelt](architecture/agent-architecture.md) |
| **Auto-RSVP** | An arriving invite matching an already-settled booking (same peer + instants) is RSVP'd by infra and never re-decided by the brain (iteration 11). | [architecture §Inbound dispatch](architecture/agent-architecture.md) |
| **Durable intake cursor** | Per-backend persisted "what's new since X" (`historyId`+`refreshed_at` / `deltaLink` / last `ReceivedTime`) so a restart doesn't skip mail received while down. | [architecture §Account](architecture/agent-architecture.md) |
| **catch_up** | How far back to read when there's no usable cursor (`"7d"`/`"24h"`/`"today"`/`"none"`, default `"1d"`). | [README Backend setup](../../README.md) |

## Design principles (from the brief)

| Term | Meaning | Where |
|---|---|---|
| **Information-minimized exchange** | Agents find a slot without exposing each other's calendars — **inward-full, outward-minimized**: the brain sees its own user's data; the wire carries only offered slots and answers. | [architecture §Privacy](architecture/agent-architecture.md) |
| **Override-as-training-data** | When the human overrides or guides the agent, the signal is captured — today as learned policy (`remember_policy`); target: guardrail/gateway training. No flow discards it. | [architecture §Policy](architecture/agent-architecture.md) |
| **Determinism is the funnel; the LLM at the narrow end** | The deterministic gateway/wire/infra guards bound what the LLM sees and can do; the model supplies judgment, not authority. | [architecture §floor](architecture/agent-architecture.md) |
