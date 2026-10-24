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
import re
import secrets

from django.conf import settings as django_settings
from django.core.serializers.json import DjangoJSONEncoder
from django.db import DatabaseError, connection, transaction
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
        from management.services.ig_ingress_source_proof import presence_source_current
        from management.services.ig_revision_outbox import _revision_namespace

        now = timezone.now()
        query = IgCustomerTurnRevision.objects.filter(
            pk=revision.pk, client_id=revision.client_id, active_slot=1,
            state=state, claim_token=token, lease_until__gt=now,
        )
        if revision.recovery_code == "provider_dispatch_budget":
            query = query.filter(recovery_state="manual")
        else:
            query = query.filter(Q(overall_deadline__gt=now) | Q(recovery_state="execution"))
        return query.exists() and presence_source_current(
            _revision_namespace(revision), revision.client_id, revision.client.igsid, watermark)

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


def _unanswered_generation_result(revision, reasons):
    """After safe replies abstain, settle a proved semantic stop immediately."""
    from management.services.ig_revision_recovery import (
        schedule_revision_recovery, terminal_semantic_failure_reason,
    )

    if terminal_semantic_failure_reason(revision):
        recovery = schedule_revision_recovery(revision.pk)
        if recovery.state == "manual":
            reasons = tuple(dict.fromkeys((*reasons, recovery.reason)))
    return RevisionLiveResult(revision.pk, "blocked", tuple(reasons))


def _focused_selection_clarification(plan, language):
    """A proved current fit preference advances only its missing-size question."""
    candidate = plan.configuration.get("focus_selection_candidate") or {}
    controls = candidate.get("candidate_controls") or {}
    fit = controls.get("fit")
    if (candidate.get("schema") != "focused-selection-candidate.v1" or not candidate.get("digest")
        or candidate.get("next_selector_hint") != "size" or plan.next_selector != "size"
        or set(controls) != {"product", "fit"} or fit not in {"classic", "oversize"}
        or language not in {"uk", "ru", "en"}):
        return ""
    label = {"uk": {"classic": "класична посадка", "oversize": "оверсайз"},
        "ru": {"classic": "классическая посадка", "oversize": "оверсайз"},
        "en": {"classic": "classic fit", "oversize": "oversize"}}[language][fit]
    return {"uk": f"Врахую ваше побажання: {label}. Принт залишається той самий. Який розмір ви зазвичай носите?",
        "ru": f"Учту ваше пожелание: {label}. Принт остаётся тот же. Какой размер вы обычно носите?",
        "en": f"I will use your preference: {label}. Keeping the same print. What size do you usually wear?"}[language]


def _pure_catalog_chart_request(revision):
    """A full own-source chart request, without additional commerce questions."""
    sources = (revision.bundle_snapshot or {}).get("sources") or []
    command = r"(?:(?:скин\w*|надішл\w*|надсил\w*|покаж\w*|дайте|пришл\w*|отправ\w*|send|show)(?:\s+мені|\s+мне|\s+me)?\s+)?"
    chart = r"(?:розмірн\w*\s+сіт\w*|розмірн\w*\s+таблиц\w*|таблиц\w*\s+(?:розмір\w*|размер\w*)|размерн\w*\s+(?:сет\w*|таблиц\w*)|(?:the\s+)?size\s+(?:chart|guide))"
    return bool(sources and all(row.get("role") == "user" and not row.get("media_parts")
        and not row.get("quick_reply_payload") and re.fullmatch(
            r"\s*" + command + chart + r"(?:\s*,?\s*(?:будь ласка|пожалуйста|please))?[.!?]*\s*",
            str(row.get("text") or ""), re.I) for row in sources))


def _local_catalog_chart_candidate(revision, language):
    """DB-only exact chart evidence; the normal asset producer prepares its file."""
    if not _pure_catalog_chart_request(revision) or language not in {"uk", "ru", "en"}:
        return "", {}
    from management.services.ig_product_focus import capture_discussed_configuration
    from management.services.ig_catalog_configured_choices import capture_configured_choices
    certificate = capture_discussed_configuration(revision)
    if not certificate:
        return "", {}
    capture = capture_configured_choices(certificate["product_id"], fit_code=certificate["fit_code"], metadata_only=True)
    grid = capture.get("grid") or {}
    if capture.get("error") or len(capture.get("configurations") or ()) != 1 or not grid.get("grid_id") or not grid.get("image_name"):
        return "", {}
    text = {"uk": "Розмірна сітка для цього принта й обговореної посадки.",
        "ru": "Размерная сетка для этого принта и обсуждаемой посадки.",
        "en": "Size chart for this print and the discussed fit."}[language]
    return text, {"presentation_context_digest": certificate["proof_digest"],
        "configuration_digest": capture["configuration_digest"], "grid_id": grid["grid_id"],
        "image_name_digest": _digest(grid["image_name"])}


def revision_execution_enabled() -> bool:
    from management.services.ig_revision_rollout import revision_execution_rollout

    return revision_execution_rollout().enabled


def _owned_revisions():
    from management.services.ig_revision_source_coverage import source_transfer_owned_q
    from management.services.ig_revision_commerce import source_producer_owned_q

    # media_prepare_deadline is written only by an explicit preparation claim,
    # and survives supersession. A plain ingress shadow has none of these.
    return IgCustomerTurnRevision.objects.filter(
        Q(media_prepare_deadline__isnull=False)
        | Q(sealed_at__isnull=False)
        | Q(snapshot_digest__gt="")
        | Q(generation_proposal_digest__gt="")
        | Q(delivery_effects__isnull=False)
        | source_transfer_owned_q()
        | source_producer_owned_q()
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


def capture_revision_source(*, message_id, deadline_at, remaining_seconds,
                            revision_id=None, preparation_token=""):
    """Capture the original source under the preparation's real wall deadline."""
    if connection.in_atomic_block:
        raise ValueError("caller_transaction_active")
    if remaining_seconds <= 0 or deadline_at <= timezone.now():
        return
    row = InstagramBotMessage.objects.select_related("client").filter(
        pk=message_id, role=InstagramBotMessage.Role.USER,
        client__privacy_erasure_started_at__isnull=True,
    ).first()
    from management.services.ig_ingress_source_proof import is_current_customer_source

    if row is None or not is_current_customer_source(row):
        return
    # Poll proof admits current text to the canonical reducer, not media byte
    # recapture. Richer webhook delivery retains ownership of that operation.
    if row.source == "poll":
        return
    from management.services.instagram_bot import _capture_message_media

    kwargs = {"deadline_at": deadline_at}
    if revision_id is not None and preparation_token:
        def still_owned():
            from management.services.ig_turn_revisions import preparation_claim_is_current
            from management.services.ig_revision_outbox import _active_opt_out

            now = timezone.now()
            if deadline_at <= now or not preparation_claim_is_current(revision_id, preparation_token, now=now):
                return False
            owner = IgCustomerTurnRevision.objects.select_related("client").filter(pk=revision_id).first()
            if owner is None:
                return False
            client = owner.client
            if (client.reply_permission_epoch != owner.permission_epoch or client.hidden_at
                or client.is_blocked or client.bot_paused or client.manager_takeover
                or client.privacy_erasure_started_at is not None or _active_opt_out(client)):
                return False
            ids = list(owner.sources.values_list("message_id", flat=True))
            from management.services.ig_ingress_source_proof import newer_customer_sources
            from management.services.ig_revision_outbox import _revision_namespace

            return bool(message_id in ids and ids and not newer_customer_sources(
                owner.client_id, max(ids), namespace=_revision_namespace(owner)).exists())

        if not still_owned():
            return
        kwargs["on_progress"] = still_owned
    _capture_message_media(row, **kwargs)


def _authority_from_projection(value) -> RevisionAuthorityBindingSet:
    return RevisionAuthorityBindingSet(
        ready=True,
        allowed_actions=tuple(value.get("allowed_actions") or ()),
        fact_bindings=tuple(value.get("fact_bindings") or ()),
        offer_bindings=tuple(value.get("offer_bindings") or ()),
        authority_digest=str(value.get("authority_digest") or ""),
    )


def _catalog_browse_prose_urls(plan, response, revision, prepared_effects=None):
    """Prose may repeat only a fresh URL on these authenticated themed cards."""
    from management.services.ig_catalog_browsing import browse_authorized_urls, validate_catalog_browse_response
    from management.services.ig_catalog_choice_presentations import (
        SCHEMA_THEMED, prepare_catalog_choice_presentations, validate_catalog_choice_assets,
        validate_catalog_choice_metadata,
    )
    capture = plan.configuration.get("catalog_browse")
    candidates = browse_authorized_urls(capture)
    if (revision is None or not candidates or not response.control.get("show_products")
        or validate_catalog_browse_response(capture, response)
        or not any(url in response.reply_text for url in candidates)):
        return ()
    if prepared_effects is None:
        prepared = prepare_catalog_choice_presentations(revision, response,
            InstagramBotSettings.objects.get(pk=1), response_plan=plan)
        if not prepared.ready:
            return ()
        prepared_effects = prepared.effects
    urls = []
    for effect in prepared_effects:
        metadata = effect.get("projection_metadata") or {}
        manifest = metadata.get("catalog_choice") or {}
        if manifest.get("schema") != SCHEMA_THEMED:
            continue
        if (manifest.get("scope", {}).get("catalog_browse", {}).get("digest") != capture.get("capture_digest")
            or not validate_catalog_choice_metadata(metadata, payload=effect["payload"],
                group=effect["group"], kind=effect["kind"])
            or validate_catalog_choice_assets(manifest, revision.client)):
            return ()
        for card in manifest["cards"]:
            url = card["theme_navigation"]["url"]
            if url in candidates and url not in urls:
                urls.append(url)
    return tuple(urls)


def _captured_reply_truth(plan, response, context, *, revision=None, prepared_effects=None):
    """Prove each line first, then retain whole-reply business/URL checks."""
    from management.services.ig_reply_truth import ReplyTruthResult, validate_reply_truth
    from management.services.ig_service_complaints import revision_service_complaint
    from management.services.ig_turn_intent import service_complaint_reply_reason
    context = replace(context, cooperation_policy=plan.cooperation_policy, source_price_subjects=())
    browse_urls = _catalog_browse_prose_urls(plan, response, revision, prepared_effects)
    if browse_urls:
        context = replace(context, authorized_urls=tuple(dict.fromkeys((*context.authorized_urls, *browse_urls))))

    complaint = revision_service_complaint(revision) if revision is not None else {}
    if complaint and set(response.control) == {"manager"} and response.control["manager"] is True:
        reason = service_complaint_reply_reason(response.reply_text)
        if reason:
            return ReplyTruthResult(False, (reason,))
        # A service-only acknowledgment does not need an unresolved shopping
        # selector. It receives no financial or fulfillment authority, and the
        # original plan still records every real remaining obligation/gap.
        context = replace(context, authorized_prices=(), authorized_price_ranges=(),
            authorized_discount_percents=(), authorized_discount_amounts=(),
            approved_timing_claims=(), explicitly_qualified_standard_dispatch_days=None,
            authorized_actions=(), payment_confirmed=False, order_created=False,
            shipment_state="unknown", known_tracking_refs=())
        return validate_reply_truth(response.reply_text, context=context)

    if plan.plan_gap:
        return ReplyTruthResult(False, (plan.plan_gap,))
    from management.services.ig_assisted_commerce_context import AssistedCommerceContext
    assisted = getattr(revision, "_assisted_commerce_context", None) if revision is not None else None
    if isinstance(assisted, AssistedCommerceContext):
        return assisted.validate_reply(response, context)
    if plan.configuration.get("reorder_observations"):
        context = replace(context, payment_confirmed=False, order_created=False,
            shipment_state="unknown", known_tracking_refs=(), approved_timing_claims=(),
            explicitly_qualified_standard_dispatch_days=None, authorized_actions=())
    reason = plan.validate_multiline_claims(response, context)
    if reason:
        return ReplyTruthResult(False, (reason,))
    if plan.line_plans:
        scoped = tuple(plan.line_truth_contexts(context).values())
        # This aggregate is only a second, whole-reply guard. It cannot admit
        # a price or preference rejected by the exact line check above.
        keys = ("authorized_prices", "authorized_price_ranges", "allowed_sizes", "allowed_fits",
                "allowed_colors", "source_chosen_sizes", "source_chosen_fits", "source_chosen_colors",
                "audited_chosen_sizes")
        context = replace(context, **{
            key: tuple(dict.fromkeys(value for item in scoped for value in getattr(item, key)))
            for key in keys
        }, source_price_subjects=tuple(subject for item in scoped for subject in item.source_price_subjects),
            approved_product_assertions=tuple(context.approved_product_assertions)
            + plan.answered_product_assertions(response, context))
    else:
        context = plan.truth_context(context)
    return validate_reply_truth(response.reply_text, context=context)


def _revision_response_coverage(plan, response, *, revision, local=False, checkout_cart_binding=None,
    catalog_media_evidence=None):
    """A service handoff does not answer unrelated shopping questions."""
    from management.services.ig_service_complaints import revision_service_complaint

    if revision_service_complaint(revision):
        # Preserve the actual reply and the original captured obligations. The
        # manager control remains on the sent response, but its general-purpose
        # information-handoff credit cannot discharge a service-first turn's
        # unanswered retail questions.
        response = replace(response, controls=())
    coverage = plan.coverage(response, local=local, checkout_cart_binding=checkout_cart_binding,
        catalog_media_evidence=catalog_media_evidence)
    from management.services.ig_revision_checkout import checkout_question_coverage
    coverage = checkout_question_coverage(plan, response,
        revision.bundle_snapshot.get("sources") or (), coverage)
    from management.services.ig_assisted_commerce_context import AssistedCommerceContext
    assisted = getattr(revision, "_assisted_commerce_context", None)
    if isinstance(assisted, AssistedCommerceContext) and not revision_service_complaint(revision):
        coverage = assisted.coverage(plan, response, revision, coverage)
    from management.services.ig_product_inquiries import product_inquiries_for_revision
    disposition = product_inquiries_for_revision(revision, response_plan=plan)
    # Persist the backend capture even when approved facts resolve the inquiry.
    # A refreshed transport revision must not classify that same known question
    # again from source text alone and manufacture a manager requirement.
    if not disposition.get("gap"):
        coverage["product_inquiry_capture"] = revision._product_inquiry_capture
    return coverage


def _service_complaint_reply(locale, complaint, *, first_impression=False):
    """Finite acknowledgment; its promised local case is created before send."""
    delivery = complaint.get("kind") == "delivery_fee_dispute"
    if locale == "ru":
        return ((("Извините, что доставка оставила неприятное первое впечатление. " if first_impression else "Извините за неприятную ситуацию с доставкой. ")
                 + "Понимаю, почему такая разница в стоимости расстроила. "
                 if delivery else "Извините, что возникла такая ситуация. Понимаю ваше недовольство. ")
                + "Передадим этот вопрос руководителю, чтобы разобраться в ситуации и найти решение.")
    if locale == "en":
        return ((("I'm sorry this delivery experience left a poor first impression. " if first_impression else "I'm sorry this delivery experience was disappointing. ")
                 + "I understand why the difference you described was frustrating. "
                 if delivery else "I'm sorry about this situation. I understand your concern. ")
                + "We'll ask our team lead to review what happened and work towards a resolution.")
    return ((("Перепрошую, що доставка залишила неприємне перше враження. " if first_impression else "Перепрошую за неприємну ситуацію з доставкою. ")
             + "Розумію, чому ця різниця у вартості засмутила. "
             if delivery else "Перепрошую, що виникла така ситуація. Розумію ваше невдоволення. ")
            + "Передамо це питання керівнику, щоб розібратися в ситуації та знайти рішення.")


class RevisionGenerationBoundary:
    """Request-local authority and CAS gate inside the existing two-call budget."""

    def __init__(self, revision, token, settings_row, publication, *, has_images=False, source_cart_capture=None):
        self.revision = revision
        self.token = token
        self.settings = settings_row
        self.settings_epoch = settings_row.reply_permission_epoch
        self.publication = publication
        self.has_images = has_images
        from management.services.ig_response_plan import capture_response_plan
        client = IgClient.objects.get(pk=revision.client_id)
        self.response_plan = capture_response_plan(client, revision=revision, source_cart_capture=source_cart_capture, publication=publication)
        from management.services.ig_assisted_commerce_context import capture_assisted_context
        reference = (revision.generation_proposal or {}).get("assisted_offer_reference")
        self.assisted_context, self.assisted_reason = capture_assisted_context(
            client, revision, self.response_plan, reference=reference)
        if self.assisted_context is not None:
            revision._assisted_commerce_context = self.assisted_context
            current_capture = self.assisted_context.current_capture
            if current_capture.get("capture_digest") != self.response_plan.source_cart_capture.get("capture_digest"):
                self.response_plan = capture_response_plan(client, revision=revision, source_cart_capture=current_capture, publication=publication)
        elif hasattr(revision, "_assisted_commerce_context"):
            del revision._assisted_commerce_context
        from management.services.ig_product_inquiries import product_inquiries_for_revision
        self.product_inquiries = product_inquiries_for_revision(revision, response_plan=self.response_plan)
        self.source_cart_fence = None
        self.source_cart_reason = ""
        if self.response_plan.source_cart_capture.get("lines"):
            from management.services.ig_response_cart_fence import capture_source_cart_authority

            cart = capture_source_cart_authority(client, self.response_plan.source_cart_capture, revision=revision)
            self.source_cart_fence = cart.binding if cart.ready else None
            self.source_cart_reason = cart.reason
        self.baseline = self._baseline()
        self.authority = None
        self.captured_client_state = (revision.generation_proposal or {}).get("fulfillment_capture")
        self.checkout_cart_binding = None
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
        if self.response_plan.plan_gap or self.source_cart_reason:
            return RevisionAuthorityBindingSet(False, (self.response_plan.plan_gap or self.source_cart_reason,))
        client = IgClient.objects.get(pk=self.revision.client_id)
        claims = [CLAIM_PUBLIC_POLICY_INPUTS]
        from management.services.ig_commerce_projection import source_preferences_for
        if self.source_cart_fence or source_preferences_for(client):
            claims.append(CLAIM_SOURCE_PREFERENCES)
        if self.assisted_context is not None:
            from management.services.ig_revision_authority import CLAIM_ASSISTED_COMMERCE
            claims.append(CLAIM_ASSISTED_COMMERCE)
        elif client.current_product_id:
            catalog = build_revision_authority_bindings(client, claims=(CLAIM_CATALOG_CONFIGURATION,), settings_obj=self.settings)
            if catalog.ready:
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
            control={**({"source_cart_fence": self.source_cart_fence} if self.source_cart_fence else {}),
                     **({"assisted_context": self.assisted_context} if self.assisted_context is not None else {})},
        )

    @property
    def provider_control_kinds(self):
        from management.services.ig_response_plan import REVISION_PROVIDER_CONTROL_KINDS
        return REVISION_PROVIDER_CONTROL_KINDS

    def truth_context(self, context):
        from management.services.ig_reply_authority import apply_fulfillment_timing_context
        context = apply_fulfillment_timing_context(context, getattr(self, "captured_client_state", None))
        from management.services.ig_inventory_availability import revision_inventory_capture
        from management.services.ig_reply_authority import apply_inventory_availability_context
        if retained := revision_inventory_capture(self.revision):
            context = apply_inventory_availability_context(context, retained)
        return self.response_plan.truth_context(context)

    def validate_response_truth(self, response, context):
        return _captured_reply_truth(self.response_plan, response, self.truth_context(context), revision=self.revision)

    def check(self, authority=None):
        if self.response_plan.plan_gap:
            from management.services.ig_revision_outbox import OutboxReadiness

            return OutboxReadiness(False, (self.response_plan.plan_gap,))
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
        if result.ready and self.response_plan.pending_questions:
            from management.services.ig_pending_question_context import pending_question_context_current
            from management.services.ig_revision_outbox import OutboxReadiness

            if not pending_question_context_current(self.revision, self.response_plan.pending_questions):
                return OutboxReadiness(False, ("pending_question_context_changed",))
        return result

    def response_authority(self, response):
        from management.services.ig_revision_intents import manager_case_reason

        # This request-local proof is produced by the checkout owner. A prior
        # candidate or model control cannot discharge the current cart's debt.
        self.checkout_cart_binding = None
        control = response.control
        if self.response_plan.configuration.get("reorder_observations") and (
            set(control).intersection(SELECTION_KEYS | {"items", "paylink", "payment", "price_quoted"})):
            return RevisionAuthorityBindingSet(False, ("reorder_resolution_required",))
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
            preference_keys
            and preference_keys.issubset({"fit", "size", "qty"})
            and not checkout_requested and "price_quoted" not in control
            and all(str(control[key]) == str(preferences.get({"fit": "fit_option_code", "qty": "quantity"}.get(key, key)))
                    for key in preference_keys)
        )
        if "items" in control and not checkout_requested and not case_reason:
            return RevisionAuthorityBindingSet(False, ("revision_cart_selection_unsupported",))
        if self.assisted_context is not None and preference_keys and not preference_acknowledgement:
            return RevisionAuthorityBindingSet(False, ("assisted_configuration_requires_hosted_adoption",))
        if not case_reason and self.assisted_context is None and ((preference_keys and not preference_acknowledgement) or "price_quoted" in control or checkout_requested):
            claims.append(CLAIM_CATALOG_CONFIGURATION)
        authority_control = dict(control)
        from management.services import instagram_bot as bot
        from management.services.ig_inventory_availability import captured_inventory_projection, owned_quote_capacity_line_ids
        from management.services.ig_reply_authority import inventory_availability_authorities
        from management.services.ig_revision_authority import CLAIM_INVENTORY_AVAILABILITY
        inventory = captured_inventory_projection(getattr(self, "captured_client_state", None))
        if (owned_quote_capacity_line_ids(inventory)
            or bot._stock_claim_sentences(response.reply_text) and inventory_availability_authorities(inventory)):
            claims.append(CLAIM_INVENTORY_AVAILABILITY)
            authority_control["inventory_availability_capture"] = inventory
        if self.assisted_context is not None:
            authority_control["assisted_context"] = self.assisted_context
        if self.source_cart_fence:
            authority_control["source_cart_fence"] = self.source_cart_fence
        if checkout_requested:
            from management.services.ig_revision_checkout import authorize_revision_checkout

            source_text = "\n".join(
                str(source.get("text") or "")
                for source in self.revision.bundle_snapshot["sources"] if source.get("role") == "user"
            )
            checkout_control, reasons = authorize_revision_checkout(
                client, control, source_text,
                sealed_sources=self.revision.bundle_snapshot["sources"],
                source_cart_capture=self.response_plan.source_cart_capture or None,
                source_cart_revision_id=self.revision.pk,
                assisted_context=self.assisted_context,
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
            actions.append("assisted_checkout_proposal_create" if self.assisted_context is not None else "checkout_proposal_create")
        elif preference_keys and not preference_acknowledgement and not case_reason:
            actions.append("client_configuration_update")
        if case_reason:
            actions.append("manager_escalation_intent")
        if self.programme is not None:
            actions.append("prize_review_case_create")
        final_authority = build_revision_authority_bindings(
            client, claims=claims, control=authority_control,
            server_authorized_actions=actions, settings_obj=self.settings,
        )
        if final_authority.ready and checkout_requested:
            self.checkout_cart_binding = checkout_control.get("source_cart_binding")
        return final_authority

    def contextual_fallback(self, *, policy_manifest):
        """Independently admit a local candidate; caller stores local provenance.

        The caller must additionally fence delivery effects across the recovery
        lineage. This method fences this revision and its current sealed source.
        """
        from management.services.ig_response_guard import build_source_preference_fallback

        def fallback_coverage(response):
            # This proof is persisted as JSON. The backend-only inquiry
            # capability is recomputed for the separate guarded coverage write
            # when the admitted local reply executes; it is never proof data.
            coverage = _revision_response_coverage(self.response_plan, response,
                revision=self.revision, local=True)
            coverage.pop("product_inquiry_capture", None)
            return coverage

        client = IgClient.objects.get(pk=self.revision.client_id)
        from management.services.ig_revision_holding import resolve_revision_reply_language
        language = resolve_revision_reply_language(self.revision,
            captured_context=getattr(self, "reply_language_context", None),
            response_plan=getattr(self, "response_plan", None),
            profile_language=client.language).template_family
        chart_text, chart_proof = _local_catalog_chart_candidate(self.revision, language)
        if chart_text:
            candidate = ValidatedResponse(reply_text=chart_text)
            if self.validate(candidate, policy_manifest=policy_manifest).valid:
                return candidate, {"kind": "native_chart_fallback", "version": 1, **chart_proof,
                    "revision_id": self.revision.pk, "snapshot_digest": self.revision.snapshot_digest,
                    "language": language, "source_cart_capture": self.response_plan.source_cart_capture,
                    "coverage": fallback_coverage(candidate),
                    "authority_digest": self.authority.authority_digest}
        # The existing technical-holding owner creates durable manager debt
        # before this natural check reply. A source-preference-only receipt has
        # no manager-effect authority and must not swallow this inquiry.
        if self.product_inquiries.get("status") == "manager_required":
            return None, {"reason": "product_inquiry_manager_review_required"}
        if not hasattr(self, "response_plan"):
            from management.services.ig_response_plan import capture_response_plan
            self.response_plan = capture_response_plan(IgClient.objects.get(pk=self.revision.client_id), revision=self.revision)
        client = IgClient.objects.get(pk=self.revision.client_id)
        language = resolve_revision_reply_language(self.revision,
            captured_context=getattr(self, "reply_language_context", None),
            response_plan=self.response_plan,
            profile_language=client.language).template_family
        selection_text = _focused_selection_clarification(self.response_plan, language)
        if selection_text:
            candidate = ValidatedResponse(reply_text=selection_text)
            if self.validate(candidate, policy_manifest=policy_manifest).valid:
                return candidate, {"kind": "focused_selection_clarification", "version": 1,
                    "revision_id": self.revision.pk, "snapshot_digest": self.revision.snapshot_digest,
                    "candidate_digest": self.response_plan.configuration["focus_selection_candidate"]["digest"],
                    "language": language, "source_cart_capture": self.response_plan.source_cart_capture,
                    "coverage": fallback_coverage(candidate),
                    "authority_digest": self.authority.authority_digest}
        fit_context = self.response_plan.fit_consultation
        if fit_context and fit_context.get("focus"):
            from management.services.ig_fit_consultation import deterministic_fit_reply
            language = resolve_revision_reply_language(self.revision,
                captured_context=getattr(self, "reply_language_context", None),
                response_plan=self.response_plan,
                profile_language=client.language).template_family
            text = deterministic_fit_reply(fit_context, language=language)
            presentation_id = (fit_context["focus"]["product_id"] if fit_context.get("photos_requested")
                and any((fit_context.get("fit_photo_capability") or {}).get(key) for key in ("photos", "illustrations")) else None)
            candidate = ValidatedResponse(reply_text=text, controls=(ResponseControl("show_products", str(presentation_id)),)
                if presentation_id else ())
            if text and self.validate(candidate, policy_manifest=policy_manifest).valid:
                coverage = fallback_coverage(candidate)
                kinds = {row["kind"] for row in self.response_plan.obligations}
                proof = {"kind": "fit_consultation_fallback", "version": 1,
                    "revision_id": self.revision.pk, "snapshot_digest": self.revision.snapshot_digest,
                    "fit_context_digest": _digest(fit_context), "language": language,
                    "approved_fact_keys": fit_context.get("approved_fact_keys") or [],
                    "presentation_product_id": presentation_id, "coverage": coverage,
                    "source_cart_capture": self.response_plan.source_cart_capture,
                    "pure_approved_facts": bool(kinds and kinds <= {"info:fit", "info:audience"}
                        and not fit_context.get("photos_requested") and not coverage.get("remaining")
                        and not self.authority.allowed_actions),
                    "authority_digest": self.authority.authority_digest}
                return candidate, proof
        response, proof = build_source_preference_fallback(client, revision=self.revision,
            response_plan=self.response_plan if self.response_plan.line_plans else None)
        if response is None:
            return None, proof
        if self.has_images or self.revision.delivery_effects.exists():
            return None, {}
        from management.services.ig_commerce_projection import source_preferences_for
        projection = {} if proof.get("source_cart_capture") else source_preferences_for(client)
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
                client, claims=(CLAIM_PUBLIC_POLICY_INPUTS, CLAIM_SOURCE_PREFERENCES) if projection or self.source_cart_fence else (CLAIM_PUBLIC_POLICY_INPUTS,), settings_obj=self.settings,
                control={"source_cart_fence": self.source_cart_fence} if self.source_cart_fence else None,
            )
        decision = self.validate(response, policy_manifest=policy_manifest)
        if not decision.valid:
            return None, {}
        from management.services.ig_service_complaints import revision_service_complaint
        coverage = (fallback_coverage(response)
            if revision_service_complaint(self.revision) else proof.get("coverage") or
                fallback_coverage(response))
        proof = {**proof, "coverage": coverage,
                 "revision_id": self.revision.pk,
                 "snapshot_digest": self.revision.snapshot_digest,
                 "authority_digest": self.authority.authority_digest}
        return response, proof

    def normalize_response(self, response):
        from management.services.ig_revision_conversation_context import normalize_response_delay_apology
        from management.services.ig_service_complaints import revision_service_complaint
        from management.services.ig_turn_intent import service_complaint_reply_reason

        complaint = revision_service_complaint(self.revision)
        if complaint:
            from management.services.ig_reply_language import resolve_own_source_reply_language

            decision = resolve_own_source_reply_language(
                sources=self.revision.bundle_snapshot.get("sources") or [],
                profile_language=self.revision.client.language,
                reset_floor=complaint["reset_floor"],
            )
            locale = decision.template_family if decision.template_family in {"uk", "ru", "en"} else "en"
            text = response.reply_text
            if service_complaint_reply_reason(text):
                complaint_ids = {item["message_id"] for item in complaint["source_refs"]}
                first_impression = any(re.search(r"перш\w*\s+вражен\w*|перв\w*\s+впечатлен\w*|first\s+impression", str(item.get("text") or ""), re.I)
                    for item in self.revision.bundle_snapshot.get("sources") or [] if item.get("message_id") in complaint_ids)
                text = _service_complaint_reply(locale, complaint, first_impression=first_impression)
            # Service first, including mixed shopping requests. No selection,
            # checkout or reward authority can be supplied by this candidate.
            response = replace(response, reply_text=text, controls=(ResponseControl("manager", True),))

        clean = normalize_response_delay_apology(response.reply_text, self.revision)
        if clean == response.reply_text:
            return response
        logging.getLogger(__name__).info(
            "ig_revision_reply_normalized revision=%s reason=unsupported_response_delay_prefix "
            "original_digest=%s normalized_digest=%s",
            self.revision.pk, _digest(response.reply_text), _digest(clean),
        )
        return replace(response, reply_text=clean)

    def normalize_media_response(self, response, *, request_media_binding, usage):
        """Normalize the admitted candidate before winner/action validation."""
        from management.services.ig_media_analysis import bound_media_parts, MediaAnalysisError
        from management.services.ig_revision_proposal import _request_media_projection
        from management.services.ig_media_response import normalize_media_reply
        artifact = response.turn_intelligence
        if artifact is None:
            return response
        observed = usage if isinstance(usage, dict) else {}
        supplied = dict(request_media_binding or {})
        supplied.update(actual_inline_count=observed.get("_request_inline_count"),
            actual_content_hashes=observed.get("_request_inline_content_hashes"))
        media, reason = _request_media_projection(self.revision, supplied)
        if reason:
            return response
        if not media["items"] and not media["outcomes"]:
            return response
        try:
            parts = bound_media_parts(parts=media["items"], observations=artifact.media_observations,
                legacy_images=artifact.image_observations, actual_inline_count=media["actual_inline_count"],
                actual_content_hashes=media["actual_content_hashes"])
        except MediaAnalysisError:
            return response
        analysis = _media_reply_analysis({"parts": parts, "capture_outcomes": media["outcomes"]}, self.revision)
        from management.services.ig_turn_intent import build_turn_intent
        intent = build_turn_intent(self.revision.client, self.revision)
        from management.services.instagram_bot import _media_reply_source_context, _authored_reply_locale
        context = _media_reply_source_context(self.revision.client,
            _revision_media_reply_sources(self.revision, self.revision.client),
            commerce_evidence_refs=intent["commerce_evidence_refs"])
        analysis = {**analysis, "source_service": context["source_service"]}
        from types import SimpleNamespace
        from management.services.ig_revision_holding import resolve_revision_reply_language
        locale = resolve_revision_reply_language(self.revision,
            captured_context=getattr(self, "reply_language_context", None),
            response_plan=getattr(self, "response_plan", None))
        template_client = SimpleNamespace(language=_authored_reply_locale(locale))
        return self.normalize_response(replace(response, reply_text=normalize_media_reply(template_client,
            response.reply_text, media_analysis=analysis, social_only=context["social_only"]),
            controls=tuple(item for item in response.controls if item.kind == "manager") if context["suppress_catalog"] else response.controls))

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
        from management.services.ig_cooperation_policy import selected_policy_reason
        cooperation_reason = selected_policy_reason(response.reply_text, self.response_plan.cooperation_policy, policy_manifest)
        if cooperation_reason:
            self.last_reasons = (cooperation_reason,)
            return ValidationDecision(False, self.last_reasons)
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
        validation_plan = self.response_plan
        if (self.assisted_context is not None and (response.control.get("paylink") or response.control.get("payment"))
            and self.assisted_context.purchase_ready(self.revision, self.response_plan.source_cart_capture)):
            # A native offer proves checkout eligibility, while every other
            # source-choice/selector/payment constraint remains on the old plan.
            validation_plan = replace(validation_plan, authority={**validation_plan.authority, "audited_size_configuration_matches": True})
        plan_reason = validation_plan.validate(response) if not intent.get("service_complaint") else ""
        if plan_reason:
            self.last_reasons = (plan_reason,)
            return ValidationDecision(False, self.last_reasons)
        from management.services.ig_business_cooperation import recruitment_response_reason
        recruitment_reason = recruitment_response_reason(self.revision.bundle_snapshot.get("sources") or [], response)
        if recruitment_reason:
            self.last_reasons = (recruitment_reason,)
            return ValidationDecision(False, self.last_reasons)
        case_reason = manager_case_reason(self.revision, response)
        from management.services.ig_product_inquiries import validate_inquiry_response
        inquiry_reason = validate_inquiry_response(self.product_inquiries, response)
        if inquiry_reason:
            self.last_reasons = (inquiry_reason,)
            return ValidationDecision(False, self.last_reasons)
        if manager_handoff_promised(response) and not case_reason:
            self.last_reasons = ("unnecessary_manager_handoff",)
            return ValidationDecision(False, self.last_reasons)
        if case_reason == "custom_print":
            from management.services.instagram_bot import _provider_reply_truth_context
            from management.services.ig_reply_truth import validate_reply_truth

            context = self.truth_context(_provider_reply_truth_context(self.revision.client, response.control, response.reply_text))
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
        if response.control.get("paylink") or response.control.get("payment"):
            from management.services.ig_revision_checkout import checkout_question_reason
            question_reason = checkout_question_reason(self.response_plan, response,
                self.revision.bundle_snapshot.get("sources") or (), checkout_cart_binding=self.checkout_cart_binding)
            if question_reason:
                self.last_reasons = (question_reason,)
                return ValidationDecision(False, self.last_reasons)
        self.authority = authority
        self.last_reasons = ()
        return ValidationDecision(True)

    def repair(self, payload, parsed, reasons, *, base_repair):
        if self.repaired or set(reasons).intersection({"revision_domain_action_unsupported", "revision_cart_selection_unsupported"}) or any(
            str(reason).startswith(("checkout", "custom_confirmation"))
            and reason not in {"checkout_purchase_authority_missing", "checkout_cart_clarification_required"} for reason in reasons
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
        from management.services.ig_product_inquiries import inquiry_prompt

        client = IgClient.objects.get(pk=self.revision.client_id)
        facts = asdict(build_reply_truth_context(client, captured_client_state=getattr(self, "captured_client_state", None)))
        facts.pop("source_price_subjects", None)  # Internal line validator metadata, never prompt authority.
        repaired.setdefault("contents", []).append({
            "role": "user", "parts": [{"text": (
                "[SERVER FACTS FOR THE SAME SEALED CUSTOMER BUNDLE]\n"
                + json.dumps(facts, ensure_ascii=False, default=_repair_fact_json_default, separators=(",", ":"))
                + "\n" + self.response_plan.prompt_guidance()
                + "\n" + inquiry_prompt(self.product_inquiries)
            )}],
        })
        self.repaired = True
        return repaired


def _repair_fact_json_default(value):
    if isinstance(value, (set, frozenset)):
        return sorted(value, key=str)
    return DjangoJSONEncoder().default(value)


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
        from management.services.ig_revision_finalization_faults import finalization_fault_blocks_execution
        if finalization_fault_blocks_execution(revision):
            return None, ""
        lease_deadline = now + timedelta(seconds=EXECUTION_LEASE_SECONDS)
        if revision.overall_deadline <= now and revision.recovery_code != "provider_dispatch_budget":
            from management.services.ig_revision_recovery import effective_execution_receipt, execution_resume_is_current

            if (not settings_row or not settings_row.is_enabled or not settings_row.ai_enabled
                or not client or client.reply_permission_epoch != revision.permission_epoch
                or client.manager_takeover or client.bot_paused or client.hidden_at or client.is_blocked
                or client.privacy_erasure_started_at is not None or revision.active_slot != 1
                or not execution_resume_is_current(revision, now=now)):
                return None, ""
            receipt = effective_execution_receipt(revision)
            if receipt["settings_id"] != settings_id or receipt["settings_permission_epoch"] != settings_row.reply_permission_epoch:
                return None, ""
            fixed_expiry = timezone.datetime.fromisoformat(receipt["expires_at"])
            lease_deadline = fixed_expiry if receipt.get("transport_tail") else min(lease_deadline, fixed_expiry)
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


def _media_reply_analysis(analysis, revision):
    """Presentation-only kinds from exact failed source/part receipts."""
    unavailable = [row.get("mime") for row in analysis.get("parts", ())
        if row.get("analysis_state") in {"omitted", "unreadable", "uncertain"}]
    failures = {(row.get("source_message_id"), row.get("source_part_id"))
        for row in analysis.get("capture_outcomes", ()) if row.get("collection_outcome") != "admitted"}
    for source in revision.bundle_snapshot.get("sources", ()):
        for part in source.get("media_parts", ()):
            if (source.get("message_id"), part.get("source_part_id")) in failures:
                unavailable.append(part.get("mime") or part.get("type") or "")
    return {**analysis, "unavailable_kinds": unavailable}


def _revision_media_reply_sources(revision, client):
    """Fresh owned source rows, never a changed or historical caption."""
    from management.services.ig_conversation_routes import conversation_route_reset_floor
    from management.services.ig_turn_revisions import _source_payload
    from management.services.ig_ingress_source_proof import is_current_customer_source

    floor = conversation_route_reset_floor(client.pk)
    snapshots = {row["message_id"]: row for row in revision.bundle_snapshot.get("sources", ())}
    result = []
    for source in revision.sources.select_related("message").order_by("ordinal", "id"):
        row, snapshot = source.message, snapshots.get(source.message_id)
        if (snapshot is None or row.client_id != client.pk or row.pk < floor
            or row.role != "user" or not is_current_customer_source(row, namespace=source.source_namespace)
            or row.sender_id != client.igsid
            or row.provider_namespace != source.source_namespace
            or snapshot.get("source_digest") != source.source_digest):
            continue
        try:
            if _source_payload(row, previous=source, ordinal=source.ordinal)["source_digest"] != source.source_digest:
                continue
        except (TypeError, ValueError):
            continue
        result.append(row)
    return result


def _normalize_response(response, artifact, client, *, revision=None, reply_language=None):
    from management.services.instagram_bot import _apply_turn_intelligence_resolution, _media_reply_source_context, _authored_reply_locale

    analysis = artifact.get("media_analysis") if isinstance(artifact, dict) else None
    context = {"social_only": False, "suppress_catalog": False, "source_service": False}
    if revision is not None and analysis is not None:
        from management.services.ig_turn_intent import build_turn_intent
        intent = build_turn_intent(client, revision)
        context = _media_reply_source_context(client, _revision_media_reply_sources(revision, client),
            commerce_evidence_refs=intent["commerce_evidence_refs"])
    if context["suppress_catalog"]:
        reply, control = response.reply_text, {key: value for key, value in response.control.items() if key == "manager"}
    else:
        reply, control = _apply_turn_intelligence_resolution(
            response.reply_text, response.control, artifact, client,
            reply_language=reply_language,
        )
    if analysis is not None:
        from management.services.ig_media_response import normalize_media_reply
        from types import SimpleNamespace
        template_client = (SimpleNamespace(language=_authored_reply_locale(reply_language))
            if reply_language is not None else client)
        analysis = {**analysis, "source_service": context["source_service"]}
        reply = normalize_media_reply(template_client, reply,
            media_analysis=_media_reply_analysis(analysis, revision) if revision is not None else analysis,
            social_only=context["social_only"])
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
    from management.services.gemini_routing import TurnFacts, classify_live_turn
    from management.services.ig_turn_capture import capture_revision_routing
    from management.services.ig_turn_intelligence import TurnContextError

    sources = revision.bundle_snapshot["sources"]
    source_id = int(sources[-1]["message_id"])
    images = collection.inline_media
    has_image = any(part.mime.startswith("image/") for part in collection.parts)
    has_audio = any(part.mime.startswith("audio/") for part in collection.parts)
    routing_decision = classify_live_turn(TurnFacts(has_image=has_image, has_audio=has_audio,
        reasoning_task_hint="media_analysis" if (has_image or has_audio) else ""), settings_obj=settings_row)
    context_mode = getattr(django_settings, "IG_TURN_CONTEXT_MODE", "legacy")
    context_reason = "context_legacy_mode"
    routing_capture = None
    if context_mode in {"shadow", "unified"}:
        settings_row.turn_intelligence_mode = context_mode
        try:
            routing_capture = capture_revision_routing(revision, generation_boundary=boundary,
                collection=collection, settings_row=settings_row, publication=publication)
        except TurnContextError as exc:
            context_reason = exc.reason
            if context_mode == "unified" and exc.reason != "legacy_sealed_time_unavailable":
                return None, boundary, (exc.reason,)
        if context_mode == "unified" and routing_capture is not None:
            routing_decision = routing_capture.decision
    from management.services.call_ai_analysis import prepare_live_chat_candidate_plan
    from management.services.ig_revision_provider_execution import _frozen_candidates

    manual_key = ((settings_row.custom_gemini_key or "").strip() or None
        if settings_row.gemini_source == InstagramBotSettings.CredSource.CUSTOM else None)
    _models, execution_plan = prepare_live_chat_candidate_plan(
        model_chain_override=routing_decision.model_chain, manual_key=manual_key)

    admission = record_generation_admission(
        revision.pk, token, settings_id=settings_row.pk,
        settings_permission_epoch=boundary.settings_epoch,
        publication=publication, authority=boundary.baseline,
        candidate_plan=_frozen_candidates(execution_plan),
    )
    if not admission.ready:
        return None, boundary, (admission.reason,)
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
    candidate_set = bot._build_turn_candidate_set() if images else None
    deadline = revision.overall_deadline - timedelta(seconds=5)
    media_context = [
        {**part, "status": "owned" if part.get("capture_outcome") == "owned" else "unavailable"}
        for source in sources for part in source.get("media_parts") or []
    ]
    from management.services.ig_turn_integration import (
        legacy_revision_context_metadata, prepare_revision_turn_context,
    )
    prepared_context = None
    captured_context = None
    if routing_capture is not None:
        try:
            captured_context = prepare_revision_turn_context(
                revision, generation_boundary=boundary, collection=collection,
                settings_row=settings_row, publication=publication,
                routing_capture=routing_capture,
            )
        except TurnContextError as exc:
            context_reason = exc.reason
            if context_mode == "unified" and exc.reason != "legacy_sealed_time_unavailable":
                if exc.reason == "required_context_budget_exceeded":
                    # Capture failed before the compiler/request graph. Keep
                    # this exact zero-dispatch fault on the existing local
                    # readiness path instead of losing it as a missing
                    # provider manifest when the claim later expires.
                    boundary.policy_readiness_failure = {"code": exc.reason, "details": {}}
                return None, boundary, (exc.reason,)
        if captured_context is not None:
            if context_mode == "unified":
                prepared_context = captured_context
                routing_decision = captured_context.context.decision
            else:
                logging.getLogger(__name__).info(
                    "turn_context_shadow route_equal=%s omissions=%s",
                    routing_decision == captured_context.context.decision,
                    len(captured_context.context.omissions),
                )
    actual_context_mode = context_mode if context_mode != "unified" or prepared_context else "legacy"
    request_context_metadata = (
        prepared_context.request_metadata if prepared_context is not None
        else legacy_revision_context_metadata(revision, mode=actual_context_mode, reason=context_reason)
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
    from management.services.ig_reply_authority import checkout_communication_prompt_guidance
    coverage_note += "\n" + checkout_communication_prompt_guidance(boundary.response_plan.communication_context)
    coverage_note += "\n" + _catalog_customer_guidance()
    coverage_note += "\n" + _catalog_source_guidance(revision, settings_row)
    from management.services.ig_product_inquiries import inquiry_prompt
    coverage_note += "\n" + inquiry_prompt(boundary.product_inquiries)
    if boundary.assisted_context is not None:
        coverage_note += "\n" + boundary.assisted_context.prompt_guidance()
    from management.services.ig_turn_intent import build_turn_intent, intent_generation_guidance
    complaint_intent = build_turn_intent(revision.client, revision)
    if complaint_intent.get("service_complaint"):
        coverage_note += "\n" + intent_generation_guidance(complaint_intent)
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
    if prepared_context is not None:
        boundary.captured_client_state = prepared_context.state
        coverage_note += "\n" + prepared_context.context.turn_note
    else:
        from management.services.ig_revision_conversation_context import conversation_timing_guidance
        coverage_note += "\n" + conversation_timing_guidance(revision)
        coverage_note += "\n" + boundary.response_plan.prompt_guidance()
    from management.services.gemini_accounting_runtime import revision_request_execution
    from management.services.ig_revision_holding import resolve_revision_reply_language
    boundary.reply_language_context = prepared_context.context if prepared_context is not None else None
    reply_language = resolve_revision_reply_language(revision, sources=sources,
        captured_context=boundary.reply_language_context, response_plan=boundary.response_plan)

    if prepared_context is not None:
        from management.services.ig_prompt_projections import deduplicate_captured_consultation_note
        coverage_note = deduplicate_captured_consultation_note(coverage_note,
            dynamic_notes=prepared_context.dynamic_notes)

    with revision_request_execution(
        revision.pk, token, settings_id=settings_row.pk,
        settings_permission_epoch=boundary.settings_epoch,
    ), turn_lineage(
        lane=Lane.LIVE, client_id=revision.client_id,
        source_message_id=source_id, logical_turn_id=f"ig-revision:{revision.pk}",
    ):
        response = bot.gemini_generate(
            settings_row, list(prepared_context.history) if prepared_context is not None else build_sealed_history(revision), images=images or None,
            routing_decision=routing_decision,
            client=revision.client, turn_note=coverage_note,
            turn_candidate_set=candidate_set,
            turn_media_binding=collection.binding,
            turn_media_context=media_context,
            failure_context=failure, generation_boundary=boundary,
            deadline_at=deadline,
            customer_route_context=route_context,
            memory_note=prepared_context.context.memory_note if prepared_context is not None else None,
            context_note=prepared_context.context.context_note if prepared_context is not None else None,
            captured_client_state=prepared_context.state if prepared_context is not None else None,
            captured_dynamic_notes=prepared_context.dynamic_notes if prepared_context is not None else None,
            captured_turn_text=prepared_context.current_text if prepared_context is not None else None,
            request_context_metadata=request_context_metadata,
            captured_policy_tags=prepared_context.policy_inputs.tags if prepared_context is not None else None,
            captured_knowledge_language=prepared_context.policy_inputs.knowledge_language if prepared_context is not None else None,
            captured_knowledge_selectors=prepared_context.policy_inputs.knowledge_selectors if prepared_context is not None else None,
            captured_reply_language=reply_language,
        )
    if not isinstance(response, ValidatedResponse) or not response.valid:
        from management.services.ig_revision_recovery import policy_readiness_failure_code
        policy_code = policy_readiness_failure_code(failure)
        if policy_code:
            boundary.policy_readiness_failure = {"code": policy_code, "details": failure.get("policy_readiness_details")}
            return None, boundary, (policy_code,)
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
    response = _normalize_response(response, artifact, revision.client, revision=revision,
        reply_language=reply_language)
    response = boundary.normalize_response(response)
    from management.services.ig_reply_language import reply_language_mismatch
    if reply_language_mismatch(response.reply_text, reply_language):
        return None, boundary, ("reply_language_mismatch",)
    authority = boundary.response_authority(response)
    readiness = boundary.check(authority)
    if not readiness.ready:
        return None, boundary, readiness.reasons
    from management.services.ig_reply_truth import validate_reply_truth

    from management.services.ig_reply_authority import build_reply_truth_context
    truth_client = IgClient.objects.get(pk=revision.client_id)
    truth_context = (build_reply_truth_context(truth_client, control=response.control)
        if boundary.response_plan.line_plans else bot._provider_reply_truth_context(truth_client, response.control, response.reply_text))
    from management.services.ig_catalog_category_discovery import category_urls_for_control
    category_urls = category_urls_for_control(response.control, truth_client,
        locale=reply_language.template_family)
    if category_urls:
        truth_context = replace(truth_context, authorized_urls=tuple(dict.fromkeys(
            (*truth_context.authorized_urls, *category_urls))))
    truth = boundary.validate_response_truth(response, truth_context)
    if not truth.valid:
        return None, boundary, truth.reasons
    from management.services.ig_response_debt import record_response_coverage
    catalog_evidence = {"artifact": artifact, "binding": media_manifest}
    coverage_response = response
    if any(row["kind"] == "info:size_chart" for row in boundary.response_plan.obligations):
        prepared_effects, preparation_reasons = _prepare_effects(revision, response, settings_row,
            response_plan=boundary.response_plan)
        if preparation_reasons:
            return None, boundary, preparation_reasons
        charts = [row for row in prepared_effects if (row.get("projection_metadata") or {}).get("catalog_chart")]
        fit_effect = next((row for row in prepared_effects if (row.get("projection_metadata") or {}).get("delivery") == "native"
            and "fit_illustration" in (row.get("projection_metadata") or {})), None)
        if charts or fit_effect:
            catalog_evidence["native_size_charts"] = charts
            catalog_evidence["native_fit_illustration"] = fit_effect
            coverage_response = _response_with_prepared_text(response, prepared_effects)
    if not record_response_coverage(revision.pk, token, _revision_response_coverage(
        boundary.response_plan, coverage_response, revision=revision, checkout_cart_binding=boundary.checkout_cart_binding,
        catalog_media_evidence=catalog_evidence,
    )):
        return None, boundary, ("response_coverage_cas_failed",)
    from management.services.ig_fulfillment_context import retained_fulfillment_frame
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
        source_cart_capture=boundary.response_plan.source_cart_capture if boundary.source_cart_fence else None,
        fulfillment_capture=retained_fulfillment_frame(boundary.captured_client_state),
    )
    if not stored.stored:
        return None, boundary, stored.reasons
    boundary.authority = authority
    revision.refresh_from_db()
    return response, boundary, ()


def _catalog_customer_guidance():
    # This turn note accompanies the active DB publication, not only the
    # default prompt text (which an already published instruction overrides).
    from management.services.ig_catalog_category_discovery import catalog_discovery_guidance

    return (
        "[CATALOG AND DELIVERY CUSTOMER EXPERIENCE]\n"
        "Answer every question and remaining request in the full sealed source bundle, including text "
        "accompanying a catalog button. A button must not erase another delivery or product question. "
        "For an explicit shopping request to show cards or photos of an identifiable category, use "
        "show_products with 1–3 actual published products from that category as a starting shortlist now. "
        "Optional fit, print, colour or size preferences are not prerequisites for showing that shortlist; "
        "refine it afterward. If colors and sizes are also requested, explain verified configured choices "
        "for the shown products or offer the next relevant selector after a product pick; do not replace "
        "the requested cards with only a preference question. Never invent a category match or product ID. "
        "Keep existing social-only suppression, source authority and request-budget limits. "
        "Use product-specific photos in show_products; do not reuse one product's image for other models. "
        "Configured colors and sizes are choices, never proof of physical stock, including zero-stock rules. "
        "Ask only for a missing required choice, and do not repeat a known size or color or guess a line in a mixed cart. "
        "An exact available size chart is sent as an image in this chat. If unavailable or ambiguous, explain "
        "the uncertainty and arrange manager confirmation; do not claim an image was sent or substitute a product photo. "
        "Acknowledge delivery concerns warmly and explain the confirmed terms. Normally the recipient pays "
        "the carrier separately; an agreed delivery cap is not the actual carrier charge. Merchant-funded "
        "delivery requires explicit approved terms. Never invent free delivery, promotions, insurance or "
        "a guaranteed arrival date. For urgent timing or uncertain availability, get manager confirmation "
        "and avoid a firm shipment commitment.\n" + catalog_discovery_guidance()
    )


def _catalog_native_source_choice(revision):
    """Recover a chart/page from the exact own source and delivered invitation."""
    from management.services.ig_catalog_choice_presentations import delivered_catalog_choice

    choices = []
    for source in _revision_media_reply_sources(revision, revision.client):
        if not str(source.quick_reply_payload or "").startswith("CC1:"):
            continue
        # Historical inspection only identifies the kind. Admission for an
        # effect always uses a second, fresh delivery/scope/asset check.
        historical = delivered_catalog_choice(revision.client, source, fresh=False)
        if not historical.get("ready"):
            return None, (historical.get("reason") or "catalog_choice_unverified",)
        if (historical.get("action") or {}).get("kind") not in {"chart", "page"}:
            continue
        fresh = delivered_catalog_choice(revision.client, source, fresh=True)
        if not fresh.get("ready"):
            return None, (fresh.get("reason") or "catalog_choice_stale",)
        choices.append(fresh)
    if len(choices) > 1:
        return None, ("catalog_choice_source_ambiguous",)
    return (choices[0] if choices else None), ()


def _catalog_source_guidance(revision, settings_row):
    from management.services.ig_catalog_choice_presentations import prepare_catalog_chart_effect

    choice, reasons = _catalog_native_source_choice(revision)
    if reasons:
        return "[CATALOG SOURCE REQUEST] The old catalog action is unavailable, stale or unverified. Explain this honestly and ask for a fresh choice or manager help. Do not say a choice was applied, an order was changed, or a chart/page was sent. Address every remaining source question."
    plain_chart = not choice and _catalog_plain_chart_requested(revision)
    if not choice and not plain_chart:
        return ""
    kind = choice["action"]["kind"] if choice else "chart"
    if kind == "chart":
        prepared = prepare_catalog_chart_effect(revision, ValidatedResponse(""), settings_row,
            **({"choice": choice} if choice else {}))
        if not prepared.ready:
            return "[CATALOG SOURCE REQUEST] The customer requested a size chart, but an exact current chart image is unavailable or ambiguous. Explain this and ask a manager to confirm it. Do not say the chart is attached, below or already sent. Address the other source questions too."
    return "[CATALOG SOURCE REQUEST] A verified customer action requests " + kind + ". Its prepared content will accompany your reply. Address all other source text and questions as well."


def _catalog_plain_chart_requested(revision):
    """An own explicit chart request, never a model directive or quoted request."""
    from management.services.ig_response_plan import capture_response_plan, _unquoted_text
    from management.services.ig_reply_truth import _locally_negated

    requested = False
    for source in _revision_media_reply_sources(revision, revision.client):
        if source.quick_reply_payload:
            continue
        text = _unquoted_text(source.text or "")
        for clause in re.findall(r"[^.!?;\n]+[?]?", text):
            if not re.search(r"таблиц\w*|сіт\w*\s+розмір|розмір\w*\s+сіт\w*|сетк\w*|size\s+chart|(?:обидв\w*|обе|both)\s+(?:сіт\w*|charts?|guides?)", clause, re.I):
                continue
            if re.search(r"сказа\w*|казав|казала|говори\w*|просив|просила|просил|попрос\w*|asked|said|told", clause, re.I):
                continue
            # Explicitly named different models need a source-bound product
            # resolution first; 'this model' refers to the current selection.
            if re.search(r"\b(?:для|for)\b", clause, re.I) and not re.search(
                r"для\s+(?:цієї|цього|этой|этого|мене|меня)|for\s+(?:this|me)\b", clause, re.I):
                continue
            cue = re.search(r"покаж\w*|надішл\w*|пришл\w*|скин\w*|дайте|send|show|please|можна|можно|can|could|потріб\w*|нуж\w*", clause, re.I)
            bare = re.fullmatch(r"\s*(?:таблиц\w*\s+(?:розмір\w*|размер\w*)|розмір\w*\s+сіт\w*|размер\w*\s+сетк\w*|size\s+chart)\s*\?\s*", clause, re.I)
            if bare or (cue and not _locally_negated(clause, cue.start())):
                requested = True
    if not requested:
        return False
    if not revision.client.current_product_id:
        from management.services.ig_product_focus import capture_discussed_configuration, capture_product_focus, capture_fit_chart_comparison
        if not capture_discussed_configuration(revision):
            from management.services.ig_fit_consultation import requested_chart_fits
            from management.services.ig_revision_catalog_assets import capture_fit_photo_capability
            from management.services.ig_commerce_projection import capture_current_selection_lines
            cart = (revision.generation_proposal or {}).get("source_cart_capture")
            focus = capture_product_focus(revision, source_cart_capture=cart if cart is not None else capture_current_selection_lines(revision.client_id))
            supported = capture_fit_photo_capability(focus["product_id"], metadata_only=True).get("supported_fits", []) if focus.get("status") == "focused" else []
            fits = requested_chart_fits((revision.bundle_snapshot or {}).get("sources") or [], supported_fits=supported)
            if not any(capture_fit_chart_comparison(revision, fit) for fit in fits):
                return False
    plan = capture_response_plan(revision.client, revision=revision,
        source_cart_capture=(revision.generation_proposal or {}).get("source_cart_capture"))
    return not plan.plan_gap and len(plan.line_plans) <= 1 and len((plan.source_cart_capture or {}).get("lines") or ()) <= 1


def _catalog_configuration_selector(revision, response):
    from management.services.ig_response_plan import capture_response_plan, asks_next_selector
    from management.services.ig_catalog_choice_actions import native_choice_request

    if (not response.valid or not revision.client.current_product_id
        or response.control.get("show_products") or response.control.get("paylink") or response.control.get("order")):
        return ""
    sources = _revision_media_reply_sources(revision, revision.client)
    if not sources:
        return ""
    plan = capture_response_plan(revision.client, revision=revision,
        source_cart_capture=(revision.generation_proposal or {}).get("source_cart_capture"))
    if plan.plan_gap or len(plan.line_plans) > 1 or len((plan.source_cart_capture or {}).get("lines") or ()) > 1:
        return ""
    selector_plan = plan.line_plans[0] if plan.line_plans else plan
    selector_plan = selector_plan.as_dict() if hasattr(selector_plan, "as_dict") else selector_plan
    configuration = selector_plan.get("configuration", {}) if isinstance(selector_plan, dict) else selector_plan.configuration
    choices = selector_plan.get("choices", {}) if isinstance(selector_plan, dict) else selector_plan.choices
    evidence = selector_plan.get("evidence", {}) if isinstance(selector_plan, dict) else selector_plan.evidence
    kind = plan.next_selector
    if kind not in {"color", "size"} or kind not in (configuration.get("missing") or ()):
        return ""
    if choices.get(kind) or getattr(revision.client, "current_" + kind, ""):
        return ""
    own_ids = {source.pk for source in sources}
    product_evidence = evidence.get("product_id") or {}
    own_product = (product_evidence.get("source_message_id") in own_ids
        and choices.get("product_id") == revision.client.current_product_id)
    own_action = False
    for source in sources:
        if str(source.quick_reply_payload or "").startswith("CC1:"):
            request = native_choice_request(revision.client, source, historical=True)
            if request is not None:
                action = request.source_binding.get("native_catalog_choice", {}).get("action") or {}
                own_action |= action.get("product_id") == revision.client.current_product_id
    # A question in a validated response is a directive only within this
    # independently verified current configuration and missing-field plan.
    requested = any(asks_next_selector(source.text or "", kind) for source in sources)
    directive = asks_next_selector(response.reply_text, kind)
    return kind if own_product or own_action or requested or directive else ""


def _catalog_source_effect_reason(revision):
    """Fresh origin verification also runs for crash-resumed catalog effects."""
    metadata = [row.projection_metadata or {} for row in revision.delivery_effects.all()
        if (row.projection_metadata or {}).get("catalog_choice") or (row.projection_metadata or {}).get("catalog_chart")]
    if not metadata:
        return ""
    own = _revision_media_reply_sources(revision, revision.client)
    expected_ids = {row["message_id"] for row in revision.bundle_snapshot.get("sources", ())}
    if {row.pk for row in own} != expected_ids:
        return "catalog_choice_source_changed"
    for row in metadata:
        manifest = row.get("catalog_choice") or row.get("catalog_chart")
        scope = manifest.get("scope") or {}
        origin = scope.get("origin_manifest_digest") or manifest.get("origin_manifest_digest")
        if origin:
            choice, reasons = _catalog_native_source_choice(revision)
            if reasons:
                return reasons[0]
            if not choice or choice.get("manifest_digest") != origin:
                return "catalog_choice_origin_changed"
    return ""


def _catalog_unavailable_chart_reason(reply):
    # Generation saw the exact-grid result before making image claims. A
    # late loss must never send a stale promise that an image accompanies it.
    from management.services.ig_reply_truth import _locally_negated

    image = r"таблиц\w*|сіт\w*|сетк\w*|chart|image|фото|зображен\w*"
    sent = r"надісл\w*|надсил\w*|прикріп\w*|дода(?:ю|в|ємо)\w*|нижче|отправ\w*|прикреп\w*|ниже|attached|sending|sent|below"
    for clause in re.findall(r"[^.!?\n]+", str(reply or "")):
        if not re.search(image, clause, re.I):
            continue
        for match in re.finditer(sent, clause, re.I):
            if not _locally_negated(clause, match.start()):
                return "catalog_choice_chart_copy_without_image"
    return ""


def _catalog_prepared_reply(reply, prepared):
    """Keep every generated answer and append only the ready producer's notice."""
    hint = str(getattr(prepared, "copy_hint", "") or "").strip()
    if not hint or not prepared.ready or hint in reply:
        return reply
    return reply.rstrip() + "\n\n" + hint


def _response_with_prepared_text(response, effects):
    text = "\n".join(str((row.get("payload", {}).get("message") or {}).get("text") or "")
        for row in effects if row.get("group") == "substantive_text")
    return replace(response, reply_text=text) if text else response


def _combined_chart_and_fit_content(revision, response, settings_row, chart, *, response_plan=None):
    """Each requested native attachment keeps its own signed source proof."""
    from management.services.ig_product_inquiry_presentation import prepare_fit_illustration

    reply = _catalog_prepared_reply(response.reply_text, chart)
    if not chart.effects and not chart.reasons:
        reason = _catalog_unavailable_chart_reason(response.reply_text)
        if reason:
            return (), reply, (reason,)
        hint = str(getattr(chart, "copy_hint", "") or "").strip()
        if hint and hint not in reply:
            reply = reply.rstrip() + "\n\n" + hint
    from management.services.ig_fit_consultation import fit_consultation_request
    context = response_plan.fit_consultation if response_plan is not None else None
    wants_photos = (context or fit_consultation_request((revision.bundle_snapshot or {}).get("sources") or [])).get("photos_requested")
    if not wants_photos:
        return chart.effects, reply, chart.reasons
    illustrated = prepare_fit_illustration(revision, response, settings_row,
        context=response_plan.fit_consultation if response_plan is not None else None,
        native_part_index=sum(row.get("group") == "template" for row in chart.effects),
        unavailable_chart_fits=(getattr(chart, "manifest", None) or {}).get("unavailable_chart_fits", ()))
    if not illustrated.handled:
        return chart.effects, reply, chart.reasons
    effects = (*chart.effects, *illustrated.effects)
    reasons = tuple(dict.fromkeys((*chart.reasons, *illustrated.reasons)))
    return effects, _catalog_prepared_reply(reply, illustrated), reasons


def _prepare_visible_content(revision, response, settings_row, *, response_plan=None):
    from management.services import instagram_bot as bot
    from management.services.ig_catalog_media import prepare_catalog_media

    reply = response.reply_text
    effects = []
    # Checkout retains its existing photo and authority path. Ordinary native
    # choices precede the legacy image gallery and share the canonical outbox.
    if not response.control.get("paylink") and not response.control.get("order"):
        from management.services.ig_catalog_choice_presentations import (
            prepare_catalog_choice_presentations, prepare_catalog_configuration_choices,
            prepare_catalog_chart_effect, prepare_catalog_choice_page,
        )
        choice, reasons = _catalog_native_source_choice(revision)
        if reasons:
            # A stale button is still a legitimate customer message. Gemini
            # received the failure before generating this contextual reply;
            # it grants no stale catalog effects or selection authority.
            reason = _catalog_unavailable_chart_reason(reply)
            return (), reply, (reason,) if reason else ()
        if choice:
            factory = prepare_catalog_chart_effect if choice["action"]["kind"] == "chart" else prepare_catalog_choice_page
            prepared = factory(revision, response, settings_row, choice=choice, response_plan=response_plan)
            if prepared.handled:
                if choice["action"]["kind"] == "chart":
                    return _combined_chart_and_fit_content(revision, response, settings_row, prepared,
                        response_plan=response_plan)
                return prepared.effects, _catalog_prepared_reply(reply, prepared), prepared.reasons
            # A missing/ambiguous chart was explicitly disclosed to Gemini.
            # It must not fall through to an unrelated product photograph.
            reason = _catalog_unavailable_chart_reason(reply) if choice["action"]["kind"] == "chart" else "catalog_choice_page_unavailable"
            return (), reply, (reason,) if reason else ()
        if _catalog_plain_chart_requested(revision):
            prepared = prepare_catalog_chart_effect(revision, response, settings_row, response_plan=response_plan)
            if prepared.handled:
                return _combined_chart_and_fit_content(revision, response, settings_row, prepared,
                    response_plan=response_plan)
            reason = _catalog_unavailable_chart_reason(reply)
            return (), reply, (reason,) if reason else ()
        from management.services.ig_product_inquiry_presentation import prepare_fit_illustration

        illustrated = prepare_fit_illustration(revision, response, settings_row,
            context=response_plan.fit_consultation if response_plan is not None else None)
        if illustrated.handled:
            return illustrated.effects, _catalog_prepared_reply(reply, illustrated), illustrated.reasons
        prepared = prepare_catalog_choice_presentations(revision, response, settings_row, response_plan=response_plan)
        if prepared.handled:
            return prepared.effects, _catalog_prepared_reply(reply, prepared), prepared.reasons
        kind = _catalog_configuration_selector(revision, response)
        if kind:
            prepared = prepare_catalog_configuration_choices(revision, response, settings_row,
                kind=kind, response_plan=response_plan)
            if prepared.handled:
                return prepared.effects, reply, prepared.reasons
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


def _prepare_effects(revision, response, settings_row, *, response_plan=None):
    from management.services import instagram_bot as bot
    from management.services.ig_revision_transport import prepare_text_effects
    from management.services.ig_response_plan import capture_response_plan
    from management.services.ig_catalog_browsing import validate_catalog_browse_response

    client = IgClient.objects.get(pk=revision.client_id)
    if response_plan is None:
        response_plan = capture_response_plan(client, revision=revision,
            source_cart_capture=(revision.generation_proposal or {}).get("source_cart_capture"),
            publication=settings_row.active_instruction_publication)
    if reason := validate_catalog_browse_response(response_plan.configuration.get("catalog_browse"), response):
        return (), (reason,)

    prior, reply, reasons = _prepare_visible_content(revision, response, settings_row, response_plan=response_plan)
    if reasons:
        return (), reasons
    effects = list(prior)
    from management.services.ig_reply_truth import validate_reply_truth

    from management.services.ig_reply_authority import build_reply_truth_context
    context = (build_reply_truth_context(client, control=response.control) if response_plan.line_plans
        else bot._provider_reply_truth_context(client, response.control, reply))
    from management.services.ig_reply_authority import apply_fulfillment_timing_context
    context = apply_fulfillment_timing_context(context, (revision.generation_proposal or {}).get("fulfillment_capture"))
    from management.services.ig_inventory_availability import revision_inventory_capture, validate_revision_inventory
    from management.services.ig_reply_authority import apply_inventory_availability_context
    if inventory := revision_inventory_capture(revision):
        context = apply_inventory_availability_context(context, inventory)
    if reason := validate_revision_inventory(revision):
        return (), (reason,)
    from management.services.ig_fulfillment_context import validate_retained_fulfillment
    if reason := validate_retained_fulfillment(revision, (revision.generation_proposal or {}).get("fulfillment_capture")):
        return (), (reason,)
    truth = _captured_reply_truth(response_plan, replace(response, reply_text=reply), context, revision=revision,
        prepared_effects=prior)
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
    from management.services.ig_payment_reminders import record_delivery
    from management.services.ig_restock_subscriptions import record_delivery as record_restock_delivery

    projected = project_sent_reply(revision_id)
    # The same receipt hook is used by normal finalization and exact-echo
    # recovery. The service requires the whole invitation with confirmed MIDs.
    record_delivery(revision_id)
    record_restock_delivery(revision_id)
    return projected


def _revision_response_purpose_reason(revision):
    """Recheck current source purpose against the whole immutable reply.

    Checking individual split parts could miss a CTA crossing their boundary.
    This gate has no business mutations and also runs on crash-resumed outboxes.
    """
    from management.services.ig_turn_intent import build_turn_intent, validate_turn_response, service_complaint_manual_reply_owned
    from management.services.ig_revision_outbox import revision_has_newer_source, revision_sources_current

    current = IgCustomerTurnRevision.objects.select_related("client").get(pk=revision.pk)
    if service_complaint_manual_reply_owned(current):
        return "service_complaint_manager_reply_owned"
    if revision_has_newer_source(current):
        return "pending_inbound"
    if not revision_sources_current(current):
        return "revision_sources_changed"
    fallback = (current.action_receipts or {}).get("source_preference_fallback") or {}
    proof = fallback.get("proof") or {}
    if proof.get("kind") in {"focused_selection_clarification", "fit_consultation_fallback", "native_chart_fallback"}:
        from management.services.ig_response_plan import capture_response_plan
        plan = capture_response_plan(current.client, revision=current,
            source_cart_capture=proof.get("source_cart_capture"))
        if proof["kind"] == "native_chart_fallback":
            text, current_proof = _local_catalog_chart_candidate(current, proof.get("language"))
            if text != fallback.get("reply_text") or any(proof.get(key) != value for key, value in current_proof.items()) or not current_proof:
                return "deterministic_native_chart_changed"
        elif proof["kind"] == "focused_selection_clarification":
            if (proof.get("candidate_digest") != (plan.configuration.get("focus_selection_candidate") or {}).get("digest")
                or _focused_selection_clarification(plan, proof.get("language")) != fallback.get("reply_text")):
                return "deterministic_focused_selection_changed"
        else:
            from management.services.ig_fit_consultation import deterministic_fit_reply
            if (proof.get("fit_context_digest") != _digest(plan.fit_consultation)
                or deterministic_fit_reply(plan.fit_consultation, language=proof.get("language")) != fallback.get("reply_text")):
                return "deterministic_fit_facts_changed"
    if catalog_reason := _catalog_source_effect_reason(current):
        return catalog_reason
    text_effects = tuple(current.delivery_effects.filter(group="substantive_text").order_by("order_index"))
    text = "\n".join(str((row.payload.get("message") or {}).get("text") or "") for row in text_effects)
    from management.services.ig_assisted_commerce_context import CLAIM, capture_assisted_context
    references = [row.get("selector", {}).get("assisted_reference") for effect in text_effects
                  for row in effect.fact_bindings if row.get("claim") == CLAIM]
    reference = references[0] if references else (current.generation_proposal or {}).get("assisted_offer_reference")
    if reference is not None:
        if any(item != reference for item in references):
            return "assisted_outbox_reference_conflict"
        context, reason = capture_assisted_context(current.client, current, reference=reference)
        if context is None:
            return reason or "assisted_offer_unavailable"
        current._assisted_commerce_context = context
    if current.delivery_effects.filter(purpose="technical_holding").exists():
        from management.services.ig_revision_holding import holding_receipt_valid
        if not holding_receipt_valid(current, text=text):
            return "holding_handoff_no_longer_open"
        from management.services.ig_terminal_fault_ack import MODE as TERMINAL_FAULT_ACK_MODE
        if ((current.action_receipts.get("technical_holding") or {}).get("acknowledgement_mode") == TERMINAL_FAULT_ACK_MODE
            and not current.delivery_effects.exclude(purpose="technical_holding").exists()):
            # This exact one-text acknowledgement is already bound to the
            # real technical-debt task by holding_receipt_valid. It makes no
            # product/support decision that requires a separate business case.
            # The canonical outbox still enforces every source/permission and
            # messaging-window fence before physical dispatch.
            return ""
    from management.services.ig_revision_intents import product_review_receipt_reason
    product_reason = product_review_receipt_reason(current)
    if product_reason:
        return product_reason
    intent = build_turn_intent(current.client, current)
    if intent.get("service_complaint"):
        from management.models import IgFollowUpTask, IgBotNotification

        receipt = (current.action_receipts or {}).get("manager_handoff") or {}
        if not isinstance(receipt, dict) or receipt.get("case_kind") != "service_complaint_review":
            return "service_complaint_handoff_missing"
        task = IgFollowUpTask.objects.filter(
            pk=receipt.get("task_id"), client_id=current.client_id, kind="manager_task",
            reason="revision_case:service_complaint",
            event_key=f"ig-revision-case:{current.client_id}:{current.pk}:service_complaint_review",
        ).first()
        if (task is None or (task.manager_context or {}).get("service_complaint") != intent["service_complaint"]
                or receipt.get("generation_proposal_digest") != current.generation_proposal_digest
                or not IgBotNotification.objects.filter(pk=receipt.get("notification_id"), client_id=current.client_id).exists()):
            return "service_complaint_handoff_invalid"
        if task.status in {"completed", "cancelled"}:
            return "service_complaint_human_handled"
    return validate_turn_response(intent, text)


def _drain_effects(revision, token, settings_row, access_token):
    # A late advisory HTTP completion is cleaned up separately from this send.
    _stop_revision_presence(dispatch=True)
    from management.services.ig_revision_delivery import drain_group
    from management.services.ig_revision_execution import finalize_sent_revision_effects
    from management.services.ig_revision_transport import build_provider_part_callback, RevisionProviderTransport
    from management.services.ig_revision_delivery import ProviderPartResult
    from management.services.ig_payment_reminders import provider_boundary_for_revision, record_delivery
    from management.services.ig_restock_subscriptions import (
        provider_boundary_for_revision as restock_provider_boundary,
        record_delivery as record_restock_delivery,
    )

    effect = revision.delivery_effects.order_by("order_index").first()
    if effect is None:
        return RevisionLiveResult(revision.pk, "blocked", ("effects_missing",))
    transport = build_provider_part_callback(
        settings_row, expected_namespace=effect.provider_namespace,
        expected_recipient=effect.recipient_igsid, access_token=access_token,
        response_gate=lambda _payload: _revision_response_purpose_reason(revision),
    )
    original_transport = transport

    def guarded_send(payload):
        # Retain the canonical transport's shape/preflight and echo handling.
        # The invitation's committed admission is rechecked immediately before
        # each physical request; no database transaction spans the HTTP call.
        with provider_boundary_for_revision(revision.pk) as allowed:
            if not allowed:
                return ProviderPartResult(
                    provider_namespace=effect.provider_namespace,
                    outcome="known_not_dispatched",
                    explicit_rejection_code="payment_reminder_scope_changed",
                )
            with restock_provider_boundary(revision.pk) as restock_allowed:
                if not restock_allowed:
                    return ProviderPartResult(
                        provider_namespace=effect.provider_namespace,
                        outcome="known_not_dispatched",
                        explicit_rejection_code="restock_subscription_scope_changed",
                    )
                from management.services.ig_fulfillment_context import validate_retained_fulfillment
                current = IgCustomerTurnRevision.objects.get(pk=revision.pk)
                if reason := validate_retained_fulfillment(current, (current.generation_proposal or {}).get("fulfillment_capture")):
                    return ProviderPartResult(provider_namespace=effect.provider_namespace,
                        outcome="known_not_dispatched", explicit_rejection_code="fulfillment_scope_changed")
                from management.services.ig_inventory_availability import validate_revision_inventory
                if reason := validate_revision_inventory(current, fact_bindings=effect.fact_bindings):
                    return ProviderPartResult(provider_namespace=effect.provider_namespace,
                        outcome="known_not_dispatched", explicit_rejection_code=reason)
                return original_transport(payload)

    transport = RevisionProviderTransport(original_transport.preflight, guarded_send)
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
    if states and all(state == IgRevisionDeliveryEffect.State.SENT for state in states):
        record_delivery(revision.pk)
        record_restock_delivery(revision.pk)
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
        from management.services.ig_revision_execution import _completion_decision, _whole_sent_proof

        effects = list(revision.delivery_effects.select_for_update().order_by("order_index", "id"))
        states = {effect.state for effect in effects}
        replacement_complete = bool("definite_failed" in states
            and _completion_decision(effects) == (True, False, "delivered")
            and _whole_sent_proof(revision, effects)[0])
        if "sent" not in states or (not states.issubset({"sent", "cancelled", "superseded"}) and not replacement_complete):
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
    from management.services.ig_response_plan import capture_response_plan
    original_cart = (receipt.get("proof") or {}).get("source_cart_capture")
    plan = capture_response_plan(revision.client, revision=revision, source_cart_capture=original_cart)
    fit_proof = (receipt.get("proof") or {}).get("kind") == "fit_consultation_fallback"
    chart_proof = (receipt.get("proof") or {}).get("kind") == "native_chart_fallback"
    media_chart_proof = chart_proof or (fit_proof and any(row["kind"] == "info:size_chart" for row in plan.obligations))
    local_response = ValidatedResponse(reply_text=reply)
    if chart_proof:
        proof = receipt["proof"]
        text, current_proof = _local_catalog_chart_candidate(revision, proof.get("language"))
        if text != reply or not current_proof or any(proof.get(key) != value for key, value in current_proof.items()):
            return RevisionLiveResult(revision.pk, "blocked", ("deterministic_native_chart_changed",))
    if (receipt.get("proof") or {}).get("kind") == "focused_selection_clarification":
        proof = receipt["proof"]
        if (proof.get("candidate_digest") != (plan.configuration.get("focus_selection_candidate") or {}).get("digest")
            or _focused_selection_clarification(plan, proof.get("language")) != reply):
            return RevisionLiveResult(revision.pk, "blocked", ("deterministic_focused_selection_changed",))
    if fit_proof:
        from management.services.ig_fit_consultation import deterministic_fit_reply

        proof = receipt["proof"]
        if (proof.get("fit_context_digest") != _digest(plan.fit_consultation)
            or deterministic_fit_reply(plan.fit_consultation, language=proof.get("language")) != reply):
            return RevisionLiveResult(revision.pk, "blocked", ("deterministic_fit_facts_changed",))
        if proof.get("presentation_product_id"):
            local_response = replace(local_response, controls=(ResponseControl("show_products", str(proof["presentation_product_id"])),))
    from management.services.ig_reply_authority import build_reply_truth_context
    context = (build_reply_truth_context(revision.client) if plan.line_plans
        else bot._provider_reply_truth_context(revision.client, {}, reply))
    from management.services.ig_fulfillment_context import current_fulfillment_frame
    from management.services.ig_reply_authority import apply_fulfillment_timing_context
    context = apply_fulfillment_timing_context(context, current_fulfillment_frame(revision, plan.source_cart_capture))
    truth = _captured_reply_truth(plan, local_response, context, revision=revision)
    if not truth.valid:
        return RevisionLiveResult(revision.pk, "blocked", truth.reasons)
    if plan.source_cart_capture.get("lines"):
        from management.services.ig_response_cart_fence import capture_source_cart_authority

        cart = capture_source_cart_authority(revision.client, plan.source_cart_capture, revision=revision)
        if not cart.ready:
            return RevisionLiveResult(revision.pk, "blocked", (cart.reason,))
        old = [row for row in authority.fact_bindings if row["claim"] == CLAIM_SOURCE_PREFERENCES]
        if old and (old[0].get("selector") or {}).get("source_cart_fence") != cart.binding:
            return RevisionLiveResult(revision.pk, "blocked", ("deterministic_source_cart_changed",))
        scoped = build_revision_authority_bindings(revision.client,
            claims=(CLAIM_SOURCE_PREFERENCES,), control={"source_cart_fence": cart.binding}, settings_obj=settings_row)
        if not scoped.ready:
            return RevisionLiveResult(revision.pk, "blocked", scoped.reasons)
        facts = tuple(row for row in authority.fact_bindings if row["claim"] != CLAIM_SOURCE_PREFERENCES) + scoped.fact_bindings
        combined = sorted((*facts, *authority.offer_bindings), key=lambda item: item["claim"])
        authority = replace(authority, fact_bindings=facts, authority_digest=_digest(combined))
    if (receipt.get("origin") == "source_preference_fallback" and not media_chart_proof) or (receipt.get("origin") == "static_reply" and plan.obligations):
        from management.services.ig_response_debt import record_response_coverage
        coverage = (receipt.get("proof") or {}).get("coverage")
        if receipt.get("origin") == "static_reply":
            coverage = _revision_response_coverage(plan, ValidatedResponse(reply_text=reply), revision=revision, local=True)
            # The verified static trigger is a handled control source. It does
            # not discharge additional size/purchase/information requests.
            trigger_ids = {f"{row['message_id']}:unclassified" for row in revision.bundle_snapshot.get("sources", [])
                           if row.get("text") == settings_row.trigger_text}
            handled = [item for item in coverage["remaining"] if item in trigger_ids]
            coverage["remaining"] = [item for item in coverage["remaining"] if item not in trigger_ids]
            coverage["covered"].extend(handled)
            coverage["disposition"] = "recovery" if coverage["remaining"] else "complete"
        if not coverage or not record_response_coverage(revision.pk, token, coverage):
            return RevisionLiveResult(revision.pk, "blocked", ("response_coverage_cas_failed",))
    prepared = prepare_text_effects(
        revision.client.igsid, reply, quick_replies=quick_replies,
        provider_namespace=revision.bundle_snapshot["sources"][0]["source_namespace"],
    )
    if prepared.error:
        return RevisionLiveResult(revision.pk, "blocked", (prepared.error,))
    effects = prepared.effects
    if media_chart_proof or (fit_proof and plan.fit_consultation.get("photos_requested")):
        effects, reasons = _prepare_effects(revision, local_response, settings_row, response_plan=plan)
        if reasons:
            return RevisionLiveResult(revision.pk, "blocked", reasons)
        from management.services.ig_revision_catalog_assets import prepare_catalog_asset_control
        from management.services.ig_revision_authority import CLAIM_CATALOG_ASSET_REFERENCES

        control, reasons = prepare_catalog_asset_control(revision.client, effects, control=local_response.control)
        if reasons:
            return RevisionLiveResult(revision.pk, "blocked", reasons)
        assets = build_revision_authority_bindings(revision.client, claims=(CLAIM_CATALOG_ASSET_REFERENCES,),
            control=control, settings_obj=settings_row)
        if not assets.ready:
            return RevisionLiveResult(revision.pk, "blocked", assets.reasons)
        facts = (*authority.fact_bindings, *assets.fact_bindings)
        authority = replace(authority, fact_bindings=facts,
            authority_digest=_digest(sorted((*facts, *authority.offer_bindings), key=lambda row: row["claim"])))
        if media_chart_proof:
            from management.services.ig_response_debt import record_response_coverage
            charts = [row for row in effects if (row.get("projection_metadata") or {}).get("catalog_chart")]
            fit_effect = next((row for row in effects if (row.get("projection_metadata") or {}).get("delivery") == "native"
                and "fit_illustration" in (row.get("projection_metadata") or {})), None)
            coverage = _revision_response_coverage(plan, _response_with_prepared_text(local_response, effects), revision=revision, local=True,
                catalog_media_evidence={"native_size_charts": charts, "native_fit_illustration": fit_effect})
            if not record_response_coverage(revision.pk, token, coverage):
                return RevisionLiveResult(revision.pk, "blocked", ("response_coverage_cas_failed",))
    access_token = bot.get_page_token(settings_row)
    if not access_token:
        return RevisionLiveResult(revision.pk, "blocked", ("provider_not_configured",))
    plan = plan_revision_effects(
        revision.pk, token, source_message_id=(receipt.get("source_message_id") or receipt["source_message_ids"][-1]),
        settings_id=receipt["settings_id"], settings_permission_epoch=receipt["settings_permission_epoch"],
        publication=publication, authority_context_digest=authority.authority_digest,
        effects=effects, fact_bindings=authority.fact_bindings,
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
    except DatabaseError:
        raise
    except Exception as exc:
        from management.services.ig_revision_recovery import record_local_preprovider_fault

        recorded = record_local_preprovider_fault(revision_id, token,
            stage="revision_execution", exception_type=type(exc).__name__)
        if not recorded.ready:
            raise
        try:
            from management.services.ig_terminal_fault_ack import record_terminal_fault_ack_candidate

            record_terminal_fault_ack_candidate(revision_id, settings_id=settings_row.pk)
        except Exception:
            # The original fault and its operator owner remain authoritative.
            # A failed optional acknowledgement admission never masks them.
            logging.getLogger(__name__).warning("revision_terminal_ack_candidate_unavailable")
        return RevisionLiveResult(revision_id, "blocked", (recorded.reason,))
    finally:
        _stop_revision_presence(scope)
        _PRESENCE_SCOPE.reset(context_token)


def _park_policy_fault_ack(revision_id, token, reason):
    """Keep failed finite acknowledgement debt; never rewrite its send ledger."""
    from management.services.ig_response_debt import park_manual_revision

    identity = IgCustomerTurnRevision.objects.filter(pk=revision_id).values("client_id").first()
    if identity is None:
        return
    with transaction.atomic():
        client = IgClient.objects.select_for_update().filter(pk=identity["client_id"]).first()
        revision = IgCustomerTurnRevision.objects.select_for_update().filter(
            pk=revision_id, claim_token=token).first()
        if client is not None and revision is not None:
            park_manual_revision(revision, f"policy_fault_ack_{reason}"[:100])


def _execute_claimed_revision(revision_id, token, settings_row) -> RevisionLiveResult:
    """Execute an explicitly claimed revision; never delegates to legacy sends."""
    if connection.in_atomic_block:
        return RevisionLiveResult(revision_id, "blocked", ("caller_transaction_active",))
    from management.services.ig_turn_intent import service_complaint_manual_reply_owned
    current = IgCustomerTurnRevision.objects.select_related("client").filter(pk=revision_id).first()
    if current is not None and service_complaint_manual_reply_owned(current):
        return RevisionLiveResult(revision_id, "blocked", ("service_complaint_manager_reply_owned",))
    from management.services import instagram_bot as bot

    settings_row = InstagramBotSettings.objects.select_related("active_instruction_publication").get(pk=settings_row.pk)
    revision = IgCustomerTurnRevision.objects.select_related("client").filter(pk=revision_id).first()
    if revision is None:
        return RevisionLiveResult(revision_id, "blocked", ("revision_missing",))
    from management.services.ig_revision_finalization_faults import finalization_fault_blocks_execution
    if finalization_fault_blocks_execution(revision):
        return RevisionLiveResult(revision_id, "blocked", ("finalization_proof_quarantined",))
    if bot.ingress_provider_namespace(settings_row) != revision.bundle_snapshot.get("sources", [{}])[0].get("source_namespace"):
        return RevisionLiveResult(revision_id, "blocked", ("provider_namespace_changed",))
    if revision.delivery_effects.exists():
        token_value = bot.get_page_token(settings_row)
        if not token_value:
            return RevisionLiveResult(revision_id, "blocked", ("provider_not_configured",))
        return _drain_effects(revision, token, settings_row, token_value)
    if not revision.generation_proposal_digest:
        from management.services.ig_payment_observation import observe_payment_source

        # Complete observations before the first context/proposal capture only.
        # Execution-only continuations retain their immutable captured proposal;
        # the independent source worker owns later observation/reconciliation.
        # Existing authority/epoch/source guards still check every real effect.
        for source in revision.bundle_snapshot.get("sources") or []:
            observe_payment_source(int(source["message_id"]))
        revision.client.refresh_from_db()
    # Incoming source facts have their own recipient/reset/privacy admission.
    # Output policy (no reply, static, disabled AI or manager pause) cannot erase
    # a customer's accepted choice or create send/effect authority from it.
    from management.services.ig_revision_commerce import reduce_revision_commerce
    commerce = reduce_revision_commerce(
        revision.pk, token, settings_id=settings_row.pk,
        settings_permission_epoch=settings_row.reply_permission_epoch, publication=None,
    )
    if not commerce.ready:
        return RevisionLiveResult(revision_id, "blocked", (commerce.reason,))
    revision.refresh_from_db()
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
    if input_decision.reason == "rate_limited":
        from management.services.ig_revision_rate_handoff import record_rate_handoff, park_rate_handoff

        handoff = record_rate_handoff(revision.pk, token, settings_id=settings_row.pk,
            automation_lease_token=(_PRESENCE_SCOPE.get() or {}).get("lease", ""))
        if not handoff.ready:
            return RevisionLiveResult(revision_id, "blocked", (handoff.reason,))
        if handoff.origin == "technical_holding":
            try:
                outcome = _execute_deterministic_input(revision, token, settings_row, handoff.receipt)
            finally:
                with transaction.atomic():
                    IgClient.objects.select_for_update().get(pk=revision.client_id)
                    current = IgCustomerTurnRevision.objects.select_for_update().filter(
                        pk=revision.pk, claim_token=token).first()
                    if current is not None:
                        park_rate_handoff(current)
            return outcome
        return RevisionLiveResult(revision_id, "delivery_pending", (handoff.reason,))
    if input_decision.origin == "no_reply":
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
    if input_decision.origin == "generate" and not revision.generation_proposal_digest:
        # A real source-bound case is persisted before any promise to check.
        # Mixed questions stay on the ordinary complete response-plan path.
        from management.services.ig_fulfillment_reviews import prepare_fulfillment_review
        fulfillment = prepare_fulfillment_review(revision, settings_obj=settings_row)
        if fulfillment.get("ready"):
            revision.refresh_from_db()
            input_decision.receipt["fulfillment_review"] = {
                "request_id": fulfillment.get("request_id"),
                "request_digest": fulfillment.get("request_digest"),
            }
        # Free text proposes a reminder time; only its signed, delivered
        # invitation can later establish business agreement. Multiple sources
        # and media remain on the complete ordinary response-plan path.
        sources = revision.bundle_snapshot.get("sources") or []
        if len(sources) == 1 and not sources[0].get("media_parts") and not sources[0].get("quick_reply_payload"):
            from management.services.ig_payment_reminders import prepare_request

            source = revision.sources.select_related("message").filter(
                message_id=sources[0]["message_id"], message__client_id=revision.client_id,
                message__role=InstagramBotMessage.Role.USER,
            ).first()
            if source is not None:
                reminder = prepare_request(revision, source.message, settings_obj=settings_row)
                if reminder.handled:
                    receipt = {**input_decision.receipt, "origin": "payment_reminder_request",
                        "source_message_id": source.message_id, "reply_text": reminder.reply_text,
                        "invitation_id": str(reminder.invitation_id) if reminder.invitation_id else None,
                        "reason": reminder.reason}
                    return _execute_deterministic_input(
                        revision, token, settings_row, receipt, quick_replies=reminder.quick_replies,
                    )
                from management.services.ig_restock_subscriptions import prepare_request as prepare_restock_request

                restock = prepare_restock_request(revision, source.message, settings_obj=settings_row)
                if restock.handled:
                    receipt = {**input_decision.receipt, "origin": "restock_subscription_request",
                        "source_message_id": source.message_id, "reply_text": restock.reply_text,
                        "invitation_id": str(restock.invitation_id) if restock.invitation_id else None,
                        "reason": restock.reason}
                    return _execute_deterministic_input(
                        revision, token, settings_row, receipt, quick_replies=restock.quick_replies,
                    )
    if not settings_row.ai_enabled or (
        input_decision.receipt["settings_permission_epoch"] != settings_row.reply_permission_epoch
        and not holding_recovery
    ):
        return RevisionLiveResult(revision_id, "blocked", ("settings_permission_changed",))
    publication = _publication(settings_row)
    if revision.generation_proposal_digest:
        if _digest(revision.generation_proposal) != revision.generation_proposal_digest:
            return RevisionLiveResult(revision_id, "blocked", ("generation_proposal_changed",))
        response = _restore_response(revision.generation_proposal)
        execution = revision.generation_proposal.get("execution_binding") or {}
        if execution.get("settings_id") != settings_row.pk or not isinstance(execution.get("settings_permission_epoch"), int):
            return RevisionLiveResult(revision_id, "blocked", ("proposal_execution_binding_missing",))
        boundary = RevisionGenerationBoundary(revision, token, settings_row, publication,
            source_cart_capture=revision.generation_proposal.get("source_cart_capture"))
        boundary.settings_epoch = execution["settings_permission_epoch"]
        boundary.authority = _authority_from_projection(revision.generation_proposal["authority"])
        original_publication = revision.generation_proposal["policy_manifest"]["instruction_publication"]
        publication = PublicationBinding(
            original_publication["id"], original_publication["version"], original_publication["hash"],
        )
        boundary.publication = publication
    else:
        # Pure source-proved fit/audience facts use the existing local receipt
        # and outbox without opening a provider graph. Mixed requests retain
        # the normal generation path and its complete obligation plan.
        if not GeminiRequest.objects.filter(logical_turn_id=f"ig-revision:{revision.pk}").exists():
            from management.services.ig_revision_input import record_source_preference_fallback

            chart_request = _pure_catalog_chart_request(revision)
            local = record_source_preference_fallback(revision.pk, token, settings_id=settings_row.pk,
                approved_fit_facts=not chart_request, approved_chart=chart_request)
            if local.ready:
                return _execute_deterministic_input(revision, token, settings_row, local.receipt)
        # A crashed request without its successful proposal is reconciliation
        # debt, never an excuse to open another two-dispatch request.
        if GeminiRequest.objects.filter(logical_turn_id=f"ig-revision:{revision.pk}").exists():
            from management.services.ig_revision_input import record_source_preference_fallback

            fallback = record_source_preference_fallback(revision.pk, token, settings_id=settings_row.pk)
            if fallback.ready:
                return _execute_deterministic_input(revision, token, settings_row, fallback.receipt)
            from management.services.ig_revision_holding import record_technical_holding

            holding = record_technical_holding(
                revision.pk, token, settings_id=settings_row.pk,
                allow_neutral=getattr(django_settings, "IG_REVISION_OPTIONAL_HOLDING_ENABLED", False),
            )
            if holding is not None and holding.ready:
                return _execute_deterministic_input(revision, token, settings_row, holding.receipt)
            return _unanswered_generation_result(revision, ("generation_reconciliation_required",))
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
            from management.services.ig_revision_recovery import (
                LOCAL_POLICY_READINESS_CODES, record_policy_readiness_failure,
            )
            policy_failure = getattr(boundary, "policy_readiness_failure", None) or {}
            if policy_failure.get("code") in LOCAL_POLICY_READINESS_CODES:
                recorded = record_policy_readiness_failure(
                    revision.pk, token, settings_id=settings_row.pk,
                    settings_permission_epoch=boundary.settings_epoch, publication=publication,
                    code=policy_failure["code"], details=policy_failure.get("details"), park=False,
                )
                if recorded.ready:
                    from management.services.ig_revision_holding import record_policy_fault_ack

                    acknowledgement = record_policy_fault_ack(revision.pk, token, settings_id=settings_row.pk)
                    if acknowledgement.ready:
                        try:
                            outcome = _execute_deterministic_input(revision, token, settings_row, acknowledgement.receipt)
                        except Exception:
                            _park_policy_fault_ack(revision.pk, token, "execution_exception")
                            raise
                        if outcome.state != "completed":
                            _park_policy_fault_ack(revision.pk, token,
                                outcome.reasons[0] if outcome.reasons else outcome.state)
                        return outcome
                    recorded = record_policy_readiness_failure(
                        revision.pk, token, settings_id=settings_row.pk,
                        settings_permission_epoch=boundary.settings_epoch, publication=publication,
                        code=policy_failure["code"], details=policy_failure.get("details"),
                    )
                return RevisionLiveResult(revision_id, "blocked", reasons if recorded.ready else
                    tuple(dict.fromkeys((*reasons, recorded.reason))))
            from management.services.ig_revision_input import record_source_preference_fallback

            fallback = record_source_preference_fallback(revision.pk, token, settings_id=settings_row.pk)
            if fallback.ready:
                return _execute_deterministic_input(revision, token, settings_row, fallback.receipt)
            from management.services.ig_revision_holding import record_technical_holding

            holding = record_technical_holding(
                revision.pk, token, settings_id=settings_row.pk,
                allow_neutral=getattr(django_settings, "IG_REVISION_OPTIONAL_HOLDING_ENABLED", False),
                product_inquiry_capture=getattr(boundary.revision, "_product_inquiry_capture", None),
            )
            if holding is not None and holding.ready:
                return _execute_deterministic_input(revision, token, settings_row, holding.receipt)
            return _unanswered_generation_result(revision, reasons)
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

        request_media = revision.generation_proposal.get("request_media_manifest") or {}
        if request_media.get("items") or request_media.get("outcomes"):
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
    assisted_checkout_requested = "assisted_checkout_proposal_create" in authority.allowed_actions
    checkout_requested = "checkout_proposal_create" in authority.allowed_actions or assisted_checkout_requested
    if checkout_requested:
        effects, checkout_reply, reasons = _prepare_visible_content(revision, response, settings_row)
    else:
        effects, reasons = _prepare_effects(revision, response, settings_row, response_plan=boundary.response_plan)
    if reasons:
        return RevisionLiveResult(revision_id, "blocked", reasons)
    if not checkout_requested:
        charts = [row for row in effects if (row.get("projection_metadata") or {}).get("catalog_chart")]
        fit_effect = next((row for row in effects if (row.get("projection_metadata") or {}).get("delivery") == "native"
            and "fit_illustration" in (row.get("projection_metadata") or {})), None)
        if charts or (fit_effect and fit_effect["projection_metadata"]["fit_illustration"].get("unavailable_chart_fits")):
            from management.services.ig_response_debt import record_response_coverage
            coverage = _revision_response_coverage(boundary.response_plan, _response_with_prepared_text(response, effects), revision=revision,
                checkout_cart_binding=boundary.checkout_cart_binding, catalog_media_evidence={"native_size_charts": charts, "native_fit_illustration": fit_effect,
                    "artifact": revision.generation_proposal.get("turn_intelligence") or {},
                    "binding": revision.generation_proposal.get("request_media_manifest") or {}})
            if not record_response_coverage(revision.pk, token, coverage):
                return RevisionLiveResult(revision_id, "blocked", ("response_coverage_cas_failed",))
    # Credentials may perform provider I/O; obtain them before the short atomic
    # plan commit. No raw token is persisted in the revision or its effects.
    access_token = bot.get_page_token(settings_row)
    if not access_token:
        return RevisionLiveResult(revision_id, "blocked", ("provider_not_configured",))
    generation = revision.generation_proposal["generation"]
    if checkout_requested:
        from management.services.ig_revision_checkout import authorize_revision_checkout, prepare_revision_checkout

        if assisted_checkout_requested:
            from management.services.ig_revision_checkout import prepare_revision_assisted_checkout
            checkout = prepare_revision_assisted_checkout(
                revision.pk, token, source_message_id=int(revision.bundle_snapshot["sources"][-1]["message_id"]),
                settings_id=settings_row.pk, settings_permission_epoch=boundary.settings_epoch,
                publication=publication, generation_proposal_digest=revision.generation_proposal_digest,
                authority=authority, reply_text=checkout_reply, noncheckout_effects=effects,
                source_cart_capture=boundary.response_plan.source_cart_capture)
            if not checkout.planned:
                return RevisionLiveResult(revision_id, "blocked", checkout.reasons)
            return _drain_effects(revision, token, settings_row, access_token)

        from management.services.instagram_bot import _source_purchase_assent
        checkout_sources = [source for source in reversed(revision.bundle_snapshot["sources"]) if source.get("role") == "user"]
        direct_sources = [source for source in checkout_sources if _source_purchase_assent(source.get("text") or "") > 0]
        checkout_source = next((
            source for source in (direct_sources or checkout_sources)
            if source.get("role") == "user" and not authorize_revision_checkout(
                revision.client, response.control, source.get("text") or "",
                sealed_sources=revision.bundle_snapshot["sources"],
                source_cart_capture=boundary.response_plan.source_cart_capture or None,
                source_cart_revision_id=revision.pk,
                assisted_context=boundary.assisted_context,
            )[1]
        ), None)
        if checkout_source is None:
            return RevisionLiveResult(revision_id, "blocked", ("checkout_purchase_authority_missing",))
        checkout = prepare_revision_checkout(
            revision.pk, token, source_message_id=int(checkout_source["message_id"]),
            settings_id=settings_row.pk, settings_permission_epoch=boundary.settings_epoch,
            publication=publication, generation_proposal_digest=revision.generation_proposal_digest,
            authority=authority, noncheckout_effects=effects, reply_text=checkout_reply,
            source_cart_capture=boundary.response_plan.source_cart_capture or None,
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
    from management.services.ig_revision_recovery import reserve_proposal_transport_tail
    tail = reserve_proposal_transport_tail(revision.pk, token, effects=effects,
        settings_id=settings_row.pk, authority=authority)
    if tail.state != "execution":
        return RevisionLiveResult(revision_id, "blocked", (tail.reason,))
    revision.refresh_from_db()
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


def _log_revision_blocked(revision_id, client_id, outcome, *, event="revision_execution"):
    """Keep proven identity and finite failure codes in the operational console."""
    from management.services import instagram_bot as bot
    from management.services.ig_console_read_model import REASONS

    reason = next((code for code in outcome.reasons if code in REASONS), "failed")
    bot.log(
        "warning", event,
        f"revision={revision_id} state={outcome.state} reasons={','.join(outcome.reasons)}",
        kind="reply_failed", scope={"revision_id": revision_id, "client_id": client_id},
        reason=reason,
    )


def _process_terminal_fault_acknowledgements(settings_row):
    """One already-admitted acknowledgement in the existing customer worker."""
    from management.services import instagram_bot as bot
    from management.services.ig_terminal_fault_ack import (
        terminal_fault_ack_revision_ids, claim_terminal_fault_ack, execute_terminal_fault_ack,
    )

    for revision_id in terminal_fault_ack_revision_ids(limit=1):
        if bot.maintenance_status()["active"]:
            break
        identity = IgCustomerTurnRevision.objects.filter(pk=revision_id).values("client_id").first()
        if not identity:
            continue
        client, lease = bot.acquire_client_automation_lease(identity["client_id"])
        if client is None:
            continue
        try:
            revision, token = claim_terminal_fault_ack(revision_id,
                settings_id=settings_row.pk, automation_lease_token=lease)
            if revision is None or not token:
                continue
            outcome = execute_terminal_fault_ack(revision_id, token, settings_row)
            if outcome.sent_parts:
                return 1
            if outcome.reasons:
                _log_revision_blocked(revision_id, identity["client_id"], outcome,
                    event="revision_terminal_fault_ack")
        except DatabaseError:
            raise
        except Exception as exc:
            bot.log("error", "revision_terminal_fault_ack",
                f"revision={revision_id} error={type(exc).__name__}")
        finally:
            bot.release_client_automation_lease(identity["client_id"], lease)
    return 0


def process_pending_revisions(settings_row, *, max_items=15, create_new=True) -> int:
    """Bounded optional worker; flag rollback only resumes canonical effects."""
    from management.services import instagram_bot as bot
    from management.services.ig_revision_execution import (
        available_client_automation_q, due_revision_ids, prepare_revision,
        expired_revision_debt_ids, record_expired_revision_debt,
    )

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
            settle_parked_collaboration_revision,
        )

        # Historical handoffs must make progress without occupying the entire
        # front of a worker pass while a fresh customer revision is due.
        for revision_id in parked_collaboration_revision_ids(limit=1):
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
                    settle_parked_collaboration_revision(
                        revision_id, token, reason=outcome.reasons[0],
                    )
                    _log_revision_blocked(revision_id, identity["client_id"], outcome,
                        event="revision_collaboration_recovery")
            except DatabaseError:
                raise
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
        try:
            from management.services.ig_revision_finalization_faults import finalization_fault_blocks_execution

            expired_revision = IgCustomerTurnRevision.objects.filter(pk=revision_id).first()
            if expired_revision is not None and finalization_fault_blocks_execution(expired_revision):
                continue
            recovery = None
            if recover_new and not IgRevisionDeliveryEffect.objects.filter(revision_id=revision_id, state__in=("sent", "provider_started", "unknown", "definite_failed")).exists():
                from management.services.ig_revision_recovery import schedule_revision_recovery

                recovery = schedule_revision_recovery(revision_id)
            if recovery is not None and recovery.reason == "finalization_proof_quarantined":
                continue
            if recovery is None or recovery.state not in {"waiting", "spawned", "cancelled", "execution"}:
                record_expired_revision_debt(
                    revision_id,
                    cutover_at=rollout.cutover_at if create_new else None,
                )
        except DatabaseError:
            raise
        except Exception as exc:
            bot.log("error", "revision_expiry_classification",
                f"revision={revision_id} stage=expiry_classification code=local_bookkeeping_fault error={type(exc).__name__}")
    if recover_new:
        from management.services.ig_revision_recovery import due_recovery_revision_ids, prepare_outage_recovery

        # Permission-invalid jobs must settle even when client lease admission
        # refuses them. This producer can cancel/park, never mint a new lifetime.
        for revision_id in due_recovery_revision_ids(limit=limit):
            try:
                recovery = prepare_outage_recovery(revision_id, classify_only=True)
                if recovery.state == "manual" and recovery.reason != "finalization_proof_quarantined":
                    record_expired_revision_debt(revision_id, cutover_at=rollout.cutover_at)
            except DatabaseError:
                raise
            except Exception as exc:
                bot.log("error", "revision_recovery_classification",
                    f"revision={revision_id} stage=recovery_classification code=local_bookkeeping_fault error={type(exc).__name__}")
    from management.services.ig_revision_commerce import source_producer_owned_q

    candidate_now = timezone.now()
    candidates = list(IgCustomerTurnRevision.objects.filter(
        state=IgCustomerTurnRevision.State.CLAIMED, lease_until__lte=candidate_now,
    ).filter(Q(overall_deadline__gt=candidate_now) | Q(recovery_state="execution"))
        .filter(available_client_automation_q(now=candidate_now))
        .exclude(claim_token__startswith="finalize:").exclude(claim_token__startswith="debt:")
        .filter(Q(delivery_effects__isnull=False) | (Q(active_slot=1) if create_new else (
            Q(active_slot=1) & source_producer_owned_q())))
        .order_by("lease_until", "id").values_list("id", flat=True).distinct()[:limit])
    if create_new:
        from management.services.ig_revision_recovery import due_preparation_recovery_ids, due_recovery_revision_ids
        candidates.extend(due_preparation_recovery_ids(limit=limit, available_only=True))
        candidates.extend(due_recovery_revision_ids(limit=limit, available_only=True))
        candidates.extend(due_revision_ids(limit=limit, cutover_at=rollout.cutover_at))
    else:
        # Intake already admitted these sources to the revision reducer. Finish
        # that owner across rollback even when preparation has not started yet.
        candidates.extend(due_revision_ids(limit=limit, source_producer_only=True))
    # Merge bounded lanes by source age instead of allowing a full stale lane
    # to hide every fresh head. Recheck availability before the combined LIMIT;
    # the exact lease CAS below remains the concurrency authority.
    candidates = list(IgCustomerTurnRevision.objects.filter(pk__in=candidates)
        .filter(available_client_automation_q(now=timezone.now()))
        .order_by("quiet_deadline", "id").values_list("pk", flat=True)[:max(0, limit - handled)])
    for revision_id in candidates:
        if bot.maintenance_status()["active"]:
            break
        identity = IgCustomerTurnRevision.objects.filter(pk=revision_id).values(
            "client_id", "state", "recovery_state", "recovery_code").first()
        if not identity:
            continue
        client, lease = bot.acquire_client_automation_lease(identity["client_id"])
        if client is None:
            continue
        presence_scope = {"lease": lease, "handle": None}
        try:
            if create_new and (identity["recovery_state"] == "waiting"
                and identity["recovery_code"] != "preparation_recovery_due"
                or identity["recovery_state"] == "manual"
                and identity["recovery_code"] == "recovery_existing_proposal_requires_execution"):
                from management.services.ig_revision_recovery import prepare_outage_recovery

                recovery = prepare_outage_recovery(revision_id, automation_lease_token=lease)
                if recovery.state == "spawned" and recovery.child_id:
                    revision_id = recovery.child_id
                    identity = {**identity, "state": IgCustomerTurnRevision.State.SEALED}
                elif recovery.state == "execution":
                    identity = {**identity, "state": IgCustomerTurnRevision.State.CLAIMED}
                else:
                    if recovery.state == "manual" and recovery.reason != "finalization_proof_quarantined":
                        record_expired_revision_debt(revision_id, cutover_at=rollout.cutover_at)
                    continue
            # An unstarted job receives its one fresh preparation window only
            # when this worker owns its client lease and can actually run it.
            if create_new and identity["state"] in {IgCustomerTurnRevision.State.COLLECTING, IgCustomerTurnRevision.State.PREPARING}:
                from management.services.ig_revision_recovery import (
                    schedule_revision_recovery, prepare_unstarted_preparation_recovery,
                )
                queued = IgCustomerTurnRevision.objects.only("overall_deadline", "recovery_code").get(pk=revision_id)
                if queued.overall_deadline <= timezone.now():
                    scheduled = schedule_revision_recovery(revision_id)
                    if scheduled.state not in {"waiting", "spawned"}:
                        continue
                    fresh = prepare_unstarted_preparation_recovery(revision_id, automation_lease_token=lease)
                    if fresh.state != "spawned" or not fresh.child_id:
                        continue
                    revision_id = fresh.child_id
                    identity = {**identity, "state": IgCustomerTurnRevision.State.COLLECTING}
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
                        capture_kwargs.setdefault("revision_id", revision_id)
                        capture_kwargs.setdefault("preparation_token", preparation_token)
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
                _log_revision_blocked(revision_id, identity["client_id"], outcome)
        except DatabaseError:
            raise
        except Exception as exc:
            # A durable lease/proposal/effect remains the recovery authority.
            # No source attempts, transcript, or legacy send key are reset.
            bot.log("error", "revision_execution", f"revision={revision_id} error={type(exc).__name__}")
        finally:
            _stop_revision_presence(presence_scope)
            bot.release_client_automation_lease(identity["client_id"], lease)
    # Fresh source work keeps its existing slot priority. This queue contains
    # only explicit new native-fault candidates, never a generic manual scan.
    if handled < limit:
        handled += _process_terminal_fault_acknowledgements(settings_row)
    return handled
