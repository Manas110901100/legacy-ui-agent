"""A scripted stand-in for BankAPP behind the Surface seam, so replay runs without the real app,
the mouse, OCR or GPT-4o. Screens are built in the same shape parse_screen.py produces."""
import re

from cua.perception.surface import Screen

MAIN = "BankAPP - Core Banking System"
ACCOUNTS = [
    {"no": "ACC100002", "name": "Sarah Miller", "type": "Checking", "balance": "12,613.28", "status": "ACTIVE",
     "email": "sarah.miller92@example.com", "phone": "555-765-9928",
     "txns": [("2025-09-11", "DEPOSIT", "4,022.90", "12,613.28", "Cash deposit"),
              ("2025-09-02 13:35:00", "WITHDRAWAL", "3,577.78", "8,590.38", "ATM withdrawal")]},
    {"no": "ACC100024", "name": "Sarah Jones", "type": "Savings", "balance": "27,567.72", "status": "ACTIVE",
     "email": "sarah.jones@example.com", "phone": "555-111-2233",
     "txns": [("2026-02-22", "DEPOSIT", "4,181.45", "27,567.72", "Cash deposit")]},
    {"no": "ACC100030", "name": "Sarah Khan", "type": "Checking", "balance": "11,141.91", "status": "ACTIVE",
     "email": "sarah.khan@example.com", "phone": "555-444-9876", "txns": []},
    {"no": "ACC100007", "name": "Ahmed Chen", "type": "Savings", "balance": "902.10", "status": "ACTIVE",
     "email": "ahmed.chen@example.com", "phone": "555-222-0101", "txns": []},
]
COLUMNS = ["Account No", "Customer Name", "Type", "Balance", "Status", "Email", "Phone"]


def slug(t):
    return re.sub(r"[^a-z0-9]+", "_", t.lower()).strip("_")


def build_screen(title, buttons=(), inputs=(), table=None, pairs=(), texts=(), rows_under=None, handle=None):
    """-> Screen. inputs: [(label, value)]; table: (columns, [values...]); pairs: [(label, value)] shown
    as 'Label:' + value on one row; texts: free text lines; rows_under: (header cells, [row cells])."""
    elements, y = [], 10

    def add(e):
        cx, cy = (e["box"][0] + e["box"][2]) / 2, (e["box"][1] + e["box"][3]) / 2
        e.setdefault("click_screen", [round(cx), round(cy)])
        elements.append(e)
        return e

    for b in buttons:
        add({"id": f"btn_{slug(b)}", "kind": "button", "text": b, "key": b, "box": [10, y, 90, y + 20],
             "interaction": "click", "content": "static"})
        y += 25
    view_inputs = []
    for n, (label, value) in enumerate(inputs, 1):
        add({"id": f"lbl_{slug(label)}", "kind": "label", "text": label + ":", "key": label + ":",
             "box": [10, y, 120, y + 20], "interaction": "none", "content": "static"})
        add({"id": f"input_{n}", "kind": "input", "text": value, "value": value, "label": label, "key": label,
             "role": "search" if "Name" in label else "field", "box": [130, y, 330, y + 20],
             "interaction": "type", "content": "dynamic"})
        view_inputs.append({"id": f"input_{n}", "kind": "input", "label": label, "role": "field", "value": value})
        y += 25
    for label, value in pairs:
        add({"id": f"t_{slug(label)}", "kind": "text", "text": label + ":", "key": label + ":",
             "box": [10, y, 110, y + 18], "interaction": "none", "content": "static"})
        add({"id": f"t_{slug(label)}_v", "kind": "text", "text": value, "key": value, "box": [120, y, 320, y + 18],
             "interaction": "none", "content": "dynamic"})
        y += 22
    view_table = None
    if table:
        cols, rows = table
        view_table = {"columns": cols, "rows": []}
        for n, values in enumerate(rows, 1):
            add({"id": f"row_{n}", "kind": "table_row", "text": values[0], "key": values[0], "values": list(values),
                 "box": [10, y, 700, y + 18], "interaction": "select", "content": "dynamic", "selected": n == 1})
            view_table["rows"].append({"id": f"row_{n}", "values": list(values)})
            y += 20
    if rows_under:
        head, rows = rows_under
        for k, h in enumerate(head):
            add({"id": f"h_{slug(h)}", "kind": "text", "text": h, "key": h, "box": [10 + 120 * k, y, 120 + 120 * k, y + 18],
                 "interaction": "none", "content": "static"})
        y += 22
        for cells in rows:
            for k, c in enumerate(cells):
                add({"id": f"c_{y}_{k}", "kind": "text", "text": c, "key": c, "box": [10 + 120 * k, y, 120 + 120 * k, y + 18],
                     "interaction": "none", "content": "dynamic"})
            y += 20
    for t in texts:
        add({"id": f"txt_{slug(t)[:20]}", "kind": "text", "text": t, "key": t, "box": [10, y, 400, y + 18],
             "interaction": "none", "content": "dynamic" if re.search(r"\d|@", t) else "static"})
        y += 22
    skeleton = {"window_title": title, "size": [800, 600],
                "elements": [{"kind": e["kind"], "key": e["key"], "box": e["box"]} for e in elements
                             if e["kind"] in ("menu_item", "button", "input", "dropdown", "label", "column_header")]}
    view = {"screen": title, "menu": [], "buttons": [{"id": e["id"], "text": e["text"]} for e in elements
                                                      if e["kind"] == "button"],
            "inputs": view_inputs,
            "other_text": [{"text": e["text"], "content": e["content"], "kind": e["kind"]} for e in elements
                           if e["kind"] == "text"]}
    if view_table:
        view["table"] = view_table
    return Screen(title, view, elements, skeleton, handle or title, {"window_rect": [0, 0, 800, 600]})


class FakeBank:
    """Behaves like BankAPP for the account-details flow: search, pick a row, details window,
    'Record Not Found', injected 'Temporarily Unavailable' and 'Session Expired' dialogs."""
    main, pid = "main", 4242

    def __init__(self, transient_finds=0, session_expired=False, esc_closes=True):
        self.esc_closes = esc_closes           # False: Esc does nothing (like BankAPP's details window)
        self.search, self.shown = "", list(ACCOUNTS)
        self.stack = ["main"]                  # open windows, topmost first
        self.dialogs = {}                      # handle -> Screen of that dialog
        self.transient_finds, self.session_expired = transient_finds, session_expired
        self.actions = []

    # -- windows
    def foreground(self): return self.stack[0]
    def owns(self, h): return h in self.stack
    def windows(self): return list(self.stack)
    def title(self, h): return MAIN if h == "main" else self.dialogs[h].title if h in self.dialogs else ""
    def kind(self, h): return "main" if h == "main" else "dialog"
    def owner(self, h): return None if h == "main" else "main"
    def covering(self, h): return []
    def front(self, h, maximize=False):
        if h in self.stack:
            self.stack.remove(h)
            self.stack.insert(0, h)
    def close(self, h):
        if h in self.stack and h != "main":
            self.stack.remove(h)
    def wait(self, seconds): pass
    def refocus(self, screen): pass

    def _open(self, screen):
        self.dialogs[screen.hwnd] = screen
        self.stack.insert(0, screen.hwnd)

    # -- see
    def capture(self):
        h = self.stack[0]
        if h != "main":
            return self.dialogs[h]
        rows = [[a["no"], a["name"], a["type"], a["balance"], a["status"], a["email"], a["phone"]] for a in self.shown]
        return build_screen(MAIN, buttons=["New", "Edit", "Details", "Deposit", "Withdraw", "Transfer", "Close Acct",
                                           "Find", "Show All"],
                            inputs=[("Account No / Name", self.search)], table=(COLUMNS, rows),
                            texts=[f"{len(rows)} account(s)"], handle="main")

    # -- act
    def act(self, event, el, screen, text=None):
        self.actions.append((screen.title, event["type"], (el or {}).get("key"), text))
        h, t = screen.hwnd, event["type"]
        if h != "main":                                            # dialogs: Enter / Esc / OK / Close
            esc_ignored = t == "key" and event.get("keys") == "esc" and not self.esc_closes
            if (t == "key" and not esc_ignored) or (el and el["key"] in ("OK", "Close")):
                self.close(h)
            return
        if t == "type":
            self.search = text
        elif t == "click" and el["key"] == "Show All":
            self.search, self.shown = "", list(ACCOUNTS)
        elif t == "click" and el["key"] == "Find":
            if self.session_expired:
                self._open(build_screen("Session Expired", buttons=["OK"], texts=["Your session has expired."],
                                        handle="expired"))
                return
            if self.transient_finds > 0:
                self.transient_finds -= 1
                self._open(build_screen("Application Error", buttons=["OK"], handle="transient", texts=[
                    "Service unavailable after 3 attempts (The server did not respond in time.). Please try again later.",
                    "Error ID: 7f3a"]))                    # how BankAPP reports it after its own 3 retries
                return
            q = self.search.lower()
            hits = [a for a in ACCOUNTS if q in a["no"].lower() or q in a["name"].lower()]
            if not hits:
                self._open(build_screen("Record Not Found", buttons=["OK"], texts=[f"No accounts match '{self.search}'."],
                                        handle="notfound"))
            else:
                self.shown = hits
        elif t == "double_click" and el["kind"] == "table_row":
            a = next(x for x in ACCOUNTS if x["no"] == el["values"][0])
            self._open(build_screen(f"Account Details - {a['no']}", buttons=["Counter", "Close"],
                                    pairs=[("Account No", a["no"]), ("Customer", a["name"]), ("Type", a["type"]),
                                           ("Status", a["status"]), ("Balance", a["balance"])],
                                    rows_under=(["Date/Time", "Type", "Amount", "Balance After", "Description"],
                                                [list(x) for x in a["txns"]]),
                                    texts=[f"Transactions ({len(a['txns'])})"], handle="details"))
