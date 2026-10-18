"""Wire — no longer a protocol, just the pieces that must stay deterministic
(2026-08-31 refactor — see docs/iterations/refactor-plan.md "Wire protocol —
dissolves, not just shrinks"). The Brain writes outbound peer mail itself, in
its own words, through `AccountsGate.send` — there is no template builder here
anymore, and no JSON hidden in an HTML comment. What's left:

  * `new_ref()` — mint the one correlation id for a meeting.
  * `extract_ref(text)` — pull a visible `(ref: mtg-xyz)` tag back out of a
    message's subject/body. The Brain is instructed to always include one on
    a meeting's first send and thread it through every later message; infra
    reads it back the same way it already reads the ask/answer channel's ref.
  * `invite_negotiation_id` + the invite branch of `parse` — pure `.ics`
    territory (a real calendar invite), unrelated to anything Parley authors.
  * `status_notification` — recognizing a THIRD PARTY's fixed text
    ("Accepted:"/"Declined:" — Gmail/Outlook's own notifications), not
    anything Parley writes.

`parse(message)` ties these together: the payload a message carries (possibly
just a bare `negotiation_id` with no `type` at all — the Brain reads
everything else straight from the prose, the same way it already reads a
genuine human's free-text reply). Correlation from there — thread, then
(peer, topic), ambiguous → ask — is `Agent`'s job, not this module's.
"""

import re
import uuid


def new_ref():
    """Mint the ONE correlation ref for a meeting: `mtg-<hex8>`. Everything about
    the meeting — peer mail, ShortTermMemory, the log, escalations — keys on this
    single id; reopens append an epoch (`mtg-x#2`)."""
    return "mtg-" + uuid.uuid4().hex[:8]


# The visible ref tag the Brain is instructed to write on outbound mail, e.g.
# "... (ref: mtg-ab12cd34)" — plain text, survives a forward or a mail gateway
# that would have mangled a hidden HTML comment. Same shape the ask/answer
# channel already uses.
_REF_RE = re.compile(r"\(ref:\s*([A-Za-z0-9#_-]{6,})\)", re.I)


def extract_ref(text):
    """The visible ref in `text` (subject + body, typically), or None."""
    m = _REF_RE.search(text or "")
    return m.group(1) if m else None


# A provider's plain RSVP-notification subject — "Accepted: X", "Declined: X @ …",
# "Tentatively accepted: X" — carries NO iCal REPLY / Graph response object, so without
# this it slips through as noise and the asker never learns its invite's fate.
_STATUS_RE = re.compile(
    r"^\s*(?:re:\s*)?(accepted|declined|tentatively accepted|tentative)\b\s*"
    r"(?:event|invitation)?:?\s*(.*)$", re.I)
_STATUS_RESPONSE = {"accepted": "accepted", "declined": "declined",
                    "tentative": "tentative", "tentatively accepted": "tentative"}


def status_notification(message):
    """An `rsvp_reply` payload synthesized from a provider's plain status-notification
    subject ("Declined: <title>"), or None. Carries no times (the subject rarely has
    the absolute instants), so correlation falls back to (peer, title) in memory."""
    m = _STATUS_RE.match(getattr(message, "subject", "") or "")
    if not m:
        return None
    response = _STATUS_RESPONSE.get(m.group(1).strip().lower())
    if not response:
        return None
    title = re.split(r"\s+@\s+", m.group(2).strip())[0].strip() or "(meeting)"
    return {"type": "rsvp_reply", "response": response,
            "responder": message.sender, "title": title}


# The organizer cancelled — "Canceled event: X @ …" (Google), "Canceled: X" (Outlook).
# METHOD:CANCEL carries no parsable invite (ics.invite_from_ics returns None for it),
# and over Graph the notice arrives as ordinary mail with no .ics at all, so the subject
# is the one cross-provider signal — the same reasoning as _STATUS_RE above.
_CANCEL_RE = re.compile(
    r"^\s*(?:re:\s*)?cancell?ed\b\s*(?:event|invitation|meeting)?:?\s+(.+)$", re.I)


def cancellation_notification(message):
    """A `cancellation` payload synthesized from the organizer's cancellation notice,
    or None. Deterministic infra business (2026-09-06): before this, a cancellation
    matched nothing, reached the brain as plain mail, cost a model turn to conclude
    "nothing to do" — and then the no-silent-inaction backstop escalated it, so the
    user got a toast with the raw cancellation email in it every time ANY meeting was
    cancelled. Carries no times (the subject rarely has the absolute instants), so
    correlation falls back to (peer, title), like status_notification."""
    m = _CANCEL_RE.match(getattr(message, "subject", "") or "")
    if not m:
        return None
    title = re.split(r"\s+@\s+", m.group(1).strip())[0].strip()
    if not title:
        return None
    return {"type": "cancellation", "organizer": message.sender, "title": title}


def invite_negotiation_id(organizer, start, end, uid=None):
    """The deterministic ref of a bare calendar invite.

    Preferred key: the invite's **iCalUID** — stable across resends AND across moves,
    so a rescheduled invite maps to the SAME negotiation (the reopen path can see it's
    the same meeting, not a new one; see ShortTermMemory.find_by_uid). Both sides
    derive the same ref from the same invite.

    Fallback (no UID surfaced): organizer + slot instants, as before — resends still
    map to one id, though a moved invite then arrives as a new meeting.

    `mtg-i` prefix: same `mtg-` namespace as minted refs, `i` marks invite-derived."""
    import hashlib
    key = (f"uid|{uid.strip().lower()}" if uid and str(uid).strip()
           else f"{(organizer or '').lower()}|{start}|{end}").encode()
    return "mtg-i" + hashlib.sha1(key).hexdigest()[:8]


def parse(message):
    """The payload `message` carries, or `{}`.

    A real calendar invite (message.invite) normalizes into a `meeting_request`
    (via_invite=True) or, if it's a REPLY to an invite WE sent, an `rsvp_reply` —
    both pure `.ics` territory. A provider's plain status notification
    ("Declined: X") also becomes an `rsvp_reply`. Anything else just gets its
    visible ref extracted, if it has one — no `type`, nothing more asserted;
    the Brain reads the actual content itself."""
    inv = getattr(message, "invite", None)
    if inv:
        organizer = inv.get("organizer") or message.sender
        neg_id = invite_negotiation_id(organizer, inv["start"], inv["end"], uid=inv.get("uid"))
        if inv.get("response"):
            # METHOD:REPLY / Graph meetingAccepted|Declined: the fate of an invite
            # WE organized — never a meeting_request.
            return {"type": "rsvp_reply", "via_invite": True,
                   "response": inv["response"],
                   "responder": inv.get("responder") or message.sender,
                   "negotiation_id": neg_id, "title": inv.get("title", "(meeting)"),
                   "start": inv["start"], "end": inv["end"], "from": organizer,
                   "uid": inv.get("uid")}
        return {"type": "meeting_request", "via_invite": True,
               "negotiation_id": neg_id, "title": inv.get("title", "(meeting)"),
               "start": inv["start"], "end": inv["end"], "from": organizer,
               "uid": inv.get("uid")}
    cancelled = cancellation_notification(message)
    if cancelled:
        return cancelled
    notif = status_notification(message)
    if notif:
        return notif
    ref = extract_ref(f"{getattr(message, 'subject', '') or ''}\n"
                      f"{getattr(message, 'body', '') or ''}")
    if ref:
        return {"negotiation_id": ref, "from": getattr(message, "sender", None)}
    return {}
