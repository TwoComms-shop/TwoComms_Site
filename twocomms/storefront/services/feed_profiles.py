"""Validation primitives shared by marketplace feed profiles and staff forms."""

from __future__ import annotations

from copy import deepcopy
from numbers import Real

from django.core.exceptions import ValidationError
from storefront.services.feed_registry import FEED_ADAPTERS, reserved_feed_slugs


SYSTEM_FEED_ADAPTERS = {key: definition.key for key, definition in FEED_ADAPTERS.items()}
RESERVED_FEED_SLUGS = reserved_feed_slugs()

_RULE_KEYS = frozenset({"filters", "availability", "images", "text"})
_FILTER_KEYS = frozenset({
    "category_ids",
    "include_product_ids",
    "exclude_product_ids",
    "min_image_count",
    "price_min",
    "price_max",
    "search_keywords",
    "dropship_only",
})
_AVAILABILITY_KEYS = frozenset({"mode", "quantity"})
_IMAGE_KEYS = frozenset({"mode", "max_count"})
_TEXT_KEYS = frozenset({"language"})
_AVAILABILITY_MODES = frozenset({"inherit", "force_in_stock", "force_out_of_stock"})
_IMAGE_MODES = frozenset({"variant_first", "newest_first", "main_first", "selected"})
_LANGUAGES = frozenset({"uk", "ru", "uk_ru"})


def _validation_error(message: str) -> ValidationError:
    return ValidationError({"rules": message})


def _ensure_mapping(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise _validation_error(f"{name} має бути JSON-об'єктом.")
    return value


def _ensure_known_keys(value: dict[str, object], allowed: frozenset[str], name: str) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise _validation_error(f"Невідомі ключі {name}: {', '.join(sorted(unknown))}.")


def _ensure_id_list(value: object, name: str) -> None:
    if not isinstance(value, list) or any(not isinstance(item, int) or isinstance(item, bool) or item <= 0 for item in value):
        raise _validation_error(f"{name} має бути списком додатних ID.")


def _ensure_non_negative_integer(value: object, name: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise _validation_error(f"{name} має бути невід'ємним цілим числом.")


def validate_feed_rules(value: object) -> dict[str, object]:
    """Validate the version-one editable feed rules without coercing them."""
    rules = _ensure_mapping(value, "rules")
    _ensure_known_keys(rules, _RULE_KEYS, "rules")

    filters = rules.get("filters")
    if filters is not None:
        filters = _ensure_mapping(filters, "filters")
        _ensure_known_keys(filters, _FILTER_KEYS, "filters")
        for key in ("category_ids", "include_product_ids", "exclude_product_ids"):
            if key in filters:
                _ensure_id_list(filters[key], f"filters.{key}")
        if "min_image_count" in filters:
            _ensure_non_negative_integer(filters["min_image_count"], "filters.min_image_count")
        for key in ("price_min", "price_max"):
            if key in filters and (
                not isinstance(filters[key], Real)
                or isinstance(filters[key], bool)
                or filters[key] < 0
            ):
                raise _validation_error(f"filters.{key} має бути невід'ємним числом.")
        if "price_min" in filters and "price_max" in filters and filters["price_min"] > filters["price_max"]:
            raise _validation_error("filters.price_min не може бути більшим за filters.price_max.")
        if "search_keywords" in filters and (
            not isinstance(filters["search_keywords"], list)
            or any(not isinstance(item, str) or not item.strip() for item in filters["search_keywords"])
        ):
            raise _validation_error("filters.search_keywords має бути списком непорожніх рядків.")
        if "dropship_only" in filters and not isinstance(filters["dropship_only"], bool):
            raise _validation_error("filters.dropship_only має бути boolean.")

    availability = rules.get("availability")
    if availability is not None:
        availability = _ensure_mapping(availability, "availability")
        _ensure_known_keys(availability, _AVAILABILITY_KEYS, "availability")
        if availability.get("mode", "inherit") not in _AVAILABILITY_MODES:
            raise _validation_error("availability.mode має містити дозволений режим.")
        if "quantity" in availability:
            _ensure_non_negative_integer(availability["quantity"], "availability.quantity")

    images = rules.get("images")
    if images is not None:
        images = _ensure_mapping(images, "images")
        _ensure_known_keys(images, _IMAGE_KEYS, "images")
        if images.get("mode", "main_first") not in _IMAGE_MODES:
            raise _validation_error("images.mode має містити дозволений режим.")
        if "max_count" in images:
            _ensure_non_negative_integer(images["max_count"], "images.max_count")
            if images["max_count"] == 0:
                raise _validation_error("images.max_count має бути більшим за нуль.")

    text = rules.get("text")
    if text is not None:
        text = _ensure_mapping(text, "text")
        _ensure_known_keys(text, _TEXT_KEYS, "text")
        if text.get("language", "uk") not in _LANGUAGES:
            raise _validation_error("text.language має містити дозволену мову.")

    return deepcopy(rules)


def deep_merge_rules(base: dict[str, object], override: dict[str, object]) -> dict[str, object]:
    """Merge the shallow versioned rule shape without mutating model JSON."""
    result = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge_rules(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def resolve_feed_rules(feed) -> dict[str, object]:
    """Resolve defaults then parent profiles in a bounded, deterministic order."""
    try:
        definition = FEED_ADAPTERS[feed.adapter]
    except KeyError as exc:
        raise ValidationError({"adapter": "Непідтримуваний адаптер фіда."}) from exc

    chain = []
    seen = set()
    current = feed
    while current is not None:
        identity = current.pk if current.pk is not None else id(current)
        if identity in seen or len(chain) >= 12:
            raise ValidationError({"parent": "Неможливий цикл залежностей фідів."})
        if current.adapter != feed.adapter:
            raise ValidationError({"parent": "Батьківський фід має використовувати той самий адаптер."})
        seen.add(identity)
        chain.append(current)
        current = current.parent

    rules = deepcopy(definition.default_rules)
    for profile in reversed(chain):
        rules = deep_merge_rules(rules, validate_feed_rules(profile.rules))
    return rules
