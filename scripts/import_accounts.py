#!/usr/bin/env python3
"""Import Instagram account credentials from stdin into a local secrets file.

Expected line format:
username----password----totp_secret----cookie_string----phone

The output file is local-only and should never be committed.
"""

from __future__ import annotations

import json
import re
import stat
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SECRETS_DIR = ROOT / ".secrets"
ACCOUNTS_FILE = SECRETS_DIR / "instagram_accounts.json"


def parse_line(line: str) -> dict:
    parts = line.strip().split("----", 3)
    if len(parts) != 4:
        raise ValueError("expected username----password----totp_secret----cookie_string[----phone]")
    username, password, totp_secret, cookie_and_phone = parts
    phone = ""
    cookie_string = cookie_and_phone.strip()
    phone_match = re.search(r"-+\+(\d{6,})$", cookie_string)
    if phone_match:
        phone = f"+{phone_match.group(1)}"
        cookie_string = cookie_string[: phone_match.start()].rstrip("-")
    # Handle email----phone format at the end
    email = ""
    email_match = re.search(r"-+([^\-]+@[^\-]+\.[^\-]+)-+\+(\d{6,})$", cookie_string)
    if email_match:
        email = email_match.group(1)
        phone = f"+{email_match.group(2)}"
        cookie_string = cookie_string[: email_match.start()].rstrip("-")
    return {
        "username": username.strip(),
        "password": password.strip(),
        "totp_secret": totp_secret.strip(),
        "cookie_string": cookie_string.strip(),
        "phone": phone.strip(),
    }


def main() -> int:
    raw_lines = [line for line in sys.stdin.read().splitlines() if line.strip()]
    if not raw_lines:
        raise SystemExit("Paste account lines via stdin.")

    accounts = []
    for index, line in enumerate(raw_lines, start=1):
        try:
            accounts.append(parse_line(line))
        except ValueError as exc:
            raise SystemExit(f"Line {index}: {exc}") from exc

    SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    ACCOUNTS_FILE.write_text(json.dumps({"accounts": accounts}, ensure_ascii=False, indent=2), encoding="utf-8")
    ACCOUNTS_FILE.chmod(stat.S_IRUSR | stat.S_IWUSR)

    print(f"Saved {len(accounts)} accounts to {ACCOUNTS_FILE}")
    for acc in accounts:
        print(f"  - {acc['username']}")
    print("File permissions set to 600.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
