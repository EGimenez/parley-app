"""Change an agent's engine in place: only the `llm:` / `llm-gateway:` blocks.

The wizard writes a whole cfg from a template; changing the engine afterwards
(the tray's Settings window, the wizard on an existing cfg) must not. A cfg holds
live credentials, teaching comments and hand edits, and PyYAML would drop every
comment on a round trip — so this edits TEXT, line by line:

  * inside the two blocks, only the ENGINE keys are replaced (provider, model,
    key, base_url — and `driver`, which only means something on claude-code);
    every other key (`tz:` ...) and every comment line stays where it was;
  * every line outside the two blocks is untouched, byte for byte;
  * a gateway switched off is replaced by the commented hint, and back.

The block text comes from the same templates the wizard writes with, so a cfg
looks the same whichever surface last touched it. `write_llm` proves the result
through the real loader before it replaces the file.
"""

import os
import re

# The `model` lines take a family name ("opus" / "sonnet" / "haiku" = always the
# newest of that family) or a full id that pins one release.
LLM_KEY_LINES = """\
  anthropic_key: "{key}"
  model: "{model}"   # "opus" = always the newest Opus; a full id pins one release
"""

LLM_CLAUDE_CODE_LINES = """\
  # The Claude Code CLI is the brain: it runs on your claude.ai login, so there is
  # no API key here. Needs `claude` on PATH and logged in. Slower per step than a key.
  provider: claude-code
  model: "{model}"   # "opus" = always the newest Opus; a full id pins one release
  # driver: session   # optional: hand each event to a background Claude Code session
"""

GATEWAY_BLOCK = """\
# The gateway's intake classifier — a SMALL model that triages plain mail
# (ADMIT/IGNORE) before the brain ever runs; fails open to the keyword heuristic.
# It uses the brain's engine (and key) unless it names its own.
llm-gateway:
  model: "{gateway_model}"
{gateway_key_line}"""

NO_GATEWAY_BLOCK = """\
# No gateway model: a keyword list decides which plain mails reach the brain.
# To let a small model decide instead (any phrasing, any language), add:
#   llm-gateway:
#     model: "haiku"
"""

# Keys that ARE the engine. Anything else in the block is the user's and stays.
_ENGINE_KEYS = {"provider", "model", "anthropic_key", "anthropic_key_file", "base_url"}
_KEY_RE = re.compile(r"^  ([A-Za-z_][\w-]*)\s*:")
_TOP_RE = re.compile(r"^([A-Za-z_][\w-]*)\s*:")


def yaml_str(value):
    """Escape a python string for a double-quoted YAML scalar."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def llm_lines(llm):
    """The engine lines under `llm:` for a step_llm-shaped dict."""
    if llm["provider"] == "claude-code":
        return LLM_CLAUDE_CODE_LINES.format(model=yaml_str(llm["model"]))
    return LLM_KEY_LINES.format(key=yaml_str(llm["anthropic_key"]),
                                model=yaml_str(llm["model"]))


def gateway_block(llm):
    """The whole `llm-gateway:` block (with its header comment), or the hint."""
    if not llm["gateway_model"]:
        return NO_GATEWAY_BLOCK
    key_line = ""
    if llm.get("gateway_key"):
        key_line = f'  anthropic_key: "{yaml_str(llm["gateway_key"])}"  # its own key\n'
    return GATEWAY_BLOCK.format(gateway_model=yaml_str(llm["gateway_model"]),
                                gateway_key_line=key_line)


def _block(lines, name):
    """(start, end) of a top-level `name:` mapping — end exclusive, trailing blank
    lines left outside — or None. A block ends at the next column-0 line."""
    start = next((i for i, l in enumerate(lines) if _TOP_RE.match(l)
                  and _TOP_RE.match(l).group(1) == name), None)
    if start is None:
        return None
    end = start + 1
    while end < len(lines) and (not lines[end].strip() or lines[end][0] in " \t"):
        end += 1
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    return start, end


def _header(lines, start):
    """Index of the first of the column-0 comment lines directly above `start`."""
    while start > 0 and lines[start - 1].startswith("#"):
        start -= 1
    return start


def _kept(body, drop, template):
    """The lines of a block body that survive: not an engine key (with the deeper-
    indented lines that continue it), and not a comment our own template writes
    (it is re-emitted fresh, so keeping the old copy would duplicate it)."""
    ours = {l.strip() for l in template.splitlines() if l.strip().startswith("#")}
    kept, dropping = [], False
    for line in body:
        key = _KEY_RE.match(line)
        if key:
            dropping = key.group(1) in drop
        elif line.startswith("   ") and not line.lstrip().startswith("#"):
            pass  # continuation of the key above: follows its decision
        else:
            dropping = False
            if line.strip() in ours:
                continue
        if not dropping:
            kept.append(line)
    return kept


def set_llm(text, llm):
    """`text` (a whole cfg) with its engine set to `llm` (step_llm's dict:
    provider, anthropic_key, model, gateway_model, gateway_key)."""
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    keyless = llm["provider"] == "claude-code"
    templates = LLM_KEY_LINES + LLM_CLAUDE_CODE_LINES

    # llm: — engine keys out, the new ones in front of whatever the user keeps.
    drop = _ENGINE_KEYS | ({"driver"} if not keyless else set())
    new_llm = ["llm:\n"] + llm_lines(llm).splitlines(keepends=True)
    span = _block(lines, "llm")
    if span:
        start, end = span
        lines[start:end] = new_llm + _kept(lines[start + 1:end], drop, templates)
    else:
        at = _block(lines, "account")
        at = _header(lines, at[0]) if at else len(lines)
        lines[at:at] = new_llm + ["\n"]

    # llm-gateway: — same, or swapped for / from the commented hint.
    span = _block(lines, "llm-gateway")
    hint = NO_GATEWAY_BLOCK.splitlines(keepends=True)
    if span:
        start, end = span
        if llm["gateway_model"]:
            new_gw = gateway_block(llm).splitlines(keepends=True)
            head = new_gw[:new_gw.index("llm-gateway:\n") + 1]
            engine = new_gw[len(head):]
            kept = _kept(lines[start + 1:end], _ENGINE_KEYS, GATEWAY_BLOCK)
            lines[start:end] = ["llm-gateway:\n"] + engine + kept
            # The header comment is re-emitted only when the old one was ours.
            top = _header(lines, start)
            if "".join(lines[top:start]) == "".join(head[:-1]) or top == start:
                lines[top:start] = head[:-1]
        else:
            lines[_header(lines, start):end] = hint
    elif llm["gateway_model"]:
        block = gateway_block(llm).splitlines(keepends=True)
        at = next((i for i in range(len(lines) - len(hint) + 1)
                   if lines[i:i + len(hint)] == hint), None)
        if at is not None:
            lines[at:at + len(hint)] = block
        else:
            end = _block(lines, "llm")[1]
            lines[end:end] = ["\n"] + block
    return "".join(lines)


def write_llm(path, llm):
    """Set the engine of the cfg at `path`, atomically, after proving the result
    through the real loader. The previous file is kept as `<path>.bak`."""
    import shutil
    import yaml
    from parley.client_server.config import _model_name, load_llm

    with open(path, encoding="utf-8") as f:
        old = f.read()
    new = set_llm(old, llm)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(new)
    try:
        with open(tmp, encoding="utf-8") as f:
            yaml.safe_load(f)
        cfg = load_llm(tmp)
        expected = "claude-code" if llm["provider"] == "claude-code" else ""
        if (cfg.provider or "") != expected or cfg.model != _model_name(llm["model"]) \
                or cfg.gateway_model != _model_name(llm["gateway_model"]):
            raise ValueError("the rewritten cfg does not read back as the chosen engine")
    except BaseException:
        os.remove(tmp)
        raise
    shutil.copyfile(path, path + ".bak")
    os.replace(tmp, path)
    return new
