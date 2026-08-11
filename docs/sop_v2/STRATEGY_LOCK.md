# 采集策略固化（v2 · 2026-07-15，更新至 2026-08-11）

本文件锁定经实测收敛的关键策略，后续开发以此为准；改动需在此登记原因。

## 0. 两条被实测证伪的旧假设
- ❌「随机采前 N 帖找购买意图」→ 全 0。购买意图只存在于**博主的带货/导购推广帖**评论里，
  教育型/生活方式博主（如 carmenbauza 的"评论 GUÍA 领免费指南"引流帖）根本没有购买意图可找。
- ❌「品牌判定必须走 instaloader/`web_profile_info` API」→ 该 API 被 IG 激进限流（429 秒回 +
  instaloader 指数退避 = 多分钟卡死，"太慢"根因）。品牌类目其实**渲染在 profile 页头部 DOM**，
  浏览器一次渲染即可拿到，零额外请求。

## 1. 购买意图：推广帖优先（已落地 comments.py + browser_collect_v2）
1. `comments.is_promotional(caption)`：caption 命中带货词（link in bio / use code / amazon /
   código / disponible en / shopmy / ltk …）才算推广帖。
2. **只在推广帖里找购买意图**；非推广帖跳过评论深扫（省时且避免噪声）。
3. `find_intent_in_text(text, promo_context=True)`：推广帖上下文放宽到"考虑购买"问句
   （does this work / vale la pena / para piel grasa / cuál me recomiendas …）；教育帖只认强买信号
   （where's the link / just ordered）。**同句靠上下文消歧**。
4. `promotional_post_count` 作为"带货型红人"的核心信号入候选。

## 2. 截图：意图评论滚到中央再截，截不到就给链接（已落地）
- `_scroll_snippet_into_view(pg, snippet)`：把含购买意图原话的评论 DOM `scrollIntoView({block:'center'})`，
  保证截图真的拍到那条评论（否则拍到的是图片不是评论）。
- `_load_comments(pg)`：点"查看更多评论" + 右侧评论列（x≈820）滚动加载更多。
- 截图失败**不死磕**：落 `intent_comment_link`（帖子链接 + 原话），供客户自行核验。

## 3. Profile 浅扫 & 品牌判定：浏览器优先，API 退居兜底（本次落地）
- **浏览器渲染 profile 页一次拿全**：粉丝（og:description）、全名/bio、**专业号类目**（头部标签）、
  商业按钮信号、bio 外链锚点、帖子网格 shortcode。
- 品牌判定：`is_business`(有类目或商业按钮) AND 类目/名/bio 命中 BRAND_CATEGORIES → brand，早跳。
- instaloader 只在浏览器拿不到外链时**快速兜底**（`max_connection_attempts=1`，不再多分钟退避）。
- 命中缓存库（30 天新鲜）→ 零请求直接用。

## 4. 限流纪律（避免把号测进冷却）
- **禁止**用裸 `web_profile_info` API 批量探测（秒进 429，连累浏览器 session redirect-loop）。
- 浅扫走浏览器 + 缓存；同一号请求间 `_pause`；号池轮换。
- 号一旦 429/redirect-loop = 临时冷却，**停手等恢复**（分钟级～小时级），不要连续重击。

## 5. 种子：通用电商导购型红人
- 内容关键词种子（"skincare routine"）拉来的是教育/生活方式号 → 无橱窗、无购买意图。
- 改用带货导向 Modash 查询（shopping links / LTK / ShopMy / link in bio / creator shop）→
  命中真带货号；
  **副作用**：会混入店铺/品牌号（hebestore19 等）→ 靠浏览器品牌判定 + 早跳过滤。

Amazon 只是可用电商信号之一，不是 Storefront 白名单。候选出现 Amazon、LTK、ShopMy、
明确自营店或已识别购物聚合入口都算 `confirmed_yes`；确认没有任何 Storefront 的
`confirmed_no` 也继续深采。

## 5b. 客户 2026-07-15 确认变更（已落地）
- **历史口径（已被 2026-08-10 来源策略取代）**：当时 discovery 收窄到 Amazon Finds；
  当前不再以单一平台限定召回，但仍保留护肤/美妆赛道词和排店铺信号。
- **ER 放宽**：Modash General ER **降为参考、不再硬淘汰**（`general_er_reference_only=true`）；
  **实算 ER = 硬门槛**，取**前 10 帖**赞评/粉丝算（`[real_er]`：<0.5% Exclude、[0.5,1.0) Review、
  缺数据 Review 不误杀）。GATE-13 落地。
- **采集全程复用浏览器会话**：`browser_collect_v2` 移除 instaloader/instagrapi 调用；
  profile/品牌/橱窗/帖子网格/赞评主要走浏览器渲染；报价播放指标由同一登录态会话调用
  Instagram 同源 media info。强制英文 locale 保证登出/多语言下解析稳。

## 5c. 账号现状（阻塞项，2026-07-15）
- 老 5 号：过度测 API → 临时限流冷却（redirect-loop）。
- 新 5 号：**2 个死号(登录页) + 3 个"验证你是真人"风控挑战** → 均不可用于登录态采集。
- 登出态只能拿 og 元标签（粉丝+名字）；**bio/橱窗/帖子/评论/类目全被登录墙挡** → 浅扫也需登录态。
- **结论**：评论购买意图、实算 ER、storefront 穿透都需可登录的号；供应商需交付能保持登录、
  无风控挑战的号（我不能代过人机验证）。

## 5d. 客户 2026-07-28 确认变更

- **Storefront 通用化**：Amazon、LTK、ShopMy、明确自营店和已识别购物聚合入口均算有；
  `confirmed_no` 不早淘汰，`unknown` 才 Review。
- **展示型预估报价**：先排置顶，再从剩余 Reels 按发布时间倒序取最近 10 条；默认报价为
  均播×$35/1000，区间为均播×$35–$40/1000。播放证据由登录态浏览器会话读取
  Instagram 同源 media info；均播只使用 IG 原生 `ig_play_count`，总
  `play_count`/`fb_play_count` 仅审计、不得抬价。
- **证据降级必须显式**：10 条=`complete`；严格闭合且实际只有 1–9 条=
  `complete_available`；严格证明没有 Reels=`not_applicable_no_reels` 且报价为 `null`；
  1–9 条但总体未闭合=`partial`；无法证明没有 Reels=`missing`。Modash 均播、总播放和
  Facebook 播放都不能作报价 fallback，历史 `fallback_modash` 会阻断当前 B3。
- **决策隔离**：展示估价不是实际报价，不写 `paid_cpm`，不进入 Gate、F 分、固定 Review
  或五池路由。

## 5e. 两轮反馈后的来源与治理固化（2026-08-10）

- **来源配额**：50% approved/collaborated 金种子 Lookalike、30% 通用电商搜索、20% 主题探索；
  金种子不足时缺口按 30:20 重分配，防止整轮因人工 Lookalike 未完成而停摆，也防止飞轮变窄。
- **轮次合同**：正式 Stage 1 前冻结 carryover 模式、国家、Storefront、CPM/Reels、翻译、
  来源配额、配置 SHA 和代码 SHA；运行前校验漂移。
- **拒绝原因可选**：客户只点“不合适”即可导出；空原因只更新账号状态，不能推断策略。
- **反馈不直改规则**：客户只能提交账号结果或 `policy_signal`。策略必须经 proposed、历史回放、
  测试和人工确认后，才能追加 approved 变更记录。

## 5f. 深采边缘证据固化（2026-08-11）

- **协作帖不猜 shortcode**：超长 access token 只能由页面 Instagram HTTPS canonical 绑定，
  再由同源 media-info 响应 code 回证；双证据闭合后，播放/赞评/置顶/时间/来源/provenance
  整个报价 bundle 一次性更新，禁止截断 token、局部补字段或覆盖已观测样本。
- **空评论不等于零评论**：`verified_empty_thread` 仍保留正数 `reported_count`，必须同时取得
  页面可见叶节点精确 `No comments yet.` 和登录态 comments endpoint 的全空/无更多结果，
  并保存 DOM + endpoint provenance；它不是 `verified_zero`。普通 `reported_count>2` 空抽仍
  严格失败，只有 1–2 条低量媒体可沿既有连续真实失败合同终止重试。
- **Graph 换代码必须换链**：修改 `graph_runner.py` 或 run contract 覆盖的 worker/调度代码后，
  结束旧审计链并新建版本化 schedule/event；新链从所有旧 durable consumer intents 的历史
  最大实际 rotation 加 1 续接 `account_rotation_base`，不得重置为 0。

## 5g. 生产编排、额度与交付固化（2026-08-11）

- **固定 Graph**：正式 Stage 2/3 只能由 `graph_runner` 运行“一浅采 producer + 多深采
  consumer”的流水并发；B1/B2/B3 不得跨越，不能临时改成全串行或手工多终端。
- **尾批与轮换**：每波按 `qualified_unlocked_count` 给 worker 动态均分正数 claim cap，差值
  最多 1；账号切片固定互斥，实际轮换序号为持久 `account_rotation_base + consumer intent
  ordinal`，resume 不归零。
- **报价独立入账**：短总体/无 Reels 报价只通过 pricing-only ledger finalizer 追加 attempt 并
  单调选择 pricing owner；不得推进 deep canonical 或评论 retry state。
- **Modash 草稿预算与正式补数分离**：默认 `route_actionable` / `--modash-cap 20` 只为预算草稿
  购买“可能改变路由”的报告，不能证明正式字段完整。正式交付必须使用
  `--modash-all-missing --modash-cap 0 --strict-enrichment-completeness`，覆盖全部尚无
  `modash_report=true` 的候选；报告已存在但某个源字段为空时交付显示“Modash无”，不得重买。
  一份全新 120 人 cohort 约需 120 个 Profile credit；当前 SKIN6 已有 4 份身份校验缓存，增量为
  116。结构化搜索与 Golden Lookalike 列表浏览不消耗 Profile Report credit。
- **Storefront 独立补证**：B3 后只对 `effective_status=unknown` 的精确 cohort 运行
  `storefront_backfill collect`，人工复核不可覆盖 plan 后再 `apply`；人数、Handle SHA、代理、
  账号/Profile lease、跨账号 unknown 重试、计划 SHA 与逐行 CAS 任一不闭合均整批拒绝。该流程
  只补 Storefront allowlist，不重跑或改写 Stage 3 深采、报价、翻译和 attempt ledger。
- **评论证据不截断**：XLSX“评论证据”表导出决策数据中的全部结构化评论译文/原文行；HTML
  或摘要可展示代表样本，但不能代替全量表。
- **翻译 owner 固定在 Stage 3**：LLM 翻译在 canonical attempt 落库和 B3 前完成。正式
  Stage 4 的 `--strict-comment-translations` 只读校验已存行/摘要/语义，不调用 LLM、不截断、
  不重算或改写 candidate/ledger；`--translate-comments` 只允许草稿/补译，round contract 或
  strict/full-deep 正式模式必须拒绝。
- **正式交付**：deep/pricing/translation 三项新鲜计数与 cohort 对账共同通过 B3 后，按
  “Storefront/Modash enrichment → strict Stage 4 → B4 → export”执行。B4 必须绑定当前 exact
  delivery-state SHA，要求全体 `decided`，并令 `audit/pricing/translation/modash/storefront/
  sponsorship` 六项失败计数全部为 0。B3/B4 工件只供内部审计，不交客户；客户只收到由同一
  决策基线生成的 JSON/XLSX/HTML。已发出的 `formal-...-r1` 永不覆盖，任何修复生成新的 r2。

## 6. 硬门槛与 Modash 预算边界
followers 档 / 赞助饱和 / **Storefront 三态留证（yes/no 都不早淘汰）** / 排品牌号 / 赛道 /
实算 ER（帖量足够大才准）仍先以 Instagram 证据计算。预算草稿只为通过本地阻断、且补齐后可能
改变路由的候选购买 Modash fake%/受众报告；正式交付则不以“是否可能晋级”缩小购买集合，必须
按 all-missing 补齐全部尚无报告候选，再由 strict enrichment 与 B4 统一验收。

## 7. 数据库飞轮（见 DB_FLYWHEEL_DESIGN.md）
机器自动沉淀浅扫/采集（库①②）；客户审核门控优质红人进金种子库③、拒绝进负向库④；
逐条反馈事件与策略变更记录保持 append-only。金种子驱动下一轮 Modash Lookalike，来源批准率
决定后续配额建议，但系统不能自动修改硬门槛。
