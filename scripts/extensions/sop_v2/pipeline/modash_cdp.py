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


def _pct_list(items, key="name"):
    return [{"name": x.get(key) or x.get("code"), "pct": round((x.get("weight") or 0) * 100, 1)}
            for x in (items or [])]


def _parse_audience_block(aud: dict) -> dict:
    """解析一个 audience 块（followers 或 likers 通用）：拿 Modash 给的全部维度。"""
    o = {}
    cred = aud.get("credibility")
    if cred is not None:
        o["fake_pct"] = round((1 - cred) * 100, 1)
    geos = aud.get("geoCountries") or []
    if geos:
        o["top_audience_country"] = _cc(geos[0].get("name"))
        o["audience_countries"] = _pct_list(geos[:5])
        tgt = sum(g.get("weight", 0) for g in geos if _cc(g.get("name")) in _TARGET)
        o["target_countries_audience_pct"] = round(tgt * 100, 1)
    if aud.get("geoCities"):
        o["audience_cities"] = _pct_list(aud["geoCities"][:5])
    if aud.get("ages"):
        o["audience_ages"] = [{"name": a.get("code"), "pct": round((a.get("weight") or 0) * 100, 1)}
                              for a in aud["ages"]]
    if aud.get("genders"):
        o["audience_genders"] = [{"name": g.get("code"), "pct": round((g.get("weight") or 0) * 100, 1)}
                                 for g in aud["genders"]]
    if aud.get("gendersPerAge"):
        o["audience_genders_per_age"] = [
            {"age": g.get("code"), "female": round((g.get("female") or 0) * 100, 1),
             "male": round((g.get("male") or 0) * 100, 1)} for g in aud["gendersPerAge"]]
    langs = aud.get("languages") or []
    if langs:
        o["top_language"] = langs[0].get("name") or langs[0].get("code")
        o["top_language_pct"] = round((langs[0].get("weight") or 0) * 100, 1)
        o["audience_languages"] = _pct_list(langs[:4])
    if aud.get("interests"):
        o["audience_interests"] = [i.get("name") for i in aud["interests"][:8] if i.get("name")]
    if aud.get("hashtags"):
        o["audience_hashtags"] = [{"name": h.get("tag"), "pct": round((h.get("weight") or 0) * 100, 1)}
                                  for h in aud["hashtags"][:6]]
    if aud.get("mentions"):
        o["audience_mentions"] = [{"name": m.get("tag"), "pct": round((m.get("weight") or 0) * 100, 1)}
                                  for m in aud["mentions"][:6]]
    at = aud.get("audienceTypes")
    if at:  # 真人/网红/水军/机器人拆解——最硬的粉丝质量信号
        o["audience_types"] = {
            "real_people": round((at.get("realPeople") or 0) * 100, 1),
            "influencers": round((at.get("influencers") or 0) * 100, 1),
            "mass_followers": round((at.get("massFollowers") or 0) * 100, 1),
            "suspicious": round((at.get("suspiciousMassFollowers") or 0) * 100, 1),
            "bots": round((at.get("bots") or 0) * 100, 1)}
    if aud.get("notable") is not None:
        o["notable_pct"] = round(aud["notable"] * 100, 1)
    if aud.get("audienceReachability"):
        o["audience_reachability"] = [{"name": r.get("code"), "pct": round((r.get("weight") or 0) * 100, 1)}
                                      for r in aud["audienceReachability"]]
    return o


def parse_report(data: dict, handle: str) -> dict | None:
    """把 show-profile 报告里 Modash 能给的**全部**信息拿出来（一次 credit 榨干）。"""
    pd = (((data or {}).get("profile") or {}).get("profileData")) or {}
    prof = pd.get("profile") or {}
    if (prof.get("username") or "").lower() != handle.lstrip("@").lower():
        return None                                    # 防串号
    out = {"modash_report": True}                      # 标记：报告已拿到（区分"Modash无" vs "未补数"）

    # ── 受众画像（followers）：全维度 ──
    out.update(_parse_audience_block(pd.get("audience") or {}))
    # ── 点赞者画像（likers，比 followers 更难造假）：单独命名空间 ──
    likers = _parse_audience_block(pd.get("audienceLikers") or {})
    if "fake_pct" in likers:
        out["likers_fake_pct"] = likers["fake_pct"]
    if likers.get("audience_countries"):
        out["likers_countries"] = likers["audience_countries"]
        out["likers_top_country"] = likers.get("top_audience_country")
    if likers.get("audience_genders"):
        out["likers_genders"] = likers["audience_genders"]
    if likers.get("audience_ages"):
        out["likers_ages"] = likers["audience_ages"]

    # ── 创作者本人档 ──
    er = prof.get("engagementRate")
    if er is not None:
        out["general_er"] = round(er * 100, 2)
    for src, dst in (("fullname", "creator_fullname"), ("gender", "creator_gender"),
                     ("isVerified", "creator_verified"), ("postsCount", "posts_count"),
                     ("avgLikes", "avg_likes"), ("avgComments", "avg_comments"),
                     ("avgReelsPlays", "avg_reels_plays")):
        if prof.get(src) is not None:
            out[dst] = prof.get(src)
    if pd.get("accountType"):
        out["account_type"] = pd.get("accountType")
    if pd.get("contactsHasEmail") is not None:
        out["contacts_has_email"] = bool(pd.get("contactsHasEmail"))
    loc = ((pd.get("location") or {}).get("country") or {})
    if loc.get("name"):
        out["creator_country"] = _cc(loc.get("name")) or loc.get("name")

    # ── 跨平台账号（TikTok/YouTube/Linktree…）──
    sa = pd.get("socialAccounts") or {}
    if isinstance(sa, dict):
        social = {k: (v.get("url") if isinstance(v, dict) else v) for k, v in sa.items() if v}
        if social:
            out["social_accounts"] = social

    # ── 增长趋势 ──
    stats = pd.get("stats") or {}
    fg = ((stats.get("followers") or {}).get("compared"))
    if fg is not None:
        out["followers_growth_pct"] = round(fg * 100, 2)

    # ── 合作品牌 + 赞助帖样例（带链接/互动，"有什么拿什么"）──
    brands, seen, sp_samples = [], set(), []
    for p in (pd.get("sponsoredPosts") or []):
        for cand in ([p.get("sponsor")] if p.get("sponsor") else []) + (p.get("mentions") or []):
            bb = (cand or "").lstrip("@")
            if bb and bb.lower() not in seen and len(bb) < 40:
                seen.add(bb.lower())
                brands.append(bb)
        if p.get("url"):
            spon = p.get("sponsor")
            sp_samples.append({"url": p.get("url"),
                               "sponsor": spon if isinstance(spon, str) else None,
                               "likes": p.get("likes"), "comments": p.get("comments")})
    if brands:
        out["brand_collaborations"] = brands[:12]
    if sp_samples:
        out["sponsored_post_samples"] = sp_samples[:6]
    return out


def _cache_path(cache_dir, handle):
    import re
    from pathlib import Path
    safe = re.sub(r"[^a-z0-9_.-]", "_", handle.lower())
    return Path(cache_dir) / f"{safe}.json"


def enrich_via_cdp(cands: list[dict], query: str, filters: dict,
                   cdp_url: str = "http://127.0.0.1:9222", cache_dir: str | None = None) -> dict:
    """对候选补数：重搜建 servicePlatformId 映射 → fetch show-profile → 解析完整 audience 写回。

    cache_dir：原始报告落盘目录。命中缓存则**免 credit 重解**（改进解析器后无需重付费）；
    未命中才 fetch（耗 1 credit）并存盘。一次 credit 榨干、且只付一次。
    """
    from pathlib import Path
    from playwright.sync_api import sync_playwright
    if cache_dir:
        Path(cache_dir).mkdir(parents=True, exist_ok=True)
    matched = from_cache = 0

    def _apply(cand, data, h):
        d = parse_report(data, h) if data else None
        if not d:
            return False
        for k, v in d.items():
            if v is not None and cand.get(k) is None:
                cand[k] = v
        print(f"  Modash ✓ @{h}: 假粉{d.get('fake_pct')}%(点赞者{d.get('likers_fake_pct')}%) "
              f"真人{(d.get('audience_types') or {}).get('real_people')}% 创作国{d.get('creator_country')} "
              f"受众{[(x['name'],x['pct']) for x in (d.get('audience_countries') or [])[:2]]} "
              f"语言{d.get('top_language')} 品牌{len(d.get('brand_collaborations') or [])} 字段{len(d)}")
        return True

    # 1) 先吃缓存（免 credit）
    need = []
    for cand in cands:
        h = (cand.get("handle") or "").lstrip("@")
        cp = _cache_path(cache_dir, h) if cache_dir else None
        if cp and cp.exists():
            try:
                data = json.loads(cp.read_text())
            except Exception:  # noqa: BLE001
                data = None
            if data and _apply(cand, data, h):
                matched += 1
                from_cache += 1
                continue
        need.append(cand)
    if not need:
        print(f"  全部命中本地缓存（0 credit）: {matched}/{len(cands)}")
        return {"matched": matched, "total": len(cands), "from_cache": from_cache}

    # 2) 剩余的才连浏览器 fetch（耗 credit），并存盘
    handles = [c.get("handle") for c in need if c.get("handle")]
    with sync_playwright() as pw:
        b = pw.chromium.connect_over_cdp(cdp_url)
        try:
            pg = _find_modash(b)
            if not pg:
                return {"error": "no_modash_tab", "matched": matched, "total": len(cands),
                        "from_cache": from_cache}
            idmap = resolve_platform_ids(pg, handles, query, filters)
            for cand in need:
                h = (cand.get("handle") or "").lstrip("@")
                spid = idmap.get(h.lower())
                data = fetch_report(pg, spid) if spid else None
                if data and cache_dir:
                    try:
                        _cache_path(cache_dir, h).write_text(json.dumps(data, ensure_ascii=False))
                    except Exception:  # noqa: BLE001
                        pass
                if _apply(cand, data, h):
                    matched += 1
                else:
                    print(f"  Modash ✗ @{h}（spid={'有' if spid else '无'}，报告未拿到 → 标待补数）")
        finally:
            b.close()
    return {"matched": matched, "total": len(cands), "from_cache": from_cache}
