"""评分引擎（P0-6，A-F 六模块 + N/A 归一化 + AI Score + 9.5 封顶）。

Normalized Total = earned / applicable * 100（N/A 项从分母移除，不送分不扣分）。
AI Vetting Score = round(normalized_total/10, 1)，仅作展示；分层判定用 normalized_total。
所有分档取自 config。子项证据不足时按项目口径记 N/A 或 0（区别见各子项注释）。
"""
from __future__ import annotations

from .contracts import ScoreItem


def _band_score(value, bands, key, default=0):
    """bands 为 [{key: 阈值, 'score': n}, ...]，按半开区间匹配。
    key='min' → value>=阈值取第一档；key='max' → value<阈值取第一档。"""
    if value is None:
        return None
    for b in bands:
        if key == "min" and value >= b["min"]:
            return b["score"]
        if key == "max" and value < b["max"]:
            return b["score"]
    return default


def _si(module, item, earned, available, reason=""):
    return ScoreItem(module=module, item=item, earned=earned, available=available, reason=reason)


def score_A(cand, cfg):
    a = cfg["scoring"]["A"]
    items = []
    # A1 核心垂类
    niche = cand.get("core_niche_key")  # skincare/beauty_device/beauty_wellness/lifestyle/other
    v = a["core_niche"].get(niche, 0) if niche else None
    items.append(_si("A", "core_niche", v, 5, f"niche={niche}"))
    # A2 导购型占比
    ratio = cand.get("amazon_finds_ratio")
    items.append(_si("A", "amazon_finds", _band_score(ratio, a["amazon_finds_bands"], "min"), 5,
                     f"ratio={ratio}"))
    # A3 SKU/竞品/场景 每类 1 分封顶 3
    sku = cand.get("sku_categories_hit")  # 命中类数 0-3
    items.append(_si("A", "sku", min(sku, a["sku_max"]) if sku is not None else None, a["sku_max"],
                     f"categories={sku}"))
    # A4 organic
    org = cand.get("organic_relevant_posts")
    items.append(_si("A", "organic", _band_score(org, a["organic_bands"], "min"), 2, f"organic={org}"))
    return items


def score_B(cand, cfg):
    b = cfg["scoring"]["B"]
    is_device = cand.get("core_niche_key") == "beauty_device"   # 只有美容仪赛道才考设备规格
    items = []
    items.append(_si("B", "ingredients", cand.get("ingredients_score"), b["ingredients_max"]))
    # B2 设备规格：非美容仪赛道(Amazon 护肤导购) → N/A（不适用，从分母移除，不拖分）
    if is_device:
        items.append(_si("B", "device_specs", cand.get("device_specs_score"), b["device_specs_max"]))
    else:
        items.append(_si("B", "device_specs", None, None, "not_device_niche_na"))
    items.append(_si("B", "skin_science", cand.get("skin_science_score"), b["skin_science_max"]))
    # B4 Raw Skin：人工核验项。客户 2026-07-16：人工部分不作 Include 要求 → 未核验记 N/A（不拖分），
    # 已核验(A/B/C) 才按档送分。
    grade = cand.get("raw_skin_grade")
    if grade in b["raw_skin"]:
        items.append(_si("B", "raw_skin", b["raw_skin"][grade], 4, f"grade={grade}"))
    else:
        items.append(_si("B", "raw_skin", None, None, "unverified_na"))
    # B5 VO：人工确认。未确认 → N/A（不拖分）；确认了才送分
    vo = cand.get("has_vo")   # True/False/None(pending)
    if vo is None:
        items.append(_si("B", "vo", None, None, "unverified_na"))
    else:
        items.append(_si("B", "vo", b["vo_score"] if vo else 0, b["vo_score"], f"vo={vo}"))
    return items


def score_C(cand, cfg):
    c = cfg["scoring"]["C"]
    items = []
    # C1 IG 互动率达标
    meets = cand.get("meets_er_benchmark")
    items.append(_si("C", "ig_er", (c["ig_er_score"] if meets else 2) if meets is not None else None,
                     c["ig_er_score"], f"meets={meets}"))
    # C2 Modash General ER：客户已把 Modash ER 降为参考（本赛道普遍<2%）→ 评分也 N/A（不拖分）。
    # 保留旧口径可回退（reference_only=false 时按 >2% 送分）。
    if cfg["modash_gates"].get("general_er_reference_only"):
        items.append(_si("C", "modash_er", None, None, "modash_er_reference_na"))
    else:
        ger = cand.get("general_er")
        items.append(_si("C", "modash_er", c["modash_er_score"] if (ger is not None and ger > 2.0) else
                         (0 if ger is not None else None), c["modash_er_score"]))
    # C3 高意图评论（双条件满分）
    cnt = cand.get("high_intent_count")
    ratio = cand.get("high_intent_ratio")
    if cnt is None or ratio is None:
        hi = None
    elif cnt >= cfg["comments"]["high_intent_min_count"] and ratio >= cfg["comments"]["high_intent_min_ratio"]:
        hi = c["high_intent_full"]
    elif ratio >= 5.0:
        hi = c["high_intent_partial"]   # [5%,15%) 或（≥15% 但 <5 条）→ 3
    else:
        hi = 0
    items.append(_si("C", "high_intent", hi, c["high_intent_full"], f"count={cnt} ratio={ratio}"))
    # C4 低质评论分档
    lq = cand.get("low_quality_ratio")
    if lq is None:
        lq_s = None
    elif lq >= cfg["comments"]["low_quality_zero_review_at"]:
        lq_s = 0
    else:
        lq_s = _band_score(lq, cfg["comments"]["low_quality_bands"], "max", default=0)
    items.append(_si("C", "low_quality", lq_s, c["low_quality_score_max"], f"lq={lq}"))
    # C5 Save/Share/DM：缺失 → N/A（不扣分不进 Review，客户覆盖）
    sh = cand.get("avg_reels_shares")
    if sh is None and c.get("save_share_missing_is_na", True):
        items.append(_si("C", "save_share", None, None, "missing_na"))   # available None → N/A
    else:
        items.append(_si("C", "save_share", cand.get("save_share_score", 0), c["save_share_score"]))
    return items


def score_D(cand, cfg):
    d = cfg["scoring"]["D"]
    items = []
    st = cand.get("storefront_status")
    sf = {"confirmed_yes": d["storefront_confirm"]["yes"],
          "confirmed_no": d["storefront_confirm"]["no"]}.get(st)
    items.append(_si("D", "storefront", sf, 4, f"status={st}"))
    # D2 成熟度：确认无 Storefront 时 N/A
    if st == "confirmed_no":
        items.append(_si("D", "storefront_maturity", None, None, "no_storefront_na"))
    else:
        mat = cand.get("storefront_maturity_score")
        items.append(_si("D", "storefront_maturity", mat, d["storefront_maturity"]))
    # D4 赞助健康度
    sat = cand.get("sponsorship_saturation")
    if sat is None:
        sp = None
    elif sat < 30.0:
        sp = d["sponsorship_health"]["healthy"]
    elif sat <= 40.0:
        sp = d["sponsorship_health"]["borderline"]
    else:
        sp = 0
    items.append(_si("D", "sponsorship_health", sp, 3, f"sat={sat}"))
    # D5 Elite Brand（Omnilux/CurrentBody/Therabody 等设备品牌合作史）：
    # 非美容仪赛道(Amazon 护肤导购)不适用 → N/A（不拖分）；仅美容仪赛道且有命中数才计。
    is_device = cand.get("core_niche_key") == "beauty_device"
    elite = cand.get("elite_brand_hits")
    if not is_device or elite is None:
        items.append(_si("D", "elite_brand", None, None, "not_device_niche_na"))
    else:
        eb = min(elite * d["elite_brand_each"], d["elite_brand_cap"])
        if cand.get("red_light_mask_and_vo"):
            eb = d["elite_brand_cap"]
        items.append(_si("D", "elite_brand", eb, d["elite_brand_cap"], f"hits={elite}"))
    return items


def score_E(cand, cfg):
    e = cfg["scoring"]["E"]
    items = []
    fake = cand.get("fake_pct")
    items.append(_si("E", "fake", _band_score(fake, e["fake_bands"], "max"), 6, f"fake={fake}"))
    cc, tac = cand.get("creator_country"), cand.get("top_audience_country")
    cm = e["country_match_score"] if (cc and tac and cc.upper() == tac.upper()) else \
        (0 if (cc and tac) else None)
    items.append(_si("E", "country_match", cm, e["country_match_score"]))
    ts = cand.get("target_countries_audience_pct")
    if ts is None:
        tsc = None
    elif ts >= 50.0:
        tsc = e["target_share"]["full"]
    elif ts >= 35.0:
        tsc = e["target_share"]["partial"]
    else:
        tsc = 0
    items.append(_si("E", "target_share", tsc, 4, f"pct={ts}"))
    # E4 年龄/性别恒 N/A
    items.append(_si("E", "age_gender", None, None, "client_no_requirement_na"))
    # E5 语言
    lang = cand.get("top_language_pct")
    lsc = e["language_score"] if (lang is not None and lang >= 50.0) else (0 if lang is not None else None)
    items.append(_si("E", "language", lsc, e["language_score"], f"lang={lang}"))
    return items


def score_F(cand, cfg):
    f = cfg["scoring"]["F"]
    items = []
    # 客户 2026-07-17：CPM 计算公式待补，**本轮整个经济性/触达模块不计入评分**——全 N/A（从分母移除，
    # 不拖分）。以后补了 CPM 公式，把 config [scoring.F] defer_this_round 关掉即恢复。
    if f.get("defer_this_round"):
        for it in ("cpm", "budget_tier", "contact", "risk"):
            items.append(_si("F", it, None, None, "deferred_pending_cpm_formula"))
        return items
    track = cand.get("campaign_track")
    # F1 CPM：Modash 无历史报价、需联系红人才知 → 缺报价记 N/A（不作 Include 要求、不拖分）；
    # 有报价(人工填/未来数据源)才按档送分。Gifting 恒 N/A。
    cpm = cand.get("paid_cpm")
    if (track == "gifting" and f.get("gifting_cpm_is_na", True)) or cpm is None:
        items.append(_si("F", "cpm", None, None, "no_quote_na"))
    else:
        if cpm <= f["cpm_threshold_pass"]:
            cpm_s = f["cpm_full"]
        elif cpm <= f["cpm_threshold_exclude"]:
            cpm_s = f["cpm_review"]
        else:
            cpm_s = 0
        items.append(_si("F", "cpm", cpm_s, f["cpm_full"], f"cpm={cpm}"))
    items.append(_si("F", "budget_tier", cand.get("budget_tier_score"), f["budget_tier_score"]))
    contact = cand.get("contact_availability")  # email_or_form/dm_only/none
    items.append(_si("F", "contact", f["contact"].get(contact) if contact else None, 2))
    risk = cand.get("partnership_risk")  # clear/unknown/risk
    rsc = {"clear": f["risk"]["clear"], "risk": 0}.get(risk)  # unknown → None → Review
    items.append(_si("F", "risk", rsc, 2, f"risk={risk}"))
    return items


def score_all(cand, cfg) -> list[ScoreItem]:
    items = []
    for fn in (score_A, score_B, score_C, score_D, score_E, score_F):
        items.extend(fn(cand, cfg))
    return items


def normalize(items: list[ScoreItem]) -> dict:
    """earned/applicable*100；N/A（available None）不进分母。缺 earned 的可用项按 0 计入分母
    但需上游用固定 Review 拦截（缺核心证据不应走到打分定案）。"""
    earned = sum(i.earned for i in items if i.available is not None and i.earned is not None)
    applicable = sum(i.available for i in items if i.available is not None)
    normalized = round(earned / applicable * 100, 2) if applicable else 0.0
    return {
        "earned": round(earned, 2),
        "applicable": applicable,
        "normalized_total": normalized,
        "ai_vetting_score": round(normalized / 10, 1),
    }


def exceptional_ok(cand) -> bool:
    """9.5+ 特殊封顶六条件（docx §13 严口径）：硬门槛全过 + Elite/红光史 + VO +
    成熟 Storefront + 高意图满分 + 赞助<30%。"""
    return all([
        cand.get("all_hard_gates_pass"),
        (cand.get("elite_brand_hits") or 0) > 0 or cand.get("red_light_mask_and_vo"),
        cand.get("has_vo") is True,
        cand.get("storefront_status") == "confirmed_yes" and cand.get("storefront_active"),
        cand.get("high_intent_full_marks"),
        (cand.get("sponsorship_saturation") is not None and cand["sponsorship_saturation"] < 30.0),
    ])


def apply_exceptional_cap(normalized_ai: float, cand, cfg) -> float:
    floor = cfg["ai_score"]["exceptional_floor"]
    cap = cfg["ai_score"]["exceptional_cap"]
    if normalized_ai >= floor and not exceptional_ok(cand):
        return cap
    return normalized_ai
