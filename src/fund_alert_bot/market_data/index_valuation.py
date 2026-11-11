"""The public CSI recent indicator file, with explicit metric identities."""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import date, datetime
from io import BytesIO
from typing import Any

import pandas as pd
import requests

from fund_alert_bot.market_data.exceptions import (
    MarketDataFetchError,
    MarketDataNormalizeError,
)

CSI_SOURCE = "csindex_indicator"
VALUATION_TIMEOUT_SECONDS = 8
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_VALUATION_CONTEXT_CHARS = 600
_COLUMNS = (
    "日期Date",
    "指数代码Index Code",
    "指数中文全称Chinese Name(Full)",
    "指数中文简称Index Chinese Name",
    "指数英文全称English Name(Full)",
    "指数英文简称Index English Name",
    "市盈率1（总股本）P/E1",
    "市盈率2（计算用股本）P/E2",
    "股息率1（总股本）D/P1",
    "股息率2（计算用股本）D/P2",
)


@dataclass(frozen=True, slots=True)
class IndexValuation:
    index_symbol: str
    name: str
    date: date
    pe1: float | None
    pe2: float | None
    dividend_yield1: float | None
    dividend_yield2: float | None

    def __post_init__(self) -> None:
        if not isinstance(self.index_symbol, str) or not re.fullmatch(
            r"[0-9]{6}", self.index_symbol
        ):
            raise ValueError("Invalid valuation index code.")
        if (
            not isinstance(self.name, str)
            or not self.name.strip()
            or len(self.name) > 80
            or not self.name.isprintable()
        ):
            raise ValueError("Invalid valuation index name.")
        if type(self.date) is not date:
            raise ValueError("Invalid valuation date.")
        values = (self.pe1, self.pe2, self.dividend_yield1, self.dividend_yield2)
        if all(value is None for value in values):
            raise ValueError("No usable valuation metrics.")
        for index, value in enumerate(values):
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not math.isfinite(value)
                or value < 0
                or (index < 2 and value == 0)
            ):
                raise ValueError("Invalid valuation metric.")

    def payload(self) -> dict[str, Any]:
        return {**asdict(self), "date": self.date.isoformat(), "source": CSI_SOURCE}

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> IndexValuation:
        values = dict(payload)
        if values.pop("source") != CSI_SOURCE:
            raise ValueError("Unexpected valuation source.")
        values["date"] = date.fromisoformat(values["date"])
        return cls(**values)


@dataclass(frozen=True, slots=True)
class ValuationContext:
    index_symbol: str
    as_of_date: date
    quote: IndexValuation | None = None
    refresh_failed: bool = False

    def __post_init__(self) -> None:
        if self.quote is not None and (
            self.quote.index_symbol != self.index_symbol
            or self.quote.date > self.as_of_date
        ):
            raise ValueError("Valuation context identity or date does not match.")

    def payload(self) -> dict[str, Any]:
        return {
            "index_symbol": self.index_symbol,
            "as_of_date": self.as_of_date.isoformat(),
            "source": CSI_SOURCE,
            "quote": None if self.quote is None else self.quote.payload(),
            "refresh_failed": self.refresh_failed,
        }

    def lines(self) -> tuple[str, ...]:
        if self.quote is None:
            return (
                f"Valuation index: {self.index_symbol}",
                "Valuation unavailable; drawdown reminder unchanged.",
            )
        quote = self.quote

        def number(value: float | None, suffix: str = "") -> str:
            return "—" if value is None else f"{value:.6g}{suffix}"

        lines = [
            f"Valuation index: {quote.name} ({quote.index_symbol})",
            f"PE1 / PE2: {number(quote.pe1)} / {number(quote.pe2)}",
            "Dividend yield D/P1 / D/P2: "
            f"{number(quote.dividend_yield1, '%')} / "
            f"{number(quote.dividend_yield2, '%')}",
            "PE1, D/P1: total shares; PE2, D/P2: calculation shares.",
            f"Valuation date: {quote.date.isoformat()}",
            "Valuation source: CSI official indicator file",
            "PB / historical percentiles: not provided by this source",
        ]
        if quote.date < self.as_of_date:
            lines.append("Valuation predates the price date.")
        if self.refresh_failed:
            lines.append("Cached valuation; refresh failed.")
        return tuple(lines)


def fetch_csindex_valuations(
    index_symbol: str,
    *,
    today: date,
    http_get: Callable[..., Any] = requests.get,
) -> tuple[IndexValuation, ...]:
    if not re.fullmatch(r"[0-9]{6}", index_symbol):
        raise ValueError("index_code must contain exactly six digits")
    url = (
        "https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/"
        f"file/autofile/indicator/{index_symbol}indicator.xls"
    )
    try:
        response = http_get(
            url, timeout=VALUATION_TIMEOUT_SECONDS, stream=True, allow_redirects=False
        )
        try:
            if response.status_code != 200:
                raise MarketDataFetchError(
                    "Official index valuation file is unavailable."
                )
            content = bytearray()
            for chunk in response.iter_content(chunk_size=65536):
                content.extend(chunk)
                if len(content) > MAX_RESPONSE_BYTES:
                    raise MarketDataFetchError(
                        "Index valuation file exceeds the size limit."
                    )
        finally:
            response.close()
    except requests.RequestException as exc:
        raise MarketDataFetchError("Index valuation request failed.") from exc
    try:
        return parse_csindex_valuations(
            pd.read_excel(BytesIO(content), engine="xlrd"),
            index_symbol=index_symbol,
            today=today,
        )
    except Exception as exc:
        raise MarketDataNormalizeError(
            "Official valuation data failed validation."
        ) from exc


def parse_csindex_valuations(
    frame: pd.DataFrame,
    *,
    index_symbol: str,
    today: date,
) -> tuple[IndexValuation, ...]:
    if (
        not 1 <= len(frame) <= 1000
        or tuple(str(col).strip() for col in frame.columns) != _COLUMNS
    ):
        raise ValueError("Unexpected valuation columns.")
    observations: dict[date, IndexValuation] = {}
    for row in frame.itertuples(index=False, name=None):
        code = row[1]
        if isinstance(code, int | float) and not isinstance(code, bool):
            if not math.isfinite(code) or int(code) != code:
                raise ValueError("Invalid source index code.")
            code = str(int(code)).zfill(6)
        if str(code).strip() != index_symbol:
            raise ValueError("Valuation file contains another index.")
        raw_date = row[0]
        if isinstance(raw_date, date):
            day = raw_date.date() if isinstance(raw_date, datetime) else raw_date
        else:
            if isinstance(raw_date, float) and raw_date.is_integer():
                raw_date = int(raw_date)
            day = datetime.strptime(str(raw_date), "%Y%m%d").date()
        if day > today:
            raise ValueError("Valuation file contains a future date.")
        metrics = []
        for index, raw in enumerate(row[6:10]):
            try:
                number = float(raw)
            except (TypeError, ValueError):
                number = math.nan
            metrics.append(
                number
                if not isinstance(raw, bool)
                and math.isfinite(number)
                and (number > 0 if index < 2 else number >= 0)
                else None
            )
        if all(value is None for value in metrics):
            continue
        if not isinstance(row[3], str):
            raise ValueError("Missing source index name.")
        quote = IndexValuation(index_symbol, str(row[3]).strip(), day, *metrics)
        if day in observations and observations[day] != quote:
            raise ValueError("Conflicting valuation observations for one date.")
        observations[day] = quote
    if not observations:
        raise ValueError("No usable index valuations.")
    return tuple(observations[day] for day in sorted(observations))
