from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace

import pytest
import requests

from fund_alert_bot.market_data import proxy as proxy_module
from fund_alert_bot.market_data.proxy import install_akshare_proxy


def test_paid_proxy_is_disabled_without_installing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = ModuleType("akshare_proxy_patch")
    fake.install_patch = lambda **_kwargs: pytest.fail("proxy should be disabled")
    monkeypatch.setitem(sys.modules, "akshare_proxy_patch", fake)
    monkeypatch.setattr(
        proxy_module.requests,
        "get",
        lambda *_args, **_kwargs: pytest.fail("balance should not be queried"),
    )

    assert install_akshare_proxy(enabled=False, auth_token="", retry=1) is False


def test_paid_proxy_requires_token() -> None:
    with pytest.raises(ValueError, match="AKSHARE_PROXY_AUTH_TOKEN"):
        install_akshare_proxy(enabled=True, auth_token="", retry=1)


def test_paid_proxy_requires_positive_retry() -> None:
    with pytest.raises(ValueError, match="AKSHARE_PROXY_RETRY"):
        install_akshare_proxy(enabled=True, auth_token="paid-token", retry=0)


def test_paid_proxy_uses_narrow_non_concurrent_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []
    balance_calls: list[dict[str, object]] = []
    fake = ModuleType("akshare_proxy_patch")

    def install_patch(*args: object, **kwargs: object) -> None:
        calls.append({"args": args, **kwargs})

    fake.install_patch = install_patch
    monkeypatch.setitem(sys.modules, "akshare_proxy_patch", fake)

    def get_balance(url: str, **kwargs: object) -> SimpleNamespace:
        balance_calls.append({"url": url, **kwargs})
        return SimpleNamespace(status_code=200, json=lambda: {"balance": 1})

    monkeypatch.setattr(
        proxy_module.requests,
        "get",
        get_balance,
    )

    assert install_akshare_proxy(enabled=True, auth_token="paid-token", retry=1)

    assert calls == [
        {
            "args": ("101.201.173.125",),
            "auth_token": "paid-token",
            "retry": 1,
            "hook_domains": [
                "fund.eastmoney.com",
                "push2.eastmoney.com",
                "push2his.eastmoney.com",
                "emweb.securities.eastmoney.com",
            ],
            "fast": False,
        }
    ]
    assert balance_calls == [
        {
            "url": "http://101.201.173.125:47001/api/token/paid-token",
            "timeout": 5,
        }
    ]


def test_paid_proxy_skips_install_when_balance_is_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = ModuleType("akshare_proxy_patch")
    fake.install_patch = lambda **_kwargs: pytest.fail("proxy should be disabled")
    monkeypatch.setitem(sys.modules, "akshare_proxy_patch", fake)
    monkeypatch.setattr(
        proxy_module.requests,
        "get",
        lambda *_args, **_kwargs: SimpleNamespace(
            status_code=200, json=lambda: {"balance": 0}
        ),
    )

    assert (
        install_akshare_proxy(enabled=True, auth_token="paid-token", retry=1) is False
    )


@pytest.mark.parametrize(
    "get_response",
    [
        lambda: SimpleNamespace(status_code=503, json=lambda: {"balance": 1}),
        lambda: SimpleNamespace(status_code=200, json=lambda: {"balance": "1"}),
    ],
)
def test_paid_proxy_skips_install_for_unavailable_or_malformed_balance(
    monkeypatch: pytest.MonkeyPatch,
    get_response,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake = ModuleType("akshare_proxy_patch")
    fake.install_patch = lambda **_kwargs: pytest.fail("proxy should be disabled")
    monkeypatch.setitem(sys.modules, "akshare_proxy_patch", fake)
    monkeypatch.setattr(
        proxy_module.requests,
        "get",
        lambda *_args, **_kwargs: get_response(),
    )

    assert (
        install_akshare_proxy(
            enabled=True,
            auth_token="secret-paid-token",
            retry=1,
        )
        is False
    )
    assert "secret-paid-token" not in caplog.text


def test_paid_proxy_skips_install_when_balance_request_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = ModuleType("akshare_proxy_patch")
    fake.install_patch = lambda **_kwargs: pytest.fail("proxy should be disabled")
    monkeypatch.setitem(sys.modules, "akshare_proxy_patch", fake)

    def fail_get(*_args: object, **_kwargs: object) -> None:
        raise requests.Timeout("token should not be logged")

    monkeypatch.setattr(proxy_module.requests, "get", fail_get)

    assert (
        install_akshare_proxy(enabled=True, auth_token="paid-token", retry=1) is False
    )
