#!/usr/bin/env python3
"""五池 XLSX 业务交付导出（专业可读版）。

对齐客户 SOP §8 字段；以"购买意向评论证据"为重点（客户核心诉求）；可点真实链接
（IG 主页 / 电商橱窗）；评论区截图嵌入证据 sheet 并内链跳转。
风格：色标验收建议 + 隔行底纹 + 合理行高列宽 + 冻结窗格 + 自动筛选，专业可读。
"""
from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
from export_v2_comment_status import comment_collection_complete
from extensions.sop_v2 import storefront as storefront_mod
POOLS = ["Include-With-Storefront", "Include-Without-Storefront",
         "Priority-Review", "Review", "Exclude"]
MISSING = "缺失"
NICHE_LABEL = {"skincare": "护肤", "beauty_device": "美容仪器", "beauty_wellness": "美妆/健康",
               "lifestyle": "生活方式", "other": "其他"}
POOL_ZH = {"Include-With-Storefront": "纳入·有橱窗", "Include-Without-Storefront": "纳入·无橱窗",
           "Priority-Review": "优先复核", "Review": "待复核", "Exclude": "已排除"}

# 配色（克制、专业）
C_HEAD = "1F4E4A"       # 深墨绿表头
C_STRIPE = "F4F7F6"     # 隔行浅底
C_LINK = "1A6E64"
POOL_FILL = {"Include-With-Storefront": "DCEEE4", "Include-Without-Storefront": "E4F0EA",
             "Priority-Review": "FBF0D9", "Review": "FBF4E6", "Exclude": "F7E7E5"}
POOL_FONT = {"Include-With-Storefront": "1E7A47", "Include-Without-Storefront": "1E7A47",
             "Priority-Review": "9A6A1E", "Review": "9A6A1E", "Exclude": "AE3B37"}

# 列定义：(标题, key, 宽, 是否wrap)
COLS = [
    ("验收建议", "verdict", 12, False),
    ("Handle", "handle_at", 20, False),
    ("全名", "full_name", 22, False),
    ("粉丝", "follower_count", 9, False),
    ("赛道", "niche", 11, False),
    ("购买意向评论（原话）", "intent_snippet", 40, True),
    ("评论证据", "comment_ev", 10, False),
    ("电商橱窗 / 购物入口", "storefront_link", 20, False),
    ("赞助占比", "sponsorship", 9, False),
    ("Fake%（Modash）", "fake", 12, False),
    ("ER 对照", "er_compare", 20, True),
    ("非置顶 Reels 样本", "pricing_sample", 13, False),
    ("近 10 条非置顶 Reels 均播", "pricing_avg", 17, False),
    ("预估报价 USD（CPM 35）", "pricing_quote", 17, False),
    ("预估上限 USD（CPM 40）", "pricing_quote_high", 17, False),
    ("报价状态 / 来源", "pricing_status", 26, True),
    ("AI 评分", "ai", 8, False),
    ("结论", "summary", 30, True),
    ("待补/原因", "reasons", 34, True),
    ("主页", "profile", 8, False),
    ("采集时间", "captured_at", 14, False),
    ("Herman Approval", "_blank", 15, False),
    ("Herman's Feedback", "_blank", 15, False),
]


def _verdict(pool):
    return POOL_ZH.get(pool, pool)


def _xlsx_safe(value):
    """Neutralize formula-like external text before writing it to XLSX cells."""
    if isinstance(value, str) and value.startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def _cell(c, key):
    if key == "_blank":
        return ""
    if key == "verdict":
        return _verdict(c.get("final_pool", "Review"))
    if key == "handle_at":
        h = c.get("handle") or ""
        return h if h.startswith("@") else f"@{h}"
    if key == "full_name":
        return c.get("full_name") or "—"
    if key == "niche":
        return NICHE_LABEL.get(c.get("core_niche_key"), c.get("core_niche_key") or "—")
    if key == "intent_snippet":
        snips = c.get("high_intent_snippets") or []
        deep_complete = comment_collection_complete(c)
        comment_unavailable_count = len(c.get("comment_unavailable_posts") or [])
        sampled_posts = [
            post for post in (c.get("sampled_posts") or [])
            if isinstance(post, dict)
        ]
        verified_zero = (
            deep_complete
            and bool(sampled_posts)
            and all(post.get("comment_count") == 0 for post in sampled_posts)
        )
        vc = c.get("valid_comments")
        valid_count = int(vc or 0)
        completion_note = ""
        if deep_complete and comment_unavailable_count:
            completion_note = (
                f"{comment_unavailable_count}帖低量评论重复不可见（已复采）"
            )
        elif verified_zero:
            completion_note = "未发现公开评论（已完成采集）"
        elif deep_complete and valid_count < 20:
            completion_note = (
                f"公开有效评论有限（{valid_count}条，已完成采集）"
            )
        elif not deep_complete and snips:
            completion_note = "评论采集未完成（待复采）"
        if snips:
            rows = [completion_note] if completion_note else []
            rows.extend(f"· {s}" for s in snips[:3])
            return "\n".join(rows)
        if completion_note:
            return completion_note
        # 四态区分：深采未跑 / 抽取失败(系统侧) / 样本偏少 / 真没意图——别把抽取失败甩锅成账号受限
        if not c.get("comments_read"):
            return "评论待采集（深采未完成）"
        if (c.get("comments_analyzed") or 0) == 0:
            return "评论抽取失败（待复采）"
        if (vc or 0) < 20:
            return f"样本偏少（{vc} 条，待补采）"
        return f"无明显购买意向（有效评论 {vc} 条）"
    if key == "comment_ev":
        return "帖子 ↗" if (c.get("intent_posts") or c.get("comment_shots")) else "—"
    if key == "storefront_link":
        st = storefront_mod.effective_status(c)
        if st == "confirmed_yes":
            return f"{storefront_mod.storefront_type(c) or '打开橱窗'} ↗"
        return {"confirmed_no": "确认无橱窗", "unknown": "未确认"}.get(st, "—")
    if key == "sponsorship":
        v = c.get("sponsorship_saturation")
        return f"{v}%" if v is not None else "—"
    if key == "fake":
        v = c.get("fake_pct")
        if v is not None:
            return f"{v}%"
        if c.get("modash_report"):
            return "数据源无"
        if c.get("final_pool") == "Exclude":
            return "未补（硬门槛已排除）"
        return "待补"
    if key == "er_compare":
        mo = c.get("modash_er")
        ig = c.get("ig_er")
        parts = []
        parts.append(f"Modash {mo}%" if mo is not None else "Modash —")
        parts.append(f"IG实算 {ig}%" if ig is not None else "IG 待读")
        return " / ".join(parts)
    if key.startswith("pricing_"):
        p = c.get("pricing_estimate") or {}
        status = p.get("status") or "missing"
        requested = int(p.get("requested_reels") or 10)
        sample = int(p.get("sample_count") or 0)
        if key == "pricing_sample":
            if status == "fallback_modash":
                return "第三方"
            return f"{sample}/{requested}" if sample else "待补"
        if key == "pricing_avg":
            return p.get("average_plays")
        quote = p.get("quote_usd") or {}
        if key == "pricing_quote":
            return quote.get("default")
        if key == "pricing_quote_high":
            return quote.get("max")
        if status == "complete":
            return f"完整 · IG 最近 {requested} 条非置顶 Reels"
        if status == "partial":
            return f"样本不足 · IG {sample}/{requested}（暂估）"
        if status == "fallback_modash":
            return "第三方 Reels 均播替代（未验证最近 10 条/置顶）"
        return "待补 Reels 播放量"
    if key == "ai":
        return c.get("ai_vetting_score") if c.get("ai_vetting_score") is not None else "—"
    if key == "summary":
        return c.get("decision_summary") or ""
    if key == "reasons":
        r = (c.get("review_reasons_text") or []) + (c.get("exclude_reasons_text") or [])
        return "；".join(r) or "—"
    if key == "profile":
        return "打开 ↗"
    if key == "captured_at":
        return c.get("captured_at") or ""
    v = c.get(key)
    return MISSING if v is None else v


def _url(c, key):
    if key == "profile":
        h = (c.get("handle") or "").lstrip("@")
        return c.get("profile_url") or f"https://www.instagram.com/{h}/"
    if key == "comment_ev":       # 评论证据 → 有意图评论的帖子链接（客户点开核验"谁说了什么"）
        ip = c.get("intent_posts") or []
        return ip[0].get("post_url") if ip else None
    if key == "storefront_link" and storefront_mod.has_storefront(c):
        return storefront_mod.storefront_url(c)
    if key == "pricing_sample":
        reels = (c.get("pricing_estimate") or {}).get("reels") or []
        return reels[0].get("url") if reels else None
    return None


def build_workbook(decisions):
    from openpyxl import Workbook
    from openpyxl.drawing.image import Image as XLImage
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    cands = decisions.get("candidates", [])
    head_font = Font(bold=True, color="FFFFFF", size=10.5, name="Microsoft YaHei")
    head_fill = PatternFill("solid", fgColor=C_HEAD)
    link_font = Font(color=C_LINK, underline="single", size=10)
    base_font = Font(size=10, name="Microsoft YaHei")
    thin = Side(style="thin", color="E0E4E2")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    wrap = Alignment(wrap_text=True, vertical="top")
    center = Alignment(horizontal="center", vertical="center")

    wb = Workbook()

    # ── 证据截图 sheet（评论区）──
    wse = wb.active
    wse.title = "评论证据"
    wse.cell(1, 1, "评论区截图 · 购买意向证据（浏览器真实抓取）").font = Font(bold=True, size=13, name="Microsoft YaHei")
    wse.cell(2, 1, "每个候选取近帖评论区截图；点候选表『评论证据』列跳到对应截图").font = Font(size=9, color="5E6672")
    wse.column_dimensions["A"].width = 46
    anchor = {}
    er = 4
    for c in cands:
        shots = c.get("comment_shots") or []
        shots = [s for s in shots if (ROOT / s).exists()]
        if not shots:
            continue
        h = c.get("handle")
        anchor[h] = er
        hc = wse.cell(er, 1, f"@{h} · {c.get('full_name') or ''}")
        hc.font = Font(bold=True, size=11, name="Microsoft YaHei")
        row = er + 1
        for sp in shots[:4]:
            try:
                img = XLImage(str(ROOT / sp))
                ratio = 300 / img.width if img.width else 1
                img.width = 300
                img.height = int((img.height or 400) * ratio)
                wse.add_image(img, f"A{row}")
                row += 16
            except Exception:  # noqa: BLE001
                pass
        er = row + 2

    # ── Batch Summary ──
    ws = wb.create_sheet("批次总览", 0)
    meta = decisions.get("manifest", {})
    pc = Counter(c.get("final_pool", "Review") for c in cands)
    sf = [c["handle"] for c in cands if storefront_mod.has_storefront(c)]
    price_status = Counter(
        (c.get("pricing_estimate") or {}).get("status", "missing") for c in cands
    )
    ws.cell(1, 1, "Instagram 红人筛选 · 交付总览").font = Font(bold=True, size=15, name="Microsoft YaHei")
    rows = [
        ("批次", meta.get("batch_id", "")),
        ("生成时间", decisions.get("generated_at", "")),
        ("Campaign Track", meta.get("campaign_track", "")),
        ("候选总数", len(cands)),
        ("纳入·有橱窗 / 无橱窗", f"{pc.get('Include-With-Storefront',0)} / {pc.get('Include-Without-Storefront',0)}"),
        ("优先复核 / 待复核 / 已排除", f"{pc.get('Priority-Review',0)} / {pc.get('Review',0)} / {pc.get('Exclude',0)}"),
        ("确认有电商橱窗/购物入口", f"{len(sf)} 个"),
        ("原生报价口径完整", f"{price_status.get('complete', 0)} 个"),
        ("报价样本不足 / 第三方替代 / 缺失",
         f"{price_status.get('partial', 0)} / {price_status.get('fallback_modash', 0)} / {price_status.get('missing', 0)}"),
        ("", ""),
        ("阅读说明", "五个决策池互斥，一人一池。『验收建议』色标区分；『购买意向评论』是核心，评论证据列可跳截图。"),
        ("链接", "Handle/主页/电商橱窗或购物入口均可点击核验。"),
        ("ER 口径", "Modash ER（近两月中位数，偏低）与 IG 实算 ER（近帖，部分藏赞）并列参考；本赛道 Modash ER 普遍<2%，硬门槛以可靠标准+购买意向评论为准。"),
        ("预估报价口径", "先排除置顶 Reels，再取最近 10 条平均播放量；默认 CPM $35，参考区间 $35–40。样本不足/第三方替代会明确标记；仅供预算参考，不是博主实际报价，也不参与评分或路由。"),
        ("Herman 两列", "供客户审批回填。"),
    ]
    for r, (k, v) in enumerate(rows, 3):
        ws.cell(r, 1, k).font = Font(bold=True, size=10.5, name="Microsoft YaHei")
        cell = ws.cell(r, 2, v)
        cell.alignment = wrap
        cell.font = base_font
    ws.column_dimensions["A"].width = 26
    ws.column_dimensions["B"].width = 96

    # ── 五池 sheet ──
    by_pool = {p: [] for p in POOLS}
    for c in cands:
        by_pool.setdefault(c.get("final_pool", "Review"), []).append(c)

    for pool in POOLS:
        ws = wb.create_sheet(POOL_ZH[pool][:31])
        for j, (title, _, w, _wrap) in enumerate(COLS, 1):
            cell = ws.cell(1, j, title)
            cell.font = head_font
            cell.fill = head_fill
            cell.alignment = center
            cell.border = border
            ws.column_dimensions[get_column_letter(j)].width = w
        ws.row_dimensions[1].height = 30
        for r, c in enumerate(by_pool.get(pool, []), 2):
            ws.row_dimensions[r].height = 46
            for j, (_, key, _w, do_wrap) in enumerate(COLS, 1):
                cell = ws.cell(r, j, _xlsx_safe(_cell(c, key)))
                cell.border = border
                cell.font = base_font
                cell.alignment = wrap if do_wrap else Alignment(vertical="center")
                if r % 2 == 0:
                    cell.fill = PatternFill("solid", fgColor=C_STRIPE)
                # 链接
                u = _url(c, key)
                if u:
                    cell.hyperlink = u
                    cell.font = link_font
                if key == "comment_ev" and c.get("handle") in anchor:
                    cell.hyperlink = f"#评论证据!A{anchor[c['handle']]}"
                    cell.font = link_font
                if key == "pricing_avg" and isinstance(cell.value, (int, float)):
                    cell.number_format = "#,##0.00"
                if key in ("pricing_quote", "pricing_quote_high") and isinstance(cell.value, (int, float)):
                    cell.number_format = '$#,##0.00'
                # 验收建议色标
                if key == "verdict":
                    cell.fill = PatternFill("solid", fgColor=POOL_FILL[pool])
                    cell.font = Font(bold=True, size=10, color=POOL_FONT[pool], name="Microsoft YaHei")
                    cell.alignment = center
        ws.freeze_panes = "C2"
        if by_pool.get(pool):
            ws.auto_filter.ref = f"A1:{get_column_letter(len(COLS))}{len(by_pool[pool])+1}"
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
    print(f"专业交付 XLSX → {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
