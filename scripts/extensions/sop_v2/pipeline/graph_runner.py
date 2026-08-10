"""Formal Stage 2/3 DAG runner.

The runner owns orchestration only.  Stage workers keep owning candidate claims and
browser collection.  Before a subprocess is started this module:

* validates the frozen shallow/deep account order and persistent profiles;
* proves B1, or (only with ``resume_from_stage3``) proves persisted B2 state;
* acquires bundle-atomic account/profile leases; and
* appends an immutable wave intent to an atomically-written schedule.

One Stage 2 producer may stream work to bounded Stage 3 consumer waves.  A temporary
empty ``qualified`` queue never closes the graph while that producer is alive.

Importing this module has no filesystem or database side effects.  All paths are
explicit in :class:`GraphConfig`, which also makes the orchestration testable with a
temporary database and fake subprocess factory.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import fcntl
import hashlib
import inspect
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import sys
from typing import Any, Awaitable, Callable, Iterable, Mapping, Sequence
from urllib.parse import unquote
import uuid

from extensions.sop_v2.pipeline import barriers, resource_leases


SCHEDULE_SCHEMA = "sop-v2-graph-schedule-v1"
EVENT_SCHEMA = "sop-v2-graph-event-v1"
EVENT_GENESIS_HASH = "0" * 64
ACCOUNT_ROTATION_POLICY = "consumer-wave-index-zero-based-within-slice-v1"
CONSUMER_CLAIM_SHARDING_POLICY = "eligible-qualified-even-split-capped-v1"
_USERNAME_RE = re.compile(r"^[A-Za-z0-9._]+$")
_PROFILE_LOCK_NAMES = ("SingletonLock", "SingletonSocket", "SingletonCookie")


class GraphRunnerError(RuntimeError):
    """Base class for fail-closed graph orchestration errors."""


class AccountPreflightError(GraphRunnerError, ValueError):
    """An account pool or Chrome profile cannot be used safely."""


class ScheduleError(GraphRunnerError):
    """The append-only wave schedule is absent, corrupt, or incompatible."""


class EventLogError(GraphRunnerError):
    """The append-only graph event chain is missing, corrupt, or inconsistent."""


class ResumeMismatchError(ScheduleError):
    """Frozen account order or run identity changed during resume."""


class BarrierFailed(GraphRunnerError):
    """A required DAG barrier did not pass."""

    def __init__(self, name: str, failures: Sequence[str] = ()) -> None:
        self.name = name
        self.failures = tuple(failures)
        detail = ", ".join(self.failures) if self.failures else "predicate returned false"
        super().__init__(f"{name} barrier failed: {detail}")


class SubprocessFailed(GraphRunnerError):
    """A stage subprocess returned non-zero; the exact code is retained."""

    def __init__(self, worker_id: str, command: Sequence[str], returncode: int) -> None:
        self.worker_id = worker_id
        self.command = tuple(command)
        self.returncode = int(returncode)
        super().__init__(f"{worker_id} exited with status {returncode}")


class GraphIncompleteError(GraphRunnerError):
    """A bounded run stopped before the qualified queue was drained."""


class ProcessTerminationError(GraphRunnerError):
    """A worker process group could not be confirmed dead after SIGKILL."""


@dataclass(frozen=True)
class AccountSpec:
    username: str
    profile_path: Path

    def as_dict(self) -> dict[str, str]:
        return {
            "username": self.username,
            "chrome_profile": str(self.profile_path),
        }


@dataclass(frozen=True)
class AccountPools:
    shallow_source: Path
    deep_source: Path
    profile_root: Path
    shallow: tuple[AccountSpec, ...]
    deep: tuple[AccountSpec, ...]
    shallow_order_sha256: str
    deep_order_sha256: str
    combined_order_sha256: str

    def schedule_identity(self) -> dict[str, Any]:
        return {
            "shallow": {
                "path": str(self.shallow_source),
                "parsed_account_count": len(self.shallow),
                "username_order_sha256": self.shallow_order_sha256,
            },
            "deep": {
                "path": str(self.deep_source),
                "parsed_account_count": len(self.deep),
                "username_order_sha256": self.deep_order_sha256,
            },
            "combined_order_sha256": self.combined_order_sha256,
            "profile_root": str(self.profile_root),
        }


@dataclass(frozen=True)
class WorkerSlice:
    worker_id: str
    limit: int
    account_offset: int
    account_count: int
    accounts: tuple[AccountSpec, ...]

    @property
    def usernames(self) -> tuple[str, ...]:
        return tuple(account.username for account in self.accounts)

    @property
    def profile_paths(self) -> tuple[Path, ...]:
        return tuple(account.profile_path for account in self.accounts)

    def as_dict(self, command: Sequence[str] | None = None) -> dict[str, Any]:
        value: dict[str, Any] = {
            "worker_id": self.worker_id,
            "limit": self.limit,
            "account_offset": self.account_offset,
            "account_count": self.account_count,
            "usernames": list(self.usernames),
            "chrome_profiles": [str(path) for path in self.profile_paths],
        }
        if command is not None:
            value["command"] = list(command)
        return value


@dataclass(frozen=True)
class QueueState:
    seed_count: int
    qualified_count: int
    qualified_unlocked_count: int
    collected_count: int = 0
    rejected_count: int = 0
    lock_count: int = 0

    def __post_init__(self) -> None:
        for name in (
            "seed_count",
            "qualified_count",
            "qualified_unlocked_count",
            "collected_count",
            "rejected_count",
            "lock_count",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.qualified_unlocked_count > self.qualified_count:
            raise ValueError("qualified_unlocked_count cannot exceed qualified_count")


@dataclass(frozen=True)
class GraphConfig:
    batch_id: str
    creator_db: Path
    lease_db: Path
    stage1_artifact: Path
    shallow_accounts_file: Path
    deep_accounts_file: Path
    profile_root: Path
    schedule_path: Path
    event_log_path: Path | None = None
    round_contract: Path | None = None
    worker_count: int = 2
    worker_limit: int = 24
    account_rotation_base: int = 0
    posts: int = 10
    max_consumer_waves: int = 100
    wave_cooldown_seconds: float = 120.0
    max_no_progress_waves: int = 3
    poll_interval_seconds: float = 2.0
    lease_ttl_seconds: float = 120.0
    heartbeat_interval_seconds: float = 30.0
    subprocess_spawn_timeout_seconds: float = 20.0
    termination_grace_seconds: float = 5.0
    termination_quarantine_seconds: float = 3600.0
    resume: bool = False
    resume_from_stage3: bool = False
    strict_completeness: bool = True
    translate_comments: bool = True
    translation_provider: str = "ollama"
    translation_model: str | None = "qwen3.5:4b"
    python_executable: str = sys.executable
    workspace_root: Path = field(
        default_factory=lambda: Path(__file__).resolve().parents[4]
    )
    run_id: str | None = None
    require_profile_directories: bool = True
    reject_active_profile_markers: bool = True


@dataclass(frozen=True)
class GraphRunResult:
    batch_id: str
    run_id: str
    producer_started: bool
    consumer_waves_started: int
    schedule_path: Path
    event_log_path: Path
    final_queue: QueueState
    b1: Any = None
    b2: Any = None


ProcessFactory = Callable[..., Awaitable[Any]]
QueueProbe = Callable[[], QueueState | Awaitable[QueueState]]
BarrierEvaluator = Callable[[], Any]
Sleep = Callable[[float], Awaitable[Any]]
EventEmitter = Callable[[str, str, Mapping[str, Any]], Any]


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _nonempty_label(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value.strip()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def username_order_sha256(usernames: Iterable[str]) -> str:
    """Hash exact parsed usernames in file order, with no trailing newline."""

    normalized: list[str] = []
    for username in usernames:
        value = _nonempty_label(username, "username")
        normalized.append(value)
    return hashlib.sha256("\n".join(normalized).encode("utf-8")).hexdigest()


def combined_account_order_sha256(
    shallow_usernames: Iterable[str], deep_usernames: Iterable[str]
) -> str:
    """Bind both ordered pools and their roles into one resume fingerprint."""

    payload = {
        "deep": list(deep_usernames),
        "shallow": list(shallow_usernames),
    }
    raw = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def parse_account_usernames(path: str | os.PathLike[str]) -> tuple[str, ...]:
    """Parse the same usable records as ``browser_collect_v2.load_accounts``.

    Only the username and cookie *names* are inspected.  Cookie values, passwords,
    TOTP seeds, and proxy credentials never enter the returned value or schedule.
    """

    source = Path(path).expanduser().resolve()
    try:
        lines = source.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise AccountPreflightError(f"account file is not readable: {source}") from exc

    usernames: list[str] = []
    identities: set[str] = set()
    for line_number, raw in enumerate(lines, 1):
        if not raw.strip():
            continue
        parts = raw.strip().split("|")
        cookie = parts[3] if len(parts) > 3 else ""
        session_id = data_user_id = ""
        # Keep this deliberately line-for-line equivalent to
        # browser_collect_v2.load_accounts: last matching cookie wins,
        # sessionid is URL-decoded, and both resulting values must be truthy.
        for raw_cookie in cookie.split(";"):
            selected = raw_cookie.strip()
            if selected.startswith("sessionid="):
                session_id = unquote(selected[len("sessionid=") :])
            elif selected.startswith("ds_user_id="):
                data_user_id = selected[len("ds_user_id=") :]
        if not session_id or not data_user_id:
            continue
        username = parts[0].strip()
        if not username or not _USERNAME_RE.fullmatch(username):
            raise AccountPreflightError(
                f"invalid username at {source}:{line_number}"
            )
        identity = username.casefold()
        if identity in identities:
            raise AccountPreflightError(
                f"duplicate account (case-insensitive) in {source}: {username}"
            )
        identities.add(identity)
        usernames.append(username)
    if not usernames:
        raise AccountPreflightError(f"account pool has no usable records: {source}")
    return tuple(usernames)


def _profile_has_active_marker(profile: Path) -> str | None:
    for name in _PROFILE_LOCK_NAMES:
        marker = profile / name
        if os.path.lexists(marker):
            return name
    return None


def _account_specs(
    usernames: Sequence[str],
    profile_root: Path,
    *,
    require_profile_directories: bool,
    reject_active_profile_markers: bool,
) -> tuple[AccountSpec, ...]:
    accounts: list[AccountSpec] = []
    seen_profiles: set[str] = set()
    for username in usernames:
        profile = (profile_root / username).resolve(strict=False)
        if require_profile_directories and not profile.is_dir():
            raise AccountPreflightError(
                f"Chrome profile directory is missing for {username}: {profile}"
            )
        if reject_active_profile_markers:
            marker = _profile_has_active_marker(profile)
            if marker:
                raise AccountPreflightError(
                    f"Chrome profile appears active for {username}: {profile / marker}"
                )
        profile_key = os.path.normcase(str(profile))
        if profile_key in seen_profiles:
            raise AccountPreflightError(f"aliased Chrome profile: {profile}")
        seen_profiles.add(profile_key)
        accounts.append(AccountSpec(username=username, profile_path=profile))
    return tuple(accounts)


def preflight_account_pools(
    shallow_accounts_file: str | os.PathLike[str],
    deep_accounts_file: str | os.PathLike[str],
    profile_root: str | os.PathLike[str],
    *,
    require_profile_directories: bool = True,
    reject_active_profile_markers: bool = True,
) -> AccountPools:
    """Resolve and prove account/Profile isolation before any browser process."""

    shallow_source = Path(shallow_accounts_file).expanduser().resolve()
    deep_source = Path(deep_accounts_file).expanduser().resolve()
    profiles = Path(profile_root).expanduser().resolve()
    if require_profile_directories and not profiles.is_dir():
        raise AccountPreflightError(f"Chrome profile root is missing: {profiles}")

    shallow_names = parse_account_usernames(shallow_source)
    deep_names = parse_account_usernames(deep_source)
    overlap = {name.casefold() for name in shallow_names} & {
        name.casefold() for name in deep_names
    }
    if overlap:
        raise AccountPreflightError(
            "shallow/deep account pools overlap: " + ", ".join(sorted(overlap))
        )

    shallow = _account_specs(
        shallow_names,
        profiles,
        require_profile_directories=require_profile_directories,
        reject_active_profile_markers=reject_active_profile_markers,
    )
    deep = _account_specs(
        deep_names,
        profiles,
        require_profile_directories=require_profile_directories,
        reject_active_profile_markers=reject_active_profile_markers,
    )
    shallow_profiles = {os.path.normcase(str(item.profile_path)) for item in shallow}
    deep_profiles = {os.path.normcase(str(item.profile_path)) for item in deep}
    profile_overlap = shallow_profiles & deep_profiles
    if profile_overlap:
        raise AccountPreflightError(
            "shallow/deep Chrome profiles overlap: "
            + ", ".join(sorted(profile_overlap))
        )

    return AccountPools(
        shallow_source=shallow_source,
        deep_source=deep_source,
        profile_root=profiles,
        shallow=shallow,
        deep=deep,
        shallow_order_sha256=username_order_sha256(shallow_names),
        deep_order_sha256=username_order_sha256(deep_names),
        combined_order_sha256=combined_account_order_sha256(
            shallow_names, deep_names
        ),
    )


def build_worker_slices(
    accounts: Sequence[AccountSpec],
    *,
    worker_count: int,
    limit: int | Sequence[int],
    worker_prefix: str = "stage3-w",
) -> tuple[WorkerSlice, ...]:
    """Split the frozen deep pool into deterministic contiguous exclusive slices."""

    count = _positive_int(worker_count, "worker_count")
    if count > len(accounts):
        raise ValueError("worker_count cannot exceed deep account count")
    if not accounts:
        raise ValueError("deep account pool must not be empty")
    if isinstance(limit, Sequence) and not isinstance(limit, (str, bytes)):
        limits = tuple(_positive_int(value, "worker limit") for value in limit)
        if len(limits) != count:
            raise ValueError("worker limit list must match worker_count")
    else:
        limits = (_positive_int(limit, "worker limit"),) * count

    quotient, remainder = divmod(len(accounts), count)
    result: list[WorkerSlice] = []
    offset = 0
    for index in range(count):
        account_count = quotient + int(index < remainder)
        selected = tuple(accounts[offset : offset + account_count])
        result.append(
            WorkerSlice(
                worker_id=f"{worker_prefix}{index + 1}",
                limit=limits[index],
                account_offset=offset,
                account_count=account_count,
                accounts=selected,
            )
        )
        offset += account_count
    validate_worker_slices(result)
    return tuple(result)


def validate_worker_slices(workers: Sequence[WorkerSlice]) -> None:
    """Fail closed for unbounded, empty, overlapping, or aliased workers."""

    if not workers:
        raise ValueError("at least one Stage 3 worker is required")
    worker_ids: set[str] = set()
    accounts: set[str] = set()
    profiles: set[str] = set()
    intervals: list[tuple[int, int, str]] = []
    for worker in workers:
        _positive_int(worker.limit, f"{worker.worker_id}.limit")
        _positive_int(worker.account_count, f"{worker.worker_id}.account_count")
        if worker.account_offset < 0:
            raise ValueError(f"{worker.worker_id}.account_offset must be non-negative")
        if worker.account_count != len(worker.accounts):
            raise ValueError(f"{worker.worker_id} account_count does not match slice")
        if worker.worker_id in worker_ids:
            raise ValueError(f"duplicate worker_id: {worker.worker_id}")
        worker_ids.add(worker.worker_id)
        interval = (
            worker.account_offset,
            worker.account_offset + worker.account_count,
            worker.worker_id,
        )
        for start, end, other in intervals:
            if interval[0] < end and start < interval[1]:
                raise ValueError(f"account index slices overlap: {other}/{worker.worker_id}")
        intervals.append(interval)
        for account in worker.accounts:
            identity = account.username.casefold()
            profile = os.path.normcase(str(account.profile_path.resolve(strict=False)))
            if identity in accounts:
                raise ValueError(f"account slices overlap: {account.username}")
            if profile in profiles:
                raise ValueError(f"Chrome profile slices overlap: {profile}")
            accounts.add(identity)
            profiles.add(profile)


def plan_consumer_wave_workers(
    workers: Sequence[WorkerSlice],
    *,
    eligible_qualified_count: int,
    configured_worker_limit: int,
) -> tuple[WorkerSlice, ...]:
    """Return deterministic active workers with an even, bounded claim cap.

    The input slices are the run-contract workers and retain their frozen account
    and Chrome Profile ownership.  Only the per-wave ``limit`` changes.  Every
    positive wave keeps the whole frozen worker/account pool leased and active for
    cross-run isolation.  When fewer rows than workers remain, every worker gets
    the minimum safe cap of one; atomic queue claims decide which workers receive a
    row, without ever using a zero that a worker CLI could interpret as unbounded.
    """

    validate_worker_slices(workers)
    eligible = _nonnegative_int(
        eligible_qualified_count, "eligible_qualified_count"
    )
    worker_limit = _positive_int(configured_worker_limit, "configured_worker_limit")
    if any(worker.limit != worker_limit for worker in workers):
        raise ValueError("configured worker slices do not match worker_limit")

    planned_claim_count = min(eligible, len(workers) * worker_limit)
    if planned_claim_count == 0:
        return ()

    if planned_claim_count < len(workers):
        caps = (1,) * len(workers)
    else:
        quotient, remainder = divmod(planned_claim_count, len(workers))
        caps = tuple(
            quotient + int(index < remainder) for index in range(len(workers))
        )
    if any(cap <= 0 or cap > worker_limit for cap in caps):
        raise GraphRunnerError("computed consumer claim cap is outside configured bounds")
    if max(caps) - min(caps) > 1 or sum(caps) < planned_claim_count:
        raise GraphRunnerError("computed consumer claim caps are not a balanced cover")

    return tuple(
        replace(worker, limit=caps[index])
        for index, worker in enumerate(workers)
    )


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _event_log_path(config: GraphConfig) -> Path:
    if config.event_log_path is not None:
        return Path(config.event_log_path).expanduser().resolve()
    return Path(config.schedule_path).expanduser().resolve().with_name(
        "graph_events.jsonl"
    )


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _valid_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _event_hash(event_without_hash: Mapping[str, Any]) -> str:
    return _sha256_json(event_without_hash)


def _read_event_log_unlocked(path: Path) -> tuple[dict[str, Any], ...]:
    if not path.exists():
        return ()
    try:
        raw_lines = path.read_bytes().splitlines()
    except OSError as exc:
        raise EventLogError(f"event log is not readable: {path}") from exc
    events: list[dict[str, Any]] = []
    previous_hash = EVENT_GENESIS_HASH
    for index, raw_line in enumerate(raw_lines, 1):
        if not raw_line.strip():
            raise EventLogError(f"blank event line at seq {index}")
        try:
            event = json.loads(raw_line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EventLogError(f"invalid event JSON at seq {index}") from exc
        if not isinstance(event, dict):
            raise EventLogError(f"event at seq {index} must be an object")
        if event.get("schema") != EVENT_SCHEMA:
            raise EventLogError(f"event schema mismatch at seq {index}")
        if event.get("seq") != index:
            raise EventLogError(f"event sequence is not contiguous at seq {index}")
        if event.get("prev_hash") != previous_hash:
            raise EventLogError(f"event previous hash mismatch at seq {index}")
        actual_hash = event.get("event_hash")
        if not _valid_sha256(actual_hash):
            raise EventLogError(f"event hash is invalid at seq {index}")
        unsigned = dict(event)
        unsigned.pop("event_hash", None)
        expected_hash = _event_hash(unsigned)
        if actual_hash != expected_hash:
            raise EventLogError(f"event hash mismatch at seq {index}")
        for label in ("run_id", "batch_id", "wave_id", "worker_id", "timestamp"):
            try:
                _nonempty_label(event.get(label), f"event.{label}")
            except ValueError as exc:
                raise EventLogError(f"invalid {label} at seq {index}") from exc
        try:
            datetime.fromisoformat(str(event["timestamp"]).replace("Z", "+00:00"))
        except ValueError as exc:
            raise EventLogError(f"invalid timestamp at seq {index}") from exc
        if not isinstance(event.get("payload"), dict):
            raise EventLogError(f"event payload must be an object at seq {index}")
        previous_hash = actual_hash
        events.append(event)
    return tuple(events)


def validate_graph_event_log(
    path: str | os.PathLike[str],
    *,
    run_id: str | None = None,
    batch_id: str | None = None,
) -> tuple[dict[str, Any], ...]:
    """Validate every event and hash link under the same lock used by appends."""

    event_path = Path(path).expanduser().resolve()
    event_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = event_path.with_name(event_path.name + ".lock")
    with open(lock_path, "a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_SH)
        events = _read_event_log_unlocked(event_path)
    for event in events:
        if run_id is not None and event["run_id"] != run_id:
            raise EventLogError("event log contains a different run_id")
        if batch_id is not None and event["batch_id"] != batch_id:
            raise EventLogError("event log contains a different batch_id")
    return events


def append_graph_event(
    path: str | os.PathLike[str],
    *,
    run_id: str,
    batch_id: str,
    wave_id: str,
    worker_id: str,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Append one fsynced hash-chained JSONL record without rewriting history."""

    event_path = Path(path).expanduser().resolve()
    event_path.parent.mkdir(parents=True, exist_ok=True)
    selected_run = _nonempty_label(run_id, "run_id")
    selected_batch = _nonempty_label(batch_id, "batch_id")
    selected_wave = _nonempty_label(wave_id, "wave_id")
    selected_worker = _nonempty_label(worker_id, "worker_id")
    if not isinstance(payload, Mapping):
        raise EventLogError("event payload must be an object")
    try:
        frozen_payload = json.loads(_canonical_json(payload))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise EventLogError("event payload is not JSON serializable") from exc
    if not isinstance(frozen_payload, dict):
        raise EventLogError("event payload must serialize to an object")

    lock_path = event_path.with_name(event_path.name + ".lock")
    with open(lock_path, "a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        events = _read_event_log_unlocked(event_path)
        if events:
            if events[0]["run_id"] != selected_run:
                raise EventLogError("cannot append a different run_id")
            if events[0]["batch_id"] != selected_batch:
                raise EventLogError("cannot append a different batch_id")
        unsigned = {
            "schema": EVENT_SCHEMA,
            "seq": len(events) + 1,
            "prev_hash": events[-1]["event_hash"] if events else EVENT_GENESIS_HASH,
            "run_id": selected_run,
            "batch_id": selected_batch,
            "wave_id": selected_wave,
            "worker_id": selected_worker,
            "timestamp": _utc_now(),
            "payload": frozen_payload,
        }
        event = {**unsigned, "event_hash": _event_hash(unsigned)}
        line = (_canonical_json(event) + "\n").encode("utf-8")
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        descriptor = os.open(event_path, flags, 0o600)
        try:
            written = 0
            while written < len(line):
                count = os.write(descriptor, line[written:])
                if count <= 0:
                    raise EventLogError("event append made no progress")
                written += count
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        directory_fd = os.open(event_path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        persisted = _read_event_log_unlocked(event_path)
        if not persisted or persisted[-1] != event:
            raise EventLogError("event append verification failed")
        return event


def _wave_sha256(wave: Mapping[str, Any]) -> str:
    return _sha256_json(wave)


def validate_schedule_event_coverage(
    schedule: Mapping[str, Any], events: Sequence[Mapping[str, Any]]
) -> None:
    """Bind every immutable wave to exactly one prior intent event."""

    intents: dict[str, list[Mapping[str, Any]]] = {}
    for event in events:
        payload = event.get("payload")
        if isinstance(payload, Mapping) and payload.get("state") == "intent_persisted":
            intents.setdefault(str(event.get("wave_id")), []).append(event)
    for wave in schedule.get("waves", []):
        wave_id = str(wave.get("wave_id"))
        matches = intents.get(wave_id, [])
        if len(matches) != 1:
            raise EventLogError(
                f"wave {wave_id} must have exactly one persisted intent event"
            )
        if matches[0]["payload"].get("wave_sha256") != _wave_sha256(wave):
            raise EventLogError(f"wave {wave_id} intent fingerprint mismatch")


def consecutive_no_progress_waves(events: Sequence[Mapping[str, Any]]) -> int:
    """Recover the trailing no-progress counter from immutable wave results."""

    count = 0
    for event in events:
        payload = event.get("payload")
        if not isinstance(payload, Mapping) or payload.get("state") != "wave_result":
            continue
        delta = payload.get("collected_delta")
        if isinstance(delta, bool) or not isinstance(delta, int):
            raise EventLogError("wave_result.collected_delta must be an integer")
        count = count + 1 if delta <= 0 else 0
    return count


def _file_fingerprint(path: Path | None) -> dict[str, str] | None:
    if path is None:
        return None
    resolved = Path(path).expanduser().resolve()
    try:
        digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
    except OSError:
        digest = "missing"
    return {"path": str(resolved), "sha256": digest}


def build_run_contract(
    config: GraphConfig,
    pools: AccountPools,
    workers: Sequence[WorkerSlice],
) -> dict[str, Any]:
    """Freeze every semantic input that must not drift between resume waves."""

    validate_worker_slices(workers)
    workspace = Path(config.workspace_root).expanduser().resolve()
    worker_code_paths = (
        workspace / "scripts/extensions/sop_v2/pipeline/stage2_qualify.py",
        workspace / "scripts/extensions/sop_v2/pipeline/stage3_collect.py",
        workspace / "scripts/extensions/sop_v2/pipeline/_base.py",
        workspace / "scripts/extensions/sop_v2/pipeline/deep_attempts.py",
        workspace / "scripts/extensions/sop_v2/pipeline/deep_evidence_merge.py",
        workspace / "scripts/extensions/sop_v2/creator_cache.py",
        workspace / "scripts/extensions/sop_v2/comment_translation.py",
        workspace / "scripts/extensions/sop_v2/comments.py",
        workspace / "scripts/extensions/sop_v2/content.py",
        workspace / "scripts/extensions/sop_v2/pricing.py",
        workspace / "scripts/extensions/sop_v2/storefront.py",
        workspace / "scripts/extensions/sop_v2/token_cost.py",
        workspace / "scripts/extensions/sop_v2/config.py",
        workspace / "scripts/browser_collect_v2.py",
        workspace / "config/sop_v2.toml",
    )
    value = {
        "schema_version": 1,
        "creator_db": str(Path(config.creator_db).expanduser().resolve()),
        "lease_db": str(Path(config.lease_db).expanduser().resolve()),
        # Freeze identity/location, never the changing append-only content hash.
        "event_log_path": str(_event_log_path(config)),
        "stage1_artifact": _file_fingerprint(config.stage1_artifact),
        "round_contract": _file_fingerprint(config.round_contract),
        "account_order_sha256": pools.combined_order_sha256,
        "pipeline": {
            "posts": config.posts,
            "strict_completeness": config.strict_completeness,
            "translate_comments": config.translate_comments,
            "translation_provider": config.translation_provider,
            "translation_model": config.translation_model,
            "account_rotation_policy": ACCOUNT_ROTATION_POLICY,
            "account_rotation_base": config.account_rotation_base,
            "consumer_claim_sharding_policy": CONSUMER_CLAIM_SHARDING_POLICY,
            "max_consumer_waves": config.max_consumer_waves,
            "wave_cooldown_seconds": config.wave_cooldown_seconds,
            "max_no_progress_waves": config.max_no_progress_waves,
        },
        "workers": [
            {
                "worker_id": worker.worker_id,
                "limit": worker.limit,
                "account_offset": worker.account_offset,
                "account_count": worker.account_count,
                "usernames": list(worker.usernames),
                "chrome_profiles": [str(path) for path in worker.profile_paths],
            }
            for worker in workers
        ],
        "python_executable": str(Path(config.python_executable).expanduser().resolve()),
        "workspace_root": str(workspace),
        "runner_code": _file_fingerprint(Path(__file__)),
        "worker_code": {
            str(path.relative_to(workspace)): _file_fingerprint(path)["sha256"]
            for path in worker_code_paths
        },
    }
    # JSON round-trip guarantees only stable, serializable values reach the schedule.
    return json.loads(_canonical_json(value))


def run_contract_sha256(contract: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(contract).encode("utf-8")).hexdigest()


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with open(temporary, "xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _new_schedule(
    batch_id: str,
    run_id: str,
    pools: AccountPools,
    run_contract: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    frozen_contract = (
        json.loads(_canonical_json(run_contract))
        if run_contract is not None
        else {
            "schema_version": 1,
            "account_order_sha256": pools.combined_order_sha256,
        }
    )
    return {
        "schema": SCHEDULE_SCHEMA,
        "batch_id": _nonempty_label(batch_id, "batch_id"),
        "run_id": _nonempty_label(run_id, "run_id"),
        "created_at": _utc_now(),
        "account_identity": pools.schedule_identity(),
        "run_contract": frozen_contract,
        "run_contract_sha256": run_contract_sha256(frozen_contract),
        "waves": [],
    }


def load_schedule(path: str | os.PathLike[str]) -> dict[str, Any]:
    schedule_path = Path(path).expanduser().resolve()
    try:
        value = json.loads(schedule_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ScheduleError(f"schedule is not readable JSON: {schedule_path}") from exc
    if not isinstance(value, dict) or value.get("schema") != SCHEDULE_SCHEMA:
        raise ScheduleError(f"unsupported schedule schema: {schedule_path}")
    if not isinstance(value.get("waves"), list):
        raise ScheduleError("schedule.waves must be a list")
    _nonempty_label(value.get("batch_id"), "schedule.batch_id")
    _nonempty_label(value.get("run_id"), "schedule.run_id")
    run_contract = value.get("run_contract")
    if not isinstance(run_contract, Mapping):
        raise ScheduleError("schedule.run_contract must be an object")
    if value.get("run_contract_sha256") != run_contract_sha256(run_contract):
        raise ScheduleError("schedule run contract fingerprint is invalid")
    wave_ids: set[str] = set()
    consumer_wave_ordinal = 0
    for wave in value["waves"]:
        if not isinstance(wave, Mapping):
            raise ScheduleError("schedule wave must be an object")
        _validate_wave_record(wave)
        if wave.get("kind") == "stage3_consumers":
            _validate_consumer_wave_run_contract(
                wave,
                run_contract,
                consumer_wave_ordinal=consumer_wave_ordinal,
            )
            consumer_wave_ordinal += 1
        wave_id = str(wave["wave_id"])
        if wave_id in wave_ids:
            raise ScheduleError(f"duplicate historical wave_id: {wave_id}")
        wave_ids.add(wave_id)
    return value


def _assert_schedule_identity(
    schedule: Mapping[str, Any],
    *,
    batch_id: str,
    run_id: str,
    pools: AccountPools,
    run_contract: Mapping[str, Any] | None = None,
) -> None:
    if schedule.get("batch_id") != batch_id:
        raise ResumeMismatchError("schedule batch_id does not match")
    if schedule.get("run_id") != run_id:
        raise ResumeMismatchError("schedule run_id does not match")
    identity = schedule.get("account_identity")
    if not isinstance(identity, Mapping):
        raise ScheduleError("schedule.account_identity must be an object")
    expected = pools.schedule_identity()
    for pool_name in ("shallow", "deep"):
        actual_pool = identity.get(pool_name)
        if not isinstance(actual_pool, Mapping):
            raise ScheduleError(f"schedule account identity missing {pool_name}")
        if actual_pool.get("username_order_sha256") != expected[pool_name][
            "username_order_sha256"
        ]:
            raise ResumeMismatchError(f"{pool_name} account order SHA changed")
        if actual_pool.get("parsed_account_count") != expected[pool_name][
            "parsed_account_count"
        ]:
            raise ResumeMismatchError(f"{pool_name} account count changed")
    if identity.get("combined_order_sha256") != pools.combined_order_sha256:
        raise ResumeMismatchError("combined account order SHA changed")
    if identity.get("profile_root") != str(pools.profile_root):
        raise ResumeMismatchError("Chrome profile root changed")
    if run_contract is not None:
        actual_sha = schedule.get("run_contract_sha256")
        expected_sha = run_contract_sha256(run_contract)
        if actual_sha != expected_sha:
            raise ResumeMismatchError(
                "run contract changed (DB/artifact/options/code/worker slices)"
            )


def _validate_wave_record(wave: Mapping[str, Any]) -> None:
    _nonempty_label(wave.get("wave_id"), "wave.wave_id")
    kind = wave.get("kind")
    if kind == "stage2_producer":
        producer = wave.get("producer")
        if not isinstance(producer, Mapping):
            raise ScheduleError("producer wave must contain one producer")
        _nonempty_label(producer.get("worker_id"), "producer.worker_id")
        command = producer.get("command")
        if not isinstance(command, list) or not command:
            raise ScheduleError("producer command must be a non-empty list")
        return
    if kind != "stage3_consumers":
        raise ScheduleError(f"unsupported wave kind: {kind}")
    workers = wave.get("workers")
    if not isinstance(workers, list) or not workers:
        raise ScheduleError("consumer wave must contain workers")
    claim_plan = wave.get("claim_plan")
    if not isinstance(claim_plan, Mapping):
        raise ScheduleError("consumer wave must contain a claim_plan")
    if claim_plan.get("policy") != CONSUMER_CLAIM_SHARDING_POLICY:
        raise ScheduleError("consumer claim sharding policy mismatch")
    try:
        eligible_count = _nonnegative_int(
            claim_plan.get("eligible_qualified_count"),
            "claim_plan.eligible_qualified_count",
        )
        planned_count = _positive_int(
            claim_plan.get("planned_claim_count"),
            "claim_plan.planned_claim_count",
        )
        allocated_capacity = _positive_int(
            claim_plan.get("allocated_claim_capacity"),
            "claim_plan.allocated_claim_capacity",
        )
        configured_limit = _positive_int(
            claim_plan.get("configured_worker_limit"),
            "claim_plan.configured_worker_limit",
        )
    except ValueError as exc:
        raise ScheduleError(str(exc)) from exc
    configured_worker_ids = claim_plan.get("configured_worker_ids")
    if not isinstance(configured_worker_ids, list) or not configured_worker_ids:
        raise ScheduleError("claim_plan.configured_worker_ids must be non-empty")
    try:
        configured_worker_ids = [
            _nonempty_label(worker_id, "configured worker_id")
            for worker_id in configured_worker_ids
        ]
    except ValueError as exc:
        raise ScheduleError(str(exc)) from exc
    if len(set(configured_worker_ids)) != len(configured_worker_ids):
        raise ScheduleError("claim_plan configured worker_ids must be unique")

    account_names: set[str] = set()
    profiles: set[str] = set()
    intervals: list[tuple[int, int]] = []
    worker_ids: list[str] = []
    worker_limits: list[int] = []
    worker_rotations: list[int] = []
    for raw in workers:
        if not isinstance(raw, Mapping):
            raise ScheduleError("worker entry must be an object")
        try:
            worker_id = _nonempty_label(raw.get("worker_id"), "worker.worker_id")
            limit = _positive_int(raw.get("limit"), "worker.limit")
            account_count = _positive_int(
                raw.get("account_count"), "worker.account_count"
            )
        except ValueError as exc:
            raise ScheduleError(str(exc)) from exc
        if worker_id in worker_ids:
            raise ScheduleError("consumer worker_ids must be unique")
        worker_ids.append(worker_id)
        worker_limits.append(limit)
        offset = raw.get("account_offset")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ScheduleError("worker.account_offset must be non-negative")
        usernames = raw.get("usernames")
        chrome_profiles = raw.get("chrome_profiles")
        command = raw.get("command")
        if not isinstance(usernames, list) or len(usernames) != account_count:
            raise ScheduleError("worker usernames do not match account_count")
        if not isinstance(chrome_profiles, list) or len(chrome_profiles) != account_count:
            raise ScheduleError("worker profiles do not match account_count")
        if not isinstance(command, list) or not command:
            raise ScheduleError("worker command must be a non-empty list")
        for option, expected in (
            ("--limit", limit),
            ("--account-offset", offset),
            ("--account-count", account_count),
        ):
            if command.count(option) != 1:
                raise ScheduleError(
                    f"worker command must contain exactly one {option}"
                )
            value_index = command.index(option) + 1
            if value_index >= len(command) or command[value_index] != str(expected):
                raise ScheduleError(
                    f"worker command {option} does not match frozen worker value"
                )
        if command.count("--account-rotation") != 1:
            raise ScheduleError(
                "worker command must contain exactly one --account-rotation"
            )
        rotation_index = command.index("--account-rotation") + 1
        try:
            rotation = _nonnegative_int(
                int(command[rotation_index]), "worker command account rotation"
            )
        except (IndexError, TypeError, ValueError) as exc:
            raise ScheduleError(
                "worker command account rotation must be a non-negative integer"
            ) from exc
        if command[rotation_index] != str(rotation):
            raise ScheduleError("worker command account rotation is not canonical")
        worker_rotations.append(rotation)
        interval = (offset, offset + account_count)
        if any(interval[0] < end and start < interval[1] for start, end in intervals):
            raise ScheduleError("consumer account index slices overlap")
        intervals.append(interval)
        for username in usernames:
            identity = _nonempty_label(username, "worker username").casefold()
            if identity in account_names:
                raise ScheduleError("consumer account slices overlap")
            account_names.add(identity)
        for profile in chrome_profiles:
            key = resource_leases.chrome_profile_resource_key(str(profile))
            if key in profiles:
                raise ScheduleError("consumer Chrome profile slices overlap")
            profiles.add(key)

    expected_planned = min(
        eligible_count, len(configured_worker_ids) * configured_limit
    )
    if planned_count != expected_planned:
        raise ScheduleError("claim_plan planned count does not cover eligible work")
    expected_active_count = len(configured_worker_ids)
    expected_active_ids = configured_worker_ids
    active_worker_ids = claim_plan.get("active_worker_ids")
    if active_worker_ids != expected_active_ids or worker_ids != expected_active_ids:
        raise ScheduleError("consumer active worker set is not deterministic")
    if planned_count < expected_active_count:
        expected_limits = [1] * expected_active_count
    else:
        quotient, remainder = divmod(planned_count, expected_active_count)
        expected_limits = [
            quotient + int(index < remainder)
            for index in range(expected_active_count)
        ]
    if worker_limits != expected_limits:
        raise ScheduleError("consumer claim caps are not the deterministic even split")
    if any(limit > configured_limit for limit in worker_limits):
        raise ScheduleError("consumer claim cap exceeds configured worker limit")
    expected_caps = [
        {"worker_id": worker_id, "limit": limit}
        for worker_id, limit in zip(worker_ids, worker_limits)
    ]
    if claim_plan.get("claim_caps") != expected_caps:
        raise ScheduleError("claim_plan claim_caps do not match workers")
    if allocated_capacity != sum(expected_limits):
        raise ScheduleError("claim_plan allocated capacity does not match workers")
    if len(set(worker_rotations)) != 1:
        raise ScheduleError("consumer workers must use one account rotation ordinal")


def _validate_consumer_wave_run_contract(
    wave: Mapping[str, Any],
    run_contract: Mapping[str, Any],
    *,
    consumer_wave_ordinal: int,
) -> None:
    """Bind a stored per-wave allocation to the frozen full worker pool."""

    pipeline = run_contract.get("pipeline")
    if not isinstance(pipeline, Mapping):
        raise ScheduleError("consumer wave run contract is missing pipeline options")
    if pipeline.get("account_rotation_policy") != ACCOUNT_ROTATION_POLICY:
        raise ScheduleError("run contract account rotation policy mismatch")
    if (
        pipeline.get("consumer_claim_sharding_policy")
        != CONSUMER_CLAIM_SHARDING_POLICY
    ):
        raise ScheduleError("run contract consumer claim sharding policy mismatch")
    try:
        rotation_base = _nonnegative_int(
            pipeline.get("account_rotation_base"),
            "run contract account_rotation_base",
        )
        ordinal = _nonnegative_int(
            consumer_wave_ordinal, "consumer_wave_ordinal"
        )
    except ValueError as exc:
        raise ScheduleError(str(exc)) from exc
    expected_rotation = rotation_base + ordinal
    frozen_workers = run_contract.get("workers")
    if not isinstance(frozen_workers, list) or not frozen_workers:
        raise ScheduleError("consumer wave run contract is missing worker slices")
    if any(not isinstance(worker, Mapping) for worker in frozen_workers):
        raise ScheduleError("run contract worker entry must be an object")

    frozen_ids = [str(worker.get("worker_id") or "") for worker in frozen_workers]
    if any(not worker_id for worker_id in frozen_ids) or len(set(frozen_ids)) != len(
        frozen_ids
    ):
        raise ScheduleError("run contract worker_ids are invalid")
    claim_plan = wave["claim_plan"]
    if claim_plan.get("configured_worker_ids") != frozen_ids:
        raise ScheduleError("consumer active pool differs from frozen worker set")
    try:
        frozen_limits = [
            _positive_int(worker.get("limit"), "run contract worker.limit")
            for worker in frozen_workers
        ]
    except ValueError as exc:
        raise ScheduleError(str(exc)) from exc
    configured_limit = claim_plan.get("configured_worker_limit")
    if any(limit != configured_limit for limit in frozen_limits):
        raise ScheduleError("consumer configured limit differs from frozen worker cap")

    frozen_by_id = {
        worker_id: frozen
        for worker_id, frozen in zip(frozen_ids, frozen_workers)
    }
    for worker in wave["workers"]:
        frozen = frozen_by_id[str(worker["worker_id"])]
        for field_name in (
            "account_offset",
            "account_count",
            "usernames",
            "chrome_profiles",
        ):
            if worker.get(field_name) != frozen.get(field_name):
                raise ScheduleError(
                    f"consumer {field_name} differs from frozen worker slice"
                )
        if worker["limit"] > frozen["limit"]:
            raise ScheduleError("consumer claim cap exceeds frozen worker cap")
        command = worker["command"]
        rotation_index = command.index("--account-rotation") + 1
        if command[rotation_index] != str(expected_rotation):
            raise ScheduleError(
                "consumer account rotation differs from run-contract base and wave ordinal"
            )


def append_wave_manifest(
    path: str | os.PathLike[str],
    *,
    batch_id: str,
    run_id: str,
    pools: AccountPools,
    wave: Mapping[str, Any],
    resume: bool,
    run_contract: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Atomically append one immutable wave; never replace an existing wave.

    An advisory lock serializes read/verify/append/replace so two graph runners cannot
    lose one another's history even though the final file replacement is atomic.
    """

    schedule_path = Path(path).expanduser().resolve()
    schedule_path.parent.mkdir(parents=True, exist_ok=True)
    _validate_wave_record(wave)
    if wave.get("batch_id") != batch_id:
        raise ScheduleError("wave batch_id does not match schedule batch_id")
    lock_path = schedule_path.with_name(schedule_path.name + ".lock")
    with open(lock_path, "a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if schedule_path.exists():
            if not resume:
                raise ScheduleError(
                    "schedule already exists; explicit resume is required"
                )
            schedule = load_schedule(schedule_path)
            _assert_schedule_identity(
                schedule,
                batch_id=batch_id,
                run_id=run_id,
                pools=pools,
                run_contract=run_contract,
            )
        else:
            schedule = _new_schedule(batch_id, run_id, pools, run_contract)

        previous_waves = copy.deepcopy(schedule["waves"])
        wave_id = str(wave["wave_id"])
        if any(
            isinstance(item, Mapping) and item.get("wave_id") == wave_id
            for item in previous_waves
        ):
            raise ScheduleError(f"wave_id already exists: {wave_id}")
        if wave.get("kind") == "stage3_consumers":
            consumer_wave_ordinal = sum(
                1
                for historical_wave in previous_waves
                if isinstance(historical_wave, Mapping)
                and historical_wave.get("kind") == "stage3_consumers"
            )
            _validate_consumer_wave_run_contract(
                wave,
                schedule["run_contract"],
                consumer_wave_ordinal=consumer_wave_ordinal,
            )
        schedule["waves"] = previous_waves + [copy.deepcopy(dict(wave))]
        if schedule["waves"][:-1] != previous_waves:
            raise ScheduleError("existing wave history changed during append")
        _atomic_write_json(schedule_path, schedule)
        persisted = load_schedule(schedule_path)
        if persisted["waves"][:-1] != previous_waves:
            raise ScheduleError("persisted wave history was not append-only")
        return persisted


def ensure_schedule_manifest(
    path: str | os.PathLike[str],
    *,
    batch_id: str,
    run_id: str,
    pools: AccountPools,
    resume: bool,
    run_contract: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Create an empty schedule or validate an explicitly resumed one."""

    schedule_path = Path(path).expanduser().resolve()
    schedule_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = schedule_path.with_name(schedule_path.name + ".lock")
    with open(lock_path, "a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if schedule_path.exists():
            if not resume:
                raise ScheduleError(
                    "schedule already exists; explicit resume is required"
                )
            schedule = load_schedule(schedule_path)
            _assert_schedule_identity(
                schedule,
                batch_id=batch_id,
                run_id=run_id,
                pools=pools,
                run_contract=run_contract,
            )
            return schedule
        schedule = _new_schedule(batch_id, run_id, pools, run_contract)
        _atomic_write_json(schedule_path, schedule)
        return schedule


def read_queue_state(
    db_path: str | os.PathLike[str], batch_id: str
) -> QueueState:
    """Read only the orchestration counters from an explicit SQLite path."""

    selected_batch = _nonempty_label(batch_id, "batch_id")
    path = Path(db_path).expanduser().resolve()
    uri = path.as_uri() + "?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        rows = connection.execute(
            "SELECT status,(locked_at IS NULL) AS is_unlocked,COUNT(*) AS n "
            "FROM creator_profiles WHERE discovery_batch=? "
            "GROUP BY status,locked_at IS NULL",
            (selected_batch,),
        ).fetchall()
    except sqlite3.Error as exc:
        raise GraphRunnerError(f"queue state read failed: {path}") from exc
    finally:
        if "connection" in locals():
            connection.close()
    counts: dict[str, int] = {}
    unlocked_qualified = 0
    locks = 0
    for row in rows:
        status = row["status"] if isinstance(row["status"], str) else "null"
        count = int(row["n"])
        counts[status] = counts.get(status, 0) + count
        if status == "qualified" and bool(row["is_unlocked"]):
            unlocked_qualified += count
        if not bool(row["is_unlocked"]):
            locks += count
    return QueueState(
        seed_count=counts.get("seed", 0),
        qualified_count=counts.get("qualified", 0),
        qualified_unlocked_count=unlocked_qualified,
        collected_count=counts.get("collected", 0),
        rejected_count=counts.get("rejected", 0),
        lock_count=locks,
    )


def _validate_config(config: GraphConfig) -> None:
    _nonempty_label(config.batch_id, "batch_id")
    _positive_int(config.worker_count, "worker_count")
    _positive_int(config.worker_limit, "worker_limit")
    _nonnegative_int(config.account_rotation_base, "account_rotation_base")
    _positive_int(config.posts, "posts")
    _positive_int(config.max_consumer_waves, "max_consumer_waves")
    _positive_int(config.max_no_progress_waves, "max_no_progress_waves")
    if config.poll_interval_seconds < 0:
        raise ValueError("poll_interval_seconds must be non-negative")
    if config.wave_cooldown_seconds < 0:
        raise ValueError("wave_cooldown_seconds must be non-negative")
    if config.lease_ttl_seconds <= 0 or config.heartbeat_interval_seconds <= 0:
        raise ValueError("lease TTL/heartbeat interval must be positive")
    if config.heartbeat_interval_seconds >= config.lease_ttl_seconds:
        raise ValueError("heartbeat interval must be shorter than lease TTL")
    if config.subprocess_spawn_timeout_seconds <= 0:
        raise ValueError("subprocess spawn timeout must be positive")
    if config.subprocess_spawn_timeout_seconds >= config.lease_ttl_seconds:
        raise ValueError("subprocess spawn timeout must be shorter than lease TTL")
    if config.termination_grace_seconds <= 0:
        raise ValueError("termination grace must be positive")
    if config.termination_quarantine_seconds < config.lease_ttl_seconds:
        raise ValueError("termination quarantine must be at least the lease TTL")
    if config.resume_from_stage3 and not config.resume:
        raise ValueError("resume_from_stage3 requires explicit resume")
    if not config.resume_from_stage3 and config.round_contract is None:
        raise ValueError("round_contract is required before B1")
    if Path(config.creator_db).resolve() == Path(config.lease_db).resolve():
        raise ValueError("lease_db must be isolated from creator_db")


def _assert_worker_pool_bindings(config: GraphConfig) -> None:
    """Prove the preflighted files are exactly the files current worker CLIs open.

    Stage 2 has no account-file flag, and Stage 3 currently reads its deep pool from
    ``sop_v2.toml``.  Accepting a different orchestration path would lease one pool
    while the subprocess silently opens another, so the real subprocess path fails
    closed.  Injected test/process factories may intentionally model other layouts.
    """

    from extensions.sop_v2.config import load_config

    root = Path(config.workspace_root).resolve()
    expected_shallow = (root / ".secrets/accounts_raw.txt").resolve()
    configured_deep = load_config().get("pipeline", {}).get("deep_accounts_file")
    if not isinstance(configured_deep, str) or not configured_deep.strip():
        raise AccountPreflightError("pipeline.deep_accounts_file is not configured")
    expected_deep = (root / configured_deep).resolve()
    expected_profiles = (root / ".secrets/chrome-instagram-profiles").resolve()
    actual = {
        "shallow account file": Path(config.shallow_accounts_file).resolve(),
        "deep account file": Path(config.deep_accounts_file).resolve(),
        "Chrome profile root": Path(config.profile_root).resolve(),
    }
    expected = {
        "shallow account file": expected_shallow,
        "deep account file": expected_deep,
        "Chrome profile root": expected_profiles,
    }
    mismatches = [
        f"{label}: runner={actual[label]} worker={expected[label]}"
        for label in expected
        if actual[label] != expected[label]
    ]
    if mismatches:
        raise AccountPreflightError(
            "worker account/Profile binding mismatch; " + "; ".join(mismatches)
        )


def _stage2_command(config: GraphConfig) -> tuple[str, ...]:
    command = [
        config.python_executable,
        "-m",
        "extensions.sop_v2.pipeline.stage2_qualify",
        "--batch-id",
        config.batch_id,
    ]
    if config.resume:
        command.append("--resume")
    return tuple(command)


def _stage3_command(
    config: GraphConfig,
    worker: WorkerSlice,
    account_rotation: int = 0,
) -> tuple[str, ...]:
    if (
        isinstance(account_rotation, bool)
        or not isinstance(account_rotation, int)
        or account_rotation < 0
    ):
        raise ValueError("account_rotation must be a non-negative integer")
    command = [
        config.python_executable,
        "-m",
        "extensions.sop_v2.pipeline.stage3_collect",
        "--batch-id",
        config.batch_id,
        "--limit",
        str(worker.limit),
        "--posts",
        str(config.posts),
        "--account-offset",
        str(worker.account_offset),
        "--account-count",
        str(worker.account_count),
        "--account-rotation",
        str(account_rotation),
    ]
    if config.strict_completeness:
        command.append("--strict-completeness")
    if config.resume:
        command.append("--resume")
    if config.translate_comments:
        command.extend(
            ["--translate-comments", "--translation-provider", config.translation_provider]
        )
        if config.translation_model:
            command.extend(["--translation-model", config.translation_model])
    return tuple(command)


def _process_environment(config: GraphConfig) -> dict[str, str]:
    environment = os.environ.copy()
    scripts = str(Path(config.workspace_root).resolve() / "scripts")
    prior = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = scripts + (os.pathsep + prior if prior else "")
    return environment


def _resource_keys(accounts: Sequence[AccountSpec]) -> tuple[str, ...]:
    keys: list[str] = []
    for account in accounts:
        keys.append(resource_leases.account_resource_key(account.username))
        keys.append(resource_leases.chrome_profile_resource_key(account.profile_path))
    return tuple(keys)


def _stage2_singleton_resource_key(batch_id: str) -> str:
    """A lease shared by every conforming runner prevents a second producer."""

    selected_batch = _nonempty_label(batch_id, "batch_id").casefold()
    return f"pipeline:stage2-producer:{selected_batch}"


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def _emit_event(
    emitter: EventEmitter,
    wave_id: str,
    worker_id: str,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    event = await _maybe_await(emitter(wave_id, worker_id, payload))
    if not isinstance(event, dict):
        raise EventLogError("event emitter must return the persisted event object")
    return event


async def _best_effort_event(
    emitter: EventEmitter,
    wave_id: str,
    worker_id: str,
    payload: Mapping[str, Any],
) -> None:
    try:
        await _emit_event(emitter, wave_id, worker_id, payload)
    except BaseException:
        pass


def _barrier_passed(value: Any) -> tuple[bool, tuple[str, ...]]:
    if isinstance(value, bool):
        return value, ()
    if isinstance(value, Mapping):
        failures = value.get("failures", ())
        if not isinstance(failures, Sequence) or isinstance(failures, (str, bytes)):
            failures = (str(failures),)
        return bool(value.get("passed", False)), tuple(str(item) for item in failures)
    passed = bool(getattr(value, "passed", False))
    failures = getattr(value, "failures", ())
    return passed, tuple(str(item) for item in failures)


def _barrier_json(value: Any) -> Any:
    if value is None or isinstance(value, (bool, str, int, float, list, dict)):
        return value
    method = getattr(value, "as_dict", None)
    return method() if callable(method) else str(value)


def _wave_id(schedule: Mapping[str, Any], kind: str) -> str:
    return f"wave-{len(schedule.get('waves', [])) + 1:04d}-{kind}"


def _producer_wave(
    wave_id: str, config: GraphConfig, pools: AccountPools, command: Sequence[str]
) -> dict[str, Any]:
    return {
        "wave_id": wave_id,
        "kind": "stage2_producer",
        "created_at": _utc_now(),
        "batch_id": config.batch_id,
        "producer": {
            "worker_id": "stage2-producer",
            "command": list(command),
            "usernames": [account.username for account in pools.shallow],
            "chrome_profiles": [str(account.profile_path) for account in pools.shallow],
        },
    }


def _consumer_wave(
    wave_id: str,
    config: GraphConfig,
    configured_workers: Sequence[WorkerSlice],
    active_workers: Sequence[WorkerSlice],
    commands: Mapping[str, Sequence[str]],
    *,
    eligible_qualified_count: int,
) -> dict[str, Any]:
    validate_worker_slices(configured_workers)
    expected_workers = plan_consumer_wave_workers(
        configured_workers,
        eligible_qualified_count=eligible_qualified_count,
        configured_worker_limit=config.worker_limit,
    )
    if not expected_workers:
        raise ValueError("consumer wave cannot be created without eligible work")
    if tuple(active_workers) != expected_workers:
        raise ValueError("active workers do not match deterministic claim plan")
    if set(commands) != {worker.worker_id for worker in active_workers}:
        raise ValueError("commands must exactly match active workers")
    planned_claim_count = min(
        eligible_qualified_count,
        len(configured_workers) * config.worker_limit,
    )
    allocated_claim_capacity = sum(worker.limit for worker in active_workers)
    return {
        "wave_id": wave_id,
        "kind": "stage3_consumers",
        "created_at": _utc_now(),
        "batch_id": config.batch_id,
        "claim_plan": {
            "policy": CONSUMER_CLAIM_SHARDING_POLICY,
            "eligible_qualified_count": eligible_qualified_count,
            "planned_claim_count": planned_claim_count,
            "allocated_claim_capacity": allocated_claim_capacity,
            "configured_worker_limit": config.worker_limit,
            "configured_worker_ids": [
                worker.worker_id for worker in configured_workers
            ],
            "active_worker_ids": [worker.worker_id for worker in active_workers],
            "claim_caps": [
                {"worker_id": worker.worker_id, "limit": worker.limit}
                for worker in active_workers
            ],
        },
        "workers": [
            worker.as_dict(commands[worker.worker_id]) for worker in active_workers
        ],
        "assertions": {
            "finite_positive_limits": True,
            "claim_caps_even_and_bounded": True,
            "claim_caps_cover_planned_work": True,
            "active_worker_set_is_deterministic": True,
            "explicit_positive_account_counts": True,
            "account_slices_mutually_exclusive": True,
            "profile_slices_mutually_exclusive": True,
        },
    }


async def _release_bundles(
    lease_api: Any,
    lease_db: Path,
    bundles: Iterable[resource_leases.LeaseBundle],
    *,
    reason: str,
) -> list[BaseException]:
    failures: list[BaseException] = []
    for bundle in bundles:
        try:
            lease_api.release_resources(lease_db, bundle, reason=reason)
        except BaseException as exc:  # cleanup must attempt every bundle
            failures.append(exc)
    return failures


def _acquire_bundles(
    lease_api: Any,
    config: GraphConfig,
    run_id: str,
    wave_id: str,
    resources: Sequence[tuple[str, tuple[str, ...]]],
) -> dict[str, resource_leases.LeaseBundle]:
    acquired: dict[str, resource_leases.LeaseBundle] = {}
    try:
        for worker_id, keys in resources:
            acquired[worker_id] = lease_api.acquire_resources(
                config.lease_db,
                batch_id=config.batch_id,
                run_id=run_id,
                wave_id=wave_id,
                worker_id=worker_id,
                resource_keys=keys,
                ttl_seconds=config.lease_ttl_seconds,
            )
    except BaseException:
        for bundle in acquired.values():
            try:
                lease_api.release_resources(
                    config.lease_db, bundle, reason="wave_acquire_rollback"
                )
            except BaseException:
                pass
        raise
    return acquired


def _signal_process(process: Any, selected_signal: signal.Signals) -> None:
    """Signal the dedicated worker process group, with a fake/process fallback."""

    pid = getattr(process, "pid", None)
    if isinstance(pid, int) and pid > 0:
        try:
            # Real workers are spawned with start_new_session=True, so PGID == PID.
            os.killpg(pid, selected_signal)
            return
        except ProcessLookupError:
            return
        except OSError:
            pass
    method_name = "terminate" if selected_signal == signal.SIGTERM else "kill"
    method = getattr(process, method_name, None)
    if not callable(method) and selected_signal == signal.SIGKILL:
        method = getattr(process, "terminate", None)
    if callable(method):
        try:
            method()
        except ProcessLookupError:
            pass


async def _wait_for_process_exit(process: Any, timeout: float) -> bool:
    if getattr(process, "returncode", None) is not None:
        return True
    try:
        await asyncio.wait_for(process.wait(), timeout=timeout)
        return True
    except asyncio.TimeoutError:
        return getattr(process, "returncode", None) is not None
    except BaseException:
        return getattr(process, "returncode", None) is not None


async def _terminate_process(process: Any, *, grace_seconds: float) -> bool:
    """TERM then KILL a whole worker session; never wait without a deadline."""

    if getattr(process, "returncode", None) is not None:
        return True
    _signal_process(process, signal.SIGTERM)
    if await _wait_for_process_exit(process, grace_seconds):
        return True
    _signal_process(process, signal.SIGKILL)
    return await _wait_for_process_exit(process, grace_seconds)


def _quarantine_bundle(
    lease_api: Any,
    config: GraphConfig,
    bundle: resource_leases.LeaseBundle,
) -> resource_leases.LeaseBundle:
    """Delay takeover when a killed process group cannot be confirmed dead."""

    return lease_api.heartbeat_resources(
        config.lease_db,
        bundle,
        ttl_seconds=config.termination_quarantine_seconds,
    )


async def _supervise_process(
    *,
    worker_id: str,
    command: Sequence[str],
    process: Any,
    bundle: resource_leases.LeaseBundle,
    config: GraphConfig,
    lease_api: Any,
    sleep: Sleep,
    emit_event: EventEmitter,
) -> int:
    current_bundle = [bundle]

    async def heartbeat() -> None:
        while True:
            await sleep(config.heartbeat_interval_seconds)
            current_bundle[0] = lease_api.heartbeat_resources(
                config.lease_db,
                current_bundle[0],
                ttl_seconds=config.lease_ttl_seconds,
            )

    wait_task = asyncio.create_task(process.wait())
    heartbeat_task = asyncio.create_task(heartbeat())
    reason = "worker_release"
    release_allowed = True
    try:
        done, _ = await asyncio.wait(
            (wait_task, heartbeat_task), return_when=asyncio.FIRST_COMPLETED
        )
        if heartbeat_task in done:
            exception = heartbeat_task.exception()
            if exception is not None:
                reason = "lease_heartbeat_failed"
                terminated = await _terminate_process(
                    process, grace_seconds=config.termination_grace_seconds
                )
                if not terminated:
                    release_allowed = False
                    try:
                        current_bundle[0] = _quarantine_bundle(
                            lease_api, config, current_bundle[0]
                        )
                    except BaseException:
                        pass
                    await _emit_event(
                        emit_event,
                        bundle.wave_id,
                        worker_id,
                        {
                            "state": "failed",
                            "reason": "lease_heartbeat_failed_process_survived",
                        },
                    )
                    raise ProcessTerminationError(
                        f"{worker_id} process group survived SIGKILL"
                    ) from exception
                await _emit_event(
                    emit_event,
                    bundle.wave_id,
                    worker_id,
                    {"state": "failed", "reason": "lease_heartbeat_failed"},
                )
                raise GraphRunnerError(
                    f"{worker_id} lost its resource heartbeat"
                ) from exception
        returncode = int(await wait_task)
        if returncode != 0:
            reason = "subprocess_nonzero"
            await _emit_event(
                emit_event,
                bundle.wave_id,
                worker_id,
                {
                    "state": "failed",
                    "reason": "subprocess_nonzero",
                    "returncode": returncode,
                },
            )
            raise SubprocessFailed(worker_id, command, returncode)
        await _emit_event(
            emit_event,
            bundle.wave_id,
            worker_id,
            {"state": "completed", "returncode": returncode},
        )
        return returncode
    except asyncio.CancelledError:
        reason = "runner_cancelled"
        terminated = await _terminate_process(
            process, grace_seconds=config.termination_grace_seconds
        )
        if not terminated:
            release_allowed = False
            try:
                current_bundle[0] = _quarantine_bundle(
                    lease_api, config, current_bundle[0]
                )
            except BaseException:
                pass
        await _best_effort_event(
            emit_event,
            bundle.wave_id,
            worker_id,
            {
                "state": "failed",
                "reason": "runner_cancelled",
                "termination_confirmed": terminated,
            },
        )
        raise
    finally:
        for task in (wait_task, heartbeat_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(wait_task, heartbeat_task, return_exceptions=True)
        release_failures = (
            await _release_bundles(
                lease_api,
                config.lease_db,
                (current_bundle[0],),
                reason=reason,
            )
            if release_allowed
            else []
        )
        if release_allowed and not release_failures:
            await _emit_event(
                emit_event,
                bundle.wave_id,
                worker_id,
                {"state": "released", "reason": reason},
            )
        elif release_failures:
            await _best_effort_event(
                emit_event,
                bundle.wave_id,
                worker_id,
                {
                    "state": "failed",
                    "reason": "resource_release_failed",
                    "error": type(release_failures[0]).__name__,
                },
            )
        if release_failures and reason == "worker_release":
            raise GraphRunnerError(f"{worker_id} resource release failed") from release_failures[0]


async def _spawn_group(
    *,
    entries: Sequence[tuple[str, tuple[str, ...], resource_leases.LeaseBundle]],
    config: GraphConfig,
    process_factory: ProcessFactory,
    lease_api: Any,
    sleep: Sleep,
    emit_event: EventEmitter,
) -> asyncio.Task[list[int]]:
    commands = {worker_id: command for worker_id, command, _ in entries}
    bundles = {worker_id: bundle for worker_id, _, bundle in entries}
    processes: dict[str, Any] = {}
    try:
        for worker_id, _, _ in entries:
            # Keep every acquired bundle alive while subprocess creation is still
            # sequential.  Each individual spawn is bounded well inside the TTL.
            for owner, bundle in tuple(bundles.items()):
                bundles[owner] = lease_api.heartbeat_resources(
                    config.lease_db,
                    bundle,
                    ttl_seconds=config.lease_ttl_seconds,
                )
            process = await asyncio.wait_for(
                process_factory(
                    *commands[worker_id],
                    cwd=str(Path(config.workspace_root).resolve()),
                    env=_process_environment(config),
                    start_new_session=True,
                ),
                timeout=config.subprocess_spawn_timeout_seconds,
            )
            processes[worker_id] = process
            await _emit_event(
                emit_event,
                bundles[worker_id].wave_id,
                worker_id,
                {
                    "state": "spawned",
                    "pid": getattr(process, "pid", None),
                    "command_sha256": hashlib.sha256(
                        "\0".join(commands[worker_id]).encode("utf-8")
                    ).hexdigest(),
                },
            )
        for owner, bundle in tuple(bundles.items()):
            bundles[owner] = lease_api.heartbeat_resources(
                config.lease_db,
                bundle,
                ttl_seconds=config.lease_ttl_seconds,
            )
    except BaseException as spawn_error:
        releasable: list[resource_leases.LeaseBundle] = []
        for worker_id, bundle in bundles.items():
            process = processes.get(worker_id)
            if process is None:
                releasable.append(bundle)
                await _best_effort_event(
                    emit_event,
                    bundle.wave_id,
                    worker_id,
                    {
                        "state": "failed",
                        "reason": "subprocess_not_spawned",
                        "error": type(spawn_error).__name__,
                    },
                )
                continue
            terminated = await _terminate_process(
                process, grace_seconds=config.termination_grace_seconds
            )
            if terminated:
                releasable.append(bundle)
            else:
                try:
                    bundles[worker_id] = _quarantine_bundle(
                        lease_api, config, bundle
                    )
                except BaseException:
                    pass
            await _best_effort_event(
                emit_event,
                bundle.wave_id,
                worker_id,
                {
                    "state": "failed",
                    "reason": "wave_spawn_failed",
                    "error": type(spawn_error).__name__,
                    "termination_confirmed": terminated,
                },
            )
        await _release_bundles(
            lease_api,
            config.lease_db,
            releasable,
            reason="subprocess_spawn_failed",
        )
        for bundle in releasable:
            await _best_effort_event(
                emit_event,
                bundle.wave_id,
                bundle.worker_id,
                {"state": "released", "reason": "subprocess_spawn_failed"},
            )
        if any(
            worker_id in processes and bundles[worker_id] not in releasable
            for worker_id in bundles
        ):
            raise ProcessTerminationError(
                "spawn failed and one or more worker groups survived SIGKILL"
            ) from spawn_error
        raise

    async def supervise_group() -> list[int]:
        results = await asyncio.gather(
            *(
                _supervise_process(
                    worker_id=worker_id,
                    command=commands[worker_id],
                    process=processes[worker_id],
                    bundle=bundles[worker_id],
                    config=config,
                    lease_api=lease_api,
                    sleep=sleep,
                    emit_event=emit_event,
                )
                for worker_id in processes
            ),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException):
                raise result
        return [int(result) for result in results]

    return asyncio.create_task(supervise_group())


async def run_graph(
    config: GraphConfig,
    *,
    process_factory: ProcessFactory = asyncio.create_subprocess_exec,
    queue_probe: QueueProbe | None = None,
    b1_evaluator: BarrierEvaluator | None = None,
    b2_evaluator: BarrierEvaluator | None = None,
    lease_api: Any = resource_leases,
    sleep: Sleep = asyncio.sleep,
) -> GraphRunResult:
    """Run the bounded producer/consumer graph and return only after B2 + drain.

    ``resume_from_stage3`` is the sole path that skips live B1 evaluation.  It is
    allowed only when explicit B2 evaluation passes against the immutable Stage 1
    artifact, proving Stage 2 already closed in persisted state.
    """

    _validate_config(config)
    if process_factory is asyncio.create_subprocess_exec:
        _assert_worker_pool_bindings(config)
    pools = preflight_account_pools(
        config.shallow_accounts_file,
        config.deep_accounts_file,
        config.profile_root,
        require_profile_directories=config.require_profile_directories,
        reject_active_profile_markers=config.reject_active_profile_markers,
    )
    workers = build_worker_slices(
        pools.deep,
        worker_count=config.worker_count,
        limit=config.worker_limit,
    )
    frozen_run_contract = build_run_contract(config, pools, workers)
    event_path = _event_log_path(config)

    existing: dict[str, Any] | None = None
    historical_events: tuple[dict[str, Any], ...] = ()
    if Path(config.schedule_path).exists():
        if not config.resume:
            raise ScheduleError("schedule exists; pass resume to append a new wave")
        existing = load_schedule(config.schedule_path)
        run_id = str(existing.get("run_id") or "")
        _assert_schedule_identity(
            existing,
            batch_id=config.batch_id,
            run_id=run_id,
            pools=pools,
            run_contract=frozen_run_contract,
        )
        if config.run_id is not None and config.run_id != run_id:
            raise ResumeMismatchError("requested run_id differs from frozen schedule")
        historical_events = validate_graph_event_log(
            event_path, run_id=run_id, batch_id=config.batch_id
        )
        validate_schedule_event_coverage(existing, historical_events)
    else:
        run_id = config.run_id or uuid.uuid4().hex
        orphan_events = validate_graph_event_log(event_path)
        if orphan_events:
            raise EventLogError(
                "event log exists without its frozen schedule; refusing a new run"
            )

    def emit_event(
        wave_id: str, worker_id: str, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        return append_graph_event(
            event_path,
            run_id=run_id,
            batch_id=config.batch_id,
            wave_id=wave_id,
            worker_id=worker_id,
            payload=payload,
        )

    probe = queue_probe or (
        lambda: read_queue_state(config.creator_db, config.batch_id)
    )
    default_b2 = lambda: barriers.evaluate_b2(
        config.creator_db,
        stage1_artifact=config.stage1_artifact,
        batch_id=config.batch_id,
        producer_exited=True,
    )
    b1_result: Any = None
    b2_result: Any = None
    producer_started = False
    producer_task: asyncio.Task[list[int]] | None = None
    consumer_task: asyncio.Task[list[int]] | None = None
    consumer_waves = 0
    no_progress_waves = consecutive_no_progress_waves(historical_events)
    active_consumer_wave_id: str | None = None
    active_consumer_start: QueueState | None = None

    if config.resume_from_stage3:
        b2_result = await _maybe_await((b2_evaluator or default_b2)())
        passed, failures = _barrier_passed(b2_result)
        if not passed:
            raise BarrierFailed("B2", failures)
        producer_done = True
    else:
        default_b1 = lambda: barriers.evaluate_b1(
            config.creator_db,
            round_contract=config.round_contract,  # validated non-null above
            stage1_artifact=config.stage1_artifact,
            batch_id=config.batch_id,
        )
        b1_result = await _maybe_await((b1_evaluator or default_b1)())
        passed, failures = _barrier_passed(b1_result)
        if not passed:
            raise BarrierFailed("B1", failures)
        producer_done = False

    schedule = ensure_schedule_manifest(
        config.schedule_path,
        batch_id=config.batch_id,
        run_id=run_id,
        pools=pools,
        resume=config.resume,
        run_contract=frozen_run_contract,
    )
    # A restart must not reset the bounded retry budget.  Immutable schedule waves
    # are launch intents persisted before spawn, so every historical Stage 3 wave
    # counts even when the previous runner died before it could emit wave_result.
    historical_consumer_waves = sum(
        1
        for wave in schedule.get("waves", [])
        if isinstance(wave, Mapping) and wave.get("kind") == "stage3_consumers"
    )
    await _emit_event(
        emit_event,
        "graph",
        "graph-runner",
        {
            "state": "barrier_verified",
            "barrier": "B2" if config.resume_from_stage3 else "B1",
            "result": _barrier_json(b2_result if config.resume_from_stage3 else b1_result),
        },
    )

    if not producer_done:
        command = _stage2_command(config)
        wave_id = _wave_id(schedule, "stage2")
        bundles = _acquire_bundles(
            lease_api,
            config,
            run_id,
            wave_id,
            (
                (
                    "stage2-producer",
                    (
                        _stage2_singleton_resource_key(config.batch_id),
                        *_resource_keys(pools.shallow),
                    ),
                ),
            ),
        )
        try:
            schedule = append_wave_manifest(
                config.schedule_path,
                batch_id=config.batch_id,
                run_id=run_id,
                pools=pools,
                wave=_producer_wave(wave_id, config, pools, command),
                resume=True,
                run_contract=frozen_run_contract,
            )
            persisted_wave = schedule["waves"][-1]
            await _emit_event(
                emit_event,
                wave_id,
                "stage2-producer",
                {
                    "state": "intent_persisted",
                    "schedule_path": str(Path(config.schedule_path).resolve()),
                    "wave_sha256": _wave_sha256(persisted_wave),
                },
            )
            await _emit_event(
                emit_event,
                wave_id,
                "stage2-producer",
                {
                    "state": "resources_leased",
                    "resource_count": len(
                        bundles["stage2-producer"].resource_keys
                    ),
                    "expires_at": bundles["stage2-producer"].expires_at,
                },
            )
        except BaseException:
            release_failures = await _release_bundles(
                lease_api,
                config.lease_db,
                bundles.values(),
                reason="schedule_append_failed",
            )
            if not release_failures:
                for bundle in bundles.values():
                    await _best_effort_event(
                        emit_event,
                        wave_id,
                        bundle.worker_id,
                        {"state": "released", "reason": "schedule_append_failed"},
                    )
            raise
        producer_task = await _spawn_group(
            entries=(("stage2-producer", command, bundles["stage2-producer"]),),
            config=config,
            process_factory=process_factory,
            lease_api=lease_api,
            sleep=sleep,
            emit_event=emit_event,
        )
        producer_started = True

    try:
        while True:
            # Give immediately-completing fake/real subprocess waiters a scheduling turn.
            await asyncio.sleep(0)
            if producer_task is not None and producer_task.done():
                await producer_task  # propagates exact non-zero status
                producer_task = None
                producer_done = True
                b2_result = await _maybe_await((b2_evaluator or default_b2)())
                passed, failures = _barrier_passed(b2_result)
                if not passed:
                    if consumer_task is not None:
                        await consumer_task
                    raise BarrierFailed("B2", failures)

            consumer_just_finished = False
            if consumer_task is not None and consumer_task.done():
                await consumer_task  # propagate any worker's exact non-zero status
                consumer_task = None
                consumer_just_finished = True

            state = await _maybe_await(probe())
            if not isinstance(state, QueueState):
                raise GraphRunnerError("queue_probe must return QueueState")

            if consumer_just_finished:
                if active_consumer_wave_id is None or active_consumer_start is None:
                    raise GraphRunnerError("completed consumer wave has no start snapshot")
                collected_delta = (
                    state.collected_count - active_consumer_start.collected_count
                )
                if collected_delta < 0:
                    raise GraphRunnerError("collected count decreased across a wave")
                no_progress_waves = no_progress_waves + 1 if collected_delta == 0 else 0
                await _emit_event(
                    emit_event,
                    active_consumer_wave_id,
                    "graph-runner",
                    {
                        "state": "wave_result",
                        "collected_before": active_consumer_start.collected_count,
                        "collected_after": state.collected_count,
                        "collected_delta": collected_delta,
                        "qualified_before": active_consumer_start.qualified_count,
                        "qualified_after": state.qualified_count,
                        "consecutive_no_progress_waves": no_progress_waves,
                        "producer_done": producer_done,
                    },
                )
                active_consumer_wave_id = None
                active_consumer_start = None
                if no_progress_waves >= config.max_no_progress_waves:
                    raise GraphIncompleteError(
                        "Stage 3 stopped after consecutive waves with no collected progress"
                    )
                # Do not burn the account pool in a tight retry loop.  No cooldown is
                # needed when the graph is already completely drained.
                if not producer_done or state.qualified_count > 0:
                    await sleep(config.wave_cooldown_seconds)
                    continue

            if consumer_task is None and state.qualified_unlocked_count > 0:
                if no_progress_waves >= config.max_no_progress_waves:
                    raise GraphIncompleteError(
                        "Stage 3 no-progress threshold was already reached"
                    )
                if (
                    historical_consumer_waves + consumer_waves
                    >= config.max_consumer_waves
                ):
                    raise GraphIncompleteError(
                        "qualified queue remains after max_consumer_waves"
                    )
                wave_id = _wave_id(schedule, "stage3")
                # ``state`` is the read-only eligible snapshot taken immediately
                # before this intent.  It determines finite per-worker caps for this
                # wave; the immutable schedule then freezes the exact allocation.
                eligible_qualified_count = state.qualified_unlocked_count
                wave_workers = plan_consumer_wave_workers(
                    workers,
                    eligible_qualified_count=eligible_qualified_count,
                    configured_worker_limit=config.worker_limit,
                )
                if not wave_workers:
                    raise GraphRunnerError(
                        "positive eligible queue produced an empty consumer plan"
                    )
                # The Stage 3 wave ordinal is derived only from durable historical
                # intents plus this process's already-started waves, then added to
                # the run-contract base.  A new schedule can therefore continue the
                # prior account position explicitly, while same-schedule resume
                # advances and modulo rotation remains inside each worker slice.
                account_rotation = (
                    config.account_rotation_base
                    + historical_consumer_waves
                    + consumer_waves
                )
                commands = {
                    worker.worker_id: _stage3_command(
                        config, worker, account_rotation=account_rotation
                    )
                    for worker in wave_workers
                }
                bundles = _acquire_bundles(
                    lease_api,
                    config,
                    run_id,
                    wave_id,
                    tuple(
                        (worker.worker_id, _resource_keys(worker.accounts))
                        for worker in wave_workers
                    ),
                )
                try:
                    schedule = append_wave_manifest(
                        config.schedule_path,
                        batch_id=config.batch_id,
                        run_id=run_id,
                        pools=pools,
                        wave=_consumer_wave(
                            wave_id,
                            config,
                            workers,
                            wave_workers,
                            commands,
                            eligible_qualified_count=eligible_qualified_count,
                        ),
                        resume=True,
                        run_contract=frozen_run_contract,
                    )
                    persisted_wave = schedule["waves"][-1]
                    # One wave intent is recorded before the first worker spawn.
                    await _emit_event(
                        emit_event,
                        wave_id,
                        "graph-runner",
                        {
                            "state": "intent_persisted",
                            "schedule_path": str(Path(config.schedule_path).resolve()),
                            "wave_sha256": _wave_sha256(persisted_wave),
                            "eligible_qualified_count": eligible_qualified_count,
                            "planned_claim_count": persisted_wave["claim_plan"][
                                "planned_claim_count"
                            ],
                            "allocated_claim_capacity": persisted_wave["claim_plan"][
                                "allocated_claim_capacity"
                            ],
                            "active_worker_ids": persisted_wave["claim_plan"][
                                "active_worker_ids"
                            ],
                        },
                    )
                    for worker in wave_workers:
                        bundle = bundles[worker.worker_id]
                        await _emit_event(
                            emit_event,
                            wave_id,
                            worker.worker_id,
                            {
                                "state": "resources_leased",
                                "resource_count": len(bundle.resource_keys),
                                "expires_at": bundle.expires_at,
                            },
                        )
                except BaseException:
                    release_failures = await _release_bundles(
                        lease_api,
                        config.lease_db,
                        bundles.values(),
                        reason="schedule_append_failed",
                    )
                    if not release_failures:
                        for bundle in bundles.values():
                            await _best_effort_event(
                                emit_event,
                                wave_id,
                                bundle.worker_id,
                                {
                                    "state": "released",
                                    "reason": "schedule_append_failed",
                                },
                            )
                    raise
                consumer_task = await _spawn_group(
                    entries=tuple(
                        (
                            worker.worker_id,
                            commands[worker.worker_id],
                            bundles[worker.worker_id],
                        )
                        for worker in wave_workers
                    ),
                    config=config,
                    process_factory=process_factory,
                    lease_api=lease_api,
                    sleep=sleep,
                    emit_event=emit_event,
                )
                consumer_waves += 1
                active_consumer_wave_id = wave_id
                active_consumer_start = state
                continue

            if producer_done and consumer_task is None:
                # B2 may already have been proven by resume_from_stage3.  In a normal
                # run it is evaluated immediately after the actual producer exits.
                if b2_result is None:
                    b2_result = await _maybe_await((b2_evaluator or default_b2)())
                    passed, failures = _barrier_passed(b2_result)
                    if not passed:
                        raise BarrierFailed("B2", failures)
                if state.qualified_count == 0:
                    final_events = validate_graph_event_log(
                        event_path, run_id=run_id, batch_id=config.batch_id
                    )
                    validate_schedule_event_coverage(schedule, final_events)
                    return GraphRunResult(
                        batch_id=config.batch_id,
                        run_id=run_id,
                        producer_started=producer_started,
                        consumer_waves_started=consumer_waves,
                        schedule_path=Path(config.schedule_path).resolve(),
                        event_log_path=event_path,
                        final_queue=state,
                        b1=_barrier_json(b1_result),
                        b2=_barrier_json(b2_result),
                    )
                if state.qualified_unlocked_count == 0:
                    raise GraphIncompleteError(
                        "qualified rows remain locked after all managed workers exited"
                    )

            # Crucial graph invariant: producer alive + empty qualified is idle, not done.
            await sleep(config.poll_interval_seconds)
    except BaseException:
        tasks = [task for task in (producer_task, consumer_task) if task is not None]
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        raise


def _parser() -> argparse.ArgumentParser:
    root = Path(__file__).resolve().parents[4]
    parser = argparse.ArgumentParser(description="SOP V2 formal Stage 2/3 DAG runner")
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--creator-db", type=Path, default=root / "data/creator_cache.db")
    parser.add_argument("--lease-db", type=Path, default=root / "data/resource_leases.db")
    parser.add_argument("--stage1-artifact", type=Path, required=True)
    parser.add_argument("--round-contract", type=Path)
    parser.add_argument(
        "--schedule",
        type=Path,
        help="default: data/runs/<batch>/graph_schedule.json",
    )
    parser.add_argument(
        "--event-log",
        type=Path,
        help="default: graph_events.jsonl beside --schedule",
    )
    parser.add_argument(
        "--shallow-accounts",
        type=Path,
        default=root / ".secrets/accounts_raw.txt",
    )
    parser.add_argument(
        "--deep-accounts",
        type=Path,
        default=root / ".secrets/accounts_deep.txt",
    )
    parser.add_argument(
        "--profile-root",
        type=Path,
        default=root / ".secrets/chrome-instagram-profiles",
    )
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--worker-limit", type=int, default=24)
    parser.add_argument(
        "--account-rotation-base",
        type=int,
        default=0,
        help="first Stage 3 account rotation ordinal for a new schedule",
    )
    parser.add_argument("--posts", type=int, default=10)
    parser.add_argument("--max-consumer-waves", type=int, default=100)
    parser.add_argument("--wave-cooldown", type=float, default=120.0)
    parser.add_argument("--max-no-progress-waves", type=int, default=3)
    parser.add_argument("--poll-interval", type=float, default=2.0)
    parser.add_argument("--lease-ttl", type=float, default=120.0)
    parser.add_argument("--heartbeat-interval", type=float, default=30.0)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--resume-from-stage3",
        action="store_true",
        help="explicitly skip live B1 only after persisted B2 passes",
    )
    parser.add_argument("--no-strict-completeness", action="store_true")
    parser.add_argument("--no-translate-comments", action="store_true")
    parser.add_argument(
        "--translation-provider", choices=("ollama", "anthropic"), default="ollama"
    )
    parser.add_argument("--translation-model", default="qwen3.5:4b")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = Path(__file__).resolve().parents[4]
    if not args.resume_from_stage3 and args.round_contract is None:
        print("error: --round-contract is required unless --resume-from-stage3", file=sys.stderr)
        return 2
    schedule = args.schedule or (
        root / "data/runs" / args.batch_id / "graph_schedule.json"
    )
    event_log = args.event_log or schedule.with_name("graph_events.jsonl")
    config = GraphConfig(
        batch_id=args.batch_id,
        creator_db=args.creator_db,
        lease_db=args.lease_db,
        stage1_artifact=args.stage1_artifact,
        round_contract=args.round_contract,
        shallow_accounts_file=args.shallow_accounts,
        deep_accounts_file=args.deep_accounts,
        profile_root=args.profile_root,
        schedule_path=schedule,
        event_log_path=event_log,
        worker_count=args.workers,
        worker_limit=args.worker_limit,
        account_rotation_base=args.account_rotation_base,
        posts=args.posts,
        max_consumer_waves=args.max_consumer_waves,
        wave_cooldown_seconds=args.wave_cooldown,
        max_no_progress_waves=args.max_no_progress_waves,
        poll_interval_seconds=args.poll_interval,
        lease_ttl_seconds=args.lease_ttl,
        heartbeat_interval_seconds=args.heartbeat_interval,
        resume=args.resume,
        resume_from_stage3=args.resume_from_stage3,
        strict_completeness=not args.no_strict_completeness,
        translate_comments=not args.no_translate_comments,
        translation_provider=args.translation_provider,
        translation_model=args.translation_model,
        workspace_root=root,
    )
    try:
        result = asyncio.run(run_graph(config))
    except SubprocessFailed as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.returncode if 0 < exc.returncode < 256 else 1
    except (GraphRunnerError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "batch_id": result.batch_id,
                "run_id": result.run_id,
                "producer_started": result.producer_started,
                "consumer_waves_started": result.consumer_waves_started,
                "schedule_path": str(result.schedule_path),
                "event_log_path": str(result.event_log_path),
                "final_queue": result.final_queue.__dict__,
                "b1": result.b1,
                "b2": result.b2,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "AccountPools",
    "AccountPreflightError",
    "AccountSpec",
    "ACCOUNT_ROTATION_POLICY",
    "CONSUMER_CLAIM_SHARDING_POLICY",
    "BarrierFailed",
    "GraphConfig",
    "GraphIncompleteError",
    "GraphRunResult",
    "GraphRunnerError",
    "ProcessTerminationError",
    "QueueState",
    "ResumeMismatchError",
    "SCHEDULE_SCHEMA",
    "ScheduleError",
    "SubprocessFailed",
    "WorkerSlice",
    "append_wave_manifest",
    "build_worker_slices",
    "build_run_contract",
    "combined_account_order_sha256",
    "ensure_schedule_manifest",
    "load_schedule",
    "main",
    "parse_account_usernames",
    "plan_consumer_wave_workers",
    "preflight_account_pools",
    "read_queue_state",
    "run_graph",
    "run_contract_sha256",
    "username_order_sha256",
    "validate_worker_slices",
]
