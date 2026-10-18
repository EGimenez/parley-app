"""iCalendar (RFC 5545) — the pure parse/build helpers shared by backends.

Stdlib-only (re + datetime), no provider SDKs, so any Account can read an
incoming invite or build an outgoing REQUEST without dragging google-api / msal
in. Gmail reads invites with the parsers here; Yahoo both reads (IMAP) and sends
(SMTP) invites, so it uses the builder too.

  * parsing — `unfold`, `ics_value`, `ics_when`, `ics_email`, `invite_from_ics`
  * building — `build_ics_request` (a minimal METHOD:REQUEST VEVENT)
"""

import re
import uuid
from datetime import datetime, timezone

from parley.core import to_iso

# Windows timezone names → IANA. Microsoft surfaces (Graph mailboxSettings, and the
# TZIDs in the iCal invites Outlook/Graph emit) speak Windows names; zoneinfo speaks
# IANA. Shared here so the outlook backend and the invite parser use the same map —
# an unmapped Windows TZID was iteration 11's shifted-instant bug (a 12:00 UTC invite
# read as 12:00 machine-local).
WINDOWS_TZ = {
    "Romance Standard Time": "Europe/Madrid",
    "W. Europe Standard Time": "Europe/Berlin",
    "Central Europe Standard Time": "Europe/Budapest",
    "Central European Standard Time": "Europe/Warsaw",
    "GMT Standard Time": "Europe/London",
    "GTB Standard Time": "Europe/Athens",
    "UTC": "UTC",
    "Eastern Standard Time": "America/New_York",
    "Central Standard Time": "America/Chicago",
    "Mountain Standard Time": "America/Denver",
    "Pacific Standard Time": "America/Los_Angeles",
    # 2026-07-31 live bug (Madhulika Chakraborty / Capgemini invite, negotiation
    # mtg-i9ee48ad4): "India Standard Time" was missing, so an unresolved TZID fell
    # through to the floating-time branch below and the invite's 11:30 IST DTSTART
    # was read as 11:30 in the MACHINE's own zone (Europe/Madrid) instead of Kolkata
    # — a 3h30 silent shift (09:30 UTC computed vs. 06:00 UTC actual, confirmed via
    # parley_inspect.py against the real Yahoo CalDAV event).
    "India Standard Time": "Asia/Kolkata",
    "China Standard Time": "Asia/Shanghai",
    "Tokyo Standard Time": "Asia/Tokyo",
    "SE Asia Standard Time": "Asia/Bangkok",
    "Singapore Standard Time": "Asia/Singapore",
    "AUS Eastern Standard Time": "Australia/Sydney",
    "E. Australia Standard Time": "Australia/Brisbane",
    "New Zealand Standard Time": "Pacific/Auckland",
    "SA Pacific Standard Time": "America/Bogota",
    "SA Eastern Standard Time": "America/Cayenne",
    "E. South America Standard Time": "America/Sao_Paulo",
    "US Eastern Standard Time": "America/Indianapolis",
    "US Mountain Standard Time": "America/Phoenix",
    "Arabian Standard Time": "Asia/Dubai",
    "Arab Standard Time": "Asia/Riyadh",
    "Israel Standard Time": "Asia/Jerusalem",
    "South Africa Standard Time": "Africa/Johannesburg",
    "Egypt Standard Time": "Africa/Cairo",
    "Russian Standard Time": "Europe/Moscow",
    "FLE Standard Time": "Europe/Helsinki",
    "W. Central Africa Standard Time": "Africa/Lagos",
    "Pacific SA Standard Time": "America/Santiago",
    "Korea Standard Time": "Asia/Seoul",
    "Taipei Standard Time": "Asia/Taipei",
    "West Asia Standard Time": "Asia/Tashkent",
    "Central Asia Standard Time": "Asia/Almaty",
}


# --- parsing -----------------------------------------------------------------

def unfold(text):
    """Undo iCalendar line folding: a CRLF followed by a space/tab continues the
    previous line. Yahoo folds ORGANIZER/ATTENDEE, so parse on the joined text."""
    return re.sub(r"\r?\n[ \t]", "", text)


def ics_value(text, name):
    """First value of an iCalendar property (the params before ':' are stripped)."""
    m = re.search(rf"^{name}[^:\r\n]*:(.*)$", text, re.MULTILINE)
    return m.group(1).strip() if m else ""


def _zone(tzid):
    """A TZID → tzinfo, best-effort. Handles the Microsoft forms Graph/Outlook put in
    real invites: quoted TZIDs, `tzone://Microsoft/Utc`-style URIs, and Windows names
    ('Romance Standard Time'). None when genuinely unresolvable."""
    name = (tzid or "").strip().strip('"')
    if "microsoft" in name.lower():          # tzone://Microsoft/Utc, .../Custom, ...
        name = name.rsplit("/", 1)[-1]
        name = {"utc": "UTC"}.get(name.lower(), name)
    name = WINDOWS_TZ.get(name, name)
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:
        return None


def ics_when(text, prop):
    """Parse DTSTART/DTEND to a tz-aware UTC datetime, honoring Z / TZID / floating."""
    m = re.search(rf"^{prop}([^:\r\n]*):([0-9T]+Z?)", text, re.MULTILINE)
    if not m:
        return None
    params, value = m.group(1), m.group(2).strip()
    raw = value.rstrip("Z")
    try:
        dt = datetime.strptime(raw, "%Y%m%dT%H%M%S") if "T" in raw \
            else datetime.strptime(raw, "%Y%m%d")
    except ValueError:
        return None
    if value.endswith("Z"):
        return dt.replace(tzinfo=timezone.utc)
    tzid = re.search(r"TZID=([^;:]+)", params)
    zone = _zone(tzid.group(1)) if tzid else None
    if zone:
        return dt.replace(tzinfo=zone).astimezone(timezone.utc)
    # Floating / unknown tz → interpret in this machine's local timezone.
    local = datetime.now().astimezone().tzinfo
    return dt.replace(tzinfo=local).astimezone(timezone.utc)


def ics_email(value):
    return value.split("mailto:")[-1].strip().lower() if value else ""


def invite_from_ics(ics):
    """Parse an .ics blob into an invite dict, or None.

    METHOD:REQUEST → {title, start, end, organizer} (a new/updated invite).
    METHOD:REPLY   → the same plus {response: accepted|declined|tentative,
                     responder} — the attendee's RSVP to OUR invite (iteration 17:
                     these used to be classifier-ignored, so a decline vanished).
    Anything else (CANCEL, …) → None."""
    ics = unfold(ics)
    method = (ics_value(ics, "METHOD") or "REQUEST").upper()
    if method not in ("REQUEST", "REPLY"):
        return None
    # Restrict to the VEVENT block — a VTIMEZONE block also has DTSTART lines.
    vevent = re.search(r"BEGIN:VEVENT(.*?)END:VEVENT", ics, re.DOTALL)
    scope = vevent.group(1) if vevent else ics
    start, end = ics_when(scope, "DTSTART"), ics_when(scope, "DTEND")
    if not start or not end:
        return None
    out = {"title": ics_value(scope, "SUMMARY") or "(meeting)",
           "start": to_iso(start), "end": to_iso(end),
           "organizer": ics_email(ics_value(scope, "ORGANIZER")),
           # S1 (iteration 21 phase 2): the iCalUID is THE meeting identity — stable
           # across resends and moves, it keys the invite's negotiation ref.
           "uid": ics_value(scope, "UID")}
    if method == "REPLY":
        att = re.search(r"^ATTENDEE([^:\r\n]*):(.*)$", scope, re.MULTILINE)
        partstat = re.search(r"PARTSTAT=([A-Za-z-]+)", att.group(1)) if att else None
        out["response"] = {"ACCEPTED": "accepted", "DECLINED": "declined",
                           "TENTATIVE": "tentative"}.get(
            (partstat.group(1).upper() if partstat else ""), "accepted")
        out["responder"] = ics_email(att.group(2)) if att else ""
    return out


def event_from_vevent(ics):
    """Parse ONE stored calendar object (a CalDAV VEVENT, no METHOD) into
    {uid, title, start, end, organizer, attendees[]}, or None. The read half of
    Yahoo CalDAV (iteration 17)."""
    ics = unfold(ics)
    vevent = re.search(r"BEGIN:VEVENT(.*?)END:VEVENT", ics, re.DOTALL)
    scope = vevent.group(1) if vevent else ics
    start, end = ics_when(scope, "DTSTART"), ics_when(scope, "DTEND")
    if not start or not end:
        return None
    attendees = [ics_email(m.group(1))
                 for m in re.finditer(r"^ATTENDEE[^:\r\n]*:(.*)$", scope, re.MULTILINE)]
    return {"uid": ics_value(scope, "UID"),
            "title": ics_value(scope, "SUMMARY") or "(meeting)",
            "start": start, "end": end,
            "organizer": ics_email(ics_value(scope, "ORGANIZER")),
            "attendees": [a for a in attendees if a]}


# --- building ----------------------------------------------------------------

def _z(dt):
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _vevent_lines(uid, organizer, attendee, title, start, end, sequence=0):
    # `attendee` may be one address or a list (multiparty, iteration 21 phase 3):
    # one ATTENDEE line each, same VEVENT.
    attendees = attendee if isinstance(attendee, (list, tuple)) else [attendee]
    return ([
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{_z(datetime.now(timezone.utc))}",
        f"DTSTART:{_z(start)}", f"DTEND:{_z(end)}",
        f"SUMMARY:{title}",
        f"ORGANIZER;CN={organizer}:mailto:{organizer}"]
        + [f"ATTENDEE;RSVP=TRUE;CN={a}:mailto:{a}" for a in attendees]
        + ["STATUS:CONFIRMED", f"SEQUENCE:{sequence}", "END:VEVENT"])


def build_ics_request(organizer, attendee, title, start, end, uid=None):
    """A minimal METHOD:REQUEST VEVENT (UTC, Z-form), CRLF-joined per RFC 5545.

    The wire an agentless peer uses to *send* an invite (Yahoo → the recipient's
    inbox); Gmail/Graph auto-add such an .ics on receipt so the recipient's agent
    can negotiate/RSVP with no human in the loop."""
    lines = (["BEGIN:VCALENDAR", "PRODID:-//Parley//Account//EN", "VERSION:2.0",
              "METHOD:REQUEST"]
             + _vevent_lines(uid or f"{uuid.uuid4()}@parley", organizer, attendee,
                             title, start, end)
             + ["END:VCALENDAR"])
    return "\r\n".join(lines) + "\r\n"


def build_ics_reply(uid, organizer, attendee, title, start, end,
                    response="ACCEPTED"):
    """A METHOD:REPLY VEVENT — the mail-borne RSVP a calendar without a scheduling
    API (Yahoo) sends back to the organizer. `uid` MUST be the invite's own UID and
    `attendee` the invited address, or the organizer's provider won't match the
    seat. Only the replying attendee appears in a REPLY (RFC 5546 §3.2.3)."""
    lines = ["BEGIN:VCALENDAR", "PRODID:-//Parley//Account//EN", "VERSION:2.0",
             "METHOD:REPLY",
             "BEGIN:VEVENT",
             f"UID:{uid}",
             f"DTSTAMP:{_z(datetime.now(timezone.utc))}",
             f"DTSTART:{_z(start)}", f"DTEND:{_z(end)}",
             f"SUMMARY:{title}",
             f"ORGANIZER;CN={organizer}:mailto:{organizer}",
             f"ATTENDEE;PARTSTAT={response.upper()};CN={attendee}:mailto:{attendee}",
             "SEQUENCE:0",
             "END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(lines) + "\r\n"


def build_ics_event(uid, organizer, attendee, title, start, end, sequence=0):
    """A stored calendar object for CalDAV PUT — same VEVENT, **no METHOD** (RFC
    4791 forbids METHOD on stored objects; a server may reject or misfile it)."""
    lines = (["BEGIN:VCALENDAR", "PRODID:-//Parley//Account//EN", "VERSION:2.0"]
             + _vevent_lines(uid, organizer, attendee, title, start, end, sequence)
             + ["END:VCALENDAR"])
    return "\r\n".join(lines) + "\r\n"
