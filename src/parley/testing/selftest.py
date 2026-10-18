"""In-memory selftest — the redesigned plumbing, no credentials, no tokens.

2026-08-31 refactor: this is a CURATED REWRITE, not a full 1:1 port of the
pre-refactor suite (which had 48 checks locking in the old ToolBelt/
NegotiationMemory/Guardrail/wire-protocol shape). Every check below proves one
load-bearing piece of the NEW architecture end to end. What is **not yet
re-ported** — flagged here, not silently dropped, see
docs/iterations/refactor-plan.md "Behavior inventory" for the full list this
was checked against:

  * the pending-invite / counter-invite guard (item #6) — no dedicated test
    existed even before this refactor;
  * the ambiguous-reply escalation path (item #24) — same, already a known gap;
  * multiparty fan-out + the same-meeting supersede exemption (item #10);
  * the reschedule-reopen path (item #18) and its `_reopenable` translation;
  * conflict guard / accept-over-booked / move-targeting / attendee-move-refused
    (items #3, #8, #9);
  * RSVP deferral + fates, stall timer, resumable negotiations, prompt caching,
    the ollama adapter, the human-readable carrier round-trip.

`selftest()` returns non-zero on the first failure (CI-friendly).
"""

import json
import os
import re
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

from parley.agent import Agent, wire
from parley.agent.llm_port import LLMResponse, ToolCall
from parley.agent.short_term_memory import ShortTermMemory
from parley.connectors.accounts.cursor import load_cursor, parse_lookback, save_cursor
from parley.connectors.accounts.test_account import MemoryBus, TestAccount
from parley.core import Message, Slot, email_body, from_iso, to_iso

_START = to_iso(datetime(2026, 7, 4, 10, 0, tzinfo=timezone.utc))
_END = to_iso(datetime(2026, 7, 4, 10, 30, tzinfo=timezone.utc))

_FROM_RE = re.compile(r"^From:\s*(.+)$", re.M)


def selftest():
    return (_selftest_permission() or _selftest_memory() or _selftest_restart_cursor()
            or _selftest_outlook_sleep_gap()
            or _selftest_user_messaging() or _selftest_brain_book()
            or _selftest_brain_human_gate() or _selftest_idempotency()
            or _selftest_rsvp_decline()
            or _selftest_cancellation() or _selftest_reschedule_no_orphan()
            or _selftest_send_rethread()
            or _selftest_tz_infra() or _selftest_booking_dedupe()
            or _selftest_log() or _selftest_settled_invite_rsvp()
            or _selftest_gateway() or _selftest_turn_seam()
            or _selftest_work_queue() or _selftest_session_driver()
            or _selftest_session_routing() or _selftest_negotiate_endpoint()
            or _selftest_reply_threading() or _selftest_group_conversation()
            or _selftest_send_cap()
            or _selftest_human_surface()
            or _selftest_question_cards()
            or _selftest_focus_blocks()
            or _selftest_ask_session()
            or _selftest_skill_install()
            or _selftest_session_supervisor()
            or _selftest_mcp_server()
            or _selftest_claude_code_adapter()
            or _selftest_model_alias()
            or _selftest_setup_engine()
            or _selftest_cfg_edit())


# -- helpers ------------------------------------------------------------------

def _inject(agent, planner):
    from parley.agent.llm_port import FakeLLMClient
    agent.brain.client = FakeLLMClient(planner=planner)


def _event_from(text):
    m = _FROM_RE.search(text)
    return m.group(1).strip() if m else ""


def _acted(transcript):
    return transcript and transcript[-1]["role"] == "tool"


def _extract_ref(text):
    return wire.extract_ref(text)


# == 1. permission (pure) ======================================================

def _selftest_permission():
    from parley.agent.guardrail import ALLOW, ASK, DENY, HANDLE_INBOUND, Action, permission
    print("\n--- running permission: empty table allows; trust table rules both boundaries ---\n")
    assert permission(Action("apply", target="anyone@example.com"), {}) == ALLOW, \
        "FAIL: empty trust table must ALLOW everything"
    structured = {"trust": {
        "boss@example.com": "auto",
        "noisy@example.com": "ignore",
        "new@example.com": "ask",
        "peer@example.com": {"default": "auto", "apply": "ask"}}}
    assert permission(Action(HANDLE_INBOUND, target="noisy@example.com"), structured) == DENY, \
        "FAIL: trust ignore must DENY at intake"
    assert permission(Action(HANDLE_INBOUND, target="new@example.com"), structured) == ASK, \
        "FAIL: trust ask must ASK at intake"
    assert permission(Action("apply", target="peer@example.com"), structured) == ASK, \
        "FAIL: per-verb override must ASK on that verb"
    assert permission(Action("send", target="peer@example.com"), structured) == ALLOW, \
        "FAIL: per-verb dict must fall back to its default"
    from parley.agent.guardrail import trusted
    assert trusted(structured, "boss@example.com"), "FAIL: blanket auto must read as trusted"
    shim = {"accept_requests_from": ["gmail@example.com"]}
    assert permission(Action(HANDLE_INBOUND, target="yahoo@example.com",
                             payload={"type": "meeting_request"}), shim) == ASK, \
        "FAIL: shim — unlisted requester must ASK at intake"
    assert permission(Action(HANDLE_INBOUND, target="gmail@example.com",
                             payload={"type": "meeting_request"}), shim) == ALLOW, \
        "FAIL: shim — listed requester must be ALLOW"
    print("PERMISSION SELFTEST PASSED ✅ — trust table rules intake + verbs; shim intact.")
    return 0


# == 2. ShortTermMemory (pure) =================================================

def _selftest_memory():
    print("\n--- running memory: upsert → summary → persist → reload → close forgets ---\n")
    path = os.path.join(tempfile.mkdtemp(prefix="parley_mem_"), "state", "neg.json")
    m = ShortTermMemory(path)
    m.upsert("n1", direction="outgoing", status="awaiting_peer", slot={"start": _START, "end": _END})
    m.add_party("n1", "a@b.com")
    m.record("n1", "sent request")
    m.upsert("n2", direction="incoming", status="open")
    m.add_party("n2", "c@d.com")
    assert len(m.open()) == 2 and "n1" in m.summary(), "FAIL: open negotiations not tracked"

    reloaded = ShortTermMemory(path)
    assert len(reloaded.open()) == 2, "FAIL: negotiations did not survive reload"
    reloaded.close("n1")
    assert len(ShortTermMemory(path).open()) == 1, "FAIL: closed negotiation not forgotten"

    reloaded.close("ghost123", "booked")
    assert reloaded.is_terminal("ghost123"), "FAIL: close() on an unknown id must tombstone it"

    assert reloaded.find_overlap("a@b.com", _START, _END) is not None, \
        "FAIL: find_overlap should match the booked n1 slot by peer+instants"
    assert reloaded.find_overlap("a@b.com", _END, _END) is None, \
        "FAIL: find_overlap matched a different slot"

    reloaded.set_calendar_uid("n2", "uid-123")
    assert reloaded.find_by_uid("uid-123").id == "n2", \
        "FAIL: find_by_uid should recognize the same real calendar object"
    print("MEMORY SELFTEST PASSED ✅ — persists across reload; closed ones are forgotten; "
         "find_overlap/find_by_uid work.")
    return 0


# == 3. Restart cursor (pure) =================================================

class _DurableInbox:
    def __init__(self, path, log):
        self.path, self.log = path, log
        saved = load_cursor(path)
        self.pos = int(saved["pos"]) if saved and "pos" in saved else len(log)
        if saved is None:
            save_cursor(path, pos=str(self.pos))

    def list_messages(self):
        fresh = self.log[self.pos:]
        self.pos = len(self.log)
        save_cursor(self.path, pos=str(self.pos))
        return fresh


def _selftest_restart_cursor():
    print("\n--- running restart-cursor: durable cursor replays downtime mail ---\n")
    d = tempfile.mkdtemp(prefix="parley_cursor_")
    path = os.path.join(d, "history_test.json")
    assert load_cursor(path) is None, "FAIL: load_cursor of a missing file should be None"
    save_cursor(path, history_id="100", refreshed_at="2026-06-30T00:00:00+00:00")
    assert (load_cursor(path) or {}).get("history_id") == "100", "FAIL: cursor round-trip"
    assert parse_lookback("7d") == timedelta(days=7), "FAIL: parse_lookback('7d')"
    assert parse_lookback("none") is None, "FAIL: parse_lookback('none')"

    state = os.path.join(d, "inbox_pos.json")
    log = []
    a = _DurableInbox(state, log)
    log.append("m1")
    assert a.list_messages() == ["m1"], "FAIL: normal delivery while up"
    log.append("m-during-downtime")
    b = _DurableInbox(state, log)   # restart
    assert b.list_messages() == ["m-during-downtime"], \
        "FAIL: restart dropped the message received while down"
    print("RESTART-CURSOR SELFTEST PASSED ✅ — mail received while down is replayed.")
    return 0


class _FakeOutlookItems:
    """Just enough of Outlook's Items collection for the poll window: Sort is a
    no-op, Restrict parses the same "[ReceivedTime] >= '...'" filter Outlook gets."""
    def __init__(self, mail):
        self.mail = mail

    def Sort(self, *_):
        pass

    def Restrict(self, flt):
        since = datetime.strptime(flt.split("'")[1], "%m/%d/%Y %I:%M %p")
        return [m for m in self.mail if m.ReceivedTime >= since]


class _FakeOutlookMail:
    def __init__(self, eid, received):
        self.EntryID, self.ReceivedTime = eid, received
        self.Subject, self.Body, self.SenderEmailAddress = eid, "", "peer@example.com"


def _selftest_outlook_sleep_gap():
    try:
        from parley.connectors.accounts import local_outlook
    except ImportError:  # no pywin32 off Windows
        print("\n--- skipping outlook-sleep-gap: classic Outlook backend unavailable ---\n")
        return 0
    print("\n--- running outlook-sleep-gap: mail that lands while the PC sleeps ---\n")
    clock = {"now": datetime(2026, 9, 24, 22, 0)}
    real_now = local_outlook._now
    local_outlook._now = lambda: clock["now"]
    try:
        acct = local_outlook.LocalOutlookAccount()
        acct.state_file = os.path.join(tempfile.mkdtemp(prefix="parley_olk_"), "state.json")
        inbox = []
        acct._tls.app = object()  # _bind() sees a bound thread and returns _tls as is
        acct._tls.inbox = type("Inbox", (), {"Items": _FakeOutlookItems(inbox)})()
        acct._since, acct._seen = None, set()

        inbox.append(_FakeOutlookMail("before-sleep", clock["now"]))
        assert [m.id for m in acct.list_messages()] == ["before-sleep"], \
            "FAIL: normal delivery while awake"
        # Sleep 10 h. Mail lands at 07:59 and 08:22; the PC wakes at 08:29.
        inbox.append(_FakeOutlookMail("decline", datetime(2026, 9, 25, 7, 59)))
        inbox.append(_FakeOutlookMail("move-it", datetime(2026, 9, 25, 8, 22)))
        clock["now"] = datetime(2026, 9, 25, 8, 29)
        got = [m.id for m in acct.list_messages()]
        assert got == ["decline", "move-it"], \
            f"FAIL: mail received while asleep must surface once on wake, got {got}"
        clock["now"] += timedelta(seconds=30)
        assert acct.list_messages() == [], "FAIL: the next poll must not replay anything"
    finally:
        local_outlook._now = real_now
    print("OUTLOOK-SLEEP-GAP SELFTEST PASSED ✅ — the first poll after wake reaches back "
          "to the last poll, so nothing that arrived during sleep is skipped.")
    return 0


# == 4. UserMessaging (pure) ====================================================

def _selftest_user_messaging():
    from parley.agent.user_messaging import UserMessaging
    print("\n--- running user-messaging: instruct/ask/answer, idempotent, first-wins ---\n")
    path = os.path.join(tempfile.mkdtemp(prefix="parley_um_"), "um.json")
    um = UserMessaging(path)
    um.instruct("book a meeting with x")
    pq1, reused1 = um.ask("mtg-1", "Accept?")
    pq2, reused2 = um.ask("mtg-1", "Accept? (again)")
    assert not reused1 and reused2, "FAIL: a repeat ask for the same ref must be reused, not duplicated"
    assert pq1.ref == pq2.ref == "mtg-1"
    assert len(um.list_pending()) == 1

    assert um.answer("mtg-1", "yes") is True, "FAIL: first answer should succeed"
    assert um.answer("mtg-1", "no") is False, "FAIL: a second answer to the same ref must be ignored"

    instructions, answered = um.drain()
    assert [i.text for i in instructions] == ["book a meeting with x"]
    assert [a.ref for a in answered] == ["mtg-1"] and answered[0].answer == "yes"
    assert not um.list_pending(), "FAIL: an answered question must not still be pending"

    reloaded = UserMessaging(path)
    assert not reloaded.inbox and not reloaded.pending, \
        "FAIL: drained state should have persisted as empty"

    um.grant("me@example.com", "apply", "peer@example.com")
    assert um.consume_grant("me@example.com", "apply", "peer@example.com"), \
        "FAIL: a granted one-shot approval should be consumable once"
    assert not um.consume_grant("me@example.com", "apply", "peer@example.com"), \
        "FAIL: a consumed grant must not be consumable twice"
    print("USER-MESSAGING SELFTEST PASSED ✅ — idempotent ask, first-answer-wins, one-shot grants.")
    return 0


# == 5. Brain book (fake model, two agents over the bus) ======================

def _make_agent(conn, name, base_policy=None, learned_path=None):
    return Agent([(conn, {})], name=name, base_policy=base_policy, learned_path=learned_path)


def _selftest_brain_book():
    """Direct booking = ONE email (the invite); the responder RSVPs. Exercises
    the redesigned AccountsGate.apply(create) end to end, with the new
    connector primitive names (`list_messages`/`respond_event`/...)."""
    print("\n--- running brain-book: direct booking = ONE email (the invite); outlook RSVPs ---\n")
    bus = MemoryBus()
    gmail_conn = TestAccount("gmail@example.com", bus)
    outlook_conn = TestAccount("outlook@example.com", bus)
    gmail = _make_agent(gmail_conn, "gmail")
    outlook = _make_agent(outlook_conn, "outlook")

    nid = "book0001"

    def gmail_planner(system, transcript, tools):
        if _acted(transcript):
            return LLMResponse(text="Done.")
        return LLMResponse(tool_calls=[
            ToolCall("g1", "apply", {"change": "create", "attendee": "outlook@example.com",
                                     "start": _START, "end": _END, "title": "Kickoff",
                                     "ref": nid})])

    def outlook_planner(system, transcript, tools):
        if _acted(transcript):
            return LLMResponse(text="RSVP'd.")
        text = transcript[0]["content"]
        return LLMResponse(tool_calls=[
            ToolCall("o", "apply", {"change": "rsvp", "attendee": _event_from(text),
                                    "start": _START, "end": _END, "title": "Kickoff"})])

    _inject(gmail, gmail_planner)
    _inject(outlook, outlook_planner)

    gmail.instruct("set a meeting with outlook@example.com tomorrow at 10:00 about Kickoff")
    assert sum(e.title == "Kickoff" for e in gmail_conn.calendar) == 1, \
        "FAIL: the asker (gmail) should have created exactly one event"
    assert gmail.short_term_memory.is_terminal(nid), "FAIL: the asker's booking must settle its negotiation"
    assert not bus.boxes["outlook@example.com"], \
        "FAIL: a direct booking must send no extra mail — the invite is the message"

    bus.deliver("outlook@example.com",
               Message(uuid.uuid4().hex, "gmail@example.com", "Invitation: Kickoff", "",
                       invite={"title": "Kickoff", "start": _START, "end": _END,
                              "organizer": "gmail@example.com"}))
    for message in outlook_conn.list_messages():
        outlook.handle(message, outlook_conn)
    assert sum(e.title == "Kickoff" for e in outlook_conn.calendar) == 1, \
        "FAIL: outlook should have RSVP'd exactly one seat on the asker's invite"
    print("BRAIN-BOOK SELFTEST PASSED ✅ — one email (the invite); responder RSVPs; no shadow mail.")
    return 0


# == 6. Brain human-gate (ask_user → decide → book) ===========================

def _selftest_brain_human_gate():
    print("\n--- running brain-human-gate: ask_user → pending → decide → book ---\n")
    bus = MemoryBus()
    conn = TestAccount("me@example.com", bus)
    from parley.agent.policy import Policy
    agent = _make_agent(conn, "me",
                        base_policy=Policy(unstructured="Ask me before accepting a brand-new request."))

    def planner(system, transcript, tools):
        if _acted(transcript):
            return LLMResponse(text="Done.")
        text = transcript[0]["content"]
        if "Their answer" in text:
            m = re.search(r"negotiation (\S+)", text)
            ref = m.group(1).rstrip(".)") if m else None
            return LLMResponse(tool_calls=[
                ToolCall("a1", "apply", {"change": "rsvp", "attendee": "stranger@example.com",
                                         "start": _START, "end": _END, "title": "Budget review",
                                         "ref": ref})])
        ref = _extract_ref(text)
        return LLMResponse(tool_calls=[ToolCall("q", "ask_user", {
            "question": "Accept 'Budget review' from stranger@example.com?", "ref": ref})])

    _inject(agent, planner)

    subject = "Budget review (ref: mtg-hg001)"
    body = "Can we meet? (ref: mtg-hg001)"
    bus.deliver(conn.address(), Message("seed", "stranger@example.com", subject, body,
                                        invite=None))
    # A real invite carries no visible ref of our own making, so seed this as a
    # plain wire-style message instead (a peer's own first contact, ref self-assigned).
    for m in conn.list_messages():
        agent.handle(m, conn)

    pend = agent.pending_decisions()
    assert pend, f"FAIL: not escalated — {pend}"
    ref = pend[0]["negotiation_id"]
    assert not any(e.title == "Budget review" for e in conn.calendar), \
        "FAIL: escalated request must NOT be booked before the human answers"

    agent.decide(ref, "accept")
    assert any(e.title == "Budget review" for e in conn.calendar), \
        "FAIL: not booked after the human accepted"
    assert not agent.pending_decisions(), "FAIL: escalation not cleared after decide()"
    print("BRAIN-HUMAN-GATE SELFTEST PASSED ✅ — ask_user → decide(free-text) → booked.")
    return 0


# == 7. Idempotency (a resent invite must NOT double-book) =====================

def _selftest_idempotency():
    print("\n--- running idempotency: terminal id never re-writes; resend dropped ---\n")
    bus = MemoryBus()
    conn = TestAccount("me@example.com", bus)
    agent = _make_agent(conn, "me")
    calls = {"n": 0}
    _inject(agent, lambda system, transcript, tools:
            (calls.__setitem__("n", calls["n"] + 1), LLMResponse(text="Noted."))[1])

    r1 = agent.accounts_gate.apply(conn, "create", attendee="peer@example.com",
                                   start=_START, end=_END, title="Sync", ref="mtg-dup1")
    assert r1["ok"] and agent.short_term_memory.is_terminal("mtg-dup1")
    r2 = agent.accounts_gate.apply(conn, "create", attendee="peer@example.com",
                                   start=_START, end=_END, title="Sync", ref="mtg-dup1")
    assert r2.get("already_booked"), "FAIL: a terminal ref must never re-write"
    assert calls["n"] == 0, "FAIL: this must be a zero-model-turn check, not a brain decision"
    assert sum(e.title == "Sync" for e in conn.calendar) == 1, "FAIL: duplicate write happened"
    print("IDEMPOTENCY SELFTEST PASSED ✅ — terminal id guard holds; no duplicate write.")
    return 0


# == 7b. Declining a received invite is an RSVP, not an email ==================

def _selftest_rsvp_decline():
    """Regression for scenario A.2 (2026-09-06): the brain was told it could
    "decline" a real invite, but no decline could reach the calendar — every
    respond_event call was hardcoded "accepted". It emailed a counter-proposal,
    reported "I declined", and left the user double-booked with the invite still
    unanswered. apply(change=rsvp, response=declined) is that missing path."""
    print("\n--- running rsvp-decline: declining an invite RSVPs it, never books ---\n")
    bus = MemoryBus()
    conn = TestAccount("me@example.com", bus)
    agent = _make_agent(conn, "me")
    calls = {"n": 0}
    _inject(agent, lambda system, transcript, tools:
            (calls.__setitem__("n", calls["n"] + 1), LLMResponse(text="Noted."))[1])

    # The peer's invite is already on our calendar, holding the slot.
    conn.create_event("Their meeting", Slot(from_iso(_START), from_iso(_END)), "peer@example.com")
    before = len(conn.calendar)

    res = agent.accounts_gate.apply(conn, "rsvp", response="declined",
                                    attendee="peer@example.com", start=_START, end=_END,
                                    title="Their meeting", ref="mtg-i0decline")
    assert res["ok"] and res.get("declined"), f"FAIL: decline did not go through: {res}"
    assert conn.accepted and conn.accepted[-1]["response"] == "declined", \
        f"FAIL: the backend was not asked to DECLINE: {conn.accepted}"
    assert not agent.short_term_memory.is_terminal("mtg-i0decline"), \
        "FAIL: declining must not settle the negotiation as booked"
    assert len(conn.calendar) == before, "FAIL: a decline must not write a new event"
    assert calls["n"] == 0, "FAIL: this is infra, not a brain decision"

    # And the default is still accept — a plain rsvp must not start declining.
    res2 = agent.accounts_gate.apply(conn, "rsvp", attendee="peer@example.com",
                                     start=_START, end=_END, title="Their meeting",
                                     ref="mtg-i0accept")
    assert res2["ok"] and conn.accepted[-1]["response"] == "accepted", \
        f"FAIL: rsvp without response must still accept: {conn.accepted}"
    print("RSVP-DECLINE SELFTEST PASSED ✅ — decline reaches the backend, books nothing, "
          "and accept is still the default.")
    return 0


# == 7e. A follow-up send threads, it does not fork a second negotiation =======

def _selftest_send_rethread():
    """Regression for the 2026-09-06 duplicate-ref: a `send` without `ref` minted a
    new negotiation unconditionally, so gmail's "Reschedule confirmed: 93-Pepito"
    follow-up opened a SECOND record for a meeting already on file — one meeting,
    two entries in working memory, two questions for the human."""
    print("\n--- running send-rethread: a follow-up joins its meeting, not a new one ---\n")
    bus = MemoryBus()
    conn = TestAccount("me@example.com", bus)
    agent = _make_agent(conn, "me")
    mem = agent.short_term_memory

    first = agent.accounts_gate.send(conn, "peer@example.com", "Shall we meet?",
                                     subject="93-Pepito", topic="93-Pepito")
    assert first["ok"], f"FAIL: first send failed: {first}"
    ref = first["negotiation_id"]

    # The exact shape that forked it: same peer, topic elaborated, ref forgotten.
    follow = agent.accounts_gate.send(conn, "peer@example.com", "Done, moved to 16:00.",
                                      subject="Reschedule confirmed: 93-Pepito",
                                      topic="93-Pepito reschedule")
    assert follow["ok"], f"FAIL: follow-up send failed: {follow}"
    assert follow["negotiation_id"] == ref, \
        f"FAIL: follow-up forked {follow['negotiation_id']} instead of threading onto {ref}"
    assert len(mem.open()) == 1, f"FAIL: one meeting must be ONE negotiation, got {len(mem.open())}"

    # MULTIPARTY FAN-OUT: same topic, DIFFERENT peer, same turn — one meeting, one ref.
    # (scenario E, 2026-09-06: both parallel sends had ref=None and each minted its own.)
    trio_a = agent.accounts_gate.send(conn, "alice@example.com", "When are you free?",
                                      subject="97-Trio", topic="97-Trio")
    trio_b = agent.accounts_gate.send(conn, "bob@example.com", "When are you free?",
                                      subject="97-Trio", topic="97-Trio")
    assert trio_a["negotiation_id"] == trio_b["negotiation_id"],         f"FAIL: fan-out forked into {trio_a['negotiation_id']} and {trio_b['negotiation_id']}"
    parties = {p["addr"] for p in mem.get(trio_a["negotiation_id"]).parties}
    assert {"alice@example.com", "bob@example.com"} <= parties,         f"FAIL: both parties must join the one negotiation, got {parties}"

    # A genuinely different meeting with the same peer must still open its own.
    other = agent.accounts_gate.send(conn, "peer@example.com", "Different thing entirely.",
                                     subject="Budget", topic="Budget review")
    assert other["negotiation_id"] != ref, "FAIL: an unrelated topic must open its own negotiation"
    assert len(mem.open()) == 3, "FAIL: expected 3 negotiations (Pepito, trio, Budget)"

    # And the outgoing body carries the ref plus the keep-this-line note.
    delivered = bus.boxes["peer@example.com"]
    assert delivered, "FAIL: nothing was delivered to the peer"
    body = delivered[-1].body or ""
    assert wire.extract_ref(body) == other["negotiation_id"], \
        "FAIL: the outgoing body must carry its own visible ref"
    assert "keep this line" in body.lower(), "FAIL: the ref must explain why not to delete it"
    print("SEND-RETHREAD SELFTEST PASSED ✅ — follow-ups thread, new topics fork, "
          "ref + note go out.")
    return 0


# == 7d. Rescheduling moves the event, it does not clone it ====================

def _selftest_reschedule_no_orphan():
    """Regression for scenario A.2 (2026-09-06): after a peer declined, the reopened
    negotiation booked the new slot with apply(create), so the ORIGINAL event stayed
    on the calendar — yahoo ended holding two '92-Juanito', one at 15:00 nobody was
    coming to and one at 11:00."""
    print("\n--- running reschedule: a reopened negotiation MOVES its event ---\n")
    bus = MemoryBus()
    conn = TestAccount("me@example.com", bus)
    agent = _make_agent(conn, "me")
    mem = agent.short_term_memory

    r = agent.accounts_gate.apply(conn, "create", attendee="peer@example.com",
                                  start=_START, end=_END, title="Sync", ref="mtg-resched")
    assert r["ok"] and len(conn.calendar) == 1, "FAIL: setup booking did not land"

    later_s = to_iso(from_iso(_START) + timedelta(hours=3))
    later_e = to_iso(from_iso(_END) + timedelta(hours=3))
    n = mem.reopen("mtg-resched", "the peer declined")
    r2 = agent.accounts_gate.apply(conn, "create", attendee="peer@example.com",
                                   start=later_s, end=later_e, title="Sync", ref=n.id)
    assert r2["ok"], f"FAIL: rebooking the reopened negotiation failed: {r2}"
    syncs = [e for e in conn.calendar if e.title == "Sync"]
    assert len(syncs) == 1, \
        f"FAIL: reschedule left an orphan — {len(syncs)} 'Sync' events on the calendar"
    assert syncs[0].start == from_iso(later_s), \
        f"FAIL: the surviving event is at {syncs[0].start}, expected {later_s}"

    # A genuinely NEW meeting at another time must still create, not move this one.
    other_s = to_iso(from_iso(_START) + timedelta(days=1))
    other_e = to_iso(from_iso(_END) + timedelta(days=1))
    agent.accounts_gate.apply(conn, "create", attendee="peer@example.com",
                              start=other_s, end=other_e, title="Other", ref="mtg-other")
    assert len([e for e in conn.calendar if e.title == "Other"]) == 1 and len(conn.calendar) == 2, \
        "FAIL: an unrelated new booking must still create its own event"
    print("RESCHEDULE SELFTEST PASSED ✅ — the event moves, no orphan; unrelated "
          "bookings still create.")
    return 0


# == 7c. A cancellation is infra's business, never a toast =====================

def _selftest_cancellation():
    """Regression for the 2026-09-06 toast-spam: a cancellation notice matched no
    infra branch, reached the brain as plain mail, cost a model turn to conclude
    "nothing to do" — and the no-silent-inaction backstop then escalated it, so the
    user was toasted the raw cancellation email every time a meeting was cancelled."""
    print("\n--- running cancellation: organizer cancels → closed silently, no ask ---\n")
    bus = MemoryBus()
    conn = TestAccount("me@example.com", bus)
    agent = _make_agent(conn, "me")
    calls = {"n": 0}
    _inject(agent, lambda system, transcript, tools:
            (calls.__setitem__("n", calls["n"] + 1), LLMResponse(text="hm"))[1])

    # Subject forms both providers use.
    for subject in ("Canceled event: Budget review @ Mon Sep 7, 2026 3pm (CEST)",
                    "Cancelled: Budget review"):
        p = wire.parse(Message("c", "peer@example.com", subject, ""))
        assert p.get("type") == "cancellation", f"FAIL: {subject!r} not read as a cancellation"
        assert p.get("title") == "Budget review", f"FAIL: title not extracted from {subject!r}"

    agent.short_term_memory.upsert("mtg-cancel1", topic="Budget review", direction="incoming",
                                   status="booked", account=conn.address())
    agent.short_term_memory.add_party("mtg-cancel1", "peer@example.com")
    agent.handle(Message("c1", "peer@example.com", "Canceled event: Budget review", ""), conn)
    assert agent.short_term_memory.is_terminal("mtg-cancel1"), \
        "FAIL: a cancellation must close the negotiation it matches"
    assert not agent.user_messaging.list_pending(), \
        "FAIL: a cancellation must never park a question for the human"
    assert calls["n"] == 0, "FAIL: a cancellation has no decision in it — zero model turns"

    # An unknown cancellation is dropped, not escalated.
    agent.handle(Message("c2", "stranger@example.com", "Canceled event: Some other thing", ""),
                 conn)
    assert not agent.user_messaging.list_pending(), \
        "FAIL: an untracked cancellation must be dropped, not escalated"
    assert calls["n"] == 0, "FAIL: still no model turn"

    # A real invite must NOT be mistaken for a cancellation.
    p = wire.parse(Message("i", "peer@example.com", "Invitation: Budget review", "",
                           invite={"title": "Budget review", "start": _START, "end": _END,
                                   "organizer": "peer@example.com"}))
    assert p.get("type") == "meeting_request", "FAIL: a real invite must stay a meeting_request"
    print("CANCELLATION SELFTEST PASSED ✅ — closed silently, unknown dropped, "
          "zero model turns, invites unaffected.")
    return 0


# == 8. Timezone is infra's ====================================================

def _selftest_tz_infra():
    print("\n--- running tz-infra: naive local times land at the right UTC instant ---\n")
    bus = MemoryBus()
    conn = TestAccount("me@example.com", bus, tz="Europe/Madrid")   # July → UTC+2
    agent = _make_agent(conn, "me")

    def planner(system, transcript, tools):
        if _acted(transcript):
            return LLMResponse(text="Done.")
        return LLMResponse(tool_calls=[ToolCall("t", "apply", {
            "change": "create", "attendee": "peer@example.com", "start": "2026-07-06T09:15",
            "end": "2026-07-06T09:30", "title": "Banana"})])

    _inject(agent, planner)
    agent.instruct("set a meeting with peer@example.com monday at 9:15 about Banana")
    ev = next(e for e in conn.calendar if e.title == "Banana")
    assert to_iso(ev.start.astimezone(timezone.utc)) == "2026-07-06T07:15:00+00:00", \
        f"FAIL: 09:15 Europe/Madrid should book at 07:15 UTC, got {to_iso(ev.start)}"

    r2 = agent.accounts_gate.apply(conn, "create", attendee="peer@example.com",
                                   start="2026-07-07T09:15", end="2026-07-07T09:30",
                                   title="NY call", timezone="America/New_York")
    ev2 = next(e for e in conn.calendar if e.title == "NY call")
    assert to_iso(ev2.start.astimezone(timezone.utc)) == "2026-07-07T13:15:00+00:00", \
        f"FAIL: 09:15 America/New_York should book at 13:15 UTC, got {to_iso(ev2.start)}"

    try:
        agent.accounts_gate.check(TestAccount("z@example.com", bus, tz="Not/AZone"),
                                  "2026-07-06T09:15", "2026-07-06T09:30")
        raise AssertionError("FAIL: an unresolvable timezone must raise, not guess UTC")
    except ValueError as err:
        assert "Not/AZone" in str(err), f"FAIL: tz error should name the zone: {err}"

    from parley.connectors.accounts.ics import ics_when
    w = ics_when("DTSTART;TZID=Romance Standard Time:20260705T140000", "DTSTART")
    assert to_iso(w) == "2026-07-05T12:00:00+00:00", \
        f"FAIL: Windows TZID should map to Europe/Madrid, got {to_iso(w)}"
    w = ics_when("DTSTART;TZID=India Standard Time:20260705T170000", "DTSTART")
    assert to_iso(w) == "2026-07-05T11:30:00+00:00", \
        f"FAIL: Windows TZID should map to Asia/Kolkata (+5:30), got {to_iso(w)}"
    print("TZ-INFRA SELFTEST PASSED ✅ — local→calendar-tz→UTC in infra; explicit tz wins.")
    return 0


# == 9. Booking dedupe (calendar-truth backstop) ===============================

def _selftest_booking_dedupe():
    print("\n--- running booking-dedupe: one event per peer+slot; resent invite dropped ---\n")
    bus = MemoryBus()
    conn = TestAccount("me@example.com", bus)
    agent = _make_agent(conn, "me")

    r1 = agent.accounts_gate.apply(conn, "create", attendee="peer@example.com",
                                   start=_START, end=_END, title="Sync")
    r2 = agent.accounts_gate.apply(conn, "create", attendee="peer@example.com",
                                   start=_START, end=_END, title="Sync")
    assert r1["ok"] and r2["ok"] and r2.get("already_booked"), \
        "FAIL: second create should report already_booked (calendar-truth backstop)"
    assert sum(e.title == "Sync" for e in conn.calendar) == 1, \
        "FAIL: duplicate create wrote a second event"
    print("BOOKING-DEDUPE SELFTEST PASSED ✅ — one event per peer+slot (calendar-truth check).")
    return 0


# == 10. Log — every message + decision leaves a record ========================

def _selftest_log():
    print("\n--- running log: every message + decision leaves a record ---\n")
    bus = MemoryBus()
    conn = TestAccount("me@example.com", bus)
    agent = _make_agent(conn, "me")

    def planner(system, transcript, tools):
        if _acted(transcript):
            return LLMResponse(text="Done.")
        text = transcript[0]["content"]
        m = re.search(r"negotiation (\S+) ", text)
        ref = m.group(1) if m else None
        return LLMResponse(tool_calls=[ToolCall("c", "apply", {
            "change": "rsvp", "attendee": "boss@example.com", "start": _START, "end": _END,
            "title": "Board sync", "ref": ref})])

    _inject(agent, planner)
    m = Message(uuid.uuid4().hex, "boss@example.com", "Invitation: Board sync",
               "You are invited.", invite={"title": "Board sync", "start": _START,
                                           "end": _END, "organizer": "boss@example.com"})
    bus.deliver(conn.address(), m)
    for msg in conn.list_messages():
        agent.handle(msg, conn)
    kinds = [r["kind"] for r in agent.log.records]
    for expected in ("message_received", "payload", "brain_turn", "tool_call",
                     "tool_result", "settle", "brain_done"):
        assert expected in kinds, f"FAIL: log is missing a '{expected}' record: {kinds}"

    with tempfile.TemporaryDirectory() as tmp:
        from parley.agent.log import Log
        j = Log(tmp, "disk")
        j.log("message_received", sender="a@b", body="hi")
        j.log("drop", reason="gateway_ignore")
        files = [f for f in os.listdir(tmp) if f.startswith("journal-") and f.endswith(".jsonl")]
        assert len(files) == 1, f"FAIL: expected one daily log file, got {files}"
        import json
        with open(os.path.join(tmp, files[0]), encoding="utf-8") as f:
            lines = [json.loads(line) for line in f if line.strip()]
        assert [r["kind"] for r in lines] == ["message_received", "drop"], \
            f"FAIL: log file content: {lines}"
        assert all("ts" in r and r["agent"] == "disk" for r in lines), \
            "FAIL: log lines must carry ts + agent"
    print("LOG SELFTEST PASSED ✅ — full trail in memory; valid JSONL on disk.")
    return 0


# == 11. Settled-invite RSVP (widened match: booked OR awaiting_peer) ==========

def _selftest_settled_invite_rsvp():
    """The widened item #19/#21 match: a real invite arriving for a negotiation
    that's already `booked` OR still `awaiting_peer` gets auto-RSVP'd with zero
    model turns — `request_confirmed` no longer needs to exist as its own
    message type for this to work."""
    print("\n--- running settled-invite-rsvp: widened match auto-RSVPs; resend dropped ---\n")
    bus = MemoryBus()
    conn = TestAccount("me@example.com", bus)
    agent = _make_agent(conn, "me")
    calls = {"n": 0}
    _inject(agent, lambda system, transcript, tools:
            (calls.__setitem__("n", calls["n"] + 1), LLMResponse(text="Noted."))[1])

    nid = "mtg-conf01"
    agent.short_term_memory.upsert(nid, direction="outgoing", status="awaiting_peer",
                                   slot={"start": _START, "end": _END})
    agent.short_term_memory.add_party(nid, "peer@example.com")

    inv = {"title": "Kickoff", "start": _START, "end": _END, "organizer": "peer@example.com",
          "uid": "ical-uid-1"}

    def deliver_invite():
        bus.deliver(conn.address(), Message(uuid.uuid4().hex, "peer@example.com",
                                            "Invitation: Kickoff", "", invite=dict(inv)))
        for m in conn.list_messages():
            agent.handle(m, conn)

    deliver_invite()
    assert calls["n"] == 0, "FAIL: a matching invite must settle in infra, zero model turns"
    assert agent.short_term_memory.is_terminal(nid), "FAIL: the negotiation must settle as booked"
    assert sum(e.title == "Kickoff" for e in conn.calendar) == 1, \
        "FAIL: infra should have RSVP'd the invite exactly once"

    deliver_invite()  # resend, same uid → dropped
    assert calls["n"] == 0 and sum(e.title == "Kickoff" for e in conn.calendar) == 1, \
        "FAIL: a resent invite re-ran the brain or double-booked"
    print("SETTLED-INVITE-RSVP SELFTEST PASSED ✅ — widened match auto-RSVPs; resend dropped; "
         "zero model turns; request_confirmed not needed.")
    return 0


# == 12. Gateway (pure function) ===============================================

def _selftest_turn_seam():
    """The prepare/run split (phase 0 of the blocked-session design) actually holds.

    This suite exists to STOP the seam closing again. A background Claude Code session
    replaces `Brain.run()` and nothing else, so preparing a turn, asking for its
    context and executing one tool call must each work with no LLM anywhere near them.
    If someone folds these back into the loop, this fails.
    """
    print("\n--- running turn-seam: prepare, context and dispatch work without any LLM ---\n")
    from parley.agent.brain import TOOLS, Turn
    from parley.agent.llm_port import ToolCall
    bus = MemoryBus()
    conn = TestAccount("gmail@example.com", bus)
    agent = _make_agent(conn, "gmail")
    agent.brain.client = None  # the point: no model, and yet all three halves work

    turn = agent.brain.turn_for_instruction("book me something with ana", conn)
    assert isinstance(turn, Turn) and turn.kind == "instruction", "FAIL: not a prepared Turn"
    assert "book me something with ana" in turn.event, \
        "FAIL: the user's words must reach the event text"
    assert turn.message is None, "FAIL: an instruction has no inbound message"

    ctx = agent.brain.context(turn)
    assert ctx["tools"] is TOOLS and ctx["system"], \
        "FAIL: context must serve the system prompt and the tool schemas"
    assert "gmail@example.com" in ctx["system"], \
        "FAIL: the system prompt must be built for THIS turn's account"

    # dispatch reaches the real, gated AccountsGate — no model in the path
    result = agent.brain.dispatch(conn, ToolCall("t1", "check", {"start": _START, "end": _END}))
    assert result.get("ok") and "busy" in result, \
        f"FAIL: dispatch must execute the real tool, got {result}"
    unknown = agent.brain.dispatch(conn, ToolCall("t2", "not_a_tool", {}))
    assert not unknown.get("ok"), "FAIL: an unknown tool must be refused, not executed"

    # Regression, found live 2026-09-19 by a session driving a real turn: dispatch
    # journals an emoji line, and on a console that cannot encode it the raise was
    # caught by the tool's own except and reported to the model as the TOOL failing.
    # Every Parley verb came back "UnicodeEncodeError" and the session concluded,
    # reasonably, that Parley was broken. Observation must not change what it observes.
    import io as _io
    import sys as _sys
    narrow = _io.TextIOWrapper(_io.BytesIO(), encoding="cp1252", errors="strict")
    real_stdout, _sys.stdout = _sys.stdout, narrow
    try:
        on_narrow = agent.brain.dispatch(
            conn, ToolCall("t3", "check", {"start": _START, "end": _END}))
    finally:
        _sys.stdout = real_stdout
    assert on_narrow.get("ok"), \
        f"FAIL: a console that cannot print an emoji must not fail the tool: {on_narrow}"

    # an inbound turn correlates ONCE, at preparation time, and carries its message
    msg = Message("m1", "peer@example.com", "Re: Kickoff", "sounds good (ref: mtg-seam001)")
    inbound = agent.brain.turn_for_message(msg, conn)
    assert inbound.kind == "inbound" and inbound.message is msg, \
        "FAIL: an inbound turn must carry the message the agent exposes mid-turn"

    # and with no client, running is still the loud refusal it always was
    refused = agent.brain.run(turn)
    assert not refused["ok"] and "no LLM client" in refused["message"], \
        "FAIL: no client must fail loudly, never silently do nothing"
    assert agent.current_turn_message is None, \
        "FAIL: the turn's message must be cleared even when the run refuses"

    # The system prompt is frozen FOR THE TURN — it carries `now`, so it is not
    # reproducible across calls, but every step of one run must see the same one
    # (iteration 18: rebuilding it per step let the model's own mid-turn writes
    # reappear to it as pre-existing state).
    _inject(agent, lambda system, transcript, tools: (
        LLMResponse(tool_calls=[ToolCall("s1", "check", {"start": _START, "end": _END})])
        if not _acted(transcript) else LLMResponse(text="done")))
    agent.brain.run(agent.brain.turn_for_instruction("check my tuesday", conn))
    seen = [call[0] for call in agent.brain.client.calls]
    assert len(seen) == 2, f"FAIL: expected a two-step run, got {len(seen)}"
    assert seen[0] == seen[1], "FAIL: the system prompt must be frozen across a turn's steps"
    print("TURN-SEAM SELFTEST PASSED ✅ — prepare, context and dispatch stand alone; "
          "run() is just one driver.")


def _selftest_work_queue():
    """The durable hand-off: at-least-once, poison-bounded, and it survives a restart."""
    print("\n--- running work-queue: leases, redelivery, poison parking, durability ---\n")
    from parley.agent.work_queue import WorkQueue
    path = os.path.join(tempfile.mkdtemp(prefix="parley_q_"), "state", "queue_brain.json")

    q = WorkQueue(path=path, lease_seconds=0.2, max_attempts=2)
    assert q.lease(timeout=0.05) is None, "FAIL: an empty queue must time out, not block forever"
    q.push("inbound", {"event": "one"})
    q.push("inbound", {"event": "two"})
    assert q.stats() == {"pending": 2, "leased": 0, "parked": 0}, f"FAIL: {q.stats()}"

    first = q.lease(timeout=1)
    assert first["payload"]["event"] == "one", "FAIL: the queue must be FIFO"
    second = q.lease(timeout=1)
    assert second["payload"]["event"] == "two", \
        "FAIL: a leased item must not block the one behind it"
    assert q.stats()["leased"] == 2, "FAIL: both should be leased"

    # at-least-once: a session that dies gets its turn back
    time.sleep(0.25)
    again = q.lease(timeout=1)
    assert again["id"] == first["id"] and again["attempts"] == 2, \
        "FAIL: an expired lease must be redelivered, not lost"

    assert q.ack(second["id"]), "FAIL: ack must remove the item"
    assert not q.ack(second["id"]), "FAIL: acking twice must be a harmless no-op"

    # poison pill: attempts are bounded, or one bad event starves the queue forever
    time.sleep(0.25)
    assert q.lease(timeout=0.05) is None, "FAIL: a spent item must not be handed out again"
    assert q.stats()["parked"] == 1 and q.parked()[0]["id"] == first["id"], \
        f"FAIL: the poison pill must be parked, got {q.stats()}"

    # durability: a restart keeps the backlog and invalidates every lease
    fresh = WorkQueue(path=path, lease_seconds=60)
    fresh.push("inbound", {"event": "three"})
    reopened = WorkQueue(path=path)
    assert reopened.stats()["pending"] == 1, \
        f"FAIL: the backlog must survive a restart, got {reopened.stats()}"
    assert reopened.lease(timeout=1)["payload"]["event"] == "three", \
        "FAIL: a restart must invalidate leases so nothing is stranded"

    # the wait really blocks, and a push really wakes it
    slow = WorkQueue()
    woke = []
    waiter = threading.Thread(target=lambda: woke.append(slow.lease(timeout=5)))
    waiter.start()
    time.sleep(0.1)
    slow.push("inbound", {"event": "late"})
    waiter.join(5)
    assert woke and woke[0] and woke[0]["payload"]["event"] == "late", \
        "FAIL: a push must wake a blocked waiter"
    print("WORK-QUEUE SELFTEST PASSED ✅ — FIFO, redelivery, poison parking, durable, "
          "wakes on push.")


def _selftest_session_driver():
    """A turn survives the queue and a session can drive it end to end — the same
    guarded tools as the in-process loop, no LLM anywhere in the path."""
    print("\n--- running session-driver: turn round-trip, wait → tool → done ---\n")
    from parley.client_server.server import ParleyServer
    bus = MemoryBus()
    conn = TestAccount("gmail@example.com", bus)
    agent = _make_agent(conn, "gmail")
    agent.brain.client = None  # the point: the session is the driver, not a model

    # round-trip a prepared turn through plain JSON, message and all
    msg = Message("m9", "peer@example.com", "Kickoff?", "can we meet (ref: mtg-drv001)")
    original = agent.brain.turn_for_message(msg, conn)
    payload = agent.brain.turn_to_payload(original)
    assert json.loads(json.dumps(payload)), "FAIL: a queued turn must be plain JSON"
    rebuilt = agent.brain.turn_from_payload(payload)
    assert rebuilt.conn is conn and rebuilt.event == original.event, \
        "FAIL: the rebuilt turn must name the same account and event"
    assert rebuilt.message.sender == "peer@example.com", "FAIL: the message must round-trip"

    # an account we do not have is an error, NOT a silent fallback to the first one
    try:
        agent.brain.turn_from_payload(dict(payload, account="stranger@example.com"))
    except LookupError:
        pass
    else:
        raise AssertionError("FAIL: an unknown account must raise, never run as someone else")

    server = ParleyServer(agent)
    agent.work_queue("brain").push("inbound", payload)

    turn = server.work_wait({"role": "brain", "timeout": 2})
    assert turn.get("turn_id") and "Kickoff?" in turn["event"], f"FAIL: no turn served: {turn}"
    assert turn["system"] and "gmail@example.com" in turn["system"], \
        "FAIL: the turn must arrive with its frozen system prompt"

    result = server.work_tool({"role": "brain", "turn_id": turn["turn_id"],
                               "name": "check", "input": {"start": _START, "end": _END}})
    assert result.get("ok") and "busy" in result, f"FAIL: the tool must really run: {result}"

    stale = server.work_tool({"role": "brain", "turn_id": "w-nope", "name": "check",
                              "input": {"start": _START, "end": _END}})
    assert not stale.get("ok"), "FAIL: a tool call for a turn we no longer hold must be refused"

    # finishing IS asking for the next one, and it must carry a conclusion
    assert agent.work_queue("brain").stats()["leased"] == 1, "FAIL: the turn should be held"
    nothing = server.work_wait({"role": "brain", "timeout": 0.2,
                                "done_because": "asked the peer for times"})
    assert nothing.get("empty"), "FAIL: with nothing queued the wait must report empty"
    assert agent.work_queue("brain").stats() == {"pending": 0, "leased": 0, "parked": 0}, \
        "FAIL: asking for the next turn must acknowledge the one just finished"

    orphan = server.work_tool({"role": "brain", "name": "check",
                               "input": {"start": _START, "end": _END}})
    assert not orphan.get("ok"), "FAIL: a tool call with no held turn must be refused"
    print("SESSION-DRIVER SELFTEST PASSED ✅ — turn round-trips; wait serves context; "
          "tools run gated; finishing acks.")


def _selftest_session_routing():
    """driver: session routes admitted mail to the queue — and keeps every guard.

    The switch must change WHO reasons, nothing else: the deterministic intake still
    runs here, and the no-silent-inaction backstop still fires, just at the other end
    of the turn. A guard that quietly disappears when you change driver is worse than
    no guard, because the cfg says nothing about it.
    """
    print("\n--- running session-routing: admitted mail is queued, guards survive ---\n")
    from types import SimpleNamespace
    from parley.client_server.server import ParleyServer
    bus = MemoryBus()
    conn = TestAccount("gmail@example.com", bus)
    policy = {"structured": {"trust": {"peer@example.com": "auto"}}, "unstructured": ""}
    agent = Agent([(conn, policy)], name="sess", llm=SimpleNamespace(driver="session"))
    assert agent.session_driven, "FAIL: llm: driver: session must switch the driver"

    def _explode(*a, **k):
        raise AssertionError("FAIL: the in-process brain must NOT run in session mode")

    agent.brain.run = _explode

    agent.handle(Message("r1", "peer@example.com", "Kickoff",
                         "can we meet about the kickoff next week?"), conn)
    queue = agent.work_queue("brain")
    assert queue.stats()["pending"] == 1, f"FAIL: the turn must be queued, got {queue.stats()}"

    # a message the deterministic intake drops must never reach the queue
    agent.handle(Message("r2", "no-reply@list.com", "newsletter", "buy now"), conn)
    assert queue.stats()["pending"] == 1, \
        "FAIL: the gateway's own drops must not be handed to the session"

    server = ParleyServer(agent)
    turn = server.work_wait({"role": "brain", "timeout": 2})
    assert turn.get("turn_id"), f"FAIL: nothing served: {turn}"

    # a READ tool is not acting: a first-contact turn that only looks around and stops
    # is exactly the silence the backstop exists to catch
    server.work_tool({"role": "brain", "name": "check",
                      "input": {"start": _START, "end": _END}})
    server.work_wait({"role": "brain", "timeout": 0.2, "done_because": "had a look"})
    assert agent.user_messaging.pending, \
        "FAIL: a first-contact turn that acted on nothing must still reach the human"

    # and when the session DOES act, the backstop stays quiet
    agent.user_messaging.pending.clear()
    agent.handle(Message("r3", "peer@example.com", "Another",
                         "can we meet about the other thing?"), conn)
    turn = server.work_wait({"role": "brain", "timeout": 2})
    acted = server.work_tool({"role": "brain", "name": "send",
                              "input": {"to": "peer@example.com", "body": "how about Tuesday?",
                                        "topic": "the other thing"}})
    assert acted.get("ok"), f"FAIL: send should work: {acted}"
    server.work_wait({"role": "brain", "timeout": 0.2, "done_because": "proposed Tuesday"})
    assert not agent.user_messaging.pending, \
        "FAIL: a turn that really acted must not be escalated as silent"
    print("SESSION-ROUTING SELFTEST PASSED ✅ — intake still deterministic here; the "
          "backstop still fires there.")


def _selftest_negotiate_endpoint():
    """Path A: the skill researched it, Parley still commits it.

    Two shapes decided by the caller's certainty — a drafted message when the peer's
    calendar is unknown, an invitation when it is known free. Both must go through the
    gated verbs, and both must write the user's own context into Parley, because the
    reply lands days later in a session that never saw them type anything.
    """
    print("\n--- running negotiate: drafted message vs verified invitation ---\n")
    from parley.client_server.server import ParleyServer
    bus = MemoryBus()
    conn = TestAccount("me@example.com", bus)
    agent = _make_agent(conn, "path-a")
    server = ParleyServer(agent)

    bad = server.negotiate({"topic": "no recipient"})
    assert not bad.get("ok"), "FAIL: negotiate needs a peer"
    bad = server.negotiate({"to": "x@y.com", "topic": "t"})
    assert not bad.get("ok") and "body" in bad.get("error", ""), \
        "FAIL: without a verified slot there is nothing to send"

    # (1) unknown availability → a drafted message opens the ordinary negotiation
    out = server.negotiate({"to": "ana@example.com", "topic": "Q4 budget",
                            "body": "Could we meet about the Q4 budget?",
                            "context": "Alex asked for this before the board meeting",
                            "deadline": "2026-09-30"})
    ref = out.get("negotiation_id")
    assert out.get("ok") is not False and ref, f"FAIL: no negotiation opened: {out}"
    assert not out.get("invited"), "FAIL: an unverified slot must not send an invitation"
    n = agent.short_term_memory.get(ref)
    assert n and n.status == "awaiting_peer", f"FAIL: unexpected state {n and n.status}"
    detail = agent.short_term_memory.detail(ref)
    assert "board meeting" in detail and "2026-09-30" in detail, \
        "FAIL: the user's context and deadline must be written INTO Parley — a later " \
        "reply reaches a session that never saw them type it"

    # (2) verified free → the proposal IS the invitation, and it holds the slot
    before = len(conn.list_events(Slot(from_iso(_START) - timedelta(days=1),
                                       from_iso(_END) + timedelta(days=1))))
    out = server.negotiate({"to": "bob@example.com", "topic": "Design review",
                            "verified": True, "slot": {"start": _START, "end": _END},
                            "title": "Design review"})
    assert out.get("invited"), f"FAIL: a verified slot must go out as an invitation: {out}"
    after = conn.list_events(Slot(from_iso(_START) - timedelta(days=1),
                                  from_iso(_END) + timedelta(days=1)))
    assert len(after) == before + 1, \
        "FAIL: the invitation IS the hold — the event must exist from the moment it is proposed"
    assert "bob@example.com" in (after[-1].attendees or []), \
        "FAIL: the peer must be on the invitation"
    assert agent.short_term_memory.get(out["negotiation_id"]) is not None, \
        "FAIL: an invited meeting is still a negotiation Parley tracks"
    print("NEGOTIATE SELFTEST PASSED ✅ — message when unsure, invitation when verified; "
          "the instruction's context lands in Parley.")


def _selftest_reply_threading():
    """Parley's own mail joins the conversation instead of starting a new one.

    The Message-IDs were recorded from the beginning — `link_thread` exists so a
    plain human reply can be correlated back — but nothing ever wrote them on the way
    OUT, so one meeting arrived in the peer's client as five unrelated mails (user,
    2026-09-24, reading a real Outlook thread).
    """
    print("\n--- running reply-threading: outbound mail replies onto the thread ---\n")
    from parley.core import mime_message

    plain = mime_message("Hi", "me@example.com", "peer@example.com", "hello")
    assert plain.get("In-Reply-To") is None and plain.get("References") is None, \
        "FAIL: a first message threads onto nothing"
    reply = mime_message("Re: Hi", "me@example.com", "peer@example.com", "sure",
                         thread=["<a@x>", "<b@x>", ""])
    assert reply["In-Reply-To"] == "<b@x>", \
        "FAIL: In-Reply-To names the message being answered (the newest)"
    assert reply["References"] == "<a@x> <b@x>", \
        "FAIL: References is the whole chain, which is what clients thread on"

    # end to end through the gate: the ids recorded on the negotiation reach the wire
    bus = MemoryBus()
    conn = TestAccount("me@example.com", bus)
    peer = TestAccount("peer@example.com", bus)
    agent = _make_agent(conn, "thread")
    ref = "mtg-thread01"
    agent.short_term_memory.upsert(ref, topic="Sync", direction="outgoing",
                                   status="open", account=conn.address())
    agent.short_term_memory.link_thread(ref, "<first@peer>", "<second@peer>")
    result = agent.accounts_gate.send(conn, to="peer@example.com", body="how about Tuesday?",
                                      ref=ref)
    assert result.get("ok"), f"FAIL: send should work: {result}"
    landed = peer.list_messages()
    assert len(landed) == 1, f"FAIL: expected one mail, got {len(landed)}"
    assert landed[0].in_reply_to == "<second@peer>", \
        "FAIL: the gate must hand the negotiation's thread to the backend"
    assert landed[0].thread_refs() == ["<second@peer>", "<first@peer>"], \
        f"FAIL: the whole chain must travel, got {landed[0].thread_refs()}"

    # and a brand-new negotiation still opens its own conversation
    fresh = agent.accounts_gate.send(conn, to="peer@example.com", body="new topic",
                                     topic="Something else")
    assert fresh.get("ok"), f"FAIL: {fresh}"
    assert peer.list_messages()[0].in_reply_to is None, \
        "FAIL: a first contact has no thread to reply onto"
    print("REPLY-THREADING SELFTEST PASSED ✅ — replies carry In-Reply-To + References; "
          "a first contact does not.")


def _selftest_group_conversation():
    """A meeting with several people is ONE conversation, and Parley answers the way
    it was spoken to.

    Parley used to hold N private conversations about one meeting: each invitee got
    their own email and could not see who else had been asked, and the same person
    could end up in two threads that knew nothing about each other (user, 2026-09-24,
    reading a real Outlook mailbox). One send, one mail, everybody on the To: line.
    """
    print("\n--- running group-conversation: one meeting, one thread, N people ---\n")
    from parley.core import Message

    bus = MemoryBus()
    conn = TestAccount("me@example.com", bus)
    laura = TestAccount("laura@example.com", bus)
    carles = TestAccount("carles@example.com", bus)
    joan = TestAccount("joan@example.com", bus)
    agent = _make_agent(conn, "group")
    everyone = "laura@example.com, carles@example.com, joan@example.com"

    out = agent.accounts_gate.send(conn, to=everyone, body="when suits you all?",
                                  topic="Roadmap review")
    ref = out["negotiation_id"]
    assert out["recipients"] == ["laura@example.com", "carles@example.com",
                                 "joan@example.com"], f"FAIL: {out}"
    landed = [box.list_messages() for box in (laura, carles, joan)]
    assert all(len(m) == 1 for m in landed), "FAIL: each invitee gets the mail exactly once"
    assert len({m[0].message_id for m in landed}) == 1, \
        "FAIL: it is ONE message, so it carries one Message-ID — not three mails"
    assert all(len(m[0].recipients) == 3 for m in landed), \
        "FAIL: everyone must see who else was asked"
    assert len(agent.short_term_memory.get(ref).parties) == 3, \
        "FAIL: one negotiation, three parties"

    # A reply to everyone is answered to everyone...
    to_all = Message("r1", "joan@example.com", "Re: Roadmap review",
                     f"Tuesday works for me. (ref: {ref})",
                     recipients=["me@example.com", "laura@example.com", "carles@example.com"])
    event = agent.brain.turn_for_message(to_all, conn).event
    assert "Also addressed to:" in event and "laura@example.com" in event, \
        "FAIL: the brain must be told who else is in this conversation"
    assert "reply to ALL" in event

    # ...and a message sent to nobody but us is answered in private.
    aside = Message("r2", "carles@example.com", "Re: Roadmap review",
                    f"Between us, I would rather not meet Joan. (ref: {ref})",
                    recipients=["me@example.com"])
    event = agent.brain.turn_for_message(aside, conn).event
    assert "sent to you ALONE" in event, "FAIL: a private aside must be recognised as one"
    assert "Also addressed to:" not in event

    # A backend that cannot report recipients must read as "cannot tell", never as
    # "private" — guessing privacy wrong is how a confidence gets forwarded.
    blind = Message("r3", "carles@example.com", "Re: Roadmap review", f"ok (ref: {ref})")
    event = agent.brain.turn_for_message(blind, conn).event
    assert "sent to you ALONE" not in event and "Also addressed to:" not in event, \
        "FAIL: unknown recipients must not be read as a private aside"

    # And the mis-correlation that started all this: a separate one-to-one whose topic
    # merely RESEMBLES the group meeting's must not inherit the group's ref.
    solo = agent.accounts_gate.send(conn, to="joan@example.com", body="just us two?",
                                    topic="Roadmap review follow-up with Joan")
    assert solo["negotiation_id"] != ref, \
        "FAIL: a one-to-one must not be absorbed into a group meeting by a fuzzy topic"
    # while a genuine follow-up to the same group still threads onto it
    same = agent.accounts_gate.send(conn, to=everyone, body="reminder",
                                    topic="Roadmap review")
    assert same["negotiation_id"] == ref, "FAIL: the group's own follow-up keeps its ref"
    print("GROUP-CONVERSATION SELFTEST PASSED ✅ — one mail to all, reply mode follows "
          "the message, and a one-to-one keeps its own ref.")


def _selftest_send_cap():
    """Nobody gets written to forever: a per-party bound that asks the human.

    The first real multi-party run put TEN of the fourteen outgoing mails in front of
    one colleague (2026-09-24). No error was raised and no rule was broken — each turn
    was individually reasonable and nothing counted them. The bound is per PARTY, so a
    fan-out to several people is untouched, and tripping it hands the meeting to the
    human rather than abandoning it.
    """
    print("\n--- running send-cap: one party, one meeting, a bounded number of mails ---\n")
    bus = MemoryBus()
    conn = TestAccount("me@example.com", bus)
    peer = TestAccount("peer@example.com", bus)
    other = TestAccount("other@example.com", bus)
    agent = _make_agent(conn, "capped")
    _inject(agent, lambda system, transcript, tools: LLMResponse(text="noted."))
    gate = agent.accounts_gate
    cap = gate.MAX_SENDS_PER_PARTY

    opened = gate.send(conn, to="peer@example.com", body="round 1", topic="Budget")
    ref = opened["negotiation_id"]
    for i in range(2, cap + 1):
        assert gate.send(conn, to="peer@example.com", body=f"round {i}", ref=ref).get("ok"), \
            f"FAIL: message {i} is within the budget and must go out"
    assert len(peer.list_messages()) == cap, "FAIL: every message up to the cap must be sent"

    # Same meeting, different person: a multiparty fan-out must not inherit the
    # exhaustion of one track — the count is per party for exactly this reason.
    assert gate.send(conn, to="other@example.com", body="same meeting, other person",
                     ref=ref).get("ok"), "FAIL: the cap is per party, not per negotiation"
    assert len(other.list_messages()) == 1

    blocked = gate.send(conn, to="peer@example.com", body="round 7", ref=ref)
    assert not blocked.get("ok"), "FAIL: the send past the cap must be refused"
    assert blocked.get("escalated") == ref, "FAIL: refusing must ask the human, on this ref"
    assert not peer.list_messages(), "FAIL: the refused message must not reach the peer"
    assert any(q["negotiation_id"] == ref for q in agent.pending_decisions()), \
        "FAIL: the question must be waiting for the human"
    assert agent.short_term_memory.get(ref).status == "awaiting_human"

    # Answering is the informed go-ahead the cap exists to demand — and it must
    # actually restore the budget, or "yes, keep going" trips the same cap forever.
    agent.decide(ref, "keep going")
    assert gate.send(conn, to="peer@example.com", body="round 8", ref=ref).get("ok"), \
        "FAIL: once the human has answered, the agent may write again"
    assert len(peer.list_messages()) == 1
    print("SEND-CAP SELFTEST PASSED ✅ — bounded per party, escalates instead of "
          "abandoning, and the human's answer restores the budget.")


def _selftest_human_surface():
    """A session helping the human decide can read and answer — and nothing else.

    This suite is the security boundary of the human surface written down. A Claude
    session sitting with the user is prompted, and a prompt is not a permission
    system: the daemon has to refuse the acting verbs itself, whatever the session
    asks for and whatever its own tool list says.
    """
    print("\n--- running human-surface: read-only consult, answer, standing rules ---\n")
    from parley.client_server.server import ParleyServer
    from parley.mcp_server import Bridge, _tool_list
    bus = MemoryBus()
    conn = TestAccount("gmail@example.com", bus)
    agent = _make_agent(conn, "human", learned_path=os.path.join(
        tempfile.mkdtemp(prefix="parley_learned_"), "policy_learned.md"))
    server = ParleyServer(agent)

    # reads go through
    got = server.consult({"name": "check", "input": {"start": _START, "end": _END}})
    assert got.get("ok") and "busy" in got, f"FAIL: a read verb must run: {got}"

    # everything that ACTS is refused here, by the daemon, by name
    for verb in ("send", "apply", "close", "defer_action", "ask_user", "remember_policy"):
        refused = server.consult({"name": verb, "input": {}})
        assert not refused.get("ok"), \
            f"FAIL: {verb} must never be reachable from a consulting session"
        assert "read" in refused.get("error", "").lower(), \
            f"FAIL: the refusal should say why, got {refused.get('error')!r}"

    # policy: read, then add a standing rule, then see it in what the brain runs on
    before = server.policy()
    assert before["ok"] and before["effective"], "FAIL: policy must be readable"
    assert "trust" in before, "FAIL: the cfg-only trust table should be visible (not writable)"
    added = server.policy({"rule": "never book me before 10:00"})
    assert added.get("ok"), f"FAIL: adding a rule should work: {added}"
    after = server.policy()
    assert "never book me before 10:00" in after["effective"], \
        "FAIL: a standing rule must reach the policy the brain actually runs on"

    # answering a real escalation resolves it
    ref = agent.escalate(conn, "Accept Tuesday 10:00?", negotiation_id="mtg-human01")
    assert agent.pending_decisions(), "FAIL: the question should be pending"
    agent.decide(ref, "yes, accept it")
    assert not [p for p in agent.pending_decisions() if p["negotiation_id"] == ref], \
        "FAIL: answering must resolve the question"

    # and the session is never even OFFERED a way to act
    offered = {t["name"] for t in _tool_list("human")}
    assert offered == {"pending", "answer", "dismiss", "policy", "check", "get_negotiation",
                       "find_contact", "search_events"}, f"FAIL: unexpected tools {offered}"
    assert not offered & {"send", "apply", "close", "ask_user", "defer_action"}, \
        "FAIL: helping someone decide is not the same job as acting for them"
    assert "wait_for_work" not in offered, "FAIL: a human session drains no queue"

    # routing: each tool reaches the endpoint that enforces its own rules
    seen = []
    bridge = Bridge("http://x", "human")
    bridge._post = lambda path, body, timeout: seen.append(("POST", path)) or {}
    bridge._get = lambda path, timeout=30: seen.append(("GET", path)) or {}
    bridge.human_call("pending", {})
    bridge.human_call("answer", {"ref": "r", "text": "yes"})
    bridge.human_call("dismiss", {"ref": "r", "reason": "done in Outlook"})
    bridge.human_call("policy", {})
    bridge.human_call("policy", {"rule": "no fridays"})
    bridge.human_call("check", {"start": "a", "end": "b"})
    assert seen == [("GET", "/v1/pending"), ("POST", "/v1/decide"), ("POST", "/v1/dismiss"),
                    ("GET", "/v1/policy"),
                    ("POST", "/v1/policy"), ("POST", "/v1/consult")], f"FAIL: routing {seen}"
    print("HUMAN-SURFACE SELFTEST PASSED ✅ — reads allowed, acting verbs refused by the "
          "daemon, rules land in the live policy.")


def _selftest_question_cards():
    """Questions show short and close themselves when nothing is left to decide.

    Two complaints from real use (2026-10-18): a card was a wall of text (a quoted mail,
    up to ~900 characters, headed by a bare ref), and the tray kept questions about
    meetings long since booked, cancelled, past, or answered straight in Outlook — 32
    open at once. What must hold: every question carries a one-line summary and who /
    what / when; a question closes — silently, no brain turn — when its meeting ends
    after it was asked, when its date passes, or when the user answers the invite in
    their calendar; and the user can dismiss one without the agent re-planning on it.
    """
    print("\n--- running question cards: summary, dismiss, auto-close ---\n")
    from parley.agent.user_messaging import SUMMARY_MAX, UserMessaging, one_line
    from parley.client_server.server import ParleyServer
    from parley.core import Event
    bus = MemoryBus()
    conn = TestAccount("me@example.com", bus)
    agent = _make_agent(conn, "cards")
    turns = []
    _inject(agent, lambda *a, **k: turns.append(a) or LLMResponse(text="ok", done=True))

    # a long question gets a one-line summary that fits
    long_q = ("Ana Ruiz sent you an invite for the budget review on Tuesday at 10:00 and "
              "it overlaps your weekly sync with Joan, which you organize. " * 6
              + "\n\nFrom: ana@example.com\nSubject: Budget\n\n" + "quoted mail " * 80)
    assert len(one_line(long_q)) <= SUMMARY_MAX, "FAIL: a summary must fit on one line"
    assert "\n" not in one_line("line one is long enough to stand?\nline two"), \
        "FAIL: a summary is one line"

    # an invite question, asked mid-turn: kind, who, title and slot come from the mail
    start = (datetime.now(timezone.utc) + timedelta(days=2)).replace(microsecond=0)
    end = start + timedelta(minutes=30)
    invite = Message("m-inv", "ana@example.com", "Budget review", "join us",
                     invite={"title": "Budget review", "start": to_iso(start),
                             "end": to_iso(end), "organizer": "ana@example.com"},
                     sender_name="Ana Ruiz")
    agent.current_turn_message = invite
    invite_ref = wire.parse(invite)["negotiation_id"]
    asked = agent.accounts_gate.ask_user(conn, long_q, ref=invite_ref)
    other = agent.accounts_gate.ask_user(conn, "And the other one?", ref="mtg-other001",
                                         summary="The other one?")["escalated"]
    agent.current_turn_message = None
    ref = asked["escalated"]
    stray = next(p for p in agent.pending_decisions() if p["negotiation_id"] == other)
    assert stray["title"] is None and stray["start"] is None, \
        f"FAIL: a question about ANOTHER meeting must not borrow this turn's mail: {stray}"
    agent.dismiss(other)
    card = next(p for p in agent.pending_decisions() if p["negotiation_id"] == ref)
    assert card["kind"] == "invite" and card["from"] == "Ana Ruiz" \
        and card["title"] == "Budget review" and card["start"] == to_iso(start), \
        f"FAIL: an invite card must say who, what and when: {card}"
    assert len(card["summary"]) <= SUMMARY_MAX and card["question"] == long_q, \
        "FAIL: the summary is short; the full question is still there for the detail view"

    # the user answers the invite in their own calendar: the sweep notices
    conn.calendar.append(Event("ev-1", "Budget review", start, end,
                               ["me@example.com", "ana@example.com"],
                               organizer="ana@example.com", response="none"))
    assert agent.sweep_questions(calendar=True) == [], \
        "FAIL: an invite not answered yet in the calendar is still open"
    conn.calendar[-1].response = "accepted"
    assert agent.sweep_questions(calendar=False) == [], \
        "FAIL: the calendar is only read when the sweep says so"
    assert agent.sweep_questions(calendar=True) == [ref], \
        "FAIL: an invite the user accepted in their calendar must close"

    # a question about a negotiation: closes once the meeting ends AFTER the ask...
    mem = agent.short_term_memory
    mem.upsert("mtg-neg00001", topic="Sync", status="awaiting_peer", account=conn.address())
    agent.escalate(conn, "No reply yet. Nudge?", negotiation_id="mtg-neg00001", kind="stalled")
    assert agent.sweep_questions(calendar=False) == [], "FAIL: nothing ended yet"
    mem.close("mtg-neg00001", "booked")
    assert agent.sweep_questions(calendar=False) == ["mtg-neg00001"], \
        "FAIL: a question whose meeting got booked must close"
    # ...but NOT when it was asked about an already-ended one (a stall ask on a
    # deadlocked negotiation, a move ask on a booked one)
    mem.upsert("mtg-dead0001", topic="Lunch", status="awaiting_peer", account=conn.address())
    mem.close("mtg-dead0001", "deadlocked")
    agent.escalate(conn, "Drop it?", negotiation_id="mtg-dead0001", kind="stalled")
    assert agent.sweep_questions(calendar=False) == [], \
        "FAIL: a question asked ABOUT a deadlock must not close because of it"

    # a question whose meeting is over closes
    past = datetime.now(timezone.utc) - timedelta(days=1)
    agent.escalate(conn, "Accept the old one?", negotiation_id="mtg-past0001",
                   start=to_iso(past), end=to_iso(past + timedelta(hours=1)))
    assert agent.sweep_questions(calendar=False) == ["mtg-past0001"], \
        "FAIL: a question about a meeting that is over must close"

    # dismiss: closed, journaled, the negotiation unfrozen — and NO brain turn
    mem.upsert("mtg-dism0001", topic="Coffee", status="open", account=conn.address())
    agent.escalate(conn, "Engage?", negotiation_id="mtg-dism0001")
    mem.upsert("mtg-dism0001", status="awaiting_human")
    server = ParleyServer(agent)
    got = server.dismiss({"negotiation_id": "mtg-dism0001", "reason": "done in Outlook"})
    assert got.get("ok"), f"FAIL: dismiss should close an open question: {got}"
    assert not turns, "FAIL: dismissing must not wake the brain"
    assert mem.get("mtg-dism0001").status == "open", \
        "FAIL: a negotiation frozen on a dismissed question must be unfrozen"
    assert not server.dismiss({"negotiation_id": "mtg-dism0001"}).get("ok"), \
        "FAIL: a second dismiss is a no-op"
    assert [p["negotiation_id"] for p in agent.pending_decisions()] == ["mtg-dead0001"], \
        f"FAIL: only the deadlock question should be left: {agent.pending_decisions()}"
    closed = [r for r in agent.log.records if r["kind"] == "question_closed"]
    assert len(closed) == 5, f"FAIL: every close must be journaled, got {len(closed)}"

    # questions saved before these fields existed still load
    path = os.path.join(tempfile.mkdtemp(prefix="parley_um_"), "user_messaging.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"inbox": [], "grants": [], "pending": {"mtg-old": {
            "ref": "mtg-old", "question": "Old?", "created_at": "2026-09-01T00:00:00+00:00",
            "resolved": False, "answer": None, "some_future_field": 1}}}, f)
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    raw["pending"]["mtg-oldmail"] = {
        "ref": "mtg-oldmail", "created_at": "2026-09-01T00:00:00+00:00", "resolved": False,
        "question": "Engage with this email?\nFrom: joan@example.com\nSubject: Pressupost\n\nHola"}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(raw, f)
    legacy = UserMessaging(path)
    assert legacy.list_pending()[0].kind is None, "FAIL: an old question must still load"

    # ...and get their invite's slot back from the journal, so the sweep can see them
    agent.user_messaging = legacy
    agent.log.dir = tempfile.mkdtemp(prefix="parley_journal_")
    with open(os.path.join(agent.log.dir, "journal-2026-09-01.jsonl"), "w",
              encoding="utf-8") as f:
        f.write(json.dumps({"kind": "payload", "type": "meeting_request",
                            "negotiation_id": "mtg-old", "title": "Old sync ",
                            "start": to_iso(past), "end": to_iso(past)}) + "\n")
    assert sorted(agent.backfill_questions()) == ["mtg-old", "mtg-oldmail"], \
        "FAIL: the backfill (journal + Parley's own question text)"
    mail = next(p for p in agent.pending_decisions() if p["negotiation_id"] == "mtg-oldmail")
    assert (mail["kind"], mail["from"], mail["title"]) == \
        ("email", "joan@example.com", "Pressupost"), f"FAIL: an old intake ask: {mail}"
    agent.dismiss("mtg-oldmail")
    assert agent.sweep_questions(calendar=False) == ["mtg-old"], \
        "FAIL: a backfilled question about a past meeting must close"
    # classic Outlook: a colleague's "Aceptada: …" is an answer to our invite, not
    # mail to ask about (live 2026-10-18: three of these sat in the tray as questions)
    from types import SimpleNamespace as _NS
    from parley.connectors.accounts import local_outlook
    when = datetime(2026, 11, 3, 9, 0)
    appt = _NS(Subject="Quarterly review", StartUTC=when,
               EndUTC=when + timedelta(minutes=30), GlobalAppointmentID="uid-1",
               GetOrganizer=lambda: _NS(GetExchangeUser=lambda: _NS(
                   PrimarySmtpAddress="me@example.com")))
    for cls, expected in (("IPM.Schedule.Meeting.Resp.Pos", "accepted"),
                          ("IPM.Schedule.Meeting.Resp.Tent", "tentative"),
                          ("IPM.Schedule.Meeting.Resp.Neg", "declined")):
        item = _NS(MessageClass=cls, SenderEmailAddress="joan@example.com",
                   SenderEmailType="SMTP", GetAssociatedAppointment=lambda _f: appt)
        inv = local_outlook._invite_from_meeting_item(item)
        assert inv and inv["response"] == expected and inv["responder"] == "joan@example.com" \
            and inv["organizer"] == "me@example.com", f"FAIL: a {cls} answer: {inv}"
    accepted = Message("m-acc", "joan@example.com", "Aceptada: Quarterly review", "",
                       invite=local_outlook._invite_from_meeting_item(_NS(
                           MessageClass="IPM.Schedule.Meeting.Resp.Pos",
                           SenderEmailAddress="joan@example.com", SenderEmailType="SMTP",
                           GetAssociatedAppointment=lambda _f: appt)))
    assert wire.parse(accepted)["type"] == "rsvp_reply", "FAIL: it must parse as an answer"
    before = len(agent.pending_decisions())
    agent.handle(accepted, conn)
    assert len(agent.pending_decisions()) == before and not turns, \
        "FAIL: an acceptance must neither ask the user nor wake the brain"
    plain = _NS(MessageClass="IPM.Note")
    assert local_outlook._invite_from_meeting_item(plain) is None, "FAIL: plain mail"

    print("QUESTION-CARDS SELFTEST PASSED ✅ — one-line summaries, who/what/when, "
          "silent dismiss, auto-close on ended / past / answered-in-calendar.")


def _selftest_focus_blocks():
    """Dynamic Focus Blocks, F4: the calendar writes underneath them.

    A focus block is time kept on the user's own calendar for their own work — "2 h to
    prepare meeting Z". What must hold before any logic is built on it: a block has no
    attendees and sends nothing; it carries Parley's mark, so it can always be found
    again; it can be hardened (free → busy) and moved; and Parley refuses to change or
    delete anything WITHOUT the mark, so a stale id can never touch a real meeting.
    The three real backends must read the mark and the free/busy state back."""
    print("\n--- running focus blocks: hold, harden, move, release, refuse ---\n")
    from types import SimpleNamespace as _NS
    from parley.connectors.accounts import gmail as _gmail
    from parley.connectors.accounts import local_outlook as _lo
    from parley.connectors.accounts import outlook as _graph
    from parley.core import FOCUS_MARK, Event
    bus = MemoryBus()
    conn = TestAccount("me@example.com", bus)
    agent = _make_agent(conn, "focus")
    gate = agent.accounts_gate
    start = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)
    end = start + timedelta(hours=2)

    made = gate.hold(conn, to_iso(start), to_iso(end), "Prep Meeting Z")
    assert made.get("ok") and made["title"] == "🧠 DFB: Prep Meeting Z", \
        f"FAIL: a focus block is created and named for its purpose: {made}"
    block = next(e for e in conn.calendar if e.id == made["event_id"])
    assert block.focus and block.show_as == "free" and block.attendees == ["me@example.com"], \
        f"FAIL: a block is ours, free, with nobody invited: {block}"
    assert not any(bus.boxes.values()), "FAIL: a focus block sends nothing"
    assert not gate.hold(conn, to_iso(start), to_iso(end), "x", show_as="maybe").get("ok"), \
        "FAIL: an unknown free/busy state is refused"

    # harden, then move
    got = gate.update_hold(conn, block.id, to_iso(start), to_iso(end), show_as="busy")
    assert got.get("ok") and block.show_as == "busy", f"FAIL: hardening: {got}"
    later = start + timedelta(days=1)
    got = gate.update_hold(conn, block.id, to_iso(start), to_iso(end),
                           new_start=to_iso(later), new_end=to_iso(later + timedelta(hours=2)))
    assert got.get("ok") and block.start == later, f"FAIL: moving: {got}"

    # never on a real meeting, even by its id
    meeting = conn.create_event("Budget review", Slot(later, later + timedelta(hours=1)),
                                "ana@example.com")
    for refused in (gate.update_hold(conn, meeting.id, to_iso(later),
                                     to_iso(later + timedelta(hours=1)), show_as="free"),
                    gate.release_hold(conn, meeting.id, to_iso(later),
                                      to_iso(later + timedelta(hours=1)))):
        assert not refused.get("ok") and meeting in conn.calendar, \
            f"FAIL: Parley must never change or delete a meeting that is not its block: {refused}"

    # release; releasing again is fine
    got = gate.release_hold(conn, block.id, to_iso(later), to_iso(later + timedelta(hours=2)),
                            reason="Z cancelled")
    assert got.get("ok") and block not in conn.calendar, f"FAIL: releasing: {got}"
    again = gate.release_hold(conn, block.id, to_iso(later), to_iso(later + timedelta(hours=2)))
    assert again.get("ok") and again.get("already_gone"), "FAIL: a second release is a no-op"
    kinds = [r["kind"] for r in agent.log.records]
    assert {"hold_created", "hold_updated", "hold_released", "hold_refused"} <= set(kinds), \
        "FAIL: every focus-block write is journaled"

    # the real backends read the mark and the free/busy state back
    graph = _graph.OutlookAccount.__new__(_graph.OutlookAccount)
    ev = graph._to_event({"id": "g1", "subject": "🧠 DFB: Prep",
                          "start": {"dateTime": "2026-11-02T09:00:00"},
                          "end": {"dateTime": "2026-11-02T11:00:00"},
                          "showAs": "free", "categories": [FOCUS_MARK]})
    assert ev.focus and ev.show_as == "free", f"FAIL: Graph mapping: {ev}"
    google = _gmail.GmailAccount.__new__(_gmail.GmailAccount)
    google._address, google._calendar_of = "me@example.com", {}
    ev = google._to_event({"id": "c1", "summary": "🧠 DFB: Prep",
                           "start": {"dateTime": "2026-11-02T09:00:00+00:00"},
                           "end": {"dateTime": "2026-11-02T11:00:00+00:00"},
                           "transparency": "transparent", "organizer": {"self": True},
                           "extendedProperties": {"private": {"parley": FOCUS_MARK}}})
    assert ev.focus and ev.show_as == "free", f"FAIL: Google mapping: {ev}"
    when = datetime(2026, 11, 2, 9, 0)
    appt = _NS(MessageClass="IPM.Appointment", Subject="🧠 DFB: Prep", StartUTC=when,
               EndUTC=when + timedelta(hours=2), Recipients=[], EntryID="o1",
               BusyStatus=0, Categories=f"Blue, {FOCUS_MARK}", ResponseStatus=0,
               GetOrganizer=lambda: None)
    ev = _lo._to_event(appt)
    assert ev.focus and ev.show_as == "free", f"FAIL: classic Outlook mapping: {ev}"
    appt.BusyStatus, appt.Categories = 2, "Blue"
    ev = _lo._to_event(appt)
    assert not ev.focus and ev.show_as == "busy", f"FAIL: a plain busy appointment: {ev}"
    # == F5: the Dynamic Focus Block, kept true while the calendar changes ==========
    from parley.agent import focus as _focus
    from parley.client_server.server import ParleyServer

    # the arithmetic: working day, free time, capacity, latest-first placement
    mon = datetime(2026, 11, 2, tzinfo=timezone.utc)
    days = _focus.working_windows(Slot(mon, mon + timedelta(days=7)), "Europe/Madrid")
    assert len(days) == 5 and days[0].start.hour == 8, \
        f"FAIL: Mon–Fri 09:00–18:00 Madrid (08:00 UTC in November): {days[:1]}"
    free = _focus.free_intervals([Slot(mon + timedelta(hours=9), mon + timedelta(hours=18))],
                                 [Slot(mon + timedelta(hours=10, minutes=10),
                                       mon + timedelta(hours=12))])
    assert [(f.start.hour, f.start.minute, f.end.hour) for f in free] == \
        [(9, 0, 10), (12, 0, 18)], f"FAIL: free time snapped to the quarter hour: {free}"
    assert _focus.capacity(free, 2, False) == 6 and _focus.capacity(free, 2, True) == 7, \
        "FAIL: capacity counts whole blocks unless splittable"
    assert _focus.place(free, 1, 2, False)[0].start.hour == 16, \
        "FAIL: a block goes as close to the meeting as it can"

    # a DFB end to end, two weeks out so the dates never go stale
    bus = MemoryBus()
    conn = TestAccount("me@example.com", bus)
    agent = _make_agent(conn, "dfb")
    agent.tz = "UTC"  # the working day in UTC, so the numbers below are plain
    gate = agent.accounts_gate
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    base = today + timedelta(days=14 + (7 - today.weekday()) % 7)        # a Monday
    at = lambda d, h, m=0: base + timedelta(days=d, hours=h, minutes=m)  # noqa: E731
    z = conn.create_event("Board review", Slot(at(3, 10), at(3, 11)), "ana@example.com")
    made = agent.focus.create({"anchor": {"title": "board review", "start": to_iso(at(3, 10))},
                               "window": {"days": 3}, "blocks": 1, "hours": 2,
                               "purpose": "Prep Board review"})
    assert made.get("ok") and made["anchor"]["event_id"] == z.id, \
        f"FAIL: Z is found by title + start and anchored by its id: {made}"
    dfb_id = made["dfb_id"]
    assert made["state"] == "soft" and len(made["placed"]) == 1 \
        and made["placed"][0]["start"] == to_iso(at(2, 16)) \
        and made["placed"][0]["show_as"] == "free", f"FAIL: placed loosely, late: {made}"
    assert gate.check(conn, to_iso(at(2, 16)), to_iso(at(2, 18)))["free"], \
        "FAIL: a soft block must not count as busy — a meeting may take it"

    # someone books over it: it moves
    conn.create_event("Sync", Slot(at(2, 16), at(2, 17)), "joan@example.com")
    agent.focus.sweep(force=True)
    placed = agent.focus.describe(dfb_id)["placed"]
    assert len(placed) == 1 and placed[0]["start"] != to_iso(at(2, 16)), \
        f"FAIL: a block that lost its time moves: {placed}"

    # the window gets tight: it hardens, and the user is told (not asked)
    for d, h0, h1 in ((0, 0, 24), (1, 0, 24), (2, 9, 13)):
        conn.create_event(f"Busy {d}", Slot(at(d, h0), at(d, h1)), "x@example.com")
    agent.focus.sweep(force=True)
    got = agent.focus.describe(dfb_id)
    assert got["state"] == "hard" and got["placed"][0]["show_as"] == "busy", \
        f"FAIL: tight time hardens the block: {got}"
    notice = [p for p in agent.pending_decisions() if p["kind"] == "notice"]
    assert len(notice) == 1, "FAIL: hardening tells the user, once"
    agent.focus.sweep(force=True)
    assert len([p for p in agent.pending_decisions() if p["kind"] == "notice"]) == 1, \
        "FAIL: no second notice while nothing changes"

    # no room at all: the user is asked; "drop it" drops it, with no brain turn
    turns = []
    _inject(agent, lambda *a, **k: turns.append(a) or LLMResponse(text="ok", done=True))
    conn.create_event("Busy 2b", Slot(at(2, 13), at(2, 15)), "x@example.com")
    agent.focus.sweep(force=True)
    ask = [p for p in agent.pending_decisions() if p["kind"] == "focus"]
    assert agent.focus.describe(dfb_id)["state"] == "short" and len(ask) == 1, \
        "FAIL: no room asks the user what yields"
    agent.decide(ask[0]["negotiation_id"], "drop it")
    assert dfb_id not in agent.focus.items and not turns, \
        "FAIL: 'drop it' drops the focus time, without waking the brain"
    assert not [p for p in agent.pending_decisions() if p["negotiation_id"].startswith(dfb_id)], \
        "FAIL: a dropped focus time leaves no notice or question behind"
    assert not [e for e in conn.calendar if e.focus], "FAIL: its blocks are gone too"

    # Z cancelled: the blocks go (after two sweeps, so one short read deletes nothing)
    z2 = conn.create_event("Offsite", Slot(at(10, 10), at(10, 11)), "ana@example.com")
    made = agent.focus.create({"anchor": {"event_id": z2.id, "start": to_iso(at(10, 10))},
                               "window": {"days": 2}, "blocks": 2, "hours": 1})
    assert made.get("ok") and len(made["placed"]) == 2, f"FAIL: two blocks: {made}"
    conn.delete_event(z2.id)
    agent.focus.sweep(force=True)
    assert made["dfb_id"] in agent.focus.items, "FAIL: one missing read is not enough"
    agent.focus.sweep(force=True)
    assert made["dfb_id"] not in agent.focus.items and not [e for e in conn.calendar
                                                            if e.focus], \
        "FAIL: a cancelled Z takes its focus time with it"

    # Z moved: the window follows
    z3 = conn.create_event("Retro", Slot(at(10, 10), at(10, 11)), "ana@example.com")
    made = agent.focus.create({"anchor": {"event_id": z3.id, "start": to_iso(at(10, 10))},
                               "window": {"hours": 24}, "blocks": 1, "hours": 1})
    conn.update_event(z3.id, {"start": at(11, 15), "end": at(11, 16)})
    agent.focus.sweep(force=True)
    got = agent.focus.describe(made["dfb_id"])
    assert got["anchor"]["start"] == to_iso(at(11, 15)) \
        and got["placed"][0]["end"] <= to_iso(at(11, 15)) \
        and got["placed"][0]["start"] >= to_iso(at(10, 15)), f"FAIL: Z moved: {got}"
    assert [e.id for e in conn.calendar if e.focus] == [got["placed"][0]["event_id"]], \
        "FAIL: the block the window left behind is released at once, not orphaned"

    # at start: a marked block nobody owns is an orphan and goes; owned ones stay
    orphan = gate.hold(conn, to_iso(at(5, 9)), to_iso(at(5, 10)), "Lost")
    agent.focus.reconcile()
    ids = {e.id for e in conn.calendar}
    assert orphan["event_id"] not in ids and got["placed"][0]["event_id"] in ids, \
        "FAIL: reconcile deletes orphans only"

    # the endpoint the CLI and /myorganizer use
    server = ParleyServer(agent)
    listed = server.focus({"action": "list"})
    assert listed["ok"] and [b["dfb_id"] for b in listed["blocks"]] == [made["dfb_id"]], \
        f"FAIL: list: {listed}"
    assert server.focus({"action": "cancel", "dfb_id": made["dfb_id"]})["ok"] \
        and not agent.focus.items, "FAIL: cancel"
    assert not server.focus({"action": "nonsense"})["ok"], "FAIL: an unknown action"

    # == F6: a booking that would take the LAST time for focus work is a decision ====
    conn6 = TestAccount("me6@example.com", MemoryBus())
    agent6 = _make_agent(conn6, "dfb6")
    agent6.tz = "UTC"
    gate6 = agent6.accounts_gate
    z6 = conn6.create_event("Launch", Slot(at(3, 10), at(3, 11)), "ana@example.com")
    conn6.create_event("Standup", Slot(at(2, 10), at(2, 12)), "joan@example.com")
    made = agent6.focus.create({"anchor": {"event_id": z6.id, "start": to_iso(at(3, 10))},
                                "window": {"hours": 24}, "blocks": 1, "hours": 4,
                                "margin_hours": 0, "purpose": "Launch deck"})
    assert made["state"] == "soft", f"FAIL: 6 h free for 4 h of work: {made}"
    assert made["dfb_id"] in agent6.brain._system(conn6), \
        "FAIL: the brain sees the user's focus time in its context"
    harmless = gate6.check(conn6, to_iso(at(2, 12)), to_iso(at(2, 13)))
    assert "focus" not in harmless, f"FAIL: a slot that leaves room says nothing: {harmless}"
    risky = gate6.check(conn6, to_iso(at(2, 15)), to_iso(at(2, 16)))
    assert risky.get("focus") and risky["focus"][0]["state_after"] == "short", \
        f"FAIL: check flags a slot that takes the last focus time: {risky}"
    held = gate6.apply(conn6, "create", start=to_iso(at(2, 15)), end=to_iso(at(2, 16)),
                       attendee="bob@example.com", title="Quick chat")
    assert not held.get("ok") and held.get("focus_conflict"), \
        f"FAIL: apply refuses the last focus time without policy cover: {held}"
    assert not [e for e in conn6.calendar if e.title == "Quick chat"], "FAIL: nothing written"
    ok = gate6.apply(conn6, "create", start=to_iso(at(2, 12)), end=to_iso(at(2, 13)),
                     attendee="bob@example.com", title="Coffee")
    assert ok.get("ok"), f"FAIL: a slot that leaves room books normally: {ok}"
    forced = gate6.apply(conn6, "create", start=to_iso(at(2, 15)), end=to_iso(at(2, 16)),
                         attendee="carol@example.com", title="Board call",
                         override_focus=True)
    assert forced.get("ok"), f"FAIL: policy may override the focus guard: {forced}"
    agent6.focus.sweep(force=True)
    assert agent6.focus.describe(made["dfb_id"])["state"] == "short" and \
        [p for p in agent6.pending_decisions() if p["kind"] == "focus"], \
        "FAIL: after an override the focus time is short and the user is asked"

    print("FOCUS-BLOCKS SELFTEST PASSED ✅ — no attendees, nothing sent, marked; harden, "
          "move, release; real meetings refused; Graph/Google/COM read it back; DFB "
          "places late, moves when taken, hardens and tells, asks when short, follows Z, "
          "drops with Z, cleans orphans.")


def _selftest_ask_session():
    """Clicking a pending question can open a Claude session instead of the window.

    Nothing here spawns anything: what is worth locking down is the shape of what
    WOULD be spawned — that the session is handed only the human role, that a prompt
    survives cmd's second pass at the line, and above all that every failure falls
    back to the built-in window. A question you cannot answer is worse than an ugly one.
    """
    print("\n--- running ask-session: launch shape, cmd quoting, fallbacks ---\n")
    from parley.client_server import ask_session as ask
    from parley.client_server.config import load_desktop

    # `ask_via` is gone: questions always open in the window, with "Open in Claude"
    # per card. A cfg from an older install that still carries it must load cleanly.
    path = os.path.join(tempfile.mkdtemp(prefix="parley_cfg_"), "cfg.yaml")
    with open(path, "w", encoding="utf-8") as f:
        f.write("desktop:\n  ask_via: claude\n  hotkey: ctrl+alt+p\n")
    desktop = load_desktop(path)
    assert "ask_via" not in desktop, "FAIL: a leftover ask_via must be ignored"
    assert desktop["hotkey"] == "ctrl+alt+p", "FAIL: the rest of desktop: still applies"
    real_which, ask.shutil.which = ask.shutil.which, lambda _name: None
    try:
        assert not ask.available(), \
            "FAIL: no CLI means no 'Open in Claude' button, not one that can only fail"
    finally:
        ask.shutil.which = real_which

    cfg = ask.mcp_config("http://127.0.0.1:9338/")
    args = cfg["mcpServers"]["parley"]["args"]
    assert args[-2:] == ["--role", "human"], f"FAIL: the session gets the human role: {args}"
    assert "http://127.0.0.1:9338" in args, "FAIL: the base URL must lose its trailing slash"
    assert list(cfg["mcpServers"]) == ["parley"], \
        "FAIL: exactly one server — with --strict-mcp-config that is what keeps the " \
        "user's own MCP servers out of a session about their calendar"

    # the prompt has to survive being re-read by cmd from inside a quoted argument
    rough = 'Line one\nline two 100% "quoted" and an em-dash — here'
    safe = ask.cmd_safe(rough)
    assert "\n" not in safe, "FAIL: a newline ends the .cmd line, truncating the prompt"
    assert '"' not in safe, "FAIL: an inner quote would end the argument early"
    assert "%%" in safe, "FAIL: a bare % is expanded by cmd and vanishes silently"
    assert safe.isascii(), "FAIL: cmd reads the script in the OEM codepage"
    assert ask.cmd_safe("a" * 3).startswith("aaa")

    prompt = ask.seed_prompt("outlook_classic2", [{"negotiation_id": "mtg-1"}])
    assert "outlook_classic2" in prompt and "1 waiting" in prompt, "FAIL: prompt content"
    assert "pending" in prompt and "answer" in prompt, "FAIL: the loop must be spelled out"
    assert "never deciding for them" in prompt, \
        "FAIL: the session relays a decision, it does not make one"
    assert "mtg-1" not in prompt, "FAIL: no focus given, so no question is singled out"

    # The card's button: the session starts on THAT question, yet still sees the rest,
    # because people answer "and the Tuesday one?" in the same breath.
    waiting = [{"negotiation_id": "mtg-a", "from": "Joan", "title": "About Parley"},
               {"negotiation_id": "mtg-b", "title": "Budget"}]
    focused = ask.seed_prompt("agent", waiting, focus=waiting[0])
    assert "2 waiting" in focused, "FAIL: the session must know the others exist"
    assert "mtg-a" in focused and "About Parley" in focused, \
        "FAIL: the clicked question must be where the session starts"
    assert "mtg-b" not in focused, "FAIL: only the clicked question is singled out"
    assert "one `answer` per question" in focused, \
        "FAIL: answers to the others must go back one by one, so each leaves the pile"

    # a real launch, with the spawn itself stubbed out
    spawned = []
    clicked = {"negotiation_id": "mtg-click", "title": "Sync"}
    ok, script = ask.launch("test agent", "http://127.0.0.1:9999", [clicked],
                            run=lambda argv: spawned.append(argv),
                            directory=tempfile.mkdtemp(prefix="parley_ask_"), focus=clicked)
    if sys.platform == "win32":
        assert ok, f"FAIL: launch should succeed: {script}"
        assert spawned and spawned[0][0] == "cmd", f"FAIL: unexpected argv {spawned}"
        body = open(script, encoding="ascii").read()
        assert "--strict-mcp-config" in body, "FAIL: other MCP servers must stay out"
        for tool in ask.HUMAN_TOOLS:
            assert f"mcp__parley__{tool}" in body, f"FAIL: {tool} must be allowed"
        for verb in ("send", "apply", "close", "ask_user"):
            assert f"mcp__parley__{verb}" not in body, \
                f"FAIL: {verb} must not even be offered to a consulting session"
        assert "open-test-agent.cmd" in script, "FAIL: the agent name must be filename-safe"
        # The prompt must come BEFORE the variadic options. Live 2026-09-19: placed
        # after --allowedTools it was read as one more tool name, so the session
        # opened with nothing to do and no visible error at all.
        # by --allowedTools, not by "claude": the banner line mentions Claude too
        line = next(l for l in body.splitlines() if "--allowedTools" in l)
        prompt_at = line.find("You are helping")
        assert prompt_at != -1, "FAIL: no prompt in the launcher"
        assert "mtg-click" in line, "FAIL: the clicked question must reach the session"
        for variadic in ("--allowedTools", "--mcp-config"):
            assert line.find(variadic) > prompt_at, \
                f"FAIL: {variadic} takes a LIST — a prompt after it is swallowed as " \
                f"another value and the session starts empty"
    else:
        assert not ok and "Windows" in script, "FAIL: say so rather than pretend"

    # no CLI -> a clean refusal the caller can fall back from
    real_which, ask.shutil.which = ask.shutil.which, lambda _name: None
    try:
        ok, detail = ask.launch("a", "http://x", run=lambda argv: None,
                                directory=tempfile.mkdtemp(prefix="parley_ask_"))
    finally:
        ask.shutil.which = real_which
    assert not ok and "PATH" in detail, f"FAIL: expected a clean refusal, got {detail!r}"
    print("ASK-SESSION SELFTEST PASSED ✅ — human role only, starts on the clicked question, "
          "prompt survives cmd, no CLI means no button.")


def _selftest_skill_install():
    """Parley's Claude Code skills ship with the package and install into Claude Code.

    Four things are worth locking down. That the shipped copies and the ones in this
    repo's own .claude/skills cannot drift: they are two trees by necessity (package
    data must live inside the package; Claude Code reads the repo one when you work
    here) and nothing but a test stops them diverging. That uninstalling never takes
    anything that is not ours, and installing never overwrites a skill of the user's
    own that happens to share a name. And — the repo is public — that no shipped skill
    carries a real person's address.
    """
    import shutil
    print("\n--- running skill-install: ships with the package, installs, uninstalls ---\n")
    from parley.install_config import skill_install as si

    documents = {"parley": ["parley --pending", "parley --negotiate"],
                 "myorganizer": ["~/.parley/myorganizer", "references/onboarding.md"]}
    assert set(si.SKILLS) == set(documents), "FAIL: a new skill needs its checks here"
    email = re.compile(r"[\w.+-]+@(?:[\w-]+\.)+[a-z]{2,}", re.I)
    placeholder_domains = {"example.com", "example.org", "example.net", "acme.com"}
    for name in si.SKILLS:
        shipped = si.source_path(name)
        assert os.path.exists(shipped), f"FAIL: the skill must ship with the package: {shipped}"
        with open(shipped, encoding="utf-8") as f:
            body = f.read()
        assert body.lstrip().startswith("---") and f"name: {name}" in body, \
            f"FAIL: /{name} needs its frontmatter or Claude Code will not load it"
        for needle in documents[name]:
            assert needle in body, f"FAIL: /{name} is missing what it documents: {needle}"

        repo_dir = os.path.join(os.getcwd(), ".claude", "skills", name)
        for rel in si.shipped_files(name):
            with open(os.path.join(si.source_dir(name), *rel.split("/")),
                      encoding="utf-8") as f:
                text = f.read()
            for m in email.finditer(text):
                assert m.group(0).lower().split("@")[1] in placeholder_domains, \
                    f"FAIL: /{name}/{rel} ships a real address ({m.group(0)}) — the repo " \
                    f"is public; use example.com"
            if os.path.isdir(repo_dir):  # only in a checkout, not in a wheel install
                repo_copy = os.path.join(repo_dir, *rel.split("/"))
                assert os.path.exists(repo_copy), \
                    f"FAIL: .claude/skills/{name}/{rel} is missing " \
                    f"(src/parley/skills/{name}/ is canonical — copy it over)"
                with open(repo_copy, encoding="utf-8") as f:
                    assert f.read() == text, \
                        f"FAIL: .claude/skills/{name}/{rel} has drifted from the shipped " \
                        f"copy (src/parley/skills/{name}/ is canonical — copy it over)"

    # install / status / uninstall against a fake home
    home = tempfile.mkdtemp(prefix="parley_claude_")
    real_home, si.claude_home = si.claude_home, lambda: home
    try:
        assert si.status()[:2] == (False, False), "FAIL: nothing installed yet"
        ok, detail = si.install()
        assert ok, f"FAIL: install: {detail}"
        for name in si.SKILLS:
            for rel in si.shipped_files(name):
                assert os.path.exists(os.path.join(si.target_dir(name), *rel.split("/"))), \
                    f"FAIL: /{name} installed without {rel} — a skill is a whole folder"
        assert si.status()[:2] == (True, True), "FAIL: should report installed + current"

        stale = os.path.join(si.target_dir("myorganizer"), "references", "sources.md")
        with open(stale, "w", encoding="utf-8") as f:
            f.write("stale")
        assert si.status("myorganizer")[:2] == (True, False), \
            "FAIL: status compares the CONTENT of every file — an upgraded Parley must " \
            "spot a stale reference, not just a stale SKILL.md"
        assert si.install()[0] and si.status()[:2] == (True, True), \
            "FAIL: installing over an existing copy must refresh it"

        # a file of the user's own beside ours is kept, and said so
        mine = os.path.join(si.target_dir("parley"), "notes.md")
        with open(mine, "w", encoding="utf-8") as f:
            f.write("my own notes")
        ok, detail = si.uninstall("parley")
        assert ok and os.path.exists(mine), \
            "FAIL: uninstall must never take a file we did not put there"
        assert not os.path.exists(si.target_path("parley")), "FAIL: ours should be gone"
        assert "yours" in detail, f"FAIL: say what was kept, got {detail!r}"
        os.remove(mine)
        assert si.uninstall()[0], "FAIL: uninstalling every skill"
        for name in si.SKILLS:
            assert not os.path.isdir(si.target_dir(name)), \
                f"FAIL: with nothing of theirs left, /{name}'s directory goes"
        assert si.uninstall()[0], "FAIL: uninstalling twice must be a no-op, not an error"

        # a skill of the user's own with our name is never overwritten nor removed
        own = si.target_path("myorganizer")
        os.makedirs(os.path.dirname(own))
        with open(own, "w", encoding="utf-8") as f:
            f.write("---\nname: myorganizer\n---\nmine")
        ok, detail = si.install("myorganizer")
        assert not ok and "your own" in detail, \
            f"FAIL: must refuse to overwrite the user's own skill, got {detail!r}"
        assert si.uninstall("myorganizer")[0] and os.path.exists(own), \
            "FAIL: uninstall must leave a skill it did not install"
        with open(own, encoding="utf-8") as f:
            assert f.read().endswith("mine"), "FAIL: the user's own skill was touched"
        shutil.rmtree(si.target_dir("myorganizer"))

        # an old /parley install (before the manifest) is still ours to upgrade
        legacy = si.target_path("parley")
        os.makedirs(os.path.dirname(legacy))
        with open(legacy, "w", encoding="utf-8") as f:
            f.write("old version")
        assert si.install("parley")[0] and si.status("parley")[:2] == (True, True), \
            "FAIL: a pre-manifest /parley install must still upgrade"
    finally:
        si.claude_home = real_home
    _selftest_objectives_reader()
    print("SKILL-INSTALL SELFTEST PASSED ✅ — both skills ship whole, install, refresh, "
          "never touch the user's own, and carry no real address.")


def _selftest_objectives_reader():
    """The /myorganizer objectives reader takes the layout from the profile, not the
    code. Skipped when openpyxl is absent — it is the optional `organizer` extra."""
    try:
        import openpyxl
    except ImportError:
        print("objectives reader: openpyxl not installed (parley[organizer]) — skipped")
        return
    import importlib.util
    from parley.install_config import skill_install as si
    path = os.path.join(si.source_dir("myorganizer"), "scripts", "objectives.py")
    spec = importlib.util.spec_from_file_location("parley_objectives", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Signed"
    rows = [("Objectives 2026", None, None),         # a loose title: NOT the anchor
            ("OBJECTIVES 2026", "WEIGHT", None),     # the table's header
            ("Delivery", 0.6, 0.5),
            ("Ship the thing on time and in budget.", 1, "done in March"),
            ("Sales", 0.4, None),
            ("Two new accounts signed this year.", 0.5, None),
            ("Help close the renewal of the main account.", 0.5, None),
            ("TOTAL", 1, None),
            ("Stray", 0.3, None)]                    # past the end marker
    for i, row in enumerate(rows, start=1):
        for col, value in zip("BVW", row):
            ws[f"{col}{i}"] = value
    wb.copy_worksheet(ws).title = "2026-04"
    xlsx = os.path.join(tempfile.mkdtemp(prefix="parley_obj_"), "objectives.xlsx")
    wb.save(xlsx)

    source = {"kind": "excel", "path": xlsx, "sheet": "Signed",
              "review_sheets": r"\d{4}-\d{2}",
              "columns": {"text": "B", "weight": "V", "extra": "W"},
              "start_marker": "OBJECTIVES {year}", "start_weight": "WEIGHT",
              "end_marker": "TOTAL"}
    data = mod.read(source, 2026)
    cats = data["categories"]
    assert [(c["name"], c["weight"]) for c in cats] == [("Delivery", 0.6), ("Sales", 0.4)], \
        f"FAIL: categories come from the profile's layout, got {cats}"
    assert cats[0]["objectives"][0]["comment"] == "done in March", \
        "FAIL: the extra column is the objective's comment"
    assert len(cats[1]["objectives"]) == 2, "FAIL: objectives belong to their category"
    assert list(data["reviews"]) == ["2026-04"], "FAIL: review sheets by the profile's regex"
    print("objectives reader: layout from the profile, header told from a loose title ✅")


def _selftest_session_supervisor():
    """What the supervisor asks for, without asking for it: argv, models, backoff."""
    print("\n--- running session-supervisor: argv, model split, backoff on a fast death ---\n")
    from types import SimpleNamespace
    from parley.agent import session_supervisor as sup_mod
    from parley.agent.brain import TOOLS
    bus = MemoryBus()
    agent = Agent([(TestAccount("gmail@example.com", bus), {})], name="sup",
                  llm=SimpleNamespace(driver="session", model="brain-model",
                                      gateway_model="small-model", owner="Alex"))
    sup = sup_mod.SessionSupervisor(agent, "http://127.0.0.1:8787/",
                                    roles=("brain", "filter"))
    assert sup.base_url == "http://127.0.0.1:8787", "FAIL: the base URL must be normalised"
    assert sup._model_for("brain") == "brain-model", "FAIL: the brain runs on llm: model"
    assert sup._model_for("filter") == "small-model", \
        "FAIL: the filter runs on llm-gateway: model — it wakes far more often"

    path = sup._write_mcp_config()
    try:
        with open(path, encoding="utf-8") as f:
            servers = json.load(f)["mcpServers"]
        assert set(servers) == {"parley-brain", "parley-filter"}, \
            f"FAIL: one MCP server per role, got {sorted(servers)}"
        assert "--role" in servers["parley-brain"]["args"], "FAIL: the role must be passed"
    finally:
        os.unlink(path)

    sup._config_path = path
    argv = sup._argv("claude.cmd", "brain")
    assert argv[:3] == ["cmd", "/c", "claude.cmd"], \
        "FAIL: a .cmd shim cannot be exec'd directly on Windows"
    assert "--strict-mcp-config" in argv, \
        "FAIL: without strict MCP config the user's own servers leak into the agent"
    allowed = argv[argv.index("--allowedTools") + 1]
    for tool in ["wait_for_work"] + [t.name for t in TOOLS]:
        assert f"mcp__parley-brain__{tool}" in allowed, f"FAIL: {tool} must be allowed"
    disallowed = argv[argv.index("--disallowedTools") + 1]
    assert "Bash" in disallowed and "Write" in disallowed, \
        "FAIL: the session reasons and calls Parley's verbs; it does not touch the disk"
    prompt = argv[argv.index("-p") + 1]
    assert "wait_for_work" in prompt and "Alex" in prompt and "20 turns" in prompt, \
        "FAIL: the loop prompt must name the loop, the owner and the recycle point"

    # A session that dies instantly must back off — relaunching it hot would turn one
    # problem (bad quota, bad model) into a rate-limit ban.
    waits, starts = [], []

    def _instant_death(exe, role):
        starts.append(role)
        if len(starts) >= 4:
            sup._stop.set()
        return 1, "boom"

    sup._stop.wait = lambda seconds: waits.append(seconds)
    sup._run_once = _instant_death
    sup._keep_alive("claude.cmd", "brain")
    assert waits[:3] == [5, 10, 20], f"FAIL: backoff must double on fast deaths, got {waits}"
    assert len(starts) == 4, f"FAIL: the supervisor must keep trying, got {len(starts)}"

    # A session we spawn must not inherit the session that spawned Parley. Seen live
    # 2026-09-19: the window announced "Transcript saving is off — inherited
    # CLAUDE_CODE_CHILD_SESSION", and among the ten variables that came across were a
    # messaging socket and token pointing back into the parent's session.
    real = dict(os.environ)
    os.environ.update({"CLAUDE_CODE_CHILD_SESSION": "1", "CLAUDECODE": "1",
                       "CLAUDE_CODE_MESSAGING_TOKEN": "secret", "PARLEY_KEEP": "yes"})
    try:
        env = sup_mod.child_env()
    finally:
        os.environ.clear()
        os.environ.update(real)
    assert not [k for k in env if k.upper().startswith("CLAUDE")], \
        "FAIL: every CLAUDE* marker must be dropped — one of them is a live channel " \
        "back into the parent session"
    assert env.get("PARLEY_KEEP") == "yes", "FAIL: the rest of the environment stays"

    assert sup_mod._summarize('{"result": "done", "total_cost_usd": 0.04}', "")["cost_usd"] == 0.04, \
        "FAIL: the journal should record what a session cost"
    assert "not json" in str(sup_mod._summarize("not json", "")), \
        "FAIL: unparseable output must still be recorded, not dropped"
    print("SESSION-SUPERVISOR SELFTEST PASSED ✅ — one server per role, own tools refused, "
          "backoff doubles.")


def _selftest_mcp_server():
    """The stdio bridge speaks MCP and never lets an empty wait reach the model."""
    print("\n--- running mcp-server: JSON-RPC surface, verbs shared with the brain ---\n")
    import io as _io
    from parley.agent.brain import TOOLS
    from parley.mcp_server import serve

    class _FakeBridge:
        role = "brain"

        def __init__(self):
            self.done = []
            self.calls = []

        def wait(self, done_because):
            self.done.append(done_because)
            return {"turn_id": "w-1", "kind": "inbound", "account": "a@b.com",
                    "system": "POLICY", "event": "peer wrote", "attempt": 2}

        def tool(self, name, arguments):
            self.calls.append((name, arguments))
            return {"ok": True, "busy": []}

    bridge = _FakeBridge()
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},   # no id: no answer
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "wait_for_work", "arguments": {"done_because": "sent a reply"}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
         "params": {"name": "check", "arguments": {"start": "x", "end": "y"}}},
        {"jsonrpc": "2.0", "id": 5, "method": "nonsense"},
    ]
    out = _io.StringIO()
    serve(bridge, stdin=iter(json.dumps(r) + "\n" for r in requests), stdout=out)
    answers = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
    by_id = {a["id"]: a for a in answers}
    assert set(by_id) == {1, 2, 3, 4, 5}, \
        f"FAIL: a notification must get no answer; got ids {sorted(by_id)}"

    names = [t["name"] for t in by_id[2]["result"]["tools"]]
    assert names[0] == "wait_for_work", "FAIL: wait_for_work must be offered"
    assert {t.name for t in TOOLS}.issubset(set(names)), \
        "FAIL: the session must be offered the SAME verbs as the in-process brain"

    turn_text = by_id[3]["result"]["content"][0]["text"]
    assert "POLICY" in turn_text and "peer wrote" in turn_text, \
        "FAIL: a turn must reach the model with its context and its event"
    assert "REDELIVERED" in turn_text, \
        "FAIL: a redelivered turn must say so — it may have been half-done already"
    assert bridge.done == ["sent a reply"], \
        "FAIL: done_because must reach the daemon, or the journal cannot tell concluded "\
        "from stuck"
    assert bridge.calls == [("check", {"start": "x", "end": "y"})], \
        "FAIL: a verb call must be forwarded verbatim"
    assert by_id[5].get("error"), "FAIL: an unknown method must answer with an error"
    print("MCP-SERVER SELFTEST PASSED ✅ — initialize/list/call; one tool definition, "
          "two drivers.")


def _selftest_claude_code_adapter():
    """The `claude -p` adapter's pure mappers — no subprocess, no network, no login.

    What must hold: the model's answer survives however it is wrapped; ids get minted
    (this wire carries none) so the brain can correlate results inside a turn; a
    non-JSON answer is a finished turn, not a crash; and a session is resumed ONLY
    when the transcript extends one we already answered under the SAME system prompt —
    resuming across a changed system prompt would silently reuse a stale "now"."""
    print("\n--- running claude-code adapter: answer parsing, minted ids, resume safety ---\n")
    from parley.agent.llm_port import ClaudeCodeClient as CC, Tool, build_llm_client

    obj = '{"text": "t", "tool_calls": [{"name": "check", "input": {"start": "s"}}]}'
    for wrapped in (obj, "```json\n" + obj + "\n```", "Sure! " + obj + " — done."):
        assert CC._extract_json(wrapped) is not None, \
            "FAIL: the answer object must be found bare, fenced, or wrapped in prose"
    assert CC._extract_json('{"text": "he said \\"hi {x}\\"", "tool_calls": []}') is not None, \
        "FAIL: braces and quotes inside a string must not end the object early"
    assert CC._extract_json("no object here") is None, "FAIL: no object must read as None"

    r = CC._parse_result('{"text": "t", "tool_calls": [{"name": "check", "input": {"a": 1}},'
                         ' {"name": "send", "input": "{\\"to\\": \\"x\\"}"}]}')
    assert [c.id for c in r.tool_calls] == ["cc-1", "cc-2"], \
        "FAIL: this wire has no tool ids — the adapter must mint correlatable ones"
    assert r.tool_calls[1].input == {"to": "x"}, \
        "FAIL: input arriving as a JSON string must be coerced to a dict"
    assert not r.done and r.stop_reason == "tool_use", "FAIL: tool calls mean not done"
    assert CC._parse_result('{"text": "all set", "tool_calls": []}').done, \
        "FAIL: an empty tool_calls list is a finished turn"
    plain = CC._parse_result("I could not produce JSON.")
    assert plain.done and plain.stop_reason == "text", \
        "FAIL: a non-JSON answer must end the turn as text, never crash it"

    turns = [{"role": "user", "content": "an event"},
             {"role": "assistant", "text": "thinking",
              "tool_calls": [ToolCall("cc-1", "check", {"a": 1})]},
             {"role": "tool", "tool_call_id": "cc-1", "content": '{"free": true}'}]
    rendered = CC._render(turns)
    assert "You called check(" in rendered and "Result of check:" in rendered, \
        "FAIL: a tool result must be labelled with the tool it answers (no structural block here)"
    prompt = CC._system_prompt("POLICY", [Tool("check", "look", {"type": "object"})])
    assert "POLICY" in prompt and "ONE JSON object" in prompt and '"check"' in prompt, \
        "FAIL: the system prompt must carry the policy, the contract and the schemas"

    c = CC(model="m")
    assert c._plan("SYS", turns) == ("", turns), "FAIL: with no anchor every call is fresh"
    c._last = {"system": "SYS", "turns": 2, "session": "sid-1"}
    assert c._plan("SYS", turns) == ("sid-1", turns[2:]), \
        "FAIL: an extended transcript must resume and send only the new turns"
    assert c._plan("SYS CHANGED", turns) == ("", turns), \
        "FAIL: a changed system prompt must start fresh — a resumed session keeps the old 'now'"
    c._last = {"system": "SYS", "turns": 3, "session": "sid-1"}
    assert c._plan("SYS", turns) == ("", turns), "FAIL: nothing new to send must not resume"

    assert type(build_llm_client("claude-code", model="m")).__name__ == "ClaudeCodeClient", \
        "FAIL: the factory must route provider: claude-code"
    try:
        CC(model="")
    except ValueError:
        pass
    else:
        raise AssertionError("FAIL: no model must be an error, never a silent default")
    print("CLAUDE-CODE ADAPTER SELFTEST PASSED ✅ — parsing, minted ids, resume only on a "
          "frozen system prompt.")


def _selftest_model_alias():
    """`model: opus` means the newest Opus — resolved, never guessed.

    What must hold: an alias picks the FIRST family match of a newest-first listing
    (old-style ids like claude-3-opus-... count too); a concrete id passes through
    untouched; a family the account cannot use, or a listing that fails, is a loud
    error naming the alias; the cfg folds an alias to lower case and leaves a real id
    alone; and the journal records which concrete model the role really runs."""
    print("\n--- running model alias: newest of the family, loud on no match ---\n")
    from types import SimpleNamespace
    from parley.agent.llm_port import AnthropicClient, resolve_model_alias
    from parley.client_server.config import load_llm

    listing = ["claude-sonnet-5-5", "claude-opus-5-5", "claude-haiku-5-5",
               "claude-opus-4-8", "claude-3-opus-20240229"]
    assert resolve_model_alias("opus", listing) == "claude-opus-5-5",         "FAIL: an alias must pick the newest (first-listed) model of its family"
    assert resolve_model_alias("Haiku", listing) == "claude-haiku-5-5",         "FAIL: the alias match must not care about case"
    assert resolve_model_alias("opus", ["claude-3-opus-20240229"]) == "claude-3-opus-20240229",         "FAIL: old-style ids (family after the version) are still that family"
    assert resolve_model_alias("claude-opus-4-8", []) == "claude-opus-4-8",         "FAIL: a concrete id must pass through without a listing"
    try:
        resolve_model_alias("opus", ["claude-sonnet-5-5"])
    except ValueError as err:
        assert "opus" in str(err), "FAIL: the error must name the alias"
    else:
        raise AssertionError("FAIL: no family match must be an error, never another model")

    def fake(models=None, error=None):
        def _list():
            if error:
                raise error
            return [SimpleNamespace(id=m) for m in models]
        client = AnthropicClient.__new__(AnthropicClient)  # no SDK, no network
        client._client = SimpleNamespace(models=SimpleNamespace(list=_list))
        return client
    assert fake(listing)._resolve("sonnet") == "claude-sonnet-5-5",         "FAIL: the API client must resolve through the account's own listing"
    try:
        fake(error=OSError("offline"))._resolve("opus")
    except RuntimeError as err:
        assert "opus" in str(err) and "offline" in str(err),             "FAIL: a failed listing must say which alias and why"
    else:
        raise AssertionError("FAIL: a failed listing must be an error, never a fallback")

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "cfg.yaml")
        with open(path, "w", encoding="utf-8") as f:
            f.write('llm:\n  provider: claude-code\n  model: "Opus"\n'
                    'llm-gateway:\n  model: "claude-Haiku-4-5"\n')
        cfg = load_llm(path)
    assert cfg.model == "opus", "FAIL: the cfg must fold an alias to lower case"
    assert cfg.gateway_model == "claude-Haiku-4-5", "FAIL: a concrete id is kept as written"

    agent = Agent([(TestAccount("alias@example.com", MemoryBus()), {})], name="alias")
    agent._log_model("brain", "anthropic", "opus", SimpleNamespace(model="claude-opus-5-5"))
    rec = [r for r in agent.log.records if r["kind"] == "llm_model"][-1]
    assert (rec["configured"], rec["model"]) == ("opus", "claude-opus-5-5"),         "FAIL: the journal must record the concrete model the alias resolved to"
    print("MODEL ALIAS SELFTEST PASSED ✅ — newest of the family, concrete ids untouched, "
          "loud on no match.")


def _selftest_setup_engine():
    """The wizard's engine step, driven offline through the frontend seam.

    What must hold: with the Claude Code CLI on PATH the wizard offers it, and
    picking it writes `provider: claude-code` with NO key and never asks for one;
    the gateway can run a model through the same CLI or none at all (keyword list);
    every model is proven live (here: a stubbed ping) before it is written; and with
    no CLI the key path is unchanged, aliases offered first. Each cfg must load
    through the real loader."""
    print("\n--- running setup engine: Claude Code without a key, gateway choices, key path ---\n")
    import shutil
    from parley.agent.llm_port import ClaudeCodeClient
    from parley.client_server.config import load_llm
    from parley.install_config import console, install_config, steps

    class Scripted:
        """A frontend that answers from a {prompt-substring: answer} script."""
        def __init__(self, choices, allow_secret=False):
            self.choices, self.allow_secret, self.asked = choices, allow_secret, []
        def say(self, text=""):
            pass
        def ask(self, prompt, default=None):
            return "selftest" if "name" in prompt.lower() and "agent" in prompt.lower() \
                else (default if default is not None else "x")
        def ask_secret(self, prompt):
            assert self.allow_secret, f"FAIL: the keyless engine must never ask a secret ({prompt})"
            return "sk-ant-selftest-key"
        def ask_yn(self, prompt, default=False, yes_label="Yes", no_label="No"):
            return False
        def ask_choice(self, prompt, options, other=None, default=None):
            self.asked.append(prompt)
            for needle, answer in self.choices.items():
                if needle in prompt:
                    assert answer in [v for v, _ in options], \
                        f"FAIL: {answer!r} must be offered for {prompt!r}"
                    return answer
            return "skip" if any(v == "skip" for v, _ in options) else options[0][0]

    pinged = []
    saved = (shutil.which, ClaudeCodeClient.ping, steps._ping, steps._live_models,
             install_config.PARLEY_HOME, install_config.autostart_add,
             install_config.scaffold_home)

    def run(choices, cli=True, allow_secret=False, keep=False):
        shutil.which = (lambda name, *a, **k: "C:/fake/claude.exe" if cli else None)
        frontend = Scripted(choices, allow_secret)
        console.set_frontend(frontend)
        try:
            install_config.run_setup("selftest", console_only=True)
        finally:
            console.set_frontend(None)
        path = os.path.join(install_config.PARLEY_HOME, "cfg_selftest.yaml")
        if keep:
            return None
        with open(path, encoding="utf-8") as f:
            text = f.read()
        os.remove(path)
        cfg_path = path + ".check.yaml"
        with open(cfg_path, "w", encoding="utf-8") as f:
            f.write(text)
        return text, load_llm(cfg_path), frontend

    try:
        ClaudeCodeClient.ping = lambda self, timeout=120: (pinged.append(self.model)
                                                            or f"claude-{self.model}-9-9")
        steps._ping = lambda key, model: None
        steps._live_models = lambda key, limit=12: []
        install_config.autostart_add = lambda name: None
        install_config.scaffold_home = lambda: None
        with tempfile.TemporaryDirectory() as home:
            install_config.PARLEY_HOME = home

            text, cfg, ui = run({"engine": "claude-code", "brain": "opus",
                                 "Gateway model": "haiku"})
            assert "Which engine" in ui.asked[0] or any("Which engine" in a for a in ui.asked), \
                "FAIL: with the CLI on PATH the wizard must ask which engine"
            assert cfg.provider == "claude-code" and cfg.model == "opus", \
                "FAIL: Claude Code must be written as provider: claude-code + the alias"
            assert "anthropic_key" not in text and not cfg.anthropic_key, \
                "FAIL: the keyless engine must write no key at all"
            assert cfg.gateway_model == "haiku", "FAIL: the gateway may run Claude via the CLI"
            assert pinged == ["opus", "haiku"], \
                "FAIL: brain and gateway models must each be proven before they are written"

            pinged.clear()
            text, cfg, _ = run({"engine": "claude-code", "brain": "sonnet",
                                "Gateway model": "none"})
            assert cfg.gateway_model == "" and "\nllm-gateway:" not in text, \
                "FAIL: 'none' must leave the gateway to the keyword list"
            assert pinged == ["sonnet"], "FAIL: no gateway model means no gateway ping"

            text, cfg, ui = run({"brain": "opus", "Gateway model": "haiku"},
                                cli=False, allow_secret=True)
            assert not any("Which engine" in a for a in ui.asked), \
                "FAIL: without the CLI there is no engine to choose"
            assert cfg.anthropic_key == "sk-ant-selftest-key" and cfg.model == "opus" \
                and not cfg.provider, "FAIL: the key path must write the key + the alias"

            # The wizard on an EXISTING cfg: change only the engine, keep the rest.
            path = os.path.join(home, "cfg_selftest.yaml")
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(text + "# hand edit\n")
            run({"do with it": "engine", "engine": "claude-code", "brain": "opus",
                 "Gateway model": "none"}, keep=True)
            with open(path, encoding="utf-8") as f:
                changed = f.read()
            assert "provider: claude-code" in changed and "sk-ant-selftest-key" not in changed \
                and changed.endswith("# hand edit\n"), \
                "FAIL: 'change only the engine' must swap the engine and keep the rest"
    finally:
        (shutil.which, ClaudeCodeClient.ping, steps._ping, steps._live_models,
         install_config.PARLEY_HOME, install_config.autostart_add,
         install_config.scaffold_home) = saved
    print("SETUP ENGINE SELFTEST PASSED ✅ — Claude Code writes no key and asks none; "
          "gateway via the CLI or keywords; key path unchanged.")


def _selftest_cfg_edit():
    """Changing the engine edits TEXT, and only the engine.

    What must hold: switching to Claude Code drops the key and writes the provider;
    a non-engine key in `llm:` (tz) and every teaching comment survive; everything
    outside the two blocks is byte-identical; a gateway switched off becomes the
    commented hint and comes back from it; the edit is idempotent and a round trip
    returns the same text; and write_llm keeps a .bak and leaves the original alone
    when the result would not load."""
    print("\n--- running cfg edit: engine-only, comments kept, round trip, safe write ---\n")
    from importlib import resources
    import yaml
    from parley.install_config.cfg_edit import set_llm, write_llm

    base = resources.files("parley").joinpath(
        "config_templates", "cfg.gmail.example.yaml").read_text(encoding="utf-8")
    base = base.replace("llm:\n", 'llm:\n  tz: "Europe/Madrid"\n', 1)
    cc = {"provider": "claude-code", "anthropic_key": "", "model": "opus",
          "gateway_model": "haiku", "gateway_key": ""}
    off = dict(cc, gateway_model="")
    api = {"provider": "anthropic", "anthropic_key": "sk-ant-new", "model": "sonnet",
           "gateway_model": "haiku", "gateway_key": ""}

    def outside(text):
        return text[text.index("\naccount:"):]

    text = set_llm(base, cc)
    data = yaml.safe_load(text)
    assert data["llm"]["provider"] == "claude-code" and "anthropic_key" not in data["llm"], \
        "FAIL: Claude Code must write the provider and drop the key"
    assert data["llm"]["tz"] == "Europe/Madrid", "FAIL: a non-engine llm: key must survive"
    assert "OR a LOCAL brain via Ollama" in text, "FAIL: teaching comments must survive"
    assert outside(text) == outside(base), "FAIL: lines outside the blocks must not change"
    assert set_llm(text, cc) == text, "FAIL: the edit must be idempotent"
    assert set_llm(set_llm(text, api), cc) == text, "FAIL: a round trip must return the same text"
    assert yaml.safe_load(set_llm(text, api))["llm"]["anthropic_key"] == "sk-ant-new", \
        "FAIL: the key path must write the key"

    none = set_llm(text, off)
    assert "llm-gateway" not in yaml.safe_load(none) and '#     model: "haiku"' in none, \
        "FAIL: a gateway switched off must become the commented hint"
    assert yaml.safe_load(set_llm(none, cc))["llm-gateway"]["model"] == "haiku", \
        "FAIL: the gateway must come back from the hint"

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "cfg_x.yaml")
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(base)
        write_llm(path, cc)
        with open(path + ".bak", encoding="utf-8") as f:
            assert f.read() == base, "FAIL: write_llm must keep the previous file as .bak"
        with open(path, encoding="utf-8") as f:
            written = f.read()
        try:
            write_llm(path, dict(cc, model=""))
        except BaseException:  # noqa: BLE001 — the loader's SystemExit included
            pass
        else:
            raise AssertionError("FAIL: a cfg that does not load must not be written")
        with open(path, encoding="utf-8") as f:
            assert f.read() == written, "FAIL: a failed write must leave the cfg untouched"
        assert not os.path.exists(path + ".tmp"), "FAIL: a failed write must clean up"

        # What the tray's Settings window reads back from the same file.
        from parley.client_server.config import agent_settings
        write_llm(path, off)
        info = agent_settings(path)
        assert (info["engine"], info["model"], info["gateway"]) == \
            ("Claude Code (no API key)", "opus", "keyword list"), \
            "FAIL: Settings must show the engine, the brain model and 'keyword list'"
        assert info["connections"] and info["connections"][0].startswith("gmail"), \
            "FAIL: Settings must list the agent's connections"
        assert "error" in agent_settings(os.path.join(tmp, "missing.yaml")), \
            "FAIL: an unreadable cfg must come back as an error, not raise"
    print("CFG EDIT SELFTEST PASSED ✅ — engine-only edit, comments and the rest kept, "
          "safe write with .bak.")


def _selftest_gateway():
    print("\n--- running gateway: structured/ref admits; bulk/plain ignores; classifier fail-open ---\n")
    from parley.agent import gateway
    tagged = Message("g1", "peer@example.com", "hi", "let's talk (ref: mtg-xyz123)")
    assert gateway.route(tagged, {"negotiation_id": "mtg-xyz123"}, {}) == gateway.ADMIT, \
        "FAIL: a visible ref must always admit (replaces the old typed-payload fast path)"

    bulk = Message("g2", "no-reply@list.com", "newsletter", "buy now")
    assert gateway.route(bulk, {}, {}) == gateway.IGNORE, "FAIL: a bulk sender must be ignored"

    plain_meets = Message("g3", "human@example.com", "coffee?", "shall we meet on friday?")
    plain_junk = Message("g4", "human@example.com", "newsletter", "buy our stuff today")
    assert gateway.route(plain_meets, {}, {}) == gateway.ADMIT, \
        "FAIL: the lexical heuristic should admit obvious scheduling words"
    assert gateway.route(plain_junk, {}, {}) == gateway.IGNORE, \
        "FAIL: the lexical heuristic should ignore unrelated plain mail"

    def broken(subject, body):
        raise RuntimeError("classifier API down")

    assert gateway.route(plain_meets, {}, {}, classifier=broken) == gateway.ADMIT, \
        "FAIL: a classifier failure must fall open to the lexical heuristic (admit)"
    assert gateway.route(plain_junk, {}, {}, classifier=broken) == gateway.IGNORE, \
        "FAIL: a classifier failure must fall open to the lexical heuristic (ignore)"
    print("GATEWAY SELFTEST PASSED ✅ — visible-ref fast path; bulk/lexical; classifier fail-open.")
    return 0
