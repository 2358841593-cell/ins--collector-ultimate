#!/usr/bin/env python3
"""客户交付版 Excel —— 美观、高可读、字段全方位、带证据路径。

四个 sheet：
  1. 候选红人   决策→身份→硬门槛→内容→赞助互动→评论信任→评分→需求达成→待人工→证据
                 分组配色表头 / 冻结 / 筛选 / 数字格式 / 活URL超链接 / 内链跳证据
  2. 证据截图   每个核验候选：嵌入现场截图缩略图 + 源URL + 时间戳（自包含可验证）
  3. 已排除     原因 + 主页
  4. 说明       怎么用 / 字段释义 / 证据路径说明

证据路径设计（双保险）：
  · 活URL超链接（主页/橱窗/bio/原帖）—— 一键看真实页面，永久有效，日常抽查
  · 嵌入截图缩略图 —— 扫描时点抓取的快照，存证防争议；点缩略图开原图

用法：.venv/bin/python scripts/export_xlsx.py [--run data/runs/discovery-XXXX.json]
输出：data/runs/delivery-{run_id}.xlsx
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNS_DIR = ROOT / "data" / "runs"
EVID_DIR = RUNS_DIR / "evidence"


def latest_run() -> str:
    files = sorted(glob.glob(str(RUNS_DIR / "discovery-*.json")), key=os.path.getmtime)
    if not files:
        raise SystemExit("没有 discovery-*.json")
    return files[-1]


def load_verifications(run_id: str) -> dict:
    p = RUNS_DIR / f"verifications-{run_id}.json"
    return json.loads(p.read_text(encoding="utf-8")).get("candidates", {}) if p.exists() else {}


def vget(vrec, cid):
    for k in (vrec or {}).get("checks", []):
        if k.get("id") == cid:
            ev = k.get("evidence", {})
            return k.get("result"), k.get("result_text", ""), ev.get("screenshot", ""), ev.get("source_url", ""), ev.get("captured_at", "")
    return None, "", "", "", ""


def amazon_state(c, vrec):
    r, *_ = vget(vrec, "amazon_storefront")
    has = c.get("has_amazon_storefront")
    if r == "pass" or has is True:
        return "confirmed"
    if has is False:
        return "none"            # bio 本就无任何相关链接 → 确实无
    # has == "unverified"（检到 Linktree/Beacons/LTK 聚合页）。浏览器穿透 fail 多因
    # LTK/联盟跳转(liketk.it/rstyle.me)隐藏最终零售商 → 仍判"待验证"，不武断判"无"。
    return "unverified"


def verdict(c, vrec):
    amz = amazon_state(c, vrec)
    fit = c.get("campaign_fit")
    trust = c.get("trust_level")
    lowq = c.get("low_quality_ratio") or 0
    pod = c.get("engagement_pod_ratio") or 0
    if c.get("status") == "exclude":
        return "不纳入", "未通过硬性条件（见已排除表）"
    if amz == "none":
        return "倾向排除", "核验未发现 Amazon Storefront —— 硬门槛不满足"
    if fit == "off":
        return "倾向排除", f"赛道「{c.get('niche_primary_label','?')}」跨垂类，与护肤设备活动不符"
    if lowq >= 0.50:   # 评论区刷量主导 → 受众真实度存疑（客户最在意的防刷信号）
        return "倾向排除", f"评论区刷量主导（互赞团 {pod:.0%}+Bot 占比 {lowq:.0%}），社区真实度存疑"
    if amz == "confirmed" and fit == "core" and trust != "low":
        return "重点候选", "已核实 Amazon 橱窗 + 对口赛道 + 评论真实；剩余仅抽查视觉真实度"
    if c.get("status") == "include" and trust != "low":
        return "建议纳入", "硬性条件全过；剩余仅需抽查视觉真实度"
    return "待人工核验", f"有 {len(c.get('review_reasons') or [])} 项待办（Amazon 待穿透确认 / 评论信任偏低 等，多为可抽查项）"


def thumb(src_rel: str, tmpdir: str, width: int = 460):
    """把证据截图缩成缩略图，返回临时路径（控制 Excel 体积）。"""
    if not src_rel:
        return None
    fp = RUNS_DIR / src_rel
    if not fp.exists():
        return None
    try:
        from PIL import Image as PILImage
        im = PILImage.open(fp)
        if im.mode in ("RGBA", "P"):
            im = im.convert("RGB")
        ratio = width / im.width
        im = im.resize((width, int(im.height * ratio)))
        out = os.path.join(tmpdir, os.path.basename(src_rel).replace(".png", ".jpg"))
        im.save(out, "JPEG", quality=72)
        return out
    except Exception:
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", type=str, default=None)
    args = ap.parse_args()

    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter
    from openpyxl.drawing.image import Image as XLImage

    run_path = args.run or latest_run()
    data = json.loads(Path(run_path).read_text(encoding="utf-8"))
    run_id = data.get("run_id", "run")
    verifs = load_verifications(run_id)
    tmpdir = tempfile.mkdtemp()

    # ── 调色板 ──
    INK = "1F2430"
    GROUP = {  # 分组表头底色
        "决策": "2E3440", "身份": "3B4252", "硬门槛": "1B5E5A", "内容": "4A3B66",
        "赞助互动": "7A4E1E", "评论信任": "1E5A2E", "评分": "8A6D1E",
        "需求": "3B4252", "待人工": "6B5618", "证据": "234E7A",
    }
    FILL_VERDICT = {"重点候选": "C8EBD3", "建议纳入": "C8EBD3", "待人工核验": "FBF0CE",
                    "倾向排除": "F6D6D1", "不纳入": "E8E8E8"}
    FILL_FIT = {"对口": "C8EBD3", "相关": "FBF0CE", "跨垂类": "F6D6D1"}
    TRUST_FONT = {"high": "1E7E34", "medium": "7A5C00", "low": "9A4A3F"}
    THIN = Side(style="thin", color="DDDDDD")
    BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
    WRAP = Alignment(vertical="center", wrap_text=True)
    WRAPL = Alignment(vertical="center", wrap_text=True, horizontal="left")
    CTR = Alignment(vertical="center", horizontal="center", wrap_text=True)

    wb = Workbook()

    # ════════════ Sheet 1：候选红人 ════════════
    ws = wb.active
    ws.title = "候选红人"
    # (表头, 宽, 组, 数字格式, 对齐)
    COLS = [
        ("验收建议", 11, "决策", None, "c"), ("建议理由", 30, "决策", None, "l"),
        ("Handle", 17, "身份", None, "l"), ("全名", 20, "身份", None, "l"),
        ("主赛道", 9, "身份", None, "c"), ("次赛道", 14, "身份", None, "l"),
        ("对口度", 8, "身份", None, "c"), ("粉丝", 10, "身份", "#,##0", "r"),
        ("档位", 7, "身份", None, "c"),
        ("Amazon橱窗", 14, "硬门槛", None, "c"), ("链接类型", 13, "硬门槛", None, "c"),
        ("内容画像", 11, "内容", None, "c"), ("产品推荐占比", 11, "内容", "0.0%", "r"),
        ("权威度", 8, "内容", "0", "r"),
        ("赞助占比", 9, "赞助互动", "0.0%", "r"), ("自然产品帖", 9, "赞助互动", "0", "r"),
        ("Reels互动率", 10, "赞助互动", "0.0%", "r"), ("Static互动率", 10, "赞助互动", "0.0%", "r"),
        ("达标", 7, "赞助互动", None, "c"),
        ("信任等级", 9, "评论信任", None, "c"), ("购买意图", 9, "评论信任", "0.0%", "r"),
        ("互赞团", 8, "评论信任", "0.0%", "r"), ("Bot", 7, "评论信任", "0.0%", "r"),
        ("分析评论数", 9, "评论信任", "0", "r"),
        ("综合评分", 9, "评分", "0.0", "r"),
        ("①Bio→Amazon", 13, "需求", None, "c"), ("②视觉真实", 11, "需求", None, "c"),
        ("③赞助≤40%", 11, "需求", None, "c"), ("④自然内容", 10, "需求", None, "c"),
        ("需人工/Modash", 17, "待人工", None, "l"),
        ("主页", 13, "证据", None, "c"), ("bio链接", 13, "证据", None, "c"),
        ("Amazon橱窗链接", 14, "证据", None, "c"), ("证据截图", 11, "证据", None, "c"),
        ("购买意图评论样例", 42, "评论信任", None, "l"), ("互赞团评论样例", 42, "评论信任", None, "l"),
    ]
    # 表头
    for ci, (name, w, grp, *_rest) in enumerate(COLS, 1):
        cell = ws.cell(row=1, column=ci, value=name)
        cell.fill = PatternFill("solid", fgColor=GROUP[grp])
        cell.font = Font(bold=True, color="FFFFFF", size=9)
        cell.alignment = CTR
        cell.border = BORDER
        ws.column_dimensions[get_column_letter(ci)].width = w
    ws.row_dimensions[1].height = 32
    ws.freeze_panes = "C2"            # 冻结到 Handle 左侧 + 表头
    ws.sheet_view.showGridLines = False

    cands = [c for c in data["candidates"] if c.get("status") in ("include", "review")]
    cands.sort(key=lambda c: -c.get("discovery_score", 0))

    # 证据 sheet 行锚点（候选 → 该候选在证据sheet的起始行），用于内链
    evidence_anchor = {}

    r = 2
    for c in cands:
        h = c["handle"]
        vrec = verifs.get(h, {})
        vt, vw = verdict(c, vrec)
        amz = amazon_state(c, vrec)
        amz_r, *_ = vget(vrec, "amazon_storefront")
        vis_r, *_ = vget(vrec, "visual")
        spon = c.get("sponsored_ratio") or 0
        org = c.get("organic_amazon_posts_count") or 0
        prof = c.get("profile_url") or f"https://instagram.com/{h}"
        bio = c.get("bio_link_url") or ""
        amz_label = ("待确认(LTK)" if amz_r == "inconclusive"
                     else {"confirmed": "✓ 已核实", "none": "✗ 无", "unverified": "待验证"}[amz])
        req1 = {"confirmed": "✓ 已核实", "none": "✗ 无", "unverified": "待验证"}[amz]
        req2 = {"pass": "✓ 真实", "fail": "✗ 存疑", "note": "注: 垂类"}.get(vis_r, "待抽查")
        req3 = "✓ 达标" if spon <= 0.40 else "✗ 超标"
        req4 = "✓" if org >= 3 else ("⚠ 偏弱" if org >= 1 else "✗ 少")
        reels = (c.get("reels_engagement_rate") or 0) / 100
        static = (c.get("static_engagement_rate") or 0) / 100
        intent_s = c.get("top_intent_comments") or []
        intent_txt = " ｜ ".join("「" + (d.get("text") or "").strip().replace(chr(10), " ")[:60] + "」"
                                 for d in intent_s[:3]) or "—"
        pod_s = c.get("pod_comment_samples") or []
        pod_txt = " ｜ ".join("「" + str(s).strip()[:50] + "」" for s in pod_s[:3]) or "无"
        vals = [
            vt, vw, "@" + h, c.get("full_name", ""),
            c.get("niche_primary_label", "—"), "/".join(c.get("niche_secondary") or []),
            c.get("campaign_fit_label", "—"), c.get("follower_count", 0), c.get("tier", "—"),
            amz_label, c.get("bio_link_type", "—"),
            c.get("creator_archetype", "—"), c.get("product_rec_ratio") or 0, c.get("skincare_authority_score") or 0,
            spon, org, reels, static, ("是" if c.get("meets_er_benchmark") else "否"),
            c.get("trust_level", "—"), c.get("purchase_intent_ratio") or 0,
            c.get("engagement_pod_ratio") or 0, c.get("bot_comment_ratio") or 0, c.get("comments_analyzed") or 0,
            c.get("discovery_score", 0),
            req1, req2, req3, req4,
            "Save率/DM(Modash)" + ("；视觉待查" if vis_r is None else ""),
            "打开 ↗", ("打开 ↗" if bio else "—"),
            ("打开 ↗" if (c.get("has_amazon_storefront") is True and bio) else "—"),
            "—",  # 证据截图内链，下面填
            intent_txt, pod_txt,
        ]
        for ci, (col, val) in enumerate(zip(COLS, vals), 1):
            name, w, grp, numfmt, al = col
            cell = ws.cell(row=r, column=ci, value=val)
            cell.border = BORDER
            cell.font = Font(size=9)
            cell.alignment = {"c": CTR, "r": Alignment(vertical="center", horizontal="right"), "l": WRAPL}[al]
            if numfmt:
                cell.number_format = numfmt
        # 配色
        ws.cell(row=r, column=1).fill = PatternFill("solid", fgColor=FILL_VERDICT.get(vt, "FFFFFF"))
        ws.cell(row=r, column=1).font = Font(bold=True, size=9)
        if c.get("campaign_fit_label") in FILL_FIT:
            ws.cell(row=r, column=7).fill = PatternFill("solid", fgColor=FILL_FIT[c["campaign_fit_label"]])
        if c.get("trust_level") in TRUST_FONT:
            ws.cell(row=r, column=20).font = Font(size=9, bold=True, color=TRUST_FONT[c["trust_level"]])
        # 超链接（活URL）
        for col_idx, url in [(31, prof), (32, bio), (33, bio if c.get("has_amazon_storefront") is True else "")]:
            if url and ws.cell(row=r, column=col_idx).value == "打开 ↗":
                ws.cell(row=r, column=col_idx).hyperlink = url
                ws.cell(row=r, column=col_idx).font = Font(color="2A6AD4", underline="single", size=9)
        r += 1

    ws.auto_filter.ref = f"A1:{get_column_letter(len(COLS))}{r-1}"

    # ── 候选表底部：因暂无 Modash 造成的局限 ──
    nr = r + 2
    title = ws.cell(row=nr, column=1, value="⚠ 因暂无 Modash 会员造成的局限（直接影响「有用红人产出量」与部分字段，需补会员才能解决）")
    title.font = Font(bold=True, size=11, color="9A4A3F")
    title.alignment = WRAPL
    ws.merge_cells(start_row=nr, start_column=1, end_row=nr, end_column=14)
    ws.cell(row=nr, column=1).fill = PatternFill("solid", fgColor="F6D6D1")
    GAPS = [
        ("① 产出量 / 精准筛选（根因）",
         "organic IG 无法在扫描前按「bio 含 Amazon 橱窗 + 护肤垂类」预筛，只能广撒网再逐个核验，大部分人无橱窗被砍 → 合格红人产出有限。"
         " Modash 可 bio 搜 'amazon.com/shop' + 护肤 + 10–150K 粉丝，一次直接返回成百个已满足硬门槛的对口创作者。这是「红人太少」的根本原因。"),
        ("② Save 率 / DM 分享（客户关键信任信号）",
         "IG 对非本人在任何公开页面都不渲染 saves / DM 分享数，浏览器也拿不到。仅 Modash 或创作者后台有。当前表中该项留空。"),
        ("③ 粉丝画像（年龄 / 性别 / 国家 / 语言）",
         "无法验证受众是否为目标市场（如美国、女性、18–34）。公开 API 不提供，仅 Modash。"),
        ("④ 假粉比例 fake follower %",
         "目前只能用评论区互赞团/水军占比粗略推断，无法精确量化。Modash 有专门的 Audience Credibility 检测。"),
        ("⑤ Lookalike 扩展精度",
         "现用 IG 原生「相似账号」图谱，噪声大（皮肤科医生→被推成家居/生活博主）。Modash 有专门 lookalike + 跨平台，扩展更准更多。"),
    ]
    nr += 1
    for t, d in GAPS:
        tc = ws.cell(row=nr, column=1, value=t); tc.font = Font(bold=True, size=9, color="9A4A3F"); tc.alignment = WRAPL
        ws.merge_cells(start_row=nr, start_column=1, end_row=nr, end_column=3)
        dc = ws.cell(row=nr, column=4, value=d); dc.font = Font(size=9); dc.alignment = WRAPL
        ws.merge_cells(start_row=nr, start_column=4, end_row=nr, end_column=14)
        ws.row_dimensions[nr].height = 42
        nr += 1
    concl = ws.cell(row=nr + 1, column=1,
                    value="结论：②③④可用 Modash 补全为自动字段；①是产出量的根本瓶颈——补 Modash 后有用红人数量可成倍提升。")
    concl.font = Font(bold=True, size=9, color="1E5A2E"); concl.alignment = WRAPL
    ws.merge_cells(start_row=nr + 1, start_column=1, end_row=nr + 1, end_column=14)

    # ════════════ Sheet 2：证据截图（嵌入现场截图）════════════
    wse = wb.create_sheet("证据截图")
    wse.sheet_view.showGridLines = False
    wse.column_dimensions["A"].width = 28
    wse.column_dimensions["B"].width = 70
    wse.cell(row=1, column=1, value="浏览器核验现场截图 · 证据存档").font = Font(bold=True, size=13)
    wse.cell(row=2, column=1, value="由 Playwright 真实 Chrome 抓取；点截图开原图，点链接看实时页面。证明结论非臆测。").font = Font(size=9, color="666666")
    er = 4
    for c in cands:
        h = c["handle"]
        vrec = verifs.get(h, {})
        checks = [k for k in (vrec.get("checks") or []) if k.get("evidence", {}).get("screenshot")]
        if not checks:
            continue
        evidence_anchor[h] = er
        wse.cell(row=er, column=1, value=f"@{h}").font = Font(bold=True, size=12, color="234E7A")
        prof = c.get("profile_url") or f"https://instagram.com/{h}"
        pc = wse.cell(row=er, column=2, value="主页 ↗"); pc.hyperlink = prof
        pc.font = Font(color="2A6AD4", underline="single", size=10)
        er += 1
        for k in checks:
            ev = k.get("evidence", {})
            res = {"pass": "通过", "fail": "未通过", "inconclusive": "待确认", "note": "提示", "error": "加载失败"}.get(k.get("result"), k.get("result", ""))
            wse.cell(row=er, column=1, value=f"  {k.get('title','')} · {res}").font = Font(bold=True, size=10)
            txt = wse.cell(row=er, column=2, value=k.get("result_text", "")); txt.alignment = WRAPL; txt.font = Font(size=9)
            er += 1
            src = ev.get("source_url", "")
            if src:
                sc = wse.cell(row=er, column=2, value=f"证据源 ↗ {src[:60]}  ·  抓取于 {ev.get('captured_at','')}")
                sc.hyperlink = src; sc.font = Font(color="2A6AD4", underline="single", size=8)
                er += 1
            tp = thumb(ev.get("screenshot", ""), tmpdir)
            if tp:
                img = XLImage(tp)
                anchor_row = er
                wse.add_image(img, f"A{anchor_row}")
                # 预留行高（缩略图约 460px 宽，按比例占多行）
                rows_needed = max(8, int(img.height / 18) + 1)
                er += rows_needed
            er += 1   # 间隔
        er += 1

    # 回填 候选红人 sheet 的「证据截图」内链
    for i, c in enumerate(cands):
        rr = 2 + i
        anc = evidence_anchor.get(c["handle"])
        cell = ws.cell(row=rr, column=[col[0] for col in COLS].index("证据截图") + 1)
        if anc:
            cell.value = "查看 →"
            cell.hyperlink = f"#证据截图!A{anc}"
            cell.font = Font(color="2A6AD4", underline="single", size=9)
        else:
            cell.value = "无核验"
            cell.font = Font(size=9, color="999999")

    # ════════════ Sheet 3：已排除 ════════════
    ws2 = wb.create_sheet("已排除")
    ws2.sheet_view.showGridLines = False
    EXC = [("Handle", 17), ("主页", 12), ("主赛道", 9), ("对口度", 8), ("粉丝", 10), ("排除原因", 30)]
    for ci, (name, w) in enumerate(EXC, 1):
        cell = ws2.cell(row=1, column=ci, value=name)
        cell.fill = PatternFill("solid", fgColor=INK); cell.font = Font(bold=True, color="FFFFFF", size=9)
        cell.alignment = CTR; cell.border = BORDER
        ws2.column_dimensions[get_column_letter(ci)].width = w
    ws2.row_dimensions[1].height = 26
    ws2.freeze_panes = "A2"
    RTXT = {"followers_out_of_range": "粉丝不在 10K–150K", "no_amazon_storefront": "无 Amazon 橱窗(硬门槛)",
            "not_product_focused": "非产品导向", "brand_like": "疑似品牌号", "inactive": "近30天未发帖",
            "private_account": "私密账号", "no_engagement_data": "无互动数据", "off_vertical": "跨垂类"}
    excl = sorted([c for c in data["candidates"] if c.get("status") == "exclude"],
                  key=lambda c: -(c.get("follower_count") or 0))
    r = 2
    for c in excl:
        rs = c.get("filter_reasons") or []
        vals = ["@" + c["handle"], "打开 ↗", c.get("niche_primary_label", "—"),
                c.get("campaign_fit_label", "—"), c.get("follower_count", 0),
                "；".join(RTXT.get(x.split(":")[0], x) for x in rs)]
        for ci, val in enumerate(vals, 1):
            cell = ws2.cell(row=r, column=ci, value=val); cell.border = BORDER
            cell.font = Font(size=9); cell.alignment = WRAPL if ci in (1, 6) else CTR
            if ci == 5:
                cell.number_format = "#,##0"
        pc = ws2.cell(row=r, column=2); pc.hyperlink = c.get("profile_url") or f"https://instagram.com/{c['handle']}"
        pc.font = Font(color="2A6AD4", underline="single", size=9)
        r += 1
    ws2.auto_filter.ref = f"A1:F{r-1}"

    # ════════════ Sheet 4：说明 ════════════
    ws3 = wb.create_sheet("说明")
    ws3.sheet_view.showGridLines = False
    ws3.column_dimensions["A"].width = 22
    ws3.column_dimensions["B"].width = 95
    s = data["summary"]
    notes = [
        ("Amazon Finds 导购型红人发现 — 交付说明", "", True),
        ("批次 / 候选", f"{run_id} · 共 {s.get('total_candidates')}（Include {s.get('include')} / Review {s.get('review')} / Exclude {s.get('exclude')}）", False),
        ("", "", False),
        ("【怎么用】", "看「候选红人」首列『验收建议』即可分流：重点候选/建议纳入→直接联系；倾向排除→忽略；待人工核验→按各列证据判断。", False),
        ("【日常无需逐项人工】", "Amazon橱窗、内容画像、赞助、互动率、评论真实度均已自动判定并写入对应列。", False),
        ("【唯一需人工/外部数据】", "Save率/DM分享（IG不公开，仅Modash/创作者后台）；视觉真实度可抽查。", False),
        ("【证据路径】", "双保险：①活URL超链接（主页/bio/橱窗）——点开看实时真实页面；②「证据截图」sheet——扫描时点抓取的现场快照(Playwright真浏览器)，存证防争议。", False),
        ("【对口度】", "对口=护肤/美妆/美容仪器核心；相关=时尚/母婴/生活；跨垂类=理财/美食/旅行（评分已×0.55降权）。", False),
        ("【综合评分】", "互动率+产品推荐占比+评论购买意图+权威度+赞助健康度+发现证据，再×赛道对口系数。逐项依据见 HTML 报告。", False),
        ("【需求达成列】", "①Bio→Amazon ②视觉真实 ③赞助≤40% ④自然内容 —— 对应客户4条核心要求，✓达成/⚠偏弱/✗未达。", False),
        ("【互赞团/水军】", "评论区刷量账号已识别并入持久库，跨创作者累积，下次扫到直接略过。", False),
        ("", "", False),
        ("⚠ 无 Modash 的局限", "（详见「候选红人」表底部）", True),
        ("① 产出量瓶颈(根因)", "organic IG 无法按「有 Amazon 橱窗 + 护肤垂类」预筛，只能广撒网逐个查→合格红人有限。Modash bio 搜索可一次返回成百个合格对口创作者。", False),
        ("② Save率/DM", "IG 不公开，浏览器拿不到，仅 Modash/创作者后台。", False),
        ("③ 粉丝画像", "年龄/性别/国家/语言无法获取，无法验证目标市场匹配，仅 Modash。", False),
        ("④ 假粉比例", "只能用水军占比粗推，Modash 有专门 Audience Credibility。", False),
        ("⑤ Lookalike精度", "IG 原生相似账号噪声大；Modash lookalike 更准更多。", False),
        ("结论", "②③④补 Modash 即自动化；①是产出量根本瓶颈，补 Modash 后有用红人可成倍提升。", False),
    ]
    for i, (k, v, big) in enumerate(notes, 1):
        kc = ws3.cell(row=i, column=1, value=k); kc.font = Font(bold=True, size=13 if big else 10)
        kc.alignment = Alignment(vertical="top")
        vc = ws3.cell(row=i, column=2, value=v); vc.alignment = WRAPL; vc.font = Font(size=10)

    # ════════════ Sheet：字段说明 · 评分逻辑（每个字段怎么算 / 样本数 / 来源）════════════
    ws4 = wb.create_sheet("字段说明·评分逻辑", 1)   # 紧跟「候选红人」之后
    ws4.sheet_view.showGridLines = False
    ws4.column_dimensions["A"].width = 15
    ws4.column_dimensions["B"].width = 104
    FIELD_METHOD = [
        ("验收建议", "综合 Amazon橱窗核实度 + 综合评分 + 评论信任，给出分流建议：重点候选 / 建议纳入 / 待人工核验 / 倾向排除。"),
        ("建议理由", "该建议的一句话依据（命中的关键正/负信号）。"),
        ("Handle / 全名", "IG 用户名 / 显示名（profile 原值）。"),
        ("主赛道 / 次赛道", "赛道打分 = bio 命中词×3 + caption 命中词×1，取最高为主赛道，得分≥主赛道30%者为次赛道。共 11 垂类。样本：bio + 近 20 帖 caption。"),
        ("对口度", "对口 = 护肤/美妆/美容仪器（核心）；相关 = 美发/时尚/健身/母婴/生活；跨垂类 = 理财/美食/旅行。直接决定综合分系数 ×1.0 / 0.9 / 0.55。"),
        ("粉丝", "profile 原值。硬门槛：1万–15万，越界即排除。"),
        ("档位", "粉丝 <10万 = micro，10万–15万 = mid。决定互动率达标基准线。"),
        ("Amazon橱窗", "✓已核实 = 浏览器穿透 bio 链接渲染后命中 amazon.com/shop 橱窗；待验证 = 检到 Linktree/Beacons/LTK 等聚合页但默认不穿透；✗无。"),
        ("链接类型", "bio 链接类型：amazon_direct / amazon_in_bio_text / linktree / beacons / ltk / stan_store / other / none。"),
        ("内容画像", "按产品推荐占比定型：≥40% amazon_finds，≥20% mixed，<20% lifestyle。样本：近 20 帖。"),
        ("产品推荐占比", "含产品推荐词的帖数 / 20。样本：近 20 帖 caption。"),
        ("权威度", "Σ(各类目命中词数 × 权重) × 5，封顶 100。权重：护肤成分×3 · 设备规格×4 · 皮肤科学×3 · 适用场景×2（设备规格最高，呼应红光/LED 专业度）。样本：20 帖 caption 全文。"),
        ("赞助占比", "赞助帖 / 近 15 帖。赞助双信号：caption 含赞助标记 或 sponsor_tags。门槛：≤40%（过饱和=不真诚）。"),
        ("自然产品帖", "非赞助的自然产品推荐帖数（近 15 帖）。"),
        ("Reels/Static互动率", "(平均赞 + 平均评) / 粉丝 × 100%。样本：近 20 帖按 Reels / 图文 分别计算。"),
        ("达标", "micro：Reels≥3.0% 或 图文≥1.8%；mid：Reels/图文≥1.5%。"),
        ("信任等级", "high = 购买意图≥15% 且 刷量<40%；medium = 意图≥5% 且 刷量<60%；low = 刷量≥50% 或 无意图。"),
        ("购买意图", "意图评论 / 有效评论（总−低质）。意图词分级：强×3（where to buy / link / 购买）· 中×2 · 弱×1。"),
        ("互赞团", "互赞团评论 / 总评论。三信号（任一且无购买意图）：命中持久水军库 / 同账号跨≥2 帖刷评 / 泛泛吹捧『内容/创作者』本身。"),
        ("Bot", "机器评论（纯 emoji / 纯 @ / 单词吹捧 / spam）/ 总评论。"),
        ("分析评论数", "样本 = 互动最高 4 帖 + 最近 4 帖（去重≤8 帖）× 每帖 25 条 ≈ 最多 200 条/创作者。多帖+下沉穿过水军层，提升采样真实度。"),
        ("综合评分", "Σ(各维子分 0-100 × 权重) × 赛道对口系数。无 Modash 权重：互动0.25 · 产品推荐0.20 · 购买意图0.20 · 权威0.15 · 赞助健康0.10 · 证据0.10。子分公式见各列；逐项数值见 CSV 末列 score_detail。"),
        ("①Bio→Amazon", "客户需求①：bio 能否到达成熟 Amazon 橱窗。浏览器穿透核实，✓已核实/待验证/✗无。"),
        ("②视觉真实", "客户需求②：近距离真实皮肤纹理、非重滤镜/柔光。浏览器截图供抽查，✓真实/✗存疑/注:垂类。"),
        ("③赞助≤40%", "客户需求③：近 15 帖赞助占比 ≤40%。✓达标/✗超标。"),
        ("④自然内容", "客户需求④：自然（非赞助）产品推荐帖 ≥3=✓ · ≥1=⚠偏弱 · <1=✗少。"),
        ("需人工/Modash", "仅靠公开 IG 拿不到、需 Modash 或人工的项（Save率/DM 分享、粉丝画像、假粉率）。"),
        ("主页 / bio链接 / Amazon橱窗链接", "活 URL 超链接，点开看实时真实页面（证据①，可随时抽查）。"),
        ("证据截图", "扫描时 Playwright 真浏览器现场快照（证据②，存证防争议），见「证据截图」sheet。"),
    ]
    h0 = ws4.cell(row=1, column=1, value="字段"); h1 = ws4.cell(row=1, column=2, value="评分逻辑 / 样本数 / 数据来源")
    for hc in (h0, h1):
        hc.font = Font(bold=True, color="FFFFFF", size=10); hc.fill = PatternFill("solid", fgColor=INK)
        hc.alignment = CTR; hc.border = BORDER
    ws4.row_dimensions[1].height = 24
    for i, (f, m) in enumerate(FIELD_METHOD, start=2):
        a = ws4.cell(row=i, column=1, value=f); a.font = Font(bold=True, size=9)
        a.alignment = Alignment(vertical="top", wrap_text=True); a.border = BORDER
        b = ws4.cell(row=i, column=2, value=m); b.font = Font(size=9); b.alignment = WRAPL; b.border = BORDER
    ws4.freeze_panes = "A2"

    out = RUNS_DIR / f"delivery-{run_id}.xlsx"
    wb.save(out)
    print(f"✓ Excel 交付版: {out}")
    print(f"  候选红人 {len(cands)} · 证据截图 {len(evidence_anchor)} 个候选 · 已排除 {len(excl)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
