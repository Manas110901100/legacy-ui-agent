"""The learning layer: rewards, reliability, most-reliable routes, learning from failed moves, map
hygiene (merging duplicates, never storing data), the approval gate and demotion."""
import tempfile
from pathlib import Path
from types import SimpleNamespace

from cua import settings
from cua.artifact import capability
from cua.engine import runner
from cua.learning import rewards, screenmap
from cua.learning.reliability import CapabilityStats, estimate
from cua.safety.pii import static_elements
from fakes import FakeBank, build_screen
from test_replay import details_capability, open_account, replay


def result(status="success", interventions=(), recoveries=(), seconds=10, code=None):
    failure = SimpleNamespace(code=code, step="s2") if code else None
    return SimpleNamespace(status=status, interventions=list(interventions), recoveries=list(recoveries),
                           duration_s=seconds, failure=failure, outcome=None, run_id="r", version=1, mode="replay")


def test_reward_and_reliability():
    assert rewards.reward(result()) == 1.0
    assert rewards.reward(result(status="business_outcome")) == 1.0          # "not found" is a right answer
    assert rewards.reward(result(interventions=["i1"], recoveries=["r1"])) == 0.7
    assert rewards.reward(result(status="failed", code="target_missing")) == 0.0
    assert rewards.reward(result(status="failed", code="invalid_input")) is None   # not the capability's fault
    assert estimate(0, 0)[0] == 0.5 and estimate(9, 1)[0] > 0.8 > estimate(1, 9)[0] * 4
    assert estimate(3, 0)[1] < estimate(30, 0)[1]                            # more evidence, tighter bound


def little_map():
    m = screenmap.ScreenMap(Path(tempfile.mkdtemp()) / "m.json")
    main = m.learn_screen(FakeBank().capture().skeleton, "main")
    details = m.learn_screen(build_screen("Account Details - ACC100002", buttons=["Counter", "Close"]).skeleton,
                             "dialog", main)
    return m, main, details


def test_a_move_that_keeps_failing_is_dropped_for_a_reliable_one():
    m, main, details = little_map()
    esc = {"type": "key", "keys": "esc"}
    close = {"type": "click", "target": {"kind": "button", "key": "Close"}}
    assert [screenmap.describe_action(a) for a, _ in m.path(details, main)] == ["key esc"]   # built-in guess
    for _ in range(3):
        m.learn_miss(details, esc, main)                 # Esc did not close it, three times
    assert m.path(details, main) is None                 # no longer trusted, nothing else known
    for _ in range(4):
        m.learn_edge(details, close, main)               # a person / the agent closed it with "Close"
    assert [screenmap.describe_action(a) for a, _ in m.path(details, main)] == ["click Close"]
    lines = "\n".join(m.move_lines())
    assert "key esc--> BankAPP - Core Banking System: 0 ok, 3 failed" in lines and "(no longer used)" in lines


def test_a_failing_move_is_learned_and_then_skipped(workspace):
    replay(FakeBank(), "ACC100002")                       # the map learns the main window
    logs = []
    for _ in range(4):
        bank = FakeBank(esc_closes=False)                 # like BankAPP's Account Details window
        open_account(bank, "ACC100007")                   # someone left it open
        res, run = replay(bank, "ACC100002")
        assert res.status == "success" and res.outputs["account_no"] == "ACC100002"   # closed it anyway
        logs.append((run.dir / "run.log").read_text(encoding="utf-8"))
    m = screenmap.ScreenMap(settings.MAPS / "bankapp.json")
    esc = next(e for e in m.edges if e["action"] == {"type": "key", "keys": "esc"})
    assert sum(esc["miss"].values()) == 3                 # tried three times, failed three times...
    assert all("--key esc-->" in log for log in logs[:3])
    assert "--key esc-->" not in logs[3] and "no reliable learned way back" in logs[3]   # ...then not again


def test_duplicate_windows_are_merged_and_data_is_never_stored():
    m, main, details = little_map()
    polluted = build_screen("Account Details - ACC100030", buttons=["Counter", "Close", "Find"]).skeleton
    m.nodes["n9"] = {"name": "Account Details - *", "kind": "dialog", "parent": main, "seen": 1,
                     "skeleton": {"window_title": "Account Details - *", "elements": polluted["elements"]}}
    m.learn_edge(main, {"type": "click", "target": {"kind": "button", "key": "Details"}}, "n9")
    m.save()
    again = screenmap.ScreenMap(m.file)
    assert again.merged == 1 and "n9" not in again.nodes
    assert [e["to"] for e in again.edges] == [{details: 1}]
    row = [{"kind": "menu_item", "key": k, "box": [10 * i, 50, 10 * i + 8, 60]}
           for i, k in enumerate(["Sarah Miller", "sarah.miller92@example.com", "555-765-9928"])]
    kept = static_elements(row + [{"kind": "button", "key": "New", "box": [0, 5, 40, 20]}])
    assert [e["key"] for e in kept] == ["New"]           # the whole misread row goes, names included


def test_approval_needs_a_proven_record_and_bad_runs_demote(workspace):
    cap = details_capability()
    capability.save(cap, log=lambda m: None)
    ok, why = runner.approve(cap.id)
    assert not ok and "clean run" in why                 # never ran: not for unattended use yet
    for acct in ("ACC100002", "ACC100024", "ACC100007"):
        res, _ = replay(FakeBank(), acct, cap=capability.load(cap.id))
        assert res.status == "success" and res.reliability is not None
    ok, why = runner.approve(cap.id)
    assert ok and capability.load(cap.id).status == "approved"
    broken = details_capability(find_label="Search")      # the app changed: the Find button is gone
    broken.status, broken.version = "approved", capability.load(cap.id).version
    capability.save(broken, log=lambda m: None, bump=False)
    for _ in range(3):
        replay(FakeBank(), "ACC100002", cap=capability.load(cap.id))
    assert capability.load(cap.id).status == "draft"      # stopped being reliable -> back to review
    stats = CapabilityStats(cap.id)
    assert stats.failures()[("target_missing", "s2")] == 3
