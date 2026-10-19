"""CAS-gated visibility alignment for the two explicitly reviewed current hoodies.

No generic category rewrite and no order/profile/price conversion. Default
execution only reads the authorized product and fit rows and returns a plan.
"""
from hashlib import sha256
import json

from django.db import transaction

from storefront.models import Product, ProductFitOption, ProductStatus
from storefront.services.catalog_helpers import bump_public_product_order_version


VERSION = "2026-10-19.current-hoodie-fit-visibility.2"
# Product -> (existing classic row, existing oversize row), captured read-only
# and specifically approved for the current slightly oversized physical base.
AUTHORIZED_HOODIE_FITS = {102: (67, 68), 115: (65, 66)}
_FIT_FIELDS = ("id", "product_id", "code", "label", "description", "icon", "order", "is_default", "is_active")


class HoodieFitAlignmentError(RuntimeError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def _capture(*, lock=False):
    products = Product.objects.filter(pk__in=AUTHORIZED_HOODIE_FITS).select_related("category").order_by("pk")
    fits = ProductFitOption.objects.filter(product_id__in=AUTHORIZED_HOODIE_FITS).order_by("product_id", "pk")
    if lock:
        products, fits = products.select_for_update(), fits.select_for_update()
    products, fits = list(products), list(fits)
    if {item.pk for item in products} != set(AUTHORIZED_HOODIE_FITS):
        raise HoodieFitAlignmentError("hoodie_product_missing", "An authorized current hoodie is missing")
    snapshot, changes, skipped = [], [], []
    for product in products:
        if product.status not in ProductStatus.values:
            raise HoodieFitAlignmentError("hoodie_product_scope_changed", "An authorized product has an unknown publication status")
        published = product.status == "published"
        if published and (product.category.slug != "hoodie" or not product.category.is_active):
            raise HoodieFitAlignmentError("hoodie_product_scope_changed", "An authorized product is no longer a published hoodie")
        rows = [item for item in fits if item.product_id == product.pk]
        by_code = {item.code: item for item in rows}
        classic_id, oversize_id = AUTHORIZED_HOODIE_FITS[product.pk]
        if (set(by_code) != {"classic", "oversize"} or len(rows) != 2
                or by_code["classic"].pk != classic_id or by_code["oversize"].pk != oversize_id
                or (published and not by_code["oversize"].is_active)):
            raise HoodieFitAlignmentError("hoodie_fit_profile_changed", "The exact reviewed active oversize/legacy classic rows are required")
        snapshot.append({
            "product_id": product.pk, "slug": product.slug, "title": product.title,
            "status": product.status, "category_id": product.category_id, "category_slug": product.category.slug,
            "category_active": product.category.is_active,
            "category_updated_at": product.category.updated_at.isoformat() if product.category.updated_at else None,
            "updated_at": product.updated_at.isoformat() if product.updated_at else None,
            "price": product.price, "discount_percent": product.discount_percent,
            "fit_selector_enabled": product.fit_selector_enabled,
            "fits": [{field: getattr(item, field) for field in _FIT_FIELDS} for item in rows],
        })
        if not published:
            skipped.append({"product_id": product.pk, "reason": "not_published", "status": product.status})
            continue
        classic, oversize = by_code["classic"], by_code["oversize"]
        if classic.is_active or classic.is_default or not oversize.is_default:
            changes.append({"product_id": product.pk,
                "classic": {"id": classic.pk, "before": {"is_active": classic.is_active, "is_default": classic.is_default},
                            "after": {"is_active": False, "is_default": False}},
                "oversize": {"id": oversize.pk, "before": {"is_default": oversize.is_default},
                             "after": {"is_default": True}}})
    digest = sha256(json.dumps({"version": VERSION, "products": snapshot}, ensure_ascii=False,
                              sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return snapshot, changes, skipped, digest


def align_current_hoodie_fits(*, apply=False, expected_snapshot_hash=""):
    """Read-only plan by default; apply only the reviewed exact snapshot in a transaction."""
    if not apply:
        snapshot, changes, skipped, digest = _capture()
        return {"mode": "dry-run", "version": VERSION, "snapshot_hash": digest,
                "snapshot": snapshot, "changes": changes, "skipped": skipped, "provider_io": False}
    if not expected_snapshot_hash:
        raise HoodieFitAlignmentError("hoodie_fit_cas_required", "The reviewed dry-run snapshot hash is required")
    with transaction.atomic():
        _snapshot, changes, skipped, digest = _capture(lock=True)
        if digest != expected_snapshot_hash:
            raise HoodieFitAlignmentError("hoodie_fit_cas_changed", "The reviewed product or fit snapshot changed")
        for change in changes:
            # Clear the generated unique default identity before electing the
            # existing oversize row. Keep historical classic rows and labels.
            cleared = ProductFitOption.objects.filter(pk=change["classic"]["id"]).update(is_active=False, is_default=False)
            elected = ProductFitOption.objects.filter(pk=change["oversize"]["id"]).update(is_default=True)
            if cleared != 1 or elected != 1:
                raise HoodieFitAlignmentError("hoodie_fit_write_conflict", "An exact reviewed fit row disappeared")
        _after, remaining, after_skipped, after_digest = _capture(lock=True)
        if (after_skipped != skipped or
                [{key: value for key, value in row.items() if key != "fits"} for row in _after] !=
                [{key: value for key, value in row.items() if key != "fits"} for row in _snapshot]):
            raise HoodieFitAlignmentError("hoodie_fit_cas_changed", "Product scope changed during fit alignment")
        if remaining:
            raise HoodieFitAlignmentError("hoodie_fit_verification_failed", "Persisted fit flags do not match the reviewed target")
        report = {"mode": "applied", "version": VERSION, "changed": bool(changes),
                  "before_snapshot_hash": digest, "snapshot_hash": after_digest,
                  "changes": changes, "skipped": skipped, "provider_io": False,
                  "post_commit": {"public_cache": "scheduled" if changes else "unchanged"}}
        if changes:
            def refresh_public_fits():
                try:
                    report["post_commit"]["public_cache_version"] = bump_public_product_order_version()
                    report["post_commit"]["public_cache"] = "invalidated"
                except Exception:
                    # Rows are already committed: report a retryable cache issue
                    # rather than pretend the data transaction rolled back.
                    report["post_commit"]["public_cache"] = "failed"
                    report["post_commit"]["error"] = "cache_refresh_failed"
            transaction.on_commit(refresh_public_fits)
    return report
