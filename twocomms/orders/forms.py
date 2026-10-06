from django import forms

from orders.nova_poshta_documents import normalize_phone, normalize_phone_for_np, order_contains_dtf_film


class CompanyProfileForm(forms.Form):
    company_name = forms.CharField(
        label="Назва компанії",
        max_length=200,
        required=True,
        widget=forms.TextInput(attrs={"placeholder": "TwoComms Partner"})
    )
    company_number = forms.CharField(
        label="ЄДРПОУ / ІПН",
        max_length=50,
        required=False,
        widget=forms.TextInput(attrs={"placeholder": "1234567890"})
    )
    phone = forms.CharField(
        label="Телефон",
        max_length=32,
        required=True,
        widget=forms.TextInput(attrs={"placeholder": "+380XXXXXXXXX"})
    )
    email = forms.EmailField(
        label="Email",
        required=False,
        widget=forms.EmailInput(attrs={"placeholder": "partner@email.com"})
    )
    website = forms.URLField(
        label="Сайт або магазин",
        required=False,
        assume_scheme="https",
        widget=forms.URLInput(attrs={"placeholder": "https://"}),
    )
    instagram = forms.CharField(
        label="Instagram",
        required=False,
        widget=forms.TextInput(attrs={"placeholder": "@brand"})
    )
    telegram = forms.CharField(
        label="Telegram",
        required=False,
        widget=forms.TextInput(attrs={"placeholder": "@manager"})
    )
    payment_method = forms.ChoiceField(
        label="Спосіб виплати",
        choices=[('card', 'На картку'), ('iban', 'IBAN')],
        required=True,
        initial='card',
        widget=forms.Select(attrs={"class": "ds-select"})
    )
    payment_details = forms.CharField(
        label="Реквізити для виплат",
        required=False,
        widget=forms.TextInput(
            attrs={
                "placeholder": "1234 5678 9012 3456",
                "maxlength": 50,
                "class": "ds-input",
                "id": "id_payment_details",
            }
        )
    )
    avatar = forms.ImageField(
        label="Логотип",
        required=False
    )

    def clean_phone(self):
        phone = normalize_phone(self.cleaned_data.get('phone', ''))
        if not phone:
            raise forms.ValidationError('Телефон обов’язковий для заповнення.')
        return phone

    def clean(self):
        cleaned = super().clean()
        return cleaned

    def clean_website(self):
        website = self.cleaned_data.get('website', '').strip()
        if website and not website.startswith(('http://', 'https://')):
            website = f'https://{website}'
        return website

    def clean_instagram(self):
        instagram = self.cleaned_data.get('instagram', '').strip()
        if instagram and not instagram.startswith('@') and 'http' not in instagram:
            instagram = f'@{instagram}'
        return instagram

    def clean_payment_details(self):
        return self.cleaned_data.get('payment_details', '').strip()


class TelegramNovaPoshtaWaybillForm(forms.Form):
    PAYER_TYPE_CHOICES = [
        ("Recipient", "Одержувач сплачує доставку"),
        ("Sender", "Відправник сплачує доставку"),
    ]
    PAYMENT_METHOD_CHOICES = [
        ("Cash", "Готівка"),
        ("NonCash", "Безготівково"),
    ]

    recipient_full_name = forms.CharField(label="ПІБ одержувача", max_length=200)
    recipient_phone = forms.CharField(label="Телефон одержувача", max_length=32)
    recipient_city = forms.CharField(label="Місто одержувача", max_length=150)
    recipient_settlement_ref = forms.CharField(max_length=36, required=False, widget=forms.HiddenInput())
    recipient_city_ref = forms.CharField(max_length=36, required=False, widget=forms.HiddenInput())
    recipient_city_token = forms.CharField(max_length=2048, required=False, widget=forms.HiddenInput())
    recipient_warehouse = forms.CharField(label="Відділення / поштомат одержувача", max_length=200)
    recipient_warehouse_ref = forms.CharField(max_length=36, required=False, widget=forms.HiddenInput())
    recipient_warehouse_token = forms.CharField(max_length=2048, required=False, widget=forms.HiddenInput())

    sender_city = forms.CharField(label="Місто відправника", max_length=150)
    sender_settlement_ref = forms.CharField(max_length=36, required=False, widget=forms.HiddenInput())
    sender_city_ref = forms.CharField(max_length=36, required=False, widget=forms.HiddenInput())
    sender_city_token = forms.CharField(max_length=2048, required=False, widget=forms.HiddenInput())
    sender_warehouse = forms.CharField(label="Відділення відправника", max_length=200)
    sender_warehouse_ref = forms.CharField(max_length=36, required=False, widget=forms.HiddenInput())
    sender_warehouse_token = forms.CharField(max_length=2048, required=False, widget=forms.HiddenInput())

    description = forms.CharField(label="Опис відправлення", max_length=100)
    shipment_contract_token = forms.CharField(max_length=1024, required=False, widget=forms.HiddenInput())
    packaging_type = forms.ChoiceField(
        label="Пакування",
        required=False,
        choices=[("own_packaging", "Власне пакування"), ("np_clothing_bag", "Пакет Нової пошти для одягу"), ("np_tube_60", "Тубус Нової пошти 60 см")],
        help_text="Пакування Нової пошти додається до ТТН за актуальним довідником. При власному пакуванні послуга не додається.",
    )
    declared_cost = forms.DecimalField(label="Оголошена вартість", max_digits=12, decimal_places=2, min_value=0)
    weight = forms.DecimalField(label="Вага, кг", max_digits=6, decimal_places=2, min_value=0.1)
    seats_amount = forms.IntegerField(label="Кількість місць", min_value=1, max_value=1)
    length_cm = forms.DecimalField(label="Довжина, см", max_digits=6, decimal_places=2, min_value=1)
    width_cm = forms.DecimalField(label="Ширина, см", max_digits=6, decimal_places=2, min_value=1)
    height_cm = forms.DecimalField(label="Висота, см", max_digits=6, decimal_places=2, min_value=1)
    cod_amount = forms.DecimalField(
        label="Післяплата, грн",
        max_digits=12,
        decimal_places=2,
        min_value=0,
        required=False,
    )
    payer_type = forms.ChoiceField(
        label="Хто сплачує доставку",
        required=False,
        choices=PAYER_TYPE_CHOICES,
    )
    payment_method = forms.ChoiceField(
        label="Форма оплати доставки",
        required=False,
        choices=PAYMENT_METHOD_CHOICES,
        help_text="Безготівкова оплата потребує доступного договору з Новою поштою для обраного платника. Якщо API відхиляє NonCash, перевірте договір або оберіть готівкову оплату.",
    )

    def __init__(self, *args, **kwargs):
        order = kwargs.pop("order", None)
        super().__init__(*args, **kwargs)
        if order is not None:
            self.fields["packaging_type"].choices = (
                [("np_tube_60", "Тубус Нової пошти 60 см")]
                if order_contains_dtf_film(order)
                else [("own_packaging", "Власне пакування"), ("np_clothing_bag", "Пакет Нової пошти для одягу")]
            )
        placeholders = {
            "recipient_full_name": "ПІБ одержувача",
            "recipient_phone": "+380XXXXXXXXX",
            "recipient_city": "Оберіть місто Нової пошти",
            "recipient_warehouse": "Оберіть відділення або поштомат",
            "sender_city": "Місто відправника",
            "sender_warehouse": "Відділення відправника",
            "description": "Що відправляємо",
            "declared_cost": "0.00",
            "weight": "1.0",
            "seats_amount": "1",
            "length_cm": "30",
            "width_cm": "20",
            "height_cm": "8",
            "cod_amount": "0.00",
        }
        for name, field in self.fields.items():
            if isinstance(field.widget, forms.HiddenInput):
                continue
            field.widget.attrs.setdefault("class", "telegram-waybill-input")
            field.widget.attrs.setdefault("placeholder", placeholders.get(name, ""))
            if name == "declared_cost":
                field.widget.attrs.setdefault("data-payment-declared-field", "")
            if name == "cod_amount":
                field.widget.attrs.setdefault("data-payment-cod-field", "")
        self.fields["payer_type"].initial = "Recipient"
        self.fields["payment_method"].initial = "Cash"

    def clean_recipient_phone(self):
        phone = normalize_phone(self.cleaned_data.get("recipient_phone", ""))
        if not normalize_phone_for_np(phone):
            raise forms.ValidationError("Телефон одержувача має бути у форматі +380XXXXXXXXX.")
        return phone

    def clean(self):
        cleaned = super().clean()
        for field_name in (
            "recipient_full_name",
            "recipient_city",
            "recipient_warehouse",
            "sender_city",
            "sender_warehouse",
            "description",
        ):
            cleaned[field_name] = str(cleaned.get(field_name) or "").strip()

        payer_type = str(cleaned.get("payer_type") or "").strip() or "Recipient"
        payment_method = str(cleaned.get("payment_method") or "").strip() or "Cash"
        cleaned["payer_type"] = payer_type
        cleaned["payment_method"] = payment_method

        cod_amount = cleaned.get("cod_amount")
        if cod_amount is None:
            cleaned["cod_amount"] = 0

        return cleaned
