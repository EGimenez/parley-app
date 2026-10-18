"""The provider-agnostic I/O surface (and concrete backends) — the reusable
connector, self-contained (2026-08-31 refactor): no Parley-specific concept
lives in this package, on purpose, so it can be picked up wholesale by
unrelated projects.

Only the base `Account` is re-exported here: the concrete backends pull in
heavy provider SDKs (google-api, msal, pywin32), so they are imported lazily by
`parley.agent.compose.make_account` — `--selftest` must stay credential- and
library-free. The credential-free `TestAccount` (test_account.py, was
`memory.py`/`MemoryAccount`) is the exception used by the selftest.
"""

from parley.connectors.accounts.account import Account

__all__ = ["Account"]
