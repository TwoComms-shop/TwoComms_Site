"""Bounded read-only policy inputs for an already validated sealed capture.

These inputs select public instructions; they grant no business authority.
No profile objection/stage, latest message, bootstrap or provider is consulted.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from collections.abc import Mapping

from management.services.ig_turn_intelligence import (
    MAX_HISTORY, MAX_SOURCES, MAX_SOURCE_CHARS, SCOPE_KEYS, TurnContextError,
    capture_digest, current_objections,
)

VERSION = "captured-policy-inputs.v2"
LANGUAGES = frozenset({"uk", "ru", "en"})


def _plain(value):
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return value


def _date(value):
    if not isinstance(value, datetime):
        value = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError("aware time required")
    return value


@dataclass(frozen=True)
class CapturedPolicyInputs:
    """The JSON snapshot is immutable; each consumer receives detached values."""
    _snapshot: str

    @property
    def tags(self):
        return set(json.loads(self._snapshot)["tags"])

    @property
    def automation_note(self):
        return json.loads(self._snapshot)["automation_note"]

    @property
    def knowledge_language(self):
        return json.loads(self._snapshot)["knowledge_language"]

    @property
    def state_slots(self):
        return json.loads(self._snapshot)["state_slots"]

    @property
    def metadata(self):
        return json.loads(self._snapshot)["metadata"]


def _validated_inputs(client_id, boundary, sources, history, captured_at):
    try:
        if boundary["client_id"] != client_id or boundary.get("erasure_epoch"):
            raise ValueError("owner")
        if any(key not in boundary for key in SCOPE_KEYS):
            raise ValueError("scope")
        if not boundary["source_namespace"] or not 1 <= len(sources) <= MAX_SOURCES or len(history) > MAX_HISTORY:
            raise ValueError("bounds")
        ids = [row["message_id"] for row in sources]
        if ids != boundary["source_ids"] or len(set(ids)) != len(ids):
            raise ValueError("source window")
        if sum(len(str(row.get("text") or "")) for row in sources) > MAX_SOURCE_CHARS:
            raise ValueError("text budget")
        if capture_digest(sources) != boundary["sealed_sources_digest"]:
            raise ValueError("seal")
        watermark = (_date(boundary["watermark"]["event_at"]), boundary["watermark"]["message_id"])
        for row in sources:
            if (row.get("role") != "user" or row.get("source_namespace") != boundary["source_namespace"]
                or ("client_id" in row and row["client_id"] != client_id)
                or row["message_id"] < boundary["reset_floor"]
                or row["source_digest"] != boundary["source_digests"][str(row["message_id"])]):
                raise ValueError("source")
            at = _date(row.get("provider_created_at") or row.get("observed_created_at"))
            if (at, row["message_id"]) > watermark:
                raise ValueError("event")
        for row in history:
            if (row["message_id"] < boundary["reset_floor"] or row["message_id"] >= min(ids)
                or any(row["scope"].get(key) != boundary[key] for key in SCOPE_KEYS)
                or (_date(row["event_at"]), row["message_id"]) > watermark
                or boundary.get("history_digests", {}).get(str(row["message_id"])) != capture_digest(row)):
                raise ValueError("history")
    except (KeyError, TypeError, ValueError):
        raise TurnContextError("policy_capture_scope_invalid") from None


def _language(sources, history):
    from management.services.ig_reply_language import resolve_own_source_reply_language

    decision = resolve_own_source_reply_language(sources=sources, history=history)
    evidence = next((row for row in (*sources, *history)
        if row.get("message_id") == decision.source_message_id), None)
    return decision.reply_language, evidence, decision.basis


def capture_policy_inputs(client_id, *, boundary, sources, history, response_plan, captured_at):
    """Freeze public routing and guardrail inputs before request assembly.

    At most four reads: owner, current episode, open service case, case source.
    Sources/history must be the previously validated sealed capture, including
    their exact digest bindings. Stored language is only a compatibility hint.
    """
    from management.models import IgClient, IgCommercialEpisode, InstagramBotMessage
    from management.services.ig_commerce_turns import parse_turn
    from management.services.ig_post_sale import open_service_case
    from management.services.ig_turn_snapshot import prompt_snapshot
    from management.services.instagram_bot import automation_guardrails

    boundary, sources, history = _plain(boundary), _plain(sources), _plain(history)
    try:
        captured_at = _date(captured_at)
    except (TypeError, ValueError):
        raise TurnContextError("policy_capture_scope_invalid") from None
    _validated_inputs(client_id, boundary, sources, history, captured_at)
    client = IgClient.objects.only("pk", "igsid", "language", "current_commercial_episode_id",
        "privacy_erasure_started_at").filter(pk=client_id).first()
    if (client is None or client.privacy_erasure_started_at is not None
        or client.current_commercial_episode_id != boundary["episode_id"]):
        raise TurnContextError("policy_capture_owner_changed")
    scope = {key: boundary.get(key) for key in (*SCOPE_KEYS, "order_id")}
    from management.services.ig_reply_language import resolve_own_source_reply_language

    decision = resolve_own_source_reply_language(sources=sources, history=history,
        profile_language=client.language, reset_floor=boundary["reset_floor"],
        watermark_message_id=boundary["watermark"]["message_id"],
        watermark_event_at=boundary["watermark"]["event_at"])
    language = decision.reply_language
    evidence = next((row for row in (*sources, *history)
        if row.get("message_id") == decision.source_message_id), None)
    language_basis = decision.basis if evidence else "language_source_unknown"
    hint = client.language if client.language in LANGUAGES else ""
    knowledge_language = decision.knowledge_locale
    language_slot = {"value": language or None, "status": "confirmed" if language and evidence else "unknown",
        "authority": "customer_source" if evidence else "none", "scope": scope,
        "source_refs": ([{"kind": "message", "id": evidence["message_id"],
                          "event_at": evidence.get("provider_created_at") or evidence.get("observed_created_at") or evidence.get("event_at")}]
                        if evidence else []),
        "observed_at": (evidence.get("provider_created_at") or evidence.get("observed_created_at") or evidence.get("event_at")) if evidence else None,
        "validity": "valid" if language and evidence else "unknown", "mandatory": bool(language and evidence),
        "omission_reason": "" if language and evidence else "language_source_unknown"}
    tags = {"global", "core", "sales", knowledge_language}
    objections = current_objections(sources)
    for item in objections:
        kind = item["kind"]
        tags.update({kind, "objection_" + kind})
        tags.update({"price": {"discount"}, "cheaper_elsewhere": {"price", "discount"},
            "size_risk": {"size", "fit"}, "prepayment_trust": {"prepayment", "trust"},
            "delivery_time": {"delivery"}}.get(kind, set()))
    requests = [parse_turn(str(row.get("text") or "")) for row in sources]
    if any(row.custom_print_requested for row in requests):
        tags.add("custom_print")
    if any(row.personalized_fit_requested for row in requests):
        tags.update({"size", "fit"})
    if any(row.support_requested for row in requests):
        tags.add("support")
    choices = getattr(response_plan, "choices", {}) or {}
    if choices.get("product_id"):
        tags.update({"product", "catalog"})
    if choices.get("size"):
        tags.add("size")
    episode = None
    if boundary["episode_id"] is not None:
        episode = IgCommercialEpisode.objects.filter(pk=boundary["episode_id"], client_id=client_id).only(
            "pk", "state", "open_slot", "intended_order_id").first()
    if episode is not None and episode.open_slot == 1 and episode.state == episode.State.ORDER_CREATED and episode.intended_order_id == boundary.get("order_id"):
        # An order record is not payment confirmation.
        tags.add("order_created")
    # This local cache scopes the existing server renderer and the proof read
    # to one case query. No cache or ORM selector escapes into assembly.
    with prompt_snapshot():
        case = open_service_case(client)
        automation_note = automation_guardrails(client)
    service = {"status": "absent", "scope": scope}
    omissions = []
    if case is not None:
        source = InstagramBotMessage.objects.filter(pk=case.source_message_id, client_id=client_id,
            sender_id=client.igsid, role="user", provider_namespace=boundary["source_namespace"],
            pk__gte=boundary["reset_floor"], pk__lte=max(boundary["source_ids"])).first()
        at = (source.provider_created_at or source.created_at) if source else None
        valid = (source is not None and source.source in {"webhook", "poll"}
            and source.status != "failed" and case.case_type in case.CaseType.values
            and case.status in case.Status.values
            and (at, source.pk) <= (_date(boundary["watermark"]["event_at"]), boundary["watermark"]["message_id"])
            and case.updated_at <= captured_at and case.commercial_episode_id == boundary["episode_id"]
            and case.order_id in (None, boundary.get("order_id")))
        if valid:
            tags.difference_update({"sales", "price", "discount"})
            tags.update({"post_sale", "service", str(case.case_type)})
            service = {"status": "captured", "scope": scope, "case_id": case.pk,
                "case_type": str(case.case_type), "case_status": str(case.status),
                "version": case.updated_at.isoformat(), "source_message_id": source.pk}
        else:
            # Do not label an unrelated historical case as this turn's context.
            # Keep the server's restrictive guardrail text; it grants no sales
            # or service operation and avoids weakening an existing guard.
            service["status"] = "unknown"
            omissions.append("service_scope_unproven")
    payload = {"tags": sorted(tags), "automation_note": automation_note,
        "knowledge_language": knowledge_language, "state_slots": {"language": language_slot},
        "metadata": {"version": VERSION, "language_basis": language_basis,
            "knowledge_language_basis": language_basis if language else "stored_profile_hint" if hint and hint not in decision.excluded_languages else "compatibility_default",
            "language_source_id": evidence["message_id"] if evidence else None,
            "reply_language": decision.as_dict(),
            "service": service, "omissions": omissions, "read_only": True,
            "routing_stage_basis": "current_episode_order_created" if "order_created" in tags else "stage_source_unknown"}}
    return CapturedPolicyInputs(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False))
