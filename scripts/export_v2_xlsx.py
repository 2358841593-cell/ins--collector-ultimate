#!/usr/bin/env python3
"""五池 XLSX 业务交付导出（P0-9 / DEL-01..08）。

对齐客户 SOP §8 字段 + §13 原值/标准化/采集时间；采用可点真实链接（IG 主页 / Bio /
Amazon 橱窗）+ 嵌入截图的证据 sheet（参考客户 sample.xlsx 呈现风格）。
五个互斥决策池 + Batch Summary + 证据截图 + 说明。缺失显示"缺失"不填 0；Herman 两列空。
"""
from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from pathlib import Path

POOLS = ["Include-With-Storefront", "Include-Without-Storefront",
         "Priority-Review", "Review", "Exclude"]
MISSING = "缺失"
NICHE_LABEL = {"skincare": "Skincare", "beauty_device": "Beauty Device",
               "beauty_wellness": "Beauty/Wellness", "lifestyle": "Lifestyle", "other": "Other"}

# 数据列（纯文本，对齐 SOP §8 + §13）
DATA_COLS = [
    ("Country", "country"),
    ("Handle (IG)", "handle_at"),
    ("Full Name", "full_name"),
    ("Followers", "follower_count"),
    ("Creator Niche", "niche_label"),
    ("Fake / ER (%)", "fake_er"),
    ("Amazon Storefront", "storefront_state"),
    ("Sponsorship Saturation", "sponsorship_cell"),
    ("Has VO & Raw Skin", "vo_rawskin"),
    ("Elite Brand History", "elite_brand"),
    ("AI Vetting Score (1-10)", "ai_reason"),
    ("High-Intent Comments", "high_intent_snips"),
    ("Review Reason", "review_reason"),
    ("Exclude Reason", "exclude_reason"),
    ("Missing Data", "missing_data"),
    ("Discovery Source", "discovery_source"),
    ("Normalized Total", "normalized_total"),
    ("Score A-F", "score_af"),
    ("Captured At", "captured_at"),
    ("Herman Approval", "_blank"),
    ("Herman's Feedback", "_blank"),
]
# 链接列（可点）：标题 → (取链接的函数 key, 显示文案)
LINK_COLS = [
    ("主页 (IG)", "ig", "打开 ↗"),
    ("Bio 链接", "bio", "打开 ↗"),
    ("Amazon 橱窗链接", "amazon", "打开 ↗"),
    ("证据截图", "shot", "查看 →"),
]


def _link_url(cand, kind):
    if kind == "ig":
        return cand.get("profile_url") or f"https://instagram.com/{cand.get('handle','')}"
    if kind == "bio":
        ext = cand.get("external_url")
        links = cand.get("bio_links") or []
        return ext or (links[0] if links else None)
    if kind == "amazon":
        return cand.get("amazon_storefront_link") if cand.get("storefront_status") == "confirmed_yes" else None
    if kind == "shot":
        return (cand.get("storefront_evidence") or {}).get("screenshot")
    return None


def _cell(cand, key):
    if key == "_blank":
        return ""
    if key == "country":
        return cand.get("creator_country") or MISSING
    if key == "handle_at":
        h = cand.get("handle") or ""
        return h if h.startswith("@") else f"@{h}"
    if key == "niche_label":
        return NICHE_LABEL.get(cand.get("core_niche_key"), cand.get("core_niche_key") or MISSING)
    if key == "fake_er":
        fake = f"{cand['fake_pct']}% Fake" if cand.get("fake_pct") is not None else "— Fake"
        er = f"{cand['general_er']}% ER" if cand.get("general_er") is not None else "— ER"
        return f"{fake} / {er}"
    if key == "storefront_state":
        return {"confirmed_yes": "✓ 有橱窗", "confirmed_no": "确认无",
                "unknown": "未确认"}.get(cand.get("storefront_status"), "未确认")
    if key == "sponsorship_cell":
        v = cand.get("sponsorship_saturation")
        return f"{v}%" if v is not None else MISSING
    if key == "vo_rawskin":
        vo = {True: "Yes", False: "No"}.get(cand.get("has_vo"), "Pending")
        return f"VO: {vo} / Raw Skin: {cand.get('raw_skin_grade') or 'Pending'}"
    if key == "elite_brand":
        h = cand.get("elite_brand_hits")
        return f"命中 {h} 个" if h else ("无" if h == 0 else MISSING)
    if key == "ai_reason":
        ai = cand.get("ai_vetting_score")
        return f"{ai} — {cand.get('decision_summary','')}" if ai is not None else cand.get("decision_summary", "")
    if key == "high_intent_snips":
        snips = cand.get("high_intent_snippets") or []
        return " / ".join(snips[:2]) if snips else ("—" if cand.get("comments_analyzed") else "评论待采集")
    if key == "review_reason":
        return "；".join(cand.get("review_reasons_text") or []) or ""
    if key == "exclude_reason":
        return "；".join(cand.get("exclude_reasons_text") or []) or ""
    if key == "missing_data":
        return "; ".join(cand.get("missing_data") or []) or ""
    if key == "score_af":
        sm = cand.get("score_by_module") or {}
        return " ".join(f"{m}:{sm[m]['earned']}/{sm[m]['available']}" if sm.get(m, {}).get("available")
                        else f"{m}:N/A" for m in "ABCDEF" if m in sm)
    if key == "captured_at":
        return cand.get("captured_at") or ""
    v = cand.get(key)
    return MISSING if v is None else v


def build_workbook(decisions):
    from openpyxl import Workbook
    from openpyxl.drawing.image import Image as XLImage
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    LINK_FONT = Font(color="1F6E6A", underline="single", size=10)
    HEAD_FILL = PatternFill("solid", fgColor="0E6E62")
    HEAD_FONT = Font(bold=True, color="FFFFFF", size=10)
    cands = decisions.get("candidates", [])

    wb = Workbook()

    # ── 证据截图 sheet（先建，拿到每个 handle 的锚点行）──
    wse = wb.active
    wse.title = "证据截图"
    wse.cell(1, 1, "Amazon / 电商橱窗 浏览器核验截图 · 证据存档").font = Font(bold=True, size=12)
    wse.cell(2, 1, "由 Playwright 真实 Chrome 抓取；点候选表『证据截图』列跳到对应截图，点链接看实时页").font = Font(size=9, color="5E6672")
    wse.column_dimensions["A"].width = 42
    anchor = {}
    er = 4
    for c in cands:
        ev = c.get("storefront_evidence") or {}
        shot = ev.get("screenshot")
        if not (shot and os.path.exists(shot)):
            continue
        h = c.get("handle")
        anchor[h] = er
        hc = wse.cell(er, 1, f"@{h}")
        hc.font = Font(bold=True, size=10)
        src = ev.get("source_url")
        if src:
            sc = wse.cell(er, 2, "打开实时页 ↗")
            sc.hyperlink = src
            sc.font = LINK_FONT
        try:
            img = XLImage(shot)
            # 缩略到宽约 360px 保持比例
            if img.width and img.height:
                ratio = 360 / img.width
                img.width = 360
                img.height = int(img.height * ratio)
            wse.add_image(img, f"A{er + 1}")
        except Exception:  # noqa: BLE001
            pass
        er += 22  # 每张截图留高度

    # ── Batch Summary ──
    ws = wb.create_sheet("Batch Summary", 0)
    meta = decisions.get("manifest", {})
    pools_count = Counter(c.get("final_pool", "Review") for c in cands)
    exc_reasons = Counter()
    for c in cands:
        for r in (c.get("exclude_reasons_text") or []):
            exc_reasons[r] += 1
    ers = [c["general_er"] for c in cands if c.get("general_er") is not None]
    if ers:
        over2 = sum(1 for e in ers if e > 2.0)
        er_finding = (f"本批 {len(ers)} 个有 Modash ER：区间 {min(ers):.2f}%–{max(ers):.2f}%，"
                      f"其中 >2%(硬门槛)仅 {over2} 个（{over2 * 100 // len(ers)}%）。"
                      f"红光/LED 护肤设备赛道互动率普遍偏低，建议客户校准该赛道 ER 门槛。")
    else:
        er_finding = "本批无 Modash ER 值（待补数）。"
    sf_confirmed = [c["handle"] for c in cands if c.get("storefront_status") == "confirmed_yes"]
    rows = [
        ("批次报告", "Instagram 红人筛选 · SOP V2 交付"),
        ("Batch ID", meta.get("batch_id", "")),
        ("SOP 版本", meta.get("sop_version", "")),
        ("Campaign Track", meta.get("campaign_track", "")),
        ("生成时间", decisions.get("generated_at", "")),
        ("", ""),
        ("候选总数", len(cands)),
        ("— Include (With Storefront)", pools_count.get("Include-With-Storefront", 0)),
        ("— Include (Without Storefront)", pools_count.get("Include-Without-Storefront", 0)),
        ("— Priority Review", pools_count.get("Priority-Review", 0)),
        ("— Review", pools_count.get("Review", 0)),
        ("— Exclude", pools_count.get("Exclude", 0)),
        ("", ""),
        ("确认有 Amazon 橱窗", f"{len(sf_confirmed)} 个：{', '.join('@' + h for h in sf_confirmed)}" or "0"),
        ("主要淘汰原因", "；".join(f"{k}×{v}" for k, v in exc_reasons.most_common(4)) or "—"),
        ("★ 校准发现（ER 分布）", er_finding),
        ("", ""),
        ("阅读说明", "五个 sheet 为互斥决策池，同一候选只出现一次。缺失显示「缺失」，不等于 0。"),
        ("", "链接列（主页/Bio/Amazon 橱窗）可直接点击核验；证据截图列跳到浏览器核验现场截图。"),
        ("", "AI Vetting Score 1-10；分层用 Normalized Total。Herman 两列供客户回填。"),
        ("Modash 补数说明", "本批未逐个开 Modash Profile（受众/假粉/国家/语言待补），相关候选按 SOP 固定进 Review。"),
    ]
    for r, (k, v) in enumerate(rows, 1):
        ws.cell(r, 1, k).font = Font(bold=True)
        ws.cell(r, 2, v).alignment = Alignment(wrap_text=True, vertical="top")
    ws.column_dimensions["A"].width = 30
    ws.column_dimensions["B"].width = 88

    # ── 五池 sheet ──
    by_pool = {p: [] for p in POOLS}
    for c in cands:
        by_pool.setdefault(c.get("final_pool", "Review"), []).append(c)
    titles = [t for t, _ in DATA_COLS] + [t for t, _, _ in LINK_COLS]

    for pool in POOLS:
        ws = wb.create_sheet(pool[:31])
        for col, t in enumerate(titles, 1):
            cell = ws.cell(1, col, t)
            cell.font = HEAD_FONT
            cell.fill = HEAD_FILL
        for r, cand in enumerate(by_pool.get(pool, []), 2):
            for col, (_, key) in enumerate(DATA_COLS, 1):
                ws.cell(r, col, _cell(cand, key))
            base = len(DATA_COLS)
            for j, (_, kind, disp) in enumerate(LINK_COLS, 1):
                col = base + j
                if kind == "shot":
                    a = anchor.get(cand.get("handle"))
                    if a:
                        cc = ws.cell(r, col, disp)
                        cc.hyperlink = f"#证据截图!A{a}"
                        cc.font = LINK_FONT
                    else:
                        ws.cell(r, col, "—")
                    continue
                url = _link_url(cand, kind)
                if url:
                    cc = ws.cell(r, col, disp)
                    cc.hyperlink = url
                    cc.font = LINK_FONT
                else:
                    ws.cell(r, col, "—")
        ws.freeze_panes = "C2"
        if by_pool.get(pool):
            ws.auto_filter.ref = f"A1:{get_column_letter(len(titles))}{len(by_pool[pool]) + 1}"
        ws.column_dimensions["B"].width = 22
        ws.column_dimensions["C"].width = 24
        ws.column_dimensions["K"].width = 34

    # ── 说明 sheet ──
    ws = wb.create_sheet("说明")
    ws.cell(1, 1, "交付说明 · 字段与规则").font = Font(bold=True, size=12)
    dd = [
        ("五池", "Include-With/Without-Storefront / Priority-Review / Review / Exclude，互斥，一人一池"),
        ("链接列", "主页/Bio/Amazon 橱窗均为可点真实链接；证据截图跳到浏览器核验现场截图"),
        ("Amazon Storefront", "浏览器穿透聚合页读真实出链确认；有/无均可 Include，未知进 Review"),
        ("Fake / ER (%)", "Modash 原值；ER 硬门槛 >2%，假粉硬门槛 <25%"),
        ("AI Vetting Score", "1-10，附一句话理由；分层用 Normalized Total（≥75 Include / 65-75 Priority / 50-65 Review / <50 Exclude）"),
        ("缺失 / N/A", "缺失=未采集/未补数，不等于 0；N/A=子项不适用，从评分分母移除"),
        ("固定 Review", "缺 Modash 核心/Storefront 未知/评论不足/Raw Skin·VO 未核验/缺报价——高分不覆盖"),
        ("Herman 两列", "供客户审批回填；批准者回流下一轮 Modash Lookalike"),
    ]
    for r, (k, v) in enumerate(dd, 2):
        ws.cell(r, 1, k).font = Font(bold=True)
        ws.cell(r, 2, v).alignment = Alignment(wrap_text=True, vertical="top")
    ws.column_dimensions["A"].width = 20
    ws.column_dimensions["B"].width = 92
    return wb


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--decisions", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    decisions = json.loads(Path(args.decisions).read_text())
    wb = build_workbook(decisions)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    wb.save(args.out)
    print(f"业务交付 XLSX → {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
