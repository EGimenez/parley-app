"""Parley config — parse a YAML file describing one human's agent.

One config file = one human = one `Agent`, and — since iteration 07-0 — **one
complete, private agent**: the cfg is the single root, holding the LLM key and every
*provided* secret inline, and *naming* only the files the agent *generates*. The agent
reads this file on start and builds **one connection per account** listed here.

  * a top-level `llm:` mapping — the agent's brain: `anthropic_key` (inline) or
                       `anthropic_key_file`, and `model` (see `load_llm`).
  * a backend        — gmail | outlook | yahoo | local-outlook
  * backend settings — inline provided secrets (`google:` under an account, Yahoo
                       `app_password`, Outlook `client_id`) + names for generated
                       state files (kept opaque in `settings`, handed to the
                       connection). Everything not consumed structurally passes through.
  * a set of calendars — busy/free is the *union* across them; the first is the
                       booking target where new events land
  * its own policy   — so the same agent can be permissive on one account and
                       strict on another

Generated files live under `~/.parley/state/<name>/` (off any cloud-synced repo tree);
a relative path in the cfg resolves against `PARLEY_HOME`, not the cwd (see
`resolve_state_path` / the per-account `state_dir`).

See parley/config_templates/cfg.gmail.example.yaml for the shape (seeded into
~/.parley on first run). Parsing is read-only (PyYAML's
safe_load) and nothing here touches os.environ — the old flat KEY=value loader is
gone. The only writers live at the bottom: the ~/.parley pointers/registries the CLI
and daemon maintain (default, usage.json, servers.json — iteration 22).
"""

import difflib
import json
import os
import re
import zlib
import yaml
from dataclasses import dataclass, field

# Per-user state home (default-config pointer, server logs). See client-server.md §5.
PARLEY_HOME = os.path.join(os.path.expanduser("~"), ".parley")
_DEFAULT_PORT_BASE = 8787

# Keys consumed structurally on an account entry; everything else is passed
# through to the connection as backend-specific `settings` (so `google:`,
# `app_password`, `client_id`, `token`, … all reach the backend untouched).
_ACCOUNT_KEYS = {"backend", "address", "calendars", "policy", "gateway",
                 "poll_seconds", "name", "state_dir", "intake"}

# Settings keys that name a *generated* state file: resolved to an absolute path under
# PARLEY_HOME so a cfg can write `token = "state/gmail/token.json"` and mean
# `~/.parley/state/gmail/token.json` regardless of the cwd.
_STATE_PATH_KEYS = ("token", "history", "history_file", "msal_cache", "delta", "state_file")


@dataclass
class LLMConfig:
    """The agent's models: the top-level `llm:` mapping (the brain: key + model) and
    the optional `llm-gateway:` mapping right after it — the SMALL model the gateway's
    relevance classifier runs on (iteration 12). The gateway mapping may carry its own
    `provider` and `anthropic_key`(`_file`); each falls back to the brain's. The key
    comes ONLY from the cfg (`anthropic_key` / `anthropic_key_file`) — there is no
    ambient `ANTHROPIC_API_KEY` env fallback (iteration 07-0: the cfg is the one source
    of the key). Empty mappings → no key (the brain can't act) and no gateway model just
    means the lexical heuristic."""
    anthropic_key: str = ""
    model: str = ""
    provider: str = ""    # llm: provider — anthropic (default) | ollama; empty → anthropic
    base_url: str = ""    # llm: base_url — OpenAI-compat endpoint (provider: ollama); empty →
                          # the provider's default (ollama → http://localhost:11434/v1)
    driver: str = ""  # llm: driver — inprocess (default) | session. "session" hands each
                      # admitted event to a background Claude Code session over the work
                      # queue instead of running the tool loop in this process.
    tz: str = ""      # optional IANA tz override; empty → use each calendar's own zone
    owner: str = ""   # the human this agent speaks for (top-level `user: {name: …}`); the
                      # name Parley introduces itself with in human-readable mail. Empty → none
    gateway_model: str = ""     # llm-gateway: model — enables the intake classifier
    gateway_provider: str = ""  # llm-gateway: provider — default: the brain's
    gateway_key: str = ""       # llm-gateway: anthropic_key / api_key — default: the brain's
    gateway_base_url: str = ""  # llm-gateway: base_url — default: the brain's


@dataclass
class AccountConfig:
    """One authenticated identity the agent speaks for."""
    backend: str                                     # gmail | outlook | yahoo | local-outlook
    address: str = ""                                # own address (also discovered from the API)
    calendars: list = field(default_factory=list)    # busy-union; calendars[0] = booking target
    settings: dict = field(default_factory=dict)     # backend-specific (inline secrets + state paths)
    policy: dict = field(default_factory=dict)       # {"structured": {...}, "unstructured": ""}
    gateway: dict = field(default_factory=dict)      # account.gateway: route rules
    poll_seconds: int = 15
    intake: bool = True                              # False = calendar-only: never polled for
                                                     # mail, but still counted in busy/free


def load_config(path):
    """Parse a Parley YAML file into a list[AccountConfig] (one per `account:` entry)."""
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    poll_default = int(data.get("poll_seconds", 15))
    accounts = [_account(raw, poll_default) for raw in data.get("account", [])]
    if not accounts:
        raise SystemExit(f"No account: entries in {path}")
    return accounts


def state_home():
    """`~/.parley/state` — where every agent's *generated* files live, off any
    cloud-synced repo tree. Not created here (parsing stays read-only); the backend
    makes its own `state/<name>/` on first connect."""
    return os.path.join(PARLEY_HOME, "state")


def resolve_state_path(path, base=None):
    """Resolve a cfg path: `~` expands, an absolute path is returned unchanged, and a
    relative path resolves under `base` (default PARLEY_HOME) — so a cfg names its
    generated files relative to `~/.parley`, never the cwd. Pure string work; no disk."""
    if not path:
        return path
    p = os.path.expanduser(str(path))
    if os.path.isabs(p):
        return os.path.normpath(p)
    return os.path.normpath(os.path.join(base or PARLEY_HOME, p))


def _table_key(table):
    """A table's API key: `anthropic_key`/`api_key` inline (the generic `api_key` is the
    provider-neutral alias — same meaning, friendlier for non-Anthropic providers), else
    the contents of the file named by `anthropic_key_file`/`api_key_file` (resolved under
    PARLEY_HOME), else '' (a local provider like ollama needs no key)."""
    key = str(table.get("anthropic_key", "") or table.get("api_key", "") or "").strip()
    key_file = table.get("anthropic_key_file") or table.get("api_key_file")
    if not key and key_file:
        key_path = resolve_state_path(key_file)
        if os.path.exists(key_path):
            with open(key_path, encoding="utf-8") as fh:
                key = fh.read().strip()
    return key


def _model_name(value):
    """A cfg model: a concrete id kept as written, or a family alias (opus / sonnet /
    haiku — "the newest of that family") folded to lower case, the form the CLI and
    the alias resolver both expect."""
    from parley.agent.llm_port import is_model_alias
    model = str(value or "").strip()
    return model.lower() if is_model_alias(model) else model


def load_llm(path):
    """Read the optional top-level `llm:` mapping (the brain) and `llm-gateway:`
    mapping (the intake classifier's small model; own provider/key optional, defaults
    to the brain's) → `LLMConfig`. Absent mappings yield an empty LLMConfig (no key —
    the brain can't act); the key is only ever what the cfg names, never an env var."""
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    llm = data.get("llm", {}) or {}
    key = _table_key(llm)
    model = _model_name(llm.get("model"))
    # The model is a config decision — never defaulted in code. A cfg that bothers to
    # declare an llm: mapping must name its model; fail loudly (naming the file) rather
    # than let a silent fallback pick the model for the user.
    if llm and not model:
        raise SystemExit(f"{path}: llm: needs a model: \"...\" (e.g. \"opus\" for the newest "
                         f"Opus, or a full id like \"claude-sonnet-4-6\").")
    gateway = data.get("llm-gateway", {}) or {}
    user = data.get("user", {}) or {}
    return LLMConfig(anthropic_key=key, model=model,
                     provider=str(llm.get("provider", "") or "").strip(),
                     base_url=str(llm.get("base_url", "") or "").strip(),
                     driver=str(llm.get("driver", "") or "").strip().lower(),
                     tz=str(llm.get("tz", "") or "").strip(),
                     owner=str(user.get("name", "") or "").strip(),
                     gateway_model=_model_name(gateway.get("model")),
                     gateway_provider=str(gateway.get("provider", "") or "").strip(),
                     gateway_key=_table_key(gateway),
                     gateway_base_url=str(gateway.get("base_url", "") or "").strip())


def load_server(path):
    """Read the optional `server:` mapping → (host, port). Port defaults to a stable,
    per-config value (8787 + crc32(path) % 1000) so distinct configs get distinct
    ports — two local agents (e.g. gmail + outlook) don't collide. crc32 (not the
    salted built-in hash) keeps the port stable across CLI invocations."""
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    srv = data.get("server", {}) or {}
    host = srv.get("host", "127.0.0.1")
    default_port = _DEFAULT_PORT_BASE + zlib.crc32(os.path.abspath(path).encode()) % 1000
    return host, int(srv.get("port", default_port))


# --- Config resolution + default pointer (client-server.md §5) ----------------

def scaffold_home():
    """First-run bootstrap: ensure ~/.parley/ exists with a `logs/` dir and the
    per-backend example configs (`cfg.<backend>.example.yaml`), so a new user has
    templates to copy to `cfg_<name>.yaml`. Idempotent — only writes an example that
    isn't already there, and never touches a real cfg. The templates ship INSIDE
    the package (`parley/config_templates/`, package data), so this also works for
    a wheel install without the source tree."""
    os.makedirs(os.path.join(PARLEY_HOME, "logs"), exist_ok=True)
    from importlib import resources
    try:
        entries = [e for e in resources.files("parley").joinpath("config_templates").iterdir()
                   if e.name.startswith("cfg.") and e.name.endswith(".example.yaml")]
    except (ModuleNotFoundError, FileNotFoundError, OSError):
        return
    for entry in sorted(entries, key=lambda e: e.name):
        dst = os.path.join(PARLEY_HOME, entry.name)
        if not os.path.exists(dst):
            try:
                with open(dst, "w", encoding="utf-8", newline="\n") as d:
                    d.write(entry.read_text(encoding="utf-8"))
            except OSError:
                pass


def cfg_display_name(path):
    """~/.parley/cfg_gmail2.yaml -> 'gmail2' (fallback: the bare filename). The
    agent's display name everywhere (registry, tray, --status): distinguishes two
    configs on the same backend (cfg_gmail.yaml vs cfg_gmail2.yaml), which the
    backend's own class-level name ("gmail") cannot."""
    base = os.path.basename(path or "")
    if base.startswith("cfg_") and base.endswith(".yaml"):
        return base[4:-5]
    return base or "?"


def resolve_config(name):
    """Resolve a `-cfg NAME` to a YAML path. NAME may be a literal path or a short
    name (`gmail` → cfg_gmail.yaml), searched: cwd `config/` · ~/.parley. Seeds the
    ~/.parley example templates on the way (scaffold_home)."""
    if name and os.path.exists(name):
        return os.path.abspath(name)
    scaffold_home()
    # Short names resolve case-insensitively: the wizard writes cfg_<name>.yaml
    # lowercased, so `-cfg Ramon` and `-cfg ramon` are the same agent.
    fname = name if name.endswith(".yaml") else f"cfg_{name.lower()}.yaml"
    for d in (os.path.join(os.getcwd(), "config"), PARLEY_HOME):
        cand = os.path.join(d, fname)
        if os.path.exists(cand):
            return os.path.abspath(cand)
    # A near-miss existing agent is FAR likelier than a genuinely new one with a
    # similar name (e.g. "-cfg local-outlook" when the agent was actually named
    # "outlooklocal" at setup — the backend's own key and the agent's chosen name
    # are different things by design, easy to misremember). Flag it before ever
    # offering to build a duplicate agent under the mistyped name.
    match = _closest_agent_name(name)
    if match:
        print(f"'{name}' isn't a known agent — did you mean '{match}'? "
              f"(parley -cfg {match} ...)")
    # First run on a tty: offer the wizard under this very name, then re-check
    # (iteration 23 phase 4). Scripts/pipes fall straight through to the error.
    from parley.install_config.install_config import offer_setup
    if offer_setup(name=name, reason=f"Config '{name}' not found.") is not None:
        cand = os.path.join(PARLEY_HOME, fname)
        if os.path.exists(cand):
            return os.path.abspath(cand)
    raise SystemExit(
        f"Config '{name}' not found. Run  parley --setup {name}  — or copy the matching "
        f"cfg.<backend>.example.yaml from {PARLEY_HOME} to {fname} and fill it in "
        f"(the llm: key + your account's inline secrets).")


def _closest_agent_name(name):
    """The best-matching existing agent short-name for a `-cfg NAME` that didn't
    resolve, or None below a confidence floor (never suggest something unrelated).

    Plain character-similarity (`difflib`) alone picks the wrong thing for exactly
    the case this exists for: "local-outlook" (the backend's own key — easy to
    guess) vs. an agent actually named "outlooklocal" at setup (the agent's name
    and the backend's key are different things by design) — `outlook` alone
    scores HIGHER against "local-outlook" than "outlooklocal" does, since it's a
    clean substring. So: first check whether every word of the query appears
    (in any order) inside the candidate — catches exactly that reordering/no-
    separator case — and only fall back to character similarity for plain typos
    ("gmial" -> "gmail")."""
    candidates = []
    for d in (os.path.join(os.getcwd(), "config"), PARLEY_HOME):
        if os.path.isdir(d):
            candidates.extend(cfg_display_name(f) for f in os.listdir(d)
                              if f.startswith("cfg_") and f.endswith(".yaml"))
    query = name.lower()
    query_tokens = re.findall(r"[a-z0-9]+", query)
    best, best_score = None, 0.0
    for c in candidates:
        c_low = c.lower()
        if query_tokens and all(t in c_low for t in query_tokens):
            extra = len(c_low) - sum(len(t) for t in query_tokens)
            score = 1.0 if extra <= 1 else 0.75
        else:
            score = difflib.SequenceMatcher(None, query, c_low).ratio()
        if score > best_score:
            best, best_score = c, score
    return best if best_score >= 0.6 else None


def default_config():
    """The path saved by `-as default`, or None."""
    pointer = os.path.join(PARLEY_HOME, "default")
    if not os.path.exists(pointer):
        return None
    with open(pointer, encoding="utf-8") as f:
        path = f.read().strip()
    return path or None


def set_default(path):
    """Record `path` as the default config (written by `-as default`)."""
    os.makedirs(PARLEY_HOME, exist_ok=True)
    pointer = os.path.join(PARLEY_HOME, "default")
    with open(pointer, "w", encoding="utf-8") as f:
        f.write(os.path.abspath(path))


# --- Usage nudge (iteration 22, D3) -------------------------------------------

def note_usage(cfg_name):
    """Called on a client command that named `-cfg` explicitly while NO default is
    set. Bumps `~/.parley/usage.json` and returns a one-time hint string once the
    same name has been used 3 times — after which `hinted` sticks and the nudge
    never repeats. Returns None otherwise (including on any IO/JSON trouble:
    the nudge is a courtesy, never a failure)."""
    if default_config():
        return None
    path = os.path.join(PARLEY_HOME, "usage.json")
    try:
        os.makedirs(PARLEY_HOME, exist_ok=True)
        try:
            with open(path, encoding="utf-8") as f:
                usage = json.load(f)
        except (OSError, ValueError):
            usage = {}
        entry = usage.setdefault(cfg_name, {"count": 0, "hinted": False})
        entry["count"] += 1
        hint = None
        if entry["count"] >= 3 and not entry["hinted"]:
            entry["hinted"] = True
            hint = (f"💡 tip: you've used -cfg {cfg_name} {entry['count']} times — "
                    f"make it the default:  parley -cfg {cfg_name} --default")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(usage, f, indent=1)
        return hint
    except OSError:
        return None


# --- Servers registry (iteration 22, D9/D13) -----------------------------------
#
# `~/.parley/servers.json`: {cfg_abs_path: {name, cfg_path, host, port, pid, started}}.
# A daemon registers on start and deregisters on any *deliberate* stop (shutdown
# endpoint, Ctrl+C). A crash or reboot leaves the entry behind — that is exactly the
# "registered-but-down" state the tray revives (D13). Keyed by cfg path so a restart
# replaces its own stale entry.

def _registry_path():
    return os.path.join(PARLEY_HOME, "servers.json")


def read_registry():
    """The registry as a dict (possibly empty). Tolerant: unreadable → {}."""
    try:
        with open(_registry_path(), encoding="utf-8") as f:
            reg = json.load(f)
        return reg if isinstance(reg, dict) else {}
    except (OSError, ValueError):
        return {}


def register_server(cfg_path, name, host, port, started):
    key = os.path.abspath(cfg_path)
    reg = read_registry()
    reg[key] = {"name": name, "cfg_path": key, "host": host, "port": port,
                "pid": os.getpid(), "started": started}
    _write_registry(reg)


def deregister_server(cfg_path):
    reg = read_registry()
    if reg.pop(os.path.abspath(cfg_path), None) is not None:
        _write_registry(reg)


def _write_registry(reg):
    try:
        os.makedirs(PARLEY_HOME, exist_ok=True)
        with open(_registry_path(), "w", encoding="utf-8") as f:
            json.dump(reg, f, indent=1)
    except OSError:
        pass  # registry is best-effort; the daemon must not die over it


# --- Autostart list (the tray's boot seed) -----------------------------------
#
# `~/.parley/autostart.json`: a plain list of agent NAMES the tray starts ONCE,
# when it launches. Distinct from servers.json (the live keep-alive registry):
# this is the durable "I want these running" intent. A deliberate `parley --stop`
# removes an agent from servers.json AND marks it in autostart_suppressed.json
# below, so it stays down both for the rest of this session AND across the next
# tray launch — until you start it again yourself, which clears the mark.

def _autostart_path():
    return os.path.join(PARLEY_HOME, "autostart.json")


def _norm_agent(name):
    """Short names are case-folded (the wizard writes cfg_<name>.yaml lowercased,
    so `Gmail` and `gmail` are one agent); explicit paths/filenames pass through."""
    if os.path.exists(name) or name.endswith(".yaml") or os.sep in name:
        return name
    return name.lower()


def read_autostart():
    """The pinned agent names as a list (possibly empty). Tolerant: unreadable → []."""
    try:
        with open(_autostart_path(), encoding="utf-8") as f:
            names = json.load(f)
        return [str(n) for n in names] if isinstance(names, list) else []
    except (OSError, ValueError):
        return []


def _write_autostart(names):
    os.makedirs(PARLEY_HOME, exist_ok=True)
    with open(_autostart_path(), "w", encoding="utf-8") as f:
        json.dump(names, f, indent=1)


def autostart_add(name):
    """Pin an agent (idempotent). Returns the updated list."""
    name = _norm_agent(name)
    names = read_autostart()
    if name not in names:
        names.append(name)
        _write_autostart(names)
    return names


def autostart_remove(name):
    """Unpin an agent (matches the case-folded form too). Returns the updated list."""
    name = _norm_agent(name)
    names = read_autostart()
    kept = [n for n in names if _norm_agent(n) != name]
    if kept != names:
        _write_autostart(kept)
    return kept


def autostart_paths():
    """Pinned names → existing cfg paths, skipping any that don't resolve to a file.
    GENTLE (unlike resolve_config): never triggers the setup wizard — the tray must
    seed unattended. Order preserved."""
    out = []
    for name in read_autostart():
        if os.path.exists(name):
            out.append(os.path.abspath(name))
            continue
        fname = name if name.endswith(".yaml") else f"cfg_{name.lower()}.yaml"
        cand = os.path.join(PARLEY_HOME, fname)
        if os.path.exists(cand):
            out.append(os.path.abspath(cand))
        else:
            print(f"⚠️ autostart: no config for '{name}' (skipped) — expected {cand}")
    return out


# `~/.parley/autostart_suppressed.json`: cfg paths a DELIBERATE stop (the tray's
# "Quit (stop agents too)", or `parley --stop`/Ctrl+C — anything that reaches
# serve_from_config's deregister) most recently touched. autostart_paths() lists
# durable *intent* ("I want this running"); this list overrides it for exactly one
# thing — the tray's boot-time seed — so a deliberate stop actually stays stopped
# across tray restarts (user report 2026-09-05: "Quit" looked broken because the
# very next tray launch silently revived everything again). Cleared the moment the
# agent registers again (any real start — `serve_from_config`'s `register_server`
# call), so once you bring it back yourself, autostart resumes trusting it.

def _suppressed_path():
    return os.path.join(PARLEY_HOME, "autostart_suppressed.json")


def _read_suppressed():
    try:
        with open(_suppressed_path(), encoding="utf-8") as f:
            paths = json.load(f)
        return set(paths) if isinstance(paths, list) else set()
    except (OSError, ValueError):
        return set()


def _write_suppressed(paths):
    try:
        os.makedirs(PARLEY_HOME, exist_ok=True)
        with open(_suppressed_path(), "w", encoding="utf-8") as f:
            json.dump(sorted(paths), f, indent=1)
    except OSError:
        pass  # best-effort, same as the registry


def suppress_autostart(cfg_path):
    paths = _read_suppressed()
    paths.add(os.path.abspath(cfg_path))
    _write_suppressed(paths)


def clear_autostart_suppression(cfg_path):
    paths = _read_suppressed()
    if os.path.abspath(cfg_path) in paths:
        paths.discard(os.path.abspath(cfg_path))
        _write_suppressed(paths)


def autostart_suppressed(cfg_path):
    return os.path.abspath(cfg_path) in _read_suppressed()


def agent_overview():
    """Every agent this install knows about, and the THREE facts that decide whether
    it is running — in one place, because until now none of them were visible.

    `parley --status` read only the live registry, so an agent you had pinned but
    that was not registered simply did not appear, and a deliberately-stopped one
    looked identical to one that had never existed. The state was real and
    unobservable, which is the actual reason two autostart files feel confusing
    (user, 2026-09-24).

    Returns one dict per agent, sorted by name:
        name, cfg_path, registered (in servers.json), host/port,
        autostart (pinned), suppressed (a deliberate stop, until it starts again)
    Union of the live registry, the autostart pins, and any cfg_*.yaml on disk, so
    an agent cannot hide by being absent from one of them."""
    agents = {}

    def slot(cfg_path, name=None):
        key = os.path.abspath(cfg_path)
        if key not in agents:
            agents[key] = {"name": name or cfg_display_name(key), "cfg_path": key,
                           "registered": False, "host": None, "port": None,
                           "autostart": False, "suppressed": autostart_suppressed(key)}
        elif name:
            agents[key]["name"] = name
        return agents[key]

    for entry in read_registry().values():
        row = slot(entry.get("cfg_path", ""), entry.get("name"))
        row.update(registered=True, host=entry.get("host"), port=entry.get("port"))
    for path in autostart_paths():
        slot(path)["autostart"] = True
    if os.path.isdir(PARLEY_HOME):
        for fname in sorted(os.listdir(PARLEY_HOME)):
            if fname.startswith("cfg_") and fname.endswith(".yaml"):
                slot(os.path.join(PARLEY_HOME, fname))
    return sorted(agents.values(), key=lambda a: a["name"])


_ENGINE_LABELS = {"claude-code": "Claude Code (no API key)", "ollama": "Ollama (local)",
                  "anthropic": "Anthropic API key"}


def agent_settings(cfg_path):
    """What the tray's Settings window shows for one agent: its engine, the brain and
    gateway models, and its connections. Reads the cfg as data only — never builds
    an agent — and an unreadable cfg comes back as {"error": ...} so one broken
    file cannot hide the others."""
    try:
        with open(cfg_path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception as err:  # noqa: BLE001 — shown in the window, not raised
        return {"error": str(err)}
    llm = data.get("llm") or {}
    provider = str(llm.get("provider") or "anthropic").strip().lower().replace("_", "-")
    provider = "claude-code" if provider == "claudecode" else provider
    connections = []
    for account in data.get("account") or []:
        line = f"{account.get('backend', '?')}  {account.get('address', '')}".strip()
        connections.append(line + ("  (calendar only)" if account.get("intake") is False else ""))
    return {"provider": provider, "engine": _ENGINE_LABELS.get(provider, provider),
            "model": str(llm.get("model") or "(none)"),
            "gateway": str((data.get("llm-gateway") or {}).get("model") or "keyword list"),
            "connections": connections}


def load_desktop(path):
    """Read the optional `desktop:` mapping → dict: `hotkey` ("win+alt+p" default,
    D12) and `autostart` (bool, default True, D13). Missing file/mapping →
    defaults, so the tray app runs with no cfg at all.

    A leftover `ask_via:` from an older install is ignored: questions always open
    in the window now, with "Open in Claude" on each card."""
    defaults = {"hotkey": "win+alt+p", "autostart": True}
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except OSError:
        return defaults
    d = data.get("desktop", {}) or {}
    return {"hotkey": str(d.get("hotkey", defaults["hotkey"]) or defaults["hotkey"]).lower(),
            "autostart": bool(d.get("autostart", True))}


def _account(raw, poll_default):
    backend = raw.get("backend")
    if not backend:
        raise SystemExit("Each [[account]] needs a backend (gmail | outlook | local-outlook)")
    # account.policy: → the policy document. Every key is a structured rule
    # except an optional `unstructured` prose string; a legacy `name` is ignored
    # (policy is data now, not a class selection — see policy.py).
    pol = raw.get("policy", {}) or {}
    policy = {
        "structured": {k: v for k, v in pol.items() if k not in ("name", "unstructured")},
        "unstructured": str(pol.get("unstructured", "") or ""),
    }
    settings = {k: v for k, v in raw.items() if k not in _ACCOUNT_KEYS}
    # Generated state lives under ~/.parley/state/<name> (name = explicit `name`, else
    # the backend). The backend resolves its own files against this base; any state path
    # the cfg set explicitly (token, msal_cache, delta, …) is resolved under PARLEY_HOME.
    name = str(raw.get("name") or backend)
    settings["state_dir"] = resolve_state_path(raw.get("state_dir") or os.path.join("state", name))
    for k in _STATE_PATH_KEYS:
        if settings.get(k):
            settings[k] = resolve_state_path(settings[k])
    return AccountConfig(
        backend=backend,
        address=raw.get("address", ""),
        calendars=list(raw.get("calendars", [])),
        settings=settings,
        policy=policy,
        gateway=dict(raw.get("gateway", {}) or {}),
        poll_seconds=int(raw.get("poll_seconds", poll_default)),
        intake=bool(raw.get("intake", True)),
    )
