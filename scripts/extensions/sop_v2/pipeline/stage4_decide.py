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
    ap.add_argument("--cdp", default="http://127.0.0.1:9222")
    args = ap.parse_args()
    cfg = load_config()

    cands = cc.export_candidates("collected", args.batch_id)
    if not cands:
        print("✗ 无 collected 候选（先跑 stage3）"); return 1
    for c in cands:
        c.setdefault("campaign_track", args.track)

    if args.modash_cdp:
        from extensions.sop_v2.pipeline.modash_cdp import enrich_via_cdp
        print(f"Modash 补数(CDP)：逐个读 show-profile（约 {len(cands)} credits）…")
        r = enrich_via_cdp(cands, args.cdp)
        print(f"Modash 补数(CDP): 命中 {r.get('matched')}/{r.get('total')}"
              + (f"  ⚠ {r['error']}" if r.get("error") else ""))
    elif args.modash_csv:
        from extensions.sop_v2.pipeline.modash_enrich import enrich
        r = enrich(cands, args.modash_csv)
        print(f"Modash 补数(CSV): 匹配 {r['matched']}/{r['total']}（CSV {r['csv_rows']} 行）")
    else:
        print("⚠ 未提供 Modash 补数：缺假粉/受众/国家 → 候选诚实落 Review(modash_core_missing)，Include 恒空。")
    if args.manual_csv:
        from extensions.sop_v2.pipeline.modash_enrich import enrich_manual
        r = enrich_manual(cands, args.manual_csv)
        print(f"人工核验回填(Raw Skin/VO/报价): 匹配 {r.get('matched')}/{r.get('total')}")

    decisions = [run_v2.decide(c, cfg) for c in cands]
    for c, d in zip(cands, decisions):
        cc.advance(c["handle"], "decided", {**c, "final_pool": d["final_pool"]})

    out = {
        "generated_at": args.generated_at or time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "manifest": {"batch_id": args.batch_id, "sop_version": cfg.get("sop_version", ""),
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
