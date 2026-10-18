"""parley — the thin client-server CLI client (IUC-1).

Since iteration 22 the argument surface lives in `parley/__main__.py` (the one
`parley` entry); this module holds the client behavior it dispatches to. Running
`python -m parley.client_server.parley ...` still works — `main` below forwards
to the umbrella parser, so old scripts keep their meaning.

The ASK/END contract (user decision 2026-07-05): every agent reply to a client is
either **ASK** (`status: question` — the prompt stays open so the user can answer,
and follow-up questions chain) or **END** (`status: done` — the reply is printed and
the command exits immediately, no Enter, no held terminal). Negotiations that then
run for minutes or hours never hold the terminal: their later questions arrive in
the desktop tray, or here again via `--watch` (attach and answer every parked
question as it comes). A "what's still open?" line is answered by the brain
from its own negotiation memory — and if the user should pick one, the *brain* ASKs
("which one?") instead of ENDing. The agent's state stays on the server (server.py);
if the target is a local server that isn't running, the shared `Client` spawns it
(client-server.md §4).

    parley -cfg gmail --default              # remember a default config
    parley set me a meeting with x@y.com before friday about sync
    parley what meetings are still open      # the brain answers from memory
    parley --watch                           # attach: answer parked questions as they come
    parley --status                          # all registered daemons (or -cfg for one)
    parley --stop

Config: `-cfg NAME` resolves to ~/.parley/cfg_<NAME>.yaml (or a literal path); with
`--default` the choice is remembered in ~/.parley/default and later runs need no
`-cfg`. An explicit `-cfg` targets that agent for this call only and never touches
the default. All HTTP/daemon plumbing is inherited from `client_server.client.Client`.
"""

import json
import os
import sys
import time
import urllib.error

# Status output uses arrows/emoji; force UTF-8 so it doesn't crash a cp1252 console.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from parley.client_server.client import Client
from parley.client_server.config import (autostart_add, autostart_remove, default_config,
                                         load_server, note_usage, read_autostart,
                                         read_registry, resolve_config, set_default)


class ParleyCLI(Client):
    """The CLI is a `Client` used for one-shot instructions (a subset of the
    surface: instruct / status / stop, plus the daemon auto-start it inherits)."""


def client_main(args):
    """The client mode of the `parley` entry (parley/__main__.py parses `args`)."""
    # Registry overview needs no config at all: `parley --status` with no -cfg.
    if args.status and not args.cfg:
        return _status_all()

    # `parley --stop` with no -cfg stops EVERY registered daemon (mirrors
    # `--status`), not just the default — and never spawns one to kill it.
    if args.stop and not args.cfg:
        return _stop_all()

    # `parley --autostart` manages the tray's boot seed (no daemon needed).
    if getattr(args, "autostart", None) is not None:
        return _autostart_cmd(args.cfg, args.autostart)

    # Resolve which config this command targets.
    hint = None
    if args.cfg:
        config_path = resolve_config(args.cfg)
        if args.default:
            set_default(config_path)
            print(f"Default config set → {config_path}")
        else:
            hint = note_usage(args.cfg)  # one-time "make it the default" nudge (D3)
    else:
        if args.default:
            raise SystemExit("--default needs -cfg, e.g.  parley -cfg gmail --default")
        config_path = default_config()
        if not config_path:
            # First run on a tty: offer the wizard, then re-check (it can set
            # the default). Scripts/pipes fall straight through to the error.
            from parley.install_config.install_config import offer_setup
            offer_setup(reason="No config given and no default agent set yet.")
            config_path = default_config()
        if not config_path:
            raise SystemExit("No config given and no default set. "
                             "Run  parley --setup  (or:  parley -cfg gmail --default)")
        if not os.path.exists(config_path):
            raise SystemExit(f"Saved default config no longer exists: {config_path}")

    instruction = " ".join(args.words).strip()
    # `-cfg X --default` with no instruction is a pure "remember it" call.
    if args.default and not instruction and not args.status and not args.stop:
        return 0

    cli = ParleyCLI(config_path=config_path)

    # A targeted stop kills that one daemon if it's up — it must NOT spawn a fresh
    # server just to shut it down, so this runs before ensure_server().
    if args.stop:
        return _stop_one(cli)

    cli.ensure_server()

    if args.watch and not instruction:
        return _watch(cli)
    if args.status or not instruction:
        print(json.dumps(cli.status(), indent=2))
        return 0

    code = _converse(cli, instruction)
    if hint:
        print(hint)
    return code


def _status_all():
    """`parley --status` with no -cfg: every agent this install knows about, and why
    it is or is not running.

    It used to list only the live registry, which made two real states invisible: an
    agent pinned to autostart but not registered did not appear at all, and one you
    had deliberately stopped looked exactly like one that had never existed. Both
    facts live on disk; neither was shown anywhere (user, 2026-09-24)."""
    from parley.client_server.config import agent_overview
    agents = agent_overview()
    if not agents:
        print("No agents yet. Create one with  parley --setup .")
        return 0
    for agent in agents:
        pins = []
        pins.append("autostart on" if agent["autostart"] else "autostart off")
        if agent["suppressed"]:
            # The one piece of state a user cannot otherwise explain: the tray is
            # deliberately NOT reviving this, and starting it clears the latch.
            pins.append("STOPPED by you - start it to re-arm")
        note = f"  [{', '.join(pins)}]"
        if not agent["registered"]:
            print(f"  {agent['name']:<18} not running{note}")
            continue
        base = f"http://{agent['host']}:{agent['port']}"
        try:
            st = Client._http(base + "/v1/status", timeout=2)
            print(f"● {agent['name']:<18} {base}  up - "
                  f"{st.get('pending', 0)} pending, {st.get('open_negotiations', 0)} open"
                  f"{note}")
        except (urllib.error.URLError, OSError):
            print(f"○ {agent['name']:<18} {base}  down (registered){note}")
    return 0


def _stop_all():
    """`parley --stop` with no -cfg: stop every registered daemon that's up
    (probe-and-stop, never spawn). Down entries are left as they are. A deliberate
    shutdown deregisters the daemon, so a stopped agent stays stopped — unless the
    tray's autostart is running, which revives registered-but-down agents."""
    reg = read_registry()
    if not reg:
        print("No daemons registered — nothing to stop.")
        return 0
    stopped = 0
    for entry in sorted(reg.values(), key=lambda e: e.get("name", "")):
        base = f"http://{entry['host']}:{entry['port']}"
        name = entry.get("name", "?")
        if not Client._server_up(base):
            print(f"○ {name:<12} already stopped")
            continue
        try:
            Client(base=base).stop()
            print(f"● {name:<12} stopping")
            stopped += 1
        except (urllib.error.URLError, OSError) as err:
            print(f"⚠ {name:<12} couldn't stop: {err}")
    if stopped:
        pinned = read_autostart()
        note = ""
        if pinned:
            note = (f" Pinned agents ({', '.join(pinned)}) start again the next time the "
                    "tray launches — unpin one with  parley -cfg NAME --autostart off.")
        print(f"\nStopped {stopped} agent(s); they stay down for now.{note}")
    return 0


def _autostart_cmd(cfg, value):
    """`parley --autostart` manages the tray's boot seed (~/.parley/autostart.json):
    with no -cfg, list the pinned agents; with -cfg, pin (default) or unpin (`off`)."""
    if not cfg:
        names = read_autostart()
        if names:
            print("Autostart agents (the tray starts these once when it launches):")
            for n in names:
                print(f"  • {n}")
            print("\nUnpin one with:  parley -cfg NAME --autostart off")
        else:
            print("No autostart agents pinned. Pin one with:  parley -cfg gmail --autostart")
        return 0
    if value == "off":
        autostart_remove(cfg)
        print(f"Unpinned {cfg} — the tray will no longer start it at launch "
              "(a running daemon keeps running until you --stop it).")
    else:
        autostart_add(cfg)
        print(f"Pinned {cfg} to autostart — the tray starts it whenever it launches.")
    return 0


def _stop_one(cli):
    """`parley --stop -cfg X`: stop that one daemon if it's up — never spawn a
    server just to shut it down (the old ensure_server()+stop() did exactly that)."""
    host, port = load_server(cli.config_path)
    base = f"http://{host}:{port}"
    if not Client._server_up(base):
        print("Already stopped.")
        return 0
    Client(base=base).stop()
    print("Server stopping.")
    return 0


def _converse(cli, instruction):
    """ASK/END: send the instruction; while the agent ASKs, the prompt stays open and
    answers chain (`_answer_chain`); on END the reply is printed and the command exits
    immediately. Questions raised later (a peer replies in an hour) go to the
    desktop tray / `parley --watch` — never a held terminal."""
    result = _answer_chain(cli, cli.instruct(instruction))
    if result is None:
        return 0  # user left a question open / detached
    flag = "✅" if result.get("accepted", True) else "🛑"
    print(f"{flag} {result.get('message', result)}")
    return 0


def _answer_chain(cli, result):
    """Prompt + reply while the server keeps answering with questions. Returns the
    final non-question result, or None if the user detached / left it open."""
    while result.get("status") == "question":
        print(f"\n🤖 {result.get('question') or result.get('message')}")
        try:
            answer = input("   your reply (ACCEPT / DENY / or any guidance) > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n(left the question open — answer later in the desktop "
                  "tray, or with `parley --watch`.)")
            return None
        if not answer:
            print("(no reply — left open.)")
            return None
        result = cli.decide(result.get("ref") or result.get("negotiation_id"), answer)
    return result


def _watch(cli):
    """Explicit attach mode (`parley --watch`): negotiations keep moving on the
    server (peer replies arrive on the poll), and every question parked for the
    human — from ANY negotiation, whatever triggered it — is offered here inline.
    Polls /v1/pending, detaches on Ctrl+C — or by itself once nothing is open or
    pending anymore."""
    left_open = set()
    try:
        st = cli.status()
        if not st.get("pending") and not st.get("open_negotiations"):
            print("Nothing open or pending — nothing to watch.")
            return 0
        print("\n👀 attached — questions will appear here (Ctrl+C to detach; "
              "email replies and the desktop tray work too).")
        while True:
            try:
                items = cli.pending()
            except (urllib.error.URLError, OSError):
                time.sleep(3)
                continue
            for item in items:
                ref = item.get("negotiation_id")
                if ref in left_open:
                    continue
                print(f"\n🤖 {item.get('question') or item.get('title')}")
                try:
                    answer = input("   your reply (ACCEPT / DENY / or any guidance) > ").strip()
                except (EOFError, KeyboardInterrupt):
                    raise
                if not answer:
                    left_open.add(ref)  # don't nag; email/desktop can still answer it
                    print("(no reply — left open.)")
                    continue
                result = _answer_chain(cli, cli.decide(ref, answer))
                if result is not None:
                    print(f"✅ {result.get('message', '')}")
            st = cli.status()
            if not st.get("pending") and not st.get("open_negotiations"):
                print("✅ nothing open or pending — detaching.")
                return 0
            time.sleep(3)
    except (EOFError, KeyboardInterrupt):
        print("\n(detached — the agent keeps working; questions still reach you "
              "in the desktop tray.)")
        return 0


def main(argv=None):
    """Backcompat entry: `python -m parley.client_server.parley ...` — same surface
    as `python -m parley` (the umbrella owns the parser now)."""
    from parley.__main__ import main as umbrella_main
    return umbrella_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
