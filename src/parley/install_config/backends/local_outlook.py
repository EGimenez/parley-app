"""Classic desktop Outlook wizard backend: no cloud setup — the probe proves that
classic Outlook (not the COM-less 'new Outlook') answers on THIS machine, and then
asks WHICH of its accounts this agent speaks for."""

import os

from parley.install_config import console

KEY = "local-outlook"
MENU_LABEL = ("classic desktop Outlook on THIS machine - the usual pick for a "
              "CORPORATE account (COM, no cloud app to register)")

ACCOUNT_ITEM = """\
  - backend: local-outlook
    # Classic desktop Outlook on THIS machine via COM (probed at setup). `address`
    # SELECTS which account of the profile this agent is: its inbox, its calendar
    # and its outgoing mail. Change it only to a different account of the same
    # profile - the backend refuses to start if it is not there.
    address: "{address}"
"""


def collect():
    # No cloud pages here, so no BACK / NEXT stepper (nothing to step through):
    # one line of context, then straight to the COM probe. A failed probe offers
    # Retry / Cancel rather than aborting the whole wizard (user feedback
    # 2026-07-21: a "1 of 2 - press NEXT" info page was just noise here).
    console.say()
    console.say("-- Classic desktop Outlook (this machine) --")
    console.say("No cloud setup and no app to register: Parley drives the classic Outlook")
    console.say("already signed in on this PC over COM. This is normally how a CORPORATE")
    console.say("account is used, since companies rarely hand out their own app")
    console.say("registration. (Only CLASSIC Outlook works; 'new Outlook' has no COM.)")
    while True:
        console.working("Probing the installed Outlook over COM - if Outlook shows a\n"
                        "security prompt asking to allow access, click Allow.")
        accounts, current, error = _local_outlook_probe()
        if error is None:
            return {"backend": "local-outlook", "address": _pick(accounts, current)}
        console.say(f"That did not work: {error}")
        console.say("This backend needs Windows + CLASSIC Outlook with a configured MAPI")
        console.say("profile ('new Outlook' has no COM). Start classic Outlook, then retry,")
        console.say("or cancel and use the outlook (Graph) backend instead.")
        if not console.ask_yn("Retry the Outlook probe?", default=True,
                              yes_label="Retry", no_label="Cancel"):
            raise SystemExit("Setup aborted - local-outlook is not usable on this machine.")


def _pick(accounts, current):
    """Which account of the profile is this agent?

    Asked rather than guessed, because a profile routinely holds two (a work one and
    a personal one) and the two COM ways of answering "who am I" disagree: the
    identity Outlook reports is not necessarily the mailbox its default folders
    belong to. Guessing there is how an agent ends up introducing itself as one
    person while reading another's mail (found live 2026-09-19). Whatever is chosen
    here is written as `address:`, which the backend then uses to SELECT the inbox,
    the calendar and the sending account — so this one answer settles all three."""
    if not accounts:
        # No SMTP address readable (an unusual profile). Fall back to the identity
        # COM reports, which is what the backend would have used anyway.
        console.say(f"OK - classic Outlook answers as {current}.")
        return current
    if len(accounts) == 1:
        console.say(f"OK - classic Outlook answers as {accounts[0]}.")
        return accounts[0]
    console.say()
    console.say(f"This Outlook profile holds {len(accounts)} accounts. Pick the ONE this")
    console.say("agent speaks for - its mailbox, its calendar, and the address its mail")
    console.say("goes out from. (Another agent can have the other one: run setup again.)")
    options = [(address, "the identity Outlook reports for you" if address == current
                else "also in this profile") for address in accounts]
    return console.ask_choice("Which account should this agent manage?", options,
                              default=current if current in accounts else accounts[0])


def _local_outlook_probe():
    """(accounts, current_user, error). Connects with NO address so the probe works
    before we know which one to ask for; the answer feeds the cfg we are writing."""
    from parley.agent.compose import make_account
    from parley.client_server.config import AccountConfig, resolve_state_path
    cfg = AccountConfig(
        backend="local-outlook",
        settings={"state_dir": resolve_state_path(os.path.join("state", "local-outlook"))})
    account = make_account(cfg)
    try:
        account.connect()
    except SystemExit as exc:
        return [], None, str(exc)
    except Exception as exc:
        return [], None, str(exc) or type(exc).__name__
    current = account.address() or "(unknown address)"
    addresses = []
    try:
        accounts = account._ns.Accounts
        for i in range(accounts.Count):
            smtp = (getattr(accounts.Item(i + 1), "SmtpAddress", "") or "").strip()
            if smtp and smtp not in addresses:
                addresses.append(smtp)
    except Exception:  # noqa: BLE001 — an unreadable account list is not fatal
        pass
    return addresses, current, None
