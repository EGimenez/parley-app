"""Parley server — the persistent Agent behind the client-server HTTP API.

Wraps one user's `Agent`: connects its accounts, runs the inbox poll loop in a
background thread, and serves the client-server protocol (client-server.md §3) on
127.0.0.1. The *same* server code backs every deployment; only `[server]
host/port` (and future auth) differ.

  GET  /v1/status   → identity + health (clients use it to detect a live server)
  POST /v1/instruct → {text} or a structured intent → act (UC-0a `book`)
  GET  /v1/pending  → escalations awaiting a human accept/deny (human-gating)
  POST /v1/decide   → {negotiation_id, choice} → resolve an escalation
  POST /v1/focus    → {action: create|list|cancel, ...} → Dynamic Focus Blocks
  POST /v1/dismiss  → {negotiation_id, reason} → close one WITHOUT answering it
                      (handled elsewhere / no longer matters; the brain is not woken)
  POST /v1/answer   → {ref|negotiation_id, answer|choice} → same, client-server naming
  POST /v1/negotiate → open a negotiation the /parley skill already researched
  POST /v1/consult  → run ONE read-only verb for a session helping the human decide
  GET/POST /v1/policy → read the effective policy / add a standing rule
  POST /v1/shutdown → stop the daemon

Blocked-session API (iteration_2026_09_19_claude-parley.md) — a background Claude Code
session drives a turn from its own side, through Parley's own gated tools:

  POST /v1/work/wait → {role, done_because, timeout} → ack what you held, BLOCK for the
                       next turn, return it with its frozen system prompt
  POST /v1/work/tool → {role, turn_id, name, input} → run one gated tool call
  GET  /v1/work/status → queue depths per role

The bidirectional client-server channel (agentic-architecture.md §3): the agent asks via
`/v1/pending` (and emails the user); the human answers via `/v1/decide` / `/v1/answer`
(or by replying to the email).

The poll loop and the HTTP handlers both touch the Agent (and its provider I/O),
so a single lock serialises them — no concurrent calendar mutation. The ONE exception is
`/v1/work/wait`, which blocks for as long as the caller asks: it takes no lock (the work
queue has its own), because holding one there would freeze the poll loop behind it.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from parley.core import as_addresses



class ParleyServer:
    def __init__(self, agent, host="127.0.0.1", port=8787):
        self.agent = agent
        self.host = host
        self.port = port
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._httpd = None
        # role -> the turn that role's session is currently holding. One at a time by
        # construction: a session cannot ask for another until it finishes this one.
        self._held = {}
        self._supervisor = None

    # -- API actions (each returns a JSON-able object) ------------------------

    def status(self):
        return {
            "name": self.agent.name,
            "addresses": sorted(self.agent.addresses),
            "connections": [c.name for c in self.agent.connections],
            "open_negotiations": len(self.agent.short_term_memory.open()),
            "pending": len(self.agent.user_messaging.pending),
        }

    def pending(self):
        with self._lock:
            return self.agent.pending_decisions()

    # -- the blocked-session API (iteration_2026_09_19_claude-parley.md) -------
    #
    # A background Claude Code session drives a turn from its own side: it waits here
    # for work, calls tools back through here, and finishes by asking for the next
    # turn. The guardrail, the journal and idempotent settlement stay in this process.

    #: Tool calls one session-driven turn may make before we cut it off. Mirrors the
    #: in-process loop's MAX_STEPS — a session that keeps calling tools is stuck, and
    #: without a bound it would hold its lease until the watchdog fires.
    MAX_TOOL_CALLS = 16

    def work_wait(self, body):
        """Acknowledge the turn this role is holding, then block for the next one.

        Finishing is not a separate call on purpose: the ONLY way to end a turn is to
        ask for the next, and doing so requires saying what you concluded. That keeps
        the journal able to tell *investigated and concluded* from *got stuck* — the
        distinction the no-silent-inaction backstop cannot make on its own.

        Deliberately takes NO agent lock: this call blocks for as long as the caller
        asked, and holding the lock here would freeze the poll loop with it.
        """
        role = str(body.get("role") or "brain")
        queue = self.agent.work_queue(role)
        held = self._held.pop(role, None)
        if held:
            queue.ack(held["item_id"], note=str(body.get("done_because") or ""))
            self.agent.log.log("work_done", role=role, turn_id=held["item_id"],
                               done_because=str(body.get("done_because") or ""),
                               tool_calls=held["calls"], acted=held["acted"])
            # The guards that run AFTER a turn must not depend on who drove it.
            with self._lock:
                self.agent.after_session_turn(held["turn"], held["acted"])
        try:
            timeout = float(body.get("timeout") or 300)
        except (TypeError, ValueError):
            timeout = 300.0
        item = queue.lease(timeout=min(max(timeout, 1.0), 3600.0), stop=self._stop)
        if item is None:
            return {"empty": True}
        try:
            turn = self.agent.brain.turn_from_payload(item["payload"])
        except Exception as err:  # noqa: BLE001 — a turn we cannot rebuild is poison
            queue.ack(item["id"], note=f"undeliverable: {err}")
            self.agent.log.log("error", where="work_wait", error=str(err),
                               human=f"dropped a queued turn we could not rebuild: {err}")
            return {"empty": True, "error": str(err)}
        self._held[role] = {"item_id": item["id"], "turn": turn, "calls": 0, "acted": False}
        ctx = self.agent.brain.context(turn)
        self.agent.log.log("brain_turn", account=turn.conn.name, event=turn.event,
                           driver=f"session:{role}", turn_id=item["id"])
        return {"turn_id": item["id"], "kind": turn.kind, "event": turn.event,
                "system": ctx["system"], "negotiation_id": turn.negotiation_id,
                "account": turn.conn.address(), "attempt": item["attempts"]}

    def work_tool(self, body):
        """Run one tool call for the turn a role is holding. Takes the agent lock: this
        is where calendars get written, and it must serialise with the poll loop."""
        role = str(body.get("role") or "brain")
        held = self._held.get(role)
        if not held:
            return {"ok": False, "error": "no turn is being held — call wait_for_work first"}
        if body.get("turn_id") and body["turn_id"] != held["item_id"]:
            return {"ok": False, "error": f"turn {body['turn_id']} is not the one you hold "
                                          f"({held['item_id']}) — it was probably redelivered"}
        if held["calls"] >= self.MAX_TOOL_CALLS:
            return {"ok": False, "error": f"tool-call budget for this turn is spent "
                                          f"({self.MAX_TOOL_CALLS}) — finish and call "
                                          f"wait_for_work with what you concluded"}
        held["calls"] += 1
        from parley.agent.agent import _ACTING_TOOLS
        from parley.agent.llm_port import ToolCall
        call = ToolCall(f"s-{held['calls']}", str(body.get("name") or ""),
                        dict(body.get("input") or {}))
        with self._lock:
            self.agent.current_turn_message = getattr(held["turn"], "message", None)
            try:
                result = self.agent.brain.dispatch(held["turn"].conn, call)
            finally:
                self.agent.current_turn_message = None
        # "Did this turn actually do anything?" is what the no-silent-inaction
        # backstop asks when the turn ends, so record it as it happens.
        if call.name in _ACTING_TOOLS and (result or {}).get("ok"):
            held["acted"] = True
        return result

    # -- the human-facing surface (a Claude session helping you decide) -------
    #
    # The tray has always been a thin client of /v1/pending + /v1/decide: see a
    # question, press a button. A Claude session can be a better client of the same
    # thing, because a question like "this reply fits two of your meetings, which?" is
    # a CONVERSATION — you want to ask what the other one was about before choosing.
    # That needs read access to the agent's own context, and nothing more.

    #: Verbs a consulting session may run. READ-ONLY, by name, on purpose: this is the
    #: security boundary of the whole human surface. `close` and `defer_action` are
    #: memory writes and `send`/`apply` act on the world — none of them belong to a
    #: session whose entire job is to help the human answer a question.
    CONSULT_TOOLS = ("check", "get_negotiation", "find_contact", "search_events")

    def consult(self, body):
        """Run one READ-ONLY verb for a session that is helping the human decide.

        Unlike /v1/work/tool this needs no turn in flight — nobody is holding a lease,
        the human just wants to look something up. It takes the agent lock anyway:
        these read the same provider connections the poll loop uses."""
        name = str(body.get("name") or "")
        if name not in self.CONSULT_TOOLS:
            return {"ok": False,
                    "error": f"{name!r} is not available here. A consulting session may "
                             f"only read: {', '.join(self.CONSULT_TOOLS)}."}
        conn = (self.agent._conn_for_address(body["account"]) if body.get("account")
                else self.agent.default_conn)
        from parley.agent.llm_port import ToolCall
        with self._lock:
            return self.agent.brain.dispatch(conn, ToolCall("consult", name,
                                                            dict(body.get("input") or {})))

    def policy(self, body=None):
        """GET: the policy the brain actually runs on. POST {rule}: add a standing rule.

        Writes land in the learned-policy file, which exists for exactly this — rules
        the user states in passing, kept beside the cfg's own prose rather than inside
        it. The cfg's structured trust table is NOT writable here: it is enforced
        before the brain reasons and lives in a file holding inline secrets, so
        rewriting it from a chat session is a different decision with different risks."""
        conn = self.agent.default_conn
        if not body or not body.get("rule"):
            return {"ok": True, "account": conn.address(),
                    "effective": self.agent.effective_policy_text(conn),
                    "trust": (self.agent.policy_for(conn).structured or {}).get("trust", {}),
                    "note": "trust entries are cfg-only; ask the user to edit the cfg for those"}
        with self._lock:
            result = self.agent.remember_policy(str(body["rule"]))
        self.agent.log.log("policy_added", rule=str(body["rule"]), via="human-session",
                           human=f"🧠 new standing rule from a Claude session: {body['rule']}")
        return result

    def negotiate(self, body):
        """Open a negotiation somebody else already did the legwork for.

        This is path A of the blocked-session design: the /parley skill, running inside
        the user's own Claude Code session, resolved the peer, looked at free/busy over
        its own connectors and drafted the prose. It then hands the result here, because
        of the one rule the whole design rests on: **Claude discovers, Parley commits**.
        Everything outward-facing still goes through the gated verbs, so the guardrail,
        the journal and idempotent settlement apply exactly as they do to the brain.

        Two shapes, and which one you get is decided by the caller's certainty:

          * `slot` + `verified: true` — you KNOW both sides are free (you read their
            calendar), so the proposal is an invitation. The invitation IS the hold:
            the event exists from the moment it is proposed, and the peer can decline.
          * otherwise — a plain message carrying `body`, which opens the ordinary
            negotiation and lets the agents work it out.

        `context` is not decoration. A reply arrives days later, to a background session
        that never saw the user type anything, so whatever mattered about the request
        has to be written into Parley now or it is gone.
        """
        from parley.agent import wire
        # `to` is one person or several (a list, or comma/semicolon-separated): a
        # meeting with several people is ONE conversation with all of them, both as
        # mail and as a calendar invitation.
        recipients = as_addresses(body.get("to"))
        to = ", ".join(recipients)
        topic = str(body.get("topic") or "").strip()
        if not recipients or not topic:
            return {"ok": False, "error": "negotiate needs at least `to` and `topic`"}
        conn = (self.agent._conn_for_address(body["account"]) if body.get("account")
                else self.agent.default_conn)
        slot = body.get("slot") or {}
        verified = bool(body.get("verified")) and slot.get("start") and slot.get("end")
        mem = self.agent.short_term_memory
        gate = self.agent.accounts_gate

        with self._lock:
            ref = None
            try:
                if verified:
                    ref = wire.new_ref()
                    mem.upsert(ref, topic=topic, direction="outgoing", status="open",
                               account=conn.address())
                    mem.record(ref, f"opened from /parley: {to} was verified free, so the "
                                    f"proposal goes out as an invitation")
                    result = gate.apply(conn, change="create", ref=ref, attendee=to,
                                        start=slot["start"], end=slot["end"],
                                        title=body.get("title") or topic,
                                        timezone=body.get("timezone"))
                else:
                    prose = str(body.get("body") or "").strip()
                    if not prose:
                        return {"ok": False,
                                "error": "without a verified slot, negotiate needs a `body` to send"}
                    result = gate.send(conn, to=recipients, body=prose,
                                       subject=body.get("subject") or topic, topic=topic)
                    ref = (result or {}).get("negotiation_id")
            except Exception as err:  # noqa: BLE001 — e.g. Outlook COM refusing Send()
                # An uncaught error here used to drop the HTTP connection with no
                # answer and leave the negotiation `open` forever with nothing sent
                # (user report 2026-09-24). Settle it and say what failed instead.
                if ref and mem.get(ref) is not None:
                    mem.record(ref, f"cancelled: the account refused it ({err})")
                    mem.close(ref, "cancelled")
                self.agent.log.log("negotiate_failed", negotiation_id=ref, to=to,
                                   topic=topic, error=str(err),
                                   human=f"❌ /parley could not open '{topic}' with {to}: {err}")
                return {"ok": False, "negotiation_id": ref, "account": conn.address(),
                        "error": f"the account refused it: {err}"}

            if ref:
                for label, value in (("context", body.get("context")),
                                     ("deadline", body.get("deadline"))):
                    if value:
                        mem.record(ref, f"{label} from your instruction: {value}")
        self.agent.log.log("negotiate", negotiation_id=ref, to=to, topic=topic,
                           verified=bool(verified), account=conn.address(),
                           human=f"📨 /parley opened {ref} with {to} about '{topic}'"
                                 f"{' (invitation sent)' if verified else ''}")
        return {**(result or {}), "negotiation_id": ref, "account": conn.address(),
                "invited": bool(verified)}

    def _start_sessions(self):
        """With llm: driver: session, bring up the background Claude Code session that
        drains the brain queue. Only the brain for now: nothing pushes to the filter
        queue yet, and a session waiting on a queue no one fills is just a process.

        A failure here is NOT fatal. Events keep queueing durably, so the agent
        catches up the moment a session comes up — losing the session must not lose
        the mail."""
        if not self.agent.session_driven:
            return
        from parley.agent.session_supervisor import SessionSupervisor
        self._supervisor = SessionSupervisor(
            self.agent, base_url=f"http://{self.host}:{self.port}", roles=("brain",))
        if not self._supervisor.start():
            self._supervisor = None

    def work_status(self):
        return {role: self.agent.work_queue(role).stats() for role in ("brain", "filter")}

    def decide(self, body):
        with self._lock:
            before = set(self.agent.user_messaging.pending)
            result = self.agent.decide(body.get("negotiation_id"), body.get("choice"))
        return self._report(before, result)

    def answer(self, body):
        """client-server naming for `decide` — accepts `ref`/`answer` as aliases of
        `negotiation_id`/`choice` (matches ACCEPT/DENY on the tray)."""
        with self._lock:
            before = set(self.agent.user_messaging.pending)
            result = self.agent.decide(
                body.get("negotiation_id") or body.get("ref"),
                body.get("choice") or body.get("answer"))
        return self._report(before, result)

    def focus(self, body):
        """Dynamic Focus Blocks: create one, list them, cancel one (agent/focus.py)."""
        action = str(body.get("action") or "create").lower()
        with self._lock:
            if action == "list":
                return {"ok": True, "blocks": self.agent.focus.list()}
            if action == "cancel":
                return self.agent.focus.cancel(str(body.get("dfb_id") or ""),
                                               body.get("reason") or "cancelled by the user")
            if action == "create":
                return self.agent.focus.create(body)
        return {"ok": False, "error": f"unknown focus action {action!r}: "
                                      f"create / list / cancel"}

    def dismiss(self, body):
        """Close a question without answering it. No brain turn, nothing sent: the
        user has dealt with it some other way, or it simply no longer matters."""
        with self._lock:
            result = self.agent.dismiss(body.get("negotiation_id") or body.get("ref"),
                                        body.get("reason"))
        return {"status": "done", "accepted": bool(result.get("ok")),
                "message": result.get("text") or result.get("message", ""), **result}

    def instruct(self, body):
        """A free-text instruction ({text}) the brain plans + acts on. Always answers
        with either DONE (+summary) or a QUESTION the agent needs answered — so the
        client can stay attached and reply (topic 1)."""
        text = body.get("text", "")
        if not text:
            return {"status": "done", "accepted": False, "message": "no instruction text"}
        with self._lock:
            before = set(self.agent.user_messaging.pending)
            result = self.agent.instruct(text)
        return self._report(before, result)

    def _report(self, before_refs, result):
        """Turn a brain run into a client-facing reply. If the run raised a *new*
        escalation, return it as a QUESTION (with its ref); otherwise DONE + the brain's
        summary. `accepted`/`message` are kept for older clients."""
        new = [r for r in self.agent.user_messaging.pending if r not in before_refs]
        if new:
            ref = new[0]
            pq = self.agent.user_messaging.pending[ref]
            return {"status": "question", "accepted": True, "ref": ref,
                    "negotiation_id": ref, "question": pq.question,
                    "title": pq.title or "", "message": pq.question,
                    "kind": pq.kind, "summary": pq.summary, "from": pq.who,
                    "start": pq.start}
        text = (result or {}).get("text") or "done"
        return {"status": "done", "accepted": bool((result or {}).get("ok", True)),
                "message": text}

    def shutdown(self):
        self._stop.set()
        if self._httpd is not None:
            # serve_forever() must be stopped from another thread.
            threading.Thread(target=self._httpd.shutdown, daemon=True).start()
        return {"ok": True}

    # -- Background poll loop -------------------------------------------------

    def _poll_loop(self):
        # Questions left over from before this start: close what no longer needs an
        # answer (meeting ended or over, invite answered in the calendar) first, so
        # the tray never re-shows them.
        with self._lock:
            try:
                self.agent.backfill_questions()
                self.agent.sweep_questions(calendar=True)
                self.agent.focus.reconcile()
            except Exception as err:  # noqa: BLE001 — housekeeping never stops the loop
                self.agent.log.log("error", where="sweep_questions", error=str(err))
        while not self._stop.wait(self.agent.poll_seconds):
            with self._lock:
                for instruction in self.agent.user_messaging.drain()[0]:
                    try:
                        self.agent.instruct(instruction.text)
                    except Exception as err:  # noqa: BLE001 — keep the loop alive
                        self.agent.log.log("error", where="instruct", error=str(err))
                for conn in self.agent.connections:
                    try:
                        messages = conn.list_messages()
                    except Exception as err:  # noqa: BLE001 — keep the loop alive
                        self.agent.log.log(
                            "error", where="poll", account=conn.name, error=str(err),
                            human=f"poll error on {conn.name}: {err}")
                        continue
                    for message in messages:
                        try:
                            self.agent.handle(message, conn)
                        except Exception as err:  # noqa: BLE001
                            self.agent.log.log(
                                "error", where="handle", message_id=message.id,
                                error=str(err),
                                human=f"error handling {message.id}: {err}")
                self.agent._retry_pending_rsvps()
                self.agent._check_stalled()
                self.agent.sweep_questions()
                try:
                    self.agent.focus.sweep()
                except Exception as err:  # noqa: BLE001 — keep the loop alive
                    self.agent.log.log("error", where="focus_sweep", error=str(err))

    # -- Lifecycle ------------------------------------------------------------

    def serve(self):
        print(f"Parley server connecting ({self.agent.name})...")
        for conn in self.agent.connections:
            conn.connect()  # first run: OAuth browser redirect happens here
        addrs = ", ".join(sorted(self.agent.addresses))
        self.agent.log.log(
            "server_start", addresses=sorted(self.agent.addresses),
            model=self.agent.llm_model, poll_seconds=self.agent.poll_seconds,
            accounts=[c.name for c in self.agent.connections],
            api=f"http://{self.host}:{self.port}",
            human=f"Connected as {addrs}. Client-server API on http://{self.host}:{self.port} "
                  f"(poll {self.agent.poll_seconds}s). Ctrl+C to stop.")
        self.agent.check_timezones()
        threading.Thread(target=self._poll_loop, daemon=True).start()
        self._start_sessions()
        self._httpd = ThreadingHTTPServer((self.host, self.port), _make_handler(self))
        try:
            self._httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            self._stop.set()
            if self._supervisor:
                self._supervisor.stop()
            self._httpd.server_close()


def _make_handler(server):
    class _Handler(BaseHTTPRequestHandler):
        def _send(self, code, obj):
            data = json.dumps(obj).encode("utf-8")
            try:
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except (ConnectionError, OSError):
                # The client hung up before we finished writing (common when a poller
                # times out or is cancelled — e.g. WinError 10053). Nothing to send to;
                # drop it quietly instead of dumping a per-request traceback.
                pass

        def _read_json(self):
            length = int(self.headers.get("Content-Length", 0) or 0)
            if not length:
                return {}
            try:
                return json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                return {}

        def do_GET(self):
            if self.path == "/v1/status":
                self._send(200, server.status())
            elif self.path == "/v1/pending":
                self._send(200, server.pending())
            elif self.path == "/v1/policy":
                self._send(200, server.policy())
            elif self.path == "/v1/work/status":
                self._send(200, server.work_status())
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):
            body = self._read_json()
            # log every mutating API call (GET /status noise stays out).
            server.agent.log.log("http", path=self.path, body=body)
            if self.path == "/v1/instruct":
                self._send(200, server.instruct(body))
            elif self.path == "/v1/decide":
                self._send(200, server.decide(body))
            elif self.path == "/v1/answer":
                self._send(200, server.answer(body))
            elif self.path == "/v1/focus":
                self._send(200, server.focus(body))
            elif self.path == "/v1/dismiss":
                self._send(200, server.dismiss(body))
            elif self.path == "/v1/consult":
                self._send(200, server.consult(body))
            elif self.path == "/v1/policy":
                self._send(200, server.policy(body))
            elif self.path == "/v1/negotiate":
                self._send(200, server.negotiate(body))
            elif self.path == "/v1/work/wait":
                self._send(200, server.work_wait(body))
            elif self.path == "/v1/work/tool":
                self._send(200, server.work_tool(body))
            elif self.path == "/v1/shutdown":
                self._send(200, server.shutdown())
            else:
                self._send(404, {"error": "not found"})

        def log_message(self, *args):
            pass  # silence stderr per-request logging

    return _Handler


# -- Entry point: start a server from a config (the server's main lives here) --

def serve_from_config(config_file=None, provider=None):
    """Build the agent (via `agent.compose`) for a YAML config — or a single env
    backend — and serve the client-server HTTP API. The one place "start the
    server" lives; `parley_start_server` and `python -m parley --serve` both call
    here."""
    import time as _time
    from parley.agent.compose import (build_agent, load_llm_config, resolve_accounts,
                                       set_window_title)
    from parley.client_server.config import (cfg_display_name, clear_autostart_suppression,
                                             deregister_server, load_server,
                                             register_server, scaffold_home,
                                             suppress_autostart)
    scaffold_home()  # first-run: seed ~/.parley with example configs + a logs/ dir
    # The config's own filename, not Agent's built-in fallback (the connector's
    # class-level backend label, e.g. "gmail" — identical for every config on
    # that backend, so two gmail accounts would be indistinguishable everywhere
    # this name shows: the registry, `--status`, the tray picker).
    name = cfg_display_name(config_file) if config_file else None
    agent = build_agent(resolve_accounts(provider=provider, config_file=config_file),
                        llm=load_llm_config(config_file), name=name)
    host, port = load_server(config_file) if config_file else ("127.0.0.1", 8787)
    set_window_title(f"Parley — {agent.name} (:{port})")
    # Servers registry (iteration 22 D9/D13): register on start; deregister on any
    # deliberate stop (the finally covers /v1/shutdown and Ctrl+C). A crash/kill/
    # reboot skips it — leaving the "registered-but-down" entry the tray revives.
    if config_file:
        register_server(config_file, name=agent.name, host=host, port=port,
                        started=_time.strftime("%Y-%m-%dT%H:%M:%S"))
        # A real start, however it was triggered (tray revive, manual `parley -cfg`,
        # `--serve`): trust autostart again from here on.
        clear_autostart_suppression(config_file)
    try:
        ParleyServer(agent, host, port).serve()
    finally:
        if config_file:
            deregister_server(config_file)
            # Same "deliberate stop" moment as the deregister above — suppress the
            # NEXT tray launch's autostart seed too, not just this session's revives
            # (user report 2026-09-05: Quit looked broken because the tray's next
            # launch silently brought everything back).
            suppress_autostart(config_file)


def main(argv=None):
    """`parley-server` console script + the `parley_start_server` module entry."""
    import argparse
    parser = argparse.ArgumentParser(
        prog="parley-server",
        description="Start the persistent Parley client-server server (client-server.md).")
    parser.add_argument("provider", nargs="?",
                        help="single env backend to serve: gmail | outlook | local-outlook | memory")
    parser.add_argument("--config-file", dest="config_file",
                        help="YAML file listing the accounts to serve "
                             "(see ~/.parley/cfg.gmail.example.yaml, seeded on first run)")
    args = parser.parse_args(argv)
    return serve_from_config(config_file=args.config_file, provider=args.provider)
