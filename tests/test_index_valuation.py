from datetime import date

import pandas as pd
import pytest
import requests

from fund_alert_bot.market_data import (
    AkshareMarketDataProvider,
    MarketDataFetchError,
    MarketDataNormalizeError,
    index_valuation,
)
from fund_alert_bot.market_data.index_valuation import (
    MAX_RESPONSE_BYTES,
    IndexValuation,
    ValuationContext,
    fetch_csindex_valuations,
    parse_csindex_valuations,
)


@pytest.fixture
def frame():
    return pd.DataFrame(
        [
            [
                20261110,
                510,
                "中证A500指数",
                "中证A500",
                "CSI A500",
                "A500",
                15.52,
                18.32,
                2.74,
                2.26,
            ],
            [
                20261109,
                510,
                "中证A500指数",
                "中证A500",
                "CSI A500",
                "A500",
                15.5,
                18.3,
                2.7,
                2.2,
            ],
        ],
        columns=[
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
        ],
    )


class Response:
    def __init__(self, *, status=200, chunks=(b"official XLS bytes",)):
        self.status_code = status
        self.chunks = chunks
        self.closed = False

    def iter_content(self, chunk_size):
        assert chunk_size == 65536
        yield from self.chunks

    def close(self):
        self.closed = True


def test_official_request_is_bounded_and_preserves_metric_identity(frame, monkeypatch):
    response = Response()
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return response

    def read_excel(content, *, engine):
        assert content.read() == b"official XLS bytes"
        assert engine == "xlrd"
        return frame

    monkeypatch.setattr(index_valuation.pd, "read_excel", read_excel)
    provider = AkshareMarketDataProvider(
        http_get=get, today_factory=lambda: date(2026, 11, 11)
    )
    values = provider.get_index_valuations("000510")
    assert [(value.date, value.index_symbol) for value in values] == [
        (date(2026, 11, 9), "000510"),
        (date(2026, 11, 10), "000510"),
    ]
    assert values[-1].pe1 == 15.52 and values[-1].pe2 == 18.32
    assert values[-1].dividend_yield1 == 2.74
    assert "pb" not in values[-1].payload()
    assert calls == [
        (
            "https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/file/autofile/indicator/000510indicator.xls",
            {"timeout": 8, "stream": True, "allow_redirects": False},
        )
    ]
    assert response.closed
    assert provider.request_counts() == {"csindex:valuation": 1}


@pytest.mark.parametrize(
    "problem",
    ["columns", "code", "date", "future", "name", "missing_name", "duplicate", "empty"],
)
def test_ambiguous_or_mismatched_source_is_rejected(frame, problem):
    if problem == "columns":
        frame.columns = [*frame.columns[:6], "unverified TTM PE", *frame.columns[7:]]
    elif problem == "code":
        frame.iloc[1, 1] = 688
    elif problem == "date":
        frame.iloc[1, 0] = 20261310
    elif problem == "future":
        frame.iloc[1, 0] = 20261112
    elif problem == "name":
        frame.iloc[1, 3] = "bad\nname"
    elif problem == "missing_name":
        frame.iloc[1, 3] = None
    elif problem == "duplicate":
        frame.iloc[1, 0] = 20261110
    else:
        frame = frame.iloc[:0]
    with pytest.raises(ValueError):
        parse_csindex_valuations(frame, index_symbol="000510", today=date(2026, 11, 11))


def test_missing_or_nonpositive_pe_is_not_zero_filled_but_zero_dividend_is_valid(frame):
    frame.iloc[0, 6:10] = [0, -12, 0, float("inf")]
    quote = parse_csindex_valuations(
        frame, index_symbol="000510", today=date(2026, 11, 11)
    )[-1]
    assert quote.pe1 is None and quote.pe2 is None
    assert quote.dividend_yield1 == 0 and quote.dividend_yield2 is None
    text = "\n".join(ValuationContext("000510", date(2026, 11, 10), quote).lines())
    assert "PE1 / PE2: — / —" in text
    assert "0% / —" in text


@pytest.mark.parametrize(
    "status,chunks",
    [
        (302, (b"redirect",)),
        (404, (b"missing",)),
        (200, (b"x" * (MAX_RESPONSE_BYTES + 1),)),
    ],
)
def test_bad_http_or_oversized_file_is_closed_and_rejected(status, chunks):
    response = Response(status=status, chunks=chunks)
    with pytest.raises(MarketDataFetchError):
        fetch_csindex_valuations(
            "000510", today=date(2026, 11, 11), http_get=lambda *a, **k: response
        )
    assert response.closed


def test_timeout_and_bad_workbook_are_provider_failures():
    def timeout(*args, **kwargs):
        raise requests.Timeout("bounded")

    with pytest.raises(MarketDataFetchError):
        fetch_csindex_valuations("000510", today=date(2026, 11, 11), http_get=timeout)
    response = Response(chunks=(b"not an XLS file",))
    with pytest.raises(MarketDataNormalizeError):
        fetch_csindex_valuations(
            "000510", today=date(2026, 11, 11), http_get=lambda *a, **k: response
        )
    assert response.closed


@pytest.mark.parametrize(
    "code", ["../000510", "000510?x", "000510/", "510", "https://other.test"]
)
def test_index_input_cannot_change_endpoint(code):
    def unexpected_request(*args, **kwargs):
        pytest.fail("invalid code must not make a request")

    with pytest.raises(ValueError):
        fetch_csindex_valuations(
            code, today=date(2026, 11, 11), http_get=unexpected_request
        )


def test_context_cannot_display_a_future_quote_or_another_index():
    quote = IndexValuation(
        "000510", "A500", date(2026, 11, 10), 15.52, 18.32, 2.74, 2.26
    )
    with pytest.raises(ValueError):
        ValuationContext("000688", date(2026, 11, 10), quote)
    with pytest.raises(ValueError):
        ValuationContext("000510", date(2026, 11, 9), quote)
