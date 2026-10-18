"""Open a Claude Code session on the questions an agent is waiting for.

Piece 2 of the human surface. A pending question always opens in the tray's own
question window first — it is instant, and most questions are a plain yes or no. Each
card there carries an "Open in Claude" button for the ones that are not. It used to be
an install-time choice (`desktop: ask_via`), all or nothing, made before you had seen
a single question; a session takes the better part of a minute to start, which is a
poor trade for "Accept". Nothing here is a *push*: the user clicked, so they are at the
keyboard by construction — no windows appear unbidden and no session outlives an
unattended toast.

Why bother, when the question window already has Accept and Deny: because the
questions that genuinely need a person are rarely yes/no. "This reply fits two of your
open meetings — which did you mean?" deserves "what was the other one about?" first,
and a session with the `human` MCP role can answer that from the agent's own memory
before you commit. A popup cannot.

The session is given the `human` role only (see mcp_server): read the agent's context,
relay the user's answer, record a standing rule. It cannot send mail or touch a
calendar, and `--strict-mcp-config` keeps the user's own MCP servers out of it.

Windows-first, like the tray itself. Elsewhere `launch()` reports that it cannot open
a terminal rather than pretending, and the caller falls back to the window.
"""

import json
import os
import shutil
import subprocess
import sys

#: What the session may call. Mirrors mcp_server's human role; the daemon enforces the
#: read-only boundary again on its own side, where a prompt cannot reach it.
HUMAN_TOOLS = ("pending", "answer", "dismiss", "policy", "check", "get_negotiation",
               "find_contact", "search_events")

_PROMPT = """You are helping {owner} answer questions from Parley, their scheduling \
agent ({agent}). {count}

Start by calling `pending` to see what is waiting. Be brief: short sentences and \
bullets, no long paragraphs, no quoted mail. List the questions by their `summary` \
(who, what, when), grouped by `kind`, and open the full `question` only for the one \
you are discussing. Then talk it through with them like a colleague would: say what \
the question is in plain words, and offer what you know. \
If they need context before deciding, get it — `get_negotiation` has the history of a \
negotiation, `search_events` and `check` see their calendar.

When they have decided, call `answer` with their decision in their own words. Only \
then: you are relaying their answer, never deciding for them, and never guessing to \
move things along. "Ask the other side what it is about first" is a perfectly good \
answer to relay.

If they say it is already handled ("ya está", "I answered it in Outlook", "forget \
it"), call `dismiss` instead: it closes the question without waking the agent.

If they state a preference that outlives this question — "never book me before 10" — \
offer to record it with `policy`. A one-off decision is not a rule; do not turn one \
into the other on your own.

You cannot send mail or change a calendar from here, and you should not try. Parley \
does that once it has their answer."""


_FOCUS = """ They opened this session from one question in particular: {what} \
(negotiation {ref}). Start with that one. They may bring up the others too; handle \
those the same way, one `answer` per question they decide."""


def available(cli="claude"):
    """Whether a session can be opened at all: the CLI is on PATH and we know how to
    open a terminal here. The question window hides its button otherwise, rather than
    offering something that can only fail."""
    return sys.platform == "win32" and shutil.which(cli) is not None


def mcp_config(base_url, python=None):
    """The MCP config the session is launched with: Parley's human role and nothing
    else. Paired with --strict-mcp-config, so none of the user's own servers (and none
    of their tools) come along into a session about their calendar."""
    return {"mcpServers": {"parley": {
        "command": python or sys.executable,
        "args": ["-m", "parley.mcp_server", "--base", base_url.rstrip("/"),
                 "--role", "human"]}}}


def seed_prompt(agent_name, pending, owner="your user", focus=None):
    """The session's opening prompt. `focus` is the pending item whose button was
    clicked: the session starts there, but still sees — and may answer — the rest,
    because "and the other one, the Tuesday one?" is how people actually talk."""
    count = (f"There {'is' if len(pending) == 1 else 'are'} {len(pending)} waiting."
             if pending else "Check whether anything is still waiting.")
    if focus:
        what = " - ".join(x for x in (focus.get("from"), focus.get("title"),
                                      focus.get("summary")) if x)
        count += _FOCUS.format(what=f"'{what}'" if what else "this one",
                               ref=focus.get("negotiation_id") or "?")
    return _PROMPT.format(owner=owner, agent=agent_name, count=count)


def cmd_safe(text):
    """One line, safe to sit inside double quotes in a .cmd file.

    cmd re-reads the line it is running, so `%` would be expanded (turning `%s` into
    nothing, silently) and an inner double quote would end the argument early, handing
    Claude half a prompt and cmd the rest as commands. Newlines simply cannot survive
    at all — a .cmd line ends at the newline — so they collapse to spaces."""
    flat = " ".join(str(text).split())
    # cmd reads the script back in the OEM codepage, so a stray em-dash becomes
    # mojibake mid-prompt. Fold to ASCII here rather than hope.
    flat = flat.replace("—", " - ").replace("–", "-")
    flat = flat.encode("ascii", "ignore").decode("ascii")
    return flat.replace("%", "%%").replace('"', "'")


def _safe_name(name):
    """A filename/title fragment: no separators, no accents cmd would mangle when it
    reads the script back in the OEM codepage."""
    ascii_only = str(name).encode("ascii", "ignore").decode("ascii")
    return "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in ascii_only) or "agent"


def _state_dir():
    from parley.client_server.config import PARLEY_HOME
    path = os.path.join(PARLEY_HOME, "state", "ask")
    os.makedirs(path, exist_ok=True)
    return path


def launch(agent_name, base_url, pending=(), owner="your user", cli="claude", python=None,
           run=None, directory=None, focus=None):
    """Open a terminal running Claude Code against this agent's human role, starting
    on `focus` (the question whose button was clicked) when given.

    Returns (ok, detail). A failure is never fatal to the caller: the question stays
    in the tray's own window, because a missing CLI must not mean an unanswerable
    question."""
    exe = shutil.which(cli)
    if not exe:
        return False, f"the Claude Code CLI ({cli}) is not on PATH"
    if sys.platform != "win32":
        return False, "opening a terminal is only wired for Windows so far"

    # `directory` is for tests: nothing should write into the user's real
    # ~/.parley just to check the shape of a launcher script.
    directory = directory or _state_dir()
    # Stable names per agent: overwritten on each click, so nothing accumulates and
    # the last launch is always inspectable when something goes wrong.
    config_path = os.path.join(directory, f"mcp-{_safe_name(agent_name)}.json")
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(mcp_config(base_url, python=python), f)

    allowed = ",".join(f"mcp__parley__{name}" for name in HUMAN_TOOLS)
    prompt = cmd_safe(seed_prompt(agent_name, list(pending), owner=owner, focus=focus))
    # A .cmd wrapper rather than an argv through `start`: that path re-parses the
    # command line twice and mangles a long quoted prompt differently depending on
    # which parser gets it. A file is read once, by one thing.
    script_path = os.path.join(directory, f"open-{_safe_name(agent_name)}.cmd")
    count = len(list(pending))
    waiting = (f"{count} question{'s' if count != 1 else ''} waiting"
               if count else "checking what is waiting")
    with open(script_path, "w", encoding="ascii", errors="replace", newline="\r\n") as f:
        # Say something IMMEDIATELY. Claude spends the better part of a minute
        # starting its MCP server and reading the negotiation before it prints a
        # word, and a blank window for forty seconds reads as "nothing happened" —
        # which is exactly how this looked the first time it was tried for real.
        f.write("@echo off\n"
                f"title Parley - {_safe_name(agent_name)}\n"
                f"echo Parley: {waiting} for {_safe_name(agent_name)}.\n"
                "echo Opening Claude - it takes a moment to read the negotiation "
                "before it answers.\n"
                "echo.\n"
                # The prompt goes FIRST. `--allowedTools` and `--mcp-config` are
                # variadic, so a prompt placed after either is read as one more tool
                # name and the session starts with nothing to do — which is exactly
                # how this failed live: Claude opened, sat there, and the only clue
                # was "Input must be provided..." when stdout happened to be a file.
                f'"{exe}" "{prompt}" --strict-mcp-config --mcp-config "{config_path}" '
                f'--allowedTools "{allowed}"\n'
                # Without this the window vanishes on a bad exit, taking the reason
                # with it. On a clean exit it closes as you would expect.
                "if errorlevel 1 (echo. & echo Claude exited with an error. & pause)\n")
    from parley.agent.session_supervisor import child_env
    runner = run or (lambda argv: subprocess.Popen(
        argv, creationflags=subprocess.CREATE_NEW_CONSOLE, env=child_env()))
    try:
        runner(["cmd", "/c", script_path])
    except OSError as err:
        return False, f"could not open a terminal: {err}"
    return True, script_path
