# 两层库 + 自我强化发现 —— 设计方案（保留，待账号池扩大后启用）

状态：**已设计、字段已就位、晋升/优质种子扩散流程暂不启用**（先不跑大规模扩展）。
账号池足够大后再开启 `promote` 晋升 + 优质种子飞轮。

---

## 三个数据库

| 库 | 存储 | 定位 | 价值 |
|----|------|------|------|
| **水军库** | `data/pod_accounts.json` | 互赞团/水军用户名，跨 run 累积，扫到直接略过 | 过滤、提质 |
| **基本库 (Tier 1)** | `discovery.db · creator_index` (quality_tier=1) | 所有爬过的创作者，去重累积 | 原始资产 + 图节点池 |
| **高质量库 (Tier 2)** | `discovery.db · creator_index` (quality_tier=2) | 严格门槛晋升的电商对口精英 | **最高价值——够大够全可直接卖数据/建数据站** |

> 设计原则：**基本库、尤其高质量库，字段必须存全**——凡 Excel 交付要输出的数据全部入库
> （关键字段建独立列便于查询/排序，完整记录存 `data_json`，含分解/评论样例等嵌套结构，无损）。

---

## creator_index 全字段 schema（已落地）

关键可查询列（节选）：身份(handle/pk/full_name/bio/followers/tier_label/is_verified/category) ·
Amazon(has_amazon/bio_link_type/bio_link_url) · 赛道(niche/niche_secondary/fit/archetype) ·
内容(product_rec/authority/authority_evidence) · 赞助互动(sponsored/organic/reels_er/static_er/meets_benchmark) ·
评论信任(comments_analyzed/intent_ratio/bot_ratio/pod_ratio/trust_level) · 综合(status/score/centrality) ·
血缘(hop/discovered_via/first_seen/last_crawled) · 两层库(quality_tier/verified/promoted_at/outreach) ·
**data_json**（完整候选记录，含 top_intent_comments / pod_samples / *_breakdown / reasons 等）。

---

## 晋升门槛（定义"高质量/电商对口"）

只有**全部满足**才从基本库(1)晋升高质量库(2)：

```
✓ Amazon Storefront = 已核实（confirmed / 浏览器穿透通过，不含纯 unverified）
✓ 对口度 = core（护肤 / 美妆 / 美容仪器）
✓ 粉丝 10K–150K
✓ 互动率达标（meets_benchmark）
✓ 评论信任 ≥ medium（真实购买意图，非水军）
✓ 综合评分 ≥ 阈值（默认 40）
```

可分两级：**银**=对口+橱窗+互动达标；**金**=再加评论信任。

---

## 自我强化飞轮（核心，待启用）

```
        ┌────────── 高质量库(Tier2) 黄金种子 ──────────┐
        ↓                                              │
  取其 suggested相似号 / 同帖共现（封号磁铁 media_likers 不用）
        ↓                                              │ 飞轮越转
  爬取+评估 → 进基本库(Tier1) → 建相似度图边           │ 种子越优质
        ↓ 达晋升门槛？                                  │ 发现越精准
       是 → 晋升高质量库(Tier2) ──────────────────────┘
```

原理：优质红人的相似号/合作圈天然是同类电商红人 → 从他们扩散命中率远高于品牌 tagged/关键词。
每轮把好苗子晋升 → 下轮种子更好 → 正反馈。

---

## 搜索流程的简化演进

| 阶段 | 发现方式 |
|------|---------|
| 现在（冷启动） | 品牌/关键词种子临场爬 + 多跳扩散（discover_graph ingest）|
| 飞轮起步后 | **先查高质量库**（瞬时、零账号、已精选）→ 够就直接交付 |
| 库存不足 | 才触发一轮"优质种子扩散"补货 |

→ 日常 = 查库 + 偶尔补货，不再每次全量爬。

---

## 待启用时要做的（TODO）

1. `discover_graph.py` 加 `promote` 命令：按门槛批量把 Tier1 达标者 `UPDATE quality_tier=2, promoted_at=now`
2. `seed_frontier` 改：优先 `SELECT handle FROM creator_index WHERE quality_tier=2` 作种子；空则回退品牌/关键词
3. `ingest` 评估后顺带判晋升（或独立 `promote` 步骤）
4. `search` 默认查 quality_tier=2；`--all` 查全量
5. 加 `golden` 命令：列高质量库 + 运营状态（outreach）
6. 晋升门槛里的"评论信任"需评论数据 → 决定 ingest 是否带评论，或晋升分银/金两级
7. 数据资产化：高质量库够大后，导出/API/建站对外

---

## 现状小结（本次）

- ✅ 三库定位确定；creator_index 扩成全字段（52 列 + data_json）+ 预留 quality_tier/verified/outreach
- ✅ 修复 ingest 的 `handle` KeyError（fetch_user_info 返回 username）
- ⏸ 晋升流程 + 优质种子飞轮 **暂不启用**，待账号池扩大
- ⏸ 大规模 ingest 暂不跑（账号当日已用较多）
