# Learning from outcomes (the reinforcement layer)

**Goal: more consistency.** A capability that worked once should keep working, get *more*
dependable with use, and say so when it stops. This layer turns every run into a signal and feeds it
back into the decisions the agent already makes.

## 1. Why reinforcement from outcomes, and not a trial-and-error policy

A classic RL agent learns a policy by exploring: trying actions, many thousands of times, and seeing
what pays off. That is the wrong tool here:

* **Exploration is not safe.** On a banking app "trying something" can open an account, move money or
  lock a customer. The allow-list forbids exactly what exploration needs.
* **It is sample-hungry.** Each step takes ~1.5 s against a live app; a learned policy would need
  weeks of interaction per app, per tenant.
* **The hard part is already solved differently.** The model discovers the flow once; the artifact
  makes it deterministic. What is left to learn is **which of the known ways is the reliable one**
  and **whether a capability can still be trusted** - a problem of estimating reliability from
  outcomes, which is safe to do online.

So the agent never explores. It chooses among the moves the allow-list permits, and learns from what
happened when it (or a person) made them.

## 2. The problem as an MDP

| | |
|---|---|
| **State** | the window the app is on (a node of the map) + the step of the capability |
| **Actions** | the moves the allow-list permits in that window (click a button, a key, open a row) |
| **Transition** | where a move led (learned: the map's edges) |
| **Reward** | how the run ended (below); each move is also scored arrived / did not |
| **Policy** | deterministic: the saved steps, plus the most reliable route when off track |

## 3. Reward (`cua/learning/rewards.py`)

| Outcome | Reward |
|---|---|
| success, or a legitimate business outcome (record not found is a correct answer) | 1.0 |
| per hand-over to a person | −0.25 |
| per recovery (dismissed error, retried step) | −0.05 |
| slow (over 60 s) | up to −0.2 |
| hard failure | 0 |
| refused / cancelled / invalid input / app not running | not scored (not the capability's fault) |

## 4. What is built

**Reliability (`cua/learning/reliability.py`).** Beta-Bernoulli: with `ok` successes and `fail`
failures, reliability is the mean of Beta(1 + ok, 1 + fail) - a thing tried once is not "100 %", and
a record that keeps failing sinks quickly. Rewards count as fractional successes.

**Moves on the map (`cua/learning/screenmap.py`).**
* Every move records where it led (`to`) **and where it failed to lead (`miss`)** - before this layer,
  failures were silently dropped: the map believed Esc closed *Account Details* 9 of 9 times while it
  had failed 7 times in the logs. Misses are counted from this version on (the committed map predates
  it, so it still shows only successes until new runs add misses).
* `path()` is a most-reliable-route search (cost = −log reliability); a move tried ≥ 3 times with
  reliability < 0.3 is **no longer used** - the agent closes the window directly or takes a learned
  *Close* move instead. (Test: `test_a_failing_move_is_learned_and_then_skipped`.)
* The model's context during discovery lists the known moves with their reliability.
* **Consolidation**: windows that are the same (title + controls; a window seen with a dialog on top
  counts) are merged on load, so the map does not grow noise; fingerprints never keep text that looks
  like data.

**Capabilities (`capabilities/<id>.stats.json`, runtime telemetry next to the reviewed artifact).**
* Each run's reward, status, code, failing step, recoveries, hand-overs, duration.
* `RunResult.reliability` returns it to the caller; `python -m cua --list` shows it.
* **Approval gate**: `--approve` needs ≥ 3 clean runs (no hand-over) and reliability ≥ 0.7
  (`--force` overrides and is recorded in the capability's provenance).
* **Demotion**: an approved capability whose last 10 runs fall below 0.6 goes back to `draft`
  ("needs review") - it degrades gracefully instead of failing silently in production.
* `failures()` says *where* it breaks (`target_missing at s2 × 3`) - the step to look at first.

**Offline learning from logs.** `python -m cua --learn` rebuilds every capability's record from the
results in `runs/` (oldest first). On this project's real runs: `bankapp.account_details` -
reliability 0.76 over 19 runs, 14 clean.

**Learning from people.** A person's moves during a hand-over become map edges marked `by: person`;
approved steps become human steps in the capability (handed to a person again next time).

## 5. Next steps

1. **Promotion of demonstrations (imitation).** When the same human step has been done the same way
   (same recorded steps) by people N times, propose it as automated steps in a new draft - a reviewer
   approves; the approval gate then requires clean replays.
2. **Bandits over alternatives.** Keep several ways of finding a control (label, neighbour label,
   position within a table) and of recovering (wait longer, dismiss + retry, re-open the window);
   choose by Thompson sampling on their records, within the allow-list.
3. **Offline RL from `events.jsonl`.** Every run's structured events are a trajectory (state = window
   + step, action, next window, reward); fitted Q-evaluation on those logs can compare recovery
   strategies and routes without touching the app.
4. **Per-tenant priors.** Tenants running the same vendor product share the product's records as a
   prior; a tenant's own runs update it. A tenant whose windows stop matching the product's
   fingerprints gets its capabilities demoted to draft for that tenant only.
5. **A simulator.** The `FakeSurface` used by the tests (scripted screens and transitions) or a
   sandbox tenant is where any exploratory learning would happen - never production.

## 6. Guard rails of the learning layer

* It never explores and never widens the allow-list: it only ranks moves that are already permitted.
* It never changes a flow on its own: changed flows (a person's steps) go back to `draft`.
* It never stores data: map fingerprints and reliability records hold UI labels, codes and counts.
