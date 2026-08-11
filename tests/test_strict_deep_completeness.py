"""Strict deep-collection gates distinguish genuine low activity from failures."""
from __future__ import annotations

from copy import deepcopy
import json
import sys

import browser_collect_v2
import pytest

from extensions.sop_v2 import comments as comment_semantics
from extensions.sop_v2 import creator_cache as cc
from extensions.sop_v2 import pricing as pricing_mod
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


def _verified_empty_thread_post():
    endpoint = {
        "http_status": 200,
        "status": "ok",
        "comment_count": 0,
        "comments_count": 0,
        "fb_comments_count": 0,
        "has_more_comments": False,
        "has_more_headload_comments": False,
        "has_more_headload_fb_comments": False,
    }
    post = {
        "code": "/p/EMPTY/",
        "url": "https://www.instagram.com/p/EMPTY/",
        "like_count": 3,
        "comment_count": 3,
        "comments_collected": 0,
        "comment_sampling_status": "verified_empty_thread",
        "comment_empty_thread_evidence": {
            "schema": comment_semantics.EMPTY_THREAD_EVIDENCE_SCHEMA,
            "source": comment_semantics.EMPTY_THREAD_EVIDENCE_SOURCE,
            "marker": "No comments yet.",
            "marker_visible": True,
            "reported_count": 3,
            "endpoint": endpoint,
        },
    }
    unavailable = {
        "url": post["url"],
        "reported_count": 3,
        "reason": comment_semantics.EMPTY_THREAD_REASON,
        "source": comment_semantics.EMPTY_THREAD_EVIDENCE_SOURCE,
        "marker": "No comments yet.",
        "endpoint_summary": deepcopy(endpoint),
    }
    return post, unavailable


def _explicit_verified_empty_candidate():
    post, unavailable = _verified_empty_thread_post()
    return _explicit_complete(
        sampled_posts=[post, *_posts(9)],
        comment_attempted_posts=1,
        comment_completed_posts=10,
        comment_failed_posts=[],
        comment_unavailable_posts=[unavailable],
    )


def test_strict_contract_accepts_dom_and_endpoint_verified_empty_thread():
    assert cc.strict_deep_reasons(
        _explicit_verified_empty_candidate(),
        status="collected",
        target_posts=10,
    ) == []


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_evidence",
        "invalid_marker",
        "endpoint_not_ok",
        "endpoint_nonempty",
        "pairs_present",
        "reported_count_mismatch",
        "missing_unavailable_row",
    ],
)
def test_strict_contract_rejects_forged_verified_empty_thread(mutation):
    candidate = _explicit_verified_empty_candidate()
    post = candidate["sampled_posts"][0]
    unavailable = candidate["comment_unavailable_posts"][0]
    if mutation == "missing_evidence":
        post.pop("comment_empty_thread_evidence")
    elif mutation == "invalid_marker":
        post["comment_empty_thread_evidence"]["marker"] = "comments hidden"
    elif mutation == "endpoint_not_ok":
        post["comment_empty_thread_evidence"]["endpoint"]["status"] = "fail"
    elif mutation == "endpoint_nonempty":
        post["comment_empty_thread_evidence"]["endpoint"]["comments_count"] = 1
    elif mutation == "pairs_present":
        post["comments_collected"] = 1
    elif mutation == "reported_count_mismatch":
        unavailable["reported_count"] = 2
    elif mutation == "missing_unavailable_row":
        candidate["comment_unavailable_posts"] = []

    reasons = cc.strict_deep_reasons(
        candidate, status="collected", target_posts=10
    )

    assert any("评论不可见终态证据无效" in reason for reason in reasons)


def test_strict_contract_never_accepts_count_three_as_low_retry_terminal():
    candidate = _explicit_complete(
        sampled_posts=[
            {
                "code": "/p/HIGH/",
                "like_count": 12,
                "comment_count": 3,
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
                "url": "https://www.instagram.com/p/HIGH/",
                "reported_count": 3,
                "reason": "reported_low_count_unavailable_after_retry",
            }
        ],
    )

    reasons = cc.strict_deep_reasons(
        candidate, status="collected", target_posts=10
    )

    assert any("评论不可见终态证据无效" in reason for reason in reasons)


def test_strict_contract_rejects_positive_post_with_forged_collected_status():
    candidate = _explicit_complete(
        sampled_posts=[
            {
                "code": "/p/FORGED/",
                "like_count": 12,
                "comment_count": 3,
                "comments_collected": 0,
                "comment_sampling_status": "collected",
            },
            *_posts(9),
        ],
        comment_attempted_posts=1,
        comment_completed_posts=10,
        comment_failed_posts=[],
    )

    reasons = cc.strict_deep_reasons(
        candidate, status="collected", target_posts=10
    )

    assert any("逐帖评论完成证据无效" in reason for reason in reasons)


def test_strict_contract_checks_low_terminal_retry_state_when_present():
    candidate = _explicit_complete(
        sampled_posts=[
            {
                "code": "/p/LOW/",
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
                "url": "https://www.instagram.com/p/LOW/",
                "reported_count": 2,
                "reason": "reported_low_count_unavailable_after_retry",
            }
        ],
        stage3_comment_retry_state=[
            {
                "identity": "LOW",
                "url": "https://www.instagram.com/p/LOW/",
                "state": "retry_pending",
                "failure_streak": 1,
            }
        ],
    )

    reasons = cc.strict_deep_reasons(
        candidate, status="collected", target_posts=10
    )
    assert any("评论不可见终态证据无效" in reason for reason in reasons)

    candidate["stage3_comment_retry_state"][0].update(
        state="terminal_unavailable", failure_streak=2
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
        lambda *args, **kwargs: (_explicit_complete(), []),
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
        browser_collect_v2, "deep_collect", lambda *args, **kwargs: (failed, [])
    )
    verdict = stage3_collect.collect_one(
        object(), candidate, cfg, "BATCH", 10, strict_completeness=True
    )
    assert verdict[0] == "error"
    assert verdict[1].startswith("deep_incomplete:")
    assert verdict[2] is failed


def test_formal_stage3_disables_pre_cas_cache_write(monkeypatch):
    cfg = load_config()
    observed = {}

    def fake_deep_collect(*args, **kwargs):
        observed.update(kwargs)
        return _explicit_complete(), []

    monkeypatch.setattr(browser_collect_v2, "deep_collect", fake_deep_collect)

    verdict = stage3_collect.collect_one(
        object(),
        {"handle": "creator"},
        cfg,
        "BATCH",
        10,
        strict_completeness=True,
    )

    assert verdict[0:2] == ("advance", "collected")
    assert observed["persist_cache"] is False


def test_stage3_optional_translator_enriches_result_without_recollecting(
    monkeypatch,
):
    cfg = load_config()
    collected = _explicit_complete(
        comments_analyzed=1,
        valid_comments=1,
        comment_sample=["Où acheter ?"],
    )
    monkeypatch.setattr(
        browser_collect_v2,
        "deep_collect",
        lambda *args, **kwargs: (collected, []),
    )
    calls = []

    def translate(result):
        calls.append(result)
        result["comment_translations"] = [
            {
                "original_text": "Où acheter ?",
                "translated_zh": "在哪里买？",
                "source_language": "fr",
                "status": "translated",
            }
        ]

    verdict = stage3_collect.collect_one(
        object(),
        {"handle": "creator"},
        cfg,
        "BATCH",
        10,
        strict_completeness=True,
        comment_translator=translate,
    )

    assert verdict[0:2] == ("advance", "collected")
    assert calls == [collected]
    assert verdict[2]["comment_translations"][0]["translated_zh"] == "在哪里买？"


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
        browser_collect_v2, "deep_collect", lambda *args, **kwargs: (failed, [])
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
        "deep_collection_attempts": [{"attempt_id": "old-attempt"}],
        "deep_canonical_attempt_id": "old-attempt",
        "deep_canonical_quality": {"sampled_posts": 2},
        "pricing_canonical_attempt_id": "old-attempt",
        "pricing_canonical_quality": {"pricing_rank": 4},
        "stage3_comment_retry_state": {
            "schema_version": 1,
            "by_identity": {"P0": {"state": "terminal_unavailable"}},
        },
        "deep_evidence_merge_provenance": {"internal": True},
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
    assert "deep_collection_attempts" not in saved
    assert "deep_canonical_attempt_id" not in saved
    assert "deep_canonical_quality" not in saved
    assert "pricing_canonical_attempt_id" not in saved
    assert "pricing_canonical_quality" not in saved
    assert "stage3_comment_retry_state" not in saved
    assert "deep_evidence_merge_provenance" not in saved
    assert saved["pricing_estimate"]["status"] == "complete"
    assert saved["pricing_reel_samples"] == [{"code": "/reel/KEEP/"}]


def test_requeue_reset_covers_every_stage3_internal_field():
    from extensions.sop_v2.pipeline import deep_attempts

    assert set(deep_attempts.STAGE3_RESET_FIELDS) <= set(cc._DEEP_FIELDS)  # noqa: SLF001


def test_stage4_strict_and_all_candidate_modes_block_before_output(
    tmp_path, monkeypatch
):
    pricing_rows = [
        {
            "code": f"/reel/PRICE{index}/",
            "url": f"https://www.instagram.com/reel/PRICE{index}/",
            "is_reel": True,
            "pinned": False,
            "play_count": 10_000,
            "play_count_status": "observed",
            "play_count_source": "ig_media_info.ig_play_count",
            "grid_rank": index,
        }
        for index in range(10)
    ]
    pricing_estimate = pricing_mod.derive_quote_estimate(
        {"pricing_reel_samples": pricing_rows}, load_config()
    )
    active = clean_full(
        handle="active",
        _status="collected",
        _reject_reason=None,
        _discovery_batch="NEW",
        pricing_reel_samples=pricing_rows,
        pricing_estimate=pricing_estimate,
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


def test_stage4_strict_blocks_unproven_short_pricing_before_output(
    tmp_path, monkeypatch, capsys
):
    rows = [
        {
            "code": f"/reel/SHORT{index}/",
            "url": f"https://www.instagram.com/reel/SHORT{index}/",
            "is_reel": True,
            "pinned": False,
            "play_count": 10_000,
            "play_count_status": "observed",
            "play_count_source": "ig_media_info.ig_play_count",
            "grid_rank": index,
        }
        for index in range(5)
    ]
    partial = pricing_mod.derive_quote_estimate(
        {"pricing_reel_samples": rows}, load_config()
    )
    candidate = clean_full(
        handle="partial_price",
        _status="collected",
        _reject_reason=None,
        _discovery_batch="NEW",
        pricing_reel_samples=rows,
        pricing_estimate=partial,
        **_explicit_complete(),
    )
    monkeypatch.setattr(
        stage4_decide.cc, "export_all_with_data", lambda _scope: [candidate]
    )
    monkeypatch.setattr(stage4_decide.cc, "advance", lambda *args: None)
    out = tmp_path / "blocked.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "stage4_decide",
            "--batch-id", "NEW",
            "--track", "paid",
            "--strict-completeness",
            "--out", str(out),
            "--no-xlsx",
        ],
    )

    assert stage4_decide.main() == 1
    assert not out.exists()
    assert "报价总体证据未闭合(status=partial)" in capsys.readouterr().out


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


def _enrichment_ready(handle: str, **overrides):
    sampled_posts = [
        {"caption": "#ad skincare routine"},
        {"caption_text": "Paid partnership skincare routine"},
        *[{"caption": "organic skincare routine"} for _ in range(8)],
    ]
    candidate = clean_full(
        handle=handle,
        modash_report=True,
        promotional_post_count=2,
        sponsorship_saturation=20.0,
        sampled_posts=sampled_posts,
        deep_target_posts=10,
        deep_available_posts=10,
        _status="collected",
        _reject_reason=None,
        _discovery_batch="NEW",
    )
    candidate.update(overrides)
    if candidate.get("storefront_status") == "confirmed_yes":
        candidate.setdefault("storefront_url", f"https://shopmy.us/{handle}")
        candidate.setdefault("storefront_type", "ShopMy")
    return candidate


def _run_strict_enrichment(tmp_path, monkeypatch, candidates, advances):
    monkeypatch.setattr(
        stage4_decide.cc,
        "export_all_with_data",
        lambda _scope: candidates,
    )
    monkeypatch.setattr(
        stage4_decide.cc,
        "advance",
        lambda *args: advances.append(args),
    )
    monkeypatch.setattr(stage4_decide.cc, "status_dist", lambda _batch: {})
    out = tmp_path / "strict-enrichment.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "stage4_decide",
            "--batch-id", "NEW",
            "--track", "paid",
            "--strict-enrichment-completeness",
            "--out", str(out),
            "--no-xlsx",
        ],
    )
    return stage4_decide.main(), out


def test_stage4_strict_enrichment_blocks_r1_gap_before_db_or_output(
    tmp_path, monkeypatch, capsys
):
    candidates = [
        _enrichment_ready("complete"),
        _enrichment_ready("r1_missing_modash", modash_report=False),
    ]
    advances = []

    rc, out = _run_strict_enrichment(
        tmp_path, monkeypatch, candidates, advances
    )

    assert rc == 1
    assert advances == []
    assert not out.exists()
    output = capsys.readouterr().out
    assert "@r1_missing_modash" in output
    assert "modash_report未完成" in output


def test_stage4_strict_enrichment_complete_cohort_passes_and_manifests_stats(
    tmp_path, monkeypatch
):
    candidates = [
        _enrichment_ready("with_storefront"),
        _enrichment_ready(
            "without_storefront",
            storefront_status="confirmed_no",
            bio_links=["https://example.com/about"],
            promotional_post_count=0,
            sponsorship_saturation=0.0,
            sampled_posts=[
                {"caption": "organic skincare routine"}
                for _ in range(10)
            ],
        ),
    ]
    advances = []

    rc, out = _run_strict_enrichment(
        tmp_path, monkeypatch, candidates, advances
    )

    assert rc == 0
    assert out.exists()
    assert [args[0] for args in advances] == [
        "with_storefront",
        "without_storefront",
    ]
    manifest = json.loads(out.read_text(encoding="utf-8"))["manifest"]
    assert manifest["strict_enrichment_completeness"] is True
    assert manifest["enrichment_completeness"] == {
        "candidate_count": 2,
        "complete_count": 2,
        "incomplete_count": 0,
        "modash_report_complete_count": 2,
        "modash_report_missing_count": 0,
        "storefront_complete_count": 2,
        "storefront_incomplete_count": 0,
        "sponsorship_complete_count": 2,
        "sponsorship_incomplete_count": 0,
    }


@pytest.mark.parametrize(
    ("overrides", "expected_reason"),
    [
        (
            {"storefront_url": None},
            "confirmed_yes_storefront_url_missing",
        ),
        (
            {
                "storefront_url": "javascript://amazon.com/shop/unsafe",
                "storefront_type": "Amazon",
            },
            "storefront_url_not_safe_absolute_http",
        ),
        (
            {
                "storefront_url": "https://amazon.com/shop/type-conflict",
                "storefront_type": "ShopMy",
            },
            "storefront_url_type_conflict",
        ),
        (
            {"storefront_type": None},
            "confirmed_yes_storefront_type_missing_or_invalid",
        ),
        (
            {
                "storefront_status": "confirmed_no",
                "storefront_url": "https://shopmy.us/stale",
                "storefront_type": "ShopMy",
            },
            "status_conflict",
        ),
        (
            {
                "storefront_status": "confirmed_no",
                "amazon_storefront_link": "https://amazon.com/shop/stale",
            },
            "confirmed_no_amazon_storefront_link_present",
        ),
        (
            {"storefront_status": "confirmed_no", "storefront_type": "Amazon"},
            "confirmed_no_storefront_type_present",
        ),
        (
            {"storefront_status": "unknown"},
            "status_unknown",
        ),
        (
            {
                "storefront_url": "https://shopmy.us/current",
                "storefront_type": "ShopMy",
                "amazon_storefront_link": "https://amazon.com/shop/stale",
            },
            "amazon_storefront_link_conflict",
        ),
    ],
)
def test_stage4_strict_enrichment_rejects_invalid_storefront_contract_before_write(
    tmp_path, monkeypatch, capsys, overrides, expected_reason
):
    candidate = _enrichment_ready("invalid_storefront", **overrides)
    advances = []

    rc, out = _run_strict_enrichment(
        tmp_path, monkeypatch, [candidate], advances
    )

    assert rc == 1
    assert advances == []
    assert not out.exists()
    assert expected_reason in capsys.readouterr().out


def test_stage4_strict_enrichment_accepts_report_with_source_field_gaps(
    tmp_path, monkeypatch
):
    candidate = _enrichment_ready(
        "report_has_source_gaps",
        fake_pct=None,
        creator_country=None,
        top_audience_country=None,
        general_er=None,
        target_countries_audience_pct=None,
        top_language_pct=None,
    )
    advances = []

    rc, out = _run_strict_enrichment(
        tmp_path, monkeypatch, [candidate], advances
    )

    assert rc == 0
    assert out.exists()
    assert [args[0] for args in advances] == ["report_has_source_gaps"]


def test_stage4_modash_all_missing_covers_rejected_and_skips_existing_report(
    tmp_path, monkeypatch
):
    active = _enrichment_ready("active_missing", modash_report=False)
    rejected = _enrichment_ready(
        "rejected_missing",
        modash_report=False,
        _status="rejected",
        _reject_reason="off_niche",
    )
    existing = _enrichment_ready(
        "existing_report",
        fake_pct=None,
        creator_country=None,
    )
    candidates = [active, rejected, existing]
    received = []
    advances = []
    monkeypatch.setattr(
        stage4_decide.cc,
        "export_all_with_data",
        lambda _scope: candidates,
    )
    monkeypatch.setattr(
        stage4_decide.cc,
        "advance",
        lambda *args: advances.append(args),
    )
    monkeypatch.setattr(stage4_decide.cc, "status_dist", lambda _batch: {})

    def fake_enrich(shortlist, *_args, **_kwargs):
        received.extend(candidate["handle"] for candidate in shortlist)
        for candidate in shortlist:
            candidate["modash_report"] = True
        return {"matched": len(shortlist), "total": len(shortlist)}

    monkeypatch.setattr(modash_cdp, "enrich_via_cdp", fake_enrich)
    out = tmp_path / "all-missing.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "stage4_decide",
            "--batch-id", "NEW",
            "--track", "paid",
            "--modash-all-missing",
            "--modash-cap", "0",
            "--strict-enrichment-completeness",
            "--out", str(out),
            "--no-xlsx",
        ],
    )

    assert stage4_decide.main() == 0
    assert received == ["active_missing", "rejected_missing"]
    assert [args[0] for args in advances] == ["active_missing", "existing_report"]
    manifest = json.loads(out.read_text(encoding="utf-8"))["manifest"]
    assert manifest["modash_enrichment"]["mode"] == "all_missing_reports"
    assert manifest["modash_enrichment"]["cap"] == 0
    assert manifest["modash_enrichment"]["shortlist"]["selected"] == 2


def test_stage4_modash_all_missing_requires_strict_gate_before_runtime_load(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(
        stage4_decide,
        "load_config",
        lambda: (_ for _ in ()).throw(
            AssertionError("invalid startup flags must fail before runtime load")
        ),
    )
    out = tmp_path / "never.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "stage4_decide",
            "--batch-id", "NEW",
            "--track", "paid",
            "--modash-all-missing",
            "--out", str(out),
            "--no-xlsx",
        ],
    )

    assert stage4_decide.main() == 1
    assert not out.exists()
    assert "--strict-enrichment-completeness" in capsys.readouterr().out


def test_stage4_modash_all_missing_requires_explicit_unlimited_cap(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(
        stage4_decide,
        "load_config",
        lambda: (_ for _ in ()).throw(
            AssertionError("invalid startup flags must fail before runtime load")
        ),
    )
    out = tmp_path / "never.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "stage4_decide",
            "--batch-id", "NEW",
            "--track", "paid",
            "--modash-all-missing",
            "--strict-enrichment-completeness",
            "--modash-cap", "20",
            "--out", str(out),
            "--no-xlsx",
        ],
    )

    assert stage4_decide.main() == 1
    assert not out.exists()
    assert "--modash-cap 0" in capsys.readouterr().out


def test_stage4_modash_runtime_failure_never_writes_db_or_output(
    tmp_path, monkeypatch, capsys
):
    candidate = _enrichment_ready("runtime_failure", modash_report=False)
    advances = []
    monkeypatch.setattr(
        stage4_decide.cc,
        "export_all_with_data",
        lambda _scope: [candidate],
    )
    monkeypatch.setattr(
        stage4_decide.cc,
        "advance",
        lambda *args: advances.append(args),
    )
    monkeypatch.setattr(
        modash_cdp,
        "enrich_via_cdp",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("cache unavailable")
        ),
    )
    out = tmp_path / "never.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "stage4_decide",
            "--batch-id", "NEW",
            "--track", "paid",
            "--modash-all-missing",
            "--strict-enrichment-completeness",
            "--modash-cap", "0",
            "--out", str(out),
            "--no-xlsx",
        ],
    )

    assert stage4_decide.main() == 1
    assert advances == []
    assert not out.exists()
    assert "OSError" in capsys.readouterr().out


def test_sponsorship_gate_recomputes_caption_window_not_promotional_count():
    candidate = _enrichment_ready(
        "independent_signals",
        promotional_post_count=9,
    )

    assert stage4_decide._sponsorship_window_is_consistent(  # noqa: SLF001
        candidate
    )
    candidate["sponsorship_saturation"] = 10.0
    assert not stage4_decide._sponsorship_window_is_consistent(  # noqa: SLF001
        candidate
    )


@pytest.mark.parametrize(
    "sampled_posts",
    [None, [], ["not-an-object"], [{"caption": 123}]],
)
def test_sponsorship_gate_fails_closed_for_invalid_caption_window(
    sampled_posts,
):
    candidate = _enrichment_ready(
        "invalid_sponsorship_window",
        sampled_posts=sampled_posts,
    )

    assert not stage4_decide._sponsorship_window_is_consistent(  # noqa: SLF001
        candidate
    )


def test_sponsorship_gate_counts_missing_caption_as_empty_window_row():
    candidate = _enrichment_ready(
        "missing_caption_is_valid",
        sampled_posts=[{}, {"caption": None}, {"caption_text": "#ad"}],
        sponsorship_saturation=33.3,
    )

    assert stage4_decide._sponsorship_window_is_consistent(  # noqa: SLF001
        candidate
    )


@pytest.mark.parametrize(
    "saturation",
    [None, True, "20.0", -0.1, 100.1, float("nan")],
)
def test_sponsorship_gate_fails_closed_for_invalid_percentage(saturation):
    candidate = _enrichment_ready(
        "invalid_sponsorship_percentage",
        sponsorship_saturation=saturation,
    )

    assert not stage4_decide._sponsorship_window_is_consistent(  # noqa: SLF001
        candidate
    )


def test_sponsorship_gate_uses_at_most_first_fifteen_sampled_posts():
    candidate = _enrichment_ready(
        "fifteen_post_window",
        sampled_posts=[
            *[{"caption": "organic skincare routine"} for _ in range(15)],
            {"caption": "#ad outside the target window"},
        ],
        sponsorship_saturation=0.0,
    )

    assert stage4_decide._sponsorship_window_is_consistent(  # noqa: SLF001
        candidate
    )
