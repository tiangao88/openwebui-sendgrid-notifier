# OpenWebUI SendGrid Notifier

An OpenWebUI workspace tool that lets an LLM send an email notification to the
current user. The destination is locked to the email address in the authenticated
OpenWebUI user profile. It is not exposed as a tool argument or valve.

## Features

- Uses OpenWebUI's injected `__user__["email"]` as the only recipient
- Keeps the SendGrid API key in an admin-configured password valve
- Sends plain-text email through SendGrid API v3
- Shows in-chat progress and error notifications
- Suppresses repeated sends from the same OpenWebUI message for 24 hours
- Limits each user to one successful notification every 10 minutes by default
- Uses only Python's standard library for HTTP requests
- Masks the destination address in the tool result

## Install

1. In OpenWebUI, open **Workspace → Tools → Create Tool**.
2. Copy the complete contents of `openwebui_sendgrid_notifier.py` into the editor.
3. Save the tool.
4. Open the tool's **Valves** settings and configure:
   - `SENDGRID_API_KEY`: a SendGrid key with **Mail Send** permission
   - `SENDER_EMAIL`: a sender address verified in SendGrid
   - `SENDER_NAME`: the display name recipients will see
   - `RATE_LIMIT_MINUTES`: minimum delay per user; defaults to `10` and can be
     set to `0` to disable the cooldown
5. Enable the tool for the desired model or select it in a chat.

The sender address is configurable because SendGrid requires a verified sender.
The recipient is deliberately not configurable.

## Suggested model instruction

Add this to the model's system prompt if you want conservative tool use:

> Use `send_email_notification` only when the user explicitly asks for an email
> notification. Briefly confirm the subject and notification content before calling
> it. The recipient is automatically the current user's OpenWebUI account email.

## Tool arguments

| Argument | Description |
|---|---|
| `subject` | Email subject, maximum 200 characters |
| `message` | Plain-text email body, maximum 50,000 characters |

There is intentionally no `to`, `recipient`, `cc`, or `bcc` argument.

## Delivery safeguards

- **Idempotency:** OpenWebUI's injected `__message_id__` identifies the active
  assistant turn. Once a notification for that message succeeds, repeat calls are
  suppressed for 24 hours, even if the model changes the subject or body.
- **Per-user rate limit:** only one successful delivery is allowed per user during
  `RATE_LIMIT_MINUTES`. The authenticated OpenWebUI user ID is used when available;
  otherwise the normalized account email is used.
- **Failures are retryable:** SendGrid failures do not consume the cooldown and do
  not mark the message as delivered.
- **Concurrency:** simultaneous calls for the same user are serialized by the
  safeguards, preventing two requests from passing the checks together.

Safeguard state is held in memory and shared by all tool instances in one Python
process. It resets when OpenWebUI restarts and is not shared between multiple
OpenWebUI workers or replicas. Use a shared persistent store if you require
cross-process or restart-safe enforcement.

## Test

```bash
python -m pip install -e '.[test]'
pytest -q
```

Tests mock the SendGrid endpoint and never send real email.

## Security notes

- OpenWebUI workspace tools execute inside the OpenWebUI server process. Review
  all tool code before installing it.
- Give the SendGrid key only **Mail Send** permission.
- Restrict tool availability if users should not be able to trigger notifications.
- SendGrid may still enforce account, sender-verification, and rate limits.

## License

MIT
