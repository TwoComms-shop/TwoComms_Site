"""Trusted whole-cart prices and payment policy for brigade merchandise.

Retail prices live in Product/variant rows. The 225 offer removes a fixed
amount from eligible units, retaining every material/fit/option surcharge.
All brigade merchandise is excluded from additional promotional discounts.
"""
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

from product_catalog.services import effective_cart_unit_price

MONEY = Decimal("0.01")
TEE_OFFER_DISCOUNT = Decimal("80.00")
HOODIE_OFFER_DISCOUNT = Decimal("145.00")
LEGACY_225_SLUGS = {"225-tshirt", "225-hoodie"}


def brigade_225_unit_offer_price(standalone, retail, garment):
    delta = TEE_OFFER_DISCOUNT if garment == 'tee' else HOODIE_OFFER_DISCOUNT if garment == 'hoodie' else Decimal('0.00')
    return min(Decimal(standalone), max(Decimal('0.00'), Decimal(retail) - delta)).quantize(MONEY)


def brigade_225_offer_price(product, variant=None, *, fit_code='', option_values=None):
    """The per-unit 225 offer for a qualifying pair/quantity selection."""
    policy = get_product_brigade_policy(product)
    standalone = Decimal(effective_cart_unit_price(product, variant, fit_code=fit_code, option_values=option_values or {})).quantize(MONEY)
    if not policy['is_225']:
        return standalone
    retail = standalone
    if variant is None or variant.price_override is None:
        retail += Decimal(product.price) - Decimal(product.final_price)
    return brigade_225_unit_offer_price(standalone, retail, policy['garment'])


def _policy(product, assignments):
    collections = [assignment.collection for assignment in assignments]
    collection_225 = next((collection for collection in collections if collection.slug == "225"), None)
    brigade = next((collection for collection in collections if collection.kind == "brigade"), None)
    is_225 = collection_225 is not None or product.slug in LEGACY_225_SLUGS
    category_slug = getattr(getattr(product, "category", None), "slug", "")
    garment = "tee" if category_slug in {"tshirts", "tshirt", "t-shirt"} else "hoodie" if category_slug == "hoodie" else ""
    return {
        "is_brigade": bool(brigade or is_225),
        "is_225": is_225,
        "full_payment_only": bool(brigade or is_225),
        "garment": garment,
        "collection_slug": "225" if is_225 else getattr(brigade, "slug", ""),
    }


def get_product_brigade_policy(product):
    """Classify by taxonomy, with exact legacy slugs during assignment rollout."""
    cached = getattr(product, "_brigade_commerce_policy", None)
    if cached is not None:
        return dict(cached)
    manager = product.merch_collection_assignments
    if "merch_collection_assignments" in getattr(product, "_prefetched_objects_cache", {}):
        assignments = manager.all()
    else:
        assignments = manager.select_related("collection").all()
    policy = _policy(product, assignments)
    product._brigade_commerce_policy = policy
    return dict(policy)


def _cache_policies(products):
    from product_catalog.models import ProductMerchCollection

    missing = [product for product in products.values() if not hasattr(product, "_brigade_commerce_policy")]
    if not missing:
        return
    assignments = {product.pk: [] for product in missing}
    for assignment in ProductMerchCollection.objects.filter(product_id__in=assignments).select_related("collection"):
        assignments[assignment.product_id].append(assignment)
    for product in missing:
        product._brigade_commerce_policy = _policy(product, assignments[product.pk])


def brigade_payment_error():
    from storefront.services.brigade_presentation import brigade_text
    return brigade_text("full_payment")


def products_require_full_payment(products):
    product_map = {product.pk: product for product in products if product is not None}
    _cache_policies(product_map)
    return any(get_product_brigade_policy(product)["full_payment_only"] for product in product_map.values())


@dataclass(frozen=True)
class BrigadeLinePrice:
    standalone_unit_price: Decimal
    offer_unit_price: Decimal
    qty: int
    discounted_qty: int
    line_total: Decimal
    original_unit_price: Decimal
    policy: dict
    rule: str = ""
    tiers: tuple = ()
    bundle_allocations: tuple = ()

    @property
    def unit_price(self):
        return (self.line_total / self.qty).quantize(MONEY, rounding=ROUND_HALF_UP)

    @property
    def discount_amount(self):
        return (self.standalone_unit_price * self.qty - self.line_total).quantize(MONEY)

    def public_metadata(self):
        return {
            "is_brigade": self.policy["is_brigade"], "is_225": self.policy["is_225"],
            "discounted_qty": self.discounted_qty, "regular_qty": self.qty - self.discounted_qty,
            "standalone_unit_price": float(self.standalone_unit_price),
            "offer_unit_price": float(self.offer_unit_price),
            "discount_amount": float(self.discount_amount), "rule": self.rule,
        }

    def snapshot_parts(self):
        """Exact unit-price groups; no rounded weighted price in paid snapshots."""
        if self.tiers:
            return [(qty, unit) for qty, unit, _rule, _eligible in self.tiers]
        parts = []
        if self.discounted_qty:
            parts.append((self.discounted_qty, self.offer_unit_price))
        regular_qty = self.qty - self.discounted_qty
        if regular_qty:
            parts.append((regular_qty, self.standalone_unit_price))
        return parts

    def frozen_price_parts(self):
        if not self.tiers:
            return []
        return [{"qty": qty, "unit_price": str(unit), "rule": rule, "promo_eligible": eligible}
                for qty, unit, rule, eligible in self.tiers]

    def bundle_metadata(self):
        paired = sum(allocation.qty for allocation in self.bundle_allocations)
        return {
            "paired_qty": paired,
            "same_design_qty": sum(allocation.qty for allocation in self.bundle_allocations if allocation.kind == "same_design"),
            "other_design_qty": sum(allocation.qty for allocation in self.bundle_allocations if allocation.kind == "other_design"),
            "discount_amount": float(self.discount_amount) if self.bundle_allocations else 0.0,
            "tiers": [{"qty": qty, "unit_price": float(unit), "rule": rule, "promo_eligible": eligible}
                      for qty, unit, rule, eligible in self.tiers],
            "pairs": [allocation.public_metadata() for allocation in self.bundle_allocations],
        }


@dataclass(frozen=True)
class BrigadeCartPrice:
    lines: dict
    subtotal: Decimal
    original_subtotal: Decimal
    offer_discount_total: Decimal
    promo_eligible_subtotal: Decimal
    full_payment_only: bool
    tee_qty: int
    hoodie_qty: int
    bundle_allocations: tuple = ()
    bundle_hoodie_capacity: tuple = ()

    def public_metadata(self):
        return {
            "has_brigade_items": self.full_payment_only,
            "has_225_items": any(line.policy["is_225"] for line in self.lines.values()),
            "full_payment_only": self.full_payment_only,
            "brigade_discount_total": float(self.offer_discount_total),
            "brigade_tee_qty": self.tee_qty, "brigade_hoodie_qty": self.hoodie_qty,
            "brigade_pair_qty": min(self.tee_qty, self.hoodie_qty),
            "promo_eligible_subtotal": float(self.promo_eligible_subtotal),
            "promo_excludes_brigade": True,
            "bundle_discount_total": float(sum((allocation.discount_amount for allocation in self.bundle_allocations), Decimal("0.00"))),
            "bundle_paired_tee_qty": sum(allocation.qty for allocation in self.bundle_allocations),
            "bundle_same_design_qty": sum(allocation.qty for allocation in self.bundle_allocations if allocation.kind == "same_design"),
            "bundle_other_design_qty": sum(allocation.qty for allocation in self.bundle_allocations if allocation.kind == "other_design"),
            "bundle_available_hoodie_qty": sum(qty for _key, qty in self.bundle_hoodie_capacity),
            "bundle_available_hoodies": [{"key": key, "qty": qty} for key, qty in self.bundle_hoodie_capacity if qty > 0],
            "bundle_pairs": [allocation.public_metadata() for allocation in self.bundle_allocations],
        }


def calculate_brigade_cart_pricing(cart, products=None, variants=None):
    """Price validated session rows from trusted DB state, never session prices."""
    from storefront.models import Product
    from productcolors.models import ProductColorVariant

    if products is None:
        products = Product.objects.select_related("category").in_bulk([item["product_id"] for item in cart.values()])
    if variants is None:
        variants = ProductColorVariant.objects.select_related("product", "color").in_bulk([
            item["color_variant_id"] for item in cart.values() if item.get("color_variant_id")
        ])
    _cache_policies(products)
    prepared = {}
    tee_qty = hoodie_qty = 0
    for key, item in sorted(cart.items(), key=lambda pair: str(pair[0])):
        product = products.get(int(item["product_id"]))
        if product is None:
            continue
        variant_id = item.get("color_variant_id")
        variant = variants.get(int(variant_id)) if variant_id else None
        if variant_id and (variant is None or variant.product_id != product.pk):
            continue
        qty = int(item.get("qty") or item.get("quantity") or 1)
        if qty <= 0:
            continue
        policy = get_product_brigade_policy(product)
        unit = effective_cart_unit_price(product, variant, fit_code=item.get("fit_option_code") or item.get("fit_code") or item.get("fit") or "", option_values=item.get("option_values") or {})
        unit = Decimal(unit).quantize(MONEY)
        retail_unit = unit
        if variant is None or variant.price_override is None:
            retail_unit += Decimal(product.price) - Decimal(product.final_price)
        prepared[key] = (product, policy, qty, unit, retail_unit)
        if policy["is_225"]:
            if policy["garment"] == "tee":
                tee_qty += qty
            elif policy["garment"] == "hoodie":
                hoodie_qty += qty
    from storefront.services.garment_bundle import allocate_garment_bundles, is_bundle_product_eligible
    bundle_allocations = allocate_garment_bundles(cart, prepared, variants)
    paired_by_key = {}
    for allocation in bundle_allocations:
        paired_by_key.setdefault(allocation.tee_key, []).append(allocation)
        paired_by_key.setdefault(allocation.hoodie_key, []).append(allocation)
    hoodie_capacity = tuple(
        (key, qty - sum(allocation.qty for allocation in paired_by_key.get(key, ())))
        for key, (product, policy, qty, _unit, _retail) in prepared.items()
        if policy["garment"] == "hoodie" and is_bundle_product_eligible(product, policy)
    )
    pairs = min(tee_qty, hoodie_qty)
    budgets = {"tee": tee_qty if tee_qty >= 2 else pairs, "hoodie": hoodie_qty if hoodie_qty >= 2 else pairs}
    lines = {}
    subtotal = original = offer_discount = promo_eligible = Decimal("0.00")
    for key, (product, policy, qty, unit, retail_unit) in prepared.items():
        garment = policy["garment"]
        eligible = min(qty, budgets.get(garment, 0)) if policy["is_225"] else 0
        if eligible:
            budgets[garment] -= eligible
        delta = TEE_OFFER_DISCOUNT if garment == "tee" else HOODIE_OFFER_DISCOUNT if garment == "hoodie" else Decimal("0.00")
        # Choose the better site price or offer; never apply both discounts.
        discounted = brigade_225_unit_offer_price(unit, retail_unit, garment) if eligible else unit
        total = (discounted * eligible + unit * (qty - eligible)).quantize(MONEY)
        original_unit = max(unit, retail_unit)
        rule = ("tee_quantity" if garment == "tee" and tee_qty >= 2 else "hoodie_quantity" if garment == "hoodie" and hoodie_qty >= 2 else "tee_hoodie_pair") if eligible else ""
        allocations = tuple(paired_by_key.get(key, ()))
        tiers = []
        if allocations:
            paired_qty = sum(allocation.qty for allocation in allocations)
            tier_counts = {}
            for allocation in allocations:
                tier_unit = allocation.tee_unit_price if garment == "tee" else allocation.hoodie_unit_price
                tier_key = (tier_unit, allocation.kind)
                tier_counts[tier_key] = tier_counts.get(tier_key, 0) + allocation.qty
            tiers = [(count, tier_unit, f"bundle_{kind}", False)
                     for (tier_unit, kind), count in sorted(tier_counts.items())]
            if qty > paired_qty:
                tiers.append((qty - paired_qty, unit, "", True))
            total = sum((count * tier_unit for count, tier_unit, _kind, _promo in tiers), Decimal("0.00"))
            eligible = paired_qty
            discounted = min(tier_unit for _count, tier_unit, _kind, _promo in tiers)
            rule = "garment_bundle"
        else:
            if eligible:
                tiers.append((eligible, discounted, rule, not policy["is_brigade"]))
            if qty > eligible:
                tiers.append((qty - eligible, unit, "", not policy["is_brigade"]))
        line = BrigadeLinePrice(unit, discounted, qty, eligible, total, original_unit, policy, rule, tuple(tiers), allocations)
        lines[key] = line
        subtotal += total
        original += original_unit * qty
        if policy["is_brigade"]:
            offer_discount += line.discount_amount
        promo_eligible += sum((count * tier_unit for count, tier_unit, _kind, allowed in tiers if allowed), Decimal("0.00"))
    return BrigadeCartPrice(lines, subtotal, original, offer_discount, promo_eligible, any(line.policy["is_brigade"] for line in lines.values()), tee_qty, hoodie_qty, bundle_allocations, hoodie_capacity)
