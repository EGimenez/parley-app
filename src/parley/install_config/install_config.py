"""The setup wizard orchestrator (iteration 23).

`run_setup(name)` drives the interactive first-run flow:

    1. agent name                       -> ~/.parley/cfg_<name>.yaml
    2. accounts, looped until "done"    (backends.BY_KEY[...].collect(),
       each verified LIVE) + a policy per account — or skip
    3. llm: engine (Claude Code or API key), brain + gateway, pinged live
       (steps.step_llm)
    4. write the cfg atomically, read it back through the REAL loader
       (config.load_config), print a summary
    5. offer to remember it as the default agent (set_default)

Which backends exist is the `install_config.backends` registry's business —
this module only orchestrates.

YAML emission is template-fill, not a YAML writer: the emitted file keeps
teaching comments, and yaml.safe_load (read-only) verifies the result.
"""

import os
import re
import yaml

from parley.client_server.config import (PARLEY_HOME, autostart_add, scaffold_home,
                                         set_default)
from parley.install_config import backends, cfg_edit, console
from parley.install_config.steps import (step_claude_code, step_llm,
                                         step_policy)

_NAME_RE = re.compile(r"[a-z][a-z0-9_-]*")


def _ask_backend(adding=False):
    verb = "Add ANOTHER account to this agent" if adding else "Add an account now"
    done = "done - no more accounts" if adding else "not now"
    return console.ask_choice(
        f"{verb}? Every choice is verified live before anything is written.",
        [(m.KEY, m.MENU_LABEL) for m in backends.ALL] + [("skip", done)],
        # After the first account, most runs are done — default to "skip" so an
        # inattentive NEXT/Enter doesn't accidentally start a second account setup.
        default="skip" if adding else None)

CFG_TEMPLATE = """\
# Parley agent config — written by `parley --setup`.
# ONE cfg = ONE private agent: the LLM key (if any) + provided secrets live INLINE here;
# generated files are only NAMED (relative paths resolve under ~/.parley/).
# Keep this file private — it holds live credentials.

poll_seconds: 15

# The human this agent schedules for. Parley introduces itself with this name in the
# emails it sends peers ("Hi, this is Parley, <name>'s scheduling assistant ...").
user:
  name: "{owner}"

# The agent's brain (per cfg → per agent).
llm:
{llm_lines}

{gateway_block}
"""

# Trailer when the user skips the account step: the cfg parses but the agent
# has nothing to drive yet — say where the blocks live.
NO_ACCOUNT_TRAILER = """\
# Accounts: add one entry under `account:` per connection (gmail / outlook / yahoo /
# local-outlook). Copy the matching block from cfg.<backend>.example.yaml in
# this folder — or rerun `parley --setup`.
"""

# Account blocks are emitted below `account:` from each backend module's
# ACCOUNT_ITEM template (install_config/backends/); every item ends with the
# shared policy sub-map below. Templates carry the teaching comments.
ACCOUNT_HEADER = "account:\n"

_POLICY_TRAILER = """\
    policy:
      # Free-text policy the brain follows for this account. Trust table and
      # more knobs: see the cfg.<backend>.example.yaml templates in this folder.
      unstructured: "{policy}"
"""


def render_account_item(account, policy):
    """One `  - backend: ...` list item (no `account:` header), fully escaped."""
    fields = {k: _yaml_str(str(v)) for k, v in account.items() if not isinstance(v, list)}
    if "calendars" in account:
        fields["calendars"] = ", ".join(f'"{_yaml_str(c)}"' for c in account["calendars"])
    # Optional `calendar` pin (yahoo): emit the whole line only when one was chosen,
    # so a mail-only account stays clean. Templates that don't use it ignore the field.
    fields["calendar_line"] = (
        f'\n    calendar: "{_yaml_str(str(account["calendar"]))}"'
        "              # Yahoo CalDAV calendar to book on (pinned at setup)"
        if account.get("calendar") else "")
    template = backends.BY_KEY[account["backend"]].ACCOUNT_ITEM + _POLICY_TRAILER
    return template.format(policy=_yaml_str(policy), **fields)


_yaml_str = cfg_edit.yaml_str


def _ask_name(name):
    while True:
        if not name:
            name = console.ask("Please give your agent a name (e.g. gmail, work, home)")
        candidate = name.strip().lower()  # case is the user's to type; we store + resolve lowercase
        if _NAME_RE.fullmatch(candidate):
            return candidate
        console.say(f"'{name}' won't do - start with a letter, then "
                    "letters/digits/-/_ (case doesn't matter; it becomes cfg_<name>.yaml).")
        name = None


def _existing_keys(exclude_name):
    """[(agent_name, anthropic_key)] harvested from the other cfg_*.yaml files,
    so a second agent can reuse a key instead of re-pasting it."""
    found = []
    for fn in sorted(os.listdir(PARLEY_HOME)) if os.path.isdir(PARLEY_HOME) else []:
        m = re.fullmatch(r"cfg_(.+)\.yaml", fn)
        if not m or m.group(1) == exclude_name:
            continue
        try:
            with open(os.path.join(PARLEY_HOME, fn), encoding="utf-8") as f:
                key = ((yaml.safe_load(f) or {}).get("llm") or {}).get("anthropic_key", "")
        except Exception:
            continue  # unreadable cfg — just not offered for reuse
        if key and not key.startswith("sk-ant-…"):
            found.append((m.group(1), key))
    return found


def _window_frontend():
    """The Setup Window when it can actually open: no PARLEY_SETUP_CONSOLE=1
    override, and either a tty run (normal `parley --setup`) OR an explicit GUI
    launch (`PARLEY_SETUP_GUI=1` — the installer runs the wizard under pythonw,
    which has no console/tty, precisely so no black window sits behind it,
    iteration 26 A1). Piped/CI runs (no tty, no flag) fall through to the
    console wizard, still the only surface for pipes/SSH."""
    import sys
    if os.environ.get("PARLEY_SETUP_CONSOLE"):
        return None
    gui = bool(os.environ.get("PARLEY_SETUP_GUI"))
    tty = bool(getattr(sys.stdin, "isatty", lambda: False)())
    if not gui and not tty:
        return None
    try:
        from parley.install_config import setup_window
        if not setup_window.available():
            return None
        return setup_window.SetupWindow()
    except Exception:
        return None


def run_setup(name=None, console_only=False):
    # GUI front end (iteration 24): same wizard, driven from the Setup Window.
    # The check on console._frontend keeps recursive calls ("set up ANOTHER
    # agent?") inside the already-open window instead of nesting a second one.
    if not console_only and console._frontend is None:
        window = _window_frontend()
        if window is not None:
            return window.run(lambda: run_setup(name, console_only=True))
    console.say("Parley setup - builds one private agent config under ~/.parley.")
    scaffold_home()  # ~/.parley + logs/ + the example templates

    name = _ask_name(name)
    path = os.path.join(PARLEY_HOME, f"cfg_{name}.yaml")
    if os.path.exists(path):
        console.say(f"{path} already exists.")
        action = console.ask_choice(
            "What do you want to do with it?",
            [("engine", "change only the engine (accounts, policy and the rest stay)"),
             ("overwrite", "start over - its current contents will be lost"),
             ("abort", "leave it as it is")],
            default="engine")
        if action == "engine":
            return run_change_engine(path, console_only=True)
        if action == "abort":
            raise SystemExit("Setup aborted - your existing config is untouched.")

    # Accounts first — they're the thing being set up; the LLM config is
    # agent-level plumbing and comes after. One agent can drive SEVERAL
    # accounts (busy/free unioned), so loop until "done" (user feedback,
    # 2026-07-13). Console stays on top while browsers fly around.
    console.spotlight(True)
    try:
        accounts = []  # [(account_dict, policy_prose), ...]
        console.say()
        owner = console.ask(
            "Your name — Parley introduces itself with it to the people it emails "
            "(e.g. Alex). Leave blank to skip", default="").strip()
        console.say()
        backend = _ask_backend()
        while backend != "skip":
            account = backends.BY_KEY[backend].collect()
            accounts.append((account, step_policy()))
            console.say()
            backend = _ask_backend(adding=True)

        llm = step_llm(existing_keys=_existing_keys(name))
        claude_code = step_claude_code()
    finally:
        console.spotlight(False)

    body = CFG_TEMPLATE.format(llm_lines=cfg_edit.llm_lines(llm).rstrip("\n"),
                               owner=_yaml_str(owner),
                               gateway_block=cfg_edit.gateway_block(llm))
    if accounts:
        body += ACCOUNT_HEADER + "".join(render_account_item(a, p) for a, p in accounts)
    else:
        body += NO_ACCOUNT_TRAILER

    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(body)
    os.replace(tmp, path)

    with open(path, encoding="utf-8") as f:  # read-back proof 1: valid YAML
        parsed = yaml.safe_load(f)
    if accounts:  # read-back proof 2: the REAL loader accepts it end to end
        from parley.client_server.config import load_config
        loaded = load_config(path)  # list[AccountConfig]; raises if unusable
        assert len(loaded) == len(accounts), "loader round-trip mismatch"

    # Pin the new agent to the tray's boot seed (~/.parley/autostart.json) — else
    # the tray launches with an EMPTY set: it seeds only what's pinned here, and
    # nothing else ever writes this list on a fresh install (user report
    # 2026-07-21: "no autostart.json is being created"). Idempotent; unpin later
    # with  parley -cfg NAME --autostart off.
    autostart_add(name)

    console.say()
    console.say("Written and verified:")
    console.say(f"  agent    {name}")
    if owner:
        console.say(f"  for      {owner} (Parley introduces itself with this name)")
    console.say(f"  file     {path}")
    keyless = llm["provider"] == "claude-code"
    engine = "Claude Code, no API key" if keyless else "Anthropic API"
    console.say(f"  brain    {parsed['llm']['model']} ({engine}, verified live)")
    if not parsed.get("llm-gateway"):
        console.say("  gateway  keyword list (no model)")
    else:
        gw_note = ("via Claude Code" if keyless else
                   "own key, verified live" if llm["gateway_key"] else "same key as the agent")
        console.say(f"  gateway  {parsed['llm-gateway']['model']} ({gw_note})")
    if not keyless:
        console.say(f"  key      sk-ant-...{llm['anthropic_key'][-4:]} (verified live)")
    for acct, _ in accounts:
        extra = f", tz {acct['timezone']}" if acct.get("timezone") else ""
        console.say(f"  account  {acct['backend']} {acct['address']} (verified live{extra})")
    console.say(f"  tray     pinned to autostart (Parley Desktop starts this agent on its next launch)")
    if claude_code["install_skill"]:
        from parley.install_config.skill_install import install as install_skill
        ok, detail = install_skill()
        console.say(f"  skills   /parley and /myorganizer installed for Claude Code ({detail})"
                    if ok else f"  skills   could NOT all be installed: {detail}")
    console.say()

    if console.ask_yn("Make this your default agent? (parley then works with no -cfg)",
                      default=True):
        set_default(path)
        console.say(f"Default agent: {name}  (~/.parley/default)")

    console.say()
    if accounts:
        console.say("All set. Try:")
        console.say(f"  parley set me a meeting with x@y.com friday about sync")
    else:
        console.say(f"Next: give the agent an account:  parley --add-account -cfg {name}")

    # One install can host several agents (e.g. two people, or work vs home).
    console.say()
    if console.ask_yn("Set up ANOTHER agent (a separate person/mailbox, its own cfg)?",
                      default=False):
        console.say()
        return run_setup(None)
    return 0


def offer_setup(name=None, reason=""):
    """First-run friendliness (iteration 23 phase 4): an interactive human who
    hit a no-config error is offered the wizard instead of the bare SystemExit.
    Non-tty callers (scripts, pipes) get None back immediately — never blocks.
    Returns run_setup's result when the wizard ran, else None."""
    import sys
    if not sys.stdin.isatty():
        return None
    if reason:
        console.say(reason)
    if not console.ask_yn("Run the setup wizard now?", default=True):
        return None
    return run_setup(name if name and _NAME_RE.fullmatch(name.lower()) else None)


def run_change_engine(path, console_only=False):
    """`parley --engine`: change only an agent's engine (brain + gateway), verified
    live like the first setup. Only the engine keys of the `llm:` / `llm-gateway:`
    blocks change (cfg_edit) — accounts, policy, comments and hand edits stay. The
    previous file is kept as `<cfg>.bak`. A running agent keeps its old engine
    until it restarts."""
    if not console_only and console._frontend is None:
        window = _window_frontend()
        if window is not None:
            return window.run(lambda: run_change_engine(path, console_only=True))
    if not os.path.exists(path):
        raise SystemExit(f"{path} not found - run `parley --setup` first.")
    name = re.sub(r"^cfg_|\.yaml$", "", os.path.basename(path))
    console.say(f"Changing the engine of agent '{name}' ({path}).")
    llm = step_llm(existing_keys=_existing_keys(name))
    cfg_edit.write_llm(path, llm)
    console.say()
    console.say("Written and verified:")
    engine = "Claude Code, no API key" if llm["provider"] == "claude-code" else "Anthropic API"
    console.say(f"  brain    {llm['model']} ({engine})")
    console.say(f"  gateway  {llm['gateway_model'] or 'keyword list (no model)'}")
    console.say(f"  backup   {path}.bak")
    console.say("A running agent keeps its old engine until it restarts.")
    return 0


def run_add_account(path, console_only=False):
    """`parley --add-account`: append one MORE verified account to an existing
    cfg. Append is text-level (comments survive); the result must round-trip
    through yaml + the real loader BEFORE it replaces the original — if the
    `account:` list isn't the file's last section the append can't attach, and
    the original is left untouched with the block printed for hand-pasting."""
    if not console_only and console._frontend is None:
        window = _window_frontend()
        if window is not None:
            return window.run(lambda: run_add_account(path, console_only=True))
    if not os.path.exists(path):
        raise SystemExit(f"{path} not found - run `parley --setup` first.")
    console.say(f"Adding an account to {path}")
    with open(path, encoding="utf-8") as f:
        original = f.read()
    existing = (yaml.safe_load(original) or {}).get("account") or []

    backend = _ask_backend(adding=True)
    if backend == "skip":
        raise SystemExit("Nothing added.")
    console.spotlight(True)
    try:
        account = backends.BY_KEY[backend].collect()
        item = render_account_item(account, step_policy())
    finally:
        console.spotlight(False)

    has_list = re.search(r"^account:", original, re.M)
    text = original.rstrip("\n") + "\n" + (item if has_list
                                           else "\n" + ACCOUNT_HEADER + item)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    with open(tmp, encoding="utf-8") as f:
        appended = (yaml.safe_load(f) or {}).get("account") or []
    ok = (len(appended) == len(existing) + 1
          and appended[-1].get("backend") == account["backend"])
    if ok:
        from parley.client_server.config import load_config
        load_config(tmp)  # the REAL loader must accept it too (raises if not)
        os.replace(tmp, path)
        console.say()
        console.say(f"Added: {account['backend']} {account.get('address', '')} "
                    f"({len(appended)} account(s) now). Restart the agent to pick it up.")
        return 0
    os.remove(tmp)
    console.say()
    console.say(f"{path} is untouched: its account: list isn't the last section, so a")
    console.say("clean append isn't possible. Paste this under your account: list yourself:")
    console.say()
    console.say(item)
    raise SystemExit(1)
