"""Closed registry for supported marketplace feed formats.

The registry intentionally contains data and small dispatch helpers only.  It
keeps URLs and XML serializers fixed while allowing staff to create bounded
profiles for those known adapters.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class FeedAdapterDefinition:
    key: str
    label: str
    format_label: str
    languages: tuple[str, ...]
    canonical_paths: tuple[str, ...]
    snapshot_name: str | None
    command_name: str | None
    default_rules: dict[str, object]


def _default_rules(*, language: str = "uk") -> dict[str, object]:
    return {
        "filters": {},
        "availability": {"mode": "inherit"},
        "images": {"mode": "variant_first", "max_count": 10},
        "text": {"language": language},
    }


FEED_ADAPTERS = {
    "google": FeedAdapterDefinition(
        "google", "Google Merchant", "Google XML", ("uk", "ru"),
        (
            "/google_merchant_feed.xml",
            "/google-merchant-feed.xml",
            "/google-merchant-feed-v2.xml",
            "/media/google-merchant-v2.xml",
            "/media/google-merchant-v3.xml",
        ),
        "google-merchant-v3.xml",
        "generate_google_merchant_feed",
        _default_rules(),
    ),
    "meta": FeedAdapterDefinition(
        "meta", "Instagram / Meta", "Meta XML", ("uk", "ru"),
        ("/media/instagram-feed.xml",),
        "instagram-feed.xml",
        "generate_instagram_feed",
        {
            "filters": {
                "exclude_product_ids": [225],
                "min_image_count": 2,
                "search_keywords": ["худі", "худи", "hood", "hudi", "sweat"],
                "dropship_only": True,
            },
            "availability": {"mode": "force_in_stock", "quantity": 100},
            "images": {"mode": "newest_first", "max_count": 10},
            "text": {"language": "uk"},
        },
    ),
    "rozetka": FeedAdapterDefinition(
        "rozetka", "Rozetka", "YML", ("uk", "ru"),
        ("/rozetka-feed.xml",), "rozetka-feed.xml", "generate_rozetka_feed", _default_rules(),
    ),
    "kasta": FeedAdapterDefinition(
        "kasta", "Kasta", "YML", ("uk_ru",),
        ("/kasta-feed.xml",), "kasta-feed.xml", "generate_kasta_feed", _default_rules(language="uk_ru"),
    ),
    "buyme": FeedAdapterDefinition(
        "buyme", "BuyMe", "YML", ("uk",),
        ("/buyme-feed.xml",), "buyme-feed.xml", "generate_buyme_feed", _default_rules(),
    ),
    "prom": FeedAdapterDefinition(
        "prom", "Prom.ua", "YML", ("uk", "ru"),
        ("/prom-feed.xml",), "prom-feed.xml", "generate_prom_feed", _default_rules(),
    ),
    "bezzet": FeedAdapterDefinition(
        "bezzet", "Bezzet", "YML", ("uk", "ru"),
        ("/products_feed.xml",), None, None, _default_rules(),
    ),
}


def reserved_feed_slugs() -> frozenset[str]:
    """Return route-sensitive slugs which must not be claimed by profiles."""
    path_slugs = {
        path.rsplit("/", 1)[-1].removesuffix(".xml")
        for definition in FEED_ADAPTERS.values()
        for path in definition.canonical_paths
    }
    return frozenset({*FEED_ADAPTERS, *path_slugs, "admin-panel", "feeds"})


def get_system_feed(system_key: str):
    """Resolve an active system profile, falling back safely before migration."""
    try:
        from django.db import OperationalError, ProgrammingError
        from storefront.models import MarketplaceFeed

        return MarketplaceFeed.objects.filter(
            system_key=system_key,
            is_system=True,
            is_active=True,
        ).first()
    except (OperationalError, ProgrammingError):
        return None


def build_feed_xml(adapter: str, *, base_url: str | None = None, feed=None) -> bytes:
    """Dispatch a known adapter without accepting arbitrary dotted paths."""
    from storefront.services import marketplace_feeds

    builders: dict[str, Callable[..., bytes]] = {
        "google": marketplace_feeds.build_google_merchant_feed_xml,
        "meta": marketplace_feeds.build_meta_catalog_feed_xml,
        "rozetka": marketplace_feeds.build_rozetka_feed_xml,
        "kasta": marketplace_feeds.build_kasta_feed_xml,
        "buyme": marketplace_feeds.build_buyme_feed_xml,
        "prom": marketplace_feeds.build_prom_feed_xml,
        "bezzet": marketplace_feeds.build_uaprom_products_feed_xml,
    }
    try:
        return builders[adapter](base_url=base_url, feed=feed)
    except KeyError as exc:
        raise ValueError(f"Unsupported marketplace feed adapter: {adapter}") from exc
