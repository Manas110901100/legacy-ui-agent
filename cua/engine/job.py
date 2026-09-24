"""
job.py - one request in progress (Job) and its evidence (Run).

Everything a run writes goes through the run's PII vault: no customer data in clear.
    runs/<id>/run.log            readable log
    runs/<id>/events.jsonl       structured events (observe, decision, action, condition, recovery, ...)
    runs/<id>/NN_screen.png      screenshot, data blurred       NN_screen.json  masked parsed screen
    runs/<id>/llm_NN.json        exactly what GPT-4o received (masked)
    runs/<id>/intervention_NN.json, result.json
"""
import json
import sys
import time
from datetime import datetime

from PIL import ImageFilter

from cua import settings
from cua.learning import screenmap


class Run:
    def __init__(self, name, vault):
        base = settings.RUNS / f"{datetime.now():%Y%m%d_%H%M%S}_{name}"
        self.dir, n = base, 1
        while self.dir.exists():                        # two runs in the same second get their own folder
            n += 1
            self.dir = base.with_name(f"{base.name}_{n}")
        self.dir.mkdir(parents=True)
        self.vault = vault
        self.n = self.llm_n = 0
        self.last_screenshot = None

    def log(self, msg):
        line = f"[{datetime.now():%H:%M:%S}] {self.vault.scrub_text(msg)}"
        print(line, file=sys.stderr)                    # stdout stays clean for --json results
        with open(self.dir / "run.log", "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def event(self, _event, **fields):
        rec = {"t": datetime.now().isoformat(timespec="milliseconds"), "event": _event, **fields}
        with open(self.dir / "events.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(self.vault.scrub(rec), ensure_ascii=False, default=str) + "\n")

    def save_llm(self, payload):
        """Exactly what was sent to GPT-4o (already masked), for review."""
        self.llm_n += 1
        (self.dir / f"llm_{self.llm_n:02d}.json").write_text(
            json.dumps(payload, indent=1, ensure_ascii=False), encoding="utf-8")

    def save_json(self, name, obj):
        (self.dir / name).write_text(json.dumps(self.vault.scrub(obj), indent=1, ensure_ascii=False, default=str),
                                     encoding="utf-8")

    def save_screen(self, screen):
        """Screenshot with every piece of data blurred, masked view, roles image."""
        from cua.perception.parse_screen import draw_roles
        self.n += 1
        if screen.image is not None:
            img = blurred(screen, self.vault)
            name = f"{self.n:02d}_screen.png"
            img.save(self.dir / name)
            draw_roles(img, screen.elements, self.dir / f"{self.n:02d}_roles.png")
            self.last_screenshot = name
        (self.dir / f"{self.n:02d}_screen.json").write_text(
            json.dumps(self.vault.redact_view(screen.view), indent=1, ensure_ascii=False), encoding="utf-8")


def blurred(screen, vault):
    """Copy of the screenshot with data blurred: field values, table rows, and every text that is
    not plain UI wording (numbers, names, e-mails...). Buttons, menus and headers stay readable."""
    img = screen.image.convert("RGB").copy()
    for e in screen.elements:
        text = str(e.get("text", ""))
        data = e["kind"] in ("input", "dropdown", "table_row", "table_cell") or e.get("content") == "dynamic" \
            or (e["kind"] in ("text", "label", "status", "title") and vault.mask_words(text) != text)
        if data:
            x0, y0, x1, y1 = [int(v) for v in e["box"]]
            if x1 > x0 and y1 > y0:
                region = img.crop((x0, y0, x1, y1))
                img.paste(region.filter(ImageFilter.GaussianBlur(radius=6)), (x0, y0))
    return img


class Job:
    """One request in progress: what to do, with which values, on which surface, where to log."""
    def __init__(self, surface, profile, intent, params, vault, run, request, relearn=False, interactive=True,
                 cap=None):
        self.surface, self.profile, self.intent, self.params = surface, profile, intent, params
        self.vault, self.run, self.request, self.relearn, self.interactive = vault, run, request, relearn, interactive
        self.cap = cap
        kinds = {s.name: s.kind for s in intent.slots}
        self.param_tokens = {k: vault.token(v, kinds.get(k, "TEXT")) if kinds.get(k, "TEXT") else v
                             for k, v in params.items()}
        self.map = screenmap.ScreenMap(settings.MAPS / f"{profile.app}.json")
        self.hwnd_nodes = {}           # window handle -> map node, for the window tree
        self.picks = {}                # row choices the user made during this run
        self.pending = None            # (node, event) of the last action, until its result is seen
        self.handoffs, self.screen, self.step_id = 0, None, None
        self.recoveries, self.interventions = [], []
        self.deadline = time.monotonic() + profile.timeout

    def ask(self, question, *args):
        """Wait for a person (a hooks function: choose, take_over, ...). Time spent waiting for a
        person does not count against the run's time limit."""
        start = time.monotonic()
        try:
            return question(*args)
        finally:
            self.deadline += time.monotonic() - start
