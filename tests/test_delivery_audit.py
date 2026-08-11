from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from extensions.sop_v2.pipeline import barriers, delivery_audit


def _db(tmp_path: Path) -> Path:
    path = tmp_path / "creator_cache.db"
    conn = sqlite3.connect(path)
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
    conn.execute(
        "INSERT INTO creator_profiles VALUES (?,?,?,?,?,?)",
        (
            "example_creator",
            "decided",
            None,
            None,
            "B-DELIVERY",
            json.dumps({"handle": "example_creator", "modash_report": True}),
        ),
    )
    conn.commit()
    conn.close()
    return path


def _artifact() -> dict:
    return {
        "batch_id": "B-DELIVERY",
        "target_total": 1,
        "handle_set_sha256": barriers.handle_set_sha256(["example_creator"]),
    }


def test_evaluate_delivery_binds_zero_failure_audit_to_exact_snapshot(
    tmp_path, monkeypatch
):
    db = _db(tmp_path)
    monkeypatch.setattr(delivery_audit, "load_config", lambda path=None: {})
    monkeypatch.setattr(delivery_audit, "config_sha256", lambda path=None: "c" * 64)
    monkeypatch.setattr(
        delivery_audit,
        "_candidate_failure_report",
        lambda candidates, cfg, target_posts: (
            {key: 0 for key in barriers.REQUIRED_B4_FAILURE_KEYS},
            {key: [] for key in barriers.REQUIRED_B4_FAILURE_KEYS},
            {"translation": {}, "enrichment": {}},
        ),
    )

    result = delivery_audit.evaluate_delivery(
        db_path=db,
        batch_id="B-DELIVERY",
        stage1_artifact=_artifact(),
    )

    assert result["barrier"]["passed"] is True
    assert result["barrier"]["observed"]["delivery_state_sha256"] == result[
        "validated_delivery_state_sha256"
    ]


def test_read_candidates_is_read_only_and_hash_changes_with_stage_json(tmp_path):
    db = _db(tmp_path)
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    candidates, first_hash = delivery_audit._read_candidates_ro(db, "B-DELIVERY")

    assert candidates[0]["_status"] == "decided"
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before
    assert not Path(str(db) + "-wal").exists()

    conn = sqlite3.connect(db)
    stage = json.loads(
        conn.execute("SELECT stage_json FROM creator_profiles").fetchone()[0]
    )
    stage["storefront_status"] = "confirmed_yes"
    conn.execute(
        "UPDATE creator_profiles SET stage_json=?",
        (json.dumps(stage),),
    )
    conn.commit()
    conn.close()

    _, second_hash = delivery_audit._read_candidates_ro(db, "B-DELIVERY")
    assert first_hash != second_hash


def test_append_only_audit_output_refuses_overwrite(tmp_path):
    output = tmp_path / "b4.json"
    payload = {"barrier": {"passed": False}}
    delivery_audit._write_new_json(output, payload)

    with pytest.raises(delivery_audit.DeliveryAuditError, match="拒绝覆盖"):
        delivery_audit._write_new_json(output, payload)

    assert json.loads(output.read_text()) == payload


def test_read_candidates_rejects_stage_json_handle_substitution(tmp_path):
    db = _db(tmp_path)
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE creator_profiles SET stage_json=?",
        (json.dumps({"handle": "another_creator"}),),
    )
    conn.commit()
    conn.close()

    with pytest.raises(delivery_audit.DeliveryAuditError, match="身份不一致"):
        delivery_audit._read_candidates_ro(db, "B-DELIVERY")
