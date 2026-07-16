"""四阶段模块化流水线（PIPELINE_SPEC.md）。

① stage1_discover  Modash 找种子      → status=seed      （无需 IG 号）
② stage2_qualify   浏览器浅扫合格      → qualified/rejected（需 IG 号，轻）
③ stage3_collect   浏览器深采意图+ER   → collected/rejected（需 IG 号，重）
④ stage4_decide    run_v2 五池+交付     → decided          （+Modash 补数）
经 creator_cache.db 的 status 字段逐级交接，全程可断点续跑。
"""
