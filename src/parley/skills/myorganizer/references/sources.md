# Sources — how to read each kind

The profile names the sources; this file says how to read each **kind**. Identifiers
(page ids, JQL, paths) are in `~/.parley/myorganizer/profile.yaml`, never here.

If a call fails, re-discover and tell the user what changed — do not guess.

---

## `outlook` — Microsoft 365 connector (calendar + mail)

### Calendar — `outlook_calendar_search`

```
query: "*"
afterDateTime / beforeDateTime: the horizon window
order: "oldest"
limit: 25        (max; page with nextOffset)
```

⚠️ **Check the timezone field before converting.** Events come as
`start: {dateTime, timeZone}`. When `timeZone` is a Windows zone name (e.g. *Romance
Standard Time*), `dateTime` is **already wall-clock time** in that zone. Convert only when
it literally says `UTC`. Adding an offset "to convert" shifts the whole agenda.

| Field | Use |
|---|---|
| `isOrganizer: true` | The user convenes → they prepare the agenda |
| `showAs: "free"` | Own block — and that is why it gets overwritten |
| `isCancelled: true` | Still listed. Do not count it as busy |
| `showAs: "tentative"` | Not confirmed. A risk if a client is in it |
| `attendees` > 10 | Broad meeting, no individual prep |

The connector may refuse `outlook_create_event`. Calendar writes go through Parley anyway
(hard rule 9).

### Mail — `outlook_email_search`

Returns **metadata**, not the body. To read a mail: `read_resource` on its `uri`.

```
afterDateTime / beforeDateTime: the horizon window
order: "newest"     (⚠️ incompatible with a free-text query)
limit: 25
```

Filters: `sender` (partial), `recipient` (TO/CC/BCC), `folderName`.
`recipient` is not compatible with `folderName` nor `order`.

Read-only from this skill. `outlook_send_mail`, `outlook_create_draft` etc. exist — do not
use them here.

---

## `jira` — Atlassian connector

Ask only for the fields you need, or the response overflows:

```
fields: ["summary","status","priority","duedate"]
maxResults: 25
```

JQL comes from `profile` (`sources[].jql`). A sane default:

```sql
project = <KEY> AND (assignee = currentUser() OR reporter = currentUser())
ORDER BY updated DESC
```

Date field for overdue: `duedate`. Map the board's statuses to *to-do / doing / waiting /
done* in `sources[].statuses` — board names never match other sources.

---

## `notion` — personal notebook or Radar (read-write)

Two connectors may exist with different tool names (a local `notionApi_*` API server and
the hosted Notion connector). The profile says which one each source uses
(`sources[].connector`). Use that one; the other may not see the page.

Read: retrieve the page as markdown.
⚠️ Large pages come back as one long line and spill to a file. Read the file in spans of
~26 000 characters after `s.replace('\\n', '\n')`.

Write: **find-and-replace only** (`update_content` with exact `old_str`).
**Never `replace_content`** on a notebook — it would wipe the page.

### Notion markup to know

| Markup | Meaning |
|---|---|
| `<details><summary>text</summary>` | A toggle item. The real content is in the `summary` |
| `~~text~~` | Done or dropped. Without `🤖` = the user did it — never touch |
| `<mention-date start="YYYY-MM-DD"/>` | The item's date. The only "overdue" signal in Notion. The API drops the `reminder` attribute |
| `**text**` | Bold — a common importance marker (strip `<span …>` around it) |
| `<synced_block url="…">` | ⛔ **Never write inside.** It propagates to other pages |

```python
for m in re.finditer(r'\*\*(.+?)\*\*', s):
    t = re.sub(r'<[^>]+>', '', m.group(1)).strip()
```

### ⛔ Editing a table cell

**Opening and closing `<td>`, always both.** If `old_str` starts with `<td>` and `new_str`
does not, Notion does not fail — it **re-normalises the table and drops that whole column
in every row.**

`old_str` and `new_str` start and end on the same boundary: either edit text **inside**
a cell without touching tags, or replace the whole cell `<td>…</td>` → `<td>…</td>`.
After any table edit, **count the columns in the response** before trusting it.

If a Radar section grows, move it to a sub-page and keep its `<page url="…">` in the
content (otherwise a replace tries to delete it and fails).

---

## `notion` — team space (read-only)

Query databases with the SQL mode of the data-source query tool, by `collection://` id
(from the profile). Typical schema: `Task` · `State` · `Assignees` · `DueDate` · `Priority`.

Team trackers often leave `DueDate` empty. If so, **no overdue from here** — do not invent.

---

## `excel` — objectives spreadsheet

The reader ships with this skill, in its own `scripts/` folder:

```bash
python <this skill's directory>/scripts/objectives.py [--year YYYY] [--file PATH.xlsx]
```

It reads the `kind: excel` source of the profile: `path` (a file or a folder, `{year}`
allowed), `sheet` (signed objectives), `review_sheets` (regex), `columns`
(`{text, weight, extra}`), and optionally `start_marker` / `start_weight` /
`end_marker` to find the table inside the sheet. Output:
`{year, file, categories:[{name, weight, achieved, objectives}], reviews:{"<sheet>": [...]}}`

- Always read **from disk**, never from a stale copy: the file is the truth.
- Needs `openpyxl` and `PyYAML` (`pip install parley[organizer]`). Use the Python
  environment the user's `CLAUDE.md` names, if any.
- Run with `PYTHONIOENCODING=utf-8` on Windows consoles (cp1252 breaks on accents and `≥`).
- OneDrive may lock the file for Python (`PermissionError`) while `cp` can read it: the
  script retries on a temp copy; if that fails too, copy it yourself and pass `--file`.
