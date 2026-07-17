"""采集完整性监控 + 回头补采（客户 2026-07-17：抖动失败记下来，回头补，不完整再出来）。

真实重跑教训：代理抖动/限流会让深采"跑了但啥也没拿到"——网格有 12 帖 code 却 0 帖采到、
或有评论却抽 0 条。这类静默不完整不会报错、照样进 collected，交付表只剩空白。
本工具把它们揪出来、记下原因，并可一键退回 qualified 等下一轮 stage3 重采。

用法：
    # 只看监控（不改库）
    cd scripts && ../.venv/bin/python -m extensions.sop_v2.pipeline.audit_collect --batch-id SKIN3-20260717
    # 回头补：把不完整的退回 qualified，然后重跑 stage3 即可
    ... --batch-id SKIN3-20260717 --requeue
    ... --batch-id SKIN3-20260717 --requeue --only "帖子全失败"   # 只补某类失败
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from extensions.sop_v2 import creator_cache as cc  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch-id", default=None, help="限定批次；缺省看全部")
    ap.add_argument("--requeue", action="store_true",
                    help="把不完整/失败的号退回 qualified 等重采（清深采字段，保留浅扫+Modash）")
    ap.add_argument("--only", default=None, help="只处理原因含该关键词的（如 '帖子全失败'）")
    args = ap.parse_args()

    items = cc.incomplete_items(args.batch_id)
    if args.only:
        items = [x for x in items if any(args.only in r for r in x["reasons"])]

    scope = f"批次 {args.batch_id}" if args.batch_id else "全部批次"
    if not items:
        print(f"✓ {scope}：采集完整，无失败/不完整项")
        return 0

    print(f"⚠ {scope}：{len(items)} 个采集不完整/失败\n")
    for x in items:
        print(f"  @{x['handle']:<28} [{x['status']}] {'；'.join(x['reasons'])}")

    # 原因归类看板（哪类失败最多 → 指导修哪里）
    print("\n原因分布：")
    tally = Counter(r.split("(")[0] for x in items for r in x["reasons"])
    for reason, n in tally.most_common():
        print(f"  {reason}: {n}")

    if args.requeue:
        n = cc.requeue_for_recollect([x["handle"] for x in items])
        print(f"\n✓ 已退回 {n} 个 → qualified（深采字段已清，浅扫/Modash 数据保留）")
        print("  下一步：重跑 stage3 即可补采")
        print(f"    ../.venv/bin/python -m extensions.sop_v2.pipeline.stage3_collect --batch-id {args.batch_id}")
    else:
        print("\n（只读监控。加 --requeue 可把这些退回 qualified 等下一轮 stage3 补采）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
