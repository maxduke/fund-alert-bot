from __future__ import annotations

from datetime import date, timedelta

from fund_alert_bot.db import (
    DEFAULT_RETENTION_DAYS,
    add_alert_event,
    add_rule,
    connect,
    init_db,
    prune_database,
    upsert_fund_nav,
    upsert_market_history,
)


def test_prune_market_history_uses_enabled_rule_windows() -> None:
    connection = connect(":memory:")
    try:
        init_db(connection)
        today = date(2026, 1, 1)
        add_rule(
            connection,
            type="drawdown_from_high",
            symbol="510300",
            name="ETF",
            asset_type="cn_etf",
            params={"lookback_days": 10, "thresholds": [0.1]},
        )
        for row_date, basis in (
            (today - timedelta(days=25), "unadjusted"),
            (today - timedelta(days=24), "unadjusted"),
            (today, "unadjusted"),
            (today, "qfq"),
        ):
            upsert_market_history(
                connection,
                symbol="510300",
                asset_type="cn_etf",
                price_basis=basis,
                rows=[
                    {
                        "date": row_date.isoformat(),
                        "close": 1,
                        "source": "test",
                    }
                ],
            )

        counts = prune_database(connection, today=today)

        assert counts["market_daily_history"] == 2
        rows = connection.execute(
            "SELECT date, price_basis FROM market_daily_history ORDER BY date"
        ).fetchall()
        assert [(row["date"], row["price_basis"]) for row in rows] == [
            ((today - timedelta(days=24)).isoformat(), "unadjusted"),
            (today.isoformat(), "unadjusted"),
        ]
    finally:
        connection.close()


def test_prune_plan_history_keeps_active_peak_as_a_bounded_exception() -> None:
    connection = connect(":memory:")
    try:
        init_db(connection)
        today = date(2026, 1, 1)
        rule_id = add_rule(
            connection,
            type="drawdown_plan",
            symbol="510300",
            name="Plan",
            asset_type="cn_etf",
            params={
                "investment_fund_symbol": "000001",
                "lookback_days": 10,
                "sma_window": 2,
                "sma_slope_window": 1,
                "tiers": [],
            },
        )
        connection.execute(
            """
            INSERT INTO drawdown_cycles (
                rule_id, peak_date, initial_peak_price, peak_price,
                last_evaluated_date, created_at, updated_at
            ) VALUES (?, '2020-01-01', 1, 1, '2020-01-02', '2020', '2020')
            """,
            (rule_id,),
        )
        for row_date in (
            date(2020, 1, 1),
            date(2020, 1, 2),
            today - timedelta(days=25),
            today - timedelta(days=24),
            today,
        ):
            upsert_market_history(
                connection,
                symbol="510300",
                asset_type="cn_etf",
                price_basis="qfq",
                rows=[
                    {
                        "date": row_date.isoformat(),
                        "close": 0.5 if row_date == date(2020, 1, 2) else 1,
                        "source": "test",
                    }
                ],
            )

        prune_database(connection, today=today)

        rows = connection.execute(
            "SELECT date FROM market_daily_history ORDER BY date"
        ).fetchall()
        assert [row["date"] for row in rows] == [
            date(2020, 1, 1).isoformat(),
            (today - timedelta(days=24)).isoformat(),
            today.isoformat(),
        ]
        cycle = connection.execute(
            "SELECT saw_below_peak FROM drawdown_cycles"
        ).fetchone()
        assert cycle["saw_below_peak"] == 1
    finally:
        connection.close()


def test_prune_fund_nav_preserves_latest_and_pending_effective_dates() -> None:
    connection = connect(":memory:")
    try:
        init_db(connection)
        today = date(2026, 1, 1)
        rule_id = add_rule(
            connection,
            type="drawdown_plan",
            symbol="510300",
            name="Plan",
            asset_type="cn_etf",
            params={"investment_fund_symbol": "000001", "tiers": []},
        )
        connection.execute(
            """
            INSERT INTO drawdown_cycles (
                rule_id, peak_date, initial_peak_price, peak_price,
                last_evaluated_date, created_at, updated_at
            ) VALUES (?, '2020-01-01', 1, 1, '2020-01-01',
                      '2020-01-01', '2020-01-01')
            """,
            (rule_id,),
        )
        cycle_id = int(
            connection.execute("SELECT id FROM drawdown_cycles").fetchone()[0]
        )
        source_event_id = add_alert_event(
            connection,
            rule_id=rule_id,
            alert_key="old-source",
            title="Plan",
            message="source",
            triggered_at="2020-01-01",
        )
        connection.execute(
            """
            INSERT INTO manual_add_estimates (
                rule_id, cycle_id, source_alert_event_id, fund_symbol,
                tier_keys_json, gross_amount, fee_mode, fee_value,
                action_at, action_date, cutoff_time, cutoff_choice,
                effective_date, created_at, updated_at
            ) VALUES (?, ?, ?, '000001', '[]', 100, 'rate', 0,
                      '2020-01-01', '2020-01-01', '15:00', 'before',
                      ?, '2020-01-01', '2020-01-01')
            """,
            (
                rule_id,
                cycle_id,
                source_event_id,
                (today - timedelta(days=500)).isoformat(),
            ),
        )
        for nav_date in (
            today - timedelta(days=500),
            today - timedelta(days=401),
            today - timedelta(days=399),
            today - timedelta(days=200),
        ):
            upsert_fund_nav(
                connection,
                fund_symbol="000001",
                nav_date=nav_date,
                unit_nav=1,
                source="test",
            )

        prune_database(connection, today=today)

        rows = connection.execute(
            "SELECT nav_date FROM fund_nav_history ORDER BY nav_date"
        ).fetchall()
        assert [row["nav_date"] for row in rows] == [
            (today - timedelta(days=500)).isoformat(),
            (today - timedelta(days=399)).isoformat(),
            (today - timedelta(days=200)).isoformat(),
        ]
    finally:
        connection.close()


def test_prune_protects_active_cycles_and_removes_terminal_old_cycles() -> None:
    connection = connect(":memory:")
    try:
        init_db(connection)
        today = date(2026, 1, 1)
        rule_id = add_rule(
            connection,
            type="drawdown_plan",
            symbol="510300",
            name="Plan",
            asset_type="cn_etf",
            params={"investment_fund_symbol": "000001", "tiers": []},
        )
        for end_date in (None, "2020-01-01"):
            connection.execute(
                """
                INSERT INTO drawdown_cycles (
                    rule_id, peak_date, initial_peak_price, peak_price,
                    last_evaluated_date, end_date, created_at, updated_at
                ) VALUES (?, '2020-01-01', 1, 1, '2020-01-01', ?,
                          '2020-01-01', '2020-01-01')
                """,
                (rule_id, end_date),
            )
        cycle_ids = [
            int(row[0])
            for row in connection.execute(
                "SELECT id FROM drawdown_cycles ORDER BY id"
            ).fetchall()
        ]
        connection.execute(
            """
            INSERT INTO drawdown_tier_records (
                cycle_id, tier_key, drawdown, amount, source, data_date, created_at
            ) VALUES (?, '0.1', 0.1, 100, 'close_confirmed', '2020-01-01', '2020-01-01')
            """,
            (cycle_ids[1],),
        )
        connection.execute(
            """
            INSERT INTO drawdown_tier_reminder_states (
                cycle_id, tier_key, created_at, updated_at
            ) VALUES (?, '0.1', '2020-01-01', '2020-01-01')
            """,
            (cycle_ids[1],),
        )
        connection.commit()

        counts = prune_database(connection, today=today)

        assert counts["drawdown_cycles"] == 1
        assert (
            connection.execute("SELECT COUNT(*) FROM drawdown_cycles").fetchone()[0]
            == 1
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM drawdown_tier_records").fetchone()[
                0
            ]
            == 0
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM drawdown_tier_reminder_states"
            ).fetchone()[0]
            == 0
        )
    finally:
        connection.close()


def test_prune_rejects_non_positive_retention() -> None:
    connection = connect(":memory:")
    try:
        init_db(connection)
        for value in (0, -1, True):
            try:
                prune_database(connection, today=date(2026, 1, 1), retention_days=value)
            except ValueError:
                pass
            else:
                raise AssertionError("invalid retention_days was accepted")
        assert DEFAULT_RETENTION_DAYS == 400
    finally:
        connection.close()


def test_prune_expires_known_dated_events_but_keeps_unknown_dedupe_keys() -> None:
    connection = connect(":memory:")
    try:
        init_db(connection)
        today = date(2026, 1, 1)
        rule_id = add_rule(
            connection,
            type="drawdown_plan",
            symbol="510300",
            name="Plan",
            asset_type="cn_etf",
            params={"investment_fund_symbol": "000001", "tiers": []},
        )
        expired_event_id = add_alert_event(
            connection,
            rule_id=rule_id,
            alert_key="1:drawdown_plan:after_close:2020-01-01",
            title="Drawdown plan reminder",
            message="old dated event",
            payload={"phase": "after_close"},
            triggered_at="2020-01-01",
        )
        connection.execute(
            "UPDATE alert_events SET notification_status = 'sent' WHERE id = ?",
            (expired_event_id,),
        )
        add_alert_event(
            connection,
            rule_id=rule_id,
            alert_key="future-alert-type:dedupe-key",
            title="Future reminder",
            message="unknown semantics",
            triggered_at="2020-01-01",
        )

        counts = prune_database(connection, today=today)

        assert counts["alert_events"] == 1
        remaining = connection.execute("SELECT alert_key FROM alert_events").fetchall()
        assert [row["alert_key"] for row in remaining] == [
            "future-alert-type:dedupe-key"
        ]
    finally:
        connection.close()


def test_prune_preserves_every_undelivered_alert_and_target() -> None:
    connection = connect(":memory:")
    try:
        init_db(connection)
        today = date(2026, 1, 1)
        rule_id = add_rule(
            connection,
            type="drawdown_plan",
            symbol="510300",
            name="Plan",
            asset_type="cn_etf",
            params={"investment_fund_symbol": "000001", "tiers": []},
        )
        delivery_states = (
            ("pending", "pending"),
            ("failed", "failed"),
            ("pending", "sending"),
            ("sent", "sending"),
        )
        for index, (event_status, delivery_status) in enumerate(delivery_states):
            event_id = add_alert_event(
                connection,
                rule_id=rule_id,
                alert_key=f"1:drawdown_plan:after_close:2020-01-0{index + 1}",
                title="Drawdown plan reminder",
                message="undelivered",
                payload={"phase": "after_close"},
                triggered_at=f"2020-01-0{index + 1}",
            )
            connection.execute(
                "UPDATE alert_events SET notification_status = ? WHERE id = ?",
                (event_status, event_id),
            )
            connection.execute(
                """
                INSERT INTO notification_deliveries (
                    event_id, target_key, channel, status, created_at, updated_at
                ) VALUES (?, ?, 'telegram', ?, '2020-01-01', '2020-01-01')
                """,
                (event_id, f"telegram:{index}", delivery_status),
            )
        connection.commit()

        counts = prune_database(connection, today=today)

        assert counts["alert_events"] == 0
        assert (
            connection.execute("SELECT COUNT(*) FROM alert_events").fetchone()[0] == 4
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM notification_deliveries"
            ).fetchone()[0]
            == 4
        )
    finally:
        connection.close()
