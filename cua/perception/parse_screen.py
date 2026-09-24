"""
parse_screen.py - turn an OCR capture (from capture.py or tools/ocr_capture.py) into UI elements.

Usage:
    python parse_screen.py captures/20260924_111541.json

Reads  <stamp>.json + <stamp>_raw.png
Writes <stamp>_screen.json   -> compact, text-only view for the LLM (no pixels)
       <stamp>_elements.json -> id -> box / click point / role (for your executor)
       <stamp>_roles.png     -> colour-coded check image

Every element gets:
    kind        : title | menu_item | button | input | label | column_header |
                  table_row | table_cell | status | text | scrollbar
    interaction : click | type | select | none
    content     : static (fixed UI text) | dynamic (data that changes)

Install: pip install opencv-python pillow numpy
"""
import difflib
import json
import re
import sys
from collections import Counter
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw

# ------------------------------------------------------------------ helpers

def center(b):
    return ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)


def inside(pt, b, pad=0):
    return b[0] - pad <= pt[0] <= b[2] + pad and b[1] - pad <= pt[1] <= b[3] + pad


def union(boxes):
    return [min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes)]


QUOTED = re.compile(r"""\s*['"‘’“”`].*['"‘’“”`]\s*""")   # 'text' / "text"


def slug(text):
    s = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return s or "item"


def group_by_gap(words):
    """Split a row of words into phrases wherever the horizontal gap is large."""
    words = sorted(words, key=lambda w: w["box"][0])
    groups, cur = [], [words[0]]
    for w in words[1:]:
        h = w["box"][3] - w["box"][1]
        gap = w["box"][0] - cur[-1]["box"][2]
        if gap > max(8, 0.8 * h):
            groups.append(cur)
            cur = [w]
        else:
            cur.append(w)
    groups.append(cur)
    return [{"text": " ".join(w["text"] for w in g), "box": union([w["box"] for w in g]),
             "words": g} for g in groups]


def visual_rows(words, tol=5):
    """Cluster words into horizontal rows by vertical centre."""
    rows = []
    for w in sorted(words, key=lambda w: center(w["box"])[1]):
        cy = center(w["box"])[1]
        if rows and abs(cy - rows[-1]["cy"]) <= tol:
            rows[-1]["words"].append(w)
            ys = [center(x["box"])[1] for x in rows[-1]["words"]]
            rows[-1]["cy"] = sum(ys) / len(ys)
        else:
            rows.append({"cy": cy, "words": [w]})
    for r in rows:
        r["box"] = union([w["box"] for w in r["words"]])
    return rows


def background(gray, box):
    """Median grey of a strip around a box = its background colour."""
    x0, y0, x1, y1 = box
    strip = gray[max(0, y0 - 2):y1 + 3, max(0, x0):x1 + 1]
    return int(np.median(strip)) if strip.size else 255

# ------------------------------------------------------------ rectangles

def detect_rects(gray):
    """Find bordered rectangles (buttons, inputs, dropdowns, bands) with OpenCV."""
    P = 2                                           # pad so shapes touching the edge still close
    padded = cv2.copyMakeBorder(gray, P, P, P, P, cv2.BORDER_CONSTANT, value=0)
    edges = cv2.Canny(padded, 15, 45)               # low thresholds: 3D borders are faint
    thick = cv2.dilate(edges, np.ones((3, 3), np.uint8)) > 0
    contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    rects = []

    Hp, Wp = padded.shape

    def horiz_ok(yy, x0, x1):
        # continuous edge AND a plain, uniform line (text outlines are not uniform)
        if not 0 <= yy < Hp:
            return False
        m = (x1 - x0) // 10                           # ignore corners
        return thick[yy, x0:x1].mean() >= 0.85 and float(np.std(padded[yy, x0 + m:x1 - m])) < 30

    def vert_ok(xx, y0, y1):
        return 0 <= xx < Wp and thick[y0:y1, xx].mean() >= 0.85

    for c in contours:
        px, py, w, h = cv2.boundingRect(c)
        if not (12 <= h <= 45 and w >= 25 and w / h > 1.4):
            continue
        # each side must be found within 2px of the contour's bounding box (3D borders are soft)
        ok = (any(horiz_ok(py + d, px, px + w) for d in range(-2, 3)) and
              any(horiz_ok(py + h - 1 + d, px, px + w) for d in range(-2, 3)) and
              any(vert_ok(px + d, py, py + h) for d in range(-2, 3)) and
              any(vert_ok(px + w - 1 + d, py, py + h) for d in range(-2, 3)))
        if not ok:
            continue
        x, y = px - P, py - P
        if any(abs(x - r["x"]) < 4 and abs(y - r["y"]) < 4 and abs(w - r["w"]) < 6 for r in rects):
            continue
        inner = gray[y + 3:y + h - 3, x + 3:x + w - 3]
        rects.append({"x": x, "y": y, "w": w, "h": h, "box": [x, y, x + w, y + h],
                      "bg": int(np.median(inner)) if inner.size else 0})
    return rects

def has_dropdown_arrow(gray, r):
    """True if the right end of a box holds a small solid down-pointing triangle."""
    x, y, w, h = r["x"], r["y"], r["w"], r["h"]
    zone = gray[y + 2:y + h - 2, max(x, x + w - h - 8):x + w - 1]
    if zone.size == 0:
        return False
    dark = (zone < 100).astype(np.uint8)
    n, _, stats, _ = cv2.connectedComponentsWithStats(dark, 8)
    for i in range(1, n):
        cx, cy, cw, ch, area = stats[i]
        if not (5 <= cw <= 16 and 3 <= ch <= 10 and cw >= ch):
            continue
        comp = dark[cy:cy + ch, cx:cx + cw]
        widths = [int(row.sum()) for row in comp]
        solid = all(row.sum() == 0 or (np.ptp(np.flatnonzero(row)) + 1 == row.sum()) for row in comp)
        shrinking = all(a > b for a, b in zip(widths, widths[1:]) if b)
        if solid and shrinking and widths[0] >= 5 and widths[-1] <= 3:
            return True
    return False


# ------------------------------------------------------- column cleaning

DIGIT_FIX = str.maketrans({"I": "1", "l": "1", "|": "1", "i": "1", "O": "0", "o": "0",
                           "S": "5", "B": "8"})


def fix_id(v, plen):
    """Keep the letter prefix (length learned from the column), fix the digit part."""
    if len(v) <= plen:
        return v
    return v[:plen].upper() + v[plen:].translate(DIGIT_FIX)


def fix_money(v):
    return v.translate(DIGIT_FIX).replace(" ", "")


def fix_phone(v):
    d = re.sub(r"\D", "", v.translate(DIGIT_FIX))
    return f"{d[:3]}-{d[3:6]}-{d[6:]}" if len(d) == 10 else v


PATTERNS = {
    "id": r"^[A-Z]{2,4}\d{4,}$",
    "money": r"^-?[\d,]+\.\d{2}$",
    "phone": r"^\d{3}-\d{3}-\d{4}$",
    "email": r"^[\w.+-]+@[\w-]+\.[\w.]+$",
}


def infer_type(values):
    vals = [v for v in values if v]
    if not vals:
        return "text"
    def share(pred):
        return sum(1 for v in vals if pred(v)) / len(vals)
    if share(lambda v: re.match(r"^[A-Za-z]{2,4}[0-9IOlo|]{4,}$", v)) >= 0.7:
        return "id"
    if share(lambda v: re.match(PATTERNS["money"], fix_money(v))) >= 0.7:
        return "money"
    if share(lambda v: len(re.sub(r"\D", "", v.translate(DIGIT_FIX))) == 10 and "@" not in v) >= 0.7:
        return "phone"
    if share(lambda v: "@" in v) >= 0.7:
        return "email"
    if len(set(vals)) <= max(3, len(vals) // 6):
        return "enum"
    return "text"


def clean_table(columns, rows):
    """Type each column, fix common OCR confusions, flag cells still in doubt."""
    types = [infer_type([r["values"][i] for r in rows]) for i in range(len(columns))]

    for i, t in enumerate(types):
        common = [v for v, _ in Counter(r["values"][i] for r in rows).most_common(5)]
        plen = Counter(len(m.group(1)) for r in rows
                       if (m := re.match(r"^([A-Z]+)\d+$", r["values"][i]))).most_common(1)
        plen = plen[0][0] if plen else 3
        for r in rows:
            v = r["values"][i]
            if t == "id":
                v = fix_id(v, plen)
            elif t == "money":
                v = fix_money(v)
            elif t == "phone":
                v = fix_phone(v)
            elif t == "enum":
                m = difflib.get_close_matches(v, common, n=1, cutoff=0.7)
                v = m[0] if m else v
            r["values"][i] = v

    # Cross-column check: email local part should match "first.last" of a name column
    name_col = next((i for i, t in enumerate(types) if t == "text"
                     and sum(len(r["values"][i].split()) == 2 for r in rows) > len(rows) * 0.7), None)
    email_col = next((i for i, t in enumerate(types) if t == "email"), None)
    if name_col is not None and email_col is not None:
        for r in rows:
            expected = r["values"][name_col].lower().replace(" ", ".")
            email = r["values"][email_col]
            local, _, domain = email.partition("@")
            head, tail = local[:len(expected)], local[len(expected):]
            if domain and tail.translate(DIGIT_FIX).isdigit() or (domain and tail == ""):
                if 0.8 <= difflib.SequenceMatcher(None, head, expected).ratio() < 1:
                    r["values"][email_col] = f"{expected}{tail.translate(DIGIT_FIX)}@{domain}"

    for r in rows:
        r["uncertain"] = []
        for i, t in enumerate(types):
            v = r["values"][i]
            bad = (t in PATTERNS and v and not re.match(PATTERNS[t], v)) or \
                  any(ord(ch) > 127 for ch in v)
            if bad:
                r["uncertain"].append(columns[i])
    return types

# ------------------------------------------------------------------ main

def analyze(cap, img):
    """cap = capture dict, img = PIL image. Returns (view, elements, skeleton)."""
    img = img.convert("RGB")
    gray = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2GRAY)
    W, H = img.size
    wx, wy = cap["window_rect"][0], cap["window_rect"][1]

    words = [dict(w) for line in cap["lines"] for w in line["words"]
             if w["text"].strip("|[]_") != ""]           # borders misread as | [ ] _
    for w in words:
        w["used"] = False

    elements = []

    def add(kind, text, box, interaction, content, **extra):
        base = extra.pop("id", None) or f"{kind}_{slug(text)}"
        eid, n = base, 2
        while any(e["id"] == eid for e in elements):
            eid, n = f"{base}_{n}", n + 1
        cx, cy = center(box)
        e = {"id": eid, "kind": kind, "text": text, "box": box,
             "click_screen": [round(wx + cx), round(wy + cy)],
             "interaction": interaction, "content": content, **extra}
        elements.append(e)
        return e

    def take(pred):
        got = [w for w in words if not w["used"] and pred(w)]
        for w in got:
            w["used"] = True
        return got

    rects = detect_rects(gray)
    small = [r for r in rects if r["w"] < 0.8 * W]
    bands = [r for r in rects if r["w"] >= 0.8 * W]

    # 1. Title bar: dark band at the very top
    title_band = next((b for b in bands if b["y"] < 20 and b["bg"] < 120), None)
    if title_band:
        ws = take(lambda w: inside(center(w["box"]), title_band["box"]))
        if ws:
            g = group_by_gap(ws)
            add("title", " ".join(x["text"] for x in g), union([x["box"] for x in g]),
                "none", "static", id="title")

    # 2. Menu bar: white full-width band near the top containing words
    menu_band = next((b for b in bands if b["bg"] >= 245 and b["y"] < 0.2 * H
                      and any(inside(center(w["box"]), b["box"]) for w in words if not w["used"])), None)
    if menu_band:
        ws = take(lambda w: inside(center(w["box"]), menu_band["box"]))
        for g in group_by_gap(ws):
            add("menu_item", g["text"], g["box"], "click", "static", id=f"menu_{slug(g['text'])}")

    # 3. Buttons and inputs from small bordered rectangles
    inputs = []
    def ring_bg(r, pad=4):
        """Median colour just OUTSIDE a box = what the box sits on."""
        x0, y0, x1, y1 = r["box"]
        outer = gray[max(0, y0 - pad):min(H, y1 + pad), max(0, x0 - pad):min(W, x1 + pad)].astype(int)
        mask = np.ones(outer.shape, bool)
        mask[pad:-pad or None, pad:-pad or None] = False
        return int(np.median(outer[mask])) if mask.any() else r["bg"]

    def surface_bg(r):
        """Colour of the surface the box sits on: its enclosing band, else its surroundings."""
        band = next((b for b in bands if inside((r["x"], r["y"]), b["box"], 1)
                     and inside((r["x"] + r["w"], r["y"] + r["h"]), b["box"], 1)), None)
        return band["bg"] if band else ring_bg(r)

    for r in sorted(small, key=lambda r: (r["y"], r["x"])):
        ws = [w for w in words if not w["used"] and inside(center(w["box"]), r["box"])]
        text = " ".join(w["text"] for w in sorted(ws, key=lambda w: w["box"][0]))
        if has_dropdown_arrow(gray, r):           # box with a down-arrow = dropdown / combobox
            kind = "dropdown"
        elif r["bg"] >= 250:                     # white field = text input
            kind = "input"
        elif ws and abs(r["bg"] - surface_bg(r)) >= 8 and len(re.findall(r"\d", text)) < 4 \
                and not QUOTED.fullmatch(text):
            kind = "button"                       # face colour differs from what it sits on
                                                  # (a mostly-numeric "caption" is a focused table cell;
                                                  #  a quoted one echoes input: "matching '...'")
        else:
            continue                              # flat panel (table header cell, status pane)
        for w in ws:
            w["used"] = True
        if kind == "button":
            add("button", text, r["box"], "click", "static", id=f"btn_{slug(text)}")
        else:
            inputs.append(add(kind, text, r["box"], "select" if kind == "dropdown" else "type",
                              "dynamic", id=f"{kind}_{len(inputs) + 1}", value=text))

    # 4. Table: longest run of evenly spaced rows with several cells
    rows = visual_rows([w for w in words if not w["used"]])

    def header_like(row):                         # 3+ separate captions, no digits
        return len(group_by_gap(row["words"])) >= 3 and not re.search(r"\d", " ".join(w["text"] for w in row["words"]))

    best, best_key = (0, 0), (0, False)
    for i in range(len(rows) - 1):
        if len(rows[i]["words"]) < 2:
            continue
        dy0 = rows[i + 1]["cy"] - rows[i]["cy"]
        heights = sorted(w["box"][3] - w["box"][1] for w in rows[i]["words"])
        if dy0 > 4 * max(8, heights[len(heights) // 2]):
            continue                              # the next row is far away: no table starts here
        j = i + 1
        while j < len(rows) and len(rows[j]["words"]) >= 2:
            if abs((rows[j]["cy"] - rows[j - 1]["cy"]) - dy0) > 2.5:
                break
            j += 1
        key = (j - i, header_like(rows[i]))       # longest run; on a tie, the one under a header
        if key > best_key:
            best, best_key = (i, j), key
    table = None

    if best[1] - best[0] >= 3 or (best[1] - best[0] == 2 and header_like(rows[best[0]])):   # header + 1 row counts
        run = rows[best[0]:best[1]]
        body_bg = Counter(background(gray, r["box"]) for r in run).most_common(1)[0][0]
        first = run[0]
        header_row = None
        fbg = background(gray, first["box"])
        if len(run) == 2 or (fbg != body_bg and 120 < fbg < 250
                             and not re.search(r"\d", " ".join(w["text"] for w in first["words"]))):
            header_row, run = first, run[1:]

        if header_row:
            header_cells = group_by_gap(header_row["words"])
            for w in header_row["words"]:
                w["used"] = True
        else:                                     # no header: use first row positions
            header_cells = [{"text": f"col{k + 1}", "box": g["box"]}
                            for k, g in enumerate(group_by_gap(run[0]["words"]))]
        starts = [h["box"][0] - 6 for h in header_cells]
        columns = [h["text"] for h in header_cells]

        dy = (run[-1]["cy"] - run[0]["cy"]) / (len(run) - 1) if len(run) > 1 else \
            (run[0]["cy"] - header_row["cy"] if header_row else 20)   # one row: spacing from the header
        left = min(starts[0], min(r["box"][0] for r in run))
        right = max(r["box"][2] for r in run)
        # extend right edge to the table border (first non-background pixel)
        yprobe = int(run[min(1, len(run) - 1)]["cy"])
        x = right
        while x < W - 1 and abs(int(gray[yprobe, x]) - body_bg) < 10:
            x += 1
        right = x - 1

        if header_row:
            for h in header_cells:
                add("column_header", h["text"], h["box"], "click", "static",
                    id=f"col_{slug(h['text'])}")

        table_rows = []
        for n, r in enumerate(run, start=1):
            cells = [[] for _ in columns]
            for w in r["words"]:
                w["used"] = True
                cx = center(w["box"])[0]
                k = max([i for i, s in enumerate(starts) if s <= cx] or [0])
                cells[k].append(w)
            values = [" ".join(w["text"] for w in sorted(c, key=lambda w: w["box"][0])) for c in cells]
            rbox = [left, int(r["cy"] - dy / 2), right, int(r["cy"] + dy / 2)]
            selected = background(gray, [left + 2, rbox[1] + 2, right - 2, rbox[3] - 2]) < 100
            table_rows.append({"n": n, "box": rbox, "values": values, "cells": cells,
                               "selected": selected})

        types = clean_table(columns, table_rows)
        for tr in table_rows:
            add("table_row", tr["values"][0], tr["box"], "select", "dynamic",
                id=f"row_{tr['n']}", selected=tr["selected"], values=tr["values"])
            for k, c in enumerate(tr["cells"]):
                if c:
                    add("table_cell", tr["values"][k], union([w["box"] for w in c]), "none",
                        "dynamic", id=f"row_{tr['n']}_{slug(columns[k])}")

        table = {"columns": columns, "types": types, "rows": table_rows,
                 "box": [left, int(run[0]["cy"] - dy / 2), right, int(run[-1]["cy"] + dy / 2)]}

        # 5. Vertical scrollbar just right of the table
        x0 = right + 3
        sb_top = (header_row["box"][1] - 4) if header_row else table["box"][1]
        sb_bot = min(H - 1, table["box"][3] + int(dy))
        if x0 + 10 < W:
            prof = np.median(gray[sb_top:sb_bot, x0:x0 + 10], axis=1).astype(int)
            levels = [v for v, c in Counter(prof).most_common(2) if c > 0.1 * len(prof)]
            if len(levels) == 2 and abs(levels[0] - levels[1]) > 15:
                thumb, track = max(levels), min(levels)   # thumb is usually lighter
                # longest contiguous run of the thumb colour = the thumb itself
                runs, start = [], None
                for k, v in enumerate(list(prof) + [-1]):
                    if v == thumb and start is None:
                        start = k
                    elif v != thumb and start is not None:
                        runs.append((start, k))
                        start = None
                t0, t1 = max(runs, key=lambda r: r[1] - r[0])
                track_px = np.where(prof == track)[0]
                above = int((track_px < t0).sum())
                below = int((track_px >= t1).sum())
                frac = (t1 - t0) / max(1, (t1 - t0) + above + below)
                table["scroll"] = {"visible_fraction": round(float(frac), 2),
                                   "at_top": above == 0, "at_bottom": below == 0}
                add("scrollbar", "vertical scrollbar", [x0 - 2, sb_top, x0 + 14, sb_bot],
                    "click", "dynamic", id="scrollbar_v", **table["scroll"])

    # 6. Remaining text: labels, status bar, other
    table_bottom = table["box"][3] if table else 0
    for r in visual_rows([w for w in words if not w["used"]]):
        for g in group_by_gap(r["words"]):
            text, box = g["text"], g["box"]
            # label directly left of an input?
            cands = [i for i in inputs if abs(center(i["box"])[1] - center(box)[1]) < 8
                     and 0 <= i["box"][0] - box[2] < 200 and "label" not in i]
            inp = min(cands, key=lambda i: i["box"][0] - box[2]) if cands else None
            if inp:
                add("label", text, box, "none", "static", id=f"lbl_{slug(text)}", for_input=inp["id"])
                inp["label"] = text.rstrip(":").strip()
                continue
            kind = "status" if box[1] > table_bottom and box[1] > 0.85 * H else "text"
            if ":" in text and not text.endswith(":") and not re.search(r"\d:\d", text):  # "User: admin"
                # -> label + value (but a time like 13:35:00 is not a label)
                lab, val = text.split(":", 1)
                add(kind, lab.strip() + ":", box, "none", "static")
                add(kind, val.strip(), box, "none", "dynamic", label=lab.strip())
            else:
                dynamic = kind == "status" or bool(re.search(r"\d|@", text))
                add(kind, text, box, "none", "dynamic" if dynamic else "static")

    # 7. Name inputs; mark search boxes (label or neighbouring button says find/search)
    for i in inputs:
        btn = next((e for e in elements if e["kind"] == "button"
                    and abs(center(e["box"])[1] - center(i["box"])[1]) < 8
                    and 0 <= e["box"][0] - i["box"][2] < 40), None)
        hint = f"{i.get('label', '')} {btn['text'] if btn else ''}".lower()
        i["role"] = "search" if re.search(r"search|find|lookup|filter|\bgo\b", hint) else "field"
        if btn:
            i["submit_button"] = btn["id"]

    # 8. Semantic key per element (what replay uses instead of ids/coordinates)
    for e in elements:
        if e["kind"] in ("input", "dropdown"):
            e["key"] = e.get("label") or e["id"]
        elif e["kind"] == "table_row":
            e["key"] = e["values"][0] if e["values"] else e["id"]
        else:
            e["key"] = e["text"]

    # ---------------------------------------------------- outputs
    view = {"screen": next((e["text"] for e in elements if e["kind"] == "title"), cap.get("window_title")),
            "menu": [{"id": e["id"], "text": e["text"]} for e in elements if e["kind"] == "menu_item"],
            "buttons": [{"id": e["id"], "text": e["text"]} for e in elements if e["kind"] == "button"],
            "inputs": [{"id": i["id"], "kind": i["kind"], "label": i.get("label", ""), "role": i["role"],
                        "value": i["value"], **({"submit": i["submit_button"]} if "submit_button" in i else {})}
                       for i in inputs]}
    if table:
        view["table"] = {
            "columns": table["columns"],
            "sortable_headers": [e["id"] for e in elements if e["kind"] == "column_header"],
            "visible_rows": len(table["rows"]),
            "selected_row": next((f"row_{r['n']}" for r in table["rows"] if r["selected"]), None),
            **({"scroll": table["scroll"]} if "scroll" in table else {}),
            "rows": [{"id": f"row_{r['n']}", "values": r["values"],
                      **({"check": r["uncertain"]} if r["uncertain"] else {})} for r in table["rows"]],
        }
    view["other_text"] = [{"text": e["text"], "content": e["content"], "kind": e["kind"]}
                          for e in elements if e["kind"] in ("status", "text")]

    # Skeleton = the fixed UI only (no data). Used to recognise screens and detect drift.
    skeleton = {"window_title": cap.get("window_title", ""), "size": [W, H],
                "elements": [{"kind": e["kind"], "key": e["key"], "box": e["box"]}
                             for e in elements if e["kind"] in SKELETON_KINDS]}
    return view, elements, skeleton


SKELETON_KINDS = ("menu_item", "button", "input", "dropdown", "label", "column_header")


def draw_roles(img, elements, path):
    img = img.convert("RGB")
    W, H = img.size
    colors = {"button": (0, 170, 0), "menu_item": (0, 170, 0), "column_header": (0, 170, 0),
              "input": (0, 90, 255), "dropdown": (0, 90, 255), "label": (130, 130, 130),
              "title": (130, 130, 130), "scrollbar": (200, 0, 200)}
    out = img.copy()
    d = ImageDraw.Draw(out)
    for e in elements:
        if e["kind"] == "table_row":
            if e["selected"]:
                d.rectangle(e["box"], outline=(200, 0, 200), width=2)
            continue
        col = colors.get(e["kind"], (255, 140, 0) if e["content"] == "dynamic" else (130, 130, 130))
        d.rectangle(e["box"], outline=col, width=2 if e["interaction"] != "none" else 1)
    legend = [("clickable", (0, 170, 0)), ("input/dropdown", (0, 90, 255)), ("static", (130, 130, 130)),
              ("dynamic", (255, 140, 0)), ("selected row / scrollbar", (200, 0, 200))]
    canvas = Image.new("RGB", (W, H + 26), (255, 255, 255))   # legend strip below the image
    canvas.paste(out, (0, 0))
    d, lx = ImageDraw.Draw(canvas), 10
    for name, col in legend:
        d.rectangle([lx, H + 8, lx + 10, H + 18], fill=col)
        d.text((lx + 16, H + 7), name, fill=(0, 0, 0))
        lx += 30 + 7 * len(name)
    canvas.save(path)


def parse(json_path):
    """File mode: <stamp>.json + <stamp>_raw.png -> _screen.json, _elements.json, _roles.png"""
    json_path = Path(json_path)
    cap = json.loads(json_path.read_text(encoding="utf-8"))
    img = Image.open(json_path.with_name(json_path.stem + "_raw.png"))
    view, elements, skeleton = analyze(cap, img)
    stem = json_path.with_suffix("")
    Path(f"{stem}_screen.json").write_text(json.dumps(view, indent=1, ensure_ascii=False), encoding="utf-8")
    Path(f"{stem}_elements.json").write_text(json.dumps(
        {"window_rect": cap["window_rect"], "skeleton": skeleton, "elements": elements},
        indent=1, ensure_ascii=False), encoding="utf-8")
    draw_roles(img, elements, f"{stem}_roles.png")
    counts = Counter(e["kind"] for e in elements)
    print(f"{json_path.name}: " + ", ".join(f"{v} {k}" for k, v in counts.items()))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("usage: python parse_screen.py captures/<stamp>.json")
    for p in sys.argv[1:]:
        parse(p)