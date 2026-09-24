"""
errors.py - how a run ends early. Every exception carries a machine-readable code so the caller
gets a clear result (see outcomes.RunResult), and a failure says what was expected / observed.

    Outcome  a legitimate business result the caller needs ("record_not_found"): not a crash
    Stuck    the agent cannot go on by itself: a person may take over (interactive), else a failure
    Drift    Stuck because the app shows a window the flow does not expect
    Stop     anything else that must end the run: aborted, not permitted, focus lost, timeout...
"""


class Stop(Exception):
    code = "stopped"

    def __init__(self, message, code=None, expected=None, observed=None):
        super().__init__(message)
        self.message = message
        self.code = code or self.code
        self.expected, self.observed = expected, observed


class Stuck(Stop):
    code = "stuck"


class Drift(Stuck):
    code = "unknown_window"

    def __init__(self, step, diffs, expected=None, observed=None):
        self.step, self.diffs = step, diffs
        super().__init__(f"unexpected window before step {step}: " + "; ".join(diffs[:6]),
                         expected=expected, observed=observed)


class Outcome(Stop):
    """A business outcome: the app answered, just not with success (no such record, invalid input...)."""
    code = "business_outcome"
