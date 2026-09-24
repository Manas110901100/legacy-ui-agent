# REPORT

## 1. Architecture

```
 goal ──► router (GPT-4o, masked text) ──► intent + typed inputs (validated locally, asked if missing)
                                               │
              capability saved? ── no ──► DISCOVERY: observe → mask → GPT-4o picks one event → allow-list → act
                    │ yes                        └─► draft capability (steps, targets, checkpoint, outputs)
                    ▼
               REPLAY (no LLM): observe → locate window on the map → classify → resolve target → act
                    │                         → checkpoint → read outputs → RunResult ──► reward → reliability
                    └─ stuck ─► hand-over of the live session (record → review → resume) / escalate
 everything above talks to a Surface: DesktopOcrSurface = screenshot + Windows OCR + OpenCV, mouse + keyboard
```

The architecture diagram, the flowcharts (goal → result, discovery, replay, hand-over, control
states, learning) and the module map are in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md). One
Python package (`cua/`: `engine`, `artifact`, `perception`, `safety`, `operator`,
`learning`), synchronous, one app session at a time. Key decisions:

* **Perception by pixels, not by an object model.** I started with Playwright: fine on a clean web
  page, wrong for this problem. Legacy screens have missing or generated ids that change between
  versions and tenants, so selectors must be written and maintained per app, per version, per tenant;
  native desktop, terminal and Citrix apps have no DOM at all. What stays stable is what an operator
  sees: labels, titles, layout. So the screen is captured (DPI-aware, client area only, three frames
  merged so the blinking caret disappears), read with the built-in Windows OCR, and parsed with OpenCV
  into typed elements (button, input, dropdown, label, table row/cell, status) by border shading and
  layout (`perception/parse_screen.py`). BankAPP (Tkinter, custom-drawn grid) exposes almost nothing
  through UI Automation anyway. Trade-off: slower (~1.5 s per look) and OCR-noisy, so every text
  comparison is fuzzy. UIA/Win32 text would be exact where it exists: the next surface, not a
  replacement (§4).
* **The LLM gets text, never images.** GPT-4o sees a compact JSON of the screen with every piece of
  customer data replaced by tokens (§6). Cheaper than vision, and no screenshot can leak data.
* **Replay needs no model.** The router is only used for free-text goals; an AI agent in production
  calls a capability by id with typed inputs (`python -m cua --capability … --input …`): zero LLM calls.
* **A learned map of the app** (`learning/screenmap.py`): each kind of window is a node (title with
  record numbers masked + its static controls), nodes form a tree via the Windows owner relation, and
  every action that moved between windows is an edge, with how often it arrived and how often it did
  not. Replay recognises where it is, walks back by the most reliable route, and resumes after a
  hand-over without asking the model. A person's moves during a hand-over extend it.
* **Learning from outcomes** (`learning/`, design in [`docs/LEARNING.md`](docs/LEARNING.md)): every run
  gets a reward; moves and capabilities keep reliability records that choose routes, gate approval and
  demote a capability that stops working. No trial-and-error exploration: only permitted moves are ranked.
* **Configuration, not code, for the app**: `apps/bankapp.json` holds the target window, the
  capability contracts, the allow-list, deny list and known dialogs.

## 2. Artifact schema

`capabilities/bankapp.account_details.json` (schema: `capabilities/schema.json`, models in `cua/artifact/capability.py`):

```jsonc
{ "schema": "capability/1", "id": "bankapp.account_details", "version": 2, "status": "approved",
  "app": {"app": "bankapp", "product": "BankAPP", "vendor_version": "1.0", "surface": "desktop-ocr"},
  "description": "Open one account's details (balance, status, contact, transactions) and read them.",
  "mode": "read",
  "inputs":  {"account": {"type": "account_ref", "pii": "ACCOUNT", "record_key": true,
                          "description": "Account number (ACC123456) or customer name"}},
  "outputs": {"balance": {"type": "money", "extract": {"label": "Balance"}},
              "transactions": {"type": "table", "extract": {"rows_below": "Date Time"}}, "...": {}},
  "steps": [
    {"id": "s1", "window": "BankAPP - Core Banking System", "action": "type",
     "target": {"role": "input", "label": "Account No / Name"}, "value": "{account}", "fingerprint": {}},
    {"id": "s2", "window": "BankAPP - Core Banking System", "action": "click",
     "target": {"role": "button", "label": "Find"}},
    {"id": "s3", "window": "BankAPP - Core Banking System", "action": "double_click",
     "target": {"role": "table_row", "column": "Account No", "equals": "{account}"}, "on_many": "ask_user"}],
  "checkpoint": {"window": "Account Details - *"},
  "outcomes": {"record_not_found": "return", "transient": "retry", "session_expired": "escalate",
               "result_mismatch": "fail", "...": "..."},
  "provenance": {"discovered_in": "runs/…", "model": "gpt-4o", "example_goal": "what is the balance of <ACCT_1>",
                 "approved_by": "…", "approved_at": "…"} }
```

* **Contract first.** A calling agent needs inputs (typed, with PII class), outputs (typed, with where
  they are read) and the outcomes it can get back, so those lead the file; steps are the implementation.
* **Targets = role + visible label** ("the `Find` button", "the `Account No / Name` field"), matched
  OCR-tolerantly. Legacy apps have no ids; coordinates change with window size, DPI and data; labels
  are what an operator would name and what stays stable across versions. **Table rows are addressed by
  a parameter** (`equals: "{account}"` in its column, falling back to any column, then containment),
  never by a customer's data; several matches → the user picks, or `ambiguous_match` unattended.
* **Each step names its window** (title, numbers masked) and keeps the window's static controls as a
  fingerprint (anything that looks like data is left out): the precondition and the drift signal.
* **Checkpoint** = window title pattern + optional visible text / table cell; values generated per run
  are generalised to `*`, input values to `{param}`. A window pattern alone can't tell *which* record
  is shown, so inputs that identify the record carry `"record_key": true`: after every run the value
  must appear in the outputs (ACC100012 → `account_no` ACC100012, "sarah" → `customer` "Sarah Jones"),
  else `result_mismatch`. This caught a real case: during a hand-over the operator opened ACC100011
  while ACC100012 was asked for.
* **Human steps** (`"kind": "human"`) are first-class: a part of the flow a person does (with a guide
  of what they did last time).
* **Versioned and reviewable**: every save archives the previous version; `draft` after discovery;
  `approved` only by a reviewer (`--approve`) **and only once it has proved itself** (≥ 3 clean
  replays, reliability ≥ 0.7; `--force` is recorded). Unattended invocation of a draft is refused; a
  replay that changes the flow resets it to `draft`, and so does a run of failures. Run statistics live
  next to the artifact (`<id>.stats.json`), never in it. No transcript, screenshots or customer data in
  the file; the example goal is stored masked.

## 3. Determinism & error handling

Replay per step: look at the front window (if the app opened a window on top without activating it,
switch to it; wait out "Please Wait") → locate it on the map → if it is not the step's window, take the
most reliable learned route there (allow-listed moves only) → **classify** it → resolve the target
(fuzzy label ≥ 0.85 / row by parameter) → act (the point must be on the observed window of the app) →
settle. At the end: checkpoint, record key, then outputs read by static labels / patterns / column
headers (`artifact/outputs.py`), by position, whichever way the parser grouped the texts. Typing
clears the field char-by-char (Shift+Home proved unreliable); dropdowns are chosen by OCR of the list.

Every window is classified from the profile's known dialogs (BankAPP's own error classes, narrowed by
their text: "Application Error … try again later" is transient, other Application Errors are hard)
plus a keyword fallback:

| kind | codes | behaviour | result |
|---|---|---|---|
| business | `record_not_found`, `validation_error`, `permission_denied`, `ambiguous_match` | stop | `business_outcome` + message |
| recoverable | `busy`, `transient` | wait; dismiss + retry the last step (≤ 2, **reads only**) | `success` + `recoveries` |
| escalate | `session_expired`, `confirmation` | a person decides | hand-over / `escalated` + intervention request |
| hard | `app_error`, `unknown_window`, `target_missing`, `checkpoint_failed`, `output_missing`, `result_mismatch`, `timeout`, `not_permitted` | stop | hand-over (interactive) / `failed` |

`RunResult` (`artifact/outcomes.py`): `status` (success · business_outcome · failed · escalated ·
cancelled · refused), `outcome`, typed `outputs`, `failure` {code, step, expected, observed, blurred
screenshot}, `recoveries`, `interventions`, `llm_calls`, `duration_s`, `reliability`. Example: a
missing button gives `target_missing` at `s2`, expected `button 'Find'`, observed `window 'BankAPP…'
with New, Edit, …`.

**Consistency over time.** Each result is scored (1 for a success or a correct business outcome, less
for hand-overs, recoveries and slowness, 0 for a failure) into the capability's record, and every move
records whether it arrived. That record found a real flaw: the map believed Esc closed *Account
Details* 9 of 9 times while the logs showed it failing 7 times; now the misses count, and a move that
keeps failing is dropped from routes (the window is closed directly or a learned *Close* is used).
`python -m cua --learn` rebuilds the records from past runs (account_details: 0.76 over 19 runs).

UI drift is secondary: windows are matched on their controls only (≥ 80 % in common, labels fuzzy), so
a moved or re-styled control does not stop the run; an unknown window or a missing control does.

## 4. Heterogeneity & multi-tenant

**Surface seam.** The recorded flow never mentions pixels or DOM: it holds windows (title patterns),
roles and labels. A `Surface` (`perception/surface.py`) turns whatever the app is into `Screen`s of
typed elements and acts on them. Built: `DesktopOcrSurface`. Next: `UiaSurface` (native controls via
UI Automation / Win32 `WM_GETTEXT`, exact text and states where the toolkit exposes them, OCR where it
does not; the perception study is in [`docs/PERCEPTION_STUDY.md`](docs/PERCEPTION_STUDY.md)) and
`WebSurface` for legacy web (accessibility tree first, DOM heuristics for framesets/tables,
screenshot+OCR as the floor). Same capability, same replay engine; only the surface and the parser
change. The tests already run the engine on a fake surface.

**Multi-tenant reuse.** A capability belongs to a **vendor product + version**, not to a tenant:
`app` + `vendor_version` in the artifact. Tenants running that product share it and differ only in an
**overlay profile** layered over the product profile: label aliases (`"Customer name" → "Member name"`),
window title aliases, branding, a narrower allow-list, extra known dialogs. Replay resolves targets
through the overlay, so one artifact serves many tenants. Drift per tenant/version is detected cheaply:
window fingerprints (controls) and map nodes are compared on every replay, and reliability is kept per
capability; a tenant whose windows stop matching the product's fingerprints, or whose reliability
drops, gets that capability demoted to `draft` for that tenant (re-discovery or a hand-over records
the difference as an override, not a fork). The product's record is a prior for a new tenant. Not
built: the overlay loader and per-tenant records (§7).

## 5. Escalation & handoff

**Detect.** Any hard or escalate condition (table above), GPT-4o giving up / repeating itself / three
refused proposals / 25 steps during discovery, a window the map cannot route from, or a saved human step.

**Route.** An intervention request is written (`intervention_NN.json`: capability, masked goal, step id,
reason, code, window, blurred screenshot, who is in control, timestamps, what the person did and whether
it was saved). Unattended runs stop there with status `escalated` (the request is the queue item).

**Take control of the live session.** Interactive runs hand over the *same* app session: the operator
panel (`operator/panel.py`, a local Tk window, the mock operator console) shows the request, the switch
flips to **Human**, the input "sheet" lifts. While the agent works, a low-level input guard ignores the
operator's clicks and typing into the app (the agent's own injected input passes; other programs, the
panel and the abort hotkeys keep working), so control is never shared by accident. The operator can
also take over at any moment by clicking the switch; the agent pauses at its next look at the screen.

**Record and hand back.** While the person works, low-level mouse/keyboard hooks and a frame ring buffer
record clicks and typing on the app only (`operator/recorder.py`); after hand-back the frames are OCR'd
and turned into steps (role + label targets, typed values → `{param}`), shown for review, and the
approved ones are saved as a human step (handed to a person again next time). The person's moves also
extend the map. The agent then **resumes** from wherever the map says it is: the matching step, the
final checkpoint, a walk to the next step (reads), or a restart of the read; a write is never restarted,
it asks the person to finish. Whatever the person did, the result is still checked against the request
(checkpoint + record key), so a hand-over that ends on the wrong record is caught and handed back.
Control states: `AGENT → PAUSING (next observe) → HUMAN → RESUMING → AGENT`, visible on the switch
(sequence and state diagrams in `docs/ARCHITECTURE.md` §6).

## 6. Safety

* **Allow-list** (`apps/bankapp.json`): the only capabilities are three reads and one write
  (`create_account`); each lists the buttons, fields, dropdowns and windows it may use. A deny list
  (edit, deposit, withdraw, transfer, close acct, delete, log off …) wins over everything; menus are
  refused outright. Enforced on GPT-4o's proposals, on saved steps before a replay, on navigation
  moves and on steps a person demonstrates. Every click must land inside the app's observed window.
  The learning layer only ranks moves that are already permitted; it never explores.
* **Risky actions.** The one irreversible action (creating an account) needs a confirmation of the
  exact values (panel) or an explicit `--confirm-write` from the caller; transient errors are not
  retried for writes and a lost write is never restarted (no duplicate accounts). Everything else that
  changes data is simply not possible: blocking beats confirming for a system that runs unattended.
* **Personal data.** A per-run token vault replaces names, e-mails, phones, account numbers, amounts
  and unknown words (default deny) before anything reaches GPT-4o; a fail-closed leak check blocks the
  call if a known value or a PII pattern is still in the payload. Logs, events, screen JSONs and results
  are written through the vault; screenshots are blurred wherever data is shown; capabilities, map
  fingerprints and reliability records hold no data; masked LLM payloads are kept for audit.
  Credentials are never handled: the operator logs in, session expiry escalates. The operator panel,
  switch and sheet are excluded from screen capture.
* **Limits.** Masking depends on OCR: text OCR misses is not blurred; a name that is also a common
  English word can pass the request filter (the leak check covers every value the agent knows); the
  guard is per machine; capture exclusion needs Windows 10 2004+.

## 7. Cuts

Cut deliberately: multi-tenant infrastructure (overlay profiles, per-tenant approvals and records,
designed in §4); UIA and web surfaces (seam built, surfaces not); a real remote operator console (local
panel instead; production would route intervention requests to a queue and give the operator the
session via RDP/VNC shadowing with the same switch semantics); parsing more than one table per window
(outputs read by position instead); reinforcement learning beyond reliability estimation (no
exploration, no learned policy; see `docs/LEARNING.md`); bounded LLM recovery of a single failed step;
video recording of runs; cross-platform support.

Next, in order: UIA/Win32 hybrid perception (exact text, less OCR); tenant overlay profiles with label
aliases; promoting repeated human demonstrations to automated draft steps; a queue + remote session
for interventions; bandits over alternative locators and recoveries; multi-table parsing;
OCR-confidence gating of risky steps.
