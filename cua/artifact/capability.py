"""
capability.py - the saved artifact: a typed, versioned, reviewable capability an AI agent can call.

A capability is what one successful discovery run turns into. It is decoupled from the model
transcript: no prompts, no screenshots, no customer data - only the contract (inputs, outputs,
outcomes), the steps and how each control is found, and the checkpoint that proves success.

    capabilities/<app>.<name>.json          current version
    capabilities/archive/<id>_v<n>.json     earlier versions
    capabilities/schema.json                JSON Schema of this file format

How controls are identified (Target): by ROLE + VISIBLE LABEL, the way an operator would name
them ("the 'Save' button", "the 'Email' field"), matched OCR-tolerantly (similarity >= 0.85).
Legacy desktop apps have no DOM ids or test ids, and pixel positions move with window size, DPI
and data - labels do not. Table rows are found by a parameter ("the row whose Customer Name
contains {account}"), never by a customer's data, with "ask the user" when several rows match.
Each step also names the window it happens in (title with record numbers masked) and keeps the
window's static fingerprint, which the replay uses as its precondition / drift check.
"""
import json
import shutil
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from cua import settings
from cua.artifact.outcomes import policy
from cua.learning.screenmap import mask_title
from cua.safety.pii import static_elements

ROLES = ("button", "input", "dropdown", "table_row", "column_header", "menu_item", "scrollbar")


class InputSpec(BaseModel):
    type: str = Field(description="name | email | phone | enum | money | account_ref | string")
    description: str = ""
    enum: list[str] | None = None
    pii: str | None = Field(None, description="personal-data class; masked before any LLM call and in logs")
    required: bool = True
    record_key: bool = Field(False, description="identifies the record: every result must show this value "
                                                "(else result_mismatch)")


class Extract(BaseModel):
    """Where an output is read on the final screen (static UI text only, never customer data)."""
    label: str | None = Field(None, description="value right of this label, e.g. 'Balance'")
    pattern: str | None = Field(None, description="text line, {value} = the output, * = anything")
    column: str | None = Field(None, description="all values of this table column")
    rows_below: str | None = Field(None, description="table rows under this header")


class OutputSpec(BaseModel):
    type: str = Field(description="string | money | number | account_no | list | table")
    description: str = ""
    pattern: str | None = Field(None, description="regex the value must match")
    pii: str | None = None
    extract: Extract | None = None


class Target(BaseModel):
    """How the control a step acts on is found on the screen."""
    role: str = Field(description="button | input | dropdown | table_row | column_header | menu_item | scrollbar")
    label: str | None = Field(None, description="visible label / caption (OCR-tolerant match)")
    column: str | None = Field(None, description="table_row: column to look in")
    equals: str | None = Field(None, description="table_row: cell equals this ({param} allowed)")
    contains: str | None = Field(None, description="table_row: cell contains this ({param} allowed)")
    row: int | None = Field(None, description="table_row: position, 1 = first (only when no parameter fits)")


class Step(BaseModel):
    id: str
    kind: Literal["action", "human"] = "action"
    window: str = Field(description="window the step happens in, record numbers masked")
    action: Literal["click", "double_click", "type", "select", "key", "wait"] | None = None
    target: Target | None = None
    value: str | None = Field(None, description="text to type / option to select; {param} placeholders")
    keys: str | None = None
    seconds: float | None = None
    on_many: Literal["ask_user", "fail"] | None = Field(None, description="several table rows fit")
    reason: str | None = Field(None, description="human step: why a person does this part")
    guide: list["Step"] = Field(default_factory=list, description="human step: what the person did")
    fingerprint: dict = Field(default_factory=dict, description="static controls of the window")
    end_fingerprint: dict | None = None


class Checkpoint(BaseModel):
    """Success condition checked on the final screen. All parts must hold."""
    window: str = Field(description="window title pattern, * = any text")
    text: str | None = Field(None, description="text that must be visible ({param}, * allowed)")
    table_contains: dict[str, str] | None = None


class AppRef(BaseModel):
    app: str
    product: str
    vendor_version: str | None = None
    surface: str = "desktop-ocr"


class Provenance(BaseModel):
    discovered_in: str | None = Field(None, description="run folder of the discovery run")
    model: str | None = None
    recorded_at: str
    example_goal: str | None = Field(None, description="the goal it was learned from, personal data masked")
    human_steps: int = 0
    migrated_from: str | None = None
    approved_by: str | None = None
    approved_at: str | None = None


class Capability(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    schema_: Literal["capability/1"] = Field("capability/1", alias="schema")
    id: str = Field(description="<app>.<name>, e.g. bankapp.account_details")
    version: int = 1
    status: Literal["draft", "approved"] = Field("draft", description="unattended replay needs 'approved'")
    app: AppRef
    description: str
    mode: Literal["read", "write"] = Field(description="write = changes data; confirmed before every run")
    inputs: dict[str, InputSpec]
    outputs: dict[str, OutputSpec]
    steps: list[Step]
    checkpoint: Checkpoint
    final_fingerprint: dict = Field(default_factory=dict)
    outcomes: dict[str, str] = Field(default_factory=dict,
                                     description="condition -> return | wait | retry | escalate | fail")
    provenance: Provenance

    @property
    def name(self):
        return self.id.split(".", 1)[1]

    # ------------------------------------------------ engine format <-> artifact
    def engine_steps(self):
        """Steps in the replay engine's format: {"screen", "event"} / human blocks."""
        return [_to_engine(s) for s in self.steps]


def _fingerprint(skeleton):
    """A window's static controls - never data the parser may have mistaken for a control."""
    return {"window_title": mask_title(skeleton["window_title"]), "size": skeleton.get("size"),
            "elements": static_elements(skeleton.get("elements", []))}


def _target(t):
    if t.get("kind") == "table_row":
        if "row" in t:
            return Target(role="table_row", row=t["row"])
        (col, want), = t["match"].items()
        return Target(role="table_row", column=col, **({"contains": want} if t.get("contains") else {"equals": want}))
    if "id" in t:
        return Target(role=t.get("kind", "button"), label=t["id"])
    return Target(role=t["kind"], label=t["key"])


def step_from_engine(n, s):
    if s.get("human"):
        return Step(id=f"s{n}", kind="human", window=mask_title(s["screen"]["window_title"]), reason=s["reason"],
                    guide=[step_from_engine(f"{n}.{k}", x) for k, x in enumerate(s["steps"], 1)],
                    fingerprint=_fingerprint(s["screen"]),
                    end_fingerprint=_fingerprint(s["end_screen"]) if s.get("end_screen") else None)
    ev = s["event"]
    target = _target(ev["target"]) if ev.get("target") else None
    return Step(id=f"s{n}", window=mask_title(s["screen"]["window_title"]), action=ev["type"], target=target,
                value=ev.get("text", ev.get("value")), keys=ev.get("keys"), seconds=ev.get("seconds"),
                on_many="ask_user" if target and target.role == "table_row" and target.row is None else None,
                fingerprint=_fingerprint(s["screen"]))


def _to_engine(step):
    if step.kind == "human":
        return {"human": True, "reason": step.reason, "screen": step.fingerprint,
                "end_screen": step.end_fingerprint, "steps": [_to_engine(g) for g in step.guide]}
    ev = {"type": step.action}
    t = step.target
    if t is not None:
        if t.role == "table_row":
            if t.row is not None:
                ev["target"] = {"kind": "table_row", "row": t.row}
            else:
                ev["target"] = {"kind": "table_row", "match": {t.column: t.contains or t.equals},
                                **({"contains": True} if t.contains is not None else {})}
        else:
            ev["target"] = {"kind": t.role, "key": t.label}
    if step.value is not None:
        ev["text" if step.action == "type" else "value"] = step.value
    if step.keys:
        ev["keys"] = step.keys
    if step.seconds:
        ev["seconds"] = step.seconds
    return {"screen": step.fingerprint, "event": ev}


def build(profile, intent, engine_steps, final_skeleton, checkpoint, extracts, run_dir, model, goal):
    """A finished discovery run -> a draft capability."""
    return Capability(
        id=f"{profile.app}.{intent.name}",
        app=AppRef(app=profile.app, product=profile.product, vendor_version=profile.vendor_version,
                   surface=profile.surface),
        description=intent.about, mode=intent.mode,
        inputs={s.name: InputSpec(type=s.type, description=s.description, enum=s.enum, pii=s.kind,
                                  record_key=s.record_key) for s in intent.slots},
        outputs={o.name: OutputSpec(type=o.type, description=o.description, pattern=o.pattern, pii=o.pii,
                                    extract=Extract(**extracts[o.name]) if extracts.get(o.name) else None)
                 for o in intent.outputs.values()},
        steps=[step_from_engine(n, s) for n, s in enumerate(engine_steps, 1)],
        checkpoint=Checkpoint(window=checkpoint.get("title") or mask_title(final_skeleton["window_title"]),
                              text=checkpoint.get("text_visible"), table_contains=checkpoint.get("table_contains")),
        final_fingerprint=_fingerprint(final_skeleton),
        outcomes={d[1]: policy(d[1], intent.mode) for d in profile.dialogs} | {
            "busy": "wait", "ambiguous_match": "return", "unknown_window": "fail", "target_missing": "fail",
            "checkpoint_failed": "fail", "output_missing": "fail", "result_mismatch": "fail"},
        provenance=Provenance(discovered_in=str(run_dir) if run_dir else None, model=model,
                              recorded_at=datetime.now().isoformat(timespec="seconds"),
                              example_goal=goal,
                              human_steps=sum(1 for s in engine_steps if s.get("human"))))


def checkpoint_dict(cap):
    """Checkpoint in the engine's verify() format."""
    c = cap.checkpoint
    out = {"title": c.window}
    if c.text:
        out["text_visible"] = c.text
    if c.table_contains:
        out["table_contains"] = c.table_contains
    return out


def outputs_dict(cap):
    return {k: {"type": o.type, "pattern": o.pattern, "extract": o.extract.model_dump(exclude_none=True)
                if o.extract else None} for k, o in cap.outputs.items()}


# ---------------------------------------------------------------- storage

def path_for(cap_id):
    return settings.CAPS / f"{cap_id}.json"


def load(cap_id):
    p = path_for(cap_id)
    return Capability.model_validate_json(p.read_text(encoding="utf-8")) if p.exists() else None


def catalog():
    return [Capability.model_validate_json(p.read_text(encoding="utf-8"))
            for p in sorted(settings.CAPS.glob("*.json")) if p.name != "schema.json" and not p.name.endswith(".stats.json")]


def save(cap, log=print, bump=True):
    """Write a capability; the previous version is archived and the version number goes up."""
    p = path_for(cap.id)
    settings.CAPS.mkdir(exist_ok=True)
    if p.exists() and bump:
        old = Capability.model_validate_json(p.read_text(encoding="utf-8"))
        (settings.CAPS / "archive").mkdir(exist_ok=True)
        shutil.copy(p, settings.CAPS / "archive" / f"{cap.id}_v{old.version}.json")
        cap.version = old.version + 1
    p.write_text(cap.model_dump_json(indent=1, by_alias=True, exclude_none=True), encoding="utf-8")
    log(f"SAVED capability '{cap.id}' v{cap.version} [{cap.status}] ({len(cap.steps)} steps) -> {p}")
    return p


def export_schema():
    settings.CAPS.mkdir(exist_ok=True)
    (settings.CAPS / "schema.json").write_text(json.dumps(Capability.model_json_schema(by_alias=True), indent=1),
                                      encoding="utf-8")


def from_workflow(wf, profile, source):
    """One-off migration of the older workflows/<name>.json format."""
    intent = profile.intents[wf["workflow"]]
    verify = dict(wf.get("verify") or {})
    extracts = {o.name: o.extract for o in intent.outputs.values() if o.extract}
    cap = build(profile, intent, wf["steps"], wf["final_screen"], verify, extracts, None, None, wf.get("example_task"))
    cap.version = wf.get("version", 1)
    cap.provenance.migrated_from = source
    cap.provenance.recorded_at = wf.get("created", cap.provenance.recorded_at)[:19]
    return cap
