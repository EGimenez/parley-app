"""Install Parley's Claude Code skills, so they work outside a checkout.

Piece 4 of the human surface. The skills ship inside the package (see pyproject's
package-data) and this copies them to `~/.claude/skills/<name>/`, which is where Claude
Code looks for a user's own skills. Without that, `/parley` only exists when the user
happens to be working inside the Parley repository — which is precisely where they are
NOT when they want to arrange a meeting.

Two skills ship today:
  * `parley`      — talk to the scheduling agent (arrange, answer, set rules).
  * `myorganizer` — organise the user's work across the sources they pick at first run.
A skill is a directory, not a file: SKILL.md plus whatever `references/` and `scripts/`
it brings. Everything under the shipped directory is copied as is.

Nothing here registers an MCP server, and that is deliberate. The only consumer of
Parley's `human` MCP role is the session the tray launches, which writes its own
`--mcp-config` pointing at that one agent's port; a globally registered server would
have to name a single daemon and would be wrong for every other agent. The skills
themselves reach the agent through the `parley` CLI, which resolves the right one.

Ownership. Each installed directory gets a small manifest (MANIFEST_NAME) listing the
files we put there. Uninstalling removes exactly those, so a user who has added their
own notes beside them keeps them. Installing refuses a directory that already holds a
skill of the user's own with the same name (no manifest, different content): we do not
overwrite what we did not write. `parley` is exempt from that refusal because versions
before the manifest installed it without one.

Idempotent in both directions: installing over our own copy refreshes it, removing one
that is not there is not an error.
"""

import json
import os
import shutil

SKILLS = ("parley", "myorganizer")
MANIFEST_NAME = ".parley-skill.json"
# Installed by earlier versions without a manifest: an existing copy is ours.
_LEGACY_UNMANIFESTED = {"parley"}


def skills_root():
    """Where the skills live inside the package — the canonical copies."""
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "skills")


def source_dir(name="parley"):
    return os.path.join(skills_root(), name)


def source_path(name="parley"):
    return os.path.join(source_dir(name), "SKILL.md")


def claude_home():
    return os.path.join(os.path.expanduser("~"), ".claude")


def target_dir(name="parley"):
    return os.path.join(claude_home(), "skills", name)


def target_path(name="parley"):
    return os.path.join(target_dir(name), "SKILL.md")


def shipped_files(name="parley"):
    """Relative paths (forward slashes) of every file the skill ships, sorted."""
    root = source_dir(name)
    out = []
    for here, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for f in files:
            if f.endswith((".pyc", ".pyo")):
                continue
            rel = os.path.relpath(os.path.join(here, f), root)
            out.append(rel.replace(os.sep, "/"))
    return sorted(out)


def _read(path):
    with open(path, "rb") as f:
        return f.read()


def _manifest(name):
    """The files we installed last time, or None when there is no manifest."""
    try:
        with open(os.path.join(target_dir(name), MANIFEST_NAME), encoding="utf-8") as f:
            return json.load(f).get("files", [])
    except (OSError, ValueError):
        return None


def _is_ours(name):
    """May we write into target_dir(name)? Yes when it is absent, carries our manifest,
    is a legacy install, or already holds exactly what we ship."""
    if not os.path.exists(target_path(name)):
        return True
    if _manifest(name) is not None or name in _LEGACY_UNMANIFESTED:
        return True
    try:
        return _read(target_path(name)) == _read(source_path(name))
    except OSError:
        return False


def status(name=None):
    """(installed, up_to_date, path). `up_to_date` compares content, not mtime: an
    upgraded Parley must be able to say an installed skill is stale. With no name,
    the answer covers every shipped skill: installed / current only if all are."""
    if name is None:
        each = [status(n) for n in SKILLS]
        return (all(s[0] for s in each), all(s[1] for s in each),
                os.path.join(claude_home(), "skills"))
    target = target_path(name)
    if not os.path.exists(target):
        return False, False, target
    try:
        current = all(_read(os.path.join(target_dir(name), *rel.split("/"))) ==
                      _read(os.path.join(source_dir(name), *rel.split("/")))
                      for rel in shipped_files(name))
    except OSError:
        current = False
    return True, current, target


def install(name=None):
    """Copy the shipped skill(s) into ~/.claude/skills/. Returns (ok, detail).
    With no name, installs every shipped skill and reports them together."""
    if name is None:
        results = [(n, install(n)) for n in SKILLS]
        ok = all(r[0] for _, r in results)
        detail = "; ".join(f"/{n}: {r[1]}" for n, r in results)
        return ok, detail
    if not os.path.exists(source_path(name)):
        return False, f"the shipped skill is missing at {source_path(name)}"
    if not _is_ours(name):
        return False, (f"{target_dir(name)} holds a skill of your own with that name; "
                       f"left untouched")
    files = shipped_files(name)
    try:
        for rel in files:
            dest = os.path.join(target_dir(name), *rel.split("/"))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copyfile(os.path.join(source_dir(name), *rel.split("/")), dest)
        # Files a previous version shipped and this one does not: ours, so they go.
        for rel in set(_manifest(name) or []) - set(files):
            stale = os.path.join(target_dir(name), *rel.split("/"))
            if os.path.isfile(stale):
                os.remove(stale)
        with open(os.path.join(target_dir(name), MANIFEST_NAME), "w",
                  encoding="utf-8") as f:
            json.dump({"skill": name, "files": files}, f, indent=2)
    except OSError as err:
        return False, f"could not write {target_dir(name)}: {err}"
    return True, target_path(name)


def _prune_empty(directory):
    """Remove empty subdirectories bottom-up, the directory itself included."""
    for here, _dirs, _files in os.walk(directory, topdown=False):
        if not os.listdir(here):
            os.rmdir(here)


def uninstall(name=None):
    """Remove it again. Only ever removes the files we put there — the manifest says
    which — and a user who has added their own notes beside them keeps them, and is
    told so. With no name, uninstalls every shipped skill."""
    if name is None:
        results = [(n, uninstall(n)) for n in SKILLS]
        ok = all(r[0] for _, r in results)
        detail = "; ".join(f"/{n}: {r[1]}" for n, r in results)
        return ok, detail
    directory = target_dir(name)
    if not os.path.isdir(directory):
        return True, "was not installed"
    owned = _manifest(name)
    if owned is None:
        if name not in _LEGACY_UNMANIFESTED:
            return True, f"{directory} is not ours; left untouched"
        owned = ["SKILL.md"]
    try:
        for rel in owned + [MANIFEST_NAME]:
            path = os.path.join(directory, *rel.split("/"))
            if os.path.isfile(path):
                os.remove(path)
        _prune_empty(directory)
    except OSError as err:
        return False, f"could not remove {directory}: {err}"
    if os.path.isdir(directory):
        extra = sorted(os.listdir(directory))
        return True, f"removed ours but kept {directory} ({', '.join(extra)} is yours)"
    return True, directory
