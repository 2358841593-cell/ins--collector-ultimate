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
        "合作品牌", "赞助", "Fake%", "受众画像（Modash）", "ER 对照", "AI", "结论", "待补 / 原因"]


def esc(x):
    return html.escape(str(x)) if x is not None else ""


def _na(c):
    # 有 Modash 报告但字段空 = Modash 本身没有；没报告 = 待补数（区分"Modash无"和"我们没采"）
    return '<span class="muted">Modash 无</span>' if c.get("modash_report") else '<span class="muted">待补数</span>'


def _fake_cell(c):
    v = c.get("fake_pct")
    if v is None:
        return _na(c)
    cls = "bad" if v >= 25 else "ok"
    return f'<span class="{cls}">{v}%</span>'


def _gz(code):
    return {"FEMALE": "女", "MALE": "男"}.get(code, esc(code))


def _pct_join(items, n=99):
    return "、".join(f'{esc(x["name"])} {x["pct"]}%' for x in (items or [])[:n] if x.get("name"))


def _audience_cell(c):
    if not c.get("modash_report") and not c.get("audience_countries"):
        return _na(c)
    parts = []
    ac = c.get("audience_countries") or []
    if ac:
        parts.append("国家: " + _pct_join(ac, 3))
    al = c.get("audience_languages") or []
    if al:
        parts.append("语言: " + _pct_join(al, 2))
    ag = c.get("audience_genders") or []
    if ag:
        parts.append("性别: " + "、".join(f'{_gz(x["name"])} {x["pct"]}%' for x in ag[:2]))
    aa = c.get("audience_ages") or []
    if aa:
        top = max(aa, key=lambda x: x.get("pct", 0))
        parts.append(f'主龄段: {esc(top["name"])} {top["pct"]}%')
    at = c.get("audience_types") or {}
    if at:
        parts.append(f'真人 {at.get("real_people")}% · 机器人 {at.get("bots")}%')
    cc = c.get("creator_country")
    if cc:
        parts.append(f'创作者: {esc(cc)}')
    return "<br>".join(f'<span class="aud">{p}</span>' for p in parts) if parts else _na(c)


def _kv(label, val_html):
    return f'<div class="kv"><span class="k">{esc(label)}</span><span class="v">{val_html}</span></div>' if val_html else ""


def _detail_panel(c, ncols):
    """每个红人的完整 Modash 画像（可折叠）：一次 credit 拿到的全部维度都铺出来。"""
    h = (c.get("handle") or "").lstrip("@")
    if not c.get("modash_report"):
        inner = '<div class="dl-note">此红人尚未做 Modash 补数（待补数）。</div>'
        return (f'<tr class="det"><td colspan="{ncols}"><details><summary>▸ 完整 Modash 画像 · @{esc(h)}</summary>'
                f'{inner}</details></td></tr>')
    blocks = []

    # 粉丝质量拆解
    at = c.get("audience_types") or {}
    q = []
    if at:
        q.append(_kv("真人 / 网红 / 普通量粉 / 可疑 / 机器人",
                     f'{at.get("real_people")}% / {at.get("influencers")}% / {at.get("mass_followers")}% '
                     f'/ {at.get("suspicious")}% / {at.get("bots")}%'))
    q.append(_kv("假粉率（粉丝 / 点赞者）",
                 f'{c.get("fake_pct")}%' + (f' / {c.get("likers_fake_pct")}%（点赞者更难造假）'
                 if c.get("likers_fake_pct") is not None else '') if c.get("fake_pct") is not None else ""))
    if c.get("notable_pct") is not None:
        q.append(_kv("名人/影响力粉占比", f'{c.get("notable_pct")}%'))
    if q:
        blocks.append('<div class="grp"><div class="gt">粉丝质量</div>' + "".join(q) + '</div>')

    # 受众地区/人群
    a = []
    a.append(_kv("受众国家", _pct_join(c.get("audience_countries"))))
    a.append(_kv("受众城市", _pct_join(c.get("audience_cities"))))
    if c.get("target_countries_audience_pct") is not None:
        a.append(_kv("目标国受众占比", f'{c.get("target_countries_audience_pct")}%'))
    ag = c.get("audience_genders") or []
    if ag:
        a.append(_kv("受众性别", "、".join(f'{_gz(x["name"])} {x["pct"]}%' for x in ag)))
    a.append(_kv("受众年龄段", _pct_join(c.get("audience_ages"))))
    gpa = c.get("audience_genders_per_age") or []
    if gpa:
        a.append(_kv("各龄段性别", "；".join(f'{esc(x["age"])} 女{x["female"]}%/男{x["male"]}%' for x in gpa)))
    a.append(_kv("受众语言", _pct_join(c.get("audience_languages"))))
    if c.get("audience_interests"):
        a.append(_kv("受众兴趣", "、".join(esc(i) for i in c["audience_interests"])))
    if [x for x in a if x]:
        blocks.append('<div class="grp"><div class="gt">受众画像（粉丝）</div>' + "".join(a) + '</div>')

    # 点赞者画像（更真）
    lk = []
    lk.append(_kv("点赞者国家", _pct_join(c.get("likers_countries"))))
    lg = c.get("likers_genders") or []
    if lg:
        lk.append(_kv("点赞者性别", "、".join(f'{_gz(x["name"])} {x["pct"]}%' for x in lg)))
    lk.append(_kv("点赞者年龄", _pct_join(c.get("likers_ages"))))
    if [x for x in lk if x]:
        blocks.append('<div class="grp"><div class="gt">点赞者画像（更难造假，反映真实互动人群）</div>'
                      + "".join(lk) + '</div>')

    # 受众高频标签/提及
    t = []
    t.append(_kv("受众高频话题", _pct_join(c.get("audience_hashtags"))))
    t.append(_kv("受众高频提及", _pct_join(c.get("audience_mentions"))))
    if [x for x in t if x]:
        blocks.append('<div class="grp"><div class="gt">受众关注</div>' + "".join(t) + '</div>')

    # 创作者档 + 跨平台
    p = []
    if c.get("creator_fullname"):
        p.append(_kv("全名", esc(c["creator_fullname"])))
    bits = []
    if c.get("creator_gender"):
        bits.append(_gz(c["creator_gender"]))
    if c.get("creator_verified"):
        bits.append("已认证 ✓")
    if c.get("account_type"):
        bits.append(esc(c["account_type"]))
    if bits:
        p.append(_kv("类型", " · ".join(bits)))
    stat = []
    if c.get("posts_count") is not None:
        stat.append(f'发帖 {c["posts_count"]:,}')
    if c.get("avg_likes") is not None:
        stat.append(f'均赞 {c["avg_likes"]:,}')
    if c.get("avg_comments") is not None:
        stat.append(f'均评 {c["avg_comments"]:,}')
    if c.get("avg_reels_plays") is not None:
        stat.append(f'Reels均播 {c["avg_reels_plays"]:,}')
    if stat:
        p.append(_kv("表现", " · ".join(stat)))
    if c.get("followers_growth_pct") is not None:
        g = c["followers_growth_pct"]
        p.append(_kv("近月涨粉", f'<span class="{"ok" if g>=0 else "bad"}">{g:+}%</span>'))
    if c.get("contacts_has_email") is not None:
        p.append(_kv("公开邮箱", "有 ✓" if c["contacts_has_email"] else "无"))
    sa = c.get("social_accounts") or {}
    if sa:
        links = " · ".join(f'<a href="{esc(u)}" target="_blank">{esc(k)} ↗</a>' for k, u in sa.items())
        p.append(_kv("跨平台", links))
    if [x for x in p if x]:
        blocks.append('<div class="grp"><div class="gt">创作者档案</div>' + "".join(p) + '</div>')

    # 赞助帖样例（带链接/互动）
    sp = c.get("sponsored_post_samples") or []
    if sp:
        items = []
        for s in sp[:6]:
            meta = []
            if s.get("sponsor"):
                meta.append(f'@{esc(s["sponsor"])}')
            if s.get("likes") is not None:
                meta.append(f'♥{s["likes"]:,}')
            if s.get("comments") is not None:
                meta.append(f'💬{s["comments"]:,}')
            items.append(f'<a href="{esc(s.get("url"))}" target="_blank">帖 ↗</a>'
                         + (f' <span class="muted">{" · ".join(meta)}</span>' if meta else ''))
        blocks.append('<div class="grp"><div class="gt">赞助帖样例</div><div class="kv"><span class="v">'
                      + "　".join(items) + '</span></div></div>')

    inner = "".join(blocks) or '<div class="dl-note">Modash 报告已拉取，但受众明细为空（Modash 无此号画像数据）。</div>'
    return (f'<tr class="det"><td colspan="{ncols}"><details><summary>▸ 完整 Modash 画像 · @{esc(h)}'
            f'</summary><div class="dl">{inner}</div></details></td></tr>')


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
    # 无意图片段：区分四种性质不同的情况，别把"我们抽取失败"甩锅成"账号受限"
    if not c.get("comments_read"):
        return '<span class="muted">评论待采集（深采未完成）</span>'
    vc = c.get("valid_comments")
    if (c.get("comments_analyzed") or 0) == 0:
        return '<span class="warn">评论抽取失败（待复采）</span>'   # 深采跑了但一条没抽到 = 系统侧待修
    if (vc or 0) < 20:
        return f'<span class="muted">样本偏少（{vc} 条，待补采）</span>'
    return '<span class="muted">无明显购买意向（有效评论 {} 条）</span>'.format(vc)


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
        _fake_cell(c),
        _audience_cell(c),
        _er_cell(c),
        f'<b>{esc(c.get("ai_vetting_score"))}</b>' if c.get("ai_vetting_score") is not None else "—",
        esc(c.get("decision_summary") or ""),
        ("；".join(esc(r) for r in reasons) if reasons else '<span class="muted">—</span>'),
    ]
    main = "<tr>" + "".join(f"<td>{v}</td>" for v in cells) + "</tr>"
    return main + _detail_panel(c, len(cells))


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
.aud{{display:block;font-size:12px;color:#41504c}}
.ok{{color:#1e7a47;font-weight:600}} .bad{{color:#c0554f;font-weight:600}} .warn{{color:#b06a1e;font-weight:600}}
tr.det td{{padding:0;border-top:0;background:#fff!important}}
tr.det details{{margin:0}}
tr.det summary{{cursor:pointer;padding:8px 14px;color:#1a6e64;font-size:12.5px;background:#f3f7f5;user-select:none;list-style:none}}
tr.det summary::-webkit-details-marker{{display:none}}
tr.det summary:hover{{background:#eaf1ee}}
tr.det details[open] summary{{border-bottom:1px solid #e3e8e6}}
.dl{{padding:12px 16px;display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:14px}}
.dl-note{{padding:12px 16px;color:#8a9490}}
.grp{{background:#fafcfb;border:1px solid #eef2f0;border-radius:10px;padding:10px 12px}}
.grp .gt{{font-weight:700;color:#1f4e4a;font-size:12.5px;margin-bottom:6px}}
.kv{{display:flex;gap:8px;font-size:12.5px;padding:2px 0;align-items:baseline}}
.kv .k{{color:#7b857f;min-width:96px;flex-shrink:0}} .kv .v{{color:#2a3a36;word-break:break-word}}
</style></head><body><div class="wrap">
<h1>Instagram 红人筛选 · 交付表</h1>
<div class="meta">批次 {esc(meta.get('batch_id',''))} · {esc(meta.get('campaign_track',''))} · 生成 {esc(decisions.get('generated_at',''))} · 候选 {len(cands)} · 确认 Amazon 橱窗 {sf}</div>
<div class="cards">{cards}</div>
<div class="note"><b>阅读说明：</b>五池互斥，一人一池，『验收』色标区分。<b>购买意向评论分三级</b>——高(求链接/已下单)·中(考虑/问适用)·低(真诚产品热情，非水军)，均标明"谁说了什么"，点『看帖 ↗』核验。<b>每行下方『▸ 完整 Modash 画像』可展开</b>：真人/机器人拆解、点赞者画像(更难造假)、受众国家/城市/年龄/性别/语言/兴趣、跨平台账号、涨粉趋势、赞助帖样例——Modash 一次补数拿到的全部维度。『Modash 无』=Modash 本身没有该字段，『待补数』=尚未补。ER 对照：Modash（参考）+ IG 实算（近帖赞评，硬门槛）。</div>
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
