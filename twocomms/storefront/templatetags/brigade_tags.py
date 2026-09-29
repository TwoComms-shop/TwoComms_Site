from django import template
from django.urls import reverse

from storefront.services.brigade_presentation import brigade_copy

register = template.Library()


@register.simple_tag
def brigade_words():
    return brigade_copy()


@register.simple_tag(takes_context=True)
def brigade_product_offer(context, product):
    from storefront.services.brigade_commerce import calculate_brigade_cart_pricing, get_product_brigade_policy
    from storefront.models import Product

    policy = get_product_brigade_policy(product)
    if not policy['is_225']:
        return None
    request = context.get('request')
    cache = getattr(request, '_brigade_offer_products', None)
    if cache is None:
        from django.db.models import Prefetch
        from productcolors.models import ProductColorVariant

        variants = ProductColorVariant.objects.select_related(
            'color', 'color__product_catalog_profile', 'product_catalog_details',
        ).prefetch_related(
            'product_catalog_details__i18n',
            'product_catalog_fit_rules', 'product_catalog_size_rules',
            'product_catalog_faqs', 'product_catalog_combinations',
            'product_catalog_combinations__i18n',
        )
        cache = {p.slug: p for p in Product.objects.filter(
            slug__in=('225-tshirt', '225-hoodie'), status='published'
        ).select_related('category').prefetch_related(
            'fit_options', 'product_catalog_fit_notes',
            'product_catalog_option_profiles', 'product_catalog_option_profiles__i18n',
            'product_catalog_axis_presentations', 'category__product_catalog_flows',
            Prefetch('color_variants', queryset=variants),
        )}
        if request is not None:
            request._brigade_offer_products = cache
    sibling_slug = '225-tshirt' if policy['garment'] == 'hoodie' else '225-hoodie'
    sibling = cache.get(sibling_slug)
    if sibling is None:
        return None
    from product_catalog.services import effective_cart_unit_price, variant_allows_purchase
    minimums = {}
    candidates = {}
    candidate_variants = {}
    for slug, piece in cache.items():
        variants = list(piece.color_variants.all()) or [None]
        fits = [fit.code for fit in piece.fit_options.all() if fit.is_active] or ['']
        prices = [(effective_cart_unit_price(piece, variant, fit_code=fit, option_values={'fit': fit} if fit else {}), variant, fit)
                  for variant in variants for fit in fits
                  if variant_allows_purchase(piece, variant, fit_code=fit)]
        if prices:
            price, variant, fit = min(prices, key=lambda choice: choice[0])
            minimums[slug] = price
            candidates[slug] = {'product_id': piece.pk, 'qty': 1, 'color_variant_id': variant.pk if variant else None,
                                'fit_option_code': fit, 'option_values': {'fit': fit} if fit else {}}
            if variant:
                candidate_variants[variant.pk] = variant
    if len(minimums) != 2:
        return None
    regular_total = sum(minimums.values())
    quote = calculate_brigade_cart_pricing(candidates, products={piece.pk: piece for piece in cache.values()}, variants=candidate_variants)
    # This presentation promises exactly 80 + 145; suppress it if a future sale
    # already gives a better price rather than claiming a second discount.
    if regular_total - quote.subtotal != 225:
        return None
    return {
        'policy': policy, 'sibling': sibling,
        'url': reverse('product', kwargs={'slug': sibling.slug}),
        'cta': brigade_copy()['choose_tee' if policy['garment'] == 'hoodie' else 'choose_hoodie'],
        'tee': cache.get('225-tshirt'), 'hoodie': cache.get('225-hoodie'),
        'set_regular_price': regular_total,
        'set_offer_price': quote.subtotal,
    }


@register.simple_tag(takes_context=True)
def brigade_policy(context, product):
    """Cards need only the 225 flag; batch taxonomy lookup once per request."""
    if product.slug in ('225-tshirt', '225-hoodie'):
        return {'is_225': True}
    request = context.get('request')
    ids = getattr(request, '_brigade_card_ids', None)
    if ids is None:
        from product_catalog.models import ProductMerchCollection
        ids = set(ProductMerchCollection.objects.filter(
            collection__slug='225'
        ).values_list('product_id', flat=True))
        if request is not None:
            request._brigade_card_ids = ids
    return {'is_225': product.pk in ids}
