"""Customer delivery models exclude internal Stage 3 retry state."""
from __future__ import annotations

import sys
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from extensions.sop_v2 import run_v2  # noqa: E402
from extensions.sop_v2.pipeline import deep_attempts  # noqa: E402


def test_delivery_candidate_strips_attempt_and_retry_metadata():
    source = {
        "handle": "creator",
        "_status": "collected",
        "_stage_error": "internal error",
        "_reject_reason": "internal reject",
        "_discovery_batch": "ROUND-1",
        "_future_runtime_state": {"internal": True},
        "sampled_posts": [{"code": "/p/KEEP/"}],
        "deep_collection_attempts": [{"error": "internal"}],
        "deep_canonical_attempt_id": "attempt-1",
        "deep_canonical_quality": {"sampled_posts": 10},
        "pricing_canonical_attempt_id": "attempt-2",
        "pricing_canonical_quality": {"pricing_rank": 4},
        "stage3_comment_retry_state": [{"identity": "MEDIA"}],
        "stage3_comment_retry_history": [{"identity": "LEGACY"}],
        "deep_evidence_merge_provenance": {"internal": True},
        "evidence_dir": "data/evidence/ROUND-1/creator",
    }

    delivered = run_v2._delivery_candidate(source)  # noqa: SLF001

    assert delivered == {
        "handle": "creator",
        "discovery_batch": "ROUND-1",
        "sampled_posts": [{"code": "/p/KEEP/"}],
    }
    assert source["deep_collection_attempts"] == [{"error": "internal"}]


def test_delivery_denylist_covers_every_stage3_reset_field():
    assert set(deep_attempts.STAGE3_RESET_FIELDS) <= run_v2._INTERNAL_RUNTIME_FIELDS  # noqa: SLF001


def test_delivery_denylist_covers_creator_cache_runtime_fields():
    assert {
        "_status",
        "_stage_error",
        "_reject_reason",
        "_discovery_batch",
        "_queue_lock_token",
        "_queue_from_status",
        "_cache_hit",
        "evidence_dir",
    } <= run_v2._INTERNAL_RUNTIME_FIELDS  # noqa: SLF001


def test_rejected_delivery_keeps_public_reason_without_runtime_fields():
    delivered = run_v2.decide_rejected(
        {
            "handle": "creator",
            "_status": "rejected",
            "_stage_error": None,
            "_reject_reason": "off_niche",
            "_discovery_batch": "ROUND-1",
        }
    )

    assert delivered["exclude_reasons"] == ["off_niche"]
    assert delivered["discovery_batch"] == "ROUND-1"
    assert not any(key.startswith("_") for key in delivered)
