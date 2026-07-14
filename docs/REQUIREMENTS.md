# Amazon Finds 导购型红人发现 — 客户需求拆解

更新时间：2026-06-01（按客户完整 brief 重新提炼）

## 项目定位

为品牌 Prime Day 转化营销活动，在 Instagram 上发现符合"Amazon Finds"导购型画像的创作者。

不做 TikTok（brief 含 TikTok 基准，本项目只做 Instagram）。不做全 IG 穷尽搜索。采用 seed-based discovery + 多层漏斗筛选 + 人工核验清单。

## 创作者画像（客户总体标准）

| # | 标准 | 含义 |
|---|------|------|
| 1 | **"Amazon Finds" 导购型** | 整个内容策略围绕产品推荐，而非泛生活方式 / vlog |
| 2 | **高意图 Micro/Mid** | 10K–150K 粉丝，社区信任度与互动最高 |
| 3 | **成熟的橱窗基础设施** | Bio 直链一个**成熟、活跃、有组织**的 Amazon Storefront |
| 4 | **护肤 & 设备专业度** | 能自信谈论 skin barrier、成分、设备规格（LED 波长、irradiance），有权威感而非追热点 |
| 5 | **高信任社区** | 帖子 **save 率高**，评论区充满产品问答、链接请求、购买意图 |

---

## 三类判定：硬门槛 / 软信号 / 人工核验

把每条需求明确归类——这决定了候选最终是 include / review / exclude。

### A. 硬性门槛（不满足 → exclude，脚本自动判定）

| 门槛 | 规则 |
|------|------|
| 粉丝区间 | 10K ≤ followers ≤ 150K（Micro 10K–100K / Mid 100K–150K）|
| 账号活跃 | 近 30 天有发帖 |
| 非品牌/官方号 | 排除品牌、店铺、官方号 |
| **有 Amazon Storefront 链接** | Bio 直链 `amazon.com/shop/*`，或经聚合页（Linktree/Beacons/LTK）间接链接。完全无 Amazon 链接 → exclude |

### B. 软信号（用于评分与排序，脚本自动计算）

| 信号 | 计算 |
|------|------|
| 内容画像 | 近 15 条 caption 产品推荐占比 → amazon_finds / mixed / lifestyle |
| 护肤权威度 | 成分 / 设备规格 / 皮肤科学 / 场景词命中加权 0–100 |
| 赞助饱和度 | 近 15 条 #ad/#sponsored/Paid Partnership 占比（>40% 广告疲劳）|
| 互动率 | Reels / Static 分档计算，对比基准 |
| 评论购买意图 | 高意图关键词占比（排除 bot/互赞团后）|

### C. 必须人工核验（脚本无法自动判定 → 标 review + 列入核验清单）

这是本次明确补强的部分。以下要么公开 API 拿不到，要么本质需要人眼判断：

| 核验项 | 为什么人工 | 去哪核验 |
|--------|-----------|---------|
| **Amazon Storefront 穿透** | bio 是 Linktree/Beacons/LTK 聚合页时，脚本未必能穿透确认是否真的含 Amazon 橱窗 | 点开 bio 链接，找 Amazon Storefront 入口 |
| **Storefront 成熟度** | 客户要"成熟、活跃、有组织"，不只是"存在" | 打开 storefront，看商品数 / 分类 / 更新频率 |
| **视觉验证（护肤专项）** | 客户要真实未修图的近距离皮肤纹理特写，排斥重滤镜 / 柔光环形灯。这是纯视觉判断 | 打开主页，看 Reels / 帖子的皮肤特写真实度 |
| **Save 率 / DM 分享** | 公开 API **不返回** saves 和 shares，但客户视其为关键信任信号 | 需 Modash 数据，或创作者后台截图 |

> 设计原则：**Amazon Storefront 是硬门槛，内容/互动是软信号排序，视觉与 save 数据是人工/Modash 补充。** 凡涉及 C 类未决项的候选不直接淘汰，而是标 **review** 并在交付物里附**人工核验清单**。

---

## 量化基准（Instagram，客户原文）

### Micro-Tier（10K–100K 粉丝）
| 内容类型 | 互动率基准 |
|----------|-----------|
| Reels | 3.0%–5.0% |
| Static / Carousel | 1.8%–3.0% |
| Share / Save | 高 save 与 DM 分享 = 受众在收藏推荐（**人工/Modash**）|

### Mid-to-Macro（100K–150K，本项目上限 150K）
| 内容类型 | 互动率基准 |
|----------|-----------|
| Reels | 1.5%–3.0% |

互动率 = (likes + comments) / followers × 100。saves/shares 公开 API 不返回，保留字段位，作为人工/Modash 补充项。

---

## 信任度指标（评论分析）

### 高意图购买信号（正向）
- 链接请求："Where is the link?" / "Link please"
- Storefront 询问："Is it in your storefront?" / "Which folder is this under?"
- 产品问答："Does this work for sensitive skin?" / "What wavelength is this?"（高教育度、高信任护肤社区）
- 购买行为："Ordered!" / "Just bought this"

### 低价值 / bot 评论（过滤）
- 通用无意义："Beautiful!" / "Love this" / "Nice pic" / "Gorgeous"
- 纯 emoji、互相 tag 无内容
- **互赞团/水军**："love your content" / "amazing presentation, need to make the same" 等夸内容套话

计算：purchase_intent_ratio（高意图 / 有效评论）、bot_ratio、engagement_pod_ratio。
阈值：intent ≥ 15% → high trust；bot/pod ≥ 50% → 降级 review。

---

## 内容架构与行为标准

### Bio 链接基础设施
link-in-bio 工具（Linktree / Beacons / LTK）必须**直接挂接 Amazon Storefront**。没有可用 Amazon 橱窗的，不进入 Prime Day 转化名单。

- **LTK = LiketoKnow.it**：创作者购物联盟橱窗（RewardStyle 旗下），需确认是否含 Amazon 商品
- **Linktree / Beacons**：通用 link-in-bio 聚合页，需点开看是否有 Amazon Storefront 入口
- **amazon.com/shop 直链**：直接满足，最优

### 视觉验证（护肤专项，人工）
红光治疗 / 护肤类信任建立在**真实、未修图的皮肤纹理**上。优先：清晰、近距离的皮肤特写宏观镜头；排斥：重滤镜、低曝光、柔光环形灯视频。→ 人工核验项。

### 赞助饱和度过滤
- 上限：近 15 条含 #ad / #sponsored / Paid Partnership 若 >40% → 广告疲劳，转化率下降 → 降级/排除
- Organic 一致性：必须经常发 organic 的 "Amazon Finds" / "Skincare Routine" 维持社区信任

---

## 输出形式（客户需要拿到什么）

**一份分层的创作者目录**，每个候选给出判定 + 证据 + 待办：

### 分层
- **Include**：所有硬门槛通过，无未决项 → 可直接进入触达
- **Review**：硬门槛通过，但有 C 类人工核验项（Amazon 穿透 / 视觉 / save 数据 / 内容画像存疑）→ 附核验清单
- **Exclude**：任一硬门槛不满足 → 附排除原因

### 每个候选字段
```
handle, profile_url, full_name, followers, tier
has_amazon_storefront, bio_link_type, bio_link_url
creator_archetype, product_rec_ratio, skincare_authority_score
sponsored_ratio, organic_amazon_posts_count
reels_engagement_rate, static_engagement_rate, meets_er_benchmark
purchase_intent_ratio, bot_comment_ratio, engagement_pod_ratio, trust_level
top_intent_comments（高意图评论原文）
discovery_sources, discovery_score
status（include/review/exclude）
filter_reasons / review_reasons（→ 渲染成人工核验清单）
[人工/Modash 补充] save_rate, dm_shares, visual_validation, storefront_maturity
```

### 交付物形式
- **HTML 仪表盘**（`report-*.html`）：统计 + 可筛选表 + 候选详情抽屉 + **人工核验清单**。客户双击即看。
- **CSV / JSON**：全字段，供二次处理。
- **XLSX**（后续）：高亮交付版。

> 核心交付理念：工具不替客户拍板，而是**把能自动判定的都判定掉，把必须人工看的明确列出来并直达链接**——让人工只花在真正需要人眼的地方（视觉、Storefront 穿透、save 数据）。
