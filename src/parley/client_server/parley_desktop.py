"""Parley Desktop — the tray app (iteration 22, D5–D7, D11–D13).

One background process, four jobs:

  * Tray icon — grey sail idle, teal when agents are up, amber badge = total
    pending questions across EVERY registered daemon (~/.parley/servers.json).
  * Toasts — a new pending question raises a Windows toast; clicking it opens
    the question window.
  * Question window — replaces the old modal accept/deny popups: Accept / Deny /
    free-text guidance per question, and a follow-up ASK chains in place (the
    ASK/END contract, same as the CLI). Each card also offers "Open in Claude"
    when the Claude Code CLI is installed, for questions worth talking through.
  * Win-Alt-P — global hotkey → floating prompt. Sends to the default cfg's
    daemon; `@name ` targets another running agent for that one message; Tab in
    an empty prompt cycles the target badge (D11). Configurable via `[desktop]
    hotkey = "win+alt+p"` in the default cfg (D12).

Autostart (D13): on start and on every poll, registered-but-down daemons are
revived (spawned exactly like the CLI client does) — unless `--no-autostart`.
A deliberate `parley --stop` deregisters, so a stopped agent stays stopped.
Tray menu: *Settings…* shows each agent's engine, models and connections and opens
the wizards for it (D-settings, 2026-10-18); *Detach* quits the tray leaving agents
running; *Quit* stops them too.

    python -m parley.client_server.parley_desktop                 # registry-driven
    python -m parley.client_server.parley_desktop -cfg outlook    # + watch this cfg too
    python -m parley.client_server.parley_desktop --no-autostart  # dev: watch only

It holds no agent state — the servers are the agents; this is a `Client`-powered
shell around `/v1/pending`, `/v1/decide` and `/v1/instruct`.
"""

import argparse
import atexit
import ctypes
import os
import queue
import signal
import sys
import threading
import time
import urllib.error

import tkinter as tk

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from parley.client_server.client import Client
from parley.client_server.config import (PARLEY_HOME, agent_overview, agent_settings,
                                         autostart_add,
                                         autostart_paths, autostart_remove,
                                         autostart_suppressed, clear_autostart_suppression,
                                         cfg_display_name, default_config, load_desktop,
                                         load_server, read_registry, resolve_config,
                                         set_default)
from parley.client_server.tray_backend import (COLORS as C, GlobalHotkey, MONO_FONT,
                                               Submenu, TrayIcon, UI_FONT, notify,
                                               set_app_icon)

POLL_SECONDS = 3
REVIVE_COOLDOWN = 60   # don't re-attempt a failing spawn more often than this
CARD_GRACE = 10        # a just-created chain card survives one stale snapshot


# --- The app ----------------------------------------------------------------------

class ParleyDesktop:
    def __init__(self, extra_cfgs=(), autostart=True, hotkey_spec=None):
        self.autostart = autostart
        self.extra_cfgs = [os.path.abspath(p) for p in extra_cfgs]
        self.events = queue.Queue()
        self.agents = []            # last snapshot: [{name, base, cfg_path, up, pending}]
        self._toasted = set()       # (base, ref) already announced
        self._revive_at = {}        # cfg_path → next allowed revive attempt (monotonic)

        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title("Parley")
        set_app_icon(self.root)  # Toplevels (question/prompt windows) inherit it
        self.question_win = None
        self.prompt_win = None
        self.settings_win = None
        self._wizard = None         # (kind, cfg_path) while a wizard process is open

        self.tray = TrayIcon([
            ("Open questions", lambda: self.events.put(("open",))),
            ("Ask Parley…", lambda: self.events.put(("prompt",))),
            ("Settings…", lambda: self.events.put(("settings",))),
            None,
            Submenu("Agents (tick = starts with the tray)", self._agent_menu),
            None,
            ("Detach (leave agents running)", lambda: self.events.put(("detach",))),
            ("Quit (stop agents too)", lambda: self.events.put(("quit",))),
        ])

        spec = hotkey_spec or self._desktop_settings()["hotkey"]
        self.hotkey = GlobalHotkey(spec, lambda: self.events.put(("prompt",)))
        if self.hotkey.error:
            print(f"⚠️ hotkey: {self.hotkey.error}")
            notify(self.tray, "Parley", f"Hotkey unavailable: {self.hotkey.error}")
        else:
            print(f"Parley Desktop up — hotkey {spec}, "
                  f"autostart {'on' if autostart else 'off'}.")

        if self.autostart:
            self._seed_autostart()

        threading.Thread(target=self._poll_loop, daemon=True).start()
        self.root.after(150, self._pump)

    def _agent_menu(self):
        """One row per agent, rebuilt each time the menu opens: what it is doing
        now, and a tick for whether the tray starts it. Clicking toggles that.

        Same `agent_overview()` the CLI's --status prints, so the two surfaces
        cannot disagree about which agents exist or why one is down.

        "running" comes from the poller's last live probe, not from the registry:
        a stale servers.json entry (dead pid, wrong port) used to show as running
        while the icon stayed grey (user report 2026-09-24)."""
        live = self._live_paths()
        rows = []
        for agent in agent_overview():
            if os.path.normcase(agent["cfg_path"]) in live:
                state = "running"
            elif agent["registered"]:
                state = "registered, not answering"
            elif agent["suppressed"]:
                state = "stopped by you"
            else:
                state = "not running"
            rows.append((f"{agent['name']} - {state}",
                         (lambda path=agent["cfg_path"], on=agent["autostart"]:
                          self.events.put(("autostart", path, not on))),
                         agent["autostart"]))
        return rows or [("(no agents yet - run parley --setup)", lambda: None, False)]

    def _set_autostart(self, cfg_path, enabled):
        """Tick/untick one agent. Enabling also clears a deliberate stop: without
        that, ticking a suppressed agent would change the tick and nothing else,
        because the suppression is what actually keeps the tray off it."""
        name = cfg_display_name(cfg_path)
        if enabled:
            autostart_add(name)
            clear_autostart_suppression(cfg_path)
        else:
            autostart_remove(name)
        print(f"autostart {'on' if enabled else 'off'} for {name}")
        notify(self.tray, "Parley",
               f"{name}: {'starts with the tray' if enabled else 'will not start on its own'}")

    def _seed_autostart(self):
        """Start each pinned agent (~/.parley/autostart.json) ONCE, at launch. They
        then register in servers.json and the normal keep-alive loop maintains them.
        This is boot-only: a later `parley --stop` deregisters an agent and it stays
        down until the next launch — the seed does not re-fire. Setting the revive
        cooldown up front stops the poll loop from double-spawning a still-starting
        agent (which, for outlook, would mean a second interactive browser login)."""
        now = time.monotonic()
        for cfg_path in autostart_paths():
            if autostart_suppressed(cfg_path):
                # A deliberate stop (tray Quit / `parley --stop`) on a PRIOR run —
                # respect it until the user starts this agent again themselves.
                print(f"autostart: skipping {cfg_display_name(cfg_path)} "
                      "(deliberately stopped last time — run it manually to resume)")
                continue
            self._revive_at[cfg_path] = now + REVIVE_COOLDOWN
            print(f"autostart: starting {cfg_display_name(cfg_path)}…")
            threading.Thread(target=self._revive, args=(cfg_path,), daemon=True).start()

    @staticmethod
    def _desktop_settings():
        path = default_config()
        return load_desktop(path) if path else {"hotkey": "win+alt+p", "autostart": True}

    # -- Poller thread: registry → probe → (maybe revive) → snapshot event --------

    def _watch_list(self):
        """Registry entries + any -cfg passed on the command line (deduped)."""
        entries = dict(read_registry())
        for cfg in self.extra_cfgs:
            if cfg not in entries:
                host, port = load_server(cfg)
                entries[cfg] = {"name": cfg_display_name(cfg), "cfg_path": cfg,
                                "host": host, "port": port}
        return list(entries.values())

    def _poll_loop(self):
        while True:
            snapshot = []
            for entry in self._watch_list():
                base = f"http://{entry['host']}:{entry['port']}"
                item = {"name": entry.get("name") or cfg_display_name(entry.get("cfg_path")),
                        "base": base, "cfg_path": entry.get("cfg_path"),
                        "up": False, "pending": []}
                try:
                    Client._http(base + "/v1/status", timeout=2)
                    item["up"] = True
                    item["pending"] = Client._http(base + "/v1/pending", timeout=3)
                except (urllib.error.URLError, OSError):
                    if self.autostart and item["cfg_path"]:
                        self._maybe_revive(item["cfg_path"])
                snapshot.append(item)
            self.events.put(("snapshot", snapshot))
            time.sleep(POLL_SECONDS)

    def _maybe_revive(self, cfg_path):
        if autostart_suppressed(cfg_path):
            return  # deliberately stopped — only an explicit start clears this
        now = time.monotonic()
        if now < self._revive_at.get(cfg_path, 0):
            return
        self._revive_at[cfg_path] = now + REVIVE_COOLDOWN
        print(f"reviving {cfg_display_name(cfg_path)} (registered but down)…")
        threading.Thread(target=self._revive, args=(cfg_path,), daemon=True).start()

    @staticmethod
    def _revive(cfg_path):
        try:
            Client(config_path=cfg_path).ensure_server()
        except SystemExit as err:  # ensure_server raises it on failure; stay alive
            print(f"revive of {cfg_display_name(cfg_path)} failed: {err}")

    # -- Main-thread event pump ----------------------------------------------------

    def _pump(self):
        try:
            while True:
                event = self.events.get_nowait()
                kind, payload = event[0], event[1:]
                if kind == "snapshot":
                    self._on_snapshot(payload[0])
                elif kind == "open":
                    self._open_questions()
                elif kind == "claude":
                    self._open_in_claude(payload[0], payload[1])
                elif kind == "autostart":
                    self._set_autostart(payload[0], payload[1])
                elif kind == "prompt":
                    self._prompt_window().show()
                elif kind == "settings":
                    self._settings_window().show()
                elif kind == "wizard":
                    self._launch_wizard(payload[0], payload[1])
                elif kind == "wizard_done":
                    self._after_wizard(*payload)
                elif kind == "settings_status":
                    if self.settings_win is not None and self.settings_win.alive():
                        self.settings_win.refresh(payload[0])
                elif kind == "detach":
                    return self._shutdown(stop_agents=False)
                elif kind == "quit":
                    return self._shutdown(stop_agents=True)
        except queue.Empty:
            pass
        self.root.after(150, self._pump)

    def _on_snapshot(self, snapshot):
        self.agents = snapshot
        live = any(a["up"] for a in snapshot)
        total = sum(len(a["pending"]) for a in snapshot)
        self.tray.set_state(live, total)
        self.tray.refresh_menu()  # win32 pystray won't rebuild it on right-click
        for agent in snapshot:
            for item in agent["pending"]:
                key = (agent["base"], item.get("negotiation_id"))
                if key in self._toasted:
                    continue
                self._toasted.add(key)
                ask = (item.get("summary") or item.get("question") or item.get("title")
                       or "a decision is waiting")
                who = item.get("from") or item.get("title") or ""
                notify(self.tray, f"Parley · {agent['name']}",
                       f"{who}: {ask}" if who else ask,
                       on_click=lambda: self.events.put(("open",)))
        self._toasted &= {(a["base"], i.get("negotiation_id"))
                          for a in snapshot for i in a["pending"]}
        if self.question_win is not None:
            self.question_win.update_snapshot(snapshot)
        if self.prompt_win is not None:
            self.prompt_win.update_targets(snapshot)

    def _open_questions(self):
        """Clicking a pending question always opens the question window: it is
        instant, and most questions need no more than Accept. A Claude session is
        one button away on each card (`_open_in_claude`)."""
        return self._question_window().show()

    def _open_in_claude(self, base, ref):
        """A card's "Open in Claude": a session on that agent, starting on that
        question. Looked up in the LATEST snapshot, not the one the card was drawn
        from, so the session is told what is actually still waiting. On failure the
        question simply stays in the window — nothing is lost."""
        from parley.client_server import ask_session
        agent = next((a for a in self.agents if a["base"] == base), None)
        pending = (agent or {}).get("pending") or ()
        focus = next((i for i in pending if i.get("negotiation_id") == ref), None)
        if agent is None or focus is None:
            return  # answered meanwhile; its card is on its way out
        ok, detail = ask_session.launch(agent["name"], base, pending, focus=focus)
        if ok:
            print(f"opened a Claude session for {agent['name']} on {ref} ({detail})")
        else:
            print(f"⚠️ could not open Claude: {detail}")
            notify(self.tray, "Parley", f"Could not open Claude: {detail}")

    def _question_window(self):
        if self.question_win is None or not self.question_win.alive():
            self.question_win = QuestionWindow(self.root, self.events)
            self.question_win.update_snapshot(self.agents)
        return self.question_win

    def _prompt_window(self):
        if self.prompt_win is None or not self.prompt_win.alive():
            self.prompt_win = PromptWindow(self.root, self.events)
            self.prompt_win.update_targets(self.agents)
        return self.prompt_win

    def _live_paths(self):
        """cfg paths (normcased) whose daemon answered the poller's last probe."""
        return {os.path.normcase(a["cfg_path"]) for a in self.agents
                if a["up"] and a["cfg_path"]}

    def _settings_window(self):
        if self.settings_win is None or not self.settings_win.alive():
            self.settings_win = SettingsWindow(self.root, self.events, self._live_paths)
        self.settings_win.refresh()
        return self.settings_win

    # -- Settings: the wizards run as their own process -----------------------------

    # kind → the `parley` flags that open that wizard (in its Setup Window).
    _WIZARDS = {"new": ["--setup"], "engine": ["--engine"], "account": ["--add-account"]}

    def _launch_wizard(self, kind, cfg_path):
        """Open one wizard in its own process — the same `parley --setup` /
        `--engine` / `--add-account` the CLI runs, so the tray adds no second
        setup path. PARLEY_SETUP_GUI makes it open the Setup Window even though
        the tray has no console. One wizard at a time: two would both write
        cfgs and both pop browsers."""
        if self._wizard is not None:
            notify(self.tray, "Parley", "A setup window is already open.")
            return
        import subprocess
        argv = [sys.executable, "-m", "parley", *self._WIZARDS[kind]]
        if cfg_path:
            argv += ["-cfg", cfg_path]
        self._wizard = (kind, cfg_path)
        before = _mtime(cfg_path)
        self.events.put(("settings_status", "The setup window is open…"))

        def _wait():
            try:
                code = subprocess.run(
                    argv, env=dict(os.environ, PARLEY_SETUP_GUI="1"),
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).returncode
            except OSError as err:
                print(f"⚠️ could not open the wizard: {err}")
                code = -1
            changed = cfg_path is not None and _mtime(cfg_path) != before
            self.events.put(("wizard_done", kind, cfg_path, code, changed))
        threading.Thread(target=_wait, daemon=True).start()

    def _after_wizard(self, kind, cfg_path, code, changed):
        """A wizard closed. If it changed a RUNNING agent's cfg, restart that agent —
        an agent reads its engine and accounts once, at start, so without this the
        change would silently wait for the next reboot."""
        self._wizard = None
        target = os.path.normcase(os.path.abspath(cfg_path or ""))
        running = next((a for a in self.agents if a.get("up") and a.get("cfg_path")
                        and os.path.normcase(os.path.abspath(a["cfg_path"])) == target),
                       None)
        if kind == "new":
            status = "New agent set up." if code == 0 else "Setup closed - nothing written."
        elif not changed:
            status = "Closed - nothing changed."
        elif running:
            status = f"{running['name']}: restarting with the new settings…"
            threading.Thread(target=self._restart, args=(running,), daemon=True).start()
        else:
            status = "Saved. The agent uses it the next time it starts."
        self.events.put(("settings_status", status))

    def _restart(self, agent):
        """Stop, wait until the port is free, start again. The stop marks a
        deliberate stop (so the poll loop does not race us with a revive); the
        start through ensure_server clears that mark, same as a manual start."""
        name = agent["name"]
        try:
            Client(base=agent["base"]).stop()
        except (urllib.error.URLError, OSError):
            pass
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and Client._server_up(agent["base"]):
            time.sleep(0.5)
        try:
            Client(config_path=agent["cfg_path"]).ensure_server()
            message = f"{name} restarted with the new settings."
        except SystemExit as err:
            message = f"{name} did not come back: {err}"
        print(message)
        notify(self.tray, "Parley", message)
        self.events.put(("settings_status", message))

    def _shutdown(self, stop_agents):
        if stop_agents:
            for agent in self.agents:
                if agent["up"]:
                    try:
                        Client(base=agent["base"]).stop()
                        print(f"stopped {agent['name']}.")
                    except (urllib.error.URLError, OSError):
                        pass
        else:
            print("detached — agents keep running.")
        self.tray.stop()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


# --- Question window (mockup exhibit C) --------------------------------------------

#: A card's header word per question kind (UserMessaging.PendingQuestion.kind).
KIND_LABELS = {"invite": "INVITE", "rsvp": "INVITE", "email": "MAIL",
               "stalled": "NO REPLY", "limit": "STOPPED", "ambiguous": "WHICH MEETING?",
               "permission": "PERMISSION", "calendar": "SETUP", "question": "QUESTION",
               "focus": "FOCUS TIME", "notice": "FYI"}

#: The two buttons per kind: (label, the answer it sends). The text field next to
#: them is always there for anything else ("propose Thursday instead").
ANSWER_BUTTONS = {
    "invite": (("Accept", "accept"), ("Decline", "decline")),
    "rsvp": (("Keep trying", "keep trying"), ("Drop", "drop it")),
    "stalled": (("Nudge", "nudge them again"), ("Drop", "drop it")),
    "limit": (("Keep going", "keep going"), ("Drop", "drop it")),
    "email": (("Engage", "engage with it"), ("Ignore", "ignore it")),
    "focus": (("Keep trying", "keep trying"), ("Drop it", "drop it")),
    "question": (("Accept", "accept"), ("Deny", "deny")),
}


def _short_who(who):
    """A sender for a card header: a name as is; one address, its local part's
    words; an Exchange DN (an internal sender we could not resolve), nothing."""
    who = (who or "").strip()
    if not who or who.upper().startswith("/O="):
        return ""
    first = who.split(",")[0].strip()
    more = " +" + str(who.count(",")) if "," in who else ""
    if "@" in first:
        first = first.split("@")[0].replace(".", " ").replace("_", " ").title()
    return first + more


def _when(iso):
    """'Wed 21 Oct 16:00' in local time, or '' if there is no usable date."""
    if not iso:
        return ""
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).astimezone() \
            .strftime("%a %d %b %H:%M")
    except ValueError:
        return ""


class QuestionWindow:
    """All pending questions, grouped by agent. Accept / Deny / free-text guidance;
    a follow-up ASK renders in place of the answered card (teal spine)."""

    def __init__(self, root, events):
        self.events = events
        self.win = tk.Toplevel(root)
        self.win.title("Parley — questions")
        self.win.configure(bg=C["surface"])
        self.win.geometry("460x520")
        self.win.protocol("WM_DELETE_WINDOW", self.win.withdraw)
        # A long question (title + prose + slot) can be taller than the window —
        # without a scroll path its Accept/Deny row lands below the visible area
        # with no way to reach it (user report 2026-09-05: could not answer a
        # question at all). Canvas + Scrollbar is Tk's standard scrollable-frame
        # recipe: `self.body` lives INSIDE the canvas, not packed straight into
        # the window; the canvas's scrollregion tracks the body's actual size.
        canvas = tk.Canvas(self.win, bg=C["surface"], highlightthickness=0)
        scrollbar = tk.Scrollbar(self.win, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True, padx=4, pady=4)
        self.body = tk.Frame(canvas, bg=C["surface"])
        body_window = canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.body.bind("<Configure>",
                       lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfigure(body_window, width=e.width))

        # bind_all is app-wide, so only wire it up while the pointer is actually
        # over THIS canvas (Enter/Leave) — otherwise scrolling anywhere else in
        # the app (e.g. the prompt window) would hijack this canvas instead.
        def _wheel(event):
            canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")
        canvas.bind("<Enter>", lambda _e: canvas.bind_all("<MouseWheel>", _wheel))
        canvas.bind("<Leave>", lambda _e: canvas.unbind_all("<MouseWheel>"))

        self.footer = tk.Label(
            self.win, bg=C["surface"], fg=C["faint"], font=(UI_FONT[0], 8),
            text="answered here or with parley --watch — first answer wins")
        # Packed BEFORE the scrollbar and canvas: packed after them it got the strip
        # they left on the right, which squeezed every card narrow.
        self.footer.pack(side="bottom", fill="x", pady=(0, 6), before=scrollbar)
        self.sections = {}   # agent base → (frame, header_label)
        self.cards = {}      # (base, ref) → (frame, born_at)
        # Checked once per window, not per card: a PATH lookup for every card
        # redrawn on every poll is waste, and installing Claude mid-session is rare.
        from parley.client_server import ask_session
        self.claude_ok = ask_session.available()

    def alive(self):
        return self.win.winfo_exists()

    def show(self):
        self.win.deiconify()
        self.win.lift()

    # -- Rendering ------------------------------------------------------------------

    def update_snapshot(self, snapshot):
        if not self.alive():
            return
        now = time.monotonic()
        wanted = set()
        for agent in snapshot:
            for item in agent["pending"]:
                key = (agent["base"], item.get("negotiation_id"))
                wanted.add(key)
                if key not in self.cards:
                    self._add_card(agent, item, chain=False)
        for key, (frame, born) in list(self.cards.items()):
            if key not in wanted and now - born > CARD_GRACE:
                frame.destroy()
                del self.cards[key]
        self._prune_sections()

    def _section(self, agent):
        if agent["base"] not in self.sections:
            frame = tk.Frame(self.body, bg=C["surface"])
            frame.pack(fill="x", anchor="n")
            header = tk.Label(frame, text=f"{agent['name'].upper()}",
                              bg=C["surface"], fg=C["faint"], font=(UI_FONT[0], 8, "bold"),
                              anchor="w")
            header.pack(fill="x", padx=12, pady=(8, 0))
            self.sections[agent["base"]] = frame
        return self.sections[agent["base"]]

    def _prune_sections(self):
        for base, frame in list(self.sections.items()):
            if not any(k[0] == base for k in self.cards):
                frame.destroy()
                del self.sections[base]

    def _add_card(self, agent, item, chain):
        ref = item.get("negotiation_id")
        section = self._section(agent)
        card = tk.Frame(section, bg=C["raised"], bd=0,
                        highlightbackground=C["accent"] if chain else C["border"],
                        highlightthickness=2 if chain else 1)
        card.pack(fill="x", padx=12, pady=6)
        # Compact first, detail on demand (2026-10-18): KIND · who · when, the title,
        # ONE line of summary. The full question — often a quoted mail, hundreds of
        # characters — sits folded under "More detail".
        kind = item.get("kind") or "question"
        head = " · ".join(x for x in (KIND_LABELS.get(kind, kind.upper()),
                                      _short_who(item.get("from")),
                                      _when(item.get("start"))) if x)
        tk.Label(card, text=head if not chain else f"FOLLOW-UP · {head}",
                 bg=C["raised"], fg=C["muted"], font=(UI_FONT[0], 8, "bold"),
                 anchor="w").pack(fill="x", padx=12, pady=(8, 0))
        if item.get("title"):
            tk.Label(card, text=item["title"], bg=C["raised"], fg=C["text"],
                     font=(UI_FONT[0], 10, "bold"), anchor="w", wraplength=380,
                     justify="left").pack(fill="x", padx=12, pady=(2, 0))
        full = item.get("question") or ""
        summary = item.get("summary") or full or item.get("title") or ""
        tk.Label(card, text=summary, bg=C["raised"], fg=C["text"], font=UI_FONT,
                 anchor="w", wraplength=380, justify="left").pack(fill="x", padx=12,
                                                                  pady=(2, 0))
        if full and full.strip() != summary.strip():
            detail = tk.Label(card, text=full, bg=C["inset"], fg=C["muted"],
                              font=(UI_FONT[0], 9), anchor="w", wraplength=370,
                              justify="left", padx=6, pady=4)
            toggle = tk.Label(card, text="More detail ▸", bg=C["raised"], fg=C["accent"],
                              font=(UI_FONT[0], 8), cursor="hand2", anchor="w")
            toggle.pack(fill="x", padx=12, pady=(2, 0))

            def _flip(_e=None, detail=detail, toggle=toggle):
                if detail.winfo_ismapped():
                    detail.pack_forget()
                    toggle.configure(text="More detail ▸")
                else:
                    detail.pack(fill="x", padx=12, pady=(2, 0), after=toggle)
                    toggle.configure(text="Less detail ▾")
            toggle.bind("<Button-1>", _flip)
        row = tk.Frame(card, bg=C["raised"])
        row.pack(fill="x", padx=12, pady=(8, 10))
        # Text field first, Accept/Deny after — NOT "Accept / Deny / Text": that order
        # reads as three parallel choices, with the free-text field looking tied to
        # Deny (user report 2026-09-05). A placeholder makes its purpose legible before
        # anyone types, since Tk has no native placeholder support.
        _PLACEHOLDER = "Write any observation…"
        entry = tk.Entry(row, bg=C["inset"], fg=C["faint"], insertbackground=C["accent"],
                         relief="flat", font=UI_FONT)
        entry.insert(0, _PLACEHOLDER)
        placeholder = {"active": True}

        def _focus_in(_e):
            if placeholder["active"]:
                entry.delete(0, "end")
                entry.configure(fg=C["text"])
                placeholder["active"] = False

        def _focus_out(_e):
            if not entry.get():
                entry.insert(0, _PLACEHOLDER)
                entry.configure(fg=C["faint"])
                placeholder["active"] = True

        entry.bind("<FocusIn>", _focus_in)
        entry.bind("<FocusOut>", _focus_out)

        def answer(choice=None):
            typed = "" if placeholder["active"] else entry.get().strip()
            reply = choice or typed
            if not reply:
                return
            for child in row.winfo_children():
                child.configure(state="disabled")
            threading.Thread(target=self._decide, args=(agent, ref, reply),
                             daemon=True).start()

        if kind == "notice":
            # Nothing to decide: one button, and it only closes the card.
            row.destroy()
            tk.Button(card, text="OK", command=lambda: threading.Thread(
                          target=self._dismiss, args=(agent, ref), daemon=True).start(),
                      bg=C["accent"], fg="#0c1613", activebackground="#5ad0b3",
                      relief="flat", font=(UI_FONT[0], 9, "bold"), padx=14).pack(
                          anchor="e", padx=12, pady=(8, 10))
            self.cards[(agent["base"], ref)] = (card, time.monotonic())
            return
        entry.pack(side="left", fill="x", expand=True, ipady=3)
        (yes_label, yes_text), (no_label, no_text) = ANSWER_BUTTONS.get(
            kind, ANSWER_BUTTONS["question"])
        tk.Button(row, text=yes_label, command=lambda: answer(yes_text),
                  bg=C["accent"], fg="#0c1613", activebackground="#5ad0b3",
                  relief="flat", font=(UI_FONT[0], 9, "bold"), padx=10).pack(
                      side="left", padx=(6, 6))
        tk.Button(row, text=no_label, command=lambda: answer(no_text),
                  bg=C["raised"], fg=C["red"], activebackground=C["inset"],
                  relief="flat", font=(UI_FONT[0], 9), padx=10).pack(side="left")
        entry.bind("<Return>", lambda _e: answer())
        # The quiet line under the answers: neither is an answer. "Dismiss" closes the
        # question without waking the agent (handled elsewhere / no longer matters);
        # "Open in Claude" talks it through first. The card stays until one lands.
        links = tk.Frame(card, bg=C["raised"])
        links.pack(fill="x", padx=12, pady=(0, 8))

        def dismiss():
            for child in row.winfo_children() + links.winfo_children():
                child.configure(state="disabled")
            threading.Thread(target=self._dismiss, args=(agent, ref), daemon=True).start()

        if ref:
            tk.Button(links, text="Dismiss", command=dismiss,
                      bg=C["raised"], fg=C["faint"], activebackground=C["inset"],
                      relief="flat", font=(UI_FONT[0], 8), padx=4, cursor="hand2").pack(
                          side="left")
        if self.claude_ok and ref:
            tk.Button(links, text="Open in Claude ›",
                      command=lambda: self.events.put(("claude", agent["base"], ref)),
                      bg=C["raised"], fg=C["muted"], activebackground=C["inset"],
                      relief="flat", font=(UI_FONT[0], 8), padx=4, cursor="hand2").pack(
                          side="right")
        self.cards[(agent["base"], ref)] = (card, time.monotonic())

    # -- Answering (worker thread → back to tk via `after`) --------------------------

    def _decide(self, agent, ref, choice):
        try:
            result = Client(base=agent["base"]).decide(ref, choice)
        except (urllib.error.URLError, OSError) as err:
            result = {"status": "done", "accepted": False,
                      "message": f"couldn't send the decision: {err}"}
        self.win.after(0, lambda: self._on_decided(agent, ref, result))

    def _dismiss(self, agent, ref):
        try:
            result = Client(base=agent["base"]).dismiss(ref, "dismissed in the tray")
        except (urllib.error.URLError, OSError) as err:
            result = {"status": "done", "accepted": False,
                      "message": f"couldn't dismiss it: {err}"}
        self.win.after(0, lambda: self._on_decided(agent, ref, result))

    def _on_decided(self, agent, ref, result):
        if not self.alive():
            return
        key = (agent["base"], ref)
        if key in self.cards:
            self.cards[key][0].destroy()
            del self.cards[key]
        if result.get("status") == "question":
            self._add_card(agent, {"negotiation_id": result.get("ref") or result.get("negotiation_id"),
                                   "question": result.get("question") or result.get("message"),
                                   "summary": result.get("summary"),
                                   "kind": result.get("kind"), "from": result.get("from"),
                                   "start": result.get("start"),
                                   "title": result.get("title", "")}, chain=True)
        else:
            self.footer.configure(
                text=("✅ " if result.get("accepted", True) else "🛑 ")
                     + str(result.get("message", ""))[:80])
        self._prune_sections()


# --- Settings window ----------------------------------------------------------------

def _mtime(path):
    try:
        return os.path.getmtime(path) if path else None
    except OSError:
        return None


class SettingsWindow:
    """One card per agent: engine, models, connections, and the buttons that open
    the wizards for it (Engine…, Add connection) — plus New agent. The window only
    SHOWS and LAUNCHES; every change goes through the same live-verified wizard the
    CLI runs, so nothing unverified is ever written from here."""

    def __init__(self, root, events, live_paths):
        self.events = events
        self.live_paths = live_paths  # same live probe as the Agents menu
        self.win = tk.Toplevel(root)
        self.win.title("Parley — settings")
        self.win.configure(bg=C["surface"])
        self.win.geometry("500x560")
        self.win.protocol("WM_DELETE_WINDOW", self.win.withdraw)

        top = tk.Frame(self.win, bg=C["surface"])
        top.pack(fill="x", padx=12, pady=(10, 0))
        tk.Label(top, text="Agents", bg=C["surface"], fg=C["text"],
                 font=(UI_FONT[0], 12, "bold"), anchor="w").pack(side="left")
        tk.Button(top, text="New agent", command=lambda: self._open("new", None),
                  bg=C["accent"], fg="#0c1613", activebackground="#5ad0b3",
                  relief="flat", font=(UI_FONT[0], 9, "bold"), padx=10).pack(side="right")

        # Same scrollable-frame recipe as the question window: many agents with
        # several connections each are taller than the window.
        canvas = tk.Canvas(self.win, bg=C["surface"], highlightthickness=0)
        scrollbar = tk.Scrollbar(self.win, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        self.status = tk.Label(self.win, bg=C["surface"], fg=C["muted"],
                               font=(UI_FONT[0], 9), anchor="w", wraplength=460,
                               justify="left", text="")
        self.status.pack(side="bottom", fill="x", padx=12, pady=(0, 8))
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True, padx=4, pady=4)
        self.body = tk.Frame(canvas, bg=C["surface"])
        body_window = canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.body.bind("<Configure>",
                       lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfigure(body_window, width=e.width))

        def _wheel(event):
            canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")
        canvas.bind("<Enter>", lambda _e: canvas.bind_all("<MouseWheel>", _wheel))
        canvas.bind("<Leave>", lambda _e: canvas.unbind_all("<MouseWheel>"))

    def alive(self):
        return self.win.winfo_exists()

    def show(self):
        self.win.deiconify()
        self.win.lift()

    def _open(self, kind, cfg_path):
        self.events.put(("wizard", kind, cfg_path))

    def refresh(self, status=None):
        """Rebuild every card from the cfgs on disk (cheap: a few small YAML reads)."""
        if not self.alive():
            return
        if status is not None:
            self.status.configure(text=status)
        for child in self.body.winfo_children():
            child.destroy()
        agents = agent_overview()
        live = self.live_paths()
        if not agents:
            tk.Label(self.body, text="No agents yet - press New agent.", bg=C["surface"],
                     fg=C["muted"], font=UI_FONT, anchor="w").pack(fill="x", padx=12, pady=12)
        for agent in agents:
            self._card(agent, os.path.normcase(agent["cfg_path"]) in live)

    def _card(self, agent, running):
        info = agent_settings(agent["cfg_path"])
        card = tk.Frame(self.body, bg=C["raised"], highlightbackground=C["border"],
                        highlightthickness=1)
        card.pack(fill="x", padx=12, pady=6)
        state = "running" if running else "not running"
        tk.Label(card, text=f"{agent['name'].upper()}  ·  {state}", bg=C["raised"],
                 fg=C["accent"] if running else C["faint"],
                 font=(UI_FONT[0], 9, "bold"), anchor="w").pack(fill="x", padx=12, pady=(8, 2))

        def line(label, value, color=C["text"]):
            row = tk.Frame(card, bg=C["raised"])
            row.pack(fill="x", padx=12)
            tk.Label(row, text=label, width=12, bg=C["raised"], fg=C["muted"],
                     font=(UI_FONT[0], 9), anchor="w").pack(side="left")
            tk.Label(row, text=value, bg=C["raised"], fg=color, font=(UI_FONT[0], 9),
                     anchor="w", justify="left", wraplength=330).pack(side="left", fill="x")

        if "error" in info:
            line("Config", f"cannot read it: {info['error']}", C["red"])
        else:
            line("Engine", info["engine"])
            line("Brain model", info["model"])
            line("Gateway", info["gateway"])
            line("Connections", "\n".join(info["connections"]) or "(none yet)",
                 C["text"] if info["connections"] else C["amber"])
        row = tk.Frame(card, bg=C["raised"])
        row.pack(fill="x", padx=12, pady=(8, 10))
        for text, kind in (("Engine…", "engine"), ("Add connection", "account")):
            tk.Button(row, text=text, command=lambda k=kind: self._open(k, agent["cfg_path"]),
                      bg=C["inset"], fg=C["text"], activebackground=C["border"],
                      relief="flat", font=(UI_FONT[0], 9), padx=10).pack(side="left", padx=(0, 6))


# --- Hotkey prompt (mockup exhibit D) -----------------------------------------------

class PromptWindow:
    """Borderless floating prompt. Enter sends to the target agent (default cfg
    unless `@name ` retargets this message, D11); Tab in an empty prompt cycles the
    target. END replies render below; an ASK keeps the entry as the answer line."""

    def __init__(self, root, events):
        self.events = events
        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.configure(bg=C["border"])
        inner = tk.Frame(self.win, bg=C["surface"])
        inner.pack(fill="both", expand=True, padx=1, pady=1)

        line = tk.Frame(inner, bg=C["surface"])
        line.pack(fill="x", padx=14, pady=(12, 4))
        tk.Label(line, text="⛵ ›", bg=C["surface"], fg=C["accent"],
                 font=(MONO_FONT[0], 12, "bold")).pack(side="left", anchor="n")
        # A Text, not an Entry: long instructions word-wrap and the window grows
        # (an Entry just scrolls off to the right). Enter sends; Shift+Enter = newline.
        self.entry = tk.Text(line, bg=C["surface"], fg=C["text"], relief="flat",
                             insertbackground=C["accent"], font=(MONO_FONT[0], 12),
                             height=1, wrap="word", padx=0, pady=2,
                             highlightthickness=0)
        self.entry.pack(side="left", fill="x", expand=True, padx=(8, 0))

        # The transcript: a read-only Text so the conversation accumulates (you never
        # lose what you already read), wraps, grows with its content and scrolls
        # past ~14 display lines.
        self.reply = tk.Text(inner, bg=C["inset"], fg=C["muted"], font=UI_FONT,
                             wrap="word", relief="flat", height=2, width=56,
                             padx=10, pady=8, highlightthickness=0,
                             state="disabled", cursor="arrow")
        self.reply.tag_configure("you", foreground=C["faint"])
        self.reply.tag_configure("bot", foreground=C["text"])
        self.reply.tag_configure("meta", foreground=C["faint"])
        self.reply.tag_configure("err", foreground=C["red"])
        hints = tk.Frame(inner, bg=C["surface"])
        hints.pack(fill="x", side="bottom", padx=14, pady=(4, 10))
        tk.Label(hints, text="Enter send · Esc dismiss · Tab/click agent · @name for one msg",
                 bg=C["surface"], fg=C["faint"], font=(UI_FONT[0], 8)).pack(side="left")
        # The target badge is a button: click it to pick who you're talking to (or set
        # the default), the visible counterpart of Tab-cycling and the `@name` prefix.
        self.badge = tk.Label(hints, text="", bg=C["surface"], fg=C["faint"],
                              font=(UI_FONT[0], 8), cursor="hand2")
        self.badge.pack(side="right")
        self.badge.bind("<Button-1>", self._open_target_menu)

        self.entry.bind("<Return>", self._send)
        self.entry.bind("<Shift-Return>", lambda _e: None)  # newline passes through
        self.entry.bind("<Tab>", self._cycle)
        self.entry.bind("<KeyRelease>", self._autosize)
        self.win.bind("<Escape>", lambda _e: self.hide())
        self.win.withdraw()

        self.targets = []           # [(name, base, cfg_path, up)]
        self.target_ix = 0
        self.ask_ref = None         # set while chaining an ASK
        self.ask_base = None
        self._busy = False

    def alive(self):
        return self.win.winfo_exists()

    # -- Targets (default cfg first, then the other running agents) -----------------

    def update_targets(self, snapshot):
        default = default_config()
        default_name = cfg_display_name(default) if default else None
        targets = []
        if default:
            up = any(a["cfg_path"] == os.path.abspath(default) and a["up"] for a in snapshot)
            targets.append((f"{default_name} (default)", None, default, up))
        for a in snapshot:
            if default and a["cfg_path"] == os.path.abspath(default):
                continue
            targets.append((a["name"], a["base"], a["cfg_path"], a["up"]))
        self.targets = targets or [("no config — run: parley -cfg NAME --default", None, None, False)]
        self.target_ix = min(self.target_ix, len(self.targets) - 1)
        self._show_target()

    def _show_target(self):
        name, _base, _cfg, up = self.targets[self.target_ix]
        self.badge.configure(text=f"{name} {'●' if up else '○'}",
                             fg=C["accent"] if up else C["faint"])

    def _text(self):
        return self.entry.get("1.0", "end-1c").strip()

    def _clear(self):
        self.entry.delete("1.0", "end")
        self._autosize()

    def _autosize(self, _event=None):
        """Grow the input with its wrapped content (1..4 display lines)."""
        try:
            lines = self.entry.count("1.0", "end-1c", "displaylines")
            lines = int(lines[0] if isinstance(lines, tuple) else (lines or 1))
        except tk.TclError:
            lines = 1
        self.entry.configure(height=max(1, min(lines, 4)))
        self.win.geometry("")  # re-fit the window to its content

    def _cycle(self, _event):
        if not self._text():
            self.target_ix = (self.target_ix + 1) % len(self.targets)
            self._show_target()
        return "break"  # keep focus in the entry either way

    def _open_target_menu(self, event=None):
        """Click the badge → pick the agent to talk to (the visible form of Tab /
        `@name`), or set the current one as the default agent."""
        menu = tk.Menu(self.win, tearoff=0, bg=C["raised"], fg=C["text"],
                       activebackground=C["accent"], activeforeground="#0c1613",
                       bd=0, font=(UI_FONT[0], 9))
        for ix, (name, _base, _cfg, up) in enumerate(self.targets):
            mark = "● " if up else "○ "
            menu.add_command(label=mark + name,
                             command=lambda i=ix: self._pick_target(i))
        name, _base, cfg_path, _up = self.targets[self.target_ix]
        if cfg_path:
            menu.add_separator()
            menu.add_command(label=f"Set '{name.split(' ')[0]}' as default",
                             command=lambda p=cfg_path: self._make_default(p))
        try:
            menu.tk_popup(event.x_root, event.y_root) if event else menu.tk_popup(0, 0)
        finally:
            menu.grab_release()

    def _pick_target(self, ix):
        self.target_ix = ix
        self._show_target()
        self.entry.focus_force()

    def _make_default(self, cfg_path):
        """Write cfg_path as the default agent; the next snapshot relabels the badges."""
        try:
            set_default(cfg_path)
            self.badge.configure(text=f"default → {cfg_display_name(cfg_path)}", fg=C["accent"])
        except OSError as err:
            self._append(f"couldn't set default: {err}", "err")

    # -- Show / hide -----------------------------------------------------------------

    def show(self):
        # Anchor top-center; height is whatever the content needs (the transcript
        # grows downward as replies arrive).
        self.win.update_idletasks()
        x = (self.win.winfo_screenwidth() - max(self.win.winfo_reqwidth(), 540)) // 2
        y = self.win.winfo_screenheight() // 4
        self.win.geometry(f"+{x}+{y}")
        self.win.deiconify()
        self.win.lift()
        self.win.attributes("-topmost", True)
        self.entry.focus_force()

    def hide(self):
        self.ask_ref = self.ask_base = None
        self._clear()
        self._reset_transcript()
        self.win.withdraw()

    def _reset_transcript(self):
        self.reply.configure(state="normal")
        self.reply.delete("1.0", "end")
        self.reply.configure(state="disabled", height=2)
        self.reply.pack_forget()

    # -- Sending ----------------------------------------------------------------------

    def _send(self, _event):
        if self._busy:
            return "break"
        text = self._text()
        if not text:
            return "break"
        if self.ask_ref:  # answering a chained ASK
            self._append(f"› {text}", "you")
            self._dispatch(self.ask_base, None, ("decide", self.ask_ref, text))
            return "break"
        base, cfg_path = None, None
        if text.startswith("@"):
            name, _, rest = text[1:].partition(" ")
            match = [t for t in self.targets if t[0].split(" ")[0] == name]
            if not match or not rest.strip():
                self._append(f"no running agent called @{name} — Tab cycles the "
                             "known ones", "err")
                return "break"
            _n, base, cfg_path, _up = match[0]
            text = rest.strip()
        else:
            _n, base, cfg_path, _up = self.targets[self.target_ix]
        if base is None and cfg_path is None:
            self._append("no config yet — run: parley -cfg NAME --default", "err")
            return "break"
        self._append(f"› {text}", "you")
        self._dispatch(base, cfg_path, ("instruct", text))
        return "break"

    def _dispatch(self, base, cfg_path, action):
        self._busy = True
        self._clear()
        self.badge.configure(text="thinking…" if base else "starting the agent…",
                             fg=C["amber"])
        threading.Thread(target=self._call, args=(base, cfg_path, action),
                         daemon=True).start()

    def _call(self, base, cfg_path, action):
        try:
            cli = Client(base=base) if base else Client(config_path=cfg_path)
            if base is None:
                cli.ensure_server()   # spawn the default's daemon if it's down
                base = cli.base
            if action[0] == "instruct":
                result = cli.instruct(action[1])
            else:
                result = cli.decide(action[1], action[2])
        except (urllib.error.URLError, OSError, SystemExit) as err:
            result = {"status": "done", "accepted": False, "message": str(err)}
        self.win.after(0, lambda: self._on_result(base, result))

    def _on_result(self, base, result):
        self._busy = False
        if not self.alive():
            return
        self._show_target()  # restore the target badge after "thinking…"
        if result.get("status") == "question":
            self.ask_ref = result.get("ref") or result.get("negotiation_id")
            self.ask_base = base
            self._append("⛵ " + str(result.get("question") or result.get("message")), "bot")
            self._append("(type your answer above — ACCEPT / DENY / any guidance)", "meta")
        else:
            self.ask_ref = self.ask_base = None
            flag = "✅" if result.get("accepted", True) else "🛑"
            self._append(f"{flag} {result.get('message', '')}", "bot")
            self._append("(Esc closes — the negotiation keeps running)", "meta")

    def _append(self, text, tag="bot"):
        """Add one line to the transcript; grow the window with it (scroll past 14
        display lines) and keep the newest line in view."""
        self.reply.configure(state="normal")
        if self.reply.get("1.0", "end-1c"):
            self.reply.insert("end", "\n")
        self.reply.insert("end", text, tag)
        self.reply.configure(state="disabled")
        if not self.reply.winfo_ismapped():
            self.reply.pack(fill="x", padx=10, pady=(2, 4), before=self.badge.master)
        try:
            lines = self.reply.count("1.0", "end-1c", "displaylines")
            lines = int(lines[0] if isinstance(lines, tuple) else (lines or 1))
        except tk.TclError:
            lines = 2
        self.reply.configure(height=max(2, min(lines + 1, 14)))
        self.reply.see("end")
        self.win.geometry("")  # re-fit the window to the grown transcript


# --- Single instance + `parley_desktop --stop` ---------------------------------------
#
# One tray per user: the running instance records its pid in ~/.parley/desktop.pid
# (removed on clean exit). `--stop` terminates that instance — agents keep running,
# same as the tray's *Detach* (stop them with `parley --stop`).

def _pid_file():
    return os.path.join(PARLEY_HOME, "desktop.pid")


def _running_pid():
    """Pid of a live tray instance, else None (a stale file reads as None).

    Windows: OpenProcess alone is NOT proof of life — a process that has already
    exited stays openable while anything still holds a handle on it (a parent, a
    Task Manager / Get-Process query), so the old check reported a dead tray as
    running and the next launch refused to start, telling the user to `--stop`
    something that no longer existed (2026-09-06: an unclean kill left the user
    with no tray and no way back). GetExitCodeProcess settles it: STILL_ACTIVE
    means running, any other code means it is gone.
    """
    try:
        with open(_pid_file(), encoding="utf-8") as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        return None
    if sys.platform == "win32":
        still_active = 259  # STILL_ACTIVE
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED
        if not handle:
            return None
        try:
            code = ctypes.c_ulong()
            ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
        # Can't read the code → assume alive; refusing to start is safer than two trays.
        return pid if (not ok or code.value == still_active) else None
    try:
        os.kill(pid, 0)
        return pid
    except OSError:
        return None


def _write_pid_file():
    os.makedirs(PARLEY_HOME, exist_ok=True)
    with open(_pid_file(), "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))
    atexit.register(_remove_pid_file)


def _remove_pid_file():
    try:
        os.remove(_pid_file())
    except OSError:
        pass


def _stop_running():
    pid = _running_pid()
    if pid is None:
        _remove_pid_file()  # clear a stale file if any
        print("no Parley Desktop instance is running.")
        return 0
    if sys.platform == "win32":
        handle = ctypes.windll.kernel32.OpenProcess(0x0001, False, pid)  # TERMINATE
        if handle:
            ctypes.windll.kernel32.TerminateProcess(handle, 0)
            ctypes.windll.kernel32.CloseHandle(handle)
    else:
        os.kill(pid, signal.SIGTERM)
    _remove_pid_file()
    print(f"Parley Desktop stopped (pid {pid}). Agents keep running — "
          "stop one with: parley --stop [-cfg NAME]")
    return 0


# --- Entry ---------------------------------------------------------------------------

def _attach_parent_console():
    """This is a GUI script (pythonw.exe) — it has no console, so --help, --stop
    and any error print into the void. When we were launched FROM a terminal
    (`parley_desktop --help`) the parent owns a console; attach to it so the
    output actually shows. A no-op when there's no parent console (the Startup
    shortcut, `parley --desktop`, an already-attached `python -m …`), so it never
    pops a stray console window."""
    if sys.platform != "win32":
        return
    ATTACH_PARENT_PROCESS = -1  # DWORD (DWORD)-1
    if ctypes.windll.kernel32.AttachConsole(ATTACH_PARENT_PROCESS):
        try:
            sys.stdout = open("CONOUT$", "w", encoding="utf-8")
            sys.stderr = open("CONOUT$", "w", encoding="utf-8")
        except OSError:
            pass


def main(argv=None):
    _attach_parent_console()  # make --help / --stop / errors visible from a terminal
    parser = argparse.ArgumentParser(
        prog="parley_desktop",
        description="Parley tray app — icon, toasts, question window, Win-Alt-P prompt.")
    parser.add_argument("-cfg", dest="cfgs", action="append", default=[],
                        help="also watch this config (name/path); repeatable. "
                             "Without it, the servers registry drives everything.")
    parser.add_argument("--no-autostart", action="store_true",
                        help="dev mode: only watch, never revive registered-but-down agents")
    parser.add_argument("--hotkey", help='override the prompt hotkey (default "win+alt+p" '
                                         "or [desktop] hotkey in the default cfg)")
    parser.add_argument("--stop", action="store_true",
                        help="stop the running tray instance (agents keep running)")
    args = parser.parse_args(argv)
    if args.stop:
        return _stop_running()
    existing = _running_pid()
    if existing:
        print(f"Parley Desktop is already running (pid {existing}) — "
              "one tray is enough. Stop it with: parley_desktop --stop")
        print(f"(If no tray is actually running, delete {_pid_file()} and retry.)")
        return 1
    _write_pid_file()
    extra = [resolve_config(c) for c in args.cfgs]
    app = ParleyDesktop(extra_cfgs=extra, autostart=not args.no_autostart,
                        hotkey_spec=args.hotkey)
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
