"""Multi-process token usage and cost ledger.

The ledger is deliberately independent from any LLM SDK.  Provider adapters
write the usage returned by the provider and a small background monitor reads
the same SQLite database.  Prompts, responses, API keys and cookies are never
stored here.
"""
from __future__ import annotations

import fnmatch
import json
import os
import socket
import sqlite3
import tomllib
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[3]
DEFAULT_POLICY_PATH = ROOT / "config" / "token_cost_policy.toml"
DEFAULT_DB_PATH = ROOT / "data" / "token_costs.db"
SCHEMA_VERSION = 1
NANO_USD_PER_USD = 1_000_000_000


SCHEMA = """
CREATE TABLE IF NOT EXISTS token_processes (
  process_id TEXT PRIMARY KEY,
  pid INTEGER NOT NULL,
  ppid INTEGER,
  hostname TEXT NOT NULL,
  process_name TEXT NOT NULL,
  batch_id TEXT,
  run_id TEXT,
  stage TEXT,
  started_at TEXT NOT NULL,
  heartbeat_at TEXT NOT NULL,
  finished_at TEXT,
  status TEXT NOT NULL,
  exit_code INTEGER,
  metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_token_process_status
  ON token_processes(status, heartbeat_at);

CREATE TABLE IF NOT EXISTS token_usage_events (
  event_id TEXT PRIMARY KEY,
  request_id TEXT,
  process_id TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  provider TEXT NOT NULL,
  model TEXT NOT NULL,
  feature TEXT NOT NULL,
  stage TEXT,
  batch_id TEXT,
  run_id TEXT,
  status TEXT NOT NULL,
  input_tokens INTEGER NOT NULL,
  output_tokens INTEGER NOT NULL,
  cache_read_input_tokens INTEGER NOT NULL,
  cache_write_input_tokens INTEGER NOT NULL,
  total_tokens INTEGER NOT NULL,
  cost_nano_usd INTEGER,
  pricing_key TEXT,
  pricing_status TEXT NOT NULL,
  latency_ms INTEGER,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  FOREIGN KEY(process_id) REFERENCES token_processes(process_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_token_usage_request
  ON token_usage_events(provider, request_id)
  WHERE request_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_token_usage_time
  ON token_usage_events(occurred_at);
CREATE INDEX IF NOT EXISTS idx_token_usage_dimensions
  ON token_usage_events(batch_id, run_id, feature, provider, model);
"""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None = None) -> str:
    return (value or _utc_now()).astimezone(timezone.utc).isoformat(timespec="seconds")


def _safe_text(value: Any, *, limit: int = 160) -> str:
    return str(value or "").strip()[:limit]


def _nonnegative_int(value: Any) -> int:
    try:
        result = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, result)


def _json(value: dict | None) -> str:
    """Serialize bounded operational metadata, never provider payloads."""
    safe = {}
    for key, item in (value or {}).items():
        name = _safe_text(key, limit=64)
        if not name or any(secret in name.lower() for secret in (
            "key", "secret", "token", "cookie", "prompt", "response", "content",
        )):
            continue
        if isinstance(item, (str, int, float, bool)) or item is None:
            safe[name] = _safe_text(item, limit=240) if isinstance(item, str) else item
    return json.dumps(safe, ensure_ascii=False, separators=(",", ":"))


def usd_to_nano(value: Any) -> int:
    try:
        return max(0, round(float(value) * NANO_USD_PER_USD))
    except (TypeError, ValueError):
        return 0


def nano_to_usd(value: Any) -> float | None:
    if value is None:
        return None
    return round(int(value) / NANO_USD_PER_USD, 9)


@dataclass(frozen=True)
class Price:
    key: str
    input_nano_per_token: int
    output_nano_per_token: int
    cache_read_nano_per_token: int
    cache_write_nano_per_token: int
    status: str = "priced"

    def cost(
        self,
        *,
        input_tokens: int,
        output_tokens: int,
        cache_read_input_tokens: int,
        cache_write_input_tokens: int,
    ) -> int:
        return (
            input_tokens * self.input_nano_per_token
            + output_tokens * self.output_nano_per_token
            + cache_read_input_tokens * self.cache_read_nano_per_token
            + cache_write_input_tokens * self.cache_write_nano_per_token
        )


def load_policy(path: str | Path | None = None) -> dict:
    policy_path = Path(path or os.environ.get("TOKEN_COST_POLICY") or DEFAULT_POLICY_PATH)
    with policy_path.open("rb") as handle:
        policy = tomllib.load(handle)
    policy["_path"] = str(policy_path.resolve())
    return policy


def db_path_from_policy(policy: dict) -> Path:
    configured = os.environ.get("TOKEN_COST_DB") or (policy.get("storage") or {}).get("database")
    if not configured:
        return DEFAULT_DB_PATH
    path = Path(str(configured))
    return path if path.is_absolute() else ROOT / path


def resolve_price(policy: dict, provider: str, model: str) -> Price | None:
    lookup = f"{provider}/{model}"
    models = (policy.get("pricing") or {}).get("models") or {}
    matches = []
    for pattern, raw in models.items():
        if fnmatch.fnmatchcase(lookup, pattern):
            literal_length = len(pattern.replace("*", "").replace("?", ""))
            matches.append((literal_length, pattern, raw))
    if not matches:
        return None
    _length, key, raw = max(matches, key=lambda row: row[0])
    per_million_to_nano_per_token = lambda value: round(float(value or 0) * 1000)
    return Price(
        key=key,
        input_nano_per_token=per_million_to_nano_per_token(
            raw.get("input_usd_per_million")
        ),
        output_nano_per_token=per_million_to_nano_per_token(
            raw.get("output_usd_per_million")
        ),
        cache_read_nano_per_token=per_million_to_nano_per_token(
            raw.get("cache_read_usd_per_million", raw.get("input_usd_per_million"))
        ),
        cache_write_nano_per_token=per_million_to_nano_per_token(
            raw.get("cache_write_usd_per_million", raw.get("input_usd_per_million"))
        ),
        status="local_zero" if raw.get("local_zero_cost") else "priced",
    )


class TokenCostLedger:
    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        policy: dict | None = None,
        policy_path: str | Path | None = None,
    ) -> None:
        self.policy = policy or load_policy(policy_path)
        self.db_path = Path(db_path) if db_path else db_path_from_policy(self.policy)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=15000")
        return connection

    def _initialize(self) -> None:
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(SCHEMA)
            connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def start_process(
        self,
        *,
        process_id: str | None = None,
        pid: int | None = None,
        ppid: int | None = None,
        hostname: str | None = None,
        process_name: str = "llm-worker",
        batch_id: str | None = None,
        run_id: str | None = None,
        stage: str | None = None,
        metadata: dict | None = None,
    ) -> str:
        process_id = _safe_text(process_id or uuid.uuid4().hex, limit=80)
        now = _iso()
        values = (
            process_id,
            pid or os.getpid(),
            ppid if ppid is not None else os.getppid(),
            _safe_text(hostname or socket.gethostname(), limit=120),
            _safe_text(process_name, limit=120) or "llm-worker",
            _safe_text(batch_id) or None,
            _safe_text(run_id) or None,
            _safe_text(stage) or None,
            now,
            now,
            "running",
            _json(metadata),
        )
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO token_processes(
                  process_id,pid,ppid,hostname,process_name,batch_id,run_id,stage,
                  started_at,heartbeat_at,status,metadata_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(process_id) DO UPDATE SET
                  pid=excluded.pid, ppid=excluded.ppid, hostname=excluded.hostname,
                  process_name=excluded.process_name,
                  batch_id=COALESCE(excluded.batch_id, token_processes.batch_id),
                  run_id=COALESCE(excluded.run_id, token_processes.run_id),
                  stage=COALESCE(excluded.stage, token_processes.stage),
                  heartbeat_at=excluded.heartbeat_at, status='running',
                  finished_at=NULL, exit_code=NULL
                """,
                values,
            )
        return process_id

    def ensure_process(self, process_id: str | None = None, **context: Any) -> str:
        process_id = _safe_text(
            process_id or os.environ.get("TOKEN_COST_PROCESS_ID")
            or f"{socket.gethostname()}-{os.getpid()}",
            limit=80,
        )
        with self.connect() as connection:
            row = connection.execute(
                "SELECT process_id FROM token_processes WHERE process_id=?", (process_id,)
            ).fetchone()
        if row:
            self.heartbeat(process_id)
            return process_id
        return self.start_process(
            process_id=process_id,
            process_name=context.get("process_name") or "embedded-llm-worker",
            batch_id=context.get("batch_id") or os.environ.get("TOKEN_COST_BATCH_ID"),
            run_id=context.get("run_id") or os.environ.get("TOKEN_COST_RUN_ID"),
            stage=context.get("stage") or os.environ.get("TOKEN_COST_STAGE"),
        )

    def heartbeat(self, process_id: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """UPDATE token_processes
                   SET heartbeat_at=?, status='running'
                   WHERE process_id=? AND finished_at IS NULL""",
                (_iso(), process_id),
            )

    def finish_process(self, process_id: str, *, exit_code: int = 0) -> None:
        now = _iso()
        with self.connect() as connection:
            connection.execute(
                """UPDATE token_processes
                   SET heartbeat_at=?, finished_at=?, status=?, exit_code=?
                   WHERE process_id=?""",
                (now, now, "succeeded" if exit_code == 0 else "failed", exit_code, process_id),
            )

    def record_usage(
        self,
        *,
        provider: str,
        model: str,
        feature: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_input_tokens: int = 0,
        cache_write_input_tokens: int = 0,
        process_id: str | None = None,
        request_id: str | None = None,
        event_id: str | None = None,
        occurred_at: str | None = None,
        status: str = "success",
        stage: str | None = None,
        batch_id: str | None = None,
        run_id: str | None = None,
        latency_ms: int | None = None,
        metadata: dict | None = None,
    ) -> str:
        provider = _safe_text(provider, limit=64).lower() or "unknown"
        model = _safe_text(model, limit=160) or "unknown"
        feature = _safe_text(feature, limit=120) or "unknown"
        context = {
            "batch_id": batch_id,
            "run_id": run_id,
            "stage": stage,
        }
        process_id = self.ensure_process(process_id, **context)
        counts = {
            "input": _nonnegative_int(input_tokens),
            "output": _nonnegative_int(output_tokens),
            "cache_read": _nonnegative_int(cache_read_input_tokens),
            "cache_write": _nonnegative_int(cache_write_input_tokens),
        }
        price = resolve_price(self.policy, provider, model)
        cost = None
        pricing_key = None
        pricing_status = "unknown_price"
        if price is not None:
            pricing_key = price.key
            pricing_status = price.status
            cost = price.cost(
                input_tokens=counts["input"],
                output_tokens=counts["output"],
                cache_read_input_tokens=counts["cache_read"],
                cache_write_input_tokens=counts["cache_write"],
            )
        event_id = _safe_text(event_id or uuid.uuid4().hex, limit=80)
        with self.connect() as connection:
            process = connection.execute(
                "SELECT batch_id,run_id,stage FROM token_processes WHERE process_id=?",
                (process_id,),
            ).fetchone()
            effective_batch = _safe_text(batch_id) or (process["batch_id"] if process else None)
            effective_run = _safe_text(run_id) or (process["run_id"] if process else None)
            effective_stage = _safe_text(stage) or (process["stage"] if process else None)
            connection.execute(
                """
                INSERT OR IGNORE INTO token_usage_events(
                  event_id,request_id,process_id,occurred_at,provider,model,feature,
                  stage,batch_id,run_id,status,input_tokens,output_tokens,
                  cache_read_input_tokens,cache_write_input_tokens,total_tokens,
                  cost_nano_usd,pricing_key,pricing_status,latency_ms,metadata_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    event_id, _safe_text(request_id, limit=160) or None, process_id,
                    occurred_at or _iso(), provider, model, feature,
                    effective_stage, effective_batch, effective_run,
                    _safe_text(status, limit=32) or "success",
                    counts["input"], counts["output"], counts["cache_read"],
                    counts["cache_write"], sum(counts.values()), cost, pricing_key,
                    pricing_status, _nonnegative_int(latency_ms) if latency_ms is not None else None,
                    _json(metadata),
                ),
            )
        return event_id

    def _period_start(self, period: str, now: datetime | None = None) -> datetime:
        name = str((self.policy.get("monitor") or {}).get("timezone") or "UTC")
        zone = ZoneInfo(name)
        local = (now or _utc_now()).astimezone(zone)
        if period == "month":
            start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        else:
            start = local.replace(hour=0, minute=0, second=0, microsecond=0)
        return start.astimezone(timezone.utc)

    def summary(
        self,
        *,
        since: str | None = None,
        period: str = "day",
        batch_id: str | None = None,
        run_id: str | None = None,
        process_id: str | None = None,
    ) -> dict:
        since = since or _iso(self._period_start(period))
        clauses = ["occurred_at>=?"]
        params: list[Any] = [since]
        for column, value in (
            ("batch_id", batch_id), ("run_id", run_id), ("process_id", process_id)
        ):
            if value:
                clauses.append(f"{column}=?")
                params.append(value)
        where = " AND ".join(clauses)
        with self.connect() as connection:
            total = connection.execute(
                f"""
                SELECT COUNT(*) AS requests,
                       COALESCE(SUM(input_tokens),0) AS input_tokens,
                       COALESCE(SUM(output_tokens),0) AS output_tokens,
                       COALESCE(SUM(cache_read_input_tokens),0) AS cache_read_input_tokens,
                       COALESCE(SUM(cache_write_input_tokens),0) AS cache_write_input_tokens,
                       COALESCE(SUM(total_tokens),0) AS total_tokens,
                       COALESCE(SUM(cost_nano_usd),0) AS known_cost_nano_usd,
                       COALESCE(SUM(CASE WHEN cost_nano_usd IS NULL THEN 1 ELSE 0 END),0) AS unknown_price_requests,
                       COALESCE(SUM(CASE WHEN status!='success' THEN 1 ELSE 0 END),0) AS failed_requests
                FROM token_usage_events WHERE {where}
                """,
                params,
            ).fetchone()
            groups = connection.execute(
                f"""
                SELECT provider,model,feature,COUNT(*) AS requests,
                       SUM(total_tokens) AS total_tokens,
                       COALESCE(SUM(cost_nano_usd),0) AS cost_nano_usd,
                       SUM(CASE WHEN cost_nano_usd IS NULL THEN 1 ELSE 0 END) AS unknown_price_requests
                FROM token_usage_events WHERE {where}
                GROUP BY provider,model,feature
                ORDER BY cost_nano_usd DESC,total_tokens DESC
                """,
                params,
            ).fetchall()
        result = dict(total)
        result["known_cost_usd"] = nano_to_usd(result.pop("known_cost_nano_usd"))
        result["since"] = since
        result["period"] = period
        result["groups"] = [
            {
                **{key: row[key] for key in (
                    "provider", "model", "feature", "requests", "total_tokens",
                    "unknown_price_requests",
                )},
                "cost_usd": nano_to_usd(row["cost_nano_usd"]),
            }
            for row in groups
        ]
        return result

    def budget_status(self, *, now: datetime | None = None) -> dict:
        budgets = self.policy.get("budgets") or {}
        states = {}
        overall = "ok"
        for period in ("day", "month"):
            summary = self.summary(period=period, since=_iso(self._period_start(period, now)))
            spent_nano = usd_to_nano(summary["known_cost_usd"])
            warning = usd_to_nano(budgets.get(f"{period}_warning_usd"))
            hard = usd_to_nano(budgets.get(f"{period}_hard_limit_usd"))
            status = "ok"
            if hard and spent_nano >= hard:
                status = "blocked"
                overall = "blocked"
            elif warning and spent_nano >= warning:
                status = "warning"
                if overall == "ok":
                    overall = "warning"
            states[period] = {
                "status": status,
                "spent_usd": nano_to_usd(spent_nano),
                "warning_usd": nano_to_usd(warning) if warning else None,
                "hard_limit_usd": nano_to_usd(hard) if hard else None,
                "usage_ratio": round(spent_nano / hard, 4) if hard else None,
            }
        return {"status": overall, "periods": states}

    def list_processes(self, *, include_finished: bool = False, limit: int = 100) -> list[dict]:
        where = "" if include_finished else "WHERE finished_at IS NULL"
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT p.*,
                       COALESCE(SUM(e.total_tokens),0) AS total_tokens,
                       COALESCE(SUM(e.cost_nano_usd),0) AS cost_nano_usd
                FROM token_processes p
                LEFT JOIN token_usage_events e ON e.process_id=p.process_id
                {where}
                GROUP BY p.process_id
                ORDER BY p.started_at DESC LIMIT ?
                """,
                (max(1, min(int(limit), 1000)),),
            ).fetchall()
        return [
            {
                **{key: row[key] for key in (
                    "process_id", "pid", "hostname", "process_name", "batch_id",
                    "run_id", "stage", "started_at", "heartbeat_at", "finished_at",
                    "status", "exit_code", "total_tokens",
                )},
                "cost_usd": nano_to_usd(row["cost_nano_usd"]),
            }
            for row in rows
        ]

    def reconcile_processes(self, *, stale_seconds: int | None = None) -> dict:
        monitor = self.policy.get("monitor") or {}
        stale_seconds = max(5, int(stale_seconds or monitor.get("stale_after_seconds") or 60))
        cutoff = _iso(_utc_now() - timedelta(seconds=stale_seconds))
        hostname = socket.gethostname()
        marked_exited = 0
        marked_stale = 0
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT process_id,pid,hostname,heartbeat_at FROM token_processes "
                "WHERE finished_at IS NULL"
            ).fetchall()
            for row in rows:
                alive = True
                if row["hostname"] == hostname:
                    try:
                        os.kill(int(row["pid"]), 0)
                    except (ProcessLookupError, ValueError):
                        alive = False
                    except PermissionError:
                        alive = True
                if not alive:
                    now = _iso()
                    connection.execute(
                        """UPDATE token_processes SET status='exited',finished_at=?,
                           heartbeat_at=?,exit_code=-1 WHERE process_id=?""",
                        (now, now, row["process_id"]),
                    )
                    marked_exited += 1
                elif row["heartbeat_at"] < cutoff:
                    connection.execute(
                        "UPDATE token_processes SET status='stale' WHERE process_id=?",
                        (row["process_id"],),
                    )
                    marked_stale += 1
        return {"marked_exited": marked_exited, "marked_stale": marked_stale}

    def process_status_counts(self, *, period: str = "day") -> dict:
        since = _iso(self._period_start(period))
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT status,COUNT(*) AS count FROM token_processes
                   WHERE started_at>=? GROUP BY status""",
                (since,),
            ).fetchall()
        counts = {status: 0 for status in (
            "running", "stale", "succeeded", "failed", "exited"
        )}
        counts.update({row["status"]: row["count"] for row in rows})
        return counts

    def snapshot(self) -> dict:
        self.reconcile_processes()
        processes = self.list_processes()
        return {
            "schema_version": SCHEMA_VERSION,
            "generated_at": _iso(),
            "database": str(self.db_path.resolve()),
            "summary": self.summary(period="day"),
            "budgets": self.budget_status(),
            "processes": processes,
            "process_counts": self.process_status_counts(period="day"),
        }


def record_provider_usage(
    usage: dict,
    *,
    provider: str,
    model: str,
    feature: str,
    context: dict | None = None,
    request_id: str | None = None,
    latency_ms: int | None = None,
    policy_path: str | Path | None = None,
    db_path: str | Path | None = None,
) -> str:
    """Convenience adapter used by provider transports."""
    context = context or {}
    ledger = TokenCostLedger(db_path, policy_path=policy_path)
    return ledger.record_usage(
        provider=provider,
        model=model,
        feature=feature,
        input_tokens=usage.get("input_tokens") or usage.get("prompt_eval_count"),
        output_tokens=usage.get("output_tokens") or usage.get("eval_count"),
        cache_read_input_tokens=usage.get("cache_read_input_tokens"),
        cache_write_input_tokens=(
            usage.get("cache_creation_input_tokens")
            or usage.get("cache_write_input_tokens")
        ),
        process_id=context.get("process_id"),
        request_id=request_id,
        status=context.get("status") or "success",
        stage=context.get("stage"),
        batch_id=context.get("batch_id"),
        run_id=context.get("run_id"),
        latency_ms=latency_ms,
        metadata={"attempt": context.get("attempt")},
    )
