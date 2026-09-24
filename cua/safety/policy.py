"""
policy.py - everything the agent is allowed to do in BankAPP.

Every request is mapped to one of these intents before anything touches the UI.
Reads are allowed; the only write is create_account. Each intent lists the values (slots)
it needs and the exact buttons / fields / screens it may use. Anything else is refused,
whether GPT-4o proposes it, a human demonstrates it or a saved workflow contains it.

The check functions return None when an action is fine, else the reason it is not.
"""
import json
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Callable

from cua import settings

# ---------------------------------------------------------------- validators
# Same rules as BankAPP itself (bankapp/services.py), so bad values are caught before the UI.

ACCOUNT_TYPES = ("Savings", "Checking")


def v_name(s):
    v = " ".join(str(s).split())
    if not re.fullmatch(r"[A-Za-z][A-Za-z .'\-]{1,59}", v):
        raise ValueError("Name must be 2-60 letters (spaces, . ' - allowed).")
    return v


def v_email(s):
    v = str(s).strip().lower()
    if len(v) > 100 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}", v):
        raise ValueError(f"'{str(s).strip()}' is not a valid email address.")
    return v


def v_phone(s):
    v = str(s).strip()
    if not re.fullmatch(r"\+?[0-9][0-9 \-]{6,18}[0-9]", v):
        raise ValueError("Phone must be 8-20 digits (spaces, dashes, leading + ok).")
    return v


def v_account_type(s):
    for t in ACCOUNT_TYPES:
        if str(s).strip().lower() == t.lower():
            return t
    raise ValueError(f"Account type must be one of: {', '.join(ACCOUNT_TYPES)}.")


def v_amount(s):
    raw = str(s).strip().replace(",", "").lstrip("$")
    try:
        a = Decimal(raw)
    except InvalidOperation:
        raise ValueError(f"'{str(s).strip()}' is not a valid amount.") from None
    if not a.is_finite() or a <= 0:
        raise ValueError("Amount must be greater than zero.")
    if a.as_tuple().exponent < -2:
        raise ValueError("Amount can have at most 2 decimal places.")
    if a > Decimal("1000000"):
        raise ValueError("Amount exceeds the single-transaction maximum of 1,000,000.00.")
    return f"{a:.2f}"


def v_account(s):
    v = " ".join(str(s).split())
    if re.fullmatch(r"(?i)acc\d{6}", v):
        return v.upper()
    if re.fullmatch(r"[A-Za-z][A-Za-z .'\-]{1,59}", v):
        return v
    raise ValueError("Give an account number (like ACC100001) or a customer name.")


# ---------------------------------------------------------------- registry
# The registry itself is configuration: apps/<app>.json (inputs, outputs, allow-list, deny list,
# known dialogs). Only the validators live here, referenced from the profile by name.

VALIDATORS = {"name": v_name, "email": v_email, "phone": v_phone, "account_type": v_account_type,
              "amount": v_amount, "account": v_account}


@dataclass
class Slot:
    """One typed input of a capability."""
    name: str
    question: str
    validate: Callable
    kind: str | None = "TEXT"      # token kind for pii.Vault; None = not personal data
    type: str = "string"
    description: str = ""
    enum: list | None = None
    record_key: bool = False       # identifies the record: the result must show this value


@dataclass
class Output:
    """One typed output a capability returns to its caller."""
    name: str
    type: str                      # string | money | number | account_no | list | table
    description: str = ""
    pattern: str | None = None
    pii: str | None = None
    extract: dict | None = None    # where the vendor product shows it (used if discovery doesn't point)


@dataclass
class Intent:
    name: str
    mode: str                      # "read" | "write"
    about: str
    slots: list
    outputs: dict = field(default_factory=dict)
    clicks: tuple = ()             # button captions that may be clicked
    types: tuple = ()              # input labels that may be typed into
    selects: tuple = ()            # dropdown labels that may be changed
    rows: bool = False             # click / double-click table rows
    headers: bool = False          # click column headers and the scrollbar
    keys: tuple = ("enter", "tab", "esc")
    screens: tuple = ()            # window-title prefixes this intent may pass through
    deny: re.Pattern = None        # buttons that are always refused (from the profile)
    common: tuple = ("please wait",)


@dataclass
class Profile:
    """Everything app-specific: target window, allow-list, known dialogs. apps/<app>.json"""
    app: str
    product: str
    vendor_version: str
    surface: str
    window_title: str
    launch: str
    timeout: float
    busy: tuple
    close_buttons: tuple
    deny: re.Pattern
    dialogs: list                  # [(title, outcome code, message regex or None)] - first match wins
    intents: dict
    reset: list = field(default_factory=list)   # safe steps that bring the main window to its start state


def deny_regex(words):
    """['close acc', 'edit'] -> one pattern matching 'Close Acct', 'Edit...' as whole words."""
    alternatives = [r"\s*".join(re.escape(part) for part in w.split()) + r"\w*" for w in words]
    return re.compile(r"\b(" + "|".join(alternatives) + r")", re.I)


def load_profile(app="bankapp"):
    data = json.loads((settings.APPS / f"{app}.json").read_text(encoding="utf-8"))
    deny = deny_regex(data["deny"])
    busy = tuple(b.lower() for b in data.get("busy_windows", []))
    intents = {}
    for name, c in data["capabilities"].items():
        allow = c.get("allow", {})
        slots = [Slot(k, v["question"], VALIDATORS[v["validate"]], v.get("pii"), v.get("type", "string"),
                      v.get("description", ""), v.get("enum"), v.get("record_key", False))
                 for k, v in c.get("inputs", {}).items()]
        outputs = {k: Output(k, v["type"], v.get("description", ""), v.get("pattern"), v.get("pii"),
                             v.get("extract")) for k, v in c.get("outputs", {}).items()}
        intents[name] = Intent(name, c["mode"], c["description"], slots, outputs,
                               clicks=tuple(allow.get("clicks", ())), types=tuple(allow.get("types", ())),
                               selects=tuple(allow.get("selects", ())), rows=allow.get("rows", False),
                               headers=allow.get("headers", False),
                               keys=tuple(allow.get("keys", ("enter", "tab", "esc"))),
                               screens=tuple(w.lower() for w in allow.get("windows", ())), deny=deny, common=busy)
    return Profile(data["app"], data["product"], data.get("vendor_version", ""), data.get("surface", "desktop-ocr"),
                   data["window_title"], data.get("launch", ""), float(data.get("run_timeout_seconds", 300)),
                   busy, tuple(data.get("close_buttons", ())), deny,
                   [(d["title"], d["outcome"], d.get("text")) for d in data.get("dialogs", [])], intents,
                   data.get("reset", []))


PROFILE = load_profile()           # the default app; the CLI can load another with --app
INTENTS = PROFILE.intents
DENY = PROFILE.deny


def describe(intent):
    """What GPT-4o is told it may do for this intent."""
    parts = [f"click buttons: {', '.join(intent.clicks) or 'none'}"]
    if intent.types:
        parts.append(f"type into: {', '.join(intent.types)}")
    if intent.selects:
        parts.append(f"select in: {', '.join(intent.selects)}")
    if intent.rows:
        parts.append("click / double-click table rows")
    if intent.headers:
        parts.append("click column headers and the scrollbar")
    parts.append(f"keys: {', '.join(intent.keys)}")
    return "; ".join(parts) + ". Menus and every other button are refused."


# ---------------------------------------------------------------- checks

def _btn(s):
    """Button caption for comparison; OCR often reads O as 0."""
    return re.sub(r"[^a-z0-9]", "", str(s).lower()).replace("0", "o")


def same_button(a, b):
    return _btn(a) == _btn(b)


def _similar(label, allowed):
    a = re.sub(r"[^a-z0-9]", "", str(label).lower())
    return any(a and a == re.sub(r"[^a-z0-9]", "", x.lower()) for x in allowed)


def check_action(intent, event, el=None):
    """event = agent event dict; el = element it acts on (None: use the event's saved target)."""
    t = event.get("type")
    target = event.get("target") or {}
    kind = el["kind"] if el else target.get("kind")
    key = (el.get("key") or el.get("text", "")) if el else target.get("key", "")
    if t == "wait":
        return None
    if t == "key":
        k = str(event.get("keys", "")).lower().replace(" ", "")
        return None if k in intent.keys else f"key '{k}' is not permitted"
    if t in ("click", "double_click"):
        if kind == "menu_item":
            return f"menu '{key}' is not permitted"
        if kind == "button":
            if (intent.deny or DENY).search(str(key)):
                return f"'{key}' changes data and is not permitted"
            if _btn(key) in {_btn(c) for c in intent.clicks}:
                return None
            return f"button '{key}' is not part of {intent.name}"
        if kind == "table_row":
            return None if intent.rows else "clicking table rows is not part of this task"
        if kind in ("column_header", "scrollbar"):
            return None if intent.headers else f"clicking a {kind} is not part of this task"
        if kind == "input" and _similar(key, intent.types):
            return None                                   # focusing an allowed field
        if kind == "dropdown" and _similar(key, intent.selects):
            return None
        return f"clicking {kind or 'that'} '{key}' is not permitted"
    if t == "type":
        if kind == "input" and _similar(key, intent.types):
            return None
        return f"typing into '{key}' is not permitted for {intent.name}"
    if t == "select":
        if kind == "dropdown" and _similar(key, intent.selects):
            return None
        return f"changing '{key}' is not permitted for {intent.name}"
    return f"'{t}' is not permitted"


def check_screen(intent, title):
    t = " ".join(str(title).lower().split())
    if any(t.startswith(p) for p in intent.screens + intent.common):
        return None
    return f"window '{title}' is not part of {intent.name}"


def check_steps(intent, steps):
    """Every saved step (also those inside human blocks) must be allowed. steps: [{"event"} |
    {"human": True, "steps": [...]}]. Returns problems."""
    problems = []
    for n, step in enumerate(steps, start=1):
        inner = step["steps"] if step.get("human") else [step]
        for s in inner:
            reason = check_action(intent, s["event"])
            if reason:
                problems.append(f"step {n}: {reason}")
    return problems


def check_workflow(intent, wf):
    return check_steps(intent, wf.get("steps", []))
