"""Modash 结构化搜索发现（替换 AI Search 只捞 6 个小号的老路）。

实测契约（2026-07-16）：POST /api/search/v2/instagram
  body = {skip, limit:6, search_origin:"lookalikes", query:<NL 描述>, filters:{...}}
  filters.followers = {min, max}；filters.engagementRate = {min}
  响应 {results:[{username, follower_count, engagement_rate, is_brand, is_private,
        creator_description(bio), account_category, ...}], total}
每次上限 6 → 用 skip 分页（0,6,12,…）累积；搜索列表**免费**（不耗 credit，只有开 report 才耗）。
响应已含粉丝/ER/品牌/bio/类目 → **发现阶段就预过滤**（排品牌/私密、要 amazon bio），产出高质量 seed。
"""
from __future__ import annotations

import json

_SEARCH_JS = """async (body) => {
  const r = await fetch('/api/search/v2/instagram', {method:'POST',
    headers:{'content-type':'application/json'}, body: JSON.stringify(body)});
  return {status: r.status, text: await r.text()};
}"""


def _find_modash(b):
    for c in b.contexts:
        for p in c.pages:
            if "modash.io" in (p.url or ""):
                return p
    return None


def _page(pg, query, filters, skip, limit=6):
    body = {"skip": skip, "limit": limit, "search_origin": "lookalikes",
            "query": query, "filters": filters}
    r = pg.evaluate(_SEARCH_JS, body)
    if r.get("status") != 200:
        return None
    try:
        return json.loads(r["text"]).get("results", [])
    except Exception:  # noqa: BLE001
        return []


def _seed(x: dict) -> dict:
    return {"handle": (x.get("username") or "").lstrip("@"),
            "followers": x.get("follower_count"),
            "er": round((x.get("engagement_rate") or 0) * 100, 2),
            "is_brand": bool(x.get("is_brand")),
            "is_private": bool(x.get("is_private")),
            "bio": x.get("creator_description") or "",
            "category": x.get("account_category")}


_AMAZON_BIO = ("amazon", "amzn", "storefront", "shop my", "ltk", "liketoknow", "linktr",
               "beacons", "founditon", "shopmy")


def _keep(s: dict, require_amazon_bio: bool) -> bool:
    """发现阶段预过滤：排品牌/私密；可选要 bio 提及 amazon/橱窗（免费提质）。"""
    if s["is_brand"] or s["is_private"]:
        return False
    if require_amazon_bio and not any(k in (s["bio"] or "").lower() for k in _AMAZON_BIO):
        return False
    return True


def discover(query: str, filters: dict, target: int = 120, max_pages: int = 80,
             require_amazon_bio: bool = True, cdp_url: str = "http://127.0.0.1:9222") -> dict:
    """结构化搜索 + skip 分页 + 预过滤 → 高质量 seed 列表。返回 {seeds, raw_scanned, filtered_out}。"""
    from playwright.sync_api import sync_playwright
    seeds, seen = [], set()
    scanned = kept = 0
    with sync_playwright() as pw:
        b = pw.chromium.connect_over_cdp(cdp_url)
        try:
            pg = _find_modash(b)
            if not pg:
                return {"error": "no_modash_tab", "seeds": []}
            for _ in range(2):
                pg.keyboard.press("Escape")
                pg.wait_for_timeout(300)
            for i in range(max_pages):
                res = _page(pg, query, filters, skip=i * 6)
                if not res:
                    break
                for x in res:
                    s = _seed(x)
                    h = s["handle"].lower()
                    if not h or h in seen:
                        continue
                    seen.add(h)
                    scanned += 1
                    if _keep(s, require_amazon_bio):
                        seeds.append(s)
                        kept += 1
                if kept >= target:
                    break
                pg.wait_for_timeout(400)   # 拟人节奏
        finally:
            b.close()
    return {"seeds": seeds, "raw_scanned": scanned, "kept": kept, "filtered_out": scanned - kept}
