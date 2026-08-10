# SOP V2 文档索引

当前生产主线是 Modash-first、Instagram browser-only 的四阶段流水线。
2026-07-28 起，Storefront 按通用电商三态处理；展示型预估报价按最近 10 条非置顶
Reels 均播 × CPM $35（区间 $35–$40）/1000 计算。播放证据由登录态浏览器会话读取
Instagram 同源 media info，定价优先且只采用 IG 原生 `ig_play_count`；总
`play_count`/`fb_play_count` 仅审计，不得抬价。部分样本、Modash fallback 和缺失
必须如实标注，且展示估价与实际报价、评分和路由隔离。

建议阅读顺序：

1. [`../../README.md`](../../README.md)：系统定位、当前入口和端到端总览；
2. [`../ARCHITECTURE.md`](../ARCHITECTURE.md)：当前组件、数据和决策架构；
3. [`../ACCOUNT_POOL_ARCHITECTURE.md`](../ACCOUNT_POOL_ARCHITECTURE.md)：账号、会话、代理与轮换；
4. [`PIPELINE_SPEC.md`](PIPELINE_SPEC.md)：状态机、软锁和数据库 API；
5. [`RUNBOOK.md`](RUNBOOK.md)：分阶段运行命令；
6. [`FEEDBACK_GOVERNANCE.md`](FEEDBACK_GOVERNANCE.md)：客户反馈、策略提案、回放与审计；
7. [`STRATEGY_LOCK.md`](STRATEGY_LOCK.md)：经实测锁定的采集策略；
8. [`SYSTEM_STATUS.md`](SYSTEM_STATUS.md)：截至文档日期的完成度与延后项；
9. [`FINAL_DELIVERABLES.md`](FINAL_DELIVERABLES.md)：交付字段和目标结构；
10. [`REQUIREMENTS_CHECKLIST.md`](REQUIREMENTS_CHECKLIST.md)：当前逐项业务与验收口径。

事实源说明：

- `config/sop_v2.toml` 是 V2 规则配置；
- `scripts/extensions/sop_v2/pipeline/` 与 `scripts/browser_collect_v2.py` 是运行事实；
- `REQUIREMENTS_CHECKLIST.md`、`GAP_AND_IMPLEMENTATION_PLAN.md`、`DEV_EXECUTION_CHECKLIST.md`
  是需求与演进记录，不是实时完成度的唯一来源；
- 根目录 `FLOWCHART.md`、`PIPELINE_LOGIC.md`、`EXECUTION_PLAN.md`、`RESEARCH.md` 和
  `TWO_TIER_DESIGN.md` 主要描述 Legacy 或历史方案。

账号、密码、TOTP、Cookie、代理凭据、Chrome profile、数据库、证据和交付件均不得进入 Git。
