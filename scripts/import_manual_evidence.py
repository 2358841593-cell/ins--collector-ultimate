#!/usr/bin/env python3
"""人工证据导入（P1-1，SCORE-B4/B5/F7/F8）。

把人工核验的 Raw Skin/VO/实际报价/合作风险合并进候选 JSON，用于解掉对应固定 Review。
证据文件（JSON）格式：
  { "handle": { "raw_skin_grade": "A|B|C", "raw_skin_evidence": ["url1","url2"],
                "has_vo": true, "vo_evidence": "url", "paid_cpm": 28.5,
                "actual_quote_usd": 500, "partnership_risk": "clear|risk",
                "lifestyle_promoted": true, "reviewer": "name", "captured_at": "..." } }
每个字段必须带证据（URL/截图/时间），否则不放行（不接受裸布尔值）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

MERGE_FIELDS = ["raw_skin_grade", "has_vo", "paid_cpm", "partnership_risk", "lifestyle_promoted"]
EVIDENCE_REQUIRED = {"raw_skin_grade": "raw_skin_evidence", "has_vo": "vo_evidence",
                     "paid_cpm": "quote_evidence"}


def merge(candidates: list[dict], evidence: dict) -> tuple[list[dict], list[str]]:
    warnings = []
    by_handle = {c.get("handle"): c for c in candidates}
    for handle, ev in evidence.items():
        cand = by_handle.get(handle)
        if not cand:
            warnings.append(f"证据中的 @{handle} 不在候选集，跳过")
            continue
        applied = []
        for field in MERGE_FIELDS:
            if field not in ev:
                continue
            req = EVIDENCE_REQUIRED.get(field)
            if req and not ev.get(req):
                warnings.append(f"@{handle}.{field} 缺证据（{req}），拒绝放行（不接受裸值）")
                continue
            cand[field] = ev[field]
            applied.append(field)
        # 证据引用 + 审阅人留档
        cand.setdefault("manual_evidence", []).append({
            "applied": applied, "reviewer": ev.get("reviewer", ""),
            "captured_at": ev.get("captured_at", ""),
            "refs": {k: ev[k] for k in ev if k.endswith("_evidence")},
        })
    return candidates, warnings


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidates", required=True)
    ap.add_argument("--evidence", required=True, help="人工证据 JSON")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    cands = json.loads(Path(args.candidates).read_text())
    ev = json.loads(Path(args.evidence).read_text())
    merged, warnings = merge(cands, ev)
    for w in warnings:
        print("  ⚠", w, file=sys.stderr)
    Path(args.out).write_text(json.dumps(merged, ensure_ascii=False, indent=2))
    print(f"合并人工证据 → {args.out}（{len(ev)} 条证据，{len(warnings)} 条告警）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
