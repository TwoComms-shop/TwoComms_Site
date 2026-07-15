"""Зв'язування витринних товарів з позиціями складу.

Логіка: на основі OrderItem (product.category + size + color) знаходимо
відповідні StockItem'и. Підкатегорія вручну обирається адміном під час
списання — тут ми просто повертаємо всі можливі варіанти.
"""
from __future__ import annotations

from typing import Iterable

from warehouse.models import Print, StockItem, StorageCategory


_STOCK_ORDER = (
    "subcategory__category__order",
    "subcategory__order",
    "subcategory__name",
    "color__name",
    "size",
)


def all_active_stock_items() -> list[StockItem]:
    """Усі активні складські позиції (для повного вибору під час списання).

    Використовується, щоб оператор завжди міг обрати потрібний крій/колір/розмір
    вручну, навіть якщо авто-матчинг не дав точного збігу.
    """
    return list(
        StockItem.objects.select_related("subcategory__category", "color")
        .filter(subcategory__is_active=True, subcategory__category__is_active=True)
        .order_by(*_STOCK_ORDER)
    )


def group_stock_items_by_category(items) -> list[dict]:
    """Групує StockItem'и за складською категорією для <optgroup> у формі списання."""
    groups: list[dict] = []
    index: dict[int, dict] = {}
    for it in items:
        cat = it.subcategory.category
        bucket = index.get(cat.id)
        if bucket is None:
            bucket = {"category_id": cat.id, "category_name": cat.name, "items": []}
            index[cat.id] = bucket
            groups.append(bucket)
        bucket["items"].append(it)
    return groups


def find_stock_items_for_order_item(order_item) -> list[StockItem]:
    """Повертає список StockItem, що найкраще підходять для OrderItem.

    Каскадний (graceful) матчинг — повертає перший непорожній рівень:
    1. категорія(прив'язка) + розмір + колір — точний збіг;
    2. категорія + розмір;
    3. категорія + колір;
    4. лише категорія;
    і якщо ``linked_storefront_category`` ніде не задано (категорії не
    прив'язані) — той самий каскад, але вже по всьому складу, щоб оператор
    усе одно отримав осмислений дефолт замість порожнечі.

    Повний перелік для ручного вибору дає :func:`all_active_stock_items`.
    """
    base = StockItem.objects.select_related("subcategory__category", "color").filter(
        subcategory__is_active=True,
        subcategory__category__is_active=True,
    )

    storefront_category = getattr(order_item.product, "category", None)
    category_qs = base
    if storefront_category is not None:
        linked = base.filter(
            subcategory__category__linked_storefront_category=storefront_category
        )
        # Звужуємо до прив'язаної категорії лише якщо там реально щось є.
        if linked.exists():
            category_qs = linked

    size = (order_item.size or "").strip()
    color_variant = getattr(order_item, "color_variant", None)
    color_id = (
        color_variant.color_id if (color_variant and color_variant.color_id) else None
    )

    def _apply(qs, *, use_size: bool, use_color: bool):
        if use_size and size:
            qs = qs.filter(size__iexact=size)
        if use_color and color_id:
            qs = qs.filter(color_id=color_id)
        return list(qs.order_by(*_STOCK_ORDER))

    # Fable5 can explicitly bind a colour+fit to a warehouse blank family.
    # Prefer it for Telegram/manual write-off, while preserving the complete
    # graceful matcher below when the linked family has no stock rows yet.
    fit_code = str(
        getattr(order_item, "fit_option_code", "")
        or getattr(order_item, "fit", "")
        or ""
    ).strip().lower()
    if color_variant is not None and fit_code:
        try:
            from fable5.models import VariantBlankLink

            blank_link = (
                VariantBlankLink.objects
                .filter(variant=color_variant, option_key=f"fit={fit_code}")
                .first()
            )
        except Exception:
            blank_link = None
        if blank_link is not None:
            preferred_qs = base.filter(
                subcategory_id=blank_link.storage_subcategory_id
            )
            for use_size, use_color in (
                (True, True),
                (True, False),
                (False, True),
                (False, False),
            ):
                result = _apply(
                    preferred_qs,
                    use_size=use_size,
                    use_color=use_color,
                )
                if result:
                    return result

    for use_size, use_color in (
        (True, True),
        (True, False),
        (False, True),
        (False, False),
    ):
        result = _apply(category_qs, use_size=use_size, use_color=use_color)
        if result:
            return result
    return []


def find_default_print_for_order_item(order_item) -> Print | None:
    """Підставляє принт за замовчуванням для позиції замовлення.

    Шукає Print, що в default_products містить order_item.product.
    """
    return (
        Print.objects.filter(
            default_products=order_item.product, is_active=True
        )
        .order_by("name")
        .first()
    )


# Порядок розташувань для зручного сортування (грудь → спина → рукав → ...).
_PLACEMENT_ORDER = {
    "chest": 0,
    "back": 1,
    "sleeve": 2,
    "pocket": 3,
    "full": 4,
    "other": 5,
    "": 6,
}


def find_prints_for_order_item(order_item) -> list[Print]:
    """Усі активні принти, прив'язані до товару позиції замовлення.

    На відміну від :func:`find_default_print_for_order_item`, повертає
    ПОВНИЙ список (наприклад: лого на грудь + дизайн на спину), щоб
    оператор міг списати всі принти одного виробу за раз. Сортуються
    за розташуванням (грудь → спина → рукав → …), потім за назвою.
    """
    if order_item is None or order_item.product_id is None:
        return []
    prints = (
        Print.objects.filter(default_products=order_item.product, is_active=True)
        .prefetch_related("color_variants", "color_variants__colors")
        .distinct()
    )
    return sorted(
        prints,
        key=lambda p: (_PLACEMENT_ORDER.get(p.placement or "", 6), p.name.lower()),
    )


def stock_matrix_for_category(category: StorageCategory) -> dict:
    """Побудувати матрицю остатків для категорії.

    Повертає структуру:
        {
            "sizes": ["S", "M", "L", ...],
            "subcategories": [
                {
                    "id": int, "name": str, "slug": str,
                    "rows_by_color": [
                        {
                            "color_id": int|None,
                            "color_name": str,
                            "color_hex": str,
                            "by_size": {"S": qty, "M": qty, ...},
                            "row_total": int,
                        },
                        ...
                    ],
                    "subtotal_by_size": {"S": qty, ...},
                    "subtotal": int,
                },
                ...
            ],
            "total_by_size": {"S": qty, ...},
            "total": int,
        }
    """
    items = (
        StockItem.objects.filter(subcategory__category=category)
        .select_related("subcategory", "color")
        .prefetch_related("subcategory__colors")
        .order_by("subcategory__order", "subcategory__name", "color__name", "size")
    )

    sizes_set: set[str] = set()
    subcat_buckets: dict[int, dict] = {}
    for item in items:
        sub = item.subcategory
        if sub.id not in subcat_buckets:
            allowed = list(
                sub.colors.all().values("id", "name", "primary_hex", "secondary_hex")
            )
            subcat_buckets[sub.id] = {
                "id": sub.id,
                "name": sub.name,
                "slug": sub.slug,
                "allowed_colors": [
                    {
                        "id": c["id"],
                        "name": c["name"] or c["primary_hex"],
                        "hex": c["primary_hex"],
                        "secondary_hex": c.get("secondary_hex") or "",
                    }
                    for c in allowed
                ],
                "rows": {},  # (color_id, color_name, hex) -> {size: qty}
            }
        sizes_set.add(item.size)
        color_id = item.color_id
        color_name = item.color_display
        color_hex = item.color.primary_hex if item.color else ""
        key = (color_id, color_name, color_hex)
        bucket = subcat_buckets[sub.id]["rows"].setdefault(
            key, {"_total": 0, "_sizes": {}, "_costs": {}, "_last_costs": {}, "_trends": {}}
        )
        bucket["_sizes"][item.size] = bucket["_sizes"].get(item.size, 0) + item.quantity
        # cost_price per size (per unique StockItem)
        bucket["_costs"][item.size] = str(item.cost_price)
        bucket["_last_costs"][item.size] = str(item.last_cost_price)
        bucket["_trends"][item.size] = item.price_trend
        bucket["_total"] += item.quantity

    sizes = _sort_sizes(sizes_set)

    subcategories = []
    total_by_size = {s: 0 for s in sizes}
    grand_total = 0
    for sub_id, sub_data in subcat_buckets.items():
        rows_by_color = []
        subtotal_by_size = {s: 0 for s in sizes}
        subtotal = 0
        for (color_id, color_name, color_hex), bucket in sub_data["rows"].items():
            by_size = {s: bucket["_sizes"].get(s, 0) for s in sizes}
            by_size_cost = {s: bucket["_costs"].get(s, "0") for s in sizes}
            by_size_last_cost = {s: bucket["_last_costs"].get(s, "0") for s in sizes}
            by_size_trend = {s: bucket["_trends"].get(s, "") for s in sizes}
            row_total = bucket["_total"]
            for s, q in by_size.items():
                subtotal_by_size[s] += q
                total_by_size[s] += q
            subtotal += row_total
            grand_total += row_total
            rows_by_color.append(
                {
                    "color_id": color_id,
                    "color_name": color_name,
                    "color_hex": color_hex,
                    "by_size": by_size,
                    "by_size_cost": by_size_cost,
                    "by_size_last_cost": by_size_last_cost,
                    "by_size_trend": by_size_trend,
                    "row_total": row_total,
                }
            )
        # also include subcategories that have no items? — skipped to keep matrix dense
        subcategories.append(
            {
                "id": sub_id,
                "name": sub_data["name"],
                "slug": sub_data["slug"],
                "allowed_colors": sub_data["allowed_colors"],
                "rows_by_color": rows_by_color,
                "subtotal_by_size": subtotal_by_size,
                "subtotal": subtotal,
            }
        )

    return {
        "sizes": sizes,
        "subcategories": subcategories,
        "total_by_size": total_by_size,
        "total": grand_total,
    }


_SIZE_ORDER = [
    "XXS",
    "XS",
    "S",
    "M",
    "L",
    "XL",
    "XXL",
    "XXXL",
    "4XL",
    "5XL",
]


def _sort_sizes(sizes: Iterable[str]) -> list[str]:
    known, others = [], []
    upper_to_orig: dict[str, str] = {s.upper(): s for s in sizes}
    for s in _SIZE_ORDER:
        if s in upper_to_orig:
            known.append(upper_to_orig[s])
    rest = [s for s in sizes if s.upper() not in _SIZE_ORDER]
    others = sorted(rest)
    return known + others
