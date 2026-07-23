"""Redacted, non-customer Gemini connectivity probes."""
from __future__ import annotations

import json
import time

import requests

from management.services.call_ai_analysis import GENAI_BASE, _payload_for_model

PROBE_TIMEOUT = (5, 20)
PROBE_OUTPUT_TOKENS = 128


def build_probe_payload(model: str) -> dict:
    payload = {
        "contents": [{"role": "user", "parts": [{"text": "Reply exactly OK."}]}],
        "generationConfig": {
            "temperature": 0,
            "maxOutputTokens": PROBE_OUTPUT_TOKENS,
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }
    return _payload_for_model(model, payload, reasoning_task="health_probe")


def _usage_value(usage: dict, *names: str) -> int:
    for name in names:
        value = usage.get(name)
        if isinstance(value, int):
            return value
    return 0


def classify_probe_response(http_code: int, body: str) -> dict:
    """Return bounded status data; never include provider text or credentials."""
    if http_code != 200:
        status = {
            403: "forbidden",
            404: "model_unavailable",
            429: "quota",
        }.get(http_code, "provider_error" if http_code >= 500 else "request_error")
        return {"status": status, "http_code": http_code, "finish_reason": "", "thoughts_tokens": 0,
                "candidates_tokens": 0}
    try:
        data = json.loads(body or "{}")
    except (TypeError, ValueError):
        return {"status": "malformed_response", "http_code": http_code, "finish_reason": "",
                "thoughts_tokens": 0, "candidates_tokens": 0}
    if not isinstance(data, dict):
        return {"status": "malformed_response", "http_code": http_code, "finish_reason": "",
                "thoughts_tokens": 0, "candidates_tokens": 0}
    malformed = False
    candidates = data.get("candidates", [])
    if candidates is None:
        candidates = []
    elif not isinstance(candidates, list):
        malformed = True
        candidates = []
    candidate = candidates[0] if candidates and isinstance(candidates[0], dict) else {}
    reason = str(candidate.get("finishReason") or "")[:32]
    content = candidate.get("content", {})
    if content is None:
        content = {}
    elif not isinstance(content, dict):
        malformed = True
        content = {}
    parts = content.get("parts", [])
    if parts is None:
        parts = []
    elif not isinstance(parts, list):
        malformed = True
        parts = []
    text = "".join(
        str(part.get("text") or "")
        for part in parts
        if isinstance(part, dict) and not part.get("thought")
    ).strip()
    usage = data.get("usageMetadata", {})
    if usage is None:
        usage = {}
    elif not isinstance(usage, dict):
        malformed = True
        usage = {}
    result = {
        "status": "malformed_response" if malformed else ("ok" if text else "reachable_empty"),
        "http_code": http_code,
        "finish_reason": reason,
        "thoughts_tokens": _usage_value(usage, "thoughtsTokenCount", "thoughts_token_count"),
        "candidates_tokens": _usage_value(usage, "candidatesTokenCount", "candidates_token_count"),
    }
    if reason == "SAFETY" or (isinstance(data.get("promptFeedback"), dict) and (data.get("promptFeedback") or {}).get("blockReason")):
        result["status"] = "blocked"
    elif reason == "MAX_TOKENS":
        result["status"] = "reachable_degraded"
    return result


def probe_key(model: str, key: str, timeout: tuple | None = None) -> dict:
    started = time.monotonic()
    try:
        response = requests.post(
            f"{GENAI_BASE}/models/{model}:generateContent",
            data=json.dumps(build_probe_payload(model)),
            headers={"Content-Type": "application/json", "x-goog-api-key": key},
            timeout=timeout or PROBE_TIMEOUT,
        )
        result = classify_probe_response(response.status_code, response.text)
    except requests.Timeout:
        result = {"status": "timeout", "http_code": 0, "finish_reason": "", "thoughts_tokens": 0,
                  "candidates_tokens": 0}
    except requests.RequestException:
        result = {"status": "transport_error", "http_code": 0, "finish_reason": "", "thoughts_tokens": 0,
                  "candidates_tokens": 0}
    result["latency_ms"] = max(0, int((time.monotonic() - started) * 1000))
    result["model"] = model
    return result
