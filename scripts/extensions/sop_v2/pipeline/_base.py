"""流水线阶段公共骨架（PIPELINE_SPEC.md §6）。

run_browser_stage：认领队列（软锁，断点续跑）→ 逐候选 process_one(pg, cand) → advance/reject/mark_error。
process_one 返回：('advance', to_status, cand) | ('reject', reason) |
('error', err[, partial_cand])。
号问题（error）绝不 reject，只 mark_error（status 不动，可重试）——PIPELINE_SPEC 坑C 铁律。
"""
from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))  # scripts/

from extensions.sop_v2 import creator_cache as cc  # noqa: E402


def nonnegative_int(value: str) -> int:
    """argparse type：账号偏移只能是非负整数。"""
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("必须是非负整数") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("必须是非负整数")
    return parsed


def _account_index(pool_size: int, block_index: int, account_offset: int = 0) -> int:
    """把账号块序号映射到轮换池；offset 超过池大小时循环。"""
    if account_offset < 0:
        raise ValueError("account_offset 必须是非负整数")
    if pool_size <= 0:
        raise ValueError("pool_size 必须为正整数")
    return (account_offset + block_index) % pool_size


def _select_account_pool(
    accounts: list[dict], account_offset: int = 0, account_count: int = 0
) -> list[dict]:
    """选择本任务独占的账号子池。

    count=0：从 offset 起旋转整个池，保持历史的全池循环语义。
    count>0：严格取连续切片 [offset, offset+count)，不跨池尾回绕。
    """
    if account_offset < 0 or account_count < 0:
        raise ValueError("account_offset/account_count 必须是非负整数")
    if not accounts:
        return []
    if account_count == 0:
        start = account_offset % len(accounts)
        return accounts[start:] + accounts[:start]
    end = account_offset + account_count
    if account_offset >= len(accounts) or end > len(accounts):
        raise ValueError(
            f"账号子池越界：pool={len(accounts)} offset={account_offset} "
            f"count={account_count}"
        )
    return accounts[account_offset:end]


def _sticky_session(run_token: str, block_index: int, username: str) -> str:
    """Stable inside one account block, different across pipeline invocations."""
    token = "".join(ch for ch in str(run_token) if ch.isalnum())[:6] or "run"
    user = "".join(ch for ch in str(username) if ch.isalnum())[:4] or "acct"
    return f"{token}b{int(block_index)}{user}"[:16]


def apply_verdict(
    handle: str, verdict: tuple, claim: dict | None = None
) -> str:
    kind = verdict[0]
    if kind == "advance":
        cc.advance(handle, verdict[1], verdict[2])
    elif kind == "reject":
        # ('reject', reason[, cand])：带 cand 则回写已抓浅扫数据（铁律：淘汰也要留数据进 Exclude 池）
        cc.reject(handle, verdict[1], verdict[2] if len(verdict) > 2 else None)
    else:  # error → 号问题，可重试
        partial_cand = verdict[2] if len(verdict) > 2 else None
        if partial_cand is None and not claim:
            # 保留直接调用 apply_verdict 的历史接口。
            cc.mark_error(handle, verdict[1])
        else:
            claim = claim or {}
            cc.mark_error(
                handle,
                verdict[1],
                partial_cand,
                expected_status=claim.get("_queue_from_status"),
                lock_token=claim.get("_queue_lock_token"),
            )
    return kind


def run_browser_stage(from_status, batch_id, limit, resume, process_one, per_account=6,
                      stale_minutes=30, accounts_file=None, account_offset=0,
                      account_count=0):
    """浏览器阶段通用循环 + 账号轮换。断点续跑靠 claim_queue 软锁（--resume 复领陈旧锁）。
    accounts_file：可指定隔离号池（如深采专用），None 用默认 accounts_raw.txt。
    account_offset/account_count：选择独立账号子池；count=0 表示从 offset 起用整个池。"""
    import browser_collect_v2 as bc
    from playwright.sync_api import sync_playwright

    all_accts = bc.load_accounts(accounts_file)
    if not all_accts:
        print(f"✗ 无可用 IG 号（{accounts_file or 'accounts_raw.txt'}）"); return {"error": "no_accounts"}
    try:
        accts = _select_account_pool(
            all_accts, account_offset=account_offset, account_count=account_count
        )
    except ValueError as exc:
        print(f"✗ {exc}")
        return {"error": "invalid_account_slice"}
    print(
        f"  号池: {accounts_file or 'accounts_raw.txt'}"
        f"（选用 {len(accts)}/{len(all_accts)} 个，offset={account_offset}, "
        f"count={account_count or 'all'}）· 代理 sticky（一账号块一 IP）",
        flush=True,
    )
    q = cc.claim_queue(
        from_status,
        limit,
        batch_id,
        stale_minutes=stale_minutes if resume else None,
    )
    print(f"[{from_status}→] 认领 {len(q)} 个候选 · 号池 {len(accts)}", flush=True)
    counts = {"advance": 0, "reject": 0, "error": 0}
    run_token = uuid.uuid4().hex[:6]
    with sync_playwright() as pw:
        ai = used = 0
        ctx = pg = None
        pending = list(q)
        active_claim = None
        try:
            while pending:
                # 长队列可能运行数小时：每完成一个候选就给剩余锁续租。CAS 失败说明
                # 已被其他 worker 接管，本 worker 立即放弃该项，避免重复采集。
                pending, lost = cc.refresh_queue_locks(pending)
                for item in lost:
                    counts["error"] += 1
                    print(
                        f"  [error  ] @{item.get('handle')} · queue_claim_lost",
                        flush=True,
                    )
                if not pending:
                    break
                cand = pending.pop(0)
                active_claim = {
                    "handle": cand.get("handle"),
                    "_queue_lock_token": cand.get("_queue_lock_token"),
                    "_queue_from_status": cand.get("_queue_from_status"),
                }
                cand.pop("_queue_lock_token", None)
                cand.pop("_queue_from_status", None)
                h = cand["handle"]
                try:
                    if ctx is None or used >= per_account:
                        if ctx:
                            bc.close_ctx(ctx)
                        acct = accts[ai % len(accts)]
                        ai += 1
                        used = 0
                        # 每个账号块一条独立 sticky 通道 → 一个固定出口 IP 跑完整块。
                        proxy = bc.load_proxy(
                            session=_sticky_session(
                                run_token, ai, acct["username"]
                            )
                        )
                        ctx = bc.open_ctx(pw, acct, proxy)
                        pg = ctx.pages[0] if ctx.pages else ctx.new_page()
                        pg.set_default_timeout(20000)
                        pg.set_default_navigation_timeout(20000)
                        print(f"  ↻ 号 {acct['username']}", flush=True)
                    used += 1
                    verdict = process_one(pg, cand)
                except Exception as e:  # noqa: BLE001
                    verdict = ("error", f"{type(e).__name__}:{str(e)[:50]}")
                kind = apply_verdict(h, verdict, claim=active_claim)
                counts[kind] += 1
                detail = (
                    verdict[1]
                    if kind != "advance"
                    else verdict[2].get("storefront_status", "ok")
                )
                print(f"  [{kind:7}] @{h} · {detail}", flush=True)
                active_claim = None
                if kind == "error":     # 号问题 → 立刻换号
                    if ctx:
                        bc.close_ctx(ctx)
                    ctx = pg = None
                bc._pause(2, 4)
        finally:
            if ctx:
                bc.close_ctx(ctx)
            release = list(pending)
            if active_claim:
                release.append(active_claim)
            cc.release_queue_locks(release)
    print(f"[{from_status}→] advance={counts['advance']} reject={counts['reject']} error={counts['error']}",
          flush=True)
    print("  status:", cc.status_dist(batch_id), flush=True)
    return counts
