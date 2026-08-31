from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import math
import platform
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


def _canonical_json(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _render(value: Any, request_index: int) -> Any:
    if isinstance(value, str):
        return value.replace("{request_index}", str(request_index))
    if isinstance(value, list):
        return [_render(item, request_index) for item in value]
    if isinstance(value, dict):
        return {key: _render(item, request_index) for key, item in value.items()}
    return value


def _percentile(values: list[float], probability: float) -> float:
    if not values:
        raise ValueError("cannot calculate a percentile of an empty sample")
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(probability * len(ordered)) - 1))
    return ordered[index]


def run_load_test(
    *,
    url: str,
    method: str,
    payload_template: dict[str, Any] | None,
    headers: dict[str, str],
    requests: int,
    warmup: int,
    concurrency: int,
    timeout_seconds: float,
    p99_budget_ms: float,
) -> dict[str, Any]:
    if requests < 1 or concurrency < 1 or warmup < 0:
        raise ValueError("requests/concurrency must be positive and warmup non-negative")

    def issue(index: int) -> tuple[int, float, int, str | None]:
        payload = None
        request_headers = {"Accept": "application/json", **headers}
        if payload_template is not None:
            payload = _canonical_json(_render(payload_template, index))
            request_headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=payload, headers=request_headers, method=method)
        started = time.perf_counter_ns()
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                body = response.read()
                status = int(response.status)
            error = None
        except urllib.error.HTTPError as exc:
            body = exc.read()
            status = int(exc.code)
            error = f"http_{status}"
        except Exception as exc:  # load-test result must retain transport failures
            body = b""
            status = 0
            error = type(exc).__name__
        elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
        return status, elapsed_ms, len(body), error

    for index in range(-warmup, 0):
        issue(index)

    started = time.perf_counter_ns()
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
        observations = list(executor.map(issue, range(requests)))
    wall_ms = (time.perf_counter_ns() - started) / 1_000_000

    latencies = [item[1] for item in observations]
    successes = sum(1 for status, _, _, error in observations if 200 <= status < 300 and error is None)
    errors: dict[str, int] = {}
    statuses: dict[str, int] = {}
    for status, _, _, error in observations:
        statuses[str(status)] = statuses.get(str(status), 0) + 1
        if error:
            errors[error] = errors.get(error, 0) + 1
    p99 = _percentile(latencies, 0.99)
    payload_sha = hashlib.sha256(_canonical_json(payload_template)).hexdigest() if payload_template else None
    return {
        "schema_version": "barnabus-service-load-v1",
        "quantity_status": "measured_local",
        "target": {"url": url, "method": method},
        "workload": {
            "requests": requests,
            "warmup_requests": warmup,
            "concurrency": concurrency,
            "timeout_seconds": timeout_seconds,
            "payload_template_sha256": payload_sha,
            "header_names": sorted(headers),
        },
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
        },
        "measurements": {
            "successes": successes,
            "failures": requests - successes,
            "status_counts": statuses,
            "error_counts": errors,
            "response_bytes": sum(item[2] for item in observations),
            "wall_ms": round(wall_ms, 6),
            "throughput_requests_per_second": round(requests * 1000 / wall_ms, 6),
            "p50_ms": round(_percentile(latencies, 0.50), 6),
            "p95_ms": round(_percentile(latencies, 0.95), 6),
            "p99_ms": round(p99, 6),
            "max_ms": round(max(latencies), 6),
        },
        "budget": {
            "metric": "end_to_end_client_p99_ms",
            "limit_ms": p99_budget_ms,
            "passed": successes == requests and p99 <= p99_budget_ms,
        },
        "limitations": [
            "This is an observed local HTTP measurement, not a sealed-scale or production-SLA guarantee.",
            "Client and service shared one host; network and orchestration latency are not represented.",
        ],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Reproducible HTTP p99 load test for Barnabus services")
    parser.add_argument("--url", required=True)
    parser.add_argument("--method", choices=("GET", "POST"), default="GET")
    parser.add_argument("--payload", type=Path)
    parser.add_argument("--header", action="append", default=[], metavar="NAME=VALUE")
    parser.add_argument("--requests", type=int, default=200)
    parser.add_argument("--warmup", type=int, default=20)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--timeout-seconds", type=float, default=5.0)
    parser.add_argument("--p99-budget-ms", type=float, required=True)
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    payload = json.loads(args.payload.read_text(encoding="utf-8")) if args.payload else None
    headers: dict[str, str] = {}
    for item in args.header:
        if "=" not in item:
            raise SystemExit("--header values must have NAME=VALUE form")
        name, value = item.split("=", 1)
        headers[name] = value
    result = run_load_test(
        url=args.url,
        method=args.method,
        payload_template=payload,
        headers=headers,
        requests=args.requests,
        warmup=args.warmup,
        concurrency=args.concurrency,
        timeout_seconds=args.timeout_seconds,
        p99_budget_ms=args.p99_budget_ms,
    )
    output = _canonical_json(result)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(output)
    sys.stdout.buffer.write(output)
    return 0 if result["budget"]["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
