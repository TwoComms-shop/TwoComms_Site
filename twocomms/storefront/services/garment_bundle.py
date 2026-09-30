"""Deterministic, quantity-limited ordinary hoodie + tee offers."""
from dataclasses import dataclass
from decimal import Decimal
import heapq
import json

MONEY = Decimal("0.01")
SAME_DESIGN_TEE_BASE = Decimal("850.00")
OTHER_DESIGN_TEE_BASE = Decimal("900.00")
PAIRED_HOODIE_DISCOUNT = Decimal("50.00")


def hoodie_offer_price(standalone):
    standalone = Decimal(standalone).quantize(MONEY)
    return min(standalone, max(MONEY, standalone - PAIRED_HOODIE_DISCOUNT)).quantize(MONEY)


def garment_bundle_hoodie_offer_price(product, variant=None, *, fit_code="", option_values=None):
    from product_catalog.services import effective_cart_unit_price
    return hoodie_offer_price(effective_cart_unit_price(product, variant, fit_code=fit_code, option_values=option_values or {}))


def tee_offer_price(product, standalone, retail, *, same_design=False, variant_base=None):
    # A cheaper fixed variant price is an existing sale, not a negative option
    # surcharge. Keep positive variant premiums and all explicit axis deltas.
    correction = max(Decimal('0.00'), Decimal(product.price) - Decimal(variant_base)) if variant_base is not None else Decimal('0.00')
    target = (SAME_DESIGN_TEE_BASE if same_design else OTHER_DESIGN_TEE_BASE) + retail - Decimal(product.price) + correction
    return min(standalone, max(Decimal("0.00"), target)).quantize(MONEY)


def garment_bundle_tee_offer_price(product, variant=None, *, fit_code="", option_values=None, same_design=False):
    from product_catalog.services import effective_cart_unit_price
    standalone = Decimal(effective_cart_unit_price(product, variant, fit_code=fit_code, option_values=option_values or {})).quantize(MONEY)
    retail = standalone
    if variant is None or variant.price_override is None:
        retail += Decimal(product.price) - Decimal(product.final_price)
    return tee_offer_price(product, standalone, retail, same_design=same_design, variant_base=variant.price_override if variant else None)


@dataclass(frozen=True)
class BundleAllocation:
    hoodie_key: str
    tee_key: str
    qty: int
    kind: str
    tee_unit_price: Decimal
    discount_amount: Decimal
    hoodie_unit_price: Decimal
    hoodie_discount_amount: Decimal

    def public_metadata(self):
        return {
            "hoodie_key": self.hoodie_key, "tee_key": self.tee_key,
            "qty": self.qty, "kind": self.kind,
            "tee_unit_price": float(self.tee_unit_price),
            "discount_amount": float(self.discount_amount),
            "hoodie_unit_price": float(self.hoodie_unit_price),
            "hoodie_discount_amount": float(self.hoodie_discount_amount),
            "tee_discount_amount": float(self.discount_amount - self.hoodie_discount_amount),
        }


def is_bundle_product_eligible(product, policy):
    return (
        not policy["is_brigade"]
        and policy["garment"] in {"tee", "hoodie"}
        and product.pk != 110
        and product.slug != "futbolka-boiova-kvitochka"
    )


def _identity(key, cart, prepared):
    product, _policy, _qty, _unit, _retail = prepared[key]
    item = cart[key]
    return (product.pk, item.get("color_variant_id") or 0, item.get("size") or "",
            item.get("fit_option_code") or item.get("fit_code") or item.get("fit") or "",
            json.dumps(item.get("option_values") or {}, sort_keys=True), str(key))


def allocate_garment_bundles(cart, prepared, variants=None):
    """Maximum actual savings, with deterministic design/identity tie breaks.

    Capacitated minimum-cost flow keeps one hoodie unit paired to one tee
    unit. Residual edges allow a prior pair to move when another allocation
    saves more; first-found or same-design greedy matching can miss that.
    """
    from storefront.services.garment_bundle_catalog import is_same_design

    keys = sorted(prepared, key=lambda key: _identity(key, cart, prepared))
    hoodies = [key for key in keys if prepared[key][1]["garment"] == "hoodie" and is_bundle_product_eligible(prepared[key][0], prepared[key][1])]
    tees = [key for key in keys if prepared[key][1]["garment"] == "tee" and is_bundle_product_eligible(prepared[key][0], prepared[key][1])]
    if not hoodies or not tees:
        return ()
    candidates = []
    for hoodie_key in hoodies:
        hoodie = prepared[hoodie_key][0]
        hoodie_standalone = prepared[hoodie_key][3]
        hoodie_offer = hoodie_offer_price(hoodie_standalone)
        hoodie_savings = hoodie_standalone - hoodie_offer
        for tee_key in tees:
            tee, _policy, _qty, standalone, retail = prepared[tee_key]
            same = is_same_design(tee, hoodie)
            variant_id = cart[tee_key].get('color_variant_id')
            variant = (variants or {}).get(int(variant_id)) if variant_id else None
            offer = tee_offer_price(tee, standalone, retail, same_design=same, variant_base=variant.price_override if variant else None)
            savings = standalone - offer + hoodie_savings
            if savings > 0:
                candidates.append((hoodie_key, tee_key, "same_design" if same else "other_design", offer, savings, hoodie_offer, hoodie_savings))
    if not candidates:
        return ()
    candidates.sort(key=lambda row: (row[2] != "same_design", _identity(row[0], cart, prepared), _identity(row[1], cart, prepared)))
    source = 0
    h_nodes = {key: index + 1 for index, key in enumerate(hoodies)}
    t_nodes = {key: len(hoodies) + index + 1 for index, key in enumerate(tees)}
    sink = len(hoodies) + len(tees) + 1
    graph = [[] for _ in range(sink + 1)]

    def edge(start, end, capacity, cost):
        forward = [end, len(graph[end]), capacity, cost]
        reverse = [start, len(graph[start]), 0, -cost]
        graph[start].append(forward)
        graph[end].append(reverse)
        return forward

    for key in hoodies:
        edge(source, h_nodes[key], prepared[key][2], 0)
    for key in tees:
        edge(t_nodes[key], sink, prepared[key][2], 0)
    max_pairs = min(sum(prepared[key][2] for key in hoodies), sum(prepared[key][2] for key in tees))
    scale = (2 * len(candidates) + 1) * max_pairs + 1
    refs = []
    potentials = [0] * len(graph)
    for rank, row in enumerate(candidates):
        hoodie_key, tee_key, kind, _offer, savings, _hoodie_offer, _hoodie_savings = row
        cost = -int(savings * 100) * scale + rank
        capacity = min(prepared[hoodie_key][2], prepared[tee_key][2])
        ref = edge(h_nodes[hoodie_key], t_nodes[tee_key], capacity, cost)
        refs.append((row, ref, capacity))
        potentials[t_nodes[tee_key]] = min(potentials[t_nodes[tee_key]], cost)
    potentials[sink] = min(potentials[t_nodes[key]] for key in tees)
    while True:
        distances = [None] * len(graph)
        previous = [None] * len(graph)
        distances[source] = 0
        queue = [(0, source)]
        while queue:
            distance, node = heapq.heappop(queue)
            if distances[node] != distance:
                continue
            for index, candidate in enumerate(graph[node]):
                target, _reverse, capacity, cost = candidate
                if capacity <= 0:
                    continue
                value = distance + cost + potentials[node] - potentials[target]
                if distances[target] is None or value < distances[target]:
                    distances[target] = value
                    previous[target] = (node, index)
                    heapq.heappush(queue, (value, target))
        if distances[sink] is None or distances[sink] + potentials[sink] - potentials[source] >= 0:
            break
        for node, distance in enumerate(distances):
            if distance is not None:
                potentials[node] += distance
        amount = max_pairs
        node = sink
        while node != source:
            parent, index = previous[node]
            amount = min(amount, graph[parent][index][2])
            node = parent
        node = sink
        while node != source:
            parent, index = previous[node]
            candidate = graph[parent][index]
            candidate[2] -= amount
            graph[node][candidate[1]][2] += amount
            node = parent
    result = []
    for row, ref, capacity in refs:
        qty = capacity - ref[2]
        if qty:
            hoodie_key, tee_key, kind, offer, savings, hoodie_offer, hoodie_savings = row
            result.append(BundleAllocation(hoodie_key, tee_key, qty, kind, offer, (savings * qty).quantize(MONEY), hoodie_offer, (hoodie_savings * qty).quantize(MONEY)))
    return tuple(result)
