from __future__ import annotations

import copy
import hashlib
import json
import sqlite3

import pytest

from extensions.sop_v2 import comment_translation
from extensions.sop_v2 import pricing as pricing_policy
from extensions.sop_v2.config import load_config
from extensions.sop_v2.pipeline import deep_evidence_merge
from extensions.sop_v2.pipeline import recover_deep_evidence


BATCH = "TEST-RECOVERY"
PROVIDER = "custom"
MODEL = "cached-v1"


def _source_hash(text: str) -> str:
    normalized = " ".join(text.split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _translation(text: str, translated: str) -> dict:
    return {
        "source_hash": _source_hash(text),
        "original_text": text,
        "translated_text": translated,
        "translated_zh": translated,
        "source_language": "fr",
        "translation_status": "translated",
        "status": "translated",
        "translation_error": None,
        "provider": PROVIDER,
        "model": MODEL,
        "translator_version": comment_translation.SCHEMA_VERSION,
    }


def _candidate(
    handle: str,
    *,
    window_prefix: str,
    post_indexes: range | list[int],
    label: str,
    captured_day: int,
    pricing_status: str = "partial",
    pricing_samples: int = 5,
) -> dict:
    codes = [f"/p/{window_prefix}{index}/" for index in range(10)]
    posts = []
    records = []
    translations = []
    for index in post_indexes:
        code = codes[index]
        url = f"https://www.instagram.com{code}"
        text = f"commentaire {label} {index}"
        posts.append(
            {
                "code": code,
                "url": url,
                "caption": f"skincare serum {index}",
                "is_video": index % 2 == 0,
                "is_reel": index % 2 == 0,
                "like_count": 100 + index,
                "comment_count": 5,
                "metrics_status": "observed",
                "comment_sampling_status": "collected",
                "comments_collected": 1,
                "captured_at": f"2026-08-{captured_day:02d}T12:00:{index:02d}+00:00",
            }
        )
        records.append({"username": f"u_{label}_{index}", "text": text, "post_url": url})
        translations.append(_translation(text, f"我想买这个产品 {label} {index}"))
    complete = len(posts) == 10
    candidate = {
        "handle": handle,
        "full_name": "Skin Creator",
        "biography": "skincare reviews",
        "follower_count": 10_000,
        "post_refs": [{"url": code, "pinned": False} for code in codes],
        "codes": codes,
        "sampled_posts": posts,
        "deep_target_posts": 10,
        "deep_available_posts": 10,
        "deep_successful_posts": len(posts),
        "deep_failed_posts": [
            f"https://www.instagram.com{codes[index]}"
            for index in range(10)
            if index not in post_indexes
        ],
        "deep_metric_missing_posts": [],
        "comment_attempted_posts": len(posts),
        "comment_completed_posts": len(posts),
        "comment_failed_posts": [],
        "comment_unavailable_posts": [],
        "deep_collection_status": "complete" if complete else "incomplete",
        "comments_read": True,
        "comment_records": records,
        "comment_sample": [row["text"] for row in records],
        "comments_analyzed": len(records),
        "valid_comments": len(records),
        "low_quality_ratio": 0.0,
        "high_intent_count": 0,
        "high_intent_ratio": 0.0,
        "comment_translations": translations,
        "comment_translation_summary": {
            "status": "complete",
            "provider": PROVIDER,
            "model": MODEL,
            "requested_count": len(records),
            "translated_count": len(records),
            "failed_count": 0,
            "semantic_coverage_complete": True,
        },
        "pricing_reel_samples": [
            {
                "code": f"/reel/PRICE{index}/",
                "is_reel": True,
                "pinned": False,
                "play_count": 1_000 + index,
                "play_count_status": "observed",
                "play_count_source": "ig_media_info.ig_play_count",
            }
            for index in range(pricing_samples)
        ],
        "pricing_captured_at": f"2026-08-{captured_day:02d}T13:00:00+00:00",
        "evidence_dir": f"data/evidence/{BATCH}/{handle}",
        "core_niche_key": "skincare",
    }
    candidate["pricing_estimate"] = pricing_policy.derive_quote_estimate(
        candidate, load_config()
    )
    assert candidate["pricing_estimate"]["status"] == pricing_status
    return candidate


def _create_db(path, rows: list[tuple[str, dict, str | None]]) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE creator_profiles (
                handle TEXT PRIMARY KEY,
                stage_json TEXT,
                status TEXT,
                stage_error TEXT,
                locked_at TEXT,
                stage_updated_at TEXT,
                times_seen INTEGER,
                discovery_batch TEXT,
                source_batch TEXT,
                tier INTEGER,
                client_status TEXT,
                approved_at TEXT,
                rejected_reason TEXT,
                client_rejection_scope TEXT,
                client_note TEXT,
                reject_reason TEXT,
                real_er REAL,
                high_intent_count INTEGER,
                evidence_dir TEXT,
                core_niche_key TEXT
            )
            """
        )
        for handle, stage, stage_error in rows:
            conn.execute(
                """
                INSERT INTO creator_profiles (
                    handle,stage_json,status,stage_error,locked_at,stage_updated_at,
                    times_seen,discovery_batch,source_batch,tier,client_status,
                    approved_at,rejected_reason,client_rejection_scope,client_note,
                    reject_reason,real_er,high_intent_count,evidence_dir,core_niche_key
                ) VALUES (?,?, 'qualified', ?, NULL, '2026-08-10T10:00:00',
                          7, ?, 'source-a', 1, NULL, NULL, NULL, NULL,
                          'keep-client-note', NULL, ?, ?, ?, ?)
                """,
                (
                    handle,
                    json.dumps(stage, ensure_ascii=False),
                    stage_error,
                    BATCH,
                    stage.get("real_er"),
                    stage.get("high_intent_count"),
                    stage.get("evidence_dir"),
                    stage.get("core_niche_key"),
                ),
            )


def _read_row(path, handle: str) -> sqlite3.Row:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT * FROM creator_profiles WHERE handle=?", (handle,)
        ).fetchone()
    finally:
        conn.close()


def test_same_window_complementary_eight_and_nine_recovers_ten(monkeypatch):
    old = _candidate(
        "sabs_like",
        window_prefix="S",
        post_indexes=list(range(1, 10)),
        label="old",
        captured_day=8,
        pricing_status="complete",
        pricing_samples=10,
    )
    current = _candidate(
        "sabs_like",
        window_prefix="S",
        post_indexes=list(range(8)),
        label="new",
        captured_day=9,
    )
    old_s1 = next(post for post in old["sampled_posts"] if post["code"] == "/p/S1/")
    current_s1 = next(
        post for post in current["sampled_posts"] if post["code"] == "/p/S1/"
    )
    old_s1.update({"like_count": 1_000, "comment_count": 100})
    current_s1.update({"like_count": 900, "comment_count": 90})
    for row in current["comment_translations"]:
        row["provider"] = "ollama"
        row["model"] = "qwen-test"

    def network_forbidden(*_args, **_kwargs):
        raise AssertionError("recovery must not call a translation network")

    monkeypatch.setattr(comment_translation, "urlopen", network_forbidden)
    result = deep_evidence_merge.merge_same_window(
        old,
        current,
        old_attempt_id="attempt-old",
        current_attempt_id="attempt-current",
        generated_at="2026-08-10T00:00:00+00:00",
    )

    assert result.mode == "same_window_media_merge"
    assert result.candidate["deep_collection_status"] == "complete"
    assert result.candidate["deep_successful_posts"] == 10
    assert result.candidate["deep_failed_posts"] == []
    merged_s1 = next(
        post for post in result.candidate["sampled_posts"] if post["code"] == "/p/S1/"
    )
    assert (merged_s1["like_count"], merged_s1["comment_count"]) == (900, 90)
    assert len(result.candidate["comment_records"]) == 17
    assert result.candidate["comment_translation_summary"]["translated_count"] == 17
    assert result.candidate["comment_translation_summary"]["cache_reuse"] == (
        "valid_cached_union"
    )
    assert len(result.candidate["comment_translation_summary"]["cache_source_pairs"]) == 2
    assert result.candidate["pricing_estimate"]["status"] == "complete"
    provenance = result.provenance
    assert provenance["sources"] == {
        "old": {"kind": "attempt", "attempt_id": "attempt-old"},
        "current": {"kind": "attempt", "attempt_id": "attempt-current"},
    }
    assert provenance["source_quality"]["old"]["selection_tuple"]


def test_yas_like_apply_restores_ten_and_unions_comments(tmp_path):
    target = tmp_path / "target.db"
    backup = tmp_path / "backup.db"
    handle = "yas_like"
    current = _candidate(
        handle,
        window_prefix="Y",
        post_indexes=list(range(3)),
        label="new",
        captured_day=10,
        pricing_status="partial",
        pricing_samples=3,
    )
    old = _candidate(
        handle,
        window_prefix="Y",
        post_indexes=list(range(10)),
        label="old",
        captured_day=8,
        pricing_status="complete",
        pricing_samples=10,
    )
    retry_state = [
        {
            "identity": "Y9",
            "url": "https://www.instagram.com/p/Y9/",
            "state": "retry_pending",
            "failure_streak": 1,
        }
    ]
    current["stage3_comment_retry_state"] = copy.deepcopy(retry_state)
    _create_db(target, [(handle, current, "deep_incomplete:3/10")])
    _create_db(backup, [(handle, old, None)])
    protected_before = dict(_read_row(target, handle))
    plan = recover_deep_evidence.build_plan(
        db=target, backup_db=backup, batch_id=BATCH, handles=[handle]
    )

    audit = recover_deep_evidence.recover(
        db=target,
        backup_db=backup,
        batch_id=BATCH,
        handles=[handle],
        apply=True,
        expected_plan_sha256=plan.plan_sha256,
    )

    row = _read_row(target, handle)
    recovered = json.loads(row["stage_json"])
    assert audit["mode"] == "applied"
    assert recovered["deep_successful_posts"] == 10
    assert recovered["deep_collection_status"] == "complete"
    assert len(recovered["comment_records"]) == 13
    assert len(recovered["comment_translations"]) == 13
    assert len(recovered["deep_collection_attempts"]) >= 2
    assert sum(
        entry.get("event_kind") == "synthetic_evidence_recovery"
        for entry in recovered["deep_collection_attempts"]
    ) == 1
    assert recovered["pricing_estimate"]["status"] == "complete"
    attempt_ids = {
        entry["attempt_id"] for entry in recovered["deep_collection_attempts"]
    }
    assert recovered["deep_canonical_attempt_id"] in attempt_ids
    assert recovered["pricing_canonical_attempt_id"] in attempt_ids
    assert recovered["deep_canonical_quality"]["selection_tuple"]
    assert recovered["pricing_canonical_quality"]["selection_tuple"]
    assert isinstance(recovered["stage3_comment_retry_state"], list)
    assert recovered["stage3_comment_retry_state"] == retry_state
    assert "merge_provenance" in recovered["deep_collection_attempts"][-1]
    assert "deep_evidence_merge_provenance" not in recovered
    for column in recover_deep_evidence.PROTECTED_COLUMNS:
        assert row[column] == protected_before[column]


def test_apply_then_second_dry_run_is_idempotent(tmp_path):
    target = tmp_path / "target.db"
    backup = tmp_path / "backup.db"
    handles = ("sabs_repeat", "yas_repeat")
    current_rows = []
    backup_rows = []
    for handle, indexes in zip(handles, (list(range(8)), list(range(3)))):
        current_rows.append(
            (
                handle,
                _candidate(
                    handle,
                    window_prefix=handle.upper(),
                    post_indexes=indexes,
                    label="new",
                    captured_day=10,
                ),
                "deep_incomplete",
            )
        )
        backup_rows.append(
            (
                handle,
                _candidate(
                    handle,
                    window_prefix=handle.upper(),
                    post_indexes=list(range(1, 10)) if handle.startswith("sabs") else list(range(10)),
                    label="old",
                    captured_day=8,
                    pricing_status="complete",
                    pricing_samples=10,
                ),
                None,
            )
        )
    _create_db(target, current_rows)
    _create_db(backup, backup_rows)
    first = recover_deep_evidence.build_plan(
        db=target, backup_db=backup, batch_id=BATCH, handles=handles
    )
    recover_deep_evidence.apply_plan(
        first, expected_plan_sha256=first.plan_sha256
    )
    after_first = {handle: _read_row(target, handle)["stage_json"] for handle in handles}

    second = recover_deep_evidence.build_plan(
        db=target, backup_db=backup, batch_id=BATCH, handles=handles
    )

    assert all(not row.write_required for row in second.rows)
    assert all(row.audit["action"] == "skip_no_change" for row in second.rows)
    assert {handle: _read_row(target, handle)["stage_json"] for handle in handles} == after_first


def test_window_mismatch_uses_one_whole_attempt_without_field_merge(tmp_path):
    target = tmp_path / "target.db"
    backup = tmp_path / "backup.db"
    handle = "window_mismatch"
    current = _candidate(
        handle,
        window_prefix="CURRENT",
        post_indexes=list(range(9)),
        label="new",
        captured_day=10,
    )
    old = _candidate(
        handle,
        window_prefix="OLD",
        post_indexes=list(range(10)),
        label="old",
        captured_day=8,
    )
    _create_db(target, [(handle, current, "deep_incomplete:9/10")])
    _create_db(backup, [(handle, old, None)])
    plan = recover_deep_evidence.build_plan(
        db=target, backup_db=backup, batch_id=BATCH, handles=[handle]
    )

    recover_deep_evidence.recover(
        db=target,
        backup_db=backup,
        batch_id=BATCH,
        handles=[handle],
        apply=True,
        expected_plan_sha256=plan.plan_sha256,
    )
    recovered = json.loads(_read_row(target, handle)["stage_json"])

    assert [
        deep_evidence_merge.media_identity(post)
        for post in recovered["sampled_posts"]
    ] == [f"OLD{index}" for index in range(10)]
    assert len(recovered["comment_records"]) == 10
    assert not any(
        "new" in row["text"] for row in recovered["comment_records"]
    )
    assert "deep_evidence_merge_provenance" not in recovered


def test_truncated_same_prefix_is_not_a_valid_same_window():
    old = _candidate(
        "truncated",
        window_prefix="T",
        post_indexes=list(range(3)),
        label="old",
        captured_day=8,
    )
    current = _candidate(
        "truncated",
        window_prefix="T",
        post_indexes=list(range(3)),
        label="new",
        captured_day=10,
    )
    old["post_refs"] = old["post_refs"][:3]
    old["codes"] = old["codes"][:3]
    current["post_refs"] = current["post_refs"][:3]
    current["codes"] = current["codes"][:3]
    # Both still claim ten available posts, so the common three-item prefix is
    # a truncated descriptor, not an exact target window.
    assert old["deep_available_posts"] == current["deep_available_posts"] == 10
    assert deep_evidence_merge.windows_match(old, current) is False
    result = deep_evidence_merge.merge_evidence(old, current)
    assert result.mode == "whole_attempt_selection"
    assert len(result.candidate["sampled_posts"]) == 3


def test_stage_json_cas_drift_rolls_back_all_rows(tmp_path):
    target = tmp_path / "target.db"
    backup = tmp_path / "backup.db"
    rows_current = []
    rows_old = []
    for handle in ("first", "second"):
        rows_current.append(
            (
                handle,
                _candidate(
                    handle,
                    window_prefix=handle.upper(),
                    post_indexes=list(range(3)),
                    label="new",
                    captured_day=10,
                ),
                "incomplete",
            )
        )
        rows_old.append(
            (
                handle,
                _candidate(
                    handle,
                    window_prefix=handle.upper(),
                    post_indexes=list(range(10)),
                    label="old",
                    captured_day=8,
                ),
                None,
            )
        )
    _create_db(target, rows_current)
    _create_db(backup, rows_old)
    plan = recover_deep_evidence.build_plan(
        db=target,
        backup_db=backup,
        batch_id=BATCH,
        handles=["first", "second"],
    )
    first_before = _read_row(target, "first")["stage_json"]
    second = json.loads(_read_row(target, "second")["stage_json"])
    second["concurrent_writer"] = True
    with sqlite3.connect(target) as conn:
        conn.execute(
            "UPDATE creator_profiles SET stage_json=? WHERE handle='second'",
            (json.dumps(second),),
        )

    with pytest.raises(recover_deep_evidence.RecoveryError, match="content CAS"):
        recover_deep_evidence.apply_plan(
            plan, expected_plan_sha256=plan.plan_sha256
        )

    assert _read_row(target, "first")["stage_json"] == first_before
    assert json.loads(_read_row(target, "second")["stage_json"])[
        "concurrent_writer"
    ] is True


def test_config_drift_refuses_apply_without_writes(tmp_path, monkeypatch):
    target = tmp_path / "target.db"
    backup = tmp_path / "backup.db"
    handle = "config_drift"
    current = _candidate(
        handle,
        window_prefix="CFG",
        post_indexes=list(range(3)),
        label="new",
        captured_day=10,
    )
    old = _candidate(
        handle,
        window_prefix="CFG",
        post_indexes=list(range(10)),
        label="old",
        captured_day=8,
    )
    _create_db(target, [(handle, current, "incomplete")])
    _create_db(backup, [(handle, old, None)])
    plan = recover_deep_evidence.build_plan(
        db=target,
        backup_db=backup,
        batch_id=BATCH,
        handles=[handle],
    )
    before = _read_row(target, handle)["stage_json"]
    monkeypatch.setattr(recover_deep_evidence, "config_sha256", lambda: "drift")

    with pytest.raises(recover_deep_evidence.RecoveryError, match="config SHA-256"):
        recover_deep_evidence.apply_plan(
            plan, expected_plan_sha256=plan.plan_sha256
        )
    assert _read_row(target, handle)["stage_json"] == before


def test_reviewed_plan_hash_changes_when_stage_error_changes(tmp_path):
    target = tmp_path / "target.db"
    backup = tmp_path / "backup.db"
    handle = "reviewed_cas"
    current = _candidate(
        handle,
        window_prefix="CAS",
        post_indexes=list(range(3)),
        label="new",
        captured_day=10,
    )
    old = _candidate(
        handle,
        window_prefix="CAS",
        post_indexes=list(range(10)),
        label="old",
        captured_day=8,
    )
    _create_db(target, [(handle, current, "first-error")])
    _create_db(backup, [(handle, old, None)])
    reviewed = recover_deep_evidence.build_plan(
        db=target, backup_db=backup, batch_id=BATCH, handles=[handle]
    )
    with sqlite3.connect(target) as conn:
        conn.execute(
            "UPDATE creator_profiles SET stage_error=? WHERE handle=?",
            ("different-error", handle),
        )

    rebuilt = recover_deep_evidence.build_plan(
        db=target, backup_db=backup, batch_id=BATCH, handles=[handle]
    )
    assert rebuilt.plan_sha256 != reviewed.plan_sha256
    with pytest.raises(recover_deep_evidence.RecoveryError, match="expected plan"):
        recover_deep_evidence.apply_plan(
            rebuilt, expected_plan_sha256=reviewed.plan_sha256
        )


def test_valid_whole_current_noop_adds_no_ledger_event(tmp_path):
    target = tmp_path / "target.db"
    backup = tmp_path / "backup.db"
    handle = "diana_like"
    current_evidence = _candidate(
        handle,
        window_prefix="CURRENT",
        post_indexes=list(range(10)),
        label="current",
        captured_day=10,
        pricing_status="complete",
        pricing_samples=10,
    )
    current = current_evidence
    old = _candidate(
        handle,
        window_prefix="OLD",
        post_indexes=list(range(3)),
        label="old",
        captured_day=8,
    )
    _create_db(target, [(handle, current, "still_retryable")])
    _create_db(backup, [(handle, old, None)])
    before = _read_row(target, handle)["stage_json"]

    plan = recover_deep_evidence.build_plan(
        db=target, backup_db=backup, batch_id=BATCH, handles=[handle]
    )
    assert plan.rows[0].write_required is False
    assert plan.rows[0].audit["action"] == "skip_no_change"
    recover_deep_evidence.apply_plan(
        plan, expected_plan_sha256=plan.plan_sha256
    )

    after = _read_row(target, handle)["stage_json"]
    assert after == before
    assert "deep_collection_attempts" not in json.loads(after)


def test_one_invalid_source_row_prevents_every_write(tmp_path):
    target = tmp_path / "target.db"
    backup = tmp_path / "backup.db"
    valid_current = _candidate(
        "valid",
        window_prefix="V",
        post_indexes=list(range(3)),
        label="new",
        captured_day=10,
    )
    invalid_current = copy.deepcopy(valid_current)
    invalid_current["handle"] = "invalid"
    valid_old = _candidate(
        "valid",
        window_prefix="V",
        post_indexes=list(range(10)),
        label="old",
        captured_day=8,
    )
    invalid_old = _candidate(
        "invalid",
        window_prefix="I",
        post_indexes=list(range(10)),
        label="old",
        captured_day=8,
    )
    _create_db(
        target,
        [("valid", valid_current, "incomplete"), ("invalid", invalid_current, "incomplete")],
    )
    _create_db(backup, [("valid", valid_old, None), ("invalid", invalid_old, None)])
    valid_before = _read_row(target, "valid")["stage_json"]
    with sqlite3.connect(backup) as conn:
        conn.execute(
            "UPDATE creator_profiles SET stage_json='not-json' WHERE handle='invalid'"
        )

    with pytest.raises(recover_deep_evidence.RecoveryError, match="invalid stage_json"):
        recover_deep_evidence.recover(
            db=target,
            backup_db=backup,
            batch_id=BATCH,
            handles=["valid", "invalid"],
            apply=True,
            expected_plan_sha256="0" * 64,
        )
    assert _read_row(target, "valid")["stage_json"] == valid_before


def test_dry_run_and_audit_output_do_not_write_database(tmp_path):
    target = tmp_path / "target.db"
    backup = tmp_path / "backup.db"
    audit_path = tmp_path / "audit.json"
    handle = "dry_run"
    current = _candidate(
        handle,
        window_prefix="D",
        post_indexes=list(range(3)),
        label="new",
        captured_day=10,
    )
    old = _candidate(
        handle,
        window_prefix="D",
        post_indexes=list(range(10)),
        label="old",
        captured_day=8,
    )
    _create_db(target, [(handle, current, "incomplete")])
    _create_db(backup, [(handle, old, None)])
    before = target.read_bytes()

    result = recover_deep_evidence.recover(
        db=target,
        backup_db=backup,
        batch_id=BATCH,
        handles=[handle],
        audit_output=audit_path,
    )

    assert result["mode"] == "dry_run"
    assert target.read_bytes() == before
    artifact = json.loads(audit_path.read_text())
    assert artifact["rows"][0]["before_stage_json_sha256"]
    assert artifact["rows"][0]["after_stage_json_sha256"]
    serialized = audit_path.read_text()
    assert "commentaire" not in serialized
    assert "translated_text" not in serialized
