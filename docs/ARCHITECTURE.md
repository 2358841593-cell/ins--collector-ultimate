# 项目架构与改造边界

## 目标

仓库只保留一个生产入口，以已经跑通的 Instagram 红人筛选系统为稳定核心。客户 SOP V2 的细分要求通过增量模块、字段和交付层补齐，不重写账号池、采集器、发现漏斗、评分与现有导出链路。

## 四层结构

| 层 | 位置 | 约束 |
|---|---|---|
| 稳定核心 | `scripts/*.py`、`config/` | 已验证逻辑优先保持兼容；修改必须有离线回归和小批次验证 |
| 增量扩展 | `scripts/extensions/sop_v2/` | 承接 SOP 新字段、规则、人工核验编排；通过明确输入/输出接入核心 |
| 外部补充 | `scripts/extensions/integrations/` | 只补主项目拿不到或可信度不足的数据，失败时不阻断主流程 |
| 交付与证据 | `data/batches/`、`data/manual_evidence/`、`reports/deliveries/` | 批次状态、人工证据、客户交付分开保存，避免与采集缓存混用 |

## 当前稳定核心

- `scripts/account_pool.py`：账号恢复、轮换、冷却与烧号防护。
- `scripts/discover.py`：线性发现、回扫、漏斗、评论分析、评分与入库。
- `scripts/discover_graph.py`：图谱扩散、索引与复用。
- `scripts/verify_browser.py`：浏览器人工/半自动核验与截图存证。
- `scripts/export_xlsx.py`、`scripts/build_report.py`：现有 Excel 与 HTML 交付。
- `data/discovery.db`、`data/pod_accounts.json`：本机累计的数据资产；公开仓库只保留目录契约，不提交真实内容。

除非某条 SOP 要求无法通过适配层实现，否则不迁移这些文件、不改调用入口、不替换当前 Instagram 采集器。

## SOP V2 增量接入顺序

1. 以 `docs/sop_v2/REQUIREMENTS_CHECKLIST.md` 为唯一验收清单。
2. 先为现有结果增加可解释字段、状态和证据引用，不改变原筛选结果。
3. 新规则先放在 `scripts/extensions/sop_v2/`，对历史 scan cache 做离线回归。
4. 通过后再以小批次接入 `discover.py` 或导出脚本，保持旧参数继续可用。
5. 外部无法自动获得的证据写入 `data/manual_evidence/`，不伪装成系统确定值。
6. 最终批次文件统一落到 `reports/deliveries/`；只有充分脱敏并经过公开审查的样例才允许复制到 `samples/`。

## Modash 使用边界

Modash 是补充数据源，不是默认发现器，也不替代 Instagram 内容、评论、Raw Skin、VO、Storefront 与报价核验。

- 优先用搜索结果页做低成本缩圈；只有决赛圈或关键字段缺失时才进入详细 Profile。
- 默认不批量导出、不解锁邮箱、不启用 Monitoring/Payments。
- 对成本不明确的动作先单条 canary，并在操作前后核对余额。
- 尽量复用 30 天缓存；同一候选在缓存期内不重复消耗。
- 后续通过已登录 Chrome 中的 Codex 插件操作，不在主项目里写死 API 依赖。
- Modash 不可用或余额不足时，主筛选和交付仍能运行，只把相应字段标为“待补充/未核验”。

## 目录边界

- 早期 Creator Discovery MVP 和迁移前文档不进入当前公开工程，避免形成第二套入口。
- 内容监控与爆款分析是独立项目，不是本仓库运行时依赖。若未来需要其中某段通用算法，只复制最小可复用能力并做适配。
- 本机历史归档、真实数据、客户输入和交付文件均由 `.gitignore` 隔离。

## 改动准入

每项新增开发必须能对应到一个 SOP 清单项，并至少说明：输入、输出字段、证据来源、失败降级、是否消耗外部余额、离线测试样本和最终交付列。没有对应验收项的重构默认不进入主流程。
