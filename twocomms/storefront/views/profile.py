"""
Profile views - Профиль пользователя и личный кабинет.

Содержит views для:
- Просмотра и редактирования профиля
- Истории заказов
- Избранных товаров
- Истории балов
- Настройки уведомлений
"""

from django import forms
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.views.decorators.http import require_POST
from django.urls import reverse
from django.utils.translation import gettext as _

from accounts.models import UserProfile, FavoriteProduct, UserPoints, PointsHistory
from orders.models import Order
from orders.nova_poshta_checkout import NovaPoshtaSelectionError, resolve_optional_delivery_selection
from orders.nova_poshta_data import apply_nova_poshta_refs
from orders.nova_poshta_documents import normalize_checkout_phone
from ..models import Product
from .auth import ProfileSetupForm, validate_profile_upload_size


def _published_products(request):
    """
    Returns queryset limited to published products for non-staff users.
    Staff can see all items (for support/QA).
    """
    qs = Product.objects.all()
    if not (request.user.is_authenticated and request.user.is_staff):
        qs = qs.filter(status='published')
    return qs


def _apply_profile_delivery_selection(profile: UserProfile, selection) -> None:
    if selection:
        profile.city = selection.city
        profile.np_office = selection.np_office
        apply_nova_poshta_refs(
            profile,
            {
                "np_settlement_ref": selection.settlement_ref,
                "np_city_ref": selection.city_ref,
                "np_warehouse_ref": selection.warehouse_ref,
            },
        )
        return

    profile.city = ""
    profile.np_office = ""
    apply_nova_poshta_refs(profile, {})


# ==================== PROFILE VIEWS ====================

@login_required
def profile(request):
    """
    Главная страница профиля пользователя.

    Показывает:
    - Основную информацию
    - Статистику заказов
    - Текущие баллы
    - Последние заказы
    """
    try:
        user_profile = request.user.userprofile
    except UserProfile.DoesNotExist:
        user_profile = UserProfile.objects.create(user=request.user)

    # Статистика заказов
    orders = Order.objects.filter(user=request.user).order_by('-created')
    total_orders = orders.count()
    recent_orders = orders[:5]

    # Баллы
    try:
        user_points = request.user.points
    except (UserPoints.DoesNotExist, AttributeError):
        user_points = UserPoints.objects.create(user=request.user)

    # Избранное
    favorites_count = FavoriteProduct.objects.filter(user=request.user).count()

    return render(
        request,
        'pages/profile.html',
        {
            'user_profile': user_profile,
            'total_orders': total_orders,
            'recent_orders': recent_orders,
            'user_points': user_points,
            'favorites_count': favorites_count
        }
    )


@login_required
def edit_profile(request):
    """
    Редактирование профиля пользователя.

    Позволяет обновить:
    - ФИО
    - Телефон, Email
    - Адрес доставки
    - Способ оплаты
    - Аватар
    - УБД статус
    """
    try:
        user_profile = request.user.userprofile
    except UserProfile.DoesNotExist:
        user_profile = UserProfile.objects.create(user=request.user)

    if request.method == 'POST':
        # W1-10 (NEW-502): валидируем email и файлы вместо прямого присвоения
        email = (request.POST.get('email') or '').strip()
        if email:
            try:
                validate_email(email)
            except ValidationError:
                messages.error(request, _('Введіть коректний email.'))
                return redirect('profile_setup')

        # Файлы прогоняем через ImageField (тип) + лимит размера — раньше
        # request.FILES присваивались напрямую в обход любой валидации.
        avatar_file = request.FILES.get('avatar')
        ubd_doc_file = request.FILES.get('ubd_doc')
        image_field = forms.ImageField(required=False)
        try:
            avatar_file = validate_profile_upload_size(
                image_field.clean(avatar_file) if avatar_file else None
            )
            ubd_doc_file = validate_profile_upload_size(
                image_field.clean(ubd_doc_file) if ubd_doc_file else None
            )
        except ValidationError as exc:
            messages.error(request, '; '.join(exc.messages))
            return redirect('profile_setup')

        phone = normalize_checkout_phone(request.POST.get('phone', ''))
        if not phone:
            messages.error(request, _('Введіть коректний український номер телефону. Можна без +380.'))
            return redirect('profile_setup')

        try:
            delivery_selection = resolve_optional_delivery_selection(request.POST)
        except NovaPoshtaSelectionError as exc:
            messages.error(request, exc.message)
            return redirect('profile_setup')

        # Обновляем основные данные пользователя
        request.user.first_name = request.POST.get('first_name', '')[:150]
        request.user.last_name = request.POST.get('last_name', '')[:150]
        request.user.email = email
        request.user.save()

        # Обновляем профиль
        user_profile.full_name = request.POST.get('full_name', '')[:200]
        user_profile.phone = phone
        user_profile.email = email
        user_profile.telegram = request.POST.get('telegram', '')
        user_profile.instagram = request.POST.get('instagram', '')
        _apply_profile_delivery_selection(user_profile, delivery_selection)
        user_profile.pay_type = request.POST.get('pay_type', 'full')

        # Аватар
        if avatar_file:
            user_profile.avatar = avatar_file

        # УБД
        user_profile.is_ubd = 'is_ubd' in request.POST
        if ubd_doc_file:
            user_profile.ubd_doc = ubd_doc_file

        user_profile.save()

        return redirect('home')

    return render(
        request,
        'pages/edit_profile.html',
        {'user_profile': user_profile}
    )


@login_required
def profile_setup(request):
    """
    Первоначальная настройка профиля после регистрации.

    Обязательные поля:
    - Телефон

    Опциональные:
    - ФИО, Email, Telegram, Instagram
    - Адрес доставки
    """
    try:
        prof = request.user.userprofile
    except UserProfile.DoesNotExist:
        prof = UserProfile.objects.create(user=request.user)

    if request.method == 'POST' and request.POST.get('form_type') == 'push_preferences':
        prof.push_marketing_enabled = 'push_marketing_enabled' in request.POST
        prof.push_order_updates_enabled = 'push_order_updates_enabled' in request.POST
        prof.save(update_fields=['push_marketing_enabled', 'push_order_updates_enabled'])
        messages.success(request, _('Налаштування push-сповіщень оновлено.'))
        return redirect(f"{reverse('profile_setup')}#push-preferences")

    if request.method == 'POST':
        form = ProfileSetupForm(
            request.POST,
            request.FILES,
            has_existing_ubd_doc=bool(prof.ubd_doc),
        )
        if form.is_valid():
            delivery_selection = resolve_optional_delivery_selection(form.cleaned_data)
            prof.full_name = form.cleaned_data.get('full_name', '')
            prof.phone = form.cleaned_data['phone']
            prof.email = form.cleaned_data.get('email', '')
            prof.telegram = form.cleaned_data.get('telegram', '')
            prof.instagram = form.cleaned_data.get('instagram', '')
            _apply_profile_delivery_selection(prof, delivery_selection)
            prof.pay_type = form.cleaned_data.get('pay_type', 'full')
            prof.is_ubd = form.cleaned_data.get('is_ubd', False)

            if form.cleaned_data.get('avatar'):
                prof.avatar = form.cleaned_data['avatar']
            if form.cleaned_data.get('ubd_doc'):
                prof.ubd_doc = form.cleaned_data['ubd_doc']

            prof.save()

            # Обновляем email пользователя
            if form.cleaned_data.get('email'):
                request.user.email = form.cleaned_data['email']
                request.user.save()

            messages.success(request, _('Профіль оновлено.'))
            return redirect('profile_setup')
    else:
        initial = {
            'full_name': prof.full_name,
            'phone': prof.phone,
            'email': prof.email or request.user.email,
            'telegram': prof.telegram,
            'instagram': prof.instagram,
            'city': prof.city,
            'np_office': prof.np_office,
            'np_settlement_ref': prof.np_settlement_ref,
            'np_city_ref': prof.np_city_ref,
            'np_warehouse_ref': prof.np_warehouse_ref,
            'pay_type': prof.pay_type,
            'is_ubd': prof.is_ubd,
        }
        form = ProfileSetupForm(initial=initial, has_existing_ubd_doc=bool(prof.ubd_doc))

    return render(
        request,
        'pages/auth_profile_setup.html',
        {
            'form': form,
            'profile': prof,
            'auth_providers': _auth_providers_context(request.user, prof),
        },
    )


def _auth_providers_context(user, profile):
    """Повертає опис всіх методів авторизації / привʼязок для UI карток.

    Кожен elementaru: {key, label, status, meta, icon, ...}
    Викликається з шаблону профілю.
    """
    providers = []

    # 1) Логін/пароль
    has_password = user.has_usable_password()
    providers.append({
        'key': 'password',
        'label': _('Логін і пароль'),
        'connected': has_password,
        'meta': user.username if has_password else _('Пароль не встановлено'),
        'description': _('Звичайний вхід на сайт за логіном і паролем.'),
        'icon_class': 'fa-solid fa-key',
        'action_url': reverse('profile_setup') + '#set-password',
        'action_label': _('Змінити пароль') if has_password else _('Встановити пароль'),
    })

    # 2) Google
    google_meta = ''
    google_connected = False
    try:
        social_auth = user.social_auth.filter(provider='google-oauth2').first()
        if social_auth:
            google_connected = True
            extra = social_auth.extra_data or {}
            google_meta = (
                extra.get('email')
                or extra.get('name')
                or social_auth.uid
                or _('Підключено')
            )
    except Exception:
        google_connected = False

    providers.append({
        'key': 'google',
        'label': _('Google'),
        'connected': google_connected,
        'meta': google_meta if google_connected else _('Не підключено'),
        'description': _('Швидкий вхід через ваш Google-акаунт.'),
        'icon_url': '/static/img/google.svg',
        'action_url': '/oauth/login/google-oauth2/?next=/profile/setup/',
        'action_label': _('Підключити') if not google_connected else _('Перепідключити'),
        'is_oauth_link': True,
    })

    # 3) Telegram
    tg_connected = bool(profile.telegram_id)
    if tg_connected:
        bits = []
        if profile.telegram:
            bits.append(profile.telegram if profile.telegram.startswith('@') else f"@{profile.telegram}")
        if profile.phone:
            bits.append(profile.phone)
        tg_meta = ' · '.join(bits) if bits else _('Підключено')
    else:
        tg_meta = _('Не підключено')

    providers.append({
        'key': 'telegram',
        'label': _('Telegram'),
        'connected': tg_connected,
        'meta': tg_meta,
        'description': _('Сповіщення про замовлення в Telegram + швидкий вхід без паролю.'),
        'icon_class': 'fa-brands fa-telegram',
        'action_label': _('Підключити Telegram') if not tg_connected else _("Перепривʼязати"),
        'is_telegram': True,
    })

    return providers


@login_required
def order_history(request):
    """
    История заказов пользователя.

    Показывает все заказы с возможностью фильтрации по статусу.
    """
    status_filter = request.GET.get('status', '')

    orders = Order.objects.filter(user=request.user).order_by('-created').prefetch_related(
        'items__product',
        'items__color_variant__color',
        'items__color_variant__images',
        'custom_print_leads',
    )

    if status_filter:
        orders = orders.filter(status=status_filter)

    return render(
        request,
        'pages/my_orders.html',
        {
            'orders': orders,
            'status_filter': status_filter
        }
    )


@login_required
def order_detail(request, order_number):
    """
    Детальная информация о заказе.

    Args:
        order_number (str): Номер заказа
    """
    queryset = Order.objects.prefetch_related(
        'items__product',
        'items__color_variant__color',
        'items__color_variant__images',
        'custom_print_leads',
    )
    order = get_object_or_404(
        queryset,
        order_number=order_number,
        user=request.user
    )

    return render(
        request,
        'pages/order_detail.html',
        {'order': order}
    )


def favorites(request):
    """
    Список избранных товаров пользователя.
    Поддерживает как авторизованных, так и неавторизованных пользователей.
    """
    if request.user.is_authenticated:
        # Для авторизованных пользователей - получаем из базы данных
        favorites = FavoriteProduct.objects.filter(
            user=request.user
        ).select_related('product', 'product__category').prefetch_related(
            'product__color_variants__color'
        )
    else:
        # Для неавторизованных пользователей - получаем из сессии
        session_favorites = request.session.get('favorites', [])
        favorites = []

        if session_favorites:
            # Получаем товары по ID из сессии
            products = _published_products(request).filter(
                id__in=session_favorites
            ).select_related('category').prefetch_related(
                'color_variants__color'
            )

            # Создаем объекты, похожие на FavoriteProduct
            for product in products:
                favorite = type('FavoriteProduct', (), {
                    'product': product,
                    'color_variants_data': []
                })()
                favorites.append(favorite)

    # Получаем варианты цветов для избранных товаров
    for favorite in favorites:
        try:
            # Используем all() вместо filter(), так как данные уже предзагружены
            variants = favorite.product.color_variants.all()
            # Создаем список словарей с данными о цветах
            color_variants_data = [
                {
                    'primary_hex': v.color.primary_hex,
                    'secondary_hex': v.color.secondary_hex or '',
                }
                for v in variants
            ]
            # Добавляем как атрибут к объекту favorite
            favorite.color_variants_data = color_variants_data
        except Exception:
            favorite.color_variants_data = []

    return render(request, 'pages/favorites.html', {
        'favorites': favorites,
        'title': 'Обрані товари'
    })


# Алиас для обратной совместимости
favorites_list = favorites


def toggle_favorite(request, product_id):
    """
    Добавить/удалить товар из избранного.
    Универсальная функция для авторизованных и неавторизованных пользователей.
    """
    try:
        product = get_object_or_404(_published_products(request), id=product_id)

        if request.user.is_authenticated:
            # Для авторизованных пользователей - используем базу данных
            favorite, created = FavoriteProduct.objects.get_or_create(
                user=request.user,
                product=product
            )

            if not created:
                # Если запись уже существует, удаляем её
                favorite.delete()
                is_favorite = False
                message = 'Товар видалено з обраного'
            else:
                is_favorite = True
                message = 'Товар додано до обраного'
        else:
            # Для неавторизованных пользователей - используем сессии
            session_favorites = request.session.get('favorites', [])
            # Преобразуем product_id в int для корректного сравнения
            product_id_int = int(product_id)

            if product_id_int in session_favorites:
                # Удаляем из избранного
                session_favorites.remove(product_id_int)
                is_favorite = False
                message = 'Товар видалено з обраного'
            else:
                # Добавляем в избранное
                session_favorites.append(product_id_int)
                is_favorite = True
                message = 'Товар додано до обраного'

            # Сохраняем в сессии
            request.session['favorites'] = session_favorites
            request.session.modified = True

        # Подсчитываем общее количество избранных товаров
        if request.user.is_authenticated:
            favorites_count_val = FavoriteProduct.objects.filter(user=request.user).count()
        else:
            favorites_count_val = len(request.session.get('favorites', []))

        return JsonResponse({
            'success': True,
            'is_favorite': is_favorite,
            'message': message,
            'favorites_count': favorites_count_val
        })
    except Exception as e:
        return JsonResponse({
            'success': False,
            'message': _('Помилка: ') + str(e)
        }, status=400)


# Алиасы для обратной совместимости с разными API
@login_required
@require_POST
def add_to_favorites(request, product_id):
    """Добавление товара в избранное (только для авторизованных)."""
    try:
        product = _published_products(request).get(id=product_id)
        favorite, created = FavoriteProduct.objects.get_or_create(
            user=request.user,
            product=product
        )
        if created:
            return JsonResponse({
                'success': True,
                'message': f'Товар "{product.title}" додано до обраного'
            })
        else:
            return JsonResponse({
                'success': True,
                'message': _('Товар вже в обраному')
            })
    except Product.DoesNotExist:
        return JsonResponse({
            'success': False,
            'error': 'Товар не знайдено'
        }, status=404)


@login_required
@require_POST
def remove_from_favorites(request, product_id):
    """Удаление товара из избранного (только для авторизованных)."""
    try:
        favorite = FavoriteProduct.objects.get(
            user=request.user,
            product_id=product_id
        )
        favorite.delete()
        return JsonResponse({
            'success': True,
            'message': _('Товар видалено з обраного')
        })
    except FavoriteProduct.DoesNotExist:
        return JsonResponse({
            'success': False,
            'error': 'Товар не знайдено в обраному'
        }, status=404)


def check_favorite_status(request, product_id):
    """Проверить статус избранного для товара."""
    try:
        if request.user.is_authenticated:
            # Для авторизованных пользователей - проверяем в базе данных
            is_favorite = FavoriteProduct.objects.filter(
                user=request.user,
                product_id=product_id
            ).exists()
        else:
            # Для неавторизованных пользователей - проверяем в сессии
            session_favorites = request.session.get('favorites', [])
            product_id_int = int(product_id)
            is_favorite = product_id_int in session_favorites

        return JsonResponse({'is_favorite': is_favorite})
    except Exception:
        return JsonResponse({'is_favorite': False})


def favorites_count(request):
    """Получить количество товаров в избранном."""
    try:
        if request.user.is_authenticated:
            # Для авторизованных пользователей - считаем в базе данных
            count = FavoriteProduct.objects.filter(user=request.user).count()
        else:
            # Для неавторизованных пользователей - считаем в сессии
            session_favorites = request.session.get('favorites', [])
            count = len(session_favorites)

        return JsonResponse({'count': count})
    except Exception as e:
        return JsonResponse({'count': 0, 'error': str(e)})


@login_required
def points_history(request):
    """
    История транзакций балов пользователя.
    """
    try:
        user_points = request.user.points
    except (UserPoints.DoesNotExist, AttributeError):
        user_points = UserPoints.objects.create(user=request.user)

    history = PointsHistory.objects.filter(user=request.user).order_by('-created_at')

    return render(
        request,
        'pages/points_history.html',
        {
            'user_points': user_points,
            'history': history
        }
    )


@login_required
def settings(request):
    """
    Настройки аккаунта.

    Позволяет изменить:
    - Пароль
    - Email
    - Уведомления
    """
    if request.method == 'POST':
        action = request.POST.get('action')

        if action == 'change_password':
            old_password = request.POST.get('old_password')
            new_password = request.POST.get('new_password')

            if request.user.check_password(old_password):
                request.user.set_password(new_password)
                request.user.save()

                from django.contrib.auth import update_session_auth_hash
                update_session_auth_hash(request, request.user)

                return JsonResponse({
                    'success': True,
                    'message': _('Пароль успішно змінено')
                })
            else:
                return JsonResponse({
                    'success': False,
                    'error': 'Невірний старий пароль'
                }, status=400)

    return render(request, 'pages/settings.html')


# ==================== USER POINTS & REWARDS ====================

@login_required
def user_points(request):
    """
    Страница баллов пользователя.
    Показывает текущий баланс и историю начислений/списаний.
    """
    from accounts.models import UserPoints, PointsHistory

    # Получаем или создаем объект баллов пользователя
    try:
        user_points_obj = UserPoints.get_or_create_points(request.user)
    except AttributeError:
        # Если метод get_or_create_points не существует, используем get_or_create
        user_points_obj, _ = UserPoints.objects.get_or_create(user=request.user)

    # Получаем историю баллов (последние 20 записей)
    history = PointsHistory.objects.filter(user=request.user).order_by('-created_at')[:20]

    return render(request, 'pages/user_points.html', {
        'user_points': user_points_obj,
        'history': history
    })


@login_required
def my_promocodes(request):
    """
    Страница промокодов пользователя.
    Показывает использованные промокоды в заказах.
    """
    from orders.models import Order
    from django.utils import timezone
    from ..models import UserPromoCode, PromoCodeUsage

    # Получаем все заказы пользователя с промокодами
    orders_with_promocodes = Order.objects.filter(
        user=request.user,
        promo_code__isnull=False
    ).select_related('promo_code').order_by('-created')

    # Создаем список использованных промокодов
    used_promocodes = []
    for order in orders_with_promocodes:
        if order.promo_code:
            used_promocodes.append({
                'promo_code': order.promo_code,
                'order': order,
                'used_date': order.created,
                'discount_amount': order.discount_amount,
                'order_total': order.total_sum
            })

    granted = UserPromoCode.objects.filter(user=request.user).select_related('promo_code')
    used_promo_ids = set(
        PromoCodeUsage.objects.filter(user=request.user, promo_code__in=[g.promo_code for g in granted])
        .values_list('promo_code_id', flat=True)
    )

    active_promocodes = []
    expired_promocodes = []
    now = timezone.now()
    for grant in granted:
        promo = grant.promo_code
        is_used = promo.id in used_promo_ids
        is_valid = promo.is_active and promo.is_valid_now()
        entry = {
            'promo_code': promo,
            'expires_at': promo.valid_until or grant.expires_at,
            'is_used': is_used,
        }
        if is_used:
            continue
        if is_valid:
            active_promocodes.append(entry)
        else:
            expired_promocodes.append(entry)

    return render(request, 'pages/my_promocodes.html', {
        'used_promocodes': used_promocodes,
        'active_promocodes': active_promocodes,
        'expired_promocodes': expired_promocodes,
    })


@login_required
def buy_with_points(request):
    """
    Страница покупки за баллы.
    Показывает доступные товары/услуги за баллы лояльности.
    """
    from accounts.models import UserPoints

    # Получаем баллы пользователя
    try:
        user_points_obj = UserPoints.objects.get(user=request.user)
        current_points = user_points_obj.points
    except UserPoints.DoesNotExist:
        current_points = 0

    # Определяем доступные товары за баллы
    available_items = [
        {
            'id': 'promo_10',
            'name': 'Промокод на знижку 10%',
            'description': 'Отримайте промокод на знижку 10% від суми замовлення',
            'points_cost': 100,
            'type': 'promo',
            'icon': 'M21.41 11.58l-9-9C12.05 2.22 11.55 2 11 2H4c-1.1 0-2 .9-2 2v7c0 .55.22 1.05.59 1.42l9 9c.36.36.86.58 1.41.58.55 0 1.05-.22 1.41-.59l7-7c.37-.36.59-.86.59-1.41 0-.55-.23-1.06-.59-1.42zM5.5 7C4.67 7 4 6.33 4 5.5S4.67 4 5.5 4 7 4.67 7 5.5 6.33 7 5.5 7z',
            'color': 'linear-gradient(135deg, #3b82f6, #1d4ed8)',
            'can_afford': current_points >= 100
        },
        {
            'id': 'donate_zsu',
            'name': 'Донат на ЗСУ',
            'description': 'Пожертвуйте всі ваші бали на підтримку Збройних Сил України',
            'points_cost': current_points,
            'type': 'donate',
            'icon': 'M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01L12 2z',
            'color': 'linear-gradient(135deg, #dc2626, #991b1b)',
            'can_afford': current_points > 0
        }
    ]

    return render(request, 'pages/buy_with_points.html', {
        'current_points': current_points,
        'available_items': available_items
    })


@login_required
@require_POST
def purchase_with_points(request):
    """
    Обработка покупки за баллы.
    Списывает баллы и выдает соответствующую награду.
    """
    from accounts.models import UserPoints
    from django.contrib import messages

    item_id = request.POST.get('item_id')

    try:
        user_points_obj = UserPoints.objects.get(user=request.user)
    except UserPoints.DoesNotExist:
        messages.error(request, _('У вас немає балів для покупки'))
        return redirect('buy_with_points')

    if item_id == 'promo_10':
        # Покупка промокода на 10% скидки
        if user_points_obj.points >= 100:
            # Здесь будет логика создания промокода
            # Пока что просто списываем баллы
            try:
                user_points_obj.spend_points(100, 'Покупка промокода на знижку 10%')
                messages.success(
                    request,
                    _('Промокод на знижку 10% успішно придбано! Код: POINTS10')
                )
            except Exception as e:
                messages.error(request, _('Помилка при списанні балів: %(error)s') % {'error': e})
        else:
            messages.error(request, _('Недостатньо балів для покупки промокода'))

    elif item_id == 'donate_zsu':
        # Донат на ЗСУ
        if user_points_obj.points > 0:
            points_to_donate = user_points_obj.points
            try:
                user_points_obj.spend_points(points_to_donate, 'Донат на ЗСУ')
                messages.success(
                    request,
                    _('Дякуємо за підтримку ЗСУ! Пожертвовано %(points)s балів') % {'points': points_to_donate}
                )
            except Exception as e:
                messages.error(request, _('Помилка при пожертвуванні: %(error)s') % {'error': e})
        else:
            messages.error(request, _('У вас немає балів для пожертвування'))

    else:
        messages.error(request, _('Невідомий товар'))

    return redirect('buy_with_points')
