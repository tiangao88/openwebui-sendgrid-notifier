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
