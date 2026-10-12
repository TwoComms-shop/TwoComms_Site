"""Build a deterministic, verified and price-aware catalog snapshot."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal

from productcolors.models import ProductColorVariant
from storefront.models import (
    Product,
    ProductSalesSemanticProfileRevision,
    ProductStatus,
)

from .ig_catalog_pricing import prepare_pricing_context, resolve_product_pricing
from .ig_commerce_types import (
    CatalogFit,
    CatalogGraph,
    CatalogProduct,
    CatalogVariant,
    PriceSnapshot,
    PricingConfiguration,
)


def _money_json(value: Decimal | None):
    return format(value, "f") if value is not None else None


def _aliases(value) -> dict[str, tuple[str, ...]]:
    return {
        str(locale): tuple(str(alias) for alias in aliases)
        for locale, aliases in sorted((value or {}).items())
        if isinstance(aliases, list)
    }


_GARMENT_TYPES_BY_CATEGORY = {
    "tshirt": "tshirt",
    "tshirts": "tshirt",
    "t-shirt": "tshirt",
    "t-shirts": "tshirt",
    "hoodie": "hoodie",
    "hoodies": "hoodie",
    "long-sleeve": "longsleeve",
    "long-sleeves": "longsleeve",
    "longsleeve": "longsleeve",
    "longsleeves": "longsleeve",
    "sweatshirt": "sweatshirt",
    "sweatshirts": "sweatshirt",
}


def _price_snapshot(value: dict) -> PriceSnapshot:
    configurations = tuple(
        PricingConfiguration(
            variant_id=(int(row["variant_id"]) if row.get("variant_id") is not None else None),
            color_id=(int(row["color_id"]) if row.get("color_id") is not None else None),
            color_slug=str(row.get("color_slug") or ""),
            color_label=str(row.get("color") or ""),
            fit_code=str(row.get("fit_code") or ""),
            option_values={
                str(key): str(option_value)
                for key, option_value in sorted((row.get("option_values") or {}).items())
            },
            compatible_sizes=tuple(row.get("compatible_sizes") or ()),
            price=Decimal(str(row["price"])),
            reason=str(row.get("price_reason") or ""),
            is_thermo=bool(row.get("is_thermo")),
        )
        for row in value.get("configurations", ())
    )
    return PriceSnapshot(
        configurations=configurations,
        minimum=(Decimal(str(value["minimum"])) if value.get("minimum") is not None else None),
        maximum=(Decimal(str(value["maximum"])) if value.get("maximum") is not None else None),
        exact=bool(value.get("exact")),
        display=str(value.get("display") or ""),
    )


def pricing_payload(pricing: PriceSnapshot) -> dict:
    return {
        "configurations": [
            {
                "variant_id": row.variant_id,
                "color_id": row.color_id,
                "color_slug": row.color_slug,
                "color_label": row.color_label,
                "fit_code": row.fit_code,
                "option_values": dict(row.option_values),
                "compatible_sizes": list(row.compatible_sizes),
                "price": _money_json(row.price),
                "reason": row.reason,
                "is_thermo": row.is_thermo,
            }
            for row in pricing.configurations
        ],
        "minimum": _money_json(pricing.minimum),
        "maximum": _money_json(pricing.maximum),
        "exact": pricing.exact,
        "display": pricing.display,
    }


def product_payload(item: CatalogProduct) -> dict:
    return {
        "product_id": item.product_id,
        "slug": item.slug,
        "title": item.title,
        "category": {
            "id": item.category_id,
            "slug": item.category_slug,
            "label": item.category_label,
        },
        "garment_type": item.garment_type,
        "catalog_priority": item.catalog_priority,
        "aliases": {key: list(value) for key, value in item.aliases.items()},
        "traits": dict(item.traits),
        "semantic_revision_id": item.semantic_revision_id,
        "variants": [
            {
                "id": row.variant_id,
                "color_id": row.color_id,
                "color_slug": row.color_slug,
                "color_label": row.color_label,
                "sku": row.sku,
            }
            for row in item.variants
        ],
        "fits": [
            {"code": row.code, "label": row.label}
            for row in item.fits
        ],
        "pricing": pricing_payload(item.pricing),
    }


def _effective_revision(product):
    try:
        revision = product.sales_semantic_profile.effective_revision
    except Product.sales_semantic_profile.RelatedObjectDoesNotExist:
        return None
    if revision is None:
        return None
    if revision.status != ProductSalesSemanticProfileRevision.Status.VERIFIED:
        return None
    if revision.profile_id != product.sales_semantic_profile.pk:
        return None
    if revision.source == ProductSalesSemanticProfileRevision.Source.BOT_VISION:
        return None
    return revision


def build_catalog_graph(*, product_ids=None, _catalog_readback=None) -> CatalogGraph:
    product_filter = {"status": ProductStatus.PUBLISHED}
    if product_ids is not None:
        product_filter["pk__in"] = tuple(int(value) for value in product_ids)
    products = list(
        Product.objects.filter(**product_filter)
        .select_related(
            "catalog",
            "category",
            "sales_semantic_profile__effective_revision",
            "size_grid",
        )
        .order_by("pk")
    )
    variants = list(
        ProductColorVariant.objects.filter(product_id__in=[row.pk for row in products])
        .select_related("color")
        .order_by("product_id", "order", "pk")
    )
    prepare_pricing_context(products, variants)
    if _catalog_readback is not None:
        # Private transient ORM graph; never part of immutable CatalogGraph.
        # The enclosing source capture seals its owner/digest only after fences.
        _catalog_readback.update(products=tuple(products), variants=tuple(variants), prepared=True)
    variants_by_product: dict[int, list[ProductColorVariant]] = {}
    for variant in variants:
        variants_by_product.setdefault(variant.product_id, []).append(variant)
    graph_products = []
    for product in products:
        product_variants = variants_by_product.get(product.pk, [])
        revision = _effective_revision(product)
        raw_pricing = resolve_product_pricing(
            product,
            variants=product_variants,
            context_prepared=True,
        )
        graph_products.append(CatalogProduct(
            product_id=int(product.pk),
            slug=str(product.slug),
            title=str(product.title),
            category_id=int(product.category_id),
            category_slug=str(getattr(product.category, "slug", "") or ""),
            category_label=str(getattr(product.category, "name", "") or ""),
            garment_type=_GARMENT_TYPES_BY_CATEGORY.get(
                str(getattr(product.category, "slug", "") or "").strip().lower(),
                "",
            ),
            catalog_priority=int(product.priority or 0),
            aliases=_aliases(revision.aliases) if revision else {},
            traits=dict(sorted((revision.traits or {}).items())) if revision else {},
            semantic_revision_id=int(revision.pk) if revision else None,
            variants=tuple(
                CatalogVariant(
                    variant_id=int(variant.pk),
                    color_id=int(variant.color_id),
                    color_slug=str(variant.slug or ""),
                    color_label=str(getattr(variant.color, "name", "") or ""),
                    sku=str(variant.sku or ""),
                )
                for variant in product_variants
            ),
            fits=tuple(
                CatalogFit(code=str(option.code), label=str(option.label))
                for option in product.fit_options.all()
                if option.is_active
            ),
            pricing=_price_snapshot(raw_pricing),
        ))

    immutable_products = tuple(graph_products)
    canonical_json = json.dumps(
        [product_payload(item) for item in immutable_products],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return CatalogGraph(
        products=immutable_products,
        digest=hashlib.sha256(canonical_json.encode("utf-8")).hexdigest(),
        canonical_json=canonical_json,
    )
