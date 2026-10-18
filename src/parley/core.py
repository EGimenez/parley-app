"""Parley core — value types, datetime helpers, and the wire protocol.

These are the pure, dependency-free pieces shared by every layer (connections,
policy, agent, CLI). Kept here so `--selftest` can import the choreography without
pulling in any provider's API client libraries.

The wire protocol: a Parley message is an ordinary email a **human can read and act
on**. The human-readable prose IS the message; the machine payload for a peer agent
rides *hidden* inside it — an HTML comment (`<!-- PARLEY {json} -->`) a mail client
never renders. So a person on the other end sees clean prose, while an agent reads
the same JSON it always did. The legacy visible `BEGIN/END PARLEY` block is still
parsed on the way in (back-compat), never emitted.
"""

import html as _html
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import getaddresses


# --- Wire protocol -----------------------------------------------------------

PARLEY_BEGIN = "-----BEGIN PARLEY-----"
PARLEY_END = "-----END PARLEY-----"

_BLOCK_RE = re.compile(re.escape(PARLEY_BEGIN) + r"\s*(\{.*?\})\s*" + re.escape(PARLEY_END), re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")


def encode_block(payload):
    return PARLEY_BEGIN + "\n" + json.dumps(payload, indent=2) + "\n" + PARLEY_END


def parse_block(body):
    """Return the embedded payload dict, or None. Tolerates HTML-wrapped bodies
    (Graph often returns mail as HTML even when it was sent as text)."""
    if body is None:
        return None
    text = _TAG_RE.sub("", body).replace("&quot;", '"').replace("&amp;", "&")
    m = _BLOCK_RE.search(text)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def email_body(human_text, payload):
    return human_text.rstrip() + "\n\n" + encode_block(payload) + "\n"


# --- Hidden carrier: the machine payload a human never sees ------------------
#
# The payload rides in an HTML comment. A mail client renders the prose and drops
# the comment; an agent reads the comment back out. `_TAG_RE` below would eat the
# whole comment (there's no `>` until `-->`), so the carrier is read BEFORE any tag
# stripping — parse_carrier runs its own regex on the raw body.

PARLEY_COMMENT = "<!-- PARLEY"
_COMMENT_RE = re.compile(r"<!--\s*PARLEY\s*(\{.*?\})\s*-->", re.DOTALL)


def encode_carrier(payload):
    """The machine payload as a hidden HTML comment (no rendered glyphs)."""
    return "<!-- PARLEY " + json.dumps(payload) + " -->"


def parse_carrier(body):
    """The payload dict carried in `body`'s hidden PARLEY comment, or None.
    Tolerant of a backend that HTML-escapes the comment's contents (Graph can)."""
    if not body:
        return None
    m = _COMMENT_RE.search(body)
    if not m:
        return None
    try:
        return json.loads(_html.unescape(m.group(1)))
    except json.JSONDecodeError:
        return None


def parse_payload(body):
    """The PARLEY payload in `body`: the hidden HTML comment (current wire), or the
    legacy visible BEGIN/END block (back-compat for mail sent before the switch),
    else None. One predicate for 'is this a peer-to-peer message?'."""
    return parse_carrier(body) or parse_block(body)


def html_to_text(body):
    """An HTML body reduced to the readable prose a human sees: the PARLEY comment
    dropped, `<br>`/`</p>` turned back into line breaks, remaining tags and entities
    removed. Used for the text/plain alternative on send and to show the brain clean
    prose instead of markup. A body that isn't HTML passes through unchanged."""
    if not body:
        return ""
    text = _COMMENT_RE.sub("", body)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p\s*>", "\n\n", text)
    text = _TAG_RE.sub("", text)
    return re.sub(r"\n{3,}", "\n\n", _html.unescape(text)).strip()


def _as_html_prose(human_text):
    return _html.escape((human_text or "").strip()).replace("\n", "<br>\n")


def email_html(human_text, payload):
    """A wire message body: readable HTML prose with the machine payload hidden in a
    trailing comment. The human sees only `human_text`; an agent reads the payload."""
    return (f"<html><body><p>{_as_html_prose(human_text)}</p>\n"
            f"{encode_carrier(payload)}\n</body></html>")


def render_parts(body):
    """`(text, html)` for an outbound body. A wire/HTML body (carries the PARLEY
    comment) → (readable prose, the HTML); any other body → (body, None), i.e. plain
    text as before. Backends send multipart/alternative only when `html` is not None."""
    if body and PARLEY_COMMENT in body:
        return html_to_text(body), body
    return body, None


def as_addresses(value):
    """Normalise a recipient argument to a clean list of bare addresses.

    Every send path now takes either one address or several — a meeting with four
    people is ONE email to the four of them, not four emails (user, 2026-09-24) —
    so a single place decides what "several" means: a list/tuple, or a string with
    commas or semicolons (Outlook writes semicolons, everyone else commas). Blanks
    are dropped and duplicates collapse case-insensitively, first spelling kept:
    the same person twice in a To: line is a mistake nobody wants delivered."""
    if value is None:
        return []
    items = value if isinstance(value, (list, tuple, set)) else str(value).replace(";", ",").split(",")
    seen, out = set(), []
    for item in items:
        addr = str(item or "").strip().strip("<>")
        if addr and addr.lower() not in seen:
            seen.add(addr.lower())
            out.append(addr)
    return out


def addresses_in(*headers):
    """The bare addresses in one or more RFC 5322 recipient headers (To:, Cc:).

    Header values carry display names, quoting and folding — `getaddresses` is the
    stdlib's answer to all three, and `as_addresses` then applies the same cleaning
    the outbound side uses, so both directions agree on what an address is."""
    return as_addresses([addr for _, addr in getaddresses([h for h in headers if h])])


def mime_message(subject, from_addr, to_addr, body, thread=None):
    """A stdlib email message for `body`: multipart/alternative (text + hidden-carrier
    HTML) for a wire message, else a plain text/plain message. Shared by the MIME/SMTP
    backends (gmail, yahoo) so the carrier is built one way.

    `thread` is the negotiation's Message-IDs, oldest first. With it the mail goes
    out as a REPLY (RFC 5322 In-Reply-To + References) instead of a fresh message:
    every mail client then shows one conversation rather than a pile of unrelated
    ones. Parley read these headers from the start and never wrote them, so its own
    side of a thread always looked like spam (user, 2026-09-24)."""
    text, html = render_parts(body)
    if html is None:
        msg = MIMEText(text)
    else:
        msg = MIMEMultipart("alternative")
        msg.attach(MIMEText(text, "plain"))
        msg.attach(MIMEText(html, "html"))
    msg["Subject"], msg["From"] = subject, from_addr
    # One meeting, one conversation: every invitee is on the same To: line, so each of
    # them can see who else was asked and reply to all.
    msg["To"] = ", ".join(as_addresses(to_addr))
    chain = [m for m in (thread or []) if m]
    if chain:
        # In-Reply-To names the message being answered; References is the whole
        # chain, which is what clients actually thread on.
        msg["In-Reply-To"] = chain[-1]
        msg["References"] = " ".join(chain)
    return msg


# --- Small value types -------------------------------------------------------

@dataclass
class Slot:
    start: datetime
    end: datetime

    def overlaps(self, other):
        return self.start < other.end and other.start < self.end

    def human(self):
        # Convert to real UTC before formatting — the datetimes are tz-aware
        # (e.g. +02:00 from Google), so printing the wall-clock would mislabel them.
        s = self.start.astimezone(timezone.utc)
        e = self.end.astimezone(timezone.utc)
        return f"{s:%a %b %d %H:%M}–{e:%H:%M} UTC"


@dataclass
class Message:
    id: str
    sender: str          # bare email address
    subject: str
    body: str
    invite: dict = None  # set by a connection when the mail is a real calendar invite
    # Email threading (RFC 5322) — the correlation key a human maintains for free by
    # hitting Reply (Phase 2). Backends populate these from the mail headers; a reply
    # threads onto the negotiation whose recorded Message-IDs its chain references.
    message_id: str = None    # this mail's own Message-ID
    in_reply_to: str = None   # the Message-ID it is a reply to
    references: str = None    # the whole thread chain (space-separated Message-IDs)
    # Everyone this mail was addressed to (To + Cc, bare addresses, us included).
    # It is what tells a reply-to-all apart from a private aside: the same words mean
    # different things depending on who else can read them, and Parley must answer the
    # way it was spoken to (user, 2026-09-24). Backends that cannot report recipients
    # leave it empty, which reads as "unknown", never as "private".
    recipients: list = field(default_factory=list)
    # The sender's display name ("Joan Roca"), for showing a human who wrote. Never
    # used to route or match: `sender` stays the address. None = backend can't tell.
    sender_name: str = None

    def others(self, me):
        """The recipients besides `me` — who else is in this conversation."""
        mine = (me or "").lower()
        return [r for r in (self.recipients or []) if (r or "").lower() != mine]

    def was_private(self, me):
        """True when this mail reached nobody but `me`: a genuine aside, to be answered
        in private. Unknown recipients answer None — 'I cannot tell', which callers
        must not silently read as 'yes'."""
        if not self.recipients:
            return None
        return not self.others(me)

    def thread_refs(self):
        """The Message-IDs this mail threads onto — its In-Reply-To plus its References
        chain, de-duplicated, order preserved."""
        seen, out = set(), []
        for mid in f"{self.in_reply_to or ''} {self.references or ''}".split():
            if mid not in seen:
                seen.add(mid)
                out.append(mid)
        return out


@dataclass
class Event:
    id: str
    title: str
    start: datetime
    end: datetime
    attendees: list      # bare email addresses
    raw: object = None
    calendar_id: str = None  # which of an account's calendars this event lives on
    organizer: str = None    # bare email of the event's organizer; None = backend can't tell
    # This account's own answer to the event: "accepted" | "tentative" | "declined" |
    # "none" (not answered yet) | "organizer". None = backend can't tell. It is how
    # Parley notices the user answered an invite straight in their calendar.
    response: str = None
    # How the event shows on free/busy: "free" | "tentative" | "busy". None = can't tell.
    show_as: str = None
    # True when the event carries Parley's focus-block mark (FOCUS_MARK): one of OUR
    # Dynamic Focus Blocks, never a meeting of the user's.
    focus: bool = False

    @property
    def slot(self):
        return Slot(self.start, self.end)


# --- Dynamic Focus Blocks ------------------------------------------------------
#
# Time Parley keeps on the user's own calendar for their own work ("2 h to prepare
# meeting Z"). A focus block has no attendees and sends nothing. Two things identify
# one, so it can always be found and cleaned up even if Parley's own records are lost:
# the title prefix the user sees, and a mark the backend stores (a category in
# Outlook, a private extended property in Google).

FOCUS_MARK = "DFB"
FOCUS_PREFIX = "🧠 DFB: "
SHOW_AS = ("free", "tentative", "busy")


def focus_title(purpose):
    """'Prep Meeting Z' → '🧠 DFB: Prep Meeting Z' (idempotent)."""
    purpose = (purpose or "").strip()
    return purpose if purpose.startswith(FOCUS_PREFIX) else FOCUS_PREFIX + purpose


# --- datetime helpers --------------------------------------------------------

def to_iso(dt):
    return dt.isoformat()


def from_iso(s):
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def slot_from(payload, prefix=""):
    return Slot(from_iso(payload[prefix + "start"]), from_iso(payload[prefix + "end"]))
