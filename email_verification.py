"""SMTP email ownership checks with durable per-account and recipient limits."""

import hashlib
import hmac
import os
import re
import secrets
import smtplib
import ssl
import time
from email.message import EmailMessage

import database
from tutorial import progress

TOKEN_LIFETIME = 30 * 60
RESEND_COOLDOWN = 60
SEND_WINDOW = 24 * 3600
MAX_SENDS = 5
MAX_ATTEMPTS = 5


def normalize_email(address):
    """Use a conservative ASCII mailbox syntax and collapse common aliases."""
    if not isinstance(address, str) or len(address) > 254:
        raise ValueError("Enter a valid email address (maximum 254 characters).")
    address = address.strip().lower()
    if address.count("@") != 1:
        raise ValueError("Enter a valid email address.")
    local, domain = address.split("@")
    if (
        not 1 <= len(local) <= 64
        or not re.fullmatch(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+", local)
        or local.startswith(".") or local.endswith(".") or ".." in local
        or not re.fullmatch(
            r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
            r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+", domain
        )
    ):
        raise ValueError("Enter a valid email address.")
    canonical = local.split("+", 1)[0]
    if not canonical:
        raise ValueError("Enter a valid email address.")
    if domain in ("gmail.com", "googlemail.com"):
        domain = "gmail.com"
        canonical = canonical.replace(".", "")
    return address, f"{canonical}@{domain}"


def send_verification_email(address, token):
    host = os.environ.get("GRM_SMTP_HOST")
    sender = os.environ.get("GRM_SMTP_FROM")
    if not host or not sender:
        raise ValueError("Email delivery is not configured; tutorial rewards remain locked.")
    sender, _ = normalize_email(sender)
    try:
        port = int(os.environ.get("GRM_SMTP_PORT", "587"))
    except ValueError:
        raise ValueError("Email delivery configuration is invalid.") from None
    security = os.environ.get("GRM_SMTP_SECURITY", "starttls")
    username = os.environ.get("GRM_SMTP_USERNAME")
    password = os.environ.get("GRM_SMTP_PASSWORD")
    if not 1 <= port <= 65535 or security not in ("starttls", "ssl") or bool(username) != bool(password):
        raise ValueError("Email delivery configuration is invalid.")
    message = EmailMessage()
    message["From"] = sender
    message["To"] = address
    message["Subject"] = "Galaxy Resource Manager: verify your tutorial email"
    message.set_content(
        "Verify your email in Galaxy Resource Manager with:\n\n"
        f"tutorial verify {token}\n\n"
        "This token expires in 30 minutes and works only in the account that "
        "requested it. If you did not request it, ignore this email.\n"
    )
    context = ssl.create_default_context()
    if security == "ssl":
        client = smtplib.SMTP_SSL(host, port, timeout=10, context=context)
    else:
        client = smtplib.SMTP(host, port, timeout=10)
    with client:
        if security == "starttls":
            client.starttls(context=context)
        if username:
            client.login(username, password)
        refused = client.send_message(message)
        if refused:
            raise smtplib.SMTPException("Recipient refused")


def request(player, address, clock=time.time):
    address, email_key = normalize_email(address)
    now = int(clock())
    token = secrets.token_urlsafe(24)
    digest = hashlib.sha256(token.encode("ascii")).hexdigest()
    with database._connection() as c:
        c.execute("BEGIN IMMEDIATE")
        progress(c, player)
        if c.execute(
            "SELECT 1 FROM tutorial_verified_emails WHERE player_id=?", (player,)
        ).fetchone():
            raise ValueError("This account already has a verified tutorial email.")
        previous = c.execute(
            "SELECT * FROM tutorial_email_requests WHERE player_id=?", (player,)
        ).fetchone()
        recipient = c.execute(
            "SELECT * FROM tutorial_email_limits WHERE email_key=?", (email_key,)
        ).fetchone()
        for limit in (previous, recipient):
            if limit is not None:
                if now - limit["last_sent_at"] < RESEND_COOLDOWN:
                    raise ValueError("Wait at least 60 seconds before requesting another email.")
                if now - limit["window_started_at"] < SEND_WINDOW and limit["sends"] >= MAX_SENDS:
                    raise ValueError("Verification email daily limit reached; try again after 24 hours.")
        account_window = (
            previous["window_started_at"]
            if previous is not None and now - previous["window_started_at"] < SEND_WINDOW
            else now
        )
        account_sends = previous["sends"] + 1 if account_window != now else 1
        recipient_window = (
            recipient["window_started_at"]
            if recipient is not None and now - recipient["window_started_at"] < SEND_WINDOW
            else now
        )
        recipient_sends = recipient["sends"] + 1 if recipient_window != now else 1
        c.execute(
            "INSERT INTO tutorial_email_requests VALUES (?,?,?,?,?,0,?,?,?) "
            "ON CONFLICT(player_id) DO UPDATE SET address=excluded.address,"
            "email_key=excluded.email_key,token_hash=excluded.token_hash,"
            "expires_at=excluded.expires_at,attempts=0,last_sent_at=excluded.last_sent_at,"
            "window_started_at=excluded.window_started_at,sends=excluded.sends",
            (player, address, email_key, digest, now + TOKEN_LIFETIME,
             now, account_window, account_sends)
        )
        c.execute(
            "INSERT INTO tutorial_email_limits VALUES (?,?,?,?) "
            "ON CONFLICT(email_key) DO UPDATE SET window_started_at=excluded.window_started_at,"
            "sends=excluded.sends,last_sent_at=excluded.last_sent_at",
            (email_key, recipient_window, recipient_sends, now)
        )
    # Reserve limits before SMTP, without holding the game's write lock during network I/O.
    try:
        send_verification_email(address, token)
    except (ValueError, OSError, smtplib.SMTPException):
        with database._connection() as c:
            c.execute(
                "UPDATE tutorial_email_requests SET token_hash=NULL "
                "WHERE player_id=? AND token_hash=?", (player, digest)
            )
        raise ValueError(
            "Verification email could not be sent; check SMTP configuration and try again later."
        ) from None
    return "Verification email sent. Use tutorial verify TOKEN within 30 minutes."


def verify(player, token, clock=time.time):
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32}", token):
        raise ValueError("Invalid verification token.")
    digest = hashlib.sha256(token.encode("ascii")).hexdigest()
    now = int(clock())
    error = None
    with database._connection() as c:
        c.execute("BEGIN IMMEDIATE")
        progress(c, player)
        if c.execute(
            "SELECT 1 FROM tutorial_verified_emails WHERE player_id=?", (player,)
        ).fetchone():
            raise ValueError("This account already has a verified tutorial email.")
        row = c.execute(
            "SELECT * FROM tutorial_email_requests WHERE player_id=?", (player,)
        ).fetchone()
        if row is None or row["token_hash"] is None or now >= row["expires_at"]:
            raise ValueError("Verification token is missing or expired; request another email.")
        if row["attempts"] >= MAX_ATTEMPTS:
            raise ValueError("Too many verification attempts; request another email.")
        c.execute(
            "UPDATE tutorial_email_requests SET attempts=attempts+1 WHERE player_id=?", (player,)
        )
        if not hmac.compare_digest(digest, row["token_hash"]):
            error = "Invalid verification token."
        else:
            c.execute("UPDATE tutorial_email_requests SET token_hash=NULL WHERE player_id=?", (player,))
            if c.execute(
                "SELECT 1 FROM tutorial_verified_emails WHERE email_key=?", (row["email_key"],)
            ).fetchone():
                error = "This email is already linked to a tutorial reward account."
            else:
                c.execute(
                    "INSERT INTO tutorial_verified_emails VALUES (?,?,?)",
                    (row["email_key"], player, now)
                )
    # Commit failed-attempt counters and consumed tokens even when reporting failure.
    if error:
        raise ValueError(error)
    return "Email verified. Finish the tutorial and use tutorial claim for your acre."
