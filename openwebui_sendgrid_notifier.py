"""
title: SendGrid Email Notification
author: Aikumi Partners
author_url: https://aikumipartners.com
description: Sends an email notification, optionally with an Open Terminal attachment, to the current OpenWebUI user's account email through SendGrid.
required_open_webui_version: 0.11.0
version: 1.2.0
license: MIT
"""

import asyncio
import base64
import json
import math
import mimetypes
import posixpath
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from types import SimpleNamespace
from typing import Awaitable, Callable, ClassVar, NamedTuple, Optional

from pydantic import BaseModel, Field


EventEmitter = Callable[[dict], Awaitable[None]]


class EmailAttachment(NamedTuple):
    content: bytes
    filename: str
    content_type: str


class DuplicateNotificationError(Exception):
    """Raised when a notification was already sent for this OpenWebUI message."""


class NotificationRateLimitError(Exception):
    """Raised when a user must wait before sending another notification."""

    def __init__(self, retry_after_seconds: int):
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"Retry after {retry_after_seconds} seconds.")


class Tools:
    # OpenWebUI can create more than one Tools instance in a process. Class-level
    # state makes the safeguards apply across those instances.
    _state_lock: ClassVar[threading.Lock] = threading.Lock()
    _last_delivery_by_user: ClassVar[dict[str, float]] = {}
    _sent_message_ids: ClassVar[dict[tuple[str, str], float]] = {}
    _inflight_users: ClassVar[set[str]] = set()
    _inflight_message_ids: ClassVar[set[tuple[str, str]]] = set()
    _IDEMPOTENCY_TTL_SECONDS: ClassVar[int] = 24 * 60 * 60
    _MAX_ATTACHMENT_BYTES: ClassVar[int] = 10 * 1024 * 1024

    class Valves(BaseModel):
        SENDGRID_API_KEY: str = Field(
            default="",
            description="SendGrid API key with Mail Send permission.",
            json_schema_extra={"input": {"type": "password"}},
        )
        SENDER_EMAIL: str = Field(
            default="",
            description="Verified SendGrid sender email address.",
        )
        SENDER_NAME: str = Field(
            default="OpenWebUI",
            description="Display name used for the sender.",
        )
        RATE_LIMIT_MINUTES: int = Field(
            default=10,
            ge=0,
            le=10_080,
            description=(
                "Minimum minutes between successful notifications per user. "
                "Set to 0 to disable rate limiting."
            ),
        )

    def __init__(self):
        self.valves = self.Valves()

    async def send_email_notification(
        self,
        subject: str,
        message: str,
        attachment_path: str = "",
        __user__: Optional[dict] = None,
        __message_id__: Optional[str] = None,
        __metadata__: Optional[dict] = None,
        __request__=None,
        __oauth_token__: Optional[dict] = None,
        __event_emitter__: Optional[EventEmitter] = None,
    ) -> str:
        """
        Send an email notification to the current OpenWebUI user.

        The recipient is always taken from the authenticated OpenWebUI user profile.
        Never ask for, infer, or accept a destination email address.

        :param subject: Short, descriptive email subject.
        :param message: Plain-text notification body. Include all information the user needs.
        :param attachment_path: Optional absolute path to one file in the Open Terminal selected for this chat. Leave empty for no attachment.
        :return: A delivery confirmation or a safe error message.
        """
        try:
            recipient = self._get_recipient(__user__)
            user_key = self._get_user_key(__user__, recipient)
            self._validate_configuration()
            subject = self._clean_subject(subject)
            message = self._clean_message(message)
            message_id = (__message_id__ or "").strip()

            reservation = self._reserve_delivery(user_key, message_id)

            try:
                attachment = None
                if attachment_path.strip():
                    await self._emit(
                        __event_emitter__, "Retrieving Open Terminal attachment…", False
                    )
                    attachment = await self._get_open_terminal_attachment(
                        attachment_path,
                        __request__,
                        __user__,
                        __metadata__,
                        __oauth_token__,
                    )

                await self._emit(
                    __event_emitter__, "Sending email notification…", False
                )
                await asyncio.to_thread(
                    self._send_via_sendgrid,
                    recipient,
                    subject,
                    message,
                    attachment,
                )
            except BaseException:
                self._release_delivery(reservation)
                raise
            self._complete_delivery(reservation)

            await self._emit(__event_emitter__, "Email notification sent.", True)
            return f"Email notification sent successfully to {self._mask_email(recipient)}."
        except DuplicateNotificationError:
            detail = "A notification was already sent for this message; duplicate suppressed."
            await self._emit(__event_emitter__, detail, True, "complete")
            return detail
        except NotificationRateLimitError as exc:
            wait = self._format_wait(exc.retry_after_seconds)
            detail = f"Notification rate limit reached. Try again in {wait}."
            await self._emit(__event_emitter__, detail, True, "error")
            return f"Email notification was not sent: {detail}"
        except ValueError as exc:
            await self._emit(__event_emitter__, str(exc), True, "error")
            return f"Email notification was not sent: {exc}"
        except urllib.error.HTTPError as exc:
            detail = self._sendgrid_error(exc)
            await self._emit(__event_emitter__, detail, True, "error")
            return f"Email notification was not sent: {detail}"
        except (urllib.error.URLError, TimeoutError):
            detail = "SendGrid could not be reached. Try again later."
            await self._emit(__event_emitter__, detail, True, "error")
            return f"Email notification was not sent: {detail}"

    async def _get_open_terminal_attachment(
        self,
        attachment_path: str,
        request,
        user: Optional[dict],
        metadata: Optional[dict],
        oauth_token: Optional[dict],
    ) -> EmailAttachment:
        path, filename = self._clean_attachment_path(attachment_path)
        terminal_id = str((metadata or {}).get("terminal_id") or "").strip()
        if not terminal_id:
            raise ValueError(
                "No system-level Open Terminal connection is selected for this chat."
            )
        if request is None:
            raise ValueError("OpenWebUI did not provide the request context.")

        try:
            from open_webui.models.config import Config
            from open_webui.utils.access_control import has_connection_access
            from open_webui.utils.terminals import get_terminal_server_url
        except ImportError as exc:
            raise ValueError(
                "This OpenWebUI version does not expose its Open Terminal configuration to tools."
            ) from exc

        connections = await Config.get("terminal_server.connections", []) or []
        connection = next(
            (item for item in connections if item.get("id") == terminal_id), None
        )
        if connection is None:
            raise ValueError("The selected Open Terminal connection was not found.")
        if not connection.get("enabled", True):
            raise ValueError("The selected Open Terminal connection is disabled.")

        user_id = str((user or {}).get("id") or "").strip()
        user_role = str((user or {}).get("role") or "user").strip()
        if not user_id:
            raise ValueError("The current OpenWebUI user has no valid user ID.")

        openwebui_user = SimpleNamespace(id=user_id, role=user_role)
        if not await has_connection_access(openwebui_user, connection):
            raise ValueError(
                "The current user cannot access the selected Open Terminal connection."
            )

        base_url = get_terminal_server_url(connection)
        if not base_url:
            raise ValueError("The selected Open Terminal URL is not configured.")

        headers = {"X-User-Id": user_id}
        chat_id = str((metadata or {}).get("chat_id") or "").strip()
        if chat_id:
            headers["X-Session-Id"] = chat_id

        auth_type = connection.get("auth_type", "bearer")
        if auth_type == "bearer":
            api_key = str(connection.get("key") or "").strip()
            if not api_key:
                raise ValueError("The selected Open Terminal API key is not configured.")
            headers["Authorization"] = f"Bearer {api_key}"
        elif auth_type == "session":
            credentials = str(
                getattr(getattr(request.state, "token", None), "credentials", "")
                or ""
            ).strip()
            if not credentials:
                raise ValueError("The Open Terminal session credentials are unavailable.")
            headers["Authorization"] = f"Bearer {credentials}"
            cookie = request.headers.get("cookie", "")
            if cookie:
                headers["Cookie"] = cookie
        elif auth_type == "system_oauth":
            access_token = str((oauth_token or {}).get("access_token") or "").strip()
            if not access_token:
                raise ValueError("The Open Terminal OAuth credentials are unavailable.")
            headers["Authorization"] = f"Bearer {access_token}"
        elif auth_type != "none":
            raise ValueError(
                f"Open Terminal authentication type '{auth_type}' is not supported."
            )

        url = f"{base_url.rstrip('/')}/files/view?{urllib.parse.urlencode({'path': path})}"
        try:
            content, content_type = await asyncio.to_thread(
                self._download_terminal_file, url, headers
            )
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise ValueError("The Open Terminal attachment file was not found.") from exc
            if exc.code in (401, 403):
                raise ValueError(
                    "Open Terminal rejected access to the attachment file."
                ) from exc
            raise ValueError(
                f"Open Terminal could not return the attachment (HTTP {exc.code})."
            ) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise ValueError("Open Terminal could not be reached.") from exc

        if not content_type:
            content_type = (
                mimetypes.guess_type(filename)[0] or "application/octet-stream"
            )
        return EmailAttachment(content, filename, content_type)

    def _download_terminal_file(
        self, url: str, headers: dict[str, str]
    ) -> tuple[bytes, str]:
        terminal_request = urllib.request.Request(
            url,
            headers={**headers, "User-Agent": "openwebui-sendgrid-notifier/1.2"},
            method="GET",
        )
        with urllib.request.urlopen(terminal_request, timeout=30) as response:
            content_length = response.headers.get("Content-Length")
            if content_length:
                try:
                    reported_size = int(content_length)
                except (TypeError, ValueError):
                    reported_size = None
                if (
                    reported_size is not None
                    and reported_size > self._MAX_ATTACHMENT_BYTES
                ):
                    raise ValueError("The attachment exceeds the 10 MB size limit.")

            content = response.read(self._MAX_ATTACHMENT_BYTES + 1)
            if len(content) > self._MAX_ATTACHMENT_BYTES:
                raise ValueError("The attachment exceeds the 10 MB size limit.")
            content_type = response.headers.get("Content-Type", "").split(";", 1)[
                0
            ].strip()
            return content, content_type

    @staticmethod
    def _clean_attachment_path(path: str) -> tuple[str, str]:
        value = path.strip()
        if not value:
            raise ValueError("The attachment path cannot be empty.")
        if len(value) > 4_096 or "\x00" in value:
            raise ValueError("The attachment path is invalid.")
        if not value.startswith("/"):
            raise ValueError("The Open Terminal attachment path must be absolute.")

        filename = posixpath.basename(value.rstrip("/"))
        filename = "".join(
            character for character in filename if ord(character) >= 32
        ).strip()
        if not filename:
            raise ValueError("The attachment path must identify a file.")
        if len(filename) > 255:
            filename = filename[:255]
        return value, filename

    @staticmethod
    def _get_recipient(user: Optional[dict]) -> str:
        email = (user or {}).get("email", "").strip()
        if not email or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
            raise ValueError("The current OpenWebUI account has no valid email address.")
        return email

    @staticmethod
    def _get_user_key(user: Optional[dict], email: str) -> str:
        user_id = str((user or {}).get("id", "")).strip()
        return f"id:{user_id}" if user_id else f"email:{email.casefold()}"

    def _validate_configuration(self) -> None:
        if not self.valves.SENDGRID_API_KEY.strip():
            raise ValueError("The SendGrid API key valve is not configured.")
        if not re.fullmatch(
            r"[^\s@]+@[^\s@]+\.[^\s@]+", self.valves.SENDER_EMAIL.strip()
        ):
            raise ValueError("The SendGrid sender email valve is missing or invalid.")
        if not isinstance(self.valves.RATE_LIMIT_MINUTES, int) or not (
            0 <= self.valves.RATE_LIMIT_MINUTES <= 10_080
        ):
            raise ValueError(
                "The notification rate-limit valve must be between 0 and 10,080 minutes."
            )

    def _reserve_delivery(
        self, user_key: str, message_id: str
    ) -> tuple[str, Optional[tuple[str, str]]]:
        now = time.monotonic()
        message_key = (user_key, message_id) if message_id else None
        cooldown_seconds = self.valves.RATE_LIMIT_MINUTES * 60

        with self._state_lock:
            self._prune_state(now, cooldown_seconds)

            if message_key and (
                message_key in self._sent_message_ids
                or message_key in self._inflight_message_ids
            ):
                raise DuplicateNotificationError

            if user_key in self._inflight_users:
                raise NotificationRateLimitError(max(1, cooldown_seconds))

            last_delivery = self._last_delivery_by_user.get(user_key)
            if cooldown_seconds and last_delivery is not None:
                remaining = cooldown_seconds - (now - last_delivery)
                if remaining > 0:
                    raise NotificationRateLimitError(math.ceil(remaining))

            self._inflight_users.add(user_key)
            if message_key:
                self._inflight_message_ids.add(message_key)
            return user_key, message_key

    @classmethod
    def _release_delivery(
        cls, reservation: tuple[str, Optional[tuple[str, str]]]
    ) -> None:
        user_key, message_key = reservation
        with cls._state_lock:
            cls._inflight_users.discard(user_key)
            if message_key:
                cls._inflight_message_ids.discard(message_key)

    @classmethod
    def _complete_delivery(
        cls, reservation: tuple[str, Optional[tuple[str, str]]]
    ) -> None:
        user_key, message_key = reservation
        now = time.monotonic()
        with cls._state_lock:
            cls._inflight_users.discard(user_key)
            cls._last_delivery_by_user[user_key] = now
            if message_key:
                cls._inflight_message_ids.discard(message_key)
                cls._sent_message_ids[message_key] = now

    @classmethod
    def _prune_state(cls, now: float, cooldown_seconds: int) -> None:
        cls._sent_message_ids = {
            key: sent_at
            for key, sent_at in cls._sent_message_ids.items()
            if now - sent_at < cls._IDEMPOTENCY_TTL_SECONDS
        }
        retention = max(cls._IDEMPOTENCY_TTL_SECONDS, cooldown_seconds)
        cls._last_delivery_by_user = {
            key: sent_at
            for key, sent_at in cls._last_delivery_by_user.items()
            if now - sent_at < retention
        }

    @staticmethod
    def _format_wait(seconds: int) -> str:
        if seconds < 60:
            return f"{seconds} second{'s' if seconds != 1 else ''}"
        minutes = math.ceil(seconds / 60)
        return f"{minutes} minute{'s' if minutes != 1 else ''}"

    @staticmethod
    def _clean_subject(subject: str) -> str:
        value = " ".join(subject.split()).strip()
        if not value:
            raise ValueError("The email subject cannot be empty.")
        if len(value) > 200:
            raise ValueError("The email subject must be 200 characters or fewer.")
        return value

    @staticmethod
    def _clean_message(message: str) -> str:
        value = message.strip()
        if not value:
            raise ValueError("The email message cannot be empty.")
        if len(value) > 50_000:
            raise ValueError("The email message must be 50,000 characters or fewer.")
        return value

    def _send_via_sendgrid(
        self,
        recipient: str,
        subject: str,
        message: str,
        attachment: Optional[EmailAttachment] = None,
    ) -> None:
        payload = {
            "personalizations": [{"to": [{"email": recipient}]}],
            "from": {
                "email": self.valves.SENDER_EMAIL.strip(),
                "name": self.valves.SENDER_NAME.strip() or "OpenWebUI",
            },
            "subject": subject,
            "content": [{"type": "text/plain", "value": message}],
        }
        if attachment:
            payload["attachments"] = [
                {
                    "content": base64.b64encode(attachment.content).decode("ascii"),
                    "type": attachment.content_type,
                    "filename": attachment.filename,
                    "disposition": "attachment",
                }
            ]
        request = urllib.request.Request(
            "https://api.sendgrid.com/v3/mail/send",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.valves.SENDGRID_API_KEY.strip()}",
                "Content-Type": "application/json",
                "User-Agent": "openwebui-sendgrid-notifier/1.2",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            if response.status != 202:
                raise RuntimeError(f"Unexpected SendGrid response: HTTP {response.status}")

    @staticmethod
    def _sendgrid_error(error: urllib.error.HTTPError) -> str:
        if error.code in (401, 403):
            return "SendGrid rejected the API credentials or sender permissions."
        if error.code == 429:
            return "SendGrid rate-limited the request. Try again later."
        return f"SendGrid rejected the email request (HTTP {error.code})."

    @staticmethod
    async def _emit(
        emitter: Optional[EventEmitter],
        description: str,
        done: bool,
        status: str = "in_progress",
    ) -> None:
        if emitter:
            await emitter(
                {
                    "type": "status",
                    "data": {
                        "description": description,
                        "done": done,
                        "status": status,
                    },
                }
            )

    @staticmethod
    def _mask_email(email: str) -> str:
        local, domain = email.split("@", 1)
        visible = local[:2] if len(local) > 2 else local[:1]
        return f"{visible}***@{domain}"
