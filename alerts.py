"""
Fire-and-forget alerts: Discord webhook and optional email on trades and errors.
Failing to send never breaks the bot.
Discord messages are deliberately over-the-top dramatic and meme-worthy.
"""
import os
import random
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from dotenv import load_dotenv

load_dotenv()

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")
# Optional email (e.g. for errors only): ALERT_EMAIL_SMTP_URL, ALERT_EMAIL_FROM, ALERT_EMAIL_TO
# ALERT_EMAIL_SMTP_URL format: smtps://user:pass@smtp.example.com:465 or smtp://...

# Overly dramatic Discord one-liners (embarrassingly meme-worthy)
BUY_LINES = [
    "🚀 TO THE MOON!!! 🚀",
    "📈 WE'RE GOING UP!!! BUY THE DIP!!! 📈",
    "💎 DIAMOND HANDS INCOMING!!! 💎",
    "🦍 APE TOGETHER STRONG!!! 🦍",
    "🌙 MOON MISSION ACTIVATED!!! 🌙",
    "🔥 THIS IS THE WAY!!! 🔥",
    "⚡ LFG!!! LAMBOS WHEN?! ⚡",
    "🎯 BULLISH AF!!! 🎯",
]
SELL_LINES = [
    "💸 EXIT STAGE LEFT!!! 💸",
    "📉 TAKING PROFITS LIKE A DEGEN!!! 📉",
    "🖐️ PAPER HANDS? NAH, SMART HANDS!!! 🖐️",
    "🏃 CATCH YOU ON THE FLIP SIDE!!! 🏃",
    "✨ CASH IS A POSITION!!! ✨",
    "🎲 WE OUT!!! 🎲",
]
STOP_LOSS_LINES = [
    "🛑 STOP-LOSS HIT!!! LIVE TO TRADE ANOTHER DAY!!! 🛑",
    "😤 NOT TODAY, MARKET!!! NOT TODAY!!! 😤",
    "💪 CUTTING LOSSES LIKE A BOSS!!! 💪",
    "🛡️ RISK MANAGEMENT = WINNING!!! 🛡️",
]
HODL_LINES = [
    "💎 HODL!!! HODL!!! HODL!!! 💎🙌",
    "🙌 DIAMOND HANDS!!! NO SIGNAL = NO PROBLEM!!! 🙌",
    "😌 CHILLIN!!! WAITING FOR THE PERFECT ENTRY!!! 😌",
    "🦗 *crickets* ... STILL HODLING!!! 🦗",
    "⏳ PATIENCE IS A VIRTUE!!! HODL!!! ⏳",
]
ERROR_LINES = [
    "🔥 THE SYSTEM IS IN CHAOS!!! 🔥",
    "😱 NOT LIKE THIS!!! NOT LIKE THIS!!! 😱",
    "🚨 EVERYBODY STAY CALM!!! (I'm not calm)!!! 🚨",
    "💥 SOMETHING HAS GONE TERRIBLY WRONG!!! 💥",
    "🤖 BOT HAS THE SAD!!! 🤖",
]


def send_alert(message: str, level: str = "info") -> None:
    """
    Send an alert to Discord (if configured) and optionally to email for level 'error'.
    level: 'info', 'trade', 'error', 'hodl'. Discord gets dramatic meme flair. Does not raise.
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


def _dramatize(message: str, level: str) -> str:
    """Wrap message in embarrassingly dramatic Discord flair."""
    msg_upper = message.upper()
    if "BUY" in msg_upper and "SELL" not in msg_upper:
        flair = random.choice(BUY_LINES)
        return f"{flair}\n\n**{message}**"
    if "STOP-LOSS" in msg_upper or "STOP LOSS" in msg_upper:
        flair = random.choice(STOP_LOSS_LINES)
        return f"{flair}\n\n**{message}**"
    if "SELL" in msg_upper:
        flair = random.choice(SELL_LINES)
        return f"{flair}\n\n**{message}**"
    if level == "hodl":
        flair = random.choice(HODL_LINES)
        return f"{flair}\n\n**{message}**"
    if level == "error":
        flair = random.choice(ERROR_LINES)
        return f"{flair}\n\n**{message}**"
    return f"**{message}**"


def _send_discord(message: str, level: str) -> None:
    import requests
    content = _dramatize(message, level)
    if len(content) > 2000:
        content = content[:1997] + "..."
    requests.post(DISCORD_WEBHOOK_URL, json={"content": content}, timeout=5)


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
