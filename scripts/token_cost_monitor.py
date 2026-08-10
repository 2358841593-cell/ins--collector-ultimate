#!/usr/bin/env python3
"""Background process and token-cost monitor.

Examples:
  python scripts/token_cost_monitor.py serve
  python scripts/token_cost_monitor.py status
  python scripts/token_cost_monitor.py run --batch-id BATCH-1 --stage stage3 -- \
      python -m extensions.sop_v2.pipeline.stage3_collect ...
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from extensions.sop_v2.token_cost import (  # noqa: E402
    TokenCostLedger,
    db_path_from_policy,
    load_policy,
)


def _status_path(policy: dict) -> Path:
    configured = (policy.get("storage") or {}).get("status_file") or "data/token_cost_status.json"
    path = Path(str(configured))
    return path if path.is_absolute() else ROOT / path


def _write_snapshot(path: Path, snapshot: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(path)


def _prometheus(snapshot: dict) -> str:
    summary = snapshot["summary"]
    budgets = snapshot["budgets"]
    lines = [
        "# HELP ins_collector_token_requests_today LLM provider calls today.",
        "# TYPE ins_collector_token_requests_today gauge",
        f"ins_collector_token_requests_today {summary['requests']}",
        "# HELP ins_collector_tokens_today Provider-reported tokens today.",
        "# TYPE ins_collector_tokens_today gauge",
        f"ins_collector_tokens_today {summary['total_tokens']}",
        "# HELP ins_collector_token_cost_usd_today Known provider cost in USD today.",
        "# TYPE ins_collector_token_cost_usd_today gauge",
        f"ins_collector_token_cost_usd_today {summary['known_cost_usd']}",
        "# HELP ins_collector_token_unknown_price_requests_today Calls without a price rule.",
        "# TYPE ins_collector_token_unknown_price_requests_today gauge",
        f"ins_collector_token_unknown_price_requests_today {summary['unknown_price_requests']}",
        "# HELP ins_collector_token_budget_blocked Whether a configured hard budget is reached.",
        "# TYPE ins_collector_token_budget_blocked gauge",
        f"ins_collector_token_budget_blocked {1 if budgets['status'] == 'blocked' else 0}",
        "# HELP ins_collector_llm_processes Monitored LLM worker processes by status.",
        "# TYPE ins_collector_llm_processes gauge",
    ]
    for status, count in snapshot["process_counts"].items():
        lines.append(f'ins_collector_llm_processes{{status="{status}"}} {count}')
    return "\n".join(lines) + "\n"


def _handler(ledger: TokenCostLedger, status_path: Path):
    class Handler(BaseHTTPRequestHandler):
        server_version = "TokenCostMonitor/1"

        def _send_json(self, status: int, document: dict) -> None:
            body = json.dumps(document, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("content-type", "application/json; charset=utf-8")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/healthz":
                snapshot = ledger.snapshot()
                code = 503 if snapshot["budgets"]["status"] == "blocked" else 200
                self._send_json(code, {
                    "status": snapshot["budgets"]["status"],
                    "generated_at": snapshot["generated_at"],
                    "process_counts": snapshot["process_counts"],
                })
                return
            if parsed.path == "/v1/summary":
                query = urllib.parse.parse_qs(parsed.query)
                period = (query.get("period") or ["day"])[0]
                if period not in {"day", "month"}:
                    self._send_json(400, {"error": "period must be day or month"})
                    return
                self._send_json(200, {
                    "summary": ledger.summary(
                        period=period,
                        batch_id=(query.get("batch_id") or [None])[0],
                        run_id=(query.get("run_id") or [None])[0],
                        process_id=(query.get("process_id") or [None])[0],
                    ),
                    "budgets": ledger.budget_status(),
                })
                return
            if parsed.path == "/v1/processes":
                query = urllib.parse.parse_qs(parsed.query)
                include_finished = (query.get("include_finished") or ["0"])[0] in {
                    "1", "true", "yes",
                }
                self._send_json(200, {
                    "processes": ledger.list_processes(include_finished=include_finished)
                })
                return
            if parsed.path == "/metrics":
                body = _prometheus(ledger.snapshot()).encode("utf-8")
                self.send_response(200)
                self.send_header("content-type", "text/plain; version=0.0.4")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self._send_json(404, {"error": "not_found"})

        def log_message(self, format: str, *args) -> None:  # noqa: A002
            # Access logs intentionally omit query strings and never contain prompts.
            sys.stderr.write(
                f"token-monitor http {self.address_string()} {args[0] if args else ''}\n"
            )

    return Handler


class SnapshotLoop:
    def __init__(self, ledger: TokenCostLedger, path: Path, interval: float) -> None:
        self.ledger = ledger
        self.path = path
        self.interval = max(1.0, interval)
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, name="token-cost-snapshot", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.thread.join(timeout=self.interval + 2)

    def _run(self) -> None:
        while not self.stop_event.is_set():
            try:
                _write_snapshot(self.path, self.ledger.snapshot())
            except Exception as exc:  # noqa: BLE001 - monitor must survive transient DB/filesystem errors
                sys.stderr.write(f"token-monitor snapshot error: {type(exc).__name__}\n")
            self.stop_event.wait(self.interval)


def _ledger(args) -> tuple[dict, TokenCostLedger]:
    policy = load_policy(args.policy)
    db_path = Path(args.db) if args.db else db_path_from_policy(policy)
    return policy, TokenCostLedger(db_path, policy=policy)


def command_status(args) -> int:
    _policy, ledger = _ledger(args)
    snapshot = ledger.snapshot()
    if args.json:
        print(json.dumps(snapshot, ensure_ascii=False, indent=2))
    else:
        summary = snapshot["summary"]
        print(
            f"今日 {summary['requests']} 次 / {summary['total_tokens']} tokens / "
            f"${summary['known_cost_usd']:.6f}"
        )
        print(
            f"预算 {snapshot['budgets']['status']} · "
            f"运行 {snapshot['process_counts']['running']} · "
            f"失联 {snapshot['process_counts']['stale']} · "
            f"未知单价 {summary['unknown_price_requests']}"
        )
        for group in summary["groups"]:
            print(
                f"  {group['provider']}/{group['model']} {group['feature']}: "
                f"{group['requests']} 次, {group['total_tokens']} tokens, "
                f"${group['cost_usd']:.6f}"
            )
    return 2 if snapshot["budgets"]["status"] == "blocked" else 0


def command_snapshot(args) -> int:
    policy, ledger = _ledger(args)
    snapshot = ledger.snapshot()
    path = _status_path(policy)
    _write_snapshot(path, snapshot)
    print(path)
    return 2 if snapshot["budgets"]["status"] == "blocked" else 0


def command_serve(args) -> int:
    policy, ledger = _ledger(args)
    monitor = policy.get("monitor") or {}
    host = args.host or str(monitor.get("listen_host") or "127.0.0.1")
    if host not in {"127.0.0.1", "localhost", "::1"} and not args.allow_remote:
        print("拒绝监听非本机地址；确需暴露时显式传 --allow-remote 并在上层配置鉴权", file=sys.stderr)
        return 2
    port = args.port or int(monitor.get("listen_port") or 9469)
    interval = args.interval or float(monitor.get("poll_interval_seconds") or 5)
    status_path = _status_path(policy)
    loop = SnapshotLoop(ledger, status_path, interval)
    server = ThreadingHTTPServer((host, port), _handler(ledger, status_path))
    loop.start()
    print(
        f"token cost monitor: http://{host}:{port} · db={ledger.db_path} · "
        f"snapshot={status_path}",
        flush=True,
    )
    try:
        server.serve_forever(poll_interval=min(interval, 1.0))
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        loop.stop()
        _write_snapshot(status_path, ledger.snapshot())
    return 0


def command_run(args) -> int:
    if not args.command:
        print("-- 后必须提供待运行命令", file=sys.stderr)
        return 2
    command = list(args.command)
    if command[0] == "--":
        command = command[1:]
    if not command:
        print("-- 后必须提供待运行命令", file=sys.stderr)
        return 2
    _policy, ledger = _ledger(args)
    process_id = os.urandom(12).hex()
    environment = os.environ.copy()
    environment.update({
        "TOKEN_COST_PROCESS_ID": process_id,
        "TOKEN_COST_DB": str(ledger.db_path.resolve()),
    })
    for name, value in (
        ("TOKEN_COST_BATCH_ID", args.batch_id),
        ("TOKEN_COST_RUN_ID", args.run_id),
        ("TOKEN_COST_STAGE", args.stage),
    ):
        if value:
            environment[name] = value
    child = subprocess.Popen(command, env=environment)  # noqa: S603 - explicit operator command
    ledger.start_process(
        process_id=process_id,
        pid=child.pid,
        ppid=os.getpid(),
        process_name=Path(command[0]).name,
        batch_id=args.batch_id,
        run_id=args.run_id,
        stage=args.stage,
    )

    def forward(signum, _frame):
        if child.poll() is None:
            child.send_signal(signum)

    previous = {}
    for signum in (signal.SIGINT, signal.SIGTERM):
        previous[signum] = signal.signal(signum, forward)
    try:
        while True:
            try:
                exit_code = child.wait(timeout=max(1.0, args.heartbeat_seconds))
                break
            except subprocess.TimeoutExpired:
                ledger.heartbeat(process_id)
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    ledger.finish_process(process_id, exit_code=exit_code)
    return exit_code


def command_record(args) -> int:
    _policy, ledger = _ledger(args)
    event_id = ledger.record_usage(
        provider=args.provider,
        model=args.model,
        feature=args.feature,
        input_tokens=args.input_tokens,
        output_tokens=args.output_tokens,
        cache_read_input_tokens=args.cache_read_tokens,
        cache_write_input_tokens=args.cache_write_tokens,
        batch_id=args.batch_id,
        run_id=args.run_id,
        stage=args.stage,
        request_id=args.request_id,
    )
    print(event_id)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="LLM 进程、token 与成本监控")
    parser.add_argument("--policy", default=None, help="token_cost_policy.toml")
    parser.add_argument("--db", default=None, help="覆盖 SQLite 台账路径")
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    status = subparsers.add_parser("status", help="查看今日成本与进程状态")
    status.add_argument("--json", action="store_true")
    status.set_defaults(function=command_status)

    snapshot = subparsers.add_parser("snapshot", help="刷新一次状态快照")
    snapshot.set_defaults(function=command_snapshot)

    serve = subparsers.add_parser("serve", help="运行常驻监控与本机 HTTP 接口")
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)
    serve.add_argument("--interval", type=float, default=None)
    serve.add_argument("--allow-remote", action="store_true")
    serve.set_defaults(function=command_serve)

    run = subparsers.add_parser("run", help="注册、运行并跟踪一个工作进程")
    run.add_argument("--batch-id", default=None)
    run.add_argument("--run-id", default=None)
    run.add_argument("--stage", default=None)
    run.add_argument("--heartbeat-seconds", type=float, default=5)
    run.add_argument("command", nargs=argparse.REMAINDER)
    run.set_defaults(function=command_run)

    record = subparsers.add_parser("record", help="手工写入一条 provider usage")
    record.add_argument("--provider", required=True)
    record.add_argument("--model", required=True)
    record.add_argument("--feature", required=True)
    record.add_argument("--input-tokens", type=int, default=0)
    record.add_argument("--output-tokens", type=int, default=0)
    record.add_argument("--cache-read-tokens", type=int, default=0)
    record.add_argument("--cache-write-tokens", type=int, default=0)
    record.add_argument("--batch-id", default=None)
    record.add_argument("--run-id", default=None)
    record.add_argument("--stage", default=None)
    record.add_argument("--request-id", default=None)
    record.set_defaults(function=command_record)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.function(args))


if __name__ == "__main__":
    raise SystemExit(main())
