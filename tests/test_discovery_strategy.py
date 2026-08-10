from __future__ import annotations

import json
import sys
from unittest import mock

import pytest

from extensions.sop_v2 import discovery_strategy as ds
from extensions.sop_v2 import round_contract as rc
from extensions.sop_v2.config import load_config
from extensions.sop_v2.pipeline import stage1_discover as stage1


MIX = {"golden_lookalike": 50, "generic_commerce": 30, "exploration": 20}


def test_full_golden_supply_keeps_50_30_20_mix():
    plan = ds.build_source_plan(target=100, golden_available=80, source_mix_pct=MIX)
    assert plan["targets"] == {
        "golden_lookalike": 50,
        "generic_commerce": 30,
        "exploration": 20,
    }


def test_golden_shortfall_is_reallocated_without_losing_exploration():
    plan = ds.build_source_plan(target=100, golden_available=20, source_mix_pct=MIX)
    assert plan["targets"] == {
        "golden_lookalike": 20,
        "generic_commerce": 48,
        "exploration": 32,
    }
    assert plan["golden_shortfall"] == 30


def test_no_golden_results_falls_back_to_60_40_structured_mix():
    plan = ds.build_source_plan(target=111, golden_available=0, source_mix_pct=MIX)
    assert plan["targets"] == {
        "golden_lookalike": 0,
        "generic_commerce": 67,
        "exploration": 44,
    }


@pytest.mark.parametrize(
    "mix",
    [
        {"golden_lookalike": 50, "generic_commerce": 50},
        {"golden_lookalike": 50, "generic_commerce": 31, "exploration": 20},
        {"golden_lookalike": 100, "generic_commerce": 0, "exploration": 0},
    ],
)
def test_invalid_source_mix_fails_before_browser_work(mix):
    with pytest.raises(ds.DiscoveryStrategyError):
        ds.build_source_plan(target=100, golden_available=0, source_mix_pct=mix)


def test_repository_config_has_both_non_amazon_source_buckets():
    cfg = load_config()
    queries = ds.structured_queries(cfg["discovery"])
    assert set(queries) == {"generic_commerce", "exploration"}
    assert "amazon" not in queries["generic_commerce"].lower()
    assert "skincare" in queries["exploration"].lower()


def test_stage1_enforces_fallback_mix_and_persists_bucket_provenance(tmp_path):
    observed_targets = []

    def fake_discover(query, filters, *, target, **kwargs):
        observed_targets.append(target)
        bucket = "generic" if "shopping links" in query else "explore"
        return {
            "seeds": [{"handle": f"{bucket}_{i}"} for i in range(target)],
            "raw_scanned": target,
            "kept": target,
            "filtered_out": 0,
        }

    argv = [
        "stage1",
        "--batch-id", "B-MIX",
        "--target", "10",
        "--golden-seeds-out", str(tmp_path / "golden.json"),
    ]
    with mock.patch.object(sys, "argv", argv), \
            mock.patch.object(stage1.cc, "golden_seeds", return_value=[]), \
            mock.patch.object(stage1.ms, "discover", side_effect=fake_discover), \
            mock.patch.object(
                stage1,
                "ingest_discovered_seeds",
                return_value={"new_seeds": 10, "deduped": 0, "rejected_skipped": 0},
            ) as ingest, \
            mock.patch.object(stage1.cc, "status_dist", return_value={"seed": 10}):
        assert stage1.main() == 0

    assert observed_targets == [6, 4]
    records = ingest.call_args.args[0]
    assert len(records) == 10
    assert sum(
        "modash_structured_search:generic_commerce" in row["discovery_sources"]
        for row in records
    ) == 6
    assert sum(
        "modash_structured_search:exploration" in row["discovery_sources"]
        for row in records
    ) == 4


def test_global_partition_merges_sources_before_filtering():
    class FakeCache:
        def __init__(self):
            self.checked = []

        def should_ingest_seed(self, handle):
            self.checked.append(handle.lower())
            return handle.lower() != "already_known"

    cache = FakeCache()
    ingestible, existing = stage1.partition_globally_ingestible_seeds(
        [
            {
                "handle": "MultiSource",
                "discovery_sources": [
                    "modash_manual_lookalike:golden:seed_a"
                ],
                "golden_seed_handles": ["seed_a"],
            },
            {
                "handle": "multisource",
                "discovery_sources": [
                    "modash_structured_search:generic_commerce"
                ],
            },
            {
                "handle": "already_known",
                "discovery_sources": [
                    "modash_structured_search:exploration"
                ],
            },
        ],
        cache=cache,
    )

    assert cache.checked == ["multisource", "already_known"]
    assert [row["handle"].lower() for row in ingestible] == ["multisource"]
    assert [row["handle"] for row in existing] == ["already_known"]
    assert ingestible[0]["discovery_sources"] == [
        "modash_manual_lookalike:golden:seed_a",
        "modash_structured_search:generic_commerce",
    ]
    assert ingestible[0]["golden_seed_handles"] == ["seed_a"]


def test_global_partition_db_lookup_is_case_insensitive(tmp_path, monkeypatch):
    monkeypatch.setattr(stage1.cc, "DB", tmp_path / "creator_cache.db")
    stage1.cc.seed_handles([{"handle": "CaseSensitiveCreator"}], "OLD")

    ingestible, existing = stage1.partition_globally_ingestible_seeds(
        [{"handle": "casesensitivecreator", "discovery_sources": ["source"]}],
        cache=stage1.cc,
    )

    assert ingestible == []
    assert [row["handle"] for row in existing] == ["casesensitivecreator"]


def _write_contract(tmp_path, *, mode="new_only"):
    contract = rc.build_round_contract(
        batch_id="B-FORMAL",
        campaign_track="paid",
        carryover_mode=mode,
        created_at="2026-08-10T12:00:00+0800",
    )
    path = tmp_path / f"round-{mode}.json"
    rc.write_round_contract(path, contract)
    return path, contract


def test_formal_contract_rejects_ai_search_when_exploration_is_required(tmp_path):
    contract_path, _ = _write_contract(tmp_path)
    argv = [
        "stage1",
        "--batch-id", "B-FORMAL",
        "--target", "10",
        "--ai-search",
        "--round-contract", str(contract_path),
        "--golden-seeds-out", str(tmp_path / "golden.json"),
        "--stage1-barrier-out", str(tmp_path / "stage1-barrier.json"),
    ]
    with mock.patch.object(sys, "argv", argv), \
            mock.patch.object(stage1.cc, "golden_seeds", return_value=[]), \
            mock.patch.object(stage1, "discover_seeds") as discover:
        assert stage1.main() == 2
    discover.assert_not_called()


@pytest.mark.parametrize("result_kind", ["error", "short"])
def test_formal_contract_blocks_failed_or_short_structured_bucket(
    tmp_path, result_kind
):
    contract_path, _ = _write_contract(tmp_path)
    calls = 0

    def fake_discover(query, filters, *, target, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1 and result_kind == "error":
            return {"error": "no_modash_tab", "seeds": []}
        actual = target - 1 if calls == 1 and result_kind == "short" else target
        return {
            "seeds": [{"handle": f"bucket{calls}_{i}"} for i in range(actual)],
            "raw_scanned": actual,
            "kept": actual,
            "filtered_out": 0,
        }

    argv = [
        "stage1",
        "--batch-id", "B-FORMAL",
        "--target", "10",
        "--round-contract", str(contract_path),
        "--golden-seeds-out", str(tmp_path / "golden.json"),
    ]
    with mock.patch.object(sys, "argv", argv), \
            mock.patch.object(stage1.cc, "golden_seeds", return_value=[]), \
            mock.patch.object(stage1.ms, "discover", side_effect=fake_discover), \
            mock.patch.object(stage1, "ingest_discovered_seeds") as ingest:
        assert stage1.main() == 2
    ingest.assert_not_called()


def test_formal_contract_accepts_only_after_exact_bucket_and_unique_target(tmp_path):
    contract_path, _ = _write_contract(tmp_path)
    observed = []

    def fake_discover(query, filters, *, target, **kwargs):
        bucket = "generic" if not observed else "explore"
        observed.append(target)
        return {
            "seeds": [{"handle": f"{bucket}_{i}"} for i in range(target)],
            "raw_scanned": target,
            "kept": target,
            "filtered_out": 0,
        }

    argv = [
        "stage1",
        "--batch-id", "B-FORMAL",
        "--target", "10",
        "--round-contract", str(contract_path),
        "--golden-seeds-out", str(tmp_path / "golden.json"),
    ]
    with mock.patch.object(sys, "argv", argv), \
            mock.patch.object(stage1.cc, "golden_seeds", return_value=[]), \
            mock.patch.object(stage1.ms, "discover", side_effect=fake_discover), \
            mock.patch.object(stage1.cc, "should_ingest_seed", return_value=True), \
            mock.patch.object(
                stage1,
                "ingest_discovered_seeds",
                return_value={"new_seeds": 10, "audited_new": 10,
                              "deduped": 0, "rejected_skipped": 0},
            ) as ingest, \
            mock.patch.object(
                stage1.barriers,
                "evaluate_b1",
                return_value=mock.Mock(passed=True, failures=()),
            ), \
            mock.patch.object(stage1.cc, "status_dist", return_value={"seed": 10}):
        assert stage1.main() == 0

    assert observed == [6, 4]
    assert len(ingest.call_args.args[0]) == 10


def test_modash_discover_quota_predicate_scans_backup_pages():
    def raw(handle):
        return {
            "username": handle,
            "follower_count": 20_000,
            "engagement_rate": 0.02,
            "is_brand": False,
            "is_private": False,
            "creator_description": "commerce creator",
        }

    manager = mock.MagicMock()
    playwright = manager.__enter__.return_value
    browser = playwright.chromium.connect_over_cdp.return_value
    page = mock.MagicMock()
    sleeps = []
    pages = [
        [raw("historical_0"), raw("historical_1")],
        [raw("fresh_0"), raw("fresh_1")],
    ]
    with mock.patch(
        "playwright.sync_api.sync_playwright", return_value=manager
    ), mock.patch.object(stage1.ms, "_find_modash", return_value=page), \
            mock.patch.object(stage1.ms, "_page", side_effect=pages) as fetch_page:
        result = stage1.ms.discover(
            "query",
            {},
            target=2,
            max_pages=5,
            page_delay=0,
            require_amazon_bio=False,
            quota_accept=lambda rec: rec["handle"].startswith("fresh_"),
            sleeper=sleeps.append,
        )

    assert fetch_page.call_count == 2
    assert result["accepted"] == 2
    assert [row["handle"] for row in result["seeds"]] == [
        "historical_0",
        "historical_1",
        "fresh_0",
        "fresh_1",
    ]
    browser.close.assert_not_called()
    page.wait_for_timeout.assert_not_called()
    assert sleeps == [0.3, 0.3, 0.4]


def test_modash_page_fetch_timeout_is_explicit_and_bounded():
    page = mock.MagicMock()
    page.evaluate.return_value = {
        "status": None,
        "text": "",
        "timedOut": True,
        "error": "request_timeout",
    }

    result = stage1.ms._page(
        page,
        "skincare",
        {"followers": {"min": 10_000}},
        skip=12,
        request_timeout_ms=3210,
    )

    assert result == {
        "results": [],
        "error": "request_timeout",
        "status": None,
    }
    payload = page.evaluate.call_args.args[1]
    assert payload["timeoutMs"] == 3210
    assert payload["body"]["skip"] == 12
    assert "AbortController" in stage1.ms._SEARCH_JS


def test_modash_discover_propagates_page_error_without_closing_cdp_browser():
    manager = mock.MagicMock()
    playwright = manager.__enter__.return_value
    browser = playwright.chromium.connect_over_cdp.return_value
    page = mock.MagicMock()
    sleeps = []

    with mock.patch(
        "playwright.sync_api.sync_playwright", return_value=manager
    ), mock.patch.object(stage1.ms, "_find_modash", return_value=page), \
            mock.patch.object(
                stage1.ms,
                "_page",
                return_value={
                    "results": [],
                    "error": "request_timeout",
                    "status": None,
                },
            ):
        result = stage1.ms.discover(
            "query",
            {},
            target=2,
            page_delay=0,
            sleeper=sleeps.append,
        )

    assert result["error"] == "request_timeout"
    assert result["seeds"] == []
    assert result["raw_scanned"] == 0
    browser.close.assert_not_called()
    page.wait_for_timeout.assert_not_called()
    assert sleeps == [0.3, 0.3]


def test_modash_page_http_and_json_errors_are_explicit():
    page = mock.MagicMock()
    page.evaluate.side_effect = [
        {"status": 429, "text": "rate limited", "timedOut": False, "error": None},
        {"status": 200, "text": "not-json", "timedOut": False, "error": None},
    ]

    assert stage1.ms._page(page, "q", {}, 0)["error"] == "http_429"
    assert stage1.ms._page(page, "q", {}, 0)["error"] == "invalid_json"


def test_formal_golden_filters_global_history_before_quota_slice(tmp_path):
    contract_path, _ = _write_contract(tmp_path)
    golden_records = [
        {
            "handle": handle,
            "discovery_sources": ["modash_manual_lookalike:golden:seed_a"],
            "golden_seed_handles": ["seed_a"],
        }
        for handle in [
            "historical_0",
            "historical_1",
            "golden_new_0",
            "golden_new_1",
            "golden_new_2",
            "golden_new_3",
            "golden_new_4",
        ]
    ]
    calls = 0

    def fake_discover(query, filters, *, target, **kwargs):
        nonlocal calls
        calls += 1
        prefix = "generic" if calls == 1 else "explore"
        return {
            "seeds": [{"handle": f"{prefix}_{i}"} for i in range(target)],
            "raw_scanned": target,
            "kept": target,
            "accepted": target,
            "filtered_out": 0,
        }

    argv = [
        "stage1",
        "--batch-id", "B-FORMAL",
        "--target", "10",
        "--round-contract", str(contract_path),
        "--golden-lookalikes-json", str(tmp_path / "lookalikes.json"),
        "--golden-seeds-out", str(tmp_path / "golden.json"),
        "--stage1-barrier-out", str(tmp_path / "stage1-barrier.json"),
    ]
    with mock.patch.object(sys, "argv", argv), \
            mock.patch.object(stage1.cc, "golden_seeds", return_value=["seed_a"]), \
            mock.patch.object(
                stage1.ms, "load_golden_lookalikes", return_value=golden_records
            ), \
            mock.patch.object(stage1.ms, "discover", side_effect=fake_discover), \
            mock.patch.object(
                stage1.cc,
                "should_ingest_seed",
                side_effect=lambda handle: not handle.startswith("historical_"),
            ), \
            mock.patch.object(
                stage1,
                "ingest_discovered_seeds",
                return_value={"new_seeds": 10, "audited_new": 10,
                              "deduped": 0, "rejected_skipped": 0},
            ) as ingest, \
            mock.patch.object(
                stage1.barriers,
                "evaluate_b1",
                return_value=mock.Mock(passed=True, failures=()),
            ), \
            mock.patch.object(stage1.cc, "status_dist", return_value={"seed": 10}):
        assert stage1.main() == 0

    ingested = ingest.call_args.args[0]
    handles = {row["handle"] for row in ingested}
    assert not {"historical_0", "historical_1"} & handles
    assert {f"golden_new_{i}" for i in range(5)} <= handles
    assert len(ingested) == 10


def test_formal_cross_bucket_duplicates_use_backups_and_keep_attribution(tmp_path):
    contract_path, _ = _write_contract(tmp_path)
    calls = 0

    def fake_discover(query, filters, *, target, quota_accept, **kwargs):
        nonlocal calls
        calls += 1
        pool = (
            [f"generic_{i}" for i in range(6)]
            if calls == 1
            else ["generic_0", *[f"explore_{i}" for i in range(4)]]
        )
        observed = []
        accepted = 0
        for handle in pool:
            rec = {"handle": handle}
            observed.append(rec)
            if quota_accept(rec):
                accepted += 1
            if accepted >= target:
                break
        return {
            "seeds": observed,
            "raw_scanned": len(observed),
            "kept": len(observed),
            "accepted": accepted,
            "filtered_out": 0,
        }

    argv = [
        "stage1",
        "--batch-id", "B-FORMAL",
        "--target", "10",
        "--round-contract", str(contract_path),
        "--golden-seeds-out", str(tmp_path / "golden.json"),
        "--stage1-barrier-out", str(tmp_path / "stage1-barrier.json"),
    ]
    with mock.patch.object(sys, "argv", argv), \
            mock.patch.object(stage1.cc, "golden_seeds", return_value=[]), \
            mock.patch.object(stage1.ms, "discover", side_effect=fake_discover), \
            mock.patch.object(stage1.cc, "should_ingest_seed", return_value=True), \
            mock.patch.object(
                stage1,
                "ingest_discovered_seeds",
                return_value={"new_seeds": 10, "audited_new": 10,
                              "deduped": 0, "rejected_skipped": 0},
            ) as ingest, \
            mock.patch.object(
                stage1.barriers,
                "evaluate_b1",
                return_value=mock.Mock(passed=True, failures=()),
            ), \
            mock.patch.object(stage1.cc, "status_dist", return_value={"seed": 10}):
        assert stage1.main() == 0

    ingested = ingest.call_args.args[0]
    assert len(ingested) == 10
    duplicate = next(row for row in ingested if row["handle"] == "generic_0")
    assert duplicate["discovery_sources"] == [
        "modash_structured_search:generic_commerce",
        "modash_structured_search:exploration",
    ]
    assert {row["handle"] for row in ingested} >= {
        f"explore_{i}" for i in range(4)
    }


def test_formal_contract_counts_only_globally_ingestible_accounts(
    tmp_path, capsys
):
    contract_path, _ = _write_contract(tmp_path)
    calls = 0

    def fake_discover(query, filters, *, target, **kwargs):
        nonlocal calls
        calls += 1
        prefix = "generic" if calls == 1 else "explore"
        handles = [f"{prefix}_{i}" for i in range(target)]
        if calls == 1:
            handles[0] = "already_global"
        return {
            "seeds": [{"handle": handle} for handle in handles],
            "raw_scanned": target,
            "kept": target,
            "filtered_out": 0,
        }

    argv = [
        "stage1",
        "--batch-id", "B-FORMAL",
        "--target", "10",
        "--round-contract", str(contract_path),
        "--golden-seeds-out", str(tmp_path / "golden.json"),
    ]
    with mock.patch.object(sys, "argv", argv), \
            mock.patch.object(stage1.cc, "golden_seeds", return_value=[]), \
            mock.patch.object(stage1.ms, "discover", side_effect=fake_discover), \
            mock.patch.object(
                stage1.cc,
                "should_ingest_seed",
                side_effect=lambda handle: handle.lower() != "already_global",
            ), \
            mock.patch.object(stage1, "ingest_discovered_seeds") as ingest:
        assert stage1.main() == 2

    ingest.assert_not_called()
    output = capsys.readouterr().out
    assert "全局去重与跨桶去重后配额不足" in output
    assert "要求 6，实际 5" in output
    assert "generic_commerce" in output


def test_formal_contract_rejects_ingest_race_that_reduces_new_count(tmp_path):
    contract_path, _ = _write_contract(tmp_path)
    calls = 0

    def fake_discover(query, filters, *, target, **kwargs):
        nonlocal calls
        calls += 1
        prefix = "generic" if calls == 1 else "explore"
        return {
            "seeds": [{"handle": f"{prefix}_{i}"} for i in range(target)],
            "raw_scanned": target,
            "kept": target,
            "filtered_out": 0,
        }

    argv = [
        "stage1",
        "--batch-id", "B-FORMAL",
        "--target", "10",
        "--round-contract", str(contract_path),
        "--golden-seeds-out", str(tmp_path / "golden.json"),
    ]
    with mock.patch.object(sys, "argv", argv), \
            mock.patch.object(stage1.cc, "golden_seeds", return_value=[]), \
            mock.patch.object(stage1.ms, "discover", side_effect=fake_discover), \
            mock.patch.object(stage1.cc, "should_ingest_seed", return_value=True), \
            mock.patch.object(
                stage1,
                "ingest_discovered_seeds",
                return_value={
                    "new_seeds": 9,
                    "deduped": 1,
                    "rejected_skipped": 0,
                    "audited_new": 9,
                },
            ), \
            mock.patch.object(stage1.cc, "status_dist") as status_dist:
        assert stage1.main() == 2

    status_dist.assert_not_called()


def test_formal_new_only_rejects_nonempty_carryover_before_discovery(tmp_path):
    contract_path, _ = _write_contract(tmp_path)
    contract_sha = rc.round_contract_sha256(contract_path)
    carryover_path = tmp_path / "carryover.json"
    carryover_path.write_text(
        json.dumps(
            {
                "next_batch_id": "B-FORMAL",
                "carryover_mode": "new_only",
                "round_contract_sha256": contract_sha,
                "counts": {
                    "source_candidates": 1,
                    "client_final": 0,
                    "carryover": 1,
                    "pipeline_retry": 0,
                },
                "retry_handles": [],
                "carryover": [
                    {"handle": "old", "needs_pipeline_retry": False}
                ],
            }
        ),
        encoding="utf-8",
    )
    argv = [
        "stage1",
        "--batch-id", "B-FORMAL",
        "--round-contract", str(contract_path),
        "--carryover-manifest", str(carryover_path),
        "--golden-seeds-out", str(tmp_path / "golden.json"),
    ]
    with mock.patch.object(sys, "argv", argv), \
            mock.patch.object(stage1.cc, "golden_seeds") as golden:
        assert stage1.main() == 2
    golden.assert_not_called()


def test_formal_unresolved_requires_bound_carryover_manifest(tmp_path):
    contract_path, _ = _write_contract(tmp_path, mode="unresolved")
    argv = [
        "stage1",
        "--batch-id", "B-FORMAL",
        "--round-contract", str(contract_path),
        "--golden-seeds-out", str(tmp_path / "golden.json"),
    ]
    with mock.patch.object(sys, "argv", argv), \
            mock.patch.object(stage1.cc, "golden_seeds") as golden:
        assert stage1.main() == 2
    golden.assert_not_called()
