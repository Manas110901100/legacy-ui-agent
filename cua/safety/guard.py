"""
guard.py - the "sheet": while the agent works, a person's clicks and typing into the application
are ignored, so a run cannot be disturbed by accident. The Agent/Human toggle is the way in:
clicking it asks the agent to hand over at its next step.

    guard = InputGuard()            # once; installs low-level mouse + keyboard hooks, passive
    guard.on_take_over = callback   # called (from the hook thread) when the toggle is clicked
    guard.start(app_pid)            # sheet on
    guard.alive()                   # the agent is still working (watchdog)
    guard.stop()                    # sheet off

Input the agent itself sends (pyautogui) carries Windows' "injected" flag and always passes.
Mouse movement always passes (the pyautogui corner fail-safe keeps working), and so do other
programs, the agent's own panel and Ctrl+Shift hotkeys. If the agent shows no sign of life for
WATCHDOG seconds, the sheet stops blocking until it does.
"""
import ctypes
import threading
import time
from ctypes import wintypes

from cua.operator.recorder import HOOKPROC, MSLLHOOKSTRUCT   # same hook types the recorder uses
from cua.perception import capture

WATCHDOG = 45
WH_KEYBOARD_LL, WH_MOUSE_LL, WM_QUIT = 13, 14, 0x0012
LLMHF_INJECTED, LLKHF_INJECTED = 0x01, 0x10
WM_LBUTTONDOWN = 0x0201
BUTTONS = set(range(0x0201, 0x020F))            # every button / wheel message; not WM_MOUSEMOVE
MODIFIERS = {0x10, 0x11, 0x12, 0x5B, 0x5C, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5}   # shift ctrl alt win
VK_SHIFT, VK_CONTROL = 0x10, 0x11

user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
user32.GetAsyncKeyState.restype = ctypes.c_short

PASS, BLOCK, TAKE_OVER = "pass", "block", "take_over"


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


def decide(kind, injected, app_pid, point_pid=None, in_toggle=False, fg_pid=None, ctrl_shift=False,
           modifier=False):
    """What to do with one input event while the sheet is on.
    kind: "click" (any button / wheel), "move" or "key"."""
    if injected or kind == "move":
        return PASS                                      # the agent's own input; pointer movement
    if kind == "click":
        if in_toggle:
            return TAKE_OVER
        return BLOCK if point_pid == app_pid else PASS
    if kind == "key":
        if modifier or ctrl_shift:
            return PASS                                  # hotkeys like Ctrl+Shift+Q must keep working
        return BLOCK if fg_pid == app_pid else PASS
    return PASS


class InputGuard:
    def __init__(self):
        self.blocking = False
        self.app_pid = None
        self.toggle_rect = None                          # screen rect of the toggle, set by the panel
        self.on_take_over = lambda: None
        self.last_alive = time.monotonic()
        threading.Thread(target=self._loop, daemon=True).start()

    def start(self, app_pid):
        self.app_pid = app_pid
        self.alive()
        self.blocking = True

    def stop(self):
        self.blocking = False

    def alive(self):
        self.last_alive = time.monotonic()

    def active(self):
        return self.blocking and time.monotonic() - self.last_alive < WATCHDOG

    def _in_toggle(self, x, y):
        r = self.toggle_rect
        return bool(r) and r[0] <= x < r[2] and r[1] <= y < r[3]

    def _loop(self):
        @HOOKPROC
        def mouse(n, wparam, lparam):
            if n == 0 and wparam in BUTTONS and self.active():
                info = ctypes.cast(lparam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                x, y = info.pt.x, info.pt.y
                verdict = decide("click", bool(info.flags & LLMHF_INJECTED), self.app_pid,
                                 point_pid=capture.pid_at(x, y), in_toggle=self._in_toggle(x, y))
                if verdict == TAKE_OVER and wparam == WM_LBUTTONDOWN:
                    self.on_take_over()
                if verdict != PASS:
                    return 1
            return user32.CallNextHookEx(None, n, wparam, lparam)

        @HOOKPROC
        def keys(n, wparam, lparam):
            if n == 0 and self.active():
                info = ctypes.cast(lparam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                held = lambda vk: bool(user32.GetAsyncKeyState(vk) & 0x8000)
                verdict = decide("key", bool(info.flags & LLKHF_INJECTED), self.app_pid,
                                 fg_pid=capture.window_pid(user32.GetForegroundWindow()),
                                 ctrl_shift=held(VK_CONTROL) and held(VK_SHIFT),
                                 modifier=info.vkCode in MODIFIERS)
                if verdict != PASS:
                    return 1
            return user32.CallNextHookEx(None, n, wparam, lparam)

        module = kernel32.GetModuleHandleW(None)
        hooks = [user32.SetWindowsHookExW(WH_MOUSE_LL, mouse, module, 0),
                 user32.SetWindowsHookExW(WH_KEYBOARD_LL, keys, module, 0)]
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            pass
        for h in hooks:
            user32.UnhookWindowsHookEx(h)
