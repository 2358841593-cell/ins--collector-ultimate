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

# 交付列，严格对齐客户 SOP §8 最终交付字段 + §13 原始/标准化值与采集时间
COLUMNS = [
    ("Country", "country"),
    ("Handle (IG)", "handle_at"),
    ("Followers", "follower_count"),
    ("Creator Niche", "niche_label"),
    ("Fake / ER (%)", "fake_er"),
    ("Amazon Storefront Link", "storefront_cell"),
    ("Sponsorship Saturation", "sponsorship_cell"),
    ("Has VO & Raw Skin", "vo_rawskin"),
    ("Elite Brand History", "elite_brand"),
    ("AI Vetting Score (1-10)", "ai_reason"),
    ("High-Intent Comment Snippets", "high_intent_snips"),
    ("Herman Approval", "_herman_approval"),
    ("Herman's Feedback", "_herman_feedback"),
    ("Review Reason", "review_reason"),
    ("Exclude Reason", "exclude_reason"),
    ("Missing Data", "missing_data"),
    ("Evidence Link", "evidence_link"),
    ("Discovery Source", "discovery_source"),
    ("Normalized Total", "normalized_total"),
    ("Score A-F", "score_af"),
    ("Captured At", "captured_at"),
]

MISSING = "缺失"

NICHE_LABEL = {"skincare": "Skincare", "beauty_device": "Beauty Device",
               "beauty_wellness": "Beauty/Wellness", "lifestyle": "Lifestyle", "other": "Other"}


def _fmt_pct(v):
    return f"{v}%" if v is not None else MISSING


def _cell(cand: dict, key: str):
    if key in ("_herman_approval", "_herman_feedback"):
        return ""  # 空列供客户回填
    if key == "country":
        return cand.get("creator_country") or MISSING
    if key == "handle_at":
        h = cand.get("handle") or ""
        return f"@{h}" if h and not h.startswith("@") else h
    if key == "niche_label":
        return NICHE_LABEL.get(cand.get("core_niche_key"), cand.get("core_niche_key") or MISSING)
    if key == "fake_er":
        fake = f"{cand['fake_pct']}% Fake" if cand.get("fake_pct") is not None else "— Fake"
        er = f"{cand['general_er']}% ER" if cand.get("general_er") is not None else "— ER"
        return f"{fake} / {er}"
    if key == "storefront_cell":
        st = cand.get("storefront_status")
        link = cand.get("amazon_storefront_link")
        if st == "confirmed_yes":
            return link or "有（链接待补）"
        if st == "confirmed_no":
            return "确认无 Storefront"
        return "未确认（待穿透/人工）"
    if key == "sponsorship_cell":
        return _fmt_pct(cand.get("sponsorship_saturation"))
    if key == "vo_rawskin":
        vo = {True: "Yes", False: "No"}.get(cand.get("has_vo"), "Pending")
        rs = cand.get("raw_skin_grade") or "Pending"
        return f"VO: {vo} / Raw Skin: {rs}"
    if key == "elite_brand":
        h = cand.get("elite_brand_hits")
        return f"命中 {h} 个" if h else ("无" if h == 0 else MISSING)
    if key == "ai_reason":
        ai = cand.get("ai_vetting_score")
        return f"{ai} — {cand.get('decision_summary', '')}" if ai is not None else cand.get("decision_summary", "")
    if key == "high_intent_snips":
        snips = cand.get("high_intent_snippets") or []
        return " / ".join(snips[:2]) if snips else ("—" if cand.get("comments_analyzed") else "评论待采集")
    if key == "review_reason":
        return "；".join(cand.get("review_reasons_text") or []) or ""
    if key == "exclude_reason":
        return "；".join(cand.get("exclude_reasons_text") or []) or ""
    if key == "missing_data":
        return "; ".join(cand.get("missing_data") or []) or ""
    if key == "evidence_link":
        import os
        parts = []
        if cand.get("profile_url"):
            parts.append(cand["profile_url"])
        ev = cand.get("storefront_evidence") or {}
        if ev.get("source_url"):
            parts.append(f"Storefront 核验: {ev['source_url']}")
        if ev.get("screenshot"):
            parts.append(f"截图: evidence/{os.path.basename(ev['screenshot'])}")  # 相对引用，不暴露本机路径
        return " | ".join(parts) or MISSING
    if key == "score_af":
        sm = cand.get("score_by_module") or {}
        return " ".join(f"{m}:{sm[m]['earned']}/{sm[m]['available']}" if sm.get(m, {}).get("available")
                        else f"{m}:N/A" for m in "ABCDEF" if m in sm)
    if key == "captured_at":
        return cand.get("captured_at") or ""
    v = cand.get(key)
    return MISSING if v is None else v


def build_workbook(decisions: dict):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    wb = Workbook()
    # Batch Summary
    ws = wb.active
    ws.title = "Batch Summary"
    from collections import Counter
    meta = decisions.get("manifest", {})
    cands = decisions.get("candidates", [])
    pools_count = {p: 0 for p in POOLS}
    for c in cands:
        pools_count[c.get("final_pool", "Review")] = pools_count.get(c.get("final_pool", "Review"), 0) + 1
    exc_reasons = Counter()
    for c in cands:
        for r in (c.get("exclude_reasons_text") or []):
            exc_reasons[r] += 1
    missing_n = sum(1 for c in cands if c.get("missing_data"))
    top_exc = "；".join(f"{k}×{v}" for k, v in exc_reasons.most_common(4)) or "—"
    # ER 分布发现（Modash 值）：赛道互动率校准依据
    ers = [c["general_er"] for c in cands if c.get("general_er") is not None]
    if ers:
        over2 = sum(1 for e in ers if e > 2.0)
        er_finding = (f"本批 {len(ers)} 个有 Modash ER：区间 {min(ers):.2f}%–{max(ers):.2f}%，"
                      f"其中 >2%(硬门槛)仅 {over2} 个（{over2*100//len(ers)}%）。"
                      f"红光/LED 护肤设备赛道互动率普遍偏低，建议客户校准该赛道 ER 门槛或接受较低产出。")
    else:
        er_finding = "本批无 Modash ER 值（待补数）。"

    rows = [
        ("批次报告", "Instagram 红人筛选 · SOP V2 交付"),
        ("Batch ID", meta.get("batch_id", "")),
        ("SOP 版本", meta.get("sop_version", "")),
        ("Campaign Track", meta.get("campaign_track", "")),
        ("生成时间", decisions.get("generated_at", "")),
        ("config SHA-256", (meta.get("config_sha256", "") or "")[:16] + "…"),
        ("", ""),
        ("候选总数", len(cands)),
        ("— Include (With Storefront)", pools_count.get("Include-With-Storefront", 0)),
        ("— Include (Without Storefront)", pools_count.get("Include-Without-Storefront", 0)),
        ("— Priority Review", pools_count.get("Priority-Review", 0)),
        ("— Review", pools_count.get("Review", 0)),
        ("— Exclude", pools_count.get("Exclude", 0)),
        ("", ""),
        ("有待补数据(Missing)候选", missing_n),
        ("主要淘汰原因", top_exc),
        ("★ 校准发现（ER 分布）", er_finding),
        ("", ""),
        ("阅读说明", "五个 sheet 为互斥决策池，同一候选只出现一次。缺失字段显示「缺失」，不等于 0。"),
        ("", "AI Vetting Score 1-10；分层用 Normalized Total（≥75 Include / 65-75 Priority / 50-65 Review / <50 Exclude）。"),
        ("", "Herman Approval / Herman's Feedback 为空列，供客户回填。"),
        ("Modash 补数说明", "本批未逐个开 Modash Profile（受众/假粉/国家/语言待补），相关候选按 SOP 固定进 Review。"),
    ]
    for r, (k, v) in enumerate(rows, 1):
        ws.cell(r, 1, k).font = Font(bold=True)
        ws.cell(r, 2, v)
    ws.column_dimensions["A"].width = 30
    ws.column_dimensions["B"].width = 82

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
