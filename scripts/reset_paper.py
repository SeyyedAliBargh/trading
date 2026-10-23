#!/usr/bin/env python3
"""Reset the paper account to the configured initial capital. Idempotent."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse

import config
import store.db as store_db


def main() -> None:
    # argparse FIRST: --help must exit without touching the database
    # (this script is destructive on purpose, so it must never run by accident)
    argparse.ArgumentParser(
        prog="reset_paper.py",
        description="Reset the paper account to the configured initial capital").parse_args()
    cfg = config.CONFIG
    store_db.init_db(cfg.storage.DB_PATH)
    ok = store_db.reset_paper_account(cfg.trading.paper_initial_capital_tmn)
    if ok:
        acct = store_db.get_paper_account()
        print(f"Paper account reset. TMN balance: {acct['tmn_balance']}")
    else:
        print("Failed to reset paper account")
        sys.exit(1)


if __name__ == "__main__":
    main()
