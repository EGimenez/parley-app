"""Log (renamed from Journal, 2026-08-31 — the design's own vocabulary already
said "Logs"; only the code lagged) — the agent's flight recorder: every message
received, every decision made.

Live-run post-mortems used to depend on console screenshots: everything was
`print()` to stdout, some drops were completely silent, and the brain's LLM
steps were only partially echoed. Log makes observability a guarantee (infra
level): one call per event appends one JSON line to a daily file under
`~/.parley/logs/<agent>/journal-YYYY-MM-DD.jsonl`, and — when the event has a
human reading — echoes the same line to the console, so screen and file never
diverge.

A deliberate sibling of `ShortTermMemory`, not a part of it (2026-08-31): this
is what the Brain never reads — write-mostly, forensic, the opposite job from
what feeds the prompt. Confirmed reading the actual code: zero coupling with
Memory, so keeping them apart cost nothing to build, only to organize.

Design constraints:
  * **Never crash a turn.** File I/O failures are swallowed; values that aren't
    JSON-able degrade to `str()` (Slots, datetimes, Message fields).
  * **Crash-safe.** Open-append-close per write; the daily filename is computed per
    write, so midnight rolls the file naturally — no rotation machinery.
  * **No secrets.** The log sees runtime traffic (mail, tool calls, decisions),
    never cfg contents; nothing here reads config.
  * `dir_path=None` → in-memory only (`self.records`) — the selftest inspects it.
"""

import json
import sys
import os
from datetime import datetime, timezone


def _echo(line):
    """Print a console line that can never take down its caller.

    Found live 2026-09-19: the daemon is spawned with `stdout=<log file>`, so the
    child picks the Windows locale encoding (cp1252) for stdout, and every `human=`
    line carrying an emoji raised UnicodeEncodeError from inside `log()`. Worse,
    `Brain.dispatch` caught that and reported it to the model as the TOOL having
    failed, so a console-encoding detail read as "check() is broken". Journalling is
    observation: it must never change what it observes.
    """
    try:
        print(line)
    except UnicodeEncodeError:
        encoding = getattr(sys.stdout, "encoding", None) or "ascii"
        try:
            print(line.encode(encoding, "replace").decode(encoding, "replace"))
        except Exception:  # noqa: BLE001 - out of ideas; losing an echo is acceptable
            pass
    except OSError:
        pass  # a closed / redirected-away stdout is not a reason to stop working


class Log:
    def __init__(self, dir_path=None, name="parley"):
        self.dir = dir_path
        self.name = name
        self.records = []  # in-memory mirror (bounded) — selftests + future /v1/journal

    _KEEP = 500  # in-memory tail size; the file is the durable record

    def log(self, kind, human=None, **data):
        """Record one event. `kind` names it (message_received, drop, llm_step, …),
        `data` is the analyzable payload, `human` an optional console echo."""
        record = {"ts": datetime.now(timezone.utc).isoformat(), "agent": self.name,
                  "kind": kind, **data}
        self.records.append(record)
        del self.records[:-self._KEEP]
        if human is not None:
            _echo(f"[{self.name}] {human}")
        if self.dir:
            try:
                os.makedirs(self.dir, exist_ok=True)
                fname = f"journal-{datetime.now(timezone.utc).date().isoformat()}.jsonl"
                with open(os.path.join(self.dir, fname), "a", encoding="utf-8") as f:
                    f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
            except OSError:
                pass  # a full disk / locked file must never take a negotiation down
