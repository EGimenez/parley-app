# Parley — Design Intuition

> **What this is.** The *reasoning* behind how a Parley agent should behave — written to build
> intuition (for a human or an assistant) **before** touching code. An exercise, grown one
> worked example at a time.
>
> **Note.** Deliberately written in plain words: the goal is to convey the *intuition*
> behind the project, not to be precise about the implementation.
>
> **What this is not.** Not a spec of the shipped code. Much of it is **future development**.
> Where an intuition and today's code disagree, the code isn't wrong — the intuition is
> simply ahead of it. What actually ships is
> [`supported-use-cases.md`](../design/use-cases/supported-use-cases.md).
>
> **This doc is the entry point to the design set.** It records how an agent should
> *think*; the docs under [`design/`](../design/README.md) record what has been built to
> make it think that way.

---

## 1. Why Parley exists — and how we describe a request

**Creating a meeting with a known time, duration, topic and attendees is trivial** — a human
just clicks. Parley earns its existence only where coordination gets *hard*:

- **A — No slot is free.** Someone has to give. A negotiation among users has to happen; an
  agent runs it, and can reason over the user's preferences (from policy) to settle faster.
- **B — No visibility.** You can't see the other side's calendar, so someone has to *ask* for
  availability and match. An agent runs that exchange **without either side exposing a full
  calendar**.

In both, the agent's edge is reasoning over preferences held in **policy**.

**Note** Commond guidelines ship within all parley agents and some specifics descrived in the policy.

### A request is a point in a space of dimensions

The *same* sentence — "meet John by Friday" — demands different behaviour depending on where it
lands: is John resolvable? is a slot free? can I see his calendar? is the duration known? So a
request isn't a fixed procedure to run; it's a **coordinate**. Read the coordinate and the
behaviour follows.

> The **easy corner** (fixed time · known attendee · free slot · full access) needs no agent.
> Parley's whole value lives in the **hard regions A and B**. Dimensions tell you *which region
> you're in*; A and B are *why the agent exists at all*.

### Agent vs Policy (who owns what)

| | Owns | Note |
|---|---|---|
| **Policy** | the *content* of a decision — per axis: proceed / use a default / ask | prose guidance + a per-person trust table |
| **Agent** | *interpreting* that policy and *executing* it — read calendars, talk to the peer, move events, write email | the agent **reasons**; it is **not** judgment-free |

### Two levels of policy

- **Implicit policy** — the built-in baseline every Parley agent runs, no matter what.
- **Explicit policy** — shipped generic, but the **user can rewrite it**. When we say "the
  policy," we mean this one. **Note** The policy sit within the cfg file.
- **Resolution order:** explicit policy says → else implicit policy says → else **ask the
  user.** ("Silence → ask" was too blunt; the implicit layer often has a say first.)
**Note** Wait. The agent has to read the policy firts in case the explicit-policy contradict the implicit policy. So Resolutions order is: Go with What ever the explicit policy says, otherwise what ever the implicit policy says, otherwise ask the user.
---

## 2. The shape of the work (not a straight line)

A request is either **create** a new meeting, or **manage** an existing one (reschedule,
cancel, or respond to someone else's change). To do either, the agent reasons about two kinds
of thing:

- **Meetings** — identified by *topic · time · participants* ("the next meeting with John,"
  "the meeting about T").
- **Meeting variables** — *When · Where · About · How* (plus *who* and *how long*).
  So far we model **When** deeply; Where/About/How are named here but still future.

> **Everything is multiparty.** A meeting's participants are a *set*, not one peer. The
> two-party case (me ⇄ John) is just the smallest one; **all the reasoning below must generalize
> to N.** Inside the set, some attendees are **required** and some **optional** — a meeting
> settles when the **required** set (a *quorum*) agrees; optional attendees are invited
> best-effort and never block.

The naive spine is `Understand → Resolve WHO → Locate WHAT → Find a SLOT → Commit` — but it is
**not linear.** Finding a slot is a *loop* with the other side; any step can bounce back to the
user for a missing variable or an ambiguity; and an **inbound message can arrive at any time**
and restart the reasoning.

```
        ┌──────────── an inbound message can arrive at any time ─────────────┐
        ▼                                                                    │
 Understand ─► Resolve WHO ─► Locate WHAT ─► Find a SLOT  ⇄  peer ─► Commit (gated)
     │  ▲         │  ▲        (existing?)       │  ▲                    │
     └──┴─────────┴──┴──── ask the user on any missing var / ambiguity ─┘
```

Treat the spine as the **skeleton** and the negotiation loop (§6) as the **muscle**. Keep the
user informed as you go ("reaching out to John for a time before Friday…").

---

## 3. The dimensions of a request

Every request is a combination of these. A scenario *name* (§8) lists them.

| Dimension | Settings |
|---|---|
| **Direction** | `outbound` (user → Parley → peer) · `inbound` (something arrives — §6) |
| **Action / message** | `create` · `reschedule` · `cancel` · `request` (ask for slots) · `accept` · `decline` · `counter-propose` |
| **Attendee identity** | `by-name` (needs resolving — §4) · `by-address` (already resolved) |
| **Parties** | `one peer` · `multiparty (N)` — pairwise is just the 2-party case; a slot must fit the whole **required** set |
| **Attendee necessity** *(per person)* | `required` (counts toward the quorum) · `optional` (best-effort, never blocks) |
| **Meeting identity** *(manage only)* | `by-topic` · `by-attendee` · `by-time` → must match an **existing** event (may be ambiguous / recurring) |
| **Role** *(existing meeting)* | `organizer` (can move it) · `attendee` (can only *propose* a move) |
| **Counterpart standing** | `teammate` · `boss/senior` · `report` · `external` — modulates **who is expected to yield** (§6) |
| **When — shape** | `fixed time` · `window` ("by end of week") · `relative window` ("+~2 days" off the current slot) |
| **When — firmness** | `hard` · `soft` ("try to") |
| **Duration** | `defined` · `undefined` (→ ask) · `inherited` (from an existing event) |
| **Schedule access** | `yes` (you can see their free/busy) · `no` (external — you can't) |
| **Topic** | `known` · `unknown` |
| **Slot availability** | `free slot exists` · **`no free slot`** (the interesting case — §5) |
| **My meetings' movability** | `pinned` … `fluid` — the importance order that make-room reasons over (§6) |

**Cross-cutting, always policy-driven — and things the agent should actively *learn*, not wait
to be told:**
- **Working hours** — honoured inside any window.
- **Priority model** — the rough ranking over *people* (standing) and *meetings* (movability)
  that powers make-room.
- **Commitment** — negotiation is autonomous; *booking vs. presenting ranked choices* is a
  policy call (the graduated-autonomy ladder).

---

## 4. Play: resolve the attendee(s) ("John" → a real person)

> Hey Parley set a meeting with John

**WHO can be a set.** Resolve each attendee independently; a meeting may mix already-resolved
addresses with names that still need work, and required with optional people. Try in order:

1. **Look in the account's contacts.**
   - One clean match (or a small set) → **confirm with the user** —
   - …**unless** policy grants *"if there's a unique match by name (or name + surname), just go
     with it."* That grant can be **global or scoped to some accounts** (auto-resolve on work,
     always-ask on personal).
2. **Ask the user** — and **store the answer** so it's never asked twice.
3. **Remember** — a resolved person lives in durable memory; next time "meeting with John" is
   free.

---

## 5. Play: find a slot

> Hey Parley set a meeting with x@y.com before Friday

Decided by two axes — *can I see their calendar?* × *is anything free?*

| | **A free slot exists** | **No free slot** |
|---|---|---|
| **Access = yes** | See the shared free/busy → pick a slot → create + send the invite. | **★ The reason Parley exists — §5.1 (make room)** |
| **Access = no** | Can't see their calendar → **§5.2** (ask, or propose) | (same as §5.2 — you're negotiating blind anyway) |

**With N parties**, "a free slot" means free for the whole **required** set — the intersection
only *shrinks* as people are added, so `no free slot` gets more common and **make-room** more
likely. Access can be **mixed** in one meeting: you may see some parties' calendars (`yes`) and
have to negotiate the rest blind (`no`) — run §5.1 for the visible side, §5.2 for the rest, and
combine.

### 5.1 — No free slot, but you *can* see it: **make room**

Nobody is free → *reason a slot into existence.* Anyone can drop a meeting into an empty slot;
only Parley negotiates one open. **But make-room is not "can I move something" — it's a
negotiation over *whose* time yields**, priced by two policy inputs (§6 spells this out):

| Counterpart standing → | My blocking meeting is… | Who yields |
|---|---|---|
| **teammate / peer** | important | **expect John to make room** — ask his side, don't move mine |
| **teammate / peer** | trivial / movable | I move mine — a cheap concession |
| **boss / senior** | anything movable | **I move mine** — I absorb the cost |
| **policy silent** | — | **ask the user, with concrete options** |

Then: contact John, **propose the slot(s) you could free**, and **reschedule** whatever was
sitting in the slot John picks.

### 5.2 — Access = no: negotiate blind (information-minimized)

You can't see John's calendar, so pick one honest path:
- **(a)** propose times from the user's own free window and let John's side choose, or
- **(b)** ask John's side for times first, then match against the user's free time.

**Information-minimized** = the two sides agree on a slot *without either revealing its owner's
full calendar.* Instead of "here's my whole calendar, diff it," they trade the minimum: I
propose 2–3 times; John's side replies yes/no or counters — John never learns what *else* is on
my calendar, only the status of the times I asked about. (Exactly polite email scheduling: "Tue
2pm or Wed 10am?" → "Wed works.") *Reference:* the W3C AI Agent Protocol
scheduling use case, and A2A — https://a2a-protocol.org.

> **Recursion:** moving a meeting (§5.1, or a reschedule) can itself land on a busy slot — which
> re-triggers §5. Making room is not a one-shot.

---

## 6. Play: the negotiation loop & inbound messages

Finding a slot is rarely one step — it's a **dialogue** (often several, one per party), and the
other side is **often a human**, not an agent.

### The counterpart may always be a human — design for it

**Dual-audience rule.** Every message a Parley agent sends is an **email a human can read and
act on.** You cannot assume an agent is on the other end — *especially early*. Any
machine-readable structure (for a peer agent) rides *inside* a human-readable email; it never
replaces it. The loop must tolerate whatever comes back: a structured agent reply, a prose
sentence ("Tuesday works"), a **calendar invite (.ics)**, a partial answer, or **silence**.

**Ownership is discovered, not assigned.** "The asker owns the event" is only a *convention
between two agents* — reality overrides it:

| Situation | Who owns | My agent's move |
|---|---|---|
| Both agents, following convention | asker creates, peer RSVPs | create + send, or RSVP |
| I emailed "send me slots" → **a human replies with a calendar invite** | the **human** | **switch from create-mode to respond-mode** → RSVP (accept / decline / propose-new). **Do not also create** — that double-books. |
| A human just books over me mid-negotiation | the human | detect the event exists → abandon my create, reconcile to theirs |

The rule is: **whoever writes the event first owns it** (this is exactly the first-write-wins
the infra already enforces), and my agent must *detect what happened* and adapt. **Handling a
meeting request that arrives as the answer to my own email is a first-class inbound case, not a
corner.**

**Infra enforces the convention without breaking the override (2026-07-21).** A responder's
brain used to be able to *steal* ownership: in an availability negotiation both sides send
OFFERs, and that symmetry fooled the responder into `apply(create)` — it self-organized the
event and the **asker** was left merely RSVP'ing (this is the live Scenario B failure). The
ToolBelt now **refuses a fresh `create` on an INCOMING _wire_ negotiation**, pointing the brain
back at `send(ACCEPT)` so the asker books. But the refusal is deliberately narrow, so every row
of the table above still holds: a **real invite** (it carries an `inv-`/`mtg-i` ref) and an
explicit `apply(rsvp)` are **exempt** — the instant a human on the other side books directly, my
agent switches to respond-mode and RSVPs *their* event, never refusing it. Convention enforced
between agents; reality (a human doing whatever they like) always wins. The exact mechanism of
the bug was an over-broad "is this invite-originated?" check that treated *every* incoming
negotiation as invite-like; the guard keys instead on the real-invite ref + the explicit rsvp
signal, which is precisely the seam between "agent convention" and "human override".

### Inbound message → responder's move

Access to my *own* calendar is always `yes`, so I always know if a proposed time is free.

| Inbound | Responder's candidate actions |
|---|---|
| **Proposal** ("here are 2–3 times") | accept · counter · make-room · decline · surface-to-user |
| **Counter** ("none of yours; try these") | accept · counter again · decline |
| **Accept** ("yes, that one") | confirm / RSVP |
| **Decline** | re-propose · widen window · give up + report |
| **Cancel** | drop the hold, free the slot, inform the user |
| **Reschedule request** | run §5 on the existing event |
| **Nudge** ("still waiting") | answer, or report the stall to the user |

### The autonomy line: policy bounds the conversation *and* gates the commit

Policy touches the loop in **two different places** — don't collapse them into one:

> - **Bounds (all through the negotiation):** policy shapes *what I may offer, and to whom.*
>   Some hours may never be offered to counterpart A yet be fine for B; some windows are
>   off-limits regardless of who's asking. So **"autonomous" means no per-message human approval
>   — not unbounded.** The negotiation always runs on **policy rails** (and still discloses only
>   *my* user's free times, never a peer's, never my whole calendar).
> - **Gate (at the commit):** writing the event or RSVP'ing is the *one* place the
>   silence-default bites.

Negotiating touches no calendar, so it stays autonomous **within the bounds**; only the commit
waits on the human. The loop never stalls on the *dialogue* — it only ever waits at the
*booking line.*

### Two parties or twenty — the loop is a star

Multiparty isn't a special mode; it's the general case. The owner's agent runs **one
sub-negotiation per party** (each with its **own stall timer**), hunting a slot in the
**intersection of the required set**:

- **Settle** when the **required** parties agree (a *quorum*) — optional attendees are invited
  best-effort and never hold up the booking.
- **Deadlock** = the required intersection is empty → make-room (§5.1) against whichever party
  blocks, or hand back to the humans.
- A late change from **one** party (counter, cancel) re-opens only *its* thread — the others'
  agreements stand unless the slot itself moves.
- **Ownership stays first-write:** one owner writes one event and everyone else RSVPs — it never
  fans out into N rival events.

### The loop and its terminal states (per party)

```
propose ⇄ counter ⇄ counter …  ─► required set agrees (quorum) ─► COMMIT gate ─► booked
   │           │
   │           └─ each round narrows the live-candidate set (must shrink → terminates)
   ├─ required intersection empty after windows / max rounds ─► DEADLOCK ─► make-room (§5.1) or hand to humans
   ├─ a party stays silent: nudge, then escalate ────────────► STALL
   └─ user changes mind / cancels ───────────────────────────► SUPERSEDED
```

- **Terminal states:** `settled · declined · deadlocked · stalled/expired · superseded`.
- **Message-level roles alternate** (a responder who counters becomes a proposer next round),
  but **event ownership is whatever got written first** — it doesn't drift mid-loop.
- **Stall timer:** *each agent runs its own*, configurable per policy — no shared handshake, so
  it's robust to a human (or a timer-less peer) on the other side.
- **Deadlock:** when there's truly no overlap, whether to *first attempt make-room on my own
  side* or *report near-misses to the user* is **not a fixed default** — it's the same
  standing-×-movability policy call as §5.1; **policy silent → ask the user with options.**

---

## 7. Play: commit (the design line)

**Negotiate autonomously (within policy bounds). Gate the *commit* on policy.**

- **Policy shapes *what gets offered*, not just whether to book.** The commit gate is policy's
  *last* touch on the request, not its only one — see the two knobs in §6.
- Once the agent holds a viable slot, policy decides: **book it** vs **present ranked slots and
  let the user pick**.
- Explicit policy silent → implicit policy → **surface ranked slots, user chooses** (rung 1 of
  the autonomy ladder).
- **Multiparty commit:** book once the **required** quorum agrees; keep chasing optional
  attendees afterward without blocking the booking.
- **The user's override is training data** — when they pick something other than the top slot,
  capture the signal.

**Inbound has the *opposite* silent-default from outbound.** For a request that arrives (e.g. a
plain human invite), policy silent → **do nothing; let the human click.** Don't auto-commit the
user, and don't nag. But Parley can **observe** the human's choice and, later (future UX, TBD),
propose a policy improvement from it. The exception is a **live agent loop**: never go silent on
the *dialogue* (that stalls a machine) — keep the handshake alive, but still hold at the commit
gate.

---

## 8. How to read / write a scenario name

A name is the dimensions from **§3**, pinned:

```
<Direction> · <Action> · Attendee <how> · Parties <one|N, required/optional> ·
[Meeting <how> · Role <?>] · Counterpart <standing> · When <shape+firmness> ·
Duration <state> · Access <yes|no|mixed> · Topic <known|unknown> · Slot <free|make-room>
```

Fill every clause and the expected behaviour is (mostly) determined — the rest is policy.

---

## 9. Worked scenarios

### Scenario 1 — Outbound · Create · Attendee by-Name · When: Window · Duration: Undefined · Access [yes|no] · Topic: Known · Slot: maybe make-room

> *"Parley, please try to set a meeting with John by the end of the week — I need to comment on
> the Q3 financials."*

| Dimension | Setting → what the agent does |
|---|---|
| Attendee | by-name → **§4** (contacts → confirm/grant → ask → remember) |
| When | "by end of week" = a **window to search**, not a time to book |
| Duration | undefined → **ask the user** (policy may set a default) |
| Access | yes → see intersection · no → **§5.2**; if yes-but-nothing-free → **§5.1** |
| Topic | "Q3 financials" → into the invite; may hint at who else / prep |
| Slot | be ready to **make room**, not just find it |

### Scenario 2 — Outbound · Reschedule · Meeting by-Topic · Role [organizer|attendee] · When: Relative Window (soft) · Duration: Inherited · Access [yes|no] · Slot: maybe make-room

> *"Parley, please help me reschedule the meeting about Topic T. Try to move it a couple of days
> forward."*

Acts on an **existing commitment that already binds other people.**

1. **Find the meeting.** Match "about T" against the **calendar** (closed negotiations are
   forgotten; the calendar isn't). Ambiguity is the danger — several meetings mention T, or T
   recurs → disambiguate or ask. **Never move the wrong one.**
2. **Establish role — this forks everything.**
   - **Organizer** → can move it, but must re-coordinate: attendees still free at the new time
     stay accepted; only re-ask on a conflict.
   - **Attendee** → *cannot* move it. Degrades to **propose** a new time to the organizer, whose
     agent then runs its own reschedule.
3. **Read "a couple of days forward" as a soft, relative window** — anchored ~+2 days off the
   current slot, same time-of-day, inside working hours. **Soft** = best effort; nothing good
   there → **report back**, don't force a bad slot.
4. **Find a new slot** — normal §5, including make-room. *Recursion:* the move may itself hit a
   busy slot.
5. **Coordinate** per role; **commit** per §7.

### Scenario 3 — Inbound · a request arrives (ownership discovered)

> My agent earlier emailed *"could you send some slots for tomorrow?"* — and the reply is a
> **calendar invite for 3pm** from a human.

1. **Recognize the direction flipped.** I asked for availability; instead an **event was
   created** by the other side. Ownership is now *theirs* (§6).
2. **Switch to respond-mode.** Check my own calendar at 3pm. Free & acceptable per policy →
   **RSVP accept** (or surface to the user if commitment policy says so). Busy → **§5** on my
   side (make-room or propose-new via a decline-with-counter).
3. **Never create a parallel event** — that double-books. Reconcile to the one that exists.
4. Policy silent on auto-accepting an inbound invite → **do nothing, surface to the user** (§7).

**Open decisions still live:** ambiguous T (ask vs best-guess-confirm) · attendee-role
reschedule (propose vs refuse) · "try to" failure (how hard to search) · auto-accept inbound vs
always-ask.

---

## 10. Glossary

| Term | Meaning |
|---|---|
| **Agent** | The reasoner+executor — *interprets* policy, then reads calendars, talks to peers, writes events. Judgment lives here; its *content* comes from policy. |
| **Policy** | The decider's *content* — per axis: proceed / default / ask. **Implicit** (built-in baseline) + **explicit** (user-editable). Prose + a per-person trust table. |
| **Window** | A *range* of time to search, not a time to book at. |
| **Intersection** | Times free for **all required** parties (two people is just the smallest case). |
| **Multiparty / quorum** | A meeting is N parties; it **settles when the required set agrees** (quorum). Optional attendees are best-effort. The loop is a **star** — one sub-negotiation per party — and one owner writes one event; the rest RSVP. |
| **Required vs optional** | Required attendees count toward the quorum and can block/deadlock; optional ones are invited best-effort and never hold up the booking. |
| **Make room / displacement** | Freeing a busy slot by moving an existing meeting — Parley's signature move. It's a negotiation over **whose time yields**, priced by standing × movability (§5.1, §6). |
| **Counterpart standing** | The peer's priority relative to the user (teammate / boss / report / external) — decides who is expected to yield. |
| **Organizer vs attendee** | Who owns a meeting: organizer can move it; attendee can only *propose* a move. |
| **Ownership is discovered** | Who owns an event is whoever *wrote it first*, not who asked — the agent detects this and adapts (§6). |
| **Dual-audience** | Every message is a human-readable email; agent structure (if any) rides inside it — the counterpart may be a person. |
| **Information-minimized** | Reaching a slot without either side exposing its full calendar (§5.2). |
| **Negotiate freely, gate the commit** | Proposing/countering commits nothing and needs no per-message approval — but it runs on **policy rails**: policy **bounds** *what* is offered and *to whom*, and separately **gates** the commit (booking/RSVP). "Autonomous" ≠ unbounded. |
| **Human-gated commitment** | The *commit* needs a human (per policy); the *negotiation* does not. |
