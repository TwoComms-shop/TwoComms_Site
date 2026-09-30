from django import template
from storefront.services.garment_bundle_presentation import bundle_copy

register = template.Library()


@register.simple_tag
def bundle_words():
    return bundle_copy()


@register.simple_tag
def garment_product_offer(product):
    from storefront.services.garment_bundle_catalog import garment_bundle_offer_summary
    result = garment_bundle_offer_summary(product)
    return result if result.get("eligible") else None


@register.simple_tag(takes_context=True)
def garment_cart_offer(context):
    """One useful invitation, only when a hoodie has spare pairing capacity."""
    if not context.get('bundle_available_hoodie_qty', 0):
        return None
    from storefront.services.brigade_commerce import get_product_brigade_policy
    used = {}
    for pair in context.get('bundle_pairs', []):
        key = pair.get('hoodie_key')
        used[key] = used.get(key, 0) + int(pair.get('qty', 0))
    for item in context.get('items', context.get('cart_items', [])):
        product = item.get('product')
        if product is None:
            continue
        policy = get_product_brigade_policy(product)
        if policy['garment'] == 'hoodie' and not policy['is_brigade']:
            key = item.get('key')
            qty = item.get('qty', item.get('quantity', 1))
            if int(qty) > used.get(key, 0):
                offer = garment_product_offer(product)
                if offer:
                    return {**offer, 'hoodie_key': key}
    return None


@register.simple_tag(takes_context=True)
def garment_mini_cart_offer(context):
    """Invite either unpaired ordinary garment into one useful category set.

    Savings are per additional pair, bounded by the actual selected cart unit.
    A promo can shrink when a unit joins a set (including voucher thresholds),
    so its net benefit cannot be promised by this category invitation.
    """
    from decimal import Decimal, ROUND_FLOOR
    from storefront.services.brigade_commerce import get_product_brigade_policy
    from storefront.services.garment_bundle import is_bundle_product_eligible, hoodie_offer_price, tee_offer_price

    request = context.get('request')
    session = getattr(request, 'session', {})
    if (context.get('applied_promo') or context.get('promo_code') or
            context.get('discount') or session.get('promo_code_id')):
        return None

    used = {}
    for pair in context.get('bundle_pairs', []):
        qty = max(0, int(pair.get('qty') or 0))
        for field in ('hoodie_key', 'tee_key'):
            key = pair.get(field)
            used[key] = used.get(key, 0) + qty

    # This is one category invitation, not a whole-cart partner search. Keep
    # cold summary work bounded even when several lines have no useful offer.
    checked_offers = 0
    for item in context.get('items', context.get('cart_items', [])):
        product, key = item.get('product'), item.get('key')
        if product is None or key is None or product.status != 'published':
            continue
        if int(item.get('qty', item.get('quantity', 1))) <= used.get(key, 0):
            continue
        policy = get_product_brigade_policy(product)
        if not is_bundle_product_eligible(product, policy):
            continue
        if checked_offers == 3:
            return None
        checked_offers += 1
        offer = garment_product_offer(product)
        if not offer or not offer.get('category_url'):
            continue

        variant = item.get('color_variant')
        standalone = item.get('brigade_pricing', {}).get('standalone_unit_price')
        if standalone is None:
            from product_catalog.services import effective_cart_unit_price
            raw = session.get('cart', {}).get(key, {})
            standalone = effective_cart_unit_price(
                product, variant, fit_code=item.get('fit_option_code') or raw.get('fit_option_code') or '',
                option_values=raw.get('option_values') or {},
            )
        standalone = Decimal(str(standalone))
        hoodie_discount = Decimal(str(offer.get('hoodie_unit_discount') or 0))
        nominal_saving = Decimal(str(offer.get('total_saving') or 0))
        if policy['garment'] == 'tee':
            variant_base = variant.price_override if variant else None
            retail = standalone + (Decimal(product.price) - Decimal(product.final_price) if variant_base is None else 0)
            selected_offer = tee_offer_price(product, standalone, retail,
                                            same_design=offer.get('is_same_design', False), variant_base=variant_base)
            saving = standalone - selected_offer + hoodie_discount
        else:
            saving = nominal_saving - hoodie_discount + standalone - hoodie_offer_price(standalone)
        # The inline copy renders whole UAH; rounding must never overpromise.
        saving = min(nominal_saving, saving).quantize(Decimal('1'), rounding=ROUND_FLOOR)
        if saving <= 0:
            continue
        return {**offer, 'kind': policy['garment'], 'total_saving': int(saving)}
    return None
