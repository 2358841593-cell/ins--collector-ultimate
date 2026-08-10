"""Token cost ledger, pricing and process-monitor tests."""
from __future__ import annotations

import json
import socket
from datetime import datetime, timezone

from extensions.sop_v2 import comment_translation
from extensions.sop_v2.token_cost import TokenCostLedger


def _policy(*, day_warning=1.0, day_hard=0) -> dict:
    return {
        "monitor": {"timezone": "Asia/Shanghai", "stale_after_seconds": 5},
        "budgets": {
            "day_warning_usd": day_warning,
            "day_hard_limit_usd": day_hard,
            "month_warning_usd": 10,
            "month_hard_limit_usd": 0,
        },
        "pricing": {
            "models": {
                "anthropic/claude-haiku-4-5-20251001": {
                    "input_usd_per_million": 1.0,
                    "output_usd_per_million": 5.0,
                    "cache_read_usd_per_million": 0.1,
                    "cache_write_usd_per_million": 1.25,
                },
                "ollama/*": {
                    "input_usd_per_million": 0,
                    "output_usd_per_million": 0,
                    "local_zero_cost": True,
                },
            }
        },
    }


def test_provider_usage_is_priced_with_an_immutable_price_snapshot(tmp_path):
    ledger = TokenCostLedger(tmp_path / "cost.db", policy=_policy())
    process_id = ledger.start_process(
        process_id="worker-1", process_name="translate", batch_id="B-1", stage="stage3"
    )
    ledger.record_usage(
        process_id=process_id,
        request_id="req-1",
        provider="anthropic",
        model="claude-haiku-4-5-20251001",
        feature="comment_translation",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_read_input_tokens=100_000,
        cache_write_input_tokens=100_000,
    )

    summary = ledger.summary(since="2000-01-01T00:00:00+00:00", batch_id="B-1")
    assert summary["requests"] == 1
    assert summary["total_tokens"] == 2_200_000
    assert summary["known_cost_usd"] == 6.135
    assert summary["unknown_price_requests"] == 0
    assert summary["groups"][0]["feature"] == "comment_translation"

    with ledger.connect() as connection:
        event = connection.execute(
            "SELECT pricing_key,cost_nano_usd FROM token_usage_events"
        ).fetchone()
    assert event["pricing_key"] == "anthropic/claude-haiku-4-5-20251001"
    assert event["cost_nano_usd"] == 6_135_000_000


def test_request_id_is_idempotent_across_retries(tmp_path):
    ledger = TokenCostLedger(tmp_path / "cost.db", policy=_policy())
    for _ in range(2):
        ledger.record_usage(
            provider="anthropic",
            model="claude-haiku-4-5-20251001",
            feature="comment_translation",
            request_id="same-provider-request",
            input_tokens=100,
            output_tokens=20,
        )

    summary = ledger.summary(since="2000-01-01T00:00:00+00:00")
    assert summary["requests"] == 1
    assert summary["total_tokens"] == 120


def test_unknown_models_are_visible_and_never_mispriced_as_free(tmp_path):
    ledger = TokenCostLedger(tmp_path / "cost.db", policy=_policy())
    ledger.record_usage(
        provider="new-provider",
        model="unpriced-model",
        feature="future_feature",
        input_tokens=100,
        output_tokens=50,
    )

    summary = ledger.summary(since="2000-01-01T00:00:00+00:00")
    assert summary["requests"] == 1
    assert summary["known_cost_usd"] == 0
    assert summary["unknown_price_requests"] == 1
    assert summary["groups"][0]["unknown_price_requests"] == 1


def test_hard_budget_changes_health_to_blocked(tmp_path):
    ledger = TokenCostLedger(
        tmp_path / "cost.db", policy=_policy(day_warning=1, day_hard=5)
    )
    ledger.record_usage(
        provider="anthropic",
        model="claude-haiku-4-5-20251001",
        feature="comment_translation",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
    )

    status = ledger.budget_status(now=datetime.now(timezone.utc))
    assert status["status"] == "blocked"
    assert status["periods"]["day"]["spent_usd"] == 6.0
    assert status["periods"]["day"]["usage_ratio"] == 1.2


def test_dead_and_stale_workers_are_reconciled(tmp_path):
    ledger = TokenCostLedger(tmp_path / "cost.db", policy=_policy())
    ledger.start_process(
        process_id="dead-worker",
        pid=999_999_999,
        hostname=socket.gethostname(),
        process_name="worker",
    )
    with ledger.connect() as connection:
        connection.execute(
            "UPDATE token_processes SET heartbeat_at=? WHERE process_id=?",
            ("2000-01-01T00:00:00+00:00", "dead-worker"),
        )

    result = ledger.reconcile_processes(stale_seconds=5)
    processes = ledger.list_processes(include_finished=True)
    assert result["marked_exited"] == 1
    assert processes[0]["status"] == "exited"
    assert processes[0]["exit_code"] == -1


def test_sensitive_metadata_is_dropped_from_ledger(tmp_path):
    ledger = TokenCostLedger(tmp_path / "cost.db", policy=_policy())
    ledger.record_usage(
        provider="ollama",
        model="qwen3.5:4b",
        feature="comment_translation",
        input_tokens=12,
        output_tokens=4,
        metadata={
            "attempt": 1,
            "api_key": "must-not-persist",
            "prompt": "must-not-persist",
        },
    )
    with ledger.connect() as connection:
        raw = connection.execute(
            "SELECT metadata_json FROM token_usage_events"
        ).fetchone()[0]
    assert json.loads(raw) == {"attempt": 1}


def test_anthropic_translation_records_provider_reported_usage(tmp_path, monkeypatch):
    db_path = tmp_path / "cost.db"
    monkeypatch.setenv("TOKEN_COST_DB", str(db_path))

    def provider(items, **_kwargs):
        return {
            "_model_output": {
                "translations": [{
                    "id": items[0]["id"],
                    "source_language": "fr",
                    "translated_text": "在哪里买？",
                }]
            },
            "_usage": {"input_tokens": 100, "output_tokens": 20},
            "_request_id": "anthropic-request-1",
            "_latency_ms": 25,
            "_attempt": 1,
        }

    monkeypatch.setattr(comment_translation, "_anthropic_transport", provider)
    candidate = {"comments_analyzed": 1, "comment_sample": ["Où acheter ?"]}
    result = comment_translation.translate_candidate(
        candidate,
        provider="anthropic",
        model="claude-haiku-4-5-20251001",
        api_key="test-only",
        usage_context={"batch_id": "B-2", "stage": "stage4_decide"},
    )

    assert result["usage_events_recorded"] == 1
    assert result["usage_events_failed"] == 0
    ledger = TokenCostLedger(db_path)
    summary = ledger.summary(since="2000-01-01T00:00:00+00:00", batch_id="B-2")
    assert summary["requests"] == 1
    assert summary["total_tokens"] == 120
    assert summary["known_cost_usd"] == 0.0002
