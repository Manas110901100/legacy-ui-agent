"""
hooks.py - how the engine talks to a person, and the signals a person can send it.

Console versions by default; the operator panel (cua/operator/panel.py) replaces them with its
windows. The engine always calls them through this module (hooks.choose(...)), so the replacement
and the tests' stand-ins take effect everywhere.
"""
import threading
import winsound

from cua.errors import Stop

ABORT = threading.Event()          # set by a hotkey: stop before the next step
TAKEOVER = threading.Event()       # set when the person flips the switch to Human
TOOK_OVER = "you switched to Human"


def ask_text(question):
    return input(f"  {question} > ").strip()


def ask_yes_no(question):
    return input(f"{question} [y/N] ").strip().lower() == "y"


def notify(message):
    print(message)


def choose(question, options):
    """Pick one option: its index, or None to stop."""
    print(question)
    for n, option in enumerate(options, start=1):
        print(f"  {n}. {option}")
    answer = input("Number (empty = stop) > ").strip()
    return int(answer) - 1 if answer.isdigit() and 1 <= int(answer) <= len(options) else None


def take_over(message):
    """A person does the next part in the app. True = handed back, False = abort."""
    return input(f"\n{message}\nPress Enter when done (or type 'abort') > ").strip().lower() != "abort"


def review_steps(lines, allowed):
    """Which recorded steps to save: list of indexes, [] = none, None = abort the run."""
    print("\nWhat you did:")
    for n, line in enumerate(lines, start=1):
        print(f"  {n}. {line}")
    answer = input("Save the allowed steps? [y/N/abort] ").strip().lower()
    if answer == "abort":
        return None
    return [i for i, ok in enumerate(allowed) if ok] if answer == "y" else []


def heartbeat():
    """Called at every look at the screen; the panel uses it to know the agent is still alive."""


def alert():
    winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)


def check_abort():
    if ABORT.is_set():
        raise Stop("aborted by user (hotkey)", code="aborted")
