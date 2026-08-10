"""Offline LLM translation backfill for already-collected comment evidence.

This command never opens Instagram and never touches ``creator_cache.db``.  It
is intended for carry-over candidates that will not pass through Stage 3 again.
The output JSON can be audited or supplied to the delivery exporters directly;
Stage 4 exposes the same engine through ``--strict-comment-translations`` and
persists successful active-candidate data through its normal atomic advance.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from extensions.sop_v2 import comment_translation  # noqa: E402


def _candidate_list(document) -> list[dict]:
    if isinstance(document, list):
        rows = document
    elif isinstance(document, dict):
        rows = document.get("candidates")
        if rows is None:
            rows = document.get("decisions")
    else:
        rows = None
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("输入必须是 candidate list 或含 candidates[]/decisions[] 的 object")
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--provider", choices=["ollama", "anthropic"], default="ollama")
    ap.add_argument("--model", default=None)
    ap.add_argument("--api-url", default=None)
    ap.add_argument("--batch-size", type=int, default=40)
    ap.add_argument(
        "--source-limit",
        "--display-limit",
        dest="source_limit",
        type=int,
        default=120,
        help="每个候选送 LLM 的已存评论上限；旧参数名仍兼容",
    )
    ap.add_argument("--strict", action="store_true",
                    help="失败或历史原文缺失时写出审计结果但返回非零，不可作为正式交付")
    args = ap.parse_args(argv)
    if args.batch_size <= 0 or args.source_limit <= 0:
        ap.error("--batch-size / --source-limit 必须为正整数")

    source = Path(args.input)
    destination = Path(args.out)
    try:
        document = json.loads(source.read_text(encoding="utf-8"))
        candidates = _candidate_list(document)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        print(f"✗ 无法读取评论翻译输入：{exc}")
        return 1

    model = args.model or (
        comment_translation.DEFAULT_MODEL
        if args.provider == "ollama"
        else comment_translation.DEFAULT_ANTHROPIC_MODEL
    )
    try:
        manifest = document.get("manifest") if isinstance(document, dict) else {}
        usage_batch_id = (
            manifest.get("batch_id") if isinstance(manifest, dict) else None
        )
        summary = comment_translation.translate_candidates(
            candidates,
            provider=args.provider,
            model=model,
            api_url=args.api_url,
            batch_size=args.batch_size,
            source_limit=args.source_limit,
            usage_context={
                "batch_id": usage_batch_id,
                "stage": "translate_comments",
                "feature": "comment_translation",
            },
        )
    except comment_translation.TranslationConfigurationError as exc:
        print(f"✗ 评论翻译配置错误：{exc}")
        return 1

    if isinstance(document, dict):
        manifest = document.setdefault("manifest", {})
        if isinstance(manifest, dict):
            manifest["comment_translation"] = summary
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(
        json.dumps(document, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(destination)
    print(
        f"评论翻译 → {destination}：成功 {summary['translated_count']}/"
        f"{summary['requested_count']}，失败 {summary['failed_count']}，"
        f"历史原文缺失 {summary['source_unavailable_count']}"
    )
    if args.strict and (
        summary["failed_count"] or summary["source_unavailable_count"]
    ):
        print("✗ 严格翻译门禁失败；审计输出已保留，但不可作为正式交付")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
