from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import sqlite3
from threading import Barrier

import pytest

from extensions.sop_v2 import creator_cache as cc
from extensions.sop_v2.pipeline import barriers
from extensions.sop_v2.pipeline import stage1_discover as stage1


def _record(handle: str, bucket: str) -> dict:
    source = f"test:{bucket}"
    return {
        "handle": handle,
        "followers": "12K",
        "er": "2.5%",
        "discovered_via": source,
        "discovery_sources": [source],
        "golden_seed_handles": ["@Golden_A"] if bucket == "golden_lookalike" else [],
        "discovery_quota_bucket": bucket,
    }


def _rows(db):
    with sqlite3.connect(db) as conn:
        return conn.execute(
            "SELECT handle,times_seen,discovery_batch,stage_json "
            "FROM creator_profiles ORDER BY lower(handle)"
        ).fetchall()


def test_concurrent_overlap_commits_exactly_one_whole_cohort(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "DB", tmp_path / "creator_cache.db")
    start = Barrier(2)

    def ingest(batch, unique):
        start.wait()
        try:
            return cc.seed_handles_atomic(
                [
                    _record("shared_handle", "generic_commerce"),
                    _record(unique, "exploration"),
                ],
                batch,
            )
        except cc.AtomicSeedIngestError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(lambda args: ingest(*args), [("B-A", "only_a"), ("B-B", "only_b")])
        )

    assert sum(isinstance(result, dict) for result in results) == 1
    assert sum(isinstance(result, cc.AtomicSeedIngestError) for result in results) == 1
    rows = _rows(cc.DB)
    assert len(rows) == 2
    assert {row[0] for row in rows} in (
        {"shared_handle", "only_a"},
        {"shared_handle", "only_b"},
    )
    assert {row[1] for row in rows} == {1}
    assert len({row[2] for row in rows}) == 1


def test_sql_failure_mid_batch_rolls_back_every_insert(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "DB", tmp_path / "creator_cache.db")
    with cc._conn() as conn:
        conn.execute(
            """CREATE TRIGGER inject_stage1_failure BEFORE INSERT ON creator_profiles
               WHEN NEW.handle='boom'
               BEGIN SELECT RAISE(ABORT, 'injected stage1 failure'); END"""
        )

    with pytest.raises(cc.AtomicSeedIngestError, match="rolled back"):
        cc.seed_handles_atomic(
            [
                _record("first_ok", "generic_commerce"),
                _record("boom", "exploration"),
            ],
            "B-ROLLBACK",
        )

    assert _rows(cc.DB) == []


def test_atomic_seed_writes_complete_provenance_once(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "DB", tmp_path / "creator_cache.db")
    result = cc.seed_handles_atomic(
        [_record("Creator.One", "golden_lookalike")], "B-PROVENANCE"
    )

    assert result["new_seeds"] == result["audited_new"] == 1
    row = _rows(cc.DB)[0]
    stage = json.loads(row[3])
    assert stage == {
        "handle": "Creator.One",
        "seed_followers": 12000,
        "modash_er": 2.5,
        "discovered_via": "test:golden_lookalike",
        "discovery_sources": ["test:golden_lookalike"],
        "golden_seed_handles": ["golden_a"],
        "discovery_batch": "B-PROVENANCE",
        "discovery_quota_bucket": "golden_lookalike",
        "stage1_ingest_mode": "atomic",
        "stage1_ingest_id": result["stage1_ingest_id"],
    }


def test_generated_artifact_passes_b1(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "DB", tmp_path / "creator_cache.db")
    records = [
        _record("golden_one", "golden_lookalike"),
        _record("generic_one", "generic_commerce"),
        _record("explore_one", "exploration"),
    ]
    cc.seed_handles_atomic(records, "B-BARRIER")
    contract = {
        "schema_version": 1,
        "batch_id": "B-BARRIER",
        "sources": {"quota_pct": {
            "golden_lookalike": 50,
            "generic_commerce": 30,
            "exploration": 20,
        }},
    }
    rendered = json.dumps(contract, ensure_ascii=False, indent=2) + "\n"
    contract_path = tmp_path / "round_contract.json"
    contract_path.write_text(rendered, encoding="utf-8")
    contract_sha = hashlib.sha256(rendered.encode()).hexdigest()
    artifact = stage1.build_stage1_barrier_artifact(
        records,
        batch_id="B-BARRIER",
        round_contract_sha256=contract_sha,
    )
    artifact_path = tmp_path / "stage1_barrier_artifact.json"
    temporary = stage1._prepare_atomic_json(artifact_path, artifact)
    stage1._publish_atomic_json(temporary, artifact_path)

    result = barriers.evaluate_b1(
        cc.DB,
        round_contract=contract_path,
        stage1_artifact=artifact_path,
    )
    assert result.passed is True
    assert result.observed["source_counts"] == {
        "golden_lookalike": 1,
        "generic_commerce": 1,
        "exploration": 1,
    }
