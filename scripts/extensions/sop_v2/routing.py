"""路由引擎（P0-7，五池互斥）。

优先级（§7 分流）：
1. 任一硬红线 gate EXCLUDE → Exclude。
2. 固定 Review 七项（高分不覆盖）→ Review。
3. Gifting 30K-50K 优秀池（硬红线与固定 Review 未触发）→ Priority Review。
4. Lifestyle 主赛道封顶 → Review。
5. 其余按 normalized_total 分层：>=75 Include / [65,75) Priority Review / [50,65) Review / <50 Exclude。
6. Include 按 storefront confirmed yes/no 拆双池。
每个候选恰好落一个池。
"""
from __future__ import annotations

from .contracts import GateVerdict, Pool


def collect_fixed_review_reasons(cand: dict, cfg: dict) -> list[str]:
    reasons = []
    core = cfg["modash_gates"]["core_fields"]
    if any(cand.get(f) is None for f in core):
        reasons.append("modash_core_missing")
    if cand.get("storefront_status") in (None, "unknown"):
        reasons.append("storefront_unknown")
    vc = cand.get("valid_comments")
    if vc is None or vc < cfg["comments"]["min_valid_sample"]:
        reasons.append("comments_insufficient")
    if cand.get("raw_skin_grade") in (None, "Pending", "C") or cand.get("has_vo") is None:
        # C 或无证据/VO 未确认 → Review（不淘汰）
        reasons.append("raw_skin_or_vo_unverified")
    if cand.get("campaign_track") == "paid" and cand.get("paid_cpm") is None:
        reasons.append("paid_quote_missing")
    ts = cand.get("target_countries_audience_pct")
    if ts is not None and ts < cfg["audience"]["target_share_partial"]:
        reasons.append("audience_target_below_35")
    lang = cand.get("top_language_pct")
    if lang is None or lang < cfg["audience"]["language_min_share"]:
        reasons.append("language_unmatched")
    return reasons


def route(cand: dict, gate_results: list, score_summary: dict, cfg: dict) -> dict:
    """返回 {pool, review_reasons, exclude_reasons, tier_by}。"""
    exclude = [g for g in gate_results if g.result == GateVerdict.EXCLUDE]
    if exclude:
        return {"pool": Pool.EXCLUDE,
                "exclude_reasons": [g.reason_code for g in exclude],
                "review_reasons": [], "tier_by": "hard_gate"}

    # track 未指定不允许打分定案
    if cand.get("campaign_track") not in ("paid", "gifting"):
        return {"pool": Pool.REVIEW, "exclude_reasons": [],
                "review_reasons": ["campaign_track_unspecified"], "tier_by": "guard"}

    fixed = collect_fixed_review_reasons(cand, cfg)
    gifting_priority = any(g.gate_id == "GATE-03" for g in gate_results)
    gate_reviews = [g.reason_code for g in gate_results if g.result == GateVerdict.REVIEW
                    and g.gate_id != "GATE-03"]

    if fixed:
        return {"pool": Pool.REVIEW, "exclude_reasons": [],
                "review_reasons": fixed + gate_reviews, "tier_by": "fixed_review"}

    if gifting_priority:
        return {"pool": Pool.PRIORITY_REVIEW, "exclude_reasons": [],
                "review_reasons": ["gifting_priority_pool"] + gate_reviews, "tier_by": "gifting_priority"}

    # Lifestyle 封顶 Review（有充分证据可提升，此处按当前证据）
    if cfg["niche_routing"].get("lifestyle_cap_review") and cand.get("core_niche_key") == "lifestyle" \
            and not cand.get("lifestyle_promoted"):
        return {"pool": Pool.REVIEW, "exclude_reasons": [],
                "review_reasons": ["lifestyle_cap"] + gate_reviews, "tier_by": "niche_cap"}

    # gate 级 REVIEW（如赞助边界）也不得进 Include
    if gate_reviews:
        return {"pool": Pool.REVIEW, "exclude_reasons": [],
                "review_reasons": gate_reviews, "tier_by": "gate_review"}

    # 分数分层（用 normalized_total）
    nt = score_summary["normalized_total"]
    a = cfg["ai_score"]
    if nt >= a["include_min"]:
        st = cand.get("storefront_status")
        pool = Pool.INCLUDE_WITH_STOREFRONT if st == "confirmed_yes" else Pool.INCLUDE_WITHOUT_STOREFRONT
        return {"pool": pool, "exclude_reasons": [], "review_reasons": [], "tier_by": "score"}
    if nt >= a["priority_min"]:
        return {"pool": Pool.PRIORITY_REVIEW, "exclude_reasons": [],
                "review_reasons": [f"score_{nt}"], "tier_by": "score"}
    if nt >= a["review_min"]:
        return {"pool": Pool.REVIEW, "exclude_reasons": [],
                "review_reasons": [f"score_{nt}"], "tier_by": "score"}
    return {"pool": Pool.EXCLUDE, "exclude_reasons": [f"low_score_{nt}"],
            "review_reasons": [], "tier_by": "score"}
