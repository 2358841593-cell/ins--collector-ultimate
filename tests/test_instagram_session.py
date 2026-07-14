from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from scripts.instagram_session import (
    apply_full_cookie_session,
    cookie_user_id,
    load_browser_cookie_export,
)


def sample_cookies() -> dict[str, str]:
    return {
        "sessionid": "12345%3Atoken",
        "ds_user_id": "12345",
        "csrftoken": "csrf",
        "mid": "machine",
        "ig_did": "device",
        "rur": "region",
    }


class InstagramSessionTests(unittest.TestCase):
    def test_cookie_user_id_accepts_url_encoded_sessionid(self) -> None:
        self.assertEqual(cookie_user_id(sample_cookies()), "12345")

    def test_cookie_user_id_rejects_identity_mismatch(self) -> None:
        cookies = sample_cookies()
        cookies["ds_user_id"] = "99999"
        with self.assertRaisesRegex(ValueError, "不属于同一账号"):
            cookie_user_id(cookies)

    def test_browser_export_requires_private_permissions_and_keeps_all_cookies(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "cookies.json"
            payload = {
                "cookies": [
                    {"name": name, "value": value}
                    for name, value in sample_cookies().items()
                ]
            }
            path.write_text(json.dumps(payload), encoding="utf-8")
            os.chmod(path, 0o600)
            self.assertEqual(load_browser_cookie_export(path), sample_cookies())

    def test_apply_full_cookie_session_syncs_private_and_public_jars(self) -> None:
        from instagrapi import Client

        cookies = sample_cookies()
        client = Client()
        user_id = apply_full_cookie_session(client, cookies)
        self.assertEqual(user_id, "12345")
        self.assertEqual(client.authorization_data["ds_user_id"], "12345")
        self.assertEqual(client.mid, "machine")
        for transport in (client.private, client.public):
            actual = transport.cookies.get_dict()
            self.assertTrue(set(cookies).issubset(actual))


if __name__ == "__main__":
    unittest.main()
