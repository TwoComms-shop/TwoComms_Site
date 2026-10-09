"""Cross-channel promo reservation and consumption primitives.

Every account-scoped checkout serializes on the owning promo group (or on the
promo itself for an ungrouped one-time code). Production therefore requires
``storefront_promocodegroup`` to use InnoDB; migration 0087 enforces that.
"""

from dataclasses import dataclass
from decimal import Decimal
import secrets

from django.db import transaction
from django.utils import timezone


ACTIVE_RESERVATION_STATES = frozenset({"reserved", "consumed"})


class PromoReservationError(ValueError):
    def __init__(self, reason="invalid"):
        self.reason = str(reason or "invalid")
        super().__init__(self.reason)


@dataclass(frozen=True)
class PromoReservation:
    promo: object
    discount: Decimal
    event_state: dict


def _authenticated_user_id(user):
    if not user or not getattr(user, "is_authenticated", False):
        return None
    return user.pk


def _has_committed_usage(*, promo, group, user_id):
    from storefront.models import PromoCodeUsage

    usages = PromoCodeUsage.objects.filter(user_id=user_id)
    if group is not None and group.one_per_account:
        return usages.filter(group_id=group.pk).exists()
    if promo.one_time_per_user:
        return usages.filter(promo_code_id=promo.pk).exists()
    return False


def has_active_account_reservation(*, promo, group, user_id):
    """Check durable PaymentAttempt reservations across every checkout surface."""
    from orders.models import PaymentAttempt

    attempts = PaymentAttempt.objects.filter(user_id=user_id)
    if group is not None and group.one_per_account:
        attempts = attempts.filter(promo_code__group_id=group.pk)
    elif promo.one_time_per_user:
        attempts = attempts.filter(promo_code_id=promo.pk)
    else:
        return False
    for event_state in attempts.values_list("event_state", flat=True):
        reservation = dict((event_state or {}).get("promo_reservation") or {})
        if reservation.get("state") in ACTIVE_RESERVATION_STATES:
            return True
    return False


def _lock_promo(*, promo_id=None, code=None):
    from storefront.models import PromoCode, PromoCodeGroup

    locator = PromoCode.objects.all()
    if promo_id is not None:
        locator = locator.filter(pk=promo_id)
    else:
        locator = locator.filter(code__iexact=str(code or "").strip())
    identity = locator.values("pk", "group_id").first()
    if identity is None:
        raise PromoReservationError("invalid")

    group = None
    if identity["group_id"]:
        group = (
            PromoCodeGroup.objects.select_for_update()
            .filter(pk=identity["group_id"])
            .first()
        )
        if group is None or not group.is_active:
            raise PromoReservationError("inactive_group")

    promo = (
        PromoCode.objects.select_for_update()
        .select_related("group")
        .get(pk=identity["pk"])
    )
    if group is not None:
        promo.group = group
    return promo, group


@transaction.atomic
def reserve_promo_for_checkout(*, promo_id=None, code=None, user=None, total_amount):
    """Validate and reserve one promo before creating an external invoice."""
    from management.ig_bot_models import IgClient, IgUgcReward
    from orders.models import Order
    from reviews.models import Review, ReviewRewardUplift
    from storefront.models import PromoCode

    locator = PromoCode.objects.all()
    locator = (locator.filter(pk=promo_id) if promo_id is not None else
               locator.filter(code__iexact=str(code or "").strip()))
    located_promo_id = locator.values_list("pk", flat=True).first()
    reward_identity = IgUgcReward.objects.filter(promo_code_id=located_promo_id).values(
        "pk", "client_id", "order_id",
    ).first() if located_promo_id is not None else None
    if reward_identity is not None:
        # Match the grant/upgrade/privacy fence before taking the promo lock.
        # Moderators release Review before their on-commit uplift callback.
        client_id = reward_identity["client_id"]
        if client_id is None or not IgClient.objects.select_for_update().filter(pk=client_id).exists():
            raise PromoReservationError("ugc_capability_unverified")
        component = ReviewRewardUplift.objects.filter(reward_id=reward_identity["pk"]).first()
        source_order_id = reward_identity["order_id"] or (
            (component.proof_snapshot.get("source") or {}).get("order_id") if component else None
        )
        if source_order_id and not Order.objects.select_for_update().filter(pk=source_order_id).exists():
            raise PromoReservationError("ugc_capability_unverified")
        promo_id = located_promo_id
    promo, group = _lock_promo(promo_id=promo_id, code=code)
    if not promo.can_be_used():
        raise PromoReservationError("invalid")
    # The UGC bearer capability must remain evidenced for authenticated
    # checkout too; login cannot bypass a rejected review component.
    issued_reward = IgUgcReward.objects.select_for_update().filter(promo_code_id=promo.pk).first()
    if issued_reward is not None:
        if (reward_identity is None or issued_reward.pk != reward_identity["pk"]
                or issued_reward.client_id != reward_identity["client_id"]
                or issued_reward.order_id != reward_identity["order_id"]):
            raise PromoReservationError("ugc_capability_unverified")
        component = ReviewRewardUplift.objects.filter(reward_id=issued_reward.pk).first()
        if component is not None:
            if not Review.objects.select_for_update().filter(pk=component.review_id).exists():
                raise PromoReservationError("ugc_capability_unverified")
    requires_ugc_proof = issued_reward is not None or (
        promo.guest_redeemable and promo.discount_value == 15
    )
    if requires_ugc_proof and not promo.is_guest_ugc_capability():
        raise PromoReservationError("ugc_capability_unverified")

    user_id = _authenticated_user_id(user)
    if user_id is None and not promo.is_guest_ugc_capability():
        raise PromoReservationError("account_required")
    account_scoped = bool(
        promo.one_time_per_user
        or (group is not None and group.one_per_account)
    )
    if account_scoped and user_id is None:
        raise PromoReservationError("account_required")
    if account_scoped and (
        _has_committed_usage(promo=promo, group=group, user_id=user_id)
        or has_active_account_reservation(
            promo=promo,
            group=group,
            user_id=user_id,
        )
    ):
        raise PromoReservationError("already_reserved_or_used")

    total = Decimal(str(total_amount or 0))
    discount = min(Decimal(str(promo.calculate_discount(total))), total)
    if discount <= 0:
        raise PromoReservationError("not_applicable")

    promo.current_uses += 1
    promo.save(update_fields=["current_uses", "updated_at"])
    reservation_generation = secrets.token_urlsafe(32)
    guest_usage_id = None
    if user_id is None:
        from storefront.models import PromoCodeGuestUsage

        guest_usage = (
            PromoCodeGuestUsage.objects.select_for_update()
            .filter(promo_code_id=promo.pk)
            .first()
        )
        reservation_key = secrets.token_urlsafe(48)
        if guest_usage is None:
            guest_usage = PromoCodeGuestUsage.objects.create(
                promo_code=promo,
                reservation_key=reservation_key,
                metadata={"surface": "checkout", "bearer": True},
            )
        else:
            # A released invoice gives the same private bearer capability back
            # its one capacity slot.  The OneToOne ledger row is reused instead
            # of creating a second row and violating its uniqueness contract.
            if guest_usage.state != PromoCodeGuestUsage.State.RELEASED:
                raise PromoReservationError("already_reserved_or_used")
            guest_usage.reservation_key = reservation_key
            guest_usage.state = PromoCodeGuestUsage.State.RESERVED
            guest_usage.order = None
            guest_usage.reserved_at = timezone.now()
            guest_usage.consumed_at = None
            guest_usage.released_at = None
            guest_usage.metadata = {"surface": "checkout", "bearer": True}
            guest_usage.save(update_fields=[
                "reservation_key", "state", "order", "reserved_at",
                "consumed_at", "released_at", "metadata",
            ])
        guest_usage_id = guest_usage.pk
    return PromoReservation(
        promo=promo,
        discount=discount,
        event_state={
            "promo_reservation": {
                "promo_id": promo.pk,
                "group_id": promo.group_id,
                "state": "reserved",
                "capacity_reserved": True,
                "reserved_at": timezone.now().isoformat(),
                # Account-scoped reservations previously had no generation at
                # all. Keep an opaque token on every new attempt so a late
                # callback can never be confused with a reissued capacity slot.
                "reservation_generation": reservation_generation,
                "guest_usage_id": guest_usage_id,
                "guest_reservation_key": (
                    guest_usage.reservation_key if guest_usage_id else None
                ),
            }
        },
    )


@transaction.atomic
def record_immediate_promo_usage(*, promo_id, user, order):
    """Atomically reserve and consume a promo for an immediately placed order."""
    total = getattr(order, "total_sum", None) or getattr(order, "final_total", None) or 0
    reservation = reserve_promo_for_checkout(
        promo_id=promo_id,
        user=user,
        total_amount=total,
    )
    from storefront.models import PromoCodeUsage

    usage = PromoCodeUsage.objects.create(
        user=user,
        promo_code=reservation.promo,
        group=reservation.promo.group,
        order=order,
    )
    return usage


def consume_payment_attempt_promo(attempt, *, order):
    """Consume a reserved slot only after its usage row exists.

    Payment truth must still materialize if usage persistence has an operational
    failure. In that case the reservation deliberately stays ``reserved`` so a
    second code in the same account scope remains blocked until reconciliation.
    """
    promo = attempt.promo_code
    if promo is None:
        return True

    event_state = dict(attempt.event_state or {})
    reservation = dict(event_state.get("promo_reservation") or {})
    was_reserved = (
        reservation.get("state") == "reserved"
        and int(reservation.get("promo_id") or 0) == promo.pk
    )
    is_v2 = bool(attempt.checkout_series_key and attempt.checkout_generation)
    expected_generation = ""
    if is_v2:
        try:
            expected_generation = str(
                attempt.instagram_checkout_generation.promo_reservation_generation
                or ""
            )
        except Exception:
            expected_generation = ""
    actual_generation = str(reservation.get("reservation_generation") or "")
    generation_authenticated = bool(
        was_reserved
        and expected_generation
        and actual_generation
        and secrets.compare_digest(expected_generation, actual_generation)
    )
    if is_v2 and not generation_authenticated:
        # V2 never falls through to legacy promo.use(). The generation row is
        # immutable ownership evidence outside the mutable attempt JSON; an
        # old paid callback cannot consume capacity reissued to a newer invoice
        # or another account/proposal.
        reservation.update({
            "promo_id": promo.pk,
            "group_id": promo.group_id,
            "usage_error_at": timezone.now().isoformat(),
            "usage_error": "stale_reservation_generation",
            "reservation_generation_mismatch": True,
        })
        event_state["promo_reservation"] = reservation
        attempt.event_state = event_state
        return False
    if not attempt.user_id and not was_reserved:
        # Anonymous redemptions are bearer-capability payments.  A released,
        # consumed, or malformed reservation must never fall back to the
        # legacy ``promo.use()`` counter path: a late provider callback could
        # otherwise consume a fresh reservation belonging to another invoice.
        reservation.update({
            "promo_id": promo.pk,
            "group_id": promo.group_id,
            "usage_error_at": timezone.now().isoformat(),
            "usage_error": "guest_reservation_missing",
        })
        event_state["promo_reservation"] = reservation
        attempt.event_state = event_state
        return False
    try:
        with transaction.atomic():
            if attempt.user_id:
                from storefront.models import PromoCodeUsage

                usage = PromoCodeUsage.objects.filter(order=order).first()
                if usage is None:
                    PromoCodeUsage.objects.create(
                        user=attempt.user,
                        promo_code=promo,
                        group=promo.group,
                        order=order,
                    )
            elif not was_reserved:
                promo.use()
            else:
                from storefront.models import PromoCodeGuestUsage

                guest_usage_lookup = {
                    "pk": reservation.get("guest_usage_id"),
                    "promo_code_id": promo.pk,
                    "state": PromoCodeGuestUsage.State.RESERVED,
                }
                if reservation.get("guest_reservation_key"):
                    guest_usage_lookup["reservation_key"] = reservation[
                        "guest_reservation_key"
                    ]
                guest_usage = PromoCodeGuestUsage.objects.select_for_update().filter(
                    **guest_usage_lookup
                ).first()
                if guest_usage is None:
                    raise PromoReservationError("guest_reservation_missing")
                guest_usage.state = PromoCodeGuestUsage.State.CONSUMED
                guest_usage.order = order
                guest_usage.consumed_at = timezone.now()
                guest_usage.save(update_fields=["state", "order", "consumed_at"])
    except Exception as exc:
        reservation.update({
            "promo_id": promo.pk,
            "group_id": promo.group_id,
            "state": "reserved",
            "usage_error_at": timezone.now().isoformat(),
            "usage_error": str(exc)[:200],
        })
        if (
            isinstance(exc, PromoReservationError)
            and exc.reason == "guest_reservation_missing"
        ):
            # A reservation key from an older invoice no longer names the
            # currently reserved ledger generation.  Keep this distinct from
            # a transient persistence failure so conversion is not retried
            # against another customer's reservation.
            reservation["guest_reservation_mismatch"] = True
        event_state["promo_reservation"] = reservation
        attempt.event_state = event_state
        return False

    if not was_reserved and attempt.user_id and not is_v2:
        # Legacy attempts did not reserve capacity before provider I/O.
        promo.use()
    # A retry may be consuming a reservation that previously failed at the
    # ledger boundary.  Clear the durable retry marker and diagnostic fields
    # only after the guest/account usage write has committed successfully.
    reservation.pop("usage_error_at", None)
    reservation.pop("usage_error", None)
    event_state.pop("promo_consumption_pending", None)
    reservation.update({
        "promo_id": promo.pk,
        "group_id": promo.group_id,
        "state": "consumed",
        "consumed_at": timezone.now().isoformat(),
        "order_id": order.pk,
    })
    event_state["promo_reservation"] = reservation
    attempt.event_state = event_state
    return True


@transaction.atomic
def release_payment_attempt_promo(attempt, *, reason="payment_terminal"):
    """Release one invoice reservation exactly once."""
    from orders.models import PaymentAttempt
    from storefront.models import PromoCode, PromoCodeGuestUsage

    locked = PaymentAttempt.objects.select_for_update().get(pk=attempt.pk)
    event_state = dict(locked.event_state or {})
    reservation = dict(event_state.get("promo_reservation") or {})
    if reservation.get("state") != "reserved":
        return False
    if locked.checkout_series_key and locked.checkout_generation:
        try:
            expected_generation = str(
                locked.instagram_checkout_generation.promo_reservation_generation
                or ""
            )
        except Exception:
            expected_generation = ""
        actual_generation = str(
            reservation.get("reservation_generation") or ""
        )
        if (
            not expected_generation
            or not actual_generation
            or not secrets.compare_digest(expected_generation, actual_generation)
        ):
            reservation.update({
                "state": "released",
                "released_at": timezone.now().isoformat(),
                "release_reason": "stale_reservation_generation",
                "reservation_generation_mismatch": True,
            })
            event_state["promo_reservation"] = reservation
            locked.event_state = event_state
            locked.save(update_fields=["event_state", "updated"])
            return False
    promo_id = reservation.get("promo_id") or locked.promo_code_id
    promo = PromoCode.objects.select_for_update().filter(pk=promo_id).first()
    guest_usage = None
    if not locked.user_id:
        guest_usage_id = reservation.get("guest_usage_id")
        guest_reservation_key = reservation.get("guest_reservation_key")
        if guest_usage_id or guest_reservation_key:
            guest_usage_lookup = {
                "promo_code_id": promo_id,
                "state": PromoCodeGuestUsage.State.RESERVED,
            }
            if guest_usage_id:
                guest_usage_lookup["pk"] = guest_usage_id
            if guest_reservation_key:
                guest_usage_lookup["reservation_key"] = guest_reservation_key
            guest_usage = PromoCodeGuestUsage.objects.select_for_update().filter(
                **guest_usage_lookup
            ).first()
        if guest_usage is None:
            # The OneToOne ledger row may have been released and reissued to a
            # newer invoice between two terminal callbacks.  An old/legacy
            # anonymous attempt without an exact generation is equally unsafe:
            # do not decrement the shared promo counter or release any other
            # bearer reservation.
            reservation.update({
                "state": "released",
                "released_at": timezone.now().isoformat(),
                "release_reason": (
                    "stale_reservation_generation"
                    if (
                        reservation.get("guest_usage_id")
                        or reservation.get("guest_reservation_key")
                    )
                    else "guest_reservation_unresolved"
                ),
            })
            event_state["promo_reservation"] = reservation
            locked.event_state = event_state
            locked.save(update_fields=["event_state", "updated"])
            return False
    if (
        promo is not None
        and reservation.get("capacity_reserved", True)
        and promo.current_uses > 0
    ):
        promo.current_uses -= 1
        promo.save(update_fields=["current_uses", "updated_at"])
    reservation.update({
        "state": "released",
        "released_at": timezone.now().isoformat(),
        "release_reason": str(reason or "payment_terminal")[:128],
    })
    event_state["promo_reservation"] = reservation
    locked.event_state = event_state
    locked.save(update_fields=["event_state", "updated"])
    if guest_usage is not None:
        guest_usage.state = PromoCodeGuestUsage.State.RELEASED
        guest_usage.released_at = timezone.now()
        guest_usage.save(update_fields=["state", "released_at"])
    return True
