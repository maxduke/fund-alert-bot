from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import pandas as pd
import pytest

from fund_alert_bot.checks import (
    evaluate_drawdown_plan_prealerts,
    evaluate_drawdown_plan_rules,
    read_drawdown_plan_statuses,
)
from fund_alert_bot.db import (
    add_rule,
    get_active_drawdown_cycle,
    initialize_database,
    list_retryable_drawdown_plan_alert_events,
    list_rules,
    open_connection,
    record_alert_notification_result,
)
from fund_alert_bot.i18n import localize_text, set_language
from fund_alert_bot.market_data import RealtimeQuote
from fund_alert_bot.market_data.index_valuation import IndexValuation
from fund_alert_bot.plan_views import format_plan_details, format_plan_overview
from fund_alert_bot.valuation import get_valuation_context, update_plan_index

NOW = datetime(2024, 1, 3, 6, tzinfo=UTC)


class Provider:
    def __init__(self):
        self.calls = 0
        self.error = False
        self.values = tuple(
            IndexValuation(
                "000510", "中证A500", date(2024, 1, day), 10 + day, 20 + day, 2, 1.8
            )
            for day in (1, 2, 3)
        )

    def get_index_valuations(self, index_symbol):
        assert index_symbol == "000510"
        if hasattr(self, "connection"):
            assert not self.connection.in_transaction
        self.calls += 1
        if self.error:
            raise TimeoutError("source temporarily unavailable")
        return self.values

    def get_history(self, instrument, *args, **kwargs):
        frame = pd.DataFrame(
            {
                "date": pd.date_range("2024-01-01", periods=2),
                "close": [100, 80],
                "source": "akshare_eastmoney",
            }
        )
        frame.attrs.update(
            symbol=instrument.symbol,
            source="akshare_eastmoney",
            price_basis="qfq",
            frequency="daily",
        )
        return frame

    def get_etf_realtime_quote(self, instrument):
        return RealtimeQuote(
            instrument.symbol,
            79,
            80,
            100,
            7900,
            "eastmoney",
            datetime(2024, 1, 3, 6, 50, tzinfo=UTC),
        )


@pytest.fixture
def path(tmp_path):
    result = tmp_path / "bot.sqlite3"
    with open_connection(result) as connection:
        initialize_database(connection)
    return result


def _add_plan(connection, *, configured=True, symbol="510300", fund="000001"):
    params = {
        "investment_fund_symbol": fund,
        "tiers": [
            {"drawdown": 0.15, "amount": 5000},
            {"drawdown": 0.2, "amount": 10000},
        ],
    }
    if configured:
        params["valuation_index_symbol"] = "000510"
    return add_rule(
        connection,
        type="drawdown_plan",
        symbol=symbol,
        name="Core",
        asset_type="cn_etf",
        params=params,
    )


def test_cache_survives_restart_and_selects_no_later_than_price_date(path):
    provider = Provider()
    with open_connection(path) as connection:
        context = get_valuation_context(
            connection, provider, "000510", as_of_date=date(2024, 1, 2), now=NOW
        )
        assert context.quote.date == date(2024, 1, 2)
        assert context.quote.pe1 == 12
    with open_connection(path) as connection:
        initialize_database(connection)
        context = get_valuation_context(
            connection,
            provider,
            "000510",
            as_of_date=date(2024, 1, 3),
            now=NOW + timedelta(minutes=1),
        )
        assert context.quote.date == date(2024, 1, 3)
        assert provider.calls == 1


def test_refresh_failure_preserves_snapshot_and_failure_cache_limits_requests(path):
    provider = Provider()
    with open_connection(path) as connection:
        get_valuation_context(
            connection, provider, "000510", as_of_date=date(2024, 1, 3), now=NOW
        )
        provider.error = True
        context = get_valuation_context(
            connection,
            provider,
            "000510",
            as_of_date=date(2024, 1, 4),
            now=NOW + timedelta(hours=1),
        )
        assert context.quote.date == date(2024, 1, 3)
        assert context.refresh_failed
        assert "Valuation predates the price date." in context.lines()
        assert "Cached valuation; refresh failed." in context.lines()
        get_valuation_context(
            connection,
            provider,
            "000510",
            as_of_date=date(2024, 1, 4),
            now=NOW + timedelta(hours=1, minutes=1),
        )
        assert provider.calls == 2
        get_valuation_context(
            connection,
            provider,
            "000510",
            as_of_date=date(2024, 1, 4),
            force_refresh=True,
            now=NOW + timedelta(hours=1, minutes=2),
        )
        assert provider.calls == 3


def test_failed_first_fetch_is_cached_then_can_recover(path):
    provider = Provider()
    provider.error = True
    with open_connection(path) as connection:
        first = get_valuation_context(
            connection, provider, "000510", as_of_date=date(2024, 1, 3), now=NOW
        )
        assert first.quote is None
        provider.error = False
        get_valuation_context(
            connection,
            provider,
            "000510",
            as_of_date=date(2024, 1, 3),
            now=NOW + timedelta(minutes=1),
        )
        assert provider.calls == 1
        recovered = get_valuation_context(
            connection,
            provider,
            "000510",
            as_of_date=date(2024, 1, 3),
            now=NOW + timedelta(minutes=6),
        )
        assert provider.calls == 2 and recovered.quote is not None


@pytest.mark.parametrize("bad_cache", ["corrupt", "wrong_index", "future"])
def test_corrupt_or_wrong_index_cache_is_not_displayed(path, bad_cache):
    provider = Provider()
    with open_connection(path) as connection:
        get_valuation_context(
            connection, provider, "000510", as_of_date=date(2024, 1, 3), now=NOW
        )
        if bad_cache == "corrupt":
            content = "{}"
        else:
            content = json.dumps(
                [
                    IndexValuation(
                        "000688" if bad_cache == "wrong_index" else "000510",
                        "Other",
                        date(2099, 1, 1) if bad_cache == "future" else date(2024, 1, 2),
                        10,
                        20,
                        1,
                        1,
                    ).payload()
                ]
            )
        connection.execute(
            "UPDATE index_valuation_cache SET observations_json = ?", (content,)
        )
        connection.commit()
        provider.error = True
        context = get_valuation_context(
            connection, provider, "000510", as_of_date=date(2024, 1, 3), now=NOW
        )
        assert context.quote is None
        assert provider.calls == 2


def test_unconfigured_plan_makes_no_valuation_request(path):
    provider = Provider()
    with open_connection(path) as connection:
        _add_plan(connection, configured=False)
        result = evaluate_drawdown_plan_rules(
            connection, provider, expected_date=date(2024, 1, 2)
        )
        assert not result.errors and len(result.notifications) == 1
        assert provider.calls == 0
        event = connection.execute("SELECT payload_json FROM alert_events").fetchone()[
            0
        ]
        assert "valuation" not in json.loads(event)
        assert (
            connection.execute("SELECT COUNT(*) FROM index_valuation_cache").fetchone()[
                0
            ]
            == 0
        )


@pytest.mark.parametrize("failure", [None, "timeout", "wrong_index", "future"])
def test_optional_valuation_preserves_close_tiers_amounts_and_actions(path, failure):
    provider = Provider()
    provider.error = failure == "timeout"
    if failure in {"wrong_index", "future"}:
        provider.values = (
            IndexValuation(
                "000688" if failure == "wrong_index" else "000510",
                "Other",
                date(2099, 1, 1) if failure == "future" else date(2024, 1, 2),
                10,
                20,
                1,
                1,
            ),
        )
    with open_connection(path) as connection:
        provider.connection = connection
        plan_id = _add_plan(connection)
        result = evaluate_drawdown_plan_rules(
            connection, provider, expected_date=date(2024, 1, 2)
        )
        assert not result.errors and not result.no_data_skips
        assert len(result.notifications) == 1
        payload = json.loads(
            connection.execute("SELECT payload_json FROM alert_events").fetchone()[0]
        )
        assert payload["total_amount"] == 15000
        assert [
            (tier["key"], tier["amount"]) for tier in payload["actionable_tiers"]
        ] == [("0.15", 5000), ("0.2", 10000)]
        quote = payload["valuation"]["quote"]
        assert quote is None if failure else quote["date"] == "2024-01-02"
        assert get_active_drawdown_cycle(connection, plan_id) is not None
        assert (
            connection.execute("SELECT COUNT(*) FROM drawdown_tier_records").fetchone()[
                0
            ]
            == 2
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM manual_add_actions").fetchone()[0]
            == 0
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM position_snapshots").fetchone()[0]
            == 0
        )
        assert result.notifications[0].telegram_actions


def test_prealert_uses_confirmed_valuation_date_even_if_todays_value_exists(path):
    provider = Provider()
    with open_connection(path) as connection:
        _add_plan(connection)
        result = evaluate_drawdown_plan_prealerts(
            connection,
            provider,
            market_date=date(2024, 1, 3),
            confirmed_date=date(2024, 1, 2),
        )
        assert not result.errors and len(result.notifications) == 1
        payload = json.loads(
            connection.execute("SELECT payload_json FROM alert_events").fetchone()[0]
        )
        assert payload["data_date"] == "2024-01-03"
        assert payload["valuation"]["quote"]["date"] == "2024-01-02"
        assert payload["total_amount"] == 15000


def test_retry_uses_frozen_valuation_after_cache_changes(path):
    provider = Provider()
    with open_connection(path) as connection:
        _add_plan(connection)
        result = evaluate_drawdown_plan_rules(
            connection, provider, expected_date=date(2024, 1, 2)
        )
        original = result.notifications[0]
        record_alert_notification_result(
            connection,
            event_id=original.event_id,
            results=[{"channel": "test", "success": False, "detail": "retry"}],
        )
        provider.values = (
            IndexValuation("000510", "中证A500", date(2024, 1, 2), 99, 100, 1, 1),
        )
        get_valuation_context(
            connection,
            provider,
            "000510",
            as_of_date=date(2024, 1, 2),
            force_refresh=True,
            now=NOW,
        )
        events = list_retryable_drawdown_plan_alert_events(connection)
        assert len(events) == 1
        assert events[0]["message"] == original.text
        payload = connection.execute(
            "SELECT payload_json FROM alert_events WHERE id = ?", (original.event_id,)
        ).fetchone()[0]
        assert json.loads(payload)["valuation"]["quote"]["pe1"] == 12


def test_status_refresh_shares_one_request_for_same_index_and_localizes(path):
    provider = Provider()
    with open_connection(path) as connection:
        _add_plan(connection)
        _add_plan(connection, symbol="510301", fund="000002")
        statuses = read_drawdown_plan_statuses(
            connection, provider, end_date=date(2024, 1, 3), force_refresh=True
        )
        assert not statuses.errors and len(statuses.statuses) == 2
        assert provider.calls == 1
        for formatter in (format_plan_overview, format_plan_details):
            text = formatter(statuses)
            assert "PE1 / PE2: 12 / 22" in text
            assert "Valuation date: 2024-01-02" in text
            set_language("zh-CN")
            try:
                chinese = localize_text(text)
                assert "估值参考指数：" in chinese
                assert "市盈率 PE1 / PE2： 12 / 22" in chinese
                assert "PB / 历史估值分位：此数据源未提供" in chinese
            finally:
                set_language("en")


def test_association_update_preserves_existing_state_and_checks_expected_config(path):
    provider = Provider()
    with open_connection(path) as connection:
        plan_id = _add_plan(connection, configured=False)
        evaluate_drawdown_plan_rules(
            connection, provider, expected_date=date(2024, 1, 2)
        )
        tables = (
            "drawdown_cycles",
            "drawdown_tier_records",
            "alert_events",
            "manual_add_actions",
            "position_snapshots",
        )
        before = {
            table: [dict(row) for row in connection.execute(f"SELECT * FROM {table}")]
            for table in tables
        }
        params = list_rules(connection)[0]["params_json"]
        update_plan_index(
            connection,
            rule_id=plan_id,
            index_symbol="000510",
            expected_params_json=params,
        )
        assert (
            json.loads(list_rules(connection)[0]["params_json"])[
                "valuation_index_symbol"
            ]
            == "000510"
        )
        assert {
            table: [dict(row) for row in connection.execute(f"SELECT * FROM {table}")]
            for table in tables
        } == before
        with pytest.raises(ValueError, match="Plan changed"):
            update_plan_index(
                connection,
                rule_id=plan_id,
                index_symbol=None,
                expected_params_json=params,
            )
        update_plan_index(
            connection,
            rule_id=plan_id,
            index_symbol=None,
            expected_params_json=list_rules(connection)[0]["params_json"],
        )
        assert "valuation_index_symbol" not in json.loads(
            list_rules(connection)[0]["params_json"]
        )
