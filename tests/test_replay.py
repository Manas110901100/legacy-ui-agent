"""The replay engine end to end on a scripted BankAPP (tests/fakes.py): outputs, business outcomes,
recoverable conditions, hard failures and escalation - and no LLM, no customer data at rest."""
import json
import time
from types import SimpleNamespace

from cua.artifact import capability
from cua.engine import hooks, llm, runner
from cua.engine.job import Job, Run
from cua.safety import pii, policy
from fakes import FakeBank

PROFILE = policy.load_profile("bankapp")
INTENT = PROFILE.intents["account_details"]
RAW_PII = ("Sarah", "sarah.", "@example.com", "555-765-9928", "12,613.28", "12613.28")


def details_capability(bank=None, find_label="Find"):
    bank = bank or FakeBank()
    main = bank.capture()
    steps = [
        {"screen": main.skeleton, "event": {"type": "type", "text": "{account}",
                                            "target": {"kind": "input", "key": "Account No / Name"}}},
        {"screen": main.skeleton, "event": {"type": "click", "target": {"kind": "button", "key": find_label}}},
        {"screen": main.skeleton, "event": {"type": "double_click", "target": {
            "kind": "table_row", "match": {"Customer Name": "{account}"}, "contains": True}}},
    ]
    bank.search = "ACC100002"
    bank.act({"type": "click"}, {"key": "Find"}, main)
    bank.act({"type": "double_click"}, {"kind": "table_row", "values": ["ACC100002"]}, main)
    final = bank.capture()
    extracts = {o.name: o.extract for o in INTENT.outputs.values()}
    return capability.build(PROFILE, INTENT, steps, final.skeleton, {"title": "Account Details - *"}, extracts,
                            None, "gpt-4o", "details of <ACCT_1>")


def replay(bank, account, interactive=False, cap=None):
    vault = pii.Vault()
    params, errors = runner.validate_inputs(INTENT, {"account": account})
    assert not errors
    vault.token(params["account"], "ACCOUNT", strict=True)
    run = Run("test", vault)
    job = Job(bank, PROFILE, INTENT, params, vault, run, "test", interactive=interactive,
                    cap=cap or details_capability())
    return runner.execute_job(job, "replay"), run


def evidence_text(run):
    return "\n".join(p.read_text(encoding="utf-8") for p in run.dir.iterdir() if p.suffix in (".log", ".json", ".jsonl"))


def test_success_returns_typed_outputs_without_llm(workspace):
    res, run = replay(FakeBank(), "ACC100002")
    assert res.status == "success", res.failure
    assert res.outputs["account_no"] == "ACC100002"
    assert res.outputs["customer"] == "Sarah Miller"
    assert res.outputs["balance"] == "12613.28"
    assert len(res.outputs["transactions"]) == 2
    assert res.llm_calls == 0 and not list(run.dir.glob("llm_*.json"))


def test_evidence_has_no_customer_data(workspace):
    res, run = replay(FakeBank(), "ACC100002")
    text = evidence_text(run)
    assert res.status == "success"
    assert not [p for p in RAW_PII if p in text], [p for p in RAW_PII if p in text]
    assert (run.dir / "events.jsonl").exists() and (run.dir / "result.json").exists()


def test_name_learned_row_step_also_works_with_an_account_number(workspace):
    res, _ = replay(FakeBank(), "ACC100024")
    assert res.status == "success" and res.outputs["customer"] == "Sarah Jones"


def test_record_not_found_is_a_business_outcome(workspace):
    res, _ = replay(FakeBank(), "ACC999999")
    assert res.status == "business_outcome"
    assert res.outcome["code"] == "record_not_found"
    assert res.failure is None


def test_transient_error_is_retried(workspace):
    res, _ = replay(FakeBank(transient_finds=1), "ACC100002")
    assert res.status == "success", res.failure
    assert res.recoveries and "retried step s2" in res.recoveries[0]


def test_too_many_transient_errors_fail_with_detail(workspace):
    res, _ = replay(FakeBank(transient_finds=5), "ACC100002")
    assert res.status == "failed" and res.failure.code == "transient"
    assert res.failure.step == "s3" and "Application Error" in res.failure.observed


def test_missing_control_is_a_hard_failure_with_expected_and_observed(workspace):
    bank = FakeBank()
    res, _ = replay(bank, "ACC100002", cap=details_capability(find_label="Search"))
    assert res.status == "failed"
    f = res.failure
    assert (f.code, f.step) == ("target_missing", "s2")
    assert "Search" in f.expected and "Find" in f.observed


def test_several_matches_unattended_is_a_business_outcome(workspace):
    res, _ = replay(FakeBank(), "sarah")
    assert res.status == "business_outcome" and res.outcome["code"] == "ambiguous_match"
    assert "Sarah" not in res.outcome["message"]


def test_several_matches_interactive_the_user_picks(workspace, monkeypatch):
    monkeypatch.setattr(hooks, "choose", lambda question, options: 1)
    res, _ = replay(FakeBank(), "sarah", interactive=True)
    assert res.status == "success" and res.outputs["account_no"] == "ACC100024"


def test_time_spent_waiting_for_a_person_does_not_time_the_run_out(workspace, monkeypatch):
    clock = [time.monotonic()]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])

    def slow_pick(question, options):                  # the person takes 20 minutes to choose
        clock[0] += 1200
        return 1
    monkeypatch.setattr(hooks, "choose", slow_pick)
    res, _ = replay(FakeBank(), "sarah", interactive=True)
    assert res.status == "success" and res.outputs["account_no"] == "ACC100024"


def test_session_expired_unattended_escalates_with_an_intervention_request(workspace):
    res, run = replay(FakeBank(session_expired=True), "ACC100002")
    assert res.status == "escalated" and res.failure.code == "session_expired"
    assert res.interventions
    rec = json.loads((run.dir / res.interventions[0]).read_text(encoding="utf-8"))
    assert rec["code"] == "session_expired" and rec["step"] == "s3"


def test_leftover_window_is_closed_before_the_run(workspace):
    bank = FakeBank()
    main = bank.capture()
    bank.search = "ACC100007"
    bank.act({"type": "click"}, {"key": "Find"}, main)
    bank.act({"type": "double_click"}, {"kind": "table_row", "values": ["ACC100007"]}, bank.capture())
    assert bank.stack[0] == "details"                           # someone left Account Details open
    res, _ = replay(bank, "ACC100002")
    assert res.status == "success" and res.outputs["account_no"] == "ACC100002"


def open_account(bank, acct):
    bank.stack = ["main"]
    bank.search = acct
    bank.act({"type": "click"}, {"key": "Find"}, bank.capture())
    bank.act({"type": "double_click"}, {"kind": "table_row", "values": [acct]}, bank.capture())


def test_person_opening_the_wrong_record_is_caught_and_asked_again(workspace, monkeypatch):
    bank, asked = FakeBank(), []

    def take_over(message):                       # 1st time the person opens the wrong account
        asked.append(message)
        open_account(bank, "ACC100030" if len(asked) == 1 else "ACC100002")
        return True
    monkeypatch.setattr(hooks, "take_over", take_over)
    hooks.TAKEOVER.set()                          # the person flips the switch right away
    try:
        res, run = replay(bank, "ACC100002", interactive=True)
    finally:
        hooks.TAKEOVER.clear()
    assert res.status == "success" and res.outputs["account_no"] == "ACC100002"
    assert len(asked) == 2 and "another record" in asked[1]
    assert len(res.interventions) == 2


def test_wrong_record_unattended_is_a_hard_failure(workspace):
    bank = FakeBank()
    cap = details_capability()
    cap.steps = [cap.steps[2].model_copy(update={"target": capability.Target(role="table_row", row=1)})]
    res, _ = replay(bank, "ACC100024", cap=cap)   # "first row" lands on ACC100002
    assert res.status == "failed" and res.failure.code == "result_mismatch"
    assert "ACC100024" not in (res.failure.observed or "")


def test_invoke_rejects_bad_input_and_drafts_before_touching_the_app(workspace):
    cap = details_capability()
    capability.save(cap, log=lambda m: None)
    bad = runner.invoke("bankapp.account_details", {"account": "!!"})
    assert bad.status == "failed" and bad.failure.code == "invalid_input"
    draft = runner.invoke("bankapp.account_details", {"account": "ACC100002"})
    assert draft.status == "refused" and draft.failure.code == "not_approved"


# ---------------------------------------------------------------- discovery with a scripted "GPT-4o"

class ScriptedLLM:
    """Stands in for the OpenAI client. Checks every payload is masked, answers from a script."""
    def __init__(self, replies):
        self.replies, self.payloads = list(replies), []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, model, temperature, response_format, messages):
        payload = messages[1]["content"]
        self.payloads.append(payload)
        assert "Sarah" not in payload and "@example.com" not in payload and "ACC1000" not in payload
        content = json.dumps(self.replies.pop(0))
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


def test_discovery_learns_a_capability_from_masked_screens(workspace, monkeypatch):
    scripted = ScriptedLLM([
        {"thought": "search", "event": {"type": "type", "target": "input_1", "text": "{account}"}},
        {"thought": "find", "event": {"type": "click", "target": "btn_find"}},
        {"thought": "open", "event": {"type": "double_click", "target": "row_1"}},
        {"thought": "shown", "event": {"type": "done", "verify": {}, "outputs": {}}},
    ])
    monkeypatch.setattr(llm, "client", scripted)
    vault = pii.Vault()
    params = {"account": "Sarah Jones"}
    vault.token("Sarah Jones", "ACCOUNT", strict=True)
    job = Job(FakeBank(), PROFILE, INTENT, params, vault, Run("discovery", vault),
                    vault.redact_request("show Sarah Jones transactions"), interactive=False)
    res = runner.execute_job(job, "discovery")
    assert res.status == "success", res.failure
    assert res.outputs["account_no"] == "ACC100024" and res.llm_calls == 4
    cap = capability.load("bankapp.account_details")
    assert cap.status == "draft" and [s.action for s in cap.steps] == ["type", "click", "double_click"]
    row = cap.steps[2].target                                  # found by the parameter, not the customer
    assert (row.role, row.column, row.equals or row.contains) == ("table_row", "Customer Name", "{account}")
    assert cap.checkpoint.window == "Account Details - *"
    assert "Sarah" not in path_text(capability.path_for(cap.id))


def path_text(p):
    return p.read_text(encoding="utf-8")
