"""Gmail wizard backend: guided OAuth-app creation (Google Auth Platform
pages), the client handed over as the Download-JSON file (or pasted), then the
REAL consent flow runs immediately — the token lands under state/gmail/ before
the cfg exists, and the address is discovered from the Gmail profile, not
typed."""

import os

from parley.install_config import console

KEY = "gmail"
MENU_LABEL = "your own Google OAuth app + browser consent"

ACCOUNT_ITEM = """\
  - backend: gmail
    address: "{address}"            # discovered at the setup consent
    token: state/gmail/token.json   # generated at setup consent (under ~/.parley/)
    calendars: [{calendars}]        # first = booking target; extras widen busy/free
    # Provided OAuth client, INLINE (Google Cloud console -> Desktop-app client).
    google:
      client_id: "{client_id}"
      client_secret: "{client_secret}"
"""

# The guided-DIY page sequence: each console page in the order a first-timer
# needs it, with the 2-3 clicks to do there. The auth pages are the NEW
# "Google Auth Platform" screens (console.cloud.google.com/auth/...), not the
# old developer-oriented APIs & Services credentials UI. ~8 minutes total.
# One clean line per instruction (no mid-sentence line breaks or hanging indent)
# so the Setup Window and console both wrap it readably (feedback 2026-07-18).
_GCP_PAGES = [
    ("https://console.cloud.google.com/projectcreate",
     "Step 1 of 10: create (or pick) a Google Cloud project.",
     "gcp-1-project.png"),
    ("https://console.cloud.google.com/apis/library/gmail.googleapis.com",
     "Step 2 of 10: press Enable on the Gmail API. Check the project shown TOP-LEFT "
     "is your NEW one - the console likes to flip back to an older project.",
     "gcp-2-gmail-api.png"),
    ("https://console.cloud.google.com/apis/library/calendar-json.googleapis.com",
     "Step 3 of 10: press Enable on the Google Calendar API too.",
     "gcp-3-calendar-api.png"),
    # Steps 4-10 walk the Google Auth Platform 'Get started' wizard screen by
    # screen (user feedback 2026-07-18 with these exact screenshots) - it opens
    # once, then everything happens on that SAME tab.
    ("https://console.cloud.google.com/auth/overview",
     "Step 4 of 10: on the Google Auth Platform, click the blue 'Get started' "
     "button. Steps 5-10 then all happen on this same tab.",
     "gcp-4-get-started.png"),
    ("https://console.cloud.google.com/auth/overview",
     "Step 5 of 10 (App Information): type an App name (e.g. 'Parley'), pick your "
     "own email as the 'User support email', then click Next.",
     "gcp-4b-app-info.png", False),
    ("https://console.cloud.google.com/auth/overview",
     "Step 6 of 10 (Audience): choose 'External', then click Next.",
     "gcp-4c-audience.png", False),
    ("https://console.cloud.google.com/auth/overview",
     "Step 7 of 10 (Contact Information): type your email, click Next. Then on "
     "'Finish', tick 'I agree to the Google API Services: User Data Policy' and "
     "click Create.",
     "gcp-4d-contact.png", False),
    ("https://console.cloud.google.com/auth/branding",
     "Step 8 of 10 (Branding, in the LEFT MENU): publishing to production (next "
     "step) needs three URLs here first - Google won't let you Publish without "
     "them:\n"
     "- App homepage: https://egimenez.github.io/parley-app-info/\n"
     "- App Privacy policy link: https://egimenez.github.io/parley-app-info/privacy.html\n"
     "- App Terms of Service link: https://egimenez.github.io/parley-app-info/terms.html\n"
     "These are already-published, ready-made pages - paste them as-is, no need to "
     "build your own. Then scroll to 'Authorized domains', click '+ Add domain', "
     "enter egimenez.github.io, and Save.",
     None, False),
    ("https://console.cloud.google.com/auth/audience",
     "Step 9 of 10 (Audience, in the LEFT MENU): under 'Publishing status' click "
     "'Publish app' and confirm, so the status becomes 'In production'. This stops "
     "Google expiring your login every 7 days (a Testing-mode limit). Your app "
     "stays UNVERIFIED - fine for a private, single-user app: no verification "
     "needed, and the 100-user cap never bites (its only user is you).",
     None, False),  # screenshot dropped: gcp-5-audience.png shows the old Test-users step
    ("https://console.cloud.google.com/auth/clients",
     "Step 10 of 10 (Clients, in the LEFT MENU): '+ Create client' -> application "
     "type 'Desktop app' -> Create.\n"
     "- The dialog shows the client id AND secret.\n"
     "- Press 'Download JSON' (save it in your Downloads folder - I'll find it there).\n"
     "- Then OK (the secret is not shown again).",
     "gcp-6-client.png", False),
]

# Shown right before the real browser consent so the 'Google hasn't verified
# this app' screen (expected for a private, unverified app) doesn't scare
# the user into pressing 'Back to safety' (round-3 feedback 2026-07-18, image1).
_CONSENT_PREVIEW = (
    "https://console.cloud.google.com/",
    "One more browser step - I'll open it next. Because your app is unverified "
    "(private, one user), Google shows the screen below: 'Google hasn't verified "
    "this app'. That's expected - do NOT click 'Back to safety'. Click 'Advanced', "
    "then 'Go to [your app name] (unsafe)', tick 'Select all', and 'Continue' / "
    "'Allow' to grant all access.",
    "gmail-consent-continue.png", False)


def collect():
    console.say()
    console.say("-- Gmail account --")
    console.say("Google requires your own (free) OAuth app - a one-time console dance.")
    console.say("I'll open each page; do the step there, then press NEXT (BACK to revisit).")
    # Guided all the way (Google blocks sign-in inside automation-controlled
    # browsers). The nine console pages are one BACK/NEXT stepper; the client
    # id + secret are then collected as two more input steps in their own
    # stepper, prefilled from the 'Download JSON' file if you saved it.
    console.guide(_GCP_PAGES)
    cred_pages = [
        {"ask": "OAuth client ID (ends in .apps.googleusercontent.com) - "
                "from the client dialog, or the Download JSON file",
         "key": "client_id", "default": None, "image": "gcp-6-client.png"},
        {"ask": "OAuth client secret (the same dialog; kept only in your private cfg)",
         "key": "client_secret", "default": None, "secret": True,
         "image": "gcp-6-client.png"},
    ]
    json_path = None
    client_id = client_secret = None
    shown_preview = False
    while True:
        if client_id is None:
            # Re-scan Downloads on every attempt so a JSON downloaded AFTER a
            # failed try (e.g. after fixing a deleted-project client) is picked
            # up; a found file's id/secret prefill the fields, else the last
            # typed value stays as the default for a quick fix.
            pre_id, pre_secret, found = _prefill_from_downloads()
            if found:
                json_path = found
                cred_pages[0]["default"] = pre_id
                cred_pages[1]["default"] = pre_secret
            answers = console.guide(cred_pages)  # BACK/NEXT between id and secret
            client_id = (answers.get("client_id") or "").strip()
            client_secret = console._clean_secret(answers.get("client_secret") or "")[0]
            cred_pages[0]["default"] = client_id       # survive a consent retry
            cred_pages[1]["default"] = client_secret
        if not shown_preview:
            console.guide([_CONSENT_PREVIEW])  # what the consent screen looks like
            shown_preview = True
        console.working("Opening Google's consent in your browser:\n"
                        "- Pick your Gmail account.\n"
                        "- On 'Google hasn't verified this app', click 'Advanced', then\n"
                        "  'Go to [your app name] (unsafe)'.\n"
                        "- Tick 'Select all', then click 'Continue'.\n"
                        "- 'Allow' all access.\n"
                        "This window continues once you've consented.")
        address, error, account = _gmail_consent(client_id, client_secret)
        if error is None:
            console.say(f"OK - consented as {address}; token saved under ~/.parley/state/gmail/.")
            break
        console.say(f"That did not work: {error}")
        low = (error or "").lower()
        if "has been deleted" in low or ("project" in low and "delet" in low):
            # The client id belongs to a Google Cloud project that no longer
            # exists - almost always a STALE client_secret*.json picked up from
            # Downloads (an earlier, since-deleted project), not the one you just
            # made. Force a fresh entry, don't re-prefill the dead client.
            console.say("The OAuth client you used belongs to a Google Cloud PROJECT that has been")
            console.say("DELETED, so its APIs can't be reached. This is usually a LEFTOVER")
            console.say("client_secret*.json in your Downloads from an older project. Fix it:")
            console.say("  1. Delete any old client_secret*.json from your Downloads folder.")
            console.say("  2. In the project you just set up, download the Desktop client's JSON")
            console.say("     again (step 9) so I read the LIVE one.")
            cred_pages[0]["default"] = cred_pages[1]["default"] = None
        elif ("accessnotconfigured" in low or "has not been used" in low
              or "service_disabled" in low or "is disabled" in low):
            console.say("A required API is not enabled on this client's project. Enable BOTH the")
            console.say("Gmail API and the Google Calendar API (steps 2-3), wait a minute, retry.")
        else:
            console.say("Re-check the client, and that BOTH APIs are enabled (Ctrl+C aborts).")
        client_id = client_secret = None  # re-ask the id/secret step
    if json_path and console.ask_yn(
            f"Delete {os.path.basename(json_path)}? (its contents now live in your cfg; "
            "Downloads is no place for a secret)", default=True):
        try:
            os.remove(json_path)
            console.say("Deleted.")
        except OSError as exc:
            console.say(f"Could not delete it ({exc}) - remove it yourself.")
    return {"backend": "gmail", "address": address, "client_id": client_id,
            "client_secret": client_secret, "calendars": _ask_calendars(account)}


def _prefill_from_downloads():
    """The easy path: the 'Download JSON' file from the client dialog, auto-found
    in ~/Downloads. If present and readable, return (client_id, client_secret,
    path) to PREFILL the credential input steps (the user just presses NEXT);
    else (None, None, None) and they paste it themselves."""
    path = _find_client_json()
    if path is None:
        return None, None, None
    try:
        client_id, client_secret = _parse_client_json(path)
        console.say(f"OK - read {os.path.basename(path)}: I'll prefill the client id + secret.")
        return client_id, client_secret, path
    except Exception as exc:  # unreadable / wrong file — say why, fall back to paste
        console.say(f"Could not use that file: {exc} - paste the id + secret instead.")
        return None, None, None


def _find_client_json():
    """Newest client_secret*.json in ~/Downloads, confirmed before use."""
    import glob
    downloads = os.path.join(os.path.expanduser("~"), "Downloads")
    candidates = sorted(glob.glob(os.path.join(downloads, "client_secret*.json")),
                        key=os.path.getmtime, reverse=True)
    if candidates and console.ask_yn(
            f"Found {os.path.basename(candidates[0])} in Downloads - use it?", default=True):
        return candidates[0]
    return None


def _parse_client_json(path):
    """client_id + client_secret out of Google's client JSON ('installed' for
    Desktop-app clients; 'web' tolerated)."""
    import json
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    entry = data.get("installed") or data.get("web") or {}
    client_id, client_secret = entry.get("client_id", ""), entry.get("client_secret", "")
    if not (client_id and client_secret):
        raise ValueError("no client_id/client_secret inside - is this the OAuth "
                         "client JSON from the 'Download JSON' button?")
    return client_id, client_secret


def _gmail_consent(client_id, client_secret):
    """Run the REAL backend's connect(): browser consent, token written under
    state/gmail/, address read from the Gmail profile; then the sanctioned
    events.list probe (timezone()) exercises the Calendar API too.
    Returns (address, None, account) on success, (None, error, None) on failure.

    A setup consent must ALWAYS re-authorise with the client the user just
    entered, so we WIPE any leftover token.json first. Otherwise a token from a
    prior attempt (e.g. one made against a since-deleted project) is still
    'valid', so connect() reuses it and NEVER re-opens the browser - silently
    ignoring the new client, and failing forever on the dead project's first API
    call (user report 2026-07-21)."""
    from parley.agent.compose import make_account
    from parley.client_server.config import AccountConfig, resolve_state_path
    state_dir = resolve_state_path(os.path.join("state", "gmail"))
    token_file = os.path.join(state_dir, "token.json")
    try:
        if os.path.exists(token_file):
            os.remove(token_file)  # force a fresh consent with THIS client
    except OSError:
        pass
    cfg = AccountConfig(
        backend="gmail", calendars=["primary"],
        settings={"google": {"client_id": client_id, "client_secret": client_secret},
                  "state_dir": state_dir})
    account = make_account(cfg)
    try:
        account.connect()
        account.timezone()  # best-effort Calendar API probe (never raises)
    except SystemExit as exc:
        return None, str(exc), None
    except Exception as exc:
        return None, str(exc) or type(exc).__name__, None
    return account.address(), None, account


def _list_calendars(account):
    """(id, name, is_primary) for every calendar on the consented account, via
    the read-only calendarList API. [] if it can't be read."""
    try:
        resp = account._cal.calendarList().list().execute()
        return [(it["id"], it.get("summary", it["id"]), bool(it.get("primary")))
                for it in resp.get("items", [])]
    except Exception:
        return []


def _ask_calendars(account):
    """'primary' is always the booking target; the user PICKS which other
    calendars to fold into busy/free from the real list (iteration 26 D4a) —
    no more typing raw calendar ids."""
    console.say()
    cals = _list_calendars(account)
    others = [(cid, name) for cid, name, is_primary in cals if not is_primary]
    if not others:
        console.say("Calendars: using your primary calendar as the booking target.")
        return ["primary"]
    console.say("Calendars: your primary calendar is the booking target. Tick any others")
    console.say("to include - their busy times are added so Parley won't double-book:")
    for i, (cid, name) in enumerate(others, 1):
        console.say(f"  {i}. {name}")
    picked = console.ask("Extra calendars to include (comma-separated numbers, Enter for none)",
                         default="")
    chosen = [others[int(t) - 1][0] for t in
              (tok.strip() for tok in picked.split(",")) if t.isdigit() and 1 <= int(t) <= len(others)]
    return ["primary"] + chosen
