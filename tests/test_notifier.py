import asyncio
import base64
import json
import struct
import sys
import types
import urllib.error
import urllib.parse
from types import SimpleNamespace

import pytest

from openwebui_sendgrid_notifier import EmailAttachment, TerminalContext, Tools


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
    assert [item["type"] for item in captured["payload"]["content"]] == [
        "text/plain",
        "text/html",
    ]
    assert captured["payload"]["content"][0]["value"] == "Your report is ready."
    assert "Your report is ready." in captured["payload"]["content"][1]["value"]
    assert "attachments" not in captured["payload"]


def test_sends_file_from_selected_open_terminal(monkeypatch):
    captured = {}

    async def fake_get_attachment(self, path, request, user, metadata, oauth_token):
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
    module_map["open_webui.utils.access_control"].has_connection_access = (
        fake_has_connection_access
    )
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
        configured_tools()._download_terminal_path(
            TerminalContext("http://terminal", {}),
            "/file",
            Tools._MAX_ATTACHMENT_BYTES,
            "attachment",
        )


def test_renders_markdown_table_and_unicode_as_styled_html(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout):
        captured["payload"] = json.loads(request.data)
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    message = """## Weekly report

**Status:** On track ✅

| Workstream | Owner |
|---|---|
| Data migration | Alice |
"""
    result = asyncio.run(
        configured_tools().send_email_notification(
            "Weekly report", message, __user__={"email": "user@example.org"}
        )
    )

    assert "sent successfully" in result
    plain, rich = captured["payload"]["content"]
    assert plain == {"type": "text/plain", "value": message.strip()}
    assert rich["type"] == "text/html"
    assert "<h2 " in rich["value"]
    assert "<strong " in rich["value"]
    assert "<table " in rich["value"]
    assert "border-collapse:collapse" in rich["value"]
    assert "background:#f1f5f9" in rich["value"]
    assert "On track ✅" in rich["value"]


def test_sanitizes_raw_html_unsafe_links_and_remote_images(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout):
        captured["payload"] = json.loads(request.data)
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    message = """<script>alert('x')</script>

[Unsafe](javascript:alert(1))

[Safe](https://example.com/report?a=1&b=2)

![tracking pixel](https://tracker.example/pixel.png)
"""
    asyncio.run(
        configured_tools().send_email_notification(
            "Security test", message, __user__={"email": "user@example.org"}
        )
    )

    rich = captured["payload"]["content"][1]["value"]
    assert "<script" not in rich
    assert "&lt;script&gt;" in rich
    assert "javascript:" not in rich
    assert 'href="https://example.com/report?a=1&amp;b=2"' in rich
    assert "tracker.example" not in rich
    assert "Remote image omitted: tracking pixel" in rich


def test_embeds_rendered_mermaid_as_inline_cid_attachment(monkeypatch):
    captured = {}
    png = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + struct.pack(">II", 1_800, 900)

    async def fake_context(self, request, user, metadata, oauth_token):
        return TerminalContext("http://terminal", {"Authorization": "Bearer test"})

    async def fake_render(self, source, diagram_number, terminal):
        captured["source"] = source
        captured["diagram_number"] = diagram_number
        captured["terminal"] = terminal
        return png

    def fake_urlopen(request, timeout):
        captured["payload"] = json.loads(request.data)
        return Response()

    monkeypatch.setattr(Tools, "_get_open_terminal_context", fake_context)
    monkeypatch.setattr(Tools, "_render_mermaid_png", fake_render)
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    message = """## Architecture

```mermaid
flowchart LR
    A[OpenWebUI] --> B[Email]
```
"""
    result = asyncio.run(
        configured_tools().send_email_notification(
            "Architecture",
            message,
            __user__={"id": "user-1", "email": "user@example.org"},
            __metadata__={"terminal_id": "terminal-1"},
            __request__=object(),
        )
    )

    assert "Embedded 1 of 1 Mermaid diagram" in result
    assert captured["source"] == "flowchart LR\n    A[OpenWebUI] --> B[Email]"
    assert captured["diagram_number"] == 1
    assert captured["terminal"].base_url == "http://terminal"
    payload = captured["payload"]
    assert payload["content"][0]["value"] == message.strip()
    inline = payload["attachments"][0]
    assert inline["disposition"] == "inline"
    assert inline["type"] == "image/png"
    assert inline["filename"] == "mermaid-diagram-1.png"
    assert base64.b64decode(inline["content"]) == png
    assert f'cid:{inline["content_id"]}' in payload["content"][1]["value"]
    assert "flowchart LR" not in payload["content"][1]["value"]


def test_mermaid_failure_falls_back_to_escaped_source_and_still_sends(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout):
        captured["payload"] = json.loads(request.data)
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    message = """```mermaid
flowchart LR
    A[One] --> B[Two]
```"""
    result = asyncio.run(
        configured_tools().send_email_notification(
            "Diagram",
            message,
            __user__={"id": "user-1", "email": "user@example.org"},
        )
    )

    assert "Embedded 0 of 1 Mermaid diagram" in result
    assert "source fallback" in result
    assert "attachments" not in captured["payload"]
    rich = captured["payload"]["content"][1]["value"]
    assert "No usable system-level Open Terminal" in rich
    assert "flowchart LR" in rich
    assert "A[One] --&gt; B[Two]" in rich


def test_renders_at_most_three_mermaid_diagrams(monkeypatch):
    captured = {"rendered": []}
    png = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + struct.pack(">II", 1_800, 900)

    async def fake_context(self, request, user, metadata, oauth_token):
        return TerminalContext("http://terminal", {})

    async def fake_render(self, source, diagram_number, terminal):
        captured["rendered"].append(diagram_number)
        return png

    def fake_urlopen(request, timeout):
        captured["payload"] = json.loads(request.data)
        return Response()

    monkeypatch.setattr(Tools, "_get_open_terminal_context", fake_context)
    monkeypatch.setattr(Tools, "_render_mermaid_png", fake_render)
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    message = "\n\n".join(
        f"```mermaid\nflowchart LR\nA{i} --> B{i}\n```" for i in range(4)
    )
    result = asyncio.run(
        configured_tools().send_email_notification(
            "Four diagrams",
            message,
            __user__={"id": "user-1", "email": "user@example.org"},
            __metadata__={"terminal_id": "terminal-1"},
            __request__=object(),
        )
    )

    assert captured["rendered"] == [1, 2, 3]
    assert len(captured["payload"]["attachments"]) == 3
    assert "Embedded 3 of 4 Mermaid diagram" in result
    assert "Only the first three" in captured["payload"]["content"][1]["value"]


def test_mermaid_renderer_uses_pinned_cli_validates_png_and_cleans_up(monkeypatch):
    commands = []
    downloads = []
    png = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + struct.pack(">II", 1_800, 900)

    async def fake_run(self, terminal, command, wait_seconds):
        commands.append((command, wait_seconds))
        return {"status": "done", "exit_code": 0}

    def fake_download(self, terminal, path, max_bytes, label):
        downloads.append((terminal, path, max_bytes, label))
        return png, "image/png"

    monkeypatch.setattr(Tools, "_run_terminal_command", fake_run)
    monkeypatch.setattr(Tools, "_download_terminal_path", fake_download)
    terminal = TerminalContext("http://terminal", {"X-User-Id": "user-1"}, "/home/user")
    result = asyncio.run(
        configured_tools()._render_mermaid_png("flowchart LR\nA --> B", 1, terminal)
    )

    assert result == png
    assert len(commands) == 2
    render_command, render_wait = commands[0]
    cleanup_command, cleanup_wait = commands[1]
    assert "timeout 180s" in render_command
    assert "@mermaid-js/mermaid-cli@11.16.0" in render_command
    assert "-t neutral -b white -w 900 -s 2" in render_command
    assert "/home/user/.openwebui-sendgrid-notifier/" in render_command
    assert "flowchart LR" not in render_command
    assert render_wait == 195
    assert cleanup_command.startswith("rm -f ")
    assert "/home/user/.openwebui-sendgrid-notifier/" in cleanup_command
    assert cleanup_wait == 10
    assert downloads[0][1].startswith(
        "/home/user/.openwebui-sendgrid-notifier/mermaid-"
    )
    assert downloads[0][2:] == (Tools._MAX_DIAGRAM_BYTES, "Mermaid diagram")


def test_mermaid_renderer_resolves_terminal_home_before_creating_files(monkeypatch):
    captured = {}

    def fake_request(self, url, headers, method, payload, timeout):
        captured.update(
            {
                "url": url,
                "headers": headers,
                "method": method,
                "payload": payload,
                "timeout": timeout,
            }
        )
        return {"cwd": "/home/user/projects/current", "home": "/home/user"}

    monkeypatch.setattr(Tools, "_request_terminal_json", fake_request)
    terminal = TerminalContext(
        "http://terminal/api/", {"Authorization": "Bearer secret"}
    )
    home = asyncio.run(configured_tools()._get_terminal_home(terminal))

    assert home == "/home/user"
    assert captured == {
        "url": "http://terminal/api/files/cwd",
        "headers": terminal.headers,
        "method": "GET",
        "payload": None,
        "timeout": 15,
    }


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        (
            [{"type": "stderr", "data": "/bin/sh: 1: npx: not found"}],
            "Node.js or npx is unavailable",
        ),
        (
            [{"type": "stderr", "data": "Error: Could not find Chrome (ver. 123)"}],
            "Chromium could not be installed or started",
        ),
        (
            [{"type": "stderr", "data": "npm error code EAI_AGAIN"}],
            "could not be downloaded from the network",
        ),
        (
            [{"type": "stderr", "data": "Parse error on line 2"}],
            "Mermaid rejected the diagram syntax",
        ),
    ],
)
def test_classifies_mermaid_renderer_diagnostics(output, expected):
    assert expected in Tools._terminal_output_diagnostic(output)


def test_terminal_command_uses_authenticated_execute_endpoint(monkeypatch):
    captured = {}

    def fake_request(self, url, headers, method, payload, timeout):
        captured.update(
            {
                "url": url,
                "headers": headers,
                "method": method,
                "payload": payload,
                "timeout": timeout,
            }
        )
        return {"status": "done", "exit_code": 0}

    monkeypatch.setattr(Tools, "_request_terminal_json", fake_request)
    terminal = TerminalContext(
        "http://terminal/api/",
        {"Authorization": "Bearer secret", "X-User-Id": "user-1"},
    )
    result = asyncio.run(
        configured_tools()._run_terminal_command(terminal, "mmdc --version", 90)
    )

    parsed = urllib.parse.urlparse(captured["url"])
    assert parsed.path == "/api/execute"
    assert urllib.parse.parse_qs(parsed.query) == {"wait": ["90"], "tail": ["30"]}
    assert captured["headers"] == terminal.headers
    assert captured["method"] == "POST"
    assert captured["payload"] == {"command": "mmdc --version"}
    assert captured["timeout"] == 100
    assert result == {"status": "done", "exit_code": 0}


def test_rejects_png_with_excessive_dimensions():
    oversized = (
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\rIHDR"
        + struct.pack(">II", Tools._MAX_DIAGRAM_WIDTH + 1, 100)
    )
    with pytest.raises(ValueError, match="too large for email"):
        Tools._validate_png(oversized, 1)


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
