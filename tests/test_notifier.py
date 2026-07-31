import asyncio
import base64
import json
import sys
import types
import urllib.error
import urllib.parse
from types import SimpleNamespace

import pytest

from openwebui_sendgrid_notifier import EmailAttachment, Tools


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
    assert "attachments" not in captured["payload"]


def test_sends_file_from_selected_open_terminal(monkeypatch):
    captured = {}

    async def fake_get_attachment(
        self, path, request, user, metadata, oauth_token
    ):
        captured["attachment_request"] = {
            "path": path,
            "request": request,
            "user": user,
            "metadata": metadata,
            "oauth_token": oauth_token,
        }
        return EmailAttachment(b"report,data\n", "report.csv", "text/csv")

    def fake_urlopen(request, timeout):
        captured["payload"] = json.loads(request.data)
        return Response()

    monkeypatch.setattr(Tools, "_get_open_terminal_attachment", fake_get_attachment)
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    request_context = object()
    user = {"id": "user-1", "email": "stephan@example.org"}
    metadata = {"terminal_id": "terminal-1", "chat_id": "chat-1"}

    result = asyncio.run(
        configured_tools().send_email_notification(
            "Report ready",
            "The report is attached.",
            attachment_path="/home/user/report.csv",
            __user__=user,
            __metadata__=metadata,
            __request__=request_context,
        )
    )

    assert "sent successfully" in result
    assert captured["attachment_request"] == {
        "path": "/home/user/report.csv",
        "request": request_context,
        "user": user,
        "metadata": metadata,
        "oauth_token": None,
    }
    assert captured["payload"]["attachments"] == [
        {
            "content": base64.b64encode(b"report,data\n").decode("ascii"),
            "type": "text/csv",
            "filename": "report.csv",
            "disposition": "attachment",
        }
    ]


def test_attachment_requires_selected_system_terminal(monkeypatch):
    called = False

    def fake_urlopen(request, timeout):
        nonlocal called
        called = True
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    result = asyncio.run(
        configured_tools().send_email_notification(
            "Report ready",
            "The report is attached.",
            attachment_path="/home/user/report.csv",
            __user__={"id": "user-1", "email": "stephan@example.org"},
            __metadata__={},
            __request__=object(),
        )
    )

    assert "No system-level Open Terminal connection is selected" in result
    assert called is False


def test_reads_selected_terminal_from_openwebui_configuration(monkeypatch):
    captured = {}

    class FakeConfig:
        @staticmethod
        async def get(key, default):
            assert key == "terminal_server.connections"
            return [
                {
                    "id": "terminal-1",
                    "enabled": True,
                    "url": "http://open-terminal:8000",
                    "key": "terminal-secret",
                    "auth_type": "bearer",
                    "config": {
                        "access_grants": [
                            {
                                "principal_type": "user",
                                "principal_id": "user-1",
                                "permission": "read",
                            }
                        ]
                    },
                }
            ]

    async def fake_has_connection_access(user, connection):
        captured["access_user"] = user
        captured["access_connection"] = connection
        return True

    class TerminalResponse:
        headers = {"Content-Length": "4", "Content-Type": "text/csv; charset=utf-8"}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self, limit):
            captured["read_limit"] = limit
            return b"a,b\n"

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["headers"] = dict(request.headers)
        captured["timeout"] = timeout
        return TerminalResponse()

    module_map = {
        "open_webui": types.ModuleType("open_webui"),
        "open_webui.models": types.ModuleType("open_webui.models"),
        "open_webui.models.config": types.ModuleType("open_webui.models.config"),
        "open_webui.utils": types.ModuleType("open_webui.utils"),
        "open_webui.utils.access_control": types.ModuleType(
            "open_webui.utils.access_control"
        ),
        "open_webui.utils.terminals": types.ModuleType("open_webui.utils.terminals"),
    }
    module_map["open_webui.models.config"].Config = FakeConfig
    module_map[
        "open_webui.utils.access_control"
    ].has_connection_access = fake_has_connection_access
    module_map["open_webui.utils.terminals"].get_terminal_server_url = (
        lambda connection: connection["url"]
    )
    for name, module in module_map.items():
        if name in ("open_webui", "open_webui.models", "open_webui.utils"):
            module.__path__ = []
        monkeypatch.setitem(sys.modules, name, module)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    request_context = SimpleNamespace(state=SimpleNamespace(), headers={})
    attachment = asyncio.run(
        configured_tools()._get_open_terminal_attachment(
            "/home/user/report.csv",
            request_context,
            {"id": "user-1", "role": "user"},
            {"terminal_id": "terminal-1", "chat_id": "chat-1"},
            None,
        )
    )

    assert attachment == EmailAttachment(b"a,b\n", "report.csv", "text/csv")
    parsed_url = urllib.parse.urlparse(captured["url"])
    assert parsed_url.path == "/files/view"
    assert urllib.parse.parse_qs(parsed_url.query) == {
        "path": ["/home/user/report.csv"]
    }
    assert captured["headers"]["Authorization"] == "Bearer terminal-secret"
    assert captured["headers"]["X-user-id"] == "user-1"
    assert captured["headers"]["X-session-id"] == "chat-1"
    assert captured["timeout"] == 30
    assert captured["read_limit"] == Tools._MAX_ATTACHMENT_BYTES + 1
    assert captured["access_user"].id == "user-1"


def test_rejects_oversized_terminal_attachment(monkeypatch):
    class OversizedResponse:
        headers = {"Content-Length": str(Tools._MAX_ATTACHMENT_BYTES + 1)}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    monkeypatch.setattr(
        "urllib.request.urlopen", lambda request, timeout: OversizedResponse()
    )

    with pytest.raises(ValueError, match="exceeds the 10 MB"):
        configured_tools()._download_terminal_file("http://terminal/file", {})


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
