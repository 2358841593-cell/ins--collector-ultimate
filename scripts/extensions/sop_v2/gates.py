"""硬门槛引擎（P0-5，GATE-01..12 + 可采集性）。

输入：合并后的候选 dict（见字段约定）+ config。输出：list[GateResult]。
硬红线 → EXCLUDE；专项固定 Review → REVIEW；通过 → PASS。
所有阈值取自 config，边界用半开区间语义。缺失字段不猜测：能判的判，判不了的交给 routing 的固定 Review。
"""
from __future__ import annotations

from .contracts import GateResult, GateVerdict


def _g(gid, verdict, observed=None, threshold=None, reason="", source="") -> GateResult:
    return GateResult(gate_id=gid, result=verdict, observed=observed,
                      threshold=threshold, reason_code=reason, source=source)


def gate_platform(cand, cfg):
    if cand.get("platform", "instagram") != "instagram":
        return _g("SCOPE-01", GateVerdict.EXCLUDE, cand.get("platform"), "instagram",
                  "non_instagram")
    return _g("SCOPE-01", GateVerdict.PASS, "instagram", "instagram")


def gate_collectability(cand, cfg):
    if cand.get("is_private"):
        return _g("COLLECT-01", GateVerdict.EXCLUDE, "private", None, "private_account", "instagram")
    if cand.get("collect_failed"):  # 重试耗尽后仍失败
        return _g("COLLECT-02", GateVerdict.REVIEW, "collect_failed", None,
                  "collect_retry_exhausted", "instagram")
    return None


def gate_followers(cand, cfg):
    track = cand.get("campaign_track")
    f = cand.get("follower_count")
    if f is None:
        return None  # 缺粉丝数交给固定 Review
    t = cfg["track"]
    if track == "paid":
        lo, hi = t["paid"]["min_followers"], t["paid"]["max_followers"]
        if lo <= f <= hi:
            return _g("GATE-01", GateVerdict.PASS, f, [lo, hi])
        return _g("GATE-01", GateVerdict.EXCLUDE, f, [lo, hi], f"followers_out_of_range:{f}")
    if track == "gifting":
        g = t["gifting"]
        if g["standard_min"] <= f < g["standard_max"]:
            return _g("GATE-02", GateVerdict.PASS, f, [g["standard_min"], g["standard_max"]], "gifting_standard")
        if g["priority_min"] <= f <= g["priority_max"]:
            return _g("GATE-03", GateVerdict.REVIEW, f, [g["priority_min"], g["priority_max"]],
                      "gifting_priority_pool")   # 优秀候选 → Priority Review
        return _g("GATE-02", GateVerdict.EXCLUDE, f, [g["standard_min"], g["priority_max"]],
                  f"gifting_out_of_range:{f}")
    return None  # track 未指定，交给 routing 报错


def gate_country(cand, cfg):
    c = cfg["country"]
    allowed = set(c["tier1"]) | set(c["tier2"])
    cc = cand.get("creator_country")
    tac = cand.get("top_audience_country")
    if cc is None or tac is None:
        return None  # 交固定 Review（modash_core_missing）
    if cc.upper() not in allowed:
        return _g("GATE-04", GateVerdict.EXCLUDE, cc, sorted(allowed), "creator_country_not_target", "modash")
    if c["require_creator_equals_top_audience"] and cc.upper() != tac.upper():
        return _g("GATE-05", GateVerdict.EXCLUDE, {"creator": cc, "top_audience": tac}, "equal",
                  "country_mismatch", "modash")
    return _g("GATE-04", GateVerdict.PASS, {"creator": cc, "top_audience": tac}, sorted(allowed))


def gate_fake(cand, cfg):
    v = cand.get("fake_pct")
    if v is None:
        return None
    thr = cfg["modash_gates"]["fake_pct_exclude_at"]
    if v >= thr:
        return _g("GATE-06", GateVerdict.EXCLUDE, v, f">={thr}", "fake_followers_high", "modash")
    return _g("GATE-06", GateVerdict.PASS, v, f"<{thr}", source="modash")


def gate_general_er(cand, cfg):
    """Modash General ER：客户放宽后只作参考，不再硬淘汰（reference_only=true）。"""
    v = cand.get("general_er")
    if v is None:
        return None
    mg = cfg["modash_gates"]
    if mg.get("general_er_reference_only"):
        return _g("GATE-07", GateVerdict.PASS, v, "reference", "modash_er_reference", "modash")
    thr = mg["general_er_exclude_at"]
    if v <= thr:
        return _g("GATE-07", GateVerdict.EXCLUDE, v, f"<={thr}", "general_er_low", "modash")
    return _g("GATE-07", GateVerdict.PASS, v, f">{thr}", source="modash")


def gate_real_er(cand, cfg):
    """实算 ER（前 N 帖赞评/粉丝）= 硬门槛。放宽后只挡僵尸/刷量；缺数据 → 固定 Review 不误杀。"""
    r = cfg.get("real_er")
    if not r:
        return None
    v = cand.get("real_er")
    if v is None:
        if r.get("missing_is_review"):
            return _g("GATE-13", GateVerdict.REVIEW, None, f">={r['review_below']}",
                      "real_er_missing", "instagram")
        return None
    # 客户 2026-07-16：进了深采的红人不硬排除——实算 ER 低只作 Review 浮现（交客户判断），不 Exclude。
    if v < r["review_below"]:
        code = "real_er_low" if v < r["exclude_below"] else "real_er_borderline"
        return _g("GATE-13", GateVerdict.REVIEW, v, f">={r['review_below']}", code, "instagram")
    return _g("GATE-13", GateVerdict.PASS, v, f">={r['review_below']}", source="instagram")


def gate_sponsorship(cand, cfg):
    v = cand.get("sponsorship_saturation")
    if v is None:
        return None
    s = cfg["sponsorship"]
    if v < s["healthy_below"]:
        return _g("GATE-08", GateVerdict.PASS, v, f"<{s['healthy_below']}", source="instagram")
    if s["review_lo"] <= v <= s["review_hi"]:
        return _g("GATE-08", GateVerdict.REVIEW, v, f"[{s['review_lo']},{s['review_hi']}]",
                  "sponsorship_borderline", "instagram")
    return _g("GATE-08", GateVerdict.EXCLUDE, v, f">{s['review_hi']}", "sponsorship_saturated", "instagram")


def gate_shein_temu(cand, cfg):
    # 只在合作语境命中；日期未知但命中仍 Exclude；普通提及不淘汰
    if cand.get("shein_temu_partnership") is True:
        return _g("GATE-09", GateVerdict.EXCLUDE, True, "any_partnership",
                  "shein_temu_partnership", cand.get("shein_temu_source", ""))
    return None


def gate_brand_account(cand, cfg):
    t = cand.get("brand_account_type")  # personal | brand | medical
    b = cfg["brand_account"]
    if t == "brand" and b["exclude_non_personal"]:
        return _g("GATE-10", GateVerdict.EXCLUDE, t, "personal", "non_personal_creator", "instagram")
    if t == "medical":
        return _g("GATE-10", GateVerdict.REVIEW, t, "personal", "medical_studio_default", "instagram")
    return None


def gate_storefront(cand, cfg):
    s = cand.get("storefront_status")  # confirmed_yes | confirmed_no | unknown
    if s == "unknown" or s is None:
        return _g("GATE-11", GateVerdict.REVIEW, s, "confirmed", "storefront_unknown", "browser")
    return _g("GATE-11", GateVerdict.PASS, s, "confirmed", source="browser")


def gate_graph(cand, cfg):
    via = str(cand.get("discovered_via", ""))
    if cfg["graph"]["cap_review"] and ("graph" in via or "lookalike_multi" in via) \
            and not cand.get("fully_audited"):
        return _g("GATE-12", GateVerdict.REVIEW, via, "audited", "graph_unaudited_cap")
    return None


GATES = [
    gate_platform, gate_collectability, gate_followers, gate_country, gate_fake,
    gate_general_er, gate_real_er, gate_sponsorship, gate_shein_temu, gate_brand_account,
    gate_storefront, gate_graph,
]


def evaluate_gates(cand: dict, cfg: dict) -> list[GateResult]:
    out = []
    for fn in GATES:
        r = fn(cand, cfg)
        if r is not None:
            out.append(r)
    return out


def gate_summary(results: list[GateResult]) -> GateVerdict:
    """任一 EXCLUDE → EXCLUDE；否则任一 REVIEW → REVIEW；否则 PASS。"""
    if any(r.result == GateVerdict.EXCLUDE for r in results):
        return GateVerdict.EXCLUDE
    if any(r.result == GateVerdict.REVIEW for r in results):
        return GateVerdict.REVIEW
    return GateVerdict.PASS
