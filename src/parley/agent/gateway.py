"""Gateway — admits an inbound message, or drops it (two stages).

The Agent hands every peer-to-peer message here (after the permission hook's trust
check — the gateway rules *relevance*, the trust table rules *authority*) and
gets one of:

  * ADMIT  — let it through to the brain.
  * IGNORE — noise.

Stage 1 — deterministic, free: a structured PARLEY message / real invite / a
visible `(ref: ...)` tag is always admitted; bulk senders are dropped; the
opt-in keyword (default "parley") admits.
Stage 2 — the residual plain mail: an optional **LLM classifier** (a small, cheap
model — the top-level `[llm-gateway]` table, per-account override via
`[account.gateway] model`; never the brain's model) answers one binary question;
without one (or on any classifier failure — **fail open**) the lexical
scheduling-words heuristic decides, as before.

The funnel principle stands: *determinism is the funnel; the LLM sits at the narrow
end* — and the classifier is itself funnel-shaped: it only ever sees mail stage 1
couldn't decide, its input is truncated, and its use is journaled (`by:
classifier`), so a misroute is visible, never silent.

**Pure function, not a per-account object (2026-08-31 refactor).** Every account
gets its own `cfg`/`classifier` at the call site — there's no need for the Agent
to construct and hold a `Gateway` instance per account; a small closure or a
partial covers the same ground with less bookkeeping. The one stateful thing
the old class had — a per-message classifier-verdict cache — is dropped: `route`
is only ever called once per message in practice, so the cache bought nothing.

**No per-principal policy here** — that's the trust table's job. This module
only ever judges CONTENT relevance, never WHO sent it, and — confirmed reading
the actual code (2026-08-31) — it never even looks at which `Account` it's
running for, so it was already reusable as-is; the Parley skill gets it for
free as part of `parley/agent/`, same as the server.
"""

ADMIT = "admit"
IGNORE = "ignore"

# Cheap lexical signals that a plain email is about scheduling (the classifier-less
# fallback — and the fail-open path when a classifier errors).
_SCHEDULING_WORDS = (
    "meet", "meeting", "call", "available", "availability", "schedule",
    "calendar", "free", "slot", "appointment",
    "monday", "tuesday", "wednesday", "thursday", "friday",
)

# The classifier's whole world: a one-line job, a subject, a truncated body.
_CLASSIFIER_SYSTEM = (
    "You are the intake filter of a scheduling agent. Decide if this email is "
    "something a scheduling agent should engage with: a meeting request, an "
    "availability question, a reschedule/cancel, or a reply in a scheduling "
    "thread — in any language. Reply with exactly one word: ADMIT or IGNORE.")
_CLASSIFIER_MAX_BODY = 1000


def route(message, payload, cfg=None, classifier=None, journal=None):
    """`cfg` is the [account.gateway] table:

        keyword    — a token a human can include to opt in (default "parley").
        lexical    — also admit on scheduling words (default True).
        route_plain_to — "ignore" to never admit plain human mail (admit only
                         structured PARLEY messages / invites / a visible ref).
                         Default admits.

    `classifier(subject, body) -> "admit"|"ignore"` may raise — any failure falls
    open to the lexical heuristic. `journal` (optional) records classifier verdicts.
    `payload` is whatever `wire.parse(message)` returned — checked for `type`
    (a real invite / notification) or `negotiation_id` (a visible ref, even on an
    otherwise plain-prose Parley message) as the deterministic fast-path."""
    cfg = cfg or {}
    route_plain_to = str(cfg.get("route_plain_to", "admit")).strip().lower()
    keyword = str(cfg.get("keyword", "parley")).strip().lower()
    lexical = bool(cfg.get("lexical", True))

    # Structured peer-to-peer message (a PARLEY type) or a real invite already
    # normalized into one, OR a bare visible ref on an otherwise plain-prose
    # Parley message → always admitted (deterministic fast-path).
    if payload and (payload.get("type") or payload.get("negotiation_id")):
        return ADMIT
    # Config can forbid acting on plain human mail entirely.
    if route_plain_to == IGNORE:
        return IGNORE
    if _is_bulk(message):
        return IGNORE
    text = f"{message.subject or ''} {getattr(message, 'body', '') or ''}".lower()
    # Deterministic opt-in: a human wrote the magic word — no model needed.
    if keyword and keyword in text:
        return ADMIT
    # Stage 2: the residual. Classifier if configured (fail open), else lexical.
    if classifier is not None:
        return _classify(message, classifier, journal, lexical)
    return ADMIT if _lexical_hit(text, lexical) else IGNORE


def _classify(message, classifier, journal, lexical):
    try:
        verdict = classifier(message.subject or "",
                             (getattr(message, "body", "") or "")[:_CLASSIFIER_MAX_BODY])
        if verdict not in (ADMIT, IGNORE):
            raise ValueError(f"classifier said {verdict!r}")
        by = "classifier"
    except Exception as err:  # noqa: BLE001 — FAIL OPEN: heuristic decides
        text = f"{message.subject or ''} {getattr(message, 'body', '') or ''}".lower()
        verdict = ADMIT if _lexical_hit(text, lexical) else IGNORE
        by = "rule"
        if journal is not None:
            journal.log("gateway", message_id=message.id, by="rule",
                       verdict=verdict, classifier_error=str(err))
        return verdict
    if journal is not None:
        journal.log("gateway", message_id=message.id, by=by,
                   verdict=verdict, sender=message.sender, subject=message.subject)
    return verdict


def _lexical_hit(text, lexical=True):
    return lexical and any(w in text for w in _SCHEDULING_WORDS)


def _is_bulk(message):
    # No raw headers on Message yet, so the noise floor is sender-based for
    # now (List-Id / Precedence: bulk detection lands when Message carries
    # headers).
    sender = (message.sender or "").lower()
    return sender.startswith(("no-reply@", "noreply@", "do-not-reply@"))


def llm_classifier(client):
    """Wrap an `LLMClient` (a small, cheap model — never the brain's) as the
    `classifier(subject, body)` callable. Strict output contract: the model's reply
    must start with ADMIT or IGNORE; anything else raises, and the gateway falls
    open to the lexical heuristic."""
    def classify(subject, body):
        resp = client.complete(
            _CLASSIFIER_SYSTEM,
            [{"role": "user", "content": f"Subject: {subject}\n\n{body}"}],
            [])
        word = (resp.text or "").strip().split()[:1]
        if word and word[0].upper() == "ADMIT":
            return ADMIT
        if word and word[0].upper() == "IGNORE":
            return IGNORE
        raise ValueError(f"unexpected classifier reply: {resp.text!r}")
    return classify
