"""④ 决策+交付：collected → decided + 五池。复用 run_v2.decide（不改其口径）。

Modash 补数（fake_pct/creator_country/top_audience_country）：PIPELINE_SPEC 坑B——
不补则 routing 的 modash_core_missing 把候选钉 Review、Include 恒空。--modash-enrich 走 CDP 补数
（尊重 export_only_shortlist）；无 Modash 会话时候选诚实落 Review（非 bug）。

用法：cd scripts && ../.venv/bin/python -m extensions.sop_v2.pipeline.stage4_decide \
        --batch-id SKINCARE-20260716 --track paid --out data/runs/B/decisions.json
    出表：../.venv/bin/python scripts/export_v2_xlsx.py --decisions data/runs/B/decisions.json ...
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from extensions.sop_v2 import creator_cache as cc, run_v2  # noqa: E402
from extensions.sop_v2.config import config_sha256, load_config  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch-id", required=True)
    ap.add_argument("--track", choices=["paid", "gifting"], required=True)
    ap.add_argument("--all-batches", action="store_true",
                    help="跨所有 batch 汇总有数据候选（默认仅本 batch）")
    ap.add_argument("--out", required=True)
    ap.add_argument("--xlsx", default=None, help="交付 XLSX 路径（默认与 --out 同目录 deliverable.xlsx）")
    ap.add_argument("--no-xlsx", action="store_true", help="只出 decisions.json，不出交付表")
    ap.add_argument("--generated-at", default=None, help="固定时间戳（可复现）")
    ap.add_argument("--modash-csv", default=None,
                    help="Modash Profile Report CSV：补假粉/受众/国家(解除 modash_core_missing→Include 才可能非空)")
    ap.add_argument("--manual-csv", default=None,
                    help="人工核验 CSV：raw_skin_grade/has_vo/paid_cpm(解除 raw_skin_or_vo_unverified→Include)")
    ap.add_argument("--modash-cdp", action="store_true",
                    help="驱动已登录 Modash Chrome(9222)读 show-profile 补假粉/受众/国家(每个约1 credit)")
    ap.add_argument("--modash-cap", type=int, default=0,
                    help="Modash 补数上限(省 credit)；默认读 config modash_budget.profile_per_round")
    ap.add_argument("--cdp", default="http://127.0.0.1:9222")
    args = ap.parse_args()
    cfg = load_config()

    # 客户铁律：抓过就纳入——拉所有有数据候选（非 seed），rejected 也进（Exclude 池写明原因，不 silently drop）
    scope = None if args.all_batches else [args.batch_id]
    cands = cc.export_all_with_data(scope)
    if not cands:
        print("✗ 无有数据候选（先跑 stage2/3）"); return 1
    active = [c for c in cands if c.get("_status") != "rejected"]     # 走完整 decide
    rejected = [c for c in cands if c.get("_status") == "rejected"]   # 直接归 Exclude 写原因
    for c in active:
        c.setdefault("campaign_track", args.track)
    scope_txt = "跨全部 batch" if args.all_batches else f"batch {args.batch_id}"
    print(f"纳入 {len(cands)}（{scope_txt}）：活跃 {len(active)}(qualified/collected/decided) + 机器淘汰 {len(rejected)}")

    if args.modash_cdp:
        from extensions.sop_v2 import gates
        from extensions.sop_v2.contracts import GateVerdict
        from extensions.sop_v2.pipeline.modash_cdp import enrich_via_cdp
        # 省 credit：只补 active 里通过"非 Modash 硬门槛"的候选(补了才够 Include)；
        # 已被硬淘汰或机器 reject 的，补 Modash 也白搭 → 不花这 credit。
        cap = args.modash_cap or cfg.get("modash_budget", {}).get("profile_per_round", 20)
        shortlist = [c for c in active
                     if gates.gate_summary(gates.evaluate_gates(c, cfg)) != GateVerdict.EXCLUDE]
        # 有橱窗优先、实算 ER 高优先（好苗子先补）
        shortlist.sort(key=lambda c: (c.get("storefront_status") == "confirmed_yes",
                                      c.get("real_er") or 0), reverse=True)
        shortlist = shortlist[:cap]
        disc = cfg.get("discovery", {})
        t = cfg["track"][args.track]
        lo, hi = ((t["min_followers"], t["max_followers"]) if args.track == "paid"
                  else (t["standard_min"], t["priority_max"]))
        filt = {"followers": {"min": lo, "max": hi},
                "engagementRate": {"min": disc.get("search_er_min", 0.015)}}
        print(f"Modash 补数(CDP)：shortlist {len(shortlist)}/{len(active)}（省 credit，上限 {cap}）…")
        cache_dir = str(Path(args.out).with_name("modash_raw"))  # 原始报告落盘→改解析器免重付费
        r = enrich_via_cdp(shortlist, disc.get("search_query", ""), filt, args.cdp, cache_dir=cache_dir)
        print(f"Modash 补数(CDP): 命中 {r.get('matched')}/{r.get('total')}"
              + (f"  ⚠ {r['error']}" if r.get("error") else ""))
    elif args.modash_csv:
        from extensions.sop_v2.pipeline.modash_enrich import enrich
        r = enrich(active, args.modash_csv)
        print(f"Modash 补数(CSV): 匹配 {r['matched']}/{r['total']}（CSV {r['csv_rows']} 行）")
    else:
        print("⚠ 未提供 Modash 补数：缺假粉/受众/国家 → 候选诚实落 Review(modash_core_missing)。")
    if args.manual_csv:
        from extensions.sop_v2.pipeline.modash_enrich import enrich_manual
        r = enrich_manual(active, args.manual_csv)
        print(f"人工核验回填(Raw Skin/VO/报价): 匹配 {r.get('matched')}/{r.get('total')}")

    # 活跃号走完整 decide；机器淘汰号直接归 Exclude 写原因（客户铁律：淘汰也要体现）
    decisions = [run_v2.decide(c, cfg) for c in active] + [run_v2.decide_rejected(c, cfg) for c in rejected]
    # 只推进"真正深采完"的（collected/decided）→ decided；qualified 留 qualified（可再深采）、rejected 留 rejected
    for c, d in zip(active, decisions):
        if c.get("_status") in ("collected", "decided"):
            cc.advance(c["handle"], "decided", {**c, "final_pool": d["final_pool"]})
    assert len(decisions) == len(cands), f"对账失败：决策 {len(decisions)} ≠ 候选 {len(cands)}（疑 silent drop）"

    batches = sorted({c.get("_discovery_batch") for c in cands if c.get("_discovery_batch")})
    out = {
        "generated_at": args.generated_at or time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "manifest": {"batch_id": args.batch_id, "batches": batches,
                     "sop_version": cfg.get("sop_version", ""),
                     "campaign_track": args.track, "config_sha256": config_sha256(None),
                     "candidate_count": len(decisions)},
        "candidates": decisions,
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    print(f"④ 决策完成 {len(decisions)} → {args.out}")
    for pool, n in Counter(d["final_pool"] for d in decisions).most_common():
        print(f"  {pool}: {n}")
    # 自动出交付 XLSX（一条命令直达交付表）
    if not args.no_xlsx:
        import export_v2_xlsx
        xlsx = args.xlsx or str(Path(args.out).with_name("deliverable.xlsx"))
        wb = export_v2_xlsx.build_workbook(out)
        wb.save(xlsx)
        print(f"   交付表 → {xlsx}")
    print("status:", cc.status_dist(args.batch_id))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
