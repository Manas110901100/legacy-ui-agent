"""
rewards.py - how good was a run? The reinforcement signal of the learning layer.

A run that ended the way the capability promises - success, or a legitimate business outcome such as
"record not found" - is worth 1. Every place a person had to step in, every recovery and a long
duration cost a little, so the most reliable and smoothest way of doing a task scores highest.
A hard failure is worth 0. Runs the capability can't be blamed for (refused, cancelled by the
caller, bad input) are not scored at all.
"""
HANDOFF_COST = 0.25                 # a person had to step in
RECOVERY_COST = 0.05                # a transient error was dismissed / a step retried
SLOW_AFTER_S = 60.0                 # seconds after which a run starts to cost...
SLOW_COST_MAX = 0.2                 # ...at most this much
NOT_SCORED = ("refused", "cancelled")
NOT_SCORED_CODES = ("invalid_input", "not_approved", "confirmation_required", "app_not_running",
                    "capability_not_found")


def reward(result):
    """RunResult -> reward in [0, 1], or None when the run says nothing about the capability."""
    code = (result.failure.code if result.failure else None)
    if result.status in NOT_SCORED or code in NOT_SCORED_CODES:
        return None
    r = 1.0 if result.status in ("success", "business_outcome") else 0.0
    r -= HANDOFF_COST * len(result.interventions)
    r -= RECOVERY_COST * len(result.recoveries)
    r -= min(SLOW_COST_MAX, max(0.0, result.duration_s - SLOW_AFTER_S) / 600)
    return round(max(0.0, min(1.0, r)), 3)


def clean(result):
    """A run that needed nobody: the flow worked on its own (recoveries are fine)."""
    return result.status in ("success", "business_outcome") and not result.interventions
