#!/usr/bin/env python3
"""五池 XLSX 导出（P0-9，DEL-01..08）。

输入 v2-decisions JSON（run_v2 产出），生成客户交付 XLSX：
Batch Summary + 五个互斥决策池 + Evidence Index + Data Dictionary。
缺失值显示"缺失"绝不填 0；Herman Approval/Feedback 为可编辑空列。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

POOLS = ["Include-With-Storefront", "Include-Without-Storefront",
         "Priority-Review", "Review", "Exclude"]

# 共用列（决策+反馈 / 身份 / 硬门槛 / 评分分解 一部分）
COLUMNS = [
    ("Final Pool", "final_pool"),
    ("AI Vetting Score", "ai_vetting_score"),
    ("Normalized Total", "normalized_total"),
    ("Country", "creator_country"),
    ("Handle (IG)", "handle_at"),
    ("Full Name", "full_name"),
    ("Followers", "follower_count"),
    ("Campaign Track", "campaign_track"),
    ("Creator Niche", "core_niche_key"),
    ("Storefront Status", "storefront_status"),
    ("Amazon Storefront Link", "amazon_storefront_link"),
    ("Fake %", "fake_pct"),
    ("Modash ER %", "general_er"),
    ("Sponsorship Saturation", "sponsorship_saturation"),
    ("A Content", "score_A"),
    ("B Prof&Visual", "score_B"),
    ("C Community", "score_C"),
    ("D Commercial", "score_D"),
    ("E Audience", "score_E"),
    ("F Economics", "score_F"),
    ("Review Reason", "review_reason"),
    ("Exclude Reason", "exclude_reason"),
    ("Missing Data", "missing_data"),
    ("Discovery Source", "discovery_source"),
    ("Profile URL", "profile_url"),
    ("Herman Approval", "_herman_approval"),
    ("Herman's Feedback", "_herman_feedback"),
]

MISSING = "缺失"


def _cell(cand: dict, key: str):
    if key in ("_herman_approval", "_herman_feedback"):
        return ""  # 空列供客户回填
    if key == "handle_at":
        h = cand.get("handle") or ""
        return f"@{h}" if h and not h.startswith("@") else h
    if key == "review_reason":
        return "; ".join(cand.get("review_reasons") or []) or ""
    if key == "exclude_reason":
        return "; ".join(cand.get("exclude_reasons") or []) or ""
    if key == "missing_data":
        return "; ".join(cand.get("missing_data") or []) or ""
    if key.startswith("score_"):
        mod = key.split("_")[1]
        sd = (cand.get("score_by_module") or {}).get(mod)
        if sd is None:
            return MISSING
        return f"{sd['earned']}/{sd['available']}" if sd.get("available") else "N/A"
    v = cand.get(key)
    if v is None:
        return MISSING
    return v


def build_workbook(decisions: dict):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    wb = Workbook()
    # Batch Summary
    ws = wb.active
    ws.title = "Batch Summary"
    meta = decisions.get("manifest", {})
    pools_count = {p: 0 for p in POOLS}
    for c in decisions.get("candidates", []):
        pools_count[c.get("final_pool", "Review")] = pools_count.get(c.get("final_pool", "Review"), 0) + 1
    rows = [
        ("Batch ID", meta.get("batch_id", "")),
        ("SOP 版本", meta.get("sop_version", "")),
        ("Campaign Track", meta.get("campaign_track", "")),
        ("config SHA-256", meta.get("config_sha256", "")),
        ("候选总数", len(decisions.get("candidates", []))),
        ("生成时间", decisions.get("generated_at", "")),
    ]
    for p in POOLS:
        rows.append((p, pools_count.get(p, 0)))
    for r, (k, v) in enumerate(rows, 1):
        ws.cell(r, 1, k).font = Font(bold=True)
        ws.cell(r, 2, v)
    ws.column_dimensions["A"].width = 26
    ws.column_dimensions["B"].width = 50

    header_fill = PatternFill("solid", fgColor="2563EB")
    by_pool = {p: [] for p in POOLS}
    for c in decisions.get("candidates", []):
        by_pool.setdefault(c.get("final_pool", "Review"), []).append(c)

    for pool in POOLS:
        ws = wb.create_sheet(pool[:31])
        for col, (title, _) in enumerate(COLUMNS, 1):
            cell = ws.cell(1, col, title)
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = header_fill
        for r, cand in enumerate(by_pool.get(pool, []), 2):
            for col, (_, key) in enumerate(COLUMNS, 1):
                ws.cell(r, col, _cell(cand, key))
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions

    # Evidence Index（雏形）
    ws = wb.create_sheet("Evidence Index")
    for col, h in enumerate(["Handle", "Field/Gate", "Type", "Source URL", "Captured At", "Result"], 1):
        ws.cell(1, col, h).font = Font(bold=True)
    r = 2
    for c in decisions.get("candidates", []):
        for ev in c.get("evidence", []):
            ws.cell(r, 1, c.get("handle"))
            ws.cell(r, 2, ev.get("field", ""))
            ws.cell(r, 3, ev.get("type", ""))
            ws.cell(r, 4, ev.get("source_url", ""))
            ws.cell(r, 5, ev.get("captured_at", ""))
            ws.cell(r, 6, ev.get("result", ""))
            r += 1

    # Data Dictionary
    ws = wb.create_sheet("Data Dictionary")
    dd = [
        ("Final Pool", "五个互斥决策池之一"),
        ("Normalized Total", "earned/applicable*100，N/A 从分母移除"),
        ("AI Vetting Score", "round(normalized/10,1)，仅展示；分层判定用 Normalized Total"),
        ("缺失", "该字段未采集/未补数，不等于 0"),
        ("N/A", "该子项不适用，从评分分母移除，不送分不扣分"),
        ("Storefront Status", "confirmed_yes/confirmed_no 均可 Include；unknown → Review"),
        ("固定 Review", "Modash 核心缺失/Storefront 未知/评论<20/Raw Skin或VO 未核验/Paid 缺报价/受众<35%/语言不匹配 —— 高分不覆盖"),
    ]
    for col, h in enumerate(["字段/概念", "含义"], 1):
        ws.cell(1, col, h).font = Font(bold=True)
    for r, (k, v) in enumerate(dd, 2):
        ws.cell(r, 1, k)
        ws.cell(r, 2, v)
    ws.column_dimensions["A"].width = 22
    ws.column_dimensions["B"].width = 80
    return wb


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--decisions", required=True, help="v2-decisions JSON")
    ap.add_argument("--out", required=True, help="输出 XLSX 路径")
    args = ap.parse_args()
    decisions = json.loads(Path(args.decisions).read_text())
    wb = build_workbook(decisions)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    wb.save(args.out)
    print(f"五池 XLSX → {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
