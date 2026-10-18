"""Durable intake cursor — tiny JSON state so a connector resumes after a restart.

A connector polls its provider for "what's new since X?" (Gmail `historyId`, Graph
`deltaLink`, an inbox timestamp…). If X lives only in memory, a restart re-baselines
to "now" and silently drops every message that arrived while the agent was down.
This persists X — plus the time it was last refreshed — to a small JSON file, so the
next start resumes from the last acknowledged point instead of skipping ahead.

Backend-agnostic on purpose: the connector decides what the fields mean
(`history_id`, `delta_link`, `pos`, `refreshed_at`…); this just round-trips a flat
dict through one file. `parse_lookback` / `lookback_cutoff` are the (pure, stdlib)
helpers behind the `catch_up` look-back window, kept here so they're testable
without pulling in any provider SDK.
"""

import json
import os
import re
import tempfile
from datetime import timedelta


def load_cursor(path):
    """Return the saved cursor dict, or None if there's no readable state file."""
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def save_cursor(path, **fields):
    """Write `fields` to `path` as JSON (creating parent dirs). Atomic-ish: write a
    temp file in the same dir then `os.replace`, so a crash mid-write can't leave a
    half-written cursor behind."""
    if not path:
        return
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(fields, f)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


_LOOKBACK_RE = re.compile(r"^(\d+)\s*([dhm])$")


def parse_lookback(spec):
    """`'7d'` / `'24h'` / `'30m'` → a `timedelta`; `'none'` / `''` / `'0'` → None.
    (`'today'` is handled by `lookback_cutoff`, which needs `now`.)"""
    s = str(spec or "").strip().lower()
    if s in ("", "none", "0", "today"):
        return None
    m = _LOOKBACK_RE.match(s)
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2)
    return {"d": timedelta(days=n), "h": timedelta(hours=n), "m": timedelta(minutes=n)}[unit]


def lookback_cutoff(spec, now):
    """The datetime to look back to for a `catch_up` spec, or None for 'none'.
    `'today'` → start of `now`'s UTC day; `'Nd/Nh/Nm'` → `now - delta`."""
    s = str(spec or "").strip().lower()
    if s == "today":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    delta = parse_lookback(s)
    return (now - delta) if delta is not None else None
