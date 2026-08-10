from __future__ import annotations

import pytest

from extensions.sop_v2 import feedback_taxonomy as ft


def test_taxonomy_codes_are_unique_and_payload_is_versioned():
    codes = [item.code for item in ft.REASON_DEFINITIONS]
    assert len(codes) == len(set(codes))
    assert set(codes) == set(ft.REASON_TAGS)
    payload = ft.taxonomy_payload()
    assert payload["taxonomy_version"] == ft.TAXONOMY_VERSION
    assert payload["reasons"][0]["code"] == codes[0]


def test_reason_tags_are_validated_deduplicated_and_ordered():
    assert ft.normalize_reason_tags(
        ["quality_video_low", "geo_mismatch", "quality_video_low"]
    ) == ["quality_video_low", "geo_mismatch"]
    with pytest.raises(ft.FeedbackTaxonomyError, match="未知 reason_tag"):
        ft.normalize_reason_tags(["invented_reason"])


def test_rejection_reason_tags_are_optional():
    assert ft.normalize_reason_tags(None) == []
    assert ft.normalize_reason_tags([]) == []


@pytest.mark.parametrize(
    ("normalizer", "default", "bad"),
    [
        (ft.normalize_feedback_scope, "account", "auto_global"),
        (ft.normalize_rejection_scope, "campaign", "forever"),
        (ft.normalize_evidence_status, "unverified", "trusted_by_llm"),
    ],
)
def test_scoped_fields_default_safely_and_reject_unknown(normalizer, default, bad):
    assert normalizer(None) == default
    with pytest.raises(ft.FeedbackTaxonomyError):
        normalizer(bad)


def test_customer_policy_signal_is_not_confirmed_policy():
    assert ft.normalize_feedback_scope("policy_signal") == "policy_signal"
    assert "confirmed_policy" in ft.FEEDBACK_SCOPES
    assert "policy_signal" != "confirmed_policy"
