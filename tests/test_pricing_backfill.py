"""报价补采必须精确选 cohort，且只写 pricing_* 字段。"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from extensions.sop_v2.pipeline import stage3_pricing_backfill as backfill  # noqa: E402


def _db(path: Path, rows: list[dict]) -> Path:
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE creator_profiles (
            handle TEXT PRIMARY KEY,
            status TEXT,
            discovery_batch TEXT,
            client_status TEXT,
            locked_at TEXT,
            stage_updated_at TEXT,
            times_seen INTEGER,
            storefront_status TEXT,
            stage_error TEXT,
            reject_reason TEXT,
            real_er REAL,
            final_pool TEXT,
            stage_json TEXT
        )"""
    )
    for row in rows:
        base = {
            "handle": row["handle"],
            "status": row.get("status", "decided"),
            "discovery_batch": row.get("discovery_batch", "NEW"),
            "client_status": row.get("client_status"),
            "locked_at": row.get("locked_at"),
            "stage_updated_at": row.get(
                "stage_updated_at", "2026-07-01T00:00:00"
            ),
            "times_seen": row.get("times_seen", 1),
            "storefront_status": row.get("storefront_status", "unknown"),
            "stage_error": row.get("stage_error"),
            "reject_reason": row.get("reject_reason"),
            "real_er": row.get("real_er"),
            "final_pool": row.get("final_pool"),
            "stage_json": json.dumps(
                row.get("stage_json", {"handle": row["handle"]}),
                ensure_ascii=False,
            ),
        }
        conn.execute(
            "INSERT INTO creator_profiles VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            tuple(base.values()),
        )
    conn.commit()
    conn.close()
    return path


def _manifest(path: Path, rows: list[tuple[str, str]]) -> Path:
    carryover = [
        {"handle": handle, "origin_batch": origin}
        for handle, origin in rows
    ]
    handles = sorted(handle.lower() for handle, _ in rows)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "next_batch_id": "NEW",
                "source_batches": sorted(
                    {origin for _, origin in rows} | {"NEW"}
                ),
                "counts": {
                    "source_candidates": len(carryover),
                    "client_final": 0,
                    "carryover": len(carryover),
                    "pipeline_retry": 0,
                },
                "retry_handles": [],
                "carryover_handle_set_sha256": hashlib.sha256(
                    "\n".join(handles).encode("utf-8")
                ).hexdigest(),
                "carryover": [
                    {
                        **row,
                        "needs_pipeline_retry": False,
                    }
                    for row in carryover
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def _row(db: Path, handle: str) -> dict:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM creator_profiles WHERE handle=?", (handle,)
        ).fetchone()
        return dict(row)
    finally:
        conn.close()


def test_manifest_and_batch_form_union_and_skip_complete_or_wrong_status(tmp_path):
    db = _db(
        tmp_path / "cache.db",
        [
            {
                "handle": "old_decided",
                "discovery_batch": "OLD",
                "status": "decided",
            },
            {
                "handle": "new_rejected",
                "discovery_batch": "NEW",
                "status": "rejected",
            },
            {
                "handle": "new_complete",
                "discovery_batch": "NEW",
                "stage_json": {
                    "pricing_estimate": {"status": "complete"}
                },
            },
            {
                "handle": "new_qualified",
                "discovery_batch": "NEW",
                "status": "qualified",
            },
        ],
    )
    manifest = _manifest(
        tmp_path / "carryover.json", [("old_decided", "OLD")]
    )

    queue = backfill.claim_candidates(
        db,
        batch_id="NEW",
        manifest_handles=backfill._read_manifest(manifest, "NEW"),
        dry_run=True,
    )

    assert [(item.handle, item.status) for item in queue] == [
        ("new_rejected", "rejected"),
        ("old_decided", "decided"),
    ]
    assert all(_row(db, item.handle)["locked_at"] is None for item in queue)


def test_save_pricing_preserves_every_business_and_comment_field(tmp_path):
    original_stage = {
        "handle": "preserve_me",
        "sampled_posts": [{"code": "COMMENT-EVIDENCE"}],
        "real_er": 2.5,
        "final_pool": "Review",
        "route_reasons": ["modash_core_missing"],
    }
    db = _db(
        tmp_path / "cache.db",
        [
            {
                "handle": "preserve_me",
                "status": "rejected",
                "reject_reason": "off_niche",
                "stage_error": "historical_diagnostic",
                "real_er": 2.5,
                "final_pool": "Review",
                "stage_json": original_stage,
            }
        ],
    )
    queue = backfill.claim_candidates(db, batch_id="NEW")
    assert len(queue) == 1
    claimed = queue[0]
    enriched = dict(claimed.cand)
    enriched.update(
        {
            "codes": ["/reel/new-grid-ref/"],
            "pricing_reel_samples": [{"code": "/reel/abc/", "play_count": 1000}],
            "pricing_captured_at": "2026-07-28T12:00:00+08:00",
            "pricing_estimate": {
                "status": "partial",
                "quote_usd": {"default": 35.0},
            },
        }
    )

    assert backfill.save_pricing(db, claimed, enriched) is True
    after = _row(db, "preserve_me")
    stage = json.loads(after["stage_json"])

    assert after["status"] == "rejected"
    assert after["reject_reason"] == "off_niche"
    assert after["stage_error"] == "historical_diagnostic"
    assert after["real_er"] == 2.5
    assert after["final_pool"] == "Review"
    assert after["locked_at"] is None
    assert stage["sampled_posts"] == [{"code": "COMMENT-EVIDENCE"}]
    assert stage["route_reasons"] == ["modash_core_missing"]
    assert "codes" not in stage
    assert stage["pricing_estimate"]["status"] == "partial"


def test_save_pricing_refuses_customer_finalized_candidate(tmp_path):
    db = _db(
        tmp_path / "cache.db",
        [{"handle": "customer_final", "status": "decided"}],
    )
    claimed = backfill.claim_candidates(db, batch_id="NEW")[0]
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE creator_profiles SET client_status='approved' "
        "WHERE handle='customer_final'"
    )
    conn.commit()
    conn.close()

    assert (
        backfill.save_pricing(
            db,
            claimed,
            {
                "pricing_estimate": {"status": "complete"},
                "pricing_reel_samples": [{"code": "/reel/SHOULD_NOT_WRITE/"}],
            },
        )
        is False
    )
    stage = json.loads(_row(db, "customer_final")["stage_json"])
    assert "pricing_estimate" not in stage
    assert "pricing_reel_samples" not in stage


def test_release_claim_does_not_clear_other_workers_lock(tmp_path):
    db = _db(
        tmp_path / "cache.db",
        [{"handle": "one", "status": "decided"}],
    )
    claimed = backfill.claim_candidates(db, batch_id="NEW")[0]
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE creator_profiles SET locked_at='other-worker' WHERE handle='one'"
    )
    conn.commit()
    conn.close()

    backfill.release_claim(db, claimed)

    assert _row(db, "one")["locked_at"] == "other-worker"


def test_resume_only_reclaims_stale_locks(tmp_path):
    db = _db(
        tmp_path / "cache.db",
        [
            {
                "handle": "stale",
                "locked_at": "2020-01-01T00:00:00",
            },
            {
                "handle": "fresh",
                "locked_at": "2999-01-01T00:00:00",
            },
        ],
    )

    assert backfill.claim_candidates(
        db, batch_id="NEW", dry_run=True
    ) == []
    resumed = backfill.claim_candidates(
        db, batch_id="NEW", resume=True, dry_run=True
    )

    assert [item.handle for item in resumed] == ["stale"]


def test_refresh_claims_renews_tokens_and_drops_customer_final(tmp_path):
    db = _db(
        tmp_path / "cache.db",
        [
            {"handle": "keep", "status": "decided"},
            {"handle": "drop", "status": "decided"},
        ],
    )
    claims = backfill.claim_candidates(db, batch_id="NEW")
    old_tokens = {item.handle: item.lock_token for item in claims}
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE creator_profiles SET client_status='rejected' WHERE handle='drop'"
    )
    conn.commit()
    conn.close()

    refreshed, lost = backfill.refresh_claims(db, claims)

    assert [item.handle for item in refreshed] == ["keep"]
    assert [item.handle for item in lost] == ["drop"]
    assert refreshed[0].lock_token != old_tokens["keep"]
    assert _row(db, "keep")["locked_at"] == refreshed[0].lock_token
    assert _row(db, "drop")["locked_at"] is None


def test_manifest_origin_drift_and_client_final_stop_before_lock(tmp_path):
    db = _db(
        tmp_path / "cache.db",
        [
            {
                "handle": "wrong_origin",
                "discovery_batch": "ACTUAL",
            },
            {
                "handle": "already_final",
                "discovery_batch": "OLD",
                "client_status": "approved",
            },
        ],
    )
    handles = {"wrong_origin": "OLD", "already_final": "OLD"}

    with pytest.raises(backfill.PricingBackfillError, match="origin_mismatch"):
        backfill.claim_candidates(db, manifest_handles=handles)

    assert _row(db, "wrong_origin")["locked_at"] is None
    assert _row(db, "already_final")["locked_at"] is None


def test_manifest_full_stage4_contract_rejects_truncated_cohort(tmp_path):
    path = _manifest(tmp_path / "carryover.json", [("one", "OLD")])
    payload = json.loads(path.read_text())
    payload["counts"]["source_candidates"] = 2
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(
        backfill.PricingBackfillError,
        match="source_candidates",
    ):
        backfill._read_manifest(path, "NEW")


def test_partial_or_missing_attempt_is_not_recollected_implicitly(tmp_path):
    db = _db(
        tmp_path / "cache.db",
        [
            {
                "handle": "partial",
                "stage_json": {
                    "pricing_estimate": {"status": "partial"}
                },
            },
            {
                "handle": "missing",
                "stage_json": {
                    "pricing_estimate": {"status": "missing"}
                },
            },
        ],
    )

    assert (
        backfill.claim_candidates(db, batch_id="NEW", dry_run=True)
        == []
    )


def test_retry_incomplete_selects_three_statuses_but_never_complete(tmp_path):
    db = _db(
        tmp_path / "cache.db",
        [
            {
                "handle": "missing",
                "stage_json": {"pricing_estimate": {"status": "missing"}},
            },
            {
                "handle": "partial",
                "stage_json": {"pricing_estimate": {"status": "partial"}},
            },
            {
                "handle": "fallback",
                "stage_json": {
                    "pricing_estimate": {"status": "fallback_modash"}
                },
            },
            {
                "handle": "complete",
                "stage_json": {"pricing_estimate": {"status": "complete"}},
            },
        ],
    )

    queue = backfill.claim_candidates(
        db,
        batch_id="NEW",
        retry_incomplete=True,
        dry_run=True,
    )

    assert {item.handle for item in queue} == {
        "missing",
        "partial",
        "fallback",
    }
    assert "complete" not in {item.handle for item in queue}


def test_handles_file_supports_lines_and_json_and_rejects_duplicates(tmp_path):
    text = tmp_path / "handles.txt"
    text.write_text("# pricing retry\n@One\n\n two \n", encoding="utf-8")
    assert backfill._read_handles_file(text) == {"one", "two"}

    payload = tmp_path / "handles.json"
    payload.write_text(
        json.dumps({"handles": ["@Three", "FOUR"]}), encoding="utf-8"
    )
    assert backfill._read_handles_file(payload) == {"three", "four"}

    payload.write_text(json.dumps(["same", "@SAME"]), encoding="utf-8")
    with pytest.raises(backfill.PricingBackfillError, match="重复"):
        backfill._read_handles_file(payload)


def test_handles_allowlist_is_exact_intersection_with_batch(tmp_path):
    db = _db(
        tmp_path / "cache.db",
        [
            {
                "handle": "wanted",
                "stage_json": {"pricing_estimate": {"status": "missing"}},
            },
            {
                "handle": "not_listed",
                "stage_json": {"pricing_estimate": {"status": "missing"}},
            },
        ],
    )

    queue = backfill.claim_candidates(
        db,
        batch_id="NEW",
        allowed_handles={"wanted"},
        retry_incomplete=True,
        dry_run=True,
    )

    assert [item.handle for item in queue] == ["wanted"]


def test_handles_allowlist_outside_scope_or_customer_final_stops(tmp_path):
    db = _db(
        tmp_path / "cache.db",
        [
            {
                "handle": "other_batch",
                "discovery_batch": "OTHER",
                "stage_json": {"pricing_estimate": {"status": "missing"}},
            },
            {
                "handle": "customer_final",
                "client_status": "approved",
                "stage_json": {"pricing_estimate": {"status": "missing"}},
            },
        ],
    )

    with pytest.raises(backfill.PricingBackfillError, match="outside_scope"):
        backfill.claim_candidates(
            db,
            batch_id="NEW",
            allowed_handles={"other_batch"},
            retry_incomplete=True,
            dry_run=True,
        )
    with pytest.raises(backfill.PricingBackfillError, match="client_final"):
        backfill.claim_candidates(
            db,
            allowed_handles={"customer_final"},
            retry_incomplete=True,
            dry_run=True,
        )


def test_handles_only_scope_can_retry_exact_incomplete_set(tmp_path):
    db = _db(
        tmp_path / "cache.db",
        [
            {
                "handle": "missing",
                "stage_json": {"pricing_estimate": {"status": "missing"}},
            },
            {
                "handle": "complete",
                "stage_json": {"pricing_estimate": {"status": "complete"}},
            },
        ],
    )

    queue = backfill.claim_candidates(
        db,
        allowed_handles={"missing", "complete"},
        retry_incomplete=True,
        dry_run=True,
    )

    assert [item.handle for item in queue] == ["missing"]


def test_dry_run_main_never_starts_browser_or_writes_db(
    tmp_path, monkeypatch, capsys
):
    db = _db(
        tmp_path / "cache.db",
        [{"handle": "dry", "status": "collected"}],
    )
    called = []
    monkeypatch.setattr(
        backfill,
        "run_backfill",
        lambda *args, **kwargs: called.append(True),
    )

    assert backfill.main(
        ["--batch-id", "NEW", "--db", str(db), "--dry-run"]
    ) == 0

    assert called == []
    assert _row(db, "dry")["locked_at"] is None
    assert "数据库未修改，浏览器未启动" in capsys.readouterr().out


def test_retry_dry_run_main_displays_exact_allowlist_without_browser(
    tmp_path, monkeypatch, capsys
):
    db = _db(
        tmp_path / "cache.db",
        [
            {
                "handle": "retry_me",
                "stage_json": {"pricing_estimate": {"status": "partial"}},
            },
            {
                "handle": "skip_complete",
                "stage_json": {"pricing_estimate": {"status": "complete"}},
            },
        ],
    )
    handles = tmp_path / "handles.txt"
    handles.write_text("retry_me\nskip_complete\n", encoding="utf-8")
    called = []
    monkeypatch.setattr(
        backfill,
        "run_backfill",
        lambda *args, **kwargs: called.append(True),
    )

    assert backfill.main(
        [
            "--handles-file",
            str(handles),
            "--retry-incomplete",
            "--db",
            str(db),
            "--dry-run",
        ]
    ) == 0

    output = capsys.readouterr().out
    assert called == []
    assert "@retry_me [decided]" in output
    assert "@skip_complete" not in output
    assert _row(db, "retry_me")["locked_at"] is None
