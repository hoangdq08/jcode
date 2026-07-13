#!/usr/bin/env python3
"""Live benchmark for proactive sponsored Discovery triggering.

The runner starts an isolated Jcode server marked with
JCODE_DISCOVERY_BENCHMARK=1, verifies that every live catalog listing has a
natural-language benchmark case, then retries each case until the model calls
``discover_tools`` and receives the expected tool in a browse listing.

No setup instructions are requested and the model process is stopped as soon
as the expected browse listing arrives.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import signal
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASES = REPO_ROOT / "scripts" / "discovery_benchmark_cases.json"
DEFAULT_OUTPUT = REPO_ROOT / "target" / "discovery-benchmark" / "latest.json"
CATEGORY_SOURCE = REPO_ROOT / "crates" / "jcode-base" / "src" / "sponsors.rs"
BENCHMARK_ENV = "JCODE_DISCOVERY_BENCHMARK"
BENCHMARK_HEADER = "x-jcode-discovery-benchmark"
LISTING_RE = re.compile(r"Discoverable tools in '([^']+)'")
EMPTY_RE = re.compile(r"No discoverable tools in category '([^']+)'")
TOOL_RE = re.compile(r"^- ([^:\n]+):", re.MULTILINE)
RUNTIME_ERROR_RE = re.compile(
    r"\b(error|failed|failure|timed out|timeout|did not start|exited before startup)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class BenchmarkCase:
    id: str
    expected_category: str
    expected_tool: str
    prompt: str


@dataclass
class DiscoveryCall:
    elapsed_seconds: float
    category: str | None
    tools: list[str]
    outcome: str
    output: str


@dataclass
class AttemptResult:
    attempt: int
    success: bool
    elapsed_seconds: float
    hit_seconds: float | None
    exit_code: int | None
    timed_out: bool
    discovery_calls: list[DiscoveryCall]
    runtime_error_count: int
    stderr_tail: str


class BenchmarkError(RuntimeError):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Benchmark whether natural prompts trigger the expected Discovery listing."
    )
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--case", action="append", dest="case_ids", help="Run only this case ID or tool name. Repeatable.")
    parser.add_argument("--catalog-file", type=Path, help="Use a saved catalog JSON instead of the live endpoint.")
    parser.add_argument("--allow-catalog-mismatch", action="store_true", help="Run even when live listings and benchmark cases differ.")
    parser.add_argument("--trials", type=int, default=1, help="Independent retry-until-hit trials per case.")
    parser.add_argument("--max-attempts", type=int, default=5, help="Maximum model attempts in each trial.")
    parser.add_argument("--timeout", type=float, default=90.0, help="Seconds allowed per model attempt.")
    parser.add_argument("--catalog-retries", type=int, default=4)
    parser.add_argument("--retry-delay", type=float, default=0.5)
    parser.add_argument("--jcode", default=os.environ.get("JCODE_BIN", "jcode"))
    parser.add_argument("--model", default=os.environ.get("JCODE_DISCOVERY_BENCHMARK_MODEL", "gpt-5.6-sol"))
    parser.add_argument("--provider", default=os.environ.get("JCODE_DISCOVERY_BENCHMARK_PROVIDER"))
    parser.add_argument(
        "--discovery-only",
        action="store_true",
        help="Expose only discover_tools for a focused smoke test instead of the normal toolset.",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--dry-run", action="store_true", help="Validate catalog coverage without calling a model.")
    args = parser.parse_args()
    if args.trials < 1 or args.max_attempts < 1:
        parser.error("--trials and --max-attempts must be at least 1")
    if args.timeout <= 0 or args.catalog_retries < 1 or args.retry_delay < 0:
        parser.error("timeouts/retries must be positive")
    return args


def load_categories(path: Path = CATEGORY_SOURCE) -> list[str]:
    text = path.read_text(encoding="utf-8")
    match = re.search(r"pub const DISCOVERY_CATEGORIES: &\[&str\] = &\[(.*?)\];", text, re.DOTALL)
    if not match:
        raise BenchmarkError(f"could not parse DISCOVERY_CATEGORIES from {path}")
    categories = re.findall(r'"([a-z0-9-]+)"', match.group(1))
    if not categories:
        raise BenchmarkError(f"no Discovery categories found in {path}")
    return categories


def load_cases(path: Path) -> list[BenchmarkCase]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("version") != 1 or not isinstance(data.get("cases"), list):
        raise BenchmarkError(f"unsupported benchmark case file: {path}")
    cases: list[BenchmarkCase] = []
    seen_ids: set[str] = set()
    seen_tools: set[tuple[str, str]] = set()
    for raw in data["cases"]:
        case = BenchmarkCase(
            id=str(raw.get("id", "")).strip(),
            expected_category=str(raw.get("expected_category", "")).strip().lower(),
            expected_tool=str(raw.get("expected_tool", "")).strip().lower(),
            prompt=str(raw.get("prompt", "")).strip(),
        )
        if not all(asdict(case).values()):
            raise BenchmarkError(f"benchmark case has an empty field: {raw}")
        key = (case.expected_category, case.expected_tool)
        if case.id in seen_ids:
            raise BenchmarkError(f"duplicate benchmark case id: {case.id}")
        if key in seen_tools:
            raise BenchmarkError(f"duplicate benchmark target: {key[0]}/{key[1]}")
        lowered_prompt = case.prompt.lower()
        if case.expected_tool in lowered_prompt or "discover_tools" in lowered_prompt or "tool discovery" in lowered_prompt:
            raise BenchmarkError(f"case {case.id} leaks its expected tool or Discovery into the prompt")
        seen_ids.add(case.id)
        seen_tools.add(key)
        cases.append(case)
    return cases


def load_catalog_file(path: Path, categories: list[str]) -> dict[str, list[dict[str, Any]]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    raw_categories = data.get("categories", data)
    if not isinstance(raw_categories, dict):
        raise BenchmarkError("catalog file must contain a category-to-listing mapping")
    catalog: dict[str, list[dict[str, Any]]] = {}
    for category in categories:
        raw = raw_categories.get(category, [])
        if isinstance(raw, dict):
            raw = raw.get("tools", [])
        if not isinstance(raw, list):
            raise BenchmarkError(f"catalog file category {category!r} is not a list")
        catalog[category] = raw
    return catalog


def catalog_targets(catalog: dict[str, list[dict[str, Any]]]) -> set[tuple[str, str]]:
    targets: set[tuple[str, str]] = set()
    for category, tools in catalog.items():
        for tool in tools:
            name = str(tool.get("name", "")).strip().lower()
            if not name:
                raise BenchmarkError(f"catalog entry in {category!r} has no name")
            targets.add((category, name))
    return targets


def validate_catalog_coverage(cases: list[BenchmarkCase], catalog: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    live = catalog_targets(catalog)
    covered = {(case.expected_category, case.expected_tool) for case in cases}
    return {
        "live_targets": sorted(f"{category}/{tool}" for category, tool in live),
        "case_targets": sorted(f"{category}/{tool}" for category, tool in covered),
        "missing_cases": sorted(f"{category}/{tool}" for category, tool in live - covered),
        "stale_cases": sorted(f"{category}/{tool}" for category, tool in covered - live),
    }


def filter_cases(cases: list[BenchmarkCase], filters: list[str] | None) -> list[BenchmarkCase]:
    if not filters:
        return cases
    wanted = {value.lower() for value in filters}
    selected = [case for case in cases if case.id.lower() in wanted or case.expected_tool in wanted]
    missing = wanted - {case.id.lower() for case in selected} - {case.expected_tool for case in selected}
    if missing:
        raise BenchmarkError(f"unknown --case values: {', '.join(sorted(missing))}")
    return selected


def parse_discovery_output(output: str, elapsed: float) -> DiscoveryCall:
    listing = LISTING_RE.search(output)
    empty = EMPTY_RE.search(output)
    category = listing.group(1) if listing else empty.group(1) if empty else None
    tools = [match.strip().lower() for match in TOOL_RE.findall(output)] if listing else []
    if listing:
        outcome = "listing"
    elif empty:
        outcome = "empty"
    elif output.startswith("Error:"):
        outcome = "error"
    else:
        outcome = "other"
    return DiscoveryCall(
        elapsed_seconds=round(elapsed, 3),
        category=category,
        tools=tools,
        outcome=outcome,
        output=output[:4000],
    )


def _pump(stream: Any, source: str, messages: queue.Queue[tuple[str, str | None]]) -> None:
    try:
        for line in iter(stream.readline, ""):
            messages.put((source, line))
    finally:
        messages.put((source, None))


def terminate_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=5)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass


def benchmark_environment(socket_path: Path) -> dict[str, str]:
    return {
        **os.environ,
        BENCHMARK_ENV: "1",
        "JCODE_RUNTIME_DIR": str(socket_path.parent),
    }


def run_debug_command(
    args: argparse.Namespace,
    socket_path: Path,
    command: str,
    argument: str | None = None,
    session_id: str | None = None,
    timeout: float = 30,
) -> str:
    invocation = [args.jcode, "--socket", str(socket_path), "debug"]
    if session_id:
        invocation += ["-S", session_id]
    invocation.append(command)
    if argument is not None:
        invocation.append(argument)
    result = subprocess.run(
        invocation,
        capture_output=True,
        text=True,
        env=benchmark_environment(socket_path),
        timeout=timeout,
    )
    if result.returncode != 0:
        raise BenchmarkError(result.stderr.strip() or result.stdout.strip() or f"debug command failed: {command}")
    return result.stdout.strip()


def fetch_catalog_via_jcode(
    args: argparse.Namespace,
    socket_path: Path,
    workdir: Path,
    categories: list[str],
) -> dict[str, list[dict[str, Any]]]:
    created = json.loads(
        run_debug_command(args, socket_path, f"create_session:{workdir}", timeout=60)
    )
    session_id = created["session_id"]
    catalog: dict[str, list[dict[str, Any]]] = {}
    try:
        for index, category in enumerate(categories, start=1):
            last_error: Exception | None = None
            for attempt in range(1, args.catalog_retries + 1):
                tool_input = json.dumps(
                    {
                        "category": category,
                        "query": "benchmark catalog coverage",
                        "reason": "validate live Discovery benchmark catalog coverage",
                    },
                    separators=(",", ":"),
                )
                try:
                    raw = run_debug_command(
                        args,
                        socket_path,
                        "tool",
                        f"discover_tools {tool_input}",
                        session_id=session_id,
                        timeout=20,
                    )
                    payload = json.loads(raw)
                    call = parse_discovery_output(str(payload.get("output", "")), 0.0)
                    if call.category != category or call.outcome not in {"listing", "empty"}:
                        raise BenchmarkError(
                            f"unexpected catalog response for {category}: {payload.get('output', '')}"
                        )
                    catalog[category] = [{"name": name} for name in call.tools]
                    break
                except Exception as error:
                    last_error = error
                    if attempt < args.catalog_retries:
                        time.sleep(args.retry_delay * attempt)
            else:
                raise BenchmarkError(
                    f"catalog discovery failed for {category} after "
                    f"{args.catalog_retries} attempts: {last_error}"
                )
            progress(index, len(categories), "categories", f"Fetched catalog category {category}")
    finally:
        try:
            run_debug_command(args, socket_path, f"destroy_session:{session_id}")
        except BenchmarkError:
            pass
    return catalog


def run_attempt(args: argparse.Namespace, case: BenchmarkCase, attempt: int, socket_path: Path, workdir: Path) -> AttemptResult:
    command = [
        args.jcode,
        "--socket",
        str(socket_path),
        "--no-selfdev",
        "--no-update",
        "--model",
        args.model,
        "-C",
        str(workdir),
    ]
    if args.provider:
        command += ["--provider", args.provider]
    if args.discovery_only:
        command += ["--disable-base-tools", "--tools", "discover_tools"]
    command += ["run", "--ndjson", case.prompt]

    environment = benchmark_environment(socket_path)
    started = time.monotonic()
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        env=environment,
        start_new_session=True,
    )
    assert process.stdout is not None and process.stderr is not None
    messages: queue.Queue[tuple[str, str | None]] = queue.Queue()
    threads = [
        threading.Thread(target=_pump, args=(process.stdout, "stdout", messages), daemon=True),
        threading.Thread(target=_pump, args=(process.stderr, "stderr", messages), daemon=True),
    ]
    for thread in threads:
        thread.start()

    discovery_calls: list[DiscoveryCall] = []
    stderr_parts: list[str] = []
    success = False
    hit_seconds: float | None = None
    timed_out = False
    closed_streams = 0
    deadline = started + args.timeout

    while closed_streams < 2:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timed_out = True
            break
        try:
            source, line = messages.get(timeout=min(0.25, remaining))
        except queue.Empty:
            if process.poll() is not None and all(not thread.is_alive() for thread in threads):
                break
            continue
        if line is None:
            closed_streams += 1
            continue
        if source == "stderr":
            stderr_parts.append(line)
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "tool_done" or event.get("name") != "discover_tools":
            continue
        call = parse_discovery_output(str(event.get("output", "")), time.monotonic() - started)
        discovery_calls.append(call)
        if call.category == case.expected_category and case.expected_tool in call.tools:
            success = True
            hit_seconds = call.elapsed_seconds
            break

    if success or timed_out:
        terminate_process(process)
    else:
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            terminate_process(process)

    elapsed = time.monotonic() - started
    stderr_tail = "".join(stderr_parts)[-4000:]
    return AttemptResult(
        attempt=attempt,
        success=success,
        elapsed_seconds=round(elapsed, 3),
        hit_seconds=hit_seconds,
        exit_code=process.poll(),
        timed_out=timed_out,
        discovery_calls=discovery_calls,
        runtime_error_count=len(RUNTIME_ERROR_RE.findall(stderr_tail)),
        stderr_tail=stderr_tail,
    )


def progress(current: int, total: int, unit: str, message: str) -> None:
    print(
        "JCODE_PROGRESS "
        + json.dumps(
            {
                "current": current,
                "total": total,
                "unit": unit,
                "message": message,
            },
            separators=(",", ":"),
        ),
        flush=True,
    )


def start_server(args: argparse.Namespace, socket_path: Path) -> subprocess.Popen[str]:
    socket_path.unlink(missing_ok=True)
    command = [
        args.jcode,
        "--socket",
        str(socket_path),
        "--no-selfdev",
        "--no-update",
    ]
    if args.provider:
        command += ["--provider", args.provider]
    command += ["serve", "--server-name", f"discovery-benchmark-{os.getpid()}"]
    environment = benchmark_environment(socket_path)
    log_path = socket_path.parent / "server.log"
    log = log_path.open("w", encoding="utf-8")
    process = subprocess.Popen(
        command,
        stdout=log,
        stderr=subprocess.STDOUT,
        text=True,
        env=environment,
        start_new_session=True,
    )
    log.close()
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            probe = subprocess.run(
                [args.jcode, "--socket", str(socket_path), "debug", "server:info"],
                capture_output=True,
                text=True,
                env=environment,
                timeout=2,
            )
            if probe.returncode == 0:
                return process
        except subprocess.TimeoutExpired:
            pass
        if process.poll() not in (None, 0):
            details = log_path.read_text(encoding="utf-8", errors="replace")[-4000:]
            raise BenchmarkError(f"benchmark server exited early: {details}")
        time.sleep(0.2)
    terminate_process(process)
    details = log_path.read_text(encoding="utf-8", errors="replace")[-4000:]
    raise BenchmarkError(f"benchmark server did not become ready on {socket_path}: {details}")


def stop_server(args: argparse.Namespace, socket_path: Path, process: subprocess.Popen[str]) -> None:
    try:
        try:
            subprocess.run(
                [args.jcode, "--socket", str(socket_path), "server", "stop"],
                capture_output=True,
                text=True,
                env=benchmark_environment(socket_path),
                timeout=20,
            )
        except subprocess.TimeoutExpired:
            pass
        terminate_process(process)
    finally:
        socket_path.unlink(missing_ok=True)


def summarize_case(case: BenchmarkCase, trials: list[dict[str, Any]]) -> dict[str, Any]:
    successful = [trial for trial in trials if trial["success"]]
    attempts = [trial["attempts_to_hit"] for trial in successful]
    hit_times = [trial["hit_seconds"] for trial in successful]
    wrong_categories: dict[str, int] = {}
    runtime_confounded_trials = 0
    for trial in trials:
        if not trial["success"] and any(
            attempt.get("runtime_error_count", 0) > 0 for attempt in trial["attempts"]
        ):
            runtime_confounded_trials += 1
        for attempt in trial["attempts"]:
            for call in attempt["discovery_calls"]:
                category = call.get("category")
                if category and category != case.expected_category:
                    wrong_categories[category] = wrong_categories.get(category, 0) + 1
    return {
        "case": asdict(case),
        "trial_count": len(trials),
        "successful_trials": len(successful),
        "success_rate": len(successful) / len(trials),
        "mean_attempts_to_hit": round(statistics.mean(attempts), 3) if attempts else None,
        "median_hit_seconds": round(statistics.median(hit_times), 3) if hit_times else None,
        "runtime_confounded_trials": runtime_confounded_trials,
        "wrong_category_calls": dict(sorted(wrong_categories.items())),
        "trials": trials,
    }


def run_benchmark(args: argparse.Namespace, cases: list[BenchmarkCase], socket_path: Path, workdir: Path) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    total_trials = len(cases) * args.trials
    completed_trials = 0
    for case in cases:
        trials: list[dict[str, Any]] = []
        for trial_index in range(1, args.trials + 1):
            attempts: list[AttemptResult] = []
            for attempt_index in range(1, args.max_attempts + 1):
                print(
                    f"[{case.id}] trial {trial_index}/{args.trials}, attempt "
                    f"{attempt_index}/{args.max_attempts}",
                    flush=True,
                )
                attempt = run_attempt(args, case, attempt_index, socket_path, workdir)
                attempts.append(attempt)
                if attempt.success:
                    break
                if args.retry_delay:
                    time.sleep(args.retry_delay)
            hit = next((attempt for attempt in attempts if attempt.success), None)
            trials.append(
                {
                    "trial": trial_index,
                    "success": hit is not None,
                    "attempts_to_hit": hit.attempt if hit else None,
                    "hit_seconds": hit.hit_seconds if hit else None,
                    "outcome": (
                        "hit"
                        if hit
                        else "runtime-confounded-miss"
                        if any(attempt.runtime_error_count > 0 for attempt in attempts)
                        else "clean-miss"
                    ),
                    "attempts": [
                        {
                            **asdict(attempt),
                            "discovery_calls": [asdict(call) for call in attempt.discovery_calls],
                        }
                        for attempt in attempts
                    ],
                }
            )
            completed_trials += 1
            progress(completed_trials, total_trials, "trials", f"Completed {case.id} trial {trial_index}")
        results.append(summarize_case(case, trials))
    return results


def write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def main() -> int:
    args = parse_args()
    started_at = datetime.now(timezone.utc)
    categories = load_categories()
    all_cases = load_cases(args.cases)
    cases = filter_cases(all_cases, args.case_ids)
    catalog: dict[str, list[dict[str, Any]]]
    results: list[dict[str, Any]] = []

    if args.catalog_file and args.dry_run:
        catalog = load_catalog_file(args.catalog_file, categories)
        coverage = validate_catalog_coverage(all_cases, catalog)
        mismatch = bool(coverage["missing_cases"] or coverage["stale_cases"])
        if mismatch and not args.allow_catalog_mismatch:
            raise BenchmarkError(
                "catalog coverage mismatch: "
                f"missing cases={coverage['missing_cases']}, stale cases={coverage['stale_cases']}"
            )
    else:
        with tempfile.TemporaryDirectory(prefix="jcode-discovery-benchmark-") as temp_dir:
            temporary_root = Path(temp_dir)
            socket_path = temporary_root / "jcode.sock"
            workdir = temporary_root / "workspace"
            workdir.mkdir()
            server_process = start_server(args, socket_path)
            try:
                catalog = (
                    load_catalog_file(args.catalog_file, categories)
                    if args.catalog_file
                    else fetch_catalog_via_jcode(args, socket_path, workdir, categories)
                )
                coverage = validate_catalog_coverage(all_cases, catalog)
                mismatch = bool(coverage["missing_cases"] or coverage["stale_cases"])
                if mismatch and not args.allow_catalog_mismatch:
                    raise BenchmarkError(
                        "catalog coverage mismatch: "
                        f"missing cases={coverage['missing_cases']}, "
                        f"stale cases={coverage['stale_cases']}"
                    )
                if not args.dry_run:
                    results = run_benchmark(args, cases, socket_path, workdir)
            finally:
                stop_server(args, socket_path, server_process)

    report: dict[str, Any] = {
        "benchmark": "discovery-trigger",
        "version": 1,
        "started_at": started_at.isoformat(),
        "benchmark_marker": {
            "environment": f"{BENCHMARK_ENV}=1",
            "request_header": f"{BENCHMARK_HEADER}: 1",
            "telemetry_field": "benchmark_run=true",
        },
        "config": {
            "catalog_source": str(args.catalog_file) if args.catalog_file else "discover_tools",
            "model": args.model,
            "provider": args.provider,
            "tool_mode": "discovery-only" if args.discovery_only else "full",
            "trials": args.trials,
            "max_attempts": args.max_attempts,
            "timeout_seconds": args.timeout,
            "cases_file": str(args.cases),
        },
        "coverage": coverage,
        "catalog": catalog,
        "results": results,
    }

    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    report["passed"] = not mismatch and all(
        result["successful_trials"] == result["trial_count"] for result in report["results"]
    )
    if args.dry_run:
        report["passed"] = not mismatch
    write_report(args.output, report)

    print("\nDiscovery benchmark summary")
    print(f"  Catalog targets: {len(coverage['live_targets'])}")
    print(f"  Coverage mismatch: {mismatch}")
    for result in report["results"]:
        print(
            f"  {result['case']['id']}: {result['successful_trials']}/{result['trial_count']} trials, "
            f"mean attempts={result['mean_attempts_to_hit']}, median hit={result['median_hit_seconds']}s, "
            f"runtime-confounded misses={result['runtime_confounded_trials']}"
        )
    print(f"  Report: {args.output}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BenchmarkError as error:
        print(f"discovery benchmark error: {error}", file=sys.stderr)
        raise SystemExit(2)
