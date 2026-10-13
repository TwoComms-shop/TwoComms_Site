"""Full READY assisted approval -> the existing checkout/payment owners.

No provider call, Product, stock count, payment winner or Order is created here.
Fresh effect checks and historical paid evidence have separate boundaries.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field, replace
from decimal import Decimal
import uuid

from django.conf import settings
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils.crypto import salted_hmac
from django.utils import timezone

from management.services import ig_assisted_commerce as commerce

EXECUTION_SCHEMA = "ig-assisted-checkout-execution.v1"
_ISSUER = object()
CUSTOM_KEYS = ("product_id", "color_variant_id", "is_custom", "title", "color_name_custom",
    "size", "fit_option_code", "fit_option_label", "option_values", "option_labels", "qty",
    "unit_price", "line_total", "currency", "photo_asset_id", "photo_digest", "assisted_line")


class AssistedCheckoutError(ValueError):
    def __init__(self, reason):
        self.reason = str(reason)[:128]
        super().__init__(self.reason)


def _fail(reason):
    raise AssistedCheckoutError(reason)


def _reason(exc):
    return str(getattr(exc, "reason", None) or getattr(exc, "code", None) or "assisted_proof_invalid")[:128]


@dataclass(frozen=True)
class VerifiedAssistedExecution:
    execution_id: uuid.UUID
    proposal_id: int
    client_id: int
    execution_digest: str
    _issuer: object = field(repr=False, compare=False)

    def __post_init__(self):
        if self._issuer is not _ISSUER:
            _fail("verified_execution_requires_native_producer")


def _execution_query():
    return commerce._models()[2].objects.select_related("request", "decision", "proposal", "client", "episode")


def _proof(execution):
    return {"schema": EXECUTION_SCHEMA, "execution_id": str(execution.pk),
        "execution_digest": execution.envelope_hmac, "request_id": str(execution.request_id),
        "decision_id": str(execution.decision_id), "proposal_id": execution.proposal_id,
        "snapshot_digest": execution.snapshot_digest, "request_digest": execution.request_digest,
        "decision_digest": execution.decision_digest, "full_capture_digest": execution.full_capture_digest}


def _purchase(client, capture, settings_obj, *, revision=None, now):
    """Every line needs an actual own purchase source, separate from approval."""
    from management.models import IgCustomerTurnRevision, InstagramBotMessage
    from management.services.ig_response_cart_plan import source_line_preferences
    from management.services.ig_purpose_sources import owned_source
    from management.services.ig_commerce_turns import parse_turn
    proofs = []
    if not capture.get("coverage_complete") or not capture.get("lines"):
        _fail("checkout_purchase_authority_missing")
    for line in capture["lines"]:
        preferences = source_line_preferences(capture, line)
        evidence = (preferences.get("evidence") or {}).get("purchase_requested") or {}
        if preferences.get("values", {}).get("purchase_requested") is not True:
            _fail("checkout_purchase_authority_missing")
        source = InstagramBotMessage.objects.filter(pk=evidence.get("source_message_id"), client=client).first()
        if (not source or parse_turn(source.text).purchase_requested is not True
            or not source.provider_created_at or source.provider_created_at > now):
            _fail("checkout_purchase_authority_missing")
        source_revision = revision or IgCustomerTurnRevision.objects.filter(client=client,
            sources__message_id=source.pk, active_slot=1).order_by("-revision").first()
        if not source_revision:
            _fail("checkout_purchase_revision_missing")
        owned, reason = owned_source(source_revision, source, settings_obj)
        if not owned:
            _fail(reason)
        if source.pk not in (capture.get("fence") or {}).get("source_ids", []):
            _fail("checkout_purchase_source_changed")
        proofs.append(owned[2])
    # Full scope has one current native purchase action; old unrelated wishes
    # cannot become payable merely by sharing a manager's approved basket.
    if len({proof["message_id"] for proof in proofs}) != 1:
        _fail("checkout_purchase_scope_ambiguous")
    proof = proofs[0]
    actual_revision = IgCustomerTurnRevision.objects.get(pk=proof["revision_id"])
    return source, actual_revision, proof


def _bot_boundary(client, revision, settings_obj, *, now):
    from management.services.ig_revision_outbox import PublicationBinding, _cas_readiness
    if revision is None or revision.client_id != client.pk or settings_obj is None:
        _fail("bot_checkout_claim_missing")
    publication = getattr(settings_obj, "active_instruction_publication", None)
    if not publication:
        _fail("publication_changed")
    proposal = revision.generation_proposal
    captured = (proposal.get("policy_manifest") or {}).get("instruction_publication") if isinstance(proposal, dict) else None
    if (not revision.generation_proposal_digest or commerce._digest(proposal) != revision.generation_proposal_digest
        or not isinstance(captured, dict) or any(captured.get(key) != value for key, value in
            (("id", publication.pk), ("version", publication.version), ("hash", publication.snapshot_hash)))):
        _fail("generation_publication_mismatch")
    if "assisted_checkout_proposal_create" not in (proposal.get("authority") or {}).get("allowed_actions", ()):
        _fail("stored_assisted_checkout_action_missing")
    readiness = _cas_readiness(revision, client=client, settings_obj=settings_obj,
        revision_token=revision.claim_token, settings_id=settings_obj.pk,
        settings_permission_epoch=settings_obj.reply_permission_epoch,
        publication=PublicationBinding(publication.pk, publication.version, publication.snapshot_hash),
        fact_bindings=[], offer_bindings=[], now=now)
    if not readiness.ready:
        _fail(readiness.reasons[0] if readiness.reasons else "bot_checkout_not_current")


def _proposal_rows(proposal, revision, cap):
    """Exact persisted rows plus immutable approval identity, without effects."""
    from management.services.ig_revision_cart_binding import union_configuration, ASSISTED_SCHEMA
    frozen = revision.snapshot.get("items")
    items = list(proposal.items.order_by("position", "id"))
    if not isinstance(frozen, list) or len(items) != len(cap.lines) or len(frozen) != len(items):
        _fail("assisted_proposal_line_coverage_changed")
    rows, custom = [], []
    for index, (item, pinned, approved) in enumerate(zip(items, frozen, cap.lines, strict=True)):
        if item.position != index or not isinstance(pinned, dict):
            _fail("assisted_proposal_position_changed")
        identity = {"line_id": approved.line_id, "recipient_id": approved.recipient_id,
            "original_index": approved.original_index, "decision_id": str(cap.decision_id),
            "decision_digest": cap.decision_digest, "line_digest": approved.line_digest}
        if pinned.get("assisted_line") != identity:
            _fail("assisted_proposal_approval_line_changed")
        actual = {"product_id": item.product_id, "color_variant_id": item.color_variant_id,
            "qty": item.quantity, "size": item.size, "fit_option_code": item.fit_code,
            "option_values": item.option_values or {}, "unit_price": str(item.quoted_unit_price),
            "line_total": str(item.quoted_line_total)}
        if approved.item_kind == "custom_apparel":
            actual.update(is_custom=True, title=item.product_title, color_name_custom=item.color_label,
                currency="UAH", photo_asset_id=pinned.get("photo_asset_id"), photo_digest=pinned.get("photo_digest"),
                assisted_line=identity, fit_option_label=item.fit_label, option_labels=item.option_labels or {})
            if (item.product_id is not None or item.color_variant_id is not None
                or item.price_source != "manager_custom" or item.product_title != approved.title
                or item.color_label != approved.color or item.size != approved.size
                or item.fit_code != approved.fit_code or item.quantity != approved.quantity
                or item.quoted_unit_price != approved.unit_price or item.quoted_line_total != approved.ready_total
                or pinned.get("photo_asset_id") != (str(approved.image_id) if approved.image_id else None)
                or pinned.get("photo_digest") != approved.image_digest):
                _fail("assisted_custom_row_changed")
            custom.append({"proposal_item_id": item.pk, **{key: actual[key] for key in CUSTOM_KEYS}})
        elif approved.item_kind != "catalog_apparel" or item.product_id != approved.product_id:
            _fail("assisted_line_kind_changed")
        if (union_configuration(actual, schema=ASSISTED_SCHEMA) != union_configuration(pinned, schema=ASSISTED_SCHEMA)
            or Decimal(str(pinned.get("unit_price"))) != item.quoted_unit_price
            or Decimal(str(pinned.get("line_total"))) != item.quoted_line_total
            or item.catalog_unit_price != item.quoted_unit_price or item.catalog_line_total != item.quoted_line_total):
            _fail("assisted_proposal_snapshot_changed")
        rows.append({"proposal_item_id": item.pk, "position": index,
            "line_id": approved.line_id, "recipient_id": approved.recipient_id,
            "snapshot": deepcopy(pinned)})
    return rows, custom


def _validated_execution(execution, *, now, fresh):
    """Strong read-only guard; never acquire locks after a payment graph lock."""
    from management.models import IgCheckoutRevision, IgCustomerTurnRevision
    from management.services.ig_revision_cart_binding import validate_assisted_cart_binding, assisted_quote_evidence
    Request, Decision, _Execution, _Image = commerce._models()
    if not commerce.verify_assisted_evidence(execution, "execution"):
        _fail("assisted_execution_unverified")
    request, decision, proposal = execution.request, execution.decision, execution.proposal
    cap = commerce.verified_assisted_decision_from_evidence(request, decision, historical=not fresh)
    body = execution.envelope["snapshot"]
    if (body.get("schema") != EXECUTION_SCHEMA or execution.client_id != cap.client_id
        or execution.intention_id != cap.intention_id or execution.request_digest != cap.request_digest
        or execution.decision_digest != cap.decision_digest or execution.full_capture_digest != cap.full_capture_digest
        or execution.scope != "full" or len(cap.ready_lines) != len(cap.lines)
        or execution.quoted_total != cap.approved_total or execution.quoted_custom_quantity != cap.approved_custom_quantity
        or execution.currency != "UAH" or body.get("selected_line_ids") != [line.line_id for line in cap.lines]
        or body.get("deferred_line_ids") != [] or body.get("origin_full_capture") != cap.full_capture
        or (body.get("purchase_proof") or {}).get("message_id") != execution.purchase_source_id
        or (body.get("purchase_proof") or {}).get("revision_id") != execution.revision_id
        or body.get("scope_digest") != execution.scope_digest
        or commerce._digest({"scope": "full", "selected_line_ids": body["selected_line_ids"], "deferred_line_ids": []}) != execution.scope_digest):
        _fail("assisted_execution_terms_changed")
    revision = IgCheckoutRevision.objects.filter(pk=body.get("checkout_revision_id"), proposal=proposal,
        revision=body.get("proposal_revision")).first()
    binding = body.get("source_cart_binding")
    checked = validate_assisted_cart_binding(binding, episode_id=execution.episode_id)
    if (revision is None or not checked.ok or revision.snapshot != body.get("quote_snapshot")
        or proposal.revision != revision.revision or revision.snapshot.get("source_cart_binding") != binding
        or revision.snapshot.get("assisted_commerce") != assisted_quote_evidence(cap)
        or binding.get("assisted_commerce") != assisted_quote_evidence(cap)
        or body.get("approved_current_capture") != binding.get("current_capture")
        or body.get("proposal_id") != proposal.pk or body.get("public_id") != str(proposal.public_id)
        or body.get("episode_id") != execution.episode_id or proposal.client_id != execution.client_id
        or proposal.commercial_episode_id != execution.episode_id or execution.episode.client_id != execution.client_id
        or execution.episode.deal_id != proposal.deal_id or proposal.deal.client_id != execution.client_id
        or proposal.items_digest != revision.snapshot.get("digest")
        or proposal.quoted_total != execution.quoted_total or proposal.catalog_total != execution.quoted_total
        or proposal.currency != "UAH" or proposal.pay_type != "online_full"
        or proposal.requested_payment_amount != execution.quoted_total
        or proposal.negotiated_discount != 0 or proposal.allow_promo or not proposal.assisted_checkout_v2
        or execution.expires_at > decision.expires_at or execution.expires_at > proposal.expires_at):
        _fail("assisted_execution_owner_or_quote_changed")
    rows, custom = _proposal_rows(proposal, revision, cap)
    if rows != body.get("rows"):
        _fail("assisted_execution_rows_changed")
    if fresh:
        from management.services.ig_commerce_projection import capture_current_selection_lines
        from management.services.ig_revision_cart_binding import same_checkout_source_capture
        from management.services.ig_purpose_sources import owned_source
        if (execution.active_slot != 1 or execution.order_id is not None or execution.released_at
            or execution.settled_at or now >= execution.expires_at or execution.created_at > now
            or execution.client.current_commercial_episode_id != execution.episode_id
            or execution.episode.intended_order_id is not None):
            _fail("assisted_execution_not_current")
        if (Request.objects.filter(intention_id=execution.intention_id).order_by("-version").values_list("pk", flat=True).first() != request.pk
            or Decision.objects.filter(request=request).order_by("-version").values_list("pk", flat=True).first() != decision.pk):
            _fail("assisted_decision_superseded")
        source = request.envelope["snapshot"]
        settings_obj, _permission = commerce._permission(execution.client, source["settings_id"],
            expected_settings_epoch=source["settings_permission_epoch"], expected_client_epoch=request.client_permission_epoch)
        for proof in source["sources"]:
            member = request.revision.sources.filter(message_id=proof["message_id"]).select_related("message").first()
            owned, reason = owned_source(request.revision, member.message, settings_obj, require_head=False) if member else (None, "source_not_owned")
            if not owned or owned[2] != proof:
                _fail(reason or "request_source_changed")
        purchase_revision = IgCustomerTurnRevision.objects.filter(pk=execution.revision_id).first()
        owned, reason = owned_source(purchase_revision, execution.purchase_source, settings_obj, require_head=False) if purchase_revision else (None, "source_not_owned")
        if not owned or owned[2] != body.get("purchase_proof"):
            _fail(reason or "purchase_source_changed")
        current = capture_current_selection_lines(execution.client_id, now=now)
        if not same_checkout_source_capture(body.get("current_quote_capture"), current):
            _fail("assisted_execution_source_changed")
        task = commerce._task(execution.client, request.task_id)
        if task:
            head = task.manager_context.get("assisted_commerce") or {}
            if (head.get("request_id") != str(request.pk) or head.get("decision_id") != str(decision.pk)
                or head.get("request_digest") != request.envelope_hmac or head.get("decision_digest") != decision.envelope_hmac):
                _fail("manager_case_head_changed")
        cap = replace(cap, _current_capture_json=commerce._json(current))
    verified = VerifiedAssistedExecution(execution.pk, proposal.pk, execution.client_id, execution.envelope_hmac, _ISSUER)
    return {"ready": True, "reason": "", "proof": _proof(execution), "custom_rows": custom,
        "source_cart_binding": deepcopy(binding), "verified_execution": verified,
        "verified_decision": cap, "current_capture": cap.current_capture}


def revalidate_verified_assisted_execution(value, *, client=None, now=None):
    """Quote reconstruction calls this guard, which never reconstructs a quote."""
    if type(value) is not VerifiedAssistedExecution or value._issuer is not _ISSUER:
        _fail("verified_execution_requires_native_producer")
    if client is not None and client.pk != value.client_id:
        _fail("assisted_execution_client_changed")
    execution = _execution_query().filter(pk=value.execution_id, proposal_id=value.proposal_id, client_id=value.client_id).first()
    if not execution or execution.envelope_hmac != value.execution_digest:
        _fail("assisted_execution_changed")
    return _validated_execution(execution, now=commerce._now(now), fresh=True)["verified_decision"]


def validate_assisted_proposal(proposal, now=None, fresh=True):
    from management.services.ig_checkout import rebuild_assisted_quote_from_execution, CheckoutConfigurationError
    try:
        if type(fresh) is not bool:
            _fail("assisted_validation_mode_invalid")
        execution = _execution_query().filter(proposal_id=getattr(proposal, "pk", None)).first()
        if not execution:
            _fail("assisted_execution_missing")
        result = _validated_execution(execution, now=commerce._now(now), fresh=fresh)
        if fresh:
            quote = rebuild_assisted_quote_from_execution(client=execution.client, verified_execution=result["verified_execution"])
            if quote.digest != execution.proposal.items_digest or quote.quoted_total != execution.quoted_total:
                _fail("assisted_quote_changed")
        return result
    except (AssistedCheckoutError, commerce.AssistedDecisionError, CheckoutConfigurationError, KeyError, TypeError, ValueError) as exc:
        return {"ready": False, "reason": _reason(exc)}


def lock_assisted_checkout_owner(proposal_id):
    """Call before Deal -> Proposal -> Generation -> Attempt locks, inside atomic."""
    from management.models import IgClient
    if not transaction.get_connection().in_atomic_block:
        _fail("assisted_owner_transaction_required")
    Request, Decision, Execution, _Image = commerce._models()
    identity = Execution.objects.filter(proposal_id=proposal_id).values("pk", "client_id", "request_id", "decision_id").first()
    if identity is None:
        return None
    IgClient.objects.select_for_update().get(pk=identity["client_id"])
    Request.objects.select_for_update().get(pk=identity["request_id"])
    Decision.objects.select_for_update().get(pk=identity["decision_id"])
    return Execution.objects.select_for_update().get(pk=identity["pk"])


def _url(proposal, token=None):
    base = str(getattr(settings, "SITE_BASE_URL", "") or getattr(settings, "BOT_PUBLIC_BASE_URL", "") or "https://twocomms.shop").rstrip("/")
    return base + (reverse("ig_checkout_token_entry", kwargs={"token": token}) if token else
        reverse("ig_checkout_proposal", kwargs={"proposal_id": proposal.public_id}))


def _access_token(execution, *, now, create=False):
    """Recover only the original native bearer; never persist its raw value."""
    from management.models import IgCheckoutAccessToken
    identity = f"{execution.pk}:{execution.proposal.public_id}:{execution.operation_id}"
    keys = [settings.SECRET_KEY, *getattr(settings, "SECRET_KEY_FALLBACKS", ())]
    for secret in keys:
        raw = salted_hmac("ig-assisted-checkout-token.v1", identity, secret=secret, algorithm="sha256").hexdigest()
        token = IgCheckoutAccessToken.objects.filter(proposal_id=execution.proposal_id,
            token_digest=IgCheckoutAccessToken.digest(raw), kind=IgCheckoutAccessToken.Kind.BOT).first()
        if token is not None:
            if token.revoked_at or token.expires_at <= now or execution.expires_at <= now:
                _fail("assisted_access_token_unavailable")
            return raw
    if not create:
        _fail("assisted_access_token_unavailable")
    if execution.expires_at <= now or execution.proposal.expires_at <= now:
        _fail("assisted_access_token_unavailable")
    raw = salted_hmac("ig-assisted-checkout-token.v1", identity, secret=settings.SECRET_KEY, algorithm="sha256").hexdigest()
    IgCheckoutAccessToken.objects.create(proposal_id=execution.proposal_id,
        token_digest=IgCheckoutAccessToken.digest(raw), kind=IgCheckoutAccessToken.Kind.BOT,
        expires_at=min(execution.expires_at, execution.proposal.expires_at))
    return raw


def prepare_assisted_checkout(client_id, *, actor, request_id, decision_id, expected_request_digest,
        expected_decision_digest, operation_id, revision_id=None, settings_id=None):
    from management.models import IgClient, IgCustomerTurnRevision, InstagramBotSettings, IgCheckoutAccessToken
    from management.services.ig_commercial_episodes import commercial_episode_client_lock
    from management.services.ig_checkout import (build_assisted_quote, create_or_update_proposal,
        _deal_item_snapshot, CheckoutConfigurationError)
    from management.services.ig_revision_cart_binding import build_assisted_cart_binding
    from management.services.ig_commerce_projection import capture_current_selection_lines
    from management.services.ig_inventory import reserve_proposal_inventory, InventoryReservationError
    try:
        now = commerce._now()
        request_id, decision_id = commerce._uuid(request_id), commerce._uuid(decision_id)
        operation_id = commerce._uuid(operation_id, "operation_invalid")
        executing = commerce._actor(getattr(actor, "pk", None)) if actor is not None else None
        operation_input = {"client_id": client_id, "request_id": str(request_id), "decision_id": str(decision_id),
            "request_digest": expected_request_digest, "decision_digest": expected_decision_digest,
            "actor_id": executing.pk if executing else None, "revision_id": revision_id, "settings_id": settings_id}
        input_digest = commerce._digest(operation_input)
        Request, Decision, Execution, _Image = commerce._models()
        with commercial_episode_client_lock(client_id), transaction.atomic():
            client = IgClient.objects.select_for_update().filter(pk=client_id).first()
            if not client or client.privacy_erasure_started_at or client.hidden_at:
                _fail("client_unavailable")
            revision = IgCustomerTurnRevision.objects.select_for_update().filter(pk=revision_id, client=client).first() if revision_id else None
            if executing is None:
                settings_obj = InstagramBotSettings.objects.select_related("active_instruction_publication").filter(pk=settings_id).first()
                _bot_boundary(client, revision, settings_obj, now=now)
            previous = _execution_query().filter(operation_id=operation_id).first()
            if previous:
                if (previous.client_id != client.pk or previous.envelope.get("operation_input_digest") != input_digest):
                    _fail("operation_conflict")
                result = _validated_execution(previous, now=now, fresh=False)
                raw = _access_token(previous, now=now)
                return {**result, "proposal": previous.proposal, "proposal_id": previous.proposal_id,
                    "public_id": str(previous.proposal.public_id), "checkout_url": _url(previous.proposal, raw),
                    "token": None, "replayed": True}
            request = Request.objects.select_for_update().filter(pk=request_id, client=client).first()
            decision = Decision.objects.select_for_update().select_related("actor").filter(pk=decision_id, request=request, client=client).first() if request else None
            if not decision:
                _fail("approved_decision_missing")
            if Execution.objects.filter(intention_id=request.intention_id).exists():
                _fail("assisted_intention_already_executed")
            # The original real approver is verified as the approval subject
            # for bot initiation; it is not logged as a fabricated bot actor.
            cap = commerce.load_verified_assisted_decision(request.pk, decision.pk,
                actor=executing or decision.actor, expected_request_digest=expected_request_digest,
                expected_decision_digest=expected_decision_digest, now=now)
            if not cap.lines or len(cap.ready_lines) != len(cap.lines):
                _fail("assisted_full_ready_required")
            settings_obj, _permission = commerce._permission(client, cap.source_proof["settings_id"],
                expected_settings_epoch=cap.source_proof["settings_permission_epoch"],
                expected_client_epoch=request.client_permission_epoch)
            purchase, purchase_revision, purchase_proof = _purchase(client, cap.current_capture, settings_obj, revision=revision, now=now)
            quote = build_assisted_quote(client=client, verified_decision=cap)
            binding = build_assisted_cart_binding(verified_decision=cap,
                quoted_items=[_deal_item_snapshot(item) for item in quote.items])
            proposal = create_or_update_proposal(client=client, pay_type="online_full", item_specs=[],
                allow_promo=False, evidence={"message_ids": sorted(set(cap.current_capture["fence"]["source_ids"]) | {purchase.pk})},
                source_cart_binding=binding, source_cart_capture=cap.full_capture,
                verified_assisted_decision=cap, locale=client.language)
            quote_revision = proposal.revisions.get(revision=proposal.revision)
            binding = deepcopy(quote_revision.snapshot["source_cart_binding"])
            rows, _custom = _proposal_rows(proposal, quote_revision, cap)
            current = capture_current_selection_lines(client.pk, now=now)
            if current.get("status") != "captured" or not current.get("coverage_complete"):
                _fail("assisted_owned_rebind_capture_missing")
            scope = {"scope": "full", "selected_line_ids": [line.line_id for line in cap.lines], "deferred_line_ids": []}
            scope_digest = commerce._digest(scope)
            execution = Execution(intention_id=request.intention_id, operation_id=operation_id,
                request=request, decision=decision, proposal=proposal, client=client,
                episode_id=proposal.commercial_episode_id, choice_source=None, purchase_source=purchase,
                revision=purchase_revision, scope="full", scope_digest=scope_digest,
                request_digest=request.envelope_hmac, decision_digest=decision.envelope_hmac,
                full_capture_digest=request.full_capture_digest, currency="UAH", quoted_total=quote.quoted_total,
                quoted_custom_quantity=cap.approved_custom_quantity, expires_at=min(proposal.expires_at, cap.expires_at))
            commerce._seal(execution, "execution", {"schema": EXECUTION_SCHEMA, **scope,
                "scope_digest": scope_digest, "origin_full_capture": cap.full_capture,
                "approved_current_capture": cap.current_capture, "current_quote_capture": current,
                "source_cart_binding": binding, "quote_snapshot": deepcopy(quote_revision.snapshot),
                "checkout_revision_id": quote_revision.pk, "proposal_revision": proposal.revision,
                "proposal_id": proposal.pk, "public_id": str(proposal.public_id),
                "episode_id": proposal.commercial_episode_id, "rows": rows, "purchase_proof": purchase_proof,
                "initiator": {"kind": "manager" if executing else "bot", "actor_id": executing.pk if executing else None}},
                operation_input_digest=input_digest)
            # Real catalog pools retain ordinary policy/finite holds. Custom
            # rows consume this exact one-shot approval, never fabricated stock.
            reserve_proposal_inventory(proposal, expires_at=execution.expires_at, require_policy=True)
            result = validate_assisted_proposal(proposal, now=commerce._now(), fresh=True)
            if not result.get("ready"):
                _fail(result.get("reason") or "assisted_execution_invalid")
            raw = _access_token(execution, now=commerce._now(), create=True)
            return {**result, "proposal": proposal, "proposal_id": proposal.pk, "public_id": str(proposal.public_id),
                "checkout_url": _url(proposal, raw), "token": raw, "replayed": False}
    except (AssistedCheckoutError, commerce.AssistedDecisionError, CheckoutConfigurationError,
            InventoryReservationError, KeyError, TypeError, ValueError) as exc:
        return {"ready": False, "reason": _reason(exc), "replayed": False}
    except IntegrityError:
        return {"ready": False, "reason": "assisted_execution_conflict", "replayed": False}


def validate_assisted_attempt(attempt, now=None):
    """Read original frozen attempt/provenance, independent of current choice."""
    from orders.models import PaymentAttempt
    from management.models import IgCheckoutInvoiceGeneration
    from management.services.ig_checkout_cart_provenance import validate_attempt_provenance, CartProvenanceError
    try:
        if not isinstance(attempt, PaymentAttempt):
            _fail("assisted_attempt_native_row_required")
        actual_attempt = PaymentAttempt.objects.filter(pk=attempt.pk).first()
        if not actual_attempt or actual_attempt.cart_snapshot != attempt.cart_snapshot:
            _fail("assisted_attempt_row_changed")
        attempt = actual_attempt
        snapshot = attempt.cart_snapshot
        locator = IgCheckoutInvoiceGeneration.objects.filter(payment_attempt=attempt).values(
            "proposal_id", "proposal__assisted_commerce_execution__id").first()
        execution_id = locator.get("proposal__assisted_commerce_execution__id") if locator else None
        cart = snapshot.get("cart") if isinstance(snapshot, dict) else None
        markers = isinstance(snapshot, dict) and ("assisted_execution" in snapshot or "assisted_commerce" in snapshot
            or any(isinstance(row, dict) and (row.get("is_custom") is True or row.get("product_id") is None)
                for row in (cart if isinstance(cart, list) else [])))
        if execution_id is None:
            if markers:
                _fail("assisted_execution_missing")
            return {"ready": True, "reason": "", "is_assisted": False, "proof": None, "custom_rows": []}
        proof = snapshot.get("assisted_execution") if isinstance(snapshot, dict) else None
        if not isinstance(proof, dict):
            _fail("assisted_attempt_proof_missing")
        execution = _execution_query().filter(pk=execution_id).first()
        if not execution:
            _fail("assisted_execution_missing")
        result = _validated_execution(execution, now=commerce._now(now), fresh=False)
        provenance = validate_attempt_provenance(snapshot)
        owner = provenance.get("owner") or {}
        body = execution.envelope["snapshot"]
        if (proof != result["proof"] or provenance.get("status") != "bound"
            or provenance.get("binding") != result["source_cart_binding"]
            or snapshot.get("assisted_commerce") != body["quote_snapshot"]["assisted_commerce"]
            or owner.get("proposal_pk") != execution.proposal_id or owner.get("client_id") != execution.client_id
            or owner.get("episode_id") != execution.episode_id or owner.get("revision_id") != body["checkout_revision_id"]
            or owner.get("revision") != body["proposal_revision"]
            or attempt.gross_amount != execution.quoted_total or attempt.discount_amount != 0
            or not IgCheckoutInvoiceGeneration.objects.filter(payment_attempt=attempt, proposal_id=execution.proposal_id).exists()):
            _fail("assisted_attempt_binding_changed")
        ack = snapshot.get("assisted_terms_ack")
        if (not isinstance(ack, dict) or set(ack) != {"version", "accepted", "accepted_at", "proposal_id",
                "proposal_revision", "execution_proof_digest", "grant_id"}
            or type(ack.get("version")) is not int or ack["version"] != 1 or ack.get("accepted") is not True
            or ack.get("proposal_id") != str(execution.proposal.public_id)
            or type(ack.get("proposal_revision")) is not int or ack["proposal_revision"] != body["proposal_revision"]
            or ack.get("execution_proof_digest") != commerce._digest(result["proof"])
            or not isinstance(ack.get("grant_id"), str) or not 1 <= len(ack["grant_id"]) <= 64
            or commerce._date(ack.get("accepted_at"), "assisted_terms_ack_invalid") > min(attempt.created, commerce._now(now))):
            _fail("assisted_terms_ack_invalid")
        expected = {row["proposal_item_id"]: row for row in result["custom_rows"]}
        seen = set()
        for row in snapshot.get("cart", []):
            identity = row.get("proposal_item_id")
            if row.get("is_custom") is True or row.get("product_id") is None:
                custom = expected.get(identity)
                if not custom or identity in seen or any(row.get(key) != custom[key] for key in CUSTOM_KEYS):
                    _fail("assisted_attempt_custom_row_changed")
                seen.add(identity)
        if seen != set(expected):
            _fail("assisted_attempt_custom_coverage_changed")
        return {**result, "is_assisted": True}
    except (AssistedCheckoutError, commerce.AssistedDecisionError, CartProvenanceError, KeyError, TypeError, ValueError) as exc:
        return {"ready": False, "reason": _reason(exc)}


def _order_map(attempt, order, execution, result):
    from management.models import IgCheckoutInvoiceGeneration, IgOrderAssignment
    from management.services.ig_checkout_cart_provenance import bind_order_items, validate_attempt_provenance
    proposal = execution.proposal
    generation = IgCheckoutInvoiceGeneration.objects.filter(payment_attempt=attempt, proposal=proposal).first()
    if (attempt.order_id != order.pk or attempt.status != "converted" or not attempt.checkout_winner_claimed
        or not attempt.monobank_invoice_id or not generation or generation.winner_slot != 1
        or generation.state != "paid_winner" or proposal.status != "paid"
        or proposal.winner_invoice_generation_id != generation.pk or proposal.payment_attempt_id != attempt.pk
        or proposal.deal.order_id != order.pk or execution.episode.intended_order_id != order.pk
        or not IgOrderAssignment.objects.filter(order=order, client_id=execution.client_id, unassigned_at__isnull=True).exists()
        or not isinstance(order.payment_payload, dict) or order.payment_payload.get("attempt_id") != attempt.pk
        or order.payment_payload.get("assisted_execution") != result["proof"]):
        _fail("assisted_order_winner_unverified")
    if (attempt.paid_amount is None or attempt.paid_amount <= 0 or attempt.paid_amount != attempt.payment_amount
        or order.payment_invoice_id != attempt.monobank_invoice_id or order.total_sum != attempt.gross_amount
        or order.discount_amount != attempt.discount_amount):
        _fail("assisted_order_payment_or_totals_changed")
    metadata = order.payment_payload.get("assisted_custom_items") or []
    if not isinstance(metadata, list):
        _fail("assisted_order_custom_metadata_changed")
    by_id = {row.get("order_item_id"): row for row in metadata if isinstance(row, dict)}
    if len(by_id) != len(metadata):
        _fail("assisted_order_custom_metadata_changed")
    actual = []
    items = list(order.items.order_by("pk"))
    for item in items:
        row = {"id": item.pk, "order_id": order.pk, "product_id": item.product_id,
            "color_variant_id": item.color_variant_id, "qty": item.qty, "size": item.size,
            "fit_option_code": item.fit_option_code, "option_values": item.option_values,
            "unit_price": str(item.unit_price), "line_total": str(item.line_total)}
        if item.is_custom:
            custom = by_id.get(item.pk)
            if custom is None:
                _fail("assisted_order_custom_metadata_missing")
            row.update(is_custom=True, title=item.title, color_name_custom=item.color_name_custom,
                fit_option_label=item.fit_option_label, option_labels=item.option_labels,
                **{key: custom.get(key) for key in ("currency", "photo_asset_id", "photo_digest", "assisted_line")})
        elif item.pk in by_id or item.product_id is None:
            _fail("assisted_order_line_kind_changed")
        actual.append(row)
    if set(by_id) != {item.pk for item in items if item.is_custom}:
        _fail("assisted_order_custom_coverage_changed")
    provenance = validate_attempt_provenance(attempt.cart_snapshot)
    bound = bind_order_items(provenance, cart_items=attempt.cart_snapshot["cart"], order_id=order.pk, order_items=actual)
    if order.payment_payload.get("source_cart_provenance") != bound:
        _fail("assisted_order_item_map_changed")
    custom_rows = {row["proposal_item_id"]: row for row in result["custom_rows"]}
    return [{"proposal_item_id": row["proposal_item_id"], "order_item_id": row["order_item_id"],
        "line_id": row["line_id"], "photo_asset_id": custom_rows.get(row["proposal_item_id"], {}).get("photo_asset_id"),
        "photo_digest": custom_rows.get(row["proposal_item_id"], {}).get("photo_digest", "")}
        for row in bound["item_map"]]


def attach_assisted_order(attempt, order, now=None):
    from management.services.ig_checkout_cart_provenance import CartProvenanceError
    try:
        if not transaction.get_connection().in_atomic_block:
            _fail("assisted_order_transaction_required")
        result = validate_assisted_attempt(attempt, now=now)
        if not result.get("ready") or result.get("is_assisted") is not True:
            _fail(result.get("reason"))
        execution = _execution_query().filter(pk=result["proof"]["execution_id"]).first()
        item_map = _order_map(attempt, order, execution, result)
        payload = {"execution_id": str(execution.pk), "request_id": str(execution.request_id),
            "decision_id": str(execution.decision_id), "proof_digest": commerce._digest(result["proof"]), "item_map": item_map}
        if execution.order_id is not None:
            if execution.order_id != order.pk or order.payment_payload.get("ig_assisted_commerce") != payload:
                _fail("assisted_order_attachment_conflict")
            return {"ready": True, "reason": "", "execution_id": str(execution.pk), "item_map": item_map, "replayed": True}
        if execution.active_slot != 1 or execution.released_at or execution.settled_at:
            _fail("assisted_execution_terminal_conflict")
        execution.order = order
        execution.settled_at = commerce._now(now)
        execution.active_slot = None
        execution.save(update_fields=["order", "settled_at", "active_slot"])
        order.payment_payload = {**order.payment_payload, "ig_assisted_commerce": payload}
        order.save(update_fields=["payment_payload"])
        return {"ready": True, "reason": "", "execution_id": str(execution.pk), "item_map": item_map, "replayed": False}
    except (AssistedCheckoutError, commerce.AssistedDecisionError, CartProvenanceError, KeyError, TypeError, ValueError) as exc:
        return {"ready": False, "reason": _reason(exc)}


def validate_assisted_order(order, execution=None, now=None):
    from orders.models import PaymentAttempt, Order
    from management.services.ig_checkout_cart_provenance import CartProvenanceError
    try:
        order = Order.objects.filter(pk=getattr(order, "pk", None)).first()
        if order is None:
            _fail("assisted_order_missing")
        actual = _execution_query().filter(order_id=getattr(order, "pk", None)).first()
        if not actual or (execution is not None and actual.pk != commerce._uuid(getattr(execution, "pk", execution))):
            _fail("assisted_order_execution_missing")
        attempt = PaymentAttempt.objects.filter(order=order).first()
        if not attempt:
            _fail("assisted_order_attempt_missing")
        result = validate_assisted_attempt(attempt, now=now)
        if not result.get("ready") or result.get("is_assisted") is not True:
            _fail(result.get("reason"))
        item_map = _order_map(attempt, order, actual, result)
        payload = {"execution_id": str(actual.pk), "request_id": str(actual.request_id),
            "decision_id": str(actual.decision_id), "proof_digest": commerce._digest(result["proof"]), "item_map": item_map}
        if (actual.order_id != order.pk or actual.active_slot is not None or actual.settled_at is None
            or actual.released_at or order.payment_payload.get("ig_assisted_commerce") != payload):
            _fail("assisted_order_attachment_changed")
        return {"ready": True, "reason": "", "execution_id": str(actual.pk), "item_map": item_map}
    except (AssistedCheckoutError, commerce.AssistedDecisionError, CartProvenanceError, KeyError, TypeError, ValueError) as exc:
        return {"ready": False, "reason": _reason(exc)}


def _offer_message(reason):
    if reason == "assisted_stock_recount_required":
        return "Спочатку звірте фактичний залишок позицій каталогу. Перевірена кількість для клієнта не є загальним складським залишком."
    if reason == "assisted_catalog_price_changed":
        return "Ціна позицій каталогу змінилася. Менеджеру потрібно повторно перевірити та затвердити актуальні умови."
    if reason == "assisted_catalog_facts_changed":
        return "Опис або конфігурація позицій каталогу змінилися. Підготуйте нову версію запиту та повторно затвердьте актуальні умови."
    if reason in {"inventory_mapping_unknown", "inventory_mapping_ambiguous", "inventory_mapping_unsupported",
            "inventory_stock_identity_unknown", "exact_size_count_requires_editor", "exact_size_count_invalid",
            "disabled_size_requires_editor", "assisted_catalog_quote_unavailable"}:
        return "Менеджеру потрібно перевірити налаштування, фактичний залишок та актуальні умови позицій каталогу."
    return ""


def _offer_catalog_readiness(client, cap, *, now):
    """Observation only: the same finite protected pools as explicit recount.

    This unconsumed approval has no owned proposal hold to exclude. Manager
    checked quantities never replace physical accounting or current pricing.
    """
    from management.services import ig_fulfillment_stock_sync as inventory
    from management.services.ig_checkout import build_assisted_quote, CheckoutConfigurationError
    catalog = [line for line in cap.lines if line.item_kind == "catalog_apparel"]
    if catalog:
        entries = [{"line_id": line.line_id, "target": {"product_id": line.product_id,
            "color_variant_id": line.color_variant_id, "size": line.size,
            "fit_option_code": line.fit_code, "option_values": line.as_dict()["option_values"],
            "quantity": line.quantity}, "configuration_digest": line.line_digest,
            "label": line.title} for line in catalog]
        try:
            _allocations, pools = inventory._capture_inventory_pools(entries, now, lock=False)
        except inventory.StockSyncError as exc:
            _fail(exc.reason)
        if any(pool["before_total"] < pool["minimum_total"] or any(
                type(size["before_total"]) is int and size["before_total"] < size["minimum_total"]
                for size in pool["size_counts"].values()) for pool in pools):
            _fail("assisted_stock_recount_required")
    try:
        # Use the same canonical aggregate tier/recipient/configuration price
        # resolver as proposal creation; no quote, reservation or token writes.
        build_assisted_quote(client=client, verified_decision=cap)
    except CheckoutConfigurationError as exc:
        _fail(_reason(exc))


def current_assisted_offer(client, revision=None, now=None):
    """Called only for an explicit assisted signal; no second approval reader."""
    from management.models import InstagramBotSettings
    request = decision = None
    try:
        now = commerce._now(now)
        Request, Decision, _Execution, _Image = commerce._models()
        request = Request.objects.filter(client_id=client.pk).order_by("-created_at", "-pk").first()
        if not request:
            return {"ready": False, "reason": "assisted_request_absent", "can_prepare_checkout": False}
        decision = Decision.objects.select_related("actor").filter(request=request).order_by("-version").first()
        if not decision or decision.status != "approved":
            return {"ready": False, "reason": "approved_decision_missing", "request": request, "decision": decision, "can_prepare_checkout": False}
        execution = _execution_query().filter(request=request, decision=decision).first()
        if execution:
            result = validate_assisted_proposal(execution.proposal, now=now, fresh=True)
            if not result.get("ready"):
                _fail(result.get("reason") or "assisted_quote_changed")
            cap = result["verified_decision"]
            # An already prepared proposal is dialog truth, never capacity for
            # another operation. The root effect owner replays its same UUID.
            can_prepare, prepare_reason = False, "assisted_checkout_already_prepared"
        else:
            cap = commerce.load_verified_assisted_decision(request.pk, decision.pk, actor=decision.actor,
                expected_request_digest=request.envelope_hmac, expected_decision_digest=decision.envelope_hmac, now=now)
            result = {"proof": {"request_id": str(request.pk), "decision_id": str(decision.pk),
                "request_digest": request.envelope_hmac, "decision_digest": decision.envelope_hmac}}
            can_prepare, prepare_reason = False, "assisted_full_ready_required"
            if cap.lines and len(cap.ready_lines) == len(cap.lines):
                _offer_catalog_readiness(client, cap, now=now)
                settings_obj = InstagramBotSettings.objects.filter(pk=cap.source_proof["settings_id"]).first()
                try:
                    _purchase(client, cap.current_capture, settings_obj, revision=revision, now=now)
                    can_prepare, prepare_reason = True, ""
                except AssistedCheckoutError as exc:
                    prepare_reason = exc.reason
        return {**result, "ready": True, "reason": "", "request": request, "decision": decision,
            "verified_decision": cap, "rows": [line.as_dict() for line in cap.lines], "proof": result["proof"],
            "current_capture": cap.current_capture, "can_prepare_checkout": can_prepare,
            "prepare_reason": prepare_reason, "can_prepare_reason": prepare_reason,
            "prepare_message": _offer_message(prepare_reason)}
    except (AssistedCheckoutError, commerce.AssistedDecisionError, KeyError, TypeError, ValueError) as exc:
        reason = _reason(exc)
        return {"ready": False, "reason": reason, "request": request, "decision": decision,
            "can_prepare_checkout": False, "prepare_reason": reason, "can_prepare_reason": reason,
            "prepare_message": _offer_message(reason)}
