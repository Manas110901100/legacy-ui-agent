"""text.py - small text helpers the engine shares: fuzzy comparison, {placeholders}, step descriptions."""
import difflib
import json
import re

from cua.errors import Stop


def sim(a, b):
    return difflib.SequenceMatcher(None, str(a).lower().strip(), str(b).lower().strip()).ratio()


def norm(v):
    return re.sub(r"\s+", " ", str(v)).strip().lower()


def fill(text, params):
    """Replace {placeholders} with parameter values."""
    def rep(m):
        if m.group(1) not in params:
            raise Stop(f"missing parameter '{m.group(1)}'", code="bad_step")
        return str(params[m.group(1)])
    return re.sub(r"\{(\w+)\}", rep, str(text))


def shown(text, params):
    """{placeholders} -> values for messages to the user; unknown ones stay as they are."""
    return re.sub(r"\{(\w+)\}", lambda m: str(params.get(m.group(1), m.group(0))), str(text))


def placeholders(obj):
    return set(re.findall(r"\{(\w+)\}", json.dumps(obj)))


def parameterize(text, params):
    """Typed literal equal to a parameter value -> {placeholder}."""
    for name, val in params.items():
        if norm(text) == norm(val):
            return "{" + name + "}"
    return text


def describe(ev, target):
    t = target.get("key") or target.get("match") or target.get("id") or \
        (f"row {target['row']}" if "row" in target else "") if target else ""
    extra = ev.get("text") or ev.get("value") or ev.get("keys") or ""
    return f"{ev['type']} {t} {extra}".strip()


def describe_step(step):
    if step.get("human"):
        return f"[person] {step['reason']}"
    return describe(step["event"], step["event"].get("target"))
