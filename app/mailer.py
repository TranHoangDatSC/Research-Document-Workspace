"""Outgoing email over SMTP (standard library only), configured from .env:

    SMTP_HOST, SMTP_PORT (587), SMTP_USERNAME, SMTP_PASSWORD,
    SMTP_FROM, SMTP_SECURITY (starttls | ssl | none)

Any SMTP provider works — Gmail with an app password, Brevo, Mailgun,
SendGrid, Resend (all have SMTP endpoints; see .env.example).

With SMTP_HOST unset (local development) nothing is sent: the message is
written to the server log instead, so the password-reset flow can still be
tried end to end by copying the link from `docker compose logs web`.
"""
import logging
import os
import smtplib
import ssl
from email.message import EmailMessage

log = logging.getLogger("uvicorn.error")
TIMEOUT_SECONDS = 15


class MailError(Exception):
    pass


def configured():
    return bool(os.environ.get("SMTP_HOST", "").strip())


def send(to, subject, body):
    if not configured():
        log.warning("mail_not_sent reason=smtp-not-configured to=%s subject=%r\n%s", to, subject, body)
        return

    host = os.environ["SMTP_HOST"].strip()
    # Blank counts as the default too: `SMTP_SECURITY=` left empty in .env must
    # not silently send the SMTP password unencrypted.
    security = os.environ.get("SMTP_SECURITY", "").strip().lower() or "starttls"
    default_port = 465 if security == "ssl" else 587
    port = int(os.environ.get("SMTP_PORT", "").strip() or default_port)
    username = os.environ.get("SMTP_USERNAME", "").strip()
    password = os.environ.get("SMTP_PASSWORD", "")
    sender = os.environ.get("SMTP_FROM", "").strip() or username

    message = EmailMessage()
    message["From"] = sender
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)

    context = ssl.create_default_context()
    try:
        if security == "ssl":
            server = smtplib.SMTP_SSL(host, port, timeout=TIMEOUT_SECONDS, context=context)
        else:
            server = smtplib.SMTP(host, port, timeout=TIMEOUT_SECONDS)
        with server:
            if security == "starttls":
                server.starttls(context=context)
            if username:
                server.login(username, password)
            server.send_message(message)
    except (OSError, smtplib.SMTPException) as exc:
        # Never include the password; the exception text from smtplib doesn't.
        raise MailError(f"{type(exc).__name__}: {exc}") from None
    log.info("mail_sent to=%s subject=%r", to, subject)
