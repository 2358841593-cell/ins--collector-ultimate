# 执行逻辑 ↔ 客户需求 一一对应

更新时间：2026-06-02

本文把客户 brief 的每一条需求，对应到我们 pipeline 的**哪个阶段、什么逻辑、自动还是人工、证据在哪、现状如何**，方便定位优化点。

---

## 一、客户需求 → 实现 对照表

| # | 客户要求（brief 原意） | 我们怎么实现 | 阶段/代码 | 判定类型 | 证据/输出 | 现状 |
|---|----------------------|------------|----------|---------|----------|------|
| 1 | **"Amazon Finds" 导购型**：内容围绕产品推荐，非生活方式/vlog | 近 15 帖 caption 匹配产品推荐词 → product_rec_ratio → 画像分类（amazon_finds≥0.40 / mixed≥0.20 / lifestyle<0.20） | 阶段3.3 `_analyze_content` | **软信号**（lifestyle→标 review，不硬删，因客户说"降级或 exclude"） | `product_rec_breakdown`（命中词 + 算式 6/15=0.4） | ✅ 完整 |
| 2 | **10K–150K 粉丝**，Micro/Mid | follower_count 区间硬过滤；分 micro(<100K)/mid 档 | 阶段3.1 | **硬门槛**（超范围→exclude） | `followers_out_of_range` | ✅ 完整 |
| 3 | **成熟、活跃、有组织的 Amazon Storefront**（bio 直链或经 Linktree/Beacons/LTK） | ①正则扫 bio_links/external_url 找 amazon.com/shop（直链）+ 识别聚合页类型 ②无任何 Amazon 信号→exclude ③聚合页未穿透→review ④**真浏览器(Playwright)打开 bio 链接渲染后读真实链接找橱窗**；直链则打开橱窗数商品图（成熟度） | 阶段3.2 `_check_bio_links` + `verify_browser.py` | **硬门槛 + 自动浏览器穿透** | bio_link_type/url；浏览器核验记录（截图+出链数+橱窗链接+源URL） | ✅ 完整（穿透自动+留证） |
| 4 | **护肤/设备专业度**：skin barrier、成分、波长 wavelength、irradiance | 4 类目关键词（成分/设备规格/皮肤科学/适用场景）加权命中 ×5，封顶 100 | 阶段3.3 权威度评分 | **软信号**（排序加分，不 exclude） | `authority_breakdown`（每类命中词+权重+算式 (2×4)×5=40） | ✅ 完整 |
| 5 | **高信任社区**：高 save 率 + 评论区产品问答/链接请求/购买意图 | 评论购买意图（STRONG/MEDIUM/WEAK 关键词）+ bot/互赞团过滤 + **水军账号库**；**save 率公开 API 不返回** | 阶段4 `_analyze_comments` + 水军库 | 评论=自动；**save=数据缺口** | purchase_intent_ratio / trust_level / 高意图评论原文 / 水军账号列表 | ⚠️ 评论完整，**save 缺口** |
| 6 | **互动率基准**：Micro Reels 3-5%/Static 1.8-3%；Mid Reels 1.5-3% | Reels/Static 分别算 (likes+comments)/followers×100，按档对比基准 | 阶段3.5 `_calc_engagement` | **软信号**（未达→标 review；无数据→exclude） | reels/static_engagement_rate + meets_er_benchmark | ✅ 完整（saves/DM 缺口同上） |
| 7 | **过滤低质评论**："Beautiful!"/"Love this"/纯 emoji/互相 tag | bot 文本模式 + 互赞团三信号（库命中/跨帖刷评/泛泛吹捧） | 阶段4 + 水军库 | **自动** | bot_ratio / pod_ratio / 水军库（104 账号，跨创作者命中） | ✅ 完整 |
| 8 | **购买意图关键词**："Where is the link"/"storefront"/"what wavelength"/"Ordered" | 三级意图词表匹配，排除 bot/pod 后算占比 | 阶段4 | **自动** | intent_keywords_found + top_intent_comments 原文 | ✅ 完整 |
| 9 | **Bio link → Amazon**（无精心 Storefront 不纳入） | 同 #3 | 阶段3.2 + verify | 硬门槛 | 同 #3 | ✅ 完整 |
| 10 | **视觉验证（护肤）**：真实未修图近距离皮肤纹理，排斥滤镜/低曝光/柔焦/环形灯 | 截图候选内容 → Claude 视觉模型判断真人真实度 + 垂类 | verify_browser.py 截图 + 视觉判断 | **可 browser-use+视觉自动**（已做样例） | 内容截图 + 视觉判断文字 | 🔶 部分（做了几个，可批量补） |
| 11 | **赞助饱和度 ≤40%**：近 15 帖 #ad/#sponsored/Paid Partnership | caption 正则 + sponsor_tags 双信号；>0.40→exclude，>0.30→review | 阶段3.4 `_check_sponsorship` | **硬门槛(>40%) + 软(>30%)** | sponsored_count/ratio | ✅ 完整 |
| 12 | **自然内容稳定性**：定期发 organic "Amazon Finds"/"Skincare Routine" | 统计非赞助的产品推荐帖数 organic_amazon_posts_count | 阶段3.4 | **软信号** | organic_amazon_posts_count | 🔶 完整但判定简单（仅计数） |
| — | **赛道对口度**（我们加的）：每个 IG 账号走哪个垂类，是否切合护肤设备活动 | bio+caption 匹配 11 类赛道，判主/次赛道 + 对口度（核心/相关/跨垂类），跨垂类评分×0.55 | 阶段3.3 `_detect_niche` | 软信号（跨垂类降权 + 标 review） | niche_breakdown（命中词）+ campaign_fit | ✅ 完整（新增） |

---

## 二、完整执行流程

```
阶段0  账号池（20 个 TOTP 号）轮换登录 —— 绕开单号限流/挑战
   ↓
阶段1  Seed 采集 → 候选池
   · 品牌账号（currentbody/solawave/therabody）被 tag 的帖子 → 提取作者
   · 品牌帖 caption @mention → 提取
   · （Hashtag 端点账号无权限，跳过）
   · （Modash CSV 种子，会员到位后接入）
   → 去重得候选池
   ↓
阶段2  Profile + Posts 回扫（私有 API，user_info_by_username_v1）
   · profile：粉丝/bio/bio_links/external_url/category…
   · 近 15-20 帖：caption/like/comment/media_type/sponsor_tags…
   · 扫描结果存缓存 scan-cache（含 posts），供离线调参
   ↓
阶段3  多层漏斗（任一硬门槛不过即 exclude）
   3.1 基础过滤   粉丝 10K-150K[硬] / 非品牌号[硬] / 近30天活跃[硬]
   3.2 Bio 门槛   有 Amazon Storefront 信号？无→exclude[硬]；聚合页未穿透→review
   3.3 内容画像   产品推荐占比→archetype[软]；权威度评分[软]；赛道+对口度[软,跨垂类降权]
   3.4 赞助饱和   sponsored_ratio>40%→exclude[硬]；>30%→review[软]
   3.5 互动率     Reels/Static ER 对档基准[软]；无数据→exclude
   3.6 综合判定   有 review_reasons→review；全过→include
   ↓
阶段4  评论意图分析（仅 include/review，省成本）
   · 选帖：互动最高 5 + 最近 5（避开互赞团集中的爆款层）
   · 每帖深抓 ~40-50 条，按评论 pk 去重
   · 跨帖刷评追踪：同号在多帖评论→pod 信号
   · 三信号判 pod：水军库命中 / 跨帖刷评 / 泛泛吹捧
   · 算 purchase_intent / bot / pod / trust_level
   · 新水军账号写回持久库（跨 run 累积，下次直接略过）
   ↓
阶段5  综合评分（透明，非黑箱）
   Σ(6维子分×权重) × 赛道对口系数
   每项给「原始值 × 权重 = 贡献 + 计算依据」
   ↓
阶段6  输出 JSON + CSV
   ↓
核验    verify_browser.py：Playwright 真浏览器逐个核验 review 候选
   · Amazon 橱窗穿透（读真实链接 + 截图）
   · Storefront 成熟度（数商品图）
   · 视觉真实度（截图 + 视觉判断）
   → verifications JSON + 证据截图
   ↓
报告    build_report.py：自包含 HTML 仪表盘（Linear 视觉）
   · 统计 + 漏斗 + 怎么用 + 方法论(折叠)
   · 候选表（赛道·对口度 / Amazon / 互动 / 信任 / 评分 / 待核验）
   · 抽屉：验收建议→赛道→需求达成4项→核验证据截图→评论(含水军)→评分依据
```

---

## 三、自动化能力边界（这决定了哪些必须人工）

| 能力 | 自动 | 说明 |
|------|:---:|------|
| 粉丝/活跃/品牌号过滤 | ✅ | 采集即判 |
| Amazon Storefront 有无 | ✅ | 正则 + 真浏览器穿透 |
| Amazon Storefront 穿透（聚合页背后是不是 Amazon） | ✅ | Playwright 渲染读真实链接（LTK 联盟跳转较难，标 inconclusive） |
| Storefront 成熟度（商品数） | ✅ 部分 | 直链可数图；聚合页背后需人工 |
| 内容画像 / 赛道 / 权威度 / 赞助 / 互动率 | ✅ | caption + 私有 API |
| 评论购买意图 / bot / 互赞团 | ✅ | 评论采集 + 水军库 |
| 视觉真实度（皮肤纹理） | ✅ 可自动 | 截图 + 视觉模型（目前样例，可批量） |
| **Save 率 / DM 分享** | ❌ | 公开 API 任何页面都不渲染，**只能 Modash/创作者后台** |
| 粉丝画像（年龄/性别/国家/假粉%） | ❌ | 公开 API 拿不到，**需 Modash** |

---

## 四、现状 + 可优化点（你来决定改哪个）

### 已完整
基础过滤、Amazon 穿透+留证、内容画像、权威度、赞助饱和、互动率、评论意图、水军库、赛道对口、评分透明、验收 UX。

### 弱点 / 缺口（候选优化点）

| 优化点 | 现状 | 选项 | 影响 |
|--------|------|------|------|
| **A. 种子源（最大瓶颈）** | 只有品牌 tagged，出来的多是时尚/生活博主，纯护肤+Amazon 橱窗稀疏 → **有用红人太少** | ①扫剩余 ~39 个未扫候选 ②Modash bio 搜"amazon"+护肤垂类（正解）③别的品牌种子 | 🔥 直接决定产出量与质量 |
| **B. 视觉验证系统化** | 只对几个做了视觉判断 | browser-use 批量截图 + 视觉判断所有 review 候选 | 中（客户明确要的护肤视觉项） |
| **C. Save 率 / DM** | 缺口 | 仅 Modash 能补 | 中（客户列为关键信任信号） |
| **D. Storefront 成熟度** | 仅直链数了商品图 | 加分类数/更新频率/活跃度判定 | 低-中 |
| **E. 关键词/阈值/权重调参** | 经验值 | 产品推荐词表、权威度权重、赛道词表、评分权重、archetype/对口阈值都可调 | 中（影响排序与分类准确度） |
| **F. 评论深度 vs 账号消耗** | 深抓烧号（评论接口触发挑战） | 平衡深度/账号轮换/代理 | 中（影响 trust 准确度） |
| **G. 候选规模** | 单次扫 50 | 扩 per_source / 扫更多 | 中（更多候选但烧号） |

### 一句话判断
**A（种子源）是价值瓶颈，其余都是精度/完备性优化。** 想要"有用红人多"，根子在种子源——品牌 tagged 出不来纯护肤号，Modash 是规模化正解；当前账号没 hashtag 权限。其余 B-G 是把现有候选judge 得更准更全。
