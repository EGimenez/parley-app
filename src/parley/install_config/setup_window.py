"""Setup Window — the wizard's GUI front end (iteration 24).

A tkinter window that sizes itself to the RIGHT HALF of the screen and stays
on top, so the user can click through the Google / Azure / Yahoo pages on the
left while the instructions (with screenshots) never disappear — the fix for
Windows Terminal, where the console spotlight can't pin (2026-07-13 finding).

Architecture: the wizard logic (`install_config.run_setup`) runs unchanged on a
worker thread; this window implements the same primitives as
`install_config.console` (say / ask / ask_secret / ask_yn / ask_choice / pause /
open_page / guide) and installs itself via `console.set_frontend`. Each
primitive marshals one request onto the Tk mainloop and blocks the worker until
the user answers — NEXT here replaces "press Enter in the console". Closing the
window aborts the wizard cleanly (SystemExit, nothing written).

Screenshots: PNGs under `install_config/guide/` (package data), keyed by the
page's image name in steps.py. Missing files degrade to text-only.
"""

import os
import threading
import webbrowser

from parley.install_config import console

GUIDE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "guide")

_ABORT = object()

_FONT = ("Segoe UI", 10)
_FONT_BOLD = ("Segoe UI", 10, "bold")
_FONT_TITLE = ("Segoe UI", 14, "bold")
_BG = "#1c2733"        # dark slate — readable next to a bright browser
_FG = "#e8edf2"
_ACCENT = "#4ea1d3"    # parley sail blue
_MUTED = "#9db2c4"


def available():
    """Can a window actually open here? (tkinter importable + a display)."""
    try:
        import tkinter
        root = tkinter.Tk()
        root.withdraw()
        root.destroy()
        return True
    except Exception:
        return False


class SetupWindow:
    """One wizard run inside one window. `run(worker)` owns the Tk mainloop on
    the calling thread and executes `worker()` on a background thread with this
    window installed as the console frontend."""

    def __init__(self, title="Parley Setup", root=None):
        import tkinter as tk
        from tkinter import scrolledtext
        self._tk = tk
        # Adopt an existing window (the GUI installer hands us its own root so
        # the whole experience is ONE window, iteration 26) or make our own.
        if root is None:
            self.root = tk.Tk()
            self._owns_root = True
            from parley.client_server.tray_backend import set_app_icon
            set_app_icon(self.root)
        else:
            self.root = root
            self._owns_root = False
            for child in list(self.root.winfo_children()):
                child.destroy()
        self.root.title(title)
        self.root.configure(bg=_BG)
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        # Compact (iteration 26 A3): a small card in the top-right corner, not a
        # half-screen panel — the browser gets the rest of the desktop.
        w, h = 600, min(720, sh - 100)
        self.root.geometry(f"{w}x{h}+{sw - w - 24}+48")
        self.root.attributes("-topmost", True)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._aborted = False
        self._pending_finish = None   # the live request's finish(), for abort
        self._images = {}             # keep PhotoImage references alive
        self._clear_next = False      # after an answer, the next screen starts fresh (A3)

        header = tk.Frame(self.root, bg=_BG)
        header.pack(fill="x", padx=14, pady=(12, 4))
        tk.Label(header, text="⛵ Parley Setup", font=_FONT_TITLE,
                 bg=_BG, fg=_FG).pack(side="left")
        self._section = tk.Label(header, text="", font=_FONT, bg=_BG, fg=_MUTED)
        self._section.pack(side="right")

        # Screenshot pane (hidden until a page provides one).
        self._shot_frame = tk.Frame(self.root, bg=_BG)
        self._shot_label = tk.Label(self._shot_frame, bg=_BG)
        self._shot_label.pack()
        self._shot_caption = tk.Label(self._shot_frame, text="", font=_FONT,
                                      bg=_BG, fg=_MUTED)
        self._shot_caption.pack()

        # Transcript — everything say() prints, so nothing scrolls away lost.
        self._text = scrolledtext.ScrolledText(
            self.root, wrap="word", font=_FONT, height=12, relief="flat",
            bg="#141d26", fg=_FG, insertbackground=_FG, state="disabled")
        self._text.pack(fill="both", expand=True, padx=14, pady=8)

        # Input area — rebuilt per request at the bottom.
        self._input = tk.Frame(self.root, bg=_BG)
        self._input.pack(fill="x", padx=14, pady=(0, 14))

    # -- lifecycle --------------------------------------------------------------

    def run(self, worker):
        """Run `worker()` on a thread with this window as the console frontend;
        block in the Tk mainloop until it finishes (or the window is closed).
        Returns the worker's result; re-raises what it raised. Owns the mainloop
        — the standalone `parley --setup` GUI path."""
        outcome = {}

        def _done(value, error, has_value):
            outcome["error"] = error
            if has_value:
                outcome["value"] = value
            try:
                self.root.destroy()
            except Exception:
                pass

        self.start(worker, _done)
        self.root.mainloop()
        self._aborted = True  # anything still blocking must give up now
        if outcome.get("error") is not None:
            raise outcome["error"]
        if "value" in outcome:
            return outcome["value"]
        raise SystemExit("Setup aborted - the window was closed; nothing was written.")

    def start(self, worker, on_done):
        """Run `worker()` on a thread with this window as the console frontend
        WITHOUT owning the mainloop — for when the caller's Tk mainloop is
        already running (the GUI installer transforms its own window into the
        wizard). Calls `on_done(value, error, has_value)` on the UI thread when
        the worker finishes; the caller decides whether to close the window."""
        outcome = {}

        def _work():
            console.set_frontend(self)
            try:
                outcome["value"] = worker()
            except BaseException as exc:  # incl. SystemExit — carried to the caller
                outcome["error"] = exc
            finally:
                console.set_frontend(None)
                self._aborted = True

                def _finish():
                    on_done(outcome.get("value"), outcome.get("error"),
                            "value" in outcome)
                try:
                    self.root.after(0, _finish)
                except Exception:
                    pass

        threading.Thread(target=_work, daemon=True, name="parley-setup").start()

    def _on_close(self):
        self._aborted = True
        if self._pending_finish is not None:
            self._pending_finish(_ABORT)
        try:
            self.root.destroy()
        except Exception:
            pass

    # -- worker→UI marshalling ---------------------------------------------------

    def _request(self, build):
        """Worker side: run `build(finish)` on the UI thread, block until
        `finish(value)` is called, return the value. Window closed → SystemExit."""
        if self._aborted:
            raise SystemExit("Setup aborted - the window was closed; nothing was written.")
        done = threading.Event()
        box = {}

        def finish(value):
            if not done.is_set():
                box["value"] = value
                self._pending_finish = None
                done.set()

        def _build():
            self._pending_finish = finish
            self._raise()  # a new prompt → bring the window back to the front
            build(finish)

        try:
            self.root.after(0, _build)
        except Exception:
            raise SystemExit("Setup aborted - the window was closed; nothing was written.")
        done.wait()
        if box.get("value") is _ABORT or self._aborted:
            raise SystemExit("Setup aborted - the window was closed; nothing was written.")
        self._clear_next = True  # this answer done → the next screen starts clean (A3)
        return box["value"]

    def _ui(self, fn):
        """Fire-and-forget on the UI thread (say / screenshots)."""
        try:
            self.root.after(0, fn)
        except Exception:
            pass

    # -- focus / always-on-top (decision D3: raise the browser, return the window) --

    def _set_topmost(self, on):
        try:
            self.root.attributes("-topmost", bool(on))
        except Exception:
            pass

    def _raise(self):
        """Bring the window to the front (used whenever a new prompt appears)."""
        try:
            self.root.attributes("-topmost", True)
            self.root.lift()
            self.root.focus_force()
        except Exception:
            pass
        self._win_pin_top()  # tk's -topmost alone can fail to re-assert after the browser held foreground

    def _win_pin_top(self):
        """Windows: force the top-level window back to the TOPMOST z-order via
        SetWindowPos. After the OAuth consent (which drops -topmost so the browser
        is reachable) the wizard was falling behind the browser and staying there
        (feedback 2026-07-21) — tk's `-topmost=True` doesn't reliably re-pin once
        another app has owned the foreground, but SetWindowPos re-pins even from a
        thread that doesn't hold it. No-op off Windows / on any error.

        The HWND is `wm_frame()` (the decorated top-level, NOT winfo_id() which is
        an inner child), and HWND args MUST be typed — an untyped -1 (HWND_TOPMOST)
        truncates to 32 bits on 64-bit Windows and SetWindowPos then fails with
        ERROR_INVALID_WINDOW_HANDLE (1400)."""
        if os.name != "nt":
            return
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                            ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                            ctypes.c_uint]
            user32.SetWindowPos.restype = wintypes.BOOL
            hwnd = int(self.root.wm_frame(), 16)
            HWND_TOPMOST = wintypes.HWND(-1)
            SWP_NOMOVE_NOSIZE = 0x1 | 0x2
            user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE_NOSIZE)
        except Exception:
            pass

    def _flash_browser(self):
        """A page just opened: drop always-on-top so the foregrounded browser is
        visible, then re-raise the window (with its instructions) ~1.5s later."""
        self._set_topmost(False)
        try:
            self.root.after(1500, self._raise)
        except Exception:
            pass

    # -- shared UI helpers (UI thread only) ---------------------------------------

    def _clear_text(self):
        self._text.configure(state="normal")
        self._text.delete("1.0", "end")
        self._text.configure(state="disabled")

    def _append(self, text):
        if self._clear_next:  # first line of a new step → wipe the previous one (A3)
            self._clear_text()
            self._clear_next = False
        self._text.configure(state="normal")
        self._text.insert("end", text + "\n")
        self._text.see("end")
        self._text.configure(state="disabled")

    def _fresh_screen(self):
        """Wipe the answered screen before a new prompt is drawn, so a new screen
        never inherits the previous one's text — notably the previous question's
        answer (feedback 2026-07-21). Idempotent: only the first call after an
        answer clears; a step whose own say() lines already cleared is a no-op."""
        if self._clear_next:
            self._clear_text()
            self._clear_next = False

    def _clear_input(self):
        for child in self._input.winfo_children():
            child.destroy()

    def _button(self, parent, label, command, primary=False):
        tk = self._tk
        btn = tk.Button(parent, text=label, command=command, font=_FONT_BOLD,
                        bg=_ACCENT if primary else "#2b3a4a",
                        fg="#0b1218" if primary else _FG,
                        activebackground=_ACCENT, relief="flat",
                        padx=16, pady=4, cursor="hand2")
        return btn

    def _show_shot(self, image_name, caption=""):
        """Show (or hide, with image_name=None) the screenshot pane."""
        tk = self._tk
        if not image_name:
            self._shot_frame.pack_forget()
            return
        path = os.path.join(GUIDE_DIR, image_name)
        if not os.path.exists(path):
            self._shot_frame.pack_forget()
            return
        try:
            img = self._images.get(image_name)
            if img is None:
                img = tk.PhotoImage(file=path)
                # Fit the window's width (PhotoImage only shrinks by integers).
                max_w = max(self.root.winfo_width() - 60, 400)
                if img.width() > max_w:
                    img = img.subsample((img.width() + max_w - 1) // max_w)
                self._images[image_name] = img
            self._shot_label.configure(image=img)
            self._shot_caption.configure(text=caption)
            self._shot_frame.pack(fill="x", padx=14, pady=(4, 0),
                                  before=self._text)
        except Exception:
            self._shot_frame.pack_forget()

    # -- the console primitives (worker thread) -----------------------------------

    def say(self, text=""):
        s = str(text)
        # "-- Section --" say() lines double as the header's section label.
        if s.startswith("-- ") and s.endswith(" --"):
            title = s[3:-3]
            self._ui(lambda: self._section.configure(text=title))
        self._ui(lambda: self._append(s))

    def ask(self, prompt, default=None):
        # No answer echo: the next screen clears on draw (see _fresh_screen), so
        # echoing "prompt: value" here would just head the NEXT question's screen
        # with the PREVIOUS answer — the confusing bit users flagged (2026-07-21).
        while True:
            value = self._request(lambda finish: self._build_entry(prompt, default, finish))
            value = (value or "").strip()
            if value:
                return value
            if default is not None:
                return default

    def ask_secret(self, prompt):
        while True:
            raw = self._request(lambda finish: self._build_entry(prompt, None, finish,
                                                                 secret=True))
            cleaned, receipt = console._clean_secret((raw or "").strip())
            if not cleaned:
                self.say("  (nothing received - paste the value, then press NEXT)")
                continue
            self.say(f"{prompt}: (hidden)")
            self.say(receipt)
            return cleaned

    def ask_yn(self, prompt, default=False, yes_label="Yes", no_label="No"):
        value = self._request(lambda finish: self._build_yn(prompt, default, finish,
                                                            yes_label, no_label))
        return bool(value)  # no echo — the next screen clears on draw (_fresh_screen)

    def ask_choice(self, prompt, options, other=None, default=None):
        value = self._request(lambda finish: self._build_choice(prompt, options,
                                                                other, finish, default))
        if value is _OTHER:
            return self.ask("Enter it")
        return value  # no echo — the next screen clears on draw (_fresh_screen)

    def pause(self, prompt="Press Enter when done..."):
        self._request(lambda finish: self._build_next(prompt, finish))

    def working(self, text):
        """A blocking browser step is about to own the worker thread: clear the
        input area (no stale button) and step the window aside so the OAuth
        consent page is reachable. The next input request re-raises the window."""
        def _do():
            self._clear_input()
            self._append(text)
            self._set_topmost(False)
        self._ui(_do)

    def open_page(self, url, blurb, image=None):
        self.say(f"{blurb}\n  {url}")
        self._ui(lambda: self._show_shot(image, "what the page should look like"))
        try:
            webbrowser.open(url, new=0)  # new=0: reuse the window where the browser allows it
        except Exception:
            pass
        self._ui(self._flash_browser)  # let the user SEE the page, then return the window

    def guide(self, pages):
        """The stepper: one page at a time with BACK / NEXT and a screenshot.
        A page is either `(url, blurb[, image[, opens[, show_url]]])` (a browser
        step; `opens=False` marks an in-tab step — no new tab is opened for it;
        `show_url=False` still opens the browser but hides the raw URL text, for
        deep-links too long/ugly to be worth showing), or
        `{"ask": prompt, "key": name[, "image"][, "default"]}` (an INPUT step
        that shares the same BACK / NEXT navigation), or
        `{"form": [ {ask, key[, secret][, default]}, ... ][, "image"]}` (SEVERAL
        fields on one screen — e.g. a Yahoo address + app password together).
        Returns `{key: value}` for every input/form field, so values can be
        collected between two pages."""
        answers = {}
        typed = {}       # index -> last text entered on an input page (survives BACK)
        i = 0
        opened = set()
        while i < len(pages):
            page = pages[i]
            if isinstance(page, dict) and "form" in page:  # several fields, one screen
                defaults = typed.get(i, {f["key"]: f.get("default") for f in page["form"]})
                action, values = self._request(
                    lambda finish, p=page, idx=i, d=defaults:
                        self._build_guide_form(p, idx, len(pages), d, finish))
                if action == "back" and i > 0:
                    typed[i] = values  # keep what they typed on the way back
                    i -= 1
                    opened.discard(i)
                    continue
                typed[i] = values
                cleaned, missing = {}, False
                for field in page["form"]:
                    raw = (values.get(field["key"]) or "").strip()
                    if field.get("secret"):
                        raw = console._clean_secret(raw)[0]
                    if not raw and field.get("default") is None:
                        missing = True
                    cleaned[field["key"]] = raw or field.get("default")
                if missing:
                    continue  # a required field is still empty — re-show, don't advance
                for field in page["form"]:
                    answers[field["key"]] = cleaned[field["key"]]
                # No answer echo — the next screen clears on draw (_fresh_screen),
                # so echoing here would just head it with the previous answer
                # (the confusing bit users flagged, 2026-07-21).
                i += 1
                continue
            if isinstance(page, dict) and "ask" in page:  # an input step
                default = typed.get(i, page.get("default"))
                action, text = self._request(
                    lambda finish, p=page, idx=i, d=default:
                        self._build_guide_input(p, idx, len(pages), d, finish))
                if action == "back" and i > 0:
                    typed[i] = text  # keep what they typed on the way back
                    i -= 1
                    opened.discard(i)
                    continue
                value = (text or "").strip()
                if not value and page.get("default") is None:
                    continue  # required field still empty — re-show, don't advance
                value = value or page.get("default")
                typed[i] = value
                answers[page["key"]] = value
                # No answer echo — the next screen clears on draw (_fresh_screen).
                i += 1
                continue
            url, blurb = page[0], page[1]
            image = page[2] if len(page) > 2 else None
            opens = page[3] if len(page) > 3 else True
            show_url = page[4] if len(page) > 4 else True
            if opens and i not in opened:  # BACK re-shows, but only re-OPENS on request
                try:
                    webbrowser.open(url, new=0)  # reuse the window where the browser allows
                except Exception:
                    pass
                opened.add(i)
                self._ui(self._flash_browser)  # surface the page, then return here
            action = self._request(
                lambda finish, u=url, b=blurb, im=image, idx=i, op=opens, su=show_url:
                    self._build_guide_page(u, b, im, idx, len(pages), op, finish, su))
            if action == "back" and i > 0:
                i -= 1
                opened.discard(i)  # BACK means "take me there again"
            elif action == "reopen":
                opened.discard(i)
            else:
                i += 1
        self._ui(lambda: self._show_shot(None))
        return answers

    # -- input builders (UI thread) -------------------------------------------------

    def _build_entry(self, prompt, default, finish, secret=False):
        tk = self._tk
        self._clear_input()
        self._fresh_screen()
        tk.Label(self._input, text=prompt + ":", font=_FONT_BOLD, bg=_BG,
                 fg=_FG, anchor="w", wraplength=560, justify="left").pack(fill="x")
        row = tk.Frame(self._input, bg=_BG)
        row.pack(fill="x", pady=(4, 0))
        entry = tk.Entry(row, font=_FONT, show="•" if secret else "",
                         bg="#141d26", fg=_FG, insertbackground=_FG, relief="flat")
        if default is not None:
            entry.insert(0, str(default))
            entry.select_range(0, "end")
        entry.pack(side="left", fill="x", expand=True, ipady=4)
        submit = lambda *_: finish(entry.get())
        entry.bind("<Return>", submit)
        self._button(row, "NEXT ▶", submit, primary=True).pack(side="left", padx=(8, 0))
        entry.focus_set()

    def _build_yn(self, prompt, default, finish, yes_label="Yes", no_label="No"):
        tk = self._tk
        self._clear_input()
        self._fresh_screen()
        tk.Label(self._input, text=prompt, font=_FONT_BOLD, bg=_BG, fg=_FG,
                 anchor="w", wraplength=560, justify="left").pack(fill="x")
        row = tk.Frame(self._input, bg=_BG)
        row.pack(fill="x", pady=(4, 0))
        self._button(row, yes_label, lambda: finish(True), primary=default).pack(side="left")
        self._button(row, no_label, lambda: finish(False),
                     primary=not default).pack(side="left", padx=(8, 0))
        self.root.bind("<Return>", lambda *_: finish(default))

    def _build_choice(self, prompt, options, other, finish, default=None):
        tk = self._tk
        self._clear_input()
        self._fresh_screen()
        tk.Label(self._input, text=prompt, font=_FONT_BOLD, bg=_BG, fg=_FG,
                 anchor="w", wraplength=560, justify="left").pack(fill="x")
        var = tk.StringVar(value=default if default is not None else options[0][0])
        for value, label in options:
            tk.Radiobutton(self._input, text=f"{value} — {label}", variable=var,
                           value=value, font=_FONT, bg=_BG, fg=_FG,
                           selectcolor="#141d26", activebackground=_BG,
                           activeforeground=_FG, anchor="w", wraplength=540,
                           justify="left").pack(fill="x")
        if other:
            tk.Radiobutton(self._input, text=other, variable=var, value="\x00other",
                           font=_FONT, bg=_BG, fg=_FG, selectcolor="#141d26",
                           activebackground=_BG, activeforeground=_FG,
                           anchor="w", wraplength=540, justify="left").pack(fill="x")
        row = tk.Frame(self._input, bg=_BG)
        row.pack(fill="x", pady=(6, 0))
        submit = lambda: finish(_OTHER if var.get() == "\x00other" else var.get())
        self._button(row, "NEXT ▶", submit, primary=True).pack(side="left")

    def _build_next(self, prompt, finish):
        tk = self._tk
        self._clear_input()
        tk.Label(self._input, text=prompt, font=_FONT, bg=_BG, fg=_MUTED,
                 anchor="w", wraplength=560, justify="left").pack(fill="x")
        row = tk.Frame(self._input, bg=_BG)
        row.pack(fill="x", pady=(4, 0))
        btn = self._button(row, "NEXT ▶", lambda: finish(True), primary=True)
        btn.pack(side="left")
        self.root.bind("<Return>", lambda *_: finish(True))
        btn.focus_set()

    def _build_guide_page(self, url, blurb, image, idx, total, opens, finish, show_url=True):
        tk = self._tk
        self._clear_input()
        self._show_shot(image, "what the page should look like")
        self._append("")
        self._append(blurb)
        if opens:
            if show_url:  # a long deep-link (show_url=False) is noise — the browser already opened it
                self._append(f"  {url}")
            prompt = "Do the step on the page that just opened on the LEFT, then press NEXT."
        else:
            prompt = "Do this on the SAME browser tab — no new page opens — then press NEXT."
        tk.Label(self._input, text=prompt, font=_FONT_BOLD, bg=_BG, fg=_FG,
                 anchor="w", wraplength=560, justify="left").pack(fill="x")
        row = tk.Frame(self._input, bg=_BG)
        row.pack(fill="x", pady=(4, 0))
        if idx > 0:
            self._button(row, "◀ BACK", lambda: finish("back")).pack(side="left")
        if opens:
            self._button(row, "open the page again",
                         lambda: finish("reopen")).pack(side="left", padx=(8, 0))
        btn = self._button(row, "NEXT ▶", lambda: finish("next"), primary=True)
        btn.pack(side="left", padx=(8, 0))
        self.root.bind("<Return>", lambda *_: finish("next"))
        btn.focus_set()

    def _build_guide_input(self, page, idx, total, default, finish):
        """An input step inside guide(): an entry with BACK / NEXT, so a value
        (e.g. the Azure Application ID) is collected between two pages without
        leaving the stepper. finish((action, text)) — action is 'back'/'next'."""
        tk = self._tk
        self._clear_input()
        self._fresh_screen()  # this input has no say() of its own — clear the answered screen ourselves
        self._show_shot(page.get("image"), "what the page should look like")
        prompt = page["ask"]
        tk.Label(self._input, text=prompt, font=_FONT_BOLD, bg=_BG, fg=_FG,
                 anchor="w", wraplength=560, justify="left").pack(fill="x")
        row = tk.Frame(self._input, bg=_BG)
        row.pack(fill="x", pady=(4, 0))
        entry = tk.Entry(row, font=_FONT, show="•" if page.get("secret") else "",
                         bg="#141d26", fg=_FG, insertbackground=_FG, relief="flat")
        if default is not None:
            entry.insert(0, str(default))
            entry.select_range(0, "end")
        entry.pack(side="left", fill="x", expand=True, ipady=4)
        if idx > 0:
            self._button(row, "◀ BACK",
                         lambda: finish(("back", entry.get()))).pack(side="left", padx=(0, 8))
        submit = lambda *_: finish(("next", entry.get()))
        entry.bind("<Return>", submit)
        self._button(row, "NEXT ▶", submit, primary=True).pack(side="left")
        entry.focus_set()

    def _build_guide_form(self, page, idx, total, defaults, finish):
        """A form step inside guide(): SEVERAL labelled entries on one screen
        (e.g. a Yahoo address + app password), with BACK / NEXT shared with the
        rest of the stepper. finish((action, {key: text})) — action 'back'/'next';
        the dict carries every field so BACK preserves what was typed."""
        tk = self._tk
        self._clear_input()
        self._fresh_screen()  # this form has no say() of its own — clear the answered screen ourselves
        self._show_shot(page.get("image"), "what the page should look like")
        entries = {}
        for field in page["form"]:
            tk.Label(self._input, text=field["ask"], font=_FONT_BOLD, bg=_BG, fg=_FG,
                     anchor="w", wraplength=560, justify="left").pack(fill="x", pady=(4, 0))
            entry = tk.Entry(self._input, font=_FONT, show="•" if field.get("secret") else "",
                             bg="#141d26", fg=_FG, insertbackground=_FG, relief="flat")
            dv = defaults.get(field["key"], field.get("default"))
            if dv is not None:
                entry.insert(0, str(dv))
            entry.pack(fill="x", ipady=4, pady=(2, 0))
            entries[field["key"]] = entry
        collect = lambda: {k: e.get() for k, e in entries.items()}
        row = tk.Frame(self._input, bg=_BG)
        row.pack(fill="x", pady=(8, 0))
        if idx > 0:
            self._button(row, "◀ BACK",
                         lambda: finish(("back", collect()))).pack(side="left", padx=(0, 8))
        submit = lambda *_: finish(("next", collect()))
        for entry in entries.values():
            entry.bind("<Return>", submit)
        self._button(row, "NEXT ▶", submit, primary=True).pack(side="left")
        # Focus the first empty field so a prefilled address jumps straight to the password.
        for entry in entries.values():
            if not entry.get():
                entry.focus_set()
                break
        else:
            next(iter(entries.values())).focus_set()


_OTHER = object()
