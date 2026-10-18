"""In-memory Account backend — a credential-free mailbox + calendar + contacts.

`TestAccount` (renamed from `MemoryAccount`, 2026-08-31 — collided with
`ShortTermMemory`, a completely different thing that happened to share the word
"Memory") implements the full Account surface over a shared `MemoryBus` (an
in-process address→inbox map), with an in-memory calendar and a tiny contacts
dict. It is a **first-class backend** (`backend = "memory"` in the registry,
kept for cfg back-compat), not a test-only fake: the offline `--selftest`
builds real agents over TestAccounts via the normal path, and `python -m parley
memory` spins one up as an in-process demo. No provider SDKs, no network, no
credentials.
"""

import uuid
from collections import defaultdict

from parley.connectors.accounts.account import Account
from parley.agent.log import _echo
from parley.core import Event, Message, as_addresses


class MemoryBus:
    """An in-memory mailbox map: each address → its pending messages. Accounts on
    the same bus deliver to each other exactly as real inboxes would."""

    def __init__(self):
        self.boxes = defaultdict(list)

    def deliver(self, to, message):
        self.boxes[to.lower()].append(message)


# One process-wide default bus so registry-built TestAccounts (each constructed
# from its own AccountConfig) still share an inbox space and can reach each other.
_DEFAULT_BUS = MemoryBus()


class TestAccount(Account):
    """Credential-free Account over a `MemoryBus`, with an in-memory calendar and
    a contacts dict. Two construction paths, both first-class:

      * ``TestAccount("me@example.com", bus)`` — explicit address + shared bus
        (the selftest builds several on one bus so they negotiate);
      * ``TestAccount(account_config)`` — from the backend registry
        (`make_account`); address comes from the config, bus is the process default,
        and `settings.contacts` (if any) seeds `find_contact`.
    """

    name = "memory"

    def __init__(self, addr=None, bus=None, contacts=None, tz=None):
        # Registry path: the first positional arg is an AccountConfig.
        if hasattr(addr, "backend"):
            account = addr
            addr = account.address or "me@memory.local"
            bus = _DEFAULT_BUS
            contacts = (account.settings or {}).get("contacts")
            tz = (account.settings or {}).get("tz")
        self.name = addr.split("@")[0]
        self._addr = addr
        self._bus = bus if bus is not None else MemoryBus()
        self._contacts = {str(k).lower(): v for k, v in (contacts or {}).items()}
        self._tz = tz or "UTC"
        self.calendar = []
        self._seq = 0
        # Test hook (iteration 20, Fault 3): fail this many upcoming must_rsvp calls with
        # InviteNotReady to simulate the provider's mail-before-event race.
        self.rsvp_not_ready = 0
        # Test spy (iteration 20, Issue 1): every respond_event, as {requester, must_rsvp}.
        self.accepted = []

    def connect(self):
        pass

    def address(self):
        return self._addr

    def timezone(self):
        return self._tz

    def find_contact(self, name):
        return self._contacts.get((name or "").strip().lower())

    def list_messages(self):
        box = self._bus.boxes[self._addr.lower()]
        out, box[:] = box[:], []
        return out

    def send_message(self, to, subject, body, thread=None):
        # One message, N recipients -- the same mail lands in each of their boxes
        # carrying the whole To: list, exactly as a real group email does, so a
        # selftest can tell a group message from a private one.
        addresses = as_addresses(to)
        _echo(f"   ✉️  {self._addr} → {', '.join(addresses)}: {subject}")
        mid = uuid.uuid4().hex
        chain = [m for m in (thread or []) if m]
        # A real client threads on In-Reply-To / References; this one records them so
        # the selftest can assert Parley replies onto a conversation instead of
        # starting a new one each turn.
        for addr in addresses:
            self._bus.deliver(addr, Message(mid, self._addr, subject, body,
                                            message_id=f"<{mid}@memory.local>",
                                            in_reply_to=chain[-1] if chain else None,
                                            references=" ".join(chain) or None,
                                            recipients=list(addresses)))

    def list_calendars(self):
        return []  # one calendar, nothing to choose — matches every real backend's default

    def list_events(self, window):
        return [e for e in self.calendar if e.slot.overlaps(window)]

    def update_event(self, id, changes):
        event = self._find_by_id(id)
        if event is None:
            raise LookupError(f"no event {id!r} on this calendar")
        if "start" in changes:
            event.start = changes["start"]
        if "end" in changes:
            event.end = changes["end"]
        if "show_as" in changes:
            event.show_as = changes["show_as"]
        if "title" in changes:
            event.title = changes["title"]

    def create_hold(self, title, slot, show_as="free"):
        self._seq += 1
        ev = Event(f"hold-{self._seq}", title, slot.start, slot.end, [self._addr],
                   organizer=self._addr, show_as=show_as, focus=True)
        self.calendar.append(ev)
        return ev

    def delete_event(self, id):
        self.calendar = [e for e in self.calendar if e.id != id]

    def _find_by_id(self, id):
        return next((e for e in self.calendar if e.id == id), None)

    def respond_event(self, response, *, id=None, requester=None, slot=None, title=None):
        """The test double for RSVP: the `rsvp_not_ready` hook simulates the
        invite-not-landed race (Fault 3), and a known `id` (or a match found by
        searching for `requester`+`slot`) RSVPs an existing invite event rather
        than duplicating it (Issue 1) — mirroring how real backends respond to
        the organizer's tentative event instead of creating a second,
        self-organized copy. `response` ("accepted"/"declined") is RECORDED on
        each entry so a test can tell an acceptance from a decline — the real
        backends have always honoured it (2026-09-06)."""
        must_rsvp = id is not None or requester is not None
        self.accepted.append({"requester": requester, "must_rsvp": must_rsvp,
                              "response": response})
        if must_rsvp and self.rsvp_not_ready > 0:
            from parley.connectors.accounts.account import InviteNotReady
            self.rsvp_not_ready -= 1
            raise InviteNotReady(f"the invite from {requester} isn't on the calendar yet (test)")
        if id is not None:
            existing = self._find_by_id(id)
            if existing is not None:
                return existing
        if requester is not None:
            req = requester.lower()
            for e in self.list_events(slot):
                # organizer OR attendee — mirrors the real backends (a mail-borne ICS
                # hold may carry the sender only as ORGANIZER; 2026-07-12 phase 4).
                if req == (e.organizer or "").lower() \
                        or any(req == (a or "").lower() for a in e.attendees):
                    return e  # RSVP the existing invite event — never a duplicate
        return self.create_event(title, slot, requester)

    def create_event(self, title, slot, attendee):
        self._seq += 1
        attendees = list(attendee) if isinstance(attendee, (list, tuple)) else [attendee]
        ev = Event(str(self._seq), title, slot.start, slot.end,
                   [self._addr] + attendees, organizer=self._addr,
                   raw={"uid": f"{self._addr}-{self._seq}@memory"})
        self.calendar.append(ev)
        return ev


# Back-compat alias — the registry/cfg still says `backend = "memory"`; nothing
# about the class NAME needs to travel into user-facing config.
MemoryAccount = TestAccount
