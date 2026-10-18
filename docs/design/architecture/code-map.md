# Parley — Code Map

> Where everything lives. Companion to
> [`agent-architecture.md`](agent-architecture.md) (what the pieces *do*) and
> [`client-server.md`](client-server.md) (the human↔agent side).

Source lives in the `parley` package under `src/` (src layout; `pyproject.toml` is
the single dependency + entry-point source — there is no requirements.txt).

| Path | What |
|---|---|
| `parley/__main__.py` | THE `parley` entry (iteration 22): client by default; `--serve` / `--run` / `--selftest` / `--setup` / `--add-account` modes. |
| `parley/parley_start_server.py` | `parley-server` console entry (+ `scripts/util/parley-start-server.sh`/`.bat`). |
| `parley/core.py` | Dependency-free value types (`Slot`/`Message`/`Event`/`Resolution`) + the email wire protocol. |
| `parley/agent/agent.py` | `Agent` — composition root; run loop, deterministic dispatch guards, the human gate, client-server bindings. |
| `parley/agent/brain.py` | `Brain` — **the** decision-maker: one LLM tool-use turn per admitted event, over policy prose + working memory. |
| `parley/agent/llm_port.py` | `LLMClient` — the provider-agnostic seam (the only vendor-SDK import); Anthropic adapter + `FakeLLMClient`. |
| `parley/agent/gateway.py` | `Gateway` — relevance for plain mail: deterministic rules + optional small-model classifier (fail-open): `ADMIT` / `IGNORE`. |
| `parley/agent/guardrail.py` | `Guardrail` — the one permission model: trust table → `ALLOW` / `ASK` / `DENY` at intake and on every tool call (empty = allow-all). |
| `parley/agent/tools.py` | `ToolBelt` — thin, connection-explicit, guardrail-gated verbs; owns the one tz conversion + idempotent settlement. |
| `parley/agent/memory.py` | `NegotiationMemory` — working state across concurrent negotiations, persisted until closed (then kept as tombstones). |
| `parley/agent/journal.py` | `Journal` — flight recorder: every message + decision → daily JSONL under `~/.parley/logs/<agent>/`. |
| `parley/agent/wire.py` | Deterministic peer-to-peer parse/emit: the PARLEY block types + invite normalization (`via_invite`, `inv-` ids). |
| `parley/agent/policy.py` | `Policy` — decision **data** (`structured` + `unstructured`); learned guidance lives in `state/<name>/policy_learned.md`. |
| `parley/agent/focus.py` | `FocusBlocks` — Dynamic Focus Blocks: time kept before a meeting; free-time arithmetic, the sweep that moves / hardens / releases blocks, and `impact()` for the brain's make-room decision. State in `state/<account>/focus_blocks.json`. |
| `parley/agent/compose.py` | Backend registry + builders (`make_account`, `build_agent`, `resolve_accounts`) + state/journal paths. |
| `parley/accounts/account.py` | `Account` — the provider-agnostic mailbox + calendar + contacts I/O surface. |
| `parley/accounts/{gmail,outlook,yahoo,local_outlook,memory}.py` | Concrete backends (Google, MS Graph, Yahoo IMAP/SMTP + CalDAV, classic Outlook COM, in-process memory). |
| `parley/accounts/ics.py` | Shared iCalendar (RFC 5545) parse/build helpers used by the gmail + yahoo backends. |
| `parley/accounts/cursor.py` | Durable intake cursor — persist "what's new since X" so a restart doesn't skip downtime mail. |
| `parley/client_server/{server,parley,config,client}.py` | The human↔agent side: HTTP server, the CLI client, YAML config + `~/.parley` registries, shared `Client` base. |
| `parley/client_server/{parley_desktop,tray_backend}.py` | The tray app (iteration 22): icon + toasts + question window + Win-Alt-P prompt + Settings window (agents, engine, connections; launches the wizards as their own process and restarts a running agent after a change); platform bits behind `tray_backend`. |
| `parley/install_config/` | The `parley --setup` / `--add-account` wizard (iterations 23-26): live-verified first-run configuration. Orchestrator + agent-level steps (`install_config.py`, `steps.py` — the engine choice: Claude Code with no key, or an API key), engine-only text edits of an existing cfg (`cfg_edit.py`, used by `parley --engine`), per-backend guided flows (`backends/`), console/GUI frontends (`console.py`, `setup_window.py` — a BACK / NEXT stepper with screenshots). All backends are guided; the old Playwright assisted mode was removed in iteration 26. |
| `parley/install_config/backends/` | One wizard module per account backend (gmail, outlook, yahoo, local_outlook): menu label + guided `collect()` flow + YAML item template, behind a registry (iteration 25). |
| `parley/skills/{parley,myorganizer}/` | The Claude Code skills (package data): `SKILL.md` + `references/` + `scripts/`. The repo's `.claude/skills/` holds identical copies (a selftest keeps them in sync). |
| `parley/install_config/skill_install.py` | Copies the shipped skills into `~/.claude/skills/<name>/` with a `.parley-skill.json` manifest, so uninstall removes only what it put there. |
| `parley/testing/{selftest,fakes}.py` | In-memory selftest + fakes. |
| `parley/config_templates/cfg.{gmail,outlook,yahoo}.example.yaml` | Per-backend templates (package data — ship in wheels too) — seeded into `~/.parley/` on first run; copy one to `cfg_<name>.yaml` (or let `parley --setup` write it). |
| `scripts/` | Installers (`install-windows.*`, `uninstall-windows.*`) + zero-install shims (`parley.bat`/`.sh`, `parley-start-server.bat`/`.sh`). |
| `~/.parley/cfg_*.yaml` | One **private** cfg per agent (outside the repo): `llm:` key + inline account secrets + policy. |
| `~/.parley/state/<name>/` | Generated per-agent state (OAuth token, MSAL cache, delta cursor, `negotiations.json`, `policy_learned.md`). |
| `~/.parley/myorganizer/` | `/myorganizer`'s per-user data, written by its first-run onboarding: `profile.yaml` (sources, streams, objectives), `notes.md`, `memory/`, `proposals/`. Never in the repo. |
| `~/.parley/logs/` | Spawned-server logs (`server-<port>.log`) + per-agent journal (`<agent>/journal-YYYY-MM-DD.jsonl`). |
| `~/.parley/servers.json` | The *live* daemons registry (what **is** running) behind `parley --status`/`--stop` and the tray keep-alive. |
| `~/.parley/autostart.json` | The *durable* autostart list (what **should** run) the tray seeds once at launch; managed by `parley --autostart`. |
