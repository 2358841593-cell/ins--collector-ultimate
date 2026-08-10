import json
import sqlite3
import sys

import pytest

from extensions.sop_v2 import round_contract as round_contract_mod
from extensions.sop_v2.pipeline import prepare_next_round as prepare
from extensions.sop_v2.pipeline.prepare_next_round import ManifestError, build_manifest


def _write_json(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def _db(path):
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE creator_profiles ("
        "handle TEXT PRIMARY KEY, discovery_batch TEXT, status TEXT, "
        "stage_error TEXT, stage_json TEXT)"
    )
    conn.executemany(
        "INSERT INTO creator_profiles VALUES (?,?,?,?,?)",
        [
            ("approved_one", "OLD", "decided", None, "{}"),
            (
                "pending_one",
                "OLD",
                "decided",
                None,
                json.dumps(
                    {
                        "comments_read": True,
                        "comments_analyzed": 20,
                        "valid_comments": 20,
                        "real_er": 1.2,
                        "sampled_posts": [
                            {"like_count": 10, "comment_count": 2}
                            for _ in range(10)
                        ],
                    }
                ),
            ),
            ("unseen_one", "OLD", "qualified", "proxy_throttled", "{}"),
        ],
    )
    conn.commit()
    conn.close()
    return path


def _source(path):
    return _write_json(
        path,
        {
            "manifest": {"batch_id": "OLD"},
            "candidates": [
                {"handle": "approved_one", "final_pool": "Review", "ai_vetting_score": 5.0},
                {"handle": "pending_one", "final_pool": "Review", "ai_vetting_score": 4.0},
                {"handle": "unseen_one", "final_pool": "Exclude", "ai_vetting_score": None},
            ],
        },
    )


def test_build_manifest_preserves_origin_and_selects_retry(tmp_path):
    source = _source(tmp_path / "source.json")
    feedback = _write_json(
        tmp_path / "feedback.json",
        {
            "batch": "OLD",
            "decisions": [
                {"handle": "approved_one", "verdict": "合适", "reason": ""},
                {"handle": "pending_one", "verdict": "待定", "reason": "再看"},
            ],
        },
    )
    manifest = build_manifest(
        source, feedback, "NEW", _db(tmp_path / "cache.db"), "2026-01-01T00:00:00+0000"
    )

    assert manifest["counts"] == {
        "source_candidates": 3,
        "client_final": 1,
        "carryover": 2,
        "pipeline_retry": 1,
    }
    assert manifest["retry_handles"] == ["unseen_one"]
    by_handle = {row["handle"]: row for row in manifest["carryover"]}
    assert by_handle["pending_one"]["carryover_reason"] == "client_pending"
    assert by_handle["pending_one"]["client_reason"] == "再看"
    assert by_handle["pending_one"]["manual_recheck_required"] is True
    assert by_handle["pending_one"]["needs_pipeline_retry"] is False
    assert by_handle["unseen_one"]["carryover_reason"] == "client_unreviewed"
    assert by_handle["unseen_one"]["client_reason"] is None
    assert by_handle["unseen_one"]["manual_recheck_required"] is False
    assert {row["origin_batch"] for row in manifest["carryover"]} == {"OLD"}


@pytest.mark.parametrize(
    ("mode", "expected_handles", "expected_excluded"),
    [
        ("new_only", [], 2),
        ("retry_only", ["unseen_one"], 1),
        ("unresolved", ["pending_one", "unseen_one"], 0),
    ],
)
def test_contract_carryover_modes_select_exact_cohort(
    tmp_path, mode, expected_handles, expected_excluded
):
    source = _source(tmp_path / "source.json")
    feedback = _write_json(
        tmp_path / "feedback.json",
        {
            "batch": "OLD",
            "decisions": [
                {"handle": "approved_one", "verdict": "合适", "reason": ""},
                {"handle": "pending_one", "verdict": "待定", "reason": "再看"},
            ],
        },
    )
    manifest = build_manifest(
        source,
        feedback,
        "NEW",
        _db(tmp_path / "cache.db"),
        carryover_mode=mode,
        round_contract_sha256="a" * 64,
    )

    assert manifest["carryover_mode"] == mode
    assert manifest["round_contract_sha256"] == "a" * 64
    assert [row["handle"] for row in manifest["carryover"]] == expected_handles
    assert manifest["counts"].get("mode_excluded", 0) == expected_excluded
    assert manifest["source_batches"] == (["NEW"] if not expected_handles else ["NEW", "OLD"])
    if mode == "retry_only":
        assert manifest["retry_handles"] == expected_handles


def test_prepare_cli_consumes_and_binds_round_contract(tmp_path, monkeypatch):
    source = _source(tmp_path / "source.json")
    feedback = _write_json(
        tmp_path / "feedback.json",
        {
            "batch": "OLD",
            "decisions": [
                {"handle": "approved_one", "verdict": "合适", "reason": ""},
                {"handle": "pending_one", "verdict": "待定", "reason": "later"},
            ],
        },
    )
    contract = round_contract_mod.build_round_contract(
        batch_id="NEW",
        campaign_track="paid",
        carryover_mode="retry_only",
        created_at="2026-08-10T12:00:00+0800",
    )
    contract_path = tmp_path / "round.json"
    round_contract_mod.write_round_contract(contract_path, contract)
    out_path = tmp_path / "carryover.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "prepare_next_round",
            "--source-decisions", str(source),
            "--feedback", str(feedback),
            "--next-batch", "NEW",
            "--db", str(_db(tmp_path / "cache.db")),
            "--round-contract", str(contract_path),
            "--require-round-contract",
            "--out", str(out_path),
        ],
    )

    assert prepare.main() == 0
    result = json.loads(out_path.read_text(encoding="utf-8"))
    assert result["carryover_mode"] == "retry_only"
    assert result["round_contract_sha256"] == round_contract_mod.round_contract_sha256(
        contract_path
    )
    assert result["retry_handles"] == ["unseen_one"]


def test_reason_only_feedback_is_unresolved(tmp_path):
    source = _source(tmp_path / "source.json")
    feedback = _write_json(
        tmp_path / "feedback.json",
        {
            "batch": "OLD",
            "decisions": [{"handle": "pending_one", "verdict": "", "reason": "needs review"}],
        },
    )
    manifest = build_manifest(source, feedback, "NEW", _db(tmp_path / "cache.db"))
    assert manifest["feedback"]["undecided"] == 1
    assert manifest["counts"]["carryover"] == 3
    by_handle = {row["handle"]: row for row in manifest["carryover"]}
    assert by_handle["pending_one"]["client_reason"] == "needs review"
    assert by_handle["pending_one"]["manual_recheck_required"] is True
    assert by_handle["pending_one"]["needs_pipeline_retry"] is False
    assert "pending_one" not in manifest["retry_handles"]


def test_pending_without_reason_does_not_claim_manual_recheck(tmp_path):
    source = _source(tmp_path / "source.json")
    feedback = _write_json(
        tmp_path / "feedback.json",
        {
            "batch": "OLD",
            "decisions": [
                {"handle": "pending_one", "verdict": "待定", "reason": "  "}
            ],
        },
    )

    manifest = build_manifest(source, feedback, "NEW", _db(tmp_path / "cache.db"))
    by_handle = {row["handle"]: row for row in manifest["carryover"]}
    assert by_handle["pending_one"]["client_reason"] is None
    assert by_handle["pending_one"]["manual_recheck_required"] is False
    assert by_handle["pending_one"]["needs_pipeline_retry"] is False


def test_decided_silent_incomplete_items_are_pipeline_retries(tmp_path):
    source = _write_json(
        tmp_path / "source.json",
        {
            "manifest": {"batch_id": "OLD"},
            "candidates": [
                {"handle": "posts_failed"},
                {"handle": "comments_failed"},
                {"handle": "er_failed"},
                {"handle": "approved_incomplete"},
                {"handle": "complete"},
            ],
        },
    )
    feedback = _write_json(
        tmp_path / "feedback.json",
        {
            "batch": "OLD",
            "decisions": [
                {"handle": "approved_incomplete", "verdict": "合适", "reason": ""},
                {"handle": "complete", "verdict": "待定", "reason": "review later"}
            ],
        },
    )
    db_path = tmp_path / "cache.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE creator_profiles ("
        "handle TEXT PRIMARY KEY, discovery_batch TEXT, status TEXT, "
        "stage_error TEXT, stage_json TEXT)"
    )
    rows = [
        (
            "posts_failed", "OLD", "decided", None,
            {
                "comments_read": True,
                "codes": ["/p/a/", "/p/b/"],
                "sampled_posts": [],
            },
        ),
        (
            "comments_failed", "OLD", "decided", None,
            {
                "comments_read": True,
                "sampled_posts": [{"comment_count": 8, "like_count": None}],
                "comments_analyzed": 0,
                "real_er": 1.2,
            },
        ),
        (
            "er_failed", "OLD", "decided", None,
            {
                "comments_read": True,
                "sampled_posts": [{"comment_count": 0, "like_count": 42}],
                "comments_analyzed": 0,
                "real_er": None,
            },
        ),
        (
            "approved_incomplete", "OLD", "decided", None,
            {
                "comments_read": True,
                "codes": ["/p/approved-but-incomplete/"],
                "sampled_posts": [],
            },
        ),
        (
            "complete", "OLD", "decided", None,
            {
                "comments_read": True,
                "sampled_posts": [
                    {"comment_count": 2, "like_count": 42} for _ in range(10)
                ],
                "comments_analyzed": 20,
                "valid_comments": 20,
                "real_er": 1.1,
            },
        ),
    ]
    conn.executemany(
        "INSERT INTO creator_profiles VALUES (?,?,?,?,?)",
        [(h, batch, status, err, json.dumps(stage))
         for h, batch, status, err, stage in rows],
    )
    conn.commit()
    conn.close()

    manifest = build_manifest(source, feedback, "NEW", db_path)
    assert manifest["counts"]["pipeline_retry"] == 3
    assert manifest["retry_handles"] == [
        "posts_failed", "comments_failed", "er_failed"
    ]
    by_handle = {row["handle"]: row for row in manifest["carryover"]}
    assert any(
        "帖子覆盖不足" in reason
        for reason in by_handle["posts_failed"]["incomplete_reasons"]
    )
    assert any(
        "评论抽取失败" in reason
        for reason in by_handle["comments_failed"]["incomplete_reasons"]
    )
    assert "旧版实算 ER 缺失" in by_handle["er_failed"]["incomplete_reasons"]
    assert by_handle["complete"]["needs_pipeline_retry"] is False
    assert "approved_incomplete" not in by_handle
    assert "approved_incomplete" not in manifest["retry_handles"]


def test_mixed_review_batch_preserves_immutable_creator_origins(tmp_path):
    source = _write_json(
        tmp_path / "source.json",
        {
            "manifest": {"batch_id": "REVIEW4"},
            "candidates": [
                {"handle": "from_old", "_discovery_batch": "ORIGIN3"},
                # No per-candidate origin: fall back to the review batch.
                {"handle": "from_review"},
            ],
        },
    )
    feedback = _write_json(
        tmp_path / "feedback.json",
        {
            "batch": "REVIEW4",
            "decisions": [
                {"handle": "from_review", "verdict": "待定", "reason": "later"}
            ],
        },
    )
    db_path = tmp_path / "cache.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE creator_profiles ("
        "handle TEXT PRIMARY KEY, discovery_batch TEXT, status TEXT, "
        "stage_error TEXT, stage_json TEXT)"
    )
    conn.executemany(
        "INSERT INTO creator_profiles VALUES (?,?,?,?,?)",
        [
            ("from_old", "ORIGIN3", "decided", None, "{}"),
            ("from_review", "REVIEW4", "decided", None, "{}"),
        ],
    )
    conn.commit()
    conn.close()

    manifest = build_manifest(source, feedback, "REVIEW5", db_path)
    origins = {row["handle"]: row["origin_batch"] for row in manifest["carryover"]}
    assert origins == {"from_old": "ORIGIN3", "from_review": "REVIEW4"}
    assert manifest["source_batches"] == ["ORIGIN3", "REVIEW4", "REVIEW5"]


def test_source_origin_must_match_explicit_database(tmp_path, monkeypatch):
    source = _write_json(
        tmp_path / "source.json",
        {
            "manifest": {"batch_id": "REVIEW4"},
            "candidates": [{"handle": "from_old", "_discovery_batch": "ORIGIN3"}],
        },
    )
    feedback = _write_json(
        tmp_path / "feedback.json",
        {
            "batch": "REVIEW4",
            "decisions": [{"handle": "from_old", "verdict": "待定", "reason": ""}],
        },
    )
    explicit_db = tmp_path / "explicit.db"
    conn = sqlite3.connect(explicit_db)
    conn.execute(
        "CREATE TABLE creator_profiles ("
        "handle TEXT PRIMARY KEY, discovery_batch TEXT, status TEXT, "
        "stage_error TEXT, stage_json TEXT)"
    )
    conn.execute(
        "INSERT INTO creator_profiles VALUES (?,?,?,?,?)",
        ("from_old", "WRONG_ORIGIN", "decided", None, "{}"),
    )
    conn.commit()
    conn.close()

    # A different global path must never be consulted by build_manifest.
    monkeypatch.setattr(prepare.cc, "DB", tmp_path / "must-not-be-used.db")
    with pytest.raises(ManifestError, match="origin mismatch"):
        build_manifest(source, feedback, "REVIEW5", explicit_db)


@pytest.mark.parametrize(
    "feedback",
    [
        {"batch": "WRONG", "decisions": [{"handle": "approved_one", "verdict": "合适"}]},
        {
            "batch": "OLD",
            "decisions": [
                {"handle": "approved_one", "verdict": "合适"},
                {"handle": "@APPROVED_ONE", "verdict": "不合适"},
            ],
        },
        {"batch": "OLD", "decisions": [{"handle": "outside", "verdict": "合适"}]},
        {"batch": "OLD", "decisions": [{"handle": "approved_one", "verdict": "unknown"}]},
    ],
)
def test_rejects_unsafe_feedback(tmp_path, feedback):
    source = _source(tmp_path / "source.json")
    feedback_path = _write_json(tmp_path / "feedback.json", feedback)
    with pytest.raises(ManifestError):
        build_manifest(source, feedback_path, "NEW", _db(tmp_path / "cache.db"))
