"""AccountsGate (renamed from ToolBelt, 2026-08-31) — the brain's hands: thin,
connection-explicit, permission-gated primitives over the connectors.

The tools carry **no judgment**. Each is a dumb verb — look at the calendar,
write an event, send a message, ask the human. *All* the deciding (accept?
move? propose? ask?) lives in the brain's prompt + the policy prose.

Lives in `parley/agent/`, not `connectors/` (2026-08-31 refactor — see
docs/iterations/refactor-plan.md "Package boundary — revised"): what this adds
over a raw `Account` call — idempotent writes, the permission hook — protects
against Parley's own polling/restart runtime (a resent message, a crash mid-turn),
not a generic hazard of "using an Account". A future Claude-skill invocation of
Parley shares this file wholesale (no poll loop, but still real ShortTermMemory
and real writes); it does NOT get a stripped-down substitute.

Two invariants kept from before:
  * **Connection-explicit** — one agent drives several accounts, so every tool takes
    the `conn` it acts on. The brain injects it; it is not a model-visible argument.
  * **Permission-gated** — calendar writes and outward mail pass `permission()`
    (guardrail.py) before they run; an ASK parks the action for the human via
    `UserMessaging` and an affirmative answer grants a one-shot approval.

**Scope, narrowed from the old ToolBelt (2026-08-31 inventory pass).** Only the
outward-mutating verbs need the deterministic write guard: `apply` (calendar
writes) and `send` (peer mail). Reads (`check`, `get_negotiation`,
`find_contact`, `search_events`) and the low-stakes memory writes (`close`,
`remember_policy`, `defer_action`) don't — each of those owns whatever small
idempotency it needs locally (ShortTermMemory's own dedupe-on-mutate,
UserMessaging's one-ask-per-thing), not this shared gate.

**tz-conversion**: still done HERE (`_when`), not yet pushed down into each
connector as the design intends — that needs real new code in every backend
(confirmed reading them: none currently interpret a naive local time at all,
they only handle already-UTC-aware datetimes), which wasn't done in this pass.
Flagged, not silently dropped — see docs/iterations/refactor-plan.md.
"""

from datetime import datetime, timedelta, timezone as _utc_tz
from zoneinfo import ZoneInfo

from parley.connectors.accounts.account import InviteNotReady
from parley.agent import wire
from parley.agent.guardrail import ALLOW, ASK, Action, permission
from parley.core import SHOW_AS, Slot, as_addresses, focus_title, from_iso, to_iso


def _event_uid(event):
    """The iCalUID of a just-booked event, whatever the backend calls it in `raw`
    (Google `iCalUID`, Graph `iCalUId`, CalDAV/test-account `uid`), or None."""
    raw = getattr(event, "raw", None)
    if isinstance(raw, dict):
        return raw.get("iCalUID") or raw.get("iCalUId") or raw.get("uid")
    return None


class AccountsGate:
    # How many messages this agent may send to ONE party about ONE meeting before it
    # has to stop and ask the human. Arranging a meeting takes two or three exchanges;
    # six is already a conversation that is not converging, and the far side is a real
    # person watching their inbox fill up. Deliberately generous, because tripping it
    # costs the user an interruption — the point is to bound the pathological case
    # (10 of 14 outgoing mails to one colleague, 2026-09-24), not to police normal work.
    MAX_SENDS_PER_PARTY = 6

    def __init__(self, agent):
        self.agent = agent

    @property
    def name(self):
        return self.agent.name

    # -- gating ---------------------------------------------------------------

    def _gate(self, conn, kind, target=None, title=None, negotiation_id=None):
        """The permission gate every mutating tool passes. Returns None when the
        action may run, else the JSON-able hold the tool returns to the brain.

        ASK is a real gate, not a silent hold: the action is parked as a human
        question (`UserMessaging.ask`, one ask per thing), and the item carries
        the action so an affirmative answer grants a **one-shot** approval — the
        brain's retry then passes here exactly once."""
        if self.agent.user_messaging.consume_grant(conn.address(), kind, target or ""):
            self.agent.log.log("permission_grant", action=kind, target=str(target),
                               human=f"✅ human-granted '{kind}' → {target} (one-shot).")
            return None
        verdict = permission(Action(kind, target=target, title=title),
                             self.agent.policy_for(conn).structured)
        if verdict == ALLOW:
            return None
        if verdict == ASK:
            label = title or kind.replace("_", " ")
            question = f"May I {kind.replace('_', ' ')}" \
                       + (f" with {target}" if target else "") \
                       + (f" — '{title}'" if title else "") + "?"
            ref = self.agent.escalate(conn, question, negotiation_id=negotiation_id,
                                      action={"kind": kind, "target": target})
            self.agent.log.log("permission_hold", verdict=verdict, action=kind,
                               target=str(target), ref=ref,
                               human=f"⛔ permission ASK '{kind}' → {target} (asked, ref:{ref}).")
            return {"ok": False, "held": True, "asked": ref,
                    "note": "a human was asked; do not retry — wait for their answer"}
        self.agent.log.log("permission_hold", verdict=verdict, action=kind,
                           target=str(target),
                           human=f"⛔ permission {verdict} '{kind}' → {target} (held).")
        return {"ok": False, "held": True, "denied": True}

    def _when(self, conn, value, tz=None):
        """One model-supplied time → an aware UTC datetime. The ONE place timezone
        math happens (still infra-owned here, not yet pushed into each connector —
        see the module docstring):
          * value carries a UTC offset → taken verbatim (times echoed from messages);
          * value is naive local wall-clock ('2026-07-05T09:15') → interpreted in `tz`
            (the model passes one ONLY when the user/peer named a zone) else the
            calendar's own timezone, then converted to UTC.
        An unresolvable zone RAISES instead of degrading to UTC — a wrong instant
        is worse than no booking."""
        dt = datetime.fromisoformat(str(value))
        if dt.tzinfo is None:
            name = tz or self.agent.tz or conn.timezone() or "UTC"
            try:
                zone = ZoneInfo(name)
            except Exception as err:  # noqa: BLE001 — missing tzdata / unknown zone
                raise ValueError(
                    f"can't resolve timezone '{name}' ({err}) — refusing to guess: a "
                    f"wrong instant is worse than no booking. Is the tzdata package "
                    f"installed in this environment?") from err
            dt = dt.replace(tzinfo=zone)
        return dt.astimezone(_utc_tz.utc)

    def _slot(self, conn, start, end, tz=None):
        """[start, end] as a UTC Slot via `_when` — every tool time goes through here."""
        return Slot(self._when(conn, start, tz), self._when(conn, end, tz))

    def _find(self, conn, slot, title=None, attendee=None):
        """The event on `conn` overlapping `slot`, narrowed by `title` and/or `attendee`
        when given — so a reschedule/delete targets the RIGHT meeting when several share
        a slot. Title matches case-insensitively, exact first then substring; attendee
        matches by membership. When a disambiguator is given but nothing matches,
        returns None — never falls back to an arbitrary overlap."""
        matches = conn.list_events(slot)
        if title:
            t = title.lower()
            matches = ([e for e in matches if (e.title or "").lower() == t]
                       or [e for e in matches if t in (e.title or "").lower()])
        if attendee:
            a = attendee.lower()
            matches = [e for e in matches
                       if any(a == (x or "").lower() for x in (e.attendees or []))]
        return matches[0] if matches else None

    # == memory-only (no outward effect) =====================================

    def close(self, conn, ref, outcome="abandoned", reason=""):
        """Memory-only close of a negotiation ('never mind'). If a peer was already
        contacted, also send(CANCEL) so their agent isn't left waiting — the brain
        does that itself with a separate `send` call; this just settles our state."""
        mem = self.agent.short_term_memory
        if mem.get(ref) is None:
            return {"ok": False, "error": f"no negotiation {ref}"}
        status = "declined" if outcome == "declined" else "cancelled"
        mem.record(ref, f"{status}: {reason or 'user abandoned it'}")
        mem.close(ref, status)
        self.agent.log.log("settle", negotiation_id=ref, status=status, where="close")
        return {"ok": True, "closed": ref}

    def defer_action(self, conn, ref=None, follow_up=""):
        """Record a conditional next step on a negotiation (resumable negotiation):
        it's surfaced in the memory summary every turn until carried out."""
        mem = self.agent.short_term_memory
        if mem.get(ref) is None:
            return {"ok": False, "error": f"no negotiation {ref}"}
        mem.set_follow_up(ref, follow_up)
        mem.record(ref, f"deferred: {follow_up}")
        self.agent.log.log("defer_action", negotiation_id=ref, follow_up=follow_up,
                           human=f"⏭️  deferred on {ref}: {follow_up}")
        return {"ok": True, "deferred": ref, "follow_up": follow_up}

    def remember_policy(self, conn, guidance):
        """Persist durable user guidance (survives restarts, applied on every future
        turn). Routes to the Agent — Policy's learned file is agent-level, not
        per-tool state."""
        return self.agent.remember_policy(guidance)

    # == read-only ============================================================

    def check(self, conn, start, end, who=None, timezone=None):
        """Busy blocks on the user's OWN calendars in a window; `who` = a peer's
        email asks about THEM instead (returns accessible:false — you cannot see
        another person's calendar, email them for their times)."""
        if who and who.lower() not in self.agent.addresses:
            return {"ok": True, "who": who, "accessible": False,
                    "note": "you cannot see this person's calendar — ask them for their "
                            "times instead: send them a plain question with a window"}
        slot = self._slot(conn, start, end, timezone)
        busy = []
        for c in self.agent.connections:
            for e in c.list_events(slot):
                if _soft_focus(e):
                    continue
                busy.append({"title": e.title, "attendees": e.attendees,
                             "start": to_iso(e.start), "end": to_iso(e.end)})
        out = {"ok": True, "busy": busy, "free": not busy}
        focus = self.agent.focus.impact(slot)
        if focus:
            out["focus"] = focus
            out["focus_note"] = ("booking this slot squeezes your user's focus time (see "
                                 "`focus`); state_after=short means it takes the LAST time "
                                 "that work could fit — a make-room decision for policy")
        return out

    def get_negotiation(self, conn, ref):
        """Full history of one negotiation (the brain sees only summaries by default)."""
        return {"ok": True, "detail": self.agent.short_term_memory.detail(ref)}

    def find_contact(self, conn, name):
        """Display name ("john") → email address via `conn`'s address book, or None."""
        if name and "@" in name:
            return {"ok": True, "address": name}
        addr = conn.find_contact(name)
        return {"ok": addr is not None, "name": name, "address": addr}

    def search_events(self, conn, query=None, attendee=None, days_back=7, days_ahead=60):
        """Search the user's own calendars by meaning — title text and/or attendee —
        so 'move the meeting about F1' resolves even after its negotiation is
        forgotten. Union across all the agent's accounts; capped at 20 matches."""
        now = datetime.now(_utc_tz.utc)
        window = Slot(now - timedelta(days=int(days_back)), now + timedelta(days=int(days_ahead)))
        q = (query or "").strip().lower()
        who = (attendee or "").strip().lower()
        out = []
        for c in self.agent.connections:
            for e in c.list_events(window):
                if q and q not in (e.title or "").lower():
                    continue
                if who and not any(who in (a or "").lower() for a in (e.attendees or [])):
                    continue
                out.append({"title": e.title, "start": to_iso(e.start), "end": to_iso(e.end),
                            "attendees": e.attendees, "account": c.address()})
                if len(out) >= 20:
                    return {"ok": True, "events": out, "truncated": True}
        return {"ok": True, "events": out}

    # == the peer-messaging tool (permission-gated) ===========================

    def send(self, conn, to, body, subject=None, ref=None, topic=None):
        """The ONE peer-messaging tool. The brain writes `body` itself, in its own
        words — no templates, no hidden payload (2026-08-31: wire.py stopped being
        a protocol). Infra's only job: mint a ref for a brand-new topic and make
        sure it's visible in what goes out, so a reply can be correlated back.
        `ref` threads through every later call about the same meeting; the FIRST
        send about it may omit `ref` (one is minted) but must give `topic`.

        `to` is one person or several. Several means ONE email addressed to all of
        them — the way a person convenes a meeting — not one email each: everyone
        sees who else was asked, can reply to all, and the whole thing reads as a
        single conversation. Parley used to hold N private conversations about one
        meeting, which is how the same person ended up in two threads that did not
        know about each other (user, 2026-09-24). To answer one person alone, send
        to that one person: a private aside stays private."""
        recipients = as_addresses(to)
        if not recipients:
            return {"ok": False, "error": "send needs at least one recipient"}
        if ref is None:
            if not topic:
                return {"ok": False, "error": "the first send about a new meeting needs `topic`"}
            # A missing `ref` used to mint a new negotiation unconditionally, so a
            # follow-up the brain forgot to thread — a reschedule confirmation, say —
            # silently opened a SECOND negotiation for a meeting already on record.
            # One meeting then showed up twice: two entries in working memory, and two
            # separate questions for the human (2026-09-06, seen on gmail and yahoo).
            # Reuse the open one for this peer+topic instead; only mint if there is
            # genuinely nothing to thread onto.
            mem = self.agent.short_term_memory
            existing = None
            for addr in recipients:
                existing = mem.find_by_peer_topic(addr, topic, loose=True,
                                                  reach=len(recipients))
                if existing is not None:
                    break
            if existing is None:
                # Multiparty fan-out: same meeting, DIFFERENT peer. Largely historical
                # now that one meeting is one send — it catches a brain that still
                # writes to the parties one at a time (2026-09-06, scenario E).
                existing = mem.find_fanout(topic, conn.address())
            if existing is not None and not mem.is_terminal(existing.id):
                ref = existing.id
                self.agent.log.log(
                    "send_rethreaded", negotiation_id=ref, peer=", ".join(recipients),
                    topic=topic,
                    human=f"🧵 send about '{topic}' threaded onto the open {ref} "
                          f"instead of opening a second negotiation.")
            else:
                ref = wire.new_ref()
                self.agent.short_term_memory.upsert(ref, topic=topic, direction="outgoing",
                                                    status="open", account=conn.address())
                self.agent.short_term_memory.record(
                    ref, f"opened by sending to {', '.join(recipients)}")
        n = self.agent.short_term_memory.get(ref)
        title = topic or (n.topic if n else None) or "Meeting"
        capped = self._sends_exhausted(conn, n, recipients, title)
        if capped:
            return capped
        # The permission gate is per recipient: consent to write to one person is not
        # consent to write to another, and a group message reaches all of them at once.
        for addr in recipients:
            held = self._gate(conn, "send", target=addr, title=subject or title,
                              negotiation_id=ref)
            if held:
                return held
        if wire.extract_ref(body) != ref:
            # The tag is how any reply — from an agent OR a human hitting Reply —
            # gets correlated back to this meeting. It used to go out bare, which
            # reads like debris a tidy person deletes; the sentence tells them why
            # to leave it (user request 2026-09-06).
            body = (f"{body.rstrip()}\n\n(ref: {ref}) — please keep this line when you "
                    f"reply; it is how I keep track of this conversation.")
        # Reply onto the thread this negotiation already has, rather than starting a
        # new conversation every time. The Message-IDs were being recorded all along
        # (link_thread, for correlating replies back) and never used on the way out.
        conn.send_message(recipients, subject or title, body,
                          thread=list(n.thread or []) if n else None)
        for addr in recipients:
            self.agent.short_term_memory.add_party(ref, addr)
            self.agent.short_term_memory.note_sent(ref, addr)
        self.agent.short_term_memory.upsert(ref, status="awaiting_peer")
        self.agent.short_term_memory.record(
            ref, f"sent to {', '.join(recipients)}: {(subject or '')[:60]}")
        return {"ok": True, "negotiation_id": ref, "recipients": recipients}

    def _sends_exhausted(self, conn, n, recipients, title):
        """Stop, and ask the human, once we have written to any of `recipients`
        MAX_SENDS_PER_PARTY times about this one meeting. Returns a refusal dict, or
        None to carry on.

        This is a backstop against a failure mode with no error in it: every single
        turn looks reasonable, the peer keeps replying, and nobody counts. In the
        first real multi-party run one colleague received ten of the fourteen mails
        that went out. The bound is on OUR OUTPUT rather than on the exchange, since
        our output is the part we are responsible for and the part that costs the
        other person their attention.

        Escalating is the whole point: the negotiation is not abandoned, it is handed
        to the person whose meeting it is. `escalate` is idempotent per negotiation,
        so a brain that retries the send parks one question, not a queue of them, and
        answering it resets the budget (`reset_sent`).

        Exhausting ONE recipient stops the whole message rather than quietly dropping
        that person from the To: line: silently removing someone from a group
        conversation is a worse surprise than a pause, and the human is being asked
        anyway."""
        if n is None:
            return None
        mem = self.agent.short_term_memory
        spent = [(addr, mem.sent_to(n, addr)) for addr in recipients]
        over = [(addr, sent) for addr, sent in spent if sent >= self.MAX_SENDS_PER_PARTY]
        if not over:
            return None
        who = ", ".join(f"{addr} ({sent})" for addr, sent in over)
        names = ", ".join(addr for addr, _ in over)
        question = (f"I have already sent this many messages about '{title}' without "
                    f"settling on a time: {who}. I have stopped rather than send "
                    f"another. Do you want me to keep going, try someone else, propose "
                    f"something specific, or drop it?")
        self.agent.log.log(
            "send_capped", negotiation_id=n.id, peer=names, sent=max(s for _, s in over),
            limit=self.MAX_SENDS_PER_PARTY,
            human=f"🛑 {who} messages about '{title}' with no agreement — "
                  f"stopping and asking you instead of sending another.")
        self.agent.short_term_memory.record(
            n.id, f"stopped after {who} messages — asked the user what to do")
        return {"ok": False,
                "error": (f"you have already written this many times about this meeting "
                          f"without settling it: {who}, which is the limit; the user has "
                          f"been asked how to proceed. Do not write to {names} again on "
                          f"this negotiation until they answer."),
                "escalated": self.agent.escalate(
                    conn, question, negotiation_id=n.id, kind="limit",
                    who=names, summary="Many messages and no agreement yet. "
                                       "Keep going, try something else, or drop it?")}

    # == the calendar writer (permission-gated + idempotent) ==================

    def apply(self, conn, change, ref=None, start=None, end=None, new_start=None,
              new_end=None, attendee=None, title=None, timezone=None,
              override_conflict=False, response=None, override_focus=False):
        """The ONLY calendar writer — binding. change=create/rsvp put a meeting (or
        your seat on a received invite) on the calendar; move/cancel rewrite one —
        organizer-only. change=rsvp takes response=accepted|declined (default
        accepted) — declining is a real RSVP on THEIR event, not just an email."""
        change = (change or "").lower()
        if change in ("create", "rsvp"):
            if not attendee:
                return {"ok": False, "error": f"apply({change}) needs the attendee"}
            if change == "rsvp" and (response or "").lower() == "declined":
                return self._decline(conn, attendee, start, end,
                                     title=title or "Meeting", negotiation_id=ref,
                                     timezone=timezone)
            return self._create(conn, attendee, start, end, title=title or "Meeting",
                                negotiation_id=ref, timezone=timezone,
                                override_conflict=override_conflict,
                                override_focus=override_focus,
                                answering=(change == "rsvp"))
        if change == "move":
            return self._move(conn, start, end, new_start, new_end,
                              timezone=timezone, title=title, attendee=attendee)
        if change == "cancel":
            return self._cancel(conn, start, end, timezone=timezone,
                                title=title, attendee=attendee)
        return {"ok": False, "error": f"unknown change {change!r} — use create / move / cancel / rsvp"}

    def _create(self, conn, attendee, start, end, title="Meeting", negotiation_id=None,
               timezone=None, override_conflict=False, answering=False,
               override_focus=False):
        """Booking is where a negotiation *settles*. Three guards, enforced HERE,
        never by the model:
          * a `negotiation_id` that is already terminal → no calendar write at all;
          * an event for this peer already overlapping the slot → no second write;
          * the conflict guard: the slot holds a DIFFERENT engagement → no write
            unless `override_conflict`, journaled.
        An invite-originated negotiation (id `mtg-i…`) is passed `must_rsvp` so the
        backend never creates a duplicate when the invite's event hasn't landed yet.

        Multiparty: `attendee` may be several addresses (comma-separated or a
        list) — ONE event, N attendees, one invite fan-out by the provider."""
        mem = self.agent.short_term_memory
        attendees = ([a.strip() for a in attendee.split(",") if a.strip()]
                     if isinstance(attendee, str) else [a for a in (attendee or []) if a])
        if not attendees:
            return {"ok": False, "error": "create needs at least one attendee"}
        attendee = attendees[0]
        if negotiation_id and mem.is_terminal(negotiation_id):
            return {"ok": True, "already_booked": True, "negotiation_id": negotiation_id}
        # RESPONDER never self-organizes: a create on an INCOMING negotiation that
        # isn't answering a real invite is refused — send(...) instead, the asker books.
        if (negotiation_id and not answering
                and not str(negotiation_id).startswith("mtg-i")):
            neg = mem.get(negotiation_id)
            if neg is not None and neg.direction == "incoming":
                self.agent.log.log(
                    "responder_create_refused", negotiation_id=negotiation_id, peer=attendee,
                    human=f"⛔ responder on {negotiation_id} — the asker books; "
                          f"send() instead of apply(create).")
                return {"ok": False, "held": True,
                        "note": "You are the RESPONDER on this negotiation (they asked you). "
                                "Don't apply(create) — the asker books and sends the invite. "
                                "Agree by send()ing a plain acceptance on the same slot, or "
                                "decline. (A real invite from them is different: "
                                "apply(rsvp) its seat.)"}
        held = self._gate(conn, "apply", target=attendee, title=title,
                          negotiation_id=negotiation_id)
        if held:
            return held
        slot = self._slot(conn, start, end, timezone)
        must_rsvp = answering or self._invite_originated(negotiation_id)
        # Pending-invite guard: this peer has an open incoming ask/invite we haven't
        # answered — a create toward them that isn't answering IT is a counter-invite,
        # not an RSVP. Exempt: our own outgoing negotiation already includes this peer
        # as a party (the same-meeting case) — the commit supersedes their parallel
        # thread at settle time instead of holding on it.
        if not must_rsvp:
            pending = self._pending_invite_from(attendee)
            if pending is not None and negotiation_id:
                ours = mem.get(negotiation_id)
                if (ours is not None and ours.direction == "outgoing"
                        and mem.involves(ours, attendee)
                        and (pending.topic or "").strip().lower() == (title or "").strip().lower()):
                    pending = None
            if pending is not None:
                self.agent.log.log(
                    "counter_invite_hold", peer=attendee, pending_negotiation=pending.id,
                    human=f"⛔ {attendee} has a pending invite ({pending.id}) — answer "
                          f"THAT, don't counter-invite.")
                return {"ok": False, "held": True,
                        "open_invite": {"negotiation_id": pending.id, "topic": pending.topic,
                                        "slot": pending.slot},
                        "note": "this peer has a PENDING invite/request to you — answer "
                                "that one: apply(rsvp) with its ref and its exact times "
                                "(or decline it). Don't start a new meeting until it's answered."}
        duplicate = self._existing_with(conn, attendee, slot)
        if not override_conflict and (duplicate is None or must_rsvp):
            conflicts = self._conflicts(attendee, slot)
            if conflicts:
                self.agent.log.log(
                    "conflict_hold", peer=attendee, title=title,
                    start=to_iso(slot.start), end=to_iso(slot.end), conflicts=conflicts,
                    human=f"⛔ '{title}' clashes with {conflicts[0]['title']} — held.")
                return {"ok": False, "conflict": conflicts,
                        "note": "slot occupied by a DIFFERENT meeting — do NOT double-book. "
                                "Resolve it: reschedule your own clashing meeting or ask its "
                                "peer to free it, then retry; otherwise ask_user or decline. "
                                "override_conflict=true ONLY if policy explicitly covers this."}
        if not override_focus and (duplicate is None or must_rsvp):
            squeezed = [i for i in self.agent.focus.impact(slot)
                        if i["state_after"] == "short"]
            if squeezed:
                first = squeezed[0]
                self.agent.log.log(
                    "focus_hold", peer=attendee, title=title, start=to_iso(slot.start),
                    end=to_iso(slot.end), focus=squeezed,
                    human=f"⛔ '{title}' would take the last time for focus "
                          f"'{first['purpose']}' before '{first['meeting']}' — held.")
                return {"ok": False, "focus_conflict": squeezed,
                        "note": "this slot takes the LAST free time your user's focus work "
                                "needs before its meeting (see focus_conflict). MAKE ROOM, "
                                "priced by policy: propose the peer another time; or accept "
                                "anyway with override_focus=true ONLY if policy says this "
                                "person or meeting outranks your user's own prep time; "
                                "policy silent → ask_user (accept and lose the focus time / "
                                "propose another time)."}
        # RESCHEDULE, don't re-create. This negotiation may already have put an event
        # on the calendar at a DIFFERENT time: a previous epoch booked it and reopen()
        # carried the identity (parties/topic/slot/calendar_uid) forward. Creating again
        # would leave the old event stranded — the organizer's calendar keeps a meeting
        # nobody is coming to, and the peer keeps its copy (2026-09-06 scenario A.2:
        # yahoo ended with two '92-Juanito', 15:00 and 11:00). Move the one we already
        # own instead. Only for a meeting WE organize (never must_rsvp — an RSVP answers
        # THEIR event) and only when an event is actually there; otherwise fall through
        # to the normal create, unchanged.
        moved = None
        if duplicate is None and not must_rsvp and negotiation_id:
            moved = self._reschedule_existing(conn, negotiation_id, slot, title, attendee)
        booked = duplicate
        if moved is not None:
            booked = moved          # rescheduled in place — nothing left to write
        elif duplicate is None or must_rsvp:
            try:
                if len(attendees) > 1 and not must_rsvp:
                    booked = conn.create_event(title, slot, attendees)
                else:
                    booked = conn.respond_event("accepted", requester=attendee, slot=slot,
                                                title=title) if must_rsvp else \
                             conn.create_event(title, slot, attendee)
            except InviteNotReady:
                return self._defer_rsvp(conn, negotiation_id, attendee, slot, title)
        neg_id = self._settle_booking(conn, negotiation_id, attendee, slot, title)
        for a in attendees:
            mem.add_party(neg_id, a)
        uid = _event_uid(booked)
        if uid:
            mem.set_calendar_uid(neg_id, uid)
        # Supersede the parties' parallel threads about this same meeting (same topic,
        # same peer, still open): the booking IS the answer.
        tl = (title or "").strip().lower()
        for n in list(mem.open()):
            if (n.id != neg_id and (n.topic or "").strip().lower() == tl
                    and any(mem.involves(n, a) for a in attendees)):
                mem.clear_pending_rsvp(n.id)
                mem.close(n.id, "superseded")
                self.agent.log.log("settle", negotiation_id=n.id, status="superseded",
                                   where="create_sweep", superseded_by=neg_id)
        out = {"ok": True, "negotiation_id": neg_id, "start": to_iso(slot.start), "end": to_iso(slot.end)}
        if duplicate is not None and not must_rsvp:
            out["already_booked"] = True
        return out

    def _conflicts(self, peer, slot):
        """Events overlapping `slot` on ANY of the agent's accounts that do NOT
        involve `peer` — the deterministic double-book check."""
        peer = (peer or "").lower()
        out = []
        for c in self.agent.connections:
            for e in c.list_events(slot):
                if self._event_involves(e, peer) or _soft_focus(e):
                    continue
                out.append({"title": e.title, "start": to_iso(e.start), "end": to_iso(e.end)})
        return out

    @staticmethod
    def _event_involves(event, peer):
        """`peer` appears on the event — as an attendee OR as its ORGANIZER (a
        mail-borne ICS invite may list the sender only as ORGANIZER)."""
        return (peer == (event.organizer or "").lower()
                or any(peer == (a or "").lower() for a in (event.attendees or [])))

    def _pending_invite_from(self, peer):
        """The open incoming negotiation this `peer` started, if any — a create
        toward them that doesn't answer it is a counter-invite, not an RSVP."""
        peer = (peer or "").lower()
        for n in self.agent.short_term_memory.open():
            if n.direction == "incoming" and self.agent.short_term_memory.involves(n, peer):
                return n
        return None

    def _invite_originated(self, negotiation_id):
        """True when the negotiation began as a received invite — the booking must
        RSVP the existing event, never create one."""
        if not negotiation_id:
            return False
        if str(negotiation_id).startswith("mtg-i"):
            return True
        n = self.agent.short_term_memory.get(negotiation_id)
        return bool(n and n.direction == "incoming")

    def _existing_with(self, conn, peer, slot):
        """An event already on `conn` overlapping `slot` that involves `peer` — the
        calendar-level dedupe backstop, straight against the real calendar (not
        just ShortTermMemory) so a duplicate commit signal, a resent invite, or a
        second brain attempt never yields a second event."""
        peer = (peer or "").lower()
        for e in conn.list_events(slot):
            if self._event_involves(e, peer):
                return e
        return None

    def _settle_booking(self, conn, negotiation_id, peer, slot, title):
        """Record + close a negotiation as `booked` at the moment the calendar
        write happens — settlement is infra, never left to the model."""
        mem = self.agent.short_term_memory
        neg_id = negotiation_id or wire.new_ref()
        if mem.get(neg_id) is None:
            mem.upsert(neg_id, topic=title, direction="outgoing", status="booked",
                      account=conn.address(),
                      slot={"start": to_iso(slot.start), "end": to_iso(slot.end)})
        else:
            mem.upsert(neg_id, slot={"start": to_iso(slot.start), "end": to_iso(slot.end)})
        mem.record(neg_id, f"booked with {peer} ({title})")
        mem.close(neg_id, "booked")
        self.agent.log.log("settle", negotiation_id=neg_id, status="booked",
                           where="create", peer=peer, title=title,
                           start=to_iso(slot.start), end=to_iso(slot.end))
        return neg_id

    def _reschedule_existing(self, conn, negotiation_id, slot, title, attendee):
        """If `negotiation_id` already holds an event at a different time, MOVE it to
        `slot` and return it; else None (caller creates as usual).

        The previous slot comes from the negotiation itself — reopen() copies it onto
        the new epoch, so at this point memory still records where the meeting WAS
        while `slot` is where it is going. Organizer-only, via the same ownership guard
        `move` uses: an attendee rewriting the organizer's copy would just desync the
        two calendars.
        """
        neg = self.agent.short_term_memory.get(negotiation_id)
        if neg is None or not neg.slot:
            return None
        try:
            was = Slot(from_iso(neg.slot["start"]), from_iso(neg.slot["end"]))
        except (KeyError, TypeError, ValueError):
            return None
        if was.start == slot.start and was.end == slot.end:
            return None  # same time — nothing to reschedule
        event = self._find(conn, was, title=title, attendee=attendee)
        if event is None or self._ownership_refusal(conn, event, "move") is not None:
            return None
        try:
            conn.update_event(event.id, {"start": slot.start, "end": slot.end})
        except Exception as err:  # noqa: BLE001 — fall back to a plain create
            self.agent.log.log("reschedule_fallback", negotiation_id=negotiation_id,
                               title=title, error=str(err),
                               human=f"⚠️ could not move '{title}' — creating instead: {err}")
            return None
        self.agent.log.log("rescheduled", negotiation_id=negotiation_id, title=title,
                           was=to_iso(was.start), now=to_iso(slot.start),
                           account=conn.address(),
                           human=f"🔀 moved '{title}' {to_iso(was.start)} → {to_iso(slot.start)} "
                                 f"(no duplicate left behind).")
        return event

    def _decline(self, conn, attendee, start, end, title="Meeting",
                 negotiation_id=None, timezone=None):
        """Say NO on a received invite — an actual RSVP on THEIR event, not an email.

        Added 2026-09-06 after scenario A.2: the prompt told the brain to "decline",
        but the only decline it could reach was close(outcome=declined), which sends
        prose and closes the negotiation without ever touching the invite. The invite
        stayed needsAction on the calendar, so the user was left visibly double-booked
        while the agent reported it had declined. Every backend's respond_event has
        handled response="declined" all along — nothing reached it.

        Deliberately NOT a booking: no _settle_booking, no terminal id, and the
        negotiation stays open — declining THIS slot is usually the first half of
        counter-proposing another one.
        """
        held = self._gate(conn, "apply", target=attendee, title=title,
                          negotiation_id=negotiation_id)
        if held:
            return held
        slot = self._slot(conn, start, end, timezone)
        try:
            conn.respond_event("declined", requester=attendee, slot=slot, title=title)
        except InviteNotReady:
            return {"ok": False, "error": "that invite hasn't landed on the calendar yet",
                    "note": "wait for it to arrive, then decline it"}
        if negotiation_id:
            self.agent.short_term_memory.record(
                negotiation_id, f"declined {attendee}'s invite for {to_iso(slot.start)}")
        self.agent.log.log("rsvp_declined", negotiation_id=negotiation_id, peer=attendee,
                           start=to_iso(slot.start), end=to_iso(slot.end), title=title,
                           account=conn.address(),
                           human=f"🚫 declined {attendee}'s invite '{title}' "
                                 f"({to_iso(slot.start)}) — RSVP sent.")
        return {"ok": True, "declined": True, "negotiation_id": negotiation_id,
                "note": "Their invite is RSVP'd DECLINED — it no longer holds that slot. "
                        "If you want another time, send() them a proposal now."}

    def _defer_rsvp(self, conn, negotiation_id, attendee, slot, title):
        """The invite's event hasn't materialized yet: record a pending RSVP so the
        wake cycle retries it, and return a clean 'done — do not retry' result so
        the brain ends the turn instead of hammering apply into its step budget."""
        mem = self.agent.short_term_memory
        neg_id = negotiation_id or wire.new_ref()
        if mem.get(neg_id) is None:
            mem.upsert(neg_id, topic=title, direction="incoming", status="open",
                      account=conn.address(),
                      slot={"start": to_iso(slot.start), "end": to_iso(slot.end)})
        mem.set_pending_rsvp(neg_id, attendee, to_iso(slot.start), to_iso(slot.end), title)
        mem.record(neg_id, f"RSVP deferred — invite from {attendee} not landed yet")
        self.agent.log.log(
            "rsvp_deferred", negotiation_id=neg_id, peer=attendee,
            start=to_iso(slot.start), end=to_iso(slot.end),
            human=f"⏳ invite from {attendee} not on the calendar yet — RSVP deferred; "
                  f"infra will retry each wake cycle.")
        return {"ok": True, "deferred_rsvp": True, "negotiation_id": neg_id,
                "note": "the invite hasn't landed yet; infra will complete this RSVP "
                        "automatically when it arrives — you are DONE, do not retry."}

    def _move(self, conn, current_start, current_end, new_start, new_end, timezone=None,
              title=None, attendee=None):
        held = self._gate(conn, "apply")
        if held:
            return held
        event = self._find(conn, self._slot(conn, current_start, current_end, timezone),
                           title=title, attendee=attendee)
        if event is None:
            return {"ok": False, "error": self._not_found_msg(title, attendee)}
        refused = self._ownership_refusal(conn, event, "move")
        if refused:
            return refused
        new_slot = self._slot(conn, new_start, new_end, timezone)
        conn.update_event(event.id, {"start": new_slot.start, "end": new_slot.end})
        return {"ok": True, "moved": event.title}

    def _cancel(self, conn, current_start, current_end, timezone=None, title=None, attendee=None):
        held = self._gate(conn, "apply")
        if held:
            return held
        event = self._find(conn, self._slot(conn, current_start, current_end, timezone),
                           title=title, attendee=attendee)
        if event is None:
            return {"ok": False, "error": self._not_found_msg(title, attendee)}
        refused = self._ownership_refusal(conn, event, "cancel")
        if refused:
            return refused
        conn.delete_event(event.id)
        return {"ok": True, "deleted": event.title}

    # == focus blocks (Dynamic Focus Blocks) ===================================
    #
    # Time kept on the user's own calendar for their own work: no attendees, nothing
    # sent. The verb is `hold`, so the trust table can gate it like any other write.
    # Changing or releasing one only ever touches an event that carries our focus
    # mark: a block id that now names something else is refused, never rewritten.

    def hold(self, conn, start, end, purpose, show_as="free", timezone=None):
        """Create a focus block '🧠 DFB: <purpose>' over [start, end]."""
        if show_as not in SHOW_AS:
            return {"ok": False, "error": f"show_as must be one of {', '.join(SHOW_AS)}"}
        title = focus_title(purpose)
        held = self._gate(conn, "hold", target=conn.address(), title=title)
        if held:
            return held
        slot = self._slot(conn, start, end, timezone)
        try:
            event = conn.create_hold(title, slot, show_as)
        except NotImplementedError as err:
            return {"ok": False, "error": str(err)}
        self.agent.log.log("hold_created", event_id=event.id, title=title, show_as=show_as,
                           start=to_iso(slot.start), end=to_iso(slot.end),
                           account=conn.address(),
                           human=f"🧠 focus block '{title}' ({show_as}) "
                                 f"{slot.human()}")
        return {"ok": True, "event_id": event.id, "title": title, "show_as": show_as,
                "start": to_iso(slot.start), "end": to_iso(slot.end)}

    def update_hold(self, conn, event_id, current_start, current_end, new_start=None,
                    new_end=None, show_as=None, timezone=None):
        """Move a focus block and/or change how it shows (free → busy hardens it).
        `current_*` is where the block is now: the event is looked up there."""
        if show_as is not None and show_as not in SHOW_AS:
            return {"ok": False, "error": f"show_as must be one of {', '.join(SHOW_AS)}"}
        event, refused = self._own_hold(conn, event_id, current_start, current_end, timezone)
        if refused:
            return refused
        held = self._gate(conn, "hold", target=conn.address(), title=event.title)
        if held:
            return held
        changes = {}
        if new_start is not None and new_end is not None:
            moved = self._slot(conn, new_start, new_end, timezone)
            changes.update(start=moved.start, end=moved.end)
        if show_as is not None:
            changes["show_as"] = show_as
        if not changes:
            return {"ok": True, "event_id": event.id, "unchanged": True}
        conn.update_event(event.id, changes)
        self.agent.log.log("hold_updated", event_id=event.id, title=event.title,
                           account=conn.address(),
                           **{k: to_iso(v) if hasattr(v, "isoformat") else v
                              for k, v in changes.items()},
                           human=f"🧠 focus block '{event.title}' updated: "
                                 + ", ".join(sorted(changes)))
        return {"ok": True, "event_id": event.id, "changed": sorted(changes)}

    def release_hold(self, conn, event_id, current_start, current_end, timezone=None,
                     reason=""):
        """Delete a focus block. Already gone counts as done."""
        event, refused = self._own_hold(conn, event_id, current_start, current_end, timezone)
        if refused and refused.get("gone"):
            return {"ok": True, "event_id": event_id, "already_gone": True}
        if refused:
            return refused
        conn.delete_event(event.id)
        self.agent.log.log("hold_released", event_id=event.id, title=event.title,
                           reason=reason, account=conn.address(),
                           human=f"🧠 focus block '{event.title}' released"
                                 + (f": {reason}" if reason else ""))
        return {"ok": True, "event_id": event.id, "released": event.title}

    def _own_hold(self, conn, event_id, start, end, timezone=None):
        """(event, None) when `event_id` is one of OUR focus blocks around [start,
        end]; (None, refusal) otherwise. The mark is the guard: an id that now names
        something without it is never moved, hardened or deleted."""
        slot = self._slot(conn, start, end, timezone)
        event = next((e for e in conn.list_events(slot) if e.id == event_id), None)
        if event is None:
            return None, {"ok": False, "gone": True,
                          "error": f"no event {event_id!r} around {slot.human()}"}
        if not event.focus:
            self.agent.log.log("hold_refused", event_id=event_id, title=event.title,
                               human=f"⛔ '{event.title}' is not a Parley focus block — "
                                     f"left untouched.")
            return None, {"ok": False, "error": f"'{event.title}' is not a focus block; "
                                                f"Parley only changes its own blocks"}
        return event, None

    def _ownership_refusal(self, conn, event, verb):
        """An attendee that rewrites the organizer's meeting only changes its OWN
        calendar copy — the provider returns ok, no mail goes out, and the two
        calendars silently diverge. So move/cancel are organizer-only; an attendee
        must propose instead. Organizer unknown → allow, fail-open."""
        org = (event.organizer or "").lower()
        if not org or org in self.agent.addresses:
            return None
        self.agent.log.log(
            "ownership_refused", verb=verb, title=event.title, organizer=event.organizer,
            account=conn.address(),
            human=f"⛔ {verb} refused: '{event.title}' is organized by {event.organizer}, "
                  f"not this account — the change must be proposed, not written.")
        return {"ok": False, "error":
                f"you are not the organizer of '{event.title}' — it is organized by "
                f"{event.organizer}. Changing your local copy would silently desync the "
                f"calendars. Send {event.organizer} a plain message proposing the change "
                f"instead."}

    @staticmethod
    def _not_found_msg(title, attendee):
        who = ", ".join(x for x in (f"title={title!r}" if title else "",
                                    f"attendee={attendee!r}" if attendee else "") if x)
        return "no event found in that window" + (f" matching {who}" if who else "")

    # == human gate ============================================================

    def ask_user(self, conn, question, ref=None, requester=None, summary=None, who=None,
                 title=None, start=None, end=None):
        """Escalate to the human for a decision/guidance (free-text reply
        supported). Parks the question; returns its ref id. `summary`, `who`,
        `title`, `start` and `end` are what the tray card shows; the agent fills
        any the model left out from the message and the negotiation."""
        return {"ok": True, "escalated": self.agent.escalate(
            conn, question, negotiation_id=ref, summary=summary, who=who or requester,
            title=title, start=start, end=end)}


def _soft_focus(event):
    """A focus block that still shows as free: time Parley is keeping loosely, which
    a meeting may take — the block moves on the next sweep. A HARDENED block is busy
    like any other event."""
    return bool(getattr(event, "focus", False)) and event.show_as == "free"
