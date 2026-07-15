# 数据库自循环进化系统设计（金种子飞轮）

日期：2026-07-15
定位：把"越用越值钱"的数据资产 + 客户审核回流固化为可执行的库结构与 SOP 阶段路由。
关联：`TWO_TIER_DESIGN.md`（原两层库构想）、`creator_cache.py`（浅扫缓存）、SOP 第 8 章反馈回流。

## 0. 一句话

**机器自动沉淀"扫过的/采过的"，人工审核门控"优质的/合作的"。** 只有懂业务的客户在交付表上
批准的红人才进金种子库，金种子驱动下一轮 Modash Lookalike 扩池——越转越准、越省号。

## 1. 五个库：存什么、何时进、用途

| 库 | 存储内容 | 进库时机 | 进库方式 | 用途 |
|---|---|---|---|---|
| **① 浅扫缓存库** `creator_cache.db · tier=0` | 所有扫过的创作者浅扫字段（粉丝/外链/**商业号标志**/类目/赛道/storefront） | 采集阶段，instaloader 浅扫后 | **机器自动** | 去重、**品牌号秒跳**、省号（命中 30 天内直接用，零 IG 请求） |
| **② 候选决策库** `discovery.db · candidates` + `creator_cache · tier=1` | 完整采集 + 决策（五池/评分/证据/原因） | 决策阶段（run_v2 后） | **机器自动** | 审计、可复现、复用已采数据、质量看板 |
| **③ 金种子库（优质红人）** `creator_cache · tier=2` | **客户审核批准 / 已合作的优质红人** | **反馈回流阶段**，客户 Herman Approval=Yes | **人工门控** | Modash Lookalike 种子、直接复用交付、**飞轮核心资产** |
| **④ 负向库** `creator_cache · client_status=rejected` | 客户拒绝的创作者 + 原因 | **反馈回流阶段**，Herman Approval=No | **人工门控** | 不再重现、分歧归因、不自动泛化为规则 |
| **⑤ 水军库** `pod_accounts.json` | bot / 互赞团 username | 评论分析阶段 | 机器自动 | 过滤提质，跨 run 累积 |

> 库①②③④同处 `creator_cache.db`（一张 `creator_profiles` 表 + `tier`/`client_status` 列区分），
> 便于一次查询联动；库⑤独立沿用。数据不进公开仓库（`data/` 已 gitignore），只存公开 profile 字段，无凭证。

## 2. 进库操作在 SOP 中的阶段与路由

进库分**两类触发**，落在 SOP 不同阶段：

```
                    ┌─────────────── 机器自动进库 ───────────────┐
阶段1 发现          阶段2-6 采集/门槛/评分/路由        阶段7 交付
  │                        │                              │
  │  先查库①去重/跳品牌     │  浅扫→库①(tier0)             │  五池表给客户
  │  金种子③→Lookalike     │  完整决策→库②(tier1)         │
  ↓                        ↓                              ↓
[seed_frontier]      [creator_cache.upsert]         [export XLSX]
                                                          │
                    ┌─────────────── 人工门控进库 ───────────────┐
                                                          ↓
                                              阶段8 反馈回流（进化关键）
                                                          │
                                        客户在表上填 Herman Approval/Feedback
                                                          │
                              ┌───────────────────────────┼───────────────────┐
                         Approval=Yes/已合作          Approval=No           留空/待定
                              ↓                            ↓                    ↓
                     ③ 进金种子库(tier=2)          ④ 进负向库            维持 tier1，下批复议
                     client_status=approved        client_status=rejected
                              │
                              └──→ 回到阶段1：金种子 → Modash Lookalike 扩池（下一轮种子）
```

**关键点：进金种子库③ 只发生在阶段8、且必须由客户审核门控**——不是机器自动，是"懂业务的人"
在交付表上批准/确认合作后才进。这保证金种子库的纯度（全是人验证过的优质红人）。

## 3. 自循环进化（飞轮）

```
金种子库③(客户批准的优质红人)
        │  取其 Modash Lookalike / audience-lookalike 作下一轮种子
        ↓
阶段1 发现(种子更优 → 命中率更高)
        ↓
阶段2 采集(先查缓存①，大部分秒出/跳品牌 → 越来越快、越省号)
        ↓
阶段3-7 门槛/评分/五池/交付
        ↓
阶段8 客户审核 → 批准者再进金种子库③(库更大更纯)
        │
        └──────────── 飞轮越转：种子越优 → 发现越准 → 库越大越独家 ────────────┘
```

**演进曲线**：
- 冷启动（现在）：金种子库空 → 用 Modash 内容关键词 + 品牌种子发现，全量浅扫建缓存。
- 起步后：金种子库有 N 个 → 优先用金种子 Lookalike 扩池；采集先查缓存，命中率上升。
- 成熟期：**日常 = 先查金种子库③ + 缓存① → 够就直接交付，不够才补货扫 IG**。越用越像"查库"而非"爬库"。

## 4. 具体路由规则（代码化口径）

| 规则 | 口径 |
|---|---|
| 发现种子优先级 | 金种子库③(tier=2) > Modash 客户批准者回流 > 品牌/内容关键词（冷启动兜底） |
| 采集前查库 | 命中缓存①且 last_scanned<30天 → 用缓存浅扫字段，跳过 instaloader；品牌号直接排除不深采 |
| 决策后进库② | 每个完整候选（含五池/评分/证据）写 candidates + creator tier=1 |
| 金种子进库③ | **仅** Herman Approval=Yes 或标记"已合作" → tier=2, client_status=approved, approved_at |
| 负向进库④ | Herman Approval=No → client_status=rejected + Herman's Feedback 原文；**不自动改门槛/config** |
| 金种子回流 | tier=2 的 handle 作下一轮 Modash Lookalike 种子（阶段1） |
| 水军进库⑤ | 评论分析命中 bot/pod → pod_accounts，跨 run 累积 |
| 纯度保护 | 负向库的 handle 不再进发现队列；金种子库定期用缓存刷新 storefront/粉丝档 |

## 5. 与现有资产的关系

- 复用 `creator_cache.py`（库①），扩 `tier` / `client_status` / `approved_at` / `rejected_reason` / `source_batch` 列承载③④。
- 复用 `db.py · discovery.db`（库②候选/审计）与 `pod_accounts.json`（库⑤）。
- 客户反馈通过 `import_client_feedback.py`（P2-3）按 batch_id+handle 回导，触发③④进库 + 金种子回流。
- 不违反纪律：客户个例偏好只进负向标签，**不自动泛化为 config 规则**；只有客户明确确认的新规则才写回 SOP。
