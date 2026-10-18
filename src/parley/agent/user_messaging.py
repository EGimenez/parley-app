"""UserMessaging — the bidirectional channel between the human and their own
agent (2026-08-31 refactor). Everything that used to be smuggled through the
account's own mailbox (email-to-self instructions, `[PARLEY-ASK]` notifications,
self-answer detection by scanning your own inbox for `ref:`) is retired — the
tray talks to this directly over HTTP. One consequence: an inbound account
message never needs special-casing for "is this actually to/from myself?" —
that whole class of guard is gone, not replaced.

Two kinds of traffic, kept apart:
  * `instruct(text)` — a new command from the human ("book a meeting with...").
    Not tied to any existing negotiation; it's a fresh event.
  * `ask(ref, question)` / `answer(ref, text)` — the human-gate: the agent needs
    a decision on a specific negotiation, the human replies (free text, not
    just accept/deny).

A question can also be CLOSED without an answer (`close`): the meeting it was
about ended, its date passed, the user answered it in their calendar, or they
dismissed it. That is bookkeeping, not a decision — nothing reaches the brain.

Restart-safe: persists to `path` (JSON), same pattern as `ShortTermMemory`.
One-shot tool-call grants (a human's affirmative answer to a guardrail ASK)
live here too — they're a side effect of answering, not negotiation state.
"""

import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone


def _now():
    return datetime.now(timezone.utc).isoformat()


#: The longest summary a question card shows on one line.
SUMMARY_MAX = 120


def one_line(text, limit=SUMMARY_MAX):
    """The first sentence or line of `text`, whitespace collapsed, cut to `limit`.
    What a card shows when nobody wrote a summary — or wrote one too long."""
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    text = lines[0] if lines else ""
    m = re.search(r"(?<=[.?!])\s", text)
    if m and m.start() >= 20:
        text = text[:m.start()]
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


@dataclass
class Instruction:
    text: str
    created_at: str = field(default_factory=_now)


@dataclass
class PendingQuestion:
    ref: str
    question: str               # the full text: what the brain asked, word for word
    created_at: str = field(default_factory=_now)
    resolved: bool = False
    answer: str = None
    # What the tray shows instead of the full text. All optional: a question saved
    # before these existed loads with them empty, and every reader copes.
    kind: str = None            # invite | email | stalled | ambiguous | permission | ...
    who: str = None             # the other side, for a human: a name, else an address
    title: str = None           # the meeting or mail subject
    start: str = None           # ISO; the meeting the question is about, when it has one
    end: str = None
    summary: str = None         # one line, <= SUMMARY_MAX
    neg_status: str = None      # the negotiation's status when the question was asked
    closed: str = None          # why it was closed without an answer (None = it wasn't)


class UserMessaging:
    def __init__(self, path=None):
        self.path = path
        self.inbox = []      # [Instruction], not yet drained
        self.pending = {}    # ref -> PendingQuestion
        self.grants = set()  # (account, verb, target) one-shot tool-call approvals
        self.load()

    # -- persistence ------------------------------------------------------

    def load(self):
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, json.JSONDecodeError):
            return
        self.inbox = [Instruction(**i) for i in raw.get("inbox", [])]
        known = set(PendingQuestion.__dataclass_fields__)
        self.pending = {ref: PendingQuestion(**{k: v for k, v in p.items() if k in known})
                        for ref, p in (raw.get("pending") or {}).items()}
        self.grants = {tuple(g) for g in raw.get("grants") or []}

    def save(self):
        if not self.path:
            return
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        data = {"inbox": [asdict(i) for i in self.inbox],
                "pending": {ref: asdict(p) for ref, p in self.pending.items()},
                "grants": sorted(list(g) for g in self.grants)}
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, self.path)

    # -- instructions -------------------------------------------------------

    def instruct(self, text):
        """The tray (or a future `Client`) hands over a fresh instruction."""
        self.inbox.append(Instruction(text=text))
        self.save()

    # -- ask / answer ---------------------------------------------------------

    def ask(self, ref, question, **meta):
        """Escalate — idempotent per `ref` (one ask per thing): a repeat ask for
        an already-pending ref reuses it, no duplicate notification. Returns the
        PendingQuestion (existing or new) and whether it was reused. `meta` fills
        the display fields (kind, who, title, start, end, summary, neg_status)."""
        existing = self.pending.get(ref)
        if existing is not None and not existing.resolved:
            return existing, True
        known = set(PendingQuestion.__dataclass_fields__)
        pq = PendingQuestion(ref=ref, question=question,
                             **{k: v for k, v in meta.items() if k in known and v})
        self.pending[ref] = pq
        self.save()
        return pq, False

    def answer(self, ref, text):
        """Resolve a pending question — atomic, first answer wins: a second
        attempt for an already-resolved (or unknown) ref returns False and
        changes nothing. Returns True on success."""
        pq = self.pending.get(ref)
        if pq is None or pq.resolved:
            return False
        pq.resolved = True
        pq.answer = text
        self.save()
        return True

    def close(self, ref, reason):
        """Close a question WITHOUT answering it — same first-wins rule as `answer`.
        Returns True if it was open."""
        pq = self.pending.get(ref)
        if pq is None or pq.resolved:
            return False
        pq.resolved = True
        pq.closed = reason or "closed"
        self.save()
        return True

    def list_pending(self):
        """Open questions, for the tray's `/pending` poll."""
        return [p for p in self.pending.values() if not p.resolved]

    def drain(self):
        """Once per wake cycle: everything new since the last drain — fresh
        instructions and newly-answered questions — for the Loop's Decide phase.
        Answered questions are consumed (removed) here; unanswered ones stay in
        `pending` for the next `/pending` poll."""
        instructions, self.inbox = self.inbox, []
        answered = [p for p in self.pending.values() if p.resolved]
        for p in answered:
            del self.pending[p.ref]
        if instructions or answered:
            self.save()
        return instructions, answered

    # -- one-shot tool-call grants --------------------------------------------

    def grant(self, account, verb, target):
        """A human's affirmative answer to a guardrail ASK: the brain's retry
        passes exactly once."""
        self.grants.add((account.lower(), verb, (target or "").lower()))
        self.save()

    def consume_grant(self, account, verb, target):
        """True + consumes the grant if one is pending for this exact
        (account, verb, target); False otherwise (nothing changes)."""
        key = (account.lower(), verb, (target or "").lower())
        if key in self.grants:
            self.grants.discard(key)
            self.save()
            return True
        return False
