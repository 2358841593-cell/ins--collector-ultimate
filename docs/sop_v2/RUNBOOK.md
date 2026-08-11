# 运行手册 · 电商导购红人四阶段流水线

给一起测试的人：按本手册从零跑通一批。架构见 [PIPELINE_SPEC.md](PIPELINE_SPEC.md)，策略见 [STRATEGY_LOCK.md](STRATEGY_LOCK.md)。

## 0. 一句话流程

```mermaid
flowchart LR
    S1["① Stage 1 原子发现"] --> B1{"B1<br/>目标/配额/去重对账"}
    B1 --> GRAPH["graph_runner<br/>唯一生产调度入口"]
    GRAPH --> S2["② Stage 2<br/>唯一浅采 producer"]
    GRAPH --> S3["③ Stage 3 consumer group<br/>互斥切片 / 每波动态均分<br/>片内轮换 / unseen-first"]
    S2 -->|"qualified + unlocked"| S3
    S2 -->|"done"| B2{"B2 · producer 闭合<br/>总数/seed/Stage 2 输出对账"}
    S3 --> DRAIN{"qualified 已 drain"}
    B2 --> SNAP["reject 快照"]
    DRAIN --> SNAP
    SNAP --> TAIL["公开 reject deep tail"] --> RESUME["graph_runner<br/>resume-from-stage3"] --> S3
    SNAP --> PRIVATE["private 终态<br/>当前合同未支持例外"]
    S3 --> B3{"B3<br/>全量 deep/pricing/translation"}
    B2 --> B3
    PRIVATE -. "存在即阻断正式流" .-> B3
    B3 --> ENRICH["Storefront collect-plan/apply<br/>+ Modash all-missing enrich"]
    ENRICH --> S4["④ strict Stage 4<br/>内部决策基线"]
    S4 --> B4{"B4 · post-enrichment<br/>exact state hash + 六项失败为 0"}
    B4 --> EXPORT["版本化客户导出<br/>JSON / XLSX / HTML"]
```

这是唯一正式调度规范：B1 前不启动 Instagram；B1→B2 只由 `graph_runner` 运行一个 Stage 2
producer 和一个或多个 Stage 3 consumer worker；B2 后才处理公开 reject tail，再由同一
runner 只恢复 Stage 3；B3 后按 Storefront/Modash enrichment、strict Stage 4、B4、export
顺序执行。不得跨 Barrier，也不得把默认流程临时改成全串行或手工多终端。进程退出码、
瞬时空队列和 Stage 4 文件存在都不是正式交付完成证明。

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
| `accounts_deep.txt` | 与浅扫池同格式 | Stage 3 隔离深采池；正式并发必须存在且与浅扫池无账号交集 |
| `proxy.txt` | `http://user:pass@host:port` | 住宅代理(换 IP 绕限流)，一行 |

- 号从供应商来。**开跑前分别验证浅扫池和深采池，并核对用户名集合无交集**，死号/风控
  挑战号剔除。代码在深采文件缺失时会回退浅扫池，但这只供开发诊断；正式并发必须停机。
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
- 有上一轮客户批准结果时，先用 `stage1_discover --export-golden-only` 导出金种子模板；
  再运行 `python -m extensions.sop_v2.pipeline.modash_golden_lookalikes`，并传入同一
  `--round-contract --require-round-contract`。该步骤使用已验证的 marketer 同源
  Lookalike 请求，逐 seed 分页、质量预过滤并原子保存断点；完成文件再用
  `stage1_discover --golden-lookalikes-json --require-golden-lookalikes` 严格回导。
  `lookalikesToken` 仍不能离线当作候选列表；自动契约失效时可人工填写同一模板 fallback。
- **预算草稿补数**：默认 `route_actionable` shortlist 与 `--modash-cap 20` 只购买乐观补齐后
  可能改变路由的报告，用于控制预算，不等于正式字段完整。
- **正式交付补数**：必须同时使用
  `--modash-all-missing --modash-cap 0 --strict-enrichment-completeness`。它只抓
  `modash_report is not True` 的候选；报告已经存在但某个源字段为空，交付显示“Modash无”，
  不得再次购买。完整新批 120 人约需 120 个 Profile credit；当前 SKIN6 已有 4 份通过身份
  校验的缓存，因此增量为 116。结构化搜索和 Golden Lookalike 结果列表不消耗 Profile
  Report credit；只有获取 Profile Report 才计入这里的额度。

### 1.4 账号健康自检
```bash
PYTHONPATH=scripts .venv/bin/python scripts/dev/test_login_state.py .secrets/accounts_raw.txt
PYTHONPATH=scripts .venv/bin/python scripts/dev/test_login_state.py .secrets/accounts_deep.txt
# 每号输出已登录 / 已登出 / 风控挑战。只留确认健康的账号。
```

当前健康工具尚未接入 Pipeline，且检查出口与生产 Sticky 模型不完全一致；结果用于人工预筛，
不是自动调度状态。详见 [`../ACCOUNT_POOL_ARCHITECTURE.md`](../ACCOUNT_POOL_ARCHITECTURE.md)。

### 1.5 graph runner 预检与本地工件

账号健康确认后，正式 Stage 2/3 不再手工维护 worker 清单。`graph_runner` 会在启动子进程前：

- 验证浅/深账号 username 与 Chrome Profile 集合无交集、无别名，Profile 没有活动锁标记；
- 按 `--workers` 确定性均分深采池，生成互斥 offset/count；每个 consumer wave 只在自己的
  切片内按 `account_rotation_base + 本 schedule consumer intent ordinal` 轮换账号起点，
  resume 不从 0 重新轮换；
- 每波读取 `qualified_unlocked_count=Q` 快照并冻结 `claim_plan`，将有限正数 claim cap 动态
  均分给全部 worker；两个 worker、上限 24 时，`20→10/10`、`45→23/22`；
- 冻结账号顺序、片内轮换/claim 分片策略、非负 `account_rotation_base`、代码/配置/合同/
  Stage 1 artifact SHA 和运行参数；
- 原子获取 Stage 2 singleton 及各 worker 的账号/Profile bundle lease。

它自动维护 `data/runs/$BID/graph_schedule.json`（语义 append-only 波次，含完整
`claim_plan`）、同目录 `graph_events.jsonl`（append-only、fsync、hash-chained 事件）和
`data/resource_leases.db`。
这些文件只留本机且不进 Git；schedule/event 只记录 username、Profile 路径和摘要，不记录
Cookie 值、密码、TOTP 或代理凭据。旧 `stage3_worker_schedule.json` 与终端 A/B/C 手工清单
只供历史复现/故障诊断，不再是正式互斥或完成证明。

## 2. 跑一批（固定并发 DAG，唯一正式方式）

### 2.1 Stage 1 与 Barrier 1

Stage 1 独占运行；其间 Instagram worker 必须为 0：

```bash
cd scripts
BID=SKIN-YYYYMMDD

PYTHONPATH=. ../.venv/bin/python -m extensions.sop_v2.round_contract \
    --batch-id "$BID" --track paid --carryover-mode new_only \
    --out "../data/batches/$BID/round_contract.json"

PYTHONPATH=. ../.venv/bin/python -m extensions.sop_v2.pipeline.stage1_discover \
    --batch-id "$BID" --track paid \
    --round-contract "../data/batches/$BID/round_contract.json" --require-round-contract
```

只有全部来源、配额、全局去重、来源归因和轮次指纹都通过，完整 seed 集合一次性落库且数量
等于本轮目标，才具备 B1 条件。Stage 1 失败、短缺或仍在运行时不得启动 Stage 2；随后由
`graph_runner` 以 Stage 1 artifact 和 round contract 重新验证 B1，退出码本身不能替代对账。

### 2.2 用 graph runner 运行 Stage 2/3 DAG

Stage 1 完成后只执行这一条生产命令；不要另开终端手工启动 Stage 2 或 Stage 3：

```bash
cd scripts
BID=SKIN-YYYYMMDD
ROUND_CONTRACT="../data/batches/$BID/round_contract.json"
STAGE1_ARTIFACT="../data/runs/$BID/stage1_barrier_artifact.json"
ROTATION_BASE=0  # 全新 schedule 默认 0；跨 schedule 续接时填审计得出的下一个未用序号

PYTHONPATH=. ../.venv/bin/python -m extensions.sop_v2.pipeline.graph_runner \
    --batch-id "$BID" \
    --stage1-artifact "$STAGE1_ARTIFACT" \
    --round-contract "$ROUND_CONTRACT" \
    --schedule "../data/runs/$BID/graph_schedule.json" \
    --event-log "../data/runs/$BID/graph_events.jsonl" \
    --workers 2 --worker-limit 24 --posts 10 \
    --account-rotation-base "$ROTATION_BASE" \
    --translation-provider ollama --translation-model qwen3.5:4b \
    --max-consumer-waves 100 --wave-cooldown 120 --max-no-progress-waves 3 \
    --lease-ttl 120 --heartbeat-interval 30
```

runner 的固定行为：

1. B1 未通过、账号池/Profile 不互斥、Profile 正在使用或 worker 读取路径与预检路径不一致时，
   在启动浏览器前失败；
2. 恰好启动一个 Stage 2 producer；Stage 3 在出现 `qualified + unlocked` 后以有限波次消费。
   实际账号轮换序号为 `account_rotation_base + 本 schedule 从 0 起的 consumer intent ordinal`；
   CLI 默认 base 为 `0`、只接受非负整数，并将其冻结进 run contract。
   每波按启动前的 `qualified_unlocked_count=Q` 快照冻结 claim cap：两个 worker、上限 24 时
   `Q=20` 为 `10/10`、`Q=45` 为 `23/22`，避免可并行尾批退化成单 worker；若
   `0<Q<workers`，全切片仍使用正 `cap=1`，由原子 claim 决定实际领取者并保持整池
   账号/Profile lease 互斥。`Q=0` 不创建波次，`limit=0` 禁用；producer 仍活着时只等待；
3. 候选由 `BEGIN IMMEDIATE` 原子 claim，先领取未尝试项、再按最旧更新时间公平领取失败项；
   执行期间 candidate heartbeat 续租，最终写入和清锁使用 token/CAS；账号/Profile 则由独立
   bundle lease heartbeat/CAS 保护；
4. 每波把快照数、计划领取量和各 worker cap 写入 schedule 的不可变 `claim_plan`，把带 wave
   SHA/分配摘要的 intent 追加到 hash-chained event；分片策略与配置上限属于 run contract。
   未 drain 时波间默认 cooldown 120 秒，连续 3 波 `collected_delta=0` 或达到 100 波会失败停机；
5. Stage 2 实际退出后自动计算 B2；runner 仅在 B2 通过且当前 `qualified` 队列 drain 后返回；
6. worker 收到 `SIGTERM` 会展开到 `finally`，释放当前/待处理候选 claim 后非零退出；runner
   再按 token 释放账号/Profile bundle lease并写终止事件。kill -9/断电只能依赖 heartbeat TTL
   和 CAS 接管，不能手工删租约。

进程挂掉后，将上面同一命令原样执行并追加 `--resume`。resume 会交叉校验 schedule、完整
事件链、run contract、持久 `claim_plan`、账号顺序以及代码/配置/合同/参数 SHA；快照数、cap、
worker 集合、分片策略或指纹任一不一致都在 spawn 前 fail-closed，不能通过改 worker 数、账号
顺序或配置来绕过。若租约仍在 TTL 内，等待原 lease 到期或确认原进程退出，禁止删除 lease
数据强行抢 Profile。

若事故修复确实改变了 `graph_runner.py`、冻结 worker 代码、配置或调度语义，不得再 resume
原 hash 链。保留并明确结束旧 schedule/event，改用新的版本化 `graph_schedule.r<N>.json` 和
`graph_events.r<N>.jsonl` 建立新运行合同；禁止原地修改旧 schedule 让新代码接着跑。此时
不能沿用默认 base `0`：从全部相关旧审计链读取已持久 consumer intent 的实际 rotation，取
最大值加 1 作为新 schedule 的 `--account-rotation-base`。只看 durable intent 的 rotation
序号，不按 schedule 数量或重复序号累加；intent 一经持久化，即使 worker 随后失败也占用序号。

本批 `SKIN6-20260810` 的 r4 必须使用 `--account-rotation-base 4`：r2 有四个 durable consumer
intents，已使用 rotation `0–3`；r3 是验证性中止且重用了 `0`，没有提高历史最大值，所以下一个
未用序号仍为 `4`。

以前的终端 A/B/C、手写 `--account-offset/--account-count` 和人工
`stage3_worker_schedule.json` 现仅供历史复现/诊断；直接 worker 命令会绕过资源 lease、自动
波次、事件链、cooldown/no-progress 和 B1/B2 监督，不能用于正式交付。

### 2.3 Stage 2 done Barrier、reject 快照与 deep tail

`graph_runner` 观察到 Stage 2 实际退出后会自动计算 B2；失败则整图失败。B2 只证明浅采
producer 闭合：数据库必须证明本轮总数等于 B1 合同目标、`seed=0`，且
`qualified + collected + rejected` 等于合同目标；仅对
`status='seed'` 的 Stage 2 工作集要求 `stage_error=0`、`locked_at IS NULL`。并发 Stage 3
仍可能持有 `qualified` 锁或写入 `deep_incomplete` 等错误，所以全批 errors/locks 此时允许
非零，统一在 B3 清零。runner 只有在 B2 通过且当前 `qualified` 队列 drain 后才返回；随后
先保存机器 reject 快照，再只选择明确公开可访问的账号进入 deep tail：

```bash
cd scripts
BID=SKIN-YYYYMMDD
mkdir -p "../data/runs/$BID"

sqlite3 -header -column ../data/creator_cache.db \
  "SELECT COUNT(*) AS total,
          SUM(status='seed') AS seed,
          SUM(status='qualified') AS qualified,
          SUM(status='collected') AS collected,
          SUM(status='rejected') AS rejected,
          SUM(status IN ('qualified','collected','rejected')) AS stage2_output_total,
          SUM(status='seed' AND stage_error IS NOT NULL) AS seed_errors,
          SUM(status='seed' AND locked_at IS NOT NULL) AS seed_locked,
          SUM(status='qualified' AND stage_error IS NOT NULL) AS deep_errors_observed,
          SUM(status='qualified' AND locked_at IS NOT NULL) AS deep_locked_observed
   FROM creator_profiles WHERE discovery_batch='$BID'"

sqlite3 -json ../data/creator_cache.db \
  "SELECT handle,reject_reason,json_extract(stage_json,'$.is_private') AS is_private \
   FROM creator_profiles WHERE discovery_batch='$BID' AND status='rejected' \
   ORDER BY lower(handle)" \
  > "../data/runs/$BID/stage2_reject_snapshot.json"

sqlite3 ../data/creator_cache.db \
  "SELECT handle FROM creator_profiles \
   WHERE discovery_batch='$BID' AND status='rejected' \
     AND json_extract(stage_json,'$.is_private')=0 \
   ORDER BY lower(handle)" \
  > "../data/runs/$BID/public_reject_deep_tail.txt"

if [[ -s "../data/runs/$BID/public_reject_deep_tail.txt" ]]; then
  PYTHONPATH=. ../.venv/bin/python -m extensions.sop_v2.pipeline.audit_collect \
      --batch-id "$BID" \
      --handles-file "../data/runs/$BID/public_reject_deep_tail.txt" \
      --strict --full-deep-all-candidates --target-posts 10 --requeue
fi
```

必须先写快照，因为 requeue 会清机器 `reject_reason`。private 不得加入 deep tail，也不得伪造
帖子证据；当前严格合同还没有 private 终态例外，存在 private 时正式运行必须停在 B3 前，
等待合同与实现共同支持。不得删行或临时取消 `--full-deep-all-candidates` 绕过。

公开 reject tail 入队后，用同一冻结合同和同一 schedule/event 显式恢复 Stage 3；该模式先
验证持久 B2，绝不重启 Stage 2：

```bash
ROTATION_BASE=0  # 必须填写该 schedule 的 run contract 已冻结值
PYTHONPATH=. ../.venv/bin/python -m extensions.sop_v2.pipeline.graph_runner \
    --batch-id "$BID" \
    --stage1-artifact "../data/runs/$BID/stage1_barrier_artifact.json" \
    --round-contract "../data/batches/$BID/round_contract.json" \
    --schedule "../data/runs/$BID/graph_schedule.json" \
    --event-log "../data/runs/$BID/graph_events.jsonl" \
    --workers 2 --worker-limit 24 --posts 10 \
    --account-rotation-base "$ROTATION_BASE" \
    --translation-provider ollama --translation-model qwen3.5:4b \
    --max-consumer-waves 100 --wave-cooldown 120 --max-no-progress-waves 3 \
    --lease-ttl 120 --heartbeat-interval 30 \
    --resume --resume-from-stage3
```

严格失败项本来就保持 `qualified` 并保存部分证据，runner 会自动追加新波次继续；不要每轮
统一 requeue，否则低量评论的跨轮失败历史会被清掉。

Stage 3 重试不会把新结果原地覆盖到旧深采字段。每次尝试先使用隔离工作副本，结束后追加
不可变 `deep_collection_attempts` entry，再按确定性质量元组更新
`deep_canonical_attempt_id`；较差尝试只留审计记录，不会让 canonical 倒退。只有双方都有
完整、有效且完全相同的 ordered target window 时，才按 media identity 合并互补证据并写
synthetic attempt/provenance；跨窗、乱序、短缺或 identity 不一致一律 whole-attempt 择优。

评论按 media identity 保存 `resolved/retry_pending/terminal_unavailable`：只有实际评论抽取
失败累计连续次数，导航失败不累计，成功或明确 0 评论复位。`verified_empty_thread` 与
`verified_zero` 严格分离：前者必须保留正数 `reported_count`，且只接受页面可见叶节点精确
`No comments yet.` 与登录态 comments endpoint 的双证据；endpoint 必须 HTTP 200、
`status=ok`、IG/FB 评论数均为 0 且所有 `has_more*` 为 false。帖子与 unavailable 汇总中的
media identity、`reported_count`、source、marker、endpoint summary/provenance 必须完全一致，
否则继续失败。普通 `reported_count>2` 的空抽即使重复也不得终止；只有 1–2 条低量媒体可按
既有合同在连续真实失败后进入 `terminal_unavailable`。Reels 报价用独立
`pricing_canonical_attempt_id` 择优，不参与主页/评论 canonical 胜负。不要手工编辑这些字段；
它们是内部审计状态，客户 JSON 在 Stage 4 导出边界会剥离 ledger、canonical 指针/质量和
评论重试状态。

### 2.4 B3、独立 Enrichment、strict Stage 4 与 B4

```bash
# 只读检查，禁止在这个循环里加 --requeue
PYTHONPATH=. ../.venv/bin/python -m extensions.sop_v2.pipeline.audit_collect \
    --batch-id "$BID" --strict --full-deep-all-candidates --target-posts 10
```

B3 由 `barriers.evaluate_b3` 的 fail-closed 语义定义：`audit/pricing/translation` 任一新鲜
失败计数缺失或非零都阻断。它必须同时证明：生产端已闭合且总数一致；无 seed、qualified、
错误或锁，包括 B2 时允许
存在的深采错误和 qualified 锁；公开 cohort 全部严格深采；报价证据/状态逐项对账且没有
未处理缺口；全部已存评论翻译状态为
`complete/not_needed`，没有 `failed`、`source_unavailable` 或覆盖不足。Stage 命令的 0 退出码
不是完成证明。B3 前禁止启动 Modash Profile enrich，避免为最终仍会被阻断的候选消耗额度。

报价失败计数用同一冻结代码做只读审计；该命令不加锁、不启浏览器、不写库：

```bash
PYTHONPATH=. ../.venv/bin/python \
  -m extensions.sop_v2.pipeline.stage3_pricing_backfill \
  --batch-id "$BID" --audit-only
```

只有 `complete`、严格闭合的 `complete_available`、严格闭合的
`not_applicable_no_reels` 算通过；输出的 `failures=N` 注入 B3 的 `pricing` 计数。

翻译必须已经在 Stage 3 canonical attempt 和 B3 前完成。B3 通过后先补 Storefront，不能为此
重跑 Stage 3。`collect` 以只读方式锁定“当前状态下仍为 unknown”的精确 cohort；新批在 B3
之后通常用 `collected`，对已经完成旧 Stage 4 的 r1 修复则显式用 `decided`。先只读算出 unknown
人数与 Handle SHA-256：

```bash
DB="../data/creator_cache.db"
SF_STATUS="collected"  # 旧 r1 修复填 decided

PYTHONPATH=. ../.venv/bin/python - "$DB" "$BID" "$SF_STATUS" <<'PY'
import json, sys
from extensions.sop_v2.pipeline import storefront_backfill as sf
rows = sf.inspect_unknown_cohort(sys.argv[1], batch_id=sys.argv[2], expected_status=sys.argv[3])
print(json.dumps({
    "expected_count": len(rows),
    "expected_handle_set_sha256": (
        sf.handle_set_sha256(row["handle"] for row in rows) if rows else None
    ),
}, ensure_ascii=False))
PY
```

若 `expected_count=0`，说明 Storefront 已全部明确，跳过 collect/apply。否则把只读输出原样填入
下面两个变量；不得凭印象填写人数或 Handle SHA。plan 文件不可覆盖，
重试必须换新的 `rN` 路径：

```bash
SF_COUNT=<expected_count>
SF_HANDLE_SHA=<expected_handle_set_sha256>
SF_PLAN="../data/runs/$BID/storefront-plan-r2.json"

PYTHONPATH=. ../.venv/bin/python \
  -m extensions.sop_v2.pipeline.storefront_backfill collect \
  --db "$DB" --batch-id "$BID" --expected-status "$SF_STATUS" \
  --expected-count "$SF_COUNT" --expected-handle-set-sha256 "$SF_HANDLE_SHA" \
  --plan-out "$SF_PLAN" \
  --accounts-file ../.secrets/accounts_raw.txt \
  --deep-accounts-file ../.secrets/accounts_deep.txt \
  --profile-root ../.secrets/chrome-instagram-profiles \
  --lease-db ../data/resource_leases.db
```

`collect` 强制使用公开代理，并为浅扫账号、Chrome Profile 和批次 singleton 获取带 heartbeat
的 resource lease。每个 unknown 最多以两个不同浅扫账号尝试；任何账号/Profile 预检、代理、
身份、Bio 链或目标页证据不闭合都保留 `unknown`。此时命令返回 3、plan 的
`apply_allowed=false`，禁止 apply；排障后用新的 plan revision 重采，不能把 unknown 手改成 no。

人工核对 plan 中人数、Handle、逐条证据、`unresolved_count=0`、`apply_allowed=true` 和输出的
`plan_sha256` 后，才执行：

```bash
SF_PLAN_SHA=<collect输出的plan_sha256>
PYTHONPATH=. ../.venv/bin/python \
  -m extensions.sop_v2.pipeline.storefront_backfill apply \
  --db "$DB" --batch-id "$BID" --expected-count "$SF_COUNT" \
  --plan "$SF_PLAN" --expected-plan-sha256 "$SF_PLAN_SHA"
```

`apply` 在一个 `BEGIN IMMEDIATE` 事务中重验全批人数、Handle 集合、plan SHA、每行
`stage_json`/状态/锁/错误/热列 CAS 和受保护内容 SHA；只允许修改 Storefront allowlist，任一行
漂移即整批回滚，因此不会重写深采、报价、翻译或 attempt ledger。

Storefront 全部成为 `confirmed_yes/confirmed_no` 后，运行正式 Modash all-missing 与 strict
Stage 4。下面先只生成内部 `decisions.json` 基线；B4 通过前不得生成或发送客户 XLSX/HTML：

```bash
ROUND_CONTRACT="../data/batches/$BID/round_contract.json"
STAGE1_ARTIFACT="../data/runs/$BID/stage1_barrier_artifact.json"

PYTHONPATH=. ../.venv/bin/python -m extensions.sop_v2.pipeline.stage4_decide \
  --batch-id "$BID" --track paid \
  --round-contract "$ROUND_CONTRACT" --require-round-contract \
  --strict-completeness --full-deep-all-candidates --deep-target-posts 10 \
  --strict-comment-translations --strict-enrichment-completeness \
  --modash-all-missing --modash-cap 0 --cdp http://127.0.0.1:9222 \
  --out "../data/runs/$BID/decisions.json" --no-xlsx
```

正式模式只抓 `modash_report is not True`；身份校验通过的 raw cache 直接复用且不连接 CDP。
已有报告即使 `fake_pct`、国家、语言等个别源字段为空，也表示“报告存在、Modash无”，不能重买。
Credit 预算按“发现”和“报告”分开：Stage 1 的结构化搜索与 Golden Lookalike 只读取结果列表，
Profile Report credit 为 0；正式报告增量等于运行前精确 cohort 中
`modash_report is not True` 的人数。完整新批为 `120 - 0 = 120`；当前 SKIN6 的 Stage 1/B3 cohort
均为 120，已有 4 份通过身份校验的 raw cache，故本批正式增量为 `120 - 4 = 116`。默认
`route_actionable` / `--modash-cap 20` 只是预算草稿模式，不是正式完整性证明。

Stage 4 成功后立即运行只读 B4；`--out` 必须是新的、不可覆盖的内部审计路径：

```bash
PYTHONPATH=. ../.venv/bin/python \
  -m extensions.sop_v2.pipeline.delivery_audit \
  --db "$DB" --batch-id "$BID" --stage1-artifact "$STAGE1_ARTIFACT" \
  --target-posts 10 --out "../data/runs/$BID/b4_barrier.formal-r2.json"
```

B4 将六项新鲜失败计数 `audit/pricing/translation/modash/storefront/sponsorship` 绑定到同一个
exact delivery-state SHA，并要求 Stage 1 cohort 指纹不变、全体 `decided`、零错误、零锁。
缺失计数、状态哈希漂移或任一非零都失败。B4 JSON 只留内部审计，不复制给客户。

只有 B4 `passed=true` 后才从同一 `decisions.json` 生成客户表和 HTML：

```bash
PYTHONPATH=. ../.venv/bin/python ../scripts/export_v2_xlsx.py \
  --decisions "../data/runs/$BID/decisions.json" \
  --out "../data/runs/$BID/deliverable.xlsx"
PYTHONPATH=. ../.venv/bin/python ../scripts/export_v2_html.py \
  --decisions "../data/runs/$BID/decisions.json" \
  --out "../data/runs/$BID/deliverable.html"
```

预算草稿模式会对每个缺报告字段的候选做两次只读路由投影：当前事实一次，只对缺失的
Modash-owned 字段填入合同内乐观值再一次。只有 `final_pool` 严格晋级才进入 shortlist；既有
非空观测不覆盖，评论、Storefront、实算 ER 等非 Modash blocker 也不会被假设修复。
`--modash-cap 20` 表示最多 20 份 Profile Report，不是必须抓满 20 份；实际可行动候选只有 4 个
就只消费最多 4 个 credit。这只能用于内部预算估算；正式模式固定使用 all-missing 与 cap 0。

这里的 `--strict-comment-translations` 是纯只读验收：它不会调用 LLM，不使用
`--translation-source-limit` 截断集合，也不会修改评论译文、派生语义、candidate 或 attempt
ledger。正式 Stage 4 不传任何 `--translation-provider/model/api-url/batch-size/source-limit`。
`--translate-comments` 只允许显式草稿/补译；与 round contract、`--strict-completeness` 或
`--full-deep-all-candidates` 同时使用会在 Modash 消耗和写盘前失败。只读验收不通过时，回到
Stage 3 或独立恢复流程补齐翻译并重新过 B3，禁止在正式 Stage 4 临场重翻。

Stage 3 每次先刷新当前主页，冻结最近 N 帖作为核心深采窗口；对每个成功打开的帖子，
只有明确 `comment_count=0` 才可直接以 `verified_zero` 完成评论核验，评论数为正或未知时必须
尝试加载评论。页面看起来为空不等于零：正数报告值只有满足上一节 DOM exact marker + comments
endpoint 双证据时才可作为 `verified_empty_thread` 完成，并继续保存原 `reported_count` 与完整
provenance；普通大于 2 条的空抽仍进入失败队列。
导航失败、互动指标缺失、评论核验失败分别写入 `deep_failed_posts`、
`deep_metric_missing_posts`、`comment_failed_posts`。报价则独立进入 Reels Tab，
采集最近 10 条非置顶 Reels，不覆盖主页核心窗口。

新采评论原文默认最多保存 120 条；本机 Ollama 将本轮保存范围内的全部评论统一翻译成
简体中文，再按评论身份去重重算购买意图、有效评论与低质比例。译文不会替换原文。正式 Stage 4 必须使用
`--strict-comment-translations` 对上述已存快照做只读验收；任一翻译失败、历史原文缺失或
行/摘要/语义不一致都会在付费 Modash 补数、写库和生成交付前阻断。XLSX 的“评论证据”表逐行导出全部 `comment_translations`；历史候选缺少
翻译结构时回退全部 `comment_records`，导出器不会再按意图等级截成 6 条。

旧批记录如果没有新版显式 contract 字段，只能按严格 legacy-evidence 边界复用：
至少 10 个结构化 `sampled_posts`、`comments_analyzed > 0`、`valid_comments >= 20`、
`real_er` 非空，并且存在可观察互动指标。达不到任一条件就补采，不能因“历史已跑过”
而跳过正式门禁。

旧的快速串行入口仅用于开发诊断：
```bash
PYTHONPATH=. ../.venv/bin/python -m extensions.sop_v2.pipeline.run_pipeline --batch-id $BID --track paid --resume
```

它没有 `graph_runner` 的 B1/B2 断言、账号/Profile lease、事件链、自动波次与
cooldown/no-progress，也不能代替上面的正式命令；正式交付不得使用它作为完成证明，也不得临时把固定 DAG 改为“Stage 2
全跑完再首次启动 Stage 3”的全串行流程。事故降级串行必须另开并明确标记为诊断运行。

## 3. 看板 / 断点续跑 / 排错
```bash
PYTHONPATH=scripts .venv/bin/python -m extensions.sop_v2.creator_cache stats      # 各 status 计数 + 品牌/橱窗/金种子
```
- graph runner 中途挂 → 用完全相同的参数追加 `--resume`；先确认旧进程已退出，租约仍有效时
  等 TTL，不删除 lease 强抢。候选陈旧锁按既定规则恢复，schedule/event/合同漂移会阻断恢复。
- 登录墙/挑战/超时 → 当前候选 `mark_error`、status 不动；本轮不会立即拿下一账号重试同一候选，需后续重跑。
- 候选明确不合格（品牌/私密/宽粉丝范围/非赛道等）→ `rejected + reject_reason`。
- Amazon、LTK、ShopMy、明确自营店和已识别的购物聚合入口都算 Storefront；
  `confirmed_no` 仍继续深采，`unknown` 才在决策阶段 Review。看到
  `no_amazon_storefront` 早淘汰属于历史规则漂移，正式批次应停止并检查版本。
- `graph_runner` 已提供跨进程 Account/Profile bundle lease、candidate heartbeat/CAS 和波次
  cooldown/no-progress；但尚无按单账号健康度驱动的持久 cooldown 与连续错误自动停用。
  直接运行 worker 会绕过这些编排保护，只能用于历史复现/诊断。

### 3.1 已确认的数据事故：离线证据恢复

只有在已经确认当前库与事故前备份包含同一目标窗口的互补深采证据时，才使用
`recover_deep_evidence`。它不是常规续跑手段。先停止全部 graph/worker，确认批次零活动锁，
并为当前库再做一份只读备份。默认命令只做 dry-run；Handle 必须显式列出：

```bash
# 从仓库根目录运行
BID=SKIN-YYYYMMDD

PYTHONPATH=.:scripts .venv/bin/python \
  -m extensions.sop_v2.pipeline.recover_deep_evidence \
  --db data/creator_cache.db \
  --backup-db data/creator_cache.db.pre-recovery.bak \
  --batch-id "$BID" \
  --handle HANDLE_A --handle HANDLE_B \
  --audit-output "data/runs/$BID/deep_evidence_recovery.dry-run.json"
```

人工核对每行 before/after、窗口 identity、写入列和输出中的 `plan_sha256` 后，才允许原样重建
计划并 apply：

```bash
PLAN_SHA=<copy-from-reviewed-dry-run>

PYTHONPATH=.:scripts .venv/bin/python \
  -m extensions.sop_v2.pipeline.recover_deep_evidence \
  --db data/creator_cache.db \
  --backup-db data/creator_cache.db.pre-recovery.bak \
  --batch-id "$BID" \
  --handle HANDLE_A --handle HANDLE_B \
  --apply --expected-plan-sha256 "$PLAN_SHA" \
  --audit-output "data/runs/$BID/deep_evidence_recovery.applied.json"
```

apply 会重验配置 SHA、计划 SHA、精确行 CAS、`qualified` 状态和零活动锁，然后在一个
`BEGIN IMMEDIATE` 事务中先校验全体目标、再写入、再复核受保护列；任一漂移整批回滚。
恢复仍只允许完整 ordered same-window 的 media identity 合并，跨窗口只保留较优 whole
attempt。apply 后先做数据库完整性与只读严格审计，再启动新的 graph schedule；若 worker
代码已经改变，不能 resume 旧 schedule/event 链。

## 4. Modash 与人工补数

Routing 对第三方核心字段缺失设置固定 Review：

- `--modash-cdp` 或 `--modash-csv`：补 `fake_pct / creator_country / top_audience_country` 等；
- 默认 `route_actionable` / `--modash-cap 20` 是预算草稿 shortlist，只买可能改变路由的报告，
  不能作为正式字段完整性证明；
- 正式交付固定使用
  `--modash-all-missing --modash-cap 0 --strict-enrichment-completeness`。已有
  `modash_report=true` 的报告不重买，报告内源字段为空显示“Modash无”；
- `--manual-csv`：可补 Raw Skin、VO、实际报价/实际 CPM、SHEIN/Temu 等人工事实；
- 当前 F 经济性模块整体延期为 N/A，Raw Skin/VO/报价不再是所有候选的固定 Review 条件；
- 内部草稿不补 Modash 核心字段时诚实落 Review，不伪造数据；正式交付则由 enrichment 门禁
  要求全部候选报告存在。

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

协作帖链接若携带超长 access token，不得按长度截断或猜 shortcode。只有媒体页声明的
Instagram HTTPS canonical 与原链接媒体类型一致、canonical code 为原 token 的精确值或前缀，
并由登录态同源 media-info `200` 响应的 `code` 再次回证身份，才允许使用该别名。通过后必须
把播放值/状态/来源、IG/总/FB 审计值、赞评数、置顶及来源、发布时间、隐藏计数标记和
`media_identity_provenance` 作为一个报价 bundle 整体更新；不能只补播放量或混用旧行字段，
身份冲突、非 Instagram canonical 或未回证都保持缺失。

运行后逐候选检查 `pricing_estimate`：

- `complete`：原生样本 `10/10`；
- `complete_available`：账号严格证明只有 `1–9/10` 条可用 Reels，按全部可用样本报价；
- `not_applicable_no_reels`：严格证明没有 Reels，报价为 `null`，不能填 0；
- `partial`：原生样本 `1–9/10`，但总体证据未闭合，只能暂估且 B3 阻断；
- `missing`：原生样本缺失，也无法证明账号没有 Reels，报价留空。

短总体逐 Reel 必须具备 media-info pin list 与响应 code 身份回证；已确认置顶的 Reel
直接排除且无需播放量，只有已确认非置顶的 Reel 才必须具备原生 `ig_play_count`。
总体闭合可来自健康 Reels Tab 到底连续两轮无增长；0 条也可使用独立
`reels_surface_absent`：两次精确 `/reels/` 请求都回同账号健康主页，主页有 `/p/` 内容，
但无 exact Reels Tab link/Reel link 且无 loading/login/challenge/private。两次导航必须各自
保存并验证当前页 `/p/` identity/计数/哈希，不能复用上一页 DOM。后一种不得展示成“Tab
穷尽”。Modash/总播放/Facebook 数据禁止 fallback；历史 `fallback_modash` 只能作为旧
数据透明展示并阻断 B3。以上状态必须原样进入交付，不能把部分样本或缺失写成完整原生窗口。
media-info provenance 必须与当前行 `code/url` 精确绑定；alias 另须 HTTPS Instagram、同媒体
类型、original/canonical 前缀与 requested 闭合，禁止跨媒体复制 provenance。

报价回填完成时必须经 pricing-only ledger finalizer：追加 full attempt event，单调选择 pricing
owner/pointer/quality，再由同一 lock-token CAS 保存。更差或无效的新证据只留审计事件，不覆盖
旧报价；deep canonical 与 comment retry state 不推进。B3 还会重派生 canonical estimate，核对
所有客户展示/计算字段，不能只看 `status/sample_count`。
`pricing_estimate.quote_usd.default/min/max` 是展示估算，不是实际报价；不得人工复制
到 `paid_cpm`，也不会影响 Gate、A-F、固定 Review 或五池。若客户或代理之后返回实际
USD 报价，应作为独立人工证据保存，再另算实际 Paid CPM。

## 5. 交付物

- `data/runs/<BID>/decisions.json`：五池决策（run_v2 结构，可复现）。
- `data/runs/<BID>/deliverable.xlsx`：批次总览、全量结构化评论证据、展示型预估报价和五池客户交付表。
- `data/runs/<BID>/deliverable.html`：自包含客户评审页；原因/标签可空，可选作用范围并导出 JSON。
- `data/evidence/<BID>/<handle>/`：候选证据目录；当前稳定证据以评论原话、用户名和帖子 URL 为主。
> `data/` 运行内容全部 gitignore，不进仓库（含真实候选、证据和数据库）。

发布前把同一基线的三个文件写入
`reports/deliveries/<BID>/formal-<YYYYMMDD>-r<N>/` 并保存 SHA-256。发给客户后该目录只读：
修复必须递增 `rN`，不得覆盖已交付版本；正式 r1 发现问题后保留原目录并生成 r2，禁止把
新文件复制回 r1。B3/B4、Storefront plan 和第三方 raw cache 都是内部审计工件，不放入客户
三件套。当前审计基线
`SKIN4-20260723/formal-20260729-r2` 为 157 人（89 carryover + 68 new）、
`retry_pending_count=0`，定价在当时合同下为 156 个 `complete` + 1 个历史
`fallback_modash`（按当前合同属于 B3 未闭合）；
`formal-20260728-r1` 保持冻结。

正式发布按以下顺序执行，任何一步失败都不得创建可交付版本：

1. 对冻结 cohort 运行严格 deep audit，取得新鲜 `audit` 失败数；
2. 运行 pricing-only `--audit-only`，取得新鲜 `pricing` 失败数；
3. 在 Stage 4 外只读复核全部已存评论翻译覆盖，取得新鲜 `translation` 失败数；
4. 把三项计数传给 `barriers.evaluate_b3`，并对同一 Stage 1 工件核验总数、Handle 指纹、状态、
   错误和锁；缺失计数也视为失败；
5. 将通过结果写入新的本地 `data/runs/<BID>/b3_barrier*.json` 并保存 SHA-256；
6. 对 Storefront unknown 运行不可覆盖的 collect-plan，人工复核后以 plan SHA 和全批 CAS
   原子 apply；随后用 all-missing/cap 0 补齐全部 Modash 报告；
7. strict Stage 4 生成内部决策基线并推进全体候选到 `decided`；
8. 运行 `delivery_audit` 生成新的内部 B4 工件：exact delivery-state SHA 必须与当前库一致，
   `audit/pricing/translation/modash/storefront/sponsorship` 六项失败为 0，且全体 `decided`；
9. B4 通过后才核对 JSON 候选唯一且五池互斥、生成 XLSX/HTML、核对评论证据行数和 schema 2
   导出；再把客户三件套和 `SHA256SUMS` 写入新的 formal revision。不要复制 B3/B4 工件。

B3 之后如果代码、配置、轮次合同、enrichment 或导出逻辑有任何改变，原审计工件不能自动
沿用；应对新状态重新生成 Stage 4/B4 与客户产物，并递增 `rN`，不得覆盖旧交付。

## 6. 客户反馈回流（飞轮）

客户反馈必须带原交付基线安全回导：

```bash
PYTHONPATH=scripts .venv/bin/python \
  -m extensions.sop_v2.pipeline.ingest_client_decisions \
  --file /absolute/path/client_decisions_<OLD_BID>.json \
  --source-decisions reports/deliveries/<OLD_BID>/formal-<YYYYMMDD>-r<N>/decisions.json \
  --dry-run
```

去掉 `--dry-run` 才会在单事务中写客户状态和 SHA ledger。`round_contract` 为 `new_only`
时不生成 carryover、不重新采集历史账号；只有 `unresolved/retry_only` 才使用
`prepare_next_round`。精确补采、人工 Lookalike 回导和组合 Stage 4 命令见仓库根
[`README.md`](../../README.md)“客户反馈回流与下一轮准备”。不要改写旧账号的
`discovery_batch`，也不要用 `--all-batches` 代替 carryover 精确组合。正式交付必须保留
`audit_collect --strict --full-deep-all-candidates --target-posts 10`、Stage 3
`--strict-completeness`，以及 Stage 4
`--strict-completeness --full-deep-all-candidates --deep-target-posts 10` 三层完整性门禁。

Carryover 的 Stage 4 复跑必须幂等：`needs_pipeline_retry=true` 的历史
`collected/rejected` 可继续；已经是 `decided` 的记录只有在 `_stage_error` 为空且
`strict_deep_reasons` 通过时才可复用。相同 manifest 再跑后若
`retry_pending_count` 不是 0，停止发布并回到精确补采。

新导入还会在同一事务写 `client_feedback_events`；空拒绝原因合法，但不会产生策略信号。
历史已有 ledger 的文件要补逐条事件时，显式增加 `--backfill-events`，先 dry-run，再执行。
分析、策略提案和历史回放命令见
[`FEEDBACK_GOVERNANCE.md`](FEEDBACK_GOVERNANCE.md)。客户 JSON 只能提出 `policy_signal`，
不能直接修改配置或确认全局硬规则。
