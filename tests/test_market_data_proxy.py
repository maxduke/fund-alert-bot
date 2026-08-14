from __future__ import annotations

import sys
from types import ModuleType

import pytest

from fund_alert_bot.market_data.proxy import install_akshare_proxy


def test_paid_proxy_is_disabled_without_installing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = ModuleType("akshare_proxy_patch")
    fake.install_patch = lambda **_kwargs: pytest.fail("proxy should be disabled")
    monkeypatch.setitem(sys.modules, "akshare_proxy_patch", fake)

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
    fake = ModuleType("akshare_proxy_patch")

    def install_patch(*args: object, **kwargs: object) -> None:
        calls.append({"args": args, **kwargs})

    fake.install_patch = install_patch
    monkeypatch.setitem(sys.modules, "akshare_proxy_patch", fake)

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
