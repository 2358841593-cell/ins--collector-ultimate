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
from extensions.sop_v2 import creator_cache as cc  # noqa: E402
from extensions.sop_v2.pipeline._base import (  # noqa: E402
    nonnegative_int,
    run_browser_stage,
)


def collect_one(
    pg, cand, cfg, batch_id, n_posts, strict_completeness: bool = False,
    comment_translator=None,
):
    import browser_collect_v2 as bc
    h = cand["handle"]
    ev_dir = bc.EVIDENCE_ROOT / batch_id / h
    result, ev = bc.deep_collect(
        pg,
        cand,
        ev_dir,
        n_posts,
        persist_cache=False,
    )
    if result is None:
        return ("error", ev or "logged_out")          # logged_out/grid_nav_failed = 号问题，可重试
    # 产出完整性校验：深采真跑过必写 comments_read + sampled_posts。缺 = 零产出被静默放行（历史 bug）→
    # 判 error 报 stage_error 回退 qualified 重试，绝不静默进 collected。
    if not result.get("comments_read") or "sampled_posts" not in result:
        return ("error", "deep_no_output")
    # 网格有帖子 code 却一帖都没采到 = 帖子页导航全失败（代理抖动/限流），不是"真没帖"→ 判 error 重试。
    # （livvvmarkley/mirandacorneliusbeauty 教训：codes=12 但 sampled_posts=0 仍被放行进 collected）
    if result.get("codes") and not result.get("sampled_posts"):
        return ("error", "deep_all_posts_failed")
    if comment_translator is not None:
        # LLM runs only on persisted comment text and is independent of the IG
        # browser session.  Provider failures become explicit translation rows;
        # they never erase source comments or force another Instagram read.
        comment_translator(result)
    if strict_completeness:
        strict_reasons = cc.strict_deep_reasons(
            result,
            status="collected",
            target_posts=n_posts,
        )
        if strict_reasons:
            # 严格失败仍是一次有价值的采集：把成功帖子、三类失败 URL、评论/报价
            # 等部分证据交给 error 路径原子落库；status 保持 qualified，等待重试。
            return (
                "error",
                "deep_incomplete:" + "；".join(strict_reasons),
                result,
            )
    # 深采完就进 collected——绝不在此丢弃。实算ER/意图等"不太合格"处由 routing 归池 + 交付表写明原因，
    # 让客户真实浏览判断（某处差但有合作价值的也要浮现，不静默丢）。
    return ("advance", "collected", result)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch-id", required=True)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--posts", type=int, default=10, help="前 N 帖算实算 ER（客户口径 10）")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument(
        "--strict-completeness",
        action="store_true",
        help=(
            "严格要求采满 --posts、至少有可判定的赞评指标；评论抽取为 0 时"
            "必须能由逐帖 comment_count=0 证明真实低互动"
        ),
    )
    ap.add_argument(
        "--account-offset",
        type=nonnegative_int,
        default=0,
        help="从深采号池该下标开始轮换（并行批次应使用不同偏移）",
    )
    ap.add_argument(
        "--account-count",
        type=nonnegative_int,
        default=0,
        help="仅使用 offset 起连续 N 个账号；0 表示使用整个池",
    )
    ap.add_argument(
        "--account-rotation",
        type=nonnegative_int,
        default=0,
        help="在已选账号子池内轮换起点；不会跨越并行 worker 的子池边界",
    )
    ap.add_argument("--translate-comments", action="store_true",
                    help="采集后用本机 LLM 生成结构化中文评论译文")
    ap.add_argument("--translation-provider", choices=["ollama", "anthropic"],
                    default="ollama")
    ap.add_argument("--translation-model", default=None)
    ap.add_argument("--translation-api-url", default=None)
    ap.add_argument("--translation-batch-size", type=int, default=40)
    ap.add_argument(
        "--translation-source-limit",
        "--translation-display-limit",
        dest="translation_source_limit",
        type=int,
        default=120,
        help="每个候选送 LLM 的已存评论上限；旧参数名仍兼容（默认 120）",
    )
    args = ap.parse_args(argv)
    if args.translation_batch_size <= 0 or args.translation_source_limit <= 0:
        ap.error("translation batch/source limit 必须为正整数")
    cfg = load_config()
    comment_translator = None
    if args.translate_comments:
        from extensions.sop_v2 import comment_translation

        model = args.translation_model or (
            comment_translation.DEFAULT_MODEL
            if args.translation_provider == "ollama"
            else comment_translation.DEFAULT_ANTHROPIC_MODEL
        )

        def comment_translator(candidate):
            return comment_translation.translate_candidate(
                candidate,
                provider=args.translation_provider,
                model=model,
                api_url=args.translation_api_url,
                batch_size=args.translation_batch_size,
                source_limit=args.translation_source_limit,
                usage_context={
                    "batch_id": args.batch_id,
                    "stage": "stage3_collect",
                    "feature": "comment_translation",
                },
            )
    p = cfg.get("pipeline", {})
    # 深采用隔离号池（存在则用，否则回退默认池）
    deep = p.get("deep_accounts_file")
    root = Path(__file__).resolve().parents[3].parent
    deep_file = str(root / deep) if (deep and (root / deep).exists()) else None
    if deep and not deep_file:
        print(f"  ⚠ 深采号池 {deep} 不存在 → 回退默认池", flush=True)
    run_browser_stage("qualified", args.batch_id, args.limit, args.resume,
                      lambda pg, cand: collect_one(
                          pg,
                          cand,
                          cfg,
                          args.batch_id,
                          args.posts,
                          args.strict_completeness,
                          comment_translator,
                      ),
                      per_account=p.get("collect_per_account", 5),
                      stale_minutes=p.get("resume_stale_minutes", 30),
                      accounts_file=deep_file,
                      account_offset=args.account_offset,
                      account_count=args.account_count,
                      account_rotation=args.account_rotation)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
