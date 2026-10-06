"""Server-side payment adapter for Instagram checkout proposals.

The proposal is the commercial snapshot.  This module only turns a validated,
first-submit recipient into the existing ``PaymentAttempt`` contract; it never
accepts a client-supplied amount or cart.
"""
from __future__ import annotations

import hashlib
import json
import logging
import secrets
from datetime import timedelta, timezone as datetime_timezone
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from orders.models import PaymentAttempt
from orders.nova_poshta_checkout import NovaPoshtaSelectionError, resolve_delivery_selection
from orders.nova_poshta_documents import normalize_checkout_phone
from orders.promo_reservations import (
    PromoReservationError,
    release_payment_attempt_promo,
    reserve_promo_for_checkout,
)

from management.services.ig_inventory import (
    commit_proposal_inventory,
    mark_overbooked_proposal_inventory,
    release_proposal_inventory,
    reserve_proposal_inventory,
)

logger = logging.getLogger("management.ig_checkout_payment")


class CheckoutPaymentError(ValueError):
    """A safe, user-facing validation or lifecycle error."""

    def __init__(self, code: str, message: str, *, field: str = ""):
        super().__init__(message)
        self.code = code
        self.field = field
        self.message = message


_STALE_INVOICE_LEASE = object()


def _verified_payment_at(attempt, *, fallback=None):
    """Return provider payment time from the frozen successful observation."""
    for observation in reversed(list(attempt.payment_history or [])):
        if not isinstance(observation, dict):
            continue
        if str(observation.get("status") or "").lower() != "success":
            continue
        payload = observation.get("payload")
        if not isinstance(payload, dict):
            continue
        parsed = parse_datetime(str(payload.get("modifiedDate") or ""))
        if parsed is None:
            continue
        if timezone.is_naive(parsed):
            parsed = timezone.make_aware(
                parsed,
                timezone.get_current_timezone(),
            )
        return parsed
    return attempt.last_status_at or fallback or timezone.now()


def _invoice_creation_lease_expired(event_state, *, now=None):
    """Treat a malformed or elapsed lease as unsafe to retry blindly."""
    raw_expires_at = (event_state or {}).get("invoice_creation_lease_expires_at")
    if not raw_expires_at:
        return False
    expires_at = parse_datetime(str(raw_expires_at))
    if expires_at is None:
        return True
    if timezone.is_naive(expires_at):
        expires_at = timezone.make_aware(expires_at, timezone.get_current_timezone())
    return expires_at <= (now or timezone.now())


def _clean(value, limit=255):
    return " ".join(str(value or "").split())[:limit]


def _fingerprint(proposal, *, full_name, phone, email, delivery, promo_code):
    payload = {
        "version": 1,
        "proposal": str(proposal.public_id),
        "revision": proposal.revision,
        "recipient": {
            "full_name": full_name,
            "phone": phone,
            "email": email.lower(),
        },
        "delivery": {
            "city": delivery.city,
            "office": delivery.np_office,
            "settlement_ref": delivery.settlement_ref,
            "city_ref": delivery.city_ref,
            "warehouse_ref": delivery.warehouse_ref,
        },
        "promo": promo_code,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def _proposal_basket(proposal, *, promo_discount=Decimal("0.00")):
    basket = []
    for item in _snapshot(proposal)["cart"]:
        basket.append({
            "name": _clean(item["title"], 128),
            "qty": item["qty"],
            "sum": int((Decimal(item["line_total"]) * 100).to_integral_value()),
            "unit": "шт",
        })
    total_discount = Decimal(proposal.negotiated_discount or 0) + promo_discount
    if total_discount > 0:
        basket.append({
            "name": "Знижка",
            "qty": 1,
            "sum": -int((total_discount * 100).to_integral_value()),
            "unit": "шт",
        })
    if len(basket) > 30:
        return [{"name": "Замовлення", "qty": 1, "sum": sum(row["sum"] for row in basket), "unit": "шт"}]
    return basket


def _invoice_payload(request, attempt, proposal, *, payment_amount, promo_discount):
    if attempt.pay_type in {
        PaymentAttempt.PayType.PREPAYMENT,
        PaymentAttempt.PayType.PREPAY_200,
    }:
        description = (
            f"Передоплата замовлення {attempt.reference} на суму {payment_amount:.2f} грн. "
            f"Повна сума після знижок: {attempt.payable_amount:.2f} грн."
        )
        basket = [{
            "name": f"Передоплата за замовленням {attempt.reference}"[:128],
            "qty": 1,
            "sum": int((payment_amount * 100).to_integral_value()),
            "unit": "шт",
        }]
    else:
        description = f"Оплата замовлення {attempt.reference} на суму {payment_amount:.2f} грн."
        basket = _proposal_basket(proposal, promo_discount=promo_discount)
    public_base = getattr(settings, "MONOBANK_PUBLIC_BASE_URL", "").rstrip("/")
    if public_base:
        return_url = f"{public_base}{reverse('monobank_return')}?attemptId={attempt.pk}"
        webhook_url = f"{public_base}{reverse('monobank_webhook')}"
    else:
        return_url = request.build_absolute_uri(f"{reverse('monobank_return')}?attemptId={attempt.pk}")
        webhook_url = request.build_absolute_uri(reverse("monobank_webhook"))
    merchant_info = {
        "reference": attempt.reference,
        "destination": description,
        "basketOrder": basket,
    }
    # Preserve the canonical cart checkout contract when the customer asks
    # for an email receipt; Monobank accepts the field as optional.
    if attempt.email:
        merchant_info["customerEmails"] = [attempt.email]
    return {
        "amount": int((payment_amount * 100).to_integral_value()),
        "ccy": 980,
        "merchantPaymInfo": merchant_info,
        "redirectUrl": return_url,
        "webHookUrl": webhook_url,
    }


def _proposal_brigade_policy(proposal):
    """Use frozen line totals for promotions, live taxonomy for payment safety."""
    from storefront.services.brigade_commerce import get_product_brigade_policy
    full_only = False
    eligible = Decimal("0.00")
    frozen_items = _frozen_proposal_items(proposal)
    for index, item in enumerate(proposal.items.select_related("product__category").order_by('position', 'id')):
        if item.product_id and get_product_brigade_policy(item.product)["is_brigade"]:
            full_only = True
        else:
            frozen = frozen_items[index] if index < len(frozen_items) else {}
            parts = frozen.get('price_parts') or []
            if parts:
                # Public presentation also uses this policy, including paid
                # historical proposals whose catalog product may be deleted.
                # Invalid frozen parts never unlock a promotional subtotal.
                try:
                    if (frozen.get('product_id') == item.product_id
                            and sum(int(part['qty']) for part in parts) == item.quantity
                            and sum((int(part['qty']) * Decimal(part['unit_price']) for part in parts), Decimal('0.00')) == item.quoted_line_total):
                        eligible += sum((int(part['qty']) * Decimal(part['unit_price']) for part in parts if part.get('promo_eligible', False)), Decimal('0.00'))
                except (KeyError, TypeError, ValueError, ArithmeticError):
                    pass
            else:
                eligible += Decimal(item.quoted_line_total or 0)
    eligible = max(eligible - Decimal(proposal.negotiated_discount or 0), Decimal("0.00"))
    return full_only, eligible


def _validate_payload(proposal, payload, *, user=None):
    from storefront.services.brigade_commerce import brigade_payment_error
    brigade_full_only, promo_eligible_total = _proposal_brigade_policy(proposal)
    if brigade_full_only and proposal.pay_type != proposal.PayType.ONLINE_FULL:
        raise CheckoutPaymentError("brigade_full_payment_required", brigade_payment_error(), field="pay_type")
    full_name = _clean(payload.get("full_name"), 200)
    if len(full_name.split()) < 2:
        raise CheckoutPaymentError("full_name", "Вкажіть ім'я та прізвище.", field="full_name")

    raw_phone = _clean(payload.get("phone"), 32)
    phone = normalize_checkout_phone(raw_phone)
    if not phone:
        raise CheckoutPaymentError(
            "phone", "Вкажіть коректний український номер телефону.", field="phone"
        )

    email = _clean(payload.get("email"), 254)
    if email:
        try:
            validate_email(email)
        except ValidationError as exc:
            raise CheckoutPaymentError("email", "Перевірте email для чека.", field="email") from exc

    try:
        delivery = resolve_delivery_selection(payload)
    except NovaPoshtaSelectionError as exc:
        raise CheckoutPaymentError(exc.field, exc.message, field=exc.field) from exc

    promo_code = _clean(payload.get("promo_code"), 32).upper()
    promo = None
    promo_discount = Decimal("0.00")
    promo_event_state = {}
    if promo_code:
        if not proposal.allow_promo:
            raise CheckoutPaymentError("promo_unavailable", "Промокод для этого предложения недоступен.", field="promo_code")
        try:
            # Keep the capacity reservation and the negotiated-discount gate
            # in one savepoint.  This helper is normally called from the
            # proposal transaction, but retaining the boundary here prevents
            # a direct/replayed invocation from burning a guest capability
            # before the stacking policy rejects it.
            with transaction.atomic():
                reservation = reserve_promo_for_checkout(
                    code=promo_code,
                    user=user,
                    # A prepayment is only the amount collected now. Promo value
                    # belongs to the full eligible merchandise total and therefore
                    # reduces the later balance, not the agreed deposit itself.
                    total_amount=promo_eligible_total,
                )
                if (
                    Decimal(str(proposal.negotiated_discount or 0)) > 0
                    and reservation.promo.is_guest_ugc_capability()
                ):
                    raise CheckoutPaymentError(
                        "promo_unavailable",
                        "Промокод для этого предложения недоступен.",
                        field="promo_code",
                    )
        except PromoReservationError as exc:
            error = "promo_requires_account" if exc.reason == "account_required" else "promo_invalid"
            message = (
                "Этот промокод доступен только в личном кабинете."
                if error == "promo_requires_account"
                else "Промокод недействителен или уже использован."
            )
            raise CheckoutPaymentError(error, message, field="promo_code") from exc
        promo = reservation.promo
        promo_discount = reservation.discount
        promo_event_state = reservation.event_state

    order_payable = max(
        Decimal(proposal.quoted_total) - promo_discount,
        Decimal("0.00"),
    )
    if order_payable <= 0:
        raise CheckoutPaymentError("invalid_amount", "Сумма заказа должна быть больше нуля.")
    payment_amount = (
        min(Decimal(proposal.requested_payment_amount), order_payable)
        if proposal.pay_type == proposal.PayType.PREPAYMENT
        else order_payable
    )
    if payment_amount <= 0:
        raise CheckoutPaymentError("invalid_amount", "Сумма платежа должна быть больше нуля.")
    return {
        "full_name": full_name,
        "phone": phone,
        "email": email,
        "delivery": delivery,
        "promo": promo,
        "promo_code": promo_code,
        "promo_discount": promo_discount,
        "promo_event_state": promo_event_state,
        "order_payable": order_payable,
        "payment_amount": payment_amount,
    }


def release_attempt_promo(attempt, *, reason="payment_terminal"):
    """Release one assisted-checkout promo slot exactly once."""
    return release_payment_attempt_promo(attempt, reason=reason)


def _frozen_proposal_items(proposal):
    from management.models import IgCheckoutRevision
    revision = IgCheckoutRevision.objects.filter(proposal=proposal, revision=proposal.revision).order_by("-id").first()
    return (revision.snapshot or {}).get("items", []) if revision else []


def _snapshot(proposal):
    frozen = _frozen_proposal_items(proposal)
    items = []
    for index, item in enumerate(proposal.items.order_by("position", "id")):
        if not item.product_id:
            raise CheckoutPaymentError("item_unavailable", "Один из товаров больше недоступен.")
        base = {
            "product_id": item.product_id,
            "title": item.product_title,
            "qty": int(item.quantity or 1),
            "size": item.size or "",
            "fit_option_code": item.fit_code or "",
            "fit_option_label": item.fit_label or "",
            "color_variant_id": item.color_variant_id,
            "option_values": item.option_values or {},
            "option_labels": item.option_labels or {},
            "unit_price": str(item.quoted_unit_price),
            "line_total": str(item.quoted_line_total),
            "proposal_item_id": item.pk,
        }
        frozen_item = frozen[index] if index < len(frozen) else {}
        parts = frozen_item.get("price_parts") or []
        if parts:
            try:
                valid = (frozen_item.get("product_id") == item.product_id
                         and sum(int(part["qty"]) for part in parts) == item.quantity
                         and all(int(part["qty"]) > 0 and Decimal(part["unit_price"]) >= 0 for part in parts)
                         and sum((int(part["qty"]) * Decimal(part["unit_price"]) for part in parts), Decimal("0.00")) == item.quoted_line_total)
            except (KeyError, TypeError, ValueError, ArithmeticError):
                valid = False
            if not valid:
                raise CheckoutPaymentError("catalog_changed", "Умови комплекту змінилися. Оновіть пропозицію.")
            for part in parts:
                qty = int(part["qty"])
                unit = Decimal(part["unit_price"])
                items.append({**base, "qty": qty, "unit_price": str(unit), "line_total": str(unit * qty),
                              "pricing_rule": part.get("rule", ""), "promo_eligible": bool(part.get("promo_eligible", False)),
                              "bundle_pricing": frozen_item.get("bundle_pricing") or {}})
        else:
            items.append(base)
    if not items:
        raise CheckoutPaymentError("empty_items", "В предложении нет товаров.")
    # This metadata is server-owned and travels with the frozen attempt.  The
    # generic PaymentAttempt materializer uses it to preserve the commercial
    # source on the canonical Order without coupling orders back to IG models.
    return {
        "cart": items,
        "custom_print_lead_ids": [],
        "checkout_surface": "instagram_proposal",
        "sale_source": "Instagram",
        "proposal_id": str(proposal.public_id),
    }


def _revalidate_frozen_proposal(proposal):
    """Reject catalog drift before recipient data or an invoice is locked."""
    from management.models import IgCheckoutRevision
    from management.services.ig_checkout import validate_checkout_items

    revision = (
        IgCheckoutRevision.objects.filter(
            proposal=proposal,
            revision=proposal.revision,
        )
        .order_by("-id")
        .first()
    )
    # Hand-built historical fixtures may predate revision persistence. Real
    # bot-created proposals always have a revision and are fully revalidated.
    if revision is None:
        return

    item_specs = [
        {
            "product_id": item.product_id,
            "color_variant_id": item.color_variant_id,
            "qty": item.quantity,
            "size": item.size,
            "fit_option_code": item.fit_code,
            "option_values": item.option_values or {},
        }
        for item in proposal.items.order_by("position", "id")
    ]
    evidence_ids = list(revision.evidence_message_ids or [])
    try:
        quote = validate_checkout_items(
            client=proposal.client,
            item_specs=item_specs,
            evidence={"message_ids": evidence_ids},
            pay_type=proposal.pay_type,
            negotiated_total=(
                proposal.quoted_total
                if proposal.negotiated_discount > 0
                else None
            ),
            requested_payment_amount=proposal.requested_payment_amount,
            allow_promo=proposal.allow_promo,
        )
    except Exception as exc:
        raise CheckoutPaymentError(
            "catalog_changed",
            "Один із товарів або його умови змінилися. Попросіть бота оновити пропозицію.",
        ) from exc

    if (
        quote.digest != proposal.items_digest
        or quote.catalog_total != proposal.catalog_total
        or quote.quoted_total != proposal.quoted_total
        or quote.requested_payment_amount != proposal.requested_payment_amount
    ):
        raise CheckoutPaymentError(
            "catalog_changed",
            "Один із товарів або його умови змінилися. Попросіть бота оновити пропозицію.",
        )


def _capture_attempt_tracking(request, attempt):
    """Freeze the first valid payer browser before any provider call."""
    try:
        from storefront.utm_tracking import build_order_tracking_context
        from twocomms.middleware import _client_rate_limit_ip

        tracking = build_order_tracking_context(request, attempt) or {}
        trusted_ip = _client_rate_limit_ip(request)
        if trusted_ip:
            tracking["client_ip_address"] = trusted_ip
    except Exception:
        logger.warning(
            "Unable to freeze IG checkout tracking context for attempt %s",
            attempt.pk,
            exc_info=True,
        )
        tracking = {}
    tracking["external_id"] = tracking.get("external_id") or (
        f"user:{request.user.pk}"
        if request.user.is_authenticated
        else f"session:{request.session.session_key}"
    )
    tracking["add_payment_event_id"] = attempt.add_payment_event_id
    attempt.tracking_payload = tracking
    attempt.save(update_fields=["tracking_payload", "updated"])
    return tracking


def _send_add_payment_info_if_missing(attempt, request):
    """Persist the stable AddPaymentInfo intent without provider I/O in request."""
    attempt.refresh_from_db(fields=["event_state", "tracking_payload", "updated"])
    if (attempt.event_state or {}).get("fb_capi_add_payment_info"):
        return True
    from orders.payment_side_effects import enqueue_attempt_add_payment_info_side_effect

    enqueue_attempt_add_payment_info_side_effect(
        attempt.pk,
        source_url=request.build_absolute_uri(),
    )
    return True


def _persist_invoice_created_alert(locked, attempt):
    """Persist one idempotent manager alert for an invoice."""
    from management.services.ig_alerts import format_operator_alert
    from management.services.instagram_bot import notify_manager

    return notify_manager(
        format_operator_alert(
            "💳 IG: платіжне посилання створено",
            event_type="ig_checkout_invoice_created",
            client_id=locked.client_id,
            deal_id=locked.deal_id,
            proposal_id=locked.pk,
            attempt_id=attempt.pk,
            amount=attempt.payment_amount,
            status="invoice_created",
            instruction_code="ig_checkout_invoice_created",
        ),
        dedupe_key=f"ig-checkout-invoice-created:{attempt.pk}",
        event_type="ig_checkout_invoice_created",
        client=locked.client,
        deliver_immediately=False,
    )


def _enqueue_invoice_created_alert_best_effort(locked, attempt):
    """Keep a notification failure from changing the provider invoice result."""
    try:
        alert_persisted = _persist_invoice_created_alert(locked, attempt)
    except Exception:
        logger.warning(
            "Failed to persist IG invoice alert %s after provider success",
            attempt.pk,
            exc_info=True,
        )
    else:
        if not alert_persisted:
            logger.warning(
                "Failed to persist IG invoice alert %s after provider success",
                attempt.pk,
            )


def _lock_attempt_proposal_graph(attempt_id, *, proposal_related=()):
    """Lock an existing graph in canonical Deal -> Proposal -> Attempt order."""
    from management.models import (
        IgCheckoutInvoiceGeneration,
        IgCheckoutProposal,
        IgDeal,
    )

    generation_locator = (
        IgCheckoutInvoiceGeneration.objects.filter(payment_attempt_id=attempt_id)
        .values("pk", "proposal_id", "proposal__deal_id")
        .first()
    )
    if generation_locator is not None:
        deal = IgDeal.objects.select_for_update().get(
            pk=generation_locator["proposal__deal_id"]
        )
        proposal_query = IgCheckoutProposal.objects.select_for_update()
        safe_related = tuple(
            relation for relation in proposal_related if relation != "payment_attempt"
        )
        if safe_related:
            proposal_query = proposal_query.select_related(*safe_related)
        proposal = proposal_query.get(
            pk=generation_locator["proposal_id"],
            deal_id=deal.pk,
        )
        # The generation row is intentionally locked between Proposal and
        # PaymentAttempt even though the legacy return signature stays stable.
        generation = IgCheckoutInvoiceGeneration.objects.select_for_update().get(
            pk=generation_locator["pk"],
            proposal_id=proposal.pk,
        )
        attempt = PaymentAttempt.objects.select_for_update().get(pk=attempt_id)
        generation._state.fields_cache["payment_attempt"] = attempt
        proposal._state.fields_cache["payment_attempt"] = attempt
        return attempt, deal, proposal

    locator = (
        IgCheckoutProposal.objects.filter(payment_attempt_id=attempt_id)
        .values("pk", "deal_id")
        .first()
    )
    if locator is None:
        attempt = PaymentAttempt.objects.select_for_update().get(pk=attempt_id)
        return attempt, None, None
    deal = IgDeal.objects.select_for_update().get(pk=locator["deal_id"])
    proposal_query = IgCheckoutProposal.objects.select_for_update()
    safe_related = tuple(
        relation for relation in proposal_related if relation != "payment_attempt"
    )
    if safe_related:
        proposal_query = proposal_query.select_related(*safe_related)
    proposal = proposal_query.filter(
        pk=locator["pk"],
        deal_id=deal.pk,
        payment_attempt_id=attempt_id,
    ).first()
    attempt = PaymentAttempt.objects.select_for_update().get(pk=attempt_id)
    if proposal is not None:
        # Avoid a later implicit query through the OneToOne descriptor and make
        # every caller use the already locked attempt instance.
        proposal._state.fields_cache["payment_attempt"] = attempt
    return attempt, deal, proposal


@transaction.atomic
def lock_proposal_details(proposal, *, payload, request, grant_id=""):
    """Validate and persist first-submit-wins recipient data."""
    from management.models import IgCheckoutProposal

    from management.models import IgDeal

    deal = IgDeal.objects.select_for_update().get(pk=proposal.deal_id)
    locked = IgCheckoutProposal.objects.select_for_update().select_related(
        "client", "commercial_episode"
    ).get(pk=proposal.pk, deal_id=deal.pk)
    locked_attempt = None
    if locked.payment_attempt_id:
        locked_attempt = PaymentAttempt.objects.select_for_update().get(
            pk=locked.payment_attempt_id
        )
        locked._state.fields_cache["payment_attempt"] = locked_attempt
    from storefront.services.brigade_commerce import brigade_payment_error
    full_only, _eligible = _proposal_brigade_policy(locked)
    if full_only and (locked.pay_type != locked.PayType.ONLINE_FULL or (locked_attempt is not None and locked_attempt.pay_type != PaymentAttempt.PayType.ONLINE_FULL)):
        raise CheckoutPaymentError("brigade_full_payment_required", brigade_payment_error(), field="pay_type")
    now = timezone.now()
    if locked.expires_at <= now:
        raise CheckoutPaymentError("expired", "Срок действия предложения истек.")
    if locked.status in {
        locked.Status.PAID,
        locked.Status.INVOICE_CREATED,
        locked.Status.DETAILS_LOCKED,
    } and locked.payment_attempt_id:
        attempt = locked_attempt
        if (attempt.event_state or {}).get("invoice_creation_ambiguous"):
            raise CheckoutPaymentError(
                "provider_ambiguous",
                "Платіж уже передано банку, але його статус ще потрібно перевірити.",
            )
        if attempt.invoice_url:
            return locked, attempt, None, True
        if attempt.status in {PaymentAttempt.Status.INITIATED, PaymentAttempt.Status.PROCESSING}:
            if (
                attempt.status == PaymentAttempt.Status.PROCESSING
                and _invoice_creation_lease_expired(attempt.event_state)
            ):
                event_state = dict(attempt.event_state or {})
                event_state["invoice_creation_ambiguous"] = True
                event_state.pop("invoice_creation_lease", None)
                event_state.pop("invoice_creation_lease_expires_at", None)
                attempt.event_state = event_state
                attempt.error_reason = "invoice_creation_ambiguous:stale_lease"
                attempt.last_status_at = timezone.now()
                attempt.save(update_fields=[
                    "event_state", "error_reason", "last_status_at", "updated",
                ])
                return locked, attempt, _STALE_INVOICE_LEASE, True
            raise CheckoutPaymentError("in_progress", "Платеж уже создается. Подождите несколько секунд.")
    if locked.status not in {locked.Status.READY, locked.Status.VIEWED}:
        raise CheckoutPaymentError("unavailable", "Это предложение больше нельзя оплатить.")

    _revalidate_frozen_proposal(locked)
    values = _validate_payload(locked, payload, user=request.user)
    if not request.session.session_key:
        request.session.save()
    fingerprint = _fingerprint(
        locked,
        full_name=values["full_name"],
        phone=values["phone"],
        email=values["email"],
        delivery=values["delivery"],
        promo_code=values["promo_code"],
    )
    existing = PaymentAttempt.objects.select_for_update().filter(fingerprint=fingerprint).first()
    if existing:
        if existing.invoice_url:
            locked.payment_attempt = existing
            locked.status = locked.Status.INVOICE_CREATED
            locked.save(update_fields=["payment_attempt", "status", "updated_at"])
            return locked, existing, values, True
        raise CheckoutPaymentError("in_progress", "Платеж уже создается. Подождите несколько секунд.")

    attempt = PaymentAttempt.objects.create(
        fingerprint=fingerprint,
        user=request.user if request.user.is_authenticated else None,
        session_key=request.session.session_key,
        full_name=values["full_name"],
        phone=values["phone"],
        email=values["email"],
        city=values["delivery"].city,
        np_office=values["delivery"].np_office,
        np_settlement_ref=values["delivery"].settlement_ref,
        np_city_ref=values["delivery"].city_ref,
        np_warehouse_ref=values["delivery"].warehouse_ref,
        pay_type=(
            PaymentAttempt.PayType.PREPAYMENT
            if locked.pay_type == locked.PayType.PREPAYMENT
            else PaymentAttempt.PayType.ONLINE_FULL
        ),
        cart_snapshot=_snapshot(locked),
        gross_amount=locked.catalog_total,
        discount_amount=Decimal(locked.negotiated_discount or 0) + values["promo_discount"],
        payable_amount=values["order_payable"],
        payment_amount=values["payment_amount"],
        promo_code=values["promo"],
        event_state=values["promo_event_state"],
    )
    tracking = _capture_attempt_tracking(request, attempt)
    if grant_id:
        tracking["ig_checkout_grant_id"] = str(grant_id)[:64]
        tracking["ig_initiate_checkout_event_id"] = hashlib.sha256(
            f"ig:{proposal.pk}:{proposal.revision}:{grant_id}".encode()
        ).hexdigest()[:40]
        attempt.tracking_payload = tracking
        attempt.save(update_fields=["tracking_payload", "updated"])
    locked.payment_attempt = attempt
    locked.status = locked.Status.DETAILS_LOCKED
    locked.details_locked_at = now
    deal.np_full_name = values["full_name"]
    deal.np_phone = values["phone"]
    deal.np_city = values["delivery"].city
    deal.np_office = values["delivery"].np_office
    deal.np_settlement_ref = values["delivery"].settlement_ref
    deal.np_city_ref = values["delivery"].city_ref
    deal.np_warehouse_ref = values["delivery"].warehouse_ref
    deal.np_warehouse_kind = values["delivery"].warehouse_kind
    deal.delivery_status = deal.DeliveryStatus.VALIDATED
    deal.delivery_source = "instagram_checkout"
    deal.status = deal.Status.AWAITING_PAYMENT
    deal.payment_status = "unpaid"
    deal.save(update_fields=[
        "np_full_name", "np_phone", "np_city", "np_office", "np_settlement_ref",
        "np_city_ref", "np_warehouse_ref", "np_warehouse_kind", "delivery_status",
        "delivery_source", "status", "payment_status", "updated_at",
    ])
    locked.save(update_fields=["payment_attempt", "status", "details_locked_at", "updated_at"])
    reserve_proposal_inventory(locked, expires_at=min(
        locked.expires_at,
        now + timedelta(minutes=25),
    ))
    return locked, attempt, values, False


def create_or_reuse_invoice(proposal, *, request, payload, grant_id=""):
    """Create one standard Monobank invoice for a proposal."""
    from storefront.views.monobank import _monobank_api_request

    locked, attempt, values, reused = lock_proposal_details(
        proposal, payload=payload, request=request, grant_id=grant_id
    )
    if values is _STALE_INVOICE_LEASE:
        raise CheckoutPaymentError(
            "provider_ambiguous",
            "Платіж уже передано банку, але його статус ще потрібно перевірити.",
        )
    if reused and attempt.invoice_url:
        _send_add_payment_info_if_missing(attempt, request)
        _enqueue_invoice_created_alert_best_effort(locked, attempt)
        return attempt, attempt.invoice_url, True
    if values is None:
        raise CheckoutPaymentError("in_progress", "Платеж уже создается. Подождите несколько секунд.")

    # Claim invoice creation before the provider call.  A second browser sees
    # this durable lease and cannot create a second invoice for the same
    # proposal while the first request is in flight.
    lease = secrets.token_urlsafe(24)
    attempt.refresh_from_db()
    if attempt.invoice_url:
        _send_add_payment_info_if_missing(attempt, request)
        return attempt, attempt.invoice_url, True
    event_state = dict(attempt.event_state or {})
    if event_state.get("invoice_creation_ambiguous"):
        raise CheckoutPaymentError(
            "provider_ambiguous",
            "Платіж уже передано банку, але його статус ще потрібно перевірити.",
        )
    event_state["invoice_creation_lease"] = lease
    event_state["invoice_creation_lease_expires_at"] = (
        timezone.now() + timedelta(minutes=5)
    ).isoformat()
    updated = PaymentAttempt.objects.filter(
        pk=attempt.pk,
        status=PaymentAttempt.Status.INITIATED,
        invoice_url="",
    ).update(
        status=PaymentAttempt.Status.PROCESSING,
        event_state=event_state,
        last_status_at=timezone.now(),
    )
    if not updated:
        attempt.refresh_from_db()
        if attempt.invoice_url:
            return attempt, attempt.invoice_url, True
        raise CheckoutPaymentError(
            "in_progress",
            "Платіж уже створюється. Зачекайте кілька секунд.",
        )

    invoice_payload = _invoice_payload(
        request,
        attempt,
        locked,
        payment_amount=values["payment_amount"],
        promo_discount=values["promo_discount"],
    )
    try:
        creation = _monobank_api_request(
            "POST", "/api/merchant/invoice/create", json_payload=invoice_payload
        )
        result = creation.get("result") if isinstance(creation.get("result"), dict) else creation
        invoice_id = result.get("invoiceId") or result.get("invoice_id")
        invoice_url = result.get("pageUrl") or result.get("invoiceUrl")
        if not invoice_id or not invoice_url:
            raise RuntimeError("Monobank returned an invalid invoice")
    except Exception as exc:
        if not bool(getattr(exc, "ambiguous", True)):
            failed_at = timezone.now()
            PaymentAttempt.objects.filter(pk=attempt.pk).update(
                status=PaymentAttempt.Status.FAILED,
                error_reason=f"invoice_creation_failed:{exc}"[:500],
                last_status_at=failed_at,
            )
            from management.models import IgCheckoutProposal

            with transaction.atomic():
                reset = IgCheckoutProposal.objects.select_for_update().get(pk=locked.pk)
                if reset.payment_attempt_id == attempt.pk and not attempt.invoice_url:
                    reset.status = IgCheckoutProposal.Status.READY
                    reset.payment_attempt = None
                    reset.details_locked_at = None
                    reset.save(update_fields=[
                        "status", "payment_attempt", "details_locked_at", "updated_at",
                    ])
                    release_proposal_inventory(reset, reason="invoice_creation_failed")
                    release_attempt_promo(attempt, reason="invoice_creation_failed")
            raise CheckoutPaymentError(
                "provider_error",
                "Не вдалося створити платіж. Спробуйте ще раз пізніше.",
            ) from exc
        event_state["invoice_creation_ambiguous"] = True
        event_state.pop("invoice_creation_lease", None)
        event_state.pop("invoice_creation_lease_expires_at", None)
        PaymentAttempt.objects.filter(pk=attempt.pk).update(
            status=PaymentAttempt.Status.PROCESSING,
            event_state=event_state,
            error_reason=f"invoice_creation_ambiguous:{exc}"[:500],
            last_status_at=timezone.now(),
        )
        # Keep the recipient lock and reservation.  A provider timeout can
        # mean that an invoice exists even when the response was lost.
        raise CheckoutPaymentError(
            "provider_ambiguous",
            "Не вдалося підтвердити відповідь банку. Не повторюйте оплату: ми перевіримо рахунок.",
        ) from exc

    tracking = dict(attempt.tracking_payload or {})
    now = timezone.now()
    event_state.pop("invoice_creation_lease", None)
    event_state.pop("invoice_creation_lease_expires_at", None)
    event_state.pop("invoice_creation_ambiguous", None)
    with transaction.atomic():
        PaymentAttempt.objects.filter(pk=attempt.pk).update(
            monobank_invoice_id=str(invoice_id)[:128],
            invoice_url=str(invoice_url)[:600],
            invoice_payload={"request": invoice_payload, "create": creation},
            tracking_payload=tracking,
            invoice_expires_at=min(locked.expires_at, now + timedelta(minutes=25)),
            event_state=event_state,
            status=PaymentAttempt.Status.PROCESSING,
            last_status_at=now,
        )
        _send_add_payment_info_if_missing(attempt, request)
    # The provider invoice and payment side-effect intent are durable before
    # the best-effort, idempotent manager notification is attempted.
    _enqueue_invoice_created_alert_best_effort(locked, attempt)
    from management.models import IgCheckoutProposal
    locked.refresh_from_db()
    locked.status = IgCheckoutProposal.Status.INVOICE_CREATED
    locked.save(update_fields=["status", "updated_at"])
    try:
        from management.models import IgClient

        locked.client.set_stage(
            IgClient.Stage.PAYMENT_PENDING,
            reason="checkout_invoice_created",
        )
    except Exception:
        logger.warning(
            "Failed to advance IG client after invoice creation for proposal %s",
            locked.pk,
            exc_info=True,
        )
    request.session["monobank_invoice_id"] = str(invoice_id)
    request.session["monobank_pending_attempt_id"] = attempt.pk
    request.session["monobank_attempt_id"] = attempt.pk
    request.session["ig_checkout_proposal_id"] = str(locked.public_id)
    request.session.modified = True

    attempt.refresh_from_db()
    return attempt, attempt.invoice_url, False


@transaction.atomic
def project_terminal_payment(attempt_id, *, status, payload=None, source="provider_pull"):
    """Project trusted non-payable provider truth into the Instagram ledger."""
    from management.models import (
        IgCheckoutProposal,
        IgDeal,
        IgPaymentEvent,
        IgPaymentProjection,
        provider_evidence_signature,
    )

    normalized_status = str(status or "").strip().lower()
    truth = {
        "cancelled": IgDeal.PaymentTruth.CANCELLED,
        "canceled": IgDeal.PaymentTruth.CANCELLED,
        "expired": IgDeal.PaymentTruth.CANCELLED,
        "failure": IgDeal.PaymentTruth.FAILED,
        "rejected": IgDeal.PaymentTruth.FAILED,
    }.get(normalized_status)
    canonical_source = {
        "provider": "provider",
        "provider_pull": "provider_pull",
        "provider_webhook": "provider_webhook",
        "signed_webhook": "signed_webhook",
        "webhook": "provider_webhook",
        "return": "provider_pull",
        "ig_reconcile": "provider_pull",
        "poll": "provider_pull",
    }.get(str(source or "").strip().lower())
    if truth is None or canonical_source is None:
        return None

    attempt, deal, proposal = _lock_attempt_proposal_graph(
        attempt_id,
        proposal_related=("client", "payment_attempt"),
    )
    if proposal is None:
        return None
    if attempt.order_id or deal.order_id:
        return None

    raw_payload = payload if isinstance(payload, dict) else {}
    evidence = {
        "attempt_id": attempt.pk,
        "attempt_reference": attempt.reference,
        "invoice_id": attempt.monobank_invoice_id,
        "status": normalized_status,
        "ccy": raw_payload.get("ccy") or raw_payload.get("currency"),
        "amount": raw_payload.get("paidAmount", raw_payload.get("finalAmount", raw_payload.get("amount"))),
    }
    payload_digest = hashlib.sha256(
        json.dumps(evidence, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()
    evidence["signature"] = provider_evidence_signature(
        deal_id=deal.pk,
        client_id=proposal.client_id,
        provider="monobank",
        source=canonical_source,
        invoice_id=attempt.monobank_invoice_id,
        provider_status=normalized_status,
        payload_digest=payload_digest,
    )
    event, _created = IgPaymentEvent.objects.get_or_create(
        event_key=f"attempt:{attempt.pk}:terminal:{normalized_status}"[:64],
        defaults={
            "deal": deal,
            "client": proposal.client,
            "provider": "monobank",
            "source": canonical_source,
            "invoice_id": str(attempt.monobank_invoice_id or "")[:128],
            "provider_status": normalized_status,
            "provider_modified_at": attempt.last_status_at or timezone.now(),
            "gross_amount": attempt.payment_amount,
            "final_amount": Decimal("0.00"),
            "refunded_amount": Decimal("0.00"),
            "amount_valid": None,
            "currency": proposal.currency or "UAH",
            "evidence": evidence,
            "payload_digest": payload_digest,
        },
    )
    projection, _created = IgPaymentProjection.objects.select_for_update().get_or_create(
        deal=deal,
        defaults={"client": proposal.client},
    )
    projection.client = proposal.client
    projection.truth = truth
    projection.gross_amount = Decimal("0.00")
    projection.refunded_amount = Decimal("0.00")
    projection.paid_at = None
    projection.provider_modified_at = attempt.last_status_at or timezone.now()
    projection.last_event = event
    projection.needs_reconciliation = False
    projection.reconciled_at = timezone.now()
    projection.save()

    deal.payment_truth = truth
    deal.payment_status = "unpaid"
    deal.paid_amount = Decimal("0.00")
    deal.paid_at = None
    deal.payment_truth_updated_at = timezone.now()
    deal.save(update_fields=[
        "payment_truth", "payment_status", "paid_amount", "paid_at",
        "payment_truth_updated_at", "updated_at",
    ])
    proposal.status = IgCheckoutProposal.Status.CANCELLED
    proposal.invoice_cancelled_at = proposal.invoice_cancelled_at or timezone.now()
    proposal.provider_cancellation_event = event
    proposal.save(update_fields=[
        "status", "invoice_cancelled_at", "provider_cancellation_event", "updated_at",
    ])
    return event


def _paid_amount_from_provider_payload(attempt, payload):
    raw = None
    if isinstance(payload, dict):
        raw = payload.get("paidAmount")
        if raw is None:
            raw = payload.get("finalAmount")
        if raw is None:
            raw = payload.get("amount")
    if raw is None:
        return Decimal(attempt.payment_amount or 0).quantize(Decimal("0.01"))
    try:
        return (Decimal(str(raw)) / Decimal("100")).quantize(Decimal("0.01"))
    except (ArithmeticError, TypeError, ValueError):
        return Decimal(attempt.payment_amount or 0).quantize(Decimal("0.01"))


@transaction.atomic
def record_late_local_payment_for_review(
    attempt_id,
    *,
    payload=None,
    source="provider_pull",
):
    """Persist late provider money without materializing a duplicate Order.

    A locally expired/cancelled invoice may overlap a newly issued generation.
    Until the invoice-series winner schema exists, every such success fails
    closed to an explicit manager review. Provider truth is still recorded.
    """

    from management.models import (
        IgFollowUpTask,
        IgPaymentEvent,
        IgPaymentProjection,
        provider_evidence_signature,
    )

    attempt, deal, proposal = _lock_attempt_proposal_graph(
        attempt_id,
        proposal_related=("client", "commercial_episode"),
    )
    if proposal is None or deal is None or attempt.order_id:
        return False
    local_terminal = dict(
        (attempt.event_state or {}).get("local_terminalization") or {}
    )
    if not local_terminal:
        return False

    now = timezone.now()
    paid_amount = _paid_amount_from_provider_payload(attempt, payload)
    expected = Decimal(attempt.payment_amount or 0).quantize(Decimal("0.01"))
    amount_valid = paid_amount == expected and paid_amount > 0
    evidence_payload = {
        "attempt_id": attempt.pk,
        "attempt_reference": attempt.reference,
        "invoice_id": attempt.monobank_invoice_id,
        "status": "success",
        "amount": str(paid_amount),
        "expected_amount": str(expected),
        "local_terminal_event_key": str(local_terminal.get("event_key") or "")[:180],
        "late_conflict_review": True,
    }
    payload_digest = hashlib.sha256(
        json.dumps(
            evidence_payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    signature = provider_evidence_signature(
        deal_id=deal.pk,
        client_id=deal.client_id,
        provider="monobank",
        source="provider_attempt",
        invoice_id=attempt.monobank_invoice_id,
        provider_status="success",
        payload_digest=payload_digest,
    )
    evidence_payload["signature"] = signature
    payment_event, _created = IgPaymentEvent.objects.get_or_create(
        event_key=f"attempt:{attempt.pk}:verified",
        defaults={
            "deal": deal,
            "client": deal.client,
            "provider": "monobank",
            "source": "provider_attempt",
            "invoice_id": attempt.monobank_invoice_id[:128],
            "provider_status": "success",
            "provider_modified_at": now,
            "gross_amount": paid_amount,
            "final_amount": paid_amount,
            "refunded_amount": Decimal("0.00"),
            "amount_valid": amount_valid,
            "currency": proposal.currency or "UAH",
            "evidence": evidence_payload,
            "payload_digest": payload_digest,
        },
    )
    projection, _created = IgPaymentProjection.objects.select_for_update().get_or_create(
        deal=deal,
        defaults={"client": deal.client},
    )
    projection.client = deal.client
    projection.truth = deal.PaymentTruth.CONFIRMED
    projection.gross_amount = paid_amount
    projection.refunded_amount = Decimal("0.00")
    projection.paid_at = projection.paid_at or now
    projection.provider_modified_at = now
    projection.last_event = payment_event
    projection.needs_reconciliation = True
    projection.reconciled_at = None
    projection.save()

    history = list(attempt.payment_history or [])
    history.append(
        {
            "ts": now.isoformat(),
            "status": "success",
            "source": str(source or "provider_pull")[:32],
            "payload": {
                "invoiceId": attempt.monobank_invoice_id,
                "paidAmount": str(paid_amount),
                "late_conflict_review": True,
            },
        }
    )
    attempt.payment_history = history[-30:]
    attempt_event_state = dict(attempt.event_state or {})
    recheck_claimed = bool(
        attempt.provider_recheck_state
        == PaymentAttempt.ProviderRecheckState.CHECKING
        and attempt.provider_recheck_claim_token
    )
    provider_update_fields = []
    if not recheck_claimed:
        local_terminal["provider_check_state"] = "resolved"
        local_terminal["provider_last_status"] = "success"
        local_terminal["provider_last_check_at"] = now.isoformat()
        local_terminal["provider_next_check_at"] = None
        attempt_event_state["local_terminalization"] = local_terminal
        local_history = list(
            attempt_event_state.get("local_terminalization_events") or []
        )
        if (
            local_history
            and local_history[-1].get("event_key") == local_terminal.get("event_key")
        ):
            local_history[-1] = local_terminal
            attempt_event_state["local_terminalization_events"] = local_history[-8:]
        attempt.provider_recheck_state = PaymentAttempt.ProviderRecheckState.RESOLVED
        attempt.provider_recheck_next_at = None
        attempt.provider_recheck_claim_token = ""
        attempt.provider_recheck_claim_until = None
        attempt.provider_recheck_last_status = "success"
        provider_update_fields = [
            "provider_recheck_state", "provider_recheck_next_at",
            "provider_recheck_claim_token", "provider_recheck_claim_until",
            "provider_recheck_last_status",
        ]
    attempt.event_state = attempt_event_state
    attempt.status = (
        PaymentAttempt.Status.PREPAID
        if attempt.pay_type in {
            PaymentAttempt.PayType.PREPAYMENT,
            PaymentAttempt.PayType.PREPAY_200,
        }
        else PaymentAttempt.Status.PAID
    )
    attempt.paid_amount = paid_amount
    attempt.last_status_at = now
    attempt.error_reason = "late_local_terminal_payment_review"
    attempt.save(
        update_fields=[
            "status", "paid_amount", "last_status_at", "error_reason",
            "payment_history", "event_state", "updated",
            *provider_update_fields,
        ]
    )
    deal.status = deal.Status.PAID
    deal.payment_status = (
        "prepaid"
        if attempt.pay_type in {
            PaymentAttempt.PayType.PREPAYMENT,
            PaymentAttempt.PayType.PREPAY_200,
        }
        else "paid"
    )
    deal.payment_truth = deal.PaymentTruth.CONFIRMED
    deal.paid_amount = paid_amount
    deal.paid_at = deal.paid_at or now
    deal.payment_truth_updated_at = now
    deal.save(
        update_fields=[
            "status", "payment_status", "payment_truth", "paid_amount",
            "paid_at", "payment_truth_updated_at", "updated_at",
        ]
    )
    proposal.status = proposal.Status.MANAGER_REVIEW
    proposal.paid_at = proposal.paid_at or now
    proposal.save(update_fields=["status", "paid_at", "updated_at"])
    mark_overbooked_proposal_inventory(
        proposal,
        paid_at=now,
        reason="late_local_payment_review",
    )
    IgFollowUpTask.objects.get_or_create(
        event_key=f"late-local-payment-review:{attempt.pk}",
        defaults={
            "client": proposal.client,
            "deal": deal,
            "due_at": now,
            "status": IgFollowUpTask.Status.SKIPPED,
            "kind": IgFollowUpTask.Kind.MANAGER_TASK,
            "reason": "late_local_payment_review",
            "trigger": IgFollowUpTask.Trigger.EVENT,
            "event_occurred_at": now,
            "event_payload": {
                "proposal_id": proposal.pk,
                "attempt_id": attempt.pk,
                "payment_event_id": payment_event.pk,
                "amount_valid": amount_valid,
            },
            "skip_reason": "human_agent_required",
            "message_text": (
                "Оплата надійшла за локально закритим IG invoice. "
                "Перевірте новішу пропозицію, склад, промокод або повернення."
            ),
        },
    )
    return True


def _explicit_provider_time(payload):
    try:
        parsed = parse_datetime(str(payload.get("modifiedDate") or "")) if isinstance(payload, dict) else None
    except (TypeError, ValueError):
        return None
    return parsed.astimezone(datetime_timezone.utc) if parsed is not None and timezone.is_aware(parsed) else None


def _attempt_success_provider_time(attempt):
    for observation in reversed(list(attempt.payment_history or [])):
        if isinstance(observation, dict) and observation.get("status") == "success":
            return _explicit_provider_time(observation.get("payload"))
    return None


def _settlement_minor(value):
    # Same exact minor-unit forms admitted by the existing hosted success gate.
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    if isinstance(value, str) and value.isascii() and value.isdecimal() and len(value) <= 18 and (value == "0" or not value.startswith("0")):
        return int(value)
    return None


def _provider_aliases_match(attempt, payload, *, status, require_complete=True):
    """Every supplied provider alias must attest the same exact receipt."""
    if not isinstance(payload, dict):
        return False
    merchant = payload.get("merchantPaymInfo")
    if merchant is not None and not isinstance(merchant, dict):
        return False
    merchant = merchant or {}
    invoices = [payload[key] for key in ("invoiceId", "invoice_id") if key in payload]
    references = [payload["reference"]] if "reference" in payload else []
    if "reference" in merchant:
        references.append(merchant["reference"])
    currencies = [payload[key] for key in ("ccy", "currencyCode") if key in payload]
    statuses = [payload[key] for key in ("status", "statusCode") if key in payload]
    if require_complete and not (attempt.monobank_invoice_id and attempt.reference and invoices
            and references and currencies and statuses):
        return False
    return bool(all(str(value) == str(attempt.monobank_invoice_id) for value in invoices)
        and all(str(value) == str(attempt.reference) for value in references)
        and all(str(value) in {"980", "UAH"} for value in currencies)
        and all(str(value).lower() == status for value in statuses))


def _initial_hosted_payment_reason(attempt, payload, *, status="success"):
    if not _provider_aliases_match(attempt, payload, status=status, require_complete=False):
        return "identity_currency_mismatch"
    values = [_settlement_minor(payload[key]) for key in ("paidAmount", "finalAmount", "amount")
        if payload.get(key) is not None]
    if not values:
        return "amount_missing"
    if any(value is None for value in values):
        return "amount_malformed"
    if len(set(values)) != 1:
        return "amount_conflict"
    return "" if values[0] == int(Decimal(str(attempt.payment_amount)) * 100) else "amount_mismatch"


def normalize_attempt_settlement(attempt, *, status, payload, source):
    """Validate a post-payment observation using existing provider fields.

    The initial invoice/winner amount gate is unchanged. Missing financial or
    provider-version evidence never manufactures a refund or cancellation.
    """
    canonical_source = {"webhook": "provider_webhook", "signed_webhook": "signed_webhook",
        "provider_webhook": "provider_webhook", "return": "provider_pull", "ig_reconcile": "provider_pull",
        "provider_pull": "provider_pull", "provider": "provider", "poll": "provider_pull"}.get(source)
    normalized = str(status or "").lower()
    if canonical_source is None or normalized not in {"success", "reversed"}:
        return {"reason": "settlement_source_or_status_unknown"}
    if not isinstance(payload, dict):
        return {"reason": "settlement_payload_missing"}
    if not _provider_aliases_match(attempt, payload, status=normalized):
        return {"reason": "settlement_identity_currency_mismatch"}
    invoice, reference = str(attempt.monobank_invoice_id), str(attempt.reference)
    provider_time = _explicit_provider_time(payload)
    if provider_time is None:
        return {"reason": "settlement_provider_version_unknown"}
    gross, final = _settlement_minor(payload.get("amount")), _settlement_minor(payload.get("finalAmount"))
    expected = int(Decimal(str(attempt.payment_amount)) * 100)
    if gross is None or final is None:
        return {"reason": "settlement_amount_missing_or_malformed"}
    if gross != expected or gross <= 0 or not 0 <= final <= gross:
        return {"reason": "settlement_amount_mismatch"}
    if payload.get("paidAmount") is not None and _settlement_minor(payload["paidAmount"]) != gross:
        return {"reason": "settlement_amount_conflict"}
    if normalized == "reversed" and final != 0:
        return {"reason": "settlement_reversal_amount_unknown"}
    evidence = {"attempt_id": attempt.pk, "attempt_reference": reference, "invoice_id": invoice,
        "status": normalized, "ccy": "980", "amount": gross, "finalAmount": final,
        "modifiedDate": provider_time.isoformat()}
    return {"reason": "", "source": canonical_source, "provider_time": provider_time,
        "gross": Decimal(gross) / 100, "final": Decimal(final) / 100, "evidence": evidence}


def _settlement_review(attempt, deal, reason):
    from management.models import IgBotNotification
    marker = {"reason": reason, "attempt_id": attempt.pk, "invoice_id": attempt.monobank_invoice_id}
    state = dict(attempt.event_state or {})
    state["payment_settlement_review"] = marker
    attempt.event_state = state
    attempt.save(update_fields=["event_state", "updated"])
    IgBotNotification.objects.get_or_create(
        dedupe_key=f"hosted-settlement:{attempt.pk}:{reason}", defaults={
            "client_id": deal.client_id, "event_type": "hosted_settlement_review",
            "payload": {"text": f"IG payment requires review: attempt {attempt.pk}; {reason}.", "chat_id": "", **marker},
            "status": IgBotNotification.Status.PENDING})


def _repair_hosted_settlement(projection_id):
    try:
        from management.services.bot_payments import reconcile_payment_projection
        reconcile_payment_projection(projection_id)
    except Exception:
        # The committed needs_reconciliation flag remains the durable backstop.
        logger.exception("Unable to repair hosted settlement projection %s", projection_id)


def _owned_settlement_projection(projection, attempt, deal):
    from secrets import compare_digest
    from management.models import provider_evidence_signature
    event = projection.last_event
    if (event is None or event.deal_id != deal.pk or event.client_id != deal.client_id
            or event.provider != "monobank" or event.invoice_id != attempt.monobank_invoice_id
            or event.currency != "UAH" or event.amount_valid is not True):
        return False
    expected = provider_evidence_signature(deal_id=deal.pk, client_id=deal.client_id,
        provider=event.provider, source=event.source, invoice_id=event.invoice_id,
        provider_status=event.provider_status, payload_digest=event.payload_digest)
    return compare_digest(str((event.evidence or {}).get("signature") or ""), expected)


@transaction.atomic
def project_attempt_settlement(attempt_id, *, status, payload, source):
    """Project one proved hosted settlement; reuse the existing payment ledger."""
    from management.models import IgPaymentEvent, IgPaymentProjection, IgCheckoutInvoiceGeneration, provider_evidence_signature
    attempt, deal, proposal = _lock_attempt_proposal_graph(attempt_id)
    if deal is None or proposal is None:
        return {"handled": False, "applied": False, "reason": "not_hosted_checkout"}
    if proposal.client_id != deal.client_id or str(proposal.currency or "UAH") != "UAH" or str(deal.currency or "UAH") != "UAH":
        _settlement_review(attempt, deal, "settlement_owner_currency_mismatch")
        return {"handled": True, "applied": False, "reason": "settlement_owner_currency_mismatch"}
    observation = normalize_attempt_settlement(attempt, status=status, payload=payload, source=source)
    if observation["reason"]:
        _settlement_review(attempt, deal, observation["reason"])
        return {"handled": True, "applied": False, "reason": observation["reason"]}
    evidence = observation["evidence"]
    projection = IgPaymentProjection.objects.select_for_update().filter(deal=deal, client_id=deal.client_id).first()
    if (projection is not None and projection.last_event_id
            and _owned_settlement_projection(projection, attempt, deal)
            and proposal.payment_attempt_id == attempt.pk
            and _explicit_provider_time(projection.last_event.evidence) == observation["provider_time"]
            and projection.provider_modified_at == observation["provider_time"]
            and projection.last_event.provider_status == evidence["status"]
            and projection.gross_amount == observation["gross"] and projection.net_paid_amount == observation["final"]):
        return {"handled": True, "applied": False, "reason": "settlement_receipt_replay", "event_id": projection.last_event_id}
    digest = hashlib.sha256(json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    evidence = {**evidence, "signature": provider_evidence_signature(deal_id=deal.pk, client_id=deal.client_id,
        provider="monobank", source=observation["source"], invoice_id=attempt.monobank_invoice_id,
        provider_status=evidence["status"], payload_digest=digest)}
    event, _created = IgPaymentEvent.objects.get_or_create(event_key=hashlib.sha256(
        f"hosted-settlement:{attempt.pk}:{digest}".encode()).hexdigest(), defaults={
            "deal": deal, "client_id": deal.client_id, "provider": "monobank", "source": observation["source"],
            "invoice_id": attempt.monobank_invoice_id, "provider_status": evidence["status"],
            "provider_modified_at": observation["provider_time"], "gross_amount": observation["gross"],
            "final_amount": observation["final"], "refunded_amount": observation["gross"] - observation["final"],
            "amount_valid": True, "currency": "UAH", "evidence": evidence, "payload_digest": digest})
    winner = (IgCheckoutInvoiceGeneration.objects.filter(pk=proposal.winner_invoice_generation_id)
        .values_list("payment_attempt_id", flat=True).first()) if proposal.winner_invoice_generation_id else None
    if (winner is not None and winner != attempt.pk) or (projection is not None and projection.last_event_id
            and projection.last_event.invoice_id != attempt.monobank_invoice_id):
        reason = "settlement_non_winner_receipt_review"
    elif projection is None or not projection.last_event_id:
        reason = "settlement_paid_receipt_binding_unknown"
    elif proposal.payment_attempt_id != attempt.pk or deal.order_id != attempt.order_id:
        reason = "settlement_order_binding_mismatch"
    elif not _owned_settlement_projection(projection, attempt, deal):
        reason = "settlement_prior_receipt_unknown"
    elif projection.gross_amount != observation["gross"]:
        reason = "settlement_prior_amount_conflict"
    elif (not projection.provider_modified_at or not _explicit_provider_time(projection.last_event.evidence)
            or _explicit_provider_time(projection.last_event.evidence) != projection.provider_modified_at):
        reason = "settlement_prior_version_unknown"
    elif observation["provider_time"] < projection.provider_modified_at:
        return {"handled": True, "applied": False, "reason": "settlement_stale_provider_version", "event_id": event.pk}
    elif observation["provider_time"] == projection.provider_modified_at:
        if projection.gross_amount == observation["gross"] and projection.net_paid_amount == observation["final"]:
            return {"handled": True, "applied": False, "reason": "settlement_receipt_replay", "event_id": event.pk}
        reason = "settlement_provider_version_conflict"
    elif observation["final"] > projection.net_paid_amount:
        reason = "settlement_net_increase_review"
    else:
        reason = ""
    if reason:
        _settlement_review(attempt, deal, reason)
        return {"handled": True, "applied": False, "reason": reason, "event_id": event.pk}
    projection.truth = (deal.PaymentTruth.REVERSED if evidence["status"] == "reversed"
        else deal.PaymentTruth.REFUNDED if observation["final"] == 0
        else deal.PaymentTruth.PARTIALLY_REFUNDED if observation["final"] < observation["gross"]
        else deal.PaymentTruth.CONFIRMED)
    projection.gross_amount = observation["gross"]
    projection.refunded_amount = observation["gross"] - observation["final"]
    projection.provider_modified_at = observation["provider_time"]
    projection.last_event = event
    projection.needs_reconciliation = True
    projection.reconciled_at = None
    projection.save()
    if projection.truth in {deal.PaymentTruth.REVERSED, deal.PaymentTruth.REFUNDED}:
        from management.services.bot_payments import _reconcile_reversed_order, _ensure_reversal_review_outbox
        _reconcile_reversed_order(deal, truth=projection.truth)
        _ensure_reversal_review_outbox(deal, projection.truth)
    elif projection.truth == deal.PaymentTruth.PARTIALLY_REFUNDED:
        _settlement_review(attempt, deal, "settlement_partial_refund_review")
    transaction.on_commit(lambda projection_id=projection.pk: _repair_hosted_settlement(projection_id))
    return {"handled": True, "applied": True, "reason": "settlement_projected", "event_id": event.pk, "projection_id": projection.pk}


@transaction.atomic
def project_verified_payment_without_order(
    *,
    attempt,
    deal,
    proposal,
    verified_at=None,
):
    """Append canonical signed payment truth without authorizing an Order."""
    from management.models import (
        IgPaymentEvent,
        IgPaymentProjection,
        provider_evidence_signature,
    )
    current_attempt, current_deal, current_proposal = _lock_attempt_proposal_graph(attempt.pk)
    if (current_deal is None or current_proposal is None or current_deal.pk != deal.pk
            or current_proposal.pk != proposal.pk or current_deal.client_id != deal.client_id
            or current_proposal.client_id != current_deal.client_id):
        raise ValueError("hosted_payment_scope_changed")
    attempt, deal, proposal = current_attempt, current_deal, current_proposal
    now = timezone.now()
    provider_time = _attempt_success_provider_time(attempt)
    verified_at = verified_at or _verified_payment_at(attempt, fallback=now)
    paid_amount = Decimal(attempt.paid_amount or attempt.payment_amount or 0).quantize(Decimal("0.01"))
    evidence_payload = {
        "attempt_id": attempt.pk,
        "attempt_reference": attempt.reference,
        "invoice_id": attempt.monobank_invoice_id,
        "status": "success",
        "amount": str(paid_amount),
        "modifiedDate": provider_time.isoformat() if provider_time else "",
    }
    payload_digest = hashlib.sha256(
        json.dumps(evidence_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    source = "provider_attempt"
    signature = provider_evidence_signature(
        deal_id=deal.pk,
        client_id=deal.client_id,
        provider="monobank",
        source=source,
        invoice_id=attempt.monobank_invoice_id,
        provider_status="success",
        payload_digest=payload_digest,
    )
    evidence_payload["signature"] = signature
    payment_event, _ = IgPaymentEvent.objects.get_or_create(
        event_key=f"attempt:{attempt.pk}:verified",
        defaults={
            "deal": deal,
            "client": deal.client,
            "provider": "monobank",
            "source": source,
            "invoice_id": attempt.monobank_invoice_id[:128],
            "provider_status": "success",
            "provider_modified_at": provider_time or verified_at,
            "gross_amount": paid_amount,
            "final_amount": paid_amount,
            "refunded_amount": Decimal("0.00"),
            "amount_valid": True,
            "currency": proposal.currency or "UAH",
            "evidence": evidence_payload,
            "payload_digest": payload_digest,
        },
    )
    projection, projection_created = IgPaymentProjection.objects.select_for_update().get_or_create(
        deal=deal,
        defaults={"client": deal.client},
    )
    # A late losing receipt remains append-only evidence; it does not become
    # the winner order's payment provenance. Duplicate binding also cannot
    # erase an admitted refund/reversal or a later provider version.
    winner = proposal.winner_invoice_generation_id
    if winner:
        from management.models import IgCheckoutInvoiceGeneration
        winner_attempt = IgCheckoutInvoiceGeneration.objects.filter(pk=winner).values_list("payment_attempt_id", flat=True).first()
        if winner_attempt != attempt.pk:
            return payment_event, projection
    if not projection_created and projection.last_event_id:
        if (projection.truth in {deal.PaymentTruth.REFUNDED, deal.PaymentTruth.REVERSED}
                or projection.refunded_amount > 0
                or (projection.provider_modified_at and verified_at < projection.provider_modified_at)
                or projection.last_event_id == payment_event.pk):
            return payment_event, projection
    projection.client = deal.client
    projection.truth = deal.PaymentTruth.CONFIRMED
    projection.gross_amount = paid_amount
    projection.refunded_amount = Decimal("0.00")
    projection.paid_at = projection.paid_at or deal.paid_at or verified_at
    projection.provider_modified_at = provider_time or verified_at
    projection.last_event = payment_event
    projection.needs_reconciliation = False
    projection.reconciled_at = now
    projection.save()
    return payment_event, projection


@transaction.atomic
def bind_verified_payment(attempt_id, order):
    """Bind a verified PaymentAttempt/Order to proposal and Instagram truth."""
    from management.models import (
        IgCheckoutProposal,
        IgDeal,
        IgLifecycleEvent,
        IgClient,
    )
    from management.services.ig_order_links import create_order_attribution

    attempt, deal, proposal = _lock_attempt_proposal_graph(
        attempt_id,
        proposal_related=("client", "commercial_episode", "payment_attempt"),
    )
    if proposal is None:
        return None
    now = timezone.now()
    verified_at = _verified_payment_at(attempt, fallback=now)
    _payment_event, _projection = project_verified_payment_without_order(
        attempt=attempt,
        deal=deal,
        proposal=proposal,
        verified_at=verified_at,
    )
    if (_projection.truth != deal.PaymentTruth.CONFIRMED or _projection.refunded_amount > 0
            or not _projection.last_event_id or _projection.last_event.invoice_id != attempt.monobank_invoice_id):
        return None

    try:
        generation = attempt.instagram_checkout_generation
    except Exception:
        generation = None

    # A released warehouse reservation cannot be silently converted into an
    # order just because the provider reported success late.  Keep provider
    # truth, mark the allocation for human review, and leave the generic Order
    # unattached to this Instagram deal until stock/refund handling is decided.
    if generation is None and mark_overbooked_proposal_inventory(
        proposal,
        order=order,
        paid_at=verified_at,
    ):
        if attempt.status != PaymentAttempt.Status.CONVERTED:
            attempt.status = PaymentAttempt.Status.CONVERTED
            attempt.save(update_fields=["status", "updated"])
        deal.status = IgDeal.Status.PAID
        deal.payment_status = (
            "prepaid"
            if attempt.pay_type in {
                PaymentAttempt.PayType.PREPAYMENT,
                PaymentAttempt.PayType.PREPAY_200,
            }
            else "paid"
        )
        deal.payment_truth = IgDeal.PaymentTruth.CONFIRMED
        deal.paid_amount = attempt.paid_amount or attempt.payment_amount
        deal.paid_at = deal.paid_at or verified_at
        deal.payment_truth_updated_at = now
        deal.save(update_fields=[
            "status", "payment_status", "payment_truth", "paid_amount", "paid_at",
            "payment_truth_updated_at", "updated_at",
        ])
        proposal.status = proposal.Status.MANAGER_REVIEW
        proposal.paid_at = proposal.paid_at or verified_at
        proposal.save(update_fields=["status", "paid_at", "updated_at"])
        return None

    attribution = create_order_attribution(
        order,
        client=proposal.client,
        deal=deal,
        creation_mode="provider_auto",
        payment_source="provider_attempt",
    )
    # ``IgCheckoutProposal.save`` requires a converted attempt before it will
    # accept the paid state.  The Order is already durably materialized by the
    # provider adapter, so this is the final conversion marker rather than a
    # second payment transition.
    if attempt.status != PaymentAttempt.Status.CONVERTED:
        attempt.status = PaymentAttempt.Status.CONVERTED
        attempt.save(update_fields=["status", "updated"])
    deal.order_id = order.pk
    deal.status = deal.Status.ORDER_CREATED
    deal.payment_status = (
        "prepaid"
        if attempt.pay_type in {
            PaymentAttempt.PayType.PREPAYMENT,
            PaymentAttempt.PayType.PREPAY_200,
        }
        else "paid"
    )
    deal.payment_truth = deal.PaymentTruth.CONFIRMED
    deal.paid_amount = attempt.paid_amount or attempt.payment_amount
    deal.paid_at = deal.paid_at or verified_at
    deal.payment_truth_updated_at = now
    deal.save(update_fields=[
        "order", "status", "payment_status", "payment_truth", "paid_amount", "paid_at",
        "payment_truth_updated_at", "updated_at",
    ])
    proposal.status = proposal.Status.PAID
    proposal.paid_at = proposal.paid_at or verified_at
    proposal.save(update_fields=["status", "paid_at", "updated_at"])
    if generation is None:
        commit_proposal_inventory(
            proposal,
            order=order,
            paid_at=verified_at,
        )
    else:
        from management.models import IgCheckoutInventoryReservation

        IgCheckoutInventoryReservation.objects.filter(
            invoice_generation_id=generation.pk,
            state__in=[
                IgCheckoutInventoryReservation.State.PAID_COMMITTED,
                IgCheckoutInventoryReservation.State.FULFILLED,
            ],
        ).update(order=order, updated_at=now)
        generation.state = generation.State.PAID_WINNER
        generation.paid_at = verified_at
        generation.terminal_at = verified_at
        generation.active_slot = None
        generation.save(update_fields=[
            "state", "paid_at", "terminal_at", "active_slot", "updated_at",
        ])
    from management.services.ig_lifecycle import (
        dispatch_lifecycle_event,
        ensure_lifecycle_event,
    )
    from management.services.ig_follow_cta import (
        payment_follow_preparation_due_at,
        prepare_local_payment_follow_snapshot,
        queue_payment_follow_preparation,
    )

    payment_follow_due_at = (
        None
        if proposal.assisted_checkout_v2
        else payment_follow_preparation_due_at(proposal.client, now=now)
    )
    event, _created = ensure_lifecycle_event(
        order,
        IgLifecycleEvent.Kind.PAYMENT_VERIFIED,
        payload={
            "attempt_id": attempt.pk,
            "attempt_reference": attempt.reference,
            "amount": str(attempt.paid_amount or attempt.payment_amount),
            "currency": proposal.currency,
        },
        # The payment confirmation is mandatory and must remain dispatchable
        # immediately.  Optional follow preparation owns its own bounded
        # deadline below and can never delay this lifecycle event.
        due_at=now,
    )
    # The durable event is committed with payment truth; only then may the
    # Instagram adapter call Meta. Replays claim the same event idempotently.
    if event is not None:
        try:
            # A fresh local NOT_FOLLOWING observation is already authoritative
            # enough for one deterministic optional sentence. This never calls
            # Meta or Gemini and therefore cannot delay the mandatory receipt.
            if not proposal.assisted_checkout_v2:
                prepare_local_payment_follow_snapshot(event.pk, now=now)
        except Exception:
            logger.exception(
                "Unable to prepare local payment follow snapshot for lifecycle event %s",
                event.pk,
            )
        try:
            if not proposal.assisted_checkout_v2:
                queue_payment_follow_preparation(
                    event.pk,
                    deadline_at=payment_follow_due_at,
                )
        except Exception:
            logger.exception(
                "Unable to queue optional payment follow preparation for lifecycle event %s",
                event.pk,
            )
        transaction.on_commit(
            lambda event_id=event.pk: dispatch_lifecycle_event(event_id)
        )
    try:
        proposal.client.set_stage(IgClient.Stage.ORDER_CREATED, reason="instagram_checkout_paid")
    except Exception:
        logger.warning("Failed to advance IG client stage for proposal %s", proposal.pk, exc_info=True)
    try:
        from management.services.bot_payment_truth import recalculate_client_payment_aggregates

        recalculate_client_payment_aggregates(proposal.client)
    except Exception:
        logger.warning("Failed to refresh IG payment aggregates for proposal %s", proposal.pk, exc_info=True)
    try:
        from management.services.ig_commercial_episodes import sync_episode_payment

        sync_episode_payment(deal=deal)
    except Exception:
        logger.warning("Failed to refresh IG episode payment for deal %s", deal.pk, exc_info=True)
    return event
