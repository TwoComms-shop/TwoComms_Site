"""D033: fixed UAH first, then independent percentage rights, rounding once."""

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP


@dataclass(frozen=True)
class PromoComposition:
    total: Decimal
    discount: Decimal
    contributions: tuple


def session_promo_ids(session):
    """Read the current batch while retaining already-open single-code carts."""
    raw = session.get('promo_code_ids')
    if raw is None:
        raw = [session['promo_code_id']] if session.get('promo_code_id') else []
    if not isinstance(raw, (list, tuple)):
        raise ValueError('invalid_promos')
    ids = [int(value) for value in raw]
    if any(value <= 0 for value in ids) or len(ids) != len(set(ids)):
        raise ValueError('invalid_promos')
    return ids


def compose_promos(*, total_amount, promos):
    """Compose already eligible rights against the actual goods basket.

    Contributions contain (promo, money discount), reconcile to the final
    discount, and omit zero-value rights so they do not consume capacity.
    """
    total = Decimal(str(total_amount or 0))
    if not total.is_finite() or total < 0:
        raise ValueError("invalid_total")
    total = total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    unique = {}
    for promo in promos:
        identity = promo.pk or str(promo.code).strip().upper()
        if identity in unique:
            raise ValueError("duplicate_promo")
        unique[identity] = promo
    ordered = sorted(unique.values(), key=lambda p: (p.discount_type != "fixed", p.pk or 0, p.code))
    remaining = total
    contributions = []
    allocated = Decimal("0.00")
    for promo in ordered:
        value = Decimal(str(promo.discount_value or 0))
        if (not value.is_finite() or value < 0 or promo.discount_type not in {"fixed", "percentage"}
                or (promo.discount_type == "percentage" and value > 100)):
            raise ValueError("invalid_discount")
        if promo.min_order_amount and total < promo.min_order_amount:
            raise ValueError("not_applicable")
        before = remaining
        if promo.discount_type == "fixed":
            remaining = max(Decimal(0), remaining - value)
        else:
            remaining *= 1 - value / 100
        # Rounded cumulative allocation avoids a rounding drift in receipts.
        cumulative = (total - remaining).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        contribution = cumulative - allocated
        if remaining < before and contribution > 0:
            contributions.append((promo, contribution))
            allocated = cumulative
    final = remaining.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    discount = total - final
    # Half-cent ties must follow final-price rounding, not discount rounding.
    if contributions and allocated != discount:
        promo, amount = contributions[-1]
        contributions[-1] = (promo, amount + discount - allocated)
    return PromoComposition(total=final, discount=discount, contributions=tuple(
        (promo, amount) for promo, amount in contributions if amount > 0
    ))
