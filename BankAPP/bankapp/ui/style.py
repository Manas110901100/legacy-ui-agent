"""Classic Win32 / MFC look: grey chrome, white fields, navy selection, Tahoma 9."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

BG = "#d4d0c8"
FIELD = "#ffffff"
SELECT = "#000080"
DISABLED_TEXT = "#808080"
FONT = ("Tahoma", 9)
BOLD = ("Tahoma", 9, "bold")
TITLE = ("Tahoma", 12, "bold")


def apply(root: tk.Tk) -> None:
    root.configure(bg=BG)
    root.option_add("*Font", FONT)
    root.option_add("*Menu.Font", FONT)

    style = ttk.Style(root)
    style.theme_use("winnative" if "winnative" in style.theme_names() else "classic")
    style.configure(".", background=BG, foreground="#000000", font=FONT)
    style.configure("TEntry", fieldbackground=FIELD)
    style.configure("TCombobox", fieldbackground=FIELD)
    style.configure("Treeview", background=FIELD, fieldbackground=FIELD, rowheight=18)
    style.map("Treeview", background=[("selected", SELECT)], foreground=[("selected", FIELD)])
    style.configure("Treeview.Heading", font=BOLD)
    style.configure("Title.TLabel", font=TITLE)
    style.configure("Bold.TLabel", font=BOLD)
    style.configure("Status.TLabel", relief="sunken", padding=(4, 1))
    style.configure("Hint.TLabel", foreground=DISABLED_TEXT)
