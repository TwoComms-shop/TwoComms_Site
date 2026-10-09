"""Phase 21 (PR-4c) — review submission form.

Supports both authenticated users and guests. Photo handling is at the
view layer (``InMemoryUploadedFile`` list); the form only validates
text + rating + honeypot + length floors.

Validation contract:
    * optional rating ∈ {1, 2, 3, 4, 5}
    * body length ≥ 20 visible characters (whitespace-stripped)
    * author_name 1–80 chars
    * email required for guests; validated and kept private
    * honeypot field ``website`` MUST be empty (bot trap)
"""

from __future__ import annotations

from django import forms
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _
from .services.content import has_contact_or_markup


_MIN_BODY_LEN = 20


class ReviewForm(forms.Form):
    def __init__(self, *args, guest=False, purchase_invited=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.purchase_invited = purchase_invited
        self.fields["email"].required = guest and not purchase_invited
        self.fields["email"].error_messages["required"] = _("Залиш email для зв’язку. Він не публікується.")

    campaign_opt_in = forms.BooleanField(required=False)
    kind = forms.ChoiceField(required=False, choices=[("review", "Відгук"), ("comment", "Коментар")])
    city = forms.CharField(required=False, max_length=80)
    pros = forms.CharField(required=False, max_length=600)
    cons = forms.CharField(required=False, max_length=600)
    rating = forms.TypedChoiceField(
        required=False,
        choices=[(str(n), f"{n}★") for n in range(1, 6)],
        coerce=int,
        empty_value=0,
        error_messages={"required": _("Оберіть оцінку від 1 до 5.")},
    )
    title = forms.CharField(
        required=False,
        max_length=120,
        strip=True,
    )
    body = forms.CharField(
        widget=forms.Textarea,
        max_length=4000,
        strip=True,
        error_messages={"required": _("Розкажіть про свій досвід — мінімум 20 символів.")},
    )
    author_name = forms.CharField(
        max_length=80,
        strip=True,
        error_messages={"required": _("Як вас підписати?")},
    )
    email = forms.EmailField(
        required=False,
    )

    # Honeypot — visible to bots, hidden from humans via CSS in the
    # template. If anything ends up here we silently reject as if the
    # form was valid (caller checks ``cleaned_data['_is_bot']``).
    website = forms.CharField(required=False, widget=forms.HiddenInput)

    def clean_rating(self) -> int:
        value = int(self.cleaned_data.get("rating") or 0)
        if value and (value < 1 or value > 5):
            raise ValidationError("Оцінка має бути цілим числом 1-5.")
        return value

    def clean_body(self) -> str:
        body = (self.cleaned_data.get("body") or "").strip()
        if len(body) < _MIN_BODY_LEN:
            raise ValidationError(
                _("Текст відгуку має містити щонайменше 20 символів.")
            )
        return body

    def clean_author_name(self) -> str:
        name = (self.cleaned_data.get("author_name") or "").strip()
        if not name:
            raise ValidationError(_("Введіть ім'я для публікації."))
        return name

    def clean(self):
        cleaned = super().clean()
        if self.purchase_invited:
            if cleaned.get("kind") == "comment":
                self.add_error("kind", _("За цим запрошенням потрібен відгук з оцінкою 1–5."))
            cleaned["kind"] = "review"
        cleaned["kind"] = cleaned.get("kind") or ("review" if cleaned.get("rating") else "comment")
        if cleaned["kind"] == "comment":
            cleaned["rating"] = None
        elif not cleaned.get("rating"):
            self.add_error("rating", _("Оберіть оцінку від 1 до 5."))
        for field in ("author_name", "city", "title", "body", "pros", "cons"):
            if has_contact_or_markup(cleaned.get(field) or ""):
                self.add_error(field, _("Приберіть посилання, контакти та HTML. Тут ділимося власним досвідом."))
        cleaned["_is_bot"] = bool((cleaned.get("website") or "").strip())
        return cleaned
