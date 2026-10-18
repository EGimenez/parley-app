"""SessionSupervisor — keeps a background Claude Code session alive per role.

Phase 2b of `docs/iterations/iteration_2026_09_19_claude-parley.md`. When a cfg says
`llm: driver: session`, the daemon stops reasoning in-process and instead keeps a
headless `claude -p` running that drains a work queue through Parley's MCP surface.

**Why this is much less machinery than it sounds.** `claude -p` runs until the model
stops calling tools. A session told to loop on `wait_for_work` therefore never finishes
on its own: it blocks inside a pending tool call, which costs nothing, and comes back
when Parley has work. And when it *does* end — the context filled, the model decided to
stop, the process died — that exit IS the recycle. The supervisor's whole job is to
notice and start another one. No separate "recycle at N events" timer is needed; the
session is told to stop after `events_per_session` turns and the relaunch does the rest.

**Backoff matters more than it looks.** Two live sessions per agent already compete with
the user's own Claude Code for subscription rate limits. A session that dies immediately
— quota exhausted, bad model name, CLI missing — must not be relaunched in a hot loop,
or Parley turns one problem into a rate-limit ban. Anything that exits faster than
`_HEALTHY_SECONDS` doubles the wait, up to `_MAX_BACKOFF`.

Nothing in here decides anything about scheduling. It starts processes and writes down
what happened to them.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time

#: A session that survives this long is considered to have started properly; anything
#: shorter is a failure to launch and backs off.
_HEALTHY_SECONDS = 60
_BASE_BACKOFF = 5
_MAX_BACKOFF = 300
#: Turns one session handles before being told to stop, so a fresh one takes over with a
#: clean context. Cost does not force this (re-reading a cached prefix is ~12x cheaper
#: than re-writing it, so a transcript would have to pass ~300k tokens to pay for a
#: relaunch); avoiding Claude Code's lossy auto-compaction does.
DEFAULT_EVENTS_PER_SESSION = 20

#: Claude Code's own tools, refused: this session reasons and calls Parley's verbs, it
#: does not read the disk. The prompt says so too; this makes it true.
_OWN_TOOLS = ("Bash", "Read", "Edit", "Write", "NotebookEdit", "Glob", "Grep",
              "WebFetch", "WebSearch", "Task", "Agent", "TodoWrite", "Artifact")

_PROMPT = """You are Parley's {role} for {owner}, running as a background session. \
Nobody is watching you; there is no one to ask and nothing to print.

Your entire job is this loop:

  1. Call wait_for_work. It blocks — often for hours — and that is correct and free. \
Never poll, never give up on it, never do anything else while waiting.
  2. It returns one event together with everything you need to judge it: the policy, \
the open negotiations, the current time. Follow those instructions exactly.
  3. Act using the Parley tools you were given, and only those. They are the only way \
anything reaches a calendar or a mailbox.
  4. Call wait_for_work again, with done_because describing what you concluded — that \
both finishes the turn and fetches the next one.

Handle at most {events} turns, then stop and reply with one line summarising the \
session. A fresh session takes over immediately, so stopping loses nothing.

Never use your own tools — no shell, no files, no web. If a Parley tool fails, say so \
in done_because and move on; do not try to work around it another way."""


def child_env():
    """The environment to hand a Claude Code session we spawn.

    Whoever started Parley may themselves be sitting inside Claude Code — which is
    precisely what happens while developing and testing it — and a plain spawn
    inherits their session markers. Seen live on 2026-09-19: the session we opened for
    the user announced "Transcript saving is off - inherited
    CLAUDE_CODE_CHILD_SESSION". Ten variables came across, and two of them were
    CLAUDE_CODE_MESSAGING_SOCKET and _TOKEN — a live channel back into the parent's
    session. That is not untidiness, it is one session wired into another.

    A session we open is a session of its own, so every CLAUDE* marker is dropped.
    ANTHROPIC_* is deliberately left alone: that is the user's own auth choice, not
    our plumbing.
    """
    return {k: v for k, v in os.environ.items() if not k.upper().startswith("CLAUDE")}


class SessionSupervisor:
    """One thread per role, each keeping a `claude -p` alive against the daemon."""

    def __init__(self, agent, base_url, roles=("brain",), cli="claude",
                 events_per_session=DEFAULT_EVENTS_PER_SESSION, cwd=None):
        self.agent = agent
        self.base_url = base_url.rstrip("/")
        self.roles = tuple(roles)
        self.cli = cli
        self.events_per_session = events_per_session
        self.cwd = cwd
        self._stop = threading.Event()
        self._threads = []
        self._procs = {}
        self._config_path = None

    # -- lifecycle ------------------------------------------------------------

    def start(self):
        exe = shutil.which(self.cli)
        if not exe:
            self.agent.log.log(
                "error", where="session_supervisor", error=f"{self.cli} not on PATH",
                human=f"⚠ llm: driver: session needs the Claude Code CLI ({self.cli}) on "
                      f"PATH — events will queue up until one is running.")
            return False
        self._config_path = self._write_mcp_config()
        for role in self.roles:
            thread = threading.Thread(target=self._keep_alive, args=(exe, role),
                                      name=f"parley-session-{role}", daemon=True)
            thread.start()
            self._threads.append(thread)
        return True

    def stop(self):
        self._stop.set()
        for proc in list(self._procs.values()):
            try:
                proc.terminate()
            except Exception:  # noqa: BLE001 — already gone
                pass
        if self._config_path:
            try:
                os.unlink(self._config_path)
            except OSError:
                pass

    # -- internals ------------------------------------------------------------

    def _write_mcp_config(self):
        """One stdio MCP server per role, all pointed at this daemon. `--strict-mcp-config`
        plus this file means the session sees Parley's verbs and NOTHING else — none of
        the user's own MCP servers leak into the agent's context."""
        servers = {f"parley-{role}": {
            "command": sys.executable,
            "args": ["-m", "parley.mcp_server", "--base", self.base_url, "--role", role]}
            for role in self.roles}
        fd, path = tempfile.mkstemp(prefix="parley-mcp-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"mcpServers": servers}, f)
        return path

    def _model_for(self, role):
        """The brain runs on `llm: model`, the filter on `llm-gateway: model` — the split
        the cfg already declares. The filter wakes far more often, which is exactly why
        it gets the small one."""
        if role == "filter":
            return self.agent.llm_gateway_model or self.agent.llm_model
        return self.agent.llm_model

    def _argv(self, exe, role):
        from parley.agent.brain import TOOLS
        verbs = ["wait_for_work"] + [t.name for t in TOOLS]
        allowed = ",".join(f"mcp__parley-{role}__{name}" for name in verbs)
        prompt = _PROMPT.format(role=role, owner=self.agent.owner or "your user",
                                events=self.events_per_session)
        # Windows ships `claude` as a .cmd shim, which CreateProcess cannot exec.
        argv = (["cmd", "/c", exe] if exe.lower().endswith((".cmd", ".bat")) else [exe])
        return argv + ["-p", prompt, "--strict-mcp-config",
                       "--mcp-config", self._config_path,
                       "--allowedTools", allowed,
                       "--disallowedTools", ",".join(_OWN_TOOLS),
                       "--model", self._model_for(role),
                       "--output-format", "json"]

    def _keep_alive(self, exe, role):
        backoff = _BASE_BACKOFF
        while not self._stop.is_set():
            started = time.time()
            self.agent.log.log("session_start", role=role, model=self._model_for(role),
                               human=f"▶ starting the {role} session "
                                     f"({self._model_for(role)})")
            code, summary = self._run_once(exe, role)
            lived = time.time() - started
            if self._stop.is_set():
                return
            self.agent.log.log("session_exit", role=role, exit_code=code,
                               seconds=round(lived), summary=summary,
                               human=f"■ the {role} session ended after {lived:.0f}s "
                                     f"(exit {code}) — starting another")
            if lived >= _HEALTHY_SECONDS:
                backoff = _BASE_BACKOFF  # it ran properly; this was an ordinary recycle
            else:
                self.agent.log.log(
                    "warning", where="session_supervisor", role=role, backoff=backoff,
                    human=f"⚠ the {role} session died after {lived:.0f}s — waiting "
                          f"{backoff}s before retrying (quota? model name? CLI?)")
            self._stop.wait(backoff)
            if lived < _HEALTHY_SECONDS:
                backoff = min(backoff * 2, _MAX_BACKOFF)

    def _run_once(self, exe, role):
        kwargs = {}
        if sys.platform == "win32":
            # The daemon may itself be console-less; never pop a window for a session
            # the user is not supposed to see.
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            proc = subprocess.Popen(
                self._argv(exe, role), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
                cwd=self.cwd, env=child_env(), **kwargs)
        except OSError as err:
            return -1, f"could not start: {err}"
        self._procs[role] = proc
        out, err = proc.communicate()
        self._procs.pop(role, None)
        return proc.returncode, _summarize(out, err)


def _summarize(out, err):
    """The CLI's JSON envelope reduced to what belongs in a journal line: what it said,
    what it cost, and the error if it failed to start at all."""
    try:
        envelope = json.loads(out or "{}")
    except ValueError:
        return (out or err or "").strip()[:300]
    usage = envelope.get("usage") or {}
    return {"result": str(envelope.get("result") or "")[:300],
            "cost_usd": envelope.get("total_cost_usd"),
            "turns": envelope.get("num_turns"),
            "is_error": envelope.get("is_error"),
            "cache_read": usage.get("cache_read_input_tokens")}
