"""Application shell: screen switching, central error handling, idle-session watchdog."""

from __future__ import annotations

import logging
import tkinter as tk
import uuid
from collections.abc import Callable
from tkinter import messagebox, ttk
from types import TracebackType
from typing import Any

from bankapp.auth import Session
from bankapp.config import Config
from bankapp.errors import (
    AppError,
    BankError,
    ConfirmationRequired,
    NotFoundError,
    PermissionDeniedError,
    SessionExpiredError,
    ValidationError,
)
from bankapp.services import BankService
from bankapp.ui import style
from bankapp.ui.login import LoginFrame
from bankapp.ui.main_window import MainWindow
from bankapp.ui.worker import Worker

log = logging.getLogger("bankapp")

IDLE_CHECK_MS = 1000


class App:
    def __init__(self, root: tk.Tk, service: BankService, config: Config) -> None:
        self.root = root
        self.service = service
        self.config = config
        self.session: Session | None = None
        self.worker = Worker(root)
        self._screen: ttk.Frame | None = None

        root.title("BankAPP - Core Banking System")
        root.geometry("1000x600")
        root.minsize(800, 450)
        style.apply(root)
        root.report_callback_exception = self._on_uncaught
        root.protocol("WM_DELETE_WINDOW", self.exit)
        for sequence in ("<Any-KeyPress>", "<Any-ButtonPress>"):
            root.bind_all(sequence, self._on_activity, add="+")

        self.show_login()
        root.after(IDLE_CHECK_MS, self._check_idle)

    # ----- screens ----------------------------------------------------------------------

    def _swap(self, screen: ttk.Frame) -> None:
        if self._screen is not None:
            self._screen.destroy()
        self._screen = screen
        screen.pack(fill="both", expand=True)

    def show_login(self) -> None:
        self.root.config(menu=tk.Menu(self.root))
        self._swap(LoginFrame(self.root, self))

    def on_login(self, session: Session) -> None:
        self.session = session
        log.info("login user=%s role=%s", session.username, session.role)
        self._swap(MainWindow(self.root, self))

    def require_session(self) -> Session:
        if self.session is None:
            raise SessionExpiredError("You are not logged in.")
        return self.session

    def _close_popups(self) -> None:
        for widget in self.root.winfo_children():
            if isinstance(widget, tk.Toplevel):
                widget.destroy()

    def logout(self) -> None:
        if not messagebox.askyesno(
            "Log Off", "Are you sure you want to log off?", parent=self.root
        ):
            return
        log.info("logout user=%s", self.session.username if self.session else "-")
        self.session = None
        self._close_popups()
        self.show_login()

    def exit(self) -> None:
        if self.session is not None and not messagebox.askyesno(
            "Exit BankAPP",
            "You are still logged on.\nExit the application?",
            icon="warning",
            parent=self.root,
        ):
            return
        self.root.destroy()

    # ----- background calls + error routing ---------------------------------------------

    def call(
        self,
        action: Callable[[bool], Any],
        on_success: Callable[[Any], None],
        *,
        parent: tk.Misc | None = None,
        fields: dict[str, tk.Widget] | None = None,
        message: str = "Processing, please wait...",
    ) -> None:
        """Run action(confirmed) in the background.

        If it raises ConfirmationRequired the user is asked Yes/No and, on Yes, the action is
        re-run with confirmed=True. Every other error is shown by handle_error.
        """

        def attempt(confirmed: bool) -> None:
            self.worker.run(
                lambda: action(confirmed),
                on_success,
                lambda exc: self.handle_error(exc, parent, fields, retry=lambda: attempt(True)),
                message,
            )

        attempt(False)

    def handle_error(
        self,
        exc: BaseException,
        parent: tk.Misc | None = None,
        fields: dict[str, tk.Widget] | None = None,
        retry: Callable[[], None] | None = None,
    ) -> None:
        if parent is None or not parent.winfo_exists():
            parent = self.root

        if isinstance(exc, ConfirmationRequired):
            if retry is not None and messagebox.askyesno(
                exc.title, exc.message, icon="warning", parent=parent
            ):
                retry()
            return
        if isinstance(exc, SessionExpiredError):
            self.expire_session(exc.message)
            return

        if isinstance(exc, ValidationError):
            messagebox.showwarning(exc.title, exc.message, parent=parent)
            widget = (fields or {}).get(exc.field)
            if widget is not None and widget.winfo_exists():
                widget.focus_set()
                if isinstance(widget, ttk.Entry) and str(widget.cget("state")) != "readonly":
                    widget.select_range(0, "end")
        elif isinstance(exc, NotFoundError):
            messagebox.showinfo(exc.title, exc.message, parent=parent)
        elif isinstance(exc, PermissionDeniedError):
            log.warning("permission denied: %s", exc.message)
            messagebox.showerror(exc.title, exc.message, parent=parent)
        elif isinstance(exc, AppError):
            error_id = self._log_failure(exc)
            messagebox.showerror(exc.title, f"{exc.message}\n\nError ID: {error_id}", parent=parent)
        elif isinstance(exc, BankError):
            messagebox.showerror(exc.title, exc.message, parent=parent)
        else:
            self._report_unexpected(exc, parent)

    def _log_failure(self, exc: BaseException) -> str:
        error_id = uuid.uuid4().hex[:8].upper()
        log.error(
            "error_id=%s %s: %s",
            error_id,
            type(exc).__name__,
            exc,
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        return error_id

    def _report_unexpected(self, exc: BaseException, parent: tk.Misc | None = None) -> None:
        error_id = self._log_failure(exc)
        messagebox.showerror(
            "Application Error",
            "An unexpected error occurred.\n\n"
            f"{type(exc).__name__}: {exc}\n\n"
            f"Error ID: {error_id}\n"
            f"Details were written to {self.config.log_path}.\n"
            "The application will continue running.",
            parent=parent or self.root,
        )

    def _on_uncaught(
        self, exc_type: type[BaseException], exc: BaseException, tb: TracebackType | None
    ) -> None:
        """Tk hook for exceptions raised inside any UI callback."""
        exc.__traceback__ = tb
        self._report_unexpected(exc)

    # ----- session timeout --------------------------------------------------------------

    def _on_activity(self, _event: tk.Event) -> None:
        if self.session is not None:
            self.session.touch()

    def _check_idle(self) -> None:
        try:
            if self.session is not None and not self.worker.busy and self.session.is_expired():
                self.expire_session(
                    f"Your session expired after {int(self.session.timeout)} seconds of "
                    "inactivity.\nPlease log in again."
                )
        finally:
            self.root.after(IDLE_CHECK_MS, self._check_idle)

    def expire_session(self, message: str) -> None:
        if self.session is None:
            return
        log.info("session expired user=%s", self.session.username)
        self.session = None
        self._close_popups()
        self.show_login()
        messagebox.showwarning(SessionExpiredError.title, message, parent=self.root)
