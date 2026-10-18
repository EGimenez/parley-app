"""Composition — wire a user's Agent from config.

The backend registry + the builders that turn `AccountConfig`s into a live
`Agent`. No argparse, no serving: the entry points (`__main__`, the
`parley_start_server` server entry, `client_server.server.serve_from_config`)
call in here. Kept out of `agent/__init__` so importing the Agent never drags in
config parsing.

  * `ACCOUNTS`         — backend name → (module, class), imported lazily.
  * `make_account`     — one AccountConfig → a live backend instance.
  * `build_agent`      — list[AccountConfig] → the single Agent.
  * `resolve_accounts` — CLI-ish inputs (a config file, or one env backend) →
                         list[AccountConfig] (prompts for the backend if absent).
  * `set_window_title` — label the console (Windows) so agents are tellable apart.
"""

import importlib
import os
import sys

from parley.agent.agent import Agent
from parley.agent.policy import Policy
from parley.client_server.config import AccountConfig

# backend name → (module, class). Imported lazily so `--selftest` needs no API libs.
# `memory` is the credential-free in-process backend (also drives the selftest) —
# the cfg key stays `memory` for back-compat even though the class is `TestAccount`
# now (2026-08-31: `MemoryAccount` collided with `ShortTermMemory`).
ACCOUNTS = {
    "gmail": ("parley.connectors.accounts.gmail", "GmailAccount"),
    "outlook": ("parley.connectors.accounts.outlook", "OutlookAccount"),
    "yahoo": ("parley.connectors.accounts.yahoo", "YahooAccount"),
    "local-outlook": ("parley.connectors.accounts.local_outlook", "LocalOutlookAccount"),
    "memory": ("parley.connectors.accounts.test_account", "TestAccount"),
}


def make_account(account):
    """Instantiate the backend for one AccountConfig, handing it its settings."""
    if account.backend not in ACCOUNTS:
        raise SystemExit(
            f"Unknown backend '{account.backend}'. Choose from: {', '.join(ACCOUNTS)}")
    module_name, class_name = ACCOUNTS[account.backend]
    module = importlib.import_module(module_name)
    return getattr(module, class_name)(account)


def build_agent(accounts, llm=None, base_policy=None, name=None):
    """Build one Agent from a list[AccountConfig]: one (account, policy, gateway)
    per account — each account's own `policy:` is its DELTA over `base_policy`
    (the cfg's top-level `policy:`, if any; empty by default, which keeps this
    fully backward compatible — an account's own block is then its whole
    effective policy, exactly as before). `llm` is the cfg's `[llm]` config; its
    key/model drive the agent's brain. Open negotiations + the user-messaging
    channel persist under `~/.parley/state/<agent>/`.

    `name` should be the config's own display name (`config.cfg_display_name`),
    not left to Agent's own fallback: with one account, that fallback is the
    connector's class-level backend label ("gmail") — identical for every config
    on that backend, so two gmail accounts would register and show in the tray
    under the same indistinguishable name.

    Note: wiring the actual top-level `policy:` YAML key into `base_policy` is
    config.py's job and is NOT done as part of this pass — `base_policy`
    currently always arrives empty from every real caller, so behavior is
    unchanged from before this refactor. Flagged, not silently dropped."""
    triples = []
    for account in accounts:
        conn = make_account(account)
        triples.append((conn, account.policy, account.gateway, account.intake))
    first = triples[0][0]
    return Agent(triples, name=name, poll_seconds=accounts[0].poll_seconds, llm=llm,
                 base_policy=base_policy or Policy(),
                 memory_path=_state_path(first, "negotiations.json"),
                 user_messaging_path=_state_path(first, "user_messaging.json"),
                 learned_path=_state_path(first, "policy_learned.md"),
                 log_dir=_journal_dir(first),
                 state_dir=_state_path(first, ""))


def _state_path(conn, filename):
    """`~/.parley/state/<account>/<filename>` — generated per-agent state. Path
    knowledge lives here in composition, so `agent.py` stays free of it."""
    try:
        from parley.client_server.config import PARLEY_HOME
    except Exception:  # noqa: BLE001 — no config layer (shouldn't happen) → in-memory
        return None
    name = getattr(conn, "name", None) or "default"
    return os.path.join(PARLEY_HOME, "state", name, filename)


def _journal_dir(conn):
    """`~/.parley/logs/<agent>/` — where the flight recorder (log.py) appends its
    daily JSONL of every message received + every decision made."""
    try:
        from parley.client_server.config import PARLEY_HOME
    except Exception:  # noqa: BLE001 — no config layer → in-memory log only
        return None
    name = getattr(conn, "name", None) or "default"
    return os.path.join(PARLEY_HOME, "logs", name)


def load_llm_config(config_file):
    """The cfg's `[llm]` config (`config.load_llm`) for `build_agent`, or None for the
    env-backed dev path (`python -m parley gmail`, no config file → env-var fallback)."""
    if not config_file:
        return None
    from parley.client_server.config import load_llm
    return load_llm(config_file)


def resolve_accounts(provider=None, config_file=None):
    """Turn the CLI-ish inputs into a list[AccountConfig]: a YAML config file if
    given, else a single env-backed backend (prompting for the backend name if
    none was supplied). Shared by the foreground and server entry points."""
    if config_file:
        from parley.client_server.config import load_config
        if not os.path.exists(config_file):
            raise SystemExit(f"Config file not found: {config_file}")
        return load_config(config_file)
    backend = provider
    while not backend or backend not in ACCOUNTS:
        if backend:
            print(f"Unknown backend '{backend}'. Choose from: {', '.join(ACCOUNTS)}")
        backend = input(f"Which backend? ({'/'.join(ACCOUNTS)}): ").strip()
    return [_env_account(backend)]


def _env_account(backend):
    """Single-account dev path (`python -m parley gmail`): one account whose
    backend settings come from the environment. PARLEY_POLICY=config pulls a few
    structured rules from the environment."""
    structured = {}
    if os.environ.get("PARLEY_POLICY", "default") == "config":
        structured = {
            "accept_reschedules_from": os.environ.get("PARLEY_ACCEPT_RESCHEDULES_FROM", ""),
            "work_start": os.environ.get("PARLEY_WORK_START", ""),
            "work_end": os.environ.get("PARLEY_WORK_END", ""),
        }
    return AccountConfig(
        backend=backend,
        policy={"structured": structured, "unstructured": ""},
        poll_seconds=int(os.environ.get("PARLEY_POLL_SECONDS", "15")),
    )


def set_window_title(text):
    """Label this process's terminal tab / console so several running agents are
    tellable apart. Emits the OSC-0 title escape (honored by Windows Terminal, VS
    Code, and most terminals — where the plain Win32 call doesn't show) and, on
    Windows, also the Win32 console title for classic conhost. Best-effort; never
    fatal. Note: under Windows Terminal the *taskbar* entry stays "Windows Terminal"
    — the OSC sequence controls the *tab* title, which is what's actually per-agent."""
    try:
        sys.stdout.write(f"\x1b]0;{text}\x07")
        sys.stdout.flush()
    except Exception:
        pass
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleTitleW(text)
        except Exception:
            pass
