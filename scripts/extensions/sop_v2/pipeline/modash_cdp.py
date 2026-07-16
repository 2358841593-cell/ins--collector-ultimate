"""Modash CDP 补数：驱动用户已登录 Chrome（port 9222）读 show-profile 报告，
回填假粉/受众/国家/ER —— 解除 routing 的 modash_core_missing（否则 Include 恒空）。

数据源：Modash 报告 API `/api/discovery/show-profile/<userId>`（点 View profile 触发，每个约 1 credit）。
字段路径（实测 2026-07-16）：
  假粉 fake_pct   = (1 - profileData.audience.credibility) * 100
  Modash ER       = profileData.profile.engagementRate * 100
  创作者国        = profileData.location.country.name
  受众主国        = profileData.audience.geoCountries[0].name
  目标受众合计%   = Σ geoCountries.weight（命中 SOP 目标国）* 100
  主语言占比      = profileData.audience.languages[0].weight * 100
只读，不点 Save/Bulk。补不到的字段留 None（诚实，交 Review），绝不编造。
"""
from __future__ import annotations

# Modash 国名 → SOP 2 字码（config country tier1/tier2 口径）
_CC = {
    "united states": "US", "canada": "CA", "united kingdom": "UK", "germany": "DE",
    "italy": "IT", "france": "FR", "spain": "ES", "netherlands": "NL", "belgium": "BE",
    "switzerland": "CH", "sweden": "SE", "australia": "AU", "brazil": "BR", "mexico": "MX",
    "india": "IN", "indonesia": "ID", "portugal": "PT", "ireland": "IE", "austria": "AT",
    "poland": "PL", "turkey": "TR", "philippines": "PH", "united arab emirates": "AE",
}
_TARGET = {"US", "CA", "UK", "DE", "IT", "FR", "ES", "NL", "BE", "CH", "SE"}
_MARKET_LANG = {"en", "de", "it", "fr", "es", "nl", "sv"}   # 目标市场主语言


def _cc(name):
    return _CC.get((name or "").strip().lower())


def parse_report(data: dict, handle: str) -> dict | None:
    pd = (((data.get("profile") or {}).get("profileData")) or {})
    prof = pd.get("profile") or {}
    aud = pd.get("audience") or {}
    if (prof.get("username") or "").lower() != handle.lstrip("@").lower():
        return None                                    # 防串号：用户名不匹配直接弃
    out = {}
    cred = aud.get("credibility")
    if cred is not None:
        out["fake_pct"] = round((1 - cred) * 100, 1)
    er = prof.get("engagementRate")
    if er is not None:
        out["general_er"] = round(er * 100, 2)
    loc = ((pd.get("location") or {}).get("country") or {})
    if loc.get("name"):
        out["creator_country"] = _cc(loc.get("name"))
    geos = aud.get("geoCountries") or []
    if geos:
        out["top_audience_country"] = _cc(geos[0].get("name"))
        tgt = sum(g.get("weight", 0) for g in geos if _cc(g.get("name")) in _TARGET)
        out["target_countries_audience_pct"] = round(tgt * 100, 1)
    langs = aud.get("languages") or []
    if langs:
        out["top_language"] = langs[0].get("code")
        out["top_language_pct"] = round(langs[0].get("weight", 0) * 100, 1)
    # 合作品牌"有什么拿什么"：从 sponsoredPosts 的 sponsor + @mentions 收集
    brands = []
    seen = set()
    for p in (pd.get("sponsoredPosts") or []):
        cands = []
        if p.get("sponsor"):
            cands.append(str(p["sponsor"]).lstrip("@"))
        for m in (p.get("mentions") or []):
            if m:
                cands.append(str(m).lstrip("@"))
        for b in cands:
            k = b.lower()
            if b and k not in seen and len(b) < 40:
                seen.add(k)
                brands.append(b)
    if brands:
        out["brand_collaborations"] = brands[:12]
    out["modash_report"] = True
    return out


def read_profile(pg, handle: str) -> dict | None:
    """AI Search 搜 handle → JS 点 View profile → 抓 show-profile 报告 → 解析。"""
    h = handle.lstrip("@")
    for _ in range(2):
        pg.keyboard.press("Escape")
        pg.wait_for_timeout(400)
    try:
        pg.get_by_text("AI Search", exact=True).first.click(timeout=3000)
        pg.wait_for_timeout(800)
    except Exception:  # noqa: BLE001
        pass
    ed = pg.query_selector("[contenteditable='true']")
    if not ed:
        return None
    try:
        ed.click(timeout=3000)
    except Exception:  # noqa: BLE001
        pg.evaluate("() => {const e=document.querySelector('[contenteditable=true]'); e&&e.focus();}")
    pg.keyboard.press("Meta+A")
    pg.keyboard.press("Backspace")
    pg.keyboard.type(h, delay=15)
    pg.wait_for_timeout(500)
    pg.keyboard.press("Enter")
    pg.wait_for_timeout(6000)
    try:
        with pg.expect_response(
                lambda r: "discovery/show-profile" in r.url and r.status == 200, timeout=15000) as ri:
            pg.evaluate(r"""() => {
              const el=[...document.querySelectorAll('button,a,div,span')].find(e=>
                /view profile/i.test((e.innerText||'').trim()) && (e.innerText||'').length<30);
              if(el) el.dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true,view:window}));
            }""")
        data = ri.value.json()
    except Exception:  # noqa: BLE001
        return None
    return parse_report(data, h)


def enrich_via_cdp(cands: list[dict], cdp_url: str = "http://127.0.0.1:9222") -> dict:
    """对候选逐个 Modash CDP 补数（None 不覆盖已有）。返回统计。"""
    from playwright.sync_api import sync_playwright
    matched = 0
    with sync_playwright() as pw:
        b = pw.chromium.connect_over_cdp(cdp_url)
        try:
            pg = None
            for c in b.contexts:
                for p in c.pages:
                    if "modash.io" in (p.url or ""):
                        pg = p
                        break
                if pg:
                    break
            if not pg:
                return {"error": "no_modash_tab", "matched": 0, "total": len(cands)}
            for cand in cands:
                d = None
                try:
                    d = read_profile(pg, cand.get("handle") or "")
                except Exception:  # noqa: BLE001
                    d = None
                if d:
                    for k, v in d.items():
                        if v is not None and cand.get(k) is None:
                            cand[k] = v
                    matched += 1
                    print(f"  Modash ✓ @{cand.get('handle')}: fake={d.get('fake_pct')} "
                          f"er={d.get('general_er')} 创作国={d.get('creator_country')} 受众国={d.get('top_audience_country')}")
                else:
                    print(f"  Modash ✗ @{cand.get('handle')}（读不到，留 None → Review）")
                try:
                    pg.keyboard.press("Escape")
                    pg.wait_for_timeout(1000)
                except Exception:  # noqa: BLE001
                    pass
        finally:
            b.close()
    return {"matched": matched, "total": len(cands)}
