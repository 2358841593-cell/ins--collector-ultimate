# 采集策略固化（v2 · 2026-07-15，2026-07-28 补充）

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

## 5. 种子：带货型红人（amazon-finds 导向）
- 内容关键词种子（"skincare routine"）拉来的是教育/生活方式号 → 无橱窗、无购买意图。
- 改用带货导向 Modash 查询（amazon storefront / shop my amazon / link in bio）→ 命中真带货号；
  **副作用**：会混入店铺/品牌号（hebestore19 等）→ 靠浏览器品牌判定 + 早跳过滤。

这里的 Amazon 关键词只是发现阶段的高精度召回信号，不是 Storefront 白名单。候选后续出现
LTK、ShopMy、明确自营店或已识别购物聚合入口，同样算 `confirmed_yes`；确认没有任何
Storefront 的 `confirmed_no` 也继续深采。

## 5b. 客户 2026-07-15 确认变更（已落地）
- **discovery 只做「Amazon Finds 导购型」赛道**：config `[discovery]` 换成 amazon_shopping_guide
  （带货导向 Modash 查询 + 橱窗信号 + 护肤/美妆赛道词 + 排店铺信号）；红光设备种子退役。
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
- **证据降级必须显式**：10 条=`complete`、1–9 条=`partial`、无原生样本才
  `fallback_modash`、均无=`missing`。
- **决策隔离**：展示估价不是实际报价，不写 `paid_cpm`，不进入 Gate、F 分、固定 Review
  或五池路由。

## 6. 硬门槛（不依赖 Modash 的先行）→ 通过者才 Modash 补数
followers 档 / 赞助饱和 / **Storefront 三态留证（yes/no 都不早淘汰）** / 排品牌号 / 赛道 /
实算 ER（帖量足够大才准）→
过硬门槛 → 才用 Modash 补 fake%/受众（客户既有业务流程，走本机已登录 yibo Chrome CDP）。

## 7. 数据库飞轮（见 DB_FLYWHEEL_DESIGN.md）
机器自动沉淀浅扫/采集（库①②）；客户审核门控优质红人进金种子库③、拒绝进负向库④；
金种子驱动下一轮 Modash Lookalike。越用越快、越省号。
