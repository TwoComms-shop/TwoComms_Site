"""Транзакції, повторення та вкладення — ядро фінансової логіки."""
from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.db import models

from .models_core import Account, Category, Counterparty, Company, Project, Tag


class RecurrenceRule(models.Model):
    """Правило повторення планового платежу."""

    FREQ_CHOICES = [
        ('daily', 'Щодня'),
        ('weekly', 'Щотижня'),
        ('monthly', 'Щомісяця'),
        ('yearly', 'Щороку'),
        ('custom', 'Інше'),
    ]

    # Режим завершення повторення (керує розрахунком «скільки залишилось»):
    #   never — безстроково; until — до дати end_date; count — рівно N разів.
    END_NEVER = 'never'
    END_UNTIL = 'until'
    END_COUNT = 'count'
    END_CHOICES = [
        (END_NEVER, 'Безстроково'),
        (END_UNTIL, 'До дати'),
        (END_COUNT, 'Певну кількість разів'),
    ]

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='recurrence_rules')
    # Людська назва зобов'язання (напр. «Комуналка», «Оренда офісу») — показується
    # одним рядком у списку планових замість дублів-копій за кожен період.
    title = models.CharField(max_length=255, blank=True, default='')
    frequency = models.CharField(max_length=12, choices=FREQ_CHOICES, default='monthly')
    interval = models.PositiveIntegerField(default=1)
    by_day = models.CharField(max_length=32, blank=True, default='')  # напр. 'MO,WE'
    by_month_day = models.PositiveIntegerField(blank=True, null=True)
    start_date = models.DateField()
    end_mode = models.CharField(max_length=8, choices=END_CHOICES, default=END_NEVER)
    end_date = models.DateField(blank=True, null=True)
    count = models.PositiveIntegerField(blank=True, null=True)
    next_occurrence = models.DateField(
        blank=True,
        null=True,
        help_text='Дата наступного автоматичного створення планової транзакції',
    )
    auto_create = models.BooleanField(
        default=True,
        help_text='Автоматично створювати планові транзакції',
    )
    notification_days_before = models.PositiveIntegerField(
        default=3,
        help_text='За скільки днів надсилати нагадування',
    )
    last_generated_at = models.DateTimeField(
        blank=True,
        null=True,
        help_text='Коли останній раз генерували транзакцію',
    )
    template_amount = models.DecimalField(
        max_digits=18,
        decimal_places=2,
        blank=True,
        null=True,
        help_text='Сума шаблону для створення транзакцій',
    )
    # Чи сума орієнтовна (предполагаемая): для комуналки/змінних рахунків точну
    # суму знаємо лише при оплаті. Орієнтовні платежі показуються з «≈», а при
    # погашенні просять увести фактичну суму.
    amount_is_estimated = models.BooleanField(
        default=False,
        help_text='Сума орієнтовна — уточнюється при оплаті',
    )
    template_account = models.ForeignKey(
        Account,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name='recurrence_rules_as_template',
        help_text='Рахунок шаблону',
    )
    template_category = models.ForeignKey(
        Category,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name='recurrence_rules_as_template',
        help_text='Категорія шаблону',
    )
    template_type = models.CharField(
        max_length=12,
        choices=[('income', 'Дохід'), ('expense', 'Витрата')],
        default='expense',
        help_text='Тип транзакції шаблону',
    )
    template_comment = models.TextField(
        blank=True,
        default='',
        help_text='Коментар шаблону',
    )
    template_counterparty = models.ForeignKey(
        Counterparty,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name='recurrence_rules_as_template',
        help_text='Контрагент шаблону',
    )
    template_project = models.ForeignKey(
        Project,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name='recurrence_rules_as_template',
        help_text='Проєкт шаблону',
    )
    template_currency = models.CharField(max_length=3, blank=True, default='UAH')
    template_is_business = models.BooleanField(default=True)
    is_active = models.BooleanField(
        default=True,
        help_text='Чи активне правило',
    )

    class Meta:
        verbose_name = 'Правило повторення'
        verbose_name_plural = 'Правила повторення'

    def __str__(self):
        return self.title or f'{self.get_frequency_display()} x{self.interval}'

    # ------------------------------------------------------------------
    # Допоміжні властивості для відображення «повторюваний / скільки лишилось».
    # ------------------------------------------------------------------
    @property
    def frequency_label(self) -> str:
        """Короткий людський опис періодичності: «щомісяця», «кожні 2 тижні»."""
        every = self.interval or 1
        base = {
            'daily': ('щодня', 'дні'),
            'weekly': ('щотижня', 'тижні'),
            'monthly': ('щомісяця', 'місяці'),
            'yearly': ('щороку', 'роки'),
            'custom': ('за графіком', 'періоди'),
        }.get(self.frequency, ('за графіком', 'періоди'))
        if every == 1:
            return base[0]
        return f'кожні {every} {base[1]}'

    @property
    def end_label(self) -> str:
        """Людський опис межі повторення для UI."""
        if self.end_mode == self.END_UNTIL and self.end_date:
            return f'до {self.end_date.strftime("%d.%m.%Y")}'
        if self.end_mode == self.END_COUNT and self.count:
            return f'{self.count} разів'
        return 'безстроково'

    @property
    def repeat_badge(self) -> str:
        """Компактний індикатор «скільки разів» для списку (як ×6 у магазинах).

        ``×N`` для обмеженої кількості повторень, ``∞`` для безстрокових,
        ``до ДД.ММ`` для обмежених датою.
        """
        if self.end_mode == self.END_COUNT and self.count:
            return f'×{self.count}'
        if self.end_mode == self.END_UNTIL and self.end_date:
            return f'до {self.end_date.strftime("%d.%m")}'
        return '∞'



class Attachment(models.Model):
    """Вкладення до операції/інвойсу (макс. 10 МБ перевіряється у формі)."""

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='attachments')
    file = models.FileField(upload_to='finance/attachments/')
    original_name = models.CharField(max_length=255, blank=True, default='')
    content_type = models.CharField(max_length=128, blank=True, default='')
    size = models.PositiveIntegerField(default=0)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                    blank=True, null=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Вкладення'
        verbose_name_plural = 'Вкладення'

    def __str__(self):
        return self.original_name or self.file.name


class Transaction(models.Model):
    """Єдина сутність операції: дохід, витрата, переказ.

    Ключові облікові поля:
    - date_actual    — дата руху грошей (для балансу, Cash Flow, календаря);
    - date_agreement — дата угоди/документа (для P&L, дебіторки/кредиторки).
    Статус ``planned`` не впливає на фактичний баланс, лише на прогноз.
    """

    TYPE_INCOME = 'income'
    TYPE_EXPENSE = 'expense'
    TYPE_TRANSFER = 'transfer'
    TYPE_CHOICES = [
        (TYPE_INCOME, 'Дохід'),
        (TYPE_EXPENSE, 'Витрата'),
        (TYPE_TRANSFER, 'Переказ'),
    ]

    STATUS_ACTUAL = 'actual'
    STATUS_PLANNED = 'planned'
    STATUS_DRAFT = 'draft'
    STATUS_CANCELLED = 'cancelled'
    STATUS_CHOICES = [
        (STATUS_ACTUAL, 'Фактична'),
        (STATUS_PLANNED, 'Планова'),
        (STATUS_DRAFT, 'Чернетка'),
        (STATUS_CANCELLED, 'Скасована'),
    ]

    SOURCE_CHOICES = [
        ('manual', 'Вручну'),
        ('import', 'Імпорт виписки'),
        ('integration', 'Банківська інтеграція'),
        ('invoice', 'Інвойс'),
        ('rule', 'Автоправило'),
        ('recurring', 'Повторення'),
        ('ai', 'AI радник'),
    ]

    OWNERSHIP_SCOPE_CHOICES = [
        ('business', 'Бизнес'), ('personal', 'Личное'), ('mixed', 'Смешанное'),
        ('unknown', 'Не определено'),
    ]
    ECONOMIC_KIND_CHOICES = [
        ('sale', 'Продажа'), ('operating_expense', 'Операционный расход'),
        ('investment', 'Инвестиция'), ('grant_inflow', 'Грант'),
        ('internal_transfer', 'Внутренний перевод'), ('owner_draw', 'Вывод владельцу'),
        ('debt_repayment', 'Погашение долга'), ('expense_refund', 'Возврат расхода'),
        ('pension_income', 'Пенсійна виплата'), ('transfer_fee', 'Комісія за переказ'),
        ('personal_transfer', 'Личный перевод'), ('adjustment', 'Корректировка'),
        ('unknown', 'Не классифицировано'),
    ]

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='transactions')
    type = models.CharField(max_length=12, choices=TYPE_CHOICES)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=STATUS_ACTUAL)

    amount = models.DecimalField(max_digits=18, decimal_places=2)
    currency = models.CharField(max_length=3, default='UAH')
    amount_base = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'))

    # Для переказів: account = джерело, to_account = отримувач.
    account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name='transactions',
                                blank=True, null=True)
    to_account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name='incoming_transfers',
                                   blank=True, null=True)
    exchange_rate = models.DecimalField(max_digits=18, decimal_places=6, blank=True, null=True)
    to_amount = models.DecimalField(max_digits=18, decimal_places=2, blank=True, null=True)

    date_actual = models.DateTimeField()
    date_agreement = models.DateTimeField(blank=True, null=True)

    category = models.ForeignKey(Category, on_delete=models.SET_NULL, blank=True, null=True,
                                 related_name='transactions')
    counterparty = models.ForeignKey(Counterparty, on_delete=models.SET_NULL, blank=True, null=True,
                                     related_name='transactions')
    project = models.ForeignKey(Project, on_delete=models.SET_NULL, blank=True, null=True,
                                related_name='transactions')
    tags = models.ManyToManyField(Tag, blank=True, related_name='transactions')
    comment = models.TextField(blank=True, default='')
    attachments = models.ManyToManyField(Attachment, blank=True, related_name='transactions')

    source = models.CharField(max_length=16, choices=SOURCE_CHOICES, default='manual')
    external_id = models.CharField(max_length=128, blank=True, default='')
    # Сирі дані провайдера для багатшого аналізу: mcc, cashbackAmount,
    # commissionRate, counterIban/counterName, operationAmount/currencyCode тощо.
    # Дозволяє фільтрувати/аналізувати без окремих колонок під кожне поле.
    external_data = models.JSONField(default=dict, blank=True)
    # Управленческий смысл хранится отдельно от банковского типа движения.
    # Значения nullable для безопасного rollout на старую историю.
    ownership_scope = models.CharField(max_length=16, choices=OWNERSHIP_SCOPE_CHOICES,
                                       default='unknown', db_index=True)
    economic_kind = models.CharField(max_length=32, choices=ECONOMIC_KIND_CHOICES,
                                     default='unknown', db_index=True)
    funding_source = models.ForeignKey(
        'finance.FundingSource', on_delete=models.SET_NULL, blank=True, null=True,
        related_name='classified_transactions',
    )
    # MCC (Merchant Category Code) винесено окремою колонкою для індексованого
    # групування витрат за категоріями мерчанта.
    mcc = models.PositiveIntegerField(blank=True, null=True, db_index=True)

    is_recurring = models.BooleanField(default=False)
    # Орієнтовна сума (предполагаемая): для планових платежів зі змінною сумою.
    # Точну суму вводять при погашенні. Не впливає на облік фактичних.
    amount_is_estimated = models.BooleanField(default=False)
    recurrence_rule = models.ForeignKey(RecurrenceRule, on_delete=models.SET_NULL, blank=True,
                                        null=True, related_name='transactions')
    # Прив'язка до магазину-реалізатора (модуль consignment). Для планових
    # боргів магазину та фактичних виплат — дозволяє reseller.transactions.
    reseller = models.ForeignKey('finance.Reseller', on_delete=models.SET_NULL, blank=True,
                                 null=True, related_name='transactions')
    parent_transaction = models.ForeignKey('self', on_delete=models.CASCADE, blank=True, null=True,
                                           related_name='children')
    is_split = models.BooleanField(default=False)
    split_group = models.CharField(max_length=64, blank=True, default='')

    linked_invoice = models.ForeignKey('finance.Invoice', on_delete=models.SET_NULL, blank=True,
                                       null=True, related_name='payments')

    excluded_from_reports = models.BooleanField(default=False)
    # Бізнес vs особиста транзакція. За замовчуванням успадковує is_business
    # рахунку (ФОП → бізнес; особиста картка → особисте), але кожну операцію
    # можна перекваліфікувати вручну для коректної бізнес-статистики.
    is_business = models.BooleanField(default=False, db_index=True)
    is_demo = models.BooleanField(default=False)

    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, blank=True,
                                   null=True, related_name='+')
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, blank=True,
                                   null=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Операція'
        verbose_name_plural = 'Операції'
        ordering = ['-date_actual', '-id']
        indexes = [
            models.Index(fields=['company', 'type', 'status']),
            models.Index(fields=['date_actual']),
            models.Index(fields=['date_agreement']),
            models.Index(fields=['external_id']),
        ]

    def __str__(self):
        return f'{self.get_type_display()} {self.amount} {self.currency} ({self.date_actual:%Y-%m-%d})'

    @property
    def signed_amount(self) -> Decimal:
        """Сума зі знаком: доходи +, витрати −, перекази 0 для P&L-сенсу."""
        if self.type == self.TYPE_INCOME:
            return self.amount
        if self.type == self.TYPE_EXPENSE:
            return -self.amount
        return Decimal('0')

    @property
    def is_actual(self) -> bool:
        return self.status == self.STATUS_ACTUAL

    @property
    def is_planned(self) -> bool:
        return self.status == self.STATUS_PLANNED

    @property
    def planned_date(self):
        """Дата для прогнозу/календаря планових операцій."""
        return self.date_actual


class ObligationSettlement(models.Model):
    """Зв'язок «реальний платіж → погашене зобов'язання за конкретний період».

    Молекулярна точність обліку: кожна гривня погашення прив'язана до конкретної
    фактичної транзакції (з її рахунком і датою) та до конкретного зобов'язання
    (повторюване правило або разова планова) і періоду. Дозволяє кілька доплат на
    одне зобов'язання та чесну історію «що чим погашено». НЕ впливає на оцінку
    (template_amount) — погашення фактом ніколи не змінює плановану суму.
    """

    company = models.ForeignKey(Company, on_delete=models.CASCADE,
                                related_name='obligation_settlements')
    # Фактичний платіж, що погашає: expense для кредиторки, income для дебіторки.
    payment = models.ForeignKey('finance.Transaction', on_delete=models.CASCADE,
                                related_name='settlements')
    # Зобов'язання: повторюване правило АБО конкретна планова транзакція (onetime).
    rule = models.ForeignKey(RecurrenceRule, on_delete=models.SET_NULL, blank=True,
                             null=True, related_name='settlements')
    planned_txn = models.ForeignKey('finance.Transaction', on_delete=models.SET_NULL,
                                    blank=True, null=True, related_name='planned_settlements')
    period_key = models.CharField(max_length=16, blank=True, default='')   # 'YYYY-MM'
    period_label = models.CharField(max_length=64, blank=True, default='')
    amount = models.DecimalField(max_digits=18, decimal_places=2)
    currency = models.CharField(max_length=3, default='UAH')
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                   blank=True, null=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Погашення зобов'язання"
        verbose_name_plural = "Погашення зобов'язань"
        ordering = ['-created_at', '-id']
        indexes = [
            models.Index(fields=['company', 'rule']),
            models.Index(fields=['payment']),
        ]

    def __str__(self):
        return f'{self.amount} {self.currency} → {self.period_label or self.rule_id}'
