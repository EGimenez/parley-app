"""Windowless GUI installer for Parley (iteration 26 A2).

Launched by install-windows-gui.vbs under pythonw, so the user NEVER sees a
black console - not during install, not behind the setup wizard. It only
collects the install choices as checkboxes, then runs install-windows.ps1
HIDDEN and non-interactively (-NonInteractive + switches), streaming its
output into this window. The ps1 keeps all the real logic (pip, PATH,
shortcuts, launching the setup wizard under pythonw).

Requires only the standard library (tkinter + subprocess) - it does NOT import
parley, so it runs from a fresh checkout before anything is installed.

The classic scripts/util/install-windows.bat stays as a fully-working fallback for
machines where a .vbs double-click is blocked by policy.
"""

import os
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import scrolledtext

# This file lives in scripts/util/; the repo root (pyproject.toml) is two up.
HERE = os.path.dirname(os.path.abspath(__file__))       # scripts/util
REPO = os.path.dirname(os.path.dirname(HERE))           # repo root
PS1 = os.path.join(HERE, "install-windows.ps1")         # sibling in scripts/util
# Same file the installed app uses (src/parley/client_server/assets/parley.ico) —
# referenced by raw path, never imported, since parley isn't installed yet here.
ICO = os.path.join(REPO, "src", "parley", "client_server", "assets", "parley.ico")

_BG = "#1c2733"
_FG = "#e8edf2"
_ACCENT = "#4ea1d3"
_MUTED = "#9db2c4"
_FONT = ("Segoe UI", 10)
_FONT_BOLD = ("Segoe UI", 10, "bold")
_FONT_TITLE = ("Segoe UI", 14, "bold")

# (attribute, label, default-on, powershell-switch-when-TRUE, switch-when-FALSE)
# NOTE: "configure" carries NO ps1 switch — the wizard is run in THIS window
# after the install (one window, not a second --setup process).
# NOTE: "traynow" carries NO ps1 switch either — the ps1 would start the tray
# BEFORE the wizard runs here, so it would come up EMPTY (the tray seeds only
# agents pinned in autostart.json, and the wizard pins the new one). We start it
# HERE instead, AFTER setup, so it seeds the just-configured agent (user report
# 2026-07-21).
_OPTIONS = [
    ("configure", "Configure my first agent now (LLM key + one account)", True, None, None),
    ("desktop", "Install the desktop tray app", True, None, "-NoDesktop"),
    ("traynow", "Start the tray now", True, None, None),
    ("autostart", "Start the tray at login", True, "-Autostart", None),
]


class Installer:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Install Parley")
        try:
            self.root.iconbitmap(ICO)
        except Exception:  # noqa: BLE001 — cosmetic only, stock icon still works
            pass
        self.root.configure(bg=_BG)
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        w, h = 620, 560
        self.root.geometry(f"{w}x{h}+{(sw - w) // 2}+{(sh - h) // 3}")

        tk.Label(self.root, text="⛵ Install Parley", font=_FONT_TITLE,
                 bg=_BG, fg=_FG).pack(anchor="w", padx=16, pady=(14, 2))
        tk.Label(self.root, text=f"Python {sys.version_info.major}.{sys.version_info.minor}"
                 "  ·  installs the parley CLI + tray app, then verifies everything.",
                 font=_FONT, bg=_BG, fg=_MUTED).pack(anchor="w", padx=16)

        opts = tk.Frame(self.root, bg=_BG)
        opts.pack(fill="x", padx=16, pady=10)
        self._vars = {}
        for attr, label, default, _on, _off in _OPTIONS:
            var = tk.BooleanVar(value=default)
            self._vars[attr] = var
            tk.Checkbutton(opts, text=label, variable=var, font=_FONT, bg=_BG,
                           fg=_FG, selectcolor="#141d26", activebackground=_BG,
                           activeforeground=_FG, anchor="w").pack(fill="x")

        self._log = scrolledtext.ScrolledText(
            self.root, wrap="word", font=("Consolas", 9), height=14, relief="flat",
            bg="#141d26", fg=_FG, state="disabled")
        self._log.pack(fill="both", expand=True, padx=16, pady=(4, 8))

        row = tk.Frame(self.root, bg=_BG)
        row.pack(fill="x", padx=16, pady=(0, 14))
        self._btn = tk.Button(row, text="Install ▶", font=_FONT_BOLD, bg=_ACCENT,
                              fg="#0b1218", relief="flat", padx=18, pady=5,
                              cursor="hand2", command=self._start)
        self._btn.pack(side="left")
        self._close = tk.Button(row, text="Close", font=_FONT, bg="#2b3a4a", fg=_FG,
                                relief="flat", padx=14, pady=5, cursor="hand2",
                                command=self.root.destroy)
        self._close.pack(side="right")

    def _append(self, text):
        # The wizard adopts this window (SetupWindow(root=...) destroys our
        # original widgets, including self._log), so any _append AFTER setup — e.g.
        # from _start_tray in the done callback — would raise TclError on a dead
        # widget. Swallow it so it can never block the window from closing.
        try:
            self._log.configure(state="normal")
            self._log.insert("end", text)
            self._log.see("end")
            self._log.configure(state="disabled")
        except tk.TclError:
            pass

    def _ui(self, fn):
        try:
            self.root.after(0, fn)
        except Exception:
            pass

    def _flags(self):
        flags = ["-NonInteractive"]
        for attr, _label, _default, on, off in _OPTIONS:
            picked = self._vars[attr].get()
            if picked and on:
                flags.append(on)
            if (not picked) and off:
                flags.append(off)
        return flags

    def _start(self):
        self._btn.configure(state="disabled", text="Installing...")
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
               "-File", PS1] + self._flags()
        self._ui(lambda: self._append("> " + " ".join(cmd[5:]) + "\n\n"))
        no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True,
                                    bufsize=1, creationflags=no_window)
        except Exception as exc:
            self._ui(lambda: self._done(False, f"Could not start PowerShell: {exc}"))
            return
        for line in proc.stdout:
            self._ui(lambda ln=line: self._append(ln))
        proc.wait()
        self._ui(lambda: self._after_install(proc.returncode == 0))

    def _after_install(self, ok):
        if not ok:
            self._append("\nInstall did not finish cleanly - see the messages above, "
                         "or run scripts\\util\\install-windows.bat.\n")
            self._btn.configure(text="Retry ▶", state="normal")
            return
        if not self._vars["configure"].get():
            self._start_tray()  # honours the checkbox; seeds any pre-existing agents
            self._append("\nDone. You can close this window.\n")
            self._btn.configure(text="Done", state="disabled")
            return
        # One window: turn THIS window into the setup wizard instead of spawning
        # a second --setup process.
        self._append("\nInstall complete - opening setup in this window...\n")
        self.root.after(600, self._launch_wizard)

    def _start_tray(self):
        """Launch Parley Desktop as a detached process — AFTER the wizard has run
        and pinned the new agent to autostart.json, so the tray seeds it right
        away instead of coming up empty (the ps1 used to start it before setup;
        user report 2026-07-21). Honours the 'Start the tray now' checkbox and
        prefers the pythonw-based parley_desktop.exe so no console flashes."""
        if not self._vars["traynow"].get():
            return
        import sysconfig
        no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        exe = None
        for d in (sysconfig.get_path("scripts"), sysconfig.get_path("scripts", "nt_user")):
            cand = os.path.join(d, "parley_desktop.exe") if d else None
            if cand and os.path.exists(cand):
                exe = cand
                break
        try:
            if exe:
                subprocess.Popen([exe], creationflags=no_window)
            else:
                pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
                if not os.path.exists(pythonw):
                    pythonw = sys.executable
                subprocess.Popen([pythonw, "-m", "parley.client_server.parley_desktop"],
                                 creationflags=no_window)
            self._append("\nParley Desktop started - look for the sail icon in the "
                         "system tray.\n")
        except Exception as exc:
            self._append(f"\nCould not start the tray automatically ({exc}) - open it "
                         "from the Start Menu (press Win, type 'parley').\n")

    def _launch_wizard(self):
        import importlib
        import site
        importlib.invalidate_caches()  # the pip step just added packages
        # .pth files are only read at interpreter startup, and this process started
        # BEFORE pip ran. pywin32 relies on its pywin32.pth to put win32/lib (where
        # pywintypes lives) on sys.path and its DLLs on the search path - without
        # re-reading it, win32com fails here with "No module named 'pywintypes'".
        # addsitedir re-processes every .pth in the dir (and adds a user site that
        # pip's --user fallback may have only just created).
        for d in site.getsitepackages() + [site.getusersitepackages()]:
            if os.path.isdir(d):
                site.addsitedir(d)
        src = os.path.join(REPO, "src")
        if src not in sys.path:
            sys.path.insert(0, src)
        os.environ["PARLEY_SETUP_GUI"] = "1"
        try:
            from parley.install_config import install_config, setup_window
        except Exception as exc:
            self._append(f"\nCould not open setup automatically ({exc}).\n"
                         "Open a terminal and run:  parley --setup\n")
            self._btn.configure(text="Done", state="disabled")
            return
        self._wizard_mods = (install_config, setup_window)
        self._run_wizard()

    def _run_wizard(self):
        # SetupWindow(root=...) clears our widgets and rebuilds the wizard UI in
        # the SAME window; start() drives it on the already-running mainloop. A
        # fresh SetupWindow each attempt resets its aborted/frontend state so
        # "Retry setup" can re-enter cleanly (round-3 feedback: a retry button).
        install_config, setup_window = self._wizard_mods
        win = setup_window.SetupWindow(root=self.root)

        def done(value, error, has_value):
            # Clean finish -> start the tray (it now seeds the just-pinned agent),
            # then close. A user-close (SystemExit) -> just close, no tray (nothing
            # was written). A real failure -> keep the window and offer Retry.
            if error is not None and not isinstance(error, SystemExit):
                self._wizard_failed(error)
                return
            if error is None:
                try:
                    self._start_tray()
                except Exception:
                    pass  # a tray-start hiccup must never keep the window open
            try:
                self.root.destroy()
            except Exception:
                pass

        win.start(lambda: install_config.run_setup(None, console_only=True), done)

    def _wizard_failed(self, error):
        """The setup wizard hit a hard error: replace the window with a small
        card that shows what went wrong and a Retry that re-enters setup."""
        for child in list(self.root.winfo_children()):
            child.destroy()
        self.root.configure(bg=_BG)
        tk.Label(self.root, text="Setup didn't finish", font=_FONT_TITLE,
                 bg=_BG, fg=_FG).pack(anchor="w", padx=16, pady=(16, 4))
        tk.Label(self.root, text=str(error) or type(error).__name__, font=_FONT,
                 bg=_BG, fg=_MUTED, wraplength=560, justify="left",
                 anchor="w").pack(fill="x", padx=16, pady=(0, 4))
        tk.Label(self.root, text="Parley itself is installed - you can retry setup "
                 "now, or later from a terminal with:  parley --setup",
                 font=_FONT, bg=_BG, fg=_MUTED, wraplength=560, justify="left",
                 anchor="w").pack(fill="x", padx=16, pady=(0, 12))
        row = tk.Frame(self.root, bg=_BG)
        row.pack(fill="x", padx=16, pady=(0, 16))
        tk.Button(row, text="Retry setup ▶", font=_FONT_BOLD, bg=_ACCENT,
                  fg="#0b1218", relief="flat", padx=18, pady=5, cursor="hand2",
                  command=self._run_wizard).pack(side="left")
        tk.Button(row, text="Close", font=_FONT, bg="#2b3a4a", fg=_FG, relief="flat",
                  padx=14, pady=5, cursor="hand2",
                  command=self.root.destroy).pack(side="right")

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    Installer().run()
