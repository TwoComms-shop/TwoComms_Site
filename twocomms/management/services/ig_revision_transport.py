"""Exact payload planning and one-request transport for revision effects."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Callable, Mapping
from urllib.parse import urlsplit

from django.db import connection

from management.services.ig_delivery_plan import DeliveryPlan, build_delivery_plan
from management.services.ig_revision_delivery import ProviderPartResult


MAX_PROVIDER_PAYLOAD_BYTES = 256 * 1024
MAX_RECIPIENT_CHARS = 64
MAX_IMAGE_URL_CHARS = 4096


@dataclass(frozen=True)
class PreparedRevisionText:
    effects: tuple[dict, ...] = ()
    delivery_plan: DeliveryPlan | None = None
    degraded_fields: tuple[str, ...] = field(default_factory=tuple)
    error: str = ""


def _bounded_text(value, *, maximum: int, byte_limit: int | None = None) -> str:
    text = str(value or "")
    if (
        not text
        or len(text) > maximum
        or any(ord(char) < 32 and char not in "\n\r\t" for char in text)
        or (byte_limit is not None and len(text.encode("utf-8")) > byte_limit)
    ):
        return ""
    return text


def _recipient(value) -> str:
    text = _bounded_text(value, maximum=MAX_RECIPIENT_CHARS)
    return text if text and text.strip() == text else ""


def prepare_text_effects(
    recipient_id: str,
    text: str,
    *,
    quick_replies=(),
    provider_namespace: str = "",
    limit: int = 950,
    max_chunks: int = 4,
) -> PreparedRevisionText:
    """Split once and return exact outbox effect specs for every text part."""
    recipient = _recipient(recipient_id)
    if not recipient:
        return PreparedRevisionText(error="recipient_invalid")
    plan = build_delivery_plan(text, limit=limit, max_chunks=max_chunks)
    if not plan.deliverable:
        return PreparedRevisionText(delivery_plan=plan, error=plan.reason)

    normalized_quick_replies = None
    degraded_fields: tuple[str, ...] = ()
    if quick_replies:
        from management.services.ig_message_templates import (
            QuickReplyMessage,
            TemplateValidationError,
            normalize_quick_reply_message,
            quick_reply_message_payload,
        )

        try:
            normalized_quick_replies = normalize_quick_reply_message(
                QuickReplyMessage(plan.chunks[-1], tuple(quick_replies))
            )
        except TemplateValidationError:
            return PreparedRevisionText(
                delivery_plan=plan, error="quick_replies_invalid"
            )
        degraded_fields = normalized_quick_replies.degraded_fields

    legacy = str(provider_namespace or "").startswith("legacy_page:")
    effects = []
    for index, chunk in enumerate(plan.chunks):
        message = {"text": chunk}
        if normalized_quick_replies is not None and index == len(plan.chunks) - 1:
            message = quick_reply_message_payload(normalized_quick_replies)
        payload = {"recipient": {"id": recipient}, "message": message}
        if legacy:
            payload["messaging_type"] = "RESPONSE"
        effects.append({
            "group": "substantive_text",
            "kind": "text",
            "payload": payload,
        })
    return PreparedRevisionText(
        effects=tuple(effects),
        delivery_plan=plan,
        degraded_fields=degraded_fields,
    )


def _https_url(value) -> bool:
    raw = str(value or "")
    if not raw or len(raw) > MAX_IMAGE_URL_CHARS:
        return False
    try:
        parsed = urlsplit(raw)
        return bool(
            parsed.scheme == "https"
            and parsed.hostname
            and not parsed.username
            and not parsed.password
            and not parsed.fragment
        )
    except (TypeError, ValueError):
        return False


def _valid_quick_replies(value) -> bool:
    if value is None:
        return True
    if not isinstance(value, list) or not 1 <= len(value) <= 13:
        return False
    for item in value:
        if not isinstance(item, Mapping) or set(item) != {
            "content_type", "title", "payload"
        }:
            return False
        if item.get("content_type") != "text":
            return False
        if not _bounded_text(item.get("title"), maximum=20):
            return False
        if not _bounded_text(item.get("payload"), maximum=1000):
            return False
    return True


def _valid_button(value) -> bool:
    if not isinstance(value, Mapping):
        return False
    kind = value.get("type")
    if kind == "postback":
        return (
            set(value) == {"type", "title", "payload"}
            and bool(_bounded_text(value.get("title"), maximum=20))
            and bool(_bounded_text(value.get("payload"), maximum=1000))
        )
    if kind == "web_url":
        return (
            set(value) == {"type", "title", "url"}
            and bool(_bounded_text(value.get("title"), maximum=20))
            and _https_url(value.get("url"))
        )
    return False


def _valid_generic_element(value) -> bool:
    if not isinstance(value, Mapping):
        return False
    allowed = {"title", "subtitle", "image_url", "default_action", "buttons"}
    if not set(value).issubset(allowed):
        return False
    if not _bounded_text(value.get("title"), maximum=80):
        return False
    subtitle = value.get("subtitle")
    if subtitle is not None and not _bounded_text(subtitle, maximum=80):
        return False
    image_url = value.get("image_url")
    if image_url is not None and not _https_url(image_url):
        return False
    default_action = value.get("default_action")
    if default_action is not None and not (
        isinstance(default_action, Mapping)
        and set(default_action) == {"type", "url"}
        and default_action.get("type") == "web_url"
        and _https_url(default_action.get("url"))
    ):
        return False
    buttons = value.get("buttons")
    if buttons is not None and not (
        isinstance(buttons, list)
        and 1 <= len(buttons) <= 3
        and all(_valid_button(button) for button in buttons)
    ):
        return False
    return bool(subtitle or image_url or default_action or buttons)


def _valid_template_payload(value) -> bool:
    if not isinstance(value, Mapping):
        return False
    template_type = value.get("template_type")
    if template_type == "generic":
        if set(value) != {"template_type", "elements"}:
            return False
        elements = value.get("elements")
        return bool(
            isinstance(elements, list)
            and 1 <= len(elements) <= 10
            and all(_valid_generic_element(item) for item in elements)
        )
    if template_type == "button":
        if set(value) != {"template_type", "text", "buttons"}:
            return False
        buttons = value.get("buttons")
        return bool(
            _bounded_text(value.get("text"), maximum=640)
            and isinstance(buttons, list)
            and 1 <= len(buttons) <= 3
            and all(_valid_button(button) for button in buttons)
        )
    return False


def _payload_kind(payload: Mapping, *, expected_recipient: str) -> str:
    if not isinstance(payload, Mapping):
        return ""
    if not set(payload).issubset({"recipient", "message", "messaging_type"}):
        return ""
    if payload.get("messaging_type") not in (None, "RESPONSE"):
        return ""
    recipient = payload.get("recipient")
    if (
        not isinstance(recipient, Mapping)
        or set(recipient) != {"id"}
        or _recipient(recipient.get("id")) != expected_recipient
    ):
        return ""
    message = payload.get("message")
    if not isinstance(message, Mapping):
        return ""
    quick_replies = message.get("quick_replies")
    if not _valid_quick_replies(quick_replies):
        return ""
    if "text" in message:
        if not set(message).issubset({"text", "quick_replies"}):
            return ""
        return "text" if _bounded_text(
            message.get("text"), maximum=4000, byte_limit=1000
        ) else ""
    if set(message).difference({"attachment", "quick_replies"}):
        return ""
    attachment = message.get("attachment")
    if not isinstance(attachment, Mapping) or set(attachment) != {"type", "payload"}:
        return ""
    if attachment.get("type") == "image":
        image_payload = attachment.get("payload")
        return "image" if (
            isinstance(image_payload, Mapping)
            and set(image_payload).issubset({"url", "is_reusable"})
            and "url" in image_payload
            and _https_url(image_payload.get("url"))
            and (
                "is_reusable" not in image_payload
                or isinstance(image_payload.get("is_reusable"), bool)
            )
            and quick_replies is None
        ) else ""
    if attachment.get("type") == "template":
        return "template" if _valid_template_payload(
            attachment.get("payload")
        ) else ""
    return ""


def _response_digest(value) -> str:
    if isinstance(value, bytes):
        raw = value
    else:
        raw = str(value or "").encode("utf-8", "replace")
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class ProviderPartPreflight:
    ready: bool
    reason: str = ""
    payload_kind: str = ""
    provider_namespace: str = ""
    recipient: str = ""


@dataclass(frozen=True)
class RevisionProviderTransport:
    """Factory transport with explicit validation before the durable boundary.

    Credentials remain inside the factory closures; neither preflight results
    nor the object's representation contain credentials or provider payloads.
    """

    _preflight: Callable[[dict], ProviderPartPreflight] = field(repr=False)
    _send: Callable[[dict], ProviderPartResult] = field(repr=False)

    def preflight(self, payload: dict) -> ProviderPartPreflight:
        return self._preflight(payload)

    def __call__(self, payload: dict) -> ProviderPartResult:
        return self._send(payload)


def build_provider_part_callback(
    settings_obj,
    *,
    expected_namespace: str,
    expected_recipient: str,
    access_token: str,
) -> RevisionProviderTransport:
    """Capture credentials in memory and return an exactly-once transport."""
    from management.services.instagram_bot import (
        _provider_account_id,
        ingress_provider_namespace,
    )

    account_id = _provider_account_id(settings_obj)
    recipient = _recipient(expected_recipient)
    expected = str(expected_namespace or "")
    token = str(access_token or "").strip()

    def prepare(payload):
        # This routine performs no HTTP and may safely run before the outbox
        # marks a request as started. Re-read mutable provider configuration on
        # each invocation, including the defensive check inside send_one.
        if connection.in_atomic_block:
            return "transport_preflight_transaction_active", "", b"", ""
        if (
            not expected
            or ingress_provider_namespace(settings_obj) != expected
            or _provider_account_id(settings_obj) != account_id
            or not account_id
        ):
            return "transport_preflight_namespace_mismatch", "", b"", ""
        if not token or len(token) > 8192 or any(char.isspace() or ord(char) < 32 for char in token):
            return "transport_preflight_credentials_unavailable", "", b"", ""
        if not recipient:
            return "transport_preflight_recipient_invalid", "", b"", ""
        kind = _payload_kind(payload, expected_recipient=recipient)
        if not kind:
            return "transport_preflight_payload_invalid", "", b"", ""
        try:
            body = json.dumps(
                payload, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError):
            return "transport_preflight_payload_invalid", "", b"", ""
        if len(body) > MAX_PROVIDER_PAYLOAD_BYTES:
            return "transport_preflight_payload_too_large", "", b"", ""
        from management.services.instagram_bot import (
            GRAPH_VERSION, _provider_url, _valid_provider_request_url,
        )

        try:
            url = _provider_url(settings_obj, f"/{account_id}/messages")
            parsed = urlsplit(url)
            host = (
                "graph.instagram.com" if expected.startswith("instagram_login:")
                else "graph.facebook.com"
            )
            if (
                not _valid_provider_request_url(url, host)
                or parsed.path != f"/{GRAPH_VERSION}/{account_id}/messages"
                or parsed.query
            ):
                return "transport_preflight_url_invalid", "", b"", ""
        except (TypeError, ValueError):
            return "transport_preflight_url_invalid", "", b"", ""
        return "", kind, body, url

    def preflight(payload: dict) -> ProviderPartPreflight:
        try:
            reason, kind, _body, _url = prepare(payload)
        except Exception:
            return ProviderPartPreflight(False, "transport_preflight_check_failed")
        return ProviderPartPreflight(not reason, reason, kind, expected, recipient)

    def send_one(payload: dict) -> ProviderPartResult:
        try:
            reason, kind, body, url = prepare(payload)
        except Exception:
            return ProviderPartResult(
                provider_namespace=expected,
                outcome="known_not_dispatched",
                explicit_rejection_code="transport_preflight_check_failed",
            )
        if reason:
            # No socket I/O was entered. A race after the recorded start marker
            # is a finite local failure, never fabricated provider rejection.
            return ProviderPartResult(
                provider_namespace=expected,
                outcome="known_not_dispatched",
                explicit_rejection_code=reason,
            )
        from management.services.instagram_bot import (
            _classify_send_error,
            _provider_http,
            _provider_message_id,
            _register_outgoing_message,
        )

        current_namespace = expected
        try:
            status, response = _provider_http(
                settings_obj, url, token=token, data=body,
            )
        except TimeoutError:
            return ProviderPartResult(
                provider_namespace=current_namespace, outcome="timeout",
            )
        except Exception:
            return ProviderPartResult(
                provider_namespace=current_namespace, outcome="exception",
            )

        try:
            status = int(status)
        except (TypeError, ValueError):
            status = None
        digest = _response_digest(response)
        message_id = _provider_message_id(response) if status and 200 <= status < 300 else ""
        if message_id:
            _register_outgoing_message(message_id, recipient, kind=kind, provider_namespace=expected)
        outcome = "response"
        rejection_code = "provider_rejected"
        if status is not None and 400 <= status < 500:
            outcome = "explicit_rejected"
            classification, _safe_hint = _classify_send_error(status, response)
            if classification == "link_restricted":
                rejection_code = "link_rejected"
        elif status is None or status < 0:
            outcome = "exception"
        return ProviderPartResult(
            provider_namespace=current_namespace,
            http_status=status,
            provider_message_id=message_id,
            outcome=outcome,
            explicit_rejection_code=rejection_code,
            response_digest=digest,
        )

    return RevisionProviderTransport(preflight, send_one)


__all__ = [
    "PreparedRevisionText", "ProviderPartPreflight", "RevisionProviderTransport",
    "build_provider_part_callback", "prepare_text_effects",
]
