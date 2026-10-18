"""LLM port — a thin, provider-agnostic seam for the brain.

Iteration 08 (decision #8): the brain must not depend on any one vendor's SDK. It
talks to this small interface instead:

    client.complete(system, transcript, tools) -> LLMResponse

so swapping Anthropic for OpenAI / Gemini / a local model is one new adapter, not a
rewrite. This is the *only* place a provider SDK is imported.

The **transcript** is a neutral list of turns (plain dicts) the brain owns and grows:

    {"role": "user",      "content": "<text>"}
    {"role": "assistant", "text": "<text>", "tool_calls": [ToolCall, ...]}
    {"role": "tool",      "tool_call_id": "<id>", "content": "<result text>"}

Each adapter translates that neutral transcript into its provider's message shape,
calls the API, and returns a neutral `LLMResponse` (assistant prose + any tool calls).
Adapters are stateless — the brain resends the full transcript each turn (which is how
tool-use loops work anyway).
"""

import json
from dataclasses import dataclass, field

# No in-code default model: which model the brain runs is a config decision, read from
# the cfg's `[llm] model` (see client_server.config). Constructing a real client without
# one is a loud error — never a silent fallback to a hardcoded name.
DEFAULT_MAX_TOKENS = 1024

# Family aliases: `model: "opus"` means "the newest Opus", looked up at every start.
# The user still picks the family — Parley never picks one for them — but the cfg
# stops pinning a release that ages silently. The Claude Code CLI resolves these
# itself; the API wants a concrete id, so AnthropicClient looks it up.
MODEL_ALIASES = ("opus", "sonnet", "haiku")


def is_model_alias(model):
    return (model or "").strip().lower() in MODEL_ALIASES


def resolve_model_alias(model, model_ids):
    """`model` unchanged when it is a concrete id. For an alias, the first id in
    `model_ids` (newest-first, the order /v1/models answers in) whose dash-separated
    parts name that family. No match is a loud error — falling back to some other
    model would be exactly the silent default the cfg rule forbids."""
    model = (model or "").strip()
    if not is_model_alias(model):
        return model
    family = model.lower()
    for model_id in model_ids:
        if family in str(model_id).lower().split("-"):
            return model_id
    raise ValueError(f"model alias {model!r}: this account lists no {family} model")


# --- neutral value types -----------------------------------------------------

@dataclass
class Tool:
    """A tool the model may call: a name, a human description, and a JSON-schema
    for its input (provider-neutral; adapters map it to their own field names)."""
    name: str
    description: str
    input_schema: dict = field(default_factory=lambda: {"type": "object", "properties": {}})


@dataclass
class ToolCall:
    """One tool invocation the model asked for. `id` correlates the result back."""
    id: str
    name: str
    input: dict = field(default_factory=dict)


@dataclass
class LLMResponse:
    """One model turn, provider-neutral. `tool_calls` empty → the model is done and
    `text` is its final answer. `usage` carries token counts incl. prompt-cache
    hits/writes (empty for adapters that don't report them)."""
    text: str = ""
    tool_calls: list = field(default_factory=list)
    stop_reason: str = ""
    usage: dict = field(default_factory=dict)

    @property
    def done(self):
        return not self.tool_calls


# cache_control marker for Anthropic prompt caching (5-minute ephemeral TTL).
_EPHEMERAL = {"type": "ephemeral"}


# --- the port ----------------------------------------------------------------

class LLMClient:
    """Provider-agnostic interface. One method; adapters implement it."""

    def complete(self, system, transcript, tools):
        """`system` prompt (str), neutral `transcript` (list of turn dicts), and the
        available `tools` (list[Tool]). Returns an `LLMResponse`."""
        raise NotImplementedError


# --- Anthropic adapter (the only SDK import in the codebase) -----------------

class AnthropicClient(LLMClient):
    def __init__(self, api_key=None, model=None, max_tokens=DEFAULT_MAX_TOKENS):
        model = (model or "").strip()
        if not model:
            raise ValueError("no model configured — set [llm] model in your cfg")
        import anthropic  # local import: --selftest with a fake client needs no SDK
        # The key must arrive from the cfg via the caller — no ambient ANTHROPIC_API_KEY
        # fallback (iteration 07-0: the cfg is the one source of the key).
        self._client = anthropic.Anthropic(api_key=api_key)
        self.alias = model.lower() if is_model_alias(model) else ""
        self.model = self._resolve(model) if self.alias else model
        self.max_tokens = max_tokens

    def _resolve(self, alias):
        """The newest concrete id of an alias's family, from this key's own listing
        (the SDK pages through it lazily, so the newest match costs one request)."""
        try:
            return resolve_model_alias(alias, (m.id for m in self._client.models.list()))
        except ValueError:
            raise
        except Exception as err:  # noqa: BLE001 — offline, bad key: say which alias failed
            raise RuntimeError(f"model alias {alias!r}: could not list the account's "
                               f"models ({err})") from err

    def complete(self, system, transcript, tools):
        req = self._cache_marked_request(system, transcript, tools)
        resp = self._client.messages.create(
            model=self.model, max_tokens=self.max_tokens, **req)
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        calls = [ToolCall(b.id, b.name, dict(b.input or {}))
                 for b in resp.content if getattr(b, "type", "") == "tool_use"]
        return LLMResponse(text=text, tool_calls=calls, stop_reason=resp.stop_reason or "",
                           usage=self._usage(resp))

    @classmethod
    def _cache_marked_request(cls, system, transcript, tools):
        """Build the `{system, tools, messages}` request kwargs with prompt-cache
        breakpoints (cache_control: ephemeral). Render order is tools → system →
        messages, so breakpoints sit at the stability boundaries:
          * the last TOOL schema — the tool set is static, so it stays cached across
            turns even when the per-turn system prompt (time, negotiations) changes;
          * the SYSTEM block — caches tools+system together across a multi-step turn
            (the system prompt is frozen per turn, iteration 18);
          * the last MESSAGE block — caches the growing transcript prefix across the
            tool-use loop's steps.
        Three breakpoints (max is 4). A sub-minimum prefix (the tiny gateway-classifier
        call, no tools) silently doesn't cache — harmless, no error. Pure/SDK-free so
        the selftest can assert the placement."""
        tool_schemas = [cls._tool_schema(t) for t in tools]
        if tool_schemas:
            tool_schemas[-1]["cache_control"] = _EPHEMERAL
        messages = cls._to_messages(transcript)
        if messages and messages[-1]["content"]:
            messages[-1]["content"][-1]["cache_control"] = _EPHEMERAL
        return {"system": cls._cached_system(system), "tools": tool_schemas,
                "messages": messages}

    @staticmethod
    def _cached_system(system):
        """The system prompt as a cache-marked text block — a breakpoint that caches the
        tools+system prefix. Empty system → passthrough (nothing to cache)."""
        if not system:
            return ""
        return [{"type": "text", "text": system, "cache_control": _EPHEMERAL}]

    @staticmethod
    def _usage(resp):
        """Token usage incl. cache hits/writes, so the journal can confirm caching works
        (cache_read > 0 from the second step of a turn onward)."""
        u = getattr(resp, "usage", None)
        if u is None:
            return {}
        return {"input": getattr(u, "input_tokens", 0),
                "output": getattr(u, "output_tokens", 0),
                "cache_read": getattr(u, "cache_read_input_tokens", 0),
                "cache_write": getattr(u, "cache_creation_input_tokens", 0)}

    @staticmethod
    def _tool_schema(tool):
        return {"name": tool.name, "description": tool.description,
                "input_schema": tool.input_schema}

    @classmethod
    def _to_messages(cls, transcript):
        """Neutral transcript → Anthropic messages, coalescing consecutive same-role
        turns (Anthropic wants tool_results grouped in the user turn after tool_use)."""
        msgs = []
        for item in transcript:
            role = item["role"]
            if role == "user":
                cls._append(msgs, "user", [{"type": "text", "text": item.get("content", "")}])
            elif role == "assistant":
                blocks = []
                if item.get("text"):
                    blocks.append({"type": "text", "text": item["text"]})
                for tc in item.get("tool_calls") or []:
                    blocks.append({"type": "tool_use", "id": tc.id, "name": tc.name,
                                   "input": tc.input or {}})
                cls._append(msgs, "assistant", blocks)
            elif role == "tool":
                cls._append(msgs, "user", [{"type": "tool_result",
                                            "tool_use_id": item["tool_call_id"],
                                            "content": item.get("content", "")}])
        return msgs

    @staticmethod
    def _append(msgs, role, blocks):
        if not blocks:
            return
        if msgs and msgs[-1]["role"] == role:
            msgs[-1]["content"].extend(blocks)
        else:
            msgs.append({"role": role, "content": blocks})


# --- Ollama adapter (local brain, OpenAI-compatible wire over requests) -------

# Ollama's default OpenAI-compatible base URL. The `/v1` is the compat prefix; the
# adapter POSTs to `<base_url>/chat/completions`.
_OLLAMA_DEFAULT_BASE_URL = "http://localhost:11434/v1"


class OllamaClient(LLMClient):
    """A local brain served by Ollama. Named for the target, not the wire: internally it
    speaks Ollama's **OpenAI-compatible** `chat/completions` format, which is why it needs
    no vendor SDK — just `requests` (already a Parley dependency). A future hosted
    OpenAI-compatible adapter (OpenAI / Groq / …) could lift this transcript/tool mapping
    unchanged; this iteration wires it for Ollama only.

    Local by default: no API key, `base_url` points at the localhost Ollama server. An
    `api_key` is sent (Bearer) only when configured, for a remote/authenticated Ollama.
    The model is still required — no default model, ever.

    Tool calls use the compat endpoint (not native `/api/chat`) so each carries an `id`
    that correlates the tool result back through the neutral transcript. There is no
    Anthropic-style `cache_control`: the compat wire has no prompt-cache markers."""

    def __init__(self, model=None, base_url=None, api_key="", max_tokens=DEFAULT_MAX_TOKENS):
        model = (model or "").strip()
        if not model:
            raise ValueError("no model configured — set [llm] model in your cfg")
        self.model = model
        self.base_url = (base_url or _OLLAMA_DEFAULT_BASE_URL).strip().rstrip("/")
        self.api_key = (api_key or "").strip()
        self.max_tokens = max_tokens

    def complete(self, system, transcript, tools):
        import requests  # a core dep; local import keeps the pure mappers testable SDK-free
        payload = {"model": self.model, "max_tokens": self.max_tokens, "stream": False,
                   "messages": self._to_messages(system, transcript)}
        tool_schemas = [self._tool_schema(t) for t in tools]
        if tool_schemas:
            payload["tools"] = tool_schemas
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        # Loud on failure (a dead local server must not degrade to silence): iteration 08's
        # "LLM errors are loud" stance — the caller (Agent._build_client / Brain) logs it.
        resp = requests.post(f"{self.base_url}/chat/completions", json=payload,
                             headers=headers, timeout=120)
        if resp.status_code != 200:
            raise RuntimeError(
                f"Ollama {resp.status_code} at {self.base_url}: {resp.text[:300]}")
        return self._parse(resp.json())

    @classmethod
    def _parse(cls, data):
        """OpenAI-compat response → neutral `LLMResponse`. Tool-call `arguments` arrive as
        a JSON string; parse to a dict (malformed → empty, never a crash)."""
        choice = (data.get("choices") or [{}])[0] or {}
        msg = choice.get("message") or {}
        text = msg.get("content") or ""
        calls = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            args = fn.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args or "{}")
                except (ValueError, TypeError):
                    args = {}
            calls.append(ToolCall(tc.get("id") or "", fn.get("name") or "",
                                  dict(args or {})))
        return LLMResponse(text=text, tool_calls=calls,
                           stop_reason=choice.get("finish_reason") or "",
                           usage=cls._usage(data.get("usage") or {}))

    @staticmethod
    def _usage(u):
        if not u:
            return {}
        return {"input": u.get("prompt_tokens", 0),
                "output": u.get("completion_tokens", 0)}

    @staticmethod
    def _tool_schema(tool):
        """Neutral Tool → OpenAI-compat function tool (`input_schema` → `parameters`)."""
        return {"type": "function",
                "function": {"name": tool.name, "description": tool.description,
                             "parameters": tool.input_schema}}

    @classmethod
    def _to_messages(cls, system, transcript):
        """Neutral transcript → OpenAI-compat messages. Unlike Anthropic, tool results are
        their own `role: tool` messages (no coalescing), assistant tool calls ride
        `tool_calls` with the args re-serialized to a JSON string, and the system prompt
        is a leading `role: system` message."""
        msgs = []
        if system:
            msgs.append({"role": "system", "content": system})
        for item in transcript:
            role = item["role"]
            if role == "user":
                msgs.append({"role": "user", "content": item.get("content", "")})
            elif role == "assistant":
                m = {"role": "assistant", "content": item.get("text") or None}
                tcs = [{"id": tc.id, "type": "function",
                        "function": {"name": tc.name,
                                     "arguments": json.dumps(tc.input or {})}}
                       for tc in item.get("tool_calls") or []]
                if tcs:
                    m["tool_calls"] = tcs
                msgs.append(m)
            elif role == "tool":
                msgs.append({"role": "tool", "tool_call_id": item["tool_call_id"],
                             "content": item.get("content", "")})
        return msgs


# --- Claude Code adapter (the brain runs inside a headless `claude -p`) ------

_CLAUDE_CODE_CONTRACT = """

--- HOW TO ANSWER ---

You are being driven as a decision engine, not as a coding assistant. Do NOT use any
of your own tools, do not read or write files, and write nothing outside the object
below.

Answer with ONE JSON object and nothing else — no prose around it, no code fence:

  {"text": "<what you concluded, one or two sentences>",
   "tool_calls": [{"name": "<tool name>", "input": {...}}]}

`tool_calls` lists the tools you want run, in order, with arguments matching their
schemas exactly. An empty list means you are done and `text` is your final answer to
the user. Never name a tool that is not listed below.

--- TOOLS YOU MAY CALL (JSON Schema) ---

"""


class ClaudeCodeClient(LLMClient):
    """Run the brain through a headless Claude Code session (`claude -p`).

    Why it exists (2026-09-19): the Claude Code CLI authenticates with the user's own
    claude.ai session, so an agent can have a frontier brain on a machine that has no
    Anthropic API key — and reach whatever that session already has (corporate
    connectors, skills) without Parley holding any corporate credential.

    Shape. Claude Code is itself an agent loop with its own tools, but the brain
    already owns the loop, and with it the guardrail, the journal and idempotent
    settlement. Handing that to the CLI would move Parley's safety properties into a
    subprocess we don't control. So this adapter uses `claude -p` as a pure decision
    *function*: its own tools are suppressed, it never touches a calendar, and it
    answers with a JSON object naming the Parley tools to run. Parley runs them.

    Cost, measured on this machine (opus, 2026-09-19): a call pays ~25k tokens of
    Claude Code's own harness prompt, which `--system-prompt` does not remove because
    the built-in tool schemas stay loaded. Cold that is ~$0.26; a fresh call with an
    identical system prompt partly re-reads it from the prompt cache (~$0.11); a
    `--resume`d step reads all of it from cache (~$0.04). So the adapter remembers the
    session it just used and resumes when the incoming transcript EXTENDS the one it
    answered under the SAME system prompt — i.e. within one brain turn, whose system
    prompt is frozen (iteration 18). A changed system prompt always starts fresh, so a
    stale "now" can never be resumed into. The `complete()` contract stays stateless;
    the session is an optimization behind it.
    """

    #: Claude Code's own tools, refused explicitly. The contract above already tells
    #: the model not to use them; this makes it true rather than merely requested.
    OWN_TOOLS = ("Bash", "Read", "Edit", "Write", "NotebookEdit", "Glob", "Grep",
                 "WebFetch", "WebSearch", "Task", "Agent", "TodoWrite", "Artifact")

    def __init__(self, model=None, cli=None, timeout=300, cwd=None):
        model = (model or "").strip()
        if not model:
            raise ValueError("no model configured — set [llm] model in your cfg")
        self.model = model
        self.cli = cli or "claude"
        self.timeout = timeout
        self.cwd = cwd
        self._last = None  # {"system", "turns", "session"} — the resume anchor

    # -- the port ------------------------------------------------------------

    def complete(self, system, transcript, tools):
        import os
        import shutil
        import subprocess
        import tempfile

        system_text = self._system_prompt(system, tools)
        resume, new_turns = self._plan(system_text, transcript)
        prompt = self._render(new_turns)

        exe = shutil.which(self.cli)
        if not exe:
            raise RuntimeError(
                f"the Claude Code CLI ({self.cli!r}) is not on PATH — install it, or "
                f"use provider: anthropic with an API key instead.")
        # Windows ships `claude` as a .cmd shim, which CreateProcess cannot exec.
        argv = (["cmd", "/c", exe] if exe.lower().endswith((".cmd", ".bat"))
                else [exe])
        argv += ["-p", "--output-format", "json", "--model", self.model,
                 "--disallowedTools", ",".join(self.OWN_TOOLS)]
        sys_file = None
        if resume:
            argv += ["--resume", resume]
        else:
            # A 25k-char system prompt does not fit a Windows command line; and
            # --strict-mcp-config with an empty config keeps every MCP server (and its
            # schemas) out of the brain's context.
            fd, sys_file = tempfile.mkstemp(prefix="parley-sys-", suffix=".txt")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(system_text)
            argv += ["--system-prompt-file", sys_file,
                     "--exclude-dynamic-system-prompt-sections"]
        try:
            # The prompt goes on stdin, not argv: a transcript can be long, and
            # Windows caps a command line at ~32k characters.
            proc = subprocess.run(argv, input=prompt, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", cwd=self.cwd,
                                  timeout=self.timeout)
        except subprocess.TimeoutExpired as err:
            self._last = None
            raise RuntimeError(
                f"claude -p timed out after {self.timeout}s") from err
        finally:
            if sys_file:
                try:
                    os.unlink(sys_file)
                except OSError:
                    pass

        envelope = self._envelope(proc)
        response = self._parse_result(envelope.get("result") or "")
        response.usage = self._usage(envelope.get("usage") or {})
        session = envelope.get("session_id") or ""
        # Anchor the NEXT call: everything we have sent so far, under this system text.
        self._last = ({"system": system_text, "turns": len(transcript), "session": session}
                      if session else None)
        return response

    def ping(self, timeout=120):
        """One tiny `claude -p`: proves the CLI is on PATH, logged in, and that
        `model` answers — the keyless stand-in for pinging an API key. Returns the
        concrete id the CLI ran (an alias comes back as the newest of its family).
        Raises with the CLI's own message on any failure."""
        import shutil
        import subprocess
        from parley.agent.session_supervisor import child_env

        exe = shutil.which(self.cli)
        if not exe:
            raise RuntimeError(f"the Claude Code CLI ({self.cli!r}) is not on PATH")
        argv = (["cmd", "/c", exe] if exe.lower().endswith((".cmd", ".bat"))
                else [exe])
        argv += ["-p", "--output-format", "json", "--model", self.model,
                 "--disallowedTools", ",".join(self.OWN_TOOLS)]
        try:
            # child_env: the wizard may itself run inside Claude Code; no window: the
            # installer runs the wizard windowless and a console would flash up.
            proc = subprocess.run(argv, input="Reply with the single word: pong",
                                  capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", timeout=timeout, env=child_env(),
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired as err:
            raise RuntimeError(f"claude -p did not answer within {timeout}s") from err
        used = list((self._envelope(proc).get("modelUsage") or {}).keys())
        family = self.model.lower() if is_model_alias(self.model) else ""
        return next((m for m in used if family and family in m.lower().split("-")),
                    used[0] if used else self.model)

    # -- session planning ----------------------------------------------------

    def _plan(self, system_text, transcript):
        """(session-to-resume or "", the turns still to send). Resume only when the
        system prompt is unchanged AND the transcript strictly extends what we already
        sent — anything else is a fresh session, because a resumed session carries the
        OLD system prompt and with it an old sense of what time it is."""
        last = self._last
        if (last and last.get("session") and last.get("system") == system_text
                and len(transcript) > last["turns"]):
            return last["session"], list(transcript[last["turns"]:])
        return "", list(transcript)

    # -- pure mappers (no subprocess; the selftest drives these directly) -----

    @classmethod
    def _system_prompt(cls, system, tools):
        """Parley's system prompt + the answering contract + the tool schemas. Claude
        Code takes no tool definitions from a caller, so the schemas have to travel as
        text and the model answers with the call it *would* have made."""
        schemas = [{"name": t.name, "description": t.description,
                    "input_schema": t.input_schema} for t in tools or []]
        return (system or "") + _CLAUDE_CODE_CONTRACT + json.dumps(schemas, indent=1)

    @classmethod
    def _render(cls, turns):
        """Neutral turns → the plain text one `claude -p` invocation receives. Tool
        results are labelled with the tool they answer, since this wire has no
        structural tool_result block to correlate them."""
        names = {}
        for item in turns:
            for tc in item.get("tool_calls") or []:
                names[tc.id] = tc.name
        out = []
        for item in turns:
            role = item.get("role")
            if role == "user":
                out.append(item.get("content", ""))
            elif role == "assistant":
                if item.get("text"):
                    out.append("You said: " + item["text"])
                for tc in item.get("tool_calls") or []:
                    out.append(f"You called {tc.name}({json.dumps(tc.input or {})})")
            elif role == "tool":
                tid = item.get("tool_call_id", "")
                label = names.get(tid) or "a tool"
                out.append(f"Result of {label}: {item.get('content', '')}")
        return "\n\n".join(out)

    @staticmethod
    def _envelope(proc):
        """The CLI's `--output-format json` wrapper. Every failure is loud: a silent
        empty answer would read to the brain as 'the model had nothing to do'."""
        raw = (proc.stdout or "").strip()
        if not raw:
            raise RuntimeError(
                f"claude -p produced no output (exit {proc.returncode}): "
                f"{(proc.stderr or '').strip()[:300]}")
        try:
            envelope = json.loads(raw)
        except ValueError as err:
            raise RuntimeError(f"claude -p returned non-JSON: {raw[:300]}") from err
        if envelope.get("is_error"):
            raise RuntimeError(f"claude -p failed: {str(envelope.get('result'))[:300]}")
        return envelope

    @classmethod
    def _parse_result(cls, text):
        """The model's answer → neutral `LLMResponse`. Tool-call ids are minted here:
        this wire has none, and the brain only needs them to correlate results back
        within the turn it is running."""
        data = cls._extract_json(text)
        if data is None:
            # Not JSON at all: treat it as a finished answer rather than crashing the
            # turn. A model that stops asking for tools is allowed to just talk.
            return LLMResponse(text=(text or "").strip(), stop_reason="text")
        calls = []
        for i, call in enumerate(data.get("tool_calls") or []):
            name = (call.get("name") or "").strip()
            if not name:
                continue
            args = call.get("input")
            if isinstance(args, str):
                try:
                    args = json.loads(args or "{}")
                except ValueError:
                    args = {}
            calls.append(ToolCall(f"cc-{i + 1}", name, dict(args or {})))
        return LLMResponse(text=str(data.get("text") or ""), tool_calls=calls,
                           stop_reason="tool_use" if calls else "end_turn")

    @staticmethod
    def _extract_json(text):
        """The object, however it was wrapped: bare, inside a ```json fence, or with
        prose either side. Returns None when there is no object to find."""
        raw = (text or "").strip()
        if not raw:
            return None
        try:
            found = json.loads(raw)
            return found if isinstance(found, dict) else None
        except ValueError:
            pass
        start = raw.find("{")
        while start != -1:
            depth, in_str, escaped = 0, False, False
            for i in range(start, len(raw)):
                ch = raw[i]
                if in_str:
                    if escaped:
                        escaped = False
                    elif ch == "\\":
                        escaped = True
                    elif ch == '"':
                        in_str = False
                    continue
                if ch == '"':
                    in_str = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            found = json.loads(raw[start:i + 1])
                        except ValueError:
                            break
                        if isinstance(found, dict):
                            return found
                        break
            start = raw.find("{", start + 1)
        return None

    @staticmethod
    def _usage(u):
        if not u:
            return {}
        return {"input": u.get("input_tokens", 0),
                "output": u.get("output_tokens", 0),
                "cache_read": u.get("cache_read_input_tokens", 0),
                "cache_write": u.get("cache_creation_input_tokens", 0)}


# --- Fake adapter (offline verification, no key, no SDK) ---------------------

class FakeLLMClient(LLMClient):
    """A scripted client for tests. Give it a `planner(system, transcript, tools) ->
    LLMResponse`, or a list of LLMResponses returned in order. Lets the brain's loop
    be exercised deterministically with no API key (used while building; the shipped
    `--selftest` is keyed per decision #6, but this keeps a token-free unit path)."""

    def __init__(self, script=None, planner=None):
        self._script = list(script or [])
        self._planner = planner
        self.calls = []  # (system, transcript-snapshot, tools) for assertions

    def complete(self, system, transcript, tools):
        self.calls.append((system, list(transcript), tools))
        if self._planner is not None:
            return self._planner(system, transcript, tools)
        if self._script:
            return self._script.pop(0)
        return LLMResponse(text="(done)", tool_calls=[])


# --- factory -----------------------------------------------------------------

def build_llm_client(provider="anthropic", api_key=None, model=None, base_url=None):
    """Construct the configured provider's adapter. `base_url` is the OpenAI-compat
    endpoint (ollama); ignored by the others. Raises on an unknown provider."""
    provider = (provider or "anthropic").strip().lower()
    if provider == "anthropic":
        return AnthropicClient(api_key=api_key, model=model)
    if provider == "ollama":
        return OllamaClient(model=model, base_url=base_url, api_key=api_key)
    if provider in ("claude-code", "claude_code", "claudecode"):
        return ClaudeCodeClient(model=model)
    raise ValueError(
        f"unknown LLM provider '{provider}' "
        f"(wired: 'anthropic', 'ollama', 'claude-code')")
