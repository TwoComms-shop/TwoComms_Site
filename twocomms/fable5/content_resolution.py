"""Resolve sparse Fable 5 content without duplicating canonical products."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .models import (
    ProductOptionProfile,
    VariantCombinationProfile,
    VariantDetails,
)


SUPPORTED_LANGUAGES = ("uk", "ru", "en")
CONTENT_FIELDS = (
    "display_name",
    "short_description",
    "full_description",
    "marketing_text",
    "seo_title",
    "seo_description",
    "seo_keywords",
    "og_title",
    "og_description",
)
TOKEN_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,49}$")

PRODUCT_FIELD_CANDIDATES = {
    "display_name": ("title",),
    "short_description": ("short_description",),
    "full_description": ("full_description", "description"),
    "marketing_text": ("details_text", "full_description", "description"),
    "seo_title": ("seo_title", "title"),
    "seo_description": ("seo_description", "short_description", "full_description"),
    "seo_keywords": ("seo_keywords",),
    "og_title": ("seo_title", "title"),
    "og_description": ("seo_description", "short_description", "full_description"),
}

LEGACY_VARIANT_FIELDS = {
    "display_name": "display_name",
    "marketing_text": "marketing_html",
    "seo_title": "seo_title",
    "seo_description": "seo_description",
    "seo_keywords": "seo_keywords",
}


@dataclass(frozen=True)
class _Layer:
    source: str
    rows: dict[str, Any]
    owner: Any


def _clean_token(value: Any) -> str:
    token = str(value or "").strip().lower()
    if not TOKEN_RE.fullmatch(token):
        raise ValueError("Некоректний код опції")
    return token


def normalize_option_values(raw: dict | None) -> dict[str, str]:
    """Return a stable, validated option mapping suitable for keys and JSON."""

    if raw in (None, {}):
        return {}
    if not isinstance(raw, dict):
        raise ValueError("Опції мають бути об'єктом")
    normalized = {
        _clean_token(key): _clean_token(value)
        for key, value in raw.items()
    }
    return dict(sorted(normalized.items()))


def build_combination_key(raw: dict | None) -> str:
    values = normalize_option_values(raw)
    return ";".join(f"{key}={value}" for key, value in values.items())


def _language(value: str | None) -> str:
    code = str(value or "uk").lower()
    return code if code in SUPPORTED_LANGUAGES else "uk"


def _nonempty(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _localized_rows(owner: Any) -> dict[str, Any]:
    if owner is None:
        return {}
    return {row.lang: row for row in owner.i18n.all()}


def _option_candidate_keys(values: dict[str, str]) -> list[str]:
    if not values:
        return []
    full_key = build_combination_key(values)
    single_keys = [f"{key}={value}" for key, value in values.items()]
    return [full_key, *[key for key in single_keys if key != full_key]]


def _load_layers(product, variant, values: dict[str, str]):
    combination_key = build_combination_key(values)
    exact = None
    if variant is not None and combination_key:
        prefetched = getattr(variant, "_prefetched_objects_cache", {}).get(
            "fable5_combinations"
        )
        if prefetched is not None:
            exact = next(
                (
                    profile for profile in prefetched
                    if profile.combination_key == combination_key and profile.is_active
                ),
                None,
            )
        else:
            exact = (
                VariantCombinationProfile.objects
                .filter(
                    variant=variant,
                    combination_key=combination_key,
                    is_active=True,
                )
                .prefetch_related("i18n")
                .first()
            )

    option_keys = _option_candidate_keys(values)
    prefetched_options = getattr(product, "_prefetched_objects_cache", {}).get(
        "fable5_option_profiles"
    )
    if prefetched_options is not None:
        option_rows = (
            profile for profile in prefetched_options
            if profile.option_key in option_keys and profile.is_active
        )
    else:
        option_rows = (
            ProductOptionProfile.objects
            .filter(product=product, option_key__in=option_keys, is_active=True)
            .prefetch_related("i18n")
        )
    options_by_key = {profile.option_key: profile for profile in option_rows}
    options = [options_by_key[key] for key in option_keys if key in options_by_key]

    details = None
    if variant is not None:
        cached_details = getattr(variant, "_state", None)
        fields_cache = getattr(cached_details, "fields_cache", {})
        if "fable5_details" in fields_cache:
            details = fields_cache.get("fable5_details")
        else:
            details = (
                VariantDetails.objects
                .filter(variant=variant)
                .prefetch_related("i18n")
                .first()
            )

    layers: list[_Layer] = []
    if exact is not None:
        layers.append(_Layer("combination", _localized_rows(exact), exact))
    layers.extend(
        _Layer(f"option:{profile.option_key}", _localized_rows(profile), profile)
        for profile in options
    )
    if details is not None:
        layers.append(_Layer("color", _localized_rows(details), details))
    return layers, exact, options, details, combination_key


def _raw_model_value(instance, field_name: str) -> str:
    return _nonempty(getattr(instance, "__dict__", {}).get(field_name))


def _product_value(product, field: str, lang: str | None = None) -> str:
    for candidate in PRODUCT_FIELD_CANDIDATES[field]:
        field_name = f"{candidate}_{lang}" if lang else candidate
        value = _raw_model_value(product, field_name)
        if value:
            if lang and value == _raw_model_value(product, candidate):
                continue
            return value
    return ""


def _resolve_content_field(product, layers, details, field: str, lang: str):
    language_order = [lang] if lang == "uk" else [lang, "uk"]
    for index, language in enumerate(language_order):
        for layer in layers:
            row = layer.rows.get(language)
            value = _nonempty(getattr(row, field, "")) if row is not None else ""
            if value:
                return value, f"{layer.source}:{language}"

        if language == "uk" and details is not None:
            legacy_field = LEGACY_VARIANT_FIELDS.get(field)
            legacy_value = _nonempty(getattr(details, legacy_field, "")) if legacy_field else ""
            if legacy_value:
                return legacy_value, "color:legacy"

        product_value = _product_value(product, field, language)
        if product_value and (language == lang or index == len(language_order) - 1):
            return product_value, f"product:{language}"

    canonical = _product_value(product, field)
    return canonical, "product:canonical"


def _resolve_sparse_scalar(exact, options, details, field: str, default: Any = ""):
    if exact is not None:
        value = getattr(exact, field, None)
        if value not in (None, ""):
            return value, "combination"
    for option in options:
        value = getattr(option, field, None)
        if value not in (None, ""):
            return value, f"option:{option.option_key}"
    if details is not None:
        value = getattr(details, field, None)
        if value not in (None, ""):
            return value, "color:legacy"
    return default, "product:canonical"


def resolve_merchandising_context(
    product,
    variant=None,
    option_values: dict | None = None,
    lang: str = "uk",
) -> dict:
    """Resolve effective content and expose where each value came from."""

    language = _language(lang)
    values = normalize_option_values(option_values)
    layers, exact, options, details, combination_key = _load_layers(
        product,
        variant,
        values,
    )
    context: dict[str, Any] = {
        "language": language,
        "option_values": values,
        "combination_key": combination_key,
        "sources": {},
    }
    for field in CONTENT_FIELDS:
        value, source = _resolve_content_field(
            product,
            layers,
            details,
            field,
            language,
        )
        context[field] = value
        context["sources"][field] = source

    price_delta, price_source = _resolve_sparse_scalar(
        exact,
        options,
        details,
        "price_delta",
        0,
    )
    price_reason, reason_source = _resolve_sparse_scalar(
        exact,
        options,
        details,
        "price_delta_reason",
        "",
    )
    youtube_url, video_source = _resolve_sparse_scalar(
        exact,
        options,
        details,
        "youtube_url",
        _nonempty(getattr(product, "video_url", "")),
    )
    context.update(
        {
            "price_delta": int(price_delta or 0),
            "price_delta_reason": price_reason,
            "youtube_url": youtube_url,
        }
    )
    context["sources"].update(
        {
            "price_delta": price_source,
            "price_delta_reason": reason_source,
            "youtube_url": video_source,
        }
    )
    return context


def resolve_variant_text(
    variant,
    field: str,
    lang: str = "uk",
    option_values: dict | None = None,
) -> str:
    if field not in CONTENT_FIELDS:
        raise ValueError(f"Непідтримуване поле: {field}")
    return resolve_merchandising_context(
        variant.product,
        variant=variant,
        option_values=option_values,
        lang=lang,
    )[field]


__all__ = [
    "CONTENT_FIELDS",
    "build_combination_key",
    "normalize_option_values",
    "resolve_merchandising_context",
    "resolve_variant_text",
]
