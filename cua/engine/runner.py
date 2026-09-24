"""
runner.py - the entry points: a goal or a capability in, a RunResult out.

    run_goal(goal)                 plain language (panel / CLI): route with the model on the masked goal,
                                   ask for missing values locally, then replay the saved capability or
                                   discover it. Interactive: a person can be asked and can take over.
    invoke(cap_id, inputs)         the production path: typed inputs, no model call at all.
    approve(cap_id)                the review gate: a capability runs unattended only once approved,
                                   and only after it proved reliable (cua/learning).
    explore(app)                   map the app ahead of time with safe moves only.

Every finished run is scored (learning/rewards.py) and kept in the capability's record; a capability
that stops being reliable goes back to draft.
"""
import json
import os
import sys
import time
from datetime import datetime

from cua import settings
from cua.artifact import capability, outcomes
from cua.artifact.outcomes import Failure, RunResult
from cua.engine import hooks
from cua.engine.checks import answer_text
from cua.engine.discovery import record
from cua.engine.handoff import open_intervention
from cua.engine.job import Job, Run
from cua.engine.llm import llm_json, router_system
from cua.engine.navigate import go_home
from cua.engine.observe import act, observe, resolve
from cua.engine.replay import replay
from cua.engine.text import describe, norm, placeholders
from cua.errors import Outcome, Stop, Stuck
from cua.learning import screenmap
from cua.learning.reliability import CapabilityStats
from cua.safety import pii, policy

REFUSAL = ("I can create new accounts, and find, list or show account details. "
           "Other changes (edit, deposit, withdraw, transfer, close) are not permitted.")


# ---------------------------------------------------------------- one job -> RunResult

def execute_job(job, mode):
    """Run discovery or replay and turn how it ended into a RunResult; score it."""
    t0, surf = time.monotonic(), job.surface
    result = dict(run_id=job.run.dir.name, capability=job.cap.id if job.cap else f"{job.profile.app}.{job.intent.name}",
                  version=job.cap.version if job.cap else None, mode=mode)
    try:
        surf.front(surf.main, maximize=settings.MAXIMIZE_MAIN)
        surf.wait(0.8)
        go_home(job)
        values = replay(job.cap, job) if mode == "replay" else record(job)
        res = RunResult(**result, status="success", outputs=values, answer=answer_text(values))
    except Outcome as o:
        res = RunResult(**result, status="business_outcome", outcome={"code": o.code, "message": o.message})
    except Stuck as s:
        if outcomes.kind_of(s.code) in ("escalate", "operator"):
            open_intervention(job, s.message, s.code)
            res = RunResult(**result, status="escalated", failure=failure(job, s))
        else:
            res = RunResult(**result, status="failed", failure=failure(job, s))
    except Stop as s:
        res = RunResult(**result, status="cancelled" if s.code in settings.CANCEL_CODES else "failed",
                        failure=failure(job, s))
    except Exception as e:                                   # includes the pyautogui fail-safe
        code = "aborted" if type(e).__name__ == "FailSafeException" else "internal_error"
        res = RunResult(**result, status="cancelled" if code == "aborted" else "failed",
                        failure=Failure(code=code, message=str(e) or type(e).__name__, step=job.step_id,
                                        screenshot=job.run.last_screenshot))
    for name, out in job.intent.outputs.items():             # personal outputs never reach result.json
        if out.pii and isinstance(res.outputs.get(name), str) and res.outputs[name]:
            job.vault.token(res.outputs[name], out.pii, strict=True)
    res.recoveries, res.interventions = job.recoveries, job.interventions
    res.llm_calls, res.duration_s = job.run.llm_n, round(time.monotonic() - t0, 1)
    if job.cap and res.version is None:
        res.version = job.cap.version
    learn_from(job, res)
    job.run.log(f"RESULT {res.status}" + (f" ({res.outcome['code']})" if res.outcome else "")
                + (f" - {res.failure.code}: {res.failure.message}" if res.failure else "")
                + (f" | reliability {res.reliability:.0%}" if res.reliability is not None else ""))
    job.run.save_json("result.json", res.model_dump())
    job.run.event("result", status=res.status, outcome=res.outcome, reliability=res.reliability,
                  failure=res.failure and res.failure.model_dump())
    return res


def learn_from(job, res):
    """Reinforcement from the outcome: score the run into the capability's record; an approved
    capability that stopped being reliable goes back to draft for review."""
    if not res.capability or not capability.load(res.capability):
        return
    stats = CapabilityStats(res.capability)
    reward = stats.add(res)
    if reward is None:
        return
    res.reliability = stats.reliability()
    job.run.event("reward", reward=reward, reliability=res.reliability)
    cap = capability.load(res.capability)
    if cap.status == "approved" and stats.should_demote():
        cap.status = "draft"
        capability.save(cap, job.run.log, bump=False)
        job.run.log(f"'{cap.id}' is back to draft: reliability over its last {settings.DEMOTE_WINDOW} runs is "
                    f"{stats.reliability(settings.DEMOTE_WINDOW):.0%} (< {settings.DEMOTE_BELOW:.0%}) - needs review")


def failure(job, s):
    return Failure(code=s.code, message=s.message, step=job.step_id, expected=s.expected, observed=s.observed,
                   screenshot=job.run.last_screenshot)


def open_surface(profile):
    from cua.perception.surface import DesktopOcrSurface
    surf = DesktopOcrSurface(profile.window_title)
    return surf if surf.main else None


def message_for(res):
    if res.status == "success":
        return f"Done: {res.capability}.\n\n{res.answer}".strip()
    if res.status == "business_outcome":
        return f"Finished - {res.outcome['code'].replace('_', ' ')}: {res.outcome['message']}"
    if res.status == "refused" and not res.failure:
        return f"Not done - {REFUSAL}"
    f = res.failure
    return f"{res.status.upper()} - {f.code}: {f.message}" if f else res.status.upper()


# ---------------------------------------------------------------- inputs

def collect_params(intent, slots, vault, task):
    """Router slots (tokens) -> validated real values. Missing or invalid ones are asked for
    locally; the answers never reach the model. None = the user cancelled."""
    params = {}
    for slot in intent.slots:
        raw, problem = vault.rehydrate(slots.get(slot.name) or "").strip(), ""
        if raw and slot.kind and norm(raw) not in norm(task):   # not in the request = made up
            raw = ""
        while True:
            if raw:
                try:
                    params[slot.name] = slot.validate(raw)
                    break
                except ValueError as e:
                    problem = f"\n({e})"
            raw = hooks.ask_text(slot.question + problem)
            if not raw:
                return None
    return params


def validate_inputs(intent, inputs):
    """Typed inputs of a direct invocation -> (params, errors)."""
    params, errors = {}, []
    for slot in intent.slots:
        if slot.name not in inputs:
            errors.append(f"{slot.name}: missing ({slot.description})")
            continue
        try:
            params[slot.name] = slot.validate(inputs[slot.name])
        except ValueError as e:
            errors.append(f"{slot.name}: {e}")
    errors += [f"{k}: not an input of this capability" for k in inputs if k not in params and
               k not in {s.name for s in intent.slots}]
    return params, errors


def summary(intent, params):
    lines = [f"  {s.name.replace('_', ' ')}: {params[s.name]}" for s in intent.slots]
    return f"{intent.about} With:\n" + "\n".join(lines) + "\n\nGo ahead?"


# ---------------------------------------------------------------- entry points

def run_goal(goal, app="bankapp", relearn=False, confirm=True):
    """Natural-language goal: route (masked), then replay the saved capability or discover it."""
    hooks.ABORT.clear()
    profile = policy.load_profile(app)
    surf = open_surface(profile)
    vault = pii.Vault()
    request = vault.redact_request(goal)
    if surf is None:
        res = RunResult(run_id="-", status="failed", failure=Failure(code="app_not_running",
                        message=f"'{profile.window_title}' window not found - start the application first."))
        hooks.notify(message_for(res))
        return res
    route_payload = {"request": request}
    try:
        route = llm_json(router_system(profile), route_payload, vault)
    except Stop as s:
        res = RunResult(run_id="-", status="failed", failure=Failure(code=s.code, message=s.message))
        hooks.notify(message_for(res))
        return res
    intent = profile.intents.get(route.get("intent"))
    run = Run(intent.name if intent else "refused", vault)
    run.save_llm(route_payload)
    run.log(f"request as GPT-4o saw it: {request}\nroute: {json.dumps(route, ensure_ascii=False)}")
    if intent is None:
        res = RunResult(run_id=run.dir.name, status="refused", llm_calls=1,
                        outcome={"code": route.get("intent", "unknown"), "message": REFUSAL})
        run.save_json("result.json", res.model_dump())
        hooks.notify(message_for(res))
        return res

    params = collect_params(intent, route.get("slots") or {}, vault, goal)
    cap = capability.load(f"{profile.app}.{intent.name}")
    job = Job(surf, profile, intent, params or {}, vault, run, request, relearn, interactive=True, cap=cap)
    if params is None:
        run.log("cancelled: a required value was not given")
        return RunResult(run_id=run.dir.name, status="cancelled", llm_calls=1)
    problems = policy.check_steps(intent, cap.engine_steps()) if cap else []
    if problems:
        hooks.notify(f"The saved '{cap.id}' capability contains steps that are not permitted:\n- "
                     + "\n- ".join(problems))
        return RunResult(run_id=run.dir.name, status="failed",
                         failure=Failure(code="not_permitted", message="; ".join(problems)))
    for p in sorted(placeholders(cap.engine_steps()) - set(params)) if cap else []:
        params[p] = job.ask(hooks.ask_text, f"Value for '{p}'")    # asked for by GPT-4o when it learned the task
        if not params[p]:
            return RunResult(run_id=run.dir.name, status="cancelled")
        vault.token(params[p], "TEXT", strict=True)
    if intent.mode == "write":
        if not job.ask(hooks.ask_yes_no, summary(intent, params)):
            return RunResult(run_id=run.dir.name, status="cancelled")
    elif confirm and not job.ask(hooks.ask_yes_no, f"{'Replay' if cap else 'Learn'} '{intent.name}'?"):
        return RunResult(run_id=run.dir.name, status="cancelled")
    res = execute_job(job, "replay" if cap else "discovery")
    hooks.notify(message_for(res) + f"\n\nLog: {run.dir}")
    return res


def invoke(cap_id, inputs, interactive=False, allow_draft=False, confirm_write=False):
    """Production path: a saved capability with typed inputs. No model is called at all."""
    hooks.ABORT.clear()
    cap = capability.load(cap_id)
    vault = pii.Vault()
    if cap is None:
        return RunResult(run_id="-", capability=cap_id, status="failed",
                         failure=Failure(code="capability_not_found", message=f"no capability '{cap_id}'"))
    profile = policy.load_profile(cap.app.app)
    intent = profile.intents[cap.name]
    run = Run(f"{cap.name}_replay", vault)
    base = dict(run_id=run.dir.name, capability=cap.id, version=cap.version, mode="replay")
    params, errors = validate_inputs(intent, inputs)
    for s in intent.slots:
        if s.kind and s.name in params:
            vault.token(params[s.name], s.kind, strict=True)
    run.log(f"invoke {cap.id} v{cap.version} [{cap.status}] with {json.dumps(inputs, ensure_ascii=False)}")
    res = None
    if errors:
        res = RunResult(**base, status="failed", failure=Failure(code="invalid_input", message="; ".join(errors)))
    elif cap.status != "approved" and not (interactive or allow_draft):
        res = RunResult(**base, status="refused", failure=Failure(
            code="not_approved", message="draft capability: review it and run --approve, or pass --allow-draft"))
    elif intent.mode == "write" and not interactive and not confirm_write:
        res = RunResult(**base, status="refused", failure=Failure(
            code="confirmation_required", message="this capability changes data: pass --confirm-write"))
    elif policy.check_steps(intent, cap.engine_steps()):
        res = RunResult(**base, status="refused", failure=Failure(
            code="not_permitted", message="; ".join(policy.check_steps(intent, cap.engine_steps()))))
    if res is None and (surf := open_surface(profile)) is None:
        res = RunResult(**base, status="failed", failure=Failure(
            code="app_not_running", message=f"'{profile.window_title}' window not found"))
    if res is not None:
        run.log(f"RESULT {res.status} - {res.failure.code}: {res.failure.message}")
        run.save_json("result.json", res.model_dump())
        return res
    request = f"invoke {cap.id} with {vault.scrub(json.dumps(inputs))}"
    job = Job(surf, profile, intent, params, vault, run, request, interactive=interactive, cap=cap)
    return execute_job(job, "replay")


def approve(cap_id, force=False, reviewer=None):
    """The review gate for unattended use: approved only after enough clean runs at a good
    reliability (settings.APPROVE_*), unless forced (recorded in the capability's provenance)."""
    cap = capability.load(cap_id)
    if cap is None:
        return False, f"no capability '{cap_id}'"
    ok, why = CapabilityStats(cap_id).can_approve()
    if not ok and not force:
        return False, f"not approved: {why} (replay it with --allow-draft or from the panel, or use --force)"
    cap.status = "approved"
    cap.provenance.approved_by = reviewer or os.environ.get("USERNAME", "reviewer")
    cap.provenance.approved_at = datetime.now().isoformat(timespec="seconds")
    if not ok:
        cap.provenance.approved_by += f" (forced: {why})"
    capability.save(cap, bump=False)
    return True, why


def explore(app="bankapp"):
    """Map the app ahead of time with safe moves only: open a window, look at it, close it again.
    Never types, never saves, never touches a denied button."""
    profile = policy.load_profile(app)
    surf = open_surface(profile)
    if surf is None:
        sys.exit(f"'{profile.window_title}' window not found - start the application first.")
    intent = policy.Intent("explore", "read", "map the application", [],
                           clicks=settings.EXPLORE_OPENERS + profile.close_buttons, rows=True, deny=profile.deny,
                           common=profile.busy)
    vault = pii.Vault()
    job = Job(surf, profile, intent, {}, vault, Run("explore", vault), "explore")
    surf.front(surf.main, maximize=settings.MAXIMIZE_MAIN)
    surf.wait(0.8)
    go_home(job)
    home = observe(job)
    targets = [{"kind": "button", "key": e["key"]} for e in home.elements if e["kind"] == "button"
               and any(policy.same_button(e["key"], o) for o in settings.EXPLORE_OPENERS)]
    if home.view.get("table", {}).get("rows"):
        targets.append({"kind": "table_row", "row": 1})
    for target in targets:
        event = {"type": "double_click" if target["kind"] == "table_row" else "click", "target": target}
        try:
            screen = observe(job)
            el = resolve(target, screen, job)
            if policy.check_action(intent, event, el):
                continue
            act(job, event, el, screen)
            after = observe(job)
            job.run.log(f"explore: {describe(event, target)} -> '{screenmap.mask_title(after.title)}'")
        except Stuck as s:
            job.run.log(f"explore: {describe(event, target)} -> {s.message}")
        go_home(job)
    job.map.save()
    print("\n".join(job.map.tree_lines()))
