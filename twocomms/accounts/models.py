from django.db import models
from django.contrib.auth.models import User
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone
from pathlib import Path
from uuid import uuid4

# Persistent cart for cross-device synchronization.
# Imported here so Django app loader registers the model.
from .cart_models import UserCart  # noqa: F401
from .payment import PAY_TYPE_CHOICES


def private_ubd_document_path(_instance, filename):
    suffix = Path(filename or '').suffix.lower()
    if suffix not in {'.jpg', '.jpeg', '.png', '.webp'}:
        suffix = '.bin'
    return f'ubd_docs/{uuid4().hex}{suffix}'


class UserProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    phone = models.CharField(max_length=32, blank=True)
    city = models.CharField(max_length=100, blank=True)
    np_office = models.CharField(max_length=200, blank=True)
    np_settlement_ref = models.CharField(max_length=36, blank=True)
    np_city_ref = models.CharField(max_length=36, blank=True)
    np_warehouse_ref = models.CharField(max_length=36, blank=True)
    pay_type = models.CharField(max_length=20, choices=PAY_TYPE_CHOICES, default='online_full')
    avatar = models.ImageField(upload_to='avatars/', blank=True, null=True)
    full_name = models.CharField(max_length=200, blank=True)
    email = models.EmailField(max_length=254, blank=True, verbose_name='Email')
    telegram = models.CharField(max_length=100, blank=True, verbose_name='Telegram')
    telegram_id = models.BigIntegerField(null=True, blank=True, verbose_name='Telegram ID')
    instagram = models.CharField(max_length=100, blank=True, verbose_name='Instagram')
    whatsapp = models.CharField(max_length=100, blank=True, verbose_name='WhatsApp')
    viber = models.CharField(max_length=100, blank=True, verbose_name='Viber')
    birth_date = models.DateField(null=True, blank=True, verbose_name='Дата народження')
    is_ubd = models.BooleanField(default=False, verbose_name='УБД')
    ubd_doc = models.ImageField(upload_to=private_ubd_document_path, blank=True, null=True, verbose_name='Фото посвідчення УБД')
    # Менеджмент-бот
    tg_manager_chat_id = models.BigIntegerField(null=True, blank=True, verbose_name='Telegram Management Chat ID')
    tg_manager_username = models.CharField(max_length=255, blank=True, verbose_name='Telegram Management Username')
    tg_manager_bind_code = models.CharField(max_length=64, blank=True, verbose_name='Код привʼязки менеджмент-бота')
    tg_manager_bind_expires_at = models.DateTimeField(null=True, blank=True, verbose_name='Діє до')
    tg_manager_alert_15m = models.BooleanField(default=True, verbose_name='Нагадування за 15 хв')
    tg_manager_alert_5m = models.BooleanField(default=True, verbose_name='Нагадування за 5 хв')
    tg_manager_alert_due_now = models.BooleanField(default=True, verbose_name='Нагадування в момент дзвінка')
    tg_manager_alert_missed_callback = models.BooleanField(default=True, verbose_name='Алерт про пропущений передзвін')
    tg_manager_alert_report_late = models.BooleanField(default=True, verbose_name='Нагадування про прострочений звіт')
    tg_manager_daily_advice_enabled = models.BooleanField(default=True, verbose_name='Щоденні поради в Telegram')
    tg_manager_critical_advice_enabled = models.BooleanField(default=True, verbose_name='Критичні поради в Telegram')
    is_manager = models.BooleanField(default=False, verbose_name='Менеджер (доступ до Management)')
    # IP-телефонія (Binotel): внутрішня лінія менеджера (internalNumber, напр. 901).
    # Потрібна для вихідних дзвінків click-to-call: Binotel дзвонить на цю лінію
    # (софтфон/застосунок менеджера), потім з'єднує з клієнтом. Задає адмін.
    binotel_internal_number = models.CharField(
        max_length=32, blank=True, verbose_name='Лінія Binotel (internalNumber)'
    )
    push_marketing_enabled = models.BooleanField(
        default=True,
        verbose_name='Маркетингові push-сповіщення',
    )
    push_order_updates_enabled = models.BooleanField(
        default=True,
        verbose_name='Push-статуси замовлень',
    )

    # Налаштування менеджера (виплати)
    manager_position = models.CharField(max_length=120, blank=True, verbose_name='Посада/статус')
    manager_base_salary_uah = models.PositiveIntegerField(default=0, verbose_name='Ставка (грн)')
    manager_commission_percent = models.DecimalField(max_digits=6, decimal_places=2, default=0, verbose_name='Відсоток з продажів')
    manager_started_at = models.DateField(null=True, blank=True, verbose_name='Дата початку роботи')

    # Поля для оптовых заказов
    company_name = models.CharField(max_length=200, blank=True, verbose_name='Назва компанії/ФОП/ПІБ')
    company_number = models.CharField(max_length=50, blank=True, verbose_name='Номер компанії/ЄДРПОУ/ІПН')
    delivery_address = models.TextField(blank=True, verbose_name='Адреса доставки')
    website = models.URLField(blank=True, null=True, verbose_name='Посилання на магазин')
    payment_method = models.CharField(max_length=20, choices=[('card', 'На картку'), ('iban', 'IBAN')], default='card', verbose_name='Спосіб виплати')
    payment_details = models.TextField(blank=True, verbose_name='Реквізити для виплат')

    # Management Admin Center: онбординг-гейт і статус доступу.
    # Док: twocomms/Management Implementations/03_ONBOARDING_CONTRACTS_DIIA.md
    ACCESS_STATUS_CHOICES = [
        ('active', 'Активний'),
        ('blocked_until_document', 'Заблоковано до підпису документа'),
        ('suspended', 'Призупинено'),
        ('archived', 'Архів'),
    ]
    access_status = models.CharField(
        max_length=32, choices=ACCESS_STATUS_CHOICES, default='active', db_index=True,
        verbose_name='Статус доступу менеджера',
    )
    cooperation_started_at = models.DateField(null=True, blank=True, verbose_name='Початок співпраці')
    onboarding_required_version = models.CharField(
        max_length=32, blank=True, verbose_name='Необхідна версія правил для підпису',
    )

    class Meta:
        indexes = [
            models.Index(fields=['telegram_id'], name='idx_userprofile_telegram'),
            models.Index(fields=['phone'], name='idx_userprofile_phone'),
            models.Index(fields=['user', 'is_ubd'], name='idx_userprofile_user_ubd'),
        ]

    def __str__(self):
        return f'Profile for {self.user.username}'


class UserPoints(models.Model):
    """Модель для хранения баллов пользователей"""
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='points')
    points = models.PositiveIntegerField(default=0, verbose_name='Кількість балів')
    total_earned = models.PositiveIntegerField(default=0, verbose_name='Всього зароблено балів')
    total_spent = models.PositiveIntegerField(default=0, verbose_name='Всього витрачено балів')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='Створено')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='Оновлено')

    class Meta:
        verbose_name = 'Бали користувача'
        verbose_name_plural = 'Бали користувачів'

    def __str__(self):
        return f'{self.user.username} - {self.points} балів'

    def add_points(self, amount, reason=''):
        """Добавляет баллы пользователю"""
        self.points += amount
        self.total_earned += amount
        self.save()

        # Создаем запись в истории баллов
        PointsHistory.objects.create(
            user=self.user,
            points_change=amount,
            balance_after=self.points,
            reason=reason,
            change_type='earned'
        )

    def spend_points(self, amount, reason=''):
        """Тратит баллы пользователя"""
        if self.points >= amount:
            self.points -= amount
            self.total_spent += amount
            self.save()

            # Создаем запись в истории баллов
            PointsHistory.objects.create(
                user=self.user,
                points_change=-amount,
                balance_after=self.points,
                reason=reason,
                change_type='spent'
            )
            return True
        return False

    @classmethod
    def get_or_create_points(cls, user):
        """Получает или создает объект баллов для пользователя"""
        points, created = cls.objects.get_or_create(user=user)
        return points


class PointsHistory(models.Model):
    """История изменений баллов пользователя"""
    CHANGE_TYPES = [
        ('earned', 'Зароблено'),
        ('spent', 'Витрачено'),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='points_history')
    points_change = models.IntegerField(verbose_name='Зміна балів')
    balance_after = models.PositiveIntegerField(verbose_name='Баланс після зміни')
    reason = models.CharField(max_length=255, blank=True, verbose_name='Причина')
    change_type = models.CharField(max_length=10, choices=CHANGE_TYPES, verbose_name='Тип зміни')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='Створено')

    class Meta:
        verbose_name = 'Історія балів'
        verbose_name_plural = 'Історія балів'
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.user.username} - {self.points_change} балів ({self.get_change_type_display()})'


class FavoriteProduct(models.Model):
    """Модель для избранных товаров пользователя"""
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='favorites')
    product = models.ForeignKey('storefront.Product', on_delete=models.CASCADE, related_name='favorited_by')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='Додано до обраного')

    class Meta:
        verbose_name = 'Обраний товар'
        verbose_name_plural = 'Обрані товари'
        unique_together = ['user', 'product']  # Пользователь может добавить товар в избранное только один раз
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.user.username} - {self.product.title}'


# Signal to create UserProfile and UserPoints when a new User is created
@receiver(post_save, sender=User)
def create_user_related_models(sender, instance, created, **kwargs):
    """
    Automatically creates UserProfile and UserPoints for new users.

    Note: UserProfile is linked via OneToOneField, so it will be auto-created
    if accessed via instance.userprofile. However, we create it explicitly here
    to ensure it exists immediately after user creation.
    """
    if created:
        UserProfile.objects.get_or_create(user=instance)
        UserPoints.objects.get_or_create(user=instance)


# ───────────────────────────────────────────────────────────────────────────
# Telegram Contact Verification (для кастомного принта і не тільки)
# ───────────────────────────────────────────────────────────────────────────


class TelegramVerificationSession(models.Model):
    """Одноразова сесія верифікації Telegram-контакту через бота.

    Сценарій: користувач на сайті обирає канал «Telegram», натискає
    «Підтвердити Telegram». Сайт створює запис із `token` і повертає
    deep-link `t.me/<bot>?start=verify_<token>`. Бот при /start verify_*
    показує ReplyKeyboard з кнопкою «📱 Поділитись номером», користувач
    тисне — Telegram надсилає Contact, бот зберігає у цій моделі phone +
    username + user_id. Сайт поллить статус і автоматично заповнює форму.
    """

    STATUS_PENDING = "pending"
    STATUS_BOT_OPENED = "bot_opened"
    STATUS_VERIFIED = "verified"
    STATUS_EXPIRED = "expired"
    STATUS_CANCELLED = "cancelled"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Очікує підтвердження"),
        (STATUS_BOT_OPENED, "Користувач відкрив бота"),
        (STATUS_VERIFIED, "Підтверджено"),
        (STATUS_EXPIRED, "Прострочено"),
        (STATUS_CANCELLED, "Скасовано"),
    ]

    PURPOSE_CUSTOM_PRINT = "custom_print"
    PURPOSE_PROFILE_LINK = "profile_link"
    PURPOSE_LOGIN = "login"
    PURPOSE_MANAGEMENT_BIND = "management_bind"
    PURPOSE_DROPSHIPPER_LINK = "dropshipper_link"
    PURPOSE_RESTOCK = "restock"
    PURPOSE_CHOICES = [
        (PURPOSE_CUSTOM_PRINT, "Контакт у формі кастомного принта"),
        (PURPOSE_PROFILE_LINK, "Привʼязка профілю"),
        (PURPOSE_LOGIN, "Вхід через Telegram"),
        (PURPOSE_MANAGEMENT_BIND, "Привʼязка менеджмент-бота"),
        (PURPOSE_DROPSHIPPER_LINK, "Привʼязка дропшипера"),
        (PURPOSE_RESTOCK, "Очікування наявності товару"),
    ]

    token = models.CharField(
        max_length=64,
        unique=True,
        db_index=True,
        verbose_name="Токен сесії",
    )
    purpose = models.CharField(
        max_length=32,
        choices=PURPOSE_CHOICES,
        default=PURPOSE_CUSTOM_PRINT,
        verbose_name="Призначення",
    )
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_PENDING,
        verbose_name="Статус",
    )
    session_key = models.CharField(
        max_length=64,
        blank=True,
        default="",
        verbose_name="Ключ сесії браузера",
    )
    user = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="telegram_verification_sessions",
        verbose_name="Користувач (якщо авторизований)",
    )
    lead = models.ForeignKey(
        "storefront.CustomPrintLead",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="telegram_verifications",
        verbose_name="Заявка на кастомний принт",
    )
    initial_name = models.CharField(
        max_length=200,
        blank=True,
        default="",
        verbose_name="Імʼя клієнта на момент створення",
    )

    # Дані, отримані від Telegram після поділу контактом
    telegram_user_id = models.BigIntegerField(
        null=True,
        blank=True,
        verbose_name="Telegram user_id",
    )
    telegram_username = models.CharField(
        max_length=100,
        blank=True,
        default="",
        verbose_name="Telegram username",
    )
    telegram_first_name = models.CharField(
        max_length=120,
        blank=True,
        default="",
        verbose_name="Імʼя у Telegram",
    )
    telegram_last_name = models.CharField(
        max_length=120,
        blank=True,
        default="",
        verbose_name="Прізвище у Telegram",
    )
    phone = models.CharField(
        max_length=32,
        blank=True,
        default="",
        verbose_name="Телефон, отриманий з Telegram",
    )
    chat_id = models.BigIntegerField(
        null=True,
        blank=True,
        verbose_name="chat_id у боті",
    )

    # Додаткові метадані (наприклад: code прив'язки menager-бота, next URL для логіна)
    metadata = models.JSONField(
        blank=True,
        null=True,
        default=dict,
        verbose_name="Метадані сесії",
    )
    # Якщо purpose=LOGIN — після successful login тут лежить id користувача,
    # під яким залогінили (новий чи existing).
    resolved_user = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="telegram_resolved_sessions",
        verbose_name="Користувач (визначений після верифікації)",
    )
    consumed_at = models.DateTimeField(null=True, blank=True, verbose_name="Використано (login виконано)")

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    expires_at = models.DateTimeField(verbose_name="Діє до")
    completed_at = models.DateTimeField(null=True, blank=True, verbose_name="Підтверджено о")
    last_polled_at = models.DateTimeField(null=True, blank=True, verbose_name="Останній полл")

    class Meta:
        verbose_name = "Сесія верифікації Telegram"
        verbose_name_plural = "Сесії верифікації Telegram"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "expires_at"], name="idx_tgverify_status"),
            models.Index(fields=["session_key"], name="idx_tgverify_session"),
        ]

    def __str__(self):
        return f"TGVerify {self.token[:8]}… [{self.status}]"

    @property
    def is_expired(self) -> bool:
        return timezone.now() >= self.expires_at

    @property
    def is_active(self) -> bool:
        return self.status in {self.STATUS_PENDING, self.STATUS_BOT_OPENED} and not self.is_expired

    @property
    def is_verified(self) -> bool:
        return self.status == self.STATUS_VERIFIED

    def mark_bot_opened(self):
        if self.status == self.STATUS_PENDING:
            self.status = self.STATUS_BOT_OPENED
            self.save(update_fields=["status"])

    def mark_verified(
        self,
        *,
        telegram_user_id: int,
        phone: str,
        username: str = "",
        first_name: str = "",
        last_name: str = "",
        chat_id: int | None = None,
    ):
        self.telegram_user_id = telegram_user_id
        self.telegram_username = (username or "").lstrip("@")
        self.telegram_first_name = first_name or ""
        self.telegram_last_name = last_name or ""
        self.phone = phone
        if chat_id is not None:
            self.chat_id = chat_id
        self.status = self.STATUS_VERIFIED
        self.completed_at = timezone.now()
        self.save(
            update_fields=[
                "telegram_user_id",
                "telegram_username",
                "telegram_first_name",
                "telegram_last_name",
                "phone",
                "chat_id",
                "status",
                "completed_at",
            ]
        )

    def mark_polled(self):
        self.last_polled_at = timezone.now()
        self.save(update_fields=["last_polled_at"])

    def to_public_dict(self) -> dict:
        return {
            "token": self.token,
            "status": self.status,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "verified": self.is_verified,
            "phone": self.phone if self.is_verified else "",
            "telegram_username": self.telegram_username if self.is_verified else "",
            "telegram_first_name": self.telegram_first_name if self.is_verified else "",
            "telegram_last_name": self.telegram_last_name if self.is_verified else "",
            "telegram_user_id": self.telegram_user_id if self.is_verified else None,
        }
