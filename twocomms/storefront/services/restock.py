from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import timedelta
from html import escape

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.db.models import Q
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from accounts.telegram_bot import TelegramBot
from fable5.content_resolution import normalize_option_values
from fable5.services import (
    product_option_context,
    variant_allows_options,
    variant_allows_purchase,
)
from orders.telegram_notifications import TelegramNotifier
from storefront.models import Product, ProductStatus, RestockSubscription


ACTIVE_STATUSES = (
    RestockSubscription.Status.DRAFT,
    RestockSubscription.Status.ACTIVE,
    RestockSubscription.Status.SENDING,
    RestockSubscription.Status.FAILED,
)

AUTOMATIC_CHANNELS = (
    RestockSubscription.Channel.TELEGRAM,
    RestockSubscription.Channel.EMAIL,
)
DELIVERY_STATUSES = (
    RestockSubscription.Status.ACTIVE,
    RestockSubscription.Status.FAILED,
)
STALE_SENDING_AFTER = timedelta(minutes=15)
MAX_RETRY_DELAY = timedelta(hours=24)


def normalize_phone(value: str) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) == 10 and digits.startswith("0"):
        digits = f"38{digits}"
    if not 10 <= len(digits) <= 15:
        raise ValidationError("Вкажіть коректний номер телефону")
    return f"+{digits}"


def normalize_contact(channel: str, value: str) -> str:
    value = str(value or "").strip()
    if channel == RestockSubscription.Channel.TELEGRAM:
        return ""
    if channel == RestockSubscription.Channel.EMAIL:
        validate_email(value)
        return value.casefold()
    if channel in {
        RestockSubscription.Channel.PHONE,
        RestockSubscription.Channel.WHATSAPP,
    }:
        return normalize_phone(value)
    raise ValidationError("Оберіть канал зв'язку")


def build_option_labels(product, variant, option_values) -> dict:
    context = product_option_context(
        product,
        variant=variant,
        option_values=option_values,
    )
    selected = context.get("selected_values") or option_values
    result = {}
    for axis in context.get("axes") or []:
        wanted = selected.get(axis.get("code"))
        choice = next(
            (row for row in axis.get("choices") or [] if row.get("code") == wanted),
            None,
        )
        if choice:
            result[str(axis.get("label") or axis.get("code"))] = str(
                choice.get("label") or wanted
            )
    return result


def build_fingerprint(*, product_id, variant_id, size, options, channel, contact, browser_key):
    identity = contact or browser_key
    payload = json.dumps(
        [product_id, variant_id or 0, size, options, channel, identity],
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def notify_restock_admin(subscription) -> bool:
    options = " · ".join(
        f"{label}: {value}"
        for label, value in (subscription.option_labels or {}).items()
    ) or "—"
    contact = subscription.normalized_contact or "підтверджується через Telegram"
    message = (
        "🔔 <b>Очікування розміру</b>\n\n"
        f"<b>Товар:</b> {escape(subscription.product.title)}\n"
        f"<b>Розмір:</b> {escape(subscription.size)}\n"
        f"<b>Опції:</b> {escape(options)}\n"
        f"<b>Канал:</b> {escape(subscription.get_channel_display())}\n"
        f"<b>Клієнт:</b> {escape(subscription.name or '—')}\n"
        f"<b>Контакт:</b> {escape(contact)}\n"
        f"<b>ID заявки:</b> {subscription.pk}"
    )
    notifier = TelegramNotifier(
        bot_token=getattr(settings, "TELEGRAM_BOT_TOKEN", ""),
        chat_id=getattr(settings, "TELEGRAM_CHAT_ID", ""),
        async_enabled=False,
    )
    return bool(notifier.send_message(message))


def mark_admin_notification(subscription) -> bool:
    try:
        sent = notify_restock_admin(subscription)
    except Exception as exc:
        subscription.last_error = str(exc)[:1000]
        subscription.save(update_fields=["last_error", "updated_at"])
        return False
    if sent:
        subscription.admin_notified_at = timezone.now()
        subscription.last_error = ""
        subscription.save(update_fields=["admin_notified_at", "last_error", "updated_at"])
    return sent


def subscription_is_available(subscription) -> bool:
    """Recheck the exact persisted selection against live purchase rules."""

    product = Product.objects.filter(
        pk=subscription.product_id,
        status=ProductStatus.PUBLISHED,
    ).first()
    if product is None or not subscription.color_variant_id:
        return False
    variant = product.color_variants.filter(pk=subscription.color_variant_id).first()
    if variant is None:
        return False
    try:
        options = normalize_option_values(subscription.option_values or {})
    except (TypeError, ValueError):
        return False
    return variant_allows_purchase(
        product,
        variant,
        fit_code=options.get("fit", ""),
        size=subscription.size,
        option_values=options,
    )


def schedule_restock_scan(product_id, variant_id):
    """Wake matching automatic subscriptions without doing network I/O."""

    if not product_id or not variant_id:
        return 0
    return RestockSubscription.objects.filter(
        product_id=product_id,
        color_variant_id=variant_id,
        channel__in=AUTOMATIC_CHANNELS,
        status__in=DELIVERY_STATUSES,
    ).update(next_attempt_at=timezone.now())


def _filter_delivery_scope(
    queryset, *, product_id=None, variant_id=None, subscription_id=None
):
    if product_id:
        queryset = queryset.filter(product_id=product_id)
    if variant_id:
        queryset = queryset.filter(color_variant_id=variant_id)
    if subscription_id:
        queryset = queryset.filter(pk=subscription_id)
    return queryset


def due_subscriptions_queryset(
    *, product_id=None, variant_id=None, subscription_id=None, now=None
):
    now = now or timezone.now()
    queryset = RestockSubscription.objects.filter(
        channel__in=AUTOMATIC_CHANNELS,
        status__in=DELIVERY_STATUSES,
        next_attempt_at__lte=now,
    )
    queryset = _filter_delivery_scope(
        queryset,
        product_id=product_id,
        variant_id=variant_id,
        subscription_id=subscription_id,
    )
    return queryset.order_by("next_attempt_at", "created_at", "pk")


def scan_candidate_queryset(
    *, product_id=None, variant_id=None, subscription_id=None, now=None
):
    """Return due rows plus unscheduled ACTIVE rows without changing state."""

    now = now or timezone.now()
    queryset = RestockSubscription.objects.filter(
        channel__in=AUTOMATIC_CHANNELS,
    ).filter(
        Q(status=RestockSubscription.Status.ACTIVE, next_attempt_at__isnull=True)
        | Q(status__in=DELIVERY_STATUSES, next_attempt_at__lte=now)
    )
    return _filter_delivery_scope(
        queryset,
        product_id=product_id,
        variant_id=variant_id,
        subscription_id=subscription_id,
    ).order_by("created_at", "pk")


def wake_unscheduled_active_subscriptions(
    *, product_id=None, variant_id=None, subscription_id=None, now=None
) -> int:
    """Cron fallback for stock/configuration changes made outside Fable5."""

    now = now or timezone.now()
    queryset = RestockSubscription.objects.filter(
        channel__in=AUTOMATIC_CHANNELS,
        status=RestockSubscription.Status.ACTIVE,
        next_attempt_at__isnull=True,
    )
    queryset = _filter_delivery_scope(
        queryset,
        product_id=product_id,
        variant_id=variant_id,
        subscription_id=subscription_id,
    )
    return queryset.update(next_attempt_at=now)


@transaction.atomic
def claim_due_subscription(
    *, product_id=None, variant_id=None, subscription_id=None, now=None
):
    """Claim one due, currently available delivery before any network call.

    Delivery is intentionally at-least-once: if the process dies after the
    provider accepts a message but before finalize_delivery commits, stale
    recovery makes the delivery eligible again.
    """

    now = now or timezone.now()
    while True:
        subscription = (
            due_subscriptions_queryset(
                product_id=product_id,
                variant_id=variant_id,
                subscription_id=subscription_id,
                now=now,
            )
            .select_for_update()
            .select_related("product", "color_variant")
            .first()
        )
        if subscription is None:
            return None
        if not subscription_is_available(subscription):
            subscription.next_attempt_at = None
            subscription.save(update_fields=["next_attempt_at", "updated_at"])
            if subscription_id:
                return None
            continue
        subscription.status = RestockSubscription.Status.SENDING
        subscription.delivery_token = uuid.uuid4()
        subscription.notification_attempts += 1
        subscription.last_attempt_at = now
        subscription.next_attempt_at = None
        subscription.last_error = ""
        subscription.save(update_fields=[
            "status", "delivery_token", "notification_attempts",
            "last_attempt_at", "next_attempt_at", "last_error", "updated_at",
        ])
        return subscription


def _product_url(subscription) -> str:
    base = (getattr(settings, "SITE_BASE_URL", "") or "https://twocomms.shop").rstrip("/")
    return f"{base}{reverse('product', kwargs={'slug': subscription.product.slug})}"


def _option_summary(subscription) -> str:
    return " · ".join(
        f"{label}: {value}"
        for label, value in (subscription.option_labels or {}).items()
    ) or "—"


def _customer_message_context(subscription) -> dict:
    return {
        "subscription": subscription,
        "product": subscription.product,
        "product_url": _product_url(subscription),
        "option_summary": _option_summary(subscription),
    }


def _send_telegram(subscription) -> bool:
    if not subscription.telegram_chat_id:
        raise ValueError("Telegram chat is not bound")
    context = _customer_message_context(subscription)
    message = render_to_string("email/restock_available.txt", context).strip()
    if not TelegramBot().send_message(
        subscription.telegram_chat_id,
        message,
        parse_mode=None,
    ):
        raise RuntimeError("Telegram provider rejected the message")
    return True


def _send_email(subscription) -> bool:
    recipient = subscription.normalized_contact
    if not recipient:
        raise ValueError("Email recipient is missing")
    context = _customer_message_context(subscription)
    text_body = render_to_string("email/restock_available.txt", context).strip()
    html_body = render_to_string("email/restock_available.html", context)
    message = EmailMultiAlternatives(
        subject=f"{subscription.size} знову в наявності — {subscription.product.title}",
        body=text_body,
        from_email=getattr(settings, "DEFAULT_FROM_EMAIL", None),
        to=[recipient],
    )
    message.attach_alternative(html_body, "text/html")
    if message.send() != 1:
        raise RuntimeError("Email provider did not accept the message")
    return True


def send_claimed_subscription(subscription) -> bool:
    """Send an already claimed subscription; never call inside DB atomic."""

    if subscription.status != RestockSubscription.Status.SENDING:
        raise ValueError("Subscription is not claimed")
    if subscription.channel == RestockSubscription.Channel.TELEGRAM:
        return _send_telegram(subscription)
    if subscription.channel == RestockSubscription.Channel.EMAIL:
        return _send_email(subscription)
    raise ValueError("Channel is not eligible for automatic delivery")


def _retry_delay(attempts: int) -> timedelta:
    exponent = min(max(int(attempts or 1) - 1, 0), 9)
    minutes = 5 * (2 ** exponent)
    return min(timedelta(minutes=minutes), MAX_RETRY_DELAY)


@transaction.atomic
def finalize_delivery(subscription_id, delivery_token, *, success, error="") -> bool:
    subscription = RestockSubscription.objects.select_for_update().filter(
        pk=subscription_id,
        delivery_token=delivery_token,
        status=RestockSubscription.Status.SENDING,
    ).first()
    if subscription is None:
        return False
    now = timezone.now()
    subscription.delivery_token = None
    if success:
        subscription.status = RestockSubscription.Status.NOTIFIED
        subscription.customer_notified_at = now
        subscription.next_attempt_at = None
        subscription.last_error = ""
    else:
        subscription.status = RestockSubscription.Status.FAILED
        subscription.next_attempt_at = now + _retry_delay(subscription.notification_attempts)
        subscription.last_error = str(error or "Delivery failed")[:2000]
    subscription.save(update_fields=[
        "status", "delivery_token", "customer_notified_at",
        "next_attempt_at", "last_error", "updated_at",
    ])
    return True


def recover_stale_sending(
    *, product_id=None, variant_id=None, subscription_id=None, now=None
) -> int:
    now = now or timezone.now()
    queryset = RestockSubscription.objects.filter(
        status=RestockSubscription.Status.SENDING,
        last_attempt_at__lte=now - STALE_SENDING_AFTER,
    )
    queryset = _filter_delivery_scope(
        queryset,
        product_id=product_id,
        variant_id=variant_id,
        subscription_id=subscription_id,
    )
    return queryset.update(
        status=RestockSubscription.Status.FAILED,
        delivery_token=None,
        next_attempt_at=now,
        last_error="Delivery claim expired before finalization",
    )


@transaction.atomic
def create_subscription(
    *,
    product,
    variant,
    size,
    option_values,
    channel,
    name,
    contact,
    user=None,
    browser_key="",
    ip_hash="",
    user_agent="",
):
    options = normalize_option_values(option_values or {})
    if variant is not None and options and not variant_allows_options(variant, options):
        raise ValidationError("Обрана конфігурація недоступна")
    fit_code = options.get("fit", "")
    if variant is not None and variant_allows_purchase(
        product,
        variant,
        fit_code=fit_code,
        size=size,
        option_values=options,
    ):
        raise ValidationError("SIZE_ALREADY_AVAILABLE")
    normalized = normalize_contact(channel, contact)
    labels = build_option_labels(product, variant, options)
    fingerprint = build_fingerprint(
        product_id=product.pk,
        variant_id=getattr(variant, "pk", None),
        size=size,
        options=options,
        channel=channel,
        contact=normalized,
        browser_key=browser_key,
    )
    existing = RestockSubscription.objects.filter(
        fingerprint=fingerprint,
        status__in=ACTIVE_STATUSES,
    ).order_by("-created_at").first()
    if existing:
        return existing, False

    status = (
        RestockSubscription.Status.DRAFT
        if channel == RestockSubscription.Channel.TELEGRAM
        else RestockSubscription.Status.ACTIVE
    )
    subscription = RestockSubscription.objects.create(
        product=product,
        color_variant=variant,
        user=user if getattr(user, "is_authenticated", False) else None,
        size=str(size or "").strip().upper()[:20],
        option_values=options,
        option_labels=labels,
        channel=channel,
        status=status,
        name=str(name or "").strip()[:160],
        contact=str(contact or "").strip()[:254],
        normalized_contact=normalized,
        fingerprint=fingerprint,
        browser_session_key=browser_key[:64],
        request_ip_hash=ip_hash[:64],
        user_agent=user_agent[:255],
    )
    if status == RestockSubscription.Status.ACTIVE:
        transaction.on_commit(lambda: mark_admin_notification(subscription))
    return subscription, True


@transaction.atomic
def activate_telegram_subscription(session):
    restock_id = (session.metadata or {}).get("restock_id")
    subscription = RestockSubscription.objects.select_for_update().filter(
        pk=restock_id,
        channel=RestockSubscription.Channel.TELEGRAM,
    ).first()
    if subscription is None:
        return None
    subscription.telegram_user_id = session.telegram_user_id
    subscription.telegram_chat_id = session.chat_id
    subscription.telegram_username = session.telegram_username or ""
    subscription.verified_phone = session.phone or ""
    subscription.contact = (
        f"@{session.telegram_username.lstrip('@')}"
        if session.telegram_username
        else session.phone or ""
    )
    subscription.normalized_contact = session.phone or str(session.telegram_user_id or "")
    subscription.status = RestockSubscription.Status.ACTIVE
    subscription.next_attempt_at = timezone.now()
    subscription.save(update_fields=[
        "telegram_user_id", "telegram_chat_id", "telegram_username",
        "verified_phone", "contact", "normalized_contact", "status",
        "next_attempt_at", "updated_at",
    ])
    transaction.on_commit(lambda: mark_admin_notification(subscription))
    return subscription
