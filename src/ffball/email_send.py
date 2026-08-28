"""Send the combined report via Gmail SMTP (app password)."""

from __future__ import annotations

import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from .config import Settings


def send_email(settings: Settings, subject: str, html_body: str, text_body: str) -> None:
    if not settings.email_enabled:
        print("  EMAIL_ENABLED=0 — skipping send (report written to ./reports/).")
        return
    if not (settings.gmail_address and settings.gmail_app_password):
        print("  ! Missing GMAIL_ADDRESS / GMAIL_APP_PASSWORD — cannot send email.")
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = settings.gmail_address
    msg["To"] = settings.email_to
    msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx) as server:
        server.login(settings.gmail_address, settings.gmail_app_password)
        server.sendmail(settings.gmail_address, [settings.email_to], msg.as_string())
    print(f"  Email sent to {settings.email_to}")
