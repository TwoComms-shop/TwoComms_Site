"""Read-only category discovery from published product/category relations.

Category links describe the broader public category, not matching stock,
prices, print similarity, a promised product count or delivery availability.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from urllib.parse import urlsplit

from django.conf import settings
from django.urls import reverse
from django.utils.translation import override

from management.services.ig_revision_outbox import _digest

_SLUG = re.compile(r"[-a-zA-Z0-9_]{1,100}")
_KEYS = frozenset({"product_id", "category_id", "slug", "name", "locale", "url"})
_LABELS = {
    "hoodie": {"uk": "Всі худі", "ru": "Все худи", "en": "All hoodies"},
    "tshirts": {"uk": "Всі футболки", "ru": "Все футболки", "en": "All T-shirts"},
    "long-sleeve": {"uk": "Всі лонгсліви", "ru": "Все лонгсливы", "en": "All longsleeves"},
}
_GENERIC = {"uk": "Вся категорія", "ru": "Вся категория", "en": "Full category"}
_HEAD = {"uk": "Кілька моделей для початку. Повний вибір у категорії:",
    "ru": "Несколько моделей для начала. Полный выбор в категории:",
    "en": "A few models to start with. Explore the full category:"}


def _locale(locale):
    return "uk" if locale == "ua" or locale not in {"uk", "ru", "en"} else locale


def category_url(slug, locale="uk"):
    """Reverse the real storefront route and accept only configured HTTPS origin."""
    if not isinstance(slug, str) or not _SLUG.fullmatch(slug):
        raise ValueError("catalog_category_slug_invalid")
    base = str(getattr(settings, "SITE_BASE_URL", "") or "https://twocomms.shop").rstrip("/")
    parsed = urlsplit(base)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
        or parsed.query or parsed.fragment or parsed.path or parsed.port not in {None, 443}):
        raise ValueError("catalog_category_origin_invalid")
    with override(_locale(locale)):
        path = reverse("catalog_by_cat", kwargs={"cat_slug": slug})
    if not path.startswith("/") or path.startswith("//") or urlsplit(path).netloc:
        raise ValueError("catalog_category_route_invalid")
    return base + path


def category_button_label(category):
    locale = _locale(category.get("locale"))
    return _LABELS.get(category.get("slug"), _GENERIC).get(locale, _GENERIC[locale])


def validate_category(category, *, product_id):
    """Pure wire validation, including exact route/origin; performs no DB reads."""
    try:
        return (isinstance(category, dict) and set(category) == _KEYS
            and type(category["product_id"]) is int and category["product_id"] == product_id
            and type(category["category_id"]) is int and category["category_id"] > 0
            and isinstance(category["name"], str) and 0 < len(category["name"]) <= 100
            and category["locale"] in {"uk", "ru", "en"}
            and category["url"] == category_url(category["slug"], category["locale"]))
    except (KeyError, ValueError, TypeError):
        return False


def capture_product_category(product_id, locale="uk"):
    from storefront.models import Product, ProductStatus

    if type(product_id) is not int or product_id <= 0:
        raise ValueError("catalog_category_product_invalid")
    product = Product.objects.filter(pk=product_id, status=ProductStatus.PUBLISHED,
        category__is_active=True).select_related("category").only("id", "category_id",
            "category__id", "category__name", "category__slug", "category__is_active").first()
    if product is None:
        raise ValueError("catalog_category_unavailable")
    category = product.category
    result = {"product_id": product.pk, "category_id": category.pk,
        "slug": category.slug, "name": category.name, "locale": _locale(locale),
        "url": category_url(category.slug, locale)}
    if not validate_category(result, product_id=product_id):
        raise ValueError("catalog_category_unavailable")
    return result


def category_reference(category):
    if not validate_category(category, product_id=category.get("product_id")):
        raise ValueError("catalog_category_invalid")
    return {"product_id": category["product_id"], "category_id": category["category_id"],
        "locale": category["locale"],
        "name_digest": _digest(category["name"]), "url_digest": _digest(category["url"])}


def validate_current_category(value, *, product_id):
    """Recheck active category, product ownership, name, slug and reversed URL."""
    try:
        if not isinstance(value, dict) or value.get("product_id") != product_id:
            return False
        current = capture_product_category(product_id, value.get("locale"))
        return value == (current if set(value) == _KEYS else category_reference(current))
    except (KeyError, TypeError, ValueError):
        return False


def _unique_categories(categories):
    unique = {}
    for category in categories:
        if not validate_category(category, product_id=category.get("product_id")):
            raise ValueError("catalog_category_invalid")
        unique.setdefault(category["category_id"], category)
    return tuple(unique.values())


def shortlist_copy(categories, locale="uk", *, links=False):
    """Keep a bounded comparison distinct from the category's full assortment."""
    locale = _locale(locale)
    categories = _unique_categories(categories)
    if not categories:
        return {"uk": "Це кілька моделей для початку.", "ru": "Это несколько моделей для начала.",
            "en": "These are a few models to start with."}[locale]
    if links:
        return _HEAD[locale] + "\n" + "\n".join(
            f"{category_button_label(category) if category['slug'] in _LABELS else category['name']}: {category['url']}"
            for category in categories)
    labels = ", ".join(f"«{category_button_label(category)}»" for category in categories)
    return {"uk": f"Це кілька моделей для початку. Повний вибір у категорії — за кнопкою {labels} на картках.",
        "ru": f"Это несколько моделей для начала. Полный выбор в категории — по кнопке {labels} на карточках.",
        "en": f"These are a few models to start with. Explore the full category using {labels} on the cards."}[locale]


@dataclass(frozen=True)
class CatalogCategoryDiscovery:
    categories: tuple = ()
    urls: tuple = ()
    copy_hint: str = ""
    link_hint: str = ""


def catalog_category_discovery(product_ids, locale="uk"):
    """Legacy/prose adapter: bounded real IDs; unknown rows grant no URL."""
    from management.services.ig_catalog_media import parse_product_ids

    if isinstance(product_ids, (tuple, list)):
        ids = product_ids if all(type(value) is int and value > 0 for value in product_ids) else ()
    else:
        ids = parse_product_ids(product_ids)
    categories = []
    for product_id in tuple(dict.fromkeys(ids or ()))[:12]:
        try:
            categories.append(capture_product_category(product_id, locale))
        except ValueError:
            continue
    categories = _unique_categories(categories)
    return CatalogCategoryDiscovery(categories, tuple(row["url"] for row in categories),
        shortlist_copy(categories, locale), shortlist_copy(categories, locale, links=True))


def append_catalog_discovery_hint(reply, product_ids, locale="uk"):
    """Return (preserved reply + verified category hint, exact allowed URLs)."""
    discovery = catalog_category_discovery(product_ids, locale)
    text = str(reply or "").strip()
    if discovery.urls and all(url in text for url in discovery.urls):
        return text, discovery.urls
    hint = discovery.link_hint if discovery.urls else discovery.copy_hint
    return f"{text}\n{hint}".strip(), discovery.urls


def category_urls_for_control(control, client=None, *, locale=None):
    """URL approval derives only from actual published-product category rows."""
    if not isinstance(control, dict):
        return ()
    raw = control.get("show_products")
    if raw is True:
        raw = [getattr(client, "current_product_id", None)]
    if raw in (None, ""):
        product_id = control.get("product")
        raw = [int(product_id)] if str(product_id or "").isdigit() else ()
    return catalog_category_discovery(raw, locale or getattr(client, "language", "uk")).urls


def catalog_discovery_guidance():
    return ("[CATALOG BREADTH]\nProduct cards are a small starting shortlist, never the complete category. "
        "Each card has a server-verified web button for the full category; say that the customer can "
        "browse more models there. That destination covers the category, not necessarily the same print, "
        "colour or size criteria. Never invent URLs, product counts, category availability or stock. "
        "The bounded shortlist and paging do not prove that the assortment ends. Preserve and answer "
        "every other customer question in this turn; discovery is additional navigation, not a replacement answer.")
