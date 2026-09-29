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
        parts = []
        if self.discounted_qty:
            parts.append((self.discounted_qty, self.offer_unit_price))
        regular_qty = self.qty - self.discounted_qty
        if regular_qty:
            parts.append((regular_qty, self.standalone_unit_price))
        return parts


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
        discounted = min(unit, max(Decimal("0.00"), retail_unit - delta)) if eligible else unit
        total = (discounted * eligible + unit * (qty - eligible)).quantize(MONEY)
        original_unit = max(unit, retail_unit) if policy["is_brigade"] else max(unit, Decimal(product.price))
        rule = ("tee_quantity" if garment == "tee" and tee_qty >= 2 else "hoodie_quantity" if garment == "hoodie" and hoodie_qty >= 2 else "tee_hoodie_pair") if eligible else ""
        line = BrigadeLinePrice(unit, discounted, qty, eligible, total, original_unit, policy, rule)
        lines[key] = line
        subtotal += total
        original += original_unit * qty
        offer_discount += line.discount_amount
        if not policy["is_brigade"]:
            promo_eligible += total
    return BrigadeCartPrice(lines, subtotal, original, offer_discount, promo_eligible, any(line.policy["is_brigade"] for line in lines.values()), tee_qty, hoodie_qty)
