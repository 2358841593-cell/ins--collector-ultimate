"""Creator-cache retry fairness and source-hit accounting regressions."""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from extensions.sop_v2 import creator_cache as cc  # noqa: E402
import browser_collect_v2 as browser  # noqa: E402


def _qualified_rows(tmp_path: Path, monkeypatch, handles: list[str]) -> Path:
    db = tmp_path / "cache.db"
    monkeypatch.setattr(cc, "DB", db)
    cc.seed_handles(
        [{"handle": handle} for handle in handles],
        batch_id="BATCH",
    )
    for handle in handles:
        cc.advance(handle, "qualified", {"handle": handle})
    return db


def test_unattempted_qualified_rows_precede_high_value_failures(
    tmp_path, monkeypatch
):
    db = _qualified_rows(tmp_path, monkeypatch, ["fresh", "failed"])
    with sqlite3.connect(db) as conn:
        conn.execute(
            """UPDATE creator_profiles
               SET stage_error='deep_incomplete',
                   stage_updated_at='2026-01-01T00:00:00',
                   times_seen=999,
                   storefront_status='confirmed_yes'
               WHERE handle='failed'"""
        )
        conn.execute(
            """UPDATE creator_profiles
               SET stage_error=NULL,
                   stage_updated_at='2026-08-10T00:00:00',
                   times_seen=1,
                   storefront_status='confirmed_no'
               WHERE handle='fresh'"""
        )

    claimed = cc.claim_queue("qualified", 1, "BATCH", stale_minutes=None)

    assert [row["handle"] for row in claimed] == ["fresh"]


def test_failed_rows_rotate_by_oldest_attempt_time(tmp_path, monkeypatch):
    db = _qualified_rows(tmp_path, monkeypatch, ["failed_old", "failed_new"])
    with sqlite3.connect(db) as conn:
        conn.execute(
            """UPDATE creator_profiles
               SET stage_error='old failure',
                   stage_updated_at='2026-08-10T10:00:00',
                   times_seen=1,
                   storefront_status='confirmed_no'
               WHERE handle='failed_old'"""
        )
        conn.execute(
            """UPDATE creator_profiles
               SET stage_error='new failure',
                   stage_updated_at='2026-08-10T11:00:00',
                   times_seen=999,
                   storefront_status='confirmed_yes'
               WHERE handle='failed_new'"""
        )

    first = cc.claim_queue("qualified", 1, "BATCH", stale_minutes=None)[0]
    assert first["handle"] == "failed_old"

    monkeypatch.setattr(cc, "_now_iso", lambda: "2026-08-10T12:00:00")
    assert cc.mark_error(
        "failed_old",
        "failed again",
        first,
        expected_status="qualified",
        lock_token=first["_queue_lock_token"],
    )

    second = cc.claim_queue("qualified", 1, "BATCH", stale_minutes=None)[0]
    assert second["handle"] == "failed_new"
    with sqlite3.connect(db) as conn:
        attempt_at, times_seen = conn.execute(
            "SELECT stage_updated_at,times_seen FROM creator_profiles "
            "WHERE handle='failed_old'"
        ).fetchone()
    assert attempt_at == "2026-08-10T12:00:00"
    assert times_seen == 1


def test_non_cas_mark_error_updates_attempt_time_without_incrementing_times_seen(
    tmp_path, monkeypatch
):
    db = _qualified_rows(tmp_path, monkeypatch, ["failed"])
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE creator_profiles SET stage_updated_at='2026-08-10T10:00:00', "
            "times_seen=7 WHERE handle='failed'"
        )

    monkeypatch.setattr(cc, "_now_iso", lambda: "2026-08-10T13:00:00")
    assert cc.mark_error("failed", "retryable", {"handle": "failed"})

    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT stage_error,stage_updated_at,times_seen "
            "FROM creator_profiles WHERE handle='failed'"
        ).fetchone()
    assert row == ("retryable", "2026-08-10T13:00:00", 7)


def test_upsert_can_skip_incrementing_times_seen(tmp_path, monkeypatch):
    db = tmp_path / "cache.db"
    monkeypatch.setattr(cc, "DB", db)
    cc.upsert({"handle": "creator", "biography": "shallow"})
    cc.upsert({"handle": "creator", "biography": "second source"})

    cc.upsert(
        {"handle": "creator", "biography": "deep refresh"},
        increment_times_seen=False,
    )

    with sqlite3.connect(db) as conn:
        times_seen, biography = conn.execute(
            "SELECT times_seen,biography FROM creator_profiles WHERE handle='creator'"
        ).fetchone()
    assert times_seen == 2
    assert biography == "deep refresh"


def test_deep_cache_refresh_does_not_increment_source_hits(tmp_path, monkeypatch):
    db = tmp_path / "cache.db"
    monkeypatch.setattr(cc, "DB", db)
    cc.upsert({"handle": "creator", "biography": "discovery"})

    browser._cache_save({"handle": "creator", "biography": "deep evidence"})

    with sqlite3.connect(db) as conn:
        times_seen, biography = conn.execute(
            "SELECT times_seen,biography FROM creator_profiles WHERE handle='creator'"
        ).fetchone()
    assert times_seen == 1
    assert biography == "deep evidence"
