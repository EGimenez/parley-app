"""Outlook / Microsoft 365 backend for Parley — mail + calendar via Microsoft Graph.

Implements the Account I/O surface against an Outlook.com / M365 account in
the cloud. Reuses the Round-1 Graph delta-query inbox polling, and adds sending
and calendar read/write.

Delegated scopes (consented once in the browser):
  * Mail.Read           — watch the inbox
  * Mail.Send           — send negotiation / confirmation mail
  * Calendars.ReadWrite — read free/busy and create/move events
  * MailboxSettings.Read — read the mailbox's configured timezone (without it,
                           `/me/mailboxSettings` 403s and the agent silently degrades
                           every local time to UTC)

Needs an Azure app registration; the public-client id is carried **inline** in the
cfg's `[[account]]` `client_id` (iteration 07-0). The generated MSAL token cache + delta
cursor live under `~/.parley/state/<name>/`, never in the repo tree. See README.md.
"""

import os
import time
from datetime import timedelta, timezone

import msal
import requests

from parley.connectors.accounts.account import Account, InviteNotReady
from parley.connectors.accounts.ics import WINDOWS_TZ as _WINDOWS_TZ
from parley.core import (FOCUS_MARK, Event, Message, as_addresses, from_iso, render_parts,
                         to_iso)

GRAPH = "https://graph.microsoft.com/v1.0"
# Delegated Graph scopes the agent requests at CONSENT (interactive). The setup wizard
# derives the app-registration permission list from THIS set (install_config/backends/
# outlook.py), so the two can never drift. Calendars.ReadWrite.Shared lets the agent
# book on calendars shared with the user on work/school accounts (iteration 26).
SCOPES = ["Mail.Read", "Mail.Send", "Calendars.ReadWrite",
          "Calendars.ReadWrite.Shared", "MailboxSettings.Read"]
# Scopes required for a SILENT token. A personal outlook.com account can't grant
# Calendars.ReadWrite.Shared — it returns a token WITHOUT it — so asking for the full
# set in acquire_token_silent would never match the cached token and would relaunch the
# browser on every Graph call (the endless "Authentication complete" loop). Silent asks
# only for what every account, personal included, actually grants; .Shared stays a
# best-effort bonus obtained at consent time for the accounts that support it.
SILENT_SCOPES = ["Mail.Read", "Mail.Send", "Calendars.ReadWrite", "MailboxSettings.Read"]
AUTHORITY = "https://login.microsoftonline.com/common"

INITIAL_DELTA_URL = (
    f"{GRAPH}/me/mailFolders/inbox/messages/delta"
    "?$select=subject,from,body,receivedDateTime,internetMessageId,toRecipients,ccRecipients"
)
# Ask Graph to return calendar times already expressed in UTC.
_UTC_PREFER = {"Prefer": 'outlook.timezone="UTC"'}


class OutlookAccount(Account):
    name = "outlook"

    def __init__(self, config=None):
        """Bind this connection to one account's settings (config.py:AccountConfig).

        Per-account so two Outlook accounts can run in one process with separate token
        caches. The Azure public-client id is inline in the cfg; generated files (MSAL
        cache + delta cursor) live under the account's `state_dir`
        (`~/.parley/state/<name>/`). No env fallbacks (iteration 07-0)."""
        settings = getattr(config, "settings", None) or {}
        self.client_id = settings.get("client_id", "")
        # Generated state under ~/.parley/state/<name>/ (config injects state_dir; the
        # defensive default covers a hand-built AccountConfig).
        state_dir = settings.get("state_dir") or os.path.join(
            os.path.expanduser("~"), ".parley", "state", self.name)
        self.token_cache_file = settings.get("msal_cache") or os.path.join(state_dir, "msal_cache.json")
        self.delta_link_file = settings.get("delta") or os.path.join(state_dir, "delta.txt")
        # Busy/free is the union across all of them; the first is the booking target.
        # "primary"/empty → the mailbox default calendar (/me/...).
        self.calendars = list(getattr(config, "calendars", None) or []) or ["primary"]

    def connect(self):
        if not self.client_id:
            raise SystemExit("Set client_id (Azure app id) for this account. See README.md.")
        self._cache = msal.SerializableTokenCache()
        if os.path.exists(self.token_cache_file):
            self._cache.deserialize(open(self.token_cache_file).read())
        self._app = msal.PublicClientApplication(
            self.client_id, authority=AUTHORITY, token_cache=self._cache)

        token = self._token()
        # Our own address comes from the signed-in MSAL account — no /me call
        # (which would need the User.Read scope we deliberately don't request).
        accounts = self._app.get_accounts()
        self._address = accounts[0]["username"] if accounts else ""
        self._delta_link = self._baseline(token)

    def address(self):
        return self._address

    def timezone(self):
        """The mailbox's configured timezone (`/me/mailboxSettings`), cached. Graph
        usually reports a Windows name ('Romance Standard Time'); we map the common ones
        to IANA and pass anything else through (the model still reads Windows names).
        Falls back to 'UTC' on any error."""
        if getattr(self, "_tz", None):
            return self._tz
        self._tz = "UTC"
        try:
            raw = self._get(f"{GRAPH}/me/mailboxSettings/timeZone")
            name = raw.get("value") if isinstance(raw, dict) else None
            if name:
                self._tz = _WINDOWS_TZ.get(name, name)
        except Exception:  # noqa: BLE001 — best-effort; UTC on any API error
            pass
        return self._tz

    # -- Contacts -------------------------------------------------------------

    def find_contact(self, name):
        """Best-effort: ask Graph's people index (`/me/people?$search=`) for `name`
        and return the first email. Degrades to None on any error — notably a 403
        when `People.Read` wasn't consented (we don't widen SCOPES here, since MSAL
        would force an interactive re-consent; add People.Read to enable it). An
        input that is already an address passes straight through."""
        if not name:
            return None
        if "@" in name:
            return name
        try:
            url = (f"{GRAPH}/me/people?$search=\"{name}\""
                   "&$select=displayName,scoredEmailAddresses&$top=1")
            for person in self._get(url).get("value", []):
                for e in person.get("scoredEmailAddresses", []):
                    if e.get("address"):
                        return e["address"]
        except Exception as err:  # missing scope (403), network …
            print(f"[{self.name}] (contacts) resolve '{name}' unavailable: {err}")
        return None

    # -- Auth -----------------------------------------------------------------

    def _token(self):
        accounts = self._app.get_accounts()
        # Silent asks for SILENT_SCOPES (what every account grants) so a personal
        # account's cached token matches and we DON'T relaunch the browser; consent
        # still requests the broader SCOPES (incl. shared-calendar) interactively.
        result = self._app.acquire_token_silent(SILENT_SCOPES, account=accounts[0]) if accounts else None
        if not result:
            # The browser only opens here. Say WHY, loudly — a recurring line in the
            # log is the fingerprint of a token-cache problem (lost account, failed
            # silent refresh) rather than a one-off first-run consent.
            why = "no cached account" if not accounts else "silent refresh returned nothing"
            print(f"[{self.name}] opening the browser for Microsoft sign-in "
                  f"({why}); cache: {self.token_cache_file}")
            result = self._app.acquire_token_interactive(SCOPES)
        if self._cache.has_state_changed:
            # Atomic write: a plain open('w') truncates first, so a concurrent reader
            # (another daemon, a parallel Graph call) could load a half-written cache,
            # lose the account, and trigger a fresh browser login. Write + os.replace.
            d = os.path.dirname(self.token_cache_file) or "."
            os.makedirs(d, exist_ok=True)
            tmp = self.token_cache_file + ".tmp"
            with open(tmp, "w") as f:
                f.write(self._cache.serialize())
            os.replace(tmp, self.token_cache_file)
        if "access_token" not in result:
            raise SystemExit(f"Auth failed: {result.get('error_description')}")
        return result["access_token"]

    def _headers(self, extra=None):
        h = {"Authorization": f"Bearer {self._token()}"}
        if extra:
            h.update(extra)
        return h

    def _get(self, url, extra=None):
        resp = requests.get(url, headers=self._headers(extra), timeout=30)
        resp.raise_for_status()
        return resp.json()

    # -- Mail -----------------------------------------------------------------

    def _baseline(self, token):
        if os.path.exists(self.delta_link_file):
            return open(self.delta_link_file).read().strip()
        _, delta_link = self._drain(INITIAL_DELTA_URL)
        self._save_delta(delta_link)
        return delta_link

    def _drain(self, url):
        values = []
        while True:
            data = self._get(url)
            values.extend(data.get("value", []))
            if "@odata.nextLink" in data:
                url = data["@odata.nextLink"]
                continue
            return values, data.get("@odata.deltaLink")

    def _save_delta(self, delta_link):
        if delta_link:
            os.makedirs(os.path.dirname(self.delta_link_file) or ".", exist_ok=True)
            with open(self.delta_link_file, "w") as f:
                f.write(delta_link)

    # -- Calendar picker (setup + runtime ambiguity, 2026-08-31) --------------

    def list_calendars(self):
        """This account's calendars, `[{"name", "id"}]` — empty when there's
        nothing to pick from a single default."""
        try:
            items = self._get(f"{GRAPH}/me/calendars?$select=id,name").get("value", [])
        except Exception as err:  # noqa: BLE001 — best-effort; caller treats [] as "nothing to pick"
            print(f"[{self.name}] (calendars) couldn't list: {err}")
            return []
        return [{"name": c.get("name") or c["id"], "id": c["id"]} for c in items]

    def select_calendar(self, calendar_id):
        """Pin the ambiguous-calendar ask's answer as the sole booking target for
        this run (add `calendars: [...]` to the cfg to skip the ask next time)."""
        self.calendars = [calendar_id]
        print(f"[{self.name}] calendar set to {calendar_id} (add `calendars: [{calendar_id}]` "
              "to the cfg to skip this next time).")

    def list_messages(self):
        raw, self._delta_link = self._drain(self._delta_link)
        self._save_delta(self._delta_link)
        messages = []
        for item in raw:
            if "@removed" in item:
                continue
            sender = item.get("from", {}).get("emailAddress", {}).get("address", "")
            body = (item.get("body") or {}).get("content", "")
            # internetMessageId threads agent-to-agent replies; In-Reply-To/References
            # live in internetMessageHeaders (an extra per-message GET on the delta),
            # so on Graph a broken-thread reply falls to the (peer, topic) fuzzy path.
            messages.append(Message(item["id"], sender, item.get("subject", ""), body,
                                    invite=self._invite_for(item),
                                    message_id=item.get("internetMessageId"),
                                    recipients=_graph_recipients(item)))
        return messages

    # Graph meetingMessageType → RSVP outcome (iteration 17: fates recorded).
    _RESPONSE_TYPES = {"meetingAccepted": "accepted",
                       "meetingDeclined": "declined",
                       "meetingTentativelyAccepted": "tentative"}

    def _invite_for(self, item):
        """The invite dict a Graph event-mail carries, or None for ordinary mail.
        Graph marks these with a derived `@odata.type` (eventMessage*); the delta feed
        doesn't inline the linked event, so fetch it on demand. A `meetingRequest` →
        {title,start,end,organizer} (an invite to us). An RSVP response
        (meetingAccepted/Declined/TentativelyAccepted) → the same plus
        {response, responder} — the fate of an invite WE sent (iteration 17).
        Cancellations stay plain mail."""
        odata = str(item.get("@odata.type", "")).lower()
        if "eventmessage" not in odata:
            return None
        try:
            # No top-level `$select`: selecting the derived-type property
            # `meetingMessageType` on the base `message` resource 400s on Outlook.com
            # (2026-07-07 A.1 post-mortem — EVERY gmail→outlook invite hit this 400, the
            # exception below swallowed it, the invite degraded to plain mail, and the
            # responder never RSVP'd). The expand returns meetingMessageType + sender by
            # default anyway; casting the path to the derived type also 400s here.
            data = self._get(
                f"{GRAPH}/me/messages/{item['id']}"
                "?$expand=microsoft.graph.eventMessage/event"
                "($select=subject,start,end,organizer,iCalUId)",
                _UTC_PREFER)
            mtype = data.get("meetingMessageType")
            response = self._RESPONSE_TYPES.get(mtype)
            if mtype != "meetingRequest" and response is None:
                return None
            ev = data.get("event") or {}
            start = (ev.get("start") or {}).get("dateTime")
            end = (ev.get("end") or {}).get("dateTime")
            if not (start and end):
                return None
            organizer = ((ev.get("organizer") or {}).get("emailAddress") or {}).get("address", "")
            out = {"title": ev.get("subject") or item.get("subject", ""),
                   "start": to_iso(_parse_graph_time(start)),
                   "end": to_iso(_parse_graph_time(end)),
                   "organizer": organizer,
                   "uid": ev.get("iCalUId")}
            if response:
                sender = ((data.get("sender") or {}).get("emailAddress") or {}).get("address", "")
                out["response"] = response
                out["responder"] = sender
            return out
        except Exception as err:  # noqa: BLE001 — invite metadata is best-effort; plain mail flows on
            # Loud, not silent (live post-mortem 2026-07-05: this returned None
            # quietly, the invite entered as plain mail, and the human was asked a
            # second time for a meeting that was already settled).
            print(f"[{self.name}] couldn't resolve invite metadata for message "
                  f"{str(item.get('id', '?'))[:16]}…: {err} — treating it as plain mail")
            return None

    def send_message(self, to, subject, body, thread=None):
        # `to` may be one address or several: a meeting with four people is ONE mail
        # addressed to the four of them, so every invitee sees who else was asked.
        # `thread` is accepted and NOT used: Graph refuses In-Reply-To/References
        # on sendMail (internetMessageHeaders takes X- headers only), so real threading
        # needs the /reply endpoint against the original message id - real work,
        # not done here. Said out loud so nobody reads this as "handled".
        # A wire message goes as HTML so its hidden PARLEY comment survives; plain mail
        # stays Text exactly as before (Graph returns the same body.content on fetch).
        text, html = render_parts(body)
        ctype, content = ("HTML", html) if html is not None else ("Text", text)
        payload = {"message": {"subject": subject,
                               "body": {"contentType": ctype, "content": content},
                               "toRecipients": [{"emailAddress": {"address": addr}}
                                                for addr in as_addresses(to)]},
                   "saveToSentItems": True}
        resp = requests.post(f"{GRAPH}/me/sendMail", headers=self._headers(),
                             json=payload, timeout=30)
        resp.raise_for_status()

    # -- Calendar -------------------------------------------------------------

    def _cal_base(self, cal_id):
        """Graph URL base for one calendar. 'primary'/empty → the mailbox default
        (/me/…); a named id → /me/calendars/{id}/…."""
        if not cal_id or cal_id == "primary":
            return f"{GRAPH}/me"
        return f"{GRAPH}/me/calendars/{cal_id}"

    def list_events(self, window):
        # Express the window in UTC (the Prefer header tells Graph to read these
        # bounds as UTC) and pad it by an hour, so a timezone/boundary quirk can't
        # hide the event; the overlaps() filter below keeps the result exact.
        lo = (window.start - timedelta(hours=1)).astimezone(timezone.utc)
        hi = (window.end + timedelta(hours=1)).astimezone(timezone.utc)
        query = (f"/calendarView"
                 f"?startDateTime={lo.strftime('%Y-%m-%dT%H:%M:%S')}"
                 f"&endDateTime={hi.strftime('%Y-%m-%dT%H:%M:%S')}"
                 "&$select=subject,start,end,attendees,organizer,responseStatus,"
                 "showAs,categories"
                 "&$orderby=start/dateTime")
        # Union the free/busy view across every calendar on this account.
        events = []
        for cal_id in self.calendars:
            for raw in self._get(self._cal_base(cal_id) + query, _UTC_PREFER).get("value", []):
                event = self._to_event(raw, cal_id)
                if event is not None:
                    events.append(event)
        hits = [e for e in events if e.slot.overlaps(window)]
        if not hits:
            print(f"[outlook] (debug) calendarView {lo:%m-%d %H:%M}–{hi:%H:%M}Z "
                  f"returned {len(events)} event(s): "
                  + (" | ".join(f"'{e.title}' {e.start:%H:%M}-{e.end:%H:%M}Z "
                                f"att={e.attendees}" for e in events) or "(none)"))
        return hits

    def update_event(self, id, changes):
        # Graph event ids are mailbox-global, so /me/events/{id} works regardless
        # of which calendar the event lives on.
        body = {}
        if "start" in changes:
            body["start"] = _graph_time(changes["start"])
        if "end" in changes:
            body["end"] = _graph_time(changes["end"])
        if "show_as" in changes:
            body["showAs"] = changes["show_as"]
        if "title" in changes:
            body["subject"] = changes["title"]
        resp = requests.patch(f"{GRAPH}/me/events/{id}", headers=self._headers(),
                              json=body, timeout=30)
        resp.raise_for_status()

    def respond_event(self, response, *, id=None, requester=None, slot=None, title=None):
        """RSVP the invite `requester` already sent (Graph auto-adds it to the
        calendar as tentative) instead of creating a duplicate. Only an event
        *organized by* the requester is RSVP-able (Graph rejects an organizer
        responding to their own event).

        A known `id` answers directly; otherwise searches `slot`, retrying while
        Graph materializes the tentative event (iteration 17 — the 2026-07-05 F22
        race: the human accepted ~20s after the invite mail, the event wasn't on
        the calendar yet, and the old create-fallback made a duplicate
        organized-by-self event while the real invite stayed unanswered). If it
        still isn't there, raise — never create."""
        req = (requester or "").lower() if requester else None
        must_rsvp = id is not None or requester is not None
        attempts = 4 if must_rsvp else 1
        verb = "decline" if response == "declined" else "accept"
        for attempt in range(attempts):
            candidates = ([self._get_event(id)] if id is not None
                          else self.list_events(slot))
            for event in candidates:
                if event is None:
                    continue
                raw = event.raw or {}
                organizer = ((raw.get("organizer") or {}).get("emailAddress") or {}).get("address", "")
                if id is not None or (organizer.lower() == req
                                      and organizer.lower() != self._address.lower()):
                    resp = requests.post(f"{GRAPH}/me/events/{event.id}/{verb}",
                                         headers=self._headers(),
                                         json={"sendResponse": True}, timeout=30)
                    resp.raise_for_status()
                    return event
            if must_rsvp and attempt < attempts - 1:
                time.sleep(5)  # the invite's event is usually seconds behind the mail
        if must_rsvp:
            raise InviteNotReady(
                f"the invite from {requester} isn't on the calendar yet — defer and retry; "
                f"refusing to create a duplicate event instead of RSVPing")
        return self.create_event(title, slot, requester)

    def _get_event(self, id):
        try:
            return self._to_event(self._get(f"{GRAPH}/me/events/{id}"))
        except Exception:  # noqa: BLE001 — 404 or transient; caller treats None as "not found yet"
            return None

    def create_event(self, title, slot, attendee):
        booking = self.calendars[0]
        attendees = attendee if isinstance(attendee, (list, tuple)) else [attendee]
        resp = requests.post(
            f"{self._cal_base(booking)}/events", headers=self._headers(),
            json={"subject": title,
                  "start": _graph_time(slot.start), "end": _graph_time(slot.end),
                  "attendees": [{"emailAddress": {"address": a}, "type": "required"}
                                for a in attendees]},
            timeout=30)
        resp.raise_for_status()
        return self._to_event(resp.json(), booking)

    def create_hold(self, title, slot, show_as="free"):
        # No attendees, so Graph sends nothing: a plain appointment on our calendar.
        booking = self.calendars[0]
        resp = requests.post(
            f"{self._cal_base(booking)}/events", headers=self._headers(),
            json={"subject": title,
                  "start": _graph_time(slot.start), "end": _graph_time(slot.end),
                  "showAs": show_as, "categories": [FOCUS_MARK],
                  "isReminderOn": False},
            timeout=30)
        resp.raise_for_status()
        return self._to_event(resp.json(), booking)

    def delete_event(self, id):
        # Graph event ids are mailbox-global, so /me/events/{id} works regardless
        # of which calendar the event lives on (same as update_event).
        resp = requests.delete(f"{GRAPH}/me/events/{id}",
                               headers=self._headers(), timeout=30)
        resp.raise_for_status()

    def _to_event(self, item, calendar_id=None):
        start = item.get("start", {}).get("dateTime")
        end = item.get("end", {}).get("dateTime")
        if not start or not end:
            return None
        attendees = [a["emailAddress"]["address"]
                     for a in item.get("attendees", []) if a.get("emailAddress")]
        # Graph keeps the organizer OUT of `attendees`, so a meeting organized by
        # the counterpart would otherwise be unfindable by find_event (the bug
        # that made an earlier real run decline). Fold the organizer in.
        org = item.get("organizer", {}).get("emailAddress", {}).get("address")
        if org and org.lower() not in (a.lower() for a in attendees):
            attendees.append(org)
        return Event(item["id"], item.get("subject", "(no subject)"),
                     _parse_graph_time(start), _parse_graph_time(end), attendees,
                     raw=item, calendar_id=calendar_id, organizer=org,
                     response=_GRAPH_RESPONSE.get(
                         (item.get("responseStatus") or {}).get("response")),
                     show_as=_GRAPH_SHOW_AS.get(item.get("showAs")),
                     focus=FOCUS_MARK in (item.get("categories") or []))


# Graph's responseStatus.response → Event.response.
_GRAPH_RESPONSE = {"accepted": "accepted", "tentativelyAccepted": "tentative",
                   "declined": "declined", "notResponded": "none", "none": "none",
                   "organizer": "organizer"}


# Graph's showAs → Event.show_as. Out of office blocks like busy; working elsewhere
# does not.
_GRAPH_SHOW_AS = {"free": "free", "workingElsewhere": "free", "tentative": "tentative",
                  "busy": "busy", "oof": "busy"}


def _graph_recipients(item):
    """Everyone a Graph mail was addressed to: To: plus Cc:, bare addresses.

    It is how a reply-to-all is told apart from a private aside, which decides whether
    Parley answers the group or one person. Both fields are in the delta `$select`;
    a mail that somehow carries neither yields [], read downstream as "cannot tell"
    rather than as "private"."""
    out = []
    for key in ("toRecipients", "ccRecipients"):
        for entry in item.get(key) or []:
            out.append((entry.get("emailAddress") or {}).get("address") or "")
    return as_addresses(out)


def _graph_time(dt):
    return {"dateTime": dt.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": "UTC"}


def _parse_graph_time(value):
    # Graph returns e.g. "2026-06-25T15:00:00.0000000" (UTC, no offset).
    dt = from_iso(value.split(".")[0])
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
