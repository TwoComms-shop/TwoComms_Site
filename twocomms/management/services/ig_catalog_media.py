"""Catalog-owned media selection for Instagram product discovery.

The bot should show a small, trusted image set when a customer asks to see
products. This module deliberately returns media only: product URLs stay out
of the payload unless the caller explicitly asks for a link in the text flow.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import json
import mimetypes
from pathlib import Path
from urllib.parse import urlsplit

from django.conf import settings


MAX_CATALOG_MEDIA = 4
# Скільки товарів має сенс показати фото, перш ніж це перестає бути допомогою.
# Понад цю межу клієнту краще дати підбірку каталогу одним посиланням, ніж
# висипати десять картинок: у production на запит «футболку з Харковом» підходило
# 4 футболки з 10 позицій колекції, і три надіслані фото виглядали як «це все».
CATALOG_LINK_THRESHOLD = MAX_CATALOG_MEDIA
MAX_CATALOG_MEDIA_BYTES = 10 * 1024 * 1024
_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
_IMAGE_MIME_TYPES = {"image/jpeg", "image/png", "image/webp"}


class CatalogMediaState(StrEnum):
    READY = "ready"
    PARTIAL = "partial"
    EMPTY = "empty"
    AMBIGUOUS = "ambiguous"


class CatalogMediaDeliveryState(StrEnum):
    SENT = "sent"
    PARTIAL = "partial"
    FAILED = "failed"
    AMBIGUOUS = "ambiguous"
    UNKNOWN = "unknown"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class CatalogMediaItem:
    url: str
    title: str
    alt: str
    product_id: int
    mime_type: str = ""
    size_bytes: int = 0


@dataclass(frozen=True)
class CatalogMediaSelection:
    state: CatalogMediaState
    items: tuple[CatalogMediaItem, ...] = ()
    requested_product_ids: tuple[int, ...] = ()
    missing_product_ids: tuple[int, ...] = ()
    # Почему пришлось взять не точные ассеты варианта. Пустая строка = взяты
    # точные. Оператор должен видеть причину, а не догадываться (Э3.7).
    fallback_reason: str = ""
    # Скільки товарів модель просила показати і скільки з них реально отримали
    # фото. Різниця — це те, про що клієнту треба сказати словами, інакше
    # надіслані фото читаються як «це весь асортимент».
    requested_count: int = 0
    shown_product_count: int = 0

    @property
    def truncated_product_count(self) -> int:
        return max(0, int(self.requested_count) - int(self.shown_product_count))


@dataclass(frozen=True)
class CatalogMediaDelivery:
    state: CatalogMediaDeliveryState
    sent_count: int = 0
    attempted_count: int = 0
    provider_message_ids: tuple[str, ...] = ()
    error: str = ""


@dataclass(frozen=True)
class PreparedCatalogMedia:
    """Exact provider payloads plus content-safe product projection."""

    payloads: tuple[dict, ...] = ()
    product_refs: tuple[dict, ...] = ()
    error: str = ""


def _base_url() -> str:
    return (
        str(getattr(settings, "SITE_BASE_URL", "") or "https://twocomms.shop")
        .rstrip("/")
    )


def _absolute_url(value) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if raw.startswith("https://"):
        return raw
    if raw.startswith("http://"):
        return "https://" + raw.split("://", 1)[1]
    return f"{_base_url()}/{raw.lstrip('/')}"


def _image_url(image_field) -> str:
    try:
        return _absolute_url(getattr(image_field, "url", ""))
    except Exception:
        return ""


def _image_asset(image_field) -> tuple[str, str, int] | None:
    url = _image_url(image_field)
    if not url:
        return None
    name = str(getattr(image_field, "name", "") or urlsplit(url).path)
    mime_type = str(mimetypes.guess_type(name)[0] or "").lower()
    try:
        size_bytes = int(getattr(image_field, "size", 0) or 0)
    except (OSError, TypeError, ValueError):
        size_bytes = 0
    return url, mime_type, size_bytes


def _variant_assets(variant) -> list[tuple[str, str, int]]:
    assets: list[tuple[str, str, int]] = []
    urls: set[str] = set()
    images = getattr(variant, "_prefetched_objects_cache", {}).get("images")
    if images is None:
        try:
            images = list(variant.images.order_by("order", "id")[:MAX_CATALOG_MEDIA])
        except Exception:
            images = []
    for image in images:
        asset = _image_asset(getattr(image, "image", None))
        if asset and asset[0] not in urls:
            assets.append(asset)
            urls.add(asset[0])
    return assets


def _product_assets(product) -> list[tuple[str, str, int]]:
    assets: list[tuple[str, str, int]] = []
    urls: set[str] = set()
    try:
        images = list(product.images.order_by("order", "id")[:MAX_CATALOG_MEDIA])
    except Exception:
        images = []
    for image in images:
        asset = _image_asset(getattr(image, "image", None))
        if asset and asset[0] not in urls:
            assets.append(asset)
            urls.add(asset[0])
    for field_name in ("main_image", "home_card_image", "display_image"):
        asset = _image_asset(getattr(product, field_name, None))
        if asset and asset[0] not in urls:
            assets.append(asset)
            urls.add(asset[0])
    return assets


def select_catalog_media(
    product_ids,
    *,
    limit: int = MAX_CATALOG_MEDIA,
    color_variant_id: int | None = None,
    fit_code: str = "",
    size: str = "",
    selection_revision: str = "",
    expected_revision: str = "",
) -> CatalogMediaSelection:
    """Return up to four real images for published products.

    Э3.7 (`NEW-CAT-002`). Раньше медиа выбиралось независимым запросом
    `filter(stock__gt=0)` по одному только `product_ids`. Поэтому клиент мог
    увидеть фото чёрного варианта, нажать «беру» и получить белый: фото и
    resolved variant приходили из двух разных источников истины.

    Теперь при известном `color_variant_id` берутся **только** ассеты именно
    этого варианта. Generic-медиа (фото товара без привязки к варианту)
    разрешается только когда точных ассетов нет, и это фиксируется в
    `fallback_reason`, чтобы оператор видел причину, а не догадывался.

    `selection_revision` / `expected_revision`: устаревшая генерация выбора не
    отправляет медиа вообще. Медиа, относящееся к отменённому выбору, хуже
    отсутствия медиа.
    """
    if (
        expected_revision
        and str(selection_revision or "") != str(expected_revision or "")
    ):
        return CatalogMediaSelection(
            CatalogMediaState.AMBIGUOUS,
            fallback_reason="stale_selection_revision",
        )
    try:
        requested = tuple(dict.fromkeys(int(value) for value in (product_ids or ())))
    except (TypeError, ValueError):
        return CatalogMediaSelection(CatalogMediaState.AMBIGUOUS)
    requested = tuple(value for value in requested if value > 0)
    if not requested:
        return CatalogMediaSelection(CatalogMediaState.EMPTY)
    try:
        from productcolors.models import ProductColorVariant
        from storefront.models import Product, ProductStatus

        products = list(
            Product.objects.filter(pk__in=requested, status=ProductStatus.PUBLISHED)
            .prefetch_related(
                "images",
                "color_variants__images",
                "color_variants__color",
            )
        )
    except Exception:
        return CatalogMediaSelection(CatalogMediaState.AMBIGUOUS, requested_product_ids=requested)

    by_id = {product.pk: product for product in products}
    missing = [product_id for product_id in requested if product_id not in by_id]
    selected: list[CatalogMediaItem] = []
    candidate_groups: list[list[CatalogMediaItem]] = []
    fallback_reason = ""
    for product_id in requested:
        product = by_id.get(product_id)
        if product is None:
            continue
        candidates: list[tuple[str, str, int]] = []
        exact_variant = None
        if color_variant_id and len(requested) == 1:
            exact_variant = next(
                (
                    variant
                    for variant in product.color_variants.all()
                    if variant.pk == int(color_variant_id)
                ),
                None,
            )
        if exact_variant is not None:
            candidates = list(_variant_assets(exact_variant))
            if not candidates:
                # Точных ассетов у выбранного варианта нет. Generic-фото
                # допустимо, но причина обязана быть видимой.
                fallback_reason = "variant_assets_missing"
        else:
            if color_variant_id and len(requested) == 1:
                fallback_reason = "variant_not_found"
            variants = list(
                ProductColorVariant.objects.filter(product_id=product_id, stock__gt=0)
                .prefetch_related("images")
                .order_by("order", "id")
            )
            for variant in variants:
                candidates.extend(_variant_assets(variant))
        if not candidates:
            candidates = _product_assets(product)
            if not fallback_reason:
                fallback_reason = "product_assets_only"
        group = []
        for url, mime_type, size_bytes in candidates:
            group.append(
                CatalogMediaItem(
                    url=url,
                    title=str(product.title or "TwoComms")[:200],
                    alt=f"{product.title} — TwoComms"[:240],
                    product_id=product_id,
                    mime_type=mime_type,
                    size_bytes=size_bytes,
                )
            )
        candidate_groups.append(group)

    # Give each product one photo before showing extra angles of any product.
    # Single-product requests retain the existing bounded four-photo gallery.
    seen: set[str] = set()
    maximum = max(1, min(int(limit), MAX_CATALOG_MEDIA))
    while candidate_groups and len(selected) < maximum:
        remaining = []
        for group in candidate_groups:
            while group and group[0].url in seen:
                group.pop(0)
            if group and len(selected) < maximum:
                item = group.pop(0)
                seen.add(item.url)
                selected.append(item)
            if group:
                remaining.append(group)
        candidate_groups = remaining

    if not selected:
        state = CatalogMediaState.EMPTY
    elif missing:
        state = CatalogMediaState.PARTIAL
    else:
        state = CatalogMediaState.READY
    return CatalogMediaSelection(
        state=state,
        items=tuple(selected),
        requested_product_ids=requested,
        missing_product_ids=tuple(missing),
        fallback_reason=fallback_reason,
        requested_count=len(requested),
        shown_product_count=len({item.product_id for item in selected}),
    )


def parse_product_ids(raw) -> tuple[int, ...] | None:
    """Parse a model ``[SHOW_PRODUCTS:1,2]`` value without accepting junk."""
    if raw in (True, None, ""):
        return None
    if not isinstance(raw, str):
        return None
    values: list[int] = []
    for part in raw.replace(";", ",").split(","):
        try:
            value = int(part.strip())
        except (TypeError, ValueError):
            return None
        if value <= 0 or value in values:
            return None
        values.append(value)
    return tuple(values[:12]) if values else None


def _trusted_media_item(item: CatalogMediaItem) -> bool:
    """Keep the Instagram transport limited to first-party image assets."""
    try:
        parsed = urlsplit(str(item.url or ""))
        if parsed.scheme != "https" or not parsed.hostname:
            return False
        base_host = urlsplit(_base_url()).hostname
        configured = getattr(settings, "IG_CATALOG_MEDIA_ALLOWED_HOSTS", ()) or ()
        allowed_hosts = {str(base_host or "").lower()}
        allowed_hosts.update(str(value).strip().lower() for value in configured if str(value).strip())
        if parsed.hostname.lower() not in allowed_hosts:
            return False
        if (Path(parsed.path).suffix or "").lower() not in _IMAGE_SUFFIXES:
            return False
        if str(item.mime_type or "").lower() not in _IMAGE_MIME_TYPES:
            return False
        size = int(item.size_bytes or 0)
        return 0 < size <= MAX_CATALOG_MEDIA_BYTES
    except (TypeError, ValueError):
        return False


def prepare_catalog_media(
    settings_row,
    recipient_id: str,
    selection: CatalogMediaSelection,
) -> PreparedCatalogMedia:
    """Prepare the exact bounded HTTP payload list without sending anything."""
    del settings_row  # Reserved for provider-specific payload fields.
    recipient = str(recipient_id or "").strip()
    safe_items = tuple(
        item for item in selection.items if _trusted_media_item(item)
    )[:MAX_CATALOG_MEDIA]
    if not recipient or not safe_items:
        return PreparedCatalogMedia(error=str(selection.state))
    payloads = []
    refs = []
    for part_index, item in enumerate(safe_items):
        payloads.append({
            "recipient": {"id": recipient},
            "message": {
                "attachment": {
                    "type": "image",
                    "payload": {"url": item.url, "is_reusable": True},
                }
            },
        })
        refs.append({
            "part_index": part_index,
            "product_id": int(item.product_id),
            "title": str(item.title or "")[:200],
        })
    return PreparedCatalogMedia(tuple(payloads), tuple(refs))


def send_catalog_media(
    settings_row,
    recipient_id: str,
    selection: CatalogMediaSelection,
    *,
    permission_boundary_factory=None,
) -> CatalogMediaDelivery:
    """Send selected images and report partial/ambiguous transport honestly."""
    from contextlib import nullcontext

    from management.services.instagram_bot import (
        _provider_account_id,
        _provider_http,
        _provider_message_id,
        _provider_url,
        get_page_token,
    )

    prepared = prepare_catalog_media(
        settings_row, recipient_id, selection
    )
    if not prepared.payloads:
        return CatalogMediaDelivery(
            CatalogMediaDeliveryState.FAILED,
            error=prepared.error or selection.state,
        )
    account_id = _provider_account_id(settings_row)
    page_token = get_page_token(settings_row)
    if not account_id or not page_token:
        return CatalogMediaDelivery(CatalogMediaDeliveryState.FAILED, error="provider_not_configured")

    sent = 0
    attempted = 0
    message_ids: list[str] = []
    for payload in prepared.payloads:
        boundary = permission_boundary_factory() if permission_boundary_factory else nullcontext(True)
        with boundary as allowed:
            if not allowed:
                return CatalogMediaDelivery(
                    CatalogMediaDeliveryState.CANCELLED,
                    sent_count=sent,
                    attempted_count=attempted,
                    provider_message_ids=tuple(message_ids),
                    error="permission_changed",
                )
            attempted += 1
            body = json.dumps(payload).encode("utf-8")
            try:
                code, response = _provider_http(
                    settings_row,
                    _provider_url(settings_row, f"/{account_id}/messages"),
                    token=page_token,
                    data=body,
                )
            except Exception:
                state = (
                    CatalogMediaDeliveryState.PARTIAL
                    if sent
                    else CatalogMediaDeliveryState.AMBIGUOUS
                )
                return CatalogMediaDelivery(
                    state,
                    sent_count=sent,
                    attempted_count=attempted,
                    provider_message_ids=tuple(message_ids),
                    error="provider_result_unknown",
                )
            if code != 200:
                state = (
                    CatalogMediaDeliveryState.PARTIAL
                    if sent
                    else CatalogMediaDeliveryState.FAILED
                )
                return CatalogMediaDelivery(
                    state,
                    sent_count=sent,
                    attempted_count=attempted,
                    provider_message_ids=tuple(message_ids),
                    error=f"provider_http_{int(code or 0)}",
                )
            message_id = _provider_message_id(response)
            if not message_id:
                return CatalogMediaDelivery(
                    CatalogMediaDeliveryState.UNKNOWN,
                    sent_count=sent,
                    attempted_count=attempted,
                    provider_message_ids=tuple(message_ids),
                    error="provider_message_id_missing",
                )
            sent += 1
            if message_id:
                message_ids.append(message_id)
                # Реєструємо всередині циклу, а не пачкою після нього: echo
                # першого фото може прийти ще до того, як відправиться друге, і
                # тоді пізня реєстрація вже не врятує. Саме через відсутність
                # цього кроку карусель бота 02.08.2026 була прийнята за
                # повідомлення менеджера — бот поставив себе на паузу, з'їв уже
                # згенерований текст відповіді, і клієнт отримав два фото без
                # жодного підпису.
                from management.services.ig_outgoing_registry import register_outgoing

                register_outgoing(message_id, recipient_id=recipient_id, kind="media")

    return CatalogMediaDelivery(
        CatalogMediaDeliveryState.SENT,
        sent_count=sent,
        attempted_count=attempted,
        provider_message_ids=tuple(message_ids),
    )
