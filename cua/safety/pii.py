"""
pii.py - keep customer data away from GPT-4o.

GPT-4o only ever sees tokens such as <NAME_1>, <EMAIL_2>, <ACCT_3>. A Vault (one per run)
remembers which real value each token stands for, so values are put back locally: just
before typing into the application, and when an answer is shown to the user.

    vault.redact_request(text)  request -> tokens; unknown words are masked too (default deny)
    vault.redact_view(view)     parse_screen view -> same view with every customer value masked
    vault.scrub(obj)            history / hints -> known values and PII patterns masked
    vault.leaks(payload)        what would still leak; the agent refuses to call GPT-4o if any
    vault.rehydrate(text)       tokens -> real values
"""
import copy
import json
import re

TOKEN = re.compile(r"<[A-Z]+_\d+>")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
ACCT = re.compile(r"\bACC\d{6}\b", re.I)
DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2})?)?\b")
PHONE = re.compile(r"(?<!\w)(?<!\d[.,])\+?\d[\d \-]{6,18}\d(?!\w|[.,]\d)")    # not part of 1,234,567.89
MONEY = re.compile(r"(?<![\w.])\$?(?:\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+\.\d{2})(?!\w|\.\d)")
NUMBER = re.compile(r"(?<![\w.<])\$?\d(?:[\d,]*\d)?(?:\.\d+)?(?![\w>])")
WORD = re.compile(r"[A-Za-z][A-Za-z'.\-]*")

# Words that may reach GPT-4o as they are. Everything else in free text is masked.
SAFE_WORDS = set("""
a an the for with and or to of in on at by from as is are was were be been it its this that these those
i me my we our you your he she his her they their them please can could would should will want need like
new open opened create created add make register set get give show shown list display find found search
look lookup up view see check tell what who whom which when where whose how many much any all every each
some no not none only one first last latest recent total count number numbers no. num
account accounts acct customer customers client clients name named called email mail e-mail phone mobile
tel telephone type kind savings saving checking deposit deposits opening initial amount balance balances
money details detail info information history transaction transactions statement status active closed
owner holder date opened contact available user admin teller viewer ready matching match matches
withdraw withdrawal withdrawals transfer transfers close delete remove edit update change modify send pay
move funds cancel save ok yes details success error invalid required please wait selection record records
bankapp core banking system about help file log off exit refresh
s there here do does did has have had into out than then so if also just now today current currently
existing more most less other another same use using via don't doesn't can't what's who's it's i'd i'm
click press button field form screen window tab enter
pull fetch retrieve bring provide return profile summary overview full everything regarding
""".split())
SAFE_VALUES = re.compile(r"(?i)^(savings|checking|active|closed|deposit|withdrawal|transfer(?:[ _-]?(?:in|out))?|"
                         r"opening deposit|yes|no|none|n/a|-)?$")
SAFE_STATUS = re.compile(r"(?i)^\d+ account\(s\)$|^ready$")

COLUMN_KINDS = [(r"account\s*no|acct|account\s*number|counterparty", "ACCT"), (r"e-?mail", "EMAIL"),
                (r"phone|mobile|tel", "PHONE"), (r"customer|name|owner|holder", "NAME"),
                (r"balance|amount|deposit|available", "AMOUNT")]
STRICT_KINDS = ("NAME", "EMAIL", "PHONE", "ACCT")     # always leak-checked


DATA_LIKE = re.compile(r"@|\d{3,}|\d[\d,]*\.\d\d|\b[A-Za-z]{2,4}[0-9IOlo|]{5,}\b")   # e-mail, ids, amounts


def static_elements(elements, tol=8):
    """Elements that are UI (labels, captions), for fingerprints stored in the map and in capabilities.
    When the parser mistakes data for controls (a highlighted table row read as a button, a band of
    rows read as menu items) their text must not be stored: anything that looks like data goes, and
    so does every element of the same kind on that row (names have no pattern, but share the row)."""
    def cy(e):
        return (e["box"][1] + e["box"][3]) / 2
    bad = [(e["kind"], cy(e)) for e in elements if DATA_LIKE.search(str(e.get("key", "")))]
    return [e for e in elements if not DATA_LIKE.search(str(e.get("key", "")))
            and not any(e["kind"] == k and abs(cy(e) - y) <= tol for k, y in bad)]


def kind_for_label(label):
    for pattern, kind in COLUMN_KINDS:
        if re.search(pattern, str(label), re.I):
            return kind
    return "TEXT"


def kind_of(value):
    """Best token kind for a free value (e.g. a search query: account number or name)."""
    v = str(value).strip()
    if ACCT.fullmatch(v):
        return "ACCT"
    if EMAIL.fullmatch(v):
        return "EMAIL"
    if PHONE.fullmatch(v):
        return "PHONE"
    return "NAME"


def _key(value):
    return " ".join(str(value).split()).casefold()


class Vault:
    def __init__(self):
        self.by_value, self.by_token, self.count = {}, {}, {}
        self.strict = set()          # values that must never appear in a GPT-4o payload

    # ------------------------------------------------ tokens
    def token(self, value, kind, strict=None):
        value = " ".join(str(value).split())
        if not value:
            return value
        if kind == "ACCOUNT":
            kind = kind_of(value)
        k = _key(value)
        if k not in self.by_value:
            self.count[kind] = self.count.get(kind, 0) + 1
            tok = f"<{kind}_{self.count[kind]}>"
            self.by_value[k], self.by_token[tok] = tok, value
        if strict or (strict is None and kind in STRICT_KINDS):
            self.strict.add(value)
        return self.by_value[k]

    def real(self, token):
        return self.by_token.get(token)

    def rehydrate(self, text):
        return TOKEN.sub(lambda m: self.by_token.get(m.group(0), m.group(0)), str(text))

    # ------------------------------------------------ masking
    def _outside_tokens(self, text, fn):
        parts = TOKEN.split(str(text))
        toks = TOKEN.findall(str(text))
        out = [fn(parts[0])]
        for tok, part in zip(toks, parts[1:]):
            out += [tok, fn(part)]
        return "".join(out)

    def _patterns(self, s):
        s = self._outside_tokens(s, lambda p: EMAIL.sub(lambda m: self.token(m.group(0), "EMAIL"), p))
        s = self._outside_tokens(s, lambda p: ACCT.sub(lambda m: self.token(m.group(0).upper(), "ACCT"), p))
        dates = []

        def keep_date(m):
            dates.append(m.group(0))
            return f"\x00{len(dates) - 1}\x00"
        s = DATE.sub(keep_date, s)
        s = self._outside_tokens(s, lambda p: PHONE.sub(lambda m: self.token(m.group(0), "PHONE"), p))
        s = self._outside_tokens(s, lambda p: MONEY.sub(lambda m: self.token(m.group(0), "AMOUNT"), p))
        return re.sub(r"\x00(\d+)\x00", lambda m: dates[int(m.group(1))], s)

    def _known(self, s):
        """Replace strict values the vault already knows (e.g. the customer name) wherever they occur."""
        for value in sorted(self.strict, key=len, reverse=True):
            if len(value) >= 2:
                rx = re.compile(rf"(?<![\w@.]){re.escape(value)}(?![\w@])", re.I)
                s = self._outside_tokens(s, lambda p, rx=rx, value=value: rx.sub(self.by_value[_key(value)], p))
        return s

    def scrub_text(self, text):
        """PII patterns and known values -> tokens. For text built by the agent or static UI text."""
        return self._known(self._patterns(str(text)))

    def mask_id(self, eid):
        """Element ids are slugs of their text (btn_show_all), where "_" glues a value to the prefix
        and hides it from word-bounded patterns: scrub the id with "_" read as a space
        (btn_acc100005 -> btn_<ACCT_1>)."""
        spaced = str(eid).replace("_", " ")
        masked = self.scrub_text(spaced)
        return masked.replace(" ", "_") if masked != spaced else eid

    def mask_words(self, text, kind="TEXT", strict=False):
        """Default deny: scrub, then every run of words not in SAFE_WORDS becomes one token."""
        s = re.sub(r"(?<=[A-Za-z])'s\b", " 's", self.scrub_text(text))    # "Brown's" -> name + 's
        out, run = [], []

        def flush():
            if run:
                phrase = "".join(run).strip()
                trail = "".join(run)[len("".join(run).rstrip()):]
                out.append(self.token(phrase, kind, strict=strict) + trail)
                run.clear()
        for piece in re.split(r"(<[A-Z]+_\d+>|[A-Za-z][A-Za-z'.\-]*|\s+)", s):
            if not piece:
                continue
            if WORD.fullmatch(piece) and not TOKEN.fullmatch(piece) and piece.lower().strip(".") not in SAFE_WORDS:
                run.append(piece)
            elif piece.isspace() and run:
                run.append(piece)
            else:
                flush()
                out.append(piece)
        flush()
        return "".join(out)

    def redact_request(self, text):
        """The user's request as GPT-4o may see it. Every value and unknown word is a token."""
        s = self._patterns(str(text))
        s = self._outside_tokens(s, lambda p: NUMBER.sub(lambda m: self.token(m.group(0), "AMOUNT"), p))
        return self.mask_words(s, "TEXT")      # becomes leak-checked once it is used as a name / account

    def scrub(self, obj):
        """Recursively scrub strings in history lines, hints etc."""
        if isinstance(obj, str):
            return self.scrub_text(obj)
        if isinstance(obj, list):
            return [self.scrub(x) for x in obj]
        if isinstance(obj, dict):
            return {k: self.scrub(v) for k, v in obj.items()}
        return obj

    def _value(self, value, kind):
        v = str(value).strip()
        if not v or SAFE_VALUES.match(v) or DATE.fullmatch(v):
            return v
        if kind == "TEXT" and kind_of(v) != "NAME":      # an email / phone / account in a free column
            kind = kind_of(v)
        return self.token(v, kind)

    def redact_view(self, view, params=None):
        """parse_screen view -> copy with customer data replaced by tokens. With params, table rows
        also say which parameters they contain ("matches"), since GPT-4o cannot see that in tokens."""
        v = copy.deepcopy(view)
        wanted = {k: _key(p) for k, p in (params or {}).items() if len(_key(p)) >= 2}
        for row in (v.get("table") or {}).get("rows", []):
            hits = [k for k, p in wanted.items() if any(p in _key(cell) for cell in row["values"])]
            if hits:
                row["matches"] = hits
        v["screen"] = self.mask_words(v.get("screen") or "")
        for item in v.get("menu", []) + v.get("buttons", []):
            item["text"] = self.scrub_text(item["text"])
            item["id"] = self.mask_id(item["id"])
        for i in v.get("inputs", []):
            i["label"] = self.scrub_text(i.get("label", ""))
            kind = "ACCOUNT" if i.get("role") == "search" else kind_for_label(i["label"])
            i["value"] = self._value(i.get("value", ""), kind)
        table = v.get("table")
        if table:
            kinds = [kind_for_label(c) for c in table["columns"]]
            table["columns"] = [self.scrub_text(c) for c in table["columns"]]
            for row in table["rows"]:
                row["values"] = [self._value(val, k) for val, k in zip(row["values"], kinds)]
        for o in v.get("other_text", []):
            text = o["text"]
            if SAFE_STATUS.match(text.strip()):
                o["text"] = self.scrub_text(text)
            elif o.get("content") == "dynamic":
                kind = kind_for_label(o["label"]) if o.get("label") else "TEXT"
                o["text"] = self._value(text, kind) if kind != "TEXT" else self.mask_words(text)
            else:
                o["text"] = self.mask_words(text)
            if o.get("label"):
                o["label"] = self.mask_words(o["label"])
        return v

    # ------------------------------------------------ safety net
    def leaks(self, payload):
        """Strings in the payload that look like PII or equal a strict value. Empty = safe to send."""
        s = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        s = TOKEN.sub(" ", s)
        s += "\n" + s.replace("_", " ")              # values inside ids (btn_acc100005) are found too
        found = [f"email:{m}" for m in EMAIL.findall(s)] + [f"account:{m}" for m in ACCT.findall(s)]
        found += [f"phone:{m}" for m in PHONE.findall(DATE.sub(" ", s))]
        for value in self.strict:
            if len(value) >= 2 and re.search(rf"(?<![\w@.]){re.escape(value)}(?![\w@])", s, re.I):
                found.append("known value")
        return found

    # ------------------------------------------------ saved checks
    def generalize(self, text, params):
        """Success check GPT-4o wrote with tokens -> reusable pattern: slot values -> {slot};
        other tokens, account numbers and any other number -> * (counts, amounts differ every run)."""
        inv = {_key(v): k for k, v in params.items()}

        def tok(m):
            real = self.by_token.get(m.group(0))
            return "{" + inv[_key(real)] + "}" if real is not None and _key(real) in inv else "*"
        s = TOKEN.sub(tok, str(text))
        for k, v in sorted(params.items(), key=lambda kv: -len(str(kv[1]))):
            if len(str(v)) >= 2:
                s = re.sub(rf"(?<![\w{{]){re.escape(str(v))}(?![\w}}])", "{" + k + "}", s, flags=re.I)
        return re.sub(r"\d[\d,.]*\d|\d", "*", ACCT.sub("*", s))
