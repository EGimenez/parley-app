"""tray_backend — the platform-specific pieces of the Parley tray app (iteration 22).

Three capabilities, each degrading gracefully so `parley_desktop` runs on a bare
Python too (D7: Windows-first, but the *app logic* never imports a platform lib):

  * `TrayIcon`    — pystray + Pillow. Missing → a console stub (the windows and the
                    hotkey still work; you just don't get the icon).
  * `notify()`    — windows-toasts (click → callback). Missing → pystray's notify
                    bubble. Missing too → a console line.
  * `GlobalHotkey`— RegisterHotKey via ctypes (stdlib; no pywin32 import needed),
                    message loop in its own thread. Non-Windows / registration
                    failure → reported via `.error`, app continues (D12).

The mockup palette lives here too so both windows and the icon share it.
"""

import ctypes
import os
import sys
import threading

# The approved mockup palette (iteration 22 §7).
COLORS = {
    "surface": "#1e2630", "raised": "#26313f", "inset": "#161d26",
    "border": "#313d4d", "text": "#e9eef4", "muted": "#93a1b3",
    "faint": "#5c6a7c", "accent": "#46c2a4", "amber": "#e0a63d", "red": "#d96a5f",
}
UI_FONT = ("Segoe UI", 10)
MONO_FONT = ("Cascadia Mono", 10)


def set_app_icon(root):
    """Brand a Tk window with the sail icon (same glyph as the tray, assets/
    parley.ico) instead of Tk's default feather. Best-effort: `.ico` only works on
    Windows via `iconbitmap` (elsewhere it needs an XBM), so any failure here is
    silently ignored — a window with the stock icon still works fine."""
    try:
        root.iconbitmap(os.path.join(os.path.dirname(__file__), "assets", "parley.ico"))
    except Exception:  # noqa: BLE001 — cosmetic only, never worth crashing over
        pass


# --- Tray icon -----------------------------------------------------------------

def _draw_icon(live, count):
    """The sail glyph from the mockup: grey idle, teal live, amber count badge."""
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    sail = COLORS["accent"] if live else "#7b8797"
    d.polygon([(30, 4), (30, 44), (10, 44)], fill=sail)    # main sail
    d.polygon([(36, 14), (36, 44), (54, 44)], fill=sail)   # jib
    d.polygon([(6, 50), (58, 50), (52, 60), (12, 60)], fill=sail)  # hull
    if count:
        d.ellipse([34, 0, 64, 30], fill=COLORS["amber"])
        text = str(count) if count < 10 else "9+"
        try:
            font = ImageFont.truetype("segoeuib.ttf", 20)
        except OSError:
            font = ImageFont.load_default()
        d.text((49, 14), text, fill="#211703", font=font, anchor="mm")
    return img


class Submenu:
    """A menu entry whose contents are rebuilt every time the menu is opened.

    `items` is a callable returning [(label, callback, checked), ...]. The win32
    backend of pystray does NOT re-evaluate it on right-click: it builds the menu
    once at start and again only on `update_menu()` (user report 2026-09-24: the
    agent list stayed frozen at boot). `TrayIcon.refresh_menu()` is that call."""

    def __init__(self, label, items):
        self.label = label
        self.items = items


class TrayIcon:
    """The tray icon + menu. `menu_items` = [(label, callback), ...], a `Submenu`, or
    None for a separator. Falls back to a console stub when pystray/Pillow aren't
    installed."""

    def __init__(self, menu_items):
        self._count = 0
        self._live = False
        self._icon = None
        try:
            import pystray
        except ImportError:
            print("(no tray icon — pip install parley[desktop] for pystray/Pillow; "
                  "windows and hotkey still work.)")
            return
        def build(item):
            if item is None:
                return pystray.Menu.SEPARATOR
            if isinstance(item, Submenu):
                def rows(_source=item.items):
                    return [pystray.MenuItem(label, _menu_cb(action),
                                             checked=(lambda _i, _c=checked: _c))
                            for label, action, checked in _source()]
                return pystray.MenuItem(item.label, pystray.Menu(rows))
            return pystray.MenuItem(item[0], _menu_cb(item[1]))

        entries = [build(item) for item in menu_items]
        self._icon = pystray.Icon("parley", _draw_icon(False, 0), "Parley", pystray.Menu(*entries))
        threading.Thread(target=self._icon.run, daemon=True).start()

    def set_state(self, live, count):
        """live = any daemon up; count = total pending questions (the badge)."""
        if (live, count) == (self._live, self._count):
            return
        self._live, self._count = live, count
        if self._icon is not None:
            self._icon.icon = _draw_icon(live, count)
            self._icon.title = f"Parley — {count} pending" if count else "Parley"

    def refresh_menu(self):
        """Rebuild the menu so each `Submenu` reflects current state."""
        if self._icon is not None:
            try:
                self._icon.update_menu()
            except Exception:  # noqa: BLE001 — a stale menu must not kill the tray
                pass

    def notify(self, title, message):
        """pystray's notification bubble — the toast fallback."""
        if self._icon is not None:
            try:
                self._icon.notify(message, title)
                return True
            except Exception:  # noqa: BLE001 — notification is best-effort
                pass
        return False

    def stop(self):
        if self._icon is not None:
            self._icon.stop()


def _menu_cb(fn):
    return lambda icon, item: fn()


# --- Toasts ----------------------------------------------------------------------

_toaster = None


def notify(tray, title, message, on_click=None):
    """A Windows toast (clickable when windows-toasts is installed); falls back to
    the tray bubble, then to a console line. `on_click` runs on the toast's thread —
    callers should only post to their event queue from it."""
    global _toaster
    try:
        from windows_toasts import Toast, WindowsToaster
        if _toaster is None:
            _toaster = WindowsToaster("Parley")
        toast = Toast([title, message])
        if on_click is not None:
            toast.on_activated = lambda _args: on_click()
        _toaster.show_toast(toast)
        return
    except Exception:  # noqa: BLE001 — ImportError or WinRT trouble: degrade
        pass
    if not (tray and tray.notify(title, message)):
        print(f"🔔 {title}: {message}")


# --- Global hotkey ----------------------------------------------------------------

_MODS = {"alt": 0x0001, "ctrl": 0x0002, "shift": 0x0004, "win": 0x0008}
_VKS = {"space": 0x20, "tab": 0x09, "enter": 0x0D}
_WM_HOTKEY = 0x0312
_MOD_NOREPEAT = 0x4000


def parse_hotkey(spec):
    """'win+alt+p' → (modifier_mask, vk). Raises ValueError on nonsense."""
    mods, vk = 0, None
    for part in str(spec).lower().replace(" ", "").split("+"):
        if part in _MODS:
            mods |= _MODS[part]
        elif part in _VKS:
            vk = _VKS[part]
        elif len(part) == 1 and (part.isalpha() or part.isdigit()):
            vk = ord(part.upper())
        else:
            raise ValueError(f"can't parse hotkey part {part!r} in {spec!r}")
    if not mods or vk is None:
        raise ValueError(f"hotkey needs modifiers + a key: {spec!r}")
    return mods, vk


class GlobalHotkey:
    """Registers a system-wide hotkey (Windows, ctypes — no extra deps) and calls
    `callback` from the listener thread on each press. On failure `.error` holds
    the reason and the app carries on (D12: menu 'Ask Parley…' still works)."""

    def __init__(self, spec, callback):
        self.error = None
        if sys.platform != "win32":
            self.error = "global hotkey is Windows-only for now"
            return
        try:
            self._mods, self._vk = parse_hotkey(spec)
        except ValueError as err:
            self.error = str(err)
            return
        self._spec = spec
        self._callback = callback
        self._ready = threading.Event()
        threading.Thread(target=self._loop, daemon=True).start()
        self._ready.wait(timeout=3)

    def _loop(self):
        user32 = ctypes.windll.user32
        if not user32.RegisterHotKey(None, 1, self._mods | _MOD_NOREPEAT, self._vk):
            self.error = (f"couldn't register {self._spec!r} (another app owns it?) — "
                          "set [desktop] hotkey in your cfg")
            self._ready.set()
            return
        self._ready.set()
        msg = ctypes.wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == _WM_HOTKEY:
                try:
                    self._callback()
                except Exception:  # noqa: BLE001 — the listener must survive
                    pass


# ctypes.wintypes import is cheap and harmless off-Windows, but do it lazily-safe:
try:
    import ctypes.wintypes  # noqa: E402  (needed by GlobalHotkey._loop)
except Exception:  # noqa: BLE001 — non-Windows exotic platforms
    pass
