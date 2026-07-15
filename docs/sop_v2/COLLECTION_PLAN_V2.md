# 采集与证据方案 V2（IG 反爬现实下的可靠架构）

日期：2026-07-15 ｜ 触发：首批交付字段大面积缺失 + IG 端点全限流实测

## 0. 实测到的硬约束（方案的前提）

IG 2026 对程序化取数激进封锁，实测确认：
- `graphql/query`（instaloader 取帖子/评论/媒体的分页）→ **403 直接封**
- `web_profile_info`（JSON profile+12帖）→ 快速请求即 **429 限流**
- **账号一旦被限流，连浏览器渲染页的动态内容（帖子网格）都不返回**，只剩静态 og meta
- 结论：**任何"快"的批量取数都会触发限流**。可靠 = 慢速拟人 + 账号轮换 + 尊重冷却。

## 1. 路线决策：纯 browser-use + 慢速拟人（不用会被封的 API 端点）

放弃 `web_profile_info` / `graphql` 这类 API 端点作为主路径（它们是被封/限流的根源）。
改为**纯浏览器渲染页读取**（人能看到的就能拿），配合：
- **每账号 1 请求 / 30–60 秒**（拟人节奏，随机抖动）
- **多账号轮换**（新账号池），单账号日请求量设上限
- **命中限流/challenge 立即移出、标冷却**，不硬打
- 例外：`profile` 字段用 **instaloader 的 Profile 端点**（实测带对 csrftoken 等头，比渲染页解析稳；仍按拟人节奏调用）

速度预期（诚实）：**30 个候选 ≈ 1–2 小时**，不是 10 分钟。要更快只能加住宅代理（路线 B，另议）。

## 2. 字段 → 数据源映射（客户已确认受众/假粉走 Modash）

| 字段 | 来源 | 方式 |
|---|---|---|
| 粉丝/全名/bio/认证/类目/外链 | IG | instaloader Profile（拟人节奏） |
| 近帖 caption/赞/评论数/是否视频/play_count/置顶 | IG | 渲染帖子页 DOM（逐帖，拟人） |
| 赞助占比/导购占比/成分设备词/organic/Reels·Static ER | 派生 | 从近帖算 |
| Storefront 有无 + Amazon 链接 | 浏览器穿透 | 渲染聚合页读出链 |
| **评论正文/购买意图/bot/低质** | IG | **渲染帖子页评论 DOM**（本次必须做对，见 §4） |
| **假粉% / 受众国家 / 受众语言 / 年龄性别 / 合作史** | **Modash** | **客户认可其原业务流程即用 Modash**；走 yibo Chrome CDP，预算受控（§5） |
| Raw Skin 等级 / VO 有无 | 人工 | 基于流程中**下载的媒体**做视觉/听觉判定（§4） |
| 实际报价 / CPM | 人工+派生 | 报价人工要；曝光=非置顶近10 Reels play_count；CPM=报价÷曝光×1000 |

→ 这样 Include 能真正填满：硬门槛(IG+Modash) + 核心字段完整(Modash补数) + 评分证据齐。

## 3. 截图审计嵌入采集流程（客户明确要求：边采边截，不回头补）

**原则**：截图在采集每一步**当场产生并落盘到证据库**，报告生成时**从证据库取**，不做"抓完再回头补截图"。

### 证据库目录结构
```
data/evidence/<batch_id>/<handle>/
    profile.png            # 打开 profile 页当场截
    post_01.png … post_NN  # 每个被核验/采样的帖子当场截
    storefront.png         # Storefront 穿透页当场截
    modash_profile.png     # Modash 补数页当场截（受众/假粉现场）
    media/                 # 下载的近帖图片/视频（供 Raw Skin/VO 判定）
    evidence.json          # 索引：每张截图的 type / source_url / captured_at / 关联字段
```

### 流程钩子（采集器每步都调）
1. 打开 profile 页 → `screenshot(profile.png)` + 记 evidence.json
2. 采样每帖打开 → `screenshot(post_i.png)` + 下载媒体到 media/
3. Storefront 穿透 → `screenshot(storefront.png)`
4. Modash 补数页 → `screenshot(modash_profile.png)`
5. 每张截图即时写入 `evidence.json`：`{type, handle, field, source_url, path, captured_at}`

### 报告/XLSX 生成时
- `export_v2_xlsx` 从 `data/evidence/<batch>/<handle>/evidence.json` 取截图路径，嵌入"证据截图" sheet，候选行内链跳转。
- 不再在导出时临时找截图——证据在采集时就已结构化存好。

## 4. 本次必须做对的两块

### 评论（客户要求必须补）
- 渲染帖子页 `/p/{code}/`，等评论区渲染，**精确定位评论正文 span**（排除用户名链接、`<time>`、赞/回复按钮），逐条取 `{username, text}`。
- headful 逆向一次选择器，验证取到的是正文而非用户名/时间。
- 采样：互动最高 10 帖 + 最近 10 帖，每帖 ≤10 条（拟人逐帖，慢）。
- 分析：三档购买意图 + bot/pod + 低质占比（`comments.py` 已就绪）。

### Raw Skin / VO
- 流程中**下载近帖媒体**（图片 + 视频关键帧/音轨）到 `media/`。
- Raw Skin：对图片/视频帧做视觉判定（真实皮肤纹理 A/B/C）。
- VO：视频音轨检测人声旁白（有/无）。
- 判定结果 + 证据媒体路径写回候选,进 B 模块评分。

## 5. Modash 补数（客户认可，预算受控）

- 走 yibo Chrome CDP（已验证可驱动），对**通过 IG 硬门槛的高潜候选**逐个开 Profile。
- 每开一个 Profile **当场截图**存 `modash_profile.png`（受众国家/假粉/语言现场证据）。
- 预算纪律：每轮 ≤20、账期 ≤110、先查 30 天缓存（`modash_cost_policy.toml`）。
- 补：假粉% / Creator·Top Audience Country / 目标国占比 / Top Language / 合作史（SHEIN/Temu、Elite 品牌）。

## 6. 需要你的三样

1. **一套新账号**（现 5 个已限流，需冷却数小时；新账号按 §1 拟人节奏跑，不会重蹈覆辙）。
2. **确认路线**：接受"慢但可靠"（1–2 小时/30 个），还是要配住宅代理提速（路线 B）。
3. **Raw Skin/VO 判定**：视觉判定我用视觉模型先做初判，最终是否要人工复核由你定。

## 7. 我会交付的采集器（等新账号 + 路线确认后建）
- `scripts/browser_collect_v2.py`：纯渲染页 + 拟人节奏 + 账号轮换 + **截图嵌入流程** + 媒体下载
- 评论 DOM 提取修正（headful 逆向选择器）
- Modash 补数 + 现场截图
- `export_v2_xlsx` 改为从证据库取截图
