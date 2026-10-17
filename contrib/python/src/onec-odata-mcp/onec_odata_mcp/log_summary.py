"""Produce a content-free aggregate from local 1C OData performance JSONL."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
import math
import os
from pathlib import Path
import stat
import sys
from typing import Any

from .diagnostics import (
    _ALLOWED_ERROR_CATEGORIES,
    _ALLOWED_OUTCOMES,
    _ALLOWED_STAGES,
    _ALLOWED_TOOLS,
    _ALLOWED_SEARCH_CLASSES,
    _ALLOWED_SEARCH_STRATEGIES,
    TRANSPORT_SUBREASONS,
)


def _number(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        if math.isfinite(number) and number >= 0:
            return number
    return None


def _integer(value: Any) -> int | None:
    number = _number(value)
    if number is not None and number.is_integer():
        return int(number)
    return None


def _stats(values: list[float]) -> dict[str, float | int]:
    ordered = sorted(values)
    if not ordered:
        return {"count": 0, "total_ms": 0, "mean_ms": 0, "p50_ms": 0, "p95_ms": 0}

    def percentile(fraction: float) -> float:
        index = max(0, math.ceil(fraction * len(ordered)) - 1)
        return round(ordered[index], 1)

    return {
        "count": len(ordered),
        "total_ms": round(sum(ordered), 1),
        "mean_ms": round(sum(ordered) / len(ordered), 1),
        "p50_ms": percentile(0.50),
        "p95_ms": percentile(0.95),
    }


def summarize(path: Path) -> dict[str, Any]:
    tool_times: dict[str, list[float]] = defaultdict(list)
    tool_event_counts: Counter[str] = Counter()
    tool_outcomes: dict[str, Counter[str]] = defaultdict(Counter)
    tool_errors: dict[str, Counter[str]] = defaultdict(Counter)
    tool_counts: dict[str, Counter[str]] = defaultdict(Counter)
    stage_times: dict[str, list[float]] = defaultdict(list)
    stage_outcomes: dict[str, Counter[str]] = defaultdict(Counter)
    stage_errors: dict[str, Counter[str]] = defaultdict(Counter)
    stage_counts: dict[str, Counter[str]] = defaultdict(Counter)
    attempt_times: dict[str, list[float]] = defaultdict(list)
    attempt_statuses: dict[str, Counter[str]] = defaultdict(Counter)
    attempt_errors: dict[str, Counter[str]] = defaultdict(Counter)
    attempt_subreasons: dict[str, Counter[str]] = defaultdict(Counter)
    attempt_tool_counts: Counter[str] = Counter()
    attempt_tool_times: dict[tuple[str, str], list[float]] = defaultdict(list)
    attempt_tool_statuses: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)
    attempt_tool_errors: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)
    attempt_tool_subreasons: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)
    row_outcomes: Counter[str] = Counter()
    row_errors: Counter[str] = Counter()
    queue_sizes: list[int] = []
    queue_concurrency: list[int] = []
    queue_result_counts: Counter[str] = Counter()
    queue_outcome_sizes: dict[str, list[int]] = defaultdict(list)
    queue_outcome_concurrency: dict[str, list[int]] = defaultdict(list)
    queue_outcome_result_counts: dict[str, Counter[str]] = defaultdict(Counter)
    run_order: list[str] = []
    run_event_counts: Counter[str] = Counter()
    run_attempt_counts: Counter[str] = Counter()
    run_tool_calls: dict[str, Counter[str]] = defaultdict(Counter)
    run_tool_elapsed: dict[str, list[float]] = defaultdict(list)
    run_tool_outcomes: dict[str, Counter[str]] = defaultdict(Counter)
    search_profile_times: dict[tuple[str, str], list[float]] = defaultdict(list)
    search_profile_counts: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)
    search_profile_outcomes: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)
    event_count = 0
    attempt_count = 0
    malformed_lines = 0
    known_runs: set[str] = set()

    with path.open("r", encoding="utf-8") as stream:
        for line in stream:
            try:
                event = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                malformed_lines += 1
                continue
            if not isinstance(event, dict):
                malformed_lines += 1
                continue
            kind = event.get("event")
            if kind not in {"mcp_call", "stage", "odata_attempt", "queue_row"}:
                malformed_lines += 1
                continue
            event_count += 1
            run_id = event.get("run_id")
            if isinstance(run_id, str) and len(run_id) == 32 and all(
                character in "0123456789abcdef" for character in run_id
            ):
                if run_id not in known_runs:
                    run_order.append(run_id)
                known_runs.add(run_id)
                run_event_counts[run_id] += 1
            else:
                run_id = None

            elapsed = _number(event.get("elapsed_ms"))
            outcome = event.get("outcome")
            if not isinstance(outcome, str) or outcome not in _ALLOWED_OUTCOMES:
                outcome = "unknown"
            stage = event.get("stage")
            if not isinstance(stage, str) or stage not in _ALLOWED_STAGES:
                stage = "other"

            if kind == "mcp_call":
                tool = event.get("tool")
                if not isinstance(tool, str) or tool not in _ALLOWED_TOOLS:
                    tool = "other"
                if elapsed is not None:
                    tool_times[tool].append(elapsed)
                tool_event_counts[tool] += 1
                tool_outcomes[tool][outcome] += 1
                if run_id is not None:
                    run_tool_calls[run_id][tool] += 1
                    run_tool_outcomes[run_id][outcome] += 1
                    if elapsed is not None:
                        run_tool_elapsed[run_id].append(elapsed)
                error = event.get("error_category")
                if isinstance(error, str) and error in _ALLOWED_ERROR_CATEGORIES:
                    tool_errors[tool][error] += 1
                for source, target in (("odata_calls", "odata_calls"),
                                       ("odata_attempts", "odata_attempts"),
                                       ("mcp_calls", "mcp_calls")):
                    number = _integer(event.get(source))
                    if number is not None:
                        tool_counts[tool][target] += number
                if tool == "onec83_apply_document_queue":
                    size = _integer(event.get("wave_size"))
                    concurrency = _integer(event.get("concurrency"))
                    # Keep the normalized outcome even when this event lacks a valid wave size.
                    queue_outcome_result_counts[outcome]
                    if size is not None:
                        queue_sizes.append(size)
                        queue_outcome_sizes[outcome].append(size)
                    if concurrency is not None:
                        queue_concurrency.append(concurrency)
                        queue_outcome_concurrency[outcome].append(concurrency)
                    for field in ("rows_succeeded", "rows_failed", "rows_uncertain"):
                        number = _integer(event.get(field))
                        if number is not None:
                            queue_result_counts[field] += number
                            queue_outcome_result_counts[outcome][field] += number
                if tool in {"onec83_search_entities", "onec83_search_entities_batch"}:
                    search_class = event.get("search_class")
                    search_strategy = event.get("search_strategy")
                    if (isinstance(search_class, str)
                            and search_class in _ALLOWED_SEARCH_CLASSES
                            and isinstance(search_strategy, str)
                            and search_strategy in _ALLOWED_SEARCH_STRATEGIES):
                        profile = (search_class, search_strategy)
                        search_profile_outcomes[profile][outcome] += 1
                        if elapsed is not None:
                            search_profile_times[profile].append(elapsed)
                        for source, target in (
                            ("odata_calls", "odata_calls"),
                            ("odata_attempts", "odata_attempts"),
                            ("fallback_page_reads", "fallback_page_reads"),
                            ("date_lower_bound_probes", "date_lower_bound_probes"),
                        ):
                            number = _integer(event.get(source))
                            search_profile_counts[profile][target] += number or 0
            elif kind == "stage":
                if elapsed is not None:
                    stage_times[stage].append(elapsed)
                stage_outcomes[stage][outcome] += 1
                error = event.get("error_category")
                if isinstance(error, str) and error in _ALLOWED_ERROR_CATEGORIES:
                    stage_errors[stage][error] += 1
                for source, target in (("odata_calls", "odata_calls"),
                                       ("odata_attempts", "odata_attempts")):
                    number = _integer(event.get(source))
                    if number is not None:
                        stage_counts[stage][target] += number
            elif kind == "odata_attempt":
                attempt_count += 1
                tool = event.get("tool")
                if not isinstance(tool, str) or tool not in _ALLOWED_TOOLS:
                    tool = "other"
                attempt_tool_counts[tool] += 1
                if run_id is not None:
                    run_attempt_counts[run_id] += 1
                if elapsed is not None:
                    attempt_times[stage].append(elapsed)
                    attempt_tool_times[(tool, stage)].append(elapsed)
                status = _integer(event.get("http_status"))
                if status is not None:
                    attempt_statuses[stage][str(status)] += 1
                    attempt_tool_statuses[(tool, stage)][str(status)] += 1
                category = event.get("error_category")
                if isinstance(category, str) and category in _ALLOWED_ERROR_CATEGORIES:
                    attempt_errors[stage][category] += 1
                    attempt_tool_errors[(tool, stage)][category] += 1
                subreason = event.get("transport_subreason")
                if (
                    event.get("http_status") is None
                    and isinstance(subreason, str)
                    and subreason in TRANSPORT_SUBREASONS
                ):
                    attempt_subreasons[stage][subreason] += 1
                    attempt_tool_subreasons[(tool, stage)][subreason] += 1
            else:
                row_outcomes[outcome] += 1
                error = event.get("error_category")
                if isinstance(error, str) and error in _ALLOWED_ERROR_CATEGORIES:
                    row_errors[error] += 1

    tools = {
        name: {
            "calls": tool_event_counts[name],
            "elapsed_ms": _stats(tool_times[name]),
            "outcomes": dict(sorted(tool_outcomes[name].items())),
            "error_categories": dict(sorted(tool_errors[name].items())),
            **dict(sorted(tool_counts[name].items())),
        }
        for name in sorted(set(tool_times) | set(tool_outcomes) | set(tool_counts))
    }
    stages = {
        name: {
            "elapsed_ms": _stats(stage_times[name]),
            "outcomes": dict(sorted(stage_outcomes[name].items())),
            "error_categories": dict(sorted(stage_errors[name].items())),
            **dict(sorted(stage_counts[name].items())),
        }
        for name in sorted(set(stage_times) | set(stage_outcomes) | set(stage_counts))
    }
    attempt_stages = sorted(
        set(attempt_times) | set(attempt_statuses) | set(attempt_errors) | set(attempt_subreasons)
    )
    attempt_tools = {}
    for tool in sorted(attempt_tool_counts):
        tool_stages = sorted(
            {key[1] for key in attempt_tool_times if key[0] == tool}
            | {key[1] for key in attempt_tool_statuses if key[0] == tool}
            | {key[1] for key in attempt_tool_errors if key[0] == tool}
            | {key[1] for key in attempt_tool_subreasons if key[0] == tool}
        )
        tool_status_stages = [
            stage for stage in tool_stages if attempt_tool_statuses[(tool, stage)]
        ]
        tool_error_stages = [
            stage for stage in tool_stages if attempt_tool_errors[(tool, stage)]
        ]
        tool_subreason_stages = [
            stage for stage in tool_stages if attempt_tool_subreasons[(tool, stage)]
        ]
        attempt_tools[tool] = {
            "count": attempt_tool_counts[tool],
            "elapsed_ms_by_stage": {
                stage: _stats(attempt_tool_times[(tool, stage)])
                for stage in tool_stages
            },
            "http_statuses_by_stage": {
                stage: dict(sorted(attempt_tool_statuses[(tool, stage)].items()))
                for stage in tool_status_stages
            },
            "error_categories_by_stage": {
                stage: dict(sorted(attempt_tool_errors[(tool, stage)].items()))
                for stage in tool_error_stages
            },
            **({"transport_subreasons_by_stage": {
                stage: dict(sorted(attempt_tool_subreasons[(tool, stage)].items()))
                for stage in tool_subreason_stages
            }} if tool_subreason_stages else {}),
        }
    attempts = {
        "count": attempt_count,
        "elapsed_ms_by_stage": {
            stage: _stats(attempt_times[stage]) for stage in attempt_stages
        },
        "http_statuses_by_stage": {
            stage: dict(sorted(attempt_statuses[stage].items()))
            for stage in attempt_stages if attempt_statuses[stage]
        },
        "error_categories_by_stage": {
            stage: dict(sorted(attempt_errors[stage].items()))
            for stage in attempt_stages if attempt_errors[stage]
        },
        **({"transport_subreasons_by_stage": {
            stage: dict(sorted(attempt_subreasons[stage].items()))
            for stage in attempt_stages if attempt_subreasons[stage]
        }} if any(attempt_subreasons.values()) else {}),
        "by_tool": attempt_tools,
    }
    runs = [
        {
            "run_index": index,
            "event_count": run_event_counts[run_id],
            "odata_attempts": run_attempt_counts[run_id],
            "tool_calls": dict(sorted(run_tool_calls[run_id].items())),
            "tool_outcomes": dict(sorted(run_tool_outcomes[run_id].items())),
            "tool_elapsed_ms": _stats(run_tool_elapsed[run_id]),
        }
        for index, run_id in enumerate(run_order, start=1)
    ]
    queue_by_outcome = {
        outcome: {
            "waves": len(queue_outcome_sizes[outcome]),
            "rows_submitted": sum(queue_outcome_sizes[outcome]),
            "max_wave_size": max(queue_outcome_sizes[outcome], default=0),
            "max_concurrency": max(queue_outcome_concurrency[outcome], default=0),
            "reported_row_counts": dict(sorted(queue_outcome_result_counts[outcome].items())),
        }
        for outcome in sorted(queue_outcome_result_counts)
    }
    search_profiles = {}
    for search_class in sorted(_ALLOWED_SEARCH_CLASSES):
        for search_strategy in sorted(_ALLOWED_SEARCH_STRATEGIES):
            profile = (search_class, search_strategy)
            if profile not in search_profile_outcomes:
                continue
            search_profiles[f"{search_class}.{search_strategy}"] = {
                "calls": sum(search_profile_outcomes[profile].values()),
                "elapsed_ms": _stats(search_profile_times[profile]),
                "outcomes": dict(sorted(search_profile_outcomes[profile].items())),
                **dict(sorted(search_profile_counts[profile].items())),
            }
    return {
        "schema_version": 1,
        "event_count": event_count,
        "malformed_lines": malformed_lines,
        "run_count": len(known_runs),
        "runs": runs,
        "tools": tools,
        "stages": stages,
        "odata_attempts": attempts,
        "search_profiles": search_profiles,
        "queue": {
            "waves": len(queue_sizes),
            "rows_submitted": sum(queue_sizes),
            "max_wave_size": max(queue_sizes, default=0),
            "max_concurrency": max(queue_concurrency, default=0),
            "row_outcomes": dict(sorted(row_outcomes.items())),
            "row_error_categories": dict(sorted(row_errors.items())),
            "reported_row_counts": dict(sorted(queue_result_counts.items())),
            "by_outcome": queue_by_outcome,
        },
    }


def _write_private(path: Path, content: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError("summary output is not a regular file")
        os.fchmod(descriptor, 0o600)
        remaining = memoryview(content.encode("utf-8"))
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("short write")
            remaining = remaining[written:]
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log_file", type=Path, help="local performance JSONL file")
    parser.add_argument("--output", type=Path, help="save aggregate JSON with private file permissions")
    args = parser.parse_args(argv)
    try:
        result = summarize(args.log_file)
    except (OSError, UnicodeError):
        print("Unable to read the local performance log.", file=sys.stderr)
        return 2
    content = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        try:
            _write_private(args.output, content)
        except OSError:
            print("Unable to write the aggregate file.", file=sys.stderr)
            return 2
    else:
        sys.stdout.write(content)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
