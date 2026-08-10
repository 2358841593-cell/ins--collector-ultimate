# 本地数据目录

本目录只提交结构说明和空目录占位。无论远端仓库可见性如何，真实运行数据和凭据都不进入 Git。

| 路径 | 用途 | 是否提交运行内容 |
|---|---|---|
| `runs/` | 单次发现、扫描缓存、截图和中间结果 | 否 |
| `session/` | instagrapi 暖 Session、轮换和冷登录状态 | 否，且必须保留在本机 |
| `modash/` | Modash 可选补数记录 | 否 |
| `source/` | 客户名单、种子和批次输入 | 否 |
| `batches/` | SOP V2 批次状态与 manifest | 否 |
| `manual_evidence/` | Raw Skin、VO、报价等人工回填 | 否 |
| `discovery.db` | 可重建的 SQLite 发现数据库 | 否 |
| `token_costs.db` / `token_cost_status.json` | LLM token 成本台账与监控快照（不含提示词/回复） | 否 |
| `pod_accounts.json` | 本地累计的水军/互赞团派生库 | 否 |

首次运行时脚本会按需创建数据库和运行文件。当前 V2 不使用 `data/session/` 的 Legacy
暖 Session；该目录仍可能保存旧链路状态，历史复现时不要随意删除。
