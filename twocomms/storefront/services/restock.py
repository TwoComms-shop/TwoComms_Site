from __future__ import annotations

import hashlib
import json
import re
from html import escape

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.utils import timezone

from fable5.content_resolution import normalize_option_values
from fable5.services import (
    product_option_context,
    variant_allows_options,
    variant_allows_purchase,
)
from orders.telegram_notifications import TelegramNotifier
from storefront.models import RestockSubscription


ACTIVE_STATUSES = (
    RestockSubscription.Status.DRAFT,
    RestockSubscription.Status.ACTIVE,
    RestockSubscription.Status.FAILED,
)


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
    subscription.save(update_fields=[
        "telegram_user_id", "telegram_chat_id", "telegram_username",
        "verified_phone", "contact", "normalized_contact", "status", "updated_at",
    ])
    transaction.on_commit(lambda: mark_admin_notification(subscription))
    return subscription
