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

    def evaluate(self, script):
        assert script == browser._GRID_JS
        return {
            "logged_out": False,
            "post_refs": self.refs,
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

    result, error = browser._collect_grid_refs(  # noqa: SLF001
        _GridPage(refs), "owner", min_non_pinned_reels=13
    )

    assert error is None
    assert len(result) == 13
    assert visited == ["https://www.instagram.com/owner/reels/"]


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
