"""
surface.py - the seam between "how we perceive and act on an application" and the recorded flow.

Discovery, replay and hand-over only talk to a Surface. A surface turns the application's current
window into a Screen (elements with a role, a label / text and a box - see parse_screen.py) and acts
on such an element ("click the 'Save' button", "type into the 'Email' field"). Capabilities store
targets the same way - role + visible label - so the same artifact can be driven through another
surface that produces the same kind of Screen:

    DesktopOcrSurface   any Windows app: screenshot + OCR + OpenCV, mouse and keyboard   (built)
    UiaSurface          native controls through UI Automation / Win32 text                (design)
    WebSurface          DOM / accessibility tree of a (legacy) web app via Playwright       (design)

The tests drive the whole replay engine through a FakeSurface (tests/fakes.py).
"""
import time
from dataclasses import dataclass, field
from typing import Protocol

from cua.errors import Stop, Stuck


@dataclass
class Screen:
    """One window as the agent sees it."""
    title: str
    view: dict                       # compact text view (parse_screen), what an LLM would get masked
    elements: list                   # every element: id, kind, text/key, box, click_screen...
    skeleton: dict                   # static controls only: identifies the window, detects drift
    hwnd: object = None              # the surface's handle of the window
    cap: dict = field(default_factory=dict)
    image: object = None             # screenshot (PIL) - saved blurred, never sent anywhere
    node: str | None = None          # node on the map of the app (set by the agent)


class Surface(Protocol):
    main: object                                     # handle of the app's main window
    pid: int

    def foreground(self): ...                        # handle of the window in front (any app)
    def owns(self, handle) -> bool: ...              # belongs to the application?
    def windows(self) -> list: ...                   # the app's titled windows, topmost first
    def title(self, handle) -> str: ...
    def kind(self, handle) -> str: ...               # main | dialog | messagebox
    def owner(self, handle): ...                     # window that opened it
    def covering(self, handle) -> list: ...          # app windows on top of it
    def front(self, handle, maximize=False) -> None: ...
    def close(self, handle) -> None: ...
    def capture(self) -> Screen | None: ...          # the app window in front; None if not the app
    def act(self, event, el, screen, text=None) -> None: ...
    def refocus(self, screen) -> None: ...
    def wait(self, seconds) -> None: ...


class DesktopOcrSurface:
    """Windows app through screenshots + Windows OCR + OpenCV; acts with the real mouse / keyboard."""
    def __init__(self, title_part, settle=0.8):
        from cua.perception import capture           # Windows-only modules load here, not in tests
        import pyautogui                             # (capture first: it sets DPI awareness)
        from cua.perception.parse_screen import analyze
        self.capture_mod, self.gui, self.analyze = capture, pyautogui, analyze
        self.settle = settle
        self.main = capture.find_window(title_part)
        self.pid = capture.window_pid(self.main) if self.main else None

    # ------------------------------------------------ windows
    def foreground(self):
        return self.capture_mod.user32.GetForegroundWindow()

    def owns(self, handle):
        return bool(handle) and self.capture_mod.window_pid(handle) == self.pid

    def windows(self):
        return self.capture_mod.app_windows(self.pid)

    def title(self, handle):
        return self.capture_mod.window_title(handle) if handle else ""

    def kind(self, handle):
        if handle and handle == self.main:
            return "main"
        return "messagebox" if handle and self.capture_mod.window_class(handle) == "#32770" else "dialog"

    def owner(self, handle):
        return self.capture_mod.owner(handle)

    def covering(self, handle):
        return self.capture_mod.covering(handle, self.pid)

    def front(self, handle, maximize=False):
        self.capture_mod.bring_to_front(handle, maximize=maximize)

    def close(self, handle):
        self.capture_mod.close_window(handle)

    def wait(self, seconds):
        time.sleep(seconds)

    # ------------------------------------------------ see
    def capture(self):
        cap, img, hwnd, pid = self.capture_mod.capture_foreground()
        if pid != self.pid:
            return None
        view, elements, skeleton = self.analyze(cap, img)
        return Screen(cap["window_title"], view, elements, skeleton, hwnd, cap, img)

    # ------------------------------------------------ act
    def act(self, event, el, screen, text=None):
        """text = the concrete value to type / select (placeholders already filled by the agent)."""
        gui, cap = self.gui, self.capture_mod
        t = event["type"]
        if t in ("click", "double_click", "type", "select") and el is None:
            raise Stop(f"{t} needs a target", code="bad_step")
        if el is not None:
            x, y = el["click_screen"]
            if cap.pid_at(x, y) != self.pid:                       # never click into another program
                raise Stop(f"refusing to act at ({x}, {y}): not inside the application", code="not_permitted")
            if cap.root_at(x, y) != screen.hwnd:                   # ...nor into another window of the app
                raise Stuck(f"'{el['key']}' is covered by another window", code="target_covered")
        if t == "click":
            gui.click(x, y)
        elif t == "double_click":
            gui.doubleClick(x, y)
        elif t == "type":
            gui.click(x, y)
            gui.press("end")                                       # shift+home does not always select
            gui.press("backspace", presses=2 * len(el.get("value") or "") + 5)   # (NumLock quirk)
            self._type(text or "")
        elif t == "select":
            self._choose_option(el, text or "", screen)
        elif t == "key":
            if self.foreground() != screen.hwnd:                   # keys go to the window in front
                self.front(screen.hwnd)
                time.sleep(0.3)
            gui.hotkey(*[k.strip() for k in event["keys"].lower().split("+")])
        elif t == "wait":
            time.sleep(float(event.get("seconds", 1)))
        else:
            raise Stop(f"unknown event type '{t}'", code="bad_step")
        time.sleep(self.settle)

    def _type(self, text):
        if text.isascii():
            self.gui.write(text, interval=0.02)
        else:                                                      # non-ASCII: paste via clipboard
            import pyperclip
            pyperclip.copy(text)
            self.gui.hotkey("ctrl", "v")

    def _choose_option(self, el, value, screen):
        """Open a dropdown, OCR the list that appears, click the wanted option."""
        from cua.perception.parse_screen import center
        norm = lambda v: " ".join(str(v).lower().split())
        if norm(el.get("value", "")) == norm(value):
            return                                                 # already selected
        self.gui.click(*el["click_screen"])
        time.sleep(0.6)
        ox, oy = screen.cap["window_rect"][:2]
        x0, y0, x1, y1 = el["box"]
        below = (ox + x0, oy + y1, ox + x1 + 40, oy + y1 + 300)
        above = (ox + x0, oy + y0 - 300, ox + x1 + 40, oy + y0)
        import difflib
        sim = lambda a, b: difflib.SequenceMatcher(None, norm(a), norm(b)).ratio()
        for region in (below, above):
            lines = self.capture_mod.ocr_region(region)
            best = max(lines, key=lambda l: sim(l["text"], value), default=None)
            if best and sim(best["text"], value) >= 0.8:
                self.gui.click(*[round(c) for c in center(best["box"])])
                return
        self.gui.press("esc")
        raise Stuck(f"option '{value}' not found in dropdown '{el['key']}'", code="target_missing")

    def refocus(self, screen):
        """Pop-ups steal focus; put the app window back and make sure it did not move or resize."""
        cap = self.capture_mod
        cap.bring_to_front(screen.hwnd)
        time.sleep(0.3)
        if not self.owns(self.foreground()):
            raise Stop("could not bring the application back to the front", code="focus_lost")
        if list(cap.client_rect_on_screen(screen.hwnd)) != list(screen.cap["window_rect"]):
            raise Stop("the application window moved or changed size", code="focus_lost")
