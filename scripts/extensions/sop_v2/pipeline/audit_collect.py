"""采集完整性监控 + 回头补采（客户 2026-07-17：抖动失败记下来，回头补，不完整再出来）。

真实重跑教训：代理抖动/限流会让深采"跑了但啥也没拿到"——网格有 12 帖 code 却 0 帖采到、
或有评论却抽 0 条。这类静默不完整不会报错、照样进 collected，交付表只剩空白。
本工具把它们揪出来、记下原因，并可一键退回 qualified 等下一轮 stage3 重采。

用法：
    # 只看监控（不改库）
    cd scripts && ../.venv/bin/python -m extensions.sop_v2.pipeline.audit_collect --batch-id SKIN3-20260717
    # 回头补：把不完整的退回 qualified，然后重跑 stage3 即可
    ... --batch-id SKIN3-20260717 --requeue
    ... --batch-id SKIN3-20260717 --requeue --only "帖子全失败"   # 只补某类失败
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from extensions.sop_v2 import creator_cache as cc  # noqa: E402


class RetryManifestError(ValueError):
    """carryover manifest cannot safely select the retry cohort."""


def _handle(value) -> str:
    return str(value or "").strip().lstrip("@").lower()


def load_handles_file(path: str | Path) -> set[str]:
    """Load an exact handle allowlist from newline text or JSON."""
    p = Path(path)
    try:
        raw = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise RetryManifestError(f"无法读取 handles file：{exc}") from exc

    values = None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        payload = None
    if payload is not None:
        if isinstance(payload, list):
            values = payload
        elif isinstance(payload, dict):
            for key in ("handles", "candidates", "carryover"):
                if isinstance(payload.get(key), list):
                    values = payload[key]
                    break
        if values is None:
            raise RetryManifestError(
                "handles JSON 必须是数组，或含 handles/candidates/carryover 数组"
            )
    else:
        values = [
            line.strip()
            for line in raw.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]

    handles = []
    for index, value in enumerate(values):
        if isinstance(value, dict):
            value = value.get("handle")
        if not isinstance(value, str):
            raise RetryManifestError(f"handles[{index}] 必须是 string 或含 handle")
        handle = _handle(value)
        if not handle:
            raise RetryManifestError(f"handles[{index}] 为空")
        handles.append(handle)
    if not handles:
        raise RetryManifestError("handles file 为空")
    if len(set(handles)) != len(handles):
        raise RetryManifestError("handles file 含重复账号")
    return set(handles)


def retry_handles_for_batch(path: str | Path, batch_id: str | None = None) -> set[str]:
    """Return the exact retry subset, optionally restricted to one immutable origin batch."""
    p = Path(path)
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RetryManifestError(f"无法读取 carryover manifest：{exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise RetryManifestError("carryover manifest schema_version 必须为 1")
    rows = payload.get("carryover")
    raw_retry = payload.get("retry_handles")
    if not isinstance(rows, list) or not isinstance(raw_retry, list):
        raise RetryManifestError("carryover[] / retry_handles[] 缺失")

    by_handle = {}
    marked = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise RetryManifestError(f"carryover[{index}] 必须是 object")
        raw_handle = row.get("handle")
        if not isinstance(raw_handle, str):
            raise RetryManifestError(f"carryover[{index}].handle 必须是 string")
        handle = _handle(raw_handle)
        origin = row.get("origin_batch")
        needs_retry = row.get("needs_pipeline_retry")
        if (
            not handle
            or handle in by_handle
            or not isinstance(origin, str)
            or not origin.strip()
            or not isinstance(needs_retry, bool)
        ):
            raise RetryManifestError(f"carryover[{index}] handle/origin/retry 字段无效")
        by_handle[handle] = origin.strip()
        if needs_retry:
            marked.add(handle)

    if any(not isinstance(value, str) for value in raw_retry):
        raise RetryManifestError("retry_handles[] 必须全部是 string")
    normalized_retry = [_handle(value) for value in raw_retry]
    if any(not handle for handle in normalized_retry):
        raise RetryManifestError("retry_handles[] 含空账号")
    retry_set = set(normalized_retry)
    if len(retry_set) != len(normalized_retry):
        raise RetryManifestError("retry_handles[] 含重复账号")
    if retry_set != marked:
        raise RetryManifestError(
            "retry_handles[] 与 carryover[].needs_pipeline_retry 不一致"
        )
    counts = payload.get("counts")
    if (
        not isinstance(counts, dict)
        or counts.get("carryover") != len(rows)
        or counts.get("pipeline_retry") != len(retry_set)
    ):
        raise RetryManifestError("counts.carryover/pipeline_retry 与清单内容不一致")
    expected_sha = hashlib.sha256(
        "\n".join(sorted(by_handle)).encode("utf-8")
    ).hexdigest()
    if payload.get("carryover_handle_set_sha256") != expected_sha:
        raise RetryManifestError("carryover_handle_set_sha256 与 carryover[] 不一致")
    if batch_id:
        retry_set = {
            handle for handle in retry_set if by_handle.get(handle) == batch_id
        }
    return retry_set


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch-id", default=None, help="限定批次；缺省看全部")
    ap.add_argument("--requeue", action="store_true",
                    help="把不完整/失败的号退回 qualified 等重采（清深采字段，保留浅扫+Modash）")
    ap.add_argument("--only", default=None, help="只处理原因含该关键词的（如 '帖子全失败'）")
    ap.add_argument(
        "--strict",
        action="store_true",
        help="启用严格完整性：检查帖子覆盖、互动指标和评论零样本是否可证明为真实低互动",
    )
    ap.add_argument(
        "--target-posts",
        type=int,
        default=10,
        help="严格完整性要求的帖子数（默认 10）",
    )
    ap.add_argument(
        "--full-deep-all-candidates",
        action="store_true",
        help="在严格检查中也要求 Stage 2 机器淘汰候选完成深采",
    )
    ap.add_argument(
        "--retry-manifest",
        help="只审计/退回 carryover manifest 的 retry_handles，避免重跑已客户终判资产",
    )
    ap.add_argument(
        "--handles-file",
        help="精确账号白名单（逐行文本或 JSON）；与 batch/retry-manifest 取交集",
    )
    args = ap.parse_args()
    if args.target_posts <= 0:
        ap.error("--target-posts 必须为正整数")

    if args.strict or args.full_deep_all_candidates:
        items = cc.incomplete_items(
            args.batch_id,
            strict=True,
            target_posts=args.target_posts,
            require_full_deep=args.full_deep_all_candidates,
        )
    else:
        # Keep the historical call shape for integrations that replace this
        # read-only probe with a one-argument adapter.
        items = cc.incomplete_items(args.batch_id)
    handle_allowlist = None
    if args.handles_file:
        try:
            handle_allowlist = load_handles_file(args.handles_file)
        except RetryManifestError as exc:
            print(f"✗ {exc}")
            return 2
        items = [
            item for item in items
            if _handle(item.get("handle")) in handle_allowlist
        ]
        if args.requeue:
            try:
                cc.validate_recollect_scope(handle_allowlist)
            except ValueError as exc:
                print(f"✗ {exc}")
                return 2
    retry_handles = None
    if args.retry_manifest:
        try:
            retry_handles = retry_handles_for_batch(args.retry_manifest, args.batch_id)
        except RetryManifestError as exc:
            print(f"✗ {exc}")
            return 2
        items = [x for x in items if _handle(x.get("handle")) in retry_handles]
    if args.only:
        items = [x for x in items if any(args.only in r for r in x["reasons"])]

    scope = f"批次 {args.batch_id}" if args.batch_id else "全部批次"
    if retry_handles is not None:
        scope += f" · manifest 精确补采集 {len(retry_handles)}"
    if handle_allowlist is not None:
        scope += f" · handles 白名单 {len(handle_allowlist)}"
    if not items:
        print(f"✓ {scope}：采集完整，无失败/不完整项")
        return 0

    print(f"⚠ {scope}：{len(items)} 个采集不完整/失败\n")
    for x in items:
        print(f"  @{x['handle']:<28} [{x['status']}] {'；'.join(x['reasons'])}")

    # 原因归类看板（哪类失败最多 → 指导修哪里）
    print("\n原因分布：")
    tally = Counter(r.split("(")[0] for x in items for r in x["reasons"])
    for reason, n in tally.most_common():
        print(f"  {reason}: {n}")

    if args.requeue:
        try:
            n = cc.requeue_for_recollect([x["handle"] for x in items])
        except ValueError as exc:
            print(f"\n✗ {exc}")
            return 2
        print(f"\n✓ 已退回 {n} 个 → qualified（深采字段已清，浅扫/Modash 数据保留）")
        print("  下一步：重跑 stage3 即可补采")
        print(f"    ../.venv/bin/python -m extensions.sop_v2.pipeline.stage3_collect --batch-id {args.batch_id}")
    else:
        print("\n（只读监控。加 --requeue 可把这些退回 qualified 等下一轮 stage3 补采）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
