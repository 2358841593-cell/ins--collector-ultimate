"""Stage 1 多来源配额计划（纯计算，不连接 Modash、不写数据库）。"""
from __future__ import annotations

import math
from typing import Mapping


SOURCE_KEYS = ("golden_lookalike", "generic_commerce", "exploration")
STRUCTURED_SOURCE_KEYS = SOURCE_KEYS[1:]


class DiscoveryStrategyError(ValueError):
    """来源配置或配额不满足可执行合同。"""


def validate_source_mix(value: Mapping[str, object]) -> dict[str, float]:
    if not isinstance(value, Mapping) or set(value) != set(SOURCE_KEYS):
        raise DiscoveryStrategyError(
            "source_mix_pct 必须且只能包含 " + "、".join(SOURCE_KEYS)
        )
    try:
        mix = {key: float(value[key]) for key in SOURCE_KEYS}
    except (TypeError, ValueError) as exc:
        raise DiscoveryStrategyError("source_mix_pct 必须是数字") from exc
    if any(not math.isfinite(v) or v < 0 for v in mix.values()):
        raise DiscoveryStrategyError("source_mix_pct 必须是非负有限数")
    if abs(sum(mix.values()) - 100.0) > 1e-6:
        raise DiscoveryStrategyError("source_mix_pct 合计必须为 100%")
    if sum(mix[key] for key in STRUCTURED_SOURCE_KEYS) <= 0:
        raise DiscoveryStrategyError("通用电商和探索来源不能同时为 0")
    return mix


def _integer_allocation(total: int, weights: Mapping[str, float], order: tuple[str, ...]) -> dict[str, int]:
    if total < 0:
        raise DiscoveryStrategyError("候选目标数不能为负数")
    weight_sum = sum(float(weights[key]) for key in order)
    if total and weight_sum <= 0:
        raise DiscoveryStrategyError("有效来源权重不能为 0")
    if total == 0:
        return {key: 0 for key in order}
    raw = {key: total * float(weights[key]) / weight_sum for key in order}
    result = {key: int(math.floor(raw[key])) for key in order}
    remainder = total - sum(result.values())
    ranked = sorted(order, key=lambda key: (-(raw[key] - result[key]), order.index(key)))
    for key in ranked[:remainder]:
        result[key] += 1
    return result


def build_source_plan(
    *,
    target: int,
    golden_available: int,
    source_mix_pct: Mapping[str, object],
) -> dict:
    """把总目标分给金种子、通用电商与探索源。

    金种子结果不足时，其缺口按 30:20 等既有非金种子权重重新分配。金种子结果
    超过目标时只使用目标份额，防止单一来源吞掉探索预算。
    """
    if isinstance(target, bool) or not isinstance(target, int) or target <= 0:
        raise DiscoveryStrategyError("target 必须是正整数")
    if (
        isinstance(golden_available, bool)
        or not isinstance(golden_available, int)
        or golden_available < 0
    ):
        raise DiscoveryStrategyError("golden_available 必须是非负整数")
    mix = validate_source_mix(source_mix_pct)
    desired = _integer_allocation(target, mix, SOURCE_KEYS)
    golden_used = min(golden_available, desired["golden_lookalike"])
    shortfall = desired["golden_lookalike"] - golden_used
    fallback = _integer_allocation(
        shortfall,
        {key: mix[key] for key in STRUCTURED_SOURCE_KEYS},
        STRUCTURED_SOURCE_KEYS,
    )
    targets = {
        "golden_lookalike": golden_used,
        "generic_commerce": desired["generic_commerce"] + fallback["generic_commerce"],
        "exploration": desired["exploration"] + fallback["exploration"],
    }
    if sum(targets.values()) != target:
        raise DiscoveryStrategyError("来源目标分配内部错误")
    return {
        "target": target,
        "source_mix_pct": mix,
        "desired": desired,
        "targets": targets,
        "golden_available": golden_available,
        "golden_shortfall": shortfall,
        "fallback_reallocated": fallback,
    }


def structured_queries(discovery_config: Mapping[str, object]) -> dict[str, str]:
    rows = discovery_config.get("structured_sources")
    if not isinstance(rows, list):
        raise DiscoveryStrategyError("discovery.structured_sources 必须是数组")
    result: dict[str, str] = {}
    for index, row in enumerate(rows, 1):
        if not isinstance(row, Mapping):
            raise DiscoveryStrategyError(f"structured_sources[{index}] 必须是对象")
        name = row.get("name")
        query = row.get("query")
        if name not in STRUCTURED_SOURCE_KEYS:
            raise DiscoveryStrategyError(f"structured_sources[{index}].name 无效")
        if name in result:
            raise DiscoveryStrategyError(f"structured_sources 中来源 {name} 重复")
        if not isinstance(query, str) or not query.strip():
            raise DiscoveryStrategyError(f"structured_sources[{index}].query 不能为空")
        result[name] = query.strip()
    missing = set(STRUCTURED_SOURCE_KEYS) - set(result)
    if missing:
        raise DiscoveryStrategyError("structured_sources 缺少：" + "、".join(sorted(missing)))
    return result
