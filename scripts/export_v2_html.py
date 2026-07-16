#!/usr/bin/env python3
"""五池 HTML 表格交付（可读版，替代难看的 XLSX）。

对齐客户 SOP §8 字段；购买意向评论**分级**(高/中/低)带"谁说的"；能点的都点：
IG 主页 / Amazon 橱窗 / 有意图评论的帖子链接 / 合作品牌。自包含单文件 HTML，浏览器直开。

用法：python scripts/export_v2_html.py --decisions decisions.json --out deliverable.html
"""
from __future__ import annotations

import argparse
import html
import json
from collections import Counter
from pathlib import Path

POOLS = ["Include-With-Storefront", "Include-Without-Storefront",
         "Priority-Review", "Review", "Exclude"]
POOL_ZH = {"Include-With-Storefront": "纳入 · 有橱窗", "Include-Without-Storefront": "纳入 · 无橱窗",
           "Priority-Review": "优先复核", "Review": "待复核", "Exclude": "已排除"}
POOL_CLASS = {"Include-With-Storefront": "inc", "Include-Without-Storefront": "inc2",
              "Priority-Review": "pri", "Review": "rev", "Exclude": "exc"}
NICHE_ZH = {"skincare": "护肤", "beauty_device": "美容仪", "beauty_wellness": "美妆/健康",
            "lifestyle": "生活方式", "other": "其他"}
GRADE_CLASS = {"高": "g-hi", "中": "g-mid", "低": "g-lo"}

COLS = ["验收", "红人", "粉丝", "赛道", "购买意向评论（谁说了什么）", "Amazon 橱窗",
        "合作品牌", "赞助", "Fake%", "ER 对照", "AI", "结论", "待补 / 原因"]


def esc(x):
    return html.escape(str(x)) if x is not None else ""


def _intent_cell(c):
    snips = c.get("high_intent_snippets") or []
    posts = c.get("intent_posts") or []
    post_url = posts[0].get("post_url") if posts else None
    g = c.get("intent_by_grade") or {}
    if snips:
        rows = []
        for s in snips[:6]:
            # s 形如 "@user（低）: 原话"
            cls = "g-lo"
            for zh, cl in GRADE_CLASS.items():
                if f"（{zh}）" in s:
                    cls = cl
            rows.append(f'<div class="cmt {cls}">{esc(s)}</div>')
        head = (f'<div class="tier">高{g.get("high",0)} 中{g.get("medium",0)} 低{g.get("low",0)}'
                + (f' · <a href="{esc(post_url)}" target="_blank">看帖 ↗</a>' if post_url else '') + '</div>')
        return head + "".join(rows)
    vc = c.get("valid_comments")
    if vc is None or vc < 20:
        return '<span class="muted">评论未采到（账号受限/待深采）</span>'
    return '<span class="muted">无明显购买意向</span>'


def _storefront_cell(c):
    st = c.get("storefront_status")
    if st == "confirmed_yes":
        u = c.get("amazon_storefront_link") or ""
        return f'<a href="{esc(u)}" target="_blank">打开橱窗 ↗</a>'
    return {"confirmed_no": '<span class="muted">确认无</span>',
            "unknown": '<span class="muted">未确认</span>'}.get(st, '<span class="muted">—</span>')


def _er_cell(c):
    mo, ig = c.get("modash_er"), c.get("ig_er") if c.get("ig_er") is not None else c.get("real_er")
    parts = [f"Modash {mo}%" if mo is not None else "Modash —",
             f'<b>IG实算 {ig}%</b>' if ig is not None else '<span class="muted">IG 待读</span>']
    return " / ".join(parts)


def _row(c):
    h = (c.get("handle") or "").lstrip("@")
    prof = c.get("profile_url") or f"https://www.instagram.com/{h}/"
    pool = c.get("final_pool", "Review")
    brands = c.get("brand_collaborations") or []
    reasons = (c.get("review_reasons_text") or []) + (c.get("exclude_reasons_text") or [])
    cells = [
        f'<span class="badge {POOL_CLASS.get(pool,"rev")}">{esc(POOL_ZH.get(pool,pool))}</span>',
        f'<a href="{esc(prof)}" target="_blank">@{esc(h)}</a><div class="sub">{esc(c.get("full_name") or "")}</div>',
        esc(f'{(c.get("follower_count") or 0):,}') if c.get("follower_count") else "—",
        esc(NICHE_ZH.get(c.get("core_niche_key"), c.get("core_niche_key") or "—")),
        _intent_cell(c),
        _storefront_cell(c),
        ("、".join(esc(b) for b in brands[:6]) if brands else '<span class="muted">待补</span>'),
        (f'{c.get("sponsorship_saturation")}%' if c.get("sponsorship_saturation") is not None else '<span class="muted">—</span>'),
        (f'{c.get("fake_pct")}%' if c.get("fake_pct") is not None else '<span class="muted">待补</span>'),
        _er_cell(c),
        f'<b>{esc(c.get("ai_vetting_score"))}</b>' if c.get("ai_vetting_score") is not None else "—",
        esc(c.get("decision_summary") or ""),
        ("；".join(esc(r) for r in reasons) if reasons else '<span class="muted">—</span>'),
    ]
    return "<tr>" + "".join(f"<td>{v}</td>" for v in cells) + "</tr>"


def build_html(decisions) -> str:
    cands = decisions.get("candidates", [])
    meta = decisions.get("manifest", {})
    pc = Counter(c.get("final_pool", "Review") for c in cands)
    by_pool = {p: [c for c in cands if c.get("final_pool") == p] for p in POOLS}
    sf = sum(1 for c in cands if c.get("storefront_status") == "confirmed_yes")

    cards = "".join(
        f'<div class="card {POOL_CLASS[p]}"><div class="n">{pc.get(p,0)}</div>'
        f'<div class="l">{POOL_ZH[p]}</div></div>' for p in POOLS)

    sections = []
    for p in POOLS:
        rows = by_pool.get(p, [])
        if not rows:
            continue
        thead = "".join(f"<th>{esc(t)}</th>" for t in COLS)
        body = "".join(_row(c) for c in rows)
        sections.append(
            f'<h2 class="{POOL_CLASS[p]}">{POOL_ZH[p]} <span class="cnt">{len(rows)}</span></h2>'
            f'<div class="tw"><table><thead><tr>{thead}</tr></thead><tbody>{body}</tbody></table></div>')

    return f"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Instagram 红人筛选交付 · {esc(meta.get('batch_id',''))}</title>
<style>
*{{box-sizing:border-box}}
body{{margin:0;font:14px/1.6 -apple-system,'PingFang SC','Microsoft YaHei',sans-serif;color:#1c2b28;background:#f6f8f7}}
.wrap{{max-width:1500px;margin:0 auto;padding:28px 20px 60px}}
h1{{font-size:22px;margin:0 0 4px}} .meta{{color:#5e6672;font-size:13px;margin-bottom:20px}}
.cards{{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:8px}}
.card{{flex:1;min-width:120px;background:#fff;border:1px solid #e3e8e6;border-radius:12px;padding:14px 16px;border-top:3px solid #ccc}}
.card .n{{font-size:26px;font-weight:700}} .card .l{{color:#5e6672;font-size:13px}}
.card.inc{{border-top-color:#1e7a47}} .card.inc2{{border-top-color:#3a9d6a}}
.card.pri{{border-top-color:#c9922b}} .card.rev{{border-top-color:#b98b2e}} .card.exc{{border-top-color:#c0554f}}
.note{{background:#fff;border:1px solid #e3e8e6;border-radius:12px;padding:14px 18px;margin:16px 0 24px;color:#41504c;font-size:13px}}
h2{{font-size:17px;margin:30px 0 10px;padding-left:10px;border-left:4px solid #ccc}}
h2.inc,h2.inc2{{border-color:#1e7a47;color:#1e7a47}} h2.pri,h2.rev{{border-color:#c9922b;color:#9a6a1e}} h2.exc{{border-color:#c0554f;color:#a43b37}}
h2 .cnt{{font-size:13px;color:#5e6672;font-weight:500}}
.tw{{overflow-x:auto;border:1px solid #e3e8e6;border-radius:12px;background:#fff}}
table{{border-collapse:collapse;width:100%;min-width:1200px}}
th{{background:#1f4e4a;color:#fff;font-weight:600;text-align:left;padding:10px 12px;position:sticky;top:0;font-size:12.5px;white-space:nowrap}}
td{{padding:10px 12px;border-top:1px solid #eef2f0;vertical-align:top}}
tr:nth-child(even) td{{background:#fafcfb}}
a{{color:#1a6e64;text-decoration:none}} a:hover{{text-decoration:underline}}
.sub{{color:#8a9490;font-size:12px}} .muted{{color:#a3aca8}}
.badge{{display:inline-block;padding:3px 9px;border-radius:20px;font-size:12px;font-weight:600;white-space:nowrap}}
.badge.inc,.badge.inc2{{background:#dceee4;color:#1e7a47}} .badge.pri,.badge.rev{{background:#fbf0d9;color:#9a6a1e}} .badge.exc{{background:#f7e7e5;color:#a43b37}}
.cmt{{padding:3px 8px;border-radius:6px;margin:2px 0;font-size:12.5px;background:#f2f6f4}}
.cmt.g-hi{{background:#dcefe2;border-left:3px solid #1e7a47}} .cmt.g-mid{{background:#eef4ea;border-left:3px solid #6a9d3a}} .cmt.g-lo{{background:#f5f7f3;border-left:3px solid #b9c4ad}}
.tier{{font-size:11.5px;color:#5e6672;margin-bottom:3px}}
td b{{color:#1c2b28}}
</style></head><body><div class="wrap">
<h1>Instagram 红人筛选 · 交付表</h1>
<div class="meta">批次 {esc(meta.get('batch_id',''))} · {esc(meta.get('campaign_track',''))} · 生成 {esc(decisions.get('generated_at',''))} · 候选 {len(cands)} · 确认 Amazon 橱窗 {sf}</div>
<div class="cards">{cards}</div>
<div class="note"><b>阅读说明：</b>五池互斥，一人一池，『验收』色标区分。<b>购买意向评论分三级</b>——高(求链接/已下单)·中(考虑/问适用)·低(真诚产品热情，非水军)，均标明"谁说了什么"，点『看帖 ↗』核验。Handle/橱窗/合作品牌均可点。ER 对照：Modash（参考）+ IG 实算（近帖赞评，硬门槛）。空白项已标『待补/待深采』并写明原因。</div>
{''.join(sections)}
</div></body></html>"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--decisions", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    decisions = json.loads(Path(args.decisions).read_text())
    Path(args.out).write_text(build_html(decisions), encoding="utf-8")
    print(f"HTML 交付 → {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
