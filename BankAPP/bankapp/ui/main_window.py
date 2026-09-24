"""Main screen: menu bar, toolbar, account grid and status bar."""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk
from typing import TYPE_CHECKING

from bankapp.errors import NotFoundError
from bankapp.services import AccountSummary, fmt_money
from bankapp.ui import style
from bankapp.ui.dialogs import AccountDialog, AmountDialog, HistoryDialog, TransferDialog

if TYPE_CHECKING:
    from bankapp.ui.app import App

COLUMNS = ("account_no", "customer", "type", "balance", "status", "email", "phone")
HEADINGS = ("Account No", "Customer Name", "Type", "Balance", "Status", "Email", "Phone")
WIDTHS = (90, 170, 75, 100, 65, 230, 100)


class MainWindow(ttk.Frame):
    def __init__(self, master: tk.Tk, app: App) -> None:
        super().__init__(master)
        self.app = app
        self._accounts: dict[str, AccountSummary] = {}
        self._query = ""
        self._build_menu(master)
        self._build_toolbar()
        self._build_grid()
        self._build_status()
        self.refresh()

    # ----- layout -----------------------------------------------------------------------

    def _build_menu(self, root: tk.Tk) -> None:
        menubar = tk.Menu(root)
        file_menu = tk.Menu(menubar, tearoff=False)
        file_menu.add_command(label="Refresh", accelerator="F5", command=self.show_all)
        file_menu.add_separator()
        file_menu.add_command(label="Log Off", command=self.app.logout)
        file_menu.add_command(label="Exit", command=self.app.exit)
        menubar.add_cascade(label="File", menu=file_menu)

        account_menu = tk.Menu(menubar, tearoff=False)
        account_menu.add_command(label="New Account...", command=self.new_account)
        account_menu.add_command(label="Edit Customer...", command=self.edit_account)
        account_menu.add_command(
            label="View Details...", accelerator="Enter", command=self.view_details
        )
        account_menu.add_separator()
        account_menu.add_command(label="Close Account...", command=self.close_account)
        menubar.add_cascade(label="Account", menu=account_menu)

        txn_menu = tk.Menu(menubar, tearoff=False)
        txn_menu.add_command(label="Deposit...", command=lambda: self.amount("deposit"))
        txn_menu.add_command(label="Withdraw...", command=lambda: self.amount("withdraw"))
        txn_menu.add_command(label="Transfer...", command=self.transfer)
        menubar.add_cascade(label="Transaction", menu=txn_menu)

        help_menu = tk.Menu(menubar, tearoff=False)
        help_menu.add_command(label="Simulate Application Error", command=self.simulate_crash)
        help_menu.add_separator()
        help_menu.add_command(label="About BankAPP...", command=self.about)
        menubar.add_cascade(label="Help", menu=help_menu)

        root.config(menu=menubar)
        root.bind("<F5>", lambda _e: self.show_all())

    def _build_toolbar(self) -> None:
        bar = ttk.Frame(self, padding=(4, 4))
        bar.pack(fill="x")
        buttons = [
            ("New", self.new_account),
            ("Edit", self.edit_account),
            ("Details", self.view_details),
            (None, None),
            ("Deposit", lambda: self.amount("deposit")),
            ("Withdraw", lambda: self.amount("withdraw")),
            ("Transfer", self.transfer),
            (None, None),
            ("Close Acct", self.close_account),
        ]
        for text, command in buttons:
            if text is None:
                ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=4)
            else:
                ttk.Button(bar, text=text, width=10, command=command).pack(side="left", padx=1)

        ttk.Button(bar, text="Show All", width=9, command=self.show_all).pack(side="right", padx=1)
        ttk.Button(bar, text="Find", width=7, command=self.find).pack(side="right", padx=1)
        self.search = ttk.Entry(bar, width=22)
        self.search.pack(side="right", padx=(2, 4))
        self.search.bind("<Return>", lambda _e: self.find())
        ttk.Label(bar, text="Account No / Name:").pack(side="right")
        ttk.Separator(self).pack(fill="x")

    def _build_grid(self) -> None:
        frame = ttk.Frame(self, padding=4)
        frame.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(frame, columns=COLUMNS, show="headings", selectmode="browse")
        for col, head, width in zip(COLUMNS, HEADINGS, WIDTHS, strict=True):
            self.tree.heading(col, text=head, anchor="w")
            self.tree.column(col, width=width, anchor="e" if col == "balance" else "w")
        self.tree.tag_configure("closed", foreground=style.DISABLED_TEXT)
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", lambda _e: self.view_details())
        self.tree.bind("<Return>", lambda _e: self.view_details())

    def _build_status(self) -> None:
        bar = ttk.Frame(self)
        bar.pack(fill="x", side="bottom")
        session = self.app.require_session()
        ttk.Label(
            bar, text=f"User: {session.username} ({session.role})", width=28, style="Status.TLabel"
        ).pack(side="right")
        self.status = ttk.Label(bar, text="Ready", style="Status.TLabel")
        self.status.pack(side="left", fill="x", expand=True)

    # ----- data -------------------------------------------------------------------------

    def refresh(self) -> None:
        session = self.app.session
        if session is None or not self.winfo_exists():
            return
        query = self._query
        self.app.call(
            lambda _c: self.app.service.list_accounts(session, query),
            self._populate,
            message="Loading accounts...",
        )

    def _populate(self, rows: list[AccountSummary]) -> None:
        if not self.winfo_exists():
            return
        self.tree.delete(*self.tree.get_children())
        self._accounts = {a.account_no: a for a in rows}
        for a in rows:
            self.tree.insert(
                "",
                "end",
                iid=a.account_no,
                tags=("closed",) if a.status == "CLOSED" else (),
                values=(
                    a.account_no,
                    a.customer_name,
                    a.account_type,
                    fmt_money(a.balance),
                    a.status,
                    a.email,
                    a.phone,
                ),
            )
        if rows:
            self.tree.selection_set(rows[0].account_no)
            self.tree.focus(rows[0].account_no)
        suffix = f" matching '{self._query}'" if self._query else ""
        self.status.configure(text=f"{len(rows)} account(s){suffix}")

    def show_all(self) -> None:
        self.search.delete(0, "end")
        self._query = ""
        self.refresh()

    def find(self) -> None:
        session = self.app.require_session()
        query = self.search.get().strip()
        if not query:
            self.show_all()
            return
        service = self.app.service

        def search(_confirmed: bool) -> list[AccountSummary]:
            if query.upper().startswith("ACC"):
                return [service.get_account(session, query)]
            rows = service.list_accounts(session, query)
            if not rows:
                raise NotFoundError(f"No accounts match '{query}'.")
            return rows

        def show(rows: list[AccountSummary]) -> None:
            self._query = query
            self._populate(rows)

        self.app.call(search, show, fields={"account_no": self.search}, message="Searching...")

    def selected(self) -> AccountSummary | None:
        selection = self.tree.selection()
        if not selection:
            messagebox.showinfo(
                "No Selection", "Please select an account first.", parent=self.app.root
            )
            return None
        return self._accounts[selection[0]]

    # ----- actions ----------------------------------------------------------------------

    def new_account(self) -> None:
        AccountDialog(self, self.app, self.refresh)

    def edit_account(self) -> None:
        if account := self.selected():
            AccountDialog(self, self.app, self.refresh, account)

    def amount(self, kind: str) -> None:
        if account := self.selected():
            AmountDialog(self, self.app, self.refresh, account, kind)

    def transfer(self) -> None:
        if account := self.selected():
            TransferDialog(self, self.app, self.refresh, account)

    def view_details(self) -> None:
        account = self.selected()
        if account is None:
            return
        session = self.app.require_session()
        number = account.account_no
        self.app.call(
            lambda _c: self.app.service.history(session, number),
            lambda data: HistoryDialog(self, *data),
            message="Loading history...",
        )

    def close_account(self) -> None:
        account = self.selected()
        if account is None:
            return
        session = self.app.require_session()
        number = account.account_no

        def done(_result: None) -> None:
            messagebox.showinfo(
                "Success", f"Account {number} has been closed.", parent=self.app.root
            )
            self.refresh()

        self.app.call(
            lambda confirmed: self.app.service.close_account(session, number, confirmed),
            done,
            message="Closing account...",
        )

    def simulate_crash(self) -> None:
        raise RuntimeError("Simulated unhandled exception (Help > Simulate Application Error)")

    def about(self) -> None:
        cfg = self.app.config
        low, high = cfg.latency
        messagebox.showinfo(
            "About BankAPP",
            "BankAPP - Core Banking System v1.0\n\n"
            f"Session timeout: {cfg.session_timeout}s\n"
            f"Simulated latency: {low}-{high}s\n"
            f"Simulated fault rate: {cfg.fault_rate:.0%}",
            parent=self.app.root,
        )
