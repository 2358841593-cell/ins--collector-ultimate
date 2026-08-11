"""Profile-credit shortlist must contain only route-actionable candidates."""
from __future__ import annotations

import pytest

from extensions.sop_v2.config import load_config
from extensions.sop_v2.pipeline import stage4_decide

from _fixtures import clean_full


CFG = load_config()


def _missing(handle: str, **overrides):
    value = clean_full(
        handle=handle,
        fake_pct=None,
        creator_country=None,
        top_audience_country=None,
    )
    value.update(overrides)
    return value


@pytest.mark.parametrize(
    "overrides",
    [
        {"valid_comments": 10},
        {"storefront_status": "unknown"},
        {"real_er": 0.2, "real_er_median": 0.2},
        {"sponsorship_saturation": 35.0},
        {"core_niche_key": "lifestyle", "lifestyle_promoted": False},
        {"brand_account_type": "medical"},
        {"discovered_via": "graph", "fully_audited": False},
    ],
)
def test_non_modash_review_blocker_is_not_credit_actionable(overrides):
    assert not stage4_decide._modash_enrichment_actionable(  # noqa: SLF001
        _missing("blocked", **overrides), CFG
    )


def test_missing_language_and_audience_fields_are_modash_actionable():
    candidate = clean_full(
        handle="actionable",
        top_language_pct=None,
        target_countries_audience_pct=None,
    )

    assert stage4_decide._modash_enrichment_actionable(  # noqa: SLF001
        candidate, CFG
    )


def test_missing_general_er_can_be_actionable_when_real_er_is_missing():
    candidate = clean_full(
        handle="general_er_gap",
        real_er=None,
        real_er_median=None,
        general_er=None,
    )

    selected, stats = stage4_decide._build_modash_shortlist(  # noqa: SLF001
        [candidate], CFG, cap=20
    )

    assert [row["handle"] for row in selected] == ["general_er_gap"]
    assert stats["missing_core"] == 0
    assert stats["missing_report_fields"] == 1


def test_gifting_priority_can_improve_from_review_to_priority_review():
    candidate = _missing(
        "gifting_priority",
        campaign_track="gifting",
        follower_count=40_000,
    )

    assert stage4_decide._modash_route_projection(  # noqa: SLF001
        candidate, CFG
    ) == ("Review", "Priority-Review")
    assert stage4_decide._modash_enrichment_actionable(  # noqa: SLF001
        candidate, CFG
    )


@pytest.mark.parametrize(
    "observed",
    [
        {"fake_pct": 30.0},
        {"creator_country": "RU", "top_audience_country": "RU"},
        {"creator_country": "US", "top_audience_country": "CA"},
        {"real_er": None, "real_er_median": None, "general_er": 0.0},
    ],
)
def test_optimistic_projection_never_overwrites_observed_modash_failures(
    observed,
):
    candidate = clean_full(
        handle="observed_failure",
        top_language_pct=None,
        **observed,
    )

    current_pool, optimistic_pool = stage4_decide._modash_route_projection(  # noqa: SLF001
        candidate, CFG
    )

    assert optimistic_pool == current_pool
    assert not stage4_decide._modash_enrichment_actionable(  # noqa: SLF001
        candidate, CFG
    )


def test_shortlist_filters_before_budget_cap_and_preserves_priority_order():
    candidates = [
        _missing("low_er", real_er=1.1, storefront_status="confirmed_yes"),
        _missing("high_er", real_er=3.0, storefront_status="confirmed_yes"),
        _missing("no_store", real_er=9.0, storefront_status="confirmed_no"),
        _missing("blocked_comments", valid_comments=2, real_er=99.0),
        clean_full(handle="already_complete"),
    ]

    selected, stats = stage4_decide._build_modash_shortlist(  # noqa: SLF001
        candidates, CFG, cap=2
    )

    assert [candidate["handle"] for candidate in selected] == [
        "high_er",
        "low_er",
    ]
    assert stats == {
        "active": 5,
        "missing_core": 4,
        "missing_report_fields": 4,
        "actionable": 3,
        "non_actionable_skipped": 1,
        "selected": 2,
    }


def test_explicit_zero_cap_keeps_every_actionable_candidate():
    candidates = [
        _missing(f"candidate_{index}", real_er=1.0 + index / 100)
        for index in range(25)
    ]

    selected, stats = stage4_decide._build_modash_shortlist(  # noqa: SLF001
        candidates, CFG, cap=0
    )

    assert len(selected) == 25
    assert stats["selected"] == 25
    assert stats["non_actionable_skipped"] == 0


def test_all_missing_mode_skips_existing_reports_and_zero_cap_is_unlimited():
    missing = [
        clean_full(handle=f"missing_{index}", modash_report=False)
        for index in range(25)
    ]
    report_with_source_gaps = clean_full(
        handle="report_present",
        modash_report=True,
        fake_pct=None,
        creator_country=None,
        top_audience_country=None,
    )

    selected, stats = (
        stage4_decide._build_modash_all_missing_shortlist(  # noqa: SLF001
            [*missing, report_with_source_gaps], cap=0
        )
    )

    assert [candidate["handle"] for candidate in selected] == [
        f"missing_{index}" for index in range(25)
    ]
    assert stats == {
        "candidate_count": 26,
        "report_present": 1,
        "report_missing": 25,
        "selected": 25,
    }
