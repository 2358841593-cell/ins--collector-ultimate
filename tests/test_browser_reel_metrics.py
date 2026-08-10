"""Reel 报价采集的媒体 ID、播放量来源和置顶语义回归测试。"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import browser_collect_v2 as browser  # noqa: E402


def test_shortcode_converts_to_exact_instagram_media_id():
    assert browser._shortcode_to_media_id("DbQlLVNRV3K") == 3949820379610308042
    assert (
        browser._shortcode_to_media_id(
            "https://www.instagram.com/reel/DbQlLVNRV3K/"
        )
        == 3949820379610308042
    )


@pytest.mark.parametrize(
    ("payload", "expected_count", "expected_source"),
    [
        (
            {
                "http_status": 200,
                "ig_play_count": 12_345,
                # play_count 可能混入 Facebook 转发，不能覆盖 IG 自身值。
                "total_play_count": 987_654,
                "fb_play_count": 975_309,
            },
            12_345,
            "ig_media_info.ig_play_count",
        ),
        (
            {
                "http_status": 200,
                "ig_play_count": None,
                "total_play_count": 54_321,
                "fb_play_count": 40_000,
            },
            None,
            None,
        ),
        (
            {
                "http_status": 200,
                # 零是 Instagram 返回的有效观测，不能用 total 覆盖。
                "ig_play_count": 0,
                "total_play_count": 7_777,
                "fb_play_count": 7_777,
            },
            0,
            "ig_media_info.ig_play_count",
        ),
        (
            {
                "http_status": 200,
                "ig_play_count": None,
                # total=0 仍只作审计，不能冒充 IG 原生播放。
                "total_play_count": 0,
                "fb_play_count": 0,
            },
            None,
            None,
        ),
    ],
)
def test_media_metric_source_priority_and_zero_semantics(
    payload, expected_count, expected_source
):
    result = browser._normalize_media_metric(payload)

    assert result["play_count"] == expected_count
    assert result["play_count_status"] == (
        "observed" if expected_count is not None else "ig_not_exposed"
    )
    assert result["play_count_source"] == expected_source


def test_media_info_normalizes_engagement_counts_for_deep_fallback():
    result = browser._normalize_media_metric(
        {
            "http_status": 200,
            "ig_play_count": None,
            "like_count": 321,
            "comment_count": 0,
        }
    )

    assert result["like_count"] == 321
    assert result["comment_count"] == 0


@pytest.mark.parametrize(
    ("payload", "expected_status"),
    [
        ({"http_status": 403}, "api_http_403"),
        ({"http_status": 429, "ig_play_count": 99}, "api_http_429"),
        ({"fetch_error": "TypeError"}, "api_TypeError"),
        ({}, "api_failed"),
    ],
)
def test_http_and_fetch_errors_never_manufacture_zero(payload, expected_status):
    result = browser._normalize_media_metric(payload)

    assert result["play_count"] is None
    assert result["play_count_status"] == expected_status
    assert result["play_count_source"] is None
    assert result["pinned"] is None


@pytest.mark.parametrize("pinned", [True, False, None])
def test_pinned_three_state_is_preserved(pinned):
    result = browser._normalize_media_metric(
        {
            "http_status": 200,
            "ig_play_count": 10,
            "pinned": pinned,
            "pinned_source": (
                "ig_media_info_pin_lists" if pinned is not None else None
            ),
        }
    )

    assert result["pinned"] is pinned
    assert result["pinned_source"] == (
        "ig_media_info_pin_lists" if pinned is not None else None
    )


class _MediaInfoPage:
    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error
        self.calls = []

    def evaluate(self, script, arg):
        self.calls.append((script, arg))
        if self.error:
            raise self.error
        return self.payload


def test_fetch_reel_metric_passes_decimal_media_id_to_same_origin_script():
    page = _MediaInfoPage(
        {
            "http_status": 200,
            "code": "DbQlLVNRV3K",
            "ig_play_count": 444,
            "total_play_count": 999,
        }
    )

    result = browser._fetch_reel_metric(
        page, "/reel/DbQlLVNRV3K/"
    )

    assert page.calls == [
        (browser._IG_MEDIA_INFO_JS, "3949820379610308042")
    ]
    assert result["play_count"] == 444
    assert result["play_count_source"] == "ig_media_info.ig_play_count"


def test_collaboration_token_uses_page_og_shortcode_and_response_proof():
    long_code = "Dbsxxpdx8a1AKZQkBUvtz0VaOdErIRRX9Qw1SI0"
    page = _MediaInfoPage(
        {
            "http_status": 200,
            "code": long_code,
            "like_count": 3,
            "comment_count": 23,
        }
    )

    result = browser._fetch_reel_metric(  # noqa: SLF001
        page,
        f"/_thebmethod/reel/{long_code}/",
        canonical_url=(
            "https://www.instagram.com/_thebmethod/reel/Dbsxxpdx8a1/"
        ),
        canonical_source="og:url",
    )

    assert page.calls == [
        (browser._IG_MEDIA_INFO_JS, "3957757088608274101")
    ]
    assert result["like_count"] == 3
    assert result["comment_count"] == 23
    assert result["media_identity_provenance"] == {
        "requested_shortcode": "Dbsxxpdx8a1",
        "original_shortcode": long_code,
        "canonical_shortcode": "Dbsxxpdx8a1",
        "requested_shortcode_source": "og:url",
        "page_canonical_url": (
            "https://www.instagram.com/_thebmethod/reel/Dbsxxpdx8a1/"
        ),
        "response_code": long_code,
        "identity_verified": True,
    }


@pytest.mark.parametrize(
    "canonical_url",
    [
        "https://evil.example/_thebmethod/reel/Dbsxxpdx8a1/",
        "http://www.instagram.com/_thebmethod/reel/Dbsxxpdx8a1/",
        "https://www.instagram.com/_thebmethod/p/Dbsxxpdx8a1/",
        "https://www.instagram.com/_thebmethod/reel/UNRELATED/",
    ],
)
def test_invalid_page_canonical_never_truncates_collaboration_token(
    canonical_url,
):
    long_code = "Dbsxxpdx8a1AKZQkBUvtz0VaOdErIRRX9Qw1SI0"
    page = _MediaInfoPage({"http_status": 400})

    result = browser._fetch_reel_metric(  # noqa: SLF001
        page,
        f"/_thebmethod/reel/{long_code}/",
        canonical_url=canonical_url,
    )

    assert page.calls == [
        (
            browser._IG_MEDIA_INFO_JS,
            str(browser._shortcode_to_media_id(long_code)),
        )
    ]
    assert result["play_count_status"] == "api_http_400"


def test_canonical_media_response_identity_mismatch_fails_closed():
    long_code = "Dbsxxpdx8a1AKZQkBUvtz0VaOdErIRRX9Qw1SI0"
    page = _MediaInfoPage(
        {
            "http_status": 200,
            "code": "DIFFERENT",
            "like_count": 999,
            "comment_count": 999,
        }
    )

    result = browser._fetch_reel_metric(  # noqa: SLF001
        page,
        f"/_thebmethod/reel/{long_code}/",
        canonical_url=(
            "https://www.instagram.com/_thebmethod/reel/Dbsxxpdx8a1/"
        ),
    )

    assert result.get("like_count") is None
    assert result.get("comment_count") is None
    assert result["play_count_status"] == "api_identity_mismatch"


class _CanonicalDeepPage:
    def wait_for_timeout(self, _milliseconds):
        return None

    def evaluate(self, script, _arg=None):
        if script == browser._DEEP_READ_JS:
            return {
                "caption": "collaboration post without exposed metrics",
                "video": True,
                "taken_at": None,
                "text": "",
                "canonical_url": (
                    "https://www.instagram.com/_thebmethod/reel/"
                    "Dbsxxpdx8a1/"
                ),
                "canonical_source": "og:url",
            }
        raise AssertionError("unexpected page evaluation")


def test_deep_canonical_refresh_restores_latest_collaboration_pricing_row(
    tmp_path, monkeypatch
):
    long_code = "Dbsxxpdx8a1AKZQkBUvtz0VaOdErIRRX9Qw1SI0"
    href = f"/_thebmethod/reel/{long_code}/"
    pricing_sample = {
        "code": href,
        "url": f"https://www.instagram.com{href}",
        "is_video": True,
        "is_reel": True,
        "pinned": None,
        "pinned_source": None,
        "grid_rank": 0,
        "captured_at": "2026-08-11T10:00:00",
        "play_count": None,
        "play_count_status": "api_http_400",
        "play_count_source": None,
    }
    monkeypatch.setattr(browser, "ROOT", tmp_path)
    monkeypatch.setattr(
        browser,
        "_collect_profile_grid_refs",
        lambda *_args, **_kwargs: ([{"url": href, "pinned": None}], None),
    )

    def fake_pricing(_page, _candidate):
        return (
            {
                "refs": [{"url": href, "pinned": None}],
                "codes": [href],
                "pricing_reels": [pricing_sample],
                "metric_by_href": {href: pricing_sample},
                "sample_by_href": {href: pricing_sample},
            },
            None,
        )

    monkeypatch.setattr(browser, "collect_pricing_evidence", fake_pricing)
    monkeypatch.setattr(browser, "_goto", lambda *_args: True)
    monkeypatch.setattr(browser, "_pause", lambda *_args: None)
    monkeypatch.setattr(browser, "_cache_save", lambda *_args: None)

    def canonical_metric(_page, metric_href, **kwargs):
        assert metric_href == href
        assert kwargs == {
            "canonical_url": (
                "https://www.instagram.com/_thebmethod/reel/"
                "Dbsxxpdx8a1/"
            ),
            "canonical_source": "og:url",
        }
        return {
            "play_count": 12_345,
            "play_count_status": "observed",
            "play_count_source": "ig_media_info.ig_play_count",
            "play_count_raw": "ig=12345;total=12345;fb=0",
            "ig_play_count": 12_345,
            "total_play_count": 12_345,
            "fb_play_count": 0,
            "like_count": 3,
            "comment_count": 0,
            "pinned": False,
            "pinned_source": "ig_media_info_pin_lists",
            "taken_at": 1_786_000_000,
            "like_and_view_counts_disabled": True,
            "media_identity_provenance": {
                "requested_shortcode": "Dbsxxpdx8a1",
                "requested_shortcode_source": "og:url",
                "page_canonical_url": (
                    "https://www.instagram.com/_thebmethod/reel/"
                    "Dbsxxpdx8a1/"
                ),
                "response_code": long_code,
                "identity_verified": True,
            },
        }

    monkeypatch.setattr(browser, "_fetch_reel_metric", canonical_metric)

    result, _evidence = browser.deep_collect(
        _CanonicalDeepPage(),
        {"handle": "diana_wellnessroute"},
        tmp_path / "data/evidence/BATCH/diana_wellnessroute",
        n_posts=1,
    )

    refreshed = result["pricing_reel_samples"][0]
    assert refreshed["code"] == href
    assert refreshed["play_count"] == 12_345
    assert refreshed["play_count_source"] == (
        "ig_media_info.ig_play_count"
    )
    assert refreshed["pinned"] is False
    assert refreshed["taken_at"] == 1_786_000_000
    assert refreshed["media_identity_provenance"]["identity_verified"] is True
    assert result["pricing_estimate"]["sample_count"] == 1
    assert result["pricing_estimate"]["reels"][0]["play_count"] == 12_345


def test_pricing_alias_refresh_rejects_unverified_response_bundle():
    sample = {
        "play_count": None,
        "play_count_status": "api_http_400",
        "pinned": None,
    }
    before = dict(sample)

    refreshed = browser._refresh_pricing_sample_from_verified_alias(  # noqa: SLF001
        sample,
        {
            "play_count": 999_999,
            "play_count_status": "observed",
            "pinned": False,
            "media_identity_provenance": {
                "page_canonical_url": "https://www.instagram.com/reel/SHORT/",
                "identity_verified": False,
            },
        },
    )

    assert refreshed is False
    assert sample == before


@pytest.mark.parametrize(
    "sample",
    [
        {
            "code": "/owner/reel/Dbsxxpdx8a1/",
            "play_count": None,
            "play_count_status": "api_http_400",
            "pinned": None,
        },
        {
            "code": (
                "/_thebmethod/reel/"
                "Dbsxxpdx8a1AKZQkBUvtz0VaOdErIRRX9Qw1SI0/"
            ),
            "play_count": 111,
            "play_count_status": "observed",
            "pinned": False,
        },
    ],
)
def test_verified_alias_never_overwrites_standard_or_observed_pricing_sample(
    sample,
):
    before = dict(sample)
    fresh = {
        "play_count": 999,
        "play_count_status": "observed",
        "play_count_source": "ig_media_info.ig_play_count",
        "pinned": False,
        "media_identity_provenance": {
            "requested_shortcode": "Dbsxxpdx8a1",
            "requested_shortcode_source": "og:url",
            "page_canonical_url": (
                "https://www.instagram.com/_thebmethod/reel/"
                "Dbsxxpdx8a1/"
            ),
            "response_code": (
                "Dbsxxpdx8a1AKZQkBUvtz0VaOdErIRRX9Qw1SI0"
            ),
            "identity_verified": True,
        },
    }

    assert browser._refresh_pricing_sample_from_verified_alias(  # noqa: SLF001
        sample, fresh
    ) is False
    assert sample == before


def test_fetch_reel_metric_exception_stays_missing_instead_of_becoming_zero():
    page = _MediaInfoPage(error=TimeoutError("media info timeout"))

    result = browser._fetch_reel_metric(page, "/reel/DbQlLVNRV3K/")

    assert result["play_count"] is None
    assert result["play_count_status"] == "api_TimeoutError"
    assert result["play_count_source"] is None


def _is_pinned_function(js_source: str) -> str:
    """只抽 isPinned 函数体，避免脚本其他 DOM 逻辑影响断言。"""
    match = re.search(
        r"const isPinned=\(a\)=>\{(?P<body>.*?)\n\s*\};",
        js_source,
        flags=re.DOTALL,
    )
    assert match, "isPinned function is missing"
    return match.group("body")


@pytest.mark.parametrize("js_source", [browser._PROFILE_JS, browser._GRID_JS])
def test_pin_detection_is_scoped_to_each_anchor_card(js_source):
    body = _is_pinned_function(js_source)

    # 只能读取当前卡片 a 自身和其后代；向父级/row/grid 爬会让一个置顶图标污染整排。
    assert "a.querySelectorAll" in body
    assert "parentElement" not in body
    assert ".parentNode" not in body
    assert ".closest(" not in body


def test_media_info_script_preserves_zero_before_python_normalization():
    # JavaScript 的 ?? 不会像 || 那样把 0 当成缺失值。
    assert "ig_play_count:m.ig_play_count??null" in browser._IG_MEDIA_INFO_JS
    assert "total_play_count:m.play_count??null" in browser._IG_MEDIA_INFO_JS
    assert "fb_play_count:m.fb_play_count??null" in browser._IG_MEDIA_INFO_JS
    assert "like_count:m.like_count??null" in browser._IG_MEDIA_INFO_JS
    assert "comment_count:m.comment_count??null" in browser._IG_MEDIA_INFO_JS


class _GridMouse:
    def wheel(self, _x, _y):
        raise AssertionError("enough refs were returned; scrolling is unexpected")


class _GridPage:
    def __init__(self, refs):
        self.refs = refs
        self.mouse = _GridMouse()

    def wait_for_timeout(self, _ms):
        return None

    def evaluate(self, script, *_args):
        if script == browser._GRID_JS:
            return {
                "logged_out": False,
                "challenge": False,
                "private_account": False,
                "error_page": False,
                "page_identity_verified": True,
                "reels_tab_route_verified": True,
                "page_healthy": True,
                "profile_healthy": True,
                "redirected_to_profile": False,
                "reels_tab_link_present": True,
                "reel_links_seen": sum(
                    "/reel/" in row["url"] for row in self.refs
                ),
                "visible_feed_posts": sum(
                    "/p/" in row["url"] for row in self.refs
                ),
                "post_refs": self.refs,
            }
        assert script == browser._REELS_SCROLL_STATE_JS
        return {
            "scroll_top": 100,
            "viewport_height": 100,
            "scroll_height": 200,
            "at_bottom": True,
            "loading_visible": False,
        }


def test_pricing_grid_uses_reels_tab_not_profile_main_grid(monkeypatch):
    visited = []
    monkeypatch.setattr(
        browser,
        "_goto",
        lambda _page, url: visited.append(url) or True,
    )
    refs = [
        {"url": f"/owner/reel/R{i}/", "pinned": None}
        for i in range(13)
    ]

    result, error, evidence = browser._collect_grid_refs(  # noqa: SLF001
        _GridPage(refs), "owner", min_non_pinned_reels=13
    )

    assert error is None
    assert len(result) == 13
    assert evidence["status"] == "target_reached"
    assert evidence["page_identity_verified"] is True
    assert visited == ["https://www.instagram.com/owner/reels/"]


class _ExhaustionMouse:
    def __init__(self, page):
        self.page = page

    def wheel(self, _x, _y):
        self.page.scrolls += 1


class _ExhaustionPage:
    def __init__(self, refs, *, empty_marker=None, **health):
        self.refs = refs
        self.empty_marker = empty_marker
        self.health = health
        self.scrolls = 0
        self.mouse = _ExhaustionMouse(self)

    def wait_for_timeout(self, _ms):
        return None

    def evaluate(self, script, *_args):
        if script == browser._GRID_JS:
            result = {
                "logged_out": False,
                "challenge": False,
                "private_account": False,
                "error_page": False,
                "page_identity_verified": True,
                "reels_tab_route_verified": True,
                "page_healthy": True,
                "post_refs": self.refs,
                "empty_state_verified": self.empty_marker is not None,
                "empty_reels_marker": self.empty_marker,
                "observed_handle": "owner",
                "final_pathname": "/owner/reels/",
            }
            result.update(self.health)
            return result
        assert script == browser._REELS_SCROLL_STATE_JS
        return {
            "scroll_top": 900,
            "viewport_height": 100,
            "scroll_height": 1_000,
            "at_bottom": True,
            "loading_visible": False,
        }


def test_grid_exhaustion_requires_two_real_bottom_no_growth_scrolls(monkeypatch):
    monkeypatch.setattr(browser, "_goto", lambda *_args: True)
    refs = [{"url": f"/owner/reel/R{i}/", "pinned": None} for i in range(5)]
    page = _ExhaustionPage(refs)

    result, error, evidence = browser._collect_grid_refs(  # noqa: SLF001
        page, "owner", min_non_pinned_reels=13
    )

    assert error is None
    assert len(result) == 5
    assert page.scrolls == 2
    assert evidence["status"] == "exhausted"
    assert evidence["reason"] == "stable_bottom_no_growth"
    assert evidence["stable_bottom_rounds"] == 2
    assert evidence["scroll_attempts"] == 2
    assert evidence["page_healthy"] is True


def test_empty_grid_records_explicit_healthy_empty_marker(monkeypatch):
    monkeypatch.setattr(browser, "_goto", lambda *_args: True)
    page = _ExhaustionPage([], empty_marker="No Reels Yet")

    refs, error, evidence = browser._collect_grid_refs(  # noqa: SLF001
        page, "owner", min_non_pinned_reels=13
    )

    assert refs == []
    assert error is None
    assert evidence["status"] == "exhausted"
    assert evidence["empty_state_verified"] is True
    assert evidence["empty_reels_marker"] == "No Reels Yet"


def test_healthy_profile_redirect_without_reels_tab_is_strict_no_reels_proof(
    monkeypatch,
):
    navigations = []
    monkeypatch.setattr(
        browser,
        "_goto",
        lambda _page, url: navigations.append(url) or True,
    )
    page = _ExhaustionPage(
        [{"url": f"/owner/p/P{i}/", "pinned": None} for i in range(12)],
        reels_tab_route_verified=False,
        page_healthy=False,
        profile_healthy=True,
        redirected_to_profile=True,
        final_pathname="/owner/",
        reels_tab_link_present=False,
        reel_links_seen=0,
        visible_feed_posts=12,
    )

    refs, error, evidence = browser._collect_grid_refs(  # noqa: SLF001
        page, "owner", min_non_pinned_reels=13
    )

    assert refs == []
    assert error is None
    assert page.scrolls == 0
    assert navigations == [
        "https://www.instagram.com/owner/reels/",
        "https://www.instagram.com/owner/reels/",
    ]
    assert evidence["status"] == "reels_surface_absent"
    assert evidence["profile_probe_rounds"] == 2
    assert evidence["reels_surface_probe_navigations"] == 2
    assert evidence["unique_feed_posts_seen"] == 12
    assert evidence["reels_tab_link_present"] is False
    assert evidence["reel_links_seen"] == 0
    snapshots = evidence["reels_surface_probe_snapshots"]
    assert [row["navigation_index"] for row in snapshots] == [1, 2]
    assert [row["feed_post_count"] for row in snapshots] == [12, 12]
    assert all(row["feed_post_identities"] for row in snapshots)
    assert all(row["feed_post_identity_sha256"] for row in snapshots)


def test_surface_absent_does_not_reuse_first_navigation_feed_posts(
    monkeypatch,
):
    monkeypatch.setattr(browser, "_goto", lambda *_args: True)

    class FirstGoodSecondEmpty(_ExhaustionPage):
        def __init__(self):
            super().__init__(
                [{"url": f"/owner/p/P{i}/", "pinned": None} for i in range(12)],
                reels_tab_route_verified=False,
                page_healthy=False,
                profile_healthy=True,
                redirected_to_profile=True,
                final_pathname="/owner/",
                reels_tab_link_present=False,
                reel_links_seen=0,
                visible_feed_posts=12,
            )
            self.grid_reads = 0

        def evaluate(self, script, *_args):
            if script == browser._GRID_JS:
                self.grid_reads += 1
                if self.grid_reads == 2:
                    self.refs = []
                    self.health["visible_feed_posts"] = 0
            return super().evaluate(script, *_args)

    _, error, evidence = browser._collect_grid_refs(  # noqa: SLF001
        FirstGoodSecondEmpty(), "owner", min_non_pinned_reels=13
    )

    assert error == "profile_redirect_without_no_reels_proof"
    assert evidence["status"] == "failed"
    assert evidence["profile_probe_rounds"] == 0
    assert [
        row["feed_post_count"]
        for row in evidence["reels_surface_probe_snapshots"]
    ] == [12, 0]


def test_profile_redirect_with_reels_tab_link_is_not_no_reels_proof(monkeypatch):
    monkeypatch.setattr(browser, "_goto", lambda *_args: True)
    page = _ExhaustionPage(
        [{"url": "/owner/p/P0/", "pinned": None}],
        reels_tab_route_verified=False,
        page_healthy=False,
        profile_healthy=True,
        redirected_to_profile=True,
        final_pathname="/owner/",
        reels_tab_link_present=True,
        reel_links_seen=0,
        visible_feed_posts=1,
    )

    _, error, evidence = browser._collect_grid_refs(  # noqa: SLF001
        page, "owner", min_non_pinned_reels=13
    )

    assert error == "profile_redirect_without_no_reels_proof"
    assert evidence["status"] == "failed"


@pytest.mark.parametrize(
    ("health", "error"),
    [
        ({"challenge": True, "page_healthy": False}, "challenge"),
        ({"logged_out": True, "page_healthy": False}, "logged_out"),
        ({"private_account": True, "page_healthy": False}, "private_account"),
        ({"error_page": True, "page_healthy": False}, "error_page"),
        (
            {"page_identity_verified": False, "page_healthy": False},
            "profile_identity_mismatch",
        ),
        ({"page_healthy": False}, "reels_tab_unhealthy"),
    ],
)
def test_error_or_wrong_profile_page_never_emits_exhaustion(
    monkeypatch, health, error
):
    monkeypatch.setattr(browser, "_goto", lambda *_args: True)
    page = _ExhaustionPage([], **health)

    _, actual_error, evidence = browser._collect_grid_refs(  # noqa: SLF001
        page, "owner", min_non_pinned_reels=13
    )

    assert actual_error == error
    assert evidence["status"] == "failed"
    assert evidence["reason"] == error


def test_relative_and_absolute_reel_urls_dedupe_to_one_identity():
    merged = browser._merge_post_refs(  # noqa: SLF001
        [{"url": "/owner/reel/ABC123/", "pinned": None}],
        [
            {
                "url": "https://www.instagram.com/reel/ABC123/?utm_source=x",
                "pinned": True,
            }
        ],
    )

    assert merged == [{"url": "/owner/reel/ABC123/", "pinned": None}]


@pytest.mark.parametrize("js_source", [browser._PROFILE_JS, browser._GRID_JS])
def test_dom_pin_absence_stays_unknown(js_source):
    body = _is_pinned_function(js_source)

    assert "? true : null" in body


def test_pricing_collection_does_not_replace_core_post_window(monkeypatch):
    original_codes = ["/p/CORE_POST/"]
    original_refs = [{"url": "/p/CORE_POST/", "pinned": None}]
    cand = {
        "handle": "owner",
        "codes": list(original_codes),
        "post_refs": [dict(row) for row in original_refs],
    }
    monkeypatch.setattr(
        browser,
        "_collect_grid_refs",
        lambda *_args, **_kwargs: (
            [{"url": "/owner/reel/PRICE_REEL/", "pinned": None}],
            None,
            {
                "schema_version": 1,
                "source": "instagram_reels_tab_grid",
                "status": "target_reached",
                "reason": "requested_window_reached",
            },
        ),
    )
    monkeypatch.setattr(
        browser,
        "_fetch_reel_metric",
        lambda *_args, **_kwargs: {
            "play_count": 12_345,
            "play_count_status": "observed",
            "play_count_source": "ig_media_info.ig_play_count",
            "pinned": False,
            "pinned_source": "ig_media_info_pin_lists",
            "taken_at": 123,
        },
    )
    monkeypatch.setattr(browser, "_pause", lambda *_args: None)

    state, error = browser.collect_pricing_evidence(object(), cand)

    assert error is None
    assert state["codes"] == ["/owner/reel/PRICE_REEL/"]
    assert cand["codes"] == original_codes
    assert cand["post_refs"] == original_refs
    assert cand["pricing_estimate"]["status"] == "partial"


def test_pricing_collection_fetches_media_info_for_pinned_reel(monkeypatch):
    cand = {"handle": "owner"}
    pinned_href = "/owner/reel/PINNED_REEL/"
    monkeypatch.setattr(
        browser,
        "_collect_grid_refs",
        lambda *_args, **_kwargs: (
            [{"url": pinned_href, "pinned": True}],
            None,
            {
                "schema_version": 1,
                "source": "instagram_reels_tab_grid",
                "status": "target_reached",
                "reason": "requested_window_reached",
            },
        ),
    )
    calls = []

    def pinned_metric(_page, href, **_kwargs):
        calls.append(href)
        return {
            "play_count": None,
            "play_count_status": "ig_not_exposed",
            "play_count_source": None,
            "pinned": True,
            "pinned_source": "ig_media_info_pin_lists",
            "taken_at": 123,
            "media_identity_provenance": {
                "requested_shortcode": "PINNED_REEL",
                "original_shortcode": "PINNED_REEL",
                "canonical_shortcode": None,
                "response_code": "PINNED_REEL",
                "identity_verified": True,
            },
        }

    monkeypatch.setattr(browser, "_fetch_reel_metric", pinned_metric)
    monkeypatch.setattr(browser, "_pause", lambda *_args: None)

    state, error = browser.collect_pricing_evidence(object(), cand)

    assert error is None
    assert calls == [pinned_href]
    assert state["pricing_reels"][0]["pinned"] is True
    assert state["pricing_reels"][0]["pinned_source"] == (
        "ig_media_info_pin_lists"
    )
    assert state["pricing_reels"][0]["play_count"] is None


def test_pricing_evidence_rank_prevents_deep_recollect_regression():
    assert browser._pricing_evidence_rank(  # noqa: SLF001
        {"status": "complete", "sample_count": 10}
    ) > browser._pricing_evidence_rank(  # noqa: SLF001
        {"status": "partial", "sample_count": 9}
    )
    assert browser._pricing_evidence_rank(  # noqa: SLF001
        {"status": "partial", "sample_count": 9}
    ) > browser._pricing_evidence_rank(  # noqa: SLF001
        {"status": "fallback_modash", "sample_count": 0}
    )
    assert browser._pricing_evidence_rank(None) == (0, 0)  # noqa: SLF001


def test_forged_empty_grid_terminal_cannot_outrank_existing_partial_five():
    partial = {"status": "partial", "sample_count": 5}
    forged_no_reels = {
        "status": "not_applicable_no_reels",
        "sample_count": 0,
        "quote_usd": {"default": None, "min": None, "max": None},
    }
    forged_candidate = {
        "pricing_estimate": forged_no_reels,
        "pricing_reel_samples": [],
        "pricing_reels_tab_evidence": {
            "status": "exhausted",
            "at_bottom": True,
            # Deliberately lacks identity/page-health/explicit-empty proof.
        },
    }

    assert browser._pricing_evidence_rank(  # noqa: SLF001
        forged_no_reels, forged_candidate
    ) == (0, 0)
    assert browser._pricing_evidence_rank(partial) > (0, 0)  # noqa: SLF001
