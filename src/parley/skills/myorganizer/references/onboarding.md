# Onboarding — first run

Runs when `~/.parley/myorganizer/profile.yaml` does not exist. Goal: a working profile in
**one short conversation**, then stop. Do not organise anything in the same run.

## Rules

- Ask with the structured question tool when available; group questions (≤ 4 per round).
- **Discover before asking.** List the connected MCP servers; offer only the source kinds
  that have a connector. Use `get_me` (or equivalent) for name, email and timezone.
- Every source asks two things: **where** (link / id / path) and **access** (read or
  read-write). Default to **read**. Read-write only when the user says so.
- Never store secrets in the profile. Connectors hold their own credentials.
- Write files only for the user (`0o600` on Unix). Never inside a repository.

## Rounds

| Round | Ask | Fills |
|---|---|---|
| 1 | Which sources do you use? (multi-select from the connected kinds: calendar+mail, notebook, issue tracker, team tracker, objectives spreadsheet, other) | `sources[]` skeleton |
| 2 | For each: where is it, and may I write to it? | `sources[].{page,jql,path,access}` |
| 3 | Where should the Radar live? (a writable notebook page, or local `radar.md`) | `sources[role=radar]` |
| 4 | *Optional.* Who pulls on your day (teams/areas)? Your objectives and weights? Your own agenda's protected block? | `streams`, `objectives`, `own_stream` |
| 5 | *Optional.* Key people and the meetings to prepare for them | `key_people` |

Rounds 4–5 can be skipped ("later"). The skill works without them; it just cannot tag by
objective or trigger meeting prep.

After round 3, **validate**: read one item from each source with its connector. Report
which ones answered. A source that fails stays in the profile with `status: unverified`.

Then create `notes.md` (empty headers) and `memory/MEMORY.md`, show the profile in a
table, and tell the user how to run `/myorganizer 1D`.

## Profile schema (v1)

```yaml
version: 1
user:
  name: Alice Example
  email: alice@example.com
  timezone: Europe/Madrid
  language: en
format:
  style: tables            # tables | prose
  max_reminders: 3
  max_questions: 2
focus_hours: ["08:00-10:00", "14:00-18:00"]

sources:
  - id: m365
    kind: outlook
    connector: claude_ai_Microsoft_365
    roles: [calendar, mail]
    access: read
  - id: notebook
    kind: notion
    connector: notionApi_Personal
    page: <page-id>
    roles: [notebook]
    access: read-write
    importance: bold         # bold | color:<name> | none
    strike_marker: "🤖"
    protected: [synced_block] # never written
    sections:                # optional: names that carry meaning
      current: "Processing"
      follow_up: "Waiting"
  - id: radar
    kind: notion
    connector: notionApi_Personal
    page: <page-id>
    roles: [radar]
    access: read-write
  - id: board
    kind: jira
    connector: atlassian
    site: example.atlassian.net
    jql: "project = KEY AND assignee = currentUser() ORDER BY updated DESC"
    statuses: {todo: [Backlog], doing: [In Progress], waiting: [Blocked], done: [Done]}
    roles: [tracker]
    access: read
  - id: team
    kind: notion
    connector: claude_ai_Notion
    collections: ["collection://<id>"]
    roles: [team-tracker]
    access: read
  - id: objectives
    kind: excel
    path: "C:/Users/alice/OneDrive/Objectives/{year}/"
    sheet: RS
    review_sheets: "^\\d{4}-\\d{2}$"
    columns: {text: B, weight: V, extra: W}
    start_marker: "OBJECTIVES {year}"   # optional: where the table starts
    start_weight: WEIGHT                # optional: weight-column text on that row
    end_marker: "TOTAL"                 # optional: where it ends
    roles: [objectives]
    access: read

streams:
  - {name: Sales, objective: Commercial, where: "notebook › Sales"}
  - {name: Operations, objective: Delivery, where: "board"}
own_stream:
  name: Strategy
  protected_block: "Think time"
  feeds: Product

objectives:
  - {name: Commercial, weight: 0.25}
  - {name: Delivery, weight: 0.25}

key_people:
  - email: boss@example.com
    role: manager
    meetings:
      - match: "1:1"
        prepare: "Go through the agenda script; propose what is missing"
        script: "notebook › Manager 1:1 › Current"

watch:
  clients: [Acme, Globex]
```
