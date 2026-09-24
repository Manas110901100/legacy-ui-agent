"""
settings.py - the model, the limits, and where data lives.

Data folders are anchored to the repository root, not the current directory, so
`python -m cua ...` works from anywhere. Modules read these at call time (settings.RUNS), so tests
can point them at a temporary folder.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APPS = ROOT / "apps"                 # app profiles: target, allow-list, known dialogs, contracts
CAPS = ROOT / "capabilities"         # saved capabilities (+ archive/, schema.json, *.stats.json)
MAPS = ROOT / "maps"                 # learned map of each app's windows
RUNS = ROOT / "runs"                 # evidence of every run

MODEL = "gpt-4o"                     # don't pin gpt-4o-2024-05-13: it shuts down Oct 23, 2026
MAXIMIZE_MAIN = True                 # same window size every run = same layout
CONFIRM_LLM_STEPS = False            # while discovering, ask before each GPT-4o event
MAX_STEPS = 25                       # GPT-4o events before a person is asked to help
MAX_HANDOFFS = 3                     # hand-overs to a person per run before giving up
MAX_RETRIES = 2                      # retries of one step after a transient error
RECORD_HUMAN = True                  # record what the person does during a hand-over
TEXT_MATCH = 0.85                    # OCR text similarity that still counts as "same text"
EXPLORE_OPENERS = ("New", "Details", "Find", "Show All")    # buttons --explore may try
CANCEL_CODES = ("cancelled", "aborted", "user_stopped", "user_rejected")

# learning from outcomes (cua/learning)
APPROVE_MIN_CLEAN_RUNS = 3           # clean runs (no hand-over, checkpoint met) before --approve
APPROVE_MIN_RELIABILITY = 0.7        # and at least this reliability
DEMOTE_WINDOW = 10                   # an approved capability is re-checked over its last N runs...
DEMOTE_BELOW = 0.6                   # ...and goes back to draft below this reliability
SKIP_MOVE_AFTER = 3                  # a map move tried this often...
SKIP_MOVE_BELOW = 0.3                # ...with reliability below this is not used any more
