"""Permission — the one authority model over the agent's actions (2026-08-31
refactor: was `Guardrail`, a per-account class; now a pure function, since the
Agent no longer needs to construct and hold an instance per account — the trust
table is just data, passed in at the call site, same as `gateway.route`).

Given an action the agent is about to take — **including engaging with an inbound
message** — decide **ALLOW | ASK | DENY**:

  * ALLOW — run it now (autonomous).
  * ASK   — park it for the human (`UserMessaging.ask` — the answer comes back
            through the tray).
  * DENY  — refuse outright (an inbound DENY is a logged drop, never silent).

The brain proposes; `permission()` decides whether it may run on its own or must
ask first. It is the deterministic *authority* — it can force a human in the
loop regardless of what the brain decided, at **both** boundaries (inbound
`handle_inbound`, outbound every `AccountsGate` call).

**An empty trust table ALLOWs everything** — fully-autonomous behaviour, the
default (2026-08-31: left empty on purpose, see docs/iterations/refactor-plan.md
"Agent"). Refinement is data, no code change: one trust table per policy
(`Policy.structured["trust"]`, itself the global+per-account merge —
see `policy.effective_policy`):

    trust = { "boss@co.com": "auto",              # always allowed
              "noisy@list.com": "ignore",         # always denied/dropped
              "new@person.com": "ask",            # human decides, per thing
              "b@co.com": { "default": "ask",     # per-verb overrides
                            "create_event": "auto" } }

`auto → ALLOW`, `ask → ASK`, `ignore → DENY`; a dict value picks by the action's
`kind` (falling back to its `"default"`). The table **bounds authority**; the
policy prose keeps nuance — and the table always wins (a trusted-`ignore`
sender is dropped no matter what the prose says).

Back-compat shim: the retired `accept_requests_from` list is folded in — listed
senders become `auto`, and while the list is present an *unlisted* sender's
incoming meeting request gets ASK at intake (the old semantics, moved to the
front door).
"""

from dataclasses import dataclass

ALLOW = "allow"
ASK = "ask"
DENY = "deny"

# trust-table words → verdicts (accept a few synonyms).
_TRUST_VERDICTS = {
    "auto": ALLOW, "allow": ALLOW,
    "ask": ASK,
    "ignore": DENY, "deny": DENY,
}

# The inbound boundary's action kind: engaging with a received message.
HANDLE_INBOUND = "handle_inbound"


@dataclass
class Action:
    """What the agent is about to do — the unit `permission()` rules on.

    kind    — a verb: "handle_inbound", "apply", "send", ... (the AccountsGate
              tool name, or a legacy verb name for back-compat).
    target  — the other party (sender / requester / recipient); compared lowercased.
    title   — human label, for the escalation card.
    payload — the underlying wire payload, carried through to the human gate.
    """

    kind: str
    target: str = None
    title: str = None
    payload: dict = None


def permission(action, structured):
    """Return ALLOW | ASK | DENY for `action`, given `structured` — a policy's
    whole `structured` dict (holds `trust` and, for the shim, possibly
    `accept_requests_from`). Unknown principals/kinds are open (ALLOW) — this
    only narrows where the table (or the shim) says so."""
    table, accept_requests_from = _parse_trust(structured)
    principal = (action.target or "").lower()
    entry = table.get(principal)
    if entry is not None:
        word = entry if isinstance(entry, str) else \
            entry.get(action.kind, entry.get("default"))
        if word in _TRUST_VERDICTS:
            return _TRUST_VERDICTS[word]
    # Shim (old semantics, now at the front door): with accept_requests_from
    # configured, an UNLISTED sender's incoming meeting request → ask the human.
    if (action.kind == HANDLE_INBOUND and accept_requests_from
            and principal not in accept_requests_from
            and (action.payload or {}).get("type") == "meeting_request"):
        return ASK
    return ALLOW


def trusted(structured, principal):
    """True when `principal` has an explicit blanket `auto` entry — a trusted
    sender whose plain mail skips the gateway's relevance check."""
    table, _ = _parse_trust(structured)
    return table.get((principal or "").lower()) == "auto"


def _parse_trust(structured):
    """A policy's `structured` dict → ({lowercased principal:
    verdict-word-or-dict}, {accept_requests_from set}). Unknown verdict words
    are dropped loudly-by-omission (treated as no entry) rather than guessed."""
    structured = structured or {}
    table_in = structured.get("trust") or {}
    shim = addr_set(structured.get("accept_requests_from"))
    table = {}
    for principal, value in (table_in or {}).items():
        key = str(principal).strip().lower()
        if not key:
            continue
        if isinstance(value, dict):
            sub = {str(k).strip().lower(): str(v).strip().lower()
                   for k, v in value.items()}
            table[key] = {k: v for k, v in sub.items() if v in _TRUST_VERDICTS}
        else:
            word = str(value).strip().lower()
            if word in _TRUST_VERDICTS:
                table[key] = word
    for addr in shim:
        table.setdefault(addr, "auto")
    return table, shim


def addr_set(value):
    """A YAML/JSON array (list) or a comma-separated string → a lowercased set."""
    if not value:
        return set()
    items = value if isinstance(value, (list, tuple, set)) else str(value).split(",")
    return {str(a).strip().lower() for a in items if str(a).strip()}
