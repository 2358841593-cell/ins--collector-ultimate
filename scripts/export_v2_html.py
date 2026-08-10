#!/usr/bin/env python3
"""五池 HTML 表格交付（可读版，替代难看的 XLSX）。

对齐客户 SOP §8 字段；购买意向评论**分级**(高/中/低)带"谁说的"；能点的都点：
IG 主页 / 电商橱窗 / 有意图评论的帖子链接 / 合作品牌。自包含单文件 HTML，浏览器直开。

用法：python scripts/export_v2_html.py --decisions decisions.json --out deliverable.html
"""
from __future__ import annotations

import argparse
import html
import json
from collections import Counter
from pathlib import Path

from export_v2_comment_status import (
    comment_collection_complete,
    comment_unavailable_note,
)
from extensions.sop_v2 import comment_translation as translation_mod
from extensions.sop_v2 import feedback_taxonomy as feedback_taxonomy_mod
from extensions.sop_v2 import storefront as storefront_mod

POOLS = ["Include-With-Storefront", "Include-Without-Storefront",
         "Priority-Review", "Review", "Exclude"]
POOL_ZH = {"Include-With-Storefront": "纳入 · 有橱窗", "Include-Without-Storefront": "纳入 · 无橱窗",
           "Priority-Review": "优先复核", "Review": "待复核", "Exclude": "已排除"}
POOL_CLASS = {"Include-With-Storefront": "inc", "Include-Without-Storefront": "inc2",
              "Priority-Review": "pri", "Review": "rev", "Exclude": "exc"}
NICHE_ZH = {"skincare": "护肤", "beauty_device": "美容仪", "beauty_wellness": "美妆/健康",
            "lifestyle": "生活方式", "other": "其他"}
GRADE_CLASS = {"高": "g-hi", "中": "g-mid", "低": "g-lo"}

COLS = ["客户选择", "验收", "红人", "粉丝", "赛道", "购买意向评论（谁说了什么）", "电商橱窗 / 购物入口",
        "合作品牌", "赞助", "Fake%", "受众画像（第三方核验）", "ER 对照",
        "预估报价（USD）", "AI", "结论", "待补 / 原因"]


def _reason_tag_controls() -> str:
    """从唯一 taxonomy 合同生成拒绝原因多选，避免页面维护另一套标签。"""
    grouped: dict[str, list] = {}
    for item in feedback_taxonomy_mod.REASON_DEFINITIONS:
        grouped.setdefault(item.category, []).append(item)
    groups = []
    for category, items in grouped.items():
        choices = "".join(
            '<label class="ro">'
            f'<input class="rt" type="checkbox" value="{esc(item.code)}" '
            'onchange="saveD(this)">'
            f'<span>{esc(item.label_zh)}</span>'
            '</label>'
            for item in items
        )
        groups.append(
            f'<fieldset class="rg"><legend>{esc(category)}</legend>{choices}</fieldset>'
        )
    return "".join(groups)


def _decide_cell(c):
    """客户交互：三选、可选拒绝原因与反馈作用范围；状态存浏览器本地。"""
    h = (c.get("handle") or "").lstrip("@")
    return (f'<div class="dec" data-h="{esc(h)}" data-pool="{esc(c.get("final_pool",""))}" '
            f'data-score="{esc(c.get("ai_vetting_score"))}">'
            '<div class="dbtns">'
            "<button class=\"db yes\" onclick=\"mark(this,'合适')\">合适</button>"
            "<button class=\"db no\" onclick=\"mark(this,'不合适')\">不合适</button>"
            "<button class=\"db maybe\" onclick=\"mark(this,'待定')\">待定</button>"
            '</div>'
            '<div class="reason-panel">'
            '<details class="rp"><summary>选择拒绝原因（可选，可多选）</summary>'
            f'<div class="rgs">{_reason_tag_controls()}</div></details>'
            '<label class="sl"><span>本次不合适的范围</span>'
            '<select class="rs" onchange="saveD(this)">'
            '<option value="campaign">仅当前活动不合适（未来可重评）</option>'
            '<option value="temporary">暂时不合适（后续复核）</option>'
            '<option value="global">永久排除（所有后续轮次）</option>'
            '</select></label>'
            '<div class="scope-note">默认只影响当前活动；只有“永久排除”会进入永久负向库。</div>'
            '</div>'
            '<input class="dr" placeholder="补充说明（可选）…" oninput="saveD(this)">'
            '<label class="sl"><span>反馈作用范围</span>'
            '<select class="fs" onchange="saveD(this)">'
            '<option value="account">仅此账号</option>'
            '<option value="policy_signal">希望后续统一参考</option>'
            '</select></label>'
            '<div class="scope-note">“统一参考”只生成策略建议，不会自动变成全局规则。</div>'
            '</div>')


def esc(x):
    return html.escape(str(x)) if x is not None else ""


# 客户交互 JS：三选 + 结构化原因 + 作用范围存浏览器本地，一键导出 JSON 回传。
# 自包含、离线可用、无外部依赖；同时保留旧 JSON 的 verdict/reason/pool/score 字段。
_INTERACT_JS = """<script>
(function(){
  var B = window.__BATCH__ || 'batch';
  var TAXONOMY_VERSION = window.__FEEDBACK_TAXONOMY_VERSION__ || '';
  var CLS = {'合适':'yes','不合适':'no','待定':'maybe'};
  function key(h){ return 'dec_'+B+'_'+h; }
  function reasonTags(dec){
    return Array.from(dec.querySelectorAll('.rt:checked')).map(function(input){ return input.value; });
  }
  function feedbackScope(dec){
    var select = dec.querySelector('.fs'); return select ? select.value : 'account';
  }
  function rejectionScope(dec){
    var select = dec.querySelector('.rs');
    var value = select ? select.value : 'campaign';
    return value === 'global' || value === 'temporary' ? value : 'campaign';
  }
  function state(dec){
    return {verdict:dec.dataset.verdict||'', reason:dec.querySelector('.dr').value||'',
            reason_tags:reasonTags(dec), feedback_scope:feedbackScope(dec),
            rejection_scope:rejectionScope(dec)};
  }
  function save(dec){ localStorage.setItem(key(dec.dataset.h), JSON.stringify(state(dec))); }
  window.mark = function(btn, v){
    var dec = btn.closest('.dec');
    dec.querySelectorAll('.db').forEach(function(b){ b.classList.remove('on'); });
    if(dec.dataset.verdict === v){ dec.dataset.verdict=''; }   // 再点一次取消
    else { dec.dataset.verdict = v; btn.classList.add('on'); }
    save(dec);
    summary();
  };
  window.saveD = function(input){
    var dec = input.closest('.dec'); save(dec); summary();
  };
  window.saveR = window.saveD; // 兼容旧交付页的内联调用名称
  function restore(){
    document.querySelectorAll('.dec').forEach(function(dec){
      var raw = localStorage.getItem(key(dec.dataset.h)); if(!raw) return;
      var d; try{ d = JSON.parse(raw); }catch(e){ return; }
      if(d.verdict){ dec.dataset.verdict = d.verdict;
        var b = dec.querySelector('.db.'+CLS[d.verdict]); if(b) b.classList.add('on'); }
      if(d.reason) dec.querySelector('.dr').value = d.reason;
      if(Array.isArray(d.reason_tags)){
        dec.querySelectorAll('.rt').forEach(function(input){
          input.checked = d.reason_tags.indexOf(input.value) !== -1;
        });
      }
      var scope = d.feedback_scope === 'policy_signal' ? 'policy_signal' : 'account';
      var select = dec.querySelector('.fs'); if(select) select.value = scope;
      var rejection = d.rejection_scope === 'global' || d.rejection_scope === 'temporary'
        ? d.rejection_scope : 'campaign';
      var rejectionSelect = dec.querySelector('.rs');
      if(rejectionSelect) rejectionSelect.value = rejection;
    });
  }
  function summary(){
    var y=0,n=0,m=0,t=0,tagged=0;
    document.querySelectorAll('.dec').forEach(function(dec){ t++;
      var v=dec.dataset.verdict; if(v==='合适')y++; else if(v==='不合适'){ n++; if(reasonTags(dec).length) tagged++; }
      else if(v==='待定')m++; });
    document.getElementById('cstat').textContent =
      '✓合适 '+y+'  ✗不合适 '+n+'（已标原因 '+tagged+'）  待定 '+m+'  未选 '+(t-y-n-m)+' / 共 '+t;
  }
  window.exportDecisions = function(){
    var out = [];
    document.querySelectorAll('.dec').forEach(function(dec){
      var v = dec.dataset.verdict || '';
      var r = (dec.querySelector('.dr').value||'').trim();
      var tags = v === '不合适' ? reasonTags(dec) : [];
      if(v || r || tags.length) out.push({handle:dec.dataset.h, verdict:v, reason:r,
                           pool:dec.dataset.pool, score:dec.dataset.score,
                           reason_tags:tags, feedback_scope:feedbackScope(dec),
                           rejection_scope:v === '不合适' ? rejectionScope(dec) : null});
    });
    if(!out.length){ alert('还没做任何选择'); return; }
    var payload = {feedback_schema_version:2, taxonomy_version:TAXONOMY_VERSION,
                   batch:B, exported_at:new Date().toISOString(), decisions:out};
    var blob = new Blob([JSON.stringify(payload, null, 2)], {type:'application/json'});
    var a = document.createElement('a'); a.href = URL.createObjectURL(blob);
    a.download = 'client_decisions_'+B+'.json'; a.click();
    setTimeout(function(){ URL.revokeObjectURL(a.href); }, 0);
  };
  window.clearDecisions = function(){
    if(!confirm('清空本机所有选择？')) return;
    document.querySelectorAll('.dec').forEach(function(dec){ localStorage.removeItem(key(dec.dataset.h));
      dec.dataset.verdict=''; dec.querySelectorAll('.db').forEach(function(b){b.classList.remove('on');});
      dec.querySelectorAll('.rt').forEach(function(input){input.checked=false;});
      dec.querySelector('.dr').value=''; dec.querySelector('.fs').value='account';
      dec.querySelector('.rs').value='campaign'; });
    summary();
  };
  restore(); summary();
})();
</script>"""


def _na(c):
    # 有第三方报告但字段空 = 数据源本身没有；硬门槛已排除 = 按预算策略不付费；
    # 其余没报告才是真正待补数。
    if c.get("modash_report"):
        label = "数据源无"
    elif c.get("final_pool") == "Exclude":
        label = "未补（已按硬门槛排除）"
    else:
        label = "待补数"
    return f'<span class="muted">{label}</span>'


def _brand_cell(c):
    brands = c.get("brand_collaborations") or []
    if brands:
        return "、".join(esc(b) for b in brands[:6])
    if c.get("modash_report"):
        return '<span class="muted">数据源未识别</span>'
    if c.get("final_pool") == "Exclude":
        return '<span class="muted">未补（已按硬门槛排除）</span>'
    return '<span class="muted">待补数</span>'


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


def _fmt_count(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    return f"{number:,.0f}" if number.is_integer() else f"{number:,.2f}"


def _fmt_usd(value):
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return "—"


def _pricing_cell(c):
    """展示型估价：绝不冒充博主实际报价。"""
    p = c.get("pricing_estimate") or {}
    status = p.get("status") or "missing"
    quote = p.get("quote_usd") or {}
    avg = p.get("average_plays")
    population = p.get("population_evidence") or {}
    if status == "not_applicable_no_reels":
        if population.get("proof_mode") == "reels_surface_absent":
            proof = (
                "Reels 入口不存在：两次访问 /reels 均回到同账号健康主页，"
                "页面有普通帖子但无 Reels Tab / Reel 链接"
            )
        else:
            proof = "Reels Tab 已穷尽且显示明确空态"
        return (
            f'<span class="muted">不适用：{proof}</span>'
            '<div class="aud">报价留空；未使用第三方/总播放/Facebook 数据</div>'
        )
    if avg is None or quote.get("default") is None:
        return (
            '<span class="muted">待补近 10 条非置顶 Reels 播放量</span>'
            '<div class="aud">Reels Tab 穷尽或原生指标完整性尚未证明</div>'
        )

    sample = int(p.get("sample_count") or 0)
    requested = int(p.get("requested_reels") or 10)
    low = quote.get("min")
    high = quote.get("max")
    if status == "complete":
        source = f"IG 近 {requested} 条非置顶 Reels"
    elif status == "complete_available":
        source = (
            f'<span class="ok">IG 全部可用 Reels {sample}/{requested}；'
            f'Tab 已穷尽（连续 {int(population.get("stable_bottom_rounds") or 0)}'
            ' 轮到底无增长）</span>'
        )
    elif status == "partial":
        source = (
            f'<span class="warn">IG 样本 {sample}/{requested}，暂估；'
            'Reels Tab 穷尽未证明</span>'
        )
    elif status == "fallback_modash":
        source = '<span class="warn">第三方均播替代，未验证近 10 条/置顶</span>'
    else:
        source = '<span class="warn">口径待核验</span>'
    return (
        f'<b>{_fmt_usd(quote.get("default"))}</b>'
        f'<div class="aud">区间 {_fmt_usd(low)}–{_fmt_usd(high)}</div>'
        f'<div class="aud">均播 {_fmt_count(avg)} · {source}</div>'
        '<div class="aud muted">按 CPM $35（区间 $35–40）估算，非实际报价</div>'
    )


def _pricing_detail_block(c):
    p = c.get("pricing_estimate") or {}
    if not p:
        return ""
    q = p.get("quote_usd") or {}
    rows = [
        _kv("性质", "展示型估算，非博主实际报价；不参与评分或路由"),
        _kv(
            "公式",
            f'{_fmt_count(p.get("average_plays"))} ÷ 1,000 × CPM '
            f'${esc((p.get("cpm_usd") or {}).get("default", 35))}',
        ),
        _kv(
            "结果",
            f'默认 {_fmt_usd(q.get("default"))} · 区间 '
            f'{_fmt_usd(q.get("min"))}–{_fmt_usd(q.get("max"))}',
        ),
        _kv(
            "样本状态",
            f'{esc(p.get("status") or "missing")} · '
            f'{int(p.get("sample_count") or 0)}/{int(p.get("requested_reels") or 10)} · '
            f'{esc(p.get("source") or "missing")}',
        ),
        _kv(
            "总体口径",
            f'{esc(p.get("population_basis") or "unproven")} · '
            f'Reels Tab 穷尽={"是" if (p.get("population_evidence") or {}).get("reels_tab_exhausted") else "否"} · '
            f'总体完整={"是" if (p.get("population_evidence") or {}).get("population_complete") else "否"}',
        ),
    ]
    reels = p.get("reels") or []
    if reels:
        links = []
        for i, reel in enumerate(reels[:10], 1):
            label = f'{i}. {_fmt_count(reel.get("play_count"))} 播放'
            if reel.get("taken_at"):
                label += f' · {esc(reel["taken_at"])}'
            if reel.get("url"):
                links.append(f'<a href="{esc(reel["url"])}" target="_blank">{label} ↗</a>')
            else:
                links.append(label)
        rows.append(_kv("Reels 明细", "<br>".join(links)))
    elif p.get("status") == "fallback_modash":
        rows.append(_kv("限制", "历史第三方账号级 Reels 均播；当前正式报价口径禁止使用，B3 必须阻断并补原生证据。"))
    elif p.get("status") == "not_applicable_no_reels":
        population = p.get("population_evidence") or {}
        proof = (
            "两次独立访问 /reels 均重定向同账号健康主页；主页有普通帖子，"
            "但没有 Reels Tab 或 Reel 链接"
            if population.get("proof_mode") == "reels_surface_absent"
            else "Reels Tab 已到底并连续两轮无增长，且显示明确空态"
        )
        rows.append(_kv("不适用证据", f"{proof}；报价为空。"))
    elif p.get("status") == "missing":
        rows.append(_kv("待补", "未取得足够的 Reels 播放量，未生成报价。"))
    return '<div class="grp"><div class="gt">预估报价证据与口径</div>' + "".join(rows) + '</div>'


def _detail_panel(c, ncols):
    """每个红人的完整受众画像（可折叠）：一次 credit 拿到的全部维度都铺出来。"""
    h = (c.get("handle") or "").lstrip("@")
    blocks = []
    pricing_block = _pricing_detail_block(c)
    if pricing_block:
        blocks.append(pricing_block)
    if not c.get("modash_report"):
        if c.get("final_pool") == "Exclude":
            note = "此红人已按非第三方硬门槛排除，因此未消耗第三方报告额度。"
        else:
            note = "此红人尚未做第三方受众核验（待补数）。"
        blocks.append(f'<div class="dl-note">{note}</div>')
        inner = "".join(blocks)
        return (f'<tr class="det"><td colspan="{ncols}"><details><summary>▸ 完整受众画像 · @{esc(h)}</summary>'
                f'<div class="dl">{inner}</div></details></td></tr>')

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
        stat.append(f'第三方 Reels均播 {c["avg_reels_plays"]:,}')
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

    inner = "".join(blocks) or '<div class="dl-note">第三方受众报告已拉取，但明细为空（数据源无此号画像数据）。</div>'
    return (f'<tr class="det"><td colspan="{ncols}"><details><summary>▸ 完整受众画像 · @{esc(h)}'
            f'</summary><div class="dl">{inner}</div></details></td></tr>')


def _intent_cell(c):
    snips = c.get("high_intent_snippets") or []
    posts = c.get("intent_posts") or []
    translated_rows = translation_mod.delivery_evidence_rows(c, limit=6)
    translated_intent = bool(
        translated_rows and translated_rows[0].get("evidence_kind") == "intent"
    )
    post_url = (
        translated_rows[0].get("post_url")
        if translated_rows and translated_rows[0].get("post_url")
        else posts[0].get("post_url") if posts else None
    )
    g = c.get("intent_by_grade") or {}
    if translated_intent:
        g = c.get("translated_intent_by_grade") or g
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
    translation_note = translation_mod.delivery_translation_note(c)
    if snips or translated_intent:
        rows = []
        if translated_intent:
            for row in translated_rows:
                grade_zh = row.get("grade_zh") or "—"
                cls = GRADE_CLASS.get(grade_zh, "g-lo")
                who = f"@{row['username']}（{grade_zh}）" if row.get("username") else f"评论（{grade_zh}）"
                if row.get("status") == "translated" and row.get("translated_zh"):
                    rows.append(
                        f'<div class="cmt {cls}"><b>{esc(who)}</b>: {esc(row["translated_zh"])}'
                        f'<div class="tr-original">原文 [{esc(row.get("source_language") or "und")}]：'
                        f'{esc(row.get("original_text"))}</div></div>'
                    )
                else:
                    rows.append(
                        f'<div class="cmt {cls}"><b>{esc(who)}</b>: {esc(row.get("original_text"))}'
                        '<div class="warn">中文翻译失败 · 原文已保留</div></div>'
                    )
        else:
            for s in snips[:6]:
                # 兼容尚未执行离线翻译的历史交付。
                cls = "g-lo"
                for zh, cl in GRADE_CLASS.items():
                    if f"（{zh}）" in s:
                        cls = cl
                rows.append(f'<div class="cmt {cls}">{esc(s)}</div>')
        head = (f'<div class="tier">高{g.get("high",0)} 中{g.get("medium",0)} 低{g.get("low",0)}'
                + (f' · <a href="{esc(post_url)}" target="_blank">看帖 ↗</a>' if post_url else '') + '</div>')
        notes = []
        if completion_note:
            notes.append(
                f'<div class="{"muted" if deep_complete else "warn"}">'
                f'{esc(completion_note)}</div>'
            )
        if translation_note:
            notes.append(f'<div class="tier">{esc(translation_note)}</div>')
        note = "".join(notes)
        return note + head + "".join(rows)
    if completion_note:
        return f'<span class="muted">{esc(completion_note)}</span>'
    # 无意图片段：区分四种性质不同的情况，别把"我们抽取失败"甩锅成"账号受限"
    if not c.get("comments_read"):
        return '<span class="muted">评论待采集（深采未完成）</span>'
    if (c.get("comments_analyzed") or 0) == 0:
        return '<span class="warn">评论抽取失败（待复采）</span>'   # 深采跑了但一条没抽到 = 系统侧待修
    if (vc or 0) < 20:
        return f'<span class="muted">样本偏少（{vc} 条，待补采）</span>'
    base = f'<span class="muted">无明显购买意向（有效评论 {vc} 条）</span>'
    if translated_rows:
        samples = []
        for row in translated_rows[:3]:
            if row.get("status") == "translated" and row.get("translated_zh"):
                samples.append(
                    f'<div class="cmt"><b>评论样本：</b>{esc(row["translated_zh"])}'
                    f'<div class="tr-original">原文 [{esc(row.get("source_language") or "und")}]：'
                    f'{esc(row.get("original_text"))}</div></div>'
                )
            else:
                samples.append(
                    f'<div class="cmt"><b>评论原文：</b>{esc(row.get("original_text"))}'
                    '<div class="warn">中文翻译失败 · 原文已保留</div></div>'
                )
        note = f'<div class="tier">{esc(translation_note)}</div>' if translation_note else ""
        return base + note + "".join(samples)
    if translation_note:
        return base + f'<div class="warn">{esc(translation_note)}</div>'
    if c.get("comment_sample"):
        return base + '<div class="warn">中文翻译尚未执行（原始评论已保留）</div>'
    return base


def _storefront_cell(c):
    """Amazon 优先；否则展示实际 LTK/ShopMy/自营店/购物聚合入口。"""
    st = storefront_mod.effective_status(c)
    su = storefront_mod.storefront_url(c)
    stype = storefront_mod.storefront_type(c)
    if st == "confirmed_yes" and su:
        return f'<a href="{esc(su)}" target="_blank">{esc(stype or "打开橱窗")} ↗</a>'
    # 仍给未归类的 bio 链作人工核验入口，但不把普通网页冒充成已确认橱窗。
    bl = c.get("bio_links") or []
    if bl:
        return f'<a href="{esc(bl[0])}" target="_blank">Bio 链接（橱窗未确认）↗</a>'
    return {"confirmed_no": '<span class="muted">确认无橱窗</span>',
            "unknown": '<span class="muted">未确认</span>'}.get(st, '<span class="muted">—</span>')


def _typical_er(c):
    """中位 ER（硬门槛依据）：优先用已存 real_er_median，旧数据从 sampled_posts 现算。"""
    if c.get("real_er_median") is not None:
        return c.get("real_er_median")
    try:
        from extensions.sop_v2.content import median_er
    except Exception:  # noqa: BLE001
        return None
    return median_er(c.get("sampled_posts"), c.get("follower_count"))


def _er_cell(c):
    """ER 对照：中位=门槛依据（平常真实互动），均值=参考（含爆款触达）。客户 2026-07-17 口径。
    中位缺失（拿不到帖子赞评）→ 本轮以 Modash general_er 为准。"""
    mo = c.get("modash_er") if c.get("modash_er") is not None else c.get("general_er")
    mean = c.get("ig_er") if c.get("ig_er") is not None else c.get("real_er")
    med = _typical_er(c)
    parts = [f"第三方 {mo}%" if mo is not None else "第三方 —"]
    if med is None and mean is None:
        # 中位/均值都拿不到 → 用 Modash 为准
        if mo is not None:
            parts[-1] = f'<b>第三方 {mo}%（本轮为准）</b>'
            return " / ".join(parts)
        parts.append('<span class="muted">IG 待读</span>')
        return " / ".join(parts)
    if med is not None:
        parts.append(f'<b>IG实算(中位) {med}%</b>')      # 门槛依据
    out = " / ".join(parts)
    if mean is not None:
        out += f'<div class="aud">均值 {mean}%（参考）'
        # 均值远高于中位 = 少数爆款撑起来的：触达强，但平常互动看中位
        if med is not None and mean > 100 and mean > med * 3:
            out += ' · <span class="warn">爆款外溢</span>：Reels 触达远超粉丝，平常水平看中位'
        out += '</div>'
    return out


def _row(c):
    h = (c.get("handle") or "").lstrip("@")
    prof = c.get("profile_url") or f"https://www.instagram.com/{h}/"
    pool = c.get("final_pool", "Review")
    reasons = (c.get("review_reasons_text") or []) + (c.get("exclude_reasons_text") or [])
    cells = [
        _decide_cell(c),
        f'<span class="badge {POOL_CLASS.get(pool,"rev")}">{esc(POOL_ZH.get(pool,pool))}</span>',
        f'<a href="{esc(prof)}" target="_blank">@{esc(h)}</a><div class="sub">{esc(c.get("full_name") or "")}</div>',
        esc(f'{(c.get("follower_count") or 0):,}') if c.get("follower_count") else "—",
        esc(NICHE_ZH.get(c.get("core_niche_key"), c.get("core_niche_key") or "—")),
        _intent_cell(c),
        _storefront_cell(c),
        _brand_cell(c),
        (f'{c.get("sponsorship_saturation")}%' if c.get("sponsorship_saturation") is not None else '<span class="muted">—</span>'),
        _fake_cell(c),
        _audience_cell(c),
        _er_cell(c),
        _pricing_cell(c),
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
    sf = sum(1 for c in cands if storefront_mod.has_storefront(c))

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
.wrap{{max-width:1560px;margin:0 auto;padding:28px 20px 84px}}
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
.tr-original{{color:#7b857f;font-size:11px;margin-top:2px;white-space:pre-wrap}}
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
.dec{{display:flex;flex-direction:column;gap:4px;min-width:100px}}
.dbtns{{display:flex;gap:3px}}
.db{{cursor:pointer;border:1px solid #d3dbd8;background:#fff;border-radius:6px;padding:3px 4px;font-size:11px;color:#41504c;flex:1}}
.db:hover{{background:#f0f4f2}} .db.on{{color:#fff;font-weight:600}}
.db.yes.on{{background:#1e7a47;border-color:#1e7a47}}
.db.no.on{{background:#c0554f;border-color:#c0554f}}
.db.maybe.on{{background:#b98b2e;border-color:#b98b2e}}
.dr{{border:1px solid #e0e6e3;border-radius:6px;padding:3px 6px;font-size:11px;width:100%}}
.reason-panel{{display:none}}
.dec[data-verdict="不合适"] .reason-panel{{display:block}}
.rp{{border:1px solid #ead7d5;border-radius:6px;background:#fff9f8}}
.rp summary{{cursor:pointer;color:#9a423d;font-size:11px;padding:4px 6px;user-select:none}}
.rgs{{max-height:300px;overflow:auto;padding:4px 6px 7px;min-width:260px}}
.rg{{border:0;border-top:1px solid #f0dfdd;margin:4px 0 0;padding:4px 0 0}}
.rg legend{{font-size:10px;color:#8b6c68;padding:0 4px}}
.ro{{display:flex;gap:5px;align-items:flex-start;font-size:11px;padding:2px 0;cursor:pointer}}
.ro input{{margin:2px 0 0;flex-shrink:0}}
.sl{{display:flex;flex-direction:column;gap:2px;color:#68736f;font-size:10px}}
.fs,.rs{{border:1px solid #e0e6e3;border-radius:6px;padding:3px 4px;background:#fff;color:#41504c;font-size:11px;width:100%}}
.scope-note{{color:#8a9490;font-size:9.5px;line-height:1.35}}
tr:has(.db.yes.on) td{{background:#f2fbf6!important}}
tr:has(.db.no.on) td{{background:#fdf5f4!important}}
#cbar{{position:fixed;left:0;right:0;bottom:0;background:#1f4e4a;color:#fff;display:flex;justify-content:space-between;align-items:center;gap:12px;padding:10px 22px;font-size:13px;z-index:200;box-shadow:0 -2px 10px rgba(0,0,0,.18);flex-wrap:wrap}}
#cbar .r{{display:flex;gap:10px;align-items:center}}
#cbar button{{background:#3a9d6a;color:#fff;border:0;border-radius:8px;padding:8px 16px;font-size:13px;font-weight:600;cursor:pointer}}
#cbar button:hover{{background:#2f8659}} #cbar button.ghost{{background:transparent;border:1px solid #5c8478}}
</style></head><body><div class="wrap">
<h1>Instagram 红人筛选 · 交付表</h1>
<div class="meta">批次 {esc(meta.get('batch_id',''))} · {esc(meta.get('campaign_track',''))} · 生成 {esc(decisions.get('generated_at',''))} · 候选 {len(cands)} · 确认有电商橱窗/购物入口 {sf}</div>
<div class="cards">{cards}</div>
<div class="note"><b>如何使用（客户）：</b>最左列『客户选择』直接点 <b>合适 / 不合适 / 待定</b>。选择“不合适”后可多选结构化拒绝原因，也可填写补充说明；<b>原因不是必填项</b>，但填写后才能用于后续策略分析。拒绝范围默认『仅当前活动不合适』，未来仍可重新评估；只有客户明确选择『永久排除』，账号才会进入所有后续轮次共用的永久负向库。『反馈作用范围』默认仅记录此账号；选择『希望后续统一参考』只会生成策略建议，<b>不会自动修改全局规则</b>。所有选择<b>自动存本机浏览器</b>（关页不丢，随时接着选）。选完点底部 <b>⬇ 导出客户决策</b> 下载一个 JSON 文件，<b>回传给我们</b>即可——我们据此更新入选/排除。<br><b>阅读说明：</b>五池互斥，一人一池。<b>购买意向评论分三级</b>——高(求链接/已下单)·中(考虑/问适用)·低(真诚产品热情，非水军)，标明"谁说了什么"，点『看帖 ↗』核验。<b>每行下方『▸ 完整受众画像』可展开</b>：真人/机器人拆解、点赞者画像、受众国家/年龄/性别/语言、跨平台、涨粉、赞助帖（接入权威第三方受众数据源交叉核验）。<b>橱窗</b>：Amazon、LTK、ShopMy、自营店和购物聚合入口都计入；确认无橱窗也不在浅扫阶段直接淘汰。ER：第三方受众数据(参考) + IG 实算中位(门槛依据，抗爆款)。<b>预估报价</b>：先排除置顶 Reels，再取最近 10 条的平均播放量，按 CPM $35 估算并给出 $35–40 区间；样本不足或使用第三方均播时会明确标记。该数值仅供预算参考，<b>不是博主实际报价，也不参与评分/路由</b>。</div>
{''.join(sections)}
</div>
<div id="cbar"><span id="cstat"></span><div class="r"><button class="ghost" onclick="clearDecisions()">清空</button><button onclick="exportDecisions()">⬇ 导出客户决策</button></div></div>
<script>window.__BATCH__={json.dumps(meta.get('batch_id',''))};window.__FEEDBACK_TAXONOMY_VERSION__={json.dumps(feedback_taxonomy_mod.TAXONOMY_VERSION)};</script>
{_INTERACT_JS}
</body></html>"""


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
