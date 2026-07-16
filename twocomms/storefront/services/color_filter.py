"""
Phase 9 — Catalog colour filter.

Given a product queryset and the current request, returns:
  * the list of selected colour slugs (parsed from ``?color=``);
  * the queryset filtered by those slugs (OR-match);
  * the chips (one per distinct ``ProductColorVariant.slug``) for the UI,
    each carrying a toggle URL that adds or removes itself from the
    current selection while preserving every other query parameter.

Slugs are global identifiers — ``ProductColorVariant.slug`` is unique
per product, but the *value* (e.g. ``black``, ``coyote``) is shared
across the catalogue thanks to the EN translation map from Phase 7.1,
which is exactly what we want for faceted browsing.
"""
from __future__ import annotations

from collections import defaultdict
from functools import wraps
from typing import Iterable, List, Dict, Any
from urllib.parse import urlencode

from django.apps import apps
from django.core.cache import cache
from django.db import DatabaseError
from django.db.models import QuerySet
from django.http import HttpResponsePermanentRedirect
from django.utils.translation import gettext_lazy as _


# 2026-05-16 — Phase 17v. The Color model is shared with admin tools and
# does not use ``django-modeltranslation`` (Color rows store a single
# Ukrainian display name in the ``name`` column). To keep the colour
# filter chips fully localised we pre-register the catalogue colours as
# gettext msgids; ``_translate_color_label`` looks them up at render
# time and falls back to the raw DB value (humanised slug) for unknown
# colours. The mapping is intentionally generous — extra entries are a
# no-op when the colour is not in the catalogue, but they keep the .po
# stable as new colours arrive.
_KNOWN_COLOR_NAMES = (
    "Чорний",
    "Білий",
    "Сірий",
    "Кайот",
    "Олива",
    "Хакі",
    "Бежевий",
    "Червоний",
    "Зелений",
    "Синій",
    "Темно-синій",
    "Жовтий",
    "Помаранчевий",
    "Рожевий",
    "Ментол",
    "Фіолетовий",
    "Коричневий",
    "Бордовий",
    "Бело-бордовий",
    "бело-бордовий",
)
_COLOR_LABEL_LOOKUP = {name: _(name) for name in _KNOWN_COLOR_NAMES}


def _translate_color_label(label: str):
    """Return a lazy translation for a known colour, or the raw label."""

    if not label:
        return label
    return _COLOR_LABEL_LOOKUP.get(label.strip(), label)


# Maximum number of colour slugs we accept in a single ``?color=`` value.
# Keeps query strings sane and shields us from accidental FACET-style abuse.
MAX_COLOR_SLUGS = 10
ALLOWED_COLOR_SLUGS_CACHE_KEY = "catalog:allowed-color-slugs:v1"
ALLOWED_COLOR_SLUGS_CACHE_TIMEOUT = 300
# Slugs follow a simple ``[a-z0-9-]+`` shape — we sanitise aggressively.
_VALID_SLUG_CHARS = set("abcdefghijklmnopqrstuvwxyz0123456789-")


def _normalise_slug(value: str) -> str:
    value = (value or "").strip().lower()
    if not value:
        return ""
    return "".join(ch for ch in value if ch in _VALID_SLUG_CHARS).strip("-")


def normalise_color_slugs(
    raw_values: Iterable[str],
    *,
    allowed_slugs: Iterable[str] | None = None,
) -> List[str]:
    """Return a bounded, sorted set of normalized colour slugs."""
    allowed = None
    if allowed_slugs is not None:
        allowed = {_normalise_slug(value) for value in allowed_slugs}
        allowed.discard("")

    result = set()
    for raw_value in raw_values:
        for piece in (raw_value or "").split(","):
            slug = _normalise_slug(piece)
            if not slug or (allowed is not None and slug not in allowed):
                continue
            result.add(slug)

    return sorted(result)[:MAX_COLOR_SLUGS]


def parse_color_filter(request, *, allowed_slugs=None) -> List[str]:
    """Return the canonical list of selected colour slugs.

    Accepts both ``?color=black,red`` and repeated ``?color=black&color=red``.
    """
    if request is None:
        return []
    return normalise_color_slugs(
        request.GET.getlist("color"),
        allowed_slugs=allowed_slugs,
    )


def get_allowed_color_slugs() -> List[str] | None:
    """Return cached slugs used by published products, or ``None`` on DB failure."""
    cached = cache.get(ALLOWED_COLOR_SLUGS_CACHE_KEY)
    if cached is not None:
        return list(cached)

    try:
        ProductColorVariant = apps.get_model("productcolors", "ProductColorVariant")
        slugs = sorted(
            set(
                ProductColorVariant.objects.filter(product__status="published")
                .exclude(slug="")
                .values_list("slug", flat=True)
            )
        )
    except (LookupError, DatabaseError):
        return None

    cache.set(
        ALLOWED_COLOR_SLUGS_CACHE_KEY,
        slugs,
        ALLOWED_COLOR_SLUGS_CACHE_TIMEOUT,
    )
    return slugs


def build_canonical_color_filter_url(request, *, allowed_slugs) -> str | None:
    """Return a canonical redirect target, or ``None`` for an already canonical URL."""
    if "color" not in request.GET:
        return None

    selected = parse_color_filter(request, allowed_slugs=allowed_slugs)
    canonical_value = ",".join(selected)
    color_identity_changed = request.GET.getlist("color") != [canonical_value]

    params: List[tuple[str, str]] = []
    for key, values in sorted(request.GET.lists()):
        if key == "color" or (key == "page" and color_identity_changed):
            continue
        params.extend((key, value) for value in values)
    if canonical_value:
        params.append(("color", canonical_value))

    current_params = [
        (key, value)
        for key, values in request.GET.lists()
        for value in values
    ]
    if current_params == params:
        return None

    query = urlencode(params, doseq=True)
    return f"{request.path}?{query}" if query else request.path


def canonical_color_filter(view_func):
    """Redirect noisy colour-filter GETs before a page-cache lookup or write."""
    @wraps(view_func)
    def _wrapped_view(request, *args, **kwargs):
        if request.method in {"GET", "HEAD"} and "color" in request.GET:
            allowed_slugs = get_allowed_color_slugs()
            if allowed_slugs is not None:
                redirect_url = build_canonical_color_filter_url(
                    request,
                    allowed_slugs=allowed_slugs,
                )
                if redirect_url is not None:
                    return HttpResponsePermanentRedirect(redirect_url)
        return view_func(request, *args, **kwargs)

    return _wrapped_view


def apply_color_filter(queryset: QuerySet, slugs: Iterable[str]) -> QuerySet:
    slugs = [s for s in slugs if s]
    if not slugs:
        return queryset
    return queryset.filter(color_variants__slug__in=slugs).distinct()


def _build_chip_url(request, slugs: List[str]) -> str:
    """Build a URL on the same path with ``color=`` rewritten to ``slugs``."""
    params: List[tuple[str, str]] = []
    for key, values in request.GET.lists():
        if key == "color" or key == "page":
            continue
        for value in values:
            params.append((key, value))
    if slugs:
        params.append(("color", ",".join(slugs)))
    qs = urlencode(params, doseq=True)
    path = request.path
    return f"{path}?{qs}" if qs else path


def build_available_colors(
    base_queryset: QuerySet,
    request,
    selected_slugs: Iterable[str],
    *,
    category=None,
) -> List[Dict[str, Any]]:
    """Return chip descriptors for every distinct colour slug in ``base_queryset``.

    ``base_queryset`` is the *pre-colour-filter* queryset so users can
    always OR-in any colour available in the current category/search.

    When ``category`` is provided **and** a published
    ``CategoryColorLanding`` exists for ``(category, slug)``, the chip's
    ``url`` is swapped from ``?color=<slug>`` to the dedicated landing
    URL (e.g. ``/catalog/tshirts/black/``). This routes organic
    traffic — and PageRank — toward the indexable landing while
    preserving the legacy filter for unmapped colours.
    """
    selected = normalise_color_slugs(selected_slugs or [])
    selected_set = set(selected)

    try:
        ProductColorVariant = apps.get_model("productcolors", "ProductColorVariant")
    except LookupError:
        return []

    try:
        product_ids = list(base_queryset.values_list("id", flat=True))
    except DatabaseError:
        return []

    if not product_ids:
        return []

    try:
        variants = (
            ProductColorVariant.objects
            .filter(product_id__in=product_ids)
            .exclude(slug="")
            .select_related("color")
            .values(
                "slug",
                "color__name",
                "color__primary_hex",
                "color__secondary_hex",
            )
        )
    except DatabaseError:
        return []

    # Aggregate by slug. Pick the most common (primary, secondary) hex pair
    # and the first non-empty colour name as the canonical label source.
    bucket: Dict[str, Dict[str, Any]] = defaultdict(lambda: {
        "count": 0,
        "names": defaultdict(int),
        "hexes": defaultdict(int),
    })
    for row in variants:
        slug = row["slug"]
        if not slug:
            continue
        entry = bucket[slug]
        entry["count"] += 1
        name = (row.get("color__name") or "").strip()
        if name:
            entry["names"][name] += 1
        primary = (row.get("color__primary_hex") or "").strip()
        secondary = (row.get("color__secondary_hex") or "").strip()
        entry["hexes"][(primary, secondary)] += 1

    chips: List[Dict[str, Any]] = []
    # Look up which colour slugs in this category have a published
    # landing page; chips for those slugs link to the landing URL
    # rather than ``?color=<slug>``. One cheap query gated on the
    # (category, is_published) compound index.
    landing_url_by_slug: Dict[str, str] = {}
    if category is not None:
        try:
            CategoryColorLanding = apps.get_model("storefront", "CategoryColorLanding")
        except LookupError:
            CategoryColorLanding = None
        if CategoryColorLanding is not None:
            try:
                cat_slug = getattr(category, "slug", "")
                if cat_slug:
                    for slug in (
                        CategoryColorLanding.objects
                        .filter(category=category, is_published=True)
                        .values_list("color_slug", flat=True)
                    ):
                        if slug:
                            landing_url_by_slug[slug] = f"/catalog/{cat_slug}/{slug}/"
            except DatabaseError:
                landing_url_by_slug = {}

    for slug, entry in bucket.items():
        # Most common label / hex-pair wins; ties resolved by sort stability.
        label = ""
        if entry["names"]:
            label = max(entry["names"].items(), key=lambda kv: kv[1])[0]
        if not label:
            # Fall back to a humanised slug ("dark-olive" -> "Dark Olive").
            label = slug.replace("-", " ").title()
        primary, secondary = ("", "")
        if entry["hexes"]:
            primary, secondary = max(entry["hexes"].items(), key=lambda kv: kv[1])[0]

        is_selected = slug in selected_set
        if is_selected:
            next_slugs = [s for s in selected if s != slug]
        else:
            next_slugs = normalise_color_slugs([*selected, slug])
        # Default URL: the legacy ``?color=`` toggle. If a published
        # colour-category landing exists for this slug, swap to the
        # dedicated landing URL — but only when the chip would *enter*
        # the filter (not when un-selecting, where the toggle URL
        # should remove the slug from the existing query string).
        landing_url = landing_url_by_slug.get(slug)
        if landing_url and not is_selected:
            chip_url = landing_url
        else:
            chip_url = _build_chip_url(request, next_slugs)

        chips.append({
            "slug": slug,
            "label": _translate_color_label(label),
            "primary_hex": primary,
            "secondary_hex": secondary,
            "count": entry["count"],
            "is_selected": is_selected,
            "url": chip_url,
            "is_landing": bool(landing_url and not is_selected),
        })

    chips.sort(key=lambda c: (-c["count"], c["slug"]))
    return chips


def build_reset_url(request) -> str:
    """URL on the same path with the ``color=`` param dropped."""
    return _build_chip_url(request, [])


def build_home_color_chips(
    base_queryset: QuerySet,
    target_path: str,
    *,
    limit: int = 12,
) -> List[Dict[str, Any]]:
    """Chip descriptors for the homepage that link to ``target_path?color=<slug>``.

    Used near the categories block so visitors can jump straight to a
    pre-filtered catalogue page. Each chip points to a single colour
    (no toggling, since the homepage itself is not filtered).
    """
    try:
        ProductColorVariant = apps.get_model("productcolors", "ProductColorVariant")
    except LookupError:
        return []

    try:
        product_ids = list(base_queryset.values_list("id", flat=True))
    except DatabaseError:
        return []

    if not product_ids:
        return []

    try:
        variants = (
            ProductColorVariant.objects
            .filter(product_id__in=product_ids)
            .exclude(slug="")
            .select_related("color")
            .values(
                "slug",
                "color__name",
                "color__primary_hex",
                "color__secondary_hex",
            )
        )
    except DatabaseError:
        return []

    bucket: Dict[str, Dict[str, Any]] = defaultdict(lambda: {
        "count": 0,
        "names": defaultdict(int),
        "hexes": defaultdict(int),
    })
    for row in variants:
        slug = row["slug"]
        if not slug:
            continue
        entry = bucket[slug]
        entry["count"] += 1
        name = (row.get("color__name") or "").strip()
        if name:
            entry["names"][name] += 1
        primary = (row.get("color__primary_hex") or "").strip()
        secondary = (row.get("color__secondary_hex") or "").strip()
        entry["hexes"][(primary, secondary)] += 1

    chips: List[Dict[str, Any]] = []
    for slug, entry in bucket.items():
        label = ""
        if entry["names"]:
            label = max(entry["names"].items(), key=lambda kv: kv[1])[0]
        if not label:
            label = slug.replace("-", " ").title()
        primary, secondary = ("", "")
        if entry["hexes"]:
            primary, secondary = max(entry["hexes"].items(), key=lambda kv: kv[1])[0]
        url = f"{target_path}?{urlencode([('color', slug)])}"
        chips.append({
            "slug": slug,
            "label": _translate_color_label(label),
            "primary_hex": primary,
            "secondary_hex": secondary,
            "count": entry["count"],
            "url": url,
        })

    chips.sort(key=lambda c: (-c["count"], c["slug"]))
    return chips[:limit]
