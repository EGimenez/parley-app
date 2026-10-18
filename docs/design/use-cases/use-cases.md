# Parley Use Cases

> Companion to [intuition.md](../../vision/intuition.md) (the reasoning — dimensions,
> policy, the negotiation loop). This doc is the compact index of *which request shapes* matter, in order
> of novel value. It does not re-explain *how* the agent reasons about them — that's
> intuition.md's job; link, don't repeat. Shipped/deferred status lives in
> [supported-use-cases.md](supported-use-cases.md), not here.

## The distinction that matters

Free/busy is a solved problem — Outlook, Google, Clockwise already pack meetings into existing
gaps. **If a mutually open slot exists, finding it is table stakes.** Parley's reason to exist
is the case where **no open slot exists**, and one has to be *created* by negotiating a
displacement. Everything below builds toward UC-3, the wedge.

## UC-0 — Delegated booking, open slot exists

> "Book me a 30-min meeting with B before Thursday, topic T."

The agent does the work — resolves B, finds a slot, books it. Whether it books directly or
presents 2–3 ranked choices first is a **policy** call (intuition §7), not two use cases.
B running Parley or not doesn't change A's experience: "here's a time" (booked, or to pick).

**Exercises:** delegation → own agent; NEGOTIATE → CONFIRM; graceful degradation against an
agentless counterpart.

## UC-1 — Reschedule an existing meeting

Meeting M already exists. The **organizer** can move it directly; an **attendee** can only
*propose* a move to the organizer (intuition §6, the ownership table). If M is one instance of
a recurring series, the move needs a **scope** — this instance, or this-and-following — since
"move M" is ambiguous on a series without one.

**Exercises:** object continuity; the organizer/attendee fork; recurrence scope as part of
identity.

## UC-2 — Multi-party convergence

A wants B **and** C; one slot has to work for the whole required set (intuition §3, quorum).
Still open-slot territory — when the intersection is empty, this becomes UC-3.

**Exercises:** N-party intersection; one coordinator fanning out and reconciling replies.

## UC-3 — No open slot exists → make room ⭐ the wedge

Nobody's free. Instead of giving up, the agent asks for slots that are occupied but *could* be
freed — a negotiation over **whose time yields**, priced by standing × movability (intuition
§5.1/§6). The single-level case (move one meeting to fit another) is today's shipped ambition;
the recursive case is not:

**The cascade (design, not built).** Freeing B's slot may require B to displace *their* meeting
with D — a sub-negotiation that can itself cascade, all racing the same deadline. That forces:
conditional/tentative candidates ("Thursday works, if D agrees"), parent→child negotiation
links, a displacement-cost policy call, deadline propagation down the chain, and privacy that
survives recursion (A never learns *why* B could free Thursday).

**Exercises:** essentially everything the protocol doesn't yet model — conditional candidates,
child negotiations, cost-weighted displacement, deadline propagation.

## UC-4 — No solution exists

No amount of shuffling produces a slot before the deadline. Fail usefully: report the closest
achievable trade-off ("no slot before Friday without moving a do-not-move meeting; earliest
otherwise is Monday 10:00") and hand the decision back — the human's relaxation is, again,
training signal (intuition §7).

**Exercises:** graceful failure; trade-offs over a flat "no"; the human as the final escape
valve.

## Cross-cutting (true of every UC above)

- **The human can step in anytime.** A direct human action — accepts, moves, cancels, replies
  in prose — always outranks the agent's plan (intuition §6, "ownership is discovered"). The
  agent must detect it (object identity + timestamp) and reconcile: abort, restart, or accept
  the negotiation as already satisfied. Never double-commit.
- **Multi-calendar is real.** A Parley agent spans several connected calendars; free/busy is
  their union, writes target one primary, and a personal/constraint-only calendar is read for
  busy/free but never written to, displaced, or revealed to a counterpart (intuition's
  inward-full / outward-minimized rule, extended between the user's *own* calendars).
- Human-gated commitment, information minimization, override-as-training-data — these are
  intuition.md §6–7's job; not restated here.

## Open questions

- [ ] Conditional candidates on the wire — representation, firming-up, collapse.
- [ ] Cascade depth / termination — what stops a displacement loop?
- [ ] Displacement-cost policy — what makes a meeting "movable," concretely, beyond prose?
- [ ] Calendar roles — how does a user declare writable-primary vs constraint-only, and can it
  be per-context?
- [ ] Working-hours / preferred-schedule policy — shares the policy language with
  standing/movability rules, or its own thing?
