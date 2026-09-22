# Computer-Use Automation — Step 1: Observer

Turns a live page (including nested iframes) into a `PageSnapshot`:
URL pattern, fingerprint, readiness, pruned XML tree, interactive / static / state
elements with stable ids and ranked locators, events (dialogs, new tabs), diff vs.
previous step, and screenshots (viewport, annotated set-of-marks, full page) with PII masked.

## Setup
    pip install -r requirements.txt
    playwright install chromium

## Run
    python demo_observe.py            # against the bundled legacy fixture
    python demo_observe.py --headed   # watch it
    python demo_observe.py --url https://example.com
    pytest -q

Snapshots land in `evidence/observe/` (JSON + PNGs).

## Layout
    cua/observer/models.py    PageSnapshot schema (surface-agnostic)
    cua/observer/extract.js   in-frame extraction (read-only)
    cua/observer/observer.py  frame stitching, ids, locators, XML, screenshots, diff
    cua/observer/ready.py     network idle + no spinner + DOM stable
    cua/observer/redact.py    PII redaction
    fixtures/legacy_bank/     hostile legacy test pages (tables, nested iframes, onclick tds)
"# legacy-ui-agent" 
