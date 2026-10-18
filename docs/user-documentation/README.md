# Parley — User Guide

Parley gives each person a **personal scheduling agent**. Your agent watches your
mailbox + calendar and negotiates meeting times **agent-to-agent** with other
people's Parley agents over ordinary email — so you stop playing calendar Tetris.
You stay in control: your **policy** (plain English) tells the agent when to act on
its own and when to **stop and ask you** before it commits.

> This is the operator/end-user guide. For the design rationale see
> [`../README.md`](../README.md) (the docs index).

## Index

1. [Concepts in 60 seconds](#1-concepts-in-60-seconds)
2. [Install](#2-install)
3. [Configure your agent](#3-configure-your-agent) —
   [brain's engine](#the-brains-engine-claude-code-or-an-api-key) ·
   [secrets](#where-secrets-live-and-what-to-guard) ·
   [policy](#policy--write-it-like-youd-brief-an-assistant) ·
   [pricing collisions](#pricing-collisions--standing-movability-offer-bounds) ·
   [one agent per account](#one-agent-per-account-recommended) ·
   [trust table](#the-trust-table--who-may-trigger-what) ·
   [mail triage](#smarter-mail-triage-optional) ·
   [restart safety](#restart-safety-dont-miss-meetings-while-the-agent-is-off)
4. [Run your agent](#4-run-your-agent)
5. [Everyday use](#5-everyday-use) —
   [book](#book-a-meeting) ·
   [ASK/END](#every-command-ends-in-ask-or-end) ·
   [what's in flight](#ask-whats-in-flight--and-steer-it) ·
   [no double-booking](#it-will-never-silently-double-book) ·
   [later questions](#questions-that-arrive-later) ·
   [reactive](#reactive-negotiation)
6. [The Desktop App (tray icon, toasts, Win-Alt-P)](#6-the-desktop-app-tray-icon-toasts-win-alt-p)
7. [Check the install](#7-check-the-install-optional)
8. [Troubleshooting](#8-troubleshooting)
9. [What it does *not* do yet](#9-what-it-does-not-do-yet)
- [Appendix — Backend setup](#appendix--backend-setup-credentials-per-provider):
   [gmail](#gmail-backend-gmail) ·
   [outlook](#outlook--microsoft-365-backend-outlook) ·
   [yahoo](#yahoo-backend-yahoo) ·
   [classic Outlook](#classic-outlook-on-this-machine-backend-local-outlook)

---

## 1. Concepts in 60 seconds

| Term | What it is |
|---|---|
| **Agent** | One per person. Drives your account(s), runs the negotiation. |
| **Brain** | The agent's single decision-maker: an LLM that handles every event (a request, an invite, your instruction, your answer) following **your policy prose**. Runs on the Claude Code CLI (your claude.ai login, no key) or on an Anthropic API key — see [The brain's engine](#the-brains-engine-claude-code-or-an-api-key). |
| **Account** | One mailbox + calendar + contacts (Gmail, Outlook/M365, Yahoo, or classic desktop Outlook). An agent can drive several. |
| **Gateway** | Triages each incoming message — admit it to the brain, or ignore it — so noise never reaches (or costs) the reasoning. |
| **Policy** | Plain-English instructions per account (*"ask me before accepting new meetings"*). Guidance you give in answers is **remembered** as standing policy. |
| **Client–server channel** | How *you* talk to *your* agent: the CLI or the Desktop App (tray), both over local HTTP. |
| **Peer-to-peer** | How your agent talks to other agents: ordinary emails with a small data block in the body. |
| **Journal** | The flight recorder: every message received and every decision made, one JSON line at a time, under `~/.parley/logs/<agent>/`. |

---

## 2. Install

**Windows, one click:** double-click **`scripts\install-windows-gui.vbs`** in the
unzipped folder (no console window at any point; `scripts\util\install-windows.bat`
does the same in a terminal). It checks Python 3.11+, installs the `parley` CLI + tray app (repairing PATH if
needed), adds a Start-Menu entry, offers tray autostart at login, and ends by
offering the **setup wizard** (§3). `scripts\util\uninstall-windows.bat` reverses it
(your `~/.parley` configs stay unless you pass `-PurgeData`).

**Manual (macOS / Linux / Windows):**

```bash
python -m venv .venv
.venv\Scripts\Activate.ps1        # Windows PowerShell  (macOS/Linux: source .venv/bin/activate)
pip install -e .                  # enables `parley`, `parley_desktop`, `parley-server`, `python -m parley`
pip install -e .[desktop]         # + the tray extras (pystray, Pillow, windows-toasts)
```

All dependencies come from `pyproject.toml` — there is no `requirements.txt`.
`pywin32` (for the classic-Outlook backend) and **`tzdata`** (without which timezone
conversion fails loudly at startup) install automatically on Windows; the Gmail,
Outlook/Graph and Yahoo backends are cross-platform. No install at all? The
`scripts/util/parley.bat` / `scripts/util/parley.sh` shims run the same CLI straight
from the repo.

**`parley` not recognized after a manual install?** A user-level pip install can
put the launcher exes in a Scripts dir that isn't on PATH. The Windows installer
repairs this automatically; by hand, add the dir that
`python -c "import sysconfig; print(sysconfig.get_path('scripts', 'nt_user'))"`
prints to your user PATH (then open a new terminal), or just use
`python -m parley`.

---

## 3. Configure your agent

**The easy way — the setup wizard (zero file editing):**

```bash
parley --setup
```

The **Setup Window** opens — it takes the right half of your screen and **stays
on top**, so you can work through the Google / Azure / Yahoo pages on the left
while the instructions (with screenshots) never disappear. Type your answers in
the window and press **NEXT** (**BACK** revisits a guided page). It asks for
one account (gmail / outlook / yahoo / classic Outlook) and the brain's engine —
Claude Code needs **no API key** (see [The brain's engine](#the-brains-engine-claude-code-or-an-api-key)) —
**verifies everything live** — model ping, OAuth consent in the browser,
IMAP login — writes `~/.parley/cfg_<name>.yaml`, and offers to make it your
default agent. For gmail/outlook it walks you through the console pages one at
a time. Prefer the terminal? `parley --setup --console` keeps the classic
console wizard (also what pipes/SSH get automatically). Add more accounts to an
existing agent later with `parley --add-account` — but read [One agent per
account](#one-agent-per-account-recommended) first. Any `parley` command that
finds no config offers to run the wizard for you.

**The manual way** — **one private config file = one complete agent**, living in
`~/.parley/` — *outside* the repo and any cloud-synced folder. On first run Parley
seeds `~/.parley/` with per-backend templates (`cfg.gmail.example.yaml`,
`cfg.outlook.example.yaml`, `cfg.yahoo.example.yaml`); copy one and fill it in:

```bash
cp ~/.parley/cfg.gmail.example.yaml ~/.parley/cfg_gmail.yaml
```

Minimal shape:

```yaml
llm:                                     # the brain — REQUIRED for the agent to act
  anthropic_key: "sk-ant-…"              # or anthropic_key_file: "…"
  model: "opus"                          # required with a key; there is no default.
                                          # The brain wants a capable model; the cheap
                                          # Haiku belongs on the gateway classifier (§3).

account:
  - backend: gmail
    address: you@gmail.com
    calendars: [primary]                 # busy/free union; calendars[0] = booking target

    google:                              # backend secrets, inline
      client_id: "…"
      client_secret: "…"

    policy:
      unstructured: |
        Ask me before accepting new meetings.
        Prefer mornings. Meetings with customers take priority.
      # accept_requests_from: [teammate@company.com]   # structured seam (optional)
```

**Always the newest model.** A family name — `opus`, `sonnet` or `haiku` — means the
newest model of that family. Parley looks it up each time the agent starts, so a new
release is picked up without editing the cfg. The journal records the exact model in
use (`llm_model` entries). A full id such as `claude-opus-4-8` pins one release instead.

### The brain's engine: Claude Code or an API key

| Engine | What you need | Trade-off |
|---|---|---|
| **Claude Code** (recommended when installed) | The [Claude Code](https://claude.com/claude-code) CLI on PATH and logged in. **No API key.** | Runs on your claude.ai login. Each step is slower (about 15–50 s), and each call carries about 25k tokens of Claude Code's own prompt. |
| **Anthropic API key** | A key from [console.anthropic.com](https://console.anthropic.com/). | Faster per step. Billed per token. |

The wizard offers Claude Code only when it finds `claude` on PATH, and proves it
with one tiny `claude -p` call before it writes anything. By hand, the `llm:`
block for Claude Code is:

```yaml
llm:
  provider: claude-code      # no anthropic_key
  model: "opus"              # always the newest Opus
```

**Change the engine later** with `parley --engine -cfg <name>`, or in the tray
(**Settings… → Engine…**). It asks the same questions, verifies live, and changes
only the engine keys of `llm:` / `llm-gateway:`. Accounts, policy, comments and
your own edits stay. The previous file is kept as `cfg_<name>.yaml.bak`. Running
`parley --setup` on an agent that exists offers the same "change only the engine".

The gateway (§[Smarter mail triage](#smarter-mail-triage-optional)) uses the same
engine. On Claude Code, each plain mail it triages is one `claude -p` call, so the
wizard also offers **no gateway model** (the keyword list decides, instantly and free).

### One agent per account (recommended)

`account:` is a list, so one agent *can* drive several mailboxes — and
`parley --add-account` appends one. Know what that gets you before you do it:

| | With two accounts in one agent |
|---|---|
| **Receiving** | Works on **both**. Every account is polled, and the agent replies through whichever one the message arrived on. |
| **Instructing** | Always goes out through the **first** account in the `accounts:` list. There is no way — no flag, no phrasing, no setting — to say *"send this one from my work address."* |

So if you keep a personal and a work mailbox, **give each its own config file**:

```bash
parley --setup                       # → ~/.parley/cfg_personal.yaml
parley --setup                       # → ~/.parley/cfg_work.yaml
parley -cfg work --default           # pick the everyday one
```

Each is a separate agent with its own port, policy and journal. Target one
explicitly with `-cfg work`, or from the tray prompt with `@work `. That's the
supported path, and it's why the tray has `@name` targeting at all.

Use several accounts in **one** agent when you want them to share one policy and
one negotiation memory — and when you're happy for everything you initiate to
leave from the first address. Put that address first in the list.

Per-provider credentials (Google Cloud project, Azure app registration, Yahoo app
password, scopes) are documented in the
[**Backend setup appendix**](#appendix--backend-setup-credentials-per-provider)
below. First run opens a browser once to grant access; tokens and other generated
state land under `~/.parley/state/<name>/` and are reused after.

### Where secrets live (and what to guard)

`~/.parley/` is the one place credentials live — deliberately **outside** the repo
(and any cloud-synced folder like OneDrive). Two different concerns:

- **Backup** — to *rebuild* an agent you only need its `cfg_<name>.yaml`; generated
  state (`state/<name>/`) is regenerable by re-consenting.
- **Secrecy** — guard the cfg **and** the state tokens: a leaked
  `state/<name>/token.json` (or MSAL cache) is a live credential on its own.

Re-authenticate an agent by deleting `~/.parley/state/<name>/` and rerunning.

### Policy — write it like you'd brief an assistant

The policy is the `unstructured` prose: the brain follows it on every decision. Say
*when to act alone and when to ask you*:

- *"Ask me before accepting new meetings."* → every new request pops a question.
- *"Auto-accept anything from my team: a@co, b@co."* → those book on their own.
- *"If a new request collides with an existing meeting, try to move the other meeting."*
- Say nothing → the brain uses good judgment and asks you when unsure; a collision
  with no matching rule is **always** asked, never silently rebooked.

**Your answers teach it.** When the agent asks and your reply contains a standing
rule ("from now on, keep Friday afternoons free"), it is persisted
(`~/.parley/state/<name>/policy_learned.md`) and applied from then on — you can also
just edit that file.

### Pricing collisions — standing, movability, offer bounds

When no common free slot exists, someone's meeting must yield. Three prose
conventions tell the agent how to price that (it never invents them; unpriced →
it asks you with concrete options):

| Convention | You write, in the policy prose | Effect |
|---|---|---|
| **Standing** — who a person is to you | *"maria@co is my boss. The x-team (a@co, b@co) are teammates. Anyone else is external."* | boss → your meeting moves; teammate → whoever's meeting matters less moves; external → asked |
| **Movability** — how pinned a meeting is | *"Standups and 1:1s are fluid; client calls and interviews are pinned."* | fluid meetings are offered up first; pinned ones never move silently |
| **Offer bounds** — what may be offered to whom | *"Never offer slots before 10:00 to external people. Fridays only to my team."* | every outgoing offer's slots respect the bound; each offer is journaled (`offer` records) so you can audit |

Declaring none of it is fine — collisions are then always asked, never guessed.

### The trust table — who may trigger what

Prose is judgment; the **trust table** is hard authority the agent enforces before
any reasoning runs (and it always beats the prose):

```yaml
    policy:
      trust:
        "boss@co.com": auto              # handled autonomously, always
        "newsletter@list.com": ignore    # dropped at the door (journaled)
        "new@person.com": ask            # every mail from them → a question to you
        "b@co.com": { default: auto, create_event: ask }  # per-verb
```

When a table rule says `ask` on an action the agent wanted to take, you get the
question (popup / email); answering **accept** approves that one action exactly once.

### Smarter mail triage (optional)

```yaml
llm-gateway:                        # top-level, right after llm:
  model: "haiku"                    # a SMALL model just for "is this about scheduling?"
  # provider: "anthropic"           # optional — could be another provider later
  # anthropic_key: "…"              # optional — defaults to llm:'s key
```
Without it, a keyword list decides which plain emails the agent looks at. With it, a
cheap classifier decides (any phrasing, any language); if the classifier ever fails,
the keyword list takes over automatically. (A per-account override exists:
`account.gateway: model`.)

### Restart safety (don't miss meetings while the agent is off)

Your agent persists a small **intake cursor** so a stop/restart resumes where it left
off instead of skipping mail that arrived during the downtime. Automatic; two knobs:

| Key | Backend | Effect |
|---|---|---|
| `catch_up` | gmail | How far back to read with no usable cursor: `"7d"` / `"24h"` / `"today"` / `"none"`. Default `"1d"`. |
| `history_file` / `state_file` | gmail / local-outlook | Where the cursor lives (defaults under `~/.parley/state/<name>/`). |

Outlook/Graph is already restart-safe (persisted `delta_link`). Open negotiations
also survive restarts (`state/<name>/negotiations.json`).

---

## 4. Run your agent

**A. Direct (foreground — watch it reason):**
```bash
python -m parley --config-file ~/.parley/cfg_gmail.yaml
```

**B. Via the CLI client (auto-starts a background daemon):**
```bash
parley -cfg gmail --status          # start (if needed) + show identity
parley --status                     # no -cfg: EVERY registered daemon, up/down + pending
parley -cfg gmail --stop            # stop one daemon
parley --stop                       # no -cfg: stop EVERY running daemon
```
First time, set a default so you can drop `-cfg`:
```bash
parley -cfg gmail --default
```
Want your agents up whenever the tray launches (login, reboot)? Pin them:
```bash
parley -cfg gmail --autostart       # pin gmail to the tray's autostart list
parley --autostart                  # list the pinned agents
parley -cfg gmail --autostart off   # unpin
```
See [§6](#6-the-desktop-app-tray-icon-toasts-win-alt-p) for how the tray uses that list.
(Keep using the same `-cfg` without a default and Parley drops a one-time hint to
set it.) Instructions don't need quotes — `parley set me a meeting with x@y.com
friday` works as-is; quote only shell-special characters (`& | < >`) or a leading `-`.
Each config gets its own port; the spawned server logs to
`~/.parley/logs/server-<port>.log`, and the agent's **journal** (every message +
decision) to `~/.parley/logs/<agent>/journal-YYYY-MM-DD.jsonl`.

### Command reference

```
parley --help                                                 # every mode on one screen
parley set a meeting with x@y.com tomorrow at 12:00 about T   # ask Parley to set a meeting with someone
parley -cfg outlook "..."                                     # target another agent for one call
parley --watch                                                # attach + answer every parked question as it comes
parley --serve -cfg gmail                                     # run the daemon in this terminal
parley --run -cfg gmail                                       # one agent in the foreground, no daemon
parley --setup                                                # guided setup wizard, everything verified live
parley --add-account -cfg gmail                               # append one more live-verified account
parley --engine -cfg gmail                                    # change only the engine (Claude Code / API key), verified live
parley --desktop                                              # tray: toasts + questions + Win-Alt-P prompt
python -m parley --selftest                                   # all-in-memory, no credentials, no network
```

---

## 5. Everyday use

### Book a meeting
```bash
parley set me a meeting with alice@host.com tomorrow at 12:00 about sync
parley set me a meeting with alice@host.com before friday about sync
```
A **stated time is honored** (checked against all your calendars; a clash follows
your policy or asks you). With no time, the brain picks a free slot. If the other
side runs Parley, their agent answers and both calendars get booked; if not, they
just receive a normal invite.

### Every command ends in ASK or END
If the agent needs you **right away** ("that slot is busy — book anyway?"), the
question appears in the same terminal and the prompt stays open until you answer
(**ASK**). Otherwise the reply prints and the command exits immediately (**END**) —
no Enter, no held terminal. Negotiations that then run for minutes or hours never
hold your terminal; when you ask "what's still open?", the agent ENDs with the list
— or ASKs "which one?" if it needs your pick.

### Ask what's in flight — and steer it
```bash
parley what meetings are still open
parley for the F22 meeting, offer 30 minutes instead
```
The **brain** answers from its own negotiation memory (numbered list + state); steer
any of them with a follow-up command by meaning.

### It will never silently double-book
Booking into a slot that already holds something is **refused by the engine itself**
— the agent must either ask you or invoke a rule you gave it ("task-style entries
don't block meetings"); every override is journaled. And when someone declines an
invite you sent, that's recorded too: the negotiation reopens and the agent
renegotiates or tells you — a decline can no longer vanish.

**Refer to meetings by meaning** — topic, person, or time: *"move the meeting about
F1"*, *"cancel my lunch with Bob"*. Every meeting you ask for exists in the agent's
memory from the moment you ask (even before anything is sent), and the agent can also
search your calendar itself — so this works across sessions and even for meetings
booked long ago. You never need an id.

### Questions that arrive later
When a counterpart replies an hour later and the agent needs you, the question
reaches you as a **Windows toast** — click it and the **question window** opens, one
card per pending question. A card is short: what kind of question it is, who, when,
the title and one line saying what is needed. *More detail* unfolds the full text.
Answer with the two buttons (they fit the question: Accept/Decline for an invite,
Nudge/Drop for a meeting with no reply, …) or type free-text guidance. You can also
attach a terminal: `parley --watch` shows every parked question as it comes (Ctrl+C
to detach). One question per thing; if you answer on two surfaces, the **first answer
wins**.

**Questions close themselves** when nothing is left to decide:

- the meeting ended after the question was asked (booked, declined, cancelled);
- the meeting's date has passed;
- it was an invite and you already answered it in your calendar (checked every 10
  minutes, and at start).

Handled something some other way? Press **Dismiss** on the card, or run
`parley --dismiss <ref> "why"`. It closes the question and wakes nothing: the agent
does not act on it and nothing is sent.

> **Both surfaces are at-desk.** Answering from your phone isn't supported yet —
> see [§9](#9-what-it-does-not-do-yet).

### Reactive negotiation
When someone's request collides with an existing meeting, the brain sees both
negotiations in its working memory and — per your policy — proposes moving one,
negotiates the shuffle, and confirms once room is made (or asks you first).

### Focus time before a meeting (`parley --focus`)
"I need 2 hours in one go, in the last 3 days before the board review, to prepare it."
Parley keeps that time for you as a **Dynamic Focus Block**:

| While… | Parley… |
|---|---|
| it fits with room to spare | puts `🧠 DFB: <purpose>` on your calendar as **free** — others may still book over it |
| someone books over it | moves it elsewhere in the window |
| the free time left gets tight | makes it **busy** and tells you (a card in the tray) |
| it no longer fits | asks you: keep trying, or drop it |
| the meeting moves / is cancelled | moves the blocks with it / removes them |

Your agent also weighs it: a new meeting that would take the **last** time your work
fits is not booked silently — your policy decides (it outranks your prep, propose
another time, or ask you).

```bash
echo '{"anchor": {"title": "Board review", "start": "2026-11-05T10:00"},
       "window": {"days": 3}, "blocks": 1, "hours": 2,
       "purpose": "Prep Board review"}' | parley --focus
parley --focus list
parley --focus cancel dfb-ab12cd34
```

Working day by default: Monday–Friday, 09:00–18:00 in your calendar's timezone
(`"day": ["08:00", "17:00"]`, `"weekends": true` to change it). `/myorganizer` proposes
focus time for you and creates it when you say so.

### From Claude Code: `/parley` and `/myorganizer`
If you have the Claude Code CLI, setup offers two skills (they install into
`~/.claude/skills/` and work from any folder):

| Skill | What it does |
|---|---|
| `/parley` | Arrange a meeting (`/parley set up 30 min with ana@example.com`), see and answer what the agent is waiting for, close what is already handled, add a standing rule. |
| `/myorganizer [1D…1Y]` | Organise your day, week or month across the sources you choose the first time (calendar and mail, notes, issue tracker, an objectives spreadsheet). Finds duplicates, overdue items and missing time. |

`/myorganizer` keeps everything about you in `~/.parley/myorganizer/` (your profile,
notes and memory), never in the skill. The objectives reader needs
`pip install parley[organizer]`. Both skills follow one rule: **Claude discovers,
Parley commits** — anything sent or booked goes through your agent.

---

## 6. The Desktop App (tray icon, toasts, Win-Alt-P)

**No terminal needed.** The installer starts the tray with Windows and puts it on your
Start Menu — press **Win**, type **Parley Desktop**, Enter. That's the everyday way in,
and once it's running you never touch a command line for normal use.

<details>
<summary>From a terminal instead (scripting, remote boxes, debugging)</summary>

```bash
parley --desktop        # (parley_desktop works too) — returns immediately
python -m parley.client_server.parley_desktop   # same, but with console output for debugging
```
</details>

A **sail icon** appears in the system tray and watches **all** your running agents:

- **Amber badge + Windows toast** when any agent has a pending question; click the
  toast (or menu → *Open questions*) for the **question window**: one compact card
  per question, grouped per agent (two answer buttons, **free-text guidance**,
  *Dismiss*, *Open in Claude*); a follow-up ASK renders in place.
- **Settings…** (tray menu) shows one card per agent: its **engine** (Claude Code or
  an API key), brain and gateway models, and its **connections**. From there:
  **New agent** runs the setup wizard, **Add connection** adds an account to that
  agent, and **Engine…** changes only its engine (see
  [The brain's engine](#the-brains-engine-claude-code-or-an-api-key)). Each button
  opens the same live-verified Setup Window as the command line. When a change
  touches a **running** agent, the tray restarts that agent so it uses the change.
- **Win-Alt-P** opens a floating prompt from anywhere: type an instruction, Enter
  sends it to your **default agent**. To talk to a *different* running agent, either
  **click the agent badge** (bottom-right) and pick one — the same menu can **set
  that agent as your default** — or press **Tab** in an empty prompt to cycle it, or
  prefix just this message with `@name` (e.g. `@outlook cancel my lunch with Bob`).
  The transcript grows in place; Esc dismisses. (The hotkey is configurable:
  `[desktop] hotkey` in your default cfg, or `--hotkey`.)
- **What starts and stays up.** The tray reads a persistent **autostart list**
  (`~/.parley/autostart.json`, managed with `parley --autostart` — see [§4](#4-run-your-agent))
  and starts those agents **once, when it launches**. While running it **keeps them
  alive** (revives a crash). A deliberate `parley --stop` takes an agent down and it
  **stays down for the session**; the next tray launch (login, reboot) brings the
  pinned ones back. In short: `autostart.json` = what *should* run; the live registry
  `~/.parley/servers.json` = what *is* running.
- **Control the tray from the terminal** (works in every shell, including cmd.exe):
  `parley --desktop --stop` quits the tray (agents keep running); `parley --desktop
  --no-autostart` / `--hotkey SPEC` launch it configured. `parley_desktop --stop`
  also works. A second tray launch refuses politely.

The installer offers a Start-Menu entry (press Win, type "parley") and autostart
at login. Answering a question here removes it everywhere (first answer wins) —
the other surface is `parley --watch` in a terminal.

---

## 7. Check the install (optional)

```bash
python -m parley --selftest
```

Runs the whole agent in memory — no credentials, no network, no tokens spent — 13
suites covering the brain loop, the trust table, timezone conversion, booking
idempotency and the journal. **Exit 0 means all green.** It's the fastest way to tell
"is my install sane?" apart from "are my credentials wrong?": if this passes but your
agent misbehaves, the problem is configuration, not code.

---

## 8. Troubleshooting

| Symptom | Fix |
|---|---|
| `no LLM client configured — cannot act` | Add the `llm:` mapping (key + `model`) to your `~/.parley/cfg_<name>.yaml`. |
| `no model configured` | Set `model: "…"` next to the key — Parley never assumes a default model. |
| `timezone '<zone>' does not resolve` at startup | `pip install tzdata` in this venv — bookings fail loudly until it resolves. |
| Browser didn't open / token errors | Delete `~/.parley/state/<name>/` and re-run to re-consent. |
| Gmail asks to sign in again every ~7 days | Your Google app is in **Testing** status (Google expires its token weekly). Publish it to **Production** (Audience → Publish app) — it stays unverified, and the weekly re-consent stops. |
| Outlook keeps reopening the "Authentication complete" sign-in page | The silent token refresh now asks only for the scopes a personal account can grant (a work-only `Calendars.ReadWrite.Shared` was making it miss the cache), and the token cache is written atomically. If it still recurs, the server log (`~/.parley/logs/server-<port>.log`) names why with `opening the browser for Microsoft sign-in (…)`; delete `~/.parley/state/outlook/` for one clean re-consent. |
| Agent doesn't react | Read the journal (`~/.parley/logs/<agent>/journal-*.jsonl`) — every inbound is recorded with why it was dropped or what the brain did. |
| Mail from someone is always dropped / always asked | That's the trust table — check `account.policy.trust:` for an `ignore`/`ask` entry (drops are journaled as `trust_ignore`). |
| Classifier misroutes mail | Journal records show `gateway … by: classifier` with the verdict; remove the `llm-gateway:` mapping to fall back to the keyword heuristic, or add the sender to the trust table. |
| Endless questions / duplicate asks | Shouldn't happen (one ask per thing; first answer wins) — if it does, the journal shows each `escalate`/`decide`; file it. |
| Two agents collide on a port | Each config gets its own port automatically; use separate config files. |
| Need to stop everything | `parley --stop` (no `-cfg`) stops every running daemon at once; add `parley --desktop --stop` to quit the tray too. Pinned agents restart at the next tray launch — unpin with `parley -cfg <name> --autostart off`. |
| Agents keep coming back after `--stop` | The tray revived them, or they're on the autostart list. Quit the tray (`parley --desktop --stop`) and/or unpin them (`parley -cfg <name> --autostart off`). |

---

## 9. What it does *not* do yet

- Away-from-desk approvals — **both** answer surfaces (the tray and `parley --watch`)
  are at-desk. There is no way to answer a question, or send an instruction, from your
  phone. An earlier email-to-yourself channel was removed in the 2026-08-31 refactor
  and has not been replaced.
- Choosing which account an instruction goes out through, when one agent holds
  several — see [§3](#3-configure-your-agent).
- Ranked multi-slot picks ("here are 3 times, choose one") — designed, not built.
- Deterministic working-hours enforcement — hours are policy prose the brain follows,
  not a hard limit.
- Reacting to you editing the calendar directly mid-negotiation (UC-5 change-watch).

Recently graduated *out* of this list: multi-party negotiation (one organizer, N
attendees, quorum settle — iteration 21, live 3/3) and priced make-room with
policy-bounded offers (iteration 21); Yahoo CalDAV is live-verified (on discovery
failure the account still degrades loudly to mail-only).

---

## Appendix — Backend setup (credentials per provider)

What each account backend needs and where to click. **The easy way is
`parley --setup`** — it opens these pages for you one at a time and verifies
every credential live. This appendix is the reference for doing it by hand
(and for understanding what the wizard just did). Secrets go **inline** in your
private `~/.parley/cfg_<name>.yaml`; generated state (tokens, caches) lands
under `~/.parley/state/<name>/` on first run.

**Every backend is guided.** The wizard opens each provider page one at a time,
tells you exactly what to click, and (in the Setup Window) shows a screenshot
and **BACK / NEXT** buttons so you can step back if you miss something — then it
verifies the result live before writing anything. Your clicks stay yours:
signing in (incl. 2FA), every policy/terms agreement, and the final OAuth
consent. (Earlier builds had an "assisted" mode that drove the Azure pages
through Playwright; Microsoft's portal fought the driven browser too often to
be reliable, so it was removed — guided is the single path now.)

### Gmail (`backend: gmail`)

Scopes requested (consented once in the browser): `gmail.readonly`,
`gmail.send`, `calendar.events`, `calendar.readonly` (the last lets setup list
your calendars so you can pick which to include, rather than typing ids).

1. In <https://console.cloud.google.com/>: create a project, then enable **both**
   the **Gmail API** and the **Google Calendar API**.
2. **OAuth consent screen:** External. Before you can **Publish the app to
   Production**, Google's **Branding** page requires an App name, a support
   email, and three URLs: **App homepage**, **Privacy policy link**, **Terms
   of Service link** — plus adding that URL's domain under **Authorized
   domains**. None of these need to be elaborate; a single free static page
   (e.g. GitHub Pages) covering all three is enough, and the same one can be
   reused across every OAuth app you own. Once Branding is filled in: Audience
   → **Publish app** → confirm, status becomes *In production*. Do **not**
   leave it on *Testing*: Testing-mode apps expire their refresh token every
   **7 days**, forcing a manual re-consent — a real problem for an agent meant
   to run unattended. Publishing stops that. The app stays **unverified** (no
   Google verification/CASA process needed for a private app); you'll click
   past a one-time "Google hasn't verified this app" warning. The 100-user cap
   that comes with an unverified app is irrelevant here — **you own your own app
   and its only user is you** (the cap is per-app, not across Parley).
3. **Credentials → OAuth client ID → Desktop app** — copy the **client ID** and
   **client secret** (the wizard can instead read the *Download JSON* file
   straight from your Downloads folder).
4. In the account's `account:` entry: set `address` and `calendars: [primary]`
   (add more calendar ids to union their free/busy); add a `google:`
   mapping with `client_id` + `client_secret` inline. The token is generated on
   first consent under `~/.parley/state/gmail/token.json`.
5. *(Optional, restart safety.)* `history_file` sets where the durable intake
   cursor lives (defaults to `~/.parley/state/gmail/history.json`); `catch_up`
   (`"7d"`/`"24h"`/`"today"`/`"none"`, default `"1d"`) is how far back to read
   when there's no usable cursor.

### Outlook / Microsoft 365 (`backend: outlook`)

Delegated scopes: `Mail.Read`, `Mail.Send`, `Calendars.ReadWrite`.

1. <https://portal.azure.com/> → **Microsoft Entra ID → App registrations → New
   registration**. Supported account types: any org + personal. Redirect URI:
   **Public client/native**, `http://localhost`.
2. Copy the **Application (client) ID** from the Overview page.
3. **API permissions → Microsoft Graph → Delegated →** add `Mail.Read`,
   `Mail.Send`, `Calendars.ReadWrite`. (No client secret — public client.)
4. In the account's `account:` entry: set `address`, `client_id` (inline), and
   `calendars: [primary]`. The MSAL cache + delta cursor are generated under
   `~/.parley/state/outlook/` (`msal_cache.json`, `delta.txt`).

Restart-safe out of the box (Graph persists its `delta_link`).

### Yahoo (`backend: yahoo`)

Mail over IMAP/SMTP + calendar over CalDAV — one **app password** covers all
three. No cloud console needed; this is the fastest backend to set up.

1. Yahoo **Account Security** (<https://login.yahoo.com/account/security>) →
   **Generate app password** (any label, e.g. "Parley"). Copy it.
2. In the account's `account:` entry: set `address`, the `app_password`
   (inline), and `timezone` (**required**, IANA form like `Europe/Madrid` —
   there is no calendar setting to infer it from).
3. Calendar: discovered automatically over CalDAV
   (`caldav.calendar.yahoo.com`, same app password) at connect. If your Yahoo
   has several calendars, pin one with `calendar: <name-or-href-substring>`.
   Discovery is best-effort: on failure the account **degrades loudly to
   mail-only** (invites still go out as mail; nothing is silently booked).

### Classic Outlook on this machine (`backend: local-outlook`)

No cloud API — talks to the **classic** desktop Outlook via COM (pywin32).
Windows only, and requires *classic* Outlook with a configured MAPI profile. The
"new Outlook" (olk.exe) has no COM model; on a new-Outlook-only machine
`connect()` fails fast — use the Graph backend instead.

Since iteration 24 it behaves like the other backends: real meeting requests are
recognized as invites (RSVP'd, never duplicated), moving or deleting a meeting
you organized notifies the attendees, and "meet john" resolves through Outlook's
own address book. Two knobs:

- `timezone:` (IANA, e.g. `Europe/Madrid`) — **set it**: with no calendar API to
  ask, the cfg is where the zone comes from (fallback: the system zone via
  tzlocal if installed; else a loud UTC warning).
- `state_file:` — where the last-seen mail time is persisted for restart safety
  (default `~/.parley/state/local-outlook/state.json`).

Limitations that remain by COM's nature: single calendar (the profile's default
Calendar folder — no `calendars` to set), and Outlook must be running/startable
on this machine.

Re-authenticate any backend by deleting `~/.parley/state/<name>/` and rerunning.
