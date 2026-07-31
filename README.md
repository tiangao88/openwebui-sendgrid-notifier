# OpenWebUI SendGrid Notifier

An OpenWebUI workspace tool that lets an LLM send an email notification to the
current user. The destination is locked to the email address in the authenticated
OpenWebUI user profile. It is not exposed as a tool argument or valve.

## Features

- Uses OpenWebUI's injected `__user__["email"]` as the only recipient
- Keeps the SendGrid API key in an admin-configured password valve
- Sends plain-text email through SendGrid API v3
- Shows in-chat progress and error notifications
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
