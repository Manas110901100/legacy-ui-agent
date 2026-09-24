"""
recorder.py - watch what a person does in the application while the agent has handed over control.

    rec = Recorder(app_pid); rec.start()
    ... the person works in the app ...
    actions, final = rec.stop()
    steps, end = interpret(actions, final)
    # steps: [{"type", "el", "text"/"value"/"keys", "view", "skeleton", "hwnd",
    #          "after", "after_hwnd"}]  - after = screen a click / key led to (for the map)

Only clicks / keys that land in the application's own windows are recorded (not the agent's
panel, not other programs). While recording, just the frames are kept; OCR + parsing happen in
interpret(), after the person hands back. Nothing here talks to GPT-4o.
"""
import ctypes
import threading
import time
from collections import deque
from ctypes import wintypes

import keyboard
from PIL import ImageGrab

from cua.perception import capture
from cua.perception.parse_screen import analyze

FRAME_EVERY = 0.2                   # seconds between background grabs of the app window
FRAME_KEEP = 4                      # frames kept per click (≈0.8 s: spans one caret blink)
CLICKABLE = ("button", "menu_item", "input", "dropdown", "table_row", "column_header", "scrollbar")
KEYS = ("enter", "tab", "esc")

user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
WH_MOUSE_LL, WM_LBUTTONDOWN, WM_QUIT = 14, 0x0201, 0x0012
LRESULT = ctypes.c_ssize_t
HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("pt", wintypes.POINT), ("mouseData", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
user32.SetWindowsHookExW.restype = wintypes.HHOOK
user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
user32.CallNextHookEx.restype = LRESULT
user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE


class Recorder:
    def __init__(self, app_pid):
        self.app_pid = app_pid
        self.frames = deque(maxlen=FRAME_KEEP)   # (time, rect, title, image) of the app's foreground window
        self.events = []                         # (time, kind, data, frames)
        self.lock = threading.Lock()
        self.running = threading.Event()
        self._tid = None
        self._kb = None

    # ------------------------------------------------ start / stop
    def start(self):
        self.running.set()
        threading.Thread(target=self._grab_loop, daemon=True).start()
        threading.Thread(target=self._mouse_loop, daemon=True).start()
        self._kb = keyboard.hook(self._on_key)

    def stop(self):
        """-> (events in time order, final state (rect, title, image) or None)."""
        self.running.clear()
        if self._kb:
            keyboard.unhook(self._kb)
        if self._tid:
            user32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)
        final = None
        hwnd = user32.GetForegroundWindow()
        if capture.window_pid(hwnd) == self.app_pid:
            rect = capture.client_rect_on_screen(hwnd)
            final = (rect, capture.window_title(hwnd), capture.grab_without_caret(rect), hwnd)
        return sorted(self.events, key=lambda e: e[0]), final

    # ------------------------------------------------ background threads
    def _grab_loop(self):
        while self.running.is_set():
            hwnd = user32.GetForegroundWindow()
            if capture.window_pid(hwnd) == self.app_pid:
                rect = capture.client_rect_on_screen(hwnd)
                if rect[2] > rect[0] and rect[3] > rect[1]:
                    img = ImageGrab.grab(bbox=rect, all_screens=True)
                    with self.lock:
                        self.frames.append((time.monotonic(), rect, capture.window_title(hwnd), img, hwnd))
            time.sleep(FRAME_EVERY)

    def _snapshot(self):
        with self.lock:
            return list(self.frames)

    def _mouse_loop(self):
        self._tid = kernel32.GetCurrentThreadId()

        @HOOKPROC
        def proc(n, wparam, lparam):
            if n == 0 and wparam == WM_LBUTTONDOWN:
                pt = ctypes.cast(lparam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents.pt
                if capture.pid_at(pt.x, pt.y) == self.app_pid:      # ignore the panel and other programs
                    self.events.append((time.monotonic(), "click", (pt.x, pt.y), self._snapshot()))
            return user32.CallNextHookEx(None, n, wparam, lparam)

        hook = user32.SetWindowsHookExW(WH_MOUSE_LL, proc, kernel32.GetModuleHandleW(None), 0)
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            pass
        user32.UnhookWindowsHookEx(hook)

    def _on_key(self, e):
        if e.event_type != "down" or capture.window_pid(user32.GetForegroundWindow()) != self.app_pid:
            return
        name = e.name or ""
        t = time.monotonic()
        if len(name) == 1 and (keyboard.is_pressed("ctrl") or keyboard.is_pressed("alt")):
            return                                   # shortcuts (ctrl+c, the agent's hotkeys) are not text
        if len(name) == 1:
            self.events.append((t, "char", name, None))
        elif name == "space":
            self.events.append((t, "char", " ", None))
        elif name == "backspace":
            self.events.append((t, "backspace", None, None))
        elif name in KEYS:
            self.events.append((t, "key", name, self._snapshot()))


# ---------------------------------------------------------------- interpretation

def parse_frames(frames):
    """Frames just before an action -> parsed screen (same shape as agent.observe)."""
    rect, title, hwnd = frames[-1][1], frames[-1][2], frames[-1][4]
    img = capture.merge_frames([f[3] for f in frames if f[1] == rect])
    return parse_image(rect, title, img, hwnd)


def parse_image(rect, title, img, hwnd=None):
    cap = {"window_title": title, "window_rect": list(rect), "image_size": list(img.size),
           "lines": capture.ocr_image(img)}
    view, elements, skeleton = analyze(cap, img)
    return {"rect": rect, "view": view, "elements": elements, "skeleton": skeleton, "hwnd": hwnd}


def element_at(parsed, x, y):
    """Smallest clickable element under a screen point."""
    lx, ly = x - parsed["rect"][0], y - parsed["rect"][1]
    hits = [e for e in parsed["elements"] if e["kind"] in CLICKABLE
            and e["box"][0] - 2 <= lx <= e["box"][2] + 2 and e["box"][1] - 2 <= ly <= e["box"][3] + 2]
    return min(hits, key=lambda e: (e["box"][2] - e["box"][0]) * (e["box"][3] - e["box"][1]), default=None)


def _screens(before, after):
    """Screen an action was done on, and (for clicks / keys) the screen it led to - for the map."""
    return {"view": before["view"], "skeleton": before["skeleton"], "hwnd": before["hwnd"],
            "after": after["skeleton"] if after else None, "after_hwnd": after["hwnd"] if after else None}


def _field_values(parsed, kind):
    return {e["key"]: e.get("value", "") for e in parsed["elements"] if e["kind"] == kind} if parsed else {}


def interpret(events, final):
    """Recorded events -> steps. Each step: type, el (element hit, None if not recognised),
    text / value / keys, plus the view + skeleton of the screen it was done on."""
    # 1. anchors = clicks and keys (each with the frames before it); text typed in between
    anchors, text, typed = [], [], []
    last_click = None
    for t, kind, data, frames in events:
        if kind == "char":
            text.append(data)
        elif kind == "backspace":
            if text:
                text.pop()
        elif frames:                             # no frame yet (app not in front) -> cannot say what was hit
            if kind == "click" and last_click and t - last_click[0] <= user32.GetDoubleClickTime() / 1000 \
                    and abs(data[0] - last_click[1][0]) <= 4 and abs(data[1] - last_click[1][1]) <= 4 and not text:
                anchors[-1]["type"] = "double_click"
                last_click = None
                continue
            typed.append("".join(text))
            text = []
            anchors.append({"type": "click" if kind == "click" else "key", "data": data, "frames": frames})
            last_click = (t, data) if kind == "click" else None
    typed.append("".join(text))                  # typed[k] = text typed before anchor k (typed[-1]: at the end)

    parsed = [parse_frames(a["frames"]) for a in anchors]
    parsed.append(parse_image(*final) if final else None)

    # 2. anchors -> steps
    steps, focus, skip = [], None, set()
    if typed[0] and anchors:                                      # typed before the first click: field unknown
        p = parsed[0]
        steps.append({"type": "type", "el": None, "text": typed[0], **_screens(p, None)})
    for k, a in enumerate(anchors):
        p, nxt = parsed[k], parsed[k + 1]
        if k in skip:
            continue
        if a["type"] == "key":
            if a["data"] != "tab" or not typed[k + 1]:          # tab before typing only moves focus
                steps.append({"type": "key", "el": None, "keys": a["data"], **_screens(p, nxt)})
            focus = None if a["data"] != "tab" else "tab"
        else:
            el = element_at(p, *a["data"])
            if el and el["kind"] == "dropdown":
                chosen = parsed[k + 2] if k + 2 < len(parsed) else nxt
                value = _field_values(chosen, "dropdown").get(el["key"], "")
                if value:
                    steps.append({"type": "select", "el": el, "value": value, **_screens(p, None)})
                skip.add(k + 1)                                   # the click on the option in the list
                continue
            if el and el["kind"] == "input" and a["type"] == "click":
                focus = el                                        # a click into a field: typing follows
            else:
                steps.append({"type": a["type"], "el": el, "point": a["data"], **_screens(p, nxt)})
                focus = None
        chars = typed[k + 1]
        if chars:
            target = focus if isinstance(focus, dict) else None
            if target is None and nxt:                            # tabbed into a field: the one that changed
                before, after = _field_values(p, "input"), _field_values(nxt, "input")
                changed = [key for key, v in after.items() if before.get(key) != v]
                target = next((e for e in p["elements"] if e["kind"] == "input" and e["key"] == changed[0]),
                              None) if len(changed) == 1 else None
            steps.append({"type": "type", "el": target, "text": chars, **_screens(p, None)})
            focus = None
    return steps, (parsed[-1]["skeleton"] if parsed[-1] else None)
