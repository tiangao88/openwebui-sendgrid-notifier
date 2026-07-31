"""
title: SendGrid Email Notification
author: Aikumi Partners
author_url: https://aikumipartners.com
description: Sends an email notification to the current OpenWebUI user's account email through SendGrid.
required_open_webui_version: 0.6.0
version: 1.0.0
license: MIT
"""

import asyncio
import json
import re
import urllib.error
import urllib.request
from typing import Awaitable, Callable, Optional

from pydantic import BaseModel, Field


EventEmitter = Callable[[dict], Awaitable[None]]


class Tools:
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

    def __init__(self):
        self.valves = self.Valves()

    async def send_email_notification(
        self,
        subject: str,
        message: str,
        __user__: Optional[dict] = None,
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
            self._validate_configuration()
            subject = self._clean_subject(subject)
            message = self._clean_message(message)

            await self._emit(__event_emitter__, "Sending email notification…", False)
            await asyncio.to_thread(self._send_via_sendgrid, recipient, subject, message)
            await self._emit(__event_emitter__, "Email notification sent.", True)
            return f"Email notification sent successfully to {self._mask_email(recipient)}."
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

    def _validate_configuration(self) -> None:
        if not self.valves.SENDGRID_API_KEY.strip():
            raise ValueError("The SendGrid API key valve is not configured.")
        if not re.fullmatch(
            r"[^\s@]+@[^\s@]+\.[^\s@]+", self.valves.SENDER_EMAIL.strip()
        ):
            raise ValueError("The SendGrid sender email valve is missing or invalid.")

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
                "User-Agent": "openwebui-sendgrid-notifier/1.0",
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
