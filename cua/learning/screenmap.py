"""
screenmap.py - the agent's map of the application, learned while it works.

Nodes are the kinds of window the app shows ("BankAPP - Core Banking System", "New Account",
"Account Details - *" ...). They form a tree: which window opened which (the Windows owner
relation). Edges record which action leads from one window to another - learned from the
agent's own steps, from what a person did during a hand-over, and from `python -m cua --explore`.
With the map the agent can tell where it is and find its way to where a saved step expects to be.

The map also learns from outcomes: every move keeps how often it led where it should and how often
it did not (an Esc that did not close the dialog, a dialog that did not open). Routes follow the most
reliable moves, and a move that keeps failing is no longer used (reliability.py, docs/LEARNING.md).

Only window titles (numbers masked), UI labels and generic actions are stored - never typed
values or customer data.

    m = ScreenMap("maps/bankapp.json")          # duplicates of one window are merged on load
    node, score = m.locate(skeleton)            # which known window is this? (None if new)
    node = m.learn_screen(skeleton, kind, parent)
    m.learn_edge(src, event, dst, by="agent")   # a move that led somewhere (a success for dst)
    m.learn_miss(src, event, expected)          # a move that did not get where it should
    route = m.path(src, dst, allowed)           # most reliable [(action, node it leads to), ...]
"""
import difflib
import heapq
import json
import math
import re
from datetime import datetime
from pathlib import Path

from cua import settings
from cua.learning.reliability import estimate
from cua.safety.pii import static_elements

TITLE_MATCH = 0.9                   # window titles at least this similar...
NODE_MATCH = 0.8                    # ...and this share of controls in common = same window
KEY_MATCH = 0.85                    # element labels this similar count as the same element
MAX_PATH = 4                        # longest route the agent will navigate on its own
ESC = {"type": "key", "keys": "esc"}   # built-in move: Esc closes a dialog / message box
CONTROLS = ("button", "input", "dropdown", "menu_item")


def _sim(a, b):
    return difflib.SequenceMatcher(None, str(a).lower(), str(b).lower()).ratio()


def mask_title(title):
    """Window title with the parts that change per record removed: 'Account Details - *'."""
    t = " ".join(str(title).split())
    t = re.sub(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", "*", t)
    t = re.sub(r"\b[A-Za-z]{2,4}\d{4,}\b", "*", t)              # record numbers like ACC100002
    return re.sub(r"\d[\d,.:/\- ]*", "*", t).strip()


def overlap(a, b):
    """Share of controls two skeletons have in common (same kind, similar label).
    Only controls count: which labels / column headers the parser sees can depend on the data
    shown (e.g. which of two tables is longer), the buttons and fields of a window do not."""
    def elements(s):
        els = [(e["kind"], e["key"]) for e in s.get("elements", [])]
        controls = [x for x in els if x[0] in CONTROLS]
        return controls or els
    A, B = elements(a), elements(b)
    if not A and not B:
        return 1.0
    return _common(A, B) / max(len(A), len(B))


def containment(a, b):
    """Share of the smaller window's controls found in the other (a window seen with a dialog on top
    has extra controls, not different ones)."""
    A = [(e["kind"], e["key"]) for e in a.get("elements", []) if e["kind"] in CONTROLS]
    B = [(e["kind"], e["key"]) for e in b.get("elements", []) if e["kind"] in CONTROLS]
    if not A or not B:
        return overlap(a, b)
    return _common(A, B) / min(len(A), len(B))


def _key_sim(a, b):
    """Label similarity tolerant of OCR's 0 / O confusion ("0K" is the OK button)."""
    return _sim(str(a).lower().replace("0", "o"), str(b).lower().replace("0", "o"))


def _common(A, B):
    pool, hits = list(B), 0
    for kind, key in A:
        best = max((x for x in pool if x[0] == kind), key=lambda x: _key_sim(x[1], key), default=None)
        if best and _key_sim(best[1], key) >= KEY_MATCH:
            pool.remove(best)
            hits += 1
    return hits


def generic(event):
    """Agent / person event -> the move stored in the map (no typed values, no row contents)."""
    t = event.get("type")
    if t == "key":
        return {"type": "key", "keys": str(event.get("keys", "")).lower()}
    if t in ("click", "double_click"):
        target = event.get("target") or {}
        if target.get("kind") == "table_row":
            return {"type": t, "kind": "table_row"}
        if target.get("kind") and target.get("key"):
            return {"type": t, "kind": target["kind"], "key": target["key"]}
    return None


def describe_action(a):
    return f"{a['type']} {a.get('key') or a.get('keys') or a.get('kind', '')}".strip()


class ScreenMap:
    def __init__(self, path):
        self.file = Path(path)
        data = json.loads(self.file.read_text(encoding="utf-8")) if self.file.exists() else {}
        self.nodes = data.get("nodes", {})
        self.edges = data.get("edges", [])
        self.merged = self.consolidate() if self.nodes else 0

    @property
    def root(self):
        """Home: the main-window node seen most often (one odd early screen can't take its place)."""
        mains = [nid for nid, n in self.nodes.items() if n["kind"] == "main"]
        return max(mains, key=lambda nid: self.nodes[nid]["seen"], default=None)

    def save(self):
        self.file.parent.mkdir(exist_ok=True)
        self.file.write_text(json.dumps({"root": self.root, "nodes": self.nodes, "edges": self.edges},
                                        indent=1, ensure_ascii=False), encoding="utf-8")

    def name(self, nid):
        return self.nodes[nid]["name"] if nid in self.nodes else "unknown window"

    # ------------------------------------------------ windows
    def locate(self, skeleton):
        """-> (node id, score) of the known window this screen is, or (None, best score)."""
        title = mask_title(skeleton["window_title"])
        skeleton = {**skeleton, "elements": static_elements(skeleton["elements"])}
        best, score = None, 0.0
        for nid, n in self.nodes.items():
            if _sim(n["name"], title) < TITLE_MATCH:
                continue
            s = overlap(n["skeleton"], skeleton)
            if s > score:
                best, score = nid, s
        return (best, score) if score >= NODE_MATCH else (None, score)

    def learn_screen(self, skeleton, kind="dialog", parent=None):
        """Known window -> its node; new window -> a new node (child of parent in the tree)."""
        nid, _ = self.locate(skeleton)
        if nid is None:
            nid = f"n{len(self.nodes) + 1}"
            while nid in self.nodes:
                nid += "_"
            self.nodes[nid] = {"name": mask_title(skeleton["window_title"]), "kind": kind, "parent": None,
                               "seen": 0, "first_seen": datetime.now().isoformat(timespec="seconds"),
                               "skeleton": {"window_title": mask_title(skeleton["window_title"]),
                                            "size": skeleton.get("size"),
                                            "elements": static_elements(skeleton["elements"])}}
        n = self.nodes[nid]
        n["seen"] += 1
        if kind == "main":
            n["kind"], n["parent"] = "main", None
        elif n.get("parent") is None and parent and parent != nid and n["kind"] != "main":
            n["parent"] = parent
        return nid

    # ------------------------------------------------ moves
    def _edge(self, src, action, create=True):
        e = next((e for e in self.edges if e["from"] == src and e["action"] == action), None)
        if e is None and create:
            e = {"from": src, "action": action, "to": {}, "miss": {}, "by": {}}
            self.edges.append(e)
        return e

    def learn_edge(self, src, event, dst, by="agent"):
        """A move was made and led to dst (a success for dst)."""
        action = generic(event)
        if not src or not dst or src == dst or action is None:
            return
        e = self._edge(src, action)
        e["to"][dst] = e["to"].get(dst, 0) + 1
        e["by"][by] = e["by"].get(by, 0) + 1

    def learn_miss(self, src, event, expected):
        """A move was made to reach `expected` and did not get there (e.g. Esc did not close it)."""
        action = generic(event)
        if not src or not expected or action is None:
            return
        miss = self._edge(src, action).setdefault("miss", {})
        miss[expected] = miss.get(expected, 0) + 1

    def reliability(self, e, dst):
        """(reliability, tries) of a move towards dst."""
        ok, fail = e["to"].get(dst, 0), e.get("miss", {}).get(dst, 0)
        return estimate(ok, fail)[0], ok + fail

    def moves(self, nid):
        """Known moves out of a window: [(action, node it leads to, reliability, tries)], best first.
        A dialog can always be tried with Esc (built in) until its own record says otherwise."""
        out = []
        for e in self.edges:
            if e["from"] == nid:
                dsts = {**e.get("miss", {}), **e["to"]}
                dst = max(dsts, key=lambda d: (e["to"].get(d, 0), -e.get("miss", {}).get(d, 0)))
                out.append((e["action"], dst, *self.reliability(e, dst)))
        n = self.nodes.get(nid)
        if n and n["kind"] != "main" and n.get("parent") and not any(a == ESC for a, *_ in out):
            out.append((ESC, n["parent"], estimate(0, 0)[0], 0))
        return sorted(out, key=lambda m: -m[2])

    @staticmethod
    def usable(rel, tries):
        return not (tries >= settings.SKIP_MOVE_AFTER and rel < settings.SKIP_MOVE_BELOW)

    def path(self, src, dst, allowed=lambda action: True):
        """Most reliable route src -> dst over moves allowed() accepts: [(action, next node)], None if
        unknown. Cost of a move = -log(reliability), so the route most likely to arrive wins; moves
        that keep failing are skipped. Row clicks are never used: which row depends on the task."""
        if src == dst:
            return []
        queue, best = [(0.0, 0, src, [])], {src: 0.0}
        while queue:
            cost, hops, nid, route = heapq.heappop(queue)
            if nid == dst:
                return route
            if hops >= MAX_PATH or cost > best.get(nid, math.inf):
                continue
            for action, to, rel, tries in self.moves(nid):
                if action.get("kind") == "table_row" or not self.usable(rel, tries) or not allowed(action):
                    continue
                c = cost - math.log(max(rel, 1e-3)) + 0.01
                if c < best.get(to, math.inf):
                    best[to] = c
                    heapq.heappush(queue, (c, hops + 1, to, route + [(action, to)]))
        return None

    # ------------------------------------------------ hygiene
    def consolidate(self):
        """Merge nodes that are the same window: same masked title and kind, controls in common.
        (Screens seen with a dialog on top, or before matching went by controls, left duplicates.)"""
        for n in self.nodes.values():                       # older maps may hold data read as controls
            n["skeleton"]["elements"] = static_elements(n["skeleton"].get("elements", []))
        merged = 0
        ids = sorted(self.nodes, key=lambda k: -self.nodes[k]["seen"])
        for i, keep in enumerate(ids):
            for other in ids[i + 1:]:
                if keep not in self.nodes or other not in self.nodes:
                    continue
                a, b = self.nodes[keep], self.nodes[other]
                if a["name"] == b["name"] and a["kind"] == b["kind"] and \
                        containment(a["skeleton"], b["skeleton"]) >= 0.9:
                    self._merge(keep, other)
                    merged += 1
        return merged

    def _merge(self, keep, gone):
        g = self.nodes.pop(gone)
        self.nodes[keep]["seen"] += g["seen"]
        for n in self.nodes.values():
            if n.get("parent") == gone:
                n["parent"] = keep
        if self.nodes[keep].get("parent") == keep:
            self.nodes[keep]["parent"] = None
        combined = {}
        for e in self.edges:
            src = keep if e["from"] == gone else e["from"]
            key = (src, json.dumps(e["action"], sort_keys=True))
            c = combined.setdefault(key, {"from": src, "action": e["action"], "to": {}, "miss": {}, "by": {}})
            for field in ("to", "miss"):
                for d, n in e.get(field, {}).items():
                    d = keep if d == gone else d
                    if d != src:
                        c[field][d] = c[field].get(d, 0) + n
            for who, n in e.get("by", {}).items():
                c["by"][who] = c["by"].get(who, 0) + n
        self.edges = [c for c in combined.values() if c["to"] or c["miss"]]

    # ------------------------------------------------ view
    def tree_lines(self):
        """The window tree as text, with how each window is reached."""
        children, via = {}, {}
        for nid, n in self.nodes.items():
            children.setdefault(n.get("parent"), []).append(nid)
        for e in self.edges:
            for dst in e["to"]:
                via.setdefault(dst, set()).add(describe_action(e["action"]))
        lines, done = [], set()

        def walk(nid, prefix, last, depth):
            if nid in done:
                return
            done.add(nid)
            n = self.nodes[nid]
            kind = {"messagebox": "message box"}.get(n["kind"], n["kind"])
            branch = "" if depth == 0 else ("`- " if last else "|- ")
            how = ", ".join(sorted(via.get(nid, [])))
            lines.append(f"{prefix}{branch}{n['name']} ({kind}, seen {n['seen']})" + (f"   via {how}" if how else ""))
            kids = sorted(children.get(nid, []), key=lambda k: self.nodes[k]["name"])
            for i, k in enumerate(kids):
                walk(k, prefix + ("" if depth == 0 else ("   " if last else "|  ")), i == len(kids) - 1, depth + 1)

        for top in ([self.root] if self.root else []) + sorted(children.get(None, [])):
            walk(top, "", True, 0)
        return lines

    def move_lines(self):
        """Every learned move with its record - what the agent has learned to trust."""
        lines = []
        for nid in self.nodes:
            for action, dst, rel, tries in self.moves(nid):
                if tries:
                    e = self._edge(nid, action, create=False)
                    ok, miss = e["to"].get(dst, 0), e.get("miss", {}).get(dst, 0)
                    flag = "" if self.usable(rel, tries) else "   (no longer used)"
                    lines.append(f"{self.name(nid)} --{describe_action(action)}--> {self.name(dst)}: "
                                 f"{ok} ok, {miss} failed, reliability {rel:.2f}{flag}")
        return lines
