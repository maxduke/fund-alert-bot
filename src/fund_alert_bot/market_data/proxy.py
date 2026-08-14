"""Optional paid proxy setup for Eastmoney-backed AKShare requests."""

from __future__ import annotations

import logging

LOGGER = logging.getLogger(__name__)

_PROXY_GATEWAY = "101.201.173.125"
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
