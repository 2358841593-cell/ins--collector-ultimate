"""每轮采集开始前冻结的可审计合同。

合同把容易在口头沟通中漂移的规则（历史结转、国家、Storefront、报价、翻译、
来源配额）与当时的配置/代码 SHA 固定下来。它不修改运行配置；流水线只应消费已经
校验通过并归档的合同。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from .config import load_config
from .contracts import sha256_of_file


SCHEMA_VERSION = 1
CARRYOVER_MODES = frozenset({"new_only", "unresolved", "retry_only"})
SOURCE_KEYS = ("golden_lookalike", "generic_commerce", "exploration")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_BATCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_REPO_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_CONFIG = _REPO_ROOT / "config" / "sop_v2.toml"


class RoundContractError(ValueError):
    """轮次合同缺字段、取值非法或文件不可安全覆盖。"""


def code_sha256(repo_root: str | Path = _REPO_ROOT) -> str:
    """计算会影响 SOP 决策与交付的代码树指纹。"""
    root = Path(repo_root).resolve()
    selected = list((root / "scripts" / "extensions" / "sop_v2").rglob("*.py"))
    selected.extend(
        root / "scripts" / name
        for name in ("browser_collect_v2.py", "export_v2_html.py", "export_v2_xlsx.py")
        if (root / "scripts" / name).is_file()
    )
    if not selected:
        raise RoundContractError(f"未找到 SOP 代码文件：{root}")
    digest = hashlib.sha256()
    for path in sorted(set(selected), key=lambda p: p.relative_to(root).as_posix()):
        rel = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(rel).to_bytes(4, "big"))
        digest.update(rel)
        data = path.read_bytes()
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()


def _git_revision(repo_root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = result.stdout.strip().lower()
    return value if re.fullmatch(r"[0-9a-f]{40,64}", value) else None


def build_round_contract(
    *,
    batch_id: str,
    campaign_track: str,
    carryover_mode: str,
    config_path: str | Path | None = None,
    source_quota_pct: dict[str, float] | None = None,
    created_at: str | None = None,
    repo_root: str | Path = _REPO_ROOT,
) -> dict:
    """从规则单一事实源生成合同；返回前执行完整校验。"""
    root = Path(repo_root).resolve()
    cfg_path = Path(config_path).resolve() if config_path else root / "config" / "sop_v2.toml"
    cfg = load_config(str(cfg_path))
    country = cfg["country"]
    pricing = cfg["pricing_estimate"]
    quota = source_quota_pct or cfg.get("discovery", {}).get("source_mix_pct") or {
        "golden_lookalike": 50,
        "generic_commerce": 30,
        "exploration": 20,
    }
    revision = _git_revision(root)
    contract = {
        "schema_version": SCHEMA_VERSION,
        "batch_id": batch_id,
        "created_at": created_at or time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "campaign_track": campaign_track,
        "carryover_mode": carryover_mode,
        "sop_version": cfg.get("sop_version"),
        "config": {
            "path": cfg_path.relative_to(root).as_posix()
            if cfg_path.is_relative_to(root)
            else str(cfg_path),
            "sha256": sha256_of_file(str(cfg_path)),
        },
        "code": {
            "scope": "scripts/extensions/sop_v2/**/*.py + browser/export entrypoints",
            "sha256": code_sha256(root),
            "git_revision": revision,
        },
        "countries": {
            "tier1": list(country["tier1"]),
            "tier2": list(country["tier2"]),
            "allowed": list(dict.fromkeys([*country["tier1"], *country["tier2"]])),
            "require_creator_equals_top_audience": bool(
                country.get("require_creator_equals_top_audience", True)
            ),
        },
        "storefront": {
            "definition": "generic_ecommerce",
            "amazon_required": False,
            "accepted_types": ["Amazon", "LTK", "ShopMy", "自营店", "链接聚合"],
            "confirmed_no_eligible": True,
            "unknown_routes_to": "Review",
        },
        "pricing_estimate": {
            "metric": "mean_recent_non_pinned_reels_views",
            "reels_window": int(pricing["reels_window"]),
            "exclude_pinned": bool(pricing["exclude_pinned"]),
            "default_cpm_usd": float(pricing["default_cpm_usd"]),
            "cpm_min_usd": float(pricing["cpm_min_usd"]),
            "cpm_max_usd": float(pricing["cpm_max_usd"]),
            "short_window_requires_reels_tab_exhaustion": bool(
                pricing["short_window_requires_reels_tab_exhaustion"]
            ),
            "zero_reels_status": str(pricing["zero_reels_status"]),
            "third_party_fallback_allowed": bool(
                pricing["third_party_fallback_allowed"]
            ),
        },
        "comment_translation": {
            "all_source_languages": True,
            "target_language": "zh-CN",
            "strict_delivery": True,
        },
        "sources": {"quota_pct": dict(quota)},
        "dedup": {
            "scope": "global_creator_cache",
            "case_insensitive_handle": True,
            "merge_multi_source_provenance": True,
        },
        "feedback_governance": {
            "rejection_reason_optional": True,
            "empty_reason_is_strategy_signal": False,
            "policy_signal_requires_internal_confirmation": True,
            "automatic_hard_gate_changes": False,
        },
    }
    return validate_round_contract(contract)


def _mapping(value: Any, label: str) -> dict:
    if not isinstance(value, dict):
        raise RoundContractError(f"{label} 必须是对象")
    return value


def _nonempty_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RoundContractError(f"{label} 必须是非空字符串")
    return value.strip()


def validate_round_contract(value: Any) -> dict:
    """严格校验并返回 JSON round-trip 后的独立副本。"""
    root = _mapping(value, "轮次合同")
    if root.get("schema_version") != SCHEMA_VERSION:
        raise RoundContractError(f"schema_version 必须是 {SCHEMA_VERSION}")
    batch_id = _nonempty_string(root.get("batch_id"), "batch_id")
    if not _BATCH_RE.fullmatch(batch_id):
        raise RoundContractError("batch_id 只能包含字母、数字、点、下划线和连字符")
    _nonempty_string(root.get("created_at"), "created_at")
    if root.get("campaign_track") not in {"paid", "gifting"}:
        raise RoundContractError("campaign_track 必须是 paid 或 gifting")
    if root.get("carryover_mode") not in CARRYOVER_MODES:
        raise RoundContractError(
            "carryover_mode 必须是 new_only、unresolved 或 retry_only"
        )
    _nonempty_string(root.get("sop_version"), "sop_version")

    for label in ("config", "code"):
        block = _mapping(root.get(label), label)
        digest = block.get("sha256")
        if not isinstance(digest, str) or not _SHA_RE.fullmatch(digest):
            raise RoundContractError(f"{label}.sha256 必须是 64 位小写 SHA-256")
    _nonempty_string(root["config"].get("path"), "config.path")
    _nonempty_string(root["code"].get("scope"), "code.scope")
    revision = root["code"].get("git_revision")
    if revision is not None and not (
        isinstance(revision, str) and re.fullmatch(r"[0-9a-f]{40,64}", revision)
    ):
        raise RoundContractError("code.git_revision 必须为空或 40–64 位十六进制")

    countries = _mapping(root.get("countries"), "countries")
    allowed = countries.get("allowed")
    if not isinstance(allowed, list) or not allowed or any(
        not isinstance(code, str) or not code.strip() for code in allowed
    ):
        raise RoundContractError("countries.allowed 必须是非空国家代码数组")
    if len(allowed) != len(set(allowed)):
        raise RoundContractError("countries.allowed 不能重复")
    if not isinstance(countries.get("require_creator_equals_top_audience"), bool):
        raise RoundContractError(
            "countries.require_creator_equals_top_audience 必须是布尔值"
        )

    storefront = _mapping(root.get("storefront"), "storefront")
    if storefront.get("definition") != "generic_ecommerce":
        raise RoundContractError("storefront.definition 必须是 generic_ecommerce")
    for field in ("amazon_required", "confirmed_no_eligible"):
        if not isinstance(storefront.get(field), bool):
            raise RoundContractError(f"storefront.{field} 必须是布尔值")
    accepted = storefront.get("accepted_types")
    if not isinstance(accepted, list) or not accepted:
        raise RoundContractError("storefront.accepted_types 必须是非空数组")

    pricing = _mapping(root.get("pricing_estimate"), "pricing_estimate")
    if pricing.get("metric") != "mean_recent_non_pinned_reels_views":
        raise RoundContractError("pricing_estimate.metric 口径未知")
    window = pricing.get("reels_window")
    if isinstance(window, bool) or not isinstance(window, int) or window <= 0:
        raise RoundContractError("pricing_estimate.reels_window 必须是正整数")
    if not isinstance(pricing.get("exclude_pinned"), bool):
        raise RoundContractError("pricing_estimate.exclude_pinned 必须是布尔值")
    if pricing.get("short_window_requires_reels_tab_exhaustion") is not True:
        raise RoundContractError(
            "pricing_estimate.short_window_requires_reels_tab_exhaustion 必须为 true"
        )
    if pricing.get("zero_reels_status") != "not_applicable_no_reels":
        raise RoundContractError(
            "pricing_estimate.zero_reels_status 必须是 not_applicable_no_reels"
        )
    if pricing.get("third_party_fallback_allowed") is not False:
        raise RoundContractError(
            "pricing_estimate.third_party_fallback_allowed 必须为 false"
        )
    try:
        low = float(pricing["cpm_min_usd"])
        default = float(pricing["default_cpm_usd"])
        high = float(pricing["cpm_max_usd"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RoundContractError("CPM 必须是有效数字") from exc
    if low < 0 or not low <= default <= high:
        raise RoundContractError("CPM 必须满足 0 <= min <= default <= max")

    translation = _mapping(root.get("comment_translation"), "comment_translation")
    if translation.get("all_source_languages") is not True:
        raise RoundContractError("comment_translation.all_source_languages 必须为 true")
    _nonempty_string(translation.get("target_language"), "comment_translation.target_language")
    if not isinstance(translation.get("strict_delivery"), bool):
        raise RoundContractError("comment_translation.strict_delivery 必须是布尔值")

    sources = _mapping(root.get("sources"), "sources")
    quota = _mapping(sources.get("quota_pct"), "sources.quota_pct")
    if set(quota) != set(SOURCE_KEYS):
        raise RoundContractError(
            "sources.quota_pct 必须且只能包含 " + "、".join(SOURCE_KEYS)
        )
    try:
        shares = [float(quota[key]) for key in SOURCE_KEYS]
    except (TypeError, ValueError) as exc:
        raise RoundContractError("来源配额必须是数字") from exc
    if any(share < 0 for share in shares) or abs(sum(shares) - 100.0) > 1e-6:
        raise RoundContractError("来源配额必须非负且合计 100%")

    dedup = _mapping(root.get("dedup"), "dedup")
    for field in ("case_insensitive_handle", "merge_multi_source_provenance"):
        if dedup.get(field) is not True:
            raise RoundContractError(f"dedup.{field} 必须为 true")
    governance = _mapping(root.get("feedback_governance"), "feedback_governance")
    if governance.get("rejection_reason_optional") is not True:
        raise RoundContractError("拒绝原因必须保持可选")
    if governance.get("empty_reason_is_strategy_signal") is not False:
        raise RoundContractError("空原因不能成为策略信号")
    if governance.get("policy_signal_requires_internal_confirmation") is not True:
        raise RoundContractError("policy signal 必须经过内部确认后才能成为全局策略")
    if governance.get("automatic_hard_gate_changes") is not False:
        raise RoundContractError("系统不能自动修改硬门槛")

    return json.loads(json.dumps(root, ensure_ascii=False))


def round_contract_sha256(path: str | Path) -> str:
    """返回冻结合同原始字节的 SHA-256，供 carryover/交付清单绑定。"""
    p = Path(path)
    try:
        return hashlib.sha256(p.read_bytes()).hexdigest()
    except OSError as exc:
        raise RoundContractError(f"合同不可读：{p}") from exc


def load_carryover_manifest(path: str | Path) -> dict:
    """读取供合同门禁使用的 carryover manifest，不替代 Stage 4 的完整形状校验。"""
    p = Path(path)
    try:
        value = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RoundContractError(f"carryover manifest 不可读或不是合法 JSON：{p}") from exc
    if not isinstance(value, dict):
        raise RoundContractError("carryover manifest 根节点必须是对象")
    return value


def validate_carryover_for_contract(
    contract: dict,
    manifest: dict,
    *,
    contract_sha256: str | None = None,
) -> dict:
    """验证结转清单与冻结模式一致。

    ``new_only`` 允许不提供清单；一旦提供，``carryover[]`` 必须为空。
    ``unresolved`` 必须保留全部未终判账号；``retry_only`` 只能保留技术补采账号。
    生成器用 ``counts.mode_excluded`` 对 intentionally omitted 的未终判账号对账，
    防止把少生成的清单误当成合法的 new/retry-only 选择。
    """
    normalized_contract = validate_round_contract(contract)
    root = _mapping(manifest, "carryover manifest")
    if root.get("next_batch_id") != normalized_contract["batch_id"]:
        raise RoundContractError(
            "carryover manifest next_batch_id 与轮次合同 batch_id 不一致"
        )
    mode = normalized_contract["carryover_mode"]
    if root.get("carryover_mode") != mode:
        raise RoundContractError(
            f"carryover manifest carryover_mode 必须为 {mode}"
        )
    if contract_sha256 is not None:
        if not _SHA_RE.fullmatch(contract_sha256):
            raise RoundContractError("轮次合同 SHA-256 格式无效")
        if root.get("round_contract_sha256") != contract_sha256:
            raise RoundContractError("carryover manifest 未绑定当前轮次合同 SHA-256")

    rows = root.get("carryover")
    retry_handles = root.get("retry_handles")
    counts = _mapping(root.get("counts"), "carryover manifest counts")
    if not isinstance(rows, list):
        raise RoundContractError("carryover manifest carryover 必须是数组")
    if not isinstance(retry_handles, list):
        raise RoundContractError("carryover manifest retry_handles 必须是数组")
    try:
        carryover_count = counts["carryover"]
        retry_count = counts["pipeline_retry"]
        source_count = counts["source_candidates"]
        client_final_count = counts["client_final"]
    except KeyError as exc:
        raise RoundContractError(f"carryover manifest counts 缺少 {exc.args[0]}") from exc
    for label, value in (
        ("carryover", carryover_count),
        ("pipeline_retry", retry_count),
        ("source_candidates", source_count),
        ("client_final", client_final_count),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise RoundContractError(f"carryover manifest counts.{label} 必须是非负整数")
    if carryover_count != len(rows):
        raise RoundContractError("carryover manifest carryover 计数不一致")

    row_retry_handles: list[str] = []
    row_handles: set[str] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise RoundContractError(f"carryover[{index}] 必须是对象")
        needs_retry = row.get("needs_pipeline_retry")
        if not isinstance(needs_retry, bool):
            raise RoundContractError(
                f"carryover[{index}].needs_pipeline_retry 必须是布尔值"
            )
        raw_handle = row.get("handle")
        if not isinstance(raw_handle, str):
            raise RoundContractError(f"carryover[{index}].handle 必须是字符串")
        handle = raw_handle.strip().lstrip("@").lower()
        if not handle:
            raise RoundContractError(f"carryover[{index}].handle 不能为空")
        if handle in row_handles:
            raise RoundContractError(f"carryover handle 重复：{handle}")
        row_handles.add(handle)
        if needs_retry:
            row_retry_handles.append(handle)
    if any(not isinstance(handle, str) for handle in retry_handles):
        raise RoundContractError("carryover manifest retry_handles 必须是字符串数组")
    normalized_retry = [
        str(handle or "").strip().lstrip("@").lower()
        for handle in retry_handles
    ]
    if (
        any(not handle for handle in normalized_retry)
        or len(set(normalized_retry)) != len(normalized_retry)
        or set(normalized_retry) != set(row_retry_handles)
        or retry_count != len(normalized_retry)
        or retry_count != len(row_retry_handles)
    ):
        raise RoundContractError(
            "carryover manifest retry_handles/pipeline_retry 与行标记不一致"
        )

    raw_excluded = counts.get("mode_excluded", 0)
    if isinstance(raw_excluded, bool) or not isinstance(raw_excluded, int) or raw_excluded < 0:
        raise RoundContractError("carryover manifest counts.mode_excluded 必须是非负整数")
    if source_count != client_final_count + carryover_count + raw_excluded:
        raise RoundContractError(
            "carryover manifest source_candidates 对账失败"
        )

    if mode == "new_only":
        if rows or retry_handles or carryover_count or retry_count:
            raise RoundContractError("new_only 轮次禁止非空 carryover manifest")
    elif mode == "unresolved":
        if raw_excluded:
            raise RoundContractError("unresolved 模式不能遗漏未终判账号")
    elif mode == "retry_only":
        if any(not row.get("needs_pipeline_retry") for row in rows):
            raise RoundContractError("retry_only 只能结转 needs_pipeline_retry=true 的账号")
        if carryover_count != retry_count:
            raise RoundContractError("retry_only 的 carryover 必须全部属于 pipeline_retry")

    return json.loads(json.dumps(root, ensure_ascii=False))


def load_round_contract(path: str | Path) -> dict:
    p = Path(path)
    try:
        value = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RoundContractError(f"合同不可读或不是合法 JSON：{p}") from exc
    return validate_round_contract(value)


def assert_contract_matches_runtime(
    contract: dict,
    *,
    batch_id: str,
    campaign_track: str,
    config_path: str | Path | None = None,
    repo_root: str | Path = _REPO_ROOT,
) -> dict:
    """在浏览器/外部服务动作前验证合同与当前运行时完全一致。"""
    normalized = validate_round_contract(contract)
    if normalized["batch_id"] != batch_id:
        raise RoundContractError(
            f"合同 batch_id={normalized['batch_id']!r}，运行参数={batch_id!r}"
        )
    if normalized["campaign_track"] != campaign_track:
        raise RoundContractError(
            "合同 campaign_track="
            f"{normalized['campaign_track']!r}，运行参数={campaign_track!r}"
        )
    root = Path(repo_root).resolve()
    cfg_path = Path(config_path).resolve() if config_path else root / "config" / "sop_v2.toml"
    current_config_sha = sha256_of_file(str(cfg_path))
    if normalized["config"]["sha256"] != current_config_sha:
        raise RoundContractError("合同 config SHA 与当前 sop_v2.toml 不一致")
    current_code_sha = code_sha256(root)
    if normalized["code"]["sha256"] != current_code_sha:
        raise RoundContractError("合同 code SHA 与当前 SOP 代码不一致")
    return normalized


def write_round_contract(path: str | Path, contract: dict) -> Path:
    """写入合同；不同内容不得静默覆盖已冻结文件。"""
    normalized = validate_round_contract(contract)
    rendered = json.dumps(normalized, ensure_ascii=False, indent=2) + "\n"
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists():
        current = p.read_text(encoding="utf-8")
        if current == rendered:
            return p
        raise RoundContractError(f"轮次合同已存在且内容不同，拒绝覆盖：{p}")
    p.write_text(rendered, encoding="utf-8")
    return p


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成并冻结下一轮采集合同")
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--track", required=True, choices=["paid", "gifting"])
    parser.add_argument("--carryover-mode", required=True, choices=sorted(CARRYOVER_MODES))
    parser.add_argument("--config", default=str(_DEFAULT_CONFIG))
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    try:
        contract = build_round_contract(
            batch_id=args.batch_id,
            campaign_track=args.track,
            carryover_mode=args.carryover_mode,
            config_path=args.config,
        )
        path = write_round_contract(args.out, contract)
    except (OSError, RoundContractError) as exc:
        parser.error(str(exc))
    print(f"✓ 已冻结轮次合同：{path}")
    print(f"  config_sha256={contract['config']['sha256']}")
    print(f"  code_sha256={contract['code']['sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
