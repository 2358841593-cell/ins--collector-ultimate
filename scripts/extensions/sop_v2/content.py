"""内容信号派生（从 BrowserCollector posts → 评分输入字段）。

赞助饱和、导购型占比、SKU/成分/设备/皮肤科学命中、互动率达标。
关键词表借鉴现有 discover.py 口径；产品/竞品/场景词从 config discovery 组取。
窗口：赞助/内容近 15 帖（首屏约 12 帖够初判，完整 30 帖待 B0-3 分页）。
"""
from __future__ import annotations

PRODUCT_REC = ["amazon find", "amazon storefront", "amazon must", "link in bio", "shop my",
               "linked it", "use code", "discount code", "must have", "must-have", "obsessed with",
               "holy grail", "repurchase", "haul", "restock", "linktree", "storefront"]
SPONSOR = ["#ad", "#sponsored", "paid partnership", "sponsored", "#gifted", "gifted by",
           "ambassador", "#partner", "in collaboration with", "commissionable"]
INGREDIENTS = ["retinol", "niacinamide", "ceramide", "hyaluronic", "vitamin c", "peptide",
               "salicylic", "azelaic", "skin barrier", "glycolic"]
DEVICE_SPECS = ["wavelength", " nm", "irradiance", "red light", "near infrared", "led mask",
                "led ", "light therapy", "joules"]
SKIN_SCIENCE = ["acne", "hyperpigmentation", "collagen", "sensitive skin", "dermatologist",
                "fine lines", "wrinkle", "cell turnover", "rosacea", "melasma"]


NICHE_KW = [
    ("beauty_device", ["led", "red light", "redlight", "device", "wavelength", "nm", "irradiance",
                       "near infrared", "光疗", "面罩", "microcurrent", "gua sha"]),
    ("skincare", ["skincare", "skin care", "derma", "retinol", "niacinamide", "acne", "esthetic",
                  "facial", "护肤", "farmac", "salud y belleza", "serum", "moisturizer", "spf",
                  "hyaluronic", "cuidado de la piel", "piel"]),
    ("beauty_wellness", ["beauty", "makeup", "wellness", "美妆", "belleza", "maquillaje", "glow"]),
    ("lifestyle", ["lifestyle", "home", "decor", "mom", "family", "生活", "家居", "vlog", "grwm"]),
]


def derive_niche(*texts) -> str:
    """从 bio/全名/caption 文本判主赛道。"""
    blob = " ".join(t for t in texts if t).lower()
    for key, kws in NICHE_KW:
        if any(k in blob for k in kws):
            return key
    return "other"


def _hits(text, terms):
    t = text.lower()
    return [k for k in terms if k in t]


def _distinct_hits(captions, terms):
    seen = set()
    for c in captions:
        for k in _hits(c, terms):
            seen.add(k)
    return len(seen)


def derive_content_signals(cand: dict, cfg: dict) -> dict:
    posts = cand.get("posts") or []
    followers = cand.get("follower_count") or 0
    out = {}
    if not posts:
        return out

    captions = [(p.get("caption_text") or "") for p in posts]
    recent15 = posts[:15]
    cap15 = [(p.get("caption_text") or "") for p in recent15]

    # 赞助饱和（近 15 帖）
    sponsored = sum(1 for c in cap15 if _hits(c, SPONSOR))
    out["sponsorship_saturation"] = round(sponsored / len(cap15) * 100, 1) if cap15 else None

    # 导购型占比
    rec_posts = sum(1 for c in captions if _hits(c, PRODUCT_REC))
    out["amazon_finds_ratio"] = round(rec_posts / len(captions) * 100, 1) if captions else None

    # SKU/竞品/场景 每类 1 分（0-3）
    disc = cfg.get("discovery", {})
    cats = 0
    if _distinct_hits(captions, disc.get("product_keywords", [])):
        cats += 1
    if _distinct_hits(captions, disc.get("brand_seeds", [])):
        cats += 1
    if _distinct_hits(captions, disc.get("scene_keywords", [])):
        cats += 1
    out["sku_categories_hit"] = cats

    # organic 稳定性：非赞助且含产品/护肤相关的帖数
    organic = sum(1 for c in cap15 if not _hits(c, SPONSOR) and
                  (_hits(c, PRODUCT_REC) or _hits(c, INGREDIENTS) or _hits(c, DEVICE_SPECS)))
    out["organic_relevant_posts"] = organic

    # 专业度：成分/设备/皮肤科学词类命中，按上限截断
    out["ingredients_score"] = min(_distinct_hits(captions, INGREDIENTS), 3)
    out["device_specs_score"] = min(_distinct_hits(captions, DEVICE_SPECS), 4)
    out["skin_science_score"] = min(_distinct_hits(captions, SKIN_SCIENCE), 2)

    # 互动率达标（Reels vs Static 分开，按档基准）
    if followers > 0:
        reels = [p for p in posts if p.get("media_type") == 2]
        static = [p for p in posts if p.get("media_type") != 2]

        def er(ps):
            if not ps:
                return None
            avg = sum((p.get("like_count") or 0) + (p.get("comment_count") or 0) for p in ps) / len(ps)
            return round(avg / followers * 100, 2)

        bm = cfg["scoring"]["C"]["er_benchmark"]
        tier_mid = followers >= 100000
        reels_er, static_er = er(reels), er(static)
        out["reels_er"] = reels_er
        out["static_er"] = static_er
        meets = False
        if tier_mid:
            meets = (reels_er is not None and reels_er >= bm["mid"]) or \
                    (static_er is not None and static_er >= bm["mid"])
        else:
            meets = (reels_er is not None and reels_er >= bm["micro_reels"]) or \
                    (static_er is not None and static_er >= bm["micro_static"])
        out["meets_er_benchmark"] = meets
    return out
