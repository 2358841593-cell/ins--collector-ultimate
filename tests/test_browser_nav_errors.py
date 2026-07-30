"""Browser navigation failures retain a safe, actionable network error code."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import browser_collect_v2 as browser  # noqa: E402
from extensions.sop_v2.pipeline import _base  # noqa: E402


class _FailingPage:
    def goto(self, *_args, **_kwargs):
        raise RuntimeError(
            "Page.goto: net::ERR_TUNNEL_CONNECTION_FAILED "
            "at https://www.instagram.com/example/"
        )


class _Response:
    status = 429


class _StatusPage:
    def goto(self, *_args, **_kwargs):
        return _Response()


def test_goto_keeps_only_safe_chromium_network_code():
    with mock.patch.object(browser.time, "sleep"):
        assert not browser._goto(_FailingPage(), "https://example.com", tries=1)
    assert browser.LAST_NAV_ERR == "net::ERR_TUNNEL_CONNECTION_FAILED"
    assert "instagram.com" not in browser.LAST_NAV_ERR


def test_goto_records_http_status():
    with mock.patch.object(browser.time, "sleep"):
        assert not browser._goto(_StatusPage(), "https://example.com", tries=1)
    assert browser.LAST_NAV_ERR == "HTTP429"


def test_sticky_session_is_stable_per_block_but_changes_per_run():
    first = _base._sticky_session("run-one", 1, "user.name")
    assert first == _base._sticky_session("run-one", 1, "user.name")
    assert first != _base._sticky_session("run-two", 1, "user.name")
    assert first != _base._sticky_session("run-one", 2, "user.name")
    assert first.isalnum()
    assert len(first) <= 16
