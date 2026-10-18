# Agent Architecture — Future Development: the simplification survey

> **Snapshot (written iter 12; status refreshed 2026-07-24).** A point-in-time planning
> record, not current direction. **Shipped since (now as-built in
> [`agent-architecture.md`](../design/architecture/agent-architecture.md)):** wave 1
> (iter 12); the **§4 tool collapse 16→8** (`send`/`apply`/`check`/`lookup` + memory verbs,
> iter 21); the **stall-nudge timer** and **override capture** (iter 21); and the hidden
> HTML-comment carrier from human-readable messaging (2026-07-22). **Still target:** the
> full S6 event bus, S8 one-store, S9/S11 surfaces, and the explicit `phase` write-guard.
> Deep per-section trimming still owed (iteration-20 open-task #4).
>
> Companion to [`agent-architecture.md`](../design/architecture/agent-architecture.md) (the as-built agent) and
> to [`communication-protocol-future-development.md`](communication-protocol-future-development.md)
> (the compact machine + the wire survey). This doc holds the **architecture / runtime /
> surfaces** side of the survey — the items *not yet built* — plus the joint
> **sequencing table** and the **what-not-to-simplify** list.
>
> Status: **exploration / target.** Every item: *Today → Simplify to → Buys / Risk*,
> with "today" naming the real mechanism in `src/parley`. Iteration 08 set the
> direction (one brain, thin tools, infra-owned guarantees); **wave 1 (S4 · S5 · S12 ·
> S15) shipped in iteration 12** and now lives in the as-built doc — summarized below,
> no longer part of the survey.

---

## Shipped — wave 1 (iteration 12)

Formerly survey items, now current architecture (authoritative treatment:
[`agent-architecture.md`](../design/architecture/agent-architecture.md)):

| Was | Now (as-built) | Where |
|---|---|---|
| S4 one permission model | the Guardrail rules **intake** (`handle_inbound`) and **every tool call** `ALLOW \| ASK \| DENY`; a tool ASK escalates (never a silent hold) and an affirmative answer grants a **one-shot** approval | [§Guardrail](../design/architecture/agent-architecture.md) |
| S5 trust table | `policy.trust`: `principal → auto \| ask \| ignore` + per-verb overrides; table beats prose; `accept_requests_from`/`allow_from` read as shims | [§Guardrail](../design/architecture/agent-architecture.md), [§Policy](../design/architecture/agent-architecture.md) |
| S12 policy shape | prose (brain) + trust table (guardrail) + shims; dead vocabulary deleted (`Resolution`, `parse_hhmm`, `ACCEPT`/`ESCALATE`) | [§Policy](../design/architecture/agent-architecture.md) |
| S15 gateway classifier | two-stage relevance: deterministic rules, then an optional small-model ADMIT/IGNORE on the residual — fail-open, cached, journaled | [§Gateway](../design/architecture/agent-architecture.md) |

Also groundwork: `human_decision` journal records carry *(principal, verb, verdict)* —
the raw material for suggesting ask→auto trust graduations (still future: the
suggestion itself).

---

## Since wave 1 — iterations 13–20 (the drift this survey must answer)

This survey was written at iteration 12. No full wave-2+ **survey item** has shipped
since; instead, **iterations 13–19 each fixed a live bug by *adding* a special case** — a
guard in infra plus a paragraph in `brain._SYSTEM` — and **iteration 20 (in progress)**
began the correction, shipping the log-proven fault fixes first:

| Iter | Added | Where it landed |
|---|---|---|
| 14 | `open_negotiation` — intent gets a durable id immediately (continuity) | a tool + prompt rule |
| 15 | one-email direct booking ("the invite IS the message; no `confirm_meeting` after") | a "Who books" prompt paragraph |
| 17 | conflict guard in `create_event`; `rsvp_reply` fates; `must_rsvp`; Yahoo CalDAV | infra guards + prompt |
| 18 | `silent_inaction` backstop (a request turn with no action → escalate) | infra + prompt |
| 19 | answers resume with original-mail ground truth; `counter_invite_hold` | infra + prompt |
| **20** *(in progress)* | event-targeting; decline detection; RSVP-not-landed → infra; check(who) seam + availability email; `_SYSTEM` tool map; **resumable negotiations** (`defer_action`/follow-up, Issue 2b); **stall timer** (nudge→escalate); Issue 1 responder-RSVP + Issue 2a fetch/gateway-logging | tools + wire + prompt + infra |

The direction of travel through iter 19 was the **opposite of this survey's** — more
choreography, concentrated in the tool surface and the ~80-line system prompt. Iteration
20 turns toward the survey: its remaining stages are the actual simplifications (wire v2,
the tool collapse, the events/store/timer runtime). The item that most directly relieves
the accumulated complexity is still the tool-surface collapse
(§4 / the [assessment](#simplification-assessment--2026-07-06) below); the felt pain
("the agent gets confused") lives in *how many overlapping tools the prompt must
adjudicate*, which §4 addresses head-on.

---

## Simplification assessment — 2026-07-06

A review prompted by the felt problem: *"the agents don't work properly; there's too
much complexity; the agent gets confused."* Three components were questioned by name.

### Verdict on the three hypotheses

| Component | Verdict | Why |
|---|---|---|
| `llm_port` | **KEEP** | It is the *only* place the Anthropic SDK is imported, and `FakeLLMClient` is the seam that lets the whole brain loop run token-free under all 22 `--selftest` scenarios. It never touches the system prompt, so it **cannot** be the confusion source. Only cleanup: `_dumps` (llm_port.py) is dead code. |
| `wire` | **KEEP** | It is the agent↔agent interop contract (the `-----BEGIN PARLEY-----` block + normalizing a real calendar invite into a `meeting_request`) **and** the privacy boundary (§Privacy) — arguably the core of the product thesis. Stateless; its four builders are each called once from `tools.py`. The redundancy one *feels* looking at wire is really the **five peer-send tools** that wrap it 1:1 — the tool surface, not wire. |
| tools + `brain._SYSTEM` | **THE PROBLEM** | 16 tools, and an ~80-line system prompt whose bulk adjudicates *choreography*: asker vs answerer, direct-booking vs wire-negotiation, `via_invite` vs `meeting_request` vs `request_confirmed`, "don't send a second email". Choosing among five overlapping "send" tools and a dual-role `create_event`, under a prompt that keeps carving exceptions, is exactly what a model fumbles. |

The confusion is not the *number* of modules; it is the number of **overlapping tools
the prompt must disambiguate**, worsened by every iter-13–19 exception (see [Since wave 1](#since-wave-1--iterations-1320-the-drift-this-survey-must-answer)).

### Proposed target — collapse 16 tools → ~6 (recorded, not executed here)

This is the survey's own [protocol §4](communication-protocol-future-development.md#4-actions--6-tools--infra)
made concrete against today's `brain.py` `TOOLS`:

| Today (16) | Collapse to |
|---|---|
| `send_meeting_request` · `propose_time` · `decline_meeting` · `confirm_meeting` | `send(verb, payload)` — one gated peer tool; verbs OFFER / ACCEPT / DECLINE / CANCEL (§3) |
| `create_event` · `move_event` · `delete_event` (+ infra auto-RSVP) | `apply(change)` — one binding calendar choke point (create / move / cancel / rsvp) |
| `check_availability` (+ future counterpart availability) | `check(window, who?)` — the `who` is the visibility branch (S14) |
| `resolve_contact` | `lookup(name)` |
| `send_email` (plain) + a status report | `tell_user` / plain send |
| `ask_user` | `ask_user` (unchanged) |
| `open_negotiation` · `cancel_negotiation` · `get_negotiation` · `remember_policy` · `find_events` | keep as memory/read helpers; revisit under wave-3's one-store |

**Why this relieves the pain, not just the count:** the prompt's largest block ("Who
books — you own the meeting") exists *only* to disambiguate the five send tools plus
`create_event`'s asker/answerer double life. A single `send(verb)` + a single
`apply(change)` let that block shrink to the one rule that actually matters — *proposals
never mutate; the asker owns the event* — with `binding` on the wire carrying the
negotiate→commit flip instead of the prompt inferring it from tool names.

**Scope note:** this rides the wire-v2 verb set (§3), so it is a scoped **wave-2**
iteration with the §5 mapping table as its back-compat shim and the 22-check selftest as
its safety net — **not** a drive-by edit. It is the recommended next build.

### The negotiate path, fully specified (worked example)

The one decision that governs everything is **is a time fixed?** — because it selects
the *phase*, and the phase (not a tool name) decides whether the calendar is touched.

| User says | Phase | Behaviour |
|---|---|---|
| "meet x@y **tomorrow at 15:00**" | **commit** | time is the commitment → one `apply(create)`; the invite *is* the message, no peer round-trip. |
| "meet x@y **tomorrow**" (day only) | **negotiate** | no agreed time → the agent reaches out to x@y **first**, never books yet. |

When the phase is **negotiate**, the agent picks one of three moves — and this is where
the *counterpart-visibility* branch (S14) lives:

1. **We can read the peer's free/busy** — `check(window, who=x@y)` answers → the brain
   proposes concrete slot(s): `send(OFFER, { slots:[…] })`.
2. **We cannot** — `check(window, who=x@y)` returns **NULL / FAIL** (no free/busy access,
   or the peer has no agent at all) → the polite, correct move is a **plain-language
   email to x@y** that introduces the agent and asks for availability over a range,
   e.g.:

   > *"Hi — I'm Alex's Parley scheduling agent. Alex would like to meet you tomorrow
   > to discuss **Topic T**. Do you have any availability between **09:00 and 18:00
   > (Europe/Madrid)**? Reply with a time or two that suit you and I'll set it up."*

   The reply is free text; the brain reads it and maps it onto a verb (an `OFFER` back, an
   `ACCEPT`, a `DECLINE`), then either **iterates** (propose/agree) or **commits**
   (`apply(create)` + send the invite). This is the graceful-degradation guarantee:
   *the first message is always valid to a human with no agent* (§13 keeper).
3. **Peer replies / counters** — normal negotiation continues on the wire until a slot is
   agreed, then the asker commits.

In the compact model both outbound forms are the **same verb**: `send(OFFER, { slots })`
for a concrete proposal, `send(OFFER, { window, slots:[] })` for the availability
question — the second rendered as prose (+ header sidecar, §2) so an agentless human can
read and answer it. One tool, two payload shapes; no separate "ask availability" tool.

**Buildable now, honestly stubbed:** counterpart free/busy is future work, but the
*branch* need not wait — a stub `check(window, who)` that returns `NULL/FAIL` for any
non-self `who` makes case 2 the live path today, exercisable in the selftest, and real
free/busy later just fills the same seam without touching the brain.

**Open coupling — the silent counterpart.** Case 2 sends an email a human may never
answer. Nothing today wakes that stalled negotiation (`awaiting_peer` has no timeout).
That is exactly the gap **[S7 — One timer](#s7--one-timer)** exists to close, and it is
why the timer and this graceful-degradation path should be designed together, not apart.

### Dead code (removed, iteration 20)

- `llm_port._dumps` — deleted (was never called).
- `Account.find_event` — deleted from the base and all five backends
  (gmail/outlook/local_outlook/memory/yahoo); it was never called, superseded by
  `events_overlapping` + the `find_events` tool.

**Shipped from this assessment (iteration 20, in progress — selftest 27/27):**
- **Prompt** — a "which tool" selection map + a reschedule-collision recipe now head
  `brain._SYSTEM` (the legibility half of §4, without the risky tool merge).
- **Event-targeting** — `move`/`delete` identify the event by title+attendee, not slot
  alone (the collision bug that moved the wrong meeting).
- **Decline detection** — a plain "Declined:" notification is parsed as an `rsvp_reply`
  and correlated by (peer, topic); no longer a silent gateway drop.
- **RSVP-not-landed → infra** (Fault 3) — a not-yet-materialized invite raises
  `InviteNotReady`; the RSVP is deferred and completed by the poll loop, never retried by
  the model into `MAX_STEPS`.
- **Counterpart-visibility seam + availability email** (stages C+G) —
  `check_availability(who=peer)` returns `accessible:false`; the brain emails for times
  instead of guessing. Real peer free/busy later fills the same seam.
- **Resumable / conditional negotiations** (stages D/E, lean form) — `defer_action` records
  a follow-up on a blocked negotiation, surfaced every turn; when the unblocking event
  arrives the brain finishes it. Fixes Issue 2b ("reschedule with X, then accept Y").
- **Stall timer** (stage F) — the poll loop wakes the brain to nudge a silent
  `meeting_request` once, then escalates; a `stalls` counter prevents per-poll re-firing.
- **Live-test fixes** — Issue 1 (responder RSVPs the tentative hold → organizer notified,
  no duplicate) and Issue 2a (gmail keeps self-addressed mail without INBOX; every gateway
  verdict journaled).
- **Hygiene** — the brain/gateway/port no longer fall back to an ambient
  `ANTHROPIC_API_KEY` env var; the cfg is the one source of the key (iteration 07-0).

**Still pending** — the *maximal* structural refactor: the 16→~6 tool merge, the
`create_event`→`book`/`rsvp` split, wire v2 (A), and the **full** one-store-keyed-by-ref +
event-bus rewrite (D/E in their survey form — the lean `follow_up`/timer above delivers
the Issue-2b capability without it). Plus the dead-code deletions above.

---

## A. Runtime

### S6 — Everything is an event

**Today** `Agent.handle()` is a hand-rolled pipeline of ordered steps:
self-instruction → own-`[PARLEY-ASK]` echo → self-answer (live or stale) →
terminal-negotiation drop → settled-invite auto-RSVP → trust verdict →
relevance → brain → `request_confirmed` settle. Timers don't exist; calendar changes
(UC-5) have no entry point at all; HTTP calls (`/v1/instruct`, `/v1/decide`) enter
through separate methods.

**Simplify to** four event kinds — **peer message · user message · timer fired ·
calendar delta** — flowing through one queue into one `TURN` (the compact machine's
loop). Each `handle()` special case becomes either an event annotation (self-answer =
user message with a `ref`) or an infra guard that runs before enqueueing (terminal
drop, auto-RSVP, trust). Turns are serialized **per ref** so concurrent events on one
negotiation can't interleave.

- **Buys:** UC-5 (human steps in) becomes "one more event kind", not a new subsystem;
  the HTTP and poll paths converge; the pipeline's order-dependent special cases turn
  into declared, testable guards.
- **Risk:** per-ref ordering and a queue introduce real concurrency machinery where
  today a single poll loop suffices — worth it only together with S8's store.

### S7 — One timer

**Today** there are no timers: a silent counterpart stalls a negotiation forever
(`awaiting_peer` has no timeout), deadlines ("before Friday") live only in prose, and
the catalogue's `STALLED`/`NUDGE`/deadline-propagation have no mechanism.

**Simplify to** one field: every `WAITING` state carries `until`; when it passes,
infra emits a **timer event** (S6) and the brain decides (nudge? escalate? fail with
alternatives?). A deadline is just the outermost `until`; a UC-3 child negotiation
**inherits** the parent's — propagation for free.

- **Buys:** `STALLED`, `NUDGE`, deadline handling, and cascade urgency all fall out of
  one concept; no separate scheduler abstractions.
- **Risk:** ~none structurally; the judgment of *what to do* on expiry stays policy.

### S8 — One store + one log

**Today** four stores with different lifecycles: `NegotiationMemory`
(`negotiations.json` — persisted, but **keyed to the first account's directory**
even for multi-account agents), `Agent.awaiting` (**in-memory only — a restart
forgets every pending question**, and the human's [PARLEY-ASK] email then dangles;
`Agent.grants` shares this fate), `policy_learned.md`, and the Journal.

**Simplify to** one **object store keyed by `ref`** holding each negotiation's state +
attributes + history; `awaiting` becomes the query `state=WAITING AND on=user` (which
is also `/v1/pending`); the Journal stays what it is — the append-only record of every
event and mutation — making **crash recovery = load store, replay tail**.

- **Buys:** escalations and grants survive restarts (fixes a real gap, not just
  aesthetics); no cross-store drift; one path per agent (not per first-account);
  `/v1/pending` stops being a separate data structure.
- **Risk:** the deepest refactor in the survey — touches memory, agent, server, and
  the selftest; do it with S6, not before.

### S13 — Settlement is infra (finish the thought)

**Today** already half-true, and *more* than half since iteration 17: `create_event`
settles + tombstones, `is_terminal` drops repeats, `_rsvp_settled` auto-RSVPs an invite
matching a settled booking, and iter 17 moved **`rsvp_reply` fates into infra** — an
accepted invite is recorded without a model turn; a declined one reopens the negotiation
and wakes the brain with an infra note (`Agent._on_rsvp_reply`). But the *responder's*
settle still leaks into `handle()`'s tail (`agent.py:343–351`, `request_confirmed`
upsert+close), and the brain still participates in RSVP choreography — so the thought is
not yet finished.

**Simplify to** a rule: **the brain's world ends at `apply`** (the one binding tool).
Everything after a commitment exists — RSVP tracking, counterpart copies, tombstones,
resend absorption — is infra that runs without a model turn.

- **Buys:** fewer brain cases to prompt for (the §brain system-prompt paragraphs about
  `request_confirmed` shrink); settlement bugs become deterministic-code bugs, testable
  without a model.
- **Risk:** infra must handle the genuinely ambiguous post-commit cases (counterpart
  declines *after* booking) by raising an **event** for the brain, not by guessing.

---

## B. Surfaces

### S9 — The Account surface mirrors the tool surface

**Today** `Account` asks each backend for ~11 methods (`connect`, `address`,
`timezone`, `fetch_new_messages`, `send`, `events_overlapping`, `move_event`,
`create_event`, `delete_event`, `resolve_contact`, `accept_request`). (The vestigial
`find_event` was deleted in iteration 20.)

**Simplify to** four facets, using the **same `change` vocabulary as the brain's
`apply` tool** (protocol doc Part I §4): `mail.fetch() / mail.send(msg)`,
`calendar.read(window)`, `calendar.apply(change)` (create / move / cancel / rsvp),
`directory.lookup(name)`.

- **Buys:** a new backend is four functions; the binding choke point reads identically
  at tool level and provider level; vestiges (`find_event`) die by construction.
- **Risk:** provider quirks concentrate inside `calendar.apply` — the `change`
  vocabulary must be chosen once, well (Graph vs Google RSVP semantics differ; Yahoo
  reads/writes its calendar over CalDAV since iteration 17, degrading *loudly* to
  mail-only only when CalDAV discovery fails — so `read` must carry that
  unsupported-when-degraded signal honestly, per backend).

### S11 — Clients are renderers

**Today** already close: `Client` is the shared HTTP base; the CLI, the desktop popup,
and email-to-self all reduce to *send text in, render a question/report out* — but
each grew its own path (the popup can't take free-text guidance; email can).

**Simplify to** the contract: a client **sends `SAY`** and **renders `ASK` / `TELL`**
(the compact machine's client-server verbs). Feature-parity (free-text everywhere)
follows from the contract instead of per-surface work.

- **Buys:** new surfaces (chat bot, web, Claude skill) are rendering exercises; the
  guidance channel stops being email-only.
- **Risk:** none real.

---

## What NOT to simplify

Complexity that earns its keep — do not fold these away:

- **The two-channel trust asymmetry.** Your human and a counterpart must never share
  an envelope or a trust level; the human gate depends on the distinction. (One
  *permission model* — wave 1 — is fine; one *channel* is not.)
- **The Guardrail as independent authority.** Wave 1 made it real (the trust table);
  the seam stays the point: the brain has judgment, the guardrail has authority, and
  they must remain separable.
- **UID-carried identity** — never re-derived (protocol §0's bug class).
- **Journal no-silent-drops** — observability is a guarantee, not a convenience.
- **Privacy at the wire** — inward-full, outward-minimized; nothing in any
  simplification may widen what leaves the agent.
- **Deterministic correlation** — an inbound is matched to its negotiation by id, never
  by the model's memory.
- **Loud failure over silent fallback** — no default model, no tz guess, no quiet
  double-book. (The gateway classifier's fail-open is *to a deterministic heuristic*,
  never to inaction or a guess.)
- **One ask per thing · first answer wins · one-shot grants** — the human-gate
  invariants.

---

## Sequencing — the waves

The joint order for both surveys (protocol Part II items included). Each wave is
independently shippable; later waves assume earlier ones.

| Wave | Items | Nature | Status |
|---|---|---|---|
| **0 — framing** | S10 one primitive · S14 binary visibility · S7 (concept) | docs only | ✅ adopted by the two survey docs |
| **1 — policy pass** | S4 one permission model · S5 trust table · S12 policy shape · S15 LLM gateway classifier | guardrail + gateway + config | ✅ **shipped — iteration 12** (selftest 17/17; live intake-ASK + classifier runs pending) |
| **2 — wire v2** | S1 one ref · S2 one serialization · S3 verbs+attrs · **§4 tool collapse** | protocol change, back-compat shim (§5 mapping) | 🟡 **partly (iter 21):** §4 tool collapse 16→8 ✅ · S1 one `ref` ✅ · S2 partly (hidden HTML comment, not yet iCal `X-PARLEY-*`) · S3 verbs-as-wire-format still open |
| **3 — runtime** | S6 events · S8 one store+log · S13 settlement infra · S7 (mechanism) | the deep refactor | 🟡 **partly:** S13 settlement mostly infra (iter 17) · S7 stall timer shipped (iter 21) · S6 event bus + S8 one-store still open (awaiting/grants restart-safe since S8 groundwork) |
| **4 — surfaces** | S9 account facets · S11 client contract | steady-state cleanup | ⬜ |

No full wave-2+ survey item has shipped since wave 1. Iterations **13–19** landed bug-fix
choreography; **iteration 20 (in progress)** shipped the log-proven fault fixes
(event-targeting, decline detection, the `_SYSTEM` tool map) and carries the wave-2/3
structural work in its later stages (see [Since wave 1](#since-wave-1--iterations-1320-the-drift-this-survey-must-answer)).
Records: wave 1 —
`iteration_2026_07_05_12_simplification-wave-1.md` (local planning note, untracked); the
waves-2-3 remainder (open-tasks #1/#2/#3/#5) now lives as backlog cards on the **Notion
Parley board** (local extract deleted 2026-07-24; #4 docs-promotion shipped as `01f9bed`).
