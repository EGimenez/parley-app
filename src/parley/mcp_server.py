"""Parley as an MCP server — the surface a blocked Claude Code session drives.

Phase 1 of `docs/iterations/iteration_2026_09_19_claude-parley.md`. Claude Code spawns
this over stdio (`--mcp-config`), and it forwards every call to the agent's own daemon
over the local HTTP API. It holds no state and makes no decisions: the guardrail, the
journal, tz conversion and idempotent settlement all stay in the daemon.

    claude  ──stdio/JSON-RPC──▶  this  ──HTTP──▶  parley daemon ──▶ AccountsGate

**Why the wait loops in here.** `wait_for_work` must not return to the model until there
is real work. A pending tool call costs nothing — the model has emitted the call and
stopped — but every *return* is a turn, and a turn costs tokens. So the HTTP request
stays bounded (the daemon answers `{"empty": true}` after its timeout, which keeps
sockets healthy) and THIS process just asks again. The model sees one call that happens
to take six hours, and pays for none of the waiting. Get this layering wrong and the
whole design collapses back into paying per tick.

Run: `python -m parley.mcp_server --base http://127.0.0.1:8787 --role brain`
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

PROTOCOL_VERSION = "2025-06-18"
#: How long one HTTP wait may block before we ask again. Long enough that an idle agent
#: makes ~12 requests an hour, short enough that a dead daemon is noticed quickly.
WAIT_SECONDS = 300
#: Backoff when the daemon is unreachable — it may simply be restarting.
RETRY_SECONDS = 5

WAIT_TOOL = {
    "name": "wait_for_work",
    "description":
        "Block until Parley has an event for you, then return it with everything you "
        "need to decide: the policy, the open negotiations, the current time, and the "
        "event itself. This is how you finish a turn AND get the next one — there is no "
        "separate 'done' call. Pass `done_because` describing what you concluded about "
        "the turn you just handled (leave it empty only on your very first call). "
        "Waiting here is free and may take hours; that is normal and expected, so call "
        "it and wait rather than polling.",
    "inputSchema": {
        "type": "object",
        "properties": {"done_because": {
            "type": "string",
            "description": "What you concluded about the turn you just finished — what "
                           "you did, or why nothing was needed. Recorded in the journal; "
                           "'investigated and concluded' must be distinguishable from "
                           "'got stuck'."}},
        "required": [],
    },
}


#: The verbs a consulting session may run — read-only, and the daemon enforces the
#: same list again at /v1/consult. Named here only so the session is OFFERED the right
#: ones; the boundary is on the other side, where it cannot be edited by a prompt.
HUMAN_READ_TOOLS = ("check", "get_negotiation", "find_contact", "search_events")

PENDING_TOOL = {
    "name": "pending",
    "description":
        "The questions your agent is waiting on you to answer. Each has a ref, a "
        "`kind` (invite, email, stalled, ...), a one-line `summary`, who / title / "
        "start / end when known, and the full `question`. Show the summaries first; "
        "open the full question only for the one being discussed. Start here.",
    "inputSchema": {"type": "object", "properties": {}, "required": []},
}

ANSWER_TOOL = {
    "name": "answer",
    "description":
        "Answer one pending question on the user's behalf, once they have told you "
        "what they want. `text` is free-form — 'accept', 'no, propose Thursday "
        "instead', 'ask them what it is about first'. The agent re-plans on it. Only "
        "ever call this with a decision the user actually made: you are relaying "
        "their answer, not deciding for them.",
    "inputSchema": {"type": "object", "properties": {
        "ref": {"type": "string", "description": "the pending question's ref"},
        "text": {"type": "string", "description": "the user's decision, in their words"}},
        "required": ["ref", "text"]},
}

DISMISS_TOOL = {
    "name": "dismiss",
    "description":
        "Close a pending question WITHOUT answering it, when the user says it is "
        "already handled ('ya está', 'lo contesté en Outlook', 'olvídalo') or it no "
        "longer matters. The agent is not woken and nothing is sent. Use `answer` "
        "instead whenever the user wants the agent to DO something.",
    "inputSchema": {"type": "object", "properties": {
        "ref": {"type": "string", "description": "the pending question's ref"},
        "reason": {"type": "string", "description": "why, in a few words"}},
        "required": ["ref"]},
}

POLICY_TOOL = {
    "name": "policy",
    "description":
        "With no arguments: the policy the agent actually runs on, including rules it "
        "has learned. With `rule`: add a standing rule for the future — 'never book me "
        "before 10', 'meetings with the board always need my approval'. Use it when "
        "the user states a preference that outlives the question in front of you; a "
        "one-off decision belongs in `answer`, not here. Trust entries (always accept "
        "X, ignore Y) live in the cfg and cannot be set from here — say so and let "
        "the user edit it.",
    "inputSchema": {"type": "object", "properties": {
        "rule": {"type": "string", "description": "omit to READ the current policy"}},
        "required": []},
}


class Bridge:
    def __init__(self, base, role, timeout=WAIT_SECONDS):
        self.base = base.rstrip("/")
        self.role = role
        self.timeout = timeout

    # -- HTTP ----------------------------------------------------------------

    def _post(self, path, body, timeout):
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(f"{self.base}{path}", data=data, method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read() or b"{}")

    # -- the two things a session can do -------------------------------------

    def wait(self, done_because):
        """Ask the daemon for work until there IS work. Never returns empty: an empty
        return would cost the model a turn for nothing."""
        first = True
        while True:
            try:
                # `done_because` only rides the FIRST request of the loop — it
                # acknowledges the turn we just finished, and re-sending it on every
                # retry would log the same conclusion again and again.
                answer = self._post("/v1/work/wait",
                                    {"role": self.role, "timeout": self.timeout,
                                     "done_because": done_because if first else ""},
                                    timeout=self.timeout + 30)
                first = False
                if not answer.get("empty"):
                    return answer
            except urllib.error.URLError as err:
                _warn(f"daemon unreachable ({err}); retrying in {RETRY_SECONDS}s")
                time.sleep(RETRY_SECONDS)
            except Exception as err:  # noqa: BLE001 — a bridge that dies takes the agent with it
                _warn(f"wait failed ({type(err).__name__}: {err}); retrying in {RETRY_SECONDS}s")
                time.sleep(RETRY_SECONDS)

    def tool(self, name, arguments):
        return self._post("/v1/work/tool",
                          {"role": self.role, "name": name, "input": arguments},
                          timeout=180)

    # -- the human role ------------------------------------------------------

    def _get(self, path, timeout=30):
        req = urllib.request.Request(f"{self.base}{path}", method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read() or b"{}")

    def human_call(self, name, arguments):
        """Route one consulting-session tool. Reads go to /v1/consult, which enforces
        the read-only list again on its own side; answering and policy have their own
        endpoints because they are the two things this session is allowed to change."""
        if name == "pending":
            return self._get("/v1/pending")
        if name == "answer":
            return self._post("/v1/decide",
                              {"negotiation_id": arguments.get("ref"),
                               "choice": arguments.get("text")}, timeout=180)
        if name == "dismiss":
            return self._post("/v1/dismiss",
                              {"negotiation_id": arguments.get("ref"),
                               "reason": arguments.get("reason") or ""}, timeout=30)
        if name == "policy":
            rule = (arguments.get("rule") or "").strip()
            return (self._post("/v1/policy", {"rule": rule}, timeout=60) if rule
                    else self._get("/v1/policy"))
        return self._post("/v1/consult", {"name": name, "input": arguments}, timeout=120)


def _warn(message):
    """stderr only — stdout is the JSON-RPC channel and must carry nothing else."""
    print(f"[parley-mcp] {message}", file=sys.stderr, flush=True)


def _turn_text(turn):
    """One turn rendered for the model: its frozen context, then the event, then how to
    finish. The system prompt travels as tool output rather than as a system prompt
    because Claude Code owns the session's system prompt and we only get to append."""
    return (f"=== PARLEY TURN {turn.get('turn_id', '?')} "
            f"({turn.get('kind', 'event')}, account {turn.get('account', '?')}"
            f"{', REDELIVERED' if (turn.get('attempt') or 1) > 1 else ''}) ===\n\n"
            f"{turn.get('system', '')}\n\n"
            f"--- THE EVENT ---\n\n{turn.get('event', '')}\n\n"
            f"--- WHEN YOU ARE DONE ---\n\n"
            f"Call wait_for_work again with done_because set to what you concluded. "
            f"That both finishes this turn and gets you the next one.")


def _tool_list(role="brain"):
    """What this role is offered.

    A brain/filter session gets `wait_for_work` plus Parley's own verbs, whose schemas
    are the SAME objects the in-process brain uses — one definition, two drivers.

    A `human` session gets a deliberately different set: it is sitting with the user
    while they decide, so it may READ the agent's context, relay their answer and
    record a standing rule. It is offered no way to send mail or touch a calendar,
    because helping someone decide is not the same job as acting for them."""
    from parley.agent.brain import TOOLS
    by_name = {t.name: {"name": t.name, "description": t.description,
                        "inputSchema": t.input_schema} for t in TOOLS}
    if role == "human":
        return ([PENDING_TOOL, ANSWER_TOOL, DISMISS_TOOL, POLICY_TOOL]
                + [by_name[n] for n in HUMAN_READ_TOOLS if n in by_name])
    return [WAIT_TOOL] + list(by_name.values())


def serve(bridge, stdin=None, stdout=None):
    """The JSON-RPC loop. Newline-delimited JSON on stdio, as MCP's stdio transport
    specifies. Returns when stdin closes (Claude Code has gone)."""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    tools = _tool_list(bridge.role)

    def reply(msg_id, result=None, error=None):
        if msg_id is None:
            return  # a notification: never answer one
        out = {"jsonrpc": "2.0", "id": msg_id}
        out["error" if error else "result"] = error or result
        stdout.write(json.dumps(out) + "\n")
        stdout.flush()

    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except ValueError:
            continue  # not our framing; ignore rather than die
        method, msg_id, params = (request.get("method"), request.get("id"),
                                  request.get("params") or {})

        if method == "initialize":
            reply(msg_id, {"protocolVersion": PROTOCOL_VERSION,
                           "capabilities": {"tools": {}},
                           "serverInfo": {"name": f"parley-{bridge.role}", "version": "1"}})
        elif method == "tools/list":
            reply(msg_id, {"tools": tools})
        elif method == "tools/call":
            name = params.get("name") or ""
            arguments = params.get("arguments") or {}
            try:
                if bridge.role == "human":
                    payload = json.dumps(bridge.human_call(name, arguments), default=str)
                elif name == "wait_for_work":
                    payload = _turn_text(bridge.wait(str(arguments.get("done_because") or "")))
                else:
                    payload = json.dumps(bridge.tool(name, arguments), default=str)
                reply(msg_id, {"content": [{"type": "text", "text": payload}]})
            except Exception as err:  # noqa: BLE001 — a failed tool is a result, not a crash
                reply(msg_id, {"content": [{"type": "text",
                                            "text": f"{type(err).__name__}: {err}"}],
                               "isError": True})
        elif method == "ping":
            reply(msg_id, {})
        elif msg_id is not None:
            reply(msg_id, error={"code": -32601, "message": f"unknown method {method}"})
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="parley.mcp_server",
        description="Serve Parley's gated verbs to a background Claude Code session.")
    parser.add_argument("--base", default="http://127.0.0.1:8787",
                        help="the agent daemon's HTTP base URL")
    parser.add_argument("--role", default="brain", choices=("brain", "filter", "human"),
                        help="brain/filter drain a work queue; human serves a session "
                             "sitting with the user while they answer a question")
    parser.add_argument("--wait-seconds", type=int, default=WAIT_SECONDS,
                        help="how long one HTTP wait blocks before asking again")
    args = parser.parse_args(argv)
    _warn(f"serving role={args.role} against {args.base}")
    return serve(Bridge(args.base, args.role, timeout=args.wait_seconds))


if __name__ == "__main__":
    sys.exit(main())
