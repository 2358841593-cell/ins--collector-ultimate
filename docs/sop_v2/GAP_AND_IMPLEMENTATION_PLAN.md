# Instagram 红人筛选 SOP V2 差距与实施方案

来源文档：`Instagram红人筛选SOP标准确认书_客户版.docx`（Client-Facing V2.0，2026-07-13）。

## 1. 结论

V2 可在现有项目基础上实现。现有六阶段主管道、Instagram 回扫、评论分析、水军库、Storefront 浏览器核验、SQLite、XLSX/HTML 输出和账号池都保留，不替换已经实跑验证的主骨架。

实施原则是“增量增强，不推倒重写”：在旧主管道之后增加 SOP V2 配置、补充字段、硬门槛、新评分、证据和五池交付；旧输出保留用于对照和回滚。

采集口径：当前项目继续使用 `scripts/discover.py` + `scripts/account_pool.py`。监控项目的早期单账号 `collect_instagrapi.py` 不替换当前采集器。

## 2. 在现有主管道上必须增量调整的逻辑

1. 删除“无 Amazon Storefront 立即 Exclude”。改为 `Include-With-Storefront` 和 `Include-Without-Storefront` 双轨；状态未知才进 Review。
2. 停止在 profile 阶段因无 Storefront 跳过 posts 回扫，否则 Without-Storefront 候选永远无法评分。
3. 单一 10K-150K 粉丝门槛拆成 Paid/Gifting 两条轨道。
4. 保留旧评分用于对照，新增 A-F 六模块 100 分与 N/A 归一化；完成回归验证后再切换客户交付口径。
5. 线性主管道继续产出原始候选与扫描缓存；新增的 V2 决策阶段合并 Modash、浏览器和人工证据，再产生客户五池。
6. 图谱/索引结果最高只能进 Review，不能绕过完整数据审计进入 Include。

## 3. 能力对照

| V2 要求 | 现状 | 处理 |
|---|---|---|
| Modash 补充审计 | 仅有宽松 CSV 解析 | Instagram 初筛后，通过 Chrome 中的 Codex 插件对少量高潜候选逐个补数，默认不批量导出 |
| Paid/Gifting 双轨 | 仅 Paid 10K-150K | 新增 campaign track 与严格边界 |
| Creator/Top Audience Country 一致 | 没有 | 新增 Modash 国家、目标国合计、语言字段及 gate |
| Fake <25%、General ER >2% | 读取部分字段，未按 V2 硬门槛 | 新增严格 gate：25% 和 2% 边界均 Exclude |
| Storefront 双轨 | 无 Storefront 硬淘汰 | 重写 funnel/verdict/export |
| Storefront 3 个月活跃度 | 只数图片 | 新增证据时间/更新日期；页面不暴露时 Review |
| SHEIN/Temu 合作淘汰 | 没有 | 只在合作语境和合作历史命中，避免普通提及误杀 |
| Raw Skin/VO 人工证据 | 输出中预留，主管道未实现 | 建立 manual evidence JSON/XLSX 回写合同 |
| 评论双条件 | 有意图比例，无“≥5条且≥15%” | 更新采样和评分，有效样本 <20 固定 Review |
| Paid CPM | 无 | 新增实际 USD 报价、非置顶近 10 Reels 均播和边界 gate |
| 100 分 + AI Score | 旧加权分 | 新增 normalized total、N/A 分母和 9.5 特殊封顶 |
| 五池交付 | 候选/证据/排除/说明 | 改为五决策池 + Evidence/Data Dictionary/Summary 辅助表 |
| Herman 反馈回流 | 无 | 保留两个空列，提供 feedback import 命令 |
| 批次可复现 | 只有 run id | 新增 manifest、SOP/config/source SHA-256、证据索引 |
| 边界回归测试 | 只有接口诊断脚本 | 新增纯规则单测和 golden fixtures |

### 从监控项目复用哪些“零件”

| 能力 | 取舍 | 原因/用法 |
|---|---|---|
| `collect_instagrapi.py` 单账号采集器 | 不迁入 | 当前 `discover.py + account_pool.py` 更新，已有轮换、冷却、暖 Session、Cookie/TOTP 和烧号防护 |
| `collect_top_comments.py` 的断点合并思路 | 拆出复用 | 当前评论分析器更新，但可借用评论 run 按 post 合并/续跑能力，避免重抓 |
| `analyze_comments.py` | 不替换 | 当前 `_analyze_comments` 已包含购买意图、bot、跨帖 pod 和持久水军库，只按 SOP 调整样本和阈值 |
| `normalize_run.py` | 拆出字段规范思路 | 将标准化、原值、来源和证据写回现有 candidate JSON，不新建另一套管道 |
| `extract_video_evidence.py` | 按需拆出复用 | 可复用视频下载、FFmpeg、关键帧、音轨和 ASR 工具；VO 最终仍按客户要求人工确认 |
| `collect_hashtag_topics.py`/爆款报告链 | 不迁入主项目 | 属内容监控业务，与本次红人筛选验收无直接关系 |
| 监控报告/HTML 生成 | 不迁入 | 当前 `build_report.py` 和 `export_xlsx.py` 是已测的红人筛选交付链，在其上增量加 V2 字段/五池 |

## 4. 目标架构（保留现有主骨架）

```text
现有：discover.py + account_pool.py
  Stage 1 发现 → Stage 2 profile/posts → Stage 3 现有漏斗
  → Stage 4 评论 → Stage 5 旧评分 → Stage 6 JSON/CSV + scan cache
                                      |
                                      | 新增，不改采集器
                                      v
                      Modash Chrome/Codex 插件定向补数
                      + Storefront/Raw Skin/VO/报价证据
                                      |
                                      v
                    sop_v2_rules.py + sop_v2_scoring.py
                                      |
                                      v
                     five-pool XLSX + HTML + SQLite

旧 JSON/CSV/scan cache 始终保留，V2 可从 scan cache 离线重跑。
```

## 5. 建议的最小增量文件

```text
config/
  sop_v2.toml
scripts/
  sop_v2_rules.py
  sop_v2_scoring.py
  import_modash_browser_evidence.py
  import_manual_evidence.py
  export_v2_xlsx.py
  import_client_feedback.py
tests/
  test_gate_boundaries.py
  test_scoring.py
  test_five_pool_routing.py
  fixtures/
```

## 6. 数据合同

每个标准化字段至少保存：

- `value`：标准化值。
- `raw_value`：源文件原值。
- `source`：Modash / Instagram / browser / manual / quote。
- `captured_at`：采集时间。
- `evidence_ref`：源 URL、截图、PDF 页或评论链接。
- `status`：available / missing / pending / not_applicable / conflict。

每个 gate 保存：

- `gate_id`、`result`、`observed`、`threshold`、`reason_code`、`source`、`evidence_ref`。

每个评分项保存：

- `module`、`item`、`earned`、`available`、`reason`、`evidence_ref`。

## 7. 分流优先级

1. 命中硬红线：`Exclude`。
2. 专项固定 Review：缺 Modash 核心字段、Storefront 未知、评论不足、Raw Skin/VO 未核验、Paid 缺实际报价。这些不被高分覆盖。
3. 证据完整后按 Normalized Total 分层。
4. Include 再按 Storefront confirmed yes/no 拆成两池。
5. 9.5+ 必须额外通过 Exceptional 条件，否则封顶 9.4。

### 文档内冲突的代码口径

- Organic 条数：早期评分表写“≥3 条满分”，客户填写为 10 条。依“客户补齐项优先”，初版 config 用 10 条作为满分阈值，不作硬淘汰；首批验收后再校准。
- Reels ER：不执行“Reels ER >2% 进 Review”的字面规则。Modash General ER 仍按 `>2%` 硬门槛；异常互动改按“至少 5 帖且单帖互动集中度 ≥70%”进 Review。
- Storefront 更新日期：页面不暴露时不伪造，标记 Missing/Review；确认无 Storefront 时成熟度子项为 N/A。
- Gifting/Paid 粉丝区间重叠：每个批次或候选必须有明确 `campaign_track`，不根据粉丝数自动猜测。

## 8. 实施阶段

### P0：先让决策可复现

- 建 `config/sop_v2.toml`，不再把门槛散落在代码里。
- 直接扩展现有 candidate JSON，增加 `field_evidence/gate_results/sop_v2_score/final_pool`，不替换原字段。
- 实现 Modash Chrome 补数记录的 raw import + 字段适配报告；默认不依赖余额消耗型批量导出。
- 实现 Paid/Gifting、国家、Fake、ER、赞助、SHEIN/Temu 硬门槛。
- 实现 Storefront 双轨、N/A 归一化、100 分和 AI Score。
- 实现五池 XLSX、Herman 两列、Missing Data 和 Evidence Link。
- 为 25%、2%、30%、40%、$35、$40 等边界写测试。
- 所有 P0 逻辑先用已有 scan cache 离线回归，不消耗 Instagram 账号。

### P1：补证据与人工节点

- 评论采样改为 Top 10 + Recent 10，每帖最多 10 条；有效样本最少 20。
- 实现“≥5 条且≥15%”购买意图满分。
- 增加 Raw Skin/VO/合作风险/实际报价人工证据导入。
- 修正 Storefront 活跃度与 LTK 手动穿透工作流。
- 实现 Paid CPM 与非置顶近 10 Reels 均播。

### P2：把一次交付变成稳定运营

- 建 batch manifest 和证据索引。
- 固化 Modash List 命名、Status/Tags/Notes/Assignee 规则。
- 实现 Herman 反馈导入和冲突分类。
- 看板统计批准率、Review 率、缺失率、硬门槛淘汰率与主要分歧。
- 客户批准者回流 Lookalike；拒绝者只记负向标签，不自动泛化为新规则。

## 9. 自动化边界

可完全自动：Instagram 原始数据入库、Modash 补数记录导入、字段映射、硬门槛、评分、五池分流、数据库、导出、批次 manifest、边界测试。

只能半自动：通过客户已登录 Chrome 中的 Codex 插件逐个读取 Modash（会话或 2FA 可能需客户）、LTK 逐商品穿透、Raw Skin/VO、合作风险、报价核验。

不应伪自动：Saves、DM Shares、未取得的实际报价、页面未暴露的 Storefront 更新日期。按 V2 记为 N/A、Missing 或 Review。

## 10. 首批验收

- 使用客户 Chrome 已登录的 Modash，通过 Codex 插件对 Instagram 初筛后的高潜候选逐个补数，完成字段映射和证据留存。
- 从 100 个候选开始，目标产出 1-10 个高质量 Include，不凑数。
- 人工抽查所有 Include 和至少 20 个 Exclude/Review。
- 同一批 raw 数据重跑必须得到同一 gate、分数和五池结果。
- 只把客户明确确认的新规则写回 config；个例偏好先保留为反馈标签。
