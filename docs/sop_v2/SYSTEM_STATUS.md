# 系统状态

更新日期：2026-07-28

## 当前结论

SOP V2 四阶段主链已经形成并在本地数据库中产生过真实阶段结果：

```text
Stage 1 Modash Discover
  → Stage 2 Browser Qualify
  → Stage 3 Browser Collect
  → Stage 4 Offline Decide/Export
```

它适合单机、串行、人工看护的小批次运行；账号健康、并发租约、规则一致性和完整证据合同
尚未达到无人值守生产标准。

## 已实现

- Modash 结构化搜索、分页和 seed 写库；
- `creator_cache.db` 五态状态机、软锁、stage_json 和客户状态；
- Playwright Profile 浅扫、品牌/私密/赛道/通用电商 Storefront 派生；
- 独立深采池、帖子/评论 DOM 抽取、购买意图和真实 ER；
- 非置顶 Reels 播放证据与展示型预估报价合同；
- Modash CSV/CDP 与人工 CSV 合并；
- Hard Gates、A-F、N/A 归一化、固定 Review 和五池路由；
- `decisions.json`、V2 XLSX 和可选 HTML；
- 客户 approved/rejected/pending 回流和 Tier 2 晋升；
- 采集完整性审计和不完整候选重排队。

## 当前账号与采集边界

- V2 使用静态浏览器账号文件，不使用 Legacy `AccountPool`；
- Stage 2 默认每号 8 个候选；Stage 3 默认每号 3 个候选；
- 每账号块创建 Sticky 代理和 persistent Chrome Context；
- error 后关闭 Context，但本轮不会换号重试同一候选；
- 持久 cooldown、账号健康调度、Account Lease 和统一预检尚未实现；
- 主链只注入 `sessionid + ds_user_id`，完整 Cookie/UA 实现未接入。

详见 [`../ACCOUNT_POOL_ARCHITECTURE.md`](../ACCOUNT_POOL_ARCHITECTURE.md)。

## 当前规则边界

已冻结：

1. Storefront 是通用电商入口，不是 Amazon-only 白名单；Amazon、LTK、ShopMy、自营店和
   已识别购物聚合入口都算有。`confirmed_no` 继续深采并可进入 Without-Storefront，
   `unknown` 才 Review；
2. 展示型预估报价按“先排置顶，再取最近 10 条非置顶 Reels 均播 × CPM
   $35（区间 $35–$40）/1000”计算；播放证据通过登录态浏览器会话读取 Instagram
   同源 media info，均播只使用 IG 原生 `ig_play_count`，总 `play_count` 和
   `fb_play_count` 仅审计、不得抬价；
3. 展示估价不是实际报价，不写 `paid_cpm`，不进入 Gate、F 分、固定 Review 或路由；
4. 报价/Raw Skin/VO 已从固定 Review 移除，当前 F 模块整体 N/A。

仍需收口：

1. 实算 ER 的配置、注释、历史测试和 Gate 实现仍有旧口径残留；
2. 配置包含低分 Exclude，当前路由低分仍全部进入 Review；
3. 评论采样参数、语言匹配和部分阈值仍有硬编码；
4. 历史结转候选可能缺原生 Reels 播放/置顶证据，必须如实标
   `partial/fallback_modash/missing`；
5. `FieldEvidence`、Batch Manifest 和 ScoreItem 证据尚未贯通最终输出。

## 测试状态

以下是 2026-07-22 的历史只读审计快照，不代表 2026-07-28 当前工作树结果：

```text
python -m unittest discover -s tests -v
Ran 38 tests
29 passed / 9 failed
```

当时失败集中在规则漂移：

- 8 个 Gate 边界测试仍传旧 `real_er`，而实现已优先使用 `real_er_median`；
- 1 个五池测试仍编码了“缺实际报价锁定 Review”的已废弃预期，而当前配置已移除该条件。

当前提交前必须重新运行完整测试，不能沿用此历史数字或在交付文档中据此声称“全部通过”。

## 高优先级待办

1. 用边界测试持续锁定 Storefront 三态，以及展示估价与实际 CPM 的隔离；
2. 修复 `run_pipeline` 的 Track、阶段退出码、补数参数和输出路径；
3. 修复 Stage 3 登录墙/partial output 判定；
4. 引入浏览器账号健康状态、持久 cooldown、Account/Proxy Lease 和同候选有界重试；
5. 将候选软锁改为带 owner/token/expiry 的原子 Lease；
6. 贯通 Evidence、Manifest、脱敏和交付前扫描；
7. 同步测试、配置、代码、README、Runbook 和最终交付规范；
8. 将 Legacy 入口迁入明确命名空间或归档。

## 文档事实源

- 总览：[`../../README.md`](../../README.md)
- 当前架构：[`../ARCHITECTURE.md`](../ARCHITECTURE.md)
- 账号专项：[`../ACCOUNT_POOL_ARCHITECTURE.md`](../ACCOUNT_POOL_ARCHITECTURE.md)
- 状态机：[`PIPELINE_SPEC.md`](PIPELINE_SPEC.md)
- 运行手册：[`RUNBOOK.md`](RUNBOOK.md)
- 规则配置：[`../../config/sop_v2.toml`](../../config/sop_v2.toml)
