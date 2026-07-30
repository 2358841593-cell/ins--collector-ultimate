# 运行手册 · 电商导购红人四阶段流水线

给一起测试的人：按本手册从零跑通一批。架构见 [PIPELINE_SPEC.md](PIPELINE_SPEC.md)，策略见 [STRATEGY_LOCK.md](STRATEGY_LOCK.md)。

## 0. 一句话流程

```
① discover(Modash) → ② qualify(浏览器) → ③ collect(浏览器) → ④ decide(+Modash补数) → JSON/XLSX/HTML
      seed                qualified            collected          decided / 五池 / 客户选择导出
```
状态全在 `data/creator_cache.db` 的 `status` 列，**跑到一半停、加 `--resume` 接着跑**。

## 1. 前置（一次性）

### 1.1 依赖
```bash
cd /path/to/ins-collector
.venv/bin/python -c "import playwright, openpyxl; print('deps ok')"   # 缺则 .venv/bin/pip install playwright openpyxl && playwright install chrome
```

### 1.2 秘钥文件（`.secrets/`，已 gitignore，绝不提交）
| 文件 | 格式 | 说明 |
|---|---|---|
| `accounts_raw.txt` | pipe 分隔账号行，Cookie 段含 `ds_user_id`、`sessionid` | Stage 2 浅扫池；V2 不读取密码/TOTP |
| `accounts_deep.txt` | 与浅扫池同格式 | Stage 3 隔离深采池；缺失时回退浅扫池 |
| `proxy.txt` | `http://user:pass@host:port` | 住宅代理(换 IP 绕限流)，一行 |

- 号从供应商来。**开跑前分别验证浅扫池和深采池**，死号/风控挑战号剔除。
- `.secrets/` 下的 `accounts_cooled_*.txt` / `accounts_v2.txt` 是历史备份，不参与主流程。
- 账号、密码、TOTP、Cookie、代理凭据和 Chrome profile 只留操作机，禁止提交 Git。

### 1.3 Modash（stage1 找种子 + stage4 补数）
- **stage1**：在一个 Chrome 里登录 Modash 并保持标签打开，用 **CDP 端口 9222** 启动它：
  ```bash
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --remote-debugging-port=9222
  ```
  （用你日常登录 Modash 的 Chrome profile；结构化搜索列表不消耗 Profile Report 额度。）
- 当前登录页使用 `https://marketer.modash.io/discovery/instagram`。Creator 精确搜索使用
  `filters.username`，响应优先读取 `serviceSdId` 并兼容历史 `servicePlatformId`；必须核对
  返回 Handle 完全一致。短暂空响应有限重试，旧 bulk discovery 只作 fallback。
- 有上一轮客户批准结果时，先用 `stage1_discover --export-golden-only` 导出金种子模板，
  人工在 Modash 执行 Lookalike 后用 `--golden-lookalikes-json` 和
  `--require-golden-lookalikes` 严格回导。现有 `lookalikesToken` 不能离线当作候选列表。
- **stage4 补数**：在 Modash 对 shortlist 导出 **Profile Report CSV**（假粉/受众/国家/ER），喂 `--modash-csv`。这是解除 Include 恒空的关键（见 §4）。

### 1.4 账号健康自检
```bash
PYTHONPATH=scripts .venv/bin/python scripts/dev/test_login_state.py .secrets/accounts_raw.txt
PYTHONPATH=scripts .venv/bin/python scripts/dev/test_login_state.py .secrets/accounts_deep.txt
# 每号输出已登录 / 已登出 / 风控挑战。只留确认健康的账号。
```

当前健康工具尚未接入 Pipeline，且检查出口与生产 Sticky 模型不完全一致；结果用于人工预筛，
不是自动调度状态。详见 [`../ACCOUNT_POOL_ARCHITECTURE.md`](../ACCOUNT_POOL_ARCHITECTURE.md)。

## 2. 跑一批（分阶段，推荐）

`cd scripts` 后各阶段独立跑（`PYTHONPATH=.` 让 `-m` 找到包）：
```bash
cd scripts
BID=SKIN-20260716            # 批次 id，自定

# ① 找种子（需 Chrome 开着已登录 Modash + 9222）
PYTHONPATH=. ../.venv/bin/python -m extensions.sop_v2.pipeline.stage1_discover --batch-id $BID --track paid

# ② 浅扫合格（需 IG 号；任意电商 Storefront/确认无 Storefront 都不在此早淘汰）
PYTHONPATH=. ../.venv/bin/python -m extensions.sop_v2.pipeline.stage2_qualify --batch-id $BID

# ③ 深采意图+实算ER+报价播放证据（先排置顶，再取最近10条非置顶 Reels）
PYTHONPATH=. ../.venv/bin/python -m extensions.sop_v2.pipeline.stage3_collect \
    --batch-id $BID --posts 10 --strict-completeness

# ④ 决策+展示估价+交付（Modash 核心字段缺失会诚实进入 Review；人工 CSV 为可选补充）
PYTHONPATH=. ../.venv/bin/python -m extensions.sop_v2.pipeline.stage4_decide --batch-id $BID --track paid \
    --strict-completeness --full-deep-all-candidates --deep-target-posts 10 \
    --out ../data/runs/$BID/decisions.json \
    --xlsx ../data/runs/$BID/deliverable.xlsx \
    --modash-csv ../data/source/$BID-modash.csv \
    --manual-csv ../data/source/$BID-manual.csv
PYTHONPATH=. ../.venv/bin/python ../scripts/export_v2_html.py \
    --decisions ../data/runs/$BID/decisions.json \
    --out ../data/runs/$BID/deliverable.html
# 产物：decisions.json + deliverable.xlsx + deliverable.html
```

Stage 3 每次先刷新当前主页，冻结最近 N 帖作为核心深采窗口；对每个成功打开的帖子，
只有明确 `comment_count=0` 才可直接完成评论核验，评论数为正或未知时必须尝试加载评论。
导航失败、互动指标缺失、评论核验失败分别写入 `deep_failed_posts`、
`deep_metric_missing_posts`、`comment_failed_posts`。报价则独立进入 Reels Tab，
采集最近 10 条非置顶 Reels，不覆盖主页核心窗口。

旧批记录如果没有新版显式 contract 字段，只能按严格 legacy-evidence 边界复用：
至少 10 个结构化 `sampled_posts`、`comments_analyzed > 0`、`valid_comments >= 20`、
`real_er` 非空，并且存在可观察互动指标。达不到任一条件就补采，不能因“历史已跑过”
而跳过正式门禁。

一键（顺序跑四阶段，各阶段仍逐条落库、可续跑）：
```bash
PYTHONPATH=. ../.venv/bin/python -m extensions.sop_v2.pipeline.run_pipeline --batch-id $BID --track paid --resume
```

## 3. 看板 / 断点续跑 / 排错
```bash
PYTHONPATH=scripts .venv/bin/python -m extensions.sop_v2.creator_cache stats      # 各 status 计数 + 品牌/橱窗/金种子
```
- 某阶段中途挂（账号疲劳/被 kill）→ 同命令加 `--resume` 重跑未完成项（软锁 `locked_at` 陈旧回收）。
- 登录墙/挑战/超时 → 当前候选 `mark_error`、status 不动；本轮不会立即拿下一账号重试同一候选，需后续重跑。
- 候选明确不合格（品牌/私密/宽粉丝范围/非赛道等）→ `rejected + reject_reason`。
- Amazon、LTK、ShopMy、明确自营店和已识别的购物聚合入口都算 Storefront；
  `confirmed_no` 仍继续深采，`unknown` 才在决策阶段 Review。看到
  `no_amazon_storefront` 早淘汰属于历史规则漂移，正式批次应停止并检查版本。
- V2 当前没有持久账号 cooldown、连续错误自动停用和 Account Lease；不要并行启动多个浏览器 worker。

## 4. Modash 与人工补数

Routing 对第三方核心字段缺失设置固定 Review：

- `--modash-cdp` 或 `--modash-csv`：补 `fake_pct / creator_country / top_audience_country` 等；
- `--manual-csv`：可补 Raw Skin、VO、实际报价/实际 CPM、SHEIN/Temu 等人工事实；
- 当前 F 经济性模块整体延期为 N/A，Raw Skin/VO/报价不再是所有候选的固定 Review 条件；
- 不补 Modash 核心字段时诚实落 Review，不伪造数据。

### 4.1 展示型预估报价

该字段不需要先取得红人实际报价。客户 2026-07-28 的固定口径为：

```text
先排除置顶 Reels
→ 对其余 Reels 按发布时间倒序取最近 10 条
→ 登录态浏览器会话读取 Instagram 同源 media info
→ 平均播放量 = 合格样本的 IG 原生 ig_play_count 总和 / 样本数
→ 默认预估报价 = 平均播放量 × 35 / 1000 USD
→ 预估区间 = 平均播放量 × 35 / 1000 ～ 平均播放量 × 40 / 1000 USD
```

逐条媒体返回的总 `play_count` 和 `fb_play_count` 只作审计，禁止进入均播或报价；
不能用跨发 Facebook 播放抬高估价。
这一报价取样窗口独立于 Stage 3 的主页最近 N 帖核心深采窗口，两套 URL 和完整性状态
必须分别保存、分别审计。

运行后逐候选检查 `pricing_estimate`：

- `complete`：原生样本 `10/10`；
- `partial`：原生样本 `1–9/10`，报价可展示但必须带样本不足提示；
- `fallback_modash`：没有合格原生样本，采用 Modash `avg_reels_plays`，必须标注第三方来源；
- `missing`：原生与 Modash 都缺失，报价留空，不能填 0。

以上状态必须原样进入交付，不能把部分样本、fallback 或缺失写成完整原生窗口。
`pricing_estimate.quote_usd.default/min/max` 是展示估算，不是实际报价；不得人工复制
到 `paid_cpm`，也不会影响 Gate、A-F、固定 Review 或五池。若客户或代理之后返回实际
USD 报价，应作为独立人工证据保存，再另算实际 Paid CPM。

## 5. 交付物

- `data/runs/<BID>/decisions.json`：五池决策（run_v2 结构，可复现）。
- `data/runs/<BID>/deliverable.xlsx`：批次总览、评论证据、展示型预估报价和五池客户交付表。
- `data/runs/<BID>/deliverable.html`：自包含客户评审页，可选择、填原因并导出客户决策 JSON。
- `data/evidence/<BID>/<handle>/`：候选证据目录；当前稳定证据以评论原话、用户名和帖子 URL 为主。
> `data/` 运行内容全部 gitignore，不进仓库（含真实候选、证据和数据库）。

发布前把同一基线的三个文件写入
`reports/deliveries/<BID>/formal-<YYYYMMDD>-r<N>/` 并保存 SHA-256。发给客户后该目录只读：
修复必须递增 `rN`，不得覆盖已交付版本。当前审计基线
`SKIN4-20260723/formal-20260729-r2` 为 157 人（89 carryover + 68 new）、
`retry_pending_count=0`，定价 156 个 `complete` + 1 个 `fallback_modash`；
`formal-20260728-r1` 保持冻结。

## 6. 客户反馈回流（飞轮）

客户反馈必须带原交付基线安全回导：

```bash
PYTHONPATH=scripts .venv/bin/python \
  -m extensions.sop_v2.pipeline.ingest_client_decisions \
  --file /absolute/path/client_decisions_<OLD_BID>.json \
  --source-decisions reports/deliveries/<OLD_BID>/formal-<YYYYMMDD>-r<N>/decisions.json \
  --dry-run
```

去掉 `--dry-run` 才会在单事务中写客户状态和 SHA ledger。随后用
`prepare_next_round` 生成 carryover manifest；精确补采、人工 Lookalike 回导和组合 Stage 4
命令见仓库根 [`README.md`](../../README.md)“客户反馈回流与下一轮准备”。不要改写旧账号的
`discovery_batch`，也不要用 `--all-batches` 代替 carryover 精确组合。正式交付必须保留
`audit_collect --strict --full-deep-all-candidates --target-posts 10`、Stage 3
`--strict-completeness`，以及 Stage 4
`--strict-completeness --full-deep-all-candidates --deep-target-posts 10` 三层完整性门禁。

Carryover 的 Stage 4 复跑必须幂等：`needs_pipeline_retry=true` 的历史
`collected/rejected` 可继续；已经是 `decided` 的记录只有在 `_stage_error` 为空且
`strict_deep_reasons` 通过时才可复用。相同 manifest 再跑后若
`retry_pending_count` 不是 0，停止发布并回到精确补采。
