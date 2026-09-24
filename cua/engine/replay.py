"""
replay.py - the production path: a saved capability, run without the model.

Per step: look -> locate the window on the map -> classify it (business outcome / recoverable /
hard) -> if the app is on another known window, walk there by the most reliable learned route ->
find the step's control by role + label -> act. At the end: checkpoint, outputs, record check.
Business outcomes end the run as such; transient errors are retried (reads only); anything else goes
to a person when one is present, or ends the run with a clear failure.
"""
import json

from cua import settings
from cua.artifact import capability
from cua.engine.checks import check_result, read_outputs, verify
from cua.engine.handoff import handoff
from cua.engine.navigate import front_again, go_home, nav_allowed, navigate
from cua.engine.observe import act, check_screen, dismiss, observe, resolve
from cua.engine.text import describe_step
from cua.errors import Drift, Stop, Stuck
from cua.safety import policy


def node_of(job, skeleton):
    """Map node of a saved screen."""
    nid, _ = job.map.locate(skeleton)
    if nid:
        return nid
    kind = "main" if job.profile.window_title.lower() in skeleton["window_title"].lower() else "dialog"
    return job.map.learn_screen(skeleton, kind, job.map.root if kind != "main" else None)


def resume(job, steps, start, final_node):
    """After a person's turn: index of the step to go on with (len(steps) = only the result is left),
    or None when the agent cannot tell where it is in the task."""
    front_again(job)
    screen = observe(job, pausable=False)
    if screen.node == final_node:
        return len(steps)
    for j in range(start, len(steps)):
        if steps[j]["node"] == screen.node:
            return j
    if job.intent.mode == "read":                        # reads may walk to where a later step starts
        for j in range(start, len(steps)):
            if job.map.path(screen.node, steps[j]["node"], lambda a: nav_allowed(job, a)) is not None:
                return j
    return None


def replay(cap, job):
    """Saved steps, no LLM. Returns the outputs."""
    from cua.engine.discovery import record           # only for --relearn
    steps = [dict(s) for s in cap.engine_steps()]
    for s, saved in zip(steps, cap.steps):
        s["node"], s["id"] = node_of(job, s["screen"]), saved.id
    final_node = node_of(job, cap.final_fingerprint)
    check, specs = capability.checkpoint_dict(cap), capability.outputs_dict(cap)
    i, changed, restarted, retries = 0, False, False, {}

    def lost():
        """The agent cannot tell where it is in the task: next step index (or 'relearn')."""
        nonlocal restarted
        if job.relearn:
            return "relearn"
        if job.intent.mode == "read" and not restarted:      # reads are safe to start over
            restarted = True
            job.run.log("lost - starting the read again from the main window")
            go_home(job)
            return 0
        handoff(job, "I can't tell where to continue - please finish the task in the app", "lost", guide=[])
        return len(steps)

    while True:
        try:
            job.step_id = steps[i]["id"] if i < len(steps) else "checkpoint"
            screen = observe(job)
            cond = check_screen(job, screen)
            if cond:
                if cond.code == "busy":
                    job.surface.wait(0.5)
                    continue
                prev = i - 1                                  # transient: the last step did not go through
                how = cap.outcomes.get(cond.code, "fail")
                if how != "retry" or retries.get(prev, 0) >= settings.MAX_RETRIES:
                    raise Stuck(f"{cond.title}: {cond.message}", code=cond.code, observed=screen.title)
                dismiss(job, screen, cond)
                retries[prev] = retries.get(prev, 0) + 1
                if prev < 0:                                  # during the reset, before step 1: just go on
                    job.recoveries.append(f"dismissed '{cond.title}' before the first step")
                    continue
                job.recoveries.append(f"retried step {steps[prev]['id']} after '{cond.title}' "
                                      f"(attempt {retries[prev]})")
                job.run.event("recovery", code=cond.code, retry_step=steps[prev]["id"], attempt=retries[prev])
                i = prev
                continue
            if i >= len(steps):                                   # all steps done: checkpoint + outputs
                if screen.node != final_node and not verify({"title": check["title"]}, screen, job.params):
                    raise Drift(i + 1, [f"expected window '{check['title']}' but found '{screen.title}'"],
                                expected=check["title"], observed=screen.title)
                if not verify(check, screen, job.params):
                    raise Stuck("checkpoint not met on the final screen", code="checkpoint_failed",
                                expected=json.dumps(check), observed=screen.title)
                values = read_outputs(job, screen, specs)
                check_result(job, values)
                break
            step = steps[i]
            if step.get("human"):                                 # always done by a person
                if not job.interactive:
                    raise Stuck(step["reason"], code="human_step")
                handoff(job, step["reason"], "human_step", guide=step["steps"])
                j = resume(job, steps, i + 1, final_node)
                j = lost() if j is None else j
                if j == "relearn":
                    return record(job, prefix=steps[:i + 1])
                i = j
                continue
            if screen.node != step["node"]:                       # elsewhere: walk there if the map knows how
                route = job.map.path(screen.node, step["node"], lambda a: nav_allowed(job, a))
                if route is None:
                    raise Drift(i + 1, [f"expected window '{job.map.name(step['node'])}' but found '{screen.title}'"],
                                expected=job.map.name(step["node"]), observed=screen.title)
                screen = navigate(job, screen, route)
            ev = step["event"]
            el = resolve(ev["target"], screen, job) if "target" in ev else None
            reason = policy.check_action(job.intent, ev, el)
            if reason:
                raise Stop(f"step {i + 1} is not permitted: {reason}", code="not_permitted")
            job.run.log(f"step {i + 1}/{len(steps)}: {describe_step(step)}")
            act(job, ev, el, screen)
            i += 1
        except Stuck as s:
            if not job.interactive:
                raise
            job.run.log(f"STUCK - {s.message}")
            if job.relearn and isinstance(s, Drift):
                return record(job, prefix=steps[:i], hint=[describe_step(x) for x in steps[i:]])
            block = handoff(job, s.message, s.code)
            j = resume(job, steps, i, final_node)
            if j is None:
                j = lost()
                if j == "relearn":
                    return record(job, prefix=steps[:i] + ([block] if block else []))
                continue_at, block = j, None                      # a person's steps only fit where we know
            else:
                continue_at = j
            if block:
                block["node"], block["id"] = node_of(job, block["screen"]), f"h{job.handoffs}"
                steps = steps[:i] + [block] + steps[continue_at:]
                i, changed = i + 1, True
            else:
                i = continue_at
    if changed:                                                   # a person's steps became part of the flow
        new = cap.model_copy(deep=True)
        new.steps = [capability.step_from_engine(n, {k: v for k, v in s.items() if k not in ("node", "id")})
                     for n, s in enumerate(steps, 1)]
        new.status = "draft"                                      # changed flows need a new review
        new.provenance.human_steps = sum(1 for s in steps if s.get("human"))
        capability.save(new, job.run.log)
    job.run.log("SUCCESS (replayed)")
    return values
