import re
import json
from urllib.parse import urlparse

from django import forms
from django.conf import settings
from django.db import models
from django.forms import BaseInlineFormSet, inlineformset_factory

from dtf.utils import (
    ALLOWED_HELP_EXTS,
    build_safe_upload_name,
    get_limits,
    normalize_phone,
    validate_uploaded_file,
)
from storefront.custom_print_config import PRODUCT_MATRIX, ZONE_LABELS, build_placement_specs, normalize_custom_print_snapshot, normalize_hoodie_included_pricing

from .models import (
    BlogCategory,
    BlogMediaAsset,
    BlogPost,
    CustomPrintBusinessKind,
    Product,
    ProductFitOption,
    Category,
    CustomPrintClientKind,
    CustomPrintContactChannel,
    CustomPrintLead,
    CustomPrintLeadAttachment,
    CustomPrintModerationStatus,
    CustomPrintProductType,
    CustomPrintServiceKind,
    CustomPrintSizeMode,
    PrintProposal,
    PushNotificationCampaign,
)


def _https_url_formfield(db_field, **kwargs):
    if isinstance(db_field, models.URLField):
        kwargs["assume_scheme"] = "https"
    return db_field.formfield(**kwargs)


# ✅ Виджет с поддержкой множественной загрузки


class MultiFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True

# ✅ Поле, умеющее принимать список файлов (или пусто) без ошибки "No file was submitted"


class MultiFileField(forms.FileField):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("widget", MultiFileInput())
        super().__init__(*args, **kwargs)

    def clean(self, data, initial=None):
        single_file_clean = super().clean

        # Пустое значение — это ок для required=False
        if not data:
            if self.required:
                return single_file_clean(data, initial)
            return []
        # Если пришёл список/кортеж — валидируем каждый файл как обычный FileField
        if isinstance(data, (list, tuple)):
            cleaned = []
            errors = []
            for f in data:
                try:
                    cleaned.append(single_file_clean(f, initial))
                except forms.ValidationError as e:
                    errors.extend(e.error_list)
            if errors:
                raise forms.ValidationError(errors)
            return cleaned
        # Одиночный файл (на случай, если браузер не поддерживает multiple)
        return [single_file_clean(data, initial)]


class CustomPrintLeadForm(forms.Form):
    PLACEMENT_CHOICES = (
        ("front", "Спереду"),
        ("back", "На спині"),
        ("sleeve", "На рукаві"),
        ("custom", "Інший варіант"),
    )

    service_kind = forms.ChoiceField(choices=CustomPrintServiceKind.choices)
    product_type = forms.ChoiceField(choices=CustomPrintProductType.choices)
    placements = forms.MultipleChoiceField(choices=PLACEMENT_CHOICES)
    placement_note = forms.CharField(required=False, max_length=255)
    files = MultiFileField(required=False, widget=MultiFileInput(attrs={"multiple": True}))
    quantity = forms.IntegerField(min_value=1)
    size_mode = forms.ChoiceField(choices=CustomPrintSizeMode.choices, required=False)
    sizes_note = forms.CharField(required=False, max_length=255)
    client_kind = forms.ChoiceField(choices=CustomPrintClientKind.choices, required=False)
    business_kind = forms.ChoiceField(choices=CustomPrintBusinessKind.choices, required=False)
    brand_name = forms.CharField(required=False, max_length=255)
    garment_note = forms.CharField(required=False, max_length=255)
    fit = forms.CharField(required=False, max_length=32)
    fabric = forms.CharField(required=False, max_length=32)
    color_choice = forms.CharField(required=False, max_length=64)
    file_triage_status = forms.CharField(required=False, max_length=32)
    exit_step = forms.CharField(required=False, max_length=32)
    placement_specs_json = forms.CharField(required=False)
    pricing_snapshot_json = forms.CharField(required=False)
    config_draft_json = forms.CharField(required=False)
    name = forms.CharField(max_length=200)
    contact_channel = forms.ChoiceField(choices=CustomPrintContactChannel.choices)
    contact_value = forms.CharField(max_length=255)
    brief = forms.CharField(required=False, max_length=2000, widget=forms.Textarea, error_messages={"max_length": "Опишіть завдання до 2000 символів. Додаткові деталі можна узгодити з менеджером."})
    # Поля з верифікації Telegram (заповнюються при підтвердженні через бота)
    telegram_verification_token = forms.CharField(required=False, max_length=64)
    telegram_verified_user_id = forms.CharField(required=False, max_length=32)
    telegram_verified_username = forms.CharField(required=False, max_length=100)
    telegram_verified_phone = forms.CharField(required=False, max_length=32)
    telegram_verified_first_name = forms.CharField(required=False, max_length=120)
    telegram_verified_last_name = forms.CharField(required=False, max_length=120)

    def __init__(self, *args, require_artwork_files=None, **kwargs):
        self.require_artwork_files = require_artwork_files
        super().__init__(*args, **kwargs)
        product_type = self.data.get("product_type") if self.is_bound else None
        allowed_zones = (PRODUCT_MATRIX.get(product_type) or {}).get("zones")
        if allowed_zones:
            self.fields["placements"].choices = [(zone, str(ZONE_LABELS.get(zone, zone))) for zone in allowed_zones]

    @staticmethod
    def _spec_requires_artwork_file(spec):
        if not isinstance(spec, dict):
            return False
        requires_artwork_file = spec.get("requires_artwork_file")
        if requires_artwork_file is None:
            return spec.get("mode") != "full_text"
        return bool(requires_artwork_file)

    def _collect_verification_data(self) -> dict:
        """Збирає дані з полів верифікації Telegram, якщо вони присутні."""
        data = self.data
        username = (data.get("telegram_verified_username") or "").strip().lstrip("@")
        phone = (data.get("telegram_verified_phone") or "").strip()
        user_id_raw = (data.get("telegram_verified_user_id") or "").strip()
        first_name = (data.get("telegram_verified_first_name") or "").strip()
        last_name = (data.get("telegram_verified_last_name") or "").strip()

        if not (username or phone or user_id_raw):
            return {}

        try:
            user_id_int = int(user_id_raw) if user_id_raw else None
        except (TypeError, ValueError):
            user_id_int = None

        from django.utils import timezone as _tz

        return {
            "telegram_verified_user_id": user_id_int,
            "telegram_verified_username": username,
            "telegram_verified_phone": phone,
            "telegram_verified_first_name": first_name,
            "telegram_verified_last_name": last_name,
            "telegram_verified_at": _tz.now(),
        }

    @staticmethod
    def _parse_json_field(raw_value, default, field_name):
        if raw_value in (None, ""):
            return default
        if isinstance(raw_value, (list, dict)):
            return raw_value
        try:
            parsed = json.loads(raw_value)
        except (TypeError, ValueError):
            raise forms.ValidationError("Некоректні дані форми.") from None
        if default == [] and not isinstance(parsed, list):
            raise forms.ValidationError("Некоректні дані форми.")
        if default == {} and not isinstance(parsed, dict):
            raise forms.ValidationError("Некоректні дані форми.")
        return parsed

    def clean_contact_value(self):
        value = (self.cleaned_data.get("contact_value") or "").strip()
        channel = (self.cleaned_data.get("contact_channel") or "").strip()

        if channel == CustomPrintContactChannel.PHONE:
            digits = normalize_phone(value)
            # Український формат: 380XXXXXXXXX (12 цифр) або хоча б 10 цифр
            if not (len(digits) >= 10 or (len(digits) == 12 and digits.startswith("380"))):
                raise forms.ValidationError(
                    "Введіть коректний український номер телефону (наприклад +380XXXXXXXXX)."
                )
            return value

        if channel == CustomPrintContactChannel.WHATSAPP:
            digits = normalize_phone(value)
            if not (len(digits) >= 10 or (len(digits) == 12 and digits.startswith("380"))):
                raise forms.ValidationError(
                    "Введіть коректний номер для WhatsApp (наприклад +380XXXXXXXXX)."
                )
            return value

        if channel == CustomPrintContactChannel.TELEGRAM:
            # Якщо клієнт пройшов верифікацію через бота — приймаємо будь-який
            # формат, бо ми вже маємо підтверджений username/phone.
            verified_username = (self.data.get("telegram_verified_username") or "").strip()
            verified_phone = (self.data.get("telegram_verified_phone") or "").strip()
            if verified_username or verified_phone:
                return value or (f"@{verified_username.lstrip('@')}" if verified_username else value)

            if value.startswith("https://t.me/"):
                return value
            if value.startswith("@") and len(value) > 1:
                return value
            if re.fullmatch(r"[A-Za-z0-9_]{3,}", value):
                return value
            # Якщо схоже на email — окрема, зрозуміла помилка з порадою
            if re.search(r"@.+\.[a-zA-Z]{2,}", value):
                raise forms.ValidationError(
                    "Це схоже на email, а не на Telegram-логін. "
                    "Натисніть «Підтвердити Telegram через бота» — ми отримаємо ваш номер автоматично."
                )
            raise forms.ValidationError(
                "Вкажіть Telegram у форматі @username, або натисніть «Підтвердити Telegram через бота»."
            )

        return value

    def clean(self):
        cleaned = super().clean()
        service_kind = cleaned.get("service_kind")
        placements = cleaned.get("placements") or []
        placement_note = (cleaned.get("placement_note") or "").strip()
        brief = (cleaned.get("brief") or "").strip()
        client_kind = cleaned.get("client_kind") or CustomPrintClientKind.PERSONAL
        business_kind = (cleaned.get("business_kind") or "").strip()
        garment_note = (cleaned.get("garment_note") or "").strip()
        files = cleaned.get("files")
        if not files:
            files = []
        elif not isinstance(files, (list, tuple)):
            files = [files]

        try:
            placement_specs = self._parse_json_field(
                cleaned.get("placement_specs_json"),
                [],
                "placement_specs_json",
            )
        except forms.ValidationError as exc:
            self.add_error("placement_specs_json", exc)
            placement_specs = []

        try:
            pricing_snapshot = self._parse_json_field(
                cleaned.get("pricing_snapshot_json"),
                {},
                "pricing_snapshot_json",
            )
        except forms.ValidationError as exc:
            self.add_error("pricing_snapshot_json", exc)
            pricing_snapshot = {}

        try:
            config_draft = self._parse_json_field(
                cleaned.get("config_draft_json"),
                {},
                "config_draft_json",
            )
        except forms.ValidationError as exc:
            self.add_error("config_draft_json", exc)
            config_draft = {}
        normalized_snapshot = normalize_custom_print_snapshot(config_draft) if config_draft else {}
        if cleaned.get("product_type") == "hoodie":
            pricing_snapshot = normalize_hoodie_included_pricing(pricing_snapshot, cleaned.get("quantity"))
        if normalized_snapshot:
            normalized_gift = (normalized_snapshot.get("order") or {}).get("gift")
            # Old cached configurators may still submit the retired packaging
            # addon. New requests cannot purchase it or retain its former fee.
            if not isinstance(normalized_gift, dict) or "box" not in normalized_gift:
                legacy_fee = (normalized_snapshot.get("pricing") or {}).get("gift_price") or 0
                if legacy_fee > 0:
                    for price_data in (normalized_snapshot["pricing"], pricing_snapshot):
                        if isinstance(price_data, dict):
                            price_data["gift_price"] = 0
                            try:
                                if price_data.get("final_total") is not None:
                                    price_data["final_total"] = max(0, float(price_data["final_total"]) - legacy_fee)
                            except (TypeError, ValueError):
                                price_data["final_total"] = None
        submission_type = (
            (config_draft.get("submission_type") if isinstance(config_draft, dict) else "")
            or ""
        ).strip()

        if not placement_specs and normalized_snapshot:
            placement_specs = build_placement_specs(normalized_snapshot)
        if not placement_specs and placements:
            placement_specs = [
                {
                    "zone": zone,
                    "label": dict(self.PLACEMENT_CHOICES).get(zone, zone),
                    "placement_key": zone,
                    "requires_artwork_file": True,
                    "file_index": index,
                }
                for index, zone in enumerate(placements)
            ]

        required_artwork_specs = [spec for spec in placement_specs if self._spec_requires_artwork_file(spec)]
        required_artwork_file_count = len(required_artwork_specs)
        require_artwork_files = self.require_artwork_files
        # Для режимів "готовий файл" і "потрібно допрацювати" аплоад файлів
        # завжди обовʼязковий, незалежно від кнопки (cart vs lead). Раніше
        # для submission_type == "lead" валідація зникала і клієнт міг
        # надіслати заявку без жодного файлу — тепер цей шлях закрито.
        # Навіть якщо view-функція передала require_artwork_files=False,
        # для READY/ADJUST силою примушуємо True. design — лишається без файлів.
        if service_kind in {
            CustomPrintServiceKind.READY,
            CustomPrintServiceKind.ADJUST,
        }:
            require_artwork_files = True
        elif require_artwork_files is None:
            require_artwork_files = submission_type != "lead"

        if "custom" in placements and not placement_note:
            self.add_error("placement_note", "Опишіть нестандартне розміщення принта.")

        if client_kind == CustomPrintClientKind.BRAND and not (cleaned.get("brand_name") or "").strip():
            self.add_error("brand_name", "Вкажіть назву бренду, команди або події.")
        if client_kind == CustomPrintClientKind.BRAND and not business_kind:
            business_kind = CustomPrintBusinessKind.BRANDING
        if cleaned.get("product_type") == CustomPrintProductType.CUSTOMER_GARMENT and not garment_note:
            self.add_error("garment_note", "Опишіть ваш виріб для попереднього прорахунку.")

        if (
            require_artwork_files
            and service_kind == CustomPrintServiceKind.READY
            and len(files) < required_artwork_file_count
        ):
            self.add_error("files", "Додайте файли для всіх зон, де потрібен макет.")

        if service_kind == CustomPrintServiceKind.ADJUST:
            if not brief:
                self.add_error("brief", "Опишіть, що саме потрібно змінити у файлі.")
            if require_artwork_files and len(files) < required_artwork_file_count:
                self.add_error("files", "Додайте файли для всіх зон, де потрібен макет.")

        if service_kind == CustomPrintServiceKind.DESIGN and not brief:
            self.add_error("brief", "Опишіть ідею, стиль або референси для дизайну.")

        validated_files = []
        limits = get_limits()
        for uploaded_file in files:
            try:
                validate_uploaded_file(
                    uploaded_file,
                    allowed_exts=ALLOWED_HELP_EXTS,
                    max_file_mb=limits["max_file_mb"],
                    strict_magic=True,
                )
            except ValueError as exc:
                code = str(exc)
                if code == "unsupported_extension":
                    self.add_error("files", "Непідтримуваний формат файлу.")
                elif code == "file_too_large":
                    self.add_error("files", f"Файл перевищує ліміт {limits['max_file_mb']} MB.")
                else:
                    self.add_error("files", "Один із файлів не пройшов перевірку безпеки.")
                continue
            uploaded_file.name = build_safe_upload_name("custom-print", uploaded_file.name)
            validated_files.append(uploaded_file)

        cleaned["client_kind"] = client_kind
        cleaned["business_kind"] = business_kind
        cleaned["brief"] = brief
        cleaned["garment_note"] = garment_note
        cleaned["placement_note"] = placement_note
        cleaned["placement_specs_json"] = placement_specs
        cleaned["pricing_snapshot_json"] = pricing_snapshot
        cleaned["config_draft_json"] = normalized_snapshot or config_draft or {}
        cleaned["fit"] = (cleaned.get("fit") or (normalized_snapshot.get("product") or {}).get("fit") or "").strip()
        cleaned["fabric"] = (cleaned.get("fabric") or (normalized_snapshot.get("product") or {}).get("fabric") or "").strip()
        cleaned["color_choice"] = (
            cleaned.get("color_choice")
            or (normalized_snapshot.get("product") or {}).get("color")
            or ""
        ).strip()
        cleaned["file_triage_status"] = (
            cleaned.get("file_triage_status")
            or (normalized_snapshot.get("artwork") or {}).get("triage_status")
            or ""
        ).strip()
        cleaned["exit_step"] = (
            cleaned.get("exit_step")
            or (normalized_snapshot.get("ui") or {}).get("current_step")
            or ""
        ).strip()
        self.cleaned_files = validated_files
        return cleaned

    def save(
        self,
        *,
        source: str = "main_custom_print",
        moderation_status: str | None = None,
        saved_uploads: list | None = None,
    ) -> CustomPrintLead:
        create_kwargs = {
            "service_kind": self.cleaned_data["service_kind"],
            "product_type": self.cleaned_data["product_type"],
            "placements": self.cleaned_data["placements"],
            "placement_note": self.cleaned_data.get("placement_note", ""),
            "quantity": self.cleaned_data["quantity"],
            "size_mode": self.cleaned_data.get("size_mode", ""),
            "sizes_note": self.cleaned_data.get("sizes_note", ""),
            "client_kind": self.cleaned_data.get("client_kind") or CustomPrintClientKind.PERSONAL,
            "business_kind": self.cleaned_data.get("business_kind", ""),
            "brand_name": self.cleaned_data.get("brand_name", ""),
            "fit": self.cleaned_data.get("fit", ""),
            "fabric": self.cleaned_data.get("fabric", ""),
            "color_choice": self.cleaned_data.get("color_choice", ""),
            "garment_note": self.cleaned_data.get("garment_note", ""),
            "file_triage_status": self.cleaned_data.get("file_triage_status", ""),
            "exit_step": self.cleaned_data.get("exit_step", ""),
            "placement_specs_json": self.cleaned_data.get("placement_specs_json") or [],
            "pricing_snapshot_json": self.cleaned_data.get("pricing_snapshot_json") or {},
            "config_draft_json": self.cleaned_data.get("config_draft_json") or {},
            "name": self.cleaned_data["name"],
            "contact_channel": self.cleaned_data["contact_channel"],
            "contact_value": self.cleaned_data["contact_value"],
            "brief": self.cleaned_data.get("brief", ""),
            "source": source or "main_custom_print",
        }
        if moderation_status is not None:
            create_kwargs["moderation_status"] = moderation_status
        elif source == "custom_print_cart":
            create_kwargs["moderation_status"] = CustomPrintModerationStatus.DRAFT

        # Verification data (опціонально, якщо клієнт пройшов підтвердження через бота)
        verification_data = self._collect_verification_data()
        if verification_data:
            create_kwargs.update(verification_data)

        lead = CustomPrintLead.objects.create(**create_kwargs)

        # Зв'язок з TelegramVerificationSession (якщо є валідний токен)
        verification_token = (self.data.get("telegram_verification_token") or "").strip()
        if verification_token:
            try:
                from accounts.models import TelegramVerificationSession

                session = TelegramVerificationSession.objects.filter(
                    token=verification_token
                ).first()
                if session and not session.lead_id:
                    session.lead = lead
                    session.save(update_fields=["lead"])
            except Exception:
                pass
        placement_specs = self.cleaned_data.get("placement_specs_json") or []
        file_spec_map = {}
        for spec in placement_specs:
            file_index = spec.get("file_index")
            if file_index is None:
                continue
            try:
                file_index = int(file_index)
            except (TypeError, ValueError):
                continue
            file_spec_map.setdefault(file_index, spec)

        # Точная карта «файл → зона» из конфигуратора (artwork.files повторяет
        # порядок поля files в FormData, включая несколько файлов на одну зону).
        draft = self.cleaned_data.get("config_draft_json") or {}
        artwork_files = (draft.get("artwork") or {}).get("files") if isinstance(draft, dict) else None
        file_meta_map = {}
        if isinstance(artwork_files, list):
            for position, item in enumerate(artwork_files):
                if not isinstance(item, dict):
                    continue
                idx = item.get("file_index")
                try:
                    idx = int(idx)
                except (TypeError, ValueError):
                    idx = position
                if item.get("placement_key") or item.get("zone"):
                    file_meta_map.setdefault(idx, item)

        for sort_order, uploaded_file in enumerate(getattr(self, "cleaned_files", [])):
            matched_spec = file_meta_map.get(sort_order) or file_spec_map.get(sort_order)
            if matched_spec is None and sort_order < len(placement_specs):
                matched_spec = placement_specs[sort_order]
            attachment_values = {
                "lead": lead,
                "file": uploaded_file,
                "placement_zone": (matched_spec or {}).get("placement_key") or (matched_spec or {}).get("zone", ""),
                "attachment_role": (matched_spec or {}).get("attachment_role")
                or (matched_spec or {}).get("role")
                or CustomPrintLeadAttachment.AttachmentRole.DESIGN,
                "sort_order": sort_order,
            }
            if saved_uploads is None:
                CustomPrintLeadAttachment.objects.create(**attachment_values)
                continue
            attachment = CustomPrintLeadAttachment(**attachment_values)
            attachment.file.save(uploaded_file.name, uploaded_file, save=False)
            saved_uploads.append((attachment.file.storage, attachment.file.name))
            attachment.save()
        return lead


class ProductFitOptionForm(forms.ModelForm):
    class Meta:
        model = ProductFitOption
        fields = [
            "code",
            "label",
            "description",
            "icon",
            "order",
            "is_default",
            "is_active",
        ]
        widgets = {
            "description": forms.TextInput(attrs={"placeholder": "Короткий опис посадки"}),
            "icon": forms.TextInput(attrs={"placeholder": "tshirt-classic"}),
            "order": forms.NumberInput(attrs={"min": "0"}),
        }


class ProductFitOptionBaseFormSet(BaseInlineFormSet):
    def clean(self):
        super().clean()
        default_count = 0
        active_count = 0
        for form in self.forms:
            if not hasattr(form, "cleaned_data") or not form.cleaned_data:
                continue
            if form.cleaned_data.get("DELETE"):
                continue
            if not form.cleaned_data.get("is_active"):
                continue
            active_count += 1
            if form.cleaned_data.get("is_default"):
                default_count += 1

        if active_count and default_count > 1:
            raise forms.ValidationError("Для товару можна обрати тільки одну посадку за замовчуванням.")


ProductFitOptionFormSet = inlineformset_factory(
    Product,
    ProductFitOption,
    form=ProductFitOptionForm,
    formset=ProductFitOptionBaseFormSet,
    extra=2,
    can_delete=True,
)


def build_product_fit_option_formset(product=None, data=None, prefix='fit_options'):
    product_instance = product or Product()
    return ProductFitOptionFormSet(
        data=data if data is not None else None,
        instance=product_instance,
        prefix=prefix,
    )


# ---------------------------------------------------------------------------
# Phase 17 — Simple fit-toggle UI for the custom admin product builder.
#
# The legacy ``ProductFitOptionFormSet`` exposes the full row schema
# (code/label/icon/description/order/is_default/is_active) which is
# overwhelming for the only two real-world fits we ship — *classic* and
# *oversize*. ``ProductFitToggleForm`` collapses the UI to:
#
#   - one switch per fit ("Класична", "Оверсайз")
#   - one radio for the default fit
#
# On ``save(product)`` we ensure both rows exist with stable
# code/label/order, flip ``is_active``, and elect the right default.
# Any other rows the legacy formset created (regular / custom codes)
# stay untouched.
# ---------------------------------------------------------------------------

FIT_TOGGLE_DEFINITIONS = (
    {"code": "classic",  "label": "Класична",  "order": 0},
    {"code": "oversize", "label": "Оверсайз", "order": 1},
)
FIT_TOGGLE_CODES = tuple(d["code"] for d in FIT_TOGGLE_DEFINITIONS)


class ProductFitToggleForm(forms.Form):
    """Compact UI for managing classic/oversize fit visibility per product."""

    fit_classic_enabled = forms.BooleanField(
        required=False,
        label="Класична посадка",
        initial=True,
        widget=forms.CheckboxInput(attrs={
            "class": "form-check-input",
            "role": "switch",
        }),
    )
    fit_oversize_enabled = forms.BooleanField(
        required=False,
        label="Оверсайз посадка",
        initial=True,
        widget=forms.CheckboxInput(attrs={
            "class": "form-check-input",
            "role": "switch",
        }),
    )
    fit_default = forms.ChoiceField(
        required=False,
        choices=(("classic", "Класична"), ("oversize", "Оверсайз")),
        widget=forms.RadioSelect,
        initial="classic",
        label="За замовчуванням",
    )

    def __init__(self, *args, product=None, **kwargs):
        self.product = product
        if product is not None and product.pk:
            self._populate_initial_from_product(product, kwargs)
        super().__init__(*args, **kwargs)

    @staticmethod
    def _populate_initial_from_product(product, kwargs):
        """Pre-fill ``initial`` from the product's existing fit_options.

        Falls back to "both enabled, classic default" when the product
        has no rows yet (typical for fresh tshirts before Phase 17).
        """
        initial = dict(kwargs.get("initial") or {})
        rows = {opt.code: opt for opt in product.fit_options.all()}
        classic = rows.get("classic")
        oversize = rows.get("oversize")
        if classic is None and oversize is None:
            # New / legacy tshirt with no fit_options yet — both on.
            initial.setdefault("fit_classic_enabled", True)
            initial.setdefault("fit_oversize_enabled", True)
            initial.setdefault("fit_default", "classic")
        else:
            initial.setdefault(
                "fit_classic_enabled",
                bool(classic and classic.is_active),
            )
            initial.setdefault(
                "fit_oversize_enabled",
                bool(oversize and oversize.is_active),
            )
            # Pick the row marked default; fall back to the first active.
            default_code = ""
            for code in FIT_TOGGLE_CODES:
                row = rows.get(code)
                if row and row.is_default and row.is_active:
                    default_code = code
                    break
            if not default_code:
                for code in FIT_TOGGLE_CODES:
                    row = rows.get(code)
                    if row and row.is_active:
                        default_code = code
                        break
            initial.setdefault("fit_default", default_code or "classic")
        kwargs["initial"] = initial

    def clean(self):
        cleaned = super().clean()
        classic = bool(cleaned.get("fit_classic_enabled"))
        oversize = bool(cleaned.get("fit_oversize_enabled"))
        default = cleaned.get("fit_default") or ""
        # Coerce the default to an *active* code; fall back gracefully
        # so that an admin who flips both off doesn't crash the form.
        if default == "classic" and not classic and oversize:
            cleaned["fit_default"] = "oversize"
        elif default == "oversize" and not oversize and classic:
            cleaned["fit_default"] = "classic"
        elif not classic and not oversize:
            cleaned["fit_default"] = ""
        return cleaned

    def save(self, product):
        """Persist toggle state. Creates rows lazily, never deletes
        rows the legacy formset may have produced (e.g. ``regular``).
        """
        if product is None or not product.pk:
            return
        cleaned = self.cleaned_data
        states = {
            "classic":  bool(cleaned.get("fit_classic_enabled")),
            "oversize": bool(cleaned.get("fit_oversize_enabled")),
        }
        default_code = cleaned.get("fit_default") or ""

        existing = {opt.code: opt for opt in product.fit_options.all()}
        for definition in FIT_TOGGLE_DEFINITIONS:
            code = definition["code"]
            row = existing.get(code)
            wanted_active = states[code]
            wanted_default = (default_code == code) and wanted_active
            if row is None:
                # Lazy-create. We only insert a row if we want it
                # active; deactivated rows simply stay missing so the
                # admin sees a clean slate the next time.
                if wanted_active:
                    ProductFitOption.objects.create(
                        product=product,
                        code=code,
                        label=definition["label"],
                        order=definition["order"],
                        is_active=True,
                        is_default=wanted_default,
                    )
            else:
                changed = []
                if row.is_active != wanted_active:
                    row.is_active = wanted_active
                    changed.append("is_active")
                if row.is_default != wanted_default:
                    row.is_default = wanted_default
                    changed.append("is_default")
                # Heal historical rows that are missing the canonical
                # label/order from FIT_TOGGLE_DEFINITIONS.
                if not (row.label or "").strip():
                    row.label = definition["label"]
                    changed.append("label")
                if changed:
                    row.save(update_fields=changed)

        # Final defensive sweep: if no row is marked default but at
        # least one is active, elect the first active one.
        rows = list(product.fit_options.filter(is_active=True).order_by("order", "id"))
        if rows and not any(r.is_default for r in rows):
            head = rows[0]
            head.is_default = True
            head.save(update_fields=["is_default"])
        # And conversely, never leave more than one default.
        defaults = [r for r in rows if r.is_default]
        if len(defaults) > 1:
            keep = next(
                (r for r in defaults if r.code == default_code),
                defaults[0],
            )
            for r in defaults:
                if r.pk != keep.pk:
                    r.is_default = False
                    r.save(update_fields=["is_default"])


def ensure_default_fit_options_for_tshirt(product) -> bool:
    """Auto-bootstrap classic+oversize for a tshirt product without rows.

    Used at view boundaries (PDP load, admin save) to repair legacy
    products created before Phase 17 where the admin never saw a fit
    UI. Returns True when rows were created, False otherwise.
    """
    if product is None or not product.pk:
        return False
    if not getattr(product, "fit_selector_enabled", True):
        return False
    # Only auto-bootstrap when we recognise it as a tshirt product
    # (mirrors storefront.views.product._is_tshirt_product without
    # importing it — keeps forms decoupled from views).
    candidates = " ".join(
        str(getattr(product, attr, "") or "").lower()
        for attr in ("title",)
    )
    cat = getattr(product, "category", None)
    cat_text = " ".join(
        str(getattr(cat, attr, "") or "").lower()
        for attr in ("name", "slug")
    ) if cat else ""
    catalog = getattr(product, "catalog", None)
    catalog_text = " ".join(
        str(getattr(catalog, attr, "") or "").lower()
        for attr in ("name", "slug")
    ) if catalog else ""
    haystack = f"{candidates} {cat_text} {catalog_text}"
    import re
    pattern = re.compile(r"(^|[^a-z0-9а-яіїєґ])(?:футбол\w*|t-?shirts?|tees?)(?=$|[^a-z0-9а-яіїєґ])")
    if not pattern.search(haystack):
        return False
    if product.fit_options.exists():
        return False
    rows_created = False
    for definition in FIT_TOGGLE_DEFINITIONS:
        ProductFitOption.objects.create(
            product=product,
            code=definition["code"],
            label=definition["label"],
            order=definition["order"],
            is_active=True,
            is_default=(definition["code"] == "classic"),
        )
        rows_created = True
    return rows_created


class CategoryForm(forms.ModelForm):
    class Meta:
        model = Category
        fields = [
            "name", "slug", "icon", "cover", "order", "description",
            "is_active", "is_featured", "seo_title", "seo_h1",
            "seo_description", "seo_text_title", "seo_intro_html",
        ]
        widgets = {
            "name": forms.TextInput(attrs={"class": "catalog-category-input"}),
            "slug": forms.TextInput(attrs={"class": "catalog-category-input", "spellcheck": "false"}),
            "icon": forms.ClearableFileInput(attrs={"class": "catalog-category-file", "accept": "image/png,image/webp,image/svg+xml"}),
            "cover": forms.ClearableFileInput(attrs={"class": "catalog-category-file", "accept": "image/*"}),
            "order": forms.NumberInput(attrs={"class": "catalog-category-input", "min": "0"}),
            "description": forms.Textarea(attrs={"class": "catalog-category-input", "rows": 4}),
            "seo_title": forms.TextInput(attrs={"class": "catalog-category-input", "maxlength": "180"}),
            "seo_h1": forms.TextInput(attrs={"class": "catalog-category-input", "maxlength": "180"}),
            "seo_description": forms.Textarea(attrs={"class": "catalog-category-input", "rows": 3, "maxlength": "320"}),
            "seo_text_title": forms.TextInput(attrs={"class": "catalog-category-input", "maxlength": "200"}),
            "seo_intro_html": forms.Textarea(attrs={"class": "catalog-category-input catalog-category-input--code", "rows": 6}),
        }

    def clean_slug(self):
        slug = self.cleaned_data.get('slug')
        if slug:
            existing = Category.objects.filter(slug=slug)
            if self.instance.pk:
                existing = existing.exclude(pk=self.instance.pk)
            if existing.exists():
                raise forms.ValidationError('Категорія з таким slug вже існує.')
        return slug


def _model_fields(model, names):
    existing = {field.name for field in model._meta.fields}
    return [name for name in names if name in existing]


class BlogCategoryForm(forms.ModelForm):
    class Meta:
        model = BlogCategory
        fields = _model_fields(
            BlogCategory,
            [
                "parent",
                "name",
                "name_uk",
                "name_ru",
                "name_en",
                "slug",
                "description",
                "description_uk",
                "description_ru",
                "description_en",
                "seo_title",
                "seo_title_uk",
                "seo_title_ru",
                "seo_title_en",
                "seo_h1",
                "seo_h1_uk",
                "seo_h1_ru",
                "seo_h1_en",
                "seo_description",
                "seo_description_uk",
                "seo_description_ru",
                "seo_description_en",
                "bottom_title",
                "bottom_title_uk",
                "bottom_title_ru",
                "bottom_title_en",
                "bottom_text",
                "bottom_text_uk",
                "bottom_text_ru",
                "bottom_text_en",
                "order",
                "is_active",
            ],
        )
        widgets = {
            "description": forms.Textarea(attrs={"rows": 3}),
            "description_uk": forms.Textarea(attrs={"rows": 3}),
            "description_ru": forms.Textarea(attrs={"rows": 3}),
            "description_en": forms.Textarea(attrs={"rows": 3}),
            "seo_description": forms.Textarea(attrs={"rows": 2}),
            "seo_description_uk": forms.Textarea(attrs={"rows": 2}),
            "seo_description_ru": forms.Textarea(attrs={"rows": 2}),
            "seo_description_en": forms.Textarea(attrs={"rows": 2}),
            "bottom_text": forms.Textarea(attrs={"rows": 4}),
            "bottom_text_uk": forms.Textarea(attrs={"rows": 4}),
            "bottom_text_ru": forms.Textarea(attrs={"rows": 4}),
            "bottom_text_en": forms.Textarea(attrs={"rows": 4}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if "parent" in self.fields:
            qs = BlogCategory.objects.filter(is_active=True).order_by("order", "name")
            if self.instance and self.instance.pk:
                qs = qs.exclude(pk=self.instance.pk)
            self.fields["parent"].queryset = qs
            self.fields["parent"].required = False
        for name, field in self.fields.items():
            if name.endswith(("_uk", "_ru", "_en")):
                field.required = False
        if "order" in self.fields:
            self.fields["order"].required = False
            self.fields["order"].initial = 0

    def clean_order(self):
        return self.cleaned_data.get("order") or 0


class BlogPostForm(forms.ModelForm):
    blocks_json = forms.CharField(required=False, widget=forms.HiddenInput(attrs={"data-blog-blocks-json": "1"}))

    class Meta:
        model = BlogPost
        formfield_callback = _https_url_formfield
        fields = _model_fields(
            BlogPost,
            [
                "category",
                "title",
                "title_uk",
                "title_ru",
                "title_en",
                "slug",
                "excerpt",
                "excerpt_uk",
                "excerpt_ru",
                "excerpt_en",
                "content_html",
                "content_html_uk",
                "content_html_ru",
                "content_html_en",
                "cover_image",
                "cover_alt",
                "cover_alt_uk",
                "cover_alt_ru",
                "cover_alt_en",
                "cover_caption",
                "cover_caption_uk",
                "cover_caption_ru",
                "cover_caption_en",
                "source_url",
                "cta_label",
                "cta_label_uk",
                "cta_label_ru",
                "cta_label_en",
                "cta_url",
                "cta_text",
                "cta_text_uk",
                "cta_text_ru",
                "cta_text_en",
                "published_at",
                "is_published",
                "seo_title",
                "seo_title_uk",
                "seo_title_ru",
                "seo_title_en",
                "seo_description",
                "seo_description_uk",
                "seo_description_ru",
                "seo_description_en",
                "seo_keywords",
                "seo_keywords_uk",
                "seo_keywords_ru",
                "seo_keywords_en",
            ],
        ) + ["blocks_json"]
        widgets = {
            "published_at": forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
            "excerpt": forms.Textarea(attrs={"rows": 3}),
            "excerpt_uk": forms.Textarea(attrs={"rows": 3}),
            "excerpt_ru": forms.Textarea(attrs={"rows": 3}),
            "excerpt_en": forms.Textarea(attrs={"rows": 3}),
            "content_html": forms.Textarea(attrs={"rows": 12}),
            "content_html_uk": forms.Textarea(attrs={"rows": 12}),
            "content_html_ru": forms.Textarea(attrs={"rows": 12}),
            "content_html_en": forms.Textarea(attrs={"rows": 12}),
            "seo_description": forms.Textarea(attrs={"rows": 2}),
            "seo_description_uk": forms.Textarea(attrs={"rows": 2}),
            "seo_description_ru": forms.Textarea(attrs={"rows": 2}),
            "seo_description_en": forms.Textarea(attrs={"rows": 2}),
            "cover_caption": forms.Textarea(attrs={"rows": 2}),
            "cover_caption_uk": forms.Textarea(attrs={"rows": 2}),
            "cover_caption_ru": forms.Textarea(attrs={"rows": 2}),
            "cover_caption_en": forms.Textarea(attrs={"rows": 2}),
            "cta_text": forms.Textarea(attrs={"rows": 2}),
            "cta_text_uk": forms.Textarea(attrs={"rows": 2}),
            "cta_text_ru": forms.Textarea(attrs={"rows": 2}),
            "cta_text_en": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["category"].queryset = BlogCategory.objects.filter(is_active=True).order_by("order", "name")
        for name, field in self.fields.items():
            if name.endswith(("_uk", "_ru", "_en")):
                field.required = False
        if self.instance and self.instance.pk and self.instance.published_at:
            self.initial["published_at"] = self.instance.published_at.strftime("%Y-%m-%dT%H:%M")
        if self.instance and self.instance.pk:
            from storefront.services.blog_blocks import admin_blocks_json

            self.initial["blocks_json"] = admin_blocks_json(self.instance)


class BlogMediaAssetForm(forms.ModelForm):
    alt_text = forms.CharField(max_length=180, required=False)
    caption_text = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))
    credit_text = forms.CharField(max_length=180, required=False)

    class Meta:
        model = BlogMediaAsset
        fields = ["file", "alt_text", "caption_text", "credit_text"]

    def save(self, commit=True):
        instance = super().save(commit=False)
        alt = (self.cleaned_data.get("alt_text") or "").strip()
        caption = (self.cleaned_data.get("caption_text") or "").strip()
        credit = (self.cleaned_data.get("credit_text") or "").strip()
        if alt:
            instance.alt = {"uk": alt}
        if caption:
            instance.caption = {"uk": caption}
        if credit:
            instance.credit = {"uk": credit}
        if commit:
            instance.save()
        return instance


class PrintProposalForm(forms.ModelForm):
    class Meta:
        model = PrintProposal
        formfield_callback = _https_url_formfield
        fields = ["image", "link_url", "description"]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 4, "placeholder": "Опис і примітки до принта"}),
        }

    def clean(self):
        data = super().clean()
        image = data.get("image")
        link_url = data.get("link_url", "").strip()

        # Требуем хотя бы один источник: файл или ссылка
        if not image and not link_url:
            raise forms.ValidationError("Додайте зображення або вкажіть посилання")

        # Лёгкая защита от спама: ограничим частоту отправок в view
        return data


class PushNotificationCampaignForm(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["title"].widget.attrs.update(
            {"class": "push-form-input", "data-push-title": "1"}
        )
        self.fields["body"].widget.attrs.update(
            {"class": "push-form-textarea", "data-push-body": "1"}
        )
        self.fields["target_url"].widget.attrs.update(
            {"class": "push-form-input", "data-push-link": "1"}
        )
        self.fields["image"].widget.attrs.update(
            {"class": "push-form-input", "accept": "image/*", "data-push-image": "1"}
        )

    class Meta:
        model = PushNotificationCampaign
        fields = ["title", "body", "target_url", "image"]
        widgets = {
            "title": forms.TextInput(
                attrs={
                    "placeholder": "Наприклад: Новий дроп уже на сайті",
                    "maxlength": 120,
                }
            ),
            "body": forms.Textarea(
                attrs={
                    "rows": 4,
                    "maxlength": 240,
                    "placeholder": "Короткий і зрозумілий текст push-сповіщення",
                }
            ),
            "target_url": forms.TextInput(
                attrs={
                    "placeholder": "/catalog/ або https://twocomms.shop/catalog/",
                }
            ),
        }
        help_texts = {
            "image": "Рекомендовано горизонтальне зображення 1200×630 або крупніше.",
        }

    def clean_target_url(self):
        raw_value = (self.cleaned_data.get("target_url") or "").strip()
        if not raw_value:
            raise forms.ValidationError("Вкажіть сторінку, куди має вести push-сповіщення.")

        site_base_url = (getattr(settings, "SITE_BASE_URL", "") or "").strip()
        site_host = urlparse(site_base_url).netloc.lower()

        if raw_value.startswith("/"):
            return raw_value

        parsed = urlparse(raw_value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise forms.ValidationError("Вкажіть внутрішнє посилання сайту або повний URL цього сайту.")

        if site_host and parsed.netloc.lower() != site_host:
            raise forms.ValidationError("Push-сповіщення може вести лише на сторінки сайту TwoComms.")

        normalized = parsed.path or "/"
        if parsed.query:
            normalized = f"{normalized}?{parsed.query}"
        if parsed.fragment:
            normalized = f"{normalized}#{parsed.fragment}"
        return normalized
