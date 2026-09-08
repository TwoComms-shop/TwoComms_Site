import re
import uuid
from decimal import Decimal
from unicodedata import normalize as unicode_normalize
from urllib.parse import urlsplit

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.conf import settings
from twocomms.db_resilience import retry_mysql_read

try:
    import phonenumbers
    from phonenumbers import PhoneNumberFormat
except Exception:  # pragma: no cover - optional runtime dependency until installed everywhere
    phonenumbers = None
    PhoneNumberFormat = None


def _ua_phone_candidates(raw_phone: str) -> list[str]:
    raw = str(raw_phone or "").strip()
    digits = re.sub(r"\D+", "", raw)
    if not digits:
        return []

    candidates: list[str] = []
    for value in (
        raw,
        f"+{digits}" if raw.startswith("+") else "",
        f"+{digits}" if digits.startswith("380") and len(digits) == 12 else "",
        digits if digits.startswith("0") and len(digits) == 10 else "",
        f"0{digits[1:]}" if digits.startswith("8") and len(digits) == 10 else "",
        f"0{digits}" if len(digits) == 9 else "",
        f"0{digits[1:]}" if digits.startswith("80") and len(digits) == 11 else "",
    ):
        value = (value or "").strip()
        if value and value not in candidates:
            candidates.append(value)
    return candidates


def normalize_phone(raw_phone: str) -> str:
    digits = re.sub(r"\D+", "", raw_phone or "")
    if not digits:
        return ""

    if phonenumbers is not None:
        for candidate in _ua_phone_candidates(raw_phone):
            try:
                parsed = phonenumbers.parse(candidate, "UA")
            except Exception:
                continue
            if not phonenumbers.is_possible_number(parsed):
                continue
            if not phonenumbers.is_valid_number(parsed):
                continue
            try:
                return phonenumbers.format_number(parsed, PhoneNumberFormat.E164)
            except Exception:
                continue

    if digits.startswith("380") and len(digits) == 12:
        return f"+{digits}"
    if digits.startswith("80") and len(digits) == 11:
        return f"+38{digits[1:]}"
    if digits.startswith("0") and len(digits) == 10:
        return f"+38{digits}"
    if len(digits) == 9:
        return f"+380{digits}"
    if raw_phone and str(raw_phone).strip().startswith("+"):
        return f"+{digits}"
    return digits


LEGAL_ENTITY_TOKENS = {
    "тов",
    "тзов",
    "фоп",
    "llc",
    "ltd",
    "inc",
    "corp",
    "company",
}


def build_phone_last7(raw_phone: str) -> str:
    normalized = normalize_phone(raw_phone)
    digits = re.sub(r"\D+", "", normalized or raw_phone or "")
    return digits[-7:] if len(digits) >= 7 else digits


def normalize_name_for_match(raw_value: str) -> str:
    value = unicode_normalize("NFKC", str(raw_value or "")).lower().strip()
    value = re.sub(r"[^\w\s]", " ", value)
    tokens = [token for token in value.split() if token and token not in LEGAL_ENTITY_TOKENS]
    return " ".join(tokens)


def normalize_website_for_match(raw_url: str) -> str:
    value = (raw_url or "").strip().lower()
    if not value:
        return ""
    parsed = urlsplit(value if "://" in value else f"https://{value}")
    host = (parsed.netloc or parsed.path).lower()
    path = parsed.path if parsed.netloc else ""
    path = re.sub(r"/+$", "", path or "")
    if host.startswith("www."):
        host = host[4:]
    combined = f"{host}{path}"
    return combined.strip("/")


class Client(models.Model):
    class Role(models.TextChoices):
        SUPERVISOR = 'supervisor', _('Управляючий')
        MANAGER = 'manager', _('Менеджер')
        REALIZER = 'realizer', _('Реалізатор')
        OWNER = 'owner', _('Власник')
        OTHER = 'other', _('Інше')

    class CallResult(models.TextChoices):
        ORDER = 'order', _('Оформив замовлення')
        SENT_EMAIL = 'sent_email', _('Відправили КП на e-mail')
        SENT_MESSENGER = 'sent_messenger', _('Відправили КП у месенджери')
        WROTE_IG = 'wrote_ig', _('Написали в Instagram')
        NO_ANSWER = 'no_answer', _('Не відповідає')
        NOT_INTERESTED = 'not_interested', _('Не цікавить')
        INVALID_NUMBER = 'invalid_number', _('Номер недоступний')
        XML_CONNECTED = 'xml_connected', _('Підключив XML')
        THINKING = 'thinking', _('Подумає')
        EXPENSIVE = 'expensive', _('Дорого')
        WAITING_PAYMENT = 'waiting_payment', _('Очікується оплата')
        WAITING_PREPAYMENT = 'waiting_prepayment', _('Очікується передоплата')
        TEST_BATCH = 'test_batch', _('Замовив тестову партію')
        OTHER = 'other', _('Інше')

    shop_name = models.CharField(_("Назва магазину / Instagram"), max_length=255)
    phone = models.CharField(_("Номер телефону"), max_length=50)
    phone_normalized = models.CharField(_("Нормалізований номер"), max_length=50, blank=True, db_index=True)
    phone_last7 = models.CharField(_("Останні 7 цифр"), max_length=7, blank=True, db_index=True)
    normalized_name_display = models.CharField(_("Нормалізоване ім'я"), max_length=255, blank=True)
    normalized_name_match_key = models.CharField(_("Ключ збігу імені"), max_length=255, blank=True, db_index=True)
    is_shared_phone = models.BooleanField(_("Спільний номер"), default=False)
    phone_group_id = models.CharField(_("ID групи номера"), max_length=64, blank=True)
    shared_phone_reason = models.CharField(_("Причина спільного номера"), max_length=255, blank=True)
    website_url = models.CharField(_("Сайт"), max_length=500, blank=True)
    website_match_key = models.CharField(_("Ключ збігу сайту"), max_length=500, blank=True, db_index=True)
    full_name = models.CharField(_("ПІБ"), max_length=255)
    role = models.CharField(_("Статус"), max_length=50, choices=Role.choices, default=Role.MANAGER)
    source = models.CharField(_("Джерело контакту"), max_length=255, blank=True)
    call_result = models.CharField(_("Підсумок розмови"), max_length=50, choices=CallResult.choices, default=CallResult.NO_ANSWER)
    points_override = models.PositiveIntegerField(_("Кастомні бали"), null=True, blank=True)
    call_result_reason_code = models.CharField(_("Код причини підсумку"), max_length=64, blank=True, db_index=True)
    call_result_reason_note = models.TextField(_("Уточнення причини"), blank=True)
    call_result_context = models.JSONField(_("Контекст підсумку"), default=dict, blank=True)
    call_result_details = models.TextField(_("Деталі підсумку"), blank=True, help_text="Якщо вибрано 'Інше'")
    manager_note = models.TextField(_("Нотатка менеджера"), blank=True)
    phase_root = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="phase_family_members",
        verbose_name=_("Коренева фаза"),
    )
    previous_phase = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="next_phase_members",
        verbose_name=_("Попередня фаза"),
    )
    phase_number = models.PositiveIntegerField(_("Номер фази"), default=1, db_index=True)
    next_call_at = models.DateTimeField(_("Наступний дзвінок"), null=True, blank=True)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("Менеджер"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="management_clients"
    )
    created_at = models.DateTimeField(auto_now_add=True, null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True, null=True, blank=True)

    class Meta:
        verbose_name = _("Клієнт")
        verbose_name_plural = _("Клієнти")
        ordering = ['-created_at']

    def save(self, *args, **kwargs):
        self.phone_normalized = normalize_phone(self.phone)
        self.phone_last7 = build_phone_last7(self.phone_normalized or self.phone)
        self.normalized_name_display = normalize_name_for_match(self.shop_name)
        self.normalized_name_match_key = self.normalized_name_display
        self.website_match_key = normalize_website_for_match(self.website_url)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.shop_name} ({self.full_name})"


class ClientPhone(models.Model):
    """Додаткові номери телефону клієнта з категоріями.

    Аддитивно до `Client.phone` (який лишається канонічним «основним» номером
    для дедуплікації й матчингу). Тут — робочий/особистий/месенджер-номери,
    які менеджер може додати й з кожного зателефонувати.
    """

    class Category(models.TextChoices):
        WORK = "work", _("Робочий")
        PERSONAL = "personal", _("Особистий")
        VIBER = "viber", _("Viber")
        TELEGRAM = "telegram", _("Telegram")
        OTHER = "other", _("Інший")

    client = models.ForeignKey(
        Client,
        on_delete=models.CASCADE,
        related_name="phones",
        verbose_name=_("Клієнт"),
    )
    number = models.CharField(_("Номер"), max_length=50)
    number_normalized = models.CharField(_("Нормалізований"), max_length=50, blank=True, db_index=True)
    number_last7 = models.CharField(_("Останні 7 цифр"), max_length=7, blank=True, db_index=True)
    category = models.CharField(
        _("Категорія"), max_length=20, choices=Category.choices, default=Category.WORK, db_index=True
    )
    label = models.CharField(_("Підпис"), max_length=120, blank=True)
    is_primary = models.BooleanField(_("Основний"), default=False, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Телефон клієнта")
        verbose_name_plural = _("Телефони клієнта")
        ordering = ["-is_primary", "id"]
        indexes = [
            models.Index(fields=["client", "-is_primary"], name="mgmt_cliphone_cli_pri"),
            models.Index(fields=["number_last7"], name="mgmt_cliphone_last7"),
        ]

    def save(self, *args, **kwargs):
        self.number_normalized = normalize_phone(self.number)
        self.number_last7 = build_phone_last7(self.number_normalized or self.number)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.client_id}:{self.number} ({self.category})"


class ManagementLead(models.Model):
    class Status(models.TextChoices):
        MODERATION = "moderation", _("На модерації")
        BASE = "base", _("У базі")
        CONVERTED = "converted", _("Оброблено")
        REJECTED = "rejected", _("Не підходить")

    class LeadSource(models.TextChoices):
        MANUAL = "manual", _("Ручне додавання")
        PARSER = "parser", _("Парсинг")

    class NicheStatus(models.TextChoices):
        NICHE = "niche", _("Нішевий")
        MAYBE = "maybe", _("Під питанням")
        NON_NICHE = "non_niche", _("Не нішевий")

    shop_name = models.CharField(_("Назва магазину"), max_length=255)
    phone = models.CharField(_("Номер телефону"), max_length=50)
    phone_normalized = models.CharField(_("Нормалізований номер"), max_length=50, blank=True, db_index=True)
    phone_last7 = models.CharField(_("Останні 7 цифр"), max_length=7, blank=True, db_index=True)
    normalized_name_display = models.CharField(_("Нормалізоване ім'я"), max_length=255, blank=True)
    normalized_name_match_key = models.CharField(_("Ключ збігу імені"), max_length=255, blank=True, db_index=True)
    is_shared_phone = models.BooleanField(_("Спільний номер"), default=False)
    phone_group_id = models.CharField(_("ID групи номера"), max_length=64, blank=True)
    shared_phone_reason = models.CharField(_("Причина спільного номера"), max_length=255, blank=True)
    full_name = models.CharField(_("ПІБ"), max_length=255, blank=True)
    role = models.CharField(_("Статус людини"), max_length=50, choices=Client.Role.choices, default=Client.Role.OTHER)
    source = models.CharField(_("Де взяли контакт"), max_length=255, blank=True)
    website_url = models.CharField(_("Сайт"), max_length=500, blank=True)
    website_match_key = models.CharField(_("Ключ збігу сайту"), max_length=500, blank=True, db_index=True)
    city = models.CharField(_("Місто"), max_length=120, blank=True)
    parser_keyword = models.CharField(_("Ключове слово парсера"), max_length=255, blank=True)
    parser_query = models.CharField(_("Пошуковий запит"), max_length=500, blank=True)
    google_place_id = models.CharField(_("Google Place ID"), max_length=255, blank=True, db_index=True)
    google_maps_url = models.CharField(_("Google Maps URL"), max_length=500, blank=True)
    details = models.TextField(_("Деталі"), blank=True)
    comments = models.TextField(_("Коментарі"), blank=True)
    extra_data = models.JSONField(_("Сирі дані"), default=dict, blank=True)
    status = models.CharField(_("Статус"), max_length=20, choices=Status.choices, default=Status.BASE, db_index=True)
    lead_source = models.CharField(_("Джерело ліда"), max_length=20, choices=LeadSource.choices, default=LeadSource.MANUAL, db_index=True)
    niche_status = models.CharField(_("Нішевість"), max_length=20, choices=NicheStatus.choices, default=NicheStatus.MAYBE, db_index=True)
    network = models.ForeignKey("LeadNetwork", verbose_name=_("Мережа"), on_delete=models.SET_NULL, null=True, blank=True, related_name="leads", db_index=True, db_constraint=False)
    network_membership_source = models.CharField(_("Джерело привʼязки до мережі"), max_length=8, blank=True)
    needs_disambiguation = models.BooleanField(_("Родова назва (розрізнити)"), default=False, db_index=True)
    ai_score = models.PositiveSmallIntegerField(_("AI-оцінка"), null=True, blank=True, db_index=True)
    ai_verdict = models.CharField(_("AI-вердикт"), max_length=10, blank=True, db_index=True)
    ai_checked_at = models.DateTimeField(_("AI-перевірено"), null=True, blank=True, db_index=True)
    requires_phone_completion = models.BooleanField(_("Потрібно дозаповнити телефон"), default=False, db_index=True)
    rejection_reason = models.TextField(_("Причина відхилення"), blank=True)
    added_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("Додав"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="management_leads_added",
    )
    moderated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("Модератор"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="management_leads_moderated",
    )
    processed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("Обробив"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="management_leads_processed",
    )
    converted_client = models.OneToOneField(
        Client,
        verbose_name=_("Створений клієнт"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="source_lead",
    )
    parser_job = models.ForeignKey(
        "LeadParsingJob",
        verbose_name=_("Сесія парсингу"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="generated_leads",
    )
    approved_to_base_at = models.DateTimeField(_("Додано у базу"), null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Лід")
        verbose_name_plural = _("Ліди")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "-created_at"], name="mgmt_lead_status_dt"),
            models.Index(fields=["lead_source", "-created_at"], name="mgmt_lead_src_dt"),
            models.Index(fields=["city", "status"], name="mgmt_lead_city_st"),
        ]

    def save(self, *args, **kwargs):
        self.phone_normalized = normalize_phone(self.phone)
        self.phone_last7 = build_phone_last7(self.phone_normalized or self.phone)
        self.normalized_name_display = normalize_name_for_match(self.shop_name)
        self.normalized_name_match_key = self.normalized_name_display
        self.website_match_key = normalize_website_for_match(self.website_url)
        super().save(*args, **kwargs)

    @property
    def added_by_display(self) -> str:
        if self.lead_source == self.LeadSource.PARSER:
            return "парсинг"
        if self.added_by:
            return self.added_by.get_full_name() or self.added_by.username
        return "невідомо"

    def __str__(self):
        return f"{self.shop_name} ({self.get_status_display()})"


class LeadParsingJob(models.Model):
    class Status(models.TextChoices):
        RUNNING = "running", _("Працює")
        PAUSED = "paused", _("Пауза")
        STOPPED = "stopped", _("Зупинено")
        COMPLETED = "completed", _("Завершено")
        FAILED = "failed", _("Помилка")

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="management_lead_parsing_jobs",
        verbose_name=_("Створив"),
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.RUNNING, db_index=True)
    keywords_raw = models.TextField(_("Ключові слова (raw)"), blank=True)
    cities_raw = models.TextField(_("Міста (raw)"), blank=True)
    keywords = models.JSONField(_("Ключові слова"), default=list, blank=True)
    cities = models.JSONField(_("Міста"), default=list, blank=True)
    request_limit = models.PositiveIntegerField(_("Ліміт запитів"), default=100)
    target_leads_limit = models.PositiveIntegerField(_("Ціль по лідах"), default=0)
    request_count = models.PositiveIntegerField(_("Виконано запитів"), default=0)
    requests_per_minute = models.PositiveIntegerField(_("Запитів за хвилину"), default=10)
    history_lookback_days = models.PositiveIntegerField(_("Lookback по історії, днів"), default=30)
    save_no_phone_leads = models.BooleanField(_("Зберігати ліди без телефону"), default=False)
    included_type = models.CharField(_("Google Places type"), max_length=64, blank=True)
    included_types = models.JSONField(_("Google Places types"), default=list, blank=True)
    strict_type_filtering = models.BooleanField(_("Strict type filtering"), default=False)
    request_success_count = models.PositiveIntegerField(_("Успішних API запитів"), default=0)
    request_error_count = models.PositiveIntegerField(_("Помилок API"), default=0)
    current_keyword_index = models.PositiveIntegerField(default=0)
    current_city_index = models.PositiveIntegerField(default=0)
    current_type_index = models.PositiveIntegerField(default=0)
    total_found = models.PositiveIntegerField(default=0)
    no_phone_skipped = models.PositiveIntegerField(default=0)
    duplicate_skipped = models.PositiveIntegerField(default=0)
    duplicate_same_job_phone_skipped = models.PositiveIntegerField(default=0)
    duplicate_same_job_place_skipped = models.PositiveIntegerField(default=0)
    duplicate_existing_client_skipped = models.PositiveIntegerField(default=0)
    duplicate_existing_lead_skipped = models.PositiveIntegerField(default=0)
    recent_history_phone_skipped = models.PositiveIntegerField(default=0)
    recent_history_place_skipped = models.PositiveIntegerField(default=0)
    saved_no_phone_to_moderation = models.PositiveIntegerField(default=0)
    added_to_moderation = models.PositiveIntegerField(default=0)
    moved_to_bad = models.PositiveIntegerField(default=0)
    already_rejected_skipped = models.PositiveIntegerField(default=0)
    queries_exhausted_normal = models.PositiveIntegerField(default=0)
    queries_exhausted_anomaly = models.PositiveIntegerField(default=0)
    current_query = models.CharField(max_length=500, blank=True)
    current_request_spec = models.JSONField(default=dict, blank=True)
    next_page_token = models.TextField(blank=True)
    next_step_not_before = models.DateTimeField(null=True, blank=True, db_index=True)
    retry_state = models.JSONField(default=dict, blank=True)
    is_step_in_progress = models.BooleanField(default=False, db_index=True)
    last_step_started_at = models.DateTimeField(null=True, blank=True)
    last_step_finished_at = models.DateTimeField(null=True, blank=True)
    last_step_duration_ms = models.PositiveIntegerField(default=0)
    heartbeat_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_error = models.TextField(blank=True)
    stop_reason_code = models.CharField(max_length=64, blank=True, db_index=True)
    started_at = models.DateTimeField(auto_now_add=True, db_index=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Сесія парсингу лідів")
        verbose_name_plural = _("Сесії парсингу лідів")
        ordering = ["-started_at"]
        indexes = [
            models.Index(fields=["status", "-started_at"], name="mgmt_parse_status_dt"),
        ]

    def __str__(self):
        return f"ParseJob#{self.id} ({self.status})"


class LeadParsingRuntimeLock(models.Model):
    singleton_key = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    active_job = models.ForeignKey(
        LeadParsingJob,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("Активна сесія"),
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Глобальний lock парсингу")
        verbose_name_plural = _("Глобальні lock-и парсингу")

    def __str__(self):
        return f"ParserLock(active_job={self.active_job_id or 'none'})"


class LeadParsingResult(models.Model):
    class ResultStatus(models.TextChoices):
        ADDED = "added", _("Додано до модерації")
        ADDED_NO_PHONE = "added_no_phone", _("Додано до модерації без телефону")
        NOTICE = "notice", _("Службове повідомлення")
        DUPLICATE = "duplicate", _("Дубль")
        NO_PHONE = "no_phone", _("Без телефону")
        REJECTED = "rejected", _("Раніше відхилено")
        ERROR = "error", _("Помилка")

    job = models.ForeignKey(
        LeadParsingJob,
        on_delete=models.CASCADE,
        related_name="results",
        verbose_name=_("Сесія парсингу"),
    )
    lead = models.ForeignKey(
        ManagementLead,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="parser_results",
        verbose_name=_("Лід"),
    )
    keyword = models.CharField(max_length=255, blank=True, verbose_name=_("Ключове слово"))
    city = models.CharField(max_length=120, blank=True, verbose_name=_("Місто"))
    query = models.CharField(max_length=500, blank=True, verbose_name=_("Запит"))
    place_id = models.CharField(max_length=255, blank=True, db_index=True, verbose_name=_("Place ID"))
    place_name = models.CharField(max_length=255, blank=True, verbose_name=_("Назва"))
    phone = models.CharField(max_length=50, blank=True, verbose_name=_("Телефон"))
    website_url = models.CharField(max_length=500, blank=True, verbose_name=_("Сайт"))
    maps_url = models.CharField(max_length=500, blank=True, verbose_name=_("Google Maps URL"))
    status = models.CharField(max_length=20, choices=ResultStatus.choices, default=ResultStatus.ADDED, db_index=True)
    reason = models.CharField(max_length=255, blank=True, verbose_name=_("Причина"))
    reason_code = models.CharField(max_length=64, blank=True, db_index=True, verbose_name=_("Код причини"))
    payload = models.JSONField(default=dict, blank=True, verbose_name=_("Payload"))
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = _("Результат парсингу")
        verbose_name_plural = _("Результати парсингу")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["job", "-created_at"], name="mgmt_parse_res_job_dt"),
            models.Index(fields=["status", "-created_at"], name="mgmt_parse_res_st_dt"),
            models.Index(fields=["job", "place_id"], name="mgmt_parse_res_job_place"),
            models.Index(fields=["job", "phone"], name="mgmt_parse_res_job_phone"),
        ]

    def __str__(self):
        return f"{self.job_id}:{self.status}:{self.place_name}"


class LeadParsingQueryState(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", _("Активний")
        EXHAUSTED = "exhausted", _("Вичерпано")
        ANOMALY = "anomaly", _("Аномалія")

    job = models.ForeignKey(
        LeadParsingJob,
        on_delete=models.CASCADE,
        related_name="query_states",
        verbose_name=_("Сесія парсингу"),
    )
    keyword = models.CharField(max_length=255, blank=True, verbose_name=_("Ключове слово"))
    city = models.CharField(max_length=120, blank=True, verbose_name=_("Місто"))
    included_type = models.CharField(max_length=64, blank=True, verbose_name=_("Google Places type"))
    text_query = models.CharField(max_length=500, blank=True, verbose_name=_("Текстовий запит"))
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE, db_index=True)
    pages_fetched = models.PositiveIntegerField(default=0)
    api_requests_sent = models.PositiveIntegerField(default=0)
    places_seen_count = models.PositiveIntegerField(default=0)
    places_added_count = models.PositiveIntegerField(default=0)
    consecutive_empty_pages = models.PositiveIntegerField(default=0)
    consecutive_repeated_pages = models.PositiveIntegerField(default=0)
    last_next_page_token_hash = models.CharField(max_length=64, blank=True)
    seen_page_fingerprints = models.JSONField(default=list, blank=True)
    exhausted_reason_code = models.CharField(max_length=64, blank=True, db_index=True)
    exhausted_message = models.CharField(max_length=255, blank=True)
    last_progress_at = models.DateTimeField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Стан query парсингу")
        verbose_name_plural = _("Стани query парсингу")
        constraints = [
            models.UniqueConstraint(
                fields=["job", "keyword", "city", "included_type"],
                name="mgmt_parse_query_state_unique",
            ),
        ]
        indexes = [
            models.Index(fields=["job", "status"], name="mgmt_parse_qs_job_status"),
            models.Index(fields=["job", "-updated_at"], name="mgmt_parse_qs_job_dt"),
        ]

    def __str__(self):
        return f"{self.job_id}:{self.keyword}:{self.city}:{self.included_type or 'all'}:{self.status}"


class Report(models.Model):
    id = models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("Менеджер"),
        on_delete=models.CASCADE,
        related_name="management_reports"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    points = models.PositiveIntegerField(default=0)
    processed = models.PositiveIntegerField(default=0)
    file = models.FileField(upload_to='reports/', blank=True, null=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = _("Звіт")
        verbose_name_plural = _("Звіти")

    def __str__(self):
        return f"Звіт {self.owner} {self.created_at:%Y-%m-%d}"


class DayReportAudit(models.Model):
    """Комплексний ШІ-аудит робочого дня менеджера (при відправці звіту).

    ШІ знає все: кожного обробленого клієнта (що ввів менеджер) + кожен дзвінок
    (через CallAIAnalysis) + дисципліну передзвонів + стаж/історію звітів. Дає
    адміну точний аудит дня: сходиться чи ні, що пропущено, прогрес, перспективи,
    недоробки. Бали/аудит — лише для адміна.
    """

    class Status(models.TextChoices):
        PENDING = "pending", _("Очікує")
        RUNNING = "running", _("Аналізується")
        DONE = "done", _("Готово")
        ERROR = "error", _("Помилка")
        SKIPPED = "skipped", _("Пропущено")

    class Verdict(models.TextChoices):
        STRONG = "strong", _("Сильний день")
        OK = "ok", _("Робочий день")
        WEAK = "weak", _("Слабкий день")
        UNKNOWN = "unknown", _("Невизначено")

    report = models.ForeignKey(
        Report, on_delete=models.CASCADE, related_name="ai_audits", verbose_name=_("Звіт")
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="management_day_audits", verbose_name=_("Менеджер"),
    )
    day = models.DateField(db_index=True, verbose_name=_("День звіту"))
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING, db_index=True)
    model = models.CharField(max_length=64, blank=True)
    day_score = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0.00"))
    verdict = models.CharField(max_length=20, choices=Verdict.choices, default=Verdict.UNKNOWN)
    summary = models.TextField(blank=True)
    matches = models.JSONField(default=list, blank=True)
    mismatches = models.JSONField(default=list, blank=True)
    missed_callbacks = models.JSONField(default=list, blank=True)
    growth = models.JSONField(default=dict, blank=True)
    weaknesses = models.JSONField(default=list, blank=True)
    recommendations = models.JSONField(default=list, blank=True)
    prospects = models.TextField(blank=True)
    result = models.JSONField(default=dict, blank=True)
    tenure_days = models.PositiveIntegerField(default=0)
    reports_count = models.PositiveIntegerField(default=0)
    calls_total = models.PositiveIntegerField(default=0)
    calls_analyzed = models.PositiveIntegerField(default=0)
    prompt_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)
    elapsed_ms = models.PositiveIntegerField(default=0)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("ШІ-аудит дня")
        verbose_name_plural = _("ШІ-аудити дня")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["owner", "-day"], name="mgmt_dayaudit_owner_day"),
            models.Index(fields=["status", "-created_at"], name="mgmt_dayaudit_status_dt"),
        ]

    def __str__(self):
        return f"DayAudit[{self.status}] {self.owner_id}:{self.day}"


class ReminderSent(models.Model):
    id = models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")
    key = models.CharField(max_length=255, db_index=True)
    chat_id = models.BigIntegerField()
    sent_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('key', 'chat_id')


class ReminderRead(models.Model):
    id = models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="management_read_reminders",
        verbose_name=_("Користувач"),
    )
    key = models.CharField(max_length=255, db_index=True)
    read_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('user', 'key')
        verbose_name = _("Прочитане нагадування")
        verbose_name_plural = _("Прочитані нагадування")


class ManagerNotification(models.Model):
    """In-app сповіщення для менеджера (дзвіночок у хедері).

    Дублює ключові події, що приходять у Telegram: винагороди (виплати),
    рішення по накладних/договорах тощо."""

    class Kind(models.TextChoices):
        PAYOUT = 'payout', _('Винагорода')
        INVOICE = 'invoice', _('Накладна')
        CONTRACT = 'contract', _('Договір')
        SYSTEM = 'system', _('Система')

    class Level(models.TextChoices):
        INFO = 'info', _('Інфо')
        SUCCESS = 'success', _('Успіх')
        WARNING = 'warning', _('Увага')
        DANGER = 'danger', _('Помилка')

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='management_notifications',
        verbose_name=_('Менеджер'),
    )
    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.SYSTEM, db_index=True)
    level = models.CharField(max_length=12, choices=Level.choices, default=Level.INFO)
    title = models.CharField(max_length=255, verbose_name=_('Заголовок'))
    body = models.TextField(blank=True, verbose_name=_('Текст'))
    amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, verbose_name=_('Сума'))
    is_read = models.BooleanField(default=False, db_index=True)
    # Попередження, що потребують явного підтвердження «ОК» (напр. розбіжності
    # ШІ-аналізу дзвінка з тим, що зберіг менеджер).
    requires_ack = models.BooleanField(default=False, db_index=True, verbose_name=_('Потребує підтвердження'))
    acknowledged_at = models.DateTimeField(null=True, blank=True, verbose_name=_('Підтверджено о'))
    related_client = models.ForeignKey(
        'management.Client', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='notifications', verbose_name=_('Клієнт'),
    )
    related_analysis = models.ForeignKey(
        'management.CallAIAnalysis', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='notifications', verbose_name=_('ШІ-аналіз'),
    )
    action_url = models.CharField(max_length=500, blank=True, verbose_name=_('Посилання дії'))
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = _('Сповіщення менеджера')
        verbose_name_plural = _('Сповіщення менеджерів')
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user', '-created_at'], name='mgmt_notif_user_dt'),
            models.Index(fields=['user', 'is_read'], name='mgmt_notif_user_read'),
        ]

    def __str__(self):
        return f"{self.user_id}: {self.title}"


class InvoiceRejectionReasonRequest(models.Model):
    invoice = models.ForeignKey(
        'orders.WholesaleInvoice',
        on_delete=models.CASCADE,
        related_name='management_rejection_reason_requests',
        verbose_name=_("Накладна"),
    )
    admin_chat_id = models.BigIntegerField(db_index=True, verbose_name=_("Telegram chat id (адмін)"))
    prompt_message_id = models.BigIntegerField(null=True, blank=True, verbose_name=_("Message id запиту"))
    is_active = models.BooleanField(default=True, db_index=True, verbose_name=_("Активний запит"))
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("Запит причини відхилення накладної")
        verbose_name_plural = _("Запити причин відхилення накладних")
        ordering = ['-created_at']


class CommercialOfferEmailSettings(models.Model):
    class CpMode(models.TextChoices):
        LIGHT = "LIGHT", _("Light")
        VISUAL = "VISUAL", _("Visual")

    class CpSegmentMode(models.TextChoices):
        NEUTRAL = "NEUTRAL", _("Neutral")
        EDGY = "EDGY", _("Edgy")

    class CpSubjectPreset(models.TextChoices):
        PRESET_1 = "PRESET_1", _("Preset 1")
        PRESET_2 = "PRESET_2", _("Preset 2")
        PRESET_3 = "PRESET_3", _("Preset 3")
        CUSTOM = "CUSTOM", _("Custom")

    class CpCtaType(models.TextChoices):
        TELEGRAM_MANAGER = "TELEGRAM_MANAGER", _("Telegram менеджера")
        WHATSAPP_MANAGER = "WHATSAPP_MANAGER", _("WhatsApp менеджера")
        TELEGRAM_GENERAL = "TELEGRAM_GENERAL", _("Telegram загальний")
        MAILTO_COOPERATION = "MAILTO_COOPERATION", _("Email cooperation@")
        REPLY_HINT_ONLY = "REPLY_HINT_ONLY", _("Відповідь на лист (без лінка)")
        CUSTOM_URL = "CUSTOM_URL", _("Custom URL")

    class PricingMode(models.TextChoices):
        OPT = "OPT", _("Опт")
        DROP = "DROP", _("Дроп")

    class OptTier(models.TextChoices):
        TIER_8_15 = "8_15", _("8–15")
        TIER_16_31 = "16_31", _("16–31")
        TIER_32_63 = "32_63", _("32–63")
        TIER_64_99 = "64_99", _("64–99")
        TIER_100_PLUS = "100_PLUS", _("100+")

    owner = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        verbose_name=_("Менеджер"),
        on_delete=models.CASCADE,
        related_name="commercial_offer_email_settings",
    )
    show_manager = models.BooleanField(default=True, verbose_name=_("Показувати менеджера"))
    manager_name = models.CharField(max_length=255, blank=True, verbose_name=_("Імʼя менеджера"))
    phone_enabled = models.BooleanField(default=False, verbose_name=_("Телефон увімкнено"))
    phone = models.CharField(max_length=50, blank=True, verbose_name=_("Телефон"))

    viber_enabled = models.BooleanField(default=False, verbose_name=_("Viber увімкнено"))
    viber = models.CharField(max_length=100, blank=True, verbose_name=_("Viber (номер)"))

    whatsapp_enabled = models.BooleanField(default=False, verbose_name=_("WhatsApp увімкнено"))
    whatsapp = models.CharField(max_length=100, blank=True, verbose_name=_("WhatsApp (номер)"))

    telegram_enabled = models.BooleanField(default=False, verbose_name=_("Telegram увімкнено"))
    telegram = models.CharField(max_length=100, blank=True, verbose_name=_("Telegram (@username або номер)"))

    general_tg = models.CharField(max_length=255, blank=True, verbose_name=_("Резервний Telegram (канал/чат)"))

    pricing_mode = models.CharField(
        max_length=10,
        choices=PricingMode.choices,
        default=PricingMode.OPT,
        verbose_name=_("База входу (опт/дроп)"),
    )
    opt_tier = models.CharField(
        max_length=10,
        choices=OptTier.choices,
        default=OptTier.TIER_8_15,
        verbose_name=_("Опт: обсяг (tier)"),
    )
    drop_tee_price = models.PositiveIntegerField(null=True, blank=True, verbose_name=_("Дроп ціна футболка (грн)"))
    drop_hoodie_price = models.PositiveIntegerField(null=True, blank=True, verbose_name=_("Дроп ціна худі (грн)"))
    dropship_loyalty_bonus = models.BooleanField(default=False, verbose_name=_("Дроп бонус (-10 грн)"))

    include_catalog_link = models.BooleanField(default=True, verbose_name=_("Додавати лінк на каталог"))
    include_wholesale_link = models.BooleanField(default=True, verbose_name=_("Додавати лінк на опт"))
    include_dropship_link = models.BooleanField(default=True, verbose_name=_("Додавати лінк на дроп"))
    include_instagram_link = models.BooleanField(default=True, verbose_name=_("Додавати Instagram"))
    include_site_link = models.BooleanField(default=True, verbose_name=_("Додавати сайт"))

    cta_type = models.CharField(
        max_length=32,
        blank=True,
        choices=CpCtaType.choices,
        verbose_name=_("CTA тип"),
    )
    cta_custom_url = models.CharField(max_length=500, blank=True, verbose_name=_("CTA custom URL"))
    cta_button_text = models.CharField(max_length=120, blank=True, verbose_name=_("Текст CTA кнопки"))
    cta_microtext = models.CharField(max_length=255, blank=True, verbose_name=_("Мікротекст під CTA"))

    gallery_initialized = models.BooleanField(default=False, verbose_name=_("Галерея налаштована"))
    gallery_neutral = models.JSONField(default=list, blank=True, verbose_name=_("Галерея (Neutral)"))
    gallery_edgy = models.JSONField(default=list, blank=True, verbose_name=_("Галерея (Edgy)"))

    mode = models.CharField(
        max_length=10,
        choices=CpMode.choices,
        default=CpMode.VISUAL,
        verbose_name=_("Режим шаблону"),
    )
    segment_mode = models.CharField(
        max_length=10,
        choices=CpSegmentMode.choices,
        default=CpSegmentMode.NEUTRAL,
        verbose_name=_("Сегмент контенту"),
    )
    subject_preset = models.CharField(
        max_length=20,
        choices=CpSubjectPreset.choices,
        default=CpSubjectPreset.PRESET_1,
        verbose_name=_("Пресет теми"),
    )
    subject_custom = models.CharField(max_length=255, blank=True, verbose_name=_("Кастомна тема"))

    tee_entry = models.PositiveIntegerField(null=True, blank=True, verbose_name=_("Вхід футболка (грн)"))
    tee_retail_example = models.PositiveIntegerField(null=True, blank=True, verbose_name=_("Роздріб футболка (приклад, грн)"))
    hoodie_entry = models.PositiveIntegerField(null=True, blank=True, verbose_name=_("Вхід худі (грн)"))
    hoodie_retail_example = models.PositiveIntegerField(null=True, blank=True, verbose_name=_("Роздріб худі (приклад, грн)"))

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Налаштування КП на e-mail")
        verbose_name_plural = _("Налаштування КП на e-mail")

    def __str__(self):
        return f"КП на e-mail: {self.owner}"


class CommercialOfferEmailLog(models.Model):
    class Status(models.TextChoices):
        SENT = "sent", _("Надіслано")
        FAILED = "failed", _("Помилка")

    class CpMode(models.TextChoices):
        LIGHT = "LIGHT", _("Light")
        VISUAL = "VISUAL", _("Visual")

    class CpSegmentMode(models.TextChoices):
        NEUTRAL = "NEUTRAL", _("Neutral")
        EDGY = "EDGY", _("Edgy")

    class CpSubjectPreset(models.TextChoices):
        PRESET_1 = "PRESET_1", _("Preset 1")
        PRESET_2 = "PRESET_2", _("Preset 2")
        PRESET_3 = "PRESET_3", _("Preset 3")
        CUSTOM = "CUSTOM", _("Custom")

    class CpCtaType(models.TextChoices):
        TELEGRAM_MANAGER = "TELEGRAM_MANAGER", _("Telegram менеджера")
        WHATSAPP_MANAGER = "WHATSAPP_MANAGER", _("WhatsApp менеджера")
        TELEGRAM_GENERAL = "TELEGRAM_GENERAL", _("Telegram загальний")
        MAILTO_COOPERATION = "MAILTO_COOPERATION", _("Email cooperation@")
        REPLY_HINT_ONLY = "REPLY_HINT_ONLY", _("Відповідь на лист (без лінка)")
        CUSTOM_URL = "CUSTOM_URL", _("Custom URL")

    class PricingMode(models.TextChoices):
        OPT = "OPT", _("Опт")
        DROP = "DROP", _("Дроп")

    class OptTier(models.TextChoices):
        TIER_8_15 = "8_15", _("8–15")
        TIER_16_31 = "16_31", _("16–31")
        TIER_32_63 = "32_63", _("32–63")
        TIER_64_99 = "64_99", _("64–99")
        TIER_100_PLUS = "100_PLUS", _("100+")

    class ResponseOutcome(models.TextChoices):
        NONE = "", _("Без реакції")
        NOT_OPENED = "not_opened", _("Не відкрив")
        OPENED_NO_REPLY = "opened_no_reply", _("Відкрив, не відповів")
        ASKED_MESSENGER = "asked_messenger", _("Просив у месенджер")
        THINKING = "thinking", _("Думає")
        REJECTED = "rejected", _("Відмова")
        ORDERED = "ordered", _("Замовлення")
        OTHER = "other", _("Інше")

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("Менеджер"),
        on_delete=models.CASCADE,
        related_name="commercial_offer_email_logs",
    )
    recipient_email = models.EmailField(verbose_name=_("Email отримувача"), db_index=True)
    recipient_name = models.CharField(max_length=255, blank=True, verbose_name=_("Імʼя/компанія отримувача"))
    subject = models.CharField(max_length=255, verbose_name=_("Тема листа"))
    preheader = models.CharField(max_length=255, blank=True, verbose_name=_("Preheader"))
    body_html = models.TextField(blank=True, verbose_name=_("HTML листа"))
    body_text = models.TextField(blank=True, verbose_name=_("Текст листа"))

    mode = models.CharField(
        max_length=10,
        choices=CpMode.choices,
        default=CpMode.VISUAL,
        verbose_name=_("Режим шаблону"),
        db_index=True,
    )
    segment_mode = models.CharField(
        max_length=10,
        choices=CpSegmentMode.choices,
        default=CpSegmentMode.NEUTRAL,
        verbose_name=_("Сегмент контенту"),
        db_index=True,
    )
    subject_preset = models.CharField(
        max_length=20,
        choices=CpSubjectPreset.choices,
        default=CpSubjectPreset.PRESET_1,
        verbose_name=_("Пресет теми"),
    )
    subject_custom = models.CharField(max_length=255, blank=True, verbose_name=_("Кастомна тема"))

    cta_type = models.CharField(
        max_length=32,
        blank=True,
        choices=CpCtaType.choices,
        verbose_name=_("CTA тип"),
    )
    cta_url = models.CharField(max_length=500, blank=True, verbose_name=_("CTA URL"))
    cta_custom_url = models.CharField(max_length=500, blank=True, verbose_name=_("CTA custom URL"))
    cta_button_text = models.CharField(max_length=120, blank=True, verbose_name=_("Текст CTA кнопки"))
    cta_microtext = models.CharField(max_length=255, blank=True, verbose_name=_("Мікротекст під CTA"))

    general_tg = models.CharField(max_length=255, blank=True, verbose_name=_("Резервний Telegram"))
    pricing_mode = models.CharField(
        max_length=10,
        choices=PricingMode.choices,
        default=PricingMode.OPT,
        verbose_name=_("База входу (опт/дроп)"),
    )
    opt_tier = models.CharField(
        max_length=10,
        choices=OptTier.choices,
        default=OptTier.TIER_8_15,
        verbose_name=_("Опт: обсяг (tier)"),
    )
    drop_tee_price = models.PositiveIntegerField(null=True, blank=True, verbose_name=_("Дроп ціна футболка (грн)"))
    drop_hoodie_price = models.PositiveIntegerField(null=True, blank=True, verbose_name=_("Дроп ціна худі (грн)"))
    dropship_loyalty_bonus = models.BooleanField(default=False, verbose_name=_("Дроп бонус (-10 грн)"))
    include_catalog_link = models.BooleanField(default=True, verbose_name=_("Каталог (лінк)"))
    include_wholesale_link = models.BooleanField(default=True, verbose_name=_("Опт (лінк)"))
    include_dropship_link = models.BooleanField(default=True, verbose_name=_("Дроп (лінк)"))
    include_instagram_link = models.BooleanField(default=True, verbose_name=_("Instagram (лінк)"))
    include_site_link = models.BooleanField(default=True, verbose_name=_("Сайт (лінк)"))

    gallery_urls = models.JSONField(default=list, blank=True, verbose_name=_("Галерея URL (ввід)"))
    gallery_items = models.JSONField(default=list, blank=True, verbose_name=_("Галерея (використано)"))

    tee_entry = models.PositiveIntegerField(null=True, blank=True, verbose_name=_("Вхід футболка (грн)"))
    tee_retail_example = models.PositiveIntegerField(null=True, blank=True, verbose_name=_("Роздріб футболка (приклад, грн)"))
    tee_profit = models.IntegerField(null=True, blank=True, verbose_name=_("Прибуток футболка (приклад, грн)"))
    hoodie_entry = models.PositiveIntegerField(null=True, blank=True, verbose_name=_("Вхід худі (грн)"))
    hoodie_retail_example = models.PositiveIntegerField(null=True, blank=True, verbose_name=_("Роздріб худі (приклад, грн)"))
    hoodie_profit = models.IntegerField(null=True, blank=True, verbose_name=_("Прибуток худі (приклад, грн)"))

    show_manager = models.BooleanField(default=True, verbose_name=_("Показувати менеджера"))
    manager_name = models.CharField(max_length=255, blank=True, verbose_name=_("Імʼя менеджера"))
    phone_enabled = models.BooleanField(default=False, verbose_name=_("Телефон увімкнено"))
    phone = models.CharField(max_length=50, blank=True, verbose_name=_("Телефон"))
    viber = models.CharField(max_length=100, blank=True, verbose_name=_("Viber"))
    whatsapp = models.CharField(max_length=100, blank=True, verbose_name=_("WhatsApp"))
    telegram = models.CharField(max_length=100, blank=True, verbose_name=_("Telegram"))

    status = models.CharField(max_length=20, choices=Status.choices, default=Status.SENT, db_index=True)
    error = models.TextField(blank=True, verbose_name=_("Помилка"))
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    # --- Tracking & client response (revamp 2026-06) ---
    track_token = models.UUIDField(default=uuid.uuid4, editable=False, unique=True, db_index=True, verbose_name=_("Токен трекінгу"))
    opened_at = models.DateTimeField(null=True, blank=True, db_index=True, verbose_name=_("Перше відкриття"))
    first_click_at = models.DateTimeField(null=True, blank=True, verbose_name=_("Перший клік"))
    click_count = models.PositiveIntegerField(default=0, verbose_name=_("Кліків"))
    response_outcome = models.CharField(
        max_length=24,
        blank=True,
        default="",
        choices=ResponseOutcome.choices,
        db_index=True,
        verbose_name=_("Реакція на КП"),
    )
    response_note = models.TextField(blank=True, verbose_name=_("Коментар реакції"))

    class Meta:
        verbose_name = _("Відправлена КП (лог)")
        verbose_name_plural = _("Відправлені КП (лог)")
        ordering = ['-created_at']

    def __str__(self):
        return f"КП → {self.recipient_email} ({self.created_at:%Y-%m-%d %H:%M})"


class Shop(models.Model):
    class ShopType(models.TextChoices):
        FULL = "full", _("Опт / повний магазин")
        TEST = "test", _("Тестова партія")

    name = models.CharField(_("Назва магазину"), max_length=255)
    photo = models.ImageField(_("Фото магазину"), upload_to="shops/photos/", blank=True, null=True)

    owner_full_name = models.CharField(_("ПІБ власника"), max_length=255, blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("Додав (менеджер)"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="management_shops_created",
    )
    managed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("Веде (менеджер)"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="management_shops_managed",
    )

    shop_type = models.CharField(_("Тип магазину"), max_length=10, choices=ShopType.choices, default=ShopType.FULL)

    registration_place = models.CharField(_("Місце реєстрації"), max_length=255, blank=True)
    is_physical = models.BooleanField(_("Фізичний магазин"), default=False)
    city = models.CharField(_("Місто"), max_length=120, blank=True)
    address = models.TextField(_("Адреса"), blank=True)

    # URLs can be stored without scheme (http/https); we'll normalize for clickable links in UI.
    website_url = models.CharField(_("Сайт"), max_length=500, blank=True)
    instagram_url = models.CharField(_("Instagram / Instashop"), max_length=500, blank=True)
    prom_url = models.CharField(_("Prom.ua"), max_length=500, blank=True)
    other_sales_channel = models.CharField(_("Інше (де ще продають)"), max_length=500, blank=True)

    # Test shop fields
    test_product = models.ForeignKey(
        "storefront.Product",
        verbose_name=_("Тестова партія: товар"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="management_test_shops",
    )
    test_package = models.JSONField(_("Тестова партія: комплектація"), default=dict, blank=True)
    test_contract_file = models.FileField(_("Договір (тест)"), upload_to="shops/contracts/", blank=True, null=True)
    test_connected_at = models.DateField(_("Дата підключення (старт 14 днів)"), null=True, blank=True)
    test_period_days = models.PositiveIntegerField(_("Тривалість тесту (днів)"), default=14)

    next_contact_at = models.DateTimeField(_("Наступний контакт"), null=True, blank=True)
    notes = models.TextField(_("Нотатки"), blank=True)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Магазин")
        verbose_name_plural = _("Магазини")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["shop_type", "-created_at"], name="mgmt_shop_type_dt"),
            models.Index(fields=["managed_by", "-created_at"], name="mgmt_shop_mgr_dt"),
        ]

    def __str__(self):
        return self.name


class ShopPhone(models.Model):
    class Role(models.TextChoices):
        OWNER = "owner", _("Власник")
        MANAGER = "manager", _("Менеджер")
        ADMIN = "admin", _("Адміністратор")
        OTHER = "other", _("Інший")

    shop = models.ForeignKey(Shop, on_delete=models.CASCADE, related_name="phones", verbose_name=_("Магазин"))
    role = models.CharField(_("Роль"), max_length=20, choices=Role.choices, default=Role.OWNER)
    role_other = models.CharField(_("Хто це (якщо інший)"), max_length=120, blank=True)
    phone = models.CharField(_("Телефон"), max_length=50)
    is_primary = models.BooleanField(_("Основний"), default=False)
    sort_order = models.PositiveIntegerField(_("Порядок"), default=0)

    class Meta:
        verbose_name = _("Контакт магазину")
        verbose_name_plural = _("Контакти магазину")
        ordering = ["sort_order", "id"]
        indexes = [
            models.Index(fields=["shop", "is_primary"], name="mgmt_shop_phone_primary"),
        ]

    def __str__(self):
        return f"{self.shop_id}: {self.phone}"


class ShopShipment(models.Model):
    shop = models.ForeignKey(Shop, on_delete=models.CASCADE, related_name="shipments", verbose_name=_("Магазин"))
    ttn_number = models.CharField(_("ТТН"), max_length=64, db_index=True)
    shipped_at = models.DateField(_("Дата відправки"), db_index=True)

    wholesale_invoice = models.ForeignKey(
        "orders.WholesaleInvoice",
        verbose_name=_("Накладна (система)"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="management_shop_shipments",
    )
    uploaded_invoice_file = models.FileField(
        _("Накладна (Excel файл)"),
        upload_to="shops/invoices/",
        blank=True,
        null=True,
    )
    invoice_summary = models.JSONField(_("Сводка накладной"), default=dict, blank=True)
    invoice_total_amount = models.DecimalField(_("Сума накладної"), max_digits=12, decimal_places=2, null=True, blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("Створив"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="management_shop_shipments_created",
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Відправка (ТТН)")
        verbose_name_plural = _("Відправки (ТТН)")
        ordering = ["-shipped_at", "-created_at"]
        indexes = [
            models.Index(fields=["shop", "-shipped_at"], name="mgmt_ship_shop_dt"),
        ]

    def __str__(self):
        return f"{self.shop_id}: {self.ttn_number}"


class ShopCommunication(models.Model):
    shop = models.ForeignKey(Shop, on_delete=models.CASCADE, related_name="communications", verbose_name=_("Магазин"))
    contacted_at = models.DateTimeField(_("Коли звʼязувались"), auto_now_add=False)
    contact_person = models.CharField(_("З ким"), max_length=255, blank=True)
    phone = models.CharField(_("Номер"), max_length=50, blank=True)
    note = models.TextField(_("Нотатка"), blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("Створив"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="management_shop_comms_created",
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = _("Комунікація (магазин)")
        verbose_name_plural = _("Комунікації (магазин)")
        ordering = ["-contacted_at", "-created_at"]
        indexes = [
            models.Index(fields=["shop", "-contacted_at"], name="mgmt_shop_comm_dt"),
        ]

    def __str__(self):
        return f"{self.shop_id}: {self.contacted_at:%Y-%m-%d %H:%M}"


class ShopInventoryMovement(models.Model):
    class Kind(models.TextChoices):
        RECEIPT = "receipt", _("Надходження")
        SALE = "sale", _("Продаж")
        ADJUST = "adjust", _("Коригування")

    shop = models.ForeignKey(Shop, on_delete=models.CASCADE, related_name="inventory_moves", verbose_name=_("Магазин"))
    shipment = models.ForeignKey(
        ShopShipment,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="inventory_moves",
        verbose_name=_("ТТН"),
    )
    kind = models.CharField(_("Тип"), max_length=20, choices=Kind.choices, db_index=True)

    product_name = models.CharField(_("Товар"), max_length=255)
    category = models.CharField(_("Категорія"), max_length=120, blank=True)
    size = models.CharField(_("Розмір"), max_length=40, blank=True)
    color = models.CharField(_("Колір"), max_length=80, blank=True)

    delta_qty = models.IntegerField(_("Зміна кількості (±)"))
    note = models.CharField(_("Коментар"), max_length=255, blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("Створив"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="management_shop_inventory_moves_created",
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = _("Рух товару (магазин)")
        verbose_name_plural = _("Рух товару (магазин)")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["shop", "kind", "-created_at"], name="mgmt_inv_shop_kind"),
        ]

    def __str__(self):
        return f"{self.shop_id}: {self.kind} {self.delta_qty}"


class ManagementDailyActivity(models.Model):
    """
    Aggregated manager activity per local day (Europe/Kiev).
    Active time counts only when the Management tab is visible + focused and user is not idle.
    Idle time counts when tab is open but not focused/active.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="management_daily_activity",
        verbose_name=_("Користувач"),
    )
    date = models.DateField(db_index=True, verbose_name=_("Дата (локальна)"))
    active_seconds = models.PositiveIntegerField(default=0, verbose_name=_("Активний час (сек)"))
    idle_seconds = models.PositiveIntegerField(default=0, verbose_name=_("Відкритий але неактивний час (сек)"))
    last_seen_at = models.DateTimeField(null=True, blank=True, verbose_name=_("Остання активність"))

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Активність менеджера (доба)")
        verbose_name_plural = _("Активність менеджера (доба)")
        unique_together = ("user", "date")
        ordering = ["-date"]
        indexes = [
            models.Index(fields=["user", "-date"], name="mgmt_act_user_dt"),
        ]

    def __str__(self):
        return f"{self.user_id} {self.date} ({self.active_seconds}s)"


class ClientFollowUp(models.Model):
    class Kind(models.TextChoices):
        CALLBACK = "callback", _("Передзвон")
        PROMISE = "promise", _("Обіцянка / очікування")
        NURTURE = "nurture", _("Догрів / продовження контакту")
        OTHER = "other", _("Інше")

    class Status(models.TextChoices):
        OPEN = "open", _("Відкрито")
        DONE = "done", _("Виконано")
        RESCHEDULED = "rescheduled", _("Перенесено")
        CANCELLED = "cancelled", _("Скасовано")
        MISSED = "missed", _("Пропущено")

    client = models.ForeignKey(
        Client,
        on_delete=models.CASCADE,
        related_name="followups",
        verbose_name=_("Клієнт"),
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="management_client_followups",
        verbose_name=_("Менеджер"),
    )

    scheduled_at = models.DateTimeField(auto_now_add=True, db_index=True, verbose_name=_("Створено"))
    due_at = models.DateTimeField(db_index=True, verbose_name=_("Коли передзвонити"))
    due_date = models.DateField(db_index=True, verbose_name=_("Дата (локальна)"))

    status = models.CharField(max_length=20, choices=Status.choices, default=Status.OPEN, db_index=True)
    followup_kind = models.CharField(max_length=24, choices=Kind.choices, default=Kind.CALLBACK, db_index=True)
    close_reason = models.CharField(max_length=64, blank=True, db_index=True, verbose_name=_("Причина закриття"))
    completion_quality = models.CharField(max_length=32, blank=True, db_index=True, verbose_name=_("Якість виконання"))
    closed_at = models.DateTimeField(null=True, blank=True, verbose_name=_("Закрито"))
    grace_until = models.DateTimeField(null=True, blank=True, db_index=True, verbose_name=_("Пільгове вікно до"))
    last_notified_at = models.DateTimeField(null=True, blank=True, db_index=True, verbose_name=_("Останнє нагадування"))
    escalation_level = models.PositiveSmallIntegerField(default=0, db_index=True, verbose_name=_("Рівень ескалації"))
    reschedule_count = models.PositiveSmallIntegerField(default=0, verbose_name=_("Кількість переносів"))
    priority_snapshot = models.JSONField(default=dict, blank=True, verbose_name=_("Знімок пріоритету"))
    source_interaction = models.ForeignKey(
        "management.ClientInteractionAttempt",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="spawned_followups",
        verbose_name=_("Початкова взаємодія"),
    )
    closed_by_report = models.ForeignKey(
        Report,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="closed_followups",
        verbose_name=_("Закрито звітом"),
    )
    meta = models.JSONField(default=dict, blank=True, verbose_name=_("Мета"))

    class Meta:
        verbose_name = _("Передзвон (клієнт)")
        verbose_name_plural = _("Передзвони (клієнти)")
        ordering = ["-due_at", "-id"]
        indexes = [
            models.Index(fields=["owner", "due_date", "status"], name="mgmt_fu_owner_dt_st"),
            models.Index(fields=["client", "status"], name="mgmt_fu_client_st"),
        ]

    def __str__(self):
        return f"FollowUp({self.owner_id} → {self.client_id}) {self.due_at} {self.status}"


class ManagementStatsAdviceDismissal(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="management_stats_advice_dismissals",
        verbose_name=_("Користувач"),
    )
    key = models.CharField(max_length=255, db_index=True, verbose_name=_("Ключ"))
    dismissed_at = models.DateTimeField(auto_now_add=True, verbose_name=_("Коли приховано"))
    expires_at = models.DateTimeField(null=True, blank=True, db_index=True, verbose_name=_("Діє до"))
    meta = models.JSONField(default=dict, blank=True, verbose_name=_("Мета"))

    class Meta:
        verbose_name = _("Прихована порада (статистика)")
        verbose_name_plural = _("Приховані поради (статистика)")
        unique_together = ("user", "key")
        indexes = [
            models.Index(fields=["user", "expires_at"], name="mgmt_adv_user_exp"),
        ]

    def __str__(self):
        return f"{self.user_id}:{self.key}"


class ManagementStatsConfig(models.Model):
    """
    Singleton-like config holder for KPI/Advice tuning (admin-editable via Django admin).
    """

    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    kpd_weights = models.JSONField(default=dict, blank=True, verbose_name=_("Ваги КПД"))
    advice_thresholds = models.JSONField(default=dict, blank=True, verbose_name=_("Пороги порад"))
    formula_version = models.CharField(max_length=64, default="mosaic-v1", verbose_name=_("Версія формули"))
    legacy_kpd_formula_version = models.CharField(max_length=64, default="kpd-v1", verbose_name=_("Версія legacy КПД"))
    shadow_mosaic_formula_version = models.CharField(max_length=64, default="mosaic-v1", verbose_name=_("Версія shadow MOSAIC"))
    defaults_version = models.CharField(max_length=64, default="2026-03-13", verbose_name=_("Версія дефолтів"))
    snapshot_schema_version = models.CharField(max_length=32, default="v1", verbose_name=_("Версія snapshot-схеми"))
    payload_version = models.CharField(max_length=32, default="v1", verbose_name=_("Версія payload"))
    rollout_state = models.CharField(max_length=32, default="shadow", verbose_name=_("Стан rollout"))
    feature_flags = models.JSONField(default=dict, blank=True, verbose_name=_("Feature flags"))
    formula_defaults = models.JSONField(default=dict, blank=True, verbose_name=_("Дефолти формул"))
    mosaic_config = models.JSONField(default=dict, blank=True, verbose_name=_("Конфіг MOSAIC"))
    payroll_config = models.JSONField(default=dict, blank=True, verbose_name=_("Конфіг payroll"))
    forecast_config = models.JSONField(default=dict, blank=True, verbose_name=_("Конфіг forecast"))
    telephony_config = models.JSONField(default=dict, blank=True, verbose_name=_("Конфіг телфонії"))
    ui_config = models.JSONField(default=dict, blank=True, verbose_name=_("UI конфіг"))
    validation_state = models.JSONField(default=dict, blank=True, verbose_name=_("Стан валідації"))
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Налаштування статистики")
        verbose_name_plural = _("Налаштування статистики")

    def __str__(self):
        return "ManagementStatsConfig"


class ComponentReadiness(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", _("Активно")
        SHADOW = "shadow", _("Тіньовий режим")
        DORMANT = "dormant", _("Неактивно")

    component = models.CharField(max_length=64, unique=True, verbose_name=_("Компонент"))
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.SHADOW, db_index=True)
    notes = models.TextField(blank=True, verbose_name=_("Нотатки"))
    meta = models.JSONField(default=dict, blank=True, verbose_name=_("Мета"))
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Готовність компонента")
        verbose_name_plural = _("Готовність компонентів")
        ordering = ["component"]

    def __str__(self):
        return f"{self.component}: {self.status}"


class DuplicateReview(models.Model):
    class Status(models.TextChoices):
        OPEN = "open", _("Відкрито")
        RESOLVED = "resolved", _("Закрито")

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="management_duplicate_reviews",
        verbose_name=_("Ініціатор"),
    )
    zone = models.CharField(max_length=20, db_index=True, verbose_name=_("Зона"))
    incoming_shop_name = models.CharField(max_length=255, verbose_name=_("Вхідна назва"))
    incoming_phone = models.CharField(max_length=50, verbose_name=_("Вхідний номер"))
    incoming_payload = models.JSONField(default=dict, blank=True, verbose_name=_("Вхідні дані"))
    candidate_summary = models.JSONField(default=list, blank=True, verbose_name=_("Кандидати"))
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.OPEN, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    resolved_at = models.DateTimeField(null=True, blank=True, db_index=True)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="management_duplicate_reviews_resolved",
        verbose_name=_("Закрив"),
    )
    resolution_note = models.TextField(blank=True, verbose_name=_("Нотатка по рішенню"))

    class Meta:
        verbose_name = _("Перевірка дубля")
        verbose_name_plural = _("Перевірки дублів")
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.incoming_shop_name} [{self.zone}]"


class ClientInteractionAttempt(models.Model):
    class VerificationLevel(models.TextChoices):
        SELF_REPORTED = "self_reported", _("Самозвіт")
        LINKED_EVIDENCE = "linked_evidence", _("Підтверджено доказом")
        OVERRIDE = "override", _("З override перевіркою")

    client = models.ForeignKey(
        Client,
        on_delete=models.CASCADE,
        related_name="interaction_attempts",
        verbose_name=_("Клієнт"),
    )
    manager = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="management_client_interactions",
        verbose_name=_("Менеджер"),
    )
    result = models.CharField(_("Підсумок"), max_length=50, choices=Client.CallResult.choices, db_index=True)
    reason_code = models.CharField(_("Код причини"), max_length=64, blank=True, db_index=True)
    reason_note = models.TextField(_("Коментар причини"), blank=True)
    context = models.JSONField(_("Контекст"), default=dict, blank=True)
    details = models.TextField(_("Деталі"), blank=True)
    next_call_at = models.DateTimeField(_("Наступний дзвінок"), null=True, blank=True)
    verification_level = models.CharField(
        _("Рівень верифікації"),
        max_length=32,
        choices=VerificationLevel.choices,
        default=VerificationLevel.SELF_REPORTED,
        db_index=True,
    )
    linked_shop = models.ForeignKey(
        "management.Shop",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="client_interaction_attempts",
        verbose_name=_("Пов'язаний магазин"),
    )
    cp_log = models.ForeignKey(
        "management.CommercialOfferEmailLog",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="client_interaction_attempts",
        verbose_name=_("Лог КП"),
    )
    duplicate_review = models.ForeignKey(
        "management.DuplicateReview",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="interaction_attempts",
        verbose_name=_("Перевірка дубля"),
    )
    duplicate_override_reason = models.TextField(_("Причина override"), blank=True)
    messenger_type = models.CharField(_("Месенджер"), max_length=32, blank=True)
    messenger_target_mode = models.CharField(_("Режим цілі месенджера"), max_length=32, blank=True)
    messenger_target_value = models.CharField(_("Ціль месенджера"), max_length=255, blank=True)
    xml_platform = models.CharField(_("XML платформа"), max_length=32, blank=True)
    xml_resource_url = models.CharField(_("XML ресурс"), max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = _("Спроба взаємодії з клієнтом")
        verbose_name_plural = _("Спроби взаємодії з клієнтами")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["manager", "-created_at"], name="mgmt_client_attempt_mgr_dt"),
            models.Index(fields=["client", "-created_at"], name="mgmt_client_attempt_cli_dt"),
        ]

    def __str__(self):
        return f"{self.client_id}: {self.result}"


class ClientCPLink(models.Model):
    client = models.ForeignKey(
        Client,
        on_delete=models.CASCADE,
        related_name="cp_links",
        verbose_name=_("Клієнт"),
    )
    cp_log = models.ForeignKey(
        "management.CommercialOfferEmailLog",
        on_delete=models.CASCADE,
        related_name="client_links",
        verbose_name=_("Лог КП"),
    )
    linked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="management_client_cp_links",
        verbose_name=_("Прив'язав"),
    )
    interaction = models.ForeignKey(
        "management.ClientInteractionAttempt",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="cp_links",
        verbose_name=_("Спроба взаємодії"),
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = _("Зв'язок клієнта з КП")
        verbose_name_plural = _("Зв'язки клієнтів з КП")
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["client", "cp_log"], name="mgmt_client_cp_unique"),
        ]

    def __str__(self):
        return f"{self.client_id} -> {self.cp_log_id}"


class CommandRunLog(models.Model):
    class Status(models.TextChoices):
        RUNNING = "running", _("Виконується")
        SUCCESS = "success", _("Успішно")
        FAILED = "failed", _("Помилка")
        PARTIAL = "partial", _("Частково")

    command_name = models.CharField(max_length=128, db_index=True, verbose_name=_("Команда"))
    run_key = models.CharField(max_length=128, unique=True, verbose_name=_("Ключ запуску"))
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.RUNNING, db_index=True)
    rows_processed = models.PositiveIntegerField(default=0, verbose_name=_("Оброблено рядків"))
    warnings_count = models.PositiveIntegerField(default=0, verbose_name=_("Попереджень"))
    error_excerpt = models.TextField(blank=True, verbose_name=_("Короткий текст помилки"))
    meta = models.JSONField(default=dict, blank=True, verbose_name=_("Мета"))
    started_at = models.DateTimeField(auto_now_add=True, db_index=True)
    finished_at = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:
        verbose_name = _("Лог запуску команди")
        verbose_name_plural = _("Логи запуску команд")
        ordering = ["-started_at"]
        indexes = [
            models.Index(fields=["command_name", "-started_at"], name="mgmt_cmd_name_dt"),
            models.Index(fields=["status", "-started_at"], name="mgmt_cmd_status_dt"),
        ]

    def mark_finished(self, *, status: str, rows_processed: int = 0, warnings_count: int = 0, error_excerpt: str = ""):
        self.status = status
        self.rows_processed = max(0, int(rows_processed or 0))
        self.warnings_count = max(0, int(warnings_count or 0))
        self.error_excerpt = (error_excerpt or "").strip()
        self.finished_at = timezone.now()
        self.save(update_fields=["status", "rows_processed", "warnings_count", "error_excerpt", "finished_at"])

    def __str__(self):
        return f"{self.command_name} [{self.status}]"


class ManagerDayStatus(models.Model):
    class Status(models.TextChoices):
        WORKING = "working", _("Робочий день")
        WEEKEND = "weekend", _("Вихідний")
        HOLIDAY = "holiday", _("Свято")
        VACATION = "vacation", _("Відпустка")
        SICK = "sick", _("Лікарняний")
        EXCUSED = "excused", _("Погоджена відсутність")
        TECH_FAILURE = "tech_failure", _("Технічний збій")
        FORCE_MAJEURE = "force_majeure", _("Форс-мажор")

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="management_day_statuses",
        verbose_name=_("Менеджер"),
    )
    day = models.DateField(db_index=True, verbose_name=_("День"))
    status = models.CharField(max_length=32, choices=Status.choices, default=Status.WORKING, db_index=True)
    capacity_factor = models.DecimalField(max_digits=4, decimal_places=2, default=Decimal("1.00"), verbose_name=_("Фактор доступності"))
    source_reason = models.CharField(max_length=255, blank=True, verbose_name=_("Причина / джерело"))
    reintegration_flag = models.BooleanField(default=False, verbose_name=_("Період повернення"))
    meta = models.JSONField(default=dict, blank=True, verbose_name=_("Мета"))
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Статус дня менеджера")
        verbose_name_plural = _("Статуси днів менеджера")
        ordering = ["-day"]
        constraints = [
            models.UniqueConstraint(fields=["owner", "day"], name="mgmt_day_owner_unique"),
        ]
        indexes = [
            models.Index(fields=["owner", "-day"], name="mgmt_day_owner_dt"),
            models.Index(fields=["status", "-day"], name="mgmt_day_status_dt"),
        ]

    def save(self, *args, **kwargs):
        value = Decimal(self.capacity_factor or 0)
        value = max(Decimal("0.00"), min(Decimal("1.00"), value))
        self.capacity_factor = value
        super().save(*args, **kwargs)
        from management.services.analytics_v7 import sync_manager_day_status_fact

        sync_manager_day_status_fact(self)

    def __str__(self):
        return f"{self.owner_id}:{self.day} {self.status}"


class NightlyScoreSnapshot(models.Model):
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="management_score_snapshots",
        verbose_name=_("Менеджер"),
    )
    snapshot_date = models.DateField(db_index=True, verbose_name=_("Локальна дата"))
    formula_version = models.CharField(max_length=64, verbose_name=_("Версія формули"))
    defaults_version = models.CharField(max_length=64, verbose_name=_("Версія дефолтів"))
    snapshot_schema_version = models.CharField(max_length=32, verbose_name=_("Версія snapshot-схеми"))
    payload_version = models.CharField(max_length=32, verbose_name=_("Версія payload"))
    kpd_value = models.DecimalField(max_digits=8, decimal_places=2, default=Decimal("0.00"), verbose_name=_("KPD"))
    mosaic_score = models.DecimalField(max_digits=8, decimal_places=2, default=Decimal("0.00"), verbose_name=_("MOSAIC"))
    score_confidence = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0.00"), verbose_name=_("Рівень довіри"))
    working_day_factor = models.DecimalField(max_digits=4, decimal_places=2, default=Decimal("1.00"), verbose_name=_("Фактор робочого дня"))
    freshness_seconds = models.PositiveIntegerField(default=0, verbose_name=_("Freshness, сек"))
    payload = models.JSONField(default=dict, blank=True, verbose_name=_("Payload"))
    job_run = models.ForeignKey(
        CommandRunLog,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="snapshots",
        verbose_name=_("Лог запуску"),
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Нічний snapshot score")
        verbose_name_plural = _("Нічні snapshot-и score")
        ordering = ["-snapshot_date", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["owner", "snapshot_date"], name="mgmt_snapshot_owner_day_unique"),
        ]
        indexes = [
            models.Index(fields=["owner", "-snapshot_date"], name="mgmt_snapshot_owner_dt"),
        ]

    def __str__(self):
        return f"{self.owner_id}:{self.snapshot_date} {self.mosaic_score}"


class ScoreAppeal(models.Model):
    class AppealType(models.TextChoices):
        SCORE = "score", _("Score")
        FREEZE = "freeze", _("Freeze")
        OWNERSHIP = "ownership", _("Ownership")

    class Status(models.TextChoices):
        OPEN = "open", _("Відкрито")
        APPROVED = "approved", _("Підтверджено")
        REJECTED = "rejected", _("Відхилено")

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="management_score_appeals",
        verbose_name=_("Менеджер"),
    )
    snapshot = models.ForeignKey(
        NightlyScoreSnapshot,
        on_delete=models.CASCADE,
        related_name="appeals",
        verbose_name=_("Snapshot"),
    )
    appeal_type = models.CharField(max_length=20, choices=AppealType.choices, default=AppealType.SCORE, db_index=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.OPEN, db_index=True)
    reason_code = models.CharField(max_length=64, blank=True, verbose_name=_("Код причини"))
    reason = models.TextField(verbose_name=_("Причина"))
    manager_note = models.TextField(blank=True, verbose_name=_("Нотатка менеджера"))
    evidence = models.JSONField(default=dict, blank=True, verbose_name=_("Докази"))
    evidence_payload = models.JSONField(default=dict, blank=True, verbose_name=_("Payload доказів"))
    resolution_note = models.TextField(blank=True, verbose_name=_("Рішення"))
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="resolved_management_score_appeals",
        verbose_name=_("Розглянув"),
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    opened_at = models.DateTimeField(default=timezone.now, db_index=True)
    due_at = models.DateTimeField(null=True, blank=True, db_index=True)
    resolved_at = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:
        verbose_name = _("Оскарження score")
        verbose_name_plural = _("Оскарження score")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["owner", "status", "-created_at"], name="mgmt_appeal_owner_st"),
        ]

    def mark_resolved(self, status: str, resolution_note: str):
        self.status = status
        self.resolution_note = (resolution_note or "").strip()
        self.resolved_at = timezone.now()
        self.save(update_fields=["status", "resolution_note", "resolved_at"])

    def __str__(self):
        return f"{self.owner_id}:{self.status}"


class ManagerCommissionAccrual(models.Model):
    class AccrualType(models.TextChoices):
        NEW = "new", _("Новий")
        REPEAT = "repeat", _("Повторний")
        REACTIVATION = "reactivation", _("Реактивація")
        RESCUE_SPIFF = "rescue_spiff", _("Rescue SPIFF")
        MANUAL = "manual", _("Ручне")

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='management_commission_accruals',
        verbose_name=_('Менеджер'),
    )
    invoice = models.OneToOneField(
        'orders.WholesaleInvoice',
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='management_commission_accrual',
        verbose_name=_('Накладна'),
    )
    source_snapshot = models.ForeignKey(
        NightlyScoreSnapshot,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="commission_accruals",
        verbose_name=_("Source snapshot"),
    )

    accrual_type = models.CharField(max_length=20, choices=AccrualType.choices, default=AccrualType.MANUAL, db_index=True)
    base_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0, verbose_name=_('База (грн)'))
    percent = models.DecimalField(max_digits=6, decimal_places=2, default=0, verbose_name=_('Відсоток'))
    amount = models.DecimalField(max_digits=12, decimal_places=2, default=0, verbose_name=_('Нараховано (грн)'))
    note = models.CharField(max_length=255, blank=True, verbose_name=_('Пояснення'))
    freeze_reason_code = models.CharField(max_length=64, blank=True, verbose_name=_("Код причини заморозки"))
    freeze_reason_text = models.TextField(blank=True, verbose_name=_("Причина заморозки"))
    working_factor_applied = models.DecimalField(max_digits=5, decimal_places=4, default=1, verbose_name=_("Working factor"))
    evidence_payload = models.JSONField(default=dict, blank=True, verbose_name=_("Payload доказів"))

    frozen_until = models.DateTimeField(db_index=True, verbose_name=_('Заморожено до'))
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _('Нарахування менеджера')
        verbose_name_plural = _('Нарахування менеджера')
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['owner', '-created_at'], name='mgmt_comm_owner_dt'),
            models.Index(fields=['owner', 'frozen_until'], name='mgmt_comm_owner_frz'),
        ]

    def __str__(self):
        return f"{self.owner_id}: +{self.amount}"


class ManagerPayoutRequest(models.Model):
    class Status(models.TextChoices):
        PROCESSING = 'processing', _('В обробці')
        APPROVED = 'approved', _('Схвалено')
        REJECTED = 'rejected', _('Відхилено')
        PAID = 'paid', _('Виплачено')

    class Kind(models.TextChoices):
        REQUEST = 'request', _('Запит менеджера')
        MANUAL = 'manual', _('Ручний запис')

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='management_payout_requests',
        verbose_name=_('Менеджер'),
    )

    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.REQUEST, db_index=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2, verbose_name=_('Сума (грн)'))

    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PROCESSING, db_index=True)
    rejection_reason = models.TextField(blank=True, verbose_name=_('Причина (якщо відхилено)'))

    processed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='processed_management_payout_requests',
        verbose_name=_('Обробив (адмін)'),
    )

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    approved_at = models.DateTimeField(null=True, blank=True, db_index=True)
    rejected_at = models.DateTimeField(null=True, blank=True, db_index=True)
    paid_at = models.DateTimeField(null=True, blank=True, db_index=True)

    admin_tg_chat_id = models.BigIntegerField(null=True, blank=True)
    admin_tg_message_id = models.BigIntegerField(null=True, blank=True)

    class Meta:
        verbose_name = _('Запит на виплату')
        verbose_name_plural = _('Запити на виплату')
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['owner', 'status', '-created_at'], name='mgmt_pay_owner_st'),
            models.Index(fields=['status', '-created_at'], name='mgmt_pay_st_dt'),
        ]

    def __str__(self):
        return f"{self.owner_id}: {self.status} {self.amount}"


class PayoutRejectionReasonRequest(models.Model):
    payout_request = models.ForeignKey(
        ManagerPayoutRequest,
        on_delete=models.CASCADE,
        related_name='rejection_reason_requests',
    )
    admin_chat_id = models.BigIntegerField(db_index=True)
    prompt_message_id = models.BigIntegerField(null=True, blank=True)
    is_active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _('Причина відхилення виплати (запит)')
        verbose_name_plural = _('Причини відхилення виплати (запити)')
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['admin_chat_id', 'is_active'], name='mgmt_payrej_chat_act'),
        ]

    def __str__(self):
        return f"{self.payout_request_id} ({'active' if self.is_active else 'closed'})"


class ContractSequence(models.Model):
    year = models.PositiveIntegerField(unique=True)
    last_number = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Нумерація договорів (рік)")
        verbose_name_plural = _("Нумерація договорів (рік)")

    def __str__(self):
        return f"{self.year}: {self.last_number}"


class ManagementContract(models.Model):
    REVIEW_STATUS_CHOICES = [
        ('draft', 'Чернетка'),
        ('pending', 'На перевірці'),
        ('approved', 'Підтверджено'),
        ('rejected', 'Відхилено'),
    ]

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="management_contracts",
        verbose_name=_("Менеджер"),
    )
    contract_number = models.CharField(max_length=50, unique=True, verbose_name=_("Номер договору"))
    contract_date = models.DateField(verbose_name=_("Дата договору"))
    realizer_name = models.CharField(max_length=255, verbose_name=_("Реалізатор"))
    file_path = models.CharField(max_length=500, verbose_name=_("Шлях до файлу"))
    review_status = models.CharField(
        max_length=20,
        choices=REVIEW_STATUS_CHOICES,
        default='draft',
        verbose_name=_("Статус перевірки"),
    )
    review_reject_reason = models.TextField(blank=True, verbose_name=_("Причина відхилення"))
    reviewed_at = models.DateTimeField(null=True, blank=True, verbose_name=_("Дата перевірки"))
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='reviewed_management_contracts',
        verbose_name=_("Перевірив (адмін)"),
    )
    admin_tg_chat_id = models.BigIntegerField(null=True, blank=True, verbose_name=_("Telegram admin chat id"))
    admin_tg_message_id = models.BigIntegerField(null=True, blank=True, verbose_name=_("Telegram admin message id"))
    is_approved = models.BooleanField(default=False, verbose_name=_("Підтверджено"))
    payload = models.JSONField(default=dict, blank=True, verbose_name=_("Дані договору"))
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("Договір (менеджмент)")
        verbose_name_plural = _("Договори (менеджмент)")
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.contract_number} ({self.realizer_name})"


class ContractRejectionReasonRequest(models.Model):
    contract = models.ForeignKey(
        ManagementContract,
        on_delete=models.CASCADE,
        related_name='rejection_reason_requests',
        verbose_name=_("Договір"),
    )
    admin_chat_id = models.BigIntegerField(db_index=True, verbose_name=_("Telegram chat id (адмін)"))
    prompt_message_id = models.BigIntegerField(null=True, blank=True, verbose_name=_("Message id запиту"))
    is_active = models.BooleanField(default=True, db_index=True, verbose_name=_("Активний запит"))
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("Запит причини відхилення договору")
        verbose_name_plural = _("Запити причин відхилення договорів")
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['admin_chat_id', 'is_active'], name='mgmt_contractrej_chat_act'),
        ]

    def __str__(self):
        return f"{self.contract_id} ({'active' if self.is_active else 'closed'})"


class OwnershipChangeLog(models.Model):
    class EntityType(models.TextChoices):
        CLIENT = "client", _("Клієнт")
        LEAD = "lead", _("Лід")
        SHOP = "shop", _("Магазин")

    entity_type = models.CharField(max_length=20, choices=EntityType.choices, db_index=True, verbose_name=_("Тип сутності"))
    entity_id = models.PositiveIntegerField(db_index=True, verbose_name=_("ID сутності"))
    previous_owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="management_previous_ownership_changes",
        verbose_name=_("Попередній owner"),
    )
    new_owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="management_new_ownership_changes",
        verbose_name=_("Новий owner"),
    )
    reason = models.CharField(max_length=255, blank=True, verbose_name=_("Причина"))
    payload = models.JSONField(default=dict, blank=True, verbose_name=_("Payload"))
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = _("Лог зміни ownership")
        verbose_name_plural = _("Логи зміни ownership")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["entity_type", "entity_id", "-created_at"], name="mgmt_ownerchange_ent"),
        ]

    def __str__(self):
        return f"{self.entity_type}:{self.entity_id}"


class TelephonyWebhookLog(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", _("Очікує")
        PROCESSED = "processed", _("Оброблено")
        FAILED = "failed", _("Помилка")
        IGNORED = "ignored", _("Пропущено")

    provider = models.CharField(max_length=64, db_index=True, verbose_name=_("Провайдер"))
    external_event_id = models.CharField(max_length=255, verbose_name=_("Зовнішній event ID"))
    event_type = models.CharField(max_length=128, blank=True, verbose_name=_("Тип події"))
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING, db_index=True)
    payload = models.JSONField(default=dict, blank=True, verbose_name=_("Payload"))
    error_excerpt = models.TextField(blank=True, verbose_name=_("Помилка"))
    received_at = models.DateTimeField(auto_now_add=True, db_index=True)
    processed_at = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:
        verbose_name = _("Webhook IP-телефонії")
        verbose_name_plural = _("Webhook-и IP-телефонії")
        ordering = ["-received_at"]
        constraints = [
            models.UniqueConstraint(fields=["provider", "external_event_id"], name="mgmt_tel_event_unique"),
        ]
        indexes = [
            models.Index(fields=["provider", "status", "-received_at"], name="mgmt_tel_provider_st"),
        ]

    def __str__(self):
        return f"{self.provider}:{self.external_event_id}"


class ManagerLevel(models.Model):
    """Поточний рівень менеджера з фінансовими умовами"""

    class Level(models.TextChoices):
        CANDIDATE = 'candidate', _('Менеджер-кандидат')
        LEVEL_1 = 'level_1', _('Менеджер 1-го рівня')
        LEVEL_2 = 'level_2', _('Менеджер 2-го рівня')
        TOP_MANAGER = 'top_manager', _('Топ-менеджер')
        PROJECT_MANAGER = 'project_manager', _('Project-менеджер')
        ADMIN = 'admin', _('Адміністратор')

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='manager_level',
        verbose_name=_('Менеджер'),
    )
    level = models.CharField(
        max_length=20,
        choices=Level.choices,
        default=Level.CANDIDATE,
        db_index=True,
        verbose_name=_('Рівень'),
    )
    assigned_at = models.DateTimeField(auto_now_add=True, db_index=True, verbose_name=_('Дата призначення'))
    assigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='assigned_manager_levels',
        verbose_name=_('Призначив'),
    )

    # Фінансові умови
    weekly_salary_uah = models.PositiveIntegerField(
        default=0,
        verbose_name=_('Тижнева ставка (грн)'),
        help_text=_('Базова тижнева ставка, виплачується при виконанні KPI'),
    )
    commission_percent = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        default=Decimal('0'),
        verbose_name=_('Відсоток від замовлення'),
        help_text=_('Відсоток від суми замовлення'),
    )
    salary_start_date = models.DateField(
        null=True,
        blank=True,
        verbose_name=_('Дата початку нарахувань'),
        help_text=_('З якої дати починаються нарахування ставки'),
    )

    # Метадані
    assignment_comment = models.TextField(blank=True, verbose_name=_('Коментар при призначенні'))
    is_active = models.BooleanField(default=True, db_index=True, verbose_name=_('Активний'))

    class Meta:
        verbose_name = _('Рівень менеджера')
        verbose_name_plural = _('Рівні менеджерів')
        indexes = [
            models.Index(fields=['level', 'is_active'], name='mgmt_level_level_active'),
        ]

    def __str__(self):
        return f"{self.user.username} - {self.get_level_display()}"


class ManagerLevelHistory(models.Model):
    """Історія змін рівнів менеджера"""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='manager_level_history',
        verbose_name=_('Менеджер'),
    )
    old_level = models.CharField(
        max_length=20,
        choices=ManagerLevel.Level.choices,
        null=True,
        blank=True,
        verbose_name=_('Попередній рівень'),
    )
    new_level = models.CharField(
        max_length=20,
        choices=ManagerLevel.Level.choices,
        null=True,
        blank=True,
        verbose_name=_('Новий рівень'),
    )
    changed_at = models.DateTimeField(auto_now_add=True, db_index=True, verbose_name=_('Дата зміни'))
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='manager_level_changes_made',
        verbose_name=_('Змінив'),
    )

    # Фінансові умови на момент зміни
    old_weekly_salary = models.PositiveIntegerField(default=0, verbose_name=_('Попередня ставка'))
    new_weekly_salary = models.PositiveIntegerField(default=0, verbose_name=_('Нова ставка'))
    old_commission_percent = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        default=Decimal('0'),
        verbose_name=_('Попередній відсоток'),
    )
    new_commission_percent = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        default=Decimal('0'),
        verbose_name=_('Новий відсоток'),
    )

    reason = models.TextField(blank=True, verbose_name=_('Причина/коментар'))

    # Re-sign при зміні рівня (док 03): чи потрібен новий документ і чи
    # блокувати доступ менеджера до моменту підпису.
    requires_document = models.BooleanField(default=False, verbose_name=_('Потрібен новий документ'))
    access_blocked_until_signed = models.BooleanField(default=False, verbose_name=_('Блокувати доступ до підпису'))

    class Meta:
        verbose_name = _('Історія рівня менеджера')
        verbose_name_plural = _('Історія рівнів менеджерів')
        ordering = ['-changed_at']
        indexes = [
            models.Index(fields=['user', '-changed_at'], name='mgmt_lvlhist_user_date'),
        ]

    def __str__(self):
        old = self.get_old_level_display() if self.old_level else 'Немає'
        new = self.get_new_level_display()
        return f"{self.user.username}: {old} → {new}"


class WeeklySalaryAccrual(models.Model):
    """Тижневі нарахування ставки на основі KPI"""

    class Status(models.TextChoices):
        PENDING = 'pending', _('Очікує')
        ACCRUED = 'accrued', _('Нараховано')
        CANCELLED = 'cancelled', _('Скасовано')

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='weekly_salary_accruals',
        verbose_name=_('Менеджер'),
    )
    week_start = models.DateField(db_index=True, verbose_name=_('Початок тижня'))
    week_end = models.DateField(verbose_name=_('Кінець тижня'))

    # KPI метрики за тиждень
    conversions_count = models.PositiveIntegerField(
        default=0,
        verbose_name=_('Кількість конверсій'),
        help_text=_('Кількість конверсій (ORDER або TEST_BATCH) за тиждень'),
    )
    processed_clients_count = models.PositiveIntegerField(
        default=0,
        verbose_name=_('Оброблено клієнтів'),
        help_text=_('Кількість оброблених клієнтів за тиждень'),
    )

    # Розрахунок ставки
    base_weekly_salary = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        verbose_name=_('Базова ставка (грн)'),
        help_text=_('Базова тижнева ставка з ManagerLevel'),
    )
    kpi_multiplier = models.DecimalField(
        max_digits=3,
        decimal_places=2,
        verbose_name=_('KPI множник'),
        help_text=_('0.0 (0 конверсій), 0.5 (1 конверсія), 1.0 (2+ конверсії)'),
    )
    accrued_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        verbose_name=_('Нараховано (грн)'),
        help_text=_('base_weekly_salary × kpi_multiplier'),
    )

    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
        verbose_name=_('Статус'),
    )
    frozen_until = models.DateTimeField(
        verbose_name=_('Заморожено до'),
        help_text=_('Дата, до якої нарахування заморожене'),
    )

    note = models.TextField(blank=True, verbose_name=_('Примітка'))
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_('Створено'))

    class Meta:
        verbose_name = _('Тижневе нарахування ставки')
        verbose_name_plural = _('Тижневі нарахування ставок')
        ordering = ['-week_start']
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'week_start'],
                name='mgmt_weekly_salary_user_week_unique',
            ),
        ]
        indexes = [
            models.Index(fields=['user', '-week_start'], name='mgmt_wksal_user_week'),
            models.Index(fields=['status', 'frozen_until'], name='mgmt_wksal_status_frozen'),
        ]

    def __str__(self):
        return f"{self.user.username} - {self.week_start} ({self.accrued_amount} грн)"


class CallSession(models.Model):
    """Сесія вихідного дзвінка, ініційованого менеджером з UI (click-to-call).

    Створюється в момент натискання «Подзвонити»: Binotel повертає
    generalCallID одразу, тож ми зв'язуємо майбутній запис (webhook
    apiCallCompleted з тим самим generalCallID) з конкретним клієнтом/лідом
    та менеджером — надійніше, ніж матч лише за номером.
    """

    class Status(models.TextChoices):
        DIALING = "dialing", _("Набір")
        RINGING = "ringing", _("Дзвінок")
        TALKING = "talking", _("Розмова")
        ENDED = "ended", _("Завершено")
        FAILED = "failed", _("Помилка")

    manager = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="management_call_sessions",
        verbose_name=_("Менеджер"),
    )
    client = models.ForeignKey(
        Client,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="call_sessions",
        verbose_name=_("Клієнт"),
    )
    lead = models.ForeignKey(
        "management.ManagementLead",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="call_sessions",
        verbose_name=_("Лід"),
    )
    provider = models.CharField(max_length=64, default="binotel")
    internal_number = models.CharField(_("Лінія менеджера"), max_length=32, blank=True)
    phone = models.CharField(_("Номер клієнта"), max_length=64, blank=True)
    phone_normalized = models.CharField(max_length=64, blank=True, db_index=True)
    general_call_id = models.CharField(max_length=64, blank=True, db_index=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DIALING, db_index=True)
    disposition = models.CharField(max_length=32, blank=True)
    started_at = models.DateTimeField(auto_now_add=True, db_index=True)
    answered_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    duration_seconds = models.PositiveIntegerField(default=0)
    call_record = models.ForeignKey(
        "management.CallRecord",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="sessions",
        verbose_name=_("Запис дзвінка"),
    )
    meta = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Сесія дзвінка")
        verbose_name_plural = _("Сесії дзвінків")
        ordering = ["-started_at"]
        indexes = [
            models.Index(fields=["manager", "-started_at"], name="mgmt_callsess_mgr_dt"),
            models.Index(fields=["general_call_id"], name="mgmt_callsess_gcid"),
            models.Index(fields=["status", "-started_at"], name="mgmt_callsess_st_dt"),
        ]

    def __str__(self):
        return f"CallSession({self.manager_id}→{self.phone}) {self.status}"


class CallRecord(models.Model):
    class Direction(models.TextChoices):
        INBOUND = "inbound", _("Вхідний")
        OUTBOUND = "outbound", _("Вихідний")
        UNKNOWN = "unknown", _("Невідомо")

    class QaStatus(models.TextChoices):
        PENDING = "pending", _("Очікує QA")
        REVIEWED = "reviewed", _("Перевірено")
        EXEMPT = "exempt", _("Без QA")

    class AiStatus(models.TextChoices):
        NONE = "none", _("Не потрібен")
        PENDING = "pending", _("У черзі на аналіз")
        RUNNING = "running", _("Аналізується")
        DONE = "done", _("Проаналізовано")
        ERROR = "error", _("Помилка аналізу")
        SKIPPED = "skipped", _("Пропущено")

    provider = models.CharField(max_length=64, db_index=True, verbose_name=_("Провайдер"))
    external_call_id = models.CharField(max_length=255, verbose_name=_("Зовнішній call ID"))
    manager = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="management_call_records",
        verbose_name=_("Менеджер"),
    )
    matched_client = models.ForeignKey(
        Client,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="call_records",
        verbose_name=_("Матчений клієнт"),
    )
    phone = models.CharField(max_length=64, blank=True, verbose_name=_("Телефон"))
    direction = models.CharField(max_length=20, choices=Direction.choices, default=Direction.UNKNOWN, db_index=True)
    started_at = models.DateTimeField(null=True, blank=True, db_index=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    duration_seconds = models.PositiveIntegerField(default=0, verbose_name=_("Тривалість, сек"))
    recording_url = models.URLField(blank=True, verbose_name=_("Recording URL"))
    transcript_excerpt = models.TextField(blank=True, verbose_name=_("Уривок транскрипту"))
    qa_status = models.CharField(max_length=20, choices=QaStatus.choices, default=QaStatus.PENDING, db_index=True)
    ai_status = models.CharField(max_length=20, choices=AiStatus.choices, default=AiStatus.NONE, db_index=True)
    ai_attempts = models.PositiveSmallIntegerField(default=0)
    ai_locked_at = models.DateTimeField(null=True, blank=True)
    payload = models.JSONField(default=dict, blank=True, verbose_name=_("Payload"))
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Запис дзвінка")
        verbose_name_plural = _("Записи дзвінків")
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["provider", "external_call_id"], name="mgmt_call_provider_ext_unique"),
        ]
        indexes = [
            models.Index(fields=["manager", "-created_at"], name="mgmt_call_mgr_dt"),
            models.Index(fields=["qa_status", "-created_at"], name="mgmt_call_qa_dt"),
        ]

    def __str__(self):
        return f"{self.provider}:{self.external_call_id}"


class TelephonyHealthSnapshot(models.Model):
    class Status(models.TextChoices):
        HEALTHY = "healthy", _("Здорово")
        DEGRADED = "degraded", _("Деградація")
        OUTAGE = "outage", _("Недоступно")

    provider = models.CharField(max_length=64, db_index=True, verbose_name=_("Провайдер"))
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DEGRADED, db_index=True)
    total_events = models.PositiveIntegerField(default=0, verbose_name=_("Усього подій"))
    unmatched_calls = models.PositiveIntegerField(default=0, verbose_name=_("Не змечені дзвінки"))
    backlog_count = models.PositiveIntegerField(default=0, verbose_name=_("Backlog"))
    meta = models.JSONField(default=dict, blank=True, verbose_name=_("Meta"))
    snapshot_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = _("Health snapshot IP-телефонії")
        verbose_name_plural = _("Health snapshot-и IP-телефонії")
        ordering = ["-snapshot_at"]
        indexes = [
            models.Index(fields=["provider", "-snapshot_at"], name="mgmt_tel_health_dt"),
        ]

    def __str__(self):
        return f"{self.provider}:{self.status}"


class BinotelWebhookEvent(models.Model):
    """Журнал вхідних вебхуків Binotel (apiCallSettings / apiCallCompleted).

    Єдина точка інтеграції: Binotel шле обидва типи на один URL, ми розрізняємо
    їх за полем requestType. Зберігаємо сирий payload для спостережуваності на
    тестовій фазі та подальшого мапінгу у CallRecord/CRM.
    """

    class RequestType(models.TextChoices):
        CALL_SETTINGS = "apiCallSettings", _("Налаштування дзвінка (до з'єднання)")
        CALL_COMPLETED = "apiCallCompleted", _("Дзвінок завершено")
        UNKNOWN = "unknown", _("Невідомо")

    request_type = models.CharField(
        max_length=40, choices=RequestType.choices, default=RequestType.UNKNOWN, db_index=True
    )
    company_id = models.CharField(max_length=64, blank=True, db_index=True, verbose_name=_("companyID"))
    general_call_id = models.CharField(max_length=64, blank=True, db_index=True, verbose_name=_("generalCallID"))
    call_type = models.CharField(max_length=8, blank=True, verbose_name=_("callType (0 вх/1 вих)"))
    external_number = models.CharField(max_length=64, blank=True, db_index=True, verbose_name=_("Зовнішній номер"))
    internal_number = models.CharField(max_length=32, blank=True, verbose_name=_("Внутрішній номер"))
    remote_ip = models.CharField(max_length=64, blank=True, db_index=True, verbose_name=_("IP джерела"))
    ip_allowed = models.BooleanField(default=False, verbose_name=_("IP у whitelist Binotel"))
    payload = models.JSONField(default=dict, blank=True, verbose_name=_("Вхідний payload"))
    response_payload = models.JSONField(default=dict, blank=True, verbose_name=_("Відповідь"))
    handled_ok = models.BooleanField(default=False, verbose_name=_("Оброблено успішно"))
    error = models.TextField(blank=True, verbose_name=_("Помилка обробки"))
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = _("Вебхук Binotel")
        verbose_name_plural = _("Вебхуки Binotel")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["request_type", "-created_at"], name="mgmt_bnwh_type_dt"),
            models.Index(fields=["general_call_id"], name="mgmt_bnwh_gcid"),
        ]

    def __str__(self):
        return f"{self.request_type}:{self.general_call_id or self.external_number}"


class CallQAReview(models.Model):
    class Verdict(models.TextChoices):
        PASS = "pass", _("ОК")
        COACHING = "coaching", _("Потрібен коучинг")
        FAIL = "fail", _("Провал")

    call_record = models.ForeignKey(CallRecord, on_delete=models.CASCADE, related_name="qa_reviews", verbose_name=_("Дзвінок"))
    reviewer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="call_qa_reviews",
        verbose_name=_("Перевірив"),
    )
    score = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0.00"), verbose_name=_("QA score"))
    verdict = models.CharField(max_length=20, choices=Verdict.choices, default=Verdict.PASS, db_index=True)
    notes = models.TextField(blank=True, verbose_name=_("Нотатки"))
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = _("QA review дзвінка")
        verbose_name_plural = _("QA reviews дзвінків")
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.call_record_id}:{self.verdict}"


class CallAIAnalysis(models.Model):
    """Результат ШІ-аналізу запису розмови (Gemini).

    Окрема модель (а не поля у CallRecord), щоб зберігати історію
    перепрогонів і не змішувати з людським QA (CallQAReview). Аудіо НЕ
    зберігаємо — воно тягнеться з Binotel за generalCallID; тут лише
    структурований розбор, оцінка у стилі Mosaic та метрики прогону
    (швидкість, розмір аудіо, токени) для тестової фази.
    """

    class Status(models.TextChoices):
        PENDING = "pending", _("Очікує")
        RUNNING = "running", _("Аналізується")
        DONE = "done", _("Готово")
        ERROR = "error", _("Помилка")

    class Verdict(models.TextChoices):
        PASS = "pass", _("Сильна розмова")
        COACHING = "coaching", _("Потрібен коучинг")
        FAIL = "fail", _("Слабка розмова")
        UNKNOWN = "unknown", _("Невизначено")

    call_record = models.ForeignKey(
        CallRecord,
        on_delete=models.CASCADE,
        related_name="ai_analyses",
        verbose_name=_("Дзвінок"),
    )
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING, db_index=True
    )
    model = models.CharField(max_length=64, blank=True, verbose_name=_("Модель"))
    overall_score = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal("0.00"), verbose_name=_("Загальний бал")
    )
    verdict = models.CharField(
        max_length=20, choices=Verdict.choices, default=Verdict.UNKNOWN, db_index=True
    )
    transcript = models.TextField(blank=True, verbose_name=_("Транскрипт"))
    summary = models.TextField(blank=True, verbose_name=_("Резюме розмови"))
    client_identification = models.TextField(blank=True, verbose_name=_("Ідентифікація клієнта"))
    axes = models.JSONField(default=list, blank=True, verbose_name=_("Осі оцінки (Mosaic-style)"))
    discussed_well = models.JSONField(default=list, blank=True, verbose_name=_("Що обговорили добре"))
    missed_topics = models.JSONField(default=list, blank=True, verbose_name=_("Що не обговорили"))
    recommendations = models.JSONField(default=list, blank=True, verbose_name=_("Рекомендації менеджеру"))
    extracted_facts = models.JSONField(default=dict, blank=True, verbose_name=_("Факти з розмови (для сверки)"))
    discrepancies = models.JSONField(default=list, blank=True, verbose_name=_("Розбіжності з даними менеджера"))
    manager_context = models.TextField(blank=True, verbose_name=_("B2B-контекст менеджера"))
    result = models.JSONField(default=dict, blank=True, verbose_name=_("Повна сира відповідь"))
    audio_bytes = models.PositiveIntegerField(default=0, verbose_name=_("Розмір аудіо, байт"))
    elapsed_ms = models.PositiveIntegerField(default=0, verbose_name=_("Час аналізу, мс"))
    prompt_tokens = models.PositiveIntegerField(default=0, verbose_name=_("Prompt tokens"))
    output_tokens = models.PositiveIntegerField(default=0, verbose_name=_("Output tokens"))
    error = models.TextField(blank=True, verbose_name=_("Помилка"))
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="call_ai_analyses",
        verbose_name=_("Запустив"),
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("ШІ-аналіз дзвінка")
        verbose_name_plural = _("ШІ-аналізи дзвінків")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["call_record", "-created_at"], name="mgmt_callai_rec_dt"),
            models.Index(fields=["status", "-created_at"], name="mgmt_callai_status_dt"),
        ]

    def __str__(self):
        return f"AI[{self.status}] {self.call_record_id}:{self.overall_score}"


class SupervisorActionLog(models.Model):
    class ActionType(models.TextChoices):
        COACHING = "coaching", _("Коучинг")
        ESCALATION = "escalation", _("Ескалація")
        FREEZE_OVERRIDE = "freeze_override", _("Override freeze")
        APPEAL_RESOLUTION = "appeal_resolution", _("Рішення по апеляції")

    manager = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="supervisor_actions",
        verbose_name=_("Менеджер"),
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="performed_supervisor_actions",
        verbose_name=_("Хто виконав"),
    )
    action_type = models.CharField(max_length=32, choices=ActionType.choices, db_index=True)
    payload = models.JSONField(default=dict, blank=True, verbose_name=_("Payload"))
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = _("Лог дії супервайзера")
        verbose_name_plural = _("Логи дій супервайзера")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["action_type", "-created_at"], name="mgmt_supervisor_act"),
        ]

    def __str__(self):
        return f"{self.action_type}:{self.manager_id}"


class DtfBridgeSnapshot(models.Model):
    class Status(models.TextChoices):
        FRESH = "fresh", _("Актуально")
        DEGRADED = "degraded", _("Деградація")
        STALE = "stale", _("Застаріло")

    source_key = models.CharField(max_length=64, db_index=True, verbose_name=_("Ключ джерела"))
    snapshot_date = models.DateField(db_index=True, verbose_name=_("Дата snapshot"))
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DEGRADED, db_index=True)
    freshness_seconds = models.PositiveIntegerField(default=0, verbose_name=_("Freshness, сек"))
    payload = models.JSONField(default=dict, blank=True, verbose_name=_("Payload"))
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("DTF bridge snapshot")
        verbose_name_plural = _("DTF bridge snapshots")
        ordering = ["-snapshot_date", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["source_key", "snapshot_date"], name="mgmt_dtf_source_day_unique"),
        ]
        indexes = [
            models.Index(fields=["source_key", "-snapshot_date"], name="mgmt_dtf_source_dt"),
        ]

    def __str__(self):
        return f"{self.source_key}:{self.snapshot_date}"


class WorkingCalendarProfile(models.Model):
    name = models.CharField(max_length=120, verbose_name=_("Назва профілю"))
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="management_working_calendar_profiles",
        null=True,
        blank=True,
        verbose_name=_("Менеджер"),
    )
    timezone_name = models.CharField(max_length=64, default="Europe/Kyiv", verbose_name=_("Таймзона"))
    weekly_template = models.JSONField(default=dict, blank=True, verbose_name=_("Тижневий шаблон"))
    capacity_template = models.JSONField(default=dict, blank=True, verbose_name=_("Шаблон доступності"))
    is_active = models.BooleanField(default=True, db_index=True, verbose_name=_("Активний"))
    meta = models.JSONField(default=dict, blank=True, verbose_name=_("Мета"))
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Профіль робочого календаря")
        verbose_name_plural = _("Профілі робочого календаря")
        ordering = ["name", "id"]

    def __str__(self):
        return self.name


class WorkingCalendarAssignment(models.Model):
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="management_working_calendar_assignments",
        verbose_name=_("Менеджер"),
    )
    profile = models.ForeignKey(
        WorkingCalendarProfile,
        on_delete=models.CASCADE,
        related_name="assignments",
        verbose_name=_("Профіль"),
    )
    effective_from = models.DateField(db_index=True, verbose_name=_("Діє з"))
    effective_to = models.DateField(null=True, blank=True, db_index=True, verbose_name=_("Діє до"))
    priority = models.PositiveSmallIntegerField(default=100, verbose_name=_("Пріоритет"))
    meta = models.JSONField(default=dict, blank=True, verbose_name=_("Мета"))
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Призначення робочого календаря")
        verbose_name_plural = _("Призначення робочих календарів")
        ordering = ["owner_id", "-effective_from", "-priority"]
        constraints = [
            models.UniqueConstraint(
                fields=["owner", "profile", "effective_from"],
                name="mgmt_workcal_assign_unique",
            ),
        ]

    def __str__(self):
        return f"{self.owner_id}:{self.profile_id}@{self.effective_from}"


class WorkingCalendarException(models.Model):
    profile = models.ForeignKey(
        WorkingCalendarProfile,
        on_delete=models.CASCADE,
        related_name="exceptions",
        verbose_name=_("Профіль"),
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="management_working_calendar_exceptions",
        null=True,
        blank=True,
        verbose_name=_("Менеджер"),
    )
    day = models.DateField(db_index=True, verbose_name=_("День"))
    status = models.CharField(max_length=32, choices=ManagerDayStatus.Status.choices, default=ManagerDayStatus.Status.WORKING, db_index=True)
    capacity_factor = models.DecimalField(max_digits=4, decimal_places=2, default=Decimal("1.00"), verbose_name=_("Фактор доступності"))
    source_reason = models.CharField(max_length=255, blank=True, verbose_name=_("Причина / джерело"))
    meta = models.JSONField(default=dict, blank=True, verbose_name=_("Мета"))
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Виняток робочого календаря")
        verbose_name_plural = _("Винятки робочого календаря")
        ordering = ["-day", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["profile", "owner", "day"],
                name="mgmt_workcal_exception_unique",
            ),
        ]

    def __str__(self):
        return f"{self.profile_id}:{self.day}"


class FollowUpEvent(models.Model):
    class EventType(models.TextChoices):
        OPENED = "opened", _("Відкрито")
        CLOSED = "closed", _("Закрито")
        NOTIFIED = "notified", _("Нагадування відправлено")
        AMENDED = "amended", _("Скориговано")

    event_key = models.CharField(max_length=128, unique=True, verbose_name=_("Ключ події"))
    followup = models.ForeignKey(
        ClientFollowUp,
        on_delete=models.CASCADE,
        related_name="events",
        verbose_name=_("Follow-up"),
    )
    client = models.ForeignKey(
        Client,
        on_delete=models.CASCADE,
        related_name="followup_events",
        verbose_name=_("Клієнт"),
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="management_followup_events",
        verbose_name=_("Менеджер"),
    )
    source_interaction = models.ForeignKey(
        "management.ClientInteractionAttempt",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="followup_events",
        verbose_name=_("Джерело взаємодії"),
    )
    event_type = models.CharField(max_length=24, choices=EventType.choices, db_index=True, verbose_name=_("Тип події"))
    followup_kind = models.CharField(max_length=24, choices=ClientFollowUp.Kind.choices, default=ClientFollowUp.Kind.CALLBACK, db_index=True)
    status_before = models.CharField(max_length=20, blank=True, verbose_name=_("Статус до"))
    status_after = models.CharField(max_length=20, blank=True, verbose_name=_("Статус після"))
    close_reason = models.CharField(max_length=64, blank=True, db_index=True, verbose_name=_("Причина закриття"))
    completion_quality = models.CharField(max_length=32, blank=True, verbose_name=_("Якість виконання"))
    due_at = models.DateTimeField(null=True, blank=True, db_index=True, verbose_name=_("Due at"))
    due_date = models.DateField(null=True, blank=True, db_index=True, verbose_name=_("Due date"))
    occurred_at = models.DateTimeField(db_index=True, verbose_name=_("Коли"))
    source = models.CharField(max_length=64, default="runtime", db_index=True, verbose_name=_("Джерело"))
    priority_snapshot = models.JSONField(default=dict, blank=True, verbose_name=_("Знімок пріоритету"))
    payload = models.JSONField(default=dict, blank=True, verbose_name=_("Payload"))
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("Подія follow-up")
        verbose_name_plural = _("Події follow-up")
        ordering = ["-occurred_at", "-id"]
        indexes = [
            models.Index(fields=["owner", "due_date", "event_type"], name="mgmt_fuevent_owner_dt"),
            models.Index(fields=["followup", "-occurred_at"], name="mgmt_fuevent_fu_dt"),
        ]

    def __str__(self):
        return f"{self.followup_id}:{self.event_type}"


class ClientStageEvent(models.Model):
    event_key = models.CharField(max_length=128, unique=True, verbose_name=_("Ключ події"))
    client = models.ForeignKey(
        Client,
        on_delete=models.CASCADE,
        related_name="stage_events",
        verbose_name=_("Клієнт"),
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="management_stage_events",
        verbose_name=_("Менеджер"),
    )
    interaction = models.ForeignKey(
        "management.ClientInteractionAttempt",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="stage_events",
        verbose_name=_("Взаємодія"),
    )
    stage_code = models.CharField(max_length=64, db_index=True, verbose_name=_("Код стадії"))
    phase_number = models.PositiveIntegerField(default=1, db_index=True, verbose_name=_("Номер фази"))
    result_code = models.CharField(max_length=50, blank=True, db_index=True, verbose_name=_("Результат"))
    occurred_at = models.DateTimeField(db_index=True, verbose_name=_("Коли"))
    payload = models.JSONField(default=dict, blank=True, verbose_name=_("Payload"))
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("Подія стадії клієнта")
        verbose_name_plural = _("Події стадій клієнта")
        ordering = ["-occurred_at", "-id"]
        indexes = [
            models.Index(fields=["owner", "stage_code", "-occurred_at"], name="mgmt_stage_owner_code"),
            models.Index(fields=["client", "-occurred_at"], name="mgmt_stage_client_dt"),
        ]

    def __str__(self):
        return f"{self.client_id}:{self.stage_code}"


class ReasonSignal(models.Model):
    event_key = models.CharField(max_length=128, unique=True, verbose_name=_("Ключ сигналу"))
    client = models.ForeignKey(
        Client,
        on_delete=models.CASCADE,
        related_name="reason_signals",
        verbose_name=_("Клієнт"),
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="management_reason_signals",
        verbose_name=_("Менеджер"),
    )
    interaction = models.ForeignKey(
        "management.ClientInteractionAttempt",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reason_signals",
        verbose_name=_("Взаємодія"),
    )
    result_code = models.CharField(max_length=50, blank=True, db_index=True, verbose_name=_("Результат"))
    reason_code = models.CharField(max_length=64, blank=True, db_index=True, verbose_name=_("Код причини"))
    quality_label = models.CharField(max_length=32, blank=True, db_index=True, verbose_name=_("Якість сигналу"))
    captured_at = models.DateTimeField(db_index=True, verbose_name=_("Коли"))
    payload = models.JSONField(default=dict, blank=True, verbose_name=_("Payload"))
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("Сигнал причини")
        verbose_name_plural = _("Сигнали причин")
        ordering = ["-captured_at", "-id"]
        indexes = [
            models.Index(fields=["owner", "reason_code", "-captured_at"], name="mgmt_reason_owner_code"),
            models.Index(fields=["client", "-captured_at"], name="mgmt_reason_client_dt"),
        ]

    def __str__(self):
        return f"{self.client_id}:{self.reason_code or 'n/a'}"


class VerifiedWorkEvent(models.Model):
    event_key = models.CharField(max_length=128, unique=True, verbose_name=_("Ключ верифікації"))
    client = models.ForeignKey(
        Client,
        on_delete=models.CASCADE,
        related_name="verified_work_events",
        verbose_name=_("Клієнт"),
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="management_verified_work_events",
        verbose_name=_("Менеджер"),
    )
    interaction = models.ForeignKey(
        "management.ClientInteractionAttempt",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="verified_work_events",
        verbose_name=_("Взаємодія"),
    )
    verification_level = models.CharField(max_length=32, choices=ClientInteractionAttempt.VerificationLevel.choices, db_index=True, verbose_name=_("Рівень верифікації"))
    evidence_kind = models.CharField(max_length=64, blank=True, db_index=True, verbose_name=_("Тип доказу"))
    verified_at = models.DateTimeField(db_index=True, verbose_name=_("Коли"))
    payload = models.JSONField(default=dict, blank=True, verbose_name=_("Payload"))
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("Подія підтвердженої роботи")
        verbose_name_plural = _("Події підтвердженої роботи")
        ordering = ["-verified_at", "-id"]
        indexes = [
            models.Index(fields=["owner", "verification_level", "-verified_at"], name="mgmt_vwork_owner_lvl"),
            models.Index(fields=["client", "-verified_at"], name="mgmt_vwork_client_dt"),
        ]

    def __str__(self):
        return f"{self.client_id}:{self.verification_level}"


class ManagerDayFact(models.Model):
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="management_day_facts",
        verbose_name=_("Менеджер"),
    )
    day = models.DateField(db_index=True, verbose_name=_("День"))
    fact_key = models.CharField(max_length=64, default="daily_shadow_v7", verbose_name=_("Ключ факту"))
    schema_version = models.CharField(max_length=32, default="v7", verbose_name=_("Версія схеми"))
    day_status = models.CharField(max_length=32, choices=ManagerDayStatus.Status.choices, default=ManagerDayStatus.Status.WORKING, db_index=True)
    capacity_factor = models.DecimalField(max_digits=4, decimal_places=2, default=Decimal("1.00"), verbose_name=_("Фактор доступності"))
    interactions_total = models.PositiveIntegerField(default=0, verbose_name=_("Всього взаємодій"))
    verified_interactions = models.PositiveIntegerField(default=0, verbose_name=_("Верифіковані взаємодії"))
    reason_signals_total = models.PositiveIntegerField(default=0, verbose_name=_("Сигнали причин"))
    followups_opened = models.PositiveIntegerField(default=0, verbose_name=_("Відкриті follow-up"))
    followups_closed = models.PositiveIntegerField(default=0, verbose_name=_("Закриті follow-up"))
    followups_completed = models.PositiveIntegerField(default=0, verbose_name=_("Виконані follow-up"))
    followups_overdue = models.PositiveIntegerField(default=0, verbose_name=_("Прострочені follow-up"))
    open_promises = models.PositiveIntegerField(default=0, verbose_name=_("Відкриті promise"))
    open_nurtures = models.PositiveIntegerField(default=0, verbose_name=_("Відкриті nurture"))
    freshness_seconds = models.PositiveIntegerField(default=0, verbose_name=_("Freshness, сек"))
    payload = models.JSONField(default=dict, blank=True, verbose_name=_("Payload"))
    materialized_at = models.DateTimeField(auto_now=True, db_index=True)

    class Meta:
        verbose_name = _("Денний факт менеджера")
        verbose_name_plural = _("Денні факти менеджера")
        ordering = ["-day", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["owner", "day", "fact_key"], name="mgmt_day_fact_unique"),
        ]
        indexes = [
            models.Index(fields=["owner", "-day"], name="mgmt_day_fact_owner_dt"),
            models.Index(fields=["fact_key", "-day"], name="mgmt_day_fact_key_dt"),
        ]

    def __str__(self):
        return f"{self.owner_id}:{self.day}:{self.fact_key}"


class ScoreAmendment(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", _("Очікує")
        APPLIED = "applied", _("Застосовано")
        REJECTED = "rejected", _("Відхилено")

    event_key = models.CharField(max_length=128, unique=True, verbose_name=_("Ключ поправки"))
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="management_score_amendments",
        verbose_name=_("Менеджер"),
    )
    effective_date = models.DateField(db_index=True, verbose_name=_("Дата дії"))
    fact = models.ForeignKey(
        ManagerDayFact,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="score_amendments",
        verbose_name=_("Факт дня"),
    )
    appeal = models.ForeignKey(
        ScoreAppeal,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="score_amendments",
        verbose_name=_("Апеляція"),
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING, db_index=True)
    delta_score = models.DecimalField(max_digits=8, decimal_places=2, default=Decimal("0.00"), verbose_name=_("Зміна score"))
    reason = models.TextField(blank=True, verbose_name=_("Причина"))
    payload = models.JSONField(default=dict, blank=True, verbose_name=_("Payload"))
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Поправка до score")
        verbose_name_plural = _("Поправки до score")
        ordering = ["-effective_date", "-id"]
        indexes = [
            models.Index(fields=["owner", "-effective_date"], name="mgmt_score_am_owner_dt"),
        ]

    def __str__(self):
        return f"{self.owner_id}:{self.effective_date}:{self.delta_score}"


# =====================================================================
# Management Admin Center — нові моделі (онбординг, компенсації, ledger,
# договори, PII, audit). Усе аддитивно: нові таблиці/поля, нічого не
# видаляємо й не змінюємо семантику існуючих моделей.
# Док: twocomms/Management Implementations/02_DATA_MODEL_AND_MIGRATIONS.md
# =====================================================================


class ManagerCompensationSettings(models.Model):
    """Історія умов винагороди менеджера (база, відсоток, заморозка, KPI).

    Окремо від ManagerLevel: рівень = роль/доступ; умови винагороди
    змінюються частіше й потребують історії. «Поточні» умови = запис із
    найбільшим effective_from, де effective_to IS NULL.
    """

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="compensation_settings",
        verbose_name=_("Менеджер"),
    )
    monthly_base_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0.00"),
        verbose_name=_("Базова винагорода за період (грн)"),
    )
    weeks_per_month = models.PositiveSmallIntegerField(
        default=4, verbose_name=_("Тижнів у періоді (для розрахунку тижневої бази)"),
    )
    commission_percent = models.DecimalField(
        max_digits=6, decimal_places=2, default=Decimal("0.00"),
        verbose_name=_("Відсоток процентної винагороди"),
    )
    frozen_days = models.PositiveSmallIntegerField(
        default=14, verbose_name=_("Період заморозки (днів)"),
    )
    kpi_min_paid_invoices_per_week = models.PositiveSmallIntegerField(
        default=2, verbose_name=_("KPI: мін. оплачених накладних/тиждень"),
    )
    kpi_min_units_per_invoice = models.PositiveSmallIntegerField(
        default=8, verbose_name=_("KPI: мін. одиниць у накладній"),
    )
    effective_from = models.DateField(db_index=True, verbose_name=_("Діє з"))
    effective_to = models.DateField(
        null=True, blank=True, db_index=True,
        verbose_name=_("Діє до (null = чинні зараз)"),
    )
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="compensation_settings_changed",
        verbose_name=_("Змінив"),
    )
    reason = models.CharField(max_length=255, blank=True, verbose_name=_("Причина зміни"))
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("Умови винагороди менеджера")
        verbose_name_plural = _("Умови винагороди менеджерів")
        ordering = ["owner_id", "-effective_from"]
        indexes = [
            models.Index(fields=["owner", "-effective_from"], name="mgmt_comp_owner_eff"),
            models.Index(fields=["effective_to"], name="mgmt_comp_eff_to"),
        ]

    @property
    def weekly_base_amount(self) -> Decimal:
        weeks = int(self.weeks_per_month or 0)
        if weeks <= 0:
            weeks = 4
        try:
            return (Decimal(self.monthly_base_amount or 0) / Decimal(weeks)).quantize(Decimal("0.01"))
        except Exception:
            return Decimal("0.00")

    def __str__(self):
        return f"{self.owner_id}: base={self.monthly_base_amount} pct={self.commission_percent} from {self.effective_from}"


class ManagerWeeklyReview(models.Model):
    """Тижневе рішення адміна по базовій винагороді (100/50/custom/0)."""

    class Decision(models.TextChoices):
        FULL = "full", _("Повна (100%)")
        HALF = "half", _("Половина (50%)")
        CUSTOM = "custom", _("Своя сума")
        NONE = "none", _("Без винагороди (0%)")

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="weekly_reviews",
        verbose_name=_("Менеджер"),
    )
    week_start = models.DateField(db_index=True, verbose_name=_("Початок тижня"))
    week_end = models.DateField(verbose_name=_("Кінець тижня"))

    kpi_paid_invoices = models.PositiveIntegerField(default=0, verbose_name=_("Оплачених накладних"))
    kpi_units = models.PositiveIntegerField(default=0, verbose_name=_("Одиниць продано"))
    kpi_sales_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0.00"), verbose_name=_("Сума продажів"),
    )
    kpi_processed_clients = models.PositiveIntegerField(default=0, verbose_name=_("Оброблено клієнтів"))

    calculated_weekly_base = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0.00"),
        verbose_name=_("Розрахункова тижнева база"),
    )
    admin_decision = models.CharField(
        max_length=10, choices=Decision.choices, blank=True, db_index=True,
        verbose_name=_("Рішення адміна"),
    )
    awarded_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0.00"),
        verbose_name=_("Нараховано (грн)"),
    )
    reason = models.TextField(blank=True, verbose_name=_("Причина (обовʼязково якщо не full)"))
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="weekly_reviews_decided",
        verbose_name=_("Вирішив"),
    )
    decided_at = models.DateTimeField(null=True, blank=True, verbose_name=_("Дата рішення"))
    ledger_entry = models.ForeignKey(
        "ManagerEarningsLedger",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="weekly_reviews",
        verbose_name=_("Запис у ledger"),
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("Тижневе рішення")
        verbose_name_plural = _("Тижневі рішення")
        ordering = ["-week_start"]
        constraints = [
            models.UniqueConstraint(fields=["owner", "week_start"], name="mgmt_weekly_review_owner_week"),
        ]
        indexes = [
            models.Index(fields=["owner", "-week_start"], name="mgmt_wreview_owner_wk"),
            models.Index(fields=["admin_decision", "-week_start"], name="mgmt_wreview_dec_wk"),
        ]

    def __str__(self):
        return f"{self.owner_id}:{self.week_start} {self.admin_decision or 'pending'}"


class ManagerEarningsLedger(models.Model):
    """Єдиний шар руху коштів менеджера (вводиться поетапно, фаза 1 — паралельно)."""

    class SourceType(models.TextChoices):
        INVOICE_COMMISSION = "invoice_commission", _("Процентна винагорода (накладна)")
        WEEKLY_BASE = "weekly_base", _("Базова винагорода (тиждень)")
        ADJUSTMENT = "adjustment", _("Коригування")
        RELEASE = "release", _("Розморозка")
        WITHDRAWAL = "withdrawal", _("Виплата")
        REFUND_CANCEL = "refund_cancel", _("Скасування через повернення")

    class Status(models.TextChoices):
        PENDING = "pending", _("Очікує")
        FROZEN = "frozen", _("Заморожено")
        AVAILABLE = "available", _("Доступно")
        PAID = "paid", _("Виплачено")
        CANCELLED = "cancelled", _("Скасовано")

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="earnings_ledger",
        verbose_name=_("Менеджер"),
    )
    source_type = models.CharField(max_length=24, choices=SourceType.choices, db_index=True, verbose_name=_("Тип джерела"))
    source_id = models.CharField(max_length=64, blank=True, db_index=True, verbose_name=_("ID джерела"))
    amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0.00"),
        verbose_name=_("Сума (+ нарахування / - списання)"),
    )
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING, db_index=True, verbose_name=_("Статус"))
    available_at = models.DateTimeField(null=True, blank=True, db_index=True, verbose_name=_("Доступно з"))
    reason = models.TextField(blank=True, verbose_name=_("Пояснення"))
    commission_accrual = models.ForeignKey(
        "ManagerCommissionAccrual",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ledger_entries",
        verbose_name=_("Звʼязане нарахування"),
    )
    meta = models.JSONField(default=dict, blank=True, verbose_name=_("Мета"))
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="earnings_ledger_created",
        verbose_name=_("Створив"),
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = _("Запис ledger винагороди")
        verbose_name_plural = _("Записи ledger винагороди")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["owner", "status"], name="mgmt_ledger_owner_st"),
            models.Index(fields=["status", "available_at"], name="mgmt_ledger_st_avail"),
            models.Index(fields=["source_type", "source_id"], name="mgmt_ledger_src"),
        ]

    def __str__(self):
        return f"{self.owner_id}:{self.source_type}:{self.amount} [{self.status}]"


class ContractTemplate(models.Model):
    """Шаблон договору співпраці (ЦПД) для рівня."""

    name = models.CharField(max_length=120, verbose_name=_("Назва"))
    version = models.CharField(max_length=32, verbose_name=_("Версія"))
    role_level_scope = models.CharField(max_length=64, blank=True, verbose_name=_("Область (рівень)"))
    template_file = models.FileField(upload_to="manager_docs/templates/", null=True, blank=True, verbose_name=_("Файл шаблону"))
    body_text = models.TextField(blank=True, verbose_name=_("Текст шаблону (placeholder до юриста)"))
    template_variables = models.JSONField(default=dict, blank=True, verbose_name=_("Змінні шаблону"))
    is_active = models.BooleanField(default=True, db_index=True, verbose_name=_("Активний"))
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("Шаблон договору")
        verbose_name_plural = _("Шаблони договорів")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["is_active", "role_level_scope"], name="mgmt_ctpl_active_scope"),
        ]

    def __str__(self):
        return f"{self.name} v{self.version}"


class ManagerDocument(models.Model):
    """Документ менеджера (онбординг/ЦПД), окремо від ManagementContract (товар)."""

    class DocKind(models.TextChoices):
        ONBOARDING_RULES = "onboarding_rules", _("Правила співпраці")
        CPD_LEVEL1 = "cpd_level1", _("ЦПД (1 рівень)")
        CPD_LEVEL2 = "cpd_level2", _("ЦПД (2 рівень)")
        TOP = "top", _("Топ-менеджер")
        PROJECT = "project", _("Project-менеджер")
        OTHER = "other", _("Інше")

    class Status(models.TextChoices):
        DRAFT = "draft", _("Чернетка")
        SENT_TO_SIGN = "sent_to_sign", _("Надіслано на підпис")
        SIGNED = "signed", _("Підписано")
        REJECTED = "rejected", _("Відхилено")
        EXPIRED = "expired", _("Прострочено")
        REVOKED = "revoked", _("Відкликано")

    class SignatureProvider(models.TextChoices):
        DIIA = "diia", _("Дія.Підпис")
        VCHASNO = "vchasno", _("Vchasno")
        MANUAL = "manual", _("Вручну")
        OTHER = "other", _("Інше")

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="manager_documents",
        verbose_name=_("Менеджер"),
    )
    template = models.ForeignKey(
        ContractTemplate,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="documents",
        verbose_name=_("Шаблон"),
    )
    doc_kind = models.CharField(max_length=24, choices=DocKind.choices, default=DocKind.OTHER, db_index=True, verbose_name=_("Тип документа"))
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT, db_index=True, verbose_name=_("Статус"))
    title = models.CharField(max_length=255, blank=True, verbose_name=_("Заголовок"))
    generated_pdf = models.FileField(upload_to="manager_docs/generated/", null=True, blank=True, verbose_name=_("Згенерований PDF"))
    signed_file = models.FileField(upload_to="manager_docs/signed/", null=True, blank=True, verbose_name=_("Підписаний файл"))
    signature_provider = models.CharField(max_length=16, choices=SignatureProvider.choices, blank=True, verbose_name=_("Провайдер підпису"))
    signature_external_id = models.CharField(max_length=255, blank=True, verbose_name=_("Зовнішній ID підпису"))
    signed_at = models.DateTimeField(null=True, blank=True, verbose_name=_("Підписано"))
    required_for_access = models.BooleanField(default=False, verbose_name=_("Потрібен для доступу"))
    access_blocking = models.BooleanField(default=False, db_index=True, verbose_name=_("Блокує доступ до підпису"))
    template_version_snapshot = models.CharField(max_length=32, blank=True, verbose_name=_("Версія шаблону (snapshot)"))
    payload = models.JSONField(default=dict, blank=True, verbose_name=_("Дані документа"))
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="manager_documents_created",
        verbose_name=_("Створив"),
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Документ менеджера")
        verbose_name_plural = _("Документи менеджерів")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["owner", "status"], name="mgmt_doc_owner_st"),
            models.Index(fields=["access_blocking", "status"], name="mgmt_doc_block_st"),
        ]

    def __str__(self):
        return f"{self.owner_id}:{self.doc_kind}:{self.status}"


class ManagerPersonalData(models.Model):
    """PII менеджера. Значення шифруються на рівні застосунку (Fernet)."""

    class Source(models.TextChoices):
        MANUAL = "manual", _("Ручний ввід")
        DIIA_SHARING = "diia_sharing", _("Дія Sharing")
        UPLOADED_DOC = "uploaded_doc", _("Завантажений документ")
        OTHER = "other", _("Інше")

    owner = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="personal_data",
        verbose_name=_("Менеджер"),
    )
    full_name_enc = models.BinaryField(null=True, blank=True, editable=False)
    birth_date_enc = models.BinaryField(null=True, blank=True, editable=False)
    tax_id_enc = models.BinaryField(null=True, blank=True, editable=False)
    passport_enc = models.BinaryField(null=True, blank=True, editable=False)
    address_enc = models.BinaryField(null=True, blank=True, editable=False)
    phone_enc = models.BinaryField(null=True, blank=True, editable=False)
    # Маски/хвости для UI (не секрет): останні цифри, щоб показати ****1234.
    tax_id_tail = models.CharField(max_length=8, blank=True, verbose_name=_("Хвіст ІПН"))
    passport_tail = models.CharField(max_length=8, blank=True, verbose_name=_("Хвіст паспорта"))
    source = models.CharField(max_length=16, choices=Source.choices, default=Source.MANUAL, verbose_name=_("Джерело"))
    consent_version = models.CharField(max_length=32, blank=True, verbose_name=_("Версія згоди"))
    consent_at = models.DateTimeField(null=True, blank=True, verbose_name=_("Дата згоди"))
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Персональні дані менеджера")
        verbose_name_plural = _("Персональні дані менеджерів")

    def __str__(self):
        return f"PII<{self.owner_id}>"


class ManagerOnboardingConsent(models.Model):
    """Згода менеджера при першому вході (18+, правила, ПДн)."""

    class SignedVia(models.TextChoices):
        CHECKBOX = "checkbox", _("Чекбокс")
        DIIA = "diia", _("Дія")

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="onboarding_consents",
        verbose_name=_("Менеджер"),
    )
    rules_version = models.CharField(max_length=32, db_index=True, verbose_name=_("Версія правил"))
    accepted_18plus = models.BooleanField(default=False, verbose_name=_("Підтвердив 18+"))
    accepted_rules = models.BooleanField(default=False, verbose_name=_("Ознайомлений з правилами"))
    accepted_pdn = models.BooleanField(default=False, verbose_name=_("Згода на обробку ПДн"))
    signed_via = models.CharField(max_length=12, choices=SignedVia.choices, default=SignedVia.CHECKBOX, verbose_name=_("Спосіб підтвердження"))
    diia_signature_id = models.CharField(max_length=255, blank=True, verbose_name=_("ID підпису Дія"))
    ip = models.GenericIPAddressField(null=True, blank=True, verbose_name=_("IP"))
    user_agent = models.CharField(max_length=512, blank=True, verbose_name=_("User-Agent"))
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = _("Згода онбордингу")
        verbose_name_plural = _("Згоди онбордингу")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["owner", "-created_at"], name="mgmt_consent_owner_dt"),
            models.Index(fields=["owner", "rules_version"], name="mgmt_consent_owner_ver"),
        ]

    def __str__(self):
        return f"{self.owner_id}:{self.rules_version}"


class AdminAuditLog(models.Model):
    """Загальний audit log критичних адмін-дій (гроші/доступ/рівень/PII)."""

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="admin_audit_logs",
        verbose_name=_("Хто"),
    )
    actor_role = models.CharField(max_length=64, blank=True, verbose_name=_("Роль"))
    action = models.CharField(max_length=128, db_index=True, verbose_name=_("Дія"))
    entity_type = models.CharField(max_length=64, blank=True, db_index=True, verbose_name=_("Тип сутності"))
    entity_id = models.CharField(max_length=64, blank=True, db_index=True, verbose_name=_("ID сутності"))
    before = models.JSONField(default=dict, blank=True, verbose_name=_("До"))
    after = models.JSONField(default=dict, blank=True, verbose_name=_("Після"))
    reason = models.TextField(blank=True, verbose_name=_("Причина"))
    ip = models.GenericIPAddressField(null=True, blank=True, verbose_name=_("IP"))
    user_agent = models.CharField(max_length=512, blank=True, verbose_name=_("User-Agent"))
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = _("Запис audit log")
        verbose_name_plural = _("Audit log")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["entity_type", "entity_id", "-created_at"], name="mgmt_audit_entity"),
            models.Index(fields=["action", "-created_at"], name="mgmt_audit_action"),
            models.Index(fields=["actor", "-created_at"], name="mgmt_audit_actor"),
        ]

    def __str__(self):
        return f"{self.action}:{self.entity_type}:{self.entity_id}"


# ===========================================================================
# Instagram Direct bot (тестова фаза) — налаштування, лог-консоль, дедуп.
# Бот працює через поллінг інбоксу @twocomms (page-token), бо webhook на
# вхідні `messages` доступний лише в Live-режимі застосунку. Webhook лишається
# підключеним і використовує той самий сервіс відповіді.
# ===========================================================================


from management.services.ig_core_policy import CANONICAL_IG_CORE_POLICY


DEFAULT_BOT_SYSTEM_PROMPT = CANONICAL_IG_CORE_POLICY
_BOT_SECRET_PREFIX = "fernet:v1:"


class BotSecretEncryptionUnavailable(RuntimeError):
    """A custom bot credential must never fall back to plaintext storage."""


def _encrypt_bot_secret(value: str) -> str:
    raw = str(value or "")
    if not raw:
        return ""
    from management.services import pii

    try:
        token = pii.encrypt(raw)
    except (pii.PIIKeyMissing, ValueError) as exc:
        raise BotSecretEncryptionUnavailable(
            "FIELD_ENCRYPTION_KEY is required before storing a custom bot credential"
        ) from exc
    if not token:
        return ""
    return _BOT_SECRET_PREFIX + token.decode("ascii")


def _decrypt_bot_secret(value: str) -> str:
    stored = str(value or "")
    if not stored:
        return ""
    # A short deployment window can observe a pre-migration row. It remains
    # readable, but every normal write and the data migration encrypt it.
    if not stored.startswith(_BOT_SECRET_PREFIX):
        return stored
    from management.services import pii

    return pii.decrypt(stored[len(_BOT_SECRET_PREFIX):])


class InstagramBotSettings(models.Model):
    """Singleton-налаштування Instagram-бота (одна строка, pk=1)."""

    class CredSource(models.TextChoices):
        ENV = "env", _("З ENV сервера")
        CUSTOM = "custom", _("Свій ключ")

    is_enabled = models.BooleanField(default=False)
    # Call auto-analysis is independently switchable from the Instagram bot.
    # The legacy column name is retained so deployment never rewrites data.
    call_auto_analysis_enabled = models.BooleanField(
        default=False, db_column="binotel_ai_enabled"
    )

    direct_source = models.CharField(
        max_length=10, choices=CredSource.choices, default=CredSource.ENV
    )
    custom_direct_token_encrypted = models.TextField(
        db_column="custom_direct_token", blank=True, default=""
    )
    gemini_source = models.CharField(
        max_length=10, choices=CredSource.choices, default=CredSource.ENV
    )
    custom_gemini_key_encrypted = models.TextField(
        db_column="custom_gemini_key", blank=True, default=""
    )

    # Account identity is explicit singleton configuration, never a repository
    # default that can silently bind a fresh install to a real account.
    page_id = models.CharField(max_length=64, default="")
    ig_user_id = models.CharField(max_length=64, default="")

    trigger_text = models.CharField(max_length=255, default="")
    reply_text = models.CharField(max_length=1000, default="")
    poll_interval_seconds = models.PositiveIntegerField(default=3)

    # AI-режим (Gemini). Якщо увімкнено — бот веде вільну розмову; інакше
    # працює простий тригер trigger_text -> reply_text.
    ai_enabled = models.BooleanField(default=True)
    class GeminiRoutingMode(models.TextChoices):
        ADAPTIVE = "adaptive", _("Адаптивний")
        PINNED = "pinned", _("Тимчасово закріплений")

    # ``gemini_model`` is retained as legacy/operator display state.  Adaptive
    # routing never treats it as a per-turn override: only the explicit,
    # audited and expiring pin below can change the first live model.
    gemini_routing_mode = models.CharField(
        max_length=12,
        choices=GeminiRoutingMode.choices,
        default=GeminiRoutingMode.ADAPTIVE,
    )
    pinned_chat_model = models.CharField(max_length=80, blank=True, default="")
    pinned_until = models.DateTimeField(null=True, blank=True)
    settings_revision = models.PositiveBigIntegerField(default=0)
    instruction_draft_revision = models.PositiveBigIntegerField(default=0, db_default=0)
    active_instruction_publication = models.ForeignKey(
        "management.BotPolicyPublication",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="active_settings",
        db_constraint=False,
    )
    gemini_model = models.CharField(max_length=80, default="gemini-3.7-flash")
    system_prompt = models.TextField(blank=True, default=DEFAULT_BOT_SYSTEM_PROMPT)
    # Додаткова база знань (правила доставки, оплати, повернень, графік тощо).
    # Підставляється в контекст Gemini поряд з каталогом. Редагується в UI.
    knowledge_base = models.TextField(blank=True, default="")
    # Білий список IGSID відправників (через кому/новий рядок). Якщо список
    # непорожній — відповідаємо лише переліченим. Якщо очистити повністю —
    # відповідаємо всім, хто має право отримати відповідь за правилами Meta.
    allowed_senders = models.TextField(blank=True, default="")
    # Резервний поллінг інбоксу IG. Після Live за замовчуванням ВИМКНЕНО:
    # бот суто event-driven (webhook), без фонових запитів до IG. Можна
    # увімкнути вручну як backstop, якщо webhook раптом не доставляє.
    receive_via_poll = models.BooleanField(default=False)
    # Durable, token-free discovery state for the polling backstop. Graph
    # paging URLs are never stored: only validated conversation ids and the
    # opaque paging.cursors.after value survive cache/process restarts.
    conversation_discovery_ids = models.JSONField(default=list, blank=True)
    conversation_discovery_cursor = models.TextField(blank=True, default="")
    conversation_discovery_page_id = models.CharField(max_length=64, blank=True, default="")
    conversation_discovery_scan_ids = models.JSONField(default=list, blank=True)
    conversation_discovery_cursor_hashes = models.JSONField(default=list, blank=True)
    conversation_discovery_pages_seen = models.PositiveIntegerField(default=0)
    conversation_discovery_updated_at = models.DateTimeField(null=True, blank=True)
    conversation_discovery_completed_at = models.DateTimeField(null=True, blank=True)
    conversation_discovery_lease_token = models.CharField(max_length=64, blank=True, default="")
    conversation_discovery_lease_expires_at = models.DateTimeField(null=True, blank=True)
    # Safe Meta CAPI feedback for IG-only funnel stages. Disabled by default until
    # production match-data/env is verified.
    meta_feedback_enabled = models.BooleanField(default=False)
    meta_feedback_test_event_code = models.CharField(max_length=120, blank=True, default="")

    # Курсор: відповідаємо лише на повідомлення, новіші за цей момент
    # (виставляється у час старту, щоб не відповідати на старий беклог).
    reply_after = models.DateTimeField(null=True, blank=True)

    # Телеметрія
    last_started_at = models.DateTimeField(null=True, blank=True)
    last_stopped_at = models.DateTimeField(null=True, blank=True)
    heartbeat_at = models.DateTimeField(null=True, blank=True)
    last_poll_at = models.DateTimeField(null=True, blank=True)
    last_inbound_at = models.DateTimeField(null=True, blank=True)
    last_reply_at = models.DateTimeField(null=True, blank=True)
    replies_count = models.PositiveIntegerField(default=0)
    last_error = models.TextField(blank=True, default="")
    # Meta may temporarily reject IG Direct messages containing links with
    # Graph 508/2534122. While this circuit is open, eligible customer replies
    # are sent as useful plain text instead of probing the same blocked URL on
    # every message. Payment links remain fail-closed and go to a manager.
    link_send_blocked_until = models.DateTimeField(null=True, blank=True)
    link_send_last_error_at = models.DateTimeField(null=True, blank=True)
    link_send_last_fbtrace_id = models.CharField(max_length=128, blank=True, default="")
    last_gemini_model = models.CharField(max_length=80, blank=True, default="")
    last_gemini_key = models.CharField(max_length=80, blank=True, default="")
    last_gemini_at = models.DateTimeField(null=True, blank=True)
    last_gemini_reasoning_task = models.CharField(max_length=64, blank=True, default="")
    last_gemini_reasoning_level = models.CharField(max_length=16, blank=True, default="")
    last_gemini_policy_version = models.CharField(max_length=32, blank=True, default="")
    last_gemini_thoughts_tokens = models.PositiveIntegerField(default=0)
    last_gemini_candidates_tokens = models.PositiveIntegerField(default=0)
    analysis_reconcile_cursor = models.PositiveBigIntegerField(default=0)
    typed_memory_reconcile_cursor = models.PositiveBigIntegerField(default=0)
    analysis_reconcile_after = models.DateTimeField(default=timezone.now)
    analysis_backfill_enabled = models.BooleanField(default=False)
    # Independent bounded cursor for the one-time durable opt-out migration.
    opt_out_backfill_cursor = models.PositiveBigIntegerField(default=0)
    # Monotonic global permission epoch. Every global stop/start boundary
    # invalidates workers that generated against an older reply policy.
    reply_permission_epoch = models.PositiveBigIntegerField(default=0)

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Instagram bot settings"
        verbose_name_plural = "Instagram bot settings"
        permissions = (
            ("operate_ig_bot", "Can operate Instagram bot"),
            (
                "view_ig_conversation_pii",
                "Can view Instagram conversation personal data",
            ),
            ("manage_ig_payments", "Can manage Instagram payment decisions"),
            ("edit_ig_prompt", "Can edit Instagram bot instructions"),
        )

    @property
    def custom_direct_token(self) -> str:
        return _decrypt_bot_secret(self.custom_direct_token_encrypted)

    @custom_direct_token.setter
    def custom_direct_token(self, value: str) -> None:
        self.custom_direct_token_encrypted = _encrypt_bot_secret(value)

    @property
    def custom_gemini_key(self) -> str:
        return _decrypt_bot_secret(self.custom_gemini_key_encrypted)

    @custom_gemini_key.setter
    def custom_gemini_key(self, value: str) -> None:
        self.custom_gemini_key_encrypted = _encrypt_bot_secret(value)

    @property
    def has_custom_direct_token(self) -> bool:
        return bool(self.custom_direct_token_encrypted)

    @property
    def has_custom_gemini_key(self) -> bool:
        return bool(self.custom_gemini_key_encrypted)

    @property
    def binotel_ai_enabled(self) -> bool:
        """Compatibility alias for callers deployed before the field rename."""
        return self.call_auto_analysis_enabled

    @binotel_ai_enabled.setter
    def binotel_ai_enabled(self, value: bool) -> None:
        self.call_auto_analysis_enabled = value

    def save(self, *args, **kwargs):
        # Keep legacy call sites using the public property names compatible
        # while the physical fields intentionally describe encrypted storage.
        update_fields = kwargs.get("update_fields")
        if update_fields is not None:
            remapped = set(update_fields)
            if "binotel_ai_enabled" in remapped:
                remapped.remove("binotel_ai_enabled")
                remapped.add("call_auto_analysis_enabled")
            if "custom_direct_token" in remapped:
                remapped.remove("custom_direct_token")
                remapped.add("custom_direct_token_encrypted")
            if "custom_gemini_key" in remapped:
                remapped.remove("custom_gemini_key")
                remapped.add("custom_gemini_key_encrypted")
            kwargs["update_fields"] = remapped
        return super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"InstagramBotSettings(enabled={self.is_enabled})"

    @classmethod
    def load(cls) -> "InstagramBotSettings":
        # This singleton is normally a read. The primary-key get-or-create is
        # also idempotent during initial bootstrap, so one reconnect is safe.
        return retry_mysql_read(lambda: cls.objects.get_or_create(pk=1)[0])


class InstagramBotLog(models.Model):
    """Подієвий лог для онлайн-консолі вкладки «Бот»."""

    class Level(models.TextChoices):
        INFO = "info", "info"
        SUCCESS = "success", "success"
        WARNING = "warning", "warning"
        ERROR = "error", "error"

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    level = models.CharField(max_length=10, choices=Level.choices, default=Level.INFO)
    event = models.CharField(max_length=120)
    detail = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-id"]

    def __str__(self) -> str:
        return f"[{self.level}] {self.event}"


class InstagramBotTaskHeartbeat(models.Model):
    """Last observed execution of an operational Instagram task.

    This is intentionally one row per task, rather than an unbounded run log:
    the invariant we need for cron supervision is freshness, not another source
    of high-volume diagnostics.
    """

    task_key = models.CharField(max_length=64, unique=True)
    label = models.CharField(max_length=160)
    expected_interval_seconds = models.PositiveIntegerField()
    stale_after_seconds = models.PositiveIntegerField()
    first_expected_at = models.DateTimeField(auto_now_add=True, db_index=True)
    last_started_at = models.DateTimeField(null=True, blank=True)
    last_succeeded_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_failed_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_duration_ms = models.PositiveIntegerField(default=0)
    consecutive_failures = models.PositiveIntegerField(default=0)
    # Only the exception class is persisted, never an arbitrary provider or
    # customer payload which could contain personal data.
    last_error_kind = models.CharField(max_length=128, blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["task_key"]

    def __str__(self) -> str:
        return f"{self.task_key}: {self.last_succeeded_at or 'not observed'}"


class InstagramBotProcessedMessage(models.Model):
    """Дедуп оброблених вхідних повідомлень за message id (mid)."""

    mid = models.CharField(max_length=255, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return self.mid


class InstagramBotMessage(models.Model):
    """Черга + локальна історія діалогів.

    Вхідні (role=user) кладуться зі статусом pending (webhook або поллінг),
    воркер їх обробляє: будує контекст з останніх рядків цього sender_id,
    генерує відповідь, відправляє і пише рядок role=model (done). Так контекст
    для Gemini зберігається локально — БЕЗ read-запитів до IG.
    """

    class Role(models.TextChoices):
        USER = "user", "user"
        MODEL = "model", "model"
        MANAGER = "manager", "manager"

    class Status(models.TextChoices):
        PENDING = "pending", "pending"
        PROCESSING = "processing", "processing"
        DONE = "done", "done"
        FAILED = "failed", "failed"

    class PrivateMediaState(models.TextChoices):
        NONE = "", "none"
        ACTIVE = "active", "active"
        DELETE_PENDING = "delete_pending", "delete_pending"
        DELETING = "deleting", "deleting"
        DELETE_FAILED = "delete_failed", "delete_failed"
        DELETED = "deleted", "deleted"

    sender_id = models.CharField(max_length=64, db_index=True)
    client = models.ForeignKey(
        "management.IgClient",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="messages",
    )
    role = models.CharField(max_length=8, choices=Role.choices)
    text = models.TextField(blank=True, default="")
    # mid унікальний лише для вхідних; вихідні (model) мають null.
    mid = models.CharField(max_length=255, null=True, blank=True, unique=True)
    # Immutable provenance for the current single-account consumer. Raw Meta
    # IDs remain unchanged for reply-to/echo reconciliation.
    provider_namespace = models.CharField(max_length=128, blank=True, default="", db_default="", db_index=True)
    synthetic_event_key = models.CharField(
        max_length=64, null=True, blank=True, unique=True, db_index=True
    )
    # `message_id`, який Meta повернула на наш Send API. Потрібен, щоб пізніше
    # впізнати власне echo: у медіа-echo тексту немає, і відпечаток по тексту
    # (`_bot_sent_key`) там не працює в принципі. Саме через це 02.08.2026
    # карусель бота була прийнята за повідомлення менеджера, бот став на паузу
    # і замовк для клієнта. Не `unique`: колізія від провайдера не повинна
    # ламати запис історії, а перевірка існування від дублів не страждає.
    provider_message_id = models.CharField(
        max_length=255, blank=True, default="", db_index=True
    )
    # Meta supplies these identities for replies to candidate prompts and
    # quick-reply buttons. They are persisted on ingress so a numeric choice
    # can be checked against the exact prompt generation later.
    reply_to_provider_message_id = models.CharField(
        max_length=255, blank=True, default="", db_index=True
    )
    quick_reply_payload = models.CharField(max_length=1000, blank=True, default="")
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.DONE)
    source = models.CharField(max_length=16, default="webhook")
    # JSON-список URL зображень-вкладень (для мультимодального аналізу Gemini).
    attachments = models.TextField(blank=True, default="")
    # Structured ownership state for attachment bytes. Existing/imported URLs
    # are migrated as metadata-only; only new live webhook rows may progress
    # from pending to an owned storage object before any provider analysis.
    attachment_media = models.JSONField(default=list, blank=True)
    # Durable ingress boundary. Migration defaults existing rows to False;
    # only a post-deploy live webhook (or its exact delayed promotion) enables
    # network capture, including raw-only attachments absent from `attachments`.
    media_capture_eligible = models.BooleanField(default=False)
    private_media_delete_after = models.DateTimeField(
        null=True,
        blank=True,
    )
    private_media_state = models.CharField(
        max_length=20,
        blank=True,
        default=PrivateMediaState.NONE,
        choices=PrivateMediaState.choices,
    )
    private_media_delete_token = models.CharField(max_length=64, blank=True, default="")
    private_media_delete_claimed_at = models.DateTimeField(null=True, blank=True)
    private_media_use_token = models.CharField(max_length=64, blank=True, default="")
    private_media_use_until = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveIntegerField(default=0)
    # Delivery boundary state. Once a provider request has started, an
    # ambiguous result must never be retried automatically (Meta has no
    # idempotency key and may have accepted the request before the timeout).
    send_state = models.CharField(max_length=16, blank=True, default="")
    send_started_at = models.DateTimeField(null=True, blank=True)
    send_completed_at = models.DateTimeField(null=True, blank=True)
    # ЭА.21: ідемпотентний ключ відправки, записаний у ТІЙ САМІЙ умовній
    # транзакції, що ставить `send_state="sending"` — тобто до будь-якого Meta I/O.
    # Унікальність у БД означає, що другий воркер (або та сама строка після
    # рестарту) фізично не може заявити той самий намір відправки вдруге.
    #
    # Чому `null=True`, а не `default=""`: унікальний індекс по порожньому рядку
    # злився б для всіх історичних строк. MariaDB/MySQL допускає багато `NULL` в
    # унікальному індексі — це той самий nullable-active-key паттерн, який уже
    # застосований у recovery.
    #
    # Формат ключа задає `services/ig_send_intent.py`. Э2.2B розширює його
    # revision-ом ходу, а не створює другу таблицю намірів.
    # Унікальність задана окремим `UniqueConstraint`, а не `unique=True`: на
    # SQLite `unique=True` змушує Django перебудувати таблицю, а перебудова
    # ламає тригер `ig_anres_v22_insert_guard`, який посилається на цю таблицю з
    # іншої. Окремий unique-індекс створюється без перебудови і на MariaDB, і на
    # SQLite, тому структурний тестовий шар лишається робочим.
    send_idempotency_key = models.CharField(
        max_length=120, null=True, blank=True, default=None
    )
    # Restricted delivery evidence for the logical reply. Customer-facing
    # alerts contain only counts, provider IDs and the failure boundary.
    delivery_original_text = models.TextField(blank=True, default="")
    delivery_planned_chunk_count = models.PositiveSmallIntegerField(default=0)
    delivery_delivered_chunk_count = models.PositiveSmallIntegerField(default=0)
    delivery_provider_message_ids = models.JSONField(default=list, blank=True)
    delivery_failure_boundary = models.CharField(max_length=64, blank=True, default="")
    # Exact provider model used for this AI-authored message. Historical and
    # deterministic rows intentionally remain blank.
    gemini_model = models.CharField(max_length=80, blank=True, default="")
    # Э2.2: рядок, поглинутий логічним ходом клієнта. Burst із трьох повідомлень
    # дає ОДНУ відповідь: одна строка обробляється, решта позначаються
    # поглинутими. Сирі повідомлення не видаляються — вони лишаються evidence.
    consumed_by_turn = models.ForeignKey(
        "management.IgCustomerTurn",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="absorbed_messages",
        db_constraint=False,
    )
    # Зворотнє посилання на провайдерський запит, що породив цей текст (ЭА.1).
    # Однієї ссылки достатньо: деталі спроб лежать у `GeminiRequestAttempt`.
    gemini_request_id = models.CharField(max_length=40, blank=True, default="", db_index=True)
    # Immutable pre-provider RoutingDecision evidence.  It intentionally lives
    # on the inbound source row so a deadline, fallback or process death cannot
    # erase why a model chain was selected.
    gemini_task_class = models.CharField(max_length=24, blank=True, default="")
    gemini_routing_reason_codes = models.JSONField(default=list, blank=True)
    gemini_routing_policy_version = models.CharField(max_length=32, blank=True, default="")
    gemini_routing_model_chain = models.JSONField(default=list, blank=True)
    gemini_routing_deadline_ms = models.PositiveIntegerField(default=0)
    gemini_routing_lane = models.CharField(max_length=16, blank=True, default="")
    gemini_routing_authority_version = models.CharField(
        max_length=32, blank=True, default=""
    )
    gemini_routing_requires_media = models.BooleanField(default=False)
    gemini_routing_commercial_risk = models.CharField(
        max_length=16, blank=True, default=""
    )
    gemini_routing_mode = models.CharField(max_length=12, blank=True, default="")
    turn_intelligence_artifact = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    # Provider timestamp is separate from the local immutable ingest time.
    # Backfill/recovery may persist old messages today, but the chat must show
    # when Meta says the message was actually created.
    provider_created_at = models.DateTimeField(null=True, blank=True, db_index=True)
    # Час саме захоплення воркером, а не надходження webhook. Потрібен для
    # безпечного reclaim: старе повідомлення могло довго чекати в pending,
    # але щойно перейти в processing.
    processing_started_at = models.DateTimeField(null=True, blank=True, db_index=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["id"]
        indexes = [
            models.Index(fields=["status", "role"]),
            models.Index(fields=["sender_id", "created_at"]),
            models.Index(
                fields=["private_media_delete_after"],
                name="mgmt_igmsg_media_del",
            ),
            models.Index(
                fields=["private_media_use_until"],
                name="mgmt_igmsg_media_use",
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(private_media_state__in=[
                    "", "active", "delete_pending", "deleting",
                    "delete_failed", "deleted",
                ]),
                name="mgmt_igmsg_media_state",
            ),
            # ЭА.21: один намір відправки — одна строка. Стовпець nullable, а
            # NULL не конфліктує в unique-індексі ні на MariaDB, ні на SQLite,
            # тому історичні рядки лишаються валідними без backfill. Свідомо БЕЗ
            # `condition=`: partial unique — не той контракт, на який можна
            # спиратись на MariaDB.
            models.UniqueConstraint(
                fields=["send_idempotency_key"],
                name="mgmt_igmsg_send_intent_unique",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.role}:{self.sender_id}:{self.status}"


class LeadNetwork(models.Model):
    class Kind(models.TextChoices):
        CHAIN_BRAND = "chain_brand", _("Мережа-бренд")
        FRANCHISE = "franchise", _("Франшиза")
        MARKETPLACE = "marketplace", _("Маркетплейс")
        VOENTORG_GROUP = "voentorg_group", _("Група військторгів")
        UNKNOWN = "unknown", _("Невідомо")

    class Policy(models.TextChoices):
        BLOCK_NO_COLLAB = "block_no_collab", _("Не співпрацює — пропускати")
        BLOCK_IRRELEVANT = "block_irrelevant", _("Не наш профіль — пропускати")
        APPLY_KNOWN_VERDICT = "apply_known_verdict", _("Застосувати вердикт мережі")
        RECHECK_EACH = "recheck_each", _("Перевіряти кожну точку")
        CUSTOM_PRINT_ONLY = "custom_print_only", _("Лише кастом-друк")
        NEEDS_REVIEW = "needs_review", _("Потребує рішення")
        PRIORITY_TARGET = "priority_target", _("Пріоритетна ціль")

    canonical_name = models.CharField(_("Канонічна назва"), max_length=255)
    slug = models.SlugField(_("Slug"), max_length=255, unique=True)
    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.UNKNOWN)
    policy = models.CharField(max_length=24, choices=Policy.choices, default=Policy.NEEDS_REVIEW, db_index=True)
    policy_params = models.JSONField(default=dict, blank=True)
    extra_instructions = models.TextField(blank=True)
    collaboration_evidence = models.CharField(max_length=10, blank=True)
    confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="confirmed_networks", db_constraint=False,
    )
    confirmed_at = models.DateTimeField(null=True, blank=True)
    suggested_by_ai = models.BooleanField(default=False)
    ai_rationale = models.TextField(blank=True)
    members_count = models.PositiveIntegerField(default=0)
    checked_count = models.PositiveIntegerField(default=0)
    skipped_count = models.PositiveIntegerField(default=0)
    tokens_saved = models.PositiveBigIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Мережа лідів")
        verbose_name_plural = _("Мережі лідів")
        ordering = ["-updated_at"]

    @property
    def is_confirmed(self) -> bool:
        return self.confirmed_by_id is not None

    def __str__(self):
        return f"LeadNetwork({self.canonical_name})"


class NetworkAlias(models.Model):
    class KeyType(models.TextChoices):
        NAME = "name", _("Назва")
        WEBSITE = "website", _("Сайт")
        INSTAGRAM = "instagram", _("Instagram")

    network = models.ForeignKey(LeadNetwork, on_delete=models.CASCADE, related_name="aliases", db_constraint=False)
    key_type = models.CharField(max_length=12, choices=KeyType.choices)
    key_value = models.CharField(max_length=500, db_index=True)
    source = models.CharField(max_length=8, default="auto")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("Алиас мережі")
        verbose_name_plural = _("Алиаси мереж")
        constraints = [models.UniqueConstraint(fields=["key_type", "key_value"], name="uniq_network_alias_key")]

    def __str__(self):
        return f"NetworkAlias({self.key_type}={self.key_value})"


class LeadCheckerSettings(models.Model):
    singleton_key = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    gemini_api_key = models.CharField(_("Ключ Gemini (опц.)"), max_length=255, blank=True)
    model_chain = models.CharField(_("Цепочка моделей (csv)"), max_length=255, blank=True)
    requests_per_minute = models.PositiveIntegerField(_("Запитів за хвилину"), default=8)
    auto_recheck = models.BooleanField(_("Авто-чекінг при відновленні квоти"), default=False)
    auto_recheck_batch = models.PositiveIntegerField(_("Розмір батчу авто-чекінгу"), default=25)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Налаштування чекера")
        verbose_name_plural = _("Налаштування чекера")

    @classmethod
    def load(cls) -> "LeadCheckerSettings":
        obj, _created = cls.objects.get_or_create(singleton_key=1)
        return obj

    def __str__(self):
        return "LeadCheckerSettings"


class GeminiModelQuotaUsage(models.Model):
    """Локальний облік квоти пари (ключ, модель) за добу Pacific (ЭБ.4).

    **Навіщо власний рахівник.** У free-tier ліміти оголошені на пару
    (проєкт, модель): 3.7-flash — 5 RPM / 250K TPM / **20 RPD**, 3.6-flash і
    3.5-flash так само, 3.5-flash-lite — 15 RPM / 250K TPM / **500 RPD**. Кожен
    ключ — окремий проєкт, тобто шість незалежних наборів цих бюджетів.

    До цього пул дізнавався про виснаження **тільки з 429**, тобто ціною
    провального ходу: клієнт чекав, ключ ішов у кулдаун, і лише тоді бралася
    наступна пара. Двадцять запитів на добу означають, що на найкращій моделі
    таких «дізнавань» за день може бути більше, ніж самих корисних відповідей.

    Рахівник — **дорадчий**, провайдер завжди головніший: квоту може витратити
    інший процес або інша сесія, тому реальний 429 закриває пару незалежно від
    локальних цифр. Зворотна помилка дешева: ми лише раніше перейдемо на іншу
    пару.
    """

    key_name = models.CharField(max_length=40, db_index=True)
    model = models.CharField(max_length=80, db_index=True)
    # Доба Pacific: саме за нею Google скидає RPD (див. `next_midnight_pt`).
    day_date = models.DateField(db_index=True)
    requests = models.PositiveIntegerField(default=0)
    tokens = models.PositiveBigIntegerField(default=0)
    # Ковзне вікно хвилини для RPM/TPM.
    minute_started_at = models.DateTimeField(null=True, blank=True)
    minute_requests = models.PositiveIntegerField(default=0)
    minute_tokens = models.PositiveBigIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Витрата квоти Gemini")
        verbose_name_plural = _("Витрати квоти Gemini")
        constraints = [
            models.UniqueConstraint(
                fields=["key_name", "model", "day_date"],
                name="gemini_quota_pair_day",
            ),
        ]
        indexes = [
            models.Index(fields=["day_date", "model"], name="gemini_quota_day_model"),
        ]

    def __str__(self):
        return f"GeminiModelQuotaUsage({self.key_name}/{self.model}@{self.day_date})"


class GeminiQuotaProfileQuerySet(models.QuerySet):
    """Application-level append-only boundary for immutable quota profiles."""

    @staticmethod
    def _reject_mutation():
        raise ValidationError("GeminiQuotaProfile is append-only.")

    def update(self, **kwargs):
        self._reject_mutation()

    def delete(self):
        self._reject_mutation()

    def bulk_update(self, objs, fields, batch_size=None):
        self._reject_mutation()

    def bulk_create(self, objs, *args, **kwargs):
        if kwargs.get("update_conflicts") or kwargs.get("update_fields"):
            self._reject_mutation()
        return super().bulk_create(objs, *args, **kwargs)


class GeminiQuotaProfile(models.Model):
    """Immutable observed quota contract for one Gemini model version.

    Runtime activation is deliberately outside this additive schema slice.
    Profiles contain only non-secret model limits and provenance labels; they
    never contain a credential, Google project id, provider body or prompt.
    """

    class Source(models.TextChoices):
        OWNER_OBSERVED = "owner_observed", _("Підтверджено власником")
        PROVIDER_DOCS = "provider_docs", _("Документація провайдера")
        PROVIDER_ERROR = "provider_error", _("Структурована помилка провайдера")
        ADMIN = "admin", _("Версія адміністратора")

    profile_version = models.CharField(max_length=40)
    model = models.CharField(max_length=80)
    rpm_limit = models.PositiveIntegerField()
    input_tpm_limit = models.PositiveBigIntegerField()
    rpd_limit = models.PositiveIntegerField()
    permit_limit = models.PositiveSmallIntegerField()
    estimator_version = models.CharField(max_length=32)
    source = models.CharField(max_length=24, choices=Source.choices)
    source_reference = models.CharField(max_length=120, blank=True, default="")
    observed_at = models.DateTimeField()
    effective_from = models.DateTimeField()
    effective_until = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    objects = GeminiQuotaProfileQuerySet.as_manager()

    class Meta:
        verbose_name = _("Профіль квоти Gemini")
        verbose_name_plural = _("Профілі квот Gemini")
        base_manager_name = "objects"
        default_manager_name = "objects"
        constraints = [
            models.UniqueConstraint(
                fields=["profile_version", "model"],
                name="gem_qprof_version_model_uniq",
            ),
            models.CheckConstraint(
                condition=models.Q(rpm_limit__gt=0),
                name="gem_qprof_rpm_gt_zero",
            ),
            models.CheckConstraint(
                condition=models.Q(input_tpm_limit__gt=0),
                name="gem_qprof_tpm_gt_zero",
            ),
            models.CheckConstraint(
                condition=models.Q(rpd_limit__gt=0),
                name="gem_qprof_rpd_gt_zero",
            ),
            models.CheckConstraint(
                condition=models.Q(permit_limit__gt=0),
                name="gem_qprof_permit_gt_zero",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(effective_until__isnull=True)
                    | models.Q(effective_until__gt=models.F("effective_from"))
                ),
                name="gem_qprof_effective_window",
            ),
        ]
        indexes = [
            models.Index(
                fields=["model", "effective_from"],
                name="gem_qprof_model_effective",
            ),
        ]

    def __str__(self):
        return f"GeminiQuotaProfile({self.profile_version}/{self.model})"

    def save(self, *args, **kwargs):
        if self.pk is not None or not self._state.adding:
            raise ValidationError("GeminiQuotaProfile is append-only.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("GeminiQuotaProfile is append-only.")


class GeminiQuotaStateQuerySet(models.QuerySet):
    _IDENTITY_FIELDS = frozenset({
        "project_identity", "model", "quota_profile", "quota_profile_id",
    })

    def update(self, **kwargs):
        if self._IDENTITY_FIELDS.intersection(kwargs):
            raise ValidationError(
                "GeminiQuotaState identity/profile fields require the contract boundary."
            )
        return super().update(**kwargs)

    def bulk_update(self, objs, fields, batch_size=None):
        if self._IDENTITY_FIELDS.intersection(fields):
            raise ValidationError(
                "GeminiQuotaState identity/profile fields require the contract boundary."
            )
        return super().bulk_update(objs, fields, batch_size=batch_size)

    def bulk_create(self, objs, *args, **kwargs):
        from management.services.gemini_accounting_contract import (
            validate_quota_state_contract,
        )

        for obj in objs:
            validate_quota_state_contract(obj)
        return super().bulk_create(objs, *args, **kwargs)

    def _rotate_profile(self, **kwargs):
        """Private write primitive; public callers use the audited contract."""
        return super().update(**kwargs)


class GeminiQuotaState(models.Model):
    """Future V2 coordination row keyed by stable project identity and model.

    The table is intentionally dormant until a later shadow-runtime release.
    Missing rows therefore mean "no V2 traffic observed", not an implicit
    provider-health assertion.
    """

    class AccountingStatus(models.TextChoices):
        UNKNOWN = "unknown", _("Невідомо")
        AVAILABLE = "available", _("Доступно за локальним станом")
        BLOCKED = "blocked", _("Заблоковано провайдером")
        DEGRADED = "degraded", _("Облік деградував")

    project_identity = models.CharField(max_length=80)
    model = models.CharField(max_length=80)
    quota_profile = models.ForeignKey(
        GeminiQuotaProfile,
        on_delete=models.PROTECT,
        related_name="quota_states",
    )
    pacific_day = models.DateField(null=True, blank=True)
    rpd_reserved = models.PositiveIntegerField(default=0)
    rpd_dispatched = models.PositiveIntegerField(default=0)
    rpd_uncertain = models.PositiveIntegerField(default=0)
    in_flight_count = models.PositiveSmallIntegerField(default=0)
    next_permit_expiry_at = models.DateTimeField(null=True, blank=True)
    # Metric -> bounded structured block. Runtime code must never place a raw
    # provider response in this field.
    provider_blocks = models.JSONField(default=dict, blank=True)
    external_usage_suspected = models.BooleanField(default=False)
    accounting_status = models.CharField(
        max_length=12,
        choices=AccountingStatus.choices,
        default=AccountingStatus.UNKNOWN,
    )
    last_success_at = models.DateTimeField(null=True, blank=True)
    last_failure_at = models.DateTimeField(null=True, blank=True)
    last_failure_kind = models.CharField(max_length=32, blank=True, default="")
    last_http_code = models.PositiveSmallIntegerField(null=True, blank=True)
    last_latency_ms = models.PositiveIntegerField(default=0)
    latency_ewma_ms = models.PositiveIntegerField(default=0)
    revision = models.PositiveBigIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    objects = GeminiQuotaStateQuerySet.as_manager()

    class Meta:
        verbose_name = _("Стан квоти Gemini V2")
        verbose_name_plural = _("Стани квот Gemini V2")
        constraints = [
            models.UniqueConstraint(
                fields=["project_identity", "model"],
                name="gem_qstate_project_model_uniq",
            ),
        ]
        indexes = [
            models.Index(
                fields=["accounting_status", "updated_at"],
                name="gem_qstate_status_updated",
            ),
            models.Index(
                fields=["next_permit_expiry_at"],
                name="gem_qstate_permit_expiry",
            ),
        ]

    def __str__(self):
        return f"GeminiQuotaState({self.project_identity}/{self.model})"

    def clean(self):
        super().clean()
        from management.services.gemini_accounting_contract import (
            validate_quota_state_contract,
        )

        validate_quota_state_contract(self)

    def save(self, *args, **kwargs):
        from management.services.gemini_accounting_contract import (
            validate_quota_state_contract,
        )

        validate_quota_state_contract(self)
        return super().save(*args, **kwargs)


class GeminiRequestQuerySet(models.QuerySet):
    def update(self, **kwargs):
        from management.services.gemini_accounting_contract import (
            REQUEST_MUTABLE_FIELDS,
        )

        if set(kwargs) - REQUEST_MUTABLE_FIELDS:
            raise ValidationError(
                "GeminiRequest update contains immutable routing/lineage fields."
            )
        return super().update(**kwargs)

    def bulk_update(self, objs, fields, batch_size=None):
        from management.services.gemini_accounting_contract import (
            REQUEST_MUTABLE_FIELDS,
        )

        if set(fields) - REQUEST_MUTABLE_FIELDS:
            raise ValidationError(
                "GeminiRequest bulk update contains immutable routing/lineage fields."
            )
        return super().bulk_update(objs, fields, batch_size=batch_size)

    def bulk_create(self, objs, *args, **kwargs):
        from management.services.gemini_accounting_contract import (
            validate_request_contract,
        )
        from management.services.gemini_accounting_runtime import validate_revision_request_creation

        for obj in objs:
            validate_request_contract(obj)
            validate_revision_request_creation(obj)
        return super().bulk_create(objs, *args, **kwargs)


class GeminiRequest(models.Model):
    """Sanitized parent graph for one logical Gemini request.

    Candidate plans contain only safe project identities and model names. The
    policy manifest contains only bounded IDs, versions, hashes and omission
    codes. The schema has no field for credentials, prompt bodies, customer
    text or raw provider bodies.
    """

    class AccountingMode(models.TextChoices):
        OFF = "off", _("Вимкнено")
        SHADOW = "shadow", _("Тіньовий облік")
        ENFORCED = "enforced", _("Обов'язковий облік")
        EMERGENCY = "emergency", _("Аварійний Lite")

    request_id = models.CharField(max_length=40, unique=True)
    parent_request = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="child_requests",
    )
    lane = models.CharField(max_length=16, blank=True, default="")
    task_class = models.CharField(max_length=24, blank=True, default="")
    reasoning_task = models.CharField(max_length=40, blank=True, default="")
    logical_turn_id = models.CharField(max_length=64, blank=True, default="")
    source_message_id = models.PositiveBigIntegerField(null=True, blank=True)
    source_execution_key = models.CharField(max_length=64, blank=True, default="", db_default="")
    client_id = models.PositiveBigIntegerField(null=True, blank=True)
    recovery_job_id = models.PositiveBigIntegerField(null=True, blank=True)
    reply_message_id = models.PositiveBigIntegerField(null=True, blank=True)
    routing_policy_version = models.CharField(max_length=32, blank=True, default="")
    accounting_policy_version = models.CharField(max_length=32, blank=True, default="")
    quota_profile_version = models.CharField(max_length=40, blank=True, default="")
    authority_snapshot_version = models.CharField(max_length=32, blank=True, default="")
    routing_mode = models.CharField(max_length=12, blank=True, default="")
    commercial_risk = models.CharField(max_length=16, blank=True, default="")
    requires_media_reasoning = models.BooleanField(default=False)
    candidate_plan = models.JSONField(default=list, blank=True)
    candidate_plan_digest = models.CharField(max_length=64, blank=True, default="")
    policy_manifest = models.JSONField(default=dict, db_default={}, blank=True)
    candidate_outcomes = models.JSONField(default=dict, blank=True)
    deadline_ms = models.PositiveIntegerField(default=0)
    deadline_at = models.DateTimeField(null=True, blank=True)
    accounting_mode = models.CharField(
        max_length=12,
        choices=AccountingMode.choices,
        default=AccountingMode.OFF,
    )
    terminal_resolution = models.CharField(max_length=24, blank=True, default="")
    terminal_reason = models.CharField(max_length=48, blank=True, default="")
    winner_attempt = models.ForeignKey(
        "management.GeminiRequestAttempt",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="won_gemini_requests",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    provider_phase_started_at = models.DateTimeField(null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    objects = GeminiRequestQuerySet.as_manager()

    class Meta:
        verbose_name = _("Запит Gemini")
        verbose_name_plural = _("Запити Gemini")
        indexes = [
            models.Index(fields=["lane", "-created_at"], name="gem_req_lane_created"),
            models.Index(
                fields=["logical_turn_id", "-created_at"],
                name="gem_req_turn_created",
            ),
            models.Index(
                fields=["terminal_resolution", "deadline_at"],
                name="gem_req_resolution_deadline",
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["source_message_id", "lane", "source_execution_key"],
                name="gem_req_source_exec_uniq",
            ),
        ]

    def __str__(self):
        return f"GeminiRequest({self.request_id}:{self.terminal_resolution or 'open'})"

    def clean(self):
        super().clean()
        from management.services.gemini_accounting_contract import (
            validate_request_contract,
        )

        validate_request_contract(self)

    def save(self, *args, **kwargs):
        from management.services.gemini_accounting_contract import (
            validate_request_contract,
        )

        validate_request_contract(self)
        if self._state.adding:
            from management.services.gemini_accounting_runtime import validate_revision_request_creation
            validate_revision_request_creation(self)
        return super().save(*args, **kwargs)


class GeminiKeyState(models.Model):
    """Стан одного Gemini-ключа (проекту) для менеджера пулів: кулдаун за квотою,
    лічильники, причина. Квота в Gemini рахується на проект, тож трекаємо per-key."""
    key_name = models.CharField(max_length=40, unique=True)
    role_hint = models.CharField(max_length=20, blank=True)
    cooldown_until = models.DateTimeField(null=True, blank=True, db_index=True)
    cooldown_scope = models.CharField(max_length=10, blank=True)  # minute|day|topup
    # ЭБ.2: квота free-tier считается на пару (проект, МОДЕЛЬ), а не на проект.
    # До этого 429 по дневному лимиту 3.7-flash закрывал ключ целиком — вместе с
    # 3.6, 3.5 и lite, у которых на том же проекте лимит в разы больше. Шесть
    # ключей исчерпывались за день по самой дефицитной модели, и живой ответ
    # оставался без ключа. Здесь — {model: iso-timestamp} до конца кулдауна.
    model_cooldowns = models.JSONField(default=dict, blank=True)
    last_status = models.CharField(max_length=20, blank=True)
    last_429_at = models.DateTimeField(null=True, blank=True)
    last_ok_at = models.DateTimeField(null=True, blank=True)
    requests_today = models.PositiveIntegerField(default=0)
    day_date = models.DateField(null=True, blank=True)
    last_error = models.TextField(blank=True)
    last_probe_at = models.DateTimeField(null=True, blank=True)
    last_probe_status = models.CharField(max_length=32, blank=True)
    last_probe_model = models.CharField(max_length=80, blank=True)
    last_probe_latency_ms = models.PositiveIntegerField(default=0)
    last_probe_finish_reason = models.CharField(max_length=32, blank=True)
    last_probe_http_code = models.PositiveSmallIntegerField(null=True, blank=True)
    last_probe_error = models.CharField(max_length=120, blank=True)
    lease_token = models.CharField(max_length=40, blank=True, default="")
    lease_until = models.DateTimeField(null=True, blank=True, db_index=True)
    lease_role = models.CharField(max_length=20, blank=True, default="")
    last_http_code = models.PositiveSmallIntegerField(null=True, blank=True)
    last_failure_kind = models.CharField(max_length=32, blank=True, default="")
    consecutive_failures = models.PositiveSmallIntegerField(default=0)
    latency_ewma_ms = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Стан Gemini-ключа")
        verbose_name_plural = _("Стани Gemini-ключів")

    @classmethod
    def get(cls, key_name: str) -> "GeminiKeyState":
        obj, _created = cls.objects.get_or_create(key_name=key_name)
        return obj

    def __str__(self):
        return f"GeminiKeyState({self.key_name})"


class GeminiModelState(models.Model):
    """Cross-process circuit state for one Gemini model name."""

    model_name = models.CharField(max_length=80, unique=True)
    circuit_until = models.DateTimeField(null=True, blank=True, db_index=True)
    circuit_reason = models.CharField(max_length=32, blank=True, default="")
    transient_failures = models.PositiveSmallIntegerField(default=0)
    last_failure_project = models.CharField(max_length=80, blank=True, default="")
    last_failure_at = models.DateTimeField(null=True, blank=True)
    last_ok_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Стан моделі Gemini")
        verbose_name_plural = _("Стани моделей Gemini")

    def __str__(self):
        return f"GeminiModelState({self.model_name})"


class GeminiRequestAttemptQuerySet(models.QuerySet):
    def update(self, **kwargs):
        from management.services.gemini_accounting_contract import (
            ATTEMPT_MUTABLE_FIELDS,
        )

        if set(kwargs) - ATTEMPT_MUTABLE_FIELDS:
            raise ValidationError(
                "GeminiRequestAttempt update contains immutable identity fields."
            )
        return super().update(**kwargs)

    def bulk_update(self, objs, fields, batch_size=None):
        from management.services.gemini_accounting_contract import (
            ATTEMPT_MUTABLE_FIELDS,
        )

        if set(fields) - ATTEMPT_MUTABLE_FIELDS:
            raise ValidationError(
                "GeminiRequestAttempt bulk update contains immutable identity fields."
            )
        return super().bulk_update(objs, fields, batch_size=batch_size)

    def bulk_create(self, objs, *args, **kwargs):
        from management.services.gemini_accounting_contract import (
            validate_attempt_contract,
        )

        for obj in objs:
            validate_attempt_contract(obj)
        return super().bulk_create(objs, *args, **kwargs)


class GeminiRequestAttempt(models.Model):
    """Redacted provider-attempt evidence, never customer or provider bodies."""

    class FsmState(models.TextChoices):
        LEGACY = "legacy", _("Legacy telemetry")
        PLANNED = "planned", _("Заплановано")
        RESERVED = "reserved", _("Квоту зарезервовано")
        PROVIDER_STARTED = "provider_started", _("Виклик провайдера розпочато")
        SUCCEEDED = "succeeded", _("Успішно")
        FAILED = "failed", _("Помилка")
        TIMEOUT_AMBIGUOUS = "timeout_ambiguous", _("Неоднозначний таймаут")
        SUCCEEDED_LATE = "succeeded_late", _("Пізній успіх")
        CANCELLED_PRE_DISPATCH = "cancelled_pre_dispatch", _("Скасовано до виклику")

    class ShadowDecision(models.TextChoices):
        UNKNOWN = "unknown", _("Невідомо")
        ALLOW = "allow", _("Було б дозволено")
        DENY = "deny", _("Було б відхилено")

    request_id = models.CharField(max_length=40, db_index=True)
    request_graph = models.ForeignKey(
        GeminiRequest,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="attempts",
    )
    role = models.CharField(max_length=20)
    key_name = models.CharField(max_length=40)
    project_group = models.CharField(max_length=80, blank=True, default="")
    project_identity = models.CharField(max_length=80, blank=True, default="")
    model = models.CharField(max_length=80)
    outcome = models.CharField(max_length=24)
    fsm_state = models.CharField(
        max_length=24,
        choices=FsmState.choices,
        default=FsmState.LEGACY,
    )
    quota_profile = models.ForeignKey(
        GeminiQuotaProfile,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="attempts",
    )
    accounting_mode = models.CharField(max_length=12, blank=True, default="")
    shadow_decision = models.CharField(
        max_length=8,
        choices=ShadowDecision.choices,
        default=ShadowDecision.UNKNOWN,
    )
    shadow_deny_reason = models.CharField(max_length=32, blank=True, default="")
    failure_kind = models.CharField(max_length=32, blank=True, default="")
    http_code = models.PositiveSmallIntegerField(null=True, blank=True)
    provider_reason = models.CharField(max_length=80, blank=True, default="")
    decision = models.CharField(max_length=48, blank=True, default="")
    latency_ms = models.PositiveIntegerField(default=0)
    remaining_deadline_ms = models.PositiveIntegerField(default=0)
    prompt_tokens = models.PositiveIntegerField(default=0)
    thoughts_tokens = models.PositiveIntegerField(default=0)
    candidates_tokens = models.PositiveIntegerField(default=0)
    total_tokens = models.PositiveBigIntegerField(default=0)
    estimated_prompt_tokens = models.PositiveBigIntegerField(default=0)
    reserved_prompt_tokens = models.PositiveBigIntegerField(default=0)
    error_detail = models.CharField(max_length=120, blank=True, default="")
    # Lineage ходу (ЭА.1): без цих полів ланцюг «вхідне → спроби → holding →
    # recovery → receipt» не побудувати запитом, а значить не довести жодну
    # правку спаму технічних вибачень. Значення передаються зверху, з шару, що
    # знає контекст ходу, і НЕ вгадуються тут.
    logical_turn_id = models.CharField(max_length=64, blank=True, default="")
    source_message_id = models.PositiveBigIntegerField(null=True, blank=True)
    client_id = models.PositiveBigIntegerField(null=True, blank=True)
    # live | holding | recovery | analysis | metadata_probe | followup
    lane = models.CharField(max_length=16, blank=True, default="")
    attempt_index = models.PositiveSmallIntegerField(default=0)
    candidate_index = models.PositiveSmallIntegerField(default=0)
    # Кандидат, який НЕ викликали, теж має рядок: інакше «шість страхуючих
    # ключів» не можна ні підтвердити, ні опровергнути.
    not_attempted_reason = models.CharField(max_length=24, blank=True, default="")
    incident_id = models.PositiveBigIntegerField(null=True, blank=True)
    recovery_job_id = models.PositiveBigIntegerField(null=True, blank=True)
    reply_message_id = models.PositiveBigIntegerField(null=True, blank=True)
    reserved_at = models.DateTimeField(null=True, blank=True)
    reservation_expires_at = models.DateTimeField(null=True, blank=True)
    provider_started_at = models.DateTimeField(null=True, blank=True)
    dispatch_pacific_day = models.DateField(null=True, blank=True)
    permit_expires_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    settled_at = models.DateTimeField(null=True, blank=True)
    reservation_released_at = models.DateTimeField(null=True, blank=True)
    permit_released_at = models.DateTimeField(null=True, blank=True)
    provider_quota_metric = models.CharField(max_length=16, blank=True, default="")
    provider_quota_id = models.CharField(max_length=120, blank=True, default="")
    provider_quota_dimensions = models.JSONField(default=dict, blank=True)
    provider_retry_after_seconds = models.PositiveIntegerField(default=0)
    provider_block_until = models.DateTimeField(null=True, blank=True)
    winner_claimed = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    objects = GeminiRequestAttemptQuerySet.as_manager()

    class Meta:
        ordering = ["-id"]
        indexes = [
            models.Index(fields=["request_id", "-id"], name="gemini_attempt_request"),
            models.Index(fields=["role", "-id"], name="gemini_attempt_role"),
            models.Index(fields=["source_message_id", "-id"], name="gemini_attempt_source"),
            models.Index(fields=["client_id", "-id"], name="gemini_attempt_client"),
            models.Index(fields=["lane", "-id"], name="gemini_attempt_lane"),
            models.Index(
                fields=["project_identity", "model", "provider_started_at"],
                name="gem_att_pair_started",
            ),
            models.Index(
                fields=["project_identity", "model", "dispatch_pacific_day", "fsm_state"],
                name="gem_att_pair_day_state",
            ),
            models.Index(
                fields=["fsm_state", "reservation_expires_at"],
                name="gem_att_state_reserve",
            ),
            models.Index(
                fields=["fsm_state", "permit_expires_at"],
                name="gem_att_state_permit",
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["request_graph", "attempt_index"],
                name="gem_att_graph_index_unique",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(provider_started_at__isnull=True)
                    | models.Q(dispatch_pacific_day__isnull=False)
                ),
                name="gem_att_started_has_day",
            ),
        ]

    def __str__(self):
        return f"GeminiRequestAttempt({self.request_id}:{self.outcome})"

    def clean(self):
        super().clean()
        from management.services.gemini_accounting_contract import (
            validate_attempt_contract,
        )

        validate_attempt_contract(self)

    def save(self, *args, **kwargs):
        from management.services.gemini_accounting_contract import (
            validate_attempt_contract,
        )

        validate_attempt_contract(self)
        return super().save(*args, **kwargs)


class LeadCheckJob(models.Model):
    class Status(models.TextChoices):
        RUNNING = "running", _("Працює")
        PAUSED = "paused", _("Пауза")
        STOPPED = "stopped", _("Зупинено")
        COMPLETED = "completed", _("Завершено")
        FAILED = "failed", _("Помилка")

    class Scope(models.TextChoices):
        UNCHECKED = "unchecked", _("Тільки неперевірені")
        ALL = "all", _("Усі (перепровірка)")
        BY_CITY = "by_city", _("За містом")
        BY_BAND = "by_band", _("За смугою вердикту")

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="lead_check_jobs", verbose_name=_("Створив"),
    )
    is_auto = models.BooleanField(_("Авто-сесія (cron)"), default=False, db_index=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.RUNNING, db_index=True)
    scope = models.CharField(max_length=20, choices=Scope.choices, default=Scope.UNCHECKED)
    city = models.CharField(max_length=120, blank=True)
    band = models.CharField(max_length=10, blank=True)
    target_limit = models.PositiveIntegerField(default=0)
    requests_per_minute = models.PositiveIntegerField(default=8)
    total_selected = models.PositiveIntegerField(default=0)
    processed = models.PositiveIntegerField(default=0)
    scored = models.PositiveIntegerField(default=0)
    errors = models.PositiveIntegerField(default=0)
    fit_count = models.PositiveIntegerField(default=0)
    maybe_count = models.PositiveIntegerField(default=0)
    unfit_count = models.PositiveIntegerField(default=0)
    cursor_id = models.PositiveIntegerField(default=0)
    current_lead_id = models.PositiveIntegerField(null=True, blank=True)
    next_step_not_before = models.DateTimeField(null=True, blank=True, db_index=True)
    is_step_in_progress = models.BooleanField(default=False, db_index=True)
    last_step_started_at = models.DateTimeField(null=True, blank=True)
    avg_seconds = models.FloatField(default=0.0)
    tokens_total = models.PositiveBigIntegerField(default=0)
    last_error = models.TextField(blank=True)
    stop_reason_code = models.CharField(max_length=64, blank=True)
    started_at = models.DateTimeField(auto_now_add=True, db_index=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Сесія чекера")
        verbose_name_plural = _("Сесії чекера")
        ordering = ["-started_at"]
        indexes = [models.Index(fields=["status", "-started_at"], name="mgmt_check_status_dt")]

    def __str__(self):
        return f"CheckJob#{self.id} ({self.status})"


class LeadCheckRuntimeLock(models.Model):
    singleton_key = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    active_job = models.ForeignKey(
        LeadCheckJob, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Lock чекера")
        verbose_name_plural = _("Lock-и чекера")

    def __str__(self):
        return f"CheckLock(active_job={self.active_job_id or 'none'})"


class LeadAICheck(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", _("В черзі")
        PROCESSING = "processing", _("Обробка")
        DONE = "done", _("Готово")
        ERROR = "error", _("Помилка")

    lead = models.ForeignKey(ManagementLead, on_delete=models.CASCADE, related_name="ai_checks")
    job = models.ForeignKey(LeadCheckJob, on_delete=models.SET_NULL, null=True, blank=True, related_name="checks")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING, db_index=True)
    overall_score = models.PositiveSmallIntegerField(null=True, blank=True)
    criteria = models.JSONField(default=list, blank=True)
    verdict_category = models.CharField(max_length=40, blank=True)
    partnership_fit = models.JSONField(default=list, blank=True)
    confidence = models.CharField(max_length=10, blank=True)
    verdict_band = models.CharField(max_length=10, blank=True, db_index=True)
    collaboration_evidence = models.CharField(max_length=10, blank=True, db_index=True)
    signals = models.JSONField(default=dict, blank=True)
    brand_summary = models.TextField(blank=True)
    audience_guess = models.TextField(blank=True)
    instagram_url = models.CharField(max_length=300, blank=True)
    comment = models.TextField(blank=True)
    recommendation = models.TextField(blank=True)
    sources = models.JSONField(default=list, blank=True)
    website_fetched = models.BooleanField(default=False)
    model_used = models.CharField(max_length=60, blank=True)
    tokens = models.JSONField(default=dict, blank=True)
    error = models.TextField(blank=True)
    checked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="lead_ai_checks",
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = _("AI-перевірка ліда")
        verbose_name_plural = _("AI-перевірки лідів")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["lead", "-created_at"], name="mgmt_aicheck_lead_dt"),
            models.Index(fields=["status", "-created_at"], name="mgmt_aicheck_status_dt"),
        ]

    def __str__(self):
        return f"AICheck#{self.id} lead={self.lead_id} score={self.overall_score}"


# ===========================================================================
# Instagram Direct bot — апгрейд (картки клієнтів, угоди, інструкції, сирі
# події). Моделі винесені в окремий модуль ig_bot_models.py, щоб не роздувати
# цей файл. Імпорт у кінці гарантує реєстрацію моделей під app_label.
# ===========================================================================
from .ig_bot_models import *  # noqa: E402,F401,F403
from .ig_journey_models import IgConversationRouteDecision  # noqa: E402,F401
