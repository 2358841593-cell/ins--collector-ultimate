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
        self.request_pattern = pattern
        self.request_handler = handler

    def route_web_socket(self, pattern, handler):
        self.websocket_pattern = pattern
        self.websocket_handler = handler

    def on(self, event, handler):
        assert event == "page"
        self.page_handler = handler

    def unroute_all(self, **_kwargs):
        return None

    def close(self):
        return None


def _activate_proxy_runtime(runtime, *, pages=()):
    context = _ActiveGuardContext(pages)
    runtime._ctx = context
    runtime._install_context_network_guards(context)
    runtime._proxy_enforced = True
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
                return True
            return ["https://instagram.com/alpha", "https://example.com/shop"]

        def click(self, *_args, **_kwargs):
            return None

        def wait_for_timeout(self, _milliseconds):
            return None

    assert browser_collect._expand_bio_links(
        PopupPage(), include_social=True
    ) == ["https://instagram.com/alpha", "https://example.com/shop"]

    class AmbiguousDeclarationPage:
        def evaluate(self, _script):
            return [
                {
                    "text": "example.com und 2 weitere Links",
                    "popup": "dialog",
                    "expanded": "false",
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
                    "interactive": False,
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
    class NoNavigationPage:
        def __getattr__(self, name):
            raise AssertionError(f"unsafe target must not use page.{name}")

    runtime = backfill._BrowserProbeRuntime(
        credentials={},
        proxy_loader=_proxy,
        headless=True,
        resolver=lambda _host: [address],
    )
    _activate_proxy_runtime(runtime)

    result = runtime._target_check(
        NoNavigationPage(), "https://creator.example/about"
    )

    assert result["status"] == "unsafe"


def test_proxy_runtime_rejects_rfc2544_ip_literal_even_with_fake_dns_answer():
    class NoNavigationPage:
        def __getattr__(self, name):
            raise AssertionError(f"unsafe target must not use page.{name}")

    runtime = backfill._BrowserProbeRuntime(
        credentials={},
        proxy_loader=_proxy,
        headless=True,
        resolver=lambda _host: ["198.18.0.9"],
    )
    _activate_proxy_runtime(runtime)

    result = runtime._target_check(NoNavigationPage(), "https://198.18.0.9/shop")

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
    class NoNavigationPage:
        def __getattr__(self, name):
            raise AssertionError(f"unsafe target must not use page.{name}")

    runtime = backfill._BrowserProbeRuntime(
        credentials={},
        proxy_loader=_proxy,
        headless=True,
        resolver=lambda _host: ["198.18.0.9"],
    )
    _activate_proxy_runtime(runtime)

    assert runtime._target_check(NoNavigationPage(), url)["status"] == "unsafe"


def test_proxy_runtime_rejects_mixed_synthetic_and_private_dns_answers():
    class NoNavigationPage:
        def __getattr__(self, name):
            raise AssertionError(f"unsafe target must not use page.{name}")

    runtime = backfill._BrowserProbeRuntime(
        credentials={},
        proxy_loader=_proxy,
        headless=True,
        resolver=lambda _host: ["198.18.0.9", "10.0.0.9"],
    )
    _activate_proxy_runtime(runtime)

    assert runtime._target_check(
        NoNavigationPage(), "https://creator.example/about"
    )["status"] == "unsafe"


def test_social_contact_target_is_successful_noncommerce_without_navigation():
    class NoNavigationPage:
        def __getattr__(self, name):
            raise AssertionError(f"social target must not use page.{name}")

    runtime = backfill._BrowserProbeRuntime(
        credentials={},
        proxy_loader=_proxy,
        headless=True,
        resolver=lambda _host: ["8.8.8.8"],
    )
    result = runtime._target_check(
        NoNavigationPage(), "https://instagram.com/shop/not-a-storefront"
    )

    assert result["status"] == "succeeded"
    assert result["commerce_links"] == []
    assert result["note"] == "recognized_noncommerce_social"
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


class _OrdinaryPage:
    def __init__(self, response, samples):
        self.url = "https://example.com/about"
        self._response = response
        self._samples = iter(samples)

    def route(self, *_args):
        return None

    def unroute(self, *_args):
        return None

    def goto(self, *_args, **_kwargs):
        return self._response

    def wait_for_timeout(self, _milliseconds):
        return None

    def evaluate(self, _script):
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
        self._guard = None
        self.navigation_route = _NavigationRoute()

    def route(self, _pattern, guard):
        self._guard = guard

    def goto(self, *_args, **_kwargs):
        assert self._guard is not None
        self._guard(
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
        self._guard = None
        self.resource_route = _NavigationRoute()

    def route(self, _pattern, guard):
        self._guard = guard

    def goto(self, *_args, **_kwargs):
        assert self._guard is not None
        self._guard(
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
    runtime = backfill._BrowserProbeRuntime(
        credentials={},
        proxy_loader=_proxy,
        headless=True,
        resolver=lambda _host: ["8.8.8.8"],
    )
    result = runtime._target_check(_OrdinaryPage(response, samples), "https://example.com/about")

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
    _activate_proxy_runtime(runtime)
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
    _activate_proxy_runtime(runtime)

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
    _activate_proxy_runtime(runtime)

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
            self.popup.context = self.context
            self.context.pages.append(self.popup)
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
    assert page.popup.closed is True
    assert context.pages == [page]
    assert runtime._active_blocked_requests is None
    assert runtime._context_guard_installed is True


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
    assert chromium.context.websocket_pattern == "**/*"

    runtime._close_context()
    assert runtime._proxy_enforced is False

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
    _activate_proxy_runtime(runtime)

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
