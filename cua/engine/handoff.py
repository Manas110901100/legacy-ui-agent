"""
handoff.py - a person takes over the live session, and hands it back.

Control moves AGENT -> HUMAN -> AGENT on the same app session. Every hand-over writes an
intervention record (why, which step, blurred screenshot, who is in control, what the person did,
what was decided). What the person does is recorded (cua/operator/recorder.py), becomes moves on
the map, and - if they approve it in the review - a human step saved in the capability.
"""
from datetime import datetime
from types import SimpleNamespace

from cua import settings
from cua.artifact import outcomes
from cua.engine import hooks
from cua.engine.observe import to_semantic
from cua.engine.text import describe, describe_step, parameterize, shown
from cua.errors import Stop
from cua.learning import screenmap
from cua.safety import policy


def open_intervention(job, reason, code):
    """Intervention request: who is asked to do what, with which evidence."""
    n = len(job.interventions) + 1
    rec = {"id": f"{job.run.dir.name}-{n}", "capability": job.cap.id if job.cap else f"{job.profile.app}."
           f"{job.intent.name}", "goal": job.request, "step": job.step_id, "reason": reason, "code": code,
           "kind": outcomes.kind_of(code), "window": screenmap.mask_title(job.screen.title) if job.screen else None,
           "screenshot": job.run.last_screenshot, "in_control": "human" if job.interactive else "nobody (queued)",
           "opened_at": datetime.now().isoformat(timespec="seconds")}
    name = f"intervention_{n:02d}.json"
    job.interventions.append(name)
    job.run.save_json(name, rec)
    job.run.event("intervention", id=rec["id"], code=code, reason=reason, step=job.step_id)
    return name, rec


def close_intervention(job, name, rec, **fields):
    rec.update(fields, closed_at=datetime.now().isoformat(timespec="seconds"), in_control="agent")
    job.run.save_json(name, rec)


def human_event(job, a):
    """What the person did (recorder.interpret) -> agent event, None if not recognised."""
    if a["type"] == "key":
        return {"type": "key", "keys": a["keys"]}
    if a["el"] is None:
        return None
    event = {"type": a["type"]}
    if "text" in a:
        event["text"] = parameterize(a["text"], job.params)
    if "value" in a:
        event["value"] = parameterize(a["value"], job.params)
    event["target"] = to_semantic(a["el"], SimpleNamespace(view=a["view"]), job.params)
    return event


def human_step(job, a):
    """-> (step, line for the review list, may be saved?)."""
    event = human_event(job, a)
    if event is None:
        what = f"typed '{a['text']}'" if a["type"] == "type" else a["type"].replace("_", " ")
        return None, f"{what} - not recognised, won't be saved", False
    line = shown(describe(event, event.get("target")), job.params)
    reason = policy.check_action(job.intent, event, a["el"])
    literal = event.get("text", event.get("value"))
    if not reason and literal and "{" not in literal and job.vault.leaks({"v": literal}):
        reason = "typed personal data that is not one of the inputs"
    if reason:
        return None, f"{line} - {reason}, won't be saved", False
    return {"screen": a["skeleton"], "event": event}, line, True


def learn_person_moves(job, actions):
    """Every recognised click / key of the person that changed the window goes on the map."""
    for a in actions:
        event = human_event(job, a)
        if event is None or a.get("after") is None:
            continue
        src = job.map.learn_screen(a["skeleton"], job.surface.kind(a.get("hwnd")))
        dst = job.map.learn_screen(a["after"], job.surface.kind(a.get("after_hwnd")), src)
        job.map.learn_edge(src, event, dst, "person")
    job.map.save()


def handoff(job, reason, code="stuck", guide=None):
    """A person does the next part in the live app, then hands back (control: agent -> human -> agent).
    guide = steps of a saved human step, shown as a reminder ([] = just a request); nothing recorded.
    Otherwise what they do is recorded; returns the human step they approved, or None."""
    job.handoffs += 1
    if job.handoffs > settings.MAX_HANDOFFS:
        raise Stop(f"gave up after {settings.MAX_HANDOFFS} hand-overs - last problem: {reason}",
                   code="too_many_handoffs")
    job.run.log(f"HAND-OVER to a person: {reason}")
    name, rec = open_intervention(job, reason, code)
    start = job.screen.skeleton if job.screen else None
    hooks.alert()
    if guide is not None:
        before = "\n".join(f"  {n}. {shown(describe_step(s), job.params)}" for n, s in enumerate(guide, 1))
        text = f"Your turn: {reason}" + (f"\nLast time you:\n{before}" if before else "") + \
            "\n\nThen switch back to Agent."
        if not job.ask(hooks.take_over, text):
            close_intervention(job, name, rec, decision="aborted")
            raise Stop("stopped by user during hand-over", code="user_stopped")
        job.run.log("person handed back")
        close_intervention(job, name, rec, decision="done by person (saved human step)")
        return None

    recorder = None
    if settings.RECORD_HUMAN:
        from cua.operator.recorder import Recorder          # low-level input hooks: only when needed
        recorder = Recorder(job.surface.pid)
        recorder.start()
    try:
        ok = job.ask(hooks.take_over, "You have control. Do what you need in the app, then switch back to Agent."
                     if reason == hooks.TOOK_OVER else
                     f"Stuck: {reason}\nPlease do this part in the app yourself, then switch back to Agent.")
    finally:
        events, final = recorder.stop() if recorder else ([], None)
    if not ok:
        close_intervention(job, name, rec, decision="aborted")
        raise Stop("stopped by user during hand-over", code="user_stopped")
    job.run.log("person handed back")
    actions, end_screen = [], None
    if events:
        from cua.operator.recorder import interpret
        actions, end_screen = interpret(events, final)
    learn_person_moves(job, actions)
    items = [human_step(job, a) for a in actions]
    chosen = job.ask(hooks.review_steps, [line for _, line, _ in items], [ok for _, _, ok in items]) if items else []
    if chosen is None:
        close_intervention(job, name, rec, decision="aborted", human_actions=[line for _, line, _ in items])
        raise Stop("stopped by user", code="user_stopped")
    steps = [items[i][0] for i in chosen if items[i][2]]
    close_intervention(job, name, rec, human_actions=[line for _, line, _ in items],
                       decision="saved as human step" if steps else "not saved")
    if not steps:
        return None
    job.run.log("saved the person's steps: " + "; ".join(describe_step(s) for s in steps))
    return {"human": True, "reason": reason, "screen": start or steps[0]["screen"], "steps": steps,
            "end_screen": end_screen}
