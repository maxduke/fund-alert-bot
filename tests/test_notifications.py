from __future__ import annotations

import asyncio
import logging

import requests

from fund_alert_bot.notifications.bark import BarkNotificationChannel
from fund_alert_bot.notifications.base import NotificationMessage, NotificationResult
from fund_alert_bot.notifications.ntfy import NtfyNotificationChannel
from fund_alert_bot.notifications.service import NotificationService
from fund_alert_bot.notifications.webhook import WebhookNotificationChannel


def test_bark_channel_posts_with_timeout(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    def fake_post(url: str, **kwargs: object) -> object:
        calls.append({"url": url, **kwargs})
        return FakeResponse(status_code=200)

    monkeypatch.setattr("fund_alert_bot.notifications.bark.requests.post", fake_post)
    channel = BarkNotificationChannel(
        server_url="https://bark.example.test",
        device_key="secret-device-key",
    )

    result = asyncio.run(channel.send(_message()))

    assert result == NotificationResult(channel="bark", success=True, detail="sent")
    assert calls == [
        {
            "url": "https://bark.example.test/push",
            "json": {
                "device_key": "secret-device-key",
                "title": "Drawdown alert",
                "body": "399006 is down 10.0%.",
            },
            "timeout": 10,
        }
    ]


def test_ntfy_channel_posts_with_timeout(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    def fake_post(url: str, **kwargs: object) -> object:
        calls.append({"url": url, **kwargs})
        return FakeResponse(status_code=200)

    monkeypatch.setattr("fund_alert_bot.notifications.ntfy.requests.post", fake_post)
    channel = NtfyNotificationChannel(
        server_url="https://ntfy.example.test",
        topic="secret-topic",
    )

    result = asyncio.run(channel.send(_message()))

    assert result == NotificationResult(channel="ntfy", success=True, detail="sent")
    assert calls == [
        {
            "url": "https://ntfy.example.test/secret-topic",
            "data": b"399006 is down 10.0%.",
            "headers": {"Title": "Drawdown alert"},
            "timeout": 10,
        }
    ]


def test_webhook_channel_posts_with_timeout(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    def fake_post(url: str, **kwargs: object) -> object:
        calls.append({"url": url, **kwargs})
        return FakeResponse(status_code=200)

    monkeypatch.setattr(
        "fund_alert_bot.notifications.webhook.requests.post",
        fake_post,
    )
    channel = WebhookNotificationChannel(url="https://hooks.example.test/secret")

    result = asyncio.run(channel.send(_message()))

    assert result == NotificationResult(channel="webhook", success=True, detail="sent")
    assert calls == [
        {
            "url": "https://hooks.example.test/secret",
            "json": {
                "title": "Drawdown alert",
                "body": "399006 is down 10.0%.",
            },
            "timeout": 10,
        }
    ]


def test_notification_service_continues_after_channel_failure() -> None:
    failing = FailingChannel()
    recording = RecordingChannel()
    service = NotificationService([failing, recording])

    results = asyncio.run(service.send_alert(title="Hello", body="World"))

    assert results == [
        NotificationResult(
            channel="failing",
            success=False,
            detail="unexpected_error=RuntimeError",
        ),
        NotificationResult(channel="recording", success=True, detail="sent"),
    ]
    assert recording.messages == [NotificationMessage(title="Hello", body="World")]


def test_request_failures_do_not_log_sensitive_webhook_url(
    monkeypatch,
    caplog,
) -> None:
    secret_url = "https://hooks.example.test/path/secret-token?key=secret"

    def fake_post(url: str, **kwargs: object) -> object:
        del url, kwargs
        raise requests.Timeout("timed out")

    monkeypatch.setattr(
        "fund_alert_bot.notifications.webhook.requests.post",
        fake_post,
    )
    service = NotificationService([WebhookNotificationChannel(url=secret_url)])
    caplog.set_level(logging.WARNING, logger="fund_alert_bot.notifications.service")

    results = asyncio.run(service.send_alert(title="Hello", body="World"))

    assert results == [
        NotificationResult(
            channel="webhook",
            success=False,
            detail="request_error=Timeout",
        )
    ]
    assert secret_url not in caplog.text
    assert "secret-token" not in caplog.text


class FakeResponse:
    def __init__(self, *, status_code: int) -> None:
        self.status_code = status_code


class FailingChannel:
    name = "failing"

    async def send(self, message: NotificationMessage) -> NotificationResult:
        del message
        raise RuntimeError("boom")


class RecordingChannel:
    name = "recording"

    def __init__(self) -> None:
        self.messages: list[NotificationMessage] = []

    async def send(self, message: NotificationMessage) -> NotificationResult:
        self.messages.append(message)
        return NotificationResult(channel=self.name, success=True, detail="sent")


def _message() -> NotificationMessage:
    return NotificationMessage(
        title="Drawdown alert",
        body="399006 is down 10.0%.",
    )
