import asyncio
import json
import urllib.error

import pytest

from openwebui_sendgrid_notifier import Tools


class Response:
    status = 202

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None


def configured_tools() -> Tools:
    tools = Tools()
    tools.valves.SENDGRID_API_KEY = "SG.test"
    tools.valves.SENDER_EMAIL = "notifications@example.com"
    tools.valves.SENDER_NAME = "My OpenWebUI"
    return tools


@pytest.fixture(autouse=True)
def reset_delivery_state():
    with Tools._state_lock:
        Tools._last_delivery_by_user.clear()
        Tools._sent_message_ids.clear()
        Tools._inflight_users.clear()
        Tools._inflight_message_ids.clear()


def test_sends_only_to_authenticated_user(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["payload"] = json.loads(request.data)
        captured["authorization"] = request.headers["Authorization"]
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    result = asyncio.run(
        configured_tools().send_email_notification(
            "  Report   ready ",
            "Your report is ready.",
            __user__={"email": "stephan@example.org"},
        )
    )

    assert "st***@example.org" in result
    assert captured["url"] == "https://api.sendgrid.com/v3/mail/send"
    assert captured["timeout"] == 15
    assert captured["authorization"] == "Bearer SG.test"
    assert captured["payload"]["personalizations"] == [
        {"to": [{"email": "stephan@example.org"}]}
    ]
    assert captured["payload"]["subject"] == "Report ready"


@pytest.mark.parametrize("user", [None, {}, {"email": "invalid"}])
def test_rejects_missing_or_invalid_user_email(user):
    result = asyncio.run(
        configured_tools().send_email_notification("Subject", "Message", __user__=user)
    )
    assert "no valid email address" in result


def test_requires_api_key():
    tools = configured_tools()
    tools.valves.SENDGRID_API_KEY = ""
    result = asyncio.run(
        tools.send_email_notification(
            "Subject", "Message", __user__={"email": "user@example.org"}
        )
    )
    assert "API key valve is not configured" in result


def test_returns_safe_sendgrid_auth_error(monkeypatch):
    def fake_urlopen(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    result = asyncio.run(
        configured_tools().send_email_notification(
            "Subject", "Message", __user__={"email": "user@example.org"}
        )
    )
    assert "rejected the API credentials" in result
    assert "SG.test" not in result


def test_suppresses_duplicate_message_id(monkeypatch):
    calls = 0

    def fake_urlopen(request, timeout):
        nonlocal calls
        calls += 1
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    tools = configured_tools()
    user = {"id": "user-1", "email": "user1@example.org"}

    first = asyncio.run(
        tools.send_email_notification(
            "Subject", "Message", __user__=user, __message_id__="message-1"
        )
    )
    duplicate = asyncio.run(
        tools.send_email_notification(
            "Changed subject",
            "Changed message",
            __user__=user,
            __message_id__="message-1",
        )
    )

    assert "sent successfully" in first
    assert "duplicate suppressed" in duplicate
    assert calls == 1


def test_rate_limits_user_to_one_success_every_ten_minutes(monkeypatch):
    now = 1_000.0
    calls = 0

    def fake_monotonic():
        return now

    def fake_urlopen(request, timeout):
        nonlocal calls
        calls += 1
        return Response()

    monkeypatch.setattr("openwebui_sendgrid_notifier.time.monotonic", fake_monotonic)
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    tools = configured_tools()
    user = {"id": "user-1", "email": "user1@example.org"}

    asyncio.run(
        tools.send_email_notification(
            "First", "Message", __user__=user, __message_id__="message-1"
        )
    )
    limited = asyncio.run(
        tools.send_email_notification(
            "Second", "Message", __user__=user, __message_id__="message-2"
        )
    )
    now += 600
    allowed = asyncio.run(
        tools.send_email_notification(
            "Third", "Message", __user__=user, __message_id__="message-3"
        )
    )

    assert "Try again in 10 minutes" in limited
    assert "sent successfully" in allowed
    assert calls == 2


def test_rate_limit_is_per_user(monkeypatch):
    calls = 0

    def fake_urlopen(request, timeout):
        nonlocal calls
        calls += 1
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    tools = configured_tools()

    for user_id in ("user-1", "user-2"):
        result = asyncio.run(
            tools.send_email_notification(
                "Subject",
                "Message",
                __user__={"id": user_id, "email": f"{user_id}@example.org"},
                __message_id__=f"message-{user_id}",
            )
        )
        assert "sent successfully" in result

    assert calls == 2


def test_failed_delivery_does_not_consume_rate_limit(monkeypatch):
    calls = 0

    def fake_urlopen(request, timeout):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise urllib.error.URLError("temporary failure")
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    tools = configured_tools()
    user = {"id": "user-1", "email": "user1@example.org"}

    failed = asyncio.run(
        tools.send_email_notification(
            "Subject", "Message", __user__=user, __message_id__="message-1"
        )
    )
    retried = asyncio.run(
        tools.send_email_notification(
            "Subject", "Message", __user__=user, __message_id__="message-1"
        )
    )

    assert "could not be reached" in failed
    assert "sent successfully" in retried
    assert calls == 2


def test_rate_limit_can_be_disabled(monkeypatch):
    calls = 0

    def fake_urlopen(request, timeout):
        nonlocal calls
        calls += 1
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    tools = configured_tools()
    tools.valves.RATE_LIMIT_MINUTES = 0
    user = {"id": "user-1", "email": "user1@example.org"}

    for message_id in ("message-1", "message-2"):
        result = asyncio.run(
            tools.send_email_notification(
                "Subject",
                "Message",
                __user__=user,
                __message_id__=message_id,
            )
        )
        assert "sent successfully" in result

    assert calls == 2
