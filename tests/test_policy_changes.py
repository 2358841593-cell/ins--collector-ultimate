from __future__ import annotations

import json
import sqlite3

import pytest

from extensions.sop_v2 import creator_cache as cc
from extensions.sop_v2.policy_changes import PolicyChangeError, append_policy_change


def _seed_event(db_path):
    old_db = cc.DB
    cc.DB = db_path
    try:
        with cc._conn() as conn:
            conn.execute("INSERT INTO creator_profiles(handle) VALUES ('one')")
            conn.execute(
                """INSERT INTO client_feedback_events
                   (event_id,review_batch,origin_batch,handle,verdict,reason_raw,
                    reason_tags_json,feedback_scope,rejection_scope,evidence_status,
                    feedback_file_sha256,source_decisions_sha256,taxonomy_version,
                    imported_at,source_mode)
                   VALUES ('event-1','B1','B1','one','rejected','bad','[\"quality_video_low\"]',
                           'policy_signal','campaign','unverified',?,?, '1.0.0',
                           '2026-08-10T00:00:00','import')""",
                ("a" * 64, "b" * 64),
            )
    finally:
        cc.DB = old_db


def test_policy_change_requires_replay_and_test_hashes_before_approval(tmp_path):
    db = tmp_path / "cache.db"
    _seed_event(db)
    proposal = append_policy_change(
        db,
        policy_key="ranking.quality_video_low",
        status="proposed",
        source_event_ids=["event-1"],
        before_value={"weight": 0},
        after_value={"weight": -1},
        created_at="2026-08-10T01:00:00",
    )
    with pytest.raises(PolicyChangeError, match="回放/测试"):
        append_policy_change(
            db,
            policy_key="ranking.quality_video_low",
            status="approved",
            supersedes_change_id=proposal["change_id"],
            confirmed_by="operator",
            confirmed_at="2026-08-10T02:00:00",
        )

    approved = append_policy_change(
        db,
        policy_key="ranking.quality_video_low",
        status="approved",
        supersedes_change_id=proposal["change_id"],
        confirmed_by="operator",
        confirmed_at="2026-08-10T02:00:00",
        config_sha_before="1" * 64,
        config_sha_after="2" * 64,
        impact_report_sha256="3" * 64,
        test_report_sha256="4" * 64,
        effective_batch="B2",
    )
    assert approved["status"] == "approved"

    conn = sqlite3.connect(db)
    try:
        rows = conn.execute(
            "SELECT status,supersedes_change_id FROM policy_change_log ORDER BY rowid"
        ).fetchall()
    finally:
        conn.close()
    assert rows == [("proposed", None), ("approved", proposal["change_id"])]


def test_policy_change_cannot_skip_or_branch_state_machine(tmp_path):
    db = tmp_path / "cache.db"
    _seed_event(db)
    with pytest.raises(PolicyChangeError, match="supersedes_change_id"):
        append_policy_change(
            db,
            policy_key="gate.country",
            status="approved",
            source_event_ids=["event-1"],
        )
    proposal = append_policy_change(
        db,
        policy_key="gate.country",
        status="proposed",
        source_event_ids=["event-1"],
    )
    append_policy_change(
        db,
        policy_key="gate.country",
        status="rejected",
        supersedes_change_id=proposal["change_id"],
        confirmed_by="operator",
        confirmed_at="2026-08-10T02:00:00",
    )
    with pytest.raises(PolicyChangeError, match="已经存在后继"):
        append_policy_change(
            db,
            policy_key="gate.country",
            status="rejected",
            supersedes_change_id=proposal["change_id"],
            confirmed_by="operator",
            confirmed_at="2026-08-10T03:00:00",
        )


def test_transition_cannot_replace_proposal_evidence_or_values(tmp_path):
    db = tmp_path / "cache.db"
    _seed_event(db)
    proposal = append_policy_change(
        db,
        policy_key="ranking.quality",
        status="proposed",
        source_event_ids=["event-1"],
        before_value={"weight": 0},
        after_value={"weight": -1},
    )

    with pytest.raises(PolicyChangeError, match="after_value"):
        append_policy_change(
            db,
            policy_key="ranking.quality",
            status="rejected",
            supersedes_change_id=proposal["change_id"],
            after_value={"weight": -99},
            confirmed_by="operator",
            confirmed_at="2026-08-10T03:00:00",
        )


def test_rollback_requires_new_config_replay_and_test_evidence(tmp_path):
    db = tmp_path / "cache.db"
    _seed_event(db)
    proposal = append_policy_change(
        db,
        policy_key="ranking.quality",
        status="proposed",
        source_event_ids=["event-1"],
        before_value={"weight": 0},
        after_value={"weight": -1},
    )
    approved = append_policy_change(
        db,
        policy_key="ranking.quality",
        status="approved",
        supersedes_change_id=proposal["change_id"],
        confirmed_by="operator",
        confirmed_at="2026-08-10T03:00:00",
        config_sha_before="1" * 64,
        config_sha_after="2" * 64,
        impact_report_sha256="3" * 64,
        test_report_sha256="4" * 64,
        effective_batch="B2",
    )

    with pytest.raises(PolicyChangeError, match="rolled_back 策略缺少"):
        append_policy_change(
            db,
            policy_key="ranking.quality",
            status="rolled_back",
            supersedes_change_id=approved["change_id"],
            confirmed_by="operator",
            confirmed_at="2026-08-10T04:00:00",
        )
