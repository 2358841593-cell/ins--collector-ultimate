"""Storefront V2 口径：任意认可电商入口都算，Amazon 不是 Stage 2 硬门槛。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import browser_collect_v2 as bc  # noqa: E402
from extensions.sop_v2 import creator_cache as cc  # noqa: E402
from extensions.sop_v2 import storefront  # noqa: E402
from extensions.sop_v2.config import load_config  # noqa: E402
from extensions.sop_v2.pipeline.stage2_qualify import qualify_one  # noqa: E402


@pytest.mark.parametrize(
    ("url", "kind"),
    [
        ("https://amazon.com/shop/example", "Amazon"),
        ("https://www.amazon.co.uk/shop/example", "Amazon"),
        ("https://creator.amazon.de/profile", "Amazon"),
        ("https://amzn.to/abc", "Amazon"),
        ("https://shopltk.com/explore/example", "LTK"),
        ("https://creator.shopltk.com/explore/example", "LTK"),
        ("https://www.liketoknow.it/example", "LTK"),
        ("https://shopmy.us/example", "ShopMy"),
        ("https://go.shopmy.us/example", "ShopMy"),
        ("https://brand.example/shop/products", "自营店"),
        ("https://brand.myshopify.com/", "自营店"),
        ("https://linktr.ee/example", "链接聚合"),
        ("https://wonderl.ink/example", "链接聚合"),
        ("https://bio.site/example", "链接聚合"),
        ("https://taplink.cc/example", "链接聚合"),
        ("https://zez.am/example", "链接聚合"),
        ("https://myyshop.com/p/example", "链接聚合"),
        ("https://creator.myyshop.com/p/example", "链接聚合"),
        ("https://myyfinds.io/example", "链接聚合"),
        ("https://creator.myyfinds.io/example", "链接聚合"),
        ("https://stan.store/example", "自营店"),
        ("https://sumupstore.com/", "自营店"),
        ("https://angies.sumupstore.com/", "自营店"),
        ("https://evilmyyshop.com/p/example", None),
        ("https://evilmyyfinds.io/example", None),
        ("https://evilsumupstore.com/", None),
        ("https://amazon.com.evil.example/profile", None),
        ("https://amazon.evil.example/profile", None),
        ("https://x.amazon.com.evil.example/profile", None),
        ("https://shopmy.us.evil.example/profile", None),
        ("https://evilshopmy.example/profile", None),
        ("https://shopltk.com.evil.example/profile", None),
        ("https://evilshopltk.example/profile", None),
        ("https://liketoknow.it.evil.example/profile", None),
        ("https://evilliketoknow.example/profile", None),
        ("https://calendly.com/example", None),
        ("https://instagram.com/example", None),
    ],
)
def test_classify_storefront_url(url, kind):
    assert storefront.classify_url(url) == kind


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://amazon.com/shop/example", True),
        ("http://brand.myshopify.com/products/example", True),
        ("javascript://amazon.com/shop/example", False),
        ("file://amazon.com/shop/example", False),
        ("amazon.com/shop/example", False),
        ("https://user:secret@amazon.com/shop/example", False),
        ("https://localhost/shop/example", False),
        ("https://127.0.0.1/shop/example", False),
        ("https://8.8.8.8/shop/example", False),
        ("https://amazon.com:444/shop/example", False),
    ],
)
def test_safe_absolute_storefront_http_url_contract(url, expected):
    assert storefront.is_safe_absolute_http_url(url) is expected


def test_delivery_validation_allows_confirmed_no_with_ordinary_bio_links():
    assert storefront.delivery_validation_reasons(
        {
            "storefront_status": "confirmed_no",
            "bio_links": ["https://example.com/about"],
        }
    ) == []


@pytest.mark.parametrize(
    ("url", "declared_type"),
    [
        ("https://amazon.com.evil.example/profile", "Amazon"),
        ("https://amazon.evil.example/profile", "Amazon"),
        ("https://shopmy.us.evil.example/profile", "ShopMy"),
        ("https://evilshopmy.example/profile", "ShopMy"),
        ("https://shopltk.com.evil.example/profile", "LTK"),
        ("https://evilshopltk.example/profile", "LTK"),
    ],
)
def test_delivery_validation_rejects_storefront_lookalike_domains(
    url, declared_type
):
    reasons = storefront.delivery_validation_reasons(
        {
            "storefront_status": "confirmed_yes",
            "storefront_url": url,
            "storefront_type": declared_type,
        }
    )
    assert "storefront_url_unrecognized" in reasons


def test_non_amazon_shop_path_is_not_mislabeled_amazon():
    assert bc._shop_type("https://brand.example/shop/skin") == "自营店"


@pytest.mark.parametrize("host", ["atom.bio", "taplink.cc", "zez.am"])
def test_profile_html_fallback_recovers_supported_link_in_bio_hosts(host):
    assert bc._extract_bio_link(f'{{"bio":"{host}/creator"}}') == (
        f"https://{host}/creator"
    )


def test_aggregator_remains_a_storefront_when_penetration_fails(monkeypatch):
    monkeypatch.setattr(bc, "_goto", lambda page, url: False)
    cand = {"external_url": "https://linktr.ee/creator"}
    bc._resolve_storefront(cand, object())
    assert cand["storefront_status"] == "confirmed_yes"
    assert cand["storefront_type"] == "链接聚合"
    assert cand["storefront_url"] == "https://linktr.ee/creator"


@pytest.mark.parametrize("status", ["confirmed_yes", "confirmed_no", "unknown"])
def test_stage2_never_rejects_only_for_storefront(monkeypatch, status):
    profile = {
        "codes": [],
        "is_private": False,
        "brand_account_type": "personal",
        "follower_count": 50_000,
        "core_niche_key": "skincare",
        "biography": "skincare creator",
    }
    monkeypatch.setattr(bc, "fetch_profile_browser", lambda page, handle: dict(profile))
    monkeypatch.setattr(
        bc,
        "_resolve_storefront",
        lambda cand, page: cand.update({"storefront_status": status}),
    )
    verdict = qualify_one(object(), {"handle": "candidate"}, load_config())
    assert verdict[0:2] == ("advance", "qualified")


def test_stage2_still_rejects_off_niche_after_storefront(monkeypatch):
    profile = {
        "codes": [],
        "is_private": False,
        "brand_account_type": "personal",
        "follower_count": 50_000,
        "core_niche_key": "other",
        "biography": "books and travel",
    }
    monkeypatch.setattr(bc, "fetch_profile_browser", lambda page, handle: dict(profile))
    monkeypatch.setattr(
        bc,
        "_resolve_storefront",
        lambda cand, page: cand.update({"storefront_status": "confirmed_no"}),
    )
    verdict = qualify_one(object(), {"handle": "candidate"}, load_config())
    assert verdict[0:2] == ("reject", "off_niche")


def test_semantic_requeue_is_exact_and_clears_old_terminal_fields(tmp_path):
    old_db = cc.DB
    cc.DB = tmp_path / "creator_cache.db"
    try:
        with cc._conn() as conn:
            for handle, reason in (("restore_me", "no_amazon_storefront"), ("keep_me", "off_niche")):
                conn.execute(
                    "INSERT INTO creator_profiles "
                    "(handle,status,reject_reason,stage_error,locked_at,final_pool,"
                    "discovery_batch,stage_json) VALUES (?,?,?,?,?,?,?,?)",
                    (
                        handle,
                        "rejected",
                        reason,
                        "old_error",
                        "2026-07-28T00:00:00",
                        "Exclude",
                        "B1",
                        json.dumps({"handle": handle, "final_pool": "Exclude"}),
                    ),
                )
        result = cc.requeue_machine_rejections(
            ["restore_me"], "no_amazon_storefront", batch_ids=["B1"]
        )
        assert result["requeued"] == 1
        with cc._conn() as conn:
            restored = conn.execute(
                "SELECT * FROM creator_profiles WHERE handle='restore_me'"
            ).fetchone()
            kept = conn.execute(
                "SELECT * FROM creator_profiles WHERE handle='keep_me'"
            ).fetchone()
        assert restored["status"] == "seed"
        assert restored["reject_reason"] is None
        assert restored["stage_error"] is None
        assert restored["locked_at"] is None
        assert restored["final_pool"] is None
        assert "final_pool" not in json.loads(restored["stage_json"])
        assert kept["status"] == "rejected"
        with pytest.raises(ValueError):
            cc.requeue_machine_rejections(
                ["keep_me"], "no_amazon_storefront", batch_ids=["B1"]
            )
    finally:
        cc.DB = old_db
