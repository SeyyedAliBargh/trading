"""Factory returning the right exchange implementation for the current mode."""
from pathlib import Path
from typing import Any, Optional, Union

import config
import store.db as store_db
from exchange.wallex import WallexClient
from exchange.paper import PaperExchange


def get_exchange(cfg: config.Config,
                 db: Any = None,
                 data_source: Optional[WallexClient] = None) -> Union[PaperExchange, WallexClient]:
    """Return a PaperExchange when DRY_RUN is true, a live WallexClient otherwise.

    `db` may be the store module (store.db) or a database path; the store is
    the module-level store.db in this project. In DRY_RUN the depth source is
    a read-only WallexClient whose GET calls are logged to api_calls; its
    write endpoints are blocked by the DRY_RUN safety interlock.
    """
    db_path = str(db) if isinstance(db, (str, Path)) else cfg.storage.DB_PATH
    if cfg.trading.dry_run:
        ds = data_source if data_source is not None else WallexClient(cfg, db=store_db)
        return PaperExchange(cfg, db_path, ds)
    store_db.init_db(db_path)
    return WallexClient(cfg, db=store_db)
