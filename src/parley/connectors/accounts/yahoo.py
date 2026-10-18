"""Yahoo backend for Parley — mail (IMAP read + SMTP send) **+ CalDAV calendar**.

Implements the Account I/O surface against a Yahoo account: mail over stdlib
`imaplib`/`smtplib`, and — iteration 17 — the calendar over **CalDAV**
(`caldav.calendar.yahoo.com`, same app password, raw `requests`). The organizer
principle holds: `create_event` writes the meeting object on the user's OWN
calendar (PUT) and then emails the attendee the iCalendar REQUEST (Yahoo CalDAV
does not schedule reliably on its own); `events_overlapping` reads real free/busy
(calendar-query REPORT), so Yahoo stops being collision-blind.

CalDAV discovery runs at connect() and is **best-effort**: any failure prints
loudly and the account degrades to the old mail-only behavior (invites still go
out; free/busy is empty) rather than blocking mail.

Credentials are carried **inline** in the cfg's `[[account]]` (`address` +
`app_password` + `timezone`) — iterations 07-0/16. An optional legacy
`~/.parley/yahoo.json` is still read as a fallback. No env fallbacks.
"""

import imaplib
import json
import os
import smtplib
import ssl
import time
import uuid
import xml.etree.ElementTree as ET
from email import message_from_bytes
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import parseaddr

import requests

from parley.connectors.accounts.account import Account, InviteNotReady
from parley.connectors.accounts.ics import (build_ics_event, build_ics_reply,
                                 build_ics_request, event_from_vevent,
                                 invite_from_ics)
from parley.core import PARLEY_COMMENT, Event, Message, addresses_in, mime_message

IMAP_HOST = "imap.mail.yahoo.com"
SMTP_HOST = "smtp.mail.yahoo.com"
SMTP_SSL_PORT = 465        # implicit TLS — Yahoo's preferred port, no STARTTLS race
SMTP_STARTTLS_PORT = 587   # fallback
SMTP_TIMEOUT = 45          # Yahoo can be slow to send the SMTP greeting

CALDAV_BASE = "https://caldav.calendar.yahoo.com"
CALDAV_TIMEOUT = 20
_DAV = "DAV:"
_CAL = "urn:ietf:params:xml:ns:caldav"


class YahooAccount(Account):
    name = "yahoo"

    def __init__(self, config=None):
        """Bind this connection to one account's settings (config.py:AccountConfig).

        Credentials are inline in the cfg (`address` from the account, `app_password`
        from its settings); an optional legacy secrets json (an explicit `secrets`
        path, then `~/.parley/yahoo.json`) is read as a fallback. No env fallbacks
        (iteration 07-0)."""
        settings = getattr(config, "settings", None) or {}
        self._addr = getattr(config, "address", "") or settings.get("address", "")
        self._password = settings.get("app_password", "")
        self._secrets_file = settings.get("secrets")  # optional legacy fallback path
        # Mail-only backend: there is no calendar to ask for a timezone, so the cfg
        # must say it (`timezone = "Europe/Madrid"`). Without it, "today at 19:00"
        # is read as UTC — the 2026-07-05 two-hours-late F3 invite.
        self._tz = str(settings.get("timezone", "") or "")
        # Optional: pin which CalDAV calendar to use (name or href substring). Yahoo's
        # home set can hold several and is returned in unstable order, so without a pin we
        # choose deterministically (see _choose_calendar).
        self._cal_pref = str(settings.get("calendar", "") or "")
        self._imap = None
        self._cal_href = None   # set by _caldav_discover(); None = mail-only
        # Set by _caldav_discover when the home set has >1 calendar and no cfg pin: the
        # list of candidates awaiting the user's choice (the Agent asks at startup, then
        # apply_calendar_choice sets _cal_href). None = nothing to ask.
        self._cal_choice = None
        # Every VEVENT calendar found by _caldav_discover (regardless of count or
        # pin) as [{"name","href"}] — the setup wizard reads this to pin one up front.
        self._cal_candidates = []
        self._seq = 0
        # First IMAP poll starts from the highest UID already present so we only
        # ever surface mail that arrives *after* connect() (like the other backends).
        self._last_uid = 0

    # -- Lifecycle / identity -------------------------------------------------

    def _credentials(self):
        """(address, app_password): inline from the cfg, else an optional legacy secrets
        json (an explicit `secrets` path, then `~/.parley/yahoo.json`). No env fallback."""
        addr, pw = self._addr, self._password
        if not (addr and pw):
            for path in (self._secrets_file,
                         os.path.join(os.path.expanduser("~"), ".parley", "yahoo.json")):
                path = os.path.expanduser(path) if path else path
                if path and os.path.exists(path):
                    with open(path, encoding="utf-8") as f:
                        data = json.load(f)
                    addr = addr or data.get("address")
                    pw = pw or data.get("app_password")
                    break
        if not (addr and pw):
            raise SystemExit(
                "Yahoo creds missing. Add `address` + `app_password` to the cfg "
                '[[account]], or write ~/.parley/yahoo.json '
                '{"address":..,"app_password":..}.')
        return addr, pw

    def connect(self):
        addr, pw = self._credentials()
        self._addr, self._password = addr, pw
        self._imap = imaplib.IMAP4_SSL(IMAP_HOST)
        self._imap.login(addr, pw)
        self._imap.select("INBOX")
        # Baseline: remember the current max UID so the first fetch_new_messages
        # returns only what lands after we connected.
        self._last_uid = self._max_uid()
        self._caldav_discover()

    # -- CalDAV (iteration 17) -------------------------------------------------

    def _journal(self, kind, **fields):
        """Best-effort structured log to the agent's flight recorder (the Agent injects
        `self.journal` after construction). Silent no-op when absent (e.g. selftest,
        standalone probe). Keeps CalDAV I/O visible in the journal, not only on stdout."""
        j = getattr(self, "journal", None)
        if j is None:
            return
        try:
            j.log(kind, account=self.name, **fields)
        except Exception:  # noqa: BLE001 — journaling must never break an I/O path
            pass

    def _dav(self, method, url, body=None, depth=None,
             content_type='text/xml; charset="utf-8"'):
        # PROPFIND/REPORT carry XML bodies (the default); a PUT of a calendar object
        # MUST be sent as text/calendar — Yahoo 403s a VEVENT PUT that claims text/xml
        # (2026-07-08 #5, confirmed: same object 403s as text/xml, 201s as text/calendar).
        #
        # A BODYLESS request (GET, DELETE) must carry NO Content-Type at all: Yahoo
        # 400s a GET that declares text/xml, even on a perfectly valid URL. That is
        # what actually broke every Yahoo move — `_get_by_href`'s GET 400'd, returned
        # None, and update_event raised the misleading "no event at <href>", which read
        # like a missing object and sent the 2026-07-12 investigation after the href
        # encoding instead (2026-09-06: proved by requesting the same URL with and
        # without the header — 400 vs 200). DELETE was equally affected; delete_event's
        # blanket except was hiding it.
        headers = {"Content-Type": content_type} if body is not None else {}
        if depth is not None:
            headers["Depth"] = str(depth)
        resp = requests.request(method, url, data=body, headers=headers,
                                auth=(self._addr, self._password),
                                timeout=CALDAV_TIMEOUT)
        resp.raise_for_status()
        return resp

    @staticmethod
    def _first_href(xml_text, prop_local):
        """The first <D:href> inside the named property of a multistatus reply."""
        root = ET.fromstring(xml_text)
        for el in root.iter():
            if el.tag.endswith(f"}}{prop_local}"):
                href = el.find(f"{{{_DAV}}}href")
                if href is not None and href.text:
                    return href.text.strip()
        return None

    def _caldav_discover(self):
        """Find this account's calendar collection: principal → home → the first
        VEVENT calendar. Best-effort — a failure prints loudly and leaves the
        account mail-only (the pre-17 behavior), never blocks mail."""
        self._cal_href = None
        propfind_principal = ('<?xml version="1.0"?><d:propfind xmlns:d="DAV:">'
                              "<d:prop><d:current-user-principal/></d:prop></d:propfind>")
        propfind_home = ('<?xml version="1.0"?><d:propfind xmlns:d="DAV:" '
                         'xmlns:c="urn:ietf:params:xml:ns:caldav">'
                         "<d:prop><c:calendar-home-set/></d:prop></d:propfind>")
        propfind_cals = ('<?xml version="1.0"?><d:propfind xmlns:d="DAV:" '
                         'xmlns:c="urn:ietf:params:xml:ns:caldav">'
                         "<d:prop><d:resourcetype/><d:displayname/>"
                         "<c:supported-calendar-component-set/></d:prop></d:propfind>")
        try:
            r = self._dav("PROPFIND", f"{CALDAV_BASE}/", propfind_principal, depth=0)
            principal = self._first_href(r.text, "current-user-principal")
            r = self._dav("PROPFIND", CALDAV_BASE + principal, propfind_home, depth=0)
            home = self._first_href(r.text, "calendar-home-set")
            r = self._dav("PROPFIND", CALDAV_BASE + home, propfind_cals, depth=1)
            root = ET.fromstring(r.text)
            candidates = []  # (href, displayname) for every VEVENT calendar
            for response in root.iter(f"{{{_DAV}}}response"):
                rtype = response.find(f".//{{{_DAV}}}resourcetype")
                if rtype is None or rtype.find(f"{{{_CAL}}}calendar") is None:
                    continue
                comps = [c.get("name") for c in response.iter(f"{{{_CAL}}}comp")]
                if comps and "VEVENT" not in comps:
                    continue
                href = response.find(f"{{{_DAV}}}href")
                name = response.find(f".//{{{_DAV}}}displayname")
                if href is not None and href.text:
                    candidates.append((href.text.strip().rstrip("/") + "/",
                                       (name.text or "").strip() if name is not None else ""))
            if not candidates:
                raise RuntimeError("no VEVENT calendar in the home set")
            self._cal_choice = None
            opts = [{"name": n, "href": h} for h, n in candidates]
            self._cal_candidates = opts  # full list for the setup wizard's picker
            if len(candidates) == 1:
                self._cal_href = candidates[0][0]
                print(f"[{self.name}] CalDAV calendar: {self._cal_href}")
                self._journal("caldav_discover", ok=True, chosen=self._cal_href,
                              count=1, options=opts)
                return
            pinned = self._pinned_calendar(candidates)
            if pinned:
                self._cal_href = pinned
                print(f"[{self.name}] {len(candidates)} CalDAV calendars — using pinned "
                      f"{self._cal_href}. Options: "
                      + ", ".join(f"{n or '?'}={h}" for h, n in candidates))
                self._journal("caldav_discover", ok=True, chosen=self._cal_href,
                              pinned=True, count=len(candidates), options=opts)
                return
            # Ambiguous AND unpinned: do NOT guess (the 2026-07-08 default-to-'Calendar'
            # guess hid events on the user's real calendar). Record the candidates for the
            # Agent to ask the user at startup; own-calendar writes degrade to mail-only
            # until one is chosen (create_event still sends the invite).
            self._cal_href = None
            self._cal_choice = opts
            print(f"[{self.name}] {len(candidates)} CalDAV calendars and no `calendar` pin "
                  "— asking the user which to use; own-calendar writes are on hold until "
                  "then. Options: "
                  + ", ".join(f"{n or '?'}={h}" for h, n in candidates))
            self._journal("caldav_choice_needed", count=len(candidates), options=opts,
                          human="Multiple Yahoo calendars and no `calendar` pin — asking "
                                "the user which to use.")
        except Exception as err:  # noqa: BLE001 — degrade to mail-only, loudly
            print(f"[{self.name}] CalDAV unavailable ({err}) — calendar is mail-only "
                  f"this run: no own-calendar events, no free/busy.")
            self._journal("caldav_discover", ok=False, error=str(err),
                          human="CalDAV unavailable — calendar is mail-only this run.")

    def _pinned_calendar(self, candidates):
        """The calendar the cfg `calendar` setting pins (matched by exact name, name
        substring, or href substring), or None when there is no pin or it matches nothing.
        Unlike a blind default, a miss here does NOT auto-pick — the caller escalates the
        ambiguous home set to the user (2026-07-08: guessing 'Calendar' silently wrote to a
        collection the user didn't have open)."""
        pref = self._cal_pref.strip().lower()
        if not pref:
            return None
        for href, name in candidates:
            if pref == name.lower() or pref in name.lower() or pref in href.lower():
                return href
        return None

    def apply_calendar_choice(self, answer):
        """Apply the user's answer to a pending calendar choice (set by _caldav_discover
        when the home set is ambiguous and unpinned). Matches by 1-based index, exact name,
        or a name/href substring. Sets `_cal_href` and clears the pending choice; returns
        the chosen href, or None if nothing matched (the caller re-asks)."""
        cands = getattr(self, "_cal_choice", None) or []
        a = (answer or "").strip().lower()
        if not (cands and a):
            return None
        if a.isdigit():
            i = int(a) - 1
            if 0 <= i < len(cands):
                return self._set_calendar(cands[i]["href"])
        for c in cands:
            name = (c.get("name") or "").lower()
            if a == name or a in name or a in c["href"].lower():
                return self._set_calendar(c["href"])
        return None

    def select_calendar(self, calendar_id):
        """Generic `Account` entry point (`agent.py`'s ambiguous-calendar `decide()`
        branch already resolved which choice this is off `list_calendars()`'s ids,
        which for Yahoo ARE the hrefs) — applies straight through to `_set_calendar`,
        same as `apply_calendar_choice`'s own resolution does."""
        self._set_calendar(calendar_id)

    def _set_calendar(self, href):
        self._cal_href = href
        self._cal_choice = None
        print(f"[{self.name}] calendar set to {href} (add `calendar = ...` to the cfg to "
              f"skip this next time).")
        self._journal("caldav_chosen", chosen=href, human=f"Calendar set to {href}.")
        return href

    def _caldav_url(self, href):
        return href if href.startswith("http") else CALDAV_BASE + href

    def address(self):
        return self._addr

    def timezone(self):
        """The cfg's `timezone` setting — a mail-only account has no calendar to
        ask. Empty → 'UTC' (and local times will be off unless the user's zone IS
        UTC; set it in the cfg)."""
        return self._tz or "UTC"

    def list_calendars(self):
        """Every VEVENT calendar `_caldav_discover` found at connect() — reuses
        the exact list the ambiguous-and-unpinned runtime ask already builds
        (2026-08-31: generalized so the setup wizard can offer the same picker,
        instead of Yahoo being the only backend with this capability)."""
        return [{"name": c["name"] or c["href"], "id": c["href"]} for c in self._cal_candidates]

    def _ensure_imap(self):
        """Yahoo drops idle IMAP connections; reconnect transparently if needed."""
        try:
            status, _ = self._imap.noop() if self._imap else ("NO", None)
            if status == "OK":
                return
        except (imaplib.IMAP4.abort, imaplib.IMAP4.error, OSError, AttributeError):
            pass
        self._imap = imaplib.IMAP4_SSL(IMAP_HOST)
        self._imap.login(self._addr, self._password)
        self._imap.select("INBOX")

    def _max_uid(self):
        status, data = self._imap.uid("search", None, "ALL")
        if status != "OK" or not data or not data[0]:
            return 0
        return max(int(u) for u in data[0].split())

    # -- Mail -----------------------------------------------------------------

    def list_messages(self):
        """INBOX messages with a UID greater than the last one we saw. Parses an
        attached/inline .ics into `Message.invite` so Yahoo can act as a receiving
        peer, mirroring GmailAccount."""
        self._ensure_imap()
        status, data = self._imap.uid(
            "search", None, f"UID {self._last_uid + 1}:*")
        if status != "OK" or not data or not data[0]:
            return []
        uids = [int(u) for u in data[0].split() if int(u) > self._last_uid]
        messages = []
        for uid in sorted(uids):
            # BODY.PEEK[] returns the full message WITHOUT setting the \Seen flag,
            # so polling leaves mail unread — the poll must not mutate the mailbox
            # (plain RFC822 / BODY[] would mark each message read as a side effect).
            status, raw = self._imap.uid("fetch", str(uid), "(BODY.PEEK[])")
            if status != "OK" or not raw:
                continue
            # imaplib interleaves the message literal (a (metadata, bytes) tuple)
            # with plain-bytes lines (FLAGS, the closing ')'); grab the tuple.
            body = next((part[1] for part in raw
                         if isinstance(part, tuple) and part[1]), None)
            if body is None:
                continue
            mime = message_from_bytes(body)
            messages.append(self._to_message(str(uid), mime))
            self._last_uid = max(self._last_uid, uid)
        return messages

    def _to_message(self, uid, mime):
        sender = parseaddr(mime.get("From", ""))[1]
        subject = str(mime.get("Subject", ""))
        body, ics = _walk_mime(mime)
        invite = invite_from_ics(ics) if ics else None
        return Message(uid, sender, subject, body, invite=invite,
                       message_id=mime.get("Message-ID"),
                       in_reply_to=mime.get("In-Reply-To"),
                       references=mime.get("References"),
                       recipients=addresses_in(mime.get("To"), mime.get("Cc")))

    def send_message(self, to, subject, body, thread=None):
        # `to` may be one address or several; SMTP takes its envelope from the built
        # message's own To: header (see `_smtp_send`), so a group message is one mail.
        organizer, pw = self._credentials()
        mime = mime_message(subject, organizer, to, body, thread=thread)
        self._smtp_send(organizer, pw, to, mime)

    def _smtp_send(self, organizer, pw, to, mime):
        """Send `mime` over Yahoo SMTP: implicit-TLS 465 first (no STARTTLS greeting
        race), STARTTLS 587 as a fallback."""
        ctx = ssl.create_default_context()
        try:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_SSL_PORT, timeout=SMTP_TIMEOUT,
                                  context=ctx) as server:
                server.login(organizer, pw)
                server.send_message(mime)
            return
        except (smtplib.SMTPException, OSError) as err:
            print(f"[{self.name}] SMTP 465 failed ({err}); retrying STARTTLS 587 …")
        with smtplib.SMTP(SMTP_HOST, SMTP_STARTTLS_PORT, timeout=SMTP_TIMEOUT) as server:
            server.ehlo()
            server.starttls(context=ctx)
            server.login(organizer, pw)
            server.send_message(mime)

    # -- Calendar (CalDAV, iteration 17; degrades to mail-only) ----------------

    def list_events(self, window):
        """Real free/busy via a calendar-query REPORT (time-range). No CalDAV →
        [] (the old mail-only behavior, already announced loudly at connect)."""
        if not getattr(self, "_cal_href", None):
            return []
        def z(dt):
            from datetime import timezone as _tz
            return dt.astimezone(_tz.utc).strftime("%Y%m%dT%H%M%SZ")
        report = ('<?xml version="1.0"?>'
                  '<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
                  "<d:prop><c:calendar-data/></d:prop>"
                  '<c:filter><c:comp-filter name="VCALENDAR">'
                  '<c:comp-filter name="VEVENT">'
                  f'<c:time-range start="{z(window.start)}" end="{z(window.end)}"/>'
                  "</c:comp-filter></c:comp-filter></c:filter></c:calendar-query>")
        try:
            r = self._dav("REPORT", self._caldav_url(self._cal_href), report, depth=1)
        except Exception as err:  # noqa: BLE001 — a read failure must not kill a poll
            print(f"[{self.name}] CalDAV query failed ({err}) — reporting no events.")
            return []
        out = []
        root = ET.fromstring(r.text)
        for response in root.iter(f"{{{_DAV}}}response"):
            href_el = response.find(f"{{{_DAV}}}href")
            data = next((el.text for el in response.iter()
                         if el.tag.endswith("}calendar-data") and el.text), None)
            parsed = event_from_vevent(data) if data else None
            if not parsed:
                continue
            href = href_el.text.strip() if href_el is not None and href_el.text else ""
            attendees = parsed["attendees"] or []
            if parsed["organizer"] and parsed["organizer"] not in attendees:
                attendees = attendees + [parsed["organizer"]]
            ev = Event(href or parsed["uid"], parsed["title"],
                       parsed["start"], parsed["end"], attendees,
                       raw={"href": href, "ics": data, "uid": parsed["uid"]},
                       organizer=parsed["organizer"])
            if ev.slot.overlaps(window):
                out.append(ev)
        return out

    def _get_by_href(self, href):
        """Read one stored VEVENT back by its href — the current title/attendees/uid,
        needed to rebuild the PUT for `update_event` since `changes` only carries the
        diff (2026-08-31: `id` for this backend IS the href, round-tripped).

        Yahoo's REPORT hands back hrefs with the uid's '@' ALREADY percent-encoded, so
        the round-tripped id is double-encoded ('%2540') and Yahoo rejects the URL
        outright — "Bad Message 400 / reason: Ambiguous double encoding", before it
        ever looks for the object. Retry unquoted, and return the href that ACTUALLY
        worked so the caller's PUT targets the same object rather than the broken form.

        This was only HALF of why every Yahoo move failed (2026-09-06, scenario A.2):
        `_dav` also sent a Content-Type on the bodyless GET, which Yahoo 400s on its
        own — see the note there. Both had to be fixed; either alone still 400s.
        """
        from urllib.parse import unquote
        candidates = [href]
        if unquote(href) != href:
            candidates.append(unquote(href))
        for candidate in candidates:
            try:
                r = self._dav("GET", self._caldav_url(candidate))
            except Exception:  # noqa: BLE001 — try the decoded form before giving up
                continue
            parsed = event_from_vevent(r.text)
            if not parsed:
                continue
            attendees = parsed["attendees"] or []
            if parsed["organizer"] and parsed["organizer"] not in attendees:
                attendees = attendees + [parsed["organizer"]]
            return Event(candidate, parsed["title"], parsed["start"], parsed["end"],
                         attendees, raw={"href": candidate, "uid": parsed["uid"]},
                         organizer=parsed["organizer"])
        return None

    def create_event(self, title, slot, attendee):
        """The organizer principle (iteration 17): PUT the meeting object on the
        user's OWN Yahoo calendar first, then email `attendee` the iCalendar
        REQUEST (Yahoo CalDAV doesn't schedule reliably by itself). Without CalDAV
        the invite still goes out (mail-only degradation)."""
        organizer, pw = self._credentials()
        attendee = (list(attendee) if isinstance(attendee, (list, tuple))
                    else [attendee])  # multiparty: N ATTENDEE lines, invite mailed to all
        uid = f"{uuid.uuid4()}@parley"
        if getattr(self, "_cal_href", None):
            put_url = self._caldav_url(f"{self._cal_href}{uid}.ics")
            try:
                resp = self._dav("PUT", put_url,
                                 build_ics_event(uid, organizer, attendee, title,
                                                 slot.start, slot.end),
                                 content_type='text/calendar; charset="utf-8"')
                # Verify it actually persisted — a 2xx that doesn't survive a read-back is
                # the silent failure the #5 post-mortem couldn't see (event absent afterward).
                # `list_events` is the Account API on every backend; the old name
                # `events_overlapping` exists nowhere, so this read-back ALWAYS raised
                # AttributeError — swallowed by the except below, which then journaled
                # caldav_write ok=False and printed "the meeting is NOT on the Yahoo
                # calendar" for bookings that had in fact persisted perfectly. Every
                # Yahoo booking was recorded as a failure (2026-09-06).
                landed = any((e.raw or {}).get("uid") == uid
                             for e in self.list_events(slot))
                self._journal("caldav_write", ok=landed, status=resp.status_code,
                              uid=uid, href=self._cal_href, title=title,
                              start=slot.start.isoformat(), end=slot.end.isoformat(),
                              human=("" if landed else
                                     f"⚠️ CalDAV PUT returned {resp.status_code} but the "
                                     f"event is NOT readable back — it did not persist."))
                if not landed:
                    print(f"[{self.name}] CalDAV PUT returned {resp.status_code} but the "
                          f"event isn't on the calendar on read-back (did not persist).")
            except Exception as err:  # noqa: BLE001 — invite still goes out
                print(f"[{self.name}] CalDAV PUT failed ({err}) — the meeting is NOT "
                      f"on the Yahoo calendar; the invite is still being sent.")
                self._journal("caldav_write", ok=False, uid=uid, href=self._cal_href,
                              title=title, error=str(err),
                              human=f"CalDAV PUT failed ({err}) — meeting NOT on the "
                                    f"Yahoo calendar; invite still sent.")
        when = f"{slot.start:%Y-%m-%d %H:%M}–{slot.end:%H:%M}Z"
        msg = MIMEMultipart("mixed")
        msg["From"], msg["To"] = organizer, ", ".join(attendee)
        msg["Subject"] = f"Invitation: {title}"
        msg.attach(MIMEText(f"You're invited to '{title}' ({when}).", "plain"))
        cal = MIMEText(build_ics_request(organizer, attendee, title, slot.start,
                                         slot.end, uid=uid),
                       "calendar; method=REQUEST", "utf-8")
        cal.add_header("Content-Disposition", "attachment; filename=invite.ics")
        msg.attach(cal)
        self._smtp_send(organizer, pw, attendee, msg)
        self._seq += 1
        return Event(f"yahoo-{self._seq}", title, slot.start, slot.end,
                     [organizer] + attendee, raw={"uid": uid})

    def respond_event(self, response, *, id=None, requester=None, slot=None, title=None):
        """RSVP the invite `requester` already sent. Yahoo auto-adds a received
        invite to the CalDAV calendar but has no scheduling API, so the ONLY way
        the organizer learns of the acceptance is a mail-borne iCalendar
        METHOD:REPLY — find the auto-added copy (a known `id`/href, or search
        `slot` by requester/attendee), email the reply to the organizer, and
        return the event. Without this override the base class created a
        duplicate self-organized event and the organizer's copy stayed
        needsAction forever (2026-07-12 Scenario E). A known `id` with no
        `requester` given still retries — the not-yet-landed-invite case has no
        id yet, by definition, so most callers search instead."""
        me, pw = self._credentials()
        must_rsvp = id is not None or requester is not None
        attempts = 4 if must_rsvp else 1
        partstat = "DECLINED" if response == "declined" else "ACCEPTED"
        for attempt in range(attempts):
            candidates = [self._get_by_href(id)] if id is not None else self.list_events(slot)
            for event in candidates:
                if event is None:
                    continue
                if id is None:
                    org = (event.organizer or "").lower()
                    if requester.lower() != org and \
                            not any(requester.lower() == a.lower() for a in event.attendees):
                        continue
                if (event.organizer or "").lower() == me.lower():
                    return event  # our own event — nothing to reply to (asker dedupe)
                uid = (event.raw or {}).get("uid")
                to = event.organizer or requester
                if not uid:
                    break  # no UID → a REPLY can't be matched; fall through
                msg = MIMEMultipart("mixed")
                msg["From"], msg["To"] = me, to
                msg["Subject"] = f"{partstat.title()}: {event.title}"
                msg.attach(MIMEText(f"'{event.title}' {partstat.lower()}.", "plain"))
                cal = MIMEText(build_ics_reply(uid, to, me, event.title,
                                               event.start, event.end, response=partstat),
                               "calendar; method=REPLY", "utf-8")
                cal.add_header("Content-Disposition", "attachment; filename=reply.ics")
                msg.attach(cal)
                self._smtp_send(me, pw, to, msg)
                self._journal("rsvp_reply_sent", uid=uid, organizer=to,
                              title=event.title, start=event.start.isoformat(),
                              end=event.end.isoformat(),
                              human=f"✅ mailed METHOD:REPLY ({partstat.lower()}) for "
                                    f"'{event.title}' to {to}.")
                return event
            if must_rsvp and attempt < attempts - 1:
                time.sleep(5)
        if must_rsvp:
            raise InviteNotReady(
                f"the invite from {requester} isn't on the Yahoo calendar yet — defer "
                f"and retry; refusing to create a duplicate event instead of RSVPing")
        # No invite found — fall back to creating the event (asker path).
        return self.create_event(title, slot, requester)

    def update_event(self, id, changes):
        """Rewrite the stored VEVENT at `id` (its href) with the new times
        (SEQUENCE+1). Re-reads the current title/attendees/uid first since
        `changes` only carries the diff. `id` must be an event this backend
        actually returned via `list_events` — Yahoo can't move something it
        never read."""
        if not getattr(self, "_cal_href", None):
            raise NotImplementedError(
                "YahooAccount can only move events it read via CalDAV.")
        current = self._get_by_href(id)
        if current is None:
            raise LookupError(f"no event at {id!r}")
        uid = (current.raw or {}).get("uid")
        others = [a for a in current.attendees if a.lower() != self._addr.lower()]
        start = changes.get("start", current.start)
        end = changes.get("end", current.end)
        # `current.id` is the href the GET actually resolved — which may be the
        # unquoted form of `id` (see _get_by_href); PUTting to `id` would 404 again.
        self._dav("PUT", self._caldav_url(current.id),
                  build_ics_event(uid, self._addr, others[0] if others else self._addr,
                                  current.title, start, end, sequence=1),
                  content_type='text/calendar; charset="utf-8"')

    def delete_event(self, id):
        """DELETE the stored object; a stub/mail-only event is a harmless no-op
        (the integration harness's cleanup relies on that). Yahoo's REPORT returns
        hrefs with the uid's '@' already percent-encoded ('%2540' = double-encoded),
        which the server rejects outright — retry once with the href unquoted.

        RAISES if both attempts fail. It used to swallow the failure behind a print,
        which is how a broken DELETE went unnoticed for months: every caller was told
        the event was gone while it sat on the calendar (the harness's cleanup even
        reported "[ok] deleted"). A delete that silently does nothing is worse than a
        loud one (2026-09-06).
        """
        if not (getattr(self, "_cal_href", None) and id):
            return None
        from urllib.parse import unquote
        attempts = [id] if unquote(id) == id else [id, unquote(id)]
        last = None
        for href in attempts:
            try:
                self._dav("DELETE", self._caldav_url(href))
                return None
            except Exception as err:  # noqa: BLE001 — try the decoded form, then give up
                last = err
        raise RuntimeError(f"CalDAV DELETE failed for {id!r}: {last}")


def _walk_mime(mime):
    """Return (body, ics_text) from a stdlib email.message. `body` is the text/html
    part when it carries a hidden PARLEY payload (a wire message), else the text/plain
    part as before — so a human's plain mail is unchanged and a peer's machine payload
    survives in the HTML comment. Reads the .ics whether inline (text/calendar) or
    attached (Yahoo/Google both do this)."""
    text, html, ics = "", "", None
    for part in mime.walk():
        ctype = part.get_content_type()
        filename = (part.get_filename() or "").lower()
        if ctype == "multipart/alternative" or part.is_multipart():
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        decoded = payload.decode(part.get_content_charset() or "utf-8", "replace")
        if ctype == "text/plain" and not text:
            text = decoded
        elif ctype == "text/html" and not html:
            html = decoded
        elif ctype in ("text/calendar", "application/ics") or filename.endswith(".ics"):
            ics = decoded
    body = html if (html and PARLEY_COMMENT in html) else text
    return body, ics
