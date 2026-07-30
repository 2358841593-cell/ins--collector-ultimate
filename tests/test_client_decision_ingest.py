"""客户反馈安全闭环：严格文件校验、事务落库、ledger 幂等与旧状态接管。"""
from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

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
        self.assertEqual(rejected["source_batch"], BATCH)
        self.assertEqual(rejected["client_note"], "keep this independent note")

        pending = self._row("pending_me")
        self.assertIsNone(pending["client_status"])
        self.assertEqual(pending["tier"], 1)
        self.assertIsNone(pending["approved_at"])
        self.assertIsNone(pending["rejected_reason"])
        self.assertEqual(pending["source_batch"], "UNCHANGED")
        self.assertEqual(pending["client_note"], "[待定] review later")

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
