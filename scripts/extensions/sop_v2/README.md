# SOP V2 实现占位

后续模块按 `docs/sop_v2/REQUIREMENTS_CHECKLIST.md` 的编号命名或在模块头部注明对应编号。建议优先建设：批次状态模型、证据引用、人工回填合并、客户交付字段适配和离线验收器。

任何新逻辑都应接受现有 discovery JSON/SQLite 数据作为输入，并在缺少 Modash 或人工证据时返回明确的“待核验”状态，而不是中断主管道。
