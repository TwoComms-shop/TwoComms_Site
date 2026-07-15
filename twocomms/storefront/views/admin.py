"""
Admin views - Административная панель.

Содержит views для:
- Управления товарами
- Управления категориями
- Управления промокодами
- Генерации контента (AI, SEO)
- Статистики и отчетов
- Дропшипинг панель
"""

from datetime import timedelta

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.conf import settings
from django.db import models, transaction
from django.db.models import (
    Avg,
    Case,
    Count,
    DurationField,
    ExpressionWrapper,
    F,
    IntegerField,
    Prefetch,
    Q,
    Sum,
    Value,
    When,
)
from django.http import JsonResponse, HttpResponse
from django.template.loader import render_to_string
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify

import json
import os

from orders.nova_poshta_documents import (
    build_order_payment_snapshot,
    canonicalize_payment_status,
    get_payment_status_label,
)
from ..models import (
    OfflineStore,
    PageView,
    Product,
    ProductImage,
    ProductStatus,
    Category,
    PromoCode,
    PromoCodeUsage,
    PrintProposal,
    CustomPrintBusinessKind,
    CustomPrintClientKind,
    CustomPrintLead,
    CustomPrintLeadStatus,
    CustomPrintModerationStatus,
    CustomPrintProductType,
    Catalog,
    PushNotificationCampaign,
    PushNotificationDelivery,
    SizeGrid,
    SiteSession,
    WebPushDeviceSubscription,
)
from ..forms import (
    ProductForm,
    ProductSEOForm,
    CategoryForm,
    PushNotificationCampaignForm,
    PrintProposalForm,
    SizeGridForm,
    CatalogOptionFormSet,
    build_color_variant_formset,
    build_product_fit_option_formset,
    ProductFitToggleForm,
    ensure_default_fit_options_for_tshirt,
)
from .utils import unique_slugify
from accounts.models import FavoriteProduct, UserPoints, UserProfile
from orders.models import (
    DropshipperOrder,
    DropshipperPayout,
    DropshipperStats,
    Order,
    OrderItem,
    WholesaleInvoice,
)
from .promo import get_promo_admin_context
from storefront.analytics_exclusions import (
    order_exclusion_q,
    pageview_exclusion_q,
    session_exclusion_q,
)
from storefront.services.catalog import (
    append_product_gallery,
    formset_to_variant_payloads,
    sync_variant_images,
)
from storefront.services.admin_analytics import (
    build_admin_analytics_context,
    build_product_admin_metrics,
)
from storefront.services.catalog_helpers import bump_public_product_order_version
from storefront.services.web_push import (
    WebPushConfigurationError,
    get_default_notification_icon_url,
    is_web_push_configured,
    send_campaign,
)
from storefront.services.indexnow import (
    get_category_public_url,
    get_core_indexnow_urls,
    get_product_public_url,
    is_indexnow_configured,
    submit_indexnow_urls,
)
from storefront.utm_tracking import ensure_order_purchase_action
from storefront.services.google_indexing import (
    NOTIFICATION_URL_DELETED,
    NOTIFICATION_URL_UPDATED,
    get_daily_quota,
    get_google_indexing_status,
    get_quota_summary,
    get_quota_window_hours,
    get_recent_submissions,
    get_quota_summary,
    get_urls_already_submitted_today,
    get_urls_successful_in_last_days,
    get_urls_successful_in_last_hours,
    is_google_indexing_configured,
    submit_google_indexing_urls,
)
from storefront.services.index_targets import (
    ALL_GROUPS,
    GROUP_LABELS,
    build_targets,
    get_default_language,
    get_supported_languages,
)


# ==================== ADMIN VIEWS ====================


def _resolve_period(period_param):
    """Возвращает параметры периода (дата начала/конца и метка)."""
    valid_periods = {'today', 'week', 'month', 'all_time'}
    period = period_param if period_param in valid_periods else 'today'
    today = timezone.localdate()

    if period == 'today':
        start_date = today
        end_date = today
        period_name = 'Сьогодні'
    elif period == 'week':
        start_date = today - timedelta(days=7)
        end_date = today
        period_name = 'За тиждень'
    elif period == 'month':
        start_date = today - timedelta(days=30)
        end_date = today
        period_name = 'За місяць'
    else:
        start_date = None
        end_date = None
        period_name = 'За весь час'

    return {
        'period': period,
        'period_name': period_name,
        'start_date': start_date,
        'end_date': end_date,
        'today': today,
    }


def _format_discount_display(promo):
    """Генерирует строку отображения скидки для промокода."""
    if promo.discount_type == 'percentage':
        return f'{promo.discount_value}%'
    return f'{promo.discount_value} ₴'


def _build_stats(period_param):
    """Собирает метрики для дашборда админки."""
    period_info = _resolve_period(period_param)
    period = period_info['period']
    start_date = period_info['start_date']
    end_date = period_info['end_date']
    today = period_info['today']

    stats = {
        'orders_today': 0,
        'orders_count': 0,
        'revenue_today': 0,
        'avg_order_value': 0,
        'total_users': 0,
        'new_users_today': 0,
        'active_users_today': 0,
        'active_users_period': 0,
        'total_products': 0,
        'total_categories': 0,
        'print_proposals_pending': 0,
        'promocodes_used_today': 0,
        'total_points_earned': 0,
        'users_with_points': 0,
        'favorites_count': 0,
        'online_users': 0,
        'unique_visitors_today': 0,
        'page_views_today': 0,
        'page_views_period': 0,
        'sessions_period': 0,
        'bounce_rate': 0,
        'avg_session_duration': 0,
        'conversion_rate': 0,
        'products_sold_today': 0,
        'abandoned_carts': 0,
        'reviews_count': 0,
        'avg_rating': 0,
        'period': period,
        'period_name': period_info['period_name'],
        'start_date': start_date,
        'end_date': end_date,
    }

    try:
        if start_date and end_date:
            orders_qs = Order.objects.filter(created__date__range=[start_date, end_date])
            users_qs = User.objects.filter(date_joined__date__range=[start_date, end_date])
            promo_usage_qs = PromoCodeUsage.objects.filter(used_at__date__range=[start_date, end_date])
            order_items_qs = OrderItem.objects.filter(order__created__date__range=[start_date, end_date])
            page_views_qs = PageView.objects.filter(when__date__range=[start_date, end_date], is_bot=False)
            sessions_qs = SiteSession.objects.filter(first_seen__date__range=[start_date, end_date], is_bot=False)
        else:
            orders_qs = Order.objects.all()
            users_qs = User.objects.all()
            promo_usage_qs = PromoCodeUsage.objects.all()
            order_items_qs = OrderItem.objects.all()
            page_views_qs = PageView.objects.filter(is_bot=False)
            sessions_qs = SiteSession.objects.filter(is_bot=False)

        # Apply admin-managed exclusions (offices, staff, bot agents) before counting.
        site_excl = session_exclusion_q()
        if site_excl:
            sessions_qs = sessions_qs.exclude(site_excl)
        pv_excl = pageview_exclusion_q()
        if pv_excl:
            page_views_qs = page_views_qs.exclude(pv_excl)
        order_excl = order_exclusion_q()
        if order_excl:
            orders_qs = orders_qs.exclude(order_excl)
            order_items_qs = order_items_qs.exclude(
                order__in=Order.objects.filter(order_excl)
            )

        stats['orders_count'] = orders_qs.count()
        stats['orders_today'] = Order.objects.filter(created__date=today).count()

        paid_orders_qs = orders_qs.filter(payment_status='paid')
        stats['revenue_today'] = paid_orders_qs.aggregate(total=Sum('total_sum'))['total'] or 0
        stats['avg_order_value'] = round(
            (paid_orders_qs.aggregate(avg=Avg('total_sum'))['avg'] or 0), 2
        )

        stats['total_users'] = User.objects.count()
        stats['registered_today'] = User.objects.filter(date_joined__date=today).count()
        stats['new_users_today'] = users_qs.count()
        stats['active_users_today'] = User.objects.filter(last_login__date=today).count()
        if start_date and end_date:
            stats['active_users_period'] = User.objects.filter(
                last_login__date__range=[start_date, end_date]
            ).count()
        else:
            stats['active_users_period'] = User.objects.filter(last_login__isnull=False).count()

        stats['total_products'] = Product.objects.count()
        stats['total_categories'] = Category.objects.count()
        stats['print_proposals_pending'] = PrintProposal.objects.filter(status='pending').count()

        stats['promocodes_used_today'] = PromoCodeUsage.objects.filter(used_at__date=today).count()

        stats['total_points_earned'] = (
            UserPoints.objects.aggregate(total=Sum('points'))['total'] or 0
        )
        stats['users_with_points'] = UserPoints.objects.filter(points__gt=0).count()

        stats['favorites_count'] = FavoriteProduct.objects.count()

        online_threshold = timezone.now() - timedelta(minutes=5)
        online_qs = SiteSession.objects.filter(last_seen__gte=online_threshold, is_bot=False)
        unique_today_qs = SiteSession.objects.filter(first_seen__date=today, is_bot=False)
        page_views_today_qs = PageView.objects.filter(when__date=today, is_bot=False)
        today_sessions = SiteSession.objects.filter(first_seen__date=today, is_bot=False)
        if site_excl:
            online_qs = online_qs.exclude(site_excl)
            unique_today_qs = unique_today_qs.exclude(site_excl)
            today_sessions = today_sessions.exclude(site_excl)
        if pv_excl:
            page_views_today_qs = page_views_today_qs.exclude(pv_excl)
        stats['online_users'] = online_qs.count()
        stats['unique_visitors_today'] = unique_today_qs.count()
        stats['page_views_today'] = page_views_today_qs.count()

        stats['page_views_period'] = page_views_qs.count()
        stats['sessions_period'] = sessions_qs.count()

        if today_sessions.exists():
            single_page_sessions = today_sessions.filter(pageviews__lte=1).count()
            stats['bounce_rate'] = round(
                single_page_sessions / today_sessions.count() * 100, 2
            )
            # Bug fix: only count sessions with measurable duration so a flood of
            # single-page sessions does not dilute the average toward zero.
            durations = list(
                today_sessions.annotate(
                    dur=ExpressionWrapper(F('last_seen') - F('first_seen'), output_field=DurationField())
                ).values_list('dur', flat=True)
            )
            valid_seconds = [d.total_seconds() for d in durations if d and d.total_seconds() > 0]
            if valid_seconds:
                stats['avg_session_duration'] = int(sum(valid_seconds) / len(valid_seconds))
            else:
                stats['avg_session_duration'] = 0

        if stats['sessions_period']:
            stats['conversion_rate'] = round(
                stats['orders_count'] / stats['sessions_period'] * 100, 2
            )

        stats['products_sold_today'] = (
            order_items_qs.aggregate(total=Sum('qty'))['total'] or 0
        )

    except Exception:
        # В случае ошибки оставляем значения по умолчанию
        pass

    return stats


def _build_push_notifications_context(request, form=None):
    active_subscriptions = WebPushDeviceSubscription.objects.filter(is_active=True)
    installation_count = (
        active_subscriptions.exclude(installation_id="")
        .values("installation_id")
        .distinct()
        .count()
    )
    anonymous_without_install_id = active_subscriptions.filter(installation_id="").count()

    campaigns = (
        PushNotificationCampaign.objects.select_related("created_by")
        .order_by("-created_at")[:20]
    )
    deliveries = PushNotificationDelivery.objects.select_related("campaign", "subscription")

    device_breakdown = list(
        active_subscriptions.values("device_type")
        .annotate(total=Count("id"))
        .order_by("-total", "device_type")
    )

    recent_performance = {
        "sent": deliveries.filter(sent_at__isnull=False).count(),
        "displayed": deliveries.filter(displayed_at__isnull=False).count(),
        "clicked": deliveries.filter(clicked_at__isnull=False).count(),
        "failed": deliveries.filter(failed_at__isnull=False).count(),
    }

    return {
        "push_form": form or PushNotificationCampaignForm(),
        "push_campaigns": campaigns,
        "push_metrics": {
            "active_subscriptions": active_subscriptions.count(),
            "active_installations": installation_count + anonymous_without_install_id,
            "known_users": active_subscriptions.filter(user__isnull=False).values("user_id").distinct().count(),
            "recent_performance": recent_performance,
            "device_breakdown": device_breakdown,
        },
        "web_push_enabled": is_web_push_configured(),
        "web_push_public_key": getattr(settings, "WEB_PUSH_VAPID_PUBLIC_KEY", ""),
        "web_push_subject": getattr(settings, "WEB_PUSH_VAPID_SUBJECT", ""),
        "web_push_default_icon_url": get_default_notification_icon_url(),
        "web_push_missing_settings": [
            name
            for name, value in (
                ("WEB_PUSH_VAPID_PUBLIC_KEY", getattr(settings, "WEB_PUSH_VAPID_PUBLIC_KEY", "")),
                ("WEB_PUSH_VAPID_PRIVATE_KEY", getattr(settings, "WEB_PUSH_VAPID_PRIVATE_KEY", "")),
                ("WEB_PUSH_VAPID_SUBJECT", getattr(settings, "WEB_PUSH_VAPID_SUBJECT", "")),
            )
            if not value
        ],
        "web_push_env_file_hint": ".env.production",
    }


def _build_orders_context(request):
    """Формирует данные для секции заказов."""
    status_filter = request.GET.get('status', 'all')
    payment_filter_raw = request.GET.get('payment', 'all')
    payment_filter = (
        canonicalize_payment_status(payment_filter_raw)
        if payment_filter_raw != 'all'
        else 'all'
    )
    user_id_filter = request.GET.get('user_id')

    orders_qs = (
        Order.objects.select_related('user')
        .prefetch_related(
            Prefetch('items', queryset=OrderItem.objects.select_related('product')),
            'custom_print_leads',
        )
        .order_by('-created')
    )

    if status_filter != 'all':
        orders_qs = orders_qs.filter(status=status_filter)
    if payment_filter != 'all':
        if payment_filter == 'prepaid':
            orders_qs = orders_qs.filter(payment_status__in=['prepaid', 'partial'])
        else:
            orders_qs = orders_qs.filter(payment_status=payment_filter)

    user_filter_info = None
    if user_id_filter:
        orders_qs = orders_qs.filter(user_id=user_id_filter)
        try:
            target_user = User.objects.get(pk=user_id_filter)
            full_name = getattr(getattr(target_user, 'userprofile', None), 'full_name', None)
            user_filter_info = {
                'username': target_user.username,
                'full_name': full_name,
            }
        except User.DoesNotExist:
            user_filter_info = None

    status_counts = {
        code: Order.objects.filter(status=code).count()
        for code, _ in Order.STATUS_CHOICES
    }

    payment_status_counts = {
        'unpaid': Order.objects.filter(payment_status='unpaid').count(),
        'checking': Order.objects.filter(payment_status='checking').count(),
        'prepaid': Order.objects.filter(payment_status__in=['prepaid', 'partial']).count(),
        'paid': Order.objects.filter(payment_status='paid').count(),
    }

    orders = list(orders_qs)
    for order in orders:
        snapshot = build_order_payment_snapshot(order)
        payload = order.payment_payload or {}
        history = payload.get('history') or []
        last_entry = history[-1] if history else {}
        order.payment_status_canonical = snapshot['payment_status']
        order.payment_status_label = snapshot['payment_status_label']
        order.payment_snapshot = snapshot
        order.payment_last_status = last_entry.get('status') or payload.get('last_status')
        order.payment_last_time = last_entry.get('received_at') or last_entry.get('ts') or payload.get('last_update_at')
        order.payment_history_safe = history[-10:]

    return {
        'orders': orders,
        'status_counts': status_counts,
        'payment_status_counts': payment_status_counts,
        'total_orders': Order.objects.count(),
        'status_filter': status_filter,
        'payment_filter': payment_filter,
        'user_filter_info': user_filter_info,
    }


def _build_users_context():
    """Собирает данные для вкладки пользователей."""
    from storefront.models import SiteSession  # local import to avoid cycles

    users_qs = (
        User.objects.select_related('userprofile', 'points')
        .prefetch_related('orders', 'orders__items')
        .order_by('username')
    )
    users = list(users_qs)
    user_ids = [u.id for u in users]

    promo_usage_map = {}
    usage_qs = (
        PromoCodeUsage.objects.filter(user_id__in=user_ids)
        .select_related('promo_code', 'order', 'group')
        .order_by('-used_at')
    )
    for usage in usage_qs:
        promo_usage_map.setdefault(usage.user_id, []).append(usage)

    payment_status_template = {
        'unpaid': 0,
        'checking': 0,
        'prepaid': 0,
        'paid': 0,
    }

    order_status_template = {code: 0 for code, _ in Order.STATUS_CHOICES}

    # Online — пользователи, у которых last_seen ≥ now−5min хоть в одной сессии.
    online_threshold = timezone.now() - timedelta(minutes=5)
    online_user_ids = set(
        SiteSession.objects.filter(
            user_id__in=user_ids,
            last_seen__gte=online_threshold,
            is_bot=False,
        ).values_list('user_id', flat=True)
    )
    last_seen_map = {
        row['user_id']: row['last_seen']
        for row in SiteSession.objects.filter(user_id__in=user_ids)
        .values('user_id')
        .annotate(last_seen=models.Max('last_seen'))
    } if user_ids else {}

    # Привʼязка соц. провайдерів (Google) — щоб показувати в карточках
    social_map = {}
    try:
        from social_django.models import UserSocialAuth

        for sa in UserSocialAuth.objects.filter(user_id__in=user_ids).values(
            'user_id', 'provider', 'extra_data'
        ):
            social_map.setdefault(sa['user_id'], []).append({
                'provider': sa['provider'],
                'extra_data': sa['extra_data'] or {},
            })
    except Exception:
        social_map = {}

    user_data = []
    for user in users:
        profile = getattr(user, 'userprofile', None)
        try:
            points = user.points
        except UserPoints.DoesNotExist:
            points = None
        user_orders = list(user.orders.all())
        total_orders = len(user_orders)

        order_status_counts = order_status_template.copy()
        payment_status_counts = payment_status_template.copy()

        for order in user_orders:
            order_status_counts[order.status] = order_status_counts.get(order.status, 0) + 1
            payment_status = canonicalize_payment_status(order.payment_status)
            payment_status_counts[payment_status] = (
                payment_status_counts.get(payment_status, 0) + 1
            )

        total_spent = sum(
            (order.total_sum for order in user_orders if order.payment_status != 'unpaid'),
            0,
        )
        points_spent = getattr(points, 'total_spent', 0)

        promo_entries = []
        used_promos = []
        for usage in promo_usage_map.get(user.id, []):
            promo = usage.promo_code
            entry = {
                'id': promo.id,
                'code': promo.code,
                'discount': float(promo.discount_value),
                'discount_display': _format_discount_display(promo),
                'is_used': usage.order is not None,
                'used_in_order': getattr(usage.order, 'order_number', None),
                'used_date': usage.used_at,
                'group_name': getattr(usage.group, 'name', None),
            }
            if usage.order:
                used_promos.append(entry)
            else:
                promo_entries.append(entry)

        # Методи входу
        socials = social_map.get(user.id, [])
        google_link = next((s for s in socials if s['provider'] == 'google-oauth2'), None)
        google_email = ''
        if google_link:
            google_email = (google_link.get('extra_data') or {}).get('email') or ''

        auth_methods = {
            'has_password': user.has_usable_password(),
            'google_connected': bool(google_link),
            'google_email': google_email,
            'telegram_connected': bool(profile and profile.telegram_id),
            'telegram_username': profile.telegram if profile else '',
            'telegram_id': profile.telegram_id if profile else None,
        }

        is_online = user.id in online_user_ids
        last_seen = last_seen_map.get(user.id) or user.last_login

        user_data.append({
            'user': user,
            'profile': profile,
            'points': points,
            'total_orders': total_orders,
            'order_status_counts': order_status_counts,
            'payment_status_counts': payment_status_counts,
            'total_spent': total_spent,
            'points_spent': points_spent,
            'promocodes': promo_entries,
            'used_promocodes': used_promos,
            'is_manager': getattr(profile, 'is_manager', False),
            'auth_methods': auth_methods,
            'is_online': is_online,
            'last_seen_at': last_seen,
            'date_joined': user.date_joined,
        })

    return {'user_data': user_data}


@staff_member_required
def admin_update_payment_status(request):
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Невірний метод запиту'}, status=405)

    order_id = request.POST.get('order_id')
    payment_status = canonicalize_payment_status(request.POST.get('payment_status'))

    if not order_id or payment_status not in {'unpaid', 'checking', 'prepaid', 'paid'}:
        return JsonResponse({'success': False, 'error': 'Невірний статус оплати'}, status=400)

    with transaction.atomic():
        order = get_object_or_404(Order.objects.select_for_update(), pk=order_id)
        order.payment_status = payment_status
        order.save(update_fields=['payment_status', 'updated'])
        ensure_order_purchase_action(
            order,
            metadata={'source': 'admin_payment_status'},
            raise_errors=True,
        )

    return JsonResponse(
        {
            'success': True,
            'message': f'Статус оплати змінено на "{get_payment_status_label(payment_status)}"',
            'payment_status': payment_status,
            'payment_status_label': get_payment_status_label(payment_status),
        }
    )


@staff_member_required
def admin_order_payment_snapshots(request):
    raw_ids = request.GET.get('ids', '')
    try:
        order_ids = [int(value) for value in raw_ids.split(',') if value.strip()]
    except ValueError:
        return JsonResponse({'success': False, 'error': 'Некоректні id замовлень'}, status=400)

    if not order_ids:
        return JsonResponse({'success': True, 'orders': {}})

    orders = Order.objects.filter(pk__in=order_ids).only(
        'id',
        'payment_status',
        'pay_type',
        'total_sum',
        'payment_payload',
    )
    payload = {}
    for order in orders:
        snapshot = build_order_payment_snapshot(order)
        payment_payload = order.payment_payload or {}
        history = payment_payload.get('history') or []
        last_entry = history[-1] if history else {}
        snapshot['payment_last_status'] = last_entry.get('status') or payment_payload.get('last_status') or ''
        snapshot['payment_last_time'] = (
            last_entry.get('received_at')
            or last_entry.get('ts')
            or payment_payload.get('last_update_at')
            or ''
        )
        payload[str(order.pk)] = snapshot

    return JsonResponse({'success': True, 'orders': payload})


def _build_catalogs_context():
    """Контекст для управления каталогами."""
    categories = (
        Category.objects.filter(is_active=True)
        .prefetch_related('products')
        .order_by('order', 'name')
    )

    products = (
        Product.objects.select_related('category', 'catalog')
        .prefetch_related('images', 'color_variants__images')
        .order_by('-priority', '-id')
    )

    catalogs = Catalog.objects.filter(is_active=True).order_by('order', 'name')

    product_list = list(products)
    product_metrics = build_product_admin_metrics([product.id for product in product_list])
    for product in product_list:
        metrics = product_metrics.get(product.id, {})
        product.total_views = metrics.get('total_views', 0)
        product.unique_ip_views = metrics.get('unique_ip_views', 0)

    return {
        'categories': categories,
        'products': product_list,
        'catalogs': catalogs,
        'product_statuses': ProductStatus,
    }


def _build_size_grids_context():
    """Minimal, JSON-safe bootstrap for the Fable size-grid workspace."""
    catalogs = list(
        Catalog.objects.filter(is_active=True)
        .order_by('order', 'name')
        .values('id', 'name', 'slug')
    )
    return {
        'size_grid_bootstrap': {
            'catalogs': catalogs,
            'urls': {
                'list': reverse('fable5_api_size_grids'),
                'save': reverse('fable5_api_size_grid_save'),
                'duplicate': reverse('fable5_api_size_grid_duplicate'),
                'archive': reverse('fable5_api_size_grid_archive'),
                'preview': reverse('fable5_api_size_grid_preview'),
            },
        },
    }


def _build_offline_stores_context():
    """Контекст для оффлайн-магазинов."""
    stores = OfflineStore.objects.all().order_by('order', 'name')
    return {
        'stores': stores,
        'total_stores': stores.count(),
        'active_stores': stores.filter(is_active=True).count(),
    }


def _build_print_proposals_context():
    """Контекст для заявок на принты."""
    proposals = (
        PrintProposal.objects.select_related('user', 'awarded_promocode')
        .order_by('-created_at')
    )
    return {
        'print_proposals': proposals,
        'total_proposals': proposals.count(),
        'pending_proposals': proposals.filter(status='pending').count(),
    }


def _group_custom_print_attachments(lead):
    zone_labels = {
        "front": "На грудях / спереду",
        "back": "На спині",
        "sleeve": "На рукаві",
        "sleeve_left": "Лівий рукав",
        "sleeve_right": "Правий рукав",
        "custom": "Інше",
        "unsorted": "Без прив'язки",
    }
    grouped = []
    buckets = {}

    for attachment in lead.attachments.all():
        zone = attachment.placement_zone or "unsorted"
        bucket = buckets.get(zone)
        if bucket is None:
            bucket = {
                "zone": zone,
                "label": zone_labels.get(zone, zone or "Без прив'язки"),
                "items": [],
            }
            buckets[zone] = bucket
            grouped.append(bucket)
        bucket["items"].append(attachment)

    return grouped


def _build_custom_print_orders_context(request):
    """Контекст для кастомних замовлень у staff-панелі."""
    status_filter = (request.GET.get("custom_print_status") or "all").strip()
    client_kind_filter = (request.GET.get("custom_print_client_kind") or "all").strip()
    business_kind_filter = (request.GET.get("custom_print_business_kind") or "all").strip()
    product_type_filter = (request.GET.get("custom_print_product_type") or "all").strip()
    has_files_filter = (request.GET.get("custom_print_has_files") or "all").strip()
    moderation_filter = (request.GET.get("custom_print_moderation") or "all").strip()
    query = (request.GET.get("custom_print_q") or "").strip()
    selected_lead_id = (request.GET.get("lead") or "").strip()

    base_qs = (
        CustomPrintLead.objects.prefetch_related("attachments")
        .select_related("order")
        .order_by("-created_at")
    )
    leads_qs = base_qs

    if status_filter != "all":
        leads_qs = leads_qs.filter(status=status_filter)
    if moderation_filter != "all":
        leads_qs = leads_qs.filter(moderation_status=moderation_filter)
    if client_kind_filter != "all":
        leads_qs = leads_qs.filter(client_kind=client_kind_filter)
    if business_kind_filter != "all":
        if business_kind_filter == "empty":
            leads_qs = leads_qs.filter(business_kind="")
        else:
            leads_qs = leads_qs.filter(business_kind=business_kind_filter)
    if product_type_filter != "all":
        leads_qs = leads_qs.filter(product_type=product_type_filter)
    if has_files_filter == "yes":
        leads_qs = leads_qs.filter(attachments__isnull=False).distinct()
    elif has_files_filter == "no":
        leads_qs = leads_qs.filter(attachments__isnull=True)
    if query:
        leads_qs = leads_qs.filter(
            Q(lead_number__icontains=query)
            | Q(name__icontains=query)
            | Q(brand_name__icontains=query)
            | Q(contact_value__icontains=query)
            | Q(brief__icontains=query)
        )

    leads = list(leads_qs[:200])
    selected_lead = None
    if selected_lead_id:
        try:
            selected_lead_pk = int(selected_lead_id)
        except (TypeError, ValueError):
            selected_lead_pk = None
        if selected_lead_pk is not None:
            selected_lead = next((lead for lead in leads if lead.pk == selected_lead_pk), None)
            if selected_lead is None:
                selected_lead = base_qs.filter(pk=selected_lead_pk).first()
    if selected_lead is None and leads:
        selected_lead = leads[0]

    selected_lead_groups = _group_custom_print_attachments(selected_lead) if selected_lead else []
    previous_lead = None
    next_lead = None
    if selected_lead and selected_lead in leads:
        selected_index = leads.index(selected_lead)
        if selected_index > 0:
            previous_lead = leads[selected_index - 1]
        if selected_index + 1 < len(leads):
            next_lead = leads[selected_index + 1]

    contact_links = _custom_print_contact_links(selected_lead) if selected_lead else {}
    attachment_cards = _custom_print_attachment_cards(selected_lead) if selected_lead else []

    # Прикріплюємо людиноорієнтовані labels (для шаблона), щоб
    # адмін-панель показувала "Преміум" замість "premium" та назву
    # кольору з PRODUCT_MATRIX замість сирого слаг-значення.
    if selected_lead is not None:
        try:
            from storefront.custom_print_config import (
                resolve_lead_display_labels,
                resolve_fabric_badge,
            )
            display_labels = resolve_lead_display_labels(selected_lead)
            fabric_badge = resolve_fabric_badge(display_labels.get("fabric_value") or "")
            selected_lead.fit_display = display_labels.get("fit_label") or ""
            selected_lead.fabric_display = display_labels.get("fabric_label") or ""
            selected_lead.color_display = {
                "label": display_labels.get("color_label") or "",
                "hex": display_labels.get("color_hex") or "",
            }
            selected_lead.fabric_badge = fabric_badge
        except Exception:
            # На випадок, якщо custom_print_config не імпортується (наприклад,
            # під час міграцій) — не валимо панель.
            selected_lead.fit_display = selected_lead.fit
            selected_lead.fabric_display = selected_lead.fabric
            selected_lead.color_display = {
                "label": selected_lead.color_choice,
                "hex": "",
            }
            selected_lead.fabric_badge = {}

    return {
        "custom_print_orders": leads,
        "custom_print_total": base_qs.count(),
        "custom_print_new_count": base_qs.filter(status=CustomPrintLeadStatus.NEW).count(),
        "custom_print_in_progress_count": base_qs.filter(status=CustomPrintLeadStatus.IN_PROGRESS).count(),
        "custom_print_closed_count": base_qs.filter(status=CustomPrintLeadStatus.CLOSED).count(),
        "custom_print_awaiting_count": base_qs.filter(
            moderation_status="awaiting_review"
        ).count(),
        "custom_print_status_filter": status_filter,
        "custom_print_moderation_filter": moderation_filter,
        "custom_print_client_kind_filter": client_kind_filter,
        "custom_print_business_kind_filter": business_kind_filter,
        "custom_print_product_type_filter": product_type_filter,
        "custom_print_has_files_filter": has_files_filter,
        "custom_print_query": query,
        "custom_print_selected_lead": selected_lead,
        "custom_print_selected_lead_groups": selected_lead_groups,
        "custom_print_selected_lead_attachments": attachment_cards,
        "custom_print_selected_contact_links": contact_links,
        "custom_print_previous_lead": previous_lead,
        "custom_print_next_lead": next_lead,
        "custom_print_statuses": CustomPrintLeadStatus.choices,
        "custom_print_moderation_choices": [
            ("draft", "Чернетка"),
            ("awaiting_review", "На перевірці"),
            ("approved", "Погоджено"),
            ("rejected", "Відхилено"),
        ],
        "custom_print_client_kinds": CustomPrintClientKind.choices,
        "custom_print_business_kinds": CustomPrintBusinessKind.choices,
        "custom_print_product_types": CustomPrintProductType.choices,
    }


def _custom_print_contact_links(lead) -> dict:
    """Quick-action ссылки для кнопок «Зателефонувати / Telegram / WhatsApp / Скопіювати»."""
    if not lead:
        return {}
    raw_value = (getattr(lead, "contact_value", "") or "").strip()
    channel = (getattr(lead, "contact_channel", "") or "").strip().lower()
    digits = "".join(ch for ch in raw_value if ch.isdigit() or ch == "+")
    cleaned_handle = raw_value.lstrip("@").replace(" ", "")

    links = {"raw": raw_value, "channel": channel}

    # 1) Якщо клієнт підтвердив Telegram через бота — використовуємо його дані з пріоритетом
    verified_id = getattr(lead, "telegram_verified_user_id", None)
    verified_username = (getattr(lead, "telegram_verified_username", "") or "").strip()
    verified_phone = (getattr(lead, "telegram_verified_phone", "") or "").strip()
    if verified_username:
        links["telegram"] = f"https://t.me/{verified_username.lstrip('@')}"
    elif verified_id:
        links["telegram"] = f"tg://user?id={verified_id}"
    if verified_phone:
        verified_digits = "".join(ch for ch in verified_phone if ch.isdigit() or ch == "+")
        if verified_digits:
            links["phone"] = f"tel:{verified_digits}"

    # 2) Канальні посилання з contact_value (якщо ще не виставлені)
    if "telegram" not in links and channel == "telegram" and cleaned_handle:
        if cleaned_handle.startswith("http"):
            links["telegram"] = cleaned_handle
        else:
            links["telegram"] = f"https://t.me/{cleaned_handle.lstrip('@')}"
    if channel == "whatsapp":
        wa_digits = digits.lstrip("+")
        if wa_digits:
            links["whatsapp"] = f"https://wa.me/{wa_digits}"
    if "phone" not in links and channel == "phone" and digits:
        links["phone"] = f"tel:{digits}"

    # 3) Якщо в contact_value лежить телефон при будь-якому каналі — даємо кнопку дзвінка
    if digits and len(digits) >= 9 and "phone" not in links:
        links["phone"] = f"tel:{digits}"

    return links


def _custom_print_attachment_cards(lead) -> list:
    """Подготовленные карточки файлов с превью/типом, удобные для шаблона."""
    import mimetypes

    if not lead:
        return []
    cards = []
    for attachment in lead.attachments.all().order_by("placement_zone", "sort_order", "id"):
        file_field = getattr(attachment, "file", None)
        if not file_field:
            continue
        file_name = getattr(file_field, "name", "") or ""
        url = ""
        try:
            url = file_field.url
        except Exception:
            url = ""
        mime_type, _ = mimetypes.guess_type(file_name)
        is_image = bool(mime_type and mime_type.startswith("image/"))
        if not mime_type:
            ext = file_name.lower().rsplit(".", 1)[-1] if "." in file_name else ""
            is_image = ext in {"jpg", "jpeg", "png", "webp", "gif"}
        zone_label = {
            "front": "На грудях",
            "back": "На спині",
            "sleeve": "На рукаві",
            "sleeve_left": "Лівий рукав",
            "sleeve_right": "Правий рукав",
            "custom": "Інше",
        }.get(attachment.placement_zone or "", attachment.placement_zone or "Без прив'язки")
        ext_label = ""
        if "." in file_name:
            ext_label = file_name.rsplit(".", 1)[-1].upper()[:5]

        cards.append({
            "url": url,
            "name": file_name.split("/")[-1],
            "is_image": is_image,
            "zone": zone_label,
            "ext_label": ext_label or "FILE",
        })
    return cards


def _build_collaboration_context():
    """Контекст для блоков співпраці (дропшипінг, опт)."""
    try:
        invoices = list(WholesaleInvoice.objects.order_by('-created_at')[:50])
        dropship_orders = list(
            DropshipperOrder.objects.select_related('dropshipper', 'dropshipper__userprofile')
            .prefetch_related('items')
            .order_by('-created_at')[:50]
        )
        dropshipper_stats = list(
            DropshipperStats.objects.select_related('dropshipper', 'dropshipper__userprofile')
            .filter(total_orders__gt=0)
            .order_by('-total_profit')[:20]
        )
        payouts = list(
            DropshipperPayout.objects.select_related('dropshipper', 'dropshipper__userprofile')
            .order_by('-requested_at')[:50]
        )
        pending_payouts = DropshipperPayout.objects.filter(status='pending').count()

        total_dropship_orders = DropshipperOrder.objects.count()
        totals = DropshipperOrder.objects.aggregate(
            revenue=Sum('total_selling_price'), profit=Sum('profit')
        )
        total_dropship_revenue = totals.get('revenue') or 0
        total_dropship_profit = totals.get('profit') or 0
        pending_orders = DropshipperOrder.objects.filter(status__in=['draft', 'pending']).count()

    except Exception:
        invoices = []
        dropship_orders = []
        dropshipper_stats = []
        payouts = []
        pending_payouts = 0
        total_dropship_orders = 0
        total_dropship_revenue = 0
        total_dropship_profit = 0
        pending_orders = 0

    return {
        'invoices': invoices,
        'dropship_orders': dropship_orders,
        'dropshipper_stats': dropshipper_stats,
        'payouts': payouts,
        'pending_payouts': pending_payouts,
        'total_dropship_orders': total_dropship_orders,
        'total_dropship_revenue': total_dropship_revenue,
        'total_dropship_profit': total_dropship_profit,
        'pending_orders': pending_orders,
    }


@staff_member_required
def admin_dashboard(request):
    """
    Главная страница административной панели.

    Показывает:
    - Статистику продаж
    - Последние заказы
    - Популярные товары
    - Сводка по складу
    """
    # Статистика за последние 30 дней
    last_30_days = timezone.now() - timedelta(days=30)

    stats = {
        'total_orders': Order.objects.filter(created__gte=last_30_days).count(),
        'total_revenue': Order.objects.filter(
            created__gte=last_30_days,
            payment_status='paid'
        ).aggregate(total=Sum('total_sum'))['total'] or 0,
        'pending_orders': Order.objects.filter(status='new').count(),
        'total_products': Product.objects.count(),
        'total_categories': Category.objects.count()
    }

    # Последние заказы
    recent_orders = Order.objects.order_by('-created')[:10]

    # Популярные товары
    # TODO: Реализовать на основе статистики продаж

    return render(
        request,
        'admin/dashboard.html',
        {
            'stats': stats,
            'recent_orders': recent_orders
        }
    )


@login_required
def admin_panel(request):
    """
    Упрощённая реализация головної адмін-панелі.

    Підтримує ключові секції:
    - stats (поки що лише базові значення)
    - promocodes (повністю через новий інтерфейс)

    Інші секції рендеряться з порожнім контекстом, щоб зберегти працездатність шаблону.
    """
    if not request.user.is_staff:
        return redirect('home')

    section = request.GET.get('section', 'stats')
    period_param = request.GET.get('period', 'today')

    context = {
        'section': section,
        'stats': _build_stats(period_param),
    }
    if section == 'stats':
        context.update(build_admin_analytics_context(request))

    if section == 'users':
        context.update(_build_users_context())
    elif section == 'catalogs':
        context.update(_build_catalogs_context())
    elif section == 'size_grids':
        context.update(_build_size_grids_context())
    elif section == 'promocodes':
        context.update(get_promo_admin_context(request))
        context['section'] = 'promocodes'
    elif section == 'offline_stores':
        context.update(_build_offline_stores_context())
    elif section == 'print_proposals':
        context.update(_build_print_proposals_context())
    elif section == 'custom_print_orders':
        context.update(_build_custom_print_orders_context(request))
    elif section == 'orders':
        context.update(_build_orders_context(request))
    elif section == 'collaboration':
        context.update(_build_collaboration_context())
    elif section == 'dispatcher':
        context.update(_build_dispatcher_context(request))
    elif section == 'push_notifications':
        context.update(_build_push_notifications_context(request))
    elif section == 'seo':
        from storefront.services.seo_dashboard import build_seo_dashboard_context
        context.update(build_seo_dashboard_context())
    elif section == 'blog':
        from storefront.views.blog import build_admin_blog_context
        context.update(build_admin_blog_context())
    elif section == 'reviews':
        # Phase 21 (PR-A1) — moderation queue inside the custom admin
        # so the team doesn't need to bounce through Django-admin for
        # day-to-day approve/reject flow. Heavy edits (raw text /
        # photo attachments) still link out via ``admin_url``.
        from storefront.services.admin_reviews import build_reviews_context
        context.update(build_reviews_context())

    html_content = render_to_string('pages/admin_panel.html', context, request=request)
    response = HttpResponse(html_content)
    response['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response['Pragma'] = 'no-cache'
    response['Expires'] = '0'

    return response


@staff_member_required
def admin_push_notifications_create(request):
    if request.method != "POST":
        return JsonResponse({"success": False, "error": "Method not allowed"}, status=405)

    submit_action = (request.POST.get("submit_action") or "send_now").strip()
    form = PushNotificationCampaignForm(request.POST, request.FILES)

    if not form.is_valid():
        context = {
            "section": "push_notifications",
            "stats": _build_stats("today"),
        }
        context.update(_build_push_notifications_context(request, form=form))
        html_content = render_to_string("pages/admin_panel.html", context, request=request)
        response = HttpResponse(html_content, status=400)
        response["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response["Pragma"] = "no-cache"
        response["Expires"] = "0"
        return response

    campaign = form.save(commit=False)
    campaign.created_by = request.user
    campaign.status = PushNotificationCampaign.Status.DRAFT
    campaign.save()

    if submit_action == "save_draft":
        messages.success(request, "Чернетку push-кампанії збережено.")
        return redirect(f"{reverse('admin_panel')}?section=push_notifications")

    try:
        result = send_campaign(campaign)
    except WebPushConfigurationError as exc:
        campaign.status = PushNotificationCampaign.Status.FAILED
        campaign.last_error = str(exc)[:255]
        campaign.sent_finished_at = timezone.now()
        campaign.save(update_fields=["status", "last_error", "sent_finished_at", "updated_at"])
        messages.error(request, f"Push не налаштовано: {exc}")
        return redirect(f"{reverse('admin_panel')}?section=push_notifications")

    if result["failed"]:
        messages.warning(
            request,
            f"Push-кампанію відправлено частково: успішно {result['sent']}, помилок {result['failed']}.",
        )
    else:
        messages.success(request, f"Push-кампанію відправлено: {result['sent']} підписок.")

    return redirect(f"{reverse('admin_panel')}?section=push_notifications")


@staff_member_required
def admin_push_notifications_send(request, campaign_id: int):
    if request.method != "POST":
        return JsonResponse({"success": False, "error": "Method not allowed"}, status=405)

    campaign = get_object_or_404(PushNotificationCampaign, pk=campaign_id)
    if campaign.status not in {
        PushNotificationCampaign.Status.DRAFT,
        PushNotificationCampaign.Status.FAILED,
        PushNotificationCampaign.Status.PARTIAL,
    }:
        messages.error(request, "Повторно можна відправити лише чернетку або кампанію з помилкою.")
        return redirect(f"{reverse('admin_panel')}?section=push_notifications")

    campaign.deliveries.all().delete()
    try:
        result = send_campaign(campaign)
    except WebPushConfigurationError as exc:
        campaign.status = PushNotificationCampaign.Status.FAILED
        campaign.last_error = str(exc)[:255]
        campaign.sent_finished_at = timezone.now()
        campaign.save(update_fields=["status", "last_error", "sent_finished_at", "updated_at"])
        messages.error(request, f"Push не налаштовано: {exc}")
        return redirect(f"{reverse('admin_panel')}?section=push_notifications")

    if result["failed"]:
        messages.warning(
            request,
            f"Push-кампанію відправлено частково: успішно {result['sent']}, помилок {result['failed']}.",
        )
    else:
        messages.success(request, f"Push-кампанію відправлено: {result['sent']} підписок.")

    return redirect(f"{reverse('admin_panel')}?section=push_notifications")


@staff_member_required
def admin_reorder_products(request):
    """
    Перестановка товаров drag&drop в каталоге: обновляет priority по порядку.
    """
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Method not allowed'}, status=405)
    try:
        payload = json.loads(request.body.decode('utf-8'))
        ids = payload.get('order') or []
        ids = [int(x) for x in ids if str(x).isdigit()]
    except Exception:
        return JsonResponse({'success': False, 'error': 'Invalid payload'}, status=400)

    ids = list(dict.fromkeys(ids))

    if not ids:
        return JsonResponse({'success': False, 'error': 'Empty order'}, status=400)

    existing_ids = set(
        Product.objects.filter(id__in=ids).values_list('id', flat=True)
    )
    ordered_existing_ids = [product_id for product_id in ids if product_id in existing_ids]
    if not ordered_existing_ids:
        return JsonResponse({'success': False, 'error': 'Products not found'}, status=404)

    priority_start = len(ordered_existing_ids)
    priority_cases = [
        When(id=product_id, then=Value(priority_start - idx))
        for idx, product_id in enumerate(ordered_existing_ids)
    ]

    with transaction.atomic():
        updated = Product.objects.filter(id__in=ordered_existing_ids).update(
            priority=Case(*priority_cases, output_field=IntegerField())
        )
        transaction.on_commit(bump_public_product_order_version)

    return JsonResponse({'success': True, 'updated': updated})


@staff_member_required
def admin_update_product_status(request):
    """Обновление статуса товара из админ-панели."""
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Method not allowed'}, status=405)
    try:
        payload = json.loads(request.body.decode('utf-8'))
        product_id = int(payload.get('product_id'))
        status_value = payload.get('status')
    except Exception:
        return JsonResponse({'success': False, 'error': 'Invalid payload'}, status=400)

    if status_value not in ProductStatus.values:
        return JsonResponse({'success': False, 'error': 'Invalid status'}, status=400)

    with transaction.atomic():
        updated = Product.objects.filter(id=product_id).update(status=status_value)
        if updated:
            transaction.on_commit(bump_public_product_order_version)
    if not updated:
        return JsonResponse({'success': False, 'error': 'Product not found'}, status=404)

    return JsonResponse({'success': True})


@staff_member_required
def admin_toggle_manager(request, user_id: int):
    """Выдать/снять менеджерский доступ для пользователя (is_manager в UserProfile)."""
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Method not allowed'}, status=405)
    try:
        payload = json.loads(request.body.decode('utf-8'))
        is_manager = bool(payload.get('is_manager'))
    except Exception:
        return JsonResponse({'success': False, 'error': 'Invalid payload'}, status=400)

    user = get_object_or_404(User, pk=user_id)
    profile, _ = UserProfile.objects.get_or_create(user=user)
    profile.is_manager = is_manager
    profile.save(update_fields=['is_manager'])

    return JsonResponse({'success': True, 'is_manager': profile.is_manager})


@staff_member_required
def admin_custom_print_lead_status(request, lead_id: int):
    """Оновлення статусу заявки кастомного принта з кастомної staff-панелі."""
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Method not allowed'}, status=405)
    try:
        payload = json.loads(request.body.decode('utf-8'))
        status_value = payload.get('status')
    except Exception:
        return JsonResponse({'success': False, 'error': 'Invalid payload'}, status=400)

    if status_value not in CustomPrintLeadStatus.values:
        return JsonResponse({'success': False, 'error': 'Invalid status'}, status=400)

    lead = get_object_or_404(CustomPrintLead, pk=lead_id)
    lead.status = status_value
    lead.save(update_fields=['status', 'updated_at'])
    return JsonResponse({'success': True, 'status': lead.status})


@staff_member_required
def admin_custom_print_lead_moderation(request, lead_id: int):
    """Manager action: approve or reject a custom-print lead from the staff panel.

    POST body: {"action": "approve"|"reject", "price": "NNN.NN" (optional, for approve), "note": "..."}
    On reject: any user session that still holds this lead in its custom-cart will
    see it marked rejected on the next cart/mini-cart load and the entry removed
    (the cart views skip leads that are REJECTED and no longer in session).
    """
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Method not allowed'}, status=405)
    try:
        payload = json.loads(request.body.decode('utf-8'))
    except Exception:
        return JsonResponse({'success': False, 'error': 'Invalid payload'}, status=400)

    action = (payload.get('action') or '').strip().lower()
    if action not in {'approve', 'reject'}:
        return JsonResponse({'success': False, 'error': 'Invalid action'}, status=400)

    note = (payload.get('note') or '').strip()
    price_raw = payload.get('price')

    lead = get_object_or_404(CustomPrintLead, pk=lead_id)

    update_fields = ['moderation_status', 'manager_note', 'reviewed_at', 'updated_at']
    lead.reviewed_at = timezone.now()
    lead.manager_note = note

    if action == 'approve':
        from decimal import Decimal, InvalidOperation
        if price_raw not in (None, ''):
            try:
                lead.approved_price = Decimal(str(price_raw))
                update_fields.append('approved_price')
            except (InvalidOperation, TypeError):
                return JsonResponse({'success': False, 'error': 'Некоректна ціна'}, status=400)
        final_price = Decimal(str(lead.final_price_value or 0))
        if final_price <= 0:
            return JsonResponse(
                {
                    'success': False,
                    'error': 'Для погодження потрібно вказати фінальну ціну більше 0 грн.',
                },
                status=400,
            )
        lead.moderation_status = CustomPrintModerationStatus.APPROVED
    else:
        lead.moderation_status = CustomPrintModerationStatus.REJECTED

    lead.save(update_fields=update_fields)

    # Notify customer via Telegram if they provided a telegram contact (best-effort).
    try:
        from storefront.custom_print_notifications import notify_custom_print_moderation_result
        notify_custom_print_moderation_result(lead)
    except Exception:
        pass

    return JsonResponse({
        'success': True,
        'moderation_status': lead.moderation_status,
        'approved_price': str(lead.approved_price or ''),
        'manager_note': lead.manager_note,
    })


@staff_member_required
def manage_products(request):
    """
    Список всех товаров с возможностью фильтрации.

    Query params:
        category: ID категории для фильтрации
        featured: Показать только featured
        search: Поиск по названию
    """
    products = Product.objects.select_related('category').order_by('-priority', '-id')

    # Фильтры
    category_id = request.GET.get('category')
    if category_id:
        products = products.filter(category_id=category_id)

    if request.GET.get('featured'):
        products = products.filter(featured=True)

    search = request.GET.get('search')
    if search:
        products = products.filter(title__icontains=search)

    categories = Category.objects.all()

    return render(
        request,
        'admin/manage_products.html',
        {
            'products': products,
            'categories': categories
        }
    )


@transaction.atomic
def add_product(request):
    """
    Добавление нового товара через унифицированный интерфейс.

    Supports:
    - AJAX форма (JSON response)
    - Обычная форма (HTML redirect)
    - Добавление цветовых вариантов
    - Загрузка изображений
    """
    # TODO: Полная реализация добавления товара
    # Временно импортируем из старого views.py
    from storefront import views as old_views
    if hasattr(old_views, 'add_product'):
        return old_views.add_product(request)

    if request.method == 'POST':
        form = ProductForm(request.POST, request.FILES)
        if form.is_valid():
            product = form.save(commit=False)
            if not getattr(product, 'slug', None):
                base = slugify(product.title or '')
                product.slug = unique_slugify(Product, base)
            product.save()
            return redirect('product', slug=product.slug)
    else:
        form = ProductForm()

    return render(
        request,
        'pages/add_product_new.html',
        {
            'form': form,
            'product': None,
            'is_new': True
        }
    )


@staff_member_required
def admin_product_builder(request, product_id=None):
    """
    Новый конструктор товара.

    Поддерживает создание и редактирование товара с цветовыми вариантами.
    """
    is_new = product_id is None

    if product_id is not None:
        product = get_object_or_404(
            Product.objects.select_related('catalog', 'category'),
            pk=product_id
        )
    else:
        product = Product(status=ProductStatus.DRAFT)
    original_slug = product.slug

    # Базовые формы
    if request.method == 'POST':
        product_form = ProductForm(
            data=request.POST,
            files=request.FILES,
            prefix='product',
            instance=product,
        )
        seo_form = ProductSEOForm(
            data=request.POST,
            prefix='seo',
            instance=product,
        )
    else:
        product_form = ProductForm(prefix='product', instance=product)
        seo_form = ProductSEOForm(prefix='seo', instance=product)

    # Определяем выбранный каталог
    catalog_instance = None
    product_form_valid = None
    seo_form_valid = None

    if request.method == 'POST':
        product_form_valid = product_form.is_valid()
        if product_form_valid:
            catalog_instance = product_form.cleaned_data.get('catalog')
        else:
            catalog_id = product_form.data.get('product-catalog')
            if catalog_id:
                catalog_instance = Catalog.objects.filter(pk=catalog_id).first()
    else:
        catalog_id = request.GET.get('catalog')
        if product.catalog_id:
            catalog_instance = product.catalog
        elif catalog_id:
            catalog_instance = Catalog.objects.filter(pk=catalog_id).first()

    # Сетка размеров
    size_grid_instance = None
    if getattr(product, 'size_grid_id', None):
        size_grid_instance = product.size_grid
    elif catalog_instance:
        size_grid_instance = catalog_instance.size_grids.filter(is_active=True).order_by('order', 'name').first()

    if request.method == 'POST':
        size_grid_base_instance = size_grid_instance
        if size_grid_base_instance is None and catalog_instance:
            size_grid_base_instance = SizeGrid(catalog=catalog_instance)
        size_grid_form = SizeGridForm(
            data=request.POST,
            files=request.FILES,
            prefix='size_grid',
            instance=size_grid_base_instance
        )
        size_grid_bound_values = [
            (request.POST.get(f'size_grid-{field_name}') or '').strip()
            for field_name in ('name', 'description', 'guide_data')
        ]
        size_grid_form_used = any(size_grid_bound_values) or bool(request.FILES.get('size_grid-image'))
        size_grid_form_valid = True
        if size_grid_form_used:
            size_grid_form_valid = size_grid_form.is_valid()
            size_grid_requires_catalog = (
                size_grid_form_valid
                and not getattr(size_grid_form.instance, 'catalog_id', None)
                and catalog_instance is None
            )
            if size_grid_requires_catalog:
                size_grid_form.add_error(None, 'Оберіть каталог перед збереженням сітки розмірів.')
                size_grid_form_valid = False
    else:
        size_grid_form = SizeGridForm(prefix='size_grid', instance=size_grid_instance)
        size_grid_form_used = False
        size_grid_form_valid = True

    # Цвета и изображения
    color_formset = build_color_variant_formset(
        product=product,
        data=request.POST if request.method == 'POST' else None,
        files=request.FILES if request.method == 'POST' else None,
        prefix='color_variants'
    )
    fit_formset = build_product_fit_option_formset(
        product=product,
        data=request.POST if request.method == 'POST' else None,
        prefix='fit_options',
    )
    # Phase 17 — simple toggle UI (classic/oversize). Coexists with the
    # legacy formset; the formset stays for advanced edits (custom fit
    # codes) but is hidden from the default product builder UI.
    fit_toggle_form = ProductFitToggleForm(
        data=request.POST if request.method == 'POST' else None,
        product=product if getattr(product, 'pk', None) else None,
        prefix='fit_toggle',
    )

    option_formset = None
    option_formset_valid = True
    option_formset_has_changes = False
    if catalog_instance:
        if request.method == 'POST':
            option_formset = CatalogOptionFormSet(
                data=request.POST,
                prefix='catalog-options',
                instance=catalog_instance
            )
            option_formset_valid = option_formset.is_valid()
            option_formset_has_changes = any(form.has_changed() for form in option_formset.forms) or bool(option_formset.deleted_forms)
        else:
            option_formset = CatalogOptionFormSet(
                prefix='catalog-options',
                instance=catalog_instance
            )

    catalogs = Catalog.objects.filter(is_active=True).order_by('order', 'name')

    if request.method == 'POST':
        if product_form_valid is None:
            product_form_valid = product_form.is_valid()

        seo_form_valid = seo_form.is_valid()
        color_formset_valid = color_formset.is_valid()
        fit_formset_valid = fit_formset.is_valid()

        images_valid = True
        for variant_form in color_formset.forms:
            images_formset = getattr(variant_form, 'images_formset', None)
            if images_formset is not None:
                if not images_formset.is_valid():
                    images_valid = False

        if product_form_valid and seo_form_valid and color_formset_valid and fit_formset_valid and images_valid and size_grid_form_valid and option_formset_valid:
            with transaction.atomic():
                product_obj = product_form.save(commit=False)
                # Генерация slug, если не задан або змінено
                desired_slug = product_obj.slug or slugify(product_obj.title or '')
                if product_obj.pk:
                    if original_slug and desired_slug == original_slug:
                        product_obj.slug = original_slug
                    else:
                        product_obj.slug = unique_slugify(Product, desired_slug)
                else:
                    product_obj.slug = unique_slugify(Product, desired_slug)
                product_obj.status = product_obj.status or ProductStatus.DRAFT

                # Обработка size grid
                size_grid_obj = product_form.cleaned_data.get('size_grid')
                if size_grid_form_used and size_grid_form_valid:
                    temp_grid = size_grid_form.save(commit=False)
                    grid_catalog = temp_grid.catalog or catalog_instance or product_form.cleaned_data.get('catalog')
                    if grid_catalog:
                        temp_grid.catalog = grid_catalog
                        temp_grid.save()
                        size_grid_obj = temp_grid
                elif size_grid_obj and not size_grid_obj.catalog_id and catalog_instance:
                    size_grid_obj.catalog = catalog_instance
                    size_grid_obj.save(update_fields=['catalog'])

                product_obj.size_grid = size_grid_obj
                product_obj.save()
                product_form.save_m2m()

                # SEO поля
                seo_form.instance = product_obj
                seo_form.save()

                # Работа с цветовыми вариантами
                saved_variants = []
                default_assigned = False
                main_image_uploaded = bool(product_form.cleaned_data.get('main_image'))
                primary_image_candidate = None
                images_updated_any = False
                default_switched = False

                for variant_form in color_formset.forms:
                    if not hasattr(variant_form, 'cleaned_data'):
                        continue

                    if variant_form.cleaned_data.get('DELETE'):
                        if variant_form.instance.pk:
                            variant_form.instance.delete()
                        continue

                    images_formset = getattr(variant_form, 'images_formset', None)
                    images_changed = images_formset.has_changed() if images_formset is not None else False

                    if (
                        not variant_form.cleaned_data
                        or (
                            not variant_form.has_changed()
                            and not images_changed
                            and not variant_form.instance.pk
                        )
                    ):
                        continue

                    variant = variant_form.save(commit=False)
                    variant.product = product_obj

                    if variant.is_default:
                        if default_assigned:
                            variant.is_default = False
                        else:
                            default_assigned = True

                    if variant.order is None:
                        variant.order = 0

                    if 'is_default' in getattr(variant_form, 'changed_data', []):
                        default_switched = True

                    variant.save()
                    saved_variants.append(variant)

                    if images_formset is not None:
                        images_formset.instance = variant
                        payloads = formset_to_variant_payloads(images_formset)
                        if payloads:
                            synced_images = sync_variant_images(variant, payloads)
                            images_updated_any = images_updated_any or images_changed
                            if (
                                not primary_image_candidate
                                and variant.is_default
                                and synced_images
                            ):
                                primary_image_candidate = synced_images[0].image

                # Если нет варианта по умолчанию — назначаем первый
                if not default_assigned and saved_variants:
                    primary_variant = saved_variants[0]
                    if not primary_variant.is_default:
                        primary_variant.is_default = True
                        primary_variant.save(update_fields=['is_default'])
                    if not primary_image_candidate:
                        first_img = primary_variant.images.order_by('order', 'id').first()
                        if first_img:
                            primary_image_candidate = first_img.image
                    default_switched = True

                # Ставим главное фото из default-варіанта, если его не загрузили вручную
                if not primary_image_candidate:
                    default_variant_obj = (
                        product_obj.color_variants.filter(is_default=True)
                        .order_by('order', 'id')
                        .first()
                    ) or product_obj.color_variants.order_by('order', 'id').first()
                    if default_variant_obj:
                        first_img = default_variant_obj.images.order_by('order', 'id').first()
                        if first_img:
                            primary_image_candidate = first_img.image

                if (
                    primary_image_candidate
                    and not main_image_uploaded
                    and (images_updated_any or default_switched or not product_obj.main_image)
                ):
                    if product_obj.main_image != primary_image_candidate:
                        product_obj.main_image = primary_image_candidate
                        product_obj.save(update_fields=['main_image'])

                # Дополнительные изображения товара
                extra_images = product_form.cleaned_data.get('extra_images') or []
                append_product_gallery(product_obj, extra_images)

                # Порядок существующих изображений товара (product images)
                gallery_order_raw = request.POST.get('product_gallery_order') or ''
                if gallery_order_raw:
                    try:
                        ids = [int(x) for x in gallery_order_raw.split(',') if x.strip()]
                        base = len(ids)
                        for index, image_id in enumerate(ids):
                            ProductImage.objects.filter(pk=image_id, product=product_obj).update(order=index)
                        # Остальным выставим порядок после указанных
                        remainder = product_obj.images.exclude(pk__in=ids).order_by('order', 'id')
                        offset = base
                        for extra_img in remainder:
                            if extra_img.order != offset:
                                extra_img.order = offset
                                extra_img.save(update_fields=['order'])
                            offset += 1
                    except Exception:
                        pass

                # Сохранение опций каталога
                if option_formset is not None and option_formset.is_bound and option_formset_valid and option_formset_has_changes:
                    option_formset.save()

                # Сохранение вариантов посадки.
                # Phase 17: legacy formset still runs (for non-classic/oversize
                # custom rows), then the simple toggle form normalises the
                # canonical pair (classic + oversize) and the default flag.
                fit_formset.instance = product_obj
                fit_formset.save()
                # Apply the new toggle UI ONLY when its prefix is present in
                # the POST. Otherwise (legacy admin scripts / API calls that
                # only post the formset) we don't want the empty toggle form
                # to silently disable freshly-saved rows.
                _toggle_present = any(
                    key.startswith('fit_toggle-') for key in request.POST.keys()
                )
                if _toggle_present and fit_toggle_form.is_valid():
                    fit_toggle_form.save(product_obj)
                # Auto-bootstrap: legacy tshirt products with zero fit_options
                # (admin never opened the new UI) get a sane default pair so
                # the storefront selector stops being silently empty.
                ensure_default_fit_options_for_tshirt(product_obj)
                active_fit_options = list(product_obj.fit_options.filter(is_active=True).order_by('order', 'id'))
                default_options = [option for option in active_fit_options if option.is_default]
                if default_options:
                    default = default_options[0]
                    product_obj.fit_options.filter(is_default=True).exclude(pk=default.pk).update(is_default=False)
                elif active_fit_options:
                    default = active_fit_options[0]
                    if not default.is_default:
                        default.is_default = True
                        default.save(update_fields=['is_default'])

                messages.success(request, 'Товар успішно збережено.')
                return redirect('admin_product_builder_edit', product_id=product_obj.pk)
        else:
            messages.error(request, 'Перевірте форму — знайдені помилки.')

    def _extract_value(form, field_name, valid_flag):
        if not form:
            return None
        if form.is_bound:
            if valid_flag is False:
                return None
            if valid_flag is True and hasattr(form, 'cleaned_data'):
                return form.cleaned_data.get(field_name)
            return form.data.get(f'{form.prefix}-{field_name}')
        if form.instance is not None and hasattr(form.instance, field_name):
            return getattr(form.instance, field_name)
        return None

    def _colors_complete():
        if color_formset.is_bound:
            for form in color_formset.forms:
                if getattr(form, 'cleaned_data', None) and form.cleaned_data.get('DELETE'):
                    continue
                if form.has_changed() or getattr(form.instance, 'pk', None):
                    return True
            return False
        if product.pk:
            return product.color_variants.exists()
        return False

    basic_complete = all(
        bool(_extract_value(product_form, field, product_form_valid))
        for field in ('title', 'category', 'price')
    )
    catalog_complete = bool(_extract_value(product_form, 'catalog', product_form_valid))
    colors_complete = _colors_complete()
    seo_complete = any(
        bool(_extract_value(seo_form, field, seo_form_valid))
        for field in ('seo_title', 'seo_description', 'seo_keywords')
    )
    status_value = _extract_value(product_form, 'status', product_form_valid)
    preview_complete = bool(
        status_value and status_value != ProductStatus.DRAFT
    )

    progress_steps = {
        'basic': basic_complete,
        'catalog': catalog_complete,
        'colors': colors_complete,
        'seo': seo_complete,
        'preview': preview_complete,
    }
    total_steps = len(progress_steps)
    completed_steps = sum(1 for completed in progress_steps.values() if completed)
    progress_percent = int(round((completed_steps / total_steps) * 100)) if total_steps else 0

    builder_progress = {
        'steps': progress_steps,
        'completed': completed_steps,
        'total': total_steps,
        'percent': progress_percent,
    }
    # Для нового товара product.pk == None, но нам нужен объект в шаблоне,
    # чтобы выражения вида product.price не падали.
    product_for_view = product_form.instance if product_form is not None else product

    gallery_images = list(product.images.all()) if getattr(product, "pk", None) else []

    context = {
        'product': product_for_view,
        'product_form': product_form,
        'seo_form': seo_form,
        'size_grid_form': size_grid_form,
        'color_formset': color_formset,
        'fit_formset': fit_formset,
        'fit_toggle_form': fit_toggle_form,
        'option_formset': option_formset,
        'catalogs': catalogs,
        'selected_catalog': catalog_instance,
        'is_new': is_new,
        'builder_progress': builder_progress,
        'gallery_images': gallery_images,
    }

    return render(request, 'pages/product_builder.html', context)


@login_required
def add_category(request):
    """
    Добавление новой категории.
    """
    if request.method == 'POST':
        form = CategoryForm(request.POST, request.FILES)
        if form.is_valid():
            form.save()
            return redirect('catalog')
    else:
        form = CategoryForm()

    return render(
        request,
        'pages/add_category.html',
        {'form': form}
    )


def add_print(request):
    """
    Страница предложения принтов от пользователей.

    Features:
    - Анти-спам (лимит 1 минута)
    - Загрузка изображений или URL
    - Система баллов за одобренные принты
    """
    import time

    last_ts = request.session.get('print_proposal_last_ts', 0)
    now = int(time.time())
    rate_limited = now - last_ts < 60
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'

    proposal_form = None
    proposals = []

    if request.user.is_authenticated:
        proposals = PrintProposal.objects.filter(
            user=request.user
        ).order_by('-created_at')[:10]

        if request.method == 'POST':
            if rate_limited:
                if is_ajax:
                    return JsonResponse({'ok': False, 'error': 'rate_limited'}, status=429)
                return redirect('cooperation')

            form = PrintProposalForm(request.POST, request.FILES)
            if form.is_valid():
                obj = form.save(commit=False)
                obj.user = request.user
                obj.status = 'pending'
                obj.save()
                request.session['print_proposal_last_ts'] = now

                if is_ajax:
                    proposal_data = {
                        'id': obj.id,
                        'created_at': obj.created_at.strftime('%d.%m.%Y %H:%M'),
                        'status': obj.status,
                        'status_display': obj.get_status_display(),
                        'awarded_points': obj.awarded_points,
                        'awarded_promocode': obj.awarded_promocode.code if obj.awarded_promocode else None,
                        'awarded_promocode_display': (
                            obj.awarded_promocode.get_discount_display() if obj.awarded_promocode else None
                        ),
                        'description': obj.description or '',
                        'image_url': obj.image.url if obj.image else None,
                    }
                    return JsonResponse({'ok': True, 'proposal': proposal_data})

                return redirect('cooperation')

            if is_ajax:
                errors = []
                for field, errs in form.errors.items():
                    errors.extend([str(e) for e in errs])
                return JsonResponse({'ok': False, 'error': errors[0] if errors else 'invalid'}, status=400)

            proposal_form = form
        else:
            proposal_form = PrintProposalForm()

    return render(
        request,
        'pages/add-print.html',
        {
            'proposal_form': proposal_form,
            'proposals': proposals,
            'rate_limited': rate_limited,
        }
    )


@staff_member_required
def manage_print_proposals(request):
    """
    Административная панель для управления предложениями принтов.

    Actions:
    - Approve (одобрить)
    - Reject (отклонить)
    - Award points (начислить баллы)
    - Award promocode (выдать промокод)
    """
    status_filter = request.GET.get('status', 'pending')

    proposals = PrintProposal.objects.filter(
        status=status_filter
    ).select_related('user').order_by('-created_at')

    return render(
        request,
        'admin/manage_print_proposals.html',
        {
            'proposals': proposals,
            'status_filter': status_filter
        }
    )


@staff_member_required
def manage_promo_codes(request):
    """
    Управление промокодами.

    Features:
    - Создание новых промокодов
    - Редактирование существующих
    - Деактивация
    - Статистика использования
    """
    promo_codes = PromoCode.objects.all().order_by('-created_at')

    return render(
        request,
        'admin/manage_promo_codes.html',
        {'promo_codes': promo_codes}
    )


@staff_member_required
def generate_seo_content(request):
    """
    Генерация SEO контента с помощью AI (OpenAI).

    Features:
    - AI-генерация keywords
    - AI-генерация descriptions
    - Bulk операции для всех товаров
    """
    # TODO: Полная реализация AI генерации
    # Временно импортируем из старого views.py
    from storefront import views as old_views
    if hasattr(old_views, 'generate_seo_content'):
        return old_views.generate_seo_content(request)

    return render(request, 'admin/generate_seo.html')


@staff_member_required
def generate_alt_texts(request):
    """
    Генерация ALT текстов для изображений.

    Uses AI для автоматического описания изображений товаров.
    """
    # TODO: Реализовать генерацию alt текстов
    return render(request, 'admin/generate_alt_texts.html')


@staff_member_required
def manage_orders(request):
    """
    Управление заказами.

    Features:
    - Список всех заказов
    - Фильтрация по статусу
    - Обновление статусов
    - Добавление TTN (tracking number)
    - Печать накладных
    """
    from orders.models import Order

    status_filter = request.GET.get('status', '')

    orders = Order.objects.select_related('user').order_by('-created')

    if status_filter:
        orders = orders.filter(status=status_filter)

    return render(
        request,
        'admin/manage_orders.html',
        {
            'orders': orders,
            'status_filter': status_filter
        }
    )


@staff_member_required
def sales_statistics(request):
    """
    Статистика продаж.

    Features:
    - Графики продаж по дням/неделям/месяцам
    - Топ товары
    - Топ категории
    - Средний чек
    - Конверсия
    """
    # TODO: Реализовать детальную статистику
    return render(request, 'admin/sales_statistics.html')


@staff_member_required
def inventory_management(request):
    """
    Управление складом.

    Features:
    - Остатки товаров
    - Поступления
    - Списания
    - Резервы под заказы
    """
    # TODO: Реализовать управление складом
    return render(request, 'admin/inventory.html')


@staff_member_required
def admin_indexnow_submit(request):
    """AJAX endpoint: submit URLs to IndexNow from the custom admin panel.

    POST JSON body:
        {"type": "product"|"category"|"core"|"all", "ids": [..., ...]}

    For ``type=product`` or ``type=category`` the ``ids`` array is required and
    only objects with public URLs (published / active) are forwarded. ``core``
    submits the curated list from ``CORE_INDEXNOW_ROUTE_NAMES``. ``all``
    submits core + every published product + every active category.
    """
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Method not allowed'}, status=405)

    try:
        payload = json.loads(request.body.decode('utf-8') or '{}')
    except Exception:
        return JsonResponse({'success': False, 'error': 'Invalid JSON'}, status=400)

    if not is_indexnow_configured():
        return JsonResponse(
            {
                'success': False,
                'error': 'IndexNow не сконфігуровано (відсутній INDEXNOW_KEY).',
            },
            status=503,
        )

    target_type = (payload.get('type') or '').strip().lower()
    raw_ids = payload.get('ids') or []
    raw_urls = payload.get('urls') or []
    try:
        ids = [int(value) for value in raw_ids if str(value).strip()]
    except (TypeError, ValueError):
        return JsonResponse({'success': False, 'error': 'Invalid ids'}, status=400)

    urls: list[str] = []

    if target_type == 'urls' or (raw_urls and not target_type):
        urls = [str(u).strip() for u in raw_urls if str(u).strip()]
    elif target_type == 'product':
        if not ids:
            return JsonResponse({'success': False, 'error': 'Empty ids list'}, status=400)
        products = Product.objects.filter(pk__in=ids).only('slug', 'status')
        urls = [u for u in (get_product_public_url(p) for p in products) if u]
    elif target_type == 'category':
        if not ids:
            return JsonResponse({'success': False, 'error': 'Empty ids list'}, status=400)
        categories = Category.objects.filter(pk__in=ids).only('slug', 'is_active')
        urls = [u for u in (get_category_public_url(c) for c in categories) if u]
    elif target_type == 'core':
        urls = list(get_core_indexnow_urls())
    elif target_type == 'all':
        urls = list(get_core_indexnow_urls())
        urls.extend(filter(None, (
            get_product_public_url(p)
            for p in Product.objects.filter(status='published').only('slug', 'status')
        )))
        urls.extend(filter(None, (
            get_category_public_url(c)
            for c in Category.objects.filter(is_active=True).only('slug', 'is_active')
        )))
    else:
        return JsonResponse(
            {'success': False, 'error': 'Unknown type (use product|category|core|all|urls)'},
            status=400,
        )

    urls = list(dict.fromkeys(urls))
    if not urls:
        return JsonResponse(
            {
                'success': False,
                'error': 'Жодного публічного URL для надсилання (можливо, об’єкти неактивні).',
            },
            status=400,
        )

    ok = submit_indexnow_urls(urls)
    return JsonResponse(
        {
            'success': bool(ok),
            'count': len(urls),
            'message': (
                f'IndexNow прийняв {len(urls)} URL(-ів).'
                if ok
                else f'IndexNow повернув помилку для {len(urls)} URL(-ів). Деталі — у логах.'
            ),
        }
    )


@staff_member_required
def admin_google_indexing_submit(request):
    """AJAX endpoint: submit URLs to Google Indexing API.

    Accepts POST JSON body with one of three URL-selection modes:

    * ``type="product"|"category"`` + ``ids=[...]`` — submit specific
      DB-bound objects (mirrors :func:`admin_indexnow_submit`).
    * ``type="core"|"all"`` + optional ``groups`` / ``languages`` —
      bulk submit all known indexable URLs. ``core`` is the curated
      static-page list; ``all`` adds products/categories/variants/
      colour landings. ``groups`` (subset of
      ``services.index_targets.ALL_GROUPS``) and ``languages``
      (defaults to all from ``settings.LANGUAGES``) trim the set.
    * ``urls=[...]`` — submit an explicit URL list (the JS preview
      panel uses this for chunked submission).

    Common knobs:

    * ``notification`` — ``URL_UPDATED`` (default) or ``URL_DELETED``.
    * ``skip_existing=True`` — drop URLs already accepted today.
    * ``respect_quota=True`` — cap to today's remaining quota.
    * ``dry_run=True`` — return the URL preview without sending.

    Google's Indexing API accepts only one URL per HTTP call, so this
    view loops the list server-side. Failures are tallied and returned
    so the UI can surface partial outages without aborting the whole
    batch.
    """
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Method not allowed'}, status=405)

    try:
        payload = json.loads(request.body.decode('utf-8') or '{}')
    except Exception:
        return JsonResponse({'success': False, 'error': 'Invalid JSON'}, status=400)

    if not is_google_indexing_configured():
        status = get_google_indexing_status()
        return JsonResponse(
            {
                'success': False,
                'error': (
                    'Google Indexing API не сконфігуровано. '
                    f'Креденшіали: {status.get("credentials_path") or "—"} '
                    f'(present={status.get("credentials_present")}).'
                ),
            },
            status=503,
        )

    target_type = (payload.get('type') or '').strip().lower()
    raw_ids = payload.get('ids') or []
    notification = (payload.get('notification') or NOTIFICATION_URL_UPDATED).strip().upper()
    if notification not in {NOTIFICATION_URL_UPDATED, NOTIFICATION_URL_DELETED}:
        return JsonResponse(
            {'success': False, 'error': f'Unknown notification: {notification}'},
            status=400,
        )

    skip_existing = bool(payload.get('skip_existing'))
    respect_quota = bool(payload.get('respect_quota'))
    dry_run = bool(payload.get('dry_run'))
    raw_groups = payload.get('groups') or None
    raw_languages = payload.get('languages') or None
    explicit_urls = payload.get('urls') or None
    try:
        skip_recent_days = int(payload.get('skip_recent_days') or 0)
    except (TypeError, ValueError):
        skip_recent_days = 0
    skip_recent_days = max(0, min(60, skip_recent_days))
    try:
        skip_recent_hours = int(payload.get('skip_recent_hours') or 0)
    except (TypeError, ValueError):
        skip_recent_hours = 0
    # Cap at 60 days = 1440 hours. Lets ops express "skip last week" /
    # "skip last 12h" without giving them a way to brick the queue.
    skip_recent_hours = max(0, min(1440, skip_recent_hours))

    try:
        ids = [int(value) for value in raw_ids if str(value).strip()]
    except (TypeError, ValueError):
        return JsonResponse({'success': False, 'error': 'Invalid ids'}, status=400)

    urls: list[str] = []
    snapshot_meta = None

    if isinstance(explicit_urls, list) and explicit_urls:
        urls = [str(u).strip() for u in explicit_urls if str(u).strip()]
        snapshot_meta = {'mode': 'explicit', 'count': len(urls)}
    elif target_type == 'product':
        if not ids:
            return JsonResponse({'success': False, 'error': 'Empty ids list'}, status=400)
        products = Product.objects.filter(pk__in=ids).only('slug', 'status')
        urls = [u for u in (get_product_public_url(p) for p in products) if u]
    elif target_type == 'category':
        if not ids:
            return JsonResponse({'success': False, 'error': 'Empty ids list'}, status=400)
        categories = Category.objects.filter(pk__in=ids).only('slug', 'is_active')
        urls = [u for u in (get_category_public_url(c) for c in categories) if u]
    elif target_type == 'core':
        groups = raw_groups or ['static']
        languages = raw_languages or get_supported_languages()
        snapshot = build_targets(groups=groups, languages=languages)
        urls = snapshot.all_urls
        snapshot_meta = snapshot.to_dict(preview_limit=10)
    elif target_type == 'all' or (raw_groups and not target_type):
        groups = raw_groups or list(ALL_GROUPS)
        languages = raw_languages or get_supported_languages()
        snapshot = build_targets(groups=groups, languages=languages)
        urls = snapshot.all_urls
        snapshot_meta = snapshot.to_dict(preview_limit=10)
    else:
        return JsonResponse(
            {'success': False, 'error': 'Unknown type (use product|category|core|all|urls)'},
            status=400,
        )

    urls = list(dict.fromkeys(urls))
    if not urls:
        return JsonResponse(
            {
                'success': False,
                'error': 'Жодного публічного URL для надсилання (можливо, фільтри занадто вузькі).',
            },
            status=400,
        )

    if dry_run:
        # Preview-only mode — used by the JS panel to confirm size
        # before firing real requests. ``skip_recent_hours`` is the
        # canonical knob; legacy ``skip_existing`` / ``skip_recent_days``
        # widen the window if larger.
        effective_hours = skip_recent_hours
        if skip_existing and effective_hours < get_quota_window_hours():
            effective_hours = get_quota_window_hours()
        if skip_recent_days > 0 and effective_hours < skip_recent_days * 24:
            effective_hours = skip_recent_days * 24
        if effective_hours > 0:
            already_filter = get_urls_successful_in_last_hours(
                urls, hours=effective_hours, notification_type=notification
            )
        else:
            already_filter = set()
        return JsonResponse({
            'success': True,
            'dry_run': True,
            'count': len(urls),
            'preview': urls[:25],
            'snapshot': snapshot_meta,
            'skip_existing_count': len(already_filter),
            'skip_recent_hours': effective_hours,
            'summary': get_quota_summary(),
            'message': f'Готово до відправки: {len(urls) - len(already_filter)} URL '
                       f'(пропущено за {effective_hours} год: {len(already_filter)}).',
        })

    quota_limit = None
    if respect_quota:
        summary = get_quota_summary()
        remaining = max(0, int(summary.get('remaining_quota') or 0))
        quota_limit = remaining

    result = submit_google_indexing_urls(
        urls,
        notification_type=notification,
        source='admin',
        skip_already_submitted_today=skip_existing,
        skip_recent_success_days=skip_recent_days,
        skip_recent_success_hours=skip_recent_hours,
        quota_limit=quota_limit,
    )
    return JsonResponse(
        {
            'success': bool(result.get('ok')),
            'count': result.get('total', len(urls)),
            'submitted': result.get('submitted', 0),
            'skipped_already': result.get('skipped_already', 0),
            'skipped_quota': result.get('skipped_quota', 0),
            'failures': result.get('failures', []),
            'notification': notification,
            'snapshot': snapshot_meta,
            'summary': get_quota_summary(),
            'message': result.get(
                'message',
                f'Google Indexing API: {result.get("submitted", 0)}/{len(urls)} URL.',
            ),
        }
    )


@staff_member_required
def admin_google_indexing_preview(request):
    """GET endpoint: preview the URL set produced by groups/languages.

    Used by the SEO dashboard so the operator can see exactly how many
    URLs (and which) will be submitted before the actual quota burn.

    Query params:
        ``groups``            — comma-separated subset of ALL_GROUPS.
        ``languages``         — comma-separated subset of LANGUAGES.
        ``skip_recent_hours`` — rolling window (0-1440) of hours to
                                dedupe successful submissions against.
                                Falls back to ``skip_recent_days*24``
                                for legacy clients.
    """
    if request.method != 'GET':
        return JsonResponse({'success': False, 'error': 'Method not allowed'}, status=405)

    groups_raw = (request.GET.get('groups') or '').strip()
    langs_raw = (request.GET.get('languages') or '').strip()
    groups = [g.strip() for g in groups_raw.split(',') if g.strip()] or list(ALL_GROUPS)
    languages = [l.strip() for l in langs_raw.split(',') if l.strip()] or get_supported_languages()
    try:
        skip_recent_hours = int(request.GET.get('skip_recent_hours') or 0)
    except (TypeError, ValueError):
        skip_recent_hours = 0
    if not skip_recent_hours:
        try:
            days_legacy = int(request.GET.get('skip_recent_days') or 0)
        except (TypeError, ValueError):
            days_legacy = 0
        skip_recent_hours = max(0, days_legacy) * 24
    skip_recent_hours = max(0, min(1440, skip_recent_hours))

    snapshot = build_targets(groups=groups, languages=languages)
    all_urls = snapshot.all_urls
    quota_window = get_quota_window_hours()
    already_window_24h = get_urls_successful_in_last_hours(
        all_urls,
        hours=quota_window,
        notification_type=NOTIFICATION_URL_UPDATED,
    )
    if skip_recent_hours > 0:
        already_window = get_urls_successful_in_last_hours(
            all_urls,
            hours=skip_recent_hours,
            notification_type=NOTIFICATION_URL_UPDATED,
        )
    else:
        already_window = set(already_window_24h)

    pending = [u for u in all_urls if u not in already_window]

    result = snapshot.to_dict(preview_limit=20)
    result.update({
        'success': True,
        'configured': is_google_indexing_configured(),
        'group_labels': GROUP_LABELS,
        'supported_languages': get_supported_languages(),
        'default_language': get_default_language(),
        'already_submitted_today': len(already_window_24h),
        'already_in_window': len(already_window),
        'skip_recent_hours': skip_recent_hours,
        'quota_window_hours': quota_window,
        'pending_count': len(pending),
        'pending_preview': pending[:20],
        'summary': get_quota_summary(),
    })
    return JsonResponse(result)


@staff_member_required
def admin_google_indexing_resolve(request):
    """POST endpoint: resolve the final URL list to submit.

    Returns an *exact* deduped list of URLs ready to be sent to Google,
    after applying the ``skip_recent_days`` rotation window and the
    per-quota cap. The JS panel uses this to drive a smooth chunked
    progress bar — instead of sending one giant request and waiting
    30-60 s for a single response, it pulls the resolved list, then
    submits in chunks of N URLs and animates progress between calls.

    Body (JSON):
        {
            "groups": [...],
            "languages": [...],
            "skip_recent_days": int,
            "respect_quota": bool,
            "notification": "URL_UPDATED" | "URL_DELETED",
            "limit": int (cap on returned URL count)
        }
    """
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Method not allowed'}, status=405)

    try:
        payload = json.loads(request.body.decode('utf-8') or '{}')
    except Exception:
        return JsonResponse({'success': False, 'error': 'Invalid JSON'}, status=400)

    groups = payload.get('groups') or list(ALL_GROUPS)
    languages = payload.get('languages') or get_supported_languages()
    notification = (payload.get('notification') or NOTIFICATION_URL_UPDATED).strip().upper()
    if notification not in {NOTIFICATION_URL_UPDATED, NOTIFICATION_URL_DELETED}:
        return JsonResponse(
            {'success': False, 'error': f'Unknown notification: {notification}'},
            status=400,
        )

    try:
        skip_recent_hours = int(payload.get('skip_recent_hours') or 0)
    except (TypeError, ValueError):
        skip_recent_hours = 0
    if not skip_recent_hours:
        try:
            legacy_days = int(payload.get('skip_recent_days') or 0)
        except (TypeError, ValueError):
            legacy_days = 0
        skip_recent_hours = max(0, legacy_days) * 24
    skip_recent_hours = max(0, min(1440, skip_recent_hours))

    respect_quota = bool(payload.get('respect_quota'))
    explicit_urls = payload.get('urls') or None

    if isinstance(explicit_urls, list) and explicit_urls:
        all_urls = [str(u).strip() for u in explicit_urls if str(u).strip()]
        snapshot_meta = {'mode': 'explicit', 'count': len(all_urls)}
    else:
        snapshot = build_targets(groups=groups, languages=languages)
        all_urls = snapshot.all_urls
        snapshot_meta = snapshot.to_dict(preview_limit=10)

    all_urls = list(dict.fromkeys(all_urls))

    # Filter by rotation window (rolling hours).
    skipped_recent = 0
    if skip_recent_hours > 0:
        already = get_urls_successful_in_last_hours(
            all_urls, hours=skip_recent_hours, notification_type=notification,
        )
        if already:
            skipped_recent = len(already)
            all_urls = [u for u in all_urls if u not in already]

    # Cap by current rolling-quota remaining.
    summary = get_quota_summary()
    remaining = max(0, int(summary.get('remaining_quota') or 0))
    skipped_quota = 0
    if respect_quota and remaining < len(all_urls):
        skipped_quota = len(all_urls) - remaining
        all_urls = all_urls[:remaining]

    # Optional hard cap via ``limit``.
    try:
        limit = int(payload.get('limit') or 0)
    except (TypeError, ValueError):
        limit = 0
    if limit > 0 and len(all_urls) > limit:
        skipped_quota += len(all_urls) - limit
        all_urls = all_urls[:limit]

    return JsonResponse({
        'success': True,
        'count': len(all_urls),
        'urls': all_urls,
        'snapshot': snapshot_meta,
        'skipped_recent': skipped_recent,
        'skipped_quota': skipped_quota,
        'skip_recent_hours': skip_recent_hours,
        'remaining_quota': remaining,
        'can_submit_now': bool(summary.get('can_submit_now')),
        'quota_resets_at': summary.get('quota_resets_at'),
        'next_slot_in_seconds': summary.get('next_slot_in_seconds'),
        'summary': summary,
    })


@staff_member_required
def admin_google_indexing_history(request):
    """JSON endpoint for the SEO dashboard "Indexing log" widget.

    Returns today's quota stats and the most recent submissions so the
    admin can see at a glance which URLs were accepted, which failed,
    and how many quota slots are still available.

    Query params:
        ``limit``    — page size (default 50, max 200).
        ``status``   — ``success`` / ``failed`` / blank (all).
        ``date``     — ``YYYY-MM-DD`` to scope to a specific date.
        ``q``        — substring filter on URL.
        ``source``   — ``admin`` / ``signal`` / ``cron`` / ``manual``.
    """
    if request.method != 'GET':
        return JsonResponse({'success': False, 'error': 'Method not allowed'}, status=405)

    try:
        limit = int(request.GET.get('limit') or 50)
    except (TypeError, ValueError):
        limit = 50
    limit = max(1, min(200, limit))

    status_filter = (request.GET.get('status') or '').strip().lower()
    date_filter = (request.GET.get('date') or '').strip()
    q_filter = (request.GET.get('q') or '').strip()
    source_filter = (request.GET.get('source') or '').strip().lower()

    from storefront.models import GoogleIndexingSubmission
    qs = GoogleIndexingSubmission.objects.order_by('-submitted_at')
    if status_filter in {'success', 'failed'}:
        qs = qs.filter(status=status_filter)
    if date_filter:
        try:
            from datetime import datetime
            d = datetime.strptime(date_filter, '%Y-%m-%d').date()
            qs = qs.filter(submission_date=d)
        except ValueError:
            pass
    if q_filter:
        qs = qs.filter(url__icontains=q_filter)
    if source_filter:
        qs = qs.filter(source__iexact=source_filter)

    rows = list(qs.values(
        'id', 'url', 'status', 'http_status',
        'notification_type', 'submitted_at', 'submission_date',
        'source', 'error_message',
    )[:limit])
    recent = []
    for row in rows:
        recent.append({
            'id': row['id'],
            'url': row['url'],
            'status': row['status'],
            'http_status': row['http_status'],
            'notification_type': row['notification_type'],
            'submitted_at': row['submitted_at'].isoformat() if row['submitted_at'] else None,
            'submission_date': str(row['submission_date']) if row['submission_date'] else None,
            'source': row['source'],
            'error_message': (row['error_message'] or '')[:300],
        })

    summary = get_quota_summary()

    # Per-group breakdown over the rolling quota window (default 24h).
    group_breakdown = []
    from storefront.services.index_targets import (
        ALL_GROUPS as TGT_GROUPS,
        GROUP_LABELS as TGT_LABELS,
        build_targets,
    )
    from datetime import timedelta
    from django.utils import timezone as djtz
    window_hours = int(summary.get('window_hours') or 24)
    cutoff = djtz.now() - timedelta(hours=window_hours)
    snapshot = build_targets()
    for group in TGT_GROUPS:
        urls = snapshot.per_group.get(group) or []
        if not urls:
            continue
        sent_in_window = (
            GoogleIndexingSubmission.objects
            .filter(submitted_at__gte=cutoff, url__in=urls, status='success')
            .values('url').distinct().count()
        )
        group_breakdown.append({
            'group': group,
            'label': TGT_LABELS.get(group, group),
            'total': len(urls),
            # Keep ``submitted_today`` / ``pending_today`` for legacy
            # JS that still reads those keys; the numbers now reflect
            # the rolling 24h window.
            'submitted_today': sent_in_window,
            'pending_today': max(0, len(urls) - sent_in_window),
            'submitted_in_window': sent_in_window,
            'pending_in_window': max(0, len(urls) - sent_in_window),
        })

    return JsonResponse({
        'success': True,
        'summary': summary,
        'recent': recent,
        'configured': is_google_indexing_configured(),
        'status': get_google_indexing_status(),
        'group_breakdown': group_breakdown,
        'filters': {
            'status': status_filter,
            'date': date_filter,
            'q': q_filter,
            'source': source_filter,
            'limit': limit,
        },
    })


@staff_member_required
def admin_seo_ai_generate(request):
    """Phase 11 — AJAX endpoint: regenerate AI keywords/description for one
    Product or Category from the SEO admin section.

    POST JSON body:
        {"target": "product"|"category", "id": <int>}

    Calls the existing ``generate_ai_content_for_*_task`` *body* directly
    (sync) because the production host has no Celery worker. Returns the
    refreshed object snapshot so the dashboard can update without a full
    page reload.
    """
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Method not allowed'}, status=405)

    try:
        payload = json.loads(request.body.decode('utf-8') or '{}')
    except Exception:
        return JsonResponse({'success': False, 'error': 'Invalid JSON'}, status=400)

    target = (payload.get('target') or '').strip().lower()
    try:
        obj_id = int(payload.get('id'))
    except (TypeError, ValueError):
        return JsonResponse({'success': False, 'error': 'Invalid id'}, status=400)

    api_key = getattr(settings, 'OPENAI_API_KEY', None) or os.environ.get('OPENAI_API_KEY')
    if not api_key:
        return JsonResponse(
            {'success': False, 'error': 'OPENAI_API_KEY не задано на сервері.'},
            status=503,
        )
    if not (getattr(settings, 'USE_AI_KEYWORDS', False) or
            getattr(settings, 'USE_AI_DESCRIPTIONS', False)):
        return JsonResponse(
            {
                'success': False,
                'error': 'AI вимкнено: USE_AI_KEYWORDS і USE_AI_DESCRIPTIONS = False.',
            },
            status=503,
        )

    from storefront.tasks import (
        generate_ai_content_for_category_task,
        generate_ai_content_for_product_task,
    )
    from ..models import Category as _Category

    if target == 'product':
        obj = Product.objects.filter(pk=obj_id).first()
        if not obj:
            return JsonResponse({'success': False, 'error': 'Product not found'}, status=404)
        if not obj.ai_generation_enabled:
            return JsonResponse(
                {'success': False, 'error': 'AI вимкнено для цього товару.'},
                status=400,
            )
        # Underlying function — runs sync (no Celery on prod, see Phase 2).
        try:
            generate_ai_content_for_product_task(obj.id)
        except Exception as exc:  # pragma: no cover - defensive
            return JsonResponse({'success': False, 'error': str(exc)[:300]}, status=500)
        obj.refresh_from_db()
        return JsonResponse({
            'success': True,
            'target': 'product',
            'id': obj.id,
            'ai_content_generated': bool(obj.ai_content_generated),
            'ai_keywords': (obj.ai_keywords or '')[:160],
            'ai_description': (obj.ai_description or '')[:300],
        })

    if target == 'category':
        obj = _Category.objects.filter(pk=obj_id).first()
        if not obj:
            return JsonResponse({'success': False, 'error': 'Category not found'}, status=404)
        if not obj.ai_generation_enabled:
            return JsonResponse(
                {'success': False, 'error': 'AI вимкнено для цієї категорії.'},
                status=400,
            )
        try:
            generate_ai_content_for_category_task(obj.id)
        except Exception as exc:  # pragma: no cover - defensive
            return JsonResponse({'success': False, 'error': str(exc)[:300]}, status=500)
        obj.refresh_from_db()
        return JsonResponse({
            'success': True,
            'target': 'category',
            'id': obj.id,
            'ai_content_generated': bool(obj.ai_content_generated),
            'ai_keywords': (obj.ai_keywords or '')[:160],
            'ai_description': (obj.ai_description or '')[:300],
        })

    return JsonResponse(
        {'success': False, 'error': 'Unknown target (use product|category)'},
        status=400,
    )


def _build_dispatcher_context(request):
    """
    Собирает данные для секции 'Диспетчер' (UTM Analytics).

    Показывает детальную аналитику по UTM-меткам:
    - Общая статистика (сессии, конверсии, доход)
    - Статистика по источникам трафика
    - Статистика по кампаниям
    - Статистика по креативам/контенту
    - Воронка конверсий
    - Геолокация (страны, города)
    - Устройства и браузеры
    - Повторные визиты
    - Последние сессии
    """
    from ..utm_analytics import (
        get_general_stats,
        get_sources_stats,
        get_campaigns_stats,
        get_content_stats,
        get_funnel_stats,
        get_geo_stats,
        get_device_stats,
        get_browser_stats,
        get_os_stats,
        get_returning_visitors_stats,
        get_recent_sessions,
    )
    from ..utm_cohort_analysis import (
        get_source_ltv_comparison,
        get_repeat_purchase_rate,
        get_cohort_analysis,
    )

    # Получаем параметры фильтрации
    period = request.GET.get('period', 'today')
    source_filter = request.GET.get('source', None)
    campaign_filter = request.GET.get('campaign', None)

    cohort_metric = request.GET.get('cohort_metric', 'retention')
    cohort_type = request.GET.get('cohort_type', 'week')
    cohort_metric_options = [
        {'value': 'retention', 'label': 'Retention (%)'},
        {'value': 'ltv', 'label': 'LTV (₴)'},
        {'value': 'orders', 'label': 'Замовлення'},
        {'value': 'revenue', 'label': 'Дохід (₴)'},
    ]
    cohort_type_options = [
        {'value': 'day', 'label': 'День'},
        {'value': 'week', 'label': 'Тиждень'},
        {'value': 'month', 'label': 'Місяць'},
    ]
    cohort_end = timezone.now()
    cohort_start = cohort_end - timedelta(days=90)

    # Собираем данные
    try:
        cohort_analysis = get_cohort_analysis(
            start_date=cohort_start,
            end_date=cohort_end,
            cohort_type=cohort_type,
            metric=cohort_metric,
            utm_source=source_filter or None,
        )

        context = {
            'period': period,
            'source_filter': source_filter,
            'campaign_filter': campaign_filter,

            # Общая статистика
            'general_stats': get_general_stats(period),

            # Источники трафика
            'sources_stats': get_sources_stats(period, limit=20),

            # Кампании
            'campaigns_stats': get_campaigns_stats(period, source_filter=source_filter, limit=20),

            # Креативы/контент
            'content_stats': get_content_stats(period, campaign_filter=campaign_filter, limit=20),

            # Воронка конверсий
            'funnel_stats': get_funnel_stats(period),

            # Геолокация
            'geo_stats': get_geo_stats(period, limit=15),

            # Устройства
            'device_stats': get_device_stats(period),

            # Браузеры
            'browser_stats': get_browser_stats(period, limit=10),

            # ОС
            'os_stats': get_os_stats(period, limit=10),

            # Повторные визиты
            'returning_stats': get_returning_visitors_stats(period),

            # Последние сессии
            'recent_sessions': get_recent_sessions(period, limit=50),

            # LTV сравнение (топ-5)
            'ltv_comparison': get_source_ltv_comparison(period)[:5],

            # Повторные покупки
            'repeat_rate': get_repeat_purchase_rate(period),

            # Когортный анализ
            'cohort_analysis': cohort_analysis,
            'cohort_metric': cohort_metric,
            'cohort_type': cohort_type,
            'cohort_metrics': cohort_metric_options,
            'cohort_types': cohort_type_options,
            'cohort_range': {
                'start': cohort_start.date(),
                'end': cohort_end.date(),
            },

            # Периоды для фильтра
            'periods': [
                {'value': 'today', 'label': 'Сьогодні'},
                {'value': 'week', 'label': 'Тиждень'},
                {'value': 'month', 'label': 'Місяць'},
                {'value': 'all_time', 'label': 'Весь час'},
            ],
        }

    except Exception as e:
        # В случае ошибки возвращаем пустой контекст с сообщением об ошибке
        import logging
        logger = logging.getLogger(__name__)
        logger.error(f"Error building dispatcher context: {e}", exc_info=True)

        context = {
            'period': period,
            'error': str(e),
            'general_stats': {
                'total_sessions': 0,
                'unique_visitors': 0,
                'total_conversions': 0,
                'conversion_rate': 0,
                'total_revenue': 0,
                'avg_order_value': 0,
                'total_score': 0,
                'avg_score_per_session': 0,
            },
            'sources_stats': [],
            'campaigns_stats': [],
            'content_stats': [],
            'funnel_stats': {
                'total': 0,
                'product_views': 0,
                'add_to_cart': 0,
                'initiate_checkout': 0,
                'leads': 0,
                'purchases': 0,
            },
            'geo_stats': {'countries': [], 'cities': []},
            'device_stats': [],
            'browser_stats': [],
            'os_stats': [],
            'returning_stats': {
                'total_sessions': 0,
                'first_visits': 0,
                'returning_visits': 0,
            },
            'recent_sessions': [],
            'ltv_comparison': [],
            'repeat_rate': {
                'total_customers': 0,
                'repeat_customers': 0,
                'one_time_customers': 0,
                'repeat_rate': 0,
                'avg_orders_per_customer': 0,
                'avg_time_between_orders': 0,
                'total_orders': 0
            },
            'cohort_analysis': {
                'cohorts': [],
                'periods': [],
                'matrix': [],
                'totals': [],
                'metric': cohort_metric,
                'cohort_type': cohort_type,
            },
            'cohort_metric': cohort_metric,
            'cohort_type': cohort_type,
            'cohort_metrics': cohort_metric_options,
            'cohort_types': cohort_type_options,
            'cohort_range': {
                'start': cohort_start.date(),
                'end': cohort_end.date(),
            },
            'periods': [
                {'value': 'today', 'label': 'Сьогодні'},
                {'value': 'week', 'label': 'Тиждень'},
                {'value': 'month', 'label': 'Місяць'},
                {'value': 'all_time', 'label': 'Весь час'},
            ],
        }

    return context


# ---------------------------------------------------------------------------
# Phase 21 (PR-A1) — review moderation endpoints for the custom admin.
# All accept POST only and return JSON; the admin_panel template wires
# them up via fetch() in admin_reviews.html. Status transitions go
# through ``Review.mark_*`` so signals (Telegram / IndexNow on first
# approval) fire exactly once.
# ---------------------------------------------------------------------------

@staff_member_required
def admin_review_action(request, review_id: int):
    """Approve or reject a single review.

    POST body (form-encoded): ``action`` ∈ {approve, reject},
    optional ``note`` for the moderator memo.
    """
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "POST required"}, status=405)

    from reviews.models import Review

    action = (request.POST.get("action") or "").strip().lower()
    if action not in {"approve", "reject"}:
        return JsonResponse({"ok": False, "error": "invalid action"}, status=400)

    review = get_object_or_404(Review, pk=review_id)
    note = (request.POST.get("note") or "").strip()

    if action == "approve":
        review.mark_approved(by=request.user, note=note)
    else:
        review.mark_rejected(by=request.user, note=note)

    return JsonResponse({"ok": True, "id": review.pk, "status": review.status})


@staff_member_required
def admin_review_bulk(request):
    """Bulk approve / reject. POST body must carry ``ids``
    (comma-separated) and ``action`` ∈ {approve, reject}.
    """
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "POST required"}, status=405)

    from reviews.models import Review, ReviewStatus

    action = (request.POST.get("action") or "").strip().lower()
    raw_ids = (request.POST.get("ids") or "").strip()
    if action not in {"approve", "reject"} or not raw_ids:
        return JsonResponse({"ok": False, "error": "invalid payload"}, status=400)

    try:
        ids = [int(x) for x in raw_ids.split(",") if x.strip().isdigit()]
    except ValueError:
        return JsonResponse({"ok": False, "error": "invalid ids"}, status=400)
    if not ids:
        return JsonResponse({"ok": False, "error": "no ids"}, status=400)

    target_status = ReviewStatus.APPROVED if action == "approve" else ReviewStatus.REJECTED
    qs = Review.objects.filter(pk__in=ids).exclude(status=target_status)

    # Walk row-by-row so signals fire (bulk update would skip them).
    updated = 0
    for review in qs:
        if action == "approve":
            review.mark_approved(by=request.user)
        else:
            review.mark_rejected(by=request.user)
        updated += 1

    return JsonResponse({"ok": True, "updated": updated})


@staff_member_required
def admin_color_seo_save(request, override_id: int):
    """Phase 21 (PR-A3) — save fields on a ``CatalogColorSeoOverride``
    row from the custom admin. POST body: ``h2``, ``body_html``,
    ``queries_json`` (raw JSON string), ``is_active`` (truthy).
    """
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "POST required"}, status=405)

    from storefront.models import CatalogColorSeoOverride

    row = get_object_or_404(CatalogColorSeoOverride, pk=override_id)

    raw_queries = (request.POST.get("queries_json") or "").strip()
    parsed_queries = []
    if raw_queries:
        try:
            parsed = json.loads(raw_queries)
        except json.JSONDecodeError as exc:
            return JsonResponse(
                {"ok": False, "error": f"invalid queries_json: {exc.msg}"},
                status=400,
            )
        if not isinstance(parsed, list):
            return JsonResponse({"ok": False, "error": "queries_json must be a list"}, status=400)
        parsed_queries = parsed

    row.h2 = (request.POST.get("h2") or "").strip()[:300]
    row.body_html = (request.POST.get("body_html") or "").strip()
    row.queries_json = parsed_queries
    row.is_active = request.POST.get("is_active") in ("1", "true", "on", "yes")
    row.save(update_fields=["h2", "body_html", "queries_json", "is_active", "updated_at"])

    return JsonResponse({
        "ok": True, "id": row.pk,
        "h2": row.h2, "is_active": row.is_active,
        "queries_count": len(row.queries_json or []),
    })


@staff_member_required
def admin_color_seo_create(request):
    """Create a new ``CatalogColorSeoOverride``. POST: ``scope``,
    optional ``color_slug``, ``category_id``. Idempotent on the
    unique tuple (``scope``, ``color_slug``, ``category_id``).
    """
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "POST required"}, status=405)

    from storefront.models import CatalogColorSeoOverride, Category

    scope = (request.POST.get("scope") or "").strip()
    valid_scopes = {c[0] for c in CatalogColorSeoOverride.SCOPE_CHOICES}
    if scope not in valid_scopes:
        return JsonResponse({"ok": False, "error": "invalid scope"}, status=400)

    color_slug = (request.POST.get("color_slug") or "").strip().lower()
    if scope in (CatalogColorSeoOverride.SCOPE_BRAND,
                 CatalogColorSeoOverride.SCOPE_CATEGORY) and not color_slug:
        return JsonResponse({"ok": False, "error": "color_slug required for this scope"}, status=400)

    category = None
    if scope == CatalogColorSeoOverride.SCOPE_CATEGORY:
        try:
            cat_id = int(request.POST.get("category_id") or 0)
        except (TypeError, ValueError):
            cat_id = 0
        if cat_id <= 0:
            return JsonResponse({"ok": False, "error": "category_id required"}, status=400)
        category = get_object_or_404(Category, pk=cat_id)

    obj, created = CatalogColorSeoOverride.objects.get_or_create(
        scope=scope, color_slug=color_slug, category=category,
        defaults={"is_active": True, "h2": "", "body_html": "", "queries_json": []},
    )
    return JsonResponse({
        "ok": True, "id": obj.pk, "created": created,
        "scope": obj.scope, "color_slug": obj.color_slug,
        "category_id": obj.category_id,
    })


@staff_member_required
def admin_color_seo_delete(request, override_id: int):
    """Hard-delete a ``CatalogColorSeoOverride`` row."""
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "POST required"}, status=405)

    from storefront.models import CatalogColorSeoOverride

    row = get_object_or_404(CatalogColorSeoOverride, pk=override_id)
    row_id = row.pk
    row.delete()
    return JsonResponse({"ok": True, "id": row_id, "deleted": True})


@staff_member_required
def admin_category_swatches_save(request):
    """Save ``Category.showcase_swatch_hexes`` for the catalog showcase
    cards. POST body: ``category_id``, ``swatches`` — comma-separated
    hex tokens (e.g. ``#000000,#ffffff,#7a8b3a``). Empty resets to ``[]``.
    """
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "POST required"}, status=405)

    try:
        cat_id = int(request.POST.get("category_id") or 0)
    except (TypeError, ValueError):
        cat_id = 0
    if cat_id <= 0:
        return JsonResponse({"ok": False, "error": "invalid id"}, status=400)

    from storefront.models import Category as _Category
    cat = get_object_or_404(_Category, pk=cat_id)

    raw = (request.POST.get("swatches") or "").strip()
    tokens: list[str] = []
    for token in raw.split(","):
        token = token.strip().lower()
        if not token:
            continue
        if not token.startswith("#"):
            token = "#" + token
        # Accept #abc / #aabbcc only — silently drop anything else so a
        # paste from the design tool doesn't poison the JSON column.
        if len(token) in (4, 7) and all(c in "#0123456789abcdef" for c in token):
            tokens.append(token)
    cat.showcase_swatch_hexes = tokens[:6]  # cap UI footprint
    cat.save(update_fields=["showcase_swatch_hexes"])
    return JsonResponse({"ok": True, "id": cat.pk, "swatches": cat.showcase_swatch_hexes})


@staff_member_required
def admin_seo_category_save(request):
    """Phase 21 (PR-A2) — save manual SEO overrides for a Category
    straight from the custom admin's SEO section, no Django-admin
    bounce. POST body: ``category_id``, ``seo_title``, ``seo_h1``,
    ``seo_description``.
    """
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "POST required"}, status=405)

    try:
        category_id = int(request.POST.get("category_id") or 0)
    except (TypeError, ValueError):
        category_id = 0
    if category_id <= 0:
        return JsonResponse({"ok": False, "error": "invalid id"}, status=400)

    from storefront.models import Category as _Category
    category = get_object_or_404(_Category, pk=category_id)

    # Cap lengths server-side; mirrors the model fields' max_length.
    category.seo_title = (request.POST.get("seo_title") or "").strip()[:180]
    category.seo_h1 = (request.POST.get("seo_h1") or "").strip()[:180]
    category.seo_description = (request.POST.get("seo_description") or "").strip()[:320]
    category.save(update_fields=["seo_title", "seo_h1", "seo_description"])

    return JsonResponse({
        "ok": True,
        "id": category.pk,
        "seo_title": category.seo_title,
        "seo_h1": category.seo_h1,
        "seo_description": category.seo_description,
    })
