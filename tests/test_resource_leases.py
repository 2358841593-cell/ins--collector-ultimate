"""The DAG resource lease database is atomic, global, and token-owned."""
from __future__ import annotations

import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from extensions.sop_v2.pipeline import resource_leases as leases  # noqa: E402


NOW = datetime(2026, 8, 10, 8, 0, tzinfo=timezone.utc)


def _keys(tmp_path: Path) -> tuple[str, str]:
    return (
        leases.account_resource_key("@Creator.One"),
        leases.chrome_profile_resource_key(tmp_path / "chrome-profile"),
    )


def _acquire(
    db: Path,
    resource_keys,
    *,
    now: datetime = NOW,
    batch_id: str = "SKIN6",
    run_id: str = "run-1",
    wave_id: str = "wave-1",
    worker_id: str = "worker-1",
    resume_token: str | None = None,
) -> leases.LeaseBundle:
    return leases.acquire_resources(
        db,
        batch_id=batch_id,
        run_id=run_id,
        wave_id=wave_id,
        worker_id=worker_id,
        resource_keys=resource_keys,
        ttl_seconds=60,
        now=now,
        resume_token=resume_token,
    )


def test_resource_key_helpers_casefold_accounts_and_resolve_profiles(tmp_path):
    assert (
        leases.account_resource_key("  @Straße  ")
        == leases.account_resource_key("STRASSE")
        == "account:instagram:strasse"
    )

    canonical = tmp_path / "profiles" / "worker-a"
    alias = tmp_path / "profiles" / "nested" / ".." / "worker-a"
    assert leases.chrome_profile_resource_key(alias) == (
        leases.chrome_profile_resource_key(canonical)
    )
    with pytest.raises(leases.ResourceLeaseValidationError, match="absolute"):
        leases.chrome_profile_resource_key("relative/profile")


def test_schema_is_explicit_idempotent_and_records_required_fields(tmp_path):
    db = tmp_path / "leases.db"

    leases.ensure_schema(db)
    leases.ensure_schema(db)

    with sqlite3.connect(db) as connection:
        columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(resource_leases)"
            ).fetchall()
        }
    assert {
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
    } <= columns

    with pytest.raises(TypeError):
        leases.ensure_schema()  # type: ignore[call-arg]


def test_compatible_legacy_schema_gets_only_additive_migration(tmp_path):
    db = tmp_path / "legacy.db"
    with sqlite3.connect(db) as connection:
        connection.execute(
            """
            CREATE TABLE resource_leases (
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
              UNIQUE(token, resource_key)
            )
            """
        )

    leases.ensure_schema(db)

    with sqlite3.connect(db) as connection:
        columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(resource_leases)"
            ).fetchall()
        }
    assert "release_reason" in columns


def test_incompatible_legacy_schema_fails_closed(tmp_path):
    db = tmp_path / "bad-legacy.db"
    with sqlite3.connect(db) as connection:
        connection.execute(
            "CREATE TABLE resource_leases(resource_key TEXT, token TEXT)"
        )

    with pytest.raises(leases.ResourceLeaseSchemaError, match="missing core"):
        leases.ensure_schema(db)


def test_acquire_is_bundle_atomic_on_cross_batch_conflict(tmp_path):
    db = tmp_path / "leases.db"
    account, profile = _keys(tmp_path)
    first = _acquire(db, [account], batch_id="SKIN6")

    with pytest.raises(leases.ResourceLeaseConflict) as caught:
        _acquire(
            db,
            [account, profile],
            batch_id="SKIN7",
            run_id="run-2",
            wave_id="wave-2",
            worker_id="pricing-worker",
        )

    assert caught.value.resource_keys == (account,)
    with sqlite3.connect(db) as connection:
        active = connection.execute(
            "SELECT resource_key,token,batch_id FROM resource_leases "
            "WHERE released_at IS NULL ORDER BY resource_key"
        ).fetchall()
    assert active == [(account, first.token, "SKIN6")]
    assert profile not in {row[0] for row in active}


def test_same_owner_reentry_conflicts_unless_resume_token_is_explicit(tmp_path):
    db = tmp_path / "leases.db"
    keys = _keys(tmp_path)
    original = _acquire(db, keys)

    with pytest.raises(leases.ResourceLeaseConflict):
        _acquire(db, keys, now=NOW + timedelta(seconds=1))

    resumed = _acquire(
        db,
        keys,
        now=NOW + timedelta(seconds=2),
        resume_token=original.token,
    )
    assert resumed.token == original.token
    assert resumed.acquired_at == original.acquired_at
    assert resumed.heartbeat_at > original.heartbeat_at
    assert resumed.expires_at > original.expires_at

    with pytest.raises(leases.ResourceLeaseResumeError, match="exact owner"):
        _acquire(
            db,
            [keys[0]],
            now=NOW + timedelta(seconds=3),
            resume_token=original.token,
        )


def test_expired_resources_can_be_taken_over_and_old_token_cannot_mutate(tmp_path):
    db = tmp_path / "leases.db"
    account = leases.account_resource_key("same_creator")
    old = _acquire(db, [account], batch_id="SKIN6")

    replacement = _acquire(
        db,
        [account],
        now=NOW + timedelta(seconds=61),
        batch_id="SKIN7",
        run_id="run-2",
        wave_id="wave-2",
        worker_id="worker-2",
    )

    assert replacement.token != old.token
    with sqlite3.connect(db) as connection:
        rows = connection.execute(
            "SELECT token,batch_id,released_at,release_reason "
            "FROM resource_leases WHERE resource_key=? ORDER BY lease_id",
            (account,),
        ).fetchall()
    assert rows[0][0:2] == (old.token, "SKIN6")
    assert rows[0][2] is not None
    assert rows[0][3] == "expired_takeover"
    assert rows[1] == (replacement.token, "SKIN7", None, None)

    with pytest.raises(leases.ResourceLeaseCASFailure) as heartbeat_failure:
        leases.heartbeat_resources(
            db,
            old,
            ttl_seconds=60,
            now=NOW + timedelta(seconds=62),
        )
    assert heartbeat_failure.value.resource_keys == (account,)


def test_heartbeat_is_bundle_cas_and_reports_only_failed_resource(tmp_path):
    db = tmp_path / "leases.db"
    keys = _keys(tmp_path)
    bundle = _acquire(db, keys)
    with sqlite3.connect(db) as connection:
        connection.execute(
            "UPDATE resource_leases SET token='other-token' WHERE resource_key=?",
            (keys[1],),
        )

    with pytest.raises(leases.ResourceLeaseCASFailure) as caught:
        leases.heartbeat_resources(
            db,
            bundle,
            ttl_seconds=60,
            now=NOW + timedelta(seconds=10),
        )

    assert caught.value.operation == "heartbeat"
    assert caught.value.resource_keys == (keys[1],)
    assert caught.value.failures[0].reason == "token_mismatch"
    with sqlite3.connect(db) as connection:
        heartbeats = dict(
            connection.execute(
                "SELECT resource_key,heartbeat_at FROM resource_leases"
            ).fetchall()
        )
    assert set(heartbeats.values()) == {bundle.heartbeat_at}


def test_release_is_bundle_cas_and_never_partially_releases(tmp_path):
    db = tmp_path / "leases.db"
    keys = _keys(tmp_path)
    bundle = _acquire(db, keys)
    with sqlite3.connect(db) as connection:
        connection.execute(
            "UPDATE resource_leases SET token='other-token' WHERE resource_key=?",
            (keys[1],),
        )

    with pytest.raises(leases.ResourceLeaseCASFailure) as caught:
        leases.release_resources(db, bundle, now=NOW + timedelta(seconds=5))

    assert caught.value.operation == "release"
    assert caught.value.resource_keys == (keys[1],)
    with sqlite3.connect(db) as connection:
        rows = connection.execute(
            "SELECT released_at FROM resource_leases"
        ).fetchall()
    assert rows == [(None,), (None,)]


def test_successful_heartbeat_and_release_update_every_bundle_member(tmp_path):
    db = tmp_path / "leases.db"
    bundle = _acquire(db, _keys(tmp_path))

    renewed = leases.heartbeat_resources(
        db,
        bundle,
        ttl_seconds=120,
        now=NOW + timedelta(seconds=10),
    )
    released = leases.release_resources(
        db,
        renewed,
        now=NOW + timedelta(seconds=20),
        reason="wave_complete",
    )

    assert renewed.expires_at > bundle.expires_at
    assert released == 2
    with sqlite3.connect(db) as connection:
        rows = connection.execute(
            "SELECT heartbeat_at,expires_at,released_at,release_reason "
            "FROM resource_leases"
        ).fetchall()
    assert len(rows) == 2
    assert all(row[0] == renewed.heartbeat_at for row in rows)
    assert all(row[1] == renewed.expires_at for row in rows)
    assert all(row[2] is not None and row[3] == "wave_complete" for row in rows)


def test_concurrent_process_equivalent_contenders_have_one_winner(tmp_path):
    db = tmp_path / "leases.db"
    account = leases.account_resource_key("contended_creator")
    leases.ensure_schema(db)

    def contend(worker_index: int):
        try:
            return _acquire(
                db,
                [account],
                worker_id=f"worker-{worker_index}",
            )
        except leases.ResourceLeaseConflict as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(contend, (1, 2)))

    assert sum(isinstance(result, leases.LeaseBundle) for result in results) == 1
    assert sum(
        isinstance(result, leases.ResourceLeaseConflict) for result in results
    ) == 1
    with sqlite3.connect(db) as connection:
        active_count = connection.execute(
            "SELECT count(*) FROM resource_leases WHERE released_at IS NULL"
        ).fetchone()[0]
    assert active_count == 1
