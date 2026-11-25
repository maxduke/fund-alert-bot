"""Focused tests for PR 3 modular decomposition and helper contracts."""

from __future__ import annotations

from datetime import date

import pandas as pd

from fund_alert_bot import commands, db, db_retention
from fund_alert_bot.market_data.akshare_provider import AkshareMarketDataProvider


def test_db_reexports_prune_database_from_db_retention() -> None:
    assert db.prune_database is db_retention.prune_database


def test_get_rule_fetches_single_rule_and_returns_none_when_missing() -> None:
    with db.open_connection(":memory:") as connection:
        db.initialize_database(connection)
        rule_id = db.add_rule(
            connection,
            type="drawdown",
            symbol="510300",
            name="CSI 300 ETF",
            asset_type="cn_etf",
            params={"lookback_days": 60, "thresholds": [0.1]},
        )
        found = db.get_rule(connection, rule_id)
        assert found is not None
        assert int(found["id"]) == rule_id
        assert found["symbol"] == "510300"
        assert db.get_rule(connection, rule_id + 999) is None


def test_store_bounded_evicts_oldest_and_refreshes_overwrite_order() -> None:
    store: dict[str, int] = {}
    commands._store_bounded(store, "a", 1, max_entries=2)
    commands._store_bounded(store, "b", 2, max_entries=2)
    commands._store_bounded(store, "a", 10, max_entries=2)
    commands._store_bounded(store, "c", 3, max_entries=2)

    assert store == {"a": 10, "c": 3}


def test_akshare_history_cache_refreshes_lru_order_on_overwrite(monkeypatch) -> None:
    monkeypatch.setattr(
        "fund_alert_bot.market_data.akshare_provider._CACHE_MAX_ENTRIES",
        2,
    )
    provider = AkshareMarketDataProvider(ak_module=object())
    frame = pd.DataFrame({"date": [date(2026, 4, 1)], "close": [1.0]})

    key_a = ("cn_etf", "510300", "20260401", "20260402", "qfq")
    key_b = ("cn_etf", "159915", "20260401", "20260402", "qfq")
    key_c = ("cn_etf", "510500", "20260401", "20260402", "qfq")

    provider._write_history_cache(key_a, frame)
    provider._write_history_cache(key_b, frame)
    provider._write_history_cache(key_a, frame)
    provider._write_history_cache(key_c, frame)

    assert list(provider._history_cache) == [key_a, key_c]
