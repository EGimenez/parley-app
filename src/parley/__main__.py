"""`python -m parley` / the `parley` console script — the ONE entry (iteration 22).

By default this is the *client*: positional args are a free-form instruction sent
to your agent's daemon (spawned if down), exactly like talking to Parley:

    parley "set me a meeting with x@y.com before friday about sync"
    parley -cfg gmail "..."          # explicit config for this call only
    parley -cfg gmail --default      # remember gmail as the default
    parley --status | --watch | --stop

Three mode flags leave client mode:
  * `--serve`     → run the persistent daemon in this terminal (server.py).
  * `--run`       → the OLD foreground mode: one agent in this terminal, no daemon.
  * `--selftest`  → the in-memory selftest (no credentials).
With `--serve`/`--run` a single positional is the env provider (gmail | outlook |
local-outlook | memory); in client mode positionals are always the instruction.

The composition (registry, builders) lives in `agent/compose.py`; serving in
`client_server/server.py`; the client loop in `client_server/parley.py`.
Keep this dispatcher thin.
"""

import argparse
import sys

# The status output uses arrows/emoji; force UTF-8 so it doesn't crash on a
# cp1252 Windows console (otherwise every run needs PYTHONUTF8=1).
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def build_parser():
    parser = argparse.ArgumentParser(
        prog="parley",
        description="Talk to your Parley agent. A bare instruction goes to your default\n"
                    "agent's daemon (started for you if it's down) and prints the reply —\n"
                    "or the agent's question, which you answer right there.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="examples:\n"
               "  parley set me a meeting with x@y.com friday about sync\n"
               "  parley -cfg gmail --default        # gmail becomes the default agent\n"
               "  parley --status                    # every registered daemon, up/down\n"
               "  parley --setup                     # first agent, everything verified live\n"
               "  parley --desktop                   # tray: toasts + questions + Win-Alt-P")

    talk = parser.add_argument_group("talk to your agent (the everyday mode)")
    talk.add_argument("words", nargs="*", metavar="instruction",
                      help="free-form instruction for your agent; with --run/--serve, "
                           "a single env provider name (gmail | outlook | local-outlook | memory)")
    talk.add_argument("-cfg", "--config-file", dest="cfg",
                      help="config name or path (e.g. gmail → cfg_gmail.yaml); "
                           "omitted = the default agent")
    talk.add_argument("--default", action="store_true",
                      help="remember -cfg as the default agent (~/.parley/default)")
    talk.add_argument("--status", action="store_true",
                      help="no -cfg: table of ALL registered daemons; with -cfg: that one's status")
    talk.add_argument("--watch", action="store_true",
                      help="attach and answer every parked question as it comes (Ctrl+C detaches)")
    talk.add_argument("--pending", action="store_true",
                      help="list the questions your agent is waiting on you to answer "
                           "(JSON)")
    talk.add_argument("--answer", nargs=2, metavar=("REF", "TEXT"),
                      help="answer one pending question: the ref, then your decision "
                           "in your own words")
    talk.add_argument("--dismiss", nargs="+", metavar=("REF", "REASON"),
                      help="close one pending question WITHOUT answering it (you "
                           "handled it elsewhere, or it no longer matters): the ref, "
                           "then optionally why. The agent is not woken")
    talk.add_argument("--focus", nargs="*", metavar="ARG",
                      help="Dynamic Focus Blocks. No argument: read a JSON request on "
                           "stdin and create one (focus time before a meeting). "
                           "'list': list them. 'cancel DFB_ID': drop one and its blocks")
    talk.add_argument("--policy", nargs="?", const="", metavar="RULE",
                      help="with no argument, print the policy your agent runs on; "
                           "with one, add it as a standing rule")
    talk.add_argument("--consult", action="store_true",
                      help="read one JSON verb call on stdin and run it READ-ONLY "
                           "for context (check, get_negotiation, find_contact, "
                           "search_events)")
    talk.add_argument("--negotiate", action="store_true",
                      help="read one negotiation as JSON on stdin and open it "
                           "(used by the /parley Claude Code skill; see "
                           "ParleyServer.negotiate)")
    talk.add_argument("--stop", action="store_true",
                      help="stop the daemon for -cfg / the default agent "
                           "(counterpart of --serve; it stays stopped until asked again)")

    run = parser.add_argument_group("run an agent (what --stop stops)")
    run.add_argument("--serve", action="store_true",
                     help="run the persistent daemon in this terminal "
                          "(the client normally spawns it for you)")
    run.add_argument("--run", action="store_true",
                     help="one agent in the foreground, no daemon — watch it reason")
    run.add_argument("--desktop", action="store_true",
                     help="launch the tray app (toasts + questions + Win-Alt-P), detached; "
                          "add --stop to stop it, --no-autostart/--hotkey to configure it")
    run.add_argument("--no-autostart", action="store_true",
                     help="with --desktop: watch only, never revive registered-but-down agents")
    run.add_argument("--hotkey", metavar="SPEC",
                     help='with --desktop: override the prompt hotkey (default "win+alt+p")')
    run.add_argument("--autostart", nargs="?", const="on", choices=["on", "off"],
                     help="pin -cfg to the tray's autostart list (started once when the "
                          "tray launches); '--autostart off' unpins; no -cfg lists the pinned agents")

    setup = parser.add_argument_group("set up & check")
    setup.add_argument("--setup", action="store_true",
                       help="guided wizard: one or more accounts + the engine (Claude "
                            "Code, no key, or an LLM key), every "
                            "credential verified live -> ~/.parley/cfg_<name>.yaml "
                            "(opens the Setup Window; --console keeps it in the terminal)")
    setup.add_argument("--add-account", action="store_true", dest="add_account",
                       help="append one more live-verified account to -cfg "
                            "(or the default agent's cfg). NOTE: the agent receives "
                            "on every account but always SENDS what you instruct from "
                            "the first one -- for a separate outbound address, run "
                            "--setup again for a second agent instead")
    setup.add_argument("--engine", action="store_true",
                       help="change only the engine of -cfg (or the default agent): "
                            "Claude Code (no API key) or an Anthropic key, brain + "
                            "gateway models, verified live; the rest of the cfg stays")
    setup.add_argument("--console", action="store_true",
                       help="with --setup/--add-account/--engine: stay in the terminal "
                            "instead of opening the Setup Window")
    setup.add_argument("--selftest", action="store_true",
                       help="run the whole agent in memory — no credentials, no network")
    return parser


def _survive_a_narrow_console():
    """Never let an un-encodable character kill a command.

    Parley's console lines are full of status glyphs, and on Windows a redirected
    or non-UTF-8 stdout is cp1252, where printing one of them raises
    UnicodeEncodeError. Losing a glyph is fine; losing the command is not. This is
    the blanket guard for every entry point; `log._echo` is the same promise for
    the daemon journal, which does not come through here.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:  # noqa: BLE001 - not a reconfigurable stream; carry on
            pass


def _read_stdin_json():
    """Parse the JSON piped in on stdin, always as UTF-8.

    The companion to `_survive_a_narrow_console` on the way in. `sys.stdin.read()`
    decodes with the locale encoding, which on Windows is cp1252: an accent in the
    payload reaches the peer mojibake'd, and a byte cp1252 leaves undefined (0x8d,
    the tail of a UTF-8 capital I-acute) becomes a lone surrogate that blows up the
    daemon journal mid-request -- so the caller sees a dropped connection and never
    learns the mail did not go out. Read the raw bytes instead; `utf-8-sig` also
    eats the BOM a PowerShell redirect likes to prepend.
    """
    raw = sys.stdin.buffer.read() or b"{}"
    import json
    return json.loads(raw.decode("utf-8-sig"))


def main(argv=None):
    _survive_a_narrow_console()
    if argv is None:
        argv = sys.argv[1:]
    args = build_parser().parse_args(argv)

    if args.selftest:
        from parley.testing.selftest import selftest
        return selftest()

    if args.focus is not None:
        import json
        from parley.client_server.config import default_config, resolve_config
        from parley.client_server.parley import ParleyCLI
        cli = ParleyCLI(config_path=resolve_config(args.cfg) if args.cfg
                        else default_config())
        cli.ensure_server()
        verb = (args.focus[0].lower() if args.focus else "create")
        if verb == "list":
            result = cli.focus({"action": "list"})
        elif verb == "cancel" and len(args.focus) > 1:
            result = cli.focus({"action": "cancel", "dfb_id": args.focus[1],
                                "reason": " ".join(args.focus[2:]) or None})
        elif verb == "create":
            try:
                payload = json.loads(sys.stdin.read() or "{}")
            except ValueError as err:
                print(json.dumps({"ok": False, "error": f"bad JSON on stdin: {err}"}))
                return 1
            result = cli.focus({**payload, "action": "create"})
        else:
            print(json.dumps({"ok": False, "error": "use: --focus | --focus list | "
                                                    "--focus cancel DFB_ID"}))
            return 1
        print(json.dumps(result, default=str, ensure_ascii=False))
        return 0 if (result or {}).get("ok") else 1

    if args.pending or args.answer or args.dismiss or args.policy is not None:
        # The human-surface trio, same shape as --negotiate: resolve the agent,
        # spawn its daemon if down, print JSON. Used by the /parley skill from a
        # session the user already has open, and perfectly usable by hand.
        import json
        from parley.client_server.config import default_config, resolve_config
        from parley.client_server.parley import ParleyCLI
        cli = ParleyCLI(config_path=resolve_config(args.cfg) if args.cfg
                        else default_config())
        cli.ensure_server()
        if args.pending:
            result = cli.pending()
        elif args.answer:
            result = cli.decide(args.answer[0], args.answer[1])
        elif args.dismiss:
            result = cli.dismiss(args.dismiss[0], " ".join(args.dismiss[1:]))
        else:
            result = cli.policy(args.policy or None)
        print(json.dumps(result, default=str, ensure_ascii=False))
        return 0 if (result or {}) is not None else 1

    if args.consult:
        import json
        from parley.client_server.config import default_config, resolve_config
        from parley.client_server.parley import ParleyCLI
        try:
            payload = _read_stdin_json()
        except ValueError as err:
            print(json.dumps({"ok": False, "error": f"stdin is not JSON: {err}"}))
            return 2
        cli = ParleyCLI(config_path=resolve_config(args.cfg) if args.cfg
                        else default_config())
        cli.ensure_server()
        result = cli.consult(payload)
        print(json.dumps(result, default=str, ensure_ascii=False))
        return 0 if (result or {}).get("ok") is not False else 1

    if args.negotiate:
        # Path A. The caller (the /parley skill) already resolved the peer, looked
        # at free/busy and drafted the prose; this just hands it to the daemon,
        # spawning one if it is down. JSON in, JSON out, so a skill can pipe it.
        import json
        from parley.client_server.config import default_config, resolve_config
        from parley.client_server.parley import ParleyCLI
        try:
            payload = _read_stdin_json()
        except ValueError as err:
            print(json.dumps({"ok": False, "error": f"stdin is not JSON: {err}"}))
            return 2
        cli = ParleyCLI(config_path=resolve_config(args.cfg) if args.cfg else default_config())
        cli.ensure_server()
        result = cli.negotiate(payload)
        print(json.dumps(result, default=str))
        return 0 if (result or {}).get("ok") is not False else 1

    if args.desktop:
        # The tray itself is a GUI script (pythonw, no console) so its own --help /
        # --stop can't print in cmd.exe (cmd doesn't wait on a GUI subsystem exe).
        # This console entry is the reliable cross-shell way to drive it.
        if args.stop:  # `parley --desktop --stop` — stop the running tray
            from parley.client_server.parley_desktop import _stop_running
            return _stop_running()
        # Otherwise launch it detached so this terminal returns immediately (the
        # tray outlives it). pythonw on Windows = no console window; elsewhere a
        # new session detaches it from this tty. Forward the tray's config flags.
        import os
        import subprocess
        cmd = [sys.executable, "-m", "parley.client_server.parley_desktop"]
        if args.no_autostart:
            cmd.append("--no-autostart")
        if args.hotkey:
            cmd += ["--hotkey", args.hotkey]
        if args.cfg:
            cmd += ["-cfg", args.cfg]
        kwargs = {}
        if sys.platform == "win32":
            pythonw = os.path.join(os.path.dirname(cmd[0]), "pythonw.exe")
            if os.path.exists(pythonw):
                cmd[0] = pythonw
            kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED | NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, **kwargs)
        print("Parley Desktop starting — look for the sail icon in the system tray. "
              "(Stop it with: parley --desktop --stop)")
        return 0

    if args.setup:
        # A single positional is the agent name (like the provider in --serve).
        if len(args.words) > 1:
            raise SystemExit("--setup takes at most one positional (the agent name).")
        from parley.install_config.install_config import run_setup
        return run_setup(args.words[0] if args.words else None,
                         console_only=args.console)

    if args.engine:
        from parley.client_server.config import default_config, resolve_config
        path = resolve_config(args.cfg) if args.cfg else default_config()
        if not path:
            raise SystemExit("No default agent yet - pass -cfg <name>, or run: parley --setup")
        from parley.install_config.install_config import run_change_engine
        return run_change_engine(path, console_only=args.console)

    if args.add_account:
        from parley.client_server.config import default_config, resolve_config
        path = resolve_config(args.cfg) if args.cfg else default_config()
        if not path:
            raise SystemExit("No default agent yet - pass -cfg <name>, or run: parley --setup")
        from parley.install_config.install_config import run_add_account
        return run_add_account(path, console_only=args.console)

    if args.serve or args.run:
        # A single positional is the env provider here (these modes take no instruction).
        provider = args.words[0] if len(args.words) == 1 else None
        if args.words and provider is None:
            raise SystemExit("--serve/--run take at most one positional (the provider).")
        config_file = None
        if args.cfg:
            from parley.client_server.config import resolve_config
            config_file = resolve_config(args.cfg)
        if args.serve:
            from parley.client_server.server import serve_from_config
            return serve_from_config(config_file=config_file, provider=provider)
        from parley.agent.compose import (build_agent, load_llm_config, resolve_accounts,
                                          set_window_title)
        agent = build_agent(resolve_accounts(provider=provider, config_file=config_file),
                            llm=load_llm_config(config_file))
        set_window_title(f"Parley — {agent.name}")
        return agent.run()

    from parley.client_server.parley import client_main
    return client_main(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
