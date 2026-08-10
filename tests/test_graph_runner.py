"""The formal graph runner freezes resources before streaming subprocess work."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import sys

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from extensions.sop_v2.pipeline import graph_runner as graph  # noqa: E402
from extensions.sop_v2.pipeline import resource_leases  # noqa: E402


BATCH = "B-GRAPH-RUNNER"


def _account_line(username: str, *, valid: bool = True) -> str:
    cookies = "sessionid=secret; ds_user_id=123" if valid else "csrftoken=x"
    return f"{username}|password|totp|{cookies}|unused"


def _pool_fixture(
    tmp_path: Path,
    *,
    shallow: tuple[str, ...] = ("shallow.one", "shallow.two"),
    deep: tuple[str, ...] = ("deep.one", "deep.two", "deep.three", "deep.four"),
) -> tuple[Path, Path, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    shallow_file = tmp_path / "accounts_raw.txt"
    deep_file = tmp_path / "accounts_deep.txt"
    shallow_file.write_text(
        "\n".join([_account_line("ignored", valid=False), *map(_account_line, shallow)]),
        encoding="utf-8",
    )
    deep_file.write_text("\n".join(map(_account_line, deep)), encoding="utf-8")
    profile_root = tmp_path / "profiles"
    for username in (*shallow, *deep):
        (profile_root / username).mkdir(parents=True, exist_ok=True)
    return shallow_file, deep_file, profile_root


def _config(
    tmp_path: Path,
    *,
    resume: bool = False,
    resume_from_stage3: bool = False,
    worker_count: int = 2,
) -> graph.GraphConfig:
    shallow, deep, profiles = _pool_fixture(tmp_path)
    return graph.GraphConfig(
        batch_id=BATCH,
        creator_db=tmp_path / "creator.db",
        lease_db=tmp_path / "leases.db",
        stage1_artifact=tmp_path / "stage1.json",
        round_contract=None if resume_from_stage3 else tmp_path / "contract.json",
        shallow_accounts_file=shallow,
        deep_accounts_file=deep,
        profile_root=profiles,
        schedule_path=tmp_path / "schedule.json",
        worker_count=worker_count,
        worker_limit=3,
        max_consumer_waves=4,
        poll_interval_seconds=0,
        lease_ttl_seconds=60,
        heartbeat_interval_seconds=30,
        resume=resume,
        resume_from_stage3=resume_from_stage3,
        workspace_root=tmp_path,
        run_id="run-test",
    )


class _FakeLeaseAPI:
    def __init__(self) -> None:
        self.acquired: list[resource_leases.LeaseBundle] = []
        self.released: list[resource_leases.LeaseBundle] = []

    def acquire_resources(self, _db, **kwargs):
        now = datetime(2026, 8, 10, tzinfo=timezone.utc)
        bundle = resource_leases.LeaseBundle(
            batch_id=kwargs["batch_id"],
            run_id=kwargs["run_id"],
            wave_id=kwargs["wave_id"],
            worker_id=kwargs["worker_id"],
            resource_keys=tuple(sorted(kwargs["resource_keys"])),
            token=f"token-{len(self.acquired)}",
            acquired_at=now.isoformat(),
            heartbeat_at=now.isoformat(),
            expires_at=(now + timedelta(seconds=kwargs["ttl_seconds"])).isoformat(),
        )
        self.acquired.append(bundle)
        return bundle

    def heartbeat_resources(self, _db, bundle, **_kwargs):
        return bundle

    def release_resources(self, _db, bundle, **_kwargs):
        self.released.append(bundle)
        return len(bundle.resource_keys)


class _FakeProcess:
    def __init__(self, returncode: int | None = None) -> None:
        self.returncode = returncode
        self._event = asyncio.Event()
        if returncode is not None:
            self._event.set()

    def finish(self, returncode: int = 0) -> None:
        self.returncode = returncode
        self._event.set()

    async def wait(self) -> int:
        await self._event.wait()
        assert self.returncode is not None
        return self.returncode

    def terminate(self) -> None:
        if self.returncode is None:
            self.finish(-15)


class _TermIgnoringProcess(_FakeProcess):
    def __init__(self) -> None:
        super().__init__()
        self.signals: list[str] = []

    def terminate(self) -> None:
        self.signals.append("TERM")

    def kill(self) -> None:
        self.signals.append("KILL")
        self.finish(-9)


async def _yielding_sleep(_seconds: float) -> None:
    await asyncio.sleep(0)


def _command_option(command: tuple[str, ...] | list[str], option: str) -> str:
    index = command.index(option)
    return command[index + 1]


def test_account_parser_preflight_and_balanced_slices_are_deterministic(tmp_path):
    shallow_file, deep_file, profiles = _pool_fixture(tmp_path)

    assert graph.parse_account_usernames(shallow_file) == (
        "shallow.one",
        "shallow.two",
    )
    pools = graph.preflight_account_pools(shallow_file, deep_file, profiles)
    workers = graph.build_worker_slices(pools.deep, worker_count=2, limit=24)

    assert pools.deep_order_sha256 == graph.username_order_sha256(
        ("deep.one", "deep.two", "deep.three", "deep.four")
    )
    assert [(worker.account_offset, worker.account_count) for worker in workers] == [
        (0, 2),
        (2, 2),
    ]
    assert set(workers[0].usernames).isdisjoint(workers[1].usernames)
    assert set(workers[0].profile_paths).isdisjoint(workers[1].profile_paths)
    assert all(worker.limit == 24 and worker.account_count > 0 for worker in workers)


@pytest.mark.parametrize(
    ("eligible", "expected_limits"),
    [
        (20, (10, 10)),
        (45, (23, 22)),
        (1, (1, 1)),
        (0, ()),
        (48, (24, 24)),
        (49, (24, 24)),
    ],
)
def test_consumer_wave_claim_caps_are_balanced_bounded_and_deterministic(
    tmp_path, eligible, expected_limits
):
    shallow, deep, profiles = _pool_fixture(tmp_path)
    pools = graph.preflight_account_pools(shallow, deep, profiles)
    frozen_workers = graph.build_worker_slices(
        pools.deep, worker_count=2, limit=24
    )

    planned = graph.plan_consumer_wave_workers(
        frozen_workers,
        eligible_qualified_count=eligible,
        configured_worker_limit=24,
    )

    assert tuple(worker.limit for worker in planned) == expected_limits
    if eligible:
        # A positive wave leases the entire frozen pool.  For a one-row tail both
        # workers use cap=1; atomic claims ensure only one receives that row.
        assert tuple(worker.worker_id for worker in planned) == tuple(
            worker.worker_id for worker in frozen_workers
        )
        assert all(worker.limit <= 24 for worker in planned)
        assert max(worker.limit for worker in planned) - min(
            worker.limit for worker in planned
        ) <= 1
        assert sum(worker.limit for worker in planned) >= min(eligible, 48)
        assert [worker.accounts for worker in planned] == [
            worker.accounts for worker in frozen_workers
        ]


def test_stage3_command_rotates_within_frozen_worker_slice():
    worker = graph.WorkerSlice(
        worker_id="stage3-w1",
        limit=24,
        account_offset=7,
        account_count=3,
        accounts=(
            graph.AccountSpec("deep.seven", Path("/profiles/deep.seven")),
            graph.AccountSpec("deep.eight", Path("/profiles/deep.eight")),
            graph.AccountSpec("deep.nine", Path("/profiles/deep.nine")),
        ),
    )
    config = graph.GraphConfig(
        batch_id=BATCH,
        creator_db=Path("creator.db"),
        lease_db=Path("leases.db"),
        stage1_artifact=Path("stage1.json"),
        shallow_accounts_file=Path("shallow.txt"),
        deep_accounts_file=Path("deep.txt"),
        profile_root=Path("profiles"),
        schedule_path=Path("schedule.json"),
    )

    wave1 = graph._stage3_command(config, worker, account_rotation=0)
    wave2 = graph._stage3_command(config, worker, account_rotation=1)

    assert wave1 != wave2
    assert _command_option(wave1, "--account-offset") == "7"
    assert _command_option(wave2, "--account-offset") == "7"
    assert _command_option(wave1, "--account-count") == "3"
    assert _command_option(wave2, "--account-count") == "3"
    assert _command_option(wave1, "--account-rotation") == "0"
    assert _command_option(wave2, "--account-rotation") == "1"


def test_cli_forwards_account_rotation_base_and_defaults_to_zero(
    tmp_path, monkeypatch, capsys
):
    default_args = graph._parser().parse_args(
        ["--batch-id", BATCH, "--stage1-artifact", str(tmp_path / "stage1.json")]
    )
    assert default_args.account_rotation_base == 0

    captured: list[graph.GraphConfig] = []

    async def fake_run_graph(config):
        captured.append(config)
        return graph.GraphRunResult(
            batch_id=config.batch_id,
            run_id="run-cli",
            producer_started=False,
            consumer_waves_started=0,
            schedule_path=config.schedule_path,
            event_log_path=config.event_log_path,
            final_queue=graph.QueueState(0, 0, 0),
            b2=True,
        )

    monkeypatch.setattr(graph, "run_graph", fake_run_graph)
    schedule = tmp_path / "schedule.json"
    event_log = tmp_path / "events.jsonl"
    assert graph.main(
        [
            "--batch-id",
            BATCH,
            "--stage1-artifact",
            str(tmp_path / "stage1.json"),
            "--schedule",
            str(schedule),
            "--event-log",
            str(event_log),
            "--resume",
            "--resume-from-stage3",
            "--account-rotation-base",
            "7",
        ]
    ) == 0

    assert len(captured) == 1
    assert captured[0].account_rotation_base == 7
    assert captured[0].schedule_path == schedule
    assert captured[0].event_log_path == event_log
    assert json.loads(capsys.readouterr().out)["run_id"] == "run-cli"


def test_account_rotation_base_must_be_non_negative(tmp_path):
    with pytest.raises(ValueError, match="account_rotation_base.*non-negative"):
        graph._validate_config(
            replace(_config(tmp_path), account_rotation_base=-1)
        )


def test_account_parser_excludes_empty_cookie_values_from_frozen_order(tmp_path):
    source = tmp_path / "accounts.txt"
    source.write_text(
        "\n".join(
            [
                "empty_session|pw|totp|sessionid=; ds_user_id=1",
                "empty_user_id|pw|totp|sessionid=abc; ds_user_id=",
                "last_empty_wins|pw|totp|sessionid=abc; sessionid=; ds_user_id=1",
                "encoded_session|pw|totp|sessionid=abc%2Fdef; ds_user_id=2",
            ]
        ),
        encoding="utf-8",
    )

    assert graph.parse_account_usernames(source) == ("encoded_session",)
    assert graph.username_order_sha256(graph.parse_account_usernames(source)) == (
        graph.username_order_sha256(("encoded_session",))
    )


def test_preflight_rejects_pool_overlap_duplicate_and_active_profile(tmp_path):
    shallow, deep, profiles = _pool_fixture(
        tmp_path, shallow=("same.user",), deep=("same.user", "deep.two")
    )
    with pytest.raises(graph.AccountPreflightError, match="overlap"):
        graph.preflight_account_pools(shallow, deep, profiles)

    shallow, deep, profiles = _pool_fixture(
        tmp_path / "active", shallow=("shallow",), deep=("deep",)
    )
    (profiles / "deep" / "SingletonLock").symlink_to("live-chrome")
    with pytest.raises(graph.AccountPreflightError, match="appears active"):
        graph.preflight_account_pools(shallow, deep, profiles)


def test_manifest_is_atomic_append_only_and_resume_binds_account_order(tmp_path):
    shallow, deep, profiles = _pool_fixture(tmp_path)
    pools = graph.preflight_account_pools(shallow, deep, profiles)
    config = replace(_config(tmp_path), worker_limit=5)
    workers = graph.build_worker_slices(pools.deep, worker_count=2, limit=5)
    commands = {
        worker.worker_id: graph._stage3_command(config, worker)
        for worker in workers
    }
    wave1 = graph._consumer_wave(
        "wave-0001-stage3",
        config,
        workers,
        workers,
        commands,
        eligible_qualified_count=10,
    )
    run_contract = graph.build_run_contract(config, pools, workers)
    schedule_path = tmp_path / "append-only.json"

    first = graph.append_wave_manifest(
        schedule_path,
        batch_id=BATCH,
        run_id="run-1",
        pools=pools,
        wave=wave1,
        resume=False,
        run_contract=run_contract,
    )
    frozen_first_wave = json.loads(json.dumps(first["waves"][0]))
    wave2 = json.loads(json.dumps(wave1))
    wave2["wave_id"] = "wave-0002-stage3"
    for worker in wave2["workers"]:
        rotation_index = worker["command"].index("--account-rotation") + 1
        worker["command"][rotation_index] = "1"
    second = graph.append_wave_manifest(
        schedule_path,
        batch_id=BATCH,
        run_id="run-1",
        pools=pools,
        wave=wave2,
        resume=True,
        run_contract=run_contract,
    )

    assert second["waves"][0] == frozen_first_wave
    assert [wave["wave_id"] for wave in second["waves"]] == [
        "wave-0001-stage3",
        "wave-0002-stage3",
    ]
    assert json.loads(schedule_path.read_text(encoding="utf-8")) == second
    with pytest.raises(graph.ScheduleError, match="already exists"):
        graph.append_wave_manifest(
            schedule_path,
            batch_id=BATCH,
            run_id="run-1",
            pools=pools,
            wave=wave2,
            resume=True,
            run_contract=run_contract,
        )

    oversized = json.loads(json.dumps(wave2))
    oversized["wave_id"] = "wave-0003-stage3"
    oversized["claim_plan"].update(
        {
            "eligible_qualified_count": 12,
            "planned_claim_count": 12,
            "allocated_claim_capacity": 12,
            "configured_worker_limit": 6,
            "claim_caps": [
                {"worker_id": "stage3-w1", "limit": 6},
                {"worker_id": "stage3-w2", "limit": 6},
            ],
        }
    )
    for worker in oversized["workers"]:
        worker["limit"] = 6
        limit_index = worker["command"].index("--limit") + 1
        worker["command"][limit_index] = "6"
    before_rejected_append = schedule_path.read_bytes()
    with pytest.raises(graph.ScheduleError, match="frozen worker cap"):
        graph.append_wave_manifest(
            schedule_path,
            batch_id=BATCH,
            run_id="run-1",
            pools=pools,
            wave=oversized,
            resume=True,
            run_contract=run_contract,
        )
    assert schedule_path.read_bytes() == before_rejected_append

    reordered_file = tmp_path / "deep-reordered.txt"
    reordered_file.write_text(
        "\n".join(
            map(
                _account_line,
                ("deep.two", "deep.one", "deep.three", "deep.four"),
            )
        ),
        encoding="utf-8",
    )
    reordered = graph.preflight_account_pools(shallow, reordered_file, profiles)
    with pytest.raises(graph.ResumeMismatchError, match="order SHA"):
        graph.append_wave_manifest(
            schedule_path,
            batch_id=BATCH,
            run_id="run-1",
            pools=reordered,
            wave={**wave2, "wave_id": "wave-0003-stage3"},
            resume=True,
            run_contract=run_contract,
        )


def test_graph_streams_consumers_and_never_closes_on_temporary_empty_queue(tmp_path):
    config = _config(tmp_path)
    lease_api = _FakeLeaseAPI()
    producer = _FakeProcess()
    process_commands: list[tuple[str, ...]] = []
    probes = 0
    stage3_spawns = 0
    state = graph.QueueState(seed_count=1, qualified_count=0, qualified_unlocked_count=0)

    async def process_factory(*command, **_kwargs):
        nonlocal stage3_spawns, state
        command = tuple(command)
        process_commands.append(command)
        # The wave containing this exact command must already be durable.
        persisted = graph.load_schedule(config.schedule_path)
        assert any(
            command == tuple(entry.get("command", ()))
            for wave in persisted["waves"]
            for entry in (
                [wave["producer"]]
                if wave["kind"] == "stage2_producer"
                else wave["workers"]
            )
        )
        if any("stage2_qualify" in part for part in command):
            return producer
        stage3_spawns += 1
        if stage3_spawns == config.worker_count:
            state = graph.QueueState(
                seed_count=0,
                qualified_count=0,
                qualified_unlocked_count=0,
                collected_count=1,
            )
        return _FakeProcess(0)

    async def queue_probe():
        nonlocal probes, state
        probes += 1
        if probes == 2:
            # Work appears while the producer is still part of the live graph.
            state = graph.QueueState(
                seed_count=0,
                qualified_count=1,
                qualified_unlocked_count=1,
            )
            producer.finish(0)
        return state

    result = asyncio.run(
        graph.run_graph(
            config,
            process_factory=process_factory,
            queue_probe=queue_probe,
            b1_evaluator=lambda: True,
            b2_evaluator=lambda: True,
            lease_api=lease_api,
            sleep=_yielding_sleep,
        )
    )

    assert probes >= 2  # first empty snapshot did not terminate the graph
    assert result.producer_started is True
    assert result.consumer_waves_started == 1
    assert result.final_queue.qualified_count == 0
    assert any("stage2_qualify" in part for part in process_commands[0])
    assert sum(
        any("stage3_collect" in part for part in command)
        for command in process_commands
    ) == 2
    assert len(lease_api.released) == len(lease_api.acquired) == 3

    schedule = graph.load_schedule(config.schedule_path)
    assert [wave["kind"] for wave in schedule["waves"]] == [
        "stage2_producer",
        "stage3_consumers",
    ]
    assert all(
        worker["limit"] > 0 and worker["account_count"] > 0
        for worker in schedule["waves"][1]["workers"]
    )


def test_explicit_stage3_resume_uses_b2_and_does_not_start_stage2(tmp_path):
    config = _config(tmp_path, resume=True, resume_from_stage3=True)
    lease_api = _FakeLeaseAPI()
    state = graph.QueueState(
        seed_count=0, qualified_count=1, qualified_unlocked_count=1
    )
    commands: list[tuple[str, ...]] = []

    async def process_factory(*command, **_kwargs):
        nonlocal state
        commands.append(tuple(command))
        if len(commands) == config.worker_count:
            state = graph.QueueState(
                seed_count=0,
                qualified_count=0,
                qualified_unlocked_count=0,
                collected_count=1,
            )
        return _FakeProcess(0)

    def forbidden_b1():
        raise AssertionError("B1 must not be evaluated against a post-Stage2 database")

    result = asyncio.run(
        graph.run_graph(
            config,
            process_factory=process_factory,
            queue_probe=lambda: state,
            b1_evaluator=forbidden_b1,
            b2_evaluator=lambda: True,
            lease_api=lease_api,
            sleep=_yielding_sleep,
        )
    )

    assert result.producer_started is False
    assert result.consumer_waves_started == 1
    assert len(commands) == config.worker_count
    assert {
        _command_option(command, "--limit") for command in commands
    } == {"1"}
    assert {
        _command_option(command, "--account-rotation") for command in commands
    } == {"0"}
    assert commands and all(
        any("stage3_collect" in part for part in command) for command in commands
    )
    assert all(
        not any("stage2_qualify" in part for part in command)
        for command in commands
    )

    wave = graph.load_schedule(config.schedule_path)["waves"][0]
    assert wave["claim_plan"] == {
        "policy": graph.CONSUMER_CLAIM_SHARDING_POLICY,
        "eligible_qualified_count": 1,
        "planned_claim_count": 1,
        "allocated_claim_capacity": 2,
        "configured_worker_limit": 3,
        "configured_worker_ids": ["stage3-w1", "stage3-w2"],
        "active_worker_ids": ["stage3-w1", "stage3-w2"],
        "claim_caps": [
            {"worker_id": "stage3-w1", "limit": 1},
            {"worker_id": "stage3-w2", "limit": 1},
        ],
    }


def test_zero_eligible_rows_do_not_create_or_spawn_a_consumer_wave(tmp_path):
    config = _config(tmp_path, resume=True, resume_from_stage3=True)
    lease_api = _FakeLeaseAPI()

    result = asyncio.run(
        graph.run_graph(
            config,
            process_factory=lambda *_args, **_kwargs: pytest.fail(
                "zero eligible rows must not spawn"
            ),
            queue_probe=lambda: graph.QueueState(
                seed_count=0,
                qualified_count=0,
                qualified_unlocked_count=0,
            ),
            b2_evaluator=lambda: True,
            lease_api=lease_api,
            sleep=_yielding_sleep,
        )
    )

    assert result.consumer_waves_started == 0
    assert graph.load_schedule(config.schedule_path)["waves"] == []
    assert lease_api.acquired == []


@pytest.mark.parametrize("queue_during_spawn", [1, 45])
def test_wave_claim_caps_stay_frozen_if_queue_changes_during_spawn(
    tmp_path, queue_during_spawn
):
    config = replace(
        _config(tmp_path, resume=True, resume_from_stage3=True),
        worker_limit=24,
    )
    state = graph.QueueState(
        seed_count=0,
        qualified_count=20,
        qualified_unlocked_count=20,
    )
    commands: list[tuple[str, ...]] = []

    async def process_factory(*command, **_kwargs):
        nonlocal state
        commands.append(tuple(command))
        if len(commands) == 1:
            state = graph.QueueState(
                seed_count=0,
                qualified_count=queue_during_spawn,
                qualified_unlocked_count=queue_during_spawn,
            )
        else:
            state = graph.QueueState(
                seed_count=0,
                qualified_count=0,
                qualified_unlocked_count=0,
                collected_count=20,
            )
        return _FakeProcess(0)

    asyncio.run(
        graph.run_graph(
            config,
            process_factory=process_factory,
            queue_probe=lambda: state,
            b2_evaluator=lambda: True,
            lease_api=_FakeLeaseAPI(),
            sleep=_yielding_sleep,
        )
    )

    assert [_command_option(command, "--limit") for command in commands] == [
        "10",
        "10",
    ]
    wave = graph.load_schedule(config.schedule_path)["waves"][0]
    assert wave["claim_plan"]["eligible_qualified_count"] == 20
    assert wave["claim_plan"]["claim_caps"] == [
        {"worker_id": "stage3-w1", "limit": 10},
        {"worker_id": "stage3-w2", "limit": 10},
    ]


def test_subprocess_nonzero_is_propagated_with_exact_status(tmp_path):
    config = _config(tmp_path)
    lease_api = _FakeLeaseAPI()

    async def process_factory(*command, **_kwargs):
        assert any("stage2_qualify" in part for part in command)
        return _FakeProcess(7)

    with pytest.raises(graph.SubprocessFailed) as error:
        asyncio.run(
            graph.run_graph(
                config,
                process_factory=process_factory,
                queue_probe=lambda: graph.QueueState(
                    seed_count=1,
                    qualified_count=0,
                    qualified_unlocked_count=0,
                ),
                b1_evaluator=lambda: True,
                b2_evaluator=lambda: True,
                lease_api=lease_api,
                sleep=_yielding_sleep,
            )
        )

    assert error.value.returncode == 7
    assert error.value.worker_id == "stage2-producer"
    assert len(lease_api.released) == len(lease_api.acquired) == 1


def test_multiple_workers_reject_unbounded_or_overlapping_slices(tmp_path):
    shallow, deep, profiles = _pool_fixture(tmp_path)
    pools = graph.preflight_account_pools(shallow, deep, profiles)
    with pytest.raises(ValueError, match="positive"):
        graph.build_worker_slices(pools.deep, worker_count=2, limit=0)

    workers = list(graph.build_worker_slices(pools.deep, worker_count=2, limit=2))
    workers[1] = graph.WorkerSlice(
        worker_id=workers[1].worker_id,
        limit=2,
        account_offset=1,
        account_count=2,
        accounts=workers[1].accounts,
    )
    with pytest.raises(ValueError, match="overlap"):
        graph.validate_worker_slices(workers)


def test_queue_probe_is_explicit_read_only_and_distinguishes_unlocked(tmp_path):
    db = tmp_path / "queue.db"
    with sqlite3.connect(db) as connection:
        connection.execute(
            "CREATE TABLE creator_profiles "
            "(status TEXT, locked_at TEXT, discovery_batch TEXT)"
        )
        connection.executemany(
            "INSERT INTO creator_profiles VALUES (?,?,?)",
            [
                ("seed", None, BATCH),
                ("qualified", None, BATCH),
                ("qualified", "owned-token", BATCH),
                ("collected", None, BATCH),
                ("qualified", None, "OTHER"),
            ],
        )

    assert graph.read_queue_state(db, BATCH) == graph.QueueState(
        seed_count=1,
        qualified_count=2,
        qualified_unlocked_count=1,
        collected_count=1,
        lock_count=1,
    )
    assert not (tmp_path / "missing.db").exists()
    with pytest.raises(graph.GraphRunnerError, match="read failed"):
        graph.read_queue_state(tmp_path / "missing.db", BATCH)
    assert not (tmp_path / "missing.db").exists()


def test_real_worker_binding_preflight_rejects_leasing_a_different_pool(tmp_path):
    config = _config(tmp_path)
    with pytest.raises(graph.AccountPreflightError, match="binding mismatch"):
        graph._assert_worker_pool_bindings(config)


def test_termination_escalates_to_kill_without_an_unbounded_wait():
    process = _TermIgnoringProcess()
    assert asyncio.run(graph._terminate_process(process, grace_seconds=0.001)) is True
    assert process.signals == ["TERM", "KILL"]
    assert process.returncode == -9


def test_resume_rejects_changed_semantic_run_contract_before_spawning(tmp_path):
    config = _config(tmp_path, resume=True, resume_from_stage3=True)
    lease_api = _FakeLeaseAPI()
    state = graph.QueueState(
        seed_count=0, qualified_count=0, qualified_unlocked_count=0
    )
    asyncio.run(
        graph.run_graph(
            config,
            process_factory=lambda *_args, **_kwargs: pytest.fail("no subprocess"),
            queue_probe=lambda: state,
            b2_evaluator=lambda: True,
            lease_api=lease_api,
            sleep=_yielding_sleep,
        )
    )

    changed = replace(config, posts=11, run_id=None)
    with pytest.raises(graph.ResumeMismatchError, match="run contract changed"):
        asyncio.run(
            graph.run_graph(
                changed,
                process_factory=lambda *_args, **_kwargs: pytest.fail("no subprocess"),
                queue_probe=lambda: state,
                b2_evaluator=lambda: True,
                lease_api=lease_api,
                sleep=_yielding_sleep,
            )
        )

    changed_rotation_base = replace(
        config, account_rotation_base=1, run_id=None
    )
    with pytest.raises(graph.ResumeMismatchError, match="run contract changed"):
        asyncio.run(
            graph.run_graph(
                changed_rotation_base,
                process_factory=lambda *_args, **_kwargs: pytest.fail(
                    "rotation-base drift must fail before spawning"
                ),
                queue_probe=lambda: state,
                b2_evaluator=lambda: True,
                lease_api=lease_api,
                sleep=_yielding_sleep,
            )
        )


def test_resume_rejects_changed_transitive_worker_dependency(tmp_path):
    config = _config(tmp_path, resume=True, resume_from_stage3=True)
    state = graph.QueueState(
        seed_count=0, qualified_count=0, qualified_unlocked_count=0
    )
    asyncio.run(
        graph.run_graph(
            config,
            process_factory=lambda *_args, **_kwargs: pytest.fail("no subprocess"),
            queue_probe=lambda: state,
            b2_evaluator=lambda: True,
            lease_api=_FakeLeaseAPI(),
            sleep=_yielding_sleep,
        )
    )

    dependency = tmp_path / "scripts/extensions/sop_v2/comments.py"
    dependency.parent.mkdir(parents=True, exist_ok=True)
    dependency.write_text("# semantic drift\n", encoding="utf-8")

    with pytest.raises(graph.ResumeMismatchError, match="run contract changed"):
        asyncio.run(
            graph.run_graph(
                replace(config, run_id=None),
                process_factory=lambda *_args, **_kwargs: pytest.fail(
                    "drift must fail before spawning"
                ),
                queue_probe=lambda: state,
                b2_evaluator=lambda: True,
                lease_api=_FakeLeaseAPI(),
                sleep=_yielding_sleep,
            )
        )


def test_resume_does_not_reset_persisted_consumer_wave_budget(tmp_path):
    config = replace(
        _config(tmp_path, resume=True, resume_from_stage3=True),
        max_consumer_waves=1,
        max_no_progress_waves=4,
    )
    state = graph.QueueState(
        seed_count=0,
        qualified_count=1,
        qualified_unlocked_count=1,
    )
    first_spawns = 0

    async def first_factory(*_command, **_kwargs):
        nonlocal first_spawns
        first_spawns += 1
        return _FakeProcess(0)

    with pytest.raises(graph.GraphIncompleteError, match="max_consumer_waves"):
        asyncio.run(
            graph.run_graph(
                config,
                process_factory=first_factory,
                queue_probe=lambda: state,
                b2_evaluator=lambda: True,
                lease_api=_FakeLeaseAPI(),
                sleep=_yielding_sleep,
            )
        )
    assert first_spawns == config.worker_count

    resumed_spawns = 0

    async def forbidden_resume_factory(*_command, **_kwargs):
        nonlocal resumed_spawns
        resumed_spawns += 1
        return _FakeProcess(0)

    with pytest.raises(graph.GraphIncompleteError, match="max_consumer_waves"):
        asyncio.run(
            graph.run_graph(
                replace(config, run_id=None),
                process_factory=forbidden_resume_factory,
                queue_probe=lambda: state,
                b2_evaluator=lambda: True,
                lease_api=_FakeLeaseAPI(),
                sleep=_yielding_sleep,
            )
        )
    assert resumed_spawns == 0


def test_crash_resume_preserves_claim_plan_and_advances_rotation(tmp_path):
    config = replace(
        _config(tmp_path, resume=True, resume_from_stage3=True),
        worker_limit=24,
        account_rotation_base=3,
    )
    state = graph.QueueState(
        seed_count=0,
        qualified_count=20,
        qualified_unlocked_count=20,
    )
    first_commands: list[tuple[str, ...]] = []

    async def interrupted_factory(*command, **_kwargs):
        first_commands.append(tuple(command))
        # Persist a complete first wave intent, then model one worker failure.
        return _FakeProcess(9 if len(first_commands) == 1 else 0)

    with pytest.raises(graph.SubprocessFailed):
        asyncio.run(
            graph.run_graph(
                config,
                process_factory=interrupted_factory,
                queue_probe=lambda: state,
                b2_evaluator=lambda: True,
                lease_api=_FakeLeaseAPI(),
                sleep=_yielding_sleep,
            )
        )

    first_schedule = graph.load_schedule(config.schedule_path)
    first_wave = next(
        wave for wave in first_schedule["waves"] if wave["kind"] == "stage3_consumers"
    )
    assert {
        _command_option(worker["command"], "--account-rotation")
        for worker in first_wave["workers"]
    } == {"3"}
    assert [worker["limit"] for worker in first_wave["workers"]] == [10, 10]
    assert first_wave["claim_plan"]["active_worker_ids"] == [
        "stage3-w1",
        "stage3-w2",
    ]

    resumed_commands: list[tuple[str, ...]] = []

    async def resumed_factory(*command, **_kwargs):
        nonlocal state
        resumed_commands.append(tuple(command))
        if len(resumed_commands) == config.worker_count:
            state = graph.QueueState(
                seed_count=0,
                qualified_count=0,
                qualified_unlocked_count=0,
                collected_count=20,
            )
        return _FakeProcess(0)

    result = asyncio.run(
        graph.run_graph(
            replace(config, run_id=None),
            process_factory=resumed_factory,
            queue_probe=lambda: state,
            b2_evaluator=lambda: True,
            lease_api=_FakeLeaseAPI(),
            sleep=_yielding_sleep,
        )
    )

    assert result.consumer_waves_started == 1
    assert {
        _command_option(command, "--account-rotation")
        for command in resumed_commands
    } == {"4"}
    resumed_schedule = graph.load_schedule(config.schedule_path)
    consumer_waves = [
        wave
        for wave in resumed_schedule["waves"]
        if wave["kind"] == "stage3_consumers"
    ]
    assert len(consumer_waves) == 2
    assert [worker["limit"] for worker in consumer_waves[1]["workers"]] == [10, 10]
    assert consumer_waves[1]["claim_plan"] == {
        **consumer_waves[0]["claim_plan"],
    }
    assert consumer_waves[0]["workers"][0]["command"] != (
        consumer_waves[1]["workers"][0]["command"]
    )


def test_resume_rejects_tampered_persisted_claim_caps_before_spawning(tmp_path):
    config = replace(
        _config(tmp_path, resume=True, resume_from_stage3=True),
        worker_limit=24,
    )
    state = graph.QueueState(
        seed_count=0,
        qualified_count=20,
        qualified_unlocked_count=20,
    )
    spawn_count = 0

    async def process_factory(*_command, **_kwargs):
        nonlocal spawn_count, state
        spawn_count += 1
        if spawn_count == config.worker_count:
            state = graph.QueueState(
                seed_count=0,
                qualified_count=0,
                qualified_unlocked_count=0,
                collected_count=20,
            )
        return _FakeProcess(0)

    asyncio.run(
        graph.run_graph(
            config,
            process_factory=process_factory,
            queue_probe=lambda: state,
            b2_evaluator=lambda: True,
            lease_api=_FakeLeaseAPI(),
            sleep=_yielding_sleep,
        )
    )

    schedule = json.loads(config.schedule_path.read_text(encoding="utf-8"))
    wave = schedule["waves"][0]
    wave["claim_plan"].update(
        {
            "eligible_qualified_count": 22,
            "planned_claim_count": 22,
            "allocated_claim_capacity": 22,
            "claim_caps": [
                {"worker_id": "stage3-w1", "limit": 11},
                {"worker_id": "stage3-w2", "limit": 11},
            ],
        }
    )
    for worker in wave["workers"]:
        worker["limit"] = 11
        limit_index = worker["command"].index("--limit") + 1
        worker["command"][limit_index] = "11"
    config.schedule_path.write_text(
        json.dumps(schedule, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    resumed_spawns = 0

    async def forbidden_factory(*_command, **_kwargs):
        nonlocal resumed_spawns
        resumed_spawns += 1
        return _FakeProcess(0)

    with pytest.raises(graph.EventLogError, match="intent fingerprint mismatch"):
        asyncio.run(
            graph.run_graph(
                replace(config, run_id=None),
                process_factory=forbidden_factory,
                queue_probe=lambda: state,
                b2_evaluator=lambda: True,
                lease_api=_FakeLeaseAPI(),
                sleep=_yielding_sleep,
            )
        )
    assert resumed_spawns == 0
