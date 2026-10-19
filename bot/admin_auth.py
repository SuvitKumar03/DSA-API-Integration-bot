"""Who is an admin.

Checked in this order:
1. Google sign-in: the admin page signs in with Google, the server verifies the Google ID
   token, checks the email against ADMIN_EMAILS and sets a signed, HttpOnly session cookie.
2. Google IAP (TRUST_IAP=true): on Cloud Run behind Identity-Aware Proxy, IAP has already
   signed the user in and passes their email in a header. Only enable this when the service
   can be reached through IAP alone.
3. Admin codes (ADMIN_CODES): local development fallback only.

The email is re-checked against ADMIN_EMAILS on every request, so removing someone from the
list revokes their access immediately.
"""

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time

from bot import config

log = logging.getLogger(__name__)

SESSION_COOKIE = "admin_session"
SESSION_HOURS = 8

_SECRET = config.ADMIN_SESSION_SECRET.encode() if config.ADMIN_SESSION_SECRET else secrets.token_bytes(32)
if not config.ADMIN_SESSION_SECRET and (config.ADMIN_EMAILS or config.USER_LOGIN == "google"):
    log.warning("ADMIN_SESSION_SECRET is not set: sign-ins end when the server restarts and "
                "don't work across several instances. Set it in production.")


def enabled() -> bool:
    return bool(config.ADMIN_EMAILS or config.ADMIN_CODES)


def is_admin_email(email: str | None) -> bool:
    return bool(email) and email.strip().lower() in config.ADMIN_EMAILS


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def make_session(email: str, aud: str = "admin", hours: float = SESSION_HOURS, **extra) -> str:
    """A signed cookie value. `aud` keeps admin and chat-user sessions apart: one can never be
    used as the other."""
    body = {"email": email.lower(), "aud": aud, "exp": int(time.time() + hours * 3600), **extra}
    payload = _b64(json.dumps(body).encode())
    sig = _b64(hmac.new(_SECRET, payload.encode(), hashlib.sha256).digest())
    return f"{payload}.{sig}"


def read_session_data(token: str | None, aud: str = "admin") -> dict | None:
    """The contents of a valid, unexpired session cookie for `aud`, or None."""
    if not token or "." not in token:
        return None
    payload, sig = token.rsplit(".", 1)
    expected = _b64(hmac.new(_SECRET, payload.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        data = json.loads(_unb64(payload))
    except ValueError:
        return None
    if data.get("aud", "admin") != aud or data.get("exp", 0) <= time.time():
        return None
    return data


def read_session(token: str | None) -> str | None:
    """The email in a valid admin session cookie, or None."""
    data = read_session_data(token, "admin")
    return data["email"] if data else None


def verify_google_credential(credential: str) -> dict:
    """Check a Google Sign-In ID token; returns its claims. Raises ValueError if invalid."""
    if not config.GOOGLE_CLIENT_ID:
        raise ValueError("Google sign-in is not configured (GOOGLE_CLIENT_ID)")
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token

    claims = id_token.verify_oauth2_token(credential, google_requests.Request(), config.GOOGLE_CLIENT_ID)
    if not claims.get("email_verified"):
        raise ValueError("Google account email is not verified")
    return claims


def iap_email(header_value: str | None) -> str | None:
    """IAP sends "accounts.google.com:user@example.com"."""
    if not config.TRUST_IAP or not header_value:
        return None
    return header_value.split(":", 1)[-1].strip().lower() or None
