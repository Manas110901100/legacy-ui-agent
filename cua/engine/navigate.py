"""
navigate.py - moving around the app with the learned map.

    navigate(job, s, route)  follow learned moves; each must land where the map expects. The outcome
                             of every move - arrived or not - goes back into the map (learning).
    go_home(job)             main window, nothing else open, start state (the profile's reset)
    map_context(job, s)      what GPT-4o is told about the map (titles, moves, their reliability)
"""
from cua.engine.handoff import handoff
from cua.engine.observe import act, observe, resolve
from cua.engine.text import describe
from cua.errors import Stuck
from cua.learning import screenmap
from cua.safety import policy


def nav_allowed(job, action, home=False):
    """May this learned move be used to get somewhere? home=True also allows closing buttons."""
    if action["type"] == "key":
        return policy.check_action(job.intent, {"type": "key", "keys": action["keys"]}) is None
    if action.get("kind") == "table_row":
        return False
    event = {"type": action["type"], "target": {"kind": action["kind"], "key": action["key"]}}
    if policy.check_action(job.intent, event) is None:
        return True
    return home and action["kind"] == "button" and not job.profile.deny.search(action["key"]) \
        and any(policy.same_button(action["key"], b) for b in job.profile.close_buttons)


def navigate(job, screen, route, pausable=True):
    """Follow learned moves; each one must land on the window the map expects. A move that does not
    is recorded as a miss, so its reliability drops and the map stops choosing it."""
    for action, expect in route:
        event = {"type": action["type"]}
        if action["type"] == "key":
            event["keys"] = action["keys"]
            el = None
        else:
            event["target"] = {"kind": action["kind"], "key": action["key"]}
            el = resolve(event["target"], screen, job)
        src = screen.node
        job.run.log(f"navigate: {job.map.name(src)} --{screenmap.describe_action(action)}--> {job.map.name(expect)}")
        act(job, event, el, screen)
        screen = observe(job, pausable)
        if screen.node != expect:
            job.map.learn_miss(src, event, expect)
            job.map.save()
            job.run.event("move_missed", move=screenmap.describe_action(action), expected=job.map.name(expect),
                          got=screenmap.mask_title(screen.title))
            raise Stuck(f"expected '{job.map.name(expect)}' after {screenmap.describe_action(action)}, "
                        f"got '{screen.title}'", code="unknown_window", expected=job.map.name(expect),
                        observed=screen.title)
    return screen


def go_home(job):
    """Back to the main window with nothing else open: the most reliable learned moves first,
    else close the app's other windows; a person if that fails too."""
    surf = job.surface
    for _ in range(4):
        extra = [h for h in surf.windows() if h != surf.main]
        if not extra:
            surf.front(surf.main)
            surf.wait(0.3)
            reset_main(job)
            return
        try:
            surf.front(extra[0])                         # topmost leftover window first
            surf.wait(0.4)
            screen = observe(job, pausable=False)
            route = job.map.path(screen.node, job.map.root, lambda a: nav_allowed(job, a, home=True)) \
                if job.map.root else None
            if route:
                navigate(job, screen, route, pausable=False)
                continue
            job.run.log(f"  no reliable learned way back from '{job.map.name(screen.node)}'")
        except Stuck as s:
            job.run.log(f"  could not use the map to get back: {s.message}")
        for h in extra:
            job.run.log(f"closing leftover window '{screenmap.mask_title(surf.title(h))}'")
            surf.close(h)
        surf.wait(0.6)
    handoff(job, f"please close the other {job.profile.product} windows", guide=[])


def reset_main(job):
    """The profile's reset steps (BankAPP: Show All) - a search left from an earlier run would
    otherwise decide what the next run sees. Only buttons, never a denied one."""
    for step in job.profile.reset:
        target = step.get("target") or {}
        if step.get("type") != "click" or target.get("kind") != "button" or job.profile.deny.search(target["key"]):
            continue
        try:
            screen = observe(job, pausable=False)
            el = resolve(target, screen, job)
        except Stuck:
            continue                                     # not on this screen: nothing to reset
        job.run.log(f"reset: {describe(step, target)}")
        act(job, step, el, screen)


def front_again(job):
    """After a hand-over by console the terminal has focus: put the app back in front."""
    if not job.surface.owns(job.surface.foreground()):
        job.surface.front(job.surface.main)
        job.surface.wait(0.5)


def map_context(job, screen):
    """What GPT-4o is told about the map while it learns a task: window titles, the moves known from
    here and how reliable they have been. No customer data (titles are masked)."""
    m = job.map
    moves = [f"{screenmap.describe_action(a)} -> {m.name(d)}" + (f" (reliability {rel:.0%} over {tries})" if tries else "")
             for a, d, rel, tries in m.moves(screen.node) if m.usable(rel, tries)]
    return job.vault.scrub({"you_are_at": m.name(screen.node), "known_moves_here": moves,
                            "known_windows": m.tree_lines()})
