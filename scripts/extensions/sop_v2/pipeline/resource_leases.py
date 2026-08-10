"""Cross-process, bundle-atomic leases for scarce pipeline resources.

The DAG runner has two kinds of resources that must never be used by two
workers at the same time:

* an Instagram account (shared by Stage 3 and pricing work); and
* an absolute Chrome profile directory (shared across workers and batches).

This module deliberately has no default database path.  Every public database
operation requires an explicit ``db_path`` so importing it can never open the
creator cache (or any other production database) as a side effect.
"""
from __future__ import annotations

import os
import sqlite3
import unicodedata
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Sequence


SCHEMA_VERSION = 1
TABLE_NAME = "resource_leases"

_CORE_COLUMNS = {
    "lease_id",
    "batch_id",
    "run_id",
    "wave_id",
    "worker_id",
    "resource_key",
    "token",
    "acquired_at",
    "heartbeat_at",
    "expires_at",
    "released_at",
}


class ResourceLeaseError(RuntimeError):
    """Base class for resource-lease failures."""


class ResourceLeaseValidationError(ResourceLeaseError, ValueError):
    """The caller supplied an ambiguous or incomplete lease request."""


class ResourceLeaseSchemaError(ResourceLeaseError):
    """An existing ``resource_leases`` table is not safely compatible."""


@dataclass(frozen=True)
class ResourceConflict:
    """One currently held resource that prevented an atomic acquisition."""

    resource_key: str
    batch_id: str
    run_id: str
    wave_id: str
    worker_id: str
    expires_at: str


class ResourceLeaseConflict(ResourceLeaseError):
    """At least one resource is active, so the whole acquisition was rolled back."""

    def __init__(self, conflicts: Sequence[ResourceConflict]):
        self.conflicts = tuple(conflicts)
        keys = ", ".join(conflict.resource_key for conflict in self.conflicts)
        super().__init__(f"resource lease conflict: {keys}")

    @property
    def resource_keys(self) -> tuple[str, ...]:
        return tuple(conflict.resource_key for conflict in self.conflicts)


@dataclass(frozen=True)
class LeaseCASFailure:
    """Why one expected member of a bundle failed ownership comparison."""

    resource_key: str
    reason: str


class ResourceLeaseCASFailure(ResourceLeaseError):
    """Heartbeat or release lost ownership of at least one bundle member."""

    def __init__(self, operation: str, failures: Sequence[LeaseCASFailure]):
        self.operation = operation
        self.failures = tuple(failures)
        detail = ", ".join(
            f"{failure.resource_key} ({failure.reason})"
            for failure in self.failures
        )
        super().__init__(f"resource lease {operation} CAS failed: {detail}")

    @property
    def resource_keys(self) -> tuple[str, ...]:
        return tuple(failure.resource_key for failure in self.failures)


class ResourceLeaseResumeError(ResourceLeaseError):
    """An explicit resume token does not identify the exact requested bundle."""


@dataclass(frozen=True)
class LeaseBundle:
    """The immutable ownership proof shared by every resource in one claim."""

    batch_id: str
    run_id: str
    wave_id: str
    worker_id: str
    resource_keys: tuple[str, ...]
    token: str
    acquired_at: str
    heartbeat_at: str
    expires_at: str

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serializable representation for worker hand-off."""

        return {
            "batch_id": self.batch_id,
            "run_id": self.run_id,
            "wave_id": self.wave_id,
            "worker_id": self.worker_id,
            "resource_keys": list(self.resource_keys),
            "token": self.token,
            "acquired_at": self.acquired_at,
            "heartbeat_at": self.heartbeat_at,
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "LeaseBundle":
        """Rebuild a bundle while reapplying all boundary validation."""

        try:
            bundle = cls(
                batch_id=str(payload["batch_id"]),
                run_id=str(payload["run_id"]),
                wave_id=str(payload["wave_id"]),
                worker_id=str(payload["worker_id"]),
                resource_keys=tuple(payload["resource_keys"]),  # type: ignore[arg-type]
                token=str(payload["token"]),
                acquired_at=str(payload["acquired_at"]),
                heartbeat_at=str(payload["heartbeat_at"]),
                expires_at=str(payload["expires_at"]),
            )
        except (KeyError, TypeError) as exc:
            raise ResourceLeaseValidationError(
                "invalid serialized lease bundle"
            ) from exc
        _validate_bundle(bundle)
        return bundle


def account_resource_key(username: str, *, platform: str = "instagram") -> str:
    """Return a cross-stage/cross-batch account key with Unicode case-folding."""

    normalized_platform = _normalized_label(platform, "platform").casefold()
    raw_username = unicodedata.normalize("NFKC", str(username or "")).strip()
    normalized_username = raw_username.lstrip("@").strip().casefold()
    if not normalized_username or any(char.isspace() for char in normalized_username):
        raise ResourceLeaseValidationError("username must be non-empty and contain no spaces")
    return f"account:{normalized_platform}:{normalized_username}"


def chrome_profile_resource_key(profile_path: str | os.PathLike[str]) -> str:
    """Return a canonical key for an absolute Chrome profile directory.

    Relative paths are rejected instead of being silently tied to a worker's
    current directory.  ``resolve(strict=False)`` also collapses ``..`` and
    existing symlinks, preventing aliases from bypassing mutual exclusion.
    """

    raw = os.fspath(profile_path)
    if not raw or not raw.strip():
        raise ResourceLeaseValidationError("Chrome profile path must be non-empty")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise ResourceLeaseValidationError("Chrome profile path must be absolute")
    canonical = os.path.normcase(str(path.resolve(strict=False)))
    return f"chrome-profile:{canonical}"


def build_resource_keys(
    *,
    accounts: Iterable[str] = (),
    chrome_profiles: Iterable[str | os.PathLike[str]] = (),
) -> tuple[str, ...]:
    """Build and validate one deterministic worker resource set."""

    keys = [account_resource_key(account) for account in accounts]
    keys.extend(chrome_profile_resource_key(path) for path in chrome_profiles)
    return _validated_resource_keys(keys)


def ensure_schema(db_path: str | os.PathLike[str]) -> None:
    """Create or safely migrate the isolated lease schema.

    Migration is intentionally additive only.  A compatible draft table that
    predates ``release_reason`` gets that nullable column.  Missing core columns
    fail closed; the function never drops, renames, or rebuilds an existing
    table.
    """

    with _connect(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
              lease_id INTEGER PRIMARY KEY AUTOINCREMENT,
              batch_id TEXT NOT NULL,
              run_id TEXT NOT NULL,
              wave_id TEXT NOT NULL,
              worker_id TEXT NOT NULL,
              resource_key TEXT NOT NULL,
              token TEXT NOT NULL,
              acquired_at TEXT NOT NULL,
              heartbeat_at TEXT NOT NULL,
              expires_at TEXT NOT NULL,
              released_at TEXT,
              release_reason TEXT,
              CHECK (length(trim(batch_id)) > 0),
              CHECK (length(trim(run_id)) > 0),
              CHECK (length(trim(wave_id)) > 0),
              CHECK (length(trim(worker_id)) > 0),
              CHECK (length(trim(resource_key)) > 0),
              CHECK (length(trim(token)) > 0),
              UNIQUE (token, resource_key)
            )
            """
        )
        columns = {
            row[1]
            for row in connection.execute(
                f"PRAGMA table_info({TABLE_NAME})"
            ).fetchall()
        }
        missing = sorted(_CORE_COLUMNS - columns)
        if missing:
            raise ResourceLeaseSchemaError(
                "incompatible resource_leases table; missing core columns: "
                + ", ".join(missing)
            )
        if "release_reason" not in columns:
            connection.execute(
                f"ALTER TABLE {TABLE_NAME} ADD COLUMN release_reason TEXT"
            )
        try:
            connection.execute(
                f"""
                CREATE UNIQUE INDEX IF NOT EXISTS
                  uq_resource_leases_active_resource
                ON {TABLE_NAME}(resource_key)
                WHERE released_at IS NULL
                """
            )
            connection.execute(
                f"""
                CREATE INDEX IF NOT EXISTS ix_resource_leases_token
                ON {TABLE_NAME}(token, released_at)
                """
            )
            connection.execute(
                f"""
                CREATE INDEX IF NOT EXISTS ix_resource_leases_owner
                ON {TABLE_NAME}(batch_id, run_id, wave_id, worker_id, released_at)
                """
            )
        except sqlite3.IntegrityError as exc:
            raise ResourceLeaseSchemaError(
                "existing resource leases contain duplicate active resource keys"
            ) from exc


def acquire_resources(
    db_path: str | os.PathLike[str],
    *,
    batch_id: str,
    run_id: str,
    wave_id: str,
    worker_id: str,
    resource_keys: Iterable[str],
    ttl_seconds: float,
    now: datetime | None = None,
    resume_token: str | None = None,
) -> LeaseBundle:
    """Atomically lease every key or none of them.

    ``BEGIN IMMEDIATE`` serializes contenders before conflict inspection.  An
    expired active row is released and can be taken over in the same commit.
    Re-entry is never implicit, even for identical owner fields.  A caller may
    explicitly resume only by presenting the exact original token, owner, and
    complete key set.
    """

    owner = _validated_owner(batch_id, run_id, wave_id, worker_id)
    keys = _validated_resource_keys(resource_keys)
    instant = _validated_now(now)
    now_text = _format_timestamp(instant)
    expires_text = _format_timestamp(instant + _validated_ttl(ttl_seconds))
    normalized_resume_token = None
    if resume_token is not None:
        normalized_resume_token = _normalized_label(resume_token, "resume_token")

    ensure_schema(db_path)
    connection = _connect(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        if normalized_resume_token is not None:
            bundle = _resume_bundle(
                connection,
                owner=owner,
                keys=keys,
                token=normalized_resume_token,
                heartbeat_at=now_text,
                expires_at=expires_text,
            )
            connection.commit()
            return bundle

        placeholders = ",".join("?" for _ in keys)
        # Expiration cleanup and the new inserts are one transaction.  If any
        # other requested resource conflicts, this cleanup is rolled back too.
        connection.execute(
            f"""
            UPDATE {TABLE_NAME}
               SET released_at=?, release_reason='expired_takeover'
             WHERE released_at IS NULL
               AND expires_at <= ?
               AND resource_key IN ({placeholders})
            """,
            (now_text, now_text, *keys),
        )
        rows = connection.execute(
            f"""
            SELECT resource_key,batch_id,run_id,wave_id,worker_id,expires_at
              FROM {TABLE_NAME}
             WHERE released_at IS NULL
               AND resource_key IN ({placeholders})
             ORDER BY resource_key
            """,
            keys,
        ).fetchall()
        if rows:
            raise ResourceLeaseConflict(
                [
                    ResourceConflict(
                        resource_key=row["resource_key"],
                        batch_id=row["batch_id"],
                        run_id=row["run_id"],
                        wave_id=row["wave_id"],
                        worker_id=row["worker_id"],
                        expires_at=row["expires_at"],
                    )
                    for row in rows
                ]
            )

        token = uuid.uuid4().hex
        acquired_at = now_text
        connection.executemany(
            f"""
            INSERT INTO {TABLE_NAME}(
              batch_id,run_id,wave_id,worker_id,resource_key,token,
              acquired_at,heartbeat_at,expires_at,released_at,release_reason
            ) VALUES (?,?,?,?,?,?,?,?,?,NULL,NULL)
            """,
            [
                (
                    *owner,
                    resource_key,
                    token,
                    acquired_at,
                    now_text,
                    expires_text,
                )
                for resource_key in keys
            ],
        )
        connection.commit()
        return LeaseBundle(
            batch_id=owner[0],
            run_id=owner[1],
            wave_id=owner[2],
            worker_id=owner[3],
            resource_keys=keys,
            token=token,
            acquired_at=acquired_at,
            heartbeat_at=now_text,
            expires_at=expires_text,
        )
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()


def heartbeat_resources(
    db_path: str | os.PathLike[str],
    bundle: LeaseBundle,
    *,
    ttl_seconds: float,
    now: datetime | None = None,
) -> LeaseBundle:
    """CAS-renew an entire bundle, rolling back if any member was lost."""

    _validate_bundle(bundle)
    instant = _validated_now(now)
    now_text = _format_timestamp(instant)
    expires_text = _format_timestamp(instant + _validated_ttl(ttl_seconds))
    ensure_schema(db_path)
    connection = _connect(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        failures = _ownership_failures(
            connection,
            bundle,
            now_text=now_text,
            expired_is_failure=True,
        )
        if failures:
            raise ResourceLeaseCASFailure("heartbeat", failures)
        changed = _cas_update(
            connection,
            bundle,
            "heartbeat_at=?, expires_at=?",
            (now_text, expires_text),
            extra_where="expires_at > ?",
            extra_params=(now_text,),
        )
        if changed != len(bundle.resource_keys):
            raise ResourceLeaseCASFailure(
                "heartbeat",
                [
                    LeaseCASFailure(resource_key, "database_changed")
                    for resource_key in bundle.resource_keys
                ],
            )
        connection.commit()
        return replace(bundle, heartbeat_at=now_text, expires_at=expires_text)
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()


def release_resources(
    db_path: str | os.PathLike[str],
    bundle: LeaseBundle,
    *,
    now: datetime | None = None,
    reason: str = "worker_release",
) -> int:
    """CAS-release an entire bundle, rolling back if any member was lost.

    An expired lease may still be released by its owner if nobody has taken it
    over.  Once takeover has marked the old row released, the stale token fails
    rather than touching the new owner's lease.
    """

    _validate_bundle(bundle)
    now_text = _format_timestamp(_validated_now(now))
    normalized_reason = _normalized_label(reason, "reason")
    ensure_schema(db_path)
    connection = _connect(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        failures = _ownership_failures(
            connection,
            bundle,
            now_text=now_text,
            expired_is_failure=False,
        )
        if failures:
            raise ResourceLeaseCASFailure("release", failures)
        changed = _cas_update(
            connection,
            bundle,
            "released_at=?, release_reason=?",
            (now_text, normalized_reason),
        )
        if changed != len(bundle.resource_keys):
            raise ResourceLeaseCASFailure(
                "release",
                [
                    LeaseCASFailure(resource_key, "database_changed")
                    for resource_key in bundle.resource_keys
                ],
            )
        connection.commit()
        return changed
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()


def _connect(db_path: str | os.PathLike[str]) -> sqlite3.Connection:
    raw_path = os.fspath(db_path)
    if not raw_path or not raw_path.strip():
        raise ResourceLeaseValidationError("db_path must be explicit and non-empty")
    connection = sqlite3.connect(raw_path, timeout=15, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=15000")
    return connection


def _normalized_label(value: object, field: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).strip()
    if not normalized:
        raise ResourceLeaseValidationError(f"{field} must be non-empty")
    if "\x00" in normalized:
        raise ResourceLeaseValidationError(f"{field} must not contain NUL")
    return normalized


def _validated_owner(
    batch_id: str,
    run_id: str,
    wave_id: str,
    worker_id: str,
) -> tuple[str, str, str, str]:
    return (
        _normalized_label(batch_id, "batch_id"),
        _normalized_label(run_id, "run_id"),
        _normalized_label(wave_id, "wave_id"),
        _normalized_label(worker_id, "worker_id"),
    )


def _validated_resource_keys(resource_keys: Iterable[str]) -> tuple[str, ...]:
    try:
        keys = tuple(
            _normalized_label(resource_key, "resource_key")
            for resource_key in resource_keys
        )
    except TypeError as exc:
        raise ResourceLeaseValidationError("resource_keys must be iterable") from exc
    if not keys:
        raise ResourceLeaseValidationError("at least one resource key is required")
    if len(set(keys)) != len(keys):
        raise ResourceLeaseValidationError("resource_keys contain duplicates")
    return tuple(sorted(keys))


def _validated_ttl(ttl_seconds: float) -> timedelta:
    if isinstance(ttl_seconds, bool):
        raise ResourceLeaseValidationError("ttl_seconds must be positive")
    try:
        value = float(ttl_seconds)
    except (TypeError, ValueError) as exc:
        raise ResourceLeaseValidationError("ttl_seconds must be positive") from exc
    if value <= 0 or value == float("inf") or value != value:
        raise ResourceLeaseValidationError("ttl_seconds must be finite and positive")
    return timedelta(seconds=value)


def _validated_now(now: datetime | None) -> datetime:
    instant = now or datetime.now(timezone.utc)
    if not isinstance(instant, datetime):
        raise ResourceLeaseValidationError("now must be a datetime")
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ResourceLeaseValidationError("now must be timezone-aware")
    return instant.astimezone(timezone.utc)


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")


def _validate_bundle(bundle: LeaseBundle) -> None:
    if not isinstance(bundle, LeaseBundle):
        raise ResourceLeaseValidationError("bundle must be a LeaseBundle")
    owner = _validated_owner(
        bundle.batch_id,
        bundle.run_id,
        bundle.wave_id,
        bundle.worker_id,
    )
    if owner != (
        bundle.batch_id,
        bundle.run_id,
        bundle.wave_id,
        bundle.worker_id,
    ):
        raise ResourceLeaseValidationError("bundle owner fields are not canonical")
    keys = _validated_resource_keys(bundle.resource_keys)
    if keys != bundle.resource_keys:
        raise ResourceLeaseValidationError("bundle resource keys are not canonical")
    _normalized_label(bundle.token, "token")
    for field in ("acquired_at", "heartbeat_at", "expires_at"):
        value = getattr(bundle, field)
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (AttributeError, TypeError, ValueError) as exc:
            raise ResourceLeaseValidationError(
                f"bundle {field} is not an ISO timestamp"
            ) from exc


def _resume_bundle(
    connection: sqlite3.Connection,
    *,
    owner: tuple[str, str, str, str],
    keys: tuple[str, ...],
    token: str,
    heartbeat_at: str,
    expires_at: str,
) -> LeaseBundle:
    rows = connection.execute(
        f"""
        SELECT batch_id,run_id,wave_id,worker_id,resource_key,token,
               acquired_at,heartbeat_at,expires_at,released_at
          FROM {TABLE_NAME}
         WHERE token=?
         ORDER BY resource_key
        """,
        (token,),
    ).fetchall()
    if not rows:
        raise ResourceLeaseResumeError("resume token does not exist")
    existing_keys = tuple(row["resource_key"] for row in rows)
    existing_owners = {
        (row["batch_id"], row["run_id"], row["wave_id"], row["worker_id"])
        for row in rows
    }
    if existing_keys != keys or existing_owners != {owner}:
        raise ResourceLeaseResumeError(
            "resume token does not match the exact owner and resource bundle"
        )
    released = [row["resource_key"] for row in rows if row["released_at"] is not None]
    if released:
        raise ResourceLeaseResumeError(
            "resume token is no longer active for: " + ", ".join(released)
        )
    changed = connection.execute(
        f"""
        UPDATE {TABLE_NAME}
           SET heartbeat_at=?, expires_at=?
         WHERE token=? AND released_at IS NULL
        """,
        (heartbeat_at, expires_at, token),
    ).rowcount
    if changed != len(keys):
        raise ResourceLeaseResumeError("resume token lost part of its bundle")
    return LeaseBundle(
        batch_id=owner[0],
        run_id=owner[1],
        wave_id=owner[2],
        worker_id=owner[3],
        resource_keys=keys,
        token=token,
        acquired_at=rows[0]["acquired_at"],
        heartbeat_at=heartbeat_at,
        expires_at=expires_at,
    )


def _ownership_failures(
    connection: sqlite3.Connection,
    bundle: LeaseBundle,
    *,
    now_text: str,
    expired_is_failure: bool,
) -> list[LeaseCASFailure]:
    failures: list[LeaseCASFailure] = []
    owner = (
        bundle.batch_id,
        bundle.run_id,
        bundle.wave_id,
        bundle.worker_id,
    )
    for resource_key in bundle.resource_keys:
        active = connection.execute(
            f"""
            SELECT batch_id,run_id,wave_id,worker_id,token,expires_at
              FROM {TABLE_NAME}
             WHERE resource_key=? AND released_at IS NULL
            """,
            (resource_key,),
        ).fetchone()
        if active is None:
            historical = connection.execute(
                f"""
                SELECT 1 FROM {TABLE_NAME}
                 WHERE resource_key=? AND token=? AND released_at IS NOT NULL
                 LIMIT 1
                """,
                (resource_key, bundle.token),
            ).fetchone()
            reason = "released" if historical is not None else "missing"
            failures.append(LeaseCASFailure(resource_key, reason))
            continue
        if active["token"] != bundle.token:
            failures.append(LeaseCASFailure(resource_key, "token_mismatch"))
            continue
        active_owner = (
            active["batch_id"],
            active["run_id"],
            active["wave_id"],
            active["worker_id"],
        )
        if active_owner != owner:
            failures.append(LeaseCASFailure(resource_key, "owner_mismatch"))
            continue
        if expired_is_failure and active["expires_at"] <= now_text:
            failures.append(LeaseCASFailure(resource_key, "expired"))
    return failures


def _cas_update(
    connection: sqlite3.Connection,
    bundle: LeaseBundle,
    set_clause: str,
    set_params: tuple[object, ...],
    *,
    extra_where: str = "",
    extra_params: tuple[object, ...] = (),
) -> int:
    placeholders = ",".join("?" for _ in bundle.resource_keys)
    owner = (
        bundle.batch_id,
        bundle.run_id,
        bundle.wave_id,
        bundle.worker_id,
    )
    suffix = f" AND {extra_where}" if extra_where else ""
    return connection.execute(
        f"""
        UPDATE {TABLE_NAME}
           SET {set_clause}
         WHERE token=?
           AND batch_id=? AND run_id=? AND wave_id=? AND worker_id=?
           AND released_at IS NULL
           AND resource_key IN ({placeholders})
           {suffix}
        """,
        (*set_params, bundle.token, *owner, *bundle.resource_keys, *extra_params),
    ).rowcount


__all__ = [
    "LeaseBundle",
    "LeaseCASFailure",
    "ResourceConflict",
    "ResourceLeaseCASFailure",
    "ResourceLeaseConflict",
    "ResourceLeaseError",
    "ResourceLeaseResumeError",
    "ResourceLeaseSchemaError",
    "ResourceLeaseValidationError",
    "SCHEMA_VERSION",
    "TABLE_NAME",
    "account_resource_key",
    "acquire_resources",
    "build_resource_keys",
    "chrome_profile_resource_key",
    "ensure_schema",
    "heartbeat_resources",
    "release_resources",
]
