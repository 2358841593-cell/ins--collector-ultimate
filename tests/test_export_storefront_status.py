"""客户交付 JSON / HTML / XLSX 的 Storefront 三态展示合同。"""
from __future__ import annotations

import json

import export_v2_html
import export_v2_xlsx
from _fixtures import clean_full
from extensions.sop_v2 import run_v2
from extensions.sop_v2.config import load_config


CFG = load_config()


def _decide(**overrides):
    return run_v2.decide(clean_full(handle="storefront_case", **overrides), CFG)


def test_confirmed_no_with_ordinary_bio_link_stays_confirmed_no_everywhere():
    candidate = _decide(
        storefront_status="confirmed_no",
        bio_links=["https://example.com/about"],
    )

    # JSON 是 HTML/XLSX 的正式共用输入；序列化后不得因普通 bio 链改变三态。
    delivered = json.loads(json.dumps(candidate, ensure_ascii=False))
    assert delivered["storefront_status"] == "confirmed_no"
    assert delivered["final_pool"] == "Include-Without-Storefront"

    html = export_v2_html._storefront_cell(delivered)  # noqa: SLF001
    assert "确认无橱窗" in html
    assert "橱窗未确认" not in html
    assert "example.com" not in html

    assert export_v2_xlsx._cell(delivered, "storefront_link") == "确认无橱窗"  # noqa: SLF001
    assert export_v2_xlsx._url(delivered, "storefront_link") is None  # noqa: SLF001


def test_only_unknown_storefront_may_offer_bio_link_for_manual_verification():
    candidate = _decide(
        storefront_status="unknown",
        bio_links=["https://example.com/about"],
    )

    delivered = json.loads(json.dumps(candidate, ensure_ascii=False))
    assert delivered["storefront_status"] == "unknown"
    assert delivered["final_pool"] == "Review"

    html = export_v2_html._storefront_cell(delivered)  # noqa: SLF001
    assert "Bio 链接（橱窗未确认）" in html
    assert "https://example.com/about" in html

    assert export_v2_xlsx._cell(delivered, "storefront_link") == "未确认"  # noqa: SLF001
    assert export_v2_xlsx._url(delivered, "storefront_link") is None  # noqa: SLF001


def test_recognized_storefront_entry_wins_over_bio_link_in_all_formats():
    candidate = _decide(
        storefront_status="confirmed_no",
        storefront_url="https://shopmy.us/storefront-case",
        storefront_type="ShopMy",
        bio_links=["https://example.com/about"],
    )

    delivered = json.loads(json.dumps(candidate, ensure_ascii=False))
    assert delivered["storefront_status"] == "confirmed_yes"
    assert delivered["storefront_type"] == "ShopMy"
    assert delivered["final_pool"] == "Include-With-Storefront"

    html = export_v2_html._storefront_cell(delivered)  # noqa: SLF001
    assert "ShopMy ↗" in html
    assert "https://shopmy.us/storefront-case" in html
    assert "橱窗未确认" not in html

    assert export_v2_xlsx._cell(delivered, "storefront_link") == "ShopMy ↗"  # noqa: SLF001
    assert export_v2_xlsx._url(delivered, "storefront_link") == (  # noqa: SLF001
        "https://shopmy.us/storefront-case"
    )
