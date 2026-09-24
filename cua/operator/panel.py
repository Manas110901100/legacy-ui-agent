"""
panel.py - the operator panel: goals in plain language, the Agent/Human switch, hand-overs.

  python -m cua --panel        (leave it running in the background)

When BankAPP opens, a small panel slides in at the bottom-right: type a goal, press Automate.
The panel flies off the screen and the task runs without per-step questions. It comes back when
the run ends, when a value is missing, before creating an account (one summary to allow), and
when the agent is stuck.

While the agent works, a light "sheet" lies over BankAPP: your clicks and typing into BankAPP are
ignored (other programs work as usual), and an Agent / Human switch sits at the top of the screen.
Click it to take over at any time: the agent stops at its next step, the sheet lifts and what you
do is recorded. Switch back to Agent when done; the panel lists what it saw you do (the steps you
keep are handed to a person again on later runs) and the agent carries on. When the agent gets
stuck by itself, the switch flips to Human for you.
The panel, sheet and switch are excluded from screen capture (SetWindowDisplayAffinity), so they
never show up in the agent's screenshots, even while they are visible to you.

  Ctrl+Shift+R   show the panel
  Ctrl+Shift+Q   abort the running task (stops before the next step)
  Ctrl+Shift+X   quit this listener

pip install keyboard   (plus everything in requirements.txt)
"""
import ctypes
import queue
import time
import tkinter as tk
import winsound
from ctypes import wintypes

import keyboard

from cua.engine import hooks, runner
from cua.perception import capture
from cua.safety.guard import InputGuard
from cua.safety.policy import PROFILE

APP_TITLE = PROFILE.window_title

RUN_KEY, ABORT_KEY, QUIT_KEY = "ctrl+shift+r", "ctrl+shift+q", "ctrl+shift+x"
WDA_EXCLUDEFROMCAPTURE = 0x11       # visible on the monitor, absent from every screen capture
GWL_EXSTYLE, WS_EX_NOACTIVATE = -20, 0x08000000
WS_EX_LAYERED, WS_EX_TRANSPARENT, WS_EX_TOOLWINDOW = 0x00080000, 0x00000020, 0x00000080
FLY_FRAMES, FLY_SECONDS = 16, 0.25
POLL_SECONDS = 1.0                  # how often to look for the application window
IDLE_TEXT = "Create an account, or ask about an account."

# mode: (field label or None, [(button, reply)], takes keyboard focus)
MODES = {
    "goal": ("Goal", [("Automate", "ok")], True),
    "answer": ("Answer", [("Continue", "ok")], True),
    "confirm": (None, [("Allow", "ok"), ("Stop", "cancel")], True),
    "choose": (None, [("Continue", "ok"), ("Stop", "cancel")], True),
    "handoff": (None, [("Switch to Agent", "ok"), ("Abort", "cancel")], False),
    "review": (None, [("Save & continue", "ok"), ("Continue without saving", "skip"), ("Abort", "cancel")], False),
}

root = tk.Tk()
root.withdraw()
user32 = ctypes.windll.user32
user32.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
jobs = queue.Queue()                # ("run", goal) | ("show", None) | ("quit", None)
guard = InputGuard()                # ignores your input to BankAPP while the agent works
session = {}                        # the run in progress: main window handle, app process id


def _force_front(win):
    """Windows blocks background apps from taking focus; an ALT tap lets our box come forward."""
    win.update_idletasks()
    user32.keybd_event(0x12, 0, 0, 0)
    user32.keybd_event(0x12, 0, 2, 0)
    hwnd = user32.GetParent(win.winfo_id()) or win.winfo_id()
    user32.SetForegroundWindow(hwnd)
    win.lift()
    win.focus_force()


def work_area():
    r = wintypes.RECT()
    user32.SystemParametersInfoW(0x30, 0, ctypes.byref(r), 0)       # SPI_GETWORKAREA (no taskbar)
    return r.left, r.top, r.right, r.bottom


def off_screen_x():
    return user32.GetSystemMetrics(76) + user32.GetSystemMetrics(78) + 20   # right of all monitors


def ease_out(t):
    return 1 - (1 - t) ** 3


def ease_in(t):
    return t ** 3


class Panel:
    """Borderless always-on-top box: status line, optional checklist / field, buttons."""
    def __init__(self):
        self.win = win = tk.Toplevel(root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.withdraw()
        box = tk.Frame(win, bd=1, relief="solid", padx=10, pady=8)
        box.pack(fill="both", expand=True)
        self.status = tk.Label(box, justify="left", anchor="w", wraplength=400)
        self.checks = tk.Frame(box)
        self.row = tk.Frame(box)
        self.row.pack(fill="x")
        self.field = tk.Frame(self.row)
        self.field_label = tk.Label(self.field)
        self.field_label.pack(side="left")
        self.entry = tk.Entry(self.field, width=38)
        self.entry.pack(side="left", padx=(6, 0), fill="x", expand=True)
        self.buttons, self.check_vars = [], []
        self.choice = tk.IntVar(value=0)
        win.bind("<Return>", lambda _e: self.press(MODES[self.mode][1][0][1]))
        win.bind("<Escape>", lambda _e: self.press("cancel"))
        self.mode, self.goal, self.shown = "goal", "", False
        self.answer = tk.StringVar()
        self.warned = False

    # ------------------------------------------------ layout / motion
    def _layout(self, mode, status, lines=None, allowed=None, options=None):
        self.mode = mode
        label, buttons, _ = MODES[mode]
        for w in (self.status, self.checks, self.field, *self.buttons):
            w.pack_forget()
        for b in self.buttons:
            b.destroy()
        for c in self.checks.winfo_children():
            c.destroy()
        if status:
            self.status.config(text=status[:900])
            self.status.pack(fill="x", pady=(0, 6), before=self.row)
        self.check_vars = []
        if lines:
            for line, ok in zip(lines, allowed):
                var = tk.BooleanVar(value=ok)
                tk.Checkbutton(self.checks, text=line, variable=var, anchor="w", justify="left", wraplength=380,
                               state="normal" if ok else "disabled").pack(fill="x", anchor="w")
                self.check_vars.append(var)
            self.checks.pack(fill="x", pady=(0, 6), before=self.row)
        if options:
            self.choice.set(0)
            for n, option in enumerate(options):
                tk.Radiobutton(self.checks, text=option, variable=self.choice, value=n, anchor="w",
                               justify="left", wraplength=380).pack(fill="x", anchor="w")
            self.checks.pack(fill="x", pady=(0, 6), before=self.row)
        if label:
            self.field_label.config(text=label)
            self.entry.delete(0, "end")
            if mode == "goal":
                self.entry.insert(0, self.goal)
                self.entry.select_range(0, "end")
            self.field.pack(side="left", fill="x", expand=True)
        self.buttons = [tk.Button(self.row, text=text, command=lambda r=reply: self.press(r),
                                  default="active" if n == 0 else "normal")
                        for n, (text, reply) in enumerate(buttons)]
        for b in self.buttons:
            b.pack(side="left", padx=(6, 0))

    def _home(self):
        self.win.update_idletasks()
        w, h = self.win.winfo_reqwidth(), self.win.winfo_reqheight()
        _, _, right, bottom = work_area()
        return right - w - 16, bottom - h - 16, w, h

    def _animate(self, x0, x1, y, w, h, ease):
        for i in range(1, FLY_FRAMES + 1):
            x = round(x0 + (x1 - x0) * ease(i / FLY_FRAMES))
            self.win.geometry(f"{w}x{h}+{x}+{y}")
            self.win.update()
            time.sleep(FLY_SECONDS / FLY_FRAMES)

    def _hwnd(self):
        return user32.GetAncestor(self.win.winfo_id(), 2)            # GA_ROOT = Tk's wrapper window

    def _exclude_from_capture(self):
        if not user32.SetWindowDisplayAffinity(self._hwnd(), WDA_EXCLUDEFROMCAPTURE) and not self.warned:
            self.warned = True
            print("warning: could not hide the panel from screen capture "
                  "(needs Windows 10 2004+); it still flies off during runs.")

    def _set_focusable(self, focusable):
        """Hand-over modes must not take the focus from BankAPP while the person works in it."""
        hwnd = self._hwnd()
        style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        style = style & ~WS_EX_NOACTIVATE if focusable else style | WS_EX_NOACTIVATE
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)

    def fly_in(self, status="", mode="goal", lines=None, allowed=None, options=None):
        self._layout(mode, status, lines, allowed, options)
        x, y, w, h = self._home()
        if self.shown:
            self.win.geometry(f"{w}x{h}+{x}+{y}")
        else:
            self.win.geometry(f"{w}x{h}+{off_screen_x()}+{y}")
            self.win.deiconify()
            self.win.update_idletasks()             # Tk creates the real top-level window on map
            self._exclude_from_capture()
            self._animate(off_screen_x(), x, y, w, h, ease_out)
            self.shown = True
        takes_focus = MODES[mode][2]
        self._set_focusable(takes_focus)
        if takes_focus:
            _force_front(self.win)
            (self.buttons[0] if mode in ("confirm", "choose") else self.entry).focus_force()

    def fly_out(self):
        if not self.shown:
            return
        x, y = self.win.winfo_x(), self.win.winfo_y()
        w, h = self.win.winfo_width(), self.win.winfo_height()
        self._animate(x, off_screen_x(), y, w, h, ease_in)
        self.win.withdraw()
        self.win.update()
        self.shown = False

    # ------------------------------------------------ input
    def press(self, reply):
        if self.mode == "goal":
            goal = self.entry.get().strip()
            if reply == "ok" and goal:
                self.goal = goal
                jobs.put(("run", goal))
            elif reply == "cancel":
                self.fly_out()
        elif reply == "cancel" or reply in [r for _, r in MODES[self.mode][1]]:
            self.answer.set(f"{reply}:{self.entry.get().strip() if MODES[self.mode][0] else ''}")

    def ask(self, question, mode, lines=None, allowed=None, options=None):
        """Fly in, wait for a button, fly out. Returns (reply, text): reply is ok / skip / cancel."""
        self.answer.set("")
        self.fly_in(question, mode, lines, allowed, options)
        root.wait_variable(self.answer)
        reply, _, text = self.answer.get().partition(":")
        self.fly_out()
        return reply, text


def overlay_window(win, clickthrough):
    """Helper window: never takes focus, not in Alt+Tab, not in screenshots; optionally click-through."""
    win.update_idletasks()
    hwnd = user32.GetAncestor(win.winfo_id(), 2)
    style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE) | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
    if clickthrough:
        style |= WS_EX_LAYERED | WS_EX_TRANSPARENT
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
    user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE)


class Sheet:
    """Light tint over BankAPP while the agent works. Click-through: the guard does the blocking."""
    def __init__(self):
        self.win = w = tk.Toplevel(root)
        w.overrideredirect(True)
        w.attributes("-topmost", True)
        w.attributes("-alpha", 0.12)
        w.configure(bg="#1f6feb")
        w.withdraw()
        self.rect = None

    def cover(self, rect):
        if rect == self.rect:
            return
        x0, y0, x1, y1 = self.rect = rect
        self.win.geometry(f"{x1 - x0}x{y1 - y0}+{x0}+{y0}")
        if self.win.state() == "withdrawn":
            self.win.deiconify()
            overlay_window(self.win, clickthrough=True)
        self.win.update_idletasks()

    def hide(self):
        self.win.withdraw()
        self.rect = None


class Toggle:
    """Agent / Human switch at the top centre of the screen, over the app's title bar
    (the agent only clicks inside windows, never on title bars)."""
    W, H = 330, 34
    TEXT = {"agent": "AGENT working - click to take over", "human": "HUMAN in control - click to hand back"}
    COLOR = {"agent": "#2e9b4f", "human": "#e07b16"}

    def __init__(self):
        self.win = w = tk.Toplevel(root)
        w.overrideredirect(True)
        w.attributes("-topmost", True)
        w.withdraw()
        self.canvas = tk.Canvas(w, width=self.W, height=self.H, bg="#202020", highlightthickness=0)
        self.canvas.pack()
        self.canvas.bind("<Button-1>", self.click)
        self.state, self.on_hand_back = "agent", None

    def _draw(self):
        c, color = self.canvas, self.COLOR[self.state]
        c.delete("all")
        c.create_oval(8, 7, 28, 27, fill=color, outline=color)
        c.create_oval(40, 7, 60, 27, fill=color, outline=color)
        c.create_rectangle(18, 7, 50, 27, fill=color, outline=color)
        knob = 50 if self.state == "agent" else 18
        c.create_oval(knob - 9, 8, knob + 9, 26, fill="white", outline="white")
        c.create_text(72, 17, text=self.TEXT[self.state], fill="white", anchor="w", font=("Segoe UI", 10, "bold"))

    def show(self, state):
        self.state = state
        self._draw()
        left, top, right, _ = work_area()
        x, y = (left + right - self.W) // 2, top + 2
        self.win.geometry(f"{self.W}x{self.H}+{x}+{y}")
        if self.win.state() == "withdrawn":
            self.win.deiconify()
            overlay_window(self.win, clickthrough=False)
        self.win.lift()
        self.win.update_idletasks()
        guard.toggle_rect = (x, y, x + self.W, y + self.H)

    def hide(self):
        self.win.withdraw()
        guard.toggle_rect = None

    def click(self, _e=None):
        if self.state == "human" and self.on_hand_back:
            self.on_hand_back()
        elif self.state == "agent":                    # normally the guard catches this click first
            request_take_over()


panel = Panel()
sheet = Sheet()
toggle = Toggle()


def request_take_over():
    """The person clicked the switch while the agent works (may run on the guard's hook thread)."""
    hooks.TAKEOVER.set()
    winsound.MessageBeep(winsound.MB_OK)


guard.on_take_over = request_take_over


def agent_working():
    """Sheet over BankAPP, switch on Agent, your clicks / typing into BankAPP ignored."""
    sheet.cover(capture.window_rect(session["hwnd"]))
    toggle.show("agent")
    guard.start(session["pid"])


def heartbeat():
    """Called by the agent at every look at the screen (on this thread): watchdog + keep windows fresh."""
    guard.alive()
    if session and guard.blocking:
        sheet.cover(capture.window_rect(session["hwnd"]))   # the app window may have been maximised
        toggle.win.lift()
    root.update()


# ---- plug the panel into the agent (replaces console input/print)
def ask_text(question):
    reply, text = panel.ask(question, "answer")
    return text if reply == "ok" else ""


def ask_yes_no(question):
    return panel.ask(question, "confirm")[0] == "ok"


def notify(message):
    print(message)
    panel.fly_in(message, "goal")


def choose(question, options):
    reply, _ = panel.ask(question, "choose", options=options)
    return panel.choice.get() if reply == "ok" else None


def take_over(message):
    """Hand-over: the sheet lifts and the switch shows Human; switching back to Agent (or the
    panel's button) hands back."""
    guard.stop()
    sheet.hide()
    if session:
        toggle.show("human")
        toggle.on_hand_back = lambda: panel.press("ok")
    try:
        return panel.ask(message, "handoff")[0] == "ok"
    finally:
        toggle.on_hand_back = None
        hooks.TAKEOVER.clear()
        if session:
            agent_working()


def review_steps(lines, allowed):
    reply, _ = panel.ask("This is what I saw you do. Untick anything that should not be saved - "
                         "saved steps are handed to a person again next time.", "review", lines, allowed)
    if reply == "cancel":
        return None
    if reply == "skip":
        return []
    return [i for i, var in enumerate(panel.check_vars) if var.get()]


hooks.ask_text, hooks.ask_yes_no, hooks.notify = ask_text, ask_yes_no, notify
hooks.take_over, hooks.review_steps, hooks.choose = take_over, review_steps, choose
hooks.heartbeat = heartbeat


# ---- main loop: every Tk call stays on this thread; hotkey callbacks only queue jobs
def main():
    running = {"busy": False}

    def on_abort():
        if running["busy"]:
            hooks.ABORT.set()
            print("Abort requested - stopping before the next step...")

    # suppress=True: the key combo is not passed on to BankAPP
    keyboard.add_hotkey(RUN_KEY, lambda: jobs.put(("show", None)), suppress=True)
    keyboard.add_hotkey(ABORT_KEY, on_abort, suppress=True)
    keyboard.add_hotkey(QUIT_KEY, lambda: jobs.put(("quit", None)), suppress=True)
    print(f"Ready. Waiting for '{APP_TITLE}'.  {RUN_KEY} = show panel   "
          f"{ABORT_KEY} = abort   {QUIT_KEY} = quit")

    app_open, next_poll = False, 0.0
    while True:
        try:
            job, goal = jobs.get(timeout=0.2)
        except queue.Empty:
            root.update()                           # keep Tk responsive while idle
            if time.monotonic() >= next_poll:       # panel appears when the app starts
                next_poll = time.monotonic() + POLL_SECONDS
                is_open = capture.find_window(APP_TITLE) is not None
                if is_open and not app_open:
                    panel.fly_in(f"{APP_TITLE} detected. {IDLE_TEXT}")
                elif app_open and not is_open:
                    panel.fly_out()
                app_open = is_open
            continue
        if job == "quit":
            break
        if job == "show":
            panel.fly_in(IDLE_TEXT)
            continue
        panel.fly_out()
        running["busy"] = True
        hwnd = capture.find_window(APP_TITLE)
        try:
            if hwnd:
                session.update(hwnd=hwnd, pid=capture.window_pid(hwnd))
                agent_working()
            result = runner.run_goal(goal, confirm=False).status
            print(f"Result: {result}")
        except Exception as e:                      # never let one failure kill the listener
            notify(f"Unexpected error: {e}")
        finally:
            guard.stop()                            # your mouse and keyboard work everywhere again
            sheet.hide()
            toggle.hide()
            session.clear()
            running["busy"] = False
            hooks.ABORT.clear()
            hooks.TAKEOVER.clear()
    print("Listener stopped.")


if __name__ == "__main__":
    main()
