"""Dynamic Focus Blocks (DFB) — time kept for the user's own work before a meeting.

"I need 2 h in one go, in the last 3 days before meeting Z, to prepare it." A DFB is
that request, kept alive: Parley places the blocks on the user's calendar and keeps
them true while the calendar changes around them.

    * While the time fits with room to spare, the blocks show as FREE: anyone may still
      book over them, and the next sweep moves them somewhere else in the window.
    * When the free time left in the window gets tight (it no longer leaves `margin`
      hours beyond what is needed), the blocks HARDEN to busy and the user is told.
      A notice, not a question: protecting the time is the point of a DFB.
    * When the blocks no longer fit at all, the user is ASKED what yields.
    * Z moved: the window moves with it. Z cancelled: the blocks go. Z over: the
      record goes (the blocks stay, as the history of the time spent).

All of it is infra — arithmetic on free time, no model turn. The calendar writes go
through `AccountsGate.hold / update_hold / release_hold` (guardrail + journal, and they
refuse anything without the focus mark). A DFB anchors on Z's event ID, never on its
title, so a renamed or moved Z is still found and a cancelled one is noticed.

The free time considered is the user's working day (default 09:00–18:00, Monday to
Friday, in the calendar's timezone). Busy is any event that does not show as free,
except our own blocks. Placement prefers the time closest to Z.
"""

import json
import os
import time
import uuid
from datetime import datetime, timedelta, timezone

from parley.core import Slot, from_iso, to_iso

GRID = timedelta(minutes=15)       # blocks start and end on the quarter hour
MIN_PIECE = timedelta(minutes=30)  # the smallest piece of a splittable block
DEFAULT_DAY = ("09:00", "18:00")
LOOKAHEAD = timedelta(days=90)     # how far ahead orphans are looked for


def _now():
    return datetime.now(timezone.utc)


# -- the arithmetic (pure: unit-tested without a calendar) ---------------------

def _hm(text):
    h, m = str(text).split(":")
    return int(h), int(m)


def working_windows(window, tzname, day=DEFAULT_DAY, weekends=False):
    """The parts of `window` (a Slot, UTC) inside the working day, as UTC Slots."""
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(tzname or "UTC")
    except Exception:  # noqa: BLE001 — an unknown zone falls back to UTC
        tz = timezone.utc
    (h0, m0), (h1, m1) = _hm(day[0]), _hm(day[1])
    out = []
    d = window.start.astimezone(tz).date()
    last = window.end.astimezone(tz).date()
    while d <= last:
        if weekends or d.weekday() < 5:
            lo = datetime(d.year, d.month, d.day, h0, m0, tzinfo=tz).astimezone(timezone.utc)
            hi = datetime(d.year, d.month, d.day, h1, m1, tzinfo=tz).astimezone(timezone.utc)
            lo, hi = max(lo, window.start), min(hi, window.end)
            if hi > lo:
                out.append(Slot(lo, hi))
        d += timedelta(days=1)
    return out


def _snap_up(t):
    base = t.replace(minute=0, second=0, microsecond=0)
    steps = -(-(t - base) // GRID)
    return base + steps * GRID


def _snap_down(t):
    base = t.replace(minute=0, second=0, microsecond=0)
    return base + ((t - base) // GRID) * GRID


def free_intervals(windows, busy):
    """`windows` minus every `busy` Slot, snapped inward to the grid."""
    free = []
    for w in windows:
        parts = [(w.start, w.end)]
        for b in busy:
            nxt = []
            for lo, hi in parts:
                if b.end <= lo or b.start >= hi:
                    nxt.append((lo, hi))
                    continue
                if b.start > lo:
                    nxt.append((lo, b.start))
                if b.end < hi:
                    nxt.append((b.end, hi))
            parts = nxt
        for lo, hi in parts:
            lo, hi = _snap_up(lo), _snap_down(hi)
            if hi > lo:
                free.append(Slot(lo, hi))
    return sorted(free, key=lambda s: s.start)


def capacity(free, hours, splittable):
    """Hours of focus time that `free` can hold, in units the DFB can use."""
    h = timedelta(hours=hours)
    if splittable:
        total = sum(((s.end - s.start) for s in free if s.end - s.start >= MIN_PIECE),
                    timedelta())
    else:
        total = sum(((s.end - s.start) // h * h for s in free), timedelta())
    return total.total_seconds() / 3600


def place(free, blocks, hours, splittable):
    """Where the blocks go: latest first (closest to Z). Returns UTC Slots, in time
    order. Fewer than asked when they do not fit."""
    h = timedelta(hours=hours)
    out = []
    if not splittable:
        for s in sorted(free, key=lambda s: s.end, reverse=True):
            end = s.end
            while len(out) < blocks and end - h >= s.start:
                out.append(Slot(end - h, end))
                end -= h
            if len(out) == blocks:
                break
    else:
        need = h * blocks
        for s in sorted(free, key=lambda s: s.end, reverse=True):
            if need <= timedelta() or s.end - s.start < MIN_PIECE:
                continue
            take = min(need, s.end - s.start)
            if take < MIN_PIECE:
                break
            out.append(Slot(s.end - take, s.end))
            need -= take
    return sorted(out, key=lambda s: s.start)


# -- the store and the sweep ----------------------------------------------------

class FocusBlocks:
    """Every DFB of one agent, persisted to `path` (JSON)."""

    #: How often the calendar is re-read for DFBs. A cancellation mail forces a sweep.
    SWEEP_SECONDS = 300
    #: Z must be missing this many sweeps in a row before its blocks are released: a
    #: calendar read that comes back short once must not delete anything.
    MISSING_LIMIT = 2

    def __init__(self, agent, path=None):
        self.agent = agent
        self.path = path
        self.items = {}
        self._last_sweep = float("-inf")
        self.load()

    # -- persistence ------------------------------------------------------------

    def load(self):
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                self.items = json.load(f).get("blocks") or {}
        except (OSError, ValueError):
            self.items = {}

    def save(self):
        if not self.path:
            return
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"blocks": self.items}, f, indent=2)
        os.replace(tmp, self.path)

    # -- the public verbs (POST /v1/focus) -------------------------------------

    def create(self, spec):
        """Register a DFB from `spec` and place it at once. See `parley --focus`."""
        anchor = spec.get("anchor") or {}
        conn, z = self._resolve_anchor(anchor)
        if z is None:
            return {"ok": False, "error": "meeting Z not found: pass anchor.event_id, or "
                                          "anchor.title + anchor.start of an event on "
                                          "your calendar"}
        window = spec.get("window") or {}
        window_hours = float(window.get("hours") or 0) + 24 * float(window.get("days") or 0)
        blocks, hours = int(spec.get("blocks") or 1), float(spec.get("hours") or 1)
        if window_hours <= 0 or blocks < 1 or hours <= 0:
            return {"ok": False, "error": "need window (days or hours), blocks >= 1 and "
                                          "hours > 0"}
        purpose = (spec.get("purpose") or spec.get("topic") or f"Prep {z.title}").strip()
        margin = spec.get("margin_hours")
        dfb_id = "dfb-" + uuid.uuid4().hex[:8]
        self.items[dfb_id] = {
            "id": dfb_id, "account": conn.address(), "purpose": purpose,
            "deliverable": spec.get("deliverable") or "",
            "anchor": {"event_id": z.id, "title": z.title,
                       "start": to_iso(z.start), "end": to_iso(z.end)},
            "window_hours": window_hours, "blocks": blocks, "hours": hours,
            "splittable": bool(spec.get("splittable", False)),
            "margin_hours": float(margin) if margin is not None else blocks * hours,
            "day": list(spec.get("day") or DEFAULT_DAY),
            "weekends": bool(spec.get("weekends", False)),
            "state": "new", "placed": [], "missing": 0, "told": None,
            "created_at": to_iso(_now())}
        self.agent.log.log("focus_created", dfb_id=dfb_id, purpose=purpose,
                           anchor=self.items[dfb_id]["anchor"], blocks=blocks, hours=hours,
                           window_hours=window_hours,
                           human=f"🧠 focus {dfb_id}: {blocks}×{hours:g} h before "
                                 f"'{z.title}'")
        self._sweep_one(dfb_id, _now())
        self.save()
        return {"ok": True, **self.describe(dfb_id)}

    def list(self):
        return [self.describe(i) for i in self.items]

    def cancel(self, dfb_id, reason="cancelled by the user"):
        dfb = self.items.get(dfb_id)
        if dfb is None:
            return {"ok": False, "error": f"no focus block {dfb_id!r}"}
        removed = self._release_all(dfb, reason)
        del self.items[dfb_id]
        self._close_notes(dfb_id, reason)
        self.save()
        self.agent.log.log("focus_dropped", dfb_id=dfb_id, reason=reason,
                           human=f"🧠 focus {dfb_id} dropped: {reason}")
        return {"ok": True, "dfb_id": dfb_id, "removed": removed}

    # -- what a booking would do to them (F6: the brain's "make room" input) ------

    def impact(self, slot, now=None):
        """What booking `slot` would do to each DFB whose window it falls in:
        [{dfb_id, purpose, meeting, meeting_start, state_now, state_after,
        free_hours_after, needed_hours}] — only the DFBs it makes worse. `state_after`
        "short" means the booking takes the LAST time the focus work could fit."""
        now = now or _now()
        out = []
        for dfb in self.items.values():
            z_start = from_iso(dfb["anchor"]["start"])
            window = Slot(max(now, z_start - timedelta(hours=dfb["window_hours"])), z_start)
            if window.end <= window.start or not slot.overlaps(window):
                continue
            conn = self.agent._conn_for_address(dfb["account"]) or self.agent.default_conn
            try:
                events = conn.list_events(window)
            except Exception:  # noqa: BLE001 — unreadable: say nothing rather than guess
                continue
            busy = [e.slot for e in events if e.id != dfb["anchor"]["event_id"]
                    and not e.focus and e.show_as != "free"]
            tz = _calendar_tz(conn, self.agent)
            windows = working_windows(window, tz, dfb["day"], dfb["weekends"])
            need = dfb["blocks"] * dfb["hours"]
            after = capacity(free_intervals(windows, busy + [slot]), dfb["hours"],
                             dfb["splittable"])
            state_after = _state(after, need, dfb["margin_hours"])
            state_now = _state(capacity(free_intervals(windows, busy), dfb["hours"],
                                        dfb["splittable"]), need, dfb["margin_hours"])
            if _RANK[state_after] > _RANK[state_now]:
                out.append({"dfb_id": dfb["id"], "purpose": dfb["purpose"],
                            "meeting": dfb["anchor"]["title"],
                            "meeting_start": dfb["anchor"]["start"],
                            "state_now": state_now, "state_after": state_after,
                            "free_hours_after": round(after, 2), "needed_hours": need})
        return out

    def summary(self):
        """One line per DFB, for the brain's context."""
        if not self.items:
            return "(none)"
        lines = []
        for d in self.items.values():
            lines.append(f"[{d['id']}] '{d['purpose']}': {d['blocks']}×{d['hours']:g} h in "
                         f"the last {d['window_hours']:g} h before '{d['anchor']['title']}' "
                         f"({d['anchor']['start']}) — {d['state']}, "
                         f"{d.get('free_hours', '?')} h free for "
                         f"{d['blocks'] * d['hours']:g} h needed "
                         f"(+{d['margin_hours']:g} h margin)")
        return "\n".join(lines)

    def describe(self, dfb_id):
        d = self.items[dfb_id]
        return {"dfb_id": d["id"], "purpose": d["purpose"], "anchor": d["anchor"],
                "state": d["state"], "blocks": d["blocks"], "hours": d["hours"],
                "needed_hours": d["blocks"] * d["hours"],
                "free_hours": d.get("free_hours"),
                "placed": [{k: p[k] for k in ("event_id", "start", "end", "show_as")}
                           for p in d["placed"]]}

    # -- the sweep --------------------------------------------------------------

    def sweep(self, now=None, force=False):
        """Bring every DFB up to date with the calendar. Cheap to call every wake:
        it only reads the calendar every SWEEP_SECONDS unless forced."""
        if not self.items:
            return
        if not force and time.monotonic() - self._last_sweep < self.SWEEP_SECONDS:
            return
        self._last_sweep = time.monotonic()
        now = now or _now()
        for dfb_id in list(self.items):
            try:
                self._sweep_one(dfb_id, now)
            except Exception as err:  # noqa: BLE001 — one DFB never stops the others
                self.agent.log.log("error", where="focus_sweep", dfb_id=dfb_id,
                                   error=str(err))
        self.save()

    def reconcile(self, now=None):
        """At start: delete focus blocks still ahead that no DFB owns (their record was
        lost or dropped), then sweep, which recreates blocks a record misses."""
        now = now or _now()
        owned = {p["event_id"] for d in self.items.values() for p in d["placed"]}
        for conn in self.agent.connections:
            try:
                events = conn.list_events(Slot(now, now + LOOKAHEAD))
            except Exception:  # noqa: BLE001 — a calendar we cannot read is left alone
                continue
            for e in events:
                if e.focus and e.id not in owned and e.start >= now:
                    self.agent.accounts_gate.release_hold(
                        conn, e.id, to_iso(e.start), to_iso(e.end),
                        reason="orphan: no focus request owns it")
        self.sweep(now=now, force=True)

    def _sweep_one(self, dfb_id, now):
        dfb = self.items[dfb_id]
        conn = self.agent._conn_for_address(dfb["account"]) or self.agent.default_conn
        z = self._find_z(conn, dfb)
        if z is _UNKNOWN:
            return  # could not read the calendar: try again next sweep
        if z is None:
            dfb["missing"] = dfb.get("missing", 0) + 1
            if dfb["missing"] >= self.MISSING_LIMIT:
                self._release_all(dfb, "the meeting is gone")
                del self.items[dfb_id]
                self._close_notes(dfb_id, "the meeting is gone")
                self.agent.log.log("focus_dropped", dfb_id=dfb_id,
                                   reason="meeting cancelled or deleted",
                                   human=f"🧠 focus {dfb_id}: '{dfb['anchor']['title']}' "
                                         f"is gone — its blocks were released.")
            return
        dfb["missing"] = 0
        if to_iso(z.start) != dfb["anchor"]["start"] or z.title != dfb["anchor"]["title"]:
            self.agent.log.log("focus_anchor_moved", dfb_id=dfb_id, title=z.title,
                               start=to_iso(z.start), was=dfb["anchor"]["start"])
            dfb["anchor"].update(title=z.title, start=to_iso(z.start), end=to_iso(z.end))
        if z.start <= now:
            del self.items[dfb_id]
            self.agent.log.log("focus_done", dfb_id=dfb_id, title=z.title,
                               human=f"🧠 focus {dfb_id}: '{z.title}' has started — "
                                     f"record closed.")
            return
        self._replan(conn, dfb, z, now)

    def _replan(self, conn, dfb, z, now):
        gate = self.agent.accounts_gate
        window = Slot(max(now, z.start - timedelta(hours=dfb["window_hours"])), z.start)
        if window.end <= window.start:
            return
        # Read wide enough to see every block we placed, even one the window has left
        # behind (Z moved): it must be released, not lost track of.
        starts = [from_iso(p["start"]) for p in dfb["placed"]] + [window.start]
        ends = [from_iso(p["end"]) for p in dfb["placed"]] + [window.end]
        events = conn.list_events(Slot(min(starts), max(ends)))
        ours = {p["event_id"] for p in dfb["placed"]}
        live = {e.id for e in events if e.id in ours}
        # Blocks someone deleted by hand are forgotten (and so re-placed); blocks in
        # the past are history.
        dfb["placed"] = [p for p in dfb["placed"]
                         if p["event_id"] in live and from_iso(p["end"]) > now]
        busy = [e.slot for e in events
                if e.id != z.id and not e.focus and e.show_as != "free"
                and e.slot.overlaps(window)]
        tz = _calendar_tz(conn, self.agent)
        free = free_intervals(working_windows(window, tz, dfb["day"], dfb["weekends"]), busy)
        need = dfb["blocks"] * dfb["hours"]
        cap = capacity(free, dfb["hours"], dfb["splittable"])
        dfb["free_hours"] = round(cap, 2)
        state = _state(cap, need, dfb["margin_hours"])
        show_as = "free" if state == "soft" else "busy"

        # Keep each block that still sits in free time; re-place the rest.
        keep, drop = [], []
        for p in dfb["placed"]:
            s = Slot(from_iso(p["start"]), from_iso(p["end"]))
            (keep if any(f.start <= s.start and s.end <= f.end for f in free)
             else drop).append(p)
        for p in drop:
            gate.release_hold(conn, p["event_id"], p["start"], p["end"],
                              reason="its time was taken")
        kept_slots = [Slot(from_iso(p["start"]), from_iso(p["end"])) for p in keep]
        still_free = free_intervals(free, kept_slots)
        kept_hours = sum((s.end - s.start).total_seconds() for s in kept_slots) / 3600
        missing_blocks = dfb["blocks"] - (round(kept_hours / dfb["hours"])
                                          if not dfb["splittable"] else 0)
        if dfb["splittable"]:
            left = need - kept_hours
            new = place(still_free, 1, left, True) if left > 0 else []
        else:
            new = place(still_free, max(missing_blocks, 0), dfb["hours"], False)
        placed = []
        for p in keep:
            if p.get("show_as") != show_as:
                gate.update_hold(conn, p["event_id"], p["start"], p["end"], show_as=show_as)
                p["show_as"] = show_as
            placed.append(p)
        for s in new:
            made = gate.hold(conn, to_iso(s.start), to_iso(s.end), dfb["purpose"],
                             show_as=show_as)
            if made.get("ok"):
                placed.append({"event_id": made["event_id"], "start": made["start"],
                               "end": made["end"], "show_as": show_as})
        dfb["placed"] = sorted(placed, key=lambda p: p["start"])
        if state != dfb["state"]:
            self.agent.log.log("focus_state", dfb_id=dfb["id"], state=state,
                               was=dfb["state"], free_hours=dfb["free_hours"],
                               needed_hours=need)
        dfb["state"] = state
        self._tell(conn, dfb, z, state, need)

    def _tell(self, conn, dfb, z, state, need):
        """Hard → a notice (once). Short → a question (once). Back to soft → nothing,
        and the next squeeze tells again."""
        if state == "soft" or dfb.get("told") == state:
            if state == "soft":
                dfb["told"] = None
            return
        dfb["told"] = state
        when = {"start": dfb["anchor"]["start"], "end": dfb["anchor"]["end"]}
        if state == "hard":
            self.agent.escalate(
                conn, f"Your focus time '{dfb['purpose']}' before '{z.title}' is now "
                      f"protected (busy): only {dfb['free_hours']:g} h of free time are left "
                      f"in the window for {need:g} h of work. Nothing to answer.",
                negotiation_id=f"{dfb['id']}-hard", kind="notice", title=z.title,
                summary=f"Focus time protected: {need:g} h before '{z.title}'.", **when)
        else:
            self.agent.escalate(
                conn, f"Your focus time '{dfb['purpose']}' no longer fits before "
                      f"'{z.title}': {need:g} h needed, {dfb['free_hours']:g} h free in the "
                      f"window. Something has to give: free some time and I will place "
                      f"it, or drop the focus time.",
                negotiation_id=f"{dfb['id']}-short", kind="focus", title=z.title,
                action={"kind": "focus_short", "dfb_id": dfb["id"]},
                summary=f"No room for {need:g} h before '{z.title}'. Keep trying, or drop "
                        f"it?", **when)

    def decide(self, dfb_id, choice):
        """The user's answer to a 'no room' question. 'drop' cancels the DFB; anything
        else keeps it, and it is placed as soon as time frees up."""
        text = str(choice or "").lower()
        if dfb_id in self.items and any(w in text for w in ("drop", "cancel", "deja",
                                                            "borra", "cancela", "no ")):
            self.cancel(dfb_id, "the user dropped it")
            return {"ok": True, "decision": "focus_dropped", "text": "Focus time dropped."}
        return {"ok": True, "decision": "focus_kept",
                "text": "Focus time kept: it is placed as soon as time frees up."}

    # -- helpers ----------------------------------------------------------------

    def _close_notes(self, dfb_id, reason):
        """A dropped DFB's notice and question no longer mean anything."""
        for suffix in ("-hard", "-short"):
            ref = dfb_id + suffix
            pq = self.agent.user_messaging.pending.get(ref)
            if pq is not None and not pq.resolved:
                self.agent._close_question(ref, f"focus time dropped: {reason}")

    def _release_all(self, dfb, reason):
        conn = self.agent._conn_for_address(dfb["account"]) or self.agent.default_conn
        removed = 0
        for p in dfb["placed"]:
            if from_iso(p["end"]) <= _now():
                continue  # a block in the past is history, not an orphan
            got = self.agent.accounts_gate.release_hold(conn, p["event_id"], p["start"],
                                                        p["end"], reason=reason)
            removed += bool(got.get("ok") and not got.get("already_gone"))
        dfb["placed"] = []
        return removed

    def _find_z(self, conn, dfb):
        """Z by its event id: first around where it was, then a month either side."""
        start = from_iso(dfb["anchor"]["start"])
        for span in (timedelta(days=1), timedelta(days=30)):
            try:
                events = conn.list_events(Slot(start - span, start + span))
            except Exception:  # noqa: BLE001 — unreadable is not "gone"
                return _UNKNOWN
            hit = next((e for e in events if e.id == dfb["anchor"]["event_id"]), None)
            if hit is not None:
                return hit
        return None

    def _resolve_anchor(self, anchor):
        """(conn, Event) for anchor.event_id, or anchor.title + anchor.start."""
        start = anchor.get("start")
        conns = self.agent.connections
        if start:
            when = self.agent.accounts_gate._when(self.agent.default_conn, start)
            span = Slot(when - timedelta(days=1), when + timedelta(days=1))
        else:
            span = Slot(_now(), _now() + LOOKAHEAD)
        title = (anchor.get("title") or "").strip().lower()
        for conn in conns:
            for e in conn.list_events(span):
                if anchor.get("event_id") and e.id == anchor["event_id"]:
                    return conn, e
                if not anchor.get("event_id") and title and start \
                        and abs((e.start - when).total_seconds()) < 60 \
                        and (title in e.title.lower() or e.title.lower() in title):
                    return conn, e
        return None, None


def _state(cap, need, margin):
    """soft: fits with `margin` to spare · hard: fits, barely · short: does not fit."""
    if cap < need:
        return "short"
    return "hard" if cap < need + margin else "soft"


_RANK = {"soft": 0, "hard": 1, "short": 2}


class _Unknown:
    """`_find_z` when the calendar could not be read: neither found nor gone."""


_UNKNOWN = _Unknown()


def _calendar_tz(conn, agent):
    if agent.tz:
        return agent.tz
    try:
        return conn.timezone() or "UTC"
    except Exception:  # noqa: BLE001
        return "UTC"
