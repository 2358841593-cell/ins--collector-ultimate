# SOP V2 开发执行清单（代码实证版）

版本：1.3.1（v1.1 = 五视角对抗校验修订 42 条；v1.2 = 合入操作机初跑实证、R0 修复阶段、Modash 真实余额；v1.2.1 = 三视角校验修订 14 条；v1.3 = 合入 Modash Discovery 浏览器实测，发现层改为 Modash-first 双通道；v1.3.1 = 双视角校验修订 11 条：Handle 池注入接口补全、两源命中承接、发现层降级口径、docx 引用消歧；v1.4 = IG 采集通道决策浏览器优先（§1.6/§2.6，Collector 后端接口，参考 browser-cdp-lab），B0 track，重塑 R0）
日期：2026-07-14
规则源：`Instagram红人筛选SOP标准确认书_客户版.docx`（Client-Facing V2.0，2026-07-13）
实证源：`FULL_INITIAL_RUN_REPORT_20260714.md` + 证据包（3 次在线 run 原始产物）+ `MODASH_FUNCTIONS_COST_REPORT.md`（客户账号只读核对）
定位：本文件是 `REQUIREMENTS_CHECKLIST.md`（验收追踪）与 `GAP_AND_IMPLEMENTATION_PLAN.md`（差距结论）之下的**开发执行层**——每个任务落到具体文件、接入点、输入输出与验收测试。所有"现状"结论均经代码逐行核实或初跑证据核实。

---

## 0. 结论摘要

1. **当前第一优先级不是 V2 功能，是认证阻断修复（R0）**。初跑证明发现入口可用（两次独立发现各得 112 去重候选），但 22/22 账号在 warm-only 下无法恢复私有 API Session，唯一活跃暖 Session 在深扫阶段 `login_required`，全链路停在 Stage 1-2。V2 决策层再完善，没有稳定采集也无法在线验收。
2. **主骨架不动**。`discover.py` 六阶段管道、账号池、图谱引擎、浏览器核验、现有交付链全部保留；V2 决策全部落在 `scripts/extensions/sop_v2/` 新模块，以 scan cache + 证据文件为输入离线运行。
3. **P0 的全部规则逻辑可在零 Instagram 账号消耗下开发与回归**——认证修复（运维+代码）与 V2 规则开发（纯离线）可以**并行**推进。
4. 代码盘点预判的坑已被初跑**在线实证**：
   - Storefront 早筛硬淘汰：4/5 候选 `no_amazon_storefront` 且 posts=0（证据包 scan cache 核实），双轨改造必须旁路 stage2（discover.py:933-940）与 stage3 两道闸；
   - posts 已含 `play_count`/`view_count`、无置顶标记（真实 scan cache 20 帖字段核实）；
   - 错误未转 Review/重试任务：run `161040` 5 个候选全 Error 终态，COLLECT-02 未实现；
   - 无断点续跑：112 人发现成果随 Session 失效作废。
5. **版本漂移**：操作机（`/Users/myb/Desktop/营销`）已有 `--warm-only`、独立 run 日志、`reports/run-audits/` 审计 JSON（含 sop_checks 框架）、`config/sop_v2.toml`、`config/modash_cost_policy.toml`——**均未回推本公开仓库**。双源漂移必须在 R0 消除，否则清单与代码对不上。
6. 旧管道另有两条隐藏线（V2 路径需停用）：Modash `credibility<0.60` 自动 exclude（L1766）；ER 双零硬 exclude（L1054-1058）。
7. 本仓库克隆后 `data/` 全空——测试 fixtures 必须合成/脱敏；初跑证据包可作 fixtures 素材（已含真实字段结构）。

---

## 1. 现状与实证

### 1.1 稳定核心（代码核实，保持兼容）

| 能力 | 位置 | 关键事实 |
|---|---|---|
| 六阶段线性管道 | `scripts/discover.py`（2320 行） | 种子(1a-1f)→回扫→六层漏斗→评论→评分→JSON/CSV+scan cache |
| 种子源 | discover.py 阶段1 | 品牌 tagged/mention（旧管道主力；**v1.3 起降为通道B辅助，主发现见 §2.1 通道A**）、关键词 search_users、Lookalike、品牌帖点赞者、Modash CSV 导入；hashtag 默认禁用（软封） |
| 回扫早筛 | stage2 L928-941 | 无橱窗 → exclude + `posts=[]` 跳过深扫（**初跑实证 4/5 候选在此出局；V2 双轨接入点之一**） |
| 漏斗门槛 | stage3 L993-1070 | 粉丝 10K-150K[硬 L995-999，初跑实证杀掉 3,157 粉候选]、品牌号[硬]、30 天活跃[硬]、无 Storefront[硬，双闸]、产品推荐<20%[软]、赞助>40%[硬]/>30%[软]、ER 低于基准[软]但**双零→硬 exclude（L1054-1058）** |
| Bio/Amazon 检测 | `_check_bio_links` L1118 | 直链→True；bio 文本→True；聚合页→`"unverified"`；`PENETRATE_LINKTREE=False`（L105） |
| 内容画像 | `_analyze_content` L1278 | product_rec_ratio：≥0.40 amazon_finds / ≥0.20 mixed / <0.20 lifestyle；权威度=Σ(4类目命中×权重 3/4/3/2)×5 封顶 100 |
| 赛道对口 | `_detect_niche` L1228 | 11 赛道，bio 命中×3 + caption×1；fit=core/related/off，评分系数 1.0/0.9/0.55 |
| 评论信任 | `_analyze_comments` L1622 | 三档购买意图（STRONG/MEDIUM/WEAK）、bot 检测、pod 三信号（水军库/跨帖刷评/泛泛吹捧）；采样=Top4+Recent4 去重(≤8帖)×每帖 25 条 |
| 现有评分 | stage5 L1742-1852 | 6 维加权（有 Modash 时 8 维）×赛道系数；score_breakdown 全透明；credibility<0.60 隐藏线（L1766） |
| posts 字段 | `_compact_media` L461/L487 | pk/caption/media_type/like/comment/sponsor_tags/**play_count/view_count**（无置顶标记）——初跑真实 scan cache 已核实同构 |
| 图谱引擎 | `discover_graph.py` | BFS 多跳、creator_index 52 列、FTS5、PageRank；晋升未启用 |
| 浏览器核验 | `verify_browser.py` | Playwright 穿透聚合页/数橱窗商品图/截图留证 |
| 交付链 | `export_xlsx.py`（5 sheet）/`build_report.py` | 现行 Excel+HTML，V2 另建五池导出不改旧链 |
| 账号池 | `account_pool.py` + `instagram_session.py` | 暖 Session→完整 Cookie→一次性冷登录；轮换/冷却/防烧号 |

### 1.2 初跑实证（操作机，2026-07-14，3 次在线 run）

| Run | 到达阶段 | 关键结果 |
|---|---|---|
| `160745` | 发现→Profile→旧漏斗→JSON/CSV | 4 品牌 tagged/mention 发现 112 去重候选；受控取 5 人：4 人 Storefront 早筛出局（posts=0）、1 人回扫 20 帖后因粉丝 3,157 出局；评论 0；0 Include/0 Review/5 Exclude |
| `161040` | 发现→Profile | 再次发现 112 人；暖 Session 中途 `login_required`，5/5 Error（未误记 Exclude）；发现成果无法续跑 |
| `161420` | 账号池预检 | 22/22 账号 warm-only 恢复失败（暖 Session 文件仅 14 个），全部 30 分钟冷却，Stage 1 前安全停止 |

**卡点归纳与承接映射**（引自初跑报告 §10）：卡点 1-4（认证模型不一致 / 单账号容量不稳定 / 账号池无健康容量 / 无断点续跑）→ §3 R0（R0-1/2/3/5 与 R0-4）；卡点 5（Storefront 早期误淘汰）→ P0-10 与 §2.3 第 1 点；卡点 6（评论/评分/Modash/浏览器证据/五池均无在线证据）→ 首批验收 Runbook 步骤 3-8 的**在线验收追踪**承接（对应 REQUIREMENTS_CHECKLIST 的 COLLECT-05/SCORE/ROUTE/DEL-01 在线验收状态，防"无编号即无人认领"）。另：初跑已通过基础验证的 **DISC-03（tagged/mention 发现）与 DISC-08（去重）**在此记录 ID 锚点，P0-11 回归时保持通过。

**认证问题首要排查线索**（登录 SOP 对照）：`INSTAGRAM_LOGIN_SESSION_SOP.md` 固定原则 6 要求**浏览器与采集客户端走同一代理出口**（`IG_PROXY`，Clash `http://127.0.0.1:7897`），而初跑均为 `--no-proxy`（run 161420 命令原文；160745/161040 依报告 §3 执行环境表）——若暖 Session 是在代理出口下建立的，直连出口使用**极易触发** `login_required` 类风控（已知高风险因素，非必然：run 160745 同为直连曾短暂跑通）。此为 R0-1 诊断的第一假设，其次是 Session 文件过期/缺失（22 账号仅 14 个文件）与批量同 IP 风控。**注意 README 运行示例本身带 `--no-proxy`，与 SOP 原则 6 自相矛盾，纪律源必须统一（见 R0-2）**。

### 1.3 版本漂移（操作机 vs 本仓库）

| 能力 | 操作机 | 本仓库 v0.1.1 |
|---|---|---|
| `--warm-only` 旗标（禁止冷登录） | 有 | **无** |
| 独立逐行 run 日志（logs/discover-*.log） | 有 | **无** |
| `reports/run-audits/*.json`（含 sop_checks/risk_markers 审计框架） | 有 | **无** |
| `config/sop_v2.toml` | 有（内容待核） | **无** |
| `config/modash_cost_policy.toml`（预算占位单价） | 有 | **无** |

→ R0-6 必须先把操作机改动**脱敏回推**本仓库（或明确以仓库为准重建），此后所有开发以仓库为唯一事实源；操作机上已有的 `sop_v2.toml` 与本清单 §2.5 口径逐项 diff 合并。

### 1.4 Modash 真实账号口径（2026-07-14 只读核对，取代早期纯文档调研）

- 套餐：**$12,000 Custom Plan yearly**，已排程约 12 个月后取消 → 需客户确认续订责任人。
- 余额桶（重置 2026-07-30）：Profiles 剩 **1,310**、Emails & Exports 剩 **929**、Monitoring 剩 298、Influential Fans profiles 剩 6,000、**Influential Fans linked accounts 剩 5（1/6 已用）**。
- 项目自设纪律（`modash_cost_policy.toml`）：Profile 本账期最多再开 **~110 个**（每轮≤20、每日≤30、保留 80%）；导出 ~129 行余量且**只导 shortlist**（每轮≤20、每日≤30、保留 80%）；筛选期**不解锁邮箱、不新增 Monitoring、Influential Fans 仅 preview（≤30 个）且保留≥90%、不新增 linked accounts、API 关闭**。
- 流程含义：**"大池不导出"**——缩池靠搜索结果页可见字段（Handle/Followers/ER，插件只读），Profile Report 只补终选候选的假粉/受众国家/语言/合作史缺口；新 Search/AI/Lookalike/Save 动作先做一次余额 canary 再批量。
- 数据边界不变：Modash 不能替代评论语义、Raw Skin/VO、Storefront 核验、实际报价（成本报告 §6）。

### 1.5 Modash Discovery 浏览器实测（2026-07-14，只读 + 单次筛选 canary）

来源：`MODASH_DISCOVERY_BROWSER_RESEARCH_20260714.md`（客户 Chrome 已登录会话，未点 View/未导出/未解锁邮箱/未 Bulk Save）。

- **零消耗边界实测**：读取已有搜索结果、三页翻页、切换一次 Account Type 筛选，五个余额桶零变化。**新的 Legacy 搜索、AI/Image Search、Lookalike、Save 的成本仍未验证**——每类动作首次执行前单独 canary。
- **结果页免费字段仅**：Handle、名称、Instagram URL、Followers、ER%、Engagement（Display 菜单无法加国家/假粉/语言/合作列）→ 结果页只能做召回缩池；**Modash 数值类硬门槛（Fake/ER/国家/受众/语言）必须由 Profile Report 原值 + gates.py 严格复判**（页面筛选是 ≥2%/≤25%，SOP 是严格 >2%/<25%）；Storefront/Raw Skin/VO/评论/CPM 类门槛证据源仍是 IG/浏览器/人工/报价（实测报告 §6）。
- **UI 总数不可信**：界面标 40 lookalikes 实读 41 个唯一 Handle → 本地入库按实际行数 + handle 去重。
- **Account Type=Creator 是有效第一层降噪**（40→28，剔除多个品牌号），但仍留泛生活号 → 只作降噪，GATE-10 品牌号判定仍在代码层。生产：Creator 主池、Regular 补充池、Business 不进个人池，双池分别记来源。
- **Topics 无 taxonomy 命中**（输入 skincare 无可选项）；**Collaborations 品牌索引不全**（omnilux 无条目）→ 竞品/SHEIN/Temu 合作不能依赖搜索过滤器，用 Mentions/Captions 召回，判定以 Profile Report 合作史 + IG 原帖为准。
- **AI Search 结果卡信息密度高**（帖子 Views/Likes/Comments、近期品牌合作可见），适合"真人讲解/自然光护肤/红光设备"内容风格召回；新查询成本与准确率待 canary。
- **数量漏斗目标**：结果页 100-300 → 基础降噪 50-100 → IG Profile/近帖 30-50 → 内容/评论深扫 15-30 → Profile Report 10-20 → Include 1-10 不凑数。与 Profiles 预算纪律（轮≤20/账期≤110）自洽。
- **对认证问题的意义**：Modash 前置大幅降低 Instagram 候选量与请求压力（初跑 tagged 噪声实证：5 取 5 全出局），但批量近 15-30 帖 + 评论语义仍必须走稳定 IG 采集——**R0 不因此降级**；证据不足仍只能 Review，不能因 Modash 指标好而 Include。
- **内部口径更新**：`ARCHITECTURE.md` 原"Modash 不是默认发现器"边界与客户 SOP §12（Discovery 为步骤 1）及本实测结论不一致 → 随 R0-6 文档回推一并修订为"Modash-first 发现 + Instagram 证据主源"。

### 1.6 IG 采集通道决策：浏览器优先（v1.4 新增，来源 `browser-cdp-lab` 参考实现）

**背景**：代码核实——当前 IG 数据 100% 走 instagrapi 私有 API（discover.py 的 `user_info_by_username_v1`/`user_medias_v1`/`media_comments`/`search_users`/`fbsearch_suggested_profiles`/`media_likers`/`chaining`）；Playwright 浏览器仅用于站外核验（`verify_browser.py` 打开 bio 聚合页/Amazon，不登录 IG）；CDP 仅用于 Cookie 导出（`export_browser_cookies.py`）。初跑悖论：**网页 Chrome 登录态存活，私有 API session 死亡（22/22）**——两条独立认证通道。

**决策**：Modash-first 将 IG 请求量降一个量级后，把 **IG 采集默认通道从私有 API 改为登录态浏览器（CDP）**。理由：绕开私有 API 风控墙（初跑的实际死因）、复用存活的网页会话、低频场景速度非约束（"挂着跑"即可）。instagrapi **保留为可选快通道**（会话健康时用于批量评论等浏览器最慢的场景），不删除。

**参考实现**：`browser-cdp-lab`（shell 拥有 Chrome 生命周期、Playwright 仅 `connectOverCDP` 观察、`open -n` 独立实例防 shell 退出带走进程、`.run/pids` 追踪、ready 检查）。本项目 `start_instagram_cdp.zsh` 已是同款模式（且多一层端口 owner-profile 校验）；吸收 lab 的**多实例管理 + PID 文件 + lib 配置单测**即可，无需另起炉灶。lab 的 `.run/x-audit/` 已实证登录态抓 X profile。

**可行性（按数据需求）**：Profile/bio/粉丝/链接 = 完全可替代（最稳）；近帖/caption/赞评 = 可替代（较慢，Reels play_count 页面可见）；评论语义 = 可替代（最慢，量小可接受）；发现 tagged/关键词 = 较难，**但已由 Modash 承担可忽略**。真实代价：DOM 解析比 API JSON 脆（IG 改版需维护）、慢。

**设计（后端接口隔离，不动骨架）**：新增 `browser_collect` 采集后端，驱动登录态 Chrome（CDP）读页面，**产出与 instagrapi `_compact_media`/profile 完全同构的候选 dict/posts 结构**；discover.py stage3-6（漏斗/评分/评论分析，纯逻辑）零改动。采集后端选择由旗标控制（`--collector browser|api`），默认 browser。

**对 R0 的重塑**：浏览器优先后不再"非要"把 cookie 转私有 API 暖 session。R0 的目标从"恢复私有 API 账号池"转为"维护 N 个登录态 Chrome profile"：
- R0-1/2/3/5（分诊/代理一致/会话重建/保活）**改为面向 Chrome profile 而非私有 API session**——登录态 Chrome 存活性远高，运维更简单；
- 冷登录/TOTP/烧号防护那套机器**降级为 instagrapi 快通道专用**（仅在明确启用 `--collector api` 时相关）；
- 健康池门槛"≥5 暖 Session"改为"≥N 个登录态 Chrome profile"（N 待定，浏览器并发低、单 profile 吞吐低，可能需要 profile 数 ≥ 并发目标）。

详见 §2.6 与执行清单 B0 track。

---

## 2. 开发设计（不动骨架）

### 2.1 数据流

```text
┌─ 候选发现（Modash-first 双通道）──────────────────────────────┐
│ 通道A(主) Modash Legacy/AI Search 结果页只读缩池              │
│   结果页只读零 Profile 消耗(新搜索动作首次先 canary)；        │
│   不点 View、不导出；本地记录可见字段                         │
│   + filters_snapshot → search_pool_import.py                  │
│ 通道B(辅) Instagram 品牌 tagged/mention/Lookalike 种子        │
│   含客户批准者回流；账号池压力大时可降为 0                    │
└────────────────────────┬──────────────────────────────────────┘
                         v  合并去重后的 Handle 池
┌─ 稳定核心（不改逻辑，只加 --v2-collect 旗标）─────────────────┐
│ discover.py --v2-collect [--warm-only] [--resume <run_id>]    │
│   Stage1 发现/接收 Handle 池(候选池全量持久化) → Stage2 回扫  │
│   (30帖+置顶;                                                 │
│   橱窗早筛旁路) → Stage3 旧漏斗(V2:Storefront/粉丝只标记)     │
│   → Stage4 评论(Top10+Recent10,每帖≤10)                       │
│   → Stage5 旧评分(对照;credibility旧线停用)                   │
│   → scan cache / JSON / run-audit                             │
└────────────────────────┬──────────────────────────────────────┘
                         │  离线输入（可反复重跑，不烧号）
                         v
┌─ V2 增量层 scripts/extensions/sop_v2/ ────────────────────────┐
│ ① merge.py     多源合并 → FieldEvidence                       │
│     inputs: scan cache + Modash 搜索页缩池记录 + Profile      │
│             补数记录(lookup_log) + shortlist 导出适配         │
│             + verifications-*.json + data/manual_evidence/    │
│ ② gates.py     硬门槛 + 可采集性分流 → GateResult             │
│ ③ scoring.py   A-F 100 分 + N/A 归一化 + AI Score + 9.5 封顶  │
│ ④ routing.py   固定 Review 优先 → 五池互斥                    │
│ ⑤ batch.py     batch_id / manifest / SHA-256 / 证据索引       │
│ 编排入口 run_v2.py：同输入必得同输出（可复现）                │
└────────────────────────┬──────────────────────────────────────┘
                         v
  export_v2_xlsx.py（8 sheet） + build_report.py V2 增量段
  reports/deliveries/<batch_id>/ …… import_client_feedback.py 回流
```

### 2.2 新增文件清单

```text
config/
  sop_v2.toml                      # 全部 V2 阈值/词表/池规则（与操作机版本 diff 合并）
  modash_cost_policy.toml          # 预算上限/保留线/canary 规则（自操作机回推）
scripts/extensions/sop_v2/
  contracts.py  merge.py  gates.py  scoring.py  routing.py  batch.py  run_v2.py
scripts/extensions/integrations/modash/
  import_modash_export.py          # shortlist 导出 CSV/XLSX → FieldEvidence + 字段映射报告
  lookup_log.py                    # Profile 补数记录 + 预算台账（run/handle/action_id/时间）
  search_pool_import.py            # 搜索结果页缩池记录导入（主发现通道；字段契约：handle/profile_url/
                                   #   display_name/followers_visible/er_visible/engagement_visible/
                                   #   search_mode/seed_or_query/filters_snapshot/result_page/captured_at/
                                   #   profile_opened=false/export_used=false；后续证据层合并不覆盖原始值）
scripts/
  pool_health.py                   # R0：账号池健康诊断/分诊/预检门（或并入 dev/ 现有工具）
  import_manual_evidence.py  export_v2_xlsx.py  import_client_feedback.py
tests/
  test_gate_boundaries.py  test_scoring.py  test_five_pool_routing.py
  test_export_v2.py  test_redaction.py  test_resume.py
  fixtures/sop_v2/                 # 合成候选（可采用初跑证据包脱敏结构）
```

> 位置说明：规则模块放 `scripts/extensions/sop_v2/`（ARCHITECTURE 四层结构的增量扩展层，有意偏离 GAP §5 顶层建议）；三个顶层脚本沿用 GAP §5 位置，属独立新模块。GAP §5 的 `import_modash_browser_evidence.py` 职责由 `lookup_log.py` + `search_pool_import.py` + `import_modash_export.py` 三文件分担（对应成本报告的三条取数通道）。

### 2.3 稳定核心的触碰点（旗标隔离，含 R0 新增）

`discover.py` 增加 `--v2-collect`（默认关闭，关闭时行为逐字节不变），共七点（六个旗标改动 + 一个 Handle 池注入接口）：

1. **Storefront 非门槛化（两处接入点）**：stage2 早筛 **L933-940** 与 stage3 漏斗分支都改为记录 `storefront_status` 并**继续**采集（初跑实证：只有此改动落地，Without-Storefront 候选才有内容数据）；
2. **粉丝区间按 track 放宽**：采集边界取自 `sop_v2.toml`（Paid 10K-150K；Gifting 5K-50K），越界只标记，最终判定交 gates.py；
3. **回扫窗口 30 帖**：`candidate_posts` 取 `max(30, 配置值)`；
4. **补采置顶标记**：play_count/view_count 已有（初跑 scan cache 核实），仅补置顶标记；
5. **评论采样 V2 口径**：Top10+Recent10 去重（≤20 帖）、每帖≤10 条；
6. **旧隐藏线停用**：credibility<0.60（L1766）与 ER 双零硬 exclude（L1054-1058→改 Review）；
7. **Handle 池注入接口**（通道A→采集层的衔接，v1.3 新增）：`--handle-pool <file>` 接收 search_pool_import.py 产出的去重 Handle 列表（复用现有 Modash CSV 种子源入口，字段映射按 §2.2 契约），Stage1 与通道B 种子合并去重后一并落盘候选池；来源标记 `modash_search:<search_mode>` 进 discovery_sources。

R0 另需两个**与旗标无关**的管道级修复（同样最小侵入，见 R0-4）：
- **候选池持久化**：Stage1 完成后先落盘全量去重候选（`candidates-pool-<run>.json`，初跑 112 人即因未持久化而作废）；
- **断点续跑**：`--resume <run_id>` 从候选池/已完成进度继续深扫，账号失效自动轮换续跑，不重复发现；候选级错误进入重试队列，重试耗尽转 Review（COLLECT-02）。

### 2.4 数据合同（contracts.py）

```python
FieldEvidence: value, raw_value, source(modash|instagram|browser|manual|quote),
               captured_at, evidence_ref, status(available|missing|pending|not_applicable|conflict)
GateResult:    gate_id, result(pass|exclude|review), observed, threshold,
               reason_code, source, evidence_ref
ScoreItem:     module(A-F), item, earned, available, reason, evidence_ref
BatchManifest: batch_id, sop_version, campaign_track, config_sha256,
               source_sha256[], 参数快照, 各阶段计数, 错误/重试/缺失统计,
               关联 run_id 列表（一个批次可能跨多次采集 run——初跑教训）
```

原则：扩展现有候选 dict（新增 `field_evidence`/`gate_results`/`sop_v2_score`/`final_pool`），不改旧字段；缺失一律 missing/pending/N/A，禁止填 0；非 Instagram 输入拒绝并记录（SCOPE-01）。

### 2.5 config/sop_v2.toml 必须固化的口径（防散落）

分档一律**半开区间**消除恰值歧义；`[CONFLICT]` 条目同时进 §5 冲突台账。**先与操作机已有 sop_v2.toml diff，以下为目标口径**：

| 组 | 内容（全部来自客户 V2 固化规则） |
|---|---|
| track | paid: 10K-150K 含边界；gifting 标准 5K-<30K；gifting 优先 30K-50K→硬红线与固定 Review 都未触发时无论分数落 Priority Review；>50K 不进 Gifting；track 必须显式指定 |
| country | tier1 = US/CA/UK/DE/IT/FR/ES；tier2 = NL/BE/CH/SE（已确认可 Include）；Creator=Top Audience 不一致→Exclude |
| modash_gates | fake_pct: <25 过，≥25 Exclude；general_er: >2 过，≤2 Exclude；core_fields = [fake_pct, general_er, creator_country, top_audience_country]（**项目默认名单，待客户确认**）缺任一→固定 Review |
| audience | 目标国受众合计 ≥50 满分 / [35,50) 部分分 / **<35→Review（不被高分覆盖）**；Top Audience Language ≥50% 匹配市场主语言，不满足或缺失→Review；年龄/性别恒 N/A |
| sponsorship | <30% 过；[30%,40%] 含两端 Review；>40% Exclude；窗口近 15 帖 |
| shein_temu | 回看 12 个月 + Modash 全部可见历史；仅合作语境命中；日期未知但命中仍 Exclude；普通提及不淘汰 |
| storefront | confirmed_yes/confirmed_no 均可 Include；unknown→Review；活跃=近 3 个月更新；无商品数/分类数要求 |
| collectability | 私密/不可读→Exclude(private_account)；临时失败重试（次数/冷却入 config），耗尽→Review 不误杀（初跑 5 个 Error 即此场景） |
| comments | Top10+Recent10、每帖≤10；有效样本≥20 否则固定 Review；高意图三档全口径：满分 6=≥5 条**且**≥15%；[5%,15%)=3；<5%=0；**≥15% 但 <5 条→3 分档**（默认待校准）；低质 [0,25)=3/[25,40)=2/[40,50)=1/[50,∞)=0 且 Review [CONFLICT-低质恰值] |
| anomaly | ≥5 帖且单帖互动集中度≥70%→Review；禁用"Reels ER>2% Review"字面规则 [CONFLICT-ReelsER，docx §15 已裁定] |
| niche_routing | Lifestyle 主赛道→封顶 Review，产品/护肤证据充分（人工证据导入）后可提升 |
| scoring | A15/B15/C20/D15/E20/F15 全子项显式入 config；A2 导购型四档 [40,∞)=5/[30,40)=4/[20,30)=2/[0,20)=0；C1 IG 互动率基准 micro reels≥3.0/static≥1.8、mid≥1.5（达标 5/略低 2，与 seeds.toml 现值一致）；B 模块成分/设备规格/皮肤问题三类词表独立成组（区别于发现侧 seeds 词表）；Fake [0,15)=6/[15,25)=3/≥25 淘汰；organic ≥10=2/1-9=1/0=0（中间档默认）[CONFLICT-organic]；C5 Save/Share 缺失→**N/A 不扣分不进 Review**（客户覆盖）；D2 confirmed_no 时 N/A；E4 恒 N/A |
| cpm | 仅实际 USD 报价；曝光=非置顶近 10 Reels 均播（缺失 fallback Modash 均播并标 source）；F1: ≤$35=8 / ($35,$40]=4 且 Review / >$40 Exclude [CONFLICT-F1分档]；Gifting 记 0 成本、不算 CPM、F1 N/A；Paid 缺报价→固定 Review |
| elite_brands | Omnilux/CurrentBody/Therabody 每命中 +1.5 封顶 4；红光面罩+VO 可直接 4；窗口 12 个月 |
| visual | 近 30 帖、≥2 条证据；A=4/B=2/C=0；Include 最低 B；C/无证据→Review；VO 人工 |
| ai_score | **分层判定用 normalized_total（≥75/[65,75)/[50,65)/<50），AI Score=round(nt/10,1) 仅展示**；9.5+ 按 docx §13 严口径六条件，否则封顶 9.4 [CONFLICT-9.5VO] |
| fixed_review | Modash 核心字段缺失 / Storefront unknown / 有效评论<20 / Raw Skin或VO 未核验 / Paid 缺报价 / 受众合计<35% / 语言不满足——高分不得覆盖 |
| merge_priority | Modash 优先：粉丝/General ER/假粉/国家/受众/语言/合作史；Instagram 优先：内容/赞助/评论/视觉；人工最高；冲突记 conflict |
| seeds | 品牌种子 Omnilux/CurrentBody/Therabody；产品词/场景词/Lookalike/排除词 #shein #temu（DISC-01/05/06/07） |
| discovery | Modash-first 双通道：**Paid 搜索模板**（Creator 主池 + Regular 补充池、Business 排除；10K-150K；Location 按 Tier1/2 分别执行；ER≥2%、Fake≤25% 仅作召回；Posted within 30 天不足放宽 90 天；Active creators 开启；Bio/Captions 词表 skincare/beauty device/red light/LED mask/anti-aging/acne/skin recovery；Mentions currentbody/omniluxled/therabody）；**Gifting 模板独立跑**（5K-<30K 与 30K-50K，不与 Paid 混用）；**AI Search 按意图拆小查询**（讲解型护肤教育/红光设备真实使用/自然光低滤镜/带教育 VO 的 Amazon 美容设备推荐）；**AI 每个查询叠加 Followers/Location/ER/Fake/Active/Account Type 基础过滤，先比结果质量再决定是否用 Image Search/Lookalike**；Gifting 除粉丝档外其余条件与 Paid 一致；Mentions 清单开放可扩（currentbody/omniluxled/therabody 等）；结果页只读不 View 不导出；UI 总数不可信按实际 handle 去重；数量漏斗 100-300→50-100→30-50→15-30→Profile 10-20→Include 1-10；**降噪与深扫排序承接成本报告"两源命中"要求：≥2 独立来源命中（Modash 搜索/IG 种子/客户回流互为独立源）优先进深扫队列，人工 Approved/客户回流可例外，单源候选仅在池量不足时按分数递补**；每类新动作（新 Search/AI/Image/Lookalike/Save）首次执行前 canary；Collaborations 索引不全，品牌合作用 Mentions/Captions 召回、以 Profile Report 合作史+IG 原帖判定 |
| graph | 图谱来源未走完整审计→封顶 Review |
| brand_account | 非个人 creator→Exclude；医生/诊所→项目默认 Review（偏离 docx §4 默认，待批示）[CONFLICT-医生诊所] |
| modash_budget | 引用 modash_cost_policy.toml：Profile 每轮≤20/每日≤30/账期≤110/保留 80%；导出只限 shortlist，**每轮≤20/每日≤30/保留 80%（≤129 行余量）**；**禁止大池 Bulk save，Save 仅限 shortlist 级且先 canary**；邮箱解锁=0；Monitoring=0；Influential Fans 仅 preview（≤30）且保留≥90%、不新增 linked accounts；新动作先 canary；付费动作全部记台账（run/handle/action_id/时间），重跑不得重复扣费 |
| collector | 默认 browser（登录态 Chrome/CDP）；api（instagrapi）为可选快通道；节奏参数（单 profile 并发、请求间随机停顿区间、每 profile 每日上限）入 config；登录态失效（跳登录页）即停该 profile 转 Review |

### 2.6 采集后端接口（Collector 协议，B0 track）

```python
class Collector(Protocol):        # ApiCollector / BrowserCollector 两实现，同一契约
    def fetch_profile(handle) -> dict        # 与现 user_info 同构：followers/bio/bio_links/external_url/category/is_verified/...
    def fetch_posts(handle, n) -> list[dict] # 与 _compact_media 同构：pk/caption_text/media_type/like/comment/sponsor_tags/play_count/pinned/taken_at
    def fetch_comments(post, sampling) -> list[dict]  # 与现评论采集同构，供 _analyze_comments
```

原则：discover.py stage3-6 只依赖上述**同构 dict**，不知道也不关心后端是浏览器还是 API（后端选择由 `--collector` 旗标决定）。`ApiCollector` 是现有 instagrapi 逻辑的零行为封装（回归基准）；`BrowserCollector` 驱动登录态 Chrome 读页面 DOM 产出同构结果。B0-5 的核心验收：同一候选两后端产出的候选 dict 关键字段一致（结构一致性测试）。

---

## 3. 执行清单

> 状态：`[ ]` 未开始 / `[~]` 进行中 / `[x]` 完成。完成必须同步更新 `REQUIREMENTS_CHECKLIST.md` 状态。R0 与 P0 可并行（R0 偏运维+采集层，P0 偏离线规则层）。

### R0 —— 认证阻断修复与单源恢复（新增，最高优先）

| # | 任务 | 依据 | 文件/动作 | 验收标准 |
|---|---|---|---|---|
| R0-1 | **账号池分诊工具**：逐账号输出失败原因分类（session 文件缺失 22-14=8 个 / 暖复载 login_required / Cookie 缺失或过期 / challenge），生成健康报告 JSON+表格；区别于现在笼统的"warm-only 禁止冷登录" | 初跑 §10-1/3；登录 SOP 故障判断节 | scripts/pool_health.py（整合 dev/health_check.py、recover_check.py） | 对 22 账号出具逐个诊断结论；**分诊自身预算受限：每账号至多一次轻量验证请求，命中 challenge/429 立即停止该账号并记录**（SOP 原则 7）；报告不含任何凭证 |
| R0-2 | **代理/IP 一致性核查与固化**：查明现存 14 个暖 Session 建立时的出口（SOP 要求 IG_PROXY 同出口，初跑却 --no-proxy）；session 元数据记录建立出口指纹，运行前预检不一致即告警拒跑；**统一仓库所有文档示例命令的代理口径**（默认 IG_PROXY；`--no-proxy` 仅限明确标注且与 session 建立出口一致的例外） | 登录 SOP 原则 6；初跑全程 --no-proxy；README 示例自相矛盾 | instagram_session.py 元数据 + pool_health.py 预检 + README/docs 修订 | 出口不一致时给出明确修复指引而非静默失败；**既有 session 出口不可考时标记 unknown 并强制按路径 A 重建（新元数据记录出口指纹）**；仓库内不再存在与 SOP 原则 6 矛盾的示例命令 |
| R0-3 | **会话重建作业（运维）**：按登录 SOP 路径 A/B 批量重建——客户 Chrome 有效登录的账号导完整 Cookie → `instagram_session.py --update-account-pool`；无浏览器登录态的账号走独立 profile 人工登录；**永不删 session、不连续密码重试**；playbook 内含补号数量估算（README 实测单机房 IP 下 20 个 cookie 仅 3~4 个直接可用） | 登录 SOP 路径 A/B；README 账号池铁律 | 运维 playbook（docs/ 补一页操作单） | 达到**最低健康池 ≥5 个可用暖 Session**（**项目自定门槛**，依据 rotate-every 4 × cooldown 30min × 单批请求量的容量推算，待运维确认；降级口径：≥3 可跑受控小批、<3 拒绝开批）；重建过程零冷登录额度浪费 |
| R0-4 | **候选池持久化 + 断点续跑**：Stage1 后落盘全量去重候选池；`--resume <run_id>` 续跑深扫；候选级失败进重试队列，耗尽转 Review（COLLECT-02 运行时化）；账号失效自动轮换续跑不重新发现 | 初跑 §10-4/§11（112 人作废、5 Error 终态） | discover.py（最小侵入）+ tests/test_resume.py | 模拟中断后 resume：不重复发现、不重复已完成深扫；Error→重试→Review 链路有测试 |
| R0-5 | **暖 Session 保活巡检**：低频轻量 keepalive（account_info 级，带抖动，冷却期不碰），健康状态入池状态文件；**命中 login_required/challenge 的账号立即移出保活名单、只读不请求，按 SOP 回浏览器重导 Cookie**；配合 `--wait-pool` 纪律 | 初跑 §10-2；README 铁律；SOP 故障判断节 | scripts/pool_health.py --keepalive 或独立 cron 文档 | 连续 N 天（config 可配）巡检期间 challenge/login_required 计数为 0；频率与抖动参数入 config；池健康数可查询 |
| R0-6 | **版本回推消除双源漂移**：操作机的 --warm-only、独立 run 日志、run-audits 审计框架、sop_v2.toml、modash_cost_policy.toml 脱敏回推本仓库；此后仓库为唯一事实源；**同步修订 ARCHITECTURE.md 的 Modash 边界口径**（"不是默认发现器"→"Modash-first 发现 + Instagram 证据主源"，与客户 SOP §12 及 §1.5 实测一致） | §1.3 漂移表；§1.5 口径更新 | git 提交（脱敏审查后） | 仓库能复现初跑同款命令；操作机与仓库 diff 为零（数据/凭证除外）；文档间无 Modash 定位矛盾 |
| R0-7 | **开批预检门**：健康账号数≥门槛、session 新鲜度、代理一致性、目录契约、config SHA 就绪，任一不满足拒绝开批（防再次 22 连败空转） | 初跑 §7 | pool_health.py --preflight，接入 discover.py 启动 | 预检不过时给出逐项原因；预检本身零 Instagram 请求 |
| R0-8 | **run 审计统一到批次**：把操作机 run-audit（risk_markers/sop_checks）扩展为跨 run 关联（一个 batch 多个 run_id），为 P2-1 manifest 打底 | 初跑 §11"审计未统一" | reports/run-audits/ 约定 + batch.py 雏形 | 初跑三个 run 可事后归入同一批次视图 |

### P0 —— 决策可复现（离线可全部完成，不烧号；与 R0 并行）

| # | 任务 | SOP ID | 文件 | 验收标准 |
|---|---|---|---|---|
| P0-1 | `config/sop_v2.toml` 全量口径固化（§2.5 全表；**先与操作机既有版本 diff 合并**）+ `modash_cost_policy.toml` 回推核对 | 全部 GATE/SCORE/ROUTE + DISC + SCOPE-01 | config/ | 规则模块零硬编码阈值；config SHA-256 进 manifest；CONFLICT 条目可被审计报告读取 |
| P0-2 | 数据合同 `contracts.py`（含 BatchManifest 跨 run 关联） | DATA-04/05/06 | extensions/sop_v2/contracts.py | JSON round-trip；未知值只能是 missing/pending/N/A/conflict |
| P0-3 | 多源合并 `merge.py`（输入：scan cache + 搜索页缩池记录 + Profile 补数 lookup_log + shortlist 导出 + 浏览器核验 + 人工证据） | DATA-01..06 | extensions/sop_v2/merge.py | 冲突不静默覆盖；IG 原值永久保留；方向与 merge_priority 一致 |
| P0-4 | **Modash 三通道取数适配**：①`search_pool_import.py` 搜索结果页缩池记录导入（**主发现通道**，零 Profile 消耗，字段契约见 §2.2；含 config discovery 组的 Paid/Gifting/AI 模板快照）；②`lookup_log.py` Profile 补数 + 预算台账（每轮≤20/每日≤30/账期≤110/80% 保留线，30 天缓存，canary 记录）；③`import_modash_export.py` **仅 shortlist** 导出适配 + 字段映射报告。外部成本：Profile/导出按 §1.4 纪律；新搜索动作首次 canary | AUTH-03/04, DATA-02/03, DISC-01/05/06/07 | extensions/integrations/modash/ | 台账绑定 run/handle/action_id；重跑不重复扣费；预算触线即停；未映射列显式列出；结果页记录带 filters_snapshot 可复现同一搜索 |
| P0-5 | 硬门槛引擎 `gates.py`（GATE-01..12 + 可采集性分流 + SHEIN/Temu 语境匹配 + 图谱封顶） | GATE-01..12, COLLECT-01/02, SCOPE-01 | extensions/sop_v2/gates.py | 边界用例：Paid 9,999/10K/150K/150,001；Gifting 4,999/5K/29,999/30K/50K/50,001；fake 24.99/25.00；ER 2.00/2.01；赞助 29.99/30/40/40.01；CPM 35.00/40.00/40.01 |
| P0-6 | 评分引擎 `scoring.py`（A-F + N/A 归一化 + AI Score + 9.5 封顶；分层用 normalized_total） | SCORE-*/TOTAL-01..04 | extensions/sop_v2/scoring.py | golden fixtures：E4 恒 N/A、D2 confirmed_no、Gifting F1 N/A、C5 缺失 N/A（C 分母 20→17）；fake 15.00/24.99；低质 25/40/50 恰值；"20 样本 3 条=15%"组合档；74.9 不入 Include；9.5 六条件缺一封顶 |
| P0-7 | 路由引擎 `routing.py`（硬红线→固定 Review（含受众/语言）→Lifestyle 封顶→分数分层→Storefront 双 Include；Gifting 30K-50K 插入位置=硬红线与固定 Review 之后） | ROUTE-01..05 | extensions/sop_v2/routing.py | 恰好一池；高分+缺报价不入 Include；Lifestyle 高分封顶；30K-50K+缺报价→Review；受众 34.9/35.0、语言 49.9/50.0 |
| P0-8 | 编排入口 `run_v2.py` | DB-01/02 | extensions/sop_v2/run_v2.py | 同输入重跑逐字节一致 |
| P0-9 | 五池导出 `export_v2_xlsx.py`（8 sheet） | DEL-01..08 | scripts/export_v2_xlsx.py | 结构测试；Herman 空列；缺失显示"缺失"不填 0 |
| P0-10 | `discover.py --v2-collect`（§2.3 七点，含 `--handle-pool` 注入接口）。外部成本：在线验证消耗账号池请求 | GATE-11, GATE-01..03, COLLECT-03/04/05, SCORE-F3 | scripts/discover.py | 不带旗标逐字节一致；带旗标：Without-Storefront（初跑 4 人即用例）与 Gifting 5K-9,999 产出完整 30 帖+评论；置顶标记落库；**通道A-only 输入（仅 --handle-pool，无 IG 种子）可完成回扫全流程** |
| P0-11 | 测试套件 + 合成 fixtures（可采初跑证据包脱敏结构） | QA-01..04 | tests/ | 全绿；覆盖每个边界与每个池 |
| P0-12 | 离线回归：历史 scan cache 跑 run_v2，新旧评分对照（流程性） | — | — | 对照表输出；无未解释翻转 |
| P0-13 | 日志/交付包脱敏扫描 + 无外发断言（联系方式仅记录） | AUTH-05, SCOPE-02, SCORE-F6 | tests/test_redaction.py + batch.py 钩子 | 命中即失败；F6 只记录可用性 |

**P0 完成定义**：同一批 raw 输入在任何机器重跑得到同一 gate/分数/五池结果；所有硬门槛有 observed/threshold/source/evidence。

### P1 —— 证据与人工节点

| # | 任务 | SOP ID | 文件 | 验收标准 |
|---|---|---|---|---|
| P1-1 | 人工证据合同 + 导入（Raw Skin ≥2 条+时间、VO、风险、报价；Lifestyle 提升证据同通道） | SCORE-B4/B5, F7/F8 | scripts/import_manual_evidence.py | 导入后重跑路由确定性变化；Pending 只进 Review |
| P1-2 | Storefront 活跃度 + LTK 人工穿透工作流。外部成本：浏览器访问 | SCORE-D2/D3 | verify_browser.py 契约不变 + manual_evidence | 日期不可见不伪造；unknown→Review |
| P1-3 | CPM 计算（非置顶近 10 Reels 均播优先，fallback Modash 均播标 source） | SCORE-F1..F4 | scoring.py + merge.py | 35.00/40.00/40.01 断言；Gifting F1 N/A（不因 CPM=0 得分也不记 0 分）；估算不作确认值 |
| P1-4 | Modash Profile 补数执行流（客户 Chrome 插件只读；先查 30 天缓存；预算内逐个；证据=值/页面/时间/截图引用）。外部成本：Profiles 配额 | DATA-02, AUTH-01..05 | lookup_log.py 执行侧 | 触线即停；未补数保留 Missing/Review；零凭证落盘 |
| P1-5 | 评论异常互动集中度（≥5 帖且≥70%→Review，CLIENT_CONFLICT 标记） | SCORE-C8 | gates.py | 69.9/70.0 边界 fixture |

### P2 —— 稳定运营

| # | 任务 | SOP ID | 文件 | 验收标准 |
|---|---|---|---|---|
| P2-1 | 批次 manifest + 证据索引（承接 R0-8；跨 run 关联） | DB-02/03 | batch.py | manifest 含 SOP 版本/config/source SHA-256/各阶段计数/关联 run_id；证据可反查 |
| P2-2 | HTML 审计报告 V2 段（五池/gate 原值/A-F/CONFLICT 台账/新旧对照） | DEL-04/05/07, DB-03 | build_report.py 增量段 | 旧报告不受影响 |
| P2-3 | Herman 反馈回导 + 批准者回流 Lookalike + 拒绝者负向标签 | FB-01..03, DISC-02 | import_client_feedback.py + db.py | batch_id+handle 匹配；个例不自动改 config |
| P2-4 | 质量看板（批准率、五池比例、缺失率、硬门槛淘汰率、误收/误杀代理率、Modash 预算消耗、账号池健康趋势） | QA/FB + R0 | db.py stats 扩展 | 每轮可对比上一批全部指标 |
| P2-5 | Modash 操作规范文档（docx §12 全七步 + 成本纪律：结果页缩池模板、canary 步骤、Profile 队列规则、shortlist 导出时点、白名单与另行授权边界；**不动客户账号既有 List/Campaign/套餐**） | AUTH-01..04, SCOPE-02 | docs/sop_v2/MODASH_OPERATIONS.md | 执行者可照做；含余额前后核对表 |

### 首批验收 Runbook（R0 通过 + P0+P1 完成后）

0. **R0 预检门通过**：健康暖 Session ≥5（项目自定门槛，定义与降级口径见 R0-3）、代理一致、config SHA 锁定，否则不开批。
1. 建批：显式选 track；Gifting 批次采集边界 5K-50K 生效；目标：合并去重 Handle 池 100-300（漏斗第一级）→ Include 1-10，不凑数（与成本报告"~100 送 Profile"区分：那是账期级 Profiles 预算概念，非单批目标）。
2. 候选发现双通道（**Modash 为主**）：a) Modash Legacy/AI Search 按 config discovery 模板执行（新搜索先 canary；结果页只读记录 → search_pool_import.py；不点 View、不导出、不 Bulk save）；b) `discover.py --v2-collect --warm-only --wait-pool`（Instagram 种子辅助 + 承接 Handle 池回扫；候选池落盘；中断可 --resume）。
3. `verify_browser.py` 核验 Storefront 三态。
4. Modash 补数：30 天缓存优先 → Profile 队列（≤20/轮）→ 终选 shortlist 才导出并完成**字段映射确认**。
5. 人工证据：Raw Skin/VO/风险/报价模板录入导入。
6. `run_v2.py` → 五池 → `export_v2_xlsx.py` + HTML + manifest；交付前脱敏扫描。
7. 人工抽查全部 Include + ≥20 条 Review/Exclude；同 raw 重跑一致性验证。
8. 交付；回收 Herman 两列回导；只把客户明确确认的新规则写回 config；CONFLICT 台账逐项裁定。

---

## 4. 外部依赖与客户侧待办（阻塞项）

| 依赖 | 影响 | 状态 |
|---|---|---|
| **账号池恢复到 ≥5 个健康暖 Session**（R0-3 运维 + 可能需补充干净新号） | 一切在线采集 | **当前最大阻塞**；22/22 不可用，需按登录 SOP 重建 |
| 代理出口策略确认（Clash 出口 vs 直连；操作机与账号建立环境一致） | R0-2 | 待操作机核查 session 建立环境 |
| Modash 套餐续订责任人确认（已排程 ~12 个月后取消） | 长期运营 | 客户侧 |
| 客户 Chrome 已登录 Modash 会话 | 缩池/补数/导出 | 已确认；2FA 客户完成 |
| 新 Legacy/AI/Image/Lookalike/Save 动作的成本 canary | Discovery 模板正式批量执行前提 | 待执行（实测仅覆盖翻页+一次筛选切换零消耗） |
| Raw Skin / VO 人工核验执行人 | P1-1 | 客户确认人工判定；需指定角色 |
| Paid 实际报价来源 | F 模块 | 缺报价固定 Review，不阻塞交付 |
| 阶段 9"触达/合作结果记录"是否本期范围 | P2-3 模板 | 待确认，暂按范围外 |

## 5. 客户待确认冲突台账（CONFLICT，随首批交付提交裁定）

初跑全部采用"初跑口径"，审计报告标注；**客户确认后才改写 config**。

| # | 条目 | 冲突/空白 | 初跑口径 |
|---|---|---|---|
| C1 | Organic 分档 | 默认"≥3=2" vs 客户"最低 10"，中间档未定义 | ≥10=2 / 1-9=1 / 0=0 |
| C2 | 9.5 封顶的 VO | docx §7"或" vs docx §13"且" | 从严按 docx §13（VO 必要） |
| C3 | F1 CPM 分档 | docx §6F"1-1.5 倍=4"($52.5) vs $40 线 | ≤$35=8 / ($35,$40]=4+Review / >$40 淘汰 |
| C4 | Reels ER 字面规则 | 与 Micro ≥3% 满分冲突 | **docx §15 已裁定**：禁用字面规则、改集中度≥70%；留台账仅作首批交付复核备案，非待裁定项 |
| C5 | 低质评论 40/50 恰值 | 默认表两档重叠 | 半开区间，50 归 Review 档 |
| C6 | 高意图组合档 | "≥15% 但 <5 条"未定义 | 按 3 分档 |
| C7 | Modash 核心字段名单 | SOP 未枚举 | fake/general_er/creator_country/top_audience_country |
| C8 | 医生/诊所账号 | §4 默认 Exclude，例外未确认 | 项目默认 Review 留证待批示 |
| C9 | Gifting 30K-50K"优秀候选"限定 | 未定义"优秀" | 硬红线+固定 Review 未触发即入 Priority Review |

## 6. 风险与降级

- **认证再次阻断**：R0-7 预检门保证不空转；`--wait-pool` + 冷却纪律；发现成果因候选池持久化不再作废；账号补充走"干净好号"渠道而非换登录方式（README 铁律）。
- **Modash 补数不可用/预算触线**：主流程照跑，字段 missing → Review，不阻塞交付；预算台账防超支与重复扣费。
- **Modash 发现通道A不可用**（v1.3 新增）：降级为通道B（IG tagged/mention/Lookalike + 客户回流）为主，**此时通道B不得同时降为 0**；双通道均不可用则本批不开批（挂 R0-7 预检门）。候选极少时 Profile/Bio/Storefront/少量近帖可走普通 Chrome 人工补证（实测报告 §10 已证可行），仅作低量兜底，不替代 R0。
- **评论接口烧号**：V2 采样上限 ≤20 帖×10=200 条与现行上限（≤8 帖×25=200）相当；样本不足→固定 Review。
- **规则冲突**：以 docx §4/§11/§15 固化口径为准，冲突入 §5 台账，不私自仲裁。
- **旧新评分翻转**：P0-12 对照表；只有客户确认的差异才调 config。
- **双源漂移复发**：R0-6 之后规定"操作机只跑不改，改动一律走仓库"；每批 manifest 记录代码版本。

## 7. Modash 字段映射参考（官方文档调研 + 客户账号实测）

### 7.1 适配层要点

1. **List 批量导出完整列清单官方未公开**（只确认均值类列）；且成本纪律为"大池不导出"→ 字段映射以**首批 shortlist 真实导出样例**为准；核心 gate 字段（Fake/国家/受众/语言/合作史）默认走单人 Profile Report 补数（lookup_log 通道）。
2. **口径差异入 Data Dictionary**：Modash ER = 近约 2 个月内容 engagements **中位数**÷followers（2-4 周刷新），与 IG 样本均值 ER 不同源不同法，并列展示不互相覆盖；Discovery ER 与 Campaign ER 公式也不同。
3. **Fake % = 1 − credibility**；V2 门槛 fake≥25% ⇔ credibility≤0.75；旧管道 0.60 线必须停用。
4. 搜索结果页可见 Handle/Followers/ER/Engagement，**不开 Profile 即可缩池**——这是**零 Profile 消耗**的第一道（成本报告仅证明"本次只读未见余额变化"，新 Search/AI/Lookalike/Save 仍须先做一次余额 canary，不得视为永远免费）；公开筛选边界（≤25% 假粉、≥2% ER）与 SOP 严格边界（<25%、>2%）不同，**必须由 gates.py 复判**。

### 7.2 canonical 字段名（Modash RAW API report schema，作适配层规范名）

| V2 所需字段 | canonical 名 | 备注 |
|---|---|---|
| Fake Followers % | `profile.audience.credibility`（1−值） | 官方 >0.7 可接受；V2 ≤0.75 淘汰 |
| Modash General ER | `profile.engagementRate` | 中位数口径 |
| Creator Country | `profile.country` | 另有 city/state |
| Top Audience Country | `profile.audience.geoCountries[]` weight 最大项 | `{name,code,weight}` |
| 目标国受众合计 | Σ geoCountries 目标国 weight | 名单来自 sop_v2.toml |
| Top Audience Language/% | `profile.audience.languages[]` | ≥50% 门槛 |
| 年龄/性别（恒 N/A） | `profile.audience.ages/.genders/.gendersPerAge` | 留档不进分母 |
| 平均 Reels 播放 | `profile.avgReelsPlays` | CPM fallback |
| 平均 Reels 分享 | `profile.avgShares` | C5 辅助，缺失 N/A |
| 合作史（SHEIN/Temu、Elite） | `profile.sponsoredPosts`、brands 端点 | 命中后回原帖取证 |
| Lookalike | `profile.lookalikes`/`audienceLookalikes` | 仅扩池 |

### 7.3 预算与操作纪律（客户账号实测，2026-07-14）

见 §1.4 与 config `modash_cost_policy.toml`：Profile 账期≤110（轮≤20/日≤30/保留 80%）；导出仅 shortlist（≤129 行余量）；不解锁邮箱、不加 Monitoring、Influential Fans 只 preview、API 关闭；新动作 canary；付费动作全部入台账；**不得动客户账号既有 List/Campaign/套餐设置**。

来源：docs.modash.io / help.modash.io / modash.io 博客；`MODASH_FUNCTIONS_COST_REPORT.md`（客户后台只读核对）。
