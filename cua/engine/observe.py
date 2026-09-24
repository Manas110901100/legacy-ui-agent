"""
observe.py - look, understand, act: the three things every step does.

    observe(job)            screenshot + parse of the app window in front, located on the map
    check_screen(job, s)    what the window means (outcomes.classify): business outcome, error to
                            recover from, a window this task has no business in, or ordinary
    resolve(target, s, job) a saved target (role + label, or table row by parameter) -> element
    act(job, event, el, s)  do it through the surface; logged with {placeholders}, never values
"""
import time

from cua import settings
from cua.artifact import outcomes
from cua.engine import hooks
from cua.engine.text import describe, fill, norm, sim
from cua.errors import Outcome, Stop, Stuck
from cua.learning import screenmap
from cua.safety import policy


def observe(job, pausable=True):
    """Look at the app window in front, locate it on the map.
    pausable: a switch to Human (the panel's toggle) is taken here, as a hand-over."""
    hooks.check_abort()
    hooks.heartbeat()
    if time.monotonic() > job.deadline:
        raise Stop(f"the run took longer than {job.profile.timeout:.0f} s", code="timeout")
    if pausable and hooks.TAKEOVER.is_set():
        hooks.TAKEOVER.clear()
        raise Stuck(hooks.TOOK_OVER, code="operator_takeover")
    surf = job.surface
    for _ in range(25):
        fg = surf.foreground()
        over = surf.covering(fg) if surf.owns(fg) else []
        if not over:
            break
        top = surf.title(over[0])
        if norm(top).startswith(job.profile.busy):          # "Please Wait" comes and goes
            surf.wait(0.4)
            continue
        job.run.log(f"switched to window '{top}'")          # the app opened it on top without activating it
        surf.front(over[0])
        surf.wait(0.4)
    else:
        raise Stuck(f"could not bring the window '{top}' to the front", code="window_blocked")
    s = surf.capture()
    if s is None:
        raise Stop(f"focus left the application (foreground window: '{surf.title(surf.foreground())}')",
                   code="focus_lost")
    job.run.save_screen(s)
    owner = surf.owner(s.hwnd)
    parent = job.hwnd_nodes.get(owner) or (job.map.root if owner and owner == surf.main else None)
    s.node = job.map.learn_screen(s.skeleton, surf.kind(s.hwnd), parent)
    job.hwnd_nodes[s.hwnd] = s.node
    if job.pending:
        job.map.learn_edge(job.pending[0], job.pending[1], s.node, "agent")
        job.pending = None
    job.map.save()
    job.run.event("observe", window=screenmap.mask_title(s.title), node=s.node, step=job.step_id)
    job.screen = s
    return s


def check_screen(job, screen):
    """What does this window mean? business -> Outcome raised; escalate / hard -> Stuck raised;
    recoverable (busy, transient) -> returned so the caller can handle it; ordinary -> None."""
    texts = [e["text"] for e in screen.elements if e["kind"] in ("text", "label", "status")]
    cond = outcomes.classify(job.profile, screen.title, texts)
    if cond:
        job.run.event("condition", code=cond.code, kind=cond.kind, window=screen.title, message=cond.message)
        if cond.kind == "business":
            raise Outcome(cond.message or cond.title, code=cond.code, observed=screen.title)
        if cond.kind == "recoverable":
            return cond
        raise Stuck(f"{cond.title}: {cond.message}".strip(": "), code=cond.code, observed=screen.title)
    problem = policy.check_screen(job.intent, screen.title)
    if problem:
        raise Stuck(problem, code="unknown_window", observed=screen.title)
    return None


def dismiss(job, screen, cond):
    """Close a known interstitial with its default button (Enter)."""
    job.run.log(f"dismissing '{cond.title}' ({cond.code}): {cond.message}")
    act(job, {"type": "key", "keys": "enter"}, None, screen)


def resolve(target, screen, job):
    """Semantic target (or element id) -> element on the current screen."""
    if "id" in target:
        el = next((e for e in screen.elements if e["id"] == target["id"]), None)
        if not el:
            raise Stuck(f"element id '{target['id']}' not on screen", code="target_missing")
        return el
    if target["kind"] == "table_row":
        return _resolve_row(target, screen, job)
    cands = [e for e in screen.elements if e["kind"] == target["kind"]]
    best = max(cands, key=lambda e: sim(e["key"], target["key"]), default=None)
    if not best or sim(best["key"], target["key"]) < settings.TEXT_MATCH:
        raise Stuck(f"{target['kind']} '{target['key']}' not found on screen", code="target_missing",
                    expected=f"{target['kind']} '{target['key']}'",
                    observed=f"window '{screen.title}' with {', '.join(e['key'] for e in cands[:8]) or 'none'}")
    return best


def _resolve_row(target, screen, job):
    """A row by position, or by a parameter: exact in the learned column, then in any column, then
    as part of a value (a name). Several fit -> the user picks (or ambiguous_match unattended)."""
    table = screen.view.get("table")
    if not table:
        raise Stuck("expected a table on screen", code="target_missing", expected="a table", observed=screen.title)
    rows = table["rows"]
    if "row" in target:
        if target["row"] > len(rows):
            raise Stuck(f"expected at least {target['row']} rows in the table", code="target_missing")
        row = rows[target["row"] - 1]
    else:
        (col, want), = target["match"].items()
        want = norm(fill(want, job.params))
        k = table["columns"].index(col) if col in table["columns"] else 0
        fits = (lambda v: want in norm(v)) if target.get("contains") else (lambda v: norm(v) == want)
        hits = [r for r in rows if fits(r["values"][k])]
        if not hits and "{" in str(target["match"][col]):     # e.g. learned with a name, now an account no.
            hits = [r for r in rows if any(fits(v) for v in r["values"])] or \
                [r for r in rows if any(want in norm(v) for v in r["values"])]   # or part of a name
        if not hits:
            raise Stuck(f"no visible row with {col} = '{want}'", code="target_missing",
                        expected=f"a row with {col} = {{value}}", observed=f"{len(rows)} rows")
        row = hits[0] if len(hits) == 1 else pick_row(job, screen, table, hits, want)
    return next(e for e in screen.elements if e["id"] == row["id"])


def pick_row(job, screen, table, rows, want):
    """Several rows fit: the user picks one (real values, shown locally only). Remembered for the run.
    Unattended, this is a business outcome the caller has to resolve."""
    if not job.interactive:
        raise Outcome(f"{len(rows)} entries match - give a more specific value", code="ambiguous_match")
    key = f"{'|'.join(table['columns'])}:{want}"
    if key in job.picks:
        row = next((r for r in rows if r["values"] == job.picks[key]), None)
        if row:
            return row
    options = [" | ".join(v for v in r["values"][:4] if v) for r in rows]
    n = job.ask(hooks.choose, f"{len(rows)} entries match '{want}' - which one?", options)
    if n is None:
        raise Stop("no entry chosen", code="cancelled")
    job.picks[key] = rows[n]["values"]
    job.run.log(f"user picked entry {n + 1} of {len(rows)}")
    job.surface.refocus(screen)
    return rows[n]


def to_semantic(el, screen, params):
    """Element chosen by the LLM (or a person) -> target that survives new ids/positions/values."""
    if el["kind"] == "table_row":
        cols = screen.view["table"]["columns"]
        for name, val in params.items():                 # a column holding a parameter
            for c, v in zip(cols, el["values"]):
                if v and norm(v) == norm(val):
                    return {"kind": "table_row", "match": {c: "{" + name + "}"}}
        for name, val in params.items():                 # ...or containing it ("sarah" in "Sarah Miller")
            for c, v in zip(cols, el["values"]):
                if v and len(norm(val)) >= 2 and norm(val) in norm(v):
                    return {"kind": "table_row", "match": {c: "{" + name + "}"}, "contains": True}
        return {"kind": "table_row", "row": int(el["id"].rsplit("_", 1)[1])}   # position, never the data
    return {"kind": el["kind"], "key": el["key"]}


def act(job, event, el, screen):
    """Do one step on the surface. Logged with placeholders - typed values never reach the log."""
    hooks.check_abort()
    value = fill(event.get("text", event.get("value", "")), job.params) if event["type"] in ("type", "select") \
        else None
    job.surface.act(event, el, screen, value)
    job.run.log(f"  did: {describe(event, event.get('target'))}")
    job.run.event("action", step=job.step_id, action=event["type"], target=event.get("target"),
                  value=event.get("text", event.get("value")), window=screenmap.mask_title(screen.title))
    job.pending = (screen.node, event)
