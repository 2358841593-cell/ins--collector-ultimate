"""Comment delivery labels follow the explicit deep-collection contract."""
from __future__ import annotations

import export_v2_html
import export_v2_xlsx


def _posts(count=10, comments=0):
    return [
        {"code": f"/p/P{index}/", "comment_count": comments}
        for index in range(count)
    ]


def _html(candidate):
    return export_v2_html._intent_cell(candidate)  # noqa: SLF001


def _xlsx(candidate):
    return export_v2_xlsx._cell(  # noqa: SLF001
        candidate, "intent_snippet"
    )


def test_complete_verified_zero_comments_is_not_labeled_for_recollection():
    candidate = {
        "deep_collection_status": "complete",
        "comments_read": True,
        "comments_analyzed": 0,
        "valid_comments": 0,
        "sampled_posts": _posts(comments=0),
    }

    expected = "未发现公开评论（已完成采集）"
    assert expected in _html(candidate)
    assert _xlsx(candidate) == expected
    assert "待补" not in _html(candidate)
    assert "待复采" not in _xlsx(candidate)


def test_complete_with_low_volume_comments_unavailable_shows_recollected_note():
    posts = _posts(comments=0)
    posts[0]["comment_count"] = 1
    posts[1]["comment_count"] = 2
    candidate = {
        "deep_collection_status": "complete",
        "comments_read": True,
        "comments_analyzed": 0,
        "valid_comments": 0,
        "sampled_posts": posts,
        "comment_unavailable_posts": [
            {
                "url": "https://www.instagram.com/p/LOW1/",
                "reported_count": 1,
                "reason": "reported_low_count_unavailable_after_retry",
            },
            {
                "url": "https://www.instagram.com/p/LOW2/",
                "reported_count": 2,
                "reason": "reported_low_count_unavailable_after_retry",
            },
        ],
    }

    expected = "2帖低量评论重复不可见（已复采）"
    html = _html(candidate)
    xlsx = _xlsx(candidate)
    assert expected in html
    assert xlsx == expected
    assert "未发现公开评论（已完成采集）" not in html
    assert "待补" not in html
    assert "待复采" not in xlsx


def test_complete_small_public_sample_is_limited_not_pending():
    candidate = {
        "deep_collection_status": "complete",
        "comments_read": True,
        "comments_analyzed": 7,
        "valid_comments": 7,
        "sampled_posts": _posts(comments=2),
    }

    expected = "公开有效评论有限（7条，已完成采集）"
    assert expected in _html(candidate)
    assert _xlsx(candidate) == expected
    assert "待补采" not in _html(candidate)


def test_legacy_complete_evidence_is_not_labeled_for_recollection():
    candidate = {
        "comments_read": True,
        "comments_analyzed": 30,
        "valid_comments": 30,
        "real_er": 1.2,
        "sampled_posts": [
            {
                "code": f"/p/P{index}/",
                "like_count": 100 + index,
                "comment_count": 3,
            }
            for index in range(10)
        ],
        "high_intent_snippets": ["@buyer（高）: link please"],
        "intent_by_grade": {"high": 1},
    }

    html = _html(candidate)
    xlsx = _xlsx(candidate)
    assert "@buyer（高）: link please" in html
    assert "@buyer（高）: link please" in xlsx
    assert "评论采集未完成" not in html
    assert "待复采" not in html
    assert "评论采集未完成" not in xlsx
    assert "待复采" not in xlsx


def test_incomplete_zero_extraction_remains_pending_recollection():
    candidate = {
        "deep_collection_status": "incomplete",
        "comments_read": True,
        "comments_analyzed": 0,
        "valid_comments": 0,
        "sampled_posts": _posts(comments=None),
    }

    assert "评论抽取失败（待复采）" in _html(candidate)
    assert _xlsx(candidate) == "评论抽取失败（待复采）"


def test_incomplete_with_existing_snippet_keeps_evidence_and_warning():
    candidate = {
        "deep_collection_status": "incomplete",
        "comments_read": True,
        "comments_analyzed": 1,
        "valid_comments": 1,
        "sampled_posts": _posts(2, comments=1),
        "high_intent_snippets": ["@buyer（高）: link please"],
        "intent_by_grade": {"high": 1},
    }

    html = _html(candidate)
    xlsx = _xlsx(candidate)
    assert "评论采集未完成（待复采）" in html
    assert "@buyer（高）: link please" in html
    assert "评论采集未完成（待复采）" in xlsx
    assert "@buyer（高）: link please" in xlsx
