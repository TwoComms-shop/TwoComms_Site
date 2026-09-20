"""Second-generation finance ledger primitives.

These models deliberately sit beside the original transaction ledger.  They
add explicit economic meaning and reviewable links without rewriting imported
bank history in place.
"""
from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models

from .models_core import Account, Category, Company, Counterparty, CounterpartyCard, Project
from .models_txn import Transaction, ObligationSettlement, RecurrenceRule


class FundingSource(models.Model):
    TYPE_CHOICES = [('grant', 'Грант'), ('targeted', 'Целевое финансирование'), ('other', 'Другое')]

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='funding_sources')
    name = models.CharField(max_length=255)
    source_type = models.CharField(max_length=16, choices=TYPE_CHOICES, default='grant')
    received_amount = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'))
    received_at = models.DateField(blank=True, null=True)
    # Programme budget stays distinct from money actually received in the bank.
    program_total_amount = models.DecimalField(max_digits=18, decimal_places=2,
                                               blank=True, null=True)
    stage_label = models.CharField(max_length=120, blank=True, default='')
    receipt_transaction = models.ForeignKey(
        Transaction, on_delete=models.SET_NULL, blank=True, null=True,
        related_name='funding_source_receipts',
    )
    valid_until = models.DateField(blank=True, null=True)
    allowed_categories = models.ManyToManyField(Category, blank=True, related_name='funding_sources')
    project = models.ForeignKey(Project, on_delete=models.SET_NULL, null=True, blank=True,
                                related_name='funding_sources')
    notes = models.TextField(blank=True, default='')
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-received_at', 'name']

    def __str__(self):
        return self.name


class LedgerClassification(models.Model):
    SCOPE_CHOICES = [
        ('business', 'Бизнес'), ('personal', 'Личное'), ('mixed', 'Смешанное'),
        ('unknown', 'Не определено'),
    ]
    KIND_CHOICES = [
        ('sale', 'Продажа'), ('operating_expense', 'Операционный расход'),
        ('investment', 'Инвестиция'), ('grant_inflow', 'Грант'),
        ('internal_transfer', 'Внутренний перевод'), ('owner_draw', 'Вывод владельцу'),
        ('debt_repayment', 'Погашение долга'), ('expense_refund', 'Возврат расхода'),
        ('pension_income', 'Пенсійна виплата'), ('transfer_fee', 'Комісія за переказ'),
        ('personal_transfer', 'Личный перевод'), ('adjustment', 'Корректировка'),
        ('unknown', 'Не классифицировано'),
    ]

    transaction = models.OneToOneField(Transaction, on_delete=models.CASCADE, related_name='ledger_classification')
    ownership_scope = models.CharField(max_length=16, choices=SCOPE_CHOICES, default='unknown', db_index=True)
    economic_kind = models.CharField(max_length=32, choices=KIND_CHOICES, default='unknown', db_index=True)
    confidence = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('0'))
    source = models.CharField(max_length=16, default='manual')
    note = models.TextField(blank=True, default='')
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)


class LedgerClassificationEvent(models.Model):
    """Append-only audit trail for a classification change."""

    classification = models.ForeignKey(LedgerClassification, on_delete=models.CASCADE,
                                        related_name='events')
    previous = models.JSONField(default=dict, blank=True)
    current = models.JSONField(default=dict, blank=True)
    reason = models.TextField(blank=True, default='')
    changed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                   null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class FundingAllocation(models.Model):
    funding_source = models.ForeignKey(FundingSource, on_delete=models.CASCADE, related_name='allocations')
    transaction = models.ForeignKey(Transaction, on_delete=models.CASCADE, related_name='funding_allocations')
    amount = models.DecimalField(max_digits=18, decimal_places=2, validators=[MinValueValidator(Decimal('0.01'))])
    allocation_type = models.CharField(max_length=16, choices=[('received', 'Поступление'), ('spent', 'Расход'), ('reserved', 'Резерв')], default='spent')
    note = models.TextField(blank=True, default='')
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['funding_source', 'transaction', 'allocation_type'], name='finance_funding_alloc_unique')]


class RefundLink(models.Model):
    """A refund/repayment linked to its original expense or debt."""

    REFUND_KIND_CHOICES = [
        ('expense_refund', 'Возврат расхода'),
        ('debt_repayment', 'Погашение долга'),
        ('rent_refund', 'Возврат аренды'),
        ('supplier_refund', 'Возврат поставщика'),
    ]
    refund_transaction = models.OneToOneField(Transaction, on_delete=models.CASCADE,
                                               related_name='refund_link')
    original_transaction = models.ForeignKey(Transaction, on_delete=models.PROTECT,
                                             related_name='refunds_received')
    amount = models.DecimalField(max_digits=18, decimal_places=2,
                                 validators=[MinValueValidator(Decimal('0.01'))])
    kind = models.CharField(max_length=24, choices=REFUND_KIND_CHOICES, default='expense_refund')
    note = models.TextField(blank=True, default='')
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                   null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class InternalTransferMatch(models.Model):
    STATUS_CHOICES = [('suggested', 'Предложено'), ('confirmed', 'Подтверждено'), ('rejected', 'Отклонено')]

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='transfer_matches')
    source_transaction = models.ForeignKey(Transaction, on_delete=models.CASCADE, related_name='outgoing_transfer_matches')
    destination_transaction = models.ForeignKey(Transaction, on_delete=models.CASCADE, related_name='incoming_transfer_matches')
    source_account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name='+')
    destination_account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name='+')
    amount = models.DecimalField(max_digits=18, decimal_places=2)
    # Difference between the principal received and the amount debited by the
    # bank. It is recorded separately so a transfer never becomes income or
    # expense while the bank commission remains visible in reports.
    fee_amount = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'))
    fee_transaction = models.ForeignKey(
        Transaction, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='transfer_fee_matches',
    )
    confidence = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('0'))
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default='suggested')
    confirmed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['source_transaction', 'destination_transaction'], name='finance_transfer_match_unique')]


class BalanceReconciliation(models.Model):
    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name='reconciliations')
    as_of = models.DateTimeField()
    observed_balance = models.DecimalField(max_digits=18, decimal_places=2)
    calculated_balance = models.DecimalField(max_digits=18, decimal_places=2)
    delta = models.DecimalField(max_digits=18, decimal_places=2)
    note = models.TextField(blank=True, default='')
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class ClassificationReview(models.Model):
    STATUS_CHOICES = [('pending', 'Ожидает'), ('accepted', 'Принято'), ('rejected', 'Отклонено'), ('skipped', 'Пропущено')]

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='classification_reviews')
    transaction = models.ForeignKey(Transaction, on_delete=models.CASCADE, related_name='classification_reviews')
    proposal = models.JSONField(default=dict)
    reason = models.TextField(blank=True, default='')
    impact = models.JSONField(default=dict, blank=True)
    confidence = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('0'))
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default='pending', db_index=True)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class CounterpartyAlias(models.Model):
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='counterparty_aliases')
    counterparty = models.ForeignKey(Counterparty, on_delete=models.CASCADE, related_name='aliases')
    value = models.CharField(max_length=255)
    normalized = models.CharField(max_length=255, db_index=True)
    source = models.CharField(max_length=16, default='manual')
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['company', 'normalized'], name='finance_counterparty_alias_unique')]


class ObligationGroup(models.Model):
    STATUS_CHOICES = [('planned', 'Планируется'), ('partial', 'Частично'), ('paid', 'Оплачено'), ('overdue', 'Просрочено')]
    ROLE_CHOICES = [('charge', 'Начисление'), ('prepayment', 'Аванс'), ('refund', 'Возврат')]

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='obligation_groups')
    title = models.CharField(max_length=255)
    type = models.CharField(max_length=12, choices=[('income', 'Доход'), ('expense', 'Расход')], default='expense')
    counterparty = models.ForeignKey(Counterparty, on_delete=models.SET_NULL, null=True, blank=True, related_name='obligation_groups')
    recurrence_rule = models.ForeignKey(RecurrenceRule, on_delete=models.SET_NULL, null=True, blank=True, related_name='obligation_groups')
    due_day = models.PositiveSmallIntegerField(null=True, blank=True)
    role = models.CharField(max_length=16, choices=ROLE_CHOICES, default='charge')
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default='planned')
    forecast_policy = models.JSONField(default=dict, blank=True)
    notes = models.TextField(blank=True, default='')
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)


class ObligationComponent(models.Model):
    group = models.ForeignKey(ObligationGroup, on_delete=models.CASCADE, related_name='components')
    name = models.CharField(max_length=255)
    category = models.ForeignKey(Category, on_delete=models.SET_NULL, null=True, blank=True, related_name='obligation_components')
    recipient_card = models.ForeignKey(CounterpartyCard, on_delete=models.SET_NULL, null=True, blank=True, related_name='obligation_components')
    fixed_amount = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    forecast_amount = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    forecast_policy = models.JSONField(default=dict, blank=True)
    payment_purpose_template = models.TextField(blank=True, default='')
    sort_order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['sort_order', 'id']


class ObligationComponentSettlement(models.Model):
    settlement = models.ForeignKey(ObligationSettlement, on_delete=models.CASCADE, related_name='component_allocations')
    component = models.ForeignKey(ObligationComponent, on_delete=models.CASCADE, related_name='settlements')
    amount = models.DecimalField(max_digits=18, decimal_places=2, validators=[MinValueValidator(Decimal('0.01'))])

    class Meta:
        constraints = [models.UniqueConstraint(fields=['settlement', 'component'], name='finance_component_settlement_unique')]


class PaymentIntent(models.Model):
    STATUS_CHOICES = [('draft', 'Черновик'), ('awaiting_confirmation', 'Ожидает подтверждения'), ('submitted', 'Отправлено'), ('detected', 'Найдено в выписке'), ('confirmed', 'Подтверждено'), ('rejected', 'Отклонено'), ('expired', 'Истекло')]

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='payment_intents')
    component = models.ForeignKey(ObligationComponent, on_delete=models.SET_NULL, null=True, blank=True, related_name='payment_intents')
    account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name='payment_intents')
    recipient_card = models.ForeignKey(CounterpartyCard, on_delete=models.SET_NULL, null=True, blank=True, related_name='payment_intents')
    amount = models.DecimalField(max_digits=18, decimal_places=2, validators=[MinValueValidator(Decimal('0.01'))])
    currency = models.CharField(max_length=3, default='UAH')
    purpose = models.TextField(blank=True, default='')
    comment = models.TextField(blank=True, default='')
    provider = models.CharField(max_length=32, default='monobank_personal')
    status = models.CharField(max_length=24, choices=STATUS_CHOICES, default='draft', db_index=True)
    idempotency_key = models.CharField(max_length=96, unique=True)
    provider_reference = models.CharField(max_length=128, blank=True, default='')
    matched_transaction = models.ForeignKey(Transaction, on_delete=models.SET_NULL, null=True, blank=True, related_name='payment_intents')
    expires_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class PaymentIntentEvent(models.Model):
    intent = models.ForeignKey(PaymentIntent, on_delete=models.CASCADE, related_name='events')
    from_status = models.CharField(max_length=24, blank=True, default='')
    to_status = models.CharField(max_length=24)
    payload = models.JSONField(default=dict, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
