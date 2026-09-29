"""Generation-scoped Assisted Checkout V2 provider and winner boundaries."""
from __future__ import annotations

import hashlib
import json
import secrets
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Max
from django.utils.crypto import salted_hmac
from django.utils.dateparse import parse_datetime
from django.utils import timezone

from management.services.ig_checkout_payment import (
    CheckoutPaymentError,
    _capture_attempt_tracking,
    _enqueue_invoice_created_alert_best_effort,
    _invoice_payload,
    _revalidate_frozen_proposal,
    _send_add_payment_info_if_missing,
    _snapshot,
    _validate_payload,
    release_attempt_promo,
)
from management.services.ig_checkout_policy import (
    GENERATION_TTL_SECONDS,
    PREPAY_200_AMOUNT,
    PROVIDER_VALIDITY_SECONDS,
    payment_choice_for_post,
)
from management.services.ig_inventory import (
    release_generation_inventory,
    reserve_proposal_inventory,
)
from orders.checkout_series import (
    assisted_checkout_v2_mode,
    build_checkout_series_identity,
)
from orders.models import PaymentAttempt

AMBIGUITY_GRACE_SECONDS = 5 * 60


def _event(generation, kind, suffix, *, payload=None):
    from management.models import IgCheckoutInvoiceGenerationEvent

    return IgCheckoutInvoiceGenerationEvent.objects.get_or_create(
        event_key=(
            f"checkout-generation:{generation.pk}:{kind}:{suffix}"
        )[:180],
        defaults={
            "generation": generation,
            "proposal_id": generation.proposal_id,
            "payment_attempt_id": generation.payment_attempt_id,
            "kind": kind,
            "payload": payload if isinstance(payload, dict) else {},
        },
    )[0]


def _exact_provider_paid_amount(attempt, payload):
    if not isinstance(payload, dict):
        return False, "amount_payload_missing"
    observed = []
    for field in ("paidAmount", "finalAmount", "amount"):
        raw = payload.get(field)
        if raw is None:
            continue
        if isinstance(raw, bool):
            return False, "amount_malformed"
        if isinstance(raw, int):
            paid_minor = raw
        elif (
            isinstance(raw, str)
            and raw.isdigit()
            and len(raw) <= 18
            and (raw == "0" or not raw.startswith("0"))
        ):
            paid_minor = int(raw)
        else:
            return False, "amount_malformed"
        observed.append((field, paid_minor))
    if not observed:
        return False, "amount_missing"
    if len({value for _field, value in observed}) != 1:
        return False, "amount_conflict"
    paid_minor = observed[0][1]
    expected_minor = int(
        (Decimal(str(attempt.payment_amount)) * Decimal("100")).to_integral_value()
    )
    if paid_minor != expected_minor:
        return False, "amount_mismatch"
    return True, ",".join(field for field, _value in observed)


def _record_amount_reconciliation(generation, attempt, *, reason):
    now = timezone.now()
    event_state = dict(attempt.event_state or {})
    event_state["payment_amount_reconciliation"] = {
        "reason": str(reason or "amount_unverified")[:64],
        "expected_minor": int(
            (Decimal(str(attempt.payment_amount)) * Decimal("100")).to_integral_value()
        ),
        "observed_at": now.isoformat(),
    }
    event_state["payment_amount_reconciliation_pending"] = True
    attempt.event_state = event_state
    if (
        not attempt.order_id
        and not attempt.checkout_winner_claimed
        and attempt.status in {
            PaymentAttempt.Status.INITIATED,
            PaymentAttempt.Status.PROCESSING,
        }
    ):
        attempt.status = PaymentAttempt.Status.PROCESSING
    attempt.error_reason = f"provider_{reason}"[:500]
    attempt.last_status_at = now
    attempt.save(update_fields=[
        "status", "event_state", "error_reason", "last_status_at", "updated",
    ])
    _event(
        generation,
        "provider_ambiguous",
        f"amount:{attempt.pk}:{reason}",
        payload={
            "attempt_reference": attempt.reference,
            "amount_valid": False,
            "reason_code": str(reason or "amount_unverified")[:64],
        },
    )


def generation_for_attempt(attempt_id):
    from management.models import IgCheckoutInvoiceGeneration

    return (
        IgCheckoutInvoiceGeneration.objects.select_related("proposal", "proposal__deal")
        .filter(payment_attempt_id=attempt_id)
        .first()
    )


def _lock_generation_graph(attempt_id):
    """Canonical Deal -> Proposal -> Generation -> Attempt lock order."""
    from management.models import (
        IgCheckoutInvoiceGeneration,
        IgCheckoutProposal,
        IgDeal,
    )

    locator = (
        IgCheckoutInvoiceGeneration.objects.filter(payment_attempt_id=attempt_id)
        .values("pk", "proposal_id", "proposal__deal_id")
        .first()
    )
    if locator is None:
        return None
    deal = IgDeal.objects.select_for_update().get(pk=locator["proposal__deal_id"])
    proposal = IgCheckoutProposal.objects.select_for_update().select_related(
        "client", "commercial_episode"
    ).get(pk=locator["proposal_id"], deal_id=deal.pk)
    locked_generations = list(
        IgCheckoutInvoiceGeneration.objects.select_for_update()
        .filter(proposal_id=proposal.pk)
        .order_by("generation", "pk")
    )
    generation = next(
        row for row in locked_generations if row.pk == locator["pk"]
    )
    attempt = PaymentAttempt.objects.select_for_update().get(
        pk=attempt_id,
        checkout_series_key=generation.series_key,
        checkout_generation=generation.generation,
    )
    generation._state.fields_cache["payment_attempt"] = attempt
    generation._state.fields_cache["proposal"] = proposal
    generation._locked_sibling_generations = locked_generations
    return deal, proposal, generation, attempt


def _attempt_fingerprint(
    proposal,
    generation_number,
    payment_choice,
    *,
    values,
):
    delivery = values["delivery"]
    payload = {
        "version": 2,
        "proposal": str(proposal.public_id),
        "revision": proposal.revision,
        "generation": generation_number,
        "payment_choice": payment_choice,
        "recipient": {
            "full_name": values["full_name"],
            "phone": values["phone"],
            "email": values["email"].lower(),
        },
        "delivery": {
            "settlement_ref": delivery.settlement_ref,
            "city_ref": delivery.city_ref,
            "warehouse_ref": delivery.warehouse_ref,
        },
        "promo": values["promo_code"],
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _server_owned_recipient_payload(attempt, deal):
    """Rebuild validation input without exposing or trusting bearer form PII."""
    from orders.nova_poshta_checkout import (
        build_city_choice_token,
        build_warehouse_choice_token,
    )

    return {
        "full_name": attempt.full_name or "",
        "phone": attempt.phone or "",
        "email": attempt.email or "",
        "city": attempt.city or "",
        "np_settlement_ref": attempt.np_settlement_ref or "",
        "np_city_ref": attempt.np_city_ref or "",
        "np_city_token": build_city_choice_token({
            "label": attempt.city or "",
            "settlement_ref": attempt.np_settlement_ref or "",
            "city_ref": attempt.np_city_ref or "",
        }),
        "np_office": attempt.np_office or "",
        "np_warehouse_ref": attempt.np_warehouse_ref or "",
        "np_warehouse_token": build_warehouse_choice_token({
            "label": attempt.np_office or "",
            "ref": attempt.np_warehouse_ref or "",
            "kind": (
                (attempt.event_state or {}).get("recipient_lock_warehouse_kind")
                or deal.np_warehouse_kind
                or "branch"
            ),
            "city_ref": attempt.np_city_ref or "",
        }),
        "promo_code": (
            attempt.promo_code.code if attempt.promo_code_id else ""
        ),
    }


def _provider_boundary_uncertain(generation, attempt) -> bool:
    event_state = attempt.event_state if attempt is not None else {}
    return bool(
        generation.state in {
            generation.State.PLANNED,
            generation.State.PROVIDER_INFLIGHT,
            generation.State.PROVIDER_AMBIGUOUS,
            generation.State.AMBIGUITY_REVIEW,
            generation.State.LATE_PROVIDER_REVIEW,
        }
        or (event_state or {}).get("invoice_creation_ambiguous")
        or (
            not generation.provider_invoice_id
            and bool(generation.provider_request_digest)
            and generation.state
            not in {generation.State.FAILED, generation.State.CANCELLED}
        )
        or (
            generation.state in {
                generation.State.INVOICE_CREATED,
                generation.State.EXPIRED,
                generation.State.FAILED,
                generation.State.CANCELLED,
            }
            and not _retry_source_is_safe(generation, attempt)
        )
    )


def _provider_identity_matches(generation, attempt) -> bool:
    generation_identity = str(generation.provider_invoice_id or "")
    attempt_identity = str(
        getattr(attempt, "monobank_invoice_id", "") or ""
    )
    return bool(
        generation_identity
        and attempt_identity
        and secrets.compare_digest(generation_identity, attempt_identity)
    )


_TRUSTED_PROVIDER_TERMINAL_SOURCES = frozenset({
    "webhook",
    "provider_pull",
    "return",
    "ig_reconcile",
})
_PROVIDER_TERMINAL_PROOF_SCHEMA = 1
_PROVIDER_TERMINAL_PROOF_KEY_ID = "django-secret-key-v1"
_PROVIDER_TERMINAL_PROOF_SALT = "management.ig_checkout.provider_terminal.v1"
_PROVIDER_TERMINAL_PROOF_FIELDS = frozenset({
    "schema_version",
    "hmac_key_id",
    "state",
    "provider_status",
    "source",
    "generation_id",
    "provider_call_token_digest",
    "provider_invoice_id_digest",
    "attempt_reference",
    "currency",
    "evidence_digest",
    "observed_at",
    "proof_hmac",
})


def _canonical_provider_terminal_status(value) -> str:
    normalized = str(value or "").strip().casefold()
    return "cancelled" if normalized == "canceled" else normalized


def _provider_terminal_evidence(
    *, status, invoice_id, reference, currency, source
) -> dict:
    return {
        "status": str(status),
        "invoice_id": str(invoice_id),
        "reference": str(reference),
        "currency": str(currency),
        "source": str(source),
    }


def _canonical_json_digest(value) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _provider_terminal_proof_hmac(proof) -> str:
    if not getattr(settings, "SECRET_KEY", ""):
        return ""
    signed = {
        key: proof[key]
        for key in sorted(_PROVIDER_TERMINAL_PROOF_FIELDS - {"proof_hmac"})
    }
    payload = json.dumps(
        signed,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return salted_hmac(
        _PROVIDER_TERMINAL_PROOF_SALT,
        payload,
        secret=settings.SECRET_KEY,
        algorithm="sha256",
    ).hexdigest()


def _build_provider_terminal_proof(
    generation,
    attempt,
    *,
    provider_status,
    provider_payload,
    source,
    now,
):
    normalized = _canonical_provider_terminal_status(provider_status)
    source = str(source or "").strip()
    if (
        normalized
        not in {"failure", "rejected", "reversed", "cancelled", "expired"}
        or source not in _TRUSTED_PROVIDER_TERMINAL_SOURCES
        or not generation.provider_call_token
        or not _provider_identity_matches(generation, attempt)
        or not isinstance(provider_payload, dict)
    ):
        return None
    envelope = provider_payload.get("result")
    if not isinstance(envelope, dict):
        envelope = provider_payload
    merchant = envelope.get("merchantPaymInfo") or {}
    if not isinstance(merchant, dict):
        merchant = {}
    observed_status = _canonical_provider_terminal_status(
        envelope.get("status") or envelope.get("statusCode")
    )
    observed_invoice_id = str(
        envelope.get("invoiceId") or envelope.get("invoice_id") or ""
    )
    observed_reference = str(
        envelope.get("reference") or merchant.get("reference") or ""
    )
    observed_currency = str(
        envelope.get("ccy") or envelope.get("currencyCode") or ""
    )
    canonical_currency = "980" if observed_currency == "UAH" else observed_currency
    if (
        observed_status != normalized
        or not observed_invoice_id
        or not secrets.compare_digest(
            observed_invoice_id,
            str(generation.provider_invoice_id or ""),
        )
        or not observed_reference
        or not secrets.compare_digest(observed_reference, str(attempt.reference))
        or canonical_currency != "980"
    ):
        return None
    evidence = _provider_terminal_evidence(
        status=normalized,
        invoice_id=observed_invoice_id,
        reference=observed_reference,
        currency=canonical_currency,
        source=source,
    )
    proof = {
        "schema_version": _PROVIDER_TERMINAL_PROOF_SCHEMA,
        "hmac_key_id": _PROVIDER_TERMINAL_PROOF_KEY_ID,
        "state": "provider_terminal",
        "provider_status": normalized,
        "source": source,
        "generation_id": generation.pk,
        "provider_call_token_digest": hashlib.sha256(
            str(generation.provider_call_token or "").encode()
        ).hexdigest(),
        "provider_invoice_id_digest": hashlib.sha256(
            observed_invoice_id.encode()
        ).hexdigest(),
        "attempt_reference": observed_reference,
        "currency": canonical_currency,
        "evidence_digest": _canonical_json_digest(evidence),
        "observed_at": now.isoformat(),
    }
    proof["proof_hmac"] = _provider_terminal_proof_hmac(proof)
    return proof


def _provider_terminal_proof_matches(generation, attempt, allowed_statuses) -> bool:
    if (
        attempt is None
        or not generation.provider_call_token
        or not _provider_identity_matches(generation, attempt)
    ):
        return False
    proof = (attempt.event_state or {}).get("provider_terminal")
    if (
        not isinstance(proof, dict)
        or set(proof) != _PROVIDER_TERMINAL_PROOF_FIELDS
        or proof.get("schema_version") != _PROVIDER_TERMINAL_PROOF_SCHEMA
        or proof.get("hmac_key_id") != _PROVIDER_TERMINAL_PROOF_KEY_ID
    ):
        return False
    try:
        proof_generation_id = int(proof.get("generation_id") or 0)
    except (TypeError, ValueError, OverflowError):
        return False
    expected_token_digest = hashlib.sha256(
        str(generation.provider_call_token or "").encode()
    ).hexdigest()
    expected_identity_digest = hashlib.sha256(
        str(generation.provider_invoice_id or "").encode()
    ).hexdigest()
    status = _canonical_provider_terminal_status(proof.get("provider_status"))
    source = str(proof.get("source") or "")
    reference = str(proof.get("attempt_reference") or "")
    currency = str(proof.get("currency") or "")
    observed_at_raw = str(proof.get("observed_at") or "")
    observed_at = parse_datetime(observed_at_raw)
    if (
        observed_at is None
        or timezone.is_naive(observed_at)
        or observed_at > timezone.now()
    ):
        return False
    proposal_expires_at = generation.proposal.expires_at
    upper_bound = max(generation.expires_at, proposal_expires_at) + timedelta(
        seconds=AMBIGUITY_GRACE_SECONDS
    )
    if observed_at < generation.created_at or observed_at > upper_bound:
        return False
    if (
        not reference
        or not secrets.compare_digest(reference, str(attempt.reference))
        or currency != "980"
    ):
        return False
    expected_evidence_digest = _canonical_json_digest(
        _provider_terminal_evidence(
            status=status,
            invoice_id=str(generation.provider_invoice_id or ""),
            reference=str(attempt.reference),
            currency=currency,
            source=source,
        )
    )
    expected_proof_hmac = _provider_terminal_proof_hmac(proof)
    return bool(
        proof.get("state") == "provider_terminal"
        and status in set(allowed_statuses)
        and source in _TRUSTED_PROVIDER_TERMINAL_SOURCES
        and proof_generation_id == int(generation.pk)
        and secrets.compare_digest(
            str(proof.get("evidence_digest") or ""),
            expected_evidence_digest,
        )
        and secrets.compare_digest(
            str(proof.get("provider_call_token_digest") or ""),
            expected_token_digest,
        )
        and secrets.compare_digest(
            str(proof.get("provider_invoice_id_digest") or ""),
            expected_identity_digest,
        )
        and bool(expected_proof_hmac)
        and secrets.compare_digest(
            str(proof.get("proof_hmac") or ""),
            expected_proof_hmac,
        )
    )


def _cancelled_pre_dispatch_proof_matches(generation, attempt) -> bool:
    if (
        attempt is None
        or generation.provider_request_digest
        or not generation.provider_call_token
    ):
        return False
    if generation.provider_invoice_id or attempt.monobank_invoice_id:
        return False
    if attempt.invoice_url or attempt.invoice_payload or attempt.payment_history:
        return False
    proof = (attempt.event_state or {}).get("provider_boundary")
    if not isinstance(proof, dict):
        return False
    try:
        proof_generation_id = int(proof.get("generation_id") or 0)
    except (TypeError, ValueError, OverflowError):
        return False
    expected_token_digest = hashlib.sha256(
        str(generation.provider_call_token or "").encode()
    ).hexdigest()
    return bool(
        proof.get("state") == "cancelled_pre_dispatch"
        and proof.get("source") == "checkout_session_reset"
        and bool(str(proof.get("observed_at") or ""))
        and proof_generation_id == int(generation.pk)
        and secrets.compare_digest(
            str(proof.get("provider_call_token_digest") or ""),
            expected_token_digest,
        )
    )


def _retry_source_is_safe(generation, attempt) -> bool:
    if attempt is None or (attempt.event_state or {}).get(
        "invoice_creation_ambiguous"
    ):
        return False
    if generation.state == generation.State.FAILED:
        return bool(
            attempt.status == PaymentAttempt.Status.FAILED
            and (
                _provider_terminal_proof_matches(
                    generation,
                    attempt,
                    {"failure", "rejected", "reversed"},
                )
                or _cancelled_pre_dispatch_proof_matches(generation, attempt)
            )
        )
    if generation.state == generation.State.CANCELLED:
        return bool(
            attempt.status == PaymentAttempt.Status.CANCELLED
            and (
                _provider_terminal_proof_matches(
                    generation,
                    attempt,
                    {"cancelled"},
                )
                or _cancelled_pre_dispatch_proof_matches(generation, attempt)
            )
        )
    if generation.state == generation.State.EXPIRED:
        return bool(
            _provider_identity_matches(generation, attempt)
            and attempt.status == PaymentAttempt.Status.EXPIRED
            and (
                generation.expires_at <= timezone.now()
                or _provider_terminal_proof_matches(
                    generation,
                    attempt,
                    {"expired"},
                )
            )
        )
    if generation.state == generation.State.INVOICE_CREATED:
        return bool(
            _provider_identity_matches(generation, attempt)
            and attempt.status in {
                PaymentAttempt.Status.INITIATED,
                PaymentAttempt.Status.PROCESSING,
            }
        )
    return False


def _clear_current_generation(proposal, generation, *, terminal_status):
    if proposal.current_invoice_generation_id != generation.pk:
        return
    proposal.current_invoice_generation = None
    if proposal.payment_attempt_id == generation.payment_attempt_id:
        proposal.payment_attempt = None
        proposal.details_locked_at = None
    if proposal.winner_invoice_generation_id:
        return
    proposal.status = (
        proposal.Status.EXPIRED
        if proposal.expires_at <= timezone.now()
        else proposal.Status.VIEWED
        if proposal.viewed_at
        else proposal.Status.READY
    )
    proposal.save(
        update_fields=[
            "current_invoice_generation",
            "payment_attempt",
            "details_locked_at",
            "status",
            "updated_at",
        ]
    )


def _terminalize_locked_generation(
    proposal,
    generation,
    attempt,
    *,
    state,
    attempt_status,
    reason,
    now,
):
    generation.state = state
    generation.active_slot = None
    generation.terminal_at = now
    generation.review_reason = str(reason or "")[:96]
    generation.save(
        update_fields=[
            "state", "active_slot", "terminal_at", "review_reason", "updated_at",
        ]
    )
    if attempt.order_id is None and not attempt.checkout_winner_claimed:
        attempt.status = attempt_status
        attempt.error_reason = str(reason or attempt_status)[:500]
        attempt.last_status_at = now
        attempt.save(
            update_fields=["status", "error_reason", "last_status_at", "updated"]
        )
    _clear_current_generation(proposal, generation, terminal_status=state)
    released = release_generation_inventory(generation, reason=reason)
    released_promo = release_attempt_promo(attempt, reason=reason)
    _event(
        generation,
        "terminalized",
        f"{state}:{attempt.pk}",
        payload={
            "state": state,
            "reason": str(reason or "")[:96],
            "released_inventory": int(released or 0),
            "released_promo": bool(released_promo),
        },
    )
    return int(released or 0), bool(released_promo)


@transaction.atomic
def _prepare_generation(proposal, *, request, payload, grant_id=""):
    from management.models import (
        IgCheckoutInvoiceGeneration,
        IgCheckoutProposal,
        IgDeal,
    )

    deal = IgDeal.objects.select_for_update().get(pk=proposal.deal_id)
    locked = IgCheckoutProposal.objects.select_for_update().select_related(
        "client", "commercial_episode"
    ).get(pk=proposal.pk, deal_id=deal.pk)
    from storefront.services.brigade_commerce import brigade_payment_error
    from management.services.ig_checkout_payment import _proposal_brigade_policy
    brigade_full_only, _eligible = _proposal_brigade_policy(locked)
    if brigade_full_only and str(payload.get("payment_choice") or "online_full").strip().casefold() == "prepay_200_cod":
        raise CheckoutPaymentError("brigade_full_payment_required", brigade_payment_error(), field="payment_choice")
    if not locked.assisted_checkout_v2:
        raise CheckoutPaymentError("unavailable", "V2 checkout is unavailable.")
    now = timezone.now()
    if locked.status == locked.Status.MANAGER_REVIEW:
        raise CheckoutPaymentError(
            "provider_ambiguous",
            "Статус попереднього рахунку потрібно звірити перед повтором.",
        )
    if locked.expires_at <= now or locked.status in {
        locked.Status.PAID,
        locked.Status.REVOKED,
        locked.Status.SUPERSEDED,
    }:
        raise CheckoutPaymentError("expired", "Срок действия предложения истек.")
    generations = list(
        IgCheckoutInvoiceGeneration.objects.select_for_update()
        .filter(proposal_id=locked.pk)
        .order_by("generation", "pk")
    )

    def lock_attempt(row):
        if row is None or not row.payment_attempt_id:
            return None
        attempt = (
            PaymentAttempt.objects.select_for_update()
            .select_related("promo_code")
            .get(pk=row.payment_attempt_id)
        )
        if (
            brigade_full_only
            and attempt.pay_type != PaymentAttempt.PayType.ONLINE_FULL
            and attempt.status not in {PaymentAttempt.Status.PAID, PaymentAttempt.Status.PREPAID}
        ):
            raise CheckoutPaymentError("brigade_full_payment_required", brigade_payment_error(), field="payment_choice")
        return attempt

    if locked.winner_invoice_generation_id:
        winner = next(
            (
                row
                for row in generations
                if row.pk == locked.winner_invoice_generation_id
            ),
            None,
        )
        if winner and winner.payment_attempt_id:
            attempt = lock_attempt(winner)
            if attempt.invoice_url:
                return locked, winner, attempt, None, True
        raise CheckoutPaymentError("unavailable", "Оплата вже обробляється.")

    current = next(
        (
            row
            for row in generations
            if row.pk == locked.current_invoice_generation_id
        ),
        None,
    )
    latest = generations[-1] if generations else None
    requested_reissue = str(payload.get("reissue_generation") or "").strip()
    recipient_source_generation = None
    recipient_source_attempt = None

    def require_locked_reissue(row):
        if requested_reissue != str(row.generation):
            raise CheckoutPaymentError(
                "recipient_locked",
                "Дані отримувача вже зафіксовані. Для змін створіть нову пропозицію в Direct.",
            )

    if current is not None:
        attempt = lock_attempt(current)
        if attempt is not None and attempt.invoice_url and current.expires_at > now:
            return locked, current, attempt, None, True
        if current.state in {
            current.State.PLANNED,
            current.State.PROVIDER_INFLIGHT,
            current.State.PROVIDER_AMBIGUOUS,
        }:
            raise CheckoutPaymentError(
                "provider_ambiguous",
                "Платіж уже передано банку. Не створюйте новий рахунок.",
            )
        if current.active_slot == 1 and current.expires_at > now:
            raise CheckoutPaymentError(
                "in_progress", "Платіж уже створюється. Зачекайте кілька секунд."
            )
        if current.state == current.State.INVOICE_CREATED:
            if not _retry_source_is_safe(current, attempt):
                raise CheckoutPaymentError(
                    "provider_ambiguous",
                    "Статус попереднього рахунку потрібно звірити перед повтором.",
                )
            if current.expires_at > now:
                raise CheckoutPaymentError(
                    "in_progress", "Рахунок уже створено."
                )
            require_locked_reissue(current)
            recipient_source_generation = current
            recipient_source_attempt = attempt
            if attempt is not None:
                _terminalize_locked_generation(
                    locked,
                    current,
                    attempt,
                    state=current.State.EXPIRED,
                    attempt_status=PaymentAttempt.Status.EXPIRED,
                    reason="generation_expired",
                    now=now,
                )
        elif current.state == current.State.EXPIRED:
            if not _retry_source_is_safe(current, attempt):
                raise CheckoutPaymentError(
                    "provider_ambiguous",
                    "Статус попереднього рахунку потрібно звірити перед повтором.",
                )
            require_locked_reissue(current)
            recipient_source_generation = current
            recipient_source_attempt = attempt
            if attempt is not None:
                _terminalize_locked_generation(
                    locked,
                    current,
                    attempt,
                    state=current.State.EXPIRED,
                    attempt_status=PaymentAttempt.Status.EXPIRED,
                    reason="generation_expired",
                    now=now,
                )
        elif current.state in {current.State.FAILED, current.State.CANCELLED}:
            if not _retry_source_is_safe(current, attempt):
                raise CheckoutPaymentError(
                    "provider_ambiguous",
                    "Статус попереднього рахунку потрібно звірити перед повтором.",
                )
            if requested_reissue and requested_reissue != str(current.generation):
                require_locked_reissue(current)
            recipient_source_generation = current
            recipient_source_attempt = attempt
        else:
            raise CheckoutPaymentError(
                "unavailable", "Цей рахунок не можна перевипустити автоматично."
            )
    elif latest is not None:
        attempt = lock_attempt(latest)
        if latest.state == latest.State.EXPIRED:
            if not _retry_source_is_safe(latest, attempt):
                raise CheckoutPaymentError(
                    "provider_ambiguous",
                    "Статус попереднього рахунку потрібно звірити перед повтором.",
                )
            require_locked_reissue(latest)
            recipient_source_generation = latest
            recipient_source_attempt = attempt
        elif latest.state in {latest.State.FAILED, latest.State.CANCELLED}:
            if not _retry_source_is_safe(latest, attempt):
                raise CheckoutPaymentError(
                    "provider_ambiguous",
                    "Статус попереднього рахунку потрібно звірити перед повтором.",
                )
            if requested_reissue and requested_reissue != str(latest.generation):
                require_locked_reissue(latest)
            recipient_source_generation = latest
            recipient_source_attempt = attempt
        elif (
            latest.state == latest.State.INVOICE_CREATED
            and latest.expires_at <= now
        ):
            if not _retry_source_is_safe(latest, attempt):
                raise CheckoutPaymentError(
                    "provider_ambiguous",
                    "Статус попереднього рахунку потрібно звірити перед повтором.",
                )
            require_locked_reissue(latest)
            recipient_source_generation = latest
            recipient_source_attempt = attempt
            if attempt is not None:
                _terminalize_locked_generation(
                    locked,
                    latest,
                    attempt,
                    state=latest.State.EXPIRED,
                    attempt_status=PaymentAttempt.Status.EXPIRED,
                    reason="generation_expired",
                    now=now,
                )
        else:
            raise CheckoutPaymentError(
                "provider_ambiguous",
                "Статус попереднього рахунку потрібно звірити перед повтором.",
            )
    elif requested_reissue:
        raise CheckoutPaymentError(
            "recipient_locked", "Немає зафіксованих даних для повторного рахунку."
        )
    if (
        recipient_source_generation is not None
        and recipient_source_attempt is None
    ):
        raise CheckoutPaymentError(
            "unavailable", "Зафіксовані дані рахунку недоступні."
        )
    recipient_source_user = None
    if (
        recipient_source_attempt is not None
        and recipient_source_attempt.user_id
    ):
        from django.contrib.auth import get_user_model

        recipient_source_user = get_user_model().objects.filter(
            pk=recipient_source_attempt.user_id
        ).first()

    # Disabling rollout stops creation of new provider generations while all
    # callbacks/reconciliation for existing rows continue to work below.
    if assisted_checkout_v2_mode() != "enforced":
        raise CheckoutPaymentError(
            "unavailable", "Створення нового рахунку тимчасово призупинено."
        )

    _revalidate_frozen_proposal(locked)
    try:
        payment_choice = payment_choice_for_post(
            locked,
            (
                recipient_source_generation.payment_choice
                if recipient_source_generation is not None
                else payload.get("payment_choice")
            ),
        )
    except ValueError as exc:
        raise CheckoutPaymentError(
            "payment_choice",
            "Оберіть доступний спосіб оплати.",
            field="payment_choice",
        ) from exc
    if brigade_full_only and payment_choice != IgCheckoutInvoiceGeneration.PaymentChoice.FULL:
        raise CheckoutPaymentError("brigade_full_payment_required", brigade_payment_error(), field="payment_choice")
    if recipient_source_attempt is not None:
        values = _validate_payload(
            locked,
            _server_owned_recipient_payload(recipient_source_attempt, deal),
            user=recipient_source_user,
        )
    else:
        values = _validate_payload(locked, payload, user=request.user)
    if (
        payment_choice
        == IgCheckoutInvoiceGeneration.PaymentChoice.PREPAY_200_COD
        and values["order_payable"] > Decimal(PREPAY_200_AMOUNT)
    ):
        payment_amount = Decimal(PREPAY_200_AMOUNT)
        attempt_pay_type = PaymentAttempt.PayType.PREPAY_200
    else:
        payment_choice = IgCheckoutInvoiceGeneration.PaymentChoice.FULL
        payment_amount = values["order_payable"]
        attempt_pay_type = PaymentAttempt.PayType.ONLINE_FULL

    generation_number = int(
        IgCheckoutInvoiceGeneration.objects.filter(proposal_id=locked.pk)
        .aggregate(value=Max("generation"))["value"]
        or 0
    ) + 1
    identity = build_checkout_series_identity(
        locked.public_id,
        generation=generation_number,
    )
    expires_at = now + timedelta(seconds=GENERATION_TTL_SECONDS)
    generation = IgCheckoutInvoiceGeneration.objects.create(
        proposal=locked,
        generation=generation_number,
        series_key=identity.series_key,
        proposal_revision=locked.revision,
        active_slot=1,
        state=IgCheckoutInvoiceGeneration.State.PROVIDER_INFLIGHT,
        payment_choice=payment_choice,
        payment_amount=payment_amount,
        policy_evidence_message_id=locked.payment_policy_evidence_message_id,
        policy_evidence_kind=locked.payment_policy_evidence_kind,
        policy_evidence_digest=locked.payment_policy_evidence_digest,
        promo_reservation_generation=str(
            (
                (
                    values["promo_event_state"].get("promo_reservation")
                    or {}
                )
                if isinstance(values["promo_event_state"], dict)
                else {}
            ).get("reservation_generation")
            or ""
        )[:64],
        provider_call_token=secrets.token_hex(32),
        expires_at=expires_at,
        provider_started_at=now,
    )
    if not request.session.session_key:
        request.session.save()
    attempt = PaymentAttempt.objects.create(
        fingerprint=_attempt_fingerprint(
            locked,
            generation_number,
            payment_choice,
            values=values,
        ),
        user=(
            recipient_source_user
            if recipient_source_attempt is not None
            else request.user if request.user.is_authenticated else None
        ),
        session_key=request.session.session_key,
        full_name=values["full_name"],
        phone=values["phone"],
        email=values["email"],
        city=values["delivery"].city,
        np_office=values["delivery"].np_office,
        np_settlement_ref=values["delivery"].settlement_ref,
        np_city_ref=values["delivery"].city_ref,
        np_warehouse_ref=values["delivery"].warehouse_ref,
        pay_type=attempt_pay_type,
        status=PaymentAttempt.Status.PROCESSING,
        cart_snapshot={
            **_snapshot(locked),
            "checkout_generation_id": generation.pk,
            "checkout_series_key": identity.series_key,
        },
        gross_amount=locked.catalog_total,
        discount_amount=(
            Decimal(locked.negotiated_discount or 0)
            + values["promo_discount"]
        ),
        payable_amount=values["order_payable"],
        payment_amount=payment_amount,
        promo_code=values["promo"],
        event_state={
            **values["promo_event_state"],
            "checkout_generation_id": generation.pk,
            "provider_call_token": generation.provider_call_token,
            "recipient_lock_warehouse_kind": values["delivery"].warehouse_kind,
            **(
                {
                    "recipient_lock_source_attempt_id": recipient_source_attempt.pk,
                    "recipient_lock_source_generation": (
                        recipient_source_generation.generation
                    ),
                }
                if recipient_source_attempt is not None
                else {}
            ),
        },
        invoice_expires_at=expires_at,
        checkout_series_key=identity.series_key,
        checkout_generation=generation_number,
    )
    tracking = _capture_attempt_tracking(request, attempt)
    if grant_id:
        tracking["ig_checkout_grant_id"] = str(grant_id)[:64]
        tracking["ig_checkout_generation_id"] = generation.pk
        attempt.tracking_payload = tracking
        attempt.save(update_fields=["tracking_payload", "updated"])
    generation.payment_attempt = attempt
    generation.save(update_fields=["payment_attempt", "updated_at"])
    locked.current_invoice_generation = generation
    locked.payment_attempt = attempt
    locked.status = locked.Status.DETAILS_LOCKED
    locked.details_locked_at = now
    locked.save(
        update_fields=[
            "current_invoice_generation", "payment_attempt", "status",
            "details_locked_at", "updated_at",
        ]
    )
    deal.np_full_name = values["full_name"]
    deal.np_phone = values["phone"]
    deal.np_city = values["delivery"].city
    deal.np_office = values["delivery"].np_office
    deal.np_settlement_ref = values["delivery"].settlement_ref
    deal.np_city_ref = values["delivery"].city_ref
    deal.np_warehouse_ref = values["delivery"].warehouse_ref
    deal.np_warehouse_kind = values["delivery"].warehouse_kind
    deal.delivery_status = deal.DeliveryStatus.VALIDATED
    deal.delivery_source = "instagram_checkout_v2"
    deal.status = deal.Status.AWAITING_PAYMENT
    deal.payment_status = "unpaid"
    deal.save(
        update_fields=[
            "np_full_name", "np_phone", "np_city", "np_office",
            "np_settlement_ref", "np_city_ref", "np_warehouse_ref",
            "np_warehouse_kind", "delivery_status", "delivery_source",
            "status", "payment_status", "updated_at",
        ]
    )
    reserve_proposal_inventory(
        locked,
        generation=generation,
        expires_at=expires_at,
    )
    _event(
        generation,
        "created",
        str(attempt.pk),
        payload={
            "generation": generation_number,
            "payment_choice": payment_choice,
            "expires_at": expires_at.isoformat(),
            "recipient_lock_source_generation": (
                recipient_source_generation.generation
                if recipient_source_generation is not None
                else None
            ),
        },
    )
    return locked, generation, attempt, values, False


@transaction.atomic
def _persist_provider_dispatch_evidence(attempt_id, invoice_payload):
    graph = _lock_generation_graph(attempt_id)
    if graph is None:
        raise CheckoutPaymentError("provider_ambiguous", "Generation ownership missing.")
    _deal, proposal, generation, attempt = graph
    if (
        proposal.current_invoice_generation_id != generation.pk
        or generation.active_slot != 1
        or generation.state != generation.State.PROVIDER_INFLIGHT
    ):
        raise CheckoutPaymentError(
            "provider_ambiguous",
            "Generation is no longer current before provider dispatch.",
        )
    encoded = json.dumps(
        invoice_payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()
    generation.provider_request_digest = hashlib.sha256(encoded).hexdigest()
    generation.save(update_fields=["provider_request_digest", "updated_at"])
    _event(
        generation,
        "provider_started",
        str(attempt.pk),
        payload={
            "attempt_reference": attempt.reference,
            "request_digest": generation.provider_request_digest,
            "provider_validity_seconds": PROVIDER_VALIDITY_SECONDS,
            "generation_expires_at": generation.expires_at.isoformat(),
        },
    )
    return generation.provider_request_digest


@transaction.atomic
def _persist_provider_success(
    attempt_id,
    *,
    invoice_id,
    invoice_url,
    invoice_payload,
    creation,
):
    graph = _lock_generation_graph(attempt_id)
    if graph is None:
        raise CheckoutPaymentError("provider_ambiguous", "Generation ownership missing.")
    deal, proposal, generation, attempt = graph
    terminal_or_winner = bool(
        attempt.order_id
        or attempt.checkout_winner_claimed
        or attempt.status in {
            PaymentAttempt.Status.PAID,
            PaymentAttempt.Status.PREPAID,
            PaymentAttempt.Status.CONVERTED,
        }
        or deal.order_id
        or proposal.winner_invoice_generation_id
        or proposal.status == proposal.Status.PAID
        or generation.winner_slot == 1
        or generation.state in {
            generation.State.WINNER_CLAIMED,
            generation.State.PAID_WINNER,
            generation.State.RESOURCE_REVIEW,
            generation.State.LATE_PAID_REVIEW,
        }
    )
    if terminal_or_winner:
        now = timezone.now()
        returned_invoice_id = str(invoice_id or "")[:128]
        known_invoice_id = str(
            generation.provider_invoice_id or attempt.monobank_invoice_id or ""
        )[:128]
        identity_conflict = bool(
            known_invoice_id
            and returned_invoice_id
            and known_invoice_id != returned_invoice_id
        )
        if not generation.provider_invoice_id:
            generation.provider_invoice_id = known_invoice_id or returned_invoice_id
        if (
            generation.winner_slot != 1
            and generation.state
            not in {
                generation.State.PAID_WINNER,
                generation.State.RESOURCE_REVIEW,
                generation.State.LATE_PAID_REVIEW,
            }
        ):
            generation.state = generation.State.LATE_PROVIDER_REVIEW
            generation.active_slot = None
            generation.review_reason = "provider_create_settled_after_winner"
        generation.provider_completed_at = generation.provider_completed_at or now
        generation.save(update_fields=[
            "provider_invoice_id", "state", "active_slot", "review_reason",
            "provider_completed_at", "updated_at",
        ])
        if not attempt.monobank_invoice_id:
            attempt.monobank_invoice_id = (
                generation.provider_invoice_id or returned_invoice_id
            )
        if invoice_url and not attempt.invoice_url:
            attempt.invoice_url = str(invoice_url)[:600]
        attempt.invoice_payload = {
            "request": invoice_payload,
            "create": creation,
            "ignored_late_transport_outcome": True,
        }
        attempt.invoice_expires_at = attempt.invoice_expires_at or generation.expires_at
        attempt.save(update_fields=[
            "monobank_invoice_id", "invoice_url", "invoice_payload",
            "invoice_expires_at", "updated",
        ])
        _event(
            generation,
            "provider_succeeded",
            f"ignored-after-winner:{attempt.pk}",
            payload={
                "attempt_reference": attempt.reference,
                "returned_provider_invoice_id": returned_invoice_id,
                "stored_provider_invoice_id": generation.provider_invoice_id or "",
                "identity_conflict": identity_conflict,
                "ignored_late_transport_outcome": True,
                "winner_generation_id": proposal.winner_invoice_generation_id,
                "order_id": attempt.order_id or deal.order_id,
            },
        )
        _queue_ambiguity_review(
            proposal,
            generation,
            attempt,
            reason="provider_create_settled_after_winner",
        )
        return proposal, generation, attempt, False
    if generation.provider_invoice_id:
        if generation.provider_invoice_id != str(invoice_id):
            raise CheckoutPaymentError("provider_ambiguous", "Invoice identity conflict.")
        if (
            proposal.current_invoice_generation_id == generation.pk
            and generation.active_slot == 1
            and generation.state == generation.State.INVOICE_CREATED
        ):
            return proposal, generation, attempt, True
        return proposal, generation, attempt, False
    now = timezone.now()
    if (
        proposal.current_invoice_generation_id != generation.pk
        or generation.active_slot != 1
        or generation.state != generation.State.PROVIDER_INFLIGHT
    ):
        generation.provider_invoice_id = str(invoice_id)[:128]
        generation.state = generation.State.LATE_PROVIDER_REVIEW
        generation.active_slot = None
        generation.provider_completed_at = now
        generation.ambiguity_review_due_at = generation.expires_at + timedelta(
            seconds=AMBIGUITY_GRACE_SECONDS
        )
        generation.review_reason = "late_provider_create_success"
        generation.save(update_fields=[
            "provider_invoice_id", "state", "active_slot",
            "provider_completed_at", "ambiguity_review_due_at", "review_reason",
            "updated_at",
        ])
        _gate_proposal_for_late_provider_review(
            proposal,
            generation,
            now=now,
        )
        attempt.monobank_invoice_id = generation.provider_invoice_id
        attempt.invoice_url = str(invoice_url)[:600]
        attempt.invoice_payload = {
            "request": invoice_payload,
            "create": creation,
        }
        attempt.invoice_expires_at = generation.expires_at
        event_state = dict(attempt.event_state or {})
        event_state["invoice_creation_ambiguous"] = True
        attempt.event_state = event_state
        attempt.error_reason = "late_provider_create_success_review"
        attempt.last_status_at = now
        attempt.save(update_fields=[
            "monobank_invoice_id", "invoice_url", "invoice_payload",
            "invoice_expires_at", "event_state", "error_reason", "last_status_at",
            "updated",
        ])
        _event(
            generation,
            "provider_ambiguous",
            f"late-success:{attempt.pk}",
            payload={
                "attempt_reference": attempt.reference,
                "provider_invoice_id": generation.provider_invoice_id,
                "request_digest": generation.provider_request_digest,
                "review_due_at": generation.ambiguity_review_due_at.isoformat(),
                "late_result": True,
            },
        )
        _queue_ambiguity_review(
            proposal,
            generation,
            attempt,
            reason="late_provider_create_success",
        )
        return proposal, generation, attempt, False
    generation.provider_invoice_id = str(invoice_id)[:128]
    generation.state = generation.State.INVOICE_CREATED
    generation.provider_completed_at = now
    generation.save(
        update_fields=[
            "provider_invoice_id", "state", "provider_completed_at", "updated_at",
        ]
    )
    event_state = dict(attempt.event_state or {})
    event_state.pop("invoice_creation_ambiguous", None)
    attempt.monobank_invoice_id = generation.provider_invoice_id
    attempt.invoice_url = str(invoice_url)[:600]
    attempt.invoice_payload = {
        "request": invoice_payload,
        "create": creation,
    }
    attempt.invoice_expires_at = generation.expires_at
    attempt.event_state = event_state
    attempt.status = PaymentAttempt.Status.PROCESSING
    attempt.last_status_at = now
    attempt.save(
        update_fields=[
            "monobank_invoice_id", "invoice_url", "invoice_payload",
            "invoice_expires_at", "event_state", "status", "last_status_at",
            "updated",
        ]
    )
    proposal.current_invoice_generation = generation
    proposal.payment_attempt = attempt
    proposal.status = proposal.Status.INVOICE_CREATED
    proposal.save(
        update_fields=[
            "current_invoice_generation", "payment_attempt", "status", "updated_at",
        ]
    )
    _event(
        generation,
        "provider_succeeded",
        generation.provider_invoice_id,
        payload={"provider_invoice_id": generation.provider_invoice_id},
    )
    return proposal, generation, attempt, True


@transaction.atomic
def _persist_provider_failure(attempt_id, *, ambiguous, reason):
    graph = _lock_generation_graph(attempt_id)
    if graph is None:
        return
    deal, proposal, generation, attempt = graph
    now = timezone.now()
    if (
        attempt.order_id
        or attempt.checkout_winner_claimed
        or attempt.status in {
            PaymentAttempt.Status.PAID,
            PaymentAttempt.Status.PREPAID,
            PaymentAttempt.Status.CONVERTED,
        }
        or deal.order_id
        or proposal.winner_invoice_generation_id
        or proposal.status == proposal.Status.PAID
        or generation.winner_slot == 1
        or generation.state in {
            generation.State.WINNER_CLAIMED,
            generation.State.PAID_WINNER,
            generation.State.RESOURCE_REVIEW,
            generation.State.LATE_PAID_REVIEW,
        }
    ):
        _event(
            generation,
            "provider_ambiguous" if ambiguous else "provider_failed",
            f"ignored-after-winner:{attempt.pk}",
            payload={
                "attempt_reference": attempt.reference,
                "ignored_late_transport_outcome": True,
                "transport_ambiguous": bool(ambiguous),
                "reason_code": type(reason).__name__ if not isinstance(reason, str) else "transport_error",
                "winner_generation_id": proposal.winner_invoice_generation_id,
                "order_id": attempt.order_id or deal.order_id,
            },
        )
        _queue_ambiguity_review(
            proposal,
            generation,
            attempt,
            reason="ignored_transport_after_winner",
        )
        return "ignored_after_winner"
    if ambiguous:
        generation.state = generation.State.PROVIDER_AMBIGUOUS
        generation.review_reason = "provider_creation_ambiguous"
        generation.ambiguity_review_due_at = generation.expires_at + timedelta(
            seconds=AMBIGUITY_GRACE_SECONDS
        )
        generation.save(update_fields=[
            "state", "review_reason", "ambiguity_review_due_at", "updated_at",
        ])
        event_state = dict(attempt.event_state or {})
        event_state["invoice_creation_ambiguous"] = True
        attempt.event_state = event_state
        attempt.error_reason = f"invoice_creation_ambiguous:{reason}"[:500]
        attempt.last_status_at = now
        attempt.save(
            update_fields=["event_state", "error_reason", "last_status_at", "updated"]
        )
        _event(
            generation,
            "provider_ambiguous",
            str(attempt.pk),
            payload={
                "attempt_reference": attempt.reference,
                "request_digest": generation.provider_request_digest,
                "provider_call_token_digest": hashlib.sha256(
                    generation.provider_call_token.encode()
                ).hexdigest(),
                "provider_validity_seconds": PROVIDER_VALIDITY_SECONDS,
                "generation_expires_at": generation.expires_at.isoformat(),
                "review_due_at": generation.ambiguity_review_due_at.isoformat(),
                "provider_invoice_id_known": bool(generation.provider_invoice_id),
            },
        )
        _queue_ambiguity_review(
            proposal,
            generation,
            attempt,
            reason="provider_creation_ambiguous",
        )
        return
    _terminalize_locked_generation(
        proposal,
        generation,
        attempt,
        state=generation.State.FAILED,
        attempt_status=PaymentAttempt.Status.FAILED,
        reason="invoice_creation_failed",
        now=now,
    )
    _event(generation, "provider_failed", str(attempt.pk))


def create_or_reuse_generation_invoice(
    proposal,
    *,
    request,
    payload,
    grant_id="",
):
    """Create one 25-minute invoice; provider I/O is outside transactions."""
    locked, generation, attempt, values, reused = _prepare_generation(
        proposal,
        request=request,
        payload=payload,
        grant_id=grant_id,
    )
    if reused:
        if attempt.invoice_url:
            _send_add_payment_info_if_missing(attempt, request)
            return attempt, attempt.invoice_url, True
        raise CheckoutPaymentError(
            "in_progress", "Платіж уже створюється. Зачекайте кілька секунд."
        )
    invoice_payload = _invoice_payload(
        request,
        attempt,
        locked,
        payment_amount=attempt.payment_amount,
        promo_discount=values["promo_discount"],
    )
    invoice_payload["validity"] = int(PROVIDER_VALIDITY_SECONDS)
    _persist_provider_dispatch_evidence(attempt.pk, invoice_payload)
    try:
        from storefront.views.monobank import _monobank_api_request

        creation = _monobank_api_request(
            "POST",
            "/api/merchant/invoice/create",
            json_payload=invoice_payload,
        )
        result = (
            creation.get("result")
            if isinstance(creation.get("result"), dict)
            else creation
        )
        invoice_id = result.get("invoiceId") or result.get("invoice_id")
        invoice_url = result.get("pageUrl") or result.get("invoiceUrl")
        if not invoice_id or not invoice_url:
            raise RuntimeError("Monobank returned an invalid invoice")
        locked, generation, attempt, accepted = _persist_provider_success(
            attempt.pk,
            invoice_id=invoice_id,
            invoice_url=invoice_url,
            invoice_payload=invoice_payload,
            creation=creation,
        )
        if not accepted:
            raise CheckoutPaymentError(
                "provider_ambiguous",
                "Late provider result requires manager reconciliation.",
            )
    except CheckoutPaymentError:
        raise
    except Exception as exc:
        ambiguous = bool(getattr(exc, "ambiguous", True))
        _persist_provider_failure(
            attempt.pk,
            ambiguous=ambiguous,
            reason=str(exc),
        )
        if ambiguous:
            raise CheckoutPaymentError(
                "provider_ambiguous",
                "Не вдалося підтвердити відповідь банку. Не повторюйте оплату.",
            ) from exc
        raise CheckoutPaymentError(
            "provider_error",
            "Не вдалося створити платіж. Спробуйте ще раз пізніше.",
        ) from exc

    _send_add_payment_info_if_missing(attempt, request)
    _enqueue_invoice_created_alert_best_effort(locked, attempt)
    request.session["monobank_invoice_id"] = str(invoice_id)
    request.session["monobank_pending_attempt_id"] = attempt.pk
    request.session["monobank_attempt_id"] = attempt.pk
    request.session["ig_checkout_proposal_id"] = str(locked.public_id)
    request.session.modified = True
    return attempt, attempt.invoice_url, False


def terminalize_generation_attempt(
    attempt_id,
    *,
    terminal_status,
    reason,
    now=None,
    require_due=False,
    terminal_source="",
    provider_status="",
    provider_payload=None,
):
    """Generation-scoped local/provider terminalization without proposal death."""
    now = now or timezone.now()
    with transaction.atomic():
        graph = _lock_generation_graph(attempt_id)
        if graph is None:
            return None
        deal, proposal, generation, attempt = graph
        if (
            attempt.order_id
            or generation.winner_slot == 1
            or deal.order_id
            or proposal.winner_invoice_generation_id
            or generation.state in {
                generation.State.WINNER_CLAIMED,
                generation.State.PAID_WINNER,
                generation.State.LATE_PAID_REVIEW,
                generation.State.RESOURCE_REVIEW,
            }
        ):
            return {"outcome": "protected_payment", "attempt_id": attempt.pk}
        if require_due and generation.expires_at > now:
            return {"outcome": "not_due", "attempt_id": attempt.pk}
        if (
            str(terminal_source or "") == "checkout_session_reset"
            and generation.state == generation.State.INVOICE_CREATED
            and _provider_identity_matches(generation, attempt)
        ):
            return {
                "outcome": "provider_active",
                "attempt_id": attempt.pk,
                "provider_invoice_id": generation.provider_invoice_id,
            }
        provider_terminal_requested = bool(str(provider_status or "").strip())
        provider_terminal_proof = _build_provider_terminal_proof(
            generation,
            attempt,
            provider_status=provider_status,
            provider_payload=provider_payload,
            source=terminal_source,
            now=now,
        )
        if provider_terminal_proof is not None:
            event_state = dict(attempt.event_state or {})
            event_state["provider_terminal"] = provider_terminal_proof
            attempt.event_state = event_state
            attempt.save(update_fields=["event_state", "updated"])
        cancelled_pre_dispatch = bool(
            terminal_status == PaymentAttempt.Status.CANCELLED
            and str(terminal_source or "") == "checkout_session_reset"
            and generation.state in {
                generation.State.PLANNED,
                generation.State.PROVIDER_INFLIGHT,
            }
            and bool(generation.provider_call_token)
            and not generation.provider_request_digest
            and not generation.provider_invoice_id
            and not attempt.monobank_invoice_id
            and not attempt.invoice_url
            and not attempt.invoice_payload
            and not attempt.payment_history
            and not (attempt.event_state or {}).get("invoice_creation_ambiguous")
        )
        if cancelled_pre_dispatch:
            event_state = dict(attempt.event_state or {})
            event_state["provider_boundary"] = {
                "state": "cancelled_pre_dispatch",
                "generation_id": generation.pk,
                "provider_call_token_digest": hashlib.sha256(
                    str(generation.provider_call_token or "").encode()
                ).hexdigest(),
                "source": "checkout_session_reset",
                "observed_at": now.isoformat(),
            }
            attempt.event_state = event_state
            attempt.save(update_fields=["event_state", "updated"])
            _event(
                generation,
                "terminalized",
                f"cancelled-pre-dispatch:{attempt.pk}",
                payload={
                    "provider_boundary_state": "cancelled_pre_dispatch",
                    "attempt_reference": attempt.reference,
                },
            )
        elif (
            _provider_boundary_uncertain(generation, attempt)
            or (provider_terminal_requested and provider_terminal_proof is None)
        ):
            if generation.state not in {
                generation.State.AMBIGUITY_REVIEW,
                generation.State.LATE_PROVIDER_REVIEW,
            }:
                generation.state = generation.State.PROVIDER_AMBIGUOUS
            generation.review_reason = "terminalization_provider_identity_unknown"
            generation.ambiguity_review_due_at = (
                generation.ambiguity_review_due_at
                or generation.expires_at + timedelta(
                    seconds=AMBIGUITY_GRACE_SECONDS
                )
            )
            generation.save(update_fields=[
                "state", "review_reason", "ambiguity_review_due_at", "updated_at",
            ])
            event_state = dict(attempt.event_state or {})
            event_state["invoice_creation_ambiguous"] = True
            attempt.event_state = event_state
            attempt.status = PaymentAttempt.Status.PROCESSING
            attempt.error_reason = "terminalization_provider_identity_unknown"
            attempt.last_status_at = now
            attempt.save(update_fields=[
                "status", "event_state", "error_reason", "last_status_at", "updated",
            ])
            proposal.status = proposal.Status.MANAGER_REVIEW
            proposal.save(update_fields=["status", "updated_at"])
            _event(
                generation,
                "provider_ambiguous",
                f"terminalize:{attempt.pk}",
                payload={
                    "attempt_reference": attempt.reference,
                    "provider_invoice_id_known": bool(
                        generation.provider_invoice_id
                    ),
                    "review_due_at": (
                        generation.ambiguity_review_due_at.isoformat()
                    ),
                },
            )
            _queue_ambiguity_review(
                proposal,
                generation,
                attempt,
                reason="terminalization_provider_identity_unknown",
            )
            return {
                "outcome": "provider_ambiguous",
                "attempt_id": attempt.pk,
                "released_inventory": 0,
                "released_promo": False,
            }
        state = (
            generation.State.EXPIRED
            if terminal_status == PaymentAttempt.Status.EXPIRED
            else generation.State.CANCELLED
            if terminal_status == PaymentAttempt.Status.CANCELLED
            else generation.State.FAILED
        )
        released_inventory, released_promo = _terminalize_locked_generation(
            proposal,
            generation,
            attempt,
            state=state,
            attempt_status=terminal_status,
            reason=reason,
            now=now,
        )
        return {
            "outcome": "terminalized",
            "attempt_id": attempt.pk,
            "released_inventory": released_inventory,
            "released_promo": released_promo,
        }


def _paid_amount(attempt, payload):
    from orders.payment_attempts import _paid_amount_from_payload

    return _paid_amount_from_payload(attempt, payload)


def _persist_paid_without_order(attempt, *, payload, source, now):
    from orders.payment_attempts import _append_history

    _append_history(attempt, "success", payload, source)
    attempt.status = (
        PaymentAttempt.Status.PREPAID
        if attempt.pay_type in {
            PaymentAttempt.PayType.PREPAYMENT,
            PaymentAttempt.PayType.PREPAY_200,
        }
        else PaymentAttempt.Status.PAID
    )
    attempt.paid_amount = _paid_amount(attempt, payload)
    attempt.last_status_at = now
    attempt.save(update_fields=[
        "status", "paid_amount", "payment_history", "last_status_at", "updated",
    ])


def _queue_paid_review(proposal, generation, attempt, *, reason):
    try:
        from management.services.instagram_bot import notify_manager

        notify_manager(
            (
                f"⚠️ IG checkout: оплата generation #{generation.generation} "
                f"для пропозиції #{proposal.pk} потребує перевірки ({reason})."
            ),
            dedupe_key=f"ig-checkout-generation-review:{generation.pk}:{reason}",
            event_type="ig_checkout_generation_payment_review",
            client=proposal.client,
            deliver_immediately=False,
        )
    except Exception:
        return


def _queue_ambiguity_review(proposal, generation, attempt, *, reason):
    try:
        from management.services.instagram_bot import notify_manager

        notify_manager(
            (
                f"⚠️ IG checkout: не визначено результат створення рахунку "
                f"generation #{generation.generation}, proposal #{proposal.pk}, "
                f"attempt {attempt.reference}. Автоповтор заборонено; потрібна звірка."
            ),
            dedupe_key=f"ig-checkout-generation-ambiguity:{generation.pk}:{reason}",
            event_type="ig_checkout_generation_ambiguity_review",
            client=proposal.client,
            deliver_immediately=False,
        )
    except Exception:
        return


def _mark_losing_paid_generation(
    deal,
    proposal,
    generation,
    attempt,
    *,
    payload,
    source,
    now,
):
    if generation.state != generation.State.LATE_PAID_REVIEW:
        _persist_paid_without_order(
            attempt,
            payload=payload,
            source=source,
            now=now,
        )
        from management.services.ig_checkout_payment import (
            project_verified_payment_without_order,
        )

        project_verified_payment_without_order(
            attempt=attempt,
            deal=deal,
            proposal=proposal,
            verified_at=now,
        )
        generation.state = generation.State.LATE_PAID_REVIEW
        generation.active_slot = None
        generation.terminal_at = now
        generation.paid_at = now
        generation.review_reason = "paid_losing_generation_refund_review"
        generation.save(update_fields=[
            "state", "active_slot", "terminal_at", "paid_at", "review_reason",
            "updated_at",
        ])
        release_generation_inventory(
            generation,
            reason="paid_losing_generation_refund_review",
        )
        release_attempt_promo(
            attempt,
            reason="paid_losing_generation_refund_review",
        )
        _event(
            generation,
            "loser_paid_review",
            str(attempt.pk),
            payload={
                "winner_generation_id": proposal.winner_invoice_generation_id,
                "requires_refund_review": True,
            },
        )
    _queue_paid_review(
        proposal,
        generation,
        attempt,
        reason="paid_losing_generation_refund_review",
    )


def _mark_obsolete_paid_review(
    deal,
    proposal,
    generation,
    attempt,
    *,
    payload,
    source,
    now,
    reason,
):
    resource_conflict = generation.state in {
        generation.State.FAILED,
        generation.State.EXPIRED,
        generation.State.CANCELLED,
    } or generation.inventory_reservations.filter(
        state__in=["released", "overbooked_review"]
    ).exists()
    _mark_losing_paid_generation(
        deal,
        proposal,
        generation,
        attempt,
        payload=payload,
        source=source,
        now=now,
    )
    if resource_conflict:
        generation.state = generation.State.RESOURCE_REVIEW
    generation.review_reason = str(reason or "obsolete_paid_generation")[:96]
    generation.save(update_fields=["state", "review_reason", "updated_at"])
    if proposal.winner_invoice_generation_id is None:
        _gate_proposal_for_late_provider_review(
            proposal,
            generation,
            now=now,
        )


def _mark_resource_review(
    deal,
    proposal,
    generation,
    attempt,
    *,
    payload,
    source,
    now,
    reason="paid_generation_resource_unavailable",
):
    _persist_paid_without_order(
        attempt,
        payload=payload,
        source=source,
        now=now,
    )
    from management.services.ig_checkout_payment import (
        project_verified_payment_without_order,
    )

    project_verified_payment_without_order(
        attempt=attempt,
        deal=deal,
        proposal=proposal,
        verified_at=now,
    )
    generation.state = generation.State.RESOURCE_REVIEW
    generation.active_slot = None
    generation.paid_at = now
    generation.terminal_at = now
    generation.review_reason = str(reason or "paid_generation_resource_unavailable")[:96]
    generation.save(update_fields=[
        "state", "active_slot", "paid_at", "terminal_at", "review_reason",
        "updated_at",
    ])
    proposal.status = proposal.Status.MANAGER_REVIEW
    proposal.paid_at = proposal.paid_at or now
    proposal.save(update_fields=["status", "paid_at", "updated_at"])
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
    deal.paid_amount = attempt.paid_amount or attempt.payment_amount
    deal.paid_at = deal.paid_at or now
    deal.payment_truth_updated_at = now
    deal.save(update_fields=[
        "status", "payment_status", "payment_truth", "paid_amount", "paid_at",
        "payment_truth_updated_at", "updated_at",
    ])
    _event(
        generation,
        "resource_review",
        str(attempt.pk),
        payload={
            "requires_stock_or_refund_review": True,
            "reason_code": generation.review_reason,
        },
    )
    _queue_paid_review(
        proposal,
        generation,
        attempt,
        reason=generation.review_reason,
    )


def _retire_competing_generations(proposal, winner, *, now):
    siblings = getattr(winner, "_locked_sibling_generations", ())
    for row in siblings:
        if row.pk == winner.pk or row.active_slot != 1 or not row.payment_attempt_id:
            continue
        attempt = PaymentAttempt.objects.select_for_update().get(
            pk=row.payment_attempt_id
        )
        row.state = row.State.CANCELLED
        row.active_slot = None
        row.terminal_at = now
        row.review_reason = "another_generation_claimed_winner"
        row.save(update_fields=[
            "state", "active_slot", "terminal_at", "review_reason", "updated_at",
        ])
        if attempt.order_id is None and not attempt.checkout_winner_claimed:
            attempt.status = PaymentAttempt.Status.CANCELLED
            attempt.error_reason = "another_generation_claimed_winner"
            attempt.last_status_at = now
            attempt.save(update_fields=[
                "status", "error_reason", "last_status_at", "updated",
            ])
        release_generation_inventory(
            row,
            reason="another_generation_claimed_winner",
        )
        release_attempt_promo(
            attempt,
            reason="another_generation_claimed_winner",
        )
        _event(
            row,
            "terminalized",
            f"winner:{winner.pk}",
            payload={"winner_generation_id": winner.pk},
        )


def _gate_proposal_for_late_provider_review(proposal, obsolete, *, now):
    """Cancel every other payable generation without repointing the obsolete one."""
    siblings = getattr(obsolete, "_locked_sibling_generations", ())
    for row in siblings:
        if row.pk == obsolete.pk or row.active_slot != 1 or not row.payment_attempt_id:
            continue
        attempt = PaymentAttempt.objects.select_for_update().get(pk=row.payment_attempt_id)
        row.state = row.State.CANCELLED
        row.active_slot = None
        row.terminal_at = now
        row.review_reason = "late_provider_result_proposal_gate"
        row.save(update_fields=[
            "state", "active_slot", "terminal_at", "review_reason", "updated_at",
        ])
        if attempt.order_id is None and not attempt.checkout_winner_claimed:
            attempt.status = PaymentAttempt.Status.CANCELLED
            attempt.error_reason = "late_provider_result_proposal_gate"
            attempt.last_status_at = now
            attempt.save(update_fields=[
                "status", "error_reason", "last_status_at", "updated",
            ])
        release_generation_inventory(
            row,
            reason="late_provider_result_proposal_gate",
        )
        release_attempt_promo(
            attempt,
            reason="late_provider_result_proposal_gate",
        )
        _event(
            row,
            "terminalized",
            f"late-provider-gate:{obsolete.pk}",
            payload={"obsolete_generation_id": obsolete.pk},
        )
    proposal.current_invoice_generation = None
    proposal.payment_attempt = None
    proposal.details_locked_at = None
    proposal.status = proposal.Status.MANAGER_REVIEW
    proposal.save(update_fields=[
        "current_invoice_generation", "payment_attempt", "details_locked_at",
        "status", "updated_at",
    ])


def apply_verified_generation_payment(
    attempt_id,
    *,
    payload=None,
    source="provider_pull",
):
    """Claim one paid winner before creating any Order; loser stays review-only."""
    from management.models import IgCheckoutInventoryReservation
    from management.services.ig_checkout_payment import bind_verified_payment
    from management.services.ig_inventory import commit_generation_inventory
    from orders.payment_attempts import (
        PaymentAttemptConversionError,
        materialize_payment_attempt,
    )

    with transaction.atomic():
        graph = _lock_generation_graph(attempt_id)
        if graph is None:
            return None, False
        deal, proposal, generation, attempt = graph
        if attempt.order_id:
            return attempt.order, False
        amount_valid, amount_reason = _exact_provider_paid_amount(attempt, payload)
        if not amount_valid:
            _record_amount_reconciliation(
                generation,
                attempt,
                reason=amount_reason,
            )
            return None, False
        if (attempt.event_state or {}).get("payment_amount_reconciliation_pending"):
            event_state = dict(attempt.event_state or {})
            event_state.pop("payment_amount_reconciliation_pending", None)
            event_state.pop("payment_amount_reconciliation", None)
            attempt.event_state = event_state
            attempt.save(update_fields=["event_state", "updated"])
        if generation.state == generation.State.RESOURCE_REVIEW:
            _queue_paid_review(
                proposal,
                generation,
                attempt,
                reason=generation.review_reason,
            )
            return None, False
        if (
            proposal.winner_invoice_generation_id
            and proposal.winner_invoice_generation_id != generation.pk
        ):
            _mark_losing_paid_generation(
                deal,
                proposal,
                generation,
                attempt,
                payload=payload,
                source=source,
                now=timezone.now(),
            )
            return None, False

        now = timezone.now()
        obsolete_reason = ""
        if deal.order_id:
            obsolete_reason = "deal_order_already_exists"
        elif getattr(proposal.commercial_episode, "intended_order_id", None):
            obsolete_reason = "episode_order_already_exists"
        elif deal.active_checkout_proposal_id != proposal.pk:
            obsolete_reason = "proposal_not_active_for_deal"
        elif proposal.current_invoice_generation_id != generation.pk:
            obsolete_reason = "generation_not_current"
        elif proposal.status not in {
            proposal.Status.DETAILS_LOCKED,
            proposal.Status.INVOICE_CREATED,
        }:
            obsolete_reason = f"proposal_{proposal.status or 'unknown'}"
        elif generation.state in {
            generation.State.LATE_PROVIDER_REVIEW,
            generation.State.AMBIGUITY_REVIEW,
        }:
            obsolete_reason = f"generation_{generation.state}"
        if obsolete_reason:
            _mark_obsolete_paid_review(
                deal,
                proposal,
                generation,
                attempt,
                payload=payload,
                source=source,
                now=now,
                reason=obsolete_reason,
            )
            return None, False

        if not proposal.winner_invoice_generation_id:
            generation.winner_slot = 1
            generation.active_slot = None
            generation.state = generation.State.WINNER_CLAIMED
            generation.winner_claimed_at = now
            generation.save(update_fields=[
                "winner_slot", "active_slot", "state", "winner_claimed_at",
                "updated_at",
            ])
            proposal.winner_invoice_generation = generation
            proposal.current_invoice_generation = generation
            proposal.payment_attempt = attempt
            proposal.save(update_fields=[
                "winner_invoice_generation", "current_invoice_generation",
                "payment_attempt", "updated_at",
            ])
            attempt.checkout_winner_claimed = True
            attempt.status = PaymentAttempt.Status.PROCESSING
            attempt.error_reason = ""
            attempt.last_status_at = now
            attempt.save(update_fields=[
                "checkout_winner_claimed", "status", "error_reason",
                "last_status_at", "updated",
            ])
            _event(generation, "winner_claimed", str(attempt.pk))
            _retire_competing_generations(
                proposal,
                generation,
                now=now,
            )

        try:
            commit_generation_inventory(
                generation,
                paid_at=now,
            )
        except (ValueError, IntegrityError) as exc:
            IgCheckoutInventoryReservation.objects.filter(
                invoice_generation_id=generation.pk,
                state__in=[
                    IgCheckoutInventoryReservation.State.ACTIVE,
                    IgCheckoutInventoryReservation.State.RELEASED,
                ],
            ).update(
                state=IgCheckoutInventoryReservation.State.OVERBOOKED_REVIEW,
                release_reason=f"paid_resource_error:{type(exc).__name__}"[:128],
                updated_at=now,
            )
        if IgCheckoutInventoryReservation.objects.filter(
            invoice_generation_id=generation.pk,
            state=IgCheckoutInventoryReservation.State.OVERBOOKED_REVIEW,
        ).exists():
            _mark_resource_review(
                deal,
                proposal,
                generation,
                attempt,
                payload=payload,
                source=source,
                now=now,
            )
            return None, False

        try:
            order, created = materialize_payment_attempt(
                attempt.pk,
                status="success",
                payload=payload,
                source=source,
            )
        except PaymentAttemptConversionError as exc:
            if bool(getattr(exc, "retryable", False)):
                event_state = dict(attempt.event_state or {})
                marker = str(getattr(exc, "marker", "") or "")[:64]
                if marker:
                    event_state[marker] = True
                promo_retry_count = 0
                if marker == "promo_consumption_pending":
                    try:
                        prior_promo_retry_count = int(
                            event_state.get("promo_consumption_retry_count") or 0
                        )
                    except (TypeError, ValueError, OverflowError):
                        prior_promo_retry_count = 0
                    promo_retry_count = max(
                        0,
                        prior_promo_retry_count,
                    ) + 1
                    event_state["promo_consumption_retry_count"] = promo_retry_count
                    if promo_retry_count >= 2:
                        event_state.pop("promo_consumption_pending", None)
                        event_state["promo_consumption_manual_review"] = True
                attempt.status = PaymentAttempt.Status.PROCESSING
                attempt.event_state = event_state
                attempt.error_reason = str(exc)[:500]
                attempt.last_status_at = now
                attempt.save(update_fields=[
                    "status", "event_state", "error_reason", "last_status_at",
                    "updated",
                ])
                if marker == "promo_consumption_pending" and promo_retry_count >= 2:
                    _mark_resource_review(
                        deal,
                        proposal,
                        generation,
                        attempt,
                        payload=payload,
                        source=source,
                        now=now,
                        reason="promo_consumption_persistence_failed",
                    )
                return None, False
            _mark_resource_review(
                deal,
                proposal,
                generation,
                attempt,
                payload=payload,
                source=source,
                now=now,
            )
            return None, False
        bind_verified_payment(attempt.pk, order)
        generation.refresh_from_db()
        return order, created


def apply_generation_provider_status(
    attempt_id,
    status,
    *,
    payload=None,
    source="provider_pull",
):
    normalized = str(status or "").strip().casefold()
    if normalized == "success":
        return apply_verified_generation_payment(
            attempt_id,
            payload=payload,
            source=source,
        )
    terminal = {
        "failure": PaymentAttempt.Status.FAILED,
        "rejected": PaymentAttempt.Status.FAILED,
        "reversed": PaymentAttempt.Status.FAILED,
        "cancelled": PaymentAttempt.Status.CANCELLED,
        "canceled": PaymentAttempt.Status.CANCELLED,
        "expired": PaymentAttempt.Status.EXPIRED,
    }.get(normalized)
    if terminal:
        terminalize_generation_attempt(
            attempt_id,
            terminal_status=terminal,
            reason=f"provider_{normalized}",
            require_due=False,
            terminal_source=source,
            provider_status=normalized,
            provider_payload=payload,
        )
    elif normalized in {"created", "processing", "hold"}:
        with transaction.atomic():
            graph = _lock_generation_graph(attempt_id)
            if graph is not None:
                _deal, _proposal, generation, attempt = graph
                if (
                    attempt.order_id is None
                    and not attempt.checkout_winner_claimed
                    and attempt.status in {
                        PaymentAttempt.Status.INITIATED,
                        PaymentAttempt.Status.PROCESSING,
                    }
                    and generation.state in {
                        generation.State.PROVIDER_INFLIGHT,
                        generation.State.INVOICE_CREATED,
                        generation.State.PROVIDER_AMBIGUOUS,
                        generation.State.LATE_PROVIDER_REVIEW,
                    }
                ):
                    reconciliation_reason = str(
                        (payload or {}).get("_twc_reconciliation_reason")
                        if isinstance(payload, dict)
                        else ""
                    )[:64]
                    if reconciliation_reason:
                        _record_amount_reconciliation(
                            generation,
                            attempt,
                            reason=reconciliation_reason,
                        )
                    attempt.status = PaymentAttempt.Status.PROCESSING
                    attempt.last_status_at = timezone.now()
                    attempt.save(update_fields=["status", "last_status_at", "updated"])
    return None, False


def resolve_due_generation_ambiguities(*, now=None, limit=100, dry_run=False):
    """Bound provider-create ambiguity after validity+grace without blind retry."""
    from management.models import IgCheckoutInvoiceGeneration

    now = now or timezone.now()
    ids = list(
        IgCheckoutInvoiceGeneration.objects.filter(
            state__in=[
                IgCheckoutInvoiceGeneration.State.PROVIDER_AMBIGUOUS,
                IgCheckoutInvoiceGeneration.State.LATE_PROVIDER_REVIEW,
            ],
            ambiguity_review_due_at__isnull=False,
            ambiguity_review_due_at__lte=now,
        )
        .order_by("ambiguity_review_due_at", "pk")
        .values_list("payment_attempt_id", flat=True)[: max(1, min(int(limit), 500))]
    )
    ids = [value for value in ids if value]
    result = {
        "due": len(ids),
        "resolved": len(ids) if dry_run else 0,
        "released_inventory": 0,
        "released_promos": 0,
        "errors": 0,
    }
    if dry_run:
        return result
    for attempt_id in ids:
        try:
            with transaction.atomic():
                graph = _lock_generation_graph(attempt_id)
                if graph is None:
                    continue
                _deal, proposal, generation, attempt = graph
                if generation.state not in {
                    generation.State.PROVIDER_AMBIGUOUS,
                    generation.State.LATE_PROVIDER_REVIEW,
                } or (
                    not generation.ambiguity_review_due_at
                    or generation.ambiguity_review_due_at > now
                ):
                    continue
                if attempt.order_id or generation.winner_slot == 1:
                    generation.ambiguity_resolved_at = now
                    generation.save(update_fields=[
                        "ambiguity_resolved_at", "updated_at",
                    ])
                    result["resolved"] += 1
                    continue
                generation.state = generation.State.AMBIGUITY_REVIEW
                generation.active_slot = None
                generation.terminal_at = now
                generation.ambiguity_resolved_at = now
                generation.review_reason = "provider_create_ambiguity_expired"
                generation.save(update_fields=[
                    "state", "active_slot", "terminal_at",
                    "ambiguity_resolved_at", "review_reason", "updated_at",
                ])
                if attempt.status in {
                    PaymentAttempt.Status.INITIATED,
                    PaymentAttempt.Status.PROCESSING,
                }:
                    attempt.status = PaymentAttempt.Status.FAILED
                    attempt.error_reason = "provider_create_ambiguity_review"
                    attempt.last_status_at = now
                    attempt.save(update_fields=[
                        "status", "error_reason", "last_status_at", "updated",
                    ])
                result["released_inventory"] += int(
                    release_generation_inventory(
                        generation,
                        reason="provider_create_ambiguity_expired",
                    )
                    or 0
                )
                result["released_promos"] += int(
                    bool(
                        release_attempt_promo(
                            attempt,
                            reason="provider_create_ambiguity_expired",
                        )
                    )
                )
                proposal.status = proposal.Status.MANAGER_REVIEW
                proposal.save(update_fields=["status", "updated_at"])
                _event(
                    generation,
                    "provider_ambiguous",
                    f"bounded-review:{attempt.pk}",
                    payload={
                        "attempt_reference": attempt.reference,
                        "request_digest": generation.provider_request_digest,
                        "provider_invoice_id": generation.provider_invoice_id or "",
                        "resources_released": True,
                        "blind_retry_allowed": False,
                    },
                )
                _queue_ambiguity_review(
                    proposal,
                    generation,
                    attempt,
                    reason="bounded_ambiguity_review",
                )
                result["resolved"] += 1
        except Exception:
            result["errors"] += 1
    return result


def expire_due_v2_proposals(*, now=None, limit=100, dry_run=False):
    """Expire only the 12-hour offer boundary; generations stay scoped."""
    from management.models import (
        IgCheckoutInvoiceGeneration,
        IgCheckoutProposal,
        IgDeal,
    )

    now = now or timezone.now()
    ids = list(
        IgCheckoutProposal.objects.filter(
            assisted_checkout_v2=True,
            expires_at__lte=now,
            winner_invoice_generation__isnull=True,
            status__in=[
                IgCheckoutProposal.Status.READY,
                IgCheckoutProposal.Status.VIEWED,
                IgCheckoutProposal.Status.DETAILS_LOCKED,
                IgCheckoutProposal.Status.INVOICE_CREATED,
            ],
        )
        .order_by("expires_at", "pk")
        .values_list("pk", flat=True)[: max(1, min(int(limit), 500))]
    )
    result = {"due": len(ids), "expired": len(ids) if dry_run else 0, "errors": 0}
    if dry_run:
        return result
    for proposal_id in ids:
        try:
            with transaction.atomic():
                locator = IgCheckoutProposal.objects.filter(pk=proposal_id).values(
                    "deal_id"
                ).first()
                if locator is None:
                    continue
                deal = IgDeal.objects.select_for_update().get(pk=locator["deal_id"])
                proposal = IgCheckoutProposal.objects.select_for_update().get(
                    pk=proposal_id,
                    deal_id=deal.pk,
                    assisted_checkout_v2=True,
                )
                if (
                    proposal.expires_at > now
                    or proposal.winner_invoice_generation_id
                ):
                    continue
                if proposal.current_invoice_generation_id:
                    generation = IgCheckoutInvoiceGeneration.objects.select_for_update().get(
                        pk=proposal.current_invoice_generation_id,
                        proposal_id=proposal.pk,
                    )
                    if generation.state in {
                        generation.State.PLANNED,
                        generation.State.PROVIDER_INFLIGHT,
                        generation.State.PROVIDER_AMBIGUOUS,
                        generation.State.LATE_PROVIDER_REVIEW,
                        generation.State.AMBIGUITY_REVIEW,
                    }:
                        proposal.status = proposal.Status.MANAGER_REVIEW
                        proposal.save(update_fields=["status", "updated_at"])
                        if generation.payment_attempt_id:
                            attempt = PaymentAttempt.objects.select_for_update().get(
                                pk=generation.payment_attempt_id
                            )
                            _queue_ambiguity_review(
                                proposal,
                                generation,
                                attempt,
                                reason="proposal_expiry_ambiguity_visible",
                            )
                        result["expired"] += 1
                        continue
                    if (
                        generation.payment_attempt_id
                        and generation.expires_at <= now
                    ):
                        attempt = PaymentAttempt.objects.select_for_update().get(
                            pk=generation.payment_attempt_id
                        )
                        if generation.state != generation.State.PROVIDER_AMBIGUOUS:
                            _terminalize_locked_generation(
                                proposal,
                                generation,
                                attempt,
                                state=generation.State.EXPIRED,
                                attempt_status=PaymentAttempt.Status.EXPIRED,
                                reason="proposal_12h_expired",
                                now=now,
                            )
                proposal.status = proposal.Status.EXPIRED
                proposal.save(update_fields=["status", "updated_at"])
                if deal.active_checkout_proposal_id == proposal.pk:
                    deal.active_checkout_proposal = None
                    deal.save(update_fields=["active_checkout_proposal", "updated_at"])
                result["expired"] += 1
        except Exception:
            result["errors"] += 1
    return result
