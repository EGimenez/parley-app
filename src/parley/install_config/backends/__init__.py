"""Wizard backends — one module per account backend (iteration 25).

Each backend module is self-contained and exposes the same small surface:

    KEY          the backend id as it appears in the cfg (`backend: <KEY>`)
    MENU_LABEL   one line for the account menu
    ACCOUNT_ITEM the YAML `account:` list-item template (no policy trailer —
                 the orchestrator appends the shared one)
    collect()    the guided flow: ask, VERIFY LIVE, return the cfg-value dict
                 (always includes "backend"; a failure re-prompts, so nothing
                 unverified ever reaches the written cfg)

The registry below is the ONLY place that knows which backends exist: the
account menu, the YAML emitters, and `--add-account` all iterate it.
"""

from parley.install_config.backends import gmail, local_outlook, outlook, yahoo

# Menu order: cloud backends a new user most likely wants first.
ALL = [gmail, outlook, yahoo, local_outlook]
BY_KEY = {module.KEY: module for module in ALL}
