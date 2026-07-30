"""Stage queue claims are atomic and only resume may reclaim stale locks."""
from __future__ import annotations

import json
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from extensions.sop_v2 import creator_cache as cc  # noqa: E402
from extensions.sop_v2.pipeline import _base  # noqa: E402


def _seed(tmp_path: Path, monkeypatch, count: int = 3) -> Path:
    db = tmp_path / "cache.db"
    monkeypatch.setattr(cc, "DB", db)
    cc.seed_handles(
        [{"handle": f"creator_{index}"} for index in range(count)],
        batch_id="BATCH",
    )
    return db


def test_non_resume_never_reclaims_an_active_lock(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch, 1)

    first = cc.claim_queue("seed", 1, "BATCH", stale_minutes=None)
    second = cc.claim_queue("seed", 1, "BATCH", stale_minutes=None)

    assert [row["handle"] for row in first] == ["creator_0"]
    assert second == []


def test_resume_can_reclaim_only_a_stale_lock(tmp_path, monkeypatch):
    db = _seed(tmp_path, monkeypatch, 1)
    assert cc.claim_queue("seed", 1, "BATCH", stale_minutes=None)
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE creator_profiles SET locked_at='2020-01-01T00:00:00' "
        "WHERE handle='creator_0'"
    )
    conn.commit()
    conn.close()

    resumed = cc.claim_queue("seed", 1, "BATCH", stale_minutes=30)

    assert [row["handle"] for row in resumed] == ["creator_0"]


def test_concurrent_workers_claim_distinct_rows(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch, 2)

    def claim_one():
        return cc.claim_queue("seed", 1, "BATCH", stale_minutes=None)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: claim_one(), range(2)))

    handles = [
        row["handle"]
        for result in results
        for row in result
    ]
    assert sorted(handles) == ["creator_0", "creator_1"]


def test_queue_lease_refresh_uses_compare_and_swap(tmp_path, monkeypatch):
    db = _seed(tmp_path, monkeypatch, 2)
    claims = cc.claim_queue("seed", 2, "BATCH", stale_minutes=None)
    original = {
        row["handle"]: row["_queue_lock_token"]
        for row in claims
    }
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE creator_profiles SET locked_at='other-worker' "
        "WHERE handle='creator_1'"
    )
    conn.commit()
    conn.close()

    kept, lost = cc.refresh_queue_locks(claims)

    assert [row["handle"] for row in kept] == ["creator_0"]
    assert [row["handle"] for row in lost] == ["creator_1"]
    assert kept[0]["_queue_lock_token"] != original["creator_0"]


def test_release_queue_locks_only_releases_owned_token(tmp_path, monkeypatch):
    db = _seed(tmp_path, monkeypatch, 2)
    claims = cc.claim_queue("seed", 2, "BATCH", stale_minutes=None)
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE creator_profiles SET locked_at='other-worker' "
        "WHERE handle='creator_1'"
    )
    conn.commit()
    conn.close()

    assert cc.release_queue_locks(claims) == 1

    conn = sqlite3.connect(db)
    rows = dict(
        conn.execute(
            "SELECT handle,locked_at FROM creator_profiles ORDER BY handle"
        ).fetchall()
    )
    conn.close()
    assert rows == {
        "creator_0": None,
        "creator_1": "other-worker",
    }


def test_error_verdict_atomically_persists_partial_evidence_and_keeps_status(
    tmp_path, monkeypatch
):
    db = _seed(tmp_path, monkeypatch, 1)
    cc.advance(
        "creator_0",
        "qualified",
        {"handle": "creator_0", "biography": "existing profile evidence"},
    )
    claim = cc.claim_queue("qualified", 1, "BATCH", stale_minutes=None)[0]
    partial = {
        "handle": "creator_0",
        "biography": "existing profile evidence",
        "sampled_posts": [{"url": "https://www.instagram.com/p/OK1/"}],
        "deep_failed_posts": ["https://www.instagram.com/p/FAILED1/"],
        "deep_metric_missing_posts": [
            "https://www.instagram.com/p/METRIC1/"
        ],
        "comment_failed_posts": [
            "https://www.instagram.com/p/COMMENT1/"
        ],
        "intent_posts": [
            {
                "post_url": "https://www.instagram.com/p/OK1/",
                "intent_comments": [{"username": "buyer", "text": "link?"}],
            }
        ],
        "evidence_dir": "data/evidence/BATCH/creator_0",
    }

    kind = _base.apply_verdict(
        "creator_0",
        ("error", "deep_incomplete:帖子读取失败(1 帖)", partial),
        claim=claim,
    )

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT status,stage_error,locked_at,stage_json,client_status "
        "FROM creator_profiles WHERE handle='creator_0'"
    ).fetchone()
    conn.close()
    saved = json.loads(row["stage_json"])
    assert kind == "error"
    assert row["status"] == "qualified"
    assert row["stage_error"].startswith("deep_incomplete:")
    assert row["locked_at"] is None
    assert row["client_status"] is None
    assert saved["deep_failed_posts"] == partial["deep_failed_posts"]
    assert (
        saved["deep_metric_missing_posts"]
        == partial["deep_metric_missing_posts"]
    )
    assert saved["comment_failed_posts"] == partial["comment_failed_posts"]
    assert saved["sampled_posts"] == partial["sampled_posts"]
    assert saved["intent_posts"] == partial["intent_posts"]
    assert saved["evidence_dir"] == partial["evidence_dir"]
    assert "_queue_lock_token" not in saved
    assert "_queue_from_status" not in saved


def test_partial_error_write_obeys_lock_compare_and_swap(
    tmp_path, monkeypatch
):
    db = _seed(tmp_path, monkeypatch, 1)
    original = {"handle": "creator_0", "marker": "original"}
    cc.advance("creator_0", "qualified", original)
    claim = cc.claim_queue("qualified", 1, "BATCH", stale_minutes=None)[0]
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE creator_profiles SET locked_at='other-worker' "
        "WHERE handle='creator_0'"
    )
    conn.commit()
    conn.close()

    written = cc.mark_error(
        "creator_0",
        "deep_incomplete:stale worker",
        {"handle": "creator_0", "marker": "stale"},
        expected_status="qualified",
        lock_token=claim["_queue_lock_token"],
    )

    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT stage_error,locked_at,stage_json FROM creator_profiles "
        "WHERE handle='creator_0'"
    ).fetchone()
    conn.close()
    assert written is False
    assert row[0] is None
    assert row[1] == "other-worker"
    assert json.loads(row[2]) == original


def test_partial_error_write_never_overwrites_client_final(
    tmp_path, monkeypatch
):
    db = _seed(tmp_path, monkeypatch, 1)
    original = {"handle": "creator_0", "marker": "client-final"}
    cc.advance("creator_0", "qualified", original)
    claim = cc.claim_queue("qualified", 1, "BATCH", stale_minutes=None)[0]
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE creator_profiles SET client_status='approved', tier=2 "
        "WHERE handle='creator_0'"
    )
    conn.commit()
    conn.close()

    written = cc.mark_error(
        "creator_0",
        "deep_incomplete:late collector",
        {"handle": "creator_0", "marker": "late"},
        expected_status="qualified",
        lock_token=claim["_queue_lock_token"],
    )

    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT status,client_status,tier,stage_error,locked_at,stage_json "
        "FROM creator_profiles WHERE handle='creator_0'"
    ).fetchone()
    conn.close()
    assert written is False
    assert row[0:3] == ("qualified", "approved", 2)
    assert row[3] is None
    assert row[4] is None
    assert json.loads(row[5]) == original
