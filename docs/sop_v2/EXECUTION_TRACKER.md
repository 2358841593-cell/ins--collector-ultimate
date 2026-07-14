# SOP V2 最终执行清单（开发追踪表）

版本：1.2 ｜ 日期：2026-07-14 ｜ 分支：`sop-v2-dev`
（v1.1 = 新增 B0 浏览器采集后端 track；v1.2 = 客户拍板 instagrapi 完全退役、浏览器唯一 IG 通道、Modash 复用已登录 Chrome；R0 全面改为登录态 profile 池）
**这是后续开发、修复与测试的唯一执行入口。** 每项任务的完整设计、阈值口径与验收标准见 [`DEV_EXECUTION_CHECKLIST.md`](DEV_EXECUTION_CHECKLIST.md)（v1.3.1，经三轮多视角对抗校验，共修订 67 处），此表只列"做什么、在哪做、什么算完成"。

执行环境（本机）：
- 仓库：`/Users/wiselq/Desktop/ins-collector`，分支 `sop-v2-dev`，边修复边测试。
- Modash：**复用本机已登录的 Chrome（yibo profile，CDP 9222）现有会话**做只读自动化，**不新建独立进程、不需 Claude 插件**（Playwright connectOverCDP 直连，只碰 Modash 标签）；成本纪律见 `DEV_EXECUTION_CHECKLIST.md` §1.4/§2.5 modash_budget。已实测 `scripts/dev/modash_cdp_read.py` 读到 discovery 结构化结果（handle/followers/ER）。
- Instagram 采集：**浏览器唯一通道，instagrapi 私有 API 退役**（§1.6）。账号由负责人提供后，为每个账号起独立登录态 Chrome profile（`start_instagram_cdp.zsh` 模式），人工首登一次、长期挂机复用。不再用 `import_pool.py` 导私有 API 账号池。
- Python 本机 3.14.6（操作机初跑为 3.13.9）——E0-1 装完后跑依赖冒烟（instagrapi 虽退役但 requirements 暂留，清理见 R0-6）。

状态图例：`[ ]` 未开始 ｜ `[~]` 进行中 ｜ `[x]` 完成 ｜ `[!]` 阻塞（注明原因）
纪律：每完成一项 → 勾选此表 → 同步 `REQUIREMENTS_CHECKLIST.md` 对应 ID 状态 → 单独 commit（信息里带任务 ID）。

## 已完成里程碑（2026-07-14 实施，真机验证，37 测试全绿）

- **认证难题彻底绕开**：cookie 注入建登录态 Chrome profile，5/5 账号 LOGGED_IN，零密码登录零烧号（`pool_health.py`）。初跑 22/22 私有 API 死因不复存在。
- **浏览器采集通道通了**：`browser_collect.py fetch_profile` 用登录态浏览器会话（web_profile_info，非 instagrapi）读真实创作者全字段。
- **Modash 无需插件**：`modash_cdp_read.py`/`modash_search.py` 通过 CDP 直连 yibo Chrome，只读抽 discovery 结构化结果，护肤搜索实测返回真实候选。
- **P0 决策引擎全建成**：config(20组) + contracts + gates(边界) + scoring(N/A归一/9.5封顶) + routing(五池互斥) + run_v2(可复现) + export(8 sheet) + redaction。
- **实数据端到端**：Modash 护肤 handle → 采集 → 决策 → 五池 XLSX，正确按 SOP 硬门槛分流，全程可追溯。

---

## E0 本机环境就绪（先行，半天内）

- [x] **E0-1** venv + requirements 装好；playwright chromium 在位；Chrome 已装；账号 5/5 解析（含 sessionid）
- [x] **B0-POC** cookie→登录态 profile 端到端验证（`scripts/dev/cookie_profile_smoke.py`）：index 0 账号 sessionid 注入 → LOGGED_IN、读到自己 profile 的 og:description 结构化字段。**浏览器唯一通道策略实测成立，零密码登录零烧号**。备注：og:description 中英文 locale 需 B0-3 处理
- [~] **E0-2** IG 登录态 profile 建立：账号已到（5 个含 live sessionid）。**改进：用 cookie 注入建 profile（B0-POC 已验证），比人工首登更快、零密码登录**；index 0 已建，其余 4 个待批量建（R0-3）
- [ ] **E0-3** 代理出口确认：本机是否有 Clash/固定出口；登录 profile 与采集尽量同出口（浏览器通道下已非阻断项，属存活性优化）
- [~] **E0-4** Modash 通道冒烟：**CDP 直连 yibo Chrome 已验证**（`modash_cdp_read.py` 读到 discovery 结果，零成本只读）。待补：读用量页记录五桶余额基线（2026-07-14 核对为 Profiles 1310/1500、Emails&Exports 929/1000、Monitoring 298/400、Fans 6000/6000、linked 5/6）作 canary 起点——需开用量页，下次连带做
- [ ] **E0-5** 操作机功能同步决策（**需负责人拍板**）：独立 run 日志、`reports/run-audits/`、`config/sop_v2.toml`、`config/modash_cost_policy.toml` 只在操作机存在——能拿到文件则拷贝回推（走 R0-6），拿不到则在本仓库按 v1.5 口径重建（工作量小，且 sop_v2.toml 本来就要按 §2.5 重写）。注：`--warm-only` 是私有 API 旗标，instagrapi 退役后作废、不回推

## B0 浏览器采集后端（IG 采集通道从私有 API 改为登录态浏览器；详见 §1.6/§2.6）

> 决策（客户拍板）：Modash-first 降低 IG 请求量后，**IG 采集走登录态 Chrome（CDP）唯一通道，instagrapi 私有 API 完全退役**（不作快通道保留，冷登录烧号太狠）。参考实现 `browser-cdp-lab`。此 track 与 R0 紧耦合。

- [~] **B0-1** 采集后端接口抽象：`scripts/browser_collect.py` 起 `BrowserCollector` 雏形（fetch_profile 已实现）；正式 `Collector` 协议（三方法）待抽出
- [x] **B0-2** `BrowserCollector.fetch_profile`：登录态浏览器会话调 web_profile_info（web app 同款端点，非 instagrapi），og:description 中英 locale 回退。**实测 @annascountryhome 18,525 粉丝/类目/bio/external_url 全部同构拿到**，零烧号
- [ ] **B0-3** `BrowserCollector` 近帖采集：滚动读帖网格 + 逐帖 caption/like/comment/media_type/play_count/置顶标记（补齐 --v2-collect 的 30 帖窗口）
- [ ] **B0-4** `BrowserCollector` 评论采集：开帖展开滚动读评论（Top/Recent 采样口径对齐；量小可接受慢）
- [ ] **B0-5** `discover.py` 采集层切到 `Collector` 接口（Stage1 发现改由 Modash Handle 池注入 + Stage2 回扫走 BrowserCollector）；stage3-6 零改动验证（产出结构与 golden 参照同构测试）
- [ ] **B0-6** CDP 启动器增强：吸收 lab 的多实例管理 + `.run/pids` 追踪 + ready 检查进 `start_instagram_cdp.zsh`；节奏拟人化（随机停顿、限速、单 profile 低并发）
- [ ] **B0-7** 反爬健壮性：DOM 选择器容错 + 版面变更告警 + 失败转 Review（不误判 Exclude）；登录态失效检测（跳登录页即停该 profile）

## R0 认证与采集基础（与 P0 并行；详见 v1.3.1 §3 R0 表）

> **重塑注记**：instagrapi 退役后 R0 全部对象为**登录态 Chrome profile**（非私有 API session）；冷登录/TOTP/烧号防护从主路径移除（代码留仓标 deprecated）。健康池门槛为"≥N 个登录态 Chrome profile"（起步 N=1 挂机跑即可）。以下按此重写。

- [x] **R0-1** `scripts/pool_health.py` Chrome profile 健康分诊（未登录/存活/失效/challenge；仅打开 IG 首页判断登录态、不批量请求；报告零凭证）
- [ ] **R0-2** 代理/IP 一致性（profile 出口指纹记录 + 预检；**统一 README 等文档 `--no-proxy` 示例口径**）——存活性优化，非阻断
- [~] **R0-3** 登录态 profile 建立作业（`start_instagram_cdp.zsh` 起独立 profile 人工首登，永不删 profile、不脚本化密码登录）；目标 **≥N 个登录态 profile**（起步 N=1 挂机跑）+ 一页 playbook
- [ ] **R0-4** 候选池持久化 + `--resume` 断点续跑 + 失败重试队列（耗尽→Review，COLLECT-02）+ `tests/test_resume.py`
- [ ] **R0-5** 登录态保活巡检（跳登录页/challenge 的 profile 立即移出、标待重登；参数入 config）
- [ ] **R0-6** 版本/文档统一（视 E0-5 回推或重建；修订 ARCHITECTURE.md/README：Modash-first + 浏览器唯一 IG 采集 + instagrapi 退役 + 删过时账号池示例）
- [ ] **R0-7** 开批预检门（健康 profile 数/登录态新鲜度/代理/config SHA/目录契约，零 IG 数据请求）
- [ ] **R0-8** run 审计跨 run 关联（batch 归组，为 P2-1 打底）

## P0 决策可复现（纯离线，零 IG 请求；13 项）

- [x] **P0-1** `config/sop_v2.toml` 全量口径（§2.5 全表 22 组含 discovery/modash_budget/CONFLICT 标注）+ `modash_cost_policy.toml`
- [x] **P0-2** `extensions/sop_v2/contracts.py`（FieldEvidence/GateResult/ScoreItem/BatchManifest）
- [ ] **P0-3** `extensions/sop_v2/merge.py` 多源合并（merge_priority 驱动、冲突不覆盖）
- [~] **P0-4** Modash 三通道适配：`search_pool_import.py`（主发现，13 字段契约）/ `lookup_log.py`（Profile 补数+预算台账）/ `import_modash_export.py`（仅 shortlist）
- [x] **P0-5** `gates.py` 硬门槛引擎（GATE-01..12 + 可采集性；全部半开区间边界用例）
- [x] **P0-6** `scoring.py`（A-F、N/A 归一化、AI Score、9.5 封顶；golden fixtures）
- [x] **P0-7** `routing.py`（固定 Review 七项优先 → Lifestyle 封顶 → 分层 → 五池互斥）
- [x] **P0-8** `run_v2.py` 编排（同输入逐字节可复现）
- [x] **P0-9** `export_v2_xlsx.py` 五池 8 sheet（Herman 空列；缺失不填 0）
- [ ] **P0-10** `discover.py --v2-collect` 七点旗标（双闸旁路/track 放宽/30 帖/置顶/评论采样/旧线停用/`--handle-pool`）；不带旗标逐字节回归
- [~] **P0-11** 测试套件 + 合成 fixtures（可用初跑证据包脱敏结构）
- [ ] **P0-12** 离线回归：初跑 scan cache 跑 run_v2 新旧对照
- [x] **P0-13** 脱敏扫描 + 无外发断言（`tests/test_redaction.py` + 交付前钩子）

## P1 证据与人工节点（5 项）

- [ ] **P1-1** `import_manual_evidence.py`（Raw Skin/VO/风险/报价模板；Lifestyle 提升同通道）
- [ ] **P1-2** Storefront 活跃度 + LTK 人工穿透工作流
- [ ] **P1-3** CPM 计算（非置顶近 10 Reels 均播；Gifting F1=N/A；35/40/40.01 断言）
- [ ] **P1-4** Modash Profile 补数执行流（yibo Chrome 只读；30 天缓存优先；预算触线即停）
- [ ] **P1-5** 互动集中度异常检测（≥5 帖且 ≥70%→Review）

## 首批验收 Runbook（R0 预检门 + P0 + P1 完成后；9 步）

- [ ] **RB-0** 预检门通过（健康登录态 Chrome profile 数 ≥N，见 R0-3；登录态新鲜；代理一致；config SHA 锁定）
- [ ] **RB-1** 建批：显式选 track；Handle 池目标 100-300 → Include 1-10 不凑数
- [ ] **RB-2** 发现（Modash 单通道）：a) Modash 模板搜索（新动作先 canary，结果页只读 → search_pool_import）+ 客户回流/人工 handle 注入；b) `discover.py --v2-collect --resume`（BrowserCollector 登录态 profile 回扫 Handle 池，中断可续跑）
- [ ] **RB-3** verify_browser.py Storefront 三态核验
- [ ] **RB-4** Modash 补数（缓存→Profile 队列 ≤20/轮→shortlist 导出+字段映射确认）
- [ ] **RB-5** 人工证据录入（Raw Skin/VO/风险/报价）
- [ ] **RB-6** run_v2 → 五池 → XLSX + HTML + manifest → 脱敏扫描
- [ ] **RB-7** 人工抽查全部 Include + ≥20 条 Review/Exclude；同 raw 重跑一致性
- [ ] **RB-8** 交付 → Herman 回填回导 → CONFLICT 台账（9 条，见 v1.3.1 §5）请客户裁定 → 只把明确确认的规则写回 config

## P2 稳定运营（5 项）

- [ ] **P2-1** batch manifest + 证据索引落盘
- [ ] **P2-2** HTML 审计 V2 段（五池/gate 原值/A-F/CONFLICT/新旧对照）
- [ ] **P2-3** Herman 反馈回导 + 批准者回流 **Modash Lookalike 扩池并注入 Handle 池**（非 IG Lookalike，已退役）+ 负向标签
- [ ] **P2-4** 质量看板（批准率/五池比例/缺失率/淘汰率/误收误杀代理率/预算消耗/池健康）
- [ ] **P2-5** `MODASH_OPERATIONS.md` 操作规范（docx §12 七步 + 成本纪律 + 白名单）

## 建议执行顺序（并行轨道）

```
轨道一(采集/运维): E0-2/3 → B0-1..7(浏览器采集,登录态 Chrome) 与 R0-1/2/3/5(按 B0 重塑) → R0-4/7/8
轨道二(离线规则):  E0-1/5 → P0-1/2 → P0-3/4/5/6/7(可并行) → P0-8/9 → P0-11/12/13
轨道三(采集改造):  P0-10(依赖 P0-1 的 config + B0-5 的后端接口; 在线验证依赖轨道一)
汇合:             P1 全部 → RB-0..8 首批 → P2
```

> 决策已定（客户拍板）：**instagrapi 完全退役**，B0 浏览器为唯一 IG 采集路径；R0 的冷登录/TOTP/烧号防护从主路径移除（代码留仓标 deprecated，requirements 清理并入 R0-6）。

## 外部待办（阻塞标记）

| 项 | 谁 | 阻塞什么 |
|---|---|---|
| 提供 IG 账号（用于登录态 Chrome profile 首登） | 负责人 | E0-2 → R0-3 → 一切在线测试 |
| 操作机文件是否可拷贝（E0-5 拍板） | 负责人 | R0-6 走回推还是重建 |
| 本机代理出口口径（有无 Clash） | 负责人/运维 | E0-3 → R0-2/3（存活性优化，非阻断） |
| 新 Search/AI/Lookalike/Save 首次 canary | 开发（RB-2 前执行） | Modash 模板批量执行 |
| Raw Skin/VO 人工核验执行人 | 客户/负责人 | P1-1 证据供给 |
| Paid 实际报价来源 | 客户 | F 模块（缺则固定 Review，不阻塞交付） |
| CONFLICT 台账 9 条裁定 | 客户（随首批交付） | 下一版 config |
| 阶段 9"触达/合作结果记录"是否本期范围 | 客户（随首批交付） | P2-3 回导模板列（暂按范围外） |
