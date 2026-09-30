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
