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
        return ("error", ev or "logged_out")          # logged_out/grid_nav_failed = 号问题，可重试
    # 产出完整性校验：深采真跑过必写 comments_read + sampled_posts。缺 = 零产出被静默放行（历史 bug）→
    # 判 error 报 stage_error 回退 qualified 重试，绝不静默进 collected。
    if not result.get("comments_read") or "sampled_posts" not in result:
        return ("error", "deep_no_output")
    # 深采完就进 collected——绝不在此丢弃。实算ER/意图等"不太合格"处由 routing 归池 + 交付表写明原因，
    # 让客户真实浏览判断（某处差但有合作价值的也要浮现，不静默丢）。
    return ("advance", "collected", result)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch-id", required=True)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--posts", type=int, default=10, help="前 N 帖算实算 ER（客户口径 10）")
    ap.add_argument("--resume", action="store_true")
    args = ap.parse_args()
    cfg = load_config()
    p = cfg.get("pipeline", {})
    # 深采用隔离号池（存在则用，否则回退默认池）
    deep = p.get("deep_accounts_file")
    root = Path(__file__).resolve().parents[3].parent
    deep_file = str(root / deep) if (deep and (root / deep).exists()) else None
    if deep and not deep_file:
        print(f"  ⚠ 深采号池 {deep} 不存在 → 回退默认池", flush=True)
    run_browser_stage("qualified", args.batch_id, args.limit, args.resume,
                      lambda pg, cand: collect_one(pg, cand, cfg, args.batch_id, args.posts),
                      per_account=p.get("collect_per_account", 5),
                      stale_minutes=p.get("resume_stale_minutes", 30),
                      accounts_file=deep_file)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
