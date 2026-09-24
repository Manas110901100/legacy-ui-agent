"""
reliability.py - learning from outcomes: how much can a move or a capability be trusted?

Every map move and every capability keeps a record of how it went. Reliability is the mean of a
Beta posterior over success (a Beta(1, 1) prior: one success and one failure imagined), so a
thing tried once is not "100 % reliable", and a record that keeps failing sinks quickly.

    estimate(ok, fail)           -> (mean, lower bound)       for map moves (success / failure)
    CapabilityStats(cap_id)      the run record of one capability  (capabilities/<id>.stats.json)
        .add(result)             score a finished run (rewards.reward) and keep it
        .reliability(last=None)  Beta mean over rewards (a reward of 0.8 = 0.8 success, 0.2 failure)
        .can_approve()           the gate for unattended use: (ok, reason)
        .should_demote()         an approved capability that stopped being reliable
"""
import json
import math
from datetime import datetime

from cua import settings
from cua.learning import rewards

KEEP_RUNS = 200


def estimate(ok, fail):
    """Beta(1 + ok, 1 + fail): posterior mean and a conservative (~5 %) lower bound."""
    a, b = 1.0 + ok, 1.0 + fail
    mean = a / (a + b)
    sd = math.sqrt(a * b / ((a + b) ** 2 * (a + b + 1)))
    return mean, max(0.0, mean - 1.645 * sd)


class CapabilityStats:
    """Runtime telemetry of one capability - kept next to, not inside, the reviewed artifact."""
    def __init__(self, cap_id):
        self.cap_id = cap_id
        self.file = settings.CAPS / f"{cap_id}.stats.json"
        data = json.loads(self.file.read_text(encoding="utf-8")) if self.file.exists() else {}
        self.runs = data.get("runs", [])

    def add(self, result, when=None):
        """Record a finished run. Returns its reward (None = not scored)."""
        r = rewards.reward(result)
        if r is None:
            return None
        self.runs.append({"t": when or datetime.now().isoformat(timespec="seconds"), "run": result.run_id,
                          "version": result.version, "mode": result.mode, "status": result.status,
                          "code": (result.outcome or {}).get("code") or (result.failure.code if result.failure else None),
                          "step": result.failure.step if result.failure else None, "reward": r,
                          "clean": rewards.clean(result), "recoveries": len(result.recoveries),
                          "handoffs": len(result.interventions), "seconds": result.duration_s})
        self.runs = self.runs[-KEEP_RUNS:]
        self.save()
        return r

    def save(self):
        settings.CAPS.mkdir(exist_ok=True)
        self.file.write_text(json.dumps({"capability": self.cap_id, "runs": self.runs}, indent=1), encoding="utf-8")

    def reliability(self, last=None):
        runs = self.runs[-last:] if last else self.runs
        ok = sum(r["reward"] for r in runs)
        return round(estimate(ok, len(runs) - ok)[0], 3)

    def clean_runs(self):
        return sum(1 for r in self.runs if r["clean"])

    def failures(self):
        """Where it goes wrong: {(code, step): count} - which step to look at first."""
        out = {}
        for r in self.runs:
            if not r["clean"] and r["code"]:
                out[(r["code"], r["step"])] = out.get((r["code"], r["step"]), 0) + 1
        return out

    def can_approve(self):
        clean, rel = self.clean_runs(), self.reliability()
        if clean < settings.APPROVE_MIN_CLEAN_RUNS:
            return False, f"only {clean} clean run(s); {settings.APPROVE_MIN_CLEAN_RUNS} needed"
        if rel < settings.APPROVE_MIN_RELIABILITY:
            return False, f"reliability {rel:.2f} < {settings.APPROVE_MIN_RELIABILITY}"
        return True, f"{clean} clean runs, reliability {rel:.2f}"

    def should_demote(self):
        recent = self.runs[-settings.DEMOTE_WINDOW:]
        return len(recent) >= 3 and self.reliability(settings.DEMOTE_WINDOW) < settings.DEMOTE_BELOW

    @classmethod
    def rebuild(cls, cap_id):
        """Offline learning: replay every recorded result of this capability in runs/ into a fresh
        record (oldest first). The evidence of past runs becomes its reliability."""
        from cua.artifact.outcomes import RunResult
        stats = cls(cap_id)
        stats.runs = []
        for p in sorted(settings.RUNS.glob("*/result.json")):
            try:
                res = RunResult.model_validate_json(p.read_text(encoding="utf-8"))
            except ValueError:
                continue                                   # older runs, before the result contract
            if res.capability == cap_id:
                stamp = p.parent.name[:15]
                stats.add(res, when=f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}T{stamp[9:11]}:{stamp[11:13]}:{stamp[13:15]}")
        stats.save()
        return stats

    def summary(self):
        if not self.runs:
            return "no runs yet"
        return (f"reliability {self.reliability():.2f} over {len(self.runs)} run(s), "
                f"{self.clean_runs()} clean")
