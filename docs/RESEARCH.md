# 外部工具与项目调研

更新时间：2026-05-28

## 结论

没有找到一个现成的开源项目能直接完成"Amazon Finds 导购型红人筛选"的完整流程。但找到了多个可以直接复用或参考的模块级工具。

---

## 一、instagrapi 已有能力（已安装，直接用）

实际检查了 instagrapi 2.7.19 的 type 定义，确认可用字段：

### User 模型
```
bio_links: List[BioLink]      # ✅ 直接拿到 bio 链接列表，每个含 url
external_url: Optional[str]    # ✅ 主链接
biography: Optional[str]       # ✅ bio 文本
is_business: bool              # ✅ 是否商业账号
category_name: Optional[str]   # ✅ 账号分类
business_category_name: Optional[str]
follower_count, following_count, media_count
is_verified, full_name, username
```

### Media 模型
```
media_type: int                # 1=photo, 2=video, 8=album
caption_text: str              # ✅ caption 全文
like_count: int
comment_count: Optional[int]
play_count: Optional[int]      # ✅ Reels/Video 播放量（但经常缺失）
view_count: Optional[int]
video_duration: Optional[float]
sponsor_tags: List[UserShort]  # ✅ 赞助标签里的品牌账号
taken_at: datetime
code: str                      # shortcode
```

### Comment 模型
```
text: str                      # ✅ 评论文本
user: UserShort                # 评论者
like_count: Optional[int]      # 评论点赞数
created_at_utc: datetime
```

### 关键发现
- `is_paid_partnership` 只在 Story 模型上，**Media 没有这个字段**
- 因此赞助检测必须通过 caption 文本匹配 + sponsor_tags 判断
- `bio_links` 直接返回解析好的链接列表，不需要自己爬 profile 页面
- `play_count` 经常为 0 或缺失，不能作为唯一热度指标

---

## 二、可复用的开源项目

### 1. CommentAnalyzer — 评论分析（最相关）

- 仓库：https://github.com/abbasi0abolfazl/CommentAnalyzer
- 功能：Instagram 评论的情感分析 + **问题识别** + 主题建模
- 技术：LDA / LSA / NMF / BERT
- 可复用点：
  - **Question Detection** 模块：识别评论中的问题（how / what / where / which）
  - 这和我们的"购买意图检测"（"where is the link" / "does this work for"）直接相关
  - 主题建模可用于自动发现评论中的高频话题
- 判断：**参考其问题识别逻辑**，但我们的场景更窄（只关心购买意图），第一版用关键词匹配足够，后续可以升级到 BERT

### 2. instagram-bot-detector — Bot 账号检测

- 仓库：https://github.com/andgom97/instagram-bot-detector
- 功能：基于 XGBoost 的 Instagram bot 检测，判断关注者中有多少是 bot
- 技术：XGBoost + Instaloader 采集
- 可复用点：
  - 它的特征工程思路：用 follower_count / following_count / media_count 比例等公开特征判断 bot
  - 训练好的模型可以参考
- 判断：我们不需要判断候选 creator 是不是 bot，而是**判断评论者是不是 bot**。它的特征思路可以参考，但直接用的场景不同

### 3. instagram_bot_classification — Bot 评论分类

- 仓库：https://github.com/marclelamy/instagram_bot_classification
- 功能：从帖子评论中区分真实用户和 bot
- 数据：从大号帖子下采集 90K 评论，用半自动方式标注
- 可复用点：
  - **评论级 bot 检测**正是我们需要的
  - 它用的特征：评论点赞数、评论者粉丝数、评论者发帖数等公开特征
  - 可以参考它的标注规则来定义我们的 bot 评论模式
- 判断：**参考其标注规则和特征**，第一版先用正则/关键词匹配（"Beautiful!" / 纯 emoji / 纯 tag），后续可接 ML

### 4. instagram-influencer-graph — 网络化红人评分

- 仓库：https://github.com/sdmirch/instagram-influencer-graph
- 功能：用社交网络图谱 + 中心性分析识别有影响力的中腰部红人
- 技术：Python / NetworkX / MongoDB / Selenium / vaderSentiment
- 评分方法：
  - Critical Mass Filter：粉丝 > 5K
  - Interaction Score = likes / followers
  - Influencer Score = eigenvector centrality（连接到其他关键成员则分高）
- 判断：它的 Interaction Score 思路可以参考，但网络图谱方法对我们太重了（需要抓 follower 关系），**不直接用**

### 5. Apify Instagram Bio Links Scraper（付费 SaaS）

- 地址：https://apify.com/instaprism/instagram-bio-links-scraper
- 功能：批量提取 Instagram profile 的 bio 链接，识别 Linktree / Beacons / Amazon 链接
- 可复用点：
  - 它能识别 bio 链接类型（哪种 link-in-bio 工具、是否有 Amazon/Shopify/Etsy）
  - 如果我们需要穿透 Linktree 检查是否有 Amazon Storefront，可以用它
- 判断：**我们不需要它**。instagrapi 已经返回 `bio_links` 列表，直接正则匹配 amazon.com/shop 就行。Linktree 穿透可以用 requests 自己做

### 6. Apify Amazon Influencers Scraper（付费 SaaS）

- 地址：https://apify.com/igview-owner/amazon-influencers-profile-scraper
- 功能：从 Amazon 侧反向采集 influencer profile（名字、描述、top creator 状态、post 数等）
- 可复用点：
  - 如果我们在 IG 侧发现一个候选有 Amazon 链接，可以反向验证其 Amazon Storefront 活跃度
  - 可以直接用 Amazon storefront URL 查询
- 判断：**可选增强**，第一版不用。我们先从 IG bio_links 判断有无 Amazon 链接

### 7. ScrapeCreators API（付费 SaaS）

- 地址：https://scrapecreators.com/
- 功能：社交媒体数据 API，含 bio links 提取、creator analytics
- 判断：**不用**，instagrapi 已覆盖

### 8. Apify Influencer Discovery Agent

- 仓库：https://github.com/apify-store/influencer-discovery
- 功能：描述理想红人 → AI 匹配候选 → 评估报告
- 现状：当前只支持 TikTok
- 判断：**不可用**，不支持 Instagram

---

## 三、instagrapi 的 sponsor_tags 用法

Media 模型有 `sponsor_tags: List[UserShort]`，如果帖子是品牌合作帖且标注了赞助商，这里会返回品牌账号列表。

用法：
```python
for media in user_medias:
    if media.sponsor_tags:
        # 这是一个 paid partnership 帖子
        sponsor_names = [s.username for s in media.sponsor_tags]
```

但实际测试中 sponsor_tags 不一定总是填充的（creator 可能不使用 IG 原生 paid partnership 标签）。因此赞助检测仍需要 caption 文本匹配作为主要方法。

---

## 四、Linktree 穿透检测（可选增强）

如果候选者 bio 是 Linktree / Beacons 链接而不是直接 Amazon 链接，我们可以：

```python
import requests

def check_linktree_for_amazon(linktree_url: str) -> bool:
    """访问 Linktree 页面，检查是否包含 Amazon 链接"""
    try:
        resp = requests.get(linktree_url, timeout=10)
        return "amazon.com/shop" in resp.text.lower()
    except:
        return False  # 无法确认，标记为 review
```

这比调 Apify 简单得多，第一版可以做。

---

## 五、对执行计划的影响

| 模块 | 原方案 | 调研后方案 |
|------|--------|-----------|
| Bio Amazon 检测 | 正则匹配 bio_links | **不变**，instagrapi `bio_links` + `external_url` 直接用。可选增加 Linktree 穿透 |
| 内容画像（Amazon Finds 型） | 自定义关键词匹配 | **不变**，没有现成分类器。关键词匹配是行业通行做法 |
| 赞助饱和度 | caption #ad 检测 | **增加 sponsor_tags 检测**，caption 匹配 + sponsor_tags 双保险 |
| 互动率 | 自算 | **不变**，没有现成工具比自己算更好 |
| 评论购买意图 | 自定义关键词 | **参考 CommentAnalyzer 的 Question Detection 思路**，第一版关键词匹配，预留 BERT 接口 |
| Bot 评论过滤 | 正则 | **参考 instagram_bot_classification 的标注规则**，增加评论者 follower/media_count 特征 |

不需要引入新依赖或付费 API。所有能力 instagrapi + 标准库就能实现。
