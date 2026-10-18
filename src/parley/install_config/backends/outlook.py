"""Outlook / Microsoft 365 wizard backend: guided Azure registration, then the
REAL MSAL interactive consent runs immediately (cache under state/outlook/)
and the address comes from the signed-in Microsoft account.

Guided only (iteration 26): assisted mode drove the Azure pages through
Playwright, but Microsoft's portal fought the driven browser too often to be
reliable, so it was removed - the guided stepper (BACK / NEXT, one screenshot
per page) is the single path now."""

import os

from parley.install_config import console

KEY = "outlook"
MENU_LABEL = "your own Azure app + Microsoft browser consent (guided)"

ACCOUNT_ITEM = """\
  - backend: outlook
    address: "{address}"                         # discovered at the setup consent
    client_id: "{client_id}"                     # Azure public-client app id (not secret)
    msal_cache: state/outlook/msal_cache.json    # generated at setup consent (under ~/.parley/)
    delta: state/outlook/delta.txt               # generated inbox delta cursor
    calendars: [primary]
"""

def _perm_rows():
    """(group, permission) per delegated Graph scope, order preserved. `group`
    is the family the Azure 'Add permissions' search files each permission under
    (Mail / Calendars / MailboxSettings). Derived from the REAL backend scopes
    (accounts.outlook.SCOPES) so the wizard and the agent can never drift."""
    from parley.connectors.accounts.outlook import SCOPES
    return [(s.split(".", 1)[0], s) for s in SCOPES]


def _perm_block(indent="    "):
    """The delegated scopes grouped one family per line, e.g.

        Calendars: Calendars.ReadWrite, Calendars.ReadWrite.Shared
        MailboxSettings: MailboxSettings.Read
        Mail: Mail.Read, Mail.Send

    Derived from the real backend scopes (_perm_rows) so nothing drifts. The
    families are shown in the order a first-timer adds them; any family not in
    that list is still appended, so a new scope can never silently vanish."""
    from collections import OrderedDict
    groups = OrderedDict()
    for group, perm in _perm_rows():
        groups.setdefault(group, []).append(perm)
    order = ["Calendars", "MailboxSettings", "Mail"]
    families = [g for g in order if g in groups] + [g for g in groups if g not in order]
    return "\n".join(f"{indent}{g}: {', '.join(groups[g])}" for g in families)


# One clean line per instruction (no mid-sentence breaks or hanging indent) so
# it wraps readably in the Setup Window and the console (feedback 2026-07-18).
def _azure_pages():
    """Guided page sequence, built fresh so the permission list always matches
    the backend's real scopes (_perm_block). Rendered by console.guide() as a
    BACK / NEXT stepper. Three semantic steps span it: register (1), add
    permissions (2), and the sign-in consent (3, run by collect()). The
    Application (client) ID is collected as an input step AFTER the app-list
    page opens (feedback 2026-07-20): the user opens the app, lands on its
    Overview page - where the ID lives - and copies it right there, before the
    API-permissions menu navigates away from Overview."""
    return [
        # Step 1: land directly on the 'Register an application' FORM (feedback
        # 2026-07-20) - no 'App registrations -> New registration' hop first.
        ("https://portal.azure.com/#view/Microsoft_AAD_RegisteredApps/CreateApplicationBlade/quickStartType~/null/isMSAApp~/false",
         "Step 1 of 3 - register the app (this page IS the 'Register an application' form):\n"
         "- NAME: type a name for the app (e.g. 'Parley').\n"
         "- Supported account types: pick 'Any Entra ID Tenant + Personal Microsoft accounts'.\n"
         "- Redirect URI: pick 'Public client/native (mobile & desktop)' and type http://localhost\n"
         "- Press Register. You land on the Overview page - I'll ask for its Application (client) ID next.\n"
         "(Azure may briefly flash a red 'creating apps outside a directory is deprecated' error - ignore it, the form loads a few seconds later.)",
         "azure-1-register.png", True, False),  # opens the browser; the raw deep-link URL is noise, don't display it (feedback 2026-07-21)
        # Step 2 opens on the app LIST: open the app just registered - it lands
        # on the Overview page, where the Application (client) ID lives, so the
        # input step below reads it off the same open tab.
        ("https://portal.azure.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade",
         "Step 2 of 3 - open your app:\n"
         "- Open your new app (if it isn't listed, switch 'Owned applications' -> 'All applications').\n"
         "- It opens on the Overview page - I'll ask for the Application (client) ID"),
        # Input step in the same stepper: the Overview page (just opened) is the
        # open tab, so the Application (client) ID is right there to copy.
        {"ask": "Application (client) ID - on the app's Overview page (now open), copy the value "
                "labelled 'Application (client) ID' (click the copy icon beside it), "
                "then paste it here",
         "key": "client_id"},
        # Same tab (opens=False): still the Overview page, now use the left menu
        # to add the Graph delegated permissions.
        ("https://portal.azure.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade",
         "Step 2 of 3 - add permissions:\n"
         "- LEFT MENU: Manage -> API permissions -> Add a permission -> Microsoft Graph -> Delegated permissions.\n"
         "- Add these scopes:\n"
         f"{_perm_block()}\n"
         "- Click 'Add permissions'. (No client secret - it's a public client.)",
         "azure-2-permissions.png", False),
    ]


def collect():
    console.say()
    console.say("-- Outlook / Microsoft 365 account --")
    console.say("Microsoft needs a (free) app registration - one ID to copy, no secret.")
    console.say()
    # Guided stepper: register -> copy the Application ID -> add permissions, all
    # with BACK / NEXT (the Application ID is an input step between the pages).
    answers = console.guide(_azure_pages())
    client_id = answers.get("client_id") or console.ask("Application (client) ID (a GUID)")
    while True:
        console.working("Step 3 of 3 - sign in and consent: opening Microsoft's sign-in in your "
                        "browser - sign in, then click 'Accept' on the permissions screen. This "
                        "window continues on its own once you've consented.")
        address, error = _outlook_consent(client_id)
        if error is None:
            console.say(f"OK - consented as {address}; MSAL cache saved under ~/.parley/state/outlook/.")
            break
        console.say(f"That did not work: {error}")
        low = error.lower()
        if "unauthorized_client" in low or "not enabled for consumers" in low:
            # The app was just registered, so either Microsoft hasn't propagated
            # it yet, or its account types exclude personal accounts.
            console.say("This 'unauthorized_client / not enabled for consumers' error usually")
            console.say("means the app registration is BRAND NEW and Microsoft has not finished")
            console.say("propagating it yet - wait a minute and retry. If it persists, open the")
            console.say("app's Overview page and confirm 'Supported account types' includes")
            console.say("personal Microsoft accounts ('All Microsoft account users').")
        console.say("Re-check the ID and the Graph delegated permissions (Ctrl+C aborts).")
        client_id = console.ask("Application (client) ID (a GUID)")
    return {"backend": "outlook", "address": address, "client_id": client_id}


def _outlook_consent(client_id):
    """connect() then force one token acquisition through the REAL backend —
    that's the interactive consent; the serialized cache lands under
    state/outlook/. Returns (address, None) or (None, error).

    A setup consent must ALWAYS re-authorise with the client the user just
    entered, so we WIPE any leftover MSAL cache first. Otherwise a cache from a
    prior attempt (e.g. a since-deleted or wrong app registration) still holds a
    signed-in account, so `acquire_token_silent` succeeds and connect() never
    re-opens the browser - silently ignoring the new client (mirrors the Gmail
    stale-token trap, 2026-07-21)."""
    from parley.agent.compose import make_account
    from parley.client_server.config import AccountConfig, resolve_state_path
    state_dir = resolve_state_path(os.path.join("state", "outlook"))
    cache_file = os.path.join(state_dir, "msal_cache.json")
    try:
        if os.path.exists(cache_file):
            os.remove(cache_file)  # force a fresh consent with THIS client
    except OSError:
        pass
    cfg = AccountConfig(
        backend="outlook", calendars=["primary"],
        settings={"client_id": client_id, "state_dir": state_dir})
    account = make_account(cfg)
    try:
        account.connect()
        account._token()  # first acquisition = the interactive browser consent
        accounts = account._app.get_accounts()
        address = accounts[0]["username"] if accounts else account.address()
    except SystemExit as exc:
        return None, str(exc)
    except Exception as exc:
        return None, str(exc) or type(exc).__name__
    if not address:
        return None, "consent finished but no signed-in account was found"
    return address, None
