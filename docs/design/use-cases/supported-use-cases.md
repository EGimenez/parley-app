# Parley — Supported Use Cases (status ledger)

> Pure status tracking for [use-cases.md](use-cases.md) (capability UCs) and
> [interface-use-cases.md](interface-use-cases.md) (surfaces). No descriptions here — follow
> the link. Legend: ✅ shipped · 🟡 partial · ⬜ designed, not built.

## Capability UCs

| UC | Status |
|---|---|
| [UC-0 — Delegated booking](use-cases.md#uc-0--delegated-booking-open-slot-exists) | ✅ autonomous booking · ⬜ ranked-pick UI |
| [UC-1 — Reschedule](use-cases.md#uc-1--reschedule-an-existing-meeting) | ✅ one-off meetings · ⬜ recurrence scope |
| [UC-2 — Multi-party](use-cases.md#uc-2--multi-party-convergence) | ✅ |
| [UC-3 — Make room](use-cases.md#uc-3--no-open-slot-exists--make-room--the-wedge) | 🟡 single-level · ⬜ recursive cascade |
| [UC-4 — No solution](use-cases.md#uc-4--no-solution-exists) | ⬜ falls back to `ask_user`, no structured trade-off report yet |
| [Human steps in](use-cases.md#cross-cutting-true-of-every-uc-above) | 🟡 first-write-wins ✅ · change-feed watch ⬜ |
| [Multi-calendar](use-cases.md#cross-cutting-true-of-every-uc-above) | 🟡 union + one writable target ✅ · calendar roles ⬜ |

## Interface UCs

| IUC | Status |
|---|---|
| [Tray (Win-Alt-P)](interface-use-cases.md#live) | ✅ |
| [Skill](interface-use-cases.md#live) | ⬜ in design — the connectors-package refactor |
| CLI (`parley`, `--watch`) | ✅ |
| Email-to-self | 🛑 retired 2026-08-31, not replaced — no away-from-desk surface today |
| Choosing the outbound account when one agent holds several | 🛑 not supported — instructions always use the first `accounts:` entry |
| Calendar-native / web-mobile / chat bot | ⬜ future |
