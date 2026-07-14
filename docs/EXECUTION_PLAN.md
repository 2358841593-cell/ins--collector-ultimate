# 执行计划

更新时间：2026-05-28

## 前提

- 环境已就绪：Python 3.13 + instagrapi + openpyxl
- 5 个采集账号已导入
- 客户需求已拆解到 `docs/REQUIREMENTS.md`
- Modash Web 会员（客户提供）：无 API，通过 CSV 导出集成

## 总体架构

一个主脚本 `scripts/discover.py`，内部分六个阶段串行执行：

```
阶段 1  Seed 采集 → 候选池（instagrapi + Modash Search CSV）
阶段 2  Profile + Posts 回扫（instagrapi）
阶段 3  多层漏斗筛选（含 Modash 数据增强）
阶段 4  评论意图分析（仅对通过前三层的候选）
阶段 5  综合评分 + 状态判定
阶段 6  输出 JSON + CSV
```

---

## 阶段 1：Seed 采集 → 候选池

### 目标

从品牌账号、hashtag、关键词三类 seed 中提取候选 creator handle 池。

### 输入

通过 CLI 参数传入：

```
--brand-handles: currentbody, solawave, therabody
--hashtags: amazonfinds, amazonskincare, amazonmusthaves, ledmask,
            redlighttherapy, skincareroutine, beautydevice, antiaging
--keywords: "amazon finds", "amazon storefront", "led mask",
            "red light therapy", "beauty device", "skin barrier",
            currentbody, skincare
```

### 采集方式

| Seed 类型 | 采集入口 | 提取 creator |
|-----------|---------|-------------|
| 品牌账号 | 品牌主页近期 posts | post 的 tagged_users + caption mentions |
| Hashtag | hashtag top/recent feed | 每条 post 的 author |
| 关键词 | 与 hashtag 采集后的 caption 匹配 | 同上 |
| **Modash CSV** | 用户在 Modash 搜索后导出 | CSV 中的 username |

### 采集参数

- 每个 seed source 取 `--per-source` 条帖子（默认 15）
- 账号间 sleep `--sleep` 秒（默认 5）
- 候选池去重：按 handle 去重

### 输出

候选 handle 集合 + 每个 handle 的 discovery_sources 记录。

### 关键实现细节

- hashtag 来源优先取帖子 author，不把 tagged 品牌号当 creator
- 品牌来源从 tagged_users 和 mentions 中提取，品牌自己 exclude
- 同一个 handle 可能从多个 source 被发现，合并 sources

---

## 阶段 2：Profile + Posts 回扫

### 目标

对候选池中的每个 handle，采集 profile 元数据和最近 15-20 条 posts。

### 采集字段

#### Profile 级

```
username, full_name, biography, follower_count, following_count,
media_count, is_verified, is_business, category,
external_url, bio_links
```

#### Post 级（最近 15-20 条）

```
code (shortcode), taken_at, media_type, caption_text,
like_count, comment_count, view_count (Reels),
video_duration, thumbnail_url, video_url
```

### 采集控制

- 每个候选账号采 `--candidate-posts` 条（默认 20）
- 串行低频，每账号间 sleep
- 失败不阻断，记录 error 继续下一个
- 使用 `user_info_by_username_v1` 避免 public web 429

---

## 阶段 3：多层漏斗筛选

六个子层，按顺序执行。任意一层 exclude 则不再进入后续层。

### 3.1 基础过滤

| 检查项 | 规则 | 不通过处理 |
|--------|------|-----------|
| 粉丝数 | 10,000 ≤ followers ≤ 150,000 | exclude: followers_out_of_range |
| 品牌/官方号 | handle/name/bio/category 含 brand/official/shop 等 | exclude: brand_like |
| 活跃度 | 最近 30 天内有发帖 | exclude: inactive |

品牌号检测规则（沿用 ins-hot-analysis 已有逻辑）：
- handle 或 full_name 含 brand seed 词的变体
- category 含 "Product/Service", "Shopping", "Brand"
- bio 含 "official", "shop now", "our products"

### 3.2 Bio 基础设施检测

检查 profile 的 `external_url` 和 `bio_links`：

```python
AMAZON_PATTERNS = [
    r"amazon\.com/shop/",
    r"amazon\.[a-z.]+/shop/",
]
LINKINBIO_PATTERNS = [
    r"linktr\.ee/",
    r"beacons\.ai/",
    r"liketoknow\.it/",
    r"ltk\.app/",
    r"linkin\.bio/",
    r"bio\.site/",
]
```

输出字段：

```
has_amazon_storefront: bool  # 直接命中 amazon.com/shop 或通过 link-in-bio
bio_link_type: "amazon_direct" | "linktree" | "ltk" | "beacons" | "other_linkinbio" | "none"
bio_link_url: str
```

判定：
- 直链 amazon.com/shop → has_amazon_storefront = True
- 有 link-in-bio 工具但无法确认是否含 Amazon → has_amazon_storefront = "unverified", review_reasons += "linkinbio_needs_manual_check"
- 无任何链接 → exclude: no_bio_link

注意：instagrapi 的 `User.bio_links` 已直接返回解析好的链接列表（每个含 url 字段），不需要自己爬 profile 页面。

Linktree 穿透：如果 bio 链接是 Linktree/Beacons，可以用 requests.get() 访问页面 HTML，检查是否含 amazon.com/shop。不需要付费 API（调研中 Apify 有 Bio Links Scraper 但不必要）。穿透失败则标记为 review。

### 3.3 内容画像判断

扫描最近 15 条 caption，计算：

#### "Amazon Finds" 导购型判断

```python
PRODUCT_REC_KEYWORDS = [
    "amazon finds", "amazon storefront", "amazon must haves",
    "amazon favorites", "amazon haul", "amazon beauty",
    "link in bio", "shop my", "in my storefront",
    "linked everything", "linked it", "linking",
    "holy grail", "repurchase", "restock",
    "currently using", "my favorites", "must haves",
    "top picks", "roundup", "recommendation",
]
```

计算：`product_rec_ratio = 含产品推荐词的帖数 / 总帖数`

判定：
- ratio >= 0.40 → creator_archetype = "amazon_finds"
- 0.20 <= ratio < 0.40 → creator_archetype = "mixed"
- ratio < 0.20 → creator_archetype = "lifestyle" → exclude: not_product_focused

#### Skincare & Tech 权威性

```python
AUTHORITY_KEYWORDS = {
    "ingredients": ["niacinamide", "retinol", "hyaluronic", "vitamin c",
                    "peptide", "ceramide", "salicylic", "aha", "bha",
                    "glycolic", "lactic acid", "squalane"],
    "device_specs": ["wavelength", "nm", "irradiance", "led",
                     "red light", "near infrared", "joules", "mw/cm"],
    "skin_science": ["skin barrier", "moisture barrier", "collagen",
                     "elastin", "cell turnover", "photobiomodulation",
                     "dermatologist", "clinical"],
    "conditions": ["sensitive skin", "acne", "hyperpigmentation",
                   "fine lines", "texture", "rosacea", "eczema",
                   "dark spots", "wrinkles", "pores"],
}
```

计算：每个类目命中数加权，总分 0-100。

输出：`skincare_authority_score`，不作为 exclude 条件，作为排序加分项。

### 3.4 赞助饱和度检测

双信号源检测：

信号源 1 — caption 文本匹配：
```python
SPONSORED_MARKERS = [
    r"#ad\b", r"#sponsored\b", r"#partner\b",
    r"paid partnership", r"gifted", r"collab\b",
    r"brand ambassador", r"discount code",
]
```

信号源 2 — instagrapi `Media.sponsor_tags`：
```python
# 如果 media.sponsor_tags 非空，说明帖子使用了 IG 原生 Paid Partnership 标签
if media.sponsor_tags:
    is_sponsored = True
```

注意：`is_paid_partnership` 字段只在 Story 模型上，Media 没有。因此 feed 帖子的赞助检测必须用 caption + sponsor_tags 双保险。（来源：instagrapi 2.7.19 types.py 实测确认）

计算：`sponsored_ratio = 命中帖数 / min(total_posts, 15)`

同时计算：`organic_amazon_posts_count` = 不含赞助标记但含产品推荐词的帖数

判定：
- sponsored_ratio > 0.40 → exclude: ad_saturated
- sponsored_ratio > 0.30 → review_reasons += "high_sponsorship"

### 3.5 互动率分档计算

将帖子按 media_type 分组（Reels vs Static/Carousel），分别计算互动率：

```python
reels_er = sum(likes + comments for reels) / len(reels) / followers * 100
static_er = sum(likes + comments for static) / len(static) / followers * 100
```

按粉丝档设阈值：

| Tier | 粉丝范围 | Reels 最低 | Static 最低 |
|------|---------|-----------|------------|
| micro | 10K–100K | 3.0% | 1.8% |
| mid | 100K–150K | 1.5% | 1.5% |

判定：
- Reels ER < 阈值 → review_reasons += "low_reels_er"
- 没有 Reels 帖子 → 用 static ER 判断
- 两项都低于阈值 → exclude: low_engagement

### 3.6 综合判定

到这一步的候选者已通过基础过滤、Bio 检测、内容画像、赞助饱和度、互动率五层。

状态判定：

```
include: 所有硬性条件通过，无 review_reasons
review:  硬性条件通过，但有待人工确认项
         (如 linkinbio_needs_manual_check, high_sponsorship, low_reels_er)
exclude: 任一硬性条件不通过
```

---

## 阶段 4：评论意图分析

### 目标

对 include 和 review 状态的候选者，采集其 Top 帖子评论，分析购买意图和社区信任度。

### 为什么放在漏斗后面

评论采集是高成本操作（API 调用多、限流风险高）。只对通过前三层的候选做，避免浪费。

### 采集方式

- 对每个候选者取互动最高的 3-5 条帖子
- 每条帖子采 20-30 条评论
- 串行低频采集

### 分析规则

方法论参考：
- 购买意图检测参考 [CommentAnalyzer](https://github.com/abbasi0abolfazl/CommentAnalyzer) 的 Question Detection 模块（基于疑问词 how/what/where/which 识别问题类评论），在此基础上收窄到购买场景
- Bot 评论过滤参考 [instagram_bot_classification](https://github.com/marclelamy/instagram_bot_classification) 的标注规则（90K 评论人工 + 半自动标注，用评论长度、评论者 follower/media_count 等公开特征）
- 第一版用关键词 + 正则，预留后续接 BERT 的接口

#### 购买意图关键词

分三类，权重不同：

```python
# 强信号：直接购买行为或链接请求
STRONG_INTENT = [
    "ordered", "just bought", "bought this", "in my cart",
    "adding to cart", "buying this",
    "where is the link", "link please", "link in bio",
    "storefront", "which folder", "is it in your",
]

# 中信号：产品问答（参考 CommentAnalyzer 的 question detection 思路）
MEDIUM_INTENT = [
    "does this work for", "what wavelength", "sensitive skin",
    "how long", "what size", "which shade", "which one",
    "do you recommend", "worth it", "would you suggest",
    "what's the difference", "how do you use",
    "does it help with", "is it good for",
]

# 弱信号：一般兴趣
WEAK_INTENT = [
    "need this", "want this", "love this one",
    "code", "discount", "promo", "sale",
    "saving this", "bookmarked",
]
```

#### Bot/低质评论

参考 instagram_bot_classification 的标注规则，分两层：

```python
# 第一层：文本模式匹配
BOT_TEXT_PATTERNS = [
    r"^(beautiful|love this|nice pic|amazing|gorgeous|so pretty|stunning|wow|fire|goals)[\!\.\s]*$",
    r"^[\U0001F300-\U0001FAFF\U00002702-\U000027B0\s]+$",  # 纯 emoji
    r"^@\w+\s*$",  # 纯 tag
    r"^(follow me|check my|dm for|collab\?).*$",  # spam
]

# 第二层：评论者特征（如果 instagrapi 返回 UserShort 含足够字段）
# 参考 instagram_bot_classification 的做法：
# - follower_count 极低（< 50）且 following_count 极高（> 2000）→ 疑似 bot
# - media_count = 0 → 疑似空号
# 注意：Comment.user 是 UserShort，字段有限，不一定有 follower_count
# 第一版只用文本模式，后续可选增加用户特征
```

### 输出字段

```
purchase_intent_ratio:    高意图评论 / 有效评论（排除 bot 后）
bot_comment_ratio:        bot 评论 / 总评论
trust_level:              high (intent >= 15%) / medium (5-15%) / low (< 5%)
top_intent_comments:      最多 5 条高意图评论原文样例
top_intent_keywords_found: 命中的意图关键词列表
```

### 对状态的影响

- trust_level = high → 加分，保持 include
- trust_level = low + bot_ratio >= 50% → 降级为 review 或 exclude
- 评论采集失败 → 不改变状态，标注 "comments_unavailable"

---

## 阶段 5：综合评分

对 include 和 review 候选者计算总分，用于排序。

```
discovery_score =
    engagement_rate_score    * 0.25
  + product_rec_ratio_score  * 0.20
  + purchase_intent_score    * 0.20
  + authority_score          * 0.15
  + sponsorship_health_score * 0.10
  + evidence_strength_score  * 0.10
```

每项 0-100，加权后得总分。

此分数不改变 status，只用于输出排序。

---

## 阶段 6：输出

### JSON 输出

完整字段，含所有中间计算结果，保存到 `data/runs/discovery-{timestamp}.json`。

### CSV 输出

全字段平铺，保存到 `data/runs/discovery-{timestamp}.csv`。

### 字段清单

```
# 基础
handle
profile_url
full_name
followers
following
media_count
is_verified
tier                          # micro / mid

# Bio 基础设施
has_amazon_storefront          # true / false / unverified
bio_link_type                  # amazon_direct / linktree / ltk / beacons / other / none
bio_link_url

# 内容画像
creator_archetype              # amazon_finds / mixed / lifestyle
product_rec_ratio              # 产品推荐帖占比
skincare_authority_score       # 0-100
authority_evidence             # 命中的权威词

# 赞助饱和度
sponsored_count                # 近 15 条中的赞助帖数
sponsored_ratio
organic_amazon_posts_count     # 非赞助的产品推荐帖数

# 互动率
reels_count                    # Reels 帖数
reels_avg_likes
reels_avg_comments
reels_engagement_rate
static_count
static_avg_likes
static_avg_comments
static_engagement_rate
meets_er_benchmark             # bool

# 评论信任度
comments_analyzed              # 分析的评论总数
purchase_intent_ratio
bot_comment_ratio
trust_level                    # high / medium / low
top_intent_comments            # 高意图评论样例
intent_keywords_found

# 发现来源与证据
discovery_sources              # 从哪里发现的
evidence_level                 # strong / medium / weak
evidence_count
matched_keywords
collab_or_relevant_post_links

# Modash 数据增强（有 Modash 导出时填充）
modash_credibility             # 真实粉丝比例 0-1
modash_fake_pct                # 假粉比例
modash_audience_us_pct         # 美国粉丝占比
modash_audience_female_pct     # 女性粉丝占比
modash_audience_age_18_34_pct  # 18-34 岁粉丝占比
modash_avg_likes               # Modash 统计的平均点赞
modash_avg_reels_plays         # Modash 统计的平均 Reels 播放
modash_er                      # Modash 计算的互动率

# 综合
discovery_score                # 加权总分
status                         # include / review / exclude
filter_reasons
review_reasons
```

---

## Modash 集成方案

### 背景

客户提供 Modash Web 会员（非 API）。Modash 提供我们 instagrapi 拿不到的三类关键数据：

1. **Fake Follower %**（`audience.credibility`）— 直接过滤刷粉号
2. **粉丝画像**（年龄/性别/国家分布）— 判断粉丝是否匹配 US 市场
3. **Lookalike 红人推荐** — 从好红人扩展更多候选

### 集成架构

```
用户操作 Modash Web → CSV 导出 → 脚本读取合并
```

三个接入点：

#### 接入点 1：Modash Search CSV → 种子源（阶段 1）

用户在 Modash Discover 搜索：
- Followers: 10K–150K
- Bio 含 "amazon"
- Hashtags: amazonfinds, skincare, beautydevice
- Engagement Rate ≥ 1.5%
- Audience Credibility ≥ 75%

导出 CSV → 传入 `--modash-search path/to/export.csv`

脚本读取 CSV 中的 username，标记 `discovery_source = "modash_search"`，合并进候选池。

#### 接入点 2：Modash Profile Reports → 数据增强（阶段 3 后）

对通过漏斗前几层的候选者：
1. 脚本输出候选 username 列表
2. 用户在 Modash 逐个查看 Profile Report 并导出
3. 传入 `--modash-profiles path/to/profiles_export.csv`

脚本读取并合并以下字段：
```
modash_credibility:      float  # 真实粉丝比例 (0-1)
modash_fake_pct:         float  # 假粉比例 = 1 - credibility
modash_audience_us_pct:  float  # 美国粉丝占比
modash_audience_female_pct: float
modash_audience_age_18_34_pct: float
modash_avg_likes:        int
modash_avg_comments:     int
modash_avg_reels_plays:  int
modash_er:               float  # Modash 计算的互动率
```

#### 接入点 3：Modash Lookalike → 扩展候选池（可选）

找到好红人后 → 在 Modash 查看 lookalikes → 导出 → 二次运行脚本。

### 对评分的影响

当 Modash 数据可用时，评分公式调整为：

```
discovery_score =
    engagement_rate_score    * 0.20  (降: Modash ER 可交叉验证)
  + product_rec_ratio_score  * 0.18
  + purchase_intent_score    * 0.17
  + authority_score          * 0.12
  + sponsorship_health_score * 0.08
  + evidence_strength_score  * 0.05
  + modash_credibility_score * 0.10  (新: 粉丝真实度)
  + modash_audience_fit_score * 0.10 (新: 粉丝画像匹配度)
```

无 Modash 数据时退回原公式（6 项，权重不变）。

### Modash 搜索操作指南

用户在 Modash Web 执行以下搜索并导出：

```
Step 1: 登录 marketer.modash.io
Step 2: Discover → Instagram
Step 3: 设置筛选条件：
  - Followers: 10,000 – 150,000
  - Bio: "amazon" 或 "storefront"
  - Hashtags: amazonfinds, amazonskincare, ledmask, redlighttherapy
  - Engagement Rate: ≥ 1.5%
  - Audience Credibility: ≥ 75%
  - Last Posted: 90 days
  - Location: United States (如适用)
Step 4: Export → CSV
Step 5: 传入脚本 --modash-search exported_file.csv
```

---

## 实现顺序

| 步骤 | 目标 | 产物 | 依赖 |
|------|------|------|------|
| Step 0 | 环境 + 账号 | .venv, .secrets | ✅ 已完成 |
| Step 1 | Seed 配置 | config/seeds.toml | ✅ 已完成 |
| Step 2 | 主脚本骨架 | scripts/discover.py（阶段 1-2） | Step 1 |
| Step 3 | 漏斗筛选 | scripts/discover.py（阶段 3） | Step 2 |
| Step 4 | 评论分析 | scripts/discover.py（阶段 4） | Step 3 |
| Step 5 | 评分 + 输出 | scripts/discover.py（阶段 5-6） | Step 4 |
| Step 6 | Modash CSV 集成 | scripts/discover.py（Modash 导入+增强评分） | Step 5 |
| Step 7 | 小样本试跑 | data/runs/discovery-*.json | Step 6 |
| Step 8 | 校准 + 修正 | 基于试跑结果调参 | Step 7 |
| Step 9 | 客户版 XLSX | 增加 XLSX 导出 | Step 8 |

## 采集账号

账号只保存在各开发者本机 `.secrets/account_pool.json`，不在文档或仓库中列出。
生产运行通过 `scripts/account_pool.py` 轮换账号并执行 cooldown；可用数量以本机
账号池健康检查结果为准。

## 安全规则

- 单进程串行采集
- 每个目标间 sleep
- 出现 429 / login challenge 时停止
- 不抓粉丝列表、like 列表
- 评论只对通过漏斗的候选采集
- 凭证不出 .secrets/
