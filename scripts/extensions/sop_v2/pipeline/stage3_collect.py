"""③ 深采：qualified → collected | rejected。纯浏览器零 API（复用 deep_collect）。

开 ~10 帖 → 只在推广帖找购买意图评论+截图 + 帖子赞评算实算 ER。
实算 ER<0.5% → reject(real_er_low)；缺失（未登录/未取赞评）不判 zombie，留 gate_real_er Review。

用法：cd scripts && ../.venv/bin/python -m extensions.sop_v2.pipeline.stage3_collect \
        --batch-id SKINCARE-20260716 [--limit 30] [--posts 10] [--resume]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from extensions.sop_v2.config import load_config  # noqa: E402
from extensions.sop_v2.pipeline._base import run_browser_stage  # noqa: E402


def collect_one(pg, cand, cfg, batch_id, n_posts):
    import browser_collect_v2 as bc
    h = cand["handle"]
    ev_dir = bc.EVIDENCE_ROOT / batch_id / h
    result, ev = bc.deep_collect(pg, cand, ev_dir, n_posts)
    if result is None:
        return ("error", ev or "logged_out")          # logged_out = 号问题，可重试
    # 实算 ER 硬门槛：只挡真僵尸；缺失（None）绝不判 zombie（→ gate_real_er missing_is_review→Review）
    rer = result.get("real_er")
    thr = cfg.get("real_er", {}).get("exclude_below", 0.5)
    if rer is not None and rer < thr:
        return ("reject", "real_er_low")
    return ("advance", "collected", result)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch-id", required=True)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--posts", type=int, default=10, help="前 N 帖算实算 ER（客户口径 10）")
    ap.add_argument("--resume", action="store_true")
    args = ap.parse_args()
    cfg = load_config()
    run_browser_stage("qualified", args.batch_id, args.limit, args.resume,
                      lambda pg, cand: collect_one(pg, cand, cfg, args.batch_id, args.posts),
                      per_account=5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
