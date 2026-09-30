"""Reviewed print identity and public choices for ordinary hoodie/tee sets.

Pairs were read from production warehouse.Print.default_products on 2026-09-30.
ProductPrintLink was empty. Print #21 (the ubiquitous brand logo) is excluded;
neither similar titles nor generic logo membership establish a shared design.
"""

from typing import NamedTuple


class SamePrintPair(NamedTuple):
    tee_id: int
    tee_slug: str
    hoodie_id: int
    hoodie_slug: str
    print_id: int


VERIFIED_SAME_PRINT_PAIRS = (
    SamePrintPair(4, "my-little-baby", 5, "my-little-baby-hd", 11),
    SamePrintPair(13, "business-money", 14, "business-money-hd", 15),
    SamePrintPair(16, "last-breath", 17, "last-breath-hd", 3),
    SamePrintPair(22, "pokrovsk-girl", 23, "pokrovsk-girl-hd", 8),
    SamePrintPair(28, "dvoznachni-summy", 29, "dvoznachni-summy-hd", 4),
    SamePrintPair(34, "red-leaves-ts", 35, "red-leaves-hd", 6),
    SamePrintPair(37, "death-gbs-ass-ts", 38, "death-gbs-ass-hd", 10),
    SamePrintPair(43, "kha-style-ts", 44, "kha-style-hd", 7),
    SamePrintPair(49, "bentejne-ts", 50, "bentejne-hd", 5),
    SamePrintPair(106, "twocomms-beliveidea-ts", 101, "idea-hd", 13),
    SamePrintPair(103, "twocomms-reality-bends-future-2026", 102, "hd-twocomms-reality-bends-future-2026", 18),
    SamePrintPair(105, "ts-twocomms-reality-bends-mentol", 102, "hd-twocomms-reality-bends-future-2026", 18),
)

BUNDLE_EXCLUDED_PRODUCT_IDS = frozenset({110})
BUNDLE_EXCLUDED_SLUGS = frozenset({"futbolka-boiova-kvitochka"})


def same_design_tee_ids_for(hoodie):
    """Return reviewed candidates only when the hoodie ID and slug still match."""
    return tuple(
        pair.tee_id for pair in VERIFIED_SAME_PRINT_PAIRS
        if pair.hoodie_id == hoodie.pk and pair.hoodie_slug == hoodie.slug
    )


def same_design_hoodie_ids_for(tee):
    return tuple(
        pair.hoodie_id for pair in VERIFIED_SAME_PRINT_PAIRS
        if pair.tee_id == tee.pk and pair.tee_slug == tee.slug
    )


def is_same_design(tee, hoodie):
    """Pure exact identity lookup; mutable product titles never affect prices."""
    return any(
        (pair.tee_id, pair.tee_slug, pair.hoodie_id, pair.hoodie_slug)
        == (tee.pk, tee.slug, hoodie.pk, hoodie.slug)
        for pair in VERIFIED_SAME_PRINT_PAIRS
    )


def is_bundle_excluded(product):
    from storefront.services.brigade_commerce import get_product_brigade_policy

    return (
        product.pk in BUNDLE_EXCLUDED_PRODUCT_IDS
        or product.slug in BUNDLE_EXCLUDED_SLUGS
        or get_product_brigade_policy(product)["is_brigade"]
    )


def _language(language=None):
    from django.utils.translation import get_language

    code = str(language or get_language() or "uk").replace("_", "-").split("-", 1)[0].lower()
    return code if code in {"uk", "ru", "en"} else "uk"


def _field_url(value):
    return value.url if value else ""


def _hoodie_offer_context(hoodie, kind="ordinary"):
    from decimal import Decimal
    from storefront.services.garment_bundle import hoodie_offer_price

    standalone = Decimal(str(hoodie.final_price))
    if kind == "225":
        from storefront.services.brigade_commerce import brigade_225_offer_price
        offered = brigade_225_offer_price(hoodie)
    else:
        offered = hoodie_offer_price(standalone)
    return {"hoodie_price": float(standalone), "hoodie_offer_price": float(offered),
            "hoodie_unit_discount": float(standalone - offered)}


def _cheapest_fits(product_choice):
    return {
        code: min(
            (fit for variant in product_choice["variants"] for fit in variant["fits"] if fit["code"] == code),
            key=lambda fit: (fit["offer_unit_price"], fit["standalone_unit_price"]), default=None,
        )
        for code in ("classic", "oversize")
    }


def _eligible_products(category):
    from django.db.models import Q
    from storefront.models import Product

    return (
        Product.objects.filter(status="published", category__slug=category)
        .exclude(pk__in=BUNDLE_EXCLUDED_PRODUCT_IDS)
        .exclude(slug__in=BUNDLE_EXCLUDED_SLUGS | {"225-tshirt", "225-hoodie"})
        .exclude(Q(merch_collection_assignments__collection__kind="brigade")
                 | Q(merch_collection_assignments__collection__slug="225"))
    )


def _225_tee_products():
    from django.db.models import Q
    from storefront.models import Product

    return Product.objects.filter(
        Q(merch_collection_assignments__collection__slug="225") | Q(slug="225-tshirt"),
        status="published", category__slug__in=("tshirts", "tshirt", "t-shirt"),
    ).distinct()


def garment_bundle_offer_summary(product, language=None):
    """Cheap PDP hint. Full colour/fit/size choices load only on chooser open."""
    from django.core.cache import cache
    from django.db.models import Case, IntegerField, Value, When
    from django.urls import reverse
    from django.utils.translation import override
    from storefront.services.catalog_helpers import get_public_product_order_version

    category = getattr(getattr(product, "category", None), "slug", "")
    if product.status != "published" or category not in {"hoodie", "tshirts"} or is_bundle_excluded(product):
        return {"eligible": False}
    language = _language(language)
    cache_key = f"garment-bundle-summary-v2:{get_public_product_order_version()}:{language}:{product.pk}:{product.slug}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    hoodie_view = category == "hoodie"
    preferred = same_design_tee_ids_for(product) if hoodie_view else same_design_hoodie_ids_for(product)
    candidates = _eligible_products("tshirts" if hoodie_view else "hoodie")
    if hoodie_view:
        candidates = candidates.filter(fit_options__code="classic", fit_options__is_active=True)
    partner = candidates.annotate(
        bundle_rank=Case(When(pk__in=preferred, then=Value(0)), default=Value(1), output_field=IntegerField())
    ).order_by("bundle_rank", "pk").first()
    if partner is None:
        return {"eligible": False}
    tee, hoodie = (partner, product) if hoodie_view else (product, partner)
    same = is_same_design(tee, hoodie)
    tee_rows = list(_catalog_queryset(summary=True, include_images=not bool(tee.main_image)).filter(pk=tee.pk))
    for row in tee_rows:
        # Summary uses prices and availability, never variant marketing/FAQ or
        # axis presentation copy. Keep the shared pricing APIs on prefetched
        # empty copy layers rather than making those unused database queries.
        row._prefetched_objects_cache["product_catalog_axis_presentations"] = []
        for profile in row.product_catalog_option_profiles.all():
            profile._prefetched_objects_cache = {"i18n": []}
        for variant in row.color_variants.all():
            variant._prefetched_objects_cache["product_catalog_faqs"] = []
            if tee.main_image:
                variant._prefetched_objects_cache["images"] = []
            details = variant._state.fields_cache.get("product_catalog_details")
            if details is not None:
                details._prefetched_objects_cache = {"i18n": []}
            for combination in variant.product_catalog_combinations.all():
                combination._prefetched_objects_cache = {"i18n": []}
    choices = _build_product_choices(hoodie, tee_rows, language)
    if not choices:
        return {"eligible": False}
    choice = choices[0]
    fits = _cheapest_fits(choice)
    default_fit = fits["classic"] or fits["oversize"]
    if default_fit is None:
        return {"eligible": False}
    with override(language):
        result = {
            "eligible": True,
            "kind": "hoodie" if hoodie_view else "tee",
            "hoodie_id": hoodie.pk, "tee_id": tee.pk,
            "title": partner.title,
            "image": choice["image"] if hoodie_view else _field_url(partner.main_image),
            "url": reverse("product", kwargs={"slug": partner.slug}),
            "is_same_design": same,
            "classic_price": fits["classic"]["offer_unit_price"] if fits["classic"] else None,
            "oversize_price": fits["oversize"]["offer_unit_price"] if fits["oversize"] else None,
            "classic_saving": fits["classic"]["total_saving"] if fits["classic"] else None,
            "oversize_saving": fits["oversize"]["total_saving"] if fits["oversize"] else None,
            "classic_pair_total": fits["classic"]["pair_total"] if fits["classic"] else None,
            "oversize_pair_total": fits["oversize"]["pair_total"] if fits["oversize"] else None,
            "total_saving": default_fit["total_saving"],
            "pair_total": default_fit["pair_total"],
            **_hoodie_offer_context(hoodie),
            "lazy_choices": True,
        }
    cache.set(cache_key, result, timeout=60)
    return result


def _catalog_queryset(*, summary=False, include_images=True, kind="ordinary"):
    """Prefetch every pricing/availability layer once for all candidate tees."""
    from django.db.models import Prefetch
    from product_catalog.models import ProductMerchCollection, ProductOptionSizeGrid, VariantOptionSizeGrid
    from productcolors.models import ProductColorVariant

    variant_prefetches = [
        "product_catalog_fit_rules", "product_catalog_size_rules", "product_catalog_combinations",
        Prefetch("product_catalog_size_grid_assignments", queryset=VariantOptionSizeGrid.objects.select_related(
            "size_grid", "size_grid__product_catalog_profile",
        )),
    ]
    if include_images:
        variant_prefetches.append("images")
    if not summary:
        variant_prefetches.extend(["product_catalog_faqs", "product_catalog_details__i18n", "product_catalog_combinations__i18n"])
    variants = ProductColorVariant.objects.select_related(
        "color", "color__product_catalog_profile", "product_catalog_details",
    ).prefetch_related(*variant_prefetches).order_by("order", "pk")
    copy_prefetches = [] if summary else ["product_catalog_option_profiles__i18n", "product_catalog_axis_presentations"]
    products = _225_tee_products() if kind == "225" else _eligible_products("tshirts")
    policy_prefetches = [Prefetch("merch_collection_assignments", queryset=ProductMerchCollection.objects.select_related("collection"))] if kind == "225" else []
    return products.select_related("category", "catalog", "size_grid").prefetch_related(
        "category__product_catalog_flows", "catalog__options__values",
        "fit_options", "product_catalog_fit_notes", "product_catalog_size_rules",
        "product_catalog_option_profiles", *copy_prefetches, *policy_prefetches,
        Prefetch("product_catalog_size_grid_assignments", queryset=ProductOptionSizeGrid.objects.select_related(
            "size_grid", "size_grid__product_catalog_profile",
        )),
        Prefetch("color_variants", queryset=variants),
    ).order_by("pk")


def _cache_default_size_grids(products):
    """Memoize resolver defaults per catalog/fit without writing any rows.

    The official comparison resolver consumes prefetched assignments. Supplying
    its already-resolved default as an unsaved assignment avoids repeating the
    same catalog-wide fallback query for every product and colour.
    """
    from product_catalog.models import ProductOptionSizeGrid
    from product_catalog.size_grid_services import resolve_option_size_grid

    defaults = {}
    for product in products:
        rows = list(product.product_catalog_size_grid_assignments.all())
        keys = {row.option_key for row in rows}
        for fit in product.fit_options.all():
            key = f"fit={fit.code}"
            if not fit.is_active or key in keys:
                continue
            cache_key = (product.catalog_id, key)
            if cache_key not in defaults:
                defaults[cache_key] = resolve_option_size_grid(product, key)
            grid = defaults[cache_key]
            if grid is not None:
                rows.append(ProductOptionSizeGrid(pk=-(len(rows) + 1), product=product, option_key=key, size_grid=grid))
        product._prefetched_objects_cache["product_catalog_size_grid_assignments"] = rows


def _legacy_sizes(product, variant, fit_code, option_key, defaults):
    """Match the purchase endpoint's no-grid fallback, with batched rules."""
    from product_catalog.size_grid_services import normalize_size_value
    from storefront.services.size_guides import detect_size_profile, resolve_product_sizes

    product_rules = {
        normalize_size_value(rule.size): rule
        for rule in product.product_catalog_size_rules.all()
        if rule.option_key == option_key
    }
    rules = sorted(variant.product_catalog_size_rules.all(), key=lambda rule: rule.pk)
    general = {normalize_size_value(rule.size): rule for rule in rules if not rule.fit_code}
    specific = {normalize_size_value(rule.size): rule for rule in rules if rule.fit_code == fit_code}
    key = (product.catalog_id, product.size_grid_id, detect_size_profile(product))
    if key not in defaults:
        defaults[key] = resolve_product_sizes(product)
    result = []
    for value in defaults[key]:
        size = normalize_size_value(value)
        product_rule = product_rules.get(size)
        enabled = product_rule.is_enabled if product_rule is not None else True
        variant_rule = specific.get(size) or general.get(size)
        if variant_rule is not None:
            enabled = variant_rule.is_enabled and (variant_rule.stock is None or variant_rule.stock > 0)
        if enabled and size not in result:
            result.append(size)
    return result


def garment_bundle_tee_catalog(hoodie, language=None):
    """JSON-safe validated choices; the add endpoint must revalidate selections.

    ``products[].variants[].fits[]`` contains explicit option_values, sizes,
    standalone_unit_price and offer_unit_price. No size is selected by default.
    Other axes are included only when their enabled choice is fixed; products
    requiring another interactive selector are not offered by this chooser.
    """
    from django.core.cache import cache
    from storefront.services.catalog_helpers import get_public_product_order_version
    from storefront.services.brigade_commerce import get_product_brigade_policy

    policy = get_product_brigade_policy(hoodie)
    kind = "225" if policy["is_225"] else "ordinary"
    if hoodie.status != "published" or policy["garment"] != "hoodie" or (kind != "225" and is_bundle_excluded(hoodie)):
        return {"eligible": False, "hoodie_id": hoodie.pk, "products": []}
    language = _language(language)
    key = f"garment-bundle-catalog-v3:{get_public_product_order_version()}:{language}:{hoodie.pk}:{hoodie.slug}:{kind}"
    cached = cache.get(key)
    if cached is not None:
        return cached
    payload = {"eligible": True, "kind": kind, "hoodie_id": hoodie.pk, **_hoodie_offer_context(hoodie, kind),
               "products": _build_product_choices(hoodie, list(_catalog_queryset(kind=kind)), language, kind=kind)}
    payload["total_saving"] = payload["products"][0]["total_saving"] if payload["products"] else 0
    cache.set(key, payload, timeout=60)
    return payload


def _build_product_choices(hoodie, products, language, kind="ordinary"):
    from decimal import Decimal
    from django.db.models import prefetch_related_objects
    from django.urls import reverse
    from django.utils.translation import override
    from product_catalog.services import build_combination_key, effective_cart_unit_price, product_option_context, variant_allows_options
    from product_catalog.size_grid_services import build_size_grid_comparison
    from productcolors.color_i18n import translate_color_name
    from storefront.services.garment_bundle import garment_bundle_tee_offer_price

    _cache_default_size_grids(products)
    # The legacy guide resolver reads catalog defaults only when no option grid
    # exists. Load those defaults in one batch instead of once per colour/fit.
    fallback_catalogs = [
        product.catalog for product in products
        if product.catalog_id and not product.product_catalog_size_grid_assignments.all()
    ]
    if fallback_catalogs:
        prefetch_related_objects(fallback_catalogs, "size_grids")
    legacy_defaults = {}
    hoodie_context = _hoodie_offer_context(hoodie, kind)
    hoodie_discount = Decimal(str(hoodie_context["hoodie_unit_discount"]))
    hoodie_offer = Decimal(str(hoodie_context["hoodie_offer_price"]))
    payload = {"products": []}
    with override(language):
        for product in products:
            variants = list(product.color_variants.all())
            has_option_grids = bool(product.product_catalog_size_grid_assignments.all()) or any(
                variant.product_catalog_size_grid_assignments.all() for variant in variants
            )
            comparisons = build_size_grid_comparison(product, variants=variants, lang=language) if has_option_grids else []
            sizes_by_choice = {
                (row["variant_id"], grid["option_key"]): row["available_sizes"]
                for grid in comparisons for row in grid["variants"] if row["grid_id"] is not None
            }
            same = is_same_design(product, hoodie)
            variant_payloads = []
            for variant in variants:
                variant.product = product
                axes = product_option_context(product, variant=variant, lang=language)["axes"]
                fit_axis = next((axis for axis in axes if axis["code"] == "fit"), None)
                if fit_axis is None:
                    continue
                fixed_options = {}
                for axis in axes:
                    if axis["code"] == "fit":
                        continue
                    enabled = [choice for choice in axis["choices"] if choice["is_enabled"]]
                    if len(enabled) != 1:
                        break
                    fixed_options[axis["code"]] = enabled[0]["code"]
                else:
                    fits = []
                    for fit in fit_axis["choices"]:
                        options = {**fixed_options, "fit": fit["code"]}
                        option_key = build_combination_key(options)
                        fit_key = f"fit={fit['code']}"
                        choice_key = (variant.pk, option_key)
                        if choice_key not in sizes_by_choice:
                            choice_key = (variant.pk, fit_key)
                        sizes = sizes_by_choice.get(choice_key)
                        if sizes is None:
                            sizes = _legacy_sizes(product, variant, fit["code"], fit_key, legacy_defaults)
                        if not fit["is_enabled"] or not sizes or not variant_allows_options(variant, options):
                            continue
                        standalone = effective_cart_unit_price(product, variant, fit_code=fit["code"], option_values=options)
                        if kind == "225":
                            from storefront.services.brigade_commerce import brigade_225_offer_price
                            offered = brigade_225_offer_price(product, variant, fit_code=fit["code"], option_values=options)
                        else:
                            offered = garment_bundle_tee_offer_price(
                                product, variant, fit_code=fit["code"], option_values=options, same_design=same,
                            )
                        fits.append({
                            "code": fit["code"], "label": fit["label"], "sizes": list(sizes),
                            "option_values": options, "available": True,
                            "standalone_unit_price": float(standalone), "offer_unit_price": float(offered),
                            "pair_total": float(hoodie_offer + offered),
                            "total_saving": float(hoodie_discount + standalone - offered),
                        })
                    if not fits:
                        continue
                    images = list(variant.images.all())
                    variant_payloads.append({
                        "id": variant.pk, "color_name": translate_color_name(variant.color.name, language),
                        "primary_hex": variant.color.primary_hex, "secondary_hex": variant.color.secondary_hex or "",
                        "title": product.title, "image": _field_url(images[0].image if images else product.main_image),
                        "url": reverse("product", kwargs={"slug": product.slug, "v1": variant.slug}) if variant.slug else reverse("product", kwargs={"slug": product.slug}),
                        "actual_base_price": float(variant.price_override if variant.price_override is not None else product.final_price),
                        "fits": fits,
                    })
            if variant_payloads:
                product_choice = {
                    "id": product.pk, "slug": product.slug, "title": product.title,
                    "image": _field_url(product.main_image) or variant_payloads[0]["image"],
                    "url": reverse("product", kwargs={"slug": product.slug}),
                    "is_same_design": same, "available": True,
                    "base_price": float(product.price), "actual_base_price": float(product.final_price),
                    "variants": variant_payloads,
                }
                cheapest = _cheapest_fits(product_choice)
                default = cheapest["classic"] or cheapest["oversize"]
                product_choice["offer_classic_price"] = cheapest["classic"]["offer_unit_price"] if cheapest["classic"] else None
                product_choice["total_saving"] = default["total_saving"] if default else 0
                payload["products"].append(product_choice)
    payload["products"].sort(key=lambda row: (not row["is_same_design"], row["id"]))
    return payload["products"]
