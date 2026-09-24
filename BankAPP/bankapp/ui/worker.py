"""Runs service calls off the Tk thread so slow operations never freeze the window."""

from __future__ import annotations

import queue
import threading
import time
import tkinter as tk
from collections.abc import Callable
from tkinter import ttk
from typing import Any

from bankapp.ui import style

BUSY_DELAY_MS = 250
POLL_MS = 50


class BusyDialog(tk.Toplevel):
    """Modal 'Processing...' box shown while a slow call is in flight."""

    def __init__(self, root: tk.Misc, message: str) -> None:
        self._previous_grab = root.grab_current()
        owner = self._previous_grab or root
        super().__init__(owner)
        self.title("Please Wait")
        self.configure(bg=style.BG)
        self.resizable(False, False)
        self.transient(owner.winfo_toplevel())
        self.protocol("WM_DELETE_WINDOW", lambda: None)

        frame = ttk.Frame(self, padding=14)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=message).pack(anchor="w")
        bar = ttk.Progressbar(frame, mode="indeterminate", length=240)
        bar.pack(pady=(8, 0))
        bar.start(12)

        self.update_idletasks()
        top = owner.winfo_toplevel()
        x = top.winfo_rootx() + (top.winfo_width() - self.winfo_width()) // 2
        y = top.winfo_rooty() + (top.winfo_height() - self.winfo_height()) // 2
        self.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        self.grab_set()

    def close(self) -> None:
        self.grab_release()
        self.destroy()
        previous = self._previous_grab
        if previous is not None and previous.winfo_exists():
            previous.grab_set()


class Worker:
    def __init__(self, root: tk.Tk) -> None:
        self._root = root
        self.busy = False

    def run(
        self,
        fn: Callable[[], Any],
        on_success: Callable[[Any], None],
        on_error: Callable[[BaseException], None],
        message: str,
    ) -> None:
        if self.busy:  # ignore double-clicks while a call is in flight
            return
        self.busy = True
        result: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)

        def target() -> None:
            try:
                result.put((True, fn()))
            except Exception as exc:  # every failure is routed to the UI error handler
                result.put((False, exc))

        threading.Thread(target=target, daemon=True).start()
        started = time.monotonic()
        busy_dialog: BusyDialog | None = None

        def poll() -> None:
            nonlocal busy_dialog
            try:
                ok, value = result.get_nowait()
            except queue.Empty:
                waited_ms = (time.monotonic() - started) * 1000
                if busy_dialog is None and waited_ms >= BUSY_DELAY_MS:
                    busy_dialog = BusyDialog(self._root, message)
                self._root.after(POLL_MS, poll)
                return
            if busy_dialog is not None:
                busy_dialog.close()
            self.busy = False
            (on_success if ok else on_error)(value)

        self._root.after(POLL_MS, poll)
