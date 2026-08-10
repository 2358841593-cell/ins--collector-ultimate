"""展示型预估报价派生。

客户口径（2026-07-28）：

- 曝光基准 = 最近 10 个非置顶 Reels 的平均播放量；
- 默认 CPM = 35 USD，参考区间 = 35–40 USD；
- 预估报价 = 平均播放量 / 1000 × CPM。

这是估算字段，不是红人/代理给出的实际报价，也不写入 ``paid_cpm``，默认不参与 Gate、
F 模块评分或最终路由。报价只允许使用 Instagram 原生 ``ig_play_count``。少于 10 条时，
只有 Reels Tab 已经用可复核的“底部连续无增长”证据证明穷尽，且每条 Reel 都完成了
同源身份与置顶分类，才可把全部可用样本作为完整总体；已确认置顶的 Reel 直接排除，
无需暴露播放量，只有已确认非置顶的 Reel 才必须具备原生 ``ig_play_count``。第三方均播、
总播放量和 Facebook 播放量一律不能代替。
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
import hashlib
import re
from urllib.parse import urlparse


REELS_TAB_EVIDENCE_SCHEMA_VERSION = 1
REELS_TAB_EVIDENCE_SOURCE = "instagram_reels_tab_grid"
REELS_TAB_EXHAUSTED_REASON = "stable_bottom_no_growth"
MIN_STABLE_BOTTOM_ROUNDS = 2
DELIVERY_COMPLETE_STATUSES = frozenset(
    {"complete", "complete_available", "not_applicable_no_reels"}
)
_INSTAGRAM_MEDIA_HOSTS = frozenset({"instagram.com", "www.instagram.com"})
_MEDIA_ROUTE_RE = re.compile(
    r"^/(?:[A-Za-z0-9._]+/)?(?P<kind>reel|p)/"
    r"(?P<code>[A-Za-z0-9_-]+)/?$",
    re.IGNORECASE,
)
_SHORTCODE_RE = re.compile(r"^[A-Za-z0-9_-]+$")
CANONICAL_ESTIMATE_FIELDS = (
    "schema_version",
    "status",
    "currency",
    "method",
    "requested_reels",
    "sample_count",
    "average_plays",
    "source",
    "population_basis",
    "population_evidence",
    "pinned_excluded",
    "cpm_usd",
    "quote_usd",
    "captured_at",
    "reels",
)


def _observed_views(post: dict):
    if post.get("play_count_status") not in (None, "observed"):
        return None
    metric_source = post.get("play_count_source")
    # 一旦样本声明了来源，只接受 IG 原生 ig_play_count。总 play_count 可能含 FB
    # cross-post，不得进入均播。无来源仅保留给旧数据/纯函数测试兼容。
    if metric_source and metric_source != "ig_media_info.ig_play_count":
        return None
    value = post.get("play_count")
    if value is None:
        value = post.get("view_count")
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return n if n >= 0 else None


def _is_reel(post: dict) -> bool:
    return bool(
        post.get("is_reel")
        or "/reel/" in str(post.get("code") or post.get("url") or "")
    )


def _money(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _post_identity(post: dict):
    values = [post.get("code"), post.get("url")]
    for value in values:
        raw = str(value or "").strip()
        match = re.search(r"/(?:[^/?#]+/)?(reel|p)/([A-Za-z0-9_-]+)", raw)
        if match:
            return f"{match.group(1)}:{match.group(2)}"
    raw = str(next((value for value in values if value), "")).strip()
    return raw.split("?", 1)[0].split("#", 1)[0].rstrip("/") or None


def _media_route(
    value, *, allow_bare_shortcode=False, require_absolute_instagram=False
):
    """Parse one row/canonical media ref without trusting another field."""
    raw = str(value or "").strip()
    if not raw:
        return None
    if allow_bare_shortcode and _SHORTCODE_RE.fullmatch(raw):
        return "reel", raw
    parsed = urlparse(raw)
    if parsed.scheme or parsed.netloc:
        if (
            parsed.scheme.lower() != "https"
            or (parsed.hostname or "").lower() not in _INSTAGRAM_MEDIA_HOSTS
        ):
            return None
    elif require_absolute_instagram:
        return None
    match = _MEDIA_ROUTE_RE.fullmatch(parsed.path)
    if not match:
        return None
    return match.group("kind").lower(), match.group("code")


def _row_media_route(post: dict):
    """Bind every populated row reference to one exact Reel shortcode."""
    routes = []
    for field in ("code", "url"):
        value = post.get(field)
        if value in (None, ""):
            continue
        route = _media_route(value, allow_bare_shortcode=field == "code")
        if route is None:
            return None
        routes.append(route)
    if not routes or any(route != routes[0] for route in routes[1:]):
        return None
    return routes[0] if routes[0][0] == "reel" else None


def _media_provenance_matches_row(post: dict, provenance: dict) -> bool:
    """Verify the media-info identity chain is cryptographically row-local.

    ``identity_verified`` alone is not trusted: a valid bundle copied from Reel B
    must not classify Reel A.  The original shortcode is therefore bound to this
    row, and any canonical alias must also be bound to an HTTPS Instagram route of
    the same media kind.
    """
    route = _row_media_route(post)
    if route is None:
        return False
    kind, row_shortcode = route
    original = str(provenance.get("original_shortcode") or "")
    requested = str(provenance.get("requested_shortcode") or "")
    canonical_value = provenance.get("canonical_shortcode")
    canonical = str(canonical_value or "")
    response = str(provenance.get("response_code") or "")
    if original != row_shortcode or provenance.get("identity_verified") is not True:
        return False
    if canonical:
        canonical_route = _media_route(
            provenance.get("page_canonical_url"),
            require_absolute_instagram=True,
        )
        if not (
            original.startswith(canonical)
            and requested == canonical
            and canonical_route == (kind, canonical)
        ):
            return False
        allowed_responses = {original, canonical}
    else:
        if requested != original or provenance.get("page_canonical_url") not in (
            None,
            "",
        ):
            return False
        allowed_responses = {original}
    return bool(response and response in allowed_responses)


def ordered_reel_identities(posts) -> list[str]:
    """Return ordered unique Reel identities using the quote-window convention."""
    output, seen = [], set()
    for post in posts or []:
        if not isinstance(post, dict) or not _is_reel(post):
            continue
        identity = _post_identity(post)
        if not identity or identity in seen:
            continue
        seen.add(identity)
        output.append(identity)
    return output


def reel_identity_sha256(posts) -> str:
    """Fingerprint the exact ordered Reels population observed in the tab."""
    payload = "\n".join(ordered_reel_identities(posts)).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _identity_list_sha256(identities) -> str:
    payload = "\n".join(str(value) for value in identities).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _valid_surface_probe_snapshots(
    evidence: dict,
    *,
    expected_handle: str,
    expected_reels_path: str,
    expected_profile_path: str,
    required_navigations: int | None,
) -> bool:
    snapshots = evidence.get("reels_surface_probe_snapshots")
    navigations = _exact_nonnegative_int(
        evidence.get("reels_surface_probe_navigations")
    )
    if not (
        isinstance(snapshots, list)
        and required_navigations is not None
        and required_navigations >= 2
        and navigations is not None
        and navigations >= required_navigations
        and len(snapshots) == navigations
    ):
        return False
    for index, snapshot in enumerate(snapshots, start=1):
        if not isinstance(snapshot, dict):
            return False
        identities = snapshot.get("feed_post_identities")
        feed_count = _exact_nonnegative_int(snapshot.get("feed_post_count"))
        if not (
            snapshot.get("navigation_index") == index
            and str(snapshot.get("requested_pathname") or "").rstrip("/").lower()
            == expected_reels_path
            and str(snapshot.get("final_pathname") or "").rstrip("/").lower()
            == expected_profile_path
            and str(snapshot.get("expected_handle") or "").lower()
            == expected_handle
            and str(snapshot.get("observed_handle") or "").lower()
            == expected_handle
            and snapshot.get("page_identity_verified") is True
            and snapshot.get("profile_healthy") is True
            and snapshot.get("redirected_to_profile") is True
            and snapshot.get("reels_tab_route_verified") is False
            and snapshot.get("reels_tab_link_present") is False
            and _exact_nonnegative_int(snapshot.get("reel_links_seen")) == 0
            and snapshot.get("loading_visible") is False
            and snapshot.get("challenge") is False
            and snapshot.get("logged_out") is False
            and snapshot.get("private_account") is False
            and snapshot.get("error_page") is False
            and isinstance(snapshot.get("captured_at"), str)
            and bool(snapshot.get("captured_at"))
            and isinstance(identities, list)
            and feed_count is not None
            and feed_count > 0
            and len(identities) == feed_count
            and len(set(identities)) == feed_count
            and all(
                isinstance(identity, str)
                and re.fullmatch(r"p:[A-Za-z0-9_-]+", identity)
                for identity in identities
            )
            and snapshot.get("feed_post_identity_sha256")
            == _identity_list_sha256(identities)
        ):
            return False
    return True


def _exact_nonnegative_int(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _strict_native_media_row(post: dict) -> bool:
    """Require the exact authenticated media-info bundle for short populations."""
    provenance = post.get("media_identity_provenance")
    if not isinstance(provenance, dict):
        return False
    identity_and_pin_verified = bool(
        isinstance(post.get("pinned"), bool)
        and post.get("pinned_source") == "ig_media_info_pin_lists"
        and _media_provenance_matches_row(post, provenance)
    )
    if not identity_and_pin_verified:
        return False
    # 置顶 Reel 仍须通过同源 media-info 200 的身份与 pin-list 回证，但它已被
    # 报价窗口排除，因此 IG 未暴露播放量并不影响总体闭合。非置顶 Reel 才需要
    # 原生 ig_play_count；total/fb/第三方指标都不能替代。
    if post.get("pinned") is True:
        return True
    return bool(
        post.get("play_count_source") == "ig_media_info.ig_play_count"
        and post.get("play_count_status") == "observed"
        and _observed_views(post) is not None
    )


def _population_state(cand: dict, posts: list[dict]) -> dict:
    """Validate browser-produced Reels-tab exhaustion and population coverage.

    ``reels_tab_exhausted`` proves only that the grid ended. ``population_complete``
    additionally proves that every observed Reel was persisted exactly once and was
    either confirmed pinned or had an observed native IG play count. Confirmed pinned
    rows need no play count because they are excluded from pricing, but still require
    an identity-verified media-info response. This distinction prevents a short grid
    with failed media-info calls from being called complete.
    """
    evidence = cand.get("pricing_reels_tab_evidence")
    evidence = evidence if isinstance(evidence, dict) else {}
    reel_rows = [post for post in posts if isinstance(post, dict) and _is_reel(post)]
    identities = ordered_reel_identities(reel_rows)
    seen = _exact_nonnegative_int(evidence.get("unique_reels_seen"))
    stable = _exact_nonnegative_int(evidence.get("stable_bottom_rounds"))
    required = _exact_nonnegative_int(
        evidence.get("required_stable_bottom_rounds")
    )
    scroll_attempts = _exact_nonnegative_int(evidence.get("scroll_attempts"))
    fingerprint = reel_identity_sha256(reel_rows)
    expected_handle = str(cand.get("handle") or "").strip().lstrip("@").lower()
    evidence_expected = str(evidence.get("expected_handle") or "").lower()
    observed_handle = str(evidence.get("observed_handle") or "").lower()
    requested_path = str(evidence.get("requested_pathname") or "").rstrip("/")
    final_path = str(evidence.get("final_pathname") or "").rstrip("/")
    expected_reels_path = f"/{expected_handle}/reels"
    expected_profile_path = f"/{expected_handle}"
    common_proof = bool(
        evidence.get("schema_version") == REELS_TAB_EVIDENCE_SCHEMA_VERSION
        and evidence.get("source") == REELS_TAB_EVIDENCE_SOURCE
        and evidence.get("loading_visible") is False
        and evidence.get("page_identity_verified") is True
        and bool(expected_handle)
        and evidence_expected == expected_handle
        and observed_handle == expected_handle
        and requested_path.lower() == expected_reels_path
        and evidence.get("challenge") is False
        and evidence.get("logged_out") is False
        and evidence.get("private_account") is False
        and evidence.get("error_page") is False
        and isinstance(evidence.get("captured_at"), str)
        and bool(evidence.get("captured_at"))
        and seen is not None
        and seen == len(identities)
        and evidence.get("ordered_reel_identity_sha256") == fingerprint
    )
    direct_exhaustion = bool(
        common_proof
        and evidence.get("status") == "exhausted"
        and evidence.get("reason") == REELS_TAB_EXHAUSTED_REASON
        and evidence.get("at_bottom") is True
        and evidence.get("reels_tab_route_verified") is True
        and evidence.get("page_healthy") is True
        and final_path.lower() == expected_reels_path
        and required is not None
        and required >= MIN_STABLE_BOTTOM_ROUNDS
        and stable is not None
        and stable >= required
        and scroll_attempts is not None
        and scroll_attempts >= stable
    )
    profile_probe_rounds = _exact_nonnegative_int(
        evidence.get("profile_probe_rounds")
    )
    required_profile_rounds = _exact_nonnegative_int(
        evidence.get("required_profile_probe_rounds")
    )
    surface_navigations = _exact_nonnegative_int(
        evidence.get("reels_surface_probe_navigations")
    )
    required_surface_navigations = _exact_nonnegative_int(
        evidence.get("required_reels_surface_probe_navigations")
    )
    no_reels_tab_proof = bool(
        common_proof
        and evidence.get("status") == "reels_surface_absent"
        and evidence.get("reason")
        == "reels_route_redirected_to_healthy_profile_without_reels_surface"
        and evidence.get("reels_tab_route_verified") is False
        and evidence.get("redirected_to_profile") is True
        and final_path.lower() == expected_profile_path
        and evidence.get("profile_healthy") is True
        and evidence.get("reels_tab_link_present") is False
        and _exact_nonnegative_int(evidence.get("reel_links_seen")) == 0
        and (_exact_nonnegative_int(evidence.get("unique_feed_posts_seen")) or 0)
        > 0
        and required_profile_rounds is not None
        and required_profile_rounds >= 2
        and profile_probe_rounds is not None
        and profile_probe_rounds >= required_profile_rounds
        and required_surface_navigations is not None
        and required_surface_navigations >= 2
        and surface_navigations is not None
        and surface_navigations >= required_surface_navigations
        and _valid_surface_probe_snapshots(
            evidence,
            expected_handle=expected_handle,
            expected_reels_path=expected_reels_path,
            expected_profile_path=expected_profile_path,
            required_navigations=required_surface_navigations,
        )
        and seen == 0
        and not identities
    )
    exhausted = direct_exhaustion or no_reels_tab_proof
    classified = bool(
        exhausted
        and len(reel_rows) == len(identities)
        and all(_strict_native_media_row(post) for post in reel_rows)
    )
    empty_state_verified = bool(
        direct_exhaustion
        and seen == 0
        and evidence.get("empty_state_verified") is True
        and isinstance(evidence.get("empty_reels_marker"), str)
        and evidence.get("empty_reels_marker")
        in {"No Reels Yet", "No reels yet", "No posts yet"}
    ) or no_reels_tab_proof
    return {
        "evidence_status": evidence.get("status") or "unknown",
        "evidence_reason": evidence.get("reason"),
        "population_closed": exhausted,
        "reels_tab_exhausted": direct_exhaustion,
        "reels_surface_absent": no_reels_tab_proof,
        "population_complete": classified,
        "empty_state_verified": empty_state_verified,
        "proof_mode": (
            "stable_reels_tab_bottom"
            if direct_exhaustion
            else (
                "reels_surface_absent"
                if no_reels_tab_proof
                else "unproven"
            )
        ),
        "reels_seen": seen,
        "classified_reels": len(reel_rows) if classified else None,
        "stable_bottom_rounds": stable,
        "required_stable_bottom_rounds": required,
        "profile_probe_rounds": profile_probe_rounds,
        "required_profile_probe_rounds": required_profile_rounds,
        "reels_surface_probe_navigations": surface_navigations,
        "required_reels_surface_probe_navigations": (
            required_surface_navigations
        ),
        "ordered_reel_identity_sha256": fingerprint if exhausted else None,
        "captured_at": evidence.get("captured_at"),
    }


def _ordered_posts(cand: dict) -> list[dict]:
    """优先使用 Reels 专页的显式 grid_rank；否则只在时间戳完整时按时间倒序。

    当时间戳有缺失/混合类型时保留采集顺序，避免“较旧但有时间戳”的 Reel 挤掉
    “较新但时间戳暂缺”的 Reel。
    """
    posts = list(cand.get("pricing_reel_samples") or [])
    if posts and all(
        isinstance(post.get("grid_rank"), int)
        and not isinstance(post.get("grid_rank"), bool)
        for post in posts
    ):
        return sorted(posts, key=lambda post: post["grid_rank"])

    numeric_times = []
    for post in posts:
        try:
            numeric_times.append(float(post.get("taken_at")))
        except (TypeError, ValueError):
            return posts
    return [
        post
        for _, post in sorted(
            zip(numeric_times, posts), key=lambda pair: pair[0], reverse=True
        )
    ]


def derive_quote_estimate(cand: dict, cfg: dict) -> dict:
    p = cfg.get("pricing_estimate", {})
    window = int(p.get("reels_window", 10))
    default_cpm = float(p.get("default_cpm_usd", 35.0))
    low_cpm = float(p.get("cpm_min_usd", 35.0))
    high_cpm = float(p.get("cpm_max_usd", 40.0))

    eligible = []
    seen = set()
    posts = _ordered_posts(cand)
    pinned_excluded = sum(
        1 for post in posts if _is_reel(post) and post.get("pinned") is True
    )
    for post in posts:
        if not _is_reel(post):
            continue
        if post.get("pinned") is True:
            continue
        if post.get("pinned") is not False:
            continue
        views = _observed_views(post)
        if views is None:
            continue
        identity = _post_identity(post)
        if identity and identity in seen:
            continue
        if identity:
            seen.add(identity)
        eligible.append(
            {
                "code": post.get("code"),
                "url": post.get("url"),
                "taken_at": post.get("taken_at"),
                "play_count": int(round(views)),
                "metric_status": post.get("play_count_status") or "observed",
                "metric_source": (
                    post.get("play_count_source")
                    or "ig_media_info.ig_play_count"
                ),
                "pinned": False,
                "source": "instagram_media_info_ig_play_count",
                "captured_at": post.get("captured_at") or cand.get("captured_at"),
            }
        )
        if len(eligible) >= window:
            break

    population = _population_state(cand, posts)
    if eligible:
        avg_decimal = sum(Decimal(x["play_count"]) for x in eligible) / Decimal(len(eligible))
        avg_views = float(avg_decimal.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        source = "instagram_media_info_ig_play_count"
        sample_count = len(eligible)
        samples = eligible
        if sample_count >= window:
            status = "complete"
            population_basis = "recent_non_pinned_reels_window"
        elif population["population_complete"]:
            status = "complete_available"
            population_basis = "all_available_non_pinned_reels"
        else:
            status = "partial"
            population_basis = "unproven_partial_window"
    elif (
        population["population_closed"]
        and population["population_complete"]
        and population["reels_seen"] == 0
        and population["empty_state_verified"]
    ):
        avg_decimal = None
        avg_views = None
        source = (
            "instagram_profile_reels_surface_absent"
            if population["proof_mode"] == "reels_surface_absent"
            else "instagram_reels_tab_exhausted_no_reels"
        )
        sample_count = 0
        samples = []
        status = "not_applicable_no_reels"
        population_basis = "no_reels"
    else:
        avg_decimal = None
        avg_views = None
        source = "missing"
        sample_count = 0
        samples = []
        status = "missing"
        population_basis = "unproven_missing"

    out = {
        "schema_version": 2,
        "status": status,
        "currency": "USD",
        "method": "mean_recent_non_pinned_reels_x_cpm",
        "requested_reels": window,
        "sample_count": sample_count,
        "average_plays": avg_views,
        "source": source,
        "population_basis": population_basis,
        "population_evidence": population,
        "pinned_excluded": pinned_excluded,
        "cpm_usd": {"default": default_cpm, "min": low_cpm, "max": high_cpm},
        "quote_usd": {"default": None, "min": None, "max": None},
        "captured_at": cand.get("pricing_captured_at") or cand.get("captured_at"),
        "reels": samples,
    }
    if avg_decimal is not None:
        divisor = Decimal("1000")
        out["quote_usd"] = {
            "default": _money(avg_decimal / divisor * Decimal(str(default_cpm))),
            "min": _money(avg_decimal / divisor * Decimal(str(low_cpm))),
            "max": _money(avg_decimal / divisor * Decimal(str(high_cpm))),
        }
    return out


def estimate_integrity_reasons(
    cand: dict,
    cfg: dict,
    *,
    allow_legacy_schema1: bool = False,
) -> list[str]:
    """Compare every customer-visible/calculation field with canonical derivation.

    Collector diagnostics such as ``collection_error`` may remain as extra keys;
    they are not rendered or used in quote arithmetic.  Every canonical field,
    including the exact delivered Reels rows and capture time, is fail-closed.
    """
    stored = cand.get("pricing_estimate")
    if not isinstance(stored, dict):
        return ["报价缺失(pricing_estimate 不存在)"]
    derived = derive_quote_estimate(cand, cfg)
    labels = {
        "schema_version": "schema_version",
        "status": "status",
        "currency": "currency",
        "method": "method",
        "requested_reels": "requested_reels",
        "sample_count": "sample_count",
        "average_plays": "average_plays",
        "source": "source",
        "population_basis": "population_basis",
        "population_evidence": "population_evidence",
        "pinned_excluded": "pinned_excluded",
        "cpm_usd": "cpm_usd",
        "quote_usd": "quote_usd",
        "captured_at": "captured_at",
        "reels": "reels",
    }
    fields = list(CANONICAL_ESTIMATE_FIELDS)
    # Existing rows were emitted by schema v1 before population summaries were
    # introduced. B3 allows this compatibility lane only for already-complete
    # 10/10 quotes. Internal monotonic selection may also recognize an internally
    # consistent legacy partial/missing bundle so a transient retry cannot erase
    # it; those statuses still fail delivery completeness. Every field v1 actually
    # delivered remains canonical in either lane.
    legacy_schema1 = (
        stored.get("status") == derived.get("status")
        and stored.get("schema_version") == 1
        and (
            stored.get("status") == "complete"
            or allow_legacy_schema1
        )
    )
    if legacy_schema1:
        fields.remove("schema_version")
        fields.remove("population_basis")
        fields.remove("population_evidence")
    return [
        f"报价字段与原始证据不一致({labels[field]})"
        for field in fields
        if stored.get(field) != derived.get(field)
    ]


def completion_reasons(cand: dict, cfg: dict) -> list[str]:
    """Return fail-closed pricing reasons for B3 and formal Stage 4.

    The stored estimate must agree with a fresh derivation from raw native samples
    and browser population evidence. This prevents hand-edited terminal statuses
    and legacy Modash fallbacks from passing the delivery barrier.
    """
    stored = cand.get("pricing_estimate")
    if not isinstance(stored, dict):
        return ["报价缺失(pricing_estimate 不存在)"]
    derived = derive_quote_estimate(cand, cfg)
    expected_status = derived["status"]
    reasons = estimate_integrity_reasons(cand, cfg)
    if expected_status not in DELIVERY_COMPLETE_STATUSES:
        reasons.append(f"报价总体证据未闭合(status={expected_status})")
    return list(dict.fromkeys(reasons))
