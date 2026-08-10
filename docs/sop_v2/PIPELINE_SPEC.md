# 四阶段模块化流水线 · 冻结规格（单一事实源）

初版日期：2026-07-16；当前修订：2026-07-28。经设计 workflow（4 facet 并行）+
对抗审查（8 blocking）收敛，并合入客户 2026-07-28 的 Storefront 与展示估价口径。
实现以本文为准。

## 0. 数据流总线

```
① discover(Modash,无号) → status=seed
② qualify(浏览器,轻)     → status=qualified | rejected
③ collect(浏览器,重)     → status=collected | rejected
④ decide(run_v2+Modash补数+export) → status=decided(含 Exclude 池) | + final_pool
反馈飞轮：客户 approve → tier=2 + client_status=approved（正交，不碰 status）
```
存放位置 = `creator_cache.db`（一张表）。三列**正交**：`status`=流水线走到哪 / `tier`=资产层级 / `client_status`=客户审批。

## 1. 冻结的状态机（5 态 + 软锁）

status ∈ `{seed, qualified, rejected, collected, decided}`。**不引入 qualifying/collecting 认领态**——用 `locked_at` 时间戳软锁（kill -9 不会卡死在 *ing）。

合法转移：`seed→qualified|rejected` · `qualified→collected|rejected` · `collected→decided`。
rejected 是每级旁路终态。任一级**瞬时失败**（号问题）→ status 不动、写 `stage_error`，`--resume` 重领。

## 2. 冻结的列名（禁止别名漂移）

新增列（全部 nullable，无 DEFAULT，走现有 `_MIGRATE` ALTER）：
- `status` / `stage_updated_at` / `locked_at`（软锁）/ `stage_error`（瞬时失败，可清零重试）
- `reject_reason`（机器淘汰原因，**区别于客户侧现有列 `rejected_reason`**）
- `discovery_batch` / `seed_followers`(int) / `modash_er` / `real_er` / `high_intent_count` / `final_pool` / `evidence_dir`
- `stage_json`（候选完整 dict 逐级累积的 JSON；候选事实源）

## 3. 三个致命坑的修法（对抗审查 high）

- **坑A：upsert 的 `INSERT OR REPLACE` 会删整行**→ 清零阶段字段、把 tier=2 金种子降级、丢 client_status（砸飞轮）。
  **修**：upsert 改 `INSERT ... ON CONFLICT(handle) DO UPDATE SET <仅 SHALLOW_FIELDS + last_scanned + times_seen+1>`，其余列一律不出现在 SET 里 → 天然不动 status/tier/client_status/stage_json。阶段写入走独立白名单 UPDATE（`advance`），None 不覆盖旧值。
- **坑B：缺 Modash 补数 → Include 恒空**。routing 的 `modash_core_missing` 要 `fake_pct/creator_country/top_audience_country`，浏览器零 API 拿不到 → 全批钉 Review。
  **修**：④decide 在调 run_v2 前，对**将 Include 的候选**走 Modash CDP 补数（尊重 `modash_budget.export_only_shortlist`）；补数失败**诚实降级 Review**（不阻塞出表）。测试阶段无 Modash 会话 → 候选诚实落 Review（符合预期，非 bug）。
- **坑C：wall/challenge 误判成 rejected → 一次限流永久烧号**。
  **修**：严格映射——`login_wall/logged_out/profile_fetch_failed/任何异常` →
  `mark_error`（status 不动，可重试）；Stage 2 仅
  `is_private/brand_account/followers_out_of_range/off_niche` → `reject`。
  `confirmed_no` 和任意非 Amazon Storefront 都必须继续深采；`unknown` 留给 Stage 4
  Review。Stage 3 的低 ER 或证据不足也不作机器早淘汰。process_one **禁止**
  `else→reject` 兜底。

## 4. 其它审查项的定夺

- **旧库 9 行回填**：迁移时一次性把**现有全部行**置 `status='decided'`（终态，旧红光设备测试数据不进新 Amazon 导购流水线）；用 `PRAGMA user_version` 守卫**只跑一次**，绝不每次 `_conn` 重跑。金种子 tier/client_status 不碰（正交）。
- **ig_er 显示**：④export 时 `cand.setdefault('ig_er', cand.get('real_er'))`（纯展示映射，不碰 gates）。
- **私密/粉丝档**：Stage 2 对 private/brand/followers_out_of_range/off_niche 记
  rejected，DB 可查审计；粉丝档只做**与 track 无关的宽粗筛**（<2k 或 >300k
  明显越界），精确分档留 `gate_followers`。Storefront 不属于 Stage 2 淘汰条件。
- **rejected 复发**：`should_ingest_seed` 对 `client_status='rejected'`（客户）与
  `status='rejected'`（机器）均排除；可变的 off_niche 跨轮复活必须走受控重排接口，
  不直接裸 SQL 改状态。历史 `no_amazon_storefront` 属规则漂移，应精确重排并保留审计。
- **并发**：`_conn()` 加 `PRAGMA journal_mode=WAL` + `busy_timeout=5000`；**同批默认单进程串行**，`locked_at`+stale 兜底孤儿。
- **证据路径**：新 pipeline 模块复用 `browser_collect_v2` 的 `_shot`/`relative_to(ROOT)`，ROOT=仓库根，与 `export_v2_xlsx.ROOT` 一致；不另算。
- **reason 码对齐**：机器浅扫淘汰码复用 gates/run_v2 的 `REASON_TEXT` 键
  （private/brand_account/followers_out_of_range/off_niche）。不得再生成
  `no_amazon_storefront`；低 ER 在当前口径进入 Review，而不是机器 rejected。

### 4.1 Storefront 三态

Storefront 表示通用电商购物入口，而非 Amazon 白名单：

- `confirmed_yes`：Amazon、LTK、ShopMy、明确自营店或已识别的购物聚合入口；
- `confirmed_no`：确认没有 Storefront，仍可完成深采并进入
  `Include-Without-Storefront`；
- `unknown`：证据不足，Stage 4 Review。

聚合页导航失败是瞬时采集错误或未知证据，不得降成 `confirmed_no`；非 Amazon 的有效购物
入口也不得降成“无橱窗”。

### 4.2 展示型预估报价

客户 2026-07-28 新增 `pricing_estimate`。采集顺序和公式冻结为：

```text
先排除置顶 Reels → 对剩余 Reels 按时间倒序取最近 10 条
登录态浏览器会话 → Instagram 同源 media info
average_plays = Σ ig_play_count / 实际合格样本数
default quote = average_plays × 35 / 1000 USD
range = average_plays × [35, 40] / 1000 USD
```

数据合同必须保留 `requested_reels=10`、`sample_count`、`pinned_excluded`、
`average_plays`、`source`、`captured_at`、逐 Reel 证据及
`quote_usd.default/min/max`、`population_basis` 和可复核的 `population_evidence`。每条媒体的总 `play_count` 和 `fb_play_count` 可以留作
审计，但不能进入 `average_plays` 或报价；报价指标优先且只使用 IG 原生
`ig_play_count`。状态只能是：

- `complete`：10 条 Instagram 原生合格样本；
- `complete_available`：严格证明总体已闭合，使用账号全部 1–9 条可用原生样本；
- `not_applicable_no_reels`：严格证明 Reels surface 为空或不存在，价格为空；
- `partial`：1–9 条原生样本但总体未闭合；
- `missing`：没有原生样本，也无法严格证明没有 Reels，价格为空。

短总体的常规证据是健康 Reels Tab 到底后连续两轮引用数/页面高度无增长；无 Reels surface
的独立证据为 `reels_surface_absent`，必须两次请求精确 `/reels/` 均回到同账号健康主页、
每次当前页都有普通帖子、无 exact Reels Tab link/Reel link、无
loading/login/challenge/private，并分别保存 `/p/` identity/计数/哈希快照；不得跨导航累计。
1–9 条的每个媒体还必须由同源 media-info 同时证明 pin list 与响应身份；已确认置顶的
Reel 是排除项、无需播放量，只有已确认非置顶的 Reel 才必须证明原生播放量。
每行 provenance 的 original 必须精确绑定本行 `code/url`；canonical alias 只接受 HTTPS
Instagram 同媒体类型、original 前缀与 requested 闭合，跨行复制证据不能通过。
Modash、总播放与 Facebook 播放禁止报价 fallback。`partial/missing` 和历史
`fallback_modash` 必须在 JSON/XLSX/HTML 中如实标注并阻断 B3。该值只用于展示，
不是实际报价，不得写 `paid_cpm`，也不得进入 Gate、F 模块、固定 Review 或五池路由。
实际报价和实际 Paid CPM 始终是另一类人工/报价证据。

pricing-only 写回先追加不可变 full ledger event，再按 canonical integrity、状态、样本和时间
单调选择 pricing owner/pointer/quality；失败尝试可审计但不能降级旧证据。写回不运行普通
Stage 3 comment retry transition，deep canonical 窗口保持不变。B3/Stage 4 将 raw evidence
重派生并核对完整报价合同字段，而不是信任存储对象中的手工值。

## 5. 冻结的 creator_cache 流水线 API（其余模块只调这些，不写裸 SQL）

```
seed_handles(recs, batch_id) -> dict          # ① dedup(should_ingest_seed)+写 status=seed(+seed_followers/modash_er)
claim_queue(from_status, limit, batch_id=None, stale_minutes=30) -> list[dict]  # 软锁认领+返回候选 dict(从 stage_json)
advance(handle, to_status, cand=None)         # 白名单 UPDATE: stage_json+热列+status+清 locked_at/stage_error
reject(handle, reason)                        # status=rejected + reject_reason + 清 locked_at
mark_error(handle, err)                       # stage_error + 清 locked_at（status 不动，可重试）
export_candidates(status, batch_id=None) -> list[dict]   # 读 stage_json 还原候选，喂 run_v2
status_dist(batch_id=None) -> dict            # 看板：各 status 计数
should_ingest_seed(handle) -> bool            # 去重：client_status/status='rejected' 或已在库 → False
# 现有 upsert/get/promote_golden/mark_rejected/is_rejected/golden_seeds/import_feedback 保持语义，仅 upsert 改 ON CONFLICT
```

## 6. 阶段模块布局

`scripts/extensions/sop_v2/pipeline/`：`__init__.py` · `_base.py`(run_stage 骨架) · `stage1_discover.py` · `stage2_qualify.py` · `stage3_collect.py` · `stage4_decide.py` · `run_pipeline.py`。
stage2/3 **复用** `browser_collect_v2` 的
`open_ctx/fetch_profile_browser/_resolve_storefront/_post_stats/_load_comments/_scroll_snippet_into_view/_goto/_shot/_pause`
（不 import instaloader/instagrapi）。Stage 3 另通过同一登录态浏览器会话调用 Instagram
同源 media info，保存非置顶 Reels 播放证据；
Stage 4 复用 `run_v2.decide` + `pricing.derive_quote_estimate` + `export_v2_xlsx`。

## 7. 验收门禁

1. 迁移后 golden 断言：tier=2 金种子仍在、`golden_seeds()` 仍返回它、client_status 不变。
2. 状态机往返：假 handle 走 seed→qualified→collected→decided，断言 status 流转 + stage_json 累积不丢。
3. upsert 不清零：对 collected 行重浅扫，断言 status/real_er/stage_json 不变。
4. 端到端：一个真实合格候选能走到 Include（依赖 ④Modash 补数）——测试期无 Modash 会话则诚实落 Review。
5. Storefront：Amazon/LTK/ShopMy/自营店/购物聚合均能归入 `confirmed_yes`；
   `confirmed_no` 不在 Stage 2 rejected；`unknown` 进入 Review。
6. 展示估价：先排置顶再取 10 条；覆盖完整、1–9 条、Modash fallback 和 missing；
   断言派生前后 `paid_cpm`、Gate、分数和最终路由均不变。
