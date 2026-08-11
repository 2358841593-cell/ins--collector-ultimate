#!/usr/bin/env python3
"""五池 XLSX 业务交付导出（专业可读版）。

对齐客户 SOP §8 字段；以"购买意向评论证据"为重点（客户核心诉求）；可点真实链接
（IG 主页 / 电商橱窗）；全部已存评论的中文翻译与原文写入证据 sheet，截图作为可选附件。
风格：色标验收建议 + 隔行底纹 + 合理行高列宽 + 冻结窗格 + 自动筛选，专业可读。
"""
from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
from export_v2_comment_status import (
    comment_collection_complete,
    comment_unavailable_note,
)
from extensions.sop_v2 import comment_translation as translation_mod
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
    ("Handle", "handle_at", 28, True),
    ("全名", "full_name", 32, True),
    ("粉丝", "follower_count", 9, False),
    ("赛道", "niche", 11, False),
    ("购买意向评论（中文 / 原文）", "intent_snippet", 40, True),
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
    ("Reels 总体证据", "pricing_population", 34, True),
    ("AI 评分", "ai", 8, False),
    ("结论", "summary", 30, True),
    ("待补/原因", "reasons", 34, True),
    ("主页", "profile", 8, False),
    ("采集时间", "captured_at", 14, True),
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


def _safe_http_url(value):
    """Return a workbook-safe HTTP(S) URL or ``None``.

    Comment/profile/storefront URLs are external evidence.  Rejecting local and
    active-content schemes here prevents a malformed candidate from creating a
    clickable ``file:``, ``javascript:`` or custom-protocol link in the delivery.
    """
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = urlparse(raw)
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return None
    return raw


def _comment_evidence_rows(candidate):
    """Yield normalized rows for the dedicated comment-evidence sheet.

    Formal SKIN6 rows have ``comment_translations``.  ``comment_records`` is a
    transparent legacy fallback so an older batch never produces an empty evidence
    sheet merely because it predates the translation schema.
    """
    translations = candidate.get("comment_translations") or []
    if isinstance(translations, list) and translations:
        for item in translations:
            if not isinstance(item, dict):
                continue
            original = str(item.get("original_text") or "")
            translated = str(
                item.get("translated_zh") or item.get("translated_text") or ""
            )
            yield {
                "username": item.get("username") or "",
                "translated_zh": translated,
                "original_text": original,
                "source_language": item.get("source_language") or "und",
                "intent_grade": (
                    item.get("translated_intent_grade_zh")
                    or item.get("grade_zh")
                    or "—"
                ),
                "low_quality": (
                    "是" if item.get("translated_low_quality") is True else "否"
                ),
                "translation_status": (
                    item.get("status")
                    or item.get("translation_status")
                    or "unknown"
                ),
                "post_url": _safe_http_url(item.get("post_url")),
                "evidence_source": "中文翻译 + 原文",
            }
        return

    records = candidate.get("comment_records") or []
    if not isinstance(records, list):
        return
    for item in records:
        if not isinstance(item, dict):
            continue
        yield {
            "username": item.get("username") or "",
            "translated_zh": "",
            "original_text": str(item.get("text") or ""),
            "source_language": "und",
            "intent_grade": "—",
            "low_quality": "—",
            "translation_status": "not_translated_legacy",
            "post_url": _safe_http_url(item.get("post_url")),
            "evidence_source": "历史结构化原文",
        }


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
        translated_rows = translation_mod.delivery_evidence_rows(c, limit=6)
        translated_intent = bool(
            translated_rows and translated_rows[0].get("evidence_kind") == "intent"
        )
        translation_note = translation_mod.delivery_translation_note(c)
        deep_complete = comment_collection_complete(c)
        unavailable_note = comment_unavailable_note(c)
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
        if deep_complete and unavailable_note:
            completion_note = unavailable_note
        elif verified_zero:
            completion_note = "未发现公开评论（已完成采集）"
        elif deep_complete and valid_count < 20:
            completion_note = (
                f"公开有效评论有限（{valid_count}条，已完成采集）"
            )
        elif not deep_complete and snips:
            completion_note = "评论采集未完成（待复采）"
        if snips or translated_intent:
            rows = [completion_note] if completion_note else []
            if translation_note:
                rows.append(translation_note)
            if translated_intent:
                for row in translated_rows[:6]:
                    grade_zh = row.get("grade_zh") or "—"
                    who = f"@{row['username']}（{grade_zh}）" if row.get("username") else f"评论（{grade_zh}）"
                    if row.get("status") == "translated" and row.get("translated_zh"):
                        rows.append(f"· {who} 中文：{row['translated_zh']}")
                        rows.append(
                            f"  原文[{row.get('source_language') or 'und'}]："
                            f"{row.get('original_text') or ''}"
                        )
                    else:
                        rows.append(f"· {who} 原文：{row.get('original_text') or ''}")
                        rows.append("  中文翻译失败 · 原文已保留")
            else:
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
        rows = [f"无明显购买意向（有效评论 {vc} 条）"]
        if translation_note:
            rows.append(translation_note)
        if translated_rows:
            for row in translated_rows[:6]:
                if row.get("status") == "translated" and row.get("translated_zh"):
                    rows.append(f"· 评论样本中文：{row['translated_zh']}")
                    rows.append(
                        f"  原文[{row.get('source_language') or 'und'}]："
                        f"{row.get('original_text') or ''}"
                    )
                else:
                    rows.append(f"· 评论原文：{row.get('original_text') or ''}")
                    rows.append("  中文翻译失败 · 原文已保留")
        elif c.get("comment_sample"):
            rows.append("中文翻译尚未执行（原始评论已保留）")
        return "\n".join(rows)
    if key == "comment_ev":
        return "证据 ↗" if (
            c.get("comment_translations")
            or c.get("comment_records")
            or c.get("translated_intent_comments")
            or c.get("intent_posts")
            or c.get("comment_shots")
        ) else "—"
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
            if status == "not_applicable_no_reels":
                return f"0/{requested}（无 Reels）"
            if status == "fallback_modash":
                return "第三方"
            return f"{sample}/{requested}" if sample else "待补"
        if key == "pricing_population":
            population = p.get("population_evidence") or {}
            return (
                f"basis={p.get('population_basis') or 'unproven'} · "
                f"proof={population.get('proof_mode') or 'unproven'} · "
                f"Tab穷尽={'是' if population.get('reels_tab_exhausted') else '否'} · "
                f"总体完整={'是' if population.get('population_complete') else '否'} · "
                f"发现Reels={population.get('reels_seen') if population.get('reels_seen') is not None else '未知'}"
            )
        if key == "pricing_avg":
            return p.get("average_plays")
        quote = p.get("quote_usd") or {}
        if key == "pricing_quote":
            return quote.get("default")
        if key == "pricing_quote_high":
            return quote.get("max")
        if status == "complete":
            return f"完整 · IG 最近 {requested} 条非置顶 Reels"
        if status == "complete_available":
            return f"完整 · IG 全部可用 Reels {sample}/{requested}（Tab 已穷尽）"
        if status == "not_applicable_no_reels":
            proof = (p.get("population_evidence") or {}).get("proof_mode")
            if proof == "reels_surface_absent":
                return "不适用 · 两次 /reels 均回健康主页且 Reels 入口不存在（报价为空）"
            return "不适用 · Reels Tab 已穷尽、明确空态（报价为空）"
        if status == "partial":
            return f"暂估 · IG {sample}/{requested}（Tab 穷尽未证明）"
        if status == "fallback_modash":
            return "历史第三方均播（当前正式口径禁止，B3 阻断）"
        return "待补 Reels 播放量（Tab 穷尽/原生指标未证明）"
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
        return _safe_http_url(c.get("profile_url")) or _safe_http_url(
            f"https://www.instagram.com/{h}/"
        )
    if key == "comment_ev":       # 评论证据 → 有意图评论的帖子链接（客户点开核验"谁说了什么"）
        translated = c.get("translated_intent_comments") or []
        for row in translated:
            if url := _safe_http_url(row.get("post_url")):
                return url
        ip = c.get("intent_posts") or []
        if ip and (url := _safe_http_url(ip[0].get("post_url"))):
            return url
        for row in c.get("comment_translations") or []:
            if isinstance(row, dict) and (
                url := _safe_http_url(row.get("post_url"))
            ):
                return url
        for row in c.get("comment_records") or []:
            if isinstance(row, dict) and (
                url := _safe_http_url(row.get("post_url"))
            ):
                return url
        return None
    if key == "storefront_link" and storefront_mod.has_storefront(c):
        return _safe_http_url(storefront_mod.storefront_url(c))
    if key == "pricing_sample":
        reels = (c.get("pricing_estimate") or {}).get("reels") or []
        return _safe_http_url(reels[0].get("url")) if reels else None
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
    center = Alignment(horizontal="center", vertical="center", wrap_text=True)

    wb = Workbook()

    # ── 结构化评论证据 sheet（截图仅作可选附件）──
    wse = wb.active
    wse.title = "评论证据"
    wse.cell(1, 1, "结构化评论证据 · 中文翻译与原文对照").font = Font(
        bold=True, size=13, name="Microsoft YaHei"
    )
    wse.cell(
        2,
        1,
        "覆盖全部已保存评论；中文译文与原文并列，帖子链接可点击核验。截图如有则附在表格下方。",
    ).font = Font(size=9, color="5E6672")
    evidence_columns = [
        ("Handle", "handle", 22),
        ("全名", "full_name", 22),
        ("评论者", "username", 20),
        ("中文译文", "translated_zh", 48),
        ("原文", "original_text", 48),
        ("原语言", "source_language", 10),
        ("购买意向等级", "intent_grade", 13),
        ("低质评论", "low_quality", 10),
        ("翻译状态", "translation_status", 16),
        ("帖子链接", "post_url", 42),
        ("证据来源", "evidence_source", 18),
    ]
    for column, (title, _key, width) in enumerate(evidence_columns, 1):
        cell = wse.cell(3, column, title)
        cell.font = head_font
        cell.fill = head_fill
        cell.alignment = center
        cell.border = border
        wse.column_dimensions[get_column_letter(column)].width = width
    anchor = {}
    er = 4
    for c in cands:
        rows = list(_comment_evidence_rows(c))
        if rows:
            handle = str(c.get("handle") or "").lstrip("@")
            anchor.setdefault(handle, er)
        for evidence in rows:
            values = {
                **evidence,
                "handle": (
                    str(c.get("handle") or "")
                    if str(c.get("handle") or "").startswith("@")
                    else f"@{c.get('handle') or ''}"
                ),
                "full_name": c.get("full_name") or "",
            }
            for column, (_title, key, _width) in enumerate(
                evidence_columns, 1
            ):
                cell = wse.cell(er, column, _xlsx_safe(values.get(key, "")))
                cell.font = base_font
                cell.border = border
                cell.alignment = wrap
                if er % 2 == 0:
                    cell.fill = PatternFill("solid", fgColor=C_STRIPE)
                if key == "post_url" and evidence.get("post_url"):
                    cell.hyperlink = evidence["post_url"]
                    cell.font = link_font
            er += 1

    structured_end = er - 1
    wse.freeze_panes = "A4"
    if structured_end >= 4:
        wse.auto_filter.ref = (
            f"A3:{get_column_letter(len(evidence_columns))}{structured_end}"
        )
    else:
        wse.cell(4, 1, "本批没有可导出的结构化评论证据")
        er = 6

    screenshot_heading_written = False
    for c in cands:
        shots = c.get("comment_shots") or []
        shots = [s for s in shots if (ROOT / s).exists()]
        if not shots:
            continue
        if not screenshot_heading_written:
            er += 1
            wse.cell(er, 1, "可选评论区截图附件").font = Font(
                bold=True, size=12, name="Microsoft YaHei"
            )
            er += 2
            screenshot_heading_written = True
        h = str(c.get("handle") or "").lstrip("@")
        anchor.setdefault(h, er)
        hc = wse.cell(
            er,
            1,
            _xlsx_safe(f"@{h} · {c.get('full_name') or ''}"),
        )
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
        (
            "报价完整（10条 / 全部可用 / 无Reels不适用）",
            f"{price_status.get('complete', 0)} / "
            f"{price_status.get('complete_available', 0)} / "
            f"{price_status.get('not_applicable_no_reels', 0)}",
        ),
        ("报价未闭合（暂估 / 历史第三方 / 缺失）",
         f"{price_status.get('partial', 0)} / {price_status.get('fallback_modash', 0)} / {price_status.get('missing', 0)}"),
        ("", ""),
        ("阅读说明", "五个决策池互斥，一人一池。『验收建议』色标区分；『购买意向评论』是核心，评论证据列可跳到中文翻译与原文对照，截图如有亦附。"),
        ("链接", "Handle/主页/电商橱窗或购物入口均可点击核验。"),
        ("ER 口径", "Modash ER（近两月中位数，偏低）与 IG 实算 ER（近帖，部分藏赞）并列参考；本赛道 Modash ER 普遍<2%，硬门槛以可靠标准+购买意向评论为准。"),
        ("预估报价口径", "先排除置顶 Reels，再取最近 10 条平均播放量；少于 10 条仅在 Reels Tab 已到底且连续两轮无增长、全部媒体原生指标均闭合时，按全部可用 Reels 估价；0 条 Reels 为不适用且报价留空。只用 Instagram 原生播放量，禁止第三方/总播放/Facebook 替代。默认 CPM $35，参考区间 $35–40；仅供预算参考，不是博主实际报价，也不参与评分或路由。"),
        ("Herman 两列", "供客户审批回填。"),
    ]
    for r, (k, v) in enumerate(rows, 3):
        ws.cell(r, 1, k).font = Font(bold=True, size=10.5, name="Microsoft YaHei")
        cell = ws.cell(r, 2, _xlsx_safe(v))
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
        ws.row_dimensions[1].height = 46
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
                normalized_handle = str(c.get("handle") or "").lstrip("@")
                if key == "comment_ev" and normalized_handle in anchor:
                    cell.hyperlink = (
                        f"#评论证据!A{anchor[normalized_handle]}"
                    )
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
