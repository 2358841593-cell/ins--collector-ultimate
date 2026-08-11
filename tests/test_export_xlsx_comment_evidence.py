"""Structured comment evidence in the customer-facing XLSX."""
from __future__ import annotations

import export_v2_xlsx


def _delivery(candidate):
    return {
        "generated_at": "2026-08-11T07:00:00+0800",
        "manifest": {
            "batch_id": "TEST",
            "campaign_track": "paid",
        },
        "candidates": [candidate],
    }


def _candidate(**overrides):
    value = {
        "handle": "multilingual_creator",
        "full_name": "Multilingual Creator",
        "final_pool": "Review",
        "comment_shots": [],
        "comment_translations": [
            {
                "username": "buyer_one",
                "post_url": "https://www.instagram.com/p/POST1/",
                "original_text": "Où puis-je acheter ça ?",
                "translated_zh": "我在哪里可以买到？",
                "source_language": "fr",
                "status": "translated",
                "translated_intent_grade_zh": "高",
                "translated_low_quality": False,
            },
            {
                "username": "buyer_two",
                "post_url": "https://www.instagram.com/reel/POST2/",
                "original_text": "=FORMULA()",
                "translated_zh": "+中文公式前缀",
                "source_language": "en",
                "status": "translated",
                "translated_intent_grade_zh": None,
                "translated_low_quality": True,
            },
        ],
    }
    value.update(overrides)
    return value


def test_comment_evidence_sheet_contains_all_translations_without_screenshots():
    workbook = export_v2_xlsx.build_workbook(_delivery(_candidate()))

    assert workbook.sheetnames == [
        "批次总览",
        "评论证据",
        "纳入·有橱窗",
        "纳入·无橱窗",
        "优先复核",
        "待复核",
        "已排除",
    ]
    evidence = workbook["评论证据"]
    assert evidence.max_row == 5
    assert [evidence.cell(3, column).value for column in range(1, 12)] == [
        "Handle",
        "全名",
        "评论者",
        "中文译文",
        "原文",
        "原语言",
        "购买意向等级",
        "低质评论",
        "翻译状态",
        "帖子链接",
        "证据来源",
    ]
    assert evidence["A4"].value == "'@multilingual_creator"
    assert evidence["C4"].value == "buyer_one"
    assert evidence["D4"].value == "我在哪里可以买到？"
    assert evidence["E4"].value == "Où puis-je acheter ça ?"
    assert evidence["F4"].value == "fr"
    assert evidence["G4"].value == "高"
    assert evidence["J4"].hyperlink.target == (
        "https://www.instagram.com/p/POST1/"
    )
    assert evidence["D5"].value == "'+中文公式前缀"
    assert evidence["E5"].value == "'=FORMULA()"

    review = workbook["待复核"]
    assert review["G2"].value == "证据 ↗"
    assert review["G2"].hyperlink.target == "#评论证据!A4"


def test_comment_evidence_rejects_active_or_local_link_protocols():
    candidate = _candidate(
        comment_translations=[
            {
                "username": "unsafe",
                "post_url": "file:///Users/example/private.html",
                "original_text": "hello",
                "translated_zh": "你好",
                "source_language": "en",
                "status": "translated",
            },
            {
                "username": "unsafe_two",
                "post_url": "javascript:alert(1)",
                "original_text": "world",
                "translated_zh": "世界",
                "source_language": "en",
                "status": "translated",
            },
        ]
    )

    workbook = export_v2_xlsx.build_workbook(_delivery(candidate))
    evidence = workbook["评论证据"]

    assert evidence["J4"].value is None
    assert evidence["J4"].hyperlink is None
    assert evidence["J5"].value is None
    assert evidence["J5"].hyperlink is None


def test_legacy_comment_records_remain_visible_without_translation_rows():
    candidate = _candidate(
        comment_translations=[],
        comment_records=[
            {
                "username": "legacy_user",
                "text": "legacy original",
                "post_url": "https://www.instagram.com/p/LEGACY/",
            }
        ],
    )

    workbook = export_v2_xlsx.build_workbook(_delivery(candidate))
    evidence = workbook["评论证据"]

    assert evidence["C4"].value == "legacy_user"
    assert evidence["D4"].value == ""
    assert evidence["E4"].value == "legacy original"
    assert evidence["I4"].value == "not_translated_legacy"
    assert evidence["K4"].value == "历史结构化原文"


def test_all_external_workbook_links_reject_non_http_protocols():
    candidate = _candidate(
        profile_url="file:///Users/example/profile.html",
        storefront_status="confirmed_yes",
        storefront_url="javascript:alert(1)",
        pricing_estimate={
            "status": "complete",
            "sample_count": 10,
            "requested_reels": 10,
            "reels": [{"url": "file:///tmp/reel.html"}],
        },
    )

    workbook = export_v2_xlsx.build_workbook(_delivery(candidate))
    review = workbook["待复核"]

    # Invalid explicit profile URL falls back to the canonical Instagram URL.
    assert review["U2"].hyperlink.target == (
        "https://www.instagram.com/multilingual_creator/"
    )
    assert review["H2"].hyperlink is None
    assert review["L2"].hyperlink is None


def test_batch_summary_metadata_is_neutralized_against_formulas():
    delivery = _delivery(_candidate())
    delivery["manifest"]["batch_id"] = "=1+1"
    delivery["generated_at"] = '=WEBSERVICE("https://example.com")'

    workbook = export_v2_xlsx.build_workbook(delivery)
    summary = workbook["批次总览"]

    assert summary["B3"].value == "'=1+1"
    assert summary["B4"].value == "'=WEBSERVICE(\"https://example.com\")"
    assert summary["B3"].data_type != "f"
    assert summary["B4"].data_type != "f"


def test_handle_with_at_prefix_still_links_to_structured_evidence():
    workbook = export_v2_xlsx.build_workbook(
        _delivery(_candidate(handle="@multilingual_creator"))
    )

    assert workbook["待复核"]["G2"].hyperlink.target == "#评论证据!A4"


def test_long_identity_and_timestamp_cells_wrap_in_candidate_sheets():
    workbook = export_v2_xlsx.build_workbook(
        _delivery(
            _candidate(
                handle="fixture.creator.with.a.long.handle",
                full_name="Fixture Creator With A Deliberately Long Display Name",
                captured_at="2026-08-11T16:19:21+0800",
            )
        )
    )

    review = workbook["待复核"]
    assert review["B2"].alignment.wrap_text is True
    assert review["C2"].alignment.wrap_text is True
    assert review["V2"].alignment.wrap_text is True
    assert review.column_dimensions["B"].width == 28
    assert review.column_dimensions["C"].width == 32
    assert review.row_dimensions[2].height == 46
    assert review["J1"].alignment.wrap_text is True
    assert review.row_dimensions[1].height == 46
