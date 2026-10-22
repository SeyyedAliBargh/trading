#!/usr/bin/env python3
"""Reset paper account to initial capital (PART 2)."""
import config
import store.db as store_db


def main():
    cfg = config.CONFIG
    db_path = cfg.storage.DB_PATH
    store_db.init_db(db_path)
    ok = store_db.reset_paper_account(cfg.trading.paper_initial_capital_tmn)
    if ok:
        acct = store_db.get_paper_account()
        print(f"Paper account reset. TMN balance: {acct['tmn_balance']}")
    else:
        print("Failed to reset paper account")
        exit(1)


if __name__ == "__main__":
    main()