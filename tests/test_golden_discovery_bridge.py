"""approved 金种子 → 下一轮 Stage1 的离线、可审计桥接测试。"""
from __future__ import annotations

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
from extensions.sop_v2.pipeline import modash_search as ms  # noqa: E402
from extensions.sop_v2.pipeline import stage1_discover as stage1  # noqa: E402


class TestGoldenDiscoveryBridge(unittest.TestCase):
    def test_golden_seeds_excludes_non_client_approved_tier2(self):
        old_db = cc.DB
        with tempfile.TemporaryDirectory() as td:
            cc.DB = Path(td) / "cache.db"
            try:
                conn = cc._conn()
                try:
                    rows = [
                        ("approved_one", 2, "approved", "2026-07-23T10:00:00"),
                        ("collab_one", 2, "collaborated", "2026-07-23T09:00:00"),
                        ("tier2_rejected", 2, "rejected", "2026-07-23T08:00:00"),
                        ("tier2_pending", 2, None, "2026-07-23T07:00:00"),
                        ("tier1_approved", 1, "approved", "2026-07-23T06:00:00"),
                    ]
                    conn.executemany(
                        """INSERT INTO creator_profiles
                           (handle,tier,client_status,approved_at,first_seen,last_scanned)
                           VALUES (?,?,?,?,?,?)""",
                        [(h, tier, status, approved, approved, approved)
                         for h, tier, status, approved in rows],
                    )
                    conn.commit()
                finally:
                    conn.close()
                self.assertEqual(cc.golden_seeds(), ["approved_one", "collab_one"])
            finally:
                cc.DB = old_db

    def test_golden_seed_limit_is_stable_when_approval_times_tie(self):
        old_db = cc.DB
        with tempfile.TemporaryDirectory() as td:
            cc.DB = Path(td) / "cache.db"
            try:
                conn = cc._conn()
                try:
                    approved_at = "2026-07-23T10:00:00"
                    conn.executemany(
                        """INSERT INTO creator_profiles
                           (handle,tier,client_status,approved_at,first_seen,last_scanned)
                           VALUES (?,2,'approved',?,?,?)""",
                        [
                            (handle, approved_at, approved_at, approved_at)
                            for handle in ("zeta", "Alpha", "beta")
                        ],
                    )
                    conn.commit()
                finally:
                    conn.close()

                self.assertEqual(cc.golden_seeds(limit=2), ["Alpha", "beta"])
            finally:
                cc.DB = old_db

    def test_manifest_and_manual_results_keep_seed_lineage(self):
        golden = ["Approved.One", "collab_two"]
        manifest = ms.build_golden_seed_manifest(
            "SKIN4-TEST", golden, generated_at="2026-07-23T10:00:00+0800"
        )
        self.assertEqual(manifest["seed_count"], 2)
        self.assertIn("client_status IN", manifest["eligibility"])

        payload = {
            "schema_version": 1,
            "batch_id": "SKIN4-TEST",
            "seed_set_sha256": manifest["seed_set_sha256"],
            "results": [
                {
                    "seed_handle": "approved.one",
                    "candidates": [
                        {"handle": "new.creator", "followers": 12000, "er_pct": 2.4},
                    ],
                },
                {
                    "seed_handle": "collab_two",
                    "candidates": [
                        {"handle": "NEW.creator", "followers": 12500, "er_pct": 2.5},
                        {"handle": "second_creator"},
                    ],
                },
            ],
        }
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "lookalikes.json"
            p.write_text(json.dumps(payload), encoding="utf-8")
            records = ms.load_golden_lookalikes(
                p, batch_id="SKIN4-TEST", golden_handles=golden
            )

        self.assertEqual([r["handle"].lower() for r in records],
                         ["new.creator", "second_creator"])
        first = records[0]
        self.assertEqual(first["discovered_via"], "multi_source")
        self.assertEqual(
            first["golden_seed_handles"],
            ["approved.one", "collab_two"],
        )
        self.assertEqual(len(first["discovery_sources"]), 2)

    def test_manifest_writer_does_not_overwrite_a_different_seed_cohort(self):
        first = ms.build_golden_seed_manifest(
            "SKIN4-TEST", ["approved_one"], generated_at="first"
        )
        second = ms.build_golden_seed_manifest(
            "SKIN4-TEST", ["approved_two"], generated_at="second"
        )
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "golden.json"
            first_path = ms.write_golden_seed_manifest(target, first)
            second_path = ms.write_golden_seed_manifest(target, second)
            self.assertEqual(first_path, target)
            self.assertNotEqual(second_path, target)
            self.assertEqual(
                json.loads(target.read_text(encoding="utf-8"))["seed_set_sha256"],
                first["seed_set_sha256"],
            )
            self.assertEqual(
                json.loads(second_path.read_text(encoding="utf-8"))["seed_set_sha256"],
                second["seed_set_sha256"],
            )

    def test_rejects_unapproved_source_and_opaque_cached_token(self):
        golden = ["approved_one"]
        manifest = ms.build_golden_seed_manifest("SKIN4-TEST", golden)
        bad = {
            "schema_version": 1,
            "batch_id": "SKIN4-TEST",
            "seed_set_sha256": manifest["seed_set_sha256"],
            "results": [{"seed_handle": "not_approved", "candidates": []}],
        }
        opaque_cache = {
            "error": False,
            "lookalikesToken": "opaque-token-is-not-a-candidate-list",
            "profile": {},
        }
        with tempfile.TemporaryDirectory() as td:
            bad_path = Path(td) / "bad.json"
            bad_path.write_text(json.dumps(bad), encoding="utf-8")
            with self.assertRaises(ms.GoldenLookalikeInputError):
                ms.load_golden_lookalikes(
                    bad_path, batch_id="SKIN4-TEST", golden_handles=golden
                )

            raw_path = Path(td) / "raw-report.json"
            raw_path.write_text(json.dumps(opaque_cache), encoding="utf-8")
            with self.assertRaises(ms.GoldenLookalikeInputError):
                ms.load_golden_lookalikes(
                    raw_path, batch_id="SKIN4-TEST", golden_handles=golden
                )

    def test_ingest_persists_audit_source_without_overwriting_existing(self):
        old_db = cc.DB
        with tempfile.TemporaryDirectory() as td:
            cc.DB = Path(td) / "cache.db"
            try:
                conn = cc._conn()
                try:
                    conn.execute(
                        """INSERT INTO creator_profiles
                           (handle,status,discovery_batch,stage_json,first_seen,last_scanned)
                           VALUES (?,?,?,?,?,?)""",
                        (
                            "existing_creator",
                            "decided",
                            "OLD-BATCH",
                            json.dumps({"handle": "existing_creator",
                                        "discovered_via": "old_source"}),
                            "2026-07-20T00:00:00",
                            "2026-07-20T00:00:00",
                        ),
                    )
                    conn.commit()
                finally:
                    conn.close()
                records = [
                    {
                        "handle": "new_creator",
                        "followers": 12345,
                        "er": 2.4,
                        "discovered_via": "modash_manual_lookalike:golden:approved_one",
                        "discovery_sources": [
                            "modash_manual_lookalike:golden:approved_one"
                        ],
                        "golden_seed_handles": ["approved_one"],
                    },
                    {
                        "handle": "existing_creator",
                        "followers": 99999,
                        "er": 9.9,
                        "discovered_via": "modash_structured_search",
                        "discovery_sources": ["modash_structured_search"],
                    },
                ]
                result = stage1.ingest_discovered_seeds(
                    records, "SKIN4-TEST", cache=cc
                )
                self.assertEqual(result["new_seeds"], 1)
                self.assertEqual(result["audited_new"], 1)

                conn = sqlite3.connect(
                    f"file:{cc.DB}?mode=ro", uri=True
                )
                try:
                    new_row = conn.execute(
                        "SELECT status,discovery_batch,stage_json FROM creator_profiles "
                        "WHERE handle='new_creator'"
                    ).fetchone()
                    old_row = conn.execute(
                        "SELECT status,discovery_batch,stage_json FROM creator_profiles "
                        "WHERE handle='existing_creator'"
                    ).fetchone()
                finally:
                    conn.close()
                stage_json = json.loads(new_row[2])
                self.assertEqual(new_row[:2], ("seed", "SKIN4-TEST"))
                self.assertEqual(
                    stage_json["discovered_via"],
                    "modash_manual_lookalike:golden:approved_one",
                )
                self.assertEqual(stage_json["golden_seed_handles"], ["approved_one"])
                self.assertEqual(old_row[:2], ("decided", "OLD-BATCH"))
                self.assertEqual(json.loads(old_row[2])["discovered_via"], "old_source")
            finally:
                cc.DB = old_db

    def test_export_only_never_calls_modash_or_candidate_db_writes(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "golden.json"
            argv = [
                "stage1",
                "--batch-id", "SKIN4-TEST",
                "--golden-seeds-out", str(out),
                "--export-golden-only",
            ]
            with mock.patch.object(sys, "argv", argv), \
                    mock.patch.object(stage1.cc, "golden_seeds",
                                      return_value=["approved_one"]), \
                    mock.patch.object(stage1.cc, "seed_handles") as seed_handles, \
                    mock.patch.object(stage1.ms, "discover") as discover:
                self.assertEqual(stage1.main(), 0)
            seed_handles.assert_not_called()
            discover.assert_not_called()
            manifest = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(manifest["seeds"], [{"handle": "approved_one"}])

    def test_require_mode_stops_before_generic_search_without_manual_results(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "golden.json"
            argv = [
                "stage1",
                "--batch-id", "SKIN4-TEST",
                "--golden-seeds-out", str(out),
                "--require-golden-lookalikes",
            ]
            with mock.patch.object(sys, "argv", argv), \
                    mock.patch.object(stage1.cc, "golden_seeds",
                                      return_value=["approved_one"]), \
                    mock.patch.object(stage1.ms, "discover") as discover:
                self.assertEqual(stage1.main(), 2)
            discover.assert_not_called()

    def test_require_mode_rejects_unfilled_export_template(self):
        golden = ["approved_one"]
        manifest = ms.build_golden_seed_manifest("SKIN4-TEST", golden)
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "golden.json"
            result_path = Path(td) / "unfilled.json"
            result_path.write_text(json.dumps(manifest), encoding="utf-8")
            argv = [
                "stage1",
                "--batch-id", "SKIN4-TEST",
                "--golden-seeds-out", str(out),
                "--golden-lookalikes-json", str(result_path),
                "--require-golden-lookalikes",
            ]
            with mock.patch.object(sys, "argv", argv), \
                    mock.patch.object(stage1.cc, "golden_seeds",
                                      return_value=golden), \
                    mock.patch.object(stage1.ms, "discover") as discover:
                self.assertEqual(stage1.main(), 2)
            discover.assert_not_called()

    def test_valid_manual_results_can_seed_when_generic_modash_is_unavailable(self):
        golden = ["approved_one"]
        manifest = ms.build_golden_seed_manifest("SKIN4-TEST", golden)
        manifest["results"][0]["candidates"] = [
            {"handle": "new_creator", "followers": 18000, "er_pct": 2.8}
        ]
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "golden.json"
            result_path = Path(td) / "lookalikes.json"
            result_path.write_text(json.dumps(manifest), encoding="utf-8")
            argv = [
                "stage1",
                "--batch-id", "SKIN4-TEST",
                "--golden-seeds-out", str(out),
                "--golden-lookalikes-json", str(result_path),
                "--require-golden-lookalikes",
            ]
            with mock.patch.object(sys, "argv", argv), \
                    mock.patch.object(stage1.cc, "golden_seeds",
                                      return_value=golden), \
                    mock.patch.object(stage1.ms, "discover",
                                      return_value={"error": "no_modash_tab",
                                                    "seeds": []}), \
                    mock.patch.object(stage1, "ingest_discovered_seeds",
                                      return_value={"new_seeds": 1,
                                                    "deduped": 0,
                                                    "rejected_skipped": 0,
                                                    "audited_new": 1}) as ingest, \
                    mock.patch.object(stage1.cc, "status_dist",
                                      return_value={"seed": 1}):
                self.assertEqual(stage1.main(), 0)
            ingested = ingest.call_args.args[0]
            self.assertEqual(len(ingested), 1)
            self.assertEqual(
                ingested[0]["discovered_via"],
                "modash_manual_lookalike:golden:approved_one",
            )


if __name__ == "__main__":
    unittest.main()
