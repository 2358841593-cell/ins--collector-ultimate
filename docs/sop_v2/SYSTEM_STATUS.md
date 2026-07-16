# 系统端到端状态 · 2026-07-16（推分支前定稿）

经端到端审计 workflow（4 路并行 + 综合）盘点后收尾。审计头号发现：主干 seed→decided 打通，但
**Include 池此前结构性恒空**（三重 fixed_review 叠加）。本轮已解除。

## 端到端流程（现状：通）

```
① discover(Modash CDP,无号) → seed
② qualify(浏览器浅扫,需号)   → qualified / rejected
③ collect(浏览器深采,需号)   → collected / rejected   （意图评论+截图 + 实算ER + comments.analyze）
④ decide(run_v2 + Modash CSV补数 + 人工核验CSV) → 五池 → decisions.json + deliverable.xlsx
```
状态经 `creator_cache.db` 的 `status` 逐级交接，`locked_at` 软锁断点续跑，号问题 mark_error 不烧号。
跑法见 [RUNBOOK.md](RUNBOOK.md)，架构见 [PIPELINE_SPEC.md](PIPELINE_SPEC.md)。

## 本轮固化（会话临时修 → 进系统 + 验证）

| 项 | 状态 | 验证 |
|---|---|---|
| **comments.analyze 接入深采**（写 valid_comments）→ 解除 comments_insufficient | ✅ | 单测:segment+analyze |
| **Modash CSV 补数**（fake%/国家/受众，modash_enrich.enrich）→ 解除 modash_core_missing | ✅ | 端到端 stage4 |
| **人工核验 CSV**（Raw Skin/VO/报价，enrich_manual）→ 解除 raw_skin_or_vo_unverified | ✅ | 端到端 stage4 |
| **stage4 自动出 XLSX**（build_workbook）→ 一条命令直达交付表 | ✅ | 7 sheet 生成 |
| **Include 可达性**：三重 fixed_review 全清（missing_data=[]），候选按分数落池 | ✅ | 合格候选→Priority-Review(72.34)，≥75 入 Include |
| 删 instaloader 死代码（make_iloader/fetch_profile_il）| ✅ | 采集/流水线零 instaloader |
| 浅扫精度：品牌判定用零售类目 STORE_CATEGORIES（不误杀皮科医生）+ external 排除 threads/meta 页脚 + 外链拿不到→unknown 不 confirmed_no 误杀 | ✅ | 4 号 live 扫 |
| 硬编码入 config `[pipeline]`（粗筛 2k/300k、per_account、stale） | ✅ | config 读 |
| QA fixtures 修复 + GATE-13 real_er 测试 | ✅ | 38 例全绿 |
| RUNBOOK 运行手册（秘钥布局/Chrome CDP/号池维护/命令序列） | ✅ | — |
| creator_cache docstring 去 instaloader 漂移 | ✅ | — |

## 客户需求达成度

**满足**：粉丝档双轨 / 赞助饱和 / Storefront 双轨 / **实算 ER 硬门槛(GATE-13)** / Modash ER 参考化 / 五池互斥 /
AI Vetting Score / 交付表 SOP §8 + Herman 两列 + 证据链接 / 品牌号红线 / 三列正交飞轮 / **评论购买意图证据(截图+入评分)** /
**Modash 假粉受众国家(CSV 补数)** / **Include 可达**。
**人工闭环**：Raw Skin/VO 视觉核验(SOP §B4/B5) + Modash CSV 导出——由人提供 CSV 回填，pipeline 不伪造。
**~30 合格**：pipeline 产出 Review/Priority-Review shortlist(全自动信号绿+证据)，人工核验 Raw Skin/VO → Include。

## 延后（可增强，不阻塞测试；按价值排序）

- SHEIN/Temu 合作史上游判定并回填 shein_temu_partnership（GATE-09 目前无 writer，红线未生效）—— 价值中。
- Modash CDP 实时补数（现走 CSV 导入，够用且对齐客户流程）—— 价值中。
- 账号健康轮换（连续 error 停用该号 + 冷却队列）+ 开跑前 pool_health 预筛挑战号 —— 价值中。
- storefront 外链抽取增强（弹层/多链/聚合页兜底，减少 unknown→Review 漏斗损耗）—— 价值中。
- 飞轮回流 stage1 `--from-golden`（golden_seeds→Modash Lookalike）—— 价值中。
- rejected 可变原因跨轮复活（off_niche/no_storefront N 天后可重扫）—— 价值中。
- 并发 `--workers` 多号并行（软锁已支持）缩短深采墙钟 —— 价值中。
- 品牌/店铺/赛道词表收进 config（现 coarse/pacing 已入 config，词表仍在代码）—— 价值低。
- 退役 assemble.py / 单体 collect_candidate+main（标 legacy，未删；deep_collect 已共享）—— 价值低。
- 其余 docs（COLLECTION_PLAN/DB_FLYWHEEL/STRATEGY_LOCK）instaloader 表述微调 —— 价值低。
