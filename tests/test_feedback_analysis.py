from __future__ import annotations

import json
from pathlib import Path

from extensions.sop_v2 import creator_cache as cc
from extensions.sop_v2.pipeline.analyze_client_feedback import (
    analyze_feedback,
    render_markdown,
)


def _event(
    conn,
    *,
    event_id,
    handle,
    verdict,
    tags=(),
    reason="",
    scope="account",
    batch="B1",
    imported_at="2026-08-10T10:00:00",
    source_context=None,
):
    conn.execute(
        """INSERT INTO client_feedback_events
           (event_id,review_batch,origin_batch,handle,verdict,reason_raw,
            reason_tags_json,feedback_scope,rejection_scope,evidence_status,
            feedback_file_sha256,source_decisions_sha256,taxonomy_version,
            imported_at,source_mode,source_context_json)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            event_id,
            batch,
            "ORIGIN",
            handle,
            verdict,
            reason,
            json.dumps(list(tags)),
            scope,
            "campaign" if verdict == "rejected" else None,
            "unverified",
            event_id.ljust(64, "0")[:64],
            "f" * 64,
            "1.0.0",
            imported_at,
            "import",
            json.dumps(source_context) if source_context is not None else None,
        ),
    )


def test_feedback_report_separates_account_results_from_strategy_signals(tmp_path):
    old_db = cc.DB
    cc.DB = tmp_path / "cache.db"
    try:
        with cc._conn() as conn:
            profiles = [
                ("approved_one", {"discovery_sources": ["modash_structured_search:generic_commerce"]}),
                ("rejected_tagged", {"discovery_sources": ["modash_structured_search:exploration"]}),
                ("rejected_blank", {"discovery_sources": ["modash_structured_search:generic_commerce"]}),
                ("pending_one", {"discovered_via": "manual"}),
            ]
            conn.executemany(
                "INSERT INTO creator_profiles(handle,stage_json) VALUES (?,?)",
                [(handle, json.dumps(stage)) for handle, stage in profiles],
            )
            _event(
                conn,
                event_id="a",
                handle="approved_one",
                verdict="approved",
                source_context={
                    "discovery_sources": [
                        "modash_structured_search:generic_commerce"
                    ],
                    "discovered_via": None,
                    "golden_seed_handles": [],
                },
            )
            _event(
                conn,
                event_id="b",
                handle="rejected_tagged",
                verdict="rejected",
                tags=("quality_video_low",),
                reason="画质差",
                scope="policy_signal",
                source_context={
                    "discovery_sources": ["modash_structured_search:exploration"],
                    "discovered_via": None,
                    "golden_seed_handles": [],
                },
            )
            _event(
                conn,
                event_id="c",
                handle="rejected_blank",
                verdict="rejected",
                source_context={
                    "discovery_sources": [
                        "modash_structured_search:generic_commerce"
                    ],
                    "discovered_via": None,
                    "golden_seed_handles": [],
                },
            )
            _event(
                conn,
                event_id="d",
                handle="pending_one",
                verdict="pending",
                source_context={
                    "discovery_sources": [],
                    "discovered_via": "manual",
                    "golden_seed_handles": [],
                },
            )
            # 当前投影可以继续变化；来源归因必须保持事件导入时的冻结值。
            conn.execute(
                "UPDATE creator_profiles SET stage_json=? WHERE handle='approved_one'",
                (json.dumps({"discovered_via": "mutable_wrong_source"}),),
            )

        report = analyze_feedback(cc.DB)

        assert report["reviewed_final"] == 3
        assert report["approval_rate"] == 0.3333
        assert report["reason_coverage"] == {
            "reasoned_rejections": 1,
            "unreasoned_rejections": 1,
            "coverage_rate": 0.5,
        }
        assert report["reason_distribution"][0]["reason_tag"] == "quality_video_low"
        assert report["strategy_proposals"][0]["eligible_for_automatic_hard_gate"] is False
        assert report["strategy_proposals"][0]["proposal_action"] == "monitor_signal"
        assert report["unstructured_reason_queue"] == []
        assert report["source_performance"][
            "modash_structured_search:generic_commerce"
        ]["approval_rate"] == 0.5
        assert "mutable_wrong_source" not in report["source_performance"]
        assert report["governance"]["source_attribution_uses_frozen_event_context"]
        markdown = render_markdown(report)
        assert "空原因 1 条只记录账号结果" in markdown
        assert "待定不进入分母" in markdown
    finally:
        cc.DB = old_db


def test_latest_revision_wins_within_same_review_batch(tmp_path):
    old_db = cc.DB
    cc.DB = tmp_path / "cache.db"
    try:
        with cc._conn() as conn:
            conn.execute(
                "INSERT INTO creator_profiles(handle,stage_json) VALUES (?,?)",
                ("changed", json.dumps({"discovered_via": "manual"})),
            )
            _event(
                conn,
                event_id="old",
                handle="changed",
                verdict="approved",
                imported_at="2026-08-10T09:00:00",
            )
            _event(
                conn,
                event_id="new",
                handle="changed",
                verdict="rejected",
                tags=("other",),
                reason="changed mind",
                imported_at="2026-08-10T10:00:00",
            )

        report = analyze_feedback(cc.DB)

        assert report["event_count"] == 1
        assert report["verdicts"] == {"rejected": 1}
    finally:
        cc.DB = old_db


def test_legacy_free_text_is_queued_but_not_inferred(tmp_path):
    old_db = cc.DB
    cc.DB = tmp_path / "cache.db"
    try:
        with cc._conn() as conn:
            conn.execute(
                "INSERT INTO creator_profiles(handle,stage_json) VALUES (?,?)",
                ("legacy", json.dumps({"discovered_via": "old_search"})),
            )
            _event(
                conn,
                event_id="legacy-event",
                handle="legacy",
                verdict="rejected",
                reason="视频看起来比较廉价",
            )

        report = analyze_feedback(cc.DB)

        assert report["strategy_proposals"] == []
        assert report["unstructured_reason_queue"] == [
            {
                "event_id": "legacy-event",
                "review_batch": "B1",
                "handle": "legacy",
                "reason_raw": "视频看起来比较廉价",
                "suggested_action": "manual_tagging_required",
            }
        ]
        assert report["quality_warnings"][
            "legacy_free_text_waiting_for_manual_tags"
        ] == 1
        assert "unknown/legacy" in report["source_performance"]
        assert report["quality_warnings"]["legacy_events_without_source_context"] == 1
    finally:
        cc.DB = old_db


def test_strategy_threshold_deduplicates_carryover_account_and_origin_round(tmp_path):
    old_db = cc.DB
    cc.DB = tmp_path / "cache.db"
    try:
        with cc._conn() as conn:
            conn.execute(
                "INSERT INTO creator_profiles(handle,stage_json) VALUES (?,?)",
                ("same_carryover", json.dumps({"discovered_via": "manual"})),
            )
            _event(
                conn,
                event_id="round-one",
                handle="same_carryover",
                verdict="rejected",
                tags=("quality_video_low",),
                reason="画质差",
                batch="B1",
                imported_at="2026-08-10T09:00:00",
            )
            _event(
                conn,
                event_id="round-two",
                handle="same_carryover",
                verdict="rejected",
                tags=("quality_video_low",),
                reason="仍然不合适",
                batch="B2",
                imported_at="2026-08-10T10:00:00",
            )

        report = analyze_feedback(cc.DB, min_labels=1, min_rounds=2)
        proposal = report["strategy_proposals"][0]

        assert proposal["unique_account_count"] == 1
        assert proposal["event_count"] == 2
        assert proposal["repeat_event_count"] == 1
        assert proposal["batches"] == ["B1", "B2"]
        assert proposal["origin_batches"] == ["ORIGIN"]
        assert proposal["threshold_met"] is False
        assert report["governance"]["strategy_threshold_uses_unique_accounts"] is True
    finally:
        cc.DB = old_db
