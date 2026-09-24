# Evidence

Each folder is one run, copied from `runs/`. Everything in it is masked: customer values appear as tokens (`<NAME_1>`, `<ACCT_2>` ...), screenshots are blurred where data is shown, `llm_NN.json` is exactly what GPT-4o received.

| kind | run | capability | status | outcome / failure | recoveries | LLM calls |
|---|---|---|---|---|---|---|
| 10_handoff | `20260924_172841_account_details` | bankapp.account_details v1 | success |  | 0 | 1 |
| 1_discovery | `20260924_171342_account_details` | bankapp.account_details v1 | success |  | 0 | 6 |
| 2_draft_refused | `20260924_171427_account_details_replay` | bankapp.account_details v1 | refused | not_approved | 0 | 0 |
| 3_replay | `20260924_171428_account_details_replay` | bankapp.account_details v1 | success |  | 0 | 0 |
| 4_replay_unlisted_account | `20260924_171445_account_details_replay` | bankapp.account_details v1 | success |  | 0 | 0 |
| 5_replay_by_name | `20260924_171501_account_details_replay` | bankapp.account_details v1 | success |  | 0 | 0 |
| 6_not_found | `20260924_171518_account_details_replay` | bankapp.account_details v1 | business_outcome | record_not_found | 0 | 0 |
| 7_ambiguous | `20260924_171531_account_details_replay` | bankapp.account_details v1 | business_outcome | ambiguous_match | 0 | 0 |
| 8_bad_input | `20260924_171547_account_details_replay` | bankapp.account_details v1 | failed | invalid_input | 0 | 0 |
| 9_transient_retry | `20260924_173923_account_details_replay` | bankapp.account_details v2 | success |  | 1 | 0 |

`capabilities/` holds the saved capability artifacts and `schema.json`.

## Notes

* **1_discovery** - GPT-4o learned "what is the balance of ACC100005" on masked screens (see `llm_*.json`):
  one proposed shortcut (opening the row without searching) was refused by the agent, then it typed
  `{account}`, clicked Find and opened the matching row. The run saved `bankapp.account_details` v1 (draft).
* **2_draft_refused -> 3..8** - the draft was refused for unattended use, approved with
  `--approve`, then replayed with **no LLM call**: ACC100005, ACC100045 (not visible until searched),
  a customer name, ACC999999 (`record_not_found` = business outcome), "sarah" (3 matches ->
  `ambiguous_match`), "!!" (`invalid_input`, the app is never touched).
* **9_transient_retry** - BankAPP restarted with `BANKAPP_FAULT_RATE=0.6`. BankAPP retries a failed call
  three times itself and then shows "Application Error: Service unavailable ... try again later"; the
  profile classifies that message as `transient`, so the agent dismissed it and retried step `s3`
  (`run.log`, `events.jsonl`: `condition` -> `recovery`).
* **10_handoff** - a real operator hand-over from the panel: the operator flipped the switch at step `s2`
  (`intervention_01.json`: reason, step, screenshot, the operator's recorded action, decision "not saved"),
  and the agent resumed from the map. In this run the operator opened ACC100011 although ACC100012 was
  asked for, and the run still reported success - **this is the bug that led to the record-key check**
  (`record_key` on the input, `result_mismatch`, capability v2). With the check, the same hand-over is
  caught and handed back to the operator (`tests/test_replay.py::test_person_opening_the_wrong_record_is_caught_and_asked_again`).
* **11_learning** - the learning layer run over the real runs above (`python -m cua --learn`, `--map`,
  `--list`). `learn.txt`: `bankapp.account_details` has reliability 0.76 over 19 runs, 14 of them
  clean. The failures, by code and step, are development-time `internal_error`s. `map.txt`: one main
  window with its child windows, and every learned move with its arrived/missed counts. The committed
  map was built before misses were counted, so it shows no failures yet; new runs add them.
  `bankapp.account_details.stats.json`: the per-run record (reward, status, code, step, recoveries,
  hand-overs, seconds), with no data values.
