"""Entry point: python -m bankapp"""

from __future__ import annotations

import logging
import tkinter as tk

from bankapp import db
from bankapp.config import load_config
from bankapp.services import BankService
from bankapp.ui.app import App


def main() -> None:
    config = load_config()
    logging.basicConfig(
        filename=config.log_path,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    conn = db.connect(config.db_path)
    db.init_db(conn)
    root = tk.Tk()
    App(root, BankService(conn, config), config)
    try:
        root.mainloop()
    finally:
        conn.close()


if __name__ == "__main__":
    main()
