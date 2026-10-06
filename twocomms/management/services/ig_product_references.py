"""Resolve customer-supplied product references without guessing identity."""

from __future__ import annotations

import re
from collections import defaultdict
from urllib.parse import unquote, urlsplit

from product_catalog.size_grid_services import normalize_size_value
from storefront.models import Product, ProductStatus

from .ig_commerce_types import ProductReference, ReferenceSource


_STOREFRONT_HOSTS = frozenset({"twocomms.shop", "www.twocomms.shop"})
_INSTAGRAM_HOSTS = frozenset({"instagram.com", "www.instagram.com"})
_PRODUCT_PATH = re.compile(
    r"^/(?:(?:uk|ru|en)/)?product/(?P<slug>[-a-z0-9_]+)(?:/(?P<options>.*))?/?$",
    re.I,
)
_INSTAGRAM_PATH = re.compile(r"^/(?:p|reel|tv)/(?P<code>[A-Za-z0-9_-]+)/?", re.I)
_KNOWN_SIZE_CODES = frozenset({
    "XXS", "XS", "S", "M", "L", "XL", "XXL", "XXXL", "4XL", "5XL",
    "ONESIZE", "ONE-SIZE",
})


def _empty(source=ReferenceSource.UNKNOWN, reason="unknown_reference", **kwargs):
    return ProductReference(source=source, reason=reason, **kwargs)


def _safe_hostname(parsed) -> str:
    try:
        return str(parsed.hostname or "").lower()
    except ValueError:
        return ""


def _parse_owned_url(value: str, *, catalog_graph=None) -> ProductReference:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return _empty(reason="invalid_url")
    hostname = _safe_hostname(parsed)
    source = ReferenceSource.STOREFRONT if hostname in _STOREFRONT_HOSTS else ReferenceSource.UNKNOWN
    try:
        unexpected_port = parsed.port not in (None, 443)
    except ValueError:
        unexpected_port = True
    if (
        parsed.scheme.lower() != "https"
        or hostname not in _STOREFRONT_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or unexpected_port
    ):
        return _empty(source=source, reason="untrusted_storefront_url")
    match = _PRODUCT_PATH.fullmatch(unquote(parsed.path or ""))
    if not match:
        return _empty(source=source, reason="not_product_url")
    if catalog_graph is not None:
        matches = [row for row in catalog_graph.products if row.slug.lower() == match.group("slug").lower()]
        product = matches[0] if len(matches) == 1 else None
    else:
        product = (
            Product.objects.filter(
                slug=match.group("slug").lower(),
                status=ProductStatus.PUBLISHED,
            )
            .prefetch_related("color_variants__color", "fit_options")
            .first()
        )
    if product is None:
        return _empty(source=source, reason="unknown_product")

    option_segments = [
        segment.lower()
        for segment in (match.group("options") or "").strip("/").split("/")
        if segment
    ]
    product_id = product.product_id if catalog_graph is not None else product.pk
    product_graph = catalog_graph
    if option_segments and product_graph is None:
        from .ig_catalog_graph import build_catalog_graph

        product_graph = build_catalog_graph(product_ids=(product_id,))
    known: dict[str, list[tuple[str, str]]] = defaultdict(list)
    variants = product.variants if catalog_graph is not None else product.color_variants.all()
    for variant in variants:
        slug = variant.color_slug if catalog_graph is not None else variant.slug
        if slug:
            known[slug.lower()].append(("color", slug.lower()))
    fits = product.fits if catalog_graph is not None else product.fit_options.all()
    for option in fits:
        if (catalog_graph is not None or option.is_active) and option.code:
            known[option.code.lower()].append(("fit", option.code.lower()))
    for segment in option_segments:
        normalized_size = normalize_size_value(segment)
        if normalized_size in _KNOWN_SIZE_CODES:
            known[segment].append(("size", normalized_size))
    if product_graph and product_graph.products:
        graph_product = next((row for row in product_graph.products if row.product_id == product_id), None)
        for configuration in graph_product.pricing.configurations if graph_product else ():
            for size in configuration.compatible_sizes:
                normalized_size = normalize_size_value(size)
                if normalized_size:
                    known[normalized_size.lower()].append(("size", normalized_size))

    constraints: dict[str, str] = {}
    seen_segments: set[str] = set()
    for segment in option_segments:
        matches = list(dict.fromkeys(known.get(segment, ())))
        if segment in seen_segments or len(matches) != 1:
            return _empty(source=source, reason="invalid_product_option")
        seen_segments.add(segment)
        key, value = matches[0]
        if key in constraints:
            return _empty(source=source, reason="conflicting_product_options")
        constraints[key] = value

    if constraints:
        from .ig_catalog_candidates import rank_candidates
        from .ig_commerce_types import CommerceTurnRequest

        decision = rank_candidates(
            product_graph,
            CommerceTurnRequest(
                exact_product_id=product_id,
                hard=constraints,
            ),
        )
        if not decision.candidates:
            return _empty(source=source, reason="incompatible_product_options")

    return ProductReference(
        product_id=int(product_id),
        is_exact=True,
        source=source,
        reason="exact_storefront_product",
        constraints=tuple(sorted(constraints.items())),
    )


def _parse_instagram_url(value: str) -> ProductReference:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return _empty(reason="invalid_url")
    hostname = _safe_hostname(parsed)
    if parsed.scheme.lower() not in {"http", "https"} or hostname not in _INSTAGRAM_HOSTS:
        return _empty(reason="untrusted_instagram_url")
    match = _INSTAGRAM_PATH.match(parsed.path or "")
    if not match:
        return _empty(reason="not_instagram_post")
    prefix = (parsed.path.strip("/").split("/", 1)[0] or "p").lower()
    return _empty(
        source=ReferenceSource.INSTAGRAM_POST,
        reason="instagram_catalog_match_required",
        external_reference=f"{prefix}:{match.group('code')}",
    )


def _published_ids(values) -> tuple[int, ...]:
    ids = sorted({value for value in values if isinstance(value, int)})
    if not ids:
        return ()
    return tuple(
        Product.objects.filter(pk__in=ids, status=ProductStatus.PUBLISHED)
        .order_by("pk")
        .values_list("pk", flat=True)
    )


def _apply_media_evidence(result: ProductReference, media_evidence) -> ProductReference:
    evidence = [item for item in (media_evidence or ()) if isinstance(item, dict)]
    if not evidence:
        return result
    verified = _published_ids(
        item.get("product_id")
        for item in evidence
        if item.get("verified_mapping") is True
        and (
            not result.external_reference
            or item.get("external_reference") == result.external_reference
        )
    )
    if len(verified) == 1:
        return ProductReference(
            product_id=verified[0],
            is_exact=True,
            source=result.source,
            reason="verified_media_mapping",
            external_reference=result.external_reference,
        )
    if len(verified) > 1:
        return _empty(
            source=result.source,
            reason="conflicting_verified_media_mapping",
            external_reference=result.external_reference,
        )

    visual = _published_ids(
        item.get("product_id")
        for item in evidence
        if item.get("verified_mapping") is not True
        and item.get("kind") in {"screenshot", "photo", "image"}
    )
    if visual:
        return ProductReference(
            source=ReferenceSource.SCREENSHOT,
            reason="visual_match_requires_confirmation",
            candidate_product_ids=visual[:3],
        )
    return result


def resolve_product_reference(value: str | None, *, media_evidence=None, catalog_graph=None) -> ProductReference:
    """Resolve exact owned URLs; keep Instagram and screenshots as evidence."""

    urls = re.findall(r"https?://[^\s<>]+", str(value or ""))
    if not urls:
        if media_evidence:
            return _apply_media_evidence(
                _empty(
                    source=ReferenceSource.SCREENSHOT,
                    reason="visual_match_requires_confirmation",
                ),
                media_evidence,
            )
        return _empty()

    normalized_urls = [url.rstrip(".,);]") for url in urls]
    owned_results = [(_parse_owned_url(url, catalog_graph=catalog_graph) if catalog_graph is not None
        else _parse_owned_url(url)) for url in normalized_urls]
    owned_results = [
        result for result in owned_results
        if result.source == ReferenceSource.STOREFRONT
    ]
    owned_failures = [result for result in owned_results if not result.is_exact]
    if owned_failures:
        return owned_failures[0]
    if owned_results:
        product_ids = {result.product_id for result in owned_results}
        if len(product_ids) > 1:
            return _empty(
                source=ReferenceSource.STOREFRONT,
                reason="multiple_products_require_clarification",
            )
        constraints: dict[str, set[str]] = defaultdict(set)
        for result in owned_results:
            for key, value in result.constraints:
                constraints[key].add(value)
        if any(len(values) > 1 for values in constraints.values()):
            return _empty(
                source=ReferenceSource.STOREFRONT,
                reason="conflicting_product_options",
            )
        first = owned_results[0]
        return ProductReference(
            product_id=first.product_id,
            is_exact=True,
            source=first.source,
            reason=first.reason,
            constraints=tuple(
                sorted((key, next(iter(values))) for key, values in constraints.items())
            ),
        )

    instagram_results = [_parse_instagram_url(url) for url in normalized_urls]
    instagram_results = [
        result for result in instagram_results
        if result.source == ReferenceSource.INSTAGRAM_POST
    ]
    if instagram_results:
        external = instagram_results[0].external_reference
        if any(result.external_reference != external for result in instagram_results):
            return _empty(
                source=ReferenceSource.INSTAGRAM_POST,
                reason="conflicting_media_references",
            )
        return _apply_media_evidence(instagram_results[0], media_evidence)

    return _empty(reason="unrecognized_reference")


ReferenceSource = ReferenceSource
