"""Account — the provider-agnostic I/O surface for one account.

An Account binds a single mailbox + calendar + contacts (Gmail, Outlook/Graph,
classic Outlook, Yahoo, an in-memory test double). It knows nothing about
negotiation, nothing about Parley: this is the reusable connector, meant to be
picked up wholesale by unrelated projects, not just Parley (2026-08-31 refactor —
see docs/iterations/refactor-plan.md "Package boundary"). An `Agent`
(parley.agent) drives one or more Accounts and supplies all Parley-specific
judgment and bookkeeping on top.

Concrete accounts live in parley/connectors/accounts/{gmail,outlook,yahoo,
local_outlook,test_account}.py.

Timezone handling lives HERE, not in the caller: any method that takes a time
accepts either a tz-aware datetime (already resolved) or a naive local
wall-clock datetime, which the Account interprets in its own `timezone()`
unless told otherwise. That's what makes the connector usable standalone (no
Parley agent required) and still get times right.
"""


class InviteNotReady(RuntimeError):
    """`respond_event(must_rsvp=True)` couldn't RSVP because the invite's event hasn't
    materialized on the calendar yet (the provider delivers the mail before the tentative
    event). A *transient* condition — the caller should defer and retry, never create a
    duplicate and never spin retrying in a tight loop."""


class Account:
    """Base class: each backend implements this small I/O surface.

    Subclasses MUST implement: connect, address, list_messages, send_message,
    list_events, create_event.
    """

    name = "account"

    # -- Lifecycle / identity -------------------------------------------------

    def connect(self):
        raise NotImplementedError

    def address(self):
        """This account's own bare email address."""
        raise NotImplementedError

    def timezone(self):
        """This calendar's IANA timezone (e.g. 'Europe/Madrid'), used to interpret
        naive local times passed to any method here. Default 'UTC'; real backends
        override to report the calendar's own zone. Best-effort — callers tolerate
        'UTC' on failure."""
        return "UTC"

    # -- Contacts -------------------------------------------------------------

    def find_contact(self, name):
        """Resolve a display name ("john") to an email address, or None if this
        account can't. Default: no address book. Real backends (Google People,
        Graph contacts, Outlook's resolver, CardDAV) and the test double override
        this."""
        return None

    # -- Mail -----------------------------------------------------------------

    def list_messages(self):
        """Return list[Message] that arrived since the last call. Each backend
        owns its own durable position (historyId, delta link, last-seen time,
        max UID, ...) — there is no `since` argument because the caller has
        nothing meaningful to supply that the backend doesn't already track
        better itself."""
        raise NotImplementedError

    def send_message(self, to, subject, body, thread=None):
        """Send ONE mail. `to` is one address or several (a list, or a string of
        comma/semicolon-separated addresses -- `core.as_addresses` normalises both):
        several recipients means one message addressed to all of them, never one
        message each, so every invitee can see who else was asked and reply to all.

        `thread` is the negotiation's Message-IDs, oldest first; a backend that can
        should send as a REPLY onto that chain. Ignoring it is allowed (the mail still
        arrives, just as its own conversation) - what is not allowed is pretending:
        say so in the backend, do not silently drop."""
        raise NotImplementedError

    # -- Calendar -------------------------------------------------------------

    def list_calendars(self):
        """This account's available calendars, as `[{"name": str, "id": str}]` —
        or `[]` when the backend has exactly one calendar and nothing to choose
        (most backends). Two callers share this: the setup wizard (offers a
        picker) and the runtime (asks, via UserMessaging, when more than one
        calendar exists and none is pinned in config — added 2026-08-31,
        generalizing what only the Yahoo backend used to do for itself)."""
        return []

    def select_calendar(self, calendar_id):
        """Pin `calendar_id` (one of `list_calendars()`'s ids) as the booking target
        for the rest of this process's life — the runtime ambiguous-calendar ask's
        answer applies here. Default: no-op, for backends where `list_calendars()`
        never returns more than one choice in the first place."""
        pass

    def list_events(self, window):
        """Return list[Event] on this calendar overlapping `window` (a Slot)."""
        raise NotImplementedError

    def create_event(self, title, slot, attendee):
        raise NotImplementedError

    def create_hold(self, title, slot, show_as="free"):
        """Put a focus block on this calendar: NO attendees, nothing sent, marked
        with `core.FOCUS_MARK` so it can be found again, shown as `show_as`
        ("free" | "tentative" | "busy"). Returns the Event (its `id` round-trips
        into update_event / delete_event). Backends that cannot mark an event do
        not implement it — a block we could not find again would be an orphan."""
        raise NotImplementedError(f"{self.name} cannot create focus blocks")

    def update_event(self, id, changes):
        """Rewrite an existing own event. `changes` is a dict of the fields to
        change: `start` / `end` (a move), and for a focus block also `show_as`
        and `title`. `id` is the value the same backend put on the `Event.id` it
        handed back earlier — never re-derived, always round-tripped."""
        raise NotImplementedError

    def delete_event(self, id):
        """Remove an event from this calendar, by the id it was given under.
        Used by the negotiation guards and by the integration-test harness to
        clean up what it seeded."""
        raise NotImplementedError

    # -- Acceptance -----------------------------------------------------------

    def respond_event(self, response, *, id=None, requester=None, slot=None, title=None):
        """Answer a received invite: put an accepted (or declined/tentative)
        meeting on this calendar. `id` is used when the caller already knows
        which event this answers; otherwise the backend searches its own
        calendar for an existing hold matching (requester, slot) — the
        not-yet-landed-invite case (`InviteNotReady`) has no `id` to give, by
        definition, so the search path is not optional. Real backends override
        this to RSVP an existing invite instead of creating a duplicate — see
        GmailAccount. The base class ignores `id`/`requester`/`slot` and just
        books a plain event; real backends never fall back this far in
        practice."""
        return self.create_event(title, slot, requester)
