"""Strict two-phase Storefront backfill never degrades the canonical candidate."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from extensions.sop_v2.pipeline import resource_leases  # noqa: E402
from extensions.sop_v2.pipeline import storefront_backfill as backfill  # noqa: E402


BATCH = "TEST-STOREFRONT"


def _candidate(handle: str) -> dict:
    return {
        "handle": handle,
        "full_name": handle.title(),
        "storefront_status": "unknown",
        "deep_evidence": {
            "posts": [{"url": f"https://instagram.com/p/{handle}/", "comments": 9}],
            "comments": [{"text": "je veux acheter", "translated_zh": "我想购买"}],
        },
        "pricing_estimate": {
            "status": "complete",
            "average_views": 12000,
            "estimated_cost_usd": 420,
        },
        "comment_translations": [
            {
                "original_text": "je veux acheter",
                "translated_zh": "我想购买",
                "source_hash": "translation-hash",
            }
        ],
        "stage3_attempt_ledger": [
            {"attempt_id": "attempt-1", "evidence_sha256": "deep-hash"}
        ],
        "stage3_canonical_attempt": "attempt-1",
        "pricing_canonical_attempt": "pricing-1",
        "deep_retry_status": "complete",
    }


def _create_db(path: Path, handles: tuple[str, ...] = ("alpha", "beta")) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            """
            CREATE TABLE creator_profiles (
                handle TEXT PRIMARY KEY,
                stage_json TEXT NOT NULL,
                storefront_status TEXT,
                amazon_storefront_link TEXT,
                status TEXT,
                client_status TEXT,
                stage_error TEXT,
                locked_at TEXT,
                stage_updated_at TEXT,
                discovery_batch TEXT,
                unrelated_column TEXT
            )
            """
        )
        for handle in handles:
            connection.execute(
                """
                INSERT INTO creator_profiles(
                    handle,stage_json,storefront_status,amazon_storefront_link,
                    status,client_status,stage_error,locked_at,stage_updated_at,
                    discovery_batch,unrelated_column
                ) VALUES (?,?,?,NULL,'decided',NULL,NULL,NULL,?,?,?)
                """,
                (
                    handle,
                    json.dumps(_candidate(handle), ensure_ascii=False),
                    "unknown",
                    "2026-08-11T08:00:00Z",
                    BATCH,
                    f"protected-{handle}",
                ),
            )
        connection.commit()
    finally:
        connection.close()


def _account_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    shallow = tmp_path / "accounts-shallow.txt"
    deep = tmp_path / "accounts-deep.txt"
    shallow.write_text(
        "shallow_one|pw|totp|sessionid=session-one; ds_user_id=101\n"
        "shallow_two|pw|totp|sessionid=session-three; ds_user_id=303\n",
        encoding="utf-8",
    )
    deep.write_text(
        "deep_one|pw|totp|sessionid=session-two; ds_user_id=202\n",
        encoding="utf-8",
    )
    profiles = tmp_path / "profiles"
    (profiles / "shallow_one").mkdir(parents=True)
    (profiles / "shallow_two").mkdir(parents=True)
    (profiles / "deep_one").mkdir(parents=True)
    return shallow, deep, profiles


def _account_fixture_many(
    tmp_path: Path, count: int = 6
) -> tuple[Path, Path, Path, tuple[str, ...]]:
    usernames = tuple(f"shallow_{index}" for index in range(count))
    shallow = tmp_path / "accounts-shallow-many.txt"
    deep = tmp_path / "accounts-deep-many.txt"
    shallow.write_text(
        "".join(
            f"{username}|pw|totp|sessionid=session-{index}; ds_user_id={1000 + index}\n"
            for index, username in enumerate(usernames)
        ),
        encoding="utf-8",
    )
    deep.write_text(
        "deep_many|pw|totp|sessionid=deep-session; ds_user_id=9000\n",
        encoding="utf-8",
    )
    profiles = tmp_path / "profiles-many"
    for username in (*usernames, "deep_many"):
        (profiles / username).mkdir(parents=True, exist_ok=True)
    return shallow, deep, profiles, usernames


def _proxy(*, session: str, ttl: int) -> dict[str, str]:
    assert session
    assert ttl > 0
    return {
        "server": "http://overseas.tunnel.qg.net:11404",
        "username": "proxy-user",
        "password": "proxy-password",
    }


class _ActiveGuardContext:
    def __init__(self, pages=()):
        self.pages = list(pages)
        self.request_handler = None
        self.websocket_handler = None
        self.page_handler = None

    def route(self, pattern, handler):
        self.route_calls = getattr(self, "route_calls", 0) + 1
        self.request_pattern = pattern
        self.request_handler = handler

    def route_web_socket(self, pattern, handler):
        self.websocket_pattern = pattern
        self.websocket_handler = handler

    def on(self, event, handler):
        assert event == "page"
        self.page_handler = handler

    def add_init_script(self, script):
        self.init_scripts = getattr(self, "init_scripts", []) + [script]

    def unroute_all(self, **_kwargs):
        return None

    def close(self):
        self.closed = True


def _activate_proxy_runtime(runtime, *, pages=()):
    context = _ActiveGuardContext(pages)
    runtime._ctx = context
    runtime._proxy_enforced = True
    for page in pages:
        page.guard_context = context
    return context


def _verified_flags(handle: str = "alpha") -> dict:
    return {
        "_identity_verified": True,
        "_profile_healthy": True,
        "_profile_identity_check": {
            "expected_handle": handle,
            "supplied_handle": handle,
            "page_url_match": True,
            "supplied_handle_match": True,
            "reason": "verified",
            "independent_match": True,
            "og_exact": True,
            "trusted_surface_match": False,
            "strong_contradiction": False,
            "contradictory_surface": False,
            "weak_surface_nonmatch": False,
            "surface_handle_source": None,
            "rejected_surface_handles": [],
            "sources": {
                "og_handle": handle,
                "canonical_handle": handle,
                "surface_handle": None,
            },
        },
        "_profile_health_check": {
            "reason": "healthy",
            "body_text_length": 120,
            "captured_body_text_length": 120,
            "signals": ["full_name"],
        },
    }


def _yes(handle: str, *_args) -> dict:
    url = f"https://amazon.com/shop/{handle}"
    return backfill.resolve_observation(
        handle=handle,
        profile={
            "handle": handle,
            "external_url": url,
            "_bio_has_more": False,
            **_verified_flags(handle),
        },
        profile_url=f"https://www.instagram.com/{handle}/",
        target_checks=[
            {
                "source_url": url,
                "status": "succeeded",
                "commerce_links": [{"url": url, "type": "Amazon"}],
                "page_health": {"healthy": True, "reason": "direct_url"},
            }
        ],
        captured_at="2026-08-11T09:00:00Z",
    )


def _no(handle: str, *_args) -> dict:
    return backfill.resolve_observation(
        handle=handle,
        profile={
            "handle": handle,
            "external_url": None,
            "_bio_has_more": False,
            **_verified_flags(handle),
        },
        profile_url=f"https://www.instagram.com/{handle}/",
        target_checks=[],
        captured_at="2026-08-11T09:00:00Z",
    )


def _mixed_probe(handle: str, *args) -> dict:
    return _yes(handle, *args) if handle == "alpha" else _no(handle, *args)


class _LeaseSpy:
    def __init__(self) -> None:
        self.heartbeats = 0
        self.acquisitions = 0
        self.releases = 0

    def build_resource_keys(self, **kwargs):
        return resource_leases.build_resource_keys(**kwargs)

    def acquire_resources(self, *args, **kwargs):
        self.acquisitions += 1
        return resource_leases.acquire_resources(*args, **kwargs)

    def heartbeat_resources(self, *args, **kwargs):
        self.heartbeats += 1
        return resource_leases.heartbeat_resources(*args, **kwargs)

    def release_resources(self, *args, **kwargs):
        self.releases += 1
        return resource_leases.release_resources(*args, **kwargs)


def _collect(
    tmp_path: Path,
    db: Path,
    *,
    probe=_mixed_probe,
    proxy_loader=_proxy,
    lease_api=resource_leases,
) -> tuple[dict, Path, Path]:
    shallow, deep, profiles = _account_fixture(tmp_path)
    plan_path = tmp_path / "storefront.plan.json"
    lease_db = tmp_path / "resource-leases.db"
    plan = backfill.collect_plan(
        db_path=db,
        batch_id=BATCH,
        expected_status="decided",
        expected_count=2,
        expected_handle_set_sha256=backfill.handle_set_sha256(("alpha", "beta")),
        plan_path=plan_path,
        accounts_file=shallow,
        deep_accounts_file=deep,
        profile_root=profiles,
        lease_db=lease_db,
        per_account=1,
        lease_ttl_seconds=3,
        heartbeat_seconds=1,
        probe_candidate=probe,
        proxy_loader=proxy_loader,
        lease_api=lease_api,
    )
    return plan, plan_path, lease_db


def _unknown(handle: str, *_args) -> dict:
    result = _no(handle)
    result["evidence"]["failures"] = ["external_target_incomplete"]
    result["evidence"]["target_checks_complete"] = False
    return result


def _legacy_plan(plan: dict, path: Path) -> dict:
    legacy = copy.deepcopy(plan)
    legacy["schema"] = backfill.LEGACY_PLAN_SCHEMA
    legacy["collection_contract"].pop("shallow_account_count", None)
    legacy.pop("resume", None)
    for row in legacy["rows"]:
        row.pop("provenance", None)
    legacy["plan_sha256"] = backfill.plan_sha256(legacy)
    path.write_text(
        json.dumps(legacy, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return legacy


def _collect_many(
    tmp_path: Path,
    db: Path,
    *,
    handles: tuple[str, ...],
    plan_name: str,
    probe,
    prior_plan: Path | None = None,
    expected_prior_sha: str | None = None,
    per_account: int = 6,
) -> tuple[dict, Path, tuple[str, ...]]:
    shallow, deep, profiles, usernames = _account_fixture_many(tmp_path)
    plan_path = tmp_path / plan_name
    plan = backfill.collect_plan(
        db_path=db,
        batch_id=BATCH,
        expected_status="decided",
        expected_count=len(handles),
        expected_handle_set_sha256=backfill.handle_set_sha256(handles),
        plan_path=plan_path,
        accounts_file=shallow,
        deep_accounts_file=deep,
        profile_root=profiles,
        lease_db=tmp_path / "resource-leases-many.db",
        per_account=per_account,
        lease_ttl_seconds=3,
        heartbeat_seconds=1,
        probe_candidate=probe,
        proxy_loader=_proxy,
        prior_plan_path=prior_plan,
        expected_prior_plan_sha256=expected_prior_sha,
    )
    return plan, plan_path, usernames


def _resolved_resume_fixture(tmp_path: Path) -> tuple[Path, tuple[str, ...], dict, Path]:
    handles = tuple(f"resume_{index:02d}" for index in range(8))
    unresolved = frozenset(handles[-2:])
    db = tmp_path / "resume-creators.db"
    _create_db(db, handles=handles)

    def initial_probe(handle, *_args):
        return _unknown(handle) if handle in unresolved else _yes(handle)

    initial, _, _ = _collect_many(
        tmp_path,
        db,
        handles=handles,
        plan_name="resume-source-v2.plan.json",
        probe=initial_probe,
    )
    prior_path = tmp_path / "resume-source-v1.plan.json"
    prior = _legacy_plan(initial, prior_path)
    resolved, resolved_path, _ = _collect_many(
        tmp_path,
        db,
        handles=handles,
        plan_name="resume-resolved-v2.plan.json",
        probe=_yes,
        prior_plan=prior_path,
        expected_prior_sha=prior["plan_sha256"],
    )
    return db, handles, resolved, resolved_path


def _write_rehashed_plan(path: Path, plan: dict) -> None:
    plan["plan_sha256"] = backfill.plan_sha256(plan)
    path.write_text(
        json.dumps(plan, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _db_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _rows(path: Path) -> dict[str, sqlite3.Row]:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        return {
            row["handle"]: row
            for row in connection.execute(
                "SELECT * FROM creator_profiles ORDER BY handle"
            ).fetchall()
        }
    finally:
        connection.close()


def test_collect_is_read_only_and_holds_proxy_account_profile_bundle(tmp_path):
    db = tmp_path / "creators.db"
    _create_db(db)
    before = _db_digest(db)
    spy = _LeaseSpy()

    plan, plan_path, lease_db = _collect(tmp_path, db, lease_api=spy)

    assert _db_digest(db) == before
    assert plan_path.is_file()
    assert plan["apply_allowed"] is True
    assert plan["unresolved_count"] == 0
    assert plan["expected_status"] == "decided"
    assert plan["expected_count"] == 2
    assert plan["plan_sha256"] == backfill.plan_sha256(plan_path)
    assert spy.acquisitions == 1
    assert spy.heartbeats >= 1
    assert spy.releases == 1
    with sqlite3.connect(lease_db) as connection:
        resources = connection.execute(
            "SELECT resource_key,released_at FROM resource_leases ORDER BY resource_key"
        ).fetchall()
    assert len(resources) == 5  # two accounts + two exact Profiles + batch singleton
    assert all(released_at is not None for _, released_at in resources)
    assert any(key.startswith("account:instagram:") for key, _ in resources)
    assert any(key.startswith("chrome-profile:") for key, _ in resources)
    assert any(key.startswith("pipeline:storefront-backfill:") for key, _ in resources)


def test_collect_missing_proxy_fails_closed_before_lease_or_plan(tmp_path):
    db = tmp_path / "creators.db"
    _create_db(db)
    before = _db_digest(db)
    spy = _LeaseSpy()

    with pytest.raises(backfill.StorefrontBackfillError, match="proxy is required"):
        _collect(
            tmp_path,
            db,
            proxy_loader=lambda **_kwargs: None,
            lease_api=spy,
        )

    assert _db_digest(db) == before
    assert spy.acquisitions == 0
    assert not (tmp_path / "storefront.plan.json").exists()


@pytest.mark.parametrize(
    "proxy",
    [
        {
            "server": "http://localhost:11404",
            "username": "user",
            "password": "password",
        },
        {
            "server": "http://127.0.0.1:11404",
            "username": "user",
            "password": "password",
        },
        {
            "server": "http://10.0.0.1:11404",
            "username": "user",
            "password": "password",
        },
        {
            "server": "http://evil.qg.net:11404",
            "username": "user",
            "password": "password",
        },
        {
            "server": "http://overseas.tunnel.qg.net:8080",
            "username": "user",
            "password": "password",
        },
        {
            "server": "http://overseas.tunnel.qg.net:99999",
            "username": "user",
            "password": "password",
        },
        {"server": "http://overseas.tunnel.qg.net:11404"},
        {
            "server": "http://overseas.tunnel.qg.net:11404",
            "username": 123,
            "password": "password",
        },
        {
            "server": "http://overseas.tunnel.qg.net:11404/path",
            "username": "user",
            "password": "password",
        },
        {
            "server": "http://overseas.tunnel.qg.net:11404",
            "username": "user",
            "password": "password",
            "bypass": "<local>",
        },
    ],
)
def test_proxy_trust_boundary_rejects_local_untrusted_or_bypass_config(proxy):
    with pytest.raises(backfill.StorefrontBackfillError, match="proxy"):
        backfill._public_proxy_check(proxy)


def test_proxy_trust_boundary_accepts_exact_authenticated_endpoint():
    backfill._public_proxy_check(
        {
            "server": "http://overseas.tunnel.qg.net:11404",
            "username": "user",
            "password": "password",
        }
    )


def test_collect_refuses_to_overwrite_formal_plan_before_acquiring_resources(tmp_path):
    db = tmp_path / "creators.db"
    _create_db(db)
    shallow, deep, profiles = _account_fixture(tmp_path)
    plan_path = tmp_path / "storefront.plan.json"
    plan_path.write_text("reviewed-plan-must-survive\n", encoding="utf-8")
    spy = _LeaseSpy()

    with pytest.raises(backfill.StorefrontBackfillError, match="plan already exists"):
        backfill.collect_plan(
            db_path=db,
            batch_id=BATCH,
            expected_count=2,
            expected_handle_set_sha256=backfill.handle_set_sha256(
                ("alpha", "beta")
            ),
            plan_path=plan_path,
            accounts_file=shallow,
            deep_accounts_file=deep,
            profile_root=profiles,
            lease_db=tmp_path / "leases.db",
            probe_candidate=_mixed_probe,
            proxy_loader=_proxy,
            lease_api=spy,
        )

    assert plan_path.read_text(encoding="utf-8") == "reviewed-plan-must-survive\n"
    assert spy.acquisitions == 0


def test_collect_rejects_same_count_handle_substitution_before_preflight_or_lease(
    tmp_path,
):
    db = tmp_path / "creators.db"
    _create_db(db, handles=("alpha", "gamma"))
    shallow, deep, profiles = _account_fixture(tmp_path)
    preflight_calls = 0
    spy = _LeaseSpy()

    def forbidden_preflight(*args, **kwargs):
        nonlocal preflight_calls
        preflight_calls += 1
        raise AssertionError("preflight must not run after cohort mismatch")

    with pytest.raises(
        backfill.StorefrontBackfillError, match="handle-set SHA-256 mismatch"
    ):
        backfill.collect_plan(
            db_path=db,
            batch_id=BATCH,
            expected_count=2,
            expected_handle_set_sha256=backfill.handle_set_sha256(
                ("alpha", "beta")
            ),
            plan_path=tmp_path / "plan.json",
            accounts_file=shallow,
            deep_accounts_file=deep,
            profile_root=profiles,
            lease_db=tmp_path / "leases.db",
            probe_candidate=_mixed_probe,
            preflight_fn=forbidden_preflight,
            proxy_loader=_proxy,
            lease_api=spy,
        )

    assert preflight_calls == 0
    assert spy.acquisitions == 0
    assert not (tmp_path / "leases.db").exists()


@pytest.mark.parametrize("alias_kind", ["same_path", "symlink", "hardlink"])
def test_collect_rejects_lease_database_alias_of_creator_before_any_write(
    tmp_path, alias_kind
):
    db = tmp_path / "creators.db"
    _create_db(db)
    before = _db_digest(db)
    shallow, deep, profiles = _account_fixture(tmp_path)
    if alias_kind == "same_path":
        lease_db = db
    elif alias_kind == "symlink":
        lease_db = tmp_path / "lease-symlink.db"
        lease_db.symlink_to(db)
    else:
        lease_db = tmp_path / "lease-hardlink.db"
        os.link(db, lease_db)
    preflight_calls = 0

    def forbidden_preflight(*_args, **_kwargs):
        nonlocal preflight_calls
        preflight_calls += 1
        raise AssertionError("account preflight must not run for aliased databases")

    with pytest.raises(
        backfill.StorefrontBackfillError, match="physically separate"
    ):
        backfill.collect_plan(
            db_path=db,
            batch_id=BATCH,
            expected_count=2,
            expected_handle_set_sha256=backfill.handle_set_sha256(
                ("alpha", "beta")
            ),
            plan_path=tmp_path / "plan.json",
            accounts_file=shallow,
            deep_accounts_file=deep,
            profile_root=profiles,
            lease_db=lease_db,
            probe_candidate=_mixed_probe,
            preflight_fn=forbidden_preflight,
            proxy_loader=_proxy,
        )

    assert preflight_calls == 0
    assert _db_digest(db) == before
    with sqlite3.connect(db) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type='table' AND name='resource_leases'"
        ).fetchone()[0] == 0
    assert not (tmp_path / "plan.json").exists()


def test_unknown_is_retried_once_with_a_different_shallow_account(tmp_path):
    db = tmp_path / "creators.db"
    _create_db(db)
    calls: dict[str, list[str]] = {"alpha": [], "beta": []}

    def flaky_probe(handle, account, _heartbeat):
        calls[handle].append(account.username)
        if handle == "alpha" and len(calls[handle]) == 1:
            raise TimeoutError("transient profile timeout")
        return _yes(handle) if handle == "alpha" else _no(handle)

    plan, _, _ = _collect(tmp_path, db, probe=flaky_probe)

    assert plan["apply_allowed"] is True
    assert calls["alpha"] == ["shallow_one", "shallow_two"]
    assert calls["beta"] == ["shallow_two"]
    alpha = next(row for row in plan["rows"] if row["handle"] == "alpha")
    assert [item["status"] for item in alpha["result"]["evidence"]["attempts"]] == [
        "unknown",
        "confirmed_yes",
    ]
    assert [
        item["account_slot"] for item in alpha["result"]["evidence"]["attempts"]
    ] == [0, 1]


def test_confirmed_no_requires_complete_profile_expansion_and_every_target():
    base_profile = {
        "handle": "alpha",
        "external_url": "https://example.com/creator",
        "_bio_has_more": False,
        **_verified_flags(),
    }
    failed_target = backfill.resolve_observation(
        handle="alpha",
        profile=base_profile,
        profile_url="https://instagram.com/alpha/",
        target_checks=[
            {
                "source_url": "https://example.com/creator",
                "status": "failed",
                "commerce_links": [],
            }
        ],
    )
    assert failed_target["status"] == "unknown"
    assert "external_target_incomplete" in failed_target["evidence"]["failures"]

    hidden_incomplete = backfill.resolve_observation(
        handle="alpha",
        profile={**base_profile, "_bio_has_more": True},
        profile_url="https://instagram.com/alpha/",
        expanded_links=[],
        expansion_succeeded=False,
        target_checks=[
            {
                "source_url": "https://example.com/creator",
                "status": "succeeded",
                "commerce_links": [],
                "http_status": 200,
                "page_health": {"healthy": True, "reason": "test"},
            }
        ],
    )
    assert hidden_incomplete["status"] == "unknown"
    assert "bio_link_expansion_incomplete" in hidden_incomplete["evidence"]["failures"]

    complete = backfill.resolve_observation(
        handle="alpha",
        profile=base_profile,
        profile_url="https://instagram.com/alpha/",
        target_checks=[
            {
                "source_url": "https://example.com/creator",
                "status": "succeeded",
                "commerce_links": [],
                "http_status": 200,
                "page_health": {"healthy": True, "reason": "test"},
            }
        ],
    )
    assert complete["status"] == "confirmed_no"


def test_hidden_bio_count_requires_raw_and_terminal_n_plus_one():
    profile = {
        "handle": "alpha",
        "external_url": "https://example.com/one",
        "_bio_has_more": True,
        "_bio_more_count": 2,
        **_verified_flags(),
    }

    def check(url):
        if backfill._is_noncommerce_social_url(url):
            return {
                "source_url": url,
                "status": "succeeded",
                "commerce_links": [],
                "page_health": {"healthy": True, "reason": "social"},
            }
        return {
            "source_url": url,
            "status": "succeeded",
            "http_status": 200,
            "commerce_links": [],
            "page_health": {"healthy": True, "reason": "test"},
        }

    incomplete_links = ["https://example.com/one", "https://example.org/two"]
    incomplete = backfill.resolve_observation(
        handle="alpha",
        profile=profile,
        profile_url="https://instagram.com/alpha/",
        expanded_links=["https://example.org/two"],
        expansion_succeeded=True,
        observed_links=incomplete_links,
        raw_observed_link_count=2,
        terminal_observed_link_count=2,
        target_checks=[check(url) for url in incomplete_links],
    )
    assert incomplete["status"] == "unknown"
    assert incomplete["_bio_more_count"] == 2
    assert "bio_link_expansion_incomplete" in incomplete["evidence"]["failures"]
    expansion = incomplete["evidence"]["bio_link_expansion"]
    assert expansion["declared_more"] == 2
    assert expansion["declared_total"] == 3
    assert expansion["raw_observed_total"] == 2
    assert expansion["terminal_complete"] is False

    complete_links = [
        "https://example.com/one",
        "https://example.org/two",
        "https://youtube.com/@alpha",
    ]
    complete = backfill.resolve_observation(
        handle="alpha",
        profile=profile,
        profile_url="https://instagram.com/alpha/",
        expanded_links=complete_links[1:],
        expansion_succeeded=True,
        observed_links=complete_links,
        raw_observed_link_count=3,
        terminal_observed_link_count=3,
        target_checks=[check(url) for url in complete_links],
    )
    assert complete["status"] == "confirmed_no"
    assert complete["evidence"]["bio_link_expansion"]["terminal_complete"] is True
    assert backfill._validate_probe_result(complete, handle="alpha")["status"] == (
        "confirmed_no"
    )


def test_storefront_yes_is_existential_but_no_remains_universal():
    amazon = "https://amazon.com/shop/alpha"
    broken = "https://example.com/broken"
    profile = {
        "handle": "alpha",
        "external_url": amazon,
        "_bio_has_more": True,
        "_bio_more_count": 1,
        **_verified_flags(),
    }
    checks = [
        {
            "source_url": amazon,
            "status": "succeeded",
            "commerce_links": [{"url": amazon, "type": "Amazon"}],
            "page_health": {"healthy": True, "reason": "direct_url"},
        },
        {
            "source_url": broken,
            "status": "failed",
            "commerce_links": [],
        },
    ]
    yes = backfill.resolve_observation(
        handle="alpha",
        profile=profile,
        profile_url="https://instagram.com/alpha/",
        expanded_links=[broken],
        expansion_succeeded=False,
        observed_links=[amazon, broken],
        raw_observed_link_count=2,
        terminal_observed_link_count=2,
        target_checks=checks,
    )
    assert yes["status"] == "confirmed_yes"
    assert yes["evidence"]["failures"] == []
    assert "external_target_incomplete" in yes["evidence"]["partial_failures"]
    assert backfill._validate_probe_result(yes, handle="alpha")["status"] == (
        "confirmed_yes"
    )

    no_claim = dict(yes)
    no_claim["status"] = "confirmed_no"
    no_claim["storefront_url"] = None
    no_claim["storefront_type"] = None
    no_claim["amazon_storefront_link"] = None
    assert backfill._validate_probe_result(no_claim, handle="alpha")["status"] == (
        "unknown"
    )


def test_multilingual_more_count_and_unfiltered_popup_links():
    import browser_collect_v2 as browser_collect

    assert browser_collect._bio_more_count("example.com y 2 más") == 2
    assert browser_collect._bio_more_count("example.com et 3 autres") == 3
    assert browser_collect._bio_more_count("example.com и ещё 4") == 4
    assert browser_collect._bio_more_count("example.com 5개 더 보기") == 5
    assert browser_collect._bio_more_count("example.com 还有 6 个") == 6

    class PopupPage:
        def __init__(self):
            self.calls = 0
            self.keyboard = type("Keyboard", (), {"press": lambda self, _key: None})()

        def evaluate(self, _script):
            self.calls += 1
            if self.calls == 1:
                return {
                    "tagged": True,
                    "candidate_count": 1,
                    "declared_more_count": 1,
                }
            return {
                "dialog_seen": True,
                "http_hrefs": [
                    "https://instagram.com/alpha",
                    "https://example.com/shop",
                ],
                "urls": [
                    "https://instagram.com/alpha",
                    "https://example.com/shop",
                ],
            }

        def click(self, *_args, **_kwargs):
            return None

        def wait_for_timeout(self, _milliseconds):
            return None

    diagnostics = {}
    assert browser_collect._expand_bio_links(
        PopupPage(),
        include_social=True,
        expected_more_count=1,
        diagnostics=diagnostics,
    ) == ["https://instagram.com/alpha", "https://example.com/shop"]
    assert diagnostics["reason"] == "success"
    assert browser_collect._expand_bio_links(
        PopupPage(), include_social=False, expected_more_count=1
    ) == ["https://example.com/shop"]

    class AmbiguousDeclarationPage:
        def evaluate(self, _script):
            return [
                {
                    "text": "example.com und 2 weitere Links",
                    "popup": "dialog",
                    "expanded": "false",
                    "interactive": True,
                    "trusted_surface": True,
                }
            ]

    has_more, declared, evidence = backfill._bio_more_declaration(
        AmbiguousDeclarationPage(),
        {"_bio_has_more": False, "_bio_more_count": None},
        parse_count=browser_collect._bio_more_count,
    )
    assert has_more is True
    assert declared is None
    assert evidence["ambiguous"] is True


def _cold_bio_dialog_html(
    *,
    hydrate_delay_ms: int,
    href_count: int = 4,
    ordinary_links: bool = False,
    dialog_extra_html: str = "",
) -> str:
    storefront_anchors = [
        (
            "https://l.instagram.com/?u=https%3A%2F%2Fwww.tiktok.com%2F"
            "%40kusumghising5",
            "www.tiktok.com/@kusumghising5",
        ),
        ("https://www.facebook.com/577440038780390", "Facebook"),
        (
            "https://l.instagram.com/?u=https%3A%2F%2Fwww.youtube.com%2F"
            "%40Roseksum",
            "www.youtube.com/@Roseksum",
        ),
        (
            "https://l.instagram.com/?u=https%3A%2F%2Fwww.myyshop.com%2Fp%2F"
            "d5397b5b",
            "www.myyshop.com/p/d5397b5b",
        ),
    ]
    ordinary_anchors = [
        (f"https://ordinary-{index}.example/about", f"Ordinary {index}")
        for index in range(1, 5)
    ]
    anchors = (ordinary_anchors if ordinary_links else storefront_anchors)[:href_count]
    encoded = json.dumps(
        "".join(
            f'<a href="{href}"><span>{text}</span></a>' for href, text in anchors
        )
        + dialog_extra_html
    )
    return f"""
      <main><header>
        <div style="display:none">
          www.tiktok.com/@kusumghising5 and 3 more
        </div>
        <button id="real-bio-control" onclick="openBioDialog()">
          <div><div>www.tiktok.com/@kusumghising5 and 3 more</div></div>
        </button>
      </header></main>
      <div id="bio-dialog" role="dialog" aria-modal="true" style="display:none"></div>
      <script>
        function openBioDialog() {{
          const dialog=document.getElementById('bio-dialog');
          dialog.style.display='block';
          setTimeout(() => {{ dialog.innerHTML={encoded}; }}, {hydrate_delay_ms});
        }}
      </script>
    """


def test_real_chrome_bio_expansion_ignores_hidden_clone_and_polls_cold_links():
    import time

    import browser_collect_v2 as browser_collect
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page()
        try:
            # Deliberately hydrate after the legacy helper's fixed 2-second wait.
            page.set_content(_cold_bio_dialog_html(hydrate_delay_ms=2200))
            diagnostics = {}
            started = time.monotonic()
            links = browser_collect._expand_bio_links(
                page,
                include_social=True,
                expected_more_count=3,
                diagnostics=diagnostics,
            )
            elapsed = time.monotonic() - started

            assert 2.0 < elapsed < 4.0
            assert len(links) == 7
            assert "https://www.myyshop.com/p/d5397b5b" in links
            assert diagnostics["reason"] == "success"
            assert diagnostics["max_http_href_count"] == 4
            terminal_links = {
                backfill.safe_external_url(
                    link,
                    resolver=lambda _host: ["8.8.8.8"],
                )
                for link in links
            }
            assert len(terminal_links) == 4
            assert backfill.storefront_policy.classify_url(
                "https://www.myyshop.com/p/d5397b5b"
            ) == "链接聚合"
            assert page.locator('[data-sop-bioexpand="1"]').evaluate(
                "element => element.tagName"
            ) == "BUTTON"
            assert page.locator("main header > div").get_attribute(
                "data-sop-bioexpand"
            ) is None
        finally:
            browser.close()


def test_real_chrome_bio_expansion_retries_one_transient_click(
    monkeypatch,
):
    import browser_collect_v2 as browser_collect
    from playwright.sync_api import sync_playwright

    monkeypatch.setattr(browser_collect, "_BIO_EXPAND_ATTEMPT_TIMEOUT_MS", 150)
    monkeypatch.setattr(browser_collect, "_BIO_EXPAND_POLL_INTERVAL_MS", 20)

    class FirstClickFails:
        def __init__(self, page):
            self._page = page
            self.click_calls = 0

        def __getattr__(self, name):
            return getattr(self._page, name)

        def click(self, *args, **kwargs):
            self.click_calls += 1
            if self.click_calls == 1:
                raise RuntimeError("transient detached control")
            return self._page.click(*args, **kwargs)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page()
        try:
            page.set_content(_cold_bio_dialog_html(hydrate_delay_ms=0))
            wrapped = FirstClickFails(page)
            diagnostics = {}
            links = browser_collect._expand_bio_links(
                wrapped,
                include_social=True,
                expected_more_count=3,
                diagnostics=diagnostics,
            )

            assert wrapped.click_calls == 2
            assert len(links) == 7
            assert diagnostics["attempts"] == 2
            assert diagnostics["click_error"] == "RuntimeError"
            assert diagnostics["reason"] == "success"
        finally:
            browser.close()


def test_real_chrome_bio_expansion_rejects_dialog_that_never_reaches_n_plus_one(
    monkeypatch,
):
    import browser_collect_v2 as browser_collect
    from playwright.sync_api import sync_playwright

    monkeypatch.setattr(browser_collect, "_BIO_EXPAND_ATTEMPT_TIMEOUT_MS", 250)
    monkeypatch.setattr(browser_collect, "_BIO_EXPAND_POLL_INTERVAL_MS", 20)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page()
        try:
            page.set_content(
                _cold_bio_dialog_html(hydrate_delay_ms=0, href_count=3)
            )
            diagnostics = {}
            links = browser_collect._expand_bio_links(
                page,
                include_social=True,
                expected_more_count=3,
                diagnostics=diagnostics,
            )

            assert links == []
            assert diagnostics["dialog_seen"] is True
            assert diagnostics["max_http_href_count"] == 3
            assert diagnostics["reason"] == "count_mismatch"
        finally:
            browser.close()


def test_real_chrome_bio_expansion_ignores_hidden_dialog_anchors_and_text(
    monkeypatch,
):
    import browser_collect_v2 as browser_collect
    from playwright.sync_api import sync_playwright

    monkeypatch.setattr(browser_collect, "_BIO_EXPAND_ATTEMPT_TIMEOUT_MS", 250)
    monkeypatch.setattr(browser_collect, "_BIO_EXPAND_POLL_INTERVAL_MS", 20)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page()
        try:
            # Three visible ordinary hrefs plus one hidden Amazon anchor must
            # not satisfy Instagram's declared total of four.
            page.set_content(
                _cold_bio_dialog_html(
                    hydrate_delay_ms=0,
                    href_count=3,
                    ordinary_links=True,
                    dialog_extra_html=(
                        '<a href="https://amazon.com/shop/hidden" '
                        'style="display:none">Hidden Amazon</a>'
                    ),
                )
            )
            diagnostics = {}
            assert browser_collect._expand_bio_links(
                page,
                include_social=True,
                expected_more_count=3,
                diagnostics=diagnostics,
            ) == []
            assert diagnostics["max_http_href_count"] == 3
            assert diagnostics["reason"] == "count_mismatch"

            # Four visible ordinary hrefs are complete, but a hidden text-only
            # storefront clone must never enter the returned evidence.
            page.set_content(
                _cold_bio_dialog_html(
                    hydrate_delay_ms=0,
                    ordinary_links=True,
                    dialog_extra_html=(
                        '<span style="visibility:hidden">'
                        "amazon.com/shop/hidden-text</span>"
                    ),
                )
            )
            diagnostics = {}
            links = browser_collect._expand_bio_links(
                page,
                include_social=True,
                expected_more_count=3,
                diagnostics=diagnostics,
            )
            assert len(links) == 4
            assert not any("amazon.com" in link for link in links)
            assert diagnostics["max_http_href_count"] == 4
            assert diagnostics["max_extracted_url_count"] == 4
            assert diagnostics["reason"] == "success"
        finally:
            browser.close()


def test_plain_domain_and_number_text_is_not_hidden_link_evidence():
    import browser_collect_v2 as browser_collect

    class PlainBioTextPage:
        def evaluate(self, _script):
            return [
                {
                    "text": "creator.example 125K followers",
                    "popup": "",
                    "expanded": "",
                    "interactive": False,
                    "trusted_surface": False,
                }
            ]

    has_more, declared, evidence = backfill._bio_more_declaration(
        PlainBioTextPage(),
        {"_bio_has_more": False, "_bio_more_count": None},
        parse_count=browser_collect._bio_more_count,
    )

    assert has_more is False
    assert declared is None
    assert evidence["ambiguous"] is False
    assert evidence["interactive_generic_signal"] is False


def test_unknown_locale_domain_and_number_requires_a_real_control():
    import browser_collect_v2 as browser_collect

    class ClickableUnknownLocalePage:
        def evaluate(self, _script):
            return [
                {
                    "text": "creator.example und zusätzlich 2",
                    "popup": "",
                    "expanded": "",
                    "interactive": True,
                    "trusted_surface": True,
                }
            ]

    has_more, declared, evidence = backfill._bio_more_declaration(
        ClickableUnknownLocalePage(),
        {"_bio_has_more": False, "_bio_more_count": None},
        parse_count=browser_collect._bio_more_count,
    )

    assert has_more is True
    assert declared is None
    assert evidence["ambiguous"] is True
    assert evidence["interactive_generic_signal"] is True


def test_known_locale_declared_count_remains_authoritative_without_dom_control():
    import browser_collect_v2 as browser_collect

    class KnownLocalePage:
        def evaluate(self, _script):
            return [
                {
                    "text": "creator.example and 2 more",
                    "popup": "",
                    "expanded": "",
                    "interactive": True,
                    "trusted_surface": True,
                }
            ]

    has_more, declared, evidence = backfill._bio_more_declaration(
        KnownLocalePage(),
        {"_bio_has_more": False, "_bio_more_count": None},
        parse_count=browser_collect._bio_more_count,
    )

    assert has_more is True
    assert declared == 2
    assert evidence["ambiguous"] is False


@pytest.mark.parametrize(
    ("profile", "dom_items"),
    [
        (
            {
                "_bio_link_label": "youtube.com/@blondiemoustache",
                "_bio_has_more": False,
                "_bio_more_count": None,
            },
            [
                {
                    "text": "mi trucco tanto (+350k) un nuovo video... more youtube.com/@blondiemoustache",
                    "popup": "",
                    "expanded": "",
                    "interactive": False,
                    "trusted_surface": False,
                },
                {
                    "text": "LINK Bambi Vol 3 Bambi vol. 2 Bambi Vol 1",
                    "popup": "",
                    "expanded": "",
                    "interactive": False,
                    "trusted_surface": False,
                },
            ],
        ),
        (
            {"_bio_has_more": False, "_bio_more_count": None},
            [
                {
                    "text": "more glow-up: TikTok csilla.zs 93k CSILLA4000 CSILLA10... more",
                    "popup": "",
                    "expanded": "",
                    "interactive": True,
                    "trusted_surface": False,
                }
            ],
        ),
        (
            {"_bio_has_more": False, "_bio_more_count": None},
            [
                {
                    "text": "TikTok pesukarhukissa 160k essileppanen@gmail... more",
                    "popup": "",
                    "expanded": "",
                    "interactive": False,
                    "trusted_surface": False,
                }
            ],
        ),
        (
            {
                "_bio_link_label": "skin-constructor.sitepulse.com.ua",
                "_bio_has_more": False,
                "_bio_more_count": None,
            },
            [
                {
                    "text": "Pravik10 korean_story_official... more skin-constructor.sitepulse.com.ua",
                    "popup": "",
                    "expanded": "",
                    "interactive": False,
                    "trusted_surface": False,
                }
            ],
        ),
        (
            {"_bio_has_more": False, "_bio_more_count": None},
            [
                {
                    "text": "Avis 100% honnêtes vallymary@hotmail.fr... more",
                    "popup": "",
                    "expanded": "",
                    "interactive": True,
                    "trusted_surface": False,
                },
                {
                    "text": "hellofresh.fr florame.com 2025 nouveauté",
                    "popup": "",
                    "expanded": "",
                    "interactive": False,
                    "trusted_surface": False,
                },
            ],
        ),
    ],
    ids=[
        "blondiemoustache",
        "csilla-zs",
        "pesukarhukissa",
        "pravik-kateryna",
        "vallymary00",
    ],
)
def test_real_biography_show_more_and_highlight_shapes_are_not_link_declarations(
    profile, dom_items
):
    import browser_collect_v2 as browser_collect

    class CapturedDomPage:
        def evaluate(self, _script):
            return dom_items

    has_more, declared, evidence = backfill._bio_more_declaration(
        CapturedDomPage(),
        profile,
        parse_count=browser_collect._bio_more_count,
    )

    assert has_more is False
    assert declared is None
    assert evidence["ambiguous"] is False
    assert evidence["popup_signal"] is False
    assert evidence["interactive_label_count"] == 0
    assert evidence["interactive_generic_signal"] is False


def test_bykusum_domain_leading_and_n_more_control_remains_authoritative():
    import browser_collect_v2 as browser_collect

    label = "www.tiktok.com/@kusumghising5 and 3 more"

    class ByKusumBioLinkPage:
        def evaluate(self, _script):
            return [
                {
                    "text": label,
                    "popup": "",
                    "expanded": "",
                    "interactive": True,
                    "trusted_surface": True,
                }
            ]

    has_more, declared, evidence = backfill._bio_more_declaration(
        ByKusumBioLinkPage(),
        {
            "_bio_link_label": label,
            "_bio_has_more": True,
            "_bio_more_count": 3,
        },
        parse_count=browser_collect._bio_more_count,
    )

    assert has_more is True
    assert declared == 3
    assert evidence["ambiguous"] is False
    assert evidence["interactive_label_count"] == 1


def test_real_dom_bio_link_boundary_rejects_show_more_and_story_surfaces():
    import browser_collect_v2 as browser_collect
    from playwright.sync_api import sync_playwright

    negative_cases = [
        (
            {
                "_bio_link_label": "youtube.com/@blondiemoustache",
                "_bio_has_more": False,
                "_bio_more_count": None,
            },
            """
            <div>mi trucco tanto (+350k) un nuovo video... more</div>
            <div role="menu"><button>LINK Bambi Vol 3 Bambi vol. 2</button></div>
            """,
        ),
        (
            {"_bio_has_more": False, "_bio_more_count": None},
            '<div role="button">more glow-up: csilla.zs 93k CSILLA4000... more</div>',
        ),
        (
            {"_bio_has_more": False, "_bio_more_count": None},
            '<div>TikTok pesukarhukissa 160k essileppanen@gmail... more</div>',
        ),
        (
            {
                "_bio_link_label": "skin-constructor.sitepulse.com.ua",
                "_bio_has_more": False,
                "_bio_more_count": None,
            },
            '<div>Pravik10 korean_story_official... more</div>',
        ),
        (
            {"_bio_has_more": False, "_bio_more_count": None},
            """
            <div role="button">Avis 100% honnêtes vallymary@hotmail.fr... more</div>
            <div role="presentation"><button>hellofresh.fr links 2025</button></div>
            """,
        ),
        (
            {"_bio_has_more": False, "_bio_more_count": None},
            '<button aria-expanded="false">Creator links 2025... more</button>',
        ),
        (
            {"_bio_has_more": False, "_bio_more_count": None},
            '<button aria-expanded="false">creator.example</button>',
        ),
    ]

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page()
        try:
            for profile, surface in negative_cases:
                page.set_content(f"<main><section>{surface}</section></main>")
                has_more, declared, evidence = backfill._bio_more_declaration(
                    page,
                    profile,
                    parse_count=browser_collect._bio_more_count,
                )
                assert has_more is False
                assert declared is None
                assert evidence["popup_signal"] is False

            page.set_content(
                "<main><header><button>"
                "www.tiktok.com/@kusumghising5 and 3 more"
                "</button></header></main>"
            )
            has_more, declared, evidence = backfill._bio_more_declaration(
                page,
                {"_bio_has_more": False, "_bio_more_count": None},
                parse_count=browser_collect._bio_more_count,
            )
            assert has_more is True
            assert declared == 3
            assert evidence["popup_signal"] is False

            page.set_content(
                '<main><section><button aria-haspopup="dialog">'
                "Links</button></section></main>"
            )
            has_more, declared, evidence = backfill._bio_more_declaration(
                page,
                {"_bio_has_more": False, "_bio_more_count": None},
                parse_count=browser_collect._bio_more_count,
            )
            assert has_more is True
            assert declared is None
            assert evidence["popup_signal"] is True
        finally:
            browser.close()


def test_probe_result_rejects_schema_and_internal_verdict_evidence_conflicts():
    invalid_schema = _no("alpha")
    invalid_schema["evidence"]["schema"] = "unexpected"
    with pytest.raises(backfill.StorefrontBackfillError, match="evidence schema"):
        backfill._validate_probe_result(invalid_schema, handle="alpha")

    yes_without_matching_evidence = _yes("alpha")
    yes_without_matching_evidence["evidence"]["target_checks"][0][
        "commerce_links"
    ] = []
    with pytest.raises(
        backfill.StorefrontBackfillError, match="absent from target evidence"
    ):
        backfill._validate_probe_result(
            yes_without_matching_evidence, handle="alpha"
        )

    no_with_commerce = backfill.resolve_observation(
        handle="alpha",
        profile={
            "handle": "alpha",
            "external_url": "https://example.com/creator",
            "_bio_has_more": False,
            **_verified_flags(),
        },
        profile_url="https://instagram.com/alpha/",
        target_checks=[
            {
                "source_url": "https://example.com/creator",
                "status": "succeeded",
                "http_status": 200,
                "page_health": {"healthy": True, "reason": "test"},
                "commerce_links": [
                    {"url": "https://amazon.com/shop/alpha", "type": "Amazon"}
                ],
            }
        ],
    )
    no_with_commerce["status"] = "confirmed_no"
    no_with_commerce["storefront_url"] = None
    no_with_commerce["storefront_type"] = None
    with pytest.raises(
        backfill.StorefrontBackfillError, match="contains commerce links"
    ):
        backfill._validate_probe_result(no_with_commerce, handle="alpha")

    source_mismatch = _yes("alpha")
    source_mismatch["evidence"]["target_checks"][0]["source_url"] = (
        "https://amazon.com/shop/different"
    )
    assert backfill._validate_probe_result(source_mismatch, handle="alpha")[
        "status"
    ] == "unknown"


def test_apply_validator_recomputes_identity_sources_and_rejects_tampered_summaries():
    surface_only = _yes("alpha")
    identity = surface_only["evidence"]["profile_identity_check"]
    identity.update(
        {
            "og_exact": False,
            "trusted_surface_match": True,
            "independent_match": True,
            "strong_contradiction": False,
            "contradictory_surface": False,
            "weak_surface_nonmatch": False,
            "surface_handle_source": "heading_text",
            "sources": {
                "og_handle": None,
                "canonical_handle": "alpha",
                "surface_handle": "alpha",
            },
        }
    )
    assert backfill._validate_probe_result(surface_only, handle="alpha")[
        "status"
    ] == "confirmed_yes"

    unbound = copy.deepcopy(surface_only)
    unbound["evidence"]["profile_identity_check"]["surface_handle_source"] = (
        "unbound_text"
    )
    with pytest.raises(
        backfill.StorefrontBackfillError, match="summary disagrees with sources"
    ):
        backfill._validate_probe_result(unbound, handle="alpha")

    forged_og = _yes("alpha")
    forged_og["evidence"]["profile_identity_check"]["sources"]["og_handle"] = (
        "different"
    )
    with pytest.raises(
        backfill.StorefrontBackfillError, match="summary disagrees with sources"
    ):
        backfill._validate_probe_result(forged_og, handle="alpha")


def test_stage_json_has_export_precedence_and_handle_must_match_database(tmp_path):
    db = tmp_path / "creators.db"
    _create_db(db, handles=("alpha",))
    connection = sqlite3.connect(db)
    try:
        raw = connection.execute(
            "SELECT stage_json FROM creator_profiles WHERE handle='alpha'"
        ).fetchone()[0]
        stage = json.loads(raw)
        stage["storefront_status"] = "confirmed_yes"
        stage["storefront_url"] = "https://amazon.com/shop/alpha"
        stage["storefront_type"] = "Amazon"
        connection.execute(
            "UPDATE creator_profiles SET stage_json=?,storefront_status='unknown' "
            "WHERE handle='alpha'",
            (json.dumps(stage),),
        )
        connection.commit()
    finally:
        connection.close()

    assert backfill.inspect_unknown_cohort(db, batch_id=BATCH) == ()

    connection = sqlite3.connect(db)
    try:
        stage["storefront_status"] = "unknown"
        stage.pop("storefront_url")
        stage.pop("storefront_type")
        connection.execute(
            "UPDATE creator_profiles SET stage_json=?,"
            "storefront_status='confirmed_yes',"
            "amazon_storefront_link='https://amazon.com/shop/stale' "
            "WHERE handle='alpha'",
            (json.dumps(stage),),
        )
        connection.commit()
    finally:
        connection.close()

    cohort = backfill.inspect_unknown_cohort(db, batch_id=BATCH)
    assert [item["handle"] for item in cohort] == ["alpha"]

    connection = sqlite3.connect(db)
    try:
        stage["handle"] = "different"
        connection.execute(
            "UPDATE creator_profiles SET stage_json=? WHERE handle='alpha'",
            (json.dumps(stage),),
        )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(backfill.StorefrontBackfillError, match="handle disagrees"):
        backfill.inspect_unknown_cohort(db, batch_id=BATCH)


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://user:pass@example.com/",
        "http://localhost/shop",
        "http://127.0.0.1/shop",
        "http://example.com:22/shop",
    ],
)
def test_external_navigation_rejects_unsafe_targets(url):
    with pytest.raises(backfill.StorefrontBackfillError, match="external URL"):
        backfill.safe_external_url(
            url,
            resolver=lambda _host: ["127.0.0.1"],
        )


def test_safe_external_url_default_rejects_proxy_synthetic_dns():
    with pytest.raises(backfill.StorefrontBackfillError, match="non-public"):
        backfill.safe_external_url(
            "https://creator.example/shop",
            resolver=lambda _host: ["198.18.12.34"],
        )


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.1",
        "172.16.0.1",
        "192.168.0.1",
        "169.254.1.1",
        "224.0.0.1",
        "192.0.2.1",
        "100.64.0.1",
        "::1",
        "fe80::1",
        "ff02::1",
    ],
)
def test_proxy_runtime_keeps_every_other_non_public_dns_range_closed(address):
    runtime = backfill._BrowserProbeRuntime(
        credentials={},
        proxy_loader=_proxy,
        headless=True,
        resolver=lambda _host: [address],
    )
    page = _NeutralOnlyPage()
    _activate_proxy_runtime(runtime, pages=(page,))

    result = runtime._target_check(page, "https://creator.example/about")

    assert result["status"] == "unsafe"


def test_proxy_runtime_rejects_rfc2544_ip_literal_even_with_fake_dns_answer():
    runtime = backfill._BrowserProbeRuntime(
        credentials={},
        proxy_loader=_proxy,
        headless=True,
        resolver=lambda _host: ["198.18.0.9"],
    )
    page = _NeutralOnlyPage()
    _activate_proxy_runtime(runtime, pages=(page,))

    result = runtime._target_check(page, "https://198.18.0.9/shop")

    assert result["status"] == "unsafe"


@pytest.mark.parametrize(
    "url",
    [
        "https://metadata.google.internal/latest",
        "https://printer.lan/status",
        "https://service.local/admin",
        "https://singlelabel/path",
        "https://0177.0.0.1/admin",
    ],
)
def test_proxy_runtime_rejects_local_or_ambiguous_domain_names(url):
    runtime = backfill._BrowserProbeRuntime(
        credentials={},
        proxy_loader=_proxy,
        headless=True,
        resolver=lambda _host: ["198.18.0.9"],
    )
    page = _NeutralOnlyPage()
    _activate_proxy_runtime(runtime, pages=(page,))

    assert runtime._target_check(page, url)["status"] == "unsafe"


def test_proxy_runtime_rejects_mixed_synthetic_and_private_dns_answers():
    runtime = backfill._BrowserProbeRuntime(
        credentials={},
        proxy_loader=_proxy,
        headless=True,
        resolver=lambda _host: ["198.18.0.9", "10.0.0.9"],
    )
    page = _NeutralOnlyPage()
    _activate_proxy_runtime(runtime, pages=(page,))

    assert runtime._target_check(page, "https://creator.example/about")["status"] == (
        "unsafe"
    )


def test_social_contact_target_is_successful_noncommerce_without_navigation():
    runtime = backfill._BrowserProbeRuntime(
        credentials={},
        proxy_loader=_proxy,
        headless=True,
        resolver=lambda _host: ["8.8.8.8"],
    )
    page = _NeutralOnlyPage()
    _activate_proxy_runtime(runtime, pages=(page,))
    result = runtime._target_check(
        page, "https://instagram.com/shop/not-a-storefront"
    )

    assert result["status"] == "succeeded"
    assert result["commerce_links"] == []
    assert result["note"] == "recognized_noncommerce_social"
    assert page.external_navigation_calls == 0
    resolved = backfill.resolve_observation(
        handle="alpha",
        profile={
            "handle": "alpha",
            "external_url": result["source_url"],
            "_bio_has_more": False,
            **_verified_flags(),
        },
        profile_url="https://instagram.com/alpha/",
        target_checks=[result],
    )
    assert backfill._validate_probe_result(resolved, handle="alpha")["status"] == (
        "confirmed_no"
    )


class _FakeResponse:
    def __init__(self, status: int):
        self.status = status


class _NeutralOnlyPage:
    def __init__(self):
        self.url = "https://instagram.com/alpha/"
        self.neutral_navigation_calls = 0
        self.external_navigation_calls = 0

    def goto(self, url, **_kwargs):
        if url != "about:blank":
            self.external_navigation_calls += 1
            raise AssertionError(f"unexpected external navigation: {url}")
        self.neutral_navigation_calls += 1
        self.url = "about:blank"
        return _FakeResponse(200)


@pytest.mark.parametrize(
    ("direct_url", "kind"),
    [
        ("https://www.myyshop.com/p/d5397b5b", "链接聚合"),
        ("https://creator.myyfinds.io/skin-picks", "链接聚合"),
        ("https://angies.sumupstore.com/", "自营店"),
    ],
)
def test_verified_direct_storefront_is_existential_without_target_navigation(
    direct_url, kind
):
    runtime = backfill._BrowserProbeRuntime(
        credentials={},
        proxy_loader=_proxy,
        headless=True,
        resolver=lambda _host: ["8.8.8.8"],
    )
    page = _NeutralOnlyPage()
    _activate_proxy_runtime(runtime, pages=(page,))

    direct = runtime._target_check(page, direct_url)

    assert direct == {
        "source_url": direct_url,
        "status": "succeeded",
        "final_url": direct_url,
        "commerce_links": [{"url": direct_url, "type": kind}],
        "note": "recognized_direct_url",
        "page_health": {
            "healthy": True,
            "reason": "navigation_not_required_recognized_url",
        },
    }
    assert page.external_navigation_calls == 0

    failed_other = "https://example.com/access-controlled"
    resolved = backfill.resolve_observation(
        handle="alpha",
        profile={
            "handle": "alpha",
            "external_url": failed_other,
            "_bio_has_more": True,
            "_bio_more_count": 1,
            **_verified_flags(),
        },
        profile_url="https://instagram.com/alpha/",
        expanded_links=[direct_url],
        expansion_succeeded=True,
        observed_links=[failed_other, direct_url],
        raw_observed_link_count=2,
        terminal_observed_link_count=2,
        target_checks=[
            {
                "source_url": failed_other,
                "status": "failed",
                "http_status": 403,
                "commerce_links": [],
                "note": "external_http_error",
                "page_health": {
                    "healthy": False,
                    "reason": "external_http_error",
                },
            },
            direct,
        ],
    )

    assert resolved["status"] == "confirmed_yes"
    assert resolved["storefront_url"] == direct_url
    assert resolved["storefront_type"] == kind
    assert resolved["evidence"]["failures"] == []
    assert resolved["evidence"]["partial_failures"] == [
        "external_target_incomplete"
    ]
    assert backfill._validate_probe_result(resolved, handle="alpha")["status"] == (
        "confirmed_yes"
    )


class _OrdinaryPage:
    def __init__(self, response, samples):
        self.url = "https://example.com/about"
        self._response = response
        self._samples = iter(samples)
        self.neutral_navigation_calls = 0
        self.script_guard_state = {
            "websocket_blocked": 0,
            "popup_blocked": 0,
        }

    def route(self, *_args):
        return None

    def unroute(self, *_args):
        return None

    def goto(self, url, **_kwargs):
        if url == "about:blank":
            self.neutral_navigation_calls += 1
            self.url = "about:blank"
            return _FakeResponse(200)
        self.url = "https://example.com/about"
        return self._response

    def wait_for_timeout(self, _milliseconds):
        return None

    def evaluate(self, _script):
        if "__sopStorefrontGuardState" in _script:
            return dict(self.script_guard_state)
        return next(self._samples)

    def eval_on_selector_all(self, *_args):
        return []


class _NavigationRoute:
    def __init__(self):
        self.fallback_calls = 0
        self.abort_calls = 0

    def fallback(self):
        self.fallback_calls += 1

    def abort(self):
        self.abort_calls += 1


class _NavigationRequest:
    def __init__(self, url: str, *, navigation: bool = True):
        self.url = url
        self._navigation = navigation

    def is_navigation_request(self):
        return self._navigation


class _ProxySyntheticDnsPage(_OrdinaryPage):
    def __init__(self, response, samples, *, redirect_url: str):
        super().__init__(response, samples)
        self.url = redirect_url
        self._redirect_url = redirect_url
        self.navigation_route = _NavigationRoute()

    def goto(self, url, **kwargs):
        if url == "about:blank":
            return super().goto(url, **kwargs)
        self.url = self._redirect_url
        self.guard_context.request_handler(
            self.navigation_route,
            _NavigationRequest(self._redirect_url),
        )
        return self._response

    def eval_on_selector_all(self, *_args):
        return ["https://amazon.com/shop/alpha"]


class _GuardedSubresourcePage(_OrdinaryPage):
    def __init__(self, response, samples, *, resource_url: str):
        super().__init__(response, samples)
        self._resource_url = resource_url
        self.resource_route = _NavigationRoute()

    def goto(self, url, **kwargs):
        if url == "about:blank":
            return super().goto(url, **kwargs)
        self.url = "https://example.com/about"
        self.guard_context.request_handler(
            self.resource_route,
            _NavigationRequest(self._resource_url, navigation=False),
        )
        return self._response


def _dom_sample(text: str, *, elements: int = 12) -> dict:
    return {
        "ready_state": "complete",
        "title": "Creator website",
        "body_text": text,
        "body_html_length": 400,
        "element_count": elements,
        "anchor_count": 2,
    }


@pytest.mark.parametrize(
    ("response", "samples", "reason"),
    [
        (None, [], "missing_navigation_response"),
        (
            _FakeResponse(200),
            [_dom_sample(""), _dom_sample("")],
            "external_page_unhealthy",
        ),
        (
            _FakeResponse(200),
            [
                _dom_sample("Just a moment... checking your browser"),
                _dom_sample("Just a moment... verify you are human"),
            ],
            "external_page_unhealthy",
        ),
        (
            _FakeResponse(200),
            [
                _dom_sample("This domain is parked and this domain is for sale today"),
                _dom_sample("This domain is parked and this domain is for sale today"),
            ],
            "external_page_unhealthy",
        ),
    ],
)
def test_ordinary_target_needs_response_and_healthy_substantive_stable_dom(
    response, samples, reason
):
    page = _OrdinaryPage(response, samples)
    runtime = backfill._BrowserProbeRuntime(
        credentials={},
        proxy_loader=_proxy,
        headless=True,
        resolver=lambda _host: ["8.8.8.8"],
    )
    _activate_proxy_runtime(runtime, pages=(page,))
    result = runtime._target_check(page, "https://example.com/about")

    assert result["status"] == "failed"
    assert result["note"] == reason
    assert result["page_health"]["healthy"] is False


def test_ordinary_target_with_real_response_and_stable_content_succeeds():
    text = (
        "Welcome to the creator's official website. Read the biography, "
        "recent projects, and contact information here."
    )
    page = _OrdinaryPage(_FakeResponse(200), [_dom_sample(text), _dom_sample(text)])
    runtime = backfill._BrowserProbeRuntime(
        credentials={},
        proxy_loader=_proxy,
        headless=True,
        resolver=lambda _host: ["8.8.8.8"],
    )
    _activate_proxy_runtime(runtime, pages=(page,))

    result = runtime._target_check(page, "https://example.com/about")

    assert result["status"] == "succeeded"
    assert result["http_status"] == 200
    assert result["page_health"]["healthy"] is True
    assert result["page_health"]["stable"] is True


def test_proxy_runtime_allows_only_domain_fake_dns_across_navigation_final_and_href():
    text = (
        "Welcome to the creator's official website. Read the biography, "
        "recent projects, and contact information here."
    )
    page = _ProxySyntheticDnsPage(
        _FakeResponse(200),
        [_dom_sample(text), _dom_sample(text)],
        redirect_url="https://redirect.creator.example/about",
    )
    runtime = backfill._BrowserProbeRuntime(
        credentials={},
        proxy_loader=_proxy,
        headless=True,
        resolver=lambda _host: ["198.19.255.254"],
    )

    # The same TUN answer is unsafe until a validated proxy-backed context has
    # been established by _open_account.
    assert runtime._target_check(page, "https://creator.example/about")["status"] == (
        "unsafe"
    )
    _activate_proxy_runtime(runtime, pages=(page,))
    result = runtime._target_check(page, "https://creator.example/about")

    assert result["status"] == "succeeded"
    assert result["final_url"] == "https://redirect.creator.example/about"
    assert result["commerce_links"] == [
        {"url": "https://amazon.com/shop/alpha", "type": "Amazon"}
    ]
    assert page.navigation_route.fallback_calls == 1
    assert page.navigation_route.abort_calls == 0

    runtime._close_context()
    assert runtime._target_check(page, "https://creator.example/about")["status"] == (
        "unsafe"
    )


def test_external_guards_are_lazy_reused_and_revoke_fake_dns_on_close():
    runtime = backfill._BrowserProbeRuntime(
        credentials={},
        proxy_loader=_proxy,
        headless=True,
        resolver=lambda _host: ["198.18.4.5"],
    )
    page = _NeutralOnlyPage()
    context = _activate_proxy_runtime(runtime, pages=(page,))

    assert runtime._context_guard_installed is False
    assert context.websocket_handler is None
    with pytest.raises(backfill.StorefrontBackfillError, match="non-public"):
        runtime._safe_external_url("https://creator.example/about")

    runtime._ensure_external_target_guards(page)

    assert page.neutral_navigation_calls == 1
    assert context.route_calls == 1
    assert context.websocket_handler is not None
    assert runtime._safe_external_url("https://creator.example/about") == (
        "https://creator.example/about"
    )

    runtime._ensure_external_target_guards(page)
    assert page.neutral_navigation_calls == 1
    assert context.route_calls == 1

    runtime._close_context()
    with pytest.raises(backfill.StorefrontBackfillError, match="non-public"):
        runtime._safe_external_url("https://creator.example/about")


def test_proxy_runtime_still_aborts_private_navigation_redirect():
    text = (
        "Welcome to the creator's official website. Read the biography, "
        "recent projects, and contact information here."
    )
    page = _ProxySyntheticDnsPage(
        _FakeResponse(200),
        [_dom_sample(text), _dom_sample(text)],
        redirect_url="https://internal.creator.example/admin",
    )

    def resolver(host):
        if host == "internal.creator.example":
            return ["10.20.30.40"]
        return ["198.18.1.2"]

    runtime = backfill._BrowserProbeRuntime(
        credentials={},
        proxy_loader=_proxy,
        headless=True,
        resolver=resolver,
    )
    _activate_proxy_runtime(runtime, pages=(page,))

    result = runtime._target_check(page, "https://creator.example/about")

    assert result["status"] == "failed"
    assert result["page_health"]["reason"] == "navigation_or_dom_exception"
    assert page.navigation_route.abort_calls == 1


@pytest.mark.parametrize(
    "resource_url",
    [
        "http://127.0.0.1/admin",
        "http://169.254.169.254/latest/meta-data/",
        "https://metadata.google.internal/computeMetadata/v1/",
        "ws://creator.example/socket",
    ],
)
def test_proxy_runtime_aborts_unsafe_xhr_iframe_or_websocket(resource_url):
    text = (
        "Welcome to the creator's official website. Read the biography, "
        "recent projects, and contact information here."
    )
    page = _GuardedSubresourcePage(
        _FakeResponse(200),
        [_dom_sample(text), _dom_sample(text)],
        resource_url=resource_url,
    )
    runtime = backfill._BrowserProbeRuntime(
        credentials={},
        proxy_loader=_proxy,
        headless=True,
        resolver=lambda _host: ["198.18.2.3"],
    )
    _activate_proxy_runtime(runtime, pages=(page,))

    result = runtime._target_check(page, "https://creator.example/about")

    assert result["status"] == "failed"
    assert result["page_health"]["reason"] == "navigation_or_dom_exception"
    assert page.resource_route.abort_calls == 1
    assert page.resource_route.fallback_calls == 0


@pytest.mark.parametrize(
    "url",
    [
        "ws://127.0.0.1/socket",
        "ws://169.254.169.254/latest/meta-data/",
        "wss://creator.example/socket",
    ],
)
def test_proxy_context_websocket_route_closes_before_connection(url):
    class FakeWebSocketRoute:
        def __init__(self, route_url):
            self.url = route_url
            self.closed = None

        def close(self, **kwargs):
            self.closed = kwargs

    runtime = backfill._BrowserProbeRuntime(
        credentials={},
        proxy_loader=_proxy,
        headless=True,
        resolver=lambda _host: ["198.18.2.3"],
    )
    context = _activate_proxy_runtime(runtime)
    neutral_page = _NeutralOnlyPage()
    context.pages.append(neutral_page)
    neutral_page.guard_context = context
    runtime._ensure_external_target_guards(neutral_page)
    websocket = FakeWebSocketRoute(url)

    context.websocket_handler(websocket)

    assert context.websocket_pattern == "**/*"
    assert websocket.closed == {
        "code": 1008,
        "reason": "external websocket blocked",
    }


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/admin",
        "http://169.254.169.254/latest/meta-data/",
        "https://metadata.google.internal/computeMetadata/v1/",
    ],
)
def test_context_guard_aborts_popup_first_private_navigation(url):
    runtime = backfill._BrowserProbeRuntime(
        credentials={},
        proxy_loader=_proxy,
        headless=True,
        resolver=lambda _host: ["198.18.2.3"],
    )
    context = _activate_proxy_runtime(runtime)
    neutral_page = _NeutralOnlyPage()
    context.pages.append(neutral_page)
    neutral_page.guard_context = context
    runtime._ensure_external_target_guards(neutral_page)
    runtime._active_blocked_requests = []
    route = _NavigationRoute()

    context.request_handler(route, _NavigationRequest(url, navigation=True))

    assert context.request_pattern == "**/*"
    assert route.abort_calls == 1
    assert route.fallback_calls == 0
    assert runtime._active_blocked_requests == ["unsafe_external_request"]


def test_context_guard_allows_public_popup_first_navigation_through_proxy_fake_dns():
    runtime = backfill._BrowserProbeRuntime(
        credentials={},
        proxy_loader=_proxy,
        headless=True,
        resolver=lambda _host: ["198.18.2.3"],
    )
    context = _activate_proxy_runtime(runtime)
    neutral_page = _NeutralOnlyPage()
    context.pages.append(neutral_page)
    neutral_page.guard_context = context
    runtime._ensure_external_target_guards(neutral_page)
    runtime._active_blocked_requests = []
    route = _NavigationRoute()

    context.request_handler(
        route,
        _NavigationRequest("https://public.creator.example/popup", navigation=True),
    )

    assert route.fallback_calls == 1
    assert route.abort_calls == 0
    assert runtime._active_blocked_requests == []


def test_target_probe_closes_new_popup_before_clearing_active_guard():
    text = (
        "Welcome to the creator's official website. Read the biography, "
        "recent projects, and contact information here."
    )

    class Popup:
        def __init__(self):
            self.closed = False
            self.closed_in_page_event = None
            self.context = None

        def close(self, **_kwargs):
            self.closed = True
            self.context.pages.remove(self)

    class PopupOpeningPage(_OrdinaryPage):
        def __init__(self, response, samples):
            super().__init__(response, samples)
            self.context = None
            self.popup = Popup()

        def goto(self, *_args, **_kwargs):
            if _args and _args[0] == "about:blank":
                return super().goto(*_args, **_kwargs)
            self.popup.context = self.context
            self.context.pages.append(self.popup)
            self.context.page_handler(self.popup)
            self.popup.closed_in_page_event = self.popup.closed
            return self._response

    page = PopupOpeningPage(
        _FakeResponse(200), [_dom_sample(text), _dom_sample(text)]
    )
    runtime = backfill._BrowserProbeRuntime(
        credentials={},
        proxy_loader=_proxy,
        headless=True,
        resolver=lambda _host: ["198.18.2.3"],
    )
    context = _activate_proxy_runtime(runtime, pages=(page,))
    page.context = context

    result = runtime._target_check(page, "https://creator.example/about")

    assert result["status"] == "failed"
    assert page.popup.closed_in_page_event is False
    assert page.popup.closed is True
    assert context.pages == [page]
    assert runtime._active_blocked_requests is None
    assert runtime._context_guard_installed is True


def test_websocket_reconnect_storm_is_bounded_and_does_not_starve_waits():
    class FakeWebSocketRoute:
        close_calls = 0

        def close(self, **_kwargs):
            type(self).close_calls += 1

    class ReconnectStormPage(_OrdinaryPage):
        def __init__(self, response, samples):
            super().__init__(response, samples)
            self.wait_calls = 0

        def wait_for_timeout(self, _milliseconds):
            self.wait_calls += 1
            for _ in range(2000):
                self.guard_context.websocket_handler(FakeWebSocketRoute())

    text = (
        "Welcome to the creator's official website. Read the biography, "
        "recent projects, and contact information here."
    )
    page = ReconnectStormPage(
        _FakeResponse(200), [_dom_sample(text), _dom_sample(text)]
    )
    runtime = backfill._BrowserProbeRuntime(
        credentials={},
        proxy_loader=_proxy,
        headless=True,
        resolver=lambda _host: ["198.18.2.3"],
    )
    _activate_proxy_runtime(runtime, pages=(page,))
    runtime._active_blocked_requests = []

    result = runtime._target_check(page, "https://creator.example/about")

    assert result["status"] == "failed"
    assert page.wait_calls == 2
    assert FakeWebSocketRoute.close_calls == 4000
    assert runtime._active_blocked_requests == ["external_websocket_blocked"]


def test_probe_closes_context_and_next_candidate_reopens(monkeypatch):
    import browser_collect_v2 as browser_collect

    class LifecyclePage(_NeutralOnlyPage):
        def close(self, **_kwargs):
            self.closed = True

    runtime = backfill._BrowserProbeRuntime(
        credentials={},
        proxy_loader=_proxy,
        headless=True,
        resolver=lambda _host: ["198.18.2.3"],
    )
    opened_contexts = []

    def fake_open_account(_account, slot):
        runtime._close_context()
        page = LifecyclePage()
        context = _ActiveGuardContext((page,))
        page.guard_context = context
        runtime._ctx = context
        runtime._page = page
        runtime._active_slot = slot
        runtime._proxy_enforced = True
        opened_contexts.append(context)

    runtime._open_account = fake_open_account

    def fetch_profile(page, handle):
        page.url = f"https://instagram.com/{handle}/"
        return {
            "handle": handle,
            "external_url": (
                f"https://instagram.com/{handle}/contact"
                if handle == "alpha"
                else None
            ),
        }

    monkeypatch.setattr(browser_collect, "fetch_profile_browser", fetch_profile)
    monkeypatch.setattr(
        backfill,
        "_profile_identity",
        lambda _page, _handle, _profile: (True, {"reason": "verified"}),
    )
    monkeypatch.setattr(
        backfill,
        "_profile_health",
        lambda _page, _profile: (True, {"reason": "healthy"}),
    )
    monkeypatch.setattr(
        backfill,
        "_bio_more_declaration",
        lambda _page, _profile, **_kwargs: (
            False,
            None,
            {"signal_detected": False},
        ),
    )
    account = type("Account", (), {"username": "shallow_one"})()

    first = runtime.probe("alpha", account, lambda: None, slot=0)
    second = runtime.probe("beta", account, lambda: None, slot=0)

    assert first["status"] == "confirmed_no"
    assert second["status"] == "confirmed_no"
    assert len(opened_contexts) == 2
    assert opened_contexts[0].route_calls == 1
    assert not hasattr(opened_contexts[1], "route_calls")
    assert all(context.closed is True for context in opened_contexts)
    assert runtime._ctx is None


def test_real_playwright_external_init_guard_prevents_reconnect_starvation(tmp_path):
    import time

    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(tmp_path / "real-playwright-profile"),
            channel="chrome",
            headless=True,
        )
        page = context.pages[0] if context.pages else context.new_page()
        runtime = backfill._BrowserProbeRuntime(
            credentials={},
            proxy_loader=_proxy,
            headless=True,
            resolver=lambda _host: ["198.18.2.3"],
        )
        runtime._ctx = context
        runtime._page = page
        runtime._proxy_enforced = True
        runtime._active_blocked_requests = []
        try:
            page.goto("data:text/html,<title>IG profile phase</title>")
            page.evaluate(
                """() => {
                  try {
                    const socket = new WebSocket('ws://127.0.0.1:9/profile');
                    socket.onerror = () => {};
                  } catch (_) {}
                }"""
            )
            started = time.monotonic()
            page.wait_for_timeout(100)
            assert time.monotonic() - started < 3
            assert runtime._context_guard_installed is False
            assert runtime._active_blocked_requests == []

            runtime._ensure_external_target_guards(page)
            page.goto("data:text/html,<title>external target phase</title>")
            started = time.monotonic()
            observed = page.evaluate(
                """() => {
                  let securityErrors = 0;
                  for (let i = 0; i < 2000; i += 1) {
                    try { new WebSocket('ws://127.0.0.1:9/reconnect'); }
                    catch (error) {
                      if (error && error.name === 'SecurityError') securityErrors += 1;
                    }
                  }
                  const popup = window.open('http://127.0.0.1/private');
                  const wsDescriptor = Object.getOwnPropertyDescriptor(
                    globalThis, 'WebSocket'
                  );
                  const openDescriptor = Object.getOwnPropertyDescriptor(
                    globalThis, 'open'
                  );
                  return {
                    securityErrors,
                    popupWasNull: popup === null,
                    wsConfigurable: wsDescriptor.configurable,
                    wsWritable: wsDescriptor.writable,
                    openConfigurable: openDescriptor.configurable,
                    openWritable: openDescriptor.writable,
                    state: globalThis.__sopStorefrontGuardState
                  };
                }"""
            )
            page.wait_for_timeout(50)
            assert time.monotonic() - started < 3
            assert observed == {
                "securityErrors": 2000,
                "popupWasNull": True,
                "wsConfigurable": False,
                "wsWritable": False,
                "openConfigurable": False,
                "openWritable": False,
                "state": {"websocket_blocked": 2000, "popup_blocked": 1},
            }
            # No constructor reached Playwright's native WS route; the marker
            # is converted into bounded audit evidence explicitly.
            assert runtime._active_blocked_requests == []
            runtime._record_external_script_blocks(page)
            assert runtime._active_blocked_requests == [
                "external_script_websocket_blocked",
                "external_script_popup_blocked",
            ]
        finally:
            runtime._close_context()

    assert runtime._proxy_enforced is False
    assert runtime._context_guard_installed is False


def test_proxy_synthetic_dns_lifetime_binds_to_successful_context_launch(tmp_path):
    class FakePage:
        def set_default_timeout(self, _value):
            return None

        def set_default_navigation_timeout(self, _value):
            return None

        def close(self, **_kwargs):
            return None

    class FakeContext:
        def __init__(self):
            self.pages = [FakePage()]

        def route(self, *_args):
            return None

        def route_web_socket(self, pattern, handler):
            self.websocket_pattern = pattern
            self.websocket_handler = handler

        def on(self, event, handler):
            assert event == "page"
            self.page_handler = handler

        def add_init_script(self, script):
            self.init_script = script

        def add_cookies(self, cookies):
            self.cookies = cookies

        def unroute_all(self, **_kwargs):
            return None

        def close(self):
            return None

    class FakeChromium:
        def __init__(self, *, fail=False):
            self.fail = fail
            self.options = None
            self.context = FakeContext()

        def launch_persistent_context(self, **options):
            self.options = options
            if self.fail:
                raise RuntimeError("launch failed")
            return self.context

    account = type(
        "Account",
        (),
        {"username": "shallow_one", "profile_path": tmp_path / "profile"},
    )()
    credentials = {
        "shallow_one": {"sessionid": "session", "ds_user_id": "123"}
    }

    chromium = FakeChromium()
    runtime = backfill._BrowserProbeRuntime(
        credentials=credentials,
        proxy_loader=_proxy,
        headless=True,
        resolver=lambda _host: ["198.18.1.2"],
    )
    runtime._pw = type("Playwright", (), {"chromium": chromium})()
    runtime._open_account(account, 0)

    assert runtime._proxy_enforced is True
    assert chromium.options["proxy"] == _proxy(session="ignored", ttl=1)
    assert runtime._context_guard_installed is False
    with pytest.raises(backfill.StorefrontBackfillError, match="non-public"):
        runtime._safe_external_url("https://creator.example/about")
    chromium.context.pages[0].goto = lambda *_args, **_kwargs: _FakeResponse(200)
    runtime._ensure_external_target_guards(chromium.context.pages[0])
    assert chromium.context.websocket_pattern == "**/*"
    assert runtime._safe_external_url("https://creator.example/about") == (
        "https://creator.example/about"
    )

    runtime._close_context()
    assert runtime._proxy_enforced is False
    with pytest.raises(backfill.StorefrontBackfillError, match="non-public"):
        runtime._safe_external_url("https://creator.example/about")

    failing_runtime = backfill._BrowserProbeRuntime(
        credentials=credentials,
        proxy_loader=_proxy,
        headless=True,
        resolver=lambda _host: ["198.18.1.2"],
    )
    failing_runtime._pw = type(
        "Playwright", (), {"chromium": FakeChromium(fail=True)}
    )()
    with pytest.raises(RuntimeError, match="launch failed"):
        failing_runtime._open_account(account, 0)
    assert failing_runtime._proxy_enforced is False


@pytest.mark.parametrize("resource_url", ["data:text/plain,ok", "blob:https://creator.example/id", "about:blank"])
def test_proxy_runtime_allows_only_non_network_subresource_schemes(resource_url):
    text = (
        "Welcome to the creator's official website. Read the biography, "
        "recent projects, and contact information here."
    )
    page = _GuardedSubresourcePage(
        _FakeResponse(200),
        [_dom_sample(text), _dom_sample(text)],
        resource_url=resource_url,
    )
    runtime = backfill._BrowserProbeRuntime(
        credentials={},
        proxy_loader=_proxy,
        headless=True,
        resolver=lambda _host: ["198.18.2.3"],
    )
    _activate_proxy_runtime(runtime, pages=(page,))

    result = runtime._target_check(page, "https://creator.example/about")

    assert result["status"] == "succeeded"
    assert page.resource_route.abort_calls == 0
    assert page.resource_route.fallback_calls == 1


class _BodyLocator:
    def __init__(self, text: str):
        self._text = text

    def inner_text(self, **_kwargs):
        return self._text


class _ProfilePage:
    def __init__(self, text: str, *, url: str = "https://instagram.com/alpha/"):
        self.url = url
        self._text = text

    def locator(self, selector):
        assert selector == "body"
        return _BodyLocator(self._text)


def _fresh_profile(**overrides):
    profile = {
        "handle": "alpha",
        "follower_count": 12000,
        "full_name": "Alice Creator",
        "biography": "Skincare reviews and tutorials",
        "external_url": None,
        "codes": ["/p/one/"],
        "post_refs": [{"url": "/p/one/"}],
        "is_private": False,
        "_profile_identity_evidence": {
            "og_handle": "alpha",
            "canonical_handle": "alpha",
            "surface_handle": "alpha",
        },
        "_profile_surface_evidence": {"body_text_length": 180},
    }
    profile.update(overrides)
    return profile


def test_profile_identity_requires_independent_og_or_surface_match():
    page = _ProfilePage("Alice Creator 12K followers skincare reviews and tutorials")
    verified, evidence = backfill._profile_identity(page, "alpha", _fresh_profile())
    assert verified is True
    assert evidence["independent_match"] is True

    input_only = _fresh_profile(_profile_identity_evidence={})
    verified, evidence = backfill._profile_identity(page, "alpha", input_only)
    assert verified is False
    assert evidence["page_url_match"] is True
    assert evidence["independent_match"] is False

    contradiction = _fresh_profile(
        _profile_identity_evidence={
            "og_handle": "different",
            "canonical_handle": "alpha",
            "surface_handle": "alpha",
        }
    )
    verified, evidence = backfill._profile_identity(page, "alpha", contradiction)
    assert verified is False
    assert evidence["contradictory_surface"] is True


def test_profile_identity_ignores_weak_surface_token_when_strong_og_is_exact():
    page = _ProfilePage("Alice Creator 12K followers skincare reviews and tutorials")
    profile = _fresh_profile(
        _profile_identity_evidence={
            "og_handle": "alpha",
            "canonical_handle": "alpha",
            "surface_handle": "Posts",
            "rejected_surface_handles": ["Reels", "Follow"],
        }
    )

    verified, evidence = backfill._profile_identity(page, "alpha", profile)

    assert verified is True
    assert evidence["strong_contradiction"] is False
    assert evidence["weak_surface_nonmatch"] is True
    assert evidence["rejected_surface_handles"] == ["reels", "follow"]


def test_profile_identity_accepts_exact_trusted_surface_without_og():
    page = _ProfilePage("Alice Creator 12K followers skincare reviews and tutorials")
    profile = _fresh_profile(
        _profile_identity_evidence={
            "og_handle": None,
            "canonical_handle": "alpha",
            "surface_handle": "alpha",
            "surface_handle_source": "heading_text",
        }
    )

    verified, evidence = backfill._profile_identity(page, "alpha", profile)

    assert verified is True
    assert evidence["independent_match"] is True
    assert evidence["trusted_surface_match"] is True


def test_profile_identity_rejects_weak_surface_without_og_and_strong_mismatch():
    page = _ProfilePage("Alice Creator 12K followers skincare reviews and tutorials")
    weak_only = _fresh_profile(
        _profile_identity_evidence={
            "og_handle": None,
            "canonical_handle": "alpha",
            "surface_handle": "Posts",
            "surface_handle_source": "heading_text",
        }
    )
    verified, evidence = backfill._profile_identity(page, "alpha", weak_only)
    assert verified is False
    assert evidence["independent_match"] is False
    assert evidence["strong_contradiction"] is False

    untrusted_exact_surface = _fresh_profile(
        _profile_identity_evidence={
            "og_handle": None,
            "canonical_handle": "alpha",
            "surface_handle": "alpha",
            "surface_handle_source": "unbound_text",
        }
    )
    verified, evidence = backfill._profile_identity(
        page, "alpha", untrusted_exact_surface
    )
    assert verified is False
    assert evidence["trusted_surface_match"] is False

    strong_mismatch = _fresh_profile(
        _profile_identity_evidence={
            "og_handle": "alpha",
            "canonical_handle": "different",
            "surface_handle": "alpha",
        }
    )
    verified, evidence = backfill._profile_identity(page, "alpha", strong_mismatch)
    assert verified is False
    assert evidence["strong_contradiction"] is True


@pytest.mark.parametrize(
    "body",
    [
        "",
        "Log in to continue",
        "Connectez-vous pour continuer",
        "Войдите, чтобы продолжить",
        "로그인하여 계속",
    ],
)
def test_profile_empty_or_multilingual_login_surface_is_unhealthy(body):
    healthy, evidence = backfill._profile_health(
        _ProfilePage(body), _fresh_profile()
    )
    assert healthy is False
    assert evidence["reason"] != "healthy"


def test_profile_needs_at_least_one_real_profile_signal():
    body = "This is a rendered Instagram surface with enough visible body content."
    no_signals = _fresh_profile(
        follower_count=None,
        full_name=None,
        biography=None,
        codes=[],
        post_refs=[],
        external_url=None,
        is_private=False,
    )
    healthy, evidence = backfill._profile_health(_ProfilePage(body), no_signals)
    assert healthy is False
    assert evidence["signals"] == []

    healthy, evidence = backfill._profile_health(_ProfilePage(body), _fresh_profile())
    assert healthy is True
    assert evidence["signals"]


def test_unresolved_collect_writes_non_applicable_plan_and_apply_refuses(tmp_path):
    db = tmp_path / "creators.db"
    _create_db(db)

    def probe(handle, *args):
        if handle == "alpha":
            return _yes(handle, *args)
        result = _no(handle, *args)
        result["evidence"]["failures"] = ["external_target_failed"]
        result["evidence"]["target_checks_complete"] = False
        return result

    plan, plan_path, _ = _collect(tmp_path, db, probe=probe)
    before = _db_digest(db)
    assert plan["apply_allowed"] is False
    assert plan["unresolved_count"] == 1
    with pytest.raises(backfill.StorefrontBackfillError, match="unresolved plan"):
        backfill.apply_plan(
            db_path=db,
            batch_id=BATCH,
            expected_count=2,
            plan_path=plan_path,
            expected_plan_sha256=plan["plan_sha256"],
        )
    assert _db_digest(db) == before


def test_resume_reuses_61_reprobes_only_7_on_unused_accounts_and_applies_all(
    tmp_path,
):
    handles = tuple(f"creator_{index:02d}" for index in range(68))
    unresolved = frozenset(handles[-7:])
    db = tmp_path / "creators.db"
    _create_db(db, handles=handles)

    def initial_probe(handle, *_args):
        if handle in unresolved:
            return _unknown(handle)
        return _yes(handle) if int(handle[-2:]) % 2 == 0 else _no(handle)

    initial, _, usernames = _collect_many(
        tmp_path,
        db,
        handles=handles,
        plan_name="partial-v2.plan.json",
        probe=initial_probe,
    )
    assert initial["unresolved_count"] == 7
    legacy_path = tmp_path / "partial-v1.plan.json"
    legacy = _legacy_plan(initial, legacy_path)
    prior_by_handle = {row["handle"]: row for row in legacy["rows"]}
    calls: list[tuple[str, str]] = []

    def resumed_probe(handle, account, _heartbeat):
        calls.append((handle, account.username))
        return _yes(handle)

    resumed, resumed_path, _ = _collect_many(
        tmp_path,
        db,
        handles=handles,
        plan_name="resumed-v2.plan.json",
        probe=resumed_probe,
        prior_plan=legacy_path,
        expected_prior_sha=legacy["plan_sha256"],
    )

    assert len(calls) == 7
    assert {handle for handle, _ in calls} == unresolved
    assert not ({handle for handle, _ in calls} & set(handles[:-7]))
    username_to_slot = {username: slot for slot, username in enumerate(usernames)}
    for handle, username in calls:
        old_slots = {
            item["account_slot"]
            for item in prior_by_handle[handle]["result"]["evidence"]["attempts"]
        }
        assert username_to_slot[username] not in old_slots
    assert len(resumed["rows"]) == 68
    assert resumed["unresolved_count"] == 0
    assert resumed["apply_allowed"] is True
    assert resumed["resume"]["reused_count"] == 61
    assert resumed["resume"]["recollected_count"] == 7
    assert resumed["plan_sha256"] == backfill.plan_sha256(resumed_path)
    for row in resumed["rows"]:
        provenance = row["provenance"]
        if row["handle"] in unresolved:
            assert provenance["decision_source"] == "current_run"
            assert provenance["result_attempt_origin"] == "current_run"
            assert provenance["prior_status"] == "unknown"
            assert provenance["prior_attempts"]
            assert provenance["current_attempts"]
        else:
            assert provenance["decision_source"] == "prior_plan"
            assert provenance["result_attempt_origin"] == "prior_plan"
            assert provenance["current_attempts"] == []

    applied = backfill.apply_plan(
        db_path=db,
        batch_id=BATCH,
        expected_count=68,
        plan_path=resumed_path,
        expected_plan_sha256=resumed["plan_sha256"],
    )
    assert applied["applied_count"] == 68
    assert {
        row["storefront_status"] for row in _rows(db).values()
    } <= {"confirmed_yes", "confirmed_no"}


def test_resume_requires_prior_path_and_external_sha_as_a_pair(tmp_path):
    db = tmp_path / "creators.db"
    _create_db(db)
    shallow, deep, profiles = _account_fixture(tmp_path)
    common = dict(
        db_path=db,
        batch_id=BATCH,
        expected_count=2,
        expected_handle_set_sha256=backfill.handle_set_sha256(("alpha", "beta")),
        accounts_file=shallow,
        deep_accounts_file=deep,
        profile_root=profiles,
        lease_db=tmp_path / "lease.db",
        probe_candidate=_mixed_probe,
        proxy_loader=_proxy,
    )
    with pytest.raises(backfill.StorefrontBackfillError, match="supplied together"):
        backfill.collect_plan(
            **common,
            plan_path=tmp_path / "missing-sha.plan.json",
            prior_plan_path=tmp_path / "prior.plan.json",
        )
    with pytest.raises(backfill.StorefrontBackfillError, match="supplied together"):
        backfill.collect_plan(
            **common,
            plan_path=tmp_path / "missing-path.plan.json",
            expected_prior_plan_sha256="0" * 64,
        )
    assert not (tmp_path / "missing-sha.plan.json").exists()
    assert not (tmp_path / "missing-path.plan.json").exists()


def test_resume_refuses_external_sha_snapshot_result_and_per_account_drift(tmp_path):
    handles = tuple(f"guard_{index:02d}" for index in range(8))
    unresolved = frozenset(handles[-2:])
    db = tmp_path / "creators.db"
    _create_db(db, handles=handles)

    def initial_probe(handle, *_args):
        return _unknown(handle) if handle in unresolved else _yes(handle)

    initial, initial_path, _ = _collect_many(
        tmp_path,
        db,
        handles=handles,
        plan_name="guard-source.plan.json",
        probe=initial_probe,
    )
    calls: list[str] = []

    def forbidden_probe(handle, *_args):
        calls.append(handle)
        return _yes(handle)

    with pytest.raises(backfill.StorefrontBackfillError, match="plan SHA-256 mismatch"):
        _collect_many(
            tmp_path,
            db,
            handles=handles,
            plan_name="wrong-sha.plan.json",
            probe=forbidden_probe,
            prior_plan=initial_path,
            expected_prior_sha="0" * 64,
        )
    assert calls == []

    with sqlite3.connect(db) as connection:
        connection.execute(
            "UPDATE creator_profiles SET stage_updated_at=? WHERE handle=?",
            ("2026-08-11T10:00:00Z", handles[0]),
        )
        connection.commit()
    with pytest.raises(backfill.StorefrontBackfillError, match="snapshot differs"):
        _collect_many(
            tmp_path,
            db,
            handles=handles,
            plan_name="stale-snapshot.plan.json",
            probe=forbidden_probe,
            prior_plan=initial_path,
            expected_prior_sha=initial["plan_sha256"],
        )
    assert calls == []
    with sqlite3.connect(db) as connection:
        connection.execute(
            "UPDATE creator_profiles SET stage_updated_at=? WHERE handle=?",
            ("2026-08-11T08:00:00Z", handles[0]),
        )
        connection.commit()

    tampered = copy.deepcopy(initial)
    tampered_row = next(
        row for row in tampered["rows"] if row["result"]["status"] == "confirmed_yes"
    )
    tampered_row["result"]["evidence"]["profile_healthy"] = False
    tampered_path = tmp_path / "invalid-result.plan.json"
    _write_rehashed_plan(tampered_path, tampered)
    with pytest.raises(backfill.StorefrontBackfillError, match="current validator"):
        _collect_many(
            tmp_path,
            db,
            handles=handles,
            plan_name="invalid-result-output.plan.json",
            probe=forbidden_probe,
            prior_plan=tampered_path,
            expected_prior_sha=tampered["plan_sha256"],
        )
    assert calls == []

    with pytest.raises(backfill.StorefrontBackfillError, match="per-account contract changed"):
        _collect_many(
            tmp_path,
            db,
            handles=handles,
            plan_name="per-account-drift.plan.json",
            probe=forbidden_probe,
            prior_plan=initial_path,
            expected_prior_sha=initial["plan_sha256"],
            per_account=5,
        )
    assert calls == []


def test_apply_rejects_reused_slot_and_incomplete_resume_source_coverage(tmp_path):
    db, handles, plan, _ = _resolved_resume_fixture(tmp_path)
    before = _db_digest(db)

    duplicate_slot = copy.deepcopy(plan)
    recollected = next(
        row
        for row in duplicate_slot["rows"]
        if row["provenance"]["decision_source"] == "current_run"
        and row["provenance"]["prior_plan"] is not None
    )
    old_slot = recollected["provenance"]["prior_attempts"][0]["account_slot"]
    recollected["provenance"]["scheduled_current_slots"][0] = old_slot
    recollected["provenance"]["current_attempts"][0]["account_slot"] = old_slot
    recollected["result"]["evidence"]["attempts"][0]["account_slot"] = old_slot
    assignment = {
        row["handle"].casefold(): row["provenance"]["scheduled_current_slots"]
        for row in duplicate_slot["rows"]
        if row["provenance"]["prior_plan"] is not None
        and row["provenance"]["decision_source"] == "current_run"
    }
    duplicate_slot["resume"]["account_assignment_sha256"] = backfill._sha_json(
        assignment
    )
    duplicate_path = tmp_path / "duplicate-slot.plan.json"
    _write_rehashed_plan(duplicate_path, duplicate_slot)
    with pytest.raises(backfill.StorefrontBackfillError, match="reused a prior account slot"):
        backfill.apply_plan(
            db_path=db,
            batch_id=BATCH,
            expected_count=len(handles),
            plan_path=duplicate_path,
            expected_plan_sha256=duplicate_slot["plan_sha256"],
        )
    assert _db_digest(db) == before
    incomplete_source = copy.deepcopy(plan)
    reused = next(
        row
        for row in incomplete_source["rows"]
        if row["provenance"]["decision_source"] == "prior_plan"
    )
    provenance = reused["provenance"]
    result_attempts = reused["result"]["evidence"]["attempts"]
    summaries = [
        {
            "attempt": item["attempt"],
            "account_slot": item["account_slot"],
            "status": item["status"],
            "evidence_sha256": backfill._sha_json(item["evidence"]),
        }
        for item in result_attempts
    ]
    scheduled = [item["account_slot"] for item in result_attempts]
    if len(scheduled) == 1:
        scheduled.append((scheduled[0] + 1) % 6)
    provenance.update(
        {
            "decision_source": "current_run",
            "result_attempt_origin": "current_run",
            "prior_plan": None,
            "prior_status": None,
            "prior_attempts": [],
            "current_attempts": summaries,
            "scheduled_current_slots": scheduled,
        }
    )
    incomplete_path = tmp_path / "incomplete-source.plan.json"
    _write_rehashed_plan(incomplete_path, incomplete_source)
    with pytest.raises(backfill.StorefrontBackfillError, match="complete cohort"):
        backfill.apply_plan(
            db_path=db,
            batch_id=BATCH,
            expected_count=len(handles),
            plan_path=incomplete_path,
            expected_plan_sha256=incomplete_source["plan_sha256"],
        )
    assert _db_digest(db) == before


def test_resume_rejects_incompatible_schema_and_source_bytes_changed_midrun(tmp_path):
    handles = tuple(f"freeze_{index:02d}" for index in range(8))
    unresolved = frozenset(handles[-2:])
    db = tmp_path / "creators.db"
    _create_db(db, handles=handles)

    def initial_probe(handle, *_args):
        return _unknown(handle) if handle in unresolved else _yes(handle)

    initial, initial_path, _ = _collect_many(
        tmp_path,
        db,
        handles=handles,
        plan_name="freeze-source.plan.json",
        probe=initial_probe,
    )
    incompatible = copy.deepcopy(initial)
    incompatible["schema"] = "sop-v2-storefront-backfill-plan-v999"
    incompatible_path = tmp_path / "incompatible.plan.json"
    _write_rehashed_plan(incompatible_path, incompatible)
    with pytest.raises(backfill.StorefrontBackfillError, match="unsupported plan schema"):
        _collect_many(
            tmp_path,
            db,
            handles=handles,
            plan_name="incompatible-output.plan.json",
            probe=_yes,
            prior_plan=incompatible_path,
            expected_prior_sha=incompatible["plan_sha256"],
        )

    changed = False

    def mutating_probe(handle, *_args):
        nonlocal changed
        if not changed:
            initial_path.write_bytes(initial_path.read_bytes() + b"\n")
            changed = True
        return _yes(handle)

    output = tmp_path / "changed-source-output.plan.json"
    with pytest.raises(backfill.StorefrontBackfillError, match="bytes changed"):
        _collect_many(
            tmp_path,
            db,
            handles=handles,
            plan_name=output.name,
            probe=mutating_probe,
            prior_plan=initial_path,
            expected_prior_sha=initial["plan_sha256"],
        )
    assert changed is True
    assert not output.exists()


def test_apply_rejects_rehashed_plan_with_weakened_collection_contract(tmp_path):
    db = tmp_path / "creators.db"
    _create_db(db)
    plan, _, _ = _collect(tmp_path, db)
    before = _db_digest(db)
    weakened = copy.deepcopy(plan)
    weakened["collection_contract"]["proxy_required"] = False
    weakened_path = tmp_path / "weakened-contract.plan.json"
    _write_rehashed_plan(weakened_path, weakened)

    with pytest.raises(
        backfill.StorefrontBackfillError,
        match="collection contract is incompatible: proxy_required",
    ):
        backfill.apply_plan(
            db_path=db,
            batch_id=BATCH,
            expected_count=2,
            plan_path=weakened_path,
            expected_plan_sha256=weakened["plan_sha256"],
        )
    assert _db_digest(db) == before


def test_apply_requires_expected_sha_and_only_changes_allowlisted_storefront_data(
    tmp_path,
):
    db = tmp_path / "creators.db"
    _create_db(db)
    plan, plan_path, _ = _collect(tmp_path, db)
    before_rows = _rows(db)

    with pytest.raises(backfill.StorefrontBackfillError, match="plan SHA-256 mismatch"):
        backfill.apply_plan(
            db_path=db,
            batch_id=BATCH,
            expected_count=2,
            plan_path=plan_path,
            expected_plan_sha256="0" * 64,
        )
    assert _rows(db)["alpha"]["stage_json"] == before_rows["alpha"]["stage_json"]

    result = backfill.apply_plan(
        db_path=db,
        batch_id=BATCH,
        expected_count=2,
        plan_path=plan_path,
        expected_plan_sha256=plan["plan_sha256"],
    )
    assert result["applied_count"] == 2
    assert result["protected_fields_unchanged"] is True

    after_rows = _rows(db)
    assert after_rows["alpha"]["storefront_status"] == "confirmed_yes"
    assert after_rows["alpha"]["amazon_storefront_link"] == (
        "https://amazon.com/shop/alpha"
    )
    assert after_rows["beta"]["storefront_status"] == "confirmed_no"
    assert after_rows["beta"]["amazon_storefront_link"] is None
    for handle in ("alpha", "beta"):
        before_row = before_rows[handle]
        after_row = after_rows[handle]
        for column in before_row.keys():
            if column not in backfill.STOREFRONT_DB_COLUMNS:
                assert after_row[column] == before_row[column]
        before_stage = json.loads(before_row["stage_json"])
        after_stage = json.loads(after_row["stage_json"])
        assert backfill._protected_sha(after_stage) == backfill._protected_sha(
            before_stage
        )
        assert after_stage["deep_evidence"] == before_stage["deep_evidence"]
        assert after_stage["pricing_estimate"] == before_stage["pricing_estimate"]
        assert after_stage["comment_translations"] == before_stage[
            "comment_translations"
        ]
        assert after_stage["stage3_attempt_ledger"] == before_stage[
            "stage3_attempt_ledger"
        ]
        assert after_stage["stage3_canonical_attempt"] == "attempt-1"
        assert after_stage["pricing_canonical_attempt"] == "pricing-1"
        assert after_stage["deep_retry_status"] == "complete"
        assert (
            backfill._changed_top_level_keys(before_stage, after_stage)
            <= backfill.STOREFRONT_STAGE_FIELDS
        )


def test_apply_rolls_back_whole_batch_when_late_row_cas_changes(tmp_path):
    db = tmp_path / "creators.db"
    _create_db(db)
    plan, plan_path, _ = _collect(tmp_path, db)
    before_alpha = _rows(db)["alpha"]["stage_json"]

    connection = sqlite3.connect(db)
    try:
        beta_raw = connection.execute(
            "SELECT stage_json FROM creator_profiles WHERE handle='beta'"
        ).fetchone()[0]
        beta = json.loads(beta_raw)
        beta["deep_evidence"]["posts"][0]["comments"] = 10
        connection.execute(
            "UPDATE creator_profiles SET stage_json=? WHERE handle='beta'",
            (json.dumps(beta, ensure_ascii=False),),
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(backfill.StorefrontBackfillError, match="stage_json CAS changed"):
        backfill.apply_plan(
            db_path=db,
            batch_id=BATCH,
            expected_count=2,
            plan_path=plan_path,
            expected_plan_sha256=plan["plan_sha256"],
        )

    rows = _rows(db)
    assert rows["alpha"]["stage_json"] == before_alpha
    assert rows["alpha"]["storefront_status"] == "unknown"
    assert json.loads(rows["beta"]["stage_json"])["deep_evidence"]["posts"][0][
        "comments"
    ] == 10


def test_apply_cas_protects_stale_hot_storefront_columns_and_rolls_back(tmp_path):
    db = tmp_path / "creators.db"
    _create_db(db)
    plan, plan_path, _ = _collect(tmp_path, db)
    before_alpha = _rows(db)["alpha"]["stage_json"]

    connection = sqlite3.connect(db)
    try:
        connection.execute(
            "UPDATE creator_profiles SET storefront_status='confirmed_no' "
            "WHERE handle='beta'"
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(
        backfill.StorefrontBackfillError, match="storefront_status CAS changed"
    ):
        backfill.apply_plan(
            db_path=db,
            batch_id=BATCH,
            expected_count=2,
            plan_path=plan_path,
            expected_plan_sha256=plan["plan_sha256"],
        )

    rows = _rows(db)
    assert rows["alpha"]["stage_json"] == before_alpha
    assert rows["alpha"]["storefront_status"] == "unknown"
    assert rows["beta"]["storefront_status"] == "confirmed_no"
