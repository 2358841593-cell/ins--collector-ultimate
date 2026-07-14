# Instagram 红人筛选 SOP V2 需求追踪与操作清单

版本：Baseline 1.0

日期：2026-07-14

规则源：`Instagram红人筛选SOP标准确认书_客户版.docx`
用途：作为后续开发、测试、批次执行和客户验收的唯一逐项清单。

## 1. 状态说明

- `EXISTING`：现有功能可直接复用。
- `PARTIAL`：已有基础，需按 V2 修改。
- `TODO`：需新开发。
- `MANUAL`：客户确认必须人工判断或回填。
- `EXTERNAL`：需从 Modash、实际报价或第三方页面取数。

## 2. 需求—实现—验收追踪表

### A. 范围、授权与数据安全

| ID | 客户需求 | 系统操作 | 开发/验收条件 | 状态 |
|---|---|---|---|---|
| SCOPE-01 | 只处理 Instagram | 限制 platform=instagram | 非 Instagram 输入被拒绝并记录 | PARTIAL |
| SCOPE-02 | 范围截止到触达前决策 | 不发邮件、DM、Gift、Campaign、Payment | 执行代码无对外发送或资金动作 | TODO |
| AUTH-01 | Modash 使用客户 Chrome 已登录会话 | 后续仅通过 Chrome 中 Codex 插件访问 | 项目不保存 Modash 账密/Cookie/Token | TODO/EXTERNAL |
| AUTH-02 | 2FA/Email 验证码由客户完成 | 遇验证时停在页面等待 | 无读取、无保存验证码 | TODO/MANUAL |
| AUTH-03 | 默认不消耗 Modash 导出余额 | Instagram 先缩池，Modash 只补查高潜候选 | 无明确授权不点击付费/消耗型 Export | TODO |
| AUTH-04 | 可查看、搜索、截图和记录 | 逐个读取必需字段 | 每条 Modash 数据有时间和证据引用 | TODO/EXTERNAL |
| AUTH-05 | 日志不得泄露敏感信息 | 日志脱敏 | 扫描无密码、Cookie、Token、验证码 | PARTIAL |

### B. 数据源与 Modash 余额控制

| ID | 需求 | 系统操作 | 验收条件 | 状态 |
|---|---|---|---|---|
| DATA-01 | Instagram 为主采集源 | 先完成发现、profile、近帖、评论、bio link | 不访问 Modash 也能完成第一轮缩池 | EXISTING |
| DATA-02 | Modash 为补充审计源 | 仅对未被 Instagram 红线排除的高潜候选补查 | 每批有 `modash_lookup_budget` 与实际查询数 | TODO |
| DATA-03 | Modash 未查不得猜测 | 字段标记 missing/pending | 缺 Fake/ER/Country 的候选不进 Include | TODO |
| DATA-04 | 原始值与标准化值同时保留 | 建 FieldEvidence 数据合同 | 可追溯 raw/value/source/time/evidence | TODO |
| DATA-05 | 数据冲突不静默覆盖 | 记录 conflict，按字段优先级处理 | 冲突出现在 Missing Data/审计报告 | TODO |
| DATA-06 | 字段缺失按专项规则分流 | 不用高分覆盖缺失 | 核心 Modash 字段缺失固定 Review | TODO |

### C. 候选发现与基础回扫

| ID | 需求 | 系统操作 | 验收条件 | 状态 |
|---|---|---|---|---|
| DISC-01 | 品牌/竞品种子 | 使用 Omnilux、CurrentBody、Therabody | 来源记录到 discovery_source | EXISTING/PARTIAL |
| DISC-02 | 客户批准者作 Lookalike 种子 | 从反馈库读取 approved | 仅批准者自动回流 | TODO |
| DISC-03 | 品牌 tagged/mention 反查 | 建候选池 | 保留源帖链接 | EXISTING |
| DISC-04 | Instagram 相似号扩池 | Lookalike/suggested 多跳 | 图谱结果最高为 Review | PARTIAL |
| DISC-05 | 产品关键词 | Nanoleaf/red light therapy/face mask/wand/panel | 命中词与帖子证据可追溯 | PARTIAL |
| DISC-06 | 竞品关键词 | Omnilux/CurrentBody/Therabody | 命中类型分开记录 | PARTIAL |
| DISC-07 | 使用场景词 | acne care/anti-aging/skin recovery/daily routine | 用于相关性评分 | PARTIAL |
| DISC-08 | 候选去重 | handle 主键去重 | 同批无重复候选 | EXISTING |
| COLLECT-01 | 公开 profile 可读 | 私密账号排除 | 原因为 private_account | EXISTING |
| COLLECT-02 | 临时读取失败先重试 | 可配置重试和冷却 | 重试后仍失败进 Review，不误 Exclude | PARTIAL |
| COLLECT-03 | 赞助/内容窗口近 15 帖 | 回扫至少 15 条 | 样本不足显示 Missing Data | EXISTING/PARTIAL |
| COLLECT-04 | Raw Skin/VO 窗口近 30 帖 | 保存最近 30 条媒体索引 | 可提交至少 2 条有效视觉证据 | TODO |
| COLLECT-05 | 评论窗口 Top 10 + Recent 10 | 去重后每帖最多 10 条 | 采样策略写入 manifest | PARTIAL |

### D. 硬红线与分流

| ID | 硬门槛 | 严格规则 | 验收测试 | 状态 |
|---|---|---|---|---|
| GATE-01 | Paid 粉丝 | 10K-150K，含边界 | 9,999 和 150,001 Exclude；10K/150K 通过 | PARTIAL |
| GATE-02 | Gifting 标准池 | 5K-<30K | 5K 通过；30K 不进标准池 | TODO |
| GATE-03 | Gifting 优秀池 | 30K-50K Priority Review | 30K/50K 进 Priority Review；>50K 不进 Gifting | TODO |
| GATE-04 | 目标国家 | Tier1 US/CA/UK/DE/IT/FR/ES；Tier2 NL/BE/CH/SE | 其他国家 Exclude | TODO/EXTERNAL |
| GATE-05 | 国家一致 | Creator Country = Top Audience Country | 不一致 Exclude | TODO/EXTERNAL |
| GATE-06 | Fake Followers | `<25%` | 24.99% 通过；25% Exclude | TODO/EXTERNAL |
| GATE-07 | Modash General ER | `>2%` | 2.00% Exclude；2.01% 通过 | TODO/EXTERNAL |
| GATE-08 | 赞助比例 | `<30%` 通过；30%-40% Review；>40% Exclude | 30%/40% Review；40.01% Exclude | PARTIAL |
| GATE-09 | SHEIN/Temu 合作 | 近 12 月+可见历史任意真实合作命中 Exclude | 普通提及不淘汰；合作语境淘汰 | TODO |
| GATE-10 | 明显品牌/官方号 | 非个人 creator Exclude | 保留命中证据 | EXISTING/PARTIAL |
| GATE-11 | Storefront | 有/无均可 Include；状态未知 Review | 无 Storefront 不再被硬淘汰 | TODO |
| GATE-12 | 图谱边界 | 图谱候选未经完整审计最高 Review | 不得由 graph 直接路由 Include | TODO |

### E. A 模块—内容赛道 15 分

| ID | 子项 | 评分规则 | 验收 | 状态 |
|---|---|---|---|---|
| SCORE-A1 | 核心垂类 5 | Skin Care/Beauty Device=5；Beauty/Wellness=4；Lifestyle=2；其他=0 | Lifestyle 默认 Review，证据足可提升 | PARTIAL |
| SCORE-A2 | Amazon Finds 导购型 5 | 近 15-20 帖：≥40%=5；30-39%=4；20-29%=2；<20%=0 | 边界测试 | PARTIAL |
| SCORE-A3 | 产品/SKU 相关 3 | 产品词/竞品词/场景词每类 1 分 | 证据显示命中词与帖子 | TODO |
| SCORE-A4 | Organic 稳定性 2 | 客户填写 10 条作满分线，不作硬淘汰 | config 可调，首批校准 | PARTIAL |

### F. B 模块—专业度与视觉真实度 15 分

| ID | 子项 | 规则 | 验收 | 状态 |
|---|---|---|---|---|
| SCORE-B1 | 护肤成分 3 | 按词类识别 retinol/niacinamide/ceramide/skin barrier 等 | 输出命中证据 | PARTIAL |
| SCORE-B2 | 设备规格 4 | wavelength/nm/irradiance/red light/near infrared | 输出命中证据 | PARTIAL |
| SCORE-B3 | 皮肤问题/科学 2 | sensitive skin/acne/hyperpigmentation/collagen 等 | 输出命中证据 | PARTIAL |
| SCORE-B4 | Raw Skin 4 | A=4；B=2；C=0；Include 最低 B | 最近 30 帖至少 2 条 URL/截图+核验时间 | TODO/MANUAL |
| SCORE-B5 | VO 2 | 红光设备内容有人声旁白=2 | 客户要求人工确认，有证据才放行 | TODO/MANUAL |
| SCORE-B6 | 视觉不一票否决 | C/无证据进 Review | 不因单独视觉分 Exclude | TODO |

### G. C 模块—社区信任 20 分

| ID | 子项 | 规则 | 验收 | 状态 |
|---|---|---|---|---|
| SCORE-C1 | IG 互动率 5 | Micro Reels≥3% 或 Static≥1.8%；Mid≥1.5% | Reels/Static 分开计算 | EXISTING/PARTIAL |
| SCORE-C2 | Modash General ER 3 | >2%=3；缺失 Review；≤2% 硬淘汰 | 与 GATE-07 一致 | TODO/EXTERNAL |
| SCORE-C3 | 高意图评论 6 | 满分同时要求≥5条且有效评论中≥15% | 只满足一个条件不得满分 | PARTIAL |
| SCORE-C4 | 有效评论样本 | 至少 20 条 | <20 固定 Review | TODO |
| SCORE-C5 | 低质评论 3 | <25%=3；25-40%=2；40-50%=1；≥50%=0 且 Review | 50% 不自动 Exclude | PARTIAL |
| SCORE-C6 | 互赞团/水军库 | 历史命中+跨帖低质刷评+泛化吹捧 | 忠实粉丝长评不误判 | EXISTING |
| SCORE-C7 | Shares/Saves/DM 3 | 无硬门槛；Modash 平均 Reels Shares 可辅助；缺失不扣分 | Saves/DM 不伪造 | TODO/EXTERNAL |
| SCORE-C8 | 异常互动 | 至少 5 帖时，单帖互动集中度≥70% Review | 不执行“ER>2% Review”冲突文字 | TODO |

### H. D 模块—商业基础设施 15 分

| ID | 子项 | 规则 | 验收 | 状态 |
|---|---|---|---|---|
| SCORE-D1 | Storefront 可确认 4 | 有=4；确认无=2；未知 Review | 有/无均可最终 Include | PARTIAL |
| SCORE-D2 | Storefront 成熟度 4 | 有且近 3 个月更新=4；较旧=2；无则 N/A | 不要求商品数/分类数 | TODO/EXTERNAL |
| SCORE-D3 | LTK/聚合页 | 人工点到可确认状态；仍未知 Review | 保留访问链和截图 | PARTIAL/MANUAL |
| SCORE-D4 | 赞助健康度 3 | <30%=3；30-40%=1+Review；>40% Exclude | 与 GATE-08 一致 | PARTIAL |
| SCORE-D5 | Elite Brand History 4 | Omnilux/CurrentBody/Therabody 每个 +1.5，封顶4；红光面罩+VO 可4 | 合作有效窗口 12 个月 | TODO/EXTERNAL |

### I. E 模块—Modash 受众质量 20 分

| ID | 子项 | 规则 | 验收 | 状态 |
|---|---|---|---|---|
| SCORE-E1 | Fake Followers 6 | <15%=6；15-24%=3；≥25% Exclude | 保留 Modash 原值/截图/时间 | TODO/EXTERNAL |
| SCORE-E2 | 国家一致 5 | Creator Country = Top Audience Country | 不一致 Exclude | TODO/EXTERNAL |
| SCORE-E3 | 目标市场受众 4 | 目标国合计≥50%=4；35-<50%=2；<35% Review | 合计可追溯到各国原值 | TODO/EXTERNAL |
| SCORE-E4 | 年龄/性别 3 | 客户无门槛，标 N/A 并从分母移除 | 不送分、不扣分 | TODO |
| SCORE-E5 | 语言/兴趣 2 | Top Audience Language 匹配市场且≥50%；兴趣辅助 | 缺失/不满足 Review | TODO/EXTERNAL |

### J. F 模块—经济性与触达准备 15 分

| ID | 子项 | 规则 | 验收 | 状态 |
|---|---|---|---|---|
| SCORE-F1 | Paid CPM 8 | ≤$35=8；>$35 且≤$40 Review/中档；>$40 Exclude | 35/40 边界测试 | TODO/MANUAL |
| SCORE-F2 | CPM 公式 | 实际 USD 报价 ÷ 总原生曝光 ×1000 | 不用估算报价作确认值 | TODO |
| SCORE-F3 | 曝光口径 | 非置顶近 10 条 Reels 平均播放；缺失才用 Modash 均播 | 保留 10 条帖子明细 | TODO |
| SCORE-F4 | Gifting | 费用记 0，不计算零 CPM，不因此加分 | 不出现 CPM=0 高分 | TODO |
| SCORE-F5 | 预算层级 3 | track、报价、粉丝档匹配 | 规则可配置 | TODO |
| SCORE-F6 | 联系方式 2 | email/form=2；仅 DM=1；无=0 | 不发送，只记录可用性 | TODO |
| SCORE-F7 | 合作风险 2 | 无禁用品牌/竞品冲突/争议=2；不明 Review | 人工证据+时间 | TODO/MANUAL |
| SCORE-F8 | 缺实际报价 | Paid 固定 Review | 高分不得覆盖 | TODO |

### K. 总分、AI Score 与五池

| ID | 需求 | 规则 | 验收 | 状态 |
|---|---|---|---|---|
| TOTAL-01 | 100 分结构 | A15+B15+C20+D15+E20+F15 | 可用分合计正确 | TODO |
| TOTAL-02 | N/A 归一化 | `earned / available * 100` | N/A 不在 earned 或 available | TODO |
| TOTAL-03 | AI Vetting Score | `round(normalized_total / 10, 1)` | 只保留 1 位小数 | TODO |
| TOTAL-04 | 9.5+ 特殊条件 | 红线全过+Elite/红光史+VO+成熟 Storefront+高意图满分+赞助<30% | 不满足封顶 9.4 | TODO |
| ROUTE-01 | Include | ≥7.5+红线全过+核心字段完整 | 再拆 Storefront 有/无 | TODO |
| ROUTE-02 | Priority Review | 6.5-<7.5，或高潜但有固定待补项 | 显示 Missing Data 和补数动作 | TODO |
| ROUTE-03 | Review | 5.0-<6.5，或专项证据缺失 | 不进触达名单 | TODO |
| ROUTE-04 | Exclude | <5.0 或任一硬红线 | 保留原因和证据 | TODO |
| ROUTE-05 | 固定 Review 优先 | Modash 核心缺失、Storefront 未知、评论不足、Raw Skin/VO 未核验、Paid 缺报价 | 高分不能自动提升 | TODO |

### L. 交付、证据、数据库和反馈

| ID | 需求 | 实现 | 验收 | 状态 |
|---|---|---|---|---|
| DEL-01 | 五池 XLSX | 五个决策 Sheet | 每个候选只出现在一个决策池 | TODO |
| DEL-02 | Herman Approval | 空列 | 导出后可填写 | TODO |
| DEL-03 | Herman's Feedback | 空列 | 原样回导不丢失 | TODO |
| DEL-04 | Review/Exclude/Missing Data | 独立列表 | 原因码和人类可读文案同时保留 | PARTIAL |
| DEL-05 | Evidence Link | 主页、Storefront、评论、截图、Modash 页面 | 不包含失效本地绝对路径 | PARTIAL |
| DEL-06 | High-Intent Snippets | 1-2 条英文原话 | 带对应帖子 URL | PARTIAL |
| DEL-07 | 原始/标准化 Modash 值 | 字段证据展开 | 可审计 | TODO |
| DEL-08 | XLSX 可作最终交付 | 默认不强制 Google Sheet | 客户可直接打开与筛选 | EXISTING/PARTIAL |
| DB-01 | 完整候选快照 | 字段、gate、score、route、evidence 入库 | 可重现当次决策 | PARTIAL |
| DB-02 | 批次 manifest | SOP 版本、config/source SHA-256、采集参数 | 同 raw+config 重跑结果一致 | TODO |
| DB-03 | 证据索引 | 截图/PDF/URL/时间/字段映射 | 找得到每个放行/淘汰证据 | TODO |
| FB-01 | 客户反馈回导 | 按 batch+handle 匹配 | Approval/Feedback 原样保留 | TODO |
| FB-02 | 批准者正向回流 | 进入 Lookalike seed | 下批可追溯来源 | TODO |
| FB-03 | 拒绝者负向标签 | 记录原因，不自动改规则 | 只有客户明确确认才写回 config | TODO |
| QA-01 | 硬边界测试 | 25%、2%、30%、40%、$35、$40、粉丝上下限 | 所有边界用例通过 | TODO |
| QA-02 | 评分测试 | N/A、分母、AI Score、9.5 封顶 | golden fixtures 通过 | TODO |
| QA-03 | 路由测试 | 五池互斥 | 不重复、不遗漏 | TODO |
| QA-04 | 导出测试 | 必需字段、链接、空反馈列 | XLSX 结构和可读性通过 | TODO |

## 3. 单批执行操作清单

### 阶段 0：批次建立

- [ ] 生成 `batch_id`。
- [ ] 锁定 SOP 版本和 `sop_v2.toml` SHA-256。
- [ ] 选择 `Paid` 或 `Gifting` track，不自动猜测。
- [ ] 记录目标国家、产品词、竞品词、场景词、种子号和黑名单。
- [ ] 设置候选目标数和 `modash_lookup_budget`。
- [ ] 创建 raw、evidence、output、manifest 目录。

### 阶段 1：Instagram 发现与廉价初筛

- [ ] 从品牌 tagged/mention、关键词、approved lookalike 发现候选。
- [ ] 去重并保留所有 discovery source。
- [ ] 采集 profile、bio links、近 30 帖索引。
- [ ] 先过私密账号、粉丝区间、品牌号等 Instagram 可判红线。
- [ ] 不因无 Storefront 跳过内容回扫。
- [ ] 不为已命中硬红线的候选消耗 Modash 查询。

### 阶段 2：内容、赞助和评论审计

- [ ] 计算赛道、产品推荐比例、SKU/竞品/场景命中。
- [ ] 统计近 15 帖赞助比例和 Organic 条数。
- [ ] 检查 SHEIN/Temu 是否处于真实合作语境。
- [ ] 分别计算 Reels/Static 互动率。
- [ ] 选 Top 10 + Recent 10，去重后每帖最多 10 条评论。
- [ ] 过滤 bot/pod，计算有效样本和高意图双条件。
- [ ] 有效评论 <20 时写入固定 Review。

### 阶段 3：Storefront 和人工内容证据

- [ ] 真浏览器核验 bio/Linktree/Beacons/LTK/Amazon 状态。
- [ ] 将 Storefront 标记为 confirmed_yes / confirmed_no / unknown。
- [ ] 有 Storefront 时检查近 3 个月活跃度；页面不暴露则记 Missing。
- [ ] 人工核验近 30 帖 Raw Skin，至少保留 2 条证据。
- [ ] 人工确认 VO 并保留 URL/截图/时间。
- [ ] 缺 Raw Skin/VO 证据不直接淘汰，进 Review。

### 阶段 4：Modash 定向补数

- [ ] 仅对通过 Instagram 初筛的高潜候选查询。
- [ ] 使用客户已登录 Chrome 中的 Codex 插件，不导入账密。
- [ ] 默认不点击余额消耗型批量 Export。
- [ ] 读取 Fake Followers、General ER、Creator Country、Top Audience Country、目标国分布、Top Language、Reels Shares/均播、合作史。
- [ ] 对每个补数字段保存值、页面、时间和截图引用。
- [ ] 到达 `modash_lookup_budget` 后停止，剩余候选保留 Missing/Review。

### 阶段 5：报价和合作风险

- [ ] Paid 仅接受红人/代理的实际 USD 报价。
- [ ] 排除置顶帖，计算近 10 Reels 平均播放。
- [ ] 计算 Paid CPM 并过 $35/$40 边界。
- [ ] Gifting 记 0 成本但不计 CPM。
- [ ] 记录联系方式可用性，不发送消息。
- [ ] 人工检查竞品冲突、禁用品牌和争议内容。

### 阶段 6：硬门槛、评分和五池

- [ ] 先跑所有硬门槛，再评分。
- [ ] 生成每个 gate 的 observed/threshold/source/reason/evidence。
- [ ] 按 A-F 生成 earned/available/reason/evidence。
- [ ] 移除 N/A 分母并计算 Normalized Total。
- [ ] 生成 AI Vetting Score，检查 9.5 特殊条件。
- [ ] 专项固定 Review 不被高分覆盖。
- [ ] 路由到且仅路由到一个五池。

### 阶段 7：质量检查与交付

- [ ] 运行边界、评分、路由和导出测试。
- [ ] 人工检查所有 Include 和至少 20 条 Review/Exclude。
- [ ] 检查所有 Evidence Link 可访问且无凭证。
- [ ] 生成五池 XLSX、Evidence Index、Summary 和 Data Dictionary。
- [ ] 生成自包含 HTML 审计报告。
- [ ] 生成 manifest JSON 和证据索引。
- [ ] 确认 Herman Approval/Feedback 为可编辑空列。
- [ ] 交付包不含 secrets、session、Cookie、Token 和原始日志。

### 阶段 8：客户反馈回流

- [ ] 按 batch_id + handle 导入 Herman Approval/Feedback。
- [ ] 客户批准者加入正向 Lookalike 种子。
- [ ] 客户拒绝者记录负向标签与原因。
- [ ] 将分歧归类为阈值、数据、视觉、商业或新规则。
- [ ] 个例偏好不自动修改 config。
- [ ] 只将客户明确确认的新规则写回下一 SOP 版本。
