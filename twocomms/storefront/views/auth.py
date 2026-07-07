"""
Authentication views для storefront приложения.

Содержит views для login, register, logout и связанные формы.
"""

import re
import logging
from django import forms
from django.shortcuts import render, redirect
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.utils.translation import gettext as _, gettext_lazy as _lazy

from ..models import Product
from accounts.models import UserProfile, FavoriteProduct
from orders.nova_poshta_checkout import NovaPoshtaSelectionError, resolve_optional_delivery_selection
from orders.nova_poshta_documents import normalize_checkout_phone


def _published_products_for_user(user):
    """
    Returns queryset limited to published products for non-staff users.
    Staff retains access to all products for support/QA tasks.
    """
    qs = Product.objects.all()
    if not (user.is_authenticated and user.is_staff):
        qs = qs.filter(status='published')
    return qs


logger = logging.getLogger(__name__)


# ==================== FORMS ====================

class LoginForm(forms.Form):
    """Форма входа в систему."""

    username = forms.CharField(
        label=_lazy("Логін"),
        max_length=150,
        widget=forms.TextInput(attrs={"class": "form-control bg-elevate"})
    )
    password = forms.CharField(
        label=_lazy("Пароль"),
        widget=forms.PasswordInput(attrs={"class": "form-control bg-elevate"})
    )


class RegisterForm(forms.Form):
    """Форма регистрации нового пользователя."""

    username = forms.CharField(
        label=_lazy("Логін"),
        max_length=150,
        widget=forms.TextInput(attrs={"class": "form-control bg-elevate"})
    )
    password1 = forms.CharField(
        label=_lazy("Пароль"),
        widget=forms.PasswordInput(attrs={"class": "form-control bg-elevate"})
    )
    password2 = forms.CharField(
        label=_lazy("Повтор паролю"),
        widget=forms.PasswordInput(attrs={"class": "form-control bg-elevate"})
    )

    def clean_username(self):
        """Проверка username на корректность."""
        username = self.cleaned_data.get('username', '')

        # Минимальная длина
        if len(username) < 3:
            raise ValidationError(_("Логін повинен містити мінімум 3 символи"))

        # Проверка на допустимые символы (латиница, цифры, . _ -)
        if not re.match(r'^[a-zA-Z0-9._-]+$', username):
            raise ValidationError(_("Логін може містити тільки латинські літери, цифри та символи ._-"))

        return username

    def clean(self):
        """Проверка совпадения паролей."""
        data = super().clean()
        password1 = data.get("password1")
        password2 = data.get("password2")

        if password1 and password2:
            if password1 != password2:
                self.add_error("password2", _("Паролі не співпадають"))

        return data


# W1-10 (NEW-502): лимит размера загружаемых файлов профиля (аватар/УБД-док)
PROFILE_UPLOAD_MAX_BYTES = 10 * 1024 * 1024  # 10 MB


def validate_profile_upload_size(uploaded_file):
    """Отклоняет файлы больше PROFILE_UPLOAD_MAX_BYTES."""
    if uploaded_file and getattr(uploaded_file, 'size', 0) > PROFILE_UPLOAD_MAX_BYTES:
        raise ValidationError(
            _("Файл завеликий. Максимальний розмір — 10 МБ.")
        )
    return uploaded_file


class ProfileSetupForm(forms.Form):
    """Форма первоначальной настройки профиля."""

    def __init__(self, *args, has_existing_ubd_doc=False, **kwargs):
        self.has_existing_ubd_doc = bool(has_existing_ubd_doc)
        super().__init__(*args, **kwargs)

    full_name = forms.CharField(
        label=_lazy("ПІБ"),
        max_length=200,
        required=False,
        widget=forms.TextInput(attrs={
            "class": "form-control bg-elevate",
            "placeholder": "Прізвище Ім'я По батькові"
        })
    )
    phone = forms.CharField(
        label=_lazy("Телефон"),
        required=True,
        widget=forms.TextInput(attrs={
            "class": "form-control bg-elevate",
            "placeholder": "0931234567",
            "autocomplete": "tel",
            "inputmode": "tel",
            "data-uk-phone-field": "1",
        })
    )
    email = forms.EmailField(
        label=_lazy("Email"),
        required=False,
        widget=forms.EmailInput(attrs={
            "class": "form-control bg-elevate",
            "placeholder": "your@email.com"
        })
    )
    telegram = forms.CharField(
        label=_lazy("Telegram"),
        required=False,
        widget=forms.TextInput(attrs={
            "class": "form-control bg-elevate",
            "placeholder": "@username"
        })
    )
    instagram = forms.CharField(
        label=_lazy("Instagram"),
        required=False,
        widget=forms.TextInput(attrs={
            "class": "form-control bg-elevate",
            "placeholder": "@username"
        })
    )
    city = forms.CharField(
        label=_lazy("Місто"),
        required=False,
        widget=forms.TextInput(attrs={
            "class": "form-control bg-elevate",
            "placeholder": "Почніть вводити місто Нової пошти",
            "autocomplete": "off",
            "spellcheck": "false",
            "data-np-city-input": "1",
            "aria-autocomplete": "list",
            "aria-expanded": "false",
        })
    )
    np_office = forms.CharField(
        label=_lazy("Відділення/Поштомат НП"),
        required=False,
        widget=forms.TextInput(attrs={
            "class": "form-control bg-elevate",
            "placeholder": "Оберіть відділення або поштомат",
            "autocomplete": "off",
            "spellcheck": "false",
            "data-np-warehouse-input": "1",
            "aria-autocomplete": "list",
            "aria-expanded": "false",
        })
    )
    np_settlement_ref = forms.CharField(
        required=False,
        widget=forms.HiddenInput(attrs={"data-np-settlement-ref": "1"}),
    )
    np_city_ref = forms.CharField(
        required=False,
        widget=forms.HiddenInput(attrs={"data-np-city-ref": "1"}),
    )
    np_city_token = forms.CharField(
        required=False,
        widget=forms.HiddenInput(attrs={"data-np-city-token": "1"}),
    )
    np_warehouse_ref = forms.CharField(
        required=False,
        widget=forms.HiddenInput(attrs={"data-np-warehouse-ref": "1"}),
    )
    np_warehouse_token = forms.CharField(
        required=False,
        widget=forms.HiddenInput(attrs={"data-np-warehouse-token": "1"}),
    )
    pay_type = forms.ChoiceField(
        label=_lazy("Тип оплати"),
        required=False,
        choices=(
            ("partial", _lazy("Часткова передоплата")),
            ("full", _lazy("Повна передоплата"))
        ),
        widget=forms.Select(attrs={"class": "form-select bg-elevate"})
    )
    avatar = forms.ImageField(label=_lazy("Аватар"), required=False)
    is_ubd = forms.BooleanField(
        label=_lazy("Я — УБД"),
        required=False,
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"})
    )
    ubd_doc = forms.ImageField(
        label=_lazy("Фото посвідчення УБД"),
        required=False
    )

    def clean_avatar(self):
        return validate_profile_upload_size(self.cleaned_data.get("avatar"))

    def clean_ubd_doc(self):
        return validate_profile_upload_size(self.cleaned_data.get("ubd_doc"))

    def clean(self):
        data = super().clean()
        if data.get("is_ubd") and not data.get("ubd_doc") and not self.has_existing_ubd_doc:
            self.add_error("ubd_doc", _("Для УБД додайте фото посвідчення"))
        try:
            delivery_selection = resolve_optional_delivery_selection(data)
        except NovaPoshtaSelectionError as exc:
            self.add_error(exc.field, exc.message)
            return data

        if delivery_selection:
            data["city"] = delivery_selection.city
            data["np_office"] = delivery_selection.np_office
            data["np_settlement_ref"] = delivery_selection.settlement_ref
            data["np_city_ref"] = delivery_selection.city_ref
            data["np_warehouse_ref"] = delivery_selection.warehouse_ref
        else:
            data["city"] = ""
            data["np_office"] = ""
            data["np_settlement_ref"] = ""
            data["np_city_ref"] = ""
            data["np_warehouse_ref"] = ""
            data["np_city_token"] = ""
            data["np_warehouse_token"] = ""
        return data

    def clean_phone(self):
        phone = normalize_checkout_phone(self.cleaned_data.get("phone", ""))
        if not phone:
            raise ValidationError(_("Введіть коректний український номер телефону. Можна без +380."))
        return phone


# ==================== VIEWS ====================

def login_view(request):
    """
    View для входа в систему.

    Features:
    - Проверяет аутентификацию
    - Переносит избранные товары из сессии в БД
    - Проверяет заполненность профиля
    - Поддерживает редирект через параметр 'next'
    """
    if request.user.is_authenticated:
        return redirect('profile_setup')

    form = LoginForm(request.POST or None)

    if request.method == 'POST' and form.is_valid():
        user = authenticate(
            request,
            username=form.cleaned_data['username'],
            password=form.cleaned_data['password']
        )

        if user:
            login(request, user)

            # Переносим избранные товары из сессии в базу данных
            session_favorites = request.session.get('favorites', [])
            if session_favorites:
                for product_id in session_favorites:
                    try:
                        product = _published_products_for_user(user).get(id=product_id)
                        FavoriteProduct.objects.get_or_create(
                            user=user,
                            product=product
                        )
                    except Product.DoesNotExist:
                        # Товар был удален, пропускаем
                        continue

                # Очищаем сессию
                del request.session['favorites']
                request.session.modified = True

            # Если профиль пустой по телефону — просим заполнить
            try:
                prof = user.userprofile
            except UserProfile.DoesNotExist:
                prof = UserProfile.objects.create(user=user)

            if not prof.phone:
                return redirect('profile_setup')

            # Проверяем параметр next для перенаправления
            next_url = request.GET.get('next') or request.POST.get('next')
            if next_url:
                return redirect(next_url)

            return redirect('home')
        else:
            form.add_error(None, _("Невірний логін або пароль"))

    return render(request, 'pages/auth_login.html', {
        'form': form,
        'next': request.GET.get('next')
    })


def register_view(request):
    """
    View для регистрации нового пользователя.

    Features:
    - Проверяет уникальность username
    - Валидирует пароль согласно Django password validators
    - ��оздает нового пользователя
    - Автоматически авторизует после регистрации
    - Переносит избранные товары из сессии
    - Редиректит на настройку профиля
    """
    if request.user.is_authenticated:
        return redirect('profile_setup')

    form = RegisterForm(request.POST or None)

    if request.method == 'POST' and form.is_valid():
        username = form.cleaned_data['username']
        password = form.cleaned_data['password1']

        # Проверяем уникальность username
        if User.objects.filter(username=username).exists():
            form.add_error('username', _('Користувач з таким логіном вже існує'))
        else:
            try:
                # КРИТИЧЕСКОЕ: Валидируем пароль ПЕРЕД созданием пользователя
                # Это предотвратит ошибки при создании пользователя
                validate_password(password, user=None)

                # Создаем пользователя
                user = User.objects.create_user(
                    username=username,
                    password=password
                )

                logger.info(f"New user registered: {username}")

                # Новый пользователь создан локальным Django backend'ом;
                # указываем его явно, чтобы регистрация не падала при
                # наличии нескольких AUTHENTICATION_BACKENDS.
                login(request, user, backend='django.contrib.auth.backends.ModelBackend')

                # Переносим избранные товары из сессии в базу данных
                session_favorites = request.session.get('favorites', [])
                if session_favorites:
                    for product_id in session_favorites:
                        try:
                            product = Product.objects.get(id=product_id)
                            FavoriteProduct.objects.get_or_create(
                                user=user,
                                product=product
                            )
                        except Product.DoesNotExist:
                            # Товар был удален, пропускаем
                            continue

                    # Очищаем сессию
                    del request.session['favorites']
                    request.session.modified = True

                return redirect('profile_setup')

            except ValidationError as e:
                # Обрабатываем ошибки валидации пароля
                logger.warning(f"Password validation failed for user {username}: {e}")

                # ValidationError может содержать список сообщений или одно сообщение
                if hasattr(e, 'error_list'):
                    for error in e.error_list:
                        form.add_error('password1', error.message)
                else:
                    # Переводим стандартные ошибки Django на украинский
                    error_messages = {
                        'This password is too short': _('Пароль занадто короткий. Мінімум 8 символів.'),
                        'This password is too common': _('Цей пароль занадто простий. Оберіть складніший пароль.'),
                        'This password is entirely numeric': _('Пароль не може складатися лише з цифр.'),
                        'The password is too similar to the': _('Пароль занадто схожий на логін.'),
                    }

                    error_text = str(e)
                    translated = False
                    for eng, ukr in error_messages.items():
                        if eng in error_text:
                            form.add_error('password1', ukr)
                            translated = True
                            break

                    if not translated:
                        # Если не нашли перевод, показываем оригинальную ошибку
                        form.add_error('password1', error_text)

            except Exception as e:
                # Логируем неожиданные ошибки
                logger.error(f"Unexpected error during registration: {e}", exc_info=True)
                form.add_error(None, _('Виникла помилка при реєстрації. Спробуйте ще раз або зверніться до підтримки.'))

    return render(request, 'pages/auth_register.html', {'form': form})


def logout_view(request):
    """
    View для выхода из системы.

    Разлогинивает пользователя и редиректит на главную.
    """
    logout(request)
    return redirect('home')
