from __future__ import annotations

import copy
import json

import pytest

from extensions.sop_v2.config import load_config
from extensions.sop_v2.pipeline.replay_policy import (
    PolicyReplayError,
    _feedback_map,
    replay_decisions,
)
from _fixtures import clean_full


def _source(candidate):
    return {
        "manifest": {"batch_id": "B1", "campaign_track": "paid"},
        "candidates": [candidate],
    }


def test_replay_detects_approved_account_newly_excluded():
    cfg = copy.deepcopy(load_config())
    candidate = clean_full(handle="approved_one")
    # Baseline is the frozen formal-delivery result, not recomputed implicitly.
    candidate["final_pool"] = "Include-With-Storefront"
    cfg["track"]["paid"]["max_followers"] = 40_000

    report = replay_decisions(
        _source(candidate), cfg, client_verdicts={"approved_one": "approved"}
    )

    assert report["approved_to_exclude"] == ["approved_one"]
    assert report["safe_to_promote_hard_gate"] is False
    assert report["changed"][0]["new_pool"] == "Exclude"


def test_replay_does_not_invent_feedback_for_unlabeled_rows():
    cfg = copy.deepcopy(load_config())
    candidate = clean_full(handle="unreviewed")
    candidate["final_pool"] = "Include-With-Storefront"

    report = replay_decisions(_source(candidate), cfg)

    assert report["labeled_count"] == 0
    assert report["approved_to_exclude"] == []
    assert report["safe_to_promote_hard_gate"] is True


def test_machine_rejection_without_pre_gate_snapshot_is_not_replayable():
    cfg = copy.deepcopy(load_config())
    candidate = clean_full(handle="machine_rejected")
    candidate.update({"_reject_reason": "brand_account", "final_pool": "Exclude"})

    report = replay_decisions(
        _source(candidate), cfg, client_verdicts={"machine_rejected": "rejected"}
    )

    assert report["new_pool_distribution"] == {"Not-Replayable": 1}
    assert report["changed_count"] == 0
    assert report["not_replayable_count"] == 1
    assert report["replay_complete"] is False
    assert report["safe_to_promote_hard_gate"] is False


def test_approved_account_downgraded_to_review_is_not_safe():
    cfg = copy.deepcopy(load_config())
    candidate = clean_full(handle="approved_review", valid_comments=0)
    candidate["final_pool"] = "Include-With-Storefront"

    report = replay_decisions(
        _source(candidate), cfg, client_verdicts={"approved_review": "approved"}
    )

    assert report["approved_to_exclude"] == []
    assert report["approved_to_noninclude"] == ["approved_review"]
    assert report["no_approved_exclusions"] is True
    assert report["no_approved_downgrades"] is False
    assert report["safe_to_promote_hard_gate"] is False


def test_replay_rejects_labels_outside_frozen_candidates():
    cfg = copy.deepcopy(load_config())
    candidate = clean_full(handle="inside")
    candidate["final_pool"] = "Include-With-Storefront"

    with pytest.raises(PolicyReplayError, match="decisions 外账号"):
        replay_decisions(
            _source(candidate), cfg, client_verdicts={"outside": "approved"}
        )


def test_feedback_map_requires_and_validates_matching_frozen_decisions(tmp_path):
    source = tmp_path / "decisions.json"
    feedback = tmp_path / "feedback.json"
    candidate = clean_full(handle="same_handle")
    candidate.update(
        {
            "final_pool": "Include-With-Storefront",
            "_discovery_batch": "B1",
        }
    )
    source.write_text(json.dumps(_source(candidate)), encoding="utf-8")
    feedback.write_text(
        json.dumps(
            {
                "batch": "WRONG-BATCH",
                "exported_at": "2026-08-10T12:00:00+08:00",
                "decisions": [
                    {"handle": "same_handle", "verdict": "approved", "reason": ""}
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="批次不一致"):
        _feedback_map(feedback, source_decisions_path=source)

    with pytest.raises(PolicyReplayError, match="必须同时提供"):
        _feedback_map(feedback)
