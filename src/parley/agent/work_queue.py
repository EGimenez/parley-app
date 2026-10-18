"""WorkQueue — the durable hand-off between Parley and a blocked Claude Code session.

Phase 1 of `docs/iterations/iteration_2026_09_19_claude-parley.md`. One queue per
(agent, role): the poll loop pushes prepared turns, a background session leases them one
at a time, and acknowledges a turn only by asking for the next one.

Three properties, each of them load-bearing:

  * **Durable.** Items live in a JSON file under the agent's state dir, so a daemon
    restart does not swallow an event that arrived while it was down. Same stance as the
    intake cursors: never silently lose mail.

  * **At-least-once, never at-most-once.** `lease()` does not remove an item; it hides it
    until `ack()` or until the lease expires. A session that dies mid-turn gets its event
    redelivered rather than losing it. This is only safe because settlement is already
    idempotent (AccountsGate owns that), which is exactly why the guarantee is on this
    side of the trade.

  * **Poison-pill bounded.** An item that has been leased `max_attempts` times without an
    ack is parked, not redelivered forever. Without this, one event that reliably kills
    the session would loop until someone noticed — the session would look busy while the
    whole queue starved behind it.

Blocking is a `threading.Condition`: `push()` wakes a waiting `lease()` at once, and the
wait re-checks periodically so an expired lease is reclaimed without a push. The waiter
holds no agent lock while blocked — a six-hour wait must not freeze the poll loop.
"""

import json
import os
import threading
import time
import uuid

DEFAULT_LEASE_SECONDS = 900
DEFAULT_MAX_ATTEMPTS = 3
_RECHECK_SECONDS = 1.0


class WorkQueue:
    """One role's pending turns. Thread-safe; one instance per (agent, role)."""

    def __init__(self, path=None, lease_seconds=DEFAULT_LEASE_SECONDS,
                 max_attempts=DEFAULT_MAX_ATTEMPTS, journal=None):
        self.path = path
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts
        self.journal = journal
        self._cv = threading.Condition()
        self._items = []     # pending + leased, in arrival order
        self._parked = []    # poison pills, kept for diagnosis, never redelivered
        self._load()

    # -- producing ------------------------------------------------------------

    def push(self, kind, payload):
        """Queue one prepared turn. Returns its id."""
        item = {"id": f"w-{uuid.uuid4().hex[:10]}", "kind": kind, "payload": payload,
                "pushed_at": time.time(), "leased_until": 0.0, "attempts": 0}
        with self._cv:
            self._items.append(item)
            self._save()
            self._cv.notify_all()
        self._log("work_push", id=item["id"], turn_kind=kind)
        return item["id"]

    # -- consuming ------------------------------------------------------------

    def lease(self, timeout=None, stop=None):
        """Block until an item is available, then lease it and return a COPY.

        `timeout` (seconds) bounds the wait; None blocks until something arrives or
        `stop` (a `threading.Event`) is set. Returns None on timeout — which the caller
        should treat as "still nothing", not as an error.

        The wait is the whole point of the design: a pending tool call costs no tokens,
        so a session parked in here is free until there is real work.
        """
        deadline = None if timeout is None else time.time() + timeout
        with self._cv:
            while True:
                self._reclaim_locked()
                item = next((i for i in self._items if i["leased_until"] <= time.time()), None)
                if item is not None:
                    item["attempts"] += 1
                    item["leased_until"] = time.time() + self.lease_seconds
                    self._save()
                    self._log("work_lease", id=item["id"], turn_kind=item["kind"],
                              attempt=item["attempts"])
                    return dict(item)
                if stop is not None and stop.is_set():
                    return None
                remaining = _RECHECK_SECONDS
                if deadline is not None:
                    left = deadline - time.time()
                    if left <= 0:
                        return None
                    remaining = min(remaining, left)
                self._cv.wait(remaining)

    def ack(self, item_id, note=""):
        """Finish a leased item. Unknown ids are a no-op: a redelivered turn that the
        previous session had in fact completed must not raise on the way out."""
        with self._cv:
            before = len(self._items)
            self._items = [i for i in self._items if i["id"] != item_id]
            done = len(self._items) != before
            if done:
                self._save()
        if done:
            self._log("work_ack", id=item_id, note=note)
        return done

    def release(self, item_id):
        """Hand a leased item straight back (the session is recycling, say) so the next
        waiter gets it immediately instead of after the lease expires."""
        with self._cv:
            for i in self._items:
                if i["id"] == item_id:
                    i["leased_until"] = 0.0
                    self._save()
                    self._cv.notify_all()
                    self._log("work_release", id=item_id)
                    return True
        return False

    # -- introspection (status endpoints, tests) ------------------------------

    def stats(self):
        now = time.time()
        with self._cv:
            return {"pending": sum(1 for i in self._items if i["leased_until"] <= now),
                    "leased": sum(1 for i in self._items if i["leased_until"] > now),
                    "parked": len(self._parked)}

    def parked(self):
        with self._cv:
            return [dict(i) for i in self._parked]

    # -- internals ------------------------------------------------------------

    def _reclaim_locked(self):
        """Expired leases come back; anything that has burned through its attempts is
        parked instead. Caller holds the condition."""
        now, keep, changed = time.time(), [], False
        for item in self._items:
            expired = 0 < item["leased_until"] <= now
            if expired and item["attempts"] >= self.max_attempts:
                self._parked.append(item)
                changed = True
                self._log("work_parked", id=item["id"], turn_kind=item["kind"],
                          attempts=item["attempts"],
                          human=f"⚠ giving up on a queued turn after {item['attempts']} "
                                f"attempts — it kept killing the session ({item['id']})")
                continue
            if expired:
                item["leased_until"] = 0.0
                changed = True
                self._log("work_redeliver", id=item["id"], attempts=item["attempts"])
            keep.append(item)
        self._items = keep
        if changed:
            self._save()

    def _log(self, event, **fields):
        if self.journal is None:
            return
        try:
            self.journal.log(event, **fields)
        except TypeError:
            # A field colliding with Log.log's own parameter names (kind, human) is a
            # BUG in this file, not a runtime hazard — and swallowing it silently is
            # how the queue lost every journal line the first time. Say it once.
            print(f"[work_queue] journal field clash logging {event!r} — fix the caller")
        except Exception:  # noqa: BLE001 — never let logging break the queue
            pass

    def _load(self):
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f) or {}
        except (OSError, ValueError):
            return  # a corrupt queue file must not stop the agent from starting
        self._items = list(data.get("items") or [])
        self._parked = list(data.get("parked") or [])
        # A restart invalidates every lease: whoever held them is gone.
        for item in self._items:
            item["leased_until"] = 0.0

    def _save(self):
        """Caller holds the condition. Write-and-rename so a crash mid-write cannot
        leave a half-written queue behind."""
        if not self.path:
            return
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = f"{self.path}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"items": self._items, "parked": self._parked}, f,
                          indent=1, default=str)
            os.replace(tmp, self.path)
        except OSError:
            pass  # a queue that cannot persist still works in memory; loud on reload
