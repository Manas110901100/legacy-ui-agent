"""
capture.py - Windows-only screen capture + built-in Windows OCR.

capture_foreground() -> (capture_dict, PIL image, hwnd, pid)
    Captures only the CLIENT area of the foreground window (no title bar),
    so the window title comes from Windows itself, not OCR.
ocr_region(bbox)     -> OCR lines for any screen rectangle (used for dropdown lists)

pip install winsdk pillow
"""
import asyncio
import ctypes
import time
from ctypes import wintypes

from PIL import Image, ImageChops, ImageGrab
import winsdk.windows.graphics.imaging as imaging
import winsdk.windows.media.ocr as ocr
import winsdk.windows.storage.streams as streams

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)   # real pixel coordinates on scaled displays
except Exception:
    pass

user32 = ctypes.windll.user32
SCALE = 2                                              # upscale before OCR; helps small fonts
CARET_FRAMES, CARET_GAP = 3, 0.3                       # 3 grabs 0.3 s apart span any caret blink cycle
CARET_MAX_WIDTH = 6                                    # px; wider changes = real UI change, not the caret


# ------------------------------------------------------------------ windows

def window_title(hwnd):
    buf = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, buf, 512)
    return buf.value


def window_pid(hwnd):
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def client_rect_on_screen(hwnd):
    r = wintypes.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(r))
    pt = wintypes.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(pt))
    return (pt.x, pt.y, pt.x + r.right, pt.y + r.bottom)


def find_window(title_part):
    """First visible top-level window whose title contains title_part."""
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd) and title_part.lower() in window_title(hwnd).lower():
            found.append(hwnd)
        return True

    user32.EnumWindows(cb, 0)
    return found[0] if found else None


def bring_to_front(hwnd, maximize=False):
    SW_RESTORE, SW_MAXIMIZE = 9, 3
    if maximize:
        user32.ShowWindow(hwnd, SW_MAXIMIZE)
    elif user32.IsIconic(hwnd):          # only un-minimise; never resize a maximised window
        user32.ShowWindow(hwnd, SW_RESTORE)
    # Windows blocks focus stealing; a tap of ALT lets SetForegroundWindow succeed
    user32.keybd_event(0x12, 0, 0, 0)
    user32.keybd_event(0x12, 0, 2, 0)
    user32.SetForegroundWindow(hwnd)


user32.WindowFromPoint.argtypes = [wintypes.POINT]
user32.WindowFromPoint.restype = wintypes.HWND
user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetAncestor.restype = wintypes.HWND


def root_at(x, y):
    """Top-level window under a screen point."""
    hwnd = user32.WindowFromPoint(wintypes.POINT(int(x), int(y)))
    return (user32.GetAncestor(hwnd, 2) or hwnd) if hwnd else None     # 2 = GA_ROOT


def pid_at(x, y):
    """Process id of the window under a screen point (used to never click outside the app)."""
    root = root_at(x, y)
    return window_pid(root) if root else None


user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetWindow.restype = wintypes.HWND


def owner(hwnd):
    """The window that opened this one (a dialog's owner), or None."""
    return user32.GetWindow(hwnd, 4)                                     # 4 = GW_OWNER


def window_class(hwnd):
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def window_rect(hwnd):
    r = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return (r.left, r.top, r.right, r.bottom)


def app_windows(pid):
    """Visible, titled top-level windows of a process, topmost first.
    Untitled ones are helpers (combobox drop-down lists and the like), not windows a user works in."""
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd) and not user32.IsIconic(hwnd) and window_pid(hwnd) == pid \
                and window_title(hwnd).strip():
            found.append(hwnd)
        return True

    user32.EnumWindows(cb, 0)
    return found


def covering(hwnd, pid):
    """Windows of the same app that lie on top of hwnd and overlap it (they pollute a screenshot)."""
    x0, y0, x1, y1 = window_rect(hwnd)
    over = []
    for h in app_windows(pid):
        if h == hwnd:
            break
        a0, b0, a1, b1 = window_rect(h)
        if a1 - a0 < 40 or b1 - b0 < 20:                                 # tooltips and similar
            continue
        if a0 < x1 and x0 < a1 and b0 < y1 and y0 < b1:
            over.append(h)
    return over


def close_window(hwnd):
    user32.PostMessageW(hwnd, 0x0010, 0, 0)                              # WM_CLOSE


# ------------------------------------------------------------------ OCR

async def _ocr_async(img):
    engine = ocr.OcrEngine.try_create_from_user_profile_languages()
    if engine is None:
        raise RuntimeError("No OCR language installed (Windows Settings > Language).")
    scale = SCALE if max(img.size) * SCALE <= ocr.OcrEngine.max_image_dimension else 1
    big = img.resize((img.width * scale, img.height * scale), Image.LANCZOS).convert("RGBA")
    writer = streams.DataWriter()
    writer.write_bytes(big.tobytes())
    bitmap = imaging.SoftwareBitmap(imaging.BitmapPixelFormat.RGBA8, big.width, big.height)
    bitmap.copy_from_buffer(writer.detach_buffer())
    result = await engine.recognize_async(bitmap)

    lines = []
    for i, line in enumerate(result.lines, start=1):
        words = []
        for w in line.words:
            b = w.bounding_rect
            words.append({"text": w.text,
                          "box": [round(b.x / scale), round(b.y / scale),
                                  round((b.x + b.width) / scale), round((b.y + b.height) / scale)]})
        if words:
            lines.append({"id": f"t{i}", "text": line.text, "words": words,
                          "box": [min(w["box"][0] for w in words), min(w["box"][1] for w in words),
                                  max(w["box"][2] for w in words), max(w["box"][3] for w in words)]})
    return lines


def ocr_image(img):
    return asyncio.run(_ocr_async(img))


def ocr_region(bbox):
    """OCR a screen rectangle; returned boxes are in SCREEN coordinates."""
    img = ImageGrab.grab(bbox=bbox, all_screens=True)
    lines = ocr_image(img)
    for ln in lines:
        for item in [ln] + ln["words"]:
            b = item["box"]
            item["box"] = [b[0] + bbox[0], b[1] + bbox[1], b[2] + bbox[0], b[3] + bbox[1]]
    return lines


# ------------------------------------------------------------------ capture

def grab_without_caret(rect):
    """The blinking text cursor in a focused field is OCR'd as a trailing 'l' / '1' / '|'.
    Grab a few frames across one blink cycle and keep the lighter pixel -> dark caret gone.
    If the frames differ in more than a caret-thin strip, the UI is still changing: use the last."""
    frames = [ImageGrab.grab(bbox=rect, all_screens=True)]
    for _ in range(CARET_FRAMES - 1):
        time.sleep(CARET_GAP)
        frames.append(ImageGrab.grab(bbox=rect, all_screens=True))
    return merge_frames(frames)


def merge_frames(frames):
    """Frames of the same window, oldest first -> one caret-free image (see grab_without_caret)."""
    merged = frames[0]
    for f in frames[1:]:
        changed = ImageChops.difference(merged.convert("L"), f.convert("L")).getbbox()
        if changed and changed[2] - changed[0] > CARET_MAX_WIDTH:
            return frames[-1]
        merged = ImageChops.lighter(merged, f)
    return merged


def capture_foreground():
    hwnd = user32.GetForegroundWindow()
    rect = client_rect_on_screen(hwnd)
    img = grab_without_caret(rect)
    cap = {"window_title": window_title(hwnd), "window_rect": list(rect),
           "image_size": list(img.size), "lines": ocr_image(img)}
    return cap, img, hwnd, window_pid(hwnd)