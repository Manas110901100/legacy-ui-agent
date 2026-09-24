"""
discovery.py - the model learns a task once (observe -> decide -> act), on masked screens.

Each turn: look at the window, mask it, ask GPT-4o for ONE event, check it against the allow-list
and the flow rules, act, repeat - until GPT-4o says done and the checkpoint holds on screen. The
steps (targets by role + label, values as {placeholders}), the checkpoint and the rules for reading
the outputs become a draft capability. A person can be brought in at any point (handoff.py).
"""
import json
import re

from cua import settings
from cua.artifact import capability, outputs
from cua.engine import hooks
from cua.engine.checks import check_result, read_outputs, verify
from cua.engine.handoff import handoff
from cua.engine.llm import STEP_SYSTEM, llm_json
from cua.engine.navigate import front_again, map_context
from cua.engine.observe import act, check_screen, dismiss, observe, resolve, to_semantic
from cua.engine.text import describe, describe_step, norm, parameterize, shown, sim
from cua.errors import Stop, Stuck
from cua.learning import screenmap
from cua.safety import policy


def searched_for(steps, values):
    """Did the flow type one of these {params} and then run the search (a button or Enter)?"""
    events = [s.get("event", {}) for s in steps]
    typed = [k for k, e in enumerate(events) if e.get("type") == "type" and e.get("text") in values]
    return bool(typed) and any(e.get("type") == "key" and e.get("keys") == "enter"
                               or e.get("type") == "click" and (e.get("target") or {}).get("kind") == "button"
                               for e in events[typed[-1] + 1:])


def generalize_check(check, job, final):
    """Success check GPT-4o wrote with tokens -> one that also works on later runs: slot values
    become {placeholders}, generated values become *, checks that say nothing are dropped, and
    the final window title is always checked."""
    def meaningful(v):
        return bool(re.sub(r"[\s*.,:;'\"()\-]", "", str(v)))
    out = {"title": screenmap.mask_title(final.title)}         # "Account Details - *": any record
    for kind, val in (check or {}).items():
        if isinstance(val, dict):
            cols = {job.vault.rehydrate(c): job.vault.generalize(v, job.params) for c, v in val.items()}
            cols = {c: v for c, v in cols.items() if meaningful(v)}
            if cols:
                out[kind] = cols
        elif kind != "title" and meaningful(job.vault.generalize(val, job.params)):
            out[kind] = job.vault.generalize(val, job.params)
    return out


def label_left_of(elements, el):
    cy = (el["box"][1] + el["box"][3]) / 2
    left = [e for e in elements if e is not el and e["kind"] in outputs.TEXT_KINDS
            and abs((e["box"][1] + e["box"][3]) / 2 - cy) <= outputs.ROW_TOL and e["box"][2] <= el["box"][0] + 2]
    return max(left, key=lambda e: e["box"][2])["text"].rstrip(": ") if left else None


def rule_from_pointer(job, final, out, pointer):
    """GPT-4o pointed at the token / text holding an output -> a rule that finds it on any record."""
    raw = job.vault.rehydrate(pointer).strip()
    if not raw:
        return None
    if out.type == "table":
        head = next((e for e in final.elements if e["kind"] in outputs.TEXT_KINDS and sim(e["text"], raw) >= 0.8),
                    None)
        return {"rows_below": head["text"]} if head else None
    if out.type == "list":
        cols = (final.view.get("table") or {}).get("columns", [])
        return {"column": raw} if raw in cols else None
    els = [e for e in final.elements if e["kind"] in outputs.TEXT_KINDS and raw.lower() in str(e["text"]).lower()]
    for el in els:                           # the same value can show up twice (e.g. balance in the history)
        if norm(el["text"]) == norm(raw):
            label = label_left_of(final.elements, el)
            if label and not re.search(r"\d", label) and job.vault.mask_words(label) == label:
                return {"label": label}      # a static caption, never data
    for el in els:
        if norm(el["text"]) != norm(raw):
            text = str(el["text"]).replace(raw, "\x00", 1)
            return {"pattern": job.vault.generalize(text, job.params).replace("\x00", "{value}")}
    return None


def derive_extracts(job, final, pointers):
    """Rules for every declared output: from what GPT-4o pointed at, else where the vendor product
    shows it (profile hint). A rule is kept only if it reads a value on the final screen now."""
    rules = {}
    for name, out in job.intent.outputs.items():
        candidates = []
        if pointers.get(name):
            candidates.append(rule_from_pointer(job, final, out, str(pointers[name])))
        candidates.append(out.extract)
        for rule in filter(None, candidates):
            if outputs.extract(rule, final.view, final.elements, job.params) not in (None, ""):
                rules[name] = rule
                break
        else:
            if out.extract:
                rules[name] = out.extract          # nothing on screen now (e.g. no rows); keep the known place
    return rules


def _finish(job, steps, final, ev):
    """GPT-4o says done: verify, derive output rules, save the draft capability, return outputs."""
    check_screen(job, final)
    check = generalize_check(ev.get("verify"), job, final)
    for part in [k for k in check if k != "title"]:      # keep only what is really on screen
        if not verify({part: check[part]}, final, job.params):
            job.run.log(f"  checkpoint: dropped {part} {check[part]!r} - not a line on the final screen")
            del check[part]
    if not verify(check, final, job.params):
        raise Stuck(f"task not verified on screen: {shown(json.dumps(check), job.params)}",
                    code="checkpoint_failed", expected=json.dumps(check), observed=final.title)
    rules = derive_extracts(job, final, ev.get("outputs") or {})
    cap = capability.build(job.profile, job.intent, steps, final.skeleton, check, rules, job.run.dir,
                           settings.MODEL, job.request)
    job.cap = cap
    values = read_outputs(job, final, capability.outputs_dict(cap))
    check_result(job, values)
    capability.save(cap, job.run.log)
    capability.export_schema()
    return values


def _refusal(job, steps, event, el):
    """Why a proposed step must not be done (allow-list, personal data, flow rules), or None."""
    reason = policy.check_action(job.intent, event, el)
    literal = event.get("text", event.get("value"))
    if not reason and literal and "{" not in literal and job.vault.leaks({"v": literal}):
        reason = "typing personal data that is not one of the inputs"
    row_by = event.get("target", {}).get("match")
    if not reason and row_by and job.intent.types and not searched_for(steps, row_by.values()):
        reason = (f"not allowed yet: first type {', '.join(row_by.values())} into the search field and "
                  f"click its search button; only then open the row (on other records the row is not "
                  f"visible without a search)")
    return reason


def record(job, prefix=None, hint=None):
    """GPT-4o drives, one event per turn, on masked screens. Saves a draft capability; returns outputs."""
    steps = list(prefix or [])
    history = [{"event": describe_step(s), "result": "ok (replayed)"} for s in steps]
    bad = calls = 0
    while True:
        try:
            job.step_id = f"s{len(steps) + 1}"
            if calls >= settings.MAX_STEPS:
                calls = 0
                raise Stuck(f"no result after {settings.MAX_STEPS} GPT-4o steps", code="max_steps")
            screen = observe(job)
            cond = check_screen(job, screen)
            if cond:
                if cond.code == "busy":
                    job.surface.wait(0.5)
                    continue
                dismiss(job, screen, cond)                    # transient: dismiss, GPT-4o tries again
                history.append({"event": f"the app said '{cond.title}' (temporary) - dismissed"})
                job.recoveries.append(f"dismissed '{cond.title}' during discovery")
                continue
            if history and "result" not in history[-1]:
                history[-1]["result"] = f"screen now: '{screen.title}'"

            prompt = {"task": job.request, "intent": job.intent.name, "allowed": policy.describe(job.intent),
                      "parameters": job.param_tokens,
                      "outputs_wanted": {o.name: f"{o.type}: {o.description}" for o in job.intent.outputs.values()},
                      "history": job.vault.scrub(history[-10:]), "map": map_context(job, screen),
                      "screen": job.vault.redact_view(screen.view, job.params)}
            if hint:
                prompt["previously_recorded_remaining_steps (UI has changed, use as hint only)"] = \
                    job.vault.scrub(hint)
            reply = llm_json(STEP_SYSTEM, prompt, job.vault, job.run)
            calls += 1
            ev = reply.get("event") or {}
            job.run.log(f"GPT-4o: {reply.get('thought', '')} -> {json.dumps(ev, ensure_ascii=False)}")
            job.run.event("decision", step=job.step_id, thought=reply.get("thought"), proposed=ev)

            if ev.get("type") == "ask":
                param = ev.get("param") or "value"
                answer = job.ask(hooks.ask_text, ev.get("question", param))
                if not answer:
                    raise Stop(f"no value given for '{param}'", code="cancelled")
                job.params[param] = answer
                job.param_tokens[param] = job.vault.token(answer, "TEXT", strict=True)
                history.append({"event": f"asked user for {param}", "result": "answered"})
                job.surface.refocus(screen)
                continue
            if ev.get("type") == "fail":
                raise Stuck(f"GPT-4o could not go on: {job.vault.rehydrate(ev.get('reason', ''))}", code="dead_end")
            if ev.get("type") == "done":
                return _finish(job, steps, observe(job), ev)

            el = None
            if "target" in ev:
                el = next((e for e in screen.elements                  # GPT-4o saw masked ids
                           if ev["target"] in (e["id"], job.vault.mask_id(e["id"]))), None)
                if el is None:
                    bad += 1
                    history.append({"event": describe(ev, {"id": ev["target"]}),
                                    "result": "REJECTED: that id is not on this screen"})
                    if bad >= 3:
                        raise Stuck("GPT-4o keeps choosing elements that do not exist", code="dead_end")
                    continue
            event = {k: v for k, v in ev.items() if k in ("type", "text", "value", "keys", "seconds")}
            for k in ("text", "value"):
                if k in event:
                    event[k] = parameterize(job.vault.rehydrate(event[k]), job.params)
            if el is not None:
                event["target"] = to_semantic(el, screen, job.params)
                if el["kind"] == "table_row":            # several rows fit -> the user picks
                    el = resolve(event["target"], screen, job)
            reason = _refusal(job, steps, event, el)
            if reason:
                bad += 1
                history.append({"event": describe(event, event.get("target")), "result": f"REFUSED: {reason}"})
                job.run.log(f"  refused: {reason}")
                job.run.event("refused", step=job.step_id, reason=reason)
                if bad >= 3:
                    raise Stuck("GPT-4o keeps proposing actions that are not permitted", code="not_permitted")
                continue
            if el is not None and len(steps) >= 2 and all(s.get("event") == event for s in steps[-2:]):
                raise Stuck(f"GPT-4o repeated the same step 3 times: {describe(event, event.get('target'))}",
                            code="dead_end")

            if settings.CONFIRM_LLM_STEPS:
                if not job.ask(hooks.ask_yes_no, f"Step {len(steps) + 1}: {shown(describe(event, event.get('target')), job.params)}"
                               f"\n\nGPT-4o: {job.vault.rehydrate(reply.get('thought', ''))}\n\nRun this step?"):
                    raise Stop("step rejected by user", code="user_rejected")
                job.surface.refocus(screen)

            act(job, event, el, screen)
            steps.append({"screen": screen.skeleton, "event": event})
            history.append({"event": describe(event, event.get("target")), "expect": reply.get("expect")})
        except Stuck as s:
            if not job.interactive:
                raise
            job.run.log(f"STUCK - {s.message}")
            block = handoff(job, s.message, s.code)
            if block:
                steps.append(block)
            did = "; ".join(describe_step(x) for x in block["steps"]) if block else "their part (not recorded)"
            history.append({"event": "a person took over", "result": f"person did: {did}"})
            bad = 0
            front_again(job)
