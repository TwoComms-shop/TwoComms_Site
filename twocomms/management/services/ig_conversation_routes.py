"""Accept low-risk discussion topics from an immutable, current source.

No runtime caller is installed here. Writers must capture ``route_binding`` on
the backend BEFORE generation, and persist it with the interpretation. Never
copy this capability from model output or reconstruct its epochs at acceptance.
Revision proposals use top-level customer_routes and route_binding. Future
OPEN_SUBFUNNEL proposals use typed_value with one intent's five normalized
fields, focus (bool), and route_binding. All proposals for that result form ONE
bounded aggregate in ordinal order, with at most one focus. Existing analysis
writers/model validation do not yet support this shape and safely abstain.

Bindings have exactly: schema_version='route-source.v1', settings_id,
settings_permission_epoch, client_permission_epoch, reset_floor,
watermark_message_id, input_digest. Input digest is the sealed revision snapshot
digest or analysis artifact_digest, respectively. The source adapter also checks
the existing winner/job/result/proposal identities. A caller's expected digest
is a CAS assertion, never authority. Lock order is settings, client, source,
journal. No HTTP, transport effect, commercial episode or funnel state writes.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re

from django.db import transaction
from django.utils import timezone

from management.models import (IgAnalysisProposal, IgClient,
    IgConversationAnalysisJob, IgConversationAnalysisResult,
    IgConversationRouteDecision, IgCustomerTurnRevision, IgFunnelResetAudit, InstagramBotMessage,
    InstagramBotSettings)
from management.services.ig_customer_route_contract import (
    SCHEMA_VERSION, normalize_customer_routes)


PRODUCER_VERSION = "conversation-route.v1"
_BINDING_KEYS = frozenset({"schema_version", "settings_id",
    "settings_permission_epoch", "client_permission_epoch", "reset_floor",
    "watermark_message_id", "input_digest"})


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def conversation_route_reset_floor(client_id: int) -> int:
    """Client route scope changes only on explicit reset, never a purchase episode.

    Read directly rather than through prompt caches at the acceptance boundary.
    Readers use this same scope to find the highest accepted sequence.
    """
    last = IgFunnelResetAudit.objects.filter(client_id=client_id).order_by("-pk").values_list(
        "reset_after_message_id", flat=True).first()
    return int(last or 0) + 1


@dataclass(frozen=True, slots=True)
class RevisionRouteSource:
    client_id: int
    settings_id: int
    revision_id: int
    revision_token: str
    expected_source_digest: str


@dataclass(frozen=True, slots=True)
class AnalysisRouteSource:
    client_id: int
    settings_id: int
    analysis_result_id: int
    expected_source_digest: str


@dataclass(frozen=True, slots=True)
class RouteAcceptance:
    decision_id: int | None = None
    created: bool = False
    reason_code: str = ""

    @property
    def accepted(self):
        return self.decision_id is not None


def _revision_source(source, client, settings_obj, now):
    from management.services.ig_revision_outbox import _cas_readiness, PublicationBinding
    from management.services.ig_revision_proposal import _generation_graph_matches, _snapshot_sources

    revision = IgCustomerTurnRevision.objects.select_for_update().filter(
        pk=source.revision_id, client_id=client.pk).first()
    if revision is None:
        return None, "source_not_owned"
    proposal = revision.generation_proposal
    if (not isinstance(proposal, dict) or not revision.generation_proposal_digest
        or source.expected_source_digest != revision.generation_proposal_digest
        or _digest(proposal) != revision.generation_proposal_digest):
        return None, "source_digest_invalid"
    binding = proposal.get("route_binding")
    if not isinstance(binding, dict):
        return None, "source_binding_missing"
    execution = proposal.get("execution_binding") or {}
    if (execution.get("settings_id") != settings_obj.pk
        or binding.get("settings_permission_epoch") != execution.get("settings_permission_epoch")
        or binding.get("client_permission_epoch") != revision.permission_epoch
        or binding.get("input_digest") != revision.snapshot_digest):
        return None, "source_binding_mismatch"
    policy = proposal.get("policy_manifest") or {}
    publication = policy.get("instruction_publication") or {}
    ready = _cas_readiness(revision, client=client, settings_obj=settings_obj,
        revision_token=source.revision_token, settings_id=settings_obj.pk,
        settings_permission_epoch=execution.get("settings_permission_epoch", -1),
        publication=PublicationBinding(publication.get("id", 0),
            publication.get("version", 0), publication.get("hash", "")),
        fact_bindings=[], offer_bindings=[], now=now)
    if not ready.ready:
        return None, ready.reasons[0]
    sources, _parts = _snapshot_sources(revision)
    if not sources or sources != proposal.get("sources"):
        return None, "source_manifest_invalid"
    ids = {row["message_id"] for row in sources}
    current_messages = {row.pk: row for row in InstagramBotMessage.objects.filter(
        client=client, pk__in=ids)}
    for sealed in revision.sources.all():
        current = current_messages.get(sealed.message_id)
        if current is None or current.role != sealed.role or current.text != sealed.text:
            return None, "source_content_changed"
    if binding.get("watermark_message_id") != max(ids):
        return None, "source_watermark_mismatch"
    generation = proposal.get("generation") or {}
    if not _generation_graph_matches(revision=revision, source_ids=ids,
        request_id=generation.get("request_id", ""), model=generation.get("actual_model", ""),
        policy_manifest=policy):
        return None, "source_winner_invalid"
    return {"payload": proposal.get("customer_routes"), "binding": binding,
        "evidence_scope": ids, "revision_id": revision.pk, "analysis_result_id": None,
        "source_digest": revision.generation_proposal_digest,
        "source_refs": {"revision_id": revision.pk,
            "request_id": generation.get("request_id"),
            "logical_turn_id": f"ig-revision:{revision.pk}"}}, ""


def _analysis_source(source, client, settings_obj, now):
    from management.services.ig_analysis_v2_projector import validate_proposal

    # Client prefix also serializes the journal across live/analysis producers.
    job = IgConversationAnalysisJob.objects.select_for_update().filter(client=client).first()
    result = IgConversationAnalysisResult.objects.select_for_update().filter(
        pk=source.analysis_result_id, client=client).first()
    if result is None:
        return None, "source_not_owned"
    if result.result_digest != source.expected_source_digest:
        return None, "source_digest_invalid"
    if job is None:
        return None, "analysis_job_missing"
    proposals = list(IgAnalysisProposal.objects.select_for_update().filter(
        analysis_result=result, proposal_type=IgAnalysisProposal.ProposalType.OPEN_SUBFUNNEL
    ).order_by("ordinal", "pk")[:5])
    if not proposals or len(proposals) > 4:
        return None, "analysis_route_count_invalid"
    intents, focus, binding, evidence_scope = [], None, None, set()
    for index, proposal in enumerate(proposals):
        # This existing validator proves digest, job revision, snapshot, logical
        # episode, ownership and USER evidence before its dependency verdict.
        proposal.client = client
        client.analysis_job = job
        checked = validate_proposal(proposal, now=now)
        if checked.code != "funnel_registry_missing":
            return None, checked.code
        value = proposal.typed_value
        required = {"kind", "subtype", "operation", "evidence_message_ids",
            "confidence", "focus", "route_binding"}
        if not isinstance(value, dict) or set(value) != required:
            return None, "source_binding_missing"
        if type(value["focus"]) is not bool or (value["focus"] and focus is not None):
            return None, "analysis_focus_invalid"
        if proposal.target_definition_version != SCHEMA_VERSION:
            return None, "analysis_route_version_invalid"
        intent_payload = {key: value[key] for key in required - {"focus", "route_binding"}}
        parsed = normalize_customer_routes({"schema_version": SCHEMA_VERSION,
            "intents": [intent_payload]})
        if parsed.abstained:
            return None, parsed.reason_code
        if (set(value["evidence_message_ids"]) != set(proposal.evidence_message_ids)
            or value["confidence"] != float(proposal.confidence)):
            return None, "analysis_route_evidence_mismatch"
        if binding is not None and binding != value["route_binding"]:
            return None, "source_binding_mismatch"
        binding = value["route_binding"]
        if value["focus"]:
            focus = index
        intents.append(intent_payload)
        evidence_scope.update(proposal.evidence_message_ids)
    if (not isinstance(binding, dict) or not result.artifact_digest
        or binding.get("input_digest") != result.artifact_digest
        or binding.get("watermark_message_id") != result.watermark_message_id):
        return None, "source_binding_mismatch"
    return {"payload": {"schema_version": SCHEMA_VERSION, "intents": intents,
        "focus_index": focus}, "binding": binding, "evidence_scope": evidence_scope,
        "revision_id": None, "analysis_result_id": result.pk,
        "source_digest": result.result_digest, "source_refs": {
            "analysis_result_id": result.pk, "proposal_ids": [p.pk for p in proposals],
            "job_revision": result.job_revision,
            "commercial_episode_id": result.commercial_episode_id, "line_id": result.line_id,
            "materiality_digest": result.materiality_digest,
            "authority_digest": result.authority_digest,
            "state_correlation": result.state_correlation}}, ""


def _binding_reason(binding, client, settings_obj):
    if not isinstance(binding, dict) or set(binding) != _BINDING_KEYS:
        return "source_binding_invalid"
    if binding["schema_version"] != "route-source.v1":
        return "source_binding_version_invalid"
    for key in _BINDING_KEYS - {"schema_version", "input_digest"}:
        if type(binding[key]) is not int or binding[key] < 0:
            return "source_binding_invalid"
    if not re.fullmatch(r"[0-9a-f]{64}", str(binding["input_digest"])):
        return "source_binding_invalid"
    if (binding["settings_id"] != settings_obj.pk
        or binding["settings_permission_epoch"] != settings_obj.reply_permission_epoch
        or binding["client_permission_epoch"] != client.reply_permission_epoch):
        return "permission_epoch_changed"
    if binding["reset_floor"] != conversation_route_reset_floor(client.pk):
        return "reset_floor_changed"
    watermark = InstagramBotMessage.objects.filter(client=client,
        role=InstagramBotMessage.Role.USER).order_by("-pk").values_list("pk", flat=True).first()
    if watermark != binding["watermark_message_id"]:
        return "source_watermark_stale"
    return ""


def _route_key(intent):
    return intent.kind + ":" + intent.subtype


def accept_customer_routes(source, *, expected_previous_decision_id, now=None):
    """CAS append one accepted decision; absent intents never close old routes.

    ``correct`` requires that exact kind/subtype to be active and creates an
    explicit correction event. Changing kinds requires an explicit withdrawal
    plus opening the replacement; no unmentioned route is inferred obsolete.
    ``continue`` and ``withdraw`` require an active route. Open-on-active records
    fresh evidence without inventing another transition. No default focus is
    inferred. Replaying the same immutable input returns its original decision.
    """
    if not isinstance(source, (RevisionRouteSource, AnalysisRouteSource)):
        return RouteAcceptance(reason_code="source_type_invalid")
    now = now or timezone.now()
    with transaction.atomic():
        settings_obj = InstagramBotSettings.objects.select_for_update().select_related(
            "active_instruction_publication").filter(pk=source.settings_id).first()
        client = IgClient.objects.select_for_update().filter(pk=source.client_id).first()
        if settings_obj is None or not settings_obj.is_enabled:
            return RouteAcceptance(reason_code="settings_disabled")
        if client is None:
            return RouteAcceptance(reason_code="client_missing")
        if client.privacy_erasure_started_at is not None:
            return RouteAcceptance(reason_code="client_erasure_changed")
        if client.manager_takeover:
            return RouteAcceptance(reason_code="manager_takeover")
        if client.hidden_at or client.is_blocked or client.bot_paused:
            return RouteAcceptance(reason_code="client_blocked")
        if client.opted_out_at and (not client.opted_in_at or client.opted_in_at < client.opted_out_at):
            return RouteAcceptance(reason_code="client_opted_out")
        from management.services.ig_permission_transitions import permission_transition_blocks
        if permission_transition_blocks(settings_id=settings_obj.pk, client_id=client.pk):
            return RouteAcceptance(reason_code="permission_transition_pending")
        adapter = _revision_source if isinstance(source, RevisionRouteSource) else _analysis_source
        data, reason = adapter(source, client, settings_obj, now)
        if reason:
            return RouteAcceptance(reason_code=reason)
        binding = data["binding"]
        reason = _binding_reason(binding, client, settings_obj)
        if reason:
            return RouteAcceptance(reason_code=reason)
        normalized = normalize_customer_routes(data["payload"])
        if normalized.abstained:
            return RouteAcceptance(reason_code=normalized.reason_code)
        proposal = normalized.proposal
        evidence_ids = {pk for intent in proposal.intents for pk in intent.evidence_message_ids}
        if not evidence_ids.issubset(data["evidence_scope"]):
            return RouteAcceptance(reason_code="evidence_outside_source")
        evidence = list(InstagramBotMessage.objects.filter(client=client, pk__in=evidence_ids,
            pk__gte=binding["reset_floor"], pk__lte=binding["watermark_message_id"],
            role=InstagramBotMessage.Role.USER).values_list("pk", "text"))
        if {pk for pk, _text in evidence} != evidence_ids:
            return RouteAcceptance(reason_code="evidence_not_current_or_owned")
        # Non-text modality needs a later explicit artifact-binding adapter;
        # an owned image/voice reference alone is not understood intent evidence.
        if any(not str(text or "").strip() for _pk, text in evidence):
            return RouteAcceptance(reason_code="evidence_modality_unavailable")
        scope = IgConversationRouteDecision.objects.filter(client=client,
            reset_floor=binding["reset_floor"])
        existing = scope.filter(input_digest=binding["input_digest"],
            interpretation_digest=proposal.digest).first()
        if existing:
            return RouteAcceptance(existing.pk, reason_code="already_accepted")
        previous = scope.select_for_update().order_by("-sequence").first()
        if (previous.pk if previous else None) != expected_previous_decision_id:
            return RouteAcceptance(reason_code="previous_decision_changed")
        if previous and previous.watermark_message_id > binding["watermark_message_id"]:
            return RouteAcceptance(reason_code="source_watermark_stale")
        active = {item["key"]: dict(item) for item in (previous.active_intents if previous else [])}
        focus_key = previous.focus_key if previous else ""
        transitions = []
        for intent in proposal.intents:
            key = _route_key(intent)
            if intent.operation != "open" and key not in active:
                return RouteAcceptance(reason_code="route_not_active")
            action = ""
            if intent.operation == "withdraw":
                del active[key]
                action = "withdraw"
                if focus_key == key:
                    focus_key = ""
            else:
                if key not in active:
                    action = "open"
                elif intent.operation == "correct":
                    action = "correct"
                active[key] = {"key": key, "kind": intent.kind, "subtype": intent.subtype}
            if action:
                transitions.append({"operation": action, "key": key,
                    "reason_code": "customer_correction" if action == "correct" else "customer_intent",
                    "evidence_message_ids": list(intent.evidence_message_ids)})
        if proposal.focus_index is not None:
            next_focus = _route_key(proposal.intents[proposal.focus_index])
            if next_focus != focus_key:
                transitions.append({"operation": "focus", "from_key": focus_key,
                    "key": next_focus, "reason_code": "customer_intent"})
            focus_key = next_focus
        source_binding = {**binding, "source_digest": data["source_digest"],
            "source_refs": data["source_refs"]}
        decision_key = _digest({"client_id": client.pk, "input": binding,
            "interpretation_digest": proposal.digest})
        active_intents = sorted(active.values(), key=lambda item: item["key"])
        decision_value = {"decision_key": decision_key,
            "previous_id": previous.pk if previous else None,
            "source_binding": source_binding, "active_intents": active_intents,
            "focus_key": focus_key, "transitions": transitions}
        decision = IgConversationRouteDecision.objects.create(client=client,
            revision_id=data["revision_id"], analysis_result_id=data["analysis_result_id"],
            previous=previous, decision_key=decision_key,
            sequence=previous.sequence + 1 if previous else 1,
            reset_floor=binding["reset_floor"], watermark_message_id=binding["watermark_message_id"],
            input_digest=binding["input_digest"], interpretation_digest=proposal.digest,
            decision_digest=_digest(decision_value), source_binding=source_binding,
            interpretation=proposal.to_dict(), active_intents=active_intents,
            focus_key=focus_key, transitions=transitions,
            reason_code="customer_correction" if any(i.operation == "correct" for i in proposal.intents)
                else "customer_intent", occurred_at=now)
        return RouteAcceptance(decision.pk, created=True)
