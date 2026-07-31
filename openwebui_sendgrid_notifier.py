"""
title: SendGrid Email Notification
author: Aikumi Partners
author_url: https://aikumipartners.com
description: Sends an email notification to the current OpenWebUI user's account email through SendGrid.
required_open_webui_version: 0.6.0
version: 1.1.0
license: MIT
"""

import asyncio
import json
import math
import re
import threading
import time
import urllib.error
import urllib.request
from typing import Awaitable, Callable, ClassVar, Optional

from pydantic import BaseModel, Field


EventEmitter = Callable[[dict], Awaitable[None]]


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
        __user__: Optional[dict] = None,
        __message_id__: Optional[str] = None,
        __event_emitter__: Optional[EventEmitter] = None,
    ) -> str:
        """
        Send an email notification to the current OpenWebUI user.

        The recipient is always taken from the authenticated OpenWebUI user profile.
        Never ask for, infer, or accept a destination email address.

        :param subject: Short, descriptive email subject.
        :param message: Plain-text notification body. Include all information the user needs.
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

            await self._emit(__event_emitter__, "Sending email notification…", False)
            try:
                await asyncio.to_thread(
                    self._send_via_sendgrid, recipient, subject, message
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

    def _send_via_sendgrid(self, recipient: str, subject: str, message: str) -> None:
        payload = {
            "personalizations": [{"to": [{"email": recipient}]}],
            "from": {
                "email": self.valves.SENDER_EMAIL.strip(),
                "name": self.valves.SENDER_NAME.strip() or "OpenWebUI",
            },
            "subject": subject,
            "content": [{"type": "text/plain", "value": message}],
        }
        request = urllib.request.Request(
            "https://api.sendgrid.com/v3/mail/send",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.valves.SENDGRID_API_KEY.strip()}",
                "Content-Type": "application/json",
                "User-Agent": "openwebui-sendgrid-notifier/1.1",
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
