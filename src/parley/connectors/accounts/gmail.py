"""Gmail backend for Parley — mail (read + send) and Google Calendar (read + write).

Implements the Account I/O surface against a Google account. Reuses the
Round-1 Gmail history-API polling, and adds sending and calendar read/write so
the agent can actually negotiate and book.

Scopes needed (the consent screen will ask once):
  * gmail.readonly  — watch the inbox
  * gmail.send      — send negotiation / confirmation mail
  * calendar.events — read free/busy and create/move events

Requires Gmail API *and* Google Calendar API enabled on the Cloud project. The OAuth
client (client_id + client_secret) is carried **inline** in the cfg's `[account.google]`
table (iteration 07-0) — a legacy `credentials.json` path is still accepted as a
fallback. The generated token + intake cursor live under `~/.parley/state/<name>/`,
never in the repo tree. See README.md.
"""

import base64
import os
import re
import time
from datetime import datetime, timezone
from email.utils import parseaddr

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from parley.connectors.accounts.account import Account, InviteNotReady
from parley.connectors.accounts.cursor import load_cursor, lookback_cutoff, save_cursor
from parley.connectors.accounts.ics import (invite_from_ics as _invite_from_ics,
                                 ics_value as _ics_value, unfold as _unfold)
from parley.core import (FOCUS_MARK, PARLEY_COMMENT, Event, Message, addresses_in, from_iso,
                         mime_message, to_iso)

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/calendar.events",
    # Read-only calendar list, so setup can OFFER the user's calendars to pick
    # from (calendar.events alone cannot enumerate calendars) — iteration 26.
    "https://www.googleapis.com/auth/calendar.readonly",
]

# Standard Google installed-app endpoints, filled in so a cfg's [account.google] table
# carries only client_id + client_secret (both overridable if ever needed).
_GOOGLE_AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
_GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"

# Gmail's tab categories, layered on top of (not instead of) the INBOX label — mail
# the user never reads as "the inbox" and the agent has no business acting on.
# CATEGORY_UPDATES/CATEGORY_PERSONAL are NOT here: real notifications and calendar
# invites often carry them too.
_CATEGORY_SKIP = {"CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL", "CATEGORY_FORUMS"}


def _client_config(google):
    """Assemble the `InstalledAppFlow.from_client_config` dict from the inline
    `[account.google]` table (the replacement for a `credentials.json` file)."""
    return {"installed": {
        "client_id": google["client_id"],
        "client_secret": google["client_secret"],
        "auth_uri": google.get("auth_uri", _GOOGLE_AUTH_URI),
        "token_uri": google.get("token_uri", _GOOGLE_TOKEN_URI),
        "redirect_uris": list(google.get("redirect_uris") or ["http://localhost"]),
    }}


class GmailAccount(Account):
    name = "gmail"

    def __init__(self, config=None):
        """Bind this connection to one account's settings (config.py:AccountConfig).

        Per-account so two Gmail accounts can run in one process without sharing a
        token file. The OAuth client is inline in `[account.google]`; generated files
        (token + intake cursor) live under the account's `state_dir`
        (`~/.parley/state/<name>/`). No env fallbacks (iteration 07-0)."""
        settings = getattr(config, "settings", None) or {}
        # Provided OAuth client: inline [account.google] (preferred), or a legacy
        # credentials.json path for a half-migrated setup. No env fallback.
        self.google = dict(settings.get("google") or {})
        self.credentials_file = settings.get("credentials")  # legacy fallback only
        # Generated state lives under ~/.parley/state/<name>/ (config injects state_dir;
        # the defensive default covers a hand-built AccountConfig).
        state_dir = settings.get("state_dir") or os.path.join(
            os.path.expanduser("~"), ".parley", "state", self.name)
        self.token_file = settings.get("token") or os.path.join(state_dir, "token.json")
        # Durable intake cursor: the historyId persisted across restarts, and how far to
        # look back when we have no usable cursor (cold start / expiry). See cursor.py.
        self.history_file = settings.get("history_file") or os.path.join(state_dir, "history.json")
        # Busy/free is the union across all calendars; the first is the booking target.
        self.calendars = list(getattr(config, "calendars", None) or []) or ["primary"]
        self.catch_up = str(settings.get("catch_up", "1d") or "1d")
        self._catch_up_from = None  # set at connect() on a cold start
        # Which calendar an event id lives on, learned as events are listed/created —
        # so update_event(id, ...)/delete_event(id) don't need a compound id just to
        # find the right calendar (2026-08-31 refactor: id is a bare Google event id,
        # matching what a human/log would expect to see).
        self._calendar_of = {}

    def connect(self):
        creds = None
        if os.path.exists(self.token_file):
            creds = Credentials.from_authorized_user_file(self.token_file, SCOPES)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                except RefreshError:
                    # Refresh token expired or revoked → fall through to interactive
                    # consent. The usual cause is a Google app left in "Testing"
                    # status, which expires refresh tokens every 7 days; say so
                    # loudly, because on a headless daemon the interactive consent
                    # below will block waiting for a browser nobody is watching.
                    print(f"[{self.name}] Gmail refresh token expired — re-consent needed. "
                          "If your Google app is in 'Testing' status, Google expires it "
                          "every 7 days; publish the app to 'In production' (it can stay "
                          "unverified) to stop this. See README.md.")
                    creds = None
            if not creds or not creds.valid:
                creds = self._make_flow().run_local_server(port=0)
            os.makedirs(os.path.dirname(self.token_file) or ".", exist_ok=True)
            with open(self.token_file, "w") as f:
                f.write(creds.to_json())

        self._creds = creds  # reused for the People API in resolve_contact
        self._gmail = build("gmail", "v1", credentials=creds)
        self._cal = build("calendar", "v3", credentials=creds)
        profile = self._gmail.users().getProfile(userId="me").execute()
        self._address = profile["emailAddress"]
        saved = load_cursor(self.history_file)
        if saved and saved.get("history_id"):
            # Normal restart: resume from the saved cursor so anything that arrived
            # while we were down is replayed by the next history poll.
            self._history_id = str(saved["history_id"])
        else:
            # Cold start (no cursor file): baseline to now, but remember a look-back
            # so the first poll replays recent mail by date instead of starting empty.
            self._history_id = str(profile["historyId"])
            self._catch_up_from = lookback_cutoff(self.catch_up, datetime.now(timezone.utc))
            self._save_history()

    def _make_flow(self):
        """The OAuth flow from the inline `[account.google]` client (preferred), or a
        legacy `credentials.json` path. No env fallback (iteration 07-0)."""
        if self.google.get("client_id") and self.google.get("client_secret"):
            return InstalledAppFlow.from_client_config(_client_config(self.google), SCOPES)
        if self.credentials_file and os.path.exists(self.credentials_file):
            return InstalledAppFlow.from_client_secrets_file(self.credentials_file, SCOPES)
        raise SystemExit(
            "Gmail needs an OAuth client: add [account.google] (client_id + client_secret) "
            "to the cfg, or point `credentials` at a client-secret JSON. See README.md.")

    def address(self):
        return self._address

    # -- Contacts -------------------------------------------------------------

    def find_contact(self, name):
        """Best-effort: search the Google address book (People API) for `name` and
        return the first email. Degrades to None on any error — notably a 403 when
        the `contacts.readonly` scope wasn't granted (we deliberately don't force a
        re-consent here; add the scope + re-auth to enable it). An input that is
        already an address passes straight through."""
        if not name:
            return None
        if "@" in name:
            return name
        try:
            people = build("people", "v1", credentials=self._creds)
            resp = people.people().searchContacts(
                query=name, readMask="emailAddresses").execute()
            for result in resp.get("results", []):
                for e in result.get("person", {}).get("emailAddresses", []):
                    if e.get("value"):
                        return e["value"]
        except Exception as err:  # missing scope (403), API disabled, network …
            print(f"[{self.name}] (contacts) resolve '{name}' unavailable: {err}")
        return None

    # -- Calendar picker (setup + runtime ambiguity, 2026-08-31) --------------

    def list_calendars(self):
        """This account's calendars, `[{"name", "id"}]` — empty when there's
        nothing to pick from a single default. Needs the readonly scope
        (calendar.events alone cannot enumerate calendars)."""
        try:
            items = self._cal.calendarList().list().execute().get("items", [])
        except Exception as err:  # noqa: BLE001 — best-effort; caller treats [] as "nothing to pick"
            print(f"[{self.name}] (calendars) couldn't list: {err}")
            return []
        return [{"name": c.get("summary") or c["id"], "id": c["id"]} for c in items]

    def select_calendar(self, calendar_id):
        """Pin the ambiguous-calendar ask's answer as the sole booking target for
        this run (add `calendars: [...]` to the cfg to skip the ask next time)."""
        self.calendars = [calendar_id]
        print(f"[{self.name}] calendar set to {calendar_id} (add `calendars: [{calendar_id}]` "
              "to the cfg to skip this next time).")

    # -- Mail -----------------------------------------------------------------

    def list_messages(self):
        # Cold-start catch-up: on the first poll after a fresh start (no cursor
        # file), replay recent mail by date once, then fall through to incremental
        # history polling from the baseline we recorded at connect().
        if self._catch_up_from is not None:
            cutoff, self._catch_up_from = self._catch_up_from, None
            messages = self._fetch_since_date(cutoff)
            self._save_history()
            return messages

        ids, new_history = [], self._history_id
        page_token = None
        try:
            while True:
                resp = self._gmail.users().history().list(
                    userId="me", startHistoryId=self._history_id,
                    historyTypes=["messageAdded"], pageToken=page_token).execute()
                for record in resp.get("history", []):
                    for added in record.get("messagesAdded", []):
                        ids.append(added["message"]["id"])
                new_history = resp.get("historyId", new_history)
                page_token = resp.get("nextPageToken")
                if not page_token:
                    break
        except HttpError as err:
            if err.resp.status == 404:  # history id too old to resolve → date replay
                return self._on_history_expired()
            raise
        self._history_id = new_history
        messages = self._messages_from_ids(ids)
        self._save_history()
        return messages

    def _on_history_expired(self):
        """The saved historyId is older than Gmail still remembers (down longer than
        ~a week). Instead of dropping the gap, replay by date from the last refresh
        (bounded by `catch_up`), then re-baseline to the current historyId."""
        now = datetime.now(timezone.utc)
        saved = load_cursor(self.history_file) or {}
        start = from_iso(saved["refreshed_at"]) if saved.get("refreshed_at") else None
        bound = lookback_cutoff(self.catch_up, now)
        if start is None:
            start = bound
        elif bound is not None and bound > start:
            start = bound  # never replay an unbounded backlog
        self._history_id = str(self._gmail.users().getProfile(userId="me").execute()["historyId"])
        messages = self._fetch_since_date(start)
        self._save_history()
        return messages

    def _fetch_since_date(self, after_dt):
        """Inbox messages newer than `after_dt` (tz-aware UTC), via a date query —
        the fallback when we have no usable historyId (cold start or expiry).
        `after_dt is None` → nothing to replay."""
        if after_dt is None:
            return []
        query = f"after:{int(after_dt.timestamp())} in:inbox"
        ids, page_token = [], None
        while True:
            resp = self._gmail.users().messages().list(
                userId="me", q=query, pageToken=page_token).execute()
            ids.extend(m["id"] for m in resp.get("messages", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        return self._messages_from_ids(ids)

    def _messages_from_ids(self, ids):
        """Fetch each id, keep INBOX messages, build Messages. Shared by the history
        feed and the date-query replay so both converge on identical handling."""
        seen, messages = set(), []
        for mid in ids:
            if mid in seen:
                continue
            seen.add(mid)
            try:
                msg = self._gmail.users().messages().get(
                    userId="me", id=mid, format="full").execute()
            except HttpError as err:
                # Gmail briefly creates then replaces invite messages, so an id
                # from the history feed can already be gone. Skip it, don't abort
                # the whole batch.
                if err.resp.status == 404:
                    continue
                raise
            labels = msg.get("labelIds", [])
            if "INBOX" not in labels and not self._is_self_addressed(msg):
                # Skip ordinary non-inbox mail (e.g. our outgoing to a peer), but KEEP a
                # message addressed to ourselves: a reply you send can be labelled
                # SENT-only without INBOX — dropping it here made a user's guidance reply
                # vanish silently (2026-07-07, Issue 2a). The email-to-self instruction
                # channel that first motivated this is gone (2026-08-31), but the
                # SENT-only case is still real, so the exception stays.
                continue
            if _CATEGORY_SKIP.intersection(labels) and not self._is_self_addressed(msg):
                # Gmail's tabs (Promotions/Social/Forums) are labels layered ON TOP of
                # INBOX, not a separate folder — a newsletter or webinar invite still
                # carries INBOX and passes the check above, even though the user never
                # sees it in the Primary tab they call "the inbox" (user report
                # 2026-09-05: an investment-webinar mail surfaced as a pending question,
                # and couldn't be found in the inbox — it was sitting in Promotions).
                # CATEGORY_UPDATES is deliberately NOT skipped: real calendar invites and
                # provider notifications often land there too.
                continue
            messages.append(self._to_message(msg))
        return messages

    def _is_self_addressed(self, msg):
        """True when the message is addressed To one of our own addresses — kept
        even without an INBOX label (a SENT-only reply must not be dropped)."""
        headers = {h["name"].lower(): h["value"] for h in msg["payload"].get("headers", [])}
        to = (headers.get("to", "") or "").lower()
        return self._address.lower() in to

    def _save_history(self):
        """Persist the intake cursor (historyId + refresh time) so a restart resumes
        here instead of re-baselining to 'now' and dropping the downtime gap."""
        save_cursor(self.history_file, history_id=str(self._history_id),
                    refreshed_at=to_iso(datetime.now(timezone.utc)))

    def _to_message(self, msg):
        headers = {h["name"].lower(): h["value"] for h in msg["payload"].get("headers", [])}
        sender = parseaddr(headers.get("from", ""))[1]
        subject = headers.get("subject", "")
        invite, skipped = self._extract_invite(msg)
        # Show the MIME tree only for a *genuine* failure: something that looks
        # like an invitation but we couldn't read. A cancellation or reply
        # (METHOD != REQUEST) was understood and intentionally skipped, so it's
        # not a parse failure — don't cry wolf over it.
        looks_like_invite = (subject.lower().startswith("invitation")
                             or _find_calendar_node(msg["payload"]) is not None)
        if invite is None and looks_like_invite and not skipped:
            print(f"[{self.name}] (debug) couldn't parse an invite in '{subject}'. MIME tree:")
            for line in _mime_summary(msg["payload"]):
                print("      " + line)
        return Message(msg["id"], sender, subject, _extract_body(msg["payload"]), invite=invite,
                       message_id=headers.get("message-id"),
                       in_reply_to=headers.get("in-reply-to"),
                       references=headers.get("references"),
                       recipients=addresses_in(headers.get("to"), headers.get("cc")))

    def _extract_invite(self, msg):
        """Return (invite, skipped). `invite` is {title,start,end,organizer} for a
        REQUEST we understood, else None. `skipped` is True when the .ics was a
        recognized non-REQUEST (a cancellation or reply) we intentionally ignored
        — so the caller can distinguish that from a real parse failure.
        Reads the .ics whether it's inline or an attachment (Yahoo attaches it)."""
        node = _find_calendar_node(msg["payload"])
        if node is None:
            return None, False
        body = node.get("body", {})
        data = body.get("data")
        if not data and body.get("attachmentId"):
            att = self._gmail.users().messages().attachments().get(
                userId="me", messageId=msg["id"], id=body["attachmentId"]).execute()
            data = att.get("data")
        if not data:
            return None, False
        ics = base64.urlsafe_b64decode(data).decode("utf-8", "replace")
        method = _ics_value(_unfold(ics), "METHOD").upper()
        if method and method != "REQUEST":
            return None, True  # cancellation / reply — understood, not a failure
        return _invite_from_ics(ics), False

    def send_message(self, to, subject, body, thread=None):
        # `to` may be one address or several: Gmail delivers to the To: header, which
        # `mime_message` builds from the whole list, so a group message is one mail.
        mime = mime_message(subject, self._address, to, body, thread=thread)
        raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()
        self._gmail.users().messages().send(userId="me", body={"raw": raw}).execute()

    # -- Calendar -------------------------------------------------------------

    def timezone(self):
        """The booking calendar's own IANA timezone, cached. Falls back to 'UTC' if
        unreachable.

        Read from `events.list` (whose response carries the calendar's `timeZone`),
        NOT `calendars.get` — the latter needs a calendar *read* scope, but Parley only
        holds `calendar.events`, so `calendars.get` 403s and would silently degrade the
        whole agent to UTC. `events.list` is permitted by `calendar.events`."""
        if getattr(self, "_tz", None):
            return self._tz
        self._tz = "UTC"
        try:
            resp = self._cal.events().list(
                calendarId=self.calendars[0], maxResults=1,
                singleEvents=True, orderBy="startTime").execute()
            self._tz = resp.get("timeZone") or "UTC"
        except Exception:  # noqa: BLE001 — best-effort; UTC on any API error
            pass
        return self._tz

    def list_events(self, window):
        # Union the free/busy view across every calendar on this account.
        out = []
        for cal_id in self.calendars:
            resp = self._cal.events().list(
                calendarId=cal_id,
                timeMin=window.start.isoformat(), timeMax=window.end.isoformat(),
                singleEvents=True, orderBy="startTime").execute()
            for item in resp.get("items", []):
                event = self._to_event(item, cal_id)
                if event is not None and event.slot.overlaps(window):
                    out.append(event)
        return out

    def update_event(self, id, changes):
        cal_id = self._calendar_of.get(id, self.calendars[0])
        body = {}
        if "start" in changes:
            body["start"] = {"dateTime": changes["start"].isoformat()}
        if "end" in changes:
            body["end"] = {"dateTime": changes["end"].isoformat()}
        if "show_as" in changes:
            body["transparency"] = _google_transparency(changes["show_as"])
        if "title" in changes:
            body["summary"] = changes["title"]
        self._cal.events().patch(calendarId=cal_id, eventId=id, body=body).execute()

    def create_hold(self, title, slot, show_as="free"):
        # Google has no "tentative" free/busy state: tentative shows as busy.
        booking = self.calendars[0]
        created = self._cal.events().insert(
            calendarId=booking, sendUpdates="none",
            body={"summary": title,
                  "start": {"dateTime": slot.start.isoformat()},
                  "end": {"dateTime": slot.end.isoformat()},
                  "transparency": _google_transparency(show_as),
                  "reminders": {"useDefault": False},
                  "extendedProperties": {"private": {"parley": FOCUS_MARK}}}).execute()
        return self._to_event(created, booking)

    def create_event(self, title, slot, attendee):
        booking = self.calendars[0]
        attendees = attendee if isinstance(attendee, (list, tuple)) else [attendee]
        created = self._cal.events().insert(
            calendarId=booking, sendUpdates="all",
            body={"summary": title,
                  "start": {"dateTime": slot.start.isoformat()},
                  "end": {"dateTime": slot.end.isoformat()},
                  "attendees": [{"email": a} for a in attendees]}).execute()
        return self._to_event(created, booking)

    def delete_event(self, id):
        cal_id = self._calendar_of.get(id, self.calendars[0])
        self._cal.events().delete(
            calendarId=cal_id, eventId=id, sendUpdates="all").execute()

    def respond_event(self, response, *, id=None, requester=None, slot=None, title=None):
        """RSVP the invite `requester` already sent (Google adds it to the calendar
        on receipt), instead of creating a duplicate event. A known `id` answers
        directly; otherwise searches `slot` for a hold from `requester`/`slot`
        (must_rsvp) — retrying while the event materializes (iteration 17), never
        creating a self-organized duplicate instead of RSVPing."""
        must_rsvp = id is not None or requester is not None
        attempts = 4 if must_rsvp else 1
        for attempt in range(attempts):
            candidates = ([self._get_event(id)] if id is not None
                          else self.list_events(slot))
            for event in candidates:
                if event is None:
                    continue
                item = event.raw or {}
                organizer = item.get("organizer", {}).get("email", "")
                if id is not None or requester.lower() == organizer.lower() or \
                        any(requester.lower() == a.lower() for a in event.attendees):
                    attendees = item.get("attendees", [])
                    for a in attendees:
                        if a.get("email", "").lower() == self._address.lower():
                            a["responseStatus"] = "accepted" if response != "declined" else "declined"
                    self._cal.events().patch(
                        calendarId=self._calendar_of.get(event.id, self.calendars[0]),
                        eventId=event.id, sendUpdates="all",
                        body={"attendees": attendees}).execute()
                    return event
            if must_rsvp and attempt < attempts - 1:
                time.sleep(5)
        if must_rsvp:
            raise InviteNotReady(
                f"the invite from {requester} isn't on the calendar yet — defer and retry; "
                f"refusing to create a duplicate event instead of RSVPing")
        # No invite found on the calendar — fall back to creating the event (asker path).
        return self.create_event(title, slot, requester)

    def _get_event(self, id):
        cal_id = self._calendar_of.get(id, self.calendars[0])
        try:
            return self._to_event(self._cal.events().get(calendarId=cal_id, eventId=id).execute(), cal_id)
        except HttpError:
            return None

    def _to_event(self, item, calendar_id=None):
        start = item.get("start", {}).get("dateTime")
        end = item.get("end", {}).get("dateTime")
        if not start or not end:  # all-day event → no dateTime; skip
            return None
        attendees = [a["email"] for a in item.get("attendees", []) if a.get("email")]
        # On a secondary calendar Google reports the CALENDAR's id as the organizer
        # email but flags it "self": true — that's still our own event, not a peer's.
        org = item.get("organizer", {})
        if calendar_id is not None:
            self._calendar_of[item["id"]] = calendar_id
        mine = next((a.get("responseStatus") for a in item.get("attendees", [])
                     if a.get("self")), None)
        return Event(item["id"], item.get("summary", "(no title)"),
                     from_iso(start), from_iso(end), attendees,
                     raw=item, calendar_id=calendar_id,
                     organizer=self._address if org.get("self") else org.get("email"),
                     response=("organizer" if org.get("self") and mine is None
                               else _GOOGLE_RESPONSE.get(mine)),
                     show_as="free" if item.get("transparency") == "transparent"
                     else "busy",
                     focus=((item.get("extendedProperties") or {}).get("private") or {})
                     .get("parley") == FOCUS_MARK)


# Google's attendee responseStatus → Event.response.
_GOOGLE_RESPONSE = {"accepted": "accepted", "tentative": "tentative",
                    "declined": "declined", "needsAction": "none"}


def _google_transparency(show_as):
    return "transparent" if show_as == "free" else "opaque"


def _extract_text(payload):
    """Pull the text/plain body out of a Gmail message payload."""
    if payload.get("mimeType") == "text/plain":
        data = payload.get("body", {}).get("data")
        if data:
            return base64.urlsafe_b64decode(data).decode("utf-8", "replace")
    for part in payload.get("parts", []):
        text = _extract_text(part)
        if text:
            return text
    return ""


def _extract_html(payload):
    """Pull the text/html body out of a Gmail message payload (recursive)."""
    if payload.get("mimeType") == "text/html":
        data = payload.get("body", {}).get("data")
        if data:
            return base64.urlsafe_b64decode(data).decode("utf-8", "replace")
    for part in payload.get("parts", []):
        html = _extract_html(part)
        if html:
            return html
    return ""


def _extract_body(payload):
    """The body Parley parses: the text/html part when it carries a hidden PARLEY
    payload (a wire message from a peer agent), else the text/plain part exactly as
    before. So a human's plain mail is untouched, while a peer's machine payload is
    read from the HTML comment the human never sees."""
    html = _extract_html(payload)
    if html and PARLEY_COMMENT in html:
        return html
    return _extract_text(payload)


def _find_calendar_node(payload):
    """Return the payload node holding the .ics (by MIME type or .ics filename)."""
    mime = payload.get("mimeType", "")
    filename = payload.get("filename", "") or ""
    if mime.startswith(("text/calendar", "application/ics")) or filename.lower().endswith(".ics"):
        return payload
    for part in payload.get("parts", []):
        node = _find_calendar_node(part)
        if node is not None:
            return node
    return None


def _mime_summary(payload, depth=0):
    """Flat lines describing the MIME tree, for debugging undetected invites."""
    body = payload.get("body", {})
    where = "inline-data" if body.get("data") else ("attachment" if body.get("attachmentId") else "-")
    name = payload.get("filename", "") or ""
    lines = ["  " * depth + f"{payload.get('mimeType', '?')} [{name}] ({where})"]
    for part in payload.get("parts", []):
        lines += _mime_summary(part, depth + 1)
    return lines


