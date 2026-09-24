"""
outcomes.py - what a window on screen means, and what a run returns to its caller.

Every window the agent looks at is classified against the app profile's known dialogs (plus a
keyword fallback). The kind of condition decides what the agent does:

    business     the app answered, just not with success -> stop and report it to the caller
                 (record_not_found, validation_error, permission_denied, ambiguous_match)
    recoverable  goes away or can be dismissed -> handle it and carry on
                 (busy: wait for "Please Wait"; transient: dismiss "Temporarily Unavailable", retry)
    escalate     a person must decide -> hand over if one is present, else report "escalated"
                 (session_expired: the agent never handles credentials; confirmation dialogs)
    hard         the agent cannot go on -> hand over if a person is present, else "failed" with
                 what step, what was expected, what was observed and a (blurred) screenshot
                 (app_error, error_message, unknown_window, target_missing, checkpoint_failed,
                  output_missing, result_mismatch, timeout, focus_lost, not_permitted)
    operator     a person took control (the Agent/Human switch) or a saved human step came up
"""
import re
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field

KINDS = {
    "record_not_found": "business", "validation_error": "business", "permission_denied": "business",
    "ambiguous_match": "business",
    "busy": "recoverable", "transient": "recoverable",
    "session_expired": "escalate", "confirmation": "escalate",
    "operator_takeover": "operator", "human_step": "operator",     # a person takes / is given control
}
ERROR_WORDS = re.compile(r"\b(error|failed|failure|invalid|exception|denied|cannot|could not|unable|"
                         r"not allowed|warning)\b", re.I)


def kind_of(code):
    return KINDS.get(code, "hard")


@dataclass
class Condition:
    code: str
    kind: str                       # business | recoverable | escalate | hard
    title: str
    message: str


def _norm(s):
    return " ".join(str(s).lower().split())


def classify(profile, title, texts=()):
    """A window (title + its texts) -> Condition, or None when it is an ordinary window."""
    t, message = _norm(title), " ".join(x for x in texts if x)[:300]
    for dialog, code, text in profile.dialogs:             # first match wins; text narrows a title
        d = _norm(dialog)
        if (t == d or t.startswith(d + " ")) and (not text or re.search(text, message, re.I)):
            return Condition(code, kind_of(code), title, message)
    if any(t.startswith(b) for b in profile.busy):
        return Condition("busy", "recoverable", title, message)
    if ERROR_WORDS.search(title):
        return Condition("app_error", "hard", title, message)
    hit = next((x for x in texts if x and ERROR_WORDS.search(x)), None)
    if hit:
        return Condition("error_message", "hard", title, hit)
    return None


def policy(code, mode):
    """How a capability treats a condition - written into the artifact so reviewers see it."""
    kind = kind_of(code)
    if kind == "business":
        return "return"
    if code == "busy":
        return "wait"
    if code == "transient":                 # a write is never repeated blindly: a person decides
        return "retry" if mode == "read" else "escalate"
    return "escalate" if kind == "escalate" else "fail"


# ---------------------------------------------------------------- the result contract

class Failure(BaseModel):
    code: str = Field(description="machine-readable error class, e.g. target_missing")
    message: str
    step: str | None = Field(None, description="capability step id where it happened")
    expected: str | None = None
    observed: str | None = None
    screenshot: str | None = Field(None, description="blurred screenshot of the last screen")


class RunResult(BaseModel):
    """What a caller (an AI agent, the CLI, the panel) gets back from one run."""
    run_id: str
    capability: str | None = None
    version: int | None = None
    mode: Literal["discovery", "replay"] | None = None
    status: Literal["success", "business_outcome", "failed", "escalated", "cancelled", "refused"]
    outcome: dict | None = Field(None, description="business outcome: {code, message}")
    outputs: dict = Field(default_factory=dict)
    answer: str | None = None
    failure: Failure | None = None
    recoveries: list[str] = Field(default_factory=list)
    interventions: list[str] = Field(default_factory=list, description="intervention request files")
    reliability: float | None = Field(None, description="the capability's reliability after this run (0-1)")
    llm_calls: int = 0
    duration_s: float = 0.0
