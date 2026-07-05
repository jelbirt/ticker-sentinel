"""Email delivery via SMTP. All secrets from env vars — never from files."""
from __future__ import annotations

import logging
import os
import smtplib
from email.message import EmailMessage

log = logging.getLogger(__name__)


def send_report(html: str, subject: str) -> tuple[bool, str]:
    """Send the HTML report. Returns (sent, human-readable status) — never raises."""
    # `or` (not .get defaults): unset secrets reach Actions steps as empty strings
    host = os.environ.get("SMTP_HOST") or "smtp.gmail.com"
    port = int(os.environ.get("SMTP_PORT") or "587")
    user = os.environ.get("SMTP_USER") or None
    password = os.environ.get("SMTP_PASS") or None
    recipients = [
        r.strip() for r in os.environ.get("RECIPIENT_EMAILS", "").split(",") if r.strip()
    ]

    if not user or not password or not recipients:
        return False, "email skipped: SMTP_USER / SMTP_PASS / RECIPIENT_EMAILS not all set"

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = ", ".join(recipients)
    msg.set_content("This report is HTML-only; please view in an HTML-capable client.")
    msg.add_alternative(html, subtype="html")

    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=60) as smtp:
                smtp.login(user, password)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=60) as smtp:
                smtp.starttls()
                smtp.login(user, password)
                smtp.send_message(msg)
    except Exception as exc:
        log.warning("email delivery failed: %s", exc)
        return False, f"email delivery failed: {exc}"
    return True, f"email sent to {len(recipients)} recipient(s)"
