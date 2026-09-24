"""Unit tests for the load-bearing pieces: masking, allow-list, outcome classification, the map,
the input guard, the capability schema and output extraction."""
import json
import tempfile
from pathlib import Path

from cua.artifact import capability, outcomes
from cua.artifact import outputs as extract
from cua.learning import screenmap
from cua.safety import guard, pii
from cua.safety import policy as intents
from fakes import FakeBank, build_screen

PROFILE = intents.load_profile("bankapp")


# ---------------------------------------------------------------- pii
def test_request_is_masked_default_deny():
    v = pii.Vault()
    r = v.redact_request("create account for mia lee, email mia@test.com, phone 555-000-1111, savings, deposit 250")
    assert "mia" not in r.lower().replace("<", "") or "<TEXT_" in r
    assert "@" not in r and "555" not in r and "250" not in r and "savings" in r


def test_request_verbs_reach_the_router_names_do_not():
    r = pii.Vault().redact_request("pull details of arjun")
    assert r.startswith("pull details of <TEXT_") and "arjun" not in r.lower()


def test_known_values_are_masked_inside_element_ids():
    v = pii.Vault()
    v.token("sarah jones", "ACCOUNT", strict=True)
    red = v.redact_view({"screen": "BankAPP", "buttons": [{"id": "btn_sarah_jones", "text": "'Sarah Jones'"},
                                                          {"id": "btn_show_all", "text": "Show All"}]})
    assert "sarah" not in json.dumps(red).lower()
    assert red["buttons"][1]["id"] == "btn_show_all"
    assert v.mask_id("btn_sarah_jones") == red["buttons"][0]["id"]     # how GPT-4o's pick is found again
    assert "ACC100005" not in v.mask_id("btn_acc100005").upper()
    assert v.leaks({"buttons": [{"id": "btn_sarah_jones"}]}) and v.leaks({"buttons": [{"id": "btn_acc100005"}]})


def test_view_is_masked_and_leak_check_catches_leftovers():
    v = pii.Vault()
    screen = FakeBank().capture()
    red = v.redact_view(screen.view, {"account": "sarah"})
    text = json.dumps(red)
    assert "Sarah" not in text and "@example.com" not in text and "ACC1000" not in text
    assert v.leaks(red) == []
    assert v.leaks({"x": "call 555-765-9928"}) != []
    assert [r.get("matches") for r in red["table"]["rows"][:3]] == [["account"]] * 3


def test_success_check_is_generalized():
    v = pii.Vault()
    assert v.generalize("Account ACC100052 opened for BO.", {"name": "BO"}) == "Account * opened for {name}."
    assert v.generalize("Transactions (5)", {}) == "Transactions (*)"


# ---------------------------------------------------------------- allow-list
def test_allow_list_comes_from_the_profile():
    ca, ad = PROFILE.intents["create_account"], PROFILE.intents["account_details"]
    click = {"type": "click"}
    assert intents.check_action(ca, click, {"kind": "button", "key": "Save"}) is None
    assert "changes data" in intents.check_action(ad, click, {"kind": "button", "key": "Close Acct"})
    assert intents.check_action(ad, click, {"kind": "button", "key": "Close"}) is None
    assert intents.check_action(ca, click, {"kind": "menu_item", "key": "Account"})
    assert intents.check_action(ca, {"type": "type"}, {"kind": "input", "key": "Opening deposit"}) is None
    assert intents.check_action(ca, {"type": "key", "keys": "delete"})


# ---------------------------------------------------------------- outcomes
def test_dialogs_are_classified():
    c = outcomes.classify(PROFILE, "Record Not Found", ["No accounts match 'x'."])
    assert (c.code, c.kind) == ("record_not_found", "business")
    assert outcomes.classify(PROFILE, "Temporarily Unavailable").kind == "recoverable"
    outage = ["Service unavailable after 3 attempts (The server did not respond in time.). Please try again later.",
              "Error ID: 7f3a"]
    assert outcomes.classify(PROFILE, "Application Error", outage).code == "transient"     # BankAPP's own wording
    assert outcomes.classify(PROFILE, "Application Error", ["Simulated unhandled exception"]).code == "app_error"
    assert outcomes.classify(PROFILE, "Session Expired").kind == "escalate"
    assert outcomes.classify(PROFILE, "Please Wait").code == "busy"
    assert outcomes.classify(PROFILE, "New Account", ["Customer name:"]) is None
    assert outcomes.policy("transient", "read") == "retry" and outcomes.policy("transient", "write") == "escalate"


# ---------------------------------------------------------------- map
def test_map_matches_windows_by_their_controls_and_finds_routes():
    m = screenmap.ScreenMap(Path(tempfile.mkdtemp()) / "m.json")
    bank = FakeBank()
    main = m.learn_screen(bank.capture().skeleton, "main")
    a = build_screen("Account Details - ACC100002", buttons=["Counter", "Close"], pairs=[("Balance", "1.00")])
    b = build_screen("Account Details - ACC100030", buttons=["Counter", "Close"], texts=["Transactions (0)"])
    d1, d2 = m.learn_screen(a.skeleton, "dialog", main), m.learn_screen(b.skeleton, "dialog", main)
    assert d1 == d2 and m.name(d1) == "Account Details - *"
    m.learn_edge(main, {"type": "click", "target": {"kind": "button", "key": "New"}}, "n9")
    m.nodes["n9"] = {"name": "New Account", "kind": "dialog", "parent": main, "seen": 1, "skeleton": {"elements": []}}
    route = m.path(d1, "n9")
    assert [screenmap.describe_action(x) for x, _ in route] == ["key esc", "click New"]
    m.nodes["n8"] = {"name": "BankAPP", "kind": "main", "parent": None, "seen": 50, "skeleton": {"elements": []}}
    assert m.root == "n8"


# ---------------------------------------------------------------- guard
def test_guard_rules():
    d = guard.decide
    assert d("click", True, 1, point_pid=1) == guard.PASS
    assert d("click", False, 1, point_pid=1) == guard.BLOCK
    assert d("click", False, 1, point_pid=2, in_toggle=True) == guard.TAKE_OVER
    assert d("click", False, 1, point_pid=3) == guard.PASS
    assert d("key", False, 1, fg_pid=1) == guard.BLOCK
    assert d("key", False, 1, fg_pid=1, ctrl_shift=True) == guard.PASS
    assert d("move", False, 1, point_pid=1) == guard.PASS


# ---------------------------------------------------------------- capability schema
def test_capability_round_trip_and_schema():
    from test_replay import details_capability
    cap = details_capability()
    again = capability.Capability.model_validate_json(cap.model_dump_json(by_alias=True))
    assert again.engine_steps() == cap.engine_steps()
    assert again.steps[2].target.contains == "{account}" and again.steps[2].on_many == "ask_user"
    schema = capability.Capability.model_json_schema(by_alias=True)
    assert {"inputs", "outputs", "steps", "checkpoint", "outcomes"} <= set(schema["properties"])


# ---------------------------------------------------------------- extraction
def test_outputs_are_read_by_position_not_by_parser_grouping():
    s = build_screen("Account Details - ACC100002", pairs=[("Balance", "12,613.28"), ("Status", "ACTIVE")],
                     rows_under=(["Date/Time", "Type", "Amount"], [["2025-09-11", "DEPOSIT", "4,022.90"]]),
                     texts=["Account ACC100052 opened for BO."])
    assert extract.label_value(s.elements, "Balance") == "12,613.28"
    assert extract.coerce("12,613.28", "money") == ("12613.28", None)
    assert extract.rows_below(s.elements, "Date/Time") == [["2025-09-11", "DEPOSIT", "4,022.90"]]
    assert extract.by_pattern(s.elements, "Account {value} opened for *", {}) == "ACC100052"
    assert extract.coerce("ACCIOOOO2", "account_no", r"ACC\d{6}") == ("ACC100002", None)
