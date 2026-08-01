import asyncio
import json
import sys
import types
from types import SimpleNamespace

import pytest

from openwebui_sendgrid_email_action import Action, TerminalContext


class Response:
    status = 202

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def configured_action() -> Action:
    action = Action()
    action.valves.SENDGRID_API_KEY = "SG.test"
    action.valves.SENDER_EMAIL = "sender@example.org"
    action.valves.RATE_LIMIT_MINUTES = 0
    return action


def install_openwebui_terminal_modules(monkeypatch, connections, access):
    class FakeConfig:
        @staticmethod
        async def get(key, default):
            assert key == "terminal_server.connections"
            return connections

    async def fake_has_connection_access(user, connection):
        return access(connection)

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


@pytest.fixture(autouse=True)
def reset_delivery_state():
    Action._last_delivery_by_user.clear()
    Action._sent_message_ids.clear()
    Action._inflight_users.clear()
    Action._inflight_message_ids.clear()
    yield
    Action._last_delivery_by_user.clear()
    Action._sent_message_ids.clear()
    Action._inflight_users.clear()
    Action._inflight_message_ids.clear()


def test_action_emails_exact_clicked_assistant_message_and_shows_success(monkeypatch):
    captured = {}
    events = []

    async def fake_send(self, **kwargs):
        captured.update(kwargs)
        return "Email notification sent successfully to us***@example.org."

    async def emit(event):
        events.append(event)

    monkeypatch.setattr(Action, "send_email_notification", fake_send)
    body = {
        "id": "assistant-1",
        "chat_id": "chat-1",
        "terminal_id": "terminal-1",
        "messages": [
            {"id": "user-1", "role": "user", "content": "Question"},
            {
                "id": "assistant-1",
                "role": "assistant",
                "content": "# Selected answer\n\nFirst body.",
            },
            {
                "id": "assistant-2",
                "role": "assistant",
                "content": "This later answer must not be sent.",
            },
        ],
    }

    result = asyncio.run(
        configured_action().action(
            body,
            __user__={"id": "user-1", "email": "user@example.org"},
            __metadata__={"session_id": "session-1"},
            __message_id__="injected-message-id",
            __request__=object(),
            __event_emitter__=emit,
        )
    )

    assert result is None
    assert captured["subject"] == "Selected answer"
    assert captured["message"] == "# Selected answer\n\nFirst body."
    assert captured["__message_id__"] == "assistant-1"
    assert captured["__metadata__"] == {
        "session_id": "session-1",
        "terminal_id": "terminal-1",
        "chat_id": "chat-1",
    }
    assert events[-1] == {
        "type": "notification",
        "data": {
            "type": "success",
            "content": "Email notification sent successfully to us***@example.org.",
        },
    }


def test_action_extracts_responses_api_output_when_content_is_empty(monkeypatch):
    captured = {}

    async def fake_send(self, **kwargs):
        captured.update(kwargs)
        return "Email notification sent successfully to us***@example.org."

    monkeypatch.setattr(Action, "send_email_notification", fake_send)
    body = {
        "id": "assistant-1",
        "messages": [
            {
                "id": "assistant-1",
                "role": "assistant",
                "content": "",
                "output": [
                    {"type": "reasoning", "summary": [{"text": "Do not send"}]},
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "## Structured answer\n\n| A | B |\n|---|---|\n| 1 | 2 |",
                            }
                        ],
                    },
                ],
            }
        ],
    }

    asyncio.run(
        configured_action().action(
            body,
            __user__={"email": "user@example.org"},
        )
    )

    assert captured["subject"] == "Structured answer"
    assert captured["message"].startswith("## Structured answer")
    assert "Do not send" not in captured["message"]


def test_action_extracts_text_parts_but_ignores_non_text_content(monkeypatch):
    captured = {}

    async def fake_send(self, **kwargs):
        captured.update(kwargs)
        return "Email notification sent successfully to us***@example.org."

    monkeypatch.setattr(Action, "send_email_notification", fake_send)
    body = {
        "id": "assistant-1",
        "messages": [
            {
                "id": "assistant-1",
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "First section."},
                    {"type": "image_url", "image_url": {"url": "secret"}},
                    {"type": "text", "text": "Second section."},
                ],
            }
        ],
    }

    asyncio.run(
        configured_action().action(
            body,
            __user__={"email": "user@example.org"},
        )
    )

    assert captured["message"] == "First section.\n\nSecond section."


def test_action_rejects_user_message_and_shows_error_notification(monkeypatch):
    events = []

    async def fail_if_called(self, **kwargs):
        raise AssertionError("Send should not be called")

    async def emit(event):
        events.append(event)

    monkeypatch.setattr(Action, "send_email_notification", fail_if_called)
    body = {
        "id": "user-1",
        "messages": [{"id": "user-1", "role": "user", "content": "Question"}],
    }

    result = asyncio.run(
        configured_action().action(
            body,
            __user__={"email": "user@example.org"},
            __event_emitter__=emit,
        )
    )

    assert result is None
    assert events[-1]["type"] == "notification"
    assert events[-1]["data"]["type"] == "error"
    assert "Only assistant messages" in events[-1]["data"]["content"]


def test_action_classifies_duplicate_as_information_notification(monkeypatch):
    events = []

    async def fake_send(self, **kwargs):
        return "A notification was already sent for this message; duplicate suppressed."

    async def emit(event):
        events.append(event)

    monkeypatch.setattr(Action, "send_email_notification", fake_send)
    body = {
        "id": "assistant-1",
        "messages": [
            {"id": "assistant-1", "role": "assistant", "content": "Answer"}
        ],
    }

    asyncio.run(
        configured_action().action(
            body,
            __user__={"email": "user@example.org"},
            __event_emitter__=emit,
        )
    )

    assert events[-1]["data"]["type"] == "info"


def test_subject_generation_skips_code_fences_and_supports_prefix():
    action = configured_action()
    action.valves.SUBJECT_PREFIX = "OpenWebUI: "
    message = """```mermaid
flowchart LR
A --> B
```

The architecture is ready. More detail follows.
"""

    assert action._derive_subject(message) == "OpenWebUI: The architecture is ready."


def test_subject_generation_is_bounded_to_sendgrid_limit():
    action = configured_action()
    subject = action._derive_subject("# " + ("A" * 400))

    assert len(subject) == 200
    assert subject.endswith("…")


def test_subject_generation_preserves_leading_year_in_heading():
    assert configured_action()._derive_subject("# 2026 Strategy") == "2026 Strategy"


def test_action_full_pipeline_renders_markdown_mermaid_and_sends_cid(monkeypatch):
    captured = {}
    events = []
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 + b"\x00\x00\x00d\x00\x00\x00d"

    async def fake_context(self, request, user, metadata, oauth_token):
        captured["metadata"] = metadata
        return TerminalContext("http://terminal", {})

    async def fake_render(self, source, diagram_number, terminal):
        captured["mermaid"] = source
        return png

    async def emit(event):
        events.append(event)

    def fake_urlopen(request, timeout):
        captured["payload"] = json.loads(request.data)
        return Response()

    monkeypatch.setattr(Action, "_get_open_terminal_context", fake_context)
    monkeypatch.setattr(Action, "_render_mermaid_png", fake_render)
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    body = {
        "id": "assistant-1",
        "metadata": {"terminal_id": "terminal-1"},
        "messages": [
            {
                "id": "assistant-1",
                "role": "assistant",
                "content": """# Architecture

| From | To |
|---|---|
| WebUI | Email |

```mermaid
flowchart LR
A --> B
```
""",
            }
        ],
    }

    asyncio.run(
        configured_action().action(
            body,
            __user__={"id": "user-1", "email": "user@example.org"},
            __metadata__={"chat_id": "chat-1"},
            __request__=object(),
            __event_emitter__=emit,
        )
    )

    payload = captured["payload"]
    assert payload["subject"] == "Architecture"
    assert payload["personalizations"] == [
        {"to": [{"email": "user@example.org"}]}
    ]
    assert "<table" in payload["content"][1]["value"]
    assert "cid:openwebui-mermaid-" in payload["content"][1]["value"]
    assert payload["attachments"][0]["disposition"] == "inline"
    assert captured["mermaid"] == "flowchart LR\nA --> B"
    assert captured["metadata"] == {
        "terminal_id": "terminal-1",
        "chat_id": "chat-1",
    }
    assert events[-1]["type"] == "notification"
    assert events[-1]["data"]["type"] == "success"


def test_action_automatically_uses_only_accessible_open_terminal(monkeypatch):
    connections = [
        {
            "id": "terminal-1",
            "name": "MyCloud",
            "enabled": True,
            "url": "http://open-terminal:8000",
            "key": "terminal-secret",
            "auth_type": "bearer",
        }
    ]
    install_openwebui_terminal_modules(monkeypatch, connections, lambda item: True)
    request = SimpleNamespace(state=SimpleNamespace(), headers={}, cookies={})

    context = asyncio.run(
        configured_action()._get_open_terminal_context(
            request,
            {"id": "user-1", "role": "user"},
            {"chat_id": "chat-1"},
            None,
        )
    )

    assert context == TerminalContext(
        "http://open-terminal:8000",
        {
            "X-User-Id": "user-1",
            "X-Session-Id": "chat-1",
            "Authorization": "Bearer terminal-secret",
        },
    )


def test_action_selects_configured_terminal_name_when_several_are_accessible(
    monkeypatch,
):
    connections = [
        {
            "id": "terminal-1",
            "name": "OtherCloud",
            "enabled": True,
            "url": "http://other-terminal:8000",
            "key": "other-secret",
            "auth_type": "bearer",
        },
        {
            "id": "terminal-2",
            "name": "MyCloud",
            "enabled": True,
            "url": "http://mycloud:8000",
            "key": "mycloud-secret",
            "auth_type": "bearer",
        },
    ]
    install_openwebui_terminal_modules(monkeypatch, connections, lambda item: True)
    action = configured_action()
    action.valves.OPEN_TERMINAL_CONNECTION = "mycloud"
    request = SimpleNamespace(state=SimpleNamespace(), headers={}, cookies={})

    context = asyncio.run(
        action._get_open_terminal_context(
            request,
            {"id": "user-1", "role": "user"},
            {"chat_id": "chat-1"},
            None,
        )
    )

    assert context.base_url == "http://mycloud:8000"
    assert context.headers["Authorization"] == "Bearer mycloud-secret"


def test_action_requires_terminal_valve_when_several_are_accessible(monkeypatch):
    connections = [
        {
            "id": "terminal-1",
            "name": "One",
            "enabled": True,
            "url": "http://one",
            "auth_type": "none",
        },
        {
            "id": "terminal-2",
            "name": "Two",
            "enabled": True,
            "url": "http://two",
            "auth_type": "none",
        },
    ]
    install_openwebui_terminal_modules(monkeypatch, connections, lambda item: True)
    request = SimpleNamespace(state=SimpleNamespace(), headers={}, cookies={})

    with pytest.raises(ValueError, match="OPEN_TERMINAL_CONNECTION"):
        asyncio.run(
            configured_action()._get_open_terminal_context(
                request,
                {"id": "user-1", "role": "user"},
                {"chat_id": "chat-1"},
                None,
            )
        )
