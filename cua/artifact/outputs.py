"""
outputs.py - read a capability's outputs from the final screen, deterministically (no LLM).

An output's extraction rule says where its value sits on the screen, the way a person would say it:

    {"label": "Balance"}                          the text right of "Balance:" on the same row
    {"pattern": "Account {value} opened for *"}   a text line; {value} is the output, * anything
    {"column": "Account No"}                      every row of the table's column (a list)
    {"rows_below": "Date/Time"}                   the rows under that header (a table)

Rules only use static UI text (labels, headers, fixed wording), never customer data, so the same
rule works for every record - and it works whichever way the parser grouped the texts (table,
label + value, loose text), because it goes by position on the screen.
"""
import difflib
import re
from decimal import Decimal, InvalidOperation

from cua.perception.parse_screen import DIGIT_FIX, center, fix_id

TEXT_KINDS = ("text", "label", "status", "table_cell", "title", "column_header")
ROW_TOL = 8                          # px: texts this close vertically are on the same row


def _norm(s):
    return " ".join(str(s).lower().replace(":", " ").split())


def _sim(a, b):
    return difflib.SequenceMatcher(None, _norm(a), _norm(b)).ratio()


def _texts(elements):
    return [e for e in elements if e["kind"] in TEXT_KINDS and str(e.get("text", "")).strip()]


def fill(text, params):
    return re.sub(r"\{(\w+)\}", lambda m: str(params.get(m.group(1), m.group(0))), str(text))


# ---------------------------------------------------------------- finding values

def label_value(elements, label):
    """Value shown right of a label on the same row ('Balance:' -> '12,613.28')."""
    texts = [e for e in _texts(elements) if e["kind"] != "column_header"]   # a label is not a table header
    for e in texts:                                    # parse_screen split "User: admin" -> label field
        if e.get("label") and _sim(e["label"], label) >= 0.85 and e.get("content") == "dynamic":
            return e["text"]
    exact = [e for e in texts if _sim(e["text"], label) >= 0.85]
    labels = exact or [e for e in texts if _norm(e["text"]).startswith(_norm(label) + " ")
                       and ":" in str(e["text"])]     # "Balance: 12,613.28" in one text
    for lab in labels:
        rest = str(lab["text"]).split(":", 1)
        if len(rest) == 2 and rest[1].strip():         # "Balance: 12,613.28" in one text
            return rest[1].strip()
        cy = center(lab["box"])[1]
        right = [e for e in texts if e is not lab and abs(center(e["box"])[1] - cy) <= ROW_TOL
                 and e["box"][0] >= lab["box"][2] - 2]
        if right:
            return min(right, key=lambda e: e["box"][0])["text"]
    return None


def compile_pattern(pattern, params):
    """'Account {value} opened for {name}.' -> regex with a 'value' group (case / spacing tolerant)."""
    parts = []
    for piece in re.split(r"(\{value\}|\*)", fill(pattern, params)):
        if piece == "{value}":
            parts.append(r"(?P<value>.+?)")
        elif piece == "*":
            parts.append(r".*?")
        elif piece:
            parts.append(r"\s+".join(re.escape(w) for w in piece.split()) + (r"\s*" if piece.endswith(" ") else ""))
    return re.compile(r"^\s*" + "".join(parts) + r"\s*$", re.I)


def by_pattern(elements, pattern, params):
    rx = compile_pattern(pattern, params)
    for e in _texts(elements):
        m = rx.match(str(e["text"]))
        if m:
            return m.group("value").strip()
    return None


def column_values(view, column):
    table = view.get("table") or {}
    cols = table.get("columns", [])
    best = max(cols, key=lambda c: _sim(c, column), default=None)
    if best is None or _sim(best, column) < 0.85:
        return None
    k = cols.index(best)
    return [r["values"][k] for r in table.get("rows", [])]


def rows_below(elements, header):
    """Rows of text under a header (the first cell of a table's header row), grouped by position."""
    texts = _texts(elements)
    heads = [e for e in texts if _sim(e["text"], header) >= 0.8 or _norm(e["text"]).startswith(_norm(header))]
    if not heads:
        return None
    head = min(heads, key=lambda e: e["box"][1])
    y0, x0 = head["box"][3], head["box"][0] - 10
    hy = center(head["box"])[1]
    width = sum(1 for e in texts if abs(center(e["box"])[1] - hy) <= ROW_TOL)   # cells in the header row
    below = sorted((e for e in texts if e["box"][1] >= y0 - 2 and e["box"][0] >= x0 and e["kind"] != "column_header"
                    and e is not head), key=lambda e: (center(e["box"])[1], e["box"][0]))
    rows, seen = [], set()
    for e in below:
        key = (tuple(e["box"]), e["text"])
        if key in seen:                                   # a table_cell and a text for the same box
            continue
        seen.add(key)
        cy = center(e["box"])[1]
        if rows and abs(rows[-1]["cy"] - cy) <= ROW_TOL:
            rows[-1]["cells"].append(e)
        else:
            rows.append({"cy": cy, "cells": [e]})
    table = []
    for r in rows[:50]:                                   # the table ends at the first line that is not a row
        if len(r["cells"]) < max(2, width // 2):
            break
        table.append([c["text"] for c in sorted(r["cells"], key=lambda c: c["box"][0])])
    return table


# ---------------------------------------------------------------- typing the values

def coerce(value, out_type, pattern=None):
    """Raw OCR text -> typed value. Returns (value, warning or None)."""
    if value is None:
        return None, "not found on screen"
    if out_type in ("list", "table"):
        return value, None
    v = str(value).strip()
    if out_type == "money":
        raw = v.translate(DIGIT_FIX).replace(",", "").replace("$", "").replace(" ", "")
        try:
            return f"{Decimal(raw):.2f}", None
        except InvalidOperation:
            return v, f"'{v}' is not an amount"
    if out_type == "number":
        digits = re.sub(r"\D", "", v.translate(DIGIT_FIX))
        return (int(digits), None) if digits else (v, f"'{v}' is not a number")
    if out_type == "account_no":
        v = fix_id(v.replace(" ", ""), 3)
    if pattern and not re.fullmatch(pattern, v):
        return v, f"'{v}' does not look like {pattern}"
    return v, None


def extract(spec, view, elements, params):
    """One output's rule -> raw value (None if the rule finds nothing)."""
    if spec.get("label"):
        return label_value(elements, spec["label"])
    if spec.get("pattern"):
        return by_pattern(elements, spec["pattern"], params)
    if spec.get("column"):
        return column_values(view, spec["column"])
    if spec.get("rows_below"):
        return rows_below(elements, spec["rows_below"])
    return None


def extract_all(outputs, view, elements, params):
    """{name: OutputSpec-like dict} -> ({name: typed value}, [warnings])."""
    values, warnings = {}, []
    for name, out in outputs.items():
        spec = out.get("extract") or {}
        if not spec:
            warnings.append(f"{name}: no extraction rule")
            continue
        value, warn = coerce(extract(spec, view, elements, params), out["type"], out.get("pattern"))
        values[name] = value
        if warn:
            warnings.append(f"{name}: {warn}")
    return values, warnings
