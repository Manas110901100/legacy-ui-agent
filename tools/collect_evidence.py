"""
collect_evidence.py - copy runs into the repo's /evidence folder, with an index.

    python tools/collect_evidence.py                        pick the latest run of each kind
    python tools/collect_evidence.py discovery=runs/<id> replay=runs/<id> ...

Kinds picked automatically from result.json: discovery, replay (success), not_found
(business outcome), transient (a replay with recoveries), handoff (a run with an intervention),
bad_input. Only the masked evidence is copied (logs, events, results, intervention records, masked
LLM payloads and screens, blurred screenshots), and every text file is checked for personal data
before it is written - the copy stops if anything looks like an e-mail, phone or account number.
"""
import json
import re
import shutil
import sys
from pathlib import Path

AGENT = Path(__file__).resolve().parents[1]
EVIDENCE = AGENT / "evidence"
KEEP = ("run.log", "events.jsonl", "result.json")
PII = re.compile(r"[\w.+-]+@[\w-]+\.\w+|\b\d{3}-\d{3}-\d{4}\b|\bACC\d{6}\b")


def kind_of(result):
    if result.get("mode") == "discovery" and result.get("status") == "success":
        return "discovery"
    if result.get("status") == "business_outcome" and (result.get("outcome") or {}).get("code") == "record_not_found":
        return "not_found"
    if (result.get("failure") or {}).get("code") == "invalid_input":
        return "bad_input"
    if result.get("interventions"):
        return "handoff"
    if result.get("mode") == "replay" and result.get("status") == "success":
        return "transient" if result.get("recoveries") else "replay"
    return None


def latest_runs():
    found = {}
    for run in sorted((AGENT / "runs").iterdir()):
        res = run / "result.json"
        if res.exists():
            kind = kind_of(json.loads(res.read_text(encoding="utf-8")))
            if kind:
                found[kind] = run                         # later runs win
    return found


def copy_run(kind, run):
    dst = EVIDENCE / kind
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True)
    for f in sorted(run.iterdir()):
        keep = f.name in KEEP or f.name.startswith(("intervention_", "llm_")) or f.name.endswith(
            ("_screen.png", "_screen.json"))
        if not keep:
            continue
        if f.suffix in (".log", ".json", ".jsonl"):
            hits = PII.findall(f.read_text(encoding="utf-8"))
            if hits:
                sys.exit(f"STOP: {f} contains what looks like personal data ({hits[:3]}) - not copied")
        shutil.copy(f, dst / f.name)
    return dst


def main():
    runs = dict(arg.split("=", 1) for arg in sys.argv[1:]) if len(sys.argv) > 1 else latest_runs()
    rows = []
    for kind, run in sorted(runs.items()):
        run = (AGENT / run) if not Path(run).is_absolute() else Path(run)
        dst = copy_run(kind, run)
        res = json.loads((run / "result.json").read_text(encoding="utf-8"))
        what = res.get("outcome") or res.get("failure") or {}
        rows.append(f"| {kind} | `{run.name}` | {res.get('capability')} v{res.get('version')} | {res.get('status')} "
                    f"| {what.get('code', '')} | {len(res.get('recoveries', []))} | {res.get('llm_calls', 0)} |")
        print(f"{kind:10} <- {run.name}  ({len(list(dst.iterdir()))} files)")
    caps = EVIDENCE / "capabilities"
    caps.mkdir(parents=True, exist_ok=True)
    for f in (AGENT / "capabilities").glob("*.json"):
        shutil.copy(f, caps / f.name)
    (EVIDENCE / "INDEX.md").write_text(
        "# Evidence\n\nEach folder is one run, copied from `runs/`. Everything in it is masked: "
        "customer values appear as tokens (`<NAME_1>`, `<ACCT_2>` ...), screenshots are blurred where data is shown, "
        "`llm_NN.json` is exactly what GPT-4o received.\n\n"
        "| kind | run | capability | status | outcome / failure | recoveries | LLM calls |\n|---|---|---|---|---|---|---|\n"
        + "\n".join(rows) + "\n\n`capabilities/` holds the saved capability artifacts and `schema.json`.\n",
        encoding="utf-8")
    print(f"index -> {EVIDENCE / 'INDEX.md'}")


if __name__ == "__main__":
    main()
