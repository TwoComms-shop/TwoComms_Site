"""Token-free Gemini model-resource readiness checks."""
from __future__ import annotations

import datetime as dt
import json
import socket
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, wait
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from django.db import transaction
from django.utils import timezone

from management.services import gemini_keys
from management.services import gemini_model_registry

MODELS = gemini_model_registry.DISPLAY_MODELS
ROLE = "health_metadata"
BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
TIMEOUT_SECONDS = 5
CHECK_DEADLINE_SECONDS = 70
MAX_METADATA_BYTES = 16 * 1024

def _status_for_http(code: int) -> str:
    known = {
        401: "unauthorized",
        403: "forbidden",
        404: "not_found",
        408: "http_408",
        429: "rate_limited",
    }
    if code in known:
        return known[code]
    if 500 <= code <= 599:
        return "http_5xx"
    return "http_error"

def _supports_generate_content(response) -> bool:
    try:
        raw = response.read(MAX_METADATA_BYTES + 1)
        if not isinstance(raw, bytes) or len(raw) > MAX_METADATA_BYTES:
            return False
        payload = json.loads(raw.decode("utf-8"))
    except (AttributeError, UnicodeDecodeError, json.JSONDecodeError, OSError):
        return False
    methods = payload.get("supportedGenerationMethods") if isinstance(payload, dict) else None
    return isinstance(methods, list) and "generateContent" in methods

def _record(
    *,
    request_id: str,
    alias: str,
    model: str,
    status: str,
    latency_ms: int = 0,
    http_code: int | None = None,
) -> None:
    succeeded = status == "metadata_ok"
    skipped = status == "not_needed"
    gemini_keys.record_attempt(
        request_id=request_id,
        role=ROLE,
        key_name=alias,
        model=model,
        outcome="succeeded" if succeeded else ("skipped" if skipped else "failed"),
        failure_kind="" if succeeded else status,
        http_code=http_code,
        decision="skipped_primary_ok" if skipped else "metadata_only",
        latency_ms=latency_ms,
    )

def _check_model(
    alias: str,
    api_key: str,
    model: str,
    request_id: str,
    *,
    deadline: float | None = None,
    record: bool = True,
) -> dict:
    started = time.monotonic()
    if deadline is not None and time.monotonic() >= deadline:
        return {
            "model": model,
            "status": "deadline_skipped",
            "http_code": None,
            "latency_ms": 0,
            "evidence_kind": "metadata_only",
            "generation_quota_proven": False,
        }
    request = Request(f"{BASE_URL}/models/{model}", headers={"X-goog-api-key": api_key, "User-Agent": "TwoComms-Gemini-Health/1.0"}, method="GET")
    code = None
    timeout = TIMEOUT_SECONDS
    if deadline is not None:
        timeout = min(timeout, max(0.1, deadline - time.monotonic()))
    try:
        with urlopen(request, timeout=timeout) as response:
            code = int(getattr(response, "status", 200) or 200)
            supports_generation = code == 200 and _supports_generate_content(response)
        status = "metadata_ok" if supports_generation else (
            "unsupported_generation" if code == 200 else _status_for_http(code)
        )
    except HTTPError as exc:
        code = int(exc.code or 0) or None
        status = _status_for_http(code or 0)
    except (TimeoutError, socket.timeout):
        status = "timeout"
    except URLError as exc:
        status = (
            "timeout"
            if isinstance(getattr(exc, "reason", None), (TimeoutError, socket.timeout))
            else "transport_error"
        )
    except OSError:
        status = "transport_error"
    latency_ms = max(0, round((time.monotonic() - started) * 1000))
    if record:
        _record(
            request_id=request_id,
            alias=alias,
            model=model,
            status=status,
            latency_ms=latency_ms,
            http_code=code,
        )
    return {"model": model, "status": status, "http_code": code, "latency_ms": latency_ms, "evidence_kind": "metadata_only", "generation_quota_proven": False}

def check_alias(
    alias: str,
    *,
    now: dt.datetime | None = None,
    request_id: str | None = None,
    deadline: float | None = None,
    record: bool = True,
    model_names: tuple[str, ...] | list[str] | None = None,
) -> list[dict]:
    now = now or timezone.now()
    request_id = request_id or f"meta-{now:%Y%m%d%H}-{uuid.uuid4().hex[:8]}"
    api_key = gemini_keys._key_value(alias)
    legacy_short_probe = model_names is None
    selected_models = tuple(model_names or MODELS[:2])
    if not api_key:
        return [{"model": model, "status": "unconfigured", "evidence_kind": "metadata_only", "generation_quota_proven": False} for model in selected_models]
    primary = _check_model(
        alias,
        api_key,
        selected_models[0],
        request_id,
        deadline=deadline,
        record=record,
    )
    if legacy_short_probe and primary["status"] == "metadata_ok" and len(selected_models) > 1:
        secondary = {
            "model": selected_models[1],
            "status": "not_needed",
            "evidence_kind": "metadata_only",
            "generation_quota_proven": False,
        }
        if record:
            _record(
                request_id=request_id,
                alias=alias,
                model=selected_models[1],
                status="not_needed",
            )
        return [primary, secondary]
    return [primary] + [
        _check_model(
            alias,
            api_key,
            model,
            request_id,
            deadline=deadline,
            record=record,
        )
        for model in selected_models[1:]
    ]


def _deadline_results() -> list[dict]:
    return [
        {
            "model": model,
            "status": "deadline_skipped",
            "http_code": None,
            "latency_ms": 0,
            "evidence_kind": "metadata_only",
            "generation_quota_proven": False,
        }
        for model in MODELS
    ]


def _record_alias_results(
    *,
    alias: str,
    request_id: str,
    results: list[dict],
) -> None:
    """Write finished alias observations from the coordinator thread only."""
    for result in results:
        status = str(result.get("status") or "")
        if status in {"deadline_skipped", "unconfigured"}:
            continue
        _record(
            request_id=request_id,
            alias=alias,
            model=str(result.get("model") or ""),
            status=status,
            latency_ms=int(result.get("latency_ms") or 0),
            http_code=result.get("http_code"),
        )


def _run_alias_check_worker(
    alias: str,
    *,
    now: dt.datetime,
    request_id: str,
    deadline: float,
) -> tuple[list[dict], float]:
    """Run one provider read and timestamp completion in its worker thread."""
    result = check_alias(
        alias,
        now=now,
        request_id=request_id,
        deadline=deadline,
        record=False,
        model_names=MODELS,
    )
    return result, time.monotonic()


def _run_alias_checks(
    *,
    batch_id: str,
    now: dt.datetime,
    deadline: float,
) -> list[list[dict]]:
    """Run provider reads concurrently; persist their facts on this thread."""
    aliases = list(gemini_keys.ALL_KEYS)
    if not aliases:
        return []
    executor = ThreadPoolExecutor(
        max_workers=len(aliases),
        thread_name_prefix="gemini-health",
    )
    futures = {}
    finished: dict[str, list[dict]] = {}
    failures: dict[str, Exception] = {}
    timed_out: set[str] = set()
    try:
        for alias in aliases:
            futures[alias] = executor.submit(
                _run_alias_check_worker,
                alias,
                now=now,
                request_id=f"{batch_id}-{alias[-1]}",
                deadline=deadline,
            )
        # Observe all workers as one batch. Never block on futures in
        # canonical alias order: a slow first key must not hide later keys.
        done, _ = wait(
            futures.values(),
            timeout=max(0.0, deadline - time.monotonic()),
        )
        for alias, future in futures.items():
            if future not in done:
                timed_out.add(alias)
                continue
            try:
                payload = future.result()
            except Exception as error:
                failures[alias] = error
                continue
            result, finished_at = payload
            # The coordinator's deadline is a logical evidence boundary. A
            # worker that finishes after it is joined is still not evidence.
            if finished_at <= deadline:
                finished[alias] = result
            else:
                timed_out.add(alias)
    finally:
        # Retain command ownership until every worker is reconciled. Active
        # slow-drip reads remain the documented IMP-044 limitation.
        executor.shutdown(wait=True, cancel_futures=True)

    # Inspect all joined futures so an exception cannot be hidden by a prior
    # deadline branch.
    for alias, future in futures.items():
        if alias in failures or not future.done():
            continue
        error = future.exception()
        if error is not None:
            failures[alias] = error

    if failures:
        aliases_with_failures = ", ".join(
            f"{alias} ({type(failures[alias]).__name__})"
            for alias in aliases
            if alias in failures
        )
        raise RuntimeError(
            f"Gemini metadata health worker failed for aliases: {aliases_with_failures}"
        ) from None

    results = [
        _deadline_results() if alias in timed_out else finished.get(alias, _deadline_results())
        for alias in aliases
    ]
    # Provider reads are complete; commit their coordinated snapshot together
    # so a transient MariaDB write error cannot expose a partial hourly batch.
    with transaction.atomic():
        for alias, result in zip(aliases, results, strict=True):
            _record_alias_results(
                alias=alias,
                request_id=f"{batch_id}-{alias[-1]}",
                results=result,
            )
    return results


def run_hour(*, now: dt.datetime | None = None) -> dict:
    now = now or timezone.now()
    batch_id = f"meta-{now:%Y%m%d%H}-{uuid.uuid4().hex[:8]}"
    deadline = time.monotonic() + CHECK_DEADLINE_SECONDS
    alias_results = _run_alias_checks(
        batch_id=batch_id,
        now=now,
        deadline=deadline,
    )
    statuses = [
        str(model_result.get("status") or "")
        for result in alias_results
        for model_result in result
    ]
    no_provider_statuses = {"deadline_skipped", "not_needed", "unconfigured"}
    return {
        "request_id": batch_id,
        "checked_aliases": len(alias_results),
        "configured_aliases": sum(
            1
            for result in alias_results
            if any(row.get("status") != "unconfigured" for row in result)
        ),
        "provider_requests": sum(
            1 for status in statuses if status not in no_provider_statuses
        ),
        "deadline_skipped_models": statuses.count("deadline_skipped"),
    }
