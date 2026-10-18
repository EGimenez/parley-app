"""Brain — the single LLM that drives everything.

One brain, a singleton (2026-08-31 refactor — was one instance per account,
each holding that account's policy; now one instance, called as
`handle(event, conn)` with the account passed explicitly, same
connection-explicit convention `AccountsGate` already used). Every admitted
event — an inbound peer message, a client-server instruction, or the human's
answer to a pending question — comes here. The brain assembles context (the
account's effective policy prose + a summary of **all** open negotiations +
the new event), then runs a tool-use loop over `AccountsGate`, deciding via the
prompt rather than any deterministic engine.

**Per-event turns, not yet the batched wake-cycle design** (2026-08-31): the
refactor's own plan calls for one Brain call per wake cycle over the WHOLE
batch of new items, so a genuine "these two requests conflict" case can be
weighed before either commits. Implementing that alongside every other change
in this pass (new ShortTermMemory, UserMessaging, AccountsGate, wire.py,
dispatch order) would compound the risk of all of them landing at once with no
way to test them independently. This keeps the proven one-event-per-turn shape
— every guard in the behavior inventory maps directly onto it — and leaves
batching as a clearly separate, additive next step over the Loop, not lost.

Correlation of an inbound message to its negotiation is deterministic (a
visible `(ref: ...)` tag, or a real invite's derived id); the brain is told
which negotiation an event belongs to and can pull full history with
`get_negotiation`.

**Preparing a turn and running it are separate** (2026-09-19, phase 0 of
`docs/iterations/iteration_2026_09_19_claude-parley.md`). `turn_for_*()` builds a
`Turn`; `context()` gives the frozen system prompt + tool schemas; `dispatch()`
executes one tool call against the gated `AccountsGate`; `run()` is the in-process
driver that ties them together in a tool-use loop over an `LLMClient`. A background
Claude Code session replaces `run()` ALONE — it is handed the same Turn, asks for the
same context and calls back into the same dispatch, so the guardrail, the journal and
idempotent settlement stay on this side of the boundary rather than moving into a
subprocess. The four entry points (`handle`/`instruct`/`on_timer`/`user_answered`)
keep their signatures and are now two lines each.
"""

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone

from parley.agent.llm_port import Tool
from parley.core import html_to_text

MAX_STEPS = 8


@dataclass
class Turn:
    """One event, prepared for reasoning but not yet reasoned about.

    Phase 0 of the blocked-session design (`docs/iterations/
    iteration_2026_09_19_claude-parley.md`): *preparing* a turn and *running* it are
    two steps now, because a background Claude Code session has to be handed the
    prepared turn and drive the tool loop from its own side. Nothing here knows which
    side drives: `Brain.run()` is the in-process driver, and a session driver is the
    same three ingredients (`context()`, `dispatch()`, this object) over a different
    transport.

    `message` is the inbound mail, when there was one — the agent exposes it as
    `current_turn_message` for the duration of the turn. `kind` is for the journal.
    """
    conn: object
    event: str
    kind: str = "event"
    message: object = None
    negotiation_id: str = ""
    payload: dict = None      # the parsed wire payload, for infra that runs AFTER
    first_contact: bool = False  # brand-new negotiation: the no-silent-inaction
                                 # backstop applies when this turn finishes


_TIME = {"type": "string", "description":
         "Either local wall-clock 'YYYY-MM-DDTHH:MM' with NO offset (Parley interprets it in the "
         "calendar's timezone, or in `timezone` if you pass one), or an ISO-8601 date-time WITH "
         "offset copied verbatim from a message. Never convert between timezones yourself."}
_TZ = {"type": "string", "description":
       "Optional IANA timezone (e.g. Europe/Madrid) for local times — pass it ONLY when your user "
       "or the peer explicitly named a zone; otherwise omit it (the calendar's zone applies)."}

TOOLS = [
    Tool("send", "The ONE peer-messaging tool (gated). Write `body` yourself, in your own "
         "words — a clear, well-written email a human could read and act on. Always end it "
         "with the meeting's ref, e.g. '(ref: mtg-ab12cd34)' — infra appends one if you forget. "
         "On the very FIRST send about a brand-new meeting, omit `ref` and pass `topic` instead "
         "— a ref is minted and returned; thread it through every later call about the same "
         "meeting. MULTIPARTY: `to` takes several addresses (comma-separated) and that is ONE "
         "email to all of them, which is how a person convenes a meeting — write to everyone at "
         "once, never one message each. Reply the way you were spoken to: answer a reply-to-all "
         "to everyone, and a message sent to you ALONE to that person alone. When you "
         "state a time to a peer, always write it as an unambiguous 'YYYY-MM-DD HH:MM' plus the "
         "explicit timezone name — never a bare relative phrase.",
         {"type": "object", "properties": {
             "to": {"type": "string", "description":
                    "one address, or several comma-separated — several means ONE email "
                    "addressed to all of them"},
             "subject": {"type": "string"}, "body": {"type": "string"},
             "ref": {"type": "string"}, "topic": {"type": "string",
                     "description": "required only when `ref` is omitted (a brand-new meeting)"}},
          "required": ["to", "body"]}),
    Tool("apply", "The ONLY tool that writes the calendar — binding. change=create books a meeting "
         "your user organizes (sends the real invite; attendee,start,end,title,ref — `attendee` "
         "takes one or MORE addresses comma-separated: multiparty is ONE event with all of them). "
         "change=rsvp answers your seat on a RECEIVED invite (same params — it RSVPs THEIR event, "
         "never creates a copy); response=accepted (default) or response=declined — declining an "
         "invite REQUIRES this call, an email saying no does not free the slot. "
         "change=move / cancel rewrite a meeting your user ORGANIZES: "
         "start/end locate it (pass title AND attendee when several overlap); move also takes "
         "new_start/new_end. Infra refuses move/cancel on a meeting someone else organizes (send "
         "a plain message proposing the change instead) and refuses a slot holding a DIFFERENT "
         "engagement (conflict) unless override_conflict=true — allowed ONLY when policy "
         "explicitly covers the clash; otherwise ask_user. It also refuses a slot that takes "
         "the LAST time your user's focus work needs (focus_conflict) unless "
         "override_focus=true — same rule: only when policy covers it.",
         {"type": "object", "properties": {
             "change": {"type": "string", "enum": ["create", "move", "cancel", "rsvp"]},
             "response": {"type": "string", "enum": ["accepted", "declined"]},
             "ref": {"type": "string"},
             "start": _TIME, "end": _TIME, "new_start": _TIME, "new_end": _TIME,
             "attendee": {"type": "string"}, "title": {"type": "string"},
             "timezone": _TZ, "override_conflict": {"type": "boolean"},
             "override_focus": {"type": "boolean"}},
          "required": ["change", "start", "end"]}),
    Tool("check", "Busy blocks on the user's OWN calendar(s) in a window — call before any apply "
         "into a specific slot. Pass who=<email> to ask about someone ELSE: returns "
         "accessible:false (you can't see their calendar — message them to ask instead).",
         {"type": "object", "properties": {"start": _TIME, "end": _TIME, "timezone": _TZ,
                                           "who": {"type": "string"}},
          "required": ["start", "end"]}),
    Tool("get_negotiation", "One negotiation's full history, by its ref.",
         {"type": "object", "properties": {"ref": {"type": "string"}}, "required": ["ref"]}),
    Tool("find_contact", "A display name ('john') → email address, via the account's own "
         "address book.",
         {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}),
    Tool("search_events", "Search the user's own calendars by meaning (title text and/or "
         "attendee) — e.g. 'the F1 meeting' resolves even once its negotiation is forgotten.",
         {"type": "object", "properties": {
             "query": {"type": "string"}, "attendee": {"type": "string"},
             "days_back": {"type": "integer"}, "days_ahead": {"type": "integer"}}}),
    Tool("ask_user", "Escalate to your user for a decision or free-text guidance. Use when policy "
         "is silent or a commitment needs a human. Pass the meeting's ref. Your user reads "
         "`summary` first, on a small card: make it ONE short line (max 120 chars) in your "
         "user's language, saying what you need decided — no greeting, no quoted mail. Put "
         "the full context in `question`. Fill who/title/start/end when you know them.",
         {"type": "object", "properties": {
             "question": {"type": "string",
                          "description": "Self-contained question with all the context."},
             "summary": {"type": "string",
                         "description": "One line, max 120 chars: the decision you need."},
             "ref": {"type": "string"},
             "who": {"type": "string", "description": "The other person, by name if known."},
             "title": {"type": "string", "description": "The meeting or mail subject."},
             "start": {"type": "string", "description": "ISO start of the meeting, if any."},
             "end": {"type": "string", "description": "ISO end of the meeting, if any."}},
          "required": ["question", "summary"]}),
    Tool("close", "Memory-only close of a negotiation the user abandoned ('never mind'). "
         "outcome=abandoned|declined. If a peer was already contacted, also send() them a plain "
         "cancellation.",
         {"type": "object", "properties": {"ref": {"type": "string"},
                                           "outcome": {"type": "string",
                                                       "enum": ["abandoned", "declined"]},
                                           "reason": {"type": "string"}},
          "required": ["ref"]}),
    Tool("remember_policy", "Persist a durable scheduling rule or preference your user just gave you "
         "(e.g. 'always keep Friday afternoons free', 'never book before 9am'), so it survives "
         "restarts and shapes every future decision. Use ONLY for standing guidance — never for a "
         "one-off choice about a single meeting.",
         {"type": "object", "properties": {"guidance": {"type": "string"}}, "required": ["guidance"]}),
    Tool("defer_action", "Record a CONDITIONAL next step to finish later, once a peer response or "
         "event makes it possible — e.g. 'reschedule with X, THEN accept Y'. Do the first step now, "
         "then defer_action on the blocked ref, saying exactly what to do and what unblocks it. It's "
         "shown to you EVERY turn until done.",
         {"type": "object", "properties": {"ref": {"type": "string"},
                                           "follow_up": {"type": "string"}},
          "required": ["ref", "follow_up"]}),
]
_TOOL_NAMES = {t.name for t in TOOLS}

_SYSTEM = """You are Parley, a scheduling agent acting for {owner} (addresses: {addresses}).
You negotiate meetings on your user's behalf with other people's agents, by email, using tools.
You only ever see your user's own calendar — never anyone else's.

Current date & time in your calendar's timezone ({tz}): {now_local}.
Resolve every relative date your user OR a peer says — "today", "tomorrow", "this afternoon",
"next Monday" — against THIS local date, never against UTC. ({now} UTC — machine reference only.)

Your user's standing policy — follow it:
{policy}

You may be handling several negotiations at once. Current open negotiations:
{negotiations}

Your user's focus time — work they keep time for before a meeting (Parley places and moves
these blocks itself; you never create, move or delete them):
{focus}

The two rules that govern all scheduling:
1. PROPOSALS NEVER MUTATE. Times are agreed over plain messages (send); nothing touches any
   calendar until apply. apply(move/cancel) works only on meetings YOUR USER organizes — infra
   refuses an attendee's write; propose the change to the organizer instead.
2. FIRST WRITE OWNS THE EVENT — the asker books, the responder answers seats. YOUR ROLE IS
   FIXED BY `direction` in the negotiations list: outgoing = you are the ASKER (you book);
   incoming = you are the RESPONDER (you NEVER apply(create) for it).
   * Your user gave a SPECIFIC time → check, then apply(create). The real invite IS the one
     and only email: no message before, no confirmation after.
   * ASKER, no time agreed yet → send() proposing a slot (or, for a peer whose calendar you
     can't see, ask them for a window in plain language) until you both agree, THEN
     apply(create) — that IS the confirmation, the invite carries it.
   * RESPONDER to a peer's request (not a real invite) → policy decides (ask_user first if it
     says so); agree by send()ing a plain acceptance on the same slot — do NOT apply(create):
     the asker books and will send you the invite (infra refuses a responder's create).
     Counter with another send(), or decline.
   * A REAL invite (the slot is fixed) — the peer, or a HUMAN on their side who ignored the
     agent handshake and just booked, has committed; that ALWAYS wins over an in-flight
     exchange, even one you started as the asker. Whether to accept is your policy's call like
     any incoming meeting (ask_user first if policy says so); then apply(rsvp) — your seat on
     THEIR event, never a new one.
     SAYING NO IS ALSO AN apply: apply(change=rsvp, response=declined). A received invite is
     already ON your user's calendar holding that slot — only the RSVP removes it. Emailing
     "that time doesn't work, how about Tuesday?" and stopping there leaves your user
     double-booked while you believe you handled it. Decline FIRST, then send() the
     counter-proposal. Never tell your user you declined unless you called apply(rsvp,
     response=declined).

MULTIPARTY (one meeting, several attendees): still ONE meeting, ONE ref — every send to
every party carries that same ref. Ask ONLY the parties whose availability you don't know, in
PARALLEL, with the same window; NEVER tell one party another's answer. Intersect the replies
with your own calendar; when one slot fits every REQUIRED party, apply(create) ONCE with ALL
attendees comma-separated — optional parties are invited but never block.

MAKE ROOM (no common free slot — someone's time must yield; policy prices WHOSE):
- Your user's policy declares each person's STANDING (boss / teammate / report / external) and
  each meeting's MOVABILITY (pinned … fluid). Price the yield: teammate and your blocking
  meeting matters → expect THEM to make room (propose it, don't move yours); teammate and yours
  is trivial/fluid → move yours, a cheap concession; boss or senior → move yours. Policy silent
  on either input → ask_user with 2–3 concrete options; NEVER pick a silent default.
- YOUR USER'S OWN WORK IS A PARTY TOO. A focus block that still shows FREE may be booked over:
  Parley moves it. But when check reports `focus` with state_after=short, or apply refuses
  with focus_conflict, the slot takes the LAST time that work can fit — price it like any
  make-room: policy says this person/meeting outranks the prep → override_focus=true;
  otherwise propose another time; policy silent → ask_user with both options.
- OFFER BOUNDS: policy may bound what may be offered to whom ("never offer before 10:00 to
  external people") — every slot you propose must respect them.

How to act:
- REFS: your first send/apply about a new meeting mints its ref (pass `topic` instead on that
  first send) — thread it through every later call. An INCOMING request/invite already carries
  its ref: use it, never start a parallel one.
- Before any apply into a specific slot, check. A busy slot is a CLASH even when the occupant
  looks unrelated — apply refuses it. override_conflict=true ONLY when policy or learned
  guidance explicitly covers that clash; otherwise ask_user.
- CONDITIONAL / MULTI-STEP guidance ("reschedule with X, THEN accept Y"): do the first step now,
  then defer_action on the blocked ref — it shows every turn until done.
- CONTINUITY: your final text ENDS the conversation — the next turn remembers only your
  negotiations list and parked questions. NEVER end a turn with a question in plain text: call
  ask_user with a SELF-CONTAINED question and the ref, then stop.
- An incoming meeting request/invite must never end in silence: act, counter, decline, or
  ask_user. Silence gets escalated to your user as a failure.
- A negotiation marked awaiting_human is FROZEN: never book, confirm, rsvp or decline it until
  your user answers.
- The user names meetings by meaning ('the F1 meeting'): resolve via your negotiations, then
  search_events. Several matches → ask_user which.
- Times: NEVER convert timezones yourself. Copy an ISO time from a message VERBATIM; pass your
  user's local words as plain 'YYYY-MM-DDTHH:MM' with NO offset. When you WRITE a time to a
  peer, always spell out the date, both times, and the zone — never a bare relative phrase.
- Standing guidance from your user (a rule for the future) → remember_policy, then act on it.
- When you are finished, reply with a short plain-text summary. Do not call more tools once done."""


class Brain:
    def __init__(self, agent, llm_client=None, model=None, max_steps=MAX_STEPS):
        self.agent = agent
        self.client = llm_client
        self.model = model
        self.max_steps = max_steps
        # Learned policy is agent-level now (one global + optional per-account file),
        # not one per Brain instance — see Agent._learned.

    # == entry points =========================================================
    #
    # Each one PREPARES a turn and then runs it in-process. The two halves are
    # separable on purpose (phase 0): a session driver prepares the same turn and
    # runs it from the other side. Signatures unchanged — every caller is untouched.

    def handle(self, message, conn, payload=None, note=None):
        """An admitted inbound email (peer message or free-text). `note` is an
        optional infra preamble — e.g. a parked question resuming with the
        human's answer attached."""
        return self.run(self.turn_for_message(message, conn, payload=payload, note=note))

    def instruct(self, text, conn):
        """A client-server instruction the user typed."""
        return self.run(self.turn_for_instruction(text, conn))

    def on_timer(self, conn, note):
        """A timer fired for a stalled negotiation: infra hands the brain a
        self-contained note and it decides — nudge the peer, wait, or escalate."""
        return self.run(self.turn_for_timer(conn, note))

    def user_answered(self, conn, question, answer, negotiation_id=None):
        """The user replied to an ask_user escalation — accept / deny / or
        free-text guidance. The brain re-plans on it."""
        return self.run(self.turn_for_answer(conn, question, answer, negotiation_id))

    # == preparing a turn (driver-agnostic) ===================================

    def turn_for_message(self, message, conn, payload=None, note=None):
        """Correlate an inbound mail to its negotiation and describe it as an event.
        Correlation happens HERE, not in the driver: it is deterministic and must
        happen exactly once per message however the turn is later run."""
        from parley.agent import wire
        payload = payload if payload is not None else wire.parse(message)
        neg_id = self._correlate(message, payload, conn)
        event = self._describe_inbound(message, payload, neg_id, conn.address())
        if note:
            event = f"{note}\n\n{event}"
        return Turn(conn=conn, event=event, kind="inbound", message=message,
                    negotiation_id=neg_id or "", payload=dict(payload or {}))

    def turn_for_instruction(self, text, conn):
        event = (f"Your user just gave you this instruction (typed on their machine, so any "
                 f"local time is in your calendar's timezone {self._timezone(conn)} unless they "
                 f"state another):\n\"{text.strip()}\"")
        return Turn(conn=conn, event=event, kind="instruction")

    def turn_for_timer(self, conn, note):
        return Turn(conn=conn, event=note, kind="timer")

    def turn_for_answer(self, conn, question, answer, negotiation_id=None):
        rel = f"(relates to negotiation {negotiation_id}.)\n" if negotiation_id else ""
        event = (f"Your user answered a question you had asked them.\n"
                 f"Question: \"{question}\"\nTheir answer: \"{answer}\"\n{rel}"
                 f"Act on their answer. If it contains standing guidance (a rule for the future, "
                 f"not just this one meeting), also call remember_policy to persist it.")
        return Turn(conn=conn, event=event, kind="answer",
                    negotiation_id=negotiation_id or "")

    # -- a Turn across a process boundary -------------------------------------
    #
    # The queue is durable, so a turn has to survive being written to disk and read
    # back by a later process. Everything in a Turn is plain data except `conn` (a live
    # backend) and `message` (a `core.Message`, which is a plain dataclass and so does
    # round-trip). The account is carried by ADDRESS and resolved on the way back in.

    @staticmethod
    def turn_to_payload(turn):
        msg = turn.message
        return {"kind": turn.kind, "event": turn.event,
                "negotiation_id": turn.negotiation_id,
                "account": turn.conn.address(),
                "payload": turn.payload or {},
                "first_contact": bool(turn.first_contact),
                "message": ({"id": msg.id, "sender": msg.sender, "subject": msg.subject,
                             "body": msg.body, "invite": msg.invite,
                             "message_id": msg.message_id, "in_reply_to": msg.in_reply_to,
                             "references": msg.references,
                             "recipients": list(msg.recipients or [])}
                            if msg is not None else None)}

    def turn_from_payload(self, payload):
        """Rebuild a Turn queued earlier. An account we no longer recognise is an ERROR,
        not a fallback to the first connection: running someone else's turn as the wrong
        identity is the failure this whole design is trying to avoid."""
        from parley.core import Message
        address = (payload.get("account") or "").lower()
        conn = next((c for c in self.agent.connections if c.address().lower() == address), None)
        if conn is None:
            raise LookupError(
                f"queued turn names account {address!r}, which this agent does not have "
                f"(has: {', '.join(sorted(self.agent.addresses))})")
        raw = payload.get("message")
        return Turn(conn=conn, event=payload.get("event", ""),
                    kind=payload.get("kind", "event"),
                    message=Message(**raw) if raw else None,
                    negotiation_id=payload.get("negotiation_id", ""),
                    payload=dict(payload.get("payload") or {}),
                    first_contact=bool(payload.get("first_contact")))

    def context(self, turn):
        """Everything needed to reason about `turn`, and nothing about who reasons:
        the system prompt and the tool schemas. FROZEN at this moment — rebuilding it
        mid-turn let the model's own writes reappear to it as pre-existing state."""
        return {"system": self._system(turn.conn), "tools": TOOLS}

    # == the in-process driver ================================================

    def run(self, turn):
        """Drive `turn` to completion here, in this process: the tool-use loop against
        our own `LLMClient`. The session driver (phase 2) is an alternative to THIS
        method alone — everything it needs is `context()`, `dispatch()` and the Turn."""
        log = self.agent.log
        conn = turn.conn
        if self.client is None:
            log.log("error", where="brain", error="no LLM client configured", event=turn.event,
                    human=f"🛑 no LLM client configured — cannot act on: {turn.event[:80]}…")
            return {"ok": False, "message": "no LLM client", "text": ""}
        log.log("brain_turn", account=conn.name, event=turn.event)
        transcript = [{"role": "user", "content": turn.event}]
        final = ""
        ctx = self.context(turn)
        system, tools = ctx["system"], ctx["tools"]
        self.agent.current_turn_message = turn.message
        try:
            for step in range(self.max_steps):
                try:
                    resp = self.client.complete(system, transcript, tools)
                except Exception as err:  # noqa: BLE001 — an API failure must not kill the turn
                    log.log("error", where="llm", step=step, error=str(err),
                            human=f"🛑 LLM call failed: {err}")
                    return {"ok": False, "message": f"LLM call failed: {err}", "text": ""}
                log.log("llm_step", step=step, text=resp.text, usage=resp.usage,
                        tool_calls=[{"name": c.name, "input": c.input} for c in resp.tool_calls])
                transcript.append({"role": "assistant", "text": resp.text,
                                   "tool_calls": resp.tool_calls})
                if resp.text:
                    final = resp.text
                if resp.done:
                    break
                for call in resp.tool_calls:
                    result = self.dispatch(conn, call)
                    log.log("tool_result", tool=call.name, input=call.input, result=result)
                    transcript.append({"role": "tool", "tool_call_id": call.id,
                                       "content": json.dumps(result, default=str)})
        finally:
            self.agent.current_turn_message = None
        log.log("brain_done", text=final, human=f"🤖 {final}" if final else None)
        return {"ok": True, "text": final, "transcript": transcript}

    def dispatch(self, conn, call):
        method = getattr(self.agent.accounts_gate, call.name, None)
        if method is None or call.name not in _TOOL_NAMES:
            return {"ok": False, "error": f"unknown tool {call.name}"}
        # Journalling sits OUTSIDE the try on purpose: everything the try catches is
        # reported to the model as the TOOL having failed, so anything in there that
        # is not the tool itself is a lie. (Live 2026-09-19: an emoji in the console
        # echo raised on a cp1252 stdout and every tool came back as a
        # UnicodeEncodeError, which a session then reported as Parley being broken.)
        self.agent.log.log(
            "tool_call", tool=call.name, input=call.input,
            human=f"🔧 {call.name}({', '.join(f'{k}={v!r}' for k, v in call.input.items())})")
        try:
            return method(conn, **call.input)
        except TypeError as err:
            # A missing/unknown argument is the MODEL mis-calling a tool, not a
            # failure of the thing it asked for. Say so in the tool's own terms:
            # a raw Python signature error ("check() missing 1 required positional
            # argument: 'end'") teaches the next turn nothing and lands in the
            # journal the Troubleshooting section tells users to read (2026-09-06).
            return {"ok": False, "error": f"bad arguments for {call.name}: {err}",
                    "note": "re-read this tool's parameters and call it again with "
                            "every required one"}
        except Exception as err:  # noqa: BLE001 — never crash the loop on a bad call
            return {"ok": False, "error": f"{type(err).__name__}: {err}"}

    # == context ==============================================================

    def _timezone(self, conn):
        """The timezone the brain interprets local times in: an explicit cfg
        override, else this connection's own calendar timezone, else UTC."""
        if self.agent.tz:
            return self.agent.tz
        try:
            return conn.timezone() or "UTC"
        except Exception:  # noqa: BLE001 — never let a tz lookup break a turn
            return "UTC"

    def _system(self, conn):
        now = datetime.now(timezone.utc)
        return _SYSTEM.format(
            owner=self.agent.name, addresses=", ".join(sorted(self.agent.addresses)),
            now=now.isoformat(), now_local=self._now_local(now, conn), tz=self._timezone(conn),
            policy=self.agent.effective_policy_text(conn),
            negotiations=self.agent.short_term_memory.summary(),
            focus=self.agent.focus.summary())

    def _now_local(self, now, conn):
        try:
            from zoneinfo import ZoneInfo
            return now.astimezone(ZoneInfo(self._timezone(conn))).isoformat()
        except Exception:  # noqa: BLE001 — unknown zone → just repeat UTC
            return now.isoformat()

    def _correlate(self, message, payload, conn):
        """Ensure ShortTermMemory knows about the negotiation this inbound belongs
        to (by the deterministic ref) and record the event. Returns the
        negotiation id or None."""
        mem = self.agent.short_term_memory
        neg_id = payload.get("negotiation_id")
        if not neg_id:
            return None
        existing = mem.get(neg_id)
        if existing is None:
            # First contact under this ref — register it INCOMING so this side
            # stays the responder (a stray apply(create) is refused).
            mem.upsert(neg_id, account=conn.address(), direction="incoming", status="open",
                      topic=payload.get("title", "Meeting"),
                      slot={"start": payload.get("start"), "end": payload.get("end")})
            mem.add_party(neg_id, payload.get("from") or message.sender)
        mem.record(neg_id, f"received a message from {message.sender}")
        return neg_id

    @staticmethod
    def _describe_inbound(message, payload, neg_id, me=None):
        lines = ["A new email arrived. Decide what to do and act with your tools.",
                 f"From: {message.sender}"]
        # Who else could read this decides who your answer may reach. Parley used to
        # hold N private conversations about one meeting, so nobody could see who else
        # had been asked; now one meeting is one conversation, and the reply mode has
        # to follow the message (user, 2026-09-24).
        others = message.others(me) if me else []
        if others:
            lines.append(f"Also addressed to: {', '.join(others)}")
            lines.append("This went to several people, so reply to ALL of them: pass every "
                         "address to `send`, in one message.")
        elif message.was_private(me) and neg_id:
            lines.append("This was sent to you ALONE, even though the meeting involves other "
                         "people — a private aside. Answer this person only, and do not "
                         "repeat what they said to the others unless they ask you to.")
        lines += [f"Subject: {message.subject or '(no subject)'}",
                  "Body:", html_to_text(message.body) or "(empty)"]
        if payload.get("via_invite"):
            lines.append("This is a REAL CALENDAR INVITE: the sender already scheduled this "
                         "meeting, so the slot is fixed — do NOT negotiate it. Whether to "
                         "ACCEPT is your policy's call like any incoming meeting: if your "
                         "policy says ask your user before new meetings, ask_user FIRST. "
                         "Then apply(rsvp) your seat, or decline.")
        elif neg_id:
            lines.append("This is a reply in an ongoing negotiation (read what they wrote and "
                         "act on it: they may agree to a time, counter with another, ask a "
                         "question, or decline — all in prose). Check get_negotiation for "
                         "what was proposed before you answer.")
        if neg_id:
            lines.append(f"This message belongs to negotiation {neg_id} "
                         f"(use get_negotiation for its history).")
        return "\n".join(lines)


def _read_learned(path):
    """The learned-policy file's contents (guidance the user taught the agent), or ''
    if there's no path or no file yet. Best-effort — never fatal at construction."""
    if not path:
        return ""
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""
