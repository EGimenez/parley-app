"""ShortTermMemory (renamed from NegotiationMemory, 2026-08-31 — collided with
`TestAccount`'s old name `MemoryAccount`, an unrelated thing that happened to
share the word "Memory") — the agent's working state across concurrent
negotiations.

The brain reasons over **all open negotiations at once**, so the agent must
remember them between events (and between server restarts). This is *domain*
state — "which meetings am I mid-negotiating, with whom, at what stage" — not
generic chat history. It is the one piece no LLM framework can manage for us,
so it lives here, ours. Pending human questions live in `UserMessaging`, not
here — a question is channel state, not negotiation state (2026-08-31: they
used to be a bolted-on `_asks` dict in this file).

  * Each turn the brain gets a compact `summary()` of every open negotiation, plus a
    `get_negotiation(id)` tool that pulls one negotiation's full `detail()` on demand
    (keeps the prompt bounded as concurrency grows).
  * State persists to JSON so non-closed negotiations survive a restart; a
    negotiation that reaches a terminal state is `close()`d — dropped from the
    working set but retained for recognition.

Dependency-light (only `parley.core` for slot formatting): takes a file path, so
wiring the concrete `~/.parley/state/<account>/` location stays in the
composition layer.
"""

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from parley.core import Slot, from_iso

# Stages a negotiation moves through. The first are "open" (kept, shown to the brain);
# the terminal ones drop out of the working set but are retained for recognition.
# `deadlocked`: the stall path exhausted — nudged, escalated, still nothing; a user
# answer reopens it (as a new epoch). `superseded`: an old epoch replaced by a reopen —
# the new epoch is the live object.
OPEN_STAGES = ("open", "proposing", "awaiting_peer", "awaiting_human")
TERMINAL_STAGES = ("booked", "declined", "cancelled", "deadlocked", "superseded")


def _now():
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Negotiation:
    """One meeting negotiation in flight. `slot` is `{"start": iso, "end": iso}` or
    None; `transcript` is a running list of short human-readable event lines."""
    id: str
    topic: str = ""
    direction: str = "outgoing"      # outgoing (we asked) | incoming (they asked)
    status: str = "open"
    slot: dict = None
    account: str = ""
    # Every party to this meeting — merges what used to be three separate fields
    # (`peer`, `aliases`, `parties`) into one (2026-08-31): [{addr, role, status}].
    # A 2-party negotiation is just one element; a solo `peer` shim no longer exists.
    parties: list = field(default_factory=list)
    # The booked event's own iCalUID, set the moment a real booking happens
    # (2026-08-31, walking inventory item #11): `find_overlap` alone can't
    # recognize a genuine reschedule — the slot changed by definition — but a
    # stable object identity can. Kept as its own field, not folded into a
    # generic alias list.
    calendar_uid: str = None
    # A deferred RSVP awaiting infra retry: the invite's event hadn't landed when we
    # tried to accept, so infra retries per wake cycle instead of the model burning
    # its step budget. {attendee, start, end, title, attempts} or None.
    pending_rsvp: dict = None
    # A conditional next step to carry out later, when a peer response or event makes it
    # possible — e.g. "once outlook agrees to move ZZ-Pepito, accept ZZ-Juanito"
    # (resumable negotiations). Free text, shown every turn.
    follow_up: str = None
    # How many times a stalled (silent-peer) negotiation has been woken by the timer:
    # 0 → not yet, 1 → nudged, 2 → escalated. Drives nudge-once-then-escalate without
    # re-firing every wake cycle.
    stalls: int = 0
    # Email Message-IDs seen in this negotiation's mail thread: a later reply that
    # References any of them correlates here even when it carries no visible ref (a
    # human replying in plain prose).
    thread: list = field(default_factory=list)
    transcript: list = field(default_factory=list)
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    def _slot_human(self):
        if not self.slot:
            return ""
        try:
            return " @ " + Slot(from_iso(self.slot["start"]), from_iso(self.slot["end"])).human()
        except Exception:  # noqa: BLE001 — never let a bad slot break the summary
            return ""

    def summary_line(self):
        who = " + ".join(f"{p.get('addr')}"
                         + ("" if p.get("role", "required") == "required" else " (optional)")
                         for p in self.parties) or "?"
        return (f"[{self.id}] {self.direction} with {who} — "
                f"'{self.topic or 'Meeting'}' — {self.status}{self._slot_human()}")

    def detail(self):
        lines = [self.summary_line(), f"  created {self.created_at}, updated {self.updated_at}"]
        if self.transcript:
            lines.append("  history:")
            lines.extend(f"    - {e}" for e in self.transcript)
        return "\n".join(lines)


class ShortTermMemory:
    """A per-agent store of open negotiations, persisted to `path` (JSON). Load on
    construct; `save()` after every mutation. Terminal negotiations are retained
    (never popped) — see `close()`."""

    def __init__(self, path=None):
        self.path = path
        self.items = {}    # id -> Negotiation
        self.load()

    # -- persistence ----------------------------------------------------------

    def load(self):
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, json.JSONDecodeError):
            return
        for obj in raw.get("negotiations", []):
            n = Negotiation(**{k: obj.get(k) for k in Negotiation.__dataclass_fields__ if k in obj})
            self.items[n.id] = n

    def save(self):
        if not self.path:
            return
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        data = {"negotiations": [asdict(n) for n in self.items.values()]}
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, self.path)

    # -- mutations ------------------------------------------------------------

    def upsert(self, id, **fields):
        """Create or update a negotiation; stamps `updated_at`; persists. Returns it."""
        n = self.items.get(id)
        if n is None:
            n = Negotiation(id=id)
            self.items[id] = n
        for k, v in fields.items():
            if k in Negotiation.__dataclass_fields__ and v is not None:
                setattr(n, k, v)
        n.updated_at = _now()
        self.save()
        return n

    def record(self, id, event):
        """Append a short event line to a negotiation's transcript (id resolves via
        epoch)."""
        n = self.get(id)
        if n is None:
            return None
        n.transcript.append(event)
        n.updated_at = _now()
        self.save()
        return n

    def close(self, id, status="booked"):
        """Mark a negotiation terminal (booked/declined/cancelled). It drops out of the
        brain's `open()`/`summary()` working set, but is **retained** (not popped) so a
        repeat wire message for it can be recognized and ignored.

        An id we've never seen still gets a **tombstone**: a settle must always arm
        `is_terminal`, or a first-contact confirmation (peer books, we had no prior
        record) leaves the door open for a duplicate commit. Returns the Negotiation."""
        n = self.items.get(id)
        if n is None:
            n = Negotiation(id=id)
            self.items[id] = n
        n.status = status if status in TERMINAL_STAGES else "booked"
        n.updated_at = _now()
        self.save()
        return n

    def add_party(self, id, addr, role="required"):
        """Register `addr` as a party of negotiation `id`: one ref, N tracks.
        Idempotent per address. Returns the Negotiation or None."""
        n = self.get(id)
        if n is None or not addr:
            return None
        if not any((p.get("addr") or "").lower() == addr.lower() for p in n.parties):
            n.parties.append({"addr": addr, "role": role, "status": "open"})
            n.updated_at = _now()
            self.save()
        return n

    def involves(self, n, addr):
        """True when `addr` is one of this negotiation's parties."""
        a = (addr or "").lower()
        return any((p.get("addr") or "").lower() == a for p in (n.parties or []))

    # -- how much we have said to each party, so nobody gets talked at forever ----
    #
    # The first real multi-party run put 10 of 14 outgoing emails in front of ONE
    # colleague (2026-09-24). Nothing was broken in the sense of raising an error:
    # each turn was individually reasonable, and there was simply no bound on how
    # many of them there could be. The count lives per PARTY, not per negotiation,
    # because a fan-out to five people legitimately sends five messages in one
    # round — it is writing to the same person over and over that is pathological.

    def note_sent(self, id, addr):
        """Record one message sent to `addr` on this negotiation; return the new count."""
        n = self.get(id)
        if n is None or not addr:
            return 0
        a = addr.lower()
        for p in n.parties:
            if (p.get("addr") or "").lower() == a:
                p["sent"] = int(p.get("sent") or 0) + 1
                n.updated_at = _now()
                self.save()
                return p["sent"]
        return 0

    def sent_to(self, n, addr):
        """How many messages this negotiation has already sent to `addr`."""
        a = (addr or "").lower()
        for p in (n.parties or []) if n else []:
            if (p.get("addr") or "").lower() == a:
                return int(p.get("sent") or 0)
        return 0

    def reset_sent(self, id):
        """Give every party a fresh budget — called when the HUMAN has answered.

        Without this the cap is a trap: the human says "yes, keep going", the agent
        tries to reply, trips the same cap and asks again. An answered escalation is
        exactly the informed go-ahead the cap exists to demand."""
        n = self.get(id)
        if n is None:
            return None
        for p in n.parties:
            p["sent"] = 0
        n.updated_at = _now()
        self.save()
        return n

    def set_calendar_uid(self, id, uid):
        """Record the booked event's own iCalUID (item #11): a later reschedule of
        the SAME real calendar object — its slot no longer matches, so peer+slot
        matching alone can't recognize it — still resolves via `find_by_uid`."""
        n = self.get(id)
        if n is None or not uid:
            return None
        n.calendar_uid = uid
        n.updated_at = _now()
        self.save()
        return n

    def reopen(self, id, reason=""):
        """Reopen a settled negotiation as a NEW epoch: `mtg-x` → `mtg-x#2`.
        The old object keeps its terminal status (relabeled `superseded`); the new
        epoch copies identity (parties/topic/slot/account/direction/calendar_uid)
        and opens fresh. Both transcripts record the lineage. Returns the new
        Negotiation (or a plain upsert if `id` was never known)."""
        old = self.get(id)
        if old is None:
            return self.upsert(id, status="open")
        base = old.id.split("#")[0]
        epoch = max([1] + [int(k.split("#")[1]) for k in self.items
                           if k.startswith(base + "#") and k.split("#")[1].isdigit()]) + 1
        new_id = f"{base}#{epoch}"
        n = self.upsert(new_id, topic=old.topic, direction=old.direction,
                        account=old.account, slot=dict(old.slot) if old.slot else None,
                        parties=[dict(p) for p in old.parties], calendar_uid=old.calendar_uid)
        n.status = "open"
        old.status = "superseded"
        old.transcript.append(f"superseded by {new_id}: {reason or 'reopened'}")
        old.updated_at = _now()
        self.record(new_id, f"reopened from {old.id}: {reason or '(no reason)'}")
        return n

    # -- reads ----------------------------------------------------------------

    def _newest_epoch(self, base):
        """The highest-numbered `base#N` negotiation, or None."""
        epochs = [k for k in self.items
                  if k.startswith(base + "#") and k.split("#")[-1].isdigit()]
        return self.items[max(epochs, key=lambda k: int(k.split("#")[-1]))] if epochs else None

    def get(self, id):
        """The negotiation `id` resolves to. A TERMINAL exact hit that was
        reopened resolves to its newest epoch (the live object for this
        meeting); else None. Epoch ids (`mtg-x#2`) always hit exact."""
        n = self.items.get(id)
        if n is None:
            return None
        if n.status in TERMINAL_STAGES:
            newest = self._newest_epoch(str(id).split("#")[0])
            if newest is not None and newest.id != n.id:
                return newest
        return n

    def is_terminal(self, id):
        """True if `id` is a known, already-settled negotiation (booked/declined/
        cancelled) — the signal to drop a repeat inbound for it. Resolves epochs:
        after a reopen the live epoch is open, so messages flow again."""
        n = self.get(id)
        return bool(n and n.status in TERMINAL_STAGES)

    def find_overlap(self, peer, start, end, status=None):
        """The negotiation matching (peer, exact slot instants) — the same physical
        meeting travels under several ids (the wire ref, the deterministic invite
        id), so id equality alone can't correlate them; peer + absolute instants
        can. `status=None` matches any status; pass `"booked"` to restrict to
        settled bookings only. Two guarantees ride this one query — same
        (peer, slot) is a dedupe/resend, a DIFFERENT peer at the same slot is a
        real conflict — callers distinguish those, this just finds what's there."""
        if not (peer and start and end):
            return None
        for n in self.items.values():
            if status is not None and n.status != status:
                continue
            if not n.slot or not self.involves(n, peer):
                continue
            try:
                if (from_iso(n.slot["start"]) == from_iso(start)
                        and from_iso(n.slot["end"]) == from_iso(end)):
                    return n
            except Exception:  # noqa: BLE001 — a malformed slot never breaks matching
                continue
        return None

    def find_by_uid(self, uid):
        """The negotiation whose booked event carries this iCalUID (item #11/#19)
        — recognizes a genuine reschedule of the SAME real calendar object even
        after its slot no longer matches what's on record. Or None."""
        if not uid:
            return None
        for n in self.items.values():
            if n.calendar_uid == uid:
                return n
        return None

    def set_pending_rsvp(self, id, attendee, start, end, title):
        """Mark a negotiation's RSVP as deferred (the invite hadn't landed) so the
        wake cycle retries it. Returns the Negotiation or None."""
        n = self.get(id)
        if n is None:
            return None
        n.pending_rsvp = {"attendee": attendee, "start": start, "end": end,
                          "title": title, "attempts": 0}
        n.updated_at = _now()
        self.save()
        return n

    def clear_pending_rsvp(self, id):
        n = self.get(id)
        if n and n.pending_rsvp is not None:
            n.pending_rsvp = None
            n.updated_at = _now()
            self.save()

    def pending_rsvps(self):
        """Negotiations whose RSVP is deferred, awaiting an infra retry."""
        return [n for n in self.items.values() if n.pending_rsvp]

    def set_follow_up(self, id, text):
        """Record a conditional next step to carry out later (resumable negotiation)."""
        n = self.get(id)
        if n is None:
            return None
        n.follow_up = (text or "").strip() or None
        n.updated_at = _now()
        self.save()
        return n

    def link_thread(self, id, *mids):
        """Record email Message-IDs belonging to this negotiation's mail thread, so
        a later reply that References any of them correlates here without a
        visible ref. Idempotent; ignores blanks."""
        n = self.get(id)
        if n is None:
            return None
        added = False
        for mid in mids:
            mid = (mid or "").strip()
            if mid and mid not in n.thread:
                n.thread.append(mid)
                added = True
        if added:
            n.updated_at = _now()
            self.save()
        return n

    def neg_for_thread(self, mids):
        """The negotiation whose mail thread contains any of `mids` (a reply's
        In-Reply-To + References chain), preferring a live epoch over a terminal
        one, or None. The precise, no-guess correlation: the reader kept the thread."""
        wanted = {(m or "").strip() for m in (mids or []) if (m or "").strip()}
        if not wanted:
            return None
        hits = [n for n in self.items.values() if wanted & set(n.thread or [])]
        if not hits:
            return None
        live = [n for n in hits if n.status not in TERMINAL_STAGES]
        return sorted(live or hits, key=lambda x: x.updated_at, reverse=True)[0]

    def open_by_peer_topic(self, peer, topic=None):
        """OPEN negotiations a prose reply from `peer` about `topic` could belong to
        (the fuzzy fallback, when the thread is broken). `topic` None → match on
        peer alone. Returns a LIST so the caller can detect ambiguity (→ ask the
        user) instead of guessing into the wrong meeting. Newest first."""
        tl = (topic or "").strip().lower()
        out = [n for n in self.items.values()
               if n.status not in TERMINAL_STAGES and self.involves(n, peer)
               and (not tl or (n.topic or "").strip().lower() == tl)]
        return sorted(out, key=lambda x: x.updated_at, reverse=True)

    def find_by_peer_topic(self, peer, topic, loose=False, reach=None):
        """Best-effort match by (peer, topic) — for a bare status notification
        ('Declined: <title>') that carries no times, so `find_overlap` can't
        correlate by slot. Skips already declined/cancelled negotiations; prefers
        the most recently updated. Returns the Negotiation or None.

        `loose=True` also accepts a topic that CONTAINS the recorded one (or is
        contained by it): the brain naturally writes "93-Pepito reschedule" for a
        follow-up about "93-Pepito", and an exact-only match let that open a second
        negotiation for the same meeting (2026-09-06). Only used where a false
        merge is cheaper than a false duplicate — never for status notifications,
        which stay exact.

        `reach` is how many people the message being matched is going to, and it
        bounds how far the loose rule may reach: a candidate involving MORE people
        than that needs an EXACT topic match. This is the fix for a real
        mis-correlation (user, 2026-09-24) — a group meeting with three colleagues
        and a separate one-to-one with one of them had near-identical topic strings,
        the loose rule merged them, and both went out carrying the same ref; the
        colleague answered the second and Parley read it as an answer to the first.
        A private message about a group meeting is a narrowing and stays allowed;
        absorbing a group meeting into a one-to-one is what is refused.
        """
        if not (peer and topic):
            return None
        tl = topic.strip().lower()

        def matches(n):
            r = (n.topic or "").strip().lower()
            if not r:
                return False
            if r == tl:
                return True
            if not loose or not (r in tl or tl in r):
                return False
            return reach is None or len(n.parties or []) <= reach

        cands = [n for n in self.items.values()
                 if self.involves(n, peer)
                 and matches(n)
                 and n.status not in ("declined", "cancelled")]
        return sorted(cands, key=lambda x: x.updated_at, reverse=True)[0] if cands else None

    def find_fanout(self, topic, account, within_seconds=600):
        """The multiparty fan-out this send belongs to, or None.

        One meeting with several people is ONE negotiation, but the brain issues the
        parallel availability sends in a single turn — so NEITHER call has a ref yet
        and each used to mint its own, leaving one meeting under two refs (2026-09-06
        scenario E). Matched on topic + account + still-open + outgoing, ignoring the
        peer (the peers are exactly what differs in a fan-out).

        `within_seconds` is the guard against a false merge: two genuinely different
        meetings that happen to share a topic string ("Sync" with Alice, "Sync" with
        Bob next week) are minutes or days apart, while a fan-out is seconds. Not
        airtight — but one meeting under two refs is a certain bug, and this is a
        rare, recent, same-topic coincidence.
        """
        if not (topic and account):
            return None
        tl = topic.strip().lower()
        now = datetime.now(timezone.utc)
        cands = []
        for n in self.items.values():
            if n.direction != "outgoing" or n.status in TERMINAL_STAGES:
                continue
            if (n.topic or "").strip().lower() != tl or n.account != account:
                continue
            try:
                age = (now - datetime.fromisoformat(n.created_at)).total_seconds()
            except (TypeError, ValueError):
                continue
            if 0 <= age <= within_seconds:
                cands.append((age, n))
        return min(cands, key=lambda p: p[0])[1] if cands else None

    def open(self):
        return [n for n in self.items.values() if n.status in OPEN_STAGES
                or n.status not in TERMINAL_STAGES]

    def recent_settled(self, n=5):
        """The last few terminal negotiations — kept visible to the brain so an
        artifact of an already-settled booking (a late reply, a trailing invite)
        is recognized instead of re-asked."""
        done = [x for x in self.items.values() if x.status in TERMINAL_STAGES]
        return sorted(done, key=lambda x: x.updated_at, reverse=True)[:n]

    def summary(self):
        """A compact, bounded view of every open negotiation for the brain's prompt —
        plus the recently settled ones (recognition, not action)."""
        items = self.open()
        lines = ([f"Open negotiations ({len(items)}):"] +
                 [" - " + n.summary_line()
                  + (f"\n   ⤷ PENDING FOLLOW-UP (do this as soon as it's possible): {n.follow_up}"
                     if n.follow_up else "")
                  for n in items]) if items \
            else ["Open negotiations: none."]
        settled = self.recent_settled()
        if settled:
            lines.append("Recently settled (already booked/closed — NEVER re-book or "
                         "re-ask; a matching arriving invite just needs an RSVP):")
            lines += [" - " + n.summary_line() for n in settled]
        if items:
            lines.append("(call get_negotiation(id) for one negotiation's full history.)")
        return "\n".join(lines)

    def detail(self, id):
        n = self.get(id)
        return n.detail() if n else f"(no negotiation {id})"
