"""Stage 3 retries retain attempt evidence and never regress canonical data."""
from __future__ import annotations

import hashlib
import json
import signal
import sys
from copy import deepcopy
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from extensions.sop_v2 import comment_translation  # noqa: E402
from extensions.sop_v2 import comments as comment_semantics  # noqa: E402
from extensions.sop_v2 import pricing as pricing_policy  # noqa: E402
from extensions.sop_v2.config import load_config  # noqa: E402
from extensions.sop_v2.pipeline import (  # noqa: E402
    _base,
    deep_attempts,
    deep_evidence_merge,
)


def _deep_evidence(
    *,
    posts: int,
    comments: int,
    complete: bool,
    failed_code: str | None = None,
) -> dict:
    target_refs = [
        {"url": f"/p/WINDOW{i}/", "pinned": False}
        for i in range(10)
    ]
    sampled = [
        {
            "code": f"/p/WINDOW{i}/",
            "url": f"https://www.instagram.com/p/WINDOW{i}/",
            "like_count": 100 + i,
            "comment_count": 10,
            "metrics_status": "observed",
            "captured_at": "2026-08-10T10:00:00+00:00",
        }
        for i in range(posts)
    ]
    failed = (
        [f"https://www.instagram.com/p/{failed_code}/"]
        if failed_code
        else []
    )
    translated = comments if complete else max(0, comments - 2)
    translation_failed = 0 if complete else min(2, comments)
    evidence = {
        "post_refs": target_refs,
        "codes": [row["url"] for row in target_refs],
        "sampled_posts": sampled,
        "comments_read": True,
        "comment_records": [
            {
                "username": f"buyer{i}",
                "text": f"comment {i}",
                "post_url": sampled[i % max(1, posts)]["url"],
            }
            for i in range(comments)
        ] if posts else [],
        "comment_sample": [f"comment {i}" for i in range(comments)],
        "comments_analyzed": comments,
        "valid_comments": comments,
        "deep_target_posts": 10,
        "deep_available_posts": 10,
        "deep_successful_posts": posts,
        "deep_failed_posts": [] if complete else ["/p/POST-FAILED/"],
        "deep_metric_missing_posts": [],
        "comment_attempted_posts": posts,
        "comment_completed_posts": posts if complete else max(0, posts - 1),
        "comment_failed_posts": failed,
        "comment_unavailable_posts": [],
        "deep_collection_status": "complete" if complete else "incomplete",
        "pricing_reel_samples": [
            {
                "code": f"/reel/PRICE{i}/",
                "is_reel": True,
                "pinned": False,
                "play_count": 1000 + i,
                "play_count_status": "observed",
                "play_count_source": "ig_media_info.ig_play_count",
            }
            for i in range(10 if complete else 3)
        ],
        "pricing_captured_at": "2026-08-10T12:00:00+08:00",
        "comment_translations": [
            {
                "id": f"comment-{i}",
                "translation_status": "translated" if i < translated else "failed",
                "translated_zh": f"评论 {i}" if i < translated else None,
            }
            for i in range(comments)
        ],
        "comment_translation_summary": {
            "status": "complete" if complete else "partial",
            "translated_count": translated,
            "failed_count": translation_failed,
            "semantic_coverage_complete": complete,
        },
    }
    evidence["pricing_estimate"] = pricing_policy.derive_quote_estimate(
        evidence, load_config()
    )
    return evidence


def _candidate(**evidence) -> dict:
    return {
        "handle": "creator",
        "biography": "stable shallow evidence",
        **evidence,
    }


def _merge_ready_evidence(
    *,
    label: str,
    included: list[int],
    window_prefix: str = "MERGE",
    failed_comment_index: int | None = None,
) -> dict:
    codes = [f"/p/{window_prefix}{index}/" for index in range(10)]
    posts = []
    records = []
    translations = []
    comment_failed = []
    for index in included:
        url = f"https://www.instagram.com{codes[index]}"
        failed = index == failed_comment_index
        text = f"commentaire {label} {index}"
        posts.append(
            {
                "code": codes[index],
                "url": url,
                "caption": f"skincare serum {index}",
                "is_video": index % 2 == 0,
                "is_reel": index % 2 == 0,
                "like_count": 100 + index,
                "comment_count": 5,
                "metrics_status": "observed",
                "comment_sampling_status": (
                    "failed_reported_comments" if failed else "collected"
                ),
                "comments_collected": 0 if failed else 1,
                "captured_at": f"2026-08-10T12:00:{index:02d}+00:00",
            }
        )
        if failed:
            comment_failed.append(url)
            continue
        record = {"username": f"u_{label}_{index}", "text": text, "post_url": url}
        records.append(record)
        source_hash = hashlib.sha256(" ".join(text.split()).encode()).hexdigest()
        translations.append(
            {
                "source_hash": source_hash,
                "original_text": text,
                "translated_text": f"我想买这个产品 {label} {index}",
                "translated_zh": f"我想买这个产品 {label} {index}",
                "source_language": "fr",
                "translation_status": "translated",
                "status": "translated",
                "translation_error": None,
                "provider": "custom",
                "model": "cached-v1",
                "translator_version": comment_translation.SCHEMA_VERSION,
            }
        )
    complete = len(included) == 10 and not comment_failed
    missing = [index for index in range(10) if index not in included]
    return {
        "follower_count": 10_000,
        "post_refs": [{"url": code, "pinned": False} for code in codes],
        "codes": codes,
        "sampled_posts": posts,
        "comments_read": True,
        "comment_records": records,
        "comment_sample": [row["text"] for row in records],
        "comments_analyzed": len(records),
        "valid_comments": len(records),
        "deep_target_posts": 10,
        "deep_available_posts": 10,
        "deep_successful_posts": len(posts),
        "deep_failed_posts": [
            f"https://www.instagram.com{codes[index]}" for index in missing
        ],
        "deep_metric_missing_posts": [],
        "comment_attempted_posts": len(posts),
        "comment_completed_posts": len(posts) - len(comment_failed),
        "comment_failed_posts": comment_failed,
        "comment_unavailable_posts": [],
        "deep_collection_status": "complete" if complete else "incomplete",
        "comment_translations": translations,
        "comment_translation_summary": {
            "status": "complete",
            "provider": "custom",
            "model": "cached-v1",
            "requested_count": len(records),
            "translated_count": len(records),
            "failed_count": 0,
            "semantic_coverage_complete": True,
        },
        "pricing_estimate": {"status": "complete", "sample_count": 10},
        "pricing_reel_samples": [
            {"code": f"/reel/PRICE{index}/", "play_count": 1000 + index}
            for index in range(10)
        ],
        "pricing_captured_at": "2026-08-10T13:00:00+00:00",
    }


def _identities(entries) -> set[str]:
    return {
        deep_attempts._media_identity(  # noqa: SLF001 - focused identity assertion
            row.get("url") if isinstance(row, dict) else row
        )
        for row in entries
    }


def test_worse_failed_retry_keeps_prior_and_ledgers_both_attempts():
    prior = _candidate(**_deep_evidence(posts=10, comments=90, complete=True))
    context = deep_attempts.prepare_stage3_attempt(prior)
    worse = _candidate(
        **_deep_evidence(
            posts=3,
            comments=20,
            complete=False,
            failed_code="NEWFAIL",
        )
    )
    worse["_queue_lock_token"] = "must-never-persist"
    worse["sampled_posts"][0]["_queue_from_status"] = "qualified"

    verdict = deep_attempts.finalize_stage3_verdict(
        context,
        ("error", "deep_incomplete:retry regressed", worse),
        attempted_at="2026-08-10T11:00:00+00:00",
    )
    saved = verdict[2]

    assert verdict[:2] == ("error", "deep_incomplete:retry regressed")
    assert len(saved["sampled_posts"]) == 10
    assert len(saved["comment_records"]) == 90
    assert saved["deep_collection_status"] == "complete"
    assert saved["pricing_estimate"]["status"] == "complete"
    assert saved["comment_translation_summary"]["status"] == "complete"
    ledger = saved[deep_attempts.LEDGER_FIELD]
    assert [len(row["evidence"].get("sampled_posts", [])) for row in ledger] == [10, 3]
    assert [len(row["evidence"].get("comment_records", [])) for row in ledger] == [90, 20]
    assert ledger[1]["error"] == "deep_incomplete:retry regressed"
    assert saved[deep_attempts.CANONICAL_ATTEMPT_FIELD] == ledger[0]["attempt_id"]
    serialized = json.dumps(saved, ensure_ascii=False)
    assert "_queue_lock_token" not in serialized
    assert "_queue_from_status" not in serialized
    assert "must-never-persist" not in serialized


def test_improving_retry_becomes_canonical_and_success_is_ledgered():
    prior = _candidate(
        **_deep_evidence(
            posts=3,
            comments=20,
            complete=False,
            failed_code="OLDFAIL",
        )
    )
    context = deep_attempts.prepare_stage3_attempt(prior)
    improved = _candidate(
        **_deep_evidence(posts=10, comments=90, complete=True)
    )

    verdict = deep_attempts.finalize_stage3_verdict(
        context,
        ("advance", "collected", improved),
        attempted_at="2026-08-10T12:00:00+00:00",
    )
    saved = verdict[2]
    ledger = saved[deep_attempts.LEDGER_FIELD]

    assert len(saved["sampled_posts"]) == 10
    assert len(saved["comment_records"]) == 90
    assert ledger[-1]["outcome"] == "advance"
    assert ledger[-1]["error"] is None
    assert saved[deep_attempts.CANONICAL_ATTEMPT_FIELD] == ledger[-1]["attempt_id"]


def test_next_retry_gets_union_of_historical_and_new_comment_failure_ids():
    prior = _candidate(
        **_deep_evidence(
            posts=8,
            comments=50,
            complete=False,
            failed_code="OLDFAIL",
        )
    )
    first = deep_attempts.prepare_stage3_attempt(prior)
    newer = _candidate(
        **_deep_evidence(
            posts=3,
            comments=20,
            complete=False,
            failed_code="NEWFAIL",
        )
    )
    saved = deep_attempts.finalize_stage3_verdict(
        first,
        ("error", "deep_incomplete", newer),
        attempted_at="2026-08-10T13:00:00+00:00",
    )[2]

    history = saved[deep_attempts.COMMENT_RETRY_HISTORY_FIELD]
    assert {row["identity"] for row in history} == {"NEWFAIL", "OLDFAIL"}
    next_attempt = deep_attempts.prepare_stage3_attempt(saved)
    assert _identities(next_attempt.candidate["comment_failed_posts"]) >= {
        "NEWFAIL",
        "OLDFAIL",
    }
    assert _identities(saved["comment_failed_posts"]) == {"OLDFAIL"}


def test_window_fingerprint_uses_target_refs_and_translation_count_fields():
    complete = _deep_evidence(posts=10, comments=90, complete=True)
    partial = _deep_evidence(posts=3, comments=20, complete=False)

    assert deep_attempts.window_fingerprint(complete) == (
        deep_attempts.window_fingerprint(partial)
    )
    quality = deep_attempts.summarize_quality(complete)
    assert quality["translation_complete"] is True
    assert quality["translated_net"] == 90


def test_empty_initial_error_never_clears_stage2_target_refs():
    prior = {
        "handle": "creator",
        "codes": [f"/p/SEED{i}/" for i in range(10)],
        "post_refs": [
            {"url": f"/p/SEED{i}/", "pinned": None}
            for i in range(10)
        ],
    }
    context = deep_attempts.prepare_stage3_attempt(prior)

    saved = deep_attempts.finalize_stage3_verdict(
        context,
        ("error", "logged_out"),
        attempted_at="2026-08-10T14:00:00+00:00",
    )[2]

    assert saved["codes"] == prior["codes"]
    assert saved["post_refs"] == prior["post_refs"]
    ledger = saved[deep_attempts.LEDGER_FIELD]
    assert ledger[0]["evidence"]["codes"] == prior["codes"]
    assert ledger[1]["evidence"] == {}
    assert saved[deep_attempts.CANONICAL_ATTEMPT_FIELD] == ledger[0]["attempt_id"]


def test_translator_failure_after_collection_keeps_self_contained_target_window():
    prior_evidence = _deep_evidence(posts=10, comments=90, complete=True)
    prior = _candidate(**prior_evidence)
    context = deep_attempts.prepare_stage3_attempt(prior)
    # Simulate deep_collect mutating the work copy completely, followed by the
    # translator raising before collect_one can return an explicit partial cand.
    failed_translation_evidence = _deep_evidence(
        posts=10, comments=95, complete=True
    )
    failed_translation_evidence.pop("comment_translations", None)
    failed_translation_evidence.pop("comment_translation_summary", None)
    context.candidate.update(failed_translation_evidence)

    saved = deep_attempts.finalize_stage3_verdict(
        context,
        ("error", "RuntimeError:translator unavailable"),
        attempted_at="2026-08-10T14:30:00+00:00",
    )[2]
    baseline, failed_translation = saved[deep_attempts.LEDGER_FIELD]
    evidence = failed_translation["evidence"]

    assert evidence["post_refs"] == prior["post_refs"]
    assert evidence["codes"] == prior["codes"]
    assert evidence["deep_target_posts"] == 10
    assert failed_translation["quality"]["window_fingerprint"] == (
        baseline["quality"]["window_fingerprint"]
    )
    assert len(saved[deep_attempts.LEDGER_FIELD]) == 2
    assert all(
        row["outcome"] != "synthetic_merge"
        for row in saved[deep_attempts.LEDGER_FIELD]
    )


def test_runtime_same_window_complementary_attempts_create_full_synthetic():
    old = _candidate(
        **_merge_ready_evidence(label="old", included=list(range(8)))
    )
    current = _candidate(
        **_merge_ready_evidence(
            label="new",
            included=list(range(1, 10)),
            failed_comment_index=1,
        )
    )

    verdict = deep_attempts.finalize_stage3_verdict(
        deep_attempts.prepare_stage3_attempt(old),
        ("error", "deep_incomplete:complementary attempts converged", current),
        attempted_at="2026-08-10T14:40:00+00:00",
    )
    saved = verdict[2]
    baseline, raw_attempt, synthetic = saved[deep_attempts.LEDGER_FIELD]

    assert verdict[:2] == ("advance", "collected")
    assert synthetic["outcome"] == "synthetic_merge"
    assert synthetic["synthetic"] is True
    assert isinstance(synthetic["evidence"], dict)
    assert "evidence_ref_attempt_id" not in synthetic
    assert synthetic["merge_provenance"]["sources"] == {
        "old": {"kind": "attempt", "attempt_id": baseline["attempt_id"]},
        "current": {
            "kind": "attempt",
            "attempt_id": raw_attempt["attempt_id"],
        },
    }
    assert synthetic["resolution"]["promoted_to"] == "collected"
    assert saved[deep_attempts.CANONICAL_ATTEMPT_FIELD] == synthetic["attempt_id"]
    assert len(saved["sampled_posts"]) == 10
    assert saved["deep_collection_status"] == "complete"
    assert saved["deep_failed_posts"] == []
    # Retry convergence consumes only the real current failure, not the
    # synthetic post chosen successfully from old evidence.
    assert _retry_row(saved, "MERGE1")["state"] == "retry_pending"
    assert _retry_row(saved, "MERGE1")["failure_streak"] == 1
    assert "deep_evidence_merge_provenance" not in saved
    assert "deep_evidence_merge_provenance" not in synthetic["evidence"]


def test_same_window_synthetic_does_not_mask_translator_or_proxy_error():
    old = _candidate(
        **_merge_ready_evidence(label="old", included=list(range(8)))
    )
    current = _candidate(
        **_merge_ready_evidence(label="new", included=list(range(1, 10)))
    )

    verdict = deep_attempts.finalize_stage3_verdict(
        deep_attempts.prepare_stage3_attempt(old),
        ("error", "RuntimeError:translator unavailable", current),
        attempted_at="2026-08-10T14:45:00+00:00",
    )

    assert verdict[:2] == ("error", "RuntimeError:translator unavailable")
    assert verdict[2][deep_attempts.LEDGER_FIELD][-1]["outcome"] == (
        "synthetic_merge"
    )
    assert "resolution" not in verdict[2][deep_attempts.LEDGER_FIELD][-1]


def test_complete_same_window_is_not_replaced_or_reexpanded_by_three_posts():
    old = _candidate(
        **_merge_ready_evidence(label="same", included=list(range(10)))
    )
    current = _candidate(
        **_merge_ready_evidence(label="same", included=list(range(3)))
    )

    saved = deep_attempts.finalize_stage3_verdict(
        deep_attempts.prepare_stage3_attempt(old),
        ("error", "deep_incomplete:3/10", current),
        attempted_at="2026-08-10T14:50:00+00:00",
    )[2]
    ledger = saved[deep_attempts.LEDGER_FIELD]

    assert len(saved["sampled_posts"]) == 10
    assert len(saved["comment_records"]) == 10
    assert saved["deep_collection_status"] == "complete"
    assert len(ledger) == 2
    assert all(row["outcome"] != "synthetic_merge" for row in ledger)
    assert saved[deep_attempts.CANONICAL_ATTEMPT_FIELD] == ledger[0]["attempt_id"]


def test_mismatched_ordered_window_never_field_merges_at_runtime():
    old = _candidate(
        **_merge_ready_evidence(
            label="old", included=list(range(10)), window_prefix="OLD"
        )
    )
    current = _candidate(
        **_merge_ready_evidence(
            label="new", included=list(range(9)), window_prefix="NEW"
        )
    )

    saved = deep_attempts.finalize_stage3_verdict(
        deep_attempts.prepare_stage3_attempt(old),
        ("error", "deep_incomplete", current),
        attempted_at="2026-08-10T14:55:00+00:00",
    )[2]

    assert deep_evidence_merge.windows_match(old, current) is False
    assert len(saved[deep_attempts.LEDGER_FIELD]) == 2
    assert len(saved["sampled_posts"]) == 10
    assert all("OLD" in post["url"] for post in saved["sampled_posts"])


def test_equal_quality_uses_newer_attempt_as_deterministic_tiebreak():
    prior_evidence = _deep_evidence(posts=10, comments=90, complete=True)
    prior_evidence["sampled_posts"][0]["caption"] = "older"
    prior = _candidate(**prior_evidence)
    context = deep_attempts.prepare_stage3_attempt(prior)
    newer = deepcopy(prior)
    newer["sampled_posts"][0]["caption"] = "newer"

    saved = deep_attempts.finalize_stage3_verdict(
        context,
        ("advance", "collected", newer),
        attempted_at="2026-08-10T15:00:00+00:00",
    )[2]

    assert saved["sampled_posts"][0]["caption"] == "newer"
    assert saved[deep_attempts.CANONICAL_ATTEMPT_FIELD] == (
        saved[deep_attempts.LEDGER_FIELD][-1]["attempt_id"]
    )


def test_identical_attempt_payload_is_content_addressed_not_duplicated():
    prior = _candidate(**_deep_evidence(posts=10, comments=90, complete=True))
    context = deep_attempts.prepare_stage3_attempt(prior)

    saved = deep_attempts.finalize_stage3_verdict(
        context,
        ("advance", "collected", deepcopy(prior)),
        attempted_at="2026-08-10T16:00:00+00:00",
    )[2]
    baseline, repeated = saved[deep_attempts.LEDGER_FIELD]

    assert "evidence" in baseline
    assert "evidence" not in repeated
    assert repeated["evidence_ref_attempt_id"] == baseline["attempt_id"]
    assert repeated["evidence_sha256"] == baseline["evidence_sha256"]
    assert saved[deep_attempts.CANONICAL_ATTEMPT_FIELD] == baseline["attempt_id"]


def test_corrupt_ref_only_canonical_pointer_resolves_to_full_owner():
    prior = _candidate(**_deep_evidence(posts=10, comments=90, complete=True))
    first = deep_attempts.finalize_stage3_verdict(
        deep_attempts.prepare_stage3_attempt(prior),
        ("advance", "collected", deepcopy(prior)),
        attempted_at="2026-08-10T16:00:00+00:00",
    )[2]
    baseline, repeated = first[deep_attempts.LEDGER_FIELD]
    assert "evidence" not in repeated

    corrupted = deepcopy(first)
    corrupted[deep_attempts.CANONICAL_ATTEMPT_FIELD] = repeated["attempt_id"]
    worse = _candidate(**_deep_evidence(posts=3, comments=20, complete=False))
    saved = deep_attempts.finalize_stage3_verdict(
        deep_attempts.prepare_stage3_attempt(corrupted),
        ("error", "deep_incomplete:3/10", worse),
        attempted_at="2026-08-10T16:30:00+00:00",
    )[2]

    assert saved[deep_attempts.CANONICAL_ATTEMPT_FIELD] == baseline["attempt_id"]
    owner = next(
        row
        for row in saved[deep_attempts.LEDGER_FIELD]
        if row["attempt_id"] == saved[deep_attempts.CANONICAL_ATTEMPT_FIELD]
    )
    assert isinstance(owner.get("evidence"), dict)


def test_retry_work_copy_strips_output_fields_but_keeps_safe_inputs():
    prior = _candidate(**_deep_evidence(posts=10, comments=90, complete=True))
    prior["real_er"] = 2.3
    context = deep_attempts.prepare_stage3_attempt(prior)
    working = context.candidate

    assert working["codes"] == prior["codes"]
    assert working["post_refs"] == prior["post_refs"]
    assert working["pricing_estimate"] == prior["pricing_estimate"]
    for output_only in (
        "sampled_posts",
        "comment_records",
        "comment_translations",
        "comment_translation_summary",
        "deep_collection_status",
        "real_er",
    ):
        assert output_only not in working
    assert context.prior_snapshot["comment_records"] == prior["comment_records"]


def _comment_attempt(code: str, status: str, *, reported_count: int = 1) -> dict:
    url = f"https://www.instagram.com/p/{code}/"
    failed = [url] if status.startswith("failed_") else []
    unavailable = (
        [
            {
                "url": url,
                "reported_count": reported_count,
                "reason": "reported_low_count_unavailable_after_retry",
            }
        ]
        if status == "unavailable_after_retry"
        else []
    )
    return {
        "post_refs": [{"url": f"/p/{code}/", "pinned": False}],
        "codes": [f"/p/{code}/"],
        "sampled_posts": [
            {
                "url": url,
                "code": f"/p/{code}/",
                "comment_count": reported_count,
                "like_count": 10,
                "comment_sampling_status": status,
            }
        ],
        "comments_read": True,
        "comment_records": (
            [{"username": "buyer", "text": "link?", "post_url": url}]
            if status == "collected"
            else []
        ),
        "deep_target_posts": 1,
        "deep_available_posts": 1,
        "deep_successful_posts": 1,
        "deep_failed_posts": [],
        "deep_metric_missing_posts": [],
        "comment_attempted_posts": 1,
        "comment_completed_posts": int(
            status in {"collected", "verified_zero", "unavailable_after_retry"}
        ),
        "comment_failed_posts": failed,
        "comment_unavailable_posts": unavailable,
        "deep_collection_status": (
            "complete" if not failed else "incomplete"
        ),
    }


def _verified_empty_comment_attempt(*, valid=True) -> dict:
    url = "https://www.instagram.com/p/EMPTY/"
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
    evidence = {
        "schema": comment_semantics.EMPTY_THREAD_EVIDENCE_SCHEMA,
        "source": comment_semantics.EMPTY_THREAD_EVIDENCE_SOURCE,
        "marker": "No comments yet." if valid else "unverified marker",
        "marker_visible": True,
        "reported_count": 3,
        "endpoint": endpoint,
    }
    row = {
        "url": url,
        "reported_count": 3,
        "reason": comment_semantics.EMPTY_THREAD_REASON,
        "source": comment_semantics.EMPTY_THREAD_EVIDENCE_SOURCE,
        "marker": "No comments yet.",
        "endpoint_summary": deepcopy(endpoint),
    }
    return {
        "post_refs": [{"url": "/p/EMPTY/", "pinned": False}],
        "codes": ["/p/EMPTY/"],
        "sampled_posts": [
            {
                "url": url,
                "code": "/p/EMPTY/",
                "comment_count": 3,
                "like_count": 10,
                "comments_collected": 0,
                "comment_sampling_status": "verified_empty_thread",
                "comment_empty_thread_evidence": evidence,
            }
        ],
        "comments_read": True,
        "comment_records": [],
        "deep_target_posts": 1,
        "deep_available_posts": 1,
        "deep_successful_posts": 1,
        "deep_failed_posts": [],
        "deep_metric_missing_posts": [],
        "comment_attempted_posts": 1,
        "comment_completed_posts": 1,
        "comment_failed_posts": [],
        "comment_unavailable_posts": [row],
        "deep_collection_status": "complete",
    }


def _retry_row(candidate: dict, identity: str) -> dict:
    return next(
        row
        for row in candidate[deep_attempts.COMMENT_RETRY_STATE_FIELD]
        if row["identity"] == identity
    )


def test_comment_retry_state_resets_success_and_counts_only_real_failures():
    initial = {"handle": "creator", "biography": "stable"}
    first = deep_attempts.finalize_stage3_verdict(
        deep_attempts.prepare_stage3_attempt(initial),
        ("error", "comment_failed", _candidate(**_comment_attempt("LOW", "failed_reported_comments"))),
        attempted_at="2026-08-10T17:00:00+00:00",
    )[2]
    assert _retry_row(first, "LOW") == {
        "identity": "LOW",
        "url": "https://www.instagram.com/p/LOW/",
        "state": "retry_pending",
        "failure_streak": 1,
    }

    success = deep_attempts.finalize_stage3_verdict(
        deep_attempts.prepare_stage3_attempt(first),
        ("advance", "collected", _candidate(**_comment_attempt("LOW", "collected"))),
        attempted_at="2026-08-10T18:00:00+00:00",
    )[2]
    assert _retry_row(success, "LOW")["state"] == "resolved"
    assert "LOW" not in _identities(
        deep_attempts.prepare_stage3_attempt(success).candidate.get(
            "comment_failed_posts", []
        )
    )

    failed_again = deep_attempts.finalize_stage3_verdict(
        deep_attempts.prepare_stage3_attempt(success),
        ("error", "comment_failed", _candidate(**_comment_attempt("LOW", "failed_reported_comments"))),
        attempted_at="2026-08-10T19:00:00+00:00",
    )[2]
    assert _retry_row(failed_again, "LOW")["failure_streak"] == 1
    prepared = deep_attempts.prepare_stage3_attempt(failed_again).candidate
    assert "LOW" in _identities(prepared["comment_failed_posts"])

    terminal = deep_attempts.finalize_stage3_verdict(
        deep_attempts.prepare_stage3_attempt(failed_again),
        ("advance", "collected", _candidate(**_comment_attempt("LOW", "unavailable_after_retry"))),
        attempted_at="2026-08-10T20:00:00+00:00",
    )[2]
    assert _retry_row(terminal, "LOW")["state"] == "terminal_unavailable"
    terminal_input = deep_attempts.prepare_stage3_attempt(terminal).candidate
    assert "LOW" in _identities(terminal_input["comment_unavailable_posts"])


def test_verified_empty_thread_resolves_only_with_full_provenance():
    valid = deep_attempts.finalize_stage3_verdict(
        deep_attempts.prepare_stage3_attempt({"handle": "creator"}),
        (
            "advance",
            "collected",
            _candidate(**_verified_empty_comment_attempt(valid=True)),
        ),
        attempted_at="2026-08-10T20:00:00+00:00",
    )[2]
    assert _retry_row(valid, "EMPTY")["state"] == "resolved"

    invalid = deep_attempts.finalize_stage3_verdict(
        deep_attempts.prepare_stage3_attempt({"handle": "creator"}),
        (
            "error",
            "comment_failed",
            _candidate(**_verified_empty_comment_attempt(valid=False)),
        ),
        attempted_at="2026-08-10T20:00:00+00:00",
    )[2]
    assert _retry_row(invalid, "EMPTY")["state"] == "retry_pending"


def test_same_window_merge_preserves_only_valid_verified_empty_thread():
    valid = _candidate(**_verified_empty_comment_attempt(valid=True))
    merged = deep_evidence_merge.merge_same_window(
        valid,
        deepcopy(valid),
        generated_at="2026-08-10T20:00:00+00:00",
    ).candidate
    assert merged["deep_collection_status"] == "complete"
    assert merged["sampled_posts"][0]["comment_sampling_status"] == (
        "verified_empty_thread"
    )
    assert merged["comment_unavailable_posts"][0]["reason"] == (
        comment_semantics.EMPTY_THREAD_REASON
    )

    invalid = _candidate(**_verified_empty_comment_attempt(valid=False))
    invalid_merged = deep_evidence_merge.merge_same_window(
        invalid,
        deepcopy(invalid),
        generated_at="2026-08-10T20:00:00+00:00",
    ).candidate
    assert invalid_merged["deep_collection_status"] == "incomplete"
    assert invalid_merged["sampled_posts"][0][
        "comment_sampling_status"
    ] == "failed_invalid_empty_thread_evidence"


def test_sampled_success_overrides_stale_aggregate_unavailable_state():
    evidence = _comment_attempt("RESET", "collected")
    evidence["comment_unavailable_posts"] = [
        {
            "url": "https://www.instagram.com/p/RESET/",
            "reported_count": 1,
            "reason": "stale aggregate",
        }
    ]
    saved = deep_attempts.finalize_stage3_verdict(
        deep_attempts.prepare_stage3_attempt({"handle": "creator"}),
        ("advance", "collected", _candidate(**evidence)),
        attempted_at="2026-08-10T20:30:00+00:00",
    )[2]

    assert _retry_row(saved, "RESET")["state"] == "resolved"
    assert _retry_row(saved, "RESET")["failure_streak"] == 0


def test_navigation_error_is_noop_and_high_count_failures_never_auto_terminal():
    first = deep_attempts.finalize_stage3_verdict(
        deep_attempts.prepare_stage3_attempt({"handle": "creator"}),
        ("error", "comment_failed", _candidate(**_comment_attempt("HIGH", "failed_reported_comments", reported_count=5))),
        attempted_at="2026-08-10T17:00:00+00:00",
    )[2]
    immutable_prefix = deepcopy(first[deep_attempts.LEDGER_FIELD])
    after_nav = deep_attempts.finalize_stage3_verdict(
        deep_attempts.prepare_stage3_attempt(first),
        ("error", "proxy_throttled"),
        attempted_at="2026-08-10T18:00:00+00:00",
    )[2]
    assert after_nav[deep_attempts.LEDGER_FIELD][
        : len(immutable_prefix)
    ] == immutable_prefix
    assert _retry_row(after_nav, "HIGH")["failure_streak"] == 1

    second = deep_attempts.finalize_stage3_verdict(
        deep_attempts.prepare_stage3_attempt(after_nav),
        ("error", "comment_failed", _candidate(**_comment_attempt("HIGH", "failed_reported_comments", reported_count=5))),
        attempted_at="2026-08-10T19:00:00+00:00",
    )[2]
    row = _retry_row(second, "HIGH")
    assert row["state"] == "retry_pending"
    assert row["failure_streak"] == 2

    prior_terminal = deepcopy(second)
    retry_row = _retry_row(prior_terminal, "HIGH")
    retry_row.update(state="terminal_unavailable", failure_streak=2)
    high_after_terminal = deep_attempts.finalize_stage3_verdict(
        deep_attempts.prepare_stage3_attempt(prior_terminal),
        ("error", "comment_failed", _candidate(**_comment_attempt("HIGH", "failed_reported_comments", reported_count=5))),
        attempted_at="2026-08-10T20:00:00+00:00",
    )[2]
    assert _retry_row(high_after_terminal, "HIGH")["state"] == "retry_pending"


def test_window_fingerprint_rejects_ambiguous_targets_and_keeps_positions():
    base = {
        "deep_target_posts": 3,
        "post_refs": [
            {"url": "/p/A/"},
            {"url": "/p/B/"},
            {"url": "/p/C/"},
        ],
    }
    reordered = deepcopy(base)
    reordered["post_refs"][0], reordered["post_refs"][1] = (
        reordered["post_refs"][1],
        reordered["post_refs"][0],
    )
    duplicate = deepcopy(base)
    duplicate["post_refs"][1] = {"url": "/reel/A/"}
    missing = deepcopy(base)
    missing["post_refs"][1] = {"pinned": False}

    assert deep_attempts.window_fingerprint(base)
    assert deep_attempts.window_fingerprint(reordered) != (
        deep_attempts.window_fingerprint(base)
    )
    assert deep_attempts.window_fingerprint(duplicate) is None
    assert deep_attempts.window_fingerprint(missing) is None


def test_explicit_zero_pricing_sample_count_does_not_fallback_to_raw_rows():
    evidence = {
        "pricing_reel_samples": [{"code": f"/reel/R{i}/"} for i in range(10)],
        "pricing_estimate": {"status": "partial", "sample_count": 0},
    }
    assert deep_attempts.summarize_quality(evidence)["pricing_samples"] == 0


def test_deep_and_pricing_windows_are_selected_independently():
    prior_evidence = _deep_evidence(posts=10, comments=90, complete=True)
    prior_evidence["pricing_captured_at"] = "2026-08-10T12:00:00"
    prior_evidence["pricing_estimate"] = pricing_policy.derive_quote_estimate(
        prior_evidence, load_config()
    )
    prior_evidence["pricing_estimate"]["marker"] = "old"
    prior = _candidate(**prior_evidence)
    context = deep_attempts.prepare_stage3_attempt(prior)
    current_evidence = _deep_evidence(posts=3, comments=20, complete=False)
    # 05:00Z is 13:00 Asia/Shanghai, newer than the naive local 12:00 above.
    current_evidence["pricing_captured_at"] = "2026-08-10T05:00:00+00:00"
    complete_pricing = _deep_evidence(posts=10, comments=20, complete=True)
    current_evidence["pricing_reel_samples"] = complete_pricing[
        "pricing_reel_samples"
    ]
    current_evidence["pricing_estimate"] = pricing_policy.derive_quote_estimate(
        current_evidence, load_config()
    )
    current_evidence["pricing_estimate"]["marker"] = "new"

    saved = deep_attempts.finalize_stage3_verdict(
        context,
        ("error", "deep_incomplete", _candidate(**current_evidence)),
        attempted_at="2026-08-10T21:00:00+00:00",
    )[2]
    ledger = saved[deep_attempts.LEDGER_FIELD]

    assert len(saved["sampled_posts"]) == 10
    assert len(saved["comment_records"]) == 90
    assert saved["pricing_estimate"]["marker"] == "new"
    assert saved["pricing_captured_at"] == "2026-08-10T05:00:00+00:00"
    assert saved[deep_attempts.CANONICAL_ATTEMPT_FIELD] == ledger[0]["attempt_id"]
    assert saved[deep_attempts.PRICING_CANONICAL_ATTEMPT_FIELD] == ledger[1]["attempt_id"]
    assert saved[deep_attempts.CANONICAL_QUALITY_FIELD]["pricing_samples"] == 10
    assert saved[deep_attempts.PRICING_CANONICAL_QUALITY_FIELD]["sample_count"] == 10
    assert deep_attempts.COMMENT_RETRY_STATE_FIELD in (
        deep_attempts.STAGE3_RESET_FIELDS
    )
    assert deep_attempts.LEDGER_FIELD in deep_attempts.STAGE3_RESET_FIELDS

    # A composed deep-old/pricing-new canonical must keep pointing to its two
    # original full owners.  The next retry must not materialize a redundant
    # full baseline merely because the independent pricing overlay differs.
    retried = deep_attempts.finalize_stage3_verdict(
        deep_attempts.prepare_stage3_attempt(saved),
        ("error", "proxy_throttled"),
        attempted_at="2026-08-10T22:00:00+00:00",
    )[2]
    assert len(retried[deep_attempts.LEDGER_FIELD]) == 3
    assert retried[deep_attempts.CANONICAL_ATTEMPT_FIELD] == ledger[0]["attempt_id"]
    assert retried[deep_attempts.PRICING_CANONICAL_ATTEMPT_FIELD] == (
        ledger[1]["attempt_id"]
    )


class _FakePage:
    def set_default_timeout(self, _value):
        pass

    def set_default_navigation_timeout(self, _value):
        pass


class _FakeContext:
    def __init__(self):
        self.pages = [_FakePage()]


class _FakePlaywright:
    def __enter__(self):
        return object()

    def __exit__(self, _exc_type, _exc, _tb):
        return False


def test_sigterm_unwinds_and_releases_pending_and_active_claims(monkeypatch):
    import browser_collect_v2 as browser
    import playwright.sync_api

    claims = [
        {
            "handle": handle,
            "_queue_lock_token": f"lock-{handle}",
            "_queue_from_status": "qualified",
        }
        for handle in ("active", "pending")
    ]
    released = []
    closed = []
    monkeypatch.setattr(browser, "load_accounts", lambda _path: [{"username": "a"}])
    monkeypatch.setattr(browser, "load_proxy", lambda session: None)
    monkeypatch.setattr(browser, "open_ctx", lambda *_args: _FakeContext())
    monkeypatch.setattr(browser, "close_ctx", lambda ctx: closed.append(ctx))
    monkeypatch.setattr(browser, "_pause", lambda *_args: None)
    monkeypatch.setattr(playwright.sync_api, "sync_playwright", lambda: _FakePlaywright())
    monkeypatch.setattr(_base.cc, "claim_queue", lambda *_args, **_kwargs: claims)
    monkeypatch.setattr(_base.cc, "refresh_queue_locks", lambda rows: (rows, []))
    monkeypatch.setattr(
        _base.cc,
        "release_queue_locks",
        lambda rows: released.extend(dict(row) for row in rows) or len(rows),
    )
    previous_handler = signal.getsignal(signal.SIGTERM)

    def terminate_inside_collector(_page, _candidate):
        signal.raise_signal(signal.SIGTERM)
        raise AssertionError("SIGTERM handler must unwind immediately")

    with pytest.raises(_base._StageTermination):  # noqa: SLF001
        _base.run_browser_stage(
            "qualified",
            "BATCH",
            0,
            False,
            terminate_inside_collector,
            heartbeat_seconds=999,
        )

    assert {row["handle"] for row in released} == {"active", "pending"}
    assert all(row.get("_queue_lock_token") for row in released)
    assert closed
    assert signal.getsignal(signal.SIGTERM) is previous_handler


def test_finalizer_failure_marks_only_candidate_error_with_prior_snapshot(
    monkeypatch,
):
    import browser_collect_v2 as browser
    import playwright.sync_api

    prior = _candidate(**_deep_evidence(posts=10, comments=90, complete=True))
    claim = {
        **prior,
        "_queue_lock_token": "owned-lock",
        "_queue_from_status": "qualified",
    }
    persisted = {}
    monkeypatch.setattr(browser, "load_accounts", lambda _path: [{"username": "a"}])
    monkeypatch.setattr(browser, "load_proxy", lambda session: None)
    monkeypatch.setattr(browser, "open_ctx", lambda *_args: _FakeContext())
    monkeypatch.setattr(browser, "close_ctx", lambda _ctx: None)
    monkeypatch.setattr(browser, "_pause", lambda *_args: None)
    monkeypatch.setattr(playwright.sync_api, "sync_playwright", lambda: _FakePlaywright())
    monkeypatch.setattr(_base.cc, "claim_queue", lambda *_args, **_kwargs: [claim])
    monkeypatch.setattr(_base.cc, "refresh_queue_locks", lambda rows: (rows, []))
    monkeypatch.setattr(_base.cc, "release_queue_locks", lambda rows: len(rows))
    monkeypatch.setattr(_base.cc, "status_dist", lambda _batch: {"qualified": 1})

    def mark_error(handle, error, candidate, **claim_kwargs):
        persisted.update(
            handle=handle,
            error=error,
            candidate=deepcopy(candidate),
            claim_kwargs=claim_kwargs,
        )
        return True

    monkeypatch.setattr(_base.cc, "mark_error", mark_error)
    monkeypatch.setattr(
        _base,
        "finalize_stage3_verdict",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    def regress(_page, candidate):
        candidate.update(_deep_evidence(posts=3, comments=20, complete=False))
        return ("error", "deep_incomplete", candidate)

    counts = _base.run_browser_stage(
        "qualified",
        "BATCH",
        1,
        False,
        regress,
        heartbeat_seconds=999,
    )

    assert counts == {"advance": 0, "reject": 0, "error": 1}
    assert persisted["error"].startswith("attempt_finalize_failed:RuntimeError")
    assert len(persisted["candidate"]["sampled_posts"]) == 10
    assert len(persisted["candidate"]["comment_records"]) == 90
    assert persisted["claim_kwargs"] == {
        "expected_status": "qualified",
        "lock_token": "owned-lock",
    }
    assert "_queue_lock_token" not in json.dumps(persisted["candidate"])
