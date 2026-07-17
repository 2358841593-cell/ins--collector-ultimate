"""流水线阶段公共骨架（PIPELINE_SPEC.md §6）。

run_browser_stage：认领队列（软锁，断点续跑）→ 逐候选 process_one(pg, cand) → advance/reject/mark_error。
process_one 返回：('advance', to_status, cand) | ('reject', reason) | ('error', err)。
号问题（error）绝不 reject，只 mark_error（status 不动，可重试）——PIPELINE_SPEC 坑C 铁律。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))  # scripts/

from extensions.sop_v2 import creator_cache as cc  # noqa: E402


def apply_verdict(handle: str, verdict: tuple) -> str:
    kind = verdict[0]
    if kind == "advance":
        cc.advance(handle, verdict[1], verdict[2])
    elif kind == "reject":
        # ('reject', reason[, cand])：带 cand 则回写已抓浅扫数据（铁律：淘汰也要留数据进 Exclude 池）
        cc.reject(handle, verdict[1], verdict[2] if len(verdict) > 2 else None)
    else:  # error → 号问题，可重试
        cc.mark_error(handle, verdict[1])
    return kind


def run_browser_stage(from_status, batch_id, limit, resume, process_one, per_account=6,
                      stale_minutes=30, accounts_file=None):
    """浏览器阶段通用循环 + 账号轮换。断点续跑靠 claim_queue 软锁（--resume 复领陈旧锁）。
    accounts_file：可指定隔离号池（如深采专用），None 用默认 accounts_raw.txt。"""
    import browser_collect_v2 as bc
    from playwright.sync_api import sync_playwright

    proxy = bc.load_proxy()
    accts = bc.load_accounts(accounts_file)
    if not accts:
        print(f"✗ 无可用 IG 号（{accounts_file or 'accounts_raw.txt'}）"); return {"error": "no_accounts"}
    print(f"  号池: {accounts_file or 'accounts_raw.txt'}（{len(accts)} 个）", flush=True)
    q = cc.claim_queue(from_status, limit, batch_id, stale_minutes=stale_minutes if resume else 0)
    print(f"[{from_status}→] 认领 {len(q)} 个候选 · 号池 {len(accts)}", flush=True)
    counts = {"advance": 0, "reject": 0, "error": 0}
    with sync_playwright() as pw:
        ai = used = 0
        ctx = pg = None
        for cand in q:
            h = cand["handle"]
            if ctx is None or used >= per_account:
                if ctx:
                    ctx.close()
                acct = accts[ai % len(accts)]; ai += 1; used = 0
                ctx = bc.open_ctx(pw, acct, proxy)
                pg = ctx.pages[0] if ctx.pages else ctx.new_page()
                pg.set_default_timeout(20000)               # 看门狗：任何 Playwright 调用 >20s 失败，
                pg.set_default_navigation_timeout(20000)    # 疲劳号坏加载快速失败，不磨到 45s（修"很慢"）
                print(f"  ↻ 号 {acct['username']}", flush=True)
            used += 1
            try:
                verdict = process_one(pg, cand)
            except Exception as e:  # noqa: BLE001
                verdict = ("error", f"{type(e).__name__}:{str(e)[:50]}")
            kind = apply_verdict(h, verdict)
            counts[kind] += 1
            detail = verdict[1] if kind != "advance" else verdict[2].get("storefront_status", "ok")
            print(f"  [{kind:7}] @{h} · {detail}", flush=True)
            if kind == "error":     # 号问题 → 立刻换号
                if ctx:
                    ctx.close()
                ctx = None
            bc._pause(2, 4)
        if ctx:
            ctx.close()
    print(f"[{from_status}→] advance={counts['advance']} reject={counts['reject']} error={counts['error']}",
          flush=True)
    print("  status:", cc.status_dist(batch_id), flush=True)
    return counts
