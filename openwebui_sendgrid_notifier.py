"""
title: SendGrid Email Notification
author: Aikumi Partners
author_url: https://aikumipartners.com
description: Sends a Markdown email notification, optionally with Open Terminal files and rendered Mermaid diagrams, to the current OpenWebUI user's account email through SendGrid.
required_open_webui_version: 0.11.0
version: 1.3.2
license: MIT
"""

import asyncio
import base64
import html
import json
import math
import mimetypes
import posixpath
import re
import shlex
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from html.parser import HTMLParser
from types import SimpleNamespace
from typing import Awaitable, Callable, ClassVar, NamedTuple, Optional

import markdown
from pydantic import BaseModel, Field

EventEmitter = Callable[[dict], Awaitable[None]]


class EmailAttachment(NamedTuple):
    content: bytes
    filename: str
    content_type: str
    disposition: str = "attachment"
    content_id: Optional[str] = None


class EmailBody(NamedTuple):
    plain: str
    html: str
    inline_attachments: list[EmailAttachment]
    mermaid_total: int
    mermaid_rendered: int
    mermaid_errors: list[str]


class TerminalContext(NamedTuple):
    base_url: str
    headers: dict[str, str]
    home: str = ""


class DuplicateNotificationError(Exception):
    """Raised when a notification was already sent for this OpenWebUI message."""


class NotificationRateLimitError(Exception):
    """Raised when a user must wait before sending another notification."""

    def __init__(self, retry_after_seconds: int):
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"Retry after {retry_after_seconds} seconds.")


class _EmailHTMLSanitizer(HTMLParser):
    """Allow only email-safe Markdown output and apply fixed inline styles."""

    _ALLOWED_TAGS = {
        "a",
        "blockquote",
        "br",
        "code",
        "del",
        "em",
        "h1",
        "h2",
        "h3",
        "h4",
        "hr",
        "li",
        "ol",
        "p",
        "pre",
        "strong",
        "table",
        "tbody",
        "td",
        "th",
        "thead",
        "tr",
        "ul",
    }
    _DANGEROUS_TAGS = {"embed", "form", "iframe", "object", "script", "style", "svg"}
    _VOID_TAGS = {"br", "hr"}
    _STYLES = {
        "a": "color:#2563eb;text-decoration:underline;",
        "blockquote": "border-left:4px solid #cbd5e1;color:#475569;margin:16px 0;padding:4px 16px;",
        "br": "",
        "code": "background:#f1f5f9;border-radius:4px;color:#0f172a;font-family:SFMono-Regular,Consolas,Liberation Mono,monospace;font-size:0.92em;padding:2px 5px;",
        "del": "color:#64748b;",
        "em": "",
        "h1": "color:#0f172a;font-size:26px;line-height:1.25;margin:0 0 18px;",
        "h2": "color:#0f172a;font-size:22px;line-height:1.3;margin:26px 0 12px;",
        "h3": "color:#1e293b;font-size:18px;line-height:1.35;margin:22px 0 10px;",
        "h4": "color:#1e293b;font-size:16px;line-height:1.4;margin:18px 0 8px;",
        "hr": "border:0;border-top:1px solid #e2e8f0;margin:24px 0;",
        "li": "margin:5px 0;",
        "ol": "margin:12px 0;padding-left:26px;",
        "p": "margin:0 0 14px;",
        "pre": "background:#f8fafc;border:1px solid #e2e8f0;border-radius:6px;box-sizing:border-box;line-height:1.45;margin:14px 0;max-width:100%;overflow-wrap:anywhere;padding:14px;white-space:pre-wrap;",
        "strong": "",
        "table": "border-collapse:collapse;margin:18px 0;max-width:100%;width:100%;",
        "tbody": "",
        "td": "border:1px solid #cbd5e1;padding:9px 10px;text-align:left;vertical-align:top;",
        "th": "background:#f1f5f9;border:1px solid #cbd5e1;color:#0f172a;font-weight:600;padding:9px 10px;text-align:left;vertical-align:top;",
        "thead": "",
        "tr": "",
        "ul": "margin:12px 0;padding-left:26px;",
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._suppressed_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        tag = tag.lower()
        if tag in self._DANGEROUS_TAGS:
            self._suppressed_depth += 1
            return
        if self._suppressed_depth or tag not in self._ALLOWED_TAGS:
            return

        safe_attrs = [f'style="{html.escape(self._STYLES.get(tag, ""), quote=True)}"']
        if tag == "a":
            attr_map = {name.lower(): value or "" for name, value in attrs}
            href = self._safe_href(attr_map.get("href", ""))
            if href:
                safe_attrs.extend(
                    [
                        f'href="{html.escape(href, quote=True)}"',
                        'rel="noopener noreferrer"',
                        'target="_blank"',
                    ]
                )
            title = attr_map.get("title", "").strip()
            if title:
                safe_attrs.append(f'title="{html.escape(title[:500], quote=True)}"')

        suffix = " /" if tag in self._VOID_TAGS else ""
        self._parts.append(f"<{tag} {' '.join(safe_attrs)}{suffix}>")

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, Optional[str]]]
    ) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self._DANGEROUS_TAGS and self._suppressed_depth:
            self._suppressed_depth -= 1
            return
        if (
            self._suppressed_depth
            or tag not in self._ALLOWED_TAGS
            or tag in self._VOID_TAGS
        ):
            return
        self._parts.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if not self._suppressed_depth:
            self._parts.append(html.escape(data, quote=False))

    def get_html(self) -> str:
        return "".join(self._parts)

    @staticmethod
    def _safe_href(value: str) -> str:
        href = "".join(character for character in value.strip() if ord(character) >= 32)
        if not href or len(href) > 2_048:
            return ""
        try:
            scheme = urllib.parse.urlsplit(href).scheme.casefold()
        except ValueError:
            return ""
        return href if scheme in {"http", "https", "mailto"} else ""


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
    _MAX_MERMAID_DIAGRAMS: ClassVar[int] = 3
    _MAX_DIAGRAM_BYTES: ClassVar[int] = 1_500_000
    _MAX_TOTAL_DIAGRAM_BYTES: ClassVar[int] = 3_000_000
    _MAX_DIAGRAM_WIDTH: ClassVar[int] = 4_000
    _MAX_DIAGRAM_HEIGHT: ClassVar[int] = 8_000
    _MERMAID_RENDER_TIMEOUT_SECONDS: ClassVar[int] = 180
    _MERMAID_CLI_VERSION: ClassVar[str] = "11.16.0"
    _MERMAID_FENCE_RE: ClassVar[re.Pattern[str]] = re.compile(
        r"^```[ \t]*mermaid[ \t]*\r?\n(.*?)^```[ \t]*$",
        re.IGNORECASE | re.MULTILINE | re.DOTALL,
    )
    _MARKDOWN_IMAGE_RE: ClassVar[re.Pattern[str]] = re.compile(
        r"!\[([^\]]*)\]\([^\n)]*\)"
    )

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
        :param message: Email body in Markdown. You may use headings, emphasis, lists, links, code blocks, concise tables, and Mermaid code blocks. Do not include raw HTML or remote images.
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
                email_body = await self._build_email_body(
                    message,
                    __request__,
                    __user__,
                    __metadata__,
                    __oauth_token__,
                    __event_emitter__,
                )

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
                    email_body,
                    attachment,
                )
            except BaseException:
                self._release_delivery(reservation)
                raise
            self._complete_delivery(reservation)

            await self._emit(__event_emitter__, "Email notification sent.", True)
            result = f"Email notification sent successfully to {self._mask_email(recipient)}."
            if email_body.mermaid_total:
                failures = email_body.mermaid_total - email_body.mermaid_rendered
                result += (
                    f" Embedded {email_body.mermaid_rendered} of "
                    f"{email_body.mermaid_total} Mermaid diagram(s)."
                )
                if failures:
                    result += (
                        f" {failures} diagram(s) were included as source fallback."
                    )
                    if email_body.mermaid_errors:
                        result += f" Renderer detail: {email_body.mermaid_errors[0]}"
            return result
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

    async def _build_email_body(
        self,
        message: str,
        request,
        user: Optional[dict],
        metadata: Optional[dict],
        oauth_token: Optional[dict],
        emitter: Optional[EventEmitter],
    ) -> EmailBody:
        diagrams: list[str] = []

        def replace_mermaid(match: re.Match[str]) -> str:
            index = len(diagrams)
            diagrams.append(match.group(1).strip())
            return f"\n\nOPENWEBUI_MERMAID_PLACEHOLDER_{index}\n\n"

        markdown_source = self._MERMAID_FENCE_RE.sub(replace_mermaid, message)
        markdown_source = self._MARKDOWN_IMAGE_RE.sub(
            lambda match: f"[Remote image omitted: {match.group(1).strip() or 'image'}]",
            markdown_source,
        )
        rendered = markdown.markdown(
            html.escape(markdown_source, quote=False),
            extensions=["tables", "fenced_code", "sane_lists", "pymdownx.tilde"],
            extension_configs={"pymdownx.tilde": {"subscript": False}},
            output_format="html",
        )
        sanitizer = _EmailHTMLSanitizer()
        sanitizer.feed(rendered)
        sanitizer.close()
        safe_html = sanitizer.get_html()

        inline_attachments: list[EmailAttachment] = []
        rendered_count = 0
        terminal_context: Optional[TerminalContext] = None
        terminal_unavailable = False
        mermaid_errors: list[str] = []

        if diagrams:
            await self._emit(emitter, "Rendering Mermaid diagrams…", False)
            try:
                terminal_context = await self._get_open_terminal_context(
                    request, user, metadata, oauth_token
                )
            except ValueError as exc:
                terminal_unavailable = True
                mermaid_errors.append(self._safe_mermaid_error(exc))

        total_inline_bytes = 0
        for index, source in enumerate(diagrams):
            diagram_number = index + 1
            replacement: str
            if index >= self._MAX_MERMAID_DIAGRAMS:
                replacement = self._mermaid_fallback_html(
                    source,
                    "Only the first three Mermaid diagrams can be rendered per email.",
                )
            elif terminal_unavailable or terminal_context is None:
                replacement = self._mermaid_fallback_html(
                    source,
                    "No usable system-level Open Terminal was selected for rendering.",
                )
            else:
                try:
                    png = await self._render_mermaid_png(
                        source, diagram_number, terminal_context
                    )
                    if total_inline_bytes + len(png) > self._MAX_TOTAL_DIAGRAM_BYTES:
                        raise ValueError(
                            "The combined Mermaid image limit was exceeded."
                        )
                    content_id = f"openwebui-mermaid-{uuid.uuid4().hex}"
                    inline_attachments.append(
                        EmailAttachment(
                            png,
                            f"mermaid-diagram-{diagram_number}.png",
                            "image/png",
                            "inline",
                            content_id,
                        )
                    )
                    total_inline_bytes += len(png)
                    rendered_count += 1
                    replacement = self._mermaid_image_html(content_id, diagram_number)
                except (
                    ValueError,
                    urllib.error.HTTPError,
                    urllib.error.URLError,
                    TimeoutError,
                ) as exc:
                    reason = self._safe_mermaid_error(exc)
                    mermaid_errors.append(reason)
                    replacement = self._mermaid_fallback_html(source, reason)

            placeholder = f"OPENWEBUI_MERMAID_PLACEHOLDER_{index}"
            safe_html = re.sub(
                rf"<p\b[^>]*>\s*{re.escape(placeholder)}\s*</p>",
                lambda _match, value=replacement: value,
                safe_html,
                count=1,
                flags=re.IGNORECASE,
            )

        document = (
            '<!doctype html><html><body style="background:#ffffff;color:#1e293b;'
            "font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial,sans-serif;"
            'font-size:15px;line-height:1.6;margin:0;padding:0;">'
            '<div style="box-sizing:border-box;margin:0 auto;max-width:720px;padding:28px 22px;">'
            f"{safe_html}</div></body></html>"
        )
        return EmailBody(
            message,
            document,
            inline_attachments,
            len(diagrams),
            rendered_count,
            mermaid_errors,
        )

    async def _render_mermaid_png(
        self, source: str, diagram_number: int, terminal: TerminalContext
    ) -> bytes:
        token = uuid.uuid4().hex
        try:
            home = terminal.home or await self._get_terminal_home(terminal)
        except urllib.error.HTTPError as exc:
            raise ValueError(
                "Open Terminal home discovery failed"
                f" (HTTP {exc.code}).{self._terminal_http_error_detail(exc)}"
            ) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise ValueError("Open Terminal home discovery could not be reached.") from exc
        except ValueError as exc:
            raise ValueError(f"Open Terminal home discovery failed. {exc}") from exc

        # Open Terminal's file API may reject hidden or mode-700 directories even
        # when /execute can write to them. Keep uniquely named temporary files in
        # the reported home directory, where /files/view works for normal files.
        prefix = posixpath.join(home.rstrip("/"), f"openwebui-mermaid-{token}")
        input_path = f"{prefix}.mmd"
        output_path = f"{prefix}.png"
        config_path = f"{prefix}-config.json"
        puppeteer_path = f"{prefix}-puppeteer.json"
        encoded_source = base64.b64encode(source.encode("utf-8")).decode("ascii")
        mermaid_config = base64.b64encode(
            json.dumps(
                {
                    "securityLevel": "strict",
                    "htmlLabels": False,
                    "flowchart": {"htmlLabels": False},
                    "maxTextSize": 50_000,
                },
                separators=(",", ":"),
            ).encode("utf-8")
        ).decode("ascii")
        puppeteer_config = base64.b64encode(
            json.dumps(
                {"args": ["--no-sandbox", "--disable-setuid-sandbox"]},
                separators=(",", ":"),
            ).encode("utf-8")
        ).decode("ascii")

        quoted = {
            name: shlex.quote(value)
            for name, value in {
                "home": home,
                "input": input_path,
                "output": output_path,
                "config": config_path,
                "puppeteer": puppeteer_path,
            }.items()
        }
        render_args = (
            f"-i {quoted['input']} -o {quoted['output']} -c {quoted['config']} "
            f"-p {quoted['puppeteer']} -t neutral -b white -w 900 -s 2"
        )
        command = (
            "set -eu\n"
            f"find {quoted['home']} -maxdepth 1 -type f "
            "-name 'openwebui-mermaid-*.png' -mmin +15 -delete 2>/dev/null || true\n"
            f"printf %s {shlex.quote(encoded_source)} | base64 -d > {quoted['input']}\n"
            f"printf %s {shlex.quote(mermaid_config)} | base64 -d > {quoted['config']}\n"
            f"printf %s {shlex.quote(puppeteer_config)} | base64 -d > {quoted['puppeteer']}\n"
            "if command -v mmdc >/dev/null 2>&1; then\n"
            f"  timeout {self._MERMAID_RENDER_TIMEOUT_SECONDS}s mmdc {render_args}\n"
            "else\n"
            f"  timeout {self._MERMAID_RENDER_TIMEOUT_SECONDS}s npx --yes "
            f"@mermaid-js/mermaid-cli@{self._MERMAID_CLI_VERSION} {render_args}\n"
            "fi\n"
            f"chmod 644 {quoted['output']}"
        )
        support_cleanup_command = "rm -f " + " ".join(
            [quoted["input"], quoted["config"], quoted["puppeteer"]]
        )
        full_cleanup_command = f"{support_cleanup_command} {quoted['output']}"
        render_completed = False
        download_completed = False

        try:
            try:
                await self._run_terminal_command(
                    terminal, command, self._MERMAID_RENDER_TIMEOUT_SECONDS + 15
                )
                render_completed = True
            except urllib.error.HTTPError as exc:
                raise ValueError(
                    "Open Terminal command execution failed"
                    f" (HTTP {exc.code}).{self._terminal_http_error_detail(exc)}"
                ) from exc
            except urllib.error.URLError as exc:
                raise ValueError(
                    "Open Terminal command execution could not be reached."
                ) from exc
            except ValueError as exc:
                raise ValueError(
                    f"Open Terminal command execution failed. {exc}"
                ) from exc

            try:
                content, _content_type = await asyncio.to_thread(
                    self._download_terminal_path,
                    terminal,
                    output_path,
                    self._MAX_DIAGRAM_BYTES,
                    "Mermaid diagram",
                )
                download_completed = True
            except urllib.error.HTTPError as exc:
                raise ValueError(
                    "Open Terminal PNG retrieval failed"
                    f" (HTTP {exc.code}).{self._terminal_http_error_detail(exc)} "
                    f"The temporary PNG was retained at {output_path}."
                ) from exc
            except urllib.error.URLError as exc:
                raise ValueError(
                    "Open Terminal PNG retrieval could not be reached. "
                    f"The temporary PNG was retained at {output_path}."
                ) from exc

            self._validate_png(content, diagram_number)
            return content
        finally:
            try:
                cleanup_command = (
                    full_cleanup_command
                    if download_completed or not render_completed
                    else support_cleanup_command
                )
                await self._run_terminal_command(terminal, cleanup_command, 10)
            except (
                ValueError,
                urllib.error.HTTPError,
                urllib.error.URLError,
                TimeoutError,
            ):
                pass

    @staticmethod
    def _terminal_http_error_detail(exc: urllib.error.HTTPError) -> str:
        try:
            raw = exc.read(2_001)
        except Exception:
            return ""
        if not raw:
            return ""
        text = raw[:2_000].decode("utf-8", errors="replace")
        text = " ".join(text.split())
        if not text:
            return ""
        return f" Open Terminal response: {text[:500]}"

    async def _get_terminal_home(self, terminal: TerminalContext) -> str:
        url = f"{terminal.base_url.rstrip('/')}/files/cwd"
        result = await asyncio.to_thread(
            self._request_terminal_json,
            url,
            terminal.headers,
            "GET",
            None,
            15,
        )
        home = str(result.get("home") or "").strip()
        if not home or not home.startswith("/"):
            raise ValueError("Open Terminal did not return a valid home directory.")
        return posixpath.normpath(home)

    async def _run_terminal_command(
        self, terminal: TerminalContext, command: str, wait_seconds: int
    ) -> dict:
        query_wait = min(max(wait_seconds, 1), 300)
        url = f"{terminal.base_url.rstrip('/')}/execute?" + urllib.parse.urlencode(
            {"wait": query_wait, "tail": 30}
        )
        result = await asyncio.to_thread(
            self._request_terminal_json,
            url,
            terminal.headers,
            "POST",
            {"command": command},
            query_wait + 10,
        )
        if result.get("status") == "running":
            raise TimeoutError("Mermaid rendering timed out in Open Terminal.")
        if result.get("exit_code") != 0:
            diagnostic = self._terminal_output_diagnostic(result.get("output"))
            detail = "Open Terminal could not render the Mermaid diagram."
            if diagnostic:
                detail += f" {diagnostic}"
            raise ValueError(detail)
        return result

    @classmethod
    def _terminal_output_diagnostic(cls, output) -> str:
        parts: list[str] = []

        def collect(value) -> None:
            if isinstance(value, str):
                parts.append(value)
            elif isinstance(value, list):
                for item in value:
                    collect(item)
            elif isinstance(value, dict):
                for key in ("data", "content", "text", "message", "stderr", "stdout"):
                    if key in value:
                        collect(value[key])
                        break

        collect(output)
        text = " ".join(parts)
        text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
        text = " ".join(text.split())
        if not text:
            return ""

        lowered = text.casefold()
        if any(
            marker in lowered
            for marker in (
                "npx: not found",
                "npx: command not found",
                "command not found: npx",
                "node: not found",
                "node: command not found",
                "command not found: node",
            )
        ):
            return "Node.js or npx is unavailable in the selected Open Terminal image."
        if any(
            marker in lowered
            for marker in (
                "could not find chrome",
                "could not find chromium",
                "failed to launch the browser",
                "browser was not found",
                "no usable sandbox",
            )
        ):
            return "Chromium could not be installed or started in Open Terminal."
        if any(
            marker in lowered
            for marker in (
                "eai_again",
                "enotfound",
                "econnreset",
                "network request failed",
                "npm error network",
            )
        ):
            return "Mermaid CLI or Chromium could not be downloaded from the network."
        if "parse error" in lowered or "syntax error" in lowered:
            return f"Mermaid rejected the diagram syntax: {text[-400:]}"
        return f"Open Terminal output: {text[-500:]}"

    @staticmethod
    def _safe_mermaid_error(exc: BaseException) -> str:
        if isinstance(exc, TimeoutError):
            return "Mermaid rendering timed out in Open Terminal."
        if isinstance(exc, urllib.error.HTTPError):
            return (
                f"Open Terminal returned HTTP {exc.code} while rendering the diagram."
            )
        if isinstance(exc, urllib.error.URLError):
            return "Open Terminal could not be reached while rendering the diagram."
        detail = " ".join(str(exc).split())
        return detail[:600] or "The Mermaid diagram could not be rendered."

    def _request_terminal_json(
        self,
        url: str,
        headers: dict[str, str],
        method: str,
        payload: Optional[dict],
        timeout: int,
    ) -> dict:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        terminal_request = urllib.request.Request(
            url,
            data=data,
            headers={
                **headers,
                "Content-Type": "application/json",
                "User-Agent": "openwebui-sendgrid-notifier/1.3",
            },
            method=method,
        )
        with urllib.request.urlopen(terminal_request, timeout=timeout) as response:
            raw = response.read(1_000_001)
            if len(raw) > 1_000_000:
                raise ValueError("Open Terminal returned an oversized response.")
        try:
            result = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Open Terminal returned an invalid response.") from exc
        if not isinstance(result, dict):
            raise ValueError("Open Terminal returned an invalid response.")
        return result

    async def _get_open_terminal_attachment(
        self,
        attachment_path: str,
        request,
        user: Optional[dict],
        metadata: Optional[dict],
        oauth_token: Optional[dict],
    ) -> EmailAttachment:
        path, filename = self._clean_attachment_path(attachment_path)
        terminal = await self._get_open_terminal_context(
            request, user, metadata, oauth_token
        )
        try:
            content, content_type = await asyncio.to_thread(
                self._download_terminal_path,
                terminal,
                path,
                self._MAX_ATTACHMENT_BYTES,
                "attachment",
            )
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise ValueError(
                    "The Open Terminal attachment file was not found."
                ) from exc
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

    async def _get_open_terminal_context(
        self,
        request,
        user: Optional[dict],
        metadata: Optional[dict],
        oauth_token: Optional[dict],
    ) -> TerminalContext:
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
                raise ValueError(
                    "The selected Open Terminal API key is not configured."
                )
            headers["Authorization"] = f"Bearer {api_key}"
        elif auth_type == "session":
            credentials = str(
                getattr(getattr(request.state, "token", None), "credentials", "") or ""
            ).strip()
            if not credentials:
                raise ValueError(
                    "The Open Terminal session credentials are unavailable."
                )
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

        return TerminalContext(base_url, headers)

    def _download_terminal_path(
        self,
        terminal: TerminalContext,
        path: str,
        max_bytes: int,
        label: str,
    ) -> tuple[bytes, str]:
        url = f"{terminal.base_url.rstrip('/')}/files/view?" + urllib.parse.urlencode(
            {"path": path}
        )
        terminal_request = urllib.request.Request(
            url,
            headers={
                **terminal.headers,
                "User-Agent": "openwebui-sendgrid-notifier/1.3",
            },
            method="GET",
        )
        with urllib.request.urlopen(terminal_request, timeout=30) as response:
            content_length = response.headers.get("Content-Length")
            if content_length:
                try:
                    reported_size = int(content_length)
                except (TypeError, ValueError):
                    reported_size = None
                if reported_size is not None and reported_size > max_bytes:
                    raise ValueError(
                        f"The {label} exceeds the {self._format_bytes(max_bytes)} size limit."
                    )

            content = response.read(max_bytes + 1)
            if len(content) > max_bytes:
                raise ValueError(
                    f"The {label} exceeds the {self._format_bytes(max_bytes)} size limit."
                )
            content_type = (
                response.headers.get("Content-Type", "").split(";", 1)[0].strip()
            )
            return content, content_type

    @classmethod
    def _validate_png(cls, content: bytes, diagram_number: int) -> None:
        if len(content) < 24 or not content.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError(
                f"Mermaid diagram {diagram_number} was not rendered as PNG."
            )
        width, height = struct.unpack(">II", content[16:24])
        if width > cls._MAX_DIAGRAM_WIDTH or height > cls._MAX_DIAGRAM_HEIGHT:
            raise ValueError(
                f"Mermaid diagram {diagram_number} is too large for email."
            )

    @staticmethod
    def _mermaid_image_html(content_id: str, diagram_number: int) -> str:
        return (
            '<div style="margin:20px 0;text-align:center;">'
            f'<img src="cid:{html.escape(content_id, quote=True)}" '
            f'alt="Mermaid diagram {diagram_number}" '
            'style="display:block;height:auto;margin:0 auto;max-width:100%;width:auto;" />'
            "</div>"
        )

    @staticmethod
    def _mermaid_fallback_html(source: str, reason: str) -> str:
        return (
            '<div style="background:#fff7ed;border:1px solid #fed7aa;border-radius:6px;'
            'margin:18px 0;padding:14px;">'
            f'<p style="color:#9a3412;font-weight:600;margin:0 0 10px;">{html.escape(reason)}</p>'
            '<pre style="background:#ffffff;border:1px solid #e2e8f0;border-radius:4px;'
            "color:#0f172a;font-family:SFMono-Regular,Consolas,Liberation Mono,monospace;"
            "font-size:13px;line-height:1.45;margin:0;overflow-wrap:anywhere;padding:12px;"
            f'white-space:pre-wrap;">{html.escape(source)}</pre></div>'
        )

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
            raise ValueError(
                "The current OpenWebUI account has no valid email address."
            )
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
    def _format_bytes(size: int) -> str:
        if size % (1024 * 1024) == 0:
            return f"{size // (1024 * 1024)} MB"
        return f"{size / (1024 * 1024):.1f} MB"

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
        email_body: EmailBody,
        attachment: Optional[EmailAttachment] = None,
    ) -> None:
        payload = {
            "personalizations": [{"to": [{"email": recipient}]}],
            "from": {
                "email": self.valves.SENDER_EMAIL.strip(),
                "name": self.valves.SENDER_NAME.strip() or "OpenWebUI",
            },
            "subject": subject,
            "content": [
                {"type": "text/plain", "value": email_body.plain},
                {"type": "text/html", "value": email_body.html},
            ],
        }
        attachments = list(email_body.inline_attachments)
        if attachment:
            attachments.append(attachment)
        if attachments:
            payload["attachments"] = []
            for item in attachments:
                encoded = {
                    "content": base64.b64encode(item.content).decode("ascii"),
                    "type": item.content_type,
                    "filename": item.filename,
                    "disposition": item.disposition,
                }
                if item.content_id:
                    encoded["content_id"] = item.content_id
                payload["attachments"].append(encoded)

        request = urllib.request.Request(
            "https://api.sendgrid.com/v3/mail/send",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.valves.SENDGRID_API_KEY.strip()}",
                "Content-Type": "application/json",
                "User-Agent": "openwebui-sendgrid-notifier/1.3",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            if response.status != 202:
                raise RuntimeError(
                    f"Unexpected SendGrid response: HTTP {response.status}"
                )

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
