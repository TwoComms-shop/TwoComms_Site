"""Phase 21 (PR-4c3) — review-related signal handlers.

Two responsibilities:

1. ``notify_moderator_on_pending_review`` — fires once per row when a
   new ``Review`` lands with ``status='pending'``. Sends a Telegram
   ping (admin chat) so the team can clear the queue without
   constantly checking the admin. No email yet — most moderators are
   on Telegram already.

2. ``ping_indexnow_on_first_approval`` — fires when a ``Review``
   transitions to ``approved``. We submit just the canonical product
   URL (no variant fan-out) to IndexNow so Google/Bing re-crawl the
   page; their renderer will pick up the new aggregateRating + Review
   nested JSON-LD on the next visit.

Both handlers fail safely: any exception is logged and swallowed —
review submission must NEVER block on a flaky external service.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from .models import Review, ReviewStatus


logger = logging.getLogger(__name__)


# In-memory marker for "status changed in this save() call". We avoid
# adding a column for this — pre_save reads the previous DB row's
# status and stamps an attribute on the instance which post_save
# checks. Cheap and request-local.
_STATUS_CHANGED_ATTR = "_phase21_status_was"


@receiver(pre_save, sender=Review)
def _capture_previous_status(sender, instance: Review, **kwargs):
    """Stash the pre-save status so post_save can detect transitions."""
    if not instance.pk:
        setattr(instance, _STATUS_CHANGED_ATTR, None)
        return
    try:
        prev = Review.objects.only("status").get(pk=instance.pk)
        setattr(instance, _STATUS_CHANGED_ATTR, prev.status)
    except Review.DoesNotExist:
        setattr(instance, _STATUS_CHANGED_ATTR, None)


@receiver(post_save, sender=Review)
def notify_moderator_on_pending_review(sender, instance: Review, created: bool, **kwargs):
    """Telegram ping when a review enters the moderation queue."""
    if not created or instance.status != ReviewStatus.PENDING:
        return
    try:
        _send_pending_telegram(instance)
    except Exception:  # pragma: no cover — defensive
        logger.exception("reviews.notify.pending failed for review=%s", instance.pk)


@receiver(post_save, sender=Review)
def ping_indexnow_on_first_approval(sender, instance: Review, created: bool, **kwargs):
    """IndexNow ping when status flips ``pending|rejected`` → ``approved``."""
    previous = getattr(instance, _STATUS_CHANGED_ATTR, None)
    if instance.status != ReviewStatus.APPROVED:
        return
    if previous == ReviewStatus.APPROVED:
        return  # Nothing changed — already approved.
    try:
        _submit_indexnow_for_product(instance.product)
    except Exception:  # pragma: no cover — defensive
        logger.exception(
            "reviews.indexnow.approval-ping failed for review=%s product=%s",
            instance.pk, instance.product_id,
        )


# --------------------------------------------------------------------
# Internal helpers
# --------------------------------------------------------------------

def _site_base_url() -> str:
    """Match the convention used elsewhere in the codebase."""
    return (getattr(settings, "SITE_BASE_URL", "") or "https://twocomms.shop").rstrip("/")


def _send_pending_telegram(review: Review) -> None:
    """Best-effort Telegram notification for moderators.

    Uses ``orders.telegram_notifications.TelegramNotifier`` if it can
    be configured from environment variables; otherwise logs and
    returns silently.
    """
    try:
        from orders.telegram_notifications import TelegramNotifier
    except Exception:
        logger.debug("reviews.notify: TelegramNotifier import unavailable")
        return

    bot_token = (getattr(settings, "TELEGRAM_BOT_TOKEN", "") or "").strip()
    chat_id = (getattr(settings, "TELEGRAM_CHAT_ID", "") or "").strip()
    if not bot_token or not chat_id:
        logger.debug("reviews.notify: telegram bot/chat not configured; skipping")
        return

    notifier = TelegramNotifier(bot_token=bot_token, chat_id=chat_id, async_enabled=False)
    if not getattr(notifier, "is_configured", lambda: True)():
        return

    base = _site_base_url()
    product_url = f"{base}/product/{review.product.slug}/"
    admin_url = f"{base}/admin/reviews/review/{review.pk}/change/"
    body_preview = (review.body or "")[:300]
    if len(review.body or "") > 300:
        body_preview = body_preview.rstrip() + "…"

    text = (
        "🆕 <b>Новий відгук на модерації</b>\n"
        f"⭐ <b>{review.rating}/5</b> — <a href=\"{product_url}\">{review.product.title}</a>\n"
        f"👤 {review.author_name}"
        + (" ✅ verified" if review.is_verified_purchase else "")
        + "\n"
        + (f"📌 <i>{review.title}</i>\n" if review.title else "")
        + f"\n{body_preview}\n\n"
        f"🔧 <a href=\"{admin_url}\">Відкрити в адмінці</a>"
    )
    # send_admin_message handles HTML / fallback errors internally.
    notifier.send_admin_message(text, parse_mode="HTML")


def _submit_indexnow_for_product(product) -> None:
    """Tell IndexNow the product page has fresh content."""
    try:
        from storefront.services.indexnow import submit_indexnow_urls, is_indexnow_configured
    except Exception:
        logger.debug("reviews.indexnow: service unavailable")
        return

    if not is_indexnow_configured():
        return

    base = _site_base_url()
    submit_indexnow_urls([f"{base}/product/{product.slug}/"])



@receiver(post_save, sender=Review)
def invalidate_public_review_cache(sender, instance, **kwargs):
    from django.db import transaction
    from storefront.services.catalog_helpers import bump_public_product_order_version
    if instance.status == ReviewStatus.APPROVED or getattr(instance, _STATUS_CHANGED_ATTR, None) == ReviewStatus.APPROVED:
        transaction.on_commit(bump_public_product_order_version)
