"""Optional paid proxy setup for Eastmoney-backed AKShare requests."""

from __future__ import annotations

import logging
import math
from numbers import Real
from urllib.parse import quote

import requests

LOGGER = logging.getLogger(__name__)

_PROXY_GATEWAY = "101.201.173.125"
_PROXY_BALANCE_URL = "http://101.201.173.125:47001/api/token/{token}"
_PROXY_BALANCE_TIMEOUT_SECONDS = 5
_EASTMONEY_HOOK_DOMAINS = (
    "fund.eastmoney.com",
    "push2.eastmoney.com",
    "push2his.eastmoney.com",
    "emweb.securities.eastmoney.com",
)


def install_akshare_proxy(
    *,
    enabled: bool,
    auth_token: str,
    retry: int,
) -> bool:
    """Install the paid proxy patch before the first lazy AKShare import."""

    if not enabled:
        return False
    if not auth_token.strip():
        raise ValueError(
            "AKSHARE_PROXY_AUTH_TOKEN is required when AKSHARE_PROXY_ENABLED=true"
        )
    if retry < 1:
        raise ValueError("AKSHARE_PROXY_RETRY must be a positive integer")

    if not _has_positive_proxy_balance(auth_token):
        return False

    try:
        import akshare_proxy_patch
    except ImportError as exc:  # pragma: no cover - dependency is packaged
        raise RuntimeError(
            "AKSHARE_PROXY_ENABLED requires akshare-proxy-patch to be installed"
        ) from exc

    akshare_proxy_patch.install_patch(
        _PROXY_GATEWAY,
        auth_token=auth_token,
        retry=retry,
        hook_domains=list(_EASTMONEY_HOOK_DOMAINS),
        # The plugin's fast mode creates concurrent page requests. It is not
        # appropriate for this bot's paid, low-volume reminder workload.
        fast=False,
    )
    LOGGER.info(
        "AKShare Eastmoney paid proxy enabled hook_domains=%s retry=%s fast=false",
        list(_EASTMONEY_HOOK_DOMAINS),
        retry,
    )
    return True


def _has_positive_proxy_balance(auth_token: str) -> bool:
    """Return whether the paid proxy reports a finite positive balance."""

    try:
        response = requests.get(
            _PROXY_BALANCE_URL.format(token=quote(auth_token, safe="")),
            timeout=_PROXY_BALANCE_TIMEOUT_SECONDS,
        )
        if not 200 <= response.status_code < 300:
            reason = "http_status"
        else:
            payload = response.json()
            balance = payload.get("balance") if isinstance(payload, dict) else None
            if (
                isinstance(balance, Real)
                and not isinstance(balance, bool)
                and math.isfinite(float(balance))
                and balance > 0
            ):
                return True
            reason = "invalid_balance"
    except (requests.RequestException, ValueError, TypeError, OverflowError):
        reason = "request_or_response_error"

    LOGGER.warning("AKShare paid proxy balance check failed reason=%s", reason)
    return False
