"""Connect the Gmail account that sends the daily "documentation gaps" emails.

Signs in with Google in the browser (send-only permission) and saves the token to
GMAIL_TOKEN_FILE (default chat_export/gmail_token.json, git-ignored). Uses the same OAuth
desktop client as the Chat export (chat_export/credentials.json). The Gmail API must be enabled
in that Google Cloud project (APIs & Services > Library > Gmail API > Enable).

Usage:
    python scripts/connect_gmail.py
    python scripts/connect_gmail.py --test <your email>    # also send a test email
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from google_auth_oauthlib.flow import InstalledAppFlow  # noqa: E402

from bot import config, mailer  # noqa: E402

CLIENT_FILE = config.ROOT_DIR / "chat_export" / "credentials.json"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--test", metavar="EMAIL", help="send a test email to this address")
    args = ap.parse_args()
    if not CLIENT_FILE.exists():
        sys.exit(f"Missing {CLIENT_FILE}: the OAuth desktop client JSON (same one the Chat export uses).")
    creds = InstalledAppFlow.from_client_secrets_file(str(CLIENT_FILE), mailer.SCOPES).run_local_server(port=0)
    config.GMAIL_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    config.GMAIL_TOKEN_FILE.write_text(creds.to_json())
    print(f"Gmail connected. Token saved to {config.GMAIL_TOKEN_FILE}")
    if args.test:
        mailer.send([args.test], "Integration Assistant: test email",
                    "Gmail is connected. Daily documentation-gap emails will be sent from this account.")
        print(f"Test email sent to {args.test}")


if __name__ == "__main__":
    main()
