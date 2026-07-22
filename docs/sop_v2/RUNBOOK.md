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
  （用你日常登录 Modash 的 Chrome profile；stage1 只读 AI Search 预览，不消耗 Profile 额度。）
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

# ② 浅扫合格（需 IG 号；--limit 控制本轮数量；--resume 断点续跑）
PYTHONPATH=. ../.venv/bin/python -m extensions.sop_v2.pipeline.stage2_qualify --batch-id $BID

# ③ 深采意图+实算ER（需 IG 号；--posts 10 = 前10帖算实算ER）
PYTHONPATH=. ../.venv/bin/python -m extensions.sop_v2.pipeline.stage3_collect --batch-id $BID --posts 10

# ④ 决策+交付（Modash 核心字段缺失会诚实进入 Review；人工 CSV 为可选补充）
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
- 某阶段中途挂（账号疲劳/被 kill）→ 同命令加 `--resume` 重跑未完成项（软锁 `locked_at` 陈旧回收）。
- 登录墙/挑战/超时 → 当前候选 `mark_error`、status 不动；本轮不会立即拿下一账号重试同一候选，需后续重跑。
- 候选明确不合格（品牌/私密/宽粉丝范围/非赛道等）→ `rejected + reject_reason`。
- V2 当前没有持久账号 cooldown、连续错误自动停用和 Account Lease；不要并行启动多个浏览器 worker。

## 4. Modash 与人工补数

Routing 对第三方核心字段缺失设置固定 Review：

- `--modash-cdp` 或 `--modash-csv`：补 `fake_pct / creator_country / top_audience_country` 等；
- `--manual-csv`：可补 Raw Skin、VO、报价、SHEIN/Temu 等人工事实；
- 当前 F 经济性模块整体延期为 N/A，Raw Skin/VO/报价不再是所有候选的固定 Review 条件；
- 不补 Modash 核心字段时诚实落 Review，不伪造数据。

## 5. 交付物
- `data/runs/<BID>/decisions.json`：五池决策（run_v2 结构，可复现）。
- `data/runs/<BID>/deliverable.xlsx`：批次总览、评论证据和五池客户交付表。
- `data/evidence/<BID>/<handle>/`：候选证据目录；当前稳定证据以评论原话、用户名和帖子 URL 为主。
> `data/` 运行内容全部 gitignore，不进仓库（含真实候选、证据和数据库）。

## 6. 客户反馈回流（飞轮）
交付表客户审核后，把 Herman Approval=Yes 的红人回导 → `promote_golden`(tier=2 金种子) → 下轮 stage1 可作 Modash Lookalike 种子；Approval=No → 负向库，discovery 不再重现。（回导脚本见 DB_FLYWHEEL_DESIGN.md。）
