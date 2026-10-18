---
name: parley
description: Talk to the user's Parley scheduling agent from inside a Claude Code session — arrange a meeting, answer the questions the agent is waiting on, or set standing scheduling rules. Use when the user asks to arrange/schedule/set up a meeting with someone ("/parley contacta con Ana para hablar del presupuesto antes del viernes", "set up 30 min with bob@acme.com"), asks what Parley is waiting for or wants to answer it ("/parley qué tienes pendiente", "dile que sí"), or states a scheduling preference to remember ("nunca me reserves antes de las 10"). Looks up availability over the Microsoft 365 connector when the person is a colleague; the Parley daemon is the only thing that sends, books or decides.
---

# /parley [@agent] …

Three jobs, from a session the user already has open. Everything reaches the agent
through the `parley` CLI — add `-cfg <name>` when they wrote `@name`.

| They say | You do |
|---|---|
| "arrange a meeting with X about Y before Z" | [Arrange a meeting](#arrange-a-meeting) |
| "what's Parley waiting for?", "tell it yes" | [Answer what is waiting](#answer-what-is-waiting) |
| "never book me before 10" | [Set a standing rule](#set-a-standing-rule) |

## Answer what is waiting

Parley escalates when it will not decide alone. `parley --pending` lists what it is
waiting on:

```bash
parley --pending
# -> [{"negotiation_id": "mtg-ab12cd34", "kind": "invite", "summary": "...",
#      "from": "Ana Ruiz", "title": "Budget review", "start": "...", "end": "...",
#      "question": "<the full text>"}]
```

**Show it short.** Group by `kind` (invite, email, stalled, ...). One bullet per item:
who · title · when, then the `summary`. Never paste the full `question` for the whole
list. Open it only for the one being discussed.

Do **not** just read the question out and ask "yes or no?". The questions that reach a
human are usually the ones where the context is the whole point, so go and get it
before they decide:

```bash
echo '{"name": "get_negotiation", "input": {"ref": "mtg-ab12cd34"}}' | parley --consult
```

`get_negotiation` has the history of that negotiation; `search_events` and `check` see
their calendar. Read-only, all of them — the daemon refuses anything that acts.

Then relay what they decided, in their words:

```bash
parley --answer mtg-ab12cd34 "sí, acepta el martes"
parley --answer mtg-ab12cd34 "no — pregúntale primero de qué va la reunión"
```

The text is free-form and the agent re-plans on it, so "ask them what it's about
first" is a perfectly good answer. Two rules about this one:

- **You relay a decision, you never make one.** If they have not actually said what
  they want, ask them — do not pick the reasonable-looking option to move things along.
- **One question at a time.** Answering resumes a real negotiation; batching through a
  list invites you to guess on the ones they skimmed.

### Close what is already handled

When the user says a question is already handled ("ya está", "lo contesté en
Outlook", "olvídalo"), **dismiss** it instead of answering it:

```bash
parley --dismiss mtg-ab12cd34 "accepted it in Outlook"
```

It closes the question without waking the agent and sends nothing. Use `--answer`
whenever the user wants the agent to *do* something; `--dismiss` only when nothing is
left to do. Before listing, also check the user's calendar for invite questions: if
they already answered the invite there, dismiss it rather than present it as open.

The agent also closes questions by itself: when the meeting ends (booked, declined,
cancelled), when its date passes, and when the user answers the invite in their
calendar.

## Set a standing rule

When they state a preference that outlives the question in front of them:

```bash
parley --policy                                   # what the agent runs on today
parley --policy "nunca me reserves antes de las 10:00"
```

A one-off decision is not a rule. "Say no to this one" goes in `--answer`; "I never
take meetings before 10" goes here. Do not turn one into the other on your own — ask
if it is genuinely unclear which they meant.

Trust entries ("always accept Joan", "ignore this sender") are **not** settable from
here. They are enforced before the agent reasons at all and live in the cfg next to
the account secrets, so they are a hand edit. `--policy` shows the current ones; say so
and leave it.

## Arrange a meeting

Path A of the Claude↔Parley design. The user wants a meeting arranged. You do the
**research and the drafting**; Parley does the **committing**.

## The one rule

> **You discover, Parley commits.**

You may read anything you have access to — calendars, free/busy, the directory — and you
write the prose. But every outward act (sending mail, creating an event, RSVPing) goes
through `parley --negotiate`. Never send mail yourself, never create a calendar event
yourself, not even when you have a tool that could. Parley owns the guardrail, the
journal, the timezone conversion and the idempotent settlement; an action that bypasses
it is invisible to the agent that has to handle the reply three days from now.

## Why Parley and not just a calendar invite

Inside the user's own tenant, Exchange is a shared broker and a colleague's free/busy is
readable — that is what makes step 3 possible. Outside it there is no broker at all, and
Parley exists precisely for that case: it negotiates agent-to-agent over ordinary email.
So the availability lookup does not *replace* Parley, it **collapses the negotiation**
from several rounds to one.

## Steps

### 1. Parse the request

Pull out: the person, the topic, the duration (default 30 min), and any deadline
("antes del viernes" → a window ending Friday). A leading `@name` picks the agent
(`-cfg name`); without one, the user's default agent is used.

### 2. Resolve the person

If the user gave an email address, use it. Otherwise resolve the name:

- `mcp__claude_ai_Microsoft_365__search_people` for a colleague.
- Failing that, ask the user rather than guessing. Sending a meeting request to the
  wrong person is not recoverable by apologising.

Then decide **internal or external**: compare the resolved address's domain with the
user's own (`mcp__claude_ai_Microsoft_365__get_me`).

### 3. If internal, look for a real hole

Use `mcp__claude_ai_Microsoft_365__find_meeting_availability` (or
`outlook_find_available_time`) for the user **and** the peer, inside the deadline
window, honouring working hours.

Read the result honestly: "free" in Exchange is not always "available" (focus blocks,
someone else's tentative). That is fine — the peer can decline — but do not present a
guess as a certainty.

### 4. Choose the shape

| What you found | What to send |
|---|---|
| Exactly one good slot, both sides verified free | An **invitation** (`verified: true` + `slot`) |
| Several candidates, or you are not certain | A **message** offering 2–3 of them |
| External peer (no visibility) | A **message**, no slots — let the agents negotiate |

The invitation *is* the hold: the event exists from the moment it is proposed, so
nothing else can take the slot while you wait. Do not try to "reserve" a slot any other
way — placeholder events that outlive their negotiation are exactly the orphan-event
class of bug this project has been bitten by before.

### 5. Draft the message

Write it yourself, as a person would: who you are writing for, what the meeting is
about, the concrete time(s) with the date and the timezone spelled out, and what you
want back. No templates. Parley appends its own tracking ref.

### 6. Show it to the user, then send

Print the recipient, the shape (message or invitation), the chosen slot(s) and the full
draft, and **wait for a yes**. This sends real mail to a real person; the user asked for
a meeting, not for a specific sentence, so let them see the sentence.

On approval, pipe the JSON to the CLI (add `-cfg <name>` when `@name` was given):

```bash
echo '{
  "to": "ana@example.com",
  "topic": "Q4 budget",
  "subject": "Q4 budget - 30 min?",
  "body": "<the prose you drafted>",
  "context": "<why the user wants it, in their words>",
  "deadline": "2026-09-30"
}' | parley --negotiate
```

For the verified-slot case, drop `body` and pass the slot instead:

```bash
echo '{
  "to": "ana@example.com",
  "topic": "Q4 budget",
  "title": "Q4 budget",
  "verified": true,
  "slot": {"start": "2026-09-22T10:00", "end": "2026-09-22T10:30"},
  "context": "<why the user wants it>"
}' | parley --negotiate
```

Times without an offset are read in the calendar's own timezone — Parley converts, you
never should. Copy an ISO time with an offset verbatim if you have one.

**Several people:** pass them all in `to`, comma-separated (or as a JSON list). That is
ONE email addressed to all of them and ONE negotiation, the way a person convenes a
meeting — never one message each. Everyone then sees who else was asked, and a reply to
all stays in the same conversation. Parley answers the way it was written to: a
reply-to-all goes back to everyone, a message sent to the agent alone is answered
privately.

### 7. Report back

Give the user the negotiation ref and say what happens next: Parley is now watching for
the reply and will handle it (or come back with a question) without this session being
open. Say that explicitly — it is the part people do not expect.

If this meeting **replaces** a pending question (the user answered "set it up with them
again" to a stalled or declined one, and you opened a new negotiation instead), dismiss
the old question so it does not stay open in the tray:

```bash
parley --dismiss mtg-old12345 "replaced by mtg-new67890"
```

## `context` is not decoration

Whatever mattered about the request has to travel in `context`, because the reply
arrives days later and is handled by a **background session that never saw the user type
anything**. "Before the board meeting", "she is only in the office Tuesdays", "keep it
short" — if it is not in `context`, it is gone. The session reasons; Parley remembers.

## When things are not clean

- **No daemon running**: `parley --negotiate` spawns one. If it cannot, say so and stop
  — do not fall back to sending mail yourself.
- **No M365 connector** (or the peer is external): skip step 3 entirely and send a
  message. That is the normal Parley path, not a degraded one.
- **Ambiguous person**: ask. Never pick the first match.
- **The user asks to cancel or move something**: that is not this skill. Use a plain
  instruction (`parley "move my Tuesday sync with Ana to Thursday"`), which runs through
  the agent's own brain with its full memory of the negotiation.
