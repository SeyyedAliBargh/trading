"""Tests for exchange/factory.py (STEP 4b)."""
import dataclasses
from unittest.mock import MagicMock, patch

import config
import store.db as store_db
from exchange.factory import get_exchange
from exchange.wallex import WallexClient, WallexError
from exchange.paper import PaperExchange


def _cfg_with_dry_run(dry: bool):
    return dataclasses.replace(config.CONFIG,
                               trading=dataclasses.replace(config.CONFIG.trading, dry_run=dry))


def test_dry_run_true_returns_paper(tmp_path):
    store_db.init_db(str(tmp_path / "f.db"))
    try:
        exch = get_exchange(_cfg_with_dry_run(True), db=str(tmp_path / "f.db"))
        assert isinstance(exch, PaperExchange)
    finally:
        store_db.close_db()


def test_dry_run_false_returns_live_client(tmp_path):
    store_db.init_db(str(tmp_path / "f.db"))
    try:
        exch = get_exchange(_cfg_with_dry_run(False), db=str(tmp_path / "f.db"))
        assert isinstance(exch, WallexClient)
    finally:
        store_db.close_db()


def test_paper_path_blocks_write_endpoints(tmp_path):
    """The data_source client injected into PaperExchange must never allow writes."""
    store_db.init_db(str(tmp_path / "f.db"))
    try:
        exch = get_exchange(_cfg_with_dry_run(True), db=str(tmp_path / "f.db"))
        assert isinstance(exch, PaperExchange)
        ds = exch.data_source
        assert isinstance(ds, WallexClient)
        # DRY_RUN interlock on the injected client
        import pytest
        with pytest.raises(WallexError):
            ds.create_order("BTCTMN", "BUY", "LIMIT", 1.0, 100.0)
        with pytest.raises(WallexError):
            ds.cancel_order("x")
    finally:
        store_db.close_db()


def test_data_source_injectable(tmp_path):
    store_db.init_db(str(tmp_path / "f.db"))
    try:
        fake = MagicMock()
        fake.get_depth.return_value = {"bids": [(99.0, 1.0, 99.0)], "asks": [(100.0, 1.0, 100.0)]}
        fake.get_fee_for.return_value = {"maker_rate": 0.001, "taker_rate": 0.002}
        exch = get_exchange(_cfg_with_dry_run(True), db=str(tmp_path / "f.db"), data_source=fake)
        assert exch.data_source is fake
    finally:
        store_db.close_db()
