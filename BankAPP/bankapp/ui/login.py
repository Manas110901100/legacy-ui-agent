"""Log-on screen."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bankapp.ui.app import App


class LoginFrame(ttk.Frame):
    def __init__(self, master: tk.Misc, app: App) -> None:
        super().__init__(master)
        self.app = app

        box = ttk.LabelFrame(self, text=" Log On ", padding=16)
        box.place(relx=0.5, rely=0.45, anchor="center")

        ttk.Label(box, text="BANKAPP", style="Title.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w"
        )
        ttk.Label(box, text="Core Banking System  v1.0").grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(0, 12)
        )

        ttk.Label(box, text="User name:").grid(row=2, column=0, sticky="w", pady=3)
        self.username = ttk.Entry(box, width=28)
        self.username.grid(row=2, column=1, pady=3)
        ttk.Label(box, text="Password:").grid(row=3, column=0, sticky="w", pady=3)
        self.password = ttk.Entry(box, width=28, show="*")
        self.password.grid(row=3, column=1, pady=3)

        buttons = ttk.Frame(box)
        buttons.grid(row=4, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(buttons, text="OK", width=10, command=self.submit).pack(side="left", padx=2)
        ttk.Button(buttons, text="Exit", width=10, command=app.exit).pack(side="left", padx=2)

        ttk.Separator(box).grid(row=5, column=0, columnspan=2, sticky="ew", pady=10)
        ttk.Label(
            box,
            text="Demo: admin/admin123   teller/teller123   viewer/viewer123",
            style="Hint.TLabel",
        ).grid(row=6, column=0, columnspan=2, sticky="w")

        for entry in (self.username, self.password):
            entry.bind("<Return>", lambda _e: self.submit())
        self.after_idle(self.username.focus_set)

    def submit(self) -> None:
        username, password = self.username.get(), self.password.get()
        fields: dict[str, tk.Widget] = {"username": self.username, "password": self.password}
        self.app.call(
            lambda _confirmed: self.app.service.login(username, password),
            self.app.on_login,
            fields=fields,
            message="Verifying credentials...",
        )
