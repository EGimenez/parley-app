"""Local Outlook backend for Parley — classic desktop Outlook via COM (pywin32).

Implements the Account I/O surface against the classic Outlook app running
on this machine, with no cloud API: mail (read via the Round-1 trailing-window
poll, send via a new MailItem) and the default Calendar folder (read + write).

Windows + *classic* Outlook only. The "new Outlook" (olk.exe) has no COM model,
and classic needs a configured MAPI profile. On a machine where only new Outlook
is set up, connect() will fail fast — use accounts/outlook.py (Graph) instead.
See README.md for the classic-vs-new note.
"""

import os
import threading
import time
from datetime import datetime, timedelta, timezone

import pythoncom
import win32com.client

from parley.connectors.accounts.account import Account, InviteNotReady
from parley.connectors.accounts.cursor import load_cursor, save_cursor
from parley.core import (FOCUS_MARK, PARLEY_COMMENT, Event, Message, as_addresses, from_iso,
                         render_parts, to_iso)

POLL_INTERVAL_SECONDS = 30
OL_FOLDER_INBOX = 6
OL_FOLDER_CALENDAR = 9
OL_MAIL_ITEM = 0
OL_APPOINTMENT_ITEM = 1
OL_MEETING = 1            # olMeeting (we organized it, attendees invited)
OL_MEETING_RECEIVED = 3   # olMeetingReceived (someone else's invite on our calendar)
OL_MEETING_CANCELED = 5   # olMeetingCanceled
OL_RESPONSE_ACCEPT = 3    # olMeetingAccepted (MeetingItem.Respond / appt.Respond)
PR_SMTP_ADDRESS = "http://schemas.microsoft.com/mapi/proptag/0x39FE001E"
PR_SENDER_SMTP_ADDRESS = "http://schemas.microsoft.com/mapi/proptag/0x5D01001F"
# AppointmentItem.ResponseStatus → Event.response. 0 (olResponseNone) is what a
# plain appointment with no meeting behind it reports, so it means "can't tell".
# AppointmentItem.BusyStatus: 0 free, 1 tentative, 2 busy, 3 out of office,
# 4 working elsewhere.
OL_BUSY_STATUS = {"free": 0, "tentative": 1, "busy": 2}
OL_SHOW_AS = {0: "free", 1: "tentative", 2: "busy", 3: "busy", 4: "free"}
OL_RESPONSE_STATUS = {1: "organizer", 2: "tentative", 3: "accepted", 4: "declined",
                      5: "none"}


class LocalOutlookAccount(Account):
    name = "local-outlook"

    def __init__(self, config=None):
        # Classic Outlook exposes only the profile's default Calendar folder over
        # COM, so this backend is single-calendar; `config` is accepted for a
        # uniform constructor signature.
        self._config = config
        settings = getattr(config, "settings", None) or {}
        # Durable cursor: persist the last-seen ReceivedTime so a restart resumes from
        # there instead of marking everything currently in the inbox as seen (which
        # would drop mail that arrived while we were down). Lives under the account's
        # state_dir (~/.parley/state/<name>/), like the other backends. No env fallback.
        state_dir = settings.get("state_dir") or os.path.join(
            os.path.expanduser("~"), ".parley", "state", self.name)
        self.state_file = settings.get("state_file") or os.path.join(state_dir, "state.json")
        self._since = None  # where the next poll's window starts (set at connect())
        # COM handles are per-THREAD (see _bind): the daemon connects on one thread,
        # polls on another and serves HTTP on a third.
        self._tls = threading.local()
        self._address = None

    def connect(self):
        self._bind()
        # Seed the "seen" set so we don't replay existing mail. With a saved
        # last-seen time, leave it empty and widen the first poll's window back to
        # that time, so anything that arrived while we were down is surfaced once.
        saved = load_cursor(self.state_file)
        if saved and saved.get("received_at"):
            # The cursor is local wall-clock time; from_iso tags a bare stamp as UTC,
            # so drop that tag to compare it with the (naive, local) poll window.
            self._since = from_iso(saved["received_at"]).replace(tzinfo=None)
            self._seen = set()
        else:
            self._since = _default_since()
            self._seen = {eid for eid, _ in self._inbox_window(self._since)}

    # -- per-thread COM ------------------------------------------------------
    #
    # COM is apartment-bound: a thread that has not called CoInitialize cannot use an
    # interface at all, and a proxy created in one apartment is not valid in another.
    # The daemon breaks both rules by construction — it connects on the main thread,
    # polls on a second, and serves every HTTP request on a third — so a shared
    # self._app died with the wonderfully unhelpful "AttributeError:
    # Outlook.Application.CreateItem" the first time a real send came in over HTTP
    # (live, 2026-09-19). Before that this backend had only ever run single-threaded
    # under `parley --run`, which is why it looked fine.
    #
    # So every thread initialises COM once and re-Dispatches its own handles. Outlook
    # is an out-of-process, single-instance server: a second Dispatch is another proxy
    # to the SAME running Outlook, not a second copy. The old attribute names are kept
    # as properties, so the rest of this file is unchanged.

    def _bind(self):
        """This thread's Outlook handles, made on first use here."""
        tls = self._tls
        if getattr(tls, "app", None) is not None:
            return tls
        pythoncom.CoInitialize()
        tls.app = win32com.client.Dispatch("Outlook.Application")
        tls.ns = tls.app.GetNamespace("MAPI")
        tls.account = self._select_account(tls.ns)
        if tls.account is not None:
            store = tls.account.DeliveryStore
            tls.inbox = store.GetDefaultFolder(OL_FOLDER_INBOX)
            tls.calendar = store.GetDefaultFolder(OL_FOLDER_CALENDAR)
            self._address = tls.account.SmtpAddress
        else:
            tls.inbox = tls.ns.GetDefaultFolder(OL_FOLDER_INBOX)
            tls.calendar = tls.ns.GetDefaultFolder(OL_FOLDER_CALENDAR)
            try:
                self._address = tls.ns.CurrentUser.AddressEntry.GetExchangeUser().PrimarySmtpAddress
            except Exception:  # noqa: BLE001 — non-Exchange profile
                self._address = str(tls.ns.CurrentUser.Address)
            self._warn_if_split(tls.inbox)
        return tls

    @property
    def _app(self):
        return self._bind().app

    @property
    def _ns(self):
        return self._bind().ns

    @property
    def _inbox(self):
        return self._bind().inbox

    @property
    def _calendar(self):
        return self._bind().calendar

    @property
    def _account(self):
        return self._bind().account

    def _select_account(self, ns):
        """Bind to the profile account whose SMTP address matches the cfg `address:`.

        A classic Outlook profile routinely holds several accounts, and the two ways
        of asking "who am I" can disagree: `Namespace.CurrentUser` reports the
        Exchange identity, while `GetDefaultFolder()` reads the profile's *default
        store*, which may be a different (e.g. personal) mailbox. Observed live on
        2026-09-19: CurrentUser said the corporate address while the default Inbox
        and Calendar belonged to the personal one — the agent would have introduced
        itself as one identity while reading and booking the other's mailbox.

        So an `address:` in the cfg now SELECTS, it no longer just documents. No
        `address:` → the old behaviour (default store + CurrentUser), plus a loud
        warning when those two disagree."""
        wanted = (getattr(self._config, "address", "") or "").strip().lower()
        if not wanted:
            return None
        found = []
        for i in range(ns.Accounts.Count):
            acct = ns.Accounts.Item(i + 1)
            smtp = (_safe(acct, "SmtpAddress", "") or "").strip()
            if smtp:
                found.append(smtp)
            if smtp.lower() == wanted:
                return acct
        raise RuntimeError(
            f"[{self.name}] cfg  address: {wanted}  is not an account in this Outlook "
            f"profile (present: {', '.join(found) or 'none'}). Refusing to start rather "
            f"than negotiate as the wrong identity — add the account to CLASSIC Outlook, "
            f"or fix `address:` in the cfg.")

    def _warn_if_split(self, inbox):
        """Unpinned account: shout if CurrentUser and the default store disagree."""
        try:
            store = (inbox.Store.DisplayName or "").strip()
        except Exception:  # noqa: BLE001 — diagnostics only
            return
        if store and self._address and store.lower() != self._address.lower():
            print(f"[{self.name}] IDENTITY SPLIT: I report {self._address} but the default "
                  f"store is {store} — I would speak as one and read the other. Pin the "
                  f"account with  address: <smtp>  in the cfg.")

    def _send_as(self, item):
        """Route an outgoing item through the bound account, and REFUSE to send if it
        did not take.

        `SendUsingAccount` is a put-by-*reference* property, and pywin32's ordinary
        attribute assignment silently does nothing to it — no exception, no effect.
        Caught live on 2026-09-19 the only way it can be caught: the mail arrived, and
        the From was the personal account while every other part of the agent said
        corporate. So set it through DISPATCH_PROPERTYPUTREF, then read it back.

        A failure here raises rather than warning. Sending as the wrong person is worse
        than not sending: the recipient replies to a mailbox this agent does not read,
        and the negotiation dies silently. Same stance as `_select_account`."""
        if self._account is None:
            return
        try:
            dispid = item._oleobj_.GetIDsOfNames("SendUsingAccount")
            item._oleobj_.Invoke(dispid, 0, pythoncom.DISPATCH_PROPERTYPUTREF, 0,
                                 self._account)
        except Exception:  # noqa: BLE001 — fall back to the plain (often no-op) assignment
            try:
                item.SendUsingAccount = self._account
            except Exception:  # noqa: BLE001 — verified below either way
                pass
        actual = ""
        try:
            actual = (_safe(item.SendUsingAccount, "SmtpAddress", "") or "").strip()
        except Exception:  # noqa: BLE001 — unreadable means unset
            actual = ""
        if actual.lower() != (self._address or "").lower():
            raise RuntimeError(
                f"[{self.name}] could not pin the sending account to {self._address} "
                f"(Outlook still says {actual or 'the profile default'}) — refusing to "
                f"send, because a reply would go to a mailbox this agent does not read.")

    def address(self):
        # A thread that has not touched Outlook yet still knows who we are: bind
        # first, since `_address` is only learned from the bound account.
        if self._address is None:
            self._bind()
        return self._address

    def timezone(self):
        """The zone unqualified local times are read in. cfg `timezone:` wins (IANA,
        like the yahoo backend); else tzlocal's system zone if installed; else the
        base-class 'UTC' — announced loudly, because on a non-UTC machine that
        books wrong instants."""
        settings = getattr(self._config, "settings", None) or {}
        tz = settings.get("timezone")
        if tz:
            return tz
        try:
            from tzlocal import get_localzone_name
            tz = get_localzone_name()
            if tz:
                return tz
        except Exception:
            pass
        if not getattr(self, "_warned_utc", False):
            self._warned_utc = True
            print(f"[{self.name}] no `timezone:` in the cfg and tzlocal not installed — "
                  "falling back to UTC. Unqualified local times will be read as UTC; "
                  "set  timezone: Europe/Madrid  (IANA) in the account entry.")
        return super().timezone()

    # -- Contacts --------------------------------------------------------------

    def find_contact(self, name):
        """Resolve a display name through Outlook's own resolver (GAL + local
        contacts) to a bare SMTP address, or None."""
        try:
            rcp = self._ns.CreateRecipient(name)
            if rcp.Resolve():
                addr = _recipient_smtp(rcp)
                if addr and "@" in addr and not addr.upper().startswith("/O="):
                    return addr
        except Exception:
            pass
        return None

    # -- Mail -----------------------------------------------------------------

    def _inbox_window(self, since=None):
        if since is None:
            since = _default_since()
        items = self._inbox.Items
        items.Sort("[ReceivedTime]", False)
        restricted = items.Restrict("[ReceivedTime] >= '" + since.strftime("%m/%d/%Y %I:%M %p") + "'")
        found = []
        for item in restricted:
            eid = _safe(item, "EntryID")
            if eid:
                found.append((eid, item))
        return found

    # -- Calendar picker (setup + runtime ambiguity, 2026-08-31) --------------

    def list_calendars(self):
        """Classic Outlook only exposes the profile's one default Calendar folder
        over COM — nothing to pick, same as most backends' single-calendar case."""
        return []

    def list_messages(self):
        pythoncom.PumpWaitingMessages()
        # The window runs from the PREVIOUS poll's start, not from "now minus a
        # fixed slack". A fixed window silently dropped every mail that arrived
        # while the machine slept: the daemon stays up across sleep, so connect()
        # never re-widens, and the first poll after wake only looked two minutes
        # back (live, 2026-09-25: a decline and a "move it" mail lost overnight).
        # The same gap opens when one brain turn outlasts the slack. The overlap
        # with the previous window is exactly what `_seen` already dedupes.
        next_since = _default_since()
        current = self._inbox_window(min(self._since or next_since, next_since))
        fresh = [item for eid, item in current if eid not in self._seen]
        self._seen = {eid for eid, _ in current}
        self._since = next_since
        self._save_state(current)
        out = []
        for i in fresh:
            mid, irt, refs = _thread_headers(i)
            out.append(Message(_safe(i, "EntryID"), _sender_address(i),
                               _safe(i, "Subject"), _body_of(i),
                               invite=_invite_from_meeting_item(i),
                               message_id=mid, in_reply_to=irt, references=refs,
                               recipients=_recipient_addresses(i),
                               sender_name=_safe(i, "SenderName") or None))
        return out

    def _save_state(self, current):
        """Persist the newest ReceivedTime in view, so a restart resumes from here."""
        if not self.state_file or not current:
            return
        newest = None
        for _, item in current:
            rt = _received_at(item)
            if rt and (newest is None or rt > newest):
                newest = rt
        if newest is not None:
            save_cursor(self.state_file, received_at=to_iso(newest))

    def send_message(self, to, subject, body, thread=None):
        mail = self._app.CreateItem(OL_MAIL_ITEM)
        # `to` may be one address or several; Outlook's To: line separates them with
        # semicolons, so a meeting with four people is ONE mail to the four of them.
        mail.To, mail.Subject = "; ".join(as_addresses(to)), subject
        _set_thread_headers(mail, thread)
        text, html = render_parts(body)
        if html is None:
            mail.Body = text
        else:
            # HTMLBody carries the hidden PARLEY comment; Outlook derives a clean
            # text/plain Body from it (dropping the comment) for a human reader.
            mail.HTMLBody = html
        self._send_as(mail)
        # Outlook will not send to an UNRESOLVED recipient: setting `.To` is just text
        # until the address book has looked at it, and `Send()` then fails with the
        # famously unhelpful "Outlook does not recognize one or more names" — naming
        # neither the name nor the mail (live, 2026-09-19; this backend had never
        # actually sent one). Resolve first, and if it fails say WHO failed.
        recipients = mail.Recipients
        recipients.ResolveAll()
        # Judge each recipient, NOT ResolveAll's return value: for a plain SMTP address
        # it answers False while every recipient reports Resolved=True (verified live,
        # 2026-09-19). Trusting the return value refuses perfectly good mail.
        unresolved = [_safe(recipients.Item(i + 1), "Name", "?")
                      for i in range(recipients.Count)
                      if not _safe(recipients.Item(i + 1), "Resolved", False)]
        if unresolved:
            raise RuntimeError(
                f"[{self.name}] Outlook could not resolve {', '.join(unresolved)} — the "
                f"mail was NOT sent. Check the address, or that the profile's address "
                f"book is reachable.")
        mail.Send()

    # -- Calendar -------------------------------------------------------------

    def _appointments(self, slot):
        items = self._calendar.Items
        items.IncludeRecurrences = True
        items.Sort("[Start]")
        # Pad the window so boundary appointments are included, then filter.
        # Outlook's Restrict compares LOCAL wall times — format the (tz-aware UTC)
        # slot bounds in local time, or every lookup is shifted by the UTC offset.
        lo = _local_wallclock(slot.start - timedelta(hours=1)).strftime("%m/%d/%Y %I:%M %p")
        hi = _local_wallclock(slot.end + timedelta(hours=1)).strftime("%m/%d/%Y %I:%M %p")
        return items.Restrict(f"[Start] <= '{hi}' AND [End] >= '{lo}'")

    def list_events(self, window):
        events = [_to_event(a) for a in self._appointments(window)]
        return [e for e in events if e is not None and e.slot.overlaps(window)]

    def _get_item(self, id):
        """Re-fetch a COM item by its EntryID — `Event.id` for this backend IS
        the EntryID, so `update_event`/`delete_event`/`respond_event(id=...)` can
        take a bare id like every other backend instead of holding a live COM
        reference (2026-08-31 refactor)."""
        try:
            return self._ns.GetItemFromID(id)
        except Exception:
            return None

    def update_event(self, id, changes):
        appt = self._get_item(id)
        if appt is None:
            raise LookupError(f"no Outlook item for EntryID {id!r}")
        if "start" in changes:
            appt.Start = _to_com_time(changes["start"])
        if "end" in changes:
            appt.End = _to_com_time(changes["end"])
        if "show_as" in changes:
            appt.BusyStatus = OL_BUSY_STATUS[changes["show_as"]]
        if "title" in changes:
            appt.Subject = changes["title"]
        appt.Save()
        # A meeting WE organized: Save() only changes our copy — Send() pushes the
        # update to the attendees (matching gmail/Graph, whose patch notifies).
        try:
            if _safe(appt, "MeetingStatus", 0) == OL_MEETING and appt.Recipients.Count:
                appt.Send()
        except Exception:
            pass  # local-only appointments have nobody to notify

    def create_event(self, title, slot, attendee):
        # Items.Add on the bound folder, not Application.CreateItem: the latter
        # always lands in the profile's DEFAULT calendar, which may not be ours.
        appt = (self._calendar.Items.Add(OL_APPOINTMENT_ITEM) if self._account is not None
                else self._app.CreateItem(OL_APPOINTMENT_ITEM))
        appt.Subject = title
        appt.Start = _to_com_time(slot.start)
        appt.End = _to_com_time(slot.end)
        appt.MeetingStatus = OL_MEETING
        # One Recipients.Add per person: handed a list, COM fails at Send() with
        # MAPI "An object cannot be found" (group invites, live 2026-09-24).
        attendees = as_addresses(attendee)
        for addr in attendees:
            appt.Recipients.Add(addr)
        appt.Recipients.ResolveAll()
        self._send_as(appt)
        appt.Send()
        return Event("(sent)", title, slot.start, slot.end, [self._address] + attendees)

    def create_hold(self, title, slot, show_as="free"):
        """A plain appointment — MeetingStatus stays olNonMeeting, nobody is added,
        and it is saved, never sent."""
        appt = (self._calendar.Items.Add(OL_APPOINTMENT_ITEM) if self._account is not None
                else self._app.CreateItem(OL_APPOINTMENT_ITEM))
        appt.Subject = title
        appt.Start = _to_com_time(slot.start)
        appt.End = _to_com_time(slot.end)
        appt.BusyStatus = OL_BUSY_STATUS[show_as]
        appt.Categories = FOCUS_MARK
        appt.ReminderSet = False
        appt.Save()
        return _to_event(appt)

    def delete_event(self, id):
        """Remove an event; a meeting we organized sends the cancellation to the
        attendees first (matching gmail/Graph delete semantics)."""
        appt = self._get_item(id)
        if appt is None:
            return  # already gone — deleting twice is a no-op, not an error
        try:
            if _safe(appt, "MeetingStatus", 0) == OL_MEETING and appt.Recipients.Count:
                appt.MeetingStatus = OL_MEETING_CANCELED
                appt.Send()
        except Exception:
            pass
        appt.Delete()

    # -- Acceptance ------------------------------------------------------------

    def respond_event(self, response, *, id=None, requester=None, slot=None, title=None):
        """RSVP the invite `requester` already sent (Outlook puts a tentative
        copy on the calendar when the meeting request arrives) instead of
        creating a duplicate. A known `id` answers directly; otherwise searches
        `slot`, retrying while the tentative copy materializes and raising
        rather than creating a second, self-organized event (iteration 17
        semantics, same as gmail/Graph)."""
        must_rsvp = id is not None or requester is not None
        attempts = 4 if must_rsvp else 1
        ol_response = OL_RESPONSE_ACCEPT if response != "declined" else 4  # olMeetingDeclined
        for attempt in range(attempts):
            pythoncom.PumpWaitingMessages()
            if id is not None:
                appt = self._get_item(id)
                candidates = [(appt, _to_event(appt))] if appt is not None else []
            else:
                candidates = [(e.raw, e) for e in self.list_events(slot)]
            for appt, event in candidates:
                if event is None:
                    continue
                if id is None and _safe(appt, "MeetingStatus", 0) != OL_MEETING_RECEIVED:
                    continue
                if id is None:
                    organizer = (event.organizer or "").lower()
                    attendees = [a.lower() for a in event.attendees]
                    if requester.lower() != organizer and requester.lower() not in attendees:
                        continue
                try:
                    reply = appt.Respond(ol_response, True)
                    if reply is not None:
                        reply.Send()
                except Exception:
                    continue  # a broken tentative copy; keep looking / retrying
                return event
            if must_rsvp and attempt < attempts - 1:
                time.sleep(5)
        if must_rsvp:
            raise InviteNotReady(
                f"the invite from {requester} isn't on the calendar yet — defer and retry; "
                f"refusing to create a duplicate event instead of RSVPing")
        # No invite found on the calendar — fall back to creating the event (asker path).
        return self.create_event(title, slot, requester)


def _safe(item, attr, default=""):
    try:
        return getattr(item, attr) or default
    except Exception:
        return default


# MAPI property tags for the RFC 5322 threading headers (PidTagInternetMessageId /
# InReplyTo / References), read via the item's PropertyAccessor. Best-effort: a MAPI
# store that doesn't surface them just yields None, and correlation falls to fuzzy.
_PR_MESSAGE_ID = "http://schemas.microsoft.com/mapi/proptag/0x1035001F"
_PR_IN_REPLY_TO = "http://schemas.microsoft.com/mapi/proptag/0x1042001F"
_PR_REFERENCES = "http://schemas.microsoft.com/mapi/proptag/0x1039001F"


def _prop(item, tag):
    try:
        return item.PropertyAccessor.GetProperty(tag) or None
    except Exception:
        return None


def _thread_headers(item):
    """(message_id, in_reply_to, references) from an Outlook item's internet headers,
    each None when unreadable."""
    return (_prop(item, _PR_MESSAGE_ID), _prop(item, _PR_IN_REPLY_TO),
            _prop(item, _PR_REFERENCES))


def _set_thread_headers(mail, thread):
    """Make an outgoing MailItem a REPLY onto `thread` (Message-IDs, oldest first).

    The same two MAPI properties `_thread_headers` reads, written before Send. Parley
    read these from day one and never wrote them, so every mail it sent opened a new
    conversation in the recipient's client — one meeting, five unrelated threads
    (user, 2026-09-24).

    Best-effort by design: a store that refuses the properties still sends the mail.
    Losing the threading is a cosmetic regression; refusing to send over it would not
    be."""
    chain = [m for m in (thread or []) if m]
    if not chain:
        return
    for tag, value in ((_PR_IN_REPLY_TO, chain[-1]), (_PR_REFERENCES, " ".join(chain))):
        try:
            mail.PropertyAccessor.SetProperty(tag, value)
        except Exception:  # noqa: BLE001 — see the docstring: never block the send
            pass


def _body_of(item):
    """The item's plain Body, unless its HTMLBody carries a hidden PARLEY payload —
    then the HTML, so a peer agent's machine comment survives (Outlook strips comments
    from the derived text Body). A human's plain mail returns the Body as before."""
    html = _safe(item, "HTMLBody")
    if html and PARLEY_COMMENT in html:
        return html
    return _safe(item, "Body")


def _default_since():
    """Start of the trailing window: a little over two poll intervals back, so a
    late ReceivedTime stamp or a slow poll still lands inside it. Local naive time,
    like the Restrict filter and `_received_at`."""
    return _now() - timedelta(seconds=POLL_INTERVAL_SECONDS * 2 + 60)


def _now():
    return datetime.now()  # seam: the selftest moves the clock to simulate a sleep


def _received_at(item):
    """An item's ReceivedTime as a naive local datetime (matching the Restrict
    window, which is built from datetime.now()), or None if unreadable."""
    try:
        rt = item.ReceivedTime
        return datetime(rt.year, rt.month, rt.day, rt.hour, rt.minute, rt.second)
    except Exception:
        return None


def _sender_address(item):
    addr = _safe(item, "SenderEmailAddress")
    if addr.upper().startswith("/O=") or _safe(item, "SenderEmailType") == "EX":
        try:
            smtp = item.Sender.PropertyAccessor.GetProperty(PR_SMTP_ADDRESS)
            if smtp:
                return smtp
        except Exception:
            pass
        # A MeetingItem (an "Accepted: …" answer, say) has no `.Sender` at all; the
        # item's own sender-SMTP property has the address for every item class.
        try:
            smtp = item.PropertyAccessor.GetProperty(PR_SENDER_SMTP_ADDRESS)
            if smtp:
                return smtp
        except Exception:
            pass
        try:
            smtp = item.Sender.GetExchangeUser().PrimarySmtpAddress
            if smtp:
                return smtp
        except Exception:
            pass
    return addr or _safe(item, "SenderName", "(unknown)")


def _recipient_addresses(item):
    """Everyone a received MailItem was addressed to (To: and Cc:), as SMTP addresses.

    Read from the Recipients collection rather than the `To` string, which holds
    DISPLAY NAMES: inside an Exchange organisation a colleague shows up as "Joan Roca"
    and, worse, their address is an X.500 /O= path, so the same PR_SMTP_ADDRESS lookup
    `_sender_address` already does is needed here. Best-effort throughout: a store that
    refuses gives [], which downstream reads as "cannot tell", never as "private"."""
    out = []
    try:
        recipients = item.Recipients
        for i in range(recipients.Count):
            r = recipients.Item(i + 1)
            addr = _safe(r, "Address")
            if "@" not in addr:  # an X.500 /O= path, or nothing at all
                try:
                    addr = r.PropertyAccessor.GetProperty(PR_SMTP_ADDRESS) or addr
                except Exception:  # noqa: BLE001 — an unresolvable entry is not fatal
                    pass
            if addr and "@" in addr:
                out.append(addr)
    except Exception:  # noqa: BLE001 — never let reading the header list drop the mail
        return []
    return as_addresses(out)


# MeetingItem classes of an attendee's answer to an invite we sent.
OL_RESPONSE_CLASSES = {"IPM.Schedule.Meeting.Resp.Pos": "accepted",
                       "IPM.Schedule.Meeting.Resp.Tent": "tentative",
                       "IPM.Schedule.Meeting.Resp.Neg": "declined"}


def _invite_from_meeting_item(item):
    """A real meeting REQUEST in the inbox → the same normalized invite dict the
    other backends attach ({title,start,end,organizer}); an attendee's ANSWER to
    one we sent ("Accepted: …") → the same dict plus `response` and `responder`,
    as the Graph and ICS backends do. None for plain mail and cancellations.

    Before 2026-10-18 answers fell through as plain mail: the agent asked "engage
    with this email?" about every "Aceptada: …" a colleague's Outlook sent back."""
    try:
        cls = str(_safe(item, "MessageClass"))
        response = next((r for prefix, r in OL_RESPONSE_CLASSES.items()
                         if cls.startswith(prefix)), None)
        if response is None and not cls.startswith("IPM.Schedule.Meeting.Request"):
            return None
        appt = item.GetAssociatedAppointment(False)
        if appt is None:
            return None
        # StartUTC/EndUTC, never Start/End - see _from_com_time.
        start = _safe(appt, "StartUTC", None) or _safe(appt, "Start", None)
        end = _safe(appt, "EndUTC", None) or _safe(appt, "End", None)
        if start is None or end is None:
            return None
        out = {"title": _safe(appt, "Subject", "(no subject)"),
               "start": to_iso(_from_com_time(start)),
               "end": to_iso(_from_com_time(end)),
               "organizer": _sender_address(item),
               # GlobalAppointmentID = COM's stable meeting identity (S1's uid role):
               # a resent/updated copy maps to the same negotiation.
               "uid": _safe(appt, "GlobalAppointmentID") or None}
        if response is not None:
            # An answer: the sender is the attendee, the organizer is us.
            out["organizer"] = _organizer_smtp(appt) or ""
            out["response"] = response
            out["responder"] = _sender_address(item)
        return out
    except Exception:
        return None  # a request we can't read is handled as plain mail, like gmail


def _organizer_smtp(appt):
    """The organizer's bare SMTP address, or None when COM can't tell."""
    try:
        entry = appt.GetOrganizer()
        if entry is not None:
            try:
                addr = entry.GetExchangeUser().PrimarySmtpAddress
            except Exception:
                addr = _safe(entry, "Address")
            if addr and "@" in addr:
                return addr
    except Exception:
        pass
    return None


def _to_event(appt):
    if _safe(appt, "MessageClass") and not str(appt.MessageClass).startswith("IPM.Appointment"):
        return None
    # StartUTC/EndUTC, never Start/End - see _from_com_time.
    start = _safe(appt, "StartUTC", None) or _safe(appt, "Start", None)
    end = _safe(appt, "EndUTC", None) or _safe(appt, "End", None)
    if start is None or end is None:
        return None
    attendees = []
    try:
        for recipient in appt.Recipients:
            attendees.append(_recipient_smtp(recipient))
    except Exception:
        pass
    return Event(_safe(appt, "EntryID"), _safe(appt, "Subject", "(no subject)"),
                 _from_com_time(start), _from_com_time(end),
                 [a for a in attendees if a], raw=appt,
                 organizer=_organizer_smtp(appt),
                 response=OL_RESPONSE_STATUS.get(_safe(appt, "ResponseStatus", None)),
                 show_as=_show_as(appt),
                 focus=FOCUS_MARK in [c.strip() for c in
                                      str(_safe(appt, "Categories")).split(",")])


def _show_as(appt):
    """BusyStatus → Event.show_as. Not through `_safe`: free is 0, which `_safe`
    would turn into "unknown"."""
    try:
        return OL_SHOW_AS.get(appt.BusyStatus)
    except Exception:
        return None


def _recipient_smtp(recipient):
    try:
        return recipient.AddressEntry.GetExchangeUser().PrimarySmtpAddress
    except Exception:
        return _safe(recipient, "Address")


def _from_com_time(value):
    """A pywin32 COM time → tz-aware UTC. Hand it StartUTC/EndUTC, never Start/End.

    pywin32 returns every COM DATE stamped +00:00 whatever it actually is, so
    `appt.Start` arrives as the LOCAL wall clock wearing a UTC label — believing that
    label shifts every reading by the UTC offset. `StartUTC` is genuinely UTC, so
    there its number and its label agree and this is an honest relabel."""
    return datetime(value.year, value.month, value.day, value.hour, value.minute,
                    tzinfo=timezone.utc)


def _local_wallclock(dt):
    """A true instant → the naive LOCAL wall clock, for Outlook's Restrict strings:
    its query language compares against local time."""
    return dt.astimezone().replace(tzinfo=None)


def _to_com_time(dt):
    """A true instant → the value to ASSIGN to Start/End so Outlook stores it right.

    Measured against real Outlook on 2026-09-19, because no amount of reading the
    documentation gets you here: pywin32 converts whatever you hand it to UTC, writes
    that wall-clock NUMBER, and Outlook reads the number back as LOCAL time. Handing
    it the correct local time therefore books the meeting one UTC-offset early — and
    it makes no difference whether you pass it naive local, aware local or aware UTC;
    all three were tried and all three land in the same wrong place.

    So: take the local wall clock we want Outlook to show, and label it UTC. That lie
    exactly cancels the one pywin32 tells on the way in.

    This was not theoretical. A meeting agreed for 10:00 Paris was booked at 08:00,
    and Parley could not see it, because `_from_com_time` read Start back with the
    mirror-image error. The two cancelled out internally and the calendar only
    disagreed with itself once Google read the invitation."""
    return dt.astimezone().replace(tzinfo=timezone.utc)
