"""
cli.py - python -m cua ...

  python -m cua "what is the balance of ACC100005"            goal: discovery, or replay if learned
  python -m cua --capability bankapp.account_details --input account=ACC100005 --json
                                                              production path: typed inputs, no model
  python -m cua --list                                        capabilities, contracts, reliability
  python -m cua --approve bankapp.account_details             review gate for unattended use
  python -m cua --map                                         learned windows, moves and their reliability
  python -m cua --explore                                     map the app ahead of time (safe moves only)
  python -m cua --panel                                       operator panel with the Agent/Human switch
"""
import argparse
import sys

from cua import settings


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m cua", description="Computer-use agent for legacy desktop apps")
    ap.add_argument("goal", nargs="?", help="goal in plain language (discovery, or replay if already learned)")
    ap.add_argument("--app", default="bankapp", help="app profile in apps/<app>.json")
    ap.add_argument("--capability", help="invoke a saved capability by id (no model call)")
    ap.add_argument("--input", action="append", default=[], metavar="NAME=VALUE", help="capability input")
    ap.add_argument("--json", action="store_true", help="print the RunResult as JSON")
    ap.add_argument("--interactive", action="store_true", help="--capability: a person may be asked / take over")
    ap.add_argument("--allow-draft", action="store_true", help="--capability: run a draft capability")
    ap.add_argument("--confirm-write", action="store_true", help="--capability: allow a capability that changes data")
    ap.add_argument("--approve", metavar="CAPABILITY", help="approve a reviewed capability for unattended use")
    ap.add_argument("--force", action="store_true", help="--approve: even before it proved reliable (recorded)")
    ap.add_argument("--list", action="store_true", help="list capabilities")
    ap.add_argument("--map", action="store_true", help="show the learned map of the app")
    ap.add_argument("--explore", action="store_true", help="map the app ahead of time (safe moves only)")
    ap.add_argument("--learn", action="store_true", help="rebuild reliability records from the results in runs/")
    ap.add_argument("--panel", action="store_true", help="start the operator panel")
    ap.add_argument("--relearn", action="store_true", help="when lost, let the model re-learn")
    args = ap.parse_args(argv)

    if args.panel:
        from cua.operator import panel
        return panel.main()
    if args.map:
        from cua.learning.screenmap import ScreenMap
        m = ScreenMap(settings.MAPS / f"{args.app}.json")
        print("\n".join(m.tree_lines()) or "The map is empty.")
        if m.move_lines():
            print("\nlearned moves:\n  " + "\n  ".join(m.move_lines()))
        return
    if args.learn:
        from cua.artifact import capability
        from cua.learning.reliability import CapabilityStats
        for cap in capability.catalog():
            stats = CapabilityStats.rebuild(cap.id)
            print(f"{cap.id}: {stats.summary()}")
            for (code, step), n in sorted(stats.failures().items(), key=lambda kv: -kv[1]):
                print(f"    {n} x {code}" + (f" at {step}" if step else ""))
        return
    from cua.engine import runner                    # the engine loads only when it is needed
    if args.explore:
        runner.explore(args.app)
        return
    if args.approve:
        ok, why = runner.approve(args.approve, force=args.force)
        print(("approved - " if ok else "") + why)
        sys.exit(0 if ok else 1)
    if args.capability:
        inputs = dict(kv.split("=", 1) for kv in args.input)
        res = runner.invoke(args.capability, inputs, interactive=args.interactive, allow_draft=args.allow_draft,
                            confirm_write=args.confirm_write)
        print(res.model_dump_json(indent=1) if args.json else runner.message_for(res))
        sys.exit(0 if res.status in ("success", "business_outcome") else 1)
    if args.list or not args.goal:
        from cua.artifact import capability
        from cua.learning.reliability import CapabilityStats
        for cap in capability.catalog():
            ins = ", ".join(f"{k}: {v.type}" for k, v in cap.inputs.items())
            outs = ", ".join(f"{k}: {v.type}" for k, v in cap.outputs.items())
            people = sum(1 for s in cap.steps if s.kind == "human")
            print(f"{cap.id}  v{cap.version}  [{cap.status}]  {cap.mode}  ({ins}) -> ({outs})  {len(cap.steps)} steps"
                  + (f", {people} by a person" if people else "") + f"\n    {CapabilityStats(cap.id).summary()}")
        return
    res = runner.run_goal(args.goal, app=args.app, relearn=args.relearn)
    if args.json:
        print(res.model_dump_json(indent=1))
    sys.exit(0 if res.status in ("success", "business_outcome") else 1)
