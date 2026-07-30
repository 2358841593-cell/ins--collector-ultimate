"""评论首抽为空时的有界重试语义。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import browser_collect_v2 as browser  # noqa: E402


def test_observed_instagram_ui_rows_are_not_counted_as_comments():
    cleaned = browser._clean_comment_pairs(
        [
            {"username": "viewer", "text": "Liked by someone and others"},
            {"username": "viewer", "text": "7 hours ago"},
            {
                "username": "viewer",
                "text": "This reel has 1 comment from Facebook.",
            },
            {"username": "viewer", "text": "Hide all replies"},
            {"username": "owner", "text": "creator caption"},
            {"username": "buyer", "text": "Where can I buy this?"},
        ],
        owner="owner",
    )

    assert cleaned == [
        {"username": "buyer", "text": "Where can I buy this?"}
    ]


@pytest.mark.parametrize(
    ("comment_count", "paired_count", "expected"),
    [
        (0, 0, False),
        (1, 0, True),
        (2, 0, True),
        (6, 0, True),
        (None, 0, True),
        (14, 1, False),
        (15, 4, True),
        (15, 5, False),
    ],
)
def test_comment_retry_decision_does_not_treat_reported_count_as_completion(
    comment_count, paired_count, expected
):
    assert (
        browser._comment_retry_needed(comment_count, paired_count) is expected
    )


class _CommentPage:
    def __init__(self, results):
        self.results = iter(results)
        self.calls = []

    def evaluate(self, script, owner):
        self.calls.append((script, owner))
        return next(self.results)


def test_low_positive_comment_count_retries_and_uses_real_second_result(
    monkeypatch,
):
    loads = []
    monkeypatch.setattr(
        browser,
        "_load_comments",
        lambda _page, rounds: loads.append(rounds),
    )
    page = _CommentPage(
        [
            [],
            [{"username": "viewer", "text": "Where can I buy this?"}],
        ]
    )

    paired, attempts = browser._sample_comment_pairs(page, "owner", 2)

    assert attempts == 2
    assert loads == [4, 3]
    assert paired == [
        {"username": "viewer", "text": "Where can I buy this?"}
    ]
    assert page.calls == [
        (browser._COMMENTS_JS, "owner"),
        (browser._COMMENTS_JS, "owner"),
    ]


def test_ui_only_first_result_is_treated_as_empty_and_retried(monkeypatch):
    loads = []
    monkeypatch.setattr(
        browser,
        "_load_comments",
        lambda _page, rounds: loads.append(rounds),
    )
    page = _CommentPage(
        [
            [{"username": "viewer", "text": "1 day ago"}],
            [{"username": "buyer", "text": "Need the link"}],
        ]
    )

    paired, attempts = browser._sample_comment_pairs(page, "owner", 2)

    assert attempts == 2
    assert loads == [4, 3]
    assert paired == [{"username": "buyer", "text": "Need the link"}]


def test_two_empty_attempts_stay_empty_for_strict_failure(monkeypatch):
    loads = []
    monkeypatch.setattr(
        browser,
        "_load_comments",
        lambda _page, rounds: loads.append(rounds),
    )

    paired, attempts = browser._sample_comment_pairs(
        _CommentPage([[], []]), "owner", 6
    )

    assert attempts == 2
    assert loads == [4, 3]
    assert paired == []


class _DeepCommentPage:
    def __init__(self, reported_count=2):
        self.comment_reads = 0
        self.reported_count = reported_count

    def wait_for_timeout(self, _milliseconds):
        return None

    def evaluate(self, script, _arg=None):
        if script == browser._DEEP_READ_JS:
            return {
                "caption": (
                    f"10 likes, {self.reported_count} comments - "
                    "owner on Instagram: test"
                ),
                "video": False,
                "taken_at": 123,
                "text": "",
            }
        if script == browser._COMMENTS_JS:
            self.comment_reads += 1
            return []
        raise AssertionError("unexpected browser script")


def _patch_deep_dependencies(monkeypatch, tmp_path, href="/owner/p/POST/"):
    monkeypatch.setattr(browser, "ROOT", tmp_path)
    monkeypatch.setattr(
        browser,
        "_collect_profile_grid_refs",
        lambda *_args, **_kwargs: (
            [{"url": href, "pinned": None}],
            None,
        ),
    )
    monkeypatch.setattr(
        browser,
        "collect_pricing_evidence",
        lambda *_args: (
            {
                "refs": [],
                "codes": [],
                "pricing_reels": [],
                "metric_by_href": {},
                "sample_by_href": {},
            },
            None,
        ),
    )
    monkeypatch.setattr(browser, "_goto", lambda *_args: True)
    monkeypatch.setattr(browser, "_load_comments", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(browser, "_pause", lambda *_args: None)
    monkeypatch.setattr(browser, "_cache_save", lambda *_args: None)


def _run_deep(
    tmp_path,
    monkeypatch,
    candidate=None,
    reported_count=2,
    href="/owner/p/POST/",
):
    _patch_deep_dependencies(monkeypatch, tmp_path, href)
    page = _DeepCommentPage(reported_count)

    result, _evidence = browser.deep_collect(
        page,
        candidate or {"handle": "owner"},
        tmp_path / "data/evidence/BATCH/owner",
        n_posts=1,
    )
    return page, result


def test_two_empty_attempts_remain_deep_incomplete(tmp_path, monkeypatch):
    page, result = _run_deep(tmp_path, monkeypatch)

    post = result["sampled_posts"][0]
    assert page.comment_reads == 2
    assert post["comment_sampling_attempts"] == 2
    assert post["comment_sampling_status"] == "failed_reported_comments"
    assert result["comment_failed_posts"] == [
        "https://www.instagram.com/owner/p/POST/"
    ]
    assert result["comment_unavailable_posts"] == []
    assert result["deep_collection_status"] == "incomplete"


@pytest.mark.parametrize("reported_count", [1, 2])
def test_same_low_count_url_failing_in_second_round_becomes_unavailable(
    tmp_path, monkeypatch, reported_count
):
    page, result = _run_deep(
        tmp_path,
        monkeypatch,
        candidate={
            "handle": "owner",
            # 绝对/相对 URL 和 handle 前缀不同仍代表同一媒体。
            "comment_failed_posts": [
                "https://www.instagram.com/p/POST/?previous=1"
            ],
        },
        reported_count=reported_count,
    )

    post = result["sampled_posts"][0]
    assert page.comment_reads == 2
    assert post["comment_count"] == reported_count
    assert post["comments_collected"] == 0
    assert post["comment_sampling_status"] == "unavailable_after_retry"
    assert result["comment_completed_posts"] == 1
    assert result["comment_failed_posts"] == []
    assert result["comment_unavailable_posts"] == [
        {
            "url": "https://www.instagram.com/owner/p/POST/",
            "reported_count": reported_count,
            "reason": "reported_low_count_unavailable_after_retry",
        }
    ]
    assert result["comment_sample"] == []
    assert result["comments_analyzed"] == 0
    assert result["deep_collection_status"] == "complete"


def test_repeated_failure_above_two_comments_remains_incomplete(
    tmp_path, monkeypatch
):
    _page, result = _run_deep(
        tmp_path,
        monkeypatch,
        candidate={
            "handle": "owner",
            "comment_failed_posts": ["/owner/p/POST/"],
        },
        reported_count=3,
    )

    assert result["comment_unavailable_posts"] == []
    assert result["comment_failed_posts"] == [
        "https://www.instagram.com/owner/p/POST/"
    ]
    assert result["deep_collection_status"] == "incomplete"


def test_different_previous_failed_url_does_not_close_current_failure(
    tmp_path, monkeypatch
):
    _page, result = _run_deep(
        tmp_path,
        monkeypatch,
        candidate={
            "handle": "owner",
            "comment_failed_posts": ["/owner/p/OTHER/"],
        },
        reported_count=2,
    )

    assert result["comment_unavailable_posts"] == []
    assert result["comment_failed_posts"] == [
        "https://www.instagram.com/owner/p/POST/"
    ]
    assert result["deep_collection_status"] == "incomplete"


def test_post_and_reel_aliases_share_comment_retry_identity(
    tmp_path, monkeypatch
):
    _page, result = _run_deep(
        tmp_path,
        monkeypatch,
        candidate={
            "handle": "owner",
            "comment_failed_posts": [
                {
                    "url": "https://www.instagram.com/p/POST/?old=1",
                }
            ],
        },
        reported_count=1,
        href="/owner/reel/POST/",
    )

    assert result["comment_failed_posts"] == []
    assert result["comment_unavailable_posts"][0]["url"] == (
        "https://www.instagram.com/owner/reel/POST/"
    )
    assert result["deep_collection_status"] == "complete"


def test_existing_unavailable_terminal_is_stable_during_direct_retry(
    tmp_path, monkeypatch
):
    previous = {
        "url": "https://www.instagram.com/owner/p/POST/",
        "reported_count": 2,
        "reason": "reported_low_count_unavailable_after_retry",
    }
    _page, result = _run_deep(
        tmp_path,
        monkeypatch,
        candidate={
            "handle": "owner",
            "comment_unavailable_posts": [previous],
        },
        reported_count=2,
    )

    assert result["comment_failed_posts"] == []
    assert result["comment_unavailable_posts"] == [previous]
    assert result["deep_collection_status"] == "complete"
