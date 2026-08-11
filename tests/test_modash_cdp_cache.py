"""Modash raw-report cache is identity-bound, atomic and credit-safe."""
from __future__ import annotations

import json
from pathlib import Path

import playwright.sync_api
import pytest

from extensions.sop_v2.pipeline import modash_cdp


def _report(handle: str) -> dict:
    return {
        "profile": {
            "profileData": {
                "profile": {"username": handle, "engagementRate": 0.031}
            }
        }
    }


class _Browser:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class _Playwright:
    def __init__(self, browser, connects):
        self.chromium = self
        self._browser = browser
        self._connects = connects

    def connect_over_cdp(self, url):
        self._connects.append(url)
        return self._browser


class _PlaywrightContext:
    def __init__(self, runtime):
        self._runtime = runtime

    def __enter__(self):
        return self._runtime

    def __exit__(self, *_args):
        return False


def _install_fake_cdp(monkeypatch, *, fetched):
    browser = _Browser()
    connects = []
    runtime = _Playwright(browser, connects)
    monkeypatch.setattr(
        playwright.sync_api,
        "sync_playwright",
        lambda: _PlaywrightContext(runtime),
    )
    monkeypatch.setattr(modash_cdp, "_find_modash", lambda _browser: object())
    monkeypatch.setattr(
        modash_cdp,
        "resolve_platform_ids",
        lambda _page, handles, *_args, **_kwargs: {
            handle.lower(): f"spid-{handle}" for handle in handles
        },
    )
    monkeypatch.setattr(
        modash_cdp,
        "fetch_report",
        lambda _page, _spid: fetched,
    )
    return browser, connects


def test_valid_cache_hit_never_connects_to_cdp(tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    cache_path = modash_cdp._cache_path(cache_dir, "cached_creator")  # noqa: SLF001
    cache_path.write_text(
        json.dumps(_report("cached_creator")), encoding="utf-8"
    )
    monkeypatch.setattr(
        playwright.sync_api,
        "sync_playwright",
        lambda: (_ for _ in ()).throw(
            AssertionError("valid cache hit must not initialize CDP")
        ),
    )
    candidate = {"handle": "cached_creator"}

    result = modash_cdp.enrich_via_cdp(
        [candidate], "", {}, cache_dir=str(cache_dir)
    )

    assert result == {"matched": 1, "total": 1, "from_cache": 1}
    assert candidate["modash_report"] is True


def test_bad_cache_is_atomically_replaced_after_identity_validation(
    tmp_path, monkeypatch
):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    cache_path = modash_cdp._cache_path(cache_dir, "target")  # noqa: SLF001
    cache_path.write_text('["malformed-shape"]', encoding="utf-8")
    browser, connects = _install_fake_cdp(
        monkeypatch, fetched=_report("target")
    )
    replacements = []
    fsync_calls = []
    real_replace = modash_cdp.os.replace
    real_fsync = modash_cdp.os.fsync

    def observed_replace(source, destination):
        replacements.append((Path(source), Path(destination)))
        return real_replace(source, destination)

    def observed_fsync(fd):
        fsync_calls.append(fd)
        return real_fsync(fd)

    monkeypatch.setattr(modash_cdp.os, "replace", observed_replace)
    monkeypatch.setattr(modash_cdp.os, "fsync", observed_fsync)
    candidate = {"handle": "target"}

    result = modash_cdp.enrich_via_cdp(
        [candidate], "", {}, cache_dir=str(cache_dir)
    )

    assert result == {"matched": 1, "total": 1, "from_cache": 0}
    assert candidate["modash_report"] is True
    assert connects == ["http://127.0.0.1:9222"]
    assert browser.closed is True
    assert len(replacements) == 1
    source, destination = replacements[0]
    assert source.parent == destination.parent == cache_dir
    assert destination == cache_path
    assert len(fsync_calls) >= 2
    assert json.loads(cache_path.read_text(encoding="utf-8")) == _report(
        "target"
    )
    assert not list(cache_dir.glob(f".{cache_path.name}.*.tmp"))


def test_wrong_identity_report_never_overwrites_raw_cache(
    tmp_path, monkeypatch
):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    cache_path = modash_cdp._cache_path(cache_dir, "target")  # noqa: SLF001
    original = json.dumps(_report("wrong_cached_identity")).encode("utf-8")
    cache_path.write_bytes(original)
    browser, connects = _install_fake_cdp(
        monkeypatch, fetched=_report("wrong_fetched_identity")
    )
    candidate = {"handle": "target"}

    result = modash_cdp.enrich_via_cdp(
        [candidate], "", {}, cache_dir=str(cache_dir)
    )

    assert result == {"matched": 0, "total": 1, "from_cache": 0}
    assert "modash_report" not in candidate
    assert cache_path.read_bytes() == original
    assert connects == ["http://127.0.0.1:9222"]
    assert browser.closed is True


def test_cache_write_failure_is_formal_failure_before_candidate_mutation(
    tmp_path, monkeypatch
):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    browser, connects = _install_fake_cdp(
        monkeypatch, fetched=_report("target")
    )
    monkeypatch.setattr(
        modash_cdp,
        "_atomic_write_cache",
        lambda *_args: (_ for _ in ()).throw(OSError("disk unavailable")),
    )
    candidate = {"handle": "target"}

    with pytest.raises(OSError, match="disk unavailable"):
        modash_cdp.enrich_via_cdp(
            [candidate], "", {}, cache_dir=str(cache_dir)
        )

    assert "modash_report" not in candidate
    assert connects == ["http://127.0.0.1:9222"]
    assert browser.closed is True
