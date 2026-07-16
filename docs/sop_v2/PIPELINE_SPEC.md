# 四阶段模块化流水线 · 冻结规格（单一事实源）

日期：2026-07-16。经设计 workflow（4 facet 并行）+ 对抗审查（8 blocking）收敛。实现以本文为准。

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
  **修**：严格映射——`login_wall/logged_out/profile_fetch_failed/任何异常` → `mark_error`（status 不动，可重试）；**仅** `is_private/brand_account/no_amazon_storefront/off_niche/real_er<0.5%` → `reject`。process_one **禁止** `else→reject` 兜底。`real_er` 缺失（未登录/未取到赞评）→ 不判 zombie，走 gate_real_er 的 `missing_is_review`→Review。

## 4. 其它审查项的定夺

- **旧库 9 行回填**：迁移时一次性把**现有全部行**置 `status='decided'`（终态，旧红光设备测试数据不进新 Amazon 导购流水线）；用 `PRAGMA user_version` 守卫**只跑一次**，绝不每次 `_conn` 重跑。金种子 tier/client_status 不碰（正交）。
- **ig_er 显示**：④export 时 `cand.setdefault('ig_er', cand.get('real_er'))`（纯展示映射，不碰 gates）。
- **私密/粉丝档**：stage2 rejected 处理（private/brand/no_storefront/off_niche 统一 rejected，DB 可查审计）；粉丝档只做**与 track 无关的宽粗筛**（<2k 或 >300k 明显越界），精确分档留 `gate_followers`。
- **rejected 复发**：本版 `should_ingest_seed` 对 `client_status='rejected'`(客户) 与 `status='rejected'`(机器) 均排除；可变原因(off_niche/no_storefront)的跨轮复活留作后续（可人工 `UPDATE status='seed'`）。
- **并发**：`_conn()` 加 `PRAGMA journal_mode=WAL` + `busy_timeout=5000`；**同批默认单进程串行**，`locked_at`+stale 兜底孤儿。
- **证据路径**：新 pipeline 模块复用 `browser_collect_v2` 的 `_shot`/`relative_to(ROOT)`，ROOT=仓库根，与 `export_v2_xlsx.ROOT` 一致；不另算。
- **reason 码对齐**：机器淘汰码复用 gates/run_v2 的 `REASON_TEXT` 键（no_amazon_storefront/brand_account/followers_out_of_range/real_er_low）。

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
stage2/3 **复用** `browser_collect_v2` 的 `open_ctx/fetch_profile_browser/_resolve_storefront/_post_stats/_load_comments/_scroll_snippet_into_view/_goto/_shot/_pause`（零 API，不 import instaloader）。stage4 复用 `run_v2.decide` + `export_v2_xlsx`。

## 7. 验收门禁

1. 迁移后 golden 断言：tier=2 金种子仍在、`golden_seeds()` 仍返回它、client_status 不变。
2. 状态机往返：假 handle 走 seed→qualified→collected→decided，断言 status 流转 + stage_json 累积不丢。
3. upsert 不清零：对 collected 行重浅扫，断言 status/real_er/stage_json 不变。
4. 端到端：一个真实合格候选能走到 Include（依赖 ④Modash 补数）——测试期无 Modash 会话则诚实落 Review。
