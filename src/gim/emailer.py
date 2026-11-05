"""Optional e-mail delivery of reports (SMTP). Nothing is sent unless configured in .env:

    GIM_SMTP_HOST=smtp.example.org
    GIM_SMTP_PORT=587                 # STARTTLS; 465 = implicit TLS
    GIM_SMTP_USER=...
    GIM_SMTP_PASSWORD=...             # redacted in all logs
    GIM_EMAIL_FROM=gim@example.org
    GIM_EMAIL_TO=you@example.org      # comma-separated

Only TLS connections are used. Reports are sent as DRAFTS with the standard caveats.
"""
from __future__ import annotations

import os
import smtplib
import ssl
from email.message import EmailMessage


class EmailNotConfigured(RuntimeError):
    pass


def config() -> dict:
    cfg = {k: os.getenv(f"GIM_{k.upper()}", "").strip() for k in
           ("smtp_host", "smtp_port", "smtp_user", "smtp_password", "email_from", "email_to")}
    missing = [k for k in ("smtp_host", "email_from", "email_to") if not cfg[k]]
    if missing:
        raise EmailNotConfigured("set " + ", ".join(f"GIM_{m.upper()}" for m in missing) + " in .env")
    cfg["smtp_port"] = int(cfg["smtp_port"] or 587)
    return cfg


def build_message(subject: str, body: str, sender: str, recipients: list[str],
                  pdf: bytes | None = None, pdf_name: str = "report.pdf") -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = subject[:200]
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg.set_content(body)
    if pdf:
        msg.add_attachment(pdf, maintype="application", subtype="pdf", filename=pdf_name)
    return msg


def send_report(subject: str, markdown: str, pdf: bytes | None, pdf_name: str) -> list[str]:
    cfg = config()
    recipients = [r.strip() for r in cfg["email_to"].split(",") if r.strip()]
    body = ("Global Intelligence Monitor — automated DRAFT report. Machine-assisted; analyst review required.\n\n"
            + markdown[:60000])
    msg = build_message(subject, body, cfg["email_from"], recipients, pdf, pdf_name)
    context = ssl.create_default_context()
    if cfg["smtp_port"] == 465:
        server = smtplib.SMTP_SSL(cfg["smtp_host"], 465, context=context, timeout=60)
    else:
        server = smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"], timeout=60)
        server.starttls(context=context)
    try:
        if cfg["smtp_user"]:
            server.login(cfg["smtp_user"], cfg["smtp_password"])
        server.send_message(msg)
    finally:
        server.quit()
    return recipients
