# Architecture

This document explains **why** the system perceives applications the way a person does, **how** the
pieces fit together, and **how** a goal flows through discovery, replay, hand-over and learning.
Diagrams are Mermaid (GitHub renders them). The short version of the design is in
[`REPORT.md`](../REPORT.md); the learning layer is detailed in [`LEARNING.md`](LEARNING.md); the
perception options that were weighed are in [`PERCEPTION_STUDY.md`](PERCEPTION_STUDY.md).

---

## 1. Why vision-first, and not the DOM / Playwright

**I started with Playwright.** It produced output quickly on a clean web page: find the element by
id, click, read. It stopped being the right tool as soon as the target looked like what banks and
credit unions actually run:

* **The ids are not there, or not stable.** Legacy back-office screens have no test ids; where ids
  exist they are generated (`ctl00_ContentPlaceHolder1_GridView1_ctl03_lnk`), change between
  versions and differ between tenants of the same vendor product. A selector strategy has to be
  written, debugged and maintained **per app, per version, per tenant** - across hundreds of
  institutions × ~20 apps that cost is the whole budget.
* **Many surfaces have no DOM at all.** Native desktop apps (Win32, VB6, Delphi, Tk, MFC), terminal
  emulators, Citrix/RDP-published apps: Playwright cannot see them.
* **What stays stable is what the operator sees.** The *Save* button is called *Save* in every
  version; the field next to *Email:* is the e-mail field. Labels, window titles and layout are
  the contract an operator relies on - so the agent relies on them too.

So the system **looks at the screen like a person**:

| | DOM / Playwright | Accessibility (UIA) | **Screen (this system)** |
|---|---|---|---|
| Works without a DOM (desktop, Citrix) | no | often (native controls) | **yes, any app** |
| Needs ids / test ids | yes | no | **no** |
| Per-tenant work when ids differ | rewrite selectors | some | **none (labels)** |
| Exactness of text | exact | exact where exposed | OCR (made robust) |
| Speed per look | fast | fast | ~1.5 s |

How it does that, and what it learns on top:

1. **Perceive like a person** - screenshot (only the app's window, the blinking caret removed),
   Windows OCR, OpenCV to find the controls (bordered boxes, 3D shading, tables, dropdown arrows),
   then every element gets a role and a label: *button "Save"*, *input "Email"*, *table row*.
   Targets are saved as **role + visible label**, never coordinates or ids.
2. **Learn where it is and the path to the next move** - every window the agent sees becomes a node
   of a **map of the app** (a tree: which window opened which), every action that moved between
   windows becomes an edge. Replay locates itself on the map at each step and walks back on track
   by the most reliable learned route.
3. **Learn from people** - when a person takes over, their clicks and typing are recorded, read the
   same way (role + label), shown for approval, and become steps and map moves.
4. **Learn from outcomes (reinforcement)** - every run is scored; every map move and every capability
   keeps a record of how it went; routes, approvals and demotions follow those records
   ([`LEARNING.md`](LEARNING.md)).

The model (GPT-4o) is only used to **discover** a task once, on **masked text** - it never sees a
screenshot or a customer value. After that the task is a **capability** replayed without the model.

---

## 2. System architecture

```mermaid
flowchart TB
    subgraph callers["Who calls it"]
        AG["AI agent / service<br/>capability id + typed inputs"]
        OP["Operator<br/>goal in plain language"]
    end
    CLI["python -m cua"]
    PANEL["Operator panel<br/>Agent / Human switch"]
    AG --> CLI
    OP --> PANEL

    subgraph engine["cua.engine"]
        RUN["runner<br/>goal or capability -> RunResult"]
        ROUTE["router<br/>masked goal -> intent + inputs"]
        DISC["discovery<br/>observe -> decide -> act"]
        REP["replay<br/>deterministic, no model"]
        HO["hand-over<br/>same live session, recorded"]
    end
    CLI --> RUN
    PANEL --> RUN
    RUN --> ROUTE
    RUN --> DISC
    RUN --> REP
    DISC <--> HO
    REP <--> HO

    subgraph perception["cua.perception - the Surface seam"]
        SURF["DesktopOcrSurface<br/>screenshot + OCR + OpenCV<br/>mouse + keyboard"]
    end
    DISC --> SURF
    REP --> SURF
    HO --> SURF
    SURF <--> APP[("Legacy application<br/>(BankAPP)")]

    subgraph artifact["cua.artifact"]
        CAP[("Capability<br/>typed, versioned, reviewed")]
        RES["RunResult<br/>outcome taxonomy"]
    end
    DISC -- "saves a draft" --> CAP
    CAP -- "steps, targets, checkpoint, outputs" --> REP
    RUN --> RES

    subgraph learning["cua.learning"]
        MAP[("Map of windows<br/>tree + moves + reliability")]
        REL[("Reliability records<br/>per capability")]
    end
    SURF -.->|"every screen"| MAP
    MAP -- "where am I, best route" --> REP
    RES -- "reward" --> REL
    REL -- "approval gate, demotion" --> CAP

    LLM[["GPT-4o<br/>masked text only"]]
    ROUTE --> LLM
    DISC --> LLM

    subgraph safety["cua.safety - applies across every layer"]
        POL["allow-list policy<br/>apps/app.json"]
        PII["PII vault + leak check"]
        GUARD["input guard<br/>the sheet"]
    end
    EV[("Evidence<br/>runs/id: logs, events,<br/>blurred screens, results")]
    RUN --> EV
```

**Boundaries.** The engine never touches pixels, OCR or the mouse directly - it talks to a
`Surface` that returns `Screen`s (typed elements with role, label, box) and acts on an element.
Capabilities never mention a surface either: they hold windows, roles and labels. That is the seam
that lets the same artifact be replayed through a different surface (UI Automation for native
controls, a DOM / accessibility-tree surface for legacy web) - see REPORT §4.

**Where safety sits.** The allow-list (`apps/bankapp.json`) is checked on every proposal of the
model, on every saved step before replay, on every navigation move and on every step a person
demonstrates. The PII vault masks everything before the model and before anything is written. The
input guard blocks a person's accidental input into the app while the agent works.

---

## 3. From goal to result

```mermaid
flowchart TD
    G(["Goal in plain language<br/>or capability id + typed inputs"]) --> M["Mask personal data<br/>names, e-mails, phones, accounts, amounts -> tokens"]
    M --> R{"Router (GPT-4o, masked):<br/>which permitted intent?"}
    R -- "not allowed / unknown" --> REF(["refused - nothing touched"])
    R -- "intent" --> IN["Inputs: validate locally,<br/>ask the person for missing ones"]
    IN --> W{"changes data?"}
    W -- "yes" --> CONF{"person confirms<br/>the exact values"}
    CONF -- "no" --> CAN(["cancelled"])
    CONF -- "yes" --> HOME
    W -- "no" --> HOME["Start clean: close leftover windows,<br/>reset the main window"]
    HOME --> C{"capability saved?"}
    C -- "no" --> D["DISCOVERY<br/>model learns the flow"]
    C -- "yes" --> P["REPLAY<br/>no model"]
    D --> O{"how did it end?"}
    P --> O
    O -- "checkpoint met,<br/>right record" --> S(["success + typed outputs"])
    O -- "record not found,<br/>validation, ambiguous" --> B(["business outcome"])
    O -- "session expired,<br/>confirmation" --> E(["escalated + intervention request"])
    O -- "hard failure" --> F(["failed: step, expected,<br/>observed, screenshot"])
    S & B & E & F --> L["Reward -> reliability records<br/>(approval / demotion)"]
```

An AI agent in production calls `python -m cua --capability <id> --input …` (or the same function):
no router, no model, a typed `RunResult`.

---

## 4. Discovery: the model learns a task once

```mermaid
flowchart TD
    A["observe: screenshot of the app window,<br/>OCR + parse, locate on the map"] --> B{"classify the window"}
    B -- "business outcome" --> OUT(["return it"])
    B -- "transient / busy" --> RC["dismiss or wait"] --> A
    B -- "error, unknown window" --> H["hand-over to a person"]
    B -- "ordinary" --> C["mask the screen: tokens only,<br/>+ map context + allowed actions"]
    C --> D[["GPT-4o proposes ONE event"]]
    D -- "done" --> V{"checkpoint on screen?<br/>outputs readable?<br/>right record?"}
    V -- "yes" --> SAVE(["save a DRAFT capability<br/>steps, targets, checkpoint, output rules"])
    V -- "no" --> H
    D -- "click / type / key" --> CHK{"allowed?<br/>no personal data typed?<br/>searched before opening a row?"}
    CHK -- "no, refused (3x = stuck)" --> A
    CHK -- "yes" --> ACT["act: resolve role + label,<br/>click / type through the Surface"]
    ACT --> REC["record the step:<br/>target by role + label,<br/>values as {placeholders}"] --> A
    H --> HB["person works, hands back;<br/>their steps may join the flow"] --> A
```

The flow rules are there because the capability must work **for every record**, not just the one
it was learned on: a row may only be opened after the parameter was searched for (on another
record the row is not on screen), checkpoint text that is not really on screen is dropped, output
labels must be static text (never a value).

---

## 5. Replay: the production path, and what happens when things go wrong

```mermaid
flowchart TD
    S0(["capability + typed inputs"]) --> OBS["observe + locate on the map"]
    OBS --> CL{"classify the window"}
    CL -- "busy: Please Wait" --> WAIT["wait"] --> OBS
    CL -- "transient: service unavailable" --> TR{"read capability<br/>and retries left?"}
    TR -- "yes" --> RT["dismiss, retry the last step"] --> OBS
    TR -- "no" --> HARD
    CL -- "business: not found, validation,<br/>permission, ambiguous" --> BO(["business outcome"])
    CL -- "escalate: session expired,<br/>confirmation" --> ESC
    CL -- "ordinary" --> WHERE{"on the window<br/>this step expects?"}
    WHERE -- "no" --> NAV{"learned route there?"}
    NAV -- "yes" --> GO["walk the most reliable route"] --> OBS
    NAV -- "no" --> HARD
    WHERE -- "yes" --> T{"find the control<br/>role + label / row by parameter"}
    T -- "not found" --> HARD
    T -- "several rows" --> PICK{"person present?"}
    PICK -- "yes" --> CH["person picks one"] --> DO
    PICK -- "no" --> BO
    T -- "found" --> DO["act"] --> MORE{"more steps?"}
    MORE -- "yes" --> OBS
    MORE -- "no" --> FIN{"checkpoint met?<br/>outputs read?<br/>result shows the requested record?"}
    FIN -- "yes" --> OK(["success + outputs"])
    FIN -- "no" --> HARD
    HARD{"person present?"} -- "yes" --> HO["hand-over, then resume<br/>where the map says we are"] --> OBS
    HARD -- "no" --> FAIL(["failed: code, step,<br/>expected, observed, screenshot"])
    ESC{"person present?"} -- "yes" --> HO
    ESC -- "no" --> QUEUE(["escalated:<br/>intervention request queued"])
```

| Condition | Kind | What the agent does | Result |
|---|---|---|---|
| `record_not_found`, `validation_error`, `permission_denied`, `ambiguous_match` | business | stop | `business_outcome` |
| `busy` | recoverable | wait | - |
| `transient` | recoverable | dismiss + retry the step (reads, ≤ 2) | `success` + `recoveries` |
| `session_expired`, `confirmation` | escalate | a person decides | hand-over / `escalated` |
| `target_missing`, `unknown_window`, `checkpoint_failed`, `output_missing`, `result_mismatch`, `app_error`, `timeout`, `not_permitted` | hard | stop | hand-over / `failed` |

---

## 6. Hand-over: the same live session, and back

```mermaid
sequenceDiagram
    autonumber
    participant AG as Agent (engine)
    participant SW as Panel + switch
    participant P as Person
    participant APP as Application (live session)
    participant R as Recorder
    Note over AG,APP: Agent in control - the sheet ignores the person's input to the app
    alt the agent is stuck
        AG->>SW: intervention request (why, step, blurred screenshot)
    else the person takes over
        P->>SW: clicks "AGENT working - click to take over"
        SW->>AG: pause at the next look at the screen
    end
    AG->>R: start recording (clicks, keys, screens)
    SW->>P: switch shows HUMAN, sheet lifted
    P->>APP: does the part by hand
    P->>SW: switches back to Agent
    R-->>AG: what the person did
    AG->>P: review: keep these steps?
    AG->>AG: steps -> map moves (+ human step if approved)
    AG->>APP: look: where are we? (the map)
    alt the result shows another record
        AG->>SW: hand back to the person ("another record than asked")
    else on track
        AG->>APP: resume the flow / verify the checkpoint
    end
```

```mermaid
stateDiagram-v2
    [*] --> AGENT
    AGENT --> PAUSING: switch clicked / stuck
    PAUSING --> HUMAN: at the next look at the screen
    HUMAN --> RESUMING: switched back to Agent
    RESUMING --> AGENT: the map says where we are
    RESUMING --> HUMAN: wrong record / lost on a write
    AGENT --> DONE: checkpoint met
    HUMAN --> ABORTED: Abort
    DONE --> [*]
    ABORTED --> [*]
```

Who is in control is always visible (the switch) and always recorded (the intervention record's
`in_control`, `opened_at`, `closed_at`, `human_actions`, `decision`).

---

## 7. Learning loop

```mermaid
flowchart LR
    RUN["a run"] --> OUT["outcome<br/>success / business / failed,<br/>recoveries, hand-overs, time"]
    OUT --> RW["reward 0..1"]
    RW --> CR[("capability record<br/>reliability, failures by step")]
    RUN --> MV["every move made:<br/>arrived / did not"]
    MV --> MR[("map: per-move<br/>success + miss counts")]
    MR --> RT["routes: most reliable path,<br/>failing moves dropped"]
    MR --> CTX["model context:<br/>known moves + reliability"]
    CR --> GATE["approval gate:<br/>3 clean runs, reliability >= 0.7"]
    CR --> DEM["demotion to draft<br/>when it stops being reliable"]
    HUM["person's hand-over"] --> MR
    RT & CTX & GATE & DEM --> NEXT["next run is more consistent"]
```

Details and the roadmap to fuller reinforcement learning: [`LEARNING.md`](LEARNING.md).

---

## 8. Module map

```
cua/
  __main__.py, cli.py     python -m cua ... (goal, --capability, --approve, --list, --map, --learn,
                          --explore, --panel)
  settings.py             model, limits, data paths
  errors.py               Stop / Stuck / Drift / Outcome - how a run ends early, with a code
  engine/
    runner.py             entry points: run_goal, invoke, approve, explore; RunResult; learning hook
    discovery.py          the model learns a task once (observe -> decide -> act)
    replay.py             deterministic replay, recovery, resume after a hand-over
    handoff.py            hand-over of the live session, intervention records, human steps
    navigate.py           moving with the map, start clean, reset, map context for the model
    observe.py            look (locate, classify), find targets, act
    checks.py             checkpoint, outputs, record check, answer text
    llm.py                the only way to the model (masked, leak-checked, logged)
    job.py                Job (one request) and Run (its evidence)
    hooks.py              console / panel interaction, abort and take-over signals
    text.py               small text helpers
  artifact/
    capability.py         the artifact schema (Pydantic -> capabilities/schema.json), storage
    outputs.py            deterministic output extraction (label, pattern, column, rows)
    outcomes.py           condition taxonomy and the RunResult contract
  perception/
    surface.py            the Surface seam; DesktopOcrSurface
    capture.py            DPI-aware window capture (caret removed), window helpers, Windows OCR
    parse_screen.py       OCR + OpenCV -> typed elements (buttons, inputs, tables, ...)
  safety/
    policy.py             allow-list and validators from apps/<app>.json
    pii.py                token vault, masking, leak check, data-free fingerprints
    guard.py              the input "sheet": a person's input to the app ignored while the agent works
  operator/
    panel.py              floating panel, Agent/Human switch, review and pick lists
    recorder.py           records a person's clicks / keys / screens during a hand-over
  learning/
    screenmap.py          map of the app: windows (tree), moves, reliability, routes, consolidation
    rewards.py            how good was a run
    reliability.py        Beta-Bernoulli reliability, capability records, approval gate, demotion
apps/bankapp.json         app profile: target, allow-list, known dialogs, capability contracts
capabilities/             saved capabilities (+ schema.json)
maps/                     learned map per app
runs/                     evidence of every run (not committed; curated copies in /evidence)
tests/                    engine on a scripted app (FakeSurface), units, learning
tools/                    evidence collector, OCR capture helper
```
