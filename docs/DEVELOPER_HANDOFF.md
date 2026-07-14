# 开发接手指南

## 1. 项目目标

本项目用于 Instagram 红人发现、筛选、证据核验和客户交付。当前稳定版本能够：

- 从品牌 tagged/mention、关键词、Lookalike 和图谱扩散建立候选池；
- 采集公开 profile、帖子和评论，并执行粉丝、内容、赞助和水军过滤；
- 计算现有可解释评分，保存扫描缓存与 SQLite 索引；
- 用真实浏览器核验 bio 聚合页和 Amazon Storefront，并保留证据；
- 输出 Excel 和 HTML 审计报告；
- 通过账号池轮换、冷却、暖 Session 和完整 Cookie 恢复降低登录风险。

目标版本是客户 SOP V2：在不推倒现有主链路的前提下，增加 Paid/Gifting 双轨、
Modash 定向补数、严格硬门槛、A-F 100 分、五个互斥决策池、人工证据、批次
manifest 和客户反馈回流。

## 2. 阅读顺序

1. `README.md`：系统能力、目录、运行入口和账号池纪律。
2. `docs/ARCHITECTURE.md`：哪些是稳定核心，哪些允许增量开发。
3. `docs/sop_v2/REQUIREMENTS_CHECKLIST.md`：唯一逐项验收清单。
4. `docs/sop_v2/GAP_AND_IMPLEMENTATION_PLAN.md`：现状差距、P0-P2 顺序。
5. `docs/sop_v2/FINAL_DELIVERABLES.md`：目标 XLSX、证据和审计包。
6. `docs/INSTAGRAM_LOGIN_SESSION_SOP.md`：账号、Cookie 和暖 Session 操作方式。

遇到文档冲突时，优先级为：客户 V2 验收清单 → 架构边界 → 当前代码行为 →
历史说明。公开仓库不包含历史归档，以免旧入口干扰判断。

## 3. 运行架构

```text
账号池 account_pool.py
  ├─ 暖 Session
  ├─ 完整浏览器 Cookie
  └─ 一次性密码/TOTP 冷登录（最后兜底）
          |
          v
discover.py / discover_graph.py
  发现 → profile/posts → 现有漏斗 → 评论 → 旧评分 → scan cache/SQLite
                                                        |
                                                        v
                    SOP V2 增量层（待开发）
           Modash + 浏览器 + 人工证据 → gates → A-F scoring
                                                        |
                                                        v
                    五池 XLSX + HTML + manifest
```

稳定主入口：

- `scripts/discover.py`：线性生产管道。
- `scripts/discover_graph.py`：图谱扩散、索引和复用。
- `scripts/account_pool.py`：登录恢复、轮换和冷却。
- `scripts/verify_browser.py`：Storefront 等真实页面核验。
- `scripts/export_xlsx.py`、`scripts/build_report.py`：当前交付链。

增量开发必须优先落在 `scripts/extensions/sop_v2/` 或独立新模块，通过清晰的
输入/输出接入稳定核心。不要复制一套新的采集器、账号池或交付主入口。

## 4. 当前完成度

### 已有且应保持兼容

- Instagram 发现、回扫、评论分析、水军库和现有评分。
- 账号池轮换、持久 cooldown、一次性冷登录纪律。
- 完整 Cookie 导出、注入、暖 Session 固化和复载验证。
- Storefront 浏览器核验、SQLite、Excel/HTML 输出。
- SOP V2 的完整需求拆解、交付规范和开发顺序。

### 仅有基础或尚未实现

- Paid/Gifting 严格双轨与国家/Fake/Modash ER 硬门槛。
- Storefront 有/无双 Include 路由。
- Raw Skin、VO、报价、合作风险等人工证据合同。
- A-F 100 分、N/A 归一化、AI Vetting Score 和 9.5 封顶条件。
- 五池 XLSX、Evidence Index、Data Dictionary 和批次 manifest。
- Herman Approval/Feedback 回导及批准者 Lookalike 回流。

不要把文档中的目标能力误认为已实现能力；以
`docs/sop_v2/REQUIREMENTS_CHECKLIST.md` 的状态列为准。

## 5. 本地初始化

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
.venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v
```

账号凭据不在仓库中。由有权限的开发者在本机导入：

```bash
.venv/bin/python scripts/import_pool.py < accounts.txt
```

已有普通 Chrome 登录态时，不要重新撞密码；严格按
`docs/INSTAGRAM_LOGIN_SESSION_SOP.md` 导出完整 Cookie 并建立暖 Session。

## 6. 下一阶段开发顺序

P0 必须先让决策可复现：

1. 新增 `config/sop_v2.toml`，集中管理所有 V2 边界。
2. 定义 `FieldEvidence`、`GateResult`、`ScoreItem`、`BatchManifest` 数据合同。
3. 实现 Paid/Gifting、国家、Fake、ER、赞助和 SHEIN/Temu 硬门槛。
4. 实现 Storefront 双轨、A-F 评分、N/A 归一化和五池互斥路由。
5. 实现五池 XLSX、Herman 两列、Missing Data 和 Evidence Index。
6. 为 25%、2%、30%、40%、35 美元、40 美元等边界添加纯规则测试。
7. 使用已有 scan cache 做离线回归后，才允许小批次在线验证。

P1/P2 顺序与验收条件见 `docs/sop_v2/GAP_AND_IMPLEMENTATION_PLAN.md`。

## 7. 开发纪律

- 每个改动必须标注对应的 SOP ID，并说明输入、输出、证据、失败降级和外部成本。
- Instagram、Modash 和人工证据不得静默互相覆盖，必须保留原值、来源和时间。
- 缺失字段使用 Missing/Pending/N/A/Conflict，不得填 0 或猜测。
- 图谱候选未经完整审计最高只能进入 Review。
- Modash 是高潜候选的补充审计源，不是主发现器；默认不批量导出或消耗余额。
- 不执行邮件、DM、Campaign、Gift 或 Payment 等外部动作。
- 不提交 `.secrets/`、`data/session/`、数据库、日志、截图和客户交付文件。
- 不删除正在使用的暖 Session，不对失败账号进行连续密码重试。

## 8. 完成定义

一个 V2 功能只有同时满足以下条件才算完成：

- 对应需求清单 ID；
- 有确定的数据合同和失败状态；
- 有边界或 golden fixture 测试；
- 能从离线 scan cache 重跑；
- 输出能追溯到原始值和证据；
- 不破坏旧参数和旧主管道；
- 文档、交付列和验收清单状态同步更新。
