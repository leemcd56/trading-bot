"""
Fire-and-forget alerts: Discord webhook and optional email on trades and errors.
Failing to send never breaks the bot.
"""
import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from dotenv import load_dotenv

load_dotenv()

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")
# Optional email (e.g. for errors only): ALERT_EMAIL_SMTP_URL, ALERT_EMAIL_FROM, ALERT_EMAIL_TO
# ALERT_EMAIL_SMTP_URL format: smtps://user:pass@smtp.example.com:465 or smtp://...


def send_alert(message: str, level: str = "info") -> None:
    """
    Send an alert to Discord (if configured) and optionally to email for level 'error'.
    level: 'info', 'trade', 'error'. Does not raise.
    """
    try:
        if DISCORD_WEBHOOK_URL:
            _send_discord(message, level)
    except Exception:
        pass
    try:
        if level == "error" and os.getenv("ALERT_EMAIL_TO"):
            _send_email(message, level)
    except Exception:
        pass


def _send_discord(message: str, level: str) -> None:
    import requests
    payload = {"content": f"[{level.upper()}] {message}"}
    if len(payload["content"]) > 2000:
        payload["content"] = payload["content"][:1997] + "..."
    requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=5)


def _send_email(message: str, level: str) -> None:
    url = os.getenv("ALERT_EMAIL_SMTP_URL", "")
    from_addr = os.getenv("ALERT_EMAIL_FROM", "")
    to_addr = os.getenv("ALERT_EMAIL_TO", "")
    if not url or not from_addr or not to_addr:
        return
    msg = MIMEMultipart()
    msg["Subject"] = f"Trading bot [{level}]"
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg.attach(MIMEText(message, "plain"))
    # Parse simple smtp or smtps URL
    use_tls = "smtps" in url.split("://")[0].lower()
    parsed = url.replace("smtps://", "").replace("smtp://", "")
    if "@" in parsed:
        auth, host = parsed.rsplit("@", 1)
        user, password = auth.split(":", 1) if ":" in auth else (None, None)
        host = host.split("/")[0]
        host, port = host.split(":") if ":" in host else (host, 465 if use_tls else 587)
        port = int(port)
    else:
        host, port, user, password = parsed.split("/")[0], 587, None, None
        if ":" in host:
            host, port = host.split(":", 1)
            port = int(port)
    if use_tls:
        with smtplib.SMTP_SSL(host, port) as s:
            if user and password:
                s.login(user, password)
            s.sendmail(from_addr, [to_addr], msg.as_string())
    else:
        with smtplib.SMTP(host, port) as s:
            s.starttls()
            if user and password:
                s.login(user, password)
            s.sendmail(from_addr, [to_addr], msg.as_string())
