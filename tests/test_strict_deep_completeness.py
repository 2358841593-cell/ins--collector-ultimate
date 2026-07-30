"""Strict deep-collection gates distinguish genuine low activity from failures."""
from __future__ import annotations

import json
import sys

import browser_collect_v2
import pytest

from extensions.sop_v2 import creator_cache as cc
from extensions.sop_v2.config import load_config
from extensions.sop_v2.pipeline import (
    audit_collect,
    modash_cdp,
    stage3_collect,
    stage4_decide,
)

from _fixtures import clean_full


def _posts(count=10, *, likes=12, comments=0):
    return [
        {
            "code": f"/p/P{index}/",
            "like_count": likes,
            "comment_count": comments,
        }
        for index in range(count)
    ]


def _explicit_complete(**overrides):
    value = {
        "comments_read": True,
        "comments_analyzed": 0,
        "valid_comments": 0,
        "real_er": 1.2,
        "sampled_posts": _posts(),
        "deep_target_posts": 10,
        "deep_available_posts": 10,
        "deep_successful_posts": 10,
        "deep_failed_posts": 0,
        "deep_metric_missing_posts": [],
        "comment_attempted_posts": 0,
        "comment_completed_posts": 10,
        "comment_failed_posts": 0,
        "deep_collection_status": "complete",
    }
    value.update(overrides)
    return value


def test_strict_contract_accepts_proven_genuine_zero_comments():
    assert cc.strict_deep_reasons(
        _explicit_complete(), status="collected", target_posts=10
    ) == []


def test_strict_contract_accepts_explicit_repeated_low_comment_unavailable():
    candidate = _explicit_complete(
        sampled_posts=[
            {
                "code": "/p/P0/",
                "like_count": 12,
                "comment_count": 2,
                "comments_collected": 0,
                "comment_sampling_status": "unavailable_after_retry",
            },
            *_posts(9),
        ],
        comment_attempted_posts=1,
        comment_completed_posts=10,
        comment_failed_posts=[],
        comment_unavailable_posts=[
            {
                "url": "https://www.instagram.com/p/P0/",
                "reported_count": 2,
                "reason": "reported_low_count_unavailable_after_retry",
            }
        ],
    )

    assert cc.strict_deep_reasons(
        candidate, status="collected", target_posts=10
    ) == []


def test_strict_contract_rejects_partial_metrics_and_unproven_zero_comments():
    legacy = {
        "comments_read": True,
        "comments_analyzed": 0,
        "valid_comments": 0,
        "real_er": None,
        "sampled_posts": [
            {"like_count": None, "comment_count": None} for _ in range(4)
        ],
    }

    reasons = cc.strict_deep_reasons(
        legacy, status="collected", target_posts=10
    )

    assert any("旧版深采证据不足" in reason for reason in reasons)
    assert any("帖子覆盖不足(4/10)" in reason for reason in reasons)
    assert any("互动指标全缺失" in reason for reason in reasons)
    assert any("无法证明账号真实低互动" in reason for reason in reasons)


def test_legacy_record_with_strong_structured_evidence_does_not_need_recollect():
    legacy = {
        "comments_read": True,
        "comments_analyzed": 25,
        "valid_comments": 24,
        "real_er": 1.7,
        "sampled_posts": _posts(10, likes=20, comments=3),
    }

    assert cc.strict_deep_reasons(
        legacy, status="decided", target_posts=10
    ) == []


def test_strict_contract_uses_explicit_failure_counters_as_primary_signal():
    candidate = _explicit_complete(
        sampled_posts=_posts(8, comments=2),
        comments_analyzed=0,
        deep_available_posts=10,
        deep_successful_posts=8,
        deep_failed_posts=["/p/FAIL1/", "/p/FAIL2/"],
        deep_metric_missing_posts=["/p/P0/"],
        comment_attempted_posts=8,
        comment_completed_posts=5,
        comment_failed_posts=["/p/C1/", "/p/C2/", "/p/C3/"],
        deep_collection_status="incomplete",
    )

    reasons = cc.strict_deep_reasons(
        candidate, status="collected", target_posts=10
    )

    assert any("深采状态未完成" in reason for reason in reasons)
    assert any("帖子覆盖不足(8/10" in reason for reason in reasons)
    assert "帖子读取失败(2 帖)" in reasons
    assert "帖子互动指标缺失(1 帖)" in reasons
    assert "评论读取失败(3 帖)" in reasons


def test_rejected_is_exempt_unless_full_deep_is_required():
    shallow_reject = {"handle": "shallow", "comments_read": None}

    assert cc.strict_deep_reasons(
        shallow_reject, status="rejected", target_posts=10
    ) == []
    reasons = cc.strict_deep_reasons(
        shallow_reject,
        status="rejected",
        target_posts=10,
        require_full_deep=True,
    )
    assert any("旧版深采证据不足" in reason for reason in reasons)
    assert any("深采未完成" in reason for reason in reasons)


def test_stage3_strict_mode_blocks_incomplete_but_allows_genuine_low(
    tmp_path, monkeypatch
):
    cfg = load_config()
    candidate = {"handle": "creator"}

    monkeypatch.setattr(
        browser_collect_v2,
        "deep_collect",
        lambda *args: (_explicit_complete(), []),
    )
    verdict = stage3_collect.collect_one(
        object(), candidate, cfg, "BATCH", 10, strict_completeness=True
    )
    assert verdict[0:2] == ("advance", "collected")

    failed = _explicit_complete(
        deep_successful_posts=7,
        deep_failed_posts=3,
        deep_collection_status="incomplete",
        sampled_posts=_posts(7),
    )
    monkeypatch.setattr(
        browser_collect_v2, "deep_collect", lambda *args: (failed, [])
    )
    verdict = stage3_collect.collect_one(
        object(), candidate, cfg, "BATCH", 10, strict_completeness=True
    )
    assert verdict[0] == "error"
    assert verdict[1].startswith("deep_incomplete:")
    assert verdict[2] is failed


def test_stage3_strict_error_carries_failed_urls_and_partial_evidence(
    monkeypatch,
):
    cfg = load_config()
    failed = _explicit_complete(
        sampled_posts=_posts(7, comments=2),
        deep_successful_posts=7,
        deep_failed_posts=[
            "https://www.instagram.com/p/FAILED1/",
            "https://www.instagram.com/p/FAILED2/",
        ],
        deep_metric_missing_posts=[
            "https://www.instagram.com/p/METRIC1/",
        ],
        comment_completed_posts=6,
        comment_failed_posts=[
            "https://www.instagram.com/p/COMMENT1/",
        ],
        deep_collection_status="incomplete",
        intent_posts=[
            {
                "post_url": "https://www.instagram.com/p/P0/",
                "intent_comments": [{"username": "buyer", "text": "where can I buy?"}],
            }
        ],
        evidence_dir="data/evidence/BATCH/creator",
    )
    monkeypatch.setattr(
        browser_collect_v2, "deep_collect", lambda *args: (failed, [])
    )

    verdict = stage3_collect.collect_one(
        object(),
        {"handle": "creator"},
        cfg,
        "BATCH",
        10,
        strict_completeness=True,
    )

    assert verdict[0] == "error"
    partial = verdict[2]
    assert partial["deep_failed_posts"] == failed["deep_failed_posts"]
    assert (
        partial["deep_metric_missing_posts"]
        == failed["deep_metric_missing_posts"]
    )
    assert partial["comment_failed_posts"] == failed["comment_failed_posts"]
    assert partial["intent_posts"] == failed["intent_posts"]
    assert partial["evidence_dir"] == failed["evidence_dir"]


def test_incomplete_items_full_deep_includes_machine_rejects(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(cc, "DB", tmp_path / "cache.db")
    with cc._conn() as conn:  # noqa: SLF001 - isolated test database
        conn.execute(
            "INSERT INTO creator_profiles "
            "(handle,status,discovery_batch,stage_json) VALUES (?,?,?,?)",
            ("machine_reject", "rejected", "B", json.dumps({"handle": "machine_reject"})),
        )

    assert cc.incomplete_items("B", strict=True) == []
    items = cc.incomplete_items(
        "B", strict=True, require_full_deep=True, target_posts=10
    )
    assert [item["handle"] for item in items] == ["machine_reject"]
    assert any("深采未完成" in reason for reason in items[0]["reasons"])


def test_handles_file_supports_text_and_decisions_json(tmp_path):
    text_path = tmp_path / "handles.txt"
    text_path.write_text("# exact cohort\n@Alpha\nbeta\n", encoding="utf-8")
    json_path = tmp_path / "decisions.json"
    json_path.write_text(
        json.dumps(
            {
                "candidates": [
                    {"handle": "Gamma"},
                    {"handle": "@delta"},
                ]
            }
        ),
        encoding="utf-8",
    )

    assert audit_collect.load_handles_file(text_path) == {"alpha", "beta"}
    assert audit_collect.load_handles_file(json_path) == {"gamma", "delta"}


def test_handles_requeue_validation_blocks_customer_final_before_mutation(
    tmp_path, monkeypatch
):
    handles_path = tmp_path / "handles.txt"
    handles_path.write_text("safe\nclient_final\n", encoding="utf-8")
    monkeypatch.setattr(
        audit_collect.cc,
        "incomplete_items",
        lambda batch: [
            {"handle": "safe", "status": "decided", "reasons": ["incomplete"]}
        ],
    )
    monkeypatch.setattr(
        audit_collect.cc,
        "validate_recollect_scope",
        lambda handles: (_ for _ in ()).throw(
            ValueError("client_final=['client_final']")
        ),
    )
    requeued = []
    monkeypatch.setattr(
        audit_collect.cc,
        "requeue_for_recollect",
        lambda handles: requeued.extend(handles),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "audit_collect",
            "--batch-id",
            "B",
            "--handles-file",
            str(handles_path),
            "--requeue",
        ],
    )

    assert audit_collect.main() == 2
    assert requeued == []


def test_machine_reject_requeue_clears_reject_state_but_preserves_pricing(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(cc, "DB", tmp_path / "cache.db")
    stage = {
        "handle": "machine",
        "comments_read": True,
        "sampled_posts": _posts(2),
        "deep_collection_status": "incomplete",
        "comment_unavailable_posts": [
            {
                "url": "https://www.instagram.com/p/P0/",
                "reported_count": 1,
                "reason": "reported_low_count_unavailable_after_retry",
            }
        ],
        "pricing_reel_samples": [{"code": "/reel/KEEP/"}],
        "pricing_estimate": {"status": "complete"},
        "pricing_captured_at": "2026-07-29T10:00:00+08:00",
    }
    with cc._conn() as conn:  # noqa: SLF001 - isolated test database
        conn.execute(
            "INSERT INTO creator_profiles "
            "(handle,status,reject_reason,discovery_batch,stage_json) "
            "VALUES (?,?,?,?,?)",
            ("machine", "rejected", "off_niche", "B", json.dumps(stage)),
        )

    assert cc.requeue_for_recollect(["machine"]) == 1
    with cc._conn() as conn:  # noqa: SLF001
        row = conn.execute(
            "SELECT status,reject_reason,stage_json FROM creator_profiles "
            "WHERE handle='machine'"
        ).fetchone()
    saved = json.loads(row["stage_json"])
    assert row["status"] == "qualified"
    assert row["reject_reason"] is None
    assert "sampled_posts" not in saved
    assert "deep_collection_status" not in saved
    assert "comment_unavailable_posts" not in saved
    assert saved["pricing_estimate"]["status"] == "complete"
    assert saved["pricing_reel_samples"] == [{"code": "/reel/KEEP/"}]


def test_stage4_strict_and_all_candidate_modes_block_before_output(
    tmp_path, monkeypatch
):
    active = clean_full(
        handle="active",
        _status="collected",
        _reject_reason=None,
        _discovery_batch="NEW",
        **_explicit_complete(),
    )
    rejected = {
        "handle": "shallow_reject",
        "_status": "rejected",
        "_reject_reason": "off_niche",
        "_discovery_batch": "NEW",
    }
    monkeypatch.setattr(
        stage4_decide.cc,
        "export_all_with_data",
        lambda scope: [active, rejected],
    )
    monkeypatch.setattr(stage4_decide.cc, "advance", lambda *args: None)

    strict_out = tmp_path / "strict.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "stage4_decide",
            "--batch-id",
            "NEW",
            "--track",
            "paid",
            "--strict-completeness",
            "--out",
            str(strict_out),
            "--no-xlsx",
        ],
    )
    assert stage4_decide.main() == 0
    assert strict_out.exists()

    all_out = tmp_path / "all.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "stage4_decide",
            "--batch-id",
            "NEW",
            "--track",
            "paid",
            "--full-deep-all-candidates",
            "--out",
            str(all_out),
            "--no-xlsx",
        ],
    )
    assert stage4_decide.main() == 1
    assert not all_out.exists()


def test_stage4_explicit_zero_modash_cap_processes_full_shortlist(
    tmp_path, monkeypatch
):
    candidates = [
        clean_full(
            handle=f"candidate_{index}",
            fake_pct=None,
            _status="collected",
            _reject_reason=None,
            _discovery_batch="NEW",
        )
        for index in range(25)
    ]
    received = []
    monkeypatch.setattr(
        stage4_decide.cc, "export_all_with_data", lambda scope: candidates
    )
    monkeypatch.setattr(stage4_decide.cc, "advance", lambda *args: None)

    def fake_enrich(cands, *args, **kwargs):
        received.extend(cand["handle"] for cand in cands)
        return {"matched": 0, "total": len(cands)}

    monkeypatch.setattr(modash_cdp, "enrich_via_cdp", fake_enrich)
    out = tmp_path / "all-modash.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "stage4_decide",
            "--batch-id",
            "NEW",
            "--track",
            "paid",
            "--modash-cdp",
            "--modash-cap",
            "0",
            "--out",
            str(out),
            "--no-xlsx",
        ],
    )

    assert stage4_decide.main() == 0
    assert len(received) == 25
    assert received == [f"candidate_{index}" for index in range(25)]


def test_stage4_rejects_negative_modash_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "stage4_decide",
            "--batch-id",
            "NEW",
            "--track",
            "paid",
            "--modash-cap",
            "-1",
            "--out",
            str(tmp_path / "never.json"),
        ],
    )

    with pytest.raises(SystemExit) as exc:
        stage4_decide.main()
    assert exc.value.code == 2
