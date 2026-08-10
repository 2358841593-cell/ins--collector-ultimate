"""客户反馈安全闭环：严格文件校验、事务落库、ledger 幂等与旧状态接管。"""
from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from extensions.sop_v2 import creator_cache as cc  # noqa: E402
from extensions.sop_v2.pipeline import ingest_client_decisions as ingest  # noqa: E402


BATCH = "SAFE-20260723"


class ClientDecisionTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.old_db = cc.DB
        cc.DB = self.root / "creator_cache.db"

    def tearDown(self):
        cc.DB = self.old_db
        self.temp.cleanup()

    def _insert(self, handle: str, **values):
        base = {
            "discovery_batch": BATCH,
            "status": "decided",
            "tier": 1,
            "client_status": None,
            "approved_at": None,
            "rejected_reason": None,
            "client_rejection_scope": None,
            "source_batch": None,
            "client_note": None,
        }
        base.update(values)
        columns = ["handle", *base]
        params = [handle, *base.values()]
        conn = cc._conn()
        try:
            conn.execute(
                f"INSERT INTO creator_profiles ({','.join(columns)}) "
                f"VALUES ({','.join('?' * len(columns))})",
                params,
            )
            conn.commit()
        finally:
            conn.close()

    def _row(self, handle: str):
        conn = cc._conn()
        try:
            return dict(
                conn.execute(
                    "SELECT * FROM creator_profiles WHERE handle=?", (handle,)
                ).fetchone()
            )
        finally:
            conn.close()

    def _feedback_events(self, handle: str | None = None):
        conn = cc._conn()
        try:
            if handle is None:
                rows = conn.execute(
                    "SELECT * FROM client_feedback_events ORDER BY rowid"
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT * FROM client_feedback_events
                       WHERE handle=? COLLATE NOCASE ORDER BY rowid""",
                    (handle,),
                ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def _write_payloads(self, decisions: list[dict], *, batch: str = BATCH,
                        source_batch: str | None = None):
        feedback_path = self.root / "feedback.json"
        source_path = self.root / "source.json"
        feedback_path.write_text(
            json.dumps(
                {
                    "batch": batch,
                    "exported_at": "2026-07-23T01:02:03.000Z",
                    "decisions": decisions,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        handles = []
        seen = set()
        for item in decisions:
            handle = item.get("handle")
            key = handle.lower() if isinstance(handle, str) else repr(handle)
            if key not in seen:
                handles.append(handle)
                seen.add(key)
        source_path.write_text(
            json.dumps(
                {
                    "manifest": {"batch_id": source_batch or batch},
                    "candidates": [
                        {"handle": handle, "_discovery_batch": source_batch or batch}
                        for handle in handles
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return feedback_path, source_path


class TestTransactionalImport(ClientDecisionTestCase):
    def test_fresh_import_is_atomic_and_idempotent(self):
        self._insert(
            "approve_me",
            rejected_reason="old rejection",
            source_batch="OLDER",
        )
        self._insert(
            "reject_me",
            tier=2,
            approved_at="2026-01-01T00:00:00",
            source_batch="OLDER",
            client_note="keep this independent note",
        )
        self._insert("pending_me", source_batch="UNCHANGED")
        feedback, source = self._write_payloads(
            [
                {
                    "handle": "approve_me",
                    "verdict": "合适",
                    "reason": "good fit",
                    "pool": "Review",
                    "score": "8.1",
                },
                {
                    "handle": "reject_me",
                    "verdict": "不合适",
                    "reason": "",
                    "pool": "Review",
                    "score": "7.5",
                },
                {
                    "handle": "pending_me",
                    "verdict": "待定",
                    "reason": "review later",
                    "pool": "Review",
                    "score": "8.0",
                },
            ]
        )
        payload = ingest.load_validated_decisions(feedback, source)
        result = cc.apply_client_decisions(
            payload["decisions"],
            batch_id=payload["batch"],
            file_sha256=payload["file_sha256"],
            source_sha256=payload["source_sha256"],
        )
        self.assertEqual(
            (result["approved"], result["rejected"], result["pending"]), (1, 1, 1)
        )

        approved = self._row("approve_me")
        self.assertEqual(approved["client_status"], "approved")
        self.assertEqual(approved["tier"], 2)
        self.assertIsNotNone(approved["approved_at"])
        self.assertIsNone(approved["rejected_reason"])
        self.assertEqual(approved["source_batch"], BATCH)
        self.assertEqual(approved["client_note"], "[合适] good fit")

        rejected = self._row("reject_me")
        self.assertEqual(rejected["client_status"], "rejected")
        self.assertEqual(rejected["tier"], 1)
        self.assertIsNone(rejected["approved_at"])
        self.assertEqual(rejected["rejected_reason"], "客户判定不合适")
        self.assertEqual(rejected["client_rejection_scope"], "global")
        self.assertEqual(rejected["source_batch"], BATCH)
        self.assertEqual(rejected["client_note"], "keep this independent note")

        pending = self._row("pending_me")
        self.assertIsNone(pending["client_status"])
        self.assertEqual(pending["tier"], 1)
        self.assertIsNone(pending["approved_at"])
        self.assertIsNone(pending["rejected_reason"])
        self.assertEqual(pending["source_batch"], "UNCHANGED")
        self.assertEqual(pending["client_note"], "[待定] review later")

        events = self._feedback_events()
        self.assertEqual(len(events), 3)
        self.assertEqual(
            [event["verdict"] for event in events],
            ["approved", "rejected", "pending"],
        )
        self.assertEqual(events[0]["reason_raw"], "good fit")
        self.assertEqual(json.loads(events[0]["reason_tags_json"]), [])
        self.assertEqual(events[0]["feedback_scope"], "account")
        self.assertEqual(events[0]["source_mode"], "import")
        self.assertIsNone(events[0]["rejection_scope"])
        self.assertEqual(events[1]["rejection_scope"], "global")
        self.assertEqual(events[0]["feedback_file_sha256"], payload["file_sha256"])
        self.assertEqual(
            events[0]["source_decisions_sha256"], payload["source_sha256"]
        )

        approved_at = approved["approved_at"]
        second = cc.apply_client_decisions(
            payload["decisions"],
            batch_id=payload["batch"],
            file_sha256=payload["file_sha256"],
            source_sha256=payload["source_sha256"],
        )
        self.assertTrue(second["already_imported"])
        self.assertEqual(self._row("approve_me")["approved_at"], approved_at)
        conn = cc._conn()
        try:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM client_feedback_imports").fetchone()[0],
                1,
            )
        finally:
            conn.close()
        self.assertEqual(len(self._feedback_events()), 3)

    def test_db_validation_failure_rolls_back_entire_batch(self):
        self._insert("valid")
        self._insert("wrong_batch", discovery_batch="OTHER")
        decisions = [
            {"handle": "valid", "action": "approved", "reason": ""},
            {"handle": "wrong_batch", "action": "rejected", "reason": "no"},
        ]
        with self.assertRaises(cc.ClientDecisionImportError):
            cc.apply_client_decisions(
                decisions,
                batch_id=BATCH,
                file_sha256="a" * 64,
                source_sha256="b" * 64,
            )
        self.assertIsNone(self._row("valid")["client_status"])
        conn = cc._conn()
        try:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM client_feedback_imports").fetchone()[0],
                0,
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM client_feedback_events").fetchone()[0],
                0,
            )
        finally:
            conn.close()

    def test_existing_client_status_conflict_requires_explicit_override(self):
        self._insert(
            "changed_mind",
            tier=2,
            client_status="approved",
            approved_at="2026-01-01T00:00:00",
            source_batch="OLDER",
        )
        decisions = [
            {"handle": "changed_mind", "action": "rejected", "reason": "new decision"}
        ]
        kwargs = {
            "batch_id": BATCH,
            "file_sha256": "e" * 64,
            "source_sha256": "f" * 64,
        }
        with self.assertRaisesRegex(
            cc.ClientDecisionImportError, "allow-status-change"
        ):
            cc.apply_client_decisions(decisions, **kwargs)
        self.assertEqual(self._row("changed_mind")["client_status"], "approved")

        cc.apply_client_decisions(
            decisions, allow_status_change=True, **kwargs
        )
        changed = self._row("changed_mind")
        self.assertEqual(changed["client_status"], "rejected")
        self.assertEqual(changed["tier"], 1)
        self.assertIsNone(changed["approved_at"])

    def test_incremental_export_reuses_prior_rows_without_refreshing_approval(self):
        self._insert("already_approved")
        self._insert("new_rejection")
        first = [
            {"handle": "already_approved", "action": "approved", "reason": ""}
        ]
        cc.apply_client_decisions(
            first,
            batch_id=BATCH,
            file_sha256="1" * 64,
            source_sha256="2" * 64,
        )
        conn = cc._conn()
        try:
            conn.execute(
                "UPDATE creator_profiles SET approved_at=? WHERE handle=?",
                ("2026-01-01T00:00:00", "already_approved"),
            )
            conn.commit()
        finally:
            conn.close()

        result = cc.apply_client_decisions(
            [
                {"handle": "already_approved", "action": "approved", "reason": ""},
                {"handle": "new_rejection", "action": "rejected", "reason": "not fit"},
            ],
            batch_id=BATCH,
            file_sha256="3" * 64,
            source_sha256="2" * 64,
        )
        self.assertEqual(result["decision_count"], 2)
        self.assertEqual(
            self._row("already_approved")["approved_at"], "2026-01-01T00:00:00"
        )
        self.assertEqual(self._row("new_rejection")["client_status"], "rejected")
        self.assertEqual(
            self._row("new_rejection")["client_rejection_scope"], "global"
        )
        conn = cc._conn()
        try:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM client_feedback_imports").fetchone()[0],
                2,
            )
        finally:
            conn.close()

    def test_explicit_terminal_to_pending_change_clears_terminal_fields(self):
        self._insert(
            "review_again",
            tier=2,
            client_status="approved",
            approved_at="2026-01-01T00:00:00",
            rejected_reason="stale",
        )
        cc.apply_client_decisions(
            [
                {
                    "handle": "review_again",
                    "action": "pending",
                    "reason": "needs another look",
                }
            ],
            batch_id=BATCH,
            file_sha256="4" * 64,
            source_sha256="5" * 64,
            allow_status_change=True,
        )
        row = self._row("review_again")
        self.assertIsNone(row["client_status"])
        self.assertIsNone(row["approved_at"])
        self.assertIsNone(row["rejected_reason"])
        self.assertEqual(row["tier"], 1)
        self.assertEqual(row["client_note"], "[待定] needs another look")

    def test_status_change_with_empty_reason_clears_stale_tagged_note(self):
        self._insert(
            "pending_to_approved",
            client_note="[待定] old note",
        )
        cc.apply_client_decisions(
            [
                {
                    "handle": "pending_to_approved",
                    "action": "approved",
                    "reason": "",
                }
            ],
            batch_id=BATCH,
            file_sha256="6" * 64,
            source_sha256="7" * 64,
        )
        self.assertIsNone(self._row("pending_to_approved")["client_note"])

        self._insert(
            "approved_to_pending",
            tier=2,
            client_status="approved",
            approved_at="2026-01-01T00:00:00",
            client_note="[合适] old note",
        )
        cc.apply_client_decisions(
            [
                {
                    "handle": "approved_to_pending",
                    "action": "pending",
                    "reason": "",
                }
            ],
            batch_id=BATCH,
            file_sha256="8" * 64,
            source_sha256="9" * 64,
            allow_status_change=True,
        )
        self.assertIsNone(self._row("approved_to_pending")["client_note"])


class TestFeedbackEventLedger(ClientDecisionTestCase):
    def test_v2_structured_feedback_is_preserved_and_empty_signal_is_demoted(self):
        self._insert("country_fix")
        self._insert("blank_rejection")
        feedback, source = self._write_payloads(
            [
                {
                    "handle": "country_fix",
                    "verdict": "不合适",
                    "reason": "客户确认创作者在俄罗斯",
                    "reason_tags": ["geo_creator_outside", "geo_creator_outside"],
                    "feedback_scope": "fact_correction",
                    "rejection_scope": "temporary",
                    "target_field": "creator_country",
                    "old_value": "US",
                    "claimed_value": "RU",
                    "evidence_status": "verified",
                },
                {
                    "handle": "blank_rejection",
                    "verdict": "不合适",
                    "reason": "",
                    "reason_tags": [],
                    "feedback_scope": "policy_signal",
                },
            ]
        )
        data = json.loads(feedback.read_text(encoding="utf-8"))
        data.update(
            {
                "feedback_schema_version": 2,
                "taxonomy_version": ingest.TAXONOMY_VERSION,
            }
        )
        feedback.write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8"
        )

        payload = ingest.load_validated_decisions(feedback, source)
        self.assertEqual(
            payload["decisions"][0]["reason_tags"], ["geo_creator_outside"]
        )
        self.assertEqual(payload["decisions"][1]["feedback_scope"], "account")
        cc.apply_client_decisions(
            payload["decisions"],
            batch_id=payload["batch"],
            file_sha256=payload["file_sha256"],
            source_sha256=payload["source_sha256"],
        )

        corrected, blank = self._feedback_events()
        self.assertEqual(json.loads(corrected["reason_tags_json"]), ["geo_creator_outside"])
        self.assertEqual(corrected["feedback_scope"], "fact_correction")
        self.assertEqual(corrected["rejection_scope"], "temporary")
        self.assertEqual(corrected["target_field"], "creator_country")
        self.assertEqual(corrected["old_value"], "US")
        self.assertEqual(corrected["claimed_value"], "RU")
        self.assertEqual(corrected["evidence_status"], "verified")
        self.assertEqual(corrected["taxonomy_version"], ingest.TAXONOMY_VERSION)
        self.assertEqual(blank["feedback_scope"], "account")
        self.assertEqual(json.loads(blank["reason_tags_json"]), [])
        self.assertEqual(blank["rejection_scope"], "campaign")
        self.assertEqual(
            self._row("country_fix")["client_rejection_scope"], "temporary"
        )
        self.assertEqual(
            self._row("blank_rejection")["client_rejection_scope"], "campaign"
        )

    def test_rejection_scope_default_is_global_for_legacy_and_campaign_for_v2(self):
        for schema_version, expected in ((None, "global"), (2, "campaign")):
            with self.subTest(schema_version=schema_version):
                feedback, source = self._write_payloads(
                    [{"handle": "one", "verdict": "不合适", "reason": ""}]
                )
                if schema_version == 2:
                    data = json.loads(feedback.read_text(encoding="utf-8"))
                    data.update(
                        {
                            "feedback_schema_version": 2,
                            "taxonomy_version": ingest.TAXONOMY_VERSION,
                        }
                    )
                    feedback.write_text(json.dumps(data), encoding="utf-8")
                payload = ingest.load_validated_decisions(feedback, source)
                self.assertEqual(
                    payload["decisions"][0]["rejection_scope"], expected
                )

    def test_source_context_is_frozen_from_source_candidate_with_field_allowlist(self):
        self._insert("lineage")
        feedback, source = self._write_payloads(
            [{"handle": "lineage", "verdict": "合适", "reason": ""}]
        )
        source_data = json.loads(source.read_text(encoding="utf-8"))
        source_data["candidates"][0].update(
            {
                "discovery_sources": ["structured:commerce", "structured:commerce"],
                "discovered_via": "lookalike",
                "golden_seed_handles": ["@Seed_A", "seed_a", "seed_b"],
                "biography": "must not enter feedback event",
                "comment_records": [{"text": "private-to-this-contract"}],
            }
        )
        source.write_text(json.dumps(source_data), encoding="utf-8")

        payload = ingest.load_validated_decisions(feedback, source)
        expected = {
            "discovery_sources": ["structured:commerce"],
            "discovered_via": "lookalike",
            "golden_seed_handles": ["seed_a", "seed_b"],
        }
        self.assertEqual(payload["decisions"][0]["source_context"], expected)
        cc.apply_client_decisions(
            payload["decisions"],
            batch_id=payload["batch"],
            file_sha256=payload["file_sha256"],
            source_sha256=payload["source_sha256"],
        )
        frozen = json.loads(self._feedback_events()[0]["source_context_json"])
        self.assertEqual(frozen, expected)
        self.assertNotIn("biography", frozen)
        self.assertNotIn("comment_records", frozen)

    def test_low_level_api_rejects_external_confirmed_policy(self):
        self._insert("unsafe_policy")
        with self.assertRaisesRegex(
            cc.ClientDecisionImportError, "不能直接确认全局策略"
        ):
            cc.apply_client_decisions(
                [
                    {
                        "handle": "unsafe_policy",
                        "action": "rejected",
                        "reason": "try to bypass importer",
                        "feedback_scope": "confirmed_policy",
                    }
                ],
                batch_id=BATCH,
                file_sha256="a" * 64,
                source_sha256="b" * 64,
            )
        self.assertIsNone(self._row("unsafe_policy")["client_status"])
        self.assertEqual(self._feedback_events(), [])

    def test_structured_contract_rejects_unknown_or_unsafe_values(self):
        cases = (
            ({"feedback_schema_version": 3}, "feedback_schema_version"),
            (
                {"feedback_schema_version": 2, "taxonomy_version": "999"},
                "taxonomy_version",
            ),
            ({"reason_tags": "geo_mismatch"}, "reason_tags"),
            ({"reason_tags": ["not_a_real_tag"]}, "未知 reason_tag"),
            ({"feedback_scope": "confirmed_policy"}, "不能直接确认全局策略"),
            ({"feedback_scope": "fact_correction"}, "target_field"),
            ({"rejection_scope": "global"}, "只有 rejected"),
            ({"evidence_status": "maybe"}, "evidence_status"),
        )
        for changes, message in cases:
            with self.subTest(changes=changes):
                verdict = "合适"
                if "reason_tags" in changes or changes.get("feedback_scope") in {
                    "confirmed_policy",
                    "fact_correction",
                }:
                    verdict = "不合适"
                decision = {"handle": "one", "verdict": verdict, "reason": "x"}
                top_level = {}
                for key, value in changes.items():
                    if key in {"feedback_schema_version", "taxonomy_version"}:
                        top_level[key] = value
                    else:
                        decision[key] = value
                feedback, source = self._write_payloads([decision])
                data = json.loads(feedback.read_text(encoding="utf-8"))
                data.update(top_level)
                feedback.write_text(json.dumps(data), encoding="utf-8")
                with self.assertRaisesRegex(ingest.FeedbackValidationError, message):
                    ingest.load_validated_decisions(feedback, source)

    def test_event_insert_failure_rolls_back_profiles_ledger_and_events(self):
        self._insert("first")
        self._insert("second")
        decisions = [
            {"handle": "first", "action": "approved", "reason": ""},
            {"handle": "second", "action": "rejected", "reason": ""},
        ]
        fixed_uuid = mock.Mock(hex="same-event-id")
        with mock.patch.object(cc.uuid, "uuid4", return_value=fixed_uuid):
            with self.assertRaises(sqlite3.IntegrityError):
                cc.apply_client_decisions(
                    decisions,
                    batch_id=BATCH,
                    file_sha256="a" * 64,
                    source_sha256="b" * 64,
                )
        self.assertIsNone(self._row("first")["client_status"])
        self.assertIsNone(self._row("second")["client_status"])
        conn = cc._conn()
        try:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM client_feedback_imports").fetchone()[0],
                0,
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM client_feedback_events").fetchone()[0],
                0,
            )
        finally:
            conn.close()

    def test_dry_run_writes_no_projection_ledger_or_event(self):
        self._insert("dry")
        result = cc.apply_client_decisions(
            [{"handle": "dry", "action": "approved", "reason": "looks good"}],
            batch_id=BATCH,
            file_sha256="c" * 64,
            source_sha256="d" * 64,
            dry_run=True,
        )
        self.assertTrue(result["dry_run"])
        self.assertIsNone(self._row("dry")["client_status"])
        self.assertEqual(self._feedback_events(), [])
        conn = cc._conn()
        try:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM client_feedback_imports").fetchone()[0],
                0,
            )
        finally:
            conn.close()

    def test_changed_verdict_links_to_previous_event(self):
        self._insert("changed")
        cc.apply_client_decisions(
            [{"handle": "changed", "action": "approved", "reason": "first"}],
            batch_id=BATCH,
            file_sha256="1" * 64,
            source_sha256="2" * 64,
        )
        first = self._feedback_events("changed")[0]
        cc.apply_client_decisions(
            [{"handle": "changed", "action": "rejected", "reason": "changed mind"}],
            batch_id=BATCH,
            file_sha256="3" * 64,
            source_sha256="2" * 64,
            allow_status_change=True,
        )
        events = self._feedback_events("changed")
        self.assertEqual(len(events), 2)
        self.assertEqual(events[1]["verdict"], "rejected")
        self.assertEqual(events[1]["supersedes_event_id"], first["event_id"])

    def test_feedback_and_policy_logs_are_append_only(self):
        self._insert("immutable")
        cc.apply_client_decisions(
            [{"handle": "immutable", "action": "approved", "reason": ""}],
            batch_id=BATCH,
            file_sha256="4" * 64,
            source_sha256="5" * 64,
        )
        event_id = self._feedback_events("immutable")[0]["event_id"]
        conn = cc._conn()
        try:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                conn.execute(
                    "UPDATE client_feedback_events SET reason_raw='changed' WHERE event_id=?",
                    (event_id,),
                )
            conn.rollback()
            with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                conn.execute(
                    "DELETE FROM client_feedback_events WHERE event_id=?", (event_id,)
                )
            conn.rollback()

            conn.execute(
                """INSERT INTO policy_change_log
                   (change_id, policy_key, status, source_event_ids_json, created_at)
                   VALUES (?,?,?,?,?)""",
                (
                    "change-1",
                    "countries.target",
                    "proposed",
                    json.dumps([event_id]),
                    "2026-08-10T00:00:00",
                ),
            )
            conn.commit()
            with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                conn.execute(
                    "UPDATE policy_change_log SET status='approved' WHERE change_id='change-1'"
                )
            conn.rollback()
            with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                conn.execute("DELETE FROM policy_change_log WHERE change_id='change-1'")
            conn.rollback()
        finally:
            conn.close()

    def test_historical_backfill_is_explicit_atomic_and_idempotent(self):
        self._insert(
            "historical_approved",
            client_status="rejected",
            rejected_reason="newer decision must remain",
            source_batch="LATER",
        )
        self._insert("historical_pending", client_note="operator note")
        feedback, source = self._write_payloads(
            [
                {"handle": "historical_approved", "verdict": "合适", "reason": "old"},
                {"handle": "historical_pending", "verdict": "待定", "reason": "later"},
            ]
        )
        payload = ingest.load_validated_decisions(feedback, source)
        imported_at = "2026-07-23T09:00:00"
        conn = cc._conn()
        try:
            conn.execute(
                """INSERT INTO client_feedback_imports
                   (file_sha256, batch_id, source_sha256, imported_at,
                    decision_count, approved_count, rejected_count, pending_count,
                    adopted_existing)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    payload["file_sha256"],
                    BATCH,
                    payload["source_sha256"],
                    imported_at,
                    2,
                    1,
                    0,
                    1,
                    0,
                ),
            )
            conn.commit()
            ledger_before = tuple(
                conn.execute(
                    "SELECT * FROM client_feedback_imports WHERE file_sha256=?",
                    (payload["file_sha256"],),
                ).fetchone()
            )
        finally:
            conn.close()

        # 普通同 SHA 导入保持旧语义：严格幂等跳过，不悄悄补历史事件。
        skipped = cc.apply_client_decisions(
            payload["decisions"],
            batch_id=BATCH,
            file_sha256=payload["file_sha256"],
            source_sha256=payload["source_sha256"],
        )
        self.assertTrue(skipped["already_imported"])
        self.assertEqual(self._feedback_events(), [])

        preview = cc.apply_client_decisions(
            payload["decisions"],
            batch_id=BATCH,
            file_sha256=payload["file_sha256"],
            source_sha256=payload["source_sha256"],
            backfill_events=True,
            dry_run=True,
        )
        self.assertEqual(preview["events_to_backfill"], 2)
        self.assertEqual(self._feedback_events(), [])

        result = cc.apply_client_decisions(
            payload["decisions"],
            batch_id=BATCH,
            file_sha256=payload["file_sha256"],
            source_sha256=payload["source_sha256"],
            backfill_events=True,
        )
        self.assertEqual(result["events_backfilled"], 2)
        events = self._feedback_events()
        self.assertEqual({event["source_mode"] for event in events}, {"historical_backfill"})
        self.assertEqual({event["imported_at"] for event in events}, {imported_at})
        self.assertEqual(
            {
                json.dumps(
                    json.loads(event["source_context_json"]), sort_keys=True
                )
                for event in events
            },
            {
                json.dumps(
                    {
                        "discovery_sources": [],
                        "discovered_via": None,
                        "golden_seed_handles": [],
                    },
                    sort_keys=True,
                )
            },
        )
        # 历史回填绝不覆盖此刻的账号投影。
        approved = self._row("historical_approved")
        self.assertEqual(approved["client_status"], "rejected")
        self.assertEqual(approved["rejected_reason"], "newer decision must remain")
        self.assertEqual(self._row("historical_pending")["client_note"], "operator note")

        conn = cc._conn()
        try:
            ledger_after = tuple(
                conn.execute(
                    "SELECT * FROM client_feedback_imports WHERE file_sha256=?",
                    (payload["file_sha256"],),
                ).fetchone()
            )
        finally:
            conn.close()
        self.assertEqual(ledger_after, ledger_before)

        repeated = cc.apply_client_decisions(
            payload["decisions"],
            batch_id=BATCH,
            file_sha256=payload["file_sha256"],
            source_sha256=payload["source_sha256"],
            backfill_events=True,
        )
        self.assertTrue(repeated["events_already_present"])
        self.assertEqual(len(self._feedback_events()), 2)

    def test_historical_backfill_rejects_missing_ledger_or_partial_events(self):
        self._insert("one")
        self._insert("two")
        decisions = [
            {"handle": "one", "action": "approved", "reason": ""},
            {"handle": "two", "action": "rejected", "reason": ""},
        ]
        kwargs = {
            "batch_id": BATCH,
            "file_sha256": "a" * 64,
            "source_sha256": "b" * 64,
            "backfill_events": True,
        }
        with self.assertRaisesRegex(cc.ClientDecisionImportError, "已有同 SHA ledger"):
            cc.apply_client_decisions(decisions, **kwargs)

        conn = cc._conn()
        try:
            conn.execute(
                """INSERT INTO client_feedback_imports
                   (file_sha256, batch_id, source_sha256, imported_at,
                    decision_count, approved_count, rejected_count, pending_count,
                    adopted_existing)
                   VALUES (?,?,?,?,?,?,?,?,0)""",
                ("a" * 64, BATCH, "b" * 64, "2026-07-23T09:00:00", 2, 1, 1, 0),
            )
            conn.execute(
                """INSERT INTO client_feedback_events
                   (event_id, review_batch, origin_batch, handle, verdict,
                    feedback_file_sha256, source_decisions_sha256,
                    taxonomy_version, imported_at, source_mode)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    "partial",
                    BATCH,
                    BATCH,
                    "one",
                    "approved",
                    "a" * 64,
                    "b" * 64,
                    ingest.TAXONOMY_VERSION,
                    "2026-07-23T09:00:00",
                    "historical_backfill",
                ),
            )
            conn.commit()
        finally:
            conn.close()
        with self.assertRaisesRegex(cc.ClientDecisionImportError, "部分逐条 events"):
            cc.apply_client_decisions(decisions, **kwargs)
        self.assertEqual(len(self._feedback_events()), 1)


class TestAdoptExisting(ClientDecisionTestCase):
    def test_requires_explicit_adoption_then_repairs_without_refreshing_timestamp(self):
        old_approved_at = "2026-07-22T09:10:00"
        self._insert(
            "legacy_approved",
            tier=2,
            client_status="approved",
            approved_at=old_approved_at,
            rejected_reason="stale rejection",
            source_batch=BATCH,
        )
        self._insert(
            "legacy_rejected",
            tier=2,
            client_status="rejected",
            approved_at="2026-01-01T00:00:00",
            rejected_reason="not a fit",
            source_batch=BATCH,
        )
        self._insert(
            "legacy_pending",
            client_note="[待定] later",
        )
        feedback, source = self._write_payloads(
            [
                {"handle": "legacy_approved", "verdict": "合适", "reason": ""},
                {"handle": "legacy_rejected", "verdict": "不合适", "reason": "not a fit"},
                {"handle": "legacy_pending", "verdict": "待定", "reason": "later"},
            ]
        )
        payload = ingest.load_validated_decisions(feedback, source)
        kwargs = {
            "batch_id": payload["batch"],
            "file_sha256": payload["file_sha256"],
            "source_sha256": payload["source_sha256"],
        }
        with self.assertRaisesRegex(cc.ClientDecisionImportError, "adopt-existing"):
            cc.apply_client_decisions(payload["decisions"], **kwargs)

        result = cc.apply_client_decisions(
            payload["decisions"], adopt_existing=True, **kwargs
        )
        self.assertTrue(result["adopted_existing"])
        approved = self._row("legacy_approved")
        self.assertEqual(approved["approved_at"], old_approved_at)
        self.assertIsNone(approved["rejected_reason"])
        rejected = self._row("legacy_rejected")
        self.assertIsNone(rejected["approved_at"])
        self.assertEqual(rejected["tier"], 1)
        self.assertEqual(self._row("legacy_pending")["client_note"], "[待定] later")
        conn = cc._conn()
        try:
            ledger = conn.execute(
                "SELECT adopted_existing FROM client_feedback_imports"
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(ledger["adopted_existing"], 1)
        self.assertEqual(
            {event["source_mode"] for event in self._feedback_events()},
            {"adopt_existing"},
        )

    def test_adoption_aborts_if_existing_state_does_not_match(self):
        self._insert(
            "legacy_rejected",
            tier=2,
            client_status="rejected",
            approved_at="must remain",
            rejected_reason="different reason",
            source_batch=BATCH,
        )
        decisions = [
            {"handle": "legacy_rejected", "action": "rejected", "reason": "expected"}
        ]
        with self.assertRaisesRegex(cc.ClientDecisionImportError, "核对失败"):
            cc.apply_client_decisions(
                decisions,
                batch_id=BATCH,
                file_sha256="c" * 64,
                source_sha256="d" * 64,
                adopt_existing=True,
            )
        row = self._row("legacy_rejected")
        self.assertEqual(row["approved_at"], "must remain")
        self.assertEqual(row["tier"], 2)


class TestStrictFileContract(ClientDecisionTestCase):
    def test_rejects_duplicate_unknown_off_whitelist_and_batch_mismatch(self):
        cases = [
            (
                "duplicate",
                [
                    {"handle": "Same", "verdict": "合适"},
                    {"handle": "same", "verdict": "不合适"},
                ],
                None,
                "重复账号",
            ),
            (
                "unknown verdict",
                [{"handle": "one", "verdict": "随便"}],
                None,
                "verdict 未知",
            ),
            (
                "batch mismatch",
                [{"handle": "one", "verdict": "合适"}],
                "OTHER",
                "批次不一致",
            ),
        ]
        for name, decisions, source_batch, message in cases:
            with self.subTest(name=name):
                feedback, source = self._write_payloads(
                    decisions, source_batch=source_batch
                )
                with self.assertRaisesRegex(ingest.FeedbackValidationError, message):
                    ingest.load_validated_decisions(feedback, source)

        feedback, source = self._write_payloads(
            [{"handle": "outside", "verdict": "合适"}]
        )
        source.write_text(
            json.dumps(
                {
                    "manifest": {"batch_id": BATCH},
                    "candidates": [{"handle": "inside", "_discovery_batch": BATCH}],
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ingest.FeedbackValidationError, "白名单外"):
            ingest.load_validated_decisions(feedback, source)

    def test_legacy_dry_run_still_works_but_real_write_requires_source(self):
        feedback, _ = self._write_payloads(
            [{"handle": "one", "verdict": "待定", "reason": ""}]
        )
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            self.assertEqual(ingest.main(["--file", str(feedback), "--dry-run"]), 0)
            self.assertEqual(ingest.main(["--file", str(feedback)]), 2)
        self.assertIn("仅完成旧版格式预览", stdout.getvalue())
        self.assertIn("--source-decisions", stderr.getvalue())
        self.assertFalse(cc.DB.exists())

    def test_event_backfill_requires_source_even_in_dry_run(self):
        feedback, _ = self._write_payloads(
            [{"handle": "one", "verdict": "待定", "reason": ""}]
        )
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result = ingest.main(
                ["--file", str(feedback), "--backfill-events", "--dry-run"]
            )
        self.assertEqual(result, 2)
        self.assertIn("--backfill-events 必须同时提供", stderr.getvalue())
        self.assertFalse(cc.DB.exists())

    def test_reason_only_row_is_pending(self):
        feedback, source = self._write_payloads(
            [{"handle": "one", "verdict": "", "reason": "needs another look"}]
        )
        payload = ingest.load_validated_decisions(feedback, source)
        self.assertEqual(payload["decisions"][0]["action"], "pending")

    def test_mixed_origin_review_preserves_discovery_batch(self):
        origin = "SKIN3-ORIGIN"
        self._insert("carryover", discovery_batch=origin)
        feedback, source = self._write_payloads(
            [{"handle": "carryover", "verdict": "合适", "pool": "Review", "score": "7.2"}]
        )
        source.write_text(
            json.dumps(
                {
                    "manifest": {
                        "batch_id": BATCH,
                        "batches": [origin, BATCH],
                    },
                    "candidates": [
                        {
                            "handle": "carryover",
                            "_discovery_batch": origin,
                            "final_pool": "Review",
                            "ai_vetting_score": 7.2,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        payload = ingest.load_validated_decisions(feedback, source)
        self.assertEqual(payload["decisions"][0]["origin_batch"], origin)
        cc.apply_client_decisions(
            payload["decisions"],
            batch_id=payload["batch"],
            file_sha256=payload["file_sha256"],
            source_sha256=payload["source_sha256"],
        )
        row = self._row("carryover")
        self.assertEqual(row["discovery_batch"], origin)
        self.assertEqual(row["source_batch"], BATCH)
        self.assertEqual(row["client_status"], "approved")

    def test_rejects_pool_or_score_mismatch(self):
        for field, value, message in (
            ("pool", "Exclude", "pool"),
            ("score", "9.9", "score"),
        ):
            with self.subTest(field=field):
                feedback, source = self._write_payloads(
                    [
                        {
                            "handle": "one",
                            "verdict": "合适",
                            "pool": "Review",
                            "score": "7.2",
                        }
                    ]
                )
                source.write_text(
                    json.dumps(
                        {
                            "manifest": {"batch_id": BATCH, "batches": [BATCH]},
                            "candidates": [
                                {
                                    "handle": "one",
                                    "_discovery_batch": BATCH,
                                    "final_pool": "Review",
                                    "ai_vetting_score": 7.2,
                                }
                            ],
                        }
                    ),
                    encoding="utf-8",
                )
                data = json.loads(feedback.read_text(encoding="utf-8"))
                data["decisions"][0][field] = value
                feedback.write_text(json.dumps(data), encoding="utf-8")
                with self.assertRaisesRegex(ingest.FeedbackValidationError, message):
                    ingest.load_validated_decisions(feedback, source)


class TestRejectionScopeRuntime(ClientDecisionTestCase):
    def test_only_global_or_legacy_rejections_are_permanent_negative_assets(self):
        for handle in ("global", "campaign", "temporary"):
            self._insert(handle)
        cc.apply_client_decisions(
            [
                {
                    "handle": "global",
                    "action": "rejected",
                    "reason": "",
                    "rejection_scope": "global",
                },
                {
                    "handle": "campaign",
                    "action": "rejected",
                    "reason": "",
                    "rejection_scope": "campaign",
                },
                {
                    "handle": "temporary",
                    "action": "rejected",
                    "reason": "",
                    "rejection_scope": "temporary",
                },
            ],
            batch_id=BATCH,
            file_sha256="a" * 64,
            source_sha256="b" * 64,
        )
        self._insert(
            "legacy",
            client_status="rejected",
            client_rejection_scope=None,
            rejected_reason="legacy permanent rejection",
        )

        self.assertTrue(cc.is_rejected("GLOBAL"))
        self.assertTrue(cc.is_rejected("legacy"))
        self.assertFalse(cc.is_rejected("campaign"))
        self.assertFalse(cc.is_rejected("temporary"))

    def test_explicit_recollect_reopens_scoped_but_not_permanent_rejections(self):
        self._insert(
            "campaign",
            client_status="rejected",
            client_rejection_scope="campaign",
            rejected_reason="this campaign only",
        )
        self._insert(
            "temporary",
            client_status="rejected",
            client_rejection_scope="temporary",
            rejected_reason="retry later",
        )
        self._insert(
            "global",
            client_status="rejected",
            client_rejection_scope="global",
        )
        self._insert("legacy", client_status="rejected")

        self.assertEqual(
            cc.validate_recollect_scope(["campaign", "temporary"]),
            {"campaign", "temporary"},
        )
        self.assertEqual(cc.requeue_for_recollect(["campaign", "temporary"]), 2)
        for handle in ("campaign", "temporary"):
            row = self._row(handle)
            self.assertEqual(row["status"], "qualified")
            self.assertIsNone(row["client_status"])
            self.assertIsNone(row["rejected_reason"])
            self.assertIsNone(row["client_rejection_scope"])

        for handle in ("global", "legacy"):
            with self.subTest(handle=handle):
                with self.assertRaisesRegex(ValueError, "客户终判|client_final"):
                    cc.requeue_for_recollect([handle])
                self.assertEqual(self._row(handle)["client_status"], "rejected")

    def test_legacy_mark_rejected_remains_global_and_approval_clears_scope(self):
        self._insert("legacy_api")
        cc.mark_rejected("legacy_api", "no", BATCH)
        self.assertEqual(
            self._row("legacy_api")["client_rejection_scope"], "global"
        )
        cc.promote_golden("legacy_api", BATCH)
        row = self._row("legacy_api")
        self.assertEqual(row["client_status"], "approved")
        self.assertIsNone(row["client_rejection_scope"])


class TestGoldenSeeds(ClientDecisionTestCase):
    def test_only_client_approved_tier2_rows_are_seeds(self):
        self._insert(
            "approved", tier=2, client_status="approved", approved_at="2026-07-23"
        )
        self._insert(
            "collaborated",
            tier=2,
            client_status="collaborated",
            approved_at="2026-07-22",
        )
        self._insert("legacy_null", tier=2, client_status=None)
        self._insert("rejected", tier=2, client_status="rejected")
        self.assertEqual(cc.golden_seeds(), ["approved", "collaborated"])


if __name__ == "__main__":
    unittest.main()
