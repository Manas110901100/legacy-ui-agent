"""Modal dialogs: account form, deposit/withdraw, transfer, and account history."""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from decimal import Decimal
from tkinter import messagebox, ttk
from typing import TYPE_CHECKING

from bankapp.services import ACCOUNT_TYPES, AccountSummary, Transaction, fmt_money
from bankapp.ui import style

if TYPE_CHECKING:
    from bankapp.ui.app import App


def _center(window: tk.Toplevel, over: tk.Misc) -> None:
    window.update_idletasks()
    top = over.winfo_toplevel()
    x = top.winfo_rootx() + (top.winfo_width() - window.winfo_width()) // 2
    y = top.winfo_rooty() + (top.winfo_height() - window.winfo_height()) // 3
    window.geometry(f"+{max(x, 0)}+{max(y, 0)}")


class FormDialog(tk.Toplevel):
    """Label/entry grid with OK/Cancel. Subclasses implement submit()."""

    def __init__(self, master: tk.Misc, app: App, title: str, on_done: Callable[[], None]) -> None:
        super().__init__(master)
        self.app = app
        self.on_done = on_done
        self.title(title)
        self.configure(bg=style.BG)
        self.resizable(False, False)
        self.transient(master.winfo_toplevel())
        self.fields: dict[str, tk.Widget] = {}
        self.body = ttk.Frame(self, padding=12)
        self.body.pack(fill="both", expand=True)
        self._row = 0
        self.bind("<Return>", lambda _e: self.submit())
        self.bind("<Escape>", lambda _e: self.destroy())

    def add_entry(
        self, key: str, label: str, value: str = "", *, readonly: bool = False
    ) -> ttk.Entry:
        ttk.Label(self.body, text=label).grid(
            row=self._row, column=0, sticky="w", pady=3, padx=(0, 8)
        )
        entry = ttk.Entry(self.body, width=34)
        entry.insert(0, value)
        if readonly:
            entry.configure(state="readonly", takefocus=False)
        entry.grid(row=self._row, column=1, sticky="w", pady=3)
        self.fields[key] = entry
        self._row += 1
        return entry

    def add_combo(self, key: str, label: str, values: tuple[str, ...], value: str) -> None:
        ttk.Label(self.body, text=label).grid(
            row=self._row, column=0, sticky="w", pady=3, padx=(0, 8)
        )
        combo = ttk.Combobox(self.body, values=values, state="readonly", width=31)
        combo.set(value)
        combo.grid(row=self._row, column=1, sticky="w", pady=3)
        self.fields[key] = combo
        self._row += 1

    def finish(self, ok_text: str = "OK") -> None:
        ttk.Separator(self.body).grid(
            row=self._row, column=0, columnspan=2, sticky="ew", pady=(10, 8)
        )
        buttons = ttk.Frame(self.body)
        buttons.grid(row=self._row + 1, column=0, columnspan=2, sticky="e")
        ttk.Button(buttons, text=ok_text, width=10, command=self.submit).pack(side="left", padx=2)
        ttk.Button(buttons, text="Cancel", width=10, command=self.destroy).pack(side="left", padx=2)
        _center(self, self.master)
        self.grab_set()
        for widget in self.fields.values():
            if isinstance(widget, ttk.Entry) and str(widget.cget("state")) != "readonly":
                widget.focus_set()
                break

    def value(self, key: str) -> str:
        return self.fields[key].get()  # type: ignore[attr-defined]

    def succeed(self, message: str) -> None:
        self.destroy()
        messagebox.showinfo("Success", message, parent=self.app.root)
        self.on_done()

    def submit(self) -> None:
        raise NotImplementedError


class AccountDialog(FormDialog):
    """Open a new account, or edit the customer details of an existing one."""

    def __init__(
        self,
        master: tk.Misc,
        app: App,
        on_done: Callable[[], None],
        account: AccountSummary | None = None,
    ) -> None:
        super().__init__(master, app, "Edit Customer" if account else "New Account", on_done)
        self.account = account
        if account:
            self.add_entry("account_no", "Account No:", account.account_no, readonly=True)
        self.add_entry("full_name", "Customer name:", account.customer_name if account else "")
        self.add_entry("email", "Email:", account.email if account else "")
        self.add_entry("phone", "Phone:", account.phone if account else "")
        if account:
            self.add_entry("account_type", "Account type:", account.account_type, readonly=True)
        else:
            self.add_combo("account_type", "Account type:", ACCOUNT_TYPES, ACCOUNT_TYPES[0])
            self.add_entry("opening_deposit", "Opening deposit:")
        self.finish("Save")

    def submit(self) -> None:
        session = self.app.require_session()
        name, email, phone = self.value("full_name"), self.value("email"), self.value("phone")
        service = self.app.service
        if self.account is None:
            kind, deposit = self.value("account_type"), self.value("opening_deposit")
            self.app.call(
                lambda _c: service.create_account(session, name, email, phone, kind, deposit),
                lambda number: self.succeed(f"Account {number} opened for {name.strip()}."),
                parent=self,
                fields=self.fields,
                message="Opening account...",
            )
        else:
            number = self.account.account_no
            self.app.call(
                lambda _c: service.update_customer(session, number, name, email, phone),
                lambda _r: self.succeed(f"Customer details for {number} updated."),
                parent=self,
                fields=self.fields,
                message="Saving changes...",
            )


class AmountDialog(FormDialog):
    """Deposit or withdrawal against one account."""

    def __init__(
        self,
        master: tk.Misc,
        app: App,
        on_done: Callable[[], None],
        account: AccountSummary,
        kind: str,
    ) -> None:
        self.kind = kind
        super().__init__(master, app, kind.title(), on_done)
        self.account = account
        self.add_entry("account_no", "Account No:", account.account_no, readonly=True)
        self.add_entry("customer", "Customer:", account.customer_name, readonly=True)
        self.add_entry("balance", "Current balance:", fmt_money(account.balance), readonly=True)
        self.add_entry("amount", "Amount:")
        self.add_entry("description", "Description:")
        self.finish(kind.title())

    def submit(self) -> None:
        session = self.app.require_session()
        amount, memo = self.value("amount"), self.value("description")
        number = self.account.account_no
        operation = (
            self.app.service.deposit if self.kind == "deposit" else self.app.service.withdraw
        )

        def done(new_balance: Decimal) -> None:
            self.succeed(
                f"{self.kind.title()} posted to {number}.\nNew balance: {fmt_money(new_balance)}"
            )

        self.app.call(
            lambda confirmed: operation(session, number, amount, memo, confirmed),
            done,
            parent=self,
            fields=self.fields,
            message=f"Posting {self.kind}...",
        )


class TransferDialog(FormDialog):
    def __init__(
        self, master: tk.Misc, app: App, on_done: Callable[[], None], account: AccountSummary
    ) -> None:
        super().__init__(master, app, "Funds Transfer", on_done)
        self.account = account
        self.add_entry("from_account", "From account:", account.account_no, readonly=True)
        self.add_entry("customer", "Customer:", account.customer_name, readonly=True)
        self.add_entry("balance", "Available:", fmt_money(account.balance), readonly=True)
        self.add_entry("to_account", "To account:")
        self.add_entry("amount", "Amount:")
        self.add_entry("description", "Description:")
        self.finish("Transfer")

    def submit(self) -> None:
        session = self.app.require_session()
        source = self.account.account_no
        target, amount = self.value("to_account"), self.value("amount")
        memo = self.value("description")

        def done(new_balance: Decimal) -> None:
            self.succeed(
                f"Transfer from {source} to {target.strip().upper()} completed.\n"
                f"New balance: {fmt_money(new_balance)}"
            )

        self.app.call(
            lambda confirmed: self.app.service.transfer(
                session, source, target, amount, memo, confirmed
            ),
            done,
            parent=self,
            fields=self.fields,
            message="Processing transfer...",
        )


class HistoryDialog(tk.Toplevel):
    """Account details plus its transaction history."""

    def __init__(
        self, master: tk.Misc, account: AccountSummary, transactions: list[Transaction]
    ) -> None:
        super().__init__(master)
        self.title(f"Account Details - {account.account_no}")
        self.configure(bg=style.BG)
        self.transient(master.winfo_toplevel())
        self.geometry("760x420")
        self.bind("<Escape>", lambda _e: self.destroy())

        details = ttk.LabelFrame(self, text=" Account ", padding=8)
        details.pack(fill="x", padx=8, pady=(8, 4))
        info = [
            ("Account No:", account.account_no),
            ("Customer:", account.customer_name),
            ("Type:", account.account_type),
            ("Status:", account.status),
            ("Balance:", fmt_money(account.balance)),
            ("Opened:", account.opened_at[:10]),
            ("Email:", account.email),
            ("Phone:", account.phone),
        ]
        for i, (label, value) in enumerate(info):
            row, col = divmod(i, 2)
            ttk.Label(details, text=label).grid(row=row, column=col * 2, sticky="w", padx=(0, 6))
            ttk.Label(details, text=value, style="Bold.TLabel").grid(
                row=row, column=col * 2 + 1, sticky="w", padx=(0, 30)
            )

        grid_frame = ttk.LabelFrame(self, text=f" Transactions ({len(transactions)}) ", padding=4)
        grid_frame.pack(fill="both", expand=True, padx=8, pady=4)
        columns = ("date", "type", "amount", "balance", "counterparty", "description")
        headings = ("Date/Time", "Type", "Amount", "Balance After", "Counterparty", "Description")
        widths = (130, 110, 90, 100, 90, 180)
        tree = ttk.Treeview(grid_frame, columns=columns, show="headings")
        for col, head, width in zip(columns, headings, widths, strict=True):
            tree.heading(col, text=head, anchor="w")
            numeric = col in ("amount", "balance")
            tree.column(col, width=width, anchor="e" if numeric else "w")
        for t in transactions:
            tree.insert(
                "",
                "end",
                values=(
                    t.created_at.replace("T", " "),
                    t.txn_type,
                    fmt_money(t.amount),
                    fmt_money(t.balance_after),
                    t.counterparty,
                    t.description,
                ),
            )
        scroll = ttk.Scrollbar(grid_frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        ttk.Button(self, text="Close", width=10, command=self.destroy).pack(
            anchor="e", padx=8, pady=(4, 8)
        )
        _center(self, master)
