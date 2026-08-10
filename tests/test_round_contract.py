from __future__ import annotations

import copy
import json

import pytest

from extensions.sop_v2 import round_contract as rc


def _build():
    return rc.build_round_contract(
        batch_id="SKIN5-20260810",
        campaign_track="paid",
        carryover_mode="new_only",
        created_at="2026-08-10T12:00:00+0800",
    )


def test_contract_freezes_current_business_rules_and_hashes():
    contract = _build()
    assert contract["carryover_mode"] == "new_only"
    assert contract["storefront"]["definition"] == "generic_ecommerce"
    assert contract["storefront"]["amazon_required"] is False
    assert contract["pricing_estimate"] == {
        "metric": "mean_recent_non_pinned_reels_views",
        "reels_window": 10,
        "exclude_pinned": True,
        "default_cpm_usd": 35.0,
        "cpm_min_usd": 35.0,
        "cpm_max_usd": 40.0,
        "short_window_requires_reels_tab_exhaustion": True,
        "zero_reels_status": "not_applicable_no_reels",
        "third_party_fallback_allowed": False,
    }
    assert contract["comment_translation"]["all_source_languages"] is True
    assert contract["feedback_governance"]["rejection_reason_optional"] is True
    assert contract["feedback_governance"]["empty_reason_is_strategy_signal"] is False
    assert len(contract["config"]["sha256"]) == 64
    assert len(contract["code"]["sha256"]) == 64


def test_contract_source_quotas_must_sum_to_100():
    contract = _build()
    contract["sources"]["quota_pct"]["exploration"] = 19
    with pytest.raises(rc.RoundContractError, match="合计 100"):
        rc.validate_round_contract(contract)


@pytest.mark.parametrize("mode", ["new_only", "unresolved", "retry_only"])
def test_supported_carryover_modes(mode):
    contract = _build()
    contract["carryover_mode"] = mode
    assert rc.validate_round_contract(contract)["carryover_mode"] == mode


def test_contract_refuses_unsafe_policy_drift():
    contract = _build()
    for path, value, message in (
        (("comment_translation", "all_source_languages"), False, "all_source_languages"),
        (("feedback_governance", "rejection_reason_optional"), False, "拒绝原因"),
        (
            ("feedback_governance", "policy_signal_requires_internal_confirmation"),
            False,
            "内部确认",
        ),
        (("feedback_governance", "automatic_hard_gate_changes"), True, "自动修改硬门槛"),
    ):
        changed = copy.deepcopy(contract)
        changed[path[0]][path[1]] = value
        with pytest.raises(rc.RoundContractError, match=message):
            rc.validate_round_contract(changed)


def test_frozen_contract_cannot_be_silently_overwritten(tmp_path):
    contract = _build()
    path = tmp_path / "round_contract.json"
    assert rc.write_round_contract(path, contract) == path
    assert rc.write_round_contract(path, contract) == path
    loaded = rc.load_round_contract(path)
    assert loaded == json.loads(path.read_text(encoding="utf-8"))

    changed = copy.deepcopy(contract)
    changed["campaign_track"] = "gifting"
    with pytest.raises(rc.RoundContractError, match="拒绝覆盖"):
        rc.write_round_contract(path, changed)


def test_runtime_contract_rejects_batch_or_code_drift():
    contract = _build()
    assert rc.assert_contract_matches_runtime(
        contract, batch_id="SKIN5-20260810", campaign_track="paid"
    )["batch_id"] == "SKIN5-20260810"
    with pytest.raises(rc.RoundContractError, match="batch_id"):
        rc.assert_contract_matches_runtime(
            contract, batch_id="WRONG", campaign_track="paid"
        )
    changed = copy.deepcopy(contract)
    changed["code"]["sha256"] = "0" * 64
    with pytest.raises(rc.RoundContractError, match="code SHA"):
        rc.assert_contract_matches_runtime(
            changed, batch_id="SKIN5-20260810", campaign_track="paid"
        )


def _carryover_manifest(mode, rows, *, mode_excluded=0):
    retry = [row["handle"] for row in rows if row["needs_pipeline_retry"]]
    counts = {
        "source_candidates": 1 + len(rows) + mode_excluded,
        "client_final": 1,
        "carryover": len(rows),
        "pipeline_retry": len(retry),
    }
    if mode_excluded:
        counts["mode_excluded"] = mode_excluded
    return {
        "next_batch_id": "SKIN5-20260810",
        "carryover_mode": mode,
        "round_contract_sha256": "a" * 64,
        "counts": counts,
        "retry_handles": retry,
        "carryover": rows,
    }


def test_new_only_rejects_nonempty_carryover():
    contract = _build()
    manifest = _carryover_manifest(
        "new_only",
        [{"handle": "old", "needs_pipeline_retry": False}],
    )
    with pytest.raises(rc.RoundContractError, match="new_only"):
        rc.validate_carryover_for_contract(
            contract, manifest, contract_sha256="a" * 64
        )


def test_retry_only_requires_every_selected_row_to_need_retry():
    contract = _build()
    contract["carryover_mode"] = "retry_only"
    manifest = _carryover_manifest(
        "retry_only",
        [{"handle": "manual_only", "needs_pipeline_retry": False}],
        mode_excluded=1,
    )
    with pytest.raises(rc.RoundContractError, match="retry_only"):
        rc.validate_carryover_for_contract(
            contract, manifest, contract_sha256="a" * 64
        )


def test_unresolved_requires_zero_mode_exclusions_and_contract_binding():
    contract = _build()
    contract["carryover_mode"] = "unresolved"
    manifest = _carryover_manifest("unresolved", [], mode_excluded=1)
    with pytest.raises(rc.RoundContractError, match="不能遗漏"):
        rc.validate_carryover_for_contract(
            contract, manifest, contract_sha256="a" * 64
        )
    manifest["counts"].pop("mode_excluded")
    manifest["counts"]["source_candidates"] = 1
    with pytest.raises(rc.RoundContractError, match="绑定"):
        rc.validate_carryover_for_contract(
            contract, manifest, contract_sha256="b" * 64
        )
