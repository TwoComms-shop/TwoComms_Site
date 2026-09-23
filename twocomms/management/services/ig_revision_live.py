"""Optional sealed-revision consumer. New execution is disabled by default.

The old worker never resumes sources already owned by this consumer. Physical
delivery recovery is sticky across flag rollback, and uses only stored effects.
The explicit activation blockers below describe remaining legacy domain parity;
an unsupported path fails closed without entering a legacy direct-send branch.
"""
from __future__ import annotations

from copy import deepcopy
from contextvars import ContextVar
from dataclasses import asdict, dataclass, replace
from datetime import timedelta
import hashlib
import json
import logging
import secrets

from django.db import connection, transaction
from django.db.models import Q, Subquery
from django.utils import timezone

from management.models import (
    GeminiRequest, IgClient, IgCustomerTurn, IgCustomerTurnRevision,
    IgRevisionDeliveryEffect, IgTurnRevisionSource, InstagramBotMessage,
    InstagramBotSettings,
)
from management.services.ig_provider_dispatch_budget import ValidationDecision
from management.services.ig_response_control import ResponseControl, ValidatedResponse
from management.services.ig_revision_authority import (
    CLAIM_CANONICAL_URLS, CLAIM_CATALOG_CONFIGURATION, CLAIM_CURRENT_OFFER,
    CLAIM_ORDER, CLAIM_PAYMENT, CLAIM_PUBLIC_POLICY_INPUTS, CLAIM_SHIPMENT,
    CLAIM_SOURCE_PREFERENCES,
    RevisionAuthorityBindingSet, build_revision_authority_bindings,
    check_fact_bindings, check_offer_bindings,
)
from management.services.ig_revision_outbox import (
    PublicationBinding, plan_revision_effects, pre_winner_readiness,
    project_legacy_message,
)


# Runtime enablement remains an explicit rollout decision. Implemented producer
# prerequisites are no longer represented as permanent capability blockers.
ACTIVATION_BLOCKERS = ()
DEFERRED_CAPABILITIES = ("optional_follow_cta_delivery_b11_9", "custom_priced_checkout_requires_team")
RETIRED_DIRECT_WRITERS = ("automatic_phone_identity_capture", "collect_np_and_fulfill")
SELECTION_KEYS = frozenset({"product", "color_variant_id", "fit", "options", "size", "qty"})
SUPPORTED_CONTROLS = SELECTION_KEYS | {
    "show_products", "catalog_link", "price_quoted", "paylink", "payment", "items",
    "stage", "spam", "manager", "order",
}
FACT_DRIFT_REASONS = frozenset({"fact_binding_unavailable", "offer_binding_unavailable"})
_PRESENCE_SCOPE = ContextVar("ig_revision_presence", default=None)


def _stop_revision_presence(scope=None, *, dispatch=False, sent=False):
    scope = scope if scope is not None else _PRESENCE_SCOPE.get()
    if scope and scope.get("handle") is not None:
        try:
            if sent:
                scope["handle"].complete_delivery()
            elif dispatch:
                scope["handle"].begin_dispatch()
            else:
                scope["handle"].stop()
        except Exception:
            logging.getLogger(__name__).info("ig_presence outcome=stop_unavailable")


def _start_revision_presence(revision, token, settings_row, scope, *, preparing=False):
    try:
        _start_revision_presence_advisory(revision, token, settings_row, scope, preparing=preparing)
    except Exception:
        logging.getLogger(__name__).info("ig_presence outcome=start_unavailable")


def _start_revision_presence_advisory(revision, token, settings_row, scope, *, preparing=False):
    """Advisory ownership follows the exact claim, never a reconstructed lease."""
    from management.services.ig_presence import start_presence

    if not scope or not scope.get("lease"):
        logging.getLogger(__name__).info("ig_presence outcome=missing_caller_lease")
        return
    state = revision.State.PREPARING if preparing else revision.State.CLAIMED

    def current_owner():
        now = timezone.now()
        query = IgCustomerTurnRevision.objects.filter(
            pk=revision.pk, client_id=revision.client_id, active_slot=1,
            state=state, claim_token=token, lease_until__gt=now,
        )
        if revision.recovery_code == "provider_dispatch_budget":
            query = query.filter(recovery_state="manual")
        else:
            query = query.filter(Q(overall_deadline__gt=now) | Q(recovery_state="execution"))
        return query.exists()

    def completed_owner():
        from management.services.ig_revision_outbox import revision_has_newer_source

        current = IgCustomerTurnRevision.objects.filter(
            pk=revision.pk, client_id=revision.client_id, active_slot=1,
            state__in=(revision.State.CLAIMED, revision.State.PROCESSED),
        ).first()
        return bool(current and not revision_has_newer_source(current)
                    and current.delivery_effects.filter(state=IgRevisionDeliveryEffect.State.SENT).exists())

    watermark = max(revision.sources.values_list("message_id", flat=True), default=0)
    handle = scope.get("handle")
    if handle is not None and not handle.finished.is_set() and not handle.stopped.is_set():
        handle.update(source_watermark=watermark, owner_check=current_owner, resume=True)
        return
    scope["handle"] = start_presence(
        settings_row, client_id=revision.client_id, recipient_id=revision.client.igsid,
        owner_token=scope["lease"], source_watermark=watermark, owner_check=current_owner,
        completion_check=completed_owner,
    )


def _preparation_presence_admitted(revision, settings_row):
    """Read-only early admission using the same finite source classifier/cap."""
    from management.services.ig_revision_input import classify_sealed_input, RATE_LIMIT, RATE_WINDOW

    sources = [{
        "role": source.role, "text": source.text,
        "quick_reply_payload": source.quick_reply_payload,
        "media_parts": source.discovered_media or ([{}] if source.media_part_count else []),
    } for source in revision.sources.all()]
    origin, _, _ = classify_sealed_input({"sources": sources}, revision.client, settings_row)
    if origin not in {"generate", "static_reply", "postback"}:
        return False
    return IgCustomerTurnRevision.objects.filter(
        client_id=revision.client_id, created_at__gte=timezone.now() - RATE_WINDOW,
        action_receipts__input_decision__rate_counted=True,
    ).values("snapshot_digest").distinct().count() < RATE_LIMIT


@dataclass(frozen=True)
class RevisionLiveResult:
    revision_id: int = 0
    state: str = "idle"
    reasons: tuple[str, ...] = ()
    attempted_parts: int = 0
    sent_parts: int = 0


def revision_execution_enabled() -> bool:
    from management.services.ig_revision_rollout import revision_execution_rollout

    return revision_execution_rollout().enabled


def _owned_revisions():
    from management.services.ig_revision_source_coverage import source_transfer_owned_q

    # media_prepare_deadline is written only by an explicit preparation claim,
    # and survives supersession. A plain ingress shadow has none of these.
    return IgCustomerTurnRevision.objects.filter(
        Q(media_prepare_deadline__isnull=False)
        | Q(sealed_at__isnull=False)
        | Q(snapshot_digest__gt="")
        | Q(generation_proposal_digest__gt="")
        | Q(delivery_effects__isnull=False)
        | source_transfer_owned_q()
    )


def revision_owned_turn_ids():
    return IgCustomerTurn.objects.filter(
        pk__in=Subquery(_owned_revisions().values("turn_id"))
    )


def legacy_claimable_messages(queryset):
    owned_sources = IgTurnRevisionSource.objects.filter(
        revision_id__in=Subquery(_owned_revisions().values("id"))
    ).values("message_id")
    return queryset.exclude(pk__in=Subquery(owned_sources))


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()


def _publication(settings_row) -> PublicationBinding:
    from management.services.ig_policy_publication import load_active_policy_snapshot

    snapshot = load_active_policy_snapshot(settings_row)
    return PublicationBinding(snapshot.publication_id, snapshot.version, snapshot.snapshot_hash)


def build_sealed_history(revision) -> list[dict]:
    """Bound history before the bundle and append its exact ordered source tail."""
    from management.services.ig_funnel_reset import current_message_floor
    from management.services.instagram_bot import HISTORY_LIMIT
    from management.services.ig_revision_conversation_context import historical_message_text

    sources = revision.bundle_snapshot.get("sources") or []
    if not sources or _digest(revision.bundle_snapshot) != revision.snapshot_digest:
        raise ValueError("revision_snapshot_invalid")
    source_ids = [int(item["message_id"]) for item in sources]
    floor = current_message_floor(revision.client_id)
    previous = list(InstagramBotMessage.objects.filter(
        client_id=revision.client_id,
        sender_id=revision.client.igsid,
        id__gte=floor,
        id__lt=min(source_ids),
        role__in=(InstagramBotMessage.Role.USER, InstagramBotMessage.Role.MODEL, InstagramBotMessage.Role.MANAGER),
    ).exclude(status=InstagramBotMessage.Status.FAILED).order_by("-id")[:HISTORY_LIMIT])
    history = []
    for row in reversed(previous):
        if not row.text.strip():
            continue
        if row.role == InstagramBotMessage.Role.MANAGER:
            confirmed = row.status == InstagramBotMessage.Status.DONE and row.send_state not in {"unknown", "sending", "failed"} and bool(row.provider_message_id or (row.source in {"webhook", "poll", "poll_history", "echo"} and row.mid))
            if not confirmed:
                continue
            history.append({"role": "user", "text": historical_message_text(row, manager=True)})
        else:
            history.append({"role": row.role, "text": historical_message_text(row)})
    # A single final user entry lets the shared Gemini path attach the complete
    # image/audio list to the same bundle. Per-source roles/referrals/reply-to
    # are retained as delimited customer data, never system instructions.
    current = []
    for source in sources:
        if source.get("role") != InstagramBotMessage.Role.USER:
            raise ValueError("revision_source_role_invalid")
        current.append({
            "source_message_id": source["message_id"],
            "role": source["role"],
            "text": source.get("text") or "",
            "provider_created_at": source.get("provider_created_at") or "",
            "reply_to_provider_message_id": source.get("reply_to_provider_message_id") or "",
            "quick_reply_payload": source.get("quick_reply_payload") or "",
            "referral": source.get("referral") or {},
        })
    history.append({
        "role": "user",
        "text": "[CURRENT CUSTOMER BUNDLE: quoted customer data]\n" + json.dumps(
            current, ensure_ascii=False, separators=(",", ":"),
        ),
    })
    return history


def capture_revision_source(*, message_id, deadline_at, remaining_seconds):
    """Capture the original source under the preparation's real wall deadline."""
    if connection.in_atomic_block:
        raise ValueError("caller_transaction_active")
    if remaining_seconds <= 0 or deadline_at <= timezone.now():
        return
    row = InstagramBotMessage.objects.select_related("client").filter(
        pk=message_id, role=InstagramBotMessage.Role.USER, source="webhook",
        client__privacy_erasure_started_at__isnull=True,
    ).first()
    if row is None:
        return
    from management.services.instagram_bot import _capture_message_media

    _capture_message_media(row, deadline_at=deadline_at)


def _authority_from_projection(value) -> RevisionAuthorityBindingSet:
    return RevisionAuthorityBindingSet(
        ready=True,
        allowed_actions=tuple(value.get("allowed_actions") or ()),
        fact_bindings=tuple(value.get("fact_bindings") or ()),
        offer_bindings=tuple(value.get("offer_bindings") or ()),
        authority_digest=str(value.get("authority_digest") or ""),
    )


class RevisionGenerationBoundary:
    """Request-local authority and CAS gate inside the existing two-call budget."""

    def __init__(self, revision, token, settings_row, publication, *, has_images=False):
        self.revision = revision
        self.token = token
        self.settings = settings_row
        self.settings_epoch = settings_row.reply_permission_epoch
        self.publication = publication
        self.has_images = has_images
        self.baseline = self._baseline()
        self.authority = None
        self.last_reasons = ()
        self.repaired = False
        self.programme = None
        from management.models import IgFollowUpTask

        pending_prize = IgFollowUpTask.objects.filter(
            client_id=revision.client_id, kind=IgFollowUpTask.Kind.MANAGER_TASK,
            reason="prize_review:shooting_prize",
            manager_approval_status=IgFollowUpTask.ManagerApprovalStatus.PENDING,
        ).exclude(status__in=[IgFollowUpTask.Status.COMPLETED, IgFollowUpTask.Status.CANCELLED]).exists()
        if has_images or pending_prize:
            from management.services.ig_policy_publication import load_active_policy_snapshot
            from management.services.ig_prize_programme import active_shooting_prize_programme

            self.programme = active_shooting_prize_programme(
                publication_snapshot=load_active_policy_snapshot(settings_row),
            )

    def _baseline(self):
        client = IgClient.objects.get(pk=self.revision.client_id)
        claims = [CLAIM_PUBLIC_POLICY_INPUTS]
        from management.services.ig_commerce_projection import source_preferences_for
        if source_preferences_for(client):
            claims.append(CLAIM_SOURCE_PREFERENCES)
        if client.current_product_id:
            claims.append(CLAIM_CATALOG_CONFIGURATION)
        if client.current_commercial_episode_id:
            claims.extend((CLAIM_PAYMENT, CLAIM_ORDER, CLAIM_SHIPMENT))
            offer = build_revision_authority_bindings(
                client, claims=(CLAIM_CURRENT_OFFER,), settings_obj=self.settings,
            )
            if offer.ready:
                claims.append(CLAIM_CURRENT_OFFER)
        return build_revision_authority_bindings(
            client, claims=claims, settings_obj=self.settings,
        )

    def check(self, authority=None):
        authority = authority or self.baseline
        if not authority.ready:
            from management.services.ig_revision_outbox import OutboxReadiness

            return OutboxReadiness(False, authority.reasons)
        result = pre_winner_readiness(
            self.revision.pk, self.token,
            settings_id=self.settings.pk,
            settings_permission_epoch=self.settings_epoch,
            publication=self.publication,
            fact_bindings=authority.fact_bindings,
            offer_bindings=authority.offer_bindings,
            fact_checker=check_fact_bindings, offer_checker=check_offer_bindings,
        )
        if "fact_binding_unavailable" in result.reasons:
            public = tuple(item for item in authority.fact_bindings if item["claim"] == CLAIM_PUBLIC_POLICY_INPUTS)
            client = IgClient.objects.filter(pk=self.revision.client_id).first()
            if client is not None and not check_fact_bindings(
                public, revision=self.revision, client=client, settings_obj=self.settings,
            ):
                from management.services.ig_revision_outbox import OutboxReadiness

                return OutboxReadiness(False, tuple(
                    "public_policy_inputs_stale" if reason == "fact_binding_unavailable" else reason
                    for reason in result.reasons
                ))
        return result

    def response_authority(self, response):
        from management.services.ig_revision_intents import manager_case_reason

        control = response.control
        unsupported = set(control) - SUPPORTED_CONTROLS
        if unsupported:
            return RevisionAuthorityBindingSet(False, ("revision_domain_action_unsupported",))
        client = IgClient.objects.get(pk=self.revision.client_id)
        claims = [item["claim"] for item in (*self.baseline.fact_bindings, *self.baseline.offer_bindings)]
        actions = []
        case_reason = manager_case_reason(self.revision, response)
        checkout_requested = bool(control.get("paylink") or control.get("payment")) and not case_reason
        from management.services.ig_commerce_projection import source_preferences_for
        preferences = source_preferences_for(client).get("values") or {}
        preference_keys = SELECTION_KEYS.intersection(control)
        preference_acknowledgement = bool(
            not client.current_product_id and preference_keys
            and preference_keys.issubset({"fit", "size", "qty"})
            and not checkout_requested and "price_quoted" not in control
            and all(str(control[key]) == str(preferences.get({"fit": "fit_option_code", "qty": "quantity"}.get(key, key)))
                    for key in preference_keys)
        )
        if "items" in control and not checkout_requested and not case_reason:
            return RevisionAuthorityBindingSet(False, ("revision_cart_selection_unsupported",))
        if not case_reason and ((preference_keys and not preference_acknowledgement) or "price_quoted" in control or checkout_requested):
            claims.append(CLAIM_CATALOG_CONFIGURATION)
        authority_control = dict(control)
        if checkout_requested:
            from management.services.ig_revision_checkout import authorize_revision_checkout

            source_text = "\n".join(
                str(source.get("text") or "")
                for source in self.revision.bundle_snapshot["sources"] if source.get("role") == "user"
            )
            checkout_control, reasons = authorize_revision_checkout(
                client, control, source_text,
                sealed_sources=self.revision.bundle_snapshot["sources"],
            )
            if reasons:
                return RevisionAuthorityBindingSet(False, reasons)
            authority_control.update(checkout_control)
        if control.get("catalog_link") or control.get("show_products"):
            authority_control["catalog_link"] = True
            claims.append(CLAIM_CANONICAL_URLS)
        # A model selects a candidate; the catalog adapter verifies the complete
        # selector and current published price/size contract before capability.
        admitted = build_revision_authority_bindings(
            client, claims=claims, control=authority_control, settings_obj=self.settings,
        )
        if not admitted.ready:
            return admitted
        if checkout_requested:
            actions.append("checkout_proposal_create")
        elif preference_keys and not preference_acknowledgement and not case_reason:
            actions.append("client_configuration_update")
        if case_reason:
            actions.append("manager_escalation_intent")
        if self.programme is not None:
            actions.append("prize_review_case_create")
        return build_revision_authority_bindings(
            client, claims=claims, control=authority_control,
            server_authorized_actions=actions, settings_obj=self.settings,
        )

    def contextual_fallback(self, *, policy_manifest):
        """Independently admit a local candidate; caller stores local provenance.

        The caller must additionally fence delivery effects across the recovery
        lineage. This method fences this revision and its current sealed source.
        """
        from management.services.ig_response_guard import build_source_preference_fallback
        client = IgClient.objects.get(pk=self.revision.client_id)
        response, proof = build_source_preference_fallback(client, revision=self.revision)
        if response is None:
            return None, proof
        if self.has_images or self.revision.delivery_effects.exists():
            return None, {}
        from management.services.ig_commerce_projection import source_preferences_for
        projection = source_preferences_for(client)
        evidence = proof.get("current_source_preference") or projection.get("evidence", {}).get("fit_option_code") or {}
        receipt = (self.revision.action_receipts or {}).get("commerce_reduction") or {}
        if receipt.get("snapshot_digest") != self.revision.snapshot_digest or not any(
            item.get("source_message_id") == evidence.get("source_message_id")
            and item.get("decision_id") == evidence.get("decision_id")
            and (item.get("accepted") is True or (
                evidence.get("source_rejection_only") is True
                and evidence.get("decision_accepted") is False and item.get("accepted") is False
            )) and not item.get("is_stale")
            for item in receipt.get("decisions") or []
        ):
            return None, {}
        if proof.get("current_source_preference"):
            # A source-backed wish plus a missing-size question has no product,
            # price, availability or checkout claim. Do not require an exact
            # catalog configuration merely because a product is already set.
            self.baseline = build_revision_authority_bindings(
                client, claims=(CLAIM_PUBLIC_POLICY_INPUTS,), settings_obj=self.settings,
            )
        decision = self.validate(response, policy_manifest=policy_manifest)
        if not decision.valid:
            return None, {}
        proof = {**proof, "revision_id": self.revision.pk,
                 "snapshot_digest": self.revision.snapshot_digest,
                 "authority_digest": self.authority.authority_digest}
        return response, proof

    def normalize_response(self, response):
        from management.services.ig_revision_conversation_context import normalize_response_delay_apology

        clean = normalize_response_delay_apology(response.reply_text, self.revision)
        if clean == response.reply_text:
            return response
        logging.getLogger(__name__).info(
            "ig_revision_reply_normalized revision=%s reason=unsupported_response_delay_prefix "
            "original_digest=%s normalized_digest=%s",
            self.revision.pk, _digest(response.reply_text), _digest(clean),
        )
        return replace(response, reply_text=clean)

    def validate(self, response, *, policy_manifest):
        from management.services.ig_revision_intents import manager_case_reason, manager_handoff_promised

        publication = (policy_manifest or {}).get("instruction_publication") or {}
        expected = self.publication
        if (
            publication.get("id") != expected.publication_id
            or publication.get("version") != expected.version
            or publication.get("hash") != expected.snapshot_hash
        ):
            self.last_reasons = ("publication_changed",)
            return ValidationDecision(False, self.last_reasons)
        readiness = self.check()
        if not readiness.ready:
            self.last_reasons = readiness.reasons
            return ValidationDecision(False, readiness.reasons)
        # Customer routes produced by this candidate are accepted only after
        # proposal storage. Unknown purpose cannot reject that classification
        # prematurely; the mandatory action/send gates below run after acceptance.
        from management.services.ig_turn_intent import build_turn_intent, source_only_noncommercial, validate_turn_response
        intent = build_turn_intent(IgClient.objects.get(pk=self.revision.client_id), self.revision)
        sources = [row.message for row in self.revision.sources.select_related("message")]
        if intent["purpose"] != "unknown" or source_only_noncommercial(intent, sources):
            intent_reason = validate_turn_response(intent, response.reply_text)
            if intent_reason:
                self.last_reasons = (intent_reason,)
                return ValidationDecision(False, self.last_reasons)
        case_reason = manager_case_reason(self.revision, response)
        if manager_handoff_promised(response) and not case_reason:
            self.last_reasons = ("unnecessary_manager_handoff",)
            return ValidationDecision(False, self.last_reasons)
        if case_reason == "custom_print":
            from management.services.instagram_bot import _provider_reply_truth_context
            from management.services.ig_reply_truth import validate_reply_truth

            context = _provider_reply_truth_context(self.revision.client, response.control, response.reply_text)
            custom_truth = validate_reply_truth(response.reply_text, context=replace(
                context, authorized_prices=(), authorized_price_ranges=(),
                approved_timing_claims=(), explicitly_qualified_standard_dispatch_days=None,
            ))
            if not custom_truth.valid:
                self.last_reasons = custom_truth.reasons
                return ValidationDecision(False, custom_truth.reasons)
        authority = self.response_authority(response)
        readiness = self.check(authority)
        if not readiness.ready:
            self.last_reasons = readiness.reasons
            return ValidationDecision(False, readiness.reasons)
        self.authority = authority
        self.last_reasons = ()
        return ValidationDecision(True)

    def repair(self, payload, parsed, reasons, *, base_repair):
        if self.repaired or "revision_domain_action_unsupported" in reasons or any(
            str(reason).startswith(("checkout", "custom_confirmation")) for reason in reasons
        ):
            return None
        readiness = self.check()
        if not readiness.ready and not set(readiness.reasons).issubset(FACT_DRIFT_REASONS):
            self.last_reasons = readiness.reasons
            return None
        if set(reasons) - FACT_DRIFT_REASONS and any(
            code.startswith(("revision_", "publication_", "public_policy_", "client_", "settings_"))
            or code in {"pending_inbound", "manager_takeover"}
            for code in reasons
        ):
            return None
        if not readiness.ready:
            self.baseline = self._baseline()
            if not self.check().ready:
                return None
        repaired = base_repair(payload, parsed, reasons)
        if repaired is None:
            return None
        from management.services.ig_reply_authority import build_reply_truth_context

        client = IgClient.objects.get(pk=self.revision.client_id)
        facts = asdict(build_reply_truth_context(client))
        repaired.setdefault("contents", []).append({
            "role": "user", "parts": [{"text": (
                "[SERVER FACTS FOR THE SAME SEALED CUSTOMER BUNDLE]\n"
                + json.dumps(facts, ensure_ascii=False, default=list, separators=(",", ":"))
            )}],
        })
        self.repaired = True
        return repaired


def _claim_preparation(revision_id, settings_row):
    """Share the legacy client's claim fence, then capture outside the lock."""
    from management.services.ig_turn_revisions import claim_revision_preparation

    identity = IgCustomerTurnRevision.objects.filter(pk=revision_id).values("client_id").first()
    if not identity:
        return None
    with transaction.atomic():
        current_settings = InstagramBotSettings.objects.select_for_update().filter(
            pk=settings_row.pk, is_enabled=True,
        ).first()
        client = IgClient.objects.select_for_update().filter(pk=identity["client_id"]).first()
        revision = IgCustomerTurnRevision.objects.select_for_update().filter(pk=revision_id).first()
        if current_settings is None or client is None or revision is None:
            return None
        if revision.state == revision.State.COLLECTING:
            from management.services.ig_turn_revisions import _authorized_manual_revision

            allowed_statuses = {InstagramBotMessage.Status.PENDING}
            if _authorized_manual_revision(revision):
                allowed_statuses.add(InstagramBotMessage.Status.DONE)
            sources = list(revision.sources.select_related("message"))
            if not sources or any(
                source.message.status not in allowed_statuses
                or source.message.send_state
                for source in sources
            ):
                return None
        return claim_revision_preparation(revision_id)


def _reclaim_execution(revision_id, *, settings_id=1):
    """Rotate an expired lease, never reconstruct or reset the sealed bundle."""
    from management.services.ig_turn_revisions import EXECUTION_LEASE_SECONDS

    identity = IgCustomerTurnRevision.objects.filter(pk=revision_id).values("client_id").first()
    if not identity:
        return None, ""
    now = timezone.now()
    with transaction.atomic():
        settings_row = InstagramBotSettings.objects.select_for_update().filter(pk=settings_id).first()
        client = IgClient.objects.select_for_update().filter(pk=identity["client_id"]).first()
        revision = IgCustomerTurnRevision.objects.select_for_update().filter(
            pk=revision_id, state=IgCustomerTurnRevision.State.CLAIMED,
            lease_until__lte=now, snapshot_digest__gt="",
        ).first()
        if revision is None:
            return None, ""
        lease_deadline = now + timedelta(seconds=EXECUTION_LEASE_SECONDS)
        if revision.overall_deadline <= now and revision.recovery_code != "provider_dispatch_budget":
            from management.services.ig_revision_recovery import EXECUTION_RESUME_KEY, execution_resume_is_current

            if (not settings_row or not settings_row.is_enabled or not settings_row.ai_enabled
                or not client or client.reply_permission_epoch != revision.permission_epoch
                or client.manager_takeover or client.bot_paused or client.hidden_at or client.is_blocked
                or client.privacy_erasure_started_at is not None or revision.active_slot != 1
                or not execution_resume_is_current(revision, now=now)):
                return None, ""
            receipt = revision.action_receipts[EXECUTION_RESUME_KEY]
            if receipt["settings_id"] != settings_id or receipt["settings_permission_epoch"] != settings_row.reply_permission_epoch:
                return None, ""
            lease_deadline = min(lease_deadline, timezone.datetime.fromisoformat(receipt["expires_at"]))
        token = secrets.token_hex(16)
        revision.claim_token = token
        revision.claimed_at = now
        revision.lease_until = lease_deadline
        revision.save(update_fields=["claim_token", "claimed_at", "lease_until", "updated_at"])
        return revision, token


def _restore_response(proposal):
    from management.services.ig_customer_route_contract import normalize_customer_routes

    response = proposal.get("response") or {}
    routes = normalize_customer_routes(proposal.get("customer_routes"))
    return ValidatedResponse(
        reply_text=response.get("reply_text") or "",
        controls=tuple(ResponseControl(item["kind"], item["value"]) for item in response.get("controls") or []),
        customer_routes=routes.proposal,
        route_abstention_reason=proposal.get("route_abstention_reason") or routes.reason_code,
    )


def _normalize_response(response, artifact, client):
    from management.services.instagram_bot import _apply_turn_intelligence_resolution

    reply, control = _apply_turn_intelligence_resolution(
        response.reply_text, response.control, artifact, client,
    )
    controls = []
    for key, value in control.items():
        if key in {"options", "items"}:
            controls.extend(ResponseControl("option" if key == "options" else "item", item) for item in value)
        else:
            controls.append(ResponseControl(key, value if isinstance(value, bool) else str(value)))
    return replace(response, reply_text=reply, controls=tuple(controls))


def _generate_proposal(revision, token, settings_row, publication, collection):
    from management.services import instagram_bot as bot
    from management.services.ig_revision_proposal import (
        capture_revision_customer_routes, store_revision_generation_proposal,
    )
    from management.services.ig_turn_lineage import Lane, turn_lineage

    boundary = RevisionGenerationBoundary(
        revision, token, settings_row, publication,
        has_images=any(part.mime.startswith("image/") for part in collection.parts),
    )
    readiness = boundary.check()
    if not readiness.ready:
        return None, boundary, readiness.reasons
    from management.services.ig_revision_recovery import record_generation_admission

    admission = record_generation_admission(
        revision.pk, token, settings_id=settings_row.pk,
        settings_permission_epoch=boundary.settings_epoch,
        publication=publication, authority=boundary.baseline,
    )
    if not admission.ready:
        return None, boundary, (admission.reason,)
    sources = revision.bundle_snapshot["sources"]
    source_id = int(sources[-1]["message_id"])
    try:
        route_capture = capture_revision_customer_routes(revision.pk, token,
            settings_id=settings_row.pk, settings_permission_epoch=boundary.settings_epoch,
            publication=publication)
        route_context = route_capture.prompt_context(collection.binding.get("items") or []) if route_capture else None
    except Exception:
        # Optional route infrastructure cannot remove the main reply path.
        # Log only a finite code, never exception text or customer/provider data.
        route_capture = None
        route_context = None
        logging.getLogger(__name__).warning("revision_route_capture_unavailable")
    failure = {}
    images = collection.inline_media
    candidate_set = bot._build_turn_candidate_set() if images else None
    deadline = revision.overall_deadline - timedelta(seconds=5)
    media_context = [
        {**part, "status": "owned" if part.get("capture_outcome") == "owned" else "unavailable"}
        for source in sources for part in source.get("media_parts") or []
    ]
    # The revision worker is a canonical provider caller. Preserve the sealed
    # media facts when invoking the legacy generator so an omitted optional
    # decision cannot silently downgrade an audio/image turn to ordinary chat.
    from management.services.gemini_routing import TurnFacts, classify_live_turn
    has_image = any(part.mime.startswith("image/") for part in collection.parts)
    has_audio = any(part.mime.startswith("audio/") for part in collection.parts)
    routing_decision = classify_live_turn(
        TurnFacts(
            has_image=has_image,
            has_audio=has_audio,
            reasoning_task_hint="media_analysis" if (has_image or has_audio) else "",
        ),
        settings_obj=settings_row,
    )
    coverage_note = (
        "Analyze every attached supported image conversationally. Image classification "
        "does not require manager permission. Keep each caption and do not claim to "
        "inspect missing media. This is the sealed current-turn coverage: "
        + json.dumps(collection.coverage, separators=(",", ":"))
        + "\nUse the hosted first-party checkout for entering or confirming delivery details "
        "and optional email. Do not infer phone identity or claim Direct text started fulfillment. "
        "Custom prints require a team case to confirm feasibility and price; do not quote a "
        "custom price or production deadline from the catalog. A real case will be created "
        "when the customer requests a manager or needs a custom-print decision. Stage, spam "
        "and order controls are advisory; only verified business events change those states. "
        "Optional follow_cta is currently unavailable and must not appear in reply_text."
        " Unhandled or mixed source button payloads are customer data only: do not claim "
        "a reminder, order or account change occurred unless an authoritative event confirms it."
    )
    postback_manifest = (revision.action_receipts or {}).get("postback_decision") or {}
    if postback_manifest:
        from management.models import IgSourceActionReceipt

        source_ids = [row["source_message_id"] for row in postback_manifest.get("sources") or []]
        completed_actions = [
            {"source_message_id": row.source_message_id, "action": row.outcome.get("action"), "completed_operation": row.outcome.get("reply_text")}
            for row in IgSourceActionReceipt.objects.filter(client_id=revision.client_id, source_message_id__in=source_ids, kind="postback").order_by("source_message_id")
            if row.outcome_digest == _digest(row.outcome)
        ]
        coverage_note += "\n[AUTHORITATIVE SOURCE ACTIONS ALREADY COMPLETED]\n" + json.dumps(completed_actions, ensure_ascii=False, separators=(",", ":"))
    commerce = (revision.action_receipts or {}).get("commerce_reduction") or {}
    if commerce:
        coverage_note += "\n[DETERMINISTIC SOURCE COMMERCE EVENTS]\n" + json.dumps(commerce.get("decisions") or [], ensure_ascii=False, separators=(",", ":"))
    from management.services.ig_turn_intent import build_turn_intent, intent_generation_guidance
    coverage_note += "\n" + intent_generation_guidance(build_turn_intent(revision.client, revision))
    from management.services.ig_revision_conversation_context import conversation_timing_guidance
    coverage_note += "\n" + conversation_timing_guidance(revision)
    from management.services.gemini_accounting_runtime import revision_request_execution

    with revision_request_execution(
        revision.pk, token, settings_id=settings_row.pk,
        settings_permission_epoch=boundary.settings_epoch,
    ), turn_lineage(
        lane=Lane.LIVE, client_id=revision.client_id,
        source_message_id=source_id, logical_turn_id=f"ig-revision:{revision.pk}",
    ):
        response = bot.gemini_generate(
            settings_row, build_sealed_history(revision), images=images or None,
            routing_decision=routing_decision,
            client=revision.client, turn_note=coverage_note,
            turn_candidate_set=candidate_set,
            turn_media_binding=collection.binding,
            turn_media_context=media_context,
            failure_context=failure, generation_boundary=boundary,
            deadline_at=deadline,
            customer_route_context=route_context,
        )
    if not isinstance(response, ValidatedResponse) or not response.valid:
        return None, boundary, boundary.last_reasons or (str(failure.get("kind") or "generation_failed"),)
    readiness = boundary.check()
    if not readiness.ready:
        return None, boundary, readiness.reasons
    # Restore the complete submitted-source mapping even if the provider trimmed
    # a suffix. Its actual inline count marks that suffix as uninspected.
    actual_binding = failure.get("request_media_binding")
    actual_binding = actual_binding if isinstance(actual_binding, dict) else {}
    actual_count = actual_binding.get("actual_inline_count")
    if not images and actual_count is None:
        actual_count = 0
    if isinstance(actual_count, bool) or not isinstance(actual_count, int):
        return None, boundary, ("unknown_inline_coverage",)
    actual_hashes = actual_binding.get("actual_content_hashes")
    if not images and actual_hashes is None:
        actual_hashes = []
    if (
        not isinstance(actual_hashes, list)
        or not 0 <= actual_count <= len(collection.parts)
        or len(actual_hashes) != actual_count
        or actual_hashes != [item["content_hash"] for item in collection.binding["items"][:actual_count]]
    ):
        return None, boundary, ("actual_media_binding_mismatch",)
    media_manifest = deepcopy(collection.binding)
    media_manifest["actual_inline_count"] = actual_count
    media_manifest["actual_content_hashes"] = list(actual_hashes)
    normalized = bot._normalize_turn_media_binding(images, collection.binding) or {}
    normalized.update({
        "actual_inline_count": actual_count,
        "actual_content_hashes": media_manifest["actual_content_hashes"],
        "request_id": failure.get("request_id") or "",
        "provider_model": failure.get("model") or "",
    })
    artifact = bot._validated_turn_intelligence(
        response.turn_intelligence, candidate_set, normalized,
        prize_programme=boundary.programme,
    )
    if artifact:
        artifact["request_permission_epoch"] = revision.permission_epoch
    response = _normalize_response(response, artifact, revision.client)
    authority = boundary.response_authority(response)
    readiness = boundary.check(authority)
    if not readiness.ready:
        return None, boundary, readiness.reasons
    from management.services.ig_reply_truth import validate_reply_truth

    truth = validate_reply_truth(response.reply_text, context=bot._provider_reply_truth_context(
        IgClient.objects.get(pk=revision.client_id), response.control, response.reply_text,
    ))
    if not truth.valid:
        return None, boundary, truth.reasons
    stored = store_revision_generation_proposal(
        revision.pk, token, source_message_ids=[source["message_id"] for source in sources],
        settings_id=settings_row.pk, settings_permission_epoch=boundary.settings_epoch,
        publication=publication, request_id=failure.get("request_id") or "",
        actual_model=failure.get("model") or "", generated_at=timezone.now(),
        response=response, turn_intelligence=artifact,
        request_media_manifest=media_manifest,
        policy_manifest=failure.get("compiled_policy") or {}, authority=authority,
        prize_programme=boundary.programme,
        customer_route_capture=route_capture,
    )
    if not stored.stored:
        return None, boundary, stored.reasons
    boundary.authority = authority
    revision.refresh_from_db()
    return response, boundary, ()


def _prepare_visible_content(revision, response, settings_row):
    from management.services import instagram_bot as bot
    from management.services.ig_catalog_media import prepare_catalog_media

    reply = response.reply_text
    effects = []
    selection = bot._catalog_media_selection_for_control(response.control, revision.client)
    if selection is not None:
        prepared_media = prepare_catalog_media(settings_row, revision.client.igsid, selection)
        if not prepared_media.payloads:
            return (), reply, ("catalog_media_unavailable",)
        namespace = revision.bundle_snapshot["sources"][0]["source_namespace"]
        for index, payload in enumerate(prepared_media.payloads):
            if namespace.startswith("legacy_page:"):
                payload = {**payload, "messaging_type": "RESPONSE"}
            effects.append({
                "group": "catalog_media", "kind": "image", "payload": payload,
                "projection_metadata": prepared_media.product_refs[index],
            })
        reply = bot._append_more_products_hint(reply, selection, revision.client)
    return tuple(effects), reply, ()


def _prepare_effects(revision, response, settings_row):
    from management.services import instagram_bot as bot
    from management.services.ig_revision_transport import prepare_text_effects

    prior, reply, reasons = _prepare_visible_content(revision, response, settings_row)
    if reasons:
        return (), reasons
    effects = list(prior)
    from management.services.ig_reply_truth import validate_reply_truth

    truth = validate_reply_truth(reply, context=bot._provider_reply_truth_context(
        IgClient.objects.get(pk=revision.client_id), response.control, reply,
    ))
    if not truth.valid:
        return (), truth.reasons
    prepared_text = prepare_text_effects(
        revision.client.igsid, reply,
        provider_namespace=revision.bundle_snapshot["sources"][0]["source_namespace"],
    )
    if prepared_text.error:
        return (), (prepared_text.error,)
    effects.extend(prepared_text.effects)
    return tuple(effects), ()


def _project_sent_history(revision_id):
    """Project canonical receipts locally; safe for finalization and exact echo."""
    from management.services.ig_revision_reply_projection import project_sent_reply

    return project_sent_reply(revision_id)


def _revision_response_purpose_reason(revision):
    """Recheck current source purpose against the whole immutable reply.

    Checking individual split parts could miss a CTA crossing their boundary.
    This gate has no business mutations and also runs on crash-resumed outboxes.
    """
    from management.services.ig_turn_intent import build_turn_intent, validate_turn_response
    from management.services.ig_revision_outbox import revision_has_newer_source

    current = IgCustomerTurnRevision.objects.select_related("client").get(pk=revision.pk)
    if revision_has_newer_source(current):
        return "pending_inbound"
    text = "\n".join(str((row.payload.get("message") or {}).get("text") or "")
        for row in current.delivery_effects.filter(group="substantive_text").order_by("order_index"))
    if current.delivery_effects.filter(purpose="technical_holding").exists():
        from management.services.ig_revision_holding import holding_receipt_valid
        if not holding_receipt_valid(current, text=text):
            return "holding_handoff_no_longer_open"
    return validate_turn_response(build_turn_intent(current.client, current), text)


def _drain_effects(revision, token, settings_row, access_token):
    # A late advisory HTTP completion is cleaned up separately from this send.
    _stop_revision_presence(dispatch=True)
    from management.services.ig_revision_delivery import drain_group
    from management.services.ig_revision_execution import finalize_sent_revision_effects
    from management.services.ig_revision_transport import build_provider_part_callback

    effect = revision.delivery_effects.order_by("order_index").first()
    if effect is None:
        return RevisionLiveResult(revision.pk, "blocked", ("effects_missing",))
    transport = build_provider_part_callback(
        settings_row, expected_namespace=effect.provider_namespace,
        expected_recipient=effect.recipient_igsid, access_token=access_token,
        response_gate=lambda _payload: _revision_response_purpose_reason(revision),
    )
    attempted = 0
    for group in dict.fromkeys(revision.delivery_effects.order_by("order_index").values_list("group", flat=True)):
        outcome = drain_group(
            revision.pk, token, group, transport,
            fact_checker=check_fact_bindings, offer_checker=check_offer_bindings,
        )
        attempted += outcome.attempted
        # Media and text have independent debt; UNKNOWN only stops siblings in
        # its own group. Every subsequent group still performs the full CAS.
    states = list(revision.delivery_effects.values_list("state", flat=True))
    if states and set(states).issubset({"cancelled", "superseded"}):
        reasons = tuple(dict.fromkeys(revision.delivery_effects.exclude(
            failure_code="",
        ).order_by("order_index").values_list("failure_code", flat=True)))
        # No receipt discharged this customer's owed reply. Keep the claim so
        # the caller can create its bounded refresh successor without changing
        # the original source status or inventing a successful old-engine turn.
        project_legacy_message(revision.pk)
        return RevisionLiveResult(revision.pk, "cancelled", reasons, attempted, 0)
    sent = revision.delivery_effects.filter(state=IgRevisionDeliveryEffect.State.SENT).count()
    if not sent:
        project_legacy_message(revision.pk)
        return RevisionLiveResult(revision.pk, "delivery_pending", tuple(dict.fromkeys(states)), attempted, 0)
    completion = finalize_sent_revision_effects(revision.pk, execution_token=token)
    return RevisionLiveResult(
        revision.pk, "completed" if completion.completed else "finalization_pending" if completion.retryable else "delivery_pending",
        (completion.reason,), attempted, sent,
    )


def _project_completed_sources(revision_id):
    """Recover local acknowledgement after a crash following completion."""
    identity = IgCustomerTurnRevision.objects.filter(pk=revision_id).values("client_id").first()
    if not identity:
        return False
    with transaction.atomic():
        client = IgClient.objects.select_for_update().filter(pk=identity["client_id"]).first()
        revision = IgCustomerTurnRevision.objects.select_for_update().filter(
            pk=revision_id, state=IgCustomerTurnRevision.State.PROCESSED,
        ).first()
        if client is None or revision is None or client.privacy_erasure_started_at is not None:
            return False
        states = set(revision.delivery_effects.values_list("state", flat=True))
        if "sent" not in states or not states.issubset({"sent", "cancelled", "superseded"}):
            return False
        turn = IgCustomerTurn.objects.select_for_update().filter(pk=revision.turn_id).first()
        source_ids = list(revision.sources.values_list("message_id", flat=True))
        updated = InstagramBotMessage.objects.filter(
            pk__in=source_ids, client=client,
            status__in=(InstagramBotMessage.Status.PENDING, InstagramBotMessage.Status.PROCESSING),
        ).update(status=InstagramBotMessage.Status.DONE, processed_at=timezone.now())
        if turn and not turn.turn_messages.exclude(message_id__in=source_ids).filter(
            message__status__in=(InstagramBotMessage.Status.PENDING, InstagramBotMessage.Status.PROCESSING),
        ).exists():
            IgCustomerTurn.objects.filter(pk=turn.pk, terminal_reason="").update(
                claim_state=IgCustomerTurn.ClaimState.PROCESSED,
                terminal_reason=IgCustomerTurn.TerminalReason.REPLIED,
                processed_at=timezone.now(), updated_at=timezone.now(),
            )
        return bool(updated)


def _execute_deterministic_input(revision, token, settings_row, receipt, *, quick_replies=()):
    from management.services import instagram_bot as bot
    from management.services.ig_revision_input import _digest as input_digest
    from management.services.ig_reply_truth import validate_reply_truth
    from management.services.ig_revision_transport import prepare_text_effects

    if receipt.get("origin") == "static_reply" and input_digest({
        "ai_enabled": bool(settings_row.ai_enabled),
        "trigger_text": settings_row.trigger_text, "reply_text": settings_row.reply_text,
    }) != receipt.get("static_settings_digest"):
        return RevisionLiveResult(revision.pk, "blocked", ("static_settings_changed",))
    authority = _authority_from_projection(receipt["authority"])
    pub = receipt["publication"]
    publication = PublicationBinding(pub["id"], pub["version"], pub["hash"])
    reply = receipt.get("static_reply_text") or receipt.get("reply_text") or ""
    truth = validate_reply_truth(reply, context=bot._provider_reply_truth_context(revision.client, {}, reply))
    if not truth.valid:
        return RevisionLiveResult(revision.pk, "blocked", truth.reasons)
    prepared = prepare_text_effects(
        revision.client.igsid, reply, quick_replies=quick_replies,
        provider_namespace=revision.bundle_snapshot["sources"][0]["source_namespace"],
    )
    if prepared.error:
        return RevisionLiveResult(revision.pk, "blocked", (prepared.error,))
    access_token = bot.get_page_token(settings_row)
    if not access_token:
        return RevisionLiveResult(revision.pk, "blocked", ("provider_not_configured",))
    plan = plan_revision_effects(
        revision.pk, token, source_message_id=(receipt.get("source_message_id") or receipt["source_message_ids"][-1]),
        settings_id=receipt["settings_id"], settings_permission_epoch=receipt["settings_permission_epoch"],
        publication=publication, authority_context_digest=authority.authority_digest,
        effects=prepared.effects, fact_bindings=authority.fact_bindings,
        fact_checker=check_fact_bindings, offer_checker=check_offer_bindings,
        purpose=receipt.get("purpose") or "normal_reply",
    )
    if not plan.effects:
        return RevisionLiveResult(revision.pk, "blocked", plan.reasons)
    return _drain_effects(revision, token, settings_row, access_token)


def execute_claimed_revision(revision_id, token, settings_row, *, presence_scope=None) -> RevisionLiveResult:
    scope = presence_scope if presence_scope is not None else {"lease": "", "handle": None}
    context_token = _PRESENCE_SCOPE.set(scope)
    try:
        outcome = _execute_claimed_revision(revision_id, token, settings_row)
        if outcome.sent_parts > 0:
            _stop_revision_presence(scope, sent=True)
        return outcome
    finally:
        _stop_revision_presence(scope)
        _PRESENCE_SCOPE.reset(context_token)


def _execute_claimed_revision(revision_id, token, settings_row) -> RevisionLiveResult:
    """Execute an explicitly claimed revision; never delegates to legacy sends."""
    if connection.in_atomic_block:
        return RevisionLiveResult(revision_id, "blocked", ("caller_transaction_active",))
    from management.services import instagram_bot as bot

    settings_row = InstagramBotSettings.objects.select_related("active_instruction_publication").get(pk=settings_row.pk)
    revision = IgCustomerTurnRevision.objects.select_related("client").filter(pk=revision_id).first()
    if revision is None:
        return RevisionLiveResult(revision_id, "blocked", ("revision_missing",))
    if bot.ingress_provider_namespace(settings_row) != revision.bundle_snapshot.get("sources", [{}])[0].get("source_namespace"):
        return RevisionLiveResult(revision_id, "blocked", ("provider_namespace_changed",))
    if revision.delivery_effects.exists():
        token_value = bot.get_page_token(settings_row)
        if not token_value:
            return RevisionLiveResult(revision_id, "blocked", ("provider_not_configured",))
        return _drain_effects(revision, token, settings_row, token_value)
    from management.services.ig_revision_input import decide_revision_input, complete_no_reply_input

    # A parked creator budget debt has no model winner and may outlive the
    # generation deadline. Admit only the local, audited holding reply; the
    # normal input classifier must not reject it before that narrow path runs.
    if revision.recovery_code == "provider_dispatch_budget":
        from management.services.ig_revision_holding import record_technical_holding

        holding = record_technical_holding(revision.pk, token, settings_id=settings_row.pk)
        if holding.ready:
            return _execute_deterministic_input(revision, token, settings_row, holding.receipt)
        return RevisionLiveResult(revision_id, "blocked", (holding.reason or "collaboration_holding_unavailable",))

    input_decision = decide_revision_input(revision.pk, token, settings_id=settings_row.pk)
    if not input_decision.ready:
        return RevisionLiveResult(revision_id, "blocked", (input_decision.reason,))
    revision.refresh_from_db()
    from management.services.ig_revision_followups import cancel_revision_sales_timers

    input_pub = input_decision.receipt["publication"]
    holding_recovery = revision.recovery_code == "provider_dispatch_budget"
    if holding_recovery:
        input_pub = {
            "id": settings_row.active_instruction_publication.pk,
            "version": settings_row.active_instruction_publication.version,
            "hash": settings_row.active_instruction_publication.snapshot_hash,
        }
    cancelled_timers = cancel_revision_sales_timers(
        revision.pk, token, settings_id=settings_row.pk,
        settings_permission_epoch=(
            settings_row.reply_permission_epoch
            if holding_recovery else input_decision.receipt["settings_permission_epoch"]
        ),
        publication=PublicationBinding(input_pub["id"], input_pub["version"], input_pub["hash"]),
        allow_expired_holding=(revision.recovery_code == "provider_dispatch_budget"),
    )
    if not cancelled_timers.ready:
        return RevisionLiveResult(revision_id, "blocked", (cancelled_timers.reason,))
    if input_decision.origin == "no_reply":
        if input_decision.reason == "rate_limited":
            from management.services.ig_revision_intents import ensure_revision_rate_alert

            notification = ensure_revision_rate_alert(revision.pk, token, settings_id=settings_row.pk)
            if not notification.ready:
                return RevisionLiveResult(revision_id, "blocked", (notification.reason,))
        consumed = complete_no_reply_input(revision.pk, token)
        return RevisionLiveResult(revision_id, "completed" if consumed else "blocked", (input_decision.reason,))
    _start_revision_presence(revision, token, settings_row, _PRESENCE_SCOPE.get())
    if input_decision.origin == "static_reply":
        return _execute_deterministic_input(revision, token, settings_row, input_decision.receipt)
    if input_decision.origin == "postback":
        from management.services.ig_revision_postbacks import apply_revision_postback

        pub = input_decision.receipt["publication"]
        postback_results = []
        for source in revision.bundle_snapshot["sources"]:
            if not source.get("quick_reply_payload"):
                continue
            postback = apply_revision_postback(
                revision.pk, token, source_message_id=source["message_id"],
                settings_id=settings_row.pk,
                settings_permission_epoch=input_decision.receipt["settings_permission_epoch"],
                publication=PublicationBinding(pub["id"], pub["version"], pub["hash"]),
            )
            if postback.handled:
                if not postback.ready:
                    return RevisionLiveResult(revision_id, "blocked", (postback.reason,))
                postback_results.append(postback)
        if postback_results and all(not result.requires_model for result in postback_results):
            postback = postback_results[-1]
            return _execute_deterministic_input(revision, token, settings_row, postback.receipt, quick_replies=postback.quick_replies)
        revision.refresh_from_db()
    if not settings_row.ai_enabled or (
        input_decision.receipt["settings_permission_epoch"] != settings_row.reply_permission_epoch
        and not holding_recovery
    ):
        return RevisionLiveResult(revision_id, "blocked", ("settings_permission_changed",))
    from management.services.ig_revision_commerce import reduce_revision_commerce

    commerce = reduce_revision_commerce(
        revision.pk, token, settings_id=settings_row.pk,
        settings_permission_epoch=input_decision.receipt["settings_permission_epoch"],
        publication=PublicationBinding(input_pub["id"], input_pub["version"], input_pub["hash"]),
    )
    if not commerce.ready:
        return RevisionLiveResult(revision_id, "blocked", (commerce.reason,))
    revision.refresh_from_db()
    publication = _publication(settings_row)
    if revision.generation_proposal_digest:
        if _digest(revision.generation_proposal) != revision.generation_proposal_digest:
            return RevisionLiveResult(revision_id, "blocked", ("generation_proposal_changed",))
        response = _restore_response(revision.generation_proposal)
        execution = revision.generation_proposal.get("execution_binding") or {}
        if execution.get("settings_id") != settings_row.pk or not isinstance(execution.get("settings_permission_epoch"), int):
            return RevisionLiveResult(revision_id, "blocked", ("proposal_execution_binding_missing",))
        boundary = RevisionGenerationBoundary(revision, token, settings_row, publication)
        boundary.settings_epoch = execution["settings_permission_epoch"]
        boundary.authority = _authority_from_projection(revision.generation_proposal["authority"])
        original_publication = revision.generation_proposal["policy_manifest"]["instruction_publication"]
        publication = PublicationBinding(
            original_publication["id"], original_publication["version"], original_publication["hash"],
        )
        boundary.publication = publication
    else:
        # A crashed request without its successful proposal is reconciliation
        # debt, never an excuse to open another two-dispatch request.
        if GeminiRequest.objects.filter(logical_turn_id=f"ig-revision:{revision.pk}").exists():
            from management.services.ig_revision_input import record_source_preference_fallback

            fallback = record_source_preference_fallback(revision.pk, token, settings_id=settings_row.pk)
            if fallback.ready:
                return _execute_deterministic_input(revision, token, settings_row, fallback.receipt)
            from management.services.ig_revision_holding import record_technical_holding

            holding = record_technical_holding(
                revision.pk, token, settings_id=settings_row.pk, allow_neutral=True,
            )
            if holding.ready:
                return _execute_deterministic_input(revision, token, settings_row, holding.receipt)
            return RevisionLiveResult(revision_id, "blocked", ("generation_reconciliation_required",))
        from management.services.ig_revision_media import collect_revision_media

        collection = collect_revision_media(revision.pk, token)
        if collection.readiness not in {"ready", "partial", "unavailable", "no_media"}:
            return RevisionLiveResult(revision_id, "blocked", collection.reasons)
        from management.services.ig_revision_input import record_unavailable_media_reply

        clarification = record_unavailable_media_reply(revision.pk, token, settings_id=settings_row.pk, collection=collection)
        if clarification.ready:
            return _execute_deterministic_input(revision, token, settings_row, clarification.receipt)
        if clarification.reason != "media_clarification_not_applicable":
            return RevisionLiveResult(revision_id, "blocked", (clarification.reason,))
        response, boundary, reasons = _generate_proposal(
            revision, token, settings_row, publication, collection,
        )
        if response is None:
            from management.services.ig_revision_input import record_source_preference_fallback

            fallback = record_source_preference_fallback(revision.pk, token, settings_id=settings_row.pk)
            if fallback.ready:
                return _execute_deterministic_input(revision, token, settings_row, fallback.receipt)
            from management.services.ig_revision_holding import record_technical_holding

            holding = record_technical_holding(
                revision.pk, token, settings_id=settings_row.pk, allow_neutral=True,
            )
            if holding.ready:
                return _execute_deterministic_input(revision, token, settings_row, holding.receipt)
            return RevisionLiveResult(revision_id, "blocked", reasons)
    # Both fresh generation and execution-only continuation use the same sealed
    # proposal. Topic acceptance is optional and never authorizes send/actions.
    if revision.generation_proposal.get("customer_routes") is not None:
        try:
            from management.services.ig_conversation_routes import (
                RevisionRouteSource, accept_customer_routes,
            )

            route_result = accept_customer_routes(RevisionRouteSource(
                client_id=revision.client_id, settings_id=settings_row.pk,
                revision_id=revision.pk, revision_token=token,
                expected_source_digest=revision.generation_proposal_digest,
            ), expected_previous_decision_id=revision.generation_proposal.get(
                "route_expected_previous_decision_id"))
            if not route_result.accepted:
                # Existing business/send fences still run below. A route-only
                # CAS rejection must not discard a valid conversational reply.
                bot.log("info", "revision_route_abstained", route_result.reason_code)
        except Exception:
            logging.getLogger(__name__).warning("revision_route_acceptance_unavailable")
    authority = boundary.authority
    from management.services.ig_turn_intent import build_turn_intent, validate_turn_response
    intent_reason = validate_turn_response(
        build_turn_intent(IgClient.objects.get(pk=revision.client_id), revision),
        response.reply_text, authority.allowed_actions,
    )
    if intent_reason:
        return RevisionLiveResult(revision_id, "blocked", (intent_reason,))
    if "manager_escalation_intent" in authority.allowed_actions:
        from management.services.ig_revision_intents import ensure_revision_manager_case

        case = ensure_revision_manager_case(revision.pk, token, settings_id=settings_row.pk)
        if not case.ready:
            return RevisionLiveResult(revision_id, "blocked", (case.reason,))
        revision.refresh_from_db()
    # Prize review is evaluated before selection changes. A restart with a
    # selection receipt skips re-running its pre-selection prize authority.
    if "client_configuration_update" not in revision.action_receipts:
        from management.services.ig_revision_proposal import project_revision_image_inspections

        image_items = (revision.generation_proposal.get("request_media_manifest") or {}).get("items") or []
        if any(str(item.get("mime") or "").startswith("image/") for item in image_items):
            projection = project_revision_image_inspections(revision.pk, token)
            if projection.reasons:
                return RevisionLiveResult(revision_id, "blocked", projection.reasons)
        if "prize_review_case_create" in authority.allowed_actions:
            from management.services.ig_policy_publication import load_active_policy_snapshot
            from management.services.ig_prize_programme import active_shooting_prize_programme
            from management.services.ig_prize_cases import upsert_prize_review_case

            programme = active_shooting_prize_programme(
                publication_snapshot=load_active_policy_snapshot(settings_row),
            )
            for source in revision.bundle_snapshot["sources"]:
                upsert_prize_review_case(
                    int(source["message_id"]), programme=programme,
                    revision_id=revision.pk, revision_token=token,
                    expected_generation_proposal_digest=revision.generation_proposal_digest,
                    settings_id=settings_row.pk,
                    settings_permission_epoch=boundary.settings_epoch, publication=publication,
                )
    if "client_configuration_update" in authority.allowed_actions:
        from management.services.ig_revision_actions import apply_revision_selection_actions

        action = apply_revision_selection_actions(
            revision.pk, token,
            source_message_id=int(revision.bundle_snapshot["sources"][-1]["message_id"]),
            settings_id=settings_row.pk, settings_permission_epoch=boundary.settings_epoch,
            publication=publication, generation_proposal_digest=revision.generation_proposal_digest,
            authority=authority,
        )
        if not (action.applied or action.already_applied) or action.post_action_authority is None:
            return RevisionLiveResult(revision_id, "blocked", action.reasons)
        authority = action.post_action_authority
        revision.client.refresh_from_db()
    readiness = boundary.check(authority)
    if not readiness.ready:
        return RevisionLiveResult(revision_id, "blocked", readiness.reasons)
    checkout_requested = "checkout_proposal_create" in authority.allowed_actions
    if checkout_requested:
        effects, checkout_reply, reasons = _prepare_visible_content(revision, response, settings_row)
    else:
        effects, reasons = _prepare_effects(revision, response, settings_row)
    if reasons:
        return RevisionLiveResult(revision_id, "blocked", reasons)
    # Credentials may perform provider I/O; obtain them before the short atomic
    # plan commit. No raw token is persisted in the revision or its effects.
    access_token = bot.get_page_token(settings_row)
    if not access_token:
        return RevisionLiveResult(revision_id, "blocked", ("provider_not_configured",))
    generation = revision.generation_proposal["generation"]
    if checkout_requested:
        from management.services.ig_revision_checkout import authorize_revision_checkout, prepare_revision_checkout

        checkout_source = next((
            source for source in reversed(revision.bundle_snapshot["sources"])
            if source.get("role") == "user" and not authorize_revision_checkout(
                revision.client, response.control, source.get("text") or "",
                sealed_sources=revision.bundle_snapshot["sources"],
            )[1]
        ), None)
        if checkout_source is None:
            return RevisionLiveResult(revision_id, "blocked", ("checkout_purchase_authority_missing",))
        checkout = prepare_revision_checkout(
            revision.pk, token, source_message_id=int(checkout_source["message_id"]),
            settings_id=settings_row.pk, settings_permission_epoch=boundary.settings_epoch,
            publication=publication, generation_proposal_digest=revision.generation_proposal_digest,
            authority=authority, noncheckout_effects=effects, reply_text=checkout_reply,
        )
        if not checkout.planned:
            return RevisionLiveResult(revision_id, "blocked", checkout.reasons)
        return _drain_effects(revision, token, settings_row, access_token)
    from management.services.ig_revision_catalog_assets import prepare_catalog_asset_control
    from management.services.ig_revision_authority import CLAIM_CATALOG_ASSET_REFERENCES

    asset_control, asset_reasons = prepare_catalog_asset_control(revision.client, effects, control=response.control)
    if asset_reasons:
        return RevisionLiveResult(revision_id, "blocked", asset_reasons)
    if asset_control:
        assets = build_revision_authority_bindings(
            revision.client, claims=(CLAIM_CATALOG_ASSET_REFERENCES,),
            control=asset_control, settings_obj=settings_row,
        )
        if not assets.ready:
            return RevisionLiveResult(revision_id, "blocked", assets.reasons)
        facts = (*authority.fact_bindings, *assets.fact_bindings)
        authority = replace(authority, fact_bindings=facts, authority_digest=_digest(sorted((*facts, *authority.offer_bindings), key=lambda item: item["claim"])))
    planned = plan_revision_effects(
        revision.pk, token,
        source_message_id=int(revision.bundle_snapshot["sources"][-1]["message_id"]),
        settings_id=settings_row.pk, settings_permission_epoch=boundary.settings_epoch,
        publication=publication, authority_context_digest=authority.authority_digest,
        effects=effects, fact_bindings=authority.fact_bindings, offer_bindings=authority.offer_bindings,
        fact_checker=check_fact_bindings, offer_checker=check_offer_bindings,
        generation_request_id=generation["request_id"], generation_model=generation["actual_model"],
    )
    if not planned.effects:
        return RevisionLiveResult(revision_id, "blocked", planned.reasons)
    return _drain_effects(revision, token, settings_row, access_token)


def process_revision_finalizations(*, max_items=15):
    from management.services.ig_revision_execution import finalization_due_ids, finalize_sent_revision_effects

    if int(max_items) <= 0 or connection.in_atomic_block:
        return 0
    handled = 0
    for revision_id in finalization_due_ids(limit=max_items):
        outcome = finalize_sent_revision_effects(revision_id)
        handled += int(outcome.completed)
    return handled


def process_pending_revisions(settings_row, *, max_items=15, create_new=True) -> int:
    """Bounded optional worker; flag rollback only resumes canonical effects."""
    from management.services import instagram_bot as bot
    from management.services.ig_revision_execution import due_revision_ids, prepare_revision, expired_revision_debt_ids, record_expired_revision_debt

    limit = max(0, min(int(max_items), 50))
    if not limit or connection.in_atomic_block:
        return 0
    handled = process_revision_finalizations(max_items=limit)
    from management.services.ig_revision_rollout import revision_execution_rollout

    rollout = revision_execution_rollout()
    create_new = bool(create_new and rollout.enabled)
    recover_new = create_new
    if recover_new:
        # Provider-budget debt normally relinquishes its lease and therefore
        # falls out of the sealed-revision queue. Creator offers are safe to
        # recover with the immutable local holding reply, so reclaim them in a
        # narrow, idempotent pass before ordinary generation recovery.
        from management.services.ig_revision_holding import (
            claim_parked_collaboration_revision, parked_collaboration_revision_ids,
        )

        for revision_id in parked_collaboration_revision_ids(limit=limit):
            identity = IgCustomerTurnRevision.objects.filter(pk=revision_id).values("client_id").first()
            if not identity or bot.maintenance_status()["active"]:
                continue
            client, lease = bot.acquire_client_automation_lease(identity["client_id"])
            if client is None:
                continue
            presence_scope = {"lease": lease, "handle": None}
            try:
                revision, token = claim_parked_collaboration_revision(
                    revision_id, settings_id=settings_row.pk,
                )
                if revision is None or not token:
                    continue
                outcome = execute_claimed_revision(
                    revision_id, token, settings_row, presence_scope=presence_scope,
                )
                if outcome.state == "completed":
                    handled += 1
                elif outcome.reasons:
                    bot.log(
                        "warning", "revision_collaboration_recovery",
                        f"revision={revision_id} state={outcome.state} reasons={','.join(outcome.reasons)}",
                    )
            except Exception as exc:
                bot.log("error", "revision_collaboration_recovery", f"revision={revision_id} error={type(exc).__name__}")
            finally:
                _stop_revision_presence(presence_scope)
                bot.release_client_automation_lease(identity["client_id"], lease)
    for revision_id in expired_revision_debt_ids(
        limit=limit,
        owned_only=not create_new,
        cutover_at=rollout.cutover_at if create_new else None,
    ):
        recovery = None
        if recover_new and not IgRevisionDeliveryEffect.objects.filter(revision_id=revision_id, state__in=("sent", "provider_started", "unknown", "definite_failed")).exists():
            from management.services.ig_revision_recovery import schedule_revision_recovery

            recovery = schedule_revision_recovery(revision_id)
        if recovery is None or recovery.state not in {"waiting", "spawned", "cancelled", "execution"}:
            record_expired_revision_debt(
                revision_id,
                cutover_at=rollout.cutover_at if create_new else None,
            )
    if recover_new:
        from management.services.ig_revision_recovery import due_recovery_revision_ids, prepare_outage_recovery

        for revision_id in due_recovery_revision_ids(limit=limit):
            recovery = prepare_outage_recovery(revision_id)
            if recovery.state == "manual":
                record_expired_revision_debt(
                    revision_id,
                    cutover_at=rollout.cutover_at if create_new else None,
                )
    candidates = list(IgCustomerTurnRevision.objects.filter(
        state=IgCustomerTurnRevision.State.CLAIMED, lease_until__lte=timezone.now(),
    ).filter(Q(overall_deadline__gt=timezone.now()) | Q(recovery_state="execution"))
        .exclude(claim_token__startswith="finalize:").exclude(claim_token__startswith="debt:")
        .filter(Q(delivery_effects__isnull=False) | (Q(active_slot=1) if create_new else Q(pk__in=[])))
        .order_by("lease_until", "id").values_list("id", flat=True).distinct()[:limit])
    if create_new:
        candidates.extend(due_revision_ids(limit=limit, cutover_at=rollout.cutover_at))
    for revision_id in list(dict.fromkeys(candidates))[:max(0, limit - handled)]:
        if bot.maintenance_status()["active"]:
            break
        identity = IgCustomerTurnRevision.objects.filter(pk=revision_id).values("client_id", "state").first()
        if not identity:
            continue
        client, lease = bot.acquire_client_automation_lease(identity["client_id"])
        if client is None:
            continue
        presence_scope = {"lease": lease, "handle": None}
        try:
            if identity["state"] == IgCustomerTurnRevision.State.CLAIMED:
                revision, token = _reclaim_execution(revision_id, settings_id=settings_row.pk)
                if revision is None:
                    continue
            else:
                preparation_token = ""
                if identity["state"] in {IgCustomerTurnRevision.State.COLLECTING, IgCustomerTurnRevision.State.PREPARING}:
                    preparation = _claim_preparation(revision_id, settings_row)
                    if preparation is None or not preparation.token:
                        continue
                    preparation_token = preparation.token

                def capture_with_presence(**capture_kwargs):
                    try:
                        revision = IgCustomerTurnRevision.objects.select_related("client").get(pk=revision_id)
                        if preparation_token and _preparation_presence_admitted(revision, settings_row):
                            _start_revision_presence(revision, preparation_token, settings_row, presence_scope, preparing=True)
                    except Exception:
                        logging.getLogger(__name__).info("ig_presence outcome=preparation_unavailable")
                    try:
                        return capture_revision_source(**capture_kwargs)
                    finally:
                        handle = presence_scope.get("handle")
                        if handle is not None:
                            # Keep the generation across seal->claim. No owner
                            # predicate/I/O runs until the exact token is updated.
                            handle.suspend()

                prepared = prepare_revision(
                    revision_id, capture_with_presence, preparation_token=preparation_token,
                )
                if not prepared.ready:
                    continue
                token = prepared.execution_token
            outcome = execute_claimed_revision(revision_id, token, settings_row, presence_scope=presence_scope)
            if outcome.state == "completed":
                handled += 1
            elif outcome.reasons:
                if create_new:
                    successor_reason = next((
                        code for code in ("publication_changed", "public_policy_inputs_stale")
                        if code in outcome.reasons
                    ), "")
                    if not successor_reason and set(outcome.reasons).issubset(FACT_DRIFT_REASONS):
                        successor_reason = "fact_binding_stale"
                    if successor_reason:
                        from management.services.ig_turn_revisions import create_refresh_successor

                        create_refresh_successor(revision_id, token, reason=successor_reason)
                bot.log("warning", "revision_execution", f"revision={revision_id} state={outcome.state} reasons={','.join(outcome.reasons)}")
        except Exception as exc:
            # A durable lease/proposal/effect remains the recovery authority.
            # No source attempts, transcript, or legacy send key are reset.
            bot.log("error", "revision_execution", f"revision={revision_id} error={type(exc).__name__}")
        finally:
            _stop_revision_presence(presence_scope)
            bot.release_client_automation_lease(identity["client_id"], lease)
    return handled
