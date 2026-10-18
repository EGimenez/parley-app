"""Tiny terminal prompt helpers for the setup wizard (iteration 23).

Design rules:
  * ASCII-only output — the wizard runs in whatever console Windows provides.
  * Piped stdin degrades gracefully: `ask_secret` falls back to a visible
    input() when stdin is not a tty (getpass on Windows reads the console
    device directly and would hang on piped input; this also makes the wizard
    scriptable for tests).
  * EOF (Ctrl+Z / exhausted pipe) aborts the whole wizard cleanly.

Frontend seam (iteration 24): every primitive routes through the module-level
`_frontend` when one is set — the Setup Window (`setup_window.SetupWindow`)
implements the same names, so `steps.py` never knows which surface it's on.
The console implementations below stay the fallback (piped runs, --console,
no tkinter)."""

import getpass
import os
import sys
import webbrowser

_frontend = None  # set by setup_window while the wizard window drives the show


def set_frontend(frontend):
    """Install (or with None, remove) the active UI frontend. It must provide:
    say, ask, ask_secret, ask_yn, ask_choice, pause, open_page, guide;
    spotlight stays console-only (the window is its own spotlight)."""
    global _frontend
    _frontend = frontend


def say(text=""):
    if _frontend:
        return _frontend.say(text)
    print(text)


def _read(prompt):
    try:
        return input(prompt)
    except EOFError:
        raise SystemExit("\nSetup aborted (end of input).")


def ask(prompt, default=None):
    """One answer, re-asked until non-empty (or the default is taken)."""
    if _frontend:
        return _frontend.ask(prompt, default)
    suffix = f" [{default}]" if default is not None else ""
    while True:
        raw = _read(f"{prompt}{suffix}: ").strip()
        if raw:
            return raw
        if default is not None:
            return default


def _clean_secret(raw):
    """Pasted secrets drag invisible junk (BOM, zero-width spaces) that later
    fails deep inside the HTTP client — keep printable ASCII only. Returns
    (cleaned, receipt_text_or_None)."""
    cleaned = "".join(c for c in raw if 33 <= ord(c) <= 126)
    if not cleaned:
        return "", None
    receipt = f"  received {len(cleaned)} characters, ending ...{cleaned[-3:]}"
    if len(cleaned) != len(raw):
        receipt += f" ({len(raw) - len(cleaned)} invisible character(s) removed)"
    return cleaned, receipt


def ask_secret(prompt):
    """A secret: hidden on a real console, visible input() when piped.

    Hidden input shows NOTHING while pasting — no stars — so a failed paste
    looks exactly like a successful one. After reading, echo a safe receipt
    (length + last 3 chars) so the user knows the paste landed."""
    if _frontend:
        return _frontend.ask_secret(prompt)
    while True:
        if sys.stdin.isatty():
            try:
                raw = getpass.getpass(f"{prompt} (input hidden - paste, then Enter): ").strip()
            except EOFError:
                raise SystemExit("\nSetup aborted (end of input).")
        else:
            raw = _read(f"{prompt}: ").strip()
        cleaned, receipt = _clean_secret(raw)
        if not cleaned:
            say("  (nothing received - paste the value, then press Enter)")
            continue
        say(receipt)
        return cleaned


def ask_yn(prompt, default=False, yes_label="Yes", no_label="No"):
    """A two-button question. `yes_label`/`no_label` rename the buttons in the
    Setup Window (e.g. 'Use it' / 'Modify'); the console fallback keeps [Y/n]."""
    if _frontend:
        return _frontend.ask_yn(prompt, default, yes_label, no_label)
    suffix = " [Y/n]" if default else " [y/N]"
    raw = _read(f"{prompt}{suffix}: ").strip().lower()
    if not raw:
        return default
    return raw.startswith("y")


def ask_choice(prompt, options, other=None, default=None):
    """Numbered menu. `options` is a list of (value, label) pairs; `other`
    (a label) appends a free-text entry. `default` (a value from `options`), if
    given, is pre-selected in the GUI and accepted on a bare Enter in the
    console. Returns the chosen value."""
    if _frontend:
        return _frontend.ask_choice(prompt, options, other, default)
    say(prompt)
    for i, (value, label) in enumerate(options, 1):
        mark = " (default)" if value == default else ""
        say(f"  {i}. {value} - {label}{mark}")
    if other:
        say(f"  {len(options) + 1}. {other}")
    top = len(options) + (1 if other else 0)
    hint = " (Enter for default)" if default is not None else ""
    while True:
        raw = _read(f"Pick 1-{top}{hint}: ").strip()
        if not raw and default is not None:
            return default
        if raw.isdigit() and 1 <= int(raw) <= top:
            n = int(raw)
            if n <= len(options):
                return options[n - 1][0]
            return ask("Enter it")
        say(f"Please answer with a number from 1 to {top}.")


def spotlight(on=True):
    """Best-effort (user feedback 2026-07-13): keep the console window ON TOP —
    slightly translucent so the browser shows through — while the wizard sends
    the user off to consent pages, so the instructions never disappear behind
    the browser. Windows/conhost only; a silent no-op elsewhere (notably
    Windows Terminal, whose GetConsoleWindow is a hidden ConPTY host)."""
    if _frontend:
        return  # the Setup Window is its own spotlight (topmost by design)
    if os.name != "nt":
        return
    if on and os.environ.get("WT_SESSION"):
        # Windows Terminal has no reachable top-level window via the console
        # handle — pinning silently fails there (live-test finding 2026-07-13).
        say("(Tip: this window can't stay on top in Windows Terminal - snap it "
            "side by side with the browser: Win+Left / Win+Right.)")
    try:
        import ctypes
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if not hwnd:
            return
        user32 = ctypes.windll.user32
        GWL_EXSTYLE, WS_EX_LAYERED, LWA_ALPHA = -20, 0x80000, 0x2
        SWP_NOMOVE_NOSIZE = 0x1 | 0x2
        if on:
            style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE) | WS_EX_LAYERED
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
            user32.SetLayeredWindowAttributes(hwnd, 0, 235, LWA_ALPHA)  # ~92% opaque
            user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, SWP_NOMOVE_NOSIZE)  # HWND_TOPMOST
        else:
            user32.SetLayeredWindowAttributes(hwnd, 0, 255, LWA_ALPHA)
            user32.SetWindowPos(hwnd, -2, 0, 0, 0, 0, SWP_NOMOVE_NOSIZE)  # HWND_NOTOPMOST
    except Exception:
        pass  # cosmetics only — never let this break the wizard


def working(text):
    """Enter a 'busy — go do something in the browser' state before a BLOCKING
    step (an OAuth consent that owns the thread until the browser redirects
    back). On the Setup Window this clears any leftover button so NEXT can't
    look clickable while nothing is listening; the console just prints."""
    if _frontend:
        return _frontend.working(text)
    say(text)


def pause(prompt="Press Enter when done..."):
    """Block until the user comes back from a browser step."""
    if _frontend:
        return _frontend.pause(prompt)
    _read(f"{prompt} ")


def open_page(url, blurb, image=None):
    """Print the URL (SSH-safe) and try to open it in the default browser.
    `image` names a screenshot under install_config/guide/ — only the Setup
    Window renders it; the console ignores it."""
    if _frontend:
        return _frontend.open_page(url, blurb, image)
    say(f"{blurb}\n  {url}")
    if not sys.stdin.isatty():
        return  # scripted/piped runs must not pop browser windows
    try:
        webbrowser.open(url)
    except Exception:
        pass  # printing the URL is the contract; the browser is a convenience


def guide(pages):
    """A guided browser-page sequence. Each page is either:
      * a tuple `(url, blurb[, image[, opens[, show_url]]])` — a browser step the
        user does on the page (`show_url=False` hides the raw URL in the Setup
        Window while still opening the browser; the console still prints it, as
        the URL is the SSH-safe fallback), or
      * a dict `{"ask": prompt, "key": name[, "image"][, "default"][, "secret"]}`
        — an INPUT step that lives in the same BACK / NEXT navigation, so a value
        (e.g. the Azure Application ID, or the Gmail client id/secret) can be
        collected between pages. `secret: True` masks it in the Setup Window.
      * a dict `{"form": [ {ask, key[, secret][, default]}, ... ][, "image"]}`
        — several fields asked on ONE screen (e.g. a Yahoo address + app
        password together), still inside the same BACK / NEXT navigation.

    The Setup Window renders this as a real stepper (BACK / NEXT, one screenshot
    per page); the console fallback is the classic open → Enter loop (linear, no
    BACK). Returns `{key: value}` for every input/form field (empty dict if
    none)."""
    if _frontend:
        return _frontend.guide(pages)
    answers = {}
    for page in pages:
        if isinstance(page, dict) and "form" in page:
            for field in page["form"]:
                if field.get("secret") and field.get("default") is None:
                    answers[field["key"]] = ask_secret(field["ask"])
                else:
                    answers[field["key"]] = ask(field["ask"], field.get("default"))
            continue
        if isinstance(page, dict) and "ask" in page:
            if page.get("secret") and page.get("default") is None:
                answers[page["key"]] = ask_secret(page["ask"])
            else:
                answers[page["key"]] = ask(page["ask"], page.get("default"))
            continue
        url, blurb = page[0], page[1]
        opens = page[3] if len(page) > 3 else True
        say()
        if opens:
            open_page(url, blurb)
        else:
            say(blurb)  # in-tab step: no new page, the blurb says where to click
        pause()
    return answers
