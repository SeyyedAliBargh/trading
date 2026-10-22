"""Tests for exchange/factory.py (PART 2)."""
import pytest
from unittest.mock import patch, MagicMock
import dataclasses
import config
from exchange.factory import get_exchange
from exchange.wallex import WallexClient
from exchange.paper import PaperExchange
import store.db as store_db


@pytest.fixture()
def temp_db(tmp_path):
    path = tmp_path / "factory_test.db"
    store_db.init_db(str(path))
    yield str(path)
    store_db.close_db()


def _cfg_with_dry_run(dry: bool):
    return dataclasses.replace(config.CONFIG, trading=dataclasses.replace(config.CONFIG.trading, dry_run=dry))


def test_dry_run_true_returns_paper(temp_db):
    cfg = _cfg_with_dry_run(True)
    exch = get_exchange(cfg, db_path=temp_db)
    assert isinstance(exch, PaperExchange)


def test_dry_run_false_returns_live_client(temp_db):
    cfg = _cfg_with_dry_run(False)
    with patch("exchange.factory.store_db") as mock_store:
        mock_store.init_db = MagicMock()
        exch = get_exchange(cfg, db_path=temp_db)
        assert isinstance(exch, WallexClient)


def test_no_network_when_dry_run_true(temp_db):
    cfg = _cfg_with_dry_run(True)
    with patch("exchange.factory.WallexClient") as mock_wallex:
        mock_wallex.return_value = MagicMock()
        exch = get_exchange(cfg, db_path=temp_db)
        assert mock_wallex.call_count == 1
        assert isinstance(exch, PaperExchange)