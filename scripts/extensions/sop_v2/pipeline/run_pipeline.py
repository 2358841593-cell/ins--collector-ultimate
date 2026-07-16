"""顶层编排：顺序跑四阶段（各 stage 也可单独跑）。状态全在 creator_cache.status，
跑到一半停、明天带 --resume 接着跑即可。

用法：cd scripts && ../.venv/bin/python -m extensions.sop_v2.pipeline.run_pipeline \
        --batch-id SKINCARE-20260716 --track paid [--stages 1,2,3,4] [--resume] [--posts 10]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from extensions.sop_v2 import creator_cache as cc  # noqa: E402
from extensions.sop_v2.pipeline import (  # noqa: E402
    stage1_discover, stage2_qualify, stage3_collect, stage4_decide)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch-id", required=True)
    ap.add_argument("--track", choices=["paid", "gifting"], default="paid")
    ap.add_argument("--stages", default="1,2,3,4")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--posts", type=int, default=10)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    stages = {s.strip() for s in args.stages.split(",")}
    bid = args.batch_id

    def argv(*extra):
        sys.argv = ["stage", "--batch-id", bid, *extra]

    if "1" in stages:
        print("\n═══ ① discover ═══")
        argv()
        stage1_discover.main()
    if "2" in stages:
        print("\n═══ ② qualify ═══")
        argv(*(["--resume"] if args.resume else []), *(["--limit", str(args.limit)] if args.limit else []))
        stage2_qualify.main()
    if "3" in stages:
        print("\n═══ ③ collect ═══")
        argv("--posts", str(args.posts), *(["--resume"] if args.resume else []),
             *(["--limit", str(args.limit)] if args.limit else []))
        stage3_collect.main()
    if "4" in stages:
        print("\n═══ ④ decide ═══")
        out = args.out or f"data/runs/{bid}/decisions.json"
        sys.argv = ["stage", "--batch-id", bid, "--track", args.track, "--out", out]
        stage4_decide.main()

    print("\n最终 status:", cc.status_dist(bid))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
