"""Modash 结构化搜索发现（替换 AI Search 只捞 6 个小号的老路）。

实测契约（2026-07-16）：POST /api/search/v2/instagram
  body = {skip, limit:6, search_origin:"lookalikes", query:<NL 描述>, filters:{...}}
  filters.followers = {min, max}；filters.engagementRate = {min}
  响应 {results:[{username, follower_count, engagement_rate, is_brand, is_private,
        creator_description(bio), account_category, ...}], total}
每次上限 6 → 用 skip 分页（0,6,12,…）累积；搜索列表**免费**（不耗 credit，只有开 report 才耗）。
响应已含粉丝/ER/品牌/bio/类目 → **发现阶段就预过滤**（排品牌/私密、要 amazon bio），产出高质量 seed。
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path


GOLDEN_LOOKALIKE_SCHEMA_VERSION = 1
_HANDLE_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")


class GoldenLookalikeInputError(ValueError):
    """人工 Modash Lookalike 结果不满足可审计导入契约。"""


def _handle(value) -> str:
    h = str(value or "").strip().lstrip("@")
    return h if _HANDLE_RE.fullmatch(h) else ""


def golden_seed_fingerprint(handles: list[str]) -> str:
    """对批准种子集合做稳定指纹，防止把别批/旧版本 Lookalike 结果错接进来。"""
    normalized = sorted({_handle(h).lower() for h in handles if _handle(h)})
    return hashlib.sha256("\n".join(normalized).encode("utf-8")).hexdigest()


def build_golden_seed_manifest(batch_id: str, handles: list[str],
                               generated_at: str | None = None) -> dict:
    """生成给人工 Modash Lookalike 步骤使用的种子清单。

    种子由 ``creator_cache.golden_seeds`` 提供；该 API 只返回
    ``tier=2 AND client_status IN ('approved','collaborated')``。manifest 中保留
    明确资格条件与集合指纹，后续结果导入必须逐项匹配。
    """
    seeds = []
    seen = set()
    for raw in handles:
        h = _handle(raw)
        if not h or h.lower() in seen:
            continue
        seen.add(h.lower())
        seeds.append({"handle": h})
    return {
        "schema_version": GOLDEN_LOOKALIKE_SCHEMA_VERSION,
        "format": "golden-lookalikes-v1",
        "batch_id": batch_id,
        "generated_at": generated_at or time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "source": "creator_cache.golden_seeds",
        "eligibility": "tier=2 AND client_status IN ('approved','collaborated')",
        "seed_set_sha256": golden_seed_fingerprint([s["handle"] for s in seeds]),
        "seed_count": len(seeds),
        "seeds": seeds,
        # 该 manifest 本身就是可填写/另存的结果模板，避免人工另猜字段。
        "results": [
            {"seed_handle": seed["handle"], "candidates": []}
            for seed in seeds
        ],
        "manual_next_step": (
            "在 Modash 对这些批准种子执行 Lookalike；把候选填入对应 results[].candidates "
            "并另存，再用 --golden-lookalikes-json 导入。候选字段为 handle(必填)、"
            "followers/er_pct(可选)。"
        ),
    }


def write_golden_seed_manifest(path: str | Path, manifest: dict) -> Path:
    """写 manifest，但不覆盖不同种子集合的既有审计文件。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists():
        try:
            current = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            current = {}
        same_cohort = (
            current.get("batch_id") == manifest.get("batch_id")
            and current.get("seed_set_sha256") == manifest.get("seed_set_sha256")
        )
        if same_cohort:
            return p
        suffix = str(manifest.get("seed_set_sha256") or "unknown")[:12]
        p = p.with_name(f"{p.stem}.{suffix}{p.suffix}")
        if p.exists():
            try:
                current = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                current = {}
            if (
                current.get("batch_id") == manifest.get("batch_id")
                and current.get("seed_set_sha256") == manifest.get("seed_set_sha256")
            ):
                return p
            raise GoldenLookalikeInputError(f"manifest 目标已存在且内容不一致：{p}")
    p.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def load_golden_lookalikes(path: str | Path, *, batch_id: str,
                           golden_handles: list[str]) -> list[dict]:
    """严格读取人工 Modash Lookalike 结果，转为 Stage1 seed records。

    输入契约 ``golden-lookalikes-v1``::

        {
          "schema_version": 1,
          "batch_id": "SKIN4-...",
          "seed_set_sha256": "<golden manifest 中的值>",
          "results": [
            {
              "seed_handle": "approved_seed",
              "candidates": [
                {"handle": "candidate", "followers": 12345, "er_pct": 2.4}
              ]
            }
          ]
        }

    不接受裸 ``lookalikesToken``。现有 show-profile 缓存中的 token 是不透明引用，
    并不包含可离线抽取的候选账号。
    """
    p = Path(path)
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise GoldenLookalikeInputError(f"结果文件不存在：{p}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise GoldenLookalikeInputError(f"结果文件不可读或不是合法 JSON：{p}") from exc
    if not isinstance(payload, dict):
        raise GoldenLookalikeInputError("结果根节点必须是对象")
    if payload.get("schema_version") != GOLDEN_LOOKALIKE_SCHEMA_VERSION:
        raise GoldenLookalikeInputError(
            f"schema_version 必须是 {GOLDEN_LOOKALIKE_SCHEMA_VERSION}"
        )
    if payload.get("batch_id") != batch_id:
        raise GoldenLookalikeInputError(
            f"结果 batch_id={payload.get('batch_id')!r}，预期 {batch_id!r}"
        )
    expected_sha = golden_seed_fingerprint(golden_handles)
    if payload.get("seed_set_sha256") != expected_sha:
        raise GoldenLookalikeInputError("seed_set_sha256 与当前批准种子集合不一致")

    allowed = {_handle(h).lower() for h in golden_handles if _handle(h)}
    results = payload.get("results")
    if not isinstance(results, list):
        raise GoldenLookalikeInputError("results 必须是数组")

    records = []
    seen_seed_rows = set()
    for index, group in enumerate(results, 1):
        if not isinstance(group, dict):
            raise GoldenLookalikeInputError(f"results[{index}] 必须是对象")
        source_seed = _handle(group.get("seed_handle"))
        if not source_seed or source_seed.lower() not in allowed:
            raise GoldenLookalikeInputError(
                f"results[{index}].seed_handle 不是当前 approved/collaborated 金种子"
            )
        source_key = source_seed.lower()
        if source_key in seen_seed_rows:
            raise GoldenLookalikeInputError(f"种子 @{source_seed} 在 results 中重复")
        seen_seed_rows.add(source_key)
        candidates = group.get("candidates")
        if not isinstance(candidates, list):
            raise GoldenLookalikeInputError(
                f"results[{index}].candidates 必须是数组"
            )
        via = f"modash_manual_lookalike:golden:{source_seed}"
        for candidate_index, item in enumerate(candidates, 1):
            if not isinstance(item, dict):
                raise GoldenLookalikeInputError(
                    f"results[{index}].candidates[{candidate_index}] 必须是对象"
                )
            h = _handle(item.get("handle"))
            if not h:
                raise GoldenLookalikeInputError(
                    f"results[{index}].candidates[{candidate_index}].handle 无效"
                )
            followers = item.get("followers")
            if followers is not None:
                if isinstance(followers, bool):
                    raise GoldenLookalikeInputError(f"@{h} followers 必须是非负整数")
                try:
                    followers = int(followers)
                except (TypeError, ValueError) as exc:
                    raise GoldenLookalikeInputError(f"@{h} followers 必须是非负整数") from exc
                if followers < 0:
                    raise GoldenLookalikeInputError(f"@{h} followers 必须是非负整数")
            er_pct = item.get("er_pct")
            if er_pct is not None:
                if isinstance(er_pct, bool):
                    raise GoldenLookalikeInputError(f"@{h} er_pct 必须在 0-100")
                try:
                    er_pct = float(er_pct)
                except (TypeError, ValueError) as exc:
                    raise GoldenLookalikeInputError(f"@{h} er_pct 必须在 0-100") from exc
                if not 0 <= er_pct <= 100:
                    raise GoldenLookalikeInputError(f"@{h} er_pct 必须在 0-100")
            records.append({
                "handle": h,
                "followers": followers,
                "er": er_pct,
                "discovered_via": via,
                "discovery_sources": [via],
                "golden_seed_handles": [source_seed],
            })
    return merge_seed_records(records)


def merge_seed_records(*groups: list[dict]) -> list[dict]:
    """按 handle 合并多源发现，保留全部来源与批准种子链路。"""
    merged: dict[str, dict] = {}
    order = []
    for group in groups:
        for raw in group:
            h = _handle(raw.get("handle"))
            if not h:
                continue
            key = h.lower()
            sources = list(raw.get("discovery_sources") or [])
            discovered_via = raw.get("discovered_via")
            # multi_source 是合并后的派生标记，不是真实发现来源；
            # 重复合并时不得把它污染进 discovery_sources。
            if (
                discovered_via
                and discovered_via != "multi_source"
                and discovered_via not in sources
            ):
                sources.append(discovered_via)
            golden = [_handle(x) for x in (raw.get("golden_seed_handles") or [])]
            golden = [x for x in golden if x]
            if key not in merged:
                rec = dict(raw)
                rec["handle"] = h
                rec["discovery_sources"] = []
                rec["golden_seed_handles"] = []
                merged[key] = rec
                order.append(key)
            rec = merged[key]
            for source in sources:
                if source and source not in rec["discovery_sources"]:
                    rec["discovery_sources"].append(source)
            for seed in golden:
                if seed.lower() not in {x.lower() for x in rec["golden_seed_handles"]}:
                    rec["golden_seed_handles"].append(seed)
            for field in ("followers", "er", "service_platform_id", "bio", "category"):
                if rec.get(field) is None and raw.get(field) is not None:
                    rec[field] = raw[field]
    for key in order:
        rec = merged[key]
        sources = rec["discovery_sources"]
        rec["discovered_via"] = (
            sources[0] if len(sources) == 1
            else "multi_source" if sources
            else "unknown"
        )
    return [merged[key] for key in order]


_SEARCH_JS = """async (args) => {
  const controller = new AbortController();
  const timeoutMs = Math.max(1, Number(args.timeoutMs) || 20000);
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const r = await fetch('/api/search/v2/instagram', {method:'POST',
      headers:{'content-type':'application/json'}, body: JSON.stringify(args.body),
      signal: controller.signal});
    return {status: r.status, text: await r.text(), timedOut: false, error: null};
  } catch (error) {
    const timedOut = controller.signal.aborted || (error && error.name === 'AbortError');
    return {status: null, text: '', timedOut,
      error: timedOut ? 'request_timeout' : ((error && error.name) || 'fetch_error')};
  } finally {
    clearTimeout(timer);
  }
}"""


def _find_modash(b):
    for c in b.contexts:
        for p in c.pages:
            if "modash.io" in (p.url or ""):
                return p
    return None


def _page(pg, query, filters, skip, limit=6, request_timeout_ms=20_000):
    """Fetch one search page and return an explicit result/error envelope."""
    body = {"skip": skip, "limit": limit, "search_origin": "lookalikes",
            "query": query, "filters": filters}
    timeout_ms = max(1, int(request_timeout_ms))
    try:
        r = pg.evaluate(_SEARCH_JS, {"body": body, "timeoutMs": timeout_ms})
    except Exception as exc:  # noqa: BLE001 - normalize Playwright transport failures
        return {
            "results": [],
            "error": f"playwright_{type(exc).__name__}",
            "status": None,
        }
    if not isinstance(r, dict):
        return {"results": [], "error": "invalid_response", "status": None}
    if r.get("timedOut") or r.get("error") == "request_timeout":
        return {"results": [], "error": "request_timeout", "status": None}
    if r.get("error"):
        return {"results": [], "error": "request_failed", "status": None}
    status = r.get("status")
    if status != 200:
        return {"results": [], "error": f"http_{status}", "status": status}
    try:
        payload = json.loads(r["text"])
    except (KeyError, TypeError, json.JSONDecodeError):
        return {"results": [], "error": "invalid_json", "status": status}
    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list):
        return {"results": [], "error": "invalid_results", "status": status}
    return {"results": results, "error": None, "status": status}


def _seed(x: dict) -> dict:
    return {"handle": (x.get("username") or "").lstrip("@"),
            "followers": x.get("follower_count"),
            "er": round((x.get("engagement_rate") or 0) * 100, 2),
            "is_brand": bool(x.get("is_brand")),
            "is_private": bool(x.get("is_private")),
            "bio": x.get("creator_description") or "",
            "category": x.get("account_category"),
            # show-profile 要的哈希 ID（存下→stage4 补数免重搜；user_id 数字/serviceSdId 都不对）
            "service_platform_id": x.get("servicePlatformId")}


_AMAZON_BIO = ("amazon", "amzn", "storefront", "shop my", "ltk", "liketoknow", "linktr",
               "beacons", "founditon", "shopmy")


def _keep(s: dict, require_amazon_bio: bool) -> bool:
    """发现阶段预过滤：排品牌/私密；可选要 bio 提及 amazon/橱窗（免费提质）。"""
    if s["is_brand"] or s["is_private"]:
        return False
    if require_amazon_bio and not any(k in (s["bio"] or "").lower() for k in _AMAZON_BIO):
        return False
    return True


def discover(query: str, filters: dict, target: int = 120, max_pages: int = 80,
             require_amazon_bio: bool = True, page_delay: float = 2.2,
             cdp_url: str = "http://127.0.0.1:9222",
             quota_accept=None, request_timeout_ms: int = 20_000,
             sleeper=time.sleep) -> dict:
    """结构化搜索 + skip 分页 + 预过滤 → 高质量 seed 列表。

    ``quota_accept`` 缺省时完全保持旧语义：预过滤后的 ``kept`` 达到
    ``target`` 即停。给定谓词时，只有谓词接受的候选计入配额，但
    ``seeds`` 仍返回分页期间实际观察到的全部预过滤候选，供上层保留
    跨来源归因。返回 ``accepted`` 为实际计入配额数。

    page_delay：每页基准停顿秒数（拟人，降 Modash 风控），实际取 [0.7x,1.4x] 随机 + 每10页长歇。
    纯节奏等待通过可注入 ``sleeper`` 执行，不调用 Playwright
    ``wait_for_timeout``，避免 CDP 附着模式下同步等待卡死。
    """
    import random
    from playwright.sync_api import sync_playwright
    seeds, seen = [], set()
    scanned = kept = accepted = 0

    def result(error=None):
        payload = {
            "seeds": seeds,
            "raw_scanned": scanned,
            "kept": kept,
            "accepted": accepted,
            "filtered_out": scanned - kept,
        }
        if error:
            payload["error"] = error
        return payload

    def rhythm_sleep(seconds):
        if seconds > 0:
            sleeper(seconds)

    with sync_playwright() as pw:
        b = pw.chromium.connect_over_cdp(cdp_url)
        # connect_over_cdp 附着的是用户已登录 Chrome。离开
        # sync_playwright 上下文即断开客户端；不得调用 b.close()。
        pg = _find_modash(b)
        if not pg:
            return result("no_modash_tab")
        for _ in range(2):
            pg.keyboard.press("Escape")
            rhythm_sleep(0.3)
        for i in range(max_pages):
            page_result = _page(
                pg,
                query,
                filters,
                skip=i * 6,
                request_timeout_ms=request_timeout_ms,
            )
            # 兼容旧测试/内部 monkeypatch 直接返回 results list；真实
            # _page 始终返回带 error/status 的 envelope。
            if isinstance(page_result, list):
                rows, page_error = page_result, None
            elif isinstance(page_result, dict):
                rows = page_result.get("results")
                page_error = page_result.get("error")
                if not isinstance(rows, list):
                    return result(page_error or "invalid_page_result")
            else:
                return result("invalid_page_result")
            if page_error:
                return result(page_error)
            if not rows:
                break
            for x in rows:
                s = _seed(x)
                h = s["handle"].lower()
                if not h or h in seen:
                    continue
                seen.add(h)
                scanned += 1
                if _keep(s, require_amazon_bio):
                    seeds.append(s)
                    kept += 1
                    if quota_accept is None or quota_accept(s):
                        accepted += 1
            if accepted >= target:
                break
            # 拟人节奏：随机停顿；每 10 页一段更长的歇口，更像手动翻页
            delay = random.uniform(page_delay * 0.7, page_delay * 1.4)
            if i and i % 10 == 0:
                delay += random.uniform(4, 8)
            rhythm_sleep(delay)
            rhythm_sleep(0.4)
    return result()
