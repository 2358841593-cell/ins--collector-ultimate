#!/usr/bin/env python3
"""候选组装（发现 → 采集 → 派生字段）：Modash handle 池 + BrowserCollector → 候选 JSON。

读 search_pool_import/modash_search 产出的 handle 池，对每个 handle 用登录态浏览器
采 profile，派生 storefront_status / core_niche_key / general_er 等，输出 run_v2 可吃的
候选 JSON。Modash 补数字段（fake/country/audience）此处不填 → 下游按固定 Review 处理。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import browser_collect  # noqa: E402
from extensions.sop_v2 import content as content_mod  # noqa: E402
from extensions.sop_v2.config import load_config  # noqa: E402

_CFG = load_config()
PENETRATE = False   # 由 CLI --penetrate 打开（Storefront 穿透较慢）
COMMENTS = False    # 由 CLI --comments 打开（评论采集较慢）

AGG = ("linktr.ee", "beacons", "ltk", "liketoknow", "shopmy", "komi.io", "stan.store",
       "linkin.bio", "milkshake", "flowpage")
AMAZON = ("amazon.com/shop", "amzn.to", "amazon storefront", "/shop/")

NICHE_KW = [
    ("beauty_device", ["led", "red light", "redlight", "device", "wavelength", "nm", "irradiance",
                        "near infrared", "光疗", "面罩"]),
    ("skincare", ["skincare", "skin care", "derma", "retinol", "niacinamide", "acne", "esthetic",
                  "facial", "护肤", "farmac", "salud y belleza"]),
    ("beauty_wellness", ["beauty", "makeup", "wellness", "美妆", "belleza"]),
    ("lifestyle", ["lifestyle", "home", "decor", "mom", "family", "生活", "家居"]),
]


def _er_to_float(s):
    if not s:
        return None
    m = re.search(r"([\d.]+)", str(s))
    return float(m.group(1)) if m else None


def derive_storefront(prof):
    links = list(prof.get("bio_links") or [])
    ext = prof.get("external_url")
    if ext:
        links.append(ext)
    joined = " ".join(links).lower()
    for a in AMAZON:
        if a in joined:
            amazon_link = next((l for l in links if any(x in l.lower() for x in ("amazon", "amzn"))), ext)
            return "confirmed_yes", amazon_link
    if any(g in joined for g in AGG):
        return "unknown", None      # 聚合页，需浏览器穿透
    if not links:
        return "confirmed_no", None
    return "unknown", None


def derive_niche(prof):
    text = f"{prof.get('biography','')} {prof.get('category','')} {prof.get('full_name','')}".lower()
    for key, kws in NICHE_KW:
        if any(k in text for k in kws):
            return key
    return "other"


def assemble_one(handle, modash_rec, account, headless=True):
    prof = browser_collect.fetch_profile(account, handle, headless=headless)
    st, amazon_link = derive_storefront(prof)
    cand = {
        "handle": handle,
        "full_name": prof.get("full_name"),
        "profile_url": prof.get("profile_url"),
        "follower_count": prof.get("follower_count"),
        "is_private": prof.get("is_private"),
        "is_verified": prof.get("is_verified"),
        "category": prof.get("category"),
        "biography": prof.get("biography"),
        "external_url": prof.get("external_url"),
        "bio_links": prof.get("bio_links"),
        "storefront_status": st,
        "amazon_storefront_link": amazon_link,
        "core_niche_key": derive_niche(prof),
        "general_er": _er_to_float(modash_rec.get("er")),
        "brand_account_type": "personal",
        "discovery_source": "modash_" + (modash_rec.get("search_mode") or "search"),
        "discovered_via": "modash_search",
        "_profile_source": prof.get("_source"),
        # Modash 补数字段留空 → 固定 Review（modash_core_missing）
        "fake_pct": None, "creator_country": None, "top_audience_country": None,
        "target_countries_audience_pct": None, "top_language_pct": None,
        # 评论/视觉/报价留空 → 固定 Review（诚实）；内容信号从 posts 派生
        "valid_comments": None, "raw_skin_grade": None, "has_vo": None, "paid_cpm": None,
        "posts": prof.get("posts"),
    }
    cand.update(content_mod.derive_content_signals(cand, _CFG))
    if PENETRATE and cand.get("storefront_status") in ("unknown", None):
        from extensions.sop_v2 import storefront_verify as sv
        sv.resolve_candidate(cand, headless=True)
    if COMMENTS:
        _collect_comments(cand, account, headless)
    cand.pop("posts", None)   # 内容信号已派生，posts 不进候选（体积/隐私）
    return cand


def _collect_comments(cand: dict, account: str, headless: bool):
    """采样 Top2+Recent2 帖抓评论并分析（量小控成本）。"""
    from extensions.sop_v2 import comments as cmt
    posts = cand.get("posts") or []
    if not posts:
        return
    by_eng = sorted(posts, key=lambda p: (p.get("like_count") or 0) + (p.get("comment_count") or 0),
                    reverse=True)[:2]
    by_recent = sorted(posts, key=lambda p: p.get("taken_at") or 0, reverse=True)[:2]
    seen, sample = set(), []
    for p in by_eng + by_recent:
        pk = p.get("pk")
        if pk and pk not in seen:
            seen.add(pk)
            sample.append(p)
    texts = []
    per_post = _CFG["comments"]["per_post_max"]
    for p in sample:
        cs = browser_collect.fetch_comments(account, p.get("pk"), p.get("code") or "",
                                            max_n=per_post, headless=headless)
        texts.extend(c["text"] for c in cs)
        time.sleep(2.0)
    res = cmt.analyze(texts)
    cand["valid_comments"] = res["valid_comments"]
    cand["comments_analyzed"] = res["comments_analyzed"]
    cand["high_intent_count"] = res["high_intent_count"]
    cand["high_intent_ratio"] = res["high_intent_ratio"]
    cand["low_quality_ratio"] = res["low_quality_ratio"]
    cand["high_intent_snippets"] = res["top_intent"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pool", required=True, help="modash handle 池 JSON")
    ap.add_argument("--account", required=True, help="用哪个登录态 profile 采集")
    ap.add_argument("--out", required=True)
    ap.add_argument("--limit", type=int, default=0, help="只采前 N 个（0=全部）")
    ap.add_argument("--penetrate", action="store_true", help="对未确认 Storefront 做浏览器穿透")
    ap.add_argument("--comments", action="store_true", help="采样帖子抓评论并分析")
    ap.add_argument("--show-head", action="store_true")
    args = ap.parse_args()
    global PENETRATE, COMMENTS
    PENETRATE = args.penetrate
    COMMENTS = args.comments

    pool = json.loads(Path(args.pool).read_text())
    recs = pool.get("candidates", pool)
    if args.limit:
        recs = recs[:args.limit]

    cands = []
    for i, rec in enumerate(recs):
        h = (rec.get("handle") or "").lstrip("@")
        if not h:
            continue
        rec.setdefault("search_mode", pool.get("search_mode"))
        print(f"  [{i+1}/{len(recs)}] 采集 @{h} …")
        try:
            cands.append(assemble_one(h, rec, args.account, headless=not args.show_head))
        except Exception as e:  # noqa: BLE001
            print(f"    采集失败：{type(e).__name__}: {e} → 标记 collect_failed")
            cands.append({"handle": h, "collect_failed": True,
                          "discovery_source": "modash_search", "campaign_track": None})
        time.sleep(3.0)  # 拟人停顿

    Path(args.out).write_text(json.dumps(cands, ensure_ascii=False, indent=2))
    print(f"组装 {len(cands)} 候选 → {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
