"""Моделі апгрейду Instagram-бота TwoComms (Phase 0+).

Винесені в окремий модуль, щоб не роздувати і без того велику models.py.
Імпортуються в кінці management/models.py (`from .ig_bot_models import *`),
тож app_label='management' визначається автоматично, а міграції лягають у
management/migrations. Перехресні FK задаються рядком ('management.IgClient',
'orders.Order') — без жорстких import, щоб уникнути циклічних залежностей.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.signing import salted_hmac
from django.db import transaction
from django.db import models
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.translation import gettext_lazy as _

__all__ = [
    "InstagramBotRawEvent",
    "IgWebhookInboxEvent",
    "IgClient",
    "IgDeal",
    "IgDealInvoiceLifecycle",
    "IgPaymentEvent",
    "IgPaymentProjection",
    "IgDealItem",
    "BotInstruction",
    "BotPolicyPublication",
    "BotQuickLink",
    "BotAdCampaign",
    "IgClientStageEvent",
    "IgFollowUpTask",
    "IgPollCursor",
    "IgInboxRefreshRun",
    "IgInboxRefreshItem",
    "IgConversationSignal",
    "IgObjection",
    "IgObjectionAttempt",
    "IgAnalysisMaterialityEvent",
    "IgConversationAnalysisSnapshot",
    "IgConversationAnalysisResult",
    "IgAnalysisProposal",
    "IgMemoryFact",
    "IgMemoryFactEvidence",
    "IgMemoryHead",
    "IG_MEMORY_MAX_CHAIN_DEPTH",
    "IgConversationAnalysisEvent",
    "IgConversationAnalysisJob",
    "IgAiReplyRecoveryJob",
    "IgProviderIncident",
    "IgClientDegradationEpisode",
    "IgCustomerTurn",
    "IgTurnMessage",
    "IgCustomerTurnRevision",
    "IgSourceActionReceipt",
    "IgTurnRevisionSource",
    "IgRevisionDeliveryEffect",
    "IgDeferredEcho",
    "IgAlertRateBucket",
    "IgPermissionTransitionJob",
    "IgMetaEventLog",
    "BotDataDeletionRequest",
    "IgBotNotification",
    "IgBotNotificationAudit",
    "IgPaymentConfirmationReview",
    "IgPaymentReviewDecision",
    "IgOrderAttribution",
    "IgOrderLinkEvent",
    "IgOrderAssignment",
    "IgOrderAssignmentEvent",
    "IgUgcReward",
    "IgUgcEvidenceAssessment",
    "IgUgcRewardLifetime",
    "IgUgcRewardDelivery",
    "IgUgcRewardLifecycleJob",
    "IgOrderCustomerEvent",
    "IgCommercialEpisode",
    "IgCommercialEpisodeEvent",
    "IgFunnelStepEvent",
    "IgFunnelDropOff",
    "IgFunnelNodeState",
    "IgPostSaleCase",
    "IgOrderShipment",
    "BotPromptRevision",
    "IgFunnelResetAudit",
    "IgCheckoutProposal",
    "IgCheckoutProposalItem",
    "IgCheckoutRevision",
    "IgCheckoutAccessToken",
    "IgCheckoutInvoiceGeneration",
    "IgCheckoutInvoiceGenerationEvent",
    "IgCheckoutInventoryReservation",
    "IgLifecycleEvent",
    "IgFollowCapabilityState",
    "IgFollowState",
    "IgFollowObservation",
    "IgFollowRefreshJob",
    "IgFollowCtaDecision",
    "IgPaymentFollowPreparation",
    "IgCommerceSelectionSession",
    "IgCommerceSelectionTransition",
    "IgCommerceTurnDecision",
    "IgCommerceManagerReview",
    "provider_evidence_signature",
]


def provider_evidence_signature(
    *,
    deal_id,
    client_id,
    provider,
    source,
    invoice_id,
    provider_status,
    payload_digest,
):
    """Authenticate provider observations before they can authorize money state."""
    canonical = json.dumps(
        {
            "client_id": int(client_id),
            "deal_id": int(deal_id),
            "invoice_id": str(invoice_id or ""),
            "payload_digest": str(payload_digest or ""),
            "provider": str(provider or ""),
            "provider_status": str(provider_status or ""),
            "source": str(source or ""),
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return salted_hmac(
        "twocomms.ig_payment_event.v1",
        canonical,
        algorithm="sha1",
    ).hexdigest()


class InstagramBotRawEvent(models.Model):
    """Сире збереження вхідних вебхук-подій IG для діагностики форматів.

    Дозволяє побачити реальний payload пересланого поста / story_mention /
    відповіді на сторис / рекламного referral / echo менеджера на цьому
    акаунті, а не покладатись на здогадки про формат Meta.
    """

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    sender_id = models.CharField(max_length=64, blank=True, default="", db_index=True)
    attachment_types = models.CharField(max_length=255, blank=True, default="")
    has_referral = models.BooleanField(default=False)
    has_echo = models.BooleanField(default=False)
    note = models.CharField(max_length=255, blank=True, default="")
    payload = models.TextField(blank=True, default="")

    class Meta:
        verbose_name = "IG raw webhook event"
        verbose_name_plural = "IG raw webhook events"
        ordering = ["-id"]
        indexes = [
            models.Index(fields=["-created_at"], name="ig_rawevent_created"),
        ]

    def __str__(self) -> str:  # pragma: no cover - тривіально
        return f"RawEvent#{self.pk} {self.sender_id} [{self.attachment_types}]"


class IgWebhookInboxEvent(models.Model):
    """One namespace-scoped webhook receipt before any bot side effect runs."""

    class Decision(models.TextChoices):
        ACCEPTED = "accepted", _("Accepted")
        REJECTED = "rejected", _("Rejected")
        BLOCKED = "blocked", _("Blocked")

    namespace = models.CharField(max_length=128)
    event_key = models.CharField(max_length=255)
    owner_id = models.CharField(max_length=64, blank=True, default="")
    customer_igsid = models.CharField(max_length=64, blank=True, default="", db_index=True)
    decision = models.CharField(max_length=16, choices=Decision.choices, db_index=True)
    reason = models.CharField(max_length=64, blank=True, default="")
    payload = models.JSONField(default=dict, blank=True)
    payload_digest = models.CharField(max_length=64)
    received_at = models.DateTimeField(auto_now_add=True, db_index=True)
    processed_at = models.DateTimeField(null=True, blank=True, db_index=True)
    attempts = models.PositiveIntegerField(default=0)
    last_error = models.CharField(max_length=64, blank=True, default="")
    next_attempt_at = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(
                fields=["namespace", "event_key"],
                name="ig_webhook_inbox_namespace_event_uniq",
            ),
        ]
        indexes = [
            models.Index(
                fields=["decision", "processed_at", "id"],
                name="ig_webhook_inbox_drain_idx",
            ),
        ]


class BotDataDeletionRequest(models.Model):
    """Public/Meta deletion request receipt for DIRECT_BOT data."""

    class Source(models.TextChoices):
        MANUAL_FORM = "manual_form", "Manual form"
        META_CALLBACK = "meta_callback", "Meta callback"

    class Status(models.TextChoices):
        COMPLETED = "completed", "Completed"
        NO_MATCH = "no_match", "No matching records"
        RECEIVED = "received", "Received"
        ERASING = "erasing", "Erasing"
        # Заявка принята, но владение идентификатором ещё не подтверждено.
        # Публичная форма создаёт ТОЛЬКО этот статус: удалять данные по
        # анонимному POST нельзя (F-SEC-002). Переход в COMPLETED/NO_MATCH
        # делает менеджер через `services.ig_data_deletion`.
        PENDING_VERIFICATION = "pending_verification", "Pending ownership verification"

    confirmation_code = models.CharField(max_length=32, unique=True, db_index=True)
    source = models.CharField(max_length=24, choices=Source.choices)
    identifier = models.CharField(max_length=255, blank=True, default="")
    normalized_identifier = models.CharField(max_length=255, blank=True, default="", db_index=True)
    meta_user_id = models.CharField(max_length=128, blank=True, default="", db_index=True)
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.RECEIVED)
    deleted_clients_count = models.PositiveIntegerField(default=0)
    deleted_messages_count = models.PositiveIntegerField(default=0)
    deleted_raw_events_count = models.PositiveIntegerField(default=0)
    deleted_logs_count = models.PositiveIntegerField(default=0)
    detail = models.TextField(blank=True, default="")
    erasure_lease_token = models.CharField(
        max_length=64, blank=True, default="", db_default=""
    )
    erasure_lease_until = models.DateTimeField(null=True, blank=True, db_index=True)
    erasure_target_client_ids = models.JSONField(default=list, db_default=[], blank=True)
    erasure_target_message_ids = models.JSONField(default=list, db_default=[], blank=True)
    erasure_target_sender_ids = models.JSONField(default=list, db_default=[], blank=True)
    erasure_target_inbox_ids = models.JSONField(default=list, db_default=[], blank=True)
    erasure_cutoff_at = models.DateTimeField(null=True, blank=True)
    erasure_actor_label = models.CharField(
        max_length=150, blank=True, default="", db_default=""
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "DIRECT_BOT data deletion request"
        verbose_name_plural = "DIRECT_BOT data deletion requests"
        ordering = ["-id"]
        indexes = [
            models.Index(fields=["status", "-created_at"], name="bot_del_status_dt"),
            models.Index(fields=["status", "erasure_lease_until"], name="bot_del_lease_idx"),
            models.Index(fields=["source", "-created_at"], name="bot_del_source_dt"),
        ]

    def mark_completed(self, *, status: str | None = None) -> None:
        if status:
            self.status = status
        self.completed_at = timezone.now()
        self.save(update_fields=[
            "status",
            "deleted_clients_count",
            "deleted_messages_count",
            "deleted_raw_events_count",
            "deleted_logs_count",
            "detail",
            "completed_at",
        ])


class IgBotNotification(models.Model):
    """Durable, idempotent Telegram notification attempt for the IG bot."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        SENDING = "sending", "Sending"
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"
        UNKNOWN = "unknown", "Delivery unknown"
        DEAD_LETTER = "dead_letter", "Manual review required"
        RESOLVED = "resolved", "Resolved by operator"

    client = models.ForeignKey(
        "management.IgClient",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="bot_notifications",
        # Kept constraint-free for compatibility with installations that have
        # not yet completed the runtime-table InnoDB migration.
        db_constraint=False,
    )
    event_type = models.CharField(max_length=64, default="generic", db_index=True)
    dedupe_key = models.CharField(max_length=255, unique=True)
    payload = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING, db_index=True)
    attempts = models.PositiveIntegerField(default=0)
    telegram_message_id = models.CharField(max_length=64, blank=True, default="")
    last_error = models.CharField(max_length=500, blank=True, default="")
    failure_kind = models.CharField(max_length=32, blank=True, default="")
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    next_attempt_at = models.DateTimeField(null=True, blank=True, db_index=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-id"]
        indexes = [
            models.Index(fields=["status", "-created_at"], name="ig_notif_status_dt"),
            models.Index(fields=["client", "event_type", "-created_at"], name="ig_notif_client_event"),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial representation
        return f"IgBotNotification#{self.pk} {self.event_type}/{self.status}"


class IgBotNotificationAudit(models.Model):
    """Immutable operator action history for notification recovery."""

    notification = models.ForeignKey(
        "management.IgBotNotification",
        on_delete=models.CASCADE,
        related_name="audit_events",
        db_constraint=False,
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="ig_notification_audits",
        db_constraint=False,
    )
    action = models.CharField(max_length=32)
    from_status = models.CharField(max_length=16)
    to_status = models.CharField(max_length=16)
    note = models.CharField(max_length=500, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-id"]
        indexes = [
            models.Index(fields=["notification", "-created_at"], name="ig_notif_audit_dt"),
        ]


class IgClient(models.Model):
    """Картка співрозмовника в Instagram Direct (B2C).

    Окрема сутність від B2B `Client` (холодний обзвон магазинів). Тут — кінцевий
    покупець, що пише в Direct: профіль, стадія воронки, атрибуція реклами,
    стисла пам'ять діалогу, лічильники, антиспам, стоп/перехоплення менеджером.
    """

    class Stage(models.TextChoices):
        NEW = "new", _("Написав")
        QUALIFYING = "qualifying", _("З'ясовуємо потребу")
        PRODUCT_MATCHED = "product_matched", _("Товар і ціна визначені")
        CHECKOUT = "checkout", _("Обирає оплату")
        PAYMENT_PENDING = "payment_pending", _("Очікуємо оплату")
        PAID = "paid", _("Оплачено")
        ORDER_CREATED = "order_created", _("Замовлення створено")
        DONE = "done", _("Завершено")
        LEAD_TO_MANAGER = "lead_manager", _("Передано менеджеру")
        SPAM = "spam", _("Спам / заблоковано")
        COLD = "cold", _("Не відповідає / охолов")

    class Intent(models.TextChoices):
        UNKNOWN = "unknown", _("Невідомо")
        PRODUCT = "product", _("Готовий товар")
        CUSTOM_PRINT = "custom_print", _("Кастомний принт")
        PRICE = "price", _("Ціна")
        SIZE = "size", _("Розмір")
        PAYMENT = "payment", _("Оплата")
        DELIVERY = "delivery", _("Доставка")
        ORDER_STATUS = "order_status", _("Статус замовлення")
        SUPPORT = "support", _("Підтримка")
        SPAM = "spam", _("Спам")

    class Objection(models.TextChoices):
        NONE = "none", _("Немає")
        PRICE = "price", _("Дорого")
        PREPAYMENT = "prepayment", _("Передоплата")
        SIZE = "size", _("Розмір")
        THINKING = "thinking", _("Подумаю")
        NO_REPLY = "no_reply", _("Не відповідає")
        NO_BUY = "no_buy", _("Не купує")
        TRUST = "trust", _("Довіра")
        DELIVERY = "delivery", _("Доставка")
        OTHER = "other", _("Інше")

    class DeliveryStatus(models.TextChoices):
        ADVANCED_ACCESS = "advanced_access", _("Meta не дозволяє відповідь: потрібен Advanced Access")
        WINDOW_CLOSED = "window_closed", _("24-годинне вікно Meta закрито")
        MESSAGE_REQUEST_CHECK = "message_request_check", _("Перевірте «Запити» в Instagram")
        SEND_BLOCKED = "send_blocked", _("Meta тимчасово або постійно блокує відповідь")

    # Головна воронка (для прогрес-бару/кружечків у картці).
    FUNNEL_ORDER = [
        Stage.NEW,
        Stage.QUALIFYING,
        Stage.PRODUCT_MATCHED,
        Stage.CHECKOUT,
        Stage.PAYMENT_PENDING,
        Stage.PAID,
        Stage.ORDER_CREATED,
        Stage.DONE,
    ]

    # Identity
    igsid = models.CharField(_("IG sender id"), max_length=64, unique=True, db_index=True)
    username = models.CharField(_("Username"), max_length=120, blank=True, default="")
    display_name = models.CharField(_("Ім'я"), max_length=255, blank=True, default="")
    profile_pic_url = models.CharField(_("Аватар URL"), max_length=600, blank=True, default="")
    # Локальна копія аватарки (media). IG CDN-URL протухають і мають hotlink-захист,
    # тож для CRM зберігаємо власну копію й віддаємо локальний URL.
    avatar_local = models.CharField(_("Аватар (локально)"), max_length=300, blank=True, default="")
    profile_fetched_at = models.DateTimeField(null=True, blank=True)
    profile_sync_attempted_at = models.DateTimeField(null=True, blank=True)
    profile_sync_failures = models.PositiveSmallIntegerField(default=0)
    profile_sync_next_at = models.DateTimeField(null=True, blank=True, db_index=True)
    profile_sync_error_kind = models.CharField(max_length=32, blank=True, default="")

    # Контакти (для ліда / замовлення)
    phone = models.CharField(_("Телефон"), max_length=50, blank=True, default="")
    phone_normalized = models.CharField(max_length=50, blank=True, default="", db_index=True)

    # Воронка
    stage = models.CharField(
        _("Стадія"), max_length=24, choices=Stage.choices, default=Stage.NEW, db_index=True
    )
    stage_updated_at = models.DateTimeField(null=True, blank=True)

    # Керування ботом / перехоплення менеджером
    bot_paused = models.BooleanField(_("Бот на паузі"), default=False, db_index=True)
    paused_reason = models.CharField(max_length=255, blank=True, default="")
    paused_at = models.DateTimeField(null=True, blank=True)
    manager_takeover = models.BooleanField(_("Веде менеджер"), default=False)
    # Monotonic permission epoch. Pause/takeover increments it so a worker
    # that generated before the transition cannot send afterwards.
    reply_permission_epoch = models.PositiveBigIntegerField(default=0)
    opted_out_at = models.DateTimeField(null=True, blank=True, db_index=True)
    opt_out_message_id = models.PositiveBigIntegerField(null=True, blank=True)
    opted_in_at = models.DateTimeField(null=True, blank=True)
    opted_in_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="ig_manual_opt_ins",
        db_constraint=False,
    )

    # Закріплений товар діалогу (визначений за [PRODUCT:id] від моделі або
    # впевненим матчингом фото). Посилання на оплату формується саме на нього,
    # без повторного вгадування. Скидається після створення замовлення.
    current_product = models.ForeignKey(
        "storefront.Product",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        verbose_name=_("Закріплений товар"),
    )
    current_size = models.CharField(_("Поточний розмір"), max_length=16, blank=True, default="")
    current_color = models.CharField(_("Поточний колір"), max_length=64, blank=True, default="")
    current_qty = models.PositiveIntegerField(_("Поточна кількість"), default=1)
    current_product_confidence = models.DecimalField(
        _("Впевненість у товарі"), max_digits=4, decimal_places=2, default=0
    )
    current_commercial_episode = models.ForeignKey(
        "management.IgCommercialEpisode",
        null=True,
        blank=True,
        on_delete=models.DO_NOTHING,
        related_name="current_for_clients",
        db_constraint=False,
    )

    # Sales brain / CRM state
    language = models.CharField(max_length=8, blank=True, default="", db_index=True)
    intent = models.CharField(
        max_length=32, choices=Intent.choices, default=Intent.UNKNOWN, db_index=True
    )
    buying_readiness = models.PositiveSmallIntegerField(default=0, db_index=True)
    primary_objection = models.CharField(
        max_length=32, choices=Objection.choices, default=Objection.NONE, db_index=True
    )
    lost_reason = models.CharField(max_length=64, blank=True, default="", db_index=True)
    hidden_at = models.DateTimeField(null=True, blank=True, db_index=True)
    hidden_reason = models.CharField(max_length=255, blank=True, default="")
    # Privacy erasure fence: once set, ingress/capture cannot create new owned
    # customer media while the two-phase deletion is draining existing blobs.
    privacy_erasure_started_at = models.DateTimeField(null=True, blank=True)
    # Короткоживуча lease одного worker-а. Вона не є станом воронки: потрібна
    # лише щоб hide не підтверджувався, поки триває відповідь цьому клієнту.
    automation_lease_token = models.CharField(max_length=40, blank=True, default="")
    automation_lease_until = models.DateTimeField(null=True, blank=True)
    discount_offered_percent = models.PositiveSmallIntegerField(default=0)
    next_followup_at = models.DateTimeField(null=True, blank=True, db_index=True)
    followup_level = models.PositiveSmallIntegerField(default=0)
    last_manager_message_at = models.DateTimeField(null=True, blank=True)
    sales_context = models.JSONField(default=dict, blank=True)

    # Остання підтверджена технічна перешкода доставки. Це не замінює стадію
    # воронки та не означає автоматичну передачу ліда менеджеру.
    delivery_status = models.CharField(
        max_length=32, choices=DeliveryStatus.choices, blank=True, default="", db_index=True
    )
    delivery_error = models.CharField(max_length=500, blank=True, default="")
    delivery_http_code = models.PositiveSmallIntegerField(null=True, blank=True)
    delivery_graph_code = models.PositiveIntegerField(null=True, blank=True)
    delivery_graph_subcode = models.PositiveIntegerField(null=True, blank=True)
    delivery_failed_at = models.DateTimeField(null=True, blank=True)

    # Атрибуція реклами (Click-to-IG-Direct)
    ad_ref = models.CharField(max_length=255, blank=True, default="")
    ad_id = models.CharField(max_length=64, blank=True, default="", db_index=True)
    ad_source = models.CharField(max_length=64, blank=True, default="")
    ad_title = models.CharField(max_length=255, blank=True, default="")
    ad_creative_url = models.CharField(max_length=600, blank=True, default="")
    referral_payload = models.JSONField(default=dict, blank=True)

    # Пам'ять діалогу (rolling summary — Task 10)
    memory_summary = models.TextField(blank=True, default="")
    memory_updated_at = models.DateTimeField(null=True, blank=True)

    # Лічильники / конверсія
    purchases_count = models.PositiveIntegerField(default=0)
    total_spent = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    conversion_flags = models.JSONField(default=dict, blank=True)

    # Антиспам
    spam_strikes = models.PositiveSmallIntegerField(default=0)
    is_blocked = models.BooleanField(default=False)

    # Тайминги
    first_contact_at = models.DateTimeField(null=True, blank=True)
    last_message_at = models.DateTimeField(null=True, blank=True, db_index=True)
    # Окно Meta считается ТОЛЬКО от этого поля. `last_message_at` смешивает
    # входящие и исходящие (у него четыре писателя, включая backfill без фильтра
    # по роли) и остаётся для сортировки списка и отображения — там смешивание
    # удобно. Разделение полей дешевле, чем дисциплинировать четырёх писателей.
    last_user_message_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_bot_reply_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("IG клієнт")
        verbose_name_plural = _("IG клієнти")
        ordering = ["-last_message_at", "-id"]
        indexes = [
            models.Index(fields=["stage", "-last_message_at"], name="ig_client_stage_dt"),
            models.Index(fields=["-last_message_at"], name="ig_client_lastmsg"),
            models.Index(fields=["intent", "stage"], name="ig_client_intent_stage"),
            models.Index(fields=["hidden_at", "-last_message_at"], name="ig_client_hidden_dt"),
            models.Index(fields=["next_followup_at"], name="ig_client_next_fu"),
        ]

    def save(self, *args, **kwargs):
        # Нормалізація телефону (lazy import, щоб уникнути циклічного імпорту).
        if self.phone:
            try:
                from management.models import normalize_phone

                self.phone_normalized = normalize_phone(self.phone)
            except Exception:
                pass
        super().save(*args, **kwargs)

    def __str__(self) -> str:  # pragma: no cover - тривіально
        label = self.username or self.display_name or self.igsid
        return f"IgClient({label}, {self.stage})"

    @classmethod
    def get_or_create_for_sender(cls, igsid: str, defaults: dict | None = None) -> "IgClient":
        obj, _created = cls.objects.get_or_create(igsid=igsid, defaults=defaults or {})
        return obj

    def set_stage(self, new_stage: str, reason: str = "") -> None:
        """Оновлює стадію + час і фіксує перехід у таймлайні (IgClientStageEvent).

        Э3.2: стадія і подія пишуться в ОДНІЙ транзакції. Раніше стадія
        зберігалась, а створення `IgClientStageEvent` стояло в окремому
        `try/except`, який **проглатував** виключення. `ig_funnel_fsm` трактував
        відсутність виключення як успішний перехід, тому в CRM з'являлась стадія
        без жодного evidence у таймлайні — і на питання «як клієнт тут опинився»
        відповіді не було. Без атомарності не можна довіряти ні воронці, ні
        аналітиці, а на них опирається майже все інше.

        Виключення тепер НЕ проглатується: транзакція відкочує і стадію, і
        подію, а `apply_stage` повертає `write_failed`. Тихого успіху більше
        немає.

        Атомарність тут реально виконувана: зріз Э0.2 на production підтвердив
        InnoDB для `management_igclient` і `management_igclientstageevent`.
        """
        from django.db import transaction
        from django.utils import timezone

        old = self.stage
        stage_at = timezone.now()
        with transaction.atomic():
            from management.models import IgClientStageEvent

            self.stage = new_stage
            self.stage_updated_at = stage_at
            self.save(update_fields=["stage", "stage_updated_at", "updated_at"])
            IgClientStageEvent.objects.create(
                client=self,
                from_stage=old or "",
                to_stage=new_stage,
                reason=(reason or "")[:255],
            )

    def touch_inbound(self) -> None:
        """Фіксує вхідне повідомлення: first_contact_at (раз) і last_message_at."""
        from django.utils import timezone

        now = timezone.now()
        fields = ["last_message_at", "last_user_message_at", "updated_at"]
        if not self.first_contact_at:
            self.first_contact_at = now
            fields.append("first_contact_at")
        self.last_message_at = now
        # Вікно Meta відкриває ТІЛЬКИ повідомлення клієнта. `last_message_at`
        # має чотирьох писателів, один з них — backfill з `Max("created_at")`
        # без фільтра по ролі, тому вихідне повідомлення бота потрапляло в поле
        # і «відкривало» вікно, якого не було (Э2.6).
        self.last_user_message_at = now
        self.save(update_fields=fields)

    @property
    def meta_window_anchor(self):
        """Єдина точка істини для початку відліку 24-годинного вікна Meta.

        Читати вікно напряму з `last_message_at` не можна: у нього пишуть і
        вихідні повідомлення, тому власне повідомлення бота «відкривало» вікно,
        якого не було.

        Перехідний dual-read: якщо нове поле ще NULL (клієнт створений до
        міграції `0172` і не потрапив у backfill), падаємо назад на
        `last_message_at`. Це expand-фаза: для таких рядків ми просто не маємо
        інформації, а вважати вікно закритим для всієї історії означало б
        замовкнути для реальних клієнтів. Для всіх рядків з заповненим полем
        (увесь новий трафік і backfill) діє строгий контракт. Прибрати fallback
        можна тільки після перевірки backfill на production — це contract-фаза.
        """
        return self.last_user_message_at or self.last_message_at or self.first_contact_at

    def funnel_progress(self) -> list[dict]:
        """Прогрес по основних стадіях воронки (для кружечків у картці)."""
        order = list(self.FUNNEL_ORDER)
        try:
            cur = order.index(self.stage)
        except ValueError:
            cur = -1
        result = []
        for i, st in enumerate(order):
            result.append({
                "stage": st.value,
                "label": str(st.label),
                "done": cur >= 0 and i <= cur,
                "current": st.value == self.stage,
            })
        return result


class IgDeal(models.Model):
    """«Кошик» діалогу: вибрані позиції, сума, оплата, invoice, дані НП.

    Замовлення (orders.Order) створюється ТІЛЬКИ після підтвердженої оплати
    (рішення Q2), тож тут зберігаємо invoice_id/url і чекаємо вебхук/поллінг.
    """

    class Status(models.TextChoices):
        DRAFT = "draft", _("Чернетка")
        QUOTED = "quoted", _("Названо ціну")
        AWAITING_PAYMENT = "awaiting_payment", _("Очікує оплату")
        PAID = "paid", _("Оплачено")
        ORDER_CREATED = "order_created", _("Замовлення створено")
        CANCELLED = "cancelled", _("Скасовано")

    class PayType(models.TextChoices):
        ONLINE_FULL = "online_full", _("Повна онлайн-оплата")
        PREPAYMENT = "prepayment", _("Передоплата за погодженою сумою")
        PREPAY_200 = "prepay_200", _("Передоплата 200 грн (legacy)")

    class PaymentTruth(models.TextChoices):
        UNVERIFIED = "unverified", _("Не підтверджено")
        PENDING = "pending", _("Перевіряється")
        CONFIRMED = "confirmed", _("Оплату підтверджено")
        PARTIALLY_REFUNDED = "partially_refunded", _("Частково повернено")
        REFUNDED = "refunded", _("Повністю повернено")
        REVERSED = "reversed", _("Платіж скасовано банком")
        FAILED = "failed", _("Оплата не пройшла")
        CANCELLED = "cancelled", _("Оплату скасовано")

    class DeliveryStatus(models.TextChoices):
        UNVERIFIED = "unverified", _("Доставку не підтверджено")
        VALIDATED = "validated", _("Доставку підтверджено довідником НП")
        NEEDS_REVIEW = "needs_review", _("Потрібна перевірка доставки")
        INVALID = "invalid", _("Дані доставки невалідні")

    client = models.ForeignKey(
        "management.IgClient", on_delete=models.CASCADE, related_name="deals"
    )
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.DRAFT, db_index=True
    )
    pay_type = models.CharField(
        max_length=20, choices=PayType.choices, default=PayType.ONLINE_FULL
    )
    requested_payment_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Сума конкретного платіжного запиту; не замінює повну вартість замовлення.",
    )
    requested_payment_evidence_ids = models.JSONField(default=list, blank=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    currency = models.CharField(max_length=8, default="UAH")

    # Monobank acquiring
    invoice_id = models.CharField(max_length=128, blank=True, default="", db_index=True)
    invoice_url = models.CharField(max_length=600, blank=True, default="")
    # Инвойсы, которые мы перестали считать актуальными (смена товара или
    # типа оплаты). Ссылка при этом остаётся оплачиваемой на стороне
    # Monobank, поэтому платёж по ней обязан находить сделку (F-PAY-001).
    superseded_invoice_ids = models.JSONField(default=list, blank=True)
    # IMP-050: без срока жизни ссылки истечение ненаблюдаемо, и бот отвечал
    # «посилання ще активне», не проверив ничего. NULL означает «не знаем»
    # (ссылка выдана до появления поля), а не «истекла».
    invoice_expires_at = models.DateTimeField(null=True, blank=True, db_index=True)
    payment_status = models.CharField(max_length=20, default="unpaid")
    payment_payload = models.JSONField(default=dict, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    payment_truth = models.CharField(
        max_length=24,
        choices=PaymentTruth.choices,
        default=PaymentTruth.UNVERIFIED,
        db_index=True,
    )
    paid_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    refunded_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    payment_truth_updated_at = models.DateTimeField(null=True, blank=True, db_index=True)

    # Створене замовлення (після оплати)
    order = models.ForeignKey(
        "orders.Order",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="ig_deals",
    )
    active_checkout_proposal = models.OneToOneField(
        "management.IgCheckoutProposal",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="active_for_deal",
    )

    # Коли клієнта вже сповістили в Direct про відправку (ТТН) — щоб не дублювати.
    shipped_notified_at = models.DateTimeField(null=True, blank=True)
    order_truth_updated_at = models.DateTimeField(null=True, blank=True)

    # Display values are kept for conversation/context.  Fulfillment may only
    # use a validated signed-directory selection stored in the Ref fields.
    np_full_name = models.CharField(max_length=255, blank=True, default="")
    np_phone = models.CharField(max_length=50, blank=True, default="")
    np_city = models.CharField(max_length=160, blank=True, default="")
    np_office = models.CharField(max_length=255, blank=True, default="")
    np_settlement_ref = models.CharField(max_length=36, blank=True, default="")
    np_city_ref = models.CharField(max_length=36, blank=True, default="")
    np_warehouse_ref = models.CharField(max_length=36, blank=True, default="")
    np_warehouse_kind = models.CharField(max_length=16, blank=True, default="branch")
    delivery_status = models.CharField(
        max_length=16,
        choices=DeliveryStatus.choices,
        default=DeliveryStatus.UNVERIFIED,
        db_index=True,
    )
    delivery_source = models.CharField(max_length=32, blank=True, default="")
    delivery_error = models.CharField(max_length=500, blank=True, default="")
    delivery_verified_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("IG угода")
        verbose_name_plural = _("IG угоди")
        ordering = ["-id"]
        indexes = [
            models.Index(fields=["client", "-id"], name="ig_deal_client_dt"),
            models.Index(fields=["status", "-id"], name="ig_deal_status_dt"),
        ]

    def __str__(self) -> str:  # pragma: no cover - тривіально
        return f"IgDeal#{self.pk} {self.client_id} {self.status} {self.amount}{self.currency}"

    def recalc_total(self) -> Decimal:
        """Перераховує суму як суму позицій і зберігає."""
        from django.db.models import Sum

        total = self.items.aggregate(s=Sum("line_total"))["s"] or Decimal("0")
        self.amount = total
        self.save(update_fields=["amount", "updated_at"])
        return total

    def payable_amount(self) -> Decimal:
        """Exact amount requested by this invoice, separate from order total."""
        requested = Decimal(self.requested_payment_amount or 0)
        if self.pay_type == self.PayType.PREPAYMENT:
            return requested
        if self.pay_type == self.PayType.PREPAY_200:
            # Preserve already-created legacy deals without using 200 for new flows.
            return requested or Decimal("200.00")
        return requested or self.amount


class IgDealInvoiceLifecycle(models.Model):
    """Bounded provider lifecycle for each current or superseded invoice."""

    class Status(models.TextChoices):
        OPEN = "open", _("Активний моніторинг")
        PAID = "paid", _("Оплачено")
        FAILED = "failed", _("Помилка оплати")
        CANCELLED = "cancelled", _("Скасовано")
        EXPIRED = "expired", _("Протерміновано")
        UNKNOWN = "unknown", _("Термін моніторингу вичерпано")

    deal = models.ForeignKey(
        "management.IgDeal",
        on_delete=models.CASCADE,
        related_name="invoice_lifecycles",
        db_constraint=False,
    )
    invoice_id = models.CharField(max_length=128, unique=True, db_index=True)
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.OPEN,
        db_index=True,
    )
    provider_status = models.CharField(max_length=32, blank=True, default="")
    superseded_at = models.DateTimeField(null=True, blank=True, db_index=True)
    expires_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_polled_at = models.DateTimeField(null=True, blank=True)
    next_poll_at = models.DateTimeField(null=True, blank=True, db_index=True)
    terminal_at = models.DateTimeField(null=True, blank=True, db_index=True)
    poll_attempts = models.PositiveIntegerField(default=0)
    last_error = models.CharField(max_length=500, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at", "-id"]
        indexes = [
            models.Index(fields=["deal", "status"], name="ig_inv_life_deal_status"),
            models.Index(fields=["status", "next_poll_at"], name="ig_inv_life_poll_due"),
        ]

    @property
    def is_terminal(self):
        return self.status != self.Status.OPEN


class AppendOnlyPaymentEventQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValueError("IgPaymentEvent is append-only")

    def delete(self):
        raise ValueError("IgPaymentEvent is append-only")


class IgPaymentEvent(models.Model):
    """Append-only, idempotent provider evidence for payment truth changes."""

    event_key = models.CharField(max_length=64, unique=True)
    deal = models.ForeignKey(
        "management.IgDeal",
        on_delete=models.DO_NOTHING,
        related_name="payment_events",
        db_constraint=False,
    )
    client = models.ForeignKey(
        "management.IgClient",
        on_delete=models.DO_NOTHING,
        related_name="payment_events",
        db_constraint=False,
    )
    provider = models.CharField(max_length=32, default="monobank")
    source = models.CharField(max_length=32, default="provider")
    invoice_id = models.CharField(max_length=128, blank=True, default="", db_index=True)
    provider_status = models.CharField(max_length=32, db_index=True)
    provider_modified_at = models.DateTimeField(null=True, blank=True, db_index=True)
    gross_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    final_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    refunded_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    amount_valid = models.BooleanField(null=True, blank=True)
    currency = models.CharField(max_length=8, default="UAH")
    evidence = models.JSONField(default=dict, blank=True)
    payload_digest = models.CharField(max_length=64)
    received_at = models.DateTimeField(auto_now_add=True, db_index=True)

    objects = models.Manager.from_queryset(AppendOnlyPaymentEventQuerySet)()

    class Meta:
        ordering = ["-provider_modified_at", "-id"]
        indexes = [
            models.Index(fields=["deal", "-received_at"], name="ig_payevt_deal_dt"),
            models.Index(fields=["provider_status", "-received_at"], name="ig_payevt_status_dt"),
        ]

    def __str__(self):
        return f"IgPaymentEvent#{self.pk} deal={self.deal_id} {self.provider_status}"

    def save(self, *args, **kwargs):
        if self.pk and not kwargs.get("force_insert"):
            raise ValueError("IgPaymentEvent is append-only")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("IgPaymentEvent is append-only")


class IgPaymentProjection(models.Model):
    """Transactional current truth derived from append-only payment events."""

    deal = models.OneToOneField(
        "management.IgDeal",
        on_delete=models.DO_NOTHING,
        related_name="payment_projection",
        db_constraint=False,
    )
    client = models.ForeignKey(
        "management.IgClient",
        on_delete=models.DO_NOTHING,
        related_name="payment_projections",
        db_constraint=False,
    )
    truth = models.CharField(
        max_length=24,
        choices=IgDeal.PaymentTruth.choices,
        default=IgDeal.PaymentTruth.UNVERIFIED,
        db_index=True,
    )
    gross_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    refunded_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    paid_at = models.DateTimeField(null=True, blank=True, db_index=True)
    provider_modified_at = models.DateTimeField(null=True, blank=True, db_index=True)
    needs_reconciliation = models.BooleanField(default=False, db_index=True)
    reconciled_at = models.DateTimeField(null=True, blank=True)
    last_event = models.ForeignKey(
        "management.IgPaymentEvent",
        on_delete=models.PROTECT,
        related_name="projected_by",
        null=True,
        blank=True,
    )
    updated_at = models.DateTimeField(auto_now=True, db_index=True)

    class Meta:
        indexes = [
            models.Index(fields=["client", "-updated_at"], name="ig_payproj_client_dt"),
            models.Index(fields=["truth", "-updated_at"], name="ig_payproj_truth_dt"),
        ]

    @property
    def net_paid_amount(self):
        return max(Decimal("0"), self.gross_amount - self.refunded_amount)


class IgPaymentConfirmationReview(models.Model):
    """Audited manager decision on customer-provided payment evidence.

    This is deliberately separate from :class:`IgPaymentProjection`: a chat
    statement or receipt is not provider truth.  A confirmed review authorizes
    the manager to prepare an order, while the provider projection remains
    unchanged and visible as unverified until a real provider event arrives.
    """

    class Status(models.TextChoices):
        PENDING = "pending", _("Очікує підтвердження")
        CONFIRMED = "confirmed", _("Підтверджено менеджером")
        CANCELLED = "cancelled", _("Скасовано менеджером")
        SUPERSEDED = "superseded", _("Замінено канонічною перевіркою")

    class ResolutionKind(models.TextChoices):
        NONE = "", _("Не завершено окремим рішенням")
        HISTORICAL_PAID_ARCHIVED = (
            "historical_paid_archived",
            _("Старий оплачений продаж архівовано"),
        )

    class ResolutionOutcome(models.TextChoices):
        ALREADY_RECEIVED = "already_received", _("Старе замовлення отримано")
        ALREADY_DELIVERED = "already_delivered", _("Старе замовлення доставлено")
        COMPLETED_UNKNOWN = (
            "completed_unknown",
            _("Старе замовлення завершено; спосіб невідомий"),
        )

    client = models.ForeignKey(
        "management.IgClient",
        on_delete=models.CASCADE,
        related_name="payment_confirmation_reviews",
        db_constraint=False,
    )
    deal = models.ForeignKey(
        "management.IgDeal",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="payment_confirmation_reviews",
        db_constraint=False,
    )
    order = models.ForeignKey(
        "orders.Order",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="instagram_payment_reviews",
        db_constraint=False,
    )
    dedupe_key = models.CharField(max_length=160, unique=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING, db_index=True)
    evidence = models.JSONField(default=dict, blank=True)
    watermark_message_id = models.PositiveBigIntegerField(default=0)
    confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="confirmed_ig_payment_reviews",
        db_constraint=False,
    )
    confirmed_at = models.DateTimeField(null=True, blank=True)
    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="cancelled_ig_payment_reviews",
        db_constraint=False,
    )
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancellation_reason = models.CharField(max_length=500, blank=True, default="")
    superseded_by = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="superseded_reviews",
        db_constraint=False,
    )
    superseded_at = models.DateTimeField(null=True, blank=True)
    supersede_reason = models.CharField(max_length=120, blank=True, default="")
    resolution_kind = models.CharField(
        max_length=40,
        choices=ResolutionKind.choices,
        blank=True,
        default=ResolutionKind.NONE,
        db_index=True,
    )
    resolution_outcome = models.CharField(
        max_length=32,
        choices=ResolutionOutcome.choices,
        null=True,
        blank=True,
    )
    resolution_note = models.TextField(blank=True, default="")
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="resolved_ig_payment_reviews",
        db_constraint=False,
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Перевірка оплати Instagram")
        verbose_name_plural = _("Перевірки оплати Instagram")
        ordering = ["-id"]
        indexes = [
            models.Index(fields=["status", "-created_at"], name="ig_payreview_status_dt"),
            models.Index(fields=["client", "-id"], name="ig_payreview_client_id"),
            models.Index(fields=["superseded_by", "-id"], name="ig_payreview_superseded"),
        ]

    @property
    def manual_payment_truth(self) -> str:
        decision = self.decisions.order_by("-id").first()
        return decision.decision if decision else ""


class _AppendOnlyDecisionQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValueError("IgPaymentReviewDecision is append-only")

    def delete(self):
        raise ValueError("IgPaymentReviewDecision is append-only")


class _AppendOnlyDecisionManager(models.Manager.from_queryset(_AppendOnlyDecisionQuerySet)):
    pass


class IgPaymentReviewDecision(models.Model):
    """Append-only, source-qualified operator decision for payment evidence.

    This record is intentionally separate from ``IgPaymentProjection``. A
    manager can verify an IBAN receipt without pretending that a provider ledger
    webhook was received, and a later provider event can still disagree.
    """

    class Decision(models.TextChoices):
        MANAGER_VERIFIED = "manager_verified", _("Підтверджено менеджером")
        MANAGER_REJECTED = "manager_rejected", _("Відхилено менеджером")
        EVIDENCE_ACCEPTED_PROVIDER_UNVERIFIED = (
            "evidence_accepted_provider_unverified",
            _("Доказ прийнято, provider не підтверджено"),
        )

    class ActorSource(models.TextChoices):
        MANAGEMENT_USER = "management_user", _("Користувач management")
        TELEGRAM_USER = "telegram_user", _("Користувач Telegram")
        LEGACY_IMPORT = "legacy_import", _("Імпортовано з legacy review")

    class VerificationScope(models.TextChoices):
        FULL_PAYMENT = "full_payment", _("Повна оплата")
        PREPAYMENT = "prepayment", _("Передоплата")
        PAYMENT_CLAIM = "payment_claim", _("Заявлений платіж")
        HISTORICAL_FULFILLED = "historical_fulfilled", _("Історично виконане замовлення")

    review = models.ForeignKey(
        "management.IgPaymentConfirmationReview",
        # The customer row may be erased for GDPR. The append-only decision
        # remains as an anonymized audit tombstone with orphan-safe IDs.
        on_delete=models.DO_NOTHING,
        related_name="decisions",
        db_constraint=False,
    )
    client = models.ForeignKey(
        "management.IgClient",
        on_delete=models.DO_NOTHING,
        related_name="payment_review_decisions",
        db_constraint=False,
    )
    decision = models.CharField(max_length=48, choices=Decision.choices, db_index=True)
    verification_source = models.CharField(max_length=32, default="manager", db_index=True)
    verification_scope = models.CharField(max_length=32, choices=VerificationScope.choices)
    confirmed_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Точна сума, фактично перевірена менеджером; не повна вартість замовлення за замовчуванням.",
    )
    order_total_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Повна вартість замовлення, відновлена для історичного завершення.",
    )
    order_total_source = models.CharField(max_length=48, blank=True, default="")
    currency = models.CharField(max_length=8, default="UAH")
    amount_source = models.CharField(max_length=48, blank=True, default="")
    amount_evidence_message_ids = models.JSONField(default=list, blank=True)
    reason_code = models.CharField(max_length=64, blank=True, default="")
    reason_text = models.CharField(max_length=500, blank=True, default="")
    evidence_watermark_message_id = models.PositiveBigIntegerField(default=0)
    review_status_before = models.CharField(max_length=16, blank=True, default="")
    review_status_after = models.CharField(max_length=16, blank=True, default="")
    stage_before = models.CharField(max_length=32, blank=True, default="")
    stage_after = models.CharField(max_length=32, blank=True, default="")
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="ig_payment_review_decisions",
        db_constraint=False,
    )
    actor_source = models.CharField(max_length=32, choices=ActorSource.choices)
    actor_external_id = models.CharField(max_length=128)
    actor_label = models.CharField(max_length=150, blank=True, default="")
    telegram_decision = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    objects = _AppendOnlyDecisionManager()

    class Meta:
        verbose_name = _("Рішення щодо перевірки оплати Instagram")
        verbose_name_plural = _("Рішення щодо перевірок оплати Instagram")
        ordering = ["-id"]
        indexes = [
            models.Index(fields=["review", "-id"], name="ig_paydec_review_id"),
            models.Index(fields=["client", "-created_at"], name="ig_paydec_client_dt"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(actor_external_id=""),
                name="ig_paydec_actor_required",
            ),
            models.CheckConstraint(
                condition=(
                    ~models.Q(decision="manager_rejected")
                    | ~models.Q(reason_code="")
                ),
                name="ig_paydec_reject_reason",
            ),
        ]

    def save(self, *args, **kwargs):
        if self.pk and not kwargs.get("force_insert"):
            raise ValueError("IgPaymentReviewDecision is append-only")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("IgPaymentReviewDecision is append-only")


class IgDealItem(models.Model):
    """Позиція угоди. product/color_variant необов'язкові (позиція може бути
    поза каталогом, як кастом). line_total рахується автоматично."""

    deal = models.ForeignKey(IgDeal, on_delete=models.CASCADE, related_name="items")
    product = models.ForeignKey(
        "storefront.Product", null=True, blank=True, on_delete=models.SET_NULL
    )
    color_variant = models.ForeignKey(
        "productcolors.ProductColorVariant", null=True, blank=True, on_delete=models.SET_NULL
    )
    title = models.CharField(max_length=255)
    size = models.CharField(max_length=16, blank=True, default="")
    fit_option_code = models.CharField(max_length=50, blank=True, default="")
    fit_option_label = models.CharField(max_length=100, blank=True, default="")
    option_values = models.JSONField(default=dict, blank=True)
    option_labels = models.JSONField(default=dict, blank=True)
    qty = models.PositiveIntegerField(default=1)
    unit_price = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    line_total = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    price_source = models.CharField(max_length=64, blank=True, default="")
    price_evidence_message_ids = models.JSONField(default=list, blank=True)

    class Meta:
        verbose_name = _("Позиція IG угоди")
        verbose_name_plural = _("Позиції IG угод")
        ordering = ["id"]

    def save(self, *args, **kwargs):
        try:
            self.line_total = (self.unit_price or Decimal("0")) * int(self.qty or 0)
        except Exception:
            self.line_total = Decimal("0")
        super().save(*args, **kwargs)

    def __str__(self) -> str:  # pragma: no cover - тривіально
        return f"{self.title} ×{self.qty}"


class _AppendOnlyOrderAttributionQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValueError("IgOrderAttribution is append-only")

    def delete(self):
        raise ValueError("IgOrderAttribution is append-only")


class IgOrderAttribution(models.Model):
    """Immutable commercial attribution snapshot for one real order.

    Direct Instagram profile values are intentionally excluded.  The digest is
    only for deterministic audit correlation and cannot be used to recover the
    profile without the application secret.
    """

    CREATION_MODES = (
        ("provider_auto", _("Автоматично за provider payment")),
        ("manager_review", _("Створено після перевірки менеджером")),
        ("linked_existing", _("Прив'язано до існуючого замовлення")),
    )
    PAYMENT_SOURCES = (
        ("provider_projection", _("Provider projection")),
        ("provider_attempt", _("Provider payment attempt")),
        ("manager_verified", _("Перевірено менеджером")),
        ("unknown", _("Невідоме джерело")),
    )

    order = models.OneToOneField(
        "orders.Order",
        on_delete=models.DO_NOTHING,
        db_constraint=False,
        related_name="instagram_attribution",
    )
    client = models.ForeignKey(
        "management.IgClient",
        on_delete=models.DO_NOTHING,
        db_constraint=False,
        related_name="order_attributions",
    )
    deal = models.ForeignKey(
        "management.IgDeal",
        null=True,
        blank=True,
        on_delete=models.DO_NOTHING,
        related_name="order_attributions",
        db_constraint=False,
    )
    payment_review = models.ForeignKey(
        "management.IgPaymentConfirmationReview",
        null=True,
        blank=True,
        on_delete=models.DO_NOTHING,
        related_name="order_attributions",
        db_constraint=False,
    )
    manager_decision = models.ForeignKey(
        "management.IgPaymentReviewDecision",
        null=True,
        blank=True,
        on_delete=models.DO_NOTHING,
        related_name="order_attributions",
        db_constraint=False,
    )
    creation_mode = models.CharField(max_length=32, choices=CREATION_MODES)
    payment_source = models.CharField(max_length=32, choices=PAYMENT_SOURCES, default="unknown")
    identity_digest = models.CharField(max_length=64, blank=True, default="", db_index=True)
    evidence_watermark_message_id = models.PositiveBigIntegerField(default=0)
    item_provenance = models.JSONField(default=list, blank=True)
    negotiated_total = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    price_source = models.CharField(max_length=64, blank=True, default="")
    price_evidence_message_ids = models.JSONField(default=list, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.DO_NOTHING,
        related_name="ig_order_attributions_created",
        db_constraint=False,
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = _("Атрибуція Instagram-замовлення")
        verbose_name_plural = _("Атрибуції Instagram-замовлень")
        indexes = [
            models.Index(fields=["client", "-created_at"], name="ig_order_attr_client_dt"),
            models.Index(fields=["creation_mode", "-created_at"], name="ig_order_attr_mode_dt"),
        ]

    objects = models.Manager.from_queryset(_AppendOnlyOrderAttributionQuerySet)()

    def save(self, *args, **kwargs):
        if self.pk and not kwargs.get("force_insert"):
            raise ValueError("IgOrderAttribution is append-only")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("IgOrderAttribution is append-only")


class _AppendOnlyOrderLinkEventQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValueError("IgOrderLinkEvent is append-only")

    def delete(self):
        raise ValueError("IgOrderLinkEvent is append-only")


class IgOrderLinkEvent(models.Model):
    """Auditable edge between a payment review episode and an order."""

    order = models.ForeignKey(
        "orders.Order", on_delete=models.DO_NOTHING, db_constraint=False,
        related_name="instagram_link_events"
    )
    client = models.ForeignKey(
        "management.IgClient", on_delete=models.DO_NOTHING, db_constraint=False,
        related_name="order_link_events",
    )
    review = models.ForeignKey(
        "management.IgPaymentConfirmationReview", null=True, blank=True,
        on_delete=models.DO_NOTHING, related_name="order_link_events", db_constraint=False,
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.DO_NOTHING,
        related_name="ig_order_link_events", db_constraint=False,
    )
    event_kind = models.CharField(max_length=32, default="linked")
    reason_code = models.CharField(max_length=64, blank=True, default="")
    mismatch_snapshot = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["order", "review", "event_kind"],
                name="ig_order_link_event_once",
            )
        ]
        indexes = [models.Index(fields=["client", "-created_at"], name="ig_order_link_client_dt")]

    objects = models.Manager.from_queryset(_AppendOnlyOrderLinkEventQuerySet)()

    def save(self, *args, **kwargs):
        if self.pk and not kwargs.get("force_insert"):
            raise ValueError("IgOrderLinkEvent is append-only")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("IgOrderLinkEvent is append-only")


_ig_order_assignment_mutation_allowed = ContextVar(
    "ig_order_assignment_mutation_allowed",
    default=False,
)


@contextmanager
def _ig_order_assignment_mutation_scope():
    token = _ig_order_assignment_mutation_allowed.set(True)
    try:
        yield
    finally:
        _ig_order_assignment_mutation_allowed.reset(token)


def _require_ig_order_assignment_mutation_scope():
    if not _ig_order_assignment_mutation_allowed.get():
        raise ValueError(
            "IgOrderAssignment is managed by the assignment service"
        )


class _IgOrderAssignmentQuerySet(models.QuerySet):
    def update(self, **kwargs):
        _require_ig_order_assignment_mutation_scope()
        return super().update(**kwargs)

    def bulk_create(self, objs, batch_size=None, ignore_conflicts=False, update_conflicts=False, update_fields=None, unique_fields=None):
        _require_ig_order_assignment_mutation_scope()
        return super().bulk_create(
            objs,
            batch_size=batch_size,
            ignore_conflicts=ignore_conflicts,
            update_conflicts=update_conflicts,
            update_fields=update_fields,
            unique_fields=unique_fields,
        )

    def bulk_update(self, objs, fields, batch_size=None):
        _require_ig_order_assignment_mutation_scope()
        return super().bulk_update(objs, fields, batch_size=batch_size)

    def delete(self):
        raise ValueError("IgOrderAssignment is managed by the assignment service")

    def _raw_delete(self, using):
        raise ValueError("IgOrderAssignment is managed by the assignment service")


class IgOrderAssignment(models.Model):
    """Current operational owner of an order in the Instagram workspace."""

    class Source(models.TextChoices):
        PROVIDER_AUTO = "provider_auto", _("Автоматично за оплатою")
        CHECKOUT_AUTO = "checkout_auto", _("Автоматично через Direct checkout")
        MANAGER_PAYMENT_REVIEW = "manager_payment_review", _("Менеджер після перевірки оплати")
        MANAGER_MANUAL = "manager_manual", _("Менеджер вручну")
        MANAGER_CREATED = "manager_created", _("Створено менеджером")
        LEGACY_ATTRIBUTION = "legacy_attribution", _("Імпортовано з атрибуції")

    order = models.OneToOneField(
        "orders.Order",
        on_delete=models.DO_NOTHING,
        db_constraint=False,
        related_name="instagram_assignment",
    )
    client = models.ForeignKey(
        "management.IgClient",
        null=True,
        blank=True,
        on_delete=models.DO_NOTHING,
        db_constraint=False,
        related_name="order_assignments",
    )
    source = models.CharField(max_length=32, choices=Source.choices, db_index=True)
    assigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.DO_NOTHING,
        db_constraint=False,
        related_name="ig_order_assignments",
    )
    assigned_at = models.DateTimeField(default=timezone.now, db_index=True)
    unassigned_at = models.DateTimeField(null=True, blank=True)
    version = models.PositiveIntegerField(default=1)
    last_reason_code = models.CharField(max_length=64, blank=True, default="")
    last_reason = models.CharField(max_length=500, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-assigned_at", "-id"]
        indexes = [
            models.Index(fields=["client", "-assigned_at"], name="ig_assign_client_dt"),
            models.Index(fields=["source", "-assigned_at"], name="ig_assign_source_dt"),
        ]

    objects = models.Manager.from_queryset(_IgOrderAssignmentQuerySet)()

    def save(self, *args, **kwargs):
        _require_ig_order_assignment_mutation_scope()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("IgOrderAssignment is managed by the assignment service")


class _AppendOnlyOrderAssignmentEventQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValueError("IgOrderAssignmentEvent is append-only")

    def bulk_update(self, objs, fields, batch_size=None):
        raise ValueError("IgOrderAssignmentEvent is append-only")

    def delete(self):
        raise ValueError("IgOrderAssignmentEvent is append-only")

    def _raw_delete(self, using):
        raise ValueError("IgOrderAssignmentEvent is append-only")


class IgOrderAssignmentEvent(models.Model):
    """Immutable audit trail for manager and automatic assignment changes."""

    class Kind(models.TextChoices):
        LINKED = "linked", _("Прив'язано")
        UNLINKED = "unlinked", _("Відв'язано")
        AUTO_CONFIRMED = "auto_confirmed", _("Автоматичну прив'язку підтверджено")

    class ActorSource(models.TextChoices):
        MANAGEMENT_USER = "management_user", _("Користувач management")
        AUTOMATION = "automation", _("Автоматизація")
        MIGRATION = "migration", _("Міграція")

    operation_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    assignment = models.ForeignKey(
        "management.IgOrderAssignment",
        on_delete=models.DO_NOTHING,
        db_constraint=False,
        related_name="events",
    )
    order = models.ForeignKey(
        "orders.Order",
        on_delete=models.DO_NOTHING,
        db_constraint=False,
        related_name="instagram_assignment_events",
    )
    kind = models.CharField(max_length=24, choices=Kind.choices, db_index=True)
    from_client = models.ForeignKey(
        "management.IgClient",
        null=True,
        blank=True,
        on_delete=models.DO_NOTHING,
        db_constraint=False,
        related_name="order_assignment_events_from",
    )
    to_client = models.ForeignKey(
        "management.IgClient",
        null=True,
        blank=True,
        on_delete=models.DO_NOTHING,
        db_constraint=False,
        related_name="order_assignment_events_to",
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.DO_NOTHING,
        db_constraint=False,
        related_name="ig_order_assignment_events",
    )
    actor_source = models.CharField(max_length=24, choices=ActorSource.choices)
    assignment_source = models.CharField(max_length=32, choices=IgOrderAssignment.Source.choices)
    reason_code = models.CharField(max_length=64, blank=True, default="")
    reason = models.CharField(max_length=500, blank=True, default="")
    assignment_version = models.PositiveIntegerField()
    snapshot = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    objects = models.Manager.from_queryset(_AppendOnlyOrderAssignmentEventQuerySet)()

    class Meta:
        ordering = ["-id"]
        indexes = [
            models.Index(fields=["assignment", "-created_at"], name="ig_assign_evt_dt"),
            models.Index(fields=["order", "-created_at"], name="ig_assign_order_evt"),
        ]

    def save(self, *args, **kwargs):
        if self.pk and not kwargs.get("force_insert"):
            raise ValueError("IgOrderAssignmentEvent is append-only")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("IgOrderAssignmentEvent is append-only")


class IgUgcReward(models.Model):
    """One lifetime UGC grant for an Instagram identity.

    ``delivered_order`` keeps the historical order/assignment requirements;
    ``external_ugc`` deliberately leaves them empty because a qualifying
    provider-native mention may come from the public site, a store, a friend,
    or another sales channel.
    """

    class EvidenceType(models.TextChoices):
        DIRECT_MESSAGE = "direct_message", _("Повідомлення Direct")
        INSTAGRAM_URL = "instagram_url", _("Посилання Instagram")
        STORY_MENTION = "story_mention", _("Відмітка в story")

    class LifecycleState(models.TextChoices):
        ACTIVE = "active", _("Активна")
        HELD = "held", _("Тимчасово призупинена")
        REVOKED = "revoked", _("Відкликана")

    client = models.ForeignKey(
        "management.IgClient",
        on_delete=models.SET_NULL,
        related_name="ugc_rewards",
        null=True,
        blank=True,
        db_constraint=False,
    )
    order = models.OneToOneField(
        "orders.Order",
        on_delete=models.PROTECT,
        related_name="instagram_ugc_reward",
        null=True,
        blank=True,
        # Production still contains legacy tables with mixed storage engines;
        # retain this relationship at the ORM/service boundary only.
        db_constraint=False,
    )
    assignment = models.ForeignKey(
        "management.IgOrderAssignment",
        on_delete=models.PROTECT,
        related_name="ugc_rewards",
        null=True,
        blank=True,
        db_constraint=False,
    )
    assignment_version = models.PositiveIntegerField(default=0)
    evidence_type = models.CharField(max_length=24, choices=EvidenceType.choices)
    evidence_message = models.ForeignKey(
        "management.InstagramBotMessage",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="ugc_rewards",
        db_constraint=False,
    )
    evidence_url = models.URLField(max_length=500, blank=True, default="")
    evidence_fingerprint = models.CharField(max_length=64, unique=True)
    review_note = models.CharField(max_length=1000, blank=True, default="")
    promo_code = models.OneToOneField(
        "storefront.PromoCode",
        on_delete=models.PROTECT,
        related_name="instagram_ugc_reward",
        db_constraint=False,
    )
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="reviewed_ig_ugc_rewards",
        null=True,
        blank=True,
        # `auth_user` на проді — MyISAM, як і ще 204 таблиці цієї бази: рушій
        # InnoDB отримують лише нові таблиці. FK на MyISAM неможливий, і
        # міграція падала з errno 150 «Foreign key constraint is incorrectly
        # formed». Обмеження лишається на рівні ORM, як це вже зроблено для
        # `RestockSubscription.user` — тобто це прийнятий у проєкті спосіб, а не
        # виняток заради деплою.
        db_constraint=False,
    )
    reward_path = models.CharField(max_length=24, default="delivered_order", db_index=True)
    decision_source = models.CharField(max_length=16, default="manager")
    assessment = models.ForeignKey(
        "management.IgUgcEvidenceAssessment",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="rewards",
        db_constraint=False,
    )
    assessment_generation_snapshot = models.PositiveBigIntegerField(default=0)
    discount_percent = models.PositiveSmallIntegerField(default=10)
    policy_version_snapshot = models.CharField(max_length=32, blank=True, default="")
    provider_object_digest_snapshot = models.CharField(max_length=64, blank=True, default="")
    catalog_candidates_snapshot = models.JSONField(default=list, blank=True)
    lifecycle_state = models.CharField(
        max_length=16,
        choices=LifecycleState.choices,
        default=LifecycleState.ACTIVE,
        db_index=True,
    )
    lifecycle_reason = models.CharField(max_length=64, blank=True, default="")
    lifecycle_updated_at = models.DateTimeField(default=timezone.now, db_index=True)
    issued_at = models.DateTimeField(default=timezone.now, db_index=True)
    lifetime_slot_key = models.CharField(max_length=128, unique=True, null=True, blank=True)
    reviewed_at = models.DateTimeField(default=timezone.now, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-reviewed_at", "-id"]
        indexes = [
            models.Index(fields=["client", "-reviewed_at"], name="ig_ugc_client_dt"),
            models.Index(fields=["assignment", "assignment_version"], name="ig_ugc_assign_ver"),
            models.Index(fields=["reward_path", "-issued_at"], name="ig_ugc_path_issued"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(
                        reward_path="external_ugc",
                        order__isnull=True,
                        assignment__isnull=True,
                    )
                    | models.Q(
                        reward_path="delivered_order",
                        order__isnull=False,
                        assignment__isnull=False,
                    )
                ),
                name="ig_ugc_reward_path_refs",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(decision_source="auto", reviewed_by__isnull=True)
                    | models.Q(decision_source="manager", reviewed_by__isnull=False)
                ),
                name="ig_ugc_reward_source_reviewer",
            ),
        ]


class IgUgcEvidenceAssessment(models.Model):
    """Durable, provenance-bound UGC assessment with deterministic policy gates."""

    class Decision(models.TextChoices):
        PENDING = "pending", _("Очікує оцінки")
        QUALIFIED_AUTO = "qualified_auto", _("Автоматично підтверджено")
        NEEDS_MANAGER_REVIEW = "needs_manager_review", _("Потрібен менеджер")
        MANAGER_APPROVED = "manager_approved", _("Підтверджено менеджером")
        REJECTED = "rejected", _("Відхилено")

    client = models.ForeignKey(
        "management.IgClient",
        on_delete=models.SET_NULL,
        related_name="ugc_assessments",
        db_constraint=False,
        null=True,
        blank=True,
    )
    source_message_id = models.CharField(max_length=255, db_index=True)
    provider_object_key = models.CharField(max_length=255, blank=True, default="", db_index=True)
    provider_object_digest = models.CharField(
        max_length=64,
        null=True,
        blank=True,
        unique=True,
    )
    provider_media_id = models.CharField(max_length=255, blank=True, default="")
    provider_event_id = models.CharField(max_length=255, blank=True, default="")
    target_username = models.CharField(max_length=80, blank=True, default="")
    evidence_fingerprint = models.CharField(max_length=128, db_index=True)
    perceptual_fingerprint = models.CharField(max_length=128, blank=True, default="", db_index=True)
    decision = models.CharField(max_length=24, choices=Decision.choices, default=Decision.PENDING, db_index=True)
    decision_source = models.CharField(max_length=16, default="policy")
    policy_version = models.CharField(max_length=32, default="ugc-v1")
    reason_codes = models.JSONField(default=list, blank=True)
    catalog_candidates = models.JSONField(default=list, blank=True)
    confidence = models.DecimalField(max_digits=5, decimal_places=4, default=0)
    people_count = models.PositiveSmallIntegerField(default=0)
    garment_count = models.PositiveSmallIntegerField(default=0)
    reward_owner_client_id = models.PositiveBigIntegerField(null=True, blank=True)
    generation = models.PositiveBigIntegerField(default=1)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="reviewed_ig_ugc_assessments",
        db_constraint=False,
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    lease_token = models.CharField(max_length=64, blank=True, default="")
    lease_expires_at = models.DateTimeField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["client", "source_message_id"],
                name="ig_ugc_assess_source_once",
            ),
        ]
        indexes = [
            models.Index(fields=["client", "-created_at"], name="ig_ugc_assess_client_dt"),
            models.Index(fields=["decision", "-created_at"], name="ig_ugc_assess_decision_dt"),
            models.Index(fields=["provider_object_key", "source_message_id"], name="ig_ugc_assess_provider"),
        ]


class IgUgcRewardLifetime(models.Model):
    """InnoDB identity slot that serializes all UGC reward paths per client."""

    client = models.OneToOneField(
        "management.IgClient",
        on_delete=models.SET_NULL,
        related_name="ugc_reward_lifetime",
        db_constraint=False,
        null=True,
        blank=True,
    )
    identity_digest = models.CharField(max_length=128, unique=True)
    reward = models.OneToOneField(
        "management.IgUgcReward",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="lifetime_slot",
        db_constraint=False,
    )
    # Privacy deletion may remove the reward/promo payload while the
    # irreversible lifetime grant remains represented by this digest.
    consumed_at = models.DateTimeField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=["client", "reward"], name="ig_ugc_life_client_reward")]


class IgUgcRewardDelivery(models.Model):
    """Receipt-backed outbox for the private UGC code message."""

    class State(models.TextChoices):
        PENDING = "pending", "Pending"
        PROCESSING = "processing", "Processing"
        SENT = "sent", "Sent"
        WAITING_WINDOW = "waiting_window", "Waiting window"
        AMBIGUOUS = "ambiguous", "Ambiguous"
        FAILED = "failed", "Failed"

    reward = models.OneToOneField(
        "management.IgUgcReward",
        on_delete=models.PROTECT,
        related_name="delivery",
        db_constraint=False,
    )
    client = models.ForeignKey(
        "management.IgClient",
        on_delete=models.SET_NULL,
        related_name="ugc_reward_deliveries",
        db_constraint=False,
        null=True,
        blank=True,
    )
    message_snapshot = models.TextField()
    state = models.CharField(max_length=20, choices=State.choices, default=State.PENDING, db_index=True)
    due_at = models.DateTimeField(default=timezone.now, db_index=True)
    attempts = models.PositiveIntegerField(default=0)
    lease_token = models.CharField(max_length=64, blank=True, default="")
    lease_expires_at = models.DateTimeField(null=True, blank=True, db_index=True)
    provider_message_ids = models.JSONField(default=list, blank=True)
    last_error = models.CharField(max_length=500, blank=True, default="")
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["due_at", "id"]
        indexes = [
            models.Index(fields=["state", "due_at", "id"], name="ig_ugc_delivery_due"),
            models.Index(fields=["client", "-created_at"], name="ig_ugc_delivery_client"),
        ]


class IgUgcRewardLifecycleJob(models.Model):
    """Event-scoped retry queue for linked-order reward truth changes."""

    order_id = models.PositiveBigIntegerField(null=True, blank=True, db_index=True)
    client_id = models.PositiveBigIntegerField(null=True, blank=True, db_index=True)
    source = models.CharField(max_length=32, blank=True, default="")
    due_at = models.DateTimeField(default=timezone.now, db_index=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    last_error_kind = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["due_at", "id"]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(order_id__isnull=False)
                    | models.Q(client_id__isnull=False)
                ),
                name="ig_ugc_life_job_target",
            ),
        ]
        indexes = [
            models.Index(
                fields=["due_at", "id"],
                name="ig_ugc_life_job_due",
            ),
        ]


class _IgOrderCustomerEventQuerySet(models.QuerySet):
    _IDENTITY_FIELDS = {
        "event_key",
        "assignment",
        "assignment_id",
        "assignment_version",
        "order",
        "order_id",
        "client",
        "client_id",
        "kind",
        "locale",
        "message_snapshot",
        "payload",
    }

    def update(self, **kwargs):
        if self._IDENTITY_FIELDS.intersection(kwargs):
            raise ValueError("IgOrderCustomerEvent identity is immutable")
        return super().update(**kwargs)

    def bulk_update(self, objs, fields, batch_size=None):
        if self._IDENTITY_FIELDS.intersection(fields):
            raise ValueError("IgOrderCustomerEvent identity is immutable")
        return super().bulk_update(objs, fields, batch_size=batch_size)

    def delete(self):
        raise ValueError("IgOrderCustomerEvent is durable")

    def _raw_delete(self, using):
        raise ValueError("IgOrderCustomerEvent is durable")


class IgOrderCustomerEvent(models.Model):
    """Durable localized customer message derived from current order ownership."""

    class Kind(models.TextChoices):
        TTN_ASSIGNED = "ttn_assigned", _("ТТН прив'язано")
        # An exchange replacement is not a second shipment of the order. The
        # generic text «Ваше замовлення відправлено» reads as a repeat and hides
        # the fact that this is the size the customer asked for.
        EXCHANGE_SHIPPED = "exchange_shipped", _("Заміну відправлено")
        # F-PAY-007: подтверждение оплаты клиенту не было детерминированным —
        # оно зависело от того, сгенерирует ли модель нужную фразу.
        PAYMENT_CONFIRMED = "payment_confirmed", _("Оплату підтверджено")
        DELIVERED_REVIEW = "delivered_review", _("Запит відгуку після отримання")

    class State(models.TextChoices):
        PENDING = "pending", _("Очікує")
        PROCESSING = "processing", _("Обробляється")
        SENT = "sent", _("Надіслано")
        WAITING_WINDOW = "waiting_window", _("Очікує вікна відповіді")
        MANAGER_REVIEW = "manager_review", _("Потрібен менеджер")
        AMBIGUOUS = "ambiguous", _("Невідомий результат")
        FAILED = "failed", _("Помилка")
        CANCELLED = "cancelled", _("Скасовано")

    event_key = models.CharField(max_length=180, unique=True)
    assignment = models.ForeignKey(
        "management.IgOrderAssignment",
        on_delete=models.DO_NOTHING,
        db_constraint=False,
        related_name="customer_events",
    )
    assignment_version = models.PositiveIntegerField()
    order = models.ForeignKey(
        "orders.Order",
        on_delete=models.DO_NOTHING,
        db_constraint=False,
        related_name="instagram_customer_events",
    )
    client = models.ForeignKey(
        "management.IgClient",
        on_delete=models.DO_NOTHING,
        db_constraint=False,
        related_name="order_customer_events",
    )
    kind = models.CharField(max_length=32, choices=Kind.choices, db_index=True)
    locale = models.CharField(max_length=12, default="uk")
    message_snapshot = models.TextField()
    payload = models.JSONField(default=dict, blank=True)
    state = models.CharField(max_length=24, choices=State.choices, default=State.PENDING, db_index=True)
    due_at = models.DateTimeField(default=timezone.now, db_index=True)
    attempts = models.PositiveIntegerField(default=0)
    lease_token = models.CharField(max_length=64, blank=True, default="")
    lease_expires_at = models.DateTimeField(null=True, blank=True, db_index=True)
    provider_message_id = models.CharField(max_length=255, blank=True, default="")
    delivery_provider_message_ids = models.JSONField(default=list, blank=True)
    last_error = models.CharField(max_length=1000, blank=True, default="")
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["due_at", "id"]
        indexes = [
            models.Index(fields=["state", "due_at", "id"], name="ig_order_evt_state_due"),
            models.Index(fields=["client", "-created_at"], name="ig_order_evt_client_dt"),
            models.Index(fields=["order", "kind"], name="ig_order_evt_kind"),
        ]

    objects = models.Manager.from_queryset(_IgOrderCustomerEventQuerySet)()

    _IDENTITY_FIELDS = (
        "event_key",
        "kind",
        "assignment_id",
        "assignment_version",
        "order_id",
        "client_id",
        "locale",
        "message_snapshot",
        "payload",
    )

    def save(self, *args, **kwargs):
        if self.pk:
            previous = type(self).objects.filter(pk=self.pk).values(*self._IDENTITY_FIELDS).first()
            if previous:
                for field_name in self._IDENTITY_FIELDS:
                    if getattr(self, field_name) != previous[field_name]:
                        raise ValueError("IgOrderCustomerEvent identity is immutable")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("IgOrderCustomerEvent is durable")


class IgCommercialEpisode(models.Model):
    """Durable Instagram purchase journey with one intended physical order."""

    class State(models.TextChoices):
        ACTIVE = "active", _("Активний цикл")
        ORDER_CREATED = "order_created", _("Замовлення створено")
        FULFILLED = "fulfilled", _("Замовлення виконано")
        CANCELLED = "cancelled", _("Скасовано")
        LOST = "lost", _("Втрачено")

    class RepeatKind(models.TextChoices):
        FIRST_PURCHASE = "first_purchase", _("Перша покупка")
        EXPLICIT_MORE = "explicit_more", _("Хоче ще")
        REORDER = "reorder", _("Повторне замовлення")
        GIFT = "gift", _("Подарунок")
        ANOTHER_RECIPIENT = "another_recipient", _("Для іншого отримувача")

    client = models.ForeignKey(
        "management.IgClient",
        on_delete=models.DO_NOTHING,
        related_name="commercial_episodes",
        db_constraint=False,
    )
    sequence = models.PositiveIntegerField()
    # MariaDB permits multiple NULL values but only one value=1 per client.
    # This gives a database-backed single-current-episode guard.
    open_slot = models.PositiveSmallIntegerField(null=True, blank=True, default=1)
    materialization_key = models.CharField(max_length=96, unique=True)
    state = models.CharField(
        max_length=24,
        choices=State.choices,
        default=State.ACTIVE,
        db_index=True,
    )
    repeat_kind = models.CharField(
        max_length=32,
        choices=RepeatKind.choices,
        default=RepeatKind.FIRST_PURCHASE,
        db_index=True,
    )
    deal = models.OneToOneField(
        "management.IgDeal",
        null=True,
        blank=True,
        on_delete=models.DO_NOTHING,
        related_name="commercial_episode",
        db_constraint=False,
    )
    primary_payment_review = models.OneToOneField(
        "management.IgPaymentConfirmationReview",
        null=True,
        blank=True,
        on_delete=models.DO_NOTHING,
        related_name="commercial_episode",
        db_constraint=False,
    )
    order_attribution = models.OneToOneField(
        "management.IgOrderAttribution",
        null=True,
        blank=True,
        on_delete=models.DO_NOTHING,
        related_name="commercial_episode",
        db_constraint=False,
    )
    intended_order = models.OneToOneField(
        "orders.Order",
        null=True,
        blank=True,
        on_delete=models.DO_NOTHING,
        related_name="instagram_commercial_episode",
        db_constraint=False,
    )
    stage_snapshot = models.JSONField(default=dict, blank=True)
    product_snapshot = models.JSONField(default=list, blank=True)
    price_snapshot = models.JSONField(default=dict, blank=True)
    payment_snapshot = models.JSONField(default=dict, blank=True)
    fulfillment_snapshot = models.JSONField(default=dict, blank=True)
    outcome = models.CharField(max_length=64, blank=True, default="")
    repeat_evidence_message_ids = models.JSONField(default=list, blank=True)
    repeat_confidence = models.DecimalField(max_digits=5, decimal_places=4, default=0)
    analysis_model = models.CharField(max_length=80, blank=True, default="")
    analysis_prompt_version = models.CharField(max_length=80, blank=True, default="")
    opened_watermark_message_id = models.BigIntegerField(default=0, db_index=True)
    shipment_notified_at = models.DateTimeField(null=True, blank=True)
    opened_at = models.DateTimeField(auto_now_add=True, db_index=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Комерційний епізод Instagram")
        verbose_name_plural = _("Комерційні епізоди Instagram")
        ordering = ["-sequence", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["client", "sequence"],
                name="ig_episode_client_sequence_once",
            ),
            models.UniqueConstraint(
                fields=["client", "open_slot"],
                name="ig_episode_one_open_slot",
            ),
            models.CheckConstraint(
                condition=models.Q(open_slot__isnull=True) | models.Q(open_slot=1),
                name="ig_episode_open_slot_null_or_one",
            ),
        ]
        indexes = [
            models.Index(fields=["client", "-opened_at"], name="ig_episode_client_opened"),
            models.Index(fields=["state", "-updated_at"], name="ig_episode_state_updated"),
        ]

    def __str__(self):  # pragma: no cover - trivial representation
        return f"IgCommercialEpisode#{self.pk} client={self.client_id} seq={self.sequence}"

    @property
    def evidence_message_ids(self):
        """Stable public alias for the bounded episode API/test contract."""
        return list(self.repeat_evidence_message_ids or [])


def default_checkout_proposal_expiry():
    return timezone.now() + timedelta(minutes=25)


class IgCheckoutProposalQuerySet(models.QuerySet):
    _PROTECTED_UPDATE_FIELDS = {
        "client",
        "client_id",
        "deal",
        "deal_id",
        "commercial_episode",
        "commercial_episode_id",
        "status",
        "revision",
        "catalog_total",
        "negotiated_discount",
        "quoted_total",
        "requested_payment_amount",
        "currency",
        "pay_type",
        "allow_promo",
        "items_digest",
        "expires_at",
        "details_locked_at",
        "invoice_cancelled_at",
        "provider_cancellation_event",
        "provider_cancellation_event_id",
        "paid_at",
        "payment_attempt",
        "payment_attempt_id",
        "assisted_checkout_v2",
        "current_invoice_generation",
        "current_invoice_generation_id",
        "winner_invoice_generation",
        "winner_invoice_generation_id",
        "payment_policy",
        "payment_policy_evidence_message",
        "payment_policy_evidence_message_id",
        "payment_policy_evidence_kind",
        "payment_policy_evidence_revision",
        "payment_policy_evidence_digest",
        "custom_print_full_only",
        "superseded_by",
        "superseded_by_id",
    }

    def update(self, **kwargs):
        if self._PROTECTED_UPDATE_FIELDS.intersection(kwargs):
            raise ValueError(
                "Instagram checkout proposal state requires a locked transition service"
            )
        return super().update(**kwargs)

    def delete(self):
        raise ValueError("IgCheckoutProposal financial evidence cannot be deleted")


class IgCheckoutProposalManager(models.Manager.from_queryset(IgCheckoutProposalQuerySet)):
    def _proposal_values(self, *, deal, **values):
        from management.services.ig_commercial_episodes import ensure_episode_for_deal

        episode = ensure_episode_for_deal(deal)
        values.setdefault("client", deal.client)
        values.setdefault("commercial_episode", episode)
        values.setdefault("expires_at", default_checkout_proposal_expiry())
        values.setdefault("catalog_total", values.get("quoted_total"))
        values.setdefault(
            "requested_payment_amount",
            values.get("quoted_total"),
        )
        values.setdefault("negotiated_discount", Decimal("0.00"))
        values.setdefault("revision", 1)
        return values

    @transaction.atomic
    def create_current(self, *, deal, **values):
        deal = (
            IgDeal.objects.select_for_update()
            .select_related("client", "active_checkout_proposal")
            .get(pk=deal.pk)
        )
        if deal.active_checkout_proposal_id:
            raise ValidationError("Deal already has a current proposal")
        values = self._proposal_values(deal=deal, **values)
        proposal = self.model(deal=deal, **values)
        proposal.full_clean()
        proposal.save(force_insert=True)
        deal.active_checkout_proposal = proposal
        deal.save(update_fields=["active_checkout_proposal", "updated_at"])
        return proposal

    @transaction.atomic
    def replace_current(self, *, deal, **values):
        deal = (
            IgDeal.objects.select_for_update()
            .select_related("client", "active_checkout_proposal")
            .get(pk=deal.pk)
        )
        current = deal.active_checkout_proposal
        if current is None:
            raise ValidationError("Deal has no current proposal to replace")
        current = self.select_for_update().get(pk=current.pk)
        if current.status == current.Status.PAID:
            raise ValidationError("A paid proposal cannot be superseded")
        if current.status == current.Status.INVOICE_CREATED:
            raise ValidationError("A payable or ambiguous invoice cannot be replaced")
        attempt = None
        event = None
        projection = None
        if current.payment_attempt_id:
            from orders.models import PaymentAttempt

            attempt = PaymentAttempt.objects.select_for_update().get(
                pk=current.payment_attempt_id
            )
        if current.provider_cancellation_event_id:
            event = IgPaymentEvent.objects.select_for_update().get(
                pk=current.provider_cancellation_event_id
            )
            projection = (
                IgPaymentProjection.objects.select_for_update()
                .filter(deal_id=deal.pk)
                .first()
            )
        if not current.has_provider_confirmed_cancellation(
            attempt=attempt,
            event=event,
            projection=projection,
        ):
            raise ValidationError("The current proposal is not provider-confirmed cancelled")

        values = self._proposal_values(deal=deal, **values)
        replacement = self.model(deal=deal, **values)
        replacement.full_clean()
        replacement.save(force_insert=True)

        current.status = current.Status.SUPERSEDED
        current.superseded_by = replacement
        current.save(update_fields=["status", "superseded_by", "updated_at"])
        deal.active_checkout_proposal = replacement
        deal.save(update_fields=["active_checkout_proposal", "updated_at"])
        return replacement


class IgCheckoutProposal(models.Model):
    """Frozen first-party checkout offer created from an Instagram deal."""

    class Status(models.TextChoices):
        READY = "ready", _("Готова")
        VIEWED = "viewed", _("Переглянута")
        DETAILS_LOCKED = "details_locked", _("Дані зафіксовані")
        INVOICE_CREATED = "invoice_created", _("Рахунок створено")
        MANAGER_REVIEW = "manager_review", _("Потрібна перевірка менеджера")
        PAID = "paid", _("Оплачено")
        CANCELLED = "cancelled", _("Рахунок скасовано")
        EXPIRED = "expired", _("Протерміновано")
        REVOKED = "revoked", _("Відкликано")
        SUPERSEDED = "superseded", _("Замінено")

    class PayType(models.TextChoices):
        ONLINE_FULL = "online_full", _("Повна онлайн-оплата")
        PREPAYMENT = "prepayment", _("Передоплата")

    class PaymentPolicy(models.TextChoices):
        LEGACY = "legacy", _("Legacy checkout policy")
        FULL_ONLY = "full_only", _("Only full payment")
        FULL_OR_200_COD = (
            "full_or_200_cod",
            _("Full payment or 200 UAH + cash on delivery"),
        )

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    client = models.ForeignKey(
        "management.IgClient",
        on_delete=models.PROTECT,
        related_name="checkout_proposals",
    )
    deal = models.ForeignKey(
        "management.IgDeal",
        on_delete=models.PROTECT,
        related_name="checkout_proposals",
    )
    commercial_episode = models.ForeignKey(
        "management.IgCommercialEpisode",
        on_delete=models.PROTECT,
        related_name="checkout_proposals",
    )
    status = models.CharField(
        max_length=24,
        choices=Status.choices,
        default=Status.READY,
        db_index=True,
    )
    revision = models.PositiveIntegerField(default=1)
    locale = models.CharField(max_length=12, default="uk")
    currency = models.CharField(max_length=8, default="UAH")
    catalog_total = models.DecimalField(max_digits=12, decimal_places=2)
    negotiated_discount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=0,
    )
    quoted_total = models.DecimalField(max_digits=12, decimal_places=2)
    requested_payment_amount = models.DecimalField(max_digits=12, decimal_places=2)
    pay_type = models.CharField(
        max_length=20,
        choices=PayType.choices,
        default=PayType.ONLINE_FULL,
    )
    allow_promo = models.BooleanField(default=False)
    items_digest = models.CharField(max_length=64)
    expires_at = models.DateTimeField(default=default_checkout_proposal_expiry, db_index=True)
    viewed_at = models.DateTimeField(null=True, blank=True)
    details_locked_at = models.DateTimeField(null=True, blank=True)
    invoice_cancelled_at = models.DateTimeField(null=True, blank=True)
    provider_cancellation_event = models.ForeignKey(
        "management.IgPaymentEvent",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="cancelled_checkout_proposals",
    )
    paid_at = models.DateTimeField(null=True, blank=True)
    payment_attempt = models.OneToOneField(
        "orders.PaymentAttempt",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="instagram_checkout_proposal",
    )
    # Slice 2b is additive and fail-closed. Existing rows remain legacy even
    # when the rollout flag changes; callbacks use this durable marker rather
    # than the current setting so rollback cannot orphan an already-issued V2
    # invoice.
    assisted_checkout_v2 = models.BooleanField(default=False)
    current_invoice_generation = models.ForeignKey(
        "management.IgCheckoutInvoiceGeneration",
        null=True,
        blank=True,
        on_delete=models.DO_NOTHING,
        related_name="current_for_proposals",
        db_constraint=False,
    )
    winner_invoice_generation = models.OneToOneField(
        "management.IgCheckoutInvoiceGeneration",
        null=True,
        blank=True,
        on_delete=models.DO_NOTHING,
        related_name="winner_for_proposal",
        db_constraint=False,
    )
    payment_policy = models.CharField(
        max_length=24,
        choices=PaymentPolicy.choices,
        default=PaymentPolicy.LEGACY,
    )
    payment_policy_evidence_message = models.ForeignKey(
        "management.InstagramBotMessage",
        null=True,
        blank=True,
        on_delete=models.DO_NOTHING,
        related_name="checkout_payment_policy_evidence",
        db_constraint=False,
    )
    payment_policy_evidence_kind = models.CharField(
        max_length=32,
        blank=True,
        default="",
    )
    payment_policy_evidence_revision = models.PositiveBigIntegerField(default=0)
    payment_policy_evidence_digest = models.CharField(
        max_length=64,
        blank=True,
        default="",
    )
    custom_print_full_only = models.BooleanField(default=False)
    superseded_by = models.OneToOneField(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="supersedes",
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = IgCheckoutProposalManager()

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["status", "expires_at"], name="ig_prop_status_exp"),
            models.Index(fields=["client", "-created_at"], name="ig_prop_client_dt"),
            models.Index(fields=["deal", "-created_at"], name="ig_prop_deal_dt"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(catalog_total__gt=0),
                name="ig_prop_catalog_positive",
            ),
            models.CheckConstraint(
                condition=models.Q(quoted_total__gt=0),
                name="ig_prop_quoted_positive",
            ),
            models.CheckConstraint(
                condition=models.Q(requested_payment_amount__gt=0),
                name="ig_prop_payment_positive",
            ),
            models.CheckConstraint(
                condition=models.Q(negotiated_discount__gte=0),
                name="ig_prop_discount_nonnegative",
            ),
            models.CheckConstraint(
                condition=models.Q(revision__gte=1),
                name="ig_prop_revision_positive",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(status="superseded", superseded_by__isnull=False)
                    | ~models.Q(status="superseded")
                ),
                name="ig_prop_superseded_link",
            ),
            models.CheckConstraint(
                condition=~models.Q(status="paid", superseded_by__isnull=False),
                name="ig_prop_paid_not_superseded",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(assisted_checkout_v2=False, payment_policy="legacy")
                    | models.Q(
                        assisted_checkout_v2=True,
                        payment_policy__in=["full_only", "full_or_200_cod"],
                    )
                ),
                name="ig_prop_v2_policy_shape",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(custom_print_full_only=False)
                    | models.Q(payment_policy="full_only")
                ),
                name="ig_prop_custom_full_only",
            ),
        ]

    def __str__(self):  # pragma: no cover - trivial representation
        return f"IgCheckoutProposal#{self.pk} deal={self.deal_id} {self.status}"

    @property
    def is_expired(self):
        return self.expires_at <= timezone.now()

    def has_provider_confirmed_cancellation(
        self,
        *,
        attempt=None,
        event=None,
        projection=None,
    ):
        if self.status != self.Status.CANCELLED or not self.invoice_cancelled_at:
            return False
        attempt = attempt or self.payment_attempt
        event = event or self.provider_cancellation_event
        if attempt is None or attempt.order_id or self.deal.order_id:
            return False
        if event is None:
            return False
        if event.deal_id != self.deal_id or event.client_id != self.client_id:
            return False
        invoice_id = str(attempt.monobank_invoice_id or "").strip()
        status = str(event.provider_status or "").strip().lower()
        if not invoice_id or event.provider != "monobank":
            return False
        if str(event.invoice_id or "").strip() != invoice_id:
            return False
        if str(event.source or "").strip().lower() not in {
            "provider",
            "provider_pull",
            "provider_webhook",
            "signed_webhook",
            "webhook",
            "poll",
        }:
            return False
        from secrets import compare_digest

        expected_signature = provider_evidence_signature(
            deal_id=self.deal_id,
            client_id=self.client_id,
            provider=event.provider,
            source=event.source,
            invoice_id=event.invoice_id,
            provider_status=event.provider_status,
            payload_digest=event.payload_digest,
        )
        if not compare_digest(
            str((event.evidence or {}).get("signature") or ""),
            expected_signature,
        ):
            return False
        expected_attempt_status = {
            "cancelled": attempt.Status.CANCELLED,
            "canceled": attempt.Status.CANCELLED,
            "expired": attempt.Status.EXPIRED,
            "failure": attempt.Status.FAILED,
            "rejected": attempt.Status.FAILED,
        }.get(status)
        if expected_attempt_status is None or attempt.status != expected_attempt_status:
            return False
        projection = projection
        if projection is None:
            try:
                projection = self.deal.payment_projection
            except IgPaymentProjection.DoesNotExist:
                return False
        expected_truth = (
            self.deal.PaymentTruth.CANCELLED
            if expected_attempt_status in {attempt.Status.CANCELLED, attempt.Status.EXPIRED}
            else self.deal.PaymentTruth.FAILED
        )
        return (
            projection.last_event_id == event.pk
            and projection.truth == expected_truth
        )

    def clean(self):
        errors = {}
        if self.deal_id and self.client_id and self.deal.client_id != self.client_id:
            errors["client"] = "Proposal client must own the deal"
        if self.commercial_episode_id:
            episode = self.commercial_episode
            if self.client_id and episode.client_id != self.client_id:
                errors["commercial_episode"] = "Episode must belong to the proposal client"
            if episode.deal_id and self.deal_id and episode.deal_id != self.deal_id:
                errors["commercial_episode"] = "Episode must be bound to the proposal deal"
        if self.quoted_total is not None and self.quoted_total <= 0:
            errors["quoted_total"] = "Active proposal total must be positive"
        if self.catalog_total is not None and self.catalog_total <= 0:
            errors["catalog_total"] = "Catalog total must be positive"
        if (
            self.requested_payment_amount is not None
            and self.quoted_total is not None
            and (
                self.requested_payment_amount <= 0
                or self.requested_payment_amount > self.quoted_total
            )
        ):
            errors["requested_payment_amount"] = "Requested payment must fit the quote"
        if self._state.adding and self.status in {
            self.Status.READY,
            self.Status.VIEWED,
        } and self.expires_at <= timezone.now():
            errors["expires_at"] = "Active proposal expiry must be in the future"
        if self.status == self.Status.PAID and self.superseded_by_id:
            errors["superseded_by"] = "A paid proposal cannot be superseded"
        if self.status == self.Status.SUPERSEDED and not self.superseded_by_id:
            errors["superseded_by"] = "Superseded proposal must point to its replacement"
        if self.assisted_checkout_v2:
            if self.payment_policy == self.PaymentPolicy.LEGACY:
                errors["payment_policy"] = "V2 proposal requires a persisted payment policy"
            if self.payment_policy == self.PaymentPolicy.FULL_OR_200_COD:
                if (
                    not self.payment_policy_evidence_message_id
                    or self.payment_policy_evidence_kind
                    not in {"direct_question", "quick_reply"}
                    or not re.fullmatch(
                        r"[0-9a-f]{64}",
                        str(self.payment_policy_evidence_digest or ""),
                    )
                ):
                    errors["payment_policy"] = (
                        "200 UAH + COD requires current direct customer evidence"
                    )
                elif (
                    self.payment_policy_evidence_message.role
                    != self.payment_policy_evidence_message.Role.USER
                    or self.payment_policy_evidence_message.client_id != self.client_id
                ):
                    errors["payment_policy_evidence_message"] = (
                        "Payment policy evidence must be this customer's message"
                    )
            if self.custom_print_full_only and (
                self.payment_policy != self.PaymentPolicy.FULL_ONLY
            ):
                errors["payment_policy"] = "Custom print requires full payment"
            if (
                self.current_invoice_generation_id
                and self.current_invoice_generation.proposal_id != self.pk
            ):
                errors["current_invoice_generation"] = (
                    "Current generation must belong to this proposal"
                )
            if (
                self.winner_invoice_generation_id
                and self.winner_invoice_generation.proposal_id != self.pk
            ):
                errors["winner_invoice_generation"] = (
                    "Winner generation must belong to this proposal"
                )
        elif self.payment_policy != self.PaymentPolicy.LEGACY:
            errors["payment_policy"] = "Legacy proposal cannot carry a V2 policy"
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        if self.pk:
            previous = type(self).objects.filter(pk=self.pk).values(
                "client_id",
                "deal_id",
                "commercial_episode_id",
                "status",
                "revision",
                "catalog_total",
                "negotiated_discount",
                "quoted_total",
                "requested_payment_amount",
                "currency",
                "pay_type",
                "allow_promo",
                "items_digest",
                "expires_at",
                "details_locked_at",
                "invoice_cancelled_at",
                "provider_cancellation_event_id",
                "paid_at",
                "payment_attempt_id",
                "assisted_checkout_v2",
                "current_invoice_generation_id",
                "winner_invoice_generation_id",
                "payment_policy",
                "payment_policy_evidence_message_id",
                "payment_policy_evidence_kind",
                "payment_policy_evidence_revision",
                "payment_policy_evidence_digest",
                "custom_print_full_only",
                "superseded_by_id",
            ).first()
            if previous:
                if previous["status"] == self.Status.PAID:
                    protected = {
                        field: getattr(self, field)
                        for field in (
                            "client_id", "deal_id", "commercial_episode_id", "revision",
                            "catalog_total", "negotiated_discount", "quoted_total",
                            "requested_payment_amount", "currency", "pay_type", "allow_promo",
                            "items_digest", "expires_at", "details_locked_at",
                            "invoice_cancelled_at", "provider_cancellation_event_id",
                            "paid_at", "payment_attempt_id", "superseded_by_id",
                            "assisted_checkout_v2", "current_invoice_generation_id",
                            "winner_invoice_generation_id", "payment_policy",
                            "payment_policy_evidence_message_id",
                            "payment_policy_evidence_kind",
                            "payment_policy_evidence_revision",
                            "payment_policy_evidence_digest",
                            "custom_print_full_only",
                        )
                    }
                    if any(protected[field] != previous[field] for field in protected):
                        raise ValidationError("A paid proposal financial/state record is immutable")
                if previous["status"] == self.Status.PAID and self.status != self.Status.PAID:
                    raise ValidationError("A paid proposal cannot be superseded")
                if previous["status"] != self.Status.PAID and self.status == self.Status.PAID:
                    if not self.paid_at or not self.payment_attempt_id:
                        raise ValidationError("A proposal can be paid only after verified payment")
                    from orders.models import PaymentAttempt

                    attempt = PaymentAttempt.objects.filter(pk=self.payment_attempt_id).first()
                    if not attempt or attempt.status != PaymentAttempt.Status.CONVERTED or not attempt.order_id:
                        raise ValidationError("A proposal can be paid only after verified payment")
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("IgCheckoutProposal financial evidence cannot be deleted")


class IgCheckoutProposalItem(models.Model):
    proposal = models.ForeignKey(
        "management.IgCheckoutProposal",
        on_delete=models.CASCADE,
        related_name="items",
    )
    product = models.ForeignKey(
        "storefront.Product",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="instagram_checkout_items",
        db_constraint=False,
    )
    color_variant = models.ForeignKey(
        "productcolors.ProductColorVariant",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="instagram_checkout_items",
        db_constraint=False,
    )
    product_title = models.CharField(max_length=255)
    sku = models.CharField(max_length=128, blank=True, default="")
    image_url = models.CharField(max_length=600, blank=True, default="")
    color_code = models.CharField(max_length=64, blank=True, default="")
    color_label = models.CharField(max_length=100, blank=True, default="")
    size = models.CharField(max_length=32, blank=True, default="")
    fit_code = models.CharField(max_length=64, blank=True, default="")
    fit_label = models.CharField(max_length=100, blank=True, default="")
    option_values = models.JSONField(default=dict, blank=True)
    option_labels = models.JSONField(default=dict, blank=True)
    quantity = models.PositiveIntegerField(default=1)
    catalog_unit_price = models.DecimalField(max_digits=12, decimal_places=2)
    catalog_line_total = models.DecimalField(max_digits=12, decimal_places=2)
    quoted_unit_price = models.DecimalField(max_digits=12, decimal_places=2)
    quoted_line_total = models.DecimalField(max_digits=12, decimal_places=2)
    price_source = models.CharField(max_length=64, blank=True, default="catalog")
    evidence_message_ids = models.JSONField(default=list, blank=True)
    position = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["position", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["proposal", "position"],
                name="ig_prop_item_position_once",
            ),
            models.CheckConstraint(
                condition=models.Q(quantity__gt=0),
                name="ig_prop_item_qty_positive",
            ),
            models.CheckConstraint(
                condition=models.Q(catalog_unit_price__gte=0)
                & models.Q(quoted_unit_price__gte=0),
                name="ig_prop_item_prices_nonneg",
            ),
        ]


class _AppendOnlyCheckoutRevisionQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValueError("IgCheckoutRevision is append-only")

    def delete(self):
        raise ValueError("IgCheckoutRevision is append-only")


class IgCheckoutRevision(models.Model):
    class Source(models.TextChoices):
        BOT_CREATE = "bot_create", _("Створено ботом")
        BOT_UPDATE = "bot_update", _("Оновлено ботом")
        SYSTEM_SUPERSEDE = "system_supersede", _("Системна заміна")

    proposal = models.ForeignKey(
        "management.IgCheckoutProposal",
        on_delete=models.CASCADE,
        related_name="revisions",
    )
    revision = models.PositiveIntegerField()
    digest = models.CharField(max_length=64)
    snapshot = models.JSONField(default=dict)
    source = models.CharField(max_length=24, choices=Source.choices)
    evidence_message_ids = models.JSONField(default=list, blank=True)
    source_watermark_message_id = models.PositiveBigIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    objects = models.Manager.from_queryset(_AppendOnlyCheckoutRevisionQuerySet)()

    class Meta:
        ordering = ["proposal", "revision"]
        constraints = [
            models.UniqueConstraint(
                fields=["proposal", "revision"],
                name="ig_prop_revision_once",
            ),
            models.CheckConstraint(
                condition=models.Q(revision__gte=1),
                name="ig_prop_revision_num_pos",
            ),
        ]

    def save(self, *args, **kwargs):
        if self.pk and not kwargs.get("force_insert"):
            raise ValueError("IgCheckoutRevision is append-only")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("IgCheckoutRevision is append-only")


class IgCheckoutAccessToken(models.Model):
    class Kind(models.TextChoices):
        BOT = "bot", _("Посилання бота")
        SHARE = "share", _("Посилання для оплати іншою людиною")
        REPLACEMENT = "replacement", _("Посилання заміни")

    proposal = models.ForeignKey(
        "management.IgCheckoutProposal",
        on_delete=models.CASCADE,
        related_name="access_tokens",
    )
    token_digest = models.CharField(max_length=64, unique=True)
    kind = models.CharField(max_length=16, choices=Kind.choices, default=Kind.BOT)
    expires_at = models.DateTimeField(db_index=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    use_count = models.PositiveIntegerField(default=0)
    last_used_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["proposal", "kind", "expires_at"], name="ig_token_prop_exp"),
        ]

    @classmethod
    def issue(cls, *, proposal, kind=Kind.BOT, expires_at=None):
        raw_token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        token = cls.objects.create(
            proposal=proposal,
            token_digest=digest,
            kind=kind,
            expires_at=min(expires_at or proposal.expires_at, proposal.expires_at),
        )
        return raw_token, token

    @classmethod
    def digest(cls, raw_token):
        return hashlib.sha256(str(raw_token or "").encode("utf-8")).hexdigest()


class IgCheckoutInvoiceGeneration(models.Model):
    """One 25-minute provider-invoice attempt inside a 12-hour proposal."""

    class State(models.TextChoices):
        PLANNED = "planned", _("Planned")
        PROVIDER_INFLIGHT = "provider_inflight", _("Provider request in flight")
        INVOICE_CREATED = "invoice_created", _("Invoice created")
        PROVIDER_AMBIGUOUS = "provider_ambiguous", _("Provider result ambiguous")
        AMBIGUITY_REVIEW = "ambiguity_review", _("Ambiguous call needs review")
        LATE_PROVIDER_REVIEW = "late_provider_review", _("Late create result review")
        FAILED = "failed", _("Generation failed")
        EXPIRED = "expired", _("Generation expired")
        CANCELLED = "cancelled", _("Generation cancelled")
        WINNER_CLAIMED = "winner_claimed", _("Verified winner claimed")
        PAID_WINNER = "paid_winner", _("Winner order materialized")
        LATE_PAID_REVIEW = "late_paid_review", _("Paid losing generation review")
        RESOURCE_REVIEW = "resource_review", _("Paid but resources need review")

    class PaymentChoice(models.TextChoices):
        FULL = "online_full", _("Full online payment")
        PREPAY_200_COD = "prepay_200_cod", _("200 UAH + COD")

    proposal = models.ForeignKey(
        "management.IgCheckoutProposal",
        on_delete=models.PROTECT,
        related_name="invoice_generations",
        db_constraint=False,
    )
    generation = models.PositiveIntegerField()
    series_key = models.CharField(max_length=64)
    proposal_revision = models.PositiveIntegerField()
    active_slot = models.PositiveSmallIntegerField(null=True, blank=True)
    winner_slot = models.PositiveSmallIntegerField(null=True, blank=True)
    state = models.CharField(
        max_length=28,
        choices=State.choices,
        default=State.PLANNED,
        db_index=True,
    )
    payment_choice = models.CharField(
        max_length=24,
        choices=PaymentChoice.choices,
        default=PaymentChoice.FULL,
    )
    payment_amount = models.DecimalField(max_digits=12, decimal_places=2)
    policy_evidence_message = models.ForeignKey(
        "management.InstagramBotMessage",
        null=True,
        blank=True,
        on_delete=models.DO_NOTHING,
        related_name="checkout_invoice_generation_policy_evidence",
        db_constraint=False,
    )
    policy_evidence_kind = models.CharField(max_length=32, blank=True, default="")
    policy_evidence_digest = models.CharField(max_length=64, blank=True, default="")
    promo_reservation_generation = models.CharField(
        max_length=64,
        blank=True,
        default="",
    )
    payment_attempt = models.OneToOneField(
        "orders.PaymentAttempt",
        null=True,
        blank=True,
        on_delete=models.DO_NOTHING,
        related_name="instagram_checkout_generation",
        db_constraint=False,
    )
    provider = models.CharField(max_length=24, default="monobank")
    provider_invoice_id = models.CharField(
        max_length=128,
        null=True,
        blank=True,
    )
    provider_call_token = models.CharField(max_length=64, unique=True)
    provider_request_digest = models.CharField(max_length=64, blank=True, default="")
    expires_at = models.DateTimeField(db_index=True)
    ambiguity_review_due_at = models.DateTimeField(null=True, blank=True)
    ambiguity_resolved_at = models.DateTimeField(null=True, blank=True)
    provider_started_at = models.DateTimeField(null=True, blank=True)
    provider_completed_at = models.DateTimeField(null=True, blank=True)
    winner_claimed_at = models.DateTimeField(null=True, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    terminal_at = models.DateTimeField(null=True, blank=True)
    review_reason = models.CharField(max_length=96, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["proposal_id", "generation"]
        constraints = [
            models.UniqueConstraint(
                fields=["proposal", "generation"],
                name="ig_invgen_proposal_generation_uniq",
            ),
            models.UniqueConstraint(
                fields=["proposal", "active_slot"],
                name="ig_invgen_active_slot_uniq",
            ),
            models.UniqueConstraint(
                fields=["proposal", "winner_slot"],
                name="ig_invgen_winner_slot_uniq",
            ),
            models.UniqueConstraint(
                fields=["provider", "provider_invoice_id"],
                name="ig_invgen_provider_invoice_uniq",
            ),
            models.CheckConstraint(
                condition=models.Q(generation__gte=1),
                name="ig_invgen_generation_positive",
            ),
            models.CheckConstraint(
                condition=models.Q(active_slot__isnull=True) | models.Q(active_slot=1),
                name="ig_invgen_active_slot_shape",
            ),
            models.CheckConstraint(
                condition=models.Q(winner_slot__isnull=True) | models.Q(winner_slot=1),
                name="ig_invgen_winner_slot_shape",
            ),
            models.CheckConstraint(
                condition=models.Q(payment_amount__gt=0),
                name="ig_invgen_payment_positive",
            ),
        ]
        indexes = [
            models.Index(
                fields=["proposal", "state", "generation"],
                name="ig_invgen_prop_state_gen",
            ),
            models.Index(
                fields=["state", "expires_at", "id"],
                name="ig_invgen_state_expiry",
            ),
            models.Index(
                fields=["provider", "provider_invoice_id"],
                name="ig_invgen_provider_invoice",
            ),
            models.Index(
                fields=["state", "ambiguity_review_due_at", "id"],
                name="ig_invgen_ambiguity_due",
            ),
        ]


class _AppendOnlyCheckoutGenerationEventQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValueError("IgCheckoutInvoiceGenerationEvent is append-only")

    def delete(self):
        raise ValueError("IgCheckoutInvoiceGenerationEvent is append-only")


class IgCheckoutInvoiceGenerationEvent(models.Model):
    class Kind(models.TextChoices):
        CREATED = "created", _("Generation created")
        PROVIDER_STARTED = "provider_started", _("Provider request started")
        PROVIDER_SUCCEEDED = "provider_succeeded", _("Provider request succeeded")
        PROVIDER_FAILED = "provider_failed", _("Provider request failed")
        PROVIDER_AMBIGUOUS = "provider_ambiguous", _("Provider request ambiguous")
        TERMINALIZED = "terminalized", _("Generation terminalized")
        WINNER_CLAIMED = "winner_claimed", _("Winner claimed")
        LOSER_PAID_REVIEW = "loser_paid_review", _("Paid loser needs review")
        RESOURCE_REVIEW = "resource_review", _("Paid generation lacks resources")

    event_key = models.CharField(max_length=180, unique=True)
    generation = models.ForeignKey(
        "management.IgCheckoutInvoiceGeneration",
        on_delete=models.DO_NOTHING,
        related_name="events",
        db_constraint=False,
    )
    proposal = models.ForeignKey(
        "management.IgCheckoutProposal",
        on_delete=models.DO_NOTHING,
        related_name="invoice_generation_events",
        db_constraint=False,
    )
    payment_attempt = models.ForeignKey(
        "orders.PaymentAttempt",
        null=True,
        blank=True,
        on_delete=models.DO_NOTHING,
        related_name="instagram_checkout_generation_events",
        db_constraint=False,
    )
    kind = models.CharField(max_length=32, choices=Kind.choices, db_index=True)
    payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    objects = models.Manager.from_queryset(
        _AppendOnlyCheckoutGenerationEventQuerySet
    )()

    class Meta:
        base_manager_name = "objects"
        ordering = ["id"]
        indexes = [
            models.Index(
                fields=["generation", "kind", "id"],
                name="ig_invgevt_generation_kind",
            ),
            models.Index(
                fields=["proposal", "kind", "id"],
                name="ig_invgevt_proposal_kind",
            ),
        ]

    def save(self, *args, **kwargs):
        if self.pk and not kwargs.get("force_insert"):
            raise ValueError("IgCheckoutInvoiceGenerationEvent is append-only")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("IgCheckoutInvoiceGenerationEvent is append-only")


class IgCheckoutInventoryReservation(models.Model):
    class State(models.TextChoices):
        ACTIVE = "active", _("Зарезервовано")
        PAID_COMMITTED = "paid_committed", _("Оплата підтверджена")
        FULFILLED = "fulfilled", _("Виконано")
        RELEASED = "released", _("Звільнено")
        OVERBOOKED_REVIEW = "overbooked_review", _("Потрібна перевірка дефіциту")
        # Kept for rows written by the pre-allocation implementation. New
        # payment paths use FULFILLED; the lifecycle migration normalizes old
        # consumed rows where it can prove a catalog allocation.
        CONSUMED = "consumed", _("Використано")

    proposal = models.ForeignKey(
        "management.IgCheckoutProposal",
        on_delete=models.PROTECT,
        related_name="inventory_reservations",
    )
    invoice_generation = models.ForeignKey(
        "management.IgCheckoutInvoiceGeneration",
        null=True,
        blank=True,
        on_delete=models.DO_NOTHING,
        related_name="inventory_reservations",
        db_constraint=False,
    )
    item = models.ForeignKey(
        "management.IgCheckoutProposalItem",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="inventory_reservations",
    )
    product = models.ForeignKey(
        "storefront.Product",
        on_delete=models.PROTECT,
        related_name="instagram_checkout_reservations",
        db_constraint=False,
    )
    color_variant = models.ForeignKey(
        "productcolors.ProductColorVariant",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="instagram_checkout_reservations",
        db_constraint=False,
    )
    allocation_source = models.CharField(max_length=24, default="catalog_variant")
    stock_item = models.ForeignKey(
        "warehouse.StockItem",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="instagram_checkout_reservations",
        db_constraint=False,
    )
    allocation_key = models.CharField(max_length=128, blank=True, default="")
    line_ids = models.JSONField(default=list, blank=True)
    order = models.ForeignKey(
        "orders.Order",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="instagram_inventory_reservations",
        db_constraint=False,
    )
    order_item = models.ForeignKey(
        "orders.OrderItem",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="instagram_inventory_reservations",
        db_constraint=False,
    )
    write_off_request = models.ForeignKey(
        "warehouse.WriteOffRequest",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="instagram_inventory_reservations",
        db_constraint=False,
    )
    stock_movement = models.OneToOneField(
        "warehouse.StockMovement",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="instagram_inventory_reservation",
        db_constraint=False,
    )
    quantity = models.PositiveIntegerField()
    reservation_fingerprint = models.CharField(max_length=64, unique=True)
    state = models.CharField(
        max_length=24,
        choices=State.choices,
        default=State.ACTIVE,
        db_index=True,
    )
    expires_at = models.DateTimeField(db_index=True)
    released_at = models.DateTimeField(null=True, blank=True)
    consumed_at = models.DateTimeField(null=True, blank=True)
    paid_committed_at = models.DateTimeField(null=True, blank=True)
    fulfilled_at = models.DateTimeField(null=True, blank=True)
    release_reason = models.CharField(max_length=128, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["state", "expires_at"], name="ig_res_state_exp"),
            models.Index(fields=["proposal", "state"], name="ig_res_prop_state"),
            models.Index(
                fields=["invoice_generation", "state", "id"],
                name="ig_res_generation_state",
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(quantity__gt=0),
                name="ig_res_qty_positive",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(allocation_source="warehouse", stock_item__isnull=False)
                    | models.Q(allocation_source="catalog_variant")
                    | models.Q(allocation_source="untracked")
                ),
                name="ig_res_allocation_source_valid",
            ),
        ]


class _IgLifecycleEventQuerySet(models.QuerySet):
    _IDENTITY_FIELDS = {
        "event_key",
        "kind",
        "client",
        "client_id",
        "deal",
        "deal_id",
        "proposal",
        "proposal_id",
        "order",
        "order_id",
        "commercial_episode",
        "commercial_episode_id",
        "attribution",
        "attribution_id",
        "locale",
        "payload",
        "final_text",
    }

    def update(self, **kwargs):
        if self._IDENTITY_FIELDS.intersection(kwargs):
            raise ValueError("IgLifecycleEvent identity is immutable")
        return super().update(**kwargs)

    def delete(self):
        raise ValueError("IgLifecycleEvent is durable")


class IgLifecycleEvent(models.Model):
    class Kind(models.TextChoices):
        PAYMENT_VERIFIED = "payment_verified", _("Оплату підтверджено")
        TTN_CREATED = "ttn_created", _("ТТН створено")
        # Код 7 Нової Пошти — «прибуло у відділення». До цього між «ТТН створено»
        # і «отримано» у бота не було жодної події, хоча незабрана посилка — це
        # прямий збиток: зворотна доставка плюс замерзлий товар плюс майже
        # гарантована втрата повторної продажі, а для наложки ще й втрата виручки.
        PARCEL_ARRIVED = "parcel_arrived", _("Посилка у відділенні")
        DELIVERED_REVIEW_REQUESTED = (
            "delivered_review_requested",
            _("Замовлення отримано, запит відгуку"),
        )

    class State(models.TextChoices):
        PENDING = "pending", _("Очікує")
        PROCESSING = "processing", _("Обробляється")
        SENT = "sent", _("Надіслано")
        WAITING_WINDOW = "waiting_window", _("Поза вікном відповіді")
        MANAGER_REVIEW = "manager_review", _("Потрібен менеджер")
        AMBIGUOUS = "ambiguous", _("Невідомий результат")
        FAILED = "failed", _("Помилка")
        CANCELLED = "cancelled", _("Скасовано")

    event_key = models.CharField(max_length=180, unique=True)
    kind = models.CharField(max_length=40, choices=Kind.choices, db_index=True)
    client = models.ForeignKey(
        "management.IgClient",
        on_delete=models.PROTECT,
        related_name="lifecycle_events",
    )
    deal = models.ForeignKey(
        "management.IgDeal",
        on_delete=models.PROTECT,
        related_name="lifecycle_events",
    )
    proposal = models.ForeignKey(
        "management.IgCheckoutProposal",
        on_delete=models.PROTECT,
        related_name="lifecycle_events",
    )
    order = models.ForeignKey(
        "orders.Order",
        on_delete=models.PROTECT,
        related_name="instagram_lifecycle_events",
    )
    commercial_episode = models.ForeignKey(
        "management.IgCommercialEpisode",
        on_delete=models.PROTECT,
        related_name="lifecycle_events",
    )
    attribution = models.ForeignKey(
        "management.IgOrderAttribution",
        on_delete=models.PROTECT,
        related_name="lifecycle_events",
    )
    locale = models.CharField(max_length=12, default="uk")
    payload = models.JSONField(default=dict, blank=True)
    # Optional follow copy is materialized exactly once immediately before
    # provider I/O. It is deliberately separate from the immutable business
    # payload so retries cannot rewrite payment evidence or message identity.
    final_text = models.TextField(blank=True, default="")
    state = models.CharField(
        max_length=24,
        choices=State.choices,
        default=State.PENDING,
        db_index=True,
    )
    due_at = models.DateTimeField(default=timezone.now, db_index=True)
    attempts = models.PositiveIntegerField(default=0)
    lease_token = models.CharField(max_length=64, blank=True, default="")
    lease_expires_at = models.DateTimeField(null=True, blank=True, db_index=True)
    provider_message_id = models.CharField(max_length=128, blank=True, default="")
    last_error = models.CharField(max_length=1000, blank=True, default="")
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = models.Manager.from_queryset(_IgLifecycleEventQuerySet)()

    class Meta:
        ordering = ["due_at", "id"]
        indexes = [
            models.Index(fields=["state", "due_at", "id"], name="ig_life_state_due"),
            models.Index(fields=["client", "-created_at"], name="ig_life_client_dt"),
            models.Index(fields=["order", "kind"], name="ig_life_order_kind"),
        ]

    def clean(self):
        errors = {}
        if self.deal_id and self.client_id and self.deal.client_id != self.client_id:
            errors["deal"] = "Lifecycle deal must belong to the client"
        if self.proposal_id:
            if self.proposal.client_id != self.client_id:
                errors["proposal"] = "Lifecycle proposal must belong to the client"
            if self.proposal.deal_id != self.deal_id:
                errors["proposal"] = "Lifecycle proposal must belong to the deal"
            if self.proposal.commercial_episode_id != self.commercial_episode_id:
                errors["commercial_episode"] = "Lifecycle episode must match proposal"
            if not self.proposal.payment_attempt_id:
                errors["proposal"] = "Lifecycle proposal requires a payment attempt"
            elif self.proposal.payment_attempt.order_id != self.order_id:
                errors["order"] = "Lifecycle order must match proposal payment attempt"
        if not self.order_id:
            errors["order"] = "Lifecycle event requires an order"
        if not self.attribution_id:
            errors["attribution"] = "Lifecycle event requires exact attribution"
        if self.attribution_id and self.order_id:
            if self.attribution.order_id != self.order_id:
                errors["attribution"] = "Attribution must belong to the lifecycle order"
            if self.attribution.client_id != self.client_id:
                errors["attribution"] = "Attribution must belong to the lifecycle client"
            if self.attribution.deal_id != self.deal_id:
                errors["attribution"] = "Attribution must belong to the lifecycle deal"
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        if self.pk:
            previous = type(self).objects.filter(pk=self.pk).values(
                "event_key",
                "kind",
                "client_id",
                "deal_id",
                "proposal_id",
                "order_id",
                "commercial_episode_id",
                "attribution_id",
                "locale",
                "payload",
                "final_text",
            ).first()
            if previous:
                for field_name in (
                    "event_key",
                    "kind",
                    "client_id",
                    "deal_id",
                    "proposal_id",
                    "order_id",
                    "commercial_episode_id",
                    "attribution_id",
                    "locale",
                    "payload",
                ):
                    if getattr(self, field_name) != previous[field_name]:
                        raise ValidationError("IgLifecycleEvent identity is immutable")
                previous_final_text = previous.get("final_text", "")
                if previous_final_text and self.final_text != previous_final_text:
                    raise ValidationError("IgLifecycleEvent final_text is immutable")
        # Leave uniqueness enforcement to the database so duplicate event
        # delivery raises IntegrityError and remains transaction-safe.
        self.full_clean(validate_unique=False)
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("IgLifecycleEvent is durable")


class IgFollowCapabilityState(models.Model):
    """Provider-wide capability and circuit state for follow lookups."""

    class Status(models.TextChoices):
        UNKNOWN = "unknown", _("Невідомо")
        AVAILABLE = "available", _("Доступно")
        DEGRADED = "degraded", _("Тимчасово недоступно")
        BLOCKED = "blocked", _("Заблоковано політикою Meta")

    singleton_key = models.PositiveSmallIntegerField(default=1, unique=True)
    transport = models.CharField(max_length=32, blank=True, default="")
    graph_version = models.CharField(max_length=16, blank=True, default="")
    ig_user_id = models.CharField(max_length=64, blank=True, default="")
    config_fingerprint = models.CharField(max_length=64, blank=True, default="")
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.UNKNOWN,
        db_index=True,
    )
    checked_at = models.DateTimeField(null=True, blank=True)
    next_probe_at = models.DateTimeField(null=True, blank=True, db_index=True)
    blocked_until = models.DateTimeField(null=True, blank=True, db_index=True)
    consecutive_failures = models.PositiveIntegerField(default=0)
    last_error_kind = models.CharField(max_length=32, blank=True, default="")
    last_error_code = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(singleton_key=1),
                name="ig_follow_cap_singleton_one",
            ),
        ]
        indexes = [
            models.Index(
                fields=["status", "next_probe_at"],
                name="ig_follow_cap_status",
            ),
        ]

    def is_probe_blocked(self, *, now=None) -> bool:
        now = now or timezone.now()
        return bool(
            (self.blocked_until and self.blocked_until > now)
            or (self.next_probe_at and self.next_probe_at > now)
        )


class IgFollowState(models.Model):
    """Latest authoritative follow observation for one Instagram client."""

    class State(models.TextChoices):
        UNKNOWN = "unknown", _("Невідомо")
        FOLLOWING = "following", _("Підписаний")
        NOT_FOLLOWING = "not_following", _("Не підписаний")

    class CheckResult(models.TextChoices):
        NEVER = "never", _("Не перевірялось")
        KNOWN = "known", _("Отримано")
        ERROR = "error", _("Помилка")
        SKIPPED = "skipped", _("Пропущено політикою")

    client = models.OneToOneField(
        "management.IgClient",
        on_delete=models.DO_NOTHING,
        related_name="follow_state_projection",
        db_constraint=False,
    )
    state = models.CharField(
        max_length=16,
        choices=State.choices,
        default=State.UNKNOWN,
        db_index=True,
    )
    revision = models.PositiveBigIntegerField(default=0)
    source = models.CharField(max_length=32, blank=True, default="")
    graph_version = models.CharField(max_length=16, blank=True, default="")
    config_fingerprint = models.CharField(max_length=64, blank=True, default="")
    observed_at = models.DateTimeField(null=True, blank=True, db_index=True)
    expires_at = models.DateTimeField(null=True, blank=True, db_index=True)
    first_observed_following_at = models.DateTimeField(null=True, blank=True)
    last_check_at = models.DateTimeField(null=True, blank=True)
    last_result = models.CharField(
        max_length=16,
        choices=CheckResult.choices,
        default=CheckResult.NEVER,
    )
    consecutive_failures = models.PositiveIntegerField(default=0)
    last_error_kind = models.CharField(max_length=32, blank=True, default="")
    last_error_code = models.CharField(max_length=64, blank=True, default="")
    next_retry_at = models.DateTimeField(null=True, blank=True, db_index=True)
    refresh_generation = models.PositiveBigIntegerField(default=0)
    refresh_lease_token = models.CharField(max_length=64, blank=True, default="")
    refresh_lease_expires_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_cta_touch_at = models.DateTimeField(null=True, blank=True, db_index=True)
    cta_refused_at = models.DateTimeField(null=True, blank=True)
    cta_refusal_message_id = models.PositiveBigIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(
                fields=["state", "expires_at"],
                name="ig_follow_state_fresh",
            ),
            models.Index(
                fields=["next_retry_at", "client"],
                name="ig_follow_state_retry",
            ),
        ]


class _AppendOnlyFollowObservationQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValueError("IgFollowObservation is append-only")

    def bulk_update(self, objs, fields, batch_size=None):
        raise ValueError("IgFollowObservation is append-only")

    def delete(self):
        raise ValueError("IgFollowObservation is append-only")

    def _raw_delete(self, using):
        raise ValueError("IgFollowObservation is append-only")


class IgFollowObservation(models.Model):
    """Append-only, token-free evidence returned by the Meta follow endpoint."""

    class Result(models.TextChoices):
        KNOWN = "known", _("Отримано")
        ERROR = "error", _("Помилка")
        SKIPPED = "skipped", _("Пропущено")

    client = models.ForeignKey(
        "management.IgClient",
        on_delete=models.DO_NOTHING,
        related_name="follow_observations",
        db_constraint=False,
    )
    revision = models.PositiveBigIntegerField(default=0)
    trigger = models.CharField(max_length=32)
    result = models.CharField(max_length=16, choices=Result.choices, db_index=True)
    observed_value = models.BooleanField(null=True, blank=True)
    field_present = models.BooleanField(default=False)
    field_type = models.CharField(max_length=24, blank=True, default="")
    transport = models.CharField(max_length=32, blank=True, default="")
    graph_version = models.CharField(max_length=16, blank=True, default="")
    config_fingerprint = models.CharField(max_length=64)
    http_code = models.PositiveSmallIntegerField(null=True, blank=True)
    graph_code = models.PositiveIntegerField(null=True, blank=True)
    graph_subcode = models.PositiveIntegerField(null=True, blank=True)
    error_kind = models.CharField(max_length=32, blank=True, default="")
    error_code = models.CharField(max_length=64, blank=True, default="")
    duration_ms = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    objects = models.Manager.from_queryset(_AppendOnlyFollowObservationQuerySet)()

    class Meta:
        ordering = ["-id"]
        indexes = [
            models.Index(
                fields=["client", "-created_at"],
                name="ig_follow_obs_client_dt",
            ),
            models.Index(
                fields=["result", "-created_at"],
                name="ig_follow_obs_result_dt",
            ),
        ]

    def save(self, *args, **kwargs):
        if self.pk and not kwargs.get("force_insert"):
            raise ValueError("IgFollowObservation is append-only")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("IgFollowObservation is append-only")


class IgFollowRefreshJob(models.Model):
    """One coalescing, lease-backed follow lookup request per client."""

    class Status(models.TextChoices):
        PENDING = "pending", _("Очікує")
        PROCESSING = "processing", _("Обробляється")
        DONE = "done", _("Завершено")
        FAILED = "failed", _("Помилка")

    client = models.OneToOneField(
        "management.IgClient",
        on_delete=models.DO_NOTHING,
        related_name="follow_refresh_job",
        db_constraint=False,
    )
    requested_generation = models.PositiveBigIntegerField(default=0)
    claimed_generation = models.PositiveBigIntegerField(default=0)
    triggers = models.JSONField(default=list, blank=True)
    expected_config_fingerprint = models.CharField(max_length=64, blank=True, default="")
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    due_at = models.DateTimeField(default=timezone.now, db_index=True)
    attempts = models.PositiveIntegerField(default=0)
    lease_token = models.CharField(max_length=64, blank=True, default="")
    lease_expires_at = models.DateTimeField(null=True, blank=True, db_index=True)
    next_attempt_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_error_kind = models.CharField(max_length=32, blank=True, default="")
    last_error_code = models.CharField(max_length=64, blank=True, default="")
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(
                fields=["status", "due_at", "id"],
                name="ig_follow_job_due",
            ),
        ]


class _IgFollowCtaDecisionQuerySet(models.QuerySet):
    _IDENTITY_FIELDS = {
        "trigger_key",
        "client",
        "client_id",
        "opportunity",
        "commercial_episode",
        "commercial_episode_id",
        "order",
        "order_id",
        "lifecycle_event",
        "lifecycle_event_id",
        "source_message",
        "source_message_id",
        "follow_state_revision",
        "conversation_watermark",
        "context_fingerprint",
        "base_text",
    }

    def update(self, **kwargs):
        if self._IDENTITY_FIELDS.intersection(kwargs):
            raise ValueError("IgFollowCtaDecision identity is immutable")
        return super().update(**kwargs)

    def bulk_update(self, objs, fields, batch_size=None):
        if self._IDENTITY_FIELDS.intersection(fields):
            raise ValueError("IgFollowCtaDecision identity is immutable")
        return super().bulk_update(objs, fields, batch_size=batch_size)

    def delete(self):
        raise ValueError("IgFollowCtaDecision is durable")

    def _raw_delete(self, using):
        raise ValueError("IgFollowCtaDecision is durable")


class IgFollowCtaDecision(models.Model):
    """Durable optional follow CTA decision and provider delivery outcome."""

    class Opportunity(models.TextChoices):
        PAYMENT = "payment", _("Підтвердження оплати")
        HESITATION = "hesitation", _("М'яке вагання")
        POST_DELIVERY = "post_delivery", _("Після позитивної відповіді")

    class State(models.TextChoices):
        SUPPRESSED = "suppressed", _("Заборонено політикою")
        WAITING_FOLLOW = "waiting_follow", _("Очікує перевірки підписки")
        PREPARING = "preparing", _("Готується")
        PREPARED = "prepared", _("Підготовлено")
        RESERVED = "reserved", _("Зарезервовано до відправлення")
        SENT = "sent", _("Надіслано")
        AMBIGUOUS = "ambiguous", _("Результат невідомий")
        CANCELLED = "cancelled", _("Скасовано")
        FAILED = "failed", _("Помилка")

    trigger_key = models.CharField(max_length=180, unique=True)
    client = models.ForeignKey(
        "management.IgClient",
        on_delete=models.DO_NOTHING,
        related_name="follow_cta_decisions",
        db_constraint=False,
    )
    commercial_episode = models.ForeignKey(
        "management.IgCommercialEpisode",
        null=True,
        blank=True,
        on_delete=models.DO_NOTHING,
        related_name="follow_cta_decisions",
        db_constraint=False,
    )
    order = models.ForeignKey(
        "orders.Order",
        null=True,
        blank=True,
        on_delete=models.DO_NOTHING,
        related_name="instagram_follow_cta_decisions",
        db_constraint=False,
    )
    lifecycle_event = models.ForeignKey(
        "management.IgLifecycleEvent",
        null=True,
        blank=True,
        on_delete=models.DO_NOTHING,
        related_name="follow_cta_decisions",
        db_constraint=False,
    )
    source_message = models.ForeignKey(
        "management.InstagramBotMessage",
        null=True,
        blank=True,
        on_delete=models.DO_NOTHING,
        related_name="follow_cta_decisions",
        db_constraint=False,
    )
    opportunity = models.CharField(max_length=24, choices=Opportunity.choices, db_index=True)
    policy_version = models.CharField(max_length=32, default="follow-v1")
    state = models.CharField(
        max_length=24,
        choices=State.choices,
        default=State.PREPARING,
        db_index=True,
    )
    episode_slot_key = models.CharField(
        max_length=160,
        null=True,
        blank=True,
        unique=True,
    )
    sent_scope_key = models.CharField(
        max_length=180,
        null=True,
        blank=True,
        unique=True,
    )
    follow_state_revision = models.PositiveBigIntegerField(default=0)
    conversation_watermark = models.PositiveBigIntegerField(default=0)
    context_fingerprint = models.CharField(max_length=64, blank=True, default="")
    base_text = models.TextField(blank=True, default="")
    candidate_text = models.CharField(max_length=300, blank=True, default="")
    candidate_hash = models.CharField(max_length=64, blank=True, default="")
    final_text = models.TextField(blank=True, default="")
    suppression_reason = models.CharField(max_length=64, blank=True, default="")
    reason_codes = models.JSONField(default=list, blank=True)
    model = models.CharField(max_length=80, blank=True, default="")
    model_key_alias = models.CharField(max_length=80, blank=True, default="")
    prompt_version = models.CharField(max_length=40, blank=True, default="")
    lease_token = models.CharField(max_length=64, blank=True, default="")
    lease_expires_at = models.DateTimeField(null=True, blank=True, db_index=True)
    provider_message_ids = models.JSONField(default=list, blank=True)
    provider_io_started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    follow_observed_after_cta_at = models.DateTimeField(null=True, blank=True)
    last_error_kind = models.CharField(max_length=32, blank=True, default="")
    last_error_code = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = models.Manager.from_queryset(_IgFollowCtaDecisionQuerySet)()

    _IDENTITY_FIELDS = (
        "trigger_key",
        "client_id",
        "commercial_episode_id",
        "order_id",
        "lifecycle_event_id",
        "source_message_id",
        "opportunity",
        "follow_state_revision",
        "conversation_watermark",
        "context_fingerprint",
        "base_text",
    )

    class Meta:
        ordering = ["-id"]
        indexes = [
            models.Index(
                fields=["client", "-created_at"],
                name="ig_follow_dec_client_dt",
            ),
            models.Index(
                fields=["state", "-created_at"],
                name="ig_follow_dec_state_dt",
            ),
            models.Index(
                fields=["commercial_episode", "state"],
                name="ig_follow_dec_episode",
            ),
        ]

    def save(self, *args, **kwargs):
        if self.pk:
            previous = type(self).objects.filter(pk=self.pk).values(
                *self._IDENTITY_FIELDS
            ).first()
            if previous:
                for field_name in self._IDENTITY_FIELDS:
                    if getattr(self, field_name) != previous[field_name]:
                        raise ValueError("IgFollowCtaDecision identity is immutable")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("IgFollowCtaDecision is durable")


class IgPaymentFollowPreparation(models.Model):
    """Lease-backed optional-copy preparation for one payment lifecycle event."""

    class State(models.TextChoices):
        PENDING = "pending", _("Очікує підготовки")
        WAITING_FOLLOW = "waiting_follow", _("Очікує перевірки підписки")
        PROCESSING = "processing", _("Готується")
        PREPARED = "prepared", _("Підготовлено")
        SUPPRESSED = "suppressed", _("Не додавати CTA")
        EXPIRED = "expired", _("Час підготовки минув")
        FAILED = "failed", _("Помилка підготовки")

    lifecycle_event = models.OneToOneField(
        "management.IgLifecycleEvent",
        on_delete=models.DO_NOTHING,
        related_name="follow_preparation",
        db_constraint=False,
    )
    client = models.ForeignKey(
        "management.IgClient",
        on_delete=models.DO_NOTHING,
        related_name="payment_follow_preparations",
        db_constraint=False,
    )
    deadline_at = models.DateTimeField(db_index=True)
    state = models.CharField(
        max_length=24,
        choices=State.choices,
        default=State.PENDING,
        db_index=True,
    )
    attempts = models.PositiveSmallIntegerField(default=0)
    lease_token = models.CharField(max_length=64, blank=True, default="")
    lease_expires_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_error_kind = models.CharField(max_length=32, blank=True, default="")
    last_error_code = models.CharField(max_length=64, blank=True, default="")
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(
                fields=["state", "deadline_at", "id"],
                name="ig_pay_follow_prep_due",
            ),
            models.Index(
                fields=["client", "state"],
                name="ig_pay_follow_prep_client",
            ),
        ]


class IgPostSaleCase(models.Model):
    """Exchange or return request tied to an existing purchase journey."""

    class CaseType(models.TextChoices):
        EXCHANGE = "exchange", _("Обмін")
        RETURN = "return", _("Повернення")

    class Status(models.TextChoices):
        NEEDS_DETAILS = "needs_details", _("Потрібні уточнення")
        OPEN = "open", _("Відкрито")
        APPROVED = "approved", _("Погоджено")
        IN_TRANSIT = "in_transit", _("У дорозі")
        RECEIVED = "received", _("Отримано")
        COMPLETED = "completed", _("Завершено")
        REJECTED = "rejected", _("Відхилено")
        CANCELLED = "cancelled", _("Скасовано")

    client = models.ForeignKey(
        "management.IgClient",
        on_delete=models.DO_NOTHING,
        related_name="post_sale_cases",
        db_constraint=False,
    )
    order = models.ForeignKey(
        "orders.Order",
        null=True,
        blank=True,
        on_delete=models.DO_NOTHING,
        related_name="instagram_post_sale_cases",
        db_constraint=False,
    )
    commercial_episode = models.ForeignKey(
        "management.IgCommercialEpisode",
        null=True,
        blank=True,
        on_delete=models.DO_NOTHING,
        related_name="post_sale_cases",
        db_constraint=False,
    )
    source_message = models.OneToOneField(
        "management.InstagramBotMessage",
        on_delete=models.DO_NOTHING,
        related_name="post_sale_case",
        db_constraint=False,
    )
    case_type = models.CharField(max_length=16, choices=CaseType.choices, db_index=True)
    status = models.CharField(
        max_length=24,
        choices=Status.choices,
        default=Status.NEEDS_DETAILS,
        db_index=True,
    )
    source_item_title = models.CharField(max_length=255, blank=True, default="")
    source_fit = models.CharField(max_length=64, blank=True, default="")
    source_size = models.CharField(max_length=32, blank=True, default="")
    requested_fit = models.CharField(max_length=64, blank=True, default="")
    requested_size = models.CharField(max_length=32, blank=True, default="")
    reason = models.CharField(max_length=500, blank=True, default="")
    manager_note = models.TextField(blank=True, default="")
    evidence_message_ids = models.JSONField(default=list, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.DO_NOTHING,
        related_name="ig_post_sale_cases_created",
        db_constraint=False,
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = _("Післяпродажне звернення Instagram")
        verbose_name_plural = _("Післяпродажні звернення Instagram")
        ordering = ["-updated_at", "-id"]
        indexes = [
            models.Index(fields=["client", "status", "-updated_at"], name="ig_postsale_client_state"),
            models.Index(fields=["order", "-updated_at"], name="ig_postsale_order_dt"),
        ]


class _AppendOnlyOrderShipmentQuerySet(models.QuerySet):
    def update(self, **kwargs):
        forbidden = {
            "order",
            "order_id",
            "tracking_number",
            "direction",
            "purpose",
            "supersedes",
            "supersedes_id",
        }
        if forbidden.intersection(kwargs):
            raise ValueError("IgOrderShipment identity is immutable")
        return super().update(**kwargs)

    def delete(self):
        raise ValueError("IgOrderShipment is append-only")

    def _raw_delete(self, using):
        raise ValueError("IgOrderShipment is append-only")


class IgOrderShipment(models.Model):
    """Every parcel that ever moved for one order, in both directions.

    ``Order.tracking_number`` is a single scalar with no history anywhere in the
    project, so writing an exchange replacement into it used to erase the
    original number and with it the fact that there were two shipments.

    An exchange is one purchase and several parcels, which is why this is a
    journal on the order rather than a second order: a second order would double
    the revenue and ``purchases_count``.
    """

    class Direction(models.TextChoices):
        OUTBOUND = "outbound", _("Ми відправили")
        INBOUND = "inbound", _("Клієнт відправив нам")

    class Purpose(models.TextChoices):
        INITIAL = "initial", _("Перша відправка")
        EXCHANGE_REPLACEMENT = "exchange_replacement", _("Заміна відправлена")
        RETURN_INBOUND = "return_inbound", _("Повернення від клієнта")
        CORRECTION = "correction", _("Переоформлена ТТН")

    class Source(models.TextChoices):
        ORDER_FIELD = "order_field", _("Поле замовлення")
        MANAGER_MANUAL = "manager_manual", _("Введено менеджером")
        CUSTOMER_MESSAGE = "customer_message", _("З повідомлення клієнта")

    class Payer(models.TextChoices):
        SHOP = "shop", _("За наш рахунок")
        CUSTOMER = "customer", _("За рахунок клієнта")
        UNKNOWN = "unknown", _("Не визначено")

    order = models.ForeignKey(
        "orders.Order",
        on_delete=models.DO_NOTHING,
        db_constraint=False,
        related_name="instagram_shipments",
    )
    post_sale_case = models.ForeignKey(
        "management.IgPostSaleCase",
        null=True,
        blank=True,
        on_delete=models.DO_NOTHING,
        db_constraint=False,
        related_name="shipments",
    )
    tracking_number = models.CharField(max_length=64)
    direction = models.CharField(max_length=16, choices=Direction.choices, db_index=True)
    purpose = models.CharField(max_length=32, choices=Purpose.choices, db_index=True)
    supersedes = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.DO_NOTHING,
        db_constraint=False,
        related_name="superseded_by_shipments",
    )
    source = models.CharField(
        max_length=32, choices=Source.choices, default=Source.ORDER_FIELD
    )
    payer = models.CharField(
        max_length=16, choices=Payer.choices, default=Payer.SHOP
    )
    # A Nova Poshta "fast return" travels back on the SAME waybill as the
    # outbound parcel, and the customer pays nothing for it. One number in two
    # directions is therefore normal, not a data-entry mistake, and the flag
    # keeps the timeline from reading like a duplicate.
    reuses_outbound_tracking = models.BooleanField(default=False)
    evidence_message_id = models.PositiveBigIntegerField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        db_constraint=False,
        related_name="ig_order_shipments",
    )
    note = models.CharField(max_length=500, blank=True, default="")
    notified_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    objects = models.Manager.from_queryset(_AppendOnlyOrderShipmentQuerySet)()

    class Meta:
        verbose_name = _("Відправка замовлення Instagram")
        verbose_name_plural = _("Відправки замовлень Instagram")
        ordering = ["created_at", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["order", "tracking_number", "direction"],
                name="ig_shipment_once_per_direction",
            ),
        ]
        indexes = [
            models.Index(fields=["order", "created_at"], name="ig_shipment_order_dt"),
            models.Index(
                fields=["post_sale_case", "created_at"], name="ig_shipment_case_dt"
            ),
        ]

    def delete(self, *args, **kwargs):
        raise ValueError("IgOrderShipment is append-only")

    def __str__(self) -> str:  # pragma: no cover - тривіально
        return f"{self.tracking_number} ({self.purpose})"


class _AppendOnlyPromptRevisionQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValueError("BotPromptRevision is append-only")

    def delete(self):
        raise ValueError("BotPromptRevision is append-only")


class BotPromptRevision(models.Model):
    """Append-only history for instructions, live directives and core policy."""

    class Target(models.TextChoices):
        INSTRUCTION = "instruction", _("Інструкція бота")
        KNOWLEDGE_BASE = "knowledge_base", _("Оперативні директиви")
        SYSTEM_PROMPT = "system_prompt", _("Опубліковане ядро бота")

    class Kind(models.TextChoices):
        EDIT = "edit", _("Зміна")
        ROLLBACK = "rollback", _("Відкат")

    target = models.CharField(max_length=32, choices=Target.choices, db_index=True)
    target_id = models.PositiveIntegerField(null=True, blank=True, db_index=True)
    kind = models.CharField(max_length=16, choices=Kind.choices, default=Kind.EDIT)
    title = models.CharField(max_length=200, blank=True, default="")
    body = models.TextField(blank=True, default="")
    previous_body = models.TextField(blank=True, default="")
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        db_constraint=False,
        related_name="bot_prompt_revisions",
    )
    actor_label = models.CharField(max_length=150, blank=True, default="")
    note = models.CharField(max_length=500, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    objects = models.Manager.from_queryset(_AppendOnlyPromptRevisionQuerySet)()

    class Meta:
        verbose_name = _("Ревізія промпту бота")
        verbose_name_plural = _("Ревізії промпту бота")
        ordering = ["-id"]
        indexes = [
            models.Index(fields=["target", "target_id", "-id"], name="bot_prompt_rev_target"),
        ]

    def save(self, *args, **kwargs):
        if self.pk and not kwargs.get("force_insert"):
            raise ValueError("BotPromptRevision is append-only")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("BotPromptRevision is append-only")

    def diff_lines(self) -> list[str]:
        """Unified diff between the previous and the new body."""
        import difflib

        return list(
            difflib.unified_diff(
                (self.previous_body or "").splitlines(),
                (self.body or "").splitlines(),
                lineterm="",
                n=2,
            )
        )


class IgFunnelResetAudit(models.Model):
    """Immutable operator boundary between old CRM inference and a new test run."""

    client = models.ForeignKey(
        "management.IgClient",
        on_delete=models.DO_NOTHING,
        related_name="funnel_reset_audits",
        db_constraint=False,
    )
    reset_after_message_id = models.PositiveBigIntegerField(default=0, db_index=True)
    previous_state = models.JSONField(default=dict, blank=True)
    resulting_stage = models.CharField(max_length=24, blank=True, default="")
    reason = models.CharField(max_length=255)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        on_delete=models.SET_NULL,
        related_name="ig_funnel_reset_audits",
        db_constraint=False,
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = _("Скидання воронки Instagram")
        verbose_name_plural = _("Скидання воронок Instagram")
        ordering = ["-id"]
        indexes = [
            models.Index(
                fields=["client", "-created_at"],
                name="ig_funnel_reset_client_dt",
            ),
        ]


class _AppendOnlyCommercialEpisodeEventQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValueError("IgCommercialEpisodeEvent is append-only")

    def delete(self):
        raise ValueError("IgCommercialEpisodeEvent is append-only")


class IgCommercialEpisodeEvent(models.Model):
    """Append-only episode timeline for funnel, payment, order and shipment."""

    episode = models.ForeignKey(
        "management.IgCommercialEpisode",
        on_delete=models.DO_NOTHING,
        related_name="events",
        db_constraint=False,
    )
    dedupe_key = models.CharField(max_length=160, unique=True)
    event_type = models.CharField(max_length=40)
    from_state = models.CharField(max_length=32, blank=True, default="")
    to_state = models.CharField(max_length=32, blank=True, default="")
    stage = models.CharField(max_length=32, blank=True, default="")
    source = models.CharField(max_length=40, blank=True, default="")
    evidence = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    objects = models.Manager.from_queryset(_AppendOnlyCommercialEpisodeEventQuerySet)()

    class Meta:
        verbose_name = _("Подія комерційного епізоду Instagram")
        verbose_name_plural = _("Події комерційних епізодів Instagram")
        ordering = ["-id"]
        indexes = [
            models.Index(fields=["episode", "-created_at"], name="ig_episode_event_dt"),
        ]

    def save(self, *args, **kwargs):
        if self.pk and not kwargs.get("force_insert"):
            raise ValueError("IgCommercialEpisodeEvent is append-only")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("IgCommercialEpisodeEvent is append-only")


class _AppendOnlyFunnelStepEventQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValueError("IgFunnelStepEvent is append-only")

    def delete(self):
        raise ValueError("IgFunnelStepEvent is append-only")


class IgFunnelStepEvent(models.Model):
    """Immutable event-time fact for one commercial funnel episode."""

    class Type(models.TextChoices):
        CONVERSATION_STARTED = "conversation_started", _("Діалог розпочато")
        BOT_REPLIED_FIRST = "bot_replied_first", _("Перша відповідь бота")
        PRODUCT_PINNED = "product_pinned", _("Товар визначено")
        VARIANT_SELECTED = "variant_selected", _("Варіант визначено")
        PRICE_QUOTED = "price_quoted", _("Ціну названо")
        PAYLINK_ISSUED = "paylink_issued", _("Посилання на оплату видано")
        PAYLINK_VIEWED = "paylink_viewed", _("Посилання на оплату відкрито")
        PAYMENT_CONFIRMED = "payment_confirmed", _("Оплату підтверджено")
        OBJECTION_RAISED = "objection_raised", _("Заперечення зафіксовано")
        OBJECTION_HANDLED = "objection_handled", _("Заперечення опрацьовано")
        DISCOUNT_OFFERED = "discount_offered", _("Знижку запропоновано")
        MANAGER_ENGAGED = "manager_engaged", _("Менеджер долучився")
        ORDER_CREATED = "order_created", _("Замовлення створено")
        TTN_CREATED = "ttn_created", _("ТТН створено")
        DELIVERED = "delivered", _("Замовлення отримано")
        DROP_OFF = "drop_off", _("Відвал зафіксовано")
        RECOVERED = "recovered", _("Клієнт повернувся")

    episode = models.ForeignKey(
        "management.IgCommercialEpisode",
        on_delete=models.DO_NOTHING,
        related_name="funnel_step_events",
        db_constraint=False,
    )
    event_key = models.CharField(max_length=160, unique=True)
    event_type = models.CharField(max_length=32, choices=Type.choices, db_index=True)
    stage = models.CharField(max_length=32, blank=True, default="", db_index=True)
    actor = models.CharField(max_length=40, blank=True, default="")
    occurred_at = models.DateTimeField(db_index=True)
    evidence = models.JSONField(default=dict, blank=True)
    is_backfilled = models.BooleanField(default=False, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = models.Manager.from_queryset(_AppendOnlyFunnelStepEventQuerySet)()

    class Meta:
        ordering = ["occurred_at", "id"]
        indexes = [
            models.Index(fields=["episode", "occurred_at"], name="ig_fstep_episode_dt"),
            models.Index(fields=["event_type", "occurred_at"], name="ig_fstep_type_dt"),
        ]

    def save(self, *args, **kwargs):
        if self.pk and not kwargs.get("force_insert"):
            raise ValueError("IgFunnelStepEvent is append-only")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("IgFunnelStepEvent is append-only")


class IgFunnelDropOffQuerySet(models.QuerySet):
    _RECOVERY_FIELDS = {"recovered_at", "recovered_by_followup", "recovery_event"}

    def update(self, **kwargs):
        if not set(kwargs).issubset(self._RECOVERY_FIELDS):
            raise ValueError("IgFunnelDropOff identity is immutable")
        return super().update(**kwargs)

    def delete(self):
        raise ValueError("IgFunnelDropOff cannot be deleted")


class IgFunnelDropOff(models.Model):
    """Durable classified loss fact; recovery may close it exactly once."""

    class Kind(models.TextChoices):
        SILENCE = "silence", _("Мовчання")
        EXPLICIT_REFUSAL = "explicit_refusal", _("Явна відмова")
        OPT_OUT = "opt_out", _("Відмова від повідомлень")
        UNREACHABLE = "unreachable", _("Недоступний через доставку")
        SPAM = "spam", _("Спам")
        SUPERSEDED = "superseded", _("Цикл заміщено")

    episode = models.ForeignKey(
        "management.IgCommercialEpisode",
        on_delete=models.DO_NOTHING,
        related_name="drop_offs",
        db_constraint=False,
    )
    step_event = models.OneToOneField(
        "management.IgFunnelStepEvent",
        on_delete=models.DO_NOTHING,
        related_name="drop_off",
        db_constraint=False,
    )
    kind = models.CharField(max_length=24, choices=Kind.choices, db_index=True)
    reason_code = models.CharField(max_length=80, blank=True, default="", db_index=True)
    stage_at_drop = models.CharField(max_length=32, blank=True, default="", db_index=True)
    objection_at_drop = models.CharField(max_length=32, blank=True, default="")
    silence_hours = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    followups_sent_before = models.PositiveSmallIntegerField(default=0)
    detected_by = models.CharField(max_length=40, blank=True, default="")
    is_recoverable = models.BooleanField(default=False, db_index=True)
    occurred_at = models.DateTimeField(db_index=True)
    recovered_at = models.DateTimeField(null=True, blank=True, db_index=True)
    recovered_by_followup = models.BooleanField(default=False)
    recovery_event = models.ForeignKey(
        "management.IgFunnelStepEvent",
        null=True,
        blank=True,
        on_delete=models.DO_NOTHING,
        related_name="recovered_drop_offs",
        db_constraint=False,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    objects = IgFunnelDropOffQuerySet.as_manager()
    _IDENTITY_FIELDS = (
        "episode_id", "step_event_id", "kind", "reason_code", "stage_at_drop",
        "objection_at_drop", "silence_hours", "followups_sent_before",
        "detected_by", "is_recoverable", "occurred_at",
    )

    class Meta:
        ordering = ["-occurred_at", "-id"]
        indexes = [
            models.Index(fields=["episode", "-occurred_at"], name="ig_drop_episode_dt"),
            models.Index(fields=["kind", "recovered_at"], name="ig_drop_kind_recovery"),
        ]

    def save(self, *args, **kwargs):
        if self.pk:
            previous = type(self).objects.filter(pk=self.pk).values(
                *self._IDENTITY_FIELDS
            ).first()
            if previous:
                for field_name in self._IDENTITY_FIELDS:
                    if getattr(self, field_name) != previous[field_name]:
                        raise ValueError("IgFunnelDropOff identity is immutable")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("IgFunnelDropOff cannot be deleted")


class BotInstruction(models.Model):
    """Окрема інструкція для бота (нескінченна кількість, редагується в UI).

    Усі активні інструкції збираються в один блок і інжектяться в контекст
    Gemini поряд з базою знань. intent_tags — необов'язкові ключові слова, за
    якими в майбутньому можна підбирати релевантні інструкції під запит.
    """

    title = models.CharField(_("Заголовок"), max_length=200, blank=True, default="")
    body = models.TextField(_("Текст інструкції"))
    intent_tags = models.CharField(
        _("Ключові слова (через кому)"), max_length=400, blank=True, default=""
    )
    class Locale(models.TextChoices):
        ALL = "all", _("Усі мови")
        UK = "uk", _("Українська")
        RU = "ru", _("Російська")
        EN = "en", _("Англійська")

    class TrustScope(models.TextChoices):
        PUBLIC_POLICY = "public_policy", _("Публічна політика")
        OPERATOR_ONLY = "operator_only", _("Лише оператор")

    is_active = models.BooleanField(_("Активна"), default=True, db_index=True)
    priority = models.IntegerField(_("Пріоритет"), default=100)
    locale = models.CharField(
        max_length=8,
        choices=Locale.choices,
        default=Locale.ALL,
        db_default=Locale.ALL,
        db_index=True,
    )
    trigger_codes = models.JSONField(default=list, db_default=[], blank=True)
    programme_metadata = models.JSONField(default=dict, db_default={}, blank=True)
    allowed_actions = models.JSONField(default=list, db_default=[], blank=True)
    trust_scope = models.CharField(
        max_length=20,
        choices=TrustScope.choices,
        default=TrustScope.PUBLIC_POLICY,
        db_default=TrustScope.PUBLIC_POLICY,
        db_index=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Інструкція бота")
        verbose_name_plural = _("Інструкції бота")
        ordering = ["priority", "id"]

    def __str__(self) -> str:  # pragma: no cover - тривіально
        return self.title or (self.body[:50] if self.body else f"Instruction#{self.pk}")

    @classmethod
    def active_block(cls) -> str:
        """Текст усіх активних інструкцій (для інжекту в system_instruction)."""
        parts = []
        for inst in cls.objects.filter(is_active=True).order_by("priority", "id"):
            body = (inst.body or "").strip()
            if not body:
                continue
            title = (inst.title or "").strip()
            parts.append(f"• {title}: {body}" if title else f"• {body}")
        return "\n".join(parts)


class _AppendOnlyPolicyPublicationQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValueError("BotPolicyPublication is append-only")

    def delete(self):
        raise ValueError("BotPolicyPublication is append-only")


class BotPolicyPublication(models.Model):
    """Immutable complete publication of the editable instruction set."""

    class Kind(models.TextChoices):
        BOOTSTRAP = "bootstrap", _("Початковий знімок")
        PUBLISH = "publish", _("Публікація")
        ROLLBACK = "rollback", _("Відкат")

    version = models.PositiveBigIntegerField(unique=True)
    kind = models.CharField(max_length=12, choices=Kind.choices)
    parent = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="successor_publications",
    )
    restored_from = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="rollback_publications",
    )
    schema_version = models.PositiveSmallIntegerField(default=1)
    snapshot = models.JSONField(default=dict)
    snapshot_hash = models.CharField(max_length=64, db_index=True)
    compiler_version = models.CharField(max_length=32)
    instruction_count = models.PositiveIntegerField(default=0)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        db_constraint=False,
        related_name="bot_policy_publications",
    )
    actor_label = models.CharField(max_length=150, blank=True, default="")
    note = models.CharField(max_length=500, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    objects = models.Manager.from_queryset(_AppendOnlyPolicyPublicationQuerySet)()

    class Meta:
        ordering = ["-version", "-id"]
        indexes = [
            models.Index(fields=["kind", "-version"], name="bot_policy_kind_ver"),
        ]

    def save(self, *args, **kwargs):
        if self.pk and not kwargs.get("force_insert"):
            raise ValueError("BotPolicyPublication is append-only")
        if not self.pk:
            encoded = json.dumps(
                self.snapshot,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            if hashlib.sha256(encoded).hexdigest() != self.snapshot_hash:
                raise ValueError("BotPolicyPublication snapshot hash mismatch")
            instructions = (
                self.snapshot.get("instructions")
                if isinstance(self.snapshot, dict)
                else None
            )
            if (
                not isinstance(instructions, list)
                or len(instructions) != int(self.instruction_count)
            ):
                raise ValueError("BotPolicyPublication instruction count mismatch")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("BotPolicyPublication is append-only")


class BotQuickLink(models.Model):
    """Швидке посилання, яке бот може надіслати (розмірна сітка-хайлайт,
    каталог, тощо). garment_type дозволяє підібрати правильну розмірну сітку."""

    class Kind(models.TextChoices):
        SIZE_CHART = "size_chart", _("Розмірна сітка")
        CATALOG = "catalog", _("Каталог")
        HIGHLIGHT = "highlight", _("Хайлайт")
        OTHER = "other", _("Інше")

    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.OTHER, db_index=True)
    label = models.CharField(_("Підпис"), max_length=200)
    url = models.CharField(_("Посилання"), max_length=600)
    garment_type = models.CharField(
        _("Тип одягу (tshirt/hoodie/longsleeve…)"), max_length=40, blank=True, default="", db_index=True
    )
    trigger_keywords = models.CharField(
        _("Тригер-слова (через кому)"), max_length=400, blank=True, default=""
    )
    is_active = models.BooleanField(default=True, db_index=True)
    order = models.IntegerField(default=100)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Швидке посилання бота")
        verbose_name_plural = _("Швидкі посилання бота")
        ordering = ["order", "id"]

    def __str__(self) -> str:  # pragma: no cover - тривіально
        return f"{self.label} ({self.kind})"

    @classmethod
    def for_garment(cls, garment_type: str, kind: str | None = None):
        qs = cls.objects.filter(is_active=True)
        if kind:
            qs = qs.filter(kind=kind)
        if garment_type:
            qs = qs.filter(garment_type=garment_type)
        return qs.order_by("order", "id").first()

    @classmethod
    def active_block(cls) -> str:
        """Текст активних швидких посилань (інжектиться в контекст бота)."""
        lines = []
        for ln in cls.objects.filter(is_active=True).order_by("order", "id"):
            gt = f" [{ln.garment_type}]" if ln.garment_type else ""
            lines.append(f"• {ln.get_kind_display()}{gt}: {ln.label} — {ln.url}")
        return "\n".join(lines)


class BotAdCampaign(models.Model):
    """Мапінг рекламної кампанії (Click-to-IG-Direct) на товар/тему.

    Коли клієнт пише з реклами, referral дає ad_id/ref. Якщо ad_title загальний,
    цей мапінг каже боту, ЩО саме продавала реклама (товар або тема), щоб одразу
    вести по суті, а не питати «дайте фото».
    """

    ad_id = models.CharField(_("Ad ID"), max_length=64, blank=True, default="", db_index=True)
    ref = models.CharField(_("Ref"), max_length=255, blank=True, default="", db_index=True)
    title = models.CharField(_("Назва кампанії"), max_length=255, blank=True, default="")
    product = models.ForeignKey(
        "storefront.Product", null=True, blank=True, on_delete=models.SET_NULL
    )
    theme = models.CharField(_("Тема"), max_length=120, blank=True, default="")
    landing_note = models.TextField(_("Що в рекламі / CTA"), blank=True, default="")
    is_active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Рекламна кампанія бота")
        verbose_name_plural = _("Рекламні кампанії бота")
        ordering = ["-id"]

    def __str__(self) -> str:  # pragma: no cover - тривіально
        return self.title or self.ad_id or self.ref or f"AdCampaign#{self.pk}"

    @classmethod
    def match(cls, ad_id: str | None = None, ref: str | None = None):
        qs = cls.objects.filter(is_active=True)
        if ad_id:
            obj = qs.filter(ad_id=ad_id).first()
            if obj:
                return obj
        if ref:
            obj = qs.filter(ref=ref).first()
            if obj:
                return obj
        return None


class IgClientStageEvent(models.Model):
    """Подія зміни стадії воронки клієнта (для таймлайну/кружечків у картці)."""

    client = models.ForeignKey(
        "management.IgClient", on_delete=models.CASCADE, related_name="stage_events"
    )
    from_stage = models.CharField(max_length=24, blank=True, default="")
    to_stage = models.CharField(max_length=24)
    reason = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = _("Подія стадії IG-клієнта")
        verbose_name_plural = _("Події стадій IG-клієнтів")
        ordering = ["-id"]
        indexes = [
            models.Index(fields=["client", "-id"], name="ig_stageevent_client"),
        ]

    def __str__(self) -> str:  # pragma: no cover - тривіально
        return f"{self.client_id}: {self.from_stage}→{self.to_stage}"


class _IgFollowUpTaskQuerySet(models.QuerySet):
    _EVENT_BOUNDARY_FIELDS = {
        "event_key",
        "trigger",
        "event_occurred_at",
        "event_payload",
        "policy_started_at",
        "policy_version",
    }

    def update(self, **kwargs):
        if self._EVENT_BOUNDARY_FIELDS.intersection(kwargs):
            raise ValueError("IgFollowUpTask event boundary is immutable")
        return super().update(**kwargs)

    def bulk_update(self, objs, fields, batch_size=None):
        if self._EVENT_BOUNDARY_FIELDS.intersection(fields):
            raise ValueError("IgFollowUpTask event boundary is immutable")
        return super().bulk_update(objs, fields, batch_size=batch_size)


class IgFollowUpTask(models.Model):
    """Scheduled Instagram follow-up with Meta-window and quiet-hours guardrails."""

    class Status(models.TextChoices):
        PENDING = "pending", _("Очікує")
        PROCESSING = "processing", _("Надсилається")
        SENT = "sent", _("Надіслано")
        AMBIGUOUS = "ambiguous", _("Потрібна перевірка доставки")
        COMPLETED = "completed", _("Завершено")
        CANCELLED = "cancelled", _("Скасовано")
        SKIPPED = "skipped", _("Пропущено")

    class Kind(models.TextChoices):
        QUALIFICATION = "qualification", _("Уточнення")
        PAYMENT = "payment", _("Нагадування про оплату")
        THINKING = "thinking", _("Клієнт думає")
        RESCUE = "rescue", _("Rescue offer")
        FINAL = "final", _("Фінальний офер")
        FULFILLMENT = "fulfillment", _("Дані для виконання замовлення")
        MANAGER_TASK = "manager_task", _("Завдання менеджеру")

    class Trigger(models.TextChoices):
        TIME = "time", _("За часом")
        EVENT = "event", _("Подія")
        REACTIVE = "reactive", _("Реактивно")

    class ManagerApprovalStatus(models.TextChoices):
        NOT_REQUIRED = "not_required", _("Не потрібне")
        PENDING = "pending", _("Очікує рішення")
        APPROVED = "approved", _("Підтверджено")
        REJECTED = "rejected", _("Відхилено")

    client = models.ForeignKey(
        "management.IgClient", on_delete=models.CASCADE, related_name="followup_tasks"
    )
    deal = models.ForeignKey(
        "management.IgDeal", null=True, blank=True, on_delete=models.SET_NULL, related_name="followup_tasks"
    )
    due_at = models.DateTimeField(db_index=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING, db_index=True)
    kind = models.CharField(max_length=24, choices=Kind.choices, default=Kind.QUALIFICATION, db_index=True)
    level = models.PositiveSmallIntegerField(default=0)
    reason = models.CharField(max_length=120, blank=True, default="", db_index=True)
    discount_percent = models.PositiveSmallIntegerField(default=0)
    manager_approval_status = models.CharField(
        max_length=16,
        choices=ManagerApprovalStatus.choices,
        default=ManagerApprovalStatus.NOT_REQUIRED,
        db_index=True,
    )
    manager_approval_requested_at = models.DateTimeField(null=True, blank=True)
    manager_approval_decided_at = models.DateTimeField(null=True, blank=True)
    manager_approval_actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="ig_followup_approvals",
        db_constraint=False,
    )
    meta_window_deadline = models.DateTimeField(null=True, blank=True, db_index=True)
    message_text = models.TextField(blank=True, default="")
    # Mutable operator-only context for MANAGER_TASK business cases. Customer
    # follow-up copy remains in message_text and never receives this JSON.
    manager_context = models.JSONField(null=True, blank=True, default=None)
    # Durable event identity and two-phase worker claim.  A nullable unique
    # key lets ordinary time-based tasks coexist while event-triggered tasks
    # remain idempotent across daemon/cron/retry workers.
    event_key = models.CharField(max_length=180, null=True, blank=True, unique=True)
    trigger = models.CharField(
        max_length=16,
        choices=Trigger.choices,
        default=Trigger.TIME,
        db_index=True,
    )
    event_occurred_at = models.DateTimeField(null=True, blank=True, db_index=True)
    event_payload = models.JSONField(default=dict, blank=True)
    policy_started_at = models.DateTimeField(null=True, blank=True, db_index=True)
    policy_version = models.CharField(max_length=32, default="followup-v1")
    claim_token = models.CharField(max_length=64, blank=True, default="")
    claim_until = models.DateTimeField(null=True, blank=True, db_index=True)
    provider_message_id = models.CharField(max_length=255, blank=True, default="")
    sent_message = models.ForeignKey(
        "management.InstagramBotMessage",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    delivery_review_for = models.OneToOneField(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="delivery_review",
    )
    skip_reason = models.CharField(max_length=255, blank=True, default="")
    attempt_count = models.PositiveSmallIntegerField(default=0)
    next_attempt_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_error = models.CharField(max_length=500, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    objects = models.Manager.from_queryset(_IgFollowUpTaskQuerySet)()

    class Meta:
        verbose_name = _("IG follow-up")
        verbose_name_plural = _("IG follow-ups")
        ordering = ["due_at", "id"]
        indexes = [
            models.Index(fields=["status", "due_at"], name="ig_fu_status_due"),
            models.Index(fields=["client", "status"], name="ig_fu_client_status"),
            models.Index(fields=["kind", "status"], name="ig_fu_kind_status"),
            models.Index(fields=["trigger", "event_occurred_at"], name="ig_fu_trigger_event"),
        ]

    _EVENT_BOUNDARY_FIELDS = (
        "event_key",
        "trigger",
        "event_occurred_at",
        "event_payload",
        "policy_started_at",
        "policy_version",
    )

    def save(self, *args, **kwargs):
        if self.pk:
            previous = type(self).objects.filter(pk=self.pk).values(
                *self._EVENT_BOUNDARY_FIELDS
            ).first()
            if previous:
                for field_name in self._EVENT_BOUNDARY_FIELDS:
                    if getattr(self, field_name) != previous[field_name]:
                        raise ValidationError("IgFollowUpTask event boundary is immutable")
        return super().save(*args, **kwargs)

    def __str__(self) -> str:  # pragma: no cover - тривіально
        return f"FollowUp#{self.pk} {self.client_id} {self.kind}/{self.status}"


class IgPollCursor(models.Model):
    """Durable per-conversation cursor for the optional polling backstop."""

    conversation_id = models.CharField(max_length=255, unique=True)
    participant_igsid = models.CharField(max_length=64, blank=True, default="", db_index=True)
    provider_updated_at = models.DateTimeField(null=True, blank=True, db_index=True)
    synced_provider_updated_at = models.DateTimeField(null=True, blank=True)
    excluded_at = models.DateTimeField(null=True, blank=True, db_index=True)
    excluded_reason = models.CharField(max_length=32, blank=True, default="")
    failure_count = models.PositiveSmallIntegerField(default=0)
    next_attempt_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_error = models.CharField(max_length=80, blank=True, default="")
    last_message_id = models.CharField(max_length=255, blank=True, default="")
    last_message_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "IG polling cursor"
        verbose_name_plural = "IG polling cursors"

    def __str__(self) -> str:  # pragma: no cover - trivial representation
        return f"IgPollCursor({self.conversation_id})"


class IgInboxRefreshRun(models.Model):
    """Durable administrator-requested recovery of Meta-readable messages."""

    class Status(models.TextChoices):
        QUEUED = "queued", _("У черзі")
        DISCOVERING = "discovering", _("Пошук переписок")
        RUNNING = "running", _("Оновлення повідомлень")
        CANCELLING = "cancelling", _("Скасування")
        COMPLETED = "completed", _("Завершено")
        COMPLETED_ERRORS = "completed_errors", _("Завершено з помилками")
        CANCELLED = "cancelled", _("Скасовано")
        FAILED = "failed", _("Помилка")

    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="ig_inbox_refresh_runs",
        db_constraint=False,
    )
    provider_owner_id = models.CharField(max_length=128, db_index=True)
    transport = models.CharField(max_length=32, default="instagram_login")
    # MariaDB permits multiple NULLs but only one value=1 per provider owner.
    open_slot = models.PositiveSmallIntegerField(null=True, blank=True, default=1)
    status = models.CharField(
        max_length=24,
        choices=Status.choices,
        default=Status.QUEUED,
        db_index=True,
    )
    recovery_cutoff = models.DateTimeField(db_index=True)
    discovery_cursor = models.TextField(blank=True, default="")
    discovery_pages_seen = models.PositiveSmallIntegerField(default=0)
    discovery_complete = models.BooleanField(default=False)
    lease_token = models.CharField(max_length=64, blank=True, default="")
    lease_until = models.DateTimeField(null=True, blank=True, db_index=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    next_attempt_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_error = models.CharField(max_length=1000, blank=True, default="")
    cancel_requested_at = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Оновлення Instagram inbox")
        verbose_name_plural = _("Оновлення Instagram inbox")
        ordering = ["-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["provider_owner_id", "open_slot"],
                name="ig_refresh_one_open_owner",
            ),
            models.CheckConstraint(
                condition=models.Q(open_slot__isnull=True) | models.Q(open_slot=1),
                name="ig_refresh_open_null_one",
            ),
        ]
        indexes = [
            models.Index(
                fields=["status", "next_attempt_at", "id"],
                name="ig_refresh_run_due",
            ),
            models.Index(
                fields=["provider_owner_id", "-created_at"],
                name="ig_refresh_owner_dt",
            ),
        ]


class IgInboxRefreshItem(models.Model):
    """One conversation in a durable inbox recovery run."""

    class Status(models.TextChoices):
        PENDING = "pending", _("Очікує")
        PROCESSING = "processing", _("Обробка")
        DONE = "done", _("Готово")
        SKIPPED = "skipped", _("Пропущено")
        FAILED = "failed", _("Помилка")
        CANCELLED = "cancelled", _("Скасовано")

    run = models.ForeignKey(
        "management.IgInboxRefreshRun",
        on_delete=models.CASCADE,
        related_name="items",
        db_constraint=False,
    )
    conversation_id = models.CharField(max_length=255)
    participant_igsid = models.CharField(max_length=64, blank=True, default="", db_index=True)
    client = models.ForeignKey(
        "management.IgClient",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="inbox_refresh_items",
        db_constraint=False,
    )
    provider_updated_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    skip_reason = models.CharField(max_length=64, blank=True, default="")
    attempts = models.PositiveSmallIntegerField(default=0)
    next_attempt_at = models.DateTimeField(null=True, blank=True, db_index=True)
    lease_token = models.CharField(max_length=64, blank=True, default="")
    lease_until = models.DateTimeField(null=True, blank=True, db_index=True)
    messages_seen = models.PositiveSmallIntegerField(default=0)
    messages_created = models.PositiveSmallIntegerField(default=0)
    messages_existing = models.PositiveSmallIntegerField(default=0)
    messages_after_cutoff = models.PositiveSmallIntegerField(default=0)
    analysis_watermark_message_id = models.PositiveBigIntegerField(default=0)
    history_cursor = models.TextField(blank=True, default="")
    history_complete = models.BooleanField(default=False)
    truncated_reason = models.CharField(max_length=64, blank=True, default="")
    provider_http_status = models.PositiveSmallIntegerField(null=True, blank=True)
    last_error = models.CharField(max_length=1000, blank=True, default="")
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Переписка оновлення Instagram inbox")
        verbose_name_plural = _("Переписки оновлення Instagram inbox")
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(
                fields=["run", "conversation_id"],
                name="ig_refresh_item_run_conv",
            ),
        ]
        indexes = [
            models.Index(
                fields=["status", "next_attempt_at", "id"],
                name="ig_refresh_item_due",
            ),
            models.Index(
                fields=["run", "status", "id"],
                name="ig_refresh_item_run",
            ),
            models.Index(
                fields=["participant_igsid", "status"],
                name="ig_refresh_item_user",
            ),
        ]


class IgConversationSignal(models.Model):
    """Classified sales signal extracted from client/bot/manager conversation."""

    class Type(models.TextChoices):
        PRICE_OBJECTION = "price_objection", _("Дорого")
        PREPAYMENT_OBJECTION = "prepayment_objection", _("Передоплата")
        SIZE_CONCERN = "size_concern", _("Розмір")
        THINKING_OBJECTION = "thinking_objection", _("Подумаю")
        GIFT = "gift", _("На подарунок")
        SELF_PURCHASE = "self_purchase", _("Для себе")
        CUSTOM_PRINT = "custom_print", _("Кастомний принт")
        AD_REPLY = "ad_reply", _("Відповідь з реклами")
        NO_REPLY = "no_reply", _("Не відповідає")
        CHECKOUT_STARTED = "checkout_started", _("Checkout started")
        PAYMENT_PENDING = "payment_pending", _("Очікує оплату")
        PAID = "paid", _("Оплачено")
        LOST = "lost", _("Втрачено")
        SPAM = "spam", _("Спам")
        MANAGER_TAKEOVER = "manager_takeover", _("Взяв менеджер")
        DISCOUNT_OFFER = "discount_offer", _("Знижка")
        PRODUCT_INTEREST = "product_interest", _("Інтерес до товару")

    client = models.ForeignKey(
        "management.IgClient", on_delete=models.CASCADE, related_name="conversation_signals"
    )
    message = models.ForeignKey(
        "management.InstagramBotMessage",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="conversation_signals",
    )
    signal_type = models.CharField(max_length=40, choices=Type.choices, db_index=True)
    confidence = models.DecimalField(max_digits=4, decimal_places=2, default=Decimal("1.00"))
    value = models.CharField(max_length=255, blank=True, default="")
    payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = _("IG conversation signal")
        verbose_name_plural = _("IG conversation signals")
        ordering = ["-id"]
        indexes = [
            models.Index(fields=["client", "-id"], name="ig_sig_client_id"),
            models.Index(fields=["signal_type", "-id"], name="ig_sig_type_id"),
        ]

    def __str__(self) -> str:  # pragma: no cover - тривіально
        return f"{self.client_id}: {self.signal_type}"


class IgObjection(models.Model):
    """One durable objection lifecycle inside a commercial episode."""

    class Type(models.TextChoices):
        PRICE = "price", _("Дорого")
        THINKING = "thinking", _("Подумаю")
        SIZE_RISK = "size_risk", _("Не підійде розмір")
        PREPAYMENT_TRUST = "prepayment_trust", _("Не довіряє передоплаті")
        DEFECT_RISK = "defect_risk", _("Боїться браку")
        DELIVERY_TIME = "delivery_time", _("Довго чекати")
        CHEAPER_ELSEWHERE = "cheaper_elsewhere", _("Є дешевше")
        PRINT_QUALITY = "print_quality", _("Якість принта")
        OUT_OF_STOCK = "out_of_stock", _("Немає розміру/варіанта")
        PAYDAY = "payday", _("Після зарплати")
        COMPARE_BRAND = "compare_brand", _("Порівнює з брендом")
        ASK_PARTNER = "ask_partner", _("Порадиться з близькою людиною")

    class State(models.TextChoices):
        OPEN = "open", _("Відкрите")
        HANDLED = "handled", _("Метод застосовано")
        RESOLVED = "resolved", _("Закрито фактом")
        ABANDONED = "abandoned", _("Втрачено")

    class Outcome(models.TextChoices):
        UNRESOLVED = "unresolved", _("Не вирішено")
        PURCHASED = "purchased", _("Купив")
        LOST = "lost", _("Втрачено")
        SILENT = "silent", _("Замовк")
        MANAGER_TAKEN = "manager_taken", _("Передано менеджеру")

    client = models.ForeignKey(
        "management.IgClient", on_delete=models.DO_NOTHING,
        related_name="objection_lifecycles", db_constraint=False,
    )
    episode = models.ForeignKey(
        "management.IgCommercialEpisode", null=True, blank=True,
        on_delete=models.DO_NOTHING, related_name="objections", db_constraint=False,
    )
    objection_type = models.CharField(max_length=32, choices=Type.choices, db_index=True)
    state = models.CharField(max_length=16, choices=State.choices, default=State.OPEN, db_index=True)
    is_true_objection = models.BooleanField(default=True)
    first_message = models.ForeignKey(
        "management.InstagramBotMessage", null=True, blank=True,
        on_delete=models.DO_NOTHING, related_name="opened_objections", db_constraint=False,
    )
    last_message = models.ForeignKey(
        "management.InstagramBotMessage", null=True, blank=True,
        on_delete=models.DO_NOTHING, related_name="latest_objections", db_constraint=False,
    )
    repeat_count = models.PositiveIntegerField(default=1)
    attempts_count = models.PositiveIntegerField(default=0)
    resolution_method = models.CharField(max_length=48, blank=True, default="")
    outcome = models.CharField(max_length=24, choices=Outcome.choices, default=Outcome.UNRESOLVED)
    readiness_before = models.PositiveSmallIntegerField(default=0)
    readiness_after = models.PositiveSmallIntegerField(default=0)
    opened_watermark_message_id = models.PositiveBigIntegerField(default=0, db_index=True)
    dedupe_key = models.CharField(max_length=160, unique=True)
    opened_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-updated_at", "-id"]
        indexes = [
            models.Index(fields=["client", "state", "-id"], name="ig_obj_client_state"),
            models.Index(fields=["objection_type", "state"], name="ig_obj_type_state"),
        ]


class IgObjectionAttempt(models.Model):
    """Append-only claimed objection-handling method and observed result."""

    class Result(models.TextChoices):
        PENDING = "pending", _("Очікує реакції")
        ACCEPTED = "accepted", _("Прийнято")
        RE_OBJECTED = "re_objected", _("Повторив заперечення")
        SILENT = "silent", _("Без відповіді")
        ESCALATED = "escalated", _("Передано менеджеру")
        PURCHASED = "purchased", _("Купив")
        IGNORED = "ignored", _("Метод не застосовано")

    objection = models.ForeignKey(
        "management.IgObjection", on_delete=models.DO_NOTHING,
        related_name="attempts", db_constraint=False,
    )
    method = models.CharField(max_length=48, default="none", db_index=True)
    claimed_by = models.CharField(max_length=24, default="model")
    verified = models.BooleanField(default=False, db_index=True)
    verification_reason = models.CharField(max_length=255, blank=True, default="")
    reply_message = models.OneToOneField(
        "management.InstagramBotMessage", null=True, blank=True,
        on_delete=models.DO_NOTHING, related_name="objection_attempts", db_constraint=False,
    )
    client_response_message = models.ForeignKey(
        "management.InstagramBotMessage", null=True, blank=True,
        on_delete=models.DO_NOTHING, related_name="objection_responses", db_constraint=False,
    )
    result = models.CharField(max_length=24, choices=Result.choices, default=Result.PENDING)
    readiness_before = models.PositiveSmallIntegerField(default=0)
    readiness_after = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-id"]
        indexes = [models.Index(fields=["objection", "-id"], name="ig_obj_attempt_dt")]


class _IgAnalysisMaterialityEventQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValueError("IgAnalysisMaterialityEvent is append-only")

    def delete(self):
        raise ValueError("IgAnalysisMaterialityEvent is append-only")


class IgAnalysisMaterialityEvent(models.Model):
    """PII-free evidence that a durable CRM analysis may be stale."""

    class Kind(models.TextChoices):
        CUSTOMER_TURN = "customer_turn", _("Завершений хід клієнта")
        PAYMENT_TRUTH = "payment_truth", _("Зміна істини оплати")
        ORDER_TRUTH = "order_truth", _("Зміна істини замовлення")
        MANAGER_BOUNDARY = "manager_boundary", _("Межа менеджера")
        PRODUCT_LINE = "product_line", _("Зміна товарної лінії")
        DEFERRED_INTENT = "deferred_intent", _("Відкладений намір")
        MEDIA_ARTIFACT = "media_artifact", _("Новий media artifact")

    class SourceRole(models.TextChoices):
        USER = "user", _("Клієнт")
        MANAGER = "manager", _("Менеджер")
        AUTHORITY = "authority", _("Авторитетний backend")
        SYSTEM = "system", _("Система")

    client = models.ForeignKey(
        "management.IgClient",
        on_delete=models.DO_NOTHING,
        related_name="analysis_materiality_events",
        db_constraint=False,
    )
    episode = models.ForeignKey(
        "management.IgCommercialEpisode",
        null=True,
        blank=True,
        on_delete=models.DO_NOTHING,
        related_name="analysis_materiality_events",
        db_constraint=False,
    )
    line_id = models.CharField(max_length=96, blank=True, default="")
    customer_turn = models.ForeignKey(
        "management.IgCustomerTurn",
        null=True,
        blank=True,
        on_delete=models.DO_NOTHING,
        related_name="analysis_materiality_events",
        db_constraint=False,
    )
    source_message = models.ForeignKey(
        "management.InstagramBotMessage",
        null=True,
        blank=True,
        on_delete=models.DO_NOTHING,
        related_name="analysis_materiality_events",
        db_constraint=False,
    )
    source_role = models.CharField(max_length=16, choices=SourceRole.choices)
    event_kind = models.CharField(max_length=32, choices=Kind.choices)
    event_key = models.CharField(max_length=160)
    event_digest = models.CharField(max_length=64)
    authority_digest = models.CharField(max_length=64, blank=True, default="")
    artifact_revision = models.PositiveBigIntegerField(default=0)
    artifact_digest = models.CharField(max_length=64, blank=True, default="")
    relevant_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(
                fields=["event_key"],
                name="ig_mat_event_key_unique",
            ),
        ]
        indexes = [
            models.Index(fields=["client", "-id"], name="ig_mat_client_id"),
            models.Index(fields=["episode", "-id"], name="ig_mat_episode_id"),
            models.Index(fields=["event_kind", "-id"], name="ig_mat_kind_id"),
            models.Index(fields=["relevant_at", "id"], name="ig_mat_relevant"),
        ]

    objects = models.Manager.from_queryset(_IgAnalysisMaterialityEventQuerySet)()

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValueError("IgAnalysisMaterialityEvent is append-only")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("IgAnalysisMaterialityEvent is append-only")


class IgConversationAnalysisSnapshot(models.Model):
    """Versioned, evidence-bound interpretation of one conversation watermark."""

    class Band(models.TextChoices):
        COLD = "cold", _("Холодний")
        EXPLORING = "exploring", _("Вивчає")
        QUALIFIED = "qualified", _("Кваліфікований")
        HIGH_INTENT = "high_intent", _("Високий намір")
        CHECKOUT = "checkout", _("Оформлення")
        PAID = "paid", _("Оплачено")
        LOST = "lost", _("Втрачено")
        OPTED_OUT = "opted_out", _("Відмовився від повідомлень")

    class InteractionType(models.TextChoices):
        UNKNOWN = "unknown", _("Невідомо")
        REACTION_ONLY = "reaction_only", _("Лише реакція")
        INFORMATION_ONLY = "information_only", _("Лише інформація")
        PRODUCT_INTEREST = "product_interest", _("Інтерес до товару")
        SIZE_FIT_QUESTION = "size_fit_question", _("Питання про розмір")
        CUSTOM_PRINT = "custom_print", _("Кастомний принт")
        PRICE_OBJECTION = "price_objection", _("Заперечення щодо ціни")
        HIGH_INTENT = "high_intent", _("Високий намір")
        PAYMENT_PENDING = "payment_pending", _("Очікує оплату")
        PAID_ORDER_WAITING = "paid_order_waiting", _("Оплачено / очікує товар")
        NO_REPLY = "no_reply", _("Не відповідає")
        EXPLICIT_NO_BUY = "explicit_no_buy", _("Явно не купує")
        OPT_OUT = "opt_out", _("Відмовився від повідомлень")
        SPAM_ABUSE = "spam_abuse", _("Спам / образи")
        MANAGER_OBSERVATION = "manager_observation", _("Спостереження менеджера")
        COLLABORATION = "collaboration", _("Співпраця / creator")
        WHOLESALE_B2B = "wholesale_b2b", _("Опт / B2B")
        SUPPORT_COMPLAINT = "support_complaint", _("Підтримка / скарга")
        # A size exchange is neither a complaint nor an objection to buying: it
        # is a service case on top of a purchase that already happened. Keeping
        # it inside SUPPORT_COMPLAINT made a satisfied customer read as an
        # unhappy one (F-SCORE-002).
        EXCHANGE_REQUEST = "exchange_request", _("Обмін товару")
        RETURN_REQUEST = "return_request", _("Повернення товару")
        COMMUNITY_CASUAL = "community_casual", _("Спільнота / casual")

    client = models.ForeignKey(
        "management.IgClient",
        on_delete=models.CASCADE,
        related_name="analysis_snapshots",
        # IgClient is a legacy MyISAM table in production; keep this new
        # InnoDB snapshot table valid without a cross-engine FK constraint.
        db_constraint=False,
    )
    last_analyzed_message = models.ForeignKey(
        "management.InstagramBotMessage",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="analysis_snapshots",
        db_constraint=False,
    )
    dedupe_key = models.CharField(max_length=160, unique=True)
    score_band = models.CharField(max_length=24, choices=Band.choices, db_index=True)
    interaction_type = models.CharField(
        max_length=32,
        choices=InteractionType.choices,
        default=InteractionType.UNKNOWN,
        db_index=True,
    )
    purchase_probability = models.DecimalField(
        max_digits=5, decimal_places=4, default=Decimal("0.0000")
    )
    confidence = models.DecimalField(
        max_digits=5, decimal_places=4, default=Decimal("0.0000")
    )
    evidence = models.JSONField(default=list, blank=True)
    uncertainties = models.JSONField(default=list, blank=True)
    repeat_intent = models.JSONField(default=dict, blank=True)
    commercial_episode = models.ForeignKey(
        "management.IgCommercialEpisode",
        null=True,
        blank=True,
        on_delete=models.DO_NOTHING,
        related_name="analysis_snapshots",
        db_constraint=False,
    )
    analysis_model = models.CharField(max_length=80, blank=True, default="rules")
    analysis_prompt_version = models.CharField(max_length=40, blank=True, default="")
    required_state_fingerprint = models.CharField(max_length=64, blank=True, default="")
    rules_version = models.CharField(max_length=40, blank=True, default="")
    key_alias = models.CharField(max_length=32, blank=True, default="")
    reasoning_task = models.CharField(max_length=64, blank=True, default="")
    reasoning_level = models.CharField(max_length=16, blank=True, default="")
    reasoning_policy_version = models.CharField(max_length=32, blank=True, default="")
    thoughts_tokens = models.PositiveIntegerField(default=0)
    candidates_tokens = models.PositiveIntegerField(default=0)
    trigger = models.CharField(max_length=32, blank=True, default="message", db_index=True)
    analysis_latency_ms = models.PositiveIntegerField(default=0)
    analyzed_at = models.DateTimeField(default=timezone.now, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = _("Знімок аналізу IG-діалогу")
        verbose_name_plural = _("Знімки аналізу IG-діалогів")
        ordering = ["-id"]
        indexes = [
            models.Index(fields=["client", "-id"], name="ig_analysis_client_id"),
            models.Index(fields=["score_band", "-id"], name="ig_analysis_band_id"),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial representation
        return f"{self.client_id}: {self.score_band} ({self.purchase_probability})"


_ANALYSIS_V2_CODE_RE = re.compile(r"^[a-z0-9_.:+-]{0,160}$")
_ANALYSIS_V2_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
_ANALYSIS_V2_VERSION_TOKEN_RE = re.compile(r"^[a-z0-9][a-z0-9_.:+-]*$")
_ANALYSIS_V2_VERSION_MARKER_RE = re.compile(
    r"(?:^|[._-])v[0-9]+(?:$|[._-])"
)
_ANALYSIS_V2_PROJECT_SLOTS = frozenset({
    "", "gslot_7f3a", "gslot_c921", "gslot_18de",
    "gslot_a604", "gslot_52bb", "gslot_e17c",
})
_ANALYSIS_V2_CLAIM_CODES = frozenset({
    "interaction", "purchase_intent", "objection", "deferred_intent",
    "repeat_intent", "injection_risk", "conflict",
    "explicit_no_buy", "opt_out",
})
_ANALYSIS_V2_CONFLICT_CODES = frozenset({
    "product_conflict", "recipient_conflict", "line_conflict",
    "payment_claim_conflict", "manager_customer_conflict",
    "artifact_conflict", "live_agent_inconsistency",
})
_ANALYSIS_V2_UNCERTAINTY_CODES = frozenset({
    "analysis_v2_missing", "custom_print_user_evidence_missing",
    "deferred_evidence_missing", "evidence_unverified", "injection_signal",
    "manager_evidence_not_customer_intent", "payment_unverified",
    "probability_evidence_missing", "product", "size",
})
_ANALYSIS_V2_REPEAT_KINDS = frozenset({
    "", "explicit_more", "reorder", "gift", "another_recipient",
})
_ANALYSIS_V2_DECISION_CODES = frozenset({
    "", "shadow_valid", "funnel_registry_missing", "legacy_repeat_event_owner",
    "not_pending", "result_missing", "legacy_snapshot_scope_mismatch",
    "client_mismatch", "episode_mismatch", "line_mismatch",
    "result_digest_invalid", "result_digest_mismatch", "proposal_key_invalid",
    "materiality_digest_mismatch", "authority_digest_mismatch",
    "state_correlation_mismatch", "client_hidden", "client_blocked",
    "client_opted_out", "manager_takeover", "analysis_job_missing",
    "analysis_state_superseded", "state_correlation_stale",
    "current_episode_changed", "invalid_evidence_id", "evidence_missing",
    "evidence_not_current_or_owned", "evidence_not_customer_owned",
    "invalid_probability_value", "invalid_objection_type",
    "invalid_deferred_kind", "invalid_clarification_codes",
    "unsupported_proposal_type",
})


def _validate_positive_message_ids(values, *, field_name):
    if (
        not isinstance(values, list)
        or len(values) > 40
        or len(values) != len(set(values))
        or any(not isinstance(value, int) or value <= 0 for value in values)
    ):
        raise ValidationError({field_name: "Only unique bounded message identities are allowed."})


def _validate_hex(value, *, field_name, optional=False):
    normalized = str(value or "")
    if optional and not normalized:
        return
    if not _ANALYSIS_V2_HEX_RE.fullmatch(normalized):
        raise ValidationError({field_name: "Expected a 64-character hexadecimal identity."})


def _validate_finite_01(value, *, field_name, optional=False):
    if value is None and optional:
        return
    try:
        parsed = Decimal(str(value))
    except Exception as exc:
        raise ValidationError({field_name: "Expected a finite 0..1 value."}) from exc
    if not parsed.is_finite() or not Decimal("0") <= parsed <= Decimal("1"):
        raise ValidationError({field_name: "Expected a finite 0..1 value."})


def _validate_analysis_version_token(value, *, field_name, maximum):
    """Accept future version tokens without accepting arbitrary free text."""
    normalized = str(value or "")
    if not normalized:
        return
    if (
        len(normalized) > maximum
        or not _ANALYSIS_V2_VERSION_TOKEN_RE.fullmatch(normalized)
        or not _ANALYSIS_V2_VERSION_MARKER_RE.search(normalized)
    ):
        raise ValidationError({field_name: "Expected a bounded version token."})


def _validate_analysis_v2_codes(values, *, field_name):
    if not isinstance(values, list) or any(
        not isinstance(value, str)
        or not _ANALYSIS_V2_CODE_RE.fullmatch(value)
        for value in values
    ):
        raise ValidationError({field_name: "Only bounded typed codes are allowed."})


def _validate_analysis_result_payload(result):
    manifest = result.evidence_manifest
    if not isinstance(manifest, list):
        raise ValidationError({"evidence_manifest": "Evidence manifest must be a list."})
    allowed_roles = {"user", "manager", "model", "system"}
    allowed_keys = {"message_id", "source_role", "claim_codes"}
    for row in manifest:
        if not isinstance(row, dict) or set(row) - allowed_keys:
            raise ValidationError({"evidence_manifest": "Raw evidence fields are forbidden."})
        if not isinstance(row.get("message_id"), int) or row.get("message_id", 0) <= 0:
            raise ValidationError({"evidence_manifest": "Invalid message identity."})
        if row.get("source_role") not in allowed_roles:
            raise ValidationError({"evidence_manifest": "Invalid source role."})
        claim_codes = row.get("claim_codes")
        _validate_analysis_v2_codes(claim_codes, field_name="evidence_manifest")
        if any(code not in _ANALYSIS_V2_CLAIM_CODES for code in claim_codes):
            raise ValidationError({"evidence_manifest": "Unknown claim code."})
    if len(manifest) > 40 or len({row["message_id"] for row in manifest}) != len(manifest):
        raise ValidationError({"evidence_manifest": "Evidence identities must be unique and bounded."})
    customer_rows = [row for row in manifest if row["source_role"] == "user"]
    manager_rows = [row for row in manifest if row["source_role"] == "manager"]
    if result.customer_evidence_count != len(customer_rows):
        raise ValidationError({"customer_evidence_count": "Customer evidence count mismatch."})
    if result.manager_evidence_count != len(manager_rows):
        raise ValidationError({"manager_evidence_count": "Manager evidence count mismatch."})
    if result.authority_evidence_count not in {0, 1}:
        raise ValidationError({"authority_evidence_count": "Authority evidence count must be 0 or 1."})
    _validate_analysis_v2_codes(result.conflict_codes, field_name="conflict_codes")
    if any(code not in _ANALYSIS_V2_CONFLICT_CODES for code in result.conflict_codes):
        raise ValidationError({"conflict_codes": "Unknown conflict code."})
    _validate_analysis_v2_codes(result.uncertainty_codes, field_name="uncertainty_codes")
    if any(code not in _ANALYSIS_V2_UNCERTAINTY_CODES for code in result.uncertainty_codes):
        raise ValidationError({"uncertainty_codes": "Unknown uncertainty code."})
    _validate_positive_message_ids(
        result.injection_evidence_message_ids,
        field_name="injection_evidence_message_ids",
    )
    for field_name in (
        "materiality_digest", "state_correlation", "result_digest",
    ):
        _validate_hex(getattr(result, field_name), field_name=field_name)
    for field_name in ("authority_digest", "artifact_digest"):
        _validate_hex(getattr(result, field_name), field_name=field_name, optional=True)
    if not re.fullmatch(r"analysis-v2:[0-9a-f]{64}", str(result.result_key or "")):
        raise ValidationError({"result_key": "Invalid Analysis V2 result identity."})
    if result.result_schema_version not in {"analysis-v2.1", "analysis-v2.2"}:
        raise ValidationError({"result_schema_version": "Unsupported result schema."})
    if result.normalizer_version != "analysis-v2-normalizer.1":
        raise ValidationError({"normalizer_version": "Unsupported normalizer."})
    if result.source_kind not in {"ai", "rules"}:
        raise ValidationError({"source_kind": "Unsupported source kind."})
    if result.interaction_type not in IgConversationAnalysisSnapshot.InteractionType.values:
        raise ValidationError({"interaction_type": "Unsupported interaction type."})
    if result.score_band not in IgConversationAnalysisSnapshot.Band.values:
        raise ValidationError({"score_band": "Unsupported score band."})
    if result.detected_language not in {"", "uk", "ru", "en", "mixed", "unknown"}:
        raise ValidationError({"detected_language": "Unsupported language code."})
    _validate_positive_message_ids(
        result.language_evidence_message_ids,
        field_name="language_evidence_message_ids",
    )
    if result.probability_basis not in {
        "customer_evidence", "deterministic_no_buy", "deterministic_opt_out",
        "insufficient_evidence",
    }:
        raise ValidationError({"probability_basis": "Unsupported probability basis."})
    _validate_finite_01(result.purchase_probability, field_name="purchase_probability", optional=True)
    _validate_finite_01(result.purchase_confidence, field_name="purchase_confidence", optional=True)
    _validate_finite_01(
        result.active_objection_confidence,
        field_name="active_objection_confidence",
        optional=True,
    )
    _validate_finite_01(
        result.repeat_intent_confidence,
        field_name="repeat_intent_confidence",
        optional=True,
    )
    if result.probability_basis == "insufficient_evidence" and (
        result.purchase_probability is not None or result.purchase_confidence is not None
    ):
        raise ValidationError({"probability_basis": "Insufficient evidence must remain nullable."})
    if result.probability_basis in {"customer_evidence", "deterministic_no_buy", "deterministic_opt_out"} and result.customer_evidence_count <= 0:
        raise ValidationError({"customer_evidence_count": "Customer evidence is required."})
    user_claims = {
        code for row in customer_rows for code in row.get("claim_codes", [])
    }
    user_claim_ids = {
        code: {
            row["message_id"]
            for row in customer_rows
            if code in row.get("claim_codes", [])
        }
        for code in _ANALYSIS_V2_CLAIM_CODES
    }
    if result.result_schema_version == "analysis-v2.2" and (
        bool(result.detected_language) != bool(result.language_evidence_message_ids)
    ):
        raise ValidationError({"detected_language": "Language and user evidence must agree."})
    if (
        result.result_schema_version == "analysis-v2.1"
        and result.language_evidence_message_ids
    ):
        raise ValidationError({"language_evidence_message_ids": "V2.1 has no language-evidence field."})
    required_claim = {
        "customer_evidence": "purchase_intent",
        "deterministic_no_buy": "explicit_no_buy",
        "deterministic_opt_out": "opt_out",
    }.get(result.probability_basis)
    if required_claim and required_claim not in user_claims:
        raise ValidationError({"probability_basis": "Claim-specific user evidence is required."})
    if result.probability_basis == "customer_evidence" and result.purchase_probability is None:
        raise ValidationError({"purchase_probability": "Customer evidence requires a probability."})
    if result.probability_basis in {"deterministic_no_buy", "deterministic_opt_out"} and (
        result.purchase_probability != Decimal("0")
        or result.purchase_confidence != Decimal("1")
    ):
        raise ValidationError({"purchase_probability": "Deterministic terminal intent must be zero with full confidence."})
    deterministic_interaction = {
        "deterministic_no_buy": IgConversationAnalysisSnapshot.InteractionType.EXPLICIT_NO_BUY,
        "deterministic_opt_out": IgConversationAnalysisSnapshot.InteractionType.OPT_OUT,
    }.get(result.probability_basis)
    if deterministic_interaction and result.interaction_type != deterministic_interaction:
        raise ValidationError({"interaction_type": "Deterministic terminal intent type mismatch."})
    if result.active_objection_type not in {"", *IgObjection.Type.values}:
        raise ValidationError({"active_objection_type": "Unsupported objection type."})
    if result.active_objection_type and not user_claim_ids["objection"]:
        raise ValidationError({"active_objection_type": "Objection requires claim-specific user evidence."})
    deferred_conditions = {
        "none": {""}, "date": {"customer_date"}, "event": {"after_event"},
        "payday": {"payday"}, "indefinite": {"indefinite"},
    }
    if result.deferred_kind not in deferred_conditions or result.deferred_condition_code not in deferred_conditions[result.deferred_kind]:
        raise ValidationError({"deferred_condition_code": "Invalid deferred intent schema."})
    if (result.deferred_kind == "date") != bool(result.deferred_until):
        raise ValidationError({"deferred_until": "Only date deferrals require a timestamp."})
    if result.deferred_kind != "none" and not user_claim_ids["deferred_intent"]:
        raise ValidationError({"deferred_kind": "Deferral requires claim-specific user evidence."})
    if result.repeat_intent_kind not in _ANALYSIS_V2_REPEAT_KINDS:
        raise ValidationError({"repeat_intent_kind": "Unsupported repeat kind."})
    if result.repeat_intent_kind and not user_claim_ids["repeat_intent"]:
        raise ValidationError({"repeat_intent_kind": "Repeat intent requires claim-specific user evidence."})
    injection_claim_ids = sorted(
        row["message_id"]
        for row in manifest
        if "injection_risk" in row.get("claim_codes", [])
    )
    if sorted(result.injection_evidence_message_ids) != injection_claim_ids:
        raise ValidationError({"injection_evidence_message_ids": "Injection evidence manifest mismatch."})
    if (result.injection_risk == "none") != (not injection_claim_ids):
        raise ValidationError({"injection_risk": "Injection signal and evidence must agree."})
    if bool(result.has_conflicts) != bool(result.conflict_codes):
        raise ValidationError({"has_conflicts": "Conflict flag and typed codes must agree."})
    if result.project_slot not in _ANALYSIS_V2_PROJECT_SLOTS:
        raise ValidationError({"project_slot": "Unknown project slot."})
    if result.gemini_request_ref and not re.fullmatch(r"greq_[0-9a-f]{20}", result.gemini_request_ref):
        raise ValidationError({"gemini_request_ref": "Invalid opaque request reference."})
    _validate_analysis_version_token(
        result.prompt_version,
        field_name="prompt_version",
        maximum=40,
    )
    _validate_analysis_version_token(
        result.routing_policy_version,
        field_name="routing_policy_version",
        maximum=32,
    )
    _validate_analysis_version_token(
        result.reasoning_policy_version,
        field_name="reasoning_policy_version",
        maximum=32,
    )
    if not re.fullmatch(
        r"(?:|unknown|rules|gemini-[a-z0-9][a-z0-9_.:+-]{0,71})",
        result.analysis_model,
    ):
        raise ValidationError({"analysis_model": "Unknown analysis model."})
    if result.usage_status not in {"accounting_unknown", "provider_reported", "estimated"}:
        raise ValidationError({"usage_status": "Unknown usage status."})


_ANALYSIS_PROPOSAL_VALUE_KEYS = frozenset({
    "basis",
    "condition_code",
    "deferred_until",
    "kind",
    "objection_type",
    "probability",
    "reason_codes",
    "repeat_kind",
})


def _validate_analysis_proposal_payload(proposal):
    value = proposal.typed_value
    if not isinstance(value, dict):
        raise ValidationError({"typed_value": "Only typed proposal fields are allowed."})
    schemas = {
        "update_probability": {"probability", "basis"},
        "record_objection": {"objection_type"},
        "record_deferred_intent": {"kind", "condition_code", "deferred_until"},
        "start_repeat_episode": {"repeat_kind"},
        "request_clarification": {"reason_codes"},
        "close_node": set(), "invalidate_node": set(),
        "open_subfunnel": set(), "switch_active_line": set(),
    }
    expected_keys = schemas.get(proposal.proposal_type)
    if expected_keys is None or set(value) != expected_keys:
        raise ValidationError({"typed_value": "Proposal payload does not match its type."})
    if proposal.proposal_type == "update_probability":
        _validate_finite_01(value.get("probability"), field_name="typed_value")
        if value.get("basis") != "customer_evidence":
            raise ValidationError({"typed_value": "Probability basis must be customer evidence."})
    elif proposal.proposal_type == "record_objection":
        if value.get("objection_type") not in IgObjection.Type.values:
            raise ValidationError({"typed_value": "Unsupported objection type."})
    elif proposal.proposal_type == "record_deferred_intent":
        deferred_conditions = {
            "date": "customer_date", "event": "after_event",
            "payday": "payday", "indefinite": "indefinite",
        }
        kind = value.get("kind")
        if kind not in deferred_conditions or value.get("condition_code") != deferred_conditions[kind]:
            raise ValidationError({"typed_value": "Invalid deferred intent."})
        parsed = parse_datetime(str(value.get("deferred_until") or ""))
        if (kind == "date") != bool(parsed):
            raise ValidationError({"typed_value": "Invalid deferred timestamp."})
    elif proposal.proposal_type == "start_repeat_episode":
        if value.get("repeat_kind") not in _ANALYSIS_V2_REPEAT_KINDS - {""}:
            raise ValidationError({"typed_value": "Unsupported repeat kind."})
    elif proposal.proposal_type == "request_clarification":
        codes = value.get("reason_codes")
        if not isinstance(codes, list) or not codes or len(codes) > 20 or len(codes) != len(set(codes)) or any(code not in _ANALYSIS_V2_CONFLICT_CODES for code in codes):
            raise ValidationError({"typed_value": "Unsupported clarification codes."})
    _validate_positive_message_ids(
        proposal.evidence_message_ids,
        field_name="evidence_message_ids",
    )
    _validate_finite_01(proposal.confidence, field_name="confidence")
    for field_name in (
        "line_id", "target_definition_key", "target_definition_version", "target_key",
    ):
        if not _ANALYSIS_V2_CODE_RE.fullmatch(str(getattr(proposal, field_name) or "")):
            raise ValidationError({field_name: "Only bounded typed identifiers are allowed."})
    if not re.fullmatch(r"analysis-proposal:[0-9a-f]{64}", str(proposal.proposal_key or "")):
        raise ValidationError({"proposal_key": "Invalid proposal identity."})
    for field_name in (
        "source_result_digest", "expected_materiality_digest",
        "expected_state_correlation",
    ):
        _validate_hex(getattr(proposal, field_name), field_name=field_name)
    _validate_hex(
        proposal.expected_authority_digest,
        field_name="expected_authority_digest",
        optional=True,
    )
    if proposal.status not in {
        "pending", "shadow_validated", "blocked_dependency",
        "blocked_legacy_owner", "rejected", "applied",
    }:
        raise ValidationError({"status": "Unsupported proposal status."})
    if proposal.decision_code not in _ANALYSIS_V2_DECISION_CODES:
        raise ValidationError({"decision_code": "Unsupported projector decision."})
    if proposal.projector_version not in {"", "analysis-v2-projector.1"}:
        raise ValidationError({"projector_version": "Unsupported projector version."})


def _validate_analysis_proposal_mutable_values(values):
    if "status" in values and values["status"] not in {
        "pending", "shadow_validated", "blocked_dependency",
        "blocked_legacy_owner", "rejected", "applied",
    }:
        raise ValidationError({"status": "Unsupported proposal status."})
    if "decision_code" in values and values["decision_code"] not in _ANALYSIS_V2_DECISION_CODES:
        raise ValidationError({"decision_code": "Unsupported projector decision."})
    if "projector_version" in values and values["projector_version"] not in {
        "", "analysis-v2-projector.1",
    }:
        raise ValidationError({"projector_version": "Unsupported projector version."})


class _IgConversationAnalysisResultQuerySet(models.QuerySet):
    @staticmethod
    def _reject_mutation():
        raise ValueError("IgConversationAnalysisResult is append-only")

    def update(self, **kwargs):
        self._reject_mutation()

    def delete(self):
        self._reject_mutation()

    def bulk_update(self, objs, fields, batch_size=None):
        self._reject_mutation()

    def bulk_create(self, objs, *args, **kwargs):
        if kwargs.get("update_conflicts") or kwargs.get("update_fields"):
            self._reject_mutation()
        for obj in objs:
            _validate_analysis_result_payload(obj)
        return super().bulk_create(objs, *args, **kwargs)


class IgConversationAnalysisResult(models.Model):
    """Immutable, PII-free typed interpretation of one claimed analysis state."""

    class SourceKind(models.TextChoices):
        AI = "ai", _("Gemini analysis")
        RULES = "rules", _("Deterministic rules")

    class ProbabilityBasis(models.TextChoices):
        CUSTOMER_EVIDENCE = "customer_evidence", _("Customer evidence")
        DETERMINISTIC_NO_BUY = "deterministic_no_buy", _("Explicit no-buy")
        DETERMINISTIC_OPT_OUT = "deterministic_opt_out", _("Explicit opt-out")
        INSUFFICIENT_EVIDENCE = "insufficient_evidence", _("Insufficient evidence")

    class DeferredKind(models.TextChoices):
        NONE = "none", _("Not deferred")
        DATE = "date", _("Specific date")
        EVENT = "event", _("External event")
        PAYDAY = "payday", _("Payday")
        INDEFINITE = "indefinite", _("No exact time")

    class LtvSignal(models.TextChoices):
        UNKNOWN = "unknown", _("Unknown")
        FIRST_PURCHASE = "first_purchase", _("First purchase")
        REPEAT_CUSTOMER = "repeat_customer", _("Repeat customer")
        REACTIVATION = "reactivation", _("Reactivation")

    class InjectionRisk(models.TextChoices):
        NONE = "none", _("No signal")
        SUSPECTED = "suspected", _("Suspected")
        HIGH = "high", _("High confidence signal")

    class UsageStatus(models.TextChoices):
        ACCOUNTING_UNKNOWN = "accounting_unknown", _("Accounting unknown")
        PROVIDER_REPORTED = "provider_reported", _("Provider reported")
        ESTIMATED = "estimated", _("Estimated")

    result_key = models.CharField(max_length=160, unique=True)
    legacy_snapshot = models.OneToOneField(
        "management.IgConversationAnalysisSnapshot",
        on_delete=models.DO_NOTHING,
        related_name="analysis_v2_result",
        db_constraint=False,
    )
    client = models.ForeignKey(
        "management.IgClient",
        on_delete=models.DO_NOTHING,
        related_name="analysis_v2_results",
        db_constraint=False,
    )
    commercial_episode = models.ForeignKey(
        "management.IgCommercialEpisode",
        null=True,
        blank=True,
        on_delete=models.DO_NOTHING,
        related_name="analysis_v2_results",
        db_constraint=False,
    )
    line_id = models.CharField(max_length=96, blank=True, default="")
    watermark_message_id = models.PositiveBigIntegerField()
    job_revision = models.PositiveBigIntegerField()
    materiality_event_highwater = models.PositiveBigIntegerField()
    materiality_digest = models.CharField(max_length=64)
    authority_digest = models.CharField(max_length=64, blank=True, default="")
    artifact_digest = models.CharField(max_length=64, blank=True, default="")
    state_correlation = models.CharField(max_length=64)
    result_schema_version = models.CharField(max_length=32)
    normalizer_version = models.CharField(max_length=32)
    source_kind = models.CharField(
        max_length=16,
        choices=SourceKind.choices,
        default=SourceKind.AI,
    )
    interaction_type = models.CharField(
        max_length=32,
        choices=IgConversationAnalysisSnapshot.InteractionType.choices,
        default=IgConversationAnalysisSnapshot.InteractionType.UNKNOWN,
        db_index=True,
    )
    score_band = models.CharField(
        max_length=24,
        choices=IgConversationAnalysisSnapshot.Band.choices,
        db_index=True,
    )
    detected_language = models.CharField(max_length=12, blank=True, default="")
    language_evidence_message_ids = models.JSONField(default=list, blank=True)
    purchase_probability = models.DecimalField(
        max_digits=5,
        decimal_places=4,
        null=True,
        blank=True,
    )
    purchase_confidence = models.DecimalField(
        max_digits=5,
        decimal_places=4,
        null=True,
        blank=True,
    )
    probability_basis = models.CharField(
        max_length=32,
        choices=ProbabilityBasis.choices,
        default=ProbabilityBasis.INSUFFICIENT_EVIDENCE,
    )
    evidence_manifest = models.JSONField(default=list, blank=True)
    customer_evidence_count = models.PositiveSmallIntegerField(default=0)
    manager_evidence_count = models.PositiveSmallIntegerField(default=0)
    authority_evidence_count = models.PositiveSmallIntegerField(default=0)
    active_objection_type = models.CharField(max_length=32, blank=True, default="")
    active_objection_confidence = models.DecimalField(
        max_digits=5,
        decimal_places=4,
        null=True,
        blank=True,
    )
    deferred_kind = models.CharField(
        max_length=16,
        choices=DeferredKind.choices,
        default=DeferredKind.NONE,
    )
    deferred_until = models.DateTimeField(null=True, blank=True)
    deferred_condition_code = models.CharField(max_length=32, blank=True, default="")
    repeat_intent_kind = models.CharField(max_length=32, blank=True, default="")
    repeat_intent_confidence = models.DecimalField(
        max_digits=5,
        decimal_places=4,
        null=True,
        blank=True,
    )
    prior_purchase_count = models.PositiveIntegerField(default=0)
    ltv_signal = models.CharField(
        max_length=24,
        choices=LtvSignal.choices,
        default=LtvSignal.UNKNOWN,
    )
    injection_risk = models.CharField(
        max_length=16,
        choices=InjectionRisk.choices,
        default=InjectionRisk.NONE,
    )
    injection_evidence_message_ids = models.JSONField(default=list, blank=True)
    has_conflicts = models.BooleanField(default=False)
    conflict_codes = models.JSONField(default=list, blank=True)
    uncertainty_codes = models.JSONField(default=list, blank=True)
    analysis_model = models.CharField(max_length=80, blank=True, default="")
    prompt_version = models.CharField(max_length=40, blank=True, default="")
    routing_policy_version = models.CharField(max_length=32, blank=True, default="")
    reasoning_policy_version = models.CharField(max_length=32, blank=True, default="")
    project_slot = models.CharField(max_length=24, blank=True, default="")
    gemini_request_ref = models.CharField(max_length=40, blank=True, default="")
    usage_status = models.CharField(
        max_length=24,
        choices=UsageStatus.choices,
        default=UsageStatus.ACCOUNTING_UNKNOWN,
    )
    prompt_tokens = models.PositiveBigIntegerField(default=0)
    thoughts_tokens = models.PositiveBigIntegerField(default=0)
    candidates_tokens = models.PositiveBigIntegerField(default=0)
    total_tokens = models.PositiveBigIntegerField(default=0)
    analysis_latency_ms = models.PositiveIntegerField(default=0)
    result_digest = models.CharField(max_length=64)
    analyzed_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    objects = _IgConversationAnalysisResultQuerySet.as_manager()

    class Meta:
        base_manager_name = "objects"
        ordering = ["-id"]
        constraints = [
            models.UniqueConstraint(
                fields=[
                    "client",
                    "job_revision",
                    "materiality_event_highwater",
                    "result_schema_version",
                ],
                name="ig_anres_cursor_version_uniq",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(purchase_probability__isnull=True)
                    | (
                        models.Q(purchase_probability__gte=Decimal("0"))
                        & models.Q(purchase_probability__lte=Decimal("1"))
                    )
                ),
                name="ig_anres_probability_range",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(purchase_confidence__isnull=True)
                    | (
                        models.Q(purchase_confidence__gte=Decimal("0"))
                        & models.Q(purchase_confidence__lte=Decimal("1"))
                    )
                ),
                name="ig_anres_confidence_range",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(active_objection_confidence__isnull=True)
                    | (
                        models.Q(active_objection_confidence__gte=Decimal("0"))
                        & models.Q(active_objection_confidence__lte=Decimal("1"))
                    )
                ),
                name="ig_anres_objection_conf_range",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(repeat_intent_confidence__isnull=True)
                    | (
                        models.Q(repeat_intent_confidence__gte=Decimal("0"))
                        & models.Q(repeat_intent_confidence__lte=Decimal("1"))
                    )
                ),
                name="ig_anres_repeat_conf_range",
            ),
            models.CheckConstraint(
                condition=models.Q(materiality_event_highwater__gt=0),
                name="ig_anres_materiality_positive",
            ),
        ]
        indexes = [
            models.Index(fields=["client", "-created_at"], name="ig_anres_client_created"),
            models.Index(
                fields=["commercial_episode", "line_id", "-id"],
                name="ig_anres_episode_line",
            ),
            models.Index(
                fields=["materiality_event_highwater", "-id"],
                name="ig_anres_materiality",
            ),
            models.Index(
                fields=["purchase_probability", "-id"],
                name="ig_anres_probability",
            ),
        ]

    def save(self, *args, **kwargs):
        if self.pk is not None or not self._state.adding:
            raise ValueError("IgConversationAnalysisResult is append-only")
        _validate_analysis_result_payload(self)
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("IgConversationAnalysisResult is append-only")


class _IgAnalysisProposalQuerySet(models.QuerySet):
    _MUTABLE_FIELDS = frozenset({
        "status",
        "decision_code",
        "projector_version",
        "decided_at",
        "updated_at",
    })

    @staticmethod
    def _reject_delete():
        raise ValueError("IgAnalysisProposal cannot be deleted")

    def update(self, **kwargs):
        if set(kwargs) - self._MUTABLE_FIELDS:
            raise ValueError("IgAnalysisProposal identity is immutable")
        _validate_analysis_proposal_mutable_values(kwargs)
        return super().update(**kwargs)

    def bulk_update(self, objs, fields, batch_size=None):
        if set(fields) - self._MUTABLE_FIELDS:
            raise ValueError("IgAnalysisProposal identity is immutable")
        for obj in objs:
            _validate_analysis_proposal_payload(obj)
        # Django implements bulk_update() through QuerySet.update(CASE ...).
        # Use a plain QuerySet only after validating every object so the
        # public mutable-value boundary does not mistake internal CASE
        # expressions for untrusted caller values.
        plain_queryset = models.QuerySet(
            model=self.model,
            query=self.query.chain(),
            using=self.db,
            hints=self._hints,
        )
        return plain_queryset.bulk_update(objs, fields, batch_size=batch_size)

    def bulk_create(self, objs, *args, **kwargs):
        if kwargs.get("update_conflicts") or kwargs.get("update_fields"):
            raise ValueError("IgAnalysisProposal identity is immutable")
        for obj in objs:
            _validate_analysis_proposal_payload(obj)
        return super().bulk_create(objs, *args, **kwargs)

    def delete(self):
        self._reject_delete()


class IgAnalysisProposal(models.Model):
    """Immutable generic model proposal plus projector-owned shadow outcome."""

    class ProposalType(models.TextChoices):
        CLOSE_NODE = "close_node", _("Close funnel node")
        INVALIDATE_NODE = "invalidate_node", _("Invalidate funnel node")
        OPEN_SUBFUNNEL = "open_subfunnel", _("Open sub-funnel")
        SWITCH_ACTIVE_LINE = "switch_active_line", _("Switch active line")
        START_REPEAT_EPISODE = "start_repeat_episode", _("Start repeat episode")
        RECORD_OBJECTION = "record_objection", _("Record objection")
        RECORD_DEFERRED_INTENT = "record_deferred_intent", _("Record deferred intent")
        UPDATE_PROBABILITY = "update_probability", _("Update probability")
        REQUEST_CLARIFICATION = "request_clarification", _("Request clarification")

    class TargetScope(models.TextChoices):
        CLIENT = "client", _("Client")
        EPISODE = "episode", _("Episode")
        LINE = "line", _("Line")
        FUNNEL_NODE = "funnel_node", _("Funnel node")
        SUBFUNNEL = "subfunnel", _("Sub-funnel")

    class Status(models.TextChoices):
        PENDING = "pending", _("Pending validation")
        SHADOW_VALIDATED = "shadow_validated", _("Validated in shadow")
        BLOCKED_DEPENDENCY = "blocked_dependency", _("Blocked by dependency")
        BLOCKED_LEGACY_OWNER = "blocked_legacy_owner", _("Blocked by legacy owner")
        REJECTED = "rejected", _("Rejected")
        APPLIED = "applied", _("Applied")

    _IDENTITY_FIELDS = frozenset({
        "proposal_key",
        "analysis_result_id",
        "ordinal",
        "client_id",
        "commercial_episode_id",
        "line_id",
        "proposal_type",
        "target_scope",
        "target_definition_key",
        "target_definition_version",
        "target_key",
        "typed_value",
        "evidence_message_ids",
        "confidence",
        "source_result_digest",
        "expected_materiality_digest",
        "expected_authority_digest",
        "expected_state_correlation",
        "created_at",
    })

    proposal_key = models.CharField(max_length=160, unique=True)
    analysis_result = models.ForeignKey(
        "management.IgConversationAnalysisResult",
        on_delete=models.DO_NOTHING,
        related_name="proposals",
        db_constraint=False,
    )
    ordinal = models.PositiveSmallIntegerField()
    client = models.ForeignKey(
        "management.IgClient",
        on_delete=models.DO_NOTHING,
        related_name="analysis_v2_proposals",
        db_constraint=False,
    )
    commercial_episode = models.ForeignKey(
        "management.IgCommercialEpisode",
        null=True,
        blank=True,
        on_delete=models.DO_NOTHING,
        related_name="analysis_v2_proposals",
        db_constraint=False,
    )
    line_id = models.CharField(max_length=96, blank=True, default="")
    proposal_type = models.CharField(max_length=32, choices=ProposalType.choices)
    target_scope = models.CharField(max_length=24, choices=TargetScope.choices)
    target_definition_key = models.CharField(max_length=96, blank=True, default="")
    target_definition_version = models.CharField(max_length=32, blank=True, default="")
    target_key = models.CharField(max_length=96, blank=True, default="")
    typed_value = models.JSONField(default=dict, blank=True)
    evidence_message_ids = models.JSONField(default=list, blank=True)
    confidence = models.DecimalField(max_digits=5, decimal_places=4)
    source_result_digest = models.CharField(max_length=64)
    expected_materiality_digest = models.CharField(max_length=64)
    expected_authority_digest = models.CharField(max_length=64, blank=True, default="")
    expected_state_correlation = models.CharField(max_length=64)
    status = models.CharField(
        max_length=24,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    decision_code = models.CharField(max_length=64, blank=True, default="")
    projector_version = models.CharField(max_length=32, blank=True, default="")
    decided_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    objects = _IgAnalysisProposalQuerySet.as_manager()

    class Meta:
        base_manager_name = "objects"
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(
                fields=["analysis_result", "ordinal"],
                name="ig_anprop_result_ordinal_uniq",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(confidence__gte=Decimal("0"))
                    & models.Q(confidence__lte=Decimal("1"))
                ),
                name="ig_anprop_confidence_range",
            ),
            models.CheckConstraint(
                condition=models.Q(status__in=(
                    "pending",
                    "shadow_validated",
                    "blocked_dependency",
                    "blocked_legacy_owner",
                    "rejected",
                    "applied",
                )),
                name="ig_anprop_status_valid",
            ),
        ]
        indexes = [
            models.Index(fields=["status", "id"], name="ig_anprop_status_id"),
            models.Index(
                fields=["client", "status", "id"],
                name="ig_anprop_client_status",
            ),
            models.Index(
                fields=["commercial_episode", "line_id", "status", "id"],
                name="ig_anprop_episode_line",
            ),
            models.Index(
                fields=["proposal_type", "status", "id"],
                name="ig_anprop_type_status",
            ),
        ]

    def save(self, *args, **kwargs):
        if self.pk:
            previous = (
                type(self)._base_manager.filter(pk=self.pk)
                .values(*self._IDENTITY_FIELDS)
                .first()
            )
            if previous:
                for field_name in self._IDENTITY_FIELDS:
                    if getattr(self, field_name) != previous[field_name]:
                        raise ValueError("IgAnalysisProposal identity is immutable")
        _validate_analysis_proposal_payload(self)
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("IgAnalysisProposal cannot be deleted")


_MEMORY_CODE_RE = re.compile(r"^[a-z0-9_.:+-]{1,96}$")
_MEMORY_LINE_RE = re.compile(r"^line:(?!.*[0-9]{7})[a-z0-9][a-z0-9_.-]{0,79}$")
_MEMORY_KEY_ID_RE = re.compile(r"^tmk_(?!.*[0-9]{7})[a-z0-9][a-z0-9_.-]{0,27}$")
_MEMORY_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
_MEMORY_RECORD_RE = re.compile(r"^memory-fact:[0-9a-f]{64}$")
_MEMORY_SLOT_RE = re.compile(r"^memory-slot:[0-9a-f]{64}$")
_MEMORY_SCHEMA_VERSION = "typed-memory.v1"
_MEMORY_POLICY_VERSION = "typed-memory-projector.v1"
IG_MEMORY_MAX_CHAIN_DEPTH = 512
_MEMORY_FACT_KEYS = frozenset({
    "observed_language", "objection_observed", "deferred_intent",
})
_MEMORY_CLAIM_BY_FACT = {
    "observed_language": "language",
    "objection_observed": "objection",
    "deferred_intent": "deferred_intent",
}
_MEMORY_INVALIDATION_REASONS = frozenset({
    "episode_closed", "explicit_retraction", "line_replaced",
    "reset_boundary", "superseded_by_new_assertion", "valid_until_elapsed",
})
_MEMORY_NON_TTL_INVALIDATION_REASONS = (
    "episode_closed", "explicit_retraction", "line_replaced",
    "reset_boundary", "superseded_by_new_assertion",
)


def _memory_scope_shape(instance) -> bool:
    scope = str(instance.scope or "")
    episode_id = getattr(instance, "commercial_episode_id", None)
    line_id = str(getattr(instance, "line_id", "") or "")
    order_id = getattr(instance, "order_id", None)
    case_id = getattr(instance, "post_sale_case_id", None)
    return {
        "client": not episode_id and not line_id and not order_id and not case_id,
        "episode": bool(episode_id and not line_id and not order_id and not case_id),
        "line": bool(episode_id and line_id and not order_id and not case_id),
        "order": bool(order_id and not episode_id and not line_id and not case_id),
        "case": bool(case_id and not episode_id and not line_id and not order_id),
    }.get(scope, False)


def _validate_memory_typed_value(fact_key, value, *, operation):
    if not isinstance(value, dict):
        raise ValidationError({"typed_value": "Typed memory value must be an object."})
    if operation != "assert":
        if value:
            raise ValidationError({"typed_value": "Memory tombstones cannot contain values."})
        return
    if fact_key == "observed_language":
        if set(value) != {"code"} or value.get("code") not in {
            "uk", "ru", "en", "mixed", "unknown",
        }:
            raise ValidationError({"typed_value": "Invalid observed-language value."})
        return
    if fact_key == "objection_observed":
        if set(value) != {"type"} or value.get("type") not in IgObjection.Type.values:
            raise ValidationError({"typed_value": "Invalid objection value."})
        return
    if fact_key == "deferred_intent":
        if set(value) != {"kind", "condition_code"}:
            raise ValidationError({"typed_value": "Invalid deferred-intent shape."})
        expected = {
            "date": "customer_date", "event": "after_event",
            "payday": "payday", "indefinite": "indefinite",
        }
        kind = value.get("kind")
        if (
            kind not in expected
            or value.get("condition_code") != expected.get(kind)
        ):
            raise ValidationError({"typed_value": "Invalid deferred-intent value."})
        return
    raise ValidationError({"fact_key": "Unsupported typed-memory fact."})


def _validate_memory_fact_payload(fact):
    if not _MEMORY_RECORD_RE.fullmatch(str(fact.record_key or "")):
        raise ValidationError({"record_key": "Invalid typed-memory record identity."})
    if not _MEMORY_SLOT_RE.fullmatch(str(fact.slot_key or "")):
        raise ValidationError({"slot_key": "Invalid typed-memory slot identity."})
    if fact.fact_key not in _MEMORY_FACT_KEYS:
        raise ValidationError({"fact_key": "Unsupported typed-memory fact."})
    if fact.schema_version != _MEMORY_SCHEMA_VERSION:
        raise ValidationError({"schema_version": "Unsupported typed-memory schema."})
    if not _memory_scope_shape(fact):
        raise ValidationError({"scope": "Typed-memory scope coordinates are inconsistent."})
    if fact.line_id and not _MEMORY_LINE_RE.fullmatch(str(fact.line_id)):
        raise ValidationError({"line_id": "Invalid line identity."})
    if fact.operation not in {"assert", "invalidate", "expire"}:
        raise ValidationError({"operation": "Unsupported typed-memory operation."})
    _validate_memory_typed_value(
        fact.fact_key,
        fact.typed_value,
        operation=fact.operation,
    )
    _validate_finite_01(fact.confidence, field_name="confidence", optional=True)
    if not _MEMORY_HEX_RE.fullmatch(str(fact.integrity_hmac or "")):
        raise ValidationError({"integrity_hmac": "Invalid typed-memory HMAC."})
    if not _MEMORY_KEY_ID_RE.fullmatch(str(fact.integrity_key_id or "")):
        raise ValidationError({"integrity_key_id": "Invalid typed-memory HMAC key id."})
    if fact.valid_until and fact.valid_until <= fact.observed_at:
        raise ValidationError({"valid_until": "Memory expiry must follow observation."})
    if fact.operation == "assert":
        if (
            fact.producer != "analysis_v2"
            or fact.producer_policy_version != _MEMORY_POLICY_VERSION
            or fact.closure_method != "analysis_assertion"
            or fact.source_role != "user"
            or not fact.source_result_id
            or not 1 <= int(fact.expected_evidence_count or 0) <= 40
            or fact.reason_code
            or fact.source_event_digest
        ):
            raise ValidationError({"source_result": "Analysis assertions require user evidence."})
        for field_name in (
            "source_result_digest", "source_materiality_digest",
            "source_state_correlation",
        ):
            if not _MEMORY_HEX_RE.fullmatch(str(getattr(fact, field_name) or "")):
                raise ValidationError({field_name: "Invalid analysis identity."})
        if int(fact.source_watermark_message_id or 0) <= 0:
            raise ValidationError({"source_watermark_message_id": "Invalid analysis watermark."})
        result = fact.source_result
        if (
            result.client_id != fact.client_id
            or result.result_schema_version != "analysis-v2.2"
            or result.result_digest != fact.source_result_digest
            or result.materiality_digest != fact.source_materiality_digest
            or result.state_correlation != fact.source_state_correlation
            or result.watermark_message_id != fact.source_watermark_message_id
            or result.analyzed_at != fact.observed_at
        ):
            raise ValidationError({"source_result": "Fact does not match its Analysis V2 source."})
        if fact.fact_key == "observed_language" and (
            fact.typed_value != {"code": result.detected_language}
            or fact.expected_evidence_count
            != len(result.language_evidence_message_ids or [])
            or fact.confidence is not None
            or fact.valid_until is not None
        ):
            raise ValidationError({"typed_value": "Language fact does not match its source."})
        if fact.fact_key == "objection_observed" and (
            fact.typed_value != {"type": result.active_objection_type}
            or fact.commercial_episode_id != result.commercial_episode_id
            or fact.line_id != result.line_id
            or fact.confidence != result.active_objection_confidence
            or fact.valid_until is not None
            or fact.expected_evidence_count != sum(
                row.get("source_role") == "user"
                and "objection" in (row.get("claim_codes") or [])
                for row in result.evidence_manifest or []
                if isinstance(row, dict)
            )
        ):
            raise ValidationError({"typed_value": "Objection fact does not match its source."})
        if fact.fact_key == "deferred_intent" and (
            fact.commercial_episode_id != result.commercial_episode_id
            or fact.line_id
            or fact.typed_value.get("kind") != result.deferred_kind
            or fact.typed_value.get("condition_code") != result.deferred_condition_code
            or fact.confidence is not None
            or fact.valid_until != result.deferred_until
            or fact.expected_evidence_count != sum(
                row.get("source_role") == "user"
                and "deferred_intent" in (row.get("claim_codes") or [])
                for row in result.evidence_manifest or []
                if isinstance(row, dict)
            )
        ):
            raise ValidationError({"typed_value": "Deferred fact does not match its source."})
    else:
        tombstone_shape = (
            fact.operation == "invalidate"
            and fact.closure_method == "deterministic_invalidation"
            and fact.reason_code in (
                _MEMORY_NON_TTL_INVALIDATION_REASONS
            )
        ) or (
            fact.operation == "expire"
            and fact.closure_method == "ttl_expiry"
            and fact.reason_code == "valid_until_elapsed"
        )
        if (
            not tombstone_shape
            or
            fact.producer != "deterministic_projector"
            or fact.producer_policy_version != _MEMORY_POLICY_VERSION
            or fact.closure_method not in {"deterministic_invalidation", "ttl_expiry"}
            or fact.source_role not in {"system", "authority"}
            or fact.source_result_id
            or fact.source_result_digest
            or fact.source_materiality_digest
            or fact.source_state_correlation
            or fact.source_watermark_message_id
            or fact.expected_evidence_count
            or fact.reason_code not in _MEMORY_INVALIDATION_REASONS
            or not _MEMORY_HEX_RE.fullmatch(str(fact.source_event_digest or ""))
            or fact.confidence is not None
            or fact.valid_until is not None
            or not fact.supersedes_id
        ):
            raise ValidationError({"source_event_digest": "Invalid deterministic tombstone source."})
    expected_policy = {
        "observed_language": ("client", "low", "client"),
        "objection_observed": ({"episode", "line"}, "personal_preference", "episode"),
        "deferred_intent": ("episode", "personal_preference", None),
    }[fact.fact_key]
    allowed_scope = expected_policy[0]
    if isinstance(allowed_scope, set):
        scope_ok = fact.scope in allowed_scope
    else:
        scope_ok = fact.scope == allowed_scope
    retention_ok = (
        fact.retention_class in {"episode", "until_date"}
        if fact.fact_key == "deferred_intent"
        else fact.retention_class == expected_policy[2]
    )
    if not scope_ok or fact.sensitivity != expected_policy[1] or not retention_ok:
        raise ValidationError({"fact_key": "Fact scope/sensitivity/retention policy mismatch."})
    if fact.fact_key == "deferred_intent" and fact.operation == "assert":
        kind = fact.typed_value.get("kind")
        if kind == "date":
            if (
                fact.valid_until is None
                or fact.retention_class != "until_date"
            ):
                raise ValidationError({"valid_until": "Date deferral expiry is inconsistent."})
        elif fact.valid_until is not None or fact.retention_class != "episode":
            raise ValidationError({"valid_until": "Non-date deferral cannot carry an expiry."})
    if fact.supersedes_id:
        previous = fact.supersedes
        if (
            previous.slot_key != fact.slot_key
            or previous.client_id != fact.client_id
            or previous.scope != fact.scope
            or previous.commercial_episode_id != fact.commercial_episode_id
            or previous.line_id != fact.line_id
            or previous.order_id != fact.order_id
            or previous.post_sale_case_id != fact.post_sale_case_id
            or previous.fact_key != fact.fact_key
            or previous.schema_version != fact.schema_version
            or not previous.current_for_heads.filter(
                slot_key=fact.slot_key,
                revision__lt=IG_MEMORY_MAX_CHAIN_DEPTH,
            ).exists()
        ):
            raise ValidationError({"supersedes": "Memory fact predecessor is not current."})
    elif IgMemoryHead.objects.filter(slot_key=fact.slot_key).exists():
        raise ValidationError({"supersedes": "Existing memory slot requires a predecessor."})


class _IgMemoryImmutableQuerySet(models.QuerySet):
    @staticmethod
    def _reject():
        raise ValueError("Typed-memory evidence is append-only")

    def update(self, **kwargs):
        self._reject()

    def delete(self):
        self._reject()

    def bulk_update(self, objs, fields, batch_size=None):
        self._reject()

    def bulk_create(self, objs, *args, **kwargs):
        if kwargs.get("update_conflicts") or kwargs.get("update_fields"):
            self._reject()
        validator = (
            _validate_memory_fact_payload
            if self.model.__name__ == "IgMemoryFact"
            else _validate_memory_evidence_payload
        )
        for obj in objs:
            validator(obj)
        return super().bulk_create(objs, *args, **kwargs)


class IgMemoryFact(models.Model):
    """Immutable typed assertion or tombstone derived from reviewed evidence."""

    class Scope(models.TextChoices):
        CLIENT = "client", _("Client")
        EPISODE = "episode", _("Episode")
        LINE = "line", _("Line")
        ORDER = "order", _("Order")
        CASE = "case", _("Case")

    class FactKey(models.TextChoices):
        OBSERVED_LANGUAGE = "observed_language", _("Observed language")
        OBJECTION_OBSERVED = "objection_observed", _("Observed objection")
        DEFERRED_INTENT = "deferred_intent", _("Deferred intent")

    class Operation(models.TextChoices):
        ASSERT = "assert", _("Assert")
        INVALIDATE = "invalidate", _("Invalidate")
        EXPIRE = "expire", _("Expire")

    record_key = models.CharField(max_length=80)
    slot_key = models.CharField(max_length=80)
    client = models.ForeignKey(
        "management.IgClient", on_delete=models.DO_NOTHING,
        related_name="memory_facts", db_constraint=False,
    )
    scope = models.CharField(max_length=16, choices=Scope.choices)
    commercial_episode = models.ForeignKey(
        "management.IgCommercialEpisode", null=True, blank=True,
        on_delete=models.DO_NOTHING, related_name="memory_facts", db_constraint=False,
    )
    line_id = models.CharField(max_length=96, blank=True, default="")
    order = models.ForeignKey(
        "orders.Order", null=True, blank=True, on_delete=models.DO_NOTHING,
        related_name="instagram_memory_facts", db_constraint=False,
    )
    post_sale_case = models.ForeignKey(
        "management.IgPostSaleCase", null=True, blank=True,
        on_delete=models.DO_NOTHING, related_name="memory_facts", db_constraint=False,
    )
    fact_key = models.CharField(max_length=32, choices=FactKey.choices)
    schema_version = models.CharField(max_length=32, default=_MEMORY_SCHEMA_VERSION)
    operation = models.CharField(max_length=16, choices=Operation.choices)
    typed_value = models.JSONField(default=dict, blank=True)
    confidence = models.DecimalField(max_digits=5, decimal_places=4, null=True, blank=True)
    source_role = models.CharField(max_length=16)
    producer = models.CharField(max_length=32)
    producer_policy_version = models.CharField(
        max_length=32, default=_MEMORY_POLICY_VERSION,
    )
    closure_method = models.CharField(max_length=32)
    source_result = models.ForeignKey(
        "management.IgConversationAnalysisResult", null=True, blank=True,
        on_delete=models.DO_NOTHING, related_name="memory_facts", db_constraint=False,
    )
    source_result_digest = models.CharField(max_length=64, blank=True, default="")
    source_materiality_digest = models.CharField(max_length=64, blank=True, default="")
    source_state_correlation = models.CharField(max_length=64, blank=True, default="")
    source_watermark_message_id = models.PositiveBigIntegerField(default=0)
    source_event_digest = models.CharField(max_length=64, blank=True, default="")
    expected_evidence_count = models.PositiveSmallIntegerField(default=0)
    supersedes = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.DO_NOTHING,
        related_name="superseding_facts", db_constraint=False,
    )
    reason_code = models.CharField(max_length=48, blank=True, default="")
    integrity_hmac = models.CharField(max_length=64)
    integrity_key_id = models.CharField(max_length=32)
    observed_at = models.DateTimeField()
    valid_until = models.DateTimeField(null=True, blank=True)
    sensitivity = models.CharField(max_length=24)
    retention_class = models.CharField(max_length=24)
    created_at = models.DateTimeField(auto_now_add=True)
    objects = _IgMemoryImmutableQuerySet.as_manager()

    class Meta:
        base_manager_name = "objects"
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(fields=["record_key"], name="ig_memfact_record_uniq"),
            models.UniqueConstraint(fields=["supersedes"], name="ig_memfact_supersedes_uniq"),
            models.CheckConstraint(
                condition=models.Q(confidence__isnull=True) | (
                    models.Q(confidence__gte=Decimal("0"))
                    & models.Q(confidence__lte=Decimal("1"))
                ),
                name="ig_memfact_conf_range",
            ),
            models.CheckConstraint(
                condition=models.Q(valid_until__isnull=True)
                | models.Q(valid_until__gt=models.F("observed_at")),
                name="ig_memfact_expiry_order",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        scope="client", commercial_episode__isnull=True,
                        line_id="", order__isnull=True, post_sale_case__isnull=True,
                    )
                    | models.Q(
                        scope="episode", commercial_episode__isnull=False,
                        line_id="", order__isnull=True, post_sale_case__isnull=True,
                    )
                    | models.Q(
                        scope="line", commercial_episode__isnull=False,
                        order__isnull=True, post_sale_case__isnull=True,
                    ) & ~models.Q(line_id="")
                    | models.Q(
                        scope="order", commercial_episode__isnull=True,
                        line_id="", order__isnull=False, post_sale_case__isnull=True,
                    )
                    | models.Q(
                        scope="case", commercial_episode__isnull=True,
                        line_id="", order__isnull=True, post_sale_case__isnull=False,
                    )
                ),
                name="ig_memfact_scope_shape",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        operation="assert", producer="analysis_v2",
                        producer_policy_version=_MEMORY_POLICY_VERSION,
                        closure_method="analysis_assertion",
                        source_role="user", source_result__isnull=False,
                        source_event_digest="", reason_code="",
                        expected_evidence_count__gte=1,
                        expected_evidence_count__lte=40,
                    )
                    | models.Q(
                        operation="invalidate",
                        producer="deterministic_projector",
                        producer_policy_version=_MEMORY_POLICY_VERSION,
                        closure_method="deterministic_invalidation",
                        source_role__in=("system", "authority"),
                        source_result__isnull=True, source_result_digest="",
                        source_materiality_digest="", source_state_correlation="",
                        source_watermark_message_id=0,
                        expected_evidence_count=0,
                        reason_code__in=tuple(
                            _MEMORY_NON_TTL_INVALIDATION_REASONS
                        ),
                    )
                    | models.Q(
                        operation="expire", producer="deterministic_projector",
                        producer_policy_version=_MEMORY_POLICY_VERSION,
                        closure_method="ttl_expiry",
                        source_role__in=("system", "authority"),
                        source_result__isnull=True, source_result_digest="",
                        source_materiality_digest="", source_state_correlation="",
                        source_watermark_message_id=0,
                        expected_evidence_count=0,
                        reason_code="valid_until_elapsed",
                    )
                ),
                name="ig_memfact_source_shape",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        fact_key="observed_language", scope="client",
                        sensitivity="low", retention_class="client",
                        valid_until__isnull=True,
                    )
                    | models.Q(
                        fact_key="objection_observed",
                        scope__in=("episode", "line"),
                        sensitivity="personal_preference",
                        retention_class="episode", valid_until__isnull=True,
                    )
                    | models.Q(
                        fact_key="deferred_intent", scope="episode",
                        sensitivity="personal_preference",
                        retention_class__in=("episode", "until_date"),
                    )
                ),
                name="ig_memfact_policy_shape",
            ),
        ]
        indexes = [
            models.Index(fields=["client", "-observed_at"], name="ig_memfact_client_obs"),
            models.Index(fields=["slot_key", "-id"], name="ig_memfact_slot_id"),
            models.Index(fields=["source_result", "fact_key"], name="ig_memfact_result_key"),
            models.Index(fields=["valid_until", "id"], name="ig_memfact_valid_until"),
        ]

    def save(self, *args, **kwargs):
        if self.pk is not None or not self._state.adding:
            raise ValueError("IgMemoryFact is append-only")
        _validate_memory_fact_payload(self)
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("IgMemoryFact is append-only")


def _validate_memory_evidence_payload(evidence):
    if not 1 <= int(evidence.ordinal or 0) <= 40:
        raise ValidationError({"ordinal": "Memory evidence ordinal must be 1..40."})
    if int(evidence.message_id or 0) <= 0 or evidence.source_role != "user":
        raise ValidationError({"message_id": "Memory evidence must be customer-owned."})
    if evidence.claim_code not in {"language", "objection", "deferred_intent"}:
        raise ValidationError({"claim_code": "Unsupported memory evidence claim."})
    if evidence.fact_id and (
        evidence.claim_code != _MEMORY_CLAIM_BY_FACT.get(evidence.fact.fact_key)
    ):
        raise ValidationError({"claim_code": "Evidence claim does not match the fact."})
    if evidence.fact_id:
        fact = evidence.fact
        if evidence.ordinal > fact.expected_evidence_count or not fact.source_result_id:
            raise ValidationError({"ordinal": "Evidence exceeds the fact manifest."})
        result = fact.source_result
        if evidence.claim_code == "language":
            owned = evidence.message_id in (result.language_evidence_message_ids or [])
        else:
            owned = any(
                isinstance(row, dict)
                and row.get("message_id") == evidence.message_id
                and row.get("source_role") == "user"
                and evidence.claim_code in (row.get("claim_codes") or [])
                for row in result.evidence_manifest or []
            )
        if not owned:
            raise ValidationError({"message_id": "Evidence is not owned by the source result."})
    if not _MEMORY_HEX_RE.fullmatch(str(evidence.evidence_hmac or "")):
        raise ValidationError({"evidence_hmac": "Invalid memory evidence HMAC."})
    if not _MEMORY_KEY_ID_RE.fullmatch(str(evidence.integrity_key_id or "")):
        raise ValidationError({"integrity_key_id": "Invalid memory evidence key id."})


class IgMemoryFactEvidence(models.Model):
    fact = models.ForeignKey(
        "management.IgMemoryFact", on_delete=models.DO_NOTHING,
        related_name="evidence_rows", db_constraint=False,
    )
    ordinal = models.PositiveSmallIntegerField()
    message_id = models.PositiveBigIntegerField()
    source_role = models.CharField(max_length=16)
    claim_code = models.CharField(max_length=32)
    evidence_hmac = models.CharField(max_length=64)
    integrity_key_id = models.CharField(max_length=32)
    created_at = models.DateTimeField(auto_now_add=True)
    objects = _IgMemoryImmutableQuerySet.as_manager()

    class Meta:
        base_manager_name = "objects"
        ordering = ["ordinal", "id"]
        constraints = [
            models.UniqueConstraint(fields=["fact", "ordinal"], name="ig_memev_fact_ord_uniq"),
            models.UniqueConstraint(
                fields=["fact", "message_id", "claim_code"],
                name="ig_memev_fact_msg_claim_uniq",
            ),
            models.CheckConstraint(condition=models.Q(ordinal__gte=1), name="ig_memev_ordinal_positive"),
            models.CheckConstraint(condition=models.Q(message_id__gte=1), name="ig_memev_message_positive"),
        ]
        indexes = [
            models.Index(fields=["message_id", "id"], name="ig_memev_message_id"),
        ]

    def save(self, *args, **kwargs):
        if self.pk is not None or not self._state.adding:
            raise ValueError("IgMemoryFactEvidence is append-only")
        _validate_memory_evidence_payload(self)
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("IgMemoryFactEvidence is append-only")


def _validate_memory_head_payload(head):
    if not _MEMORY_SLOT_RE.fullmatch(str(head.slot_key or "")):
        raise ValidationError({"slot_key": "Invalid typed-memory slot identity."})
    if head.schema_version != _MEMORY_SCHEMA_VERSION or not _memory_scope_shape(head):
        raise ValidationError({"scope": "Invalid typed-memory head scope."})
    if head.fact_key not in _MEMORY_FACT_KEYS or (
        head.line_id and not _MEMORY_LINE_RE.fullmatch(str(head.line_id))
    ):
        raise ValidationError({"fact_key": "Invalid typed-memory head identity."})
    if not 1 <= int(head.revision or 0) <= IG_MEMORY_MAX_CHAIN_DEPTH:
        raise ValidationError({"revision": "Memory head revision is outside its bounded chain."})
    if head.projection_policy_version != _MEMORY_POLICY_VERSION:
        raise ValidationError({"projection_policy_version": "Unsupported memory projector."})
    if not _MEMORY_HEX_RE.fullmatch(str(head.projection_hmac or "")):
        raise ValidationError({"projection_hmac": "Invalid memory projection HMAC."})
    if not _MEMORY_KEY_ID_RE.fullmatch(str(head.integrity_key_id or "")):
        raise ValidationError({"integrity_key_id": "Invalid memory projection key id."})
    if head.current_fact_id:
        fact = head.current_fact
        expected_state = {
            "assert": "active", "invalidate": "invalidated", "expire": "expired",
        }.get(fact.operation)
        if (
            expected_state != head.state
            or fact.slot_key != head.slot_key
            or fact.client_id != head.client_id
            or fact.scope != head.scope
            or fact.commercial_episode_id != head.commercial_episode_id
            or fact.line_id != head.line_id
            or fact.order_id != head.order_id
            or fact.post_sale_case_id != head.post_sale_case_id
            or fact.fact_key != head.fact_key
            or fact.schema_version != head.schema_version
        ):
            raise ValidationError({"current_fact": "Memory head fact does not match its slot."})
        if head._state.adding and (
            head.revision != 1
            or fact.supersedes_id is not None
            or fact.operation != "assert"
        ):
            raise ValidationError({"current_fact": "Invalid initial memory head."})


class _IgMemoryHeadQuerySet(models.QuerySet):
    _MUTABLE = frozenset({
        "current_fact", "current_fact_id", "state", "revision",
        "projection_policy_version", "projection_hmac", "integrity_key_id",
        "projected_at", "updated_at",
    })

    def update(self, **kwargs):
        raise ValueError("IgMemoryHead transitions require the locked projector")

    def delete(self):
        raise ValueError("IgMemoryHead cannot be deleted outside privacy erasure")

    def bulk_create(self, objs, *args, **kwargs):
        for obj in objs:
            if obj.pk or not obj._state.adding:
                raise ValueError("IgMemoryHead identity is immutable")
            _validate_memory_head_payload(obj)
        return super().bulk_create(objs, *args, **kwargs)

    def bulk_update(self, objs, fields, batch_size=None):
        raise ValueError("IgMemoryHead transitions require the locked projector")


class IgMemoryHead(models.Model):
    class State(models.TextChoices):
        ACTIVE = "active", _("Active")
        INVALIDATED = "invalidated", _("Invalidated")
        EXPIRED = "expired", _("Expired")

    slot_key = models.CharField(max_length=80)
    client = models.ForeignKey(
        "management.IgClient", on_delete=models.DO_NOTHING,
        related_name="memory_heads", db_constraint=False,
    )
    scope = models.CharField(max_length=16, choices=IgMemoryFact.Scope.choices)
    commercial_episode = models.ForeignKey(
        "management.IgCommercialEpisode", null=True, blank=True,
        on_delete=models.DO_NOTHING, related_name="memory_heads", db_constraint=False,
    )
    line_id = models.CharField(max_length=96, blank=True, default="")
    order = models.ForeignKey(
        "orders.Order", null=True, blank=True, on_delete=models.DO_NOTHING,
        related_name="instagram_memory_heads", db_constraint=False,
    )
    post_sale_case = models.ForeignKey(
        "management.IgPostSaleCase", null=True, blank=True,
        on_delete=models.DO_NOTHING, related_name="memory_heads", db_constraint=False,
    )
    fact_key = models.CharField(max_length=32, choices=IgMemoryFact.FactKey.choices)
    schema_version = models.CharField(max_length=32, default=_MEMORY_SCHEMA_VERSION)
    current_fact = models.ForeignKey(
        "management.IgMemoryFact", on_delete=models.DO_NOTHING,
        related_name="current_for_heads", db_constraint=False,
    )
    state = models.CharField(max_length=16, choices=State.choices)
    revision = models.PositiveBigIntegerField(default=1)
    projection_policy_version = models.CharField(
        max_length=32, default=_MEMORY_POLICY_VERSION,
    )
    projection_hmac = models.CharField(max_length=64)
    integrity_key_id = models.CharField(max_length=32)
    projected_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    objects = _IgMemoryHeadQuerySet.as_manager()

    class Meta:
        base_manager_name = "objects"
        ordering = ["slot_key"]
        constraints = [
            models.UniqueConstraint(fields=["slot_key"], name="ig_memhead_slot_uniq"),
            models.UniqueConstraint(fields=["current_fact"], name="ig_memhead_fact_uniq"),
            models.CheckConstraint(
                condition=(
                    models.Q(revision__gte=1)
                    & models.Q(revision__lte=IG_MEMORY_MAX_CHAIN_DEPTH)
                ),
                name="ig_memhead_revision_bounds",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        scope="client", commercial_episode__isnull=True,
                        line_id="", order__isnull=True, post_sale_case__isnull=True,
                    )
                    | models.Q(
                        scope="episode", commercial_episode__isnull=False,
                        line_id="", order__isnull=True, post_sale_case__isnull=True,
                    )
                    | models.Q(
                        scope="line", commercial_episode__isnull=False,
                        order__isnull=True, post_sale_case__isnull=True,
                    ) & ~models.Q(line_id="")
                    | models.Q(
                        scope="order", commercial_episode__isnull=True,
                        line_id="", order__isnull=False, post_sale_case__isnull=True,
                    )
                    | models.Q(
                        scope="case", commercial_episode__isnull=True,
                        line_id="", order__isnull=True, post_sale_case__isnull=False,
                    )
                ),
                name="ig_memhead_scope_shape",
            ),
        ]
        indexes = [
            models.Index(fields=["client", "scope", "fact_key"], name="ig_memhead_client_scope"),
            models.Index(
                fields=["commercial_episode", "line_id", "fact_key"],
                name="ig_memhead_episode_line",
            ),
            models.Index(fields=["state", "-updated_at"], name="ig_memhead_state_updated"),
        ]

    def save(self, *args, **kwargs):
        _validate_memory_head_payload(self)
        if self.pk:
            previous = type(self)._base_manager.filter(pk=self.pk).values(
                "slot_key", "client_id", "scope", "commercial_episode_id",
                "line_id", "order_id", "post_sale_case_id", "fact_key",
                "schema_version", "current_fact_id", "state", "revision",
                "projected_at",
            ).first()
            if previous:
                for field_name in (
                    "slot_key", "client_id", "scope", "commercial_episode_id",
                    "line_id", "order_id", "post_sale_case_id", "fact_key",
                    "schema_version",
                ):
                    old_value = previous[field_name]
                    if getattr(self, field_name) != old_value:
                        raise ValueError("IgMemoryHead slot identity is immutable")
                if (
                    self.revision != int(previous["revision"] or 0) + 1
                    or self.current_fact_id == previous["current_fact_id"]
                    or self.projected_at < previous["projected_at"]
                    or self.current_fact.supersedes_id != previous["current_fact_id"]
                ):
                    raise ValueError("IgMemoryHead transition is not monotonic")
        elif (
            self.revision != 1
            or self.current_fact.supersedes_id is not None
            or self.current_fact.operation != IgMemoryFact.Operation.ASSERT
            or self.state != self.State.ACTIVE
        ):
            raise ValueError("IgMemoryHead initial projection is invalid")
        fact = self.current_fact
        expected_state = {
            "assert": self.State.ACTIVE,
            "invalidate": self.State.INVALIDATED,
            "expire": self.State.EXPIRED,
        }.get(fact.operation)
        if (
            expected_state != self.state
            or fact.slot_key != self.slot_key
            or fact.client_id != self.client_id
            or fact.scope != self.scope
            or fact.commercial_episode_id != self.commercial_episode_id
            or fact.line_id != self.line_id
            or fact.order_id != self.order_id
            or fact.post_sale_case_id != self.post_sale_case_id
            or fact.fact_key != self.fact_key
            or fact.schema_version != self.schema_version
        ):
            raise ValueError("IgMemoryHead current fact does not match its slot")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("IgMemoryHead cannot be deleted outside privacy erasure")


class _IgConversationAnalysisEventQuerySet(models.QuerySet):
    _IMMUTABLE_FIELDS = {
        "event_key",
        "client",
        "client_id",
        "snapshot",
        "snapshot_id",
        "event_type",
        "payload",
        "required_state_fingerprint",
        "source_digest",
    }

    def update(self, **kwargs):
        if self._IMMUTABLE_FIELDS.intersection(kwargs):
            raise ValueError("IgConversationAnalysisEvent identity is immutable")
        return super().update(**kwargs)

    def delete(self):
        raise ValueError("IgConversationAnalysisEvent identity is immutable")


class IgConversationAnalysisEvent(models.Model):
    """Typed operational proposal emitted by a completed analysis snapshot.

    The identity and payload are immutable after publication.  Only the
    deterministic consumer may advance the outcome fields, which keeps a
    provider result from mutating episodes, sessions or funnel state directly.
    """

    class EventType(models.TextChoices):
        REPEAT_EPISODE = "repeat_episode", _("Новий епізод повторного замовлення")

    class Status(models.TextChoices):
        PENDING = "pending", _("Очікує матеріалізації")
        APPLIED = "applied", _("Матеріалізовано")
        REJECTED = "rejected", _("Відхилено перевіркою")
        FAILED = "failed", _("Вичерпано повторні спроби")

    _IDENTITY_FIELDS = {
        "event_key",
        "client_id",
        "snapshot_id",
        "event_type",
        "payload",
        "required_state_fingerprint",
        "source_digest",
    }

    event_key = models.CharField(max_length=160, unique=True)
    client = models.ForeignKey(
        "management.IgClient",
        on_delete=models.DO_NOTHING,
        related_name="analysis_events",
        db_constraint=False,
    )
    snapshot = models.OneToOneField(
        "management.IgConversationAnalysisSnapshot",
        on_delete=models.DO_NOTHING,
        related_name="operational_event",
        db_constraint=False,
    )
    event_type = models.CharField(max_length=32, choices=EventType.choices)
    payload = models.JSONField(default=dict, blank=True)
    required_state_fingerprint = models.CharField(max_length=64, blank=True, default="")
    source_digest = models.CharField(max_length=64, blank=True, default="")
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    applied_episode = models.ForeignKey(
        "management.IgCommercialEpisode",
        null=True,
        blank=True,
        on_delete=models.DO_NOTHING,
        related_name="analysis_events",
        db_constraint=False,
    )
    attempts = models.PositiveSmallIntegerField(default=0)
    next_attempt_at = models.DateTimeField(
        null=True,
        blank=True,
        default=timezone.now,
        db_index=True,
    )
    last_error = models.CharField(max_length=1000, blank=True, default="")
    rejected_reason = models.CharField(max_length=120, blank=True, default="")
    applied_at = models.DateTimeField(null=True, blank=True)
    rejected_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Операційна подія аналізу IG")
        verbose_name_plural = _("Операційні події аналізу IG")
        ordering = ["created_at", "id"]
        indexes = [
            models.Index(
                fields=["status", "next_attempt_at", "id"],
                name="ig_analysis_event_due",
            ),
            models.Index(fields=["client", "status"], name="ig_analysis_event_client"),
        ]

    objects = models.Manager.from_queryset(_IgConversationAnalysisEventQuerySet)()

    def save(self, *args, **kwargs):
        if self.pk:
            previous = (
                type(self)
                .objects.filter(pk=self.pk)
                .values(*self._IDENTITY_FIELDS)
                .first()
            )
            if previous:
                for field_name in self._IDENTITY_FIELDS:
                    value = getattr(self, field_name)
                    if field_name.endswith("_id"):
                        value = getattr(self, field_name)
                    if value != previous[field_name]:
                        raise ValueError("IgConversationAnalysisEvent identity is immutable")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("IgConversationAnalysisEvent identity is immutable")

    def __str__(self) -> str:  # pragma: no cover - trivial representation
        return f"{self.event_type}:{self.event_key} ({self.status})"


class IgConversationAnalysisJob(models.Model):
    """One durable, coalescing high-reasoning analysis cursor per IG client."""

    class Status(models.TextChoices):
        PENDING = "pending", _("Очікує аналізу")
        PROCESSING = "processing", _("Аналізується")
        DONE = "done", _("Проаналізовано")
        FAILED = "failed", _("Помилка аналізу")
        SKIPPED = "skipped", _("Аналіз пропущено")

    class MediaPhase(models.TextChoices):
        NOT_STARTED = "not_started", _("Медіа не розпочато")
        ACQUIRING = "acquiring", _("Медіа обробляється")
        READY = "ready", _("Медіа готове")
        METADATA_ONLY = "metadata_only", _("Лише метадані")
        FAILED = "failed", _("Помилка медіа")

    client = models.OneToOneField(
        "management.IgClient",
        on_delete=models.CASCADE,
        related_name="analysis_job",
        db_constraint=False,
    )
    watermark_message_id = models.PositiveBigIntegerField(default=0)
    analyzed_watermark_message_id = models.PositiveBigIntegerField(default=0)
    revision = models.PositiveBigIntegerField(default=0)
    analyzed_revision = models.PositiveBigIntegerField(default=0)
    materiality_episode = models.ForeignKey(
        "management.IgCommercialEpisode",
        null=True,
        blank=True,
        on_delete=models.DO_NOTHING,
        related_name="analysis_materiality_jobs",
        db_constraint=False,
    )
    materiality_line_id = models.CharField(max_length=96, blank=True, default="")
    first_unanalysed_at = models.DateTimeField(null=True, blank=True)
    last_relevant_at = models.DateTimeField(null=True, blank=True)
    materiality_due_at = models.DateTimeField(null=True, blank=True)
    materiality_event_highwater = models.PositiveBigIntegerField(default=0)
    analyzed_materiality_event_highwater = models.PositiveBigIntegerField(default=0)
    materiality_digest = models.CharField(max_length=64, blank=True, default="")
    analyzed_materiality_digest = models.CharField(max_length=64, blank=True, default="")
    authority_digest = models.CharField(max_length=64, blank=True, default="")
    artifact_digest = models.CharField(max_length=64, blank=True, default="")
    claimed_materiality_event_highwater = models.PositiveBigIntegerField(default=0)
    claimed_materiality_digest = models.CharField(max_length=64, blank=True, default="")
    claimed_authority_digest = models.CharField(max_length=64, blank=True, default="")
    claimed_artifact_digest = models.CharField(max_length=64, blank=True, default="")
    claimed_watermark_message_id = models.PositiveBigIntegerField(default=0)
    claimed_revision = models.PositiveBigIntegerField(default=0)
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.PENDING, db_index=True
    )
    due_at = models.DateTimeField(default=timezone.now, db_index=True)
    next_attempt_at = models.DateTimeField(default=timezone.now, db_index=True)
    lease_token = models.CharField(max_length=40, blank=True, default="")
    lease_until = models.DateTimeField(null=True, blank=True, db_index=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    last_error = models.CharField(max_length=1000, blank=True, default="")
    skip_reason = models.CharField(max_length=64, blank=True, default="")
    trigger = models.CharField(max_length=32, blank=True, default="message")
    analysis_model = models.CharField(max_length=80, blank=True, default="")
    analysis_prompt_version = models.CharField(max_length=40, blank=True, default="")
    required_state_fingerprint = models.CharField(max_length=64, blank=True, default="")
    key_alias = models.CharField(max_length=32, blank=True, default="")
    reasoning_task = models.CharField(max_length=64, blank=True, default="")
    reasoning_level = models.CharField(max_length=16, blank=True, default="")
    reasoning_policy_version = models.CharField(max_length=32, blank=True, default="")
    thoughts_tokens = models.PositiveIntegerField(default=0)
    candidates_tokens = models.PositiveIntegerField(default=0)
    analysis_latency_ms = models.PositiveIntegerField(default=0)
    media_phase = models.CharField(
        max_length=24,
        choices=MediaPhase.choices,
        default=MediaPhase.NOT_STARTED,
        db_index=True,
    )
    media_error_kind = models.CharField(max_length=64, blank=True, default="")
    media_started_at = models.DateTimeField(null=True, blank=True)
    media_completed_at = models.DateTimeField(null=True, blank=True)
    media_item_count = models.PositiveSmallIntegerField(default=0)
    analyzed_at = models.DateTimeField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Завдання аналізу IG-діалогу")
        verbose_name_plural = _("Завдання аналізу IG-діалогів")
        ordering = ["due_at", "id"]
        indexes = [
            models.Index(fields=["status", "next_attempt_at", "due_at"], name="ig_analysis_job_due"),
            models.Index(
                fields=["materiality_due_at", "id"],
                name="ig_analysis_mat_due",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial representation
        return f"{self.client_id}: {self.status}@{self.watermark_message_id}"


class IgAiReplyRecoveryJob(models.Model):
    """One non-replayable recovery intent for a failed customer AI turn.

    A recovery is deliberately separate from sales follow-ups and conversation
    analysis: it exists only to complete one specific inbound turn after a
    transient provider failure.  ``SENDING`` means the Meta request boundary
    was crossed.  It is never retried automatically without a provider receipt.
    """

    class Status(models.TextChoices):
        PENDING = "pending", _("Очікує відновлення")
        PROCESSING = "processing", _("Готується відповідь")
        SENDING = "sending", _("Надсилається")
        SENT = "sent", _("Надіслано")
        CANCELLED = "cancelled", _("Скасовано")
        AMBIGUOUS = "ambiguous", _("Невідомий результат")
        FAILED = "failed", _("Помилка відновлення")

    source_message = models.OneToOneField(
        "management.InstagramBotMessage",
        on_delete=models.CASCADE,
        related_name="ai_reply_recovery_job",
        db_constraint=False,
    )
    client = models.ForeignKey(
        "management.IgClient",
        on_delete=models.CASCADE,
        related_name="ai_reply_recovery_jobs",
        db_constraint=False,
    )
    # The generic outage holding reply is allowed to precede the recovery.  A
    # later substantive model reply still cancels this stale intent.
    holding_message = models.OneToOneField(
        "management.InstagramBotMessage",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="ai_recovery_holding_for",
        db_constraint=False,
    )
    # The row is created before the Meta request.  It makes the exact draft
    # visible for manual reconciliation even if the process dies during I/O.
    reply_message = models.OneToOneField(
        "management.InstagramBotMessage",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="ai_recovery_reply_for",
        db_constraint=False,
    )
    dedupe_key = models.CharField(max_length=160, unique=True)
    # Епізод деградації (ЭА.7): курсор відновлення живе на парі
    # (клієнт, інцидент), а не на кожному source-повідомленні. Три вхідні під
    # час одного інциденту раніше давали три job'и й кілька відповідей клієнту.
    degradation_episode = models.ForeignKey(
        "management.IgClientDegradationEpisode",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="recovery_jobs",
        db_constraint=False,
    )
    superseded_by = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="superseded_jobs",
        db_constraint=False,
    )
    # «Один активний курсор на (клієнт, інцидент)» тримається в БД через
    # nullable unique-колонку: активний job має ключ, терминальний — NULL.
    # Часткові unique-індекси MariaDB не підтримує, тому це єдиний портативний
    # спосіб отримати справжню гарантію, а не перевірку в коді.
    active_cursor_key = models.CharField(
        max_length=80, null=True, blank=True, unique=True
    )
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.PENDING, db_index=True
    )
    draft_text = models.TextField(blank=True, default="")
    routing_decision = models.JSONField(default=dict, blank=True)
    provider_message_id = models.CharField(max_length=255, blank=True, default="", db_index=True)
    settings_permission_epoch = models.PositiveBigIntegerField(default=0)
    client_permission_epoch = models.PositiveBigIntegerField(default=0)
    message_floor = models.PositiveBigIntegerField(default=0)
    response_window_deadline = models.DateTimeField(null=True, blank=True, db_index=True)
    # Created before the holding Meta send, then explicitly armed only after
    # its provider receipt is persisted. A prepared job is never worker-due.
    activated_at = models.DateTimeField(null=True, blank=True, db_index=True)
    next_attempt_at = models.DateTimeField(null=True, blank=True, db_index=True)
    lease_token = models.CharField(max_length=40, blank=True, default="")
    lease_until = models.DateTimeField(null=True, blank=True, db_index=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    last_error = models.CharField(max_length=1000, blank=True, default="")
    sending_started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Відновлення AI-відповіді IG")
        verbose_name_plural = _("Відновлення AI-відповідей IG")
        ordering = ["id"]
        indexes = [
            models.Index(fields=["status", "response_window_deadline"], name="ig_ai_recovery_due"),
            models.Index(
                fields=["status", "activated_at", "next_attempt_at"],
                name="ig_ai_recovery_ready",
            ),
            models.Index(fields=["client", "status"], name="ig_ai_recovery_client"),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial representation
        return f"recovery:{self.source_message_id}:{self.status}"


class IgPermissionTransitionJob(models.Model):
    """Durable fail-closed pause/takeover/opt-out transition."""

    class Kind(models.TextChoices):
        OPT_OUT = "opt_out", _("Відмова від повідомлень")
        MANAGER_TAKEOVER = "manager_takeover", _("Діалог веде менеджер")
        CLIENT_PAUSE = "client_pause", _("Ручна пауза для клієнта")
        GLOBAL_PAUSE = "global_pause", _("Глобальна пауза бота")

    class Status(models.TextChoices):
        PENDING = "pending", _("Очікує застосування")
        PROCESSING = "processing", _("Застосовується")
        APPLIED = "applied", _("Застосовано")
        SUPERSEDED = "superseded", _("Вже не актуально")
        FAILED = "failed", _("Потрібне відновлення")

    kind = models.CharField(max_length=32, choices=Kind.choices, db_index=True)
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    client = models.ForeignKey(
        "management.IgClient",
        null=True,
        blank=True,
        on_delete=models.DO_NOTHING,
        related_name="permission_transition_jobs",
        db_constraint=False,
    )
    settings = models.ForeignKey(
        "management.InstagramBotSettings",
        null=True,
        blank=True,
        on_delete=models.DO_NOTHING,
        related_name="permission_transition_jobs",
        db_constraint=False,
    )
    source_message = models.ForeignKey(
        "management.InstagramBotMessage",
        null=True,
        blank=True,
        on_delete=models.DO_NOTHING,
        related_name="permission_transition_jobs",
        db_constraint=False,
    )
    dedupe_key = models.CharField(max_length=255, unique=True)
    next_attempt_at = models.DateTimeField(null=True, blank=True, default=timezone.now)
    lease_token = models.CharField(max_length=40, blank=True, default="")
    lease_until = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    last_error_kind = models.CharField(max_length=64, blank=True, default="")
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["id"]
        indexes = [
            models.Index(
                fields=["status", "next_attempt_at", "id"],
                name="ig_perm_transition_due",
            ),
            models.Index(
                fields=["client", "status"],
                name="ig_perm_transition_client",
            ),
            models.Index(fields=["lease_until"], name="ig_perm_transition_lease"),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial representation
        return f"permission:{self.kind}:{self.status}:{self.pk}"


class IgCommerceSelectionSession(models.Model):
    """Authoritative, reversible product-selection state for one sales episode."""

    class State(models.TextChoices):
        OPEN = "open", _("Відкрита")
        CLOSED = "closed", _("Закрита")

    client = models.ForeignKey(
        "management.IgClient",
        on_delete=models.DO_NOTHING,
        related_name="commerce_selection_sessions",
        db_constraint=False,
    )
    commercial_episode = models.ForeignKey(
        "management.IgCommercialEpisode",
        null=True,
        blank=True,
        on_delete=models.DO_NOTHING,
        related_name="commerce_selection_sessions",
        db_constraint=False,
    )
    generation = models.PositiveIntegerField()
    # MariaDB permits multiple NULL values in a unique key, so only the open
    # row occupies slot 1 while historical generations use NULL.
    open_slot = models.PositiveSmallIntegerField(null=True, blank=True, default=1)
    state = models.CharField(
        max_length=16,
        choices=State.choices,
        default=State.OPEN,
        db_index=True,
    )
    lines = models.JSONField(default=list, blank=True)
    active_index = models.PositiveIntegerField(default=0)
    selection_constraints = models.JSONField(default=dict, blank=True)
    query_constraints = models.JSONField(default=dict, blank=True)
    candidate_product_ids = models.JSONField(default=list, blank=True)
    candidate_digest = models.CharField(max_length=64, blank=True, default="")
    candidate_generation = models.PositiveIntegerField(default=0)
    candidate_prompt_provider_ids = models.JSONField(default=list, blank=True)
    rejected_selection = models.JSONField(default=dict, blank=True)
    rejected_reason = models.CharField(max_length=120, blank=True, default="")
    pending_field = models.CharField(max_length=80, blank=True, default="")
    pending_clarification = models.CharField(max_length=120, blank=True, default="")
    semantic_block_key = models.CharField(max_length=160, blank=True, default="", db_index=True)
    graph_digest = models.CharField(max_length=64, blank=True, default="")
    last_provider_event_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_provider_message_id = models.CharField(max_length=255, blank=True, default="")
    revision = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["client_id", "-generation"]
        constraints = [
            models.UniqueConstraint(
                fields=["client", "generation"],
                name="ig_commerce_client_generation",
            ),
            models.UniqueConstraint(
                fields=["client", "open_slot"],
                name="ig_commerce_one_open_slot",
            ),
            models.CheckConstraint(
                condition=models.Q(open_slot__isnull=True) | models.Q(open_slot=1),
                name="ig_commerce_open_slot_valid",
            ),
        ]
        indexes = [
            models.Index(
                fields=["client", "state", "-generation"],
                name="ig_com_sess_client_state",
            ),
            models.Index(fields=["state", "-updated_at"], name="ig_com_sess_state_dt"),
        ]

    def snapshot(self) -> dict:
        return {
            "generation": self.generation,
            "state": self.state,
            "lines": list(self.lines or []),
            "active_index": int(self.active_index or 0),
            "selection_constraints": dict(self.selection_constraints or {}),
            "query_constraints": dict(self.query_constraints or {}),
            "candidate_product_ids": list(self.candidate_product_ids or []),
            "candidate_digest": self.candidate_digest or "",
            "candidate_generation": int(self.candidate_generation or 0),
            "candidate_prompt_provider_ids": list(self.candidate_prompt_provider_ids or []),
            "rejected_selection": dict(self.rejected_selection or {}),
            "rejected_reason": self.rejected_reason or "",
            "pending_field": self.pending_field or "",
            "pending_clarification": self.pending_clarification or "",
            "semantic_block_key": self.semantic_block_key or "",
            "graph_digest": self.graph_digest or "",
            "last_provider_event_at": (
                self.last_provider_event_at.isoformat() if self.last_provider_event_at else None
            ),
            "last_provider_message_id": self.last_provider_message_id or "",
            "revision": int(self.revision or 0),
        }


class _AppendOnlyCommerceTransitionQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValueError("IgCommerceSelectionTransition is append-only")

    def delete(self):
        raise ValueError("IgCommerceSelectionTransition is append-only")


class IgCommerceSelectionTransition(models.Model):
    """Append-only state change with complete before/after evidence."""

    session = models.ForeignKey(
        "management.IgCommerceSelectionSession",
        on_delete=models.CASCADE,
        related_name="transitions",
    )
    source_message = models.OneToOneField(
        "management.InstagramBotMessage",
        on_delete=models.DO_NOTHING,
        related_name="commerce_selection_transition",
        db_constraint=False,
    )
    action = models.CharField(max_length=80, db_index=True)
    from_revision = models.PositiveIntegerField()
    to_revision = models.PositiveIntegerField()
    previous_snapshot = models.JSONField(default=dict)
    next_snapshot = models.JSONField(default=dict)
    effects = models.JSONField(default=dict, blank=True)
    reasons = models.JSONField(default=list, blank=True)
    graph_digest = models.CharField(max_length=64, blank=True, default="")
    source_order_key = models.CharField(max_length=360, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    objects = _AppendOnlyCommerceTransitionQuerySet.as_manager()

    class Meta:
        ordering = ["session_id", "to_revision", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["session", "to_revision"],
                name="ig_commerce_session_revision",
            )
        ]
        indexes = [
            models.Index(fields=["session", "-created_at"], name="ig_com_trans_session_dt"),
            models.Index(fields=["action", "-created_at"], name="ig_com_trans_action_dt"),
        ]

    def save(self, *args, **kwargs):
        if self.pk and type(self).objects.filter(pk=self.pk).exists():
            raise ValueError("IgCommerceSelectionTransition is append-only")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("IgCommerceSelectionTransition is append-only")


class _CommerceDecisionQuerySet(models.QuerySet):
    mutable_fields = frozenset({
        "delivery_state",
        "attempts",
        "delivery_started_at",
        "last_attempt_at",
        "delivered_at",
        "delivery_error",
        "text_receipts",
        "media_receipts",
        "provider_message_ids",
        "reconciliation_status",
        "reconciliation_result",
        "reconciliation_evidence",
        "updated_at",
    })

    def update(self, **kwargs):
        immutable = set(kwargs) - self.mutable_fields
        if immutable:
            raise ValueError("IgCommerceTurnDecision identity is immutable")
        return super().update(**kwargs)

    def delete(self):
        raise ValueError("IgCommerceTurnDecision is durable")


class IgCommerceTurnDecision(models.Model):
    """One immutable reduction result plus a separately mutable delivery outbox."""

    class DeliveryState(models.TextChoices):
        PENDING = "pending", _("Очікує")
        SENDING = "sending", _("Надсилається")
        UNKNOWN = "unknown", _("Результат невідомий")
        PARTIAL = "partial", _("Частково надіслано")
        SENT = "sent", _("Надіслано")
        NOT_REQUIRED = "not_required", _("Доставка не потрібна")

    class ReconciliationStatus(models.TextChoices):
        NOT_REQUIRED = "not_required", _("Не потрібне")
        REQUIRED = "required", _("Потрібне")
        PENDING = "pending", _("Очікує")
        RECONCILED = "reconciled", _("Звірено")
        MANAGER_REVIEW = "manager_review", _("Перевірка менеджера")

    source_message = models.OneToOneField(
        "management.InstagramBotMessage",
        on_delete=models.DO_NOTHING,
        related_name="commerce_turn_decision",
        db_constraint=False,
    )
    session = models.ForeignKey(
        "management.IgCommerceSelectionSession",
        on_delete=models.CASCADE,
        related_name="decisions",
    )
    transition = models.ForeignKey(
        "management.IgCommerceSelectionTransition",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="decisions",
    )
    request_payload = models.JSONField(default=dict)
    result_payload = models.JSONField(default=dict)
    reply_payload = models.JSONField(default=dict)
    effects_payload = models.JSONField(default=dict)
    accepted = models.BooleanField(default=True)
    is_stale = models.BooleanField(default=False, db_index=True)
    delivery_required = models.BooleanField(default=True)
    delivery_state = models.CharField(
        max_length=16,
        choices=DeliveryState.choices,
        default=DeliveryState.PENDING,
        db_index=True,
    )
    attempts = models.PositiveIntegerField(default=0)
    delivery_started_at = models.DateTimeField(null=True, blank=True)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    delivery_error = models.CharField(max_length=1000, blank=True, default="")
    text_receipts = models.JSONField(default=list, blank=True)
    media_receipts = models.JSONField(default=list, blank=True)
    provider_message_ids = models.JSONField(default=list, blank=True)
    reconciliation_status = models.CharField(
        max_length=24,
        choices=ReconciliationStatus.choices,
        default=ReconciliationStatus.NOT_REQUIRED,
        db_index=True,
    )
    reconciliation_result = models.JSONField(default=dict, blank=True)
    reconciliation_evidence = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = _CommerceDecisionQuerySet.as_manager()

    _immutable_fields = (
        "source_message_id",
        "session_id",
        "transition_id",
        "request_payload",
        "result_payload",
        "reply_payload",
        "effects_payload",
        "accepted",
        "is_stale",
        "delivery_required",
    )

    class Meta:
        ordering = ["id"]
        indexes = [
            models.Index(
                fields=["delivery_state", "created_at"],
                name="ig_com_decision_delivery",
            ),
            models.Index(fields=["session", "-created_at"], name="ig_com_decision_session"),
            models.Index(
                fields=["reconciliation_status", "-updated_at"],
                name="ig_com_decision_recon",
            ),
        ]

    def save(self, *args, **kwargs):
        if self.pk:
            stored = type(self).objects.filter(pk=self.pk).values(*self._immutable_fields).first()
            if stored and any(stored[name] != getattr(self, name) for name in self._immutable_fields):
                raise ValueError("IgCommerceTurnDecision identity is immutable")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("IgCommerceTurnDecision is durable")


class IgCommerceManagerReview(models.Model):
    """SLA-backed manager recovery for ambiguous or unreconciled decisions."""

    class Status(models.TextChoices):
        OPEN = "open", _("Відкрита")
        CLAIMED = "claimed", _("В роботі")
        RESOLVED = "resolved", _("Вирішена")
        CANCELLED = "cancelled", _("Скасована")

    idempotency_key = models.CharField(max_length=180, unique=True)
    client = models.ForeignKey(
        "management.IgClient",
        on_delete=models.DO_NOTHING,
        related_name="commerce_manager_reviews",
        db_constraint=False,
    )
    session = models.ForeignKey(
        "management.IgCommerceSelectionSession",
        on_delete=models.CASCADE,
        related_name="manager_reviews",
    )
    decision = models.ForeignKey(
        "management.IgCommerceTurnDecision",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="manager_reviews",
    )
    reason = models.CharField(max_length=120, db_index=True)
    selection_snapshot = models.JSONField(default=dict, blank=True)
    selection_digest = models.CharField(max_length=64, blank=True, default="")
    selection_generation = models.PositiveIntegerField(default=0)
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.OPEN,
        db_index=True,
    )
    due_at = models.DateTimeField(db_index=True)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.DO_NOTHING,
        related_name="ig_commerce_reviews_owned",
        db_constraint=False,
    )
    claimed_at = models.DateTimeField(null=True, blank=True)
    claim_lease_token = models.CharField(max_length=64, blank=True, default="")
    claim_lease_until = models.DateTimeField(null=True, blank=True, db_index=True)
    resolution_payload = models.JSONField(default=dict, blank=True)
    resolution_note = models.CharField(max_length=1000, blank=True, default="")
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.DO_NOTHING,
        related_name="ig_commerce_reviews_resolved",
        db_constraint=False,
    )
    resolved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["due_at", "id"]
        indexes = [
            models.Index(
                fields=["status", "due_at", "id"],
                name="ig_com_review_status_due",
            ),
            models.Index(
                fields=["owner", "status", "due_at"],
                name="ig_com_review_owner_due",
            ),
            models.Index(fields=["client", "-created_at"], name="ig_com_review_client_dt"),
        ]


class IgMetaEventLog(models.Model):
    """Audit log for safe Meta CAPI feedback attempts from IG Direct funnel."""

    class Status(models.TextChoices):
        DISABLED = "disabled", _("Вимкнено")
        SKIPPED = "skipped", _("Пропущено")
        SENT = "sent", _("Надіслано")
        FAILED = "failed", _("Помилка")

    event_name = models.CharField(max_length=80, db_index=True)
    event_id = models.CharField(max_length=120, blank=True, default="", db_index=True)
    client = models.ForeignKey(
        "management.IgClient", null=True, blank=True, on_delete=models.SET_NULL, related_name="meta_events"
    )
    deal = models.ForeignKey(
        "management.IgDeal", null=True, blank=True, on_delete=models.SET_NULL, related_name="meta_events"
    )
    order = models.ForeignKey(
        "orders.Order", null=True, blank=True, on_delete=models.SET_NULL, related_name="ig_meta_events"
    )
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.SKIPPED, db_index=True)
    reason = models.CharField(max_length=255, blank=True, default="")
    response_payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = _("IG Meta event log")
        verbose_name_plural = _("IG Meta event logs")
        ordering = ["-id"]
        indexes = [
            models.Index(fields=["event_name", "-id"], name="ig_meta_event_id"),
            models.Index(fields=["status", "-id"], name="ig_meta_status_id"),
        ]

    def __str__(self) -> str:  # pragma: no cover - тривіально
        return f"{self.event_name}:{self.status}"


class IgProviderIncident(models.Model):
    """Одна durable деградація провайдера, а не окремий збій запиту.

    Система вміла бачити окремі збої (`GeminiRequestAttempt`, `GeminiKeyState`),
    але не вміла відповісти на питання «чи вже триває інцидент?». Через це кожен
    новий вхідний починав міркування з нуля й отримував власне технічне
    вибачення. Інцидент — це стан ПРОВАЙДЕРА; стан конкретного клієнта в межах
    інциденту описує `IgClientDegradationEpisode`.

    Fingerprint — це `role + failure_class`. Область (`scope`: модель, ключ,
    project-group) свідомо НЕ входить у ключ: клієнту байдуже, який саме ключ
    впав, йому важливо, що бот молчить. Області накопичуються в
    `observed_scopes`, щоб інцидент по шести алиасах залишався одним інцидентом.
    """

    class State(models.TextChoices):
        OPEN = "open", _("Відкритий")
        RECOVERING = "recovering", _("Відновлюється")
        CLOSED = "closed", _("Закритий")

    class FailureClass(models.TextChoices):
        QUOTA = "quota", _("Квота (429)")
        UNAVAILABLE = "unavailable", _("Недоступність (5xx)")
        TIMEOUT = "timeout", _("Таймаут читання")
        CONNECT = "connect", _("Транспорт/з'єднання")
        INVALID_PAYLOAD = "invalid_payload", _("Некоректний запит (400)")
        AUTH = "auth", _("Ключ/доступ (401/403)")
        EMPTY = "empty", _("Порожня відповідь")
        UNKNOWN = "unknown", _("Інше")

    role = models.CharField(max_length=20, db_index=True)
    failure_class = models.CharField(
        max_length=20, choices=FailureClass.choices, default=FailureClass.UNKNOWN
    )
    fingerprint = models.CharField(max_length=120, db_index=True)
    # MariaDB не підтримує частковий unique-індекс, тому «один відкритий
    # інцидент на fingerprint» тримається на nullable unique-колонці: активний
    # інцидент має значення, закритий — NULL (MySQL/MariaDB і SQLite дозволяють
    # багато NULL у unique-індексі). Це справжня гарантія в БД, а не в коді.
    active_fingerprint = models.CharField(
        max_length=120, null=True, blank=True, unique=True
    )
    state = models.CharField(
        max_length=12, choices=State.choices, default=State.OPEN, db_index=True
    )
    opened_at = models.DateTimeField(default=timezone.now, db_index=True)
    last_failure_at = models.DateTimeField(null=True, blank=True, db_index=True)
    first_success_after_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True, db_index=True)
    close_reason = models.CharField(max_length=32, blank=True, default="")
    observed_scopes = models.JSONField(default=list, blank=True)
    failure_count = models.PositiveIntegerField(default=0)
    success_count = models.PositiveIntegerField(default=0)
    consecutive_success_count = models.PositiveSmallIntegerField(default=0)
    affected_clients_count = models.PositiveIntegerField(default=0)
    holding_sent_count = models.PositiveIntegerField(default=0)
    # Обчислюється для агрегації алертів менеджеру; НЕ використовується для
    # рішень, видимих клієнту.
    severity = models.PositiveSmallIntegerField(default=1)
    manager_alert_sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Інцидент провайдера IG")
        verbose_name_plural = _("Інциденти провайдера IG")
        ordering = ["-id"]
        indexes = [
            models.Index(fields=["state", "-last_failure_at"], name="ig_incident_state_recent"),
            models.Index(fields=["role", "state", "-id"], name="ig_incident_role_state"),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial representation
        return f"incident:{self.role}:{self.failure_class}:{self.state}"

    @property
    def is_active(self) -> bool:
        return self.state in {self.State.OPEN, self.State.RECOVERING}


class IgClientDegradationEpisode(models.Model):
    """Стан одного клієнта в межах одного інциденту провайдера.

    Одиниця «не більше одного технічного повідомлення» — це пара
    (інцидент, клієнт), а НЕ `source_message_id`. Старий dedupe по source
    формально проходив власний тест і при цьому давав клієнту три однакові
    вибачення за 6 хвилин: три різні вхідні — це три різні source_id.

    Діаграма станів:
        OPEN → HOLDING_SENT → RECOVERY_PENDING → RECOVERED
    гілки: → MANUAL (takeover/виснаження), → SUPERSEDED (новий інцидент),
            → CANCELLED (opt-out, hidden, зміна epoch).
    """

    class State(models.TextChoices):
        OPEN = "open", _("Відкритий")
        HOLDING_SENT = "holding_sent", _("Холдинг надіслано")
        RECOVERY_PENDING = "recovery_pending", _("Готується відновлення")
        RECOVERED = "recovered", _("Відновлено")
        MANUAL = "manual", _("Передано менеджеру")
        SUPERSEDED = "superseded", _("Витіснено")
        CANCELLED = "cancelled", _("Скасовано")

    _TERMINAL_STATES = frozenset({
        State.RECOVERED, State.MANUAL, State.SUPERSEDED, State.CANCELLED,
    })

    client = models.ForeignKey(
        "management.IgClient",
        on_delete=models.CASCADE,
        related_name="degradation_episodes",
        db_constraint=False,
    )
    incident = models.ForeignKey(
        "management.IgProviderIncident",
        on_delete=models.CASCADE,
        related_name="client_episodes",
        db_constraint=False,
    )
    state = models.CharField(
        max_length=20, choices=State.choices, default=State.OPEN, db_index=True
    )
    # Рівно один holding на епізод; O2O тримає цю інваріанту в БД.
    holding_message = models.OneToOneField(
        "management.InstagramBotMessage",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="degradation_episode_holding",
        db_constraint=False,
    )
    first_source_message_id = models.PositiveBigIntegerField(default=0)
    latest_source_message_id = models.PositiveBigIntegerField(default=0, db_index=True)
    logical_turn_id = models.CharField(max_length=64, blank=True, default="", db_index=True)
    holding_sent_at = models.DateTimeField(null=True, blank=True)
    holding_reserved_at = models.DateTimeField(null=True, blank=True)
    inbound_count = models.PositiveIntegerField(default=0)
    suppressed_count = models.PositiveIntegerField(default=0)
    # Фактично надіслані клієнту вибачення в межах цього епізоду. Рахуємо по
    # факту тексту, а не по флагах: holding і recovery складаються разом.
    apology_count = models.PositiveSmallIntegerField(default=0)
    last_decision = models.CharField(max_length=24, blank=True, default="")
    last_decision_reason = models.CharField(max_length=48, blank=True, default="")
    resolved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Епізод деградації клієнта IG")
        verbose_name_plural = _("Епізоди деградації клієнтів IG")
        ordering = ["-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["client", "incident"], name="ig_degradation_episode_unique"
            ),
        ]
        indexes = [
            models.Index(fields=["client", "state", "-id"], name="ig_degradation_client"),
            models.Index(fields=["incident", "state"], name="ig_degradation_incident"),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial representation
        return f"degradation:{self.client_id}:{self.incident_id}:{self.state}"

    @property
    def is_terminal(self) -> bool:
        return self.state in self._TERMINAL_STATES

    @property
    def holding_delivered(self) -> bool:
        return bool(self.holding_sent_at and self.holding_message_id)


class IgCustomerTurn(models.Model):
    """Один логічний хід клієнта — спільне поняття для трьох механізмів (Э0.6).

    Три різні механізми потребують поняття «один логічний хід»: склейка burst-у
    (Э2.2), дедуплікація webhook без `mid` (Э2.11) і provenance відповіді (Э3.6).
    Роздільні реалізації дали б три неузгоджені механізми дедуплікації, а
    найгірший випадок — burst із двох повідомлень, де одне задубльоване
    провайдером, — не обробив би жоден з них правильно.

    **Обмеження схеми, яке не можна обійти.** `IgCommerceSelectionTransition` і
    `IgCommerceTurnDecision` тримають `OneToOneField(source_message)`. Якщо хід
    об'єднує кілька повідомлень, а decision вимагає ОДНУ строку, схема стала б
    суперечливою. Тому `primary_source_message` обов'язковий: він зберігає
    сумісність з наявними OneToOne, поки вони не переведені на хід.
    """

    class ClaimState(models.TextChoices):
        OPEN = "open", _("Відкритий")
        CLAIMED = "claimed", _("Захоплений")
        PROCESSED = "processed", _("Опрацьований")
        SUPERSEDED = "superseded", _("Витіснений")

    class TerminalReason(models.TextChoices):
        """Чому хід став терміналом (Э2.2B Phase 1).

        Без причини `PROCESSED` неможливо відрізнити «відповіли клієнту» від
        «рядок зник» і від «lease застарів». Приймальний критерій ЭА/Э2.2B
        сформульований саме так: немає жодного застарілого `CLAIMED` **без
        класифікованої причини**, тому причина мусить бути durable полем, а не
        рядком у лозі.
        """

        REPLIED = "replied", _("Відповідь доставлена")
        NO_REPLY_NEEDED = "no_reply_needed", _("Відповідь не потрібна")
        SEND_UNKNOWN = "send_unknown", _("Відправка невідома")
        FAILED = "failed", _("Виконання не вдалося")
        ROW_MISSING = "row_missing", _("Рядок ходу зник")
        LEASE_EXPIRED = "lease_expired", _("Lease застарів")
        SUPERSEDED = "superseded", _("Витіснений новішим ходом")

    client = models.ForeignKey(
        "management.IgClient",
        on_delete=models.CASCADE,
        related_name="customer_turns",
        db_constraint=False,
    )
    episode = models.ForeignKey(
        "management.IgCommercialEpisode",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="customer_turns",
        db_constraint=False,
    )
    # Перше повідомлення ходу. Саме воно підставляється в наявні OneToOne-контракти.
    primary_source_message = models.OneToOneField(
        "management.InstagramBotMessage",
        on_delete=models.CASCADE,
        related_name="primary_customer_turn",
        db_constraint=False,
    )
    window_started_at = models.DateTimeField(default=timezone.now, db_index=True)
    # Дедлайн фіксується від ПЕРШОГО повідомлення і не продовжується наступними:
    # інакше клієнт, який продовжує писати, ніколи не отримав би відповіді.
    window_deadline = models.DateTimeField(db_index=True)
    claim_state = models.CharField(
        max_length=12, choices=ClaimState.choices, default=ClaimState.OPEN, db_index=True
    )
    claimed_at = models.DateTimeField(null=True, blank=True)
    claim_token = models.CharField(max_length=40, blank=True, default="")
    message_count = models.PositiveIntegerField(default=1)
    # Ключі ідентичності повідомлень ходу: native `mid`, provider object id
    # вкладення, або синтетичний ключ. Кілька типів свідомо в одному списку —
    # дедуплікація мусить працювати по будь-якому доступному.
    dedupe_keys = models.JSONField(default=list, blank=True)
    # Postback і quick reply обходять debounce: кнопка — завершена дія, а не
    # частина набору тексту. Це поле, а не комментар у коді.
    bypass_debounce = models.BooleanField(default=False)
    processed_at = models.DateTimeField(null=True, blank=True)
    # Класифікована причина терміналізації. Порожньо тільки для ходів, які ще
    # не завершені, і для історичних рядків до цієї міграції.
    terminal_reason = models.CharField(
        max_length=20,
        choices=TerminalReason.choices,
        blank=True,
        default="",
        db_index=True,
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Хід клієнта IG")
        verbose_name_plural = _("Ходи клієнтів IG")
        ordering = ["-id"]
        indexes = [
            models.Index(fields=["client", "claim_state", "-id"], name="ig_turn_client_state"),
            models.Index(fields=["claim_state", "window_deadline"], name="ig_turn_due"),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial representation
        return f"turn:{self.client_id}:{self.pk}:{self.claim_state}"

    @property
    def is_open(self) -> bool:
        return self.claim_state == self.ClaimState.OPEN


class IgTurnMessage(models.Model):
    """Append-only зв'язок «хід → повідомлення».

    Сирі повідомлення НЕ видаляються і не зливаються: кожна строка лишається як
    evidence у CRM. Хід лише групує їх.
    """

    turn = models.ForeignKey(
        "management.IgCustomerTurn",
        on_delete=models.CASCADE,
        related_name="turn_messages",
        db_constraint=False,
    )
    # Одне повідомлення належить рівно одному ходу. Унікальність у БД — це і є
    # захист від того, що дублюючий webhook додасть другу строку.
    message = models.OneToOneField(
        "management.InstagramBotMessage",
        on_delete=models.CASCADE,
        related_name="turn_membership",
        db_constraint=False,
    )
    ordinal = models.PositiveSmallIntegerField(default=1)
    role = models.CharField(max_length=8, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = _("Повідомлення ходу IG")
        verbose_name_plural = _("Повідомлення ходів IG")
        ordering = ["turn_id", "ordinal", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["turn", "ordinal"], name="ig_turn_message_ordinal_unique"
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial representation
        return f"turn_message:{self.turn_id}:{self.ordinal}"


class _ImmutableTurnRevisionSourceQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValueError("IgTurnRevisionSource is append-only")

    def delete(self):
        raise ValueError("IgTurnRevisionSource is append-only")


class _IgCustomerTurnRevisionQuerySet(models.QuerySet):
    def update(self, **kwargs):
        if set(kwargs).intersection({
            "generation_proposal", "generation_proposal_digest",
            "generation_proposed_at", "action_receipts",
        }):
            raise ValueError("revision generation proposal is immutable")
        return super().update(**kwargs)


class _IgDeferredEchoQuerySet(models.QuerySet):
    def update(self, **kwargs):
        if set(kwargs).intersection((*IgDeferredEcho._IMMUTABLE_FIELDS, "client")):
            raise ValueError("deferred echo identity is immutable")
        return super().update(**kwargs)


class IgDeferredEcho(models.Model):
    """An exact echo held until competing provider requests have attribution."""

    class State(models.TextChoices):
        WAITING_RECEIPT = "waiting_receipt", _("Очікує квитанцію")
        AMBIGUOUS = "ambiguous", _("Потрібна звірка")
        OWN = "own", _("Підтверджене власне повідомлення")
        MANAGER_PENDING = "manager_pending", _("Очікує запису менеджера")
        MANAGER_APPLIED = "manager_applied", _("Менеджера зафіксовано")

    client = models.ForeignKey("management.IgClient", on_delete=models.CASCADE, related_name="deferred_echoes", db_constraint=False)
    settings_id_snapshot = models.PositiveIntegerField()
    provider_namespace = models.CharField(max_length=128)
    recipient_igsid = models.CharField(max_length=64)
    provider_message_id = models.CharField(max_length=255)
    payload = models.JSONField(default=dict)
    event_digest = models.CharField(max_length=64)
    provider_created_at = models.DateTimeField(null=True, blank=True)
    observed_at = models.DateTimeField(default=timezone.now)
    competing_effect_ids = models.JSONField(default=list)
    candidate_overflow = models.BooleanField(default=False)
    state = models.CharField(max_length=20, choices=State.choices, default=State.WAITING_RECEIPT)
    reason = models.CharField(max_length=64, blank=True, default="")
    matched_effect = models.ForeignKey("management.IgRevisionDeliveryEffect", null=True, blank=True, on_delete=models.SET_NULL, related_name="deferred_echoes", db_constraint=False)
    manager_message = models.ForeignKey("management.InstagramBotMessage", null=True, blank=True, on_delete=models.SET_NULL, related_name="deferred_echoes", db_constraint=False)
    permission_transition = models.ForeignKey("management.IgPermissionTransitionJob", null=True, blank=True, on_delete=models.SET_NULL, related_name="deferred_echoes", db_constraint=False)
    notification = models.ForeignKey("management.IgBotNotification", null=True, blank=True, on_delete=models.SET_NULL, related_name="deferred_echoes", db_constraint=False)
    resolved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    _IMMUTABLE_FIELDS = (
        "client_id", "settings_id_snapshot", "provider_namespace", "recipient_igsid",
        "provider_message_id", "payload", "event_digest", "provider_created_at",
        "observed_at", "competing_effect_ids", "candidate_overflow",
    )
    objects = models.Manager.from_queryset(_IgDeferredEchoQuerySet)()

    class Meta:
        constraints = [models.UniqueConstraint(fields=["provider_namespace", "provider_message_id"], name="ig_echo_namespace_mid")]
        indexes = [
            models.Index(fields=["client", "provider_namespace", "state"], name="ig_echo_client_scope_state"),
            models.Index(fields=["state", "observed_at", "id"], name="ig_echo_state_observed"),
        ]

    def save(self, *args, **kwargs):
        import hashlib
        import json

        if not isinstance(self.payload, dict) or not isinstance(self.competing_effect_ids, list) or len(self.competing_effect_ids) > 32:
            raise ValueError("deferred echo payload is invalid")
        if self.state == self.State.MANAGER_APPLIED and (
            not self.manager_message_id
            or (self.payload.get("historical") is not True and not self.permission_transition_id)
        ):
            raise ValueError("deferred echo manager proof is missing")
        material = {
            "provider_namespace": self.provider_namespace,
            "recipient_igsid": self.recipient_igsid,
            "provider_message_id": self.provider_message_id,
            "payload": self.payload,
            "provider_created_at": self.provider_created_at.isoformat() if self.provider_created_at else "",
        }
        encoded = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        if len(encoded) > 32 * 1024 or hashlib.sha256(encoded).hexdigest() != self.event_digest:
            raise ValueError("deferred echo digest is invalid")
        if self.pk:
            previous = type(self).objects.filter(pk=self.pk).values(*self._IMMUTABLE_FIELDS, "state").first()
            if previous:
                if any(getattr(self, field) != previous[field] for field in self._IMMUTABLE_FIELDS):
                    raise ValueError("deferred echo identity is immutable")
                allowed = {
                    self.State.WAITING_RECEIPT: {self.State.WAITING_RECEIPT, self.State.AMBIGUOUS, self.State.OWN, self.State.MANAGER_PENDING},
                    self.State.AMBIGUOUS: {self.State.AMBIGUOUS, self.State.OWN, self.State.MANAGER_PENDING},
                    self.State.MANAGER_PENDING: {self.State.MANAGER_PENDING, self.State.MANAGER_APPLIED},
                    self.State.OWN: {self.State.OWN},
                    self.State.MANAGER_APPLIED: {self.State.MANAGER_APPLIED},
                }
                if self.state not in allowed.get(previous["state"], set()):
                    raise ValueError("deferred echo transition is invalid")
        return super().save(*args, **kwargs)


class _IgSourceActionReceiptQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValueError("source action receipt is immutable")


class IgSourceActionReceipt(models.Model):
    """One immutable, global outcome for an original customer source action."""

    client = models.ForeignKey("management.IgClient", on_delete=models.CASCADE, related_name="source_action_receipts", db_constraint=False)
    source_message = models.ForeignKey("management.InstagramBotMessage", on_delete=models.CASCADE, related_name="source_action_receipts", db_constraint=False)
    kind = models.CharField(max_length=32)
    source_digest = models.CharField(max_length=64)
    payload_digest = models.CharField(max_length=64)
    outcome = models.JSONField(default=dict)
    outcome_digest = models.CharField(max_length=64)
    first_revision_id = models.PositiveBigIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    objects = models.Manager.from_queryset(_IgSourceActionReceiptQuerySet)()

    class Meta:
        constraints = [models.UniqueConstraint(fields=["source_message", "kind"], name="ig_source_action_kind")]

    def save(self, *args, **kwargs):
        import hashlib
        import json

        if self.kind != "postback":
            raise ValueError("source action kind is unsupported")
        encoded = json.dumps(self.outcome, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        if len(encoded) > 64 * 1024 or hashlib.sha256(encoded).hexdigest() != self.outcome_digest:
            raise ValueError("source action outcome digest is invalid")
        if self.pk:
            fields = ("client_id", "source_message_id", "kind", "source_digest", "payload_digest", "outcome", "outcome_digest", "first_revision_id")
            previous = type(self).objects.filter(pk=self.pk).values(*fields).first()
            if previous and any(getattr(self, field) != previous[field] for field in fields):
                raise ValueError("source action receipt is immutable")
        return super().save(*args, **kwargs)


class IgCustomerTurnRevision(models.Model):
    """One client-head revision; its bundle payload becomes immutable at seal."""

    class State(models.TextChoices):
        COLLECTING = "collecting", _("Збирається")
        PREPARING = "preparing", _("Готується")
        SEALED = "sealed", _("Зафіксовано")
        CLAIMED = "claimed", _("Захоплено")
        PROCESSED = "processed", _("Опрацьовано")
        SUPERSEDED = "superseded", _("Витіснено")
        OVERFLOW = "overflow", _("Перевищено межі")

    class Origin(models.TextChoices):
        INBOUND = "inbound", _("Вхідний хід")
        AUTO_REFRESH = "auto_refresh", _("Автоматичне оновлення")
        MANUAL_RESUME = "manual_resume", _("Ручне повернення боту")
        OUTAGE_RECOVERY = "outage_recovery", _("Відновлення після збою")

    class SuccessorReason(models.TextChoices):
        NONE = "", _("Не наступник")
        PUBLICATION_CHANGED = "publication_changed", _("Публікація змінилася")
        PUBLIC_POLICY_INPUTS_STALE = (
            "public_policy_inputs_stale",
            _("Публічні правила змінилися"),
        )
        FACT_BINDING_STALE = "fact_binding_stale", _("Факти змінилися")
        MANUAL_RESUME = "manual_resume", _("Авторизоване ручне повернення")
        OUTAGE_RECOVERY = "outage_recovery", _("Відновлення відповіді після збою")

    client = models.ForeignKey(
        "management.IgClient",
        on_delete=models.CASCADE,
        related_name="turn_revisions",
        db_constraint=False,
    )
    turn = models.ForeignKey(
        "management.IgCustomerTurn",
        on_delete=models.CASCADE,
        related_name="revisions",
        db_constraint=False,
    )
    parent = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="successors",
        db_constraint=False,
    )
    revision = models.PositiveBigIntegerField()
    origin = models.CharField(
        max_length=16,
        choices=Origin.choices,
        default=Origin.INBOUND,
        db_default=Origin.INBOUND,
        db_index=True,
    )
    successor_reason = models.CharField(
        max_length=32,
        choices=SuccessorReason.choices,
        blank=True,
        default=SuccessorReason.NONE,
        db_default=SuccessorReason.NONE,
    )
    recovery_state = models.CharField(max_length=16, blank=True, default="", db_default="")
    recovery_due_at = models.DateTimeField(null=True, blank=True, db_default=None, db_index=True)
    recovery_code = models.CharField(max_length=64, blank=True, default="", db_default="")
    # MariaDB permits many NULL values and one active value=1 per client.
    active_slot = models.PositiveSmallIntegerField(null=True, blank=True, default=1)
    state = models.CharField(
        max_length=16,
        choices=State.choices,
        default=State.COLLECTING,
        db_index=True,
    )
    quiet_started_at = models.DateTimeField()
    quiet_deadline = models.DateTimeField(db_index=True)
    quiet_cap_at = models.DateTimeField()
    overall_deadline = models.DateTimeField(db_index=True)
    media_prepare_deadline = models.DateTimeField(null=True, blank=True)
    source_count = models.PositiveSmallIntegerField(default=0)
    text_chars = models.PositiveIntegerField(default=0)
    media_part_count = models.PositiveSmallIntegerField(default=0)
    permission_epoch = models.PositiveBigIntegerField(default=0)
    erasure_started_at_snapshot = models.DateTimeField(null=True, blank=True)
    bundle_snapshot = models.JSONField(default=dict, blank=True)
    snapshot_digest = models.CharField(max_length=64, blank=True, default="")
    generation_proposal = models.JSONField(default=dict, db_default={}, blank=True)
    generation_proposal_digest = models.CharField(
        max_length=64, blank=True, default="", db_default=""
    )
    generation_proposed_at = models.DateTimeField(null=True, blank=True)
    action_receipts = models.JSONField(default=dict, db_default={}, blank=True)
    overflow = models.JSONField(default=dict, blank=True)
    sealed_at = models.DateTimeField(null=True, blank=True)
    claim_token = models.CharField(max_length=64, blank=True, default="")
    claimed_at = models.DateTimeField(null=True, blank=True)
    lease_until = models.DateTimeField(null=True, blank=True, db_index=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = models.Manager.from_queryset(_IgCustomerTurnRevisionQuerySet)()

    class Meta:
        ordering = ["client_id", "revision"]
        constraints = [
            models.UniqueConstraint(
                fields=["client", "revision"], name="ig_turn_rev_client_num"
            ),
            models.UniqueConstraint(
                fields=["client", "active_slot"], name="ig_turn_rev_active_head"
            ),
            models.CheckConstraint(
                condition=models.Q(active_slot__isnull=True) | models.Q(active_slot=1),
                name="ig_turn_rev_active_slot",
            ),
        ]
        indexes = [
            models.Index(
                fields=["state", "quiet_deadline", "id"],
                name="ig_turn_rev_due",
            ),
            models.Index(
                fields=["client", "active_slot", "revision"],
                name="ig_turn_rev_client_head",
            ),
        ]

    def save(self, *args, **kwargs):
        if self.pk:
            previous = type(self).objects.filter(pk=self.pk).values(
                "client_id", "turn_id", "parent_id", "revision",
                "origin", "successor_reason",
                "quiet_started_at", "quiet_cap_at", "overall_deadline",
                "source_count", "text_chars", "media_part_count",
                "permission_epoch", "erasure_started_at_snapshot",
                "bundle_snapshot", "snapshot_digest", "overflow",
                "generation_proposal", "generation_proposal_digest",
                "generation_proposed_at", "action_receipts",
            ).first()
            if previous:
                immutable = (
                    "client_id", "turn_id", "parent_id", "revision",
                    "origin", "successor_reason",
                    "quiet_started_at", "quiet_cap_at", "overall_deadline",
                    "source_count", "text_chars", "media_part_count",
                    "permission_epoch", "erasure_started_at_snapshot", "overflow",
                )
                if any(getattr(self, field) != previous[field] for field in immutable):
                    raise ValueError("IgCustomerTurnRevision identity is immutable")
                if previous["snapshot_digest"] and (
                    self.snapshot_digest != previous["snapshot_digest"]
                    or self.bundle_snapshot != previous["bundle_snapshot"]
                ):
                    raise ValueError("sealed turn revision snapshot is immutable")
                if previous["generation_proposal_digest"] and (
                    self.generation_proposal_digest
                    != previous["generation_proposal_digest"]
                    or self.generation_proposal
                    != previous["generation_proposal"]
                    or self.generation_proposed_at
                    != previous["generation_proposed_at"]
                ):
                    raise ValueError("revision generation proposal is immutable")
                prior_receipts = previous["action_receipts"] or {}
                allowed_receipts = {"input_decision", "client_configuration_update", "manager_handoff", "rate_alert", "postback_decision", "normal_followups", "commerce_reduction", "manual_resume_authorization", "generation_admission", "recovery_lineage", "media_unavailable_reply", "provider_execution_manifest", "provider_execution_reference", "proposal_execution_resume"}
                if (
                    not isinstance(self.action_receipts, dict)
                    or not set(self.action_receipts).issubset(allowed_receipts)
                    or any(self.action_receipts.get(key) != value for key, value in prior_receipts.items())
                ):
                    raise ValueError("revision action receipt entries are immutable")
        return super().save(*args, **kwargs)


class IgTurnRevisionSource(models.Model):
    """Immutable source envelope reused by later execution/manual revisions."""

    revision = models.ForeignKey(
        "management.IgCustomerTurnRevision",
        on_delete=models.CASCADE,
        related_name="sources",
        db_constraint=False,
    )
    message = models.ForeignKey(
        "management.InstagramBotMessage",
        on_delete=models.CASCADE,
        related_name="revision_sources",
        db_constraint=False,
    )
    ordinal = models.PositiveSmallIntegerField()
    role = models.CharField(max_length=8)
    source_namespace = models.CharField(max_length=128, blank=True, default="")
    provider_message_id = models.CharField(max_length=255, blank=True, default="")
    synthetic_event_key = models.CharField(max_length=64, blank=True, default="")
    text = models.TextField(blank=True, default="")
    provider_created_at = models.DateTimeField(null=True, blank=True)
    reply_to_provider_message_id = models.CharField(
        max_length=255, blank=True, default=""
    )
    quick_reply_payload = models.CharField(max_length=1000, blank=True, default="")
    referral = models.JSONField(default=dict, blank=True)
    discovered_media = models.JSONField(default=list, blank=True)
    text_chars = models.PositiveIntegerField(default=0)
    media_part_count = models.PositiveSmallIntegerField(default=0)
    source_digest = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    objects = models.Manager.from_queryset(_ImmutableTurnRevisionSourceQuerySet)()

    class Meta:
        ordering = ["revision_id", "ordinal", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["revision", "ordinal"], name="ig_turn_rev_source_ord"
            ),
            models.UniqueConstraint(
                fields=["revision", "message"], name="ig_turn_rev_source_msg"
            ),
        ]
        indexes = [
            models.Index(
                fields=["message", "revision"], name="ig_turn_rev_source_lookup"
            ),
        ]

    def save(self, *args, **kwargs):
        if self.pk and not kwargs.get("force_insert"):
            raise ValueError("IgTurnRevisionSource is append-only")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("IgTurnRevisionSource is append-only")


class _IgRevisionDeliveryEffectQuerySet(models.QuerySet):
    def update(self, **kwargs):
        if set(kwargs).intersection(IgRevisionDeliveryEffect._IMMUTABLE_FIELDS):
            raise ValueError("revision delivery effect plan is immutable")
        return super().update(**kwargs)


class IgRevisionDeliveryEffect(models.Model):
    """Canonical lifecycle for one physical provider HTTP request."""

    class State(models.TextChoices):
        PLANNED = "planned", _("Заплановано")
        CLAIMED = "claimed", _("Захоплено")
        PROVIDER_STARTED = "provider_started", _("Запит почато")
        SENT = "sent", _("Надіслано")
        DEFINITE_FAILED = "definite_failed", _("Підтверджена відмова")
        UNKNOWN = "unknown", _("Результат невідомий")
        CANCELLED = "cancelled", _("Скасовано")
        SUPERSEDED = "superseded", _("Витіснено")

    revision = models.ForeignKey(
        "management.IgCustomerTurnRevision",
        on_delete=models.CASCADE,
        related_name="delivery_effects",
        db_constraint=False,
    )
    source_message = models.ForeignKey(
        "management.InstagramBotMessage",
        on_delete=models.CASCADE,
        related_name="revision_delivery_effects",
        db_constraint=False,
    )
    effect_key = models.CharField(max_length=160, unique=True)
    actor = models.CharField(max_length=16, default="bot")
    purpose = models.CharField(max_length=32, default="normal_reply")
    group = models.CharField(max_length=32)
    kind = models.CharField(max_length=32)
    order_index = models.PositiveSmallIntegerField()
    part_index = models.PositiveSmallIntegerField()
    part_count = models.PositiveSmallIntegerField()
    plan_digest = models.CharField(max_length=64)
    payload = models.JSONField(default=dict)
    payload_digest = models.CharField(max_length=64)
    projection_metadata = models.JSONField(default=dict, db_default={}, blank=True)
    projection_digest = models.CharField(max_length=64, default="", db_default="", blank=True)
    activation_group = models.CharField(max_length=32, blank=True, default="")
    activation_part_index = models.PositiveSmallIntegerField(null=True, blank=True)
    activation_failure_code = models.CharField(max_length=64, blank=True, default="")
    recipient_igsid = models.CharField(max_length=64)
    provider_namespace = models.CharField(max_length=128)
    generation_request_id = models.CharField(max_length=40, blank=True, default="")
    generation_model = models.CharField(max_length=80, blank=True, default="")
    settings_id_snapshot = models.PositiveIntegerField()
    settings_permission_epoch = models.PositiveBigIntegerField()
    client_permission_epoch = models.PositiveBigIntegerField()
    revision_snapshot_digest = models.CharField(max_length=64)
    publication_id = models.PositiveBigIntegerField()
    publication_version = models.PositiveBigIntegerField()
    publication_hash = models.CharField(max_length=64)
    authority_context_digest = models.CharField(max_length=64)
    fact_bindings = models.JSONField(default=list, blank=True)
    offer_bindings = models.JSONField(default=list, blank=True)
    state = models.CharField(
        max_length=20,
        choices=State.choices,
        default=State.PLANNED,
        db_index=True,
    )
    claim_token = models.CharField(max_length=64, blank=True, default="")
    lease_until = models.DateTimeField(null=True, blank=True, db_index=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    provider_started_at = models.DateTimeField(null=True, blank=True)
    provider_message_id = models.CharField(max_length=255, blank=True, default="")
    provider_http_status = models.PositiveSmallIntegerField(null=True, blank=True)
    provider_response_digest = models.CharField(max_length=64, blank=True, default="")
    failure_code = models.CharField(max_length=64, blank=True, default="")
    terminal_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["revision_id", "order_index", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["revision", "order_index"], name="ig_rev_effect_order"
            ),
            models.UniqueConstraint(
                fields=["revision", "group", "part_index"],
                name="ig_rev_effect_group_part",
            ),
            models.CheckConstraint(
                condition=models.Q(part_count__gte=1),
                name="ig_rev_effect_part_count",
            ),
            models.CheckConstraint(
                condition=models.Q(part_index__lt=models.F("part_count")),
                name="ig_rev_effect_part_index",
            ),
        ]
        indexes = [
            models.Index(
                fields=["revision", "group", "state", "part_index"],
                name="ig_rev_effect_group_state",
            ),
            models.Index(
                fields=["state", "lease_until", "id"],
                name="ig_rev_effect_due",
            ),
        ]

    _IMMUTABLE_FIELDS = (
        "revision_id", "source_message_id", "effect_key", "actor", "purpose",
        "group", "kind", "order_index", "part_index", "part_count",
        "plan_digest", "payload", "payload_digest", "recipient_igsid",
        "projection_metadata", "projection_digest",
        "activation_group", "activation_part_index", "activation_failure_code",
        "provider_namespace", "generation_request_id", "generation_model",
        "settings_id_snapshot", "settings_permission_epoch",
        "client_permission_epoch", "revision_snapshot_digest", "publication_id",
        "publication_version", "publication_hash", "authority_context_digest",
        "fact_bindings", "offer_bindings",
    )

    objects = models.Manager.from_queryset(_IgRevisionDeliveryEffectQuerySet)()

    def save(self, *args, **kwargs):
        if self.pk:
            previous = type(self).objects.filter(pk=self.pk).values(
                *self._IMMUTABLE_FIELDS
            ).first()
            if previous and any(
                getattr(self, field) != previous[field]
                for field in self._IMMUTABLE_FIELDS
            ):
                raise ValueError("revision delivery effect plan is immutable")
        return super().save(*args, **kwargs)


class IgAlertRateBucket(models.Model):
    """ЭА.16 — атомарний ліміт потоку алертів замість read-then-write у кеші.

    **Що саме ламалось.** `ig_alerts.throttle_gate()` робив `cache.get()` і
    `cache.set()` **окремими** операціями. Це не атомарно навіть в одному
    процесі, а production-кеш — `FileBasedCache`, не Redis: у ньому немає ні
    `incr` під блокуванням, ні транзакції. Тобто при паралельних викликах
    (демон крутить drain кожні 1.5 с, а `notify_manager(deliver_immediately=True)`
    взагалі йшов у Telegram напряму) вікно перевірялось за старим знімком і ліміт
    перевищувався.

    **Друга частина дефекту гірша.** При збої кеша функція повертала
    `True` — fail-open. Тобто найгірший момент (кеш зламався) давав саме
    необмежений потік. Розділ 11 джерела прямо вимагає bounded safe mode, а не
    fail-open.

    **Чому БД, а не Redis.** Проект живе на shared-хостингу, де Redis не
    гарантований, а MariaDB — так. Один рядок під `select_for_update()` дає
    справжню атомарність там, де вона потрібна, і не вводить нову залежність
    інфраструктури. Ціна — один короткий row-lock на алерт, що на порядки
    дешевше, ніж Telegram-запит, який він охороняє.
    """

    # Ключ бакета: `flow` для глобального потоку, окремі ключі — для лейнів,
    # якщо колись знадобиться розділити бюджет.
    bucket_key = models.CharField(max_length=64, unique=True)
    window_started_at = models.DateTimeField(default=timezone.now)
    used = models.PositiveIntegerField(default=0)
    # Скільки разів ліміт відмовив — щоб «тихо задушений потік» не став ще однією
    # невидимою поломкою (guardrail пункта).
    denied = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Ліміт потоку алертів IG")
        verbose_name_plural = _("Ліміти потоку алертів IG")
        ordering = ["bucket_key"]

    def __str__(self) -> str:  # pragma: no cover - trivial representation
        return f"{self.bucket_key}: {self.used} @ {self.window_started_at:%H:%M:%S}"


class IgFunnelNodeState(models.Model):
    """Стан одного вузла воронки — закриття доводиться даними, а не стадією (Э4.1).

    **Чому окрема таблиця, а не ще одне поле в `IgClient`.** `stage` відповідає на
    питання «де ми в лінійній воронці», і план прямо забороняє її розширювати:
    закриття вузла і стадія — різні поняття. `product_matched` не означає, що
    обраний колір; `checkout` не означає, що є відділення НП. Поки єдиним
    джерелом був `stage`, «чи закритий вузол» доводилось **виводити** зі стадії,
    і це виведення було неправдою в обидві сторони.

    **Чому це не дублює `IgFunnelStepEvent`.** Той журнал — незмінні **віхи
    епізоду** (`paylink_issued`, `payment_confirmed`): відповідає на «що вже
    сталося». Тут — **поточний стан конкретного вузла** з причиною і evidence:
    відповідає на «чого бракує і чому». Одне не виводиться з іншого: віха
    `product_pinned` не каже, чи товар досі опублікований, а вузол `product` у
    статусі `invalidated` — каже.

    **Чому `node_key` unique, а не UniqueConstraint по кортежу.** Адреса вузла
    містить nullable `commercial_episode` і порожні `line_id`/`recipient_id`. У
    MariaDB два NULL в unique-індексі вважаються різними, тому кортежний
    constraint пропустив би дублікати саме для клієнтських (позаепізодних)
    вузлів. Той самий вибір уже зроблено в `IgCommercialEpisode.materialization_key`
    і `IgMemoryFact.record_key`.

    **Чому статусів сім, а не два.** `selected`/`missing` роблять три різні
    причини «роззакриття» однаковими (Э4.2). `not_applicable` — це політика
    («один крій — питання не існує»), а не незнання, і жовтий статус в UI мусить
    означати саме її. `skipped` існує тільки для класу QUALITY і тільки з
    причиною.
    """

    class Status(models.TextChoices):
        OPEN = "open", _("Відкритий")
        COMPLETE = "complete", _("Закритий")
        PARTIAL = "partial", _("Частково закритий")
        SKIPPED = "skipped", _("Пропущений із причиною")
        NOT_APPLICABLE = "not_applicable", _("Не застосовується")
        INVALIDATED = "invalidated", _("Знецінений зміною іншого вузла")
        SUPERSEDED = "superseded", _("Витіснений новим значенням")

    class BranchType(models.TextChoices):
        """Гілка стану для сумісності з Gemini V2 (`episode + branch + line`)."""

        MAIN = "main", _("Основна гілка")
        REPEAT = "repeat", _("Повторна покупка")
        GIFT = "gift", _("Подарунок отримувачу")
        EXCHANGE = "exchange", _("Обмін або повернення")

    # Стани, після яких вузол уже не «чекає» на клієнта: далі або нічого не
    # треба, або треба переспитати з причиною (Э4.2), і це вже інша дія.
    _TERMINAL_STATES = frozenset({
        Status.COMPLETE, Status.SKIPPED, Status.NOT_APPLICABLE, Status.SUPERSEDED,
    })
    # Стани, які закривають вузол для розрахунку блокувань. `skipped` тут немає
    # свідомо: пропуск дозволений лише класу QUALITY, а QUALITY нічого не
    # блокує, тож пропуск ніколи не має відкривати необоротну дію.
    _CLOSED_STATES = frozenset({Status.COMPLETE, Status.NOT_APPLICABLE})

    # Адреса вузла. Детермінований ключ, який рахує `ig_funnel_nodes`.
    node_key = models.CharField(max_length=190, unique=True)
    client = models.ForeignKey(
        "management.IgClient",
        on_delete=models.CASCADE,
        related_name="funnel_node_states",
        db_constraint=False,
    )
    commercial_episode = models.ForeignKey(
        "management.IgCommercialEpisode",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="funnel_node_states",
        db_constraint=False,
    )
    branch_type = models.CharField(
        max_length=16,
        choices=BranchType.choices,
        default=BranchType.MAIN,
        db_index=True,
    )
    # Непрозорі ідентифікатори лінії та отримувача: у них не можна кладти
    # ні телефон, ні ім'я — вузол з даними отримувача живе окремо від покупця.
    line_id = models.CharField(max_length=96, blank=True, default="")
    recipient_id = models.CharField(max_length=96, blank=True, default="")
    # Ключ і версія визначення. Це рядки, а не FK: analysis посилається лише на
    # ключі і не має знати фізичну таблицю вузлів (контракт Gemini V2).
    definition_key = models.CharField(max_length=96, db_index=True)
    definition_version = models.CharField(max_length=32, default="")
    status = models.CharField(
        max_length=24,
        choices=Status.choices,
        default=Status.OPEN,
        db_index=True,
    )
    # Чому вузол саме в цьому статусі. Для `skipped` і `not_applicable`
    # обов'язково, інакше жовтий статус знову зіллється з незнанням.
    reason_code = models.CharField(max_length=48, blank=True, default="")
    reason_detail = models.CharField(max_length=200, blank=True, default="")
    # Який саме вузол знецінив цей. Э4.2 читає це поле, щоб сказати клієнту
    # «поміняли колір, і в ньому немає L», а не спитати розмір заново без слів.
    invalidated_by_definition_key = models.CharField(
        max_length=96, blank=True, default=""
    )
    typed_value = models.JSONField(default=dict, blank=True)
    # Попереднє значення зберігається тут, а не в окремій таблиці історії:
    # Э4.2 вимагає «клієнт передумав» не переспитувати, для цього достатньо
    # бачити, що значення було і яким саме.
    previous_typed_value = models.JSONField(default=dict, blank=True)
    evidence_message_ids = models.JSONField(default=list, blank=True)
    # Чим саме доведено закриття: `checkout_readiness`, довідник НП, оплата.
    # Порожньо тільки для `open`: закриття без доказу — це і є виведення зі стадії.
    closure_method = models.CharField(max_length=32, blank=True, default="")
    projector_version = models.CharField(max_length=32, blank=True, default="")
    observed_at = models.DateTimeField(null=True, blank=True)
    invalidated_at = models.DateTimeField(null=True, blank=True)
    superseded_at = models.DateTimeField(null=True, blank=True)
    # Коли значення варто підтвердити (адреса могла змінитись). Заповнює Э4.2;
    # колонка є вже зараз, щоб `stale_confirm` не вимагав другої міграції схеми.
    confirm_after = models.DateTimeField(null=True, blank=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    superseded_by = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="supersedes",
        db_constraint=False,
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Стан вузла воронки IG")
        verbose_name_plural = _("Стани вузлів воронки IG")
        ordering = ["definition_key", "id"]
        constraints = [
            # Пропуск і «не застосовується» без причини — це рівно та втрата
            # інформації, через яку жовтий статус зливався з незнанням.
            models.CheckConstraint(
                condition=(
                    ~models.Q(status__in=("skipped", "not_applicable"))
                    | ~models.Q(reason_code="")
                ),
                name="ig_fnode_reason_required",
            ),
        ]
        indexes = [
            models.Index(
                fields=["client", "commercial_episode", "definition_key"],
                name="ig_fnode_client_episode",
            ),
            models.Index(fields=["status", "-updated_at"], name="ig_fnode_status_dt"),
            models.Index(
                fields=["commercial_episode", "branch_type", "line_id"],
                name="ig_fnode_branch",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial representation
        return f"funnel_node:{self.definition_key}:{self.status}"

    @property
    def is_terminal(self) -> bool:
        return self.status in self._TERMINAL_STATES

    @property
    def is_closed(self) -> bool:
        """Чи знятий вузол з дороги до необоротної дії."""
        return self.status in self._CLOSED_STATES

    @classmethod
    def closed_statuses(cls) -> frozenset:
        """Публічний доступ до набору «закритих» статусів.

        Проєктор мусить рахувати блокування тим самим набором, яким модель
        відповідає на `is_closed`: два списки розійшлися б, і UI показував би не
        те, що блокує оплату.
        """
        return cls._CLOSED_STATES
