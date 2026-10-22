"""Send one operational alert email, independent of the running bot.

The login guard writes a UTF-8 JSON request and invokes this script with the
project venv python, so alerts still work when the bot process is down.
"""
from __future__ import annotations

import argparse
import json
import os
import smtplib
import ssl
import sys
from email.message import EmailMessage
from email.utils import formataddr, formatdate
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_request(path: str | Path) -> tuple[str, str]:
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("request must be a JSON object")
    subject = str(data.get("subject") or "").strip()
    body = str(data.get("body") or "").strip()
    if not subject or not body:
        raise ValueError("subject and body are required")
    if any(c in subject for c in "\r\n"):
        raise ValueError("subject must be a single line")
    return subject, body


def build_message(
    *,
    sender: str,
    sender_name: str,
    recipient: str,
    subject: str,
    body: str,
) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = formataddr((sender_name, sender))
    msg["To"] = recipient
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Auto-Submitted"] = "auto-generated"
    msg["X-InfoHub-Alert"] = "1"
    # Keep feedback/IMAP reconciliation from treating alerts as report mail.
    msg["X-InfoHub-ReadOnly"] = "1"
    msg.set_content(body)
    return msg


def send_message(
    msg: EmailMessage,
    *,
    host: str,
    port: int,
    user: str,
    password: str,
) -> None:
    context = ssl.create_default_context()
    if port == 587:
        with smtplib.SMTP(host, port, timeout=20) as smtp:
            smtp.starttls(context=context)
            smtp.login(user, password)
            smtp.send_message(msg)
    else:
        with smtplib.SMTP_SSL(host, port, context=context, timeout=20) as smtp:
            smtp.login(user, password)
            smtp.send_message(msg)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="send one info-hub alert mail")
    parser.add_argument("--request", required=True, help="UTF-8 JSON with subject/body")
    args = parser.parse_args(argv)

    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    sender = os.getenv("MAIL_ADDRESS", "").strip()
    password = os.getenv("MAIL_AUTH_CODE", "").strip()
    host = os.getenv("MAIL_SMTP_HOST", "smtp.qq.com").strip()
    recipient = os.getenv("ALERT_EMAIL_TO", "").strip() or sender
    try:
        port = int(os.getenv("MAIL_SMTP_PORT", "465").strip())
    except ValueError:
        print("invalid MAIL_SMTP_PORT", file=sys.stderr)
        return 2
    if not sender or not password or not recipient:
        print("missing mail credentials", file=sys.stderr)
        return 2

    try:
        subject, body = load_request(args.request)
    except Exception as exc:  # noqa: BLE001
        print(f"invalid request: {type(exc).__name__}", file=sys.stderr)
        return 2

    msg = build_message(
        sender=sender,
        sender_name=os.getenv("MAIL_FROM_NAME", "info-hub").strip() or "info-hub",
        recipient=recipient,
        subject=subject,
        body=body,
    )
    try:
        send_message(msg, host=host, port=port, user=sender, password=password)
    except Exception as exc:  # noqa: BLE001
        print(f"send failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    print("sent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
