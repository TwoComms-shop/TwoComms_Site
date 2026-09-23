"""Single source of truth for supported Gemini model capabilities.

The provider model id, task suitability and capability boundaries must not be
redeclared independently by the router, quota ledger and management cockpit.
This registry contains no credentials and is safe to use in read-only APIs.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelCapability:
    model: str
    label: str
    tier: str
    free_quota: bool
    supports_text: bool
    supports_image: bool
    supports_video: bool
    supports_audio: bool
    supports_structured_output: bool
    supported_reasoning: tuple[str, ...]


# Display order is also the quality order in the management cockpit. Historical
# model ids stay present so old requests/telemetry remain readable.
MODEL_CAPABILITIES: tuple[ModelCapability, ...] = (
    ModelCapability(
        "gemini-3.8-flash", "Gemini 3.8 Flash", "strong", True,
        True, True, True, True, True, ("low", "medium", "high"),
    ),
    ModelCapability(
        "gemini-3.7-flash", "Gemini 3.7 Flash", "strong", True,
        True, True, True, True, True, ("low", "medium", "high"),
    ),
    ModelCapability(
        "gemini-3.6-flash", "Gemini 3.6 Flash", "analysis", True,
        True, True, True, True, True, ("low", "medium", "high"),
    ),
    ModelCapability(
        "gemini-3.5-flash", "Gemini 3.5 Flash", "spillover", True,
        True, True, True, True, True, ("low", "medium", "high"),
    ),
    ModelCapability(
        "gemini-3.5-flash-lite", "Gemini 3.5 Flash Lite", "lite", True,
        True, True, True, False, True, ("low", "medium", "high"),
    ),
    # Legacy free models remain registered for grounded/checker and historical
    # fallback callers. They are intentionally omitted from DISPLAY_MODELS so
    # the operator cockpit shows the five supported chat profiles only.
    ModelCapability(
        "gemini-3.1-flash-lite", "Gemini 3.1 Flash Lite", "legacy", True,
        True, True, True, True, True, ("low", "medium", "high"),
    ),
    ModelCapability(
        "gemini-3.1-flash-lite-preview", "Gemini 3.1 Flash Lite Preview", "legacy", True,
        True, True, True, True, True, ("low", "medium", "high"),
    ),
    ModelCapability(
        "gemini-2.5-flash", "Gemini 2.5 Flash", "grounded", True,
        True, True, True, True, True, ("low", "medium", "high"),
    ),
    ModelCapability(
        "gemini-2.5-flash-lite", "Gemini 2.5 Flash Lite", "grounded", True,
        True, True, True, True, True, ("low", "medium", "high"),
    ),
)

CAPABILITY_BY_MODEL = {item.model: item for item in MODEL_CAPABILITIES}
DISPLAY_MODELS = tuple(
    item.model for item in MODEL_CAPABILITIES
    if item.tier in {"strong", "analysis", "spillover", "lite"}
)
FREE_QUOTA_MODELS = frozenset(
    item.model for item in MODEL_CAPABILITIES if item.free_quota
)
CHAT_MODEL_ALLOWLIST = frozenset(DISPLAY_MODELS)


def capability_for(model: str | None) -> ModelCapability | None:
    return CAPABILITY_BY_MODEL.get(str(model or "").strip())


def supports(model: str | None, feature: str) -> bool:
    capability = capability_for(model)
    return bool(capability and getattr(capability, f"supports_{feature}", False))


def supports_reasoning(model: str | None, level: str) -> bool:
    capability = capability_for(model)
    return bool(capability and str(level or "").strip() in capability.supported_reasoning)
