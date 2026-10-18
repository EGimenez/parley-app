"""Agent-level wizard steps: the LLM config (brain + gateway, pinged live) and
the per-account policy prose. Everything ACCOUNT-specific lives in one module
per backend under `install_config/backends/` (iteration 25) — this file only
keeps what applies to the agent as a whole.

Like the backend steps, each step asks, VERIFIES LIVE, and returns plain cfg
values — a step that fails re-prompts with the provider's error shown, so
nothing unverified ever reaches the written cfg.
"""

from parley.install_config import console

# Offline fallback only. The wizard normally ASKS the account which models it can
# use (see `_live_models`) — a list written here ages badly and does so silently:
# these three were current in 2026-07 and by 2026-09 the newest model was four
# releases past the one labelled "recommended". Kept for when the listing call
# cannot be made (no network, a key that cannot list) so the wizard still offers
# something rather than nothing.
BRAIN_MODELS = [
    ("claude-opus-4-8", "most capable Opus; the recommended brain"),
    ("claude-sonnet-5", "near-Opus on agentic work, cheaper and faster"),
    ("claude-sonnet-4-6", "previous Sonnet (the old template suggestion)"),
]

# Family aliases (see llm_port.MODEL_ALIASES): each always means the NEWEST model of
# its family, so the cfg the wizard writes never ages. Offered first on every path.
ALIAS_MODELS = [
    ("opus", "always the newest Opus - the recommended brain"),
    ("sonnet", "always the newest Sonnet - near-Opus, cheaper and faster"),
    ("haiku", "always the newest Haiku - small and quick"),
]

# The gateway intake classifier is a small triage model; haiku is the fit.
GATEWAY_MODEL = "haiku"


def _live_models(key, limit=12):
    """Ask this key's account which models it can actually use.

    `GET /v1/models` (no beta, free) answers newest-first, and the answer is
    per-account: a key without access to a model never sees it offered. Returns
    [(id, label)] or [] on any failure — a listing that cannot be made is not a
    reason to stop the wizard, it just falls back to the static suggestions."""
    try:
        import anthropic
        page = anthropic.Anthropic(api_key=key).models.list(limit=limit)
    except Exception:  # noqa: BLE001 — offline, bad key, old SDK: all the same here
        return []
    models = []
    for model in page.data:
        released = str(getattr(model, "created_at", "") or "")[:10]
        name = getattr(model, "display_name", "") or model.id
        models.append((model.id, f"{name} ({released})" if released else name))
    return models


def _pick_model(prompt, key, suggested=()):
    """One model menu: `suggested` first (ours, with the reason), then everything
    this account can use, then a free-text entry. No pre-selection — Parley never
    picks a model for you, so the menu makes choosing easy without choosing."""
    live = _live_models(key)
    options = list(suggested)
    listed = {model_id for model_id, _ in options}
    if live:
        options += [(model_id, label) for model_id, label in live if model_id not in listed]
    else:
        console.say("(could not list the account's models - showing the built-in "
                    "suggestions; type any id if what you want is missing.)")
        options += [(model_id, label) for model_id, label in BRAIN_MODELS
                    if model_id not in listed]
    return console.ask_choice(prompt, options,
                              other="another model id (type it yourself)")


def step_llm(existing_keys=None):
    """Agent-level LLM config: first the ENGINE, then the brain (`llm:`) and the
    gateway classifier (`llm-gateway:`), each proven live before it is accepted.

    Engine: the Claude Code CLI (rides the user's claude.ai login — no key at all)
    is offered only when `claude` is on PATH; otherwise, and when picked, an
    Anthropic API key. `existing_keys` is [(agent_name, key)] from other cfgs,
    offered for reuse so a second agent never re-pastes the key."""
    import shutil
    console.say()
    console.say("-- The agent's brain (the llm: config) --")
    if shutil.which("claude"):
        engine = console.ask_choice(
            "Which engine runs the agent's brain?",
            [("claude-code", "Claude Code - runs on your claude.ai login, NO API key"),
             ("anthropic", "Anthropic API key - billed per token, faster per step")],
            default="claude-code")
        if engine == "claude-code":
            return _step_claude_code_llm()
    else:
        console.say("(Tip: with the Claude Code CLI installed and logged in, Parley "
                    "needs no API key - https://claude.com/claude-code)")
    key = _ask_key(existing_keys)
    while True:
        model = _pick_model(
            "Model for the brain (your call - Parley never picks one for you):",
            key, suggested=ALIAS_MODELS)
        console.say("Verifying with a tiny API call...")
        error = _ping(key, model)
        if error is None:
            console.say(f"OK - key accepted, {model} answers.")
            break
        console.say(f"That did not work: {error}")
        console.say("Check the key/model and try again (Ctrl+C aborts).")
        key = _ask_key(existing_keys)
    gateway_model, gateway_key = _ask_gateway(key, model)
    return {"provider": "anthropic", "anthropic_key": key, "model": model,
            "gateway_model": gateway_model, "gateway_key": gateway_key}


def _step_claude_code_llm():
    """The keyless engine: the brain runs through `claude -p` on the user's own
    claude.ai login. Same rule as the key path — nothing is accepted until one tiny
    call proves the CLI is logged in and the model answers."""
    console.say("Claude Code runs the brain on your claude.ai login - nothing to paste.")
    console.say("Each step takes longer than with an API key (about 15-50 seconds).")
    while True:
        model = console.ask_choice(
            "Model for the brain (your call - Parley never picks one for you):",
            ALIAS_MODELS, other="a full model id (pins one release)")
        used, error = _ping_claude_code(model)
        if error is None:
            console.say(f"OK - Claude Code is logged in, {model} answers ({used}).")
            break
        console.say(f"That did not work: {error}")
        console.say("Check that Claude Code is logged in (run  claude  once in a "
                    "terminal), then pick again (Ctrl+C aborts).")
    return {"provider": "claude-code", "anthropic_key": "", "model": model,
            "gateway_model": _ask_claude_code_gateway(model), "gateway_key": ""}


def _ask_claude_code_gateway(brain_model):
    """`llm-gateway:` on the Claude Code engine: a model through the same CLI, or no
    model at all (the keyword list). Returns the model, or "" for keywords only."""
    console.say()
    console.say("-- The gateway's intake classifier (llm-gateway:) --")
    console.say("It triages every plain mail (ADMIT/IGNORE) before the brain runs.")
    console.say("Through Claude Code each mail is one claude -p call: 15-50 s and about")
    console.say("25k tokens of Claude Code's own prompt. A keyword list costs nothing.")
    options = [(GATEWAY_MODEL, "newest Haiku via Claude Code - the recommended classifier")]
    options += [(m, label) for m, label in ALIAS_MODELS if m != GATEWAY_MODEL]
    options.append(("none", "no model - a keyword list decides (instant, free)"))
    while True:
        model = console.ask_choice("Gateway model:", options,
                                   other="a full model id (pins one release)")
        if model == "none":
            return ""
        if model == brain_model:
            return model  # already proven by the brain's ping
        used, error = _ping_claude_code(model)
        if error is None:
            console.say(f"OK - {model} answers ({used}).")
            return model
        console.say(f"That did not work: {error}")


def _ping_claude_code(model):
    """(concrete model id, None) when `claude -p` answers on `model`, else
    (None, the CLI's error)."""
    from parley.agent.llm_port import ClaudeCodeClient
    console.say("Verifying with a tiny claude -p call (a few seconds)...")
    try:
        return ClaudeCodeClient(model=model).ping(), None
    except Exception as exc:  # surface the CLI's message, whatever it is
        return None, str(exc) or type(exc).__name__


def _ask_key(existing_keys):
    """The Anthropic key: reuse one from another agent's cfg, or paste fresh.
    (Menu labels never print the key itself — only the last 3 chars.)"""
    if existing_keys:
        options = [(name, f"reuse its key (ending ...{k[-3:]})")
                   for name, k in existing_keys]
        options.append(("(new)", "paste a different key"))
        picked = console.ask_choice("Anthropic API key - reuse from an existing agent?",
                                    options)
        if picked != "(new)":
            return dict(existing_keys)[picked]
    console.say("You need an Anthropic API key: https://console.anthropic.com/ -> API keys.")
    return console.ask_secret("Anthropic API key (sk-ant-...)")


def _ask_gateway(brain_key, brain_model):
    """`llm-gateway:` — the SMALL triage model. Key defaults to the brain's
    ('same key as the agent'); a separate key is pinged before it's accepted.
    Returns (model, key) with key == '' meaning 'same as the brain'."""
    console.say()
    console.say("-- The gateway's intake classifier (llm-gateway:) --")
    console.say("A SMALL model that triages plain mail (ADMIT/IGNORE) before the brain runs.")
    suggested = [(GATEWAY_MODEL, "small + cheap - the recommended classifier")]
    if brain_model != GATEWAY_MODEL:
        suggested.append((brain_model, "same model as the brain (dearer per mail)"))
    model = _pick_model("Gateway model:", brain_key, suggested=suggested)
    if console.ask_yn("Gateway key: same key as the agent?", default=True):
        return model, ""
    while True:
        key = console.ask_secret("Gateway Anthropic API key (sk-ant-...)")
        console.say("Verifying the gateway key with a tiny API call...")
        error = _ping(key, model)
        if error is None:
            console.say(f"OK - gateway key accepted, {model} answers.")
            return model, key
        console.say(f"That did not work: {error}")
        console.say("Check the key/model and try again (Ctrl+C aborts).")


def _ping(key, model):
    """One minimal completion through the same adapter the agent uses.
    Returns None on success, an error string on failure."""
    from parley.agent.llm_port import AnthropicClient
    try:
        client = AnthropicClient(api_key=key, model=model, max_tokens=64)
        client.complete("Reply with the single word: pong",
                        [{"role": "user", "content": "ping"}], [])
    except Exception as exc:  # surface the provider's message, whatever it is
        return str(exc) or type(exc).__name__
    return None


# --- policy -------------------------------------------------------------------

# The semi-negotiating stance from the brief: the agent negotiates freely, but a
# brand-new commitment is human-gated. Same prose as the example templates.
DEFAULT_POLICY = ("Ask me before accepting any brand-new meeting. Accept reschedules "
                  "of meetings I already have if the new slot is free.")


def step_claude_code():
    """Offer Parley's skills for Claude Code (/parley and /myorganizer).

    Skipped in silence when the CLI is not installed. Someone setting up a scheduling
    agent should not be pitched a second tool they do not have — and it is additive,
    so saying no costs them nothing. Answering questions in Claude is no longer asked
    here: every question card in the tray offers "Open in Claude" when the CLI is
    present, so the choice is made per question, once you can see it."""
    import shutil
    if not shutil.which("claude"):
        return {"install_skill": False}
    console.say()
    console.say("-- Claude Code (optional) --")
    console.say("You have the Claude Code CLI, so Parley can plug into it:")
    console.say("  * /parley in any session - arrange a meeting, answer what Parley is")
    console.say("    waiting for, set standing rules like 'never book me before 10'.")
    console.say("  * /myorganizer - organise your day, week or month across the sources")
    console.say("    you choose the first time you run it (calendar, notes, Jira, Excel...).")
    console.say("  * Each question in the tray gets an 'Open in Claude' button, so you")
    console.say("    can ask about a meeting before you decide.")
    install = console.ask_yn("Install the /parley and /myorganizer skills for Claude Code?",
                             default=True)
    return {"install_skill": install}


def step_policy():
    """The free-text policy the brain follows for this account."""
    console.say()
    console.say("-- Policy (plain prose the agent's brain follows) --")
    console.say("Suggested (semi-negotiating - the agent negotiates, you approve new meetings):")
    console.say(f'  "{DEFAULT_POLICY}"')
    if console.ask_yn("Use this suggested policy, or write your own?", default=True,
                      yes_label="Use it", no_label="Modify"):
        return DEFAULT_POLICY
    return console.ask("Your policy, in one line of plain prose")
