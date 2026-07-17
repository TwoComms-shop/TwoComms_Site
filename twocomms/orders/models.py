from decimal import Decimal

from django.conf import settings
from django.db import models, IntegrityError, transaction
from django.db.models import Max
from django.utils.translation import gettext_lazy as _
from storefront.models import Product, PromoCode
from productcolors.models import ProductColorVariant
from django.utils import timezone
import uuid


def _generate_payment_attempt_reference():
    return f'PA-{uuid.uuid4().hex[:20].upper()}'


class Order(models.Model):
    STATUS_CHOICES = [
        ('new', _('В обробці')),
        ('prep', _('Готується до відправлення')),
        ('ship', _('Відправлено')),
        ('done', _('Отримано')),
        ('cancelled', _('Скасовано')),
    ]

    PAY_TYPE_CHOICES = [
        ('online_full', _('Онлайн оплата (повна сума)')),
        ('prepay_200', _('Передплата 200 грн')),
        ('cod', _('Оплата при отриманні')),
        # Legacy values (для обратной совместимости)
        ('full', _('Повна оплата')),
        ('partial', _('Часткова оплата')),
    ]

    PAYMENT_STATUS_CHOICES = [
        ('unpaid', _('Не оплачено')),
        ('checking', _('На перевірці')),
        ('prepaid', _('Внесена передплата')),  # Было 'partial'
        ('paid', _('Оплачено повністю')),
        # Legacy value (для обратной совместимости)
        ('partial', _('Внесена передплата (старе)')),
    ]

    SOURCE_CHOICES = [
        ('web', _('Сайт')),
        ('manual', _('Створено вручну')),
    ]

    # Пресети джерела продажу для ручних замовлень (UI-підказки).
    # Поле ``sale_source`` лишається вільним текстом, тому оператор може
    # обрати варіант зі списку або вписати власний.
    SALE_SOURCE_PRESETS = [
        'Bezet',
        'Muyme',
        'Instagram',
        'Знайомі',
        'OLX',
        'Telegram',
        'Офлайн магазин',
        'Інше',
    ]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='orders', null=True, blank=True)
    order_number = models.CharField(max_length=20, unique=True, blank=True)
    session_key = models.CharField(max_length=40, blank=True, null=True)
    full_name = models.CharField(max_length=200)
    phone = models.CharField(max_length=32, db_index=True)
    email = models.EmailField(max_length=254, blank=True, null=True, db_index=True, verbose_name='Email')
    city = models.CharField(max_length=100)
    np_office = models.CharField(max_length=200)
    np_settlement_ref = models.CharField(max_length=36, blank=True)
    np_city_ref = models.CharField(max_length=36, blank=True)
    np_warehouse_ref = models.CharField(max_length=36, blank=True)
    pay_type = models.CharField(max_length=20, choices=PAY_TYPE_CHOICES, default='online_full')
    payment_status = models.CharField(max_length=20, choices=PAYMENT_STATUS_CHOICES, default='unpaid')
    total_sum = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    discount_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0, verbose_name='Сума знижки')
    promo_code = models.ForeignKey(PromoCode, on_delete=models.SET_NULL, null=True, blank=True, verbose_name='Використаний промокод')
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='new')
    tracking_number = models.CharField(max_length=50, blank=True, null=True, verbose_name='Номер ТТН')
    shipment_status = models.CharField(max_length=100, blank=True, null=True, verbose_name='Статус посылки')
    shipment_status_updated = models.DateTimeField(null=True, blank=True, verbose_name='Время обновления статуса')
    payment_screenshot = models.ImageField(upload_to='payment_screenshots/', blank=True, null=True, verbose_name='Скріншот оплати')
    payment_provider = models.CharField(max_length=50, blank=True, default='')
    payment_invoice_id = models.CharField(max_length=128, blank=True, default='')
    payment_payload = models.JSONField(blank=True, null=True)
    checkout_idempotency_key = models.CharField(
        max_length=64,
        unique=True,
        null=True,
        blank=True,
        editable=False,
    )
    nova_poshta_recipient_ref = models.CharField(max_length=36, blank=True, null=True)
    nova_poshta_recipient_contact_ref = models.CharField(max_length=36, blank=True, null=True)
    nova_poshta_document_ref = models.CharField(max_length=36, blank=True, null=True)
    admin_tg_chat_id = models.BigIntegerField(null=True, blank=True, verbose_name='Telegram admin chat id')
    admin_tg_message_id = models.BigIntegerField(null=True, blank=True, verbose_name='Telegram admin message id')
    admin_tg_messages = models.JSONField(blank=True, null=True)
    points_awarded = models.BooleanField(default=False, verbose_name='Бали нараховані')
    cancellation_reason = models.CharField(
        max_length=50,
        blank=True,
        null=True,
        choices=[
            ('out_of_stock', _('Немає товару')),
            ('no_print', _('Немає принта')),
            ('fake_number', _('Фейковий номер')),
            ('no_contact', _('Не виходить на зв’язок')),
            ('changed_mind', _('Передумав')),
            ('prepayment_refused', _('Не влаштувала передоплата')),
            ('other', _('Інше')),
        ],
        verbose_name=_('Причина скасування')
    )
    cancellation_comment = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        verbose_name='Коментар до скасування'
    )

    # UTM tracking fields
    utm_session = models.ForeignKey(
        'storefront.UTMSession',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='orders',
        verbose_name='UTM Сесія'
    )
    utm_source = models.CharField(max_length=255, blank=True, null=True, db_index=True, verbose_name='UTM Source')
    utm_medium = models.CharField(max_length=255, blank=True, null=True, db_index=True, verbose_name='UTM Medium')
    utm_campaign = models.CharField(max_length=255, blank=True, null=True, db_index=True, verbose_name='UTM Campaign')
    utm_content = models.CharField(max_length=255, blank=True, null=True, db_index=True, verbose_name='UTM Content')
    utm_term = models.CharField(max_length=255, blank=True, null=True, db_index=True, verbose_name='UTM Term')

    # Ручне створення замовлень (адмін створює замовлення з продажів поза сайтом)
    source = models.CharField(
        max_length=10,
        choices=SOURCE_CHOICES,
        default='web',
        db_index=True,
        verbose_name='Джерело замовлення (спосіб створення)',
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='created_orders',
        verbose_name='Створив (адмін)',
    )
    sale_source = models.CharField(
        max_length=120,
        blank=True,
        default='',
        verbose_name='Джерело продажу (звідки клієнт)',
    )
    manager_comment = models.TextField(
        blank=True,
        default='',
        verbose_name='Коментар менеджера',
    )

    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created']
        indexes = [
            models.Index(fields=['-created'], name='idx_order_created_desc'),
            models.Index(fields=['status', '-created'], name='idx_order_status_created'),
            models.Index(fields=['payment_status', '-created'], name='idx_order_payment_created'),
        ]

    def __str__(self):
        return f'Order {self.order_number} by {self.get_user_display()} — {self.get_status_display()}'

    def save(self, *args, **kwargs):
        attempts = 0
        while True:
            if not self.order_number:
                self.order_number = self.generate_order_number()
            try:
                with transaction.atomic():
                    super().save(*args, **kwargs)
                break
            except IntegrityError:
                attempts += 1
                if attempts >= 5:
                    raise
                # сбросим номер и попробуем ещё раз
                self.order_number = None

    def generate_order_number(self):
        """
        Генерирует уникальный номер заказа в формате TWC+дата+N+номер.
        Оптимизировано: использует Max() вместо перебора в цикле.
        """
        today = timezone.localdate()
        date_str = today.strftime('%d%m%Y')
        prefix = f"TWC{date_str}N"

        # Ищем максимальный номер за сегодня с этим префиксом
        # Используем startswith для надежности, хотя можно и по дате фильтровать
        last_order = Order.objects.filter(order_number__startswith=prefix).aggregate(Max('order_number'))
        max_number = last_order.get('order_number__max')

        if max_number:
            # Извлекаем последние цифры и инкрементируем
            try:
                # TWC30102025N01 -> 01
                suffix = max_number.replace(prefix, '')
                counter = int(suffix) + 1
            except ValueError:
                # Если формат нарушен, начинаем с 1 (или fallback на безопасный вариант)
                counter = 1
        else:
            counter = 1

        # Формируем новый номер
        return f"{prefix}{counter:02d}"

    def get_user_display(self):
        """Возвращает отображаемое имя пользователя"""
        if self.user:
            return f"{self.user.first_name} {self.user.last_name}".strip() or self.user.username
        return "Гість"

    @property
    def is_manual(self):
        """Замовлення створене вручну адміністратором (не через сайт)."""
        return self.source == 'manual'

    def get_payment_status_display(self):
        """Возвращает отображаемое название статуса оплаты"""
        payment_status_choices = dict([
            ('unpaid', 'Не оплачено'),
            ('checking', 'На перевірці'),
            ('prepaid', 'Внесена передплата'),
            ('paid', 'Оплачено повністю'),
            # Legacy
            ('partial', 'Внесена передплата'),
        ])
        return payment_status_choices.get(self.payment_status, self.payment_status)

    def get_facebook_event_id(self, event_type='purchase'):
        """
        Генерирует уникальный event_id для Facebook Pixel/Conversions API.
        Используется для дедупликации событий между браузером и сервером.

        Args:
            event_type: Тип события ('purchase', 'lead') - для генерации разных event_id

        Format: {order_number}_{timestamp}_{event_type}
        Example: TWC30102025N01_1730304000_purchase
        """
        import time
        # Используем order_number + timestamp создания + event_type как уникальный идентификатор
        timestamp = int(self.created.timestamp()) if self.created else int(time.time())
        return f"{self.order_number}_{timestamp}_{event_type}"

    def get_add_payment_event_id(self):
        """
        Генерирует event_id для AddPaymentInfo (добавление платёжных данных).
        Используется для дедупликации Pixel ↔ Conversions API при создании инвойса.

        Format: {order_number}_{timestamp}_add_payment_info
        Example: TWC30102025N01_1730304000_add_payment_info
        """
        return self.get_facebook_event_id(event_type='add_payment_info')

    def get_lead_event_id(self):
        """
        Генерирует event_id для Lead событий (предоплата).
        Используется в шаблоне order_success.html и для дедупликации с CAPI.

        Format: {order_number}_{timestamp}_lead
        Example: TWC30102025N01_1730304000_lead
        """
        return self.get_facebook_event_id(event_type='lead')

    def get_purchase_event_id(self):
        """
        Генерирует event_id для Purchase событий (полная оплата).
        Используется в шаблоне order_success.html и для дедупликации с CAPI.

        Format: {order_number}_{timestamp}_purchase
        Example: TWC30102025N01_1730304000_purchase
        """
        return self.get_facebook_event_id(event_type='purchase')

    def get_prepayment_amount(self):
        """Возвращает сумму предоплаты для pay_type=prepay_200"""
        from decimal import Decimal
        if self.pay_type == 'prepay_200':
            return min(Decimal('200.00'), self.final_total)
        return Decimal('0.00')

    def get_remaining_amount(self):
        """Возвращает остаток к оплате после предоплаты"""
        if self.pay_type == 'prepay_200':
            return max(self.final_total - self.get_prepayment_amount(), Decimal('0.00'))
        return self.final_total

    @classmethod
    def get_processing_count(cls):
        """Возвращает количество заказов в обработке"""
        return cls.objects.filter(status='new').count()

    @property
    def subtotal(self):
        """Сумма без скидки"""
        return sum(item.line_total for item in self.items.all())

    @property
    def final_total(self):
        """Итоговая сумма с учетом скидки"""
        return max(
            Decimal(self.total_sum or 0) - Decimal(self.discount_amount or 0),
            Decimal('0.00'),
        )

    @property
    def total_points(self):
        """Возвращает общее количество балов за заказ"""
        total = 0
        for item in self.items.all():
            if hasattr(item.product, 'points_reward') and item.product.points_reward:
                total += item.product.points_reward * item.qty
        return total

    def apply_promo_code(self, promo_code):
        """Применяет промокод к заказу"""
        if not promo_code or not promo_code.can_be_used():
            return False

        from decimal import Decimal

        subtotal = self.subtotal
        discount = promo_code.calculate_discount(subtotal)

        if discount > 0:
            # Преобразуем discount в Decimal для совместимости
            discount_decimal = Decimal(str(discount))
            self.discount_amount = discount_decimal
            self.total_sum = subtotal
            self.promo_code = promo_code
            return True

        return False


class PaymentAttempt(models.Model):
    """Immutable checkout/payment lifecycle before an Order exists."""

    class Status(models.TextChoices):
        INITIATED = 'initiated', _('Розпочато')
        PROCESSING = 'processing', _('Оплата в процесі')
        PAID = 'paid', _('Оплачено')
        PREPAID = 'prepaid', _('Внесена передплата')
        FAILED = 'failed', _('Неуспішно')
        EXPIRED = 'expired', _('Протерміновано')
        CANCELLED = 'cancelled', _('Скасовано')
        CONVERTED = 'converted', _('Перетворено на замовлення')

    class PayType(models.TextChoices):
        ONLINE_FULL = 'online_full', _('Онлайн оплата (повна сума)')
        PREPAY_200 = 'prepay_200', _('Передплата 200 грн')

    fingerprint = models.CharField(max_length=64, unique=True, db_index=True)
    reference = models.CharField(
        max_length=40, unique=True, default=_generate_payment_attempt_reference,
        editable=False,
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='payment_attempts',
    )
    session_key = models.CharField(max_length=40, blank=True, null=True, db_index=True)
    full_name = models.CharField(max_length=200)
    phone = models.CharField(max_length=32, db_index=True)
    email = models.EmailField(max_length=254, blank=True, null=True)
    city = models.CharField(max_length=100)
    np_office = models.CharField(max_length=200)
    np_settlement_ref = models.CharField(max_length=36, blank=True, default='')
    np_city_ref = models.CharField(max_length=36, blank=True, default='')
    np_warehouse_ref = models.CharField(max_length=36, blank=True, default='')
    pay_type = models.CharField(max_length=20, choices=PayType.choices)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.INITIATED)

    cart_snapshot = models.JSONField(default=dict)
    tracking_payload = models.JSONField(default=dict, blank=True)
    payment_history = models.JSONField(default=list, blank=True)
    notification_state = models.JSONField(default=dict, blank=True)
    event_state = models.JSONField(default=dict, blank=True)

    gross_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    discount_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    payable_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    payment_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    paid_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)

    promo_code = models.ForeignKey(
        PromoCode, null=True, blank=True, on_delete=models.SET_NULL, related_name='payment_attempts'
    )
    monobank_invoice_id = models.CharField(max_length=128, blank=True, default='', db_index=True)
    invoice_url = models.URLField(blank=True, default='')
    invoice_payload = models.JSONField(default=dict, blank=True)
    invoice_expires_at = models.DateTimeField(null=True, blank=True, db_index=True)
    error_reason = models.CharField(max_length=500, blank=True, default='')
    order = models.OneToOneField(
        'Order', null=True, blank=True, on_delete=models.SET_NULL, related_name='payment_attempt'
    )
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)
    last_status_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created', '-pk']
        indexes = [
            models.Index(fields=['status', '-created'], name='pay_attempt_status_created'),
            models.Index(fields=['user', '-created'], name='pay_attempt_user_created'),
            models.Index(fields=['invoice_expires_at'], name='pay_attempt_expiry'),
        ]

    @property
    def final_amount(self):
        return max(Decimal(self.payable_amount or 0), Decimal('0.00'))

    @property
    def prepayment_amount(self):
        if self.pay_type == self.PayType.PREPAY_200:
            return min(Decimal('200.00'), self.final_amount)
        return Decimal('0.00')

    @property
    def add_payment_event_id(self):
        return f'attempt-{self.pk}-add-payment-info'

    @property
    def order_number(self):
        return self.reference

    def get_add_payment_event_id(self):
        return self.add_payment_event_id

    def get_facebook_event_id(self, event_type='purchase'):
        suffix = {
            'add_payment_info': 'add-payment-info',
            'lead': 'lead',
            'purchase': 'purchase',
        }.get(event_type, event_type)
        return f'attempt-{self.pk}-{suffix}'

    @property
    def purchase_event_id(self):
        return f'attempt-{self.pk}-purchase'

    @property
    def lead_event_id(self):
        return f'attempt-{self.pk}-lead'

    @property
    def is_terminal(self):
        return self.status in {
            self.Status.PAID,
            self.Status.PREPAID,
            self.Status.FAILED,
            self.Status.EXPIRED,
            self.Status.CANCELLED,
            self.Status.CONVERTED,
        }

    @property
    def can_materialize(self):
        return self.status in {self.Status.INITIATED, self.Status.PROCESSING} and not self.order_id


class OrderItem(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name='items')
    product = models.ForeignKey(Product, on_delete=models.PROTECT, null=True, blank=True)
    color_variant = models.ForeignKey(ProductColorVariant, on_delete=models.PROTECT, null=True, blank=True)
    title = models.CharField(max_length=200)
    size = models.CharField(max_length=16, blank=True)
    fit_option_code = models.CharField(max_length=50, blank=True, default='')
    fit_option_label = models.CharField(max_length=100, blank=True, default='')
    option_values = models.JSONField(blank=True, default=dict)
    option_labels = models.JSONField(blank=True, default=dict)
    qty = models.PositiveIntegerField(default=1)
    unit_price = models.DecimalField(max_digits=12, decimal_places=2)
    line_total = models.DecimalField(max_digits=12, decimal_places=2)
    # Позиція поза каталогом (наприклад термо-футболка, якої немає в товарах).
    is_custom = models.BooleanField(default=False, verbose_name='Позиція поза каталогом')
    color_name_custom = models.CharField(max_length=100, blank=True, default='', verbose_name='Колір (для позиції поза каталогом)')

    def __str__(self):
        return f'{self.title} × {self.qty}'

    def get_offer_id(self):
        """Генерирует offer_id для синхронизации с Google Merchant Feed и пикселями"""
        if not self.product_id:
            return ''
        from storefront.utils.analytics_helpers import get_offer_id

        color_variant_id = self.color_variant.id if self.color_variant else None
        size = self.size if self.size else 'S'

        return get_offer_id(
            product_id=self.product.id,
            color_variant_id=color_variant_id,
            size=size
        )

    @property
    def fit_label(self):
        """Snapshot of selected product fit/cut for order displays."""
        return (self.fit_option_label or self.fit_option_code or '').strip()

    @property
    def generic_option_labels(self):
        fit_label = self.fit_label.casefold()
        return [
            f"{label}: {value}"
            for label, value in (self.option_labels or {}).items()
            if str(value or '').strip()
            and str(value or '').strip().casefold() != fit_label
        ]

    @property
    def color_name(self):
        """Возвращает название цвета или None"""
        if self.color_variant:
            color = getattr(self.color_variant, 'color', None)
            if color:
                name = (getattr(color, 'name', '') or '').strip()
                if name:
                    return name
                primary = (getattr(color, 'primary_hex', '') or '').strip().lstrip('#').upper()
                secondary = (getattr(color, 'secondary_hex', '') or '').strip().lstrip('#').upper()

                hex_map = {
                    '000000': 'чорний',
                    'FFFFFF': 'білий',
                    'FAFAFA': 'білий',
                    'F5F5F5': 'білий',
                    'FF0000': 'червоний',
                    'C1382F': 'бордовий',
                    'FFA500': 'помаранчевий',
                    'FFFF00': 'жовтий',
                    '00FF00': 'зелений',
                    '0000FF': 'синій',
                    '808080': 'сірий',
                    'A52A2A': 'коричневий',
                    '800080': 'фіолетовий',
                }

                if secondary:
                    a = hex_map.get(primary)
                    b = hex_map.get(secondary)
                    if a and b:
                        return f"{a}/{b}"
                if primary in hex_map:
                    return hex_map[primary]
                if secondary and secondary in hex_map:
                    return hex_map[secondary]
        # Позиція поза каталогом — колір зберігається текстом.
        custom = (self.color_name_custom or '').strip()
        return custom or None

    @property
    def product_image(self):
        """Возвращает изображение товара с учетом выбранного цвета"""
        if self.color_variant and self.color_variant.images.exists():
            return self.color_variant.images.first().image
        if self.product_id:
            return self.product.main_image
        return None


class WholesaleInvoice(models.Model):
    """Модель для оптовых накладных"""
    STATUS_CHOICES = [
        ('draft', 'Чернетка'),
        ('pending', 'Очікує перевірки'),
        ('processing', 'Прийнято в обробку'),
        ('shipped', 'Готується до відправки'),
        ('delivered', 'Товари відправлені'),
        ('cancelled', 'Скасовано'),
    ]

    REVIEW_STATUS_CHOICES = [
        ('draft', 'Чернетка'),
        ('pending', 'На перевірці'),
        ('approved', 'Підтверджено'),
        ('rejected', 'Відхилено'),
    ]

    invoice_number = models.CharField(max_length=100, unique=True, verbose_name="Номер накладної")
    company_name = models.CharField(max_length=200, verbose_name="Назва компанії/ФОП/ПІБ")
    company_number = models.CharField(max_length=50, blank=True, verbose_name="Номер компанії/ЄДРПОУ/ІПН")
    contact_phone = models.CharField(max_length=32, verbose_name="Номер телефону")
    delivery_address = models.TextField(verbose_name="Адреса доставки")
    store_link = models.URLField(blank=True, null=True, verbose_name="Посилання на магазин")

    total_tshirts = models.IntegerField(default=0, verbose_name="Кількість футболок")
    total_hoodies = models.IntegerField(default=0, verbose_name="Кількість худі")
    total_amount = models.DecimalField(max_digits=10, decimal_places=2, verbose_name="Загальна сума")

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='draft', verbose_name="Статус")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Дата створення")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Дата оновлення")

    # JSON поле для хранения деталей заказа
    order_details = models.JSONField(verbose_name="Деталі замовлення")

    # Путь к файлу накладной
    file_path = models.CharField(max_length=500, blank=True, null=True, verbose_name="Шлях до файлу")

    # Management workflow (перевірка накладних)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='created_wholesale_invoices',
        verbose_name="Створив (менеджер)",
    )
    review_status = models.CharField(
        max_length=20,
        choices=REVIEW_STATUS_CHOICES,
        default='draft',
        verbose_name="Статус перевірки",
    )
    review_reject_reason = models.TextField(blank=True, verbose_name="Причина відхилення")
    reviewed_at = models.DateTimeField(null=True, blank=True, verbose_name="Дата перевірки")
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='reviewed_wholesale_invoices',
        verbose_name="Перевірив (адмін)",
    )
    admin_tg_chat_id = models.BigIntegerField(null=True, blank=True, verbose_name="Telegram admin chat id")
    admin_tg_message_id = models.BigIntegerField(null=True, blank=True, verbose_name="Telegram admin message id")

    # Поле для одобрения накладной (доступна для оплаты)
    is_approved = models.BooleanField(default=False, verbose_name="Одобрена для оплаты")
    payment_status = models.CharField(
        max_length=20,
        choices=[
            ('not_paid', 'Не оплачена'),
            ('paid', 'Оплачена'),
            ('pending', 'Ожидает оплаты'),
            ('failed', 'Ошибка оплаты'),
            ('refunded', 'Повернено'),
        ],
        default='not_paid',
        verbose_name="Статус оплаты"
    )
    payment_url = models.URLField(blank=True, null=True, verbose_name="Посилання на оплату")
    monobank_invoice_id = models.CharField(max_length=128, blank=True, null=True, verbose_name="Monobank invoiceId")

    # === Management Admin Center: повний життєвий цикл оплати ===
    # Док: twocomms/Management Implementations/04_INVOICE_MONOBANK_PAYMENTS.md
    monobank_reference = models.CharField(
        max_length=64, blank=True, db_index=True,
        verbose_name="Monobank reference (MGMT-INV-{id})",
    )
    payment_link_created_at = models.DateTimeField(null=True, blank=True, verbose_name="Посилання створено")
    payment_link_copied_at = models.DateTimeField(null=True, blank=True, verbose_name="Посилання скопійовано")
    paid_at = models.DateTimeField(null=True, blank=True, verbose_name="Оплачено")
    payment_amount_minor = models.BigIntegerField(null=True, blank=True, verbose_name="Фактично оплачено (копійки)")
    # Повернення / коригування
    returned_at = models.DateTimeField(null=True, blank=True, verbose_name="Повернення оформлено")
    refund_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, verbose_name="Сума повернення")
    refund_reason = models.TextField(blank=True, verbose_name="Причина повернення")
    # Кількість одиниць (для KPI «8 одиниць у накладній»)
    units_count = models.PositiveIntegerField(null=True, blank=True, verbose_name="Кількість одиниць")

    class Meta:
        verbose_name = "Оптова накладна"
        verbose_name_plural = "Оптові накладні"
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.invoice_number} - {self.company_name}"

    @property
    def file_name(self):
        """Генерирует имя файла для Excel"""
        date_str = self.created_at.strftime('%Y%m%d_%H%M%S')
        return f"{self.company_name}_накладнаОПТ_{date_str}.xlsx"


class DropshipperOrder(models.Model):
    """Модель для заказов дропшиперов"""
    STATUS_CHOICES = [
        ('draft', 'Чернетка'),
        ('pending', 'Очікує підтвердження'),
        ('awaiting_payment', 'Очікує оплати'),
        ('confirmed', 'Підтверджено'),
        ('processing', 'В обробці'),
        ('awaiting_shipment', 'Очікує відправки'),
        ('shipped', 'Відправлено'),
        ('delivered_awaiting_pickup', 'Доставлено, очікує отримувача'),
        ('received', 'Товар отримано'),
        ('refused', 'Від товару відмовились'),
        ('delivered', 'Доставлено'),  # Оставляем для совместимости
        ('cancelled', 'Скасовано'),
    ]

    # Словарь для быстрого доступа к названиям статусов
    STATUS_CHOICES_DICT = dict(STATUS_CHOICES)

    PAYMENT_STATUS_CHOICES = [
        ('unpaid', 'Не оплачено'),
        ('paid', 'Оплачено'),
        ('refunded', 'Повернено'),
    ]

    PAYMENT_METHOD_CHOICES = [
        ('prepaid', 'Товар оплачено'),
        ('cod', 'Накладний платіж'),
        ('delegation', 'Повне делегування'),
    ]

    # Информация о дропшипере
    dropshipper = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='dropshipper_orders', verbose_name="Дропшипер")

    # Информация о клиенте
    client_name = models.CharField(max_length=200, verbose_name="ПІБ клієнта")
    client_phone = models.CharField(max_length=32, verbose_name="Телефон клієнта")
    client_np_address = models.CharField(max_length=500, verbose_name="Адреса НП клієнта")
    client_np_settlement_ref = models.CharField(max_length=36, blank=True, verbose_name="Ref населеного пункту НП")
    client_np_city_ref = models.CharField(max_length=36, blank=True, verbose_name="Ref міста НП")
    client_np_warehouse_ref = models.CharField(max_length=36, blank=True, verbose_name="Ref відділення НП")

    # Информация о заказе
    order_number = models.CharField(max_length=20, unique=True, blank=True, verbose_name="Номер замовлення")
    status = models.CharField(max_length=35, choices=STATUS_CHOICES, default='draft', verbose_name="Статус")
    payment_status = models.CharField(max_length=20, choices=PAYMENT_STATUS_CHOICES, default='unpaid', verbose_name="Статус оплати")

    # Финансовая информация
    total_drop_price = models.DecimalField(max_digits=10, decimal_places=2, default=0, verbose_name="Загальна ціна дропа")
    total_selling_price = models.DecimalField(max_digits=10, decimal_places=2, default=0, verbose_name="Загальна ціна продажу")
    profit = models.DecimalField(max_digits=10, decimal_places=2, default=0, verbose_name="Прибуток")

    # Информация об оплате
    payment_method = models.CharField(max_length=20, choices=PAYMENT_METHOD_CHOICES, default='cod', verbose_name="Спосіб оплати")
    prepayment_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0, verbose_name="Сума передоплати")
    dropshipper_payment_required = models.DecimalField(max_digits=10, decimal_places=2, default=0, verbose_name="Дропшипер має сплатити")
    monobank_invoice_id = models.CharField(max_length=100, blank=True, null=True, verbose_name="ID інвойсу Monobank")

    # Дополнительная информация
    notes = models.TextField(blank=True, null=True, verbose_name="Примітки")
    order_source = models.CharField(max_length=500, blank=True, null=True, verbose_name="Джерело замовлення")
    tracking_number = models.CharField(max_length=50, blank=True, null=True, verbose_name="Номер ТТН")
    shipment_status = models.CharField(max_length=100, blank=True, null=True, verbose_name='Статус НП')
    shipment_status_updated = models.DateTimeField(null=True, blank=True, verbose_name='Оновлення статусу НП')
    payout_processed = models.BooleanField(default=False, verbose_name="Виплату оброблено")

    # Временные метки
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Дата створення")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Дата оновлення")

    class Meta:
        verbose_name = "Замовлення дропшипера"
        verbose_name_plural = "Замовлення дропшиперів"
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['-created_at'], name='dropship_ord_created'),
            models.Index(fields=['status', '-created_at'], name='dropship_ord_status'),
            models.Index(fields=['payment_status', '-created_at'], name='dropship_ord_payment'),
        ]

    def __str__(self):
        return f"Дропшип замовлення #{self.order_number} - {self.client_name}"

    def save(self, *args, **kwargs):
        if not self.order_number:
            self.order_number = self.generate_order_number()

        # Автоматически меняем payment_status на paid если статус confirmed или выше
        # (confirmed, processing, awaiting_shipment, shipped, delivered_awaiting_pickup, received)
        # НО только если payment_status еще не был установлен вручную админом
        confirmed_and_higher_statuses = [
            'confirmed', 'processing', 'awaiting_shipment', 'shipped',
            'delivered_awaiting_pickup', 'received'
        ]

        if self.status in confirmed_and_higher_statuses and self.payment_status == 'unpaid':
            self.payment_status = 'paid'

        super().save(*args, **kwargs)

    def generate_order_number(self):
        """
        Генерирует уникальный номер заказа дропшипера.
        Оптимизировано: использует Max() вместо перебора.
        """
        from django.utils import timezone
        from django.db.models import Max

        today = timezone.localdate()
        date_str = today.strftime('%d%m%Y')
        prefix = f"DS{date_str}"

        last_order = DropshipperOrder.objects.filter(order_number__startswith=prefix).aggregate(Max('order_number'))
        max_number = last_order.get('order_number__max')

        if max_number:
            try:
                # DS30102025001 -> 001
                suffix = max_number.replace(prefix, '')
                counter = int(suffix) + 1
            except ValueError:
                counter = 1
        else:
            counter = 1

        return f"{prefix}{counter:03d}"

    def calculate_profit(self):
        """Рассчитывает прибыль от заказа"""
        self.profit = self.total_selling_price - self.total_drop_price
        return self.profit

    def calculate_dropshipper_payment(self):
        """Рассчитывает сумму которую должен оплатить дропшипер"""
        if self.payment_method == 'prepaid':
            # Товар оплачено - дропшипер платит полную стоимость дропа
            self.dropshipper_payment_required = self.total_drop_price
            self.prepayment_amount = 0
        elif self.payment_method == 'cod':
            # Накладний платіж - дропшипер платит предоплату 200 грн
            self.prepayment_amount = 200
            self.dropshipper_payment_required = 200
        else:  # delegation - повне делегування
            # Ми все беремо на себе - дропшипер нічого не платить
            self.prepayment_amount = 0
            self.dropshipper_payment_required = 0

        return self.dropshipper_payment_required

    def get_payment_method_display_detailed(self):
        """Возвращает детальное описание способа оплаты"""
        if self.payment_method == 'prepaid':
            return f"Товар оплачено (дропшипер сплачує {self.total_drop_price} грн)"
        elif self.payment_method == 'cod':
            return f"Накладний платіж (200 грн вираховується з суми при отриманні)"
        else:  # delegation
            return "Повне делегування (0 грн - всі ризики на нас)"

    def process_payout(self):
        """Обрабатывает выплату дропшиперу при получении товара"""
        if self.payout_processed:
            return False, "Виплату вже оброблено"


        # Сумма выплаты дропшиперу = его прибыль (независимо от способа оплаты)
        # 200 грн при COD - это наша страховка от клиента, она не влияет на прибыль дропшипера
        payout_amount = self.profit

        # Проверяем что сумма положительная
        if payout_amount <= 0:
            self.payout_processed = True
            self.save(update_fields=['payout_processed'])
            return False, f"Сума виплати <= 0 (прибуток: {payout_amount} грн)"

        # Обновляем available_for_payout в статистике дропшипера
        stats, created = DropshipperStats.objects.get_or_create(dropshipper=self.dropshipper)
        stats.available_for_payout += payout_amount
        stats.save(update_fields=['available_for_payout'])

        # Отмечаем что выплата обработана
        self.payout_processed = True
        self.save(update_fields=['payout_processed'])

        return True, f"Додано {payout_amount} грн до доступних виплат (метод: {self.get_payment_method_display()}, прибуток: {self.profit} грн)"

    def update_status_on_payment(self):
        """Обновляет статус заказа после оплаты"""
        if self.payment_status == 'paid':
            if self.payment_method in ['prepaid', 'cod']:
                # Для предоплаты и COD переводим в "Ожидает отправки"
                self.status = 'awaiting_shipment'
            else:  # delegation
                # Для делегирования сразу в "Подтверждено"
                self.status = 'confirmed'
            self.save(update_fields=['status'])
            return True
        return False

    def check_np_status_and_update(self):
        """Проверяет статус НП и обновляет статус заказа"""
        if not self.tracking_number:
            return False, "ТТН не вказано"

        try:
            from .nova_poshta_service import NovaPoshtaService
            np_service = NovaPoshtaService()
            status_info = np_service.get_tracking_info(self.tracking_number)

            if not status_info:
                return False, "Не вдалося отримати статус"

            old_status = self.shipment_status
            self.shipment_status = status_info.get('Status', '')
            self.shipment_status_updated = timezone.now()

            # Обновляем статус заказа на основе статуса НП
            np_status_code = status_info.get('StatusCode', 0)

            # 3 - Прибыло в отделение
            # 9 - Получено
            # 10 - Возвращено отправителю
            # 11 - Отказ от получения

            if np_status_code == 3:
                self.status = 'delivered_awaiting_pickup'
            elif np_status_code == 9:
                self.status = 'received'
                # Обрабатываем выплату только если еще не обработана
                if not self.payout_processed:
                    self.process_payout()
            elif np_status_code in [10, 11]:
                self.status = 'refused'

            self.save()

            return True, f"Статус оновлено: {old_status} -> {self.shipment_status}"

        except Exception as e:
            return False, f"Помилка: {str(e)}"


class DropshipperOrderItem(models.Model):
    """Элемент заказа дропшипера"""
    order = models.ForeignKey(DropshipperOrder, on_delete=models.CASCADE, related_name='items', verbose_name="Замовлення")
    product = models.ForeignKey('storefront.Product', on_delete=models.PROTECT, verbose_name="Товар")
    color_variant = models.ForeignKey('productcolors.ProductColorVariant', on_delete=models.PROTECT, null=True, blank=True, verbose_name="Колір")
    size = models.CharField(max_length=16, verbose_name="Розмір")
    fit_option_code = models.CharField(max_length=50, blank=True, default='', verbose_name="Код посадки")
    fit_option_label = models.CharField(max_length=100, blank=True, default='', verbose_name="Посадка")
    option_values = models.JSONField(blank=True, default=dict, verbose_name="Опції товару")
    option_labels = models.JSONField(blank=True, default=dict, verbose_name="Назви опцій")
    quantity = models.PositiveIntegerField(default=1, verbose_name="Кількість")

    # Цены
    drop_price = models.DecimalField(max_digits=10, decimal_places=2, verbose_name="Ціна дропа")
    selling_price = models.DecimalField(max_digits=10, decimal_places=2, verbose_name="Ціна продажу")
    recommended_price = models.DecimalField(max_digits=10, decimal_places=2, verbose_name="Рекомендована ціна")

    # Итоговые суммы
    total_drop_price = models.DecimalField(max_digits=10, decimal_places=2, verbose_name="Загальна ціна дропа")
    total_selling_price = models.DecimalField(max_digits=10, decimal_places=2, verbose_name="Загальна ціна продажу")
    item_profit = models.DecimalField(max_digits=10, decimal_places=2, verbose_name="Прибуток з товару")

    class Meta:
        verbose_name = "Товар в замовленні дропшипера"
        verbose_name_plural = "Товари в замовленнях дропшиперів"
        ordering = ['id']

    def __str__(self):
        return f"{self.product.title} - {self.size} x{self.quantity}"

    @property
    def fit_label(self):
        """Snapshot of selected product fit/cut for dropship order displays."""
        return (self.fit_option_label or self.fit_option_code or '').strip()

    @property
    def generic_option_labels(self):
        fit_label = self.fit_label.casefold()
        return [
            f"{label}: {value}"
            for label, value in (self.option_labels or {}).items()
            if str(value or '').strip()
            and str(value or '').strip().casefold() != fit_label
        ]

    def save(self, *args, **kwargs):
        # Рассчитываем итоговые суммы
        self.total_drop_price = self.drop_price * self.quantity
        self.total_selling_price = self.selling_price * self.quantity
        self.item_profit = self.total_selling_price - self.total_drop_price
        super().save(*args, **kwargs)


class DropshipperStats(models.Model):
    """Статистика дропшипера"""
    dropshipper = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='dropshipper_stats', verbose_name="Дропшипер")

    # Общая статистика
    total_orders = models.PositiveIntegerField(default=0, verbose_name="Всього замовлень")
    completed_orders = models.PositiveIntegerField(default=0, verbose_name="Виконаних замовлень")
    cancelled_orders = models.PositiveIntegerField(default=0, verbose_name="Скасованих замовлень")

    # Финансовая статистика
    total_revenue = models.DecimalField(max_digits=12, decimal_places=2, default=0, verbose_name="Загальний дохід")
    total_profit = models.DecimalField(max_digits=12, decimal_places=2, default=0, verbose_name="Загальний прибуток")
    total_drop_cost = models.DecimalField(max_digits=12, decimal_places=2, default=0, verbose_name="Загальна собівартість")
    available_for_payout = models.DecimalField(max_digits=12, decimal_places=2, default=0, verbose_name="Доступно до виплати")

    # Статистика по товарам
    total_items_sold = models.PositiveIntegerField(default=0, verbose_name="Всього товарів продано")

    # Система лояльности
    successful_orders = models.PositiveIntegerField(default=0, verbose_name="Успішних замовлень (отримано)")
    loyalty_discount = models.DecimalField(max_digits=6, decimal_places=2, default=0, verbose_name="Знижка лояльності (грн)")

    # Временные метки
    last_order_date = models.DateTimeField(null=True, blank=True, verbose_name="Дата останнього замовлення")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Дата створення")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Дата оновлення")

    class Meta:
        verbose_name = "Статистика дропшипера"
        verbose_name_plural = "Статистика дропшиперів"

    def __str__(self):
        return f"Статистика {self.dropshipper.username}"

    def update_loyalty_discount(self):
        """Обновляет скидку лояльности: -10 грн за каждый успешный заказ, максимум -120 грн"""
        from decimal import Decimal

        # Считаем успешные заказы (статус received)
        self.successful_orders = DropshipperOrder.objects.filter(
            dropshipper=self.dropshipper,
            status='received'
        ).count()

        # Рассчитываем скидку: -10 грн за заказ, но не больше -120 грн
        discount_per_order = Decimal('10.00')
        max_discount = Decimal('120.00')

        self.loyalty_discount = min(
            Decimal(self.successful_orders) * discount_per_order,
            max_discount
        )

        self.save(update_fields=['successful_orders', 'loyalty_discount'])
        return self.loyalty_discount

    def update_stats(self):
        """Обновляет статистику на основе заказов"""
        orders = DropshipperOrder.objects.filter(dropshipper=self.dropshipper)

        self.total_orders = orders.count()
        # Считаем заказы со статусом 'received' (клиент забрал товар)
        self.completed_orders = orders.filter(status='received').count()
        self.cancelled_orders = orders.filter(status='cancelled').count()

        # Финансовая статистика - считаем только полученные заказы (received)
        received_orders = orders.filter(status='received')
        self.total_revenue = sum(order.total_selling_price for order in received_orders)
        self.total_drop_cost = sum(order.total_drop_price for order in received_orders)
        self.total_profit = self.total_revenue - self.total_drop_cost

        # Статистика по товарам
        self.total_items_sold = sum(
            sum(item.quantity for item in order.items.all())
            for order in received_orders
        )

        # Дата последнего заказа
        last_order = orders.order_by('-created_at').first()
        if last_order:
            self.last_order_date = last_order.created_at

        # Обновляем скидку лояльности
        self.update_loyalty_discount()

        self.save()


class DropshipperPayout(models.Model):
    """Выплаты дропшиперам"""
    STATUS_CHOICES = [
        ('pending', 'Очікує виплати'),
        ('processing', 'В обробці'),
        ('completed', 'Виплачено'),
        ('cancelled', 'Скасовано'),
    ]

    PAYMENT_METHOD_CHOICES = [
        ('card', 'На картку'),
        ('iban', 'IBAN'),
    ]

    dropshipper = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='dropshipper_payouts', verbose_name="Дропшипер")
    order = models.ForeignKey(DropshipperOrder, on_delete=models.SET_NULL, null=True, blank=True, related_name='single_payout', verbose_name="Замовлення")

    # Информация о выплате
    payout_number = models.CharField(max_length=20, unique=True, blank=True, verbose_name="Номер виплати")
    amount = models.DecimalField(max_digits=10, decimal_places=2, verbose_name="Сума виплати")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending', verbose_name="Статус")
    payment_method = models.CharField(max_length=20, choices=PAYMENT_METHOD_CHOICES, blank=True, null=True, verbose_name="Спосіб виплати")
    description = models.CharField(max_length=500, blank=True, verbose_name="Опис")

    # Детали выплаты
    payment_details = models.CharField(max_length=500, blank=True, null=True, verbose_name="Деталі виплати (номер картки/рахунку)")
    notes = models.TextField(blank=True, null=True, verbose_name="Примітки")

    # Заказы, включенные в выплату
    included_orders = models.ManyToManyField(DropshipperOrder, related_name='payouts', blank=True, verbose_name="Включені замовлення")

    # Временные метки
    requested_at = models.DateTimeField(auto_now_add=True, verbose_name="Дата запиту")
    processed_at = models.DateTimeField(null=True, blank=True, verbose_name="Дата обробки")
    completed_at = models.DateTimeField(null=True, blank=True, verbose_name="Дата виплати")

    class Meta:
        verbose_name = "Виплата дропшипера"
        verbose_name_plural = "Виплати дропшиперів"
        ordering = ['-requested_at']

    def __str__(self):
        return f"Виплата #{self.payout_number} - {self.dropshipper.username} ({self.amount} грн)"

    def save(self, *args, **kwargs):
        if not self.payout_number:
            self.payout_number = self.generate_payout_number()
        super().save(*args, **kwargs)

    def generate_payout_number(self):
        """Генерирует уникальный номер выплаты"""
        from django.utils import timezone
        today = timezone.localdate()
        date_str = today.strftime('%d%m%Y')
        counter = 1

        while True:
            number = f"PY{date_str}{counter:03d}"
            if not DropshipperPayout.objects.filter(payout_number=number).exists():
                return number
            counter += 1


class CheckoutCapture(models.Model):
    """Снимок данных оформления заказа для восстановления брошенных корзин.

    Сохраняем всё, что покупатель ввёл в форму, даже если он не нажал
    «Оформити замовлення». Используется для уведомлений админам в Telegram
    и (при настроенном SMTP) recovery-писем.
    """

    session_key = models.CharField(max_length=64, unique=True, db_index=True)
    user = models.ForeignKey(
        'auth.User', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='checkout_captures',
    )
    full_name = models.CharField(max_length=255, blank=True, default='')
    phone = models.CharField(max_length=32, blank=True, default='')
    email = models.EmailField(blank=True, default='')
    cart_snapshot = models.JSONField(default=dict, blank=True)
    cart_total = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True, db_index=True)
    converted = models.BooleanField(default=False)
    admin_notified_at = models.DateTimeField(null=True, blank=True)
    recovery_sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = 'Незавершене оформлення'
        verbose_name_plural = 'Незавершені оформлення'

    def __str__(self):
        return f"capture {self.session_key[:8]}… {self.phone or self.email or 'anon'}"
