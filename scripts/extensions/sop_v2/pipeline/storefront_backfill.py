#!/usr/bin/env python3
"""Two-phase, fail-closed Storefront evidence backfill.

``collect`` opens the creator cache read-only, collects fresh public-profile
evidence behind the configured proxy while holding the Graph account/Profile
resource bundle, and writes a reviewable JSON plan.  ``apply`` accepts only a
fully resolved plan whose caller-supplied SHA-256 matches, then patches the
complete cohort in one ``BEGIN IMMEDIATE`` transaction with a per-row CAS.

The writer deliberately has a tiny allowlist.  Deep evidence, pricing,
translations, attempt ledgers, queue state, and every other candidate field
are protected by the raw ``stage_json`` CAS and a second protected-content
digest.
"""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import socket
import sqlite3
import sys
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import parse_qs, unquote, urlsplit, urlunsplit


sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from extensions.sop_v2 import storefront as storefront_policy  # noqa: E402
from extensions.sop_v2.pipeline import graph_runner  # noqa: E402
from extensions.sop_v2.pipeline import resource_leases  # noqa: E402


PLAN_SCHEMA = "sop-v2-storefront-backfill-plan-v1"
EVIDENCE_SCHEMA = "sop-v2-storefront-backfill-evidence-v1"
_HANDLE_RE = re.compile(r"[A-Za-z0-9._]{1,30}\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_PROXY_SYNTHETIC_DNS_NETWORK = ipaddress.ip_network("198.18.0.0/15")
_TRUSTED_PROXY_ENDPOINTS = frozenset(
    {("overseas.tunnel.qg.net", 11404)}
)
_LOCAL_HOST_SUFFIXES = (
    ".local",
    ".localdomain",
    ".internal",
    ".lan",
    ".home.arpa",
    ".localhost",
)
_NONCOMMERCE_SOCIAL_HOSTS = (
    "instagram.com",
    "tiktok.com",
    "youtube.com",
    "youtu.be",
    "facebook.com",
    "threads.net",
    "twitter.com",
    "x.com",
    "pinterest.com",
    "snapchat.com",
    "t.me",
    "telegram.me",
    "wa.me",
    "whatsapp.com",
)

# These are the only candidate JSON keys this tool may add, replace, or remove.
STOREFRONT_STAGE_FIELDS = frozenset(
    {
        "storefront_status",
        "storefront_url",
        "storefront_type",
        "amazon_storefront_link",
        "storefront_evidence",
        "bio_links",
        "external_url",
        "_bio_has_more",
        "_bio_more_count",
    }
)
STOREFRONT_DB_COLUMNS = frozenset(
    {"stage_json", "storefront_status", "amazon_storefront_link"}
)
_ROW_CAS_FIELDS = (
    "status",
    "client_status",
    "stage_error",
    "locked_at",
    "stage_updated_at",
    "discovery_batch",
)
_STOREFRONT_HOT_CAS_FIELDS = (
    "storefront_status",
    "amazon_storefront_link",
)
_CAS_FIELDS = (*_ROW_CAS_FIELDS, *_STOREFRONT_HOT_CAS_FIELDS)
_REQUIRED_COLUMNS = frozenset(
    {
        "handle",
        "stage_json",
        "storefront_status",
        "amazon_storefront_link",
        *_ROW_CAS_FIELDS,
    }
)


class StorefrontBackfillError(RuntimeError):
    """A safety precondition, collection invariant, or atomic CAS failed."""


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON number: {value}")


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise StorefrontBackfillError(f"value is not canonical JSON: {exc}") from exc


def _sha_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha_json(value: Any) -> str:
    return _sha_text(_canonical_json(value))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _normalize_handle(value: object) -> str:
    handle = str(value or "").strip().lstrip("@")
    if not _HANDLE_RE.fullmatch(handle):
        raise StorefrontBackfillError(f"invalid handle: {value!r}")
    return handle


def _load_json_object(raw: object, *, label: str) -> dict[str, Any]:
    if not isinstance(raw, str) or not raw.strip():
        raise StorefrontBackfillError(f"{label}: missing stage_json")
    try:
        value = json.loads(raw, parse_constant=_reject_json_constant)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise StorefrontBackfillError(f"{label}: invalid stage_json: {exc}") from exc
    if not isinstance(value, dict):
        raise StorefrontBackfillError(f"{label}: stage_json must be an object")
    return value


def _protected_stage(stage: Mapping[str, Any]) -> dict[str, Any]:
    """Everything outside the explicit Storefront allowlist is immutable."""

    return {key: value for key, value in stage.items() if key not in STOREFRONT_STAGE_FIELDS}


def _protected_sha(stage: Mapping[str, Any]) -> str:
    return _sha_json(_protected_stage(stage))


def _plan_sha(plan: Mapping[str, Any]) -> str:
    payload = dict(plan)
    payload.pop("plan_sha256", None)
    return _sha_json(payload)


def plan_sha256(plan_or_path: Mapping[str, Any] | str | os.PathLike[str]) -> str:
    """Return the canonical plan digest used by the mandatory apply guard."""

    if isinstance(plan_or_path, Mapping):
        return _plan_sha(plan_or_path)
    return _plan_sha(_read_plan(Path(plan_or_path)))


def _ro_connection(path: Path) -> sqlite3.Connection:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise StorefrontBackfillError(f"creator database does not exist: {resolved}")
    try:
        connection = sqlite3.connect(
            resolved.as_uri() + "?mode=ro", uri=True, timeout=30
        )
    except sqlite3.Error as exc:
        raise StorefrontBackfillError(
            f"cannot open creator database read-only: {resolved}"
        ) from exc
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA busy_timeout=30000")
    return connection


def _separate_lease_database(
    creator_db: Path, lease_db: str | os.PathLike[str]
) -> Path:
    creator = creator_db.expanduser().resolve()
    lease = Path(lease_db).expanduser().resolve(strict=False)
    aliases_creator = lease == creator
    if not aliases_creator and os.path.lexists(lease):
        try:
            aliases_creator = os.path.samefile(creator, lease)
        except OSError:
            aliases_creator = False
    if aliases_creator:
        raise StorefrontBackfillError(
            "lease database must be physically separate from creator database"
        )
    return lease


def _validate_database(connection: sqlite3.Connection) -> None:
    table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='creator_profiles'"
    ).fetchone()
    if not table:
        raise StorefrontBackfillError("creator_profiles table is missing")
    columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(creator_profiles)")
    }
    missing = sorted(_REQUIRED_COLUMNS - columns)
    if missing:
        raise StorefrontBackfillError(
            "creator_profiles columns missing: " + ", ".join(missing)
        )


def _effective_candidate(row: sqlite3.Row, stage: Mapping[str, Any]) -> dict[str, Any]:
    candidate = dict(stage)
    # Storefront provenance is one authority group.  Mixing a stage status with
    # a stale hot URL (or vice versa) can synthesize a verdict that neither
    # source actually asserted.  Fall back to hot columns only when the stage
    # group is wholly absent.
    stage_has_storefront_authority = any(
        key in stage
        for key in (
            "storefront_status",
            "storefront_url",
            "storefront_type",
            "amazon_storefront_link",
        )
    )
    if not stage_has_storefront_authority:
        if row["storefront_status"] is not None:
            candidate["storefront_status"] = row["storefront_status"]
        if row["amazon_storefront_link"] is not None:
            candidate["amazon_storefront_link"] = row["amazon_storefront_link"]
    return candidate


def _snapshot_row(row: sqlite3.Row) -> dict[str, Any]:
    handle = _normalize_handle(row["handle"])
    raw_stage = row["stage_json"]
    stage = _load_json_object(raw_stage, label=f"@{handle}")
    stage_handle = _normalize_handle(stage.get("handle"))
    if stage_handle.casefold() != handle.casefold():
        raise StorefrontBackfillError(
            f"@{handle}: stage_json handle disagrees with database handle"
        )
    return {
        "handle": handle,
        "raw_stage_json": raw_stage,
        "stage": stage,
        "stage_json_sha256": _sha_text(raw_stage),
        "protected_stage_sha256": _protected_sha(stage),
        "cas": {field: row[field] for field in _CAS_FIELDS},
        "effective_status": storefront_policy.effective_status(
            _effective_candidate(row, stage)
        ),
    }


def _load_unknown_cohort(
    connection: sqlite3.Connection,
    *,
    batch_id: str,
    expected_status: str,
) -> tuple[dict[str, Any], ...]:
    rows = connection.execute(
        """
        SELECT handle,stage_json,storefront_status,amazon_storefront_link,
               status,client_status,stage_error,locked_at,stage_updated_at,
               discovery_batch
          FROM creator_profiles
         WHERE discovery_batch=? AND status=?
         ORDER BY handle COLLATE NOCASE, handle
        """,
        (batch_id, expected_status),
    ).fetchall()
    snapshots = tuple(_snapshot_row(row) for row in rows)
    folded = [item["handle"].casefold() for item in snapshots]
    if len(folded) != len(set(folded)):
        raise StorefrontBackfillError("case-insensitive duplicate handles in cohort")
    return tuple(item for item in snapshots if item["effective_status"] == "unknown")


def inspect_unknown_cohort(
    db_path: str | os.PathLike[str], *, batch_id: str, expected_status: str = "decided"
) -> tuple[dict[str, Any], ...]:
    """Read the exact unresolved cohort without creating/migrating the database."""

    path = Path(db_path)
    with _ro_connection(path) as connection:
        _validate_database(connection)
        return _load_unknown_cohort(
            connection, batch_id=batch_id, expected_status=expected_status
        )


def handle_set_sha256(handles: Iterable[str]) -> str:
    """Hash one normalized, order-independent, duplicate-free handle cohort."""

    normalized = [_normalize_handle(item).casefold() for item in handles]
    if not normalized:
        raise StorefrontBackfillError("handle cohort must not be empty")
    if len(normalized) != len(set(normalized)):
        raise StorefrontBackfillError("handle cohort contains duplicates")
    return _sha_json(sorted(normalized))


def _write_plan(path: Path, plan: Mapping[str, Any]) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(
        plan, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False
    ) + "\n"
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    published = False
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            # A hard-link publication is atomic and, unlike os.replace(), can
            # never overwrite a reviewed formal artifact.  The temporary link
            # is removed after the destination name is durable.
            os.link(temporary, path)
            published = True
        except FileExistsError as exc:
            raise StorefrontBackfillError(f"plan already exists: {path}") from exc
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        os.unlink(temporary)
    except Exception:
        if published:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _read_plan(path: Path) -> dict[str, Any]:
    try:
        raw = path.expanduser().resolve().read_text(encoding="utf-8")
        value = json.loads(raw, parse_constant=_reject_json_constant)
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise StorefrontBackfillError(f"cannot read plan: {exc}") from exc
    if not isinstance(value, dict):
        raise StorefrontBackfillError("plan must be a JSON object")
    return value


def _public_proxy_check(proxy: object) -> None:
    if not isinstance(proxy, Mapping):
        raise StorefrontBackfillError("proxy is required but unavailable")
    unknown_keys = set(proxy) - {"server", "username", "password"}
    if unknown_keys:
        raise StorefrontBackfillError("proxy contains unsupported or bypass options")
    server = str(proxy.get("server") or "").strip()
    try:
        parsed = urlsplit(server)
        port = parsed.port
    except ValueError as exc:
        raise StorefrontBackfillError("proxy server is invalid") from exc
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        raise StorefrontBackfillError("proxy server is invalid")
    if parsed.username is not None or parsed.password is not None:
        raise StorefrontBackfillError(
            "proxy credentials must not be embedded in the server URL"
        )
    if parsed.path or parsed.query or parsed.fragment:
        raise StorefrontBackfillError("proxy server must be an origin without URL extras")
    host = parsed.hostname.rstrip(".").casefold()
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise StorefrontBackfillError("proxy server must use the trusted domain")
    if (host, port) not in _TRUSTED_PROXY_ENDPOINTS:
        raise StorefrontBackfillError("proxy server endpoint is not trusted")
    username = proxy.get("username")
    password = proxy.get("password")
    if (
        not isinstance(username, str)
        or not username.strip()
        or not isinstance(password, str)
        or not password.strip()
    ):
        raise StorefrontBackfillError("authenticated proxy credentials are required")


def _forbidden_domain_hostname(host: str) -> bool:
    """Reject local namespaces and browser-ambiguous numeric host spellings."""

    folded = str(host or "").rstrip(".").casefold()
    if not folded or "." not in folded:
        return True
    if folded in {"localhost", "localhost.localdomain"} or folded.endswith(
        _LOCAL_HOST_SUFFIXES
    ):
        return True
    return bool(
        re.fullmatch(
            r"(?:0x[0-9a-f]+|[0-9]+)(?:\.(?:0x[0-9a-f]+|[0-9]+))*",
            folded,
            re.I,
        )
    )


def _is_public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Use a stricter public predicate than ``is_global`` (which includes multicast)."""

    return bool(
        ip.is_global
        and not ip.is_private
        and not ip.is_loopback
        and not ip.is_link_local
        and not ip.is_multicast
        and not ip.is_reserved
        and not ip.is_unspecified
    )


def _default_resolver(host: str) -> tuple[str, ...]:
    try:
        records = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise StorefrontBackfillError("external hostname did not resolve") from exc
    addresses = tuple(sorted({record[4][0] for record in records}))
    if not addresses:
        raise StorefrontBackfillError("external hostname did not resolve")
    return addresses


def _unwrap_instagram_redirect(url: str) -> str:
    current = str(url or "").strip()
    for _ in range(3):
        try:
            parsed = urlsplit(current)
        except ValueError:
            return current
        host = (parsed.hostname or "").casefold().removeprefix("www.")
        if host != "l.instagram.com":
            return current
        values = parse_qs(parsed.query).get("u") or []
        if not values or not values[0]:
            return current
        current = unquote(values[0]).strip()
    return current


def safe_external_url(
    value: object,
    *,
    resolver: Callable[[str], Iterable[str]] | None = None,
    _allow_proxy_synthetic_dns: bool = False,
) -> str:
    """Validate an external navigation target against SSRF/credential hazards."""

    raw = _unwrap_instagram_redirect(str(value or "").strip())
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise StorefrontBackfillError("unsafe external URL") from exc
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        raise StorefrontBackfillError("unsafe external URL scheme or hostname")
    if parsed.username is not None or parsed.password is not None:
        raise StorefrontBackfillError("external URL must not contain credentials")
    if port not in {None, 80, 443}:
        raise StorefrontBackfillError("external URL uses a non-web port")
    host = parsed.hostname.rstrip(".").casefold()
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        raise StorefrontBackfillError("external URL resolves locally")
    try:
        literal_host = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        literal_host = None
    if literal_host is not None and not _is_public_ip(literal_host):
        raise StorefrontBackfillError("external URL resolves to a non-public address")
    if literal_host is None and _forbidden_domain_hostname(host):
        raise StorefrontBackfillError("external URL uses a local or ambiguous hostname")
    resolve = resolver or _default_resolver
    addresses = tuple(resolve(host))
    if not addresses:
        raise StorefrontBackfillError("external hostname did not resolve")
    for address in addresses:
        try:
            ip = ipaddress.ip_address(str(address).split("%")[0])
        except ValueError as exc:
            raise StorefrontBackfillError("resolver returned an invalid address") from exc
        # Some system-wide TUN configurations intentionally return RFC 2544's
        # benchmarking range as a synthetic DNS answer and recover the real
        # destination inside the mandatory browser proxy.  The exception is
        # deliberately private, opt-in, IPv4-only, and valid only for a domain
        # resolution result: an input URL containing a 198.18/15 IP literal is
        # still rejected.  Every other non-global range remains fail-closed.
        proxy_synthetic_dns = bool(
            _allow_proxy_synthetic_dns
            and literal_host is None
            and isinstance(ip, ipaddress.IPv4Address)
            and ip in _PROXY_SYNTHETIC_DNS_NETWORK
        )
        if not _is_public_ip(ip) and not proxy_synthetic_dns:
            raise StorefrontBackfillError("external URL resolves to a non-public address")
    netloc = host
    if ":" in host:
        netloc = f"[{host}]"
    if port is not None:
        netloc += f":{port}"
    return urlunsplit(
        (parsed.scheme.casefold(), netloc, parsed.path or "/", parsed.query, "")
    )


def _is_noncommerce_social_url(url: str) -> bool:
    try:
        host = (urlsplit(url).hostname or "").casefold().removeprefix("www.")
    except ValueError:
        return False
    return any(
        host == suffix or host.endswith("." + suffix)
        for suffix in _NONCOMMERCE_SOCIAL_HOSTS
    )


def _dedupe_urls(values: Iterable[object]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        url = str(value or "").strip()
        if not url:
            continue
        folded = url.casefold()
        if folded in seen:
            continue
        seen.add(folded)
        output.append(url)
    return output


def resolve_observation(
    *,
    handle: str,
    profile: Mapping[str, Any] | None,
    profile_url: str,
    expanded_links: Sequence[str] = (),
    expansion_succeeded: bool = True,
    observed_links: Sequence[str] | None = None,
    raw_observed_link_count: int | None = None,
    terminal_observed_link_count: int | None = None,
    target_checks: Sequence[Mapping[str, Any]] = (),
    captured_at: str | None = None,
) -> dict[str, Any]:
    """Turn fresh profile/target observations into the strict three-state result."""

    timestamp = captured_at or _utc_now()
    profile_data = dict(profile or {})
    identity_verified = bool(profile_data.pop("_identity_verified", False))
    profile_healthy = bool(profile_data.pop("_profile_healthy", False))
    identity_check = profile_data.pop("_profile_identity_check", None)
    health_check = profile_data.pop("_profile_health_check", None)
    declaration_check = profile_data.pop("_bio_declaration_check", None)
    raw_more_count = profile_data.get("_bio_more_count")
    more_count = (
        int(raw_more_count)
        if isinstance(raw_more_count, int)
        and not isinstance(raw_more_count, bool)
        and raw_more_count >= 1
        else None
    )
    has_more = bool(profile_data.get("_bio_has_more") or more_count is not None)
    bio_links = _dedupe_urls(
        observed_links
        if observed_links is not None
        else [profile_data.get("external_url"), *expanded_links]
    )
    minimum_link_count = (more_count + 1) if more_count is not None else None
    raw_total = (
        raw_observed_link_count
        if isinstance(raw_observed_link_count, int)
        and not isinstance(raw_observed_link_count, bool)
        and raw_observed_link_count >= 0
        else len(_dedupe_urls([profile_data.get("external_url"), *expanded_links]))
    )
    terminal_total = (
        terminal_observed_link_count
        if isinstance(terminal_observed_link_count, int)
        and not isinstance(terminal_observed_link_count, bool)
        and terminal_observed_link_count >= 0
        else len(bio_links)
    )
    if not has_more:
        bio_links_complete = True
    else:
        bio_links_complete = bool(
            expansion_succeeded
            and minimum_link_count is not None
            and raw_total >= minimum_link_count
            and terminal_total >= minimum_link_count
        )
    checks = [dict(item) for item in target_checks]
    targets_complete = len(checks) == len(bio_links) and all(
        item.get("status") == "succeeded" for item in checks
    )
    profile_failures: list[str] = []
    if not identity_verified:
        profile_failures.append("profile_identity_unverified")
    if not profile_healthy:
        profile_failures.append("profile_unhealthy")
    coverage_failures: list[str] = []
    if not bio_links_complete:
        coverage_failures.append("bio_link_expansion_incomplete")
    if not targets_complete:
        coverage_failures.append("external_target_incomplete")

    recognized: list[dict[str, str]] = []
    for check in checks:
        if check.get("status") != "succeeded":
            continue
        for item in check.get("commerce_links") or []:
            if not isinstance(item, Mapping):
                continue
            url = str(item.get("url") or "").strip()
            kind = str(item.get("type") or "").strip()
            if url and kind in storefront_policy.STOREFRONT_TYPES:
                recognized.append({"url": url, "type": kind})

    ranked = {"Amazon": 0, "LTK": 1, "ShopMy": 2, "自营店": 3, "链接聚合": 4}
    recognized.sort(key=lambda item: ranked.get(item["type"], 99))
    status = "unknown"
    blocking_failures = list(profile_failures)
    partial_failures: list[str] = []
    note = (
        blocking_failures[0]
        if blocking_failures
        else "all_targets_succeeded_no_storefront"
    )
    storefront_url = None
    storefront_type = None
    amazon_link = None
    if not profile_failures and recognized:
        status = "confirmed_yes"
        note = "recognized_storefront"
        partial_failures = coverage_failures
        storefront_url = recognized[0]["url"]
        storefront_type = recognized[0]["type"]
        if storefront_type == "Amazon":
            amazon_link = storefront_url
    elif not profile_failures:
        blocking_failures.extend(coverage_failures)
        if not blocking_failures:
            status = "confirmed_no"
            note = "fresh_profile_no_bio_links" if not bio_links else note
        else:
            note = blocking_failures[0]

    evidence = {
        "schema": EVIDENCE_SCHEMA,
        "captured_at": timestamp,
        "profile_url": profile_url,
        "identity_verified": identity_verified,
        "profile_healthy": profile_healthy,
        "profile_identity_check": identity_check,
        "profile_health_check": health_check,
        "bio_links_complete": bio_links_complete,
        "bio_link_expansion": {
            "required": has_more,
            "declared_more_count": more_count,
            "declared_more": more_count,
            "minimum_total_link_count": minimum_link_count,
            "declared_total": minimum_link_count,
            "raw_observed_total": raw_total,
            "terminal_observed_total": terminal_total,
            "observed_total_link_count": len(bio_links),
            "expanded_link_count": len(_dedupe_urls(expanded_links)),
            "complete": bio_links_complete,
            "terminal_complete": bio_links_complete,
            "declaration_evidence": declaration_check,
        },
        "target_checks_complete": targets_complete,
        "target_checks": checks,
        "failures": blocking_failures,
        "partial_failures": partial_failures,
        "note": note,
    }
    return {
        "status": status,
        "storefront_url": storefront_url,
        "storefront_type": storefront_type,
        "amazon_storefront_link": amazon_link,
        "external_url": profile_data.get("external_url"),
        "bio_links": bio_links,
        "_bio_has_more": has_more,
        "_bio_more_count": more_count,
        "evidence": evidence,
    }


def _recompute_identity_verdict(
    identity_check: object,
    *,
    handle: str,
    claimed_verified: bool,
    profile_url: object,
) -> bool:
    if not isinstance(identity_check, Mapping):
        if claimed_verified:
            raise StorefrontBackfillError(
                f"@{handle}: verified identity lacks structured evidence"
            )
        return False
    expected = handle.casefold()
    sources_value = identity_check.get("sources")
    sources = dict(sources_value) if isinstance(sources_value, Mapping) else {}

    def normalized_source(key: str) -> str | None:
        value = str(sources.get(key) or "").strip().lstrip("@").casefold()
        return value or None

    og_handle = normalized_source("og_handle")
    canonical_handle = normalized_source("canonical_handle")
    surface_handle = normalized_source("surface_handle")
    provenance = str(identity_check.get("surface_handle_source") or "").strip() or None
    rejected = identity_check.get("rejected_surface_handles")
    rejected_values = [
        str(value).strip().lstrip("@").casefold()
        for value in (rejected if isinstance(rejected, list) else [])
        if str(value).strip()
    ]
    og_exact = og_handle == expected
    trusted_surface_match = bool(
        surface_handle == expected
        and provenance in {"heading_text", "self_link_path"}
    )
    strong_contradiction = any(
        value is not None and value != expected
        for value in (og_handle, canonical_handle)
    )
    independent_match = bool(og_exact or trusted_surface_match)
    weak_surface_nonmatch = bool(
        surface_handle not in {None, expected} or rejected_values
    )
    try:
        profile_parts = [
            unquote(part)
            for part in urlsplit(str(profile_url or "")).path.split("/")
            if part
        ]
    except ValueError:
        profile_parts = []
    page_url_match = bool(
        profile_parts and profile_parts[0].casefold() == expected
    )
    supplied_handle_match = (
        str(identity_check.get("supplied_handle") or "")
        .strip()
        .lstrip("@")
        .casefold()
        == expected
    )
    expected_handle_match = (
        str(identity_check.get("expected_handle") or "")
        .strip()
        .lstrip("@")
        .casefold()
        == expected
    )
    verified = bool(
        expected_handle_match
        and page_url_match
        and supplied_handle_match
        and independent_match
        and not strong_contradiction
    )
    expected_fields = {
        "og_exact": og_exact,
        "trusted_surface_match": trusted_surface_match,
        "strong_contradiction": strong_contradiction,
        "contradictory_surface": strong_contradiction,
        "independent_match": independent_match,
        "weak_surface_nonmatch": weak_surface_nonmatch,
        "page_url_match": page_url_match,
        "supplied_handle_match": supplied_handle_match,
    }
    if claimed_verified or identity_check.get("reason") == "verified":
        inconsistent = [
            key
            for key, expected_value in expected_fields.items()
            if identity_check.get(key) is not expected_value
        ]
        if inconsistent:
            raise StorefrontBackfillError(
                f"@{handle}: identity evidence summary disagrees with sources: "
                + ", ".join(inconsistent)
            )
        expected_reason = "verified" if verified else "identity_not_independently_verified"
        if identity_check.get("reason") != expected_reason:
            raise StorefrontBackfillError(
                f"@{handle}: identity evidence reason disagrees with sources"
            )
        if bool(claimed_verified) != verified:
            raise StorefrontBackfillError(
                f"@{handle}: identity verdict disagrees with sources"
            )
    return verified


def _validate_probe_result(result: object, *, handle: str) -> dict[str, Any]:
    if not isinstance(result, Mapping):
        raise StorefrontBackfillError(f"@{handle}: probe result must be an object")
    normalized = dict(result)
    status = normalized.get("status")
    if status not in {"confirmed_yes", "confirmed_no", "unknown"}:
        raise StorefrontBackfillError(f"@{handle}: invalid probe status")
    evidence = normalized.get("evidence")
    if not isinstance(evidence, Mapping):
        raise StorefrontBackfillError(f"@{handle}: missing structured evidence")
    if evidence.get("schema") != EVIDENCE_SCHEMA:
        raise StorefrontBackfillError(f"@{handle}: invalid evidence schema")
    for boolean_field in (
        "identity_verified",
        "profile_healthy",
        "bio_links_complete",
        "target_checks_complete",
    ):
        if not isinstance(evidence.get(boolean_field), bool):
            raise StorefrontBackfillError(
                f"@{handle}: {boolean_field} must be a boolean"
            )
    failures = list(evidence.get("failures") or [])
    checks = list(evidence.get("target_checks") or [])
    if any(not isinstance(item, Mapping) for item in checks):
        raise StorefrontBackfillError(f"@{handle}: invalid target_checks")
    for check in checks:
        if check.get("status") != "succeeded":
            continue
        page_health = check.get("page_health")
        if not isinstance(page_health, Mapping) or page_health.get("healthy") is not True:
            raise StorefrontBackfillError(
                f"@{handle}: succeeded target lacks healthy page evidence"
            )
        source_url = str(check.get("source_url") or "")
        navigation_required = not _is_noncommerce_social_url(
            source_url
        ) and storefront_policy.classify_url(source_url) is None
        if navigation_required:
            http_status = check.get("http_status")
            if isinstance(http_status, bool) or not isinstance(http_status, int) or not (
                200 <= http_status < 400
            ):
                raise StorefrontBackfillError(
                    f"@{handle}: ordinary target lacks successful HTTP evidence"
                )
    all_checks_succeeded = all(
        isinstance(item, Mapping) and item.get("status") == "succeeded"
        for item in checks
    )
    bio_links = _dedupe_urls(normalized.get("bio_links") or [])
    source_urls = [str(item.get("source_url") or "").strip() for item in checks]
    sources_match = source_urls == bio_links
    complete_checks = len(checks) == len(bio_links) and sources_match
    identity_check = evidence.get("profile_identity_check")
    health_check = evidence.get("profile_health_check")
    structured_identity = _recompute_identity_verdict(
        identity_check,
        handle=handle,
        claimed_verified=evidence["identity_verified"],
        profile_url=evidence.get("profile_url"),
    )
    structured_health = bool(
        isinstance(health_check, Mapping)
        and health_check.get("reason") == "healthy"
        and isinstance(health_check.get("signals"), list)
        and bool(health_check.get("signals"))
        and isinstance(health_check.get("body_text_length"), int)
        and not isinstance(health_check.get("body_text_length"), bool)
        and health_check.get("body_text_length") >= 20
        and isinstance(health_check.get("captured_body_text_length"), int)
        and not isinstance(health_check.get("captured_body_text_length"), bool)
        and health_check.get("captured_body_text_length") >= 20
    )
    profile_complete = (
        bool(evidence.get("identity_verified"))
        and bool(evidence.get("profile_healthy"))
        and structured_identity
        and structured_health
    )
    coverage_complete = (
        bool(evidence.get("bio_links_complete"))
        and bool(evidence.get("target_checks_complete"))
        and all_checks_succeeded
        and complete_checks
    )
    if not isinstance(normalized.get("_bio_has_more"), bool):
        raise StorefrontBackfillError(f"@{handle}: _bio_has_more must be a boolean")
    more_count = normalized.get("_bio_more_count")
    if more_count is not None and (
        isinstance(more_count, bool)
        or not isinstance(more_count, int)
        or more_count < 1
    ):
        raise StorefrontBackfillError(f"@{handle}: invalid bio more count")
    has_more = normalized["_bio_has_more"]
    expansion = evidence.get("bio_link_expansion")
    if not isinstance(expansion, Mapping):
        raise StorefrontBackfillError(f"@{handle}: missing bio expansion evidence")
    minimum_total = more_count + 1 if more_count is not None else None
    raw_observed_total = expansion.get("raw_observed_total")
    if isinstance(raw_observed_total, bool) or not isinstance(
        raw_observed_total, int
    ) or raw_observed_total < 0:
        raise StorefrontBackfillError(f"@{handle}: invalid raw bio-link total")
    terminal_observed_total = expansion.get("terminal_observed_total")
    if isinstance(terminal_observed_total, bool) or not isinstance(
        terminal_observed_total, int
    ) or terminal_observed_total < 0:
        raise StorefrontBackfillError(f"@{handle}: invalid terminal bio-link total")
    expansion_consistent = (
        bool(expansion.get("required")) == has_more
        and expansion.get("declared_more_count") == more_count
        and expansion.get("declared_more") == more_count
        and expansion.get("minimum_total_link_count") == minimum_total
        and expansion.get("declared_total") == minimum_total
        and expansion.get("observed_total_link_count") == len(bio_links)
        and terminal_observed_total == len(bio_links)
        and bool(expansion.get("complete"))
        == bool(evidence.get("bio_links_complete"))
        and bool(expansion.get("terminal_complete"))
        == bool(evidence.get("bio_links_complete"))
    )
    if bool(evidence.get("bio_links_complete")) and has_more:
        expansion_consistent = bool(
            expansion_consistent
            and minimum_total is not None
            and raw_observed_total >= minimum_total
            and terminal_observed_total >= minimum_total
        )
    if not expansion_consistent:
        raise StorefrontBackfillError(f"@{handle}: inconsistent bio expansion evidence")
    if normalized["status"] == "confirmed_yes":
        should_demote = bool(failures) or not profile_complete or not complete_checks
    elif normalized["status"] == "confirmed_no":
        should_demote = bool(failures) or not profile_complete or not coverage_complete
    else:
        should_demote = True
    if should_demote:
        normalized["status"] = "unknown"
        normalized["storefront_url"] = None
        normalized["storefront_type"] = None
        normalized["amazon_storefront_link"] = None
    if normalized["status"] == "confirmed_yes":
        url = str(normalized.get("storefront_url") or "").strip()
        kind = str(normalized.get("storefront_type") or "").strip()
        if not url or kind not in storefront_policy.STOREFRONT_TYPES:
            raise StorefrontBackfillError(f"@{handle}: incomplete confirmed_yes result")
        if storefront_policy.classify_url(url) != kind:
            raise StorefrontBackfillError(f"@{handle}: storefront URL/type disagree")
        commerce_pairs = {
            (str(entry.get("url") or "").strip(), str(entry.get("type") or "").strip())
            for check in checks
            if check.get("status") == "succeeded"
            for entry in (check.get("commerce_links") or [])
            if isinstance(entry, Mapping)
        }
        if (url, kind) not in commerce_pairs:
            raise StorefrontBackfillError(
                f"@{handle}: confirmed storefront is absent from target evidence"
            )
    elif normalized["status"] == "confirmed_no":
        if normalized.get("storefront_url") or normalized.get("storefront_type"):
            raise StorefrontBackfillError(f"@{handle}: confirmed_no contains storefront")
        if any(check.get("commerce_links") for check in checks):
            raise StorefrontBackfillError(
                f"@{handle}: confirmed_no target evidence contains commerce links"
            )
        for url in bio_links:
            if _is_noncommerce_social_url(str(url)):
                continue
            if storefront_policy.classify_url(str(url)):
                raise StorefrontBackfillError(
                    f"@{handle}: confirmed_no contains recognized storefront URL"
                )
    normalized["bio_links"] = bio_links
    normalized["_bio_has_more"] = has_more
    normalized["_bio_more_count"] = more_count
    return normalized


class _LeaseHeartbeat:
    """Own one atomic resource bundle and continuously renew it."""

    def __init__(
        self,
        lease_api: Any,
        lease_db: Path,
        bundle: Any,
        *,
        ttl_seconds: float,
        interval_seconds: float,
    ) -> None:
        self._api = lease_api
        self._db = lease_db
        self._bundle = bundle
        self._ttl = ttl_seconds
        self._interval = interval_seconds
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._failure: BaseException | None = None
        self._thread = threading.Thread(
            target=self._run, name="storefront-lease-heartbeat", daemon=True
        )

    def start(self) -> None:
        # Renew once synchronously so even a short collection proves ownership.
        self._heartbeat()
        self._thread.start()

    def _heartbeat(self) -> None:
        with self._lock:
            self._bundle = self._api.heartbeat_resources(
                self._db, self._bundle, ttl_seconds=self._ttl
            )

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self._heartbeat()
            except BaseException as exc:  # noqa: BLE001
                self._failure = exc
                self._stop.set()
                return

    def check(self) -> None:
        if self._failure is not None:
            raise StorefrontBackfillError("resource lease heartbeat lost") from self._failure

    def close(self, *, reason: str) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=max(1.0, self._interval * 2))
        if self._thread.is_alive():
            raise StorefrontBackfillError("resource lease heartbeat did not stop")
        with self._lock:
            self._api.release_resources(self._db, self._bundle, reason=reason)


_PAGE_BLOCKERS = (
    "sorry, this page isn't available",
    "page isn't available",
    "challenge_required",
    "please wait a few minutes",
    "log in to continue",
    "verify you are human",
    "verify you're human",
    "confirm you're human",
    "just a moment",
    "checking your browser",
    "access denied",
    "captcha",
    "connectez-vous pour continuer",
    "page n’est pas disponible",
    "page n'est pas disponible",
    "accès refusé",
    "inicia sesión para continuar",
    "página no está disponible",
    "acceso denegado",
    "faça login para continuar",
    "página não está disponível",
    "acesso negado",
    "войдите, чтобы продолжить",
    "страница недоступна",
    "доступ запрещен",
    "로그인하여 계속",
    "페이지를 사용할 수 없습니다",
    "접근이 거부",
    "登录以继续",
    "此页面无法使用",
    "拒绝访问",
    "domain for sale",
    "buy this domain",
    "this domain is parked",
    "sedo domain parking",
    "404 not found",
    "502 bad gateway",
    "503 service unavailable",
)


def _profile_identity(
    page: Any, handle: str, profile: Mapping[str, Any] | None
) -> tuple[bool, dict[str, Any]]:
    expected = handle.casefold()
    if not isinstance(profile, Mapping):
        return False, {"reason": "profile_missing", "independent_match": False}
    try:
        path_parts = [
            unquote(item)
            for item in urlsplit(str(page.url)).path.split("/")
            if item
        ]
    except ValueError:
        path_parts = []
    page_url_match = bool(path_parts and path_parts[0].casefold() == expected)
    supplied_handle_match = str(profile.get("handle") or "").casefold() == expected
    raw_sources = profile.get("_profile_identity_evidence")
    sources = dict(raw_sources) if isinstance(raw_sources, Mapping) else {}
    normalized_sources = {
        key: str(sources.get(key) or "").strip().lstrip("@").casefold() or None
        for key in ("og_handle", "canonical_handle", "surface_handle")
    }
    surface_provenance = str(sources.get("surface_handle_source") or "").strip() or None
    # OG and a path-bound DOM heading/self-link can independently prove the
    # target identity.  Canonical metadata is a strong cross-check but is not,
    # by itself, enough.  Weak DOM tokens such as Posts/Reels/Follow are merely
    # rejected candidates and must never overrule correct strong evidence.
    og_exact = normalized_sources["og_handle"] == expected
    trusted_surface_match = bool(
        normalized_sources["surface_handle"] == expected
        and surface_provenance in {"heading_text", "self_link_path"}
    )
    independent_match = bool(og_exact or trusted_surface_match)
    strong_values = [
        normalized_sources["og_handle"],
        normalized_sources["canonical_handle"],
    ]
    strong_contradiction = any(
        value is not None and value != expected for value in strong_values
    )
    rejected_values = sources.get("rejected_surface_handles")
    rejected_surface_handles = [
        str(value).strip().lstrip("@").casefold()
        for value in (rejected_values if isinstance(rejected_values, list) else [])
        if str(value).strip()
    ]
    weak_surface_nonmatch = bool(
        normalized_sources["surface_handle"] not in {None, expected}
        or rejected_surface_handles
    )
    verified = bool(
        supplied_handle_match
        and page_url_match
        and independent_match
        and not strong_contradiction
    )
    return verified, {
        "expected_handle": handle,
        "supplied_handle": str(profile.get("handle") or ""),
        "page_url_match": page_url_match,
        "supplied_handle_match": supplied_handle_match,
        "independent_match": independent_match,
        "og_exact": og_exact,
        "trusted_surface_match": trusted_surface_match,
        "strong_contradiction": strong_contradiction,
        "contradictory_surface": strong_contradiction,
        "weak_surface_nonmatch": weak_surface_nonmatch,
        "rejected_surface_handles": rejected_surface_handles,
        "surface_handle_source": surface_provenance,
        "sources": normalized_sources,
        "reason": "verified" if verified else "identity_not_independently_verified",
    }


def _profile_health(
    page: Any, profile: Mapping[str, Any] | None
) -> tuple[bool, dict[str, Any]]:
    if not isinstance(profile, Mapping) or profile.get("_wall"):
        return False, {"reason": "profile_missing_or_wall", "signals": []}
    try:
        text = page.locator("body").inner_text(timeout=3000)
    except Exception:  # noqa: BLE001
        return False, {"reason": "profile_body_unreadable", "signals": []}
    normalized_text = " ".join(str(text or "").split())
    lowered = normalized_text.casefold()
    blocker = next((item for item in _PAGE_BLOCKERS if item in lowered), None)
    signals: list[str] = []
    if profile.get("follower_count") is not None:
        signals.append("follower_count")
    if profile.get("codes") or profile.get("post_refs"):
        signals.append("post_references")
    if profile.get("is_private"):
        signals.append("private_profile_surface")
    if str(profile.get("full_name") or "").strip():
        signals.append("full_name")
    if str(profile.get("biography") or "").strip():
        signals.append("biography")
    if str(profile.get("external_url") or "").strip():
        signals.append("external_url")
    surface = profile.get("_profile_surface_evidence")
    captured_body_length = (
        int(surface.get("body_text_length") or 0)
        if isinstance(surface, Mapping)
        else 0
    )
    healthy = bool(
        len(normalized_text) >= 20
        and captured_body_length >= 20
        and blocker is None
        and signals
    )
    return healthy, {
        "reason": (
            "healthy"
            if healthy
            else "blocked_surface"
            if blocker
            else "empty_or_unsubstantiated_profile"
        ),
        "body_text_length": len(normalized_text),
        "captured_body_text_length": captured_body_length,
        "blocker": blocker,
        "signals": signals,
    }


def _ordinary_dom_sample(page: Any) -> dict[str, Any]:
    value = page.evaluate(
        r"""() => {
          const body=document.body;
          const text=(body?.innerText||'').replace(/\s+/g,' ').trim();
          return {
            ready_state: document.readyState,
            title: document.title||'',
            body_text: text,
            body_html_length: (body?.innerHTML||'').length,
            element_count: body ? body.querySelectorAll('*').length : 0,
            anchor_count: body ? body.querySelectorAll('a[href]').length : 0
          };
        }"""
    )
    if not isinstance(value, Mapping):
        raise StorefrontBackfillError("external DOM sample is invalid")
    return dict(value)


def _ordinary_page_health(samples: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    if len(samples) != 2:
        return {"healthy": False, "reason": "dom_sample_incomplete"}
    texts = [" ".join(str(item.get("body_text") or "").split()) for item in samples]
    lowered = " ".join(texts).casefold()
    blocker = next((item for item in _PAGE_BLOCKERS if item in lowered), None)
    ready = [str(item.get("ready_state") or "") for item in samples]
    text_lengths = [len(text) for text in texts]
    html_lengths = [int(item.get("body_html_length") or 0) for item in samples]
    element_counts = [int(item.get("element_count") or 0) for item in samples]
    anchor_counts = [int(item.get("anchor_count") or 0) for item in samples]
    substantive = all(
        text_length >= 40 and html_length >= 100 and element_count >= 3
        for text_length, html_length, element_count in zip(
            text_lengths, html_lengths, element_counts
        )
    )
    text_delta = abs(text_lengths[1] - text_lengths[0])
    element_delta = abs(element_counts[1] - element_counts[0])
    stable = bool(
        all(state in {"interactive", "complete"} for state in ready)
        and text_delta <= max(200, max(text_lengths) // 2)
        and element_delta <= max(50, max(element_counts) // 2)
    )
    healthy = bool(blocker is None and substantive and stable)
    reason = (
        "healthy"
        if healthy
        else "challenge_login_error_or_parked"
        if blocker
        else "dom_not_substantive"
        if not substantive
        else "dom_not_stable"
    )
    return {
        "healthy": healthy,
        "reason": reason,
        "blocker": blocker,
        "sample_count": len(samples),
        "ready_states": ready,
        "body_text_lengths": text_lengths,
        "body_html_lengths": html_lengths,
        "element_counts": element_counts,
        "anchor_counts": anchor_counts,
        "stable": stable,
        "substantive": substantive,
    }


def _bio_more_declaration(
    page: Any,
    profile: Mapping[str, Any] | None,
    *,
    parse_count: Callable[[object], int | None],
) -> tuple[bool, int | None, dict[str, Any]]:
    labels: list[str] = []
    interactive_labels: set[str] = set()
    if isinstance(profile, Mapping) and profile.get("_bio_link_label"):
        labels.append(str(profile["_bio_link_label"]))
    try:
        dom_labels = page.evaluate(
            r"""() => [...document.querySelectorAll('main header button,main header div,main header span,main section button,main section div,main section span')]
              .map(e=>{
                const control=e.closest('button,[role="button"],[aria-haspopup="dialog"],[aria-expanded]');
                const popup=(control?.getAttribute('aria-haspopup')||e.getAttribute('aria-haspopup')||'').toLowerCase();
                const expanded=(control?.getAttribute('aria-expanded')||e.getAttribute('aria-expanded')||'').toLowerCase();
                return {
                  text:(e.innerText||e.textContent||'').replace(/\s+/g,' ').trim(),
                  popup,
                  expanded,
                  interactive:!!control
                };
              })
              .filter(x=>x.text && x.text.length<240 && (
                (/\d/.test(x.text) && /(?:more|m[aá]s|autres?|mais|ещ[её]|더|还有|另有|另外|链接|links?|enlaces?|liens?)/i.test(x.text)) ||
                (x.interactive && /\d/.test(x.text) && /(?:[a-z0-9-]+\.)+[a-z]{2,}/i.test(x.text)) ||
                (x.popup==='dialog' && /(?:links?|链接|enlaces?|liens?)/i.test(x.text))
              ));"""
        )
    except Exception:  # noqa: BLE001
        dom_labels = []
    popup_signal = False
    for item in dom_labels or []:
        if not isinstance(item, Mapping):
            continue
        text = str(item.get("text") or "").strip()
        if text:
            labels.append(text)
            if (
                item.get("interactive") is True
                or item.get("popup") == "dialog"
                or item.get("expanded") in {"true", "false"}
            ):
                interactive_labels.add(text)
        if item.get("popup") == "dialog" or item.get("expanded") in {"true", "false"}:
            popup_signal = True
    labels = list(dict.fromkeys(labels))
    parsed = [parse_count(label) for label in labels]
    counts = {count for count in parsed if isinstance(count, int) and count >= 1}
    profile_signal = bool(
        isinstance(profile, Mapping)
        and (profile.get("_bio_has_more") or profile.get("_bio_more_count") is not None)
    )
    known_language_signal = any(
        re.search(
            r"(?:\d.*(?:more|m[aá]s|autres?|mais|ещ[её]|더|还有|另有|另外|链接|links?|enlaces?|liens?))",
            label,
            re.I,
        )
        for label in labels
    )
    # The domain+number fallback exists for unknown locales, but ordinary bio
    # text often contains a domain next to a follower/year count.  Treat that
    # shape as hidden-link evidence only when the DOM ties it to a real control.
    interactive_generic_signal = any(
        re.search(
            r"(?:[a-z0-9-]+\.)+[a-z]{2,}(?:/\S+)?\s+\D{0,40}\d+",
            label,
            re.I,
        )
        for label in interactive_labels
    )
    generic_signal = bool(known_language_signal or interactive_generic_signal)
    has_more_signal = bool(counts or profile_signal or popup_signal or generic_signal)
    declared = next(iter(counts)) if len(counts) == 1 else None
    ambiguous = bool(has_more_signal and declared is None)
    return has_more_signal, declared, {
        "signal_detected": has_more_signal,
        "declared_more_count": declared,
        "ambiguous": ambiguous,
        "label_count": len(labels),
        "label_sha256": [_sha_text(label) for label in labels],
        "popup_signal": popup_signal,
        "interactive_label_count": len(interactive_labels),
        "interactive_generic_signal": bool(interactive_generic_signal),
    }


class _BrowserProbeRuntime:
    """Minimal proxy-only browser adapter for fresh profile and bio-link checks."""

    def __init__(
        self,
        *,
        credentials: Mapping[str, Mapping[str, Any]],
        proxy_loader: Callable[..., object],
        headless: bool,
        resolver: Callable[[str], Iterable[str]] | None,
    ) -> None:
        self._credentials = credentials
        self._proxy_loader = proxy_loader
        self._headless = headless
        self._resolver = resolver
        self._pw_manager: Any = None
        self._pw: Any = None
        self._ctx: Any = None
        self._page: Any = None
        self._active_slot: int | None = None
        self._proxy_enforced = False
        self._context_guard_installed = False
        self._context_request_guard: Callable[[Any, Any], None] | None = None
        self._context_websocket_guard: Callable[[Any], None] | None = None
        self._context_popup_guard: Callable[[Any], None] | None = None
        self._active_blocked_requests: list[str] | None = None
        self._nonce = uuid.uuid4().hex[:8]

    def __enter__(self) -> "_BrowserProbeRuntime":
        from playwright.sync_api import sync_playwright

        self._pw_manager = sync_playwright()
        self._pw = self._pw_manager.__enter__()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self._close_context()
        if self._pw_manager is not None:
            self._pw_manager.__exit__(exc_type, exc, traceback)

    def _close_context(self) -> None:
        self._proxy_enforced = False
        self._context_guard_installed = False
        self._active_blocked_requests = None
        if self._ctx is None:
            return
        try:
            import browser_collect_v2 as browser_collect

            browser_collect.close_ctx(self._ctx)
        finally:
            self._ctx = None
            self._page = None
            self._active_slot = None
            self._context_request_guard = None
            self._context_websocket_guard = None
            self._context_popup_guard = None

    def _open_account(self, account: Any, slot: int) -> None:
        import browser_collect_v2 as browser_collect

        self._close_context()
        credential = self._credentials[account.username.casefold()]
        proxy = self._proxy_loader(
            session=f"sfb{self._nonce}{slot}{account.username}"[:16], ttl=900
        )
        _public_proxy_check(proxy)
        options: dict[str, Any] = {
            "user_data_dir": str(account.profile_path),
            "channel": "chrome",
            "headless": self._headless,
            "viewport": {"width": 1000, "height": 1300},
            "locale": "en-US",
            "service_workers": "block",
            "extra_http_headers": {"Accept-Language": "en-US,en;q=0.9"},
            "args": [
                "--no-first-run",
                "--no-default-browser-check",
                "--lang=en-US",
                "--blink-settings=imagesEnabled=false",
            ],
            "proxy": dict(proxy),
        }
        self._ctx = self._pw.chromium.launch_persistent_context(**options)
        browser_collect._install_blockers(self._ctx)
        self._ctx.add_cookies(
            [
                {
                    "name": "sessionid",
                    "value": credential["sessionid"],
                    "domain": ".instagram.com",
                    "path": "/",
                    "secure": True,
                },
                {
                    "name": "ds_user_id",
                    "value": credential["ds_user_id"],
                    "domain": ".instagram.com",
                    "path": "/",
                    "secure": True,
                },
            ]
        )
        self._page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        self._page.set_default_timeout(20000)
        self._page.set_default_navigation_timeout(25000)
        try:
            self._install_context_network_guards(self._ctx)
            self._active_slot = slot
            # Enabled only after the validated proxy context and its permanent
            # all-page guards were both installed successfully.
            self._proxy_enforced = True
        except Exception:  # noqa: BLE001
            self._close_context()
            raise

    def _record_blocked_request(self, reason: str) -> None:
        if self._active_blocked_requests is not None:
            self._active_blocked_requests.append(reason)

    def _external_request_guard(self, route: Any, request: Any) -> None:
        request_url = str(request.url)
        try:
            scheme = urlsplit(request_url).scheme.casefold()
        except ValueError:
            scheme = ""
        # data/blob/about cannot connect to a new network destination.
        if scheme in {"data", "blob", "about"}:
            route.fallback()
            return
        # WebSockets are blocked separately at BrowserContext level.  Every
        # other non-HTTP scheme is rejected here; every HTTP(S) request,
        # including popup first navigations, XHR, iframes, scripts and images,
        # receives the same SSRF validation.
        if scheme not in {"http", "https"}:
            self._record_blocked_request("unsafe_request_scheme")
            route.abort()
            return
        try:
            self._safe_external_url(request_url)
        except StorefrontBackfillError:
            self._record_blocked_request("unsafe_external_request")
            route.abort()
            return
        route.fallback()

    def _install_context_network_guards(self, context: Any) -> None:
        """Permanently guard every page, popup, subrequest and WebSocket."""

        request_guard = self._external_request_guard

        def block_websocket(websocket_route: Any) -> None:
            self._record_blocked_request("external_websocket_blocked")
            websocket_route.close(code=1008, reason="external websocket blocked")

        def close_popup(popup: Any) -> None:
            self._record_blocked_request("unexpected_popup")
            try:
                popup.close(run_before_unload=False)
            except TypeError:
                popup.close()
            except Exception:  # noqa: BLE001
                pass

        context.route("**/*", request_guard)
        context.route_web_socket("**/*", block_websocket)
        context.on("page", close_popup)
        self._context_request_guard = request_guard
        self._context_websocket_guard = block_websocket
        self._context_popup_guard = close_popup
        self._context_guard_installed = True

    def _close_new_context_pages(self, baseline_page_ids: set[int]) -> None:
        if self._ctx is None:
            return
        for popup in list(self._ctx.pages):
            if id(popup) in baseline_page_ids:
                continue
            self._record_blocked_request("unexpected_popup")
            try:
                popup.close(run_before_unload=False)
            except TypeError:
                popup.close()
            except Exception:  # noqa: BLE001
                pass

    def _safe_external_url(self, value: object) -> str:
        return safe_external_url(
            value,
            resolver=self._resolver,
            _allow_proxy_synthetic_dns=bool(
                self._proxy_enforced
                and self._context_guard_installed
                and self._ctx is not None
            ),
        )

    def _target_check(self, page: Any, value: str) -> dict[str, Any]:
        try:
            target = self._safe_external_url(value)
        except StorefrontBackfillError:
            return {
                "source_url": str(value),
                "status": "unsafe",
                "commerce_links": [],
            }
        if _is_noncommerce_social_url(target):
            return {
                "source_url": target,
                "status": "succeeded",
                "final_url": target,
                "commerce_links": [],
                "note": "recognized_noncommerce_social",
                "page_health": {
                    "healthy": True,
                    "reason": "navigation_not_required_noncommerce_social",
                },
            }
        direct_type = storefront_policy.classify_url(target)
        if direct_type:
            return {
                "source_url": target,
                "status": "succeeded",
                "final_url": target,
                "commerce_links": [{"url": target, "type": direct_type}],
                "note": "recognized_direct_url",
                "page_health": {
                    "healthy": True,
                    "reason": "navigation_not_required_recognized_url",
                },
            }
        if self._active_blocked_requests is not None:
            raise StorefrontBackfillError("nested external target probe is forbidden")
        blocked_requests: list[str] = []
        self._active_blocked_requests = blocked_requests
        baseline_page_ids = (
            {id(item) for item in self._ctx.pages}
            if self._ctx is not None and self._context_guard_installed
            else set()
        )
        # A redundant page route keeps the helper fail-closed in isolated unit
        # use.  Production security is provided by the permanent context route,
        # which also sees popup first navigations and all other pages.
        page_request_guard = self._external_request_guard
        page.route("**/*", page_request_guard)
        try:
            response = page.goto(
                target, wait_until="domcontentloaded", timeout=30000
            )
            if blocked_requests:
                raise RuntimeError("unsafe_external_request")
            response_status = response.status if response is not None else None
            if response is None:
                return {
                    "source_url": target,
                    "status": "failed",
                    "http_status": None,
                    "commerce_links": [],
                    "note": "missing_navigation_response",
                    "page_health": {
                        "healthy": False,
                        "reason": "missing_navigation_response",
                    },
                }
            if not 200 <= int(response_status) < 400:
                return {
                    "source_url": target,
                    "status": "failed",
                    "http_status": response_status,
                    "commerce_links": [],
                    "note": "external_http_error",
                    "page_health": {
                        "healthy": False,
                        "reason": "external_http_error",
                    },
                }
            final_url = self._safe_external_url(str(page.url))
            page.wait_for_timeout(800)
            samples = [_ordinary_dom_sample(page)]
            page.wait_for_timeout(600)
            samples.append(_ordinary_dom_sample(page))
            if blocked_requests:
                raise RuntimeError("unsafe_external_request")
            page_health = _ordinary_page_health(samples)
            if not page_health["healthy"]:
                return {
                    "source_url": target,
                    "status": "failed",
                    "final_url": final_url,
                    "http_status": response_status,
                    "commerce_links": [],
                    "note": "external_page_unhealthy",
                    "page_health": page_health,
                }
            hrefs = page.eval_on_selector_all(
                "a[href]", "els => els.map(e => e.href)"
            ) or []
            commerce: list[dict[str, str]] = []
            seen: set[tuple[str, str]] = set()
            for href in [final_url, *hrefs]:
                if _is_noncommerce_social_url(str(href)):
                    continue
                kind = storefront_policy.classify_url(str(href))
                if not kind:
                    continue
                safe_href = self._safe_external_url(str(href))
                key = (safe_href.casefold(), kind)
                if key not in seen:
                    seen.add(key)
                    commerce.append({"url": safe_href, "type": kind})
            self._close_new_context_pages(baseline_page_ids)
            if blocked_requests:
                raise RuntimeError("unsafe_external_request")
            return {
                "source_url": target,
                "status": "succeeded",
                "final_url": final_url,
                "http_status": response_status,
                "commerce_links": commerce,
                "note": "opened_external_target",
                "page_health": page_health,
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "source_url": target,
                "status": "failed",
                "commerce_links": [],
                "error": type(exc).__name__,
                "page_health": {
                    "healthy": False,
                    "reason": "navigation_or_dom_exception",
                },
            }
        finally:
            self._close_new_context_pages(baseline_page_ids)
            self._active_blocked_requests = None
            try:
                page.unroute("**/*", page_request_guard)
            except Exception:  # noqa: BLE001
                pass

    def probe(
        self, handle: str, account: Any, heartbeat: Callable[[], None], *, slot: int
    ) -> dict[str, Any]:
        import browser_collect_v2 as browser_collect

        if self._active_slot != slot:
            self._open_account(account, slot)
        heartbeat()
        profile = browser_collect.fetch_profile_browser(self._page, handle)
        profile_url = str(self._page.url)
        identity, identity_check = _profile_identity(self._page, handle, profile)
        healthy, health_check = _profile_health(self._page, profile)
        has_more, declared_more, declaration_check = _bio_more_declaration(
            self._page,
            profile,
            parse_count=browser_collect._bio_more_count,
        )
        expanded: list[str] = []
        expansion_succeeded = True
        if has_more:
            expanded = browser_collect._expand_bio_links(
                self._page, include_social=True
            )
            expansion_succeeded = declared_more is not None and bool(expanded)
        profile_copy = dict(profile or {})
        profile_copy["_identity_verified"] = identity
        profile_copy["_profile_healthy"] = healthy
        profile_copy["_profile_identity_check"] = identity_check
        profile_copy["_profile_health_check"] = health_check
        profile_copy["_bio_has_more"] = has_more
        profile_copy["_bio_more_count"] = declared_more
        profile_copy["_bio_declaration_check"] = declaration_check
        raw_links = _dedupe_urls([profile_copy.get("external_url"), *expanded])
        checks: list[dict[str, Any]] = []
        source_indexes: dict[str, int] = {}
        for link in raw_links:
            heartbeat()
            check = self._target_check(self._page, link)
            source = str(check.get("source_url") or "").strip()
            folded = source.casefold()
            if not source:
                continue
            if folded in source_indexes:
                existing_index = source_indexes[folded]
                if (
                    checks[existing_index].get("status") != "succeeded"
                    and check.get("status") == "succeeded"
                ):
                    checks[existing_index] = check
                continue
            source_indexes[folded] = len(checks)
            checks.append(check)
        observed_links = [str(check["source_url"]) for check in checks]
        profile_copy["external_url"] = observed_links[0] if observed_links else None
        return resolve_observation(
            handle=handle,
            profile=profile_copy,
            profile_url=profile_url,
            expanded_links=expanded,
            expansion_succeeded=expansion_succeeded,
            observed_links=observed_links,
            raw_observed_link_count=len(raw_links),
            terminal_observed_link_count=len(observed_links),
            target_checks=checks,
        )


def _account_credentials(
    accounts_file: Path, account_specs: Sequence[Any]
) -> dict[str, Mapping[str, Any]]:
    import browser_collect_v2 as browser_collect

    try:
        records = browser_collect.load_accounts(accounts_file)
    except (OSError, UnicodeError, ValueError) as exc:
        raise StorefrontBackfillError(f"cannot load IG accounts: {type(exc).__name__}") from exc
    mapping: dict[str, Mapping[str, Any]] = {}
    for record in records:
        username = str(record.get("username") or "").strip()
        folded = username.casefold()
        if not username or folded in mapping:
            raise StorefrontBackfillError("IG account credentials are ambiguous")
        mapping[folded] = record
    missing = [
        item.username
        for item in account_specs
        if item.username.casefold() not in mapping
    ]
    if missing:
        raise StorefrontBackfillError(
            "preflight accounts lack usable login cookies: " + ", ".join(missing)
        )
    return mapping


def _probe_failure_result(handle: str, exc: BaseException) -> dict[str, Any]:
    return {
        "status": "unknown",
        "storefront_url": None,
        "storefront_type": None,
        "amazon_storefront_link": None,
        "external_url": None,
        "bio_links": [],
        "_bio_has_more": False,
        "_bio_more_count": None,
        "evidence": {
            "schema": EVIDENCE_SCHEMA,
            "captured_at": _utc_now(),
            "profile_url": f"https://www.instagram.com/{handle}/",
            "identity_verified": False,
            "profile_healthy": False,
            "bio_links_complete": False,
            "bio_link_expansion": {
                "required": False,
                "declared_more_count": None,
                "declared_more": None,
                "minimum_total_link_count": None,
                "declared_total": None,
                "raw_observed_total": 0,
                "terminal_observed_total": 0,
                "observed_total_link_count": 0,
                "expanded_link_count": 0,
                "complete": False,
                "terminal_complete": False,
                "declaration_evidence": None,
            },
            "target_checks_complete": False,
            "target_checks": [],
            "failures": [f"probe_failed:{type(exc).__name__}"],
            "partial_failures": [],
            "note": "probe_failed",
        },
    }


def _with_attempt_evidence(
    result: Mapping[str, Any], attempts: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    output = dict(result)
    evidence = dict(output["evidence"])
    evidence["attempts"] = [dict(attempt) for attempt in attempts]
    output["evidence"] = evidence
    return output


def collect_plan(
    *,
    db_path: str | os.PathLike[str],
    batch_id: str,
    expected_count: int,
    expected_handle_set_sha256: str,
    plan_path: str | os.PathLike[str],
    accounts_file: str | os.PathLike[str],
    deep_accounts_file: str | os.PathLike[str],
    profile_root: str | os.PathLike[str],
    lease_db: str | os.PathLike[str],
    expected_status: str = "decided",
    per_account: int = 8,
    lease_ttl_seconds: float = 120.0,
    heartbeat_seconds: float = 30.0,
    headless: bool = True,
    probe_candidate: Callable[[str, Any, Callable[[], None]], Mapping[str, Any]]
    | None = None,
    preflight_fn: Callable[..., Any] = graph_runner.preflight_account_pools,
    proxy_loader: Callable[..., object] | None = None,
    lease_api: Any = resource_leases,
    resolver: Callable[[str], Iterable[str]] | None = None,
) -> dict[str, Any]:
    """Collect a complete review plan without ever opening the creator DB writable."""

    if not str(batch_id or "").strip() or not str(expected_status or "").strip():
        raise StorefrontBackfillError("batch_id and expected_status are required")
    if isinstance(expected_count, bool) or int(expected_count) <= 0:
        raise StorefrontBackfillError("expected_count must be positive")
    if isinstance(per_account, bool) or int(per_account) <= 0:
        raise StorefrontBackfillError("per_account must be positive")
    if heartbeat_seconds <= 0 or lease_ttl_seconds <= heartbeat_seconds * 2:
        raise StorefrontBackfillError("lease TTL must exceed two heartbeat intervals")
    requested_destination = Path(plan_path).expanduser()
    if os.path.lexists(requested_destination):
        raise StorefrontBackfillError(f"plan already exists: {requested_destination}")
    destination = requested_destination.resolve()

    db = Path(db_path).expanduser().resolve()
    lease_database = _separate_lease_database(db, lease_db)
    snapshots = inspect_unknown_cohort(
        db, batch_id=batch_id, expected_status=expected_status
    )
    if len(snapshots) != int(expected_count):
        raise StorefrontBackfillError(
            f"unknown cohort count mismatch: expected {expected_count}, got {len(snapshots)}"
        )
    expected_handles_sha = str(expected_handle_set_sha256 or "").strip()
    if not _SHA256_RE.fullmatch(expected_handles_sha):
        raise StorefrontBackfillError(
            "expected_handle_set_sha256 must be 64 lowercase hex"
        )
    actual_handles_sha = handle_set_sha256(item["handle"] for item in snapshots)
    if actual_handles_sha != expected_handles_sha:
        raise StorefrontBackfillError(
            "unknown cohort handle-set SHA-256 mismatch"
        )
    if any(item["cas"]["locked_at"] is not None for item in snapshots):
        raise StorefrontBackfillError("unknown cohort contains locked rows")
    if any(item["cas"]["stage_error"] is not None for item in snapshots):
        raise StorefrontBackfillError("unknown cohort contains stage errors")

    try:
        pools = preflight_fn(
            accounts_file,
            deep_accounts_file,
            profile_root,
            require_profile_directories=True,
            reject_active_profile_markers=True,
        )
    except Exception as exc:  # noqa: BLE001
        raise StorefrontBackfillError(
            f"account/Profile preflight failed: {type(exc).__name__}"
        ) from exc
    accounts = tuple(pools.shallow)
    if len(accounts) < 2:
        raise StorefrontBackfillError(
            "at least two shallow accounts are required for cross-account retry"
        )
    credentials = _account_credentials(Path(accounts_file), accounts)

    if proxy_loader is None:
        import browser_collect_v2 as browser_collect

        proxy_loader = browser_collect.load_proxy
    preflight_proxy = proxy_loader(
        session=f"sfbpre{uuid.uuid4().hex[:8]}", ttl=900
    )
    _public_proxy_check(preflight_proxy)

    keys = lease_api.build_resource_keys(
        accounts=[item.username for item in accounts],
        chrome_profiles=[item.profile_path for item in accounts],
    )
    singleton = f"pipeline:storefront-backfill:{str(batch_id).casefold()}"
    keys = tuple(sorted((*keys, singleton)))
    run_id = f"storefront-backfill-{uuid.uuid4().hex[:12]}"
    bundle = lease_api.acquire_resources(
        lease_database,
        batch_id=batch_id,
        run_id=run_id,
        wave_id="storefront-backfill",
        worker_id="collector",
        resource_keys=keys,
        ttl_seconds=lease_ttl_seconds,
    )
    guard = _LeaseHeartbeat(
        lease_api,
        lease_database,
        bundle,
        ttl_seconds=lease_ttl_seconds,
        interval_seconds=heartbeat_seconds,
    )
    results: list[dict[str, Any]] = []
    completed = False
    try:
        guard.start()
        runtime: _BrowserProbeRuntime | None = None
        if probe_candidate is None:
            runtime = _BrowserProbeRuntime(
                credentials=credentials,
                proxy_loader=proxy_loader,
                headless=headless,
                resolver=resolver,
            )
            runtime.__enter__()
        try:
            for index, snapshot in enumerate(snapshots):
                guard.check()
                block = index // int(per_account)
                account_index = block % len(accounts)
                attempt_records: list[dict[str, Any]] = []
                result: dict[str, Any] | None = None
                for attempt_index in range(2):
                    selected_index = (
                        account_index if attempt_index == 0 else (account_index + 1) % len(accounts)
                    )
                    account = accounts[selected_index]
                    # Retry slots are disjoint from primary block slots, forcing
                    # the browser adapter to close the old Profile and open the
                    # different preflighted account/Profile pair.
                    runtime_slot = (
                        block
                        if attempt_index == 0
                        else len(snapshots) + index + 1
                    )
                    try:
                        if probe_candidate is not None:
                            observed = probe_candidate(
                                snapshot["handle"], account, guard.check
                            )
                        else:
                            assert runtime is not None
                            observed = runtime.probe(
                                snapshot["handle"],
                                account,
                                guard.check,
                                slot=runtime_slot,
                            )
                        attempt_result = _validate_probe_result(
                            observed, handle=snapshot["handle"]
                        )
                    except Exception as exc:  # noqa: BLE001
                        attempt_result = _probe_failure_result(
                            snapshot["handle"], exc
                        )
                    attempt_records.append(
                        {
                            "attempt": attempt_index + 1,
                            "account_slot": selected_index,
                            "status": attempt_result["status"],
                            "evidence": dict(attempt_result["evidence"]),
                        }
                    )
                    result = attempt_result
                    if attempt_result["status"] != "unknown":
                        break
                assert result is not None
                result = _with_attempt_evidence(result, attempt_records)
                results.append(
                    {
                        "handle": snapshot["handle"],
                        "snapshot": {
                            "stage_json_sha256": snapshot["stage_json_sha256"],
                            "protected_stage_sha256": snapshot[
                                "protected_stage_sha256"
                            ],
                            **snapshot["cas"],
                        },
                        "result": result,
                    }
                )
            guard.check()
            completed = True
        finally:
            if runtime is not None:
                runtime.__exit__(None, None, None)
    finally:
        guard.close(reason="collection_complete" if completed else "collection_failed")

    unresolved = [item["handle"] for item in results if item["result"]["status"] == "unknown"]
    plan: dict[str, Any] = {
        "schema": PLAN_SCHEMA,
        "created_at": _utc_now(),
        "batch_id": batch_id,
        "expected_status": expected_status,
        "expected_count": int(expected_count),
        "expected_handle_set_sha256": expected_handles_sha,
        "handle_set_sha256": actual_handles_sha,
        "apply_allowed": not unresolved and len(results) == int(expected_count),
        "unresolved_count": len(unresolved),
        "collection_contract": {
            "creator_db_mode": "read_only",
            "proxy_required": True,
            "fresh_profile_required": True,
            "all_bio_links_required": True,
            "all_targets_must_succeed_for_confirmed_no": True,
            "resource_key_set_sha256": _sha_json(keys),
            "account_schedule_sha256": pools.combined_order_sha256,
            "per_account": int(per_account),
        },
        "rows": results,
    }
    plan["plan_sha256"] = _plan_sha(plan)
    _write_plan(destination, plan)
    return plan


def _validated_plan(
    plan: Mapping[str, Any],
    *,
    expected_plan_sha256: str,
    batch_id: str,
    expected_count: int,
) -> tuple[dict[str, Any], tuple[dict[str, Any], ...]]:
    expected_sha = str(expected_plan_sha256 or "").strip().casefold()
    if not _SHA256_RE.fullmatch(expected_sha):
        raise StorefrontBackfillError("expected_plan_sha256 must be 64 lowercase hex")
    actual_sha = _plan_sha(plan)
    embedded_sha = str(plan.get("plan_sha256") or "").casefold()
    if actual_sha != expected_sha or embedded_sha != expected_sha:
        raise StorefrontBackfillError("plan SHA-256 mismatch")
    if plan.get("schema") != PLAN_SCHEMA:
        raise StorefrontBackfillError("unsupported plan schema")
    if plan.get("batch_id") != batch_id:
        raise StorefrontBackfillError("plan batch_id mismatch")
    if plan.get("expected_count") != int(expected_count):
        raise StorefrontBackfillError("plan expected_count mismatch")
    expected_status = plan.get("expected_status")
    if not isinstance(expected_status, str) or not expected_status.strip():
        raise StorefrontBackfillError("plan expected_status is missing")
    rows_value = plan.get("rows")
    if not isinstance(rows_value, list) or len(rows_value) != int(expected_count):
        raise StorefrontBackfillError("plan does not contain the exact expected cohort")
    rows: list[dict[str, Any]] = []
    handles: list[str] = []
    for raw in rows_value:
        if not isinstance(raw, Mapping):
            raise StorefrontBackfillError("plan row must be an object")
        handle = _normalize_handle(raw.get("handle"))
        snapshot = raw.get("snapshot")
        if not isinstance(snapshot, Mapping):
            raise StorefrontBackfillError(f"@{handle}: missing plan snapshot")
        for key in ("stage_json_sha256", "protected_stage_sha256"):
            if not _SHA256_RE.fullmatch(str(snapshot.get(key) or "")):
                raise StorefrontBackfillError(f"@{handle}: invalid {key}")
        for field in _CAS_FIELDS:
            if field not in snapshot:
                raise StorefrontBackfillError(f"@{handle}: missing CAS field {field}")
        result = _validate_probe_result(raw.get("result"), handle=handle)
        attempts = result["evidence"].get("attempts")
        if not isinstance(attempts, list) or len(attempts) not in {1, 2}:
            raise StorefrontBackfillError(f"@{handle}: invalid probe attempt ledger")
        if any(not isinstance(attempt, Mapping) for attempt in attempts):
            raise StorefrontBackfillError(f"@{handle}: invalid probe attempt ledger")
        if attempts[-1].get("status") != result["status"]:
            raise StorefrontBackfillError(
                f"@{handle}: final result disagrees with probe attempt ledger"
            )
        if len(attempts) == 2:
            if attempts[0].get("status") != "unknown":
                raise StorefrontBackfillError(
                    f"@{handle}: retry occurred after a conclusive attempt"
                )
            if attempts[0].get("account_slot") == attempts[1].get("account_slot"):
                raise StorefrontBackfillError(
                    f"@{handle}: retry did not switch shallow accounts"
                )
        for attempt in attempts:
            attempt_evidence = attempt.get("evidence")
            if not isinstance(attempt_evidence, Mapping) or (
                attempt_evidence.get("schema") != EVIDENCE_SCHEMA
            ):
                raise StorefrontBackfillError(
                    f"@{handle}: invalid probe attempt evidence"
                )
        if result["status"] == "unknown":
            raise StorefrontBackfillError(
                f"@{handle}: unresolved plan cannot be applied"
            )
        rows.append({"handle": handle, "snapshot": dict(snapshot), "result": result})
        handles.append(handle)
    folded = [handle.casefold() for handle in handles]
    if len(folded) != len(set(folded)):
        raise StorefrontBackfillError("plan has duplicate handles")
    handles_sha = handle_set_sha256(handles)
    if plan.get("handle_set_sha256") != handles_sha:
        raise StorefrontBackfillError("plan handle-set SHA-256 mismatch")
    if plan.get("expected_handle_set_sha256") != handles_sha:
        raise StorefrontBackfillError("plan expected handle-set SHA-256 mismatch")
    if plan.get("apply_allowed") is not True or plan.get("unresolved_count") != 0:
        raise StorefrontBackfillError("plan is not marked fully resolved")
    return dict(plan), tuple(rows)


def _changed_top_level_keys(
    before: Mapping[str, Any], after: Mapping[str, Any]
) -> set[str]:
    marker = object()
    return {
        key
        for key in set(before) | set(after)
        if before.get(key, marker) != after.get(key, marker)
    }


def _patched_stage(
    stage: Mapping[str, Any], result: Mapping[str, Any]
) -> dict[str, Any]:
    patched = dict(stage)
    status = result["status"]
    patched["storefront_status"] = status
    patched["storefront_evidence"] = dict(result["evidence"])
    patched["bio_links"] = list(result.get("bio_links") or [])
    patched["external_url"] = result.get("external_url")
    patched["_bio_has_more"] = bool(result.get("_bio_has_more"))
    patched["_bio_more_count"] = result.get("_bio_more_count")
    if status == "confirmed_yes":
        patched["storefront_url"] = result["storefront_url"]
        patched["storefront_type"] = result["storefront_type"]
        if result["storefront_type"] == "Amazon":
            patched["amazon_storefront_link"] = result["storefront_url"]
        else:
            patched.pop("amazon_storefront_link", None)
    else:
        patched.pop("storefront_url", None)
        patched.pop("storefront_type", None)
        patched.pop("amazon_storefront_link", None)
    changed = _changed_top_level_keys(stage, patched)
    if not changed <= STOREFRONT_STAGE_FIELDS:
        raise StorefrontBackfillError(
            "Storefront patch escaped allowlist: " + ", ".join(sorted(changed))
        )
    if _protected_sha(stage) != _protected_sha(patched):
        raise StorefrontBackfillError("protected candidate content changed")
    return patched


def _current_rows_by_handle(
    connection: sqlite3.Connection,
    handles: Sequence[str],
) -> dict[str, sqlite3.Row]:
    if not handles:
        return {}
    placeholders = ",".join("?" for _ in handles)
    rows = connection.execute(
        f"""
        SELECT handle,stage_json,storefront_status,amazon_storefront_link,
               status,client_status,stage_error,locked_at,stage_updated_at,
               discovery_batch
          FROM creator_profiles
         WHERE handle IN ({placeholders}) COLLATE NOCASE
        """,
        tuple(handles),
    ).fetchall()
    output: dict[str, sqlite3.Row] = {}
    for row in rows:
        folded = str(row["handle"]).casefold()
        if folded in output:
            raise StorefrontBackfillError("database has ambiguous candidate handles")
        output[folded] = row
    return output


def apply_plan(
    *,
    db_path: str | os.PathLike[str],
    batch_id: str,
    expected_count: int,
    plan_path: str | os.PathLike[str],
    expected_plan_sha256: str,
) -> dict[str, Any]:
    """CAS-apply the exact fully resolved plan in one indivisible transaction."""

    plan = _read_plan(Path(plan_path))
    _, plan_rows = _validated_plan(
        plan,
        expected_plan_sha256=expected_plan_sha256,
        batch_id=batch_id,
        expected_count=expected_count,
    )
    expected_status = str(plan["expected_status"])
    db = Path(db_path).expanduser().resolve()
    if not db.is_file():
        raise StorefrontBackfillError(f"creator database does not exist: {db}")
    connection = sqlite3.connect(db, timeout=30, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=30000")
    try:
        connection.execute("BEGIN IMMEDIATE")
        _validate_database(connection)
        cohort = _load_unknown_cohort(
            connection, batch_id=batch_id, expected_status=expected_status
        )
        current_handles = {item["handle"].casefold() for item in cohort}
        planned_handles = {item["handle"].casefold() for item in plan_rows}
        if len(cohort) != int(expected_count) or current_handles != planned_handles:
            raise StorefrontBackfillError(
                "current unresolved cohort differs from the reviewed plan"
            )
        current_rows = _current_rows_by_handle(
            connection, [item["handle"] for item in plan_rows]
        )
        if set(current_rows) != planned_handles:
            raise StorefrontBackfillError("one or more planned rows are missing")

        post_protected: dict[str, str] = {}
        post_stage_sha: dict[str, str] = {}
        for item in plan_rows:
            handle = item["handle"]
            row = current_rows[handle.casefold()]
            snapshot = item["snapshot"]
            raw_stage = row["stage_json"]
            stage = _load_json_object(raw_stage, label=f"@{handle}")
            if _sha_text(raw_stage) != snapshot["stage_json_sha256"]:
                raise StorefrontBackfillError(f"@{handle}: stage_json CAS changed")
            if _protected_sha(stage) != snapshot["protected_stage_sha256"]:
                raise StorefrontBackfillError(f"@{handle}: protected content CAS changed")
            for field in _CAS_FIELDS:
                if row[field] != snapshot[field]:
                    raise StorefrontBackfillError(f"@{handle}: {field} CAS changed")
            if row["discovery_batch"] != batch_id or row["status"] != expected_status:
                raise StorefrontBackfillError(f"@{handle}: batch/status precondition changed")
            if row["locked_at"] is not None:
                raise StorefrontBackfillError(f"@{handle}: row is locked")

            patched = _patched_stage(stage, item["result"])
            after_raw = _canonical_json(patched)
            result = item["result"]
            hot_amazon = (
                result["storefront_url"]
                if result["status"] == "confirmed_yes"
                and result["storefront_type"] == "Amazon"
                else None
            )
            cursor = connection.execute(
                """
                UPDATE creator_profiles
                   SET stage_json=?, storefront_status=?, amazon_storefront_link=?
                 WHERE handle=?
                   AND stage_json=?
                   AND status IS ?
                   AND client_status IS ?
                   AND stage_error IS ?
                   AND locked_at IS ?
                   AND stage_updated_at IS ?
                   AND discovery_batch IS ?
                   AND storefront_status IS ?
                   AND amazon_storefront_link IS ?
                """,
                (
                    after_raw,
                    result["status"],
                    hot_amazon,
                    row["handle"],
                    raw_stage,
                    snapshot["status"],
                    snapshot["client_status"],
                    snapshot["stage_error"],
                    snapshot["locked_at"],
                    snapshot["stage_updated_at"],
                    snapshot["discovery_batch"],
                    snapshot["storefront_status"],
                    snapshot["amazon_storefront_link"],
                ),
            )
            if cursor.rowcount != 1:
                raise StorefrontBackfillError(f"@{handle}: row CAS update failed")
            post_protected[handle.casefold()] = _protected_sha(patched)
            post_stage_sha[handle.casefold()] = _sha_text(after_raw)

        verified_rows = _current_rows_by_handle(
            connection, [item["handle"] for item in plan_rows]
        )
        for item in plan_rows:
            handle = item["handle"]
            row = verified_rows[handle.casefold()]
            stage = _load_json_object(row["stage_json"], label=f"@{handle} after apply")
            if _sha_text(row["stage_json"]) != post_stage_sha[handle.casefold()]:
                raise StorefrontBackfillError(f"@{handle}: post-write stage hash mismatch")
            if _protected_sha(stage) != item["snapshot"]["protected_stage_sha256"]:
                raise StorefrontBackfillError(f"@{handle}: protected content was modified")
            if _protected_sha(stage) != post_protected[handle.casefold()]:
                raise StorefrontBackfillError(f"@{handle}: protected postcondition failed")
            effective = storefront_policy.effective_status(
                _effective_candidate(row, stage)
            )
            if effective != item["result"]["status"] or effective == "unknown":
                raise StorefrontBackfillError(f"@{handle}: Storefront postcondition failed")
            for field in _ROW_CAS_FIELDS:
                if row[field] != item["snapshot"][field]:
                    raise StorefrontBackfillError(
                        f"@{handle}: protected relational field changed: {field}"
                    )
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()
    return {
        "batch_id": batch_id,
        "applied_count": len(plan_rows),
        "plan_sha256": expected_plan_sha256,
        "protected_fields_unchanged": True,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Two-phase strict Storefront evidence backfill"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    collect = subparsers.add_parser(
        "collect", help="collect a review plan without writing the creator DB"
    )
    collect.add_argument("--db", required=True, type=Path)
    collect.add_argument("--batch-id", required=True)
    collect.add_argument("--expected-status", default="decided")
    collect.add_argument("--expected-count", required=True, type=int)
    collect.add_argument("--expected-handle-set-sha256", required=True)
    collect.add_argument("--plan-out", required=True, type=Path)
    collect.add_argument("--accounts-file", required=True, type=Path)
    collect.add_argument("--deep-accounts-file", required=True, type=Path)
    collect.add_argument("--profile-root", required=True, type=Path)
    collect.add_argument("--lease-db", required=True, type=Path)
    collect.add_argument("--per-account", type=int, default=8)
    collect.add_argument("--lease-ttl-seconds", type=float, default=120.0)
    collect.add_argument("--heartbeat-seconds", type=float, default=30.0)
    collect.add_argument("--headful", action="store_true")

    apply = subparsers.add_parser(
        "apply", help="atomically apply an exact reviewed and fully resolved plan"
    )
    apply.add_argument("--db", required=True, type=Path)
    apply.add_argument("--batch-id", required=True)
    apply.add_argument("--expected-count", required=True, type=int)
    apply.add_argument("--plan", required=True, type=Path)
    apply.add_argument("--expected-plan-sha256", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "collect":
            plan = collect_plan(
                db_path=args.db,
                batch_id=args.batch_id,
                expected_status=args.expected_status,
                expected_count=args.expected_count,
                expected_handle_set_sha256=args.expected_handle_set_sha256,
                plan_path=args.plan_out,
                accounts_file=args.accounts_file,
                deep_accounts_file=args.deep_accounts_file,
                profile_root=args.profile_root,
                lease_db=args.lease_db,
                per_account=args.per_account,
                lease_ttl_seconds=args.lease_ttl_seconds,
                heartbeat_seconds=args.heartbeat_seconds,
                headless=not args.headful,
            )
            print(
                _canonical_json(
                    {
                        "plan": str(args.plan_out),
                        "plan_sha256": plan["plan_sha256"],
                        "expected_count": plan["expected_count"],
                        "unresolved_count": plan["unresolved_count"],
                        "apply_allowed": plan["apply_allowed"],
                    }
                )
            )
            return 0 if plan["apply_allowed"] else 3
        result = apply_plan(
            db_path=args.db,
            batch_id=args.batch_id,
            expected_count=args.expected_count,
            plan_path=args.plan,
            expected_plan_sha256=args.expected_plan_sha256,
        )
        print(_canonical_json(result))
        return 0
    except StorefrontBackfillError as exc:
        print(f"storefront backfill refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
