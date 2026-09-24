"""
checks.py - is the task really done, and what did it produce? Shared by discovery and replay.

    verify(check, screen, params)    the checkpoint: window title pattern, visible text, table cell
    read_outputs(job, final, specs)  the declared outputs, read from the screen (artifact/outputs.py)
    check_result(job, values)        the result is about the record that was asked for (record_key)
    answer_text(values)              outputs -> a short answer for a person
"""
import re

from cua.artifact import outputs
from cua.engine.text import fill, norm
from cua.errors import Stuck


def matches(pattern, text, whole=False):
    """pattern may contain * (any text: account numbers etc. that differ every run)."""
    rx = ".*?".join(re.escape(p) for p in norm(pattern).split("*"))
    return re.fullmatch(rx, norm(text)) if whole else re.search(rx, norm(text))


def verify(check, screen, params):
    """Checkpoint: every part of it must hold on the final screen."""
    if not check:
        return True
    if "title" in check and not matches(fill(check["title"], params), screen.title, whole=True):
        return False
    if "table_contains" in check:
        table = screen.view.get("table")
        if not table:
            return False
        for col, val in check["table_contains"].items():
            if col not in table["columns"]:
                return False
            k, want = table["columns"].index(col), fill(val, params)
            if not any(matches(want, r["values"][k], whole=True) for r in table["rows"]):
                return False
    if "text_visible" in check:
        want = fill(check["text_visible"], params)
        if not any(matches(want, e.get("text", "")) for e in screen.elements):
            return False
    return True


def read_outputs(job, final, specs):
    values, warnings = outputs.extract_all(specs, final.view, final.elements, job.params)
    for w in warnings:
        job.run.log(f"  output warning - {w}")
    missing = [k for k, v in values.items() if v is None and specs[k]["type"] not in ("list", "table")]
    if missing:
        raise Stuck(f"could not read {', '.join(missing)} on the final screen", code="output_missing",
                    expected=", ".join(f"{k} via {specs[k]['extract']}" for k in missing), observed=final.title)
    return values


def check_result(job, values):
    """The result must be about the record that was asked for: every record-key input has to show up in
    the outputs (ACC100012 -> account_no ACC100012; 'sarah' -> customer 'Sarah Jones'). Catches a flow -
    or a person during a hand-over - that ended on the wrong record."""
    keys = {s.name for s in job.intent.slots if s.record_key}
    if job.cap:
        keys |= {k for k, spec in job.cap.inputs.items() if spec.record_key}
    shown = [norm(v) for v in values.values() if isinstance(v, (str, int)) and str(v)]
    for k in sorted(keys):
        want = norm(job.params.get(k, ""))
        if want and not any(want == v or want in v for v in shown):
            observed = ", ".join(f"{n}={v}" for n, v in values.items() if isinstance(v, (str, int))
                                 and not (job.intent.outputs.get(n) and job.intent.outputs[n].pii))
            raise Stuck(f"the window shows another record than the one asked for ({k})", code="result_mismatch",
                        expected=f"{k} = {{{k}}} in the result", observed=observed[:200])


def answer_text(values):
    lines = []
    for name, v in values.items():
        if v is None:
            continue
        if isinstance(v, list) and v and isinstance(v[0], list):
            lines.append(f"{name}: {len(v)} rows")
            lines += ["  " + " | ".join(r) for r in v[:10]]
        elif isinstance(v, list):
            lines.append(f"{name}: {', '.join(map(str, v[:20]))}" + (" ..." if len(v) > 20 else ""))
        else:
            lines.append(f"{name}: {v}")
    return "\n".join(lines)
