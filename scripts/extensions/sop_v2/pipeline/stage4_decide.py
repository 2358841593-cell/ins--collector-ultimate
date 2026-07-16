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
    ap.add_argument("--generated-at", default=None, help="固定时间戳（可复现）")
    ap.add_argument("--modash-enrich", action="store_true", help="对将 Include 候选走 Modash CDP 补数")
    args = ap.parse_args()
    cfg = load_config()

    cands = cc.export_candidates("collected", args.batch_id)
    if not cands:
        print("✗ 无 collected 候选（先跑 stage3）"); return 1
    for c in cands:
        c.setdefault("campaign_track", args.track)

    if args.modash_enrich:
        # Modash CDP 补数占位：需已登录 Chrome。未补则候选诚实落 Review（modash_core_missing）。
        print("⚠ --modash-enrich 需已登录 Modash 的 Chrome；本版补数为占位，缺则 Review。")

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
    print("status:", cc.status_dist(args.batch_id))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
