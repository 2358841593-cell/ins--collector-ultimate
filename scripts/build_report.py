#!/usr/bin/env python3
"""把 discovery-*.json 渲染成自包含 HTML 仪表盘。

视觉系统：Linear（awesome-design-md/linear.app/DESIGN.md）
  - 深色 canvas #010102 + 四级 surface 阶梯
  - 单一 lavender-blue 强调色 #5e6ad2
  - 系统字体栈（macOS 上即 SF Pro = Linear 官方 fallback），零外部依赖
设计纪律：taste-skill（反 AI-slop：无紫色渐变、色彩锁定、真实交互态、圆角统一）

用法：
    .venv/bin/python scripts/build_report.py             # 用最新一次 run
    .venv/bin/python scripts/build_report.py --run data/runs/discovery-XXXX.json
输出：data/runs/report-{run_id}.html（双击即可打开，无需服务器）
"""
from __future__ import annotations

import argparse
import base64
import glob
import json
import mimetypes
import os
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNS_DIR = ROOT / "data" / "runs"


def attach_verifications(data: dict, run_id: str) -> int:
    """载入 verifications-{run_id}.json，把核验记录 + base64 截图挂到对应候选上。

    返回挂上核验记录的候选数。截图 base64 内联，保证报告单文件自包含。
    """
    vpath = RUNS_DIR / f"verifications-{run_id}.json"
    if not vpath.exists():
        return 0
    vdata = json.loads(vpath.read_text(encoding="utf-8"))
    by_handle = vdata.get("candidates", {})
    cache: dict[str, str] = {}

    def b64(rel: str) -> str:
        if not rel:
            return ""
        if rel in cache:
            return cache[rel]
        fp = RUNS_DIR / rel
        if not fp.exists():
            cache[rel] = ""
            return ""
        mime = mimetypes.guess_type(str(fp))[0] or "image/png"
        uri = f"data:{mime};base64," + base64.b64encode(fp.read_bytes()).decode()
        cache[rel] = uri
        return uri

    count = 0
    for cand in data.get("candidates", []):
        rec = by_handle.get(cand.get("handle"))
        if not rec:
            continue
        checks = []
        for chk in rec.get("checks", []):
            ev = dict(chk.get("evidence", {}))
            ev["screenshot_b64"] = b64(ev.get("screenshot", ""))
            checks.append({**chk, "evidence": ev})
        cand["_verify"] = checks
        cand["_verified_at"] = rec.get("verified_at", "")
        count += 1
    return count


def latest_run() -> str:
    files = sorted(glob.glob(str(RUNS_DIR / "discovery-*.json")), key=os.path.getmtime)
    if not files:
        raise SystemExit("没有找到 discovery-*.json，请先跑一次 discover.py")
    return files[-1]


# ─── CSS（Linear 设计 tokens）──────────────────────────────────────────────────

CSS = r"""
:root {
  /* Linear 深色 canvas 体系 */
  --canvas: #010102;
  --surface-1: #0f1011;
  --surface-2: #141516;
  --surface-3: #18191a;
  --hairline: #23252a;
  --hairline-strong: #34343a;
  --ink: #f7f8f8;
  --ink-muted: #d0d6e0;
  --ink-subtle: #8a8f98;
  --ink-tertiary: #62666d;
  /* 单一强调色 + 语义状态色（仅用于徽章/指示，不做区块填充）*/
  --primary: #5e6ad2;
  --primary-hover: #828fff;
  --primary-dim: rgba(94,106,210,0.14);
  --success: #27a644;
  --success-dim: rgba(39,166,68,0.13);
  --warn: #d9a23b;
  --warn-dim: rgba(217,162,59,0.13);
  --danger: #c4453b;
  --danger-dim: rgba(196,69,59,0.12);
  /* 圆角 */
  --r-xs: 4px; --r-sm: 6px; --r-md: 8px; --r-lg: 12px; --r-xl: 16px; --r-pill: 9999px;
  /* 字体 */
  --font: -apple-system, BlinkMacSystemFont, "SF Pro Display", "SF Pro Text", "Inter", system-ui, sans-serif;
  --mono: "SF Mono", ui-monospace, "JetBrains Mono", Menlo, monospace;
}

* { box-sizing: border-box; margin: 0; padding: 0; }
html { -webkit-font-smoothing: antialiased; text-rendering: optimizeLegibility; }
body {
  background: var(--canvas);
  color: var(--ink);
  font-family: var(--font);
  font-size: 14px;
  line-height: 1.5;
  letter-spacing: -0.05px;
}
a { color: inherit; text-decoration: none; }
::selection { background: var(--primary-dim); }

/* 滚动条 */
::-webkit-scrollbar { width: 10px; height: 10px; }
::-webkit-scrollbar-thumb { background: var(--hairline-strong); border-radius: var(--r-pill); border: 2px solid var(--canvas); }
::-webkit-scrollbar-track { background: transparent; }

/* ── 顶部导航 ── */
.topnav {
  position: sticky; top: 0; z-index: 50;
  height: 56px; display: flex; align-items: center; gap: 12px;
  padding: 0 24px;
  background: rgba(1,1,2,0.82); backdrop-filter: blur(12px);
  border-bottom: 1px solid var(--hairline);
}
.brand { display: flex; align-items: center; gap: 9px; font-weight: 600; letter-spacing: -0.3px; }
.brand .mark { width: 16px; height: 16px; border-radius: var(--r-xs); background: var(--primary);
  box-shadow: 0 0 14px rgba(94,106,210,0.55); transform: rotate(45deg); }
.topnav .spacer { flex: 1; }
.topnav .meta { color: var(--ink-subtle); font-size: 12px; font-family: var(--mono); }
.topnav .meta b { color: var(--ink-muted); font-weight: 500; }

/* ── 容器 ── */
.wrap { max-width: 1280px; margin: 0 auto; padding: 40px 24px 80px; }

/* ── 页头 ── */
.head { margin-bottom: 32px; }
.eyebrow { font-size: 13px; font-weight: 500; letter-spacing: 0.4px; color: var(--primary);
  text-transform: uppercase; margin-bottom: 12px; }
.head h1 { font-size: 40px; font-weight: 600; line-height: 1.12; letter-spacing: -1.4px; }
.head p { color: var(--ink-subtle); font-size: 16px; margin-top: 10px; max-width: 65ch; }
.head .runline { margin-top: 16px; display: flex; flex-wrap: wrap; gap: 8px 18px;
  font-family: var(--mono); font-size: 12px; color: var(--ink-tertiary); }
.head .runline b { color: var(--ink-subtle); font-weight: 400; }

/* ── 统计卡片 ── */
.stats { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 12px; }
.stat {
  background: var(--surface-1); border: 1px solid var(--hairline); border-radius: var(--r-lg);
  padding: 20px; position: relative; overflow: hidden;
  transition: border-color .18s ease, transform .18s ease;
}
.stat::before { content:""; position:absolute; top:0; left:0; width:100%; height:2px; opacity:.7; }
.stat.s-total::before { background: var(--ink-tertiary); }
.stat.s-include::before { background: var(--success); }
.stat.s-review::before { background: var(--warn); }
.stat.s-exclude::before { background: var(--ink-tertiary); }
.stat .n { font-size: 34px; font-weight: 600; letter-spacing: -1.4px; line-height: 1; }
.stat .l { color: var(--ink-subtle); font-size: 12.5px; margin-top: 8px; }
.stat .sub { color: var(--ink-tertiary); font-size: 11.5px; margin-top: 3px; font-family: var(--mono); }
.stat.s-include .n { color: #4ad06c; }
.stat.s-review .n { color: var(--warn); }

/* ── 漏斗分解 ── */
.panel { background: var(--surface-1); border: 1px solid var(--hairline); border-radius: var(--r-lg);
  padding: 18px 20px; margin-bottom: 28px; }
.panel h3 { font-size: 12px; font-weight: 500; color: var(--ink-subtle); letter-spacing: 0.3px;
  text-transform: uppercase; margin-bottom: 14px; }
.funnelbar { display: flex; height: 8px; border-radius: var(--r-pill); overflow: hidden;
  background: var(--surface-3); margin-bottom: 14px; }
.funnelbar span { height: 100%; }
.funnel-legend { display: flex; flex-wrap: wrap; gap: 6px 16px; }
.fl { display: flex; align-items: center; gap: 7px; font-size: 12.5px; color: var(--ink-muted); }
.fl .dot { width: 8px; height: 8px; border-radius: 2px; }
.fl .c { color: var(--ink-tertiary); font-family: var(--mono); font-size: 11.5px; }

/* ── 工具栏 ── */
.toolbar { display: flex; align-items: center; gap: 12px; margin-bottom: 16px; flex-wrap: wrap; }
.tabs { display: flex; gap: 4px; background: var(--surface-1); border: 1px solid var(--hairline);
  border-radius: var(--r-pill); padding: 3px; }
.tab { padding: 6px 14px; border-radius: var(--r-pill); font-size: 13px; font-weight: 500;
  color: var(--ink-subtle); cursor: pointer; border: none; background: transparent; transition: all .15s ease; }
.tab:hover { color: var(--ink); }
.tab.active { background: var(--surface-3); color: var(--ink); }
.tab .badge { font-family: var(--mono); font-size: 11px; opacity: .65; margin-left: 5px; }
.toolbar .spacer { flex: 1; }
.search { display: flex; align-items: center; gap: 8px; background: var(--surface-1);
  border: 1px solid var(--hairline); border-radius: var(--r-md); padding: 7px 12px; min-width: 220px; }
.search:focus-within { border-color: var(--hairline-strong); }
.search svg { color: var(--ink-tertiary); flex-shrink: 0; }
.search input { background: transparent; border: none; outline: none; color: var(--ink);
  font-family: var(--font); font-size: 13px; width: 100%; }
.search input::placeholder { color: var(--ink-tertiary); }
.sortsel { background: var(--surface-1); border: 1px solid var(--hairline); border-radius: var(--r-md);
  color: var(--ink-muted); font-family: var(--font); font-size: 13px; padding: 7px 10px; cursor: pointer; outline: none; }

/* ── 表格 ── */
.tablecard { background: var(--surface-1); border: 1px solid var(--hairline); border-radius: var(--r-lg); overflow: hidden; }
table { width: 100%; border-collapse: collapse; }
thead th { text-align: left; font-size: 11.5px; font-weight: 500; color: var(--ink-subtle);
  text-transform: uppercase; letter-spacing: 0.3px; padding: 12px 16px;
  border-bottom: 1px solid var(--hairline); white-space: nowrap; user-select: none; }
thead th.num { text-align: right; }
thead th.sortable { cursor: pointer; }
thead th.sortable:hover { color: var(--ink); }
thead th .arrow { opacity: .5; font-size: 9px; margin-left: 3px; }
tbody tr { border-bottom: 1px solid var(--hairline); cursor: pointer; transition: background .12s ease; }
tbody tr:last-child { border-bottom: none; }
tbody tr:hover { background: var(--surface-2); }
tbody td { padding: 13px 16px; vertical-align: middle; white-space: nowrap; }
td.num { text-align: right; font-family: var(--mono); font-size: 12.5px; }

.handle { display: flex; align-items: center; gap: 10px; }
.handle .av { width: 30px; height: 30px; border-radius: var(--r-pill); flex-shrink: 0;
  display: grid; place-items: center; font-weight: 600; font-size: 12px; color: var(--canvas);
  background: linear-gradient(135deg, #828fff, #5e6ad2); }
.handle .hn { font-weight: 500; }
.handle .fn { color: var(--ink-tertiary); font-size: 11.5px; max-width: 180px; overflow: hidden;
  text-overflow: ellipsis; white-space: nowrap; }

.badge { display: inline-flex; align-items: center; gap: 5px; padding: 2px 9px; border-radius: var(--r-pill);
  font-size: 11.5px; font-weight: 500; font-family: var(--mono); letter-spacing: 0; }
.b-include { background: var(--success-dim); color: #4ad06c; }
.b-review { background: var(--warn-dim); color: var(--warn); }
.b-exclude { background: rgba(98,102,109,0.16); color: var(--ink-subtle); }
.b-error { background: var(--danger-dim); color: #e0695f; }
.badge .d { width: 6px; height: 6px; border-radius: var(--r-pill); background: currentColor; }

.chip { font-family: var(--mono); font-size: 11.5px; padding: 1px 7px; border-radius: var(--r-sm);
  background: var(--surface-3); color: var(--ink-muted); }
.az-yes { color: #4ad06c; } .az-unv { color: var(--warn); } .az-no { color: var(--ink-tertiary); }
.arch-amazon_finds { color: var(--primary-hover); font-weight: 500; }
.arch-mixed { color: var(--ink-muted); }
.arch-lifestyle { color: var(--ink-subtle); }
.trust-high { color: #4ad06c; } .trust-medium { color: var(--ink-muted); } .trust-low { color: var(--ink-subtle); }
.trust-unknown { color: var(--ink-tertiary); }

.scorecell { display: inline-flex; align-items: center; gap: 8px; justify-content: flex-end; }
.scorebar { width: 42px; height: 4px; border-radius: var(--r-pill); background: var(--surface-3); overflow: hidden; }
.scorebar i { display: block; height: 100%; background: linear-gradient(90deg,#5e6ad2,#828fff); }
.scoreval { font-weight: 600; min-width: 34px; text-align: right; }

.empty { padding: 60px 20px; text-align: center; color: var(--ink-tertiary); }

/* ── 详情抽屉 ── */
.scrim { position: fixed; inset: 0; background: rgba(0,0,0,0.55); opacity: 0; pointer-events: none;
  transition: opacity .22s ease; z-index: 60; }
.scrim.open { opacity: 1; pointer-events: auto; }
.drawer { position: fixed; top: 0; right: 0; height: 100%; width: 480px; max-width: 92vw; z-index: 70;
  background: var(--surface-1); border-left: 1px solid var(--hairline-strong);
  transform: translateX(100%); transition: transform .26s cubic-bezier(0.16,1,0.3,1);
  display: flex; flex-direction: column; }
.drawer.open { transform: translateX(0); }
.drawer-head { padding: 22px 24px 18px; border-bottom: 1px solid var(--hairline); }
.drawer-head .top { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
.drawer-head .who { display: flex; align-items: center; gap: 12px; }
.drawer-head .av { width: 42px; height: 42px; border-radius: var(--r-pill); display: grid; place-items: center;
  font-weight: 600; color: var(--canvas); background: linear-gradient(135deg,#828fff,#5e6ad2); }
.drawer-head h2 { font-size: 19px; font-weight: 600; letter-spacing: -0.4px; }
.drawer-head .fn { color: var(--ink-subtle); font-size: 12.5px; margin-top: 1px; }
.xbtn { background: var(--surface-3); border: 1px solid var(--hairline); color: var(--ink-subtle);
  width: 30px; height: 30px; border-radius: var(--r-md); cursor: pointer; font-size: 16px; line-height: 1;
  display: grid; place-items: center; flex-shrink: 0; transition: all .15s; }
.xbtn:hover { color: var(--ink); border-color: var(--hairline-strong); }
.drawer-head .pillrow { display: flex; flex-wrap: wrap; gap: 7px; margin-top: 14px; }
.drawer-head a.viewig { display:inline-flex; align-items:center; gap:6px; margin-top:14px;
  font-size: 12.5px; color: var(--primary-hover); font-family: var(--mono); }
.drawer-head a.viewig:hover { text-decoration: underline; }
.drawer-body { overflow-y: auto; padding: 8px 24px 32px; flex: 1; }

.sec { padding: 18px 0; border-bottom: 1px solid var(--hairline); }
.sec:last-child { border-bottom: none; }
.sec h4 { font-size: 11px; font-weight: 500; color: var(--ink-subtle); text-transform: uppercase;
  letter-spacing: 0.4px; margin-bottom: 12px; }
.kv { display: flex; justify-content: space-between; align-items: baseline; gap: 16px; padding: 5px 0; font-size: 13px; }
.kv .k { color: var(--ink-subtle); }
.kv .v { color: var(--ink); font-family: var(--mono); font-size: 12.5px; text-align: right; }
.kv .v.wrap { font-family: var(--font); text-align: right; }

.metric { display:grid; grid-template-columns:1fr 1fr; gap:10px; }
.metric .m { background: var(--surface-2); border:1px solid var(--hairline); border-radius: var(--r-md); padding: 12px; }
.metric .m .mn { font-size: 20px; font-weight: 600; letter-spacing:-0.6px; }
.metric .m .ml { font-size: 11px; color: var(--ink-subtle); margin-top: 3px; }
.barline { height: 6px; border-radius: var(--r-pill); background: var(--surface-3); overflow:hidden; margin-top:9px; }
.barline i { display:block; height:100%; }

.comment { background: var(--surface-2); border:1px solid var(--hairline); border-radius: var(--r-md);
  padding: 10px 12px; margin-top: 8px; font-size: 12.5px; color: var(--ink-muted); line-height: 1.45; }
.comment .tag { font-family: var(--mono); font-size: 10.5px; color: var(--primary-hover); margin-top:5px; display:block; }
.comment.pod { color: var(--ink-subtle); }
.comment .tag a { color:var(--primary-hover); }
.spotcheck { display:inline-block; margin-top:10px; font-size:12px; color:var(--primary-hover);
  font-family:var(--mono); }
.spotcheck:hover { text-decoration:underline; }
.cl-intro { font-size:12px; color:var(--ink-subtle); line-height:1.5; margin-bottom:12px;
  background:var(--surface-2); border:1px solid var(--hairline); border-radius:var(--r-md); padding:9px 11px; }
.cl-intro b { color:#4ad06c; font-weight:600; }
.podbox { margin-top:11px; background:var(--warn-dim); border:1px solid rgba(217,162,59,0.25);
  border-radius:var(--r-md); padding:10px 11px; }
.podhdr { font-size:11.5px; font-weight:600; color:var(--warn); margin-bottom:8px; }
.podtags { display:flex; flex-wrap:wrap; gap:5px; }
.podtag { font-size:11px; font-family:var(--mono); padding:1px 7px; border-radius:var(--r-sm);
  background:var(--surface-2); color:var(--ink-muted); border:1px solid var(--hairline); }
.podtag:hover { color:var(--warn); border-color:rgba(217,162,59,0.4); }
.podmore { font-size:11px; color:var(--ink-tertiary); padding:1px 4px; }
.tagrow { display: flex; flex-wrap: wrap; gap: 6px; }
.tagrow .chip { background: var(--surface-2); border:1px solid var(--hairline); }
.note { font-size: 12px; color: var(--ink-tertiary); line-height: 1.5; margin-top: 10px; }
.reasons { display:flex; flex-direction:column; gap:6px; }
.reason { display:flex; align-items:center; gap:8px; font-size:12.5px; color: var(--ink-muted); }
.reason .ic { width:14px; height:14px; flex-shrink:0; }

/* 待核验/原因 列 */
.subtier { display:block; font-size:10.5px; color:var(--ink-tertiary); font-family:var(--font); margin-top:1px; }
.col-reason { white-space: normal; max-width: 280px; }
.rchip { display:inline-block; font-size:11px; padding:2px 8px; border-radius:var(--r-sm);
  margin:2px 4px 2px 0; line-height:1.5; white-space:nowrap; }
.rchip.review { background:var(--warn-dim); color:var(--warn); }
.rchip.exclude { background:rgba(98,102,109,0.16); color:var(--ink-subtle); }
.col-reason .ok { color:#4ad06c; font-size:12px; }
.col-reason .muted { color:var(--ink-tertiary); }
.vmark { display:inline-block; font-size:10px; padding:1px 7px; border-radius:var(--r-pill);
  background:var(--primary-dim); color:var(--primary-hover); margin:2px 4px 2px 0; white-space:nowrap; }
.vmark.v-pass { background:var(--success-dim); color:#4ad06c; }
.vmark.v-fail { background:var(--danger-dim); color:#e0695f; }

/* 使用提示 + 折叠方法论 */
.usehint { background:var(--surface-1); border:1px solid var(--hairline); border-left:3px solid var(--primary);
  border-radius:var(--r-md); padding:12px 16px; margin-bottom:12px; font-size:13px; color:var(--ink-muted); line-height:1.6; }
.usehint b { color:var(--ink); font-weight:600; }
details.method { margin-bottom:28px; }
details.method > summary { cursor:pointer; list-style:none; padding:4px 0; color:var(--ink-subtle); }
details.method > summary::-webkit-details-marker { display:none; }
details.method > summary h3 { color:var(--ink-subtle); }
details.method > summary:hover h3 { color:var(--ink); }
details.method[open] > summary { margin-bottom:14px; }
/* 方法论面板 */
.panel.method h3 { color:var(--ink-subtle); }
.mgrid { display:grid; grid-template-columns:repeat(2,1fr); gap:12px; }
.mcol { background:var(--surface-2); border:1px solid var(--hairline); border-radius:var(--r-md); padding:13px; }
.mc-h { font-size:13px; font-weight:600; color:var(--ink); margin-bottom:7px; }
.mc-concern { font-size:12px; color:var(--ink-muted); line-height:1.5; margin-bottom:6px; }
.mc-how { font-size:12px; color:var(--ink-subtle); line-height:1.5; margin-bottom:9px; }
.mc-auto { font-size:11px; font-weight:600; padding:2px 9px; border-radius:var(--r-pill); display:inline-block; }
.mc-auto.y { background:var(--success-dim); color:#4ad06c; }
.mc-auto.n { background:var(--warn-dim); color:var(--warn); }
.mfoot { margin-top:13px; font-size:12px; color:var(--ink-tertiary); line-height:1.5;
  border-top:1px solid var(--hairline); padding-top:11px; }
@media (max-width:900px){ .mgrid { grid-template-columns:1fr; } }

/* 人工核验清单（抽屉顶部）*/
.checklist { background:linear-gradient(180deg, rgba(217,162,59,0.07), transparent);
  margin:0 -24px; padding:18px 24px 16px; border-bottom:1px solid var(--hairline); }
.checklist h4 { color:var(--warn); display:flex; align-items:center; gap:6px; }
.ck { border-left:2px solid var(--warn); padding:7px 0 7px 12px; margin-bottom:11px; }
.ck.m-auto { border-left-color:#4ad06c; }
.ck.m-data { border-left-color:var(--warn); }
.ck:last-child { margin-bottom:2px; }
.ck-h { font-size:13px; font-weight:600; color:var(--ink); display:flex; align-items:center; gap:8px; flex-wrap:wrap; }
.mbadge { font-size:10px; font-weight:500; padding:1px 7px; border-radius:var(--r-pill); font-family:var(--font); }
.mbadge.auto { background:var(--success-dim); color:#4ad06c; }
.mbadge.data { background:var(--warn-dim); color:var(--warn); }
.ck-why { font-size:12px; color:var(--ink-subtle); margin-top:3px; line-height:1.45; }
.ck-act { font-size:12px; color:var(--ink-muted); margin-top:5px; }
.ck-act a { color:var(--primary-hover); font-family:var(--mono); font-size:11.5px; }
.ck-act a:hover { text-decoration:underline; }
.excl-note { background:var(--danger-dim); color:#e0695f; padding:12px 14px; border-radius:var(--r-md);
  font-size:13px; margin:8px 0 4px; }
.excl-note b { display:block; margin-bottom:4px; font-weight:600; }
.biolink { display:inline-block; margin-top:8px; font-family:var(--mono); font-size:11.5px;
  color:var(--primary-hover); word-break:break-all; }
.biolink:hover { text-decoration:underline; }
.warn-note { margin-top:9px; font-size:12px; color:var(--warn); background:var(--warn-dim);
  padding:8px 11px; border-radius:var(--r-md); line-height:1.45; }

/* 表格赛道列 */
.nichecell { line-height:1.3; }
.nichecell .np { font-size:13px; color:var(--ink); }
.nichecell .ns { font-size:10.5px; color:var(--ink-tertiary); margin-top:2px; }
.fitb { display:inline-block; font-size:10px; font-weight:600; padding:1px 7px; border-radius:var(--r-pill); margin-left:4px; }
.fit-core { background:var(--success-dim); color:#4ad06c; }
.fit-rel { background:var(--warn-dim); color:var(--warn); }
.fit-off { background:var(--danger-dim); color:#e0695f; }
.fit-unk { background:rgba(98,102,109,0.16); color:var(--ink-subtle); }

/* 验收建议横幅 */
.verdict { margin:0 -24px 0; padding:14px 24px; border-bottom:1px solid var(--hairline);
  display:flex; align-items:baseline; gap:12px; }
.verdict.v-good { background:linear-gradient(180deg,rgba(39,166,68,0.12),transparent); }
.verdict.v-warn { background:linear-gradient(180deg,rgba(217,162,59,0.12),transparent); }
.verdict.v-bad { background:linear-gradient(180deg,rgba(196,69,59,0.12),transparent); }
.vd-s { font-size:16px; font-weight:700; white-space:nowrap; letter-spacing:-0.3px; }
.v-good .vd-s { color:#4ad06c; } .v-warn .vd-s { color:var(--warn); } .v-bad .vd-s { color:#e0695f; }
.vd-w { font-size:12.5px; color:var(--ink-muted); line-height:1.5; }

/* 赛道 section */
.nichetags { display:flex; flex-wrap:wrap; align-items:center; gap:6px; margin-bottom:10px; }
.ntp { font-size:12.5px; font-weight:600; background:var(--primary-dim); color:var(--primary-hover);
  padding:2px 10px; border-radius:var(--r-pill); }
.nts { font-size:11.5px; background:var(--surface-3); color:var(--ink-muted); padding:2px 9px; border-radius:var(--r-pill); }
.nbbox { background:var(--surface-3); border:1px solid var(--hairline); border-radius:var(--r-md); padding:10px 12px; margin-top:10px; }
.nb-title { font-size:11.5px; font-weight:600; color:var(--ink-subtle); margin-bottom:7px; }
.nbrow { display:grid; grid-template-columns:72px 1fr 28px; gap:8px; align-items:center; padding:3px 0;
  border-top:1px solid var(--hairline); font-size:12px; }
.nbrow:first-of-type { border-top:none; }
.nb-l { color:var(--ink-muted); } .nb-h { display:flex; flex-wrap:wrap; gap:4px; }
.nb-s { text-align:right; font-family:var(--mono); font-size:11px; color:var(--ink-tertiary); }

/* 综合评分机制与依据 */
.scoresec h4 { color:var(--ink); }
.scomp { background:var(--surface-2); border:1px solid var(--hairline); border-radius:var(--r-md);
  padding:10px 12px; margin-bottom:8px; }
.sc-top { display:flex; align-items:baseline; gap:8px; }
.sc-name { font-size:13px; font-weight:600; color:var(--ink); }
.sc-meta2 { font-size:11px; color:var(--ink-tertiary); font-family:var(--mono); flex:1; }
.sc-contrib { font-size:13px; font-weight:600; color:var(--primary-hover); font-family:var(--mono); }
.sc-bar { height:5px; border-radius:var(--r-pill); background:var(--surface-3); overflow:hidden; margin:7px 0 6px; }
.sc-bar i { display:block; height:100%; background:linear-gradient(90deg,#5e6ad2,#828fff); }
.sc-basis { font-size:11.5px; color:var(--ink-subtle); line-height:1.45; font-family:var(--mono); }
.abbox { background:var(--surface-3); border:1px solid var(--hairline); border-radius:var(--r-md);
  padding:11px 12px; margin-top:10px; }
.ab-title { font-size:12.5px; font-weight:600; color:var(--ink); margin-bottom:5px; }
.ab-formula { font-size:11px; color:var(--ink-subtle); margin-bottom:8px; line-height:1.4; }
.abrow { display:grid; grid-template-columns:88px 1fr 36px; gap:8px; align-items:center;
  padding:4px 0; border-top:1px solid var(--hairline); font-size:12px; }
.ab-l { color:var(--ink-muted); }
.ab-w { font-size:10px; color:var(--ink-tertiary); font-family:var(--mono); }
.ab-h { display:flex; flex-wrap:wrap; gap:4px; }
.abkw { font-size:10.5px; font-family:var(--mono); background:var(--surface-1); color:var(--primary-hover);
  border:1px solid var(--hairline); border-radius:var(--r-xs); padding:0 5px; }
.abnone { font-size:11px; color:var(--ink-tertiary); }
.ab-c { text-align:right; font-family:var(--mono); font-size:12px; color:var(--ink-muted); font-weight:600; }
.ab-math { font-size:11.5px; color:var(--ink-muted); font-family:var(--mono); margin-top:8px;
  padding-top:7px; border-top:1px solid var(--hairline); }
.ab-note { font-size:11px; color:var(--ink-tertiary); margin-top:6px; line-height:1.45; }

/* 客户需求达成 scorecard */
.crsec { margin:0 -24px; padding:18px 24px 10px; border-bottom:1px solid var(--hairline); }
.crsec h4 { color:var(--ink); }
.cr { border:1px solid var(--hairline); border-left-width:3px; border-radius:var(--r-md);
  padding:11px 13px; margin-bottom:9px; background:var(--surface-2); }
.cr.cr-pass { border-left-color:#27a644; } .cr.cr-warn { border-left-color:var(--warn); }
.cr.cr-fail { border-left-color:var(--danger); } .cr.cr-pend { border-left-color:var(--ink-tertiary); }
.cr-top { display:flex; align-items:center; justify-content:space-between; gap:10px; margin-bottom:6px; }
.cr-req { font-size:13px; font-weight:600; color:var(--ink); }
.cr-badge { font-size:11px; font-weight:600; padding:2px 9px; border-radius:var(--r-pill); white-space:nowrap; }
.cr-badge.cr-pass { background:var(--success-dim); color:#4ad06c; }
.cr-badge.cr-warn { background:var(--warn-dim); color:var(--warn); }
.cr-badge.cr-fail { background:var(--danger-dim); color:#e0695f; }
.cr-badge.cr-pend { background:rgba(98,102,109,0.16); color:var(--ink-subtle); }
.cr-client { font-size:11.5px; color:var(--ink-subtle); line-height:1.5; margin-bottom:5px; }
.cr-ev { font-size:12.5px; color:var(--ink-muted); }
.cr-proof { color:var(--primary-hover); font-size:11px; margin-left:4px; }

/* 浏览器核验记录与证据 */
.verifysec { margin:0 -24px; padding:18px 24px 8px; background:linear-gradient(180deg, rgba(94,106,210,0.07), transparent);
  border-bottom:1px solid var(--hairline); }
.verifysec h4 { color:var(--primary-hover); }
.vintro { font-size:12px; color:var(--ink-subtle); line-height:1.5; margin-bottom:14px;
  background:var(--surface-2); border:1px solid var(--hairline); border-radius:var(--r-md); padding:9px 11px; }
.vintro b { color:var(--ink-muted); font-weight:600; }
.vcheck { border:1px solid var(--hairline); border-radius:var(--r-lg); padding:14px; margin-bottom:12px;
  background:var(--surface-2); border-left-width:3px; }
.vcheck.v-pass { border-left-color:#27a644; } .vcheck.v-fail { border-left-color:var(--danger); }
.vcheck.v-incon { border-left-color:var(--warn); } .vcheck.v-note { border-left-color:var(--primary); }
.vcheck.v-err { border-left-color:var(--ink-tertiary); }
.vc-top { display:flex; align-items:center; justify-content:space-between; gap:10px; margin-bottom:10px; }
.vc-title { font-size:14px; font-weight:600; }
.vbadge { font-size:11px; font-weight:600; padding:2px 10px; border-radius:var(--r-pill); white-space:nowrap; }
.vbadge.v-pass { background:var(--success-dim); color:#4ad06c; }
.vbadge.v-fail { background:var(--danger-dim); color:#e0695f; }
.vbadge.v-incon { background:var(--warn-dim); color:var(--warn); }
.vbadge.v-note { background:var(--primary-dim); color:var(--primary-hover); }
.vbadge.v-err { background:rgba(98,102,109,0.16); color:var(--ink-subtle); }
.vrow { display:grid; grid-template-columns:72px 1fr; gap:10px; padding:5px 0; font-size:12.5px;
  border-top:1px solid var(--hairline); }
.vrow:first-of-type { border-top:none; }
.vk { color:var(--ink-tertiary); font-size:11.5px; }
.vv { color:var(--ink-muted); line-height:1.5; }
.vv.vres { color:var(--ink); font-weight:500; }
.vflow { margin:0; padding-left:16px; color:var(--ink-subtle); }
.vflow li { margin:2px 0; }
.vevi { margin-top:12px; display:grid; grid-template-columns:1fr; gap:10px; }
.vshotwrap { border:1px solid var(--hairline-strong); border-radius:var(--r-md); overflow:hidden; background:var(--canvas); }
.vshot { width:100%; display:block; max-height:280px; object-fit:cover; object-position:top; cursor:zoom-in;
  transition:opacity .15s; }
.vshot:hover { opacity:.85; }
.vno { padding:20px; text-align:center; color:var(--ink-tertiary); font-size:12px; }
.vdata { display:flex; flex-wrap:wrap; gap:6px; margin-bottom:6px; }
.vd { display:flex; gap:5px; align-items:baseline; background:var(--surface-3); border-radius:var(--r-sm);
  padding:3px 8px; font-size:11px; font-family:var(--mono); }
.vd span { color:var(--ink-tertiary); } .vd b { color:var(--ink-muted); font-weight:600; }
.vsrc { font-size:11px; color:var(--ink-tertiary); font-family:var(--mono); margin-top:3px; }
.vsrc a { color:var(--primary-hover); }

footer { max-width: 1280px; margin: 0 auto; padding: 24px; color: var(--ink-tertiary);
  font-size: 11.5px; font-family: var(--mono); border-top: 1px solid var(--hairline); }

@media (max-width: 900px) {
  .stats { grid-template-columns: repeat(2, 1fr); }
  .head h1 { font-size: 30px; }
  td.col-fn, th.col-fn, td.col-tier, th.col-tier { display: none; }
}
@media (prefers-reduced-motion: reduce) { * { transition: none !important; } }
"""

# ─── JS（渲染 + 交互）──────────────────────────────────────────────────────────

JS = r"""
const fmt = {
  int: n => (n==null?'—':Number(n).toLocaleString('en-US')),
  pct: n => (n==null?'—':(Number(n)*100).toFixed(1)+'%'),
  pct1: n => (n==null?'—':Number(n).toFixed(1)+'%'),
  er: n => (n==null||n===0?'—':Number(n).toFixed(2)+'%'),
};
const STATUS_ORDER = {include:3, review:2, exclude:1, error:0};
const REASON_COLORS = {
  followers_out_of_range:'#62666d', no_amazon_storefront:'#c4453b', no_engagement_data:'#7a6a3a',
  brand_like:'#4a4a52', inactive:'#3e3e44', private_account:'#4a4a52', ad_saturated:'#d9a23b',
  'low_credibility':'#c4453b',
};

// 链接类型说明（解释 LTK / Linktree / Beacons 等）
const LINK_TYPE_INFO = {
  amazon_direct: {name:'Amazon 官方橱窗直链', desc:'amazon.com/shop/* —— 客户硬性要求，已直接满足', ok:true},
  amazon_in_bio_text: {name:'Bio 文本含 Amazon', desc:'bio 文字提到 Amazon storefront，需确认是有效可点链接', ok:false},
  linktree: {name:'Linktree 聚合页', desc:'通用 link-in-bio 聚合工具，需点开确认是否含 Amazon Storefront 入口', ok:false},
  beacons: {name:'Beacons 聚合页', desc:'通用 link-in-bio 聚合工具，需点开确认是否含 Amazon Storefront', ok:false},
  ltk: {name:'LTK（LiketoKnow.it）', desc:'创作者购物联盟橱窗（RewardStyle 旗下），需确认其中是否挂接 Amazon 商品', ok:false},
  linkin_bio: {name:'link-in-bio 聚合页', desc:'聚合工具，需点开核验是否直达 Amazon', ok:false},
  bio_site: {name:'bio.site 聚合页', desc:'聚合工具，需点开核验是否直达 Amazon', ok:false},
  other: {name:'其他链接', desc:'非 Amazon / 非主流聚合工具', ok:false},
  none: {name:'无链接', desc:'未检测到 bio 链接', ok:false},
};

// 原因说明：每条 review/exclude 原因 → 标签 + 为什么 + 怎么核验
const REASON_INFO = {
  linkinbio_needs_manual_check: {sev:'review', label:'Amazon 橱窗待核验', why:'Bio 是聚合页（Linktree/Beacons/LTK），脚本未能穿透确认是否直达 Amazon Storefront', action:'真浏览器打开 bio 链接，渲染后读真实链接找 amazon.com/shop（LTK 需跟随联盟跳转）', use:'bio', mode:'auto'},
  low_product_content: {sev:'review', label:'内容画像待确认', why:'近 15 条 caption 产品推荐占比偏低，可能偏生活方式而非纯导购', action:'截图主页 + 视觉判断是否以产品导购为主', use:'profile', mode:'auto'},
  low_engagement: {sev:'review', label:'互动率未达基准', why:'Reels/Static 互动率低于该档位客户基准', action:'结合内容质量复核（数据已自动算出）', use:'profile', mode:'auto'},
  high_sponsorship: {sev:'review', label:'赞助饱和偏高', why:'近 15 条赞助帖 >30%，临近客户 40% 上限，可能广告疲劳', action:'核对赞助 vs organic 比例（已自动统计）', use:'profile', mode:'auto'},
  high_bot_ratio: {sev:'review', label:'评论真实度存疑', why:'评论区 bot/互赞团比例偏高', action:'抽查评论区真实购买意图（已自动检测）', use:'profile', mode:'auto'},
  comments_unavailable: {sev:'review', label:'评论未采集', why:'评论采集失败，无法评估购买意图', action:'重采或浏览器打开评论区', use:'profile', mode:'auto'},
  followers_out_of_range: {sev:'exclude', label:'粉丝超出 10K–150K', why:'不在客户目标档位', action:''},
  no_amazon_storefront: {sev:'exclude', label:'无 Amazon Storefront', why:'Bio 无 Amazon 链接 / 聚合页 —— 客户硬性门槛不满足', action:''},
  not_product_focused: {sev:'exclude', label:'非产品导向', why:'内容以生活方式为主，非导购型', action:''},
  no_engagement_data: {sev:'exclude', label:'无互动数据', why:'采集不到帖子互动', action:''},
  brand_like: {sev:'exclude', label:'疑似品牌/官方号', why:'非个人创作者', action:''},
  inactive: {sev:'exclude', label:'近 30 天未发帖', why:'账号不活跃', action:''},
  private_account: {sev:'exclude', label:'私密账号', why:'无法采集内容', action:''},
};

// include/review 候选的补充核验。mode: auto=browser-use+视觉可自动 / data=只能 Modash/后台
const MANUAL_ALWAYS = [
  {label:'视觉验证（护肤专项）', why:'客户要求真实未修图的近距离皮肤纹理特写，而非重滤镜 / 柔光环形灯视频', action:'截图 Reels/帖子 → 视觉模型判断皮肤纹理真实度', use:'profile', mode:'auto'},
  {label:'Storefront 成熟度', why:'客户要求 Storefront「成熟、活跃、有组织」，不只是存在', action:'浏览器打开 storefront → 数商品/分类/更新频率', use:'bio', mode:'auto'},
  {label:'Save 率 / DM 分享', why:'客户视 saves 与 DM 分享为关键信任信号，但任何公开页面都不渲染', action:'只能 Modash 或创作者后台 —— browser-use 也拿不到', use:'none', mode:'data'},
];

function reasonChips(c){
  // 已实跑浏览器核验 → 优先展示核验结论
  let vmarks = '';
  (c._verify||[]).forEach(k=>{
    const r = k.result;
    if (k.id==='amazon_storefront'){
      if (r==='pass') vmarks += '<span class="vmark v-pass">✓ Amazon 已核实</span>';
      else if (r==='fail') vmarks += '<span class="vmark v-fail">✗ 无 Amazon 橱窗</span>';
      else vmarks += '<span class="vmark">Amazon 待确认</span>';
    }
  });
  if (c.status==='include') return '<span class="ok">✓ 硬条件全通过</span>' + vmarks;
  if (c.status==='error') return '<span class="muted">采集失败</span>';
  const rs = (c.status==='exclude'?c.filter_reasons:c.review_reasons)||[];
  const base = (!rs.length) ? (vmarks?'':'<span class="muted">—</span>')
    : rs.map(r=>{ const k=r.split(':')[0]; const info=REASON_INFO[k];
        return `<span class="rchip ${c.status}">${info?info.label:k}</span>`; }).join('');
  return vmarks + base;
}
function linkFor(c, use){
  if (use==='bio' && c.bio_link_url) return `<a href="${c.bio_link_url}" target="_blank">打开 bio 链接 ↗</a>`;
  if (use==='profile') return `<a href="${c.profile_url||('https://instagram.com/'+c.handle)}" target="_blank">打开主页 ↗</a>`;
  return '';
}

const FIT_CLS = {'对口':'fit-core','相关':'fit-rel','跨垂类':'fit-off','未识别':'fit-unk'};
function fitBadge(c){ const l=c.campaign_fit_label||'未识别'; return `<span class="fitb ${FIT_CLS[l]||'fit-unk'}">${l}</span>`; }
function nicheCell(c){
  const p = c.niche_primary_label||'未识别';
  const sec = (c.niche_secondary||[]).slice(0,2).join('·');
  return `<div class="nichecell"><span class="np">${esc(p)}</span> ${fitBadge(c)}${sec?`<div class="ns">${esc(sec)}</div>`:''}</div>`;
}

// 面向使用者的一句话验收建议
function verdict(c){
  const amazonOk = (c._verify||[]).some(k=>k.id==='amazon_storefront'&&k.result==='pass');
  const amazonFail = (c._verify||[]).some(k=>k.id==='amazon_storefront'&&k.result==='fail');
  const fit = c.campaign_fit;
  if (c.status==='exclude') return {t:'bad', s:'不纳入', w:'未通过硬性条件（见下方原因）。'};
  if (amazonFail) return {t:'bad', s:'倾向排除', w:'浏览器核验未发现 Amazon Storefront —— 客户硬门槛不满足。'};
  if (fit==='off') return {t:'bad', s:'倾向排除', w:'赛道为「'+(c.niche_primary_label||'?')+'」属跨垂类，与护肤设备活动不符。'};
  if (amazonOk && fit==='core') return {t:'good', s:'重点候选', w:'已核实 Amazon 橱窗 + 对口赛道。剩余需人工确认：视觉真实度。'};
  if (c.status==='include') return {t:'good', s:'建议纳入', w:'硬性条件全过。剩余人工项：视觉真实度（见核验清单）。'};
  const n=(c.review_reasons||[]).length;
  return {t:'warn', s:'待人工核验', w:'有 '+n+' 项待办，按下方「核验清单/证据」逐项确认后即可定夺。'};
}

const VRESULT = {
  pass:{label:'通过', cls:'v-pass'}, fail:{label:'未通过', cls:'v-fail'},
  inconclusive:{label:'待进一步确认', cls:'v-incon'}, note:{label:'提示', cls:'v-note'},
  error:{label:'加载失败', cls:'v-err'},
};
function esc(s){ return (s==null?'':String(s)).replace(/</g,'&lt;'); }

// 客户 4 条核心要求 → 达成情况 + 证据（用于人工验收）
const CR = {pass:{l:'达成',c:'cr-pass'}, warn:{l:'接近上限/偏弱',c:'cr-warn'},
            fail:{l:'未达',c:'cr-fail'}, pending:{l:'待核验',c:'cr-pend'}};
function compliance(c){
  const v={}; (c._verify||[]).forEach(k=>{ v[k.id]=k; });
  const items=[];
  // ① Bio 直链 Amazon Storefront（硬门槛）
  let r1,e1; const amz=v['amazon_storefront'];
  if (amz){
    r1 = amz.result==='pass'?'pass':(amz.result==='fail'?'fail':(amz.result==='error'?'pending':'warn'));
    e1 = amz.result_text;
  } else if (c.has_amazon_storefront===true){ r1='pass'; e1='bio 直链 Amazon（'+(c.bio_link_type||'')+'）'; }
  else if (c.has_amazon_storefront==='unverified'){ r1='warn'; e1='聚合页（'+(c.bio_link_type||'')+'），待浏览器穿透核验'; }
  else { r1='fail'; e1='bio 无 Amazon 链接'; }
  items.push({req:'① Bio 直链 Amazon Storefront', client:'必须有精心整理的 Amazon Storefront（可经 Linktree/Beacons/LTK 跳转），否则不纳入 Prime Day 名单',
    result:r1, evidence:e1, verify:amz});
  // ② 视觉真实性（护肤近距离皮肤纹理）
  let r2,e2; const vis=v['visual'];
  if (vis){ r2 = vis.result==='pass'?'pass':(vis.result==='fail'?'fail':(vis.result==='note'?'warn':'pending')); e2=vis.result_text; }
  else { r2='pending'; e2='尚未截取内容做视觉判断（可 browser-use+视觉自动补）'; }
  items.push({req:'② 视觉真实性（护肤专项）', client:'优先真实、未大幅修饰的近距离皮肤纹理；排斥重滤镜/低曝光/柔焦/强环形灯',
    result:r2, evidence:e2, verify:vis});
  // ③ 赞助饱和度 ≤40%
  const sr=c.sponsored_ratio||0; const r3 = sr>0.40?'fail':(sr>0.30?'warn':'pass');
  items.push({req:'③ 赞助饱和度 ≤ 40%', client:'近 15 帖含 #ad/#sponsored/Paid Partnership 若 >40% → 广告疲劳，降低大促转化',
    result:r3, evidence:`近 15 帖赞助 ${c.sponsored_count||0} 条 = ${(sr*100).toFixed(0)}%（阈值 40%）`});
  // ④ 自然内容稳定性
  const org=c.organic_amazon_posts_count||0; const r4 = org>=3?'pass':(org>=1?'warn':'fail');
  items.push({req:'④ 自然内容稳定性', client:'需定期发非赞助的 "Amazon Finds"/"Skincare Routine" organic 内容，维持社区信任',
    result:r4, evidence:`近 15 帖 organic 产品/护肤帖 ${org} 条`});
  return items;
}
function renderCompliance(c){
  if (c.status==='exclude'||c.status==='error') return '';
  const items=compliance(c);
  const passN=items.filter(i=>i.result==='pass').length;
  return `<div class="sec crsec">
    <h4>客户需求达成 · ${passN}/4 达成（供人工验收）</h4>
    ${items.map(it=>{ const m=CR[it.result]||CR.pending;
      const proof = it.verify && it.verify.evidence && it.verify.evidence.screenshot_b64
        ? '<span class="cr-proof">↓ 见下方核验证据截图</span>' : '';
      return `<div class="cr ${m.c}">
        <div class="cr-top"><span class="cr-req">${esc(it.req)}</span><span class="cr-badge ${m.c}">${m.l}</span></div>
        <div class="cr-client">客户：${esc(it.client)}</div>
        <div class="cr-ev">结果：${esc(it.evidence)} ${proof}</div>
      </div>`; }).join('')}</div>`;
}

function renderVerify(c){
  const checks = c._verify||[];
  if (!checks.length) return '';
  const rows = checks.map(k=>{
    const vr = VRESULT[k.result] || {label:k.result, cls:'v-note'};
    const flow = (k.workflow||[]).map(s=>`<li>${esc(s)}</li>`).join('');
    const ev = k.evidence||{};
    const data = ev.data||{};
    const dataRows = Object.keys(data).map(key=>`<div class="vd"><span>${esc(key)}</span><b>${esc(typeof data[key]==='object'?JSON.stringify(data[key]):data[key])}</b></div>`).join('');
    const shot = ev.screenshot_b64
      ? `<a href="${ev.screenshot_b64}" target="_blank" title="点击看大图"><img class="vshot" src="${ev.screenshot_b64}" loading="lazy"></a>`
      : '<div class="vno">（截图缺失）</div>';
    return `<div class="vcheck ${vr.cls}">
      <div class="vc-top"><span class="vc-title">${esc(k.title)}</span><span class="vbadge ${vr.cls}">${vr.label}</span></div>
      <div class="vrow"><span class="vk">对方关心</span><span class="vv">${esc(k.client_concern)}</span></div>
      <div class="vrow"><span class="vk">工作流程</span><span class="vv"><ol class="vflow">${flow}</ol></span></div>
      <div class="vrow"><span class="vk">验证了什么</span><span class="vv">${esc(k.checked)}</span></div>
      <div class="vrow"><span class="vk">结论</span><span class="vv vres">${esc(k.result_text)}</span></div>
      <div class="vevi">
        <div class="vshotwrap">${shot}</div>
        <div class="vmeta">
          <div class="vdata">${dataRows}</div>
          ${ev.source_url?`<div class="vsrc">证据来源 <a href="${ev.source_url}" target="_blank">${esc(ev.source_url.replace(/^https?:\/\//,'').slice(0,46))} ↗</a></div>`:''}
          ${ev.captured_at?`<div class="vsrc">现场抓取于 ${esc(ev.captured_at)}</div>`:''}
        </div>
      </div>
    </div>`;
  }).join('');
  return `<div class="sec verifysec">
    <h4>浏览器核验记录与证据 · ${checks.length} 项（已实跑）</h4>
    <div class="vintro">下列结果由 Playwright 驱动真实 Chrome <b>实际打开页面</b>核验，截图与数据为<b>现场抓取的证据</b>，可点链接复核 —— 非人工臆测/模型幻觉。</div>
    ${rows}
  </div>`;
}

// 权威度评分分解（可审计：哪些词命中、权重、算式）
function renderAuthorityBd(c){
  const ab = c.authority_breakdown;
  if (!ab || !ab.categories) return '';
  const cats = ab.categories.map(ct=>`
    <div class="abrow">
      <span class="ab-l">${esc(ct.label)} <span class="ab-w">×${ct.weight}</span></span>
      <span class="ab-h">${ct.count? ct.hits.map(h=>`<span class="abkw">${esc(h)}</span>`).join('') : '<span class="abnone">无命中</span>'}</span>
      <span class="ab-c">+${ct.contribution}</span>
    </div>`).join('');
  return `<div class="abbox">
    <div class="ab-title">权威度评分分解 · ${c.skincare_authority_score||0}/100</div>
    <div class="ab-formula">公式：${esc(ab.formula)}</div>
    ${cats}
    <div class="ab-math">算式：${esc(ab.math)}</div>
    <div class="ab-note">${esc(ab.weights_note||'')}</div>
  </div>`;
}

// 产品推荐占比分解
function renderProductRecBd(c){
  const pb = c.product_rec_breakdown;
  if (!pb || !pb.math) return '';
  const kws = (pb.matched_keywords||[]).map(k=>`<span class="abkw">${esc(k)}</span>`).join('');
  return `<div class="abbox">
    <div class="ab-title">产品推荐占比分解 · ${c.product_rec_ratio}</div>
    <div class="ab-formula">公式：${esc(pb.formula)} ｜ ${esc(pb.thresholds||'')}</div>
    <div class="ab-math">算式：${esc(pb.math)}（→ ${esc(c.creator_archetype||'')}）</div>
    ${kws?`<div class="ab-h" style="margin-top:6px">命中词：${kws}</div>`:''}
  </div>`;
}

function renderVerdict(c){
  const v = verdict(c);
  return `<div class="verdict v-${v.t}"><div class="vd-s">${v.s}</div><div class="vd-w">${esc(v.w)}</div></div>`;
}
function renderNiche(c){
  const bd = c.niche_breakdown||[];
  const sec = (c.niche_secondary||[]);
  const cats = bd.map(b=>`<div class="nbrow"><span class="nb-l">${esc(b.label)}</span><span class="nb-h">${(b.hits||[]).map(h=>`<span class="abkw">${esc(h)}</span>`).join('')}</span><span class="nb-s">${b.score}</span></div>`).join('');
  return `<div class="sec nichesec">
    <h4>创作者赛道（垂类）· ${esc(c.niche_primary_label||'未识别')}</h4>
    <div class="nichetags"><span class="ntp">${esc(c.niche_primary_label||'未识别')}</span>${sec.map(s=>`<span class="nts">${esc(s)}</span>`).join('')} ${fitBadge(c)}</div>
    <div class="note">对本次「护肤 / 红光设备」活动对口度：<b>${esc(c.campaign_fit_label||'未识别')}</b> —— 核心赛道(护肤/美妆/美容仪器)=对口；时尚/母婴/生活=相关；理财/美食/旅行=跨垂类(评分×0.55)。</div>
    <div class="nbbox"><div class="nb-title">赛道判定依据 · 命中关键词（可审计）</div>${cats||'<div class="abnone">无命中</div>'}</div>
  </div>`;
}

// 综合评分机制与依据（每项子分：原始值 × 权重 = 贡献 + 计算依据）
function renderScoring(c){
  const sb = c.score_breakdown;
  if (!sb || !sb.components) return '';
  const rows = sb.components.map(comp=>`
    <div class="scomp">
      <div class="sc-top"><span class="sc-name">${esc(comp.name)}</span>
        <span class="sc-meta2">子分 ${comp.raw} × 权重 ${(comp.weight*100).toFixed(0)}%</span>
        <span class="sc-contrib">+${comp.contribution}</span></div>
      <div class="sc-bar"><i style="width:${Math.min(100,comp.raw)}%"></i></div>
      <div class="sc-basis">${esc(comp.basis)}</div>
    </div>`).join('');
  return `<div class="sec scoresec">
    <h4>综合评分机制与依据 · ${sb.total} 分（${esc(sb.mode)}）</h4>
    <div class="vintro">${esc(sb.formula)}。每项子分 0-100，下方逐项给出<b>原始值 × 权重 = 贡献</b>及<b>计算依据</b> —— 评分不进黑箱。</div>
    ${rows}
    ${renderAuthorityBd(c)}
    ${renderProductRecBd(c)}
  </div>`;
}
function initial(h){ return (h||'?').replace(/[^a-z0-9]/gi,'').slice(0,2).toUpperCase()||'?'; }
function azClass(v){ return v===true?'az-yes':(v==='unverified'?'az-unv':'az-no'); }
function azLabel(v){ return v===true?'✓ 直链':(v==='unverified'?'待验证':'无'); }

let state = { filter:'all', q:'', sort:'discovery_score', dir:-1 };

function bestER(c){ return Math.max(c.reels_engagement_rate||0, c.static_engagement_rate||0); }

function filtered(){
  let list = DATA.candidates.slice();
  if (state.filter!=='all') list = list.filter(c=>c.status===state.filter);
  if (state.q){ const q=state.q.toLowerCase();
    list = list.filter(c => (c.handle||'').toLowerCase().includes(q) || (c.full_name||'').toLowerCase().includes(q)); }
  const k = state.sort;
  list.sort((a,b)=>{
    if (k==='status'){ return (STATUS_ORDER[b.status]-STATUS_ORDER[a.status])*(-state.dir); }
    let av, bv;
    if (k==='best_er'){ av=bestER(a); bv=bestER(b); }
    else { av=a[k]??-1; bv=b[k]??-1; }
    if (typeof av==='string'){ return av.localeCompare(bv)*state.dir; }
    return (av-bv)*state.dir;
  });
  // include/review 永远排在最前（按状态分层），层内按 sort
  list.sort((a,b)=> STATUS_ORDER[b.status]-STATUS_ORDER[a.status]);
  return list;
}

function renderStats(){
  const s = DATA.summary;
  const el = document.getElementById('stats');
  const cards = [
    {cls:'s-total', n:s.total_candidates, l:'候选总数', sub:'扫描 + 漏斗'},
    {cls:'s-include', n:s.include, l:'Include 命中', sub:'硬条件全过'},
    {cls:'s-review', n:s.review, l:'Review 待核', sub:'有 Amazon／待人工'},
    {cls:'s-exclude', n:s.exclude, l:'Exclude 排除', sub:'不符条件'},
  ];
  el.innerHTML = cards.map(c=>`<div class="stat ${c.cls}"><div class="n">${c.n}</div>
    <div class="l">${c.l}</div><div class="sub">${c.sub}</div></div>`).join('');
}

function renderFunnel(){
  const ex = DATA.candidates.filter(c=>c.status==='exclude');
  const counts = {};
  ex.forEach(c=>{ (c.filter_reasons||['other']).forEach(r=>{ const key=r.split(':')[0]; counts[key]=(counts[key]||0)+1; }); });
  const entries = Object.entries(counts).sort((a,b)=>b[1]-a[1]);
  const total = ex.length || 1;
  const bar = entries.map(([r,n])=>`<span style="width:${(n/total*100).toFixed(1)}%;background:${REASON_COLORS[r]||'#4a4a52'}"></span>`).join('');
  const legend = entries.map(([r,n])=>`<div class="fl"><span class="dot" style="background:${REASON_COLORS[r]||'#4a4a52'}"></span>${r} <span class="c">${n}</span></div>`).join('');
  document.getElementById('funnel').innerHTML =
    `<h3>排除原因分解 · ${ex.length} 个</h3><div class="funnelbar">${bar}</div><div class="funnel-legend">${legend}</div>`;
}

function statusBadge(s){
  const map={include:'Include',review:'Review',exclude:'Exclude',error:'Error'};
  return `<span class="badge b-${s}"><span class="d"></span>${map[s]||s}</span>`;
}

function renderTable(){
  const list = filtered();
  const tb = document.getElementById('tbody');
  if (!list.length){ tb.innerHTML = `<tr><td colspan="10"><div class="empty">没有符合条件的候选</div></td></tr>`; return; }
  tb.innerHTML = list.map(c=>{
    const er = bestER(c);
    const score = c.discovery_score||0;
    return `<tr onclick="openDrawer('${c.handle}')">
      <td><div class="handle"><div class="av">${initial(c.handle)}</div>
        <div><div class="hn">@${c.handle}</div><div class="fn col-fn">${(c.full_name||'').replace(/</g,'&lt;')}</div></div></div></td>
      <td class="num">${fmt.int(c.follower_count)}<span class="subtier">${c.tier||''}</span></td>
      <td>${nicheCell(c)}</td>
      <td><span class="${azClass(c.has_amazon_storefront)}">${azLabel(c.has_amazon_storefront)}</span></td>
      <td class="num">${fmt.er(er)}</td>
      <td><span class="trust-${c.trust_level||'unknown'}">${c.trust_level||'—'}</span></td>
      <td class="num"><span class="scorecell"><span class="scorebar"><i style="width:${Math.min(100,score)}%"></i></span><span class="scoreval">${score.toFixed(1)}</span></span></td>
      <td>${statusBadge(c.status)}</td>
      <td class="col-reason">${reasonChips(c)}</td>
    </tr>`;
  }).join('');
}

function kv(k,v,wrap){ return `<div class="kv"><span class="k">${k}</span><span class="v ${wrap?'wrap':''}">${v}</span></div>`; }

function openDrawer(handle){
  const c = DATA.candidates.find(x=>x.handle===handle); if(!c) return;
  const er = bestER(c);
  const ev = (c.authority_evidence||[]);
  const intentC = (c.top_intent_comments||[]);
  const podC = (c.pod_comment_samples||[]);
  const reasons = (c.status==='exclude'?c.filter_reasons:c.review_reasons)||[];

  const igProfile = c.profile_url||('https://instagram.com/'+c.handle);
  function commentLink(t){
    // 有 post_code 则直达原帖，否则回退到主页（抽查渠道）
    return t && t.post_code ? `https://instagram.com/p/${t.post_code}/` : igProfile;
  }
  let commentsHtml='';
  if (intentC.length){
    commentsHtml += intentC.slice(0,4).map(t=>`<div class="comment">"${(t.text||'').replace(/</g,'&lt;')}"<span class="tag">↳ 购买意图 · ${t.keyword||''} · <a href="${commentLink(t)}" target="_blank">原帖 ↗</a></span></div>`).join('');
  }
  if (podC.length){
    commentsHtml += podC.slice(0,3).map(t=>`<div class="comment pod">"${(t||'').replace(/</g,'&lt;')}"<span class="tag" style="color:var(--warn)">↳ 疑似互赞团/水军</span></div>`).join('');
  }
  if (!commentsHtml) commentsHtml = `<div class="note">该候选未采集到评论，或无显著意图/水军信号。</div>`;
  commentsHtml += `<a class="spotcheck" href="${igProfile}" target="_blank">抽查 ↗ 打开主页评论区逐条核对（证据渠道，日常无需逐条人工）</a>`;

  const podPct = (c.engagement_pod_ratio||0)*100;
  const botPct = (c.bot_comment_ratio||0)*100;
  const intentPct = (c.purchase_intent_ratio||0)*100;

  // 顶部：排除原因 / 已实跑核验证据 / 待核验清单
  let topHtml = '';
  if (c.status==='exclude' || c.status==='error'){
    const rs = (c.filter_reasons||[]);
    topHtml = `<div class="sec"><h4 style="color:#e0695f">为什么排除</h4>` +
      (rs.length? rs.map(r=>{ const info=REASON_INFO[r.split(':')[0]];
        return `<div class="excl-note"><b>${info?info.label:r}</b>${info?info.why:''}</div>`; }).join('')
        : `<div class="excl-note">${c.error||'未通过硬性条件'}</div>`) + `</div>`;
  } else {
    const verified = (c._verify||[]).length>0;
    // 已实跑核验的证据（最高优先级展示）
    topHtml = renderVerify(c);
    // 剩余待核验项：已实跑的项不再列；只列尚未自动化或只能 Modash 的
    const doneIds = new Set((c._verify||[]).map(k=>k.id));
    const items = [];
    (c.review_reasons||[]).forEach(r=>{ const info=REASON_INFO[r.split(':')[0]];
      if(info && !(doneIds.has('amazon_storefront') && r.split(':')[0]==='linkinbio_needs_manual_check')) items.push(info); });
    MANUAL_ALWAYS.forEach(m=>{
      if (m.label.indexOf('视觉')>=0 && doneIds.has('visual')) return;
      if (m.label.indexOf('Storefront')>=0 && doneIds.has('storefront_maturity')) return;
      items.push(m);
    });
    // 收束人工节点：auto 项是"系统自动核验（日常无需人工）"，data 项才是真人工/Modash
    const autoItems = items.filter(i=>i.mode!=='data');
    const dataItems = items.filter(i=>i.mode==='data');
    if (items.length){
      topHtml += `<div class="sec checklist">
        <h4>核验分工 · 自动 ${autoItems.length} / 需人工·Modash ${dataItems.length}</h4>
        <div class="cl-intro">日常执行<b>无需逐项人工</b>：绿色项系统可自动核验（点链接仅供偶尔抽查）；只有黄色项需人工 / Modash 补充。</div>
        ${autoItems.map(it=>`<div class="ck m-auto">
          <div class="ck-h">${it.label} <span class="mbadge auto">系统自动 · 无需日常人工</span></div>
          <div class="ck-why">${it.why}</div>
          <div class="ck-act">抽查渠道：${linkFor(c,it.use)||'—'}</div>
        </div>`).join('')}
        ${dataItems.map(it=>`<div class="ck m-data">
          <div class="ck-h">${it.label} <span class="mbadge data">需人工 / Modash</span></div>
          <div class="ck-why">${it.why}</div>
          <div class="ck-act">→ ${it.action}</div>
        </div>`).join('')}</div>`;
    }
  }

  const lt = LINK_TYPE_INFO[c.bio_link_type] || LINK_TYPE_INFO.other;

  document.getElementById('drawerBody').innerHTML = renderVerdict(c) + renderNiche(c) + renderCompliance(c) + topHtml + `
    <div class="sec">
      <h4>基础</h4>
      ${kv('粉丝数', fmt.int(c.follower_count))}
      ${kv('关注 / 帖子', fmt.int(c.following_count)+' / '+fmt.int(c.media_count))}
      ${kv('档位', (c.tier||'—')+(c.is_verified?' · 已认证':''))}
      ${kv('发现来源',(c.discovery_sources||[]).join(', ')||'—',true)}
    </div>
    <div class="sec">
      <h4>Bio 基础设施（硬门槛）</h4>
      ${kv('Amazon Storefront', `<span class="${azClass(c.has_amazon_storefront)}">${azLabel(c.has_amazon_storefront)}</span>`)}
      ${kv('链接类型', lt.name)}
      <div class="note">${lt.desc}</div>
      ${c.bio_link_url?`<a class="biolink" href="${c.bio_link_url}" target="_blank">${c.bio_link_url.replace(/^https?:\/\//,'')} ↗</a>`:''}
      ${c.has_amazon_storefront==='unverified'?`<div class="warn-note">⚠ 需人工点开此链接，确认是否直达一个成熟、活跃的 Amazon Storefront</div>`:''}
    </div>
    <div class="sec">
      <h4>内容画像</h4>
      ${kv('类型', `<span class="arch-${c.creator_archetype}">${c.creator_archetype||'—'}</span>`)}
      ${kv('产品推荐占比', fmt.pct(c.product_rec_ratio))}
      ${kv('权威度评分', (c.skincare_authority_score||0)+' / 100')}
      ${ev.length?`<div class="tagrow" style="margin-top:8px">${ev.map(e=>`<span class="chip">${e}</span>`).join('')}</div>`:''}
    </div>
    <div class="sec">
      <h4>赞助饱和度</h4>
      ${kv('赞助帖 / 占比', (c.sponsored_count||0)+' · '+fmt.pct(c.sponsored_ratio))}
      ${kv('Organic 产品帖', c.organic_amazon_posts_count||0)}
    </div>
    <div class="sec">
      <h4>互动率</h4>
      <div class="metric">
        <div class="m"><div class="mn">${fmt.er(c.reels_engagement_rate)}</div><div class="ml">Reels ER · ${c.reels_count||0} 条</div></div>
        <div class="m"><div class="mn">${fmt.er(c.static_engagement_rate)}</div><div class="ml">Static ER · ${c.static_count||0} 条</div></div>
      </div>
      <div class="note">基准${c.meets_er_benchmark?' <span style="color:#4ad06c">✓ 达标</span>':' <span style="color:var(--warn)">未达标</span>'}（按档位 micro/mid 分级）</div>
    </div>
    <div class="sec">
      <h4>评论信任度 · 分析 ${c.comments_analyzed||0} 条</h4>
      <div class="metric" style="grid-template-columns:1fr 1fr 1fr">
        <div class="m"><div class="mn">${fmt.pct1(intentPct)}</div><div class="ml">购买意图</div><div class="barline"><i style="width:${Math.min(100,intentPct*4)}%;background:#4ad06c"></i></div></div>
        <div class="m"><div class="mn">${fmt.pct1(botPct)}</div><div class="ml">Bot 评论</div><div class="barline"><i style="width:${Math.min(100,botPct)}%;background:var(--ink-subtle)"></i></div></div>
        <div class="m"><div class="mn">${fmt.pct1(podPct)}</div><div class="ml">互赞团</div><div class="barline"><i style="width:${Math.min(100,podPct)}%;background:var(--warn)"></i></div></div>
      </div>
      ${kv('信任等级', `<span class="trust-${c.trust_level||'unknown'}">${(c.trust_level||'—').toUpperCase()}</span>`)}
      ${(c.pod_commenters&&c.pod_commenters.length)?`
        <div class="podbox">
          <div class="podhdr">已标记互赞团/水军账号 · ${c.pod_commenters.length} 个（已入库，下次扫到直接略过）</div>
          <div class="podtags">${c.pod_commenters.slice(0,24).map(u=>`<a class="podtag" href="https://instagram.com/${esc(u)}" target="_blank">@${esc(u)}</a>`).join('')}${c.pod_commenters.length>24?`<span class="podmore">+${c.pod_commenters.length-24}</span>`:''}</div>
        </div>`:''}
      <div style="margin-top:10px">${commentsHtml}</div>
    </div>
    ${renderScoring(c)}
    <div class="sec">
      <h4>状态与待办</h4>
      ${reasons.length?`<div class="reasons">${reasons.map(r=>`<div class="reason"><span class="ic" style="color:${c.status==='exclude'?'var(--danger)':'var(--warn)'}">●</span>${r}</div>`).join('')}</div>`:'<div class="note">无待办标记，硬性条件全部通过。</div>'}
    </div>`;

  document.getElementById('drawerHead').innerHTML = `
    <div class="top"><div class="who"><div class="av">${initial(c.handle)}</div>
      <div><h2>@${c.handle}</h2><div class="fn">${(c.full_name||'').replace(/</g,'&lt;')}</div></div></div>
      <button class="xbtn" onclick="closeDrawer()">✕</button></div>
    <div class="pillrow">${statusBadge(c.status)}
      <span class="badge b-exclude">${fmt.int(c.follower_count)} 粉丝</span>
      <span class="badge b-exclude">score ${(c.discovery_score||0).toFixed(1)}</span></div>
    <a class="viewig" href="${c.profile_url||('https://instagram.com/'+c.handle)}" target="_blank">在 Instagram 打开 ↗</a>`;

  document.getElementById('scrim').classList.add('open');
  document.getElementById('drawer').classList.add('open');
}
function closeDrawer(){ document.getElementById('scrim').classList.remove('open'); document.getElementById('drawer').classList.remove('open'); }

function wire(){
  document.querySelectorAll('.tab').forEach(t=>t.onclick=()=>{
    state.filter=t.dataset.f; document.querySelectorAll('.tab').forEach(x=>x.classList.remove('active'));
    t.classList.add('active'); renderTable(); });
  document.getElementById('search').oninput=e=>{ state.q=e.target.value; renderTable(); };
  document.getElementById('sortsel').onchange=e=>{ state.sort=e.target.value; renderTable(); };
  document.querySelectorAll('th.sortable').forEach(th=>th.onclick=()=>{
    const k=th.dataset.k; if(state.sort===k) state.dir*=-1; else {state.sort=k; state.dir=-1;}
    document.getElementById('sortsel').value=k; renderTable(); });
  document.getElementById('scrim').onclick=closeDrawer;
  document.addEventListener('keydown',e=>{ if(e.key==='Escape') closeDrawer(); });
}

// tab counts
function tabCounts(){
  const s=DATA.summary;
  document.querySelector('[data-f="all"] .badge').textContent=s.total_candidates;
  document.querySelector('[data-f="include"] .badge').textContent=s.include;
  document.querySelector('[data-f="review"] .badge').textContent=s.review;
  document.querySelector('[data-f="exclude"] .badge').textContent=s.exclude;
}
renderStats(); renderFunnel(); tabCounts(); renderTable(); wire();
"""


def build_html(data: dict, source_name: str) -> str:
    ts = data.get("timestamp", "")
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        dt = ts
    run_id = data.get("run_id", "")
    s = data["summary"]
    gen = datetime.now().strftime("%Y-%m-%d %H:%M")

    # 水军账号库规模（持久，跨 run 累积）
    pod_count = 0
    pod_file = ROOT / "data" / "pod_accounts.json"
    if pod_file.exists():
        try:
            pod_count = json.loads(pod_file.read_text(encoding="utf-8")).get("count", 0)
        except Exception:
            pod_count = 0

    # 安全嵌入 JSON（避免 </script> 截断）
    data_json = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")

    return f"""<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Amazon Finds 红人发现 · {run_id}</title>
<style>{CSS}</style>
</head><body>

<nav class="topnav">
  <div class="brand"><span class="mark"></span> Amazon Finds Discovery</div>
  <div class="spacer"></div>
  <div class="meta">run <b>{run_id}</b> · <b>{s['total_candidates']}</b> 候选 · {dt}</div>
</nav>

<div class="wrap">
  <div class="head">
    <div class="eyebrow">Creator Discovery Report</div>
    <h1>导购型红人发现结果</h1>
    <p>从品牌合作创作者中筛选符合 Amazon Finds 画像的红人：Amazon Storefront 为硬门槛，内容画像 / 互动率 / 评论信任度多层评估。</p>
    <div class="runline">
      <span>品牌种子 <b>currentbody · solawave · therabody</b></span>
      <span>粉丝区间 <b>10K–150K</b></span>
      <span>评论信任 <b>购买意图 / Bot / 互赞团</b> 三信号</span>
      <span>水军账号库 <b>{pod_count} 个</b>（累积，扫到直接略过）</span>
    </div>
  </div>

  <div class="stats" id="stats"></div>
  <div class="panel" id="funnel"></div>

  <div class="usehint">
    <b>怎么用：</b>下表按评分排序，<b>赛道·对口度</b>列看是否切合本次护肤设备活动（绿=对口）。
    点开任一红人 → 右侧抽屉顶部给<b>一句话验收建议</b>，下方逐项列出<b>客户4条需求达成情况 + 现场核验证据截图 + 评分依据</b>，按提示逐项确认即可定夺。
  </div>

  <details class="panel method">
    <summary><h3 style="display:inline">核验方法论 · 客户关心什么 → 我们怎么验 → 能否自动（点击展开）</h3></summary>
    <div class="mgrid">
      <div class="mcol">
        <div class="mc-h">① Amazon Storefront（硬门槛）</div>
        <div class="mc-concern">客户："必须直链一个成熟、活跃、有组织的 Amazon Storefront，否则不进 Prime Day 名单。"</div>
        <div class="mc-how">→ Playwright 驱动真实 Chrome 打开 bio 链接，渲染后读真实出链找 amazon.com/shop；直链则打开橱窗数商品。</div>
        <div class="mc-auto y">实跑自动 · 有截图证据</div>
      </div>
      <div class="mcol">
        <div class="mc-h">② 视觉真实度（护肤专项）</div>
        <div class="mc-concern">客户："信任建立在真实未修图的近距离皮肤纹理上，排斥重滤镜 / 柔光环形灯。"</div>
        <div class="mc-how">→ 截图候选内容，Claude 视觉模型判断真人出镜真实度 + 内容垂类是否匹配。</div>
        <div class="mc-auto y">实跑自动 · 有截图证据</div>
      </div>
      <div class="mcol">
        <div class="mc-h">③ 内容画像 / 赞助 / 互动 / 评论意图</div>
        <div class="mc-concern">客户：以产品导购为主、赞助不过饱和、高互动、评论区有真实购买意图。</div>
        <div class="mc-how">→ instagrapi 采集近 15 帖 + Top 帖评论，脚本自动算占比 / 互动率 / 购买意图 / 互赞团。</div>
        <div class="mc-auto y">采集即自动</div>
      </div>
      <div class="mcol">
        <div class="mc-h">④ Save 率 / DM 分享</div>
        <div class="mc-concern">客户：高 save 与 DM 分享 = 受众在收藏推荐，关键信任信号。</div>
        <div class="mc-how">→ Instagram 对非本人任何公开页面都不渲染 saves/DM，浏览器也拿不到。</div>
        <div class="mc-auto n">仅 Modash / 创作者后台</div>
      </div>
    </div>
    <div class="mfoot">点开任一候选 → 右侧抽屉「浏览器核验记录与证据」可见现场截图 + 机器抓取数据 + 可复核来源链接（证明非模型臆测）。</div>
  </details>

  <div class="toolbar">
    <div class="tabs">
      <button class="tab active" data-f="all">全部 <span class="badge"></span></button>
      <button class="tab" data-f="include">Include <span class="badge"></span></button>
      <button class="tab" data-f="review">Review <span class="badge"></span></button>
      <button class="tab" data-f="exclude">Exclude <span class="badge"></span></button>
    </div>
    <div class="spacer"></div>
    <div class="search">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/></svg>
      <input id="search" placeholder="搜索 handle 或名字…" autocomplete="off">
    </div>
    <select class="sortsel" id="sortsel">
      <option value="discovery_score">按评分</option>
      <option value="follower_count">按粉丝数</option>
      <option value="best_er">按互动率</option>
      <option value="product_rec_ratio">按产品推荐占比</option>
      <option value="purchase_intent_ratio">按购买意图</option>
    </select>
  </div>

  <div class="tablecard">
    <table>
      <thead><tr>
        <th class="sortable" data-k="handle">创作者</th>
        <th class="num sortable" data-k="follower_count">粉丝 · 档位</th>
        <th class="sortable" data-k="campaign_fit">赛道 · 对口度</th>
        <th>Amazon</th>
        <th class="num sortable" data-k="best_er">互动率</th>
        <th class="sortable" data-k="trust_level">评论信任</th>
        <th class="num sortable" data-k="discovery_score">评分</th>
        <th class="sortable" data-k="status">状态</th>
        <th>待核验 / 原因</th>
      </tr></thead>
      <tbody id="tbody"></tbody>
    </table>
  </div>
</div>

<footer>
  生成于 {gen} · 数据源 {source_name} · 视觉系统 Linear · 共 {s['total_candidates']} 候选（include {s['include']} / review {s['review']} / exclude {s['exclude']}）
</footer>

<div class="scrim" id="scrim"></div>
<aside class="drawer" id="drawer">
  <div class="drawer-head" id="drawerHead"></div>
  <div class="drawer-body" id="drawerBody"></div>
</aside>

<script>const DATA = {data_json};</script>
<script>{JS}</script>
</body></html>"""


def main() -> int:
    ap = argparse.ArgumentParser(description="生成 HTML 发现报告")
    ap.add_argument("--run", type=str, default=None, help="discovery-*.json 路径（默认最新）")
    ap.add_argument("--open", action="store_true", help="生成后用默认浏览器打开")
    args = ap.parse_args()

    run_path = args.run or latest_run()
    data = json.loads(Path(run_path).read_text(encoding="utf-8"))
    run_id = data.get("run_id", "report")
    nverif = attach_verifications(data, run_id)
    if nverif:
        print(f"  已挂载 {nverif} 个候选的浏览器核验记录 + 证据截图")
    html = build_html(data, os.path.basename(run_path))

    out = RUNS_DIR / f"report-{run_id}.html"
    out.write_text(html, encoding="utf-8")
    print(f"✓ 报告已生成: {out}")
    print(f"  候选 {data['summary']['total_candidates']} · include {data['summary']['include']}"
          f" · review {data['summary']['review']} · exclude {data['summary']['exclude']}")

    if args.open:
        import subprocess
        subprocess.run(["open", str(out)], check=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
