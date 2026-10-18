"""Selftest driver helper.

The in-memory mailbox + calendar double now lives with the connectors as the
first-class `TestAccount` / `MemoryBus` (parley.connectors.accounts.test_account)
— there is no bespoke "fake connection" anymore. This module keeps only
`_run_bus`, which drives a set of (agent, account) pairs over a shared bus
until the negotiation stops making progress.
"""


def _run_bus(pairs, rounds=12):
    """Drive (agent, account) pairs over the bus until no one makes progress."""
    for _ in range(rounds):
        progressed = False
        for agent, conn in pairs:
            for message in conn.list_messages():
                agent.handle(message, conn)
                progressed = True
        if not progressed:
            break
