from __future__ import annotations

from contextvars import copy_context
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from onec_odata_mcp.diagnostics import (
    PerformanceLogger,
    TRANSPORT_SUBREASONS,
    diagnostic_row,
    diagnostic_stage,
    instrument_tool_call,
    increment_search_count,
    record_odata_attempt,
    record_request_call,
    set_search_profile,
    set_row_outcome,
)
from onec_odata_mcp.log_summary import summarize


def test_performance_logging_is_disabled_unless_explicitly_enabled(monkeypatch, tmp_path):
    monkeypatch.delenv("ONEC83_PERF_LOGGING", raising=False)
    monkeypatch.setenv("ONEC83_PERF_LOG_PATH", str(tmp_path / "private.jsonl"))

    logger = PerformanceLogger.from_environment()
    assert logger.enabled is False
    with instrument_tool_call("onec83_get_entity", logger) as call:
        assert call is None
        record_request_call()
        record_odata_attempt(stage="entity_read", elapsed_ms=1, attempt=1, status_code=200)
    assert not (tmp_path / "private.jsonl").exists()


def test_accounting_register_reader_has_dedicated_tool_and_stage_metrics(tmp_path):
    log_path = tmp_path / "onec83-performance.jsonl"
    logger = PerformanceLogger(enabled=True, path=log_path)
    tool_name = "onec83_get_accounting_register_record_set"

    with instrument_tool_call(tool_name, logger) as call:
        assert call is not None
        record_request_call()
        record_odata_attempt(
            stage=None,
            elapsed_ms=23.5,
            attempt=1,
            status_code=200,
        )

    events = [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
    ]
    mcp_call = next(event for event in events if event["event"] == "mcp_call")
    assert mcp_call["tool"] == tool_name
    assert mcp_call["stage"] == "movement_verification"
    assert mcp_call["odata_calls"] == 1
    assert mcp_call["odata_attempts"] == 1

    result = summarize(log_path)
    assert result["tools"][tool_name]["calls"] == 1
    assert result["tools"][tool_name]["odata_calls"] == 1
    assert result["tools"][tool_name]["odata_attempts"] == 1
    assert (
        result["odata_attempts"]["elapsed_ms_by_stage"]["movement_verification"]["count"]
        == 1
    )
    assert (
        result["odata_attempts"]["http_statuses_by_stage"]["movement_verification"]
        == {"200": 1}
    )


def test_performance_log_contains_only_fixed_metadata_and_has_private_permissions(tmp_path):
    log_path = tmp_path / "onec83-performance.jsonl"
    logger = PerformanceLogger(enabled=True, path=log_path)
    canary = "PRIVATE-CANARY-Company-Name-123456789-2026-09-29-1234.56"

    with instrument_tool_call("onec83_create_entity", logger) as call:
        assert call is not None
        call.observe_result({
            "ok": False,
            "error": {"kind": "odataHttp", "message": canary},
            "payload": {"Description": canary},
        })
        with diagnostic_stage("create") as trace:
            record_request_call()
            record_odata_attempt(
                stage=None,
                elapsed_ms=83.2,
                attempt=1,
                status_code=401,
            )
            trace.observe_result({"status": "mismatch", "bank_account": canary})

    contents = log_path.read_text(encoding="utf-8")
    assert canary not in contents
    assert "Authorization" not in contents
    events = [json.loads(line) for line in contents.splitlines()]
    assert {event["event"] for event in events} == {"mcp_call", "odata_attempt", "stage"}
    attempt = next(event for event in events if event["event"] == "odata_attempt")
    assert attempt["stage"] == "create"
    assert attempt["http_status"] == 401
    assert attempt["error_category"] == "http_4xx"
    stage = next(event for event in events if event["event"] == "stage")
    assert stage["outcome"] == "failed"
    mcp_call = next(event for event in events if event["event"] == "mcp_call")
    assert mcp_call["odata_calls"] == 1
    assert mcp_call["odata_attempts"] == 1
    assert mcp_call["error_category"] == "odata_http"
    assert os.stat(log_path).st_mode & 0o777 == 0o600
    assert os.stat(log_path.parent).st_mode & 0o777 == 0o700


def test_queue_row_context_survives_worker_handoff_without_logging_row_values(tmp_path):
    log_path = tmp_path / "onec83-performance.jsonl"
    logger = PerformanceLogger(enabled=True, path=log_path)
    canary = "PRIVATE-CANARY-source-row-GUID-date-amount"

    with instrument_tool_call("onec83_apply_document_queue", logger) as call:
        call.observe_result({
            "dryRun": False,
            "total": 1,
            "succeeded": 0,
            "failed": 0,
            "needsReconciliation": 1,
            "maxConcurrency": 1,
            "results": [{"source_key": canary}],
        })

        def process_row():
            with diagnostic_row("a" * 32) as counter:
                record_request_call()
                record_odata_attempt(
                    stage=None,
                    elapsed_ms=9,
                    attempt=1,
                    error_category="tls",
                )
                set_row_outcome(counter, {
                    "state": "needs_reconciliation",
                    "error": {"category": "tls", "message": canary},
                    "document": canary,
                })

        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(copy_context().run, process_row).result()

    contents = log_path.read_text(encoding="utf-8")
    assert canary not in contents
    row_event = next(json.loads(line) for line in contents.splitlines()
                     if json.loads(line)["event"] == "queue_row")
    assert row_event["outcome"] == "needs_reconciliation"
    assert row_event["error_category"] == "tls"


def test_summary_aggregates_metrics_and_discards_identifiers_and_unknown_values(tmp_path):
    log_path = tmp_path / "synthetic.jsonl"
    canary = "PRIVATE-CANARY-secret-company-GUID-amount"
    events = [
        {
            "event": "mcp_call", "run_id": "a" * 32, "call_id": "b" * 32,
            "tool": "onec83_apply_document_queue", "stage": "queue",
            "elapsed_ms": 120, "odata_calls": 5, "odata_attempts": 6,
            "outcome": "partial", "wave_size": 2, "concurrency": 2,
            "rows_succeeded": 1, "rows_failed": 0, "rows_uncertain": 1,
            "request_body": canary,
        },
        {
            "event": "stage", "run_id": "a" * 32, "call_id": "b" * 32,
            "tool": "onec83_apply_document_queue", "stage": "create",
            "elapsed_ms": 80, "odata_calls": 1, "odata_attempts": 1,
            "outcome": "success", "row_token": "real-row-value-" + canary,
        },
        {
            "event": "odata_attempt", "run_id": "a" * 32, "call_id": "b" * 32,
            "tool": "onec83_apply_document_queue", "stage": "create",
            "elapsed_ms": 80, "attempt": 1, "http_status": 503,
            "error_category": "http_5xx", "row_token": canary,
        },
        {
            "event": "queue_row", "run_id": "a" * 32, "call_id": "b" * 32,
            "tool": "onec83_apply_document_queue", "stage": "queue",
            "elapsed_ms": 82, "odata_calls": 1, "odata_attempts": 2,
            "outcome": "needs_reconciliation", "error_category": "tls",
            "row_token": canary,
        },
        {
            "event": "mcp_call", "run_id": "c" * 32, "call_id": "d" * 32,
            "tool": "onec83_" + canary, "stage": canary,
            "elapsed_ms": 1, "outcome": "success",
        },
        {
            "event": "stage", "run_id": "c" * 32, "call_id": "d" * 32,
            "tool": "onec83_apply_document_queue", "stage": canary,
            "elapsed_ms": 2, "odata_calls": 0, "odata_attempts": 0,
            "outcome": "success",
        },
    ]
    log_path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")

    result = summarize(log_path)
    summary_text = json.dumps(result, ensure_ascii=False)
    assert canary not in summary_text
    assert "real-row-value" not in summary_text
    assert result["event_count"] == 6
    assert result["run_count"] == 2
    assert result["runs"][0]["run_index"] == 1
    assert result["runs"][0]["tool_calls"]["onec83_apply_document_queue"] == 1
    assert "a" * 32 not in summary_text
    assert result["tools"]["onec83_apply_document_queue"]["elapsed_ms"]["p95_ms"] == 120
    assert result["tools"]["other"]["calls"] == 1
    assert result["stages"]["other"]["elapsed_ms"]["count"] == 1
    assert result["odata_attempts"]["count"] == 1
    assert result["odata_attempts"]["http_statuses_by_stage"]["create"] == {"503": 1}
    assert result["queue"]["rows_submitted"] == 2
    assert result["queue"]["row_outcomes"]["needs_reconciliation"] == 1
    assert result["queue"]["reported_row_counts"]["rows_succeeded"] == 1
    assert result["queue"]["reported_row_counts"]["rows_uncertain"] == 1
    assert result["queue"]["row_error_categories"]["tls"] == 1


def test_summary_attributes_odata_attempts_by_allowlisted_tool_without_leaking_fields(
    tmp_path,
):
    log_path = tmp_path / "attempt-attribution.jsonl"
    secret = "PRIVATE-CANARY-unrecognized-tool-and-field"
    events = [
        {
            "event": "odata_attempt", "tool": "onec83_search_entities",
            "stage": "entity_read", "elapsed_ms": 10,
            "http_status": 200,
        },
        {
            "event": "odata_attempt", "tool": "onec83_query_entities",
            "stage": "entity_read", "elapsed_ms": 30,
            "http_status": 503, "error_category": "http_5xx",
        },
        {
            "event": "odata_attempt", "tool": "onec83_" + secret,
            "stage": "entity_read", "elapsed_ms": 5,
            "http_status": 429, "request_url": secret,
            "call_id": secret, "raw_request": {"payload": secret},
        },
        {
            "event": "odata_attempt", "tool": 17,
            "stage": "entity_read", "elapsed_ms": 7,
            "http_status": 400,
        },
        {
            "event": "odata_attempt", "stage": "entity_read",
            "elapsed_ms": 11, "http_status": 201,
        },
    ]
    log_path.write_text(
        "\n".join(json.dumps(event) for event in events) + "\n",
        encoding="utf-8",
    )

    result = summarize(log_path)
    summary_text = json.dumps(result, ensure_ascii=False)
    attempts = result["odata_attempts"]
    by_tool = attempts["by_tool"]

    assert attempts["count"] == 5
    assert attempts["elapsed_ms_by_stage"]["entity_read"]["count"] == 5
    assert attempts["elapsed_ms_by_stage"]["entity_read"]["total_ms"] == 63
    assert attempts["http_statuses_by_stage"]["entity_read"] == {
        "200": 1, "201": 1, "400": 1, "429": 1, "503": 1,
    }
    assert by_tool["onec83_search_entities"] == {
        "count": 1,
        "elapsed_ms_by_stage": {
            "entity_read": {
                "count": 1, "total_ms": 10, "mean_ms": 10,
                "p50_ms": 10, "p95_ms": 10,
            },
        },
        "http_statuses_by_stage": {"entity_read": {"200": 1}},
        "error_categories_by_stage": {},
    }
    assert by_tool["onec83_query_entities"]["count"] == 1
    assert by_tool["onec83_query_entities"]["elapsed_ms_by_stage"]["entity_read"]["total_ms"] == 30
    assert by_tool["onec83_query_entities"]["http_statuses_by_stage"] == {
        "entity_read": {"503": 1},
    }
    assert by_tool["onec83_query_entities"]["error_categories_by_stage"] == {
        "entity_read": {"http_5xx": 1},
    }
    assert by_tool["other"]["count"] == 3
    assert by_tool["other"]["http_statuses_by_stage"]["entity_read"] == {
        "201": 1, "400": 1, "429": 1,
    }
    assert secret not in summary_text
    assert "request_url" not in summary_text
    assert "raw_request" not in summary_text
    assert "call_id" not in summary_text


def test_search_profile_logging_and_summary_are_fixed_and_content_free(tmp_path):
    log_path = tmp_path / "search-profiles.jsonl"
    logger = PerformanceLogger(enabled=True, path=log_path)
    canary = "PRIVATE-CANARY-field-value-guid-url"

    with instrument_tool_call("onec83_search_entities", logger):
        set_search_profile("date_range", "fallback_scan")
        increment_search_count("fallback_page_reads")
        increment_search_count("date_lower_bound_probes")
        record_request_call()
        for attempt in (1, 2, 3):
            record_odata_attempt(
                stage=None, elapsed_ms=12, attempt=attempt,
                status_code=500 if attempt < 3 else 200,
            )

    # Unknown classification and counter keys are ignored by both logger and summary.
    with instrument_tool_call("onec83_search_entities", logger) as call:
        set_search_profile(canary, canary)
        increment_search_count(canary)
        record_request_call()

    raw = log_path.read_text(encoding="utf-8")
    assert canary not in raw
    events = [json.loads(line) for line in raw.splitlines()]
    search_calls = [event for event in events if event["event"] == "mcp_call"]
    assert search_calls[0]["search_class"] == "date_range"
    assert search_calls[0]["search_strategy"] == "fallback_scan"
    assert search_calls[0]["fallback_page_reads"] == 1
    assert search_calls[0]["date_lower_bound_probes"] == 1
    assert "search_class" not in search_calls[1]

    result = summarize(log_path)
    assert result["search_profiles"]["date_range.fallback_scan"] == {
        "calls": 1,
        "elapsed_ms": result["search_profiles"]["date_range.fallback_scan"]["elapsed_ms"],
        "outcomes": {"success": 1},
        "date_lower_bound_probes": 1,
        "fallback_page_reads": 1,
        "odata_attempts": 3,
        "odata_calls": 1,
    }
    summary_text = json.dumps(result, ensure_ascii=False)
    assert canary not in summary_text
    assert "call_id" not in summary_text
    assert "request_url" not in summary_text


def test_batch_search_is_visible_in_content_free_search_summary(tmp_path):
    log_path = tmp_path / "batch-search-profile.jsonl"
    logger = PerformanceLogger(enabled=True, path=log_path)

    with instrument_tool_call("onec83_search_entities_batch", logger):
        set_search_profile("string_equality", "fallback_scan_cache_hit")
        increment_search_count("fallback_page_reads")
        record_request_call()
        record_odata_attempt(
            stage=None, elapsed_ms=12, attempt=1, status_code=200,
        )

    raw = log_path.read_text(encoding="utf-8")
    events = [json.loads(line) for line in raw.splitlines()]
    call = next(event for event in events if event["event"] == "mcp_call")
    assert call["tool"] == "onec83_search_entities_batch"
    assert call["stage"] == "entity_lookup"
    assert call["search_class"] == "string_equality"
    assert call["search_strategy"] == "fallback_scan_cache_hit"
    assert call["fallback_page_reads"] == 1

    summary = summarize(log_path)
    assert summary["tools"]["onec83_search_entities_batch"]["calls"] == 1
    assert summary["search_profiles"]["string_equality.fallback_scan_cache_hit"] == {
        "calls": 1,
        "elapsed_ms": summary["search_profiles"]["string_equality.fallback_scan_cache_hit"]["elapsed_ms"],
        "outcomes": {"success": 1},
        "date_lower_bound_probes": 0,
        "fallback_page_reads": 1,
        "odata_attempts": 1,
        "odata_calls": 1,
    }


def test_readback_exact_ref_key_stage_has_fixed_allowlisted_name(tmp_path):
    log_path = tmp_path / "readback-stage.jsonl"
    logger = PerformanceLogger(enabled=True, path=log_path)
    with instrument_tool_call("onec83_get_entity", logger):
        with diagnostic_stage("readback_404_ref_key_lookup"):
            record_request_call()
            record_odata_attempt(stage=None, elapsed_ms=3, attempt=1, status_code=200)
    result = summarize(log_path)
    assert result["stages"]["readback_404_ref_key_lookup"]["elapsed_ms"]["count"] == 1
    assert "other" not in result["stages"]


def test_queue_summary_breaks_totals_out_by_normalized_outcome(tmp_path):
    log_path = tmp_path / "queue-outcomes.jsonl"
    events = [
        {
            "event": "mcp_call", "tool": "onec83_apply_document_queue",
            "outcome": "dry_run", "wave_size": 2, "concurrency": 1,
            "rows_succeeded": 0, "rows_failed": 0, "rows_uncertain": 0,
        },
        {
            "event": "mcp_call", "tool": "onec83_apply_document_queue",
            "outcome": "success", "wave_size": 2, "concurrency": 2,
            "rows_succeeded": 2, "rows_failed": 0, "rows_uncertain": 0,
        },
        {
            "event": "mcp_call", "tool": "onec83_apply_document_queue",
            "outcome": "owner-specific-secret", "concurrency": 4,
        },
        {
            "event": "queue_row", "outcome": "success",
        },
    ]
    log_path.write_text(
        "\n".join(json.dumps(event) for event in events) + "\n",
        encoding="utf-8",
    )

    result = summarize(log_path)
    queue = result["queue"]

    assert queue["waves"] == 2
    assert queue["rows_submitted"] == 4
    assert queue["max_wave_size"] == 2
    assert queue["max_concurrency"] == 4
    assert queue["reported_row_counts"] == {
        "rows_failed": 0, "rows_succeeded": 2, "rows_uncertain": 0,
    }
    assert queue["row_outcomes"] == {"success": 1}
    assert queue["by_outcome"] == {
        "dry_run": {
            "waves": 1, "rows_submitted": 2, "max_wave_size": 2,
            "max_concurrency": 1,
            "reported_row_counts": {
                "rows_failed": 0, "rows_succeeded": 0, "rows_uncertain": 0,
            },
        },
        "success": {
            "waves": 1, "rows_submitted": 2, "max_wave_size": 2,
            "max_concurrency": 2,
            "reported_row_counts": {
                "rows_failed": 0, "rows_succeeded": 2, "rows_uncertain": 0,
            },
        },
        "unknown": {
            "waves": 0, "rows_submitted": 0, "max_wave_size": 0,
            "max_concurrency": 4, "reported_row_counts": {},
        },
    }
    assert "owner-specific-secret" not in json.dumps(queue)


def test_transport_subreasons_are_allowlisted_and_aggregated_without_sensitive_fields(tmp_path):
    log_path = tmp_path / "transport-subreasons.jsonl"
    logger = PerformanceLogger(enabled=True, path=log_path)
    secret = "PRIVATE-CANARY-secret-url-payload-company-guid"
    allowed = sorted(TRANSPORT_SUBREASONS)

    with instrument_tool_call("onec83_get_entity", logger):
        for index, subreason in enumerate(allowed, start=1):
            record_odata_attempt(
                stage="entity_read",
                elapsed_ms=index,
                attempt=1,
                error_category="transport",
                transport_subreason=subreason,
            )
        for invalid in (secret, [secret], {"value": secret}, "owner_specific_secret"):
            record_odata_attempt(
                stage="entity_read",
                elapsed_ms=1,
                attempt=1,
                error_category="transport",
                transport_subreason=invalid,
            )
        record_odata_attempt(
            stage="entity_read",
            elapsed_ms=1,
            attempt=1,
            status_code=503,
            transport_subreason="timeout_read",
        )

    contents = log_path.read_text(encoding="utf-8")
    assert secret not in contents
    events = [json.loads(line) for line in contents.splitlines()]
    attempts = [event for event in events if event["event"] == "odata_attempt"]
    assert [event["transport_subreason"] for event in attempts if "transport_subreason" in event] == allowed
    assert "transport_subreason" not in attempts[-1]
    for event in attempts:
        assert not ({"url", "request_url", "payload", "row_token"} & event.keys())
        assert event["call_id"] and event["run_id"]

    result = summarize(log_path)
    summary = result["odata_attempts"]
    expected = {value: 1 for value in allowed}
    assert summary["transport_subreasons_by_stage"] == {"entity_read": expected}
    tool_summary = summary["by_tool"]["onec83_get_entity"]
    assert tool_summary["transport_subreasons_by_stage"] == {"entity_read": expected}
    assert secret not in json.dumps(result, ensure_ascii=False)
    assert "call_id" not in json.dumps(result)


def test_summary_drops_malformed_transport_subreasons_and_http_status_subreasons(tmp_path):
    log_path = tmp_path / "malformed-transport-subreasons.jsonl"
    secret = "PRIVATE-CANARY-url-payload-guid"
    events = [
        {"event": "odata_attempt", "tool": "onec83_get_entity", "stage": "entity_read",
         "transport_subreason": "connection_reset"},
        {"event": "odata_attempt", "tool": "onec83_get_entity", "stage": "entity_read",
         "http_status": 503, "transport_subreason": "timeout_read"},
        {"event": "odata_attempt", "tool": "onec83_get_entity", "stage": "entity_read",
         "http_status": "503", "transport_subreason": "timeout_read"},
        {"event": "odata_attempt", "tool": "onec83_get_entity", "stage": "entity_read",
         "http_status": True, "transport_subreason": "timeout_read"},
        {"event": "odata_attempt", "tool": "onec83_get_entity", "stage": "entity_read",
         "transport_subreason": secret},
        {"event": "odata_attempt", "tool": "onec83_get_entity", "stage": "entity_read",
         "transport_subreason": [secret]},
        {"event": "odata_attempt", "tool": "onec83_get_entity", "stage": "entity_read",
         "transport_subreason": {"value": secret}},
    ]
    log_path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")

    result = summarize(log_path)
    expected = {"entity_read": {"connection_reset": 1}}
    assert result["odata_attempts"]["transport_subreasons_by_stage"] == expected
    assert result["odata_attempts"]["by_tool"]["onec83_get_entity"][
        "transport_subreasons_by_stage"
    ] == expected
    assert secret not in json.dumps(result, ensure_ascii=False)
