"""Modash 补数（stage4）：驱动已登录 Chrome 拉 show-profile 报告，补**完整** audience 数据。

实测契约（2026-07-16）——两步纯 fetch，绕开脆弱的点击/模态框：
  1. 重跑 discovery 搜索(POST /api/search/v2/instagram)分页，建 {handle → servicePlatformId} 映射
     （搜索响应里 servicePlatformId 才是 show-profile 要的哈希 ID；user_id 数字→400，serviceSdId→404）。
  2. GET /api/discovery/show-profile/<servicePlatformId>?allowOutdated=true&originatingFrom=ai_search
     &useInHouseEngagementRate=true → 完整报告 JSON（假粉/ER/创作国 + 受众国家/城市/年龄/性别/语言/兴趣 + 合作品牌）。

关键：报告拿到但某字段空 = **Modash 本身没有**（交付标"Modash无"）；报告没拿到 = 未补数（标"待补数"）。
只读，不点 Save/Bulk（列表/报告不额外耗 credit，report 消耗按 Modash 计）。防串号：username 不匹配弃。
"""
from __future__ import annotations

import json

_CC = {
    "united states": "US", "canada": "CA", "united kingdom": "UK", "germany": "DE",
    "italy": "IT", "france": "FR", "spain": "ES", "netherlands": "NL", "belgium": "BE",
    "switzerland": "CH", "sweden": "SE", "australia": "AU", "brazil": "BR", "mexico": "MX",
    "india": "IN", "indonesia": "ID", "portugal": "PT", "ireland": "IE", "austria": "AT",
    "poland": "PL", "turkey": "TR", "philippines": "PH", "united arab emirates": "AE",
}
_TARGET = {"US", "CA", "UK", "DE", "IT", "FR", "ES", "NL", "BE", "CH", "SE"}

_SEARCH_JS = """async (a) => {
  const r = await fetch('/api/search/v2/instagram', {method:'POST',
    headers:{'content-type':'application/json'},
    body: JSON.stringify({skip:a.skip, limit:6, search_origin:'lookalikes', query:a.query, filters:a.filters})});
  return await r.text();
}"""
_SHOW_JS = """async (spid) => {
  const r = await fetch('/api/discovery/show-profile/'+spid
    +'?allowOutdated=true&originatingFrom=ai_search&useInHouseEngagementRate=true');
  return {status: r.status, text: await r.text()};
}"""


def _cc(name):
    return _CC.get((name or "").strip().lower())


def _find_modash(b):
    for c in b.contexts:
        for p in c.pages:
            if "modash.io" in (p.url or ""):
                return p
    return None


def resolve_platform_ids(pg, handles, query, filters, max_pages=40):
    """重跑 discovery 搜索分页，建 {handle(lower) → servicePlatformId}。"""
    want = {h.lstrip("@").lower() for h in handles}
    idmap = {}
    for i in range(max_pages):
        try:
            res = json.loads(pg.evaluate(_SEARCH_JS, {"skip": i * 6, "query": query, "filters": filters}))
        except Exception:  # noqa: BLE001
            break
        results = res.get("results") or []
        if not results:
            break
        for x in results:
            u = (x.get("username") or "").lower()
            if u and u not in idmap:
                idmap[u] = x.get("servicePlatformId")
        if want <= set(idmap):
            break
        pg.wait_for_timeout(300)
    return idmap


def fetch_report(pg, spid):
    if not spid:
        return None
    try:
        r = pg.evaluate(_SHOW_JS, str(spid))
        if r.get("status") != 200:
            return None
        return json.loads(r["text"])
    except Exception:  # noqa: BLE001
        return None


def parse_report(data: dict, handle: str) -> dict | None:
    pd = (((data or {}).get("profile") or {}).get("profileData")) or {}
    prof = pd.get("profile") or {}
    aud = pd.get("audience") or {}
    if (prof.get("username") or "").lower() != handle.lstrip("@").lower():
        return None                                    # 防串号
    out = {"modash_report": True}                      # 标记：报告已拿到（区分"Modash无" vs "未补数"）
    cred = aud.get("credibility")
    if cred is not None:
        out["fake_pct"] = round((1 - cred) * 100, 1)
    er = prof.get("engagementRate")
    if er is not None:
        out["general_er"] = round(er * 100, 2)
    loc = ((pd.get("location") or {}).get("country") or {})
    if loc.get("name"):
        out["creator_country"] = _cc(loc.get("name")) or loc.get("name")

    def _pct_list(items, key="name"):
        return [{"name": x.get(key) or x.get("code"), "pct": round((x.get("weight") or 0) * 100, 1)}
                for x in (items or [])]

    geos = aud.get("geoCountries") or []
    if geos:
        out["top_audience_country"] = _cc(geos[0].get("name"))
        out["audience_countries"] = _pct_list(geos[:5])
        tgt = sum(g.get("weight", 0) for g in geos if _cc(g.get("name")) in _TARGET)
        out["target_countries_audience_pct"] = round(tgt * 100, 1)
    if aud.get("geoCities"):
        out["audience_cities"] = _pct_list(aud["geoCities"][:5])
    if aud.get("ages"):
        out["audience_ages"] = [{"name": a.get("code"), "pct": round((a.get("weight") or 0) * 100, 1)}
                                for a in aud["ages"]]
    if aud.get("genders"):
        out["audience_genders"] = [{"name": g.get("code"), "pct": round((g.get("weight") or 0) * 100, 1)}
                                   for g in aud["genders"]]
    langs = aud.get("languages") or []
    if langs:
        out["top_language"] = langs[0].get("code")
        out["top_language_pct"] = round((langs[0].get("weight") or 0) * 100, 1)
        out["audience_languages"] = _pct_list(langs[:4])
    if aud.get("interests"):
        out["audience_interests"] = [i.get("name") for i in aud["interests"][:6] if i.get("name")]
    # 合作品牌"有什么拿什么"
    brands, seen = [], set()
    for p in (pd.get("sponsoredPosts") or []):
        for cand in ([p.get("sponsor")] if p.get("sponsor") else []) + (p.get("mentions") or []):
            bb = (cand or "").lstrip("@")
            if bb and bb.lower() not in seen and len(bb) < 40:
                seen.add(bb.lower())
                brands.append(bb)
    if brands:
        out["brand_collaborations"] = brands[:12]
    return out


def enrich_via_cdp(cands: list[dict], query: str, filters: dict,
                   cdp_url: str = "http://127.0.0.1:9222") -> dict:
    """对候选补数：重搜建 servicePlatformId 映射 → fetch show-profile → 解析完整 audience 写回。"""
    from playwright.sync_api import sync_playwright
    matched = 0
    handles = [c.get("handle") for c in cands if c.get("handle")]
    with sync_playwright() as pw:
        b = pw.chromium.connect_over_cdp(cdp_url)
        try:
            pg = _find_modash(b)
            if not pg:
                return {"error": "no_modash_tab", "matched": 0, "total": len(cands)}
            idmap = resolve_platform_ids(pg, handles, query, filters)
            for cand in cands:
                h = (cand.get("handle") or "").lstrip("@")
                spid = idmap.get(h.lower())
                data = fetch_report(pg, spid) if spid else None
                d = parse_report(data, h) if data else None
                if d:
                    for k, v in d.items():
                        if v is not None and cand.get(k) is None:
                            cand[k] = v
                    matched += 1
                    print(f"  Modash ✓ @{h}: 假粉{d.get('fake_pct')}% 创作国{d.get('creator_country')} "
                          f"受众{[(x['name'],x['pct']) for x in (d.get('audience_countries') or [])[:2]]} "
                          f"语言{[x['name'] for x in (d.get('audience_languages') or [])[:1]]} "
                          f"品牌{len(d.get('brand_collaborations') or [])}")
                else:
                    print(f"  Modash ✗ @{h}（spid={'有' if spid else '无'}，报告未拿到 → 标待补数）")
        finally:
            b.close()
    return {"matched": matched, "total": len(cands)}
