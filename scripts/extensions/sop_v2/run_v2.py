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
    })
    # storefront link 展示
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
