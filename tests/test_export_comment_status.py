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


def test_verified_empty_thread_keeps_reported_count_and_transparent_note():
    posts = _posts(comments=0)
    posts[0].update(
        {
            "comment_count": 3,
            "comments_collected": 0,
            "comment_sampling_status": "verified_empty_thread",
        }
    )
    candidate = {
        "deep_collection_status": "complete",
        "comments_read": True,
        "comments_analyzed": 0,
        "valid_comments": 0,
        "sampled_posts": posts,
        "comment_unavailable_posts": [
            {
                "url": "https://www.instagram.com/p/EMPTY/",
                "reported_count": 3,
                "reason": "verified_empty_thread_despite_reported_count",
            }
        ],
    }

    expected = (
        "1帖评论线程明确为空"
        "（页面与端点双重核验；原上报数保留）"
    )
    assert expected in _html(candidate)
    assert _xlsx(candidate) == expected
    assert posts[0]["comment_count"] == 3


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


def test_translated_intent_shows_chinese_original_language_in_html_and_xlsx():
    candidate = {
        "deep_collection_status": "complete",
        "comments_read": True,
        "comments_analyzed": 30,
        "valid_comments": 30,
        "sampled_posts": _posts(comments=3),
        "translated_intent_by_grade": {"high": 1, "medium": 0, "low": 0},
        "translated_intent_comments": [
            {
                "username": "marie",
                "grade": "high",
                "grade_zh": "高",
                "post_url": "https://www.instagram.com/p/FRENCH/",
                "original_text": "Où puis-je acheter ce sérum ?",
                "translated_zh": "我在哪里可以买这个精华？",
                "source_language": "fr",
                "status": "translated",
            }
        ],
        "comment_translation_summary": {
            "status": "complete",
            "requested_count": 1,
            "translated_count": 1,
            "failed_count": 0,
            "source_languages": {"fr": 1},
        },
    }

    html = _html(candidate)
    xlsx = _xlsx(candidate)
    for value in (html, xlsx):
        assert "我在哪里可以买这个精华？" in value
        assert "Où puis-je acheter ce sérum ?" in value
        assert "fr" in value
    assert "意图判定暂未覆盖" not in html


def test_translation_failure_is_rendered_with_original_not_as_empty_comment():
    candidate = {
        "deep_collection_status": "complete",
        "comments_read": True,
        "comments_analyzed": 30,
        "valid_comments": 30,
        "sampled_posts": _posts(comments=3),
        "high_intent_snippets": ["@ira（高）: Где ссылка?"],
        "intent_by_grade": {"high": 1},
        "translated_intent_comments": [
            {
                "username": "ira",
                "grade": "high",
                "grade_zh": "高",
                "original_text": "Где ссылка?",
                "translated_zh": None,
                "source_language": "und",
                "status": "failed",
                "translation_error": "provider_network_error",
            }
        ],
        "comment_translation_summary": {
            "status": "failed",
            "requested_count": 1,
            "translated_count": 0,
            "failed_count": 1,
            "source_languages": {},
        },
    }

    html = _html(candidate)
    xlsx = _xlsx(candidate)
    for value in (html, xlsx):
        assert "Где ссылка?" in value
        assert "中文翻译失败" in value
        assert "原文已保留" in value


def test_translated_non_intent_sample_replaces_language_unsupported_warning():
    candidate = {
        "deep_collection_status": "complete",
        "comments_read": True,
        "comments_analyzed": 30,
        "valid_comments": 30,
        "sampled_posts": _posts(comments=3),
        "top_language": "Korean",
        "comment_sample": ["매일 배우는 뷰티 팁 정말 좋아요"],
        "comment_translations": [
            {
                "scope": "sample",
                "original_text": "매일 배우는 뷰티 팁 정말 좋아요",
                "translated_zh": "我很喜欢每天学到的美容技巧",
                "source_language": "ko",
                "status": "translated",
            }
        ],
        "comment_translation_summary": {
            "status": "complete",
            "requested_count": 1,
            "translated_count": 1,
            "failed_count": 0,
            "source_languages": {"ko": 1},
        },
    }

    html = _html(candidate)
    xlsx = _xlsx(candidate)
    for value in (html, xlsx):
        assert "我很喜欢每天学到的美容技巧" in value
        assert "매일 배우는 뷰티 팁 정말 좋아요" in value
        assert "意图判定暂未覆盖" not in value
