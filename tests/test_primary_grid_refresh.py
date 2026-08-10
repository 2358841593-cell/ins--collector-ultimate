"""Stage3 评论/ER 必须刷新当前主页主网格，不能反复使用历史 codes。"""
from __future__ import annotations

import copy
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import browser_collect_v2 as browser  # noqa: E402


class _Mouse:
    def wheel(self, _x, _y):
        raise AssertionError("首屏已有足够帖子，不应继续滚动")


class _GridPage:
    def __init__(self, refs):
        self.refs = refs
        self.mouse = _Mouse()

    def wait_for_timeout(self, _ms):
        return None

    def evaluate(self, script):
        assert script == browser._GRID_JS
        return {"logged_out": False, "post_refs": self.refs}


def _pricing_state():
    return {
        "refs": [],
        "codes": [],
        "pricing_reels": [],
        "metric_by_href": {},
        "sample_by_href": {},
    }, None


def test_profile_grid_refresh_uses_main_profile_and_preserves_mixed_order(
    monkeypatch,
):
    visited = []
    monkeypatch.setattr(
        browser,
        "_goto",
        lambda _page, url: visited.append(url) or True,
    )
    refs = [
        {"url": "/owner/p/NEW_POST/", "pinned": None},
        {"url": "/owner/reel/NEW_REEL/", "pinned": True},
        {"url": "/owner/p/SECOND_POST/", "pinned": None},
    ]

    result, error = browser._collect_profile_grid_refs(  # noqa: SLF001
        _GridPage(refs), "owner", target_posts=3
    )

    assert error is None
    assert result == refs
    assert visited == ["https://www.instagram.com/owner/"]
    assert any("/p/" in row["url"] for row in result)
    assert any("/reel/" in row["url"] for row in result)


def test_deep_collect_replaces_stale_primary_refs_before_visiting_posts(
    tmp_path, monkeypatch
):
    fresh = [
        {"url": f"/owner/p/CURRENT_{index}/", "pinned": None}
        for index in range(3)
    ]
    cand = {
        "handle": "owner",
        "codes": ["/owner/p/DELETED_OLD/"],
        "post_refs": [{"url": "/owner/p/DELETED_OLD/", "pinned": None}],
    }
    visited = []
    monkeypatch.setattr(
        browser,
        "_collect_profile_grid_refs",
        lambda *_args, **_kwargs: (fresh, None),
    )
    monkeypatch.setattr(
        browser, "collect_pricing_evidence", lambda *_args: _pricing_state()
    )
    monkeypatch.setattr(
        browser,
        "_goto",
        lambda _page, url: visited.append(url) or False,
    )
    monkeypatch.setattr(browser, "_pause", lambda *_args: None)

    result, error = browser.deep_collect(
        object(), cand, tmp_path / "evidence", n_posts=3
    )

    assert result is None
    assert error.startswith("proxy_throttled:")
    assert cand["codes"] == [row["url"] for row in fresh]
    assert cand["post_refs"] == fresh
    assert all("CURRENT_" in url for url in visited)
    assert all("DELETED_OLD" not in url for url in visited)
    assert "primary_grid_refresh_error" not in cand
    assert cand["primary_refs_source"] == "current_profile_grid"


def test_deep_collect_falls_back_to_history_only_when_refresh_fails(
    tmp_path, monkeypatch
):
    old = [
        {"url": f"/owner/p/HISTORICAL_{index}/", "pinned": None}
        for index in range(3)
    ]
    cand = {
        "handle": "owner",
        "codes": [row["url"] for row in old],
        "post_refs": [dict(row) for row in old],
    }
    visited = []
    monkeypatch.setattr(
        browser,
        "_collect_profile_grid_refs",
        lambda *_args, **_kwargs: (
            [],
            "profile_grid_nav_failed:timeout",
        ),
    )
    monkeypatch.setattr(
        browser, "collect_pricing_evidence", lambda *_args: _pricing_state()
    )
    monkeypatch.setattr(
        browser,
        "_goto",
        lambda _page, url: visited.append(url) or False,
    )
    monkeypatch.setattr(browser, "_pause", lambda *_args: None)

    result, error = browser.deep_collect(
        object(), cand, tmp_path / "evidence", n_posts=3
    )

    assert result is None
    assert error.startswith("proxy_throttled:")
    assert cand["codes"] == [row["url"] for row in old]
    assert cand["post_refs"] == old
    assert all("HISTORICAL_" in url for url in visited)
    assert cand["primary_grid_refresh_error"] == (
        "profile_grid_nav_failed:timeout"
    )
    assert cand["primary_refs_source"] == "historical_primary_refs"


def test_pricing_sample_refs_preserve_order_dedupe_and_limit():
    samples = [
        {"code": "/owner/reel/ONE/", "url": "https://ignored/ONE"},
        {"url": "https://www.instagram.com/owner/reel/TWO/"},
        {"code": "/owner/reel/ONE/"},
        {"code": "/owner/reel/THREE/"},
    ]

    refs = browser._refs_from_pricing_samples(samples, limit=2)  # noqa: SLF001

    assert refs == [
        {"url": "/owner/reel/ONE/", "pinned": None},
        {
            "url": "https://www.instagram.com/owner/reel/TWO/",
            "pinned": None,
        },
    ]


class _DeepPage:
    def wait_for_timeout(self, _ms):
        return None

    def evaluate(self, script, _arg=None):
        assert script == browser._DEEP_READ_JS
        return {
            "caption": "1 likes, 0 comments on Instagram: fallback reel",
            "video": True,
            "taken_at": 123,
            "text": "",
        }


def test_existing_complete_pricing_samples_are_last_resort_primary_window(
    tmp_path, monkeypatch
):
    old_samples = [
        {
            "code": f"/adan.skincare/reel/SAVED_{index}/",
            "pinned": False,
            "play_count": 5000 + index,
            "play_count_status": "observed",
            "play_count_source": "ig_media_info.ig_play_count",
        }
        for index in range(10)
    ]
    cand = {
        "handle": "adan.skincare",
        "pricing_reel_samples": copy.deepcopy(old_samples),
        "pricing_captured_at": "2026-07-28T12:00:00",
    }
    cand["pricing_estimate"] = browser.pricing_mod.derive_quote_estimate(
        cand, browser._CFG
    )
    old_estimate = copy.deepcopy(cand["pricing_estimate"])
    visited = []
    monkeypatch.setattr(browser, "ROOT", tmp_path)
    monkeypatch.setattr(
        browser,
        "_collect_profile_grid_refs",
        lambda *_args, **_kwargs: ([], "profile_grid_empty"),
    )

    def empty_current_pricing(_page, candidate):
        candidate["pricing_reel_samples"] = []
        candidate["pricing_estimate"] = {
            "status": "missing",
            "sample_count": 0,
        }
        return _pricing_state()

    monkeypatch.setattr(
        browser, "collect_pricing_evidence", empty_current_pricing
    )
    monkeypatch.setattr(
        browser,
        "_goto",
        lambda _page, url: visited.append(url) or True,
    )
    monkeypatch.setattr(browser, "_pause", lambda *_args: None)
    monkeypatch.setattr(browser, "_cache_save", lambda *_args: None)

    result, _evidence = browser.deep_collect(
        _DeepPage(),
        cand,
        tmp_path / "data/evidence/BATCH/adan.skincare",
        n_posts=10,
    )

    assert result is cand
    assert [row["code"] for row in result["sampled_posts"]] == [
        row["code"] for row in old_samples
    ]
    assert visited == [
        f"https://www.instagram.com{row['code']}" for row in old_samples
    ]
    assert result["deep_available_posts"] == 10
    assert result["deep_successful_posts"] == 10
    assert result["primary_refs_source"] == "existing_pricing_samples"
    assert result["primary_refs_fallback_reason"] == (
        "profile_grid_empty;historical_primary_refs_empty;"
        "current_pricing_refs_empty"
    )
    assert result["pricing_estimate"] == old_estimate
    assert result["pricing_reel_samples"] == old_samples
