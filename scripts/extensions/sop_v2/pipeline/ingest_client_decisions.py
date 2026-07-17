"""读回客户在交付表里导出的决策 JSON，落库到 client_status（闭环最后一环）。

交付表(export_v2_html)最左『客户选择』列：客户点 合适/不合适/待定 + 填原因，导出
client_decisions_<batch>.json：{batch, exported_at, decisions:[{handle, verdict, reason, pool, score}]}。
本脚本把它落库：合适→promote_golden(approved)、不合适→mark_rejected(+原因)、待定→保留 pending 仅记录。
不自动改门槛/config（客户口径正交，只入负/正向库）。

用法：cd scripts && ../.venv/bin/python -m extensions.sop_v2.pipeline.ingest_client_decisions \
        --file client_decisions_SKIN3-20260717.json [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from extensions.sop_v2 import creator_cache as cc  # noqa: E402

_APPROVE = {"合适", "approve", "approved", "yes", "y"}
_REJECT = {"不合适", "reject", "rejected", "no", "n"}
_PENDING = {"待定", "pending", "maybe", "later"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True, help="客户导出的 client_decisions_*.json")
    ap.add_argument("--dry-run", action="store_true", help="只预览映射，不写库")
    args = ap.parse_args()

    data = json.loads(Path(args.file).read_text())
    batch = data.get("batch", "")
    decisions = data.get("decisions", [])
    if not decisions:
        print("✗ 文件里没有决策记录"); return 1

    print(f"客户决策回流：批次 {batch} · {len(decisions)} 条 · 导出于 {data.get('exported_at','')}"
          + ("（DRY-RUN 预览）" if args.dry_run else ""))
    tally = Counter()
    for d in decisions:
        h = (d.get("handle") or "").lstrip("@")
        v = (d.get("verdict") or "").strip().lower()
        v_raw = (d.get("verdict") or "").strip()
        reason = (d.get("reason") or "").strip()
        if not h:
            continue
        if v_raw in _APPROVE or v in _APPROVE:
            act = "approved"
            if not args.dry_run:
                cc.promote_golden(h, batch_id=batch)
                if reason:
                    cc.set_client_note(h, f"[合适] {reason}")   # 存原因备注
        elif v_raw in _REJECT or v in _REJECT:
            act = "rejected"
            if not args.dry_run:
                cc.mark_rejected(h, reason=reason or "客户判定不合适", batch_id=batch)
        elif v_raw in _PENDING or v in _PENDING:
            act = "pending"
            if not args.dry_run and reason:
                cc.set_client_note(h, f"[待定] {reason}")
        else:
            act = f"未知({v_raw})"
        tally[act] += 1
        print(f"  @{h:<26} {act:<10}" + (f" · {reason}" if reason else ""))

    print("\n汇总：" + " · ".join(f"{k} {n}" for k, n in tally.most_common()))
    if not args.dry_run:
        print("已落库 client_status（合适=approved 进金种子库 / 不合适=rejected 进负向库）。")
        print("后续：这些 client_status 会在下一轮 discovery 复用（approved 做 lookalike 种子、rejected 入黑名单）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
