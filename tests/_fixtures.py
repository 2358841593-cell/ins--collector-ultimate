"""共享测试夹具：一个"干净应 Include"的完整候选。"""


def clean_full(**kw):
    c = {
        "platform": "instagram",
        "campaign_track": "paid",
        "follower_count": 50000,
        "storefront_status": "confirmed_yes",
        "storefront_active": True,
        "storefront_maturity_score": 4,
        "creator_country": "US",
        "top_audience_country": "US",
        "fake_pct": 10.0,
        "general_er": 3.0,
        "sponsorship_saturation": 20.0,
        "real_er": 2.5,             # 实算 ER 达标（GATE-13 PASS）
        "brand_account_type": "personal",
        "shein_temu_partnership": False,
        "discovered_via": "modash_search",
        "valid_comments": 30,
        "raw_skin_grade": "A",
        "has_vo": True,
        "paid_cpm": 30.0,
        "target_countries_audience_pct": 60.0,
        "top_language_pct": 70.0,
        # 评分字段（满分向）
        "core_niche_key": "skincare",
        "amazon_finds_ratio": 45.0,
        "sku_categories_hit": 3,
        "organic_relevant_posts": 12,
        "ingredients_score": 3,
        "device_specs_score": 4,
        "skin_science_score": 2,
        "meets_er_benchmark": True,
        "high_intent_count": 6,
        "high_intent_ratio": 20.0,
        "low_quality_ratio": 10.0,
        "avg_reels_shares": None,   # → C5 N/A
        "elite_brand_hits": 2,
        "budget_tier_score": 3,
        "contact_availability": "email_or_form",
        "partnership_risk": "clear",
        # 9.5 特殊条件旗标
        "all_hard_gates_pass": True,
        "red_light_mask_and_vo": False,
        "high_intent_full_marks": True,
    }
    c.update(kw)
    return c
