"""The formal DAG barriers are deterministic, strict, and read-only."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from extensions.sop_v2.pipeline import barriers


BATCH = "B-GRAPH"
SOURCE_COUNTS = {
    "golden_lookalike": 1,
    "generic_commerce": 1,
    "exploration": 1,
}


def _create_db(tmp_path: Path, rows: list[dict]) -> Path:
    db = tmp_path / "creator_cache.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """CREATE TABLE creator_profiles (
            handle TEXT PRIMARY KEY,
            status TEXT,
            stage_error TEXT,
            locked_at TEXT,
            discovery_batch TEXT,
            stage_json TEXT
        )"""
    )
    conn.executemany(
        "INSERT INTO creator_profiles "
        "(handle,status,stage_error,locked_at,discovery_batch,stage_json) "
        "VALUES (:handle,:status,:stage_error,:locked_at,:discovery_batch,:stage_json)",
        [
            {
                "stage_error": None,
                "locked_at": None,
                "discovery_batch": BATCH,
                "stage_json": json.dumps(
                    {
                        "handle": row["handle"],
                        "discovered_via": row.get(
                            "discovered_via",
                            "modash_manual_lookalike:golden:seed",
                        ),
                    }
                ),
                **row,
            }
            for row in rows
        ],
    )
    conn.commit()
    conn.close()
    return db


def _base_rows(status: str = "seed") -> list[dict]:
    return [
        {
            "handle": "golden_one",
            "status": status,
            "discovered_via": "modash_manual_lookalike:golden:approved_seed",
        },
        {
            "handle": "generic_one",
            "status": status,
            "discovered_via": "modash_structured_search:generic_commerce",
        },
        {
            "handle": "explore_one",
            "status": status,
            "discovered_via": "modash_structured_search:exploration",
        },
    ]


def _write_contract(tmp_path: Path) -> tuple[Path, str]:
    contract = {
        "schema_version": 1,
        "batch_id": BATCH,
        "sources": {
            "quota_pct": {
                "golden_lookalike": 50,
                "generic_commerce": 30,
                "exploration": 20,
            }
        },
    }
    path = tmp_path / "round_contract.json"
    rendered = json.dumps(contract, ensure_ascii=False, indent=2) + "\n"
    path.write_text(rendered, encoding="utf-8")
    return path, hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def _artifact(handles: list[str], contract_sha: str = "a" * 64) -> dict:
    return {
        "schema_version": 1,
        "batch_id": BATCH,
        "target_total": len(handles),
        "round_contract_sha256": contract_sha,
        "handle_set_sha256": barriers.handle_set_sha256(handles),
        "source_counts": SOURCE_COUNTS,
        "ingest": {
            "new_seeds": len(handles),
            "audited_new": len(handles),
            "deduped": 0,
            "rejected_skipped": 0,
        },
    }


def _check(result: barriers.BarrierResult, name: str) -> barriers.BarrierCheck:
    return next(check for check in result.checks if check.name == name)


def test_b1_passes_only_for_complete_bound_atomic_seed_cohort(tmp_path):
    rows = _base_rows()
    db = _create_db(tmp_path, rows)
    contract_path, contract_sha = _write_contract(tmp_path)

    result = barriers.evaluate_b1(
        db,
        round_contract=contract_path,
        stage1_artifact=_artifact([row["handle"] for row in rows], contract_sha),
    )

    assert result.passed is True
    assert result.failures == ()
    assert result.barrier == "B1"
    assert result.observed["source_counts"] == SOURCE_COUNTS
    assert result.as_dict()["passed"] is True


def test_b1_detects_same_size_cohort_substitution_and_bad_ingest_artifact(tmp_path):
    rows = _base_rows()
    db = _create_db(tmp_path, rows)
    contract_path, contract_sha = _write_contract(tmp_path)
    artifact = _artifact(["golden_one", "generic_one", "different_handle"], contract_sha)
    artifact["ingest"]["audited_new"] = 2

    result = barriers.evaluate_b1(
        db,
        round_contract=contract_path,
        stage1_artifact=artifact,
    )

    assert result.passed is False
    assert "cohort_handle_fingerprint_matches_artifact" in result.failures
    assert "stage1_audited_new_matches_target" in result.failures
    # Count alone still matches, proving the fingerprint is a distinct predicate.
    assert _check(result, "cohort_total_matches_target").passed is True


def test_b1_mapping_input_uses_explicit_original_contract_fingerprint(tmp_path):
    rows = _base_rows()
    db = _create_db(tmp_path, rows)
    contract_path, contract_sha = _write_contract(tmp_path)
    contract = json.loads(contract_path.read_text(encoding="utf-8"))

    result = barriers.evaluate_b1(
        db,
        round_contract=contract,
        round_contract_sha256=contract_sha,
        stage1_artifact=_artifact([row["handle"] for row in rows], contract_sha),
    )

    assert result.passed is True


def test_b2_allows_qualified_deep_error_and_lock(tmp_path):
    rows = _base_rows()
    rows[0]["status"] = "qualified"
    rows[0]["stage_error"] = "deep_incomplete: comments"
    rows[1]["status"] = "qualified"
    rows[1]["locked_at"] = "stage3-worker-token"
    rows[2]["status"] = "collected"
    db = _create_db(tmp_path, rows)
    artifact = _artifact([row["handle"] for row in rows])

    result = barriers.evaluate_b2(
        db,
        stage1_artifact=artifact,
        producer_exited=True,
    )

    assert result.passed is True
    assert result.observed["error_count"] == 1
    assert result.observed["lock_count"] == 1
    assert _check(result, "seed_stage_errors_zero").passed is True
    assert _check(result, "seed_locks_zero").passed is True
    assert not any(check.name == "batch_stage_errors_zero" for check in result.checks)
    assert not any(check.name == "batch_locks_zero" for check in result.checks)


def test_b2_blocks_until_producer_exits_and_seed_workset_is_closed(tmp_path):
    rows = _base_rows(status="qualified")
    rows[0]["status"] = "seed"
    rows[0]["stage_error"] = "profile_fetch_failed"
    rows[0]["locked_at"] = "stage2-worker-token"
    db = _create_db(tmp_path, rows)
    artifact = _artifact([row["handle"] for row in rows])

    result = barriers.evaluate_b2(db, stage1_artifact=artifact)

    assert result.passed is False
    assert {
        "stage2_producer_exited",
        "seed_queue_empty",
        "stage2_outputs_match_target",
        "stage2_other_statuses_zero",
        "seed_stage_errors_zero",
        "seed_locks_zero",
    }.issubset(result.failures)


def test_b3_unknown_external_results_block_even_when_database_is_complete(tmp_path):
    rows = _base_rows(status="collected")
    db = _create_db(tmp_path, rows)
    artifact = _artifact([row["handle"] for row in rows])

    result = barriers.evaluate_b3(db, stage1_artifact=artifact)

    assert result.passed is False
    assert {
        "external_pricing_failures_zero",
        "external_translation_failures_zero",
        "external_audit_failures_zero",
    }.issubset(result.failures)
    assert result.observed["failure_counts"] == {
        "audit": None,
        "pricing": None,
        "translation": None,
    }


def test_b3_passes_with_complete_db_and_all_injected_counters_zero(tmp_path):
    rows = _base_rows(status="collected")
    db = _create_db(tmp_path, rows)
    artifact = _artifact([row["handle"] for row in rows])

    result = barriers.evaluate_b3(
        db,
        stage1_artifact=artifact,
        failure_counts={
            "pricing": 0,
            "translation": 0,
            "audit": 0,
            "evidence_index": 0,
        },
    )

    assert result.passed is True
    assert result.failures == ()
    assert _check(result, "external_evidence_index_failures_zero").passed is True


def test_b3_checks_queue_errors_locks_statuses_and_external_failures(tmp_path):
    rows = _base_rows(status="collected")
    rows[0]["status"] = "qualified"
    rows[0]["stage_error"] = "deep_incomplete"
    rows[0]["locked_at"] = "worker-token"
    db = _create_db(tmp_path, rows)
    artifact = _artifact([row["handle"] for row in rows])

    result = barriers.evaluate_b3(
        db,
        stage1_artifact=artifact,
        failure_counts={"pricing": 1, "translation": 2, "audit": 3},
    )

    assert result.passed is False
    assert {
        "qualified_queue_empty",
        "all_candidates_collected",
        "non_collected_statuses_zero",
        "batch_stage_errors_zero",
        "batch_locks_zero",
        "external_pricing_failures_zero",
        "external_translation_failures_zero",
        "external_audit_failures_zero",
    }.issubset(result.failures)


def test_b4_passes_only_for_fully_decided_enriched_delivery(tmp_path):
    rows = _base_rows(status="decided")
    db = _create_db(tmp_path, rows)
    artifact = _artifact([row["handle"] for row in rows])
    validated_state = barriers._read_batch_snapshot(db, BATCH)[
        "delivery_state_sha256"
    ]

    result = barriers.evaluate_b4(
        db,
        stage1_artifact=artifact,
        failure_counts={
            "pricing": 0,
            "translation": 0,
            "audit": 0,
            "modash": 0,
            "storefront": 0,
            "sponsorship": 0,
        },
        validated_delivery_state_sha256=validated_state,
    )

    assert result.passed is True
    assert result.barrier == "B4"
    assert result.failures == ()
    assert _check(result, "all_candidates_decided").passed is True


def test_b4_fails_closed_for_unknown_enrichment_and_non_decided_rows(tmp_path):
    rows = _base_rows(status="decided")
    rows[0]["status"] = "collected"
    rows[1]["stage_error"] = "storefront_unresolved"
    rows[2]["locked_at"] = "delivery-worker-token"
    db = _create_db(tmp_path, rows)
    artifact = _artifact([row["handle"] for row in rows])

    result = barriers.evaluate_b4(
        db,
        stage1_artifact=artifact,
        failure_counts={"pricing": 0, "translation": 0, "audit": 0},
        validated_delivery_state_sha256=None,
    )

    assert result.passed is False
    assert {
        "all_candidates_decided",
        "non_decided_statuses_zero",
        "batch_stage_errors_zero",
        "batch_locks_zero",
        "external_modash_failures_zero",
        "external_storefront_failures_zero",
        "external_sponsorship_failures_zero",
        "validated_delivery_state_matches_current",
    }.issubset(result.failures)


def test_b4_rejects_stale_external_validation_snapshot(tmp_path):
    rows = _base_rows(status="decided")
    db = _create_db(tmp_path, rows)
    artifact = _artifact([row["handle"] for row in rows])
    validated_state = barriers._read_batch_snapshot(db, BATCH)[
        "delivery_state_sha256"
    ]

    conn = sqlite3.connect(db)
    stage = json.loads(
        conn.execute(
            "SELECT stage_json FROM creator_profiles WHERE handle='golden_one'"
        ).fetchone()[0]
    )
    stage["storefront_status"] = "confirmed_yes"
    conn.execute(
        "UPDATE creator_profiles SET stage_json=? WHERE handle='golden_one'",
        (json.dumps(stage),),
    )
    conn.commit()
    conn.close()

    result = barriers.evaluate_b4(
        db,
        stage1_artifact=artifact,
        validated_delivery_state_sha256=validated_state,
        failure_counts={key: 0 for key in barriers.REQUIRED_B4_FAILURE_KEYS},
    )

    assert result.passed is False
    assert "validated_delivery_state_matches_current" in result.failures


def test_all_evaluators_leave_database_bytes_unchanged(tmp_path):
    rows = _base_rows(status="collected")
    db = _create_db(tmp_path, rows)
    contract_path, contract_sha = _write_contract(tmp_path)
    artifact = _artifact([row["handle"] for row in rows], contract_sha)
    before = hashlib.sha256(db.read_bytes()).hexdigest()

    # B1 is expected to fail because the rows have already advanced, but evaluation
    # itself must remain side-effect free.
    barriers.evaluate_b1(
        db,
        round_contract=contract_path,
        stage1_artifact=artifact,
    )
    barriers.evaluate_b2(db, stage1_artifact=artifact, producer_exited=True)
    barriers.evaluate_b3(
        db,
        stage1_artifact=artifact,
        failure_counts={"pricing": 0, "translation": 0, "audit": 0},
    )
    barriers.evaluate_b4(
        db,
        stage1_artifact=artifact,
        failure_counts={
            "pricing": 0,
            "translation": 0,
            "audit": 0,
            "modash": 0,
            "storefront": 0,
            "sponsorship": 0,
        },
        validated_delivery_state_sha256=barriers._read_batch_snapshot(db, BATCH)[
            "delivery_state_sha256"
        ],
    )

    assert hashlib.sha256(db.read_bytes()).hexdigest() == before
    assert not Path(str(db) + "-wal").exists()


def test_missing_database_is_never_created(tmp_path):
    db = tmp_path / "missing.db"
    artifact = _artifact(["golden_one", "generic_one", "explore_one"])

    with pytest.raises(barriers.BarrierInputError, match="read-only"):
        barriers.evaluate_b2(db, stage1_artifact=artifact, producer_exited=True)

    assert not db.exists()
