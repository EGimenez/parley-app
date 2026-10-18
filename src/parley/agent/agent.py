"""Agent — one per user; composition root + dispatch (2026-08-31 refactor).

Collapsed from the old 12-piece list (Account, run loop, Guardrail, Gateway,
Brain-per-account, ToolBelt, Policy-per-account, NegotiationMemory, Journal,
wire.py, awaiting) to five owned pieces plus two pure functions:

  * Accounts    — the connectors this agent drives (each with its own Gateway
                  relevance config, applied here, not inside the connector).
  * UserMessaging — the bidirectional human channel (instructions in,
                  questions out) — its own channel now, not smuggled through
                  the account's mailbox.
  * ShortTermMemory — working state across concurrent negotiations.
  * Log         — the flight recorder (a SIBLING, never read by the brain).
  * Policy      — one global prose + trust table, with an optional per-account
                  delta layered on top (`policy.effective_policy`).
  * Brain       — ONE instance (not per-account); AccountsGate is its hands.
  * permission() / gateway.route() — pure functions, not per-account objects.

No owned run loop: `handle()`/`instruct()`/`decide()` are the reactive entry
points; whoever drives them (a poll loop, or a future skill's on-demand
invocation) is external.
"""

import json
import os
import re
import time

from parley.connectors.accounts.account import InviteNotReady
from parley.core import Message, Slot, from_iso, html_to_text, to_iso
from parley.agent import gateway, wire
from parley.agent.accounts_gate import AccountsGate
from parley.agent.brain import Brain, _read_learned
from parley.agent.focus import FocusBlocks
from parley.agent.guardrail import ASK, DENY, HANDLE_INBOUND, Action, permission, trusted
from parley.agent.llm_port import build_llm_client
from parley.agent.log import Log
from parley.agent.policy import Policy, effective_policy, load_policy
from parley.agent.short_term_memory import ShortTermMemory
from parley.agent.user_messaging import UserMessaging, one_line

# Providers that authenticate by API key. A local brain (ollama) needs none — its
# base_url is the whole address — and neither does claude-code, which rides the
# user's own Claude Code CLI login. A missing key must NOT mean "no client" for them.
_KEYLESS_PROVIDERS = {"ollama", "claude-code", "claude_code", "claudecode"}


def _provider_needs_key(provider):
    return (provider or "anthropic").strip().lower() not in _KEYLESS_PROVIDERS


# The start of a quoted reply (kept for the ambiguous-reply candidate text and
# reschedule notes — not for self-answer detection anymore, that channel moved
# to UserMessaging entirely).
_QUOTED_REPLY_RE = re.compile(
    r"^(On .+wrote:|El .+escribi|From:\s.+|De:\s.+|-{2,}\s*(Original|Forwarded) [Mm]essage)")

_AFFIRMATIVE = {"accept", "accepted", "yes", "y", "ok", "okay", "approve",
                "approved", "sure", "go", "do"}


def _affirmative(choice):
    words = str(choice or "").strip().lower().replace(",", " ").replace(".", " ").split()
    return bool(words) and words[0] in _AFFIRMATIVE


_SUBJECT_TOPIC_RE = re.compile(
    r"^(?:\s*(?:re|fwd|fw|aw|sv|rv)\s*:\s*)*"
    r"(?:\[PARLEY\]\s*[^:]*:\s*)?"
    r"(.*)$", re.I)

# Tool calls that count as "acting" on an inbound event — used by the
# no-silent-inaction backstop (item #25, narrowed to first-contact turns).
_ACTING_TOOLS = {"send", "apply", "ask_user", "close", "defer_action"}


class Agent:
    def __init__(self, accounts, name=None, poll_seconds=None, llm=None,
                 memory_path=None, user_messaging_path=None, tz=None,
                 learned_path=None, log_dir=None, base_policy=None, state_dir=None):
        """`accounts` is a list of (connection, policy[, gateway_cfg]) entries —
        `policy` here is each account's own DELTA, layered on `base_policy` (the
        agent-wide policy). `llm` is the cfg's `[llm]` config."""
        if not accounts:
            raise ValueError("an Agent needs at least one connection")
        self.llm_key = (getattr(llm, "anthropic_key", "") or "") if llm else ""
        self.llm_model = (getattr(llm, "model", "") or "") or None
        self.llm_provider = (getattr(llm, "provider", "") or "anthropic") if llm else "anthropic"
        self.llm_base_url = (getattr(llm, "base_url", "") or "") if llm else ""
        self.llm_gateway_model = (getattr(llm, "gateway_model", "") or "") if llm else ""
        self.llm_gateway_provider = ((getattr(llm, "gateway_provider", "") or "")
                                     if llm else "") or self.llm_provider
        self.llm_gateway_key = (getattr(llm, "gateway_key", "") or "") if llm else ""
        self.llm_gateway_base_url = ((getattr(llm, "gateway_base_url", "") or "")
                                     if llm else "") or self.llm_base_url
        self.llm_driver = ((getattr(llm, "driver", "") or "") if llm else "") or "inprocess"
        self.tz = tz or (getattr(llm, "tz", "") if llm else "") or os.environ.get("PARLEY_TZ", "")
        self.owner = (getattr(llm, "owner", "") if llm else "") or ""
        self.base_policy = base_policy or Policy()

        norm = [(a[0], load_policy(a[1]), (a[2] if len(a) > 2 else {}) or {},
                 (a[3] if len(a) > 3 else True)) for a in accounts]
        self.connections = [conn for conn, _, _, _ in norm]
        # Calendar-only accounts (`intake: false`): connected and counted in every
        # busy/free union, but never polled for mail — so a secondary mailbox
        # contributes collisions without the agent negotiating on its behalf.
        self.intake_accounts = [conn for conn, _, _, intake in norm if intake]
        self.calendar_only = [conn for conn, _, _, intake in norm if not intake]
        self.name = name or (self.connections[0].name if len(self.connections) == 1 else "parley")
        self.poll_seconds = poll_seconds or int(os.environ.get("PARLEY_POLL_SECONDS", "15"))
        _stall = norm[0][1].structured or {}
        if _stall.get("stall_nudge_seconds"):
            self._STALL_NUDGE_SECONDS = int(_stall["stall_nudge_seconds"])
        if _stall.get("stall_escalate_seconds"):
            self._STALL_ESCALATE_SECONDS = int(_stall["stall_escalate_seconds"])

        self.log = Log(log_dir, self.name)
        for conn in self.connections:
            try:
                conn.journal = self.log  # backends log through this attribute name
            except Exception:  # noqa: BLE001 — a backend may forbid attribute set
                pass

        self._policy_override = {id(conn): pol for conn, pol, _, _ in norm}
        self._gateway_cfg = {id(conn): gw for conn, _, gw, _ in norm}
        self._gateway_classifier = {id(conn): self._build_gateway_classifier(gw)
                                    for conn, _, gw, _ in norm}

        # Where per-role work queues live. None → in-memory queues (selftest, and any
        # hand-built Agent): the blocked-session path still works, it just does not
        # survive a restart.
        self.state_dir = state_dir
        self._queues = {}
        self.short_term_memory = ShortTermMemory(memory_path)
        self.user_messaging = UserMessaging(user_messaging_path)
        self.accounts_gate = AccountsGate(self)

        self._learned_path = learned_path
        self._learned = _read_learned(learned_path)

        client = self._build_client()
        self.brain = Brain(self, llm_client=client, model=self.llm_model)

        # ref -> {conn, action, message}: what a pending ask needs to resume on
        # answer. NOT persisted (2026-08-31 simplification, flagged in the
        # refactor plan) — a restart loses full message-ground-truth resumption
        # for a still-open ask; UserMessaging's own persisted state (the
        # question + who's asking) survives fine.
        self._escalation_context = {}
        self.current_turn_message = None
        self._last_calendar_sweep = float("-inf")  # see sweep_questions
        # Dynamic Focus Blocks: time kept before a meeting (agent/focus.py).
        self.focus = FocusBlocks(self, os.path.join(state_dir, "focus_blocks.json")
                                 if state_dir else None)

    def _build_gateway_classifier(self, gw_cfg):
        model = str((gw_cfg or {}).get("model", "")).strip() or self.llm_gateway_model
        if not model:
            return None
        key = self.llm_gateway_key or self.llm_key
        if not key and _provider_needs_key(self.llm_gateway_provider):
            return None
        try:
            client = build_llm_client(self.llm_gateway_provider, api_key=key, model=model,
                                      base_url=self.llm_gateway_base_url)
            self._log_model("gateway", self.llm_gateway_provider, model, client)
            return gateway.llm_classifier(client)
        except Exception as err:  # noqa: BLE001 — missing SDK etc. → heuristic only
            self.log.log("error", where="gateway_classifier", error=str(err),
                         human=f"(gateway classifier unavailable: {err})")
            return None

    def _build_client(self):
        key = self.llm_key
        if not key and _provider_needs_key(self.llm_provider):
            return None
        if not self.llm_model:
            raise SystemExit("no model configured: set [llm] model in your cfg "
                             "(e.g. model = \"opus\" for the newest Opus).")
        try:
            client = build_llm_client(self.llm_provider, api_key=key, model=self.llm_model,
                                      base_url=self.llm_base_url)
        except Exception as err:  # noqa: BLE001 — missing SDK / bad provider → no client
            self.log.log("error", where="llm_client", error=str(err),
                         human=f"no LLM client ({err}); the agent can't act until one "
                               f"is configured.")
            return None
        self._log_model("brain", self.llm_provider, self.llm_model, client)
        return client

    def _log_model(self, role, provider, configured, client):
        """Journal which model a role really runs. With an alias the cfg says "opus"
        and the API client looked up a concrete id — the post-mortem needs that id.
        The Claude Code CLI resolves aliases itself, so there `model` is the alias."""
        resolved = getattr(client, "model", "") or configured
        human = None
        if resolved != configured:
            human = f"{role} model: {configured} -> {resolved} (newest of the family)"
        self.log.log("llm_model", role=role, provider=provider or "anthropic",
                     configured=configured, model=resolved, human=human)

    # -- policy -----------------------------------------------------------

    def policy_for(self, conn):
        return effective_policy(self.base_policy, self._policy_override.get(id(conn)))

    def effective_policy_text(self, conn):
        base = self.policy_for(conn).unstructured \
            or "(no specific policy — use good judgement and ask the user when unsure.)"
        if not self._learned:
            return base
        return (f"{base}\n\nStanding guidance you've learned from your user's past answers "
                f"(apply it with the same weight as the policy above):\n{self._learned}")

    def remember_policy(self, guidance):
        line = (guidance or "").strip()
        if not line:
            return {"ok": False, "error": "empty guidance"}
        from datetime import datetime, timezone
        stamped = f"- ({datetime.now(timezone.utc).date().isoformat()}) {line}"
        self._learned = f"{self._learned}\n{stamped}" if self._learned else stamped
        if self._learned_path:
            try:
                os.makedirs(os.path.dirname(self._learned_path) or ".", exist_ok=True)
                with open(self._learned_path, "a", encoding="utf-8") as f:
                    f.write(stamped + "\n")
            except OSError as err:
                return {"ok": True, "persisted": False, "note": f"kept for this session only: {err}"}
        self.log.log("learned_rule", guidance=line, human=f"🧠 learned a new standing rule: {line}")
        return {"ok": True, "persisted": bool(self._learned_path), "guidance": line}

    # -- accessors ------------------------------------------------------------

    @property
    def session_driven(self):
        """True when admitted events go to a background Claude Code session instead of
        the in-process tool loop (cfg: llm: driver: session).

        Only INBOUND mail is routed. A typed instruction stays in-process because its
        caller is waiting on the answer (the CLI prints DONE or the question), and a
        queued turn has nothing to answer with; with provider: claude-code that path
        still runs on the CLI, just synchronously. Timers and human answers likewise.
        """
        return self.llm_driver == "session"

    def enqueue(self, turn, role="brain"):
        """Hand a prepared turn to a role's queue instead of running it here."""
        item = self.work_queue(role).push(turn.kind, self.brain.turn_to_payload(turn))
        self.log.log("queued", role=role, turn_id=item, turn_kind=turn.kind,
                     negotiation_id=turn.negotiation_id, account=turn.conn.name,
                     human=f"📤 queued a {turn.kind} turn for the {role} session")
        return item

    def after_session_turn(self, turn, acted):
        """Run what the in-process loop runs AFTER a turn: the no-silent-inaction
        backstop. A session finishing a first-contact turn without calling a single
        acting tool is the same failure as the brain doing it, and must reach the
        human the same way — otherwise switching driver would quietly drop a guard."""
        if not turn.first_contact or acted or turn.message is None:
            return
        self.log.log("silent_inaction", message_id=turn.message.id,
                     sender=turn.message.sender, driver="session",
                     human=f"⚠ the session took NO action on the message "
                           f"from {turn.message.sender} — asking the user instead.")
        self._escalate_intake(turn.conn, turn.message, turn.payload or {})

    def work_queue(self, role):
        """The durable queue of prepared turns for one role ("brain" / "filter"), made
        on first use. One per (agent, role): two roles never share a position, and two
        agents never share a file."""
        import os
        from parley.agent.work_queue import WorkQueue
        if role not in self._queues:
            path = (os.path.join(self.state_dir, f"queue_{role}.json")
                    if self.state_dir else None)
            self._queues[role] = WorkQueue(path=path, journal=self.log)
        return self._queues[role]

    @property
    def default_conn(self):
        """The account an unaddressed instruction acts on: the first one we actually
        poll. Prefers an intake account because a reply landing in a mailbox nobody
        reads is a negotiation that can never advance. Falls back to the first
        connection when every account is calendar-only."""
        return (self.intake_accounts or self.connections)[0]

    def _conn_for_address(self, address):
        a = (address or "").lower()
        for c in self.connections:
            if c.address().lower() == a:
                return c
        return self.connections[0] if self.connections else None

    @property
    def addresses(self):
        return {c.address().lower() for c in self.connections}

    def check_timezones(self):
        """Startup probe: verify every zone this agent will convert with actually
        resolves — a run that can't convert must say so at boot, not mis-book at
        noon."""
        from zoneinfo import ZoneInfo
        for conn in self.connections:
            try:
                name = self.tz or conn.timezone() or "UTC"
            except Exception:  # noqa: BLE001 — backend tz lookup is best-effort
                continue
            try:
                ZoneInfo(name)
            except Exception as err:  # noqa: BLE001
                self.log.log(
                    "error", where="check_timezones", account=conn.name, zone=name, error=str(err),
                    human=f"🛑 timezone '{name}' does not resolve ({err}) — local times "
                          f"CANNOT be converted. Install the tzdata package.")

    # -- Run loop (external drivers call handle()/instruct()/decide(); this is
    # just the reference poll-mode driver, not something the Agent owns) -------

    def run(self):
        print(f"Parley[{self.name}] connecting...")
        for conn in self.connections:
            conn.connect()
        addrs = ", ".join(sorted(self.addresses))
        brain_desc = self.llm_model or ("(no LLM — can't act)" if self.brain.client is None else "?")
        watched = ", ".join(sorted(c.address() for c in self.intake_accounts)) or "(nothing)"
        cal_only = ", ".join(sorted(c.address() for c in self.calendar_only))
        cal_note = f" Calendar-only (never polled): {cal_only}." if cal_only else ""
        if not self.intake_accounts:
            self.log.log("warning", where="intake",
                         human="every account is  intake: false  -- I will read no mail "
                               "at all and can only act on your direct instructions.")
        self.log.log("server_start",
                     human=f"Connected as {addrs}. Brain: {brain_desc}. "
                           f"Watching {watched} every {self.poll_seconds}s.{cal_note} Ctrl+C to stop.\n",
                     addresses=sorted(self.addresses), model=self.llm_model,
                     poll_seconds=self.poll_seconds, accounts=[c.name for c in self.connections])
        self.check_timezones()
        self._ask_calendar_choices()
        # Questions left over from before this start: close what no longer needs an
        # answer before the tray shows them again.
        self.backfill_questions()
        self.sweep_questions(calendar=True)
        self.focus.reconcile()
        while True:
            time.sleep(self.poll_seconds)
            self._wake()

    def _wake(self):
        """One wake cycle: drain the user channel, poll every account, act on
        each — then whatever periodic upkeep still needs a look."""
        for instruction in self.user_messaging.drain()[0]:
            try:
                self.brain.instruct(instruction.text, self.default_conn)
            except Exception as err:  # noqa: BLE001 — keep the loop alive
                self.log.log("error", where="instruct", error=str(err))
        for conn in self.intake_accounts:
            try:
                messages = conn.list_messages()
            except Exception as err:  # noqa: BLE001 — keep the loop alive
                self.log.log("error", where="poll", account=conn.name, error=str(err),
                             human=f"poll error on {conn.name}, will retry: {err}")
                continue
            for message in messages:
                try:
                    self.handle(message, conn)
                except Exception as err:  # noqa: BLE001
                    self.log.log("error", where="handle", message_id=message.id, error=str(err),
                                 human=f"error handling message {message.id}: {err}")
        self._retry_pending_rsvps()
        self._check_stalled()
        self.sweep_questions()
        self.focus.sweep()

    # -- Ambiguous calendar → ask the user (startup) --------------------------

    def _ask_calendar_choices(self):
        """A backend with >1 calendar and no cfg pin can't decide which to use —
        `list_calendars()` (generalized 2026-08-31, was Yahoo-only) surfaces the
        candidates; ask the user once per such account."""
        for conn in self.connections:
            try:
                cals = conn.list_calendars()
            except Exception:  # noqa: BLE001 — best-effort; no ask if we can't even list
                continue
            if len(cals) <= 1:
                continue
            opts = "\n".join(f"  {i + 1}. {c.get('name') or '(unnamed)'}  [{c['id']}]"
                             for i, c in enumerate(cals))
            question = (f"Your {conn.name} account has {len(cals)} calendars and no pin in "
                        f"the cfg. Which should I use for your meetings?\n\n{opts}\n\n"
                        f"Reply with the name or its number.")
            ref = f"calendar-choice-{conn.name}"
            self.escalate(conn, question, negotiation_id=ref,
                          action={"kind": "choose_calendar", "options": cals},
                          summary=f"Which of your {len(cals)} {conn.name} calendars "
                                  f"should I use?")

    # -- Deferred RSVP retry ---------------------------------------------------

    _RSVP_RETRY_LIMIT = 20

    def _retry_pending_rsvps(self):
        for n in list(self.short_term_memory.pending_rsvps()):
            pr = n.pending_rsvp or {}
            conn = self._conn_for_address(n.account)
            if conn is None:
                continue
            try:
                slot = Slot(from_iso(pr["start"]), from_iso(pr["end"]))
                conn.respond_event("accepted", requester=pr["attendee"], slot=slot,
                                   title=pr.get("title") or "Meeting")
            except InviteNotReady:
                pr["attempts"] = pr.get("attempts", 0) + 1
                if pr["attempts"] >= self._RSVP_RETRY_LIMIT:
                    self.short_term_memory.clear_pending_rsvp(n.id)
                    self.log.log("rsvp_give_up", negotiation_id=n.id, peer=pr["attendee"],
                                 attempts=pr["attempts"],
                                 human=f"🛑 invite from {pr['attendee']} never landed after "
                                       f"{pr['attempts']} tries — asking the user.")
                    self.escalate(conn, f"I couldn't RSVP the invite from {pr['attendee']} for "
                                        f"'{pr.get('title')}' — it never appeared on the calendar. "
                                        f"Keep trying, or reach out to them?",
                                 negotiation_id=n.id, kind="rsvp", who=pr["attendee"],
                                 title=pr.get("title"), start=pr.get("start"),
                                 end=pr.get("end"),
                                 summary="The invite never reached the calendar. "
                                         "Keep trying, or contact them?")
                else:
                    self.short_term_memory.save()
                continue
            except Exception as err:  # noqa: BLE001 — a real backend error; keep the marker
                self.log.log("error", where="retry_rsvp", negotiation_id=n.id, error=str(err))
                continue
            self.short_term_memory.clear_pending_rsvp(n.id)
            self.accounts_gate._settle_booking(conn, n.id, pr["attendee"], slot,
                                               pr.get("title") or "Meeting")
            self.log.log("rsvp_completed", negotiation_id=n.id, peer=pr["attendee"],
                         human=f"✅ deferred RSVP for {pr['attendee']} completed (invite landed).")

    # -- Stalled-negotiation timer ---------------------------------------------

    _STALL_NUDGE_SECONDS = 24 * 3600
    _STALL_ESCALATE_SECONDS = 48 * 3600

    def _check_stalled(self, now=None):
        from datetime import datetime, timezone as _tz
        now = now or datetime.now(_tz.utc)
        for n in list(self.short_term_memory.open()):
            if n.status != "awaiting_peer" or n.stalls >= 2:
                continue
            try:
                age = (now - from_iso(n.updated_at)).total_seconds()
            except Exception:  # noqa: BLE001 — a bad timestamp never breaks the loop
                continue
            conn = self._conn_for_address(n.account)
            if conn is None:
                continue
            who = " and ".join(p.get("addr", "?") for p in n.parties) or "?"
            if age >= self._STALL_ESCALATE_SECONDS:
                self.short_term_memory.upsert(n.id, stalls=2)
                self.short_term_memory.close(n.id, "deadlocked")
                self.log.log("stall_escalate", negotiation_id=n.id, age_seconds=int(age),
                             status="deadlocked",
                             human=f"⏰ '{n.topic}' with {who}: still no reply — deadlocked; "
                                   f"asking the user.")
                self.escalate(conn, f"'{n.topic}' with {who} has had no reply for a while. "
                                    f"Nudge again, propose another time, or drop it?",
                             negotiation_id=n.id, kind="stalled",
                             summary="No reply yet. Nudge, propose another time, or drop it?")
            elif age >= self._STALL_NUDGE_SECONDS and n.stalls < 1:
                self.short_term_memory.upsert(n.id, stalls=1)
                self.log.log("stall_nudge", negotiation_id=n.id, age_seconds=int(age),
                             human=f"⏰ '{n.topic}' with {who}: no reply yet — waking the brain to nudge.")
                self.brain.on_timer(conn, (
                    f"Infra note: your message to {who} about '{n.topic}' (ref {n.id}) has had "
                    f"no reply for a while. Decide per your policy: send a brief nudge to "
                    f"whoever hasn't answered, keep waiting, or tell your user."))

    # -- Dispatch ---------------------------------------------------------
    # Order matches docs/iterations/refactor-plan.md "Dispatch order" exactly —
    # several guards below exist BECAUSE an earlier version had them in the
    # wrong order; keep the sequence, not just the checks.

    def handle(self, message, conn):
        # 1. Journal first — before any routing decision, no exceptions.
        self.log.log("message_received", message_id=message.id, sender=message.sender,
                     subject=message.subject, body=message.body, invite=message.invite,
                     account=conn.name)

        payload = wire.parse(message)
        neg_id = payload.get("negotiation_id")
        if payload:
            self.log.log("payload", message_id=message.id, **payload)

        # 6. rsvp_reply — BEFORE the terminal guard; responses arrive precisely
        #    for settled bookings.
        if payload.get("type") == "rsvp_reply":
            self._on_rsvp_reply(conn, message, payload)
            return

        # 6b. The organizer cancelled. Deterministic: close the negotiation if we
        #     track it, drop the notice if we don't. The provider has already taken
        #     the event off the calendar, so there is nothing to undo — and nothing
        #     for the brain to decide (2026-09-06: this used to cost a model turn and
        #     then trip the no-silent-inaction backstop, toasting the user the raw
        #     cancellation mail every time any meeting was cancelled).
        if payload.get("type") == "cancellation":
            self._on_cancellation(conn, message, payload)
            return

        # 7. A reopenable proposal/moved-invite for a TERMINAL negotiation is a
        #    real reschedule, not a settled echo.
        if neg_id and self.short_term_memory.is_terminal(neg_id) \
                and self._reopenable(neg_id, payload):
            self._on_reschedule_proposal(conn, message, payload, neg_id)
            return

        # 8. A real invite matching a KNOWN negotiation (booked OR still
        #    awaiting_peer — widened 2026-08-31, replacing `request_confirmed`
        #    as its own message type) → infra auto-RSVPs, zero model turns,
        #    BEFORE the generic terminal drop (else the seat's first arrival
        #    would be eaten by the drop guard meant for resends).
        if payload.get("via_invite") and self._rsvp_settled(conn, message, payload):
            return

        # 9. Generic terminal-id drop (resend/echo).
        if neg_id and self.short_term_memory.is_terminal(neg_id):
            self.log.log("drop", reason="settled", negotiation_id=neg_id, message_id=message.id,
                         human=f"✅ message for settled negotiation {neg_id} — ignored.")
            return

        # 11. Permission — before relevance/gateway or the brain get a say.
        verdict = permission(Action(HANDLE_INBOUND, target=message.sender,
                                    title=message.subject, payload=payload),
                            self.policy_for(conn).structured)
        if verdict == DENY:
            self.log.log("drop", reason="trust_ignore", message_id=message.id,
                         sender=message.sender, subject=message.subject,
                         human=f"⛔ mail from {message.sender} (trust: ignore) — dropped")
            return
        if verdict == ASK:
            self._escalate_intake(conn, message, payload)
            return

        # 12. Relevance: a trusted (auto) sender skips it.
        is_trusted = trusted(self.policy_for(conn).structured, message.sender)
        gw_verdict = gateway.ADMIT if is_trusted else gateway.route(
            message, payload, cfg=self._gateway_cfg.get(id(conn)),
            classifier=self._gateway_classifier.get(id(conn)), journal=self.log)
        self.log.log("gateway_decision", message_id=message.id, verdict=gw_verdict,
                     trusted=is_trusted, sender=message.sender, subject=message.subject,
                     type=payload.get("type"))
        if gw_verdict == gateway.IGNORE:
            self.log.log("drop", reason="gateway_ignore", message_id=message.id,
                         sender=message.sender, subject=message.subject)
            return

        # 13. No visible ref at all → correlate by thread, then (peer, topic);
        #     ambiguous → ask the user, naming the candidates — never guess.
        if not neg_id:
            corr = self._correlate_reply(message, conn)
            if corr is _AMBIGUOUS:
                cands = self._reply_candidates(message)
                self.log.log("ambiguous_reply", message_id=message.id, sender=message.sender,
                             candidates=[n.id for n in cands],
                             human=f"❓ a reply from {message.sender} fits more than one open "
                                   f"meeting — asking the user which.")
                self._escalate_ambiguous(conn, message, cands)
                return
            if corr:
                neg_id = corr
                payload = {"negotiation_id": corr}
                self.log.log("correlated_reply", message_id=message.id, negotiation_id=corr,
                             sender=message.sender)

        was_new_contact = neg_id is None or self.short_term_memory.get(neg_id) is None

        # Session driver: everything above this line still happens HERE — journal,
        # rsvp/cancellation/terminal guards, trust, the gateway, correlation — so the
        # session only ever sees events that survived the deterministic intake. What
        # changes is who reasons about the survivor.
        if self.session_driven:
            turn = self.brain.turn_for_message(message, conn, payload)
            turn.first_contact = was_new_contact
            self.enqueue(turn)
            if neg_id and message.message_id:
                self.short_term_memory.link_thread(neg_id, message.message_id)
            return

        result = self.brain.handle(message, conn, payload)
        if neg_id and message.message_id:
            self.short_term_memory.link_thread(neg_id, message.message_id)

        # 25. No-silent-inaction backstop, narrowed to first contact (2026-08-31
        #     — the removed `type == "meeting_request"` check had no
        #     replacement field; a brand-new negotiation with zero acting tool
        #     calls is the exact shape of the original bug either way).
        if was_new_contact and not self._turn_acted(result):
            self.log.log("silent_inaction", message_id=message.id, sender=message.sender,
                         human=f"⚠️ the brain took NO action on the message from "
                               f"{message.sender} — asking the user instead.")
            self._escalate_intake(conn, message, payload)

    def _on_rsvp_reply(self, conn, message, payload):
        mem = self.short_term_memory
        responder = (payload.get("responder") or message.sender or "").lower()
        if responder in self.addresses:
            self.log.log("drop", reason="own_rsvp_echo", message_id=message.id)
            return
        response = payload.get("response", "accepted")
        n = mem.find_overlap(responder, payload.get("start"), payload.get("end"), status="booked")
        if n is None and payload.get("title"):
            n = mem.find_by_peer_topic(responder, payload.get("title"))
        self.log.log("rsvp_result", response=response, responder=responder,
                     title=payload.get("title"), negotiation_id=n.id if n else None,
                     matched=bool(n),
                     human=f"📩 {responder} {response} '{payload.get('title')}'"
                           + ("" if n else " (no matching booking found)"))
        if n is not None:
            mem.record(n.id, f"{responder} {response} the invite")
        if response != "declined":
            return
        if n is not None:
            n = mem.reopen(n.id, "the peer declined")
            note = (f"Infra note: {responder} DECLINED your booked meeting "
                    f"'{payload.get('title')}' (negotiation {n.id}, reopened). Decide per "
                    f"your policy: propose another time, or tell your user.")
        else:
            note = (f"Infra note: {responder} declined an invite ('{payload.get('title')}') "
                    f"that matches no booking on record. Investigate and inform your user "
                    f"if it matters.")
        self.brain.handle(message, conn, payload=payload, note=note)

    def _on_cancellation(self, conn, message, payload):
        """The organizer called a meeting off. Infra's job, not the brain's: the
        provider already removed the event, so the only state left to settle is ours.

        Known negotiation → close it `cancelled` (a tombstone, so a resent notice is
        dropped by the terminal guard). Unknown → journal and drop: a cancellation for
        something we never tracked genuinely needs no action, and escalating it was the
        toast-spam bug. Either way ZERO model turns — a cancellation has no decision in
        it. Our OWN cancellation echoed back is dropped first.
        """
        mem = self.short_term_memory
        organizer = (payload.get("organizer") or message.sender or "").lower()
        title = payload.get("title")
        if organizer in self.addresses:
            self.log.log("drop", reason="own_cancellation_echo", message_id=message.id)
            return
        n = mem.find_by_peer_topic(organizer, title) if title else None
        if n is None:
            self.log.log("drop", reason="cancellation_unknown", message_id=message.id,
                         organizer=organizer, title=title,
                         human=f"🗑️ {organizer} cancelled '{title}' — no negotiation on "
                               f"record, nothing to do.")
            self._focus_after_cancellation()
            return
        mem.record(n.id, f"{organizer} cancelled the meeting")
        mem.close(n.id, "cancelled")
        self._focus_after_cancellation()
        self.log.log("cancellation", negotiation_id=n.id, organizer=organizer, title=title,
                     account=conn.address(),
                     human=f"🗑️ {organizer} cancelled '{title}' — negotiation {n.id} closed.")

    def _focus_after_cancellation(self):
        """A cancelled meeting may be some focus time's Z, or may free room for one:
        sweep now rather than in up to five minutes."""
        try:
            self.focus.sweep(force=True)
        except Exception as err:  # noqa: BLE001 — never let this break intake
            self.log.log("error", where="focus_after_cancellation", error=str(err))

    def _reopenable(self, neg_id, payload):
        """A moved real invite (same uid, different slot) is a genuine reschedule.
        For anything else referencing a terminal id (no typed 'proposal' field
        exists anymore — wire.py stopped asserting one), always treat it as
        reschedulable rather than a settled echo: the write guards in
        AccountsGate (idempotent create, the conflict guard) backstop a
        too-eager reopen — worst case is one extra brain turn, never a
        double-write."""
        if payload.get("via_invite"):
            uid = payload.get("uid")
            n = self.short_term_memory.find_by_uid(uid) if uid else None
            if n is None:
                return False
            try:
                return not (from_iso(n.slot["start"]) == from_iso(payload.get("start"))
                            and from_iso(n.slot["end"]) == from_iso(payload.get("end")))
            except Exception:  # noqa: BLE001 — a malformed slot never blocks a reschedule
                return True
        return True

    def _on_reschedule_proposal(self, conn, message, payload, neg_id):
        n = self.short_term_memory.reopen(neg_id, f"{message.sender} proposed a reschedule")
        reason = "invite_moved" if payload.get("via_invite") else "reschedule_proposal"
        self.log.log("reopen", negotiation_id=n.id, reopened_from=neg_id, reason=reason,
                     message_id=message.id,
                     human=f"🔄 {message.sender} proposed a new time for settled {neg_id} — "
                           f"reopened as {n.id} to renegotiate.")
        if payload.get("via_invite"):
            note = (f"Infra note: {message.sender} (the organizer) MOVED your already-accepted "
                    f"meeting '{payload.get('title')}' to the new slot in this invite (ref "
                    f"{n.id}, reopened). If it works, RSVP your seat: apply(rsvp) with this ref "
                    f"and the NEW times.")
        else:
            note = (f"Infra note: {message.sender} proposed a new time for your already-booked "
                    f"meeting '{payload.get('title')}' (ref {n.id}, reopened). If it works, "
                    f"MOVE the existing event to it (apply change=move) and settle; then carry "
                    f"out any follow-up you deferred.")
        self.brain.handle(message, conn, payload=payload, note=note)

    @staticmethod
    def _turn_acted(result):
        for entry in (result or {}).get("transcript") or []:
            if entry.get("role") != "assistant":
                continue
            for call in entry.get("tool_calls") or []:
                if call.name in _ACTING_TOOLS:
                    return True
        return False

    def _rsvp_settled(self, conn, message, payload):
        """A real invite matching a negotiation ALREADY on record with this peer
        at these exact instants — booked OR still awaiting_peer (widened
        2026-08-31, replacing the `request_confirmed` message type: the invite
        itself is enough of a confirmation) — RSVP-accepts it and records the
        booked event's uid for future reschedule recognition. True when
        handled — the brain never sees it."""
        mem = self.short_term_memory
        organizer = (payload.get("from") or message.sender or "")
        settled = mem.find_overlap(organizer, payload.get("start"), payload.get("end"),
                                   status="booked")
        awaiting = None if settled else mem.find_overlap(
            organizer, payload.get("start"), payload.get("end"), status="awaiting_peer")
        n = settled or awaiting
        if n is None:
            return False
        if n.calendar_uid and payload.get("uid") and n.calendar_uid == payload.get("uid"):
            # The seat was already RSVP'd on a previous arrival — a resend.
            self.log.log("drop", reason="settled", negotiation_id=n.id, message_id=message.id,
                         human=f"✅ resent invite for settled {n.id} — ignored.")
            return True
        try:
            slot = Slot(from_iso(payload["start"]), from_iso(payload["end"]))
            conn.respond_event("accepted", requester=organizer, slot=slot,
                               title=payload.get("title") or "Meeting")
        except Exception as err:  # noqa: BLE001 — RSVP is best-effort; the booking stands
            self.log.log("error", where="rsvp_settled", error=str(err), negotiation_id=n.id,
                         human=f"(couldn't RSVP the settled invite from {organizer}: {err})")
        if n.status != "booked":
            mem.close(n.id, "booked")
        if payload.get("uid"):
            mem.set_calendar_uid(n.id, payload["uid"])
        self.log.log("auto_rsvp", negotiation_id=n.id, organizer=organizer,
                     start=payload.get("start"), end=payload.get("end"),
                     human=f"✅ invite from {organizer} matches negotiation {n.id} — "
                           f"RSVP'd by infra, not re-asked.")
        return True

    @staticmethod
    def _subject_topic(subject):
        m = _SUBJECT_TOPIC_RE.match(subject or "")
        topic = (m.group(1).strip() if m else "") or (subject or "").strip()
        return topic

    @staticmethod
    def _looks_like_reply(message):
        if message.thread_refs():
            return True
        return bool(re.match(r"^\s*(re|fwd|fw|aw|sv|rv)\s*:", message.subject or "", re.I))

    def _correlate_reply(self, message, conn):
        threaded = self.short_term_memory.neg_for_thread(message.thread_refs())
        if threaded is not None:
            return threaded.id
        cands = self._reply_candidates(message)
        if len(cands) == 1:
            return cands[0].id
        if len(cands) > 1:
            return _AMBIGUOUS
        return None

    def _reply_candidates(self, message):
        topic = self._subject_topic(message.subject)
        cands = self.short_term_memory.open_by_peer_topic(message.sender, topic)
        if not cands and topic and self._looks_like_reply(message):
            cands = self.short_term_memory.open_by_peer_topic(message.sender, None)
        return cands

    def _escalate_intake(self, conn, message, payload):
        body = html_to_text(message.body)
        question = (f"Engage with this email?\nFrom: {message.sender}\n"
                    f"Subject: {message.subject or '(no subject)'}\n\n"
                    f"{body[:500]}{'…' if len(body) > 500 else ''}")
        name = message.sender_name or message.sender
        self.escalate(conn, question, negotiation_id=payload.get("negotiation_id"),
                      message=message, kind=None if message.invite else "email",
                      summary=f"New mail from {name}: should I engage with it?")

    def _escalate_ambiguous(self, conn, message, cands):
        body = html_to_text(message.body)
        names = "; ".join(f"'{n.topic or 'Meeting'}'{n._slot_human()}" for n in cands) \
            or "more than one open meeting"
        question = (f"A reply from {message.sender} could belong to {len(cands)} of your "
                    f"open meetings — which did you mean?\nCandidates: {names}\n"
                    f"Subject: {message.subject or '(no subject)'}\n\n"
                    f"{body[:500]}{'…' if len(body) > 500 else ''}")
        self.escalate(conn, question, message=message, kind="ambiguous",
                      summary=f"This reply fits {len(cands)} of your meetings. Which one?")

    # -- client-server instruction ---------------------------------------

    def instruct(self, text, conn=None):
        """Act on a free-text instruction from the user immediately (used by the
        tray's synchronous instruct path; the poll loop's `_wake` also drains
        queued instructions the same way)."""
        return self.brain.instruct(text, conn or self.default_conn)

    # == Human-gated decisions: /pending + /decide =============================

    def escalate(self, conn, question, negotiation_id=None, action=None, message=None,
                 **meta):
        """Park a question for the human. Idempotent per negotiation_id (one ask
        per thing) — a repeat ask reuses the pending item via `UserMessaging`.
        `meta` (kind, who, title, start, end, summary) is what the tray shows in
        place of the full text; whatever the caller leaves out is filled in from
        the message and the negotiation (`_question_meta`)."""
        ref = negotiation_id or wire.new_ref()
        meta = self._question_meta(ref, question, message, action, meta)
        pq, reused = self.user_messaging.ask(ref, question, **meta)
        if reused:
            self.log.log("escalate", ref=ref, question=question, reused=True)
            return ref
        self._escalation_context[ref] = {"conn": conn, "action": action, "message": message}
        self.log.log("escalate", ref=ref, question=question, negotiation_id=negotiation_id,
                     reused=False, human=f"❓ asking the user (ref:{ref}): {question}")
        if negotiation_id and self.short_term_memory.get(negotiation_id) is not None:
            self.short_term_memory.upsert(negotiation_id, status="awaiting_human")
            self.short_term_memory.record(negotiation_id, f"asked the user: {question[:80]}")
        return ref

    def _question_meta(self, ref, question, message, action, meta):
        """The display fields of a question: who, what, when, one-line summary.

        Caller first; then the message the question is about (the turn's own message
        when the brain asks mid-turn); then the negotiation. The summary falls back
        to the question's first sentence, cut to size — a model that ignored the
        length rule still produces a card that fits."""
        meta = {k: v for k, v in (meta or {}).items() if v}
        if message is None and self.current_turn_message is not None:
            # The turn's message only describes THIS question if it is about the same
            # meeting — a brain may ask about another one mid-turn.
            turn_ref = wire.parse(self.current_turn_message).get("negotiation_id")
            if turn_ref in (None, ref):
                message = self.current_turn_message
        neg = self.short_term_memory.get(ref)
        inv = getattr(message, "invite", None) or {}
        if inv and not inv.get("response"):
            meta.setdefault("kind", "invite")
            meta.setdefault("title", inv.get("title"))
            meta.setdefault("start", inv.get("start"))
            meta.setdefault("end", inv.get("end"))
        elif str(ref).startswith("mtg-i"):
            meta.setdefault("kind", "invite")
        if message is not None:
            meta.setdefault("who", getattr(message, "sender_name", None) or message.sender)
            meta.setdefault("title", message.subject)
        if action and action.get("kind") == "choose_calendar":
            meta.setdefault("kind", "calendar")
        elif action and action.get("kind"):
            meta.setdefault("kind", "permission")
        if neg is not None:
            meta.setdefault("title", neg.topic)
            meta.setdefault("who", ", ".join(p.get("addr") for p in neg.parties
                                             if p.get("addr")))
            if neg.slot:
                meta.setdefault("start", neg.slot.get("start"))
                meta.setdefault("end", neg.slot.get("end"))
            meta["neg_status"] = neg.status
        meta.setdefault("kind", "question")
        meta["summary"] = one_line(meta.get("summary") or question)
        return {k: v for k, v in meta.items() if v}

    def pending_decisions(self):
        """What `/v1/pending` serves. `question` is the full text, as ever; the rest
        is what a client shows first (the tray's card, the skill's grouped list)."""
        return [{"negotiation_id": pq.ref, "question": pq.question,
                 "kind": pq.kind or "question", "summary": pq.summary or one_line(pq.question),
                 "from": pq.who, "title": pq.title, "start": pq.start, "end": pq.end,
                 "asked_at": pq.created_at}
                for pq in self.user_messaging.list_pending()]

    def dismiss(self, ref, reason=None):
        """Close a question without answering it — the user dealt with it elsewhere,
        or it no longer matters. Silent on purpose: the brain is NOT woken, nothing is
        sent. A negotiation frozen on this question goes back to `open`, so the next
        message about it is handled normally instead of being parked forever."""
        reason = (reason or "").strip() or "dismissed by the user"
        return self._close_question(ref, reason)

    def _close_question(self, ref, reason):
        pq = self.user_messaging.pending.get(ref)
        if not self.user_messaging.close(ref, reason):
            return {"ok": False, "ignored": True,
                    "message": f"'{ref}' is not an open question."}
        neg = self.short_term_memory.get(ref)
        if neg is not None and neg.status == "awaiting_human":
            self.short_term_memory.upsert(ref, status="open")
        if neg is not None:
            self.short_term_memory.record(ref, f"question closed: {reason[:60]}")
        self._escalation_context.pop(ref, None)
        self.log.log("question_closed", ref=ref, reason=reason,
                     question=pq.question if pq else "",
                     human=f"🧹 question {ref} closed: {reason}")
        return {"ok": True, "decision": "closed", "text": f"Closed: {reason}."}

    # -- closing questions that no longer need an answer -----------------------

    #: How often the calendar is read to see whether the user answered an invite
    #: there themselves. The other two rules cost nothing and run every wake.
    _CALENDAR_SWEEP_SECONDS = 600

    def sweep_questions(self, now=None, calendar=None):
        """Close every open question that no longer needs an answer:

          * its negotiation ENDED after it was asked (booked, declined, cancelled,
            superseded) — `deadlocked` is excluded, it is the very thing a stall
            question asks about;
          * the meeting it is about is OVER;
          * it is an invite, and the user already answered that invite in their own
            calendar (accepted, tentative or declined).

        Silent, like `dismiss`. `calendar=None` reads the calendar only every
        `_CALENDAR_SWEEP_SECONDS`; True/False forces it. Returns the closed refs."""
        from datetime import datetime, timezone as _tz
        now = now or datetime.now(_tz.utc)
        if calendar is None:
            calendar = (time.monotonic() - self._last_calendar_sweep
                        >= self._CALENDAR_SWEEP_SECONDS)
        if calendar:
            self._last_calendar_sweep = time.monotonic()
        closed = []
        for pq in list(self.user_messaging.list_pending()):
            try:
                reason = self._stale_reason(pq, now, calendar)
            except Exception as err:  # noqa: BLE001 — one bad question never stops the sweep
                self.log.log("error", where="sweep_questions", ref=pq.ref, error=str(err))
                continue
            if reason and self._close_question(pq.ref, reason).get("ok"):
                closed.append(pq.ref)
        return closed

    def backfill_questions(self):
        """Give questions saved before 2026-10-18 the invite details they lack.

        Those were stored as bare text, so the sweep cannot tell when their meeting
        is or find it in the calendar. The journal can: the invite's `payload` record
        holds its title and slot under the same ref. Read once, at start; a question
        whose ref is not in the journal simply stays as it was. Returns the refs filled."""
        filled = [pq.ref for pq in self.user_messaging.list_pending()
                  if not pq.kind and self._backfill_from_text(pq)]
        bare = {pq.ref: pq for pq in self.user_messaging.list_pending() if not pq.start}
        log_dir = getattr(self.log, "dir", None)
        if not bare or not log_dir or not os.path.isdir(log_dir):
            if filled:
                self.user_messaging.save()
            return filled
        for name in sorted(os.listdir(self.log.dir)):
            if not (name.startswith("journal-") and name.endswith(".jsonl")):
                continue
            try:
                with open(os.path.join(self.log.dir, name), encoding="utf-8") as f:
                    lines = [line for line in f if '"payload"' in line
                             and '"meeting_request"' in line]
            except OSError:
                continue
            for line in lines:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                pq = bare.get(rec.get("negotiation_id"))
                if pq is None or not rec.get("start"):
                    continue
                pq.kind = "invite" if pq.kind in (None, "question") else pq.kind
                pq.title = pq.title or (rec.get("title") or "").strip() or None
                pq.start, pq.end = rec.get("start"), rec.get("end")
                if pq.ref not in filled:
                    filled.append(pq.ref)
                del bare[pq.ref]
        if filled:
            self.user_messaging.save()
            self.log.log("questions_backfilled", refs=filled,
                         human=f"🧹 {len(filled)} older question(s) given their invite "
                               f"details from the journal.")
        return filled

    def _backfill_from_text(self, pq):
        """An older question's display fields, read back from text Parley itself wrote:
        the intake and stall asks have fixed shapes; the negotiation, if any, has the
        title and who. A free-form brain question gets only what the negotiation has."""
        q = pq.question or ""
        if q.startswith("Engage with this email?"):
            sender = re.search(r"^From:\s*(.+)$", q, re.M)
            subject = re.search(r"^Subject:\s*(.+)$", q, re.M)
            pq.kind = "email"
            pq.who = pq.who or (sender.group(1).strip() if sender else None)
            pq.title = pq.title or (subject.group(1).strip() if subject else None)
            pq.summary = "New mail: should I engage with it?"
        elif "has had no reply for a while" in q:
            pq.kind = "stalled"
            pq.summary = "No reply yet. Nudge, propose another time, or drop it?"
        elif q.startswith("A reply from ") and "which did you mean" in q:
            pq.kind = "ambiguous"
        else:
            pq.kind = "question"
        neg = self.short_term_memory.get(pq.ref)
        if neg is not None:
            pq.title = pq.title or neg.topic or None
            pq.who = pq.who or ", ".join(p.get("addr") for p in neg.parties
                                         if p.get("addr")) or None
            if neg.slot and not pq.start:
                pq.start, pq.end = neg.slot.get("start"), neg.slot.get("end")
            # neg_status stays unknown on purpose: today's status is not the one it
            # was asked under, and recording it would stop the sweep closing it.
        pq.summary = one_line(pq.summary or q)
        return True

    def _stale_reason(self, pq, now, calendar):
        neg = self.short_term_memory.get(pq.ref)
        ended = ("booked", "declined", "cancelled", "superseded")
        if neg is not None and neg.status in ended and neg.status != pq.neg_status:
            return f"the meeting is {neg.status}"
        start, end = pq.start, pq.end
        if neg is not None and neg.slot:
            start, end = neg.slot.get("start") or start, neg.slot.get("end") or end
        over = end or start
        if over and from_iso(over) < now:
            return "the meeting date has passed"
        if calendar and pq.kind == "invite" and start and end:
            answered = self._calendar_response(pq, start, end)
            if answered:
                return f"you already answered it in your calendar ({answered})"
        return None

    def _calendar_response(self, pq, start, end):
        """The user's own answer to the invite a question is about, read from the
        calendar: "accepted" / "tentative" / "declined", or None if unanswered or
        unknown. Matches the event by its exact slot and, when known, its title."""
        slot = Slot(from_iso(start), from_iso(end))
        title = (pq.title or "").strip().lower()
        for conn in self.connections:
            try:
                events = conn.list_events(slot)
            except Exception:  # noqa: BLE001 — a calendar we can't read just says nothing
                continue
            for e in events:
                if e.start != slot.start or e.end != slot.end:
                    continue
                name = (e.title or "").lower()
                if title and title not in name and name not in title:
                    continue
                if e.response in ("accepted", "tentative", "declined"):
                    return e.response
        return None

    def decide(self, ref, choice):
        """Resolve an escalation with the human's `choice` — accept / deny / or
        any free-text guidance. First-answer-wins via `UserMessaging.answer`'s
        atomic resolve; the brain re-plans on the answer."""
        pq = self.user_messaging.pending.get(ref)
        question_text = pq.question if pq else ""
        if not self.user_messaging.answer(ref, str(choice)):
            self.log.log("human_decision", ref=ref, choice=str(choice), duplicate=True)
            return {"ok": False, "ignored": True,
                    "message": f"'{ref}' was already answered — ignoring the duplicate."}
        ctx = self._escalation_context.pop(ref, None) or {}
        conn = ctx.get("conn") or (self.connections[0] if self.connections else None)
        action = ctx.get("action") or {}
        self.log.log("human_decision", ref=ref, choice=str(choice), duplicate=False,
                     verb=action.get("kind"), verdict=str(choice), question=question_text,
                     human=f"📥 human answered {ref}: {choice!r}")

        if action.get("kind") == "choose_calendar":
            # Ambiguous-calendar choice from `_ask_calendar_choices` — no brain turn.
            opts = action.get("options") or []
            chosen = _match_calendar_choice(opts, str(choice))
            if chosen:
                # Regression fix (2026-09-05): this used to just SAY "Calendar set" —
                # `conn.select_calendar` was never called, so the connector stayed
                # stuck in its ambiguous/degraded state for the rest of the process
                # (Yahoo: mail-only, no own-calendar writes) no matter what the user
                # answered. `conn` here is `ctx.get("conn")`, resolved above.
                conn.select_calendar(chosen["id"])
                return {"ok": True, "decision": "calendar_set",
                        "text": f"Calendar set to {chosen['name']}."}
            return {"ok": False, "decision": "unmatched",
                    "text": "That didn't match any calendar — please reply with one of the "
                            "listed names or its number."}

        if action.get("kind") == "focus_short":
            # "No room for your focus time": drop it or keep it — infra, no brain turn.
            return self.focus.decide(action.get("dfb_id"), choice)

        if action.get("kind") and _affirmative(choice):
            self.user_messaging.grant(conn.address(), action["kind"], action.get("target") or "")

        neg = self.short_term_memory.get(ref) if ref else None
        if neg is not None and neg.status == "awaiting_human":
            self.short_term_memory.upsert(ref, status="open")
            self.short_term_memory.record(ref, f"user answered: {str(choice)[:60]}")
            # A human has just looked at this negotiation and said what to do, so the
            # per-party send budget starts again. The cap exists to guarantee a human
            # checkpoint, not to cap the total volume of a meeting the user is actively
            # steering — and without this, answering "yes, keep going" would trip the
            # very same cap on the next send and ask again.
            self.short_term_memory.reset_sent(ref)
        if neg is not None and neg.status == "deadlocked":
            reopened = self.short_term_memory.reopen(ref, "the user answered the deadlock ask")
            self.log.log("reopen", negotiation_id=reopened.id, reopened_from=ref,
                         reason="deadlock_answer")

        if ctx.get("message") is not None:
            result = self.brain.handle(
                ctx["message"], conn,
                note=(f"You asked your user a question about the email below.\n"
                      f"Question: \"{question_text}\"\nTheir answer: \"{choice}\"\n"
                      f"Act on their answer, using the email's EXACT data (times, "
                      f"negotiation ref) — not the question's prose."))
        else:
            result = self.brain.user_answered(conn, question_text, str(choice), ref)
        return {"ok": True, "decision": "handled", "text": (result or {}).get("text", "")}


# Sentinel: a prose reply that matches MORE THAN ONE open negotiation with the
# same peer+topic — genuinely ambiguous. Never guess; the human is asked which.
_AMBIGUOUS = object()


def _match_calendar_choice(options, answer):
    a = (answer or "").strip().lower()
    if not (options and a):
        return None
    if a.isdigit():
        i = int(a) - 1
        if 0 <= i < len(options):
            return options[i]
    for c in options:
        name = (c.get("name") or "").lower()
        if a == name or a in name or a in c.get("id", "").lower():
            return c
    return None
