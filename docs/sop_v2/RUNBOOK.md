# 运行手册 · Amazon 导购红人四阶段流水线

给一起测试的人：按本手册从零跑通一批。架构见 [PIPELINE_SPEC.md](PIPELINE_SPEC.md)，策略见 [STRATEGY_LOCK.md](STRATEGY_LOCK.md)。

## 0. 一句话流程

```
① discover(Modash) → ② qualify(浏览器) → ③ collect(浏览器) → ④ decide(+Modash CSV补数) → deliverable.xlsx
      seed                qualified            collected           decided / 五池
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
| `accounts_raw.txt` | `username\|pw\|totp\|ds_user_id=..;sessionid=..\|email\|date` 每行一号 | IG 号池(stage2/3 用)。只需 cookie 段的 sessionid+ds_user_id 有效 |
| `proxy.txt` | `http://user:pass@host:port` | 住宅代理(换 IP 绕限流)，一行 |

- 号从供应商来。**开跑前先验证号能登录**（见 1.4），死号/风控挑战号剔除。
- `.secrets/` 下的 `accounts_cooled_*.txt` / `accounts_v2.txt` 是历史备份，不参与运行。

### 1.3 Modash（stage1 找种子 + stage4 补数）
- **stage1**：在一个 Chrome 里登录 Modash 并保持标签打开，用 **CDP 端口 9222** 启动它：
  ```bash
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --remote-debugging-port=9222
  ```
  （用你日常登录 Modash 的 Chrome profile；stage1 只读 AI Search 预览，不消耗 Profile 额度。）
- **stage4 补数**：在 Modash 对 shortlist 导出 **Profile Report CSV**（假粉/受众/国家/ER），喂 `--modash-csv`。这是解除 Include 恒空的关键（见 §4）。

### 1.4 账号健康自检（强烈建议开跑前跑）
```bash
PYTHONPATH=scripts .venv/bin/python scripts/dev/test_login_state.py .secrets/accounts_raw.txt
# 每号输出 ✅已登录 / 🔴已登出 / 🟡风控挑战。只留 ✅ 的进 accounts_raw.txt。
```

## 2. 跑一批（分阶段，推荐）

`cd scripts` 后各阶段独立跑（`PYTHONPATH=.` 让 `-m` 找到包）：
```bash
cd scripts
BID=SKIN-20260716            # 批次 id，自定

# ① 找种子（需 Chrome 开着已登录 Modash + 9222）
PYTHONPATH=. ../.venv/bin/python -m extensions.sop_v2.pipeline.stage1_discover --batch-id $BID

# ② 浅扫合格（需 IG 号；--limit 控制本轮数量；--resume 断点续跑）
PYTHONPATH=. ../.venv/bin/python -m extensions.sop_v2.pipeline.stage2_qualify --batch-id $BID

# ③ 深采意图+实算ER（需 IG 号；--posts 10 = 前10帖算实算ER）
PYTHONPATH=. ../.venv/bin/python -m extensions.sop_v2.pipeline.stage3_collect --batch-id $BID --posts 10

# ④ 决策+交付（--modash-csv 解除 modash_core_missing；--manual-csv 回填 Raw Skin/VO）
PYTHONPATH=. ../.venv/bin/python -m extensions.sop_v2.pipeline.stage4_decide --batch-id $BID --track paid \
    --out ../data/runs/$BID/decisions.json \
    --modash-csv ../data/source/$BID-modash.csv \
    --manual-csv ../data/source/$BID-manual.csv
# 产物：data/runs/$BID/decisions.json + deliverable.xlsx
```

一键（顺序跑四阶段，各阶段仍逐条落库、可续跑）：
```bash
PYTHONPATH=. ../.venv/bin/python -m extensions.sop_v2.pipeline.run_pipeline --batch-id $BID --track paid --resume
```

## 3. 看板 / 断点续跑 / 排错
```bash
PYTHONPATH=scripts .venv/bin/python -m extensions.sop_v2.creator_cache stats      # 各 status 计数 + 品牌/橱窗/金种子
```
- 某阶段中途挂（账号冷却/被 kill）→ 同命令加 `--resume` 重跑，只处理未完成项（软锁 `locked_at` 陈旧回收）。
- 号问题（登录墙/挑战/超时）→ 该候选 `mark_error`，**status 不动**，换号 `--resume` 即恢复，**绝不烧号**。
- 候选不合格（品牌/无橱窗/私密/非赛道/实算ER<0.5%）→ `rejected` + `reject_reason`，不再进后续阶段。

## 4. 为什么要 Modash CSV + 人工 CSV（Include 才非空）

routing 有**高分不覆盖的固定 Review**项。纯自动流水线拿不到的两类数据必须补，否则候选全钉 Review、Include 恒空：
- **`--modash-csv`**：补 `fake_pct / creator_country / top_audience_country`（Modash Profile Report 导出）→ 解除 `modash_core_missing`。
- **`--manual-csv`**：人工核验 `raw_skin_grade(A/B/C) / has_vo(1/0) / paid_cpm`（SOP §B4/B5 人工步骤）→ 解除 `raw_skin_or_vo_unverified`。
  CSV 列：`handle,raw_skin_grade,has_vo,paid_cpm[,shein_temu]`。
- 两者补齐后，候选按分数落 Include(≥75) / Priority-Review(65–75) / Review。**不补则诚实落 Review（不伪造数据）**。

## 5. 交付物
- `data/runs/<BID>/decisions.json`：五池决策（run_v2 结构，可复现）。
- `data/runs/<BID>/deliverable.xlsx`：客户交付表（批次总览 + 评论证据 + 五池 sheet，内嵌意图评论截图、可点 IG/Amazon 链接）。
- `data/evidence/<BID>/<handle>/intent_NN.png`：购买意图评论截图（交付表内链引用）。
> `data/` 全部 gitignore，不进仓库（含真实候选/截图/库）。

## 6. 客户反馈回流（飞轮）
交付表客户审核后，把 Herman Approval=Yes 的红人回导 → `promote_golden`(tier=2 金种子) → 下轮 stage1 可作 Modash Lookalike 种子；Approval=No → 负向库，discovery 不再重现。（回导脚本见 DB_FLYWHEEL_DESIGN.md。）
