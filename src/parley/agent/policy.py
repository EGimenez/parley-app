"""Policy — data, not code (prose + trust table + hard limits).

A policy is a plain document with three kinds of content, each with one owner:

  * unstructured — free natural-language guidance the **brain** follows (judgment):
                   "ask me before accepting new meetings", "prefer mornings".
                   Extended at runtime by the learned-policy file (remember_policy).
  * structured["trust"] — the per-principal **trust table** the AccountsGate's
                   permission hook enforces (authority): principal → auto | ask |
                   ignore, with optional per-verb overrides.
  * structured (rest) — hard limits and shims infra reads (e.g. the retired
                   `accept_requests_from` list, folded into the table).

There is no policy class hierarchy and no decision engine here: callers *read* a
Policy; the permission hook enforces its table; the brain follows its prose.

**Global + per-account delta (2026-08-31 refactor).** One agent has ONE base
policy, shared across every account, plus an optional per-account delta that
layers on top — `effective_policy(base, override)` does the merge. This is
additive over the old shape, not a breaking change: a cfg with no top-level
`policy:` has an empty base, so an account's own `policy:` block simply IS its
whole effective policy, exactly as before.
"""

import json
from dataclasses import dataclass, field


@dataclass
class Policy:
    """The faces of a policy, as plain data."""

    structured: dict = field(default_factory=dict)
    unstructured: str = ""

    @classmethod
    def from_obj(cls, obj):
        """Build from either the explicit `{structured: {...}}` shape or the flat
        YAML shape, where `unstructured` (the prose) sits next to structured keys
        (`trust`, `accept_requests_from`, …) — the flat keys ARE the structured
        face. (`unstructured` next to flat keys used to discard them — fixed.)"""
        obj = obj or {}
        if "structured" in obj:
            return cls(structured=dict(obj.get("structured") or {}),
                       unstructured=str(obj.get("unstructured") or ""))
        return cls(structured={k: v for k, v in obj.items() if k != "unstructured"},
                   unstructured=str(obj.get("unstructured") or ""))


def load_policy(source):
    """Accept a Policy, a dict, or a path to a JSON file → a Policy."""
    if isinstance(source, Policy):
        return source
    if isinstance(source, str):
        with open(source, encoding="utf-8") as f:
            return Policy.from_obj(json.load(f))
    return Policy.from_obj(source)


def effective_policy(base, override):
    """The policy an account actually runs under: `base` (the agent-wide policy)
    with `override` (that account's own `policy:` block, if any) layered on top.

    * unstructured — concatenated, override AFTER base, so "more specific wins"
      extends the same resolution order intuition.md already states for
      explicit-vs-implicit policy (account delta > agent-wide explicit > implicit
      baseline > ask).
    * trust — dict-merged per principal; an override entry for a principal beats
      the base entry for THAT account only, other accounts are untouched.
    * everything else in `structured` — override wins where present, base fills
      the rest.
    """
    base, override = base or Policy(), override or Policy()
    unstructured = "\n\n".join(p for p in (base.unstructured, override.unstructured) if p)
    structured = dict(base.structured)
    structured.update({k: v for k, v in override.structured.items() if k != "trust"})
    trust = {**(base.structured.get("trust") or {}), **(override.structured.get("trust") or {})}
    if trust:
        structured["trust"] = trust
    return Policy(structured=structured, unstructured=unstructured)


def addr_set(value):
    """A YAML/JSON array (list) or a comma-separated string → a lowercased set."""
    if not value:
        return set()
    items = value if isinstance(value, (list, tuple, set)) else str(value).split(",")
    return {str(a).strip().lower() for a in items if str(a).strip()}
