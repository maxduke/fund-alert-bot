"""Optional plan context, isolated from alert and position decisions."""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from fund_alert_bot.db import write_transaction
from fund_alert_bot.market_data.index_valuation import IndexValuation, ValuationContext
from fund_alert_bot.rules.drawdown_plan import (
    parse_drawdown_plan_config,
    validate_drawdown_plan_notification_size,
)

LOGGER = logging.getLogger(__name__)
_SUCCESS_TTL_SECONDS = 3600
_FAILURE_TTL_SECONDS = 300

if TYPE_CHECKING:
    from fund_alert_bot.market_data.provider import MarketDataProvider


def _validated_quotes(
    values: object, index_symbol: str, today: date
) -> tuple[IndexValuation, ...]:
    if not isinstance(values, list | tuple) or not 1 <= len(values) <= 1000:
        raise ValueError("Invalid valuation observations.")
    if any(
        not isinstance(value, IndexValuation)
        or value.index_symbol != index_symbol
        or value.date > today
        for value in values
    ):
        raise ValueError("Valuation identity or date does not match.")
    by_date: dict[date, IndexValuation] = {}
    for value in values:
        if value.date in by_date and by_date[value.date] != value:
            raise ValueError("Conflicting valuation dates.")
        by_date[value.date] = value
    return tuple(by_date[day] for day in sorted(by_date))


def get_valuation_context(
    connection: sqlite3.Connection,
    provider: MarketDataProvider | None,
    index_symbol: str | None,
    *,
    as_of_date: date,
    force_refresh: bool = False,
    now: datetime | None = None,
) -> ValuationContext | None:
    """Bound optional work and preserve a validated cached snapshot on failure."""

    if index_symbol is None:
        return None
    now = now or datetime.now(UTC)
    today = now.astimezone(ZoneInfo("Asia/Shanghai")).date()
    quotes: tuple[IndexValuation, ...] = ()
    row = None
    try:
        row = connection.execute(
            "SELECT * FROM index_valuation_cache WHERE index_symbol = ?",
            (index_symbol,),
        ).fetchone()
        if row is not None and row["observations_json"] is not None:
            values = json.loads(str(row["observations_json"]))
            quotes = _validated_quotes(
                [IndexValuation.from_payload(item) for item in values],
                index_symbol,
                today,
            )
    except Exception as exc:  # Optional context must not block an alert.
        LOGGER.warning(
            "Invalid valuation cache index=%s error=%s",
            index_symbol,
            type(exc).__name__,
        )
        row = None
    refresh_failed = row is not None and bool(row["refresh_failed"])
    fresh = False
    if row is not None:
        try:
            age = (
                now - datetime.fromisoformat(str(row["attempted_at"]))
            ).total_seconds()
            fresh = (
                0
                <= age
                < (_FAILURE_TTL_SECONDS if refresh_failed else _SUCCESS_TTL_SECONDS)
            )
        except (ValueError, TypeError):
            pass
    if provider is not None and (force_refresh or not fresh):
        succeeded_at = None if row is None else row["succeeded_at"]
        try:
            quotes = _validated_quotes(
                provider.get_index_valuations(index_symbol), index_symbol, today
            )
            succeeded_at = now.isoformat()
            refresh_failed = False
        except Exception as exc:
            refresh_failed = True
            LOGGER.warning(
                "Valuation refresh failed index=%s error=%s",
                index_symbol,
                type(exc).__name__,
            )
        try:
            with write_transaction(connection):
                connection.execute(
                    """
                    INSERT INTO index_valuation_cache
                        (index_symbol, observations_json, attempted_at,
                         succeeded_at, refresh_failed)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(index_symbol) DO UPDATE SET
                        observations_json = excluded.observations_json,
                        attempted_at = excluded.attempted_at,
                        succeeded_at = excluded.succeeded_at,
                        refresh_failed = excluded.refresh_failed
                    """,
                    (
                        index_symbol,
                        json.dumps([quote.payload() for quote in quotes])
                        if quotes
                        else None,
                        now.isoformat(),
                        succeeded_at,
                        int(refresh_failed),
                    ),
                )
        except Exception as exc:
            LOGGER.warning(
                "Valuation cache write failed index=%s error=%s",
                index_symbol,
                type(exc).__name__,
            )
    eligible = [quote for quote in quotes if quote.date <= as_of_date]
    return ValuationContext(
        index_symbol,
        as_of_date,
        max(eligible, key=lambda value: value.date) if eligible else None,
        refresh_failed,
    )


def update_plan_index(
    connection: sqlite3.Connection,
    *,
    rule_id: int,
    index_symbol: str | None,
    expected_params_json: str,
) -> None:
    """Save only the explicit context association, without touching plan state."""

    with write_transaction(connection):
        row = connection.execute(
            "SELECT * FROM rules WHERE id = ? "
            "AND type = 'drawdown_plan' AND enabled = 1",
            (rule_id,),
        ).fetchone()
        if row is None:
            raise ValueError("Enabled drawdown plan was not found.")
        if str(row["params_json"]) != expected_params_json:
            raise ValueError("Plan changed. Rerun /set_plan_index.")
        params = json.loads(str(row["params_json"]))
        if index_symbol is None:
            params.pop("valuation_index_symbol", None)
        else:
            params["valuation_index_symbol"] = index_symbol
        config = parse_drawdown_plan_config(
            reference_symbol=str(row["symbol"]),
            asset_type=str(row["asset_type"]),
            params=params,
        )
        validate_drawdown_plan_notification_size(
            name=str(row["name"]), reference_symbol=str(row["symbol"]), config=config
        )
        connection.execute(
            "UPDATE rules SET params_json = ?, updated_at = ? WHERE id = ?",
            (
                json.dumps(params, ensure_ascii=False),
                datetime.now(UTC).isoformat(),
                rule_id,
            ),
        )
