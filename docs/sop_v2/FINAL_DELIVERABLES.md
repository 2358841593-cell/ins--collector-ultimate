# Instagram 红人筛选 SOP V2 最终交付物规范

版本：Baseline 1.1

日期：2026-07-28
原则：客户对外交付尽量简单；项目内部保留完整审计能力。

## 1. 最终客户交付

客户默认收到一个主文件：

```text
Instagram_Influencer_Vetting_<batch_id>.xlsx
```

客户已确认 XLSX 可接受，因此不默认增加 Google Sheet 转换步骤。如后续明确要求 Google Sheet，再从同一 XLSX 导入，不另建一套交付逻辑。

### XLSX 固定 Sheet

1. `Batch Summary`
2. `Include-With-Storefront`
3. `Include-Without-Storefront`
4. `Priority-Review`
5. `Review`
6. `Exclude`
7. `Evidence Index`
8. `Data Dictionary`

其中 2-6 是客户 SOP 要求的五个互斥决策池；同一候选不得同时出现在多个池。

## 2. Batch Summary

固定展示：

- Batch ID、生成时间、SOP 版本、活动轨道（Paid/Gifting）。
- 产品/SKU、目标国家、品牌/竞品/场景词。
- 原始候选数、回扫数、Modash 补查数、五池数量。
- Include/Review/Exclude 比例、数据缺失率、主要淘汰原因。
- `modash_lookup_budget` 与实际查询数，说明本批如何控制 Modash 余额消耗。
- 数据截止时间和必要的口径说明。

## 3. 五池共用字段

### 3.1 决策与反馈

| 字段 | 说明 |
|---|---|
| Final Pool | 当前决策池 |
| AI Vetting Score | 1-10，保留 1 位小数 |
| Normalized Total | 0-100，N/A 从分母移除 |
| Decision Summary | 一句话结论 |
| Review Reason | 进 Review/Priority Review 的专项原因 |
| Exclude Reason | 硬红线或低分原因 |
| Missing Data | 待补 Modash/评论/视觉/VO/实际报价等；展示估价缺失单独标状态 |
| Herman Approval | 空列，供客户回填 |
| Herman's Feedback | 空列，供客户原文回填 |

### 3.2 身份与发现

| 字段 | 说明 |
|---|---|
| Country | Creator Country，两位国家码 |
| Handle (IG) | `@username` |
| Full Name | Instagram 显示名 |
| Profile URL | 可点击 Instagram 主页 |
| Followers | 粉丝数 |
| Campaign Track | Paid / Gifting |
| Follower Tier | Micro / Mid / Gifting Standard / Gifting Priority |
| Creator Niche | 主赛道 |
| Secondary Niche | 次赛道 |
| Discovery Source | brand tag / mention / keyword / lookalike / approved seed 等 |
| Seed Evidence | 源帖子或种子链接 |

### 3.3 硬门槛和 Modash 补数

| 字段 | 说明 |
|---|---|
| Creator Country | Modash/人工原值 |
| Top Audience Country | Modash 原值 |
| Country Match | 是/否/缺失 |
| Target Countries Audience % | 目标国家合计 |
| Top Audience Language / % | 主要语言与占比 |
| Fake Followers % | Modash 原值 |
| Modash General ER % | Modash 原值 |
| Modash Checked At | 通过 Chrome/Codex 插件补查时间 |
| Modash Evidence | 页面/截图证据引用 |

未经 Modash 补查时，必须显示缺失，不能填 0 或猜测值。

### 3.4 内容与专业度

| 字段 | 说明 |
|---|---|
| Amazon Finds Ratio | 近 15-20 帖产品推荐比例 |
| Organic Relevant Posts | 近 15 帖非赞助产品/护肤/红光内容数 |
| Product Keyword Evidence | Nanoleaf/red light/face mask/wand/panel |
| Competitor Evidence | Omnilux/CurrentBody/Therabody |
| Use Case Evidence | acne care/anti-aging/skin recovery/daily routine |
| Ingredients Evidence | 成分词证据 |
| Device Specs Evidence | wavelength/nm/irradiance 等 |
| Skin Science Evidence | 皮肤问题/科学表达 |
| Raw Skin Grade | A/B/C/Pending |
| Raw Skin Evidence | 至少 2 条 URL/截图及时间 |
| Has VO | Yes/No/Pending，人工确认 |
| VO Evidence | 对应帖子 URL/截图/时间 |

### 3.5 社区信任

| 字段 | 说明 |
|---|---|
| Reels ER % | Instagram 样本计算 |
| Static ER % | Instagram 样本计算 |
| Comments Analyzed | 原始评论数 |
| Valid Comments | 去除 bot/pod 后的有效数 |
| High-Intent Count | 高意图评论数 |
| High-Intent Ratio | 高意图/有效评论 |
| High-Intent Comment Snippets | 1-2 条英文原话+帖子链接 |
| Bot Ratio | bot/总评论 |
| Pod Ratio | 互赞团/总评论 |
| Low-Quality Ratio | bot+pod/总评论 |
| Interaction Concentration | 单帖互动集中度 |
| Average Reels Shares | Modash 有则记录；缺失不扣分 |

### 3.6 商业基础设施与经济性

| 字段 | 说明 |
|---|---|
| Storefront Status | confirmed_yes / confirmed_no / unknown |
| Storefront Type | Amazon / LTK / ShopMy / 自营店 / 购物聚合 |
| Storefront Link | 已确认的电商购物入口 URL；不限定 Amazon |
| Storefront Last Activity | 可确认的最近更新时间 |
| Storefront Evidence | 浏览器截图/源 URL/核验时间 |
| Sponsorship Saturation | 近 15 帖赞助比例 |
| SHEIN/Temu Partnership | Yes/No/Unknown + 合作证据 |
| Elite Brand History | 近 12 个月红光品牌合作史 |
| Pricing Estimate Status | complete / partial / fallback_modash / missing |
| Pricing Reels Sample | 合格样本数/10；先排置顶，再从剩余 Reels 取最近 10 条 |
| Pricing Average Plays | 合格非置顶 Reels 平均播放量 |
| Estimated Quote USD | 展示默认值：`均播×35/1000` |
| Estimated Quote Range USD | 展示区间：`均播×35/1000` 至 `均播×40/1000` |
| Pricing Estimate Source | instagram_media_info_ig_play_count（同源 media info 的 IG 原生播放）/ modash_profile_fallback / missing |
| Pricing Evidence | 每条 Reel URL、发布时间、`ig_play_count`、审计用总 `play_count`/`fb_play_count`、置顶状态与采集时间 |
| Actual Quote USD | 红人/代理实际报价 |
| Native Exposure for Paid CPM | 实际 CPM 使用的非置顶近 10 Reels 均播 |
| Paid CPM | 实际报价÷原生曝光×1000 |
| Contact Availability | Email/Form/DM/None |
| Partnership Risk | Clear/Unknown/Risk + 证据 |

展示型 `Estimated Quote` 与 `Actual Quote/Paid CPM` 必须分列。前者只是客户决策参考，
不得写入 `paid_cpm`，也不得影响 Gate、F 分、固定 Review 或最终池；后者必须有红人/代理
实际报价证据。估价只使用同源 media info 的 IG 原生 `ig_play_count`；总
`play_count`/`fb_play_count` 仅作审计，不能进入均播或抬高报价。
`partial/fallback_modash/missing` 必须原样暴露，不得伪装为完整 10 条窗口。

### 3.7 评分分解

| 字段 | 说明 |
|---|---|
| A Content Score | /15 |
| B Professional & Visual Score | /15 |
| C Community Trust Score | /20 |
| D Commercial Infrastructure Score | /15 |
| E Audience Quality Score | /20 |
| F Economics & Readiness Score | /15 |
| Applicable Score | 可用分母 |
| Earned Score | 已得分 |
| Normalized Total | `earned/applicable*100` |
| AI Vetting Score | 1-10 |
| Score Reason | 逐项计算摘要 |

## 4. Evidence Index

每个证据一行：

- Evidence ID
- Handle
- Evidence Type
- Related Field/Gate/Score Item
- Source URL
- Local/Embedded Screenshot Reference
- Captured At
- Reviewer
- Result
- Notes

证据类型包括：Instagram Profile/Post/Comment、Storefront、Modash Screen、Raw Skin、VO、Quote、Partnership History、Risk Review。

## 5. Data Dictionary

包含：

- 字段名、中文解释、数据源、计算公式。
- 硬门槛边界。
- A-F 评分规则。
- N/A、Missing、Pending、Conflict 的含义。
- Storefront 双轨和五池分流规则。
- 展示估价的 `$35` 默认、`$35–$40` 区间、先排置顶再取 10 条及四种状态；
- 展示估价与实际报价/Paid CPM 的决策隔离。
- Modash 只作定向补充、非全量导出的口径。

## 6. 项目内部审计包

内部保留，不默认全部发给客户：

```text
delivery/<batch_id>/
  Instagram_Influencer_Vetting_<batch_id>.xlsx
  audit-report-<batch_id>.html
  manifest-<batch_id>.json
  evidence-index-<batch_id>.json
  evidence/
    instagram/
    storefront/
    modash/
    raw-skin/
    vo/
    quote/
  raw/
    discovery.json
    scan-cache.json
    comments.json
    modash-browser-evidence.json
    manual-evidence.json
```

### HTML 审计报告

在现有 `build_report.py` 交互报告上增量扩展：

- 五池总览。
- 硬门槛原值、阈值、来源和证据。
- A-F 分数分解。
- Missing Data 和人工待办。
- Storefront、Modash、Raw Skin、VO、评论截图。
- 新旧评分对照，仅在开发/校准期显示。

### Manifest

至少包含：

- Batch ID、SOP 版本、config SHA-256。
- 所有 raw 源文件 SHA-256。
- 采集、评论、Modash 补查和人工核验参数。
- 各阶段输入/输出数量。
- 运行错误、重试和缺失数。
- 使用的代码版本/提交标识（如有）。

## 7. 交付前验收门槛

- [ ] 五个决策池互斥且无遗漏。
- [ ] Include 候选硬门槛全部有原值、来源和证据。
- [ ] 缺核心 Modash 字段、Storefront 未知、评论不足、受众/语言固定项未满足的候选未被自动 Include。
- [ ] Amazon/LTK/ShopMy/自营店/购物聚合均可被识别；`confirmed_no` 未在采集阶段早淘汰。
- [ ] 展示估价先排置顶再取 10 条，并显示默认值、区间、样本数、来源和完整性状态。
- [ ] 展示估价未写 `paid_cpm`，派生前后 Gate、F 分、固定 Review 和最终池不变。
- [ ] 所有 Exclude 都有可追溯硬红线或分数原因。
- [ ] 所有 Include 和至少 20 个 Review/Exclude 已人工抽查。
- [ ] AI Score、N/A 归一化和 9.5 封顶通过测试。
- [ ] 25%、2%、30%、40%、$35、$40 和粉丝上下限的边界测试通过。
- [ ] 链接可点击，截图可打开，未泄露凭证。
- [ ] Herman Approval 和 Herman's Feedback 为可编辑空列。
- [ ] 交付包不包含 `.secrets/`、session、Cookie、Token、验证码或账号日志。

## 8. 首批交付口径

- 输入目标：100 个 Instagram 候选。
- 运营目标：1-10 个高质量 Include，不强制凑数。
- Modash：只对 Instagram 初筛后有机会进入 Include/Priority Review 的候选逐个补查。
- 客户对外主交付：单一 XLSX。
- HTML、manifest、raw 和证据原文件作为内部审计与必要时的附加交付。
