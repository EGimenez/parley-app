"""Yahoo wizard backend: address + app password proven with a real IMAP login,
the CalDAV calendar to book on (discovered and pinned here so the agent isn't
left write-blocked at runtime), and the timezone Parley reads local times against."""

from parley.install_config import console

KEY = "yahoo"
MENU_LABEL = "address + app password, IMAP login verified"

# `{calendar_line}` is filled by render_account_item: the whole `calendar:` line
# when one was pinned, or empty for a mail-only account.
ACCOUNT_ITEM = """\
  - backend: yahoo
    address: "{address}"
    app_password: "{app_password}"  # app password (verified with a live IMAP login at setup)
    timezone: "{timezone}"          # REQUIRED: Parley reads local times against this zone{calendar_line}
"""


def _yahoo_pages(prefill_address=None):
    """The BACK / NEXT stepper for Yahoo: (1) log in on the app-password page and
    make one, then (2) enter the address AND app password on a SINGLE screen
    (user feedback 2026-07-21: fewer, plainer steps; one shot for both creds).
    The address is prefilled on a retry so only the password need be re-pasted."""
    return [
        # Deep-link straight to the app-passwords panel (phase-2 prototype finding:
        # the not-signed-in bounce PRESERVES this destination). No assisted mode
        # here on purpose: everything of value on that page is a credential.
        ("https://login.yahoo.com/myaccount/security/app-password",
         "You need to log in on the Yahoo screen that just opened, then make an app "
         "password (NOT your normal login password):\n"
         " - Click 'App password'\n"
         "- Click 'Create app password', name it 'Parley', then Create.\n"
         "- Copy the app password it shows.\n"
         "(If the panel opens EMPTY, reload the page once - a Yahoo quirk.)",
         "yahoo-app-password.png"),
        {"form": [{"ask": "Yahoo address (you@yahoo.com)", "key": "address",
                   "default": prefill_address},
                  {"ask": "Yahoo app password (paste it here)", "key": "app_password",
                   "secret": True}],
         "image": "yahoo-app-password.png"},
    ]


def collect():
    console.say()
    console.say("-- Yahoo account --")
    prefill = None
    while True:
        answers = console.guide(_yahoo_pages(prefill))
        address = answers.get("address")
        password = answers.get("app_password")
        console.say("Verifying with a real IMAP login...")
        account, error = _yahoo_connect(address, password)
        if error is None:
            console.say(f"OK - {address} logs in.")
            break
        console.say(f"That did not work: {error}")
        console.say("Check the address, or generate a fresh app password, and try again (Ctrl+C aborts).")
        prefill = address  # keep the address; the stepper re-opens for a fresh password
    calendar = _pick_calendar(account)  # reuse the open connection's discovered calendars
    _close(account)
    timezone = _ask_timezone()
    result = {"backend": "yahoo", "address": address,
              "app_password": password, "timezone": timezone}
    if calendar:  # omit the cfg line entirely for a mail-only account
        result["calendar"] = calendar
    return result


def _yahoo_connect(address, password):
    """Connect through the REAL backend (compose registry -> YahooAccount.connect),
    so the wizard proves exactly what the agent does at startup AND discovers the
    CalDAV calendars. Returns (account, None) on success with the connection still
    OPEN (the caller reads account.list_calendars(), then closes it), or
    (None, error_string) on failure."""
    from parley.agent.compose import make_account
    from parley.client_server.config import AccountConfig
    cfg = AccountConfig(backend="yahoo", address=address,
                        settings={"address": address, "app_password": password})
    account = make_account(cfg)
    try:
        account.connect()
    except SystemExit as exc:  # backends abort loudly on missing creds
        return None, str(exc)
    except Exception as exc:
        return None, str(exc) or type(exc).__name__
    return account, None


def _close(account):
    """Politely close the probe IMAP connection."""
    try:
        account._imap.logout()
    except Exception:
        pass


def _pick_calendar(account):
    """Pin which Yahoo CalDAV calendar the agent books on, so it isn't left
    write-blocked at runtime (the runtime deliberately refuses to guess when >1
    exist). Reads `account.list_calendars()` — the generic Account primitive
    (2026-08-31 refactor: every backend implements it, not just Yahoo's own
    discovery) — auto-pins a lone calendar, offers a picker for several, and
    returns "" (mail-only, no cfg line) when none could be read. Returns the
    chosen calendar name (or id if unnamed)."""
    cands = account.list_calendars()
    if not cands:
        console.say("Calendar: no Yahoo calendar could be read (CalDAV may be off) - this")
        console.say("account stays mail-only: no own-calendar events, no conflict checks.")
        return ""
    if len(cands) == 1:
        name = cands[0]["name"] or cands[0]["id"]
        console.say(f"Calendar: using your only Yahoo calendar, '{name}'.")
        return name
    console.say()
    console.say("Calendar: you have more than one Yahoo calendar. Which should Parley")
    console.say("book your meetings on (and read for conflicts)?")
    for i, c in enumerate(cands, 1):
        console.say(f"  {i}. {c['name'] or '(unnamed)'}")
    while True:
        raw = console.ask("Pick a calendar (number or name)", default="1")
        chosen = _match_calendar(raw, cands)
        if chosen:
            console.say(f"  using '{chosen}'")
            return chosen
        console.say("  didn't match - enter one of the listed numbers or names.")


def _match_calendar(raw, cands):
    """The chosen calendar's name (or id when unnamed), matched by 1-based index,
    exact name, or a name/id substring; None on no match."""
    a = (raw or "").strip().lower()
    if not a:
        return None
    if a.isdigit():
        i = int(a) - 1
        return (cands[i]["name"] or cands[i]["id"]) if 0 <= i < len(cands) else None
    for c in cands:
        name = (c.get("name") or "").lower()
        if a == name or (name and a in name) or a in c["id"].lower():
            return c["name"] or c["id"]
    return None


def _ask_timezone():
    """The zone Parley reads local instruction times against (Yahoo doesn't report
    one to us) — prefilled from the computer, accepting either an IANA name OR a UTC
    offset (user feedback 2026-07-18). Without it every local time silently becomes UTC."""
    default = _system_timezone()
    console.say()
    console.say("Timezone: Parley needs your zone to read times like 'friday at 15:00'")
    console.say("correctly (Yahoo doesn't report one to us).")
    console.say("An IANA name (Europe/Madrid) or a UTC offset (UTC+2, GMT-5) both work.")
    while True:
        raw = console.ask("Timezone", default=default)
        zone = _normalize_timezone(raw)
        if zone:
            if zone != raw.strip():
                console.say(f"  using {zone}")
            return zone
        console.say(f"'{raw}' isn't a zone I recognise - use the IANA form (Europe/Madrid) "
                    "or a whole-hour offset like UTC+2; for half-hour zones use the IANA name.")


def _normalize_timezone(raw):
    """Return a ZoneInfo-loadable string for `raw`, or None. Accepts a valid IANA
    name as-is, or a UTC/GMT offset the user is likelier to know — UTC+2, GMT-5,
    CET+1 (the trailing number is read as hours from UTC) — mapped to an
    Etc/GMT zone. Half-hour offsets return None (ask for the IANA name)."""
    import re
    from zoneinfo import ZoneInfo
    text = (raw or "").strip()
    try:
        ZoneInfo(text)          # already a valid IANA name
        return text
    except Exception:
        pass
    m = re.search(r"([+-])\s*(\d{1,2})(?::(\d{2}))?\s*$", text)
    if not m or int(m.group(3) or 0):   # no offset, or a fractional one
        return None
    offset = (1 if m.group(1) == "+" else -1) * int(m.group(2))
    if not -14 <= offset <= 14:
        return None
    zone = f"Etc/GMT{-offset:+d}" if offset else "Etc/GMT0"  # Etc/GMT sign is inverted
    try:
        ZoneInfo(zone)
        return zone
    except Exception:
        return None


def _system_timezone():
    """Best-effort system IANA zone: tzlocal if present, else the Windows
    registry zone mapped through the ics table. None -> no prefill."""
    try:
        from tzlocal import get_localzone_name
        zone = get_localzone_name()
        if zone:
            return zone
    except Exception:
        pass
    try:  # Windows fallback (no tzlocal): TimeZoneKeyName -> IANA
        import winreg
        from parley.connectors.accounts.ics import WINDOWS_TZ
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SYSTEM\CurrentControlSet\Control\TimeZoneInformation") as k:
            name, _ = winreg.QueryValueEx(k, "TimeZoneKeyName")
        return WINDOWS_TZ.get(name)
    except Exception:
        return None
