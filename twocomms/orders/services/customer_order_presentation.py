"""Read-only customer-facing facts from an order item's explicit labels."""
import re
import logging


logger = logging.getLogger(__name__)


_TECHNICAL_LABEL = re.compile(
    r"(?:^_|(?:^|_)(?:id|ids|ref|refs|digest|schema|token)(?:$|_))",
    re.IGNORECASE,
)


def customer_item_option_labels(item):
    """Return human option labels without exposing internal option values."""
    labels = item.option_labels if isinstance(item.option_labels, dict) else {}
    fit = str(item.fit_option_label or item.fit_option_code or '').strip().casefold()
    result = []
    for label, value in labels.items():
        if not isinstance(label, str) or not isinstance(value, str):
            continue
        label, value = label.strip(), value.strip()
        if not label or not value or _TECHNICAL_LABEL.search(label):
            continue
        if value.casefold() == fit or (
            fit and label.casefold() in {'fit', 'посадка', 'крій', 'крой'}
        ):
            continue
        text = f'{label}: {value}'
        if text not in result:
            result.append(text)
    return result


def prepare_order_item_presentation(orders, *, audience, actor):
    """Project protected photo URLs after one canonical check per actual order.

    All images are read in one metadata batch. No storage URL, file access,
    photo FK, payment mutation or per-item authority query is introduced.
    """
    from django.conf import settings
    from django.urls import NoReverseMatch, reverse
    from management.ig_assisted_commerce_models import IgAssistedCommerceImage
    from management.services import ig_assisted_commerce as commerce
    from management.services.ig_assisted_checkout import validate_assisted_order

    if audience not in {'staff', 'account'}:
        raise ValueError('Unknown order presentation audience')
    orders = list(orders)
    candidates, photo_ids = [], set()
    for order in orders:
        items = list(order.items.all())
        for item in items:
            item.assisted_image_url = ''
        payload = order.payment_payload if isinstance(order.payment_payload, dict) else {}
        metadata = payload.get('ig_assisted_commerce')
        if not isinstance(metadata, dict) or not metadata:
            continue
        if actor is None:
            continue
        if audience == 'account' and (
            not actor.is_authenticated or not actor.is_active or order.user_id != actor.pk
        ):
            continue
        try:
            checked = validate_assisted_order(order)
        except Exception:
            logger.exception('Could not validate assisted order presentation for order %s', order.pk)
            continue
        if not checked.get('ready'):
            continue
        by_id = {item.pk: item for item in items}
        for row in checked['item_map']:
            item = by_id.get(row['order_item_id'])
            if not item or not item.is_custom or not row.get('photo_asset_id'):
                continue
            photo_ids.add(row['photo_asset_id'])
            candidates.append((order, item, row, metadata['request_id']))

    if not candidates:
        return orders
    if audience == 'staff':
        try:
            commerce._actor(actor.pk)
        except commerce.AssistedDecisionError:
            return orders
    images = {str(asset.pk): asset for asset in IgAssistedCommerceImage.objects.filter(pk__in=photo_ids)}
    for order, item, row, request_id in candidates:
        asset = images.get(row['photo_asset_id'])
        if (asset is None or asset.state != IgAssistedCommerceImage.State.ACTIVE
            or asset.erased_at is not None
            or str(asset.request_id) != request_id or asset.content_digest != row['photo_digest']
            or not commerce.verify_assisted_evidence(asset, 'image')):
            continue
        # The canonical paid order above supplies business retention after an
        # unconverted request's deadline. The protected reader rechecks this
        # proof and image lifecycle; no per-image retention queries are needed.
        try:
            if audience == 'staff':
                path = reverse('management_assisted_order_item_image',
                    kwargs={'order_id': order.pk, 'item_id': item.pk}, urlconf='management.urls')
                base = (getattr(settings, 'MANAGEMENT_BASE_URL', '')
                    or 'https://management.twocomms.shop').rstrip('/')
                item.assisted_image_url = f'{base}{path}'
            else:
                item.assisted_image_url = reverse('account_assisted_order_item_image',
                    kwargs={'order_id': order.pk, 'item_id': item.pk}, urlconf='twocomms.urls')
        except NoReverseMatch:
            # The image route's owner may be temporarily unavailable; keep the
            # branded fallback without exposing asset/storage identifiers.
            continue
    return orders
