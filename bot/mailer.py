"""Sends the daily gap emails from the admin's own Gmail (Gmail API, "send" permission only).

Connect once on the machine that runs the jobs:
    python scripts/connect_gmail.py            (signs in, saves GMAIL_TOKEN_FILE)
The token only allows sending mail; it can't read the mailbox.
"""

import base64
import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from bot import config

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.send"]


class MailError(Exception):
    pass


def connected() -> bool:
    return config.GMAIL_TOKEN_FILE.exists()


def _service():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    path = config.GMAIL_TOKEN_FILE
    if not path.exists():
        raise MailError("Gmail isn't connected. Run `python scripts/connect_gmail.py` once on this computer.")
    try:
        creds = Credentials.from_authorized_user_file(str(path), SCOPES)
        if not creds.valid:
            creds.refresh(Request())
            path.write_text(creds.to_json())
    except Exception as e:
        raise MailError(f"Gmail sign-in expired or invalid; run `python scripts/connect_gmail.py` again ({type(e).__name__}).")
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def send(to: list[str], subject: str, text: str, html: str | None = None) -> None:
    """Send one email to these addresses (sent from the connected account)."""
    to = sorted({t.strip() for t in to if t and "@" in t})
    if not to:
        raise MailError("No recipients")
    msg = MIMEMultipart("alternative")
    msg["To"] = ", ".join(to)
    msg["Subject"] = subject
    msg.attach(MIMEText(text, "plain", "utf-8"))
    if html:
        msg.attach(MIMEText(html, "html", "utf-8"))
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    try:
        _service().users().messages().send(userId="me", body={"raw": raw}).execute()
    except MailError:
        raise
    except Exception as e:
        raise MailError(f"Gmail could not send the email ({type(e).__name__}: {str(e)[:200]})")
    log.info("Email sent to %s: %s", to, subject)
