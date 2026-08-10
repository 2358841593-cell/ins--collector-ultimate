"""Use the logged-in Modash Discovery page to complete a golden Lookalike manifest.

The endpoint and units in this module were verified against the marketer UI on
2026-08-10.  It only reads Discovery search results; it never opens a Profile
Report, saves a creator, or exports contact data.

Example::

    PYTHONPATH=scripts .venv/bin/python \
      -m extensions.sop_v2.pipeline.modash_golden_lookalikes \
      --manifest data/runs/SKIN6-20260810/golden_lookalike_seeds.json \
      --out data/runs/SKIN6-20260810/golden_lookalikes_completed.json \
      --track paid --target 60 \
      --round-contract data/batches/SKIN6-20260810/round_contract.json \
      --require-round-contract

The output remains ``golden-lookalikes-v1`` and can be passed directly to
``stage1_discover --golden-lookalikes-json``.  Additional ``collection`` audit
metadata is ignored by the Stage 1 loader.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import random
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from extensions.sop_v2 import creator_cache as cc  # noqa: E402
from extensions.sop_v2 import round_contract as round_contract_mod  # noqa: E402
from extensions.sop_v2.config import load_config  # noqa: E402
from extensions.sop_v2.pipeline import modash_search as ms  # noqa: E402


ENDPOINT = "/api/discovery/search/v2/multi-creator-lookalikes"
_HANDLE_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")
_FETCH_JS = """async (body) => {
  const r = await fetch('/api/discovery/search/v2/multi-creator-lookalikes', {
    method: 'POST',
    headers: {'content-type': 'application/json'},
    body: JSON.stringify(body)
  });
  return {status: r.status, text: await r.text()};
}"""


class LookalikeCollectionError(ValueError):
    """The manifest, response, or resume state cannot be used safely."""


def _handle(value: Any) -> str:
    handle = str(value or "").strip().lstrip("@")
    return handle if _HANDLE_RE.fullmatch(handle) else ""


def _manifest_seed_handles(payload: dict) -> list[str]:
    if payload.get("schema_version") != ms.GOLDEN_LOOKALIKE_SCHEMA_VERSION:
        raise LookalikeCollectionError(
            "manifest schema_version 必须是 "
            f"{ms.GOLDEN_LOOKALIKE_SCHEMA_VERSION}"
        )
    if payload.get("format") != "golden-lookalikes-v1":
        raise LookalikeCollectionError("manifest format 必须是 golden-lookalikes-v1")
    batch_id = payload.get("batch_id")
    if not isinstance(batch_id, str) or not batch_id.strip():
        raise LookalikeCollectionError("manifest batch_id 缺失")
    raw_seeds = payload.get("seeds")
    if not isinstance(raw_seeds, list) or not raw_seeds:
        raise LookalikeCollectionError("manifest seeds 必须是非空数组")
    handles = []
    for index, row in enumerate(raw_seeds):
        if not isinstance(row, dict):
            raise LookalikeCollectionError(f"seeds[{index}] 必须是对象")
        handle = _handle(row.get("handle"))
        if not handle:
            raise LookalikeCollectionError(f"seeds[{index}].handle 无效")
        handles.append(handle)
    if len({handle.lower() for handle in handles}) != len(handles):
        raise LookalikeCollectionError("manifest seeds 含重复账号")
    expected = ms.golden_seed_fingerprint(handles)
    if payload.get("seed_set_sha256") != expected:
        raise LookalikeCollectionError("manifest seed_set_sha256 与 seeds 不一致")
    if payload.get("seed_count") != len(handles):
        raise LookalikeCollectionError("manifest seed_count 与 seeds 不一致")
    return handles


def load_manifest(path: str | Path) -> tuple[dict, list[str]]:
    source = Path(path)
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LookalikeCollectionError(f"manifest 不可读或不是合法 JSON：{source}") from exc
    if not isinstance(payload, dict):
        raise LookalikeCollectionError("manifest 根节点必须是对象")
    handles = _manifest_seed_handles(payload)
    _validate_result_groups(payload, handles)
    return payload, handles


def _validate_result_groups(payload: dict, seed_handles: list[str]) -> None:
    results = payload.get("results")
    if not isinstance(results, list):
        raise LookalikeCollectionError("manifest results 必须是数组")
    expected = {handle.lower() for handle in seed_handles}
    actual = []
    for index, group in enumerate(results):
        if not isinstance(group, dict):
            raise LookalikeCollectionError(f"results[{index}] 必须是对象")
        seed = _handle(group.get("seed_handle"))
        candidates = group.get("candidates")
        if not seed or seed.lower() not in expected:
            raise LookalikeCollectionError(f"results[{index}].seed_handle 不属于当前种子")
        if not isinstance(candidates, list):
            raise LookalikeCollectionError(f"results[{index}].candidates 必须是数组")
        actual.append(seed.lower())
    if len(actual) != len(expected) or set(actual) != expected:
        raise LookalikeCollectionError("results 必须且只能为每个 seed 提供一个分组")
    if len(set(actual)) != len(actual):
        raise LookalikeCollectionError("results 中 seed_handle 重复")


def _cohort_matches(left: dict, right: dict) -> bool:
    return (
        left.get("schema_version") == right.get("schema_version")
        and left.get("format") == right.get("format")
        and left.get("batch_id") == right.get("batch_id")
        and left.get("seed_set_sha256") == right.get("seed_set_sha256")
    )


def load_or_initialize_output(
    manifest: dict,
    seed_handles: list[str],
    out_path: str | Path,
) -> dict:
    path = Path(out_path)
    if path.exists():
        try:
            output = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise LookalikeCollectionError(f"断点文件不可读或不是合法 JSON：{path}") from exc
        if not isinstance(output, dict) or not _cohort_matches(manifest, output):
            raise LookalikeCollectionError("断点文件不属于当前 batch/种子集合，拒绝覆盖")
        _validate_result_groups(output, seed_handles)
        # Reuse the strict Stage 1 value validator for any previously stored rows.
        ms.load_golden_lookalikes(
            path,
            batch_id=str(manifest["batch_id"]),
            golden_handles=seed_handles,
        )
        return output
    return copy.deepcopy(manifest)


def atomic_write_json(path: str | Path, payload: dict) -> Path:
    """Write a checkpoint without ever exposing a partially written JSON file."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    digest = hashlib.sha256(rendered.encode("utf-8")).hexdigest()[:12]
    temporary = target.with_name(f".{target.name}.{os.getpid()}.{digest}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            stream.write(rendered)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()
    return target


def _track_follower_range(cfg: dict, track: str) -> tuple[int, int]:
    block = cfg["track"][track]
    if track == "paid":
        return int(block["min_followers"]), int(block["max_followers"])
    return int(block["standard_min"]), int(block["priority_max"])


def quality_candidate(
    raw: Any,
    *,
    seed_handle: str,
    follower_min: int,
    follower_max: int,
    er_min_pct: float,
    niche_keywords: list[str],
) -> tuple[dict | None, str | None]:
    """Normalize one verified endpoint row and explain any local rejection.

    The Lookalike endpoint already returns ``engagement_rate`` in percentage
    units (3.33 means 3.33%), unlike the generic search endpoint's fraction.
    """
    if not isinstance(raw, dict):
        return None, "invalid_row"
    handle = _handle(raw.get("username"))
    if not handle:
        return None, "invalid_handle"
    if handle.lower() == seed_handle.lower():
        return None, "seed_self"
    if not isinstance(raw.get("is_brand"), bool) or not isinstance(
        raw.get("is_private"), bool
    ):
        return None, "account_flags_missing"
    if raw["is_brand"]:
        return None, "brand"
    if raw["is_private"]:
        return None, "private"
    followers = raw.get("follower_count")
    if (
        isinstance(followers, bool)
        or not isinstance(followers, (int, float))
        or not math.isfinite(float(followers))
        or not float(followers).is_integer()
    ):
        return None, "followers_missing"
    followers = int(followers)
    if followers < follower_min or followers > follower_max:
        return None, "followers_out_of_range"
    er_pct = raw.get("engagement_rate")
    if (
        isinstance(er_pct, bool)
        or not isinstance(er_pct, (int, float))
        or not math.isfinite(float(er_pct))
        or not 0 <= float(er_pct) <= 100
    ):
        return None, "er_missing"
    er_pct = float(er_pct)
    if er_pct < er_min_pct:
        return None, "er_below_min"
    blob = " ".join(
        str(raw.get(field) or "")
        for field in ("creator_description", "account_category", "creator_highlight")
    ).lower()
    keywords = [str(keyword).strip().lower() for keyword in niche_keywords if str(keyword).strip()]
    if keywords and not any(keyword in blob for keyword in keywords):
        return None, "off_niche"
    return {
        "handle": handle,
        "followers": followers,
        "er_pct": round(er_pct, 4),
    }, None


def _result_group_map(payload: dict) -> dict[str, dict]:
    return {
        _handle(group.get("seed_handle")).lower(): group
        for group in payload["results"]
    }


def _existing_unique_new(payload: dict, should_ingest: Callable[[str], bool]) -> set[str]:
    unique = set()
    for group in payload["results"]:
        for candidate in group.get("candidates") or []:
            handle = _handle(candidate.get("handle") if isinstance(candidate, dict) else None)
            if handle and should_ingest(handle):
                unique.add(handle.lower())
    return unique


def _new_stats() -> dict[str, int]:
    return {
        "requests": 0,
        "scanned": 0,
        "filtered": 0,
        "accepted": 0,
        "duplicate": 0,
        "accepted_unique": 0,
        "known": 0,
        "duplicate_observed": 0,
        "invalid_row": 0,
        "invalid_handle": 0,
        "seed_self": 0,
        "account_flags_missing": 0,
        "brand": 0,
        "private": 0,
        "followers_missing": 0,
        "followers_out_of_range": 0,
        "er_missing": 0,
        "er_below_min": 0,
        "off_niche": 0,
        "seed_not_found": 0,
    }


def _collection_state(
    output: dict,
    *,
    track: str,
    target: int,
    page_size: int,
    max_pages_per_seed: int,
    per_seed_max: int,
    per_page_accept: int,
    round_contract_sha256: str | None,
) -> dict:
    state = output.get("collection")
    expected = {
        "endpoint": ENDPOINT,
        "track": track,
        "target_new_unique": target,
        "page_size": page_size,
        "max_pages_per_seed": max_pages_per_seed,
        "per_seed_max": per_seed_max,
        "per_page_accept": per_page_accept,
    }
    if state is None:
        state = {
            **expected,
            "round_contract_sha256": round_contract_sha256,
            "status": "in_progress",
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "updated_at": None,
            "progress": {},
            "errors": [],
            "stats": _new_stats(),
        }
        output["collection"] = state
        return state
    if not isinstance(state, dict):
        raise LookalikeCollectionError("collection 断点状态必须是对象")
    for key, value in expected.items():
        if state.get(key) != value:
            raise LookalikeCollectionError(
                f"断点参数 {key}={state.get(key)!r}，本次为 {value!r}，拒绝混跑"
            )
    previous_sha = state.get("round_contract_sha256")
    if previous_sha != round_contract_sha256:
        raise LookalikeCollectionError("断点绑定的轮次合同与本次不一致")
    if not isinstance(state.get("progress"), dict):
        raise LookalikeCollectionError("collection.progress 必须是对象")
    if not isinstance(state.get("errors"), list):
        raise LookalikeCollectionError("collection.errors 必须是数组")
    stats = state.get("stats")
    if not isinstance(stats, dict):
        raise LookalikeCollectionError("collection.stats 必须是对象")
    for key, value in _new_stats().items():
        stats.setdefault(key, value)
    return state


def validate_response(raw: Any, seed_handle: str) -> tuple[list[dict], int | None]:
    if not isinstance(raw, dict):
        raise LookalikeCollectionError("Modash 返回不是对象")
    status = raw.get("status")
    if status != 200:
        raise LookalikeCollectionError(f"Modash Lookalike HTTP {status}")
    try:
        payload = json.loads(raw.get("text") or "")
    except (TypeError, json.JSONDecodeError) as exc:
        raise LookalikeCollectionError("Modash Lookalike 返回不是合法 JSON") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise LookalikeCollectionError("Modash Lookalike 返回缺少 results[]")
    seed_results = payload.get("seedResults")
    if not isinstance(seed_results, list):
        raise LookalikeCollectionError("Modash Lookalike 返回缺少 seedResults[]")
    matched = any(
        _handle(row.get("username") if isinstance(row, dict) else None).lower()
        == seed_handle.lower()
        for row in seed_results
    )
    if not matched:
        return [], None
    total = payload.get("total")
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        raise LookalikeCollectionError("Modash Lookalike total 无效")
    return payload["results"], total


def collect_with_page(
    page,
    output: dict,
    seed_handles: list[str],
    *,
    cfg: dict,
    track: str,
    target: int,
    page_size: int,
    max_pages_per_seed: int,
    per_seed_max: int,
    per_page_accept: int,
    checkpoint: Callable[[dict], None],
    should_ingest: Callable[[str], bool] = cc.should_ingest_seed,
    page_delay: float = 2.2,
    random_uniform: Callable[[float, float], float] = random.uniform,
) -> dict:
    """Round-robin seeds, checkpointing after every same-origin request."""
    state = output["collection"]
    progress = state["progress"]
    stats = state["stats"]
    groups = _result_group_map(output)
    unique_new = _existing_unique_new(output, should_ingest)
    stats["accepted_unique"] = len(unique_new)
    stats["accepted"] = len(unique_new)
    follower_min, follower_max = _track_follower_range(cfg, track)
    er_min_pct = float(cfg["discovery"].get("search_er_min", 0)) * 100.0
    niche_keywords = list(cfg["discovery"].get("niche_keywords") or [])

    for seed in seed_handles:
        progress.setdefault(
            seed.lower(),
            {"next_skip": 0, "pages": 0, "exhausted": False},
        )

    while len(unique_new) < target:
        made_request = False
        for seed in seed_handles:
            if len(unique_new) >= target:
                break
            key = seed.lower()
            item = progress[key]
            group = groups[key]
            if item.get("exhausted") or int(item.get("pages", 0)) >= max_pages_per_seed:
                continue
            if len(group["candidates"]) >= per_seed_max:
                item["exhausted"] = True
                continue
            skip = int(item.get("next_skip", 0))
            body = {
                "usernames": [seed],
                "channel": "INSTAGRAM",
                "limit": page_size,
                "skip": skip,
            }
            try:
                response = page.evaluate(_FETCH_JS, body)
            except Exception as exc:  # noqa: BLE001 - normalize Playwright failures
                raise LookalikeCollectionError(
                    f"Modash Lookalike 请求失败：@{seed} skip={skip} "
                    f"({type(exc).__name__})"
                ) from exc
            made_request = True
            stats["requests"] += 1
            rows, total = validate_response(response, seed)
            item["pages"] = int(item.get("pages", 0)) + 1
            item["next_skip"] = skip + page_size
            item["total"] = total
            if total is None:
                stats["seed_not_found"] += 1
                item["exhausted"] = True
                state["errors"].append({"seed_handle": seed, "error": "seed_not_found"})
            elif not rows or item["next_skip"] >= total:
                item["exhausted"] = True

            accepted_this_page = 0
            already_in_group = {
                _handle(row.get("handle") if isinstance(row, dict) else None).lower()
                for row in group["candidates"]
            }
            for raw_candidate in rows:
                stats["scanned"] += 1
                candidate, rejection = quality_candidate(
                    raw_candidate,
                    seed_handle=seed,
                    follower_min=follower_min,
                    follower_max=follower_max,
                    er_min_pct=er_min_pct,
                    niche_keywords=niche_keywords,
                )
                if rejection:
                    stats[rejection] += 1
                    stats["filtered"] += 1
                    continue
                handle = candidate["handle"]
                candidate_key = handle.lower()
                if not should_ingest(handle):
                    stats["known"] += 1
                    continue
                if candidate_key in already_in_group:
                    stats["duplicate"] += 1
                    stats["duplicate_observed"] += 1
                    continue
                if len(group["candidates"]) >= per_seed_max:
                    stats["filtered"] += 1
                    continue
                if candidate_key in unique_new:
                    # Keep this second seed observation so Stage 1 can merge
                    # golden_seed_handles; it does not consume the unique target.
                    group["candidates"].append(candidate)
                    already_in_group.add(candidate_key)
                    stats["duplicate"] += 1
                    stats["duplicate_observed"] += 1
                    continue
                if accepted_this_page >= per_page_accept:
                    stats["filtered"] += 1
                    continue
                group["candidates"].append(candidate)
                already_in_group.add(candidate_key)
                unique_new.add(candidate_key)
                accepted_this_page += 1
                stats["accepted_unique"] = len(unique_new)
                stats["accepted"] = len(unique_new)

            if len(group["candidates"]) >= per_seed_max:
                item["exhausted"] = True
            state["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            state["status"] = (
                "complete" if len(unique_new) >= target else "in_progress"
            )
            checkpoint(output)
            if len(unique_new) < target and page_delay > 0:
                delay = random_uniform(page_delay * 0.7, page_delay * 1.3)
                try:
                    page.wait_for_timeout(max(0, int(delay * 1000)))
                except Exception as exc:  # noqa: BLE001 - normalize Playwright failures
                    raise LookalikeCollectionError(
                        f"Modash Lookalike 等待失败 ({type(exc).__name__})"
                    ) from exc
        if not made_request:
            break

    state["status"] = "complete" if len(unique_new) >= target else "short"
    state["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    stats["accepted_unique"] = len(unique_new)
    stats["accepted"] = len(unique_new)
    checkpoint(output)
    return {
        "status": state["status"],
        "accepted_unique": len(unique_new),
        "target": target,
        "stats": dict(stats),
    }


def _find_modash_page(browser):
    for context in browser.contexts:
        for page in context.pages:
            if "modash.io" in (page.url or "") and "/identity/login" not in (page.url or ""):
                return page
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="采集 approved 金种子的 Modash Lookalike")
    parser.add_argument("--manifest", required=True, help="Stage1 导出的 golden-lookalikes-v1 模板")
    parser.add_argument("--out", required=True, help="断点/完成结果 JSON")
    parser.add_argument("--track", choices=("paid", "gifting"), default="paid")
    parser.add_argument("--target", type=int, default=60, help="全局可新入库 unique 目标")
    parser.add_argument("--page-size", type=int, default=6)
    parser.add_argument("--max-pages-per-seed", type=int, default=4)
    parser.add_argument("--per-seed-max", type=int, default=4)
    parser.add_argument("--per-page-accept", type=int, default=1,
                        help="每个种子每页最多新增账号；默认 1 以保持来源多样性")
    parser.add_argument("--page-delay", type=float, default=2.2)
    parser.add_argument("--cdp", default="http://127.0.0.1:9222")
    parser.add_argument("--round-contract", default=None)
    parser.add_argument("--require-round-contract", action="store_true")
    args = parser.parse_args(argv)
    for label, value in (
        ("target", args.target),
        ("page-size", args.page_size),
        ("max-pages-per-seed", args.max_pages_per_seed),
        ("per-seed-max", args.per_seed_max),
        ("per-page-accept", args.per_page_accept),
    ):
        if value <= 0:
            parser.error(f"--{label} 必须为正整数")
    if args.page_delay < 0:
        parser.error("--page-delay 不能为负数")
    if Path(args.manifest).resolve() == Path(args.out).resolve():
        parser.error("--out 不能覆盖原始 --manifest")

    try:
        manifest, seed_handles = load_manifest(args.manifest)
        contract_sha = None
        if args.round_contract:
            contract_path = Path(args.round_contract)
            round_contract_mod.assert_contract_matches_runtime(
                round_contract_mod.load_round_contract(contract_path),
                batch_id=str(manifest["batch_id"]),
                campaign_track=args.track,
            )
            contract_sha = round_contract_mod.round_contract_sha256(contract_path)
        elif args.require_round_contract:
            raise LookalikeCollectionError("正式 Lookalike 要求 --round-contract")
        cfg = load_config()
        output = load_or_initialize_output(manifest, seed_handles, args.out)
        _collection_state(
            output,
            track=args.track,
            target=args.target,
            page_size=args.page_size,
            max_pages_per_seed=args.max_pages_per_seed,
            per_seed_max=args.per_seed_max,
            per_page_accept=args.per_page_accept,
            round_contract_sha256=contract_sha,
        )
        atomic_write_json(args.out, output)
    except (OSError, ms.GoldenLookalikeInputError,
            round_contract_mod.RoundContractError, LookalikeCollectionError) as exc:
        print(f"✗ Lookalike 准备失败：{exc}")
        return 2

    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            # ``Browser.close()`` on a CDP attachment can close the user's
            # logged-in Chrome. Leaving Playwright's context only disconnects
            # this client, so never close the attached browser explicitly.
            browser = playwright.chromium.connect_over_cdp(args.cdp)
            page = _find_modash_page(browser)
            if page is None:
                raise LookalikeCollectionError("未找到已登录 Modash Discovery 标签")
            result = collect_with_page(
                page,
                output,
                seed_handles,
                cfg=cfg,
                track=args.track,
                target=args.target,
                page_size=args.page_size,
                max_pages_per_seed=args.max_pages_per_seed,
                per_seed_max=args.per_seed_max,
                per_page_accept=args.per_page_accept,
                checkpoint=lambda value: atomic_write_json(args.out, value),
                page_delay=args.page_delay,
            )
    except Exception as exc:  # noqa: BLE001 - preserve resumable CLI state
        output["collection"]["status"] = "error"
        output["collection"]["errors"].append({"error": str(exc)[:300]})
        output["collection"]["updated_at"] = time.strftime(
            "%Y-%m-%dT%H:%M:%S%z"
        )
        try:
            atomic_write_json(args.out, output)
        except OSError:
            pass
        print(f"✗ Modash Lookalike 中止：{exc}")
        return 2

    stats = result["stats"]
    print(
        "✓ Modash Lookalike "
        f"unique={result['accepted_unique']}/{result['target']} · "
        f"requests={stats['requests']} scanned={stats['scanned']} · "
        f"known={stats['known']} duplicate={stats['duplicate']} · "
        f"filtered={stats['filtered']} accepted={stats['accepted']}"
    )
    print(f"  结果：{args.out}")
    return 0 if result["status"] == "complete" else 2


if __name__ == "__main__":
    raise SystemExit(main())
