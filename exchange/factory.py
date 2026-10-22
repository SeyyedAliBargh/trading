"""Factory to get the appropriate exchange implementation."""
import config
from exchange.wallex import WallexClient
from exchange.paper import PaperExchange
import store.db as store_db


def get_exchange(cfg: config.Config, db_path: str = None):
    """Return a PaperExchange if DRY_RUN else WallexClient (with write capability)."""
    if cfg.trading.dry_run:
        # data_source for market data is a WallexClient without db (or with db)
        data_source = WallexClient(cfg, db=None)
        return PaperExchange(cfg, db_path or cfg.storage.DB_PATH, data_source)
    else:
        # Live client with db for logging
        db = store_db
        # ensure db initialized
        store_db.init_db(db_path or cfg.storage.DB_PATH)
        return WallexClient(cfg, db=db)