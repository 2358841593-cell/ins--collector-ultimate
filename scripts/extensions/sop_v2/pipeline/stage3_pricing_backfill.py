"""仅补采展示型预估报价，不重跑评论评分，也不改变流水线状态或路由。

候选范围是 carryover manifest 中的未终判账号，与/或 ``--batch-id`` 指定批次中
已 ``decided`` / ``rejected`` / ``collected`` 的账号。已有完整报价的账号自动跳过。

用法：
    cd scripts
    ../.venv/bin/python -m extensions.sop_v2.pipeline.stage3_pricing_backfill \
      --manifest ../data/batches/SKIN4-20260723/carryover_manifest.json \
      --batch-id SKIN4-20260723 --dry-run

    ../.venv/bin/python -m extensions.sop_v2.pipeline.stage3_pricing_backfill \
      --manifest ../data/batches/SKIN4-20260723/carryover_manifest.json \
      --batch-id SKIN4-20260723 --resume

    # 仅重试已有不完整报价；complete 永远跳过，可再加 --handles-file 精确收窄
    ../.venv/bin/python -m extensions.sop_v2.pipeline.stage3_pricing_backfill \
      --manifest ../data/batches/SKIN4-20260723/carryover_manifest.json \
      --batch-id SKIN4-20260723 --retry-incomplete --dry-run
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
import uuid
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from extensions.sop_v2 import creator_cache as cc  # noqa: E402
from extensions.sop_v2.config import load_config  # noqa: E402
from extensions.sop_v2.pipeline._base import (  # noqa: E402
    _select_account_pool,
    _sticky_session,
    nonnegative_int,
)


ELIGIBLE_STATUSES = ("decided", "rejected", "collected")
INCOMPLETE_PRICING_STATUSES = ("missing", "partial", "fallback_modash")
_PRICING_FIELDS = (
    "pricing_reel_samples",
    "pricing_captured_at",
    "pricing_estimate",
)


class PricingBackfillError(ValueError):
    """报价补采范围或 creator cache 不满足安全前置条件。"""


@dataclass(frozen=True)
class ClaimedCandidate:
    handle: str
    status: str
    cand: dict
    lock_token: str


def _handle(value) -> str:
    return str(value or "").strip().lstrip("@").lower()


def _read_handles_file(path: Path) -> set[str]:
    """读取精确账号白名单；支持逐行文本、JSON string[] 或 ``{"handles":[]}``。"""
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise PricingBackfillError(f"无法读取 handles file：{exc}") from exc
    stripped = raw.strip()
    if not stripped:
        raise PricingBackfillError("handles file 为空")

    values = None
    if stripped[0] in "[{":
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise PricingBackfillError(f"handles file JSON 无效：{exc}") from exc
        if isinstance(payload, list):
            values = payload
        elif isinstance(payload, dict):
            values = payload.get("handles")
        if not isinstance(values, list):
            raise PricingBackfillError(
                "handles file JSON 必须是 string[] 或含 handles[] 的 object"
            )
    else:
        values = [
            line.strip()
            for line in raw.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]

    if not values or any(not isinstance(value, str) for value in values):
        raise PricingBackfillError("handles file 必须包含非空 string 账号")
    normalized = [_handle(value) for value in values]
    if any(not value for value in normalized):
        raise PricingBackfillError("handles file 含空账号")
    if len(set(normalized)) != len(normalized):
        raise PricingBackfillError("handles file 含重复账号")
    return set(normalized)


def _read_manifest(path: Path, batch_id: str | None = None) -> dict[str, str]:
    """复用正式 Stage 4 的严格 manifest 合同，避免报价回填静默漏 cohort。"""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PricingBackfillError(f"无法读取 carryover manifest：{exc}") from exc
    inferred_batch = payload.get("next_batch_id") if isinstance(payload, dict) else None
    effective_batch = batch_id or inferred_batch
    if not isinstance(effective_batch, str) or not effective_batch.strip():
        raise PricingBackfillError("carryover manifest 缺少有效 next_batch_id")
    try:
        from extensions.sop_v2.pipeline.stage4_decide import (
            CarryoverManifestError,
            _load_carryover_manifest,
        )

        meta = _load_carryover_manifest(path, effective_batch.strip())
    except CarryoverManifestError as exc:
        raise PricingBackfillError(str(exc)) from exc
    return {
        row["handle"]: row["origin_batch"]
        for row in meta["rows"]
    }


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _stage_json(row: sqlite3.Row) -> dict:
    try:
        value = json.loads(row["stage_json"]) if row["stage_json"] else {}
    except (TypeError, json.JSONDecodeError):
        value = {}
    return value if isinstance(value, dict) else {}


def _should_collect_pricing(cand: dict, retry_incomplete: bool = False) -> bool:
    """默认只补从未尝试；显式重试时额外纳入三种不完整结果，永不重跑 complete。"""
    estimate = cand.get("pricing_estimate")
    if not isinstance(estimate, dict):
        return True
    status = estimate.get("status")
    if status == "complete":
        return False
    if status in INCOMPLETE_PRICING_STATUSES:
        return retry_incomplete
    return True


def _eligible_rows(
    conn: sqlite3.Connection,
    *,
    batch_id: str | None,
    manifest_handles: dict[str, str],
    allowed_handles: set[str],
    retry_incomplete: bool,
) -> list[sqlite3.Row]:
    clauses = []
    params: list[str] = list(ELIGIBLE_STATUSES)
    if batch_id:
        clauses.append("discovery_batch=?")
        params.append(batch_id)
    if manifest_handles:
        clauses.append(
            f"lower(handle) IN ({','.join('?' * len(manifest_handles))})"
        )
        params.extend(sorted(manifest_handles))
    if allowed_handles and not clauses:
        clauses.append(
            f"lower(handle) IN ({','.join('?' * len(allowed_handles))})"
        )
        params.extend(sorted(allowed_handles))
    if not clauses:
        raise PricingBackfillError(
            "必须提供 --manifest、--batch-id 或 --handles-file"
        )

    query = (
        "SELECT handle,status,discovery_batch,client_status,locked_at,stage_updated_at,"
        "times_seen,storefront_status,stage_json "
        "FROM creator_profiles "
        f"WHERE status IN ({','.join('?' * len(ELIGIBLE_STATUSES))}) "
        f"AND ({' OR '.join(clauses)}) "
    )
    if allowed_handles and (batch_id or manifest_handles):
        query += (
            f"AND lower(handle) IN ({','.join('?' * len(allowed_handles))}) "
        )
        params.extend(sorted(allowed_handles))
    query += (
        "ORDER BY (storefront_status='confirmed_yes') DESC,"
        "times_seen DESC,stage_updated_at ASC,lower(handle) ASC"
    )
    rows = conn.execute(query, params).fetchall()

    if manifest_handles:
        db_rows = conn.execute(
            "SELECT handle,discovery_batch,client_status FROM creator_profiles "
            f"WHERE lower(handle) IN ({','.join('?' * len(manifest_handles))})",
            sorted(manifest_handles),
        ).fetchall()
        found = {_handle(row["handle"]): row for row in db_rows}
        missing = sorted(set(manifest_handles) - set(found))
        mismatched = [
            handle
            for handle, origin in manifest_handles.items()
            if handle in found and found[handle]["discovery_batch"] != origin
        ]
        client_final = [
            handle
            for handle, row in found.items()
            if row["client_status"] in ("approved", "rejected", "collaborated")
        ]
        if missing or mismatched or client_final:
            raise PricingBackfillError(
                "manifest 与 creator cache 不一致；"
                f"missing={missing[:5]} origin_mismatch={mismatched[:5]} "
                f"client_final={client_final[:5]}"
            )
    if allowed_handles:
        db_rows = conn.execute(
            "SELECT handle,status,discovery_batch,client_status "
            "FROM creator_profiles "
            f"WHERE lower(handle) IN ({','.join('?' * len(allowed_handles))})",
            sorted(allowed_handles),
        ).fetchall()
        found = {_handle(row["handle"]): row for row in db_rows}
        missing = sorted(allowed_handles - set(found))
        client_final = sorted(
            handle
            for handle, row in found.items()
            if row["client_status"] in ("approved", "rejected", "collaborated")
        )
        wrong_status = sorted(
            handle
            for handle, row in found.items()
            if row["status"] not in ELIGIBLE_STATUSES
        )
        outside_scope = []
        if batch_id or manifest_handles:
            outside_scope = sorted(
                handle
                for handle, row in found.items()
                if not (
                    (batch_id and row["discovery_batch"] == batch_id)
                    or handle in manifest_handles
                )
            )
        if missing or client_final or wrong_status or outside_scope:
            raise PricingBackfillError(
                "handles file 与安全范围不一致；"
                f"missing={missing[:5]} client_final={client_final[:5]} "
                f"wrong_status={wrong_status[:5]} "
                f"outside_scope={outside_scope[:5]}"
            )
    return [
        row
        for row in rows
        if row["client_status"] not in ("approved", "rejected", "collaborated")
        and _should_collect_pricing(
            _stage_json(row), retry_incomplete=retry_incomplete
        )
    ]


def claim_candidates(
    db_path: Path,
    *,
    batch_id: str | None = None,
    manifest_handles: dict[str, str] | None = None,
    allowed_handles: set[str] | None = None,
    retry_incomplete: bool = False,
    limit: int = 0,
    resume: bool = False,
    stale_minutes: int = 30,
    dry_run: bool = False,
) -> list[ClaimedCandidate]:
    """选择并可选软锁报价补采队列；dry-run 绝不写库。"""
    manifest_handles = manifest_handles or {}
    allowed_handles = allowed_handles or set()
    conn = _connect(db_path)
    try:
        if not dry_run:
            conn.execute("BEGIN IMMEDIATE")
        rows = _eligible_rows(
            conn,
            batch_id=batch_id,
            manifest_handles=manifest_handles,
            allowed_handles=allowed_handles,
            retry_incomplete=retry_incomplete,
        )
        cutoff = time.strftime(
            "%Y-%m-%dT%H:%M:%S",
            time.localtime(time.time() - max(0, stale_minutes) * 60),
        )
        rows = [
            row
            for row in rows
            if row["locked_at"] is None
            or (resume and str(row["locked_at"]) < cutoff)
        ]
        if limit:
            rows = rows[:limit]

        claimed = []
        for row in rows:
            token = (
                str(row["locked_at"])
                if dry_run
                else datetime.now().astimezone().isoformat(timespec="microseconds")
                + "-"
                + uuid.uuid4().hex[:8]
            )
            if not dry_run:
                changed = conn.execute(
                    "UPDATE creator_profiles SET locked_at=? "
                    "WHERE handle=? AND status=? AND "
                    "(locked_at IS NULL OR locked_at < ?)",
                    (token, row["handle"], row["status"], cutoff),
                ).rowcount
                if changed != 1:
                    continue
            cand = _stage_json(row)
            cand["handle"] = row["handle"]
            claimed.append(
                ClaimedCandidate(row["handle"], row["status"], cand, token)
            )
        if dry_run:
            conn.rollback()
        else:
            conn.commit()
        return claimed
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def save_pricing(
    db_path: Path, claimed: ClaimedCandidate, enriched: dict
) -> bool:
    """只合并 pricing_* 三字段；status/route/评论/热列及 stage_error 均保持原值。"""
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT stage_json,locked_at,status,client_status "
            "FROM creator_profiles WHERE handle=?",
            (claimed.handle,),
        ).fetchone()
        if (
            not row
            or row["locked_at"] != claimed.lock_token
            or row["status"] != claimed.status
            or row["client_status"] in ("approved", "rejected", "collaborated")
        ):
            conn.rollback()
            return False
        current = _stage_json(row)
        for field in _PRICING_FIELDS:
            if field in enriched:
                current[field] = enriched[field]
        changed = conn.execute(
            "UPDATE creator_profiles SET stage_json=?,locked_at=NULL "
            "WHERE handle=? AND locked_at=? AND status=? "
            "AND (client_status IS NULL OR client_status NOT IN "
            "('approved','rejected','collaborated'))",
            (
                json.dumps(current, ensure_ascii=False, default=str),
                claimed.handle,
                claimed.lock_token,
                claimed.status,
            ),
        ).rowcount
        conn.commit()
        return changed == 1
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def release_claim(db_path: Path, claimed: ClaimedCandidate) -> None:
    """失败只释放本工具自己的锁，不污染通用 stage_error。"""
    conn = _connect(db_path)
    try:
        conn.execute(
            "UPDATE creator_profiles SET locked_at=NULL "
            "WHERE handle=? AND locked_at=?",
            (claimed.handle, claimed.lock_token),
        )
        conn.commit()
    finally:
        conn.close()


def refresh_claims(
    db_path: Path, claims: list[ClaimedCandidate]
) -> tuple[list[ClaimedCandidate], list[ClaimedCandidate]]:
    """刷新全部待处理 token，防止长队列在运行中被 ``--resume`` 重复接管。

    同时重新核验业务状态和客户终判；已丢锁/已终判的候选不会再发网络请求。
    """
    if not claims:
        return [], []
    conn = _connect(db_path)
    refreshed, lost = [], []
    try:
        conn.execute("BEGIN IMMEDIATE")
        for claimed in claims:
            row = conn.execute(
                "SELECT status,client_status,locked_at FROM creator_profiles "
                "WHERE handle=?",
                (claimed.handle,),
            ).fetchone()
            valid = (
                row
                and row["status"] == claimed.status
                and row["locked_at"] == claimed.lock_token
                and row["client_status"]
                not in ("approved", "rejected", "collaborated")
            )
            if not valid:
                if row and row["locked_at"] == claimed.lock_token:
                    conn.execute(
                        "UPDATE creator_profiles SET locked_at=NULL "
                        "WHERE handle=? AND locked_at=?",
                        (claimed.handle, claimed.lock_token),
                    )
                lost.append(claimed)
                continue
            token = (
                datetime.now().astimezone().isoformat(timespec="microseconds")
                + "-"
                + uuid.uuid4().hex[:8]
            )
            changed = conn.execute(
                "UPDATE creator_profiles SET locked_at=? "
                "WHERE handle=? AND locked_at=? AND status=? "
                "AND (client_status IS NULL OR client_status NOT IN "
                "('approved','rejected','collaborated'))",
                (token, claimed.handle, claimed.lock_token, claimed.status),
            ).rowcount
            if changed == 1:
                refreshed.append(replace(claimed, lock_token=token))
            else:
                lost.append(claimed)
        conn.commit()
        return refreshed, lost
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def run_backfill(
    queue: list[ClaimedCandidate],
    *,
    db_path: Path,
    accounts_file: str | None,
    per_account: int,
    account_offset: int = 0,
    account_count: int = 0,
) -> dict[str, int]:
    """在独立 IG 浏览器会话中补报价；不调用业务状态机。"""
    import browser_collect_v2 as bc
    from playwright.sync_api import sync_playwright

    try:
        all_accts = bc.load_accounts(accounts_file)
        accts = _select_account_pool(
            all_accts,
            account_offset=account_offset,
            account_count=account_count,
        )
    except OSError as exc:
        accts = []
        print(f"✗ 读取 IG 号池失败：{exc}")
    except ValueError as exc:
        accts = []
        print(f"✗ {exc}")
    if not accts:
        for claimed in queue:
            release_claim(db_path, claimed)
        print(f"✗ 无可用 IG 号（{accounts_file or 'accounts_raw.txt'}）")
        return {
            "complete": 0,
            "partial": 0,
            "fallback_modash": 0,
            "missing": 0,
            "error": len(queue),
        }

    counts = {
        "complete": 0,
        "partial": 0,
        "fallback_modash": 0,
        "missing": 0,
        "error": 0,
    }
    run_token = uuid.uuid4().hex[:6]
    with sync_playwright() as pw:
        ai = used = 0
        ctx = pg = None
        try:
            pending = list(queue)
            active_claim = None
            while pending:
                pending, lost = refresh_claims(db_path, pending)
                counts["error"] += len(lost)
                for item in lost:
                    print(
                        f"  [error   ] @{item.handle} · claim_lost_or_client_final",
                        flush=True,
                    )
                if not pending:
                    break
                claimed = pending.pop(0)
                active_claim = claimed
                try:
                    if ctx is None or used >= per_account:
                        if ctx:
                            bc.close_ctx(ctx)
                        acct = accts[ai % len(accts)]
                        ai += 1
                        used = 0
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
                    state, error = bc.collect_pricing_evidence(
                        pg, claimed.cand
                    )
                    if state is None:
                        raise RuntimeError(error or "pricing_no_output")
                    if not save_pricing(db_path, claimed, claimed.cand):
                        raise RuntimeError("pricing_claim_lost")
                    status = (
                        claimed.cand.get("pricing_estimate") or {}
                    ).get("status", "missing")
                    if status not in (
                        "complete",
                        "partial",
                        "fallback_modash",
                        "missing",
                    ):
                        status = "missing"
                    counts[status] += 1
                    print(
                        f"  [{status:8}] @{claimed.handle} "
                        f"· 业务状态保持 {claimed.status}",
                        flush=True,
                    )
                except Exception as exc:  # noqa: BLE001
                    release_claim(db_path, claimed)
                    counts["error"] += 1
                    print(
                        f"  [error   ] @{claimed.handle} · "
                        f"{type(exc).__name__}:{str(exc)[:80]}",
                        flush=True,
                    )
                    if ctx:
                        bc.close_ctx(ctx)
                    ctx = pg = None
                active_claim = None
                bc._pause(2, 4)
        finally:
            if ctx:
                bc.close_ctx(ctx)
            # Ctrl-C 等异常时也只释放仍由本次队列持有的锁。
            if active_claim:
                release_claim(db_path, active_claim)
            for claimed in pending:
                release_claim(db_path, claimed)
    return counts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--manifest",
        help="carryover manifest；只补其中尚未客户终判的候选",
    )
    ap.add_argument(
        "--batch-id",
        help="同时纳入该 discovery_batch 的候选；与 manifest 并用时取并集",
    )
    ap.add_argument(
        "--handles-file",
        help="精确账号白名单（逐行文本、JSON string[] 或 {handles:[]}）；与批次/manifest 取交集",
    )
    ap.add_argument(
        "--retry-incomplete",
        action="store_true",
        help="显式重试 missing/partial/fallback_modash；complete 永远不重跑",
    )
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument(
        "--resume",
        action="store_true",
        help="允许接管超过 --stale-minutes 的陈旧软锁",
    )
    ap.add_argument("--stale-minutes", type=int, default=30)
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="只列将补采的候选，不加锁、不启动浏览器、不写数据库",
    )
    ap.add_argument("--accounts-file", default=None)
    ap.add_argument(
        "--account-offset",
        type=nonnegative_int,
        default=0,
        help="从报价补采号池该下标开始轮换（并行批次应使用不同偏移）",
    )
    ap.add_argument(
        "--account-count",
        type=nonnegative_int,
        default=0,
        help="仅使用 offset 起连续 N 个账号；0 表示使用整个池",
    )
    ap.add_argument("--db", default=str(cc.DB), help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    if not args.manifest and not args.batch_id and not args.handles_file:
        ap.error("必须提供 --manifest、--batch-id 或 --handles-file")
    if args.limit < 0:
        ap.error("--limit 不能为负数")
    if args.stale_minutes < 0:
        ap.error("--stale-minutes 不能为负数")

    try:
        manifest_handles = (
            _read_manifest(Path(args.manifest), args.batch_id)
            if args.manifest
            else {}
        )
        allowed_handles = (
            _read_handles_file(Path(args.handles_file))
            if args.handles_file
            else set()
        )
        queue = claim_candidates(
            Path(args.db),
            batch_id=args.batch_id,
            manifest_handles=manifest_handles,
            allowed_handles=allowed_handles,
            retry_incomplete=args.retry_incomplete,
            limit=args.limit,
            resume=args.resume,
            stale_minutes=args.stale_minutes,
            dry_run=args.dry_run,
        )
    except (PricingBackfillError, sqlite3.Error) as exc:
        print(f"✗ {exc}")
        return 2

    base_scope = (
        f"manifest {len(manifest_handles)} + batch {args.batch_id}"
        if args.manifest and args.batch_id
        else (
            f"manifest {len(manifest_handles)}"
            if args.manifest
            else (
                f"batch {args.batch_id}"
                if args.batch_id
                else "handles-only"
            )
        )
    )
    scope = (
        f"{base_scope} ∩ handles {len(allowed_handles)}"
        if allowed_handles and (args.manifest or args.batch_id)
        else (
            f"handles {len(allowed_handles)}"
            if allowed_handles
            else base_scope
        )
    )
    retry_txt = (
        " · 显式重试 missing/partial/fallback"
        if args.retry_incomplete
        else ""
    )
    print(
        f"[pricing-only] {scope} · 待补 {len(queue)} "
        f"· complete/客户终判/非目标状态自动跳过{retry_txt}",
        flush=True,
    )
    if args.dry_run:
        for item in queue:
            print(f"  @{item.handle} [{item.status}]")
        print("✓ dry-run：数据库未修改，浏览器未启动")
        return 0
    if not queue:
        return 0

    cfg = load_config()
    pipeline = cfg.get("pipeline", {})
    root = Path(__file__).resolve().parents[3].parent
    accounts_file = args.accounts_file
    if accounts_file is None:
        deep = pipeline.get("deep_accounts_file")
        accounts_file = (
            str(root / deep) if deep and (root / deep).exists() else None
        )
    counts = run_backfill(
        queue,
        db_path=Path(args.db),
        accounts_file=accounts_file,
        per_account=max(1, int(pipeline.get("collect_per_account", 5))),
        account_offset=args.account_offset,
        account_count=args.account_count,
    )
    print(
        f"[pricing-only] complete={counts['complete']} "
        f"partial={counts['partial']} "
        f"fallback={counts['fallback_modash']} "
        f"missing={counts['missing']} error={counts['error']}",
        flush=True,
    )
    return 1 if counts["error"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
