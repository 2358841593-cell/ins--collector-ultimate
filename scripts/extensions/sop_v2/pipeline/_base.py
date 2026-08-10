"""流水线阶段公共骨架（PIPELINE_SPEC.md §6）。

run_browser_stage：认领队列（软锁，断点续跑）→ 逐候选 process_one(pg, cand) → advance/reject/mark_error。
process_one 返回：('advance', to_status, cand) | ('reject', reason) |
('error', err[, partial_cand])。
号问题（error）绝不 reject，只 mark_error（status 不动，可重试）——PIPELINE_SPEC 坑C 铁律。
"""
from __future__ import annotations

import argparse
import signal
import sys
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))  # scripts/

from extensions.sop_v2 import creator_cache as cc  # noqa: E402
from extensions.sop_v2.pipeline.deep_attempts import (  # noqa: E402
    finalize_stage3_verdict,
    prepare_stage3_attempt,
)


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
    accounts: list[dict], account_offset: int = 0, account_count: int = 0,
    account_rotation: int = 0,
) -> list[dict]:
    """选择本任务独占的账号子池。

    count=0：从 offset 起旋转整个池，保持历史的全池循环语义。
    count>0：严格取连续切片 [offset, offset+count)，不跨池尾回绕。
    rotation：只在上述已选子池内部轮换，不会改变 worker 的隔离边界。
    """
    if account_offset < 0 or account_count < 0 or account_rotation < 0:
        raise ValueError(
            "account_offset/account_count/account_rotation 必须是非负整数"
        )
    if not accounts:
        return []
    if account_count == 0:
        start = account_offset % len(accounts)
        selected = accounts[start:] + accounts[:start]
    else:
        end = account_offset + account_count
        if account_offset >= len(accounts) or end > len(accounts):
            raise ValueError(
                f"账号子池越界：pool={len(accounts)} offset={account_offset} "
                f"count={account_count}"
            )
        selected = accounts[account_offset:end]
    rotation = account_rotation % len(selected)
    return selected[rotation:] + selected[:rotation]


def _sticky_session(run_token: str, block_index: int, username: str) -> str:
    """Stable inside one account block, different across pipeline invocations."""
    token = "".join(ch for ch in str(run_token) if ch.isalnum())[:6] or "run"
    user = "".join(ch for ch in str(username) if ch.isalnum())[:4] or "acct"
    return f"{token}b{int(block_index)}{user}"[:16]


def apply_verdict(
    handle: str, verdict: tuple, claim: dict | None = None
) -> str:
    """提交候选 verdict；claim 路径的 CAS 失败返回 ``queue_claim_lost``。"""
    kind = verdict[0]
    claim_kwargs = {}
    if claim:
        expected_status = claim.get("_queue_from_status")
        lock_token = claim.get("_queue_lock_token")
        if not expected_status or not lock_token:
            return "queue_claim_lost"
        claim_kwargs = {
            "expected_status": expected_status,
            "lock_token": lock_token,
        }
    if kind == "advance":
        written = cc.advance(handle, verdict[1], verdict[2], **claim_kwargs)
    elif kind == "reject":
        # ('reject', reason[, cand])：带 cand 则回写已抓浅扫数据（铁律：淘汰也要留数据进 Exclude 池）
        written = cc.reject(
            handle,
            verdict[1],
            verdict[2] if len(verdict) > 2 else None,
            **claim_kwargs,
        )
    else:  # error → 号问题，可重试
        partial_cand = verdict[2] if len(verdict) > 2 else None
        if partial_cand is None and not claim:
            # 保留直接调用 apply_verdict 的历史接口。
            written = cc.mark_error(handle, verdict[1])
        else:
            written = cc.mark_error(
                handle,
                verdict[1],
                partial_cand,
                **claim_kwargs,
            )
    return kind if written else "queue_claim_lost"


def _heartbeat_interval_seconds(stale_minutes: int | float) -> float:
    """续租周期不超过 60 秒，且至多使用 lease 窗口的三分之一。"""
    lease_seconds = max(0.75, float(stale_minutes) * 60.0)
    return max(0.25, min(60.0, lease_seconds / 3.0))


class _QueueClaimHeartbeat:
    """在候选长耗时处理期间，独立续租当前 claim。"""

    def __init__(self, claim: dict, interval_seconds: float):
        self.claim = claim
        self.interval_seconds = max(0.001, float(interval_seconds))
        self.lost = False
        self.error: Exception | None = None
        self._stop = threading.Event()
        handle = str(claim.get("handle") or "unknown")[:24]
        self._thread = threading.Thread(
            target=self._run,
            name=f"queue-heartbeat-{handle}",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def stop_and_join(self) -> None:
        self._stop.set()
        self._thread.join()

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                kept, lost = cc.refresh_queue_locks([self.claim])
            except Exception as exc:  # noqa: BLE001
                # 无法证明 lease 仍归本 worker 时采用 fail-closed：最终结果不落库。
                self.error = exc
                self.lost = True
                return
            if lost or not kept:
                self.lost = True
                return
            # creator_cache 的正式实现原地换 token；兼容返回新 dict 的替身实现。
            renewed = kept[0]
            self.claim["_queue_lock_token"] = renewed.get("_queue_lock_token")
            self.claim["_queue_from_status"] = renewed.get("_queue_from_status")


class _StageTermination(BaseException):
    """SIGTERM converted to stack unwinding so queue cleanup always executes."""

    def __init__(self, signum: int):
        super().__init__(f"terminated by signal {signum}")
        self.signum = signum


@contextmanager
def _graceful_sigterm():
    """Locally turn SIGTERM into a non-swallowed BaseException on main thread."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = signal.getsignal(signal.SIGTERM)

    def terminate(signum, _frame):
        raise _StageTermination(signum)

    signal.signal(signal.SIGTERM, terminate)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def run_browser_stage(from_status, batch_id, limit, resume, process_one, per_account=6,
                      stale_minutes=30, accounts_file=None, account_offset=0,
                      account_count=0, heartbeat_seconds=None,
                      account_rotation=0):
    """浏览器阶段通用循环 + 账号轮换。断点续跑靠 claim_queue 软锁（--resume 复领陈旧锁）。
    accounts_file：可指定隔离号池（如深采专用），None 用默认 accounts_raw.txt。
    account_offset/account_count：选择独立账号子池；count=0 表示从 offset 起用整个池。
    account_rotation：只轮换选中子池的起始账号，不改变子池成员。"""
    import browser_collect_v2 as bc
    from playwright.sync_api import sync_playwright

    all_accts = bc.load_accounts(accounts_file)
    if not all_accts:
        print(f"✗ 无可用 IG 号（{accounts_file or 'accounts_raw.txt'}）"); return {"error": "no_accounts"}
    try:
        accts = _select_account_pool(
            all_accts,
            account_offset=account_offset,
            account_count=account_count,
            account_rotation=account_rotation,
        )
    except ValueError as exc:
        print(f"✗ {exc}")
        return {"error": "invalid_account_slice"}
    print(
        f"  号池: {accounts_file or 'accounts_raw.txt'}"
        f"（选用 {len(accts)}/{len(all_accts)} 个，offset={account_offset}, "
        f"count={account_count or 'all'}, rotation={account_rotation}）"
        "· 代理 sticky（一账号块一 IP）",
        flush=True,
    )
    counts = {"advance": 0, "reject": 0, "error": 0}
    run_token = uuid.uuid4().hex[:6]
    pending = []
    active_claim = None
    ctx = pg = None
    with _graceful_sigterm():
        try:
            q = cc.claim_queue(
                from_status,
                limit,
                batch_id,
                stale_minutes=stale_minutes if resume else None,
            )
            pending = list(q)
            print(
                f"[{from_status}→] 认领 {len(q)} 个候选 · 号池 {len(accts)}",
                flush=True,
            )
            with sync_playwright() as pw:
                ai = used = 0
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
                    attempt_context = None
                    if from_status == "qualified":
                        # deep_collect 原地写 cand；先冻结已落库 canonical，再只改工作副本。
                        attempt_context = prepare_stage3_attempt(cand)
                        cand = attempt_context.candidate
                    h = cand["handle"]
                    heartbeat = _QueueClaimHeartbeat(
                        active_claim,
                        heartbeat_seconds
                        if heartbeat_seconds is not None
                        else _heartbeat_interval_seconds(stale_minutes),
                    )
                    try:
                        heartbeat.start()
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
                            verdict = (
                                "error",
                                f"{type(e).__name__}:{str(e)[:50]}",
                            )
                        if attempt_context is not None:
                            try:
                                verdict = finalize_stage3_verdict(
                                    attempt_context, verdict
                                )
                            except Exception as exc:  # noqa: BLE001
                                # 证据选择器自身故障只让当前候选失败；持久化 pre-run
                                # snapshot，绝不能用已原地修改的 cand 覆盖 canonical。
                                verdict = (
                                    "error",
                                    "attempt_finalize_failed:"
                                    f"{type(exc).__name__}:{str(exc)[:48]}",
                                    attempt_context.prior_snapshot,
                                )
                    finally:
                        # 先停止并 join，确保没有 heartbeat 与最终 CAS 并发换 token。
                        heartbeat.stop_and_join()
                    kind = (
                        "queue_claim_lost"
                        if heartbeat.lost
                        else apply_verdict(h, verdict, claim=active_claim)
                    )
                    if kind == "queue_claim_lost":
                        counts["error"] += 1
                        print(f"  [error  ] @{h} · queue_claim_lost", flush=True)
                        # CAS 清理：若已被接管则不会释放新 owner 的锁。
                        cc.release_queue_locks([active_claim])
                    else:
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
