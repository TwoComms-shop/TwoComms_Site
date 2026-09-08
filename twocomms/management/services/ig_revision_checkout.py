"""Atomic first-party checkout URL and complete revision effect preparation.

No provider calls, model prices, or inferred chat agreements enter this boundary.
The bearer URL is retained only in the exact durable delivery payload.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Mapping

from django.conf import settings
from django.db import connection, transaction
from django.urls import reverse
from django.utils import timezone

from management.models import IgCheckoutAccessToken, IgClient, IgCustomerTurnRevision, InstagramBotSettings
from management.services.ig_checkout import CheckoutConfigurationError, create_or_update_proposal, validate_checkout_items
from management.services.ig_revision_actions import _authority_projection
from management.services.ig_revision_authority import (
    CLAIM_CATALOG_CONFIGURATION, CLAIM_CURRENT_OFFER, RevisionAuthorityBindingSet,
    build_revision_authority_bindings, check_fact_bindings, check_offer_bindings,
)
from management.services.ig_revision_outbox import (
    PublicationBinding, _digest, plan_revision_effects, pre_winner_readiness,
)
from management.services.ig_revision_transport import prepare_text_effects

ACTION = "checkout_proposal_create"


@dataclass(frozen=True)
class RevisionCheckoutResult:
    planned: bool = False
    created: bool = False
    effects: tuple = ()
    proposal_id: int = 0
    reasons: tuple[str, ...] = ()


class _Rollback(Exception):
    pass


def _payment_request(control):
    """Provider controls select a candidate; they cannot authorize an amount."""
    from management.services.ig_checkout_policy import PREPAY_200_AMOUNT

    paylink = str(control.get("paylink") or "").casefold()
    payment = control.get("payment")
    if paylink not in {"", "full", "online_full", "true", "prepay"}:
        return "", ("checkout_payment_policy_unsupported",)
    # As in finalize_paylink, a full-payment control ignores a model amount.
    # The amount remains the independently validated catalog total.
    if paylink in {"full", "online_full", "true"}:
        return "full", ()
    if payment is not None:
        try:
            amount = Decimal(str(payment).replace(",", "."))
        except (InvalidOperation, ValueError):
            return "", ("checkout_payment_amount_unsupported",)
        if not amount.is_finite() or amount != Decimal(PREPAY_200_AMOUNT):
            return "", ("checkout_payment_amount_unsupported",)
    return ("prepay" if paylink == "prepay" or payment is not None else "full"), ()


def _prepay_policy(client, sealed_sources):
    """Reproduce the existing 200+COD gate from exact sealed customer evidence."""
    from management.models import IgCheckoutProposal, InstagramBotMessage
    from management.services.ig_checkout_policy import resolve_payment_policy
    from management.services.ig_turn_revisions import MAX_SOURCES

    if not isinstance(sealed_sources, (list, tuple)) or not 1 <= len(sealed_sources) <= MAX_SOURCES:
        return None, ("checkout_payment_source_missing",)
    sources = {
        row.get("message_id"): row for row in sealed_sources
        if isinstance(row, Mapping) and row.get("role") == "user"
    }
    decision = resolve_payment_policy(client=client, evidence_message_ids=tuple(sources))
    if decision.policy != IgCheckoutProposal.PaymentPolicy.FULL_OR_200_COD:
        return None, ("checkout_prepay_not_authorized",)
    snapshot = sources.get(decision.evidence_message_id)
    source = InstagramBotMessage.objects.filter(
        pk=decision.evidence_message_id, client=client,
        role=InstagramBotMessage.Role.USER,
    ).only("text", "quick_reply_payload").first()
    if source is None or snapshot is None or (
        str(snapshot.get("text") or "") != str(source.text or "")
        or str(snapshot.get("quick_reply_payload") or "") != str(source.quick_reply_payload or "")
    ):
        return None, ("checkout_payment_source_changed",)
    return decision, ()


def checkout_authority_control(client, control):
    """Resolve provider selectors to a complete server-owned standard cart."""
    from management.services import instagram_bot as bot

    if not isinstance(control, Mapping) or control.get("_invalid"):
        return {}, ("checkout_control_invalid",)
    if "custom" in str(client.intent or "").casefold() or control.get("custom_print_lead_id"):
        return {}, ("custom_confirmation_and_price_authority_missing",)
    _request, reasons = _payment_request(control)
    if reasons:
        return {}, reasons
    if "items" in control:
        specs = bot._control_item_specs(dict(control))
        if not specs:
            return {}, ("checkout_items_invalid",)
    else:
        product_id = bot._control_product_id(dict(control)) or client.current_product_id
        selection = bot._checkout_selection_state(client, product_id)
        options = bot._control_option_values(dict(control))
        if options is None:
            return {}, ("checkout_options_invalid",)
        specs = [{
            "product_id": product_id,
            "qty": control.get("qty", client.current_qty or 1),
            "size": control.get("size", client.current_size or ""),
            "fit_option_code": control.get("fit", selection.get("fit_option_code", "")),
            "color_variant_id": control.get("variant") or control.get("color_variant_id") or selection.get("color_variant_id"),
            "option_values": {**(selection.get("option_values") or {}), **options},
        }]
    if len(specs) > 8:
        return {}, ("checkout_item_limit",)
    try:
        quote = validate_checkout_items(client=client, item_specs=specs, negotiated_total=None)
    except CheckoutConfigurationError as exc:
        return {}, ("checkout:" + exc.code,)
    return {"items": [{
        "product_id": item.product.pk,
        "color_variant_id": item.color_variant.pk if item.color_variant else None,
        "qty": item.quantity, "size": item.size,
        "fit_option_code": item.fit_code, "option_values": dict(item.option_values),
    } for item in quote.items]}, ()


def authorize_revision_checkout(client, control, source_text, *, sealed_sources=()):
    """Use the real purchase gate with sealed customer text, never model prose."""
    from management.services.instagram_bot import payment_link_allowed

    normalized, reasons = checkout_authority_control(client, control)
    if reasons:
        return normalized, reasons
    request, _reasons = _payment_request(control)
    if request == "prepay":
        _decision, reasons = _prepay_policy(client, sealed_sources)
        if reasons:
            return {}, reasons
    if not payment_link_allowed(client, dict(control), str(source_text or "")):
        return {}, ("checkout_purchase_authority_missing",)
    return normalized, ()


def _response(proposal):
    from management.services.ig_response_control import ResponseControl, ValidatedResponse

    row = proposal.get("response") or {}
    return ValidatedResponse(
        reply_text=row.get("reply_text") or "",
        controls=tuple(ResponseControl(item["kind"], item["value"]) for item in row.get("controls") or ()),
    )


def _composition(revision, response, reply_text):
    """Only the saved model text and existing deterministic catalog hint qualify."""
    from management.services import instagram_bot as bot

    expected = response.reply_text
    selection = bot._catalog_media_selection_for_control(response.control, revision.client)
    if selection is not None:
        expected = bot._append_more_products_hint(expected, selection, revision.client)
    if reply_text != expected or not reply_text or len(reply_text) > 4500:
        raise _Rollback("checkout_text_contract_mismatch")


def prepare_revision_checkout(
    revision_id: int, revision_token: str, *, source_message_id: int,
    settings_id: int, settings_permission_epoch: int, publication: PublicationBinding,
    generation_proposal_digest: str, authority: RevisionAuthorityBindingSet,
    noncheckout_effects=(), reply_text: str, now=None,
) -> RevisionCheckoutResult:
    """Commit offer, one token, and ALL physical parts or roll everything back."""
    if connection.in_atomic_block:
        return RevisionCheckoutResult(reasons=("caller_transaction_active",))
    if not isinstance(authority, RevisionAuthorityBindingSet) or not authority.ready or ACTION not in authority.allowed_actions:
        return RevisionCheckoutResult(reasons=("checkout_action_not_authorized",))
    identity = IgCustomerTurnRevision.objects.filter(pk=revision_id).values("client_id").first()
    if identity is None:
        return RevisionCheckoutResult(reasons=("revision_missing",))
    now = now or timezone.now()
    try:
        with transaction.atomic():
            settings_obj = InstagramBotSettings.objects.select_for_update().select_related("active_instruction_publication").filter(pk=settings_id).first()
            client = IgClient.objects.select_for_update().filter(pk=identity["client_id"]).first()
            revision = IgCustomerTurnRevision.objects.select_for_update().filter(pk=revision_id, client_id=identity["client_id"]).first()
            if revision is None or client is None or settings_obj is None:
                raise _Rollback("checkout_identity_missing")
            proposal = revision.generation_proposal
            if not generation_proposal_digest or revision.generation_proposal_digest != generation_proposal_digest or not isinstance(proposal, Mapping) or _digest(proposal) != generation_proposal_digest:
                raise _Rollback("generation_proposal_mismatch")
            captured_publication = (proposal.get("policy_manifest") or {}).get("instruction_publication") or {}
            if any(captured_publication.get(key) != value for key, value in (
                ("id", publication.publication_id), ("version", publication.version),
                ("hash", publication.snapshot_hash),
            )):
                raise _Rollback("generation_publication_mismatch")
            before = proposal.get("authority") or {}
            supplied = _authority_projection(authority)
            receipt = (revision.action_receipts or {}).get("client_configuration_update") or {}
            if supplied != before and not (
                receipt.get("before_authority") == before
                and receipt.get("after_authority") == supplied
                and receipt.get("generation_proposal_digest") == generation_proposal_digest
                and receipt.get("source_message_id") == source_message_id
            ):
                raise _Rollback("generation_proposal_authority_mismatch")
            if ACTION not in before.get("allowed_actions", ()):
                raise _Rollback("stored_checkout_action_not_authorized")
            source = revision.sources.filter(message_id=source_message_id, message__client_id=client.pk).first()
            snapshots = {row.get("message_id"): row for row in (revision.bundle_snapshot or {}).get("sources", ())}
            if source is None or source_message_id not in snapshots or source_message_id not in {row.get("message_id") for row in proposal.get("sources", ())}:
                raise _Rollback("source_not_in_revision")
            if snapshots[source_message_id].get("role") != "user":
                raise _Rollback("checkout_customer_source_required")
            generation = proposal.get("generation") or {}
            request_id = str(generation.get("request_id") or "")
            if not request_id:
                raise _Rollback("generation_request_missing")
            response = _response(proposal)
            _composition(revision, response, reply_text)
            prior = tuple(noncheckout_effects)
            if any(not isinstance(row, Mapping) or row.get("group") != "catalog_media" or row.get("kind") != "image" for row in prior):
                raise _Rollback("checkout_noncheckout_effect_invalid")
            plan_identity = _digest({
                "boundary": ACTION, "generation_proposal_digest": generation_proposal_digest,
                "source_message_id": source_message_id, "authority": supplied,
                "noncheckout_effects": prior, "reply_text": reply_text,
                "payment_policy": "hosted_catalog_standard_promo",
            })
            # The whole plan is the crash receipt. Never issue a replacement token
            # when an earlier preparation already committed its exact payload.
            existing = tuple(revision.delivery_effects.order_by("order_index", "id"))
            if existing:
                if any(row.authority_context_digest != plan_identity or row.generation_request_id != request_id or row.source_message_id != source_message_id or row.plan_digest != existing[0].plan_digest for row in existing):
                    raise _Rollback("checkout_plan_conflict")
                first = existing[0]
                if (first.settings_id_snapshot != settings_id
                    or first.settings_permission_epoch != settings_permission_epoch
                    or first.publication_id != publication.publication_id
                    or first.publication_version != publication.version
                    or first.publication_hash != publication.snapshot_hash):
                    raise _Rollback("checkout_plan_publication_mismatch")
                readiness = pre_winner_readiness(
                    revision.pk, revision_token, settings_id=settings_id,
                    settings_permission_epoch=settings_permission_epoch, publication=publication,
                    fact_bindings=first.fact_bindings, offer_bindings=first.offer_bindings,
                    fact_checker=check_fact_bindings, offer_checker=check_offer_bindings, now=now,
                )
                if not readiness.ready:
                    raise _Rollback("readiness:" + ",".join(readiness.reasons))
                offers = [row for row in first.offer_bindings if row.get("claim") == CLAIM_CURRENT_OFFER]
                if len(offers) != 1 or not offers[0].get("selector", {}).get("checkout_access_token_id"):
                    raise _Rollback("checkout_plan_offer_missing")
                return RevisionCheckoutResult(True, False, existing, offers[0]["subjects"]["proposal_id"])
            readiness = pre_winner_readiness(
                revision.pk, revision_token, settings_id=settings_id,
                settings_permission_epoch=settings_permission_epoch, publication=publication,
                fact_bindings=authority.fact_bindings, offer_bindings=authority.offer_bindings,
                fact_checker=check_fact_bindings, offer_checker=check_offer_bindings, now=now,
            )
            if not readiness.ready:
                raise _Rollback("readiness:" + ",".join(readiness.reasons))
            sealed_sources = tuple(snapshots.values())
            control, reasons = authorize_revision_checkout(
                client, response.control, snapshots[source_message_id].get("text", ""),
                sealed_sources=sealed_sources,
            )
            if reasons:
                raise _Rollback(",".join(reasons))
            bound = [row for row in authority.fact_bindings if row.get("claim") == CLAIM_CATALOG_CONFIGURATION]
            if len(bound) != 1 or bound[0].get("selector") != control:
                raise _Rollback("checkout_cart_authority_mismatch")
            payment_request, _reasons = _payment_request(response.control)
            payment_decision = None
            evidence_ids = [source_message_id]
            if payment_request == "prepay":
                payment_decision, reasons = _prepay_policy(client, sealed_sources)
                if reasons:
                    raise _Rollback(",".join(reasons))
                evidence_ids = [payment_decision.evidence_message_id]
            checkout = create_or_update_proposal(
                client=client, pay_type="online_full", item_specs=control["items"],
                negotiated_total=None, requested_payment_amount=None, allow_promo=True,
                evidence={"message_ids": evidence_ids}, locale=client.language,
            )
            if (
                payment_decision is not None
                or (
                    checkout.assisted_checkout_v2
                    and checkout.payment_policy == checkout.PaymentPolicy.FULL_OR_200_COD
                )
            ):
                current_decision, reasons = _prepay_policy(client, sealed_sources)
                if payment_decision is None:
                    payment_decision = current_decision
                if reasons or payment_decision is None or current_decision != payment_decision or not checkout.assisted_checkout_v2 or any(
                    getattr(checkout, field) != expected for field, expected in (
                        ("payment_policy", payment_decision.policy),
                        ("payment_policy_evidence_message_id", payment_decision.evidence_message_id),
                        ("payment_policy_evidence_kind", payment_decision.evidence_kind),
                        ("payment_policy_evidence_revision", payment_decision.evidence_revision),
                        ("payment_policy_evidence_digest", payment_decision.evidence_digest),
                        ("custom_print_full_only", False),
                    )
                ):
                    raise _Rollback("checkout_prepay_policy_unavailable")
            raw, token = IgCheckoutAccessToken.issue(proposal=checkout, kind=IgCheckoutAccessToken.Kind.BOT)
            base = str(getattr(settings, "SITE_BASE_URL", "") or getattr(settings, "BOT_PUBLIC_BASE_URL", "") or "https://twocomms.shop").rstrip("/")
            url = base + reverse("ig_checkout_token_entry", kwargs={"token": raw})
            text = reply_text.rstrip() + "\n\n" + url
            client.refresh_from_db()
            # Offer creation can create an episode; all bindings must reflect the
            # committed candidate, without rewriting immutable generation input.
            facts = []
            for old in authority.fact_bindings:
                fresh = build_revision_authority_bindings(client, claims=(old["claim"],), control=old["selector"], settings_obj=settings_obj)
                if not fresh.ready or fresh.fact_bindings[0]["authority_digest"] != old["authority_digest"]:
                    raise _Rollback("checkout_post_fact_unavailable")
                facts.extend(fresh.fact_bindings)
            # Asset authority is plan-only: never alter the saved generation
            # or accept caller-added bindings before its exact authority guard.
            from management.services.ig_revision_catalog_assets import prepare_catalog_asset_control
            from management.services.ig_revision_authority import CLAIM_CATALOG_ASSET_REFERENCES

            asset_control, asset_reasons = prepare_catalog_asset_control(client, prior, control=response.control)
            if asset_reasons:
                raise _Rollback("checkout_catalog_assets:" + ",".join(asset_reasons))
            if asset_control:
                assets = build_revision_authority_bindings(
                    client, claims=(CLAIM_CATALOG_ASSET_REFERENCES,),
                    control=asset_control, settings_obj=settings_obj,
                )
                if not assets.ready:
                    raise _Rollback("checkout_catalog_assets:" + ",".join(assets.reasons))
                facts.extend(assets.fact_bindings)
            offer = build_revision_authority_bindings(client, claims=(CLAIM_CURRENT_OFFER,), control={"checkout_access_token_id": token.pk}, settings_obj=settings_obj)
            if not offer.ready or offer.offer_bindings[0]["subjects"]["proposal_id"] != checkout.pk:
                raise _Rollback("checkout_post_offer_unavailable")
            from management.services.ig_reply_authority import build_reply_truth_context
            from management.services.ig_reply_truth import validate_reply_truth
            truth = validate_reply_truth(text, context=build_reply_truth_context(client, control=response.control, server_urls=(url,)))
            if not truth.valid:
                raise _Rollback("checkout_final_truth:" + ",".join(truth.reasons))
            namespace = revision.bundle_snapshot["sources"][0]["source_namespace"]
            prepared = prepare_text_effects(client.igsid, text, provider_namespace=namespace)
            if prepared.error:
                raise _Rollback("checkout_text:" + prepared.error)
            result = plan_revision_effects(
                revision.pk, revision_token, source_message_id=source_message_id,
                settings_id=settings_id, settings_permission_epoch=settings_permission_epoch,
                publication=publication, authority_context_digest=plan_identity,
                effects=(*prior, *prepared.effects), fact_bindings=facts,
                offer_bindings=offer.offer_bindings, fact_checker=check_fact_bindings,
                offer_checker=check_offer_bindings, generation_request_id=request_id,
                generation_model=generation.get("actual_model", ""), now=now,
            )
            if not result.effects or result.reasons:
                raise _Rollback("checkout_plan:" + ",".join(result.reasons))
            return RevisionCheckoutResult(True, result.created, result.effects, checkout.pk)
    except (CheckoutConfigurationError, _Rollback) as exc:
        return RevisionCheckoutResult(reasons=(str(exc),))
    except Exception:
        return RevisionCheckoutResult(reasons=("checkout_preparation_failed",))
