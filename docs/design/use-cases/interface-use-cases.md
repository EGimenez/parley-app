# Parley Interface Use Cases (IUC)

> Sibling of [use-cases.md](use-cases.md) — that answers *what* the human delegates, this
> answers *through which surface*. Two are live; the rest are retired-but-recoverable through
> the abstract `Client` base (see the refactor's Clients decision), or future.

## The channel is bidirectional

| Direction | Examples | Where the human is |
|---|---|---|
| Inbound (human → agent) | delegate, override, relax a constraint | usually at a desk |
| Outbound (agent → human) | "pick a slot", "I'm stuck", a conflict | anywhere — latency-sensitive |

**Always-present, un-removable:** the raw calendar + inbox — every surface is additive on top
of it, and the agent always reconciles against direct human action (use-cases.md, cross-cutting).

## Live

| IUC | Surface | Note |
|---|---|---|
| **Tray** | Win-Alt-P popup + toasts | the one interactive human-gate surface today |
| **Skill** | Claude Code / Claude Desktop, direct calls into the connectors package | no poll loop — the invocation *is* the wake cycle (see the refactor's Loop) |

## Retired for now (recoverable via the `Client` base class)

| IUC | Surface | Why |
|---|---|---|
| CLI, interactive use | answering pending questions from a terminal | superseded by the tray; CLI stays for admin (setup / status / stop) |
| Email-to-self | instruct/answer by replying to your own inbox | superseded by the tray; re-adding it is one new `Client` |

## Future (design only)

| IUC | Surface |
|---|---|
| Calendar-native | tentative events; the human picks via the calendar's own UI |
| Web / mobile | hosted, push-capable |
| Chat bot | Slack / Teams webhook |
