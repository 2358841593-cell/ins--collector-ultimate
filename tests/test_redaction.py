"""脱敏扫描测试（P0-13 / AUTH-05）。"""
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from extensions.sop_v2 import redaction  # noqa: E402


class TestRedaction(unittest.TestCase):
    def test_catches_sessionid(self):
        hits = redaction.scan_text("cookie sessionid=12345%3AabcDEF%3A22 more")
        self.assertTrue(any(h["pattern"] == "sessionid" for h in hits))

    def test_catches_password_kv(self):
        hits = redaction.scan_text("password: hunter2secret")
        self.assertTrue(any(h["pattern"] == "password_kv" for h in hits))

    def test_catches_totp(self):
        hits = redaction.scan_text("secret WSIOSZ4KVMJU25DDCLBGHLWOBXUYE325 here")
        self.assertTrue(any(h["pattern"] == "totp_secret" for h in hits))

    def test_clean_delivery_text_passes(self):
        clean = ("Handle @annascountryhome Followers 18525 Storefront confirmed_yes "
                 "AI Vetting Score 6.2 Review Reason modash_core_missing")
        self.assertEqual(redaction.scan_text(clean), [])

    def test_masked_output_no_full_secret(self):
        hits = redaction.scan_text("sessionid=12345%3AsupersecretvalueXYZ%3A22")
        for h in hits:
            self.assertIn("***", h["sample"])
            self.assertNotIn("supersecretvalueXYZ", h["sample"])


if __name__ == "__main__":
    unittest.main()
