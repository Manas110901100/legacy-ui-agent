"""
Ctrl+Shift+A -> screenshot (active window) -> Windows built-in OCR ->
saves raw PNG, annotated PNG (boxes drawn) and JSON (text + boxes) to ./captures
Ctrl+Shift+Q -> quit

Install:  pip install winsdk pillow keyboard
"""
import asyncio
import ctypes
import json
import queue
import time
from ctypes import wintypes
from pathlib import Path

import keyboard
from PIL import Image, ImageDraw, ImageGrab
import winsdk.windows.graphics.imaging as imaging
import winsdk.windows.media.ocr as ocr
import winsdk.windows.storage.streams as streams

HOTKEY = "ctrl+shift+a"
QUIT_HOTKEY = "ctrl+shift+q"
ACTIVE_WINDOW_ONLY = True   # False = capture all screens
SCALE = 2                   # upscale before OCR; helps small old-style fonts
OUT_DIR = Path("captures")

# Make coordinates match real pixels on high-DPI / scaled displays
ctypes.windll.shcore.SetProcessDpiAwareness(2)
user32 = ctypes.windll.user32


def grab():
    """Return (image, window_rect, window_title)."""
    if ACTIVE_WINDOW_ONLY:
        hwnd = user32.GetForegroundWindow()
        r = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        title = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, title, 256)
        rect = (r.left, r.top, r.right, r.bottom)
        return ImageGrab.grab(bbox=rect, all_screens=True), rect, title.value
    img = ImageGrab.grab(all_screens=True)
    return img, (0, 0, img.width, img.height), "Full screen"


async def run_ocr(img):
    engine = ocr.OcrEngine.try_create_from_user_profile_languages()
    if engine is None:
        raise RuntimeError("No OCR language installed. Add one in Windows Settings > Language.")

    scale = SCALE
    if max(img.size) * scale > ocr.OcrEngine.max_image_dimension:
        scale = 1
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
            box = [round(b.x / scale), round(b.y / scale),
                   round((b.x + b.width) / scale), round((b.y + b.height) / scale)]
            words.append({"text": w.text, "box": box})
        if not words:
            continue
        line_box = [min(w["box"][0] for w in words), min(w["box"][1] for w in words),
                    max(w["box"][2] for w in words), max(w["box"][3] for w in words)]
        lines.append({"id": f"t{i}", "text": line.text, "box": line_box, "words": words})
    return lines


def save(img, lines, rect, title):
    OUT_DIR.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")

    img.save(OUT_DIR / f"{stamp}_raw.png")

    ann = img.convert("RGB")
    d = ImageDraw.Draw(ann)
    for ln in lines:
        for w in ln["words"]:
            d.rectangle(w["box"], outline="blue", width=1)
        d.rectangle(ln["box"], outline="red", width=2)
        d.text((ln["box"][0], max(0, ln["box"][1] - 11)), ln["id"], fill="red")
    ann.save(OUT_DIR / f"{stamp}_boxes.png")

    data = {
        "window_title": title,
        "window_rect": rect,          # screen position; boxes are relative to this
        "image_size": list(img.size),
        "lines": lines,
    }
    (OUT_DIR / f"{stamp}.json").write_text(json.dumps(data, indent=2, ensure_ascii=False),
                                           encoding="utf-8")
    print(f"Saved {stamp}: {len(lines)} text lines from '{title}'")


def main():
    jobs = queue.Queue()
    keyboard.add_hotkey(HOTKEY, lambda: jobs.put("capture"))
    keyboard.add_hotkey(QUIT_HOTKEY, lambda: jobs.put("quit"))
    print(f"Ready. {HOTKEY} = capture, {QUIT_HOTKEY} = quit")

    while True:
        job = jobs.get()          # work runs on the main thread (WinRT-friendly)
        if job == "quit":
            break
        time.sleep(0.2)           # let the hotkey release before capturing
        try:
            img, rect, title = grab()
            lines = asyncio.run(run_ocr(img))
            save(img, lines, rect, title)
        except Exception as e:
            print("Error:", e)


if __name__ == "__main__":
    main()