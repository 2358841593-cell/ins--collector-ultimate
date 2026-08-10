"""并行浏览器批次可从不同账号起点轮换，且负偏移在启动前拒绝。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from extensions.sop_v2.pipeline import (  # noqa: E402
    _base,
    stage3_collect,
    stage3_pricing_backfill,
)


class _FakePage:
    def set_default_timeout(self, value):
        self.default_timeout = value

    def set_default_navigation_timeout(self, value):
        self.navigation_timeout = value


class _FakeContext:
    def __init__(self):
        self.pages = [_FakePage()]


class _FakePlaywright:
    def __enter__(self):
        return object()

    def __exit__(self, exc_type, exc, tb):
        return False


def test_account_offset_changes_start_and_wraps_pool():
    assert _base._account_index(3, 0, 0) == 0
    assert _base._account_index(3, 0, 2) == 2
    assert _base._account_index(3, 1, 2) == 0
    assert _base._account_index(3, 4, 5) == 0


def test_default_full_pool_and_explicit_non_wrapping_subpool():
    accounts = [{"username": name} for name in ("a", "b", "c", "d")]

    assert _base._select_account_pool(accounts) == accounts
    assert [
        item["username"]
        for item in _base._select_account_pool(accounts, account_offset=2)
    ] == ["c", "d", "a", "b"]
    assert [
        item["username"]
        for item in _base._select_account_pool(
            accounts, account_offset=1, account_count=2
        )
    ] == ["b", "c"]


def test_explicit_account_subpool_cannot_wrap_past_pool_end():
    accounts = [{"username": name} for name in ("a", "b", "c")]
    with pytest.raises(ValueError, match="账号子池越界"):
        _base._select_account_pool(
            accounts, account_offset=2, account_count=2
        )


def test_account_rotation_is_modulo_selected_slice_and_never_escapes():
    accounts = [{"username": name} for name in ("a", "b", "c", "d", "e")]

    selected = _base._select_account_pool(
        accounts,
        account_offset=1,
        account_count=3,
        account_rotation=4,
    )

    # 4 % 3 == 1: rotate only (b,c,d), never pull a/e across the worker boundary.
    assert [item["username"] for item in selected] == ["c", "d", "b"]
    assert {item["username"] for item in selected} == {"b", "c", "d"}


def test_run_browser_stage_uses_only_selected_subpool(monkeypatch):
    import browser_collect_v2 as browser
    import playwright.sync_api

    opened = []
    monkeypatch.setattr(
        browser,
        "load_accounts",
        lambda path: [{"username": name} for name in ("a", "b", "c")],
    )
    monkeypatch.setattr(browser, "load_proxy", lambda session: None)
    monkeypatch.setattr(
        browser,
        "open_ctx",
        lambda pw, acct, proxy: (
            opened.append(acct["username"]) or _FakeContext()
        ),
    )
    monkeypatch.setattr(browser, "close_ctx", lambda ctx: None)
    monkeypatch.setattr(browser, "_pause", lambda *args: None)
    monkeypatch.setattr(
        playwright.sync_api, "sync_playwright", lambda: _FakePlaywright()
    )
    monkeypatch.setattr(
        _base.cc,
        "claim_queue",
        lambda *args, **kwargs: [
            {
                "handle": "one",
                "_queue_lock_token": "lock",
                "_queue_from_status": "qualified",
            },
            {
                "handle": "two",
                "_queue_lock_token": "lock",
                "_queue_from_status": "qualified",
            },
        ],
    )
    monkeypatch.setattr(
        _base.cc,
        "refresh_queue_locks",
        lambda rows: (rows, []),
    )
    monkeypatch.setattr(
        _base.cc,
        "release_queue_locks",
        lambda rows: 0,
    )
    monkeypatch.setattr(_base.cc, "advance", lambda *args, **kwargs: True)
    monkeypatch.setattr(_base.cc, "status_dist", lambda batch: {})

    result = _base.run_browser_stage(
        "qualified",
        "BATCH",
        0,
        False,
        lambda pg, cand: ("advance", "collected", cand),
        per_account=1,
        account_offset=1,
        account_count=1,
    )

    assert result == {"advance": 2, "reject": 0, "error": 0}
    assert opened == ["b", "b"]


def test_invalid_subpool_stops_before_claiming_database(monkeypatch):
    import browser_collect_v2 as browser

    monkeypatch.setattr(
        browser,
        "load_accounts",
        lambda path: [{"username": "a"}, {"username": "b"}],
    )
    monkeypatch.setattr(
        _base.cc,
        "claim_queue",
        lambda *args, **kwargs: pytest.fail("database queue must not be claimed"),
    )

    result = _base.run_browser_stage(
        "qualified",
        "BATCH",
        0,
        False,
        lambda pg, cand: None,
        account_offset=1,
        account_count=2,
    )

    assert result == {"error": "invalid_account_slice"}


def test_direct_negative_offset_is_rejected():
    with pytest.raises(ValueError, match="非负整数"):
        _base._account_index(3, 0, -1)


def test_stage3_cli_forwards_account_offset_and_rotation(monkeypatch):
    received = {}
    monkeypatch.setattr(
        stage3_collect,
        "load_config",
        lambda: {"pipeline": {"collect_per_account": 4}},
    )
    monkeypatch.setattr(
        stage3_collect,
        "run_browser_stage",
        lambda *args, **kwargs: received.update(kwargs),
    )

    assert stage3_collect.main(
        [
            "--batch-id",
            "BATCH",
            "--account-offset",
            "7",
            "--account-count",
            "2",
            "--account-rotation",
            "9",
        ]
    ) == 0

    assert received["account_offset"] == 7
    assert received["account_count"] == 2
    assert received["account_rotation"] == 9
    assert received["per_account"] == 4


def test_pricing_cli_forwards_account_offset_without_real_io(monkeypatch):
    received = {}
    claimed = stage3_pricing_backfill.ClaimedCandidate(
        "handle", "decided", {"handle": "handle"}, "lock"
    )
    monkeypatch.setattr(
        stage3_pricing_backfill,
        "claim_candidates",
        lambda *args, **kwargs: [claimed],
    )
    monkeypatch.setattr(
        stage3_pricing_backfill,
        "load_config",
        lambda: {"pipeline": {"collect_per_account": 3}},
    )
    monkeypatch.setattr(
        stage3_pricing_backfill,
        "run_backfill",
        lambda *args, **kwargs: (
            received.update(kwargs)
            or {
                "complete": 1,
                "partial": 0,
                "fallback_modash": 0,
                "missing": 0,
                "error": 0,
            }
        ),
    )

    assert stage3_pricing_backfill.main(
        [
            "--batch-id",
            "BATCH",
            "--account-offset",
            "5",
            "--account-count",
            "3",
        ]
    ) == 0

    assert received["account_offset"] == 5
    assert received["account_count"] == 3
    assert received["per_account"] == 3


@pytest.mark.parametrize(
    ("main", "args"),
    [
        (
            stage3_collect.main,
            ["--batch-id", "BATCH", "--account-offset", "-1"],
        ),
        (
            stage3_pricing_backfill.main,
            ["--batch-id", "BATCH", "--account-offset", "-1"],
        ),
        (
            stage3_collect.main,
            ["--batch-id", "BATCH", "--account-count", "-1"],
        ),
        (
            stage3_collect.main,
            ["--batch-id", "BATCH", "--account-rotation", "-1"],
        ),
        (
            stage3_pricing_backfill.main,
            ["--batch-id", "BATCH", "--account-count", "-1"],
        ),
    ],
)
def test_negative_cli_offset_is_rejected_before_work(main, args):
    with pytest.raises(SystemExit) as exc:
        main(args)
    assert exc.value.code == 2
