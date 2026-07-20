#!/usr/bin/env python3
"""SOP V2 决策编排（P0-8）。

离线模式（默认，零 IG 请求，可复现）：
    python -m extensions.sop_v2.run_v2 --from-candidates cands.json \
        --track paid --batch-id B1 --out decisions.json

candidates JSON = [候选 dict, ...]（已由采集/合并层组装）。
本模块只做：gates → scoring → routing → 组装 v2-decisions（同输入必得同输出）。
时间戳/batch_id 由参数传入以保证可复现（不在内部取当前时间参与决策）。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from extensions.sop_v2 import gates, routing, scoring  # noqa: E402
from extensions.sop_v2.config import config_sha256, load_config  # noqa: E402
from extensions.sop_v2.contracts import GateVerdict  # noqa: E402


REASON_TEXT = {
    "followers_out_of_range": "粉丝档超出范围",
    "gifting_out_of_range": "Gifting 粉丝档超出范围",
    # stage2/3 机器淘汰原因（rejected 号进 Exclude 池写明）
    "private": "私密账号",
    "brand_account": "品牌/官方号（非个人创作者）",
    "no_amazon_storefront": "无 Amazon 橱窗（非本赛道导购）",
    "off_niche": "非目标赛道（护肤/美妆导购）",
    "fake_followers_high": "假粉 ≥ 25%",
    "general_er_low": "Modash General ER ≤ 2%",
    "real_er_low": "实算 ER 过低（僵尸/刷量嫌疑）",
    "real_er_borderline": "实算 ER 偏低待复核",
    "real_er_missing": "缺实算 ER（未登录/未取到赞评）",
    "sponsorship_saturated": "赞助饱和 > 40%",
    "sponsorship_borderline": "赞助 30–40% 待复核",
    "creator_country_not_target": "非目标国家",
    "country_mismatch": "创作者国家 ≠ 受众主国",
    "shein_temu_partnership": "SHEIN/Temu 合作史",
    "non_personal_creator": "非个人创作者（品牌号）",
    "medical_studio_default": "医生/诊所账号待客户批示",
    "storefront_unknown": "Storefront 状态未确认",
    "modash_core_missing": "缺第三方受众核验（假粉/国家/受众）",
    "comments_insufficient": "有效评论样本不足",
    "raw_skin_or_vo_unverified": "Raw Skin / VO 未人工核验",
    "paid_quote_missing": "缺实际报价",
    "audience_target_below_35": "目标受众合计 < 35%",
    "language_unmatched": "受众语言不匹配/缺失",
    "lifestyle_cap": "Lifestyle 赛道待补产品/护肤证据",
    "gifting_priority_pool": "Gifting 30K–50K 优秀候选优先复核",
    "graph_unaudited_cap": "图谱来源未完整审计",
}


def _humanize(codes):
    out = []
    for c in codes or []:
        base = c.split(":")[0]
        out.append(REASON_TEXT.get(base, REASON_TEXT.get(c, c)))
    # 去重保序
    seen, res = set(), []
    for x in out:
        if x not in seen:
            seen.add(x)
            res.append(x)
    return res


def decision_summary(pool: str, review_reasons, exclude_reasons, ai) -> str:
    if pool == "Exclude":
        r = _humanize(exclude_reasons or review_reasons)
        return "淘汰 · " + "；".join(r[:2]) if r else "淘汰"
    if pool.startswith("Include"):
        tail = "有 Storefront" if "With" in pool else "无 Storefront"
        return f"纳入（{tail}）· AI {ai}"
    r = _humanize(review_reasons)
    head = "优先复核" if pool == "Priority-Review" else "待复核"
    return f"{head} · " + "；".join(r[:2]) if r else head


def score_by_module(items) -> dict:
    out = {}
    for it in items:
        m = out.setdefault(it.module, {"earned": 0.0, "available": 0})
        if it.available is not None:
            m["available"] += it.available
            if it.earned is not None:
                m["earned"] += it.earned
    return out


def decide(cand: dict, cfg: dict) -> dict:
    gate_results = gates.evaluate_gates(cand, cfg)
    items = scoring.score_all(cand, cfg)
    summ = scoring.normalize(items)
    ai = scoring.apply_exceptional_cap(summ["ai_vetting_score"], cand, cfg)
    r = routing.route(cand, gate_results, summ, cfg)

    missing = list(r.get("review_reasons") or [])
    rec = dict(cand)
    rec.update({
        "final_pool": r["pool"].value,
        "review_reasons": r.get("review_reasons", []),
        "exclude_reasons": r.get("exclude_reasons", []),
        "missing_data": [m for m in missing if m in cfg["fixed_review"]["reasons"]],
        "tier_by": r["tier_by"],
        "normalized_total": summ["normalized_total"],
        "ai_vetting_score": ai,
        "score_by_module": score_by_module(items),
        "gate_results": [g.to_dict() for g in gate_results],
        "review_reasons_text": _humanize(r.get("review_reasons")),
        "exclude_reasons_text": _humanize(r.get("exclude_reasons")),
        "decision_summary": decision_summary(r["pool"].value, r.get("review_reasons"),
                                             r.get("exclude_reasons"), ai),
    })
    # storefront link 展示
    if cand.get("storefront_status") == "confirmed_yes":
        rec["amazon_storefront_link"] = cand.get("amazon_storefront_link") or cand.get("external_url")
    return rec


def decide_rejected(cand: dict, cfg: dict | None = None) -> dict:
    """机器淘汰号（stage2/3 已 reject，_reject_reason 携带原因）：直接归 Exclude 池并写明原因。
    不跑完整 decide——它们缺 Modash/深采数据，跑评分/门槛会误判。客户铁律：淘汰也要体现，
    带已抓浅扫数据（粉丝/橱窗/赛道）展示，让客户浏览判断，不 silently drop。"""
    reason = cand.get("_reject_reason") or "rejected"
    rec = dict(cand)
    txt = _humanize([reason])
    rec.update({
        "final_pool": "Exclude",
        "review_reasons": [],
        "exclude_reasons": [reason],
        "review_reasons_text": [],
        "exclude_reasons_text": txt,
        "tier_by": "stage_reject",
        "normalized_total": None,
        "ai_vetting_score": None,
        "decision_summary": "机器淘汰（发现/浅扫阶段）：" + "；".join(txt),
    })
    if cand.get("storefront_status") == "confirmed_yes":
        rec["amazon_storefront_link"] = cand.get("amazon_storefront_link") or cand.get("external_url")
    return rec


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-candidates", required=True, help="已组装候选 JSON 列表")
    ap.add_argument("--track", choices=["paid", "gifting"], required=True)
    ap.add_argument("--batch-id", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--config", default=None)
    ap.add_argument("--generated-at", default=None, help="固定时间戳（可复现用）；默认取当前")
    args = ap.parse_args()

    cfg = load_config(args.config)
    cands = json.loads(Path(args.from_candidates).read_text())
    for c in cands:
        c.setdefault("campaign_track", args.track)

    decisions = [decide(c, cfg) for c in cands]
    manifest = {
        "batch_id": args.batch_id,
        "sop_version": cfg.get("sop_version", ""),
        "campaign_track": args.track,
        "config_sha256": config_sha256(args.config),
        "candidate_count": len(decisions),
    }
    out = {
        "generated_at": args.generated_at or time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "manifest": manifest,
        "candidates": decisions,
    }
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    from collections import Counter
    dist = Counter(d["final_pool"] for d in decisions)
    print(f"决策完成 {len(decisions)} 候选 → {args.out}")
    for pool, n in dist.most_common():
        print(f"  {pool}: {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
